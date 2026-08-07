from __future__ import annotations
"""Deterministic simulator using the same intent/event lifecycle as live."""

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.market_policy import canonical_pair, symbol_for_pair
from services.common.models import LifecycleState


class _SimTrader:
    def __init__(self):
        self._running = False

    def is_running(self):
        return self._running


class SimulationAdapter:
    def __init__(self, state_store=None, guard=None, notifier=None, fault_path=None):
        self.state_store = state_store
        self.guard = guard
        self.notifier = notifier or (lambda *a, **k: None)
        self.trader = _SimTrader()
        self.fault_path = Path(fault_path) if fault_path else None
        self._counter = 0
        self.sim_positions: dict[str, dict] = {}
        self.trade_size_quote = Decimal("100")
        self.max_positions = 1
        self.enabled = False

    def start(self):
        self.trader._running = True
        self.enabled = False
        audit('simulation_adapter_started', details={'deterministic': True, 'entries_default': 'off'})
        return True

    def tick(self):
        return None

    def maybe_auto_manage(self, flow: dict):
        return 'automatic protection simulation is advisory only'

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        return 'ON' if self.enabled else 'OFF'

    def _next_fault(self) -> str:
        if not self.fault_path:
            return 'none'
        data = read_json(self.fault_path, {}) or {}
        plan = list(data.get('queue') or [])
        if not plan:
            return 'none'
        nxt = str(plan.pop(0))
        atomic_write_json(self.fault_path, {'queue': plan})
        return nxt

    def _emit(self, event: dict):
        is_new = self.state_store.record_exchange_event(event) if self.state_store else True
        if is_new and self.guard:
            self.guard.on_exchange_event(event)

    def _intent(self, trade_id: str, symbol: str, request: dict) -> str:
        value = hashlib.sha256(f"sim-entry|{trade_id}|{symbol}".encode()).hexdigest()[:32]
        if not self.state_store.prepare_intent(
            intent_id=value, operation='ENTRY', trade_id=trade_id, symbol=symbol,
            endpoint='SIMULATED/orderList/otoco', request=request,
            list_client_order_id=f'SIM-{value[:20]}'):
            raise RuntimeError('duplicate simulation operation intent')
        return value

    def submit(self, symbol: str, note: str = '', *, trade_id: str | None = None):
        if not self.trader.is_running() or not self.enabled:
            return False, 'simulation entries are not armed'
        symbol = str(symbol).upper()
        pair = self.state_store.pair_for_symbol(symbol)
        trade_id = str(trade_id or self.state_store._active_trade_id(pair) or '')
        if not trade_id:
            return False, 'no durable trade record exists for simulation entry'
        if len(self.sim_positions) >= self.max_positions:
            return False, 'maximum simulated positions reached'
        self._counter += 1
        n, fault = self._counter, self._next_fault()
        entry_order_id, list_id = 100_000 + n * 10, 500_000 + n
        protection_order_id = entry_order_id + 2
        price, qty, ts = Decimal('100'), Decimal('1'), int(time.time() * 1000)
        intent_id = self._intent(trade_id, symbol, {
            'symbol': symbol, 'quote_size': str(self.trade_size_quote), 'note': note})
        self.state_store.mark_intent_submitting(intent_id)
        if fault == 'timeout':
            self.state_store.finish_intent(intent_id, 'AMBIGUOUS', error='simulated accepted-timeout')
            self.state_store.upsert_trade(trade_id, pair,
                lifecycle_state=LifecycleState.AMBIGUOUS.value,
                reconciliation_status='SIMULATED_AMBIGUOUS_RECONCILE_REQUIRED')
            return False, 'SIMULATED accepted-timeout; outcome ambiguous and no retry is allowed'

        self.state_store.upsert_trade(trade_id, pair, entry_order_id=entry_order_id,
                                      order_list_id=list_id)
        common = {'e': 'executionReport', 's': symbol, 'i': entry_order_id,
                  'S': 'BUY', 'o': 'LIMIT', 'q': str(qty)}
        self._emit(dict(common, I=n * 100 + 1, X='NEW', z='0', Z='0', E=ts))
        if fault == 'reject':
            self._emit(dict(common, I=n * 100 + 2, X='REJECTED', z='0', Z='0',
                            r='INSUFFICIENT_BALANCE', E=ts + 1))
            self.state_store.finish_intent(intent_id, 'DEFINITE_REJECT', error='simulated reject')
            return False, 'SIMULATED entry definitely rejected'

        fill_qty = qty / 2 if fault == 'partial_fill' else qty
        quote = pair.split('/', 1)[1]
        status = 'PARTIALLY_FILLED' if fault == 'partial_fill' else 'FILLED'
        self._emit(dict(common, I=n * 100 + 3, X=status, z=str(fill_qty),
                        Z=str(fill_qty * price), N=quote, E=ts + 1))
        if fault == 'partial_fill':
            self._emit(dict(common, I=n * 100 + 4, X='CANCELED', z=str(fill_qty),
                            Z=str(fill_qty * price), E=ts + 2))
        self._emit({'e': 'listStatus', 'E': ts + 3, 's': symbol, 'g': list_id,
                    'l': 'EXEC_STARTED', 'L': 'EXECUTING', 'C': f'sim-list-{n}'})
        self._emit({'e': 'executionReport', 'I': n * 100 + 5, 's': symbol,
                    'i': protection_order_id, 'g': list_id, 'S': 'SELL',
                    'o': 'STOP_LOSS_LIMIT', 'X': 'NEW', 'q': str(fill_qty),
                    'z': '0', 'E': ts + 4})
        self.state_store.upsert_trade(trade_id, pair, stop_order_id=protection_order_id,
                                      protected_quantity=str(fill_qty))
        self.state_store.finish_intent(intent_id, 'CONFIRMED',
            exchange_order_id=entry_order_id, exchange_order_list_id=list_id)
        self.sim_positions[symbol] = {
            'qty': str(fill_qty), 'entry_price': str(price), 'order_list_id': list_id,
            'entry_order_id': entry_order_id, 'stop_order_id': protection_order_id,
            'partial': fault == 'partial_fill', 'pair': pair,
        }
        detail = 'partial fill protected' if fault == 'partial_fill' else 'filled and protected'
        return True, f'SIMULATED {symbol} {detail} (order {entry_order_id}, list {list_id})'

    def status(self):
        return json.dumps({'simulation': True, 'armed': self.enabled,
            'open_positions': self.sim_positions, 'trade_size_quote': str(self.trade_size_quote),
            'max_positions': self.max_positions}, sort_keys=True)

    def positions(self):
        return list(self.sim_positions.keys())

    def balance(self):
        return {'available': True, 'simulation': True, 'quote_free': '1000',
                'open_positions': list(self.sim_positions.keys())}

    def profit(self):
        return {'available': True, 'simulation': True, 'daily_pnl_pct': 0.0,
                'daily_trades': self._counter, 'open_positions': len(self.sim_positions)}

    def restart_user_stream(self):
        return 'not applicable in simulation'

    def validate_pair(self, pair: str) -> dict:
        pair = canonical_pair(pair)
        return {'pair': pair, 'symbol': symbol_for_pair(pair), 'simulation': True}

    def validate_pair_funding(self, pair: str) -> dict:
        pair = canonical_pair(pair)
        return {
            'pair': pair,
            'symbol': symbol_for_pair(pair),
            'simulation': True,
            'quote_free': '1000',
            'required_quote': str(self.trade_size_quote),
            'preflight': {'simulation': True},
        }

    def set_size(self, value: float):
        value = Decimal(str(value))
        if value < 5 or value > 100_000:
            return 'size must be 5-100000 quote units'
        self.trade_size_quote = value
        return f'trade size = {value} quote units'

    def set_max(self, count: int):
        if int(count) != 1:
            return 'Bitcoin release permits exactly one position'
        self.max_positions = 1
        return 'max concurrent positions = 1'

    def emergency_exit(self, symbol) -> dict:
        symbol = str(symbol).upper()
        position = self.sim_positions.get(symbol)
        if not position:
            return {'ok': False, 'stage': 'not-found', 'halt_persisted': False,
                    'detail': 'no such open position'}
        self.enabled = False
        ts = int(time.time() * 1000)
        self._emit({'e': 'executionReport', 'I': ts, 's': symbol,
                    'i': position['stop_order_id'], 'g': position['order_list_id'],
                    'S': 'SELL', 'o': 'STOP_LOSS_LIMIT', 'X': 'FILLED',
                    'z': position['qty'],
                    'Z': str(Decimal(position['qty']) * Decimal(position['entry_price'])), 'E': ts})
        self.sim_positions.pop(symbol, None)
        return {'ok': True, 'stage': 'verified-exit', 'halt_persisted': True,
                'submitted': True, 'executed_qty': position['qty'], 'remaining_base': '0',
                'detail': f'SIMULATED emergency exit of {symbol} verified'}

    def convert(self, symbol: str, mode, *, break_even=False, lock_profit_pct=None,
                trailing_delta_bips=None):
        symbol = str(symbol).upper()
        position = self.sim_positions.get(symbol)
        if not position:
            return False, 'position not found'
        old_list, ts = position['order_list_id'], int(time.time() * 1000)
        new_list = old_list + 1000
        self._emit({'e': 'listStatus', 'E': ts, 's': symbol, 'g': old_list,
                    'l': 'ALL_DONE', 'L': 'ALL_DONE', 'C': f'sim-convert-{old_list}'})
        self._emit({'e': 'listStatus', 'E': ts + 1, 's': symbol, 'g': new_list,
                    'l': 'EXEC_STARTED', 'L': 'EXECUTING', 'C': f'sim-convert-{new_list}'})
        position['order_list_id'] = new_list
        value = getattr(mode, 'value', str(mode))
        return True, f'SIMULATED {symbol} protection changed to {value}'

    def mirror_positions(self, status='MATCHED'):
        self.state_store.data['last_reconciliation_status'] = status
        self.state_store.data['last_reconciliation_at'] = time.time()
        self.state_store.save()
        return len(self.sim_positions)

    def verified_reconcile(self) -> dict:
        unresolved = self.state_store.unresolved_intents()
        ambiguous = [item for item in unresolved if item['state'] in {'SUBMITTING', 'AMBIGUOUS'}]
        ok = not ambiguous
        mirrored = self.mirror_positions('RECONCILED_SIMULATION' if ok else 'SIMULATION_AMBIGUOUS')
        return {'ok': ok, 'endpoints': {'simulation': {'ok': ok}}, 'mirrored': mirrored,
                'detail': (f'simulation reconcile complete; {mirrored} position(s)' if ok else
                           'simulation has unresolved ambiguous intent(s)')}

    def reconcile(self):
        result = self.verified_reconcile()
        return result['detail']

    def verify_flat_for_switch(self, symbols: set[str]) -> dict:
        if self.sim_positions:
            return {'ok': False, 'detail': 'simulated position is open'}
        if self.state_store.active_trade_rows():
            return {'ok': False, 'detail': 'durable nonterminal trade exists'}
        if self.state_store.unresolved_intents():
            return {'ok': False, 'detail': 'unresolved operation intent exists'}
        return {'ok': True, 'simulation': True, 'symbols': sorted(symbols),
                'detail': 'simulation state is flat'}
