from __future__ import annotations
"""Quota-safe BTC-only context from CoinGecko and CoinMarketCap.

The output is advisory telemetry.  It is intentionally isolated from strategy,
order sizing, exchange filters, protection management, and entry decisions.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from services.common.atomic import atomic_write_json
from services.common.config_bounds import env_choice, env_float, env_int
from services.common.paths import MONEYFLOW_FILE, RUNTIME
from services.moneyflow.external_clients import (
    CoinGeckoClient,
    CoinMarketCapClient,
    ProviderHTTPError,
    ProviderPayloadError,
    ProviderTransportError,
)


COINGECKO_FREE_MINUTE = 100
COINGECKO_FREE_MONTHLY = 10_000
# CoinMarketCap's public Basic-plan pages currently disagree with a newer FAQ
# about the larger allowance.  Use the lower documented figures so the bot is
# safe under either publication: 30 requests/minute and 10,000 credits/month.
COINMARKETCAP_FREE_MINUTE = 30
COINMARKETCAP_FREE_MONTHLY = 10_000


def four_percent_below(limit: int) -> int:
    """Return the integer cap at 96% without ever rounding upward."""
    return int(limit) * 96 // 100


COINGECKO_SAFE_MINUTE = four_percent_below(COINGECKO_FREE_MINUTE)
COINGECKO_SAFE_MONTHLY = four_percent_below(COINGECKO_FREE_MONTHLY)
COINMARKETCAP_SAFE_MINUTE = four_percent_below(COINMARKETCAP_FREE_MINUTE)
COINMARKETCAP_SAFE_MONTHLY = four_percent_below(COINMARKETCAP_FREE_MONTHLY)

ALLOWED_PROVIDERS = {"coingecko", "coinmarketcap"}
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024


class QuotaLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reason: str
    quota: dict


def _month_utc(now: float) -> str:
    return datetime.fromtimestamp(float(now), tz=timezone.utc).strftime("%Y-%m")


def _minute_window(now: float) -> int:
    return int(float(now) // 60)


def _safe_storage_path(path: Path, *, maximum_bytes: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise QuotaLedgerError("storage parent must not be a symlink")
    if path.is_symlink():
        raise QuotaLedgerError("storage file must not be a symlink")
    if path.exists():
        if not path.is_file():
            raise QuotaLedgerError("storage path is not a regular file")
        try:
            if path.stat().st_size > maximum_bytes:
                raise QuotaLedgerError("storage file exceeds size limit")
        except OSError as exc:
            raise QuotaLedgerError("storage metadata is unavailable") from exc


class QuotaLedger:
    """Cross-process SQLite quota reservation ledger.

    Reservations are committed before network dispatch.  A crash can therefore
    waste quota but cannot create an uncounted outbound request.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.marker_path = self.path.with_name(self.path.name + ".initialized")
        self._initialize()

    def _validate_marker(self) -> bool:
        _safe_storage_path(self.marker_path, maximum_bytes=4096)
        if not self.marker_path.exists():
            return False
        try:
            payload = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise QuotaLedgerError("quota installation marker is corrupt") from exc
        if payload != {
            "schema_version": 1,
            "quota_database": self.path.name,
        }:
            raise QuotaLedgerError("quota installation marker is invalid")
        return True

    def _create_marker(self) -> None:
        if self._validate_marker():
            return
        payload = json.dumps({
            "schema_version": 1,
            "quota_database": self.path.name,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.marker_path, flags, 0o640)
        except FileExistsError:
            if not self._validate_marker():
                raise QuotaLedgerError("quota installation marker race failed closed")
            return
        except OSError as exc:
            raise QuotaLedgerError("quota installation marker is unavailable") from exc
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _connect(self):
        _safe_storage_path(self.path, maximum_bytes=MAX_LEDGER_BYTES)
        connection = None
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=5,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise QuotaLedgerError("quota ledger is unavailable") from exc

    def _initialize(self) -> None:
        existed = self.path.exists()
        marker_existed = self._validate_marker()
        if marker_existed and not existed:
            raise QuotaLedgerError(
                "quota database disappeared after initialization; refusing a zero reset"
            )
        connection = self._connect()
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS provider_quota (
                       provider TEXT PRIMARY KEY,
                       month_utc TEXT NOT NULL,
                       month_attempts INTEGER NOT NULL CHECK(month_attempts >= 0),
                       minute_window INTEGER NOT NULL,
                       minute_attempts INTEGER NOT NULL CHECK(minute_attempts >= 0),
                       next_allowed_at REAL NOT NULL,
                       failure_count INTEGER NOT NULL CHECK(failure_count >= 0),
                       last_status TEXT NOT NULL
                   )"""
            )
            check = connection.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise QuotaLedgerError("quota ledger integrity check failed")
        except (sqlite3.Error, QuotaLedgerError) as exc:
            if isinstance(exc, QuotaLedgerError):
                raise
            raise QuotaLedgerError("quota ledger is corrupt") from exc
        finally:
            connection.close()
        if not existed:
            try:
                os.chmod(self.path, 0o640)
            except OSError:
                pass
        self._create_marker()

    @staticmethod
    def _validate(provider: str, minute_cap: int, monthly_cap: int) -> None:
        if provider not in ALLOWED_PROVIDERS:
            raise QuotaLedgerError("unsupported quota provider")
        if minute_cap < 1 or monthly_cap < 1:
            raise QuotaLedgerError("quota caps must be positive")

    @staticmethod
    def _normalize_row(row, *, provider: str, now: float) -> dict:
        current_month = _month_utc(now)
        current_minute = _minute_window(now)
        if row is None:
            return {
                "provider": provider,
                "month_utc": current_month,
                "month_attempts": 0,
                "minute_window": current_minute,
                "minute_attempts": 0,
                "next_allowed_at": 0.0,
                "failure_count": 0,
                "last_status": "never",
            }
        state = {
            "provider": provider,
            "month_utc": str(row[0]),
            "month_attempts": int(row[1]),
            "minute_window": int(row[2]),
            "minute_attempts": int(row[3]),
            "next_allowed_at": float(row[4]),
            "failure_count": int(row[5]),
            "last_status": str(row[6]),
        }
        if state["month_utc"] != current_month:
            state["month_utc"] = current_month
            state["month_attempts"] = 0
        if state["minute_window"] != current_minute:
            state["minute_window"] = current_minute
            state["minute_attempts"] = 0
        return state

    @staticmethod
    def _public(state: dict, minute_cap: int, monthly_cap: int, now: float) -> dict:
        return {
            "policy": "local_attempts_96_percent_of_current_free_quota",
            "month_utc": state["month_utc"],
            "monthly_attempts_reserved": state["month_attempts"],
            "monthly_attempt_cap": monthly_cap,
            "minute_window_utc": state["minute_window"],
            "minute_attempts_reserved": state["minute_attempts"],
            "minute_attempt_cap": minute_cap,
            "next_allowed_at_epoch": round(float(state["next_allowed_at"]), 3),
            "next_allowed_in_seconds": round(
                max(0.0, float(state["next_allowed_at"]) - float(now)), 3
            ),
        }

    @staticmethod
    def _read(connection, provider: str):
        return connection.execute(
            """SELECT month_utc,month_attempts,minute_window,minute_attempts,
                      next_allowed_at,failure_count,last_status
                 FROM provider_quota WHERE provider=?""",
            (provider,),
        ).fetchone()

    @staticmethod
    def _write(connection, state: dict) -> None:
        connection.execute(
            """INSERT INTO provider_quota(
                   provider,month_utc,month_attempts,minute_window,minute_attempts,
                   next_allowed_at,failure_count,last_status
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(provider) DO UPDATE SET
                   month_utc=excluded.month_utc,
                   month_attempts=excluded.month_attempts,
                   minute_window=excluded.minute_window,
                   minute_attempts=excluded.minute_attempts,
                   next_allowed_at=excluded.next_allowed_at,
                   failure_count=excluded.failure_count,
                   last_status=excluded.last_status""",
            (
                state["provider"],
                state["month_utc"],
                state["month_attempts"],
                state["minute_window"],
                state["minute_attempts"],
                state["next_allowed_at"],
                state["failure_count"],
                state["last_status"],
            ),
        )

    def reserve(
        self,
        provider: str,
        *,
        now: float,
        minute_cap: int,
        monthly_cap: int,
        minimum_interval_seconds: int,
    ) -> QuotaDecision:
        self._validate(provider, minute_cap, monthly_cap)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._normalize_row(self._read(connection, provider), provider=provider, now=now)
            if state["next_allowed_at"] > now:
                reason = (
                    "refresh_wait"
                    if state["last_status"] in {"success", "reserved", "never"}
                    else "backoff"
                )
                self._write(connection, state)
                connection.execute("COMMIT")
                return QuotaDecision(
                    False,
                    reason,
                    self._public(state, minute_cap, monthly_cap, now),
                )
            if state["month_attempts"] >= monthly_cap:
                self._write(connection, state)
                connection.execute("COMMIT")
                return QuotaDecision(
                    False,
                    "monthly_quota_exhausted",
                    self._public(state, minute_cap, monthly_cap, now),
                )
            if state["minute_attempts"] >= minute_cap:
                state["next_allowed_at"] = max(
                    state["next_allowed_at"],
                    float((state["minute_window"] + 1) * 60),
                )
                state["last_status"] = "minute_rate_limited"
                self._write(connection, state)
                connection.execute("COMMIT")
                return QuotaDecision(
                    False,
                    "minute_rate_limited",
                    self._public(state, minute_cap, monthly_cap, now),
                )
            state["month_attempts"] += 1
            state["minute_attempts"] += 1
            state["next_allowed_at"] = float(now) + int(minimum_interval_seconds)
            state["last_status"] = "reserved"
            self._write(connection, state)
            connection.execute("COMMIT")
            return QuotaDecision(
                True,
                "reserved",
                self._public(state, minute_cap, monthly_cap, now),
            )
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise QuotaLedgerError("quota reservation failed closed") from exc
        finally:
            connection.close()

    def mark_success(
        self,
        provider: str,
        *,
        now: float,
        minute_cap: int,
        monthly_cap: int,
        minimum_interval_seconds: int,
    ) -> dict:
        return self._mark(
            provider,
            now=now,
            minute_cap=minute_cap,
            monthly_cap=monthly_cap,
            status="success",
            delay=minimum_interval_seconds,
            failure=False,
        )

    def mark_failure(
        self,
        provider: str,
        *,
        now: float,
        minute_cap: int,
        monthly_cap: int,
        status: str,
        minimum_interval_seconds: int,
        retry_after_seconds: int | None = None,
    ) -> dict:
        self._validate(provider, minute_cap, monthly_cap)
        allowed = {
            "auth_error",
            "plan_error",
            "rate_limited",
            "provider_error",
            "transport_error",
            "payload_error",
            "http_error",
        }
        if status not in allowed:
            raise QuotaLedgerError("unsupported provider failure status")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._normalize_row(self._read(connection, provider), provider=provider, now=now)
            failures = state["failure_count"] + 1
            if status in {"auth_error", "plan_error"}:
                delay = 3600
            elif status == "rate_limited":
                delay = max(60, int(retry_after_seconds or minimum_interval_seconds))
            else:
                delay = min(3600, max(minimum_interval_seconds, 300 * (2 ** min(failures - 1, 4))))
            state["failure_count"] = failures
            state["last_status"] = status
            state["next_allowed_at"] = max(state["next_allowed_at"], float(now) + delay)
            self._write(connection, state)
            connection.execute("COMMIT")
            return self._public(state, minute_cap, monthly_cap, now)
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise QuotaLedgerError("quota failure state could not be persisted") from exc
        finally:
            connection.close()

    def _mark(
        self,
        provider: str,
        *,
        now: float,
        minute_cap: int,
        monthly_cap: int,
        status: str,
        delay: int,
        failure: bool,
    ) -> dict:
        self._validate(provider, minute_cap, monthly_cap)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._normalize_row(self._read(connection, provider), provider=provider, now=now)
            state["failure_count"] = state["failure_count"] + 1 if failure else 0
            state["last_status"] = status
            state["next_allowed_at"] = max(state["next_allowed_at"], float(now) + int(delay))
            self._write(connection, state)
            connection.execute("COMMIT")
            return self._public(state, minute_cap, monthly_cap, now)
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise QuotaLedgerError("quota state update failed") from exc
        finally:
            connection.close()

    def status(
        self,
        provider: str,
        *,
        now: float,
        minute_cap: int,
        monthly_cap: int,
    ) -> dict:
        self._validate(provider, minute_cap, monthly_cap)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._normalize_row(self._read(connection, provider), provider=provider, now=now)
            self._write(connection, state)
            connection.execute("COMMIT")
            return self._public(state, minute_cap, monthly_cap, now)
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise QuotaLedgerError("quota status failed closed") from exc
        finally:
            connection.close()


@dataclass
class ProviderSpec:
    name: str
    enabled: bool
    credential_present: bool
    client: Any = field(repr=False)
    minute_cap: int = 1
    monthly_cap: int = 1


def _finite(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    if minimum is not None and number < minimum:
        raise ValueError
    if maximum is not None and number > maximum:
        raise ValueError
    return number


def _sanitize_cached(provider: str, record: Any) -> dict | None:
    if not isinstance(record, dict) or set(record) != {"fetched_at_epoch", "data"}:
        return None
    data = record.get("data")
    expected = {
        "identity",
        "price_usd",
        "market_cap_usd",
        "volume_24h_usd",
        "percent_change_24h",
        "source_updated_at_epoch",
    }
    if not isinstance(data, dict) or set(data) != expected:
        return None
    identity = data.get("identity")
    wanted = (
        {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin"}
        if provider == "coingecko"
        else {"id": 1, "symbol": "BTC", "name": "Bitcoin"}
    )
    if identity != wanted:
        return None
    try:
        return {
            "fetched_at_epoch": _finite(record["fetched_at_epoch"], minimum=1),
            "data": {
                "identity": dict(wanted),
                "price_usd": _finite(data["price_usd"], minimum=0.00000001),
                "market_cap_usd": _finite(data["market_cap_usd"], minimum=0),
                "volume_24h_usd": _finite(data["volume_24h_usd"], minimum=0),
                "percent_change_24h": _finite(
                    data["percent_change_24h"], minimum=-100, maximum=1_000_000
                ),
                "source_updated_at_epoch": _finite(
                    data["source_updated_at_epoch"], minimum=1
                ),
            },
        }
    except (TypeError, ValueError, OverflowError):
        return None


class ExternalContextManager:
    def __init__(
        self,
        specs: list[ProviderSpec],
        *,
        ledger: QuotaLedger | None,
        cache_path: str | Path,
        refresh_seconds: int = 300,
        stale_after_seconds: int = 900,
        ledger_error: bool = False,
    ):
        self.specs = {spec.name: spec for spec in specs}
        if set(self.specs) != ALLOWED_PROVIDERS:
            raise ValueError("exactly CoinGecko and CoinMarketCap specs are required")
        self.ledger = ledger
        self.ledger_error = bool(ledger_error)
        self.cache_path = Path(cache_path)
        self.refresh_seconds = int(refresh_seconds)
        self.stale_after_seconds = int(stale_after_seconds)
        self._cache = self._load_cache()

    @classmethod
    def from_env(cls):
        refresh = env_int("EXTERNAL_MARKET_REFRESH_SECONDS", 300, 300, 3600)
        stale_after = env_int("EXTERNAL_MARKET_STALE_AFTER_SECONDS", 900, refresh, 86_400)
        timeout = env_float("EXTERNAL_MARKET_HTTP_TIMEOUT_SECONDS", 10, 1, 30)
        cg_enabled = env_choice(
            "COINGECKO_CONTEXT_ENABLED", "false", {"true", "false"}
        ) == "true"
        cmc_enabled = env_choice(
            "COINMARKETCAP_CONTEXT_ENABLED", "false", {"true", "false"}
        ) == "true"
        cg_key = str(os.getenv("COINGECKO_API_KEY", "") or "").strip()
        cmc_key = str(os.getenv("COINMARKETCAP_API_KEY", "") or "").strip()
        specs = [
            ProviderSpec(
                "coingecko",
                cg_enabled,
                bool(cg_key),
                CoinGeckoClient(cg_key, timeout=timeout),
                env_int(
                    "COINGECKO_MAX_REQUESTS_PER_MINUTE",
                    COINGECKO_SAFE_MINUTE,
                    1,
                    COINGECKO_SAFE_MINUTE,
                ),
                env_int(
                    "COINGECKO_MAX_MONTHLY_ATTEMPTS",
                    COINGECKO_SAFE_MONTHLY,
                    1,
                    COINGECKO_SAFE_MONTHLY,
                ),
            ),
            ProviderSpec(
                "coinmarketcap",
                cmc_enabled,
                bool(cmc_key),
                CoinMarketCapClient(cmc_key, timeout=timeout),
                env_int(
                    "COINMARKETCAP_MAX_REQUESTS_PER_MINUTE",
                    COINMARKETCAP_SAFE_MINUTE,
                    1,
                    COINMARKETCAP_SAFE_MINUTE,
                ),
                env_int(
                    "COINMARKETCAP_MAX_MONTHLY_ATTEMPTS",
                    COINMARKETCAP_SAFE_MONTHLY,
                    1,
                    COINMARKETCAP_SAFE_MONTHLY,
                ),
            ),
        ]
        ledger = None
        ledger_error = False
        if any(spec.enabled and spec.credential_present for spec in specs):
            try:
                ledger = QuotaLedger(
                    os.getenv(
                        "EXTERNAL_MARKET_QUOTA_DB",
                        str(RUNTIME / "external_market_quota.sqlite3"),
                    )
                )
            except QuotaLedgerError:
                ledger_error = True
        return cls(
            specs,
            ledger=ledger,
            ledger_error=ledger_error,
            cache_path=os.getenv(
                "EXTERNAL_MARKET_CACHE_FILE",
                str(MONEYFLOW_FILE.parent / "external_market_cache.json"),
            ),
            refresh_seconds=refresh,
            stale_after_seconds=stale_after,
        )

    def _load_cache(self) -> dict:
        try:
            _safe_storage_path(self.cache_path, maximum_bytes=MAX_CACHE_BYTES)
            if not self.cache_path.exists():
                return {}
            with self.cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                return {}
            rows = payload.get("providers")
            if not isinstance(rows, dict):
                return {}
            return {
                provider: clean
                for provider in ALLOWED_PROVIDERS
                if (clean := _sanitize_cached(provider, rows.get(provider))) is not None
            }
        except (OSError, ValueError, json.JSONDecodeError, QuotaLedgerError):
            return {}

    def _save_cache(self) -> bool:
        try:
            _safe_storage_path(self.cache_path, maximum_bytes=MAX_CACHE_BYTES)
            atomic_write_json(
                self.cache_path,
                {"schema_version": 1, "providers": self._cache},
                mode=0o640,
            )
            return True
        except (OSError, QuotaLedgerError):
            return False

    @staticmethod
    def _empty_quota(spec: ProviderSpec) -> dict:
        return {
            "policy": "local_attempts_96_percent_of_current_free_quota",
            "month_utc": None,
            "monthly_attempts_reserved": None,
            "monthly_attempt_cap": spec.monthly_cap,
            "minute_window_utc": None,
            "minute_attempts_reserved": None,
            "minute_attempt_cap": spec.minute_cap,
            "next_allowed_at_epoch": None,
            "next_allowed_in_seconds": None,
        }

    def _quota_status(self, spec: ProviderSpec, now: float) -> dict:
        if self.ledger is None:
            return self._empty_quota(spec)
        return self.ledger.status(
            spec.name,
            now=now,
            minute_cap=spec.minute_cap,
            monthly_cap=spec.monthly_cap,
        )

    def _result(
        self,
        spec: ProviderSpec,
        *,
        now: float,
        status: str,
        reason: str | None = None,
        quota: dict | None = None,
        ignore_cache: bool = False,
    ) -> dict:
        out = {
            "status": status,
            "enabled": spec.enabled,
            "available": False,
            "fresh": False,
            "advisory_only": True,
            "quota": quota or self._empty_quota(spec),
        }
        if reason:
            out["reason"] = reason
        cached = None if ignore_cache else self._cache.get(spec.name)
        if cached:
            age = max(0.0, float(now) - float(cached["fetched_at_epoch"]))
            out.update({
                "available": True,
                "fresh": age <= self.stale_after_seconds,
                "cache_age_seconds": round(age, 3),
                "fetched_at_epoch": cached["fetched_at_epoch"],
                "data": cached["data"],
            })
            if age > self.stale_after_seconds:
                out["status"] = "stale"
        return out

    def _provider(self, spec: ProviderSpec, now: float) -> dict:
        if not spec.enabled:
            return self._result(
                spec,
                now=now,
                status="disabled",
                reason="provider_disabled",
                ignore_cache=True,
            )
        if not spec.credential_present:
            return self._result(
                spec,
                now=now,
                status="missing_key",
                reason="enabled_provider_has_no_api_key",
                ignore_cache=True,
            )

        cached = self._cache.get(spec.name)
        if cached:
            age = max(0.0, now - float(cached["fetched_at_epoch"]))
            if age < self.refresh_seconds:
                try:
                    quota = self._quota_status(spec, now)
                except QuotaLedgerError:
                    quota = self._empty_quota(spec)
                return self._result(spec, now=now, status="cached", quota=quota)

        if self.ledger_error or self.ledger is None:
            return self._result(
                spec,
                now=now,
                status="storage_error",
                reason="quota_ledger_unavailable_fail_closed",
            )
        try:
            decision = self.ledger.reserve(
                spec.name,
                now=now,
                minute_cap=spec.minute_cap,
                monthly_cap=spec.monthly_cap,
                minimum_interval_seconds=self.refresh_seconds,
            )
        except QuotaLedgerError:
            self.ledger_error = True
            return self._result(
                spec,
                now=now,
                status="storage_error",
                reason="quota_reservation_failed_closed",
            )
        if not decision.allowed:
            return self._result(
                spec,
                now=now,
                status=decision.reason,
                reason=decision.reason,
                quota=decision.quota,
            )

        try:
            data = spec.client.fetch_bitcoin_usd()
        except ProviderHTTPError as exc:
            if exc.status_code in {401, 403}:
                status = "auth_error"
            elif exc.status_code == 402:
                status = "plan_error"
            elif exc.status_code == 429:
                status = "rate_limited"
            elif exc.status_code >= 500:
                status = "provider_error"
            else:
                status = "http_error"
            try:
                quota = self.ledger.mark_failure(
                    spec.name,
                    now=now,
                    minute_cap=spec.minute_cap,
                    monthly_cap=spec.monthly_cap,
                    status=status,
                    minimum_interval_seconds=self.refresh_seconds,
                    retry_after_seconds=exc.retry_after_seconds,
                )
            except QuotaLedgerError:
                self.ledger_error = True
                quota = decision.quota
            return self._result(
                spec,
                now=now,
                status=status,
                reason=status,
                quota=quota,
            )
        except ProviderTransportError:
            status = "transport_error"
        except ProviderPayloadError:
            status = "payload_error"
        except Exception:
            # A provider adapter must never break Binance context collection.
            status = "provider_error"
        else:
            status = ""
        if status:
            try:
                quota = self.ledger.mark_failure(
                    spec.name,
                    now=now,
                    minute_cap=spec.minute_cap,
                    monthly_cap=spec.monthly_cap,
                    status=status,
                    minimum_interval_seconds=self.refresh_seconds,
                )
            except QuotaLedgerError:
                self.ledger_error = True
                quota = decision.quota
            return self._result(
                spec,
                now=now,
                status=status,
                reason=status,
                quota=quota,
            )

        # Success path.  Only normalized data reaches the cache/snapshot.
        self._cache[spec.name] = {"fetched_at_epoch": now, "data": data}
        cache_persisted = self._save_cache()
        try:
            quota = self.ledger.mark_success(
                spec.name,
                now=now,
                minute_cap=spec.minute_cap,
                monthly_cap=spec.monthly_cap,
                minimum_interval_seconds=self.refresh_seconds,
            )
        except QuotaLedgerError:
            self.ledger_error = True
            quota = decision.quota
        result = self._result(spec, now=now, status="fresh", quota=quota)
        result["cache_persisted"] = cache_persisted
        return result

    def snapshot(self, *, now: float | None = None) -> dict:
        epoch = time.time() if now is None else float(now)
        providers = {
            name: self._provider(self.specs[name], epoch)
            for name in ("coingecko", "coinmarketcap")
        }
        return {
            "schema_version": 1,
            "generated_at_epoch": epoch,
            "advisory_only": True,
            "affects_entry_decision": False,
            "base_asset": "BTC",
            "quote_currency": "USD",
            "providers": providers,
            "attribution": {
                "coingecko": {
                    "text": "Data provided by CoinGecko",
                    "url": "https://www.coingecko.com/en/api",
                },
                "coinmarketcap": {
                    "text": "CoinMarketCap",
                    "url": "https://coinmarketcap.com/api/",
                },
            },
        }
