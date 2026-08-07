from __future__ import annotations
"""BTC money-flow polling service.

This service is read-only by construction.  Binance collection uses only public
Spot/USD-M endpoints. Optional CoinGecko/CoinMarketCap keys enrich a fixed BTC
snapshot, are isolated here, and never affect entry or protection decisions.
Binance failures are published as stale, non-bullish state.
"""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import time

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.config_bounds import env_float, env_int
from services.common.market_policy import allowed_quotes_from_env, validate_exchange_symbol
from services.common.paths import ACTIVE_PAIR_FILE, MONEYFLOW_FILE, RUNTIME
from services.execution_sidecar.pair_control import PairController
from services.moneyflow.analytics import aggregate_trade_flow, classify, depth_metrics, timeframe_context
from services.moneyflow.client import MoneyFlowClient
from services.moneyflow.external_context import ExternalContextManager


log = logging.getLogger("moneyflow")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "2h", "4h", "1d")


def collect(
    client: MoneyFlowClient,
    pair_state: dict,
    external_manager: ExternalContextManager | None = None,
) -> dict:
    symbol = pair_state["symbol"]
    depth_limit = env_int("MONEYFLOW_DEPTH_LIMIT", 100, 5, 1000)
    trade_limit = env_int("MONEYFLOW_TRADE_LIMIT", 500, 10, 1000)
    band_bps = env_int("MONEYFLOW_DEPTH_BAND_BPS", 10, 1, 500)
    metadata = client.spot_exchange_symbol(symbol)
    validate_exchange_symbol(pair_state["pair"], metadata, allowed_quotes_from_env())
    snapshot = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_at_epoch": time.time(),
        "pair": pair_state["pair"], "symbol": symbol,
        "pair_state_hash": pair_state["state_hash"],
        "spot": {
            "depth": depth_metrics(client.spot_depth(symbol, depth_limit), band_bps=band_bps),
            "trades": aggregate_trade_flow(client.spot_aggregate_trades(symbol, trade_limit)),
        },
        "timeframes": {},
        "futures": {"available": False, "same_symbol": symbol},
        "errors": [],
    }
    for timeframe in TIMEFRAMES:
        try:
            snapshot["timeframes"][timeframe] = timeframe_context(client.klines(symbol, timeframe, 64))
        except Exception as exc:
            snapshot["timeframes"][timeframe] = {"direction": "unavailable", "error": type(exc).__name__}
            snapshot["errors"].append(f"{timeframe}:{type(exc).__name__}")
    futures_meta = client.futures_exchange_symbol(symbol)
    if futures_meta and futures_meta.get("status") == "TRADING" and futures_meta.get("contractType") == "PERPETUAL":
        snapshot["futures"] = {
            "available": True, "same_symbol": symbol,
            "depth": depth_metrics(client.futures_depth(symbol, depth_limit), band_bps=band_bps),
            "taker": client.futures_taker(symbol),
            "open_interest": client.futures_open_interest(symbol),
            "premium": client.futures_premium(symbol),
        }
    else:
        snapshot["futures"]["reason"] = "matching USD-M perpetual is not available"
    snapshot["classification"] = classify(
        snapshot,
        min_taker_ratio=env_float("FLOW_MIN_TAKER_BUY_RATIO", 0.55, 0.0, 1.0),
        min_spot_imbalance=env_float("FLOW_MIN_SPOT_IMBALANCE", 0.05, -1.0, 1.0),
    )
    snapshot["ok"] = not snapshot["errors"]
    if not snapshot["ok"]:
        # A partial timeframe set is useful telemetry but never a bullish
        # authorization. This keeps both advisory and hard-gate consumers from
        # acting on a deceptively positive degraded snapshot.
        snapshot["classification"] = dict(
            snapshot["classification"], bullish=False, decision="UNAVAILABLE")
    if external_manager is not None:
        try:
            snapshot["external_context"] = external_manager.snapshot()
        except Exception as exc:
            # Optional third-party context can never degrade Binance health or
            # authorize/deny a trade. Publish only a bounded exception type.
            snapshot["external_context"] = {
                "schema_version": 1,
                "advisory_only": True,
                "affects_entry_decision": False,
                "base_asset": "BTC",
                "quote_currency": "USD",
                "status": "unavailable",
                "reason": type(exc).__name__,
                "providers": {},
            }
    return snapshot


def run_once(
    client: MoneyFlowClient | None = None,
    external_manager: ExternalContextManager | None = None,
) -> dict:
    pair_state = PairController(
        ACTIVE_PAIR_FILE,
        os.getenv("PAIRLIST_FILE", ACTIVE_PAIR_FILE.parent / "current_pairlist.json"),
        os.getenv("FREQTRADE_ACTIVE_CONFIG", ACTIVE_PAIR_FILE.parent / "freqtrade-active.json"),
    ).load()
    manager = external_manager or ExternalContextManager.from_env()
    snapshot = collect(client or MoneyFlowClient(), pair_state, manager)
    atomic_write_json(MONEYFLOW_FILE, snapshot)
    atomic_write_json(RUNTIME / "moneyflow_health.json", {
        "ok": snapshot["ok"], "ts": time.time(), "pair": snapshot["pair"],
        "futures_available": snapshot["futures"]["available"],
        "decision": snapshot["classification"]["decision"],
        "external_provider_status": {
            name: value.get("status")
            for name, value in (snapshot.get("external_context", {}).get("providers", {}) or {}).items()
            if isinstance(value, dict)
        },
    })
    return snapshot


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    interval = env_int("MONEYFLOW_REFRESH_SECONDS", 15, 5, 300)
    external_manager = ExternalContextManager.from_env()
    while True:
        try:
            snapshot = run_once(external_manager=external_manager)
            audit("moneyflow_snapshot", details={
                "pair": snapshot["pair"], "ok": snapshot["ok"],
                "decision": snapshot["classification"]["decision"],
                "futures_available": snapshot["futures"]["available"],
                "external_provider_status": {
                    name: value.get("status")
                    for name, value in (
                        snapshot.get("external_context", {}).get("providers", {}) or {}
                    ).items()
                    if isinstance(value, dict)
                },
            })
        except Exception as exc:
            log.exception("money-flow collection failed")
            previous = read_json(MONEYFLOW_FILE, {}) or {}
            failure = {
                "schema_version": 1, "ok": False, "generated_at_epoch": time.time(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "pair": previous.get("pair", ""), "pair_state_hash": previous.get("pair_state_hash", ""),
                "classification": {"bullish": False, "decision": "UNAVAILABLE"},
                "errors": [type(exc).__name__],
            }
            atomic_write_json(MONEYFLOW_FILE, failure)
            atomic_write_json(RUNTIME / "moneyflow_health.json", {
                "ok": False, "ts": time.time(), "error": type(exc).__name__})
            audit("moneyflow_collection_failed", severity="ERROR", details={"error": str(exc)})
        time.sleep(interval)


if __name__ == "__main__":
    main()
