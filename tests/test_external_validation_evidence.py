from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml
from hypothesis import given, settings, strategies as st

from scripts.verify_external_validation_evidence import REQUIRED_CASES, validate
from scripts.verify_coverage_ratchet import measured_percent


REPOSITORY = "asad13-Binana/Bitcoin-Bot-Live-Trading-on-Binance-any-Exchange-TestNet"
COMMIT = "1" * 40
MANIFEST = "2" * 64
ROOT = Path(__file__).resolve().parents[1]


def _valid() -> dict:
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "commit_sha": COMMIT,
        "release_manifest_sha256": MANIFEST,
        "package_mode": "testnet",
        "binance_base": "https://testnet.binance.vision",
        "performed_on_binance_spot_testnet": True,
        "live_orders_attempted": False,
        "credentials_redacted": True,
        "operator_approved": True,
        "started_at": "2026-08-14T00:00:00Z",
        "completed_at": "2026-08-14T01:00:00Z",
        "cases": {
            name: {"status": "PASS", "observations": 1, "evidence_sha256": "3" * 64}
            for name in REQUIRED_CASES
        },
    }


def _errors(value: dict) -> list[str]:
    return validate(
        value, expected_repository=REPOSITORY, expected_commit=COMMIT,
        expected_manifest_sha256=MANIFEST,
    )


def test_complete_release_bound_testnet_evidence_passes():
    assert _errors(_valid()) == []


def test_live_or_unredacted_evidence_can_never_pass():
    value = _valid()
    value["package_mode"] = "live"
    value["live_orders_attempted"] = True
    value["api_secret"] = "must-not-be-stored"
    errors = _errors(value)
    assert any("package_mode" in item for item in errors)
    assert any("live_orders_attempted" in item for item in errors)
    assert any("forbidden credential field" in item for item in errors)


@settings(max_examples=50, derandomize=True, deadline=None)
@given(st.text().filter(lambda value: value != "PASS"))
def test_any_non_pass_case_status_fails_closed(status: str):
    value = _valid()
    value["cases"]["partial_fill"]["status"] = status
    assert any("partial_fill.status" in item for item in _errors(value))


@settings(max_examples=40, derandomize=True, deadline=None)
@given(st.integers(max_value=0))
def test_non_positive_observation_counts_fail_closed(count: int):
    value = _valid()
    value["cases"]["restart_reconciliation"]["observations"] = count
    assert any("restart_reconciliation.observations" in item for item in _errors(value))


def test_coverage_reader_rejects_missing_total(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"totals": {}}), encoding="utf-8")
    try:
        measured_percent(path)
    except ValueError as exc:
        assert "percent_covered" in str(exc)
    else:
        raise AssertionError("missing coverage percentage was accepted")


def test_all_mandatory_cases_are_individually_required():
    for name in REQUIRED_CASES:
        value = copy.deepcopy(_valid())
        del value["cases"][name]
        assert any(name in item for item in _errors(value))


def test_ci_makes_security_and_coverage_mandatory_before_artifact():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert {"verify", "sast", "quality-evidence"} <= set(jobs["artifact"]["needs"])
    source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python scripts/scan_git_history_secrets.py" in source
    assert "python -m ruff check --select E9,F63,F7,F82 ." in source
    assert "--target 85" in source


def test_unexecuted_operator_template_cannot_pass():
    template = json.loads(
        (ROOT / "docs/testnet_validation_evidence.template.json").read_text(encoding="utf-8")
    )
    assert _errors(template)
