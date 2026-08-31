from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("failure_evidence", ROOT / "deploy/capture_failure_evidence.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def record():
    return {"Image": "sha256:" + "a" * 64, "Config": {
        "Image": "bitcoin-testnet-services:bitcoin-" + "b" * 16,
        "Env": ["BINANCE_API_SECRET=private-do-not-retain"],
        "Labels": {"com.docker.compose.project": "bitcoin-testnet",
                   "com.docker.compose.service": "moneyflow", "secret": "private-do-not-retain"}},
        "State": {"Status": "exited", "ExitCode": 1, "Error": "PermissionError: private-do-not-retain",
                  "Health": {"Status": "unhealthy", "Log": [{"ExitCode": 1,
                             "Output": "PermissionError: private-do-not-retain"}] * 10}}}


def test_projected_diagnostics_never_retain_raw_secrets_or_unbounded_health_history():
    result = capture.project_state(record(), "bitcoin-testnet", "bitcoin-" + "b" * 16)
    assert "private-do-not-retain" not in json.dumps(result)
    assert len(result["health_history"]) == 5
    assert result["state_error_categories"] == ["PermissionError"]
    assert result["exit_code"] == 1 and result["health"] == "unhealthy"


@pytest.mark.parametrize("field,value", [("project", "other-bot"), ("service", "attacker")])
def test_diagnostics_reject_foreign_project_or_service(field, value):
    source = record()
    source["Config"]["Labels"]["com.docker.compose." + field] = value
    with pytest.raises(ValueError):
        capture.project_state(source, "bitcoin-testnet", "bitcoin-" + "b" * 16)


def test_diagnostics_reject_previous_release_image():
    with pytest.raises(ValueError):
        capture.project_state(record(), "bitcoin-testnet", "bitcoin-" + "c" * 16)


def test_rollback_captures_before_down_but_does_not_depend_on_capture_success():
    code = (ROOT / "deploy/install_artifact.sh").read_text().split("rollback(){", 1)[1]
    assert code.index("capture_failure_evidence.py") < code.index("down --remove-orphans")
    assert "timeout 30s" in code
    assert '"$COMPOSE_PROJECT_NAME" "$RELEASE_HASH" ||' in code
    assert "continuing safety rollback" in code


@pytest.mark.skipif(os.name != "posix", reason="native pipe readiness requires Linux")
@pytest.mark.parametrize("program", [
    "import sys; sys.stderr.write('PermissionError: fixture')",
    "import sys; sys.stdout.write('x' * 1000000)",
    "import time; time.sleep(30)",
])
def test_real_diagnostic_subprocess_is_byte_and_time_bounded(program):
    start = time.monotonic()
    result = capture.bounded_command([sys.executable, "-c", program], limit=1024)
    assert len(result.encode()) <= 1024
    assert time.monotonic() - start < 8
    if "PermissionError" in program:
        assert capture.categories(result) == ["PermissionError"]
