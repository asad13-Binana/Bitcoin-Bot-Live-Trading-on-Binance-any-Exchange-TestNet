"""Offline checks complement, but never substitute for, the native Docker job."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("uid,gid", [(994, 985), (12345, 23456), (10001, 10001)])
@pytest.mark.parametrize("readonly", [True, False])
def test_actual_container_command_preserves_identity_and_isolation(uid, gid, readonly):
    probe = load_script("verify_services_runtime")
    command = probe.container("fixture:only", uid, gid, "python", "-c", "import services", readonly=readonly)
    assert command[command.index("--user") + 1] == f"{uid}:{gid}"
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert ("--read-only" in command) == readonly
    assert "--privileged" not in command


def test_image_normalises_only_immutable_app_after_last_copy():
    recipe = (ROOT / "Dockerfile.services").read_text()
    assert "--chown=bot:bot" not in recipe
    assert recipe.rfind("COPY ") < recipe.index("RUN chown -R 0:0 /app")
    assert "find /app -type d -exec chmod 0555" in recipe
    assert "find /app -type f -exec chmod 0444" in recipe
    assert recipe.rstrip().endswith("USER bot")
    assert "/var/lib/" not in recipe and "chmod 777" not in recipe


def test_native_and_fresh_artifact_jobs_require_services_runtime_proof():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    runtime = workflow["jobs"]["runtime-container"]
    assert set(runtime["strategy"]["matrix"]["os"]) == {"ubuntu-24.04", "ubuntu-24.04-arm"}
    assert any("python scripts/verify_services_runtime.py" in s.get("run", "") for s in runtime["steps"])
    artifact = workflow["jobs"]["artifact"]
    assert "runtime-container" in artifact["needs"]
    assert any("verify_services_runtime.py --image" in s.get("run", "") for s in artifact["steps"])


def test_public_fixture_has_no_order_surface_and_rejects_unexpected_endpoint():
    fixture = load_script("services_runtime_fixture").PublicSpotFixture()
    assert fixture.exchange_info("BTCUSDT")["symbols"][0]["baseAsset"] == "BTC"
    with pytest.raises(AssertionError):
        fixture.get("/api/v3/order", {"symbol": "BTCUSDT"})
    assert not hasattr(fixture, "post") and not hasattr(fixture, "create_order")
