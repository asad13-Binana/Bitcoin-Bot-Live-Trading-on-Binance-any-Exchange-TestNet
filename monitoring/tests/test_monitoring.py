from __future__ import annotations

import datetime
import inspect
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "a" * 64
os.environ.setdefault("MONITOR_TOKEN", TOKEN)
os.environ.setdefault("MONITOR_ALLOWED_IPS", "127.0.0.1/32,::1/128")
os.environ.setdefault("MONITOR_BIND_HOST", "127.0.0.1")

from monitoring.api import metrics  # noqa: E402
from monitoring.api.app import app  # noqa: E402
from monitoring.api.authentication import _WINDOWS  # noqa: E402
from monitoring.api.configuration import CONFIG, Config, loopback_http_url  # noqa: E402
from monitoring.api.database import query  # noqa: E402
from monitoring.api.log_redaction import redact, redact_obj  # noqa: E402
from monitoring import control  # noqa: E402
from monitoring.mcp import monitor_mcp_server as bridge  # noqa: E402
from monitoring.telegram import telegram_reporter as reporter  # noqa: E402


AUTH = {"Authorization": f"Bearer {TOKEN}"}
client = TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    CONFIG.enabled = True
    CONFIG.telegram_reports_enabled = False
    CONFIG.bind_host = "127.0.0.1"
    CONFIG.token = TOKEN
    CONFIG.allowed_ips = "127.0.0.1/32,::1/128"
    CONFIG.rate_limit_per_minute = 100000
    CONFIG.bot_mode = "testnet"
    CONFIG.bot_dir = tmp_path / "release"
    CONFIG.shared_root = tmp_path / "shared"
    CONFIG.execution_db_path = tmp_path / "execution.sqlite"
    CONFIG.signal_db_path = tmp_path / "signal.sqlite"
    CONFIG.pnl_ledger_path = tmp_path / "pnl.jsonl"
    CONFIG.log_path = tmp_path / "freqtrade.log"
    CONFIG.audit_path = tmp_path / "monitor-audit.jsonl"
    CONFIG.security_audit_path = tmp_path / "security.jsonl"
    CONFIG.container_status_path = tmp_path / "container_status.json"
    CONFIG.sidecar_health_path = tmp_path / "sidecar_health.json"
    CONFIG.telegram_health_path = tmp_path / "telegram_health.json"
    CONFIG.user_stream_health_path = tmp_path / "user_stream_health.json"
    CONFIG.moneyflow_health_path = tmp_path / "moneyflow_health.json"
    CONFIG.moneyflow_status_path = tmp_path / "moneyflow.json"
    CONFIG.active_pair_status_path = tmp_path / "active_pair.json"
    CONFIG.moneyflow_max_age_seconds = 90
    CONFIG.deploy_status_path = tmp_path / "deployment_status.json"
    CONFIG.validation_status_path = tmp_path / "release_validation.json"
    CONFIG.binance_base = "https://testnet.binance.vision"
    _WINDOWS.clear()
    bridge.URL = "http://127.0.0.1:8091"
    bridge.TOKEN = TOKEN
    for name in (
        "TELEGRAM_REPORTS_ENABLED", "TELEGRAM_MONITOR_BOT_TOKEN",
        "TELEGRAM_MONITOR_CHAT_ID", "MONITOR_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _execution_db(path: Path):
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE trade_records (
        trade_id TEXT PRIMARY KEY,pair TEXT,lifecycle_state TEXT,
        filled_quantity TEXT,protected_quantity TEXT,average_entry_price TEXT,
        protection_mode TEXT,last_event_time TEXT,reconciliation_status TEXT,
        updated_at TEXT
      );
      CREATE TABLE exchange_events (
        id INTEGER PRIMARY KEY,event_type TEXT,event_time TEXT,payload_json TEXT
      );
      CREATE TABLE processed_signals (
        signal_id TEXT PRIMARY KEY,result TEXT,processed_at TEXT
      );
    """)
    return connection


def _signal_db(path: Path):
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE trades (
      pair TEXT,close_profit_abs REAL,close_profit REAL,open_date TEXT,
      close_date TEXT,is_open INTEGER,open_rate REAL,amount REAL,stake_amount REAL
    )""")
    return connection


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


# Authentication, authorization, rate limiting, and audit durability.
def test_auth_missing_is_401():
    assert client.get("/api/v1/health").status_code == 401


def test_auth_wrong_is_401():
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_auth_valid_is_200():
    assert client.get("/api/v1/health", headers=AUTH).status_code == 200


def test_auth_uses_constant_time_compare():
    from monitoring.api import authentication
    assert "compare_digest" in inspect.getsource(authentication.require_bearer)


def test_monitor_enabled_flag_is_enforced():
    CONFIG.enabled = False
    assert client.get("/api/v1/health", headers=AUTH).status_code == 503


def test_placeholder_monitor_token_is_rejected():
    CONFIG.token = "replace_on_oracle_only"
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer replace_on_oracle_only"}).status_code == 503


def test_source_allowlist_is_enforced():
    outsider = TestClient(app, client=("10.0.0.1", 50000))
    assert outsider.get("/api/v1/health", headers=AUTH).status_code == 403


def test_invalid_auth_does_not_exhaust_valid_quota():
    CONFIG.rate_limit_per_minute = 1
    for _ in range(3):
        assert client.get("/api/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/health", headers=AUTH).status_code == 200
    assert client.get("/api/v1/health", headers=AUTH).status_code == 429


def test_authorized_request_is_audited():
    client.get("/api/v1/health", headers=AUTH)
    record = json.loads(CONFIG.audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["event"] == "authorized" and record["request_id"]


def test_audit_failure_fails_request_visibly(tmp_path):
    CONFIG.audit_path = tmp_path  # opening a directory as a file must fail
    assert client.get("/api/v1/health", headers=AUTH).status_code == 503


# Redaction regressions from the independent re-audit.
@pytest.mark.parametrize("value,secret", [
    ("Authorization: Bearer abc123", "abc123"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("MONITOR_TOKEN=short123", "short123"),
    ("MCP_AUTH_TOKEN=abcdefghijklmnop", "abcdefghijklmnop"),
    ('password="secret with spaces"', "secret with spaces"),
    ('{"token":"short123"}', "short123"),
    ('{"api_secret": "tiny"}', "tiny"),
    ("1234567:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"),
])
def test_secret_redaction(value, secret):
    output = redact(value)
    assert secret not in output and "[REDACTED]" in output


def test_recursive_redaction_covers_sensitive_fields():
    value = redact_obj({"nested": [{"api_secret": "tiny"}], "text": "token=abcdef"})
    assert value["nested"][0]["api_secret"] == "[REDACTED]"
    assert "abcdef" not in value["text"]


# SQLite, topology, and the known midnight/date-format regression.
def test_database_missing_is_structured():
    assert metrics.execution_state()["error"] == "database_missing"


def test_database_malformed_is_structured():
    CONFIG.execution_db_path.write_text("not sqlite", encoding="utf-8")
    assert metrics.execution_state()["error"].startswith("database_error")


def test_database_helper_rejects_writes(tmp_path):
    database = tmp_path / "x.sqlite"
    sqlite3.connect(database).execute("CREATE TABLE x(v INTEGER)").connection.close()
    assert query(database, "DELETE FROM x")[1] == "query_not_read_only"


def test_signal_date_filter_crosses_midnight_with_space_timestamp(monkeypatch):
    fixed = datetime.datetime(2026, 7, 19, 6, 0, tzinfo=datetime.timezone.utc)

    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(metrics, "dt", SimpleNamespace(
        datetime=FixedDateTime, timezone=datetime.timezone, timedelta=datetime.timedelta
    ))
    connection = _signal_db(CONFIG.signal_db_path)
    connection.execute(
        "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)",
        ("BTC/USDT", 2.0, 0.02, "2026-07-18 17:00:00", "2026-07-18 18:00:00", 0, 1, 1, 1),
    )
    connection.commit(); connection.close()
    result = metrics.signal_performance(1)
    assert result["closed_signals"] == 1


def test_execution_state_is_authoritative_and_separate():
    connection = _execution_db(CONFIG.execution_db_path)
    connection.execute(
        "INSERT INTO trade_records VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("t1", "BTC/USDT", "PROTECTION_ACTIVE", "1", "1", "100", "FIXED_OCO", None, "OK", "2026-07-19T00:00:00+00:00"),
    )
    connection.commit(); connection.close()
    result = metrics.execution_state()
    assert result["open_count"] == 1
    assert result["source"] == "authoritative_execution_state.sqlite"


def test_execution_pnl_ledger_drives_real_performance():
    now = time.time()
    CONFIG.pnl_ledger_path.write_text("\n".join([
        json.dumps({"ts": now - 10, "utc": "x", "symbol": "BTCUSDT", "pnl_pct": 2.0}),
        json.dumps({"ts": now - 5, "utc": "y", "symbol": "BTCUSDT", "pnl_pct": -1.0}),
    ]) + "\n", encoding="utf-8")
    result = metrics.performance(1)
    assert result["closed_trades"] == 2
    assert result["net_pnl_pct"] == 1.0
    assert result["profit_factor"] == 2.0


def test_recent_trades_are_bounded():
    now = time.time()
    CONFIG.pnl_ledger_path.write_text("\n".join(
        json.dumps({"ts": now + i, "symbol": f"S{i}", "pnl_pct": i}) for i in range(5)
    ), encoding="utf-8")
    assert len(metrics.recent_trades(2)["trades"]) == 2


def test_order_quality_uses_structured_execution_events():
    connection = _execution_db(CONFIG.execution_db_path)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    connection.execute("INSERT INTO exchange_events(event_type,event_time,payload_json) VALUES(?,?,?)", ("executionReport", now, json.dumps({"X": "REJECTED"})))
    connection.execute("INSERT INTO processed_signals VALUES(?,?,?)", ("s1", "REJECTED_BY_RISK", now))
    connection.commit(); connection.close()
    result = metrics.order_quality(1)
    assert result["rejected_orders"] == 1 and result["rejected_signals"] == 1


# Bounded logs, actual time windows, health files, market context, and deployment.
def test_log_tail_reads_bounded_suffix():
    CONFIG.MAX_LOG_SCAN_BYTES = 128
    CONFIG.log_path.write_text("old\n" * 1000 + "FINAL_ERROR\n", encoding="utf-8")
    lines, truncated, error = metrics.tail_log(10)
    assert error is None and truncated and lines[-1] == "FINAL_ERROR"


def test_crash_window_is_hours_not_fake_line_count():
    recent = datetime.datetime.now(datetime.timezone.utc).isoformat()
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)).isoformat()
    CONFIG.log_path.write_text(
        f"{old} Traceback (most recent call last):\n ValueError: old\n"
        f"{recent} Traceback (most recent call last):\n RuntimeError: new\n",
        encoding="utf-8",
    )
    assert metrics.crash_blocks(24)["crash_count"] == 1


def test_container_snapshot_replaces_docker_socket_access():
    _write(CONFIG.container_status_path, {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "containers": [
            {"service": service, "status": "running", "health": "healthy", "restart_count": 0}
            for service in ("moneyflow", "freqtrade", "execution-sidecar", "telegram-broker")
        ],
    })
    assert metrics.container_state()["status"] == "healthy"


def test_runtime_health_uses_bot_generated_files():
    now = time.time()
    for path in (CONFIG.sidecar_health_path, CONFIG.telegram_health_path, CONFIG.moneyflow_health_path):
        _write(path, {"ok": True, "ts": now})
    CONFIG.log_path.write_text("freqtrade heartbeat\n", encoding="utf-8")
    assert metrics.runtime_health()["status"] == "healthy"


def test_websocket_state_and_reconnects_are_exposed():
    _write(CONFIG.user_stream_health_path, {
        "ts": time.time(), "connected": True, "subscribed": True,
        "reconnect_count": 3, "mode": "testnet",
    })
    result = metrics.websocket_status()
    assert result["connected"] and result["subscribed"] and result["reconnect_count"] == 3


def test_active_pair_and_moneyflow_context_are_exposed():
    now = time.time()
    _write(CONFIG.active_pair_status_path, {
        "schema_version": 1, "pair": "BTC/USDT", "symbol": "BTCUSDT",
        "base": "BTC", "quote": "USDT", "generation": 1,
        "updated_at": "2026-07-22T00:00:00+00:00", "source": "bootstrap",
        "state_hash": "a" * 64,
    })
    _write(CONFIG.moneyflow_health_path, {
        "ok": True, "ts": now, "pair": "BTC/USDT", "decision": "BULLISH",
    })
    _write(CONFIG.moneyflow_status_path, {
        "schema_version": 1, "ok": True, "generated_at_epoch": now,
        "generated_at": "2026-07-22T00:00:00+00:00", "pair": "BTC/USDT",
        "symbol": "BTCUSDT", "pair_state_hash": "a" * 64,
        "classification": {"bullish": True, "decision": "BULLISH"},
        "spot": {"depth": {"imbalance": 0.2}, "trades": {"taker_buy_ratio": 0.6}},
        "futures": {"available": True, "same_symbol": "BTCUSDT"},
        "timeframes": {"1m": {"direction": "bullish"}, "1d": {"direction": "bullish"}},
        "errors": [],
    })
    pair = metrics.active_pair_status()
    flow = metrics.moneyflow_status()
    assert pair["valid"] and pair["pair"] == "BTC/USDT"
    assert flow["status"] == "healthy" and flow["pair_state_matches"]
    assert flow["classification"]["decision"] == "BULLISH"


def test_deployment_schema_matches_installer_output():
    CONFIG.bot_dir.mkdir()
    (CONFIG.bot_dir / "RELEASE_MODE").write_text("testnet", encoding="utf-8")
    (CONFIG.bot_dir / "RELEASE_SHA256.txt").write_text("b" * 64 + "  RELEASE_MANIFEST.json\n", encoding="utf-8")
    _write(CONFIG.deploy_status_path, {"ok": True, "status": "DEPLOYED", "at": "2026-07-19T01:02:03+00:00"})
    _write(CONFIG.validation_status_path, {"secret_scan": "passed", "manifest_verification": "passed"})
    result = metrics.deployment_info()
    assert result["release_sha256"] == "b" * 64
    assert result["last_deploy"] == "2026-07-19T01:02:03+00:00"
    assert result["validation"]["secret_scan"] == "passed"


def test_recent_security_warnings_are_redacted():
    CONFIG.security_audit_path.write_text(json.dumps({"severity": "CRITICAL", "details": "token=abcdef"}) + "\n", encoding="utf-8")
    value = metrics.recent_security_warnings()["warnings"][0]
    assert "abcdef" not in json.dumps(value)


# MCP and Telegram are loopback/read-only and fail without leaking secrets.
def test_mcp_rejects_non_loopback_url():
    bridge.URL = "https://evil.example"
    assert bridge._get("/health")["ok"] is False


def test_mcp_clamps_arguments_and_sends_token_only_in_header(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(bridge.httpx, "get", fake_get)
    assert bridge._get("/report", days=90)["ok"]
    assert captured["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in captured["url"]


def test_telegram_disabled_flag_is_enforced(capsys):
    assert reporter.main([]) == 1
    assert "reports_disabled" in capsys.readouterr().out


def test_telegram_failure_never_prints_its_token(monkeypatch, capsys):
    telegram = "1234567:" + "S" * 40
    monkeypatch.setenv("TELEGRAM_REPORTS_ENABLED", "true")
    monkeypatch.setenv("MONITOR_URL", "http://127.0.0.1:8091")
    monkeypatch.setenv("MONITOR_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_MONITOR_BOT_TOKEN", telegram)
    monkeypatch.setenv("TELEGRAM_MONITOR_CHAT_ID", "123")
    monkeypatch.setattr(reporter.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(reporter.httpx.ConnectError("down")))
    assert reporter.main([]) == 1
    assert telegram not in capsys.readouterr().out


def test_telegram_checks_json_ok(monkeypatch, capsys):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"ok": False}
    monkeypatch.setattr(reporter.httpx, "post", lambda *a, **k: Response())
    with pytest.raises(reporter.httpx.HTTPError):
        reporter._send("x" * 40, "1", "hello")


# Static integration and release controls.
def test_loopback_url_validation():
    assert loopback_http_url("http://127.0.0.1:8091")
    assert not loopback_http_url("https://127.0.0.1:8091")
    assert not loopback_http_url("http://example.com:8091")


def test_monitor_config_source_does_not_read_trading_credentials():
    source = inspect.getsource(Config)
    for forbidden in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "EXCHANGE_API_KEY", "SIGNAL_HMAC_KEY"):
        assert forbidden not in source


def test_mode_templates_have_correct_topology_ports_and_isolation(monkeypatch):
    simulation = (ROOT / "monitoring/.env.monitor.simulation.example").read_text(encoding="utf-8")
    testnet = (ROOT / "monitoring/.env.monitor.testnet.example").read_text(encoding="utf-8")
    live = (ROOT / "monitoring/.env.monitor.live.example").read_text(encoding="utf-8")
    assert all("/opt/bitcoin-bot/current" in text for text in (simulation, testnet, live))
    assert "BOT_MODE=simulation" in simulation
    assert "BINANCE_REST_BASE=https://api.binance.com" in simulation
    assert "MONITOR_URL=http://127.0.0.1:8091" in simulation
    assert "MONITOR_URL=http://127.0.0.1:8091" in testnet
    assert "MONITOR_URL=http://127.0.0.1:8091" in live
    assert "simulation-audit.jsonl" in simulation
    assert "testnet-audit.jsonl" in testnet and "live-audit.jsonl" in live
    assert "MONITOR_ENABLED=false" in live
    monkeypatch.setenv("BOT_MODE", "simulation")
    monkeypatch.delenv("BINANCE_REST_BASE", raising=False)
    simulation_config = Config()
    assert simulation_config.binance_base == "https://api.binance.com"
    assert simulation_config.banner() == "MODE: SIMULATION - NO EXCHANGE ORDERS"


def test_gitignore_keeps_monitor_examples():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!**/.env.*.example" in text


def test_systemd_pairs_and_hardening():
    units = ROOT / "monitoring/systemd"
    services = {path.stem for path in units.glob("*.service")}
    for timer in units.glob("*.timer"):
        text = timer.read_text(encoding="utf-8")
        target = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("Unit=")), timer.with_suffix(".service").name)
        assert Path(target).stem in services
    for mode in ("simulation", "testnet", "live"):
        api = (units / f"bitcoin-bot-monitor-{mode}.service").read_text(encoding="utf-8")
        assert "User=botmon" in api and "ProtectSystem=strict" in api and "PrivateDevices=true" in api
        assert f"/etc/bitcoin-bot/{mode}-monitor.env" in api
        assert "docker.sock" not in api


def test_ci_and_release_gate_include_monitoring():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    verifier = (ROOT / "deploy/verify_release.sh").read_text(encoding="utf-8")
    assert "requirements-monitoring.lock" in workflow
    assert "pytest -q monitoring/tests" in verifier
    assert "systemd-analyze verify" in verifier
    assert "scripts/build_manifest.py" not in verifier


def test_oracle_installer_installs_monitoring():
    installer = (ROOT / "deploy/install_artifact.sh").read_text(encoding="utf-8")
    setup = (ROOT / "deploy/oracle_setup.sh").read_text(encoding="utf-8")
    assert "install_monitoring.sh" in installer
    assert "useradd --system" in setup and "python3-venv" in setup


def test_every_api_route_is_get_only_and_has_no_order_route():
    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/"):
            assert route.methods == {"GET"}
            assert not any(word in route.path.lower() for word in ("order", "cancel", "buy", "sell"))


def test_docs_are_disabled_by_default():
    assert not any(getattr(route, "path", "") == "/docs" for route in app.routes)


def test_control_requires_explicit_telegram_enable(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REPORTS_ENABLED", "false")
    CONFIG.telegram_reports_enabled = False
    assert control.check("telegram")[0] is False
