"""Regressions adopted from the pinned official Binance source review."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from scripts.verify_binance_public_kline_archive import (
    ArchiveValidationError,
    file_sha256,
    verify_archive,
)
from services.common.binance_public import (
    BinancePublicClient,
    BinancePublicError,
)
from services.common.models import LifecycleState
from services.execution_sidecar.filters import (
    FilterDataUnavailable,
    FilterViolation,
    SpotFilterValidator,
)
from services.execution_sidecar.spot_gateway import BinanceSpotGateway
from services.execution_sidecar.state_store import StateStore
from services.execution_sidecar.user_data_stream import ModernUserDataStream


@pytest.fixture(autouse=True)
def official_source_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOWED_STABLE_QUOTES", "USDT")
    monkeypatch.setenv("AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))


def _symbol_row() -> dict:
    return {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "ocoAllowed": True,
        "otoAllowed": True,
        "allowTrailingStop": True,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.1",
                "maxPrice": "10000000",
                "tickSize": "0.1",
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.0001",
                "maxQty": "100",
                "stepSize": "0.0001",
            },
            {
                "filterType": "NOTIONAL",
                "minNotional": "5",
                "maxNotional": "1000000",
            },
        ],
    }


class _RulePublic:
    NO_REFERENCE_PRICE = BinancePublicClient.NO_REFERENCE_PRICE

    def __init__(self, *, reference="100", rule=None, rule_error=None):
        self.reference = reference
        self.rule_error = rule_error
        self.rule = rule if rule is not None else {
            "ruleType": "PRICE_RANGE",
            "bidLimitMultUp": "1.15",
            "bidLimitMultDown": "0.85",
            "askLimitMultUp": "1.15",
            "askLimitMultDown": "0.85",
        }

    def exchange_info(self, symbol):
        assert symbol == "BTCUSDT"
        return {"symbols": [_symbol_row()]}

    def execution_rules(self, symbol):
        assert symbol == "BTCUSDT"
        if self.rule_error:
            raise self.rule_error
        rules = [] if self.rule is False else [self.rule]
        return {"symbolRules": [{"symbol": symbol, "rules": rules}]}

    def reference_price(self, symbol):
        assert symbol == "BTCUSDT"
        return self.reference


def _replacement(price: str, *, side: str = "SELL") -> dict:
    return {
        "symbol": "BTCUSDT",
        "side": side,
        "type": "STOP_LOSS_LIMIT",
        "quantity": "0.1000",
        "price": price,
        "stopPrice": price,
    }


def test_public_client_queries_execution_rules_for_one_exact_btc_symbol(monkeypatch):
    client = BinancePublicClient(base="https://example.invalid", max_attempts=1)
    calls = []

    def fake_get(path, params):
        calls.append((path, params))
        return {"symbolRules": []}

    monkeypatch.setattr(client, "get", fake_get)
    assert client.execution_rules("btcusdt") == {"symbolRules": []}
    assert calls == [("/api/v3/executionRules", {"symbol": "BTCUSDT"})]
    with pytest.raises(BinancePublicError):
        client.execution_rules("ETHUSDT")


def test_price_range_preflight_blocks_non_executable_sell_price():
    validator = SpotFilterValidator(_RulePublic())
    with pytest.raises(FilterViolation, match="PRICE_RANGE"):
        validator.validate_replacement(
            "BTCUSDT", "order", _replacement("84.9")
        )
    summary = validator.validate_replacement(
        "BTCUSDT", "order", _replacement("85.0")
    )
    assert summary["execution_reference_price"] == "100"
    assert summary["execution_rule"]["askLimitMultDown"] == "0.85"
    assert "PRICE_RANGE" in summary["filters_checked"]


def test_price_range_uses_bid_multipliers_for_buy_orders():
    public = _RulePublic(rule={
        "ruleType": "PRICE_RANGE",
        "bidLimitMultUp": "1.01",
        "bidLimitMultDown": "0.99",
        "askLimitMultUp": "2",
        "askLimitMultDown": "0.5",
    })
    validator = SpotFilterValidator(public)
    with pytest.raises(FilterViolation, match="above PRICE_RANGE"):
        validator.validate_replacement(
            "BTCUSDT", "order", _replacement("101.1", side="BUY")
        )


def test_price_range_is_not_enforced_without_official_reference_price():
    public = _RulePublic(reference=BinancePublicClient.NO_REFERENCE_PRICE)
    summary = SpotFilterValidator(public).validate_replacement(
        "BTCUSDT", "order", _replacement("50.0")
    )
    assert summary["execution_reference_price"] is None
    assert summary["execution_rule"] is not None


@pytest.mark.parametrize(
    "rule",
    [
        {"ruleType": "PRICE_RANGE", "askLimitMultDown": "NaN"},
        {
            "ruleType": "PRICE_RANGE",
            "askLimitMultDown": "2",
            "askLimitMultUp": "1",
        },
    ],
)
def test_malformed_price_range_rule_fails_closed(rule):
    validator = SpotFilterValidator(_RulePublic(rule=rule))
    with pytest.raises(FilterDataUnavailable, match="PRICE_RANGE"):
        validator.validate_replacement(
            "BTCUSDT", "order", _replacement("100")
        )


def test_execution_rule_transport_failure_fails_before_replacement():
    validator = SpotFilterValidator(
        _RulePublic(rule_error=TimeoutError("unavailable"))
    )
    with pytest.raises(FilterDataUnavailable, match="execution rules"):
        validator.validate_replacement(
            "BTCUSDT", "order", _replacement("100")
        )


def _state_store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.json", tmp_path / "state.sqlite")
    store.register_symbol_pair("BTCUSDT", "BTC/USDT")
    return store


def test_expired_in_match_is_terminal_for_unfilled_entry(tmp_path):
    store = _state_store(tmp_path)
    store.upsert_trade(
        "stp-entry",
        "BTC/USDT",
        lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
        entry_order_id=101,
    )
    store.record_exchange_event({
        "e": "executionReport",
        "s": "BTCUSDT",
        "i": 101,
        "S": "BUY",
        "X": "EXPIRED_IN_MATCH",
        "o": "LIMIT",
        "z": "0",
        "q": "0.1",
        "E": 1,
        "I": 1,
    })
    row = store.trade("stp-entry")
    assert row["lifecycle_state"] == LifecycleState.ENTRY_REJECTED.value
    assert row["reconciliation_status"] == "ENTRY_TERMINAL_NO_FILL"


def test_expired_in_match_marks_sell_protection_for_reconciliation(tmp_path):
    store = _state_store(tmp_path)
    store.upsert_trade(
        "stp-sell",
        "BTC/USDT",
        lifecycle_state=LifecycleState.PROTECTION_ACTIVE.value,
        stop_order_id=202,
    )
    store.record_exchange_event({
        "e": "executionReport",
        "s": "BTCUSDT",
        "i": 202,
        "S": "SELL",
        "X": "EXPIRED_IN_MATCH",
        "o": "STOP_LOSS_LIMIT",
        "z": "0",
        "q": "0.1",
        "E": 2,
        "I": 2,
    })
    assert store.trade("stp-sell")["reconciliation_status"] == (
        "SELL_PROTECTION_TERMINAL_RECONCILE_REQUIRED"
    )


class _TimeClient:
    def __init__(self, server_time):
        self.server_time = server_time
        self.timestamp_offset = 0

    def get_server_time(self):
        return {"serverTime": self.server_time}


def test_gateway_time_sync_uses_rtt_midpoint(monkeypatch):
    gateway = object.__new__(BinanceSpotGateway)
    gateway.client = _TimeClient(1_500)
    gateway.max_time_sync_rtt_ms = 100
    samples = iter((1_000_000_000, 1_020_000_000))
    monkeypatch.setattr(
        "services.execution_sidecar.spot_gateway.time.time_ns",
        lambda: next(samples),
    )
    gateway._sync_server_time()
    assert gateway.client.timestamp_offset == 490


def test_gateway_time_sync_rejects_excessive_rtt(monkeypatch):
    gateway = object.__new__(BinanceSpotGateway)
    gateway.client = _TimeClient(1_500)
    gateway.max_time_sync_rtt_ms = 10
    samples = iter((1_000_000_000, 1_020_000_000))
    monkeypatch.setattr(
        "services.execution_sidecar.spot_gateway.time.time_ns",
        lambda: next(samples),
    )
    with pytest.raises(RuntimeError, match="RTT"):
        gateway._sync_server_time()


def test_gateway_signed_timestamp_uses_synchronized_offset(monkeypatch):
    gateway = object.__new__(BinanceSpotGateway)
    gateway.client = SimpleNamespace(timestamp_offset=250)
    monkeypatch.setattr(
        "services.execution_sidecar.spot_gateway.time.time",
        lambda: 1_700_000_000.0,
    )
    assert gateway.signed_timestamp_ms() == 1_700_000_000_250


def test_user_stream_signature_uses_gateway_clock_and_recv_window(monkeypatch):
    broker = SimpleNamespace(
        signed_timestamp_ms=lambda: 1_700_000_000_123,
        recv_window_ms=4000,
    )
    stream = ModernUserDataStream(broker, testnet=True)
    request = stream._subscription_request()
    params = request["params"]
    unsigned = {
        "apiKey": "test-key",
        "recvWindow": 4000,
        "timestamp": 1_700_000_000_123,
    }
    query = "&".join(f"{key}={unsigned[key]}" for key in sorted(unsigned))
    expected = hmac.new(
        b"test-secret", query.encode(), hashlib.sha256
    ).hexdigest()
    assert params == {**unsigned, "signature": expected}


def _write_kline_archive(
    tmp_path: Path, rows: list[str], *, member: str = "BTCUSDT-1m-2025-01.csv"
) -> tuple[Path, Path]:
    archive = tmp_path / "BTCUSDT-1m-2025-01.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member, "\n".join(rows) + "\n")
    checksum = Path(str(archive) + ".CHECKSUM")
    checksum.write_text(
        f"{file_sha256(archive)}  {archive.name}\n", encoding="utf-8"
    )
    return archive, checksum


def test_public_kline_archive_verifier_accepts_microsecond_spot_data(tmp_path):
    rows = [
        "1735689600000000,100,101,99,100.5,1,1735689659999999,100,2,0.5,50,0",
        "1735689660000000,100.5,102,100,101,2,1735689719999999,201,3,1,100,0",
    ]
    archive, checksum = _write_kline_archive(tmp_path, rows)
    result = verify_archive(archive, checksum)
    assert result["ok"] is True
    assert result["rows"] == 2
    assert result["timestamp_unit"] == "microseconds"


def test_public_kline_archive_verifier_rejects_checksum_mismatch(tmp_path):
    archive, checksum = _write_kline_archive(tmp_path, [
        "1735689600000000,100,101,99,100.5,1,1735689659999999,100,2,0.5,50,0"
    ])
    checksum.write_text(
        f"{'0' * 64}  {archive.name}\n", encoding="utf-8"
    )
    with pytest.raises(ArchiveValidationError, match="SHA-256 mismatch"):
        verify_archive(archive, checksum)


def test_public_kline_archive_verifier_rejects_traversal_member(tmp_path):
    archive, checksum = _write_kline_archive(
        tmp_path,
        ["1735689600000,100,101,99,100.5,1,1735689659999,100,2,0.5,50,0"],
        member="../BTCUSDT.csv",
    )
    with pytest.raises(ArchiveValidationError, match="unsafe ZIP member"):
        verify_archive(archive, checksum)
