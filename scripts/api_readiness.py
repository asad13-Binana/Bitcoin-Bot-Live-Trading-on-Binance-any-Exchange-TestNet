#!/usr/bin/env python3
"""Credential-gated, read-only API readiness probe for an Oracle deployment.

The probe authenticates to Binance and Telegram without placing, cancelling,
or modifying an order and without sending a Telegram message.  Optional
Bitcoin-only market-data providers are checked only when enabled.  Its JSON
output is intentionally limited to booleans, timings, and counts: credentials,
balances, account identifiers, chat identifiers, response bodies, and order
identifiers are never emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
MAX_RESPONSE_BYTES = 64 * 1024
SYMBOL = "BTCUSDT"
TESTNET_BASE = "https://testnet.binance.vision"
LIVE_BASE = "https://api.binance.com"
LIVE_CONFIRMATION = "LIVE_READ_ONLY_NO_ORDERS"


class ReadinessError(RuntimeError):
    """A deliberately redacted readiness failure."""


class _NoRedirect(HTTPRedirectHandler):
    """Prevent credentials from following a provider redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


def _required(env: dict[str, str], name: str, minimum: int = 1) -> str:
    value = str(env.get(name, ""))
    if len(value) < minimum:
        raise ReadinessError(f"{name} is missing or too short")
    upper = value.upper()
    if any(marker in upper for marker in ("REPLACE", "CHANGEME", "CHANGE_ME")):
        raise ReadinessError(f"{name} still contains a public placeholder")
    return value


def _boolean(env: dict[str, str], name: str, default: str = "false") -> bool:
    value = str(env.get(name, default)).strip().lower()
    if value not in {"true", "false"}:
        raise ReadinessError(f"{name} must be true or false")
    return value == "true"


def _integer(
    env: dict[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(str(env.get(name, default)))
    except ValueError as exc:
        raise ReadinessError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ReadinessError(f"{name} is outside its safe range")
    return value


def _release_mode(path: Path = ROOT / "RELEASE_MODE") -> str:
    try:
        mode = path.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise ReadinessError("RELEASE_MODE is unreadable") from exc
    if mode not in {"testnet", "live"}:
        raise ReadinessError("RELEASE_MODE is invalid")
    return mode


class ApiReadinessProbe:
    """Run bounded GET-only checks against fixed API identities."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        opener: Any | None = None,
        release_mode: str | None = None,
        live_confirmation: str = "",
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self.opener = opener or build_opener(_NoRedirect)
        self.release_mode = release_mode or _release_mode()
        self.live_confirmation = str(live_confirmation or "")
        self.timeout = _integer(
            self.env, "BINANCE_HTTP_TIMEOUT_SECONDS", 10, 1, 30
        )
        self.recv_window = _integer(
            self.env, "BINANCE_RECV_WINDOW_MS", 5000, 1000, 5000
        )
        self.max_rtt_ms = _integer(
            self.env, "BINANCE_TIME_SYNC_MAX_RTT_MS", 2000, 100, 5000
        )
        self.base = self._validate_identity()
        self.api_key = _required(self.env, "BINANCE_API_KEY", 16)
        self.api_secret = _required(self.env, "BINANCE_API_SECRET", 16)
        self.telegram_token = _required(self.env, "TELEGRAM_BOT_TOKEN", 24)
        self.owner_chat_id = _required(self.env, "TELEGRAM_OWNER_CHAT_ID")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", self.telegram_token):
            raise ReadinessError("TELEGRAM_BOT_TOKEN has invalid BotFather format")
        if not re.fullmatch(r"-?[0-9]+", self.owner_chat_id):
            raise ReadinessError("TELEGRAM_OWNER_CHAT_ID must be numeric")

    def _validate_identity(self) -> str:
        execution = str(self.env.get("EXECUTION_MODE", "simulation")).lower()
        bot_environment = str(self.env.get("BOT_ENVIRONMENT", "")).upper()
        if _boolean(self.env, "LIVE_TRADING_ENABLED"):
            raise ReadinessError(
                "API readiness requires LIVE_TRADING_ENABLED=false"
            )
        configured_base = str(self.env.get("BINANCE_REST_BASE", "")).rstrip("/")
        if self.release_mode == "testnet":
            if execution != "testnet" or bot_environment != "TESTNET":
                raise ReadinessError(
                    "TestNet API readiness requires EXECUTION_MODE=testnet and "
                    "BOT_ENVIRONMENT=TESTNET"
                )
            expected = TESTNET_BASE
        elif self.release_mode == "live":
            if self.live_confirmation != LIVE_CONFIRMATION:
                raise ReadinessError(
                    "LIVE API authentication is blocked without the exact "
                    "read-only confirmation"
                )
            if execution != "simulation" or bot_environment != "LIVE":
                raise ReadinessError(
                    "LIVE read-only API readiness requires EXECUTION_MODE=simulation "
                    "and BOT_ENVIRONMENT=LIVE"
                )
            expected = LIVE_BASE
        else:
            raise ReadinessError("unsupported release mode")
        if configured_base and configured_base != expected:
            raise ReadinessError("BINANCE_REST_BASE does not match package identity")
        return expected

    def _request_json(
        self,
        service: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | list:
        query = urlencode(params or {})
        target = url + ("?" + query if query else "")
        request = Request(
            target,
            headers=headers or {"Accept": "application/json"},
            method="GET",
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            raise ReadinessError(f"{service} returned HTTP {exc.code}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise ReadinessError(f"{service} request failed") from exc
        try:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ReadinessError(f"{service} response exceeded the size limit")
            status = int(getattr(response, "status", response.getcode()))
            if status != 200:
                raise ReadinessError(f"{service} returned HTTP {status}")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReadinessError(f"{service} returned malformed JSON") from exc
            if not isinstance(payload, (dict, list)):
                raise ReadinessError(f"{service} returned an invalid JSON root")
            return payload
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _signed_params(self, server_time_ms: int, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            **extra,
            "recvWindow": self.recv_window,
            "timestamp": int(server_time_ms),
        }
        query = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return params

    def _binance(self) -> dict[str, dict[str, Any]]:
        started_epoch_ms = time.time_ns() / 1_000_000
        started = time.monotonic_ns()
        time_payload = self._request_json(
            "Binance time", self.base + "/api/v3/time"
        )
        finished = time.monotonic_ns()
        finished_epoch_ms = time.time_ns() / 1_000_000
        try:
            server_time = int(time_payload["serverTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReadinessError("Binance time response is malformed") from exc
        rtt_ms = (finished - started) / 1_000_000
        midpoint_ms = (started_epoch_ms + finished_epoch_ms) / 2
        skew_ms = server_time - midpoint_ms
        if rtt_ms > self.max_rtt_ms:
            raise ReadinessError("Binance time round-trip exceeded the configured limit")
        if abs(skew_ms) > 1000:
            raise ReadinessError("Oracle clock differs from Binance by more than 1000 ms")

        headers = {
            "Accept": "application/json",
            "X-MBX-APIKEY": self.api_key,
        }
        account = self._request_json(
            "Binance account authentication",
            self.base + "/api/v3/account",
            params=self._signed_params(server_time, omitZeroBalances="true"),
            headers=headers,
        )
        if not isinstance(account, dict) or not isinstance(account.get("canTrade"), bool):
            raise ReadinessError("Binance account response is malformed")
        if self.release_mode == "testnet" and account["canTrade"] is not True:
            raise ReadinessError("Binance TestNet account is not enabled for trading")
        symbol = self._request_json(
            "Binance symbol",
            self.base + "/api/v3/exchangeInfo",
            params={"symbol": SYMBOL},
        )
        symbols = symbol.get("symbols") if isinstance(symbol, dict) else None
        if (
            not isinstance(symbols, list)
            or len(symbols) != 1
            or symbols[0].get("symbol") != SYMBOL
            or symbols[0].get("status") != "TRADING"
            or symbols[0].get("isSpotTradingAllowed") is not True
        ):
            raise ReadinessError("Binance BTCUSDT Spot identity is not tradable")
        open_orders = self._request_json(
            "Binance open-order visibility",
            self.base + "/api/v3/openOrders",
            params=self._signed_params(server_time, symbol=SYMBOL),
            headers=headers,
        )
        if not isinstance(open_orders, list):
            raise ReadinessError("Binance open-order response is malformed")
        return {
            "time": {
                "ok": True,
                "rtt_ms": round(rtt_ms, 3),
                "clock_skew_ms": round(skew_ms, 3),
            },
            "account_authentication": {
                "ok": True,
                "account_can_trade": account["canTrade"],
            },
            "btc_spot_identity": {"ok": True, "symbol": SYMBOL},
            "open_order_visibility": {"ok": True, "count": len(open_orders)},
        }

    def _telegram(self) -> dict[str, dict[str, Any]]:
        # Token is intentionally present only in the request URL.  Errors and
        # reports use the service label and never include the URL or body.
        base = f"https://api.telegram.org/bot{self.telegram_token}"
        identity = self._request_json("Telegram bot identity", base + "/getMe")
        result = identity.get("result") if isinstance(identity, dict) else None
        if identity.get("ok") is not True or not isinstance(result, dict):
            raise ReadinessError("Telegram bot identity response is malformed")
        if result.get("is_bot") is not True:
            raise ReadinessError("Telegram credential does not identify a bot")
        chat = self._request_json(
            "Telegram owner chat",
            base + "/getChat",
            params={"chat_id": self.owner_chat_id},
        )
        chat_result = chat.get("result") if isinstance(chat, dict) else None
        if chat.get("ok") is not True or not isinstance(chat_result, dict):
            raise ReadinessError("Telegram owner chat response is malformed")
        if str(chat_result.get("id")) != self.owner_chat_id:
            raise ReadinessError("Telegram owner chat identity mismatch")
        return {
            "bot_identity": {"ok": True},
            "owner_chat_visibility": {"ok": True},
        }

    def _optional_providers(self) -> dict[str, dict[str, Any]]:
        checks: dict[str, dict[str, Any]] = {}
        providers = (
            (
                "coingecko",
                "COINGECKO_CONTEXT_ENABLED",
                "COINGECKO_API_KEY",
                "https://api.coingecko.com/api/v3/simple/price",
                "x-cg-demo-api-key",
            ),
            (
                "coinmarketcap",
                "COINMARKETCAP_CONTEXT_ENABLED",
                "COINMARKETCAP_API_KEY",
                "https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/latest",
                "X-CMC_PRO_API_KEY",
            ),
        )
        timeout = _integer(
            self.env, "EXTERNAL_MARKET_HTTP_TIMEOUT_SECONDS", 10, 1, 30
        )
        for label, enabled_name, key_name, url, header_name in providers:
            if not _boolean(self.env, enabled_name):
                checks[label] = {"ok": True, "skipped": True, "reason": "disabled"}
                continue
            key = _required(self.env, key_name, 16)
            original_timeout = self.timeout
            self.timeout = timeout
            try:
                if label == "coingecko":
                    payload = self._request_json(
                        "CoinGecko Bitcoin quote",
                        url,
                        params={
                            "ids": "bitcoin",
                            "vs_currencies": "usd",
                            "include_last_updated_at": "true",
                        },
                        headers={"Accept": "application/json", header_name: key},
                    )
                    valid = (
                        isinstance(payload, dict)
                        and set(payload) == {"bitcoin"}
                        and isinstance(payload.get("bitcoin"), dict)
                        and isinstance(payload["bitcoin"].get("usd"), (int, float))
                    )
                else:
                    payload = self._request_json(
                        "CoinMarketCap Bitcoin quote",
                        url,
                        params={"id": "1", "convert": "USD"},
                        headers={"Accept": "application/json", header_name: key},
                    )
                    status = payload.get("status") if isinstance(payload, dict) else None
                    rows = payload.get("data") if isinstance(payload, dict) else None
                    row = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
                    quotes = row.get("quote") if isinstance(row, dict) else None
                    quote = (
                        quotes[0]
                        if isinstance(quotes, list) and len(quotes) == 1
                        else None
                    )
                    valid = (
                        isinstance(status, dict)
                        and type(status.get("error_code")) is int
                        and status.get("error_code") == 0
                        and isinstance(row, dict)
                        and row.get("id") == 1
                        and str(row.get("symbol", "")).upper() == "BTC"
                        and str(row.get("slug", "")).lower() == "bitcoin"
                        and str(row.get("name", "")).lower() == "bitcoin"
                        and isinstance(quote, dict)
                        and str(quote.get("symbol", "")).upper() == "USD"
                    )
            finally:
                self.timeout = original_timeout
            if not valid:
                raise ReadinessError(f"{label} returned the wrong asset identity")
            checks[label] = {"ok": True, "skipped": False, "asset": "BTC"}
        return checks

    def run(self) -> dict[str, Any]:
        return {
            "schema": "bitcoin-bot-api-readiness-v1",
            "ok": True,
            "release_mode": self.release_mode,
            "execution_mode": str(self.env.get("EXECUTION_MODE", "")).lower(),
            "safety": {
                "http_methods": ["GET"],
                "orders_submitted": False,
                "telegram_messages_sent": False,
                "secrets_emitted": False,
            },
            "checks": {
                "binance": self._binance(),
                "telegram": self._telegram(),
                "optional_market_data": self._optional_providers(),
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-live-read-only",
        default="",
        metavar="PHRASE",
        help=(
            "required only for the LIVE package; exact phrase: "
            f"{LIVE_CONFIRMATION}"
        ),
    )
    args = parser.parse_args(argv)
    try:
        report = ApiReadinessProbe(
            live_confirmation=args.confirm_live_read_only
        ).run()
    except ReadinessError as exc:
        report = {
            "schema": "bitcoin-bot-api-readiness-v1",
            "ok": False,
            "error": str(exc),
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1
    except Exception:
        # Never print a traceback: request objects can contain the Telegram
        # token in their URL or Binance credentials in headers.
        report = {
            "schema": "bitcoin-bot-api-readiness-v1",
            "ok": False,
            "error": "internal readiness failure; inspect root-only system logs",
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
