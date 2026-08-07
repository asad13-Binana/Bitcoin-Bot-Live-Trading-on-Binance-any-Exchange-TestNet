from __future__ import annotations
"""Asymmetrically signed, exact-artifact live-promotion gate."""

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from services.common.evidence_signature import EvidenceSignatureError, verify_document
from services.common.market_policy import pair_state_hash
from services.common.paths import RUNTIME
from services.common.strategy_fingerprint import fingerprints_source


EVIDENCE_PRODUCER = "release-certifier"
REQUIRED_ASSERTIONS = (
    "binance_spot_testnet_lifecycle_passed",
    "oracle_fourteen_day_soak_completed",
    "partial_fill_and_restart_drills_passed",
    "cancel_replace_ambiguity_drills_passed",
    "freqtrade_lookahead_analysis_passed",
    "freqtrade_recursive_analysis_passed",
    "three_clean_release_passes_completed",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_EVIDENCE_BYTES = 256 * 1024
_MAX_BACKTEST_BYTES = 100 * 1024 * 1024
SIGNAL_METHODS = (
    "populate_indicators_5m", "populate_indicators",
    "populate_entry_trend", "populate_exit_trend",
)


class LiveEvidenceError(RuntimeError):
    pass


def evidence_path() -> Path:
    return Path(os.getenv("LIVE_EVIDENCE_FILE", str(RUNTIME / "LIVE_EVIDENCE.json")))


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().lower()
    if value not in {"true", "false"}:
        raise LiveEvidenceError(f"{name} must be true or false")
    return value == "true"


def _number_text(name: str, default: str) -> str:
    try:
        value = Decimal(os.getenv(name, default).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise LiveEvidenceError(f"{name} is not a finite decimal") from exc
    if not value.is_finite():
        raise LiveEvidenceError(f"{name} is not a finite decimal")
    return format(value.normalize(), "f")


def runtime_risk_policy(allowed_quotes) -> dict:
    """Canonical deployed policy bound into the offline approval signature."""
    entry_mode = os.getenv("PROTECTION_MODE", "OCO_TRAILING").strip().upper()
    if entry_mode not in {"FIXED_OCO", "OCO_TRAILING", "TRAILING_ONLY"}:
        raise LiveEvidenceError("PROTECTION_MODE is invalid")
    return {
        "policy_version": 1,
        "market": "BINANCE_SPOT",
        "base_asset": "BTC",
        "max_positions": 1,
        "allowed_stable_quotes": sorted(str(value).upper() for value in allowed_quotes),
        "trade_size_quote": _number_text("TRADE_SIZE_QUOTE", "100"),
        "max_entry_open_seconds": int(os.getenv("MAX_ENTRY_OPEN_SECONDS", "300")),
        "entry_protection_mode": entry_mode,
        "protection": {
            "take_profit_pct": _number_text("TAKE_PROFIT_PCT", "1.2"),
            "fixed_stop_pct": _number_text("FIXED_STOP_PCT", "2.0"),
            "trailing_delta_bips": int(os.getenv("TRAILING_DELTA_BIPS", "40")),
            "limit_fill_buffer_bips": int(os.getenv("LIMIT_FILL_BUFFER_BIPS", "20")),
            "fee_pct_per_side": _number_text("FEE_PCT_PER_SIDE", "0.1"),
            "break_even_slippage_pct": _number_text("BREAK_EVEN_SLIPPAGE_PCT", "0.05"),
            "spot_filter_max_age_seconds": int(os.getenv("SPOT_FILTER_MAX_AGE_SECONDS", "300")),
            "auto_enabled": _boolean("AUTO_PROTECTION_ENABLED", False),
            "auto_break_even_trigger_pct": _number_text("AUTO_BREAK_EVEN_TRIGGER_PCT", "0.5"),
            "auto_tight_trail_bips": int(os.getenv("AUTO_TIGHT_TRAIL_BIPS", "20")),
        },
        "freshness_and_stops": {
            "max_signal_age_seconds": int(os.getenv("MAX_SIGNAL_AGE_SECONDS", "180")),
            "max_candle_age_seconds": int(os.getenv("MAX_CANDLE_AGE_SECONDS", "180")),
            "pair_cooldown_seconds": int(os.getenv("PAIR_COOLDOWN_SECONDS", "60")),
            "max_stopouts_per_pair_day": int(os.getenv("MAX_STOPOUTS_PER_PAIR_DAY", "3")),
            "max_stopouts_global_day": int(os.getenv("MAX_STOPOUTS_GLOBAL_DAY", "10")),
        },
        "flow": {
            "require_flow_context": _boolean("REQUIRE_FLOW_CONTEXT", False),
            "require_matching_futures": _boolean("REQUIRE_MATCHING_FUTURES", False),
            "max_flow_age_seconds": int(os.getenv("MAX_FLOW_AGE_SECONDS", "45")),
            "min_taker_buy_ratio": _number_text("FLOW_MIN_TAKER_BUY_RATIO", "0.55"),
            "min_spot_imbalance": _number_text("FLOW_MIN_SPOT_IMBALANCE", "0.05"),
            "refresh_seconds": int(os.getenv("MONEYFLOW_REFRESH_SECONDS", "15")),
            "depth_limit": int(os.getenv("MONEYFLOW_DEPTH_LIMIT", "100")),
            "trade_limit": int(os.getenv("MONEYFLOW_TRADE_LIMIT", "500")),
            "depth_band_bps": int(os.getenv("MONEYFLOW_DEPTH_BAND_BPS", "10")),
        },
        "backtest_gate": {
            "minimum_trades": int(os.getenv("LIVE_MIN_BACKTEST_TRADES", "100")),
            "minimum_profit_factor": _number_text("LIVE_MIN_PROFIT_FACTOR", "1.15"),
            "minimum_profit_total": _number_text("LIVE_MIN_PROFIT_TOTAL", "0"),
            "maximum_drawdown_account": _number_text("LIVE_MAX_DRAWDOWN_ACCOUNT", "0.20"),
            "minimum_fee_per_side": _number_text("LIVE_MIN_BACKTEST_FEE", "0.001"),
        },
    }


def risk_policy_sha256(allowed_quotes) -> str:
    raw = json.dumps(runtime_risk_policy(allowed_quotes), sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _backtest_root() -> Path:
    return (RUNTIME / "backtests").resolve()


def _safe_backtest_target(relative: str) -> Path:
    if not relative or "\\" in relative:
        raise LiveEvidenceError("backtest artifact_file must be a relative POSIX path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.name in {"", "."}:
        raise LiveEvidenceError("backtest artifact path is unsafe")
    root = _backtest_root()
    target = (root / Path(*pure.parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise LiveEvidenceError("backtest artifact path escapes the runtime directory") from exc
    return target


def _read_result_document(target: Path) -> tuple[dict, dict, bytes]:
    if target.stat().st_size > _MAX_BACKTEST_BYTES:
        raise LiveEvidenceError("backtest artifact exceeds 100 MiB")
    if not zipfile.is_zipfile(target):
        raise LiveEvidenceError(
            "live promotion requires the official Freqtrade ZIP export with embedded strategy")
    candidates: list[dict] = []
    configs: list[dict] = []
    strategy_sources: list[bytes] = []
    with zipfile.ZipFile(target) as archive:
        names: set[str] = set()
        total = 0
        for item in archive.infolist():
            pure = PurePosixPath(item.filename)
            if (pure.is_absolute() or ".." in pure.parts or "\\" in item.filename
                    or item.filename in names):
                raise LiveEvidenceError("backtest ZIP contains an unsafe or duplicate path")
            names.add(item.filename)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise LiveEvidenceError("backtest ZIP contains a symlink")
            total += int(item.file_size)
            if item.file_size > _MAX_BACKTEST_BYTES or total > 2 * _MAX_BACKTEST_BYTES:
                raise LiveEvidenceError("backtest ZIP expands beyond the allowed size")
            if item.is_dir():
                continue
            lower = item.filename.lower()
            if lower.endswith("_ictsmcstrategy.py"):
                try:
                    source_bytes = archive.read(item)
                    source_bytes.decode("utf-8")
                    strategy_sources.append(source_bytes)
                except Exception as exc:
                    raise LiveEvidenceError("embedded strategy is not valid UTF-8 source") from exc
                continue
            if not lower.endswith(".json"):
                continue
            try:
                value = json.loads(archive.read(item).decode("utf-8"))
            except Exception:
                continue
            if lower.endswith("_config.json") and isinstance(value, dict):
                configs.append(value)
            if isinstance(value, dict) and isinstance(value.get("strategy"), dict):
                if "IctSmcStrategy" in value["strategy"]:
                    candidates.append(value)
    if len(candidates) != 1 or len(configs) != 1 or len(strategy_sources) != 1:
        raise LiveEvidenceError(
            "backtest ZIP must contain exactly one result, sanitized config, and IctSmcStrategy source")
    return candidates[0], configs[0], strategy_sources[0]


def _finite(stats: dict, name: str) -> float:
    try:
        value = float(stats[name])
    except Exception as exc:
        raise LiveEvidenceError(f"backtest metric {name} is missing or invalid") from exc
    if not math.isfinite(value):
        raise LiveEvidenceError(f"backtest metric {name} is not finite")
    return value


def verified_backtest(backtest: dict, *, active_pair_state: dict,
                      allowed_quotes, strategy_fingerprints: dict,
                      strategy_file_sha256: str) -> dict:
    if not isinstance(backtest, dict):
        raise LiveEvidenceError("live evidence lacks backtest artifact metadata")
    expected = str(backtest.get("artifact_sha256", ""))
    if not _HEX64.fullmatch(expected):
        raise LiveEvidenceError("backtest evidence lacks a valid artifact sha256")
    target = _safe_backtest_target(str(backtest.get("artifact_file", "")))
    if not target.is_file():
        raise LiveEvidenceError(f"backtest artifact is missing: {target}")
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual != expected:
        raise LiveEvidenceError("backtest artifact bytes do not match signed evidence")
    result, config, embedded_source_bytes = _read_result_document(target)
    if hashlib.sha256(embedded_source_bytes).hexdigest() != strategy_file_sha256:
        raise LiveEvidenceError("embedded full strategy file differs from installed release")
    try:
        embedded_fingerprints = fingerprints_source(
            embedded_source_bytes.decode("utf-8"), "IctSmcStrategy", list(SIGNAL_METHODS))
    except Exception as exc:
        raise LiveEvidenceError("embedded strategy source is malformed") from exc
    if embedded_fingerprints != strategy_fingerprints:
        raise LiveEvidenceError("embedded backtest strategy differs from installed release")
    strategies = result.get("strategy") or {}
    stats = strategies.get("IctSmcStrategy")
    if not isinstance(stats, dict):
        raise LiveEvidenceError("backtest result is not for IctSmcStrategy")
    trades = int(stats.get("total_trades", -1))
    profit_factor = _finite(stats, "profit_factor")
    profit_total = _finite(stats, "profit_total")
    drawdown = _finite(stats, "max_drawdown_account")
    gate = runtime_risk_policy(allowed_quotes)["backtest_gate"]
    if trades < int(gate["minimum_trades"]):
        raise LiveEvidenceError("backtest has too few trades")
    if profit_factor <= float(gate["minimum_profit_factor"]):
        raise LiveEvidenceError("backtest profit factor does not pass the configured gate")
    if profit_total <= float(gate["minimum_profit_total"]):
        raise LiveEvidenceError("backtest total profit does not pass the configured gate")
    if drawdown >= float(gate["maximum_drawdown_account"]):
        raise LiveEvidenceError("backtest account drawdown does not pass the configured gate")
    if stats.get("timeframe") != "1m":
        raise LiveEvidenceError("backtest timeframe is not 1m")
    if stats.get("strategy_name") not in {None, "IctSmcStrategy"}:
        raise LiveEvidenceError("backtest strategy_name is inconsistent")
    if stats.get("enable_protections") is not True:
        raise LiveEvidenceError("backtest did not enable protections")
    if int(stats.get("max_open_trades_setting", -1)) != 1:
        raise LiveEvidenceError("backtest max_open_trades_setting is not 1")
    pair = str(active_pair_state.get("pair", ""))
    quote = str(active_pair_state.get("quote", ""))
    if stats.get("stake_currency") != quote:
        raise LiveEvidenceError("backtest stake currency does not match the active pair")
    rows = stats.get("trades")
    if not isinstance(rows, list) or len(rows) != trades:
        raise LiveEvidenceError("backtest trade ledger is missing or count-mismatched")
    minimum_fee = float(gate["minimum_fee_per_side"])
    for row in rows:
        if not isinstance(row, dict) or row.get("pair") != pair:
            raise LiveEvidenceError("backtest contains a trade outside the active BTC pair")
        if _finite(row, "fee_open") < minimum_fee or _finite(row, "fee_close") < minimum_fee:
            raise LiveEvidenceError("backtest fee assumption is below the configured minimum")
    start, end = str(stats.get("backtest_start", "")).strip(), str(stats.get("backtest_end", "")).strip()
    if not start or not end or start >= end:
        raise LiveEvidenceError("backtest date range is missing or invalid")
    exchange = config.get("exchange") if isinstance(config.get("exchange"), dict) else {}
    if str(exchange.get("name", "")).lower() != "binance":
        raise LiveEvidenceError("backtest config exchange is not Binance")
    if config.get("trading_mode") != "spot" or config.get("timeframe") != "1m":
        raise LiveEvidenceError("backtest config is not 1m Spot")
    if str(config.get("strategy", "")) != "IctSmcStrategy":
        raise LiveEvidenceError("backtest config strategy is inconsistent")
    if int(config.get("max_open_trades", -1)) != 1:
        raise LiveEvidenceError("backtest config max_open_trades is not 1")
    if config.get("stake_currency") != quote:
        raise LiveEvidenceError("backtest config stake currency does not match active pair")
    if exchange.get("pair_whitelist") != [pair]:
        raise LiveEvidenceError("backtest config whitelist is not the one active BTC pair")
    if config.get("enable_protections") is not True:
        raise LiveEvidenceError("backtest config did not enable protections")
    configured_fee = _finite(config, "fee")
    if configured_fee < minimum_fee:
        raise LiveEvidenceError("backtest config fee is below the configured minimum")
    return {
        "artifact_sha256": actual,
        "strategy": "IctSmcStrategy",
        "pair": pair,
        "timeframe": "1m",
        "trades": trades,
        "profit_factor": profit_factor,
        "profit_total": profit_total,
        "max_drawdown_account": drawdown,
        "backtest_start": start,
        "backtest_end": end,
    }


def verify_live_evidence(*, release_hash: str, strategy_fingerprints: dict,
                         strategy_file_sha256: str,
                         active_pair_state: dict, allowed_quotes,
                         path: str | Path | None = None,
                         min_remaining_seconds: float = 0.0) -> dict:
    target = Path(path) if path else evidence_path()
    if target.is_symlink() or not target.is_file() or target.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise LiveEvidenceError("live evidence is missing or exceeds 256 KiB")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        payload = verify_document(
            document,
            expected_producer=EVIDENCE_PRODUCER,
            min_remaining_seconds=min_remaining_seconds,
        )
    except (OSError, json.JSONDecodeError, EvidenceSignatureError) as exc:
        raise LiveEvidenceError(f"live evidence signature rejected: {exc}") from exc
    if str(payload.get("release_hash", "")) != release_hash:
        raise LiveEvidenceError("live evidence is bound to a different release hash")
    if payload.get("strategy_fingerprints") != strategy_fingerprints:
        raise LiveEvidenceError("strategy fingerprints do not match installed strategy")
    if payload.get("strategy_file_sha256") != strategy_file_sha256:
        raise LiveEvidenceError("live evidence full-strategy binding does not match installation")
    if str(payload.get("active_pair_state_hash", "")) != pair_state_hash(active_pair_state):
        raise LiveEvidenceError("live evidence is bound to a different active pair generation")
    expected_quotes = sorted(str(value).upper() for value in allowed_quotes)
    if sorted(payload.get("allowed_stable_quotes") or []) != expected_quotes:
        raise LiveEvidenceError("live evidence stable-quote policy does not match the installation")
    expected_policy = risk_policy_sha256(allowed_quotes)
    if payload.get("risk_policy_sha256") != expected_policy:
        raise LiveEvidenceError("live evidence risk-policy binding does not match deployment")
    metrics = verified_backtest(
        payload.get("freqtrade_backtest"), active_pair_state=active_pair_state,
        allowed_quotes=allowed_quotes, strategy_fingerprints=strategy_fingerprints,
        strategy_file_sha256=strategy_file_sha256)
    if payload.get("verified_backtest_metrics") != metrics:
        raise LiveEvidenceError("signed backtest summary differs from parsed artifact metrics")
    assertions = payload.get("assertions")
    if not isinstance(assertions, dict):
        raise LiveEvidenceError("live evidence lacks assertions")
    for name in REQUIRED_ASSERTIONS:
        if assertions.get(name) is not True:
            raise LiveEvidenceError(f"live evidence assertion not satisfied: {name}")
    return dict(payload, verified_backtest_metrics=metrics)
