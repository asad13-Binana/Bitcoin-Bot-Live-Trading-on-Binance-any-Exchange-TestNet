from __future__ import annotations
"""Sole authenticated Binance Spot order owner for the Bitcoin release."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import logging
import math
import os
import threading
import time

from services.common.audit import audit
from services.common.binance_public import BinancePublicClient, DEFAULT_BASE, SPOT_TESTNET_BASE
from services.common.config_bounds import env_float, env_int
from services.common.market_policy import (
    allowed_quotes_from_env, canonical_pair, validate_exchange_symbol,
)
from services.common.models import LifecycleState, ProtectionMode
from services.common.redaction import redact_text
from services.execution_sidecar.client_ids import ClientOrderIds
from services.execution_sidecar.filters import FilterViolation, SpotFilterValidator
from services.execution_sidecar.protection_modes import (
    OrderRequestFactory, ProtectionSettings, decimal_text,
    fee_adjusted_break_even, floor_step,
)
from services.execution_sidecar.spot_gateway import BinanceSpotGateway, SubmissionClass
from services.execution_sidecar.user_data_stream import ModernUserDataStream


log = logging.getLogger("bitcoin-spot-adapter")


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base: str
    quote: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    trail_min: int
    trail_max: int
    oco_allowed: bool
    oto_allowed: bool
    trailing_allowed: bool


def _decimal(mapping: dict, key: str, default: str = "0") -> Decimal:
    return Decimal(str(mapping.get(key, default) or default))


class BitcoinSpotAdapter:
    """Intent-first Spot execution with no automatic ambiguous-outcome retry."""

    def __init__(self, state_store, guard, pair_controller, notifier=None, *,
                 gateway=None, filter_validator=None, stream_factory=ModernUserDataStream,
                 execution_mode: str | None = None):
        self.state_store = state_store
        self.guard = guard
        self.pair_controller = pair_controller
        self.notifier = notifier or (lambda *args, **kwargs: None)
        self.mode = str(execution_mode or os.getenv("EXECUTION_MODE", "simulation")).lower()
        if self.mode not in {"testnet", "live"}:
            raise ValueError("BitcoinSpotAdapter is only for testnet/live")
        self.gateway = gateway or BinanceSpotGateway(testnet=self.mode == "testnet")
        if filter_validator is None:
            # The filter snapshot used immediately before cancel-and-replace
            # must come from the same Spot environment that owns the orders.
            # Production money-flow may remain a realistic advisory source for
            # Testnet, but it must never preflight a Testnet money-side action.
            public_base = os.getenv("BINANCE_SPOT_EXECUTION_PUBLIC_BASE", "").strip()
            if not public_base:
                public_base = SPOT_TESTNET_BASE if self.mode == "testnet" else DEFAULT_BASE
            filter_validator = SpotFilterValidator(BinancePublicClient(base=public_base))
        self.filter_validator = filter_validator
        self.stream_factory = stream_factory
        self.stream = None
        self.ids = ClientOrderIds(os.getenv("CLIENT_ORDER_PREFIX", "BTCB"))
        self.enabled = False
        self.started = False
        self.lock = threading.RLock()
        self.partial_recovery: set[str] = set()
        self.trade_size_quote = Decimal(str(env_float("TRADE_SIZE_QUOTE", 100, 5, 100_000)))
        self.max_positions = 1
        self.max_entry_open_seconds = env_int("MAX_ENTRY_OPEN_SECONDS", 300, 30, 900)
        settings = ProtectionSettings(
            take_profit_pct=Decimal(str(env_float("TAKE_PROFIT_PCT", 1.2, 0.05, 100))),
            fixed_stop_pct=Decimal(str(env_float("FIXED_STOP_PCT", 2.0, 0.05, 50))),
            trailing_delta_bips=env_int("TRAILING_DELTA_BIPS", 40, 10, 2000),
            limit_fill_buffer_bips=env_int("LIMIT_FILL_BUFFER_BIPS", 20, 1, 1000),
            fee_pct_per_side=Decimal(str(env_float("FEE_PCT_PER_SIDE", 0.1, 0, 5))),
        )
        self.factory = OrderRequestFactory(self.ids.new, settings)

    # ---- metadata / helpers ----
    def _rules(self, pair: str) -> SymbolRules:
        pair = canonical_pair(pair)
        symbol = pair.replace("/", "")
        metadata = validate_exchange_symbol(
            pair,
            self.gateway.symbol_info(symbol),
            allowed_quotes_from_env(),
            require_order_lists=False,
        )
        filters = {item.get("filterType"): item for item in metadata.get("filters", [])}
        lot, price = filters["LOT_SIZE"], filters["PRICE_FILTER"]
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        trailing = filters.get("TRAILING_DELTA") or {}
        rules = SymbolRules(
            symbol=symbol, base="BTC", quote=pair.split("/", 1)[1],
            tick=_decimal(price, "tickSize"), step=_decimal(lot, "stepSize"),
            min_qty=_decimal(lot, "minQty"), max_qty=_decimal(lot, "maxQty"),
            min_notional=_decimal(notional, "minNotional", str(notional.get("notional", "0"))),
            max_notional=_decimal(notional, "maxNotional"),
            trail_min=int(trailing.get("minTrailingBelowDelta", 10)),
            trail_max=int(trailing.get("maxTrailingBelowDelta", 2000)),
            oco_allowed=bool(metadata.get("ocoAllowed", False)),
            oto_allowed=bool(metadata.get("otoAllowed", False)),
            trailing_allowed=bool(metadata.get("allowTrailingStop", False)),
        )
        if rules.tick <= 0 or rules.step <= 0:
            raise RuntimeError(f"{symbol} lacks required Spot price/quantity filters")
        return rules

    @staticmethod
    def _require_mode_capabilities(
        rules: SymbolRules,
        mode: ProtectionMode,
        *,
        for_entry: bool,
    ) -> None:
        """Require only the exchange capabilities used by this exact request."""
        mode = ProtectionMode(mode)
        missing = []
        if for_entry and not rules.oto_allowed:
            missing.append("OTO")
        if mode in {ProtectionMode.FIXED_OCO, ProtectionMode.OCO_TRAILING}:
            if not rules.oco_allowed:
                missing.append("OCO")
        if mode in {ProtectionMode.TRAILING_ONLY, ProtectionMode.OCO_TRAILING}:
            if not rules.trailing_allowed:
                missing.append("trailing-stop")
        if missing:
            phase = "entry" if for_entry else "replacement"
            raise FilterViolation(
                f"{rules.symbol} cannot support {mode.value} {phase}: "
                + ", ".join(dict.fromkeys(missing))
                + " capability unavailable"
            )

    @staticmethod
    def _price(value) -> Decimal:
        out = Decimal(str(value))
        if not out.is_finite() or out <= 0:
            raise ValueError("exchange price must be finite and positive")
        return out

    def _best_ask(self, symbol: str) -> Decimal:
        asks = (self.gateway.order_book(symbol, 5) or {}).get("asks") or []
        if not asks:
            raise RuntimeError(f"{symbol} ask book is empty")
        return self._price(asks[0][0])

    def _best_bid(self, symbol: str) -> Decimal:
        bids = (self.gateway.order_book(symbol, 5) or {}).get("bids") or []
        if not bids:
            raise RuntimeError(f"{symbol} bid book is empty")
        return self._price(bids[0][0])

    def _current(self, symbol: str) -> Decimal:
        data = self.gateway.ticker_price(symbol)
        return self._price(data.get("price"))

    @staticmethod
    def _balances(account: dict) -> dict[str, tuple[Decimal, Decimal]]:
        rows = account.get("balances") if isinstance(account, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Binance account response has no balances list")
        out = {}
        for row in rows:
            asset = str(row.get("asset", "")).upper()
            if asset:
                out[asset] = (Decimal(str(row.get("free", "0"))), Decimal(str(row.get("locked", "0"))))
        return out

    def _free(self, asset: str) -> Decimal:
        return self._balances(self.gateway.account()).get(asset, (Decimal(0), Decimal(0)))[0]

    @staticmethod
    def _response_ids(response: dict, request: dict | None = None) -> tuple[int | None, int | None, int | None, int | None]:
        list_id = int(response.get("orderListId", 0) or 0) or None
        entry = take_profit = stop = None
        request = request or {}
        client_roles = {
            str(request.get("workingClientOrderId") or ""): "entry",
            str(request.get("pendingAboveClientOrderId") or request.get("aboveClientOrderId") or ""): "take_profit",
            str(request.get("pendingBelowClientOrderId") or request.get("belowClientOrderId") or ""): "stop",
            str(request.get("pendingClientOrderId") or request.get("newClientOrderId") or ""): "stop",
        }
        client_roles.pop("", None)
        for report in (response.get("orderReports") or response.get("orders") or []):
            order_id = int(report.get("orderId", 0) or 0) or None
            side, order_type = str(report.get("side", "")).upper(), str(report.get("type", "")).upper()
            role = client_roles.get(str(report.get("clientOrderId") or report.get("origClientOrderId") or ""))
            if side == "BUY" or role == "entry":
                entry = order_id
            elif order_type == "LIMIT_MAKER" or role == "take_profit":
                take_profit = order_id
            elif order_type in {"STOP_LOSS", "STOP_LOSS_LIMIT"} or role == "stop":
                stop = order_id
        return list_id, entry, take_profit, stop

    @staticmethod
    def _rest_event(order: dict, *, order_list_id=None) -> dict | None:
        if not isinstance(order, dict) or not order.get("orderId") or not order.get("status"):
            return None
        order_id = int(order["orderId"])
        status = str(order.get("status", "")).upper()
        event_time = order.get("updateTime") or order.get("transactTime") or order.get("time") or 0
        return {
            "e": "executionReport", "s": str(order.get("symbol", "")).upper(),
            "i": order_id, "g": order.get("orderListId", order_list_id),
            "S": order.get("side", ""), "X": status, "o": order.get("type", ""),
            "z": order.get("executedQty", "0"),
            "Z": order.get("cummulativeQuoteQty", order.get("cumulativeQuoteQty", "0")),
            "q": order.get("origQty", order.get("quantity", "0")),
            "E": event_time,
            "I": f"REST:{order_id}:{status}:{event_time}:{order.get('executedQty', '0')}",
        }

    def _record_authenticated_event(self, event: dict) -> bool:
        is_new = self.state_store.record_exchange_event(event)
        if is_new:
            self.guard.on_exchange_event(event)
        return is_new

    def _record_response_events(self, response: dict, *, order_list_id=None) -> None:
        candidates = list(response.get("orderReports") or [])
        if response.get("orderId"):
            candidates.append(response)
        for order in candidates:
            event = self._rest_event(order, order_list_id=order_list_id)
            if event:
                self._record_authenticated_event(event)

    def _bind_confirmed_intent(self, intent: dict, response: dict, request: dict) -> None:
        """Bind exchange IDs before replaying any user-stream/REST race events."""
        trade_id = str(intent.get("trade_id") or "")
        if not trade_id:
            return
        row = self.state_store.trade(trade_id)
        if not row:
            return
        operation = str(intent.get("operation") or "").upper()
        list_id, entry_id, tp_id, stop_id = self._response_ids(response, request)
        fields = {"reconciliation_status": f"{operation}_INTENT_CONFIRMED_BY_LOOKUP"}
        if operation == "ENTRY":
            fields.update(
                lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
                order_list_id=list_id, entry_order_id=entry_id,
                take_profit_order_id=tp_id, stop_order_id=stop_id,
            )
        elif operation in {"PROTECTION", "PARTIAL_FILL_PROTECTION"}:
            protected = (
                request.get("quantity") or request.get("pendingQuantity")
                or request.get("workingQuantity")
            )
            endpoint = str(intent.get("endpoint") or "")
            protection_mode = (
                ProtectionMode.TRAILING_ONLY if endpoint == "order"
                else ProtectionMode.OCO_TRAILING
                if request.get("belowTrailingDelta") is not None
                else ProtectionMode.FIXED_OCO
            )
            fields.update(
                lifecycle_state=(LifecycleState.TRAILING_ACTIVE.value
                                 if protection_mode is ProtectionMode.TRAILING_ONLY
                                 else LifecycleState.PROTECTION_ACTIVE.value),
                order_list_id=list_id, take_profit_order_id=tp_id,
                stop_order_id=(stop_id or (int(response.get("orderId", 0) or 0) or None)),
                protected_quantity=str(protected or row.get("protected_quantity") or "0"),
                protection_mode=protection_mode.value,
                trailing_delta=(request.get("trailingDelta")
                                or request.get("belowTrailingDelta")),
            )
        self.state_store.upsert_trade(trade_id, row["pair"], **fields)
        self._record_response_events(response, order_list_id=list_id)
        self.state_store.replay_exchange_events_for_trade(trade_id)

    def _preflight(self, rules: SymbolRules, endpoint: str, params: dict, *,
                   replacing_order_ids=(), replacing_order_list=False):
        return self.filter_validator.validate_replacement(
            rules.symbol, endpoint, params,
            open_orders_provider=lambda symbol: self.gateway.open_orders(symbol),
            all_open_orders_provider=lambda: self.gateway.open_orders(),
            open_order_lists_provider=self.gateway.open_order_lists,
            account_provider=self.gateway.account,
            replacing_order_ids=replacing_order_ids,
            replacing_order_list=replacing_order_list,
        )

    def _lookup(self, endpoint: str, symbol: str, params: dict):
        try:
            if endpoint.startswith("orderList/"):
                return self.gateway.get_order_list(list_client_id=str(params.get("listClientOrderId", "")))
            return self.gateway.get_order(symbol, client_id=str(params.get("newClientOrderId", "")))
        except Exception:
            return None

    def _submit_once(self, *, operation: str, trade_id: str, symbol: str,
                     endpoint: str, params: dict, parent_intent_id: str = ""):
        list_client_id = str(params.get("listClientOrderId", ""))
        client_id = str(params.get("newClientOrderId") or params.get("workingClientOrderId") or "")
        seed = json.dumps([operation, trade_id, symbol, endpoint, list_client_id, client_id],
                          separators=(",", ":"))
        intent_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        if not self.state_store.prepare_intent(
            intent_id=intent_id, operation=operation, trade_id=trade_id,
            symbol=symbol, endpoint=endpoint, request=params,
            client_order_id=client_id, list_client_order_id=list_client_id,
            parent_intent_id=parent_intent_id,
            close_parent_intent_id=parent_intent_id):
            prior = self.state_store.intent(intent_id)
            return str(prior.get("state")), None, "existing operation intent", intent_id
        self.state_store.mark_intent_submitting(intent_id)
        try:
            response = self.gateway.place(endpoint, params)
        except Exception as exc:
            found = self._lookup(endpoint, symbol, params)
            if isinstance(found, dict) and (found.get("orderListId") or found.get("orderId")):
                response = found
            elif self.gateway.classify_submission_error(exc) == SubmissionClass.DEFINITE_REJECT:
                safe_error = redact_text(exc)
                self.state_store.finish_intent(intent_id, "DEFINITE_REJECT", error=safe_error)
                return "DEFINITE_REJECT", None, safe_error, intent_id
            else:
                safe_error = redact_text(exc)
                self.state_store.finish_intent(intent_id, "AMBIGUOUS", error=safe_error)
                self._pause("ambiguous-order-submission-reconcile-required")
                return "AMBIGUOUS", None, safe_error, intent_id
        identifier = response.get("orderListId") if endpoint.startswith("orderList/") else response.get("orderId")
        if not identifier:
            found = self._lookup(endpoint, symbol, params)
            if isinstance(found, dict):
                response = found
                identifier = response.get("orderListId") if endpoint.startswith("orderList/") else response.get("orderId")
        if not identifier:
            self.state_store.finish_intent(intent_id, "AMBIGUOUS", error="malformed success response")
            self._pause("ambiguous-order-submission-reconcile-required")
            return "AMBIGUOUS", None, "success response had no exchange identifier", intent_id
        self.state_store.finish_intent(
            intent_id, "CONFIRMED",
            exchange_order_id=response.get("orderId"),
            exchange_order_list_id=response.get("orderListId"),
        )
        return "CONFIRMED", response, "confirmed", intent_id

    @staticmethod
    def _terminal_order_status(response: dict) -> bool:
        return str(response.get("status", "")).upper() in {
            "FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED",
        }

    @staticmethod
    def _terminal_list_status(response: dict) -> bool:
        return (
            str(response.get("listStatusType", "")).upper() == "ALL_DONE"
            or str(response.get("listOrderStatus", "")).upper() == "ALL_DONE"
        )

    @staticmethod
    def _intent_request(intent: dict) -> dict:
        request_text = str(intent.get("request_json") or "")
        if hashlib.sha256(request_text.encode("utf-8")).hexdigest() != str(
            intent.get("request_sha256") or ""
        ):
            raise RuntimeError("durable intent request hash mismatch")
        request = json.loads(request_text)
        if not isinstance(request, dict):
            raise RuntimeError("durable intent request is not an object")
        return request

    def _resume_existing_prepared_submission(self, intent: dict, request: dict):
        """Cross the POST boundary once for a child proven never submitted."""
        intent_id = str(intent["intent_id"])
        endpoint = str(intent["endpoint"])
        symbol = str(intent["symbol"])
        self.state_store.mark_intent_submitting(intent_id)
        try:
            response = self.gateway.place(endpoint, request)
        except Exception as exc:
            found = self._lookup(endpoint, symbol, request)
            if isinstance(found, dict) and (found.get("orderListId") or found.get("orderId")):
                response = found
            elif self.gateway.classify_submission_error(exc) == SubmissionClass.DEFINITE_REJECT:
                self.state_store.finish_intent(intent_id, "DEFINITE_REJECT", error=redact_text(exc))
                return "DEFINITE_REJECT", None
            else:
                self.state_store.finish_intent(intent_id, "AMBIGUOUS", error=redact_text(exc))
                return "AMBIGUOUS", None
        identifier = (
            response.get("orderListId") if endpoint.startswith("orderList/")
            else response.get("orderId")
        )
        if not identifier:
            self.state_store.finish_intent(
                intent_id, "AMBIGUOUS", error="resumed POST returned no exchange identifier"
            )
            return "AMBIGUOUS", None
        self.state_store.finish_intent(
            intent_id, "CONFIRMED", exchange_order_id=response.get("orderId"),
            exchange_order_list_id=response.get("orderListId"),
        )
        return "CONFIRMED", response

    def _resume_prepared_replacement(self, plan: dict) -> bool:
        """Repair the only safe PREPARED crash point after a confirmed cancel."""
        request = self._intent_request(plan)
        trade_id = str(plan.get("trade_id") or "")
        related = self.state_store.intents_for_trade(trade_id)
        cancels = [item for item in related if item.get("operation") == "CANCEL_PROTECTION"]
        if not cancels:
            self.state_store.finish_intent(
                plan["intent_id"], "ABORTED",
                error="replacement plan ended before any cancel intent was prepared",
            )
            return True
        cancel = cancels[-1]
        if cancel.get("state") != "CONFIRMED":
            return False
        cancel_request = self._intent_request(cancel)
        symbol = str(plan.get("symbol") or "")
        if cancel.get("endpoint") == "DELETE/orderList":
            terminal = self.gateway.get_order_list(
                order_list_id=cancel_request.get("orderListId")
            )
            if not self._terminal_list_status(terminal):
                return False
        else:
            terminal = self.gateway.get_order(
                symbol, order_id=cancel_request.get("orderId")
            )
            if not self._terminal_order_status(terminal):
                return False

        row = self.state_store.trade(trade_id)
        if not row:
            raise RuntimeError("replacement plan has no durable trade")
        active = self.pair_controller.load()
        rules = self._rules(active["pair"])

        children = [
            item for item in related
            if str(item.get("parent_intent_id") or "") == str(plan["intent_id"])
        ]
        if children:
            child = children[-1]
            if child.get("state") == "PREPARED":
                child_request = self._intent_request(child)
                self._preflight(rules, str(child["endpoint"]), child_request)
                state, response = self._resume_existing_prepared_submission(
                    child, child_request
                )
                if state == "CONFIRMED" and isinstance(response, dict):
                    rebound = self.state_store.intent(str(child["intent_id"]))
                    self._bind_confirmed_intent(rebound, response, child_request)
                    mode = (
                        ProtectionMode.TRAILING_ONLY if str(child["endpoint"]) == "order"
                        else ProtectionMode.OCO_TRAILING
                        if child_request.get("belowTrailingDelta") is not None
                        else ProtectionMode.FIXED_OCO
                    )
                    self.state_store.upsert_trade(
                        trade_id, row["pair"],
                        lifecycle_state=(LifecycleState.TRAILING_ACTIVE.value
                                         if mode is ProtectionMode.TRAILING_ONLY
                                         else LifecycleState.PROTECTION_ACTIVE.value),
                        protection_mode=mode.value,
                        reconciliation_status="CRASHED_PREPARED_CHILD_RESUMED_AND_CONFIRMED",
                    )
                self.state_store.finish_intent(
                    plan["intent_id"], "ABORTED",
                    error=f"prepared replacement child resumed with state {state}",
                )
                return True
            if child.get("state") == "CONFIRMED":
                if str(child.get("endpoint", "")).startswith("orderList/"):
                    response = self.gateway.get_order_list(
                        list_client_id=str(child.get("list_client_order_id") or "")
                    )
                else:
                    response = self.gateway.get_order(
                        symbol, client_id=str(child.get("client_order_id") or "")
                    )
                child_request = self._intent_request(child)
                self._bind_confirmed_intent(child, response, child_request)
                mode = (
                    ProtectionMode.TRAILING_ONLY if str(child["endpoint"]) == "order"
                    else ProtectionMode.OCO_TRAILING
                    if child_request.get("belowTrailingDelta") is not None
                    else ProtectionMode.FIXED_OCO
                )
                self.state_store.upsert_trade(
                    trade_id, row["pair"],
                    lifecycle_state=(LifecycleState.TRAILING_ACTIVE.value
                                     if mode is ProtectionMode.TRAILING_ONLY
                                     else LifecycleState.PROTECTION_ACTIVE.value),
                    protection_mode=mode.value,
                    reconciliation_status="CRASHED_CONFIRMED_CHILD_REBOUND",
                )
                self.state_store.finish_intent(
                    plan["intent_id"], "ABORTED",
                    error="replacement child was already confirmed and rebound",
                )
                return True
            if child.get("state") in {"SUBMITTING", "AMBIGUOUS"}:
                return False
            self.state_store.finish_intent(
                plan["intent_id"], "ABORTED",
                error=f"replacement child ended {child.get('state')}; no blind retry",
            )
            return True

        requested_qty = Decimal(str(request.get("quantity") or "0"))
        if not requested_qty.is_finite() or requested_qty <= 0:
            raise RuntimeError("replacement plan quantity is invalid")
        held = floor_step(self._free("BTC"), rules.step)
        if held <= 0:
            balances = self._balances(self.gateway.account())
            btc_free, btc_locked = balances.get("BTC", (Decimal(0), Decimal(0)))
            if floor_step(Decimal(btc_free) + Decimal(btc_locked), rules.step) <= 0:
                self.state_store.finish_intent(
                    plan["intent_id"], "ABORTED",
                    error="position exited before crashed replacement could resume",
                )
                self.state_store.upsert_trade(
                    trade_id, row["pair"], lifecycle_state=LifecycleState.EXIT_FILLED.value,
                    reconciliation_status="EXIT_VERIFIED_DURING_REPLACEMENT_RECOVERY",
                )
                return True
            raise RuntimeError("BTC remains locked while replacement recovery is pending")
        submission_request = dict(request)
        if held < floor_step(requested_qty, rules.step):
            # Preserve every prevalidated price/type/client ID while reducing
            # only the sell quantity to the positively observed free balance.
            submission_request["quantity"] = decimal_text(held)
        self._preflight(rules, str(plan["endpoint"]), submission_request)
        status, response, detail, child_id = self._submit_once(
            operation="PROTECTION", trade_id=trade_id, symbol=symbol,
            endpoint=str(plan["endpoint"]), params=submission_request,
            parent_intent_id=str(plan["intent_id"]),
        )
        self.state_store.finish_intent(
            plan["intent_id"], "ABORTED",
            error=f"replacement plan resumed into child intent {child_id}: {status}",
        )
        if status == "CONFIRMED" and isinstance(response, dict):
            child = self.state_store.intent(child_id)
            self._bind_confirmed_intent(child, response, submission_request)
            mode = (
                ProtectionMode.TRAILING_ONLY if str(plan["endpoint"]) == "order"
                else ProtectionMode.OCO_TRAILING if request.get("belowTrailingDelta") is not None
                else ProtectionMode.FIXED_OCO
            )
            self.state_store.upsert_trade(
                trade_id, row["pair"],
                lifecycle_state=(LifecycleState.TRAILING_ACTIVE.value
                                 if mode is ProtectionMode.TRAILING_ONLY
                                 else LifecycleState.PROTECTION_ACTIVE.value),
                protection_mode=mode.value,
                reconciliation_status="CRASHED_REPLACEMENT_RESUMED_AND_CONFIRMED",
            )
        return True

    def _resolve_operation_intents(self) -> dict:
        """Resolve may-have-landed requests by GET only; never repeat a POST."""
        resolved, errors = 0, []
        for intent in self.state_store.unresolved_intents():
            intent_id = str(intent["intent_id"])
            try:
                request = self._intent_request(intent)

                if str(intent.get("state")) == "PREPARED":
                    if str(intent.get("operation")).upper() == "REPLACE_PROTECTION":
                        continue
                    if (str(intent.get("operation")).upper() == "PROTECTION"
                            and intent.get("parent_intent_id")):
                        row = self.state_store.trade(str(intent.get("trade_id") or ""))
                        if not row:
                            raise RuntimeError("prepared protection child has no durable trade")
                        rules = self._rules(row["pair"])
                        self._preflight(rules, str(intent["endpoint"]), request)
                        state, response = self._resume_existing_prepared_submission(
                            intent, request
                        )
                        if state == "CONFIRMED" and isinstance(response, dict):
                            confirmed = self.state_store.intent(intent_id)
                            self._bind_confirmed_intent(confirmed, response, request)
                            mode = (
                                ProtectionMode.TRAILING_ONLY
                                if str(intent["endpoint"]) == "order"
                                else ProtectionMode.OCO_TRAILING
                                if request.get("belowTrailingDelta") is not None
                                else ProtectionMode.FIXED_OCO
                            )
                            self.state_store.upsert_trade(
                                row["trade_id"], row["pair"],
                                lifecycle_state=(LifecycleState.TRAILING_ACTIVE.value
                                                 if mode is ProtectionMode.TRAILING_ONLY
                                                 else LifecycleState.PROTECTION_ACTIVE.value),
                                protection_mode=mode.value,
                                reconciliation_status="PREPARED_REPLACEMENT_CHILD_RESUMED",
                            )
                        resolved += 1
                        continue
                    # PREPARED is durably before the may-have-landed boundary.
                    self.state_store.finish_intent(
                        intent_id, "ABORTED", error="startup proved POST was never begun"
                    )
                    if str(intent.get("operation")).upper() == "ENTRY" and intent.get("trade_id"):
                        row = self.state_store.trade(str(intent["trade_id"]))
                        if row:
                            self.state_store.upsert_trade(
                                row["trade_id"], row["pair"],
                                lifecycle_state=LifecycleState.ENTRY_REJECTED.value,
                                reconciliation_status="ENTRY_PREPARED_ABORTED_BEFORE_POST",
                            )
                    resolved += 1
                    continue

                endpoint = str(intent.get("endpoint") or "")
                symbol = str(intent.get("symbol") or "")
                if endpoint == "DELETE/orderList":
                    response = self.gateway.get_order_list(
                        order_list_id=request.get("orderListId")
                    )
                    if self._terminal_list_status(response):
                        state, reason = "CONFIRMED", "cancel terminal state confirmed by GET"
                    elif str(response.get("listOrderStatus", "")).upper() == "EXECUTING":
                        raise RuntimeError("order list remains active; cancel outcome not terminal")
                    else:
                        raise RuntimeError("order-list cancel lookup returned an unproven state")
                    self.state_store.finish_intent(
                        intent_id, state,
                        exchange_order_list_id=response.get("orderListId"), error=reason,
                    )
                    resolved += 1
                    continue
                if endpoint == "DELETE/order":
                    response = self.gateway.get_order(
                        symbol, order_id=request.get("orderId")
                    )
                    if self._terminal_order_status(response):
                        state, reason = "CONFIRMED", "cancel terminal state confirmed by GET"
                    elif str(response.get("status", "")).upper() in {"NEW", "PARTIALLY_FILLED"}:
                        raise RuntimeError("order remains active; cancel outcome not terminal")
                    else:
                        raise RuntimeError("order cancel lookup returned an unproven state")
                    self.state_store.finish_intent(
                        intent_id, state, exchange_order_id=response.get("orderId"), error=reason,
                    )
                    resolved += 1
                    continue

                if endpoint.startswith("orderList/"):
                    response = self.gateway.get_order_list(
                        list_client_id=str(intent.get("list_client_order_id") or "")
                    )
                    identifier = response.get("orderListId") if isinstance(response, dict) else None
                elif endpoint == "order":
                    response = self.gateway.get_order(
                        symbol, client_id=str(intent.get("client_order_id") or "")
                    )
                    identifier = response.get("orderId") if isinstance(response, dict) else None
                else:
                    raise RuntimeError(f"unsupported durable intent endpoint: {endpoint}")
                if not identifier:
                    raise RuntimeError("client-ID lookup returned no exchange identifier")
                self.state_store.finish_intent(
                    intent_id, "CONFIRMED",
                    exchange_order_id=response.get("orderId"),
                    exchange_order_list_id=response.get("orderListId"),
                    error="confirmed by deterministic client-ID GET",
                )
                confirmed = self.state_store.intent(intent_id)
                self._bind_confirmed_intent(confirmed, response, request)
                resolved += 1
            except Exception as exc:
                # A failed/negative lookup is not proof that a matching-engine
                # request did not land. Keep the intent unresolved and entries off.
                errors.append({"intent_id": intent_id, "error": type(exc).__name__})
        for plan in [
            item for item in self.state_store.unresolved_intents()
            if item.get("state") == "PREPARED"
            and item.get("operation") == "REPLACE_PROTECTION"
        ]:
            try:
                if self._resume_prepared_replacement(plan):
                    resolved += 1
            except Exception as exc:
                errors.append({"intent_id": str(plan["intent_id"]),
                               "error": type(exc).__name__})
        return {"resolved": resolved, "remaining": len(self.state_store.unresolved_intents()),
                "errors": errors}

    def _pause(self, reason: str):
        self.enabled = False
        self.state_store.set_entries(False, reason)
        try:
            self.guard.set_global_pause(reason)
        except Exception:
            pass

    # ---- lifecycle ----
    def start(self):
        state = self.pair_controller.load()
        rules = self._rules(state["pair"])
        self._require_mode_capabilities(
            rules,
            ProtectionMode(self.state_store.get_mode()),
            for_entry=True,
        )
        self.state_store.register_symbol_pair(state["symbol"], state["pair"])
        # Startup reconciliation is an execution-entry interlock, not a reason
        # to overwrite a durable risk pause restored from SQLite. Keep entries
        # off while preserving any independent owner/risk halt verbatim.
        self.enabled = False
        self.state_store.set_entries(False, "startup-reconciliation-required")
        self.stream = self.stream_factory(
            self.gateway, self._on_order_update, self._on_list_update,
            self._on_resync, testnet=self.mode == "testnet")
        self.stream.start()
        result = self.verified_reconcile()
        if not result["ok"]:
            raise RuntimeError(result["detail"])
        self.started = True
        audit("bitcoin_spot_adapter_started", details={
            "mode": self.mode, "pair": state["pair"], "entries": "off"})
        return True

    def set_enabled(self, on: bool):
        if on:
            if self.mode == "live" and self.state_store.data.get("live_restart_required", False):
                return "OFF: live pair/policy changed; fresh signed evidence and restart required"
            risk_pause = str(
                getattr(getattr(self, "guard", None), "state", {}).get("global_pause", "")
            )
            if risk_pause:
                return "OFF: global risk pause: " + risk_pause
            result = self.verified_reconcile()
            if not result["ok"]:
                return "OFF: reconciliation failed: " + result["detail"]
            if self.state_store.unresolved_intents():
                return "OFF: unresolved operation intent"
        self.enabled = bool(on)
        return "ON" if self.enabled else "OFF"

    def submit(self, symbol: str, note: str = "", *, trade_id: str | None = None):
        with self.lock:
            if not self.started or not self.enabled or not self.state_store.entries():
                return False, "entries are not armed"
            active = self.pair_controller.load()
            symbol = str(symbol).upper()
            if symbol != active["symbol"]:
                return False, "signal symbol is not the active BTC pair"
            trade_id = str(trade_id or "")
            if not trade_id:
                return False, "trade_id is required for intent-first execution"
            other = [row for row in self.state_store.active_trade_rows() if row["trade_id"] != trade_id]
            if other:
                return False, "another BTC exposure is nonterminal"
            if self.state_store.unresolved_intents():
                return False, "an operation intent is unresolved"
            rules = self._rules(active["pair"])
            quote_free = self._free(rules.quote)
            if quote_free < self.trade_size_quote:
                return False, f"insufficient {rules.quote} balance"
            entry = floor_step(self._best_ask(symbol), rules.tick)
            quantity = floor_step(self.trade_size_quote / entry, rules.step)
            mode = ProtectionMode(self.state_store.get_mode())
            self._require_mode_capabilities(rules, mode, for_entry=True)
            endpoint, params = self.factory.entry(mode, rules, quantity, entry)
            self._preflight(rules, endpoint, params)
            self.state_store.upsert_trade(
                trade_id, active["pair"], lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
                entry_client_order_id=params.get("workingClientOrderId"),
                entry_submitted_at=str(time.time()),
                protection_mode=self.state_store.get_mode(),
                trailing_delta=self.factory.settings.trailing_delta_bips,
                reconciliation_status="ENTRY_INTENT_PREPARED",
            )
            status, response, detail, intent_id = self._submit_once(
                operation="ENTRY", trade_id=trade_id, symbol=symbol,
                endpoint=endpoint, params=params)
            if status == "DEFINITE_REJECT":
                self.state_store.upsert_trade(
                    trade_id, active["pair"], lifecycle_state=LifecycleState.ENTRY_REJECTED.value,
                    reconciliation_status="ENTRY_DEFINITELY_REJECTED")
                return False, detail
            if status != "CONFIRMED":
                self.state_store.upsert_trade(
                    trade_id, active["pair"], lifecycle_state=LifecycleState.AMBIGUOUS.value,
                    reconciliation_status="ENTRY_AMBIGUOUS_RECONCILE_REQUIRED")
                return False, "entry outcome ambiguous; entries paused and no retry permitted"
            intent = self.state_store.intent(intent_id)
            self._bind_confirmed_intent(intent, response, params)
            list_id, _entry_id, _tp_id, _stop_id = self._response_ids(response, params)
            audit("bitcoin_entry_confirmed", details={
                "trade_id": trade_id, "symbol": symbol, "order_list_id": list_id,
                "note": note[:200]})
            return True, f"{symbol} OTO/OTOCO entry confirmed (list {list_id})"

    # ---- user stream / partial-fill recovery ----
    def _on_order_update(self, event: dict):
        try:
            self._record_authenticated_event(event)
            side, status = str(event.get("S", "")).upper(), str(event.get("X", "")).upper()
            if side == "BUY" and status in {
                "PARTIALLY_FILLED", "CANCELED", "EXPIRED", "REJECTED",
            } and Decimal(str(event.get("z") or "0")) > 0:
                self.partial_recovery.add(str(event.get("s", "")).upper())
                self._pause("partial-entry-fill-needs-protection")
        except Exception as exc:
            self._pause("user-stream-event-processing-failed")
            audit("user_stream_event_failed", severity="CRITICAL", details={"error": str(exc)})

    def _on_list_update(self, event: dict):
        try:
            self.state_store.record_exchange_event(event)
        except Exception as exc:
            self._pause("user-stream-list-processing-failed")
            audit("user_stream_list_failed", severity="CRITICAL", details={"error": str(exc)})

    def _on_resync(self):
        self.state_store.data["last_reconciliation_status"] = "RECONCILIATION_REQUIRED"
        self.state_store.data["last_reconciliation_at"] = time.time()
        self.state_store.save()
        self._pause("user-stream-reconnect-reconciliation-required")

    @staticmethod
    def _durable_protection_is_open(row: dict, orders: list, lists: list) -> bool:
        order_ids = {int(item.get("orderId", 0) or 0) for item in orders if isinstance(item, dict)}
        list_ids = {int(item.get("orderListId", 0) or 0) for item in lists if isinstance(item, dict)}
        stop_id = int(row.get("stop_order_id") or 0)
        tp_id = int(row.get("take_profit_order_id") or 0)
        if str(row.get("protection_mode") or "") == ProtectionMode.TRAILING_ONLY.value:
            return bool(stop_id and stop_id in order_ids)
        list_id = int(row.get("order_list_id") or 0)
        return bool(list_id and list_id in list_ids and stop_id in order_ids and tp_id in order_ids)

    def _cancel_entry_list(self, row: dict, symbol: str, *, operation: str) -> tuple[bool, str]:
        list_id = int(row.get("order_list_id") or 0)
        if not list_id:
            return True, "entry list is already absent"
        request = {"symbol": symbol, "orderListId": list_id}
        seed = json.dumps(
            [operation, row["trade_id"], request], sort_keys=True
        )
        intent_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        if not self.state_store.prepare_intent(
            intent_id=intent_id, operation=operation,
            trade_id=row["trade_id"], symbol=symbol, endpoint="DELETE/orderList",
            request=request,
        ):
            prior = self.state_store.intent(intent_id) or {}
            if prior.get("state") == "CONFIRMED":
                return True, "entry-list cancellation was already confirmed"
            return False, "entry-list cancellation remains unresolved"
        self.state_store.mark_intent_submitting(intent_id)
        try:
            response = self.gateway.cancel_order_list(symbol, list_id)
        except Exception as exc:
            self.state_store.finish_intent(intent_id, "AMBIGUOUS", error=redact_text(exc))
            return False, "entry-list cancellation outcome is ambiguous"
        if not isinstance(response, dict) or not self._terminal_list_status(response):
            self.state_store.finish_intent(
                intent_id, "AMBIGUOUS", error="cancel response did not prove ALL_DONE"
            )
            return False, "entry-list cancellation response was not terminal"
        self.state_store.finish_intent(
            intent_id, "CONFIRMED", exchange_order_list_id=response.get("orderListId")
        )
        return True, "partially-filled entry list canceled"

    def _place_partial_protection(self, symbol: str) -> tuple[bool, str]:
        row = self.state_store.active_trade_for_symbol(symbol)
        if not row:
            return False, "partial trade record is missing"
        active = self.pair_controller.load()
        rules = self._rules(active["pair"])
        # First query the working BUY. An OTO/OTOCO list is EXECUTING while a
        # partially-filled working leg is still open, but its pending SELL legs
        # do not activate until the BUY is fully filled.
        entry = self.gateway.get_order(
            symbol,
            order_id=row.get("entry_order_id") if row.get("entry_order_id") else None,
            client_id=str(row.get("entry_client_order_id") or ""),
        )
        if int(entry.get("orderId", 0) or 0) != int(row.get("entry_order_id") or 0):
            return False, "working entry lookup identity mismatch"
        event = self._rest_event(entry, order_list_id=row.get("order_list_id"))
        if not event:
            return False, "working entry lookup did not contain a terminal/active status"
        self._record_authenticated_event(event)
        self.state_store.replay_exchange_events_for_trade(row["trade_id"])
        row = self.state_store.trade(row["trade_id"]) or row
        status = str(entry.get("status", "")).upper()
        open_orders = self.gateway.open_orders(symbol)
        open_lists = self.gateway.open_order_lists()
        if status == "FILLED" and self._durable_protection_is_open(row, open_orders, open_lists):
            self.state_store.upsert_trade(
                row["trade_id"], active["pair"],
                lifecycle_state=LifecycleState.PROTECTION_ACTIVE.value,
                reconciliation_status="ENTRY_FILLED_AND_PENDING_PROTECTION_VERIFIED_OPEN",
            )
            return True, "entry completed and exchange protection is verified open"
        if status not in {"PARTIALLY_FILLED", "CANCELED", "EXPIRED", "REJECTED", "FILLED"}:
            return False, f"working entry status {status!r} is not recoverable"

        original_open = any(
            int(item.get("orderListId", 0) or 0) == int(row.get("order_list_id") or 0)
            for item in open_lists
        )
        if original_open:
            canceled, cancel_detail = self._cancel_entry_list(
                row, symbol, operation="CANCEL_PARTIAL_ENTRY"
            )
            if not canceled:
                return False, cancel_detail
            # The cancellation response is not enough to choose a sell quantity;
            # query the BUY again and require its final cumulative fill.
            entry = self.gateway.get_order(symbol, order_id=int(row["entry_order_id"]))
            event = self._rest_event(entry, order_list_id=row.get("order_list_id"))
            if not event or not self._terminal_order_status(entry):
                return False, "working entry is not terminal after list cancellation"
            self._record_authenticated_event(event)
            self.state_store.replay_exchange_events_for_trade(row["trade_id"])
            row = self.state_store.trade(row["trade_id"]) or row
        elif not self._terminal_order_status(entry):
            return False, (
                "working BUY is still active while its order list is absent; "
                "standalone protection would create a double-sell race"
            )
        held = self._free("BTC")
        filled = Decimal(str(row.get("filled_quantity") or "0"))
        qty = floor_step(min(held, filled), rules.step)
        if qty <= 0:
            return False, "no sellable BTC quantity for partial-fill protection"
        entry_price = Decimal(str(row.get("average_entry_price") or self._current(symbol)))
        current = self._current(symbol)
        mode = ProtectionMode(self.state_store.get_mode())
        self._require_mode_capabilities(rules, mode, for_entry=False)
        endpoint, params = self.factory.replacement(
            mode, rules, qty, entry_price, current)
        self._preflight(rules, endpoint, params)
        status, response, detail, intent_id = self._submit_once(
            operation="PARTIAL_FILL_PROTECTION", trade_id=row["trade_id"], symbol=symbol,
            endpoint=endpoint, params=params)
        if status != "CONFIRMED":
            return False, "partial-fill protection not confirmed: " + detail
        self._bind_confirmed_intent(self.state_store.intent(intent_id), response, params)
        self.state_store.upsert_trade(
            row["trade_id"], active["pair"],
            reconciliation_status="PARTIAL_FILL_PROTECTED")
        return True, f"partial fill protected for {qty} BTC"

    def _expire_stale_entry(self, row: dict) -> tuple[bool, str]:
        symbol = str(row["pair"]).replace("/", "")
        submitted = float(row.get("entry_submitted_at") or "nan")
        if not math.isfinite(submitted):
            return False, "entry submission time is missing or malformed"
        if time.time() - submitted <= self.max_entry_open_seconds:
            return True, "entry remains within its bounded open window"
        entry = self.gateway.get_order(
            symbol,
            order_id=row.get("entry_order_id") if row.get("entry_order_id") else None,
            client_id=str(row.get("entry_client_order_id") or ""),
        )
        if int(entry.get("orderId", 0) or 0) != int(row.get("entry_order_id") or 0):
            return False, "stale entry lookup identity mismatch"
        event = self._rest_event(entry, order_list_id=row.get("order_list_id"))
        if not event:
            return False, "stale entry lookup returned no status"
        self._record_authenticated_event(event)
        executed = Decimal(str(entry.get("executedQty") or "0"))
        if not executed.is_finite() or executed < 0:
            return False, "stale entry executed quantity is malformed"
        status = str(entry.get("status", "")).upper()
        if not self._terminal_order_status(entry):
            canceled, detail = self._cancel_entry_list(
                row, symbol, operation="CANCEL_STALE_ENTRY"
            )
            if not canceled:
                return False, detail
            entry = self.gateway.get_order(symbol, order_id=int(row["entry_order_id"]))
            if not self._terminal_order_status(entry):
                return False, "stale working BUY is not terminal after cancellation"
            event = self._rest_event(entry, order_list_id=row.get("order_list_id"))
            if not event:
                return False, "terminal stale entry lookup returned no status"
            self._record_authenticated_event(event)
            executed = Decimal(str(entry.get("executedQty") or "0"))
            if not executed.is_finite() or executed < 0:
                return False, "terminal stale entry quantity is malformed"
            status = str(entry.get("status", "")).upper()
        if executed > 0 or status == "FILLED":
            self.partial_recovery.add(symbol)
            self._pause("stale-entry-fill-needs-protection")
            return self._place_partial_protection(symbol)
        current = self.state_store.trade(row["trade_id"])
        if current:
            self.state_store.upsert_trade(
                row["trade_id"], row["pair"],
                lifecycle_state=LifecycleState.ENTRY_REJECTED.value,
                reconciliation_status="STALE_ENTRY_CANCELED_WITH_NO_FILL",
            )
        return True, "stale unfilled entry canceled and verified terminal"

    def tick(self):
        for row in self.state_store.active_trade_rows():
            if row["lifecycle_state"] == LifecycleState.ENTRY_SUBMITTED.value:
                try:
                    ok, detail = self._expire_stale_entry(row)
                    if not ok:
                        self._pause("stale-entry-cancellation-unresolved")
                        audit("stale_entry_cancellation_failed", severity="CRITICAL",
                              details={"trade_id": row["trade_id"], "detail": detail})
                except Exception as exc:
                    self._pause("stale-entry-cancellation-unresolved")
                    audit("stale_entry_cancellation_failed", severity="CRITICAL",
                          details={"trade_id": row["trade_id"], "error": str(exc)})
        candidates = set(self.partial_recovery)
        for row in self.state_store.active_trade_rows():
            if row["lifecycle_state"] == LifecycleState.ENTRY_PARTIALLY_FILLED.value:
                candidates.add(row["pair"].replace("/", ""))
        for symbol in candidates:
            try:
                ok, detail = self._place_partial_protection(symbol)
                if ok:
                    self.partial_recovery.discard(symbol)
                else:
                    audit("partial_fill_protection_failed", severity="CRITICAL",
                          details={"symbol": symbol, "detail": detail})
            except Exception as exc:
                audit("partial_fill_protection_failed", severity="CRITICAL",
                      details={"symbol": symbol, "error": str(exc)})

    # ---- cancel-and-replace protection ----
    def _cancel_protection(self, row: dict, symbol: str) -> tuple[bool, str]:
        if not row.get("order_list_id") and not row.get("stop_order_id"):
            return False, "no recorded protection identifier"
        request = {"symbol": symbol, "orderListId": row.get("order_list_id"),
                   "orderId": row.get("stop_order_id")}
        seed = json.dumps(["CANCEL_PROTECTION", row["trade_id"], request], sort_keys=True, default=str)
        intent_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        if not self.state_store.prepare_intent(
            intent_id=intent_id, operation="CANCEL_PROTECTION", trade_id=row["trade_id"],
            symbol=symbol, endpoint="DELETE/orderList" if row.get("order_list_id") else "DELETE/order",
            request=request):
            prior = self.state_store.intent(intent_id) or {}
            if prior.get("state") == "CONFIRMED":
                try:
                    if row.get("order_list_id"):
                        observed = self.gateway.get_order_list(
                            order_list_id=int(row["order_list_id"])
                        )
                        if (int(observed.get("orderListId", 0) or 0)
                                == int(row["order_list_id"])
                                and self._terminal_list_status(observed)):
                            return True, "old protection cancellation already confirmed"
                    else:
                        observed = self.gateway.get_order(
                            symbol, order_id=int(row["stop_order_id"])
                        )
                        if (int(observed.get("orderId", 0) or 0)
                                == int(row["stop_order_id"])
                                and self._terminal_order_status(observed)):
                            return True, "old protection cancellation already confirmed"
                except Exception:
                    pass
            return False, "cancel intent already exists but terminal exchange state is unproven"
        self.state_store.mark_intent_submitting(intent_id)
        try:
            if row.get("order_list_id"):
                response = self.gateway.cancel_order_list(symbol, int(row["order_list_id"]))
            else:
                response = self.gateway.cancel_order(symbol, int(row["stop_order_id"]))
        except Exception as exc:
            # A failed cancel must not be followed by a replacement unless the
            # old protection's terminal state is positively known.
            self.state_store.finish_intent(intent_id, "AMBIGUOUS", error=redact_text(exc))
            return False, "cancel outcome is ambiguous; replacement blocked"
        if not isinstance(response, dict):
            self.state_store.finish_intent(
                intent_id, "AMBIGUOUS", error="cancel response is not an object"
            )
            return False, "cancel response did not prove terminal identity"
        if row.get("order_list_id"):
            valid = (
                int(response.get("orderListId", 0) or 0) == int(row["order_list_id"])
                and self._terminal_list_status(response)
            )
        else:
            valid = (
                int(response.get("orderId", 0) or 0) == int(row["stop_order_id"])
                and self._terminal_order_status(response)
            )
        if not valid:
            self.state_store.finish_intent(
                intent_id, "AMBIGUOUS",
                error="cancel response identity/status was not terminal",
            )
            return False, "cancel response did not prove requested order terminal"
        self.state_store.finish_intent(intent_id, "CONFIRMED",
            exchange_order_id=response.get("orderId"),
            exchange_order_list_id=response.get("orderListId"))
        return True, "old protection canceled"

    def convert(self, symbol: str, mode: ProtectionMode, *, break_even=False,
                lock_profit_pct=None, trailing_delta_bips=None):
        with self.lock:
            symbol = str(symbol).upper()
            row = self.state_store.active_trade_for_symbol(symbol)
            if not row:
                return False, "position not found"
            self._pause("protection-conversion-in-progress")
            active = self.pair_controller.load()
            rules = self._rules(active["pair"])
            entry = Decimal(str(row.get("average_entry_price") or "0"))
            current = self._current(symbol)
            qty = Decimal(str(row.get("protected_quantity") or row.get("filled_quantity") or "0"))
            qty = floor_step(qty, rules.step)
            if entry <= 0 or qty <= 0:
                return False, "entry price or protected quantity is unavailable"
            stop_price = None
            if break_even or lock_profit_pct is not None:
                break_even_price = fee_adjusted_break_even(
                    entry, buy_fee_pct=self.factory.settings.fee_pct_per_side,
                    sell_fee_pct=self.factory.settings.fee_pct_per_side,
                    slippage_pct=Decimal(str(env_float("BREAK_EVEN_SLIPPAGE_PCT", 0.05, 0, 5))))
                stop_price = break_even_price
                if lock_profit_pct is not None:
                    stop_price = max(stop_price, entry * (1 + Decimal(str(abs(float(lock_profit_pct)))) / 100))
                stop_price = floor_step(stop_price, rules.tick)
                if not entry < stop_price < current:
                    return False, "profit-lock stop must be above entry and below current price"
                mode = ProtectionMode.FIXED_OCO
            self._require_mode_capabilities(
                rules,
                ProtectionMode(mode),
                for_entry=False,
            )
            endpoint, params = self.factory.replacement(
                ProtectionMode(mode), rules, qty, entry, current,
                stop_price=stop_price, trailing_delta_bips=trailing_delta_bips)
            replacing_ids = (row.get("take_profit_order_id"), row.get("stop_order_id"))
            self._preflight(rules, endpoint, params, replacing_order_ids=replacing_ids,
                            replacing_order_list=bool(row.get("order_list_id")))
            # Persist the exact replacement before cancellation. PREPARED means
            # no replacement POST has occurred; it still blocks flatness/restart.
            plan_seed = json.dumps(["REPLACE_PROTECTION", row["trade_id"], params],
                                   sort_keys=True, default=str)
            plan_id = hashlib.sha256(plan_seed.encode()).hexdigest()[:32]
            if not self.state_store.prepare_intent(
                intent_id=plan_id, operation="REPLACE_PROTECTION", trade_id=row["trade_id"],
                symbol=symbol, endpoint=endpoint, request=params,
                client_order_id=str(params.get("newClientOrderId", "")),
                list_client_order_id=str(params.get("listClientOrderId", ""))):
                return False, "replacement intent already exists"
            canceled, detail = self._cancel_protection(row, symbol)
            if not canceled:
                # Keep the PREPARED repair plan durable. If the cancel actually
                # landed but its response was lost, restart reconciliation can
                # prove the terminal old order and resume this exact plan.
                return False, detail
            held = floor_step(self._free("BTC"), rules.step)
            if held <= 0:
                balances = self._balances(self.gateway.account())
                btc_free, btc_locked = balances.get("BTC", (Decimal(0), Decimal(0)))
                if floor_step(Decimal(btc_free) + Decimal(btc_locked), rules.step) > 0:
                    return False, "BTC remains locked after cancellation; replacement plan retained"
                self.state_store.finish_intent(plan_id, "ABORTED",
                                               error="position exited during protection cancellation")
                self.state_store.upsert_trade(row["trade_id"], active["pair"],
                    lifecycle_state=LifecycleState.EXIT_FILLED.value,
                    reconciliation_status="EXITED_DURING_PROTECTION_CONVERSION")
                return True, "position exited while old protection was canceled; no replacement placed"
            if held < qty:
                endpoint, params = self.factory.replacement(
                    ProtectionMode(mode), rules, held, entry, self._current(symbol),
                    stop_price=stop_price, trailing_delta_bips=trailing_delta_bips)
            # The first preflight happened before cancellation. Refresh every
            # exchange filter and referencePrice again immediately before the
            # replacement submission, even when the quantity did not change.
            self._preflight(rules, endpoint, params)
            status, response, result_detail, intent_id = self._submit_once(
                operation="PROTECTION", trade_id=row["trade_id"], symbol=symbol,
                endpoint=endpoint, params=params, parent_intent_id=plan_id)
            if status != "CONFIRMED":
                self.state_store.upsert_trade(row["trade_id"], active["pair"],
                    lifecycle_state=LifecycleState.AMBIGUOUS.value,
                    reconciliation_status="PROTECTION_REPLACEMENT_UNCONFIRMED")
                return False, "replacement unconfirmed; no blind second sell: " + result_detail
            self._bind_confirmed_intent(self.state_store.intent(intent_id), response, params)
            list_id, _entry_id, tp_id, stop_id = self._response_ids(response, params)
            if endpoint == "order":
                stop_id = int(response.get("orderId", 0) or 0) or None
            lifecycle = (LifecycleState.BREAK_EVEN_ARMED.value if break_even else
                         LifecycleState.PROFIT_LOCKED.value if lock_profit_pct is not None else
                         LifecycleState.TRAILING_ACTIVE.value if ProtectionMode(mode) in {
                             ProtectionMode.TRAILING_ONLY, ProtectionMode.OCO_TRAILING} else
                         LifecycleState.PROTECTION_ACTIVE.value)
            self.state_store.upsert_trade(
                row["trade_id"], active["pair"], lifecycle_state=lifecycle,
                order_list_id=list_id, take_profit_order_id=tp_id, stop_order_id=stop_id,
                protected_quantity=str(min(held, qty)), protection_mode=ProtectionMode(mode).value,
                trailing_delta=trailing_delta_bips or self.factory.settings.trailing_delta_bips,
                reconciliation_status="PROTECTION_REPLACED")
            return True, f"{symbol} protection changed to {ProtectionMode(mode).value}"

    def maybe_auto_manage(self, flow: dict) -> str:
        if not (self.state_store.data.get("auto_protection_enabled", False)
                or os.getenv("AUTO_PROTECTION_ENABLED", "false").lower() == "true"):
            return "automatic protection management disabled"
        active = self.pair_controller.load()
        if not isinstance(flow, dict) or not flow.get("ok") or flow.get("pair_state_hash") != active["state_hash"]:
            return "money-flow state unavailable or belongs to another pair generation"
        try:
            generated = float(flow.get("generated_at_epoch", 0))
        except (TypeError, ValueError):
            return "money-flow timestamp is malformed"
        if not math.isfinite(generated):
            return "money-flow timestamp is malformed"
        age = time.time() - generated
        if age < -30 or age > env_int("MAX_FLOW_AGE_SECONDS", 45, 5, 300):
            return "money-flow state is stale"
        row = self.state_store.active_trade_for_symbol(active["symbol"])
        if not row or not row.get("average_entry_price"):
            return "no managed BTC position"
        entry, current = Decimal(str(row["average_entry_price"])), self._current(active["symbol"])
        trigger = Decimal(str(env_float("AUTO_BREAK_EVEN_TRIGGER_PCT", 0.5, 0.05, 20)))
        if current >= entry * (1 + trigger / 100) and row["lifecycle_state"] == LifecycleState.PROTECTION_ACTIVE.value:
            ok, detail = self.convert(active["symbol"], ProtectionMode.FIXED_OCO, break_even=True)
            return detail
        if (flow.get("classification") or {}).get("bullish") and current > entry:
            if (row.get("lifecycle_state") == LifecycleState.TRAILING_ACTIVE.value
                    or row.get("protection_mode") == ProtectionMode.TRAILING_ONLY.value):
                return "exchange-native trailing protection is already active"
            delta = env_int("AUTO_TIGHT_TRAIL_BIPS", 20, 10, 2000)
            ok, detail = self.convert(active["symbol"], ProtectionMode.TRAILING_ONLY,
                                      trailing_delta_bips=delta)
            return detail
        return "automatic protection conditions not met"

    # ---- reconciliation, account and owner controls ----
    def _reconciled_ownership(self, orders: list, lists: list, balances: dict) -> dict:
        rows = self.state_store.active_trade_rows()
        if len(rows) > 1:
            return {"ok": False, "reason": "multiple nonterminal BTC trade records"}
        active = self.pair_controller.load()
        if any(row.get("pair") != active["pair"] for row in rows):
            return {"ok": False, "reason": "nonterminal trade belongs to another pair"}

        known_order_ids = {
            int(value)
            for row in rows
            for value in (
                row.get("entry_order_id"), row.get("take_profit_order_id"),
                row.get("stop_order_id"),
            )
            if value not in (None, "", 0, "0")
        }
        known_list_ids = {
            int(row["order_list_id"])
            for row in rows
            if row.get("order_list_id") not in (None, "", 0, "0")
        }
        try:
            exchange_order_ids = {
                int(item["orderId"])
                for item in orders
                if isinstance(item, dict)
            }
            exchange_list_ids = {
                int(item["orderListId"])
                for item in lists
                if isinstance(item, dict)
            }
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "reason": "exchange order identity is malformed"}
        if len(exchange_order_ids) != len(orders):
            return {"ok": False, "reason": "exchange open-order identity is incomplete"}
        if len(exchange_list_ids) != len(lists):
            return {"ok": False, "reason": "exchange open-list identity is incomplete"}
        unknown_orders = exchange_order_ids - known_order_ids
        unknown_lists = exchange_list_ids - known_list_ids
        if unknown_orders or unknown_lists:
            return {
                "ok": False,
                "reason": "unowned exchange order or order list exists",
                "unknown_orders": len(unknown_orders),
                "unknown_lists": len(unknown_lists),
            }

        try:
            btc_free, btc_locked = balances.get("BTC", (Decimal(0), Decimal(0)))
            btc_total = Decimal(btc_free) + Decimal(btc_locked)
            if not btc_total.is_finite() or btc_total < 0:
                raise ValueError("invalid BTC total")
            rules = self._rules(active["pair"])
            rounded_total = floor_step(btc_total, rules.step)
            if not rows:
                if rounded_total > 0:
                    return {"ok": False, "reason": "unowned BTC balance exists",
                            "btc_total": str(btc_total)}
            else:
                row = rows[0]
                filled = Decimal(str(row.get("filled_quantity") or "0"))
                protected = Decimal(str(row.get("protected_quantity") or "0"))
                if any(not value.is_finite() or value < 0 for value in (filled, protected)):
                    raise ValueError("invalid recorded BTC quantity")
                owned_ceiling = floor_step(max(filled, protected), rules.step)
                owned_floor = floor_step(protected, rules.step)
                if rounded_total > owned_ceiling or rounded_total < owned_floor:
                    return {
                        "ok": False,
                        "reason": "BTC balance is outside the durable bot-owned quantity",
                        "btc_total": str(btc_total),
                        "owned_floor": str(owned_floor),
                        "owned_ceiling": str(owned_ceiling),
                    }
                lifecycle = str(row.get("lifecycle_state") or "")
                if lifecycle == LifecycleState.ENTRY_PARTIALLY_FILLED.value:
                    return {"ok": False,
                            "reason": "partially-filled entry BTC is not yet verified protected"}
                if lifecycle == LifecycleState.ENTRY_SUBMITTED.value and rounded_total > 0:
                    return {"ok": False,
                            "reason": "submitted entry has BTC exposure but no verified protection"}
                protection_states = {
                    LifecycleState.ENTRY_FILLED.value,
                    LifecycleState.PROTECTION_ACTIVE.value,
                    LifecycleState.BREAK_EVEN_ARMED.value,
                    LifecycleState.PROFIT_LOCKED.value,
                    LifecycleState.TRAILING_ACTIVE.value,
                }
                if lifecycle in protection_states:
                    if not self._durable_protection_is_open(row, orders, lists):
                        return {"ok": False,
                                "reason": "durable exchange protection is not open"}
        except (ArithmeticError, TypeError, ValueError) as exc:
            return {"ok": False, "reason": "BTC ownership state is malformed: " + redact_text(exc)}
        return {
            "ok": True,
            "active_trades": len(rows),
            "known_orders": len(known_order_ids),
            "known_lists": len(known_list_ids),
            "btc_total": str(btc_total),
        }

    def _refresh_durable_exchange_state(self) -> dict:
        """Rebuild missed lifecycle transitions from authoritative GET results."""
        refreshed, warnings = 0, []
        for original in self.state_store.active_trade_rows():
            symbol = str(original["pair"]).replace("/", "")
            queries = (
                ("entry", original.get("entry_order_id"), original.get("entry_client_order_id")),
                ("take_profit", original.get("take_profit_order_id"), ""),
                ("stop", original.get("stop_order_id"), ""),
            )
            for role, order_id, client_id in queries:
                if order_id in (None, "", 0, "0") and not client_id:
                    continue
                try:
                    response = self.gateway.get_order(
                        symbol, order_id=order_id if order_id not in (None, "", 0, "0") else None,
                        client_id=str(client_id or ""),
                    )
                    if not isinstance(response, dict) or not response.get("orderId"):
                        raise RuntimeError("order GET returned no orderId")
                    if order_id not in (None, "", 0, "0") and int(response["orderId"]) != int(order_id):
                        raise RuntimeError("order GET identity mismatch")
                    if str(response.get("symbol", symbol)).upper() != symbol:
                        raise RuntimeError("order GET symbol mismatch")
                    event = self._rest_event(response, order_list_id=original.get("order_list_id"))
                    if event:
                        self._record_authenticated_event(event)
                        refreshed += 1
                except Exception as exc:
                    warnings.append({"trade_id": original["trade_id"], "role": role,
                                     "error": type(exc).__name__})
            if original.get("order_list_id") not in (None, "", 0, "0"):
                try:
                    listing = self.gateway.get_order_list(
                        order_list_id=int(original["order_list_id"])
                    )
                    if int(listing.get("orderListId", 0) or 0) != int(original["order_list_id"]):
                        raise RuntimeError("order-list GET identity mismatch")
                    list_event = {
                        "e": "listStatus", "s": symbol,
                        "g": int(original["order_list_id"]),
                        "l": listing.get("listStatusType", ""),
                        "L": listing.get("listOrderStatus", ""),
                        "E": listing.get("transactionTime", listing.get("updateTime", 0)),
                        "u": f"REST-LIST:{original['order_list_id']}:"
                             f"{listing.get('listStatusType', '')}:"
                             f"{listing.get('listOrderStatus', '')}",
                    }
                    self.state_store.record_exchange_event(list_event)
                    for report in listing.get("orderReports") or []:
                        event = self._rest_event(report, order_list_id=original["order_list_id"])
                        if event:
                            self._record_authenticated_event(event)
                    refreshed += 1
                except Exception as exc:
                    warnings.append({"trade_id": original["trade_id"], "role": "order_list",
                                     "error": type(exc).__name__})
            self.state_store.replay_exchange_events_for_trade(original["trade_id"])
            current = self.state_store.trade(original["trade_id"])
            if current and current.get("lifecycle_state") == LifecycleState.ENTRY_PARTIALLY_FILLED.value:
                self.partial_recovery.add(symbol)
                self._pause("partial-entry-fill-needs-protection")
        return {"ok": True, "refreshed": refreshed, "warnings": warnings}

    def verified_reconcile(self) -> dict:
        endpoints = {}
        resolution = self._resolve_operation_intents()
        endpoints["intent_resolution"] = {
            "ok": resolution["remaining"] == 0,
            "resolved": resolution["resolved"],
            "remaining": resolution["remaining"],
            "errors": resolution["errors"],
        }
        endpoints["durable_status_refresh"] = self._refresh_durable_exchange_state()
        stale_results = []
        for row in self.state_store.active_trade_rows():
            if row.get("lifecycle_state") != LifecycleState.ENTRY_SUBMITTED.value:
                continue
            try:
                submitted = float(row.get("entry_submitted_at") or "nan")
                stale = not math.isfinite(submitted) or (
                    time.time() - submitted > self.max_entry_open_seconds
                )
                recovered, detail = (
                    self._expire_stale_entry(row) if stale
                    else (True, "entry remains within its bounded open window")
                )
            except Exception as exc:
                recovered, detail = False, f"{type(exc).__name__}: {redact_text(exc)}"
            stale_results.append({"trade_id": row["trade_id"], "ok": recovered,
                                  "detail": detail})
        if stale_results:
            endpoints["stale_entry_control"] = {
                "ok": all(item["ok"] for item in stale_results), "results": stale_results,
            }
        partial_results = []
        for symbol in sorted(self.partial_recovery):
            try:
                recovered, detail = self._place_partial_protection(symbol)
            except Exception as exc:
                recovered, detail = False, f"{type(exc).__name__}: {redact_text(exc)}"
            partial_results.append({"symbol": symbol, "ok": recovered, "detail": detail})
            if recovered:
                self.partial_recovery.discard(symbol)
        if partial_results:
            endpoints["partial_fill_recovery"] = {
                "ok": all(item["ok"] for item in partial_results), "results": partial_results,
            }
        try:
            orders = self.gateway.open_orders()
            if not isinstance(orders, list):
                raise RuntimeError("openOrders response is not a list")
            endpoints["openOrders"] = {"ok": True, "count": len(orders)}
        except Exception as exc:
            orders = None; endpoints["openOrders"] = {"ok": False, "error": redact_text(exc)}
        try:
            lists = self.gateway.open_order_lists()
            if not isinstance(lists, list):
                raise RuntimeError("openOrderList response is not a list")
            endpoints["openOrderList"] = {"ok": True, "count": len(lists)}
        except Exception as exc:
            lists = None; endpoints["openOrderList"] = {"ok": False, "error": redact_text(exc)}
        try:
            balances = self._balances(self.gateway.account())
            endpoints["account"] = {"ok": True, "assets": len(balances)}
        except Exception as exc:
            balances = None; endpoints["account"] = {"ok": False, "error": redact_text(exc)}
        unresolved = self.state_store.unresolved_intents()
        if unresolved:
            endpoints["intents"] = {"ok": False, "count": len(unresolved),
                                    "states": sorted({item["state"] for item in unresolved})}
        else:
            endpoints["intents"] = {"ok": True, "count": 0}
        if orders is None or lists is None or balances is None:
            endpoints["ownership"] = {
                "ok": False, "reason": "authenticated ownership enumeration is incomplete"}
        else:
            endpoints["ownership"] = self._reconciled_ownership(orders, lists, balances)
        ok = all(item["ok"] for item in endpoints.values())
        self.state_store.data["last_reconciliation_status"] = "RECONCILED" if ok else "RECONCILIATION_FAILED"
        self.state_store.data["last_reconciliation_at"] = time.time()
        self.state_store.save()
        if not ok:
            self._pause("reconciliation-failed")
        return {"ok": ok, "endpoints": endpoints,
                "detail": "all authenticated endpoints and intents verified" if ok else
                          "reconciliation incomplete; entries remain paused"}

    def reconcile(self):
        return self.verified_reconcile()["detail"]

    def verify_flat_for_switch(self, symbols: set[str]) -> dict:
        with self.lock:
            self._pause("pair-switch-verification")
            if self.state_store.active_trade_rows():
                return {"ok": False, "detail": "nonterminal trade record exists"}
            if self.state_store.unresolved_intents():
                return {"ok": False, "detail": "unresolved operation intent exists"}
            try:
                orders = self.gateway.open_orders()
                lists = self.gateway.open_order_lists()
            except Exception as exc:
                return {"ok": False, "detail":
                        "authenticated flatness enumeration failed: " + redact_text(exc)}
            if orders or lists:
                return {"ok": False, "detail":
                        "an exchange order or order list is still open"}
            # Fresh metadata validation for every target included in the report.
            for symbol in symbols:
                pair = self.state_store.data.get("symbol_pairs", {}).get(symbol)
                if pair:
                    self._rules(pair)
            balances_info = {}
            try:
                balances = self._balances(self.gateway.account())
                assets = {"BTC"}
                for symbol in symbols:
                    pair = self.state_store.data.get("symbol_pairs", {}).get(symbol)
                    if pair:
                        assets.add(pair.split("/", 1)[1])
                balances_info = {
                    asset: {
                        "free": str(balances.get(asset, (Decimal(0), Decimal(0)))[0]),
                        "locked": str(balances.get(asset, (Decimal(0), Decimal(0)))[1]),
                    }
                    for asset in sorted(assets)
                }
            except Exception:
                balances_info = {"status": "unavailable_not_used_as_pair_identity"}
            return {"ok": True, "detail": "exchange and durable state verified flat",
                    "open_orders": 0, "open_order_lists": 0,
                    "balances_informational_only": balances_info}

    def validate_pair(self, pair: str) -> dict:
        rules = self._rules(pair)
        mode = ProtectionMode(self.state_store.get_mode())
        self._require_mode_capabilities(rules, mode, for_entry=True)
        return {"pair": f"BTC/{rules.quote}", "symbol": rules.symbol,
                "base": rules.base, "quote": rules.quote,
                "oco_allowed": rules.oco_allowed, "oto_allowed": rules.oto_allowed,
                "trailing_allowed": rules.trailing_allowed,
                "entry_protection_mode": mode.value}

    def validate_pair_funding(self, pair: str) -> dict:
        """Prove the selected quote can fund and preflight one configured entry."""
        rules = self._rules(pair)
        mode = ProtectionMode(self.state_store.get_mode())
        self._require_mode_capabilities(rules, mode, for_entry=True)
        quote_free = self._free(rules.quote)
        if quote_free < self.trade_size_quote:
            raise RuntimeError(
                f"selected pair requires at least {self.trade_size_quote} "
                f"{rules.quote}; available {quote_free}"
            )
        entry = floor_step(self._best_ask(rules.symbol), rules.tick)
        quantity = floor_step(self.trade_size_quote / entry, rules.step)
        endpoint, params = self.factory.entry(
            mode,
            rules,
            quantity,
            entry,
        )
        preflight = self._preflight(rules, endpoint, params)
        return {
            "pair": f"BTC/{rules.quote}",
            "symbol": rules.symbol,
            "quote_asset": rules.quote,
            "quote_free": str(quote_free),
            "required_quote": str(self.trade_size_quote),
            "entry_protection_mode": mode.value,
            "preflight": preflight,
        }

    def emergency_exit(self, symbol: str) -> dict:
        with self.lock:
            symbol = str(symbol).upper()
            row = self.state_store.active_trade_for_symbol(symbol)
            if not row:
                return {"ok": False, "stage": "not-found", "halt_persisted": False,
                        "detail": "no nonterminal BTC position"}
            self._pause("manual-emergency-exit")
            canceled, detail = self._cancel_protection(row, symbol)
            if not canceled:
                return {"ok": False, "stage": "cancel-ambiguous", "halt_persisted": True,
                        "detail": detail}
            active = self.pair_controller.load(); rules = self._rules(active["pair"])
            filled = Decimal(str(row.get("filled_quantity") or "0"))
            protected = Decimal(str(row.get("protected_quantity") or "0"))
            if any(not value.is_finite() or value < 0 for value in (filled, protected)):
                return {"ok": False, "stage": "malformed-state", "halt_persisted": True,
                        "detail": "durable BTC ownership quantity is malformed"}
            recorded = max(filled, protected)
            free_before = floor_step(self._free("BTC"), rules.step)
            qty = floor_step(min(free_before, recorded), rules.step)
            if qty <= 0:
                return {"ok": False, "stage": "nothing-to-sell", "halt_persisted": True,
                        "detail": "no sellable BTC after protection cancellation"}
            price = floor_step(self._best_bid(symbol) * Decimal("0.99"), rules.tick)
            params = {"symbol": symbol, "side": "SELL", "type": "LIMIT",
                      "timeInForce": "IOC", "quantity": decimal_text(qty),
                      "price": decimal_text(price), "newClientOrderId": self.ids.new("EMERGENCY")}
            self._preflight(rules, "order", params)
            status, response, submit_detail, intent_id = self._submit_once(
                operation="EMERGENCY_EXIT", trade_id=row["trade_id"], symbol=symbol,
                endpoint="order", params=params)
            if status != "CONFIRMED":
                return {"ok": False, "stage": "submit-unconfirmed", "halt_persisted": True,
                        "detail": submit_detail}
            self._bind_confirmed_intent(self.state_store.intent(intent_id), response, params)
            try:
                final_order = self.gateway.get_order(
                    symbol, order_id=int(response.get("orderId", 0) or 0)
                )
                if int(final_order.get("orderId", 0) or 0) != int(response.get("orderId", 0) or 0):
                    raise RuntimeError("emergency order lookup identity mismatch")
                if not self._terminal_order_status(final_order):
                    raise RuntimeError("emergency IOC is not terminal")
                final_event = self._rest_event(final_order)
                if final_event:
                    self._record_authenticated_event(final_event)
                executed = Decimal(str(final_order.get("executedQty") or "0"))
            except Exception as exc:
                return {"ok": False, "stage": "terminal-status-unproven",
                        "halt_persisted": True, "submitted": True,
                        "detail": f"emergency IOC requires reconciliation: {type(exc).__name__}"}
            if not executed.is_finite() or executed < 0 or executed > qty:
                return {"ok": False, "stage": "malformed-fill", "halt_persisted": True,
                        "submitted": True, "detail": "emergency IOC fill quantity is invalid"}
            remaining = floor_step(max(Decimal(0), recorded - executed), rules.step)
            account_free = floor_step(self._free("BTC"), rules.step)
            observed_reduction = free_before - account_free
            verified = (
                remaining < rules.min_qty
                and observed_reduction + rules.step >= executed
            )
            if verified:
                self.state_store.upsert_trade(row["trade_id"], active["pair"],
                    lifecycle_state=LifecycleState.EXIT_FILLED.value,
                    reconciliation_status="EMERGENCY_EXIT_VERIFIED")
            return {"ok": verified, "stage": "verified-exit" if verified else "partial-or-unfilled",
                    "halt_persisted": True, "submitted": True,
                    "order_status": response.get("status", "UNKNOWN"),
                     "submitted_qty": str(qty), "executed_qty": str(executed),
                     "remaining_bot_owned_base": str(remaining),
                     "account_btc_free": str(account_free),
                     "observed_account_free_reduction": str(observed_reduction),
                    "detail": "emergency exit verified" if verified else
                              "IOC did not fully exit; reconcile and take manual action"}

    def status(self):
        return json.dumps({"mode": self.mode, "started": self.started,
            "entries_armed": self.enabled and self.state_store.entries(),
            "active_pair": self.pair_controller.load(),
            "nonterminal_trades": self.state_store.active_trade_rows(),
            "unresolved_intents": len(self.state_store.unresolved_intents())}, default=str)

    def positions(self):
        return self.state_store.active_trade_rows()

    def balance(self):
        active = self.pair_controller.load(); balances = self._balances(self.gateway.account())
        quote = active["quote"]
        return {"available": True, "pair": active["pair"],
                "BTC_free": str(balances.get("BTC", (0, 0))[0]),
                "quote_asset": quote, "quote_free": str(balances.get(quote, (0, 0))[0])}

    def profit(self):
        return {"available": False,
                "reason": "realized P&L requires reconciled fill/commission ledger; inspect monitoring report"}

    def restart_user_stream(self):
        self._pause("user-stream-manual-restart")
        if self.stream:
            self.stream.stop()
        self.stream = self.stream_factory(
            self.gateway, self._on_order_update, self._on_list_update,
            self._on_resync, testnet=self.mode == "testnet")
        self.stream.start()
        return self.reconcile()

    def set_size(self, value: float):
        value = Decimal(str(value))
        if value < 5 or value > 100_000:
            return "size must be 5-100000 quote units"
        if self.mode == "live" and value != self.trade_size_quote:
            return (
                "live trade size is evidence-bound; change TRADE_SIZE_QUOTE, "
                "produce fresh signed evidence, and redeploy"
            )
        self.trade_size_quote = value
        return f"trade size = {value} quote units"

    def set_max(self, count: int):
        return "max concurrent positions = 1" if int(count) == 1 else (
            "Bitcoin release permits exactly one position")
