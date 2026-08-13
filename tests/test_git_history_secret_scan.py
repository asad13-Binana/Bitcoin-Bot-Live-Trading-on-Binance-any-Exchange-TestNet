from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.scan_git_history_secrets import scan_history


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)


def test_deleted_historical_credential_is_still_detected(tmp_path: Path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "history-scan@example.invalid")
    _git(tmp_path, "config", "user.name", "History Scan Test")
    target = tmp_path / "runtime.env"
    target.write_text("BINANCE_API_KEY=CHANGE_ME\n", encoding="utf-8")
    _commit(tmp_path, "safe")
    target.write_text("BINANCE_API_KEY=historical-value-1234567890\n", encoding="utf-8")
    _commit(tmp_path, "unsafe")
    target.write_text("BINANCE_API_KEY=CHANGE_ME\n", encoding="utf-8")
    _commit(tmp_path, "removed")

    findings = scan_history(tmp_path)
    assert any("runtime.env" in finding and "BINANCE_API_KEY" in finding for finding in findings)
    assert all("historical-value" not in finding for finding in findings)


def test_placeholder_only_history_passes(tmp_path: Path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "history-scan@example.invalid")
    _git(tmp_path, "config", "user.name", "History Scan Test")
    (tmp_path / "runtime.env").write_text(
        "BINANCE_API_KEY=CHANGE_ME\nTELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "placeholder")
    assert scan_history(tmp_path) == []
