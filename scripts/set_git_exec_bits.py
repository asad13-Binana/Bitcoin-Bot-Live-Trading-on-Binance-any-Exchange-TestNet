#!/usr/bin/env python3
"""Record the release executable bits in the Git index.

Git for Windows sets core.filemode=false, so `git add -A` records every file as
mode 100644 and the executable bit of release executables is lost. A
clone of that repository onto the Oracle host produces a tree where
`./deploy/oracle_setup.sh` and `./deploy/install_artifact.sh` fail with
"Permission denied".

RELEASE_MANIFEST.json pins content hashes and sizes only -- it does not record
permission modes -- so nothing else in the release chain detects this.

This script applies `git update-index --chmod=+x` to exactly the paths that
scripts/build_release_zip.py stores with mode 0755, so the two definitions
cannot drift apart. It is idempotent and safe to re-run.

Usage:
    python scripts/set_git_exec_bits.py            # apply
    python scripts/set_git_exec_bits.py --check    # verify only, exit 1 on drift
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Mirrors the mode rule in scripts/build_release_zip.py.
EXPLICIT_EXECUTABLES = {
    "scripts/build_release_zip.py",
    "scripts/build_manifest.py",
    "scripts/verify_manifest.py",
    "scripts/certify_live_evidence.py",
    "scripts/verify_live_evidence.py",
    "deploy/verify_stack_identity.py",
}


def git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def tracked_files() -> list[str]:
    result = git("ls-files")
    if result.returncode != 0:
        print(
            "not a Git repository (or git is unavailable); run this from the "
            "repository root after 'git init' and 'git add -A'",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return [line for line in result.stdout.splitlines() if line]


def expected_executables(tracked: list[str]) -> list[str]:
    return sorted(
        path
        for path in tracked
        if path.endswith(".sh") or path in EXPLICIT_EXECUTABLES
    )


def indexed_modes() -> dict[str, str]:
    result = git("ls-files", "-s")
    modes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if parts:
            modes[path] = parts[0]
    return modes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without modifying the index",
    )
    args = parser.parse_args()

    tracked = tracked_files()
    expected = expected_executables(tracked)
    if not expected:
        print("no release executables are tracked yet; run 'git add -A' first")
        return 1

    modes = indexed_modes()
    wrong = [path for path in expected if modes.get(path) != "100755"]

    if args.check:
        if wrong:
            print(
                f"{len(wrong)} of {len(expected)} release executables are not "
                "mode 100755 in the Git index:",
                file=sys.stderr,
            )
            for path in wrong:
                print(f"  {modes.get(path, 'untracked')}  {path}", file=sys.stderr)
            print(
                "run: python scripts/set_git_exec_bits.py",
                file=sys.stderr,
            )
            return 1
        print(f"all {len(expected)} release executables are mode 100755 in the index")
        return 0

    if not wrong:
        print(f"all {len(expected)} release executables already mode 100755")
        return 0

    result = git("update-index", "--chmod=+x", *wrong)
    if result.returncode != 0:
        print(
            f"git update-index failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    remaining = [
        path for path in expected if indexed_modes().get(path) != "100755"
    ]
    if remaining:
        print(
            f"executable bits still missing for: {', '.join(remaining)}",
            file=sys.stderr,
        )
        return 1

    print(f"set mode 100755 on {len(wrong)} release executable(s) in the Git index")
    print("commit the result so clones on Linux receive runnable scripts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
