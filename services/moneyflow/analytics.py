from __future__ import annotations

"""Pure, deterministic money-flow calculations."""

import math
from decimal import Decimal, InvalidOperation


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


def external_confluence(
    context: dict,
    *,
    spot_mid: float,
    minimum_providers: int = 1,
    minimum_change_24h_pct: float = 0.0,
    maximum_price_deviation_bps: float = 100.0,
) -> dict:
    """Evaluate fixed-BTC third-party quotes without discovering or ranking assets.

    A provider confirms only when its cached quote is fresh, its 24-hour change
    meets the configured direction floor, and its aggregated USD price remains
    plausibly close to Binance Spot.  Every fresh provider must agree; a
    contradictory fresh provider cannot be hidden by lowering the quorum.
    """
    mid = float(spot_mid)
    minimum = int(minimum_providers)
    change_floor = float(minimum_change_24h_pct)
    deviation_limit = float(maximum_price_deviation_bps)
    if not math.isfinite(mid) or mid <= 0:
        raise ValueError("spot mid must be finite and positive")
    if minimum not in {1, 2}:
        raise ValueError("external confluence provider quorum must be 1 or 2")
    if not math.isfinite(change_floor) or not -100 <= change_floor <= 100:
        raise ValueError("external confluence change floor is invalid")
    if not math.isfinite(deviation_limit) or not 1 <= deviation_limit <= 1_000:
        raise ValueError("external confluence price deviation is invalid")

    rows = []
    providers = context.get("providers") if isinstance(context, dict) else {}
    providers = providers if isinstance(providers, dict) else {}
    for name in ("coingecko", "coinmarketcap"):
        provider = providers.get(name)
        if not isinstance(provider, dict) or not provider.get("enabled"):
            continue
        row = {
            "provider": name,
            "fresh": bool(provider.get("available") and provider.get("fresh")),
            "confirms": False,
        }
        if row["fresh"]:
            data = provider.get("data") if isinstance(provider.get("data"), dict) else {}
            try:
                price = float(data["price_usd"])
                change = float(data["percent_change_24h"])
                if not math.isfinite(price) or price <= 0 or not math.isfinite(change):
                    raise ValueError("non-finite provider value")
                deviation = abs(price - mid) / mid * 10_000.0
                row.update({
                    "price_usd": price,
                    "percent_change_24h": change,
                    "price_deviation_bps": deviation,
                    "price_consistent": deviation <= deviation_limit,
                    "direction_confirms": change >= change_floor,
                })
                row["confirms"] = bool(
                    row["price_consistent"] and row["direction_confirms"]
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                row["fresh"] = False
                row["reason"] = "malformed_provider_data"
        else:
            row["reason"] = str(provider.get("reason") or provider.get("status") or "unavailable")
        rows.append(row)

    fresh = [row for row in rows if row["fresh"]]
    confirming = [row for row in fresh if row["confirms"]]
    confirmed = len(fresh) >= minimum and len(confirming) == len(fresh)
    return {
        "confirmed": confirmed,
        "minimum_providers": minimum,
        "fresh_provider_count": len(fresh),
        "confirming_provider_count": len(confirming),
        "minimum_change_24h_pct": change_floor,
        "maximum_price_deviation_bps": deviation_limit,
        "providers": rows,
        "reason": "confirmed" if confirmed else "external_confluence_not_confirmed",
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


def classify(
    snapshot: dict,
    *,
    min_taker_ratio: float = 0.55,
    min_spot_imbalance: float = 0.05,
    require_external_confluence: bool = False,
) -> dict:
    spot = snapshot.get("spot") or {}
    spot_ok = (
        float((spot.get("trades") or {}).get("taker_buy_ratio", 0)) >= min_taker_ratio
        and float((spot.get("depth") or {}).get("imbalance", -1)) >= min_spot_imbalance
    )
    timeframes = snapshot.get("timeframes") or {}
    higher = [str((timeframes.get(name) or {}).get("direction"))
              for name in ("15m", "1h", "2h", "4h", "1d")]
    higher_ok = sum(item == "bullish" for item in higher) >= (len(higher) + 1) // 2
    external = ((snapshot.get("external_context") or {}).get("confluence") or {})
    external_ok = bool(external.get("confirmed")) if require_external_confluence else True
    bullish = bool(spot_ok and higher_ok and external_ok)
    return {
        "bullish": bullish,
        "market_context_mode": "spot_only",
        "spot_pressure_bullish": spot_ok,
        "higher_timeframe_bullish": higher_ok,
        "external_confluence_required": bool(require_external_confluence),
        "external_confluence_confirmed": (
            bool(external.get("confirmed")) if require_external_confluence else None
        ),
        "decision": "BULLISH" if bullish else "NOT_BULLISH",
    }
