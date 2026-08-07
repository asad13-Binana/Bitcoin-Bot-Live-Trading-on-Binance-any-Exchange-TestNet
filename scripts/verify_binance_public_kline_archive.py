#!/usr/bin/env python3
"""Verify an official Binance Spot kline ZIP without extracting or rewriting it.

The official binance-public-data repository publishes one adjacent CHECKSUM
file per archive and uses microsecond timestamps for Spot data from 2025-01-01
onward. This tool verifies both the content hash and the kline schema so a
download cannot silently enter a backtest with corruption or a misread time
unit.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile


class ArchiveValidationError(RuntimeError):
    """The archive or its checksum is unsafe, malformed, or inconsistent."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_checksum(checksum_path: Path, archive_name: str) -> str:
    try:
        lines = [
            line.strip()
            for line in checksum_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        raise ArchiveValidationError(
            f"cannot read checksum file {checksum_path}: {exc}"
        ) from exc
    if len(lines) != 1:
        raise ArchiveValidationError("CHECKSUM must contain exactly one nonblank line")
    match = re.fullmatch(r"([0-9A-Fa-f]{64})\s+[*]?(.+)", lines[0])
    if not match:
        raise ArchiveValidationError("CHECKSUM line is not valid sha256sum format")
    declared_name = match.group(2).strip()
    if declared_name != archive_name:
        raise ArchiveValidationError(
            f"CHECKSUM names {declared_name!r}, expected {archive_name!r}"
        )
    return match.group(1).lower()


def safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    files: list[zipfile.ZipInfo] = []
    exact_names: set[str] = set()
    folded_names: set[str] = set()
    for item in archive.infolist():
        name = item.filename
        if not name or "\\" in name or "\x00" in name:
            raise ArchiveValidationError(f"unsafe ZIP member name: {name!r}")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ArchiveValidationError(f"unsafe ZIP member path: {name!r}")
        if path.parts and ":" in path.parts[0]:
            raise ArchiveValidationError(f"drive-qualified ZIP member: {name!r}")
        normalized = "/".join(path.parts)
        folded = normalized.casefold()
        if normalized in exact_names or folded in folded_names:
            raise ArchiveValidationError(f"duplicate ZIP member: {name!r}")
        exact_names.add(normalized)
        folded_names.add(folded)
        if item.flag_bits & 0x1:
            raise ArchiveValidationError(f"encrypted ZIP member is not allowed: {name!r}")
        mode = (item.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ArchiveValidationError(f"non-regular ZIP member is not allowed: {name!r}")
        if not item.is_dir():
            files.append(item)
    csv_files = [item for item in files if item.filename.lower().endswith(".csv")]
    if len(files) != 1 or len(csv_files) != 1:
        raise ArchiveValidationError(
            "official kline ZIP must contain exactly one regular CSV file"
        )
    return csv_files


def timestamp_unit(value: int) -> str:
    magnitude = abs(value)
    if magnitude >= 1_000_000_000_000_000:
        return "microseconds"
    if magnitude >= 1_000_000_000_000:
        return "milliseconds"
    raise ArchiveValidationError(f"timestamp magnitude is unsupported: {value}")


def decimal_value(raw: str, label: str, *, positive: bool = False) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ArchiveValidationError(f"{label} is not decimal: {raw!r}") from exc
    if not value.is_finite():
        raise ArchiveValidationError(f"{label} is not finite: {raw!r}")
    if positive and value <= 0:
        raise ArchiveValidationError(f"{label} must be positive: {raw!r}")
    if not positive and value < 0:
        raise ArchiveValidationError(f"{label} must be nonnegative: {raw!r}")
    return value


def inspect_kline_csv(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo
) -> dict[str, object]:
    row_count = 0
    first_open: int | None = None
    last_open: int | None = None
    detected_unit: str | None = None
    try:
        with archive.open(member, "r") as binary:
            with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
                for row_count, row in enumerate(csv.reader(text), start=1):
                    if len(row) != 12:
                        raise ArchiveValidationError(
                            f"CSV row {row_count} has {len(row)} fields, expected 12"
                        )
                    try:
                        open_time = int(row[0])
                        close_time = int(row[6])
                        trades = int(row[8])
                    except (TypeError, ValueError) as exc:
                        raise ArchiveValidationError(
                            f"CSV row {row_count} has malformed integer fields"
                        ) from exc
                    unit = timestamp_unit(open_time)
                    if timestamp_unit(close_time) != unit:
                        raise ArchiveValidationError(
                            f"CSV row {row_count} mixes timestamp units"
                        )
                    if detected_unit is None:
                        detected_unit = unit
                    elif detected_unit != unit:
                        raise ArchiveValidationError(
                            f"CSV row {row_count} changes timestamp unit"
                        )
                    if close_time < open_time:
                        raise ArchiveValidationError(
                            f"CSV row {row_count} closes before it opens"
                        )
                    if last_open is not None and open_time <= last_open:
                        raise ArchiveValidationError(
                            f"CSV row {row_count} open time is not strictly increasing"
                        )
                    if trades < 0:
                        raise ArchiveValidationError(
                            f"CSV row {row_count} has a negative trade count"
                        )
                    open_price = decimal_value(row[1], "open", positive=True)
                    high_price = decimal_value(row[2], "high", positive=True)
                    low_price = decimal_value(row[3], "low", positive=True)
                    close_price = decimal_value(row[4], "close", positive=True)
                    for index, label in (
                        (5, "volume"),
                        (7, "quote volume"),
                        (9, "taker buy base volume"),
                        (10, "taker buy quote volume"),
                        (11, "ignore"),
                    ):
                        decimal_value(row[index], label)
                    if high_price < max(open_price, low_price, close_price):
                        raise ArchiveValidationError(
                            f"CSV row {row_count} high is below an OHLC value"
                        )
                    if low_price > min(open_price, high_price, close_price):
                        raise ArchiveValidationError(
                            f"CSV row {row_count} low is above an OHLC value"
                        )
                    if first_open is None:
                        first_open = open_time
                    last_open = open_time
    except (UnicodeDecodeError, csv.Error, zipfile.BadZipFile) as exc:
        raise ArchiveValidationError(f"cannot decode or verify kline CSV: {exc}") from exc
    if row_count == 0 or first_open is None or last_open is None or detected_unit is None:
        raise ArchiveValidationError("kline CSV is empty")
    return {
        "csv_member": member.filename,
        "rows": row_count,
        "timestamp_unit": detected_unit,
        "first_open_time": first_open,
        "last_open_time": last_open,
    }


def verify_archive(archive_path: Path, checksum_path: Path) -> dict[str, object]:
    archive_path = archive_path.resolve(strict=True)
    checksum_path = checksum_path.resolve(strict=True)
    expected = expected_checksum(checksum_path, archive_path.name)
    actual = file_sha256(archive_path)
    if actual != expected:
        raise ArchiveValidationError(
            f"SHA-256 mismatch: expected {expected}, calculated {actual}"
        )
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = safe_members(archive)
            details = inspect_kline_csv(archive, members[0])
    except zipfile.BadZipFile as exc:
        raise ArchiveValidationError(f"invalid ZIP archive: {exc}") from exc
    return {
        "ok": True,
        "archive": archive_path.name,
        "checksum_file": checksum_path.name,
        "sha256": actual,
        **details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an official Binance Spot kline ZIP and CHECKSUM."
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--checksum",
        type=Path,
        help="Adjacent .CHECKSUM path (default: <archive>.CHECKSUM)",
    )
    args = parser.parse_args(argv)
    checksum = args.checksum or Path(str(args.archive) + ".CHECKSUM")
    try:
        result = verify_archive(args.archive, checksum)
    except (ArchiveValidationError, FileNotFoundError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
