from __future__ import annotations

"""Validate retained, redacted Binance Spot Testnet lifecycle evidence.

This verifier never calls Binance and never handles credentials.  It turns a
real external drill into a deterministic release-bound record.  A template or
mocked result cannot pass because every mandatory case must be marked PASS and
bound to a SHA-256 evidence artifact.
"""

import argparse
import datetime as dt
import json
import re
from pathlib import Path


HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TESTNET_BASE = "https://testnet.binance.vision"
REQUIRED_CASES = (
    "full_fill",
    "partial_fill",
    "cancel_fill_race",
    "accepted_timeout_query_before_retry",
    "restart_reconciliation",
    "user_stream_reconnect",
    "duplicate_signal_idempotency",
    "monitoring_api_authentication",
    "telegram_delivery",
)
FORBIDDEN_KEYS = {
    "api_key", "api_secret", "secret", "token", "private_key", "credential",
    "binance_api_key", "binance_api_secret", "telegram_bot_token",
}


def _utc(value: object, field: str, errors: list[str]) -> dt.datetime | None:
    if not isinstance(value, str):
        errors.append(f"{field} must be an ISO-8601 UTC string")
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field} is not valid ISO-8601")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        errors.append(f"{field} must be UTC")
        return None
    return parsed


def _walk_keys(value: object, errors: list[str], prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key).lower()
            location = f"{prefix}.{key}" if prefix else str(key)
            if name in FORBIDDEN_KEYS:
                errors.append(f"forbidden credential field: {location}")
            _walk_keys(child, errors, location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_keys(child, errors, f"{prefix}[{index}]")


def validate(
    evidence: object, *, expected_repository: str, expected_commit: str,
    expected_manifest_sha256: str,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(evidence, dict):
        return ["evidence root must be an object"]
    _walk_keys(evidence, errors)
    expected = {
        "schema_version": 1,
        "repository": expected_repository,
        "commit_sha": expected_commit,
        "release_manifest_sha256": expected_manifest_sha256,
        "package_mode": "testnet",
        "binance_base": TESTNET_BASE,
        "performed_on_binance_spot_testnet": True,
        "live_orders_attempted": False,
        "credentials_redacted": True,
        "operator_approved": True,
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            errors.append(f"{key} must equal {value!r}")
    if not HEX40.fullmatch(str(expected_commit)):
        errors.append("expected commit must be a lowercase 40-character SHA-1")
    if not HEX64.fullmatch(str(expected_manifest_sha256)):
        errors.append("expected manifest digest must be lowercase SHA-256")

    started = _utc(evidence.get("started_at"), "started_at", errors)
    completed = _utc(evidence.get("completed_at"), "completed_at", errors)
    if started and completed and completed <= started:
        errors.append("completed_at must be later than started_at")

    cases = evidence.get("cases")
    if not isinstance(cases, dict):
        errors.append("cases must be an object")
        return errors
    for name in REQUIRED_CASES:
        case = cases.get(name)
        if not isinstance(case, dict):
            errors.append(f"missing mandatory case: {name}")
            continue
        if case.get("status") != "PASS":
            errors.append(f"{name}.status must be PASS")
        if not isinstance(case.get("observations"), int) or case["observations"] < 1:
            errors.append(f"{name}.observations must be a positive integer")
        if not HEX64.fullmatch(str(case.get("evidence_sha256", ""))):
            errors.append(f"{name}.evidence_sha256 must be lowercase SHA-256")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: evidence is unreadable: {exc}")
        return 1
    errors = validate(
        evidence,
        expected_repository=args.expected_repository,
        expected_commit=args.expected_commit,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    if errors:
        print("FAIL: external Testnet evidence is not certifiable")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("PASS: external Testnet lifecycle evidence is complete and release-bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
