from __future__ import annotations
import hashlib, json, sqlite3, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from services.common.atomic import atomic_write_json, read_json
from services.common.models import LifecycleState, ProtectionMode

BASE_SCHEMA_VERSION = 1
RISK_STATE_SCHEMA_VERSION = 2
CURRENT_SCHEMA_VERSION = RISK_STATE_SCHEMA_VERSION

RISK_STATE_SCHEMA = '''
CREATE TABLE risk_guard_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  state_version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  source_sha256 TEXT,
  applied_at TEXT NOT NULL
);
'''

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS trade_records (
  trade_id TEXT PRIMARY KEY,
  pair TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL,
  entry_client_order_id TEXT,
  entry_submitted_at TEXT,
  entry_order_id INTEGER,
  order_list_id INTEGER,
  take_profit_order_id INTEGER,
  stop_order_id INTEGER,
  filled_quantity TEXT,
  protected_quantity TEXT,
  average_entry_price TEXT,
  protection_mode TEXT,
  trailing_delta INTEGER,
  commission_asset TEXT,
  last_exchange_event_id TEXT,
  last_event_time TEXT,
  reconciliation_status TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_signals (
  signal_id TEXT PRIMARY KEY,
  pair TEXT NOT NULL,
  candle_time TEXT,
  result TEXT NOT NULL,
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_commands (
  command_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  result TEXT NOT NULL,
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT UNIQUE,
  event_type TEXT NOT NULL,
  symbol TEXT,
  order_id INTEGER,
  order_list_id INTEGER,
  commission_asset TEXT,
  commission_amount TEXT,
  event_time TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_intents (
  intent_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  trade_id TEXT,
  symbol TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  request_json TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  client_order_id TEXT,
  list_client_order_id TEXT,
  parent_intent_id TEXT,
  state TEXT NOT NULL,
  exchange_order_id INTEGER,
  exchange_order_list_id INTEGER,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_intents_state ON operation_intents(state);
'''

class StateStore:
    def __init__(self, path: str | Path, db_path: str | Path | None = None):
        self.path = Path(path)
        self.db_path = Path(db_path or self.path.with_suffix('.sqlite'))
        self.database_preexisted = self.db_path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data = read_json(self.path, {}) or {}
        self.data.setdefault('protection_mode', ProtectionMode.OCO_TRAILING.value)
        self.data.setdefault('entries_enabled', False)
        self.data.setdefault('simulation', True)
        self.data.setdefault('pause_reason', 'startup-safe-default')
        self.data.setdefault('last_reconciliation_status', 'NOT_RUN')
        self.data.setdefault('symbol_pairs', {})
        with self._connect() as con:
            # H-006: refuse to run on a corrupted database. Idempotency and
            # restart-recovery guarantees are meaningless if the authoritative
            # store is silently damaged; the operator must restore a verified
            # backup instead.
            self._verify_integrity(con)
            version = self._schema_version(con)
            if version not in {0, BASE_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}:
                raise RuntimeError(
                    f'unsupported SQLite state schema version {version}; fail closed')
            con.executescript(SCHEMA)
            columns = {row['name'] for row in con.execute('PRAGMA table_info(trade_records)')}
            if 'entry_submitted_at' not in columns:
                con.execute('ALTER TABLE trade_records ADD COLUMN entry_submitted_at TEXT')
            if version == 0:
                con.execute(f'PRAGMA user_version = {BASE_SCHEMA_VERSION}')
        self.save()

    @staticmethod
    def _schema_version(con) -> int:
        row = con.execute('PRAGMA user_version').fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _verify_integrity(con) -> None:
        try:
            rows = con.execute('PRAGMA quick_check').fetchall()
        except sqlite3.DatabaseError as exc:
            # A truncated/garbage file raises "file is not a database" here.
            raise RuntimeError(
                'SQLite state database is unreadable/corrupt — fail closed; '
                'restore a verified backup before restarting. Details: ' + str(exc)) from exc
        if not rows or str(rows[0][0]).strip().lower() != 'ok':
            details = '; '.join(str(row[0]) for row in rows[:5]) if rows else 'no result'
            raise RuntimeError(
                'SQLite state database failed integrity quick_check — fail closed; '
                'restore a verified backup before restarting. Details: ' + details)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def save(self):
        with self.lock:
            atomic_write_json(self.path, self.data)

    @staticmethod
    def _canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
        text = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
        return text, hashlib.sha256(text.encode('utf-8')).hexdigest()

    def schema_version(self) -> int:
        with self._connect() as con:
            return self._schema_version(con)

    def initialize_risk_guard_state(
        self, payload: dict[str, Any], *, source_sha256: str | None,
        allow_new_install: bool,
    ) -> dict[str, Any]:
        """Atomically migrate or initialize the authoritative risk state.

        Version 1 databases predate SQLite-backed risk state. An existing
        database therefore requires a validated legacy projection; only the
        process that created a genuinely new database may initialize empty
        state without one.
        """
        if not isinstance(payload, dict):
            raise TypeError('risk state payload must be a mapping')
        if source_sha256 is None and not allow_new_install:
            raise RuntimeError(
                'legacy risk state is unexpectedly missing for an existing database; '
                'fail closed and restore verified state')
        payload_text, payload_sha256 = self._canonical_payload(payload)
        with self.lock, self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            version = self._schema_version(con)
            if version == CURRENT_SCHEMA_VERSION:
                return self._read_risk_guard_state(con)
            if version != BASE_SCHEMA_VERSION:
                raise RuntimeError(
                    f'cannot migrate risk state from SQLite schema version {version}')
            for statement in filter(None, (item.strip() for item in RISK_STATE_SCHEMA.split(';'))):
                con.execute(statement)
            now = self._now()
            con.execute(
                '''INSERT INTO risk_guard_state(
                   singleton,state_version,payload_json,payload_sha256,updated_at)
                   VALUES(1,1,?,?,?)''',
                (payload_text, payload_sha256, now),
            )
            con.execute(
                '''INSERT INTO schema_migrations(version,name,source_sha256,applied_at)
                   VALUES(?,?,?,?)''',
                (CURRENT_SCHEMA_VERSION, 'sqlite-authoritative-risk-guard-state',
                 source_sha256, now),
            )
            con.execute(f'PRAGMA user_version = {CURRENT_SCHEMA_VERSION}')
            restored = self._read_risk_guard_state(con)
            if restored != payload:
                raise RuntimeError('risk state migration read-back verification failed')
            return restored

    def _read_risk_guard_state(self, con) -> dict[str, Any]:
        row = con.execute(
            '''SELECT state_version,payload_json,payload_sha256
               FROM risk_guard_state WHERE singleton=1'''
        ).fetchone()
        if row is None or int(row['state_version']) != 1:
            raise RuntimeError('authoritative risk state row is missing or unsupported')
        payload_text = str(row['payload_json'])
        actual = hashlib.sha256(payload_text.encode('utf-8')).hexdigest()
        if actual != str(row['payload_sha256']):
            raise RuntimeError('authoritative risk state checksum mismatch')
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError('authoritative risk state payload is malformed') from exc
        if not isinstance(payload, dict):
            raise RuntimeError('authoritative risk state payload must be a mapping')
        return payload

    def risk_guard_state(self) -> dict[str, Any]:
        with self._connect() as con:
            if self._schema_version(con) != CURRENT_SCHEMA_VERSION:
                raise RuntimeError('authoritative risk state has not been initialized')
            return self._read_risk_guard_state(con)

    def save_risk_guard_state(self, payload: dict[str, Any]) -> None:
        """Commit one complete risk-state mutation before acknowledging it."""
        if not isinstance(payload, dict):
            raise TypeError('risk state payload must be a mapping')
        payload_text, payload_sha256 = self._canonical_payload(payload)
        with self.lock, self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if self._schema_version(con) != CURRENT_SCHEMA_VERSION:
                raise RuntimeError('authoritative risk state schema is unavailable')
            cur = con.execute(
                '''UPDATE risk_guard_state
                   SET payload_json=?,payload_sha256=?,updated_at=? WHERE singleton=1''',
                (payload_text, payload_sha256, self._now()),
            )
            if cur.rowcount != 1:
                raise RuntimeError('authoritative risk state row is missing')

    def risk_guard_migration(self) -> dict[str, Any] | None:
        with self._connect() as con:
            if self._schema_version(con) < CURRENT_SCHEMA_VERSION:
                return None
            row = con.execute(
                'SELECT * FROM schema_migrations WHERE version=?',
                (CURRENT_SCHEMA_VERSION,),
            ).fetchone()
        return dict(row) if row else None

    def get_mode(self):
        return self.data.get('protection_mode', ProtectionMode.OCO_TRAILING.value)

    def set_mode(self, mode):
        self.data['protection_mode'] = ProtectionMode(mode).value
        self.save()

    def entries(self):
        return bool(self.data.get('entries_enabled', False))

    def set_entries(self, value: bool, reason: str = ''):
        self.data['entries_enabled'] = bool(value)
        self.data['pause_reason'] = '' if value else (reason or 'operator-paused')
        self.save()

    def signal_seen(self, signal_id: str) -> bool:
        return self.signal_result(signal_id) is not None

    def signal_result(self, signal_id: str) -> str | None:
        with self._connect() as con:
            row = con.execute('SELECT result FROM processed_signals WHERE signal_id=?', (signal_id,)).fetchone()
        return str(row['result']) if row else None

    def claim_signal(self, signal: dict[str, Any]) -> bool:
        """Atomically reserve a signal before any exchange submission."""
        with self._connect() as con:
            cur = con.execute(
                '''INSERT OR IGNORE INTO processed_signals(signal_id,pair,candle_time,result,processed_at)
                   VALUES(?,?,?,?,?)''',
                (signal['signal_id'], signal['pair'], signal.get('candle_time'), 'IN_PROGRESS', self._now()),
            )
            return cur.rowcount == 1

    def record_signal(self, signal: dict[str, Any], result: str):
        with self._connect() as con:
            con.execute(
                '''INSERT INTO processed_signals(signal_id,pair,candle_time,result,processed_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(signal_id) DO UPDATE SET result=excluded.result, processed_at=excluded.processed_at''',
                (signal['signal_id'], signal['pair'], signal.get('candle_time'), result, self._now()),
            )

    def command_seen(self, command_id: str) -> bool:
        return self.command_result(command_id) is not None

    def command_result(self, command_id: str) -> str | None:
        with self._connect() as con:
            row = con.execute('SELECT result FROM processed_commands WHERE command_id=?', (command_id,)).fetchone()
        return str(row['result']) if row else None

    def claim_command(self, command_id: str, command: str) -> bool:
        """Atomically reserve a command before any exchange-affecting action."""
        with self._connect() as con:
            cur = con.execute(
                'INSERT OR IGNORE INTO processed_commands(command_id,command,result,processed_at) VALUES(?,?,?,?)',
                (command_id, command, 'IN_PROGRESS', self._now()),
            )
            return cur.rowcount == 1

    def record_command(self, command_id: str, command: str, result: str):
        with self._connect() as con:
            con.execute(
                '''INSERT INTO processed_commands(command_id,command,result,processed_at) VALUES(?,?,?,?)
                   ON CONFLICT(command_id) DO UPDATE SET result=excluded.result, processed_at=excluded.processed_at''',
                (command_id, command, result, self._now()),
            )

    # ---- money-side operation intents ----
    def prepare_intent(self, *, intent_id: str, operation: str, trade_id: str | None,
                       symbol: str, endpoint: str, request: dict,
                       client_order_id: str = '', list_client_order_id: str = '',
                       parent_intent_id: str = '',
                       close_parent_intent_id: str = '') -> bool:
        """Commit the complete deterministic request before any network call."""
        payload = json.dumps(request, sort_keys=True, separators=(',', ':'), default=str)
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        now = self._now()
        with self._connect() as con:
            cur = con.execute(
                '''INSERT OR IGNORE INTO operation_intents(
                   intent_id,operation,trade_id,symbol,endpoint,request_json,request_sha256,
                   client_order_id,list_client_order_id,parent_intent_id,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?)''',
                (intent_id, operation, trade_id, str(symbol).upper(), endpoint, payload, digest,
                 client_order_id or None, list_client_order_id or None,
                 parent_intent_id or None, 'PREPARED', now, now),
            )
            if cur.rowcount == 1 and close_parent_intent_id:
                parent = con.execute(
                    "UPDATE operation_intents SET state='ABORTED',error=?,updated_at=? "
                    "WHERE intent_id=? AND state='PREPARED'",
                    (f'resumed atomically into child intent {intent_id}', now,
                     str(close_parent_intent_id)),
                )
                if parent.rowcount != 1:
                    raise RuntimeError(
                        'replacement parent was not uniquely PREPARED; child intent rolled back'
                    )
            return cur.rowcount == 1

    def mark_intent_submitting(self, intent_id: str) -> None:
        """Durably record the may-have-landed boundary immediately before POST."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE operation_intents SET state='SUBMITTING',updated_at=? "
                "WHERE intent_id=? AND state='PREPARED'", (self._now(), intent_id))
            if cur.rowcount != 1:
                raise RuntimeError(f'intent {intent_id!r} is not uniquely PREPARED')

    def finish_intent(self, intent_id: str, state: str, *, exchange_order_id=None,
                      exchange_order_list_id=None, error: str = '') -> None:
        allowed = {'CONFIRMED', 'DEFINITE_REJECT', 'AMBIGUOUS', 'ABORTED'}
        state = str(state).upper()
        if state not in allowed:
            raise ValueError(f'unsupported terminal intent state: {state}')
        with self._connect() as con:
            cur = con.execute(
                '''UPDATE operation_intents SET state=?,exchange_order_id=?,
                   exchange_order_list_id=?,error=?,updated_at=? WHERE intent_id=?''',
                (state, exchange_order_id, exchange_order_list_id, error or None,
                 self._now(), intent_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f'unknown intent: {intent_id}')

    def intent(self, intent_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute('SELECT * FROM operation_intents WHERE intent_id=?',
                              (intent_id,)).fetchone()
        return dict(row) if row else None

    def unresolved_intents(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM operation_intents WHERE state IN ('PREPARED','SUBMITTING','AMBIGUOUS') "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def intents_for_trade(self, trade_id: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                'SELECT * FROM operation_intents WHERE trade_id=? ORDER BY created_at',
                (str(trade_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_trade_rows(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                '''SELECT * FROM trade_records WHERE lifecycle_state NOT IN (?,?)
                   ORDER BY updated_at''',
                (LifecycleState.EXIT_FILLED.value, LifecycleState.ENTRY_REJECTED.value),
            ).fetchall()
        return [dict(row) for row in rows]

    def trade(self, trade_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                'SELECT * FROM trade_records WHERE trade_id=?', (str(trade_id),)
            ).fetchone()
        return dict(row) if row else None

    def active_trade_for_symbol(self, symbol: str) -> dict | None:
        pair = self.pair_for_symbol(symbol)
        trade_id = self._active_trade_id(pair)
        if not trade_id:
            return None
        with self._connect() as con:
            row = con.execute('SELECT * FROM trade_records WHERE trade_id=?', (trade_id,)).fetchone()
        return dict(row) if row else None

    def average_entry_price_for_symbol(self, symbol: str):
        """Entry average of the latest trade, including a just-closed exit event."""
        from decimal import Decimal
        pair = self.pair_for_symbol(symbol)
        with self._connect() as con:
            row = con.execute(
                '''SELECT average_entry_price FROM trade_records WHERE pair=?
                   ORDER BY updated_at DESC LIMIT 1''', (pair,)
            ).fetchone()
        value = row['average_entry_price'] if row else None
        if value in (None, ''):
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def backup(self, directory, retain: int = 14):
        """SQLite online backup with post-copy integrity verification (H-006)."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        dest = directory / f'execution_state.{stamp}.sqlite'
        with self._connect() as source:
            target = sqlite3.connect(dest)
            try:
                source.backup(target)
                rows = target.execute('PRAGMA quick_check').fetchall()
                if not rows or str(rows[0][0]).strip().lower() != 'ok':
                    raise RuntimeError('backup failed post-copy integrity quick_check')
                target.commit()
            finally:
                target.close()
        from services.common.retention import prune_files
        prune_files(directory, 'execution_state.*.sqlite', max_files=max(1, int(retain)))
        return dest

    def upsert_trade(self, trade_id: str, pair: str, **fields):
        allowed = {
            'lifecycle_state','entry_client_order_id','entry_submitted_at','entry_order_id','order_list_id',
            'take_profit_order_id','stop_order_id','filled_quantity','protected_quantity',
            'average_entry_price','protection_mode','trailing_delta','commission_asset',
            'last_exchange_event_id','last_event_time','reconciliation_status'
        }
        explicit_lifecycle = 'lifecycle_state' in fields
        clean = {k: v for k, v in fields.items() if k in allowed}
        # A new row gets the default lifecycle, but updating OTHER fields on an
        # existing row must never silently regress its lifecycle back to
        # SIGNAL_APPROVED. lifecycle_state is therefore only written on UPDATE
        # when the caller passed it explicitly.
        clean.setdefault('lifecycle_state', LifecycleState.SIGNAL_APPROVED.value)
        clean['updated_at'] = self._now()
        columns = ['trade_id','pair'] + list(clean)
        values = [trade_id, pair] + [clean[c] for c in clean]
        placeholders = ','.join('?' for _ in columns)
        update_keys = [c for c in clean if c != 'lifecycle_state' or explicit_lifecycle]
        updates = ','.join(f'{c}=excluded.{c}' for c in update_keys)
        # SQL identifiers come only from the fixed allowlist above; all values are bound.
        sql = f"INSERT INTO trade_records({','.join(columns)}) VALUES({placeholders}) ON CONFLICT(trade_id) DO UPDATE SET {updates}"  # nosec B608
        with self._connect() as con:
            con.execute(sql, values)

    def register_symbol_pair(self, symbol: str, pair: str) -> None:
        symbol, pair = str(symbol or '').upper(), str(pair or '').upper()
        if not symbol or '/' not in pair or pair.replace('/', '') != symbol:
            raise ValueError('symbol/pair mapping is inconsistent')
        with self.lock:
            self.data.setdefault('symbol_pairs', {})[symbol] = pair
            self.save()

    def pair_for_symbol(self, symbol: str) -> str:
        symbol = str(symbol or '').upper()
        pair = str(self.data.get('symbol_pairs', {}).get(symbol, ''))
        if not pair:
            raise ValueError(f'no authoritative pair mapping registered for {symbol!r}')
        return pair

    @staticmethod
    def _state_rank(value: str) -> int:
        order = {
            LifecycleState.SIGNAL_APPROVED.value: 0,
            LifecycleState.ENTRY_SUBMITTED.value: 1,
            LifecycleState.ENTRY_PARTIALLY_FILLED.value: 2,
            LifecycleState.ENTRY_FILLED.value: 3,
            LifecycleState.RECONCILED.value: 4,
            LifecycleState.PROTECTION_ACTIVE.value: 5,
            LifecycleState.BREAK_EVEN_ARMED.value: 6,
            LifecycleState.PROFIT_LOCKED.value: 7,
            LifecycleState.TRAILING_ACTIVE.value: 8,
            LifecycleState.EXIT_FILLED.value: 9,
            LifecycleState.ENTRY_REJECTED.value: 9,
            LifecycleState.AMBIGUOUS.value: 10,
            LifecycleState.ERROR.value: 10,
        }
        return order.get(str(value), -1)

    def _active_trade_id(self, pair: str) -> str | None:
        terminal = (LifecycleState.EXIT_FILLED.value, LifecycleState.ENTRY_REJECTED.value)
        with self._connect() as con:
            row = con.execute(
                '''SELECT trade_id FROM trade_records
                   WHERE pair=? AND lifecycle_state NOT IN (?,?)
                   ORDER BY updated_at DESC LIMIT 1''',
                (pair, *terminal),
            ).fetchone()
        return str(row['trade_id']) if row else None

    @staticmethod
    def _event_identity(event: dict[str, Any]):
        event_type = str(event.get('e') or event.get('eventType') or 'unknown')
        order_id = event.get('i') if event.get('i') is not None else event.get('orderId')
        order_list_id = event.get('g') if event.get('g') is not None else event.get('orderListId')
        event_time = event.get('E') if event.get('E') is not None else event.get('eventTime')
        raw_event_id = event.get('I') if event.get('I') is not None else event.get('u')
        symbol = event.get('s') or event.get('symbol')
        event_key = ':'.join(map(str, (
            event_type, symbol, order_id, order_list_id, raw_event_id, event_time,
            event.get('l') or event.get('listStatusType'),
            event.get('L') or event.get('listOrderStatus'),
            event.get('C') or event.get('listClientOrderId'),
            event.get('r') or event.get('rejectReason'),
            event.get('x') or event.get('executionType'),
            event.get('X') or event.get('orderStatus'),
            event.get('z') or event.get('executedQty'),
        )))
        return event_type, order_id, order_list_id, event_time, raw_event_id, symbol, event_key

    def _event_updates(self, row, event: dict[str, Any], event_key: str) -> dict[str, Any]:
        """Derive one monotonic trade update from an authenticated exchange event."""
        from decimal import Decimal, InvalidOperation

        event_type, _order_id, _list_id, event_time, raw_id, _symbol, _key = (
            self._event_identity(event)
        )
        updates: dict[str, Any] = {
            'last_exchange_event_id': str(raw_id) if raw_id is not None else event_key,
            'last_event_time': str(event_time or self._now()),
            'reconciliation_status': 'USER_STREAM_EVENT_APPLIED',
            'updated_at': self._now(),
        }
        commission_asset = event.get('N') or event.get('commissionAsset')
        if commission_asset:
            updates['commission_asset'] = str(commission_asset)

        lifecycle = None
        if event_type == 'executionReport':
            side = str(event.get('S') or event.get('side') or '').upper()
            status = str(event.get('X') or event.get('orderStatus') or event.get('status') or '').upper()
            order_type = str(event.get('o') or event.get('orderType') or event.get('type') or '').upper()
            cumulative = event.get('z') if event.get('z') is not None else event.get('executedQty')
            quote = event.get('Z') if event.get('Z') is not None else event.get('cummulativeQuoteQty')
            original_qty = event.get('q') if event.get('q') is not None else event.get('origQty')
            try:
                executed = Decimal(str(cumulative or '0'))
                if not executed.is_finite() or executed < 0:
                    raise InvalidOperation
            except (ArithmeticError, InvalidOperation, TypeError, ValueError):
                executed = Decimal(0)
                updates['reconciliation_status'] = 'MALFORMED_FILL_NUMERIC_RECONCILE_REQUIRED'
            if side == 'BUY' and cumulative not in (None, ''):
                updates['filled_quantity'] = str(executed)
            if side == 'BUY' and executed > 0 and quote not in (None, ''):
                try:
                    quoted = Decimal(str(quote))
                    if not quoted.is_finite() or quoted < 0:
                        raise InvalidOperation
                    updates['average_entry_price'] = str(quoted / executed)
                except (ArithmeticError, InvalidOperation, TypeError, ValueError):
                    updates['reconciliation_status'] = 'MALFORMED_FILL_NUMERIC_RECONCILE_REQUIRED'
            if side == 'SELL' and original_qty not in (None, '') and status in {'NEW', 'PARTIALLY_FILLED'}:
                try:
                    quantity = Decimal(str(original_qty))
                    if not quantity.is_finite() or quantity <= 0:
                        raise InvalidOperation
                    updates['protected_quantity'] = str(quantity)
                except (ArithmeticError, InvalidOperation, TypeError, ValueError):
                    updates['reconciliation_status'] = 'MALFORMED_PROTECTION_NUMERIC_RECONCILE_REQUIRED'

            if side == 'BUY' and status == 'PARTIALLY_FILLED':
                lifecycle = LifecycleState.ENTRY_PARTIALLY_FILLED.value
                updates['reconciliation_status'] = 'ENTRY_PARTIAL_FILL_CANCEL_AND_PROTECT_REQUIRED'
            elif side == 'BUY' and status == 'FILLED':
                lifecycle = LifecycleState.ENTRY_FILLED.value
            elif side == 'BUY' and status in {
                'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'
            }:
                if executed > 0:
                    lifecycle = LifecycleState.ENTRY_PARTIALLY_FILLED.value
                    updates['reconciliation_status'] = 'ENTRY_TERMINAL_PARTIAL_FILL_RECONCILE_REQUIRED'
                else:
                    lifecycle = LifecycleState.ENTRY_REJECTED.value
                    updates['reconciliation_status'] = 'ENTRY_TERMINAL_NO_FILL'
            elif side == 'SELL' and status in {'NEW', 'PARTIALLY_FILLED'} and order_type in {
                'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT', 'LIMIT_MAKER'
            }:
                lifecycle = LifecycleState.PROTECTION_ACTIVE.value
            elif side == 'SELL' and status == 'FILLED':
                lifecycle = LifecycleState.EXIT_FILLED.value
            elif side == 'SELL' and status in {
                'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'
            }:
                updates['reconciliation_status'] = 'SELL_PROTECTION_TERMINAL_RECONCILE_REQUIRED'
            elif status == 'REJECTED':
                updates['reconciliation_status'] = 'UNMATCHED_REJECTION_RECONCILE_REQUIRED'
        elif event_type == 'listStatus':
            list_status = str(event.get('l') or event.get('listStatusType') or '').upper()
            order_status = str(event.get('L') or event.get('listOrderStatus') or '').upper()
            if list_status in {'EXEC_STARTED', 'UPDATED'} and order_status == 'EXECUTING':
                # OTO/OTOCO is also EXECUTING while only its working BUY exists.
                # A list event alone therefore never proves SELL protection.
                updates['reconciliation_status'] = 'ORDER_LIST_EXECUTING_PHASE_UNPROVEN'
            elif list_status == 'ALL_DONE' or order_status == 'ALL_DONE':
                updates['reconciliation_status'] = 'ORDER_LIST_TERMINAL_RECONCILE_REQUIRED'
            elif list_status == 'RESPONSE' or order_status == 'REJECT':
                updates['reconciliation_status'] = 'ORDER_LIST_ACTION_REJECTED_RECONCILE_REQUIRED'

        if lifecycle:
            old = str(row['lifecycle_state'])
            terminal = {LifecycleState.EXIT_FILLED.value, LifecycleState.ENTRY_REJECTED.value}
            if old not in terminal and (
                old == LifecycleState.RECONCILED.value
                or self._state_rank(lifecycle) >= self._state_rank(old)
            ):
                updates['lifecycle_state'] = lifecycle
        return updates

    def _apply_event(self, con, row, event: dict[str, Any], event_key: str) -> None:
        updates = self._event_updates(row, event, event_key)
        set_sql = ','.join(f'{key}=?' for key in updates)
        con.execute(
            f'UPDATE trade_records SET {set_sql} WHERE trade_id=?',  # nosec B608
            [*updates.values(), row['trade_id']],
        )

    @staticmethod
    def _matching_trade_rows(con, order_id, order_list_id):
        clauses, values = [], []
        if order_id not in (None, -1, '-1'):
            clauses.append('(entry_order_id=? OR take_profit_order_id=? OR stop_order_id=?)')
            values.extend([order_id, order_id, order_id])
        if order_list_id not in (None, -1, '-1'):
            clauses.append('order_list_id=?')
            values.append(order_list_id)
        if not clauses:
            return []
        return con.execute(
            'SELECT * FROM trade_records WHERE ' + ' OR '.join(clauses), values  # nosec B608
        ).fetchall()

    def record_exchange_event(self, event: dict[str, Any]) -> bool:
        """Persist and apply one event exactly once; unmatched races stay replayable."""
        (event_type, order_id, order_list_id, event_time, _raw_id, symbol,
         event_key) = self._event_identity(event)
        commission_asset = event.get('N') or event.get('commissionAsset')
        commission_amount = event.get('n') or event.get('commission')
        with self._connect() as con:
            try:
                con.execute(
                    '''INSERT INTO exchange_events(event_key,event_type,symbol,order_id,order_list_id,
                       commission_asset,commission_amount,event_time,payload_json)
                       VALUES(?,?,?,?,?,?,?,?,?)''',
                    (event_key, event_type, symbol, order_id, order_list_id,
                     commission_asset, commission_amount, str(event_time or self._now()),
                     json.dumps(event, sort_keys=True, default=str)),
                )
            except sqlite3.IntegrityError:
                return False
            for row in self._matching_trade_rows(con, order_id, order_list_id):
                self._apply_event(con, row, event, event_key)
        return True

    def replay_exchange_events_for_trade(self, trade_id: str) -> int:
        """Apply raw events that arrived before the REST response bound IDs."""
        applied = 0
        with self._connect() as con:
            row = con.execute('SELECT * FROM trade_records WHERE trade_id=?', (trade_id,)).fetchone()
            if not row:
                return 0
            order_ids = [
                value for value in (
                    row['entry_order_id'], row['take_profit_order_id'], row['stop_order_id']
                ) if value not in (None, '', 0, '0')
            ]
            list_id = row['order_list_id']
            clauses, params = [], []
            if order_ids:
                clauses.append('order_id IN (' + ','.join('?' for _ in order_ids) + ')')
                params.extend(order_ids)
            if list_id not in (None, '', 0, '0'):
                clauses.append('order_list_id=?')
                params.append(list_id)
            if not clauses:
                return 0
            events = con.execute(
                'SELECT event_key,payload_json FROM exchange_events WHERE '
                + ' OR '.join(clauses) + ' ORDER BY id', params  # nosec B608
            ).fetchall()
            for stored in events:
                event = json.loads(stored['payload_json'])
                current = con.execute(
                    'SELECT * FROM trade_records WHERE trade_id=?', (trade_id,)
                ).fetchone()
                if current:
                    self._apply_event(con, current, event, str(stored['event_key']))
                    applied += 1
        return applied
