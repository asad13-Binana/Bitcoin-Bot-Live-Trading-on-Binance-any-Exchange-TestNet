from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from Crypto.PublicKey import ECC

from services.common.evidence_signature import public_key_b64, sign_document
from services.common.market_policy import pair_config_hash, pair_state_hash
from services.common.strategy_fingerprint import fingerprints
from services.execution_sidecar import live_evidence as live
from services.execution_sidecar import main as sidecar_main


RELEASE = "a" * 64
STRATEGY_PATH = Path(__file__).resolve().parents[1] / "freqtrade/user_data/strategies/IctSmcStrategy.py"
FINGERPRINTS = fingerprints(
    STRATEGY_PATH, "IctSmcStrategy", list(live.SIGNAL_METHODS))
STRATEGY_SHA256 = hashlib.sha256(STRATEGY_PATH.read_bytes()).hexdigest()
QUOTES = ("USDT", "USDC")


class _LeaseState:
    def __init__(self):
        self.data = {}
        self.entries_enabled = True
        self.saved = 0

    def set_entries(self, enabled, reason=""):
        self.entries_enabled = bool(enabled)
        self.data["pause_reason"] = reason

    def save(self):
        self.saved += 1


class _LeaseAdapter:
    def __init__(self):
        self.enabled = True

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        return "ON" if self.enabled else "OFF"


def test_running_live_lease_expiry_disarms_and_latches_until_restart():
    state = _LeaseState()
    adapter = _LeaseAdapter()
    lease = {"expires_at": 100.0, "path": "unused", "sha256": "a" * 64}
    ok, status = sidecar_main.enforce_live_evidence_lease(
        adapter, state, lease, now=100.0, verify_signature=False
    )
    assert ok is False and "expired" in status
    assert adapter.enabled is False and state.entries_enabled is False
    assert state.data["live_evidence_failure_latched"] is True
    assert state.data["live_restart_required"] is True
    adapter.enabled = state.entries_enabled = True
    ok_again, status_again = sidecar_main.enforce_live_evidence_lease(
        adapter, state, lease, now=50.0, verify_signature=False
    )
    assert ok_again is False and "latched" in status_again
    assert adapter.enabled is False and state.entries_enabled is False


def _pair():
    state = {
        "schema_version": 1, "pair": "BTC/USDT", "symbol": "BTCUSDT",
        "base": "BTC", "quote": "USDT", "generation": 1,
    }
    state["pair_config_hash"] = pair_config_hash(state["pair"], QUOTES)
    state["state_hash"] = pair_state_hash(state)
    return state


def _result(**overrides):
    trades = [{
        "pair": "BTC/USDT", "fee_open": 0.001, "fee_close": 0.001,
    } for _ in range(100)]
    stats = {
        "strategy_name": "IctSmcStrategy", "trades": trades,
        "total_trades": 100, "profit_factor": 1.5, "profit_total": 0.08,
        "max_drawdown_account": 0.10, "timeframe": "1m",
        "enable_protections": True, "max_open_trades_setting": 1,
        "stake_currency": "USDT", "backtest_start": "2025-01-01 00:00:00",
        "backtest_end": "2025-06-01 00:00:00",
    }
    stats.update(overrides)
    return {"strategy": {"IctSmcStrategy": stats}}


def _write_export(path: Path, result: dict, *, strategy_source: str | None = None,
                  config_overrides: dict | None = None):
    config = {
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
        "trading_mode": "spot", "timeframe": "1m",
        "strategy": "IctSmcStrategy", "max_open_trades": 1,
        "stake_currency": "USDT", "enable_protections": True, "fee": 0.001,
    }
    config.update(config_overrides or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("backtest-result.json", json.dumps(result))
        archive.writestr("backtest-result_config.json", json.dumps(config))
        archive.writestr(
            "backtest-result_IctSmcStrategy.py",
            strategy_source if strategy_source is not None else STRATEGY_PATH.read_text(encoding="utf-8"))


def _signed_fixture(tmp_path, monkeypatch, *, result=None):
    monkeypatch.setattr(live, "_backtest_root", lambda: (tmp_path / "backtests").resolve())
    monkeypatch.setenv("ALLOWED_STABLE_QUOTES", ",".join(QUOTES))
    root = tmp_path / "backtests"
    root.mkdir()
    artifact = root / "result.zip"
    _write_export(artifact, result or _result())
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    backtest = {"artifact_file": artifact.name, "artifact_sha256": digest}
    metrics = live.verified_backtest(
        backtest, active_pair_state=_pair(), allowed_quotes=QUOTES,
        strategy_fingerprints=FINGERPRINTS, strategy_file_sha256=STRATEGY_SHA256)
    payload = {
        "release_hash": RELEASE,
        "strategy_fingerprints": FINGERPRINTS,
        "strategy_file_sha256": STRATEGY_SHA256,
        "active_pair_state_hash": pair_state_hash(_pair()),
        "allowed_stable_quotes": sorted(QUOTES),
        "risk_policy_sha256": live.risk_policy_sha256(QUOTES),
        "freqtrade_backtest": backtest,
        "verified_backtest_metrics": metrics,
        "assertions": {name: True for name in live.REQUIRED_ASSERTIONS},
    }
    key = ECC.generate(curve="Ed25519")
    monkeypatch.setenv("LIVE_EVIDENCE_PUBLIC_KEY", public_key_b64(key))
    document = sign_document(
        payload=payload, private_key=key, producer=live.EVIDENCE_PRODUCER,
        valid_seconds=86400)
    evidence = tmp_path / "LIVE_EVIDENCE.json"
    evidence.write_text(json.dumps(document), encoding="utf-8")
    return evidence, artifact, document


def _verify(path, *, min_remaining_seconds=0):
    return live.verify_live_evidence(
        release_hash=RELEASE, strategy_fingerprints=FINGERPRINTS,
        strategy_file_sha256=STRATEGY_SHA256,
        active_pair_state=_pair(), allowed_quotes=QUOTES, path=path,
        min_remaining_seconds=min_remaining_seconds)


def test_asymmetric_evidence_parses_exact_backtest(tmp_path, monkeypatch):
    evidence, _artifact, _document = _signed_fixture(tmp_path, monkeypatch)
    verified = _verify(evidence)
    assert verified["verified_backtest_metrics"]["trades"] == 100
    assert verified["verified_backtest_metrics"]["pair"] == "BTC/USDT"


def test_execution_host_cannot_modify_signed_evidence(tmp_path, monkeypatch):
    evidence, _artifact, document = _signed_fixture(tmp_path, monkeypatch)
    document["payload"]["assertions"][live.REQUIRED_ASSERTIONS[0]] = False
    evidence.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(live.LiveEvidenceError, match="signature"):
        _verify(evidence)


def test_deployment_margin_rejects_evidence_that_expires_during_rollback_window(
    tmp_path, monkeypatch
):
    evidence, _artifact, _document = _signed_fixture(tmp_path, monkeypatch)
    with pytest.raises(live.LiveEvidenceError, match="transaction margin"):
        _verify(evidence, min_remaining_seconds=2 * 86400)


def test_typed_metric_claim_cannot_override_artifact(tmp_path, monkeypatch):
    evidence, _artifact, document = _signed_fixture(tmp_path, monkeypatch)
    # Re-signing simulates a legitimate certifier mistake, not runtime forgery.
    key = ECC.generate(curve="Ed25519")
    monkeypatch.setenv("LIVE_EVIDENCE_PUBLIC_KEY", public_key_b64(key))
    document["payload"]["verified_backtest_metrics"]["profit_factor"] = 99.0
    resigned = sign_document(
        payload=document["payload"], private_key=key,
        producer=live.EVIDENCE_PRODUCER, valid_seconds=86400)
    evidence.write_text(json.dumps(resigned), encoding="utf-8")
    with pytest.raises(live.LiveEvidenceError, match="summary differs"):
        _verify(evidence)


def test_policy_change_invalidates_promotion(tmp_path, monkeypatch):
    evidence, _artifact, _document = _signed_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("TRADE_SIZE_QUOTE", "101")
    with pytest.raises(live.LiveEvidenceError, match="risk-policy"):
        _verify(evidence)


def test_backtest_byte_change_invalidates_promotion(tmp_path, monkeypatch):
    evidence, artifact, _document = _signed_fixture(tmp_path, monkeypatch)
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(live.LiveEvidenceError, match="bytes"):
        _verify(evidence)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"profit_factor": 1.0}, "profit factor"),
        ({"profit_total": -0.01}, "total profit"),
        ({"max_drawdown_account": 0.25}, "drawdown"),
        ({"enable_protections": False}, "protections"),
        ({"max_open_trades_setting": 2}, "max_open_trades"),
        ({"timeframe": "5m"}, "timeframe"),
    ],
)
def test_weak_backtest_is_not_certifiable(tmp_path, monkeypatch, override, message):
    monkeypatch.setattr(live, "_backtest_root", lambda: (tmp_path / "backtests").resolve())
    root = tmp_path / "backtests"
    root.mkdir()
    artifact = root / "weak.zip"
    _write_export(artifact, _result(**override))
    meta = {"artifact_file": artifact.name,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    with pytest.raises(live.LiveEvidenceError, match=message):
        live.verified_backtest(
            meta, active_pair_state=_pair(), allowed_quotes=QUOTES,
            strategy_fingerprints=FINGERPRINTS, strategy_file_sha256=STRATEGY_SHA256)


def test_backtest_embedded_strategy_must_match_release(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_backtest_root", lambda: (tmp_path / "backtests").resolve())
    root = tmp_path / "backtests"
    root.mkdir()
    artifact = root / "wrong-strategy.zip"
    source = STRATEGY_PATH.read_text(encoding="utf-8").replace("RVOL_MIN = 1.5", "RVOL_MIN = 9.5")
    _write_export(artifact, _result(), strategy_source=source)
    meta = {"artifact_file": artifact.name,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    with pytest.raises(live.LiveEvidenceError, match="differs from installed release"):
        live.verified_backtest(
            meta, active_pair_state=_pair(), allowed_quotes=QUOTES,
            strategy_fingerprints=FINGERPRINTS, strategy_file_sha256=STRATEGY_SHA256)
