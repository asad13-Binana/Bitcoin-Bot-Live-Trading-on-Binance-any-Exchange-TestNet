#!/usr/bin/env python3
"""Root-only ownership repair for quiescent Ubuntu deployment trees.

No shell traversal or pathname-based chown. Linux openat2 rejects symlink
traversal and mount crossings (including same-device bind mounts); fchownat
changes the opened object itself. Unsupported kernels fail, with no fallback.
Run only during stopped-writer maintenance: this is not an atomic filesystem
snapshot and does not defend against a concurrent privileged host administrator.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
import os
from pathlib import Path
import platform
import stat
import sys

# Linux UAPI: these syscall/flag values are identical on amd64 and arm64.
SYS_OPENAT2 = 437
RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
AT_SYMLINK_NOFOLLOW = 0x100
MAX_ENTRIES = 250_000


class OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class LinuxOwnership:
    def __init__(self) -> None:
        if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
            raise ValueError("ownership setup requires Linux amd64 or arm64")
        if os.geteuid() != 0:
            raise ValueError("ownership setup requires root")
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long
        self.libc.fchownat.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
        ]
        self.libc.fchownat.restype = ctypes.c_int

    def open(self, parent: int, path: str, flags: int, *, crossing: bool = False) -> int:
        resolve = RESOLVE_NO_SYMLINKS
        if not crossing:
            resolve |= RESOLVE_NO_XDEV | RESOLVE_BENEATH
        how = OpenHow(flags | os.O_CLOEXEC | os.O_NOFOLLOW, 0, resolve)
        result = self.libc.syscall(
            ctypes.c_long(SYS_OPENAT2), ctypes.c_int(parent),
            ctypes.c_char_p(os.fsencode(path)), ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
        if result < 0:
            code = ctypes.get_errno()
            raise OSError(code, "safe ownership open rejected: " + os.strerror(code), path)
        return int(result)

    def chown(self, fd: int, uid: int, gid: int) -> None:
        # Empty-path operation never re-resolves a potentially replaced name.
        if self.libc.fchownat(fd, b"", uid, gid, AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW):
            code = ctypes.get_errno()
            raise OSError(code, "descriptor ownership change failed: " + os.strerror(code))


def identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def validate_object(info: os.stat_result) -> None:
    if not any(check(info.st_mode) for check in (stat.S_ISDIR, stat.S_ISREG, stat.S_ISLNK)):
        raise ValueError("ownership tree contains a special file")
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink != 1:
        raise ValueError("ownership tree contains a hard-linked or unlinked file")


def open_root(api: LinuxOwnership, path: Path) -> int:
    raw = os.fspath(path)
    if not path.is_absolute() or len(path.parts) < 3 or os.path.normpath(raw) != raw:
        raise ValueError("ownership root must be an explicit absolute deployment directory")
    # Ancestor mounts may be legitimate (for example a separate /var volume).
    # No ancestor symlink is allowed; the selected root itself must not be a mount.
    parent = api.open(AT_FDCWD, str(path.parent), os.O_RDONLY | os.O_DIRECTORY, crossing=True)
    try:
        return api.open(parent, path.name, os.O_RDONLY | os.O_DIRECTORY)
    finally:
        os.close(parent)


def inventory(api: LinuxOwnership, root_fd: int) -> list[tuple[str, tuple[int, int, int]]]:
    pending = ["."]
    result = []
    while pending:
        relative = pending.pop()
        fd = api.open(root_fd, relative, os.O_PATH)
        try:
            info = os.fstat(fd)
            validate_object(info)
            result.append((relative, identity(info)))
            if len(result) > MAX_ENTRIES:
                raise ValueError("ownership tree exceeds the bounded inventory")
            if stat.S_ISDIR(info.st_mode):
                directory = api.open(root_fd, relative, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    if identity(os.fstat(directory)) != identity(info):
                        raise ValueError("ownership directory changed during inventory")
                    for name in sorted(os.listdir(directory), reverse=True):
                        pending.append(name if relative == "." else relative + "/" + name)
                finally:
                    os.close(directory)
        finally:
            os.close(fd)
    return result


def change_ownership(paths: list[Path], uid: int, gid: int) -> int:
    if not paths or any(type(value) is not int or not 0 <= value < 2**32 - 1
                        for value in (uid, gid)):
        raise ValueError("explicit roots and valid numeric owner/group are required")
    api = LinuxOwnership()
    plans = []
    with ExitStack() as descriptors:
        # Validate ALL trees before any ownership mutation. A static foreign
        # mount, link escape or special object causes no ownership changes.
        for path in paths:
            fd = open_root(api, path)
            descriptors.callback(os.close, fd)
            plans.append((fd, inventory(api, fd)))
        changed = 0
        for root_fd, entries in plans:
            for relative, expected in entries:
                fd = api.open(root_fd, relative, os.O_PATH)
                try:
                    info = os.fstat(fd)
                    validate_object(info)
                    if identity(info) != expected:
                        raise ValueError("ownership object changed after inventory")
                    api.chown(fd, uid, gid)
                    final = os.fstat(fd)
                    if (final.st_uid, final.st_gid) != (uid, gid):
                        raise ValueError("ownership verification failed")
                    changed += 1
                finally:
                    os.close(fd)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="existing user:group or numeric uid:gid")
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        import grp
        import pwd

        user, group = args.owner.split(":")
        uid = int(user) if user.isdecimal() else pwd.getpwnam(user).pw_uid
        gid = int(group) if group.isdecimal() else grp.getgrnam(group).gr_gid
        count = change_ownership(args.roots, uid, gid)
    except (ImportError, KeyError, OSError, ValueError) as exc:
        print(f"ERROR: ownership setup refused: {exc}", file=sys.stderr)
        return 1
    print(f"descriptor-safe ownership verified: {count} objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
