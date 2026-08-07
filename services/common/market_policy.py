from __future__ import annotations
"""Fail-closed Bitcoin/Spot pair policy shared by every service.

The bot may enumerate current Binance BTC-base Spot markets for an owner menu,
but it never ranks assets or changes the selected pair automatically. Symbol
identity comes from ``exchangeInfo`` metadata for every money-moving decision;
string parsing is used only to normalize an explicitly selected BTC pair.
"""

import hashlib
import json
import os
import re
from typing import Iterable, Mapping


PAIR_RE = re.compile(r"^BTC/([A-Z0-9]{2,12})$")


class PairPolicyError(ValueError):
    pass


def _quotes(values: Iterable[str], *, allow_empty: bool = False) -> tuple[str, ...]:
    cleaned = tuple(dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip()))
    if (not cleaned and not allow_empty) or any(
        not re.fullmatch(r"[A-Z0-9]{2,12}", value) for value in cleaned
    ):
        raise PairPolicyError("BTC quote allowlist must contain 2-12 character asset codes")
    return cleaned


def allowed_quotes_from_env() -> tuple[str, ...]:
    """Return the optional operator quote cap; empty means all eligible quotes."""
    raw = os.getenv("BTC_QUOTE_ALLOWLIST")
    if raw is None:
        raw = os.getenv("ALLOWED_STABLE_QUOTES", "")
    return _quotes(str(raw).split(","), allow_empty=True)


def canonical_pair(value: str, allowed_quotes: Iterable[str] | None = None) -> str:
    raw = str(value or "").strip().upper().replace("-", "/").replace("_", "/")
    if "/" not in raw:
        if raw.startswith("BTC") and re.fullmatch(r"[A-Z0-9]{5,15}", raw):
            raw = "BTC/" + raw[3:]
    match = PAIR_RE.fullmatch(raw)
    if not match:
        raise PairPolicyError("pair must use the form BTC/QUOTE")
    quotes = (
        allowed_quotes_from_env()
        if allowed_quotes is None
        else _quotes(allowed_quotes, allow_empty=True)
    )
    if quotes and match.group(1) not in quotes:
        raise PairPolicyError("pair quote is outside the configured BTC quote allowlist")
    return raw


def symbol_for_pair(pair: str, allowed_quotes: Iterable[str] | None = None) -> str:
    return canonical_pair(pair, allowed_quotes).replace("/", "")


def quote_for_pair(pair: str, allowed_quotes: Iterable[str] | None = None) -> str:
    return canonical_pair(pair, allowed_quotes).split("/", 1)[1]


def validate_exchange_symbol(
    pair: str,
    metadata: Mapping,
    allowed_quotes: Iterable[str] | None = None,
    *,
    require_order_lists: bool = True,
) -> dict:
    """Validate current Binance metadata for the requested Spot market."""
    pair = canonical_pair(pair, allowed_quotes)
    symbol = pair.replace("/", "")
    quote = pair.split("/", 1)[1]
    if not isinstance(metadata, Mapping) or str(metadata.get("symbol", "")).upper() != symbol:
        raise PairPolicyError("exchangeInfo symbol does not match the requested pair")
    if str(metadata.get("status", "")).upper() != "TRADING":
        raise PairPolicyError(f"{symbol} is not TRADING")
    if str(metadata.get("baseAsset", "")).upper() != "BTC":
        raise PairPolicyError("base asset must be BTC")
    if str(metadata.get("quoteAsset", "")).upper() != quote:
        raise PairPolicyError("quote asset does not match the requested pair")
    permissions = {str(value).upper() for value in (metadata.get("permissions") or [])}
    if permissions and "SPOT" not in permissions:
        raise PairPolicyError(f"{symbol} is not enabled for Spot")
    permission_sets = metadata.get("permissionSets") or []
    if permission_sets:
        if not isinstance(permission_sets, list) or not any(
            isinstance(group, list)
            and "SPOT" in {str(value).upper() for value in group}
            for group in permission_sets
        ):
            raise PairPolicyError(f"{symbol} permission sets do not enable Spot")
    if metadata.get("isSpotTradingAllowed") is not True:
        raise PairPolicyError(f"{symbol} Spot trading is disabled")
    if require_order_lists:
        if not bool(metadata.get("ocoAllowed", False)):
            raise PairPolicyError(f"{symbol} does not advertise OCO support")
        if not bool(metadata.get("otoAllowed", False)):
            raise PairPolicyError(f"{symbol} does not advertise OTO/OTOCO support")
    filters = metadata.get("filters")
    if not isinstance(filters, list):
        raise PairPolicyError("exchangeInfo filters are missing")
    by_type = {str(item.get("filterType")): item for item in filters if isinstance(item, Mapping)}
    for required in ("PRICE_FILTER", "LOT_SIZE"):
        if required not in by_type:
            raise PairPolicyError(f"exchangeInfo is missing {required}")
    return dict(metadata)


def pair_state_hash(state: Mapping) -> str:
    bound = {
        "schema_version": state.get("schema_version"),
        "pair": state.get("pair"),
        "symbol": state.get("symbol"),
        "base": state.get("base"),
        "quote": state.get("quote"),
        "generation": state.get("generation"),
        "pair_config_hash": state.get("pair_config_hash"),
    }
    return hashlib.sha256(
        json.dumps(bound, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def pair_config_hash(pair: str, allowed_quotes: Iterable[str] | None = None) -> str:
    """Hash the exact one-pair Freqtrade projection used by both runtimes."""
    normalized = canonical_pair(pair, allowed_quotes)
    projection = {
        "pair": normalized,
        "symbol": normalized.replace("/", ""),
        "stake_currency": normalized.split("/", 1)[1],
        "pair_whitelist": [normalized],
    }
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
