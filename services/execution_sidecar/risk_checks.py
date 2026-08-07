from __future__ import annotations
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import json
import threading

from services.common.atomic import atomic_write_json
from services.common.audit import audit
from services.common.config_bounds import ConfigError, env_float, env_int
from services.execution_sidecar.protection_modes import fee_adjusted_break_even


class FreshSignalGuard:
    """Signal freshness, cooldown and stop-out budget guard.

    Reviewed parent-release safety fixes applied here:
      * M-006 — every read-modify-write is serialized with an RLock because the
        user-data-stream callback thread and the main sidecar loop mutate the
        same state.
      * configuration is parsed through typed, bounded helpers;
        a malformed or unsafe private environment fails startup with a precise
        error instead of silently weakening a guard.
      * M-005 — a filled stop-type SELL only consumes the stop-out budget when
        the verified exit average is at or below the recorded entry average;
        profitable exchange-native trailing exits no longer trigger cooldowns
        or daily loss lockouts.
    """

    def __init__(self, state_path, state_store=None):
        self.path = Path(state_path)
        self.store = state_store
        self.lock = threading.RLock()
        if self.path.is_symlink():
            raise ConfigError('risk state file must not be a symlink')
        try:
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            self.state = {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError('existing risk state is unreadable or malformed') from exc
        if not isinstance(self.state, dict):
            raise ConfigError('existing risk state must be a JSON object')
        self.state.setdefault('pairs', {})
        self.state.setdefault('daily', {})
        self.state.setdefault('global_pause', '')
        self._validate_state()
        self.max_stopouts_pair = env_int('MAX_STOPOUTS_PER_PAIR_DAY', 3, 1, 100)
        self.max_stopouts_global = env_int('MAX_STOPOUTS_GLOBAL_DAY', 10, 1, 1000)
        self.cooldown_seconds = env_int('PAIR_COOLDOWN_SECONDS', 60, 0, 86_400)
        self.max_age_seconds = env_int('MAX_SIGNAL_AGE_SECONDS', 180, 10, 3600)
        self.max_candle_age_seconds = env_int('MAX_CANDLE_AGE_SECONDS', 180, 10, 3600)

    def _validate_state(self):
        pairs = self.state.get('pairs')
        daily = self.state.get('daily')
        pause = self.state.get('global_pause')
        if not isinstance(pairs, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in pairs.items()
        ):
            raise ConfigError('risk state pairs mapping is malformed')
        if not isinstance(daily, dict):
            raise ConfigError('risk state daily mapping is malformed')
        if not isinstance(pause, str):
            raise ConfigError('risk state global pause is malformed')
        for day, value in daily.items():
            if not isinstance(day, str) or not isinstance(value, dict):
                raise ConfigError('risk state daily entry is malformed')
            counts = value.get('pairs', {})
            try:
                global_count = int(value.get('global_stopouts', 0))
                pair_counts = [int(item) for item in counts.values()]
            except (AttributeError, TypeError, ValueError) as exc:
                raise ConfigError('risk state stop-out counter is malformed') from exc
            if (not isinstance(counts, dict) or global_count < 0
                    or any(not isinstance(key, str) for key in counts)
                    or any(item < 0 for item in pair_counts)):
                raise ConfigError('risk state stop-out counter is malformed')

    @staticmethod
    def _dt(value):
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value) / (1000 if value > 10_000_000_000 else 1), timezone.utc)
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _day(now):
        return now.astimezone(timezone.utc).date().isoformat()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.state)

    def set_global_pause(self, reason: str):
        with self.lock:
            self.state['global_pause'] = reason
            self._save()

    def clear_global_pause(self):
        with self.lock:
            self.state['global_pause'] = ''
            self._save()

    def allow(self, signal, active_pair: str, pair_state_hash: str, state_store=None):
        now = datetime.now(timezone.utc)
        pair, token = signal.get('pair', ''), signal.get('signal_id', '')
        with self.lock:
            if self.state.get('global_pause'):
                return False, 'global risk pause: ' + self.state['global_pause']
            if not pair or not token:
                return False, 'missing pair or signal token'
            try: generated = self._dt(signal['generated_at'])
            except Exception: return False, 'invalid signal time'
            generated_age = (now - generated).total_seconds()
            if generated_age > self.max_age_seconds:
                return False, 'stale signal'
            if generated_age < -30:
                return False, 'signal generated_at is in the future'
            if pair != active_pair:
                return False, 'signal pair is not the active BTC pair'
            if not pair_state_hash or signal.get('pair_state_hash') != pair_state_hash:
                return False, 'active pair state hash mismatch'
            if state_store and state_store.signal_seen(token):
                return False, 'duplicate signal token'

            try:
                signal_candle = self._dt(signal.get('candle_time'))
                candle_age = (now - signal_candle).total_seconds()
                if candle_age > self.max_candle_age_seconds:
                    return False, 'stale signal candle'
                if candle_age < -30:
                    return False, 'signal candle is in the future'
            except Exception:
                return False, 'invalid candle timestamp'

            pair_state = self.state['pairs'].get(pair, {})
            if pair_state.get('signal_id') == token:
                return False, 'duplicate signal token'
            if pair_state.get('candle_time') == signal.get('candle_time'):
                return False, 'same-candle re-entry blocked'
            try:
                stopout_candle = self._dt(pair_state['last_stopout_time']) if pair_state.get('last_stopout_time') else None
                if stopout_candle and signal_candle <= stopout_candle:
                    return False, 'signal candle is not newer than stop-loss exit'
            except Exception:
                return False, 'invalid stop-loss timestamp state'
            try:
                cooldown_until = self._dt(pair_state['cooldown_until']) if pair_state.get('cooldown_until') else None
                if cooldown_until and now < cooldown_until:
                    return False, 'pair cooldown active'
            except Exception:
                return False, 'invalid cooldown state'

            day = self._day(now)
            daily = self.state['daily'].get(day, {'global_stopouts': 0, 'pairs': {}})
            if int(daily.get('global_stopouts', 0)) >= self.max_stopouts_global:
                return False, 'global daily stop-loss guard active'
            if int(daily.get('pairs', {}).get(pair, 0)) >= self.max_stopouts_pair:
                return False, 'maximum stop-outs for pair reached today'
            return True, 'fresh'

    def record(self, signal, result):
        with self.lock:
            pair = signal['pair']
            prior = self.state['pairs'].get(pair, {})
            prior.update({
                'signal_id': signal['signal_id'], 'candle_time': signal.get('candle_time'),
                'result': result, 'recorded_at': datetime.now(timezone.utc).isoformat()
            })
            self.state['pairs'][pair] = prior
            self._save()

    def record_stopout(self, symbol: str, event_time=None):
        with self.lock:
            now = self._dt(event_time) if event_time else datetime.now(timezone.utc)
            if self.store is None:
                raise RuntimeError('authoritative state store is required for symbol mapping')
            pair = self.store.pair_for_symbol(symbol)
            day = self._day(now)
            # Retain only a bounded recent window; old daily counters have no effect
            # on today's guard and otherwise grow forever.
            cutoff = (now - timedelta(days=14)).date().isoformat()
            self.state['daily'] = {key: value for key, value in self.state['daily'].items() if key >= cutoff}
            daily = self.state['daily'].setdefault(day, {'global_stopouts': 0, 'pairs': {}})
            daily['global_stopouts'] = int(daily.get('global_stopouts', 0)) + 1
            pairs = daily.setdefault('pairs', {})
            pairs[pair] = int(pairs.get(pair, 0)) + 1
            ps = self.state['pairs'].setdefault(pair, {})
            ps['last_stopout_time'] = now.isoformat()
            ps['cooldown_until'] = (now + timedelta(seconds=self.cooldown_seconds)).isoformat()
            self._save()

    def _classify_protective_exit(self, event: dict) -> tuple[bool, str]:
        """True when this filled stop-type SELL is a realized LOSS (M-005).

        Uses the verified exit average from the event and the entry average
        recorded in the state store. When the entry price cannot be
        determined the exit is conservatively counted as a stop-out.
        """
        symbol = str(event.get('s', ''))
        try:
            executed = Decimal(str(event.get('z') or '0'))
            quote = Decimal(str(event.get('Z') or '0'))
            exit_average = quote / executed if executed > 0 else None
        except Exception:
            exit_average = None
        entry_average = None
        if self.store is not None:
            try:
                entry_average = self.store.average_entry_price_for_symbol(symbol)
            except Exception:
                entry_average = None
        if exit_average is None or entry_average is None:
            return True, 'entry-or-exit-average-unknown-counted-conservatively'
        try:
            threshold = fee_adjusted_break_even(
                entry_average,
                buy_fee_pct=Decimal(str(env_float('FEE_PCT_PER_SIDE', 0.1, 0, 5))),
                sell_fee_pct=Decimal(str(env_float('FEE_PCT_PER_SIDE', 0.1, 0, 5))),
                slippage_pct=Decimal(str(env_float(
                    'BREAK_EVEN_SLIPPAGE_PCT', 0.05, 0, 5))),
            )
        except Exception:
            return True, 'fee-adjusted-break-even-unknown-counted-conservatively'
        if exit_average > threshold:
            return False, (
                f'protective exit above fee-adjusted break-even '
                f'({exit_average} > {threshold})'
            )
        return True, f'realized net loss ({exit_average} <= {threshold})'

    def on_exchange_event(self, event: dict):
        if str(event.get('e')) != 'executionReport':
            return
        if str(event.get('S')).upper() != 'SELL' or str(event.get('X')).upper() != 'FILLED':
            return
        order_type = str(event.get('o', '')).upper()
        if order_type not in {'STOP_LOSS', 'STOP_LOSS_LIMIT'}:
            return
        is_loss, detail = self._classify_protective_exit(event)
        if not is_loss:
            audit('profitable_protective_exit_not_counted', details={
                'symbol': event.get('s'), 'detail': detail})
            return
        self.record_stopout(str(event.get('s', '')), event.get('E'))
