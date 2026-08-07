from __future__ import annotations
"""Single-writer active-pair state and safe pair switching.

Only the execution sidecar mounts ``shared/pair`` read-write.  Freqtrade and
the money-flow service consume the projections read-only.  A switch is allowed
only after the exchange adapter has positively verified that the old and new
symbols have no position, open order, or open order list.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.market_policy import (
    PairPolicyError,
    allowed_quotes_from_env,
    canonical_pair,
    pair_config_hash,
    pair_state_hash,
    quote_for_pair,
    symbol_for_pair,
)
from services.common.pair_registry import PairRegistry, PairRegistryError


class PairStateError(RuntimeError):
    pass


PAIR_SWITCH_STAGES = {
    "ACTIVE",
    "PAUSING",
    "RECONCILING",
    "WAITING_MANUAL_SWAP",
    "APPLYING_PAIR",
    "VERIFYING_PAIR",
    "PAUSED_READY",
    "BLOCKED",
}


class PairController:
    def __init__(self, state_path: str | Path, pairlist_path: str | Path,
                 freqtrade_config_path: str | Path, *, allowed_quotes=None,
                 registry: PairRegistry | None = None,
                 switch_state_path: str | Path | None = None):
        self.state_path = Path(state_path)
        self.pairlist_path = Path(pairlist_path)
        self.freqtrade_config_path = Path(freqtrade_config_path)
        self.switch_state_path = (
            Path(switch_state_path)
            if switch_state_path is not None
            else self.state_path.with_name("pair_switch_state.json")
        )
        self.allowed_quotes = tuple(
            allowed_quotes_from_env() if allowed_quotes is None else allowed_quotes
        )
        self.registry = registry

    def _build_state(self, pair: str, generation: int, source: str) -> dict:
        pair = canonical_pair(pair, self.allowed_quotes)
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "schema_version": 1,
            "pair": pair,
            "symbol": symbol_for_pair(pair, self.allowed_quotes),
            "base": "BTC",
            "quote": quote_for_pair(pair, self.allowed_quotes),
            "generation": int(generation),
            "updated_at": now,
            "source": str(source),
        }
        state["pair_config_hash"] = pair_config_hash(pair, self.allowed_quotes)
        state["state_hash"] = pair_state_hash(state)
        return state

    def _validate_state(self, state) -> dict:
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise PairStateError("active pair state is missing or has an unsupported schema")
        pair = canonical_pair(str(state.get("pair", "")), self.allowed_quotes)
        if state.get("symbol") != symbol_for_pair(pair, self.allowed_quotes):
            raise PairStateError("active pair symbol does not match pair")
        if state.get("base") != "BTC" or state.get("quote") != quote_for_pair(pair, self.allowed_quotes):
            raise PairStateError("active pair assets are inconsistent")
        if not isinstance(state.get("generation"), int) or state["generation"] < 1:
            raise PairStateError("active pair generation is invalid")
        if state.get("pair_config_hash") != pair_config_hash(pair, self.allowed_quotes):
            raise PairStateError("active pair configuration hash is invalid")
        if state.get("state_hash") != pair_state_hash(state):
            raise PairStateError("active pair state hash is invalid")
        return dict(state)

    def load(self) -> dict:
        try:
            return self._validate_state(read_json(self.state_path, None))
        except PairPolicyError as exc:
            raise PairStateError(str(exc)) from exc

    def _publish(self, state: dict) -> None:
        """Publish projections first and the authoritative state last."""
        pair = state["pair"]
        quote = state["quote"]
        atomic_write_json(self.pairlist_path, {
            "pairs": [pair],
            "refresh_period": 10,
            "pair_state_hash": state["state_hash"],
            "pair_config_hash": state["pair_config_hash"],
        })
        atomic_write_json(self.freqtrade_config_path, {
            "stake_currency": quote,
            "exchange": {"pair_whitelist": [pair], "pair_blacklist": []},
        })
        atomic_write_json(self.state_path, state)

    def _switch_state(
        self,
        stage: str,
        *,
        target_pair: str | None = None,
        detail: str = "",
        evidence: dict | None = None,
    ) -> dict:
        if stage not in PAIR_SWITCH_STAGES:
            raise PairStateError("pair-switch stage is invalid")
        active = self.load()
        target = (
            canonical_pair(target_pair, self.allowed_quotes)
            if target_pair
            else None
        )
        value = {
            "schema_version": 1,
            "stage": stage,
            "active_pair": active["pair"],
            "active_generation": active["generation"],
            "active_pair_config_hash": active["pair_config_hash"],
            "target_pair": target,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "detail": str(detail)[:1000],
            "evidence": dict(evidence or {}),
        }
        atomic_write_json(self.switch_state_path, value, mode=0o640)
        audit("pair_switch_stage", severity=(
            "ERROR" if stage == "BLOCKED"
            else "WARNING" if stage != "ACTIVE"
            else "INFO"
        ), details={
            "stage": stage,
            "active_pair": active["pair"],
            "target_pair": target,
            "detail": value["detail"],
        })
        return value

    def switch_status(self) -> dict:
        active = self.load()
        value = read_json(self.switch_state_path, None)
        if value is None:
            return {
                "schema_version": 1,
                "stage": "ACTIVE",
                "active_pair": active["pair"],
                "active_generation": active["generation"],
                "active_pair_config_hash": active["pair_config_hash"],
                "target_pair": None,
                "detail": "no pair switch is pending",
                "evidence": {},
            }
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise PairStateError("pair-switch state is corrupt")
        stage = str(value.get("stage", ""))
        if stage not in PAIR_SWITCH_STAGES:
            raise PairStateError("pair-switch stage is corrupt")
        target = value.get("target_pair")
        if target is not None:
            target = canonical_pair(str(target), self.allowed_quotes)
        if value.get("active_pair") != active["pair"]:
            if stage not in {"VERIFYING_PAIR", "PAUSED_READY", "ACTIVE"}:
                raise PairStateError(
                    "pair-switch state does not match the authoritative active pair"
                )
        return {
            **value,
            "stage": stage,
            "target_pair": target,
            "current_active_pair": active["pair"],
            "current_generation": active["generation"],
            "current_pair_config_hash": active["pair_config_hash"],
        }

    def bootstrap(self, initial_pair: str) -> dict:
        existing = [path.exists() for path in (
            self.state_path, self.pairlist_path, self.freqtrade_config_path)]
        if any(existing) and not all(existing):
            raise PairStateError("pair state is only partially present; refusing an ambiguous bootstrap")
        if all(existing):
            state = self.load()
            pairlist = read_json(self.pairlist_path, None)
            overlay = read_json(self.freqtrade_config_path, None)
            expected_pairlist = {
                "pairs": [state["pair"]], "refresh_period": 10,
                "pair_state_hash": state["state_hash"],
                "pair_config_hash": state["pair_config_hash"],
            }
            if pairlist != expected_pairlist:
                raise PairStateError("Freqtrade pairlist does not match authoritative active pair")
            expected_overlay = {
                "stake_currency": state["quote"],
                "exchange": {"pair_whitelist": [state["pair"]], "pair_blacklist": []},
            }
            if overlay != expected_overlay:
                raise PairStateError("Freqtrade active configuration does not match active quote")
            return state
        state = self._build_state(initial_pair, 1, "bootstrap")
        self._publish(state)
        audit("active_pair_bootstrapped", details={"pair": state["pair"], "hash": state["state_hash"]})
        return state

    def switch(self, requested_pair: str,
               verify_flat: Callable[[set[str]], dict]) -> dict:
        """Compatibility wrapper for tests/tools; runtime uses the staged API."""
        current = self.load()
        requested = canonical_pair(requested_pair, self.allowed_quotes)
        eligibility = None
        if self.registry is not None:
            try:
                eligibility = self.registry.require_pair(requested)
            except PairRegistryError as exc:
                raise PairStateError(str(exc)) from exc
        if requested == current["pair"]:
            return {"ok": True, "changed": False, "state": current,
                    "detail": f"{requested} is already active"}
        symbols = {current["symbol"], symbol_for_pair(requested, self.allowed_quotes)}
        verification = verify_flat(symbols)
        if not isinstance(verification, dict) or not verification.get("ok"):
            detail = (verification or {}).get("detail", "exchange flatness could not be verified")
            raise PairStateError("pair switch refused: " + str(detail))
        next_state = self._build_state(requested, current["generation"] + 1, "owner-command")
        self._publish(next_state)
        audit("active_pair_switched", severity="WARNING", details={
            "from": current["pair"], "to": requested,
            "generation": next_state["generation"], "verification": verification,
        })
        return {"ok": True, "changed": True, "state": next_state,
                "verification": verification, "eligibility": eligibility,
                "detail": f"active pair changed to {requested}; reload Freqtrade configuration"}

    def begin_switch(
        self,
        requested_pair: str,
        verify_flat: Callable[[set[str]], dict],
        *,
        expected_registry_hash: str = "",
    ) -> dict:
        current = self.load()
        requested = canonical_pair(requested_pair, self.allowed_quotes)
        status = self.switch_status()
        if status["stage"] not in {"ACTIVE", "PAUSED_READY", "BLOCKED"}:
            raise PairStateError(
                f"pair switch already pending in {status['stage']}"
            )
        if requested == current["pair"]:
            self._switch_state(
                "ACTIVE",
                detail=f"{requested} is already active",
            )
            return {
                "ok": True,
                "changed": False,
                "state": current,
                "switch": self.switch_status(),
                "detail": f"{requested} is already active",
            }
        if self.registry is None:
            raise PairStateError("BTC pair registry is unavailable")
        try:
            eligibility = self.registry.require_pair(requested)
        except PairRegistryError as exc:
            raise PairStateError(str(exc)) from exc
        current_registry_hash = str(eligibility.get("registry_hash", ""))
        if expected_registry_hash and expected_registry_hash != current_registry_hash:
            raise PairStateError(
                "pair-menu registry changed; reopen /pairs before selecting"
            )
        self._switch_state(
            "PAUSING",
            target_pair=requested,
            detail="entries disarmed; beginning flat-state verification",
            evidence={"registry_hash": current_registry_hash},
        )
        self._switch_state(
            "RECONCILING",
            target_pair=requested,
            detail="verifying durable and exchange state",
            evidence={"registry_hash": current_registry_hash},
        )
        symbols = {
            current["symbol"],
            symbol_for_pair(requested, self.allowed_quotes),
        }
        try:
            verification = verify_flat(symbols)
        except Exception as exc:
            self._switch_state(
                "BLOCKED",
                target_pair=requested,
                detail=f"flat-state verification raised {type(exc).__name__}",
                evidence={"registry_hash": current_registry_hash},
            )
            raise
        if not isinstance(verification, dict) or verification.get("ok") is not True:
            detail = (verification or {}).get(
                "detail", "exchange flatness could not be verified"
            )
            self._switch_state(
                "BLOCKED",
                target_pair=requested,
                detail=str(detail),
                evidence={"registry_hash": current_registry_hash},
            )
            raise PairStateError("pair switch refused: " + str(detail))
        pending = self._switch_state(
            "WAITING_MANUAL_SWAP",
            target_pair=requested,
            detail=(
                "flat state verified; owner must complete any required quote-asset "
                "swap and confirm /swapdone"
            ),
            evidence={
                "registry_hash": current_registry_hash,
                "flat_verification": verification,
                "eligibility": eligibility,
            },
        )
        return {
            "ok": True,
            "changed": False,
            "state": current,
            "switch": pending,
            "verification": verification,
            "eligibility": eligibility,
            "detail": (
                f"{requested} selected; complete any manual quote-asset swap, "
                "then confirm /swapdone"
            ),
        }

    def complete_switch(
        self,
        verify_flat: Callable[[set[str]], dict],
        validate_target: Callable[[str], dict],
    ) -> dict:
        status = self.switch_status()
        if status["stage"] != "WAITING_MANUAL_SWAP":
            raise PairStateError(
                "no pair switch is waiting for manual-swap confirmation"
            )
        target = canonical_pair(
            str(status.get("target_pair", "")),
            self.allowed_quotes,
        )
        current = self.load()
        evidence = dict(status.get("evidence") or {})
        self._switch_state(
            "RECONCILING",
            target_pair=target,
            detail="owner confirmed manual swap; rechecking flat state",
            evidence=evidence,
        )
        symbols = {
            current["symbol"],
            symbol_for_pair(target, self.allowed_quotes),
        }
        try:
            verification = verify_flat(symbols)
            if not isinstance(verification, dict) or verification.get("ok") is not True:
                detail = (verification or {}).get(
                    "detail", "exchange flatness could not be verified"
                )
                raise PairStateError(str(detail))
            target_validation = validate_target(target)
        except Exception as exc:
            self._switch_state(
                "BLOCKED",
                target_pair=target,
                detail=f"manual-swap validation failed: {exc}",
                evidence=evidence,
            )
            raise
        applying_evidence = {
            **evidence,
            "second_flat_verification": verification,
            "target_validation": target_validation,
        }
        self._switch_state(
            "APPLYING_PAIR",
            target_pair=target,
            detail="publishing exact one-pair Freqtrade projections",
            evidence=applying_evidence,
        )
        next_state = self._build_state(
            target,
            current["generation"] + 1,
            "owner-confirmed-manual-swap",
        )
        try:
            self._publish(next_state)
            loaded = self.load()
            if loaded != next_state:
                raise PairStateError("published active pair failed read-back")
        except Exception as exc:
            self._switch_state(
                "BLOCKED",
                target_pair=target,
                detail=f"pair projection publication failed: {type(exc).__name__}",
                evidence=applying_evidence,
            )
            raise
        verifying = self._switch_state(
            "VERIFYING_PAIR",
            target_pair=target,
            detail="pair applied; Freqtrade reload and heartbeat proof required",
            evidence=applying_evidence,
        )
        return {
            "ok": True,
            "changed": True,
            "state": next_state,
            "switch": verifying,
            "verification": verification,
            "target_validation": target_validation,
            "detail": (
                f"active pair changed to {target}; reload Freqtrade and verify "
                "its pair-generation heartbeat"
            ),
        }

    def mark_pair_verified(self, proof: dict) -> dict:
        status = self.switch_status()
        if status["stage"] != "VERIFYING_PAIR":
            raise PairStateError("pair switch is not waiting for Freqtrade verification")
        if not isinstance(proof, dict) or proof.get("ok") is not True:
            raise PairStateError("Freqtrade pair proof is not affirmative")
        active = self.load()
        if status.get("target_pair") != active["pair"]:
            raise PairStateError("verified pair does not match the switch target")
        return self._switch_state(
            "PAUSED_READY",
            target_pair=active["pair"],
            detail="pair and Freqtrade generation verified; explicit resume required",
            evidence={**dict(status.get("evidence") or {}), "freqtrade": proof},
        )

    def require_resume_ready(self) -> dict:
        status = self.switch_status()
        if status["stage"] not in {"ACTIVE", "PAUSED_READY"}:
            raise PairStateError(
                f"entries cannot resume while pair switch is {status['stage']}"
            )
        return status

    def mark_active_after_resume(self) -> dict:
        status = self.require_resume_ready()
        if status["stage"] == "ACTIVE":
            return status
        return self._switch_state(
            "ACTIVE",
            detail="owner explicitly resumed after verified pair switch",
        )

    def eligible_pairs(self, *, force: bool = True) -> dict:
        if self.registry is None:
            raise PairStateError("BTC pair registry is unavailable")
        try:
            return self.registry.refresh(force=force)
        except PairRegistryError as exc:
            raise PairStateError(str(exc)) from exc
