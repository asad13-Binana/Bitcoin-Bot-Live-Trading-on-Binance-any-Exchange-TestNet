#!/usr/bin/env python3
from __future__ import annotations
"""Build the deterministic source ZIP and its adjacent SHA-256 file."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_files() -> list[str]:
    manifest = json.loads((ROOT / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    files = set(manifest.get("files", {}))
    files.update({"RELEASE_MANIFEST.json", "RELEASE_SHA256.txt"})
    if not files or any(
        PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts
        or "\\" in name
        for name in files
    ):
        raise ValueError("manifest contains an unsafe release path")
    return sorted(files)


def ensure_external_output(output: Path) -> Path:
    resolved = output.resolve()
    if resolved.suffix.lower() != ".zip":
        raise ValueError("output must end in .zip")
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        return resolved
    raise ValueError("write release artifacts outside the immutable source tree")


def build(output: Path) -> tuple[Path, Path, int]:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_manifest.py")],
        cwd=ROOT,
        check=True,
    )
    output = ensure_external_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    files = release_files()
    package_mode = (ROOT / "RELEASE_MODE").read_text(encoding="utf-8").strip()
    archive_root = (
        "bitcoin-bot-live-trading"
        if package_mode == "live"
        else "bitcoin-bot-testnet"
    )
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for relative in files:
            source = ROOT / Path(*PurePosixPath(relative).parts)
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"release member is missing or a symlink: {relative}")
            info = zipfile.ZipInfo(f"{archive_root}/{relative}", FIXED_TIME)
            info.create_system = 3
            mode = 0o755 if source.suffix == ".sh" or relative in {
                "scripts/build_release_zip.py",
                "scripts/build_manifest.py",
                "scripts/verify_manifest.py",
                "scripts/certify_live_evidence.py",
                "scripts/verify_live_evidence.py",
                "deploy/verify_stack_identity.py",
            } else 0o644
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes(), compresslevel=9)

    with zipfile.ZipFile(output, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise ValueError("fresh ZIP failed duplicate-name or CRC validation")
        expected = {f"{archive_root}/{name}" for name in files}
        if set(names) != expected:
            raise ValueError("fresh ZIP member set differs from the manifest")

    checksum = output.with_name(output.name + ".sha256")
    checksum.write_text(f"{sha256(output)}  {output.name}\n", encoding="ascii", newline="\n")
    return output, checksum, len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        output, checksum, count = build(args.output)
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        print(f"release ZIP build failed: {exc}", file=sys.stderr)
        return 1
    print(f"built {output} with {count} files")
    print(f"wrote {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
