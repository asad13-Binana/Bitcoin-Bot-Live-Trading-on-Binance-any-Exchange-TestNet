from __future__ import annotations
"""Pure, deterministic money-flow calculations."""

from decimal import Decimal, InvalidOperation
import math


def _d(value) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid finite decimal: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"invalid finite decimal: {value!r}")
    return out


def depth_metrics(book: dict, *, band_bps: int = 10) -> dict:
    bids = [(_d(px), _d(qty)) for px, qty, *_ in (book.get("bids") or [])]
    asks = [(_d(px), _d(qty)) for px, qty, *_ in (book.get("asks") or [])]
    if not bids or not asks or any(px <= 0 or qty < 0 for px, qty in bids + asks):
        raise ValueError("order book is empty or malformed")
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid >= best_ask:
        raise ValueError("order book is crossed")
    mid = (best_bid + best_ask) / 2
    width = Decimal(int(band_bps)) / Decimal(10_000)
    bid_floor, ask_ceiling = mid * (1 - width), mid * (1 + width)
    bid_quote = sum(px * qty for px, qty in bids if px >= bid_floor)
    ask_quote = sum(px * qty for px, qty in asks if px <= ask_ceiling)
    total = bid_quote + ask_quote
    imbalance = (bid_quote - ask_quote) / total if total > 0 else Decimal(0)
    spread_bps = (best_ask - best_bid) / mid * Decimal(10_000)
    return {
        "best_bid": float(best_bid), "best_ask": float(best_ask),
        "mid": float(mid), "spread_bps": float(spread_bps),
        "bid_quote_in_band": float(bid_quote), "ask_quote_in_band": float(ask_quote),
        "imbalance": float(imbalance), "band_bps": int(band_bps),
    }


def aggregate_trade_flow(rows: list[dict]) -> dict:
    buy_quote = sell_quote = Decimal(0)
    first_id = last_id = None
    for row in rows:
        price, qty = _d(row.get("p")), _d(row.get("q"))
        if price <= 0 or qty < 0:
            raise ValueError("aggregate trade is malformed")
        quote = price * qty
        # Binance m=true: buyer is maker, therefore the aggressive taker sold.
        if bool(row.get("m")):
            sell_quote += quote
        else:
            buy_quote += quote
        identifier = row.get("a")
        first_id = identifier if first_id is None else first_id
        last_id = identifier
    total = buy_quote + sell_quote
    ratio = buy_quote / total if total > 0 else Decimal(0)
    return {
        "taker_buy_quote": float(buy_quote), "taker_sell_quote": float(sell_quote),
        "taker_buy_ratio": float(ratio), "cvd_quote": float(buy_quote - sell_quote),
        "trade_count": len(rows), "first_aggregate_id": first_id, "last_aggregate_id": last_id,
    }


def timeframe_context(klines: list, *, fast: int = 9, slow: int = 21) -> dict:
    closes = [float(row[4]) for row in klines if isinstance(row, (list, tuple)) and len(row) > 6]
    if len(closes) < slow + 2 or any(not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError("insufficient or malformed closed candles")
    # Drop the last candle: Binance's last kline can still be open.
    closes = closes[:-1]

    def ema(values, period):
        alpha = 2.0 / (period + 1.0)
        value = sum(values[:period]) / period
        for item in values[period:]:
            value = alpha * item + (1.0 - alpha) * value
        return value

    fast_ema, slow_ema = ema(closes, fast), ema(closes, slow)
    direction = "bullish" if closes[-1] > fast_ema > slow_ema else (
        "bearish" if closes[-1] < fast_ema < slow_ema else "mixed")
    return {
        "close": closes[-1], "ema9": fast_ema, "ema21": slow_ema,
        "direction": direction, "closed_candles": len(closes),
    }


def classify(snapshot: dict, *, min_taker_ratio: float = 0.55,
             min_spot_imbalance: float = 0.05) -> dict:
    spot = snapshot.get("spot") or {}
    futures = snapshot.get("futures") or {}
    spot_ok = (
        float((spot.get("trades") or {}).get("taker_buy_ratio", 0)) >= min_taker_ratio
        and float((spot.get("depth") or {}).get("imbalance", -1)) >= min_spot_imbalance
    )
    futures_available = bool(futures.get("available"))
    futures_ok = not futures_available or (
        float((futures.get("depth") or {}).get("imbalance", -1)) >= 0
        and float((futures.get("taker") or {}).get("buySellRatio", 0)) >= 1
    )
    timeframes = snapshot.get("timeframes") or {}
    higher = [str((timeframes.get(name) or {}).get("direction"))
              for name in ("15m", "1h", "2h", "4h", "1d")]
    higher_ok = sum(item == "bullish" for item in higher) >= (len(higher) + 1) // 2
    bullish = bool(spot_ok and futures_ok and higher_ok)
    return {
        "bullish": bullish,
        "spot_pressure_bullish": spot_ok,
        "futures_confirmation": futures_ok if futures_available else None,
        "higher_timeframe_bullish": higher_ok,
        "decision": "BULLISH" if bullish else "NOT_BULLISH",
    }
