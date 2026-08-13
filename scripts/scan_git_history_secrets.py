from __future__ import annotations

"""Fail CI when a high-confidence credential existed in any Git commit.

The normal release scan protects the current tree.  This separate audit walks
added lines from every reachable commit so deleting a credential later does
not make the repository history appear clean.  Findings intentionally omit
the matched value.
"""

import argparse
import re
import subprocess
from pathlib import Path


TOKEN_PATTERNS = (
    ("OpenAI token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Telegram token", re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b")),
)
SENSITIVE_NAMES = {
    "BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_MONITOR_BOT_TOKEN", "MONITOR_TOKEN", "SIGNAL_HMAC_KEY",
    "COMMAND_HMAC_KEY", "FREQTRADE_API_PASSWORD", "FREQTRADE_API_JWT_SECRET",
    "FREQTRADE_API_WS_TOKEN", "LIVE_EVIDENCE_PRIVATE_KEY",
    "ORACLE_SSH_PRIVATE_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN",
    "COINGECKO_API_KEY", "COINMARKETCAP_API_KEY",
}
ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*(?:=|:)\s*[\"']?([^\"'\s#]*)"
)
PLACEHOLDER_MARKERS = (
    "CHANGE_ME", "GENERATE_", "YOUR_", "REPLACE_", "<", "${", "$(",
    "EXAMPLE", "OS.GETENV", "OS.ENVIRON", "GETENV(", "ENVIRON[",
)
SHELL_VARIABLE_REFERENCE = re.compile(
    r"^\$(?:[A-Za-z_][A-Za-z0-9_]*|"
    r"\{[A-Za-z_][A-Za-z0-9_]*(?:(?::?[-+?])[^}]*)?\})$"
)
SKIP_PATHS = {
    "scripts/scan_git_history_secrets.py",
    "tests/secret_scan.py",
    "tests/test_secret_scan.py",
    "tests/test_git_history_secret_scan.py",
}


def _placeholder(value: str) -> bool:
    value = value.strip()
    return (
        not value
        or SHELL_VARIABLE_REFERENCE.fullmatch(value) is not None
        or any(marker in value.upper() for marker in PLACEHOLDER_MARKERS)
    )


def scan_history(root: Path) -> list[str]:
    command = [
        "git", "log", "--all", "--full-history", "--no-renames",
        "--no-ext-diff", "--unified=0", "--no-color",
        "--format=@@COMMIT:%H", "--patch", "--", ".",
    ]
    process = subprocess.run(
        command, cwd=root, text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False, timeout=180,
    )
    if process.returncode != 0:
        raise RuntimeError("git history scan failed: " + process.stderr.strip())

    commit = "unknown"
    path = "unknown"
    findings: set[str] = set()
    for raw in process.stdout.splitlines():
        if raw.startswith("@@COMMIT:"):
            commit = raw.split(":", 1)[1]
            continue
        if raw.startswith("+++ b/"):
            path = raw[6:]
            continue
        if not raw.startswith("+") or raw.startswith("+++") or path in SKIP_PATHS:
            continue
        line = raw[1:]
        match = ASSIGNMENT.match(line)
        if match and match.group(1) in SENSITIVE_NAMES and not _placeholder(match.group(2)):
            findings.add(f"{commit[:12]}:{path}: populated {match.group(1)}")
            continue
        if any(marker in line.upper() for marker in PLACEHOLDER_MARKERS):
            continue
        for label, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                findings.add(f"{commit[:12]}:{path}: {label} pattern")
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    findings = scan_history(args.root.resolve())
    if findings:
        print("FAIL: high-confidence credential material exists in Git history")
        print("\n".join(findings))
        return 1
    print("git history secret scan: no high-confidence credential material found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
