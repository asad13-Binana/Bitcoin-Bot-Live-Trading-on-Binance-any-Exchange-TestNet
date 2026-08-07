#!/usr/bin/env python3
from __future__ import annotations
"""Build or verify deterministic file/function audit ledgers."""

import argparse
import ast
import csv
import hashlib
import io
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FUNCTION_MATRIX = ROOT / "docs/audit/FUNCTION_PARITY_MATRIX.csv"
FILE_LEDGER = ROOT / "docs/audit/FILE_REVIEW_LEDGER.csv"
EXCLUDED_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
SHELL_FUNCTION = re.compile(
    r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?\s*\{"
)
PROTECTED_METHODS = {
    "IctSmcStrategy.populate_indicators_5m",
    "IctSmcStrategy.populate_indicators",
    "IctSmcStrategy.populate_entry_trend",
    "IctSmcStrategy.populate_exit_trend",
}
MATRIX_FIELDS = (
    "path",
    "qualified_name",
    "line",
    "end_line",
    "kind",
    "source_sha256",
    "disposition",
)


def release_paths() -> set[str]:
    paths = set()
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            raise ValueError(
                f"symlink is not allowed in source release: "
                f"{path.relative_to(ROOT)}"
            )
        if not path.is_file() or any(
            part in EXCLUDED_PARTS for part in path.parts
        ):
            continue
        paths.add(path.relative_to(ROOT).as_posix())
    return paths


class FunctionVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, source: str):
        self.path = path
        self.source = source
        self.scope: list[str] = []
        self.rows: list[dict[str, str]] = []

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        qualified = ".".join([*self.scope, node.name])
        segment = ast.get_source_segment(self.source, node)
        if segment is None:
            lines = self.source.splitlines(keepends=True)
            segment = "".join(lines[node.lineno - 1:node.end_lineno])
        self.rows.append({
            "path": self.path.relative_to(ROOT).as_posix(),
            "qualified_name": qualified,
            "line": str(node.lineno),
            "end_line": str(node.end_lineno),
            "kind": (
                "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            ),
            "source_sha256": hashlib.sha256(
                segment.encode("utf-8")
            ).hexdigest(),
            "disposition": (
                "FINGERPRINT_PROTECTED"
                if qualified in PROTECTED_METHODS
                else "PRESERVE_AND_REGRESSION_TEST"
            ),
        })
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def python_function_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(ROOT.rglob("*.py")):
        if (
            not path.is_file()
            or any(part in EXCLUDED_PARTS for part in path.parts)
            or "tests" in path.relative_to(ROOT).parts
        ):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        visitor = FunctionVisitor(path, source)
        visitor.visit(tree)
        rows.extend(visitor.rows)
    return rows


def shell_function_rows() -> list[dict[str, str]]:
    """Inventory Bash callables; the manifest supplies exact whole-file integrity."""
    rows: list[dict[str, str]] = []
    for path in sorted(ROOT.rglob("*")):
        if (
            not path.is_file()
            or any(part in EXCLUDED_PARTS for part in path.parts)
            or "tests" in path.relative_to(ROOT).parts
        ):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        first = source.splitlines()[:1]
        if path.suffix != ".sh" and not (
            first and first[0] in {"#!/usr/bin/env bash", "#!/bin/bash"}
        ):
            continue
        file_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        for number, line in enumerate(source.splitlines(), 1):
            match = SHELL_FUNCTION.match(line)
            if not match:
                continue
            rows.append({
                "path": path.relative_to(ROOT).as_posix(),
                "qualified_name": match.group(1),
                "line": str(number),
                "end_line": str(number),
                "kind": "shell_function",
                "source_sha256": file_hash,
                "disposition": "PRESERVE_AND_REGRESSION_TEST",
            })
    return rows


def function_rows() -> list[dict[str, str]]:
    rows = python_function_rows() + shell_function_rows()
    return sorted(
        rows,
        key=lambda row: (
            row["path"],
            int(row["line"]),
            row["qualified_name"],
        ),
    )


def matrix_text(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=MATRIX_FIELDS,
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def verify_file_ledger() -> None:
    with FILE_LEDGER.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ledger_paths = [str(row.get("path", "")) for row in rows]
    duplicates = sorted({
        path for path in ledger_paths if ledger_paths.count(path) > 1
    })
    if duplicates:
        raise ValueError(f"file-review ledger has duplicate paths: {duplicates}")
    expected = release_paths()
    actual = set(ledger_paths)
    missing = sorted(expected - actual)
    stale = sorted(actual - expected)
    if missing or stale:
        raise ValueError(
            f"file-review ledger mismatch; missing={missing}; stale={stale}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify both ledgers without rewriting them",
    )
    args = parser.parse_args()
    rows = function_rows()
    expected = matrix_text(rows)
    if args.check:
        actual = FUNCTION_MATRIX.read_text(encoding="utf-8")
        if actual != expected:
            raise SystemExit(
                "function-parity matrix is stale; run "
                "python scripts/build_audit_ledgers.py"
            )
        try:
            verify_file_ledger()
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            f"audit ledgers verified: {len(release_paths())} files; "
            f"{len(rows)} non-test Python and shell functions"
        )
        return 0
    FUNCTION_MATRIX.parent.mkdir(parents=True, exist_ok=True)
    FUNCTION_MATRIX.write_text(expected, encoding="utf-8", newline="\n")
    print(
        f"wrote {FUNCTION_MATRIX} with "
        f"{len(rows)} non-test Python and shell functions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
