from __future__ import annotations

"""Enforce a non-regressing coverage floor while keeping the 85% target explicit."""

import argparse
import json
from pathlib import Path


def measured_percent(path: Path) -> float:
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("totals", {}).get("percent_covered")
    if not isinstance(value, (int, float)):
        raise ValueError("coverage JSON lacks totals.percent_covered")
    return float(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("--minimum", type=float, required=True)
    parser.add_argument("--target", type=float, default=85.0)
    args = parser.parse_args(argv)
    try:
        actual = measured_percent(args.coverage_json)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: coverage evidence is invalid: {exc}")
        return 1
    print(f"coverage={actual:.2f}% non-regression-floor={args.minimum:.2f}% target={args.target:.2f}%")
    if actual < args.minimum:
        print("FAIL: coverage regressed below the recorded floor")
        return 1
    if actual < args.target:
        print("TARGET PENDING: coverage has not yet reached 85%; no certification is claimed")
    else:
        print("TARGET MET: coverage reached the configured target")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
