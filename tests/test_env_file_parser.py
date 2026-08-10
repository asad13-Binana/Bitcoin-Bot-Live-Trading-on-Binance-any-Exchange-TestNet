"""Regressions for the privileged environment-file parser.

The privileged installers used to read configuration with:

    set -a
    source "$ENV_FILE"
    set +a

`source` makes Bash interpret the file, so every value was shell code executed
with the installer's privileges — root, via `as_root`. The rollback path did the
same to snapshots under /var/lib/bitcoin-bot/config-snapshots, which
oracle_setup.sh chowns to the unprivileged deployment user, turning a
deployment-user file write into root code execution.

These tests pin the replacement parser's behaviour and assert that no `source`
of a configuration file returns to the privileged scripts.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy/lib/envfile.sh"
INSTALL_ARTIFACT = ROOT / "deploy/install_artifact.sh"
INSTALL_MONITORING = ROOT / "deploy/install_monitoring.sh"
ORACLE_SETUP = ROOT / "deploy/oracle_setup.sh"
ROOT_WRAPPER = ROOT / "deploy/bitcoin-bot-deploy"

BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="bash is unavailable on this host")

INLINE_BEGIN = "# >>> BEGIN INLINED deploy/lib/envfile.sh (do not edit; see that file) >>>"
INLINE_END = "# <<< END INLINED deploy/lib/envfile.sh <<<"


def run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=cwd, capture_output=True, text=True, check=False,
    )


# --------------------------------------------------------------------------
# The parser must never execute a value
# --------------------------------------------------------------------------

INJECTIONS = {
    "command_substitution": "$(touch pwned)",
    "backticks": "`touch pwned`",
    "semicolon": "simulation; touch pwned",
    "pipe": "x | touch pwned",
    "redirect": "y > pwned",
    "and_chain": "z && touch pwned",
    "or_chain": "z || touch pwned",
    "nested_substitution": "$( $(touch pwned) )",
    "arithmetic": "$((1+1)) touch pwned",
    "tilde": "~/pwned",
    "glob": "* pwned",
    "newline_escape": "a\\ntouch pwned",
}


@requires_bash
@pytest.mark.parametrize("name,payload", sorted(INJECTIONS.items()))
def test_no_injection_payload_is_ever_executed(tmp_path, name, payload):
    env = tmp_path / f"{name}.env"
    env.write_text(f"SIGNAL_HMAC_KEY={payload}\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_load "{env.as_posix()}"; '
        'printf "%s" "$SIGNAL_HMAC_KEY"',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "pwned").exists(), f"{name} executed as shell code"
    assert result.stdout == payload, "value must be preserved literally"


@requires_bash
def test_source_would_have_executed_the_same_payload(tmp_path):
    """Documents the defect this parser replaces, so the risk stays visible."""
    env = tmp_path / "evil.env"
    env.write_text("K=$(touch pwned)\n", encoding="utf-8", newline="\n")
    run_bash(f'set -a; source "{env.as_posix()}"; set +a', tmp_path)
    assert (tmp_path / "pwned").exists(), (
        "expected `source` to execute the payload; if this fails the test itself "
        "is no longer demonstrating the original defect"
    )


@requires_bash
def test_env_file_get_does_not_execute(tmp_path):
    env = tmp_path / "g.env"
    env.write_text(
        "SIGNAL_HMAC_KEY=$(touch pwned)\nCOMMAND_HMAC_KEY=plain\n",
        encoding="utf-8", newline="\n",
    )
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_get "{env.as_posix()}" SIGNAL_HMAC_KEY',
        tmp_path,
    )
    assert result.returncode == 0
    assert not (tmp_path / "pwned").exists()
    assert result.stdout == "$(touch pwned)"


@requires_bash
def test_env_file_pairs_does_not_execute(tmp_path):
    env = tmp_path / "p.env"
    env.write_text("SIGNAL_HMAC_KEY=$(touch pwned)\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_pairs "{env.as_posix()}" | tr "\\0" "\\n"',
        tmp_path,
    )
    assert result.returncode == 0
    assert not (tmp_path / "pwned").exists()
    assert result.stdout.strip() == "SIGNAL_HMAC_KEY=$(touch pwned)"


# --------------------------------------------------------------------------
# Correct parsing behaviour
# --------------------------------------------------------------------------


@requires_bash
@pytest.mark.parametrize(
    "line,key,expected",
    [
        ('SIGNAL_HMAC_KEY=plain', 'SIGNAL_HMAC_KEY', 'plain'),
        ('SIGNAL_HMAC_KEY="double quoted"', 'SIGNAL_HMAC_KEY', 'double quoted'),
        ("SIGNAL_HMAC_KEY='single quoted'", 'SIGNAL_HMAC_KEY', 'single quoted'),
        ('SIGNAL_HMAC_KEY=', 'SIGNAL_HMAC_KEY', ''),
        ('SIGNAL_HMAC_KEY=a=b=c', 'SIGNAL_HMAC_KEY', 'a=b=c'),
        ('SIGNAL_HMAC_KEY=  leading spaces kept', 'SIGNAL_HMAC_KEY', '  leading spaces kept'),
        ('  SIGNAL_HMAC_KEY=indented key', 'SIGNAL_HMAC_KEY', 'indented key'),
    ],
)
def test_value_parsing(tmp_path, line, key, expected):
    env = tmp_path / "v.env"
    env.write_text(line + "\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_get "{env.as_posix()}" {key}', tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@requires_bash
def test_duplicate_key_fails_closed(tmp_path):
    env = tmp_path / "d.env"
    env.write_text(
        "SIGNAL_HMAC_KEY=first\nSIGNAL_HMAC_KEY=second\n",
        encoding="utf-8", newline="\n",
    )
    result = run_bash(
        f'source "{LIB.as_posix()}"; '
        f'env_file_get "{env.as_posix()}" SIGNAL_HMAC_KEY', tmp_path
    )
    assert result.returncode != 0


@requires_bash
@pytest.mark.parametrize("value", ['"unbalanced', "'unbalanced", 'unbalanced"', "unbalanced'"])
def test_unmatched_quotes_fail_closed(tmp_path, value):
    env = tmp_path / "quotes.env"
    env.write_text(f"SIGNAL_HMAC_KEY={value}\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_load "{env.as_posix()}"', tmp_path
    )
    assert result.returncode != 0


@requires_bash
def test_comments_and_blank_lines_ignored(tmp_path):
    env = tmp_path / "c.env"
    env.write_text(
        "# comment\n\n   \nSIGNAL_HMAC_KEY=v\n", encoding="utf-8", newline="\n"
    )
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_load "{env.as_posix()}"; '
        'printf "%s" "$SIGNAL_HMAC_KEY"',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "v"


@requires_bash
def test_crlf_file_is_parsed_without_trailing_carriage_return(tmp_path):
    env = tmp_path / "crlf.env"
    env.write_bytes(b"SIGNAL_HMAC_KEY=value\r\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; '
        f'env_file_get "{env.as_posix()}" SIGNAL_HMAC_KEY', tmp_path
    )
    assert result.stdout == "value"


@requires_bash
def test_missing_key_returns_failure(tmp_path):
    env = tmp_path / "m.env"
    env.write_text("SIGNAL_HMAC_KEY=1\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; '
        f'env_file_get "{env.as_posix()}" COMMAND_HMAC_KEY', tmp_path
    )
    assert result.returncode == 1


@requires_bash
@pytest.mark.parametrize("bad", ["1KEY=v", "KEY-WITH-DASH=v", "KEY WITH SPACE=v", "=novalue"])
def test_malformed_keys_fail_closed_on_load(tmp_path, bad):
    """A tampered or corrupt file must abort, not load a partial configuration."""
    env = tmp_path / "bad.env"
    env.write_text(f"SIGNAL_HMAC_KEY=1\n{bad}\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_load "{env.as_posix()}"', tmp_path
    )
    assert result.returncode != 0, f"{bad!r} should have failed the load"


@requires_bash
def test_line_without_equals_fails_closed(tmp_path):
    env = tmp_path / "noeq.env"
    env.write_text(
        "SIGNAL_HMAC_KEY=1\nthis is not an assignment\n",
        encoding="utf-8", newline="\n",
    )
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_load "{env.as_posix()}"', tmp_path
    )
    assert result.returncode != 0


@requires_bash
def test_requested_key_name_is_validated(tmp_path):
    """A key name is used as a variable name, so it must be strictly validated.

    The payload is single-quoted so the enclosing shell passes it through
    literally; double quotes would let the harness itself expand it and the test
    would prove nothing.
    """
    env = tmp_path / "k.env"
    env.write_text("SIGNAL_HMAC_KEY=1\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f"""source '{LIB.as_posix()}'; env_file_get '{env.as_posix()}' 'A[0]$(touch pwned)'""",
        tmp_path,
    )
    assert result.returncode != 0, "a malformed key name must be rejected"
    assert not (tmp_path / "pwned").exists()


# --------------------------------------------------------------------------
# Trust checks on files a privileged process is about to read
# --------------------------------------------------------------------------


@requires_bash
def test_require_trusted_rejects_a_missing_file(tmp_path):
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_require_trusted "{(tmp_path / "nope").as_posix()}"',
        tmp_path,
    )
    assert result.returncode != 0


@requires_bash
def test_require_trusted_rejects_a_world_writable_file(tmp_path):
    env = tmp_path / "loose.env"
    env.write_text("A=1\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'chmod 0666 "{env.as_posix()}"; source "{LIB.as_posix()}"; '
        f'env_file_require_trusted "{env.as_posix()}"',
        tmp_path,
    )
    assert result.returncode != 0, "a group/world-writable env file must be rejected"


@requires_bash
@pytest.mark.parametrize(
    "key",
    ["PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV", "IFS", "PYTHONHOME", "PYTHONPATH"],
)
def test_process_control_variables_are_rejected(tmp_path, key):
    env = tmp_path / "process-control.env"
    env.write_text(f"{key}=/tmp/attacker\n", encoding="utf-8", newline="\n")
    result = run_bash(
        f'source "{LIB.as_posix()}"; env_file_pairs "{env.as_posix()}" >/dev/null',
        tmp_path,
    )
    assert result.returncode != 0, f"{key} must never enter a privileged child environment"


@requires_bash
def test_every_shipped_environment_template_matches_the_allowlist(tmp_path):
    templates = [ROOT / ".env.example", *sorted((ROOT / "monitoring").glob(".env.monitor.*.example"))]
    for template in templates:
        result = run_bash(
            f'source "{LIB.as_posix()}"; env_file_pairs "{template.as_posix()}" >/dev/null',
            tmp_path,
        )
        assert result.returncode == 0, f"{template}: {result.stderr}"


# --------------------------------------------------------------------------
# The privileged scripts must not regress to `source`
# --------------------------------------------------------------------------


@pytest.mark.parametrize("script", [INSTALL_ARTIFACT, INSTALL_MONITORING])
def test_privileged_scripts_never_source_configuration(script):
    offenders = []
    for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("source ") or stripped.startswith(". "):
            offenders.append(f"{script.name}:{number}: {stripped}")
        if "set -a" in stripped and "source" in stripped:
            offenders.append(f"{script.name}:{number}: {stripped}")
    assert not offenders, (
        "privileged installers must parse configuration as literal data, never "
        f"evaluate it: {offenders}"
    )


@pytest.mark.parametrize("script", [INSTALL_ARTIFACT, INSTALL_MONITORING])
def test_inlined_parser_matches_the_canonical_library(script):
    """The inlined copies must not drift from deploy/lib/envfile.sh."""
    text = script.read_text(encoding="utf-8")
    assert INLINE_BEGIN in text and INLINE_END in text, f"{script.name} lost its inlined parser"
    inlined = text.split(INLINE_BEGIN, 1)[1].split(INLINE_END, 1)[0].strip()
    canonical = LIB.read_text(encoding="utf-8").split("\n", 1)[1].strip()
    assert inlined == canonical, (
        f"{script.name} inlined parser has drifted from deploy/lib/envfile.sh; "
        "re-run the inliner rather than editing the copy"
    )


@pytest.mark.parametrize("script", [INSTALL_ARTIFACT, INSTALL_MONITORING])
def test_privileged_scripts_check_env_ownership(script):
    text = script.read_text(encoding="utf-8")
    assert "env_file_require_trusted" in text, (
        f"{script.name} must verify ownership and permissions before reading "
        "an environment file with elevated privileges"
    )


def test_oracle_env_ownership_contract_is_root_only_end_to_end():
    setup = ORACLE_SETUP.read_text(encoding="utf-8")
    wrapper = ROOT_WRAPPER.read_text(encoding="utf-8")
    installer = INSTALL_ARTIFACT.read_text(encoding="utf-8")
    deployment = (ROOT / "docs/GITHUB_ORACLE_DEPLOYMENT.md").read_text(encoding="utf-8")
    security = (ROOT / "docs/SECURITY_AND_SECRETS_GUIDE.md").read_text(encoding="utf-8")

    assert 'install -m 0600 -o root -g root /dev/null "$PRIVATE/.env"' in setup
    assert 'chown root:root "$PRIVATE/.env"' in setup
    assert 'require_canonical_file "$ENV_FILE" 0 0 600' in wrapper
    assert "private env must be owned by root:root" in wrapper
    assert "persistent runtime ownership does not match BOT_UID/BOT_GID" in wrapper
    assert '$(stat -c \'%u\' "$ENV_FILE") == 0' in installer
    assert '$(stat -c \'%g\' "$ENV_FILE") == 0' in installer
    assert "deployment-user-owned file" not in installer
    assert "owned by `root:root`" in deployment
    assert "sudoedit /etc/bitcoin-bot/.env" in deployment
    assert "(`root:root`, mode 0600)" in security


def test_library_is_manifested():
    import json

    manifest = json.loads((ROOT / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    assert "deploy/lib/envfile.sh" in manifest["files"]


@requires_bash
def test_all_shipped_shell_scripts_still_parse():
    for script in sorted(ROOT.rglob("*.sh")):
        if any(part in {".git", "__pycache__"} for part in script.parts):
            continue
        result = subprocess.run(
            [BASH, "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"
