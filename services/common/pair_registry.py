from __future__ import annotations
"""Current Binance BTC-base Spot pair registry.

The registry exists only to populate the owner-operated pair menu and to
revalidate a requested selection. It sorts alphabetically for presentation;
it never ranks markets, inspects altcoin signals, or changes the active pair.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Mapping

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.binance_public import BinancePublicClient
from services.common.market_policy import (
    PairPolicyError,
    allowed_quotes_from_env,
    canonical_pair,
    validate_exchange_symbol,
)


class PairRegistryError(RuntimeError):
    pass


class PairRegistry:
    def __init__(
        self,
        cache_path: str | Path,
        *,
        public_client: BinancePublicClient | None = None,
        ttl_seconds: int = 300,
        allowed_quotes=None,
    ):
        self.cache_path = Path(cache_path)
        self.public = public_client or BinancePublicClient()
        self.ttl_seconds = max(60, min(int(ttl_seconds), 3600))
        self.allowed_quotes = tuple(
            allowed_quotes_from_env() if allowed_quotes is None else allowed_quotes
        )
        self._snapshot: dict | None = None

    @staticmethod
    def _registry_hash(rows: list[dict]) -> str:
        bound = [
            {
                "pair": row["pair"],
                "symbol": row["symbol"],
                "quote": row["quote"],
                "capabilities": row["capabilities"],
                "filter_types": row["filter_types"],
            }
            for row in rows
        ]
        return hashlib.sha256(
            json.dumps(bound, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _row(self, metadata: Mapping) -> dict:
        quote = str(metadata.get("quoteAsset", "")).strip().upper()
        pair = canonical_pair(f"BTC/{quote}", self.allowed_quotes)
        validated = validate_exchange_symbol(
            pair,
            metadata,
            self.allowed_quotes,
            require_order_lists=False,
        )
        filter_types = sorted({
            str(item.get("filterType"))
            for item in validated.get("filters", [])
            if isinstance(item, Mapping) and item.get("filterType")
        })
        trailing = (
            validated.get("allowTrailingStop") is True
            and "TRAILING_DELTA" in filter_types
        )
        oco = validated.get("ocoAllowed") is True
        oto = validated.get("otoAllowed") is True
        return {
            "pair": pair,
            "symbol": str(validated["symbol"]).upper(),
            "base": "BTC",
            "quote": quote,
            "capabilities": {
                "spot": True,
                "oco": oco,
                "oto": oto,
                "trailing_stop": trailing,
                # These three mode flags describe complete entry + protection
                # support, not merely the standalone replacement endpoint.
                "fixed_oco": oto and oco,
                "oco_trailing": oto and oco and trailing,
                "trailing_only": oto and trailing,
            },
            "filter_types": filter_types,
        }

    def _validate_snapshot(self, value) -> dict:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise PairRegistryError("pair registry schema is invalid")
        try:
            generated = float(value["generated_at_epoch"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PairRegistryError("pair registry timestamp is invalid") from exc
        rows = value.get("pairs")
        if not isinstance(rows, list) or not rows:
            raise PairRegistryError("pair registry contains no eligible BTC markets")
        clean = []
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                raise PairRegistryError("pair registry row is malformed")
            pair = canonical_pair(str(item.get("pair", "")), self.allowed_quotes)
            if pair in seen or item.get("symbol") != pair.replace("/", ""):
                raise PairRegistryError("pair registry identity is inconsistent")
            if item.get("base") != "BTC" or item.get("quote") != pair.split("/", 1)[1]:
                raise PairRegistryError("pair registry assets are inconsistent")
            capabilities = item.get("capabilities")
            filters = item.get("filter_types")
            if not isinstance(capabilities, dict) or capabilities.get("spot") is not True:
                raise PairRegistryError("pair registry capabilities are malformed")
            if not isinstance(filters, list) or not {
                "PRICE_FILTER", "LOT_SIZE"
            }.issubset(set(filters)):
                raise PairRegistryError("pair registry filters are incomplete")
            seen.add(pair)
            clean.append({
                "pair": pair,
                "symbol": item["symbol"],
                "base": "BTC",
                "quote": item["quote"],
                "capabilities": dict(capabilities),
                "filter_types": list(filters),
            })
        clean.sort(key=lambda row: (row["quote"], row["symbol"]))
        if value.get("registry_hash") != self._registry_hash(clean):
            raise PairRegistryError("pair registry hash is invalid")
        return {
            "schema_version": 1,
            "generated_at": str(value.get("generated_at", "")),
            "generated_at_epoch": generated,
            "source": str(value.get("source", "")),
            "registry_hash": value["registry_hash"],
            "pairs": clean,
        }

    def _cached(self, now: float) -> dict | None:
        candidates = [self._snapshot, read_json(self.cache_path, None)]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                clean = self._validate_snapshot(candidate)
                age = now - clean["generated_at_epoch"]
                if -30 <= age <= self.ttl_seconds:
                    self._snapshot = clean
                    return clean
            except (PairRegistryError, PairPolicyError):
                continue
        return None

    def refresh(self, *, force: bool = False, now: float | None = None) -> dict:
        epoch = time.time() if now is None else float(now)
        if not force and (cached := self._cached(epoch)) is not None:
            return cached
        try:
            payload = self.public.exchange_info()
        except Exception as exc:
            raise PairRegistryError(
                f"current Binance BTC pair registry is unavailable: {type(exc).__name__}"
            ) from exc
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list):
            raise PairRegistryError("Binance exchangeInfo symbols are malformed")
        rows = []
        for metadata in symbols:
            if not isinstance(metadata, Mapping):
                continue
            if str(metadata.get("baseAsset", "")).upper() != "BTC":
                continue
            try:
                rows.append(self._row(metadata))
            except PairPolicyError:
                continue
        rows.sort(key=lambda row: (row["quote"], row["symbol"]))
        if not rows:
            raise PairRegistryError("Binance returned no eligible BTC-base Spot markets")
        snapshot = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_at_epoch": epoch,
            "source": "binance_exchangeInfo",
            "registry_hash": self._registry_hash(rows),
            "pairs": rows,
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.cache_path, snapshot, mode=0o640)
        self._snapshot = snapshot
        audit("btc_pair_registry_refreshed", details={
            "eligible_pairs": len(rows),
            "registry_hash": snapshot["registry_hash"],
        })
        return snapshot

    def require_pair(self, pair: str) -> dict:
        requested = canonical_pair(pair, self.allowed_quotes)
        snapshot = self.refresh(force=True)
        row = next(
            (item for item in snapshot["pairs"] if item["pair"] == requested),
            None,
        )
        if row is None:
            raise PairRegistryError(
                f"{requested} is not currently eligible in Binance exchangeInfo"
            )
        selected = dict(row)
        selected["registry_hash"] = snapshot["registry_hash"]
        selected["registry_generated_at_epoch"] = snapshot["generated_at_epoch"]
        return selected
