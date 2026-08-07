from __future__ import annotations

import time
from pathlib import Path


def prune_files(directory: str | Path, pattern: str = '*', *, max_files: int = 0,
                max_age_seconds: int = 0) -> int:
    """Best-effort bounded retention for non-authoritative file archives.

    Database state and authoritative active-pair files are never handled here.
    A limit of zero disables that specific retention rule.
    """
    root = Path(directory)
    if not root.exists():
        return 0
    try:
        files = [path for path in root.glob(pattern) if path.is_file()]
    except OSError:
        return 0
    removed = 0
    now = time.time()
    if max_age_seconds > 0:
        for path in list(files):
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    path.unlink(missing_ok=True)
                    files.remove(path)
                    removed += 1
            except OSError:
                continue
    if max_files > 0 and len(files) > max_files:
        try:
            files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
        except OSError:
            files.sort(key=lambda path: path.name)
        for path in files[:max(0, len(files) - max_files)]:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    return removed
