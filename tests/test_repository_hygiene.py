"""Regressions for the Git round trip and the systemd verification contexts.

These cover two defects that packaging checks alone cannot see:

* a clone made with ``core.autocrlf=true`` rewrote 157 of 167 files to CRLF,
  which broke ``scripts/verify_manifest.py`` and left every shell script with a
  ``#!/usr/bin/env bash\\r`` shebang that Linux cannot execute;
* ``deploy/verify_release.sh`` swallowed every non-zero ``systemd-analyze``
  exit with a blanket ``|| echo``, which would equally have hidden a real
  missing executable on the Oracle host.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GITATTRIBUTES = ROOT / ".gitattributes"
VERIFY_RELEASE = ROOT / "deploy/verify_release.sh"
UNIT_VERIFIER = ROOT / "scripts/verify_systemd_units.py"
UNIT_DIR = ROOT / "monitoring/systemd"
WORKFLOW = ROOT / ".github/workflows/ci.yml"

EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}


def release_files() -> list[Path]:
    return [
        path
        for path in sorted(ROOT.rglob("*"))
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.parts)
    ]


# --------------------------------------------------------------------------
# .gitattributes protects the authenticated bytes
# --------------------------------------------------------------------------


def test_gitattributes_exists_and_forces_lf_globally():
    assert GITATTRIBUTES.is_file(), (
        ".gitattributes is required; without it a clone with core.autocrlf=true "
        "rewrites the manifested bytes and breaks the release gate"
    )
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    assert re.search(r"^\*\s+text=auto\s+eol=lf\s*$", text, re.MULTILINE), (
        ".gitattributes must contain a global '* text=auto eol=lf' rule"
    )


@pytest.mark.parametrize(
    "pattern", ["*.sh", "*.py", "*.json", "*.service", "*.timer", "Makefile"]
)
def test_gitattributes_pins_release_critical_types(pattern):
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    escaped = re.escape(pattern)
    assert re.search(rf"^{escaped}\s+text\s+eol=lf\s*$", text, re.MULTILINE), (
        f"{pattern} must be pinned to eol=lf explicitly"
    )


def test_no_release_file_contains_crlf():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in release_files()
        if b"\r\n" in path.read_bytes()
    ]
    assert not offenders, f"files contain CRLF line endings: {offenders}"


def test_shell_scripts_have_lf_shebangs():
    for path in release_files():
        if path.suffix != ".sh" and path.name != "bitcoin-bot-deploy":
            continue
        first = path.read_bytes().split(b"\n", 1)[0]
        assert not first.endswith(b"\r"), (
            f"{path.relative_to(ROOT)} has a CRLF shebang and cannot execute on Linux"
        )


def test_gitattributes_is_manifested():
    import json

    manifest = json.loads((ROOT / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    assert ".gitattributes" in manifest["files"], (
        ".gitattributes must be a manifested release file so it travels with a clone"
    )


def test_setup_python_cache_tracks_the_actual_requirement_files():
    """The setup-python cache must not assume a nonexistent requirements.txt."""
    text = WORKFLOW.read_text(encoding="utf-8")
    expected = {
        "requirements-dev.txt",
        "requirements.services.txt",
        "monitoring/requirements-monitoring-dev.txt",
        "monitoring/requirements-monitoring.txt",
    }
    blocks = re.findall(
        r"cache-dependency-path: \|\n((?:\s{12}[^\n]+\n)+)", text
    )
    assert len(blocks) == 2, "both setup-python steps must declare cache inputs"
    for block in blocks:
        declared = {line.strip() for line in block.splitlines() if line.strip()}
        assert declared == expected
    for relative in expected:
        assert (ROOT / relative).is_file(), f"cache input does not exist: {relative}"


# --------------------------------------------------------------------------
# The release gate no longer blanket-swallows systemd-analyze failures
# --------------------------------------------------------------------------


def test_release_gate_has_no_blanket_systemd_exception():
    text = VERIFY_RELEASE.read_text(encoding="utf-8")
    assert "systemd-analyze reported unresolved install paths" not in text, (
        "the blanket '|| echo' systemd exception must not return; it hid real "
        "missing executables as well as expected pre-install absence"
    )
    assert "SYSTEMD_VERIFY_CONTEXT" in text, (
        "the release gate must select an explicit systemd verification context"
    )
    assert "scripts/verify_systemd_units.py" in text


def test_release_gate_rejects_an_unknown_context():
    text = VERIFY_RELEASE.read_text(encoding="utf-8")
    assert "must be auto, source or installed" in text


def run_verifier(context: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ, SYSTEMD_VERIFY_CONTEXT=context)
    return subprocess.run(
        [sys.executable, str(UNIT_VERIFIER)],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def test_unknown_context_is_a_usage_error():
    result = run_verifier("definitely-not-a-context")
    assert result.returncode == 2
    assert "SYSTEMD_VERIFY_CONTEXT must be" in result.stderr


def test_source_context_passes_on_this_uninstalled_tree():
    result = run_verifier("source")
    assert result.returncode == 0, result.stdout + result.stderr


def test_installed_context_requires_systemd_analyze_or_real_paths():
    """Installed mode must never pass on a machine where the bot is absent."""
    result = run_verifier("installed")
    interpreter = Path("/opt/bitcoin-bot/monitoring-current/bin/python")
    if os.access(interpreter, os.X_OK):
        pytest.skip("the bot is installed on this host; strict mode is expected to pass")
    assert result.returncode == 1, (
        "installed context must fail when project-owned paths are missing; "
        f"got exit {result.returncode}: {result.stdout}{result.stderr}"
    )


# --------------------------------------------------------------------------
# Classifier unit tests: the exception is narrow, not a broad text pattern
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def verifier():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import verify_systemd_units

        return verify_systemd_units
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize(
    "line",
    [
        "Command /opt/bitcoin-bot/monitoring-current/bin/python is not executable: "
        "No such file or directory",
        "Command /usr/local/libexec/bitcoin-bot-monitor-snapshot is not executable: "
        "No such file or directory",
        "Failed to create bitcoin-bot-monitor-snapshot.service/start: "
        "Unit docker.service not found.",
    ],
)
def test_expected_preinstall_lines_are_accepted_in_source_mode(verifier, line):
    assert verifier.classify_source_line(line) is None


@pytest.mark.parametrize(
    "line",
    [
        # A missing executable that this project does NOT install must fail even
        # in source mode -- this is the case the old blanket exception hid.
        "Command /usr/bin/totally-unrelated is not executable: No such file or directory",
        "Unit some-third-party.service not found.",
        "Unknown key name 'ExecStrat' in section 'Service', ignoring.",
        "Invalid section header '[Servce]'",
        "bitcoin-bot-monitor-live.service: Unit configuration has fatal error",
        "Failed to parse timer value, ignoring: nonsense",
        "Ignoring unknown escape sequences",
    ],
)
def test_real_defects_are_rejected_in_source_mode(verifier, line):
    assert verifier.classify_source_line(line) == line.strip()


def test_project_owned_prefixes_do_not_cover_arbitrary_paths(verifier):
    assert verifier.project_owned("/opt/bitcoin-bot/monitoring-current/bin/python")
    assert verifier.project_owned("/usr/local/libexec/bitcoin-bot-monitor-snapshot")
    assert not verifier.project_owned("/usr/bin/python3")
    assert not verifier.project_owned("/usr/local/libexec/other-tool")
    assert not verifier.project_owned("/opt/something-else/bin/python")


def test_hardening_regression_fails_in_both_contexts(verifier, tmp_path, monkeypatch):
    """Removing NoNewPrivileges from a unit must fail source and installed mode."""
    import shutil

    staged = tmp_path / "monitoring/systemd"
    staged.parent.mkdir(parents=True)
    shutil.copytree(UNIT_DIR, staged)
    victim = staged / "bitcoin-bot-monitor-testnet.service"
    victim.write_text(
        "\n".join(
            line
            for line in victim.read_text(encoding="utf-8").splitlines()
            if not line.startswith("NoNewPrivileges=")
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(verifier, "UNIT_DIR", staged)
    with pytest.raises(SystemExit) as excinfo:
        verifier.check_hardening(verifier.unit_files())
    assert excinfo.value.code == 1


def test_malformed_unit_directory_fails(verifier, tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, "UNIT_DIR", tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit) as excinfo:
        verifier.unit_files()
    assert excinfo.value.code == 1


def test_installed_mode_rejects_a_missing_project_executable(
    verifier, tmp_path, monkeypatch
):
    """The exact case the blanket exception used to hide."""
    staged = tmp_path / "systemd"
    staged.mkdir()
    (staged / "probe.service").write_text(
        "[Unit]\nDescription=probe\n\n[Service]\n"
        "ExecStart=/opt/bitcoin-bot/monitoring-current/bin/python -m probe\n"
        "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verifier, "UNIT_DIR", staged)
    units = verifier.unit_files()
    if os.path.isfile("/opt/bitcoin-bot/monitoring-current/bin/python"):
        pytest.skip("the bot is installed on this host")
    with pytest.raises(SystemExit) as excinfo:
        verifier.check_installed_paths(units)
    assert excinfo.value.code == 1


# --------------------------------------------------------------------------
# Executable bits
# --------------------------------------------------------------------------


def test_exec_bit_helper_matches_the_zip_builder_rule():
    """set_git_exec_bits.py and build_release_zip.py must agree on the 0755 set."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import set_git_exec_bits
    finally:
        sys.path.pop(0)

    builder = (ROOT / "scripts/build_release_zip.py").read_text(encoding="utf-8")
    for path in set_git_exec_bits.EXPLICIT_EXECUTABLES:
        assert path in builder, (
            f"{path} is marked executable by set_git_exec_bits.py but not by "
            "build_release_zip.py"
        )


def test_every_shipped_shell_script_is_covered():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import set_git_exec_bits
    finally:
        sys.path.pop(0)

    tracked = [
        path.relative_to(ROOT).as_posix()
        for path in release_files()
    ]
    expected = set(set_git_exec_bits.expected_executables(tracked))
    shell = {path for path in tracked if path.endswith(".sh")}
    assert shell <= expected
    assert len(expected) == 18, (
        f"expected 18 release executables, found {len(expected)}: {sorted(expected)}"
    )
