from __future__ import annotations
import json, os, tempfile
from pathlib import Path
from typing import Any


def _fsync_parent_directory(parent: Path) -> None:
    """Persist a directory entry where the platform supports that primitive."""
    if os.name != 'posix':
        return
    try:
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Some POSIX filesystems do not support directory fsync.
        pass


def atomic_write_json(path: str | Path, payload: Any, *, mode: int = 0o640) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, sort_keys=True, default=str)
            f.flush(); os.fsync(f.fileno())
        # Shared operational projections and SQLite-adjacent state are readable
        # by the dedicated monitor group, never world-readable. Directory
        # ownership/ACLs still decide which principals can reach the file.
        os.chmod(tmp, mode)
        os.replace(tmp, target)
        # Persist the directory entry as well as the file contents. This reduces
        # the chance of losing a just-replaced state file during a host crash.
        # Directory fsync is a POSIX durability primitive. Windows does not
        # support it reliably and can block indefinitely when os.open() is used
        # on a directory, so retain it only on the Oracle/Linux runtime path.
        _fsync_parent_directory(target.parent)
    finally:
        try:
            if os.path.exists(tmp): os.unlink(tmp)
        except OSError:
            pass


def read_json(path: str | Path, default=None):
    try:
        with Path(path).open(encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default
