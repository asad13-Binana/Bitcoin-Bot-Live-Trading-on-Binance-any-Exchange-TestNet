from __future__ import annotations
"""Immutable package-mode interlock for the separate Bitcoin releases."""

from pathlib import Path


VALID_PACKAGE_MODES = {"live", "testnet"}
ALLOWED_EXECUTION_MODES = {
    "live": {"live", "simulation"},
    "testnet": {"testnet", "simulation"},
}
PACKAGE_MODE_FILE = Path(__file__).resolve().parents[2] / "RELEASE_MODE"


def load_package_mode(path: str | Path | None = None) -> str:
    target = Path(path) if path is not None else PACKAGE_MODE_FILE
    try:
        mode = target.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise SystemExit(
            f"PACKAGE MODE BLOCKED: RELEASE_MODE is unreadable at {target}"
        ) from exc
    if mode not in VALID_PACKAGE_MODES:
        raise SystemExit(
            f"PACKAGE MODE BLOCKED: invalid RELEASE_MODE {mode!r}"
        )
    return mode


def enforce_package_mode(
    execution_mode: str,
    path: str | Path | None = None,
) -> str:
    package = load_package_mode(path)
    execution = str(execution_mode or "").strip().lower()
    if execution not in ALLOWED_EXECUTION_MODES[package]:
        if package == "testnet" and execution == "live":
            raise SystemExit(
                "LIVE BLOCKED: this is the TESTNET package; live execution is "
                "not permitted by any environment setting or evidence file"
            )
        raise SystemExit(
            f"PACKAGE MODE BLOCKED: {package} package does not permit "
            f"EXECUTION_MODE={execution!r}"
        )
    return package
