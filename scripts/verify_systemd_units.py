#!/usr/bin/env python3
"""Verify the monitoring and host-guard systemd units in an explicit context.

`systemd-analyze verify` resolves every ExecStart binary against the machine
running the check. On a source-validation host the bot is not installed, so
those paths legitimately do not exist. On the Oracle deployment host they must
exist, and a missing one is a real deployment defect.

The previous release gate collapsed that distinction into a blanket
``|| echo ...``, which suppressed *every* non-zero exit -- including genuine
unit syntax errors, malformed directives, invalid dependency relationships and
security-setting regressions. This module replaces that with a line-by-line
classifier:

source mode
    Only the enumerated paths that ``deploy/install_monitoring.sh`` and
    ``deploy/oracle_setup.sh`` create may be reported missing, plus the
    documented external ``docker.service`` dependency. Every other diagnostic
    line fails the gate.

installed mode
    Nothing is excused. ``systemd-analyze verify`` must exit 0, and every
    project-owned ``ExecStart``/``ExecStartPre``/``ExecStop`` command and
    ``EnvironmentFile`` must exist -- executable where it is a command.

Context comes from ``SYSTEMD_VERIFY_CONTEXT`` (``auto``, ``source`` or
``installed``). ``auto`` resolves to ``installed`` when the monitoring
interpreter is present.

Exit codes: 0 pass, 1 verification failure, 2 usage/environment error.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIT_DIR = ROOT / "monitoring/systemd"
UNIT_DIR = DEFAULT_UNIT_DIR
HOST_UNIT_DIR = ROOT / "deploy/systemd"

# Absolute paths this project owns and that the Oracle installer creates. Only
# these may be reported absent in source mode.
INSTALL_CREATED_PREFIXES = (
    "/opt/bitcoin-bot/",
    "/usr/local/libexec/bitcoin-bot-",
    "/usr/local/libexec/bitcoin-bot/",
    "/etc/bitcoin-bot/",
    "/var/lib/bitcoin-bot/",
    "/var/log/bitcoin-bot/",
)

# External units that are documented as unavailable in a source-validation
# environment (no Docker engine on a build/CI container or a Windows host).
EXTERNAL_UNITS = ("docker.service", "docker.socket")

# systemd-analyze diagnostics that are acceptable in source mode *only* when
# the referenced path is one this project installs.
_MISSING_COMMAND = re.compile(
    r"Command (?P<path>/\S+) is not executable: No such file or directory"
)
_MISSING_EXEC_GENERIC = re.compile(
    r"Executable (?P<path>/\S+) not found|"
    r"Failed to (?:open|read) (?:environment )?file (?P<path2>/\S+): "
    r"No such file or directory"
)
_MISSING_EXTERNAL_UNIT = re.compile(
    r"Failed to create \S+/start: Unit (?P<unit>\S+) not found\.|"
    r"Unit (?P<unit2>\S+) not found\."
)

_EXEC_DIRECTIVE = re.compile(
    r"^(?P<key>ExecStart|ExecStartPre|ExecStartPost|ExecStop|ExecStopPost|"
    r"ExecReload)=(?P<value>.*)$"
)
_ENVFILE_DIRECTIVE = re.compile(r"^EnvironmentFile=(?P<value>.*)$")

# Security directives whose loss would be a hardening regression. Checked
# structurally in both contexts so a silent removal cannot pass either mode.
REQUIRED_SERVICE_HARDENING = (
    "NoNewPrivileges",
    "PrivateTmp",
    "ProtectSystem",
)


def fail(message: str) -> None:
    print(f"systemd verification failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_context() -> str:
    context = os.environ.get("SYSTEMD_VERIFY_CONTEXT", "auto").strip() or "auto"
    if context == "auto":
        interpreter = Path("/opt/bitcoin-bot/monitoring-current/bin/python")
        context = "installed" if os.access(interpreter, os.X_OK) else "source"
    if context not in {"source", "installed"}:
        print(
            f"SYSTEMD_VERIFY_CONTEXT must be auto, source or installed; got {context!r}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return context


def display_path(path: Path) -> str:
    """Repository-relative when possible, absolute otherwise (tests stage units
    outside the tree)."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def unit_files() -> list[Path]:
    directories = [UNIT_DIR]
    # Tests intentionally replace UNIT_DIR with a staged fixture. Include the
    # host units only for the real repository validation context.
    if UNIT_DIR == DEFAULT_UNIT_DIR:
        directories.append(HOST_UNIT_DIR)
    units = []
    for directory in directories:
        units.extend(sorted(directory.glob("*.service")))
        units.extend(sorted(directory.glob("*.timer")))
    if not units:
        fail(f"no systemd units found under {display_path(UNIT_DIR)}")
    return units


def project_owned(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in INSTALL_CREATED_PREFIXES)


def strip_exec_prefixes(value: str) -> str:
    """Drop systemd ExecStart prefix characters such as -, @, +, !, : ."""
    return value.lstrip("-@+!:").strip()


def command_path(value: str) -> str | None:
    value = strip_exec_prefixes(value)
    if not value:
        return None
    first = value.split()[0]
    return first if first.startswith("/") else None


def check_hardening(units: list[Path]) -> None:
    """A security-setting regression fails in both contexts."""
    for unit in units:
        if unit.suffix != ".service":
            continue
        text = unit.read_text(encoding="utf-8")
        if "[Service]" not in text:
            fail(f"{unit.name} has no [Service] section")
        missing = [
            directive
            for directive in REQUIRED_SERVICE_HARDENING
            if not re.search(rf"^{directive}=", text, re.MULTILINE)
        ]
        if missing:
            fail(
                f"{unit.name} lost required hardening directives: "
                f"{', '.join(missing)}"
            )


def check_installed_paths(units: list[Path]) -> None:
    """Installed mode: every project-owned referenced path must really exist."""
    problems: list[str] = []
    for unit in units:
        for raw in unit.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            match = _EXEC_DIRECTIVE.match(line)
            if match:
                target = command_path(match.group("value"))
                if target and project_owned(target):
                    if not os.path.isfile(target):
                        problems.append(
                            f"{unit.name}: {match.group('key')} target does not "
                            f"exist: {target}"
                        )
                    elif not os.access(target, os.X_OK):
                        problems.append(
                            f"{unit.name}: {match.group('key')} target is not "
                            f"executable: {target}"
                        )
                continue
            env = _ENVFILE_DIRECTIVE.match(line)
            if env:
                value = env.group("value").strip()
                optional = value.startswith("-")
                target = value.lstrip("-").strip()
                if target.startswith("/") and project_owned(target):
                    if not optional and not os.path.isfile(target):
                        problems.append(
                            f"{unit.name}: EnvironmentFile does not exist: {target}"
                        )
    if problems:
        fail(
            "installed-mode path checks failed:\n  "
            + "\n  ".join(problems)
        )


def classify_source_line(line: str) -> str | None:
    """Return None when the diagnostic is an accepted pre-installation absence.

    Any other non-empty diagnostic is returned unchanged so the caller can fail.
    """
    stripped = line.strip()
    if not stripped:
        return None

    match = _MISSING_COMMAND.search(stripped)
    if match:
        return None if project_owned(match.group("path")) else stripped

    match = _MISSING_EXEC_GENERIC.search(stripped)
    if match:
        path = match.group("path") or match.group("path2") or ""
        return None if project_owned(path) else stripped

    match = _MISSING_EXTERNAL_UNIT.search(stripped)
    if match:
        unit = match.group("unit") or match.group("unit2") or ""
        return None if unit in EXTERNAL_UNITS else stripped

    return stripped


def run_analyze(units: list[Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemd-analyze", "verify", *[str(unit) for unit in units]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    context = resolve_context()
    units = unit_files()
    check_hardening(units)

    if shutil.which("systemd-analyze") is None:
        if context == "installed":
            fail(
                "installed context requires systemd-analyze, which is not on PATH"
            )
        print(
            f"systemd units: {len(units)} checked; hardening directives present; "
            "systemd-analyze unavailable (source context)"
        )
        return 0

    result = run_analyze(units)
    output = f"{result.stdout}\n{result.stderr}"

    if context == "installed":
        check_installed_paths(units)
        if result.returncode != 0:
            fail(
                "installed context requires systemd-analyze to exit 0; "
                f"exit={result.returncode}\n{output.strip()}"
            )
        print(
            f"systemd units: {len(units)} verified strictly (installed context); "
            "systemd-analyze exit 0; all project-owned paths present"
        )
        return 0

    unexpected = [
        classified
        for classified in (classify_source_line(line) for line in output.splitlines())
        if classified is not None
    ]
    if unexpected:
        fail(
            "source context accepts only pre-installation absence of "
            "project-owned paths and the documented docker.service dependency; "
            "unexplained systemd-analyze output:\n  "
            + "\n  ".join(unexpected)
        )
    if result.returncode not in (0, 1):
        fail(
            f"systemd-analyze returned unexpected exit status {result.returncode}"
        )
    print(
        f"systemd units: {len(units)} verified (source context); only "
        "pre-installation project paths and docker.service were unresolved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
