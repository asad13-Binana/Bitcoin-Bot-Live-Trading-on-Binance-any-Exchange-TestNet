#!/usr/bin/env python3
"""Create missing reboot-ephemeral locks; never repair or replace unsafe files.

The CLI accepts no paths or environment overrides. It reads this release's
immutable identity as data, and keeps the same inodes as Ubuntu's /var/lock
alias. All four callers validate before opening their existing flock descriptor.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys


SUFFIXES = {
    "INSTALL_LOCK": "install.lock",
    "BACKUP_LOCK": "backup.lock",
    "OFFHOST_BACKUP_LOCK": "offhost-backup.lock",
    "ACTIONS_LOCK": "actions-deploy.lock",
}


def identity_locks(identity: Path) -> list[Path]:
    if identity.is_symlink() or not identity.is_file():
        raise ValueError("immutable lock identity is missing or a symlink")
    values = {}
    for line in identity.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"readonly ([A-Z][A-Z0-9_]*)=([/A-Za-z0-9._-]+)", line)
        if not match or match[1] in values:
            raise ValueError("invalid or duplicate immutable identity")
        values[match[1]] = match[2]
    slug = values.get("INSTANCE_SLUG")
    if slug not in {"bitcoin-testnet", "bitcoin-live"}:
        raise ValueError("lock identity is not a Bitcoin package")
    paths = []
    for key, suffix in SUFFIXES.items():
        expected = f"/run/lock/{slug}.{suffix}"
        if values.get(key) != expected:
            raise ValueError(f"noncanonical or cross-instance lock: {key}")
        paths.append(Path(expected))
    return paths


def prepare_lock(path: Path) -> None:
    # The sticky bit prevents another uid from replacing our root-owned file.
    # Reject symlinked parents and a non-sticky shared writable lock directory.
    parent = path.parent
    if parent.resolve(strict=True) != parent or parent.is_symlink():
        raise ValueError("lock directory must be canonical")
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0)
            or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
        raise ValueError("unsafe lock directory ownership or mode")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0)
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("lock must be a root:root 0600 single-link regular file")
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("lock changed during validation")
    finally:
        os.close(fd)


def main() -> int:
    if len(sys.argv) != 1 or os.geteuid() != 0:
        print("ERROR: runtime lock preparation requires root and no arguments", file=sys.stderr)
        return 1
    try:
        for path in identity_locks(Path(__file__).with_name("instance_identity.sh")):
            prepare_lock(path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: runtime lock preparation refused: {exc}", file=sys.stderr)
        return 1
    print("canonical Bitcoin runtime locks verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
