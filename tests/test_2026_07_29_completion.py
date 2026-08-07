from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from services.common.binance_public import BinancePublicClient
from services.common.market_policy import (
    PairPolicyError,
    canonical_pair,
    pair_config_hash,
)
from services.common.pair_registry import PairRegistry, PairRegistryError
from services.common.models import ProtectionMode
from services.execution_sidecar.bitcoin_adapter import BitcoinSpotAdapter, SymbolRules
from services.execution_sidecar import main as sidecar_main
from services.execution_sidecar.filters import (
    FilterDataUnavailable,
    FilterViolation,
    SpotFilterValidator,
)
from services.execution_sidecar.package_mode import enforce_package_mode
from services.execution_sidecar.pair_control import PairController, PairStateError
from services.telegram_broker import bot as telegram_bot
from services.telegram_broker.callbacks import CallbackStore


def _symbol(pair: str, **overrides) -> dict:
    base, quote = pair.split("/")
    row = {
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
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
            {"filterType": "TRAILING_DELTA"},
        ],
    }
    row.update(overrides)
    return row


class _RegistryPublic:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def exchange_info(self, symbol=None):
        assert symbol is None
        self.calls += 1
        return {"symbols": self.rows}


def test_registry_lists_only_explicitly_spot_allowed_trading_btc_base_pairs(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("BTC_QUOTE_ALLOWLIST", raising=False)
    monkeypatch.delenv("ALLOWED_STABLE_QUOTES", raising=False)
    public = _RegistryPublic([
        _symbol("BTC/USDT"),
        _symbol("BTC/EUR"),
        _symbol("BTC/USD1"),
        _symbol("BTC/USDC", isSpotTradingAllowed=None),
        _symbol("BTC/TRY", status="BREAK"),
        _symbol("ETH/USDT"),
    ])
    registry = PairRegistry(tmp_path / "eligible.json", public_client=public)
    snapshot = registry.refresh(force=True, now=1_800_000_000)
    assert [row["pair"] for row in snapshot["pairs"]] == [
        "BTC/EUR", "BTC/USD1", "BTC/USDT"
    ]
    assert all(row["base"] == "BTC" for row in snapshot["pairs"])
    assert snapshot["registry_hash"]
    assert json.loads((tmp_path / "eligible.json").read_text())["registry_hash"] == (
        snapshot["registry_hash"]
    )


def test_registry_refreshes_before_accepting_owner_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("BTC_QUOTE_ALLOWLIST", raising=False)
    monkeypatch.delenv("ALLOWED_STABLE_QUOTES", raising=False)
    public = _RegistryPublic([_symbol("BTC/USDT"), _symbol("BTC/FDUSD")])
    registry = PairRegistry(tmp_path / "eligible.json", public_client=public)
    assert registry.require_pair("BTC/FDUSD")["symbol"] == "BTCFDUSD"
    public.rows = [_symbol("BTC/USDT")]
    with pytest.raises(PairRegistryError, match="not currently eligible"):
        registry.require_pair("BTC/FDUSD")
    assert public.calls == 2


def test_pair_switch_is_staged_and_never_changes_pair_before_swap_acknowledgement(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("BTC_QUOTE_ALLOWLIST", raising=False)
    monkeypatch.delenv("ALLOWED_STABLE_QUOTES", raising=False)
    registry = PairRegistry(
        tmp_path / "eligible.json",
        public_client=_RegistryPublic([
            _symbol("BTC/USDT"),
            _symbol("BTC/USDC"),
        ]),
    )
    controller = PairController(
        tmp_path / "active.json",
        tmp_path / "pairlist.json",
        tmp_path / "overlay.json",
        allowed_quotes=(),
        registry=registry,
    )
    before = controller.bootstrap("BTC/USDT")
    registry_hash = registry.refresh(force=True)["registry_hash"]
    pending = controller.begin_switch(
        "BTC/USDC",
        lambda symbols: {"ok": True, "symbols": sorted(symbols)},
        expected_registry_hash=registry_hash,
    )
    assert pending["switch"]["stage"] == "WAITING_MANUAL_SWAP"
    assert controller.load() == before
    with pytest.raises(PairStateError, match="WAITING_MANUAL_SWAP"):
        controller.require_resume_ready()

    applied = controller.complete_switch(
        lambda symbols: {"ok": True, "symbols": sorted(symbols)},
        lambda pair: {"pair": pair, "funding_preflight": True},
    )
    assert applied["state"]["pair"] == "BTC/USDC"
    assert applied["switch"]["stage"] == "VERIFYING_PAIR"
    with pytest.raises(PairStateError, match="VERIFYING_PAIR"):
        controller.require_resume_ready()

    ready = controller.mark_pair_verified({
        "ok": True,
        "pair": "BTC/USDC",
        "pair_generation": applied["state"]["generation"],
        "pair_config_hash": applied["state"]["pair_config_hash"],
    })
    assert ready["stage"] == "PAUSED_READY"
    assert controller.require_resume_ready()["stage"] == "PAUSED_READY"
    assert controller.mark_active_after_resume()["stage"] == "ACTIVE"


def test_registry_reports_complete_entry_protection_capabilities(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("BTC_QUOTE_ALLOWLIST", raising=False)
    monkeypatch.delenv("ALLOWED_STABLE_QUOTES", raising=False)
    public = _RegistryPublic([
        _symbol("BTC/USDT"),
        _symbol("BTC/EUR", ocoAllowed=False),
        _symbol("BTC/GBP", otoAllowed=False),
        _symbol("BTC/AUD", allowTrailingStop=False),
    ])
    rows = {
        row["pair"]: row["capabilities"]
        for row in PairRegistry(
            tmp_path / "eligible.json", public_client=public
        ).refresh(force=True, now=1_800_000_000)["pairs"]
    }
    assert rows["BTC/USDT"]["fixed_oco"] is True
    assert rows["BTC/USDT"]["oco_trailing"] is True
    assert rows["BTC/USDT"]["trailing_only"] is True
    assert rows["BTC/EUR"]["fixed_oco"] is False
    assert rows["BTC/EUR"]["trailing_only"] is True
    assert rows["BTC/GBP"]["fixed_oco"] is False
    assert rows["BTC/GBP"]["trailing_only"] is False
    assert rows["BTC/AUD"]["fixed_oco"] is True
    assert rows["BTC/AUD"]["oco_trailing"] is False
    assert rows["BTC/AUD"]["trailing_only"] is False


def test_optional_quote_allowlist_is_a_cap_not_a_default(monkeypatch):
    monkeypatch.delenv("BTC_QUOTE_ALLOWLIST", raising=False)
    monkeypatch.delenv("ALLOWED_STABLE_QUOTES", raising=False)
    assert canonical_pair("btceur") == "BTC/EUR"
    assert canonical_pair("BTC/USD1") == "BTC/USD1"
    monkeypatch.setenv("BTC_QUOTE_ALLOWLIST", "USDT,USDC")
    with pytest.raises(PairPolicyError, match="allowlist"):
        canonical_pair("BTC/EUR")


class _ReferencePublic:
    NO_REFERENCE_PRICE = BinancePublicClient.NO_REFERENCE_PRICE

    def __init__(self, reference, *, error=None):
        self.reference = reference
        self.error = error

    def reference_price(self, _symbol):
        if self.error:
            raise self.error
        return self.reference

    def get(self, path, _params):
        assert path == "/api/v3/avgPrice"
        return {"price": "50"}

    def ticker_price(self, _symbol):
        return {"price": "40"}


def test_filter_uses_current_reference_price_and_only_documented_fallback():
    filters = {"PERCENT_PRICE": {"avgPriceMins": 5}}
    current = SpotFilterValidator(_ReferencePublic("100"))
    assert current._reference_price("BTCUSDT", filters) == Decimal("100")
    fallback = SpotFilterValidator(
        _ReferencePublic(BinancePublicClient.NO_REFERENCE_PRICE)
    )
    assert fallback._reference_price("BTCUSDT", filters) == Decimal("50")
    broken = SpotFilterValidator(_ReferencePublic(None, error=TimeoutError("x")))
    with pytest.raises(FilterDataUnavailable):
        broken._reference_price("BTCUSDT", filters)


def test_mode_capabilities_are_checked_for_the_exact_entry_or_replacement():
    rules = SymbolRules(
        symbol="BTCEUR",
        base="BTC",
        quote="EUR",
        tick=Decimal("0.01"),
        step=Decimal("0.00001"),
        min_qty=Decimal("0"),
        max_qty=Decimal("0"),
        min_notional=Decimal("0"),
        max_notional=Decimal("0"),
        trail_min=10,
        trail_max=2000,
        oco_allowed=False,
        oto_allowed=True,
        trailing_allowed=True,
    )
    BitcoinSpotAdapter._require_mode_capabilities(
        rules, ProtectionMode.TRAILING_ONLY, for_entry=True
    )
    with pytest.raises(FilterViolation, match="OCO"):
        BitcoinSpotAdapter._require_mode_capabilities(
            rules, ProtectionMode.FIXED_OCO, for_entry=True
        )
    rules_without_oto = SymbolRules(
        **{**rules.__dict__, "oto_allowed": False}
    )
    BitcoinSpotAdapter._require_mode_capabilities(
        rules_without_oto, ProtectionMode.TRAILING_ONLY, for_entry=False
    )
    with pytest.raises(FilterViolation, match="OTO"):
        BitcoinSpotAdapter._require_mode_capabilities(
            rules_without_oto, ProtectionMode.TRAILING_ONLY, for_entry=True
        )


class _CapabilityPublic:
    NO_REFERENCE_PRICE = BinancePublicClient.NO_REFERENCE_PRICE

    def __init__(self, row):
        self.row = row

    def exchange_info(self, _symbol=None):
        return {"symbols": [self.row]}


def test_filter_preflight_rejects_unsupported_order_list_endpoint():
    public = _CapabilityPublic(_symbol("BTC/USDT", otoAllowed=False))
    validator = SpotFilterValidator(public)
    params = {
        "workingQuantity": "0.001",
        "workingPrice": "100",
        "pendingQuantity": "0.001",
        "pendingPrice": "90",
    }
    with pytest.raises(FilterViolation, match="OTO"):
        validator.validate_replacement("BTCUSDT", "orderList/oto", params)


def test_package_mode_cannot_be_overridden_by_execution_environment(tmp_path):
    testnet = tmp_path / "RELEASE_MODE"
    testnet.write_text("testnet\n", encoding="utf-8")
    assert enforce_package_mode("simulation", testnet) == "testnet"
    assert enforce_package_mode("testnet", testnet) == "testnet"
    with pytest.raises(SystemExit, match="LIVE BLOCKED"):
        enforce_package_mode("live", testnet)
    live = tmp_path / "LIVE_MODE"
    live.write_text("live\n", encoding="utf-8")
    assert enforce_package_mode("live", live) == "live"
    with pytest.raises(SystemExit, match="PACKAGE MODE BLOCKED"):
        enforce_package_mode("testnet", live)


def test_freqtrade_heartbeat_must_match_pair_generation_and_config_hash(
    tmp_path, monkeypatch
):
    controller = PairController(
        tmp_path / "active.json",
        tmp_path / "pairlist.json",
        tmp_path / "overlay.json",
        allowed_quotes=(),
    )
    active = controller.bootstrap("BTC/USDT")
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "whitelist": 1,
        "active_pairs": 1,
        "pair": active["pair"],
        "pair_generation": active["generation"],
        "pair_config_hash": active["pair_config_hash"],
    }), encoding="utf-8")
    monkeypatch.setattr(sidecar_main, "FREQTRADE_HEARTBEAT_FILE", heartbeat)
    assert sidecar_main.freqtrade_pair_ready(controller)[0] is True
    payload = json.loads(heartbeat.read_text())
    payload["pair_config_hash"] = "0" * 64
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")
    ready, detail = sidecar_main.freqtrade_pair_ready(controller)
    assert ready is False
    assert detail["checks"]["config_hash_match"] is False
    assert active["pair_config_hash"] == pair_config_hash("BTC/USDT", ())


def test_telegram_pair_menu_is_paginated_and_nonce_bound(monkeypatch):
    rows = [
        {
            "pair": f"BTC/Q{index:02d}",
            "symbol": f"BTCQ{index:02d}",
            "base": "BTC",
            "quote": f"Q{index:02d}",
            "capabilities": {"spot": True},
            "filter_types": ["PRICE_FILTER", "LOT_SIZE"],
        }
        for index in range(10)
    ]
    monkeypatch.setattr(telegram_bot, "CB", CallbackStore(ttl=120))
    monkeypatch.setattr(
        telegram_bot,
        "sidecar_command",
        lambda *_args, **_kwargs: {
            "ok": True,
            "result": json.dumps({
                "schema_version": 1,
                "registry_hash": "a" * 64,
                "pairs": rows,
            }),
        },
    )
    sent = []
    monkeypatch.setattr(
        telegram_bot,
        "send",
        lambda text, chat_id=None, buttons=None: sent.append((text, chat_id, buttons)),
    )
    telegram_bot._show_pairs("7", 0)
    buttons = sent[-1][2]
    assert len([row for row in buttons if row[0]["callback_data"].startswith("select|")]) == 8
    assert any(
        button["callback_data"].startswith("page|")
        for row in buttons
        for button in row
    )
