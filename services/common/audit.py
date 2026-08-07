from __future__ import annotations
import json, logging, os, threading
from datetime import datetime, timezone
from pathlib import Path

from services.common.redaction import redact, redact_text

# Cross-process advisory locking. Oracle/Docker (the deployment target) uses
# Linux fcntl.flock exactly as before; the msvcrt fallback provides the same
# exclusive-lock semantics so the release test suite can also run on Windows
# development hosts. Behavior on the deployment platform is unchanged.
try:
    import fcntl

    def _lock_exclusive(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock_exclusive(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

_LOCK = threading.Lock()


def _rotate(target: Path, incoming_bytes: int) -> None:
    max_bytes = max(0, int(os.getenv('AUDIT_LOG_MAX_BYTES', str(10 * 1024 * 1024))))
    backups = max(0, int(os.getenv('AUDIT_LOG_BACKUPS', '5')))
    if max_bytes <= 0 or backups <= 0:
        return
    try:
        current = target.stat().st_size if target.exists() else 0
    except OSError:
        current = 0
    if current + incoming_bytes <= max_bytes:
        return
    oldest = target.with_name(f'{target.name}.{backups}')
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = target.with_name(f'{target.name}.{index}')
        if source.exists():
            source.replace(target.with_name(f'{target.name}.{index + 1}'))
    if target.exists():
        target.replace(target.with_name(f'{target.name}.1'))


def audit(event: str, *, severity: str='INFO', actor: str='system', details=None, path: str | None=None):
    target = Path(path or os.getenv('AUDIT_LOG', '/app/shared/audit/events.jsonl'))
    row = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': event,
        'severity': severity,
        'actor': actor,
        'details': redact(details or {}),
    }
    line = json.dumps(row, sort_keys=True, default=str) + '\n'
    encoded_size = len(line.encode('utf-8'))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(target.name + '.lock')
        with _LOCK, lock_path.open('a+', encoding='utf-8') as process_lock:
            # Independent containers append to the same shared audit file.
            # A process-local threading.Lock cannot serialize rotation across
            # them, so use the platform advisory lock (fcntl on Oracle/Docker).
            _lock_exclusive(process_lock)
            try:
                _rotate(target, encoded_size)
                with target.open('a', encoding='utf-8') as f:
                    f.write(line); f.flush(); os.fsync(f.fileno())
            finally:
                _unlock(process_lock)
    except OSError as exc:
        # Audit storage failure must be visible, but it must not terminate the
        # order supervisor or Telegram health loop after an exchange side effect.
        logging.getLogger('audit').error('audit write failed for %s: %s', event, redact_text(exc))
        row['audit_write_error'] = redact_text(exc)
    return row
