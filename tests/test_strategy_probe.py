from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "freqtrade" / "tests" / "audit_probes.py"


def test_active_strategy_gate_and_prefix_determinism():
    for module in ("numpy", "pandas", "talib"):
        try:
            __import__(module)
        except ImportError as exc:
            pytest.skip(
                f"optional strategy stack missing ({module}); "
                "the pinned Freqtrade container runs this probe"
            )
    spec = importlib.util.spec_from_file_location("bitcoin_strategy_probe", PROBE)
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    result = probe.strategy_smoke()
    probe.assert_strategy_smoke(result)
