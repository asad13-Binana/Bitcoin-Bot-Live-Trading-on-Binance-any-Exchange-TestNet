"""Strictly read-only SQLite helpers for execution and signal data."""
from __future__ import annotations

import sqlite3
from pathlib import Path


_READ_PREFIXES = ("SELECT", "WITH", "PRAGMA")
_WRITE_WORDS = (
    " INSERT ", " UPDATE ", " DELETE ", " REPLACE ", " DROP ",
    " ALTER ", " CREATE ", " ATTACH ", " DETACH ", " VACUUM ",
)


def _read_only_sql(sql: str) -> bool:
    normalized = " " + " ".join(str(sql).strip().upper().split()) + " "
    return normalized.strip().startswith(_READ_PREFIXES) and not any(
        word in normalized for word in _WRITE_WORDS
    )


def query(path: Path, sql: str, args=()):
    """Return ``(rows, error)`` while opening SQLite with ``mode=ro``."""
    path = Path(path)
    if not _read_only_sql(sql):
        return [], "query_not_read_only"
    if not path.is_file():
        return [], "database_missing"
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in connection.execute(sql, args).fetchall()]
            return rows, None
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return [], f"database_error:{type(exc).__name__}"


def status(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"present": False, "integrity": "unavailable", "reason": "database_missing"}
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2
        )
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            ok = bool(result) and str(result[0]).lower() == "ok"
        finally:
            connection.close()
        return {
            "present": True,
            "integrity": "ok" if ok else "failed",
            "size_bytes": path.stat().st_size,
            "modified_at_epoch": path.stat().st_mtime,
        }
    except (OSError, sqlite3.Error) as exc:
        return {
            "present": True,
            "integrity": "unavailable",
            "reason": f"database_error:{type(exc).__name__}",
        }
