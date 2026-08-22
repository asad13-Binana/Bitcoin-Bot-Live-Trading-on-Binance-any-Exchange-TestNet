"""Regressions adopted from the pinned official Binance source review."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import hmac
from importlib import metadata
import inspect
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


def _order_list_replacement() -> dict:
    return {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "quantity": "0.1000",
        "aboveType": "LIMIT_MAKER",
        "abovePrice": "110.0",
        "belowType": "STOP_LOSS_LIMIT",
        "belowPrice": "90.0",
        "belowStopPrice": "90.0",
    }


class _FilterScopePublic(_RulePublic):
    def __init__(self, *, symbol_filters=(), exchange_filters=()):
        super().__init__()
        self.symbol_filters = [dict(item) for item in symbol_filters]
        self.exchange_filters = [dict(item) for item in exchange_filters]

    def exchange_info(self, symbol):
        assert symbol == "BTCUSDT"
        row = _symbol_row()
        row["filters"] = [*row["filters"], *self.symbol_filters]
        return {
            "exchangeFilters": list(self.exchange_filters),
            "symbols": [row],
        }


def test_cancel_only_symbol_status_fails_closed_before_replacement():
    class CancelOnlyPublic(_RulePublic):
        def exchange_info(self, symbol):
            assert symbol == "BTCUSDT"
            row = _symbol_row()
            row["status"] = "CANCEL_ONLY"
            return {"symbols": [row]}

    with pytest.raises(FilterViolation, match="status is 'CANCEL_ONLY', not TRADING"):
        SpotFilterValidator(CancelOnlyPublic()).validate_replacement(
            "BTCUSDT",
            "order",
            _replacement("100.0"),
        )


def test_max_num_order_lists_counts_only_lists_on_the_requested_symbol():
    public = _FilterScopePublic(symbol_filters=[{
        "filterType": "MAX_NUM_ORDER_LISTS",
        "maxNumOrderLists": 2,
    }])
    summary = SpotFilterValidator(public).validate_replacement(
        "BTCUSDT",
        "orderList/oco",
        _order_list_replacement(),
        open_order_lists_provider=lambda: [
            {"symbol": "BTCUSDT", "orderListId": 101},
            {"symbol": "ETHUSDT", "orderListId": 202},
        ],
    )
    assert "MAX_NUM_ORDER_LISTS" in summary["filters_checked"]


def test_max_position_counts_base_balance_open_buys_and_incoming_buy():
    public = _FilterScopePublic(symbol_filters=[{
        "filterType": "MAX_POSITION",
        "maxPosition": "1.0000",
    }])
    with pytest.raises(FilterViolation, match="MAX_POSITION 1.0000"):
        SpotFilterValidator(public).validate_replacement(
            "BTCUSDT",
            "order",
            _replacement("100.0", side="BUY"),
            open_orders_provider=lambda _symbol: [{
                "symbol": "BTCUSDT",
                "orderId": 101,
                "side": "BUY",
                "type": "LIMIT",
                "origQty": "0.0500",
                "executedQty": "0",
            }],
            account_provider=lambda: {"balances": [{
                "asset": "BTC",
                "free": "0.8500",
                "locked": "0.0500",
            }]},
        )


def test_max_position_allows_an_incoming_buy_at_the_exact_boundary():
    public = _FilterScopePublic(symbol_filters=[{
        "filterType": "MAX_POSITION",
        "maxPosition": "1.0000",
    }])
    summary = SpotFilterValidator(public).validate_replacement(
        "BTCUSDT",
        "order",
        _replacement("100.0", side="BUY"),
        open_orders_provider=lambda _symbol: [{
            "symbol": "BTCUSDT",
            "orderId": 101,
            "side": "BUY",
            "type": "LIMIT",
            "origQty": "0.0500",
            "executedQty": "0",
        }],
        account_provider=lambda: {"balances": [{
            "asset": "BTC",
            "free": "0.8000",
            "locked": "0.0500",
        }]},
    )
    assert "MAX_POSITION" in summary["filters_checked"]


def test_exchange_max_num_orders_uses_account_wide_open_orders():
    public = _FilterScopePublic(exchange_filters=[{
        "filterType": "EXCHANGE_MAX_NUM_ORDERS",
        "maxNumOrders": 1,
    }])
    with pytest.raises(FilterViolation, match="EXCHANGE_MAX_NUM_ORDERS 1"):
        SpotFilterValidator(public).validate_replacement(
            "BTCUSDT",
            "order",
            _replacement("100.0"),
            all_open_orders_provider=lambda: [{
                "symbol": "ETHUSDT",
                "orderId": 202,
                "side": "SELL",
                "type": "LIMIT",
                "origQty": "1",
            }],
        )


def test_exchange_max_num_algo_orders_uses_account_wide_open_orders():
    public = _FilterScopePublic(exchange_filters=[{
        "filterType": "EXCHANGE_MAX_NUM_ORDERS",
        "maxNumOrders": 10,
    }, {
        "filterType": "EXCHANGE_MAX_NUM_ALGO_ORDERS",
        "maxNumAlgoOrders": 1,
    }])
    with pytest.raises(FilterViolation, match="EXCHANGE_MAX_NUM_ALGO_ORDERS 1"):
        SpotFilterValidator(public).validate_replacement(
            "BTCUSDT",
            "order",
            _replacement("100.0"),
            all_open_orders_provider=lambda: [{
                "symbol": "ETHUSDT",
                "orderId": 202,
                "side": "SELL",
                "type": "STOP_LOSS_LIMIT",
                "origQty": "1",
            }],
        )


def test_exchange_max_num_order_lists_counts_lists_across_all_symbols():
    public = _FilterScopePublic(exchange_filters=[{
        "filterType": "EXCHANGE_MAX_NUM_ORDER_LISTS",
        "maxNumOrderLists": 2,
    }])
    with pytest.raises(FilterViolation, match="EXCHANGE_MAX_NUM_ORDER_LISTS 2"):
        SpotFilterValidator(public).validate_replacement(
            "BTCUSDT",
            "orderList/oco",
            _order_list_replacement(),
            open_order_lists_provider=lambda: [
                {"symbol": "BTCUSDT", "orderListId": 101},
                {"symbol": "ETHUSDT", "orderListId": 202},
            ],
        )


def test_exchange_order_filter_without_account_wide_provider_fails_closed():
    public = _FilterScopePublic(exchange_filters=[{
        "filterType": "EXCHANGE_MAX_NUM_ORDERS",
        "maxNumOrders": 10,
    }])
    with pytest.raises(FilterDataUnavailable, match="account-wide open-order"):
        SpotFilterValidator(public).validate_replacement(
            "BTCUSDT", "order", _replacement("100.0")
        )


def test_symbol_and_exchange_capacity_filters_share_one_account_wide_snapshot():
    public = _FilterScopePublic(
        symbol_filters=[{
            "filterType": "MAX_NUM_ORDERS",
            "maxNumOrders": 4,
        }, {
            "filterType": "MAX_NUM_ALGO_ORDERS",
            "maxNumAlgoOrders": 2,
        }],
        exchange_filters=[{
            "filterType": "EXCHANGE_MAX_NUM_ORDERS",
            "maxNumOrders": 5,
        }, {
            "filterType": "EXCHANGE_MAX_NUM_ALGO_ORDERS",
            "maxNumAlgoOrders": 3,
        }, {
            "filterType": "EXCHANGE_MAX_NUM_ORDER_LISTS",
            "maxNumOrderLists": 3,
        }],
    )
    calls = []

    def all_open_orders():
        calls.append("all")
        return [{
            "symbol": "BTCUSDT",
            "orderId": 101,
            "side": "SELL",
            "type": "STOP_LOSS_LIMIT",
            "origQty": "0.1",
        }, {
            "symbol": "ETHUSDT",
            "orderId": 202,
            "side": "SELL",
            "type": "LIMIT",
            "origQty": "1",
        }]

    summary = SpotFilterValidator(public).validate_replacement(
        "BTCUSDT",
        "orderList/oco",
        _order_list_replacement(),
        open_orders_provider=lambda _symbol: pytest.fail(
            "symbol endpoint must not be called when one account snapshot exists"
        ),
        all_open_orders_provider=all_open_orders,
        open_order_lists_provider=lambda: [
            {"symbol": "BTCUSDT", "orderListId": 301},
            {"symbol": "ETHUSDT", "orderListId": 302},
        ],
        replacing_order_ids=(101,),
        replacing_order_list=True,
    )
    assert calls == ["all"]
    assert {
        "MAX_NUM_ORDERS",
        "MAX_NUM_ALGO_ORDERS",
        "EXCHANGE_MAX_NUM_ORDERS",
        "EXCHANGE_MAX_NUM_ALGO_ORDERS",
        "EXCHANGE_MAX_NUM_ORDER_LISTS",
    }.issubset(summary["filters_checked"])


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


@pytest.mark.parametrize(
    ("event_type", "expected_error"),
    [
        ("eventStreamTerminated", "event_stream_terminated"),
        ("serverShutdown", "server_shutdown"),
    ],
)
def test_user_stream_terminal_events_close_the_socket_and_clear_subscription(
    tmp_path, monkeypatch, event_type, expected_error
):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    stream = ModernUserDataStream(SimpleNamespace(), testnet=True)
    stream._connected = True
    stream._subscribed = True

    class Socket:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    socket = Socket()
    stream._ws = socket
    stream._dispatch_event({"e": event_type})

    assert socket.closed == 1
    assert stream._subscribed is False
    assert stream._last_error == expected_error


def test_pinned_python_binance_private_http_interface_remains_compatible():
    from binance.client import Client

    assert metadata.version("python-binance") == "1.0.28"
    for method_name in ("_get", "_post", "_delete"):
        parameters = list(inspect.signature(getattr(Client, method_name)).parameters.values())
        assert [parameter.name for parameter in parameters[:4]] == [
            "self",
            "path",
            "signed",
            "version",
        ]
        assert parameters[4].kind is inspect.Parameter.VAR_KEYWORD


def test_gateway_order_list_calls_match_the_pinned_python_binance_contract():
    calls = []

    class RecordingClient:
        def _get(self, path, signed=False, version="v1", **kwargs):
            calls.append(("GET", path, signed, version, kwargs))
            return {"ok": True}

        def _post(self, path, signed=False, version="v1", **kwargs):
            calls.append(("POST", path, signed, version, kwargs))
            return {"ok": True}

        def _delete(self, path, signed=False, version="v1", **kwargs):
            calls.append(("DELETE", path, signed, version, kwargs))
            return {"ok": True}

    gateway = object.__new__(BinanceSpotGateway)
    gateway.client = RecordingClient()
    gateway.open_order_lists()
    gateway.get_order_list(order_list_id=41)
    gateway.get_order_list(list_client_id="btc-list-42")
    gateway.place("orderList/otoco", {"symbol": "BTCUSDT", "quantity": "0.1"})
    gateway.cancel_order_list("BTCUSDT", 43)

    assert calls == [
        ("GET", "openOrderList", True, "v1", {"data": {}}),
        ("GET", "orderList", True, "v1", {"data": {"orderListId": 41}}),
        (
            "GET",
            "orderList",
            True,
            "v1",
            {"data": {"origClientOrderId": "btc-list-42"}},
        ),
        (
            "POST",
            "orderList/otoco",
            True,
            "v1",
            {"data": {"symbol": "BTCUSDT", "quantity": "0.1"}},
        ),
        (
            "DELETE",
            "orderList",
            True,
            "v1",
            {"data": {"symbol": "BTCUSDT", "orderListId": 43}},
        ),
    ]


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
