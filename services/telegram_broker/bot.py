from __future__ import annotations
"""Owner-only Telegram control plane for the Bitcoin Spot bot."""

import json
import logging
import math
import os
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import time
import uuid

import requests
from requests.auth import HTTPBasicAuth

from services.common import envelope
from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.market_policy import canonical_pair
from services.common.paths import (
    ACTIVE_PAIR_FILE, AUDIT_DIR, COMMAND_INBOX, COMMAND_RESULTS_DIR, MONEYFLOW_FILE, RUNTIME,
    SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED,
)
from services.common.redaction import redact_text
from services.telegram_broker.authorization import is_owner
from services.telegram_broker.callbacks import CallbackStore


log = logging.getLogger("telegram-broker")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
BASE = f"https://api.telegram.org/bot{TOKEN}"
CB = CallbackStore(ttl=int(os.getenv("CALLBACK_TTL_SECONDS", "120")))
FT_BASE = os.getenv("FREQTRADE_API_URL", "http://freqtrade:8080/api/v1").rstrip("/")
FT_USER = os.getenv("FREQTRADE_API_USERNAME", "freqtrade")
FT_PASS = os.getenv("FREQTRADE_API_PASSWORD", "")
OFFSET_PATH = RUNTIME / "telegram_offset.json"
UPDATE_DB_PATH = Path(os.getenv(
    "TELEGRAM_UPDATE_DB_PATH", str(RUNTIME / "telegram_updates.sqlite3")))
UPDATE_RETENTION = max(100, int(os.getenv("TELEGRAM_UPDATE_RETENTION", "10000")))
SIDECAR_RUNTIME = Path(os.getenv("SIDECAR_RUNTIME_DIR", str(RUNTIME)))
DEPLOYMENT_STATUS_FILE = Path(os.getenv(
    "DEPLOYMENT_STATUS_FILE", str(RUNTIME / "deployment_status.json")))
LIVE_EVIDENCE_FILE = Path(os.getenv(
    "LIVE_EVIDENCE_FILE", str(SIDECAR_RUNTIME / "LIVE_EVIDENCE.json")))


class TelegramUpdateStore:
    """Durably claim Telegram updates before any control action is handled.

    A claim and the next Telegram offset are committed in one SQLite
    transaction.  A process crash can therefore drop one uncertain command,
    which the owner may safely reissue, but cannot execute the same update
    twice.  This is intentionally fail-closed rather than pretending that an
    external Telegram request and local side effects can be exactly-once.
    """

    SCHEMA_VERSION = 1
    FINAL_STATES = {"handled", "failed", "uncertain"}

    def __init__(
        self,
        path: Path,
        *,
        legacy_offset_path: Path | None = None,
        retention: int = 10000,
        now=time.time,
    ):
        self.path = Path(path)
        self.legacy_offset_path = legacy_offset_path
        self.retention = max(100, int(retention))
        self.now = now
        if self.path.is_symlink():
            raise RuntimeError("Telegram update database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path, timeout=10.0, isolation_level=None)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
        except Exception:
            self.connection.close()
            raise

    def _initialize(self) -> None:
        version = int(self.connection.execute(
            "PRAGMA user_version").fetchone()[0])
        if version not in {0, self.SCHEMA_VERSION}:
            raise RuntimeError(
                f"unsupported Telegram update schema version: {version}")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_update_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL CHECK (
                        state IN ('claimed', 'handled', 'failed', 'uncertain')
                    ),
                    claimed_at REAL NOT NULL,
                    completed_at REAL,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO telegram_update_meta(key, value) "
                "VALUES ('offset', '0')"
            )
            legacy_offset = self._legacy_offset()
            if legacy_offset is not None:
                current = self._offset_unlocked()
                self._set_offset_unlocked(max(current, legacy_offset))
            recovered = self.connection.execute(
                "UPDATE telegram_updates SET state='uncertain', "
                "completed_at=?, error='process-stopped-after-durable-claim' "
                "WHERE state='claimed'",
                (float(self.now()),),
            ).rowcount
            self.connection.execute(
                f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self.connection.execute("COMMIT")
            self.recovered_uncertain = int(recovered)
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def _legacy_offset(self) -> int | None:
        path = self.legacy_offset_path
        if path is None or not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("legacy Telegram offset path is unsafe")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            offset = int(payload["offset"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("legacy Telegram offset is malformed") from exc
        if offset < 0:
            raise RuntimeError("legacy Telegram offset must be non-negative")
        return offset

    def _offset_unlocked(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM telegram_update_meta WHERE key='offset'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Telegram update offset metadata is missing")
        try:
            offset = int(row[0])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Telegram update offset metadata is malformed") from exc
        if offset < 0:
            raise RuntimeError("Telegram update offset metadata is negative")
        return offset

    def _set_offset_unlocked(self, offset: int) -> None:
        self.connection.execute(
            "UPDATE telegram_update_meta SET value=? WHERE key='offset'",
            (str(int(offset)),),
        )

    def offset(self) -> int:
        return self._offset_unlocked()

    def claim(self, update_id: int) -> bool:
        update_id = int(update_id)
        if not 0 <= update_id < 2**63 - 1:
            raise ValueError("Telegram update_id is outside the SQLite range")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current_offset = self._offset_unlocked()
            if update_id < current_offset:
                self.connection.execute("COMMIT")
                return False
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO telegram_updates"
                "(update_id, state, claimed_at) VALUES (?, 'claimed', ?)",
                (update_id, float(self.now())),
            ).rowcount
            self._set_offset_unlocked(max(current_offset, update_id + 1))
            if inserted:
                self.connection.execute(
                    "DELETE FROM telegram_updates WHERE update_id IN ("
                    "SELECT update_id FROM telegram_updates "
                    "WHERE state != 'claimed' ORDER BY update_id DESC "
                    "LIMIT -1 OFFSET ?)",
                    (self.retention,),
                )
            self.connection.execute("COMMIT")
            return bool(inserted)
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def complete(self, update_id: int, state: str, error: str = "") -> None:
        if state not in self.FINAL_STATES:
            raise ValueError("invalid Telegram update final state")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            updated = self.connection.execute(
                "UPDATE telegram_updates SET state=?, completed_at=?, error=? "
                "WHERE update_id=? AND state='claimed'",
                (state, float(self.now()), redact_text(error)[:500], int(update_id)),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Telegram update claim is missing or already final")
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def uncertain_count(self) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM telegram_updates WHERE state='uncertain'"
        ).fetchone()[0])

    def close(self) -> None:
        self.connection.close()


def _safe_error(exc) -> str:
    return redact_text(exc)[:500]


def send(text, chat_id=None, buttons=None):
    if not TOKEN:
        return
    data = {"chat_id": chat_id or OWNER, "text": redact_text(text)[:4000]}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    response = requests.post(BASE + "/sendMessage", data=data, timeout=15)
    response.raise_for_status()


def edit_or_send(text, chat_id, message_id=None, buttons=None):
    """Edit a callback menu in place, with a presentation-only fallback."""
    if not TOKEN:
        return
    if message_id is None:
        send(text, chat_id, buttons)
        return
    data = {"chat_id": chat_id or OWNER, "message_id": message_id,
            "text": redact_text(text)[:4000]}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        response = requests.post(BASE + "/editMessageText", data=data, timeout=15)
        if (
            response.status_code == 400
            and "message is not modified" in response.text.lower()
        ):
            return
        response.raise_for_status()
    except Exception as exc:
        log.debug("editMessageText failed; sending a new menu: %s", _safe_error(exc))
        send(text, chat_id, buttons)


def sidecar_command(name, args=None, wait=False):
    cid = uuid.uuid4().hex
    payload = {"command_id": cid, "command": name, "args": args or {},
               "created_at": time.time()}
    signed = envelope.sign_envelope(
        producer="telegram-broker", purpose=envelope.BUS_COMMAND, payload=payload,
        ttl_seconds=int(os.getenv("COMMAND_MAX_AGE_SECONDS", "120")) + 30)
    atomic_write_json(COMMAND_INBOX / f"{cid}.json", signed)
    audit("telegram_command_enqueued", actor="telegram-owner", details={
        "command_id": cid, "command": name})
    if not wait:
        return {"ok": True, "result": "queued", "command_id": cid}
    result_path = COMMAND_RESULTS_DIR / f"command_result_{cid}.json"
    deadline = time.time() + 12
    while time.time() < deadline:
        result = read_json(result_path, None)
        if result is not None:
            result_path.unlink(missing_ok=True)
            return result
        time.sleep(0.2)
    return {"ok": False,
            "result": "command outcome is uncertain; inspect status/reconcile before any retry",
            "command_id": cid}


def ft_call(method, endpoint):
    if not FT_PASS:
        return {"ok": False, "error": "Freqtrade API password not configured"}
    try:
        response = requests.request(
            method, FT_BASE + endpoint, auth=HTTPBasicAuth(FT_USER, FT_PASS), timeout=12)
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception:
            payload = {"text": response.text[:1000]}
        return {"ok": True, "data": payload}
    except Exception as exc:
        return {"ok": False, "error": _safe_error(exc)}


def normalize_pair_input(text: str) -> tuple[str | None, str]:
    try:
        return canonical_pair(str(text or "")), "ok"
    except Exception as exc:
        return None, str(exc)


def confirm_button(label, action, args=None):
    token = CB.issue(action, args)
    audit("telegram_confirmation_issued", actor="telegram-owner", details={"action": action})
    return {"text": label, "callback_data": "confirm|" + token}


def _ask_confirm(chat, label, action, args=None, text="Confirm action."):
    confirmation = confirm_button(label, action, args)
    token = confirmation["callback_data"].split("|", 1)[1]
    send(text, chat, [[confirmation],
                      [{"text": "Cancel", "callback_data": "cancel|" + token}]])


def menu():
    """Compact owner panel; every previous Bitcoin control stays reachable."""
    return [
        [{"text": "Dashboard", "callback_data": "do|menu_dashboard"},
         {"text": "Trading", "callback_data": "do|menu_trading"}],
        [{"text": "System", "callback_data": "do|menu_system"},
         {"text": "Emergency", "callback_data": "do|menu_emergency"}],
        [{"text": "Help", "callback_data": "do|help"}],
    ]


def dashboard_menu():
    return [
        [{"text": "Status", "callback_data": "do|status"},
         {"text": "Balance", "callback_data": "do|balance"}],
        [{"text": "Profit report", "callback_data": "do|profit"},
         {"text": "Last signal", "callback_data": "do|last_signal"}],
        [{"text": "Active BTC pair", "callback_data": "do|pair"},
         {"text": "Choose BTC pair", "callback_data": "do|pairs"}],
        [{"text": "Money flow", "callback_data": "do|flow"}],
        [{"text": "Home", "callback_data": "do|home"}],
    ]


def trading_menu():
    return [
        [{"text": "Resume entries", "callback_data": "do|entries_on"},
         {"text": "Pause entries", "callback_data": "do|entries_off"}],
        [{"text": "Manual swap done", "callback_data": "do|swap_done"},
         {"text": "Verify pair reload", "callback_data": "do|verify_pair"}],
        [{"text": "Fixed OCO", "callback_data": "do|mode_fixed"},
         {"text": "OCO + trailing", "callback_data": "do|mode_oco_trailing"}],
        [{"text": "Trailing only", "callback_data": "do|mode_trailing"},
         {"text": "Tight trailing help", "callback_data": "do|trailing_help"}],
        [{"text": "Break-even help", "callback_data": "do|be_help"},
         {"text": "Profit-lock help", "callback_data": "do|profit_help"}],
        [{"text": "Reconcile", "callback_data": "do|reconcile"},
         {"text": "Home", "callback_data": "do|home"}],
    ]


def system_menu():
    return [
        [{"text": "Restart user stream", "callback_data": "do|restart_stream"}],
        [{"text": "Logs", "callback_data": "do|logs"},
         {"text": "Deployment", "callback_data": "do|deploy"}],
        [{"text": "Self audit", "callback_data": "do|audit"}],
        [{"text": "Backtest gate", "callback_data": "do|backtest"},
         {"text": "Settings", "callback_data": "do|settings"}],
        [{"text": "Home", "callback_data": "do|home"}],
    ]


def emergency_menu():
    return [
        [{"text": "Emergency help", "callback_data": "do|emergency_help"},
         {"text": "Pause entries", "callback_data": "do|entries_off"}],
        [{"text": "Status", "callback_data": "do|status"},
         {"text": "Home", "callback_data": "do|home"}],
    ]


def help_text():
    return (
        "Commands: /menu /status /start /stop /balance /profit /pair /pairs /flow "
        "/switchpair BTC/USDC /swapdone /verifypair "
        "/fixed_oco /oco_trailing /trailing_only "
        "/convert BTCUSDT MODE /breakeven BTCUSDT /lockprofit BTCUSDT PCT "
        "/tighttrail BTCUSDT BIPS /autoprotection on|off /reconcile /restartws "
        "/emergency BTCUSDT /setsize QUOTE_AMOUNT /setmax 1 /logs /deploy /audit "
        "/lastsignal /backtest /settings. Pair changes and money-affecting actions require "
        "one-time confirmation. Entries remain off after restart, switch or ambiguity."
    )


def _latest_signal():
    files = []
    for folder in (SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED):
        files.extend(folder.glob("*.json"))
    if not files:
        return "No signal file has been recorded."
    path = max(files, key=lambda item: item.stat().st_mtime)
    return json.dumps({"location": path.parent.name, "signal": read_json(path, {})},
                      indent=2)[:3800]


def _tail_audit(lines=30):
    path = AUDIT_DIR / "events.jsonl"
    if not path.exists():
        return "No audit events recorded."
    # Bounded suffix read without loading an unbounded log into memory.
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - 64 * 1024))
        data = handle.read(64 * 1024).decode("utf-8", "replace")
    return redact_text("\n".join(data.splitlines()[-lines:]))[-3800:]


def _settings():
    return json.dumps({
        "release_hash": envelope.installed_release_hash(),
        "active_pair": read_json(ACTIVE_PAIR_FILE, {}),
        "sidecar": read_json(SIDECAR_RUNTIME / "sidecar_health.json", {}),
        "configured": {
            "execution_mode": os.getenv("EXECUTION_MODE", "simulation"),
            "optional_btc_quote_allowlist": (
                os.getenv("BTC_QUOTE_ALLOWLIST")
                if os.getenv("BTC_QUOTE_ALLOWLIST") is not None
                else os.getenv("ALLOWED_STABLE_QUOTES", "")
            ),
            "require_flow_context": os.getenv("REQUIRE_FLOW_CONTEXT", "false"),
            "market_context_mode": "spot_only",
            "require_external_confluence": os.getenv(
                "REQUIRE_EXTERNAL_CONFLUENCE", "false"
            ),
            "telegram_owner_configured": bool(OWNER),
            "freqtrade_api_password_configured": bool(FT_PASS),
        }}, indent=2)


def _backtest_status():
    if LIVE_EVIDENCE_FILE.exists():
        return "Signed live-evidence envelope is installed. Use monitoring to inspect hashes."
    return ("NO SIGNED LIVE EVIDENCE. Strategy profitability is unproven. Run exact-strategy "
             "backtest, lookahead/recursive analysis, Testnet lifecycle and Oracle soak first.")


def _self_audit(now: float | None = None) -> dict:
    """Return a bounded, read-only health and safe-state consistency report."""
    epoch = time.time() if now is None else float(now)
    checks: dict[str, dict] = {}
    issues: list[str] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": redact_text(detail)[:240]}
        if not ok:
            issues.append(name)

    def object_file(path: Path, label: str) -> dict:
        value = read_json(path, None)
        ok = isinstance(value, dict)
        record(label + "_file", ok, "loaded" if ok else "missing_or_invalid")
        return value if ok else {}

    def health(name: str, row: dict, maximum_age: float) -> None:
        try:
            age = epoch - float(row.get("ts"))
            fresh = -5 <= age < maximum_age
        except (TypeError, ValueError):
            age = None
            fresh = False
        record(name + "_healthy", row.get("ok") is True, str(row.get("ok")))
        record(
            name + "_fresh",
            fresh,
            f"age_seconds={round(age, 3)}" if age is not None else "timestamp_invalid",
        )

    deployment_root = DEPLOYMENT_STATUS_FILE.parent
    deployment = object_file(DEPLOYMENT_STATUS_FILE, "deployment")
    validation = object_file(deployment_root / "release_validation.json", "release_validation")
    sidecar = object_file(SIDECAR_RUNTIME / "sidecar_health.json", "sidecar")
    moneyflow = object_file(
        deployment_root / "moneyflow" / "moneyflow_health.json", "moneyflow"
    )
    telegram = object_file(RUNTIME / "telegram_health.json", "telegram")
    active_pair = object_file(ACTIVE_PAIR_FILE, "active_pair")
    freqtrade = ft_call("GET", "/ping")

    configured_mode = os.getenv("EXECUTION_MODE", "simulation")
    expected_release = os.getenv("DEPLOYED_RELEASE_HASH", "").strip()
    envelope_release = envelope.installed_release_hash()
    pair = str(active_pair.get("pair", ""))

    record(
        "deployment_active",
        deployment.get("ok") is True and deployment.get("status") == "DEPLOYED",
        str(deployment.get("status", "missing")),
    )
    record(
        "release_validation",
        validation.get("ok") is True
        and validation.get("outcome") == "DEPLOYED"
        and validation.get("container_health_gate") == "passed"
        and validation.get("monitoring_health_gate") == "passed",
        str(validation.get("outcome", "missing")),
    )
    release_values = (
        expected_release,
        envelope_release,
        str(deployment.get("release_hash", "")),
        str(validation.get("release_hash", "")),
    )
    release_hashes_valid = all(
        len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
        for value in release_values
    )
    record(
        "release_identity",
        release_hashes_valid and len(set(release_values)) == 1,
        "consistent" if release_hashes_valid and len(set(release_values)) == 1 else "mismatch",
    )
    deployment_path = str(deployment.get("release_path", ""))
    validation_path = str(validation.get("release_path", ""))
    record(
        "release_path_identity",
        bool(deployment_path) and deployment_path == validation_path,
        "consistent" if bool(deployment_path) and deployment_path == validation_path else "mismatch",
    )
    record(
        "execution_mode",
        configured_mode in {"simulation", "testnet", "live"}
        and sidecar.get("execution_mode") == configured_mode,
        configured_mode,
    )
    record(
        "deployment_mode",
        deployment.get("execution_mode") == configured_mode,
        str(deployment.get("execution_mode", "missing")),
    )
    record(
        "release_validation_mode",
        validation.get("execution_mode") == configured_mode,
        str(validation.get("execution_mode", "missing")),
    )
    record(
        "simulation_flag",
        sidecar.get("simulation") is (configured_mode == "simulation"),
        str(sidecar.get("simulation")),
    )
    record(
        "entries_off",
        sidecar.get("entries_enabled") is False,
        str(sidecar.get("entries_enabled")),
    )
    record(
        "no_unresolved_intents",
        sidecar.get("unresolved_intents") == 0,
        str(sidecar.get("unresolved_intents")),
    )
    record(
        "btc_pair_consistency",
        pair.startswith("BTC/") and sidecar.get("active_pair") == pair,
        pair or "missing",
    )
    record(
        "moneyflow_pair_consistency",
        moneyflow.get("pair") == pair,
        str(moneyflow.get("pair", "missing")),
    )
    record(
        "pair_switch_settled",
        sidecar.get("pair_switch_stage") in {"ACTIVE", "PAUSED_READY"},
        str(sidecar.get("pair_switch_stage", "missing")),
    )
    record("freqtrade_ping", freqtrade.get("ok") is True, "ok" if freqtrade.get("ok") else "failed")
    health("sidecar", sidecar, 30)
    health("moneyflow", moneyflow, 90)
    health("telegram", telegram, 180)

    result = {
        "ok": not issues,
        "read_only": True,
        "checked_at": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "execution_mode": configured_mode,
        "active_pair": pair,
        "checks": checks,
        "issues": issues,
        "note": "This command reports health and safe state; it never repairs or resumes entries.",
    }
    audit(
        "telegram_self_audit",
        actor="telegram-owner",
        severity="INFO" if result["ok"] else "WARNING",
        details={"ok": result["ok"], "issues": issues},
    )
    return result


def _flow_summary():
    """Bound owner-visible flow output; never dump arbitrary provider payloads."""
    source = read_json(MONEYFLOW_FILE, {})
    if not isinstance(source, dict):
        return {"available": False, "reason": "invalid_moneyflow_snapshot"}
    # This is an owner-visible historical snapshot, not a live service probe.
    # Never repeat stored ok/fresh flags as current health after a rollback.
    generated = source.get("generated_at_epoch")
    age = time.time() - generated if type(generated) in (int, float) else None
    fresh = age is not None and math.isfinite(age) and -30 < age < 45
    current_ok = source.get("ok") is True and fresh
    external = source.get("external_context")
    external = external if isinstance(external, dict) else {}
    rows = external.get("providers")
    rows = rows if isinstance(rows, dict) else {}
    providers = {}
    for provider in ("coingecko", "coinmarketcap"):
        row = rows.get(provider)
        if not isinstance(row, dict):
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        quota = row.get("quota") if isinstance(row.get("quota"), dict) else {}
        providers[provider] = {
            "status": row.get("status"),
            "available": row.get("available") is True and current_ok,
            "fresh": row.get("fresh") is True and current_ok,
            "cache_age_seconds": row.get("cache_age_seconds"),
            "price_usd": data.get("price_usd"),
            "volume_24h_usd": data.get("volume_24h_usd"),
            "percent_change_24h": data.get("percent_change_24h"),
            "monthly_attempts": quota.get("monthly_attempts_reserved"),
            "monthly_cap": quota.get("monthly_attempt_cap"),
        }
    return {
        "pair": source.get("pair"),
        "generated_at": source.get("generated_at"),
        "ok": current_ok,
        "fresh": fresh,
        "snapshot_ok": source.get("ok") is True,
        "status": "fresh_snapshot_not_service_health" if current_ok else "stale_or_unavailable_snapshot",
        "age_seconds": round(age, 3) if age is not None and math.isfinite(age) else None,
        "classification": source.get("classification")
        if current_ok and isinstance(source.get("classification"), dict)
        else {"bullish": False, "decision": "UNAVAILABLE"},
        "spot": source.get("spot") if isinstance(source.get("spot"), dict) else {},
        "futures": source.get("futures") if isinstance(source.get("futures"), dict) else {},
        "external_context": {
            "advisory_only": external.get("advisory_only") is True,
            "affects_entry_decision": external.get("affects_entry_decision") is True,
            "providers": providers,
            "attribution": external.get("attribution")
            if isinstance(external.get("attribution"), dict) else {},
        },
    }


def _sidecar_json_result(name: str) -> tuple[dict | None, str | None]:
    response = sidecar_command(name, wait=True)
    if response.get("ok") is not True:
        return None, str(response.get("result", "sidecar command failed"))
    try:
        payload = json.loads(str(response.get("result", "")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "sidecar returned malformed pair-registry data"
    if not isinstance(payload, dict):
        return None, "sidecar returned malformed pair-registry data"
    return payload, None


def _show_pairs(chat, page=0):
    registry, error = _sidecar_json_result("pairs")
    if registry is None:
        send("BTC pair menu unavailable: " + str(error), chat)
        return
    rows = registry.get("pairs")
    if not isinstance(rows, list) or not rows:
        send("BTC pair menu unavailable: registry contains no eligible pairs.", chat)
        return
    per_page = 8
    pages = max(1, math.ceil(len(rows) / per_page))
    page = max(0, min(int(page), pages - 1))
    active = read_json(ACTIVE_PAIR_FILE, {}) or {}
    buttons = []
    for row in rows[page * per_page:(page + 1) * per_page]:
        if not isinstance(row, dict) or not row.get("pair"):
            continue
        pair = str(row["pair"])
        token = CB.issue("select_pair", {
            "pair": pair,
            "registry_hash": registry.get("registry_hash"),
        })
        label = ("✓ " if pair == active.get("pair") else "") + pair
        buttons.append([{"text": label, "callback_data": "select|" + token}])
    navigation = []
    if page > 0:
        token = CB.issue("pairs_page", {"page": page - 1})
        navigation.append({"text": "‹ Previous", "callback_data": "page|" + token})
    if page + 1 < pages:
        token = CB.issue("pairs_page", {"page": page + 1})
        navigation.append({"text": "Next ›", "callback_data": "page|" + token})
    if navigation:
        buttons.append(navigation)
    send(
        f"Eligible Binance BTC-base Spot pairs — page {page + 1}/{pages}. "
        "Selection never resumes entries automatically.",
        chat,
        buttons,
    )


def route(action, chat, message_id=None):
    if action == "home":
        edit_or_send("Bitcoin Spot bot owner controls", chat, message_id, menu())
    elif action == "menu_dashboard":
        edit_or_send("Dashboard - read-only bot and account status.", chat,
                     message_id, dashboard_menu())
    elif action == "menu_trading":
        edit_or_send("Trading and protection controls.", chat,
                     message_id, trading_menu())
    elif action == "menu_system":
        edit_or_send("System, validation and deployment controls.", chat,
                     message_id, system_menu())
    elif action == "menu_emergency":
        edit_or_send("Emergency controls - confirmations remain mandatory.", chat,
                     message_id, emergency_menu())
    elif action == "help":
        edit_or_send(help_text(), chat, message_id,
                     [[{"text": "Home", "callback_data": "do|home"}]])
    elif action == "entries_on":
        _ask_confirm(chat, "CONFIRM resume", "resume_entries", text=
                     "Resume Freqtrade signal generation and sidecar entries?")
    elif action == "entries_off":
        sidecar = sidecar_command("entries", {"enabled": False}, wait=True)
        freqtrade = ft_call("POST", "/pause")
        send(json.dumps({"sidecar": sidecar, "freqtrade": freqtrade}, indent=2), chat)
    elif action in {"mode_fixed", "mode_trailing", "mode_oco_trailing"}:
        mode = {"mode_fixed": "FIXED_OCO", "mode_trailing": "TRAILING_ONLY",
                "mode_oco_trailing": "OCO_TRAILING"}[action]
        _ask_confirm(chat, "CONFIRM mode", "set_mode", {"mode": mode},
                     f"Use {mode} for future entries?")
    elif action in {"status", "balance", "profit", "pair"}:
        result = sidecar_command(action, wait=True)
        send(json.dumps(result, indent=2)[:3900], chat)
    elif action == "pairs":
        _show_pairs(chat, 0)
    elif action == "swap_done":
        _ask_confirm(
            chat,
            "CONFIRM manual swap completed",
            "complete_pair_switch",
            text=(
                "Confirm only after any required Binance-side quote-asset swap "
                "is complete. The sidecar will recheck flat state, funding, "
                "filters and protection capability before applying the pair."
            ),
        )
    elif action == "verify_pair":
        send(
            json.dumps(
                sidecar_command("verify_pair_switch", wait=True),
                indent=2,
            )[:3900],
            chat,
        )
    elif action == "flow":
        send(json.dumps(_flow_summary(), indent=2)[:3900], chat)
    elif action == "last_signal":
        send(_latest_signal(), chat)
    elif action == "logs":
        send((_tail_audit(25) + "\n\n" + json.dumps(ft_call("GET", "/logs?limit=20"), indent=2))[-3900:], chat)
    elif action == "reconcile":
        send(json.dumps(sidecar_command("reconcile", wait=True), indent=2), chat)
    elif action == "restart_stream":
        _ask_confirm(chat, "CONFIRM restart", "restart_stream")
    elif action == "deploy":
        send(json.dumps(read_json(DEPLOYMENT_STATUS_FILE, {}), indent=2), chat)
    elif action == "audit":
        send(json.dumps(_self_audit(), indent=2)[:3900], chat)
    elif action == "settings":
        send(_settings(), chat)
    elif action == "backtest":
        send(_backtest_status(), chat)
    elif action == "emergency_help":
        send("Use /emergency <current BTC symbol>, for example /emergency BTCUSDT.", chat)
    elif action == "trailing_help":
        send("Use /tighttrail BTCUSDT 20. Binance filters clamp the final delta.", chat)
    elif action == "be_help":
        send("Use /breakeven BTCUSDT. The replacement stop includes configured fees/slippage.", chat)
    elif action == "profit_help":
        send("Use /lockprofit BTCUSDT 0.2 to request a stop above entry.", chat)
    else:
        send(help_text(), chat)


def _confirm_action(action, args, chat):
    if action == "resume_entries":
        ft = ft_call("POST", "/start")
        sidecar = (
            sidecar_command("entries", {"enabled": True}, wait=True)
            if ft.get("ok") is True
            else {"ok": False, "result": "Freqtrade start failed; sidecar remains paused"}
        )
        rollback = None
        if ft.get("ok") is True and sidecar.get("ok") is not True:
            rollback = ft_call("POST", "/pause")
        send(json.dumps({
            "sidecar": sidecar,
            "freqtrade": ft,
            "freqtrade_rollback": rollback,
        }, indent=2), chat)
    elif action == "restart_stream":
        send(json.dumps(sidecar_command("restart_stream", wait=True), indent=2), chat)
    elif action == "set_mode":
        send(json.dumps(sidecar_command("mode", args, wait=True), indent=2), chat)
    elif action == "switch_pair":
        sidecar = sidecar_command("switch_pair", args, wait=True)
        send(json.dumps({
            "sidecar": sidecar,
            "next_action": (
                "Complete any required quote-asset swap on Binance, then use "
                "/swapdone. No pair configuration has changed yet."
                if sidecar.get("ok") is True
                else "Pair selection was refused; inspect the sidecar result."
            ),
            "entries": "remain OFF",
        }, indent=2)[:3900], chat)
    elif action == "complete_pair_switch":
        sidecar = sidecar_command("complete_pair_switch", wait=True)
        reload_result = (
            ft_call("POST", "/reload_config")
            if sidecar.get("ok") is True
            else {"ok": False, "error": "sidecar refused manual-swap completion"}
        )
        verification = (
            sidecar_command("verify_pair_switch", wait=True)
            if reload_result.get("ok") is True
            else {"ok": False, "result": "Freqtrade reload was not confirmed"}
        )
        send(json.dumps({
            "sidecar": sidecar,
            "freqtrade_reload": reload_result,
            "pair_verification": verification,
            "next_action": (
                "If verification is still pending, wait for a fresh strategy "
                "heartbeat and use /verifypair. Resume separately with /start."
            ),
            "entries": "remain OFF until explicit resume",
        }, indent=2)[:3900], chat)
    elif action in {"convert", "break_even", "lock_profit", "tight_trailing",
                    "emergency_exit", "set_size", "set_max", "auto_protection"}:
        send(json.dumps(sidecar_command(action, args, wait=True), indent=2), chat)


def handle_message(message):
    chat = str(message.get("chat", {}).get("id", ""))
    user_id = message.get("from", {}).get("id")
    text = str(message.get("text", "")).strip()
    if not is_owner(user_id, chat):
        audit("telegram_unauthorized", severity="WARNING", details={"user_id": user_id})
        return
    parts = text.split(); cmd = parts[0].lower() if parts else ""
    if cmd in {"/menu", "/owner"}:
        send("Bitcoin Spot bot owner controls", chat, menu())
    elif cmd == "/start": route("entries_on", chat)
    elif cmd in {"/stop", "/pause"}: route("entries_off", chat)
    elif cmd in {"/status", "/balance", "/profit", "/pair"}: route(cmd[1:], chat)
    elif cmd == "/pairs":
        try:
            page = int(parts[1]) - 1 if len(parts) == 2 else 0
        except ValueError:
            send("Pair-menu page must be a positive integer.", chat); return
        _show_pairs(chat, page)
    elif cmd == "/flow": route("flow", chat)
    elif cmd == "/swapdone": route("swap_done", chat)
    elif cmd == "/verifypair": route("verify_pair", chat)
    elif cmd == "/logs": route("logs", chat)
    elif cmd == "/deploy": route("deploy", chat)
    elif cmd == "/audit": route("audit", chat)
    elif cmd == "/lastsignal": route("last_signal", chat)
    elif cmd == "/backtest": route("backtest", chat)
    elif cmd == "/settings": route("settings", chat)
    elif cmd == "/reconcile": route("reconcile", chat)
    elif cmd == "/restartws": route("restart_stream", chat)
    elif cmd == "/fixed_oco": route("mode_fixed", chat)
    elif cmd == "/trailing_only": route("mode_trailing", chat)
    elif cmd == "/oco_trailing": route("mode_oco_trailing", chat)
    elif cmd == "/switchpair" and len(parts) == 2:
        pair, why = normalize_pair_input(parts[1])
        if not pair:
            send("Pair rejected: " + why, chat); return
        _ask_confirm(chat, "CONFIRM pair switch", "switch_pair", {"pair": pair},
                     f"Switch active Spot pair to {pair}? Requires verified flat account; entries stay off.")
    elif cmd == "/convert" and len(parts) == 3:
        mode = parts[2].upper()
        if mode not in {"FIXED_OCO", "TRAILING_ONLY", "OCO_TRAILING"}:
            send("Invalid protection mode.", chat); return
        _ask_confirm(chat, "CONFIRM conversion", "convert",
                     {"symbol": parts[1].upper(), "mode": mode})
    elif cmd == "/breakeven" and len(parts) == 2:
        _ask_confirm(chat, "CONFIRM break-even", "break_even",
                     {"symbol": parts[1].upper()})
    elif cmd == "/lockprofit" and len(parts) in {2, 3}:
        try: pct = float(parts[2]) if len(parts) == 3 else 0.2
        except ValueError: send("Invalid profit percentage.", chat); return
        if not math.isfinite(pct) or not 0 < pct <= 100:
            send("Profit percentage must be within (0,100].", chat); return
        _ask_confirm(chat, "CONFIRM profit lock", "lock_profit",
                     {"symbol": parts[1].upper(), "profit_pct": pct})
    elif cmd == "/tighttrail" and len(parts) in {2, 3}:
        try: bips = int(parts[2]) if len(parts) == 3 else 20
        except ValueError: send("Invalid trailing delta.", chat); return
        _ask_confirm(chat, "CONFIRM tight trail", "tight_trailing",
                     {"symbol": parts[1].upper(), "delta_bips": bips})
    elif cmd == "/autoprotection" and len(parts) == 2 and parts[1].lower() in {"on", "off"}:
        _ask_confirm(chat, "CONFIRM automatic protection", "auto_protection",
                     {"enabled": parts[1].lower() == "on"})
    elif cmd == "/emergency" and len(parts) == 2:
        _ask_confirm(chat, "CONFIRM emergency exit", "emergency_exit",
                     {"symbol": parts[1].upper()})
    elif cmd == "/setsize" and len(parts) == 2:
        try: value = float(parts[1])
        except ValueError: send("Invalid quote amount.", chat); return
        if not math.isfinite(value) or value <= 0:
            send("Quote amount must be positive and finite.", chat); return
        _ask_confirm(chat, "CONFIRM trade size", "set_size", {"quote_amount": value})
    elif cmd == "/setmax" and len(parts) == 2:
        try: value = int(parts[1])
        except ValueError: send("Invalid position count.", chat); return
        _ask_confirm(chat, "CONFIRM max positions", "set_max", {"count": value})
    else:
        send(help_text(), chat)


def handle_callback(callback):
    user_id = callback.get("from", {}).get("id")
    message = callback.get("message", {})
    chat = str(message.get("chat", {}).get("id", ""))
    raw_message_id = message.get("message_id")
    message_id = (
        raw_message_id
        if isinstance(raw_message_id, int)
        and not isinstance(raw_message_id, bool)
        and raw_message_id > 0
        else None
    )
    data = str(callback.get("data", ""))
    try:
        requests.post(BASE + "/answerCallbackQuery",
                      data={"callback_query_id": callback.get("id")}, timeout=10)
    except Exception:
        pass
    if not is_owner(user_id, chat):
        audit("telegram_callback_unauthorized", severity="WARNING", details={"user_id": user_id})
        return
    if data.startswith("do|"):
        route(data.split("|", 1)[1], chat, message_id); return
    if data.startswith(("select|", "page|")):
        item, reason = CB.consume(data.split("|", 1)[1])
        if not item:
            send("Pair action rejected: " + reason, chat); return
        if item["action"] == "pairs_page":
            _show_pairs(chat, int(item["args"].get("page", 0))); return
        if item["action"] == "select_pair":
            pair, why = normalize_pair_input(item["args"].get("pair", ""))
            if not pair:
                send("Pair rejected: " + why, chat); return
            _ask_confirm(
                chat,
                "CONFIRM pair switch",
                "switch_pair",
                {"pair": pair, "registry_hash": item["args"].get("registry_hash")},
                f"Switch active Spot pair to {pair}? The sidecar will refresh "
                "Binance metadata, verify clean durable/exchange state and "
                "disarm entries. It will then wait for /swapdone; it will not "
                "change the pair or resume entries automatically.",
            )
            return
        send("Pair action rejected: unexpected callback action.", chat)
        return
    if data.startswith("cancel|"):
        canceled, reason = CB.cancel(data.split("|", 1)[1])
        send("Action canceled." if canceled else "Cancellation rejected: " + reason, chat)
        return
    if data.startswith("confirm|"):
        item, reason = CB.consume(data.split("|", 1)[1])
        if not item:
            send("Confirmation rejected: " + reason, chat); return
        _confirm_action(item["action"], item["args"], chat)


def _poll_backoff(failures: int, *, jitter=random.uniform) -> float:
    """Return bounded exponential backoff with a small randomised delay."""
    exponent = max(0, min(int(failures) - 1, 5))
    base = min(3.0 * (2**exponent), 60.0)
    return base + float(jitter(0.0, min(1.0, base * 0.25)))


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if not TOKEN or not OWNER:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_CHAT_ID required")
    envelope.load_key(envelope.BUS_COMMAND)
    update_store = TelegramUpdateStore(
        UPDATE_DB_PATH,
        legacy_offset_path=OFFSET_PATH,
        retention=UPDATE_RETENTION,
    )
    offset = update_store.offset()
    failures = 0
    if update_store.recovered_uncertain:
        log.warning(
            "Recovered %d durably claimed Telegram update(s) as uncertain; "
            "they will not be replayed",
            update_store.recovered_uncertain,
        )
    try:
        while True:
            try:
                response = requests.get(BASE + "/getUpdates", params={
                    "offset": offset, "timeout": 25,
                    "allowed_updates": json.dumps(
                        ["message", "callback_query"])}, timeout=35).json()
                if not response.get("ok"):
                    raise RuntimeError("Telegram getUpdates returned an error")
                previous = offset
                for update in response.get("result", []):
                    update_id = int(update["update_id"])
                    if not update_store.claim(update_id):
                        audit(
                            "telegram_update_replay_rejected",
                            actor="telegram-broker",
                            details={"update_id": update_id},
                        )
                        continue
                    try:
                        if "message" in update:
                            handle_message(update["message"])
                        if "callback_query" in update:
                            handle_callback(update["callback_query"])
                    except Exception as exc:
                        update_store.complete(update_id, "failed", _safe_error(exc))
                        raise
                    else:
                        update_store.complete(update_id, "handled")
                    offset = update_store.offset()
                offset = update_store.offset()
                if offset != previous:
                    atomic_write_json(
                        OFFSET_PATH, {"offset": offset, "ts": time.time()})
                failures = 0
                atomic_write_json(RUNTIME / "telegram_health.json", {
                    "ok": True,
                    "ts": time.time(),
                    "offset": offset,
                    "uncertain_updates": update_store.uncertain_count(),
                })
            except Exception as exc:
                failures += 1
                delay = _poll_backoff(failures)
                atomic_write_json(RUNTIME / "telegram_health.json", {
                    "ok": False,
                    "ts": time.time(),
                    "offset": update_store.offset(),
                    "uncertain_updates": update_store.uncertain_count(),
                    "retry_seconds": delay,
                    "error": _safe_error(exc),
                })
                log.warning(
                    "Telegram poll failed; retrying in %.2fs: %s",
                    delay,
                    _safe_error(exc),
                )
                time.sleep(delay)
    finally:
        update_store.close()


if __name__ == "__main__":
    main()
