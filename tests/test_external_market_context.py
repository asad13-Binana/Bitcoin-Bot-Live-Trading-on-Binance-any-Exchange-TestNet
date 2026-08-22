from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from services.common.config_bounds import ConfigError
from services.moneyflow.external_clients import (
    COINGECKO_URL,
    COINMARKETCAP_URL,
    CoinGeckoClient,
    CoinMarketCapClient,
    ProviderHTTPError,
    ProviderPayloadError,
    ProviderTransportError,
)
from services.moneyflow.external_context import (
    COINGECKO_SAFE_MINUTE,
    COINGECKO_SAFE_MONTHLY,
    COINMARKETCAP_SAFE_MINUTE,
    COINMARKETCAP_SAFE_MONTHLY,
    ExternalContextManager,
    ProviderSpec,
    QuotaLedger,
    QuotaLedgerError,
    four_percent_below,
)
from services.moneyflow.service import collect
from services.telegram_broker import bot as telegram_bot

ROOT = Path(__file__).resolve().parents[1]


def _cg_payload(price=100_000.0):
    return {
        "bitcoin": {
            "usd": price,
            "usd_market_cap": 2_000_000_000_000,
            "usd_24h_vol": 50_000_000_000,
            "usd_24h_change": 1.25,
            "last_updated_at": 1_774_000_000,
        }
    }


def _cmc_payload(price=100_100.0):
    return [
        {
            "id": 1,
            "name": "Bitcoin",
            "symbol": "BTC",
            "slug": "bitcoin",
            "quote": [
                {
                    "symbol": "USD",
                    "price": price,
                    "market_cap": 2_000_000_000_000,
                    "volume_24h": 50_000_000_000,
                    "percent_change_24h": 1.1,
                    "last_updated": "2026-07-22T00:00:00Z",
                }
            ],
        }
    ]


class CaptureTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append({
            "url": url,
            "params": dict(params),
            "headers": dict(headers),
            "timeout": timeout,
        })
        return self.payload


def test_clients_use_exact_fixed_bitcoin_gets_and_header_only_credentials():
    cg_transport = CaptureTransport(_cg_payload())
    cmc_transport = CaptureTransport(_cmc_payload())
    cg_key = "cg-secret-value-123456789"
    cmc_key = "cmc-secret-value-123456789"

    cg = CoinGeckoClient(cg_key, transport=cg_transport).fetch_bitcoin_usd()
    cmc = CoinMarketCapClient(cmc_key, transport=cmc_transport).fetch_bitcoin_usd()

    assert cg["identity"] == {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin"}
    assert cmc["identity"] == {"id": 1, "symbol": "BTC", "name": "Bitcoin"}
    cg_call, cmc_call = cg_transport.calls[0], cmc_transport.calls[0]
    assert cg_call["url"] == COINGECKO_URL
    assert cg_call["params"] == {
        "ids": "bitcoin",
        "vs_currencies": "usd",
        "include_market_cap": "true",
        "include_24hr_vol": "true",
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    }
    assert cg_call["headers"]["x-cg-demo-api-key"] == cg_key
    assert cmc_call["url"] == COINMARKETCAP_URL
    assert cmc_call["params"] == {"id": "1", "convert": "USD"}
    assert cmc_call["headers"]["X-CMC_PRO_API_KEY"] == cmc_key
    for call, key in ((cg_call, cg_key), (cmc_call, cmc_key)):
        assert key not in call["url"]
        assert key not in json.dumps(call["params"])


@pytest.mark.parametrize(
    "client",
    [
        CoinGeckoClient("key", transport=CaptureTransport({"ethereum": {}})),
        CoinGeckoClient(
            "key",
            transport=CaptureTransport({"bitcoin": _cg_payload()["bitcoin"], "ethereum": {}}),
        ),
        CoinMarketCapClient(
            "key",
            transport=CaptureTransport([{
                "id": 1027,
                "name": "Ethereum",
                "symbol": "ETH",
                "slug": "ethereum",
                "quote": [{"symbol": "USD"}],
            }]),
        ),
    ],
)
def test_clients_fail_closed_on_identity_mismatch_or_extra_assets(client):
    with pytest.raises(ProviderPayloadError):
        client.fetch_bitcoin_usd()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "not-a-number"])
def test_coingecko_rejects_malformed_or_nonfinite_prices(bad):
    with pytest.raises(ProviderPayloadError):
        CoinGeckoClient("key", transport=CaptureTransport(_cg_payload(bad))).fetch_bitcoin_usd()


def test_safe_quota_caps_are_exactly_four_percent_below_current_official_free_plans():
    assert four_percent_below(100) == 96
    assert four_percent_below(10_000) == 9_600
    assert four_percent_below(30) == 28
    assert four_percent_below(10_000) == 9_600
    assert (COINGECKO_SAFE_MINUTE, COINGECKO_SAFE_MONTHLY) == (96, 9_600)
    assert (COINMARKETCAP_SAFE_MINUTE, COINMARKETCAP_SAFE_MONTHLY) == (28, 9_600)
    assert math.ceil(31 * 24 * 60 * 60 / 300) == 8_928
    assert 8_928 < min(COINGECKO_SAFE_MONTHLY, COINMARKETCAP_SAFE_MONTHLY)


def test_quota_ledger_reserves_before_dispatch_persists_and_honors_300_seconds(tmp_path):
    path = tmp_path / "quota.sqlite3"
    first = QuotaLedger(path)
    decision = first.reserve(
        "coingecko", now=1_800_000_000, minute_cap=96, monthly_cap=9_600,
        minimum_interval_seconds=300,
    )
    assert decision.allowed is True
    assert decision.quota["monthly_attempts_reserved"] == 1
    assert decision.quota["minute_attempts_reserved"] == 1

    restarted = QuotaLedger(path)
    blocked = restarted.reserve(
        "coingecko", now=1_800_000_299, minute_cap=96, monthly_cap=9_600,
        minimum_interval_seconds=300,
    )
    assert blocked.allowed is False and blocked.reason == "refresh_wait"
    allowed = restarted.reserve(
        "coingecko", now=1_800_000_300, minute_cap=96, monthly_cap=9_600,
        minimum_interval_seconds=300,
    )
    assert allowed.allowed is True
    assert allowed.quota["monthly_attempts_reserved"] == 2


def test_quota_ledger_enforces_minute_month_and_utc_rollover(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.sqlite3")
    now = datetime(2026, 7, 31, 23, 57, tzinfo=timezone.utc).timestamp()
    first = ledger.reserve(
        "coinmarketcap", now=now, minute_cap=1, monthly_cap=2,
        minimum_interval_seconds=1,
    )
    assert first.allowed
    minute_block = ledger.reserve(
        "coinmarketcap", now=now + 1, minute_cap=1, monthly_cap=2,
        minimum_interval_seconds=1,
    )
    assert minute_block.reason == "minute_rate_limited"
    second = ledger.reserve(
        "coinmarketcap", now=now + 61, minute_cap=1, monthly_cap=2,
        minimum_interval_seconds=1,
    )
    assert second.allowed
    month_block = ledger.reserve(
        "coinmarketcap", now=now + 122, minute_cap=1, monthly_cap=2,
        minimum_interval_seconds=1,
    )
    assert month_block.reason == "monthly_quota_exhausted"
    next_month = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    rolled = ledger.reserve(
        "coinmarketcap", now=next_month, minute_cap=1, monthly_cap=2,
        minimum_interval_seconds=1,
    )
    assert rolled.allowed
    assert rolled.quota["monthly_attempts_reserved"] == 1


def test_corrupt_and_symlinked_quota_ledgers_fail_closed(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(QuotaLedgerError):
        QuotaLedger(corrupt)

    target = tmp_path / "target.sqlite3"
    QuotaLedger(target)
    link = tmp_path / "link.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(QuotaLedgerError):
        QuotaLedger(link)


def test_initialized_quota_database_cannot_disappear_and_restart_at_zero(tmp_path):
    path = tmp_path / "quota.sqlite3"
    ledger = QuotaLedger(path)
    ledger.reserve(
        "coingecko", now=1_800_000_000, minute_cap=96, monthly_cap=9_600,
        minimum_interval_seconds=300,
    )
    marker = path.with_name(path.name + ".initialized")
    assert marker.is_file()
    path.unlink()
    with pytest.raises(QuotaLedgerError, match="disappeared|zero reset"):
        QuotaLedger(path)


class FakeProviderClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def fetch_bitcoin_usd(self):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return dict(self.outcome)


def _normalized(provider="coingecko"):
    return {
        "identity": {
            "id": "bitcoin" if provider == "coingecko" else 1,
            "symbol": "BTC",
            "name": "Bitcoin",
        },
        "price_usd": 100_000.0,
        "market_cap_usd": 2_000_000_000_000.0,
        "volume_24h_usd": 50_000_000_000.0,
        "percent_change_24h": 1.5,
        "source_updated_at_epoch": 1_800_000_000.0,
    }


def _manager(tmp_path, cg_client, cmc_client=None):
    cmc_client = cmc_client or FakeProviderClient(_normalized("coinmarketcap"))
    return ExternalContextManager(
        [
            ProviderSpec("coingecko", True, True, cg_client, 96, 9_600),
            ProviderSpec("coinmarketcap", False, False, cmc_client, 28, 9_600),
        ],
        ledger=QuotaLedger(tmp_path / "quota.sqlite3"),
        cache_path=tmp_path / "cache.json",
        refresh_seconds=300,
        stale_after_seconds=900,
    )


def test_manager_fetches_once_then_reuses_persistent_normalized_cache(tmp_path):
    client = FakeProviderClient(_normalized())
    manager = _manager(tmp_path, client)
    fresh = manager.snapshot(now=1_800_000_000)
    cached = manager.snapshot(now=1_800_000_100)
    assert client.calls == 1
    assert fresh["providers"]["coingecko"]["status"] == "fresh"
    assert cached["providers"]["coingecko"]["status"] == "cached"
    assert cached["providers"]["coinmarketcap"]["status"] == "disabled"
    assert fresh["advisory_only"] is True
    assert fresh["affects_entry_decision"] is False
    assert fresh["base_asset"] == "BTC"
    assert fresh["attribution"]["coingecko"]["text"] == "Data provided by CoinGecko"
    cache_text = (tmp_path / "cache.json").read_text(encoding="utf-8")
    assert "secret" not in cache_text.lower()
    assert "api_key" not in cache_text.lower()


def test_manager_429_backoff_uses_stale_cache_without_a_retry_burst(tmp_path):
    client = FakeProviderClient(_normalized())
    manager = _manager(tmp_path, client)
    manager.snapshot(now=1_800_000_000)
    client.outcome = ProviderHTTPError(429, retry_after_seconds=600)
    limited = manager.snapshot(now=1_800_000_300)
    blocked = manager.snapshot(now=1_800_000_301)
    assert client.calls == 2
    assert limited["providers"]["coingecko"]["status"] == "rate_limited"
    assert limited["providers"]["coingecko"]["available"] is True
    assert blocked["providers"]["coingecko"]["status"] == "backoff"
    assert blocked["providers"]["coingecko"]["quota"]["next_allowed_in_seconds"] >= 599


def test_manager_provider_outage_never_changes_core_binance_health_or_classification(tmp_path):
    manager = _manager(tmp_path, FakeProviderClient(ProviderTransportError("safe")))

    class BinanceFlow:
        def spot_exchange_symbol(self, symbol):
            return {
                "symbol": symbol,
                "status": "TRADING",
                "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "isSpotTradingAllowed": True,
                    "ocoAllowed": True,
                    "otoAllowed": True,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
                    ],
                }

        def spot_depth(self, symbol, limit):
            return {"bids": [["100", "2"]], "asks": [["100.1", "1"]]}

        def spot_aggregate_trades(self, symbol, limit):
            return [{"p": "100", "q": "1", "m": False}]

        def klines(self, symbol, interval, limit):
            return [
                [index, "99", "101", "98", str(100 + index), "1", index + 1]
                for index in range(64)
            ]

    pair = {
        "pair": "BTC/USDT",
        "symbol": "BTCUSDT",
        "state_hash": "a" * 64,
    }
    snapshot = collect(BinanceFlow(), pair, manager)
    assert snapshot["ok"] is True
    assert snapshot["external_context"]["providers"]["coingecko"]["status"] == "transport_error"
    assert "transport_error" not in snapshot["errors"]
    assert snapshot["external_context"]["affects_entry_decision"] is False


def test_required_external_confluence_is_a_fail_closed_moneyflow_confirmation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REQUIRE_EXTERNAL_CONFLUENCE", "true")
    monkeypatch.setenv("EXTERNAL_CONFLUENCE_MIN_PROVIDERS", "1")
    monkeypatch.setenv("EXTERNAL_CONFLUENCE_MIN_24H_CHANGE_PCT", "0")
    monkeypatch.setenv("EXTERNAL_CONFLUENCE_MAX_PRICE_DEVIATION_BPS", "100")

    class BinanceFlow:
        def spot_exchange_symbol(self, symbol):
            return {
                "symbol": symbol,
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
                "ocoAllowed": True,
                "otoAllowed": True,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
                ],
            }

        def spot_depth(self, symbol, limit):
            return {"bids": [["99999", "2"]], "asks": [["100001", "1"]]}

        def spot_aggregate_trades(self, symbol, limit):
            return [{"a": 1, "p": "100000", "q": "1", "m": False}]

        def klines(self, symbol, interval, limit):
            return [
                [index, "99900", "100100", "99800", str(100000 + index), "1", index + 1]
                for index in range(64)
            ]

    pair = {"pair": "BTC/USDT", "symbol": "BTCUSDT", "state_hash": "a" * 64}
    positive_path = tmp_path / "positive"
    negative_path = tmp_path / "negative"
    positive_path.mkdir()
    negative_path.mkdir()
    positive = _manager(positive_path, FakeProviderClient(_normalized()))
    confirmed = collect(BinanceFlow(), pair, positive)
    assert confirmed["ok"] is True
    assert confirmed["classification"]["decision"] == "BULLISH"
    assert confirmed["external_context"]["affects_entry_decision"] is True
    assert confirmed["external_context"]["confluence"]["confirmed"] is True

    negative_data = _normalized()
    negative_data["percent_change_24h"] = -0.1
    negative = _manager(negative_path, FakeProviderClient(negative_data))
    blocked = collect(BinanceFlow(), pair, negative)
    assert blocked["ok"] is False
    assert blocked["classification"]["decision"] == "UNAVAILABLE"
    assert blocked["external_context"]["confluence"]["confirmed"] is False
    assert "external_confluence:not_confirmed" in blocked["errors"]


def test_env_cannot_raise_provider_caps_above_four_percent_below(monkeypatch, tmp_path):
    monkeypatch.setenv("COINGECKO_MAX_REQUESTS_PER_MINUTE", "97")
    monkeypatch.setenv("EXTERNAL_MARKET_CACHE_FILE", str(tmp_path / "cache.json"))
    with pytest.raises(ConfigError):
        ExternalContextManager.from_env()


def test_compose_keeps_four_services_and_external_keys_only_in_moneyflow():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {
        "moneyflow", "freqtrade", "execution-sidecar", "telegram-broker"
    }
    external_keys = {"COINGECKO_API_KEY", "COINMARKETCAP_API_KEY"}
    holders = {
        name
        for name, service in services.items()
        if external_keys.intersection((service.get("environment") or {}).keys())
    }
    assert holders == {"moneyflow"}
    binance_holders = {
        name
        for name, service in services.items()
        if {"BINANCE_API_KEY", "BINANCE_API_SECRET"}.intersection(
            (service.get("environment") or {}).keys()
        )
    }
    assert binance_holders == {"execution-sidecar"}


def test_external_source_has_no_asset_discovery_or_order_capability():
    source = (
        (ROOT / "services/moneyflow/external_clients.py").read_text(encoding="utf-8")
        + (ROOT / "services/moneyflow/external_context.py").read_text(encoding="utf-8")
    ).lower()
    for forbidden in (
        "listings/latest", "coins/markets", "gainers", "trending", "top 50",
        "universe_service", ".post(", ".put(", ".delete(", "place_order",
    ):
        assert forbidden not in source
    assert '"ids": "bitcoin"' in source
    assert '"id": "1"' in source


def test_telegram_flow_summary_is_bounded_advisory_and_includes_attribution(
    monkeypatch,
):
    snapshot = {
        "pair": "BTC/USDT",
        "generated_at": "2026-07-22T00:00:00Z",
        "ok": True,
        "classification": {"decision": "NEUTRAL"},
        "spot": {},
        "futures": {},
        "external_context": {
            "advisory_only": True,
            "affects_entry_decision": False,
            "providers": {
                "coingecko": {
                    "status": "fresh", "available": True, "fresh": True,
                    "data": _normalized(),
                    "quota": {"monthly_attempts_reserved": 1, "monthly_attempt_cap": 9_600},
                    "unexpected": "must not be copied",
                }
            },
            "attribution": {
                "coingecko": {
                    "text": "Data provided by CoinGecko",
                    "url": "https://www.coingecko.com/en/api",
                }
            },
        },
    }
    monkeypatch.setattr(telegram_bot, "read_json", lambda *args, **kwargs: snapshot)
    result = telegram_bot._flow_summary()
    assert result["external_context"]["advisory_only"] is True
    assert result["external_context"]["affects_entry_decision"] is False
    assert result["external_context"]["providers"]["coingecko"]["price_usd"] == 100_000
    assert "unexpected" not in json.dumps(result)
    assert "Data provided by CoinGecko" in json.dumps(result)


def test_quota_database_contains_no_credentials(tmp_path):
    path = tmp_path / "quota.sqlite3"
    ledger = QuotaLedger(path)
    ledger.reserve(
        "coingecko", now=1_800_000_000, minute_cap=96, monthly_cap=9_600,
        minimum_interval_seconds=300,
    )
    connection = sqlite3.connect(path)
    try:
        dump = "\n".join(connection.iterdump()).lower()
    finally:
        connection.close()
    assert "api_key" not in dump
    assert "secret" not in dump
