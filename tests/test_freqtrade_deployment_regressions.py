"""AWS incident regressions; no exchange, Docker daemon or host state access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PAIR_FILE = PurePosixPath("/freqtrade/shared/pair/current_pairlist.json")


def test_shipped_pairlist_url_resolves_to_the_mounted_absolute_path():
    config = json.loads((ROOT / "freqtrade/user_data/config.json").read_text())
    pairlists = config["pairlists"]
    assert len(pairlists) == 1
    entry = pairlists[0]
    assert entry["method"] == "RemotePairList"
    # This is the 2026.6 plugin's literal prefix stripping, not RFC URL parsing.
    # The actual pinned plugin is additionally exercised by verify.sh in CI.
    filename = entry["pairlist_url"].split("file:///", 1)[1]
    assert PurePosixPath(filename).is_absolute(), filename
    assert PurePosixPath(filename) == PAIR_FILE
    assert entry["number_assets"] == 1
    assert entry["keep_pairlist_on_failure"] is False
    assert config["dry_run"] is True
    assert config["force_entry_enable"] is False
    assert config["trading_mode"] == "spot"
    assert not config["exchange"]["key"] and not config["exchange"]["secret"]
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "${SHARED_HOST_PATH:-./shared}/pair:/freqtrade/shared/pair:ro" in (
        compose["services"]["freqtrade"]["volumes"]
    )


def test_three_slash_negative_control_resolves_to_the_wrong_directory():
    bad_url = "file:///" + str(PAIR_FILE).lstrip("/")
    filename = PurePosixPath(bad_url.split("file:///", 1)[1])
    assert not filename.is_absolute()
    assert PurePosixPath("/freqtrade") / filename == PurePosixPath(
        "/freqtrade/freqtrade/shared/pair/current_pairlist.json"
    )


def test_container_gate_calls_real_remote_pairlist_without_network():
    script = (ROOT / "freqtrade/scripts/verify.sh").read_text()
    assert "remote_pairlist_probe.py" in script
    assert "--network none" in script
    assert "--read-only" in script
    assert '"${IMAGES[0]}"' in script
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "bash freqtrade/scripts/verify.sh" in workflow
    runtime = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    offline = yaml.safe_load((ROOT / "freqtrade/docker-compose.yml").read_text())
    assert runtime["services"]["freqtrade"]["image"] == (
        offline["services"]["freqtrade"]["image"]
    )
    assert "@sha256:" in offline["services"]["freqtrade"]["image"]


@pytest.mark.parametrize("status", ["healthy", "unhealthy", "missing", "compose-error"])
def test_healthcheck_supplies_exact_bindings_and_fails_closed(tmp_path, status):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    release = tmp_path / "release with spaces"
    (release / "scripts").mkdir(parents=True)
    (release / "deploy").mkdir()
    for name in ("scripts/healthcheck.sh", "deploy/instance_identity.sh"):
        shutil.copyfile(ROOT / name, release / name)
    release_hash = "a" * 64
    (release / "RELEASE_SHA256.txt").write_text(
        release_hash + "  RELEASE_MANIFEST.json\n", newline="\n"
    )
    config = tmp_path / "private snapshot.env"
    # Never source this file. The fake Compose needs no credentials.
    config.write_text("EXECUTION_MODE=simulation\n", newline="\n")
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    services = "execution-sidecar freqtrade moneyflow telegram-broker"
    if status == "missing":
        services = "execution-sidecar moneyflow telegram-broker"
    docker.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'if [[ "$1" == compose ]]; then\n'
        f'  [[ "${{DEPLOYED_RELEASE_HASH:-}}" == "{release_hash}" ]] || exit 19\n'
        f'  [[ "${{DEPLOYED_CONFIG_SHA256:-}}" == "{config_hash}" ]] || exit 20\n'
        f'  [[ "${{SIDECAR_RELEASE_HASH:-}}" == "{release_hash}" ]] || exit 21\n'
        f'  [[ "${{ENVELOPE_RELEASE_HASH:-}}" == "{release_hash}" ]] || exit 22\n'
        + ("  exit 23\n" if status == "compose-error" else "")
        + '  case "$*" in\n'
        + '    *"config -q") exit 0 ;;\n'
        + f'    *"--status running --services") printf "%s\\n" {services} ;;\n'
        + '    *"ps -q "*) printf "cid-%s\\n" "${!#}" ;;\n'
        + '    *) exit 24 ;;\n  esac\n'
        + 'elif [[ "$1" == inspect ]]; then\n'
        + ("  echo unhealthy\n" if status == "unhealthy" else "  echo healthy\n")
        + "else exit 25; fi\n",
        encoding="utf-8", newline="\n",
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [
            bash, "--noprofile", "--norc", "-c",
            'export PATH="$(cd "$1" && pwd -P):$PATH"; '
            'export BITCOIN_BOT_ENV_FILE="$2"; '
            # Poison ambient values: the script must derive its own bindings.
            'export DEPLOYED_RELEASE_HASH=wrong DEPLOYED_CONFIG_SHA256=wrong; '
            'bash "$3"',
            "healthcheck-test", fake_bin.as_posix(), config.as_posix(),
            (release / "scripts/healthcheck.sh").as_posix(),
        ],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30,
    )
    passed = "all four Bitcoin Bot services are present, running and healthy"
    if status == "healthy":
        assert result.returncode == 0, result.stdout + result.stderr
        assert passed in result.stdout
    else:
        assert result.returncode != 0
        assert passed not in result.stdout
        expected = {
            "unhealthy": "UNHEALTHY:",
            "missing": "NOT RUNNING: freqtrade",
            "compose-error": "health-check Compose configuration failed",
        }
        assert expected[status] in result.stderr
