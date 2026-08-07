from __future__ import annotations
"""Complete Binance Spot filter prevalidation (fixes V101-NEW-002).

The previous replacement prevalidation checked only quantity/step, tick
multiples, minimum notional and trailing-delta bounds. Binance can reject an
order for PRICE_FILTER min/max, PERCENT_PRICE(_BY_SIDE) reference-price bands,
NOTIONAL maximum, or MAX_NUM_ORDERS / MAX_NUM_ALGO_ORDERS /
MAX_NUM_ORDER_LISTS capacity — none of which were checked before the active
protection was cancelled. A rejected replacement after cancellation leaves the
position unprotected.

This module fetches the symbol's complete current filter set (public
exchangeInfo), current execution rules and reference price, and — through
caller-supplied authenticated callables — the open order / order-list counts,
then validates every leg of the intended replacement BEFORE anything is
cancelled.

Fail-closed rule: if the filter data cannot be fetched or is stale, the
conversion is refused before cancellation.
"""
import time
from dataclasses import dataclass, field
from decimal import Decimal

from services.common.audit import audit
from services.common.binance_public import BinancePublicClient
from services.common.config_bounds import env_int
from services.common.market_policy import allowed_quotes_from_env


class FilterDataUnavailable(RuntimeError):
    """Current exchange filter data could not be obtained. Refuse to convert."""


class FilterViolation(ValueError):
    """The intended replacement violates a current Binance filter."""


@dataclass
class OrderLeg:
    side: str                      # 'SELL' or 'BUY'
    order_type: str                # LIMIT_MAKER / STOP_LOSS_LIMIT / ...
    qty: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    trailing_delta: int | None = None
    is_algo: bool = field(init=False, default=False)

    def __post_init__(self):
        self.is_algo = self.order_type.upper() in {
            'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'
        }


def legs_from_request(endpoint: str, params: dict) -> list[OrderLeg]:
    """Translate a factory-built replacement request into validation legs."""
    if endpoint == 'order':
        return [OrderLeg(side=str(params.get('side', 'SELL')),
                         order_type=str(params.get('type', 'STOP_LOSS_LIMIT')),
                         qty=Decimal(str(params['quantity'])),
                         price=Decimal(str(params['price'])) if params.get('price') else None,
                         stop_price=Decimal(str(params['stopPrice'])) if params.get('stopPrice') else None,
                         trailing_delta=int(params['trailingDelta']) if params.get('trailingDelta') is not None else None)]
    if endpoint in {'orderList/oto', 'orderList/otoco'}:
        working = OrderLeg(
            side=str(params.get('workingSide', 'BUY')),
            order_type=str(params.get('workingType', 'LIMIT')),
            qty=Decimal(str(params['workingQuantity'])),
            price=Decimal(str(params['workingPrice'])) if params.get('workingPrice') else None,
        )
        pending_qty = Decimal(str(params['pendingQuantity']))
        if endpoint == 'orderList/oto':
            pending = OrderLeg(
                side=str(params.get('pendingSide', 'SELL')),
                order_type=str(params.get('pendingType', 'STOP_LOSS_LIMIT')),
                qty=pending_qty,
                price=Decimal(str(params['pendingPrice'])) if params.get('pendingPrice') else None,
                stop_price=Decimal(str(params['pendingStopPrice'])) if params.get('pendingStopPrice') else None,
                trailing_delta=(int(params['pendingTrailingDelta'])
                                if params.get('pendingTrailingDelta') is not None else None),
            )
            return [working, pending]
        above = OrderLeg(
            side=str(params.get('pendingSide', 'SELL')),
            order_type=str(params.get('pendingAboveType', 'LIMIT_MAKER')),
            qty=pending_qty,
            price=Decimal(str(params['pendingAbovePrice'])) if params.get('pendingAbovePrice') else None,
        )
        below = OrderLeg(
            side=str(params.get('pendingSide', 'SELL')),
            order_type=str(params.get('pendingBelowType', 'STOP_LOSS_LIMIT')),
            qty=pending_qty,
            price=Decimal(str(params['pendingBelowPrice'])) if params.get('pendingBelowPrice') else None,
            stop_price=(Decimal(str(params['pendingBelowStopPrice']))
                        if params.get('pendingBelowStopPrice') else None),
            trailing_delta=(int(params['pendingBelowTrailingDelta'])
                            if params.get('pendingBelowTrailingDelta') is not None else None),
        )
        return [working, above, below]
    qty = Decimal(str(params['quantity']))
    above = OrderLeg(side='SELL', order_type=str(params.get('aboveType', 'LIMIT_MAKER')), qty=qty,
                     price=Decimal(str(params['abovePrice'])) if params.get('abovePrice') else None)
    below = OrderLeg(side='SELL', order_type=str(params.get('belowType', 'STOP_LOSS_LIMIT')), qty=qty,
                     price=Decimal(str(params['belowPrice'])) if params.get('belowPrice') else None,
                     stop_price=Decimal(str(params['belowStopPrice'])) if params.get('belowStopPrice') else None,
                     trailing_delta=int(params['belowTrailingDelta']) if params.get('belowTrailingDelta') is not None else None)
    return [above, below]


class SpotFilterValidator:
    def __init__(self, public_client: BinancePublicClient | None = None,
                 max_age_seconds: int | None = None):
        self.public = public_client or BinancePublicClient()
        self.max_age = max_age_seconds if max_age_seconds is not None else env_int(
            'SPOT_FILTER_MAX_AGE_SECONDS', 300, 10, 3600)
        self._cache: dict[str, tuple[float, dict]] = {}

    # ---- data acquisition (fail closed) ----
    def _symbol_entry(self, symbol: str) -> dict:
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] <= self.max_age:
            return cached[1]
        try:
            info = self.public.exchange_info(symbol)
            entry = next(s for s in info.get('symbols', []) if s.get('symbol') == symbol)
        except Exception as exc:
            if cached:
                raise FilterDataUnavailable(
                    f'current exchange filters for {symbol} are stale and refresh failed: {exc}') from exc
            raise FilterDataUnavailable(
                f'current exchange filters for {symbol} unavailable: {exc}') from exc
        self._cache[symbol] = (time.time(), entry)
        return entry

    def _reference_price(self, symbol: str, filters: dict) -> Decimal:
        """Use current referencePrice, with Binance's documented fallback."""
        current = self._official_reference_price(symbol)
        if current is not None:
            return current
        return self._fallback_reference_price(symbol, filters)

    def _fallback_reference_price(self, symbol: str, filters: dict) -> Decimal:
        """Resolve only the documented percent-filter avg/ticker fallback."""
        pps = filters.get('PERCENT_PRICE_BY_SIDE') or filters.get('PERCENT_PRICE') or {}
        try:
            if int(pps.get('avgPriceMins', 0) or 0) > 0:
                data = self.public.get('/api/v3/avgPrice', {'symbol': symbol})
                value = Decimal(str(data['price']))
            else:
                data = self.public.ticker_price(symbol)
                value = Decimal(str(data['price']))
        except Exception as exc:
            raise FilterDataUnavailable(
                f'fallback reference price for {symbol} unavailable: {exc}') from exc
        if not value.is_finite() or value <= 0:
            raise FilterDataUnavailable(
                f'fallback reference price for {symbol} is invalid: {value}')
        return value

    def _official_reference_price(self, symbol: str) -> Decimal | None:
        """Return only /referencePrice; None means Binance reports no reference."""
        try:
            ref = self.public.reference_price(symbol)
        except Exception as exc:
            raise FilterDataUnavailable(f'reference price for {symbol} unavailable: {exc}') from exc
        if ref is self.public.NO_REFERENCE_PRICE:
            return None
        try:
            value = Decimal(str(ref))
        except Exception as exc:
            raise FilterDataUnavailable(
                f'malformed reference price for {symbol}: {ref!r}') from exc
        if not value.is_finite() or value <= 0:
            raise FilterDataUnavailable(
                f'non-positive reference price for {symbol}: {value}')
        return value

    def _price_range_rule(self, symbol: str) -> dict[str, Decimal | None] | None:
        """Return one validated PRICE_RANGE rule, or None when none is set."""
        try:
            payload = self.public.execution_rules(symbol)
        except Exception as exc:
            raise FilterDataUnavailable(
                f'execution rules for {symbol} unavailable: {exc}') from exc
        rows = payload.get('symbolRules') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FilterDataUnavailable(
                f'executionRules for {symbol} lacks a symbolRules list')
        if any(not isinstance(row, dict) for row in rows):
            raise FilterDataUnavailable(
                f'executionRules for {symbol} contains a malformed symbol row')
        matching = [
            row for row in rows
            if str(row.get('symbol', '')).upper() == symbol
        ]
        if not matching:
            if rows:
                raise FilterDataUnavailable(
                    f'executionRules returned rows but none for exact symbol {symbol}')
            return None
        if len(matching) != 1:
            raise FilterDataUnavailable(
                f'executionRules returned {len(matching)} rows for {symbol}')
        if len(rows) != 1:
            raise FilterDataUnavailable(
                f'executionRules returned unexpected extra symbol rows for {symbol}')
        rules = matching[0].get('rules')
        if not isinstance(rules, list):
            raise FilterDataUnavailable(
                f'executionRules for {symbol} lacks a rules list')
        if any(not isinstance(rule, dict) for rule in rules):
            raise FilterDataUnavailable(
                f'executionRules for {symbol} contains a malformed rule')
        price_rules = [
            rule for rule in rules
            if rule.get('ruleType') == 'PRICE_RANGE'
        ]
        if not price_rules:
            return None
        if len(price_rules) != 1:
            raise FilterDataUnavailable(
                f'executionRules returned duplicate PRICE_RANGE rules for {symbol}')
        source = price_rules[0]
        parsed: dict[str, Decimal | None] = {}
        for name in (
            'bidLimitMultUp', 'bidLimitMultDown',
            'askLimitMultUp', 'askLimitMultDown',
        ):
            raw = source.get(name)
            if raw in (None, ''):
                parsed[name] = None
                continue
            try:
                value = Decimal(str(raw))
            except Exception as exc:
                raise FilterDataUnavailable(
                    f'{symbol} PRICE_RANGE {name} is malformed: {raw!r}') from exc
            if not value.is_finite() or value <= 0:
                raise FilterDataUnavailable(
                    f'{symbol} PRICE_RANGE {name} is invalid: {value}')
            parsed[name] = value
        for side in ('bid', 'ask'):
            down = parsed[f'{side}LimitMultDown']
            up = parsed[f'{side}LimitMultUp']
            if down is not None and up is not None and down > up:
                raise FilterDataUnavailable(
                    f'{symbol} PRICE_RANGE {side} lower multiplier exceeds upper multiplier')
        return parsed

    # ---- validation ----
    @staticmethod
    def _dec(mapping: dict, key: str) -> Decimal | None:
        value = mapping.get(key)
        if value in (None, '', '0', 0):
            return None
        return Decimal(str(value))

    def validate_replacement(self, symbol: str, endpoint: str, params: dict, *,
                             open_orders_provider=None, open_order_lists_provider=None,
                             replacing_order_ids=(), replacing_order_list: bool = False) -> dict:
        """Validate every leg against the complete current filter set.

        Raises FilterViolation / FilterDataUnavailable. Returns a summary dict
        with the validated filter names for the audit trail.
        """
        entry = self._symbol_entry(symbol)
        if entry.get('status') != 'TRADING':
            raise FilterViolation(f'{symbol} status is {entry.get("status")!r}, not TRADING')
        if entry.get('isSpotTradingAllowed') is not True:
            raise FilterViolation(f'{symbol} spot trading is not allowed')
        if str(entry.get('baseAsset', '')).upper() != 'BTC':
            raise FilterViolation(f'{symbol} base asset is not BTC')
        quote = str(entry.get('quoteAsset', '')).upper()
        quote_allowlist = allowed_quotes_from_env()
        if quote_allowlist and quote not in quote_allowlist:
            raise FilterViolation(f'{symbol} quote {quote!r} is outside ALLOWED_STABLE_QUOTES')
        if str(entry.get('symbol', '')).upper() != 'BTC' + quote:
            raise FilterViolation(f'{symbol} exchangeInfo assets do not reconstruct the symbol')
        if endpoint == 'orderList/oto' and entry.get('otoAllowed') is not True:
            raise FilterViolation(f'{symbol} does not currently allow OTO order lists')
        if endpoint == 'orderList/otoco' and (
                entry.get('otoAllowed') is not True
                or entry.get('ocoAllowed') is not True):
            raise FilterViolation(f'{symbol} does not currently allow OTOCO order lists')
        if endpoint == 'orderList/oco' and entry.get('ocoAllowed') is not True:
            raise FilterViolation(f'{symbol} does not currently allow OCO order lists')
        filters = {f.get('filterType'): f for f in entry.get('filters', [])}
        legs = legs_from_request(endpoint, params)
        checked: list[str] = []

        price_filter = filters.get('PRICE_FILTER') or {}
        tick = self._dec(price_filter, 'tickSize')
        min_price = self._dec(price_filter, 'minPrice')
        max_price = self._dec(price_filter, 'maxPrice')
        lot = filters.get('LOT_SIZE') or {}
        step = self._dec(lot, 'stepSize')
        min_qty = self._dec(lot, 'minQty')
        max_qty = self._dec(lot, 'maxQty')
        notional = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}
        min_notional = self._dec(notional, 'minNotional') or self._dec(notional, 'notional')
        max_notional = self._dec(notional, 'maxNotional')
        trailing = filters.get('TRAILING_DELTA') or {}
        pps = filters.get('PERCENT_PRICE_BY_SIDE')
        pp = filters.get('PERCENT_PRICE')
        price_range = self._price_range_rule(symbol)

        trailing_legs = [leg for leg in legs if leg.trailing_delta is not None]
        trail_min = trail_max = None
        if trailing_legs:
            if entry.get('allowTrailingStop') is not True:
                raise FilterViolation(f'{symbol} does not currently allow trailing-stop orders')
            if not trailing:
                raise FilterViolation(f'{symbol} has no TRAILING_DELTA filter')
            try:
                trail_min = int(trailing['minTrailingBelowDelta'])
                trail_max = int(trailing['maxTrailingBelowDelta'])
            except (KeyError, TypeError, ValueError) as exc:
                raise FilterViolation(
                    f'{symbol} TRAILING_DELTA sell bounds are missing or malformed') from exc
            if trail_min < 1 or trail_max < trail_min:
                raise FilterViolation(f'{symbol} TRAILING_DELTA sell bounds are invalid')

        official_reference = None
        if price_range is not None or pps or pp:
            official_reference = self._official_reference_price(symbol)
        reference = None
        if pps or pp:
            reference = official_reference
            if reference is None:
                reference = self._fallback_reference_price(symbol, filters)

        for leg in legs:
            prices = [p for p in (leg.price, leg.stop_price) if p is not None]
            for price in prices:
                if price <= 0:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} <= 0')
                if tick and price % tick != 0:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} not a multiple of tickSize {tick}')
                if min_price and price < min_price:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} below PRICE_FILTER.minPrice {min_price}')
                if max_price and price > max_price:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} above PRICE_FILTER.maxPrice {max_price}')
                if reference is not None:
                    if pps:
                        prefix = 'ask' if leg.side.upper() == 'SELL' else 'bid'
                        down = self._dec(pps, prefix + 'MultiplierDown')
                        up = self._dec(pps, prefix + 'MultiplierUp')
                    else:
                        down = self._dec(pp, 'multiplierDown')
                        up = self._dec(pp, 'multiplierUp')
                    if down and price < reference * down:
                        raise FilterViolation(
                            f'{symbol} {leg.order_type} price {price} below percent-price band '
                            f'({reference} x {down}) — Binance would reject the replacement')
                    if up and price > reference * up:
                        raise FilterViolation(
                            f'{symbol} {leg.order_type} price {price} above percent-price band '
                            f'({reference} x {up}) — Binance would reject the replacement')
            if price_range is not None and official_reference is not None and leg.price is not None:
                prefix = 'ask' if leg.side.upper() == 'SELL' else 'bid'
                down = price_range[f'{prefix}LimitMultDown']
                up = price_range[f'{prefix}LimitMultUp']
                if down is not None and leg.price < official_reference * down:
                    raise FilterViolation(
                        f'{symbol} {leg.order_type} execution price {leg.price} below '
                        f'PRICE_RANGE ({official_reference} x {down}); execution could expire')
                if up is not None and leg.price > official_reference * up:
                    raise FilterViolation(
                        f'{symbol} {leg.order_type} execution price {leg.price} above '
                        f'PRICE_RANGE ({official_reference} x {up}); execution could expire')
            if step and leg.qty % step != 0:
                raise FilterViolation(f'{symbol} qty {leg.qty} not a multiple of stepSize {step}')
            if min_qty and leg.qty < min_qty:
                raise FilterViolation(f'{symbol} qty {leg.qty} below LOT_SIZE.minQty {min_qty}')
            if max_qty and leg.qty > max_qty:
                raise FilterViolation(f'{symbol} qty {leg.qty} above LOT_SIZE.maxQty {max_qty}')
            notional_price = leg.price or leg.stop_price
            if notional_price is not None:
                if min_notional and notional_price * leg.qty < min_notional:
                    raise FilterViolation(
                        f'{symbol} {leg.order_type} notional {notional_price * leg.qty} below minNotional {min_notional}')
                if max_notional and notional_price * leg.qty > max_notional:
                    raise FilterViolation(
                        f'{symbol} {leg.order_type} notional {notional_price * leg.qty} above maxNotional {max_notional}')
            if leg.trailing_delta is not None:
                assert trail_min is not None and trail_max is not None
                if not trail_min <= leg.trailing_delta <= trail_max:
                    raise FilterViolation(
                        f'{symbol} trailingDelta {leg.trailing_delta} outside '
                        f'[{trail_min},{trail_max}]')
        checked.extend(name for name in (
            'PRICE_FILTER', 'PERCENT_PRICE_BY_SIDE' if pps else ('PERCENT_PRICE' if pp else None),
            'LOT_SIZE', 'NOTIONAL', 'TRAILING_DELTA') if name and name in filters or name == 'NOTIONAL')
        if price_range is not None:
            checked.append('PRICE_RANGE')

        # Order/list count capacity. The caller supplies authenticated
        # enumeration callables; a failed enumeration is fail-closed because a
        # capacity rejection after cancellation would leave the position naked.
        new_orders = len(legs)
        if open_orders_provider is not None:
            max_orders_filter = filters.get('MAX_NUM_ORDERS') or {}
            max_algo_filter = filters.get('MAX_NUM_ALGO_ORDERS') or {}
            try:
                open_orders = open_orders_provider(symbol)
            except Exception as exc:
                raise FilterDataUnavailable(f'open-order enumeration failed for {symbol}: {exc}') from exc
            replaced = {int(value) for value in replacing_order_ids if value not in (None, '', 0, '0')}
            effective_orders = [item for item in open_orders if int(item.get('orderId', 0) or 0) not in replaced]
            limit = int(max_orders_filter.get('maxNumOrders', 0) or 0)
            if limit and len(effective_orders) + new_orders > limit:
                raise FilterViolation(
                    f'{symbol} would exceed MAX_NUM_ORDERS {limit} '
                    f'({len(effective_orders)} effective open + {new_orders} new)')
            algo_limit = int(max_algo_filter.get('maxNumAlgoOrders', 0) or 0)
            if algo_limit:
                open_algo = sum(1 for o in effective_orders if str(o.get('type', '')).upper() in {
                    'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'})
                new_algo = sum(1 for leg in legs if leg.is_algo)
                if open_algo + new_algo > algo_limit:
                    raise FilterViolation(
                        f'{symbol} would exceed MAX_NUM_ALGO_ORDERS {algo_limit} '
                        f'({open_algo} open + {new_algo} new)')
            checked.extend(['MAX_NUM_ORDERS', 'MAX_NUM_ALGO_ORDERS'])
        if endpoint.startswith('orderList/') and open_order_lists_provider is not None:
            list_filter = filters.get('MAX_NUM_ORDER_LISTS') or {}
            limit = int(list_filter.get('maxNumOrderLists', 0) or 0)
            if limit:
                try:
                    open_lists = open_order_lists_provider()
                except Exception as exc:
                    raise FilterDataUnavailable(f'open order-list enumeration failed: {exc}') from exc
                effective_lists = max(0, len(open_lists) - (1 if replacing_order_list else 0))
                if effective_lists + 1 > limit:
                    raise FilterViolation(
                        f'account would exceed MAX_NUM_ORDER_LISTS {limit} '
                        f'({effective_lists} effective open + 1 new)')
            checked.append('MAX_NUM_ORDER_LISTS')

        summary = {'symbol': symbol, 'endpoint': endpoint, 'legs': len(legs),
                   'reference_price': str(reference) if reference is not None else None,
                   'execution_reference_price': (
                       str(official_reference) if official_reference is not None else None
                   ),
                   'execution_rule': ({
                       key: str(value) if value is not None else None
                       for key, value in price_range.items()
                   } if price_range is not None else None),
                   'filters_checked': checked}
        audit('replacement_filters_validated', details=summary)
        return summary
