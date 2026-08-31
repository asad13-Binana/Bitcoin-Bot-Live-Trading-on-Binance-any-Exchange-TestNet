"""Offline checks complement, but never substitute for, the native Docker job."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import time

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


@pytest.mark.parametrize("number", [1, 13, 30])
def test_permission_probe_accepts_only_real_os_denial_codes(number):
    fixture = load_script("services_runtime_fixture")
    def rejected():
        raise OSError(number, "synthetic denial")
    fixture.denied(rejected)


def test_permission_probe_rejects_missing_file_as_false_security_pass():
    fixture = load_script("services_runtime_fixture")
    with pytest.raises(AssertionError):
        fixture.denied(lambda: None)
    def missing():
        raise FileNotFoundError(2, "synthetic missing path")
    with pytest.raises(AssertionError):
        fixture.denied(missing)


def test_container_public_fixture_runs_real_moneyflow_and_publishes_fresh_files(tmp_path, monkeypatch):
    from services.moneyflow import service
    from services.moneyflow.client import MoneyFlowClient

    fixture = load_script("services_runtime_fixture").PublicSpotFixture()
    pair = tmp_path / "pair"
    shutil.copytree(ROOT / "shared/pair", pair)
    latest = tmp_path / "moneyflow/latest.json"
    runtime = tmp_path / "runtime/moneyflow"
    monkeypatch.setattr(service, "ACTIVE_PAIR_FILE", pair / "active_pair.json")
    monkeypatch.setattr(service, "MONEYFLOW_FILE", latest)
    monkeypatch.setattr(service, "RUNTIME", runtime)
    for key, value in {
        "PAIRLIST_FILE": pair / "current_pairlist.json",
        "FREQTRADE_ACTIVE_CONFIG": pair / "freqtrade-active.json",
        "EXTERNAL_MARKET_CACHE_FILE": latest.parent / "external_market_cache.json",
        "BTC_QUOTE_ALLOWLIST": "USDT",
        "COINGECKO_CONTEXT_ENABLED": "false",
        "COINMARKETCAP_CONTEXT_ENABLED": "false",
        "REQUIRE_EXTERNAL_CONFLUENCE": "false",
    }.items():
        monkeypatch.setenv(key, str(value))
    # Reject every real network attempt; only the public in-memory fixture runs.
    import socket
    def no_network(*args, **kwargs):
        raise AssertionError("offline collection attempted network I/O")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    started = time.time()
    snapshot = service.run_once(client=MoneyFlowClient(spot=fixture))
    health = json.loads((runtime / "moneyflow_health.json").read_text())
    assert json.loads(latest.read_text()) == snapshot
    assert snapshot["ok"] is True and health["ok"] is True
    assert snapshot["pair"] == health["pair"] == "BTC/USDT"
    assert started <= health["ts"] <= time.time()
    assert started <= snapshot["generated_at_epoch"] <= time.time()
    assert set(snapshot["timeframes"]) == set(service.TIMEFRAMES)
    assert all(row["direction"] != "unavailable" for row in snapshot["timeframes"].values())
    assert len(fixture.calls) == 10
    assert snapshot["futures"]["disabled"] is True
    assert snapshot["external_context"]["advisory_only"] is True


@pytest.mark.parametrize("field", ["ocoAllowed", "otoAllowed", "filters"])
def test_public_fixture_cannot_bypass_missing_market_capabilities(field):
    from services.common.market_policy import PairPolicyError, validate_exchange_symbol

    metadata = load_script("services_runtime_fixture").PublicSpotFixture().exchange_info("BTCUSDT")["symbols"][0]
    metadata.pop(field)
    with pytest.raises(PairPolicyError):
        validate_exchange_symbol("BTC/USDT", metadata, ["USDT"])
