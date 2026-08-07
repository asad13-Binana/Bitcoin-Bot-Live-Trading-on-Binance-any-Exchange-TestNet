from pathlib import Path

from tests.secret_scan import scan


def test_secret_scan_allows_indirect_shell_secret_assignment(tmp_path: Path):
    (tmp_path / "install.sh").write_text(
        'COMMAND_HMAC_KEY="$OLD_COMMAND_HMAC_KEY" command --check\n'
        'SIGNAL_HMAC_KEY: ${SIGNAL_HMAC_KEY:?set SIGNAL_HMAC_KEY}\n',
        encoding="utf-8",
    )

    assert scan(tmp_path) == []


def test_secret_scan_rejects_literal_sensitive_assignment(tmp_path: Path):
    (tmp_path / "unsafe.env").write_text(
        'COMMAND_HMAC_KEY="this-is-a-hard-coded-secret"\n',
        encoding="utf-8",
    )

    assert scan(tmp_path) == [
        "unsafe.env:1:non-placeholder COMMAND_HMAC_KEY"
    ]


def test_secret_scan_rejects_variable_reference_with_literal_secret_suffix(tmp_path: Path):
    (tmp_path / "unsafe.env").write_text(
        'COMMAND_HMAC_KEY="${OLD_COMMAND_HMAC_KEY}literal-secret-suffix"\n',
        encoding="utf-8",
    )

    assert scan(tmp_path) == [
        "unsafe.env:1:non-placeholder COMMAND_HMAC_KEY"
    ]
