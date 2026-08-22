from __future__ import annotations

"""BTC Spot-only money-flow service.

This service is read-only by construction.  Binance collection uses only public
Spot endpoints. Optional fixed-BTC CoinGecko/CoinMarketCap context can be made
an explicit, fail-closed confirmation gate; it never selects assets, sizes
orders, or receives trading credentials. Binance failures are published as
stale, non-bullish state.
"""

import logging
import os
import time
from datetime import datetime, timezone

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.config_bounds import env_choice, env_float, env_int
from services.common.market_policy import (
    allowed_quotes_from_env,
    validate_exchange_symbol,
)
from services.common.paths import ACTIVE_PAIR_FILE, MONEYFLOW_FILE, RUNTIME
from services.execution_sidecar.pair_control import PairController
from services.moneyflow.analytics import (
    aggregate_trade_flow,
    classify,
    depth_metrics,
    external_confluence,
    timeframe_context,
)
from services.moneyflow.client import MoneyFlowClient
from services.moneyflow.external_context import ExternalContextManager
from services.moneyflow.spot_stream import SpotMarketStream

log = logging.getLogger("moneyflow")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "2h", "4h", "1d")


def collect(
    client: MoneyFlowClient,
    pair_state: dict,
    external_manager: ExternalContextManager | None = None,
    spot_stream: SpotMarketStream | None = None,
) -> dict:
    symbol = pair_state["symbol"]
    depth_limit = env_int("MONEYFLOW_DEPTH_LIMIT", 100, 5, 1000)
    trade_limit = env_int("MONEYFLOW_TRADE_LIMIT", 500, 10, 1000)
    band_bps = env_int("MONEYFLOW_DEPTH_BAND_BPS", 10, 1, 500)
    metadata = client.spot_exchange_symbol(symbol)
    validate_exchange_symbol(pair_state["pair"], metadata, allowed_quotes_from_env())
    depth = depth_metrics(client.spot_depth(symbol, depth_limit), band_bps=band_bps)
    stream_snapshot = None
    if spot_stream is not None:
        spot_stream.ensure_symbol(symbol)
        stream_snapshot = spot_stream.snapshot()
    if stream_snapshot and stream_snapshot.get("ready"):
        trades = dict(stream_snapshot["flow"])
        trades.update({
            "source": "binance_spot_websocket",
            "windows": stream_snapshot["windows"],
        })
    else:
        trades = aggregate_trade_flow(client.spot_aggregate_trades(symbol, trade_limit))
        trades.update({
            "source": "binance_spot_rest_fallback",
            "windows": (stream_snapshot or {}).get("windows", {}),
        })
    snapshot = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_at_epoch": time.time(),
        "pair": pair_state["pair"], "symbol": symbol,
        "pair_state_hash": pair_state["state_hash"],
        "market_context_mode": "spot_only",
        "spot": {
            "depth": depth,
            "trades": trades,
            "stream": stream_snapshot or {
                "source": "binance_spot_websocket",
                "ready": False,
                "reason": "stream_not_configured",
            },
        },
        "timeframes": {},
        # Compatibility stub for old read-only monitors. No futures client,
        # hostname, request, metric, or classification input remains.
        "futures": {
            "available": False,
            "disabled": True,
            "reason": "spot_only_policy",
        },
        "errors": [],
    }
    for timeframe in TIMEFRAMES:
        try:
            snapshot["timeframes"][timeframe] = timeframe_context(client.klines(symbol, timeframe, 64))
        except Exception as exc:  # noqa: BLE001 - each public timeframe fails closed independently
            snapshot["timeframes"][timeframe] = {"direction": "unavailable", "error": type(exc).__name__}
            snapshot["errors"].append(f"{timeframe}:{type(exc).__name__}")
    require_external = env_choice(
        "REQUIRE_EXTERNAL_CONFLUENCE", "false", {"true", "false"}
    ) == "true"
    if external_manager is not None:
        try:
            snapshot["external_context"] = external_manager.snapshot()
        except Exception as exc:  # noqa: BLE001 - provider adapters must not break Spot collection
            snapshot["external_context"] = {
                "schema_version": 1,
                "base_asset": "BTC",
                "quote_currency": "USD",
                "status": "unavailable",
                "reason": type(exc).__name__,
                "providers": {},
            }
    else:
        snapshot["external_context"] = {
            "schema_version": 1,
            "base_asset": "BTC",
            "quote_currency": "USD",
            "status": "not_configured",
            "providers": {},
        }
    confluence = external_confluence(
        snapshot["external_context"],
        spot_mid=depth["mid"],
        minimum_providers=env_int("EXTERNAL_CONFLUENCE_MIN_PROVIDERS", 1, 1, 2),
        minimum_change_24h_pct=env_float(
            "EXTERNAL_CONFLUENCE_MIN_24H_CHANGE_PCT", 0.0, -100.0, 100.0
        ),
        maximum_price_deviation_bps=env_float(
            "EXTERNAL_CONFLUENCE_MAX_PRICE_DEVIATION_BPS", 100.0, 1.0, 1_000.0
        ),
    )
    snapshot["external_context"].update({
        "advisory_only": not require_external,
        "affects_entry_decision": require_external,
        "confluence": confluence,
    })
    if require_external and not confluence["confirmed"]:
        snapshot["errors"].append("external_confluence:not_confirmed")
    snapshot["classification"] = classify(
        snapshot,
        min_taker_ratio=env_float("FLOW_MIN_TAKER_BUY_RATIO", 0.55, 0.0, 1.0),
        min_spot_imbalance=env_float("FLOW_MIN_SPOT_IMBALANCE", 0.05, -1.0, 1.0),
        require_external_confluence=require_external,
    )
    snapshot["ok"] = not snapshot["errors"]
    if not snapshot["ok"]:
        # Partial or required-but-unconfirmed context is useful telemetry but
        # can never authorize a trade.
        snapshot["classification"] = dict(
            snapshot["classification"], bullish=False, decision="UNAVAILABLE")
    return snapshot


def run_once(
    client: MoneyFlowClient | None = None,
    external_manager: ExternalContextManager | None = None,
    spot_stream: SpotMarketStream | None = None,
) -> dict:
    pair_state = PairController(
        ACTIVE_PAIR_FILE,
        os.getenv("PAIRLIST_FILE", ACTIVE_PAIR_FILE.parent / "current_pairlist.json"),
        os.getenv("FREQTRADE_ACTIVE_CONFIG", ACTIVE_PAIR_FILE.parent / "freqtrade-active.json"),
    ).load()
    manager = external_manager or ExternalContextManager.from_env()
    snapshot = collect(
        client or MoneyFlowClient(),
        pair_state,
        manager,
        spot_stream,
    )
    atomic_write_json(MONEYFLOW_FILE, snapshot)
    atomic_write_json(RUNTIME / "moneyflow_health.json", {
        "ok": snapshot["ok"], "ts": time.time(), "pair": snapshot["pair"],
        "market_context_mode": "spot_only",
        "spot_stream_ready": bool((snapshot["spot"].get("stream") or {}).get("ready")),
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
    client = MoneyFlowClient()
    spot_stream = SpotMarketStream(
        stale_after_seconds=env_int("MONEYFLOW_SPOT_STREAM_STALE_SECONDS", 10, 2, 60)
    )
    try:
        while True:
            try:
                snapshot = run_once(
                    client=client,
                    external_manager=external_manager,
                    spot_stream=spot_stream,
                )
                audit("moneyflow_snapshot", details={
                    "pair": snapshot["pair"], "ok": snapshot["ok"],
                    "decision": snapshot["classification"]["decision"],
                    "market_context_mode": "spot_only",
                    "spot_flow_source": snapshot["spot"]["trades"]["source"],
                    "external_confluence": (
                        snapshot["external_context"].get("confluence") or {}
                    ).get("confirmed"),
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
                    "schema_version": 2, "ok": False, "generated_at_epoch": time.time(),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "market_context_mode": "spot_only",
                    "pair": previous.get("pair", ""),
                    "pair_state_hash": previous.get("pair_state_hash", ""),
                    "classification": {"bullish": False, "decision": "UNAVAILABLE"},
                    "errors": [type(exc).__name__],
                }
                atomic_write_json(MONEYFLOW_FILE, failure)
                atomic_write_json(RUNTIME / "moneyflow_health.json", {
                    "ok": False, "ts": time.time(), "error": type(exc).__name__})
                audit("moneyflow_collection_failed", severity="ERROR", details={"error": str(exc)})
            time.sleep(interval)
    finally:
        spot_stream.stop()


if __name__ == "__main__":
    main()
