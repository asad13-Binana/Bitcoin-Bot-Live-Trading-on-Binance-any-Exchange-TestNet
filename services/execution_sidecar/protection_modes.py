from __future__ import annotations
"""Pure Binance Spot OTO/OTOCO/OCO request construction."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from services.common.models import ProtectionMode


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    value, step = Decimal(value), Decimal(step)
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def decimal_text(value: Decimal) -> str:
    text = format(Decimal(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class ProtectionSettings:
    take_profit_pct: Decimal = Decimal("1.2")
    fixed_stop_pct: Decimal = Decimal("2.0")
    trailing_delta_bips: int = 40
    limit_fill_buffer_bips: int = 20
    fee_pct_per_side: Decimal = Decimal("0.1")


class OrderRequestFactory:
    """Build requests only; the adapter validates filters and owns network I/O."""

    def __init__(self, new_client_id, settings: ProtectionSettings | None = None):
        self.new_client_id = new_client_id
        self.settings = settings or ProtectionSettings()

    @staticmethod
    def _delta(rules, requested: int) -> int:
        return max(int(rules.trail_min), min(int(requested), int(rules.trail_max)))

    def pending_qty(self, rules, qty: Decimal) -> Decimal:
        fee = self.settings.fee_pct_per_side
        if fee < 0 or fee >= 100:
            raise ValueError("fee percentage must be within [0,100)")
        return floor_step(qty * (Decimal(1) - fee / Decimal(100)), rules.step)

    def entry(self, mode: ProtectionMode, rules, qty: Decimal, entry: Decimal) -> tuple[str, dict]:
        qty = floor_step(qty, rules.step)
        entry = floor_step(entry, rules.tick)
        pending = self.pending_qty(rules, qty)
        if pending <= 0:
            raise ValueError("fee-shaved protective quantity rounds to zero")
        settings = self.settings
        delta = self._delta(rules, settings.trailing_delta_bips)
        tp = floor_step(entry * (1 + settings.take_profit_pct / 100), rules.tick)
        list_id = self.new_client_id("ENTRYLIST")
        common = {
            "symbol": rules.symbol,
            "listClientOrderId": list_id,
            "workingClientOrderId": self.new_client_id("ENTRY"),
            "workingType": "LIMIT", "workingSide": "BUY",
            "workingPrice": decimal_text(entry), "workingQuantity": decimal_text(qty),
            "workingTimeInForce": "GTC",
            "pendingSide": "SELL", "pendingQuantity": decimal_text(pending),
            "newOrderRespType": "FULL",
        }
        if mode is ProtectionMode.TRAILING_ONLY:
            floor = floor_step(
                entry * (1 - Decimal(delta + settings.limit_fill_buffer_bips) / 10_000), rules.tick)
            common.update({
                "pendingClientOrderId": self.new_client_id("TRAIL"),
                "pendingType": "STOP_LOSS_LIMIT", "pendingPrice": decimal_text(floor),
                "pendingTrailingDelta": delta, "pendingTimeInForce": "GTC",
            })
            return "orderList/oto", common
        common.update({
            "pendingAboveClientOrderId": self.new_client_id("TP"),
            "pendingAboveType": "LIMIT_MAKER", "pendingAbovePrice": decimal_text(tp),
            "pendingBelowClientOrderId": self.new_client_id("STOP"),
            "pendingBelowType": "STOP_LOSS_LIMIT", "pendingBelowTimeInForce": "GTC",
        })
        if mode is ProtectionMode.FIXED_OCO:
            stop = floor_step(entry * (1 - settings.fixed_stop_pct / 100), rules.tick)
            floor = floor_step(stop * (1 - Decimal(settings.limit_fill_buffer_bips) / 10_000), rules.tick)
            common.update({"pendingBelowStopPrice": decimal_text(stop),
                           "pendingBelowPrice": decimal_text(floor)})
        else:
            floor = floor_step(
                entry * (1 - Decimal(delta + settings.limit_fill_buffer_bips) / 10_000), rules.tick)
            common.update({"pendingBelowTrailingDelta": delta,
                           "pendingBelowPrice": decimal_text(floor)})
        return "orderList/otoco", common

    def replacement(self, mode: ProtectionMode, rules, qty: Decimal,
                    entry: Decimal, current: Decimal, *, stop_price: Decimal | None = None,
                    trailing_delta_bips: int | None = None) -> tuple[str, dict]:
        qty = floor_step(qty, rules.step)
        entry, current = Decimal(entry), Decimal(current)
        if qty <= 0 or entry <= 0 or current <= 0:
            raise ValueError("replacement quantity and prices must be positive")
        settings = self.settings
        delta = self._delta(rules, trailing_delta_bips or settings.trailing_delta_bips)
        if mode is ProtectionMode.TRAILING_ONLY:
            floor = floor_step(
                current * (1 - Decimal(delta + settings.limit_fill_buffer_bips) / 10_000), rules.tick)
            return "order", {
                "symbol": rules.symbol, "side": "SELL", "type": "STOP_LOSS_LIMIT",
                "quantity": decimal_text(qty), "price": decimal_text(floor),
                "trailingDelta": delta, "timeInForce": "GTC",
                "newClientOrderId": self.new_client_id("TRAIL"),
            }
        tp = floor_step(max(
            entry * (1 + settings.take_profit_pct / 100),
            current * (1 + Decimal("0.002"))), rules.tick)
        params = {
            "symbol": rules.symbol, "side": "SELL", "quantity": decimal_text(qty),
            "listClientOrderId": self.new_client_id("PROTECT"),
            "aboveClientOrderId": self.new_client_id("TP"),
            "aboveType": "LIMIT_MAKER", "abovePrice": decimal_text(tp),
            "belowClientOrderId": self.new_client_id("STOP"),
            "belowType": "STOP_LOSS_LIMIT", "belowTimeInForce": "GTC",
            "newOrderRespType": "FULL",
        }
        if mode is ProtectionMode.FIXED_OCO:
            stop = floor_step(stop_price if stop_price is not None else (
                entry * (1 - settings.fixed_stop_pct / 100)), rules.tick)
            if stop >= current:
                raise ValueError("fixed protective stop must be below current price")
            floor = floor_step(stop * (1 - Decimal(settings.limit_fill_buffer_bips) / 10_000), rules.tick)
            params.update({"belowStopPrice": decimal_text(stop), "belowPrice": decimal_text(floor)})
        else:
            floor = floor_step(
                current * (1 - Decimal(delta + settings.limit_fill_buffer_bips) / 10_000), rules.tick)
            params.update({"belowTrailingDelta": delta, "belowPrice": decimal_text(floor)})
        return "orderList/oco", params


def fee_adjusted_break_even(entry: Decimal, *, buy_fee_pct: Decimal,
                            sell_fee_pct: Decimal, slippage_pct: Decimal = Decimal(0)) -> Decimal:
    entry = Decimal(entry)
    buy_fee_pct, sell_fee_pct, slippage_pct = map(Decimal, (
        buy_fee_pct, sell_fee_pct, slippage_pct))
    if entry <= 0 or any(value < 0 or value >= 100 for value in (
            buy_fee_pct, sell_fee_pct, slippage_pct)):
        raise ValueError("invalid break-even inputs")
    cost = entry * (1 + (buy_fee_pct + slippage_pct) / 100)
    proceeds_ratio = 1 - (sell_fee_pct + slippage_pct) / 100
    if proceeds_ratio <= 0:
        raise ValueError("fees/slippage leave no proceeds")
    return cost / proceeds_ratio
