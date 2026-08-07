from __future__ import annotations
"""Execution-sidecar supervisor and signed owner-command consumer."""

import json
import hashlib
from datetime import datetime, timezone
import logging
import math
import os
from pathlib import Path
import re
import signal
import threading
import time

from services.common import envelope
from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.binance_public import (
    BinancePublicClient,
    DEFAULT_BASE,
    SPOT_TESTNET_BASE,
)
from services.common.config_bounds import ConfigError
from services.common.config_bounds import env_int
from services.common.evidence_signature import EvidenceSignatureError, verify_document
from services.common.market_policy import allowed_quotes_from_env, canonical_pair
from services.common.models import ExecutionMode, ProtectionMode
from services.common.paths import (
    ACTIVE_PAIR_FILE, COMMAND_INBOX, COMMAND_RESULTS_DIR, ELIGIBLE_PAIRS_FILE,
    FREQTRADE_ACTIVE_CONFIG, FREQTRADE_HEARTBEAT_FILE, MONEYFLOW_FILE,
    PAIRLIST_FILE, RUNTIME,
    SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED,
)
from services.common.pair_registry import PairRegistry
from services.common.redaction import redact_text
from services.common.retention import prune_files
from services.execution_sidecar.bitcoin_adapter import BitcoinSpotAdapter
from services.execution_sidecar.live_evidence import (
    EVIDENCE_PRODUCER, LiveEvidenceError, evidence_path, verify_live_evidence,
)
from services.execution_sidecar.order_manager import OrderManager
from services.execution_sidecar.package_mode import enforce_package_mode
from services.execution_sidecar.pair_control import PairController
from services.execution_sidecar.risk_checks import FreshSignalGuard
from services.execution_sidecar.simulation_adapter import SimulationAdapter
from services.execution_sidecar.state_store import StateStore


log = logging.getLogger("sidecar")
STOP = threading.Event()
RELEASE_HASH_FILE = Path(__file__).resolve().parents[2] / "RELEASE_SHA256.txt"
MAX_LIVE_EVIDENCE_BYTES = 256 * 1024


def notify(text, buttons=None, chat_id=None):
    audit("sidecar_notification", details={
        "text": str(text)[:1000], "buttons": buttons, "chat_id": chat_id})


def _safe_command_id(value) -> str | None:
    value = str(value or "")
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


def _active_symbol(value, pair_controller: PairController) -> str:
    symbol = str(value or "").upper()
    active = pair_controller.load()
    if symbol != active["symbol"]:
        raise ValueError(f"symbol must be the active pair {active['symbol']}")
    return symbol


def _disarm(adapter, state: StateStore, reason: str) -> str:
    errors = []
    try:
        state.set_entries(False, reason)
    except Exception as exc:
        errors.append("state persistence failed: " + str(exc))
    try:
        result = adapter.set_enabled(False)
    except Exception as exc:
        result = "OFF"
        errors.append("adapter disarm failed: " + str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def freqtrade_pair_ready(
    pair_controller: PairController,
    *,
    now: float | None = None,
) -> tuple[bool, dict]:
    """Prove Freqtrade loaded the same one-pair configuration as the sidecar."""
    current = time.time() if now is None else float(now)
    active = pair_controller.load()
    heartbeat = read_json(FREQTRADE_HEARTBEAT_FILE, None)
    detail = {
        "required": True,
        "path": str(FREQTRADE_HEARTBEAT_FILE),
        "active_pair": active["pair"],
        "pair_generation": active["generation"],
        "pair_config_hash": active["pair_config_hash"],
    }
    if not isinstance(heartbeat, dict):
        detail["reason"] = "Freqtrade signal-seam heartbeat is missing"
        return False, detail
    try:
        timestamp = datetime.fromisoformat(
            str(heartbeat.get("ts", "")).replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = current - timestamp.timestamp()
    except (TypeError, ValueError, OverflowError):
        detail["reason"] = "Freqtrade heartbeat timestamp is malformed"
        return False, detail
    checks = {
        "heartbeat_ok": heartbeat.get("ok") is True,
        "fresh": -30 <= age <= 120,
        "one_whitelisted_pair": heartbeat.get("whitelist") == 1,
        "one_active_pair": heartbeat.get("active_pairs") == 1,
        "pair_match": heartbeat.get("pair") == active["pair"],
        "generation_match": heartbeat.get("pair_generation") == active["generation"],
        "config_hash_match": (
            heartbeat.get("pair_config_hash") == active["pair_config_hash"]
        ),
    }
    detail.update({"age_seconds": round(age, 3), "checks": checks})
    ready = all(checks.values())
    if not ready:
        detail["reason"] = "Freqtrade has not proved the active pair configuration"
    return ready, detail


def _write_command_result(cid, cmd, ok, result, state):
    payload = {"ok": bool(ok), "result": redact_text(result),
               "command_id": cid, "command": cmd}
    state.record_command(cid, cmd, json.dumps(payload, sort_keys=True))
    atomic_write_json(COMMAND_RESULTS_DIR / f"command_result_{cid}.json", payload)
    audit("command_result", severity="INFO" if ok else "ERROR", details=payload)
    return payload


def process_command(adapter, state: StateStore, guard: FreshSignalGuard,
                    pair_controller: PairController, path: Path):
    try:
        if path.stat().st_size > 65_536:
            raise ValueError("command file exceeds 65536 bytes")
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = envelope.verify_envelope(
            raw, purpose=envelope.BUS_COMMAND,
            expected_producers={"telegram-broker", "deploy-installer"})
        cmd, args = str(data["command"]), data.get("args", {})
        cid = _safe_command_id(data.get("command_id", path.stem))
        if not cid or not isinstance(args, dict):
            raise ValueError("invalid command_id or args")
    except Exception as exc:
        audit("command_rejected", severity="CRITICAL", details={
            "file": path.name, "error": str(exc)})
        path.unlink(missing_ok=True)
        return
    try:
        age = time.time() - float(data["created_at"])
        maximum = int(os.getenv("COMMAND_MAX_AGE_SECONDS", "120"))
        if age > maximum or age < -30:
            raise ValueError(f"command age {age:.1f}s is outside the allowed window")
    except Exception as exc:
        if state.claim_command(cid, cmd):
            _write_command_result(cid, cmd, False, "expired or invalid command: " + str(exc), state)
        path.unlink(missing_ok=True)
        return
    if not state.claim_command(cid, cmd):
        prior = state.command_result(cid) or "UNKNOWN"
        result_path = COMMAND_RESULTS_DIR / f"command_result_{cid}.json"
        if prior == "IN_PROGRESS" and not result_path.exists():
            atomic_write_json(result_path, {"ok": False,
                "result": "prior command outcome is uncertain; reconcile before retrying",
                "command_id": cid, "command": cmd})
            state.set_entries(False, "uncertain-prior-command-reconcile-required")
        elif prior not in {"UNKNOWN", "IN_PROGRESS"} and not result_path.exists():
            try:
                atomic_write_json(result_path, json.loads(prior))
            except Exception:
                atomic_write_json(result_path, {"ok": False, "result": prior,
                                                "command_id": cid, "command": cmd})
        path.unlink(missing_ok=True)
        return

    ok, result = True, "unknown command"
    try:
        if cmd == "entries":
            enabled = args.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("entries.enabled must be a JSON boolean")
            if enabled:
                pair_controller.load()
                pair_controller.require_resume_ready()
                pair_ready, pair_detail = freqtrade_pair_ready(pair_controller)
                if not pair_ready:
                    raise ValueError(
                        "Freqtrade active-pair proof failed: "
                        + json.dumps(pair_detail, sort_keys=True)
                    )
                if guard.state.get("global_pause"):
                    raise ValueError("risk pause must be reconciled/cleared first: " +
                                     str(guard.state["global_pause"]))
                result = adapter.set_enabled(True)
                if result != "ON":
                    raise RuntimeError(str(result))
                state.set_entries(True)
                try:
                    pair_controller.mark_active_after_resume()
                except Exception:
                    _disarm(
                        adapter,
                        state,
                        "pair-switch-active-state-persistence-failed",
                    )
                    raise
            else:
                result = _disarm(adapter, state, "operator-paused")
        elif cmd == "mode":
            if getattr(adapter, "mode", "simulation") == "live":
                raise ValueError(
                    "live entry protection mode is evidence-bound; change PROTECTION_MODE, "
                    "produce fresh signed evidence, and redeploy"
                )
            state.set_mode(args["mode"]); result = "mode " + state.get_mode()
        elif cmd == "status":
            result = adapter.status()
        elif cmd == "balance":
            result = json.dumps(adapter.balance(), sort_keys=True, default=str)
        elif cmd == "profit":
            result = json.dumps(adapter.profit(), sort_keys=True, default=str)
        elif cmd == "restart_stream":
            result = adapter.restart_user_stream()
        elif cmd == "set_size":
            value = float(args.get("quote_amount", args.get("usdt", 0)))
            if not math.isfinite(value) or value <= 0:
                raise ValueError("trade size must be a positive finite number")
            result = adapter.set_size(value)
            ok = str(result).lower().startswith("trade size =")
        elif cmd == "set_max":
            result = adapter.set_max(int(args["count"]))
            ok = str(result).lower().startswith("max concurrent positions =")
        elif cmd == "reconcile":
            report = adapter.verified_reconcile()
            ok, result = bool(report.get("ok")), json.dumps(report, sort_keys=True, default=str)
        elif cmd == "pair":
            result = json.dumps({
                "active": pair_controller.load(),
                "switch": pair_controller.switch_status(),
            }, sort_keys=True)
        elif cmd == "pairs":
            result = json.dumps(
                pair_controller.eligible_pairs(force=True),
                sort_keys=True,
            )
        elif cmd == "switch_pair":
            _disarm(adapter, state, "pair-switch-owner-resume-required")
            target = canonical_pair(str(args.get("pair", "")))
            adapter.validate_pair(target)
            switch = pair_controller.begin_switch(
                target,
                adapter.verify_flat_for_switch,
                expected_registry_hash=str(args.get("registry_hash", "")),
            )
            result = json.dumps(switch, sort_keys=True, default=str)
        elif cmd == "complete_pair_switch":
            _disarm(adapter, state, "pair-switch-owner-resume-required")
            switch = pair_controller.complete_switch(
                adapter.verify_flat_for_switch,
                adapter.validate_pair_funding,
            )
            new_state = switch["state"]
            state.register_symbol_pair(new_state["symbol"], new_state["pair"])
            if getattr(adapter, "mode", "simulation") == "live":
                # The promotion evidence is bound to an exact pair-state hash.
                # A live pair change therefore cannot be armed in this process.
                state.data["live_restart_required"] = True
                state.save()
                guard.set_global_pause(
                    "live-pair-change-requires-fresh-evidence-and-restart")
            result = json.dumps(switch, sort_keys=True, default=str)
        elif cmd == "verify_pair_switch":
            pair_ready, pair_detail = freqtrade_pair_ready(pair_controller)
            if not pair_ready:
                ok = False
                result = json.dumps({
                    "ok": False,
                    "switch": pair_controller.switch_status(),
                    "freqtrade": pair_detail,
                }, sort_keys=True, default=str)
            else:
                verified = pair_controller.mark_pair_verified({
                    "ok": True,
                    **pair_detail,
                })
                result = json.dumps({
                    "ok": True,
                    "switch": verified,
                    "freqtrade": pair_detail,
                }, sort_keys=True, default=str)
        elif cmd == "emergency_exit":
            _disarm(adapter, state, "emergency-exit-owner-resume-required")
            outcome = adapter.emergency_exit(_active_symbol(args.get("symbol"), pair_controller))
            ok, result = outcome.get("ok") is True, json.dumps(outcome, sort_keys=True, default=str)
        elif cmd == "convert":
            _disarm(adapter, state, "protection-conversion-owner-resume-required")
            ok, result = adapter.convert(
                _active_symbol(args.get("symbol"), pair_controller),
                ProtectionMode(args["mode"]))
        elif cmd == "break_even":
            _disarm(adapter, state, "protection-conversion-owner-resume-required")
            ok, result = adapter.convert(
                _active_symbol(args.get("symbol"), pair_controller),
                ProtectionMode.FIXED_OCO, break_even=True)
        elif cmd == "lock_profit":
            pct = float(args.get("profit_pct", 0.2))
            if not math.isfinite(pct) or not 0 < pct <= 100:
                raise ValueError("profit percentage must be within (0,100]")
            _disarm(adapter, state, "protection-conversion-owner-resume-required")
            ok, result = adapter.convert(
                _active_symbol(args.get("symbol"), pair_controller),
                ProtectionMode.FIXED_OCO, lock_profit_pct=pct)
        elif cmd == "tight_trailing":
            delta = int(args.get("delta_bips", 20))
            _disarm(adapter, state, "protection-conversion-owner-resume-required")
            ok, result = adapter.convert(
                _active_symbol(args.get("symbol"), pair_controller),
                ProtectionMode.TRAILING_ONLY, trailing_delta_bips=delta)
        elif cmd == "auto_protection":
            enabled = args.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("auto_protection.enabled must be a JSON boolean")
            if getattr(adapter, "mode", "simulation") == "live":
                raise ValueError(
                    "live automatic protection policy is evidence-bound; change the private "
                    "environment, produce fresh signed evidence, and redeploy"
                )
            state.data["auto_protection_enabled"] = enabled
            state.save()
            result = "automatic protection " + ("enabled" if enabled else "disabled")
        elif cmd == "clear_risk_pause":
            guard.clear_global_pause(); result = "risk pause cleared; entries remain off"
        else:
            ok = False
    except Exception as exc:
        ok, result = False, str(exc)
    _write_command_result(cid, cmd, ok, result, state)
    path.unlink(missing_ok=True)


def _capture_live_evidence_lease(verified_payload: dict) -> dict:
    target = evidence_path()
    if target.is_symlink() or not target.is_file():
        raise LiveEvidenceError("live evidence lease source is missing or a symlink")
    raw = target.read_bytes()
    if len(raw) > MAX_LIVE_EVIDENCE_BYTES:
        raise LiveEvidenceError("live evidence lease source exceeds 256 KiB")
    try:
        document = json.loads(raw)
        direct_payload = verify_document(document, expected_producer=EVIDENCE_PRODUCER)
        expires_at = float(document["expires_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError,
            EvidenceSignatureError) as exc:
        raise LiveEvidenceError(f"live evidence lease rejected: {exc}") from exc
    if not math.isfinite(expires_at) or direct_payload != verified_payload:
        raise LiveEvidenceError("live evidence changed during full verification")
    return {
        "path": str(target),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "expires_at": expires_at,
    }


def enforce_live_evidence_lease(adapter, state: StateStore, lease: dict, *,
                                now: float | None = None,
                                verify_signature: bool = False,
                                minimum_remaining_seconds: int = 0) -> tuple[bool, str]:
    current = float(time.time() if now is None else now)
    status = "valid"
    try:
        if state.data.get("live_evidence_failure_latched", False):
            raise LiveEvidenceError("live evidence failure is latched until restart")
        expires_at = float(lease["expires_at"])
        if not math.isfinite(current) or not math.isfinite(expires_at) or current >= expires_at:
            raise LiveEvidenceError("live evidence lease expired")
        if verify_signature:
            target = Path(str(lease["path"]))
            if target.is_symlink() or not target.is_file():
                raise LiveEvidenceError("live evidence lease source is missing or a symlink")
            raw = target.read_bytes()
            if (len(raw) > MAX_LIVE_EVIDENCE_BYTES
                    or hashlib.sha256(raw).hexdigest() != lease.get("sha256")):
                raise LiveEvidenceError("live evidence bytes changed; restart recertification required")
            document = json.loads(raw)
            verify_document(
                document,
                expected_producer=EVIDENCE_PRODUCER,
                now=current,
                min_remaining_seconds=minimum_remaining_seconds,
            )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError,
            EvidenceSignatureError, LiveEvidenceError) as exc:
        status = str(exc)
        first_failure = not state.data.get("live_evidence_failure_latched", False)
        try:
            _disarm(adapter, state, "live-evidence-invalid-restart-required")
        except Exception as disarm_exc:
            status += "; disarm error: " + str(disarm_exc)
        state.data["live_evidence_ok"] = False
        state.data["live_evidence_status"] = status[:500]
        state.data["live_evidence_failure_latched"] = True
        state.data["live_restart_required"] = True
        state.save()
        if first_failure:
            audit("live_evidence_lease_failed", severity="CRITICAL", details={"error": status})
        return False, status
    state.data["live_evidence_ok"] = True
    state.data["live_evidence_status"] = status
    return True, status


def live_interlock(state: StateStore, pair_controller: PairController) -> ExecutionMode:
    try:
        mode = ExecutionMode(os.getenv("EXECUTION_MODE", "simulation").lower())
    except ValueError as exc:
        raise SystemExit("Invalid EXECUTION_MODE; use simulation, testnet, or live") from exc
    package_mode = enforce_package_mode(mode.value)
    state.data["package_mode"] = package_mode
    state.data["simulation"] = mode is ExecutionMode.SIMULATION
    state.set_entries(False, f"{mode.value}-startup-owner-resume-required")
    if mode is not ExecutionMode.LIVE:
        return mode
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true":
        raise SystemExit("LIVE BLOCKED: LIVE_TRADING_ENABLED must be explicitly true")
    if os.getenv("AUTO_CONFIRM", "false").lower() == "true":
        raise SystemExit("LIVE BLOCKED: AUTO_CONFIRM must remain false")
    try:
        state.set_mode(ProtectionMode(os.getenv("PROTECTION_MODE", "OCO_TRAILING").upper()))
        auto_enabled = os.getenv("AUTO_PROTECTION_ENABLED", "false").strip().lower()
        if auto_enabled not in {"true", "false"}:
            raise ValueError("AUTO_PROTECTION_ENABLED must be true or false")
        state.data["auto_protection_enabled"] = auto_enabled == "true"
        state.save()
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"LIVE BLOCKED: evidence-bound runtime policy is invalid: {exc}") from exc
    try:
        installed = RELEASE_HASH_FILE.read_text(encoding="utf-8").split()[0]
    except Exception as exc:
        raise SystemExit("LIVE BLOCKED: installed release hash is unreadable") from exc
    expected = os.getenv("SIDECAR_RELEASE_HASH", "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", installed) or expected != installed:
        raise SystemExit("LIVE BLOCKED: SIDECAR_RELEASE_HASH must match the installed artifact")
    try:
        from services.common.strategy_fingerprint import fingerprints
        strategy = Path(__file__).resolve().parents[2] / "freqtrade/user_data/strategies/IctSmcStrategy.py"
        prints = fingerprints(strategy, "IctSmcStrategy", [
            "populate_indicators_5m", "populate_indicators",
            "populate_entry_trend", "populate_exit_trend"])
        verified_payload = verify_live_evidence(
            release_hash=installed, strategy_fingerprints=prints,
            strategy_file_sha256=hashlib.sha256(strategy.read_bytes()).hexdigest(),
            active_pair_state=pair_controller.load(),
            allowed_quotes=allowed_quotes_from_env())
        state.data["live_evidence_lease"] = _capture_live_evidence_lease(verified_payload)
        state.data["live_evidence_ok"] = True
        state.data["live_evidence_status"] = "valid"
        state.data["live_evidence_failure_latched"] = False
        state.save()
    except (LiveEvidenceError, envelope.EnvelopeError) as exc:
        raise SystemExit(f"LIVE BLOCKED: {exc}") from exc
    except Exception as exc:
        raise SystemExit(f"LIVE BLOCKED: evidence verification failed: {exc}") from exc
    return mode


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        for purpose in (envelope.BUS_COMMAND, envelope.BUS_SIGNAL):
            envelope.load_key(purpose)
    except envelope.EnvelopeError as exc:
        raise SystemExit(f"BUS KEYS MISSING: {exc}") from exc
    if not envelope.installed_release_hash():
        raise SystemExit("RELEASE BINDING MISSING")
    execution_hint = os.getenv("EXECUTION_MODE", "simulation").strip().lower()
    pair_public_base = os.getenv("BINANCE_SPOT_EXECUTION_PUBLIC_BASE", "").strip()
    if not pair_public_base:
        pair_public_base = (
            SPOT_TESTNET_BASE if execution_hint == "testnet" else DEFAULT_BASE
        )
    registry = PairRegistry(
        ELIGIBLE_PAIRS_FILE,
        public_client=BinancePublicClient(base=pair_public_base),
        ttl_seconds=env_int("BTC_PAIR_REGISTRY_TTL_SECONDS", 300, 60, 3600),
    )
    pair_controller = PairController(
        ACTIVE_PAIR_FILE,
        PAIRLIST_FILE,
        FREQTRADE_ACTIVE_CONFIG,
        registry=registry,
    )
    pair_state = pair_controller.bootstrap(os.getenv("ACTIVE_PAIR", "BTC/USDT"))
    state = StateStore(RUNTIME / "sidecar_state.json", RUNTIME / "execution_state.sqlite")
    state.register_symbol_pair(pair_state["symbol"], pair_state["pair"])
    mode = live_interlock(state, pair_controller)
    if mode is ExecutionMode.LIVE:
        # Reaching this point proves that the new process verified evidence for
        # the currently installed release and active pair generation.
        state.data["live_restart_required"] = False
        state.save()
    try:
        guard = FreshSignalGuard(RUNTIME / "fresh_signal_guard.json", state_store=state)
    except ConfigError as exc:
        raise SystemExit(f"RISK CONFIGURATION INVALID: {exc}") from exc
    if mode is ExecutionMode.SIMULATION:
        adapter = SimulationAdapter(state, guard, notify,
            fault_path=RUNTIME / "simulation_faults.json")
        adapter.start()
        simulation_reconcile = adapter.verified_reconcile()
        if not simulation_reconcile["ok"]:
            raise RuntimeError(simulation_reconcile["detail"])
    else:
        adapter = BitcoinSpotAdapter(
            state, guard, pair_controller, notify, execution_mode=mode.value)
        adapter.start()
    _disarm(adapter, state, "startup-reconciliation-complete-owner-resume-required")
    manager = OrderManager(
        adapter, state, guard, state, pair_controller, MONEYFLOW_FILE,
        {"processed": SIGNAL_PROCESSED, "rejected": SIGNAL_REJECTED})
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    audit("sidecar_started", details={
        "mode": mode.value, "pair": pair_state["pair"],
        "protection_mode": state.get_mode(), "entries": "off"})
    next_maintenance = next_backup = 0.0
    live_evidence_ok = mode is not ExecutionMode.LIVE
    live_evidence_status = "not-required" if live_evidence_ok else "valid"
    live_recheck_seconds = env_int("LIVE_EVIDENCE_RECHECK_SECONDS", 30, 5, 300)
    next_live_evidence_check = 0.0
    live_lease = state.data.get("live_evidence_lease", {})
    while not STOP.is_set():
        if mode is ExecutionMode.LIVE:
            now = time.time()
            due = now >= next_live_evidence_check
            live_evidence_ok, live_evidence_status = enforce_live_evidence_lease(
                adapter, state, live_lease, now=now, verify_signature=due,
                minimum_remaining_seconds=live_recheck_seconds * 2 if due else 0,
            )
            if due:
                next_live_evidence_check = now + live_recheck_seconds
        for path in sorted(COMMAND_INBOX.glob("*.json")):
            try:
                process_command(adapter, state, guard, pair_controller, path)
            except Exception as exc:
                log.exception("command processing failed for %s", path.name)
                _disarm(adapter, state, "command-processing-failure")
        if mode is ExecutionMode.LIVE:
            live_evidence_ok, live_evidence_status = enforce_live_evidence_lease(
                adapter, state, live_lease, now=time.time())
        for path in sorted(SIGNAL_INBOX.glob("*.json")):
            try:
                manager.process_signal(path)
            except Exception as exc:
                log.exception("signal processing failed for %s", path.name)
                _disarm(adapter, state, "signal-processing-failure")
                try:
                    guard.set_global_pause("signal-processing-failure-reconcile-required")
                except Exception:
                    pass
        try:
            adapter.tick()
            if state.data.get("auto_protection_enabled", False):
                adapter.maybe_auto_manage(read_json(MONEYFLOW_FILE, {}) or {})
        except Exception as exc:
            audit("adapter_tick_failed", severity="CRITICAL", details={"error": str(exc)})
            _disarm(adapter, state, "adapter-tick-failed")
        if time.time() >= next_maintenance:
            prune_files(COMMAND_RESULTS_DIR, "command_result_*.json",
                        max_files=int(os.getenv("COMMAND_RESULT_MAX_FILES", "1000")),
                        max_age_seconds=int(os.getenv("COMMAND_RESULT_MAX_AGE_SECONDS", "86400")))
            try:
                interval = int(os.getenv("SQLITE_BACKUP_INTERVAL_SECONDS", "86400"))
                if interval > 0 and time.time() >= next_backup:
                    destination = state.backup(
                        RUNTIME / "db_backups", retain=int(os.getenv("SQLITE_BACKUP_RETAIN", "14")))
                    audit("sqlite_backup_verified", details={"path": str(destination)})
                    next_backup = time.time() + interval
            except Exception as exc:
                audit("sqlite_backup_failed", severity="ERROR", details={"error": str(exc)})
                next_backup = time.time() + 3600
            next_maintenance = time.time() + 60
        active = pair_controller.load()
        freqtrade_ready, freqtrade_detail = freqtrade_pair_ready(pair_controller)
        if state.entries() and not freqtrade_ready:
            try:
                _disarm(adapter, state, "freqtrade-pair-proof-lost")
            except Exception:
                pass
        flow = read_json(MONEYFLOW_FILE, {}) or {}
        unresolved_count = len(state.unresolved_intents())
        reconciliation_status = state.data.get("last_reconciliation_status", "NOT_RUN")
        reconciliation_ok = str(reconciliation_status).startswith("RECONCILED")
        stream = read_json(RUNTIME / "user_stream_health.json", {}) or {}
        if mode is ExecutionMode.SIMULATION:
            user_stream_ok = True
        else:
            try:
                user_stream_ok = bool(
                    stream.get("ok") is True
                    and time.time() - float(stream.get("ts", 0)) < 60
                )
            except (TypeError, ValueError):
                user_stream_ok = False
        readiness_ok = bool(
            live_evidence_ok and reconciliation_ok
            and unresolved_count == 0 and user_stream_ok and freqtrade_ready
        )
        pair_switch = pair_controller.switch_status()
        atomic_write_json(RUNTIME / "sidecar_health.json", {
            "ok": readiness_ok, "ts": time.time(), "execution_mode": mode.value,
            "entries_enabled": state.entries(), "pause_reason": state.data.get("pause_reason", ""),
            "simulation": state.data.get("simulation", True),
            "protection_mode": state.get_mode(), "active_pair": active["pair"],
            "pair_state_hash": active["state_hash"],
            "pair_generation": active["generation"],
            "pair_config_hash": active["pair_config_hash"],
            "pair_switch_stage": pair_switch["stage"],
            "pair_switch_target": pair_switch.get("target_pair"),
            "freqtrade_pair_ready": freqtrade_ready,
            "freqtrade_pair_detail": freqtrade_detail,
            "moneyflow_ok": bool(flow.get("ok")),
            "matching_futures_available": bool((flow.get("futures") or {}).get("available")),
            "last_reconciliation_status": reconciliation_status,
            "reconciliation_ok": reconciliation_ok,
            "user_stream_ok": user_stream_ok,
            "unresolved_intents": unresolved_count,
            "live_evidence_ok": live_evidence_ok,
            "live_evidence_status": live_evidence_status,
            "live_evidence_expires_at": live_lease.get("expires_at") if mode is ExecutionMode.LIVE else None,
        })
        STOP.wait(1)
    try:
        if getattr(adapter, "stream", None):
            adapter.stream.stop()
    finally:
        audit("sidecar_stopped")


if __name__ == "__main__":
    main()
