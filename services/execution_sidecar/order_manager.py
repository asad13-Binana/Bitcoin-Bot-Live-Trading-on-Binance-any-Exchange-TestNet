from __future__ import annotations
"""Fail-closed consumer of Freqtrade's signed BTC signal bus."""

import json
import math
import os
from pathlib import Path
import re
import shutil
import time

from services.common import envelope
from services.common.atomic import read_json
from services.common.audit import audit
from services.common.config_bounds import env_int
from services.common.market_policy import canonical_pair
from services.common.retention import prune_files


SIGNAL_PRODUCER = "freqtrade-strategy"


class OrderManager:
    def __init__(self, adapter, state, guard, state_store, pair_controller,
                 moneyflow_path, paths):
        self.adapter = adapter
        self.state = state
        self.guard = guard
        self.store = state_store
        self.pair_controller = pair_controller
        self.moneyflow_path = Path(moneyflow_path)
        self.paths = paths
        self.max_flow_age = env_int("MAX_FLOW_AGE_SECONDS", 45, 5, 300)
        self.require_flow = os.getenv("REQUIRE_FLOW_CONTEXT", "false").lower() == "true"
        self.require_matching_futures = (
            os.getenv("REQUIRE_MATCHING_FUTURES", "false").lower() == "true")

    def _flow_gate(self, pair_state: dict) -> tuple[bool, str]:
        flow = read_json(self.moneyflow_path, None)
        if not isinstance(flow, dict):
            return False, "money-flow snapshot missing"
        if flow.get("pair") != pair_state["pair"] or flow.get("pair_state_hash") != pair_state["state_hash"]:
            return False, "money-flow snapshot belongs to another pair generation"
        try:
            generated = float(flow.get("generated_at_epoch", 0))
            if not math.isfinite(generated):
                raise ValueError("non-finite timestamp")
            age = time.time() - generated
        except Exception:
            return False, "money-flow timestamp is malformed"
        if age < -30 or age > self.max_flow_age:
            return False, "money-flow snapshot is stale"
        if not flow.get("ok"):
            return False, "money-flow service is degraded"
        futures = flow.get("futures") or {}
        if self.require_matching_futures and not futures.get("available"):
            return False, "matching USD-M futures context is required but unavailable"
        if not (flow.get("classification") or {}).get("bullish"):
            return False, "money-flow context is not bullish"
        return True, "fresh bullish flow"

    def process_signal(self, path):
        path = Path(path)
        try:
            if path.stat().st_size > env_int("MAX_SIGNAL_FILE_BYTES", 65_536, 1024, 1_048_576):
                return self.reject(path, {}, "signal_file_too_large", "")
            raw = json.loads(path.read_text(encoding="utf-8"))
            sig = envelope.verify_envelope(
                raw, purpose=envelope.BUS_SIGNAL, expected_producers={SIGNAL_PRODUCER})
        except Exception as exc:
            return self.reject(path, {}, "invalid_signed_envelope", str(exc), record=False)
        required = {"signal_id", "pair", "symbol", "candle_time", "generated_at",
                    "strategy", "entry_tag", "pair_state_hash", "pair_generation",
                    "pair_config_hash", "payload"}
        missing = sorted(required - set(sig))
        if missing:
            return self.reject(path, sig, "missing_signal_fields", ",".join(missing))
        try:
            pair = canonical_pair(str(sig["pair"]))
        except Exception as exc:
            return self.reject(path, sig, "invalid_pair", str(exc))
        symbol = str(sig.get("symbol", "")).upper()
        if pair.split("/", 1)[0] != "BTC" or symbol != pair.replace("/", ""):
            return self.reject(path, sig, "btc_pair_symbol_mismatch", f"{pair} != {symbol}")
        signal_id = str(sig.get("signal_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", signal_id):
            return self.reject(path, sig, "invalid_signal_id", signal_id, record=False)
        prior = self.store.signal_result(signal_id)
        if prior == "IN_PROGRESS":
            self._pause("uncertain-prior-signal-submission-reconcile-required")
            return self.reject(path, sig, "uncertain_prior_submission", "", record=False)
        if prior is not None:
            return self.reject(path, sig, "duplicate_signal_token", "", record=False)
        if str(sig.get("strategy")) != "IctSmcStrategy":
            return self.reject(path, sig, "unexpected_strategy", str(sig.get("strategy")))
        pair_state = self.pair_controller.load()
        if pair != pair_state["pair"] or symbol != pair_state["symbol"]:
            return self.reject(path, sig, "stale_or_wrong_active_pair", pair)
        if sig.get("pair_state_hash") != pair_state["state_hash"]:
            return self.reject(path, sig, "pair_state_hash_mismatch", "")
        if sig.get("pair_generation") != pair_state["generation"]:
            return self.reject(path, sig, "pair_generation_mismatch", "")
        if sig.get("pair_config_hash") != pair_state["pair_config_hash"]:
            return self.reject(path, sig, "pair_config_hash_mismatch", "")
        ok, why = self.guard.allow(sig, pair_state["pair"], pair_state["state_hash"], self.store)
        if not ok:
            return self.reject(path, sig, why, "")
        flow_ok, flow_reason = self._flow_gate(pair_state)
        if self.require_flow and not flow_ok:
            return self.reject(path, sig, "flow_gate_blocked", flow_reason)
        if not self.require_flow and not flow_ok:
            audit("flow_context_advisory_unavailable", severity="WARNING", details={
                "signal_id": signal_id, "reason": flow_reason})
        if not self.state.entries():
            return self.reject(path, sig, "entries_paused",
                               self.state.data.get("pause_reason", ""))
        if not self.store.claim_signal(sig):
            return self.reject(path, sig, "duplicate_signal_token", "", record=False)
        self.store.register_symbol_pair(symbol, pair)
        self.store.upsert_trade(
            signal_id, pair, lifecycle_state="SIGNAL_APPROVED",
            protection_mode=self.state.get_mode(), reconciliation_status="SIGNAL_ACCEPTED")
        accepted, message = self.adapter.submit(
            symbol, f"Freqtrade signal {signal_id} candle {sig['candle_time']}",
            trade_id=signal_id)
        simulation = bool(self.state.data.get("simulation", True))
        label = "simulated:" if simulation else ""
        result = label + ("accepted" if accepted else "rejected:" + str(message))
        self.guard.record(sig, result)
        self.store.record_signal(sig, result)
        if not accepted:
            current = self.store.active_trade_for_symbol(symbol)
            if current and current["lifecycle_state"] == "SIGNAL_APPROVED":
                self.store.upsert_trade(signal_id, pair, lifecycle_state="ENTRY_REJECTED",
                                        reconciliation_status="ENTRY_NOT_SUBMITTED")
        self.move(path, self.paths["processed"] if accepted else self.paths["rejected"])
        audit("signal_execution_result", severity="INFO" if accepted else "WARNING", details={
            "signal_id": signal_id, "accepted": accepted, "message": str(message),
            "simulation": simulation, "flow_gate_required": self.require_flow,
            "flow_context": flow_reason})
        return accepted, message

    def _pause(self, reason: str):
        self.state.set_entries(False, reason)
        self.guard.set_global_pause(reason)
        try:
            self.adapter.set_enabled(False)
        except Exception:
            pass

    @staticmethod
    def move(path, destination):
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / path.name
        if target.exists():
            target = destination / f"{path.stem}.{time.time_ns()}{path.suffix}"
        shutil.move(str(path), str(target))
        prune_files(destination, "*.json",
                    max_files=max(0, int(os.getenv("SIGNAL_ARCHIVE_MAX_FILES", "10000"))))

    def reject(self, path, signal, reason, detail, *, record=True):
        if record and signal.get("signal_id") and signal.get("pair"):
            self.store.record_signal(signal, "rejected:" + reason)
        audit("signal_rejected", severity="WARNING", details={
            "file": path.name, "reason": reason, "detail": detail})
        self.move(path, self.paths["rejected"])
        return False, reason
