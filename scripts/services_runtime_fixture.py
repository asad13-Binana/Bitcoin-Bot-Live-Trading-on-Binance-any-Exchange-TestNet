"""Container-only deterministic public Spot collection; never submit an order."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time

STRATEGY_HASH = "023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340"


class PublicSpotFixture:
    """Only the public endpoint surface consumed by the real MoneyFlowClient."""

    def __init__(self):
        self.calls = []

    def exchange_info(self, symbol):
        assert symbol == "BTCUSDT"
        self.calls.append("exchangeInfo")
        return {"symbols": [{"symbol": symbol, "baseAsset": "BTC", "quoteAsset": "USDT",
                              "status": "TRADING", "isSpotTradingAllowed": True,
                              "permissions": ["SPOT"], "ocoAllowed": True, "otoAllowed": True,
                              "filters": [
                                  {"filterType": "PRICE_FILTER", "minPrice": "0.01",
                                   "maxPrice": "1000000", "tickSize": "0.01"},
                                  {"filterType": "LOT_SIZE", "minQty": "0.00001",
                                   "maxQty": "9000", "stepSize": "0.00001"},
                              ]}]}

    def get(self, path, params):
        assert params["symbol"] == "BTCUSDT"
        self.calls.append(path)
        if path == "/api/v3/depth":
            return {"bids": [["99999", "2"]], "asks": [["100001", "1"]]}
        if path == "/api/v3/aggTrades":
            return [{"a": 1, "p": "100000", "q": "1", "m": False}]
        if path == "/api/v3/klines":
            assert params["interval"] in ("1m", "5m", "15m", "1h", "2h", "4h", "1d")
            return [[i, "100000", "100001", "99999", str(100000 + i), "1", i + 1]
                    for i in range(64)]
        raise AssertionError("non-public or unexpected endpoint requested")


def denied(action):
    try:
        action()
    except (PermissionError, OSError) as exc:
        assert exc.errno in (1, 13, 30), exc  # EPERM, EACCES or EROFS only
    else:
        raise AssertionError("immutable or foreign-owned path was writable")


def inspect_application(uid, gid):
    assert (os.getuid(), os.getgid()) == (uid, gid)
    assert uid != 0 and gid != 0
    root = Path("/app")
    for path in (root, *root.rglob("*")):
        assert not path.is_symlink(), path
        stat = path.stat()
        assert (stat.st_uid, stat.st_gid) == (0, 0), path
        assert stat.st_mode & 0o222 == 0, path
        if path.is_dir():
            assert os.access(path, os.R_OK | os.X_OK), path
        else:
            path.read_bytes()
    for name in ("services", "services.moneyflow.service", "services.execution_sidecar.main",
                 "services.telegram_broker.bot"):
        importlib.import_module(name)
    strategy = root / "freqtrade/user_data/strategies/IctSmcStrategy.py"
    assert hashlib.sha256(strategy.read_bytes()).hexdigest() == STRATEGY_HASH
    assert (root / "RELEASE_MODE").read_text().strip() in {"testnet", "live"}
    for name in ("RELEASE_MANIFEST.json", "VALIDATION_STATUS.json"):
        assert isinstance(json.loads((root / name).read_text()), dict)
    assert len((root / "RELEASE_SHA256.txt").read_text().split()[0]) == 64
    denied(lambda: (root / "services/__init__.py").open("a"))
    denied(lambda: (root / "services/new_module.py").write_text("forbidden"))
    denied(lambda: (root / "services/__init__.py").chmod(0o644))
    denied(lambda: (root / "RELEASE_MODE").write_text("forbidden"))
    Path("/tmp/probe").write_text("temporary fixture")
    print(f"APPLICATION_IMPORT_READ_AND_DAC_UID_{uid}_GID_{gid}=PASS")


def collection(uid, gid):
    from services.moneyflow.client import MoneyFlowClient
    from services.moneyflow.service import TIMEFRAMES, run_once

    assert (os.getuid(), os.getgid()) == (uid, gid)
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN"):
        assert not os.getenv(name), name
    for relative in ("moneyflow", "runtime/moneyflow", "audit"):
        marker = Path("/app/shared") / relative / "probe"
        marker.write_text("owned synthetic state")
        marker.unlink()
    denied(lambda: Path("/app/shared/pair/active_pair.json").open("a"))
    denied(lambda: Path("/app/services/__init__.py").open("a"))
    fixture = PublicSpotFixture()
    started = time.time()
    snapshot = run_once(client=MoneyFlowClient(spot=fixture))
    latest = json.loads(Path("/app/shared/moneyflow/latest.json").read_text())
    health = json.loads(Path("/app/shared/runtime/moneyflow/moneyflow_health.json").read_text())
    assert latest == snapshot and latest["ok"] is True and health["ok"] is True
    assert latest["pair"] == health["pair"] == "BTC/USDT"
    assert started <= health["ts"] <= time.time()
    assert started <= latest["generated_at_epoch"] <= time.time()
    assert set(latest["timeframes"]) == set(TIMEFRAMES)
    assert all(row["direction"] != "unavailable" for row in latest["timeframes"].values())
    assert len(fixture.calls) == 10
    assert latest["market_context_mode"] == "spot_only"
    assert latest["futures"]["disabled"] is True
    assert latest["external_context"]["advisory_only"] is True
    print(f"MONEYFLOW_REAL_RUN_ONCE_FRESH_OUTPUT_UID_{uid}_GID_{gid}=PASS")


if __name__ == "__main__":
    action, uid, gid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    if action == "inspect":
        inspect_application(uid, gid)
    elif action == "collect":
        collection(uid, gid)
    else:
        raise SystemExit("unknown proof action")
