from __future__ import annotations

import json

import pytest

from services.moneyflow.analytics import external_confluence
from services.moneyflow.spot_stream import MARKET_STREAM_BASE, SpotMarketStream


def _trade(identifier: int, *, buyer_is_maker: bool, quantity: str = "1") -> dict:
    return {
        "e": "aggTrade",
        "s": "BTCUSDT",
        "a": identifier,
        "p": "100",
        "q": quantity,
        "T": 1_800_000_000_000 + identifier,
        "m": buyer_is_maker,
    }


def _book() -> dict:
    return {
        "s": "BTCUSDT",
        "b": "99.99",
        "B": "2",
        "a": "100.01",
        "A": "3",
    }


def _offline_stream() -> SpotMarketStream:
    stream = SpotMarketStream(stale_after_seconds=10)
    stream._symbol = "BTCUSDT"
    stream._connected = True
    return stream


def test_market_stream_uses_market_data_only_host_and_two_nonduplicated_streams():
    endpoint = SpotMarketStream.endpoint("BTCUSDT")
    assert endpoint.startswith(MARKET_STREAM_BASE)
    assert "data-stream.binance.vision" in endpoint
    assert "btcusdt@aggTrade" in endpoint
    assert "btcusdt@bookTicker" in endpoint
    assert "api_key" not in endpoint.lower()
    with pytest.raises(ValueError):
        SpotMarketStream.endpoint("ETHUSDT")


def test_rolling_flow_uses_receive_time_windows_and_book_freshness():
    stream = _offline_stream()
    assert stream.ingest_trade(
        _trade(10, buyer_is_maker=False),
        received_monotonic=100.0,
        received_epoch=1_800_000_000.0,
    )
    assert stream.ingest_trade(
        _trade(11, buyer_is_maker=True, quantity="2"),
        received_monotonic=159.0,
        received_epoch=1_800_000_059.0,
    )
    stream.ingest_book(
        _book(), received_monotonic=159.0, received_epoch=1_800_000_059.0
    )
    snapshot = stream.snapshot(now_monotonic=160.0)
    assert snapshot["ready"] is True
    assert snapshot["fresh"] is True
    assert snapshot["flow"]["window_seconds"] == 60
    assert snapshot["flow"]["trade_count"] == 2
    assert snapshot["flow"]["taker_buy_quote"] == 100.0
    assert snapshot["flow"]["taker_sell_quote"] == 200.0
    assert snapshot["flow"]["taker_buy_ratio"] == pytest.approx(1 / 3)
    assert snapshot["flow"]["cvd_quote"] == -100.0
    assert snapshot["windows"]["15"]["trade_count"] == 1
    assert snapshot["book_ticker"]["spread_bps"] == pytest.approx(2.0)


def test_duplicate_and_sequence_gap_fail_closed_until_a_new_full_window():
    stream = _offline_stream()
    assert stream.ingest_trade(
        _trade(20, buyer_is_maker=False),
        received_monotonic=100.0,
        received_epoch=1_800_000_000.0,
    )
    assert not stream.ingest_trade(
        _trade(20, buyer_is_maker=False),
        received_monotonic=101.0,
        received_epoch=1_800_000_001.0,
    )
    assert stream.ingest_trade(
        _trade(22, buyer_is_maker=True),
        received_monotonic=102.0,
        received_epoch=1_800_000_002.0,
    )
    stream.ingest_book(
        _book(), received_monotonic=102.0, received_epoch=1_800_000_002.0
    )
    snapshot = stream.snapshot(now_monotonic=103.0)
    assert snapshot["ready"] is False
    assert snapshot["sequence"]["duplicates_ignored"] == 1
    assert snapshot["sequence"]["gap_count"] == 1
    assert snapshot["flow"]["trade_count"] == 1


def test_stale_and_malformed_stream_data_never_becomes_ready():
    stream = _offline_stream()
    stream.ingest_trade(
        _trade(30, buyer_is_maker=False),
        received_monotonic=100.0,
        received_epoch=1_800_000_000.0,
    )
    stream.ingest_book(
        _book(), received_monotonic=100.0, received_epoch=1_800_000_000.0
    )
    assert stream.snapshot(now_monotonic=161.0)["ready"] is False
    stream._on_message(None, json.dumps({"stream": "x", "data": {"s": "ETHUSDT"}}), 0, "BTCUSDT")
    assert stream.snapshot(now_monotonic=161.0)["sequence"]["malformed_frames"] == 1


def _provider(*, change: float, price: float = 100.0, fresh: bool = True) -> dict:
    return {
        "enabled": True,
        "available": fresh,
        "fresh": fresh,
        "status": "fresh" if fresh else "stale",
        "data": {
            "price_usd": price,
            "percent_change_24h": change,
        },
    }


def test_external_confluence_requires_fresh_noncontradictory_provider_agreement():
    context = {
        "providers": {
            "coingecko": _provider(change=1.2, price=100.1),
            "coinmarketcap": _provider(change=0.8, price=99.9),
        }
    }
    result = external_confluence(
        context,
        spot_mid=100.0,
        minimum_providers=2,
        minimum_change_24h_pct=0.0,
        maximum_price_deviation_bps=25.0,
    )
    assert result["confirmed"] is True
    assert result["fresh_provider_count"] == 2
    assert result["confirming_provider_count"] == 2

    contradictory = json.loads(json.dumps(context))
    contradictory["providers"]["coinmarketcap"]["data"]["percent_change_24h"] = -0.1
    assert external_confluence(
        contradictory, spot_mid=100.0, minimum_providers=1
    )["confirmed"] is False

    stale = {"providers": {"coingecko": _provider(change=1.0, fresh=False)}}
    assert external_confluence(stale, spot_mid=100.0)["confirmed"] is False


@pytest.mark.parametrize(
    ("spot_mid", "minimum", "change", "deviation"),
    [(0, 1, 0, 100), (100, 0, 0, 100), (100, 1, -101, 100), (100, 1, 0, 0)],
)
def test_external_confluence_rejects_unsafe_configuration(
    spot_mid, minimum, change, deviation
):
    with pytest.raises(ValueError):
        external_confluence(
            {"providers": {}},
            spot_mid=spot_mid,
            minimum_providers=minimum,
            minimum_change_24h_pct=change,
            maximum_price_deviation_bps=deviation,
        )
