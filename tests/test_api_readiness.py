from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import api_readiness  # noqa: E402


TOKEN = "123456789:" + "t" * 32


def environment(mode: str = "testnet") -> dict[str, str]:
    return {
        "EXECUTION_MODE": mode,
        "BOT_ENVIRONMENT": "TESTNET" if mode == "testnet" else "LIVE",
        "LIVE_TRADING_ENABLED": "false",
        "BINANCE_API_KEY": "k" * 32,
        "BINANCE_API_SECRET": "s" * 32,
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_OWNER_CHAT_ID": "123456789",
        "COINGECKO_CONTEXT_ENABLED": "false",
        "COINMARKETCAP_CONTEXT_ENABLED": "false",
    }


def test_testnet_identity_is_fixed_and_live_is_blocked_by_default():
    probe = api_readiness.ApiReadinessProbe(
        env=environment(), release_mode="testnet"
    )
    assert probe.base == api_readiness.TESTNET_BASE

    live = environment("simulation")
    with pytest.raises(api_readiness.ReadinessError, match="confirmation"):
        api_readiness.ApiReadinessProbe(env=live, release_mode="live")
    with pytest.raises(api_readiness.ReadinessError, match="package identity"):
        api_readiness.ApiReadinessProbe(
            env={**environment(), "BINANCE_REST_BASE": api_readiness.LIVE_BASE},
            release_mode="testnet",
        )


def test_live_probe_remains_simulation_only_and_entries_off():
    live = environment("simulation")
    probe = api_readiness.ApiReadinessProbe(
        env=live,
        release_mode="live",
        live_confirmation=api_readiness.LIVE_CONFIRMATION,
    )
    assert probe.base == api_readiness.LIVE_BASE
    with pytest.raises(api_readiness.ReadinessError, match="LIVE_TRADING_ENABLED"):
        api_readiness.ApiReadinessProbe(
            env={**live, "LIVE_TRADING_ENABLED": "true"},
            release_mode="live",
            live_confirmation=api_readiness.LIVE_CONFIRMATION,
        )


def test_report_is_get_only_and_contains_no_credentials(monkeypatch):
    probe = api_readiness.ApiReadinessProbe(
        env=environment(), release_mode="testnet"
    )
    server_ms = int(api_readiness.time.time() * 1000)

    def fake_request(service, url, *, params=None, headers=None):
        if url.endswith("/api/v3/time"):
            return {"serverTime": server_ms}
        if url.endswith("/api/v3/account"):
            assert headers["X-MBX-APIKEY"] == "k" * 32
            assert len(params["signature"]) == 64
            return {"canTrade": True, "balances": [{"asset": "SECRET"}]}
        if url.endswith("/api/v3/exchangeInfo"):
            return {"symbols": [{
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            }]}
        if url.endswith("/api/v3/openOrders"):
            return [{"orderId": 999, "clientOrderId": "SECRET"}]
        if url.endswith("/getMe"):
            return {"ok": True, "result": {"is_bot": True, "username": "secret"}}
        if url.endswith("/getChat"):
            return {"ok": True, "result": {"id": 123456789, "title": "secret"}}
        raise AssertionError(service)

    monkeypatch.setattr(probe, "_request_json", fake_request)
    report = probe.run()
    encoded = json.dumps(report)
    assert report["ok"] is True
    assert report["safety"] == {
        "http_methods": ["GET"],
        "orders_submitted": False,
        "telegram_messages_sent": False,
        "secrets_emitted": False,
    }
    for secret in (TOKEN, "k" * 32, "s" * 32, "clientOrderId", "SECRET"):
        assert secret not in encoded


def test_wrapper_requires_root_trusted_env_and_literal_parser():
    wrapper = (ROOT / "deploy/api_preflight.sh").read_text(encoding="utf-8")
    assert "api_preflight.sh must run as root" in wrapper
    assert 'env_file_require_trusted "$ENV_FILE"' in wrapper
    assert 'env_file_load "$ENV_FILE"' in wrapper
    assert 'source "$ENV_FILE"' not in wrapper
    source = (ROOT / "scripts/api_readiness.py").read_text(encoding="utf-8")
    assert ".post(" not in source
    assert ".put(" not in source
    assert ".delete(" not in source


def test_unexpected_failure_never_prints_exception_or_secret(monkeypatch, capsys):
    class ExplodingProbe:
        def __init__(self, **_kwargs):
            raise RuntimeError("unexpected " + TOKEN)

    monkeypatch.setattr(api_readiness, "ApiReadinessProbe", ExplodingProbe)
    assert api_readiness.main([]) == 1
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert "unexpected" not in output
    assert json.loads(output)["error"] == (
        "internal readiness failure; inspect root-only system logs"
    )
