"""The small AWS experiment never weakens the four-bot Oracle contract."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_capacity", ROOT / "deploy/host_capacity.py")
capacity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity)


def check(**overrides):
    values = dict(profile="single-bot-experiment", mode="simulation", instance="bitcoin-testnet",
                  phase="install", memory_mib=7776, swap_mib=4095, cpu_count=2,
                  free_gib=16, architecture="x86_64", projects=[])
    values.update(overrides)
    capacity.validate(**values)


def test_reported_aws_capacity_can_run_one_experiment_not_four_bots():
    check()
    check(mode="testnet", projects=["bitcoin-testnet"] * 4)
    check(instance="bitcoin-live")
    with pytest.raises(ValueError):
        check(profile="oracle-four-bot", architecture="aarch64")


@pytest.mark.parametrize("overrides", [
    {"memory_mib": 1024}, {"memory_mib": 7167}, {"swap_mib": 3799},
    {"cpu_count": 1}, {"free_gib": 7}, {"phase": "bootstrap", "free_gib": 11},
    {"architecture": "riscv64"}, {"profile": "auto"}, {"instance": "binana-testnet"},
    {"instance": "bitcoin-live", "mode": "live"}, {"mode": "live"},
    {"instance": "bitcoin-live", "mode": "testnet"}, {"projects": ["binana-testnet"]},
    {"projects": ["bitcoin-live"]}, {"projects": [""]}, {"phase": "unknown"},
    {"memory_mib": True}, {"swap_mib": -1},
])
def test_experiment_fails_closed(overrides):
    with pytest.raises(ValueError):
        check(**overrides)


def test_oracle_minima_are_independent_and_preserved():
    oracle = dict(profile="oracle-four-bot", architecture="aarch64", memory_mib=11264,
                  swap_mib=4096, free_gib=80, phase="bootstrap")
    check(**oracle)
    check(**{**oracle, "phase": "install", "free_gib": 8})
    for change in ({"memory_mib": 11263}, {"free_gib": 79}, {"architecture": "x86_64"}):
        with pytest.raises(ValueError):
            check(**{**oracle, **change})


def test_bootstrap_allows_swap_setup_but_install_requires_it():
    check(phase="bootstrap", swap_mib=0)
    with pytest.raises(ValueError):
        check(swap_mib=0)


def test_installer_uses_private_profile_not_shell_resource_overrides():
    installer = (ROOT / "deploy/install_artifact.sh").read_text()
    assert 'env_file_get "$ENV_FILE" DEPLOYMENT_PROFILE' in installer
    assert 'MIN_PHYSICAL_MEMORY_MIB=${' not in installer
    assert 'host_capacity.py" --phase install' in installer
    assert 'experiment cannot replace/roll back to a LIVE-money deployment' in installer
    assert 'DEPLOYMENT_PROFILE=oracle-four-bot' in (ROOT / ".env.example").read_text()
