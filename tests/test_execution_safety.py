"""Focused money-side safety tests for the Bitcoin-only execution release.

These tests intentionally use deterministic fakes at the authenticated Spot
gateway boundary.  They verify durable ordering and no-resend behavior without
ever contacting Binance or requiring credentials.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

import pytest

import services.common.atomic as atomic_module
from services.common.market_policy import (
    PairPolicyError,
    canonical_pair,
    pair_config_hash,
    pair_state_hash,
    symbol_for_pair,
    validate_exchange_symbol,
)
from services.common.models import LifecycleState, ProtectionMode
from services.common.config_bounds import ConfigError
from services.execution_sidecar.bitcoin_adapter import BitcoinSpotAdapter, SymbolRules
from services.execution_sidecar.client_ids import ClientOrderIds
from services.execution_sidecar.filters import FilterViolation, SpotFilterValidator
from services.execution_sidecar.pair_control import PairController, PairStateError
from services.execution_sidecar.protection_modes import (
    OrderRequestFactory,
    ProtectionSettings,
    fee_adjusted_break_even,
)
from services.execution_sidecar.simulation_adapter import SimulationAdapter
from services.execution_sidecar.spot_gateway import BinanceSpotGateway, SubmissionClass
from services.execution_sidecar.state_store import StateStore
from services.execution_sidecar.risk_checks import FreshSignalGuard


FILTERS_PATH = Path(__file__).resolve().parents[1] / "services/execution_sidecar/filters.py"


@pytest.fixture(autouse=True)
def bounded_execution_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOWED_STABLE_QUOTES", "USDT,USDC,FDUSD")
    monkeypatch.setenv("AUDIT_LOG", str(tmp_path / "audit" / "events.jsonl"))
    monkeypatch.setenv("TRADE_SIZE_QUOTE", "100")
    monkeypatch.setenv("TAKE_PROFIT_PCT", "1.2")
    monkeypatch.setenv("FIXED_STOP_PCT", "2.0")
    monkeypatch.setenv("TRAILING_DELTA_BIPS", "40")
    monkeypatch.setenv("LIMIT_FILL_BUFFER_BIPS", "20")
    monkeypatch.setenv("FEE_PCT_PER_SIDE", "0.1")


def test_filter_validation_uses_explicit_fail_closed_trailing_bounds():
    source = FILTERS_PATH.read_text(encoding="utf-8")
    tree = compile(source, str(FILTERS_PATH), "exec", ast.PyCF_ONLY_AST)
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    assert "TRAILING_DELTA sell bounds are unavailable" in source


def exchange_symbol(pair: str = "BTC/USDT") -> dict:
    base, quote = pair.split("/")
    return {
        "symbol": base + quote,
        "status": "TRADING",
        "baseAsset": base,
        "quoteAsset": quote,
        "permissions": ["SPOT"],
        "isSpotTradingAllowed": True,
        "ocoAllowed": True,
        "otoAllowed": True,
        "allowTrailingStop": True,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.10",
                "maxPrice": "10000000",
                "tickSize": "0.10",
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
            {
                "filterType": "TRAILING_DELTA",
                "minTrailingBelowDelta": 10,
                "maxTrailingBelowDelta": 2000,
            },
        ],
    }


def make_store(tmp_path, name: str = "state") -> StateStore:
    return StateStore(tmp_path / f"{name}.json", tmp_path / f"{name}.sqlite")


def test_parent_directory_fsync_is_skipped_outside_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(atomic_module.os, "name", "nt")

    def unexpected_open(*_args, **_kwargs):
        raise AssertionError("directory os.open must not run outside POSIX")

    monkeypatch.setattr(atomic_module.os, "open", unexpected_open)
    atomic_module._fsync_parent_directory(tmp_path)


@pytest.mark.parametrize("content", ["{broken", "[]", '{"daily":{"x":{"global_stopouts":-1}}}'])
def test_existing_corrupt_risk_state_fails_closed_instead_of_resetting(tmp_path, content):
    path = tmp_path / "fresh_signal_guard.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        FreshSignalGuard(path)


def test_existing_database_cannot_silently_reset_missing_legacy_risk_state(tmp_path):
    json_path = tmp_path / "sidecar.json"
    database = tmp_path / "execution.sqlite"
    first = StateStore(json_path, database)
    assert first.schema_version() == 1
    restarted = StateStore(json_path, database)
    assert restarted.database_preexisted is True
    with pytest.raises(ConfigError, match="authoritative SQLite risk state is unavailable"):
        FreshSignalGuard(tmp_path / "missing-risk.json", state_store=restarted)


@pytest.mark.parametrize(
    "content",
    [b"{broken", b"[]", b'{"daily":{"x":{"global_stopouts":-1}}}'],
)
def test_invalid_legacy_migration_rolls_back_and_keeps_schema_at_v1(tmp_path, content):
    json_path = tmp_path / "sidecar.json"
    database = tmp_path / "execution.sqlite"
    StateStore(json_path, database)
    legacy_path = tmp_path / "fresh_signal_guard.json"
    legacy_path.write_bytes(content)
    store = StateStore(json_path, database)
    with pytest.raises(ConfigError):
        FreshSignalGuard(legacy_path, state_store=store)
    assert store.schema_version() == 1
    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_guard_state'"
        ).fetchone()
    assert table is None


def test_valid_legacy_risk_state_migrates_transactionally_with_source_hash(tmp_path):
    json_path = tmp_path / "sidecar.json"
    database = tmp_path / "execution.sqlite"
    StateStore(json_path, database)
    legacy_path = tmp_path / "fresh_signal_guard.json"
    legacy = {
        "pairs": {"BTC/USDT": {"cooldown_until": "2030-01-01T00:00:00+00:00"}},
        "daily": {"2026-08-13": {"global_stopouts": 2, "pairs": {"BTC/USDT": 2}}},
        "global_pause": "daily-risk-limit",
    }
    source = json.dumps(legacy, separators=(",", ":")).encode("utf-8")
    legacy_path.write_bytes(source)
    store = StateStore(json_path, database)
    guard = FreshSignalGuard(legacy_path, state_store=store)
    assert store.schema_version() == 2
    assert guard.state == legacy
    assert store.risk_guard_state() == legacy
    migration = store.risk_guard_migration()
    assert migration["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert migration["name"] == "sqlite-authoritative-risk-guard-state"


def test_failure_between_migration_steps_rolls_back_all_new_risk_state(tmp_path):
    json_path = tmp_path / "sidecar.json"
    database = tmp_path / "execution.sqlite"
    StateStore(json_path, database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE schema_migrations(broken TEXT)")
    risk_path = tmp_path / "fresh_signal_guard.json"
    risk_path.write_text(
        json.dumps({"pairs": {}, "daily": {}, "global_pause": "preserve-me"}),
        encoding="utf-8",
    )
    store = StateStore(json_path, database)
    with pytest.raises(ConfigError, match="authoritative SQLite risk state is unavailable"):
        FreshSignalGuard(risk_path, state_store=store)
    assert store.schema_version() == 1
    with sqlite3.connect(database) as connection:
        risk_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_guard_state'"
        ).fetchone()
    assert risk_table is None


def test_sqlite_risk_state_survives_restart_when_json_projection_disappears(tmp_path):
    json_path = tmp_path / "sidecar.json"
    database = tmp_path / "execution.sqlite"
    risk_path = tmp_path / "fresh_signal_guard.json"
    store = StateStore(json_path, database)
    store.register_symbol_pair("BTCUSDT", "BTC/USDT")
    guard = FreshSignalGuard(risk_path, state_store=store)
    guard.record_stopout("BTCUSDT", 1_700_000_000_000)
    guard.set_global_pause("daily-risk-limit")
    expected = json.loads(json.dumps(guard.state))
    risk_path.unlink()

    restarted_store = StateStore(json_path, database)
    restarted = FreshSignalGuard(risk_path, state_store=restarted_store)
    assert restarted.state == expected
    assert json.loads(risk_path.read_text(encoding="utf-8")) == expected


def test_sqlite_is_authoritative_when_legacy_projection_conflicts(tmp_path):
    store = make_store(tmp_path)
    risk_path = tmp_path / "risk.json"
    guard = FreshSignalGuard(risk_path, state_store=store)
    guard.set_global_pause("authoritative-pause")
    risk_path.write_text(
        json.dumps({"pairs": {}, "daily": {}, "global_pause": "wrong-pause"}),
        encoding="utf-8",
    )
    restarted_store = StateStore(store.path, store.db_path)
    restarted = FreshSignalGuard(risk_path, state_store=restarted_store)
    assert restarted.state["global_pause"] == "authoritative-pause"
    assert json.loads(risk_path.read_text(encoding="utf-8"))["global_pause"] == (
        "authoritative-pause"
    )


def test_risk_state_checksum_tampering_fails_closed(tmp_path):
    store = make_store(tmp_path)
    risk_path = tmp_path / "risk.json"
    FreshSignalGuard(risk_path, state_store=store)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE risk_guard_state SET payload_sha256=? WHERE singleton=1", ("0" * 64,)
        )
    restarted_store = StateStore(store.path, store.db_path)
    with pytest.raises(ConfigError, match="authoritative SQLite risk state is unavailable"):
        FreshSignalGuard(risk_path, state_store=restarted_store)


def test_failed_risk_state_commit_reverts_memory_and_disables_entries(
    tmp_path, monkeypatch
):
    store = make_store(tmp_path)
    guard = FreshSignalGuard(tmp_path / "risk.json", state_store=store)
    store.set_entries(True)

    def fail_commit(_payload):
        raise sqlite3.OperationalError("simulated disk full")

    monkeypatch.setattr(store, "save_risk_guard_state", fail_commit)
    with pytest.raises(sqlite3.OperationalError, match="simulated disk full"):
        guard.set_global_pause("must-not-appear-committed")
    assert guard.state["global_pause"] == ""
    assert store.entries() is False
    assert store.data["pause_reason"] == "risk-state-persistence-failed"


def test_unsupported_sqlite_schema_version_fails_closed(tmp_path):
    database = tmp_path / "future.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="unsupported SQLite state schema version 99"):
        StateStore(tmp_path / "sidecar.json", database)


def test_verified_sqlite_backup_contains_authoritative_risk_state(tmp_path):
    store = make_store(tmp_path)
    guard = FreshSignalGuard(tmp_path / "risk.json", state_store=store)
    guard.set_global_pause("backup-must-preserve-this")
    backup = store.backup(tmp_path / "backups", retain=2)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        payload = connection.execute(
            "SELECT payload_json FROM risk_guard_state WHERE singleton=1"
        ).fetchone()[0]
    assert json.loads(payload)["global_pause"] == "backup-must-preserve-this"


def make_pair_controller(tmp_path, pair: str = "BTC/USDT", name: str = "pair"):
    root = tmp_path / name
    controller = PairController(
        root / "active_pair.json",
        root / "pairlist.json",
        root / "freqtrade-active.json",
        allowed_quotes=("USDT", "USDC", "FDUSD"),
    )
    state = controller.bootstrap(pair)
    return controller, state


def test_protective_exit_above_entry_but_below_fee_break_even_counts_as_stopout(tmp_path):
    store = make_store(tmp_path)
    store.register_symbol_pair("BTCUSDT", "BTC/USDT")
    store.upsert_trade(
        "fee-loss", "BTC/USDT", lifecycle_state=LifecycleState.PROTECTION_ACTIVE.value,
        average_entry_price="100", filled_quantity="0.1", protected_quantity="0.1",
    )
    guard = FreshSignalGuard(tmp_path / "risk.json", state_store=store)
    guard.on_exchange_event({
        "e": "executionReport", "s": "BTCUSDT", "S": "SELL", "X": "FILLED",
        "o": "STOP_LOSS_LIMIT", "z": "0.1", "Z": "10.005", "E": 1_700_000_000_000,
    })
    day = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).date().isoformat()
    assert guard.state["daily"][day]["global_stopouts"] == 1


class FakeGuard:
    def __init__(self):
        self.pauses = []
        self.clears = 0
        self.events = []

    def set_global_pause(self, reason):
        self.pauses.append(str(reason))

    def clear_global_pause(self):
        self.clears += 1

    def on_exchange_event(self, event):
        self.events.append(dict(event))


class NoopStream:
    def __init__(self, *_args, **_kwargs):
        self.started = False

    def start(self):
        self.started = True


class FakeExchangeError(RuntimeError):
    pass


class _GatewayApiError(RuntimeError):
    def __init__(self, *, status_code=None, code=None):
        super().__init__("gateway error")
        self.status_code = status_code
        self.code = code


class _GatewayOrderError(_GatewayApiError):
    pass


class _GatewayRequestError(RuntimeError):
    pass


def test_post_error_classifier_defaults_unknown_and_malformed_responses_to_ambiguous():
    gateway = object.__new__(BinanceSpotGateway)
    gateway.api_exception = _GatewayApiError
    gateway.order_exception = _GatewayOrderError
    gateway.request_exception = _GatewayRequestError
    assert gateway.classify_submission_error(RuntimeError("malformed 2xx body")) == (
        SubmissionClass.AMBIGUOUS
    )
    assert gateway.classify_submission_error(_GatewayRequestError("truncated JSON")) == (
        SubmissionClass.AMBIGUOUS
    )
    assert gateway.classify_submission_error(
        _GatewayApiError(status_code=504, code=-1000)
    ) == SubmissionClass.AMBIGUOUS


def test_post_error_classifier_allows_only_explicit_definite_rejections():
    gateway = object.__new__(BinanceSpotGateway)
    gateway.api_exception = _GatewayApiError
    gateway.order_exception = _GatewayOrderError
    gateway.request_exception = _GatewayRequestError
    assert gateway.classify_submission_error(
        _GatewayApiError(status_code=400, code=-2010)
    ) == SubmissionClass.DEFINITE_REJECT
    assert gateway.classify_submission_error(
        _GatewayOrderError(status_code=400, code=-1013)
    ) == SubmissionClass.DEFINITE_REJECT
    assert gateway.classify_submission_error(
        _GatewayApiError(status_code=400, code=-1007)
    ) == SubmissionClass.AMBIGUOUS


class FakeGateway:
    def __init__(self, *, pair="BTC/USDT", events=None):
        self.metadata = exchange_symbol(pair)
        self.events = events if events is not None else []
        self.place_calls = 0
        self.cancel_calls = 0
        self.cancel_error = None
        self.place_error = None
        self.submission_class = SubmissionClass.DEFINITE_REJECT
        self.lookup_result = None
        self.order_lookup = {}
        self.list_lookup = {}
        self.placed_orders = {}
        self.canceled_lists = {}
        self.on_place = None
        self.orders = []
        self.order_lists = []
        self.balances = {
            "BTC": (Decimal("0.0100"), Decimal("0")),
            "USDT": (Decimal("1000"), Decimal("0")),
            "USDC": (Decimal("1000"), Decimal("0")),
            "FDUSD": (Decimal("1000"), Decimal("0")),
        }

    def symbol_info(self, symbol):
        assert symbol == self.metadata["symbol"]
        return dict(self.metadata)

    def ticker_price(self, symbol):
        return {"symbol": symbol, "price": "110.00"}

    def order_book(self, symbol, limit=5):
        return {"bids": [["109.90", "1"]], "asks": [["110.00", "1"]]}

    def account(self):
        return {
            "balances": [
                {"asset": asset, "free": str(values[0]), "locked": str(values[1])}
                for asset, values in self.balances.items()
            ]
        }

    def open_orders(self, symbol=None):
        if symbol is None:
            return list(self.orders)
        return [row for row in self.orders if row.get("symbol") == symbol]

    def open_order_lists(self):
        return list(self.order_lists)

    def place(self, endpoint, params):
        self.place_calls += 1
        self.events.append("place")
        if self.on_place:
            self.on_place(endpoint, dict(params))
        if self.place_error is not None:
            raise self.place_error
        if endpoint == "order":
            status = "FILLED" if params.get("timeInForce") == "IOC" else "NEW"
            executed = str(params.get("quantity", "0")) if status == "FILLED" else "0"
            response = {
                "symbol": params["symbol"], "orderId": 9001, "status": status,
                "side": params.get("side"), "type": params.get("type"),
                "origQty": str(params.get("quantity", "0")), "executedQty": executed,
                "cummulativeQuoteQty": "0", "updateTime": 1,
            }
            self.placed_orders[9001] = dict(response)
            if status == "FILLED" and str(params.get("side")).upper() == "SELL":
                free, locked = self.balances["BTC"]
                self.balances["BTC"] = (free - Decimal(executed), locked)
            return response
        reports = []
        if endpoint in {"orderList/oto", "orderList/otoco"}:
            reports.append({"orderId": 7001, "side": "BUY", "type": "LIMIT"})
        reports.extend([
            {"orderId": 7002, "side": "SELL", "type": "LIMIT_MAKER"},
            {"orderId": 7003, "side": "SELL", "type": "STOP_LOSS_LIMIT"},
        ])
        return {"symbol": params["symbol"], "orderListId": 8001, "orderReports": reports}

    def classify_submission_error(self, exc):
        return self.submission_class

    def get_order(self, symbol, *, order_id=None, client_id=""):
        if order_id is not None and int(order_id) in self.order_lookup:
            return dict(self.order_lookup[int(order_id)])
        if order_id is not None and int(order_id) in self.placed_orders:
            return dict(self.placed_orders[int(order_id)])
        if isinstance(self.lookup_result, dict):
            return dict(self.lookup_result)
        raise FakeExchangeError("not found")

    def get_order_list(self, *, order_list_id=None, list_client_id=""):
        if order_list_id is not None and int(order_list_id) in self.list_lookup:
            return dict(self.list_lookup[int(order_list_id)])
        if order_list_id is not None and int(order_list_id) in self.canceled_lists:
            return dict(self.canceled_lists[int(order_list_id)])
        if isinstance(self.lookup_result, dict):
            return dict(self.lookup_result)
        raise FakeExchangeError("not found")

    def cancel_order(self, symbol, order_id):
        self.cancel_calls += 1
        self.events.append("cancel")
        if self.cancel_error:
            raise self.cancel_error
        return {"symbol": symbol, "orderId": int(order_id), "status": "CANCELED"}

    def cancel_order_list(self, symbol, order_list_id):
        self.cancel_calls += 1
        self.events.append("cancel")
        if self.cancel_error:
            raise self.cancel_error
        result = {"symbol": symbol, "orderListId": int(order_list_id),
                  "listStatusType": "ALL_DONE", "listOrderStatus": "ALL_DONE"}
        self.canceled_lists[int(order_list_id)] = dict(result)
        self.order_lists = [
            item for item in self.order_lists
            if int(item.get("orderListId", 0) or 0) != int(order_list_id)
        ]
        return result


class RecordingValidator:
    def __init__(self, events=None, error=None):
        self.events = events if events is not None else []
        self.error = error
        self.calls = []

    def validate_replacement(self, symbol, endpoint, params, **kwargs):
        self.events.append("preflight")
        self.calls.append((symbol, endpoint, dict(params), dict(kwargs)))
        if self.error:
            raise self.error
        return {"symbol": symbol, "endpoint": endpoint, "ok": True}


def make_live_adapter(tmp_path, *, gateway=None, validator=None, pair="BTC/USDT"):
    controller, pair_state = make_pair_controller(tmp_path, pair)
    store = make_store(tmp_path)
    store.register_symbol_pair(pair_state["symbol"], pair_state["pair"])
    guard = FakeGuard()
    gateway = gateway or FakeGateway(pair=pair)
    validator = validator or RecordingValidator()
    adapter = BitcoinSpotAdapter(
        store,
        guard,
        controller,
        gateway=gateway,
        filter_validator=validator,
        execution_mode="testnet",
    )
    return adapter, store, guard, controller, gateway, validator


def test_startup_reconciliation_preserves_restored_global_risk_pause(tmp_path):
    controller, pair_state = make_pair_controller(tmp_path)
    store = make_store(tmp_path)
    store.register_symbol_pair(pair_state["symbol"], pair_state["pair"])
    guard = FreshSignalGuard(tmp_path / "risk.json", state_store=store)
    guard.set_global_pause("daily-risk-limit")
    adapter = BitcoinSpotAdapter(
        store,
        guard,
        controller,
        gateway=FakeGateway(),
        filter_validator=RecordingValidator(),
        stream_factory=NoopStream,
        execution_mode="testnet",
    )
    adapter.verified_reconcile = lambda: {"ok": True, "detail": "verified"}

    assert adapter.start() is True
    assert guard.state["global_pause"] == "daily-risk-limit"
    assert store.entries() is False
    assert adapter.set_enabled(True) == "OFF: global risk pause: daily-risk-limit"
    guard.clear_global_pause()
    assert adapter.set_enabled(True) == "ON"


@pytest.mark.parametrize(
    ("mode", "expected_base"),
    [
        ("testnet", "https://testnet.binance.vision"),
        ("live", "https://api.binance.com"),
    ],
)
def test_execution_filter_metadata_uses_same_spot_environment(
    tmp_path, monkeypatch, mode, expected_base
):
    monkeypatch.delenv("BINANCE_SPOT_EXECUTION_PUBLIC_BASE", raising=False)
    controller, pair_state = make_pair_controller(tmp_path)
    store = make_store(tmp_path)
    store.register_symbol_pair(pair_state["symbol"], pair_state["pair"])
    adapter = BitcoinSpotAdapter(
        store, FakeGuard(), controller, gateway=FakeGateway(), execution_mode=mode
    )
    assert adapter.filter_validator.public.base == expected_base


def test_exact_bitcoin_pair_policy_and_exchange_metadata():
    assert canonical_pair("btc-usdt") == "BTC/USDT"
    assert canonical_pair("BTC_USDC") == "BTC/USDC"
    assert canonical_pair("btcfdusd") == "BTC/FDUSD"
    assert symbol_for_pair("BTC/USDC") == "BTCUSDC"

    assert canonical_pair("BTC/BUSD", allowed_quotes=()) == "BTC/BUSD"
    for forbidden in ("ETH/USDT", "ETHUSDT", "BTC", "USDT/BTC"):
        with pytest.raises(PairPolicyError):
            canonical_pair(forbidden)

    valid = validate_exchange_symbol("BTC/USDT", exchange_symbol("BTC/USDT"))
    assert valid["baseAsset"] == "BTC" and valid["quoteAsset"] == "USDT"

    wrong_quote = exchange_symbol("BTC/USDC")
    with pytest.raises(PairPolicyError, match="symbol|quote"):
        validate_exchange_symbol("BTC/USDT", wrong_quote)
    no_oco = exchange_symbol("BTC/USDT")
    no_oco["ocoAllowed"] = False
    with pytest.raises(PairPolicyError, match="OCO"):
        validate_exchange_symbol("BTC/USDT", no_oco)


def test_pair_state_hash_binds_pair_symbol_quote_and_generation(tmp_path):
    controller, state = make_pair_controller(tmp_path)
    assert state["state_hash"] == pair_state_hash(state)
    for field, changed in (
        ("pair", "BTC/USDC"),
        ("symbol", "BTCUSDC"),
        ("quote", "USDC"),
        ("generation", 2),
        ("pair_config_hash", "0" * 64),
    ):
        altered = dict(state, **{field: changed})
        assert pair_state_hash(altered) != state["state_hash"]
    assert controller.load() == state


@pytest.mark.parametrize(
    "projection",
    ["pairlist_hash", "pairlist_refresh", "overlay_whitelist", "overlay_blacklist"],
)
def test_pair_bootstrap_rejects_corrupted_complete_projection(tmp_path, projection):
    controller, state = make_pair_controller(tmp_path)
    if projection.startswith("pairlist"):
        path = controller.pairlist_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["pair_state_hash" if projection == "pairlist_hash" else "refresh_period"] = (
            "0" * 64 if projection == "pairlist_hash" else 999
        )
    else:
        path = controller.freqtrade_config_path
        data = json.loads(path.read_text(encoding="utf-8"))
        key = "pair_whitelist" if projection == "overlay_whitelist" else "pair_blacklist"
        data["exchange"][key] = ["BTC/USDC"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PairStateError):
        controller.bootstrap(state["pair"])


def test_pair_switch_requires_positive_flatness_and_atomically_updates_projections(tmp_path):
    controller, before = make_pair_controller(tmp_path)
    seen = []

    def not_flat(symbols):
        seen.append(set(symbols))
        return {"ok": False, "detail": "BTC position is open"}

    with pytest.raises(PairStateError, match="position is open"):
        controller.switch("BTC/USDC", not_flat)
    assert controller.load() == before
    assert seen == [{"BTCUSDT", "BTCUSDC"}]

    changed = controller.switch(
        "BTC/USDC",
        lambda symbols: {"ok": True, "symbols": sorted(symbols), "btc_total": "0"},
    )
    after = changed["state"]
    assert changed["changed"] is True
    assert after["generation"] == before["generation"] + 1
    assert after["pair"] == "BTC/USDC" and after["symbol"] == "BTCUSDC"
    pairlist = json.loads(controller.pairlist_path.read_text(encoding="utf-8"))
    overlay = json.loads(controller.freqtrade_config_path.read_text(encoding="utf-8"))
    assert pairlist["pairs"] == ["BTC/USDC"]
    assert pairlist["pair_state_hash"] == after["state_hash"]
    assert pairlist["pair_config_hash"] == after["pair_config_hash"]
    assert after["pair_config_hash"] == pair_config_hash("BTC/USDC")
    assert overlay["stake_currency"] == "USDC"
    assert overlay["exchange"]["pair_whitelist"] == ["BTC/USDC"]


def test_pair_bootstrap_rejects_partial_state(tmp_path):
    root = tmp_path / "partial"
    root.mkdir()
    (root / "active.json").write_text("{}", encoding="utf-8")
    controller = PairController(
        root / "active.json", root / "pairlist.json", root / "overlay.json"
    )
    with pytest.raises(PairStateError, match="partially present"):
        controller.bootstrap("BTC/USDT")


def test_client_ids_remain_unique_with_fixed_clock_entropy_and_concurrency(monkeypatch):
    import services.execution_sidecar.client_ids as client_ids_module

    monkeypatch.setattr(client_ids_module.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    monkeypatch.setattr(client_ids_module.secrets, "token_hex", lambda _n: "a" * 12)
    generator = ClientOrderIds("BTCB")
    with ThreadPoolExecutor(max_workers=32) as pool:
        values = list(pool.map(lambda _n: generator.new("ENTRYLIST"), range(2000)))
    assert len(values) == len(set(values)) == 2000
    assert all(len(value) <= 36 for value in values)
    assert all(re.fullmatch(r"[A-Z0-9_-]+", value) for value in values)


def test_operation_intent_is_submitting_before_the_only_network_post(tmp_path):
    gateway = FakeGateway()
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    observed = []
    params = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_LOSS_LIMIT",
        "quantity": "0.01",
        "price": "99",
        "timeInForce": "GTC",
        "newClientOrderId": "BTCB-TRAIL-INTENT1",
    }

    def inspect_ledger(_endpoint, posted):
        rows = store.unresolved_intents()
        observed.append((rows[0]["state"], json.loads(rows[0]["request_json"]), posted))

    gateway.on_place = inspect_ledger
    status, response, detail, intent_id = adapter._submit_once(
        operation="PROTECTION",
        trade_id="trade-1",
        symbol="BTCUSDT",
        endpoint="order",
        params=params,
    )
    assert status == "CONFIRMED" and detail == "confirmed"
    assert response["orderId"] == 9001
    assert gateway.place_calls == 1
    assert observed == [("SUBMITTING", params, params)]
    assert store.intent(intent_id)["state"] == "CONFIRMED"


@pytest.mark.parametrize(
    ("classification", "expected_state", "entries_stay_armed"),
    [
        (SubmissionClass.DEFINITE_REJECT, "DEFINITE_REJECT", True),
        (SubmissionClass.AMBIGUOUS, "AMBIGUOUS", False),
    ],
)
def test_reject_and_ambiguous_outcomes_are_durable_and_never_resent(
    tmp_path, classification, expected_state, entries_stay_armed
):
    gateway = FakeGateway()
    gateway.place_error = FakeExchangeError("submission failed")
    gateway.submission_class = classification
    adapter, store, guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    store.set_entries(True)
    params = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_LOSS_LIMIT",
        "quantity": "0.01",
        "price": "99",
        "timeInForce": "GTC",
        "newClientOrderId": "BTCB-TRAIL-SAME",
    }
    first = adapter._submit_once(
        operation="PROTECTION", trade_id="trade-1", symbol="BTCUSDT",
        endpoint="order", params=params,
    )
    second = adapter._submit_once(
        operation="PROTECTION", trade_id="trade-1", symbol="BTCUSDT",
        endpoint="order", params=params,
    )
    assert first[0] == expected_state
    assert second[0] == expected_state
    assert gateway.place_calls == 1
    assert store.intent(first[3])["state"] == expected_state
    assert store.entries() is entries_stay_armed
    if classification == SubmissionClass.AMBIGUOUS:
        assert guard.pauses[-1] == "ambiguous-order-submission-reconcile-required"
        assert store.unresolved_intents()[0]["state"] == "AMBIGUOUS"
    else:
        assert guard.pauses == []
        assert store.unresolved_intents() == []


def test_ambiguous_transport_error_can_be_confirmed_by_lookup_without_resend(tmp_path):
    gateway = FakeGateway()
    gateway.place_error = FakeExchangeError("connection reset after POST")
    gateway.submission_class = SubmissionClass.AMBIGUOUS
    gateway.lookup_result = {"symbol": "BTCUSDT", "orderId": 444, "status": "NEW"}
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    params = {
        "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_LOSS_LIMIT",
        "quantity": "0.01", "price": "99", "timeInForce": "GTC",
        "newClientOrderId": "BTCB-TRAIL-LOOKUP",
    }
    result = adapter._submit_once(
        operation="PROTECTION", trade_id="trade-1", symbol="BTCUSDT",
        endpoint="order", params=params,
    )
    assert result[0] == "CONFIRMED"
    assert result[1]["orderId"] == 444
    assert gateway.place_calls == 1
    assert store.intent(result[3])["state"] == "CONFIRMED"


def test_submission_error_secret_is_redacted_before_intent_persistence(tmp_path):
    sentinel = "SENTINEL-BINANCE-SIGNATURE-123456789"
    gateway = FakeGateway()
    gateway.place_error = FakeExchangeError(
        "https://api.binance.com/api/v3/order?signature=" + sentinel
        + "&listenKey=private-stream-key"
    )
    gateway.submission_class = SubmissionClass.AMBIGUOUS
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    params = {
        "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_LOSS_LIMIT",
        "quantity": "0.01", "price": "99", "timeInForce": "GTC",
        "newClientOrderId": "BTCB-TRAIL-SECRET",
    }

    result = adapter._submit_once(
        operation="PROTECTION", trade_id="trade-secret", symbol="BTCUSDT",
        endpoint="order", params=params,
    )

    persisted = str(store.intent(result[3]).get("error") or "")
    assert result[0] == "AMBIGUOUS"
    assert sentinel not in result[2] + persisted
    assert "private-stream-key" not in result[2] + persisted
    assert "[REDACTED]" in result[2] and "[REDACTED]" in persisted


def test_simulator_enforces_one_bitcoin_position_and_blocks_pair_switch(tmp_path):
    controller, active = make_pair_controller(tmp_path)
    store = make_store(tmp_path)
    store.register_symbol_pair(active["symbol"], active["pair"])
    store.upsert_trade("signal-1", active["pair"], lifecycle_state=LifecycleState.SIGNAL_APPROVED.value)
    adapter = SimulationAdapter(store, FakeGuard())
    assert adapter.start() is True
    assert adapter.set_enabled(True) == "ON"
    accepted, detail = adapter.submit(active["symbol"], trade_id="signal-1")
    assert accepted is True and "filled and protected" in detail

    store.upsert_trade("signal-2", active["pair"], lifecycle_state=LifecycleState.SIGNAL_APPROVED.value)
    accepted2, detail2 = adapter.submit(active["symbol"], trade_id="signal-2")
    assert accepted2 is False and detail2 == "maximum simulated positions reached"
    assert len(adapter.sim_positions) == 1
    flatness = adapter.verify_flat_for_switch({"BTCUSDT", "BTCUSDC"})
    assert flatness["ok"] is False and "position" in flatness["detail"]


def test_live_adapter_blocks_a_second_nonterminal_bitcoin_exposure_across_quotes(tmp_path):
    gateway = FakeGateway(pair="BTC/USDC")
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway, pair="BTC/USDC"
    )
    store.register_symbol_pair("BTCUSDT", "BTC/USDT")
    store.upsert_trade(
        "old-usdt-trade", "BTC/USDT", lifecycle_state=LifecycleState.PROTECTION_ACTIVE.value
    )
    store.upsert_trade(
        "new-usdc-signal", "BTC/USDC", lifecycle_state=LifecycleState.SIGNAL_APPROVED.value
    )
    store.set_entries(True)
    adapter.started = True
    adapter.enabled = True
    accepted, detail = adapter.submit("BTCUSDC", trade_id="new-usdc-signal")
    assert accepted is False
    assert detail == "another BTC exposure is nonterminal"
    assert gateway.place_calls == 0


def seed_protected_trade(store: StateStore, pair="BTC/USDT"):
    symbol = pair.replace("/", "")
    store.register_symbol_pair(symbol, pair)
    store.upsert_trade(
        "trade-1",
        pair,
        lifecycle_state=LifecycleState.PROTECTION_ACTIVE.value,
        order_list_id=5001,
        take_profit_order_id=5002,
        stop_order_id=5003,
        filled_quantity="0.0100",
        protected_quantity="0.0100",
        average_entry_price="100.00",
        protection_mode=ProtectionMode.OCO_TRAILING.value,
    )


def test_reconciliation_accepts_only_exchange_state_owned_by_durable_trade(tmp_path):
    gateway = FakeGateway()
    gateway.orders = [
        {"symbol": "BTCUSDT", "orderId": 5002, "type": "LIMIT_MAKER"},
        {"symbol": "BTCUSDT", "orderId": 5003, "type": "STOP_LOSS_LIMIT"},
    ]
    gateway.order_lists = [{"symbol": "BTCUSDT", "orderListId": 5001}]
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    result = adapter.verified_reconcile()
    assert result["ok"] is True
    assert result["endpoints"]["ownership"]["btc_total"] == "0.0100"


@pytest.mark.parametrize("mode", [ProtectionMode.OCO_TRAILING, ProtectionMode.TRAILING_ONLY])
def test_reconciliation_rejects_disappeared_durable_protection(tmp_path, mode):
    gateway = FakeGateway()
    gateway.orders = []
    gateway.order_lists = []
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    if mode is ProtectionMode.TRAILING_ONLY:
        store.upsert_trade(
            "trade-1", "BTC/USDT",
            lifecycle_state=LifecycleState.TRAILING_ACTIVE.value,
            protection_mode=mode.value,
            order_list_id=None,
            stop_order_id=5003,
        )
    result = adapter.verified_reconcile()
    assert result["ok"] is False
    assert "protection is not open" in result["endpoints"]["ownership"]["reason"]


@pytest.mark.parametrize("defect", ["unknown-order", "unknown-list", "unowned-btc", "missing-db"])
def test_reconciliation_fails_closed_on_unowned_exchange_state(tmp_path, defect):
    gateway = FakeGateway()
    gateway.balances["BTC"] = (Decimal("0"), Decimal("0"))
    adapter, store, guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    if defect != "missing-db":
        seed_protected_trade(store)
        gateway.balances["BTC"] = (Decimal("0.0100"), Decimal("0"))
        gateway.orders = [
            {"symbol": "BTCUSDT", "orderId": 5002, "type": "LIMIT_MAKER"},
            {"symbol": "BTCUSDT", "orderId": 5003, "type": "STOP_LOSS_LIMIT"},
        ]
        gateway.order_lists = [{"symbol": "BTCUSDT", "orderListId": 5001}]
    if defect == "unknown-order":
        gateway.orders.append(
            {"symbol": "BTCUSDT", "orderId": 9999, "type": "LIMIT"}
        )
    elif defect == "unknown-list":
        gateway.order_lists.append({"symbol": "BTCUSDT", "orderListId": 9999})
    elif defect == "unowned-btc":
        gateway.balances["BTC"] = (Decimal("0.0200"), Decimal("0"))
    else:
        gateway.orders = [{"symbol": "BTCUSDT", "orderId": 9999, "type": "LIMIT"}]
    result = adapter.verified_reconcile()
    assert result["ok"] is False
    assert result["endpoints"]["ownership"]["ok"] is False
    assert store.entries() is False
    assert guard.pauses[-1] == "reconciliation-failed"


def test_emergency_exit_never_sells_unrelated_free_bitcoin(tmp_path):
    gateway = FakeGateway()
    gateway.balances["BTC"] = (Decimal("0.0500"), Decimal("0"))
    submitted = []
    gateway.on_place = lambda endpoint, params: submitted.append((endpoint, params))
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    result = adapter.emergency_exit("BTCUSDT")
    assert result["submitted"] is True
    assert submitted[0][0] == "order"
    assert Decimal(submitted[0][1]["quantity"]) == Decimal("0.0100")
    assert Decimal(submitted[0][1]["quantity"]) < gateway.balances["BTC"][0]


def test_conversion_preflight_failure_occurs_before_any_cancel_or_replacement(tmp_path):
    events = []
    gateway = FakeGateway(events=events)
    validator = RecordingValidator(events, error=ValueError("filter violation"))
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway, validator=validator
    )
    seed_protected_trade(store)
    with pytest.raises(ValueError, match="filter violation"):
        adapter.convert("BTCUSDT", ProtectionMode.TRAILING_ONLY)
    assert events == ["preflight"]
    assert gateway.cancel_calls == 0
    assert gateway.place_calls == 0
    assert store.unresolved_intents() == []


class _MetadataPublicClient:
    def __init__(self, metadata):
        self.metadata = metadata

    def exchange_info(self, symbol):
        assert symbol == self.metadata["symbol"]
        return {"symbols": [self.metadata]}

    def execution_rules(self, symbol):
        assert symbol == self.metadata["symbol"]
        return {"symbolRules": []}

    def ticker_price(self, symbol):
        return {"symbol": symbol, "price": "110.00"}


@pytest.mark.parametrize("defect", ["capability", "filter", "bounds"])
def test_trailing_conversion_rejects_unsupported_metadata_before_cancel(tmp_path, defect):
    events = []
    gateway = FakeGateway(events=events)
    if defect == "capability":
        gateway.metadata["allowTrailingStop"] = False
    elif defect == "filter":
        gateway.metadata["filters"] = [
            item for item in gateway.metadata["filters"]
            if item.get("filterType") != "TRAILING_DELTA"
        ]
    else:
        trailing = next(
            item for item in gateway.metadata["filters"]
            if item.get("filterType") == "TRAILING_DELTA"
        )
        trailing.pop("minTrailingBelowDelta")
    validator = SpotFilterValidator(_MetadataPublicClient(gateway.metadata), max_age_seconds=300)
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway, validator=validator
    )
    seed_protected_trade(store)
    with pytest.raises(FilterViolation):
        adapter.convert("BTCUSDT", ProtectionMode.TRAILING_ONLY)
    assert gateway.cancel_calls == 0
    assert gateway.place_calls == 0


def test_conversion_orders_preflight_then_cancel_then_intent_backed_post(tmp_path):
    events = []
    gateway = FakeGateway(events=events)
    validator = RecordingValidator(events)
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway, validator=validator
    )
    seed_protected_trade(store)
    observed_submission_intent = []

    def inspect_ledger(_endpoint, _params):
        candidates = [row for row in store.unresolved_intents()
                      if row["operation"] == "PROTECTION"]
        observed_submission_intent.append(
            [(row["state"], row["endpoint"]) for row in candidates]
        )

    gateway.on_place = inspect_ledger
    ok, detail = adapter.convert("BTCUSDT", ProtectionMode.TRAILING_ONLY)
    assert ok is True and "TRAILING_ONLY" in detail
    assert events == ["preflight", "cancel", "preflight", "place"]
    assert observed_submission_intent == [[("SUBMITTING", "order")]]
    assert gateway.cancel_calls == 1 and gateway.place_calls == 1
    assert store.unresolved_intents() == []
    row = store.active_trade_for_symbol("BTCUSDT")
    assert row["lifecycle_state"] == LifecycleState.TRAILING_ACTIVE.value
    assert row["stop_order_id"] == 9001


def test_ambiguous_cancel_blocks_replacement_post_and_remains_reconcilable(tmp_path):
    events = []
    gateway = FakeGateway(events=events)
    gateway.cancel_error = FakeExchangeError("cancel connection lost")
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway, validator=RecordingValidator(events)
    )
    seed_protected_trade(store)
    ok, detail = adapter.convert("BTCUSDT", ProtectionMode.TRAILING_ONLY)
    assert ok is False and "cancel outcome is ambiguous" in detail
    assert events == ["preflight", "cancel"]
    assert gateway.place_calls == 0
    unresolved = store.unresolved_intents()
    assert {item["operation"] for item in unresolved} == {
        "REPLACE_PROTECTION", "CANCEL_PROTECTION"
    }
    assert next(item for item in unresolved
                if item["operation"] == "REPLACE_PROTECTION")["state"] == "PREPARED"
    assert next(item for item in unresolved
                if item["operation"] == "CANCEL_PROTECTION")["state"] == "AMBIGUOUS"


def test_list_executing_event_does_not_claim_otoco_pending_sells_are_active(tmp_path):
    store = make_store(tmp_path)
    store.register_symbol_pair("BTCUSDT", "BTC/USDT")
    store.upsert_trade(
        "entry-list", "BTC/USDT", lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
        order_list_id=5001, entry_order_id=6001, entry_submitted_at=str(time.time()),
        protection_mode=ProtectionMode.OCO_TRAILING.value,
    )
    store.record_exchange_event({
        "e": "listStatus", "s": "BTCUSDT", "g": 5001,
        "l": "EXEC_STARTED", "L": "EXECUTING", "E": 1,
    })
    assert store.trade("entry-list")["lifecycle_state"] == LifecycleState.ENTRY_SUBMITTED.value
    assert store.trade("entry-list")["reconciliation_status"] == (
        "ORDER_LIST_EXECUTING_PHASE_UNPROVEN"
    )


def test_early_user_stream_fill_is_replayed_after_rest_ids_are_bound(tmp_path):
    store = make_store(tmp_path)
    store.register_symbol_pair("BTCUSDT", "BTC/USDT")
    event = {
        "e": "executionReport", "s": "BTCUSDT", "i": 6001, "g": 5001,
        "S": "BUY", "X": "PARTIALLY_FILLED", "o": "LIMIT",
        "z": "0.005", "Z": "0.5", "q": "0.01", "E": 2, "I": 22,
    }
    assert store.record_exchange_event(event) is True
    store.upsert_trade(
        "race", "BTC/USDT", lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
        entry_order_id=6001, order_list_id=5001, entry_submitted_at=str(time.time()),
    )
    assert store.replay_exchange_events_for_trade("race") == 1
    row = store.trade("race")
    assert row["lifecycle_state"] == LifecycleState.ENTRY_PARTIALLY_FILLED.value
    assert Decimal(row["filled_quantity"]) == Decimal("0.005")


def test_partial_working_buy_is_canceled_terminal_then_protected(tmp_path):
    events = []
    gateway = FakeGateway(events=events)
    gateway.balances["BTC"] = (Decimal("0.005"), Decimal("0"))
    gateway.orders = [{"symbol": "BTCUSDT", "orderId": 6001, "status": "PARTIALLY_FILLED"}]
    gateway.order_lists = [{"symbol": "BTCUSDT", "orderListId": 5001}]
    gateway.order_lookup[6001] = {
        "symbol": "BTCUSDT", "orderId": 6001, "orderListId": 5001,
        "side": "BUY", "type": "LIMIT", "status": "PARTIALLY_FILLED",
        "origQty": "0.01", "executedQty": "0.005", "cummulativeQuoteQty": "0.5",
        "updateTime": 2,
    }
    original_cancel = gateway.cancel_order_list

    def cancel_and_terminalize(symbol, order_list_id):
        result = original_cancel(symbol, order_list_id)
        gateway.order_lookup[6001] = dict(
            gateway.order_lookup[6001], status="CANCELED", updateTime=3
        )
        gateway.orders = []
        return result

    gateway.cancel_order_list = cancel_and_terminalize
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway, validator=RecordingValidator(events)
    )
    store.upsert_trade(
        "partial", "BTC/USDT", lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
        entry_client_order_id="ENTRY-CID", entry_submitted_at=str(time.time()),
        entry_order_id=6001, order_list_id=5001,
        take_profit_order_id=6002, stop_order_id=6003,
        protection_mode=ProtectionMode.OCO_TRAILING.value,
    )
    adapter._on_order_update({
        "e": "executionReport", "s": "BTCUSDT", "i": 6001, "g": 5001,
        "S": "BUY", "X": "PARTIALLY_FILLED", "o": "LIMIT",
        "z": "0.005", "Z": "0.5", "q": "0.01", "E": 2, "I": 23,
    })
    adapter.tick()
    row = store.trade("partial")
    assert events[-3:] == ["cancel", "preflight", "place"]
    assert gateway.cancel_calls == 1 and gateway.place_calls == 1
    assert row["lifecycle_state"] == LifecycleState.PROTECTION_ACTIVE.value
    assert Decimal(row["protected_quantity"]) == Decimal("0.005")


def test_restart_cancels_stale_unfilled_entry_before_it_can_fill_late(tmp_path):
    gateway = FakeGateway()
    gateway.balances["BTC"] = (Decimal("0"), Decimal("0"))
    gateway.orders = [{"symbol": "BTCUSDT", "orderId": 6001, "status": "NEW"}]
    gateway.order_lists = [{"symbol": "BTCUSDT", "orderListId": 5001}]
    gateway.order_lookup[6001] = {
        "symbol": "BTCUSDT", "orderId": 6001, "orderListId": 5001,
        "side": "BUY", "type": "LIMIT", "status": "NEW", "origQty": "0.01",
        "executedQty": "0", "cummulativeQuoteQty": "0", "updateTime": 2,
    }
    original_cancel = gateway.cancel_order_list

    def cancel_and_terminalize(symbol, order_list_id):
        result = original_cancel(symbol, order_list_id)
        gateway.order_lookup[6001] = dict(
            gateway.order_lookup[6001], status="CANCELED", updateTime=3
        )
        gateway.orders = []
        return result

    gateway.cancel_order_list = cancel_and_terminalize
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    store.upsert_trade(
        "stale", "BTC/USDT", lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
        entry_client_order_id="STALE-CID",
        entry_submitted_at=str(time.time() - adapter.max_entry_open_seconds - 1),
        entry_order_id=6001, order_list_id=5001,
        take_profit_order_id=6002, stop_order_id=6003,
        protection_mode=ProtectionMode.OCO_TRAILING.value,
    )
    result = adapter.verified_reconcile()
    assert result["ok"] is True
    assert gateway.cancel_calls == 1 and gateway.place_calls == 0
    assert store.trade("stale")["lifecycle_state"] == LifecycleState.ENTRY_REJECTED.value


@pytest.mark.parametrize("held", ["0.0100", "0.0050"])
def test_restart_resumes_prepared_replacement_only_after_confirmed_cancel(tmp_path, held):
    submitted = []
    gateway = FakeGateway()
    gateway.balances["BTC"] = (Decimal(held), Decimal("0"))
    gateway.on_place = lambda endpoint, params: submitted.append((endpoint, dict(params)))
    gateway.canceled_lists[5001] = {
        "symbol": "BTCUSDT", "orderListId": 5001,
        "listStatusType": "ALL_DONE", "listOrderStatus": "ALL_DONE",
    }
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    rules = adapter._rules("BTC/USDT")
    endpoint, request = adapter.factory.replacement(
        ProtectionMode.TRAILING_ONLY, rules, Decimal("0.0100"),
        Decimal("100"), Decimal("110"),
    )
    assert store.prepare_intent(
        intent_id="plan", operation="REPLACE_PROTECTION", trade_id="trade-1",
        symbol="BTCUSDT", endpoint=endpoint, request=request,
        client_order_id=request["newClientOrderId"],
    )
    cancel_request = {"symbol": "BTCUSDT", "orderListId": 5001, "orderId": 5003}
    assert store.prepare_intent(
        intent_id="cancel", operation="CANCEL_PROTECTION", trade_id="trade-1",
        symbol="BTCUSDT", endpoint="DELETE/orderList", request=cancel_request,
    )
    store.mark_intent_submitting("cancel")
    store.finish_intent("cancel", "CONFIRMED", exchange_order_list_id=5001)
    result = adapter._resolve_operation_intents()
    assert result["remaining"] == 0
    assert gateway.place_calls == 1
    assert Decimal(submitted[0][1]["quantity"]) == Decimal(held)
    assert store.intent("plan")["state"] == "ABORTED"
    assert store.active_trade_for_symbol("BTCUSDT")["lifecycle_state"] == (
        LifecycleState.TRAILING_ACTIVE.value
    )


def test_restart_resumes_atomically_prepared_child_and_never_reposts_ambiguous_child(tmp_path):
    gateway = FakeGateway()
    gateway.canceled_lists[5001] = {
        "symbol": "BTCUSDT", "orderListId": 5001,
        "listStatusType": "ALL_DONE", "listOrderStatus": "ALL_DONE",
    }
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    rules = adapter._rules("BTC/USDT")
    endpoint, request = adapter.factory.replacement(
        ProtectionMode.TRAILING_ONLY, rules, Decimal("0.0100"),
        Decimal("100"), Decimal("110"),
    )
    assert store.prepare_intent(
        intent_id="plan-child", operation="REPLACE_PROTECTION", trade_id="trade-1",
        symbol="BTCUSDT", endpoint=endpoint, request=request,
    )
    assert store.prepare_intent(
        intent_id="child", operation="PROTECTION", trade_id="trade-1",
        symbol="BTCUSDT", endpoint=endpoint, request=request,
        client_order_id=request["newClientOrderId"], parent_intent_id="plan-child",
        close_parent_intent_id="plan-child",
    )
    result = adapter._resolve_operation_intents()
    assert result["remaining"] == 0 and gateway.place_calls == 1
    assert store.intent("child")["state"] == "CONFIRMED"

    # A may-have-landed child is resolved by client-ID GET and is never POSTed.
    gateway2 = FakeGateway()
    adapter2, store2, _guard2, _controller2, _gateway2, _validator2 = make_live_adapter(
        tmp_path / "ambiguous", gateway=gateway2
    )
    seed_protected_trade(store2)
    assert store2.prepare_intent(
        intent_id="plan-amb", operation="REPLACE_PROTECTION", trade_id="trade-1",
        symbol="BTCUSDT", endpoint=endpoint, request=request,
    )
    assert store2.prepare_intent(
        intent_id="child-amb", operation="PROTECTION", trade_id="trade-1",
        symbol="BTCUSDT", endpoint=endpoint, request=request,
        client_order_id=request["newClientOrderId"], parent_intent_id="plan-amb",
        close_parent_intent_id="plan-amb",
    )
    store2.mark_intent_submitting("child-amb")
    gateway2.lookup_result = {
        "symbol": "BTCUSDT", "orderId": 9100, "status": "NEW",
        "side": "SELL", "type": "STOP_LOSS_LIMIT", "origQty": "0.01",
        "executedQty": "0", "updateTime": 4,
    }
    result2 = adapter2._resolve_operation_intents()
    assert result2["remaining"] == 0 and gateway2.place_calls == 0
    assert store2.intent("child-amb")["state"] == "CONFIRMED"


def test_rest_reconstruction_of_missed_stop_fill_updates_guard_once(tmp_path):
    gateway = FakeGateway()
    gateway.balances["BTC"] = (Decimal("0"), Decimal("0"))
    gateway.orders = []
    gateway.order_lists = []
    gateway.order_lookup[5003] = {
        "symbol": "BTCUSDT", "orderId": 5003, "orderListId": 5001,
        "side": "SELL", "type": "STOP_LOSS_LIMIT", "status": "FILLED",
        "origQty": "0.01", "executedQty": "0.01", "cummulativeQuoteQty": "0.9",
        "updateTime": 5,
    }
    adapter, store, guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    first = adapter.verified_reconcile()
    second = adapter.verified_reconcile()
    assert first["ok"] is True and second["ok"] is True
    assert store.trade("trade-1")["lifecycle_state"] == LifecycleState.EXIT_FILLED.value
    stop_events = [event for event in guard.events if event.get("i") == 5003]
    assert len(stop_events) == 1


def test_auto_bullish_trailing_conversion_happens_once_not_every_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_BREAK_EVEN_TRIGGER_PCT", "20")
    gateway = FakeGateway()
    adapter, store, _guard, controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    store.data["auto_protection_enabled"] = True
    store.save()
    active = controller.load()
    flow = {
        "ok": True, "pair_state_hash": active["state_hash"],
        "generated_at_epoch": time.time(), "classification": {"bullish": True},
    }
    adapter.maybe_auto_manage(flow)
    adapter.maybe_auto_manage(flow)
    assert gateway.cancel_calls == 1 and gateway.place_calls == 1
    assert store.active_trade_for_symbol("BTCUSDT")["lifecycle_state"] == (
        LifecycleState.TRAILING_ACTIVE.value
    )


def test_emergency_exit_sells_fee_residual_and_verifies_terminal_balance_delta(tmp_path):
    gateway = FakeGateway()
    gateway.balances["BTC"] = (Decimal("1.0000"), Decimal("0"))
    submitted = []
    gateway.on_place = lambda endpoint, params: submitted.append(dict(params))
    adapter, store, _guard, _controller, _gateway, _validator = make_live_adapter(
        tmp_path, gateway=gateway
    )
    seed_protected_trade(store)
    store.upsert_trade(
        "trade-1", "BTC/USDT", filled_quantity="1.0000", protected_quantity="0.9990"
    )
    result = adapter.emergency_exit("BTCUSDT")
    assert result["ok"] is True
    assert Decimal(submitted[0]["quantity"]) == Decimal("1.0000")
    assert Decimal(result["remaining_bot_owned_base"]) == 0


def sample_rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        base="BTC",
        quote="USDT",
        tick=Decimal("0.1"),
        step=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        max_qty=Decimal("100"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        trail_min=10,
        trail_max=2000,
        oco_allowed=True,
        oto_allowed=True,
        trailing_allowed=True,
    )


def test_fixed_otoco_prices_and_fee_shaved_pending_quantity_are_conservative():
    factory = OrderRequestFactory(
        lambda role: "CID-" + role,
        ProtectionSettings(
            take_profit_pct=Decimal("1.2"),
            fixed_stop_pct=Decimal("2.0"),
            trailing_delta_bips=40,
            limit_fill_buffer_bips=20,
            fee_pct_per_side=Decimal("0.1"),
        ),
    )
    endpoint, request = factory.entry(
        ProtectionMode.FIXED_OCO, sample_rules(), Decimal("1"), Decimal("100")
    )
    assert endpoint == "orderList/otoco"
    assert Decimal(request["workingPrice"]) == Decimal("100")
    assert Decimal(request["pendingQuantity"]) == Decimal("0.999")
    assert Decimal(request["pendingAbovePrice"]) == Decimal("101.2")
    assert Decimal(request["pendingBelowStopPrice"]) == Decimal("98")
    assert Decimal(request["pendingBelowPrice"]) == Decimal("97.8")
    assert Decimal(request["pendingBelowPrice"]) < Decimal(request["pendingBelowStopPrice"])


def test_trailing_and_profit_lock_replacement_prices_stay_on_safe_side():
    factory = OrderRequestFactory(lambda role: "CID-" + role)
    endpoint, trailing = factory.replacement(
        ProtectionMode.TRAILING_ONLY,
        sample_rules(),
        Decimal("0.01"),
        Decimal("100"),
        Decimal("110"),
        trailing_delta_bips=5,
    )
    assert endpoint == "order"
    assert trailing["trailingDelta"] == 10  # clamped to exchange minimum
    assert Decimal(trailing["price"]) == Decimal("109.6")
    assert Decimal(trailing["price"]) < Decimal("110")

    endpoint, locked = factory.replacement(
        ProtectionMode.FIXED_OCO,
        sample_rules(),
        Decimal("0.01"),
        Decimal("100"),
        Decimal("110"),
        stop_price=Decimal("100.3"),
    )
    assert endpoint == "orderList/oco"
    assert Decimal(locked["belowPrice"]) < Decimal(locked["belowStopPrice"])
    assert Decimal(locked["belowStopPrice"]) < Decimal("110")
    assert Decimal(locked["abovePrice"]) > Decimal("110")


def test_fee_adjusted_break_even_accounts_for_both_fees_and_slippage():
    result = fee_adjusted_break_even(
        Decimal("100"),
        buy_fee_pct=Decimal("0.1"),
        sell_fee_pct=Decimal("0.1"),
        slippage_pct=Decimal("0.05"),
    )
    expected = Decimal("100") * Decimal("1.0015") / Decimal("0.9985")
    assert result == expected
    assert result > Decimal("100.3")
