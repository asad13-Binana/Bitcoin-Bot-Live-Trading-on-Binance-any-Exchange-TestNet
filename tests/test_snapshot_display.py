from __future__ import annotations

import pytest

from services.telegram_broker import bot


@pytest.mark.parametrize("timestamp", [None, True, "1000", 1, 1100, float("nan"), float("inf")])
def test_telegram_old_malformed_or_future_snapshot_cannot_report_current_health(monkeypatch, timestamp):
    monkeypatch.setattr(bot.time, "time", lambda: 1000)
    monkeypatch.setattr(bot, "read_json", lambda *a, **k: {
        "ok": True, "generated_at_epoch": timestamp, "classification": {"bullish": True},
        "external_context": {"providers": {"coingecko": {"fresh": True, "available": True}}}})
    result = bot._flow_summary()
    assert result["ok"] is False and result["fresh"] is False
    assert result["snapshot_ok"] is True
    assert result["classification"] == {"bullish": False, "decision": "UNAVAILABLE"}
    assert result["external_context"]["providers"]["coingecko"]["fresh"] is False


def test_telegram_fresh_snapshot_is_not_claimed_to_be_a_service_probe(monkeypatch):
    monkeypatch.setattr(bot.time, "time", lambda: 1000)
    monkeypatch.setattr(bot, "read_json", lambda *a, **k: {
        "ok": True, "generated_at_epoch": 995, "classification": {"decision": "NEUTRAL"}})
    result = bot._flow_summary()
    assert result["ok"] is True and result["fresh"] is True
    assert result["status"] == "fresh_snapshot_not_service_health"
    assert result["age_seconds"] == 5
