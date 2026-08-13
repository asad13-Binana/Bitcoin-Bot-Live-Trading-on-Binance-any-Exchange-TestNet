"""Control-plane and public money-flow safety tests.

These tests are deliberately offline.  Network-facing components are exercised
through deterministic fakes, while static assertions make the public-only and
single-order-owner boundaries reviewable without Binance credentials.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
import math
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
import yaml

from services.common import envelope
from services.common.audit import audit
from services.common.redaction import redact_text
from services.execution_sidecar import main as sidecar_main
from services.execution_sidecar.bitcoin_adapter import BitcoinSpotAdapter
from services.execution_sidecar.order_manager import OrderManager
from services.execution_sidecar.pair_control import PairController
from services.execution_sidecar.state_store import StateStore
from services.execution_sidecar.user_data_stream import ModernUserDataStream
from services.moneyflow.analytics import (
    aggregate_trade_flow,
    classify,
    depth_metrics,
    timeframe_context,
)
from services.moneyflow.client import MoneyFlowClient
from services.moneyflow.service import TIMEFRAMES, collect
from services.telegram_broker import authorization
from services.telegram_broker import bot as telegram_bot
from services.telegram_broker.callbacks import CallbackStore


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = ROOT / "freqtrade/user_data/strategies/IctSmcStrategy.py"


@pytest.fixture(autouse=True)
def bounded_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOWED_STABLE_QUOTES", "USDT,USDC,FDUSD")
    monkeypatch.setenv("MAX_FLOW_AGE_SECONDS", "45")
    monkeypatch.setenv("AUDIT_LOG", str(tmp_path / "audit" / "events.jsonl"))
    monkeypatch.setenv("COMMAND_HMAC_KEY", "c" * 64)
    monkeypatch.setenv("ENVELOPE_RELEASE_HASH", "a" * 64)


def _book():
    return {
        "bids": [["99", "2"], ["98", "1"]],
        "asks": [["101", "1"], ["102", "2"]],
    }


def test_quoted_json_and_signed_query_values_are_redacted():
    sentinel = "SENTINEL-SIGNATURE-123456789"
    raw = (
        '{"signature":"' + sentinel + '","token":"telegram-secret"} '
        'https://api.binance.com/api/v3/order?signature=' + sentinel
        + '&listenKey=stream-secret'
    )
    cleaned = redact_text(raw)
    assert sentinel not in cleaned
    assert "telegram-secret" not in cleaned
    assert "stream-secret" not in cleaned
    assert cleaned.count("[REDACTED]") >= 4


def test_command_results_redact_before_json_and_sqlite_persistence(tmp_path, monkeypatch):
    results = tmp_path / "results"
    monkeypatch.setattr(sidecar_main, "COMMAND_RESULTS_DIR", results)
    store = StateStore(tmp_path / "state.json", tmp_path / "state.sqlite")
    sentinel = "SENTINEL-SIGNATURE-123456789"
    raw = '{"signature":"' + sentinel + '"}?listenKey=stream-secret'
    sidecar_main._write_command_result("redact-1", "reconcile", False, raw, store)
    disk = (results / "command_result_redact-1.json").read_text(encoding="utf-8")
    durable = store.command_result("redact-1") or ""
    audit_log = (tmp_path / "audit" / "events.jsonl").read_text(encoding="utf-8")
    persisted = disk + durable + audit_log
    assert sentinel not in persisted
    assert "stream-secret" not in persisted
    assert "[REDACTED]" in disk and "[REDACTED]" in durable
    assert "[REDACTED]" in audit_log


def test_audit_jsonl_redacts_nested_and_free_form_secrets(tmp_path):
    target = tmp_path / "audit" / "events.jsonl"
    sentinel = "SENTINEL-AUDIT-SIGNATURE-123456789"
    audit(
        "redaction_probe",
        details={
            "signature": sentinel,
            "message": '{"listenKey":"private-stream-key"}',
        },
        path=str(target),
    )
    persisted = target.read_text(encoding="utf-8")
    assert sentinel not in persisted
    assert "private-stream-key" not in persisted
    assert persisted.count("[REDACTED]") >= 2


def test_telegram_send_is_a_final_redaction_sink(monkeypatch):
    captured = []

    class Response:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(telegram_bot, "TOKEN", "configured")
    monkeypatch.setattr(
        telegram_bot.requests, "post",
        lambda *args, **kwargs: captured.append(kwargs["data"]) or Response(),
    )
    telegram_bot.send('{"signature":"SENTINEL-TELEGRAM-SECRET"}', "7")
    assert captured
    assert "SENTINEL-TELEGRAM-SECRET" not in captured[0]["text"]
    assert "[REDACTED]" in captured[0]["text"]


def _resign_envelope(document: dict, key: bytes = b"c" * 64) -> dict:
    body = {name: value for name, value in document.items() if name != "signature"}
    return dict(
        body,
        signature=hmac.new(key, envelope.canonical_bytes(body), hashlib.sha256).hexdigest(),
    )


@pytest.mark.parametrize("created", [float("nan"), float("inf"), float("-inf")])
def test_envelope_signing_rejects_nonfinite_created_time(created):
    with pytest.raises(envelope.EnvelopeError, match="finite"):
        envelope.sign_envelope(
            producer="telegram-broker", purpose=envelope.BUS_COMMAND,
            payload={"command_id": "finite"}, ttl_seconds=120,
            key=b"c" * 64, now=created,
        )


@pytest.mark.parametrize("ttl", [0, -1, 3601, float("inf")])
def test_envelope_signing_rejects_unsafe_ttl(ttl):
    with pytest.raises(envelope.EnvelopeError, match="TTL"):
        envelope.sign_envelope(
            producer="telegram-broker", purpose=envelope.BUS_COMMAND,
            payload={"command_id": "ttl"}, ttl_seconds=ttl,
            key=b"c" * 64, now=100.0,
        )


@pytest.mark.parametrize(
    ("created", "expires"),
    [
        (float("nan"), 200.0),
        (100.0, float("inf")),
        (100.0, float("-inf")),
        (100.0, 100.0),
        (101.0, 100.0),
        (100.0, 3701.0),
    ],
)
def test_validly_signed_envelope_rejects_nonfinite_reversed_or_excessive_window(
    created, expires
):
    signed = envelope.sign_envelope(
        producer="telegram-broker", purpose=envelope.BUS_COMMAND,
        payload={"command_id": "tampered"}, ttl_seconds=120,
        key=b"c" * 64, now=100.0,
    )
    signed["created_at"], signed["expires_at"] = created, expires
    signed = _resign_envelope(signed)
    with pytest.raises(envelope.EnvelopeError):
        envelope.verify_envelope(
            signed, purpose=envelope.BUS_COMMAND,
            expected_producers={"telegram-broker"}, key=b"c" * 64, now=150.0,
        )


def _klines(*, count=64, bad=False):
    rows = [[index, "0", "0", "0", str(100 + index), "1", index + 1]
            for index in range(count)]
    if bad and rows:
        rows[-2][4] = "NaN"
    return rows


def _spot_metadata(symbol="BTCUSDT"):
    quote = symbol.removeprefix("BTC")
    return {
        "symbol": symbol,
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": quote,
        "permissions": ["SPOT"],
        "isSpotTradingAllowed": True,
        "ocoAllowed": True,
        "otoAllowed": True,
        "allowTrailingStop": True,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "10000000",
             "tickSize": "0.1"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "100",
             "stepSize": "0.00001"},
        ],
    }


def _pair_state(pair="BTC/USDT", generation=1):
    from services.common.market_policy import pair_config_hash, pair_state_hash

    symbol = pair.replace("/", "")
    state = {
        "schema_version": 1,
        "pair": pair,
        "symbol": symbol,
        "base": "BTC",
        "quote": pair.split("/", 1)[1],
        "generation": generation,
        "updated_at": "2026-07-22T00:00:00+00:00",
        "source": "test",
    }
    state["pair_config_hash"] = pair_config_hash(pair)
    state["state_hash"] = pair_state_hash(state)
    return state


def test_depth_and_trade_flow_formulas_preserve_quote_notional_and_taker_side():
    depth = depth_metrics(_book(), band_bps=200)
    assert depth["mid"] == pytest.approx(100.0)
    assert depth["spread_bps"] == pytest.approx(200.0)
    assert depth["bid_quote_in_band"] == pytest.approx(296.0)
    assert depth["ask_quote_in_band"] == pytest.approx(305.0)
    assert depth["imbalance"] == pytest.approx((296 - 305) / (296 + 305))

    trades = aggregate_trade_flow([
        {"a": 10, "p": "100", "q": "2", "m": False},
        {"a": 11, "p": "101", "q": "1", "m": True},
    ])
    # Binance m=false means the buyer was the aggressive taker; m=true means
    # the aggressive taker sold.
    assert trades == {
        "taker_buy_quote": 200.0,
        "taker_sell_quote": 101.0,
        "taker_buy_ratio": pytest.approx(200 / 301),
        "cvd_quote": 99.0,
        "trade_count": 2,
        "first_aggregate_id": 10,
        "last_aggregate_id": 11,
    }
    assert aggregate_trade_flow([])["taker_buy_ratio"] == 0.0


@pytest.mark.parametrize(
    "book",
    [
        {},
        {"bids": [["100", "1"]], "asks": [["100", "1"]]},
        {"bids": [["NaN", "1"]], "asks": [["101", "1"]]},
        {"bids": [["99", "-1"]], "asks": [["101", "1"]]},
    ],
)
def test_depth_rejects_empty_crossed_nonfinite_and_negative_books(book):
    with pytest.raises(ValueError):
        depth_metrics(book)


@pytest.mark.parametrize(
    "rows",
    [
        [{"p": "0", "q": "1", "m": False}],
        [{"p": "100", "q": "-1", "m": False}],
        [{"p": "Infinity", "q": "1", "m": False}],
        [{"p": None, "q": "1", "m": False}],
    ],
)
def test_trade_flow_rejects_malformed_or_nonfinite_rows(rows):
    with pytest.raises(ValueError):
        aggregate_trade_flow(rows)


def test_timeframe_context_uses_only_closed_candles_and_rejects_bad_series():
    rows = _klines()
    context = timeframe_context(rows)
    assert context["closed_candles"] == 63
    assert context["close"] == 162.0  # final (potentially open) close 163 is dropped
    assert context["direction"] == "bullish"
    with pytest.raises(ValueError, match="insufficient|malformed"):
        timeframe_context(_klines(count=10))
    with pytest.raises(ValueError, match="insufficient|malformed"):
        timeframe_context(_klines(bad=True))


def test_classification_requires_spot_pressure_futures_confirmation_and_higher_context():
    bullish = {
        "spot": {
            "trades": {"taker_buy_ratio": 0.60},
            "depth": {"imbalance": 0.10},
        },
        "futures": {
            "available": True,
            "depth": {"imbalance": 0.01},
            "taker": {"buySellRatio": "1.1"},
        },
        "timeframes": {
            "1m": {"direction": "mixed"},
            "5m": {"direction": "mixed"},
            "15m": {"direction": "bullish"},
            "1h": {"direction": "bullish"},
            "2h": {"direction": "bearish"},
            "4h": {"direction": "bullish"},
            "1d": {"direction": "bearish"},
        },
    }
    assert classify(bullish)["decision"] == "BULLISH"
    bearish_futures = json.loads(json.dumps(bullish))
    bearish_futures["futures"]["taker"]["buySellRatio"] = "0.9"
    assert classify(bearish_futures)["bullish"] is False
    no_futures = json.loads(json.dumps(bullish))
    no_futures["futures"] = {"available": False}
    result = classify(no_futures)
    assert result["bullish"] is True
    assert result["futures_confirmation"] is None
    only_two_higher = json.loads(json.dumps(bullish))
    only_two_higher["timeframes"]["4h"]["direction"] = "mixed"
    assert classify(only_two_higher)["higher_timeframe_bullish"] is False
    assert classify(only_two_higher)["bullish"] is False


class FakeFlowClient:
    def __init__(self, *, futures_available=True, bad_timeframe=None):
        self.futures_available = futures_available
        self.bad_timeframe = bad_timeframe
        self.calls = []

    def spot_exchange_symbol(self, symbol):
        self.calls.append(("spot_exchange_symbol", symbol))
        return _spot_metadata(symbol)

    def spot_depth(self, symbol, limit):
        self.calls.append(("spot_depth", symbol, limit))
        return {"bids": [["100.00", "5"]], "asks": [["100.01", "1"]]}

    def spot_aggregate_trades(self, symbol, limit):
        self.calls.append(("spot_aggregate_trades", symbol, limit))
        return [{"a": 1, "p": "100", "q": "5", "m": False}]

    def klines(self, symbol, interval, limit):
        self.calls.append(("klines", symbol, interval, limit))
        return [] if interval == self.bad_timeframe else _klines()

    def futures_exchange_symbol(self, symbol):
        self.calls.append(("futures_exchange_symbol", symbol))
        if not self.futures_available:
            return None
        return {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL"}

    def futures_depth(self, symbol, limit):
        self.calls.append(("futures_depth", symbol, limit))
        return {"bids": [["100.00", "5"]], "asks": [["100.01", "1"]]}

    def futures_taker(self, symbol):
        self.calls.append(("futures_taker", symbol))
        return {"buySellRatio": "1.2"}

    def futures_open_interest(self, symbol):
        self.calls.append(("futures_open_interest", symbol))
        return {"openInterest": "123"}

    def futures_premium(self, symbol):
        self.calls.append(("futures_premium", symbol))
        return {"markPrice": "100.5"}


def test_collect_requests_all_seven_timeframes_and_exact_same_symbol_usdm():
    assert TIMEFRAMES == ("1m", "5m", "15m", "1h", "2h", "4h", "1d")
    client = FakeFlowClient(futures_available=True)
    snapshot = collect(client, _pair_state())
    assert snapshot["ok"] is True
    assert tuple(snapshot["timeframes"]) == TIMEFRAMES
    assert snapshot["futures"]["available"] is True
    assert snapshot["futures"]["same_symbol"] == "BTCUSDT"
    symbol_calls = [call for call in client.calls if len(call) > 1 and
                    call[0] not in {"klines"}]
    assert all(call[1] == "BTCUSDT" for call in symbol_calls)
    kline_calls = [call for call in client.calls if call[0] == "klines"]
    assert [(call[1], call[2]) for call in kline_calls] == [
        ("BTCUSDT", timeframe) for timeframe in TIMEFRAMES
    ]


def test_collect_marks_missing_exact_usdm_unavailable_without_querying_futures_data():
    client = FakeFlowClient(futures_available=False)
    snapshot = collect(client, _pair_state("BTC/FDUSD"))
    assert snapshot["ok"] is True
    assert snapshot["futures"] == {
        "available": False,
        "same_symbol": "BTCFDUSD",
        "reason": "matching USD-M perpetual is not available",
    }
    assert not any(call[0] in {
        "futures_depth", "futures_taker", "futures_open_interest", "futures_premium"
    } for call in client.calls)


def test_moneyflow_client_filters_exchange_info_by_exact_symbol():
    class Transport:
        def exchange_info(self, symbol):
            return {"symbols": [_spot_metadata(symbol)]}

        def get(self, endpoint, params=None):
            assert endpoint == "/fapi/v1/exchangeInfo"
            return {"symbols": [
                {"symbol": "BTCUSDC", "status": "TRADING"},
                {"symbol": "BTCUSDT", "status": "TRADING"},
            ]}

    transport = Transport()
    client = MoneyFlowClient(spot=transport, futures=transport)
    assert client.futures_exchange_symbol("BTCUSDT")["symbol"] == "BTCUSDT"
    assert client.futures_exchange_symbol("BTCFDUSD") is None


def test_degraded_timeframe_is_fail_closed_and_never_published_bullish():
    snapshot = collect(FakeFlowClient(bad_timeframe="2h"), _pair_state())
    assert snapshot["ok"] is False
    assert snapshot["timeframes"]["2h"]["direction"] == "unavailable"
    assert snapshot["errors"] == ["2h:ValueError"]
    assert snapshot["classification"]["bullish"] is False
    assert snapshot["classification"]["decision"] != "BULLISH"


def _flow_manager(tmp_path, monkeypatch, *, require_futures=False):
    monkeypatch.setenv("REQUIRE_FLOW_CONTEXT", "true")
    monkeypatch.setenv("REQUIRE_MATCHING_FUTURES", "true" if require_futures else "false")
    path = tmp_path / "flow.json"
    manager = OrderManager(None, None, None, None, None, path, {})
    return manager, path


def _valid_flow(state):
    return {
        "pair": state["pair"],
        "pair_state_hash": state["state_hash"],
        "generated_at_epoch": time.time(),
        "ok": True,
        "classification": {"bullish": True},
        "futures": {"available": True},
    }


@pytest.mark.parametrize(
    ("timestamp", "reason_fragment"),
    [
        ("not-a-number", "malformed"),
        (float("nan"), "malformed"),
        (float("inf"), "malformed"),
        (lambda: time.time() - 46, "stale"),
        (lambda: time.time() + 31, "stale"),
    ],
)
def test_flow_gate_rejects_malformed_nonfinite_stale_and_future_timestamps(
    tmp_path, monkeypatch, timestamp, reason_fragment
):
    state = _pair_state()
    manager, path = _flow_manager(tmp_path, monkeypatch)
    flow = _valid_flow(state)
    flow["generated_at_epoch"] = timestamp() if callable(timestamp) else timestamp
    path.write_text(json.dumps(flow), encoding="utf-8")
    ok, reason = manager._flow_gate(state)
    assert ok is False
    assert reason_fragment in reason


def test_flow_gate_rejects_wrong_generation_degraded_and_required_missing_futures(
    tmp_path, monkeypatch
):
    state = _pair_state()
    manager, path = _flow_manager(tmp_path, monkeypatch)
    flow = _valid_flow(state)
    flow["pair_state_hash"] = "0" * 64
    path.write_text(json.dumps(flow), encoding="utf-8")
    assert manager._flow_gate(state) == (
        False, "money-flow snapshot belongs to another pair generation"
    )

    flow = _valid_flow(state)
    flow["ok"] = False
    path.write_text(json.dumps(flow), encoding="utf-8")
    assert manager._flow_gate(state) == (False, "money-flow service is degraded")

    manager, path = _flow_manager(tmp_path, monkeypatch, require_futures=True)
    flow = _valid_flow(state)
    flow["futures"]["available"] = False
    path.write_text(json.dumps(flow), encoding="utf-8")
    assert manager._flow_gate(state) == (
        False, "matching USD-M futures context is required but unavailable"
    )


def test_moneyflow_client_has_only_public_get_endpoints_and_no_order_or_credential_api():
    source = inspect.getsource(MoneyFlowClient)
    tree = ast.parse(source)
    methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not methods.intersection({
        "place", "order", "cancel", "cancel_order", "account", "signed_request",
        "withdraw", "transfer",
    })
    endpoints = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.startswith("/")
    }
    assert endpoints == {
        "/api/v3/depth",
        "/api/v3/aggTrades",
        "/api/v3/klines",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/depth",
        "/futures/data/takerlongshortRatio",
        "/fapi/v1/openInterest",
        "/fapi/v1/premiumIndex",
    }
    lowered = source.lower()
    assert "api_key" not in lowered and "api_secret" not in lowered
    assert ".post(" not in lowered and ".delete(" not in lowered and ".put(" not in lowered


def test_telegram_authorization_requires_both_owner_user_and_private_chat(monkeypatch):
    monkeypatch.setattr(authorization, "OWNER", "4242")
    assert authorization.is_owner(4242, "4242") is True
    assert authorization.is_owner("4242", 4242) is True
    assert authorization.is_owner(4242, "9999") is False
    assert authorization.is_owner(9999, "4242") is False
    monkeypatch.setattr(authorization, "OWNER", "")
    assert authorization.is_owner(4242, "4242") is False


def _telegram_capture(monkeypatch):
    sent = []
    sidecar_calls = []
    ft_calls = []
    monkeypatch.setattr(telegram_bot, "CB", CallbackStore(ttl=120))
    monkeypatch.setattr(
        telegram_bot, "is_owner",
        lambda user_id, chat_id=None: str(user_id) == "7" and
        (chat_id is None or str(chat_id) == "7"),
    )
    monkeypatch.setattr(
        telegram_bot, "send",
        lambda text, chat_id=None, buttons=None: sent.append((str(text), str(chat_id), buttons)),
    )

    def sidecar(name, args=None, wait=False):
        sidecar_calls.append((name, args or {}, wait))
        return {"ok": True, "result": "accepted"}

    def ft(method, endpoint):
        ft_calls.append((method, endpoint))
        return {"ok": True}

    monkeypatch.setattr(telegram_bot, "sidecar_command", sidecar)
    monkeypatch.setattr(telegram_bot, "ft_call", ft)
    monkeypatch.setattr(telegram_bot.requests, "post", lambda *args, **kwargs: None)
    return sent, sidecar_calls, ft_calls


def test_telegram_rejects_non_owner_before_any_response_or_side_effect(monkeypatch):
    sent, sidecar_calls, ft_calls = _telegram_capture(monkeypatch)
    events = []
    monkeypatch.setattr(telegram_bot, "audit", lambda event, **kwargs: events.append(event))
    telegram_bot.handle_message({
        "from": {"id": 99}, "chat": {"id": 7}, "text": "/emergency BTCUSDT"
    })
    assert sent == [] and sidecar_calls == [] and ft_calls == []
    assert events == ["telegram_unauthorized"]


@pytest.mark.parametrize(
    "command",
    [
        "/start",
        "/fixed_oco",
        "/oco_trailing",
        "/trailing_only",
        "/switchpair BTC/USDC",
        "/swapdone",
        "/convert BTCUSDT FIXED_OCO",
        "/breakeven BTCUSDT",
        "/lockprofit BTCUSDT 0.2",
        "/tighttrail BTCUSDT 20",
        "/autoprotection on",
        "/emergency BTCUSDT",
        "/setsize 25",
        "/setmax 1",
        "/restartws",
    ],
)
def test_telegram_money_affecting_actions_issue_confirmation_before_dispatch(
    monkeypatch, command
):
    sent, sidecar_calls, ft_calls = _telegram_capture(monkeypatch)
    telegram_bot.handle_message({"from": {"id": 7}, "chat": {"id": 7}, "text": command})
    assert sidecar_calls == [] and ft_calls == []
    assert len(sent) == 1
    buttons = sent[0][2]
    assert buttons and buttons[0][0]["callback_data"].startswith("confirm|")


def test_telegram_pair_switch_waits_for_manual_swap_before_reload(
    monkeypatch
):
    sent, sidecar_calls, ft_calls = _telegram_capture(monkeypatch)
    telegram_bot.handle_message({
        "from": {"id": 7}, "chat": {"id": 7}, "text": "/switchpair BTC/USDC"
    })
    callback_data = sent[-1][2][0][0]["callback_data"]
    callback = {
        "id": "callback-1",
        "from": {"id": 7},
        "message": {"chat": {"id": 7}},
        "data": callback_data,
    }
    telegram_bot.handle_callback(callback)
    assert sidecar_calls == [("switch_pair", {"pair": "BTC/USDC"}, True)]
    assert ft_calls == []
    assert "/swapdone" in sent[-1][0]

    telegram_bot.handle_callback(dict(callback, id="callback-2"))
    assert sidecar_calls == [("switch_pair", {"pair": "BTC/USDC"}, True)]
    assert ft_calls == []
    assert "duplicate callback" in sent[-1][0]

    telegram_bot.handle_message({
        "from": {"id": 7}, "chat": {"id": 7}, "text": "/swapdone"
    })
    swap_callback = {
        "id": "callback-swap",
        "from": {"id": 7},
        "message": {"chat": {"id": 7}},
        "data": sent[-1][2][0][0]["callback_data"],
    }
    telegram_bot.handle_callback(swap_callback)
    assert sidecar_calls == [
        ("switch_pair", {"pair": "BTC/USDC"}, True),
        ("complete_pair_switch", {}, True),
        ("verify_pair_switch", {}, True),
    ]
    assert ft_calls == [("POST", "/reload_config")]
    assert "remain OFF until explicit resume" in sent[-1][0]


def test_telegram_cancel_invalidates_the_exact_confirmation_token(monkeypatch):
    sent, sidecar_calls, ft_calls = _telegram_capture(monkeypatch)
    telegram_bot.handle_message({
        "from": {"id": 7}, "chat": {"id": 7}, "text": "/emergency BTCUSDT"
    })
    confirm_data = sent[-1][2][0][0]["callback_data"]
    cancel_data = sent[-1][2][1][0]["callback_data"]
    base = {"from": {"id": 7}, "message": {"chat": {"id": 7}}}
    telegram_bot.handle_callback(dict(base, id="cancel-1", data=cancel_data))
    assert "canceled" in sent[-1][0].lower()
    telegram_bot.handle_callback(dict(base, id="confirm-after-cancel", data=confirm_data))
    assert sidecar_calls == [] and ft_calls == []
    assert "duplicate callback" in sent[-1][0]


def test_rejected_user_stream_subscription_closes_socket_for_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    stream = ModernUserDataStream(SimpleNamespace(), testnet=True)
    stream._request_id = "subscription-1"
    stream._connected = True

    class Socket:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    socket = Socket()
    stream._on_message(socket, json.dumps({"id": "subscription-1", "status": 400}))
    assert socket.closed == 1
    assert stream._subscribed is False
    assert stream._last_error == "subscription_rejected"


@pytest.mark.parametrize(
    "event_type",
    ["outboundAccountPosition", "balanceUpdate", "externalLockUpdate"],
)
def test_account_events_trigger_authoritative_reconciliation(
    tmp_path, monkeypatch, event_type
):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    resyncs = []
    stream = ModernUserDataStream(
        SimpleNamespace(), on_resync=lambda: resyncs.append("gap"), testnet=True
    )
    stream._dispatch_event({"e": event_type, "B": []})
    assert resyncs == ["gap"]


def test_telegram_menu_is_btc_control_only_and_has_no_altcoin_scanner_actions():
    buttons = [button for row in telegram_bot.menu() for button in row]
    callbacks = {button["callback_data"] for button in buttons}
    assert callbacks == {
        "do|entries_on", "do|entries_off", "do|status", "do|balance",
        "do|profit", "do|last_signal", "do|pair", "do|pairs", "do|flow",
        "do|swap_done", "do|verify_pair",
        "do|mode_fixed", "do|mode_oco_trailing", "do|mode_trailing",
        "do|trailing_help", "do|be_help", "do|profit_help", "do|reconcile",
        "do|restart_stream", "do|logs", "do|deploy", "do|backtest",
        "do|audit", "do|settings", "do|emergency_help", "do|help",
    }
    rendered = json.dumps(telegram_bot.menu()).lower() + " " + telegram_bot.help_text().lower()
    for forbidden in ("scanner", "scan50", "top 50", "altcoin", "sharia"):
        assert forbidden not in rendered


def test_telegram_self_audit_is_read_only_and_owner_accessible(tmp_path, monkeypatch):
    sent, sidecar_calls, ft_calls = _telegram_capture(monkeypatch)
    now = time.time()
    runtime = tmp_path / "runtime" / "telegram"
    sidecar_runtime = tmp_path / "runtime" / "sidecar"
    deployment_file = tmp_path / "runtime" / "deployment_status.json"
    moneyflow_runtime = tmp_path / "runtime" / "moneyflow"
    pair_file = tmp_path / "pair" / "active_pair.json"
    for folder in (runtime, sidecar_runtime, moneyflow_runtime, pair_file.parent):
        folder.mkdir(parents=True, exist_ok=True)
    release_hash = "a" * 64
    deployment_file.write_text(json.dumps({
        "ok": True, "status": "DEPLOYED", "release_hash": release_hash,
        "release_path": "/opt/bitcoin-bot/releases/20260730T000000Z",
        "execution_mode": "simulation",
    }), encoding="utf-8")
    (deployment_file.parent / "release_validation.json").write_text(json.dumps({
        "ok": True, "outcome": "DEPLOYED", "release_hash": release_hash,
        "release_path": "/opt/bitcoin-bot/releases/20260730T000000Z",
        "execution_mode": "simulation",
        "container_health_gate": "passed", "monitoring_health_gate": "passed",
    }), encoding="utf-8")
    (sidecar_runtime / "sidecar_health.json").write_text(json.dumps({
        "ok": True, "ts": now, "execution_mode": "simulation",
        "simulation": True, "entries_enabled": False, "unresolved_intents": 0,
        "active_pair": "BTC/USDT", "pair_switch_stage": "IDLE",
    }), encoding="utf-8")
    (moneyflow_runtime / "moneyflow_health.json").write_text(json.dumps({
        "ok": True, "ts": now, "pair": "BTC/USDT",
    }), encoding="utf-8")
    (runtime / "telegram_health.json").write_text(json.dumps({
        "ok": True, "ts": now,
    }), encoding="utf-8")
    pair_file.write_text(json.dumps({"pair": "BTC/USDT"}), encoding="utf-8")
    monkeypatch.setattr(telegram_bot, "RUNTIME", runtime)
    monkeypatch.setattr(telegram_bot, "SIDECAR_RUNTIME", sidecar_runtime)
    monkeypatch.setattr(telegram_bot, "DEPLOYMENT_STATUS_FILE", deployment_file)
    monkeypatch.setattr(telegram_bot, "ACTIVE_PAIR_FILE", pair_file)
    monkeypatch.setenv("EXECUTION_MODE", "simulation")
    monkeypatch.setenv("DEPLOYED_RELEASE_HASH", release_hash)
    telegram_bot.handle_message({
        "from": {"id": 7}, "chat": {"id": 7}, "text": "/audit",
    })
    result = json.loads(sent[-1][0])
    assert result["ok"] is True and result["read_only"] is True
    assert result["issues"] == []
    assert sidecar_calls == []
    assert ft_calls == [("GET", "/ping")]

    monkeypatch.delenv("ENVELOPE_RELEASE_HASH")
    assert telegram_bot._self_audit(now=now)["ok"] is False
    assert "release_identity" in telegram_bot._self_audit(now=now)["issues"]


def test_telegram_self_audit_fails_closed_on_stale_or_armed_state(tmp_path, monkeypatch):
    now = time.time()
    runtime = tmp_path / "runtime" / "telegram"
    sidecar_runtime = tmp_path / "runtime" / "sidecar"
    deployment_file = tmp_path / "runtime" / "deployment_status.json"
    moneyflow_runtime = tmp_path / "runtime" / "moneyflow"
    pair_file = tmp_path / "pair" / "active_pair.json"
    for folder in (runtime, sidecar_runtime, moneyflow_runtime, pair_file.parent):
        folder.mkdir(parents=True, exist_ok=True)
    deployment_file.write_text("{}", encoding="utf-8")
    (deployment_file.parent / "release_validation.json").write_text("{}", encoding="utf-8")
    (sidecar_runtime / "sidecar_health.json").write_text(json.dumps({
        "ok": True, "ts": now - 600, "execution_mode": "simulation",
        "simulation": True, "entries_enabled": True, "unresolved_intents": 2,
        "active_pair": "ETH/USDT",
    }), encoding="utf-8")
    (moneyflow_runtime / "moneyflow_health.json").write_text(json.dumps({
        "ok": False, "ts": now - 600,
    }), encoding="utf-8")
    (runtime / "telegram_health.json").write_text(json.dumps({
        "ok": False, "ts": now - 600,
    }), encoding="utf-8")
    pair_file.write_text(json.dumps({"pair": "BTC/USDT"}), encoding="utf-8")
    monkeypatch.setattr(telegram_bot, "RUNTIME", runtime)
    monkeypatch.setattr(telegram_bot, "SIDECAR_RUNTIME", sidecar_runtime)
    monkeypatch.setattr(telegram_bot, "DEPLOYMENT_STATUS_FILE", deployment_file)
    monkeypatch.setattr(telegram_bot, "ACTIVE_PAIR_FILE", pair_file)
    monkeypatch.setattr(telegram_bot, "ft_call", lambda *_: {"ok": False})
    monkeypatch.setenv("EXECUTION_MODE", "simulation")
    monkeypatch.setenv("DEPLOYED_RELEASE_HASH", "b" * 64)
    result = telegram_bot._self_audit(now=now)
    assert result["ok"] is False
    for expected in (
        "deployment_active", "release_validation", "release_identity",
        "release_path_identity", "deployment_mode", "release_validation_mode",
        "entries_off", "no_unresolved_intents", "btc_pair_consistency",
        "moneyflow_pair_consistency", "pair_switch_idle",
        "freqtrade_ping", "sidecar_fresh", "moneyflow_healthy",
        "moneyflow_fresh", "telegram_healthy", "telegram_fresh",
    ):
        assert expected in result["issues"]


def test_oracle_workflow_uses_only_root_approved_self_hosted_simulation():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    wrapper = (ROOT / "deploy/bitcoin-bot-deploy").read_text(encoding="utf-8")
    setup = (ROOT / "deploy/oracle_setup.sh").read_text(encoding="utf-8")
    assert "runs-on: [self-hosted, linux, oracle-sim]" in workflow
    assert "inputs.execution_mode == 'simulation'" in workflow
    assert "inputs.confirmation == 'SIMULATION_ONLY'" in workflow
    assert "vars.ORACLE_DEPLOY_ENABLED == 'true'" in workflow
    assert "sudo /usr/local/sbin/bitcoin-bot-deploy preflight" in workflow
    assert "sudo /usr/local/sbin/bitcoin-bot-deploy simulation" in workflow
    assert "sudo /usr/local/sbin/bitcoin-bot-deploy verify" in workflow
    for forbidden in ("ORACLE_SSH_PRIVATE_KEY", "ORACLE_HOST", "scp -P", "ssh -p"):
        assert forbidden not in workflow
    assert "options: [simulation]" in workflow
    assert "preflight|simulation|verify" in wrapper
    assert "only preflight, simulation, or verify is permitted" in wrapper
    assert 'require_canonical_file "$ACTION_LOCK" 0 0 600' in wrapper
    assert 'env -i "PATH=$PATH" "HOME=/root" "EXPECTED_EXECUTION_MODE=simulation"' in wrapper
    assert "PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT" in wrapper
    assert "root-approved SHA-256" in wrapper
    assert "requires empty Binance credentials" in wrapper
    assert "approved-artifact.sha256" in setup
    assert "gha-runner ALL=(root) NOPASSWD: /usr/local/sbin/bitcoin-bot-deploy preflight" in setup
    assert 'usermod -aG docker "$ACTIONS_RUNNER_USER"' not in setup


def _load_strategy_entry_callback():
    """Execute the exact callback body without requiring Freqtrade on the audit host."""
    tree = ast.parse(STRATEGY_PATH.read_text(encoding="utf-8"))
    strategy = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "IctSmcStrategy")
    callback = next(node for node in strategy.body
                    if isinstance(node, ast.FunctionDef) and node.name == "confirm_trade_entry")
    isolated = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
    namespace = {}
    exec(compile(isolated, str(STRATEGY_PATH), "exec"), namespace)
    return namespace["confirm_trade_entry"]


@pytest.mark.parametrize("mode", ["backtest", "hyperopt"])
def test_freqtrade_entry_callback_allows_only_offline_optimization(mode):
    callback = _load_strategy_entry_callback()
    dummy = SimpleNamespace(config={"runmode": SimpleNamespace(value=mode)})
    assert callback(
        dummy, "BTC/USDT", "limit", 1.0, 100.0, "GTC", None, None, "long"
    ) is True


@pytest.mark.parametrize("mode", ["live", "dry_run", "dry-run", "webserver", ""])
def test_freqtrade_entry_callback_denies_every_runtime_order_mode(mode):
    callback = _load_strategy_entry_callback()
    dummy = SimpleNamespace(config={"runmode": SimpleNamespace(value=mode)})
    assert callback(
        dummy, "BTC/USDT", "limit", 1.0, 100.0, "GTC", None, None, "long"
    ) is False


def _compact_expression(node):
    return ast.unparse(node).replace(" ", "").replace('"', "'")


def test_freqtrade_signal_formula_has_the_exact_preserved_1m_5m_structure():
    source = STRATEGY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    strategy = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "IctSmcStrategy")
    entry = next(node for node in strategy.body
                 if isinstance(node, ast.FunctionDef) and node.name == "populate_entry_trend")
    assignment = next(
        node for node in ast.walk(entry)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "conditions" for target in node.targets)
    )
    assert isinstance(assignment.value, ast.List)
    actual = [_compact_expression(item) for item in assignment.value.elts]
    assert actual == [
        "dataframe['ema9_5m']>dataframe['ema21_5m']",
        "dataframe['ema21_5m']>dataframe['ema50_5m']",
        "dataframe['close']>dataframe['ema50_5m']",
        "dataframe['macdhist_5m']>0",
        "dataframe['close']>dataframe['vwap']",
        "dataframe['pullback']",
        "dataframe['close']>dataframe['ema9']",
        "dataframe['ema9_rising']",
        "dataframe['rsi']>self.RSI_MIN",
        "dataframe['rsi_rising']",
        "dataframe['rvol']>=self.RVOL_MIN",
        "dataframe['adx']>20",
        "dataframe['volume']>0",
    ]
    assert "dataframe['macdhist']>0" not in actual

    assert 'timeframe = "1m"' in source
    assert '@informative("5m")' in source
    for token in (
        'timeperiod=200', 'fastperiod=12, slowperiod=26, signalperiod=9',
        'fastperiod=5, slowperiod=13, signalperiod=6',
        'rolling_vwap(dataframe, window=200)', 'dataframe["adx"] = ta.ADX(dataframe)',
    ):
        assert token in source


def test_compose_has_four_services_and_only_sidecar_receives_binance_credentials():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {
        "moneyflow", "freqtrade", "execution-sidecar", "telegram-broker"
    }
    credential_holders = {
        name for name, service in services.items()
        if {"BINANCE_API_KEY", "BINANCE_API_SECRET"}.intersection(
            (service.get("environment") or {}).keys()
        )
    }
    assert credential_holders == {"execution-sidecar"}
    assert set(services["execution-sidecar"]["environment"]).issuperset({
        "BINANCE_API_KEY", "BINANCE_API_SECRET"
    })
    assert services["moneyflow"]["command"] == "python -m services.moneyflow.service"
    assert services["freqtrade"]["image"] == (
        "freqtradeorg/freqtrade:2026.6@sha256:"
        "d451af021d5e08b70580c0eea5848534e9846b57391b34821c0a5814416397e6"
    )
    assert "--strategy IctSmcStrategy" in services["freqtrade"]["command"]


def test_environment_template_has_no_duplicate_keys():
    names = []
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            names.append(line.split("=", 1)[0].strip())
    assert len(names) == len(set(names))


def test_backtest_gate_embeds_money_relevant_cli_policy_in_official_zip_config():
    script = (ROOT / "freqtrade/scripts/backtest.sh").read_text(encoding="utf-8")
    for required in (
        "'strategy': 'IctSmcStrategy'",
        "'timeframe': '1m'",
        "'trading_mode': 'spot'",
        "'max_open_trades': 1",
        "'enable_protections': True",
        "'fee': 0.001",
        "'pairlists': [{'method': 'StaticPairList'}]",
        "--config /freqtrade/shared/pair/backtest-config.json",
    ):
        assert required in script


def test_strategy_signal_loop_treats_nan_entry_flags_as_no_signal():
    strategy = (
        ROOT / "freqtrade/user_data/strategies/IctSmcStrategy.py"
    ).read_text(encoding="utf-8")
    assert "int(row.get('enter_long'" not in strategy
    assert "if row.get('enter_long', 0) == 1:" in strategy
    assert 'if self._runmode_value() in {"backtest", "hyperopt", "edge"}:' in strategy
    assert 'return mode_value in {"backtest", "hyperopt"}' in strategy


@pytest.mark.parametrize("name", ["backtest.sh", "lookahead.sh", "recursive.sh"])
def test_offline_analysis_helpers_generate_complete_active_pair_state(name):
    script = (ROOT / "freqtrade/scripts" / name).read_text(encoding="utf-8")
    assert "pair_config_hash," in script
    assert (
        "'pair_config_hash': pair_config_hash(pair)" in script
        or '"pair_config_hash": pair_config_hash(pair)' in script
    )
    assert (
        'state["state_hash"] = pair_state_hash(state)' in script
        or "state['state_hash'] = pair_state_hash(state)" in script
    )


@pytest.mark.parametrize(
    ("name", "command", "required_option"),
    [
        ("lookahead.sh", "lookahead-analysis", "--minimum-trade-amount 20"),
        ("recursive.sh", "recursive-analysis", "--startup-candle 199 499 999 1999"),
    ],
)
def test_offline_analysis_helpers_are_single_pair_no_auth_and_artifact_only(
    name, command, required_option
):
    script = (ROOT / "freqtrade/scripts" / name).read_text(encoding="utf-8")
    for required in (
        "canonical_pair(sys.argv[1])",
        '"pair_whitelist": [pair]',
        '"pairlists": [{"method": "StaticPairList"}]',
        '"dry_run": True',
        '"key": ""',
        '"secret": ""',
        '"telegram": {"enabled": False}',
        '"api_server": {"enabled": False}',
        "FREQTRADE__EXCHANGE__KEY=",
        "FREQTRADE__EXCHANGE__SECRET=",
        "--no-deps --cap-drop ALL",
        "user_data/backtest_results",
        f"freqtrade {command}",
        '--pairs "$PAIR"',
        required_option,
    ):
        assert required in script
    executable = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "freqtrade trade" not in executable
    assert "LIVE_TRADING_ENABLED" not in script
    assert "BINANCE_API_KEY" not in script


def test_lookahead_helper_keeps_official_market_order_safety_override():
    script = (ROOT / "freqtrade/scripts/lookahead.sh").read_text(encoding="utf-8")
    executable_lines = [
        line for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any("--allow-limit-orders" in line for line in executable_lines)
    assert "--lookahead-analysis-exportfilename" in script


def test_installer_requires_preexisting_two_hash_rollback_proof_and_exact_identity():
    script = (ROOT / "deploy/install_artifact.sh").read_text(encoding="utf-8")
    assert "current release has no safe, single-line success marker" in script
    assert "current success marker does not match its release and config hashes" in script
    assert "verify_stack_identity.py" in script
    assert 'parent=$(dirname "$resolved")' in script
    assert '[[ "$parent" == "$RELEASES"' in script
    assert "Never bless a symlink here" in script
    old_audit = script.index('python3 "$OLD/scripts/verify_manifest.py"')
    new_mark = script.index('mark_release_success "$DEST" "$RELEASE_HASH"')
    assert old_audit < new_mark
    assert 'mark_release_success "$OLD"' not in script


def test_installer_preserves_residual_projects_and_never_auto_deletes_audit_evidence():
    script = (ROOT / "deploy/install_artifact.sh").read_text(encoding="utf-8")
    assert "ROLLBACK_BLOCKED_RESIDUAL_NEW_PROJECT_CRITICAL" in script
    assert "PRESERVE_FAILED_RELEASE=true" in script
    assert "docker ps -aq" in script
    assert "rm -rf --one-file-system" in script
    assert "never removed automatically" in script
    assert "rglob('*')" not in script


def test_installer_live_rollback_has_fixed_validity_margins():
    script = (ROOT / "deploy/install_artifact.sh").read_text(encoding="utf-8")
    assert "LIVE_PREFLIGHT_MARGIN_SECONDS=3600" in script
    assert "LIVE_ACTIVATION_MARGIN_SECONDS=300" in script
    assert "LIVE_EVIDENCE_MIN_REMAINING_SECONDS" in script


class _SwitchAdapter:
    mode = "live"

    def __init__(self):
        self.enabled = True
        self.validated = []
        self.flat_symbols = []

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        return "ON" if self.enabled else "OFF"

    def validate_pair(self, pair):
        self.validated.append(pair)

    def validate_pair_funding(self, pair):
        self.validated.append("funding:" + pair)
        return {"pair": pair, "funded": True}

    def verify_flat_for_switch(self, symbols):
        self.flat_symbols.append(set(symbols))
        return {"ok": True, "detail": "verified flat"}


class _SwitchGuard:
    def __init__(self):
        self.pauses = []

    def set_global_pause(self, reason):
        self.pauses.append(reason)


def test_live_pair_switch_sets_durable_restart_latch_and_keeps_entries_off(
    tmp_path, monkeypatch
):
    pair_root = tmp_path / "pair"
    controller = PairController(
        pair_root / "active.json", pair_root / "pairlist.json", pair_root / "overlay.json",
        allowed_quotes=("USDT", "USDC", "FDUSD"),
    )
    controller.registry = SimpleNamespace(
        require_pair=lambda pair: {
            "pair": pair,
            "registry_hash": "a" * 64,
            "capabilities": {"spot": True},
        }
    )
    controller.bootstrap("BTC/USDT")
    state = StateStore(tmp_path / "sidecar.json", tmp_path / "sidecar.sqlite")
    state.set_entries(True)
    adapter = _SwitchAdapter()
    guard = _SwitchGuard()
    command_results = tmp_path / "command-results"
    monkeypatch.setattr(sidecar_main, "COMMAND_RESULTS_DIR", command_results)

    payload = {
        "command_id": "switch-1",
        "command": "switch_pair",
        "args": {"pair": "BTC/USDC"},
        "created_at": time.time(),
    }
    signed = envelope.sign_envelope(
        producer="telegram-broker",
        purpose=envelope.BUS_COMMAND,
        payload=payload,
        ttl_seconds=120,
    )
    command_path = tmp_path / "switch-1.json"
    command_path.write_text(json.dumps(signed), encoding="utf-8")
    sidecar_main.process_command(adapter, state, guard, controller, command_path)

    assert command_path.exists() is False
    assert controller.load()["pair"] == "BTC/USDT"
    assert controller.switch_status()["stage"] == "WAITING_MANUAL_SWAP"
    assert adapter.validated == ["BTC/USDC"]
    assert adapter.flat_symbols == [{"BTCUSDT", "BTCUSDC"}]
    assert adapter.enabled is False and state.entries() is False
    assert state.data.get("live_restart_required") is not True
    assert guard.pauses == []
    result = json.loads((command_results / "command_result_switch-1.json").read_text())
    assert result["ok"] is True

    payload = {
        "command_id": "switch-2",
        "command": "complete_pair_switch",
        "args": {},
        "created_at": time.time(),
    }
    signed = envelope.sign_envelope(
        producer="telegram-broker",
        purpose=envelope.BUS_COMMAND,
        payload=payload,
        ttl_seconds=120,
    )
    command_path = tmp_path / "switch-2.json"
    command_path.write_text(json.dumps(signed), encoding="utf-8")
    sidecar_main.process_command(adapter, state, guard, controller, command_path)

    assert controller.load()["pair"] == "BTC/USDC"
    assert controller.switch_status()["stage"] == "VERIFYING_PAIR"
    assert adapter.validated == ["BTC/USDC", "funding:BTC/USDC"]
    assert adapter.flat_symbols == [
        {"BTCUSDT", "BTCUSDC"},
        {"BTCUSDT", "BTCUSDC"},
    ]
    assert state.data["live_restart_required"] is True
    assert guard.pauses == ["live-pair-change-requires-fresh-evidence-and-restart"]
    reloaded = StateStore(tmp_path / "sidecar.json", tmp_path / "sidecar.sqlite")
    assert reloaded.data["live_restart_required"] is True
    assert reloaded.entries() is False


def test_live_adapter_restart_latch_blocks_rearming_before_reconciliation():
    adapter = object.__new__(BitcoinSpotAdapter)
    adapter.mode = "live"
    adapter.enabled = False
    adapter.state_store = SimpleNamespace(
        data={"live_restart_required": True},
        unresolved_intents=lambda: pytest.fail("latch must short-circuit before reconciliation"),
    )
    adapter.verified_reconcile = lambda: pytest.fail(
        "latch must short-circuit before exchange reconciliation"
    )
    assert adapter.set_enabled(True) == (
        "OFF: live pair/policy changed; fresh signed evidence and restart required"
    )
    assert adapter.enabled is False


def test_telegram_update_store_migrates_offset_and_rejects_replay(tmp_path):
    legacy = tmp_path / "telegram_offset.json"
    legacy.write_text(json.dumps({"offset": 42}), encoding="utf-8")
    store = telegram_bot.TelegramUpdateStore(
        tmp_path / "telegram_updates.sqlite3",
        legacy_offset_path=legacy,
    )
    try:
        assert store.offset() == 42
        assert store.claim(41) is False
        assert store.claim(42) is True
        store.complete(42, "handled")
        assert store.offset() == 43
        assert store.claim(42) is False
    finally:
        store.close()


def test_telegram_update_claim_survives_crash_without_reexecution(tmp_path):
    database = tmp_path / "telegram_updates.sqlite3"
    first = telegram_bot.TelegramUpdateStore(database)
    assert first.claim(1001) is True
    assert first.offset() == 1002
    first.close()

    recovered = telegram_bot.TelegramUpdateStore(database)
    try:
        assert recovered.recovered_uncertain == 1
        assert recovered.uncertain_count() == 1
        assert recovered.offset() == 1002
        assert recovered.claim(1001) is False
    finally:
        recovered.close()


def test_telegram_update_store_fails_closed_on_malformed_legacy_offset(tmp_path):
    legacy = tmp_path / "telegram_offset.json"
    legacy.write_text('{"offset": "not-an-integer"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="legacy Telegram offset is malformed"):
        telegram_bot.TelegramUpdateStore(
            tmp_path / "telegram_updates.sqlite3",
            legacy_offset_path=legacy,
        )


def test_telegram_poll_backoff_is_bounded_exponential_with_jitter_hook():
    no_jitter = lambda _low, _high: 0.0
    delays = [
        telegram_bot._poll_backoff(failures, jitter=no_jitter)
        for failures in range(1, 9)
    ]
    assert delays == [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 60.0, 60.0]
    assert telegram_bot._poll_backoff(1, jitter=lambda _low, high: high) == 3.75
