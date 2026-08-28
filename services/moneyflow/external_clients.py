from __future__ import annotations
"""Fixed-identity, read-only BTC market-data clients.

These clients deliberately expose one GET operation each.  They cannot list,
rank, discover, trade, or select assets.  Credentials are sent only in request
headers and are never included in URLs, return values, or exception messages.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import time
from typing import Any

import requests


MAX_RESPONSE_BYTES = 64 * 1024
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
COINMARKETCAP_URL = (
    "https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/latest"
)


class ProviderError(RuntimeError):
    """Safe base exception: messages never contain response bodies or secrets."""


class ProviderTransportError(ProviderError):
    pass


class ProviderPayloadError(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code: int, retry_after_seconds: int | None = None):
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"provider returned HTTP {self.status_code}")


def _retry_after_seconds(value: Any, *, now: float | None = None) -> int | None:
    if value in (None, ""):
        return None
    try:
        seconds = int(str(value).strip())
        return max(0, min(seconds, 86_400))
    except (TypeError, ValueError):
        pass
    try:
        moment = parsedate_to_datetime(str(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        delay = moment.timestamp() - (time.time() if now is None else float(now))
        return max(0, min(math.ceil(delay), 86_400))
    except (TypeError, ValueError, OverflowError):
        return None


def _read_json_response(response) -> dict | list:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    if status != 200:
        raise ProviderHTTPError(
            status,
            _retry_after_seconds(headers.get("Retry-After")),
        )

    length = headers.get("Content-Length")
    if length not in (None, ""):
        try:
            if int(length) > MAX_RESPONSE_BYTES:
                raise ProviderPayloadError("provider response exceeds size limit")
        except ValueError as exc:
            raise ProviderPayloadError("provider returned invalid content length") from exc

    payload = bytearray()
    try:
        chunks = response.iter_content(chunk_size=8192)
    except AttributeError:
        chunks = (getattr(response, "content", b""),)
    for chunk in chunks:
        if not chunk:
            continue
        payload.extend(chunk)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ProviderPayloadError("provider response exceeds size limit")
    try:
        decoded = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderPayloadError("provider returned malformed JSON") from exc
    if not isinstance(decoded, (dict, list)):
        raise ProviderPayloadError("provider response root must be an object or array")
    return decoded


class _JsonTransport:
    def __init__(self, session=None):
        self._session = session or requests.Session()

    def get(self, url: str, *, params: dict, headers: dict, timeout: float) -> dict | list:
        try:
            response = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise ProviderTransportError("provider request failed") from exc
        try:
            return _read_json_response(response)
        finally:
            try:
                response.close()
            except Exception:
                pass


def _number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ProviderPayloadError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderPayloadError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise ProviderPayloadError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise ProviderPayloadError(f"{field} is below its safe range")
    if maximum is not None and result > maximum:
        raise ProviderPayloadError(f"{field} is above its safe range")
    return result


def _iso_epoch(value: Any, field: str) -> float:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        epoch = moment.timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProviderPayloadError(f"{field} must be an ISO-8601 timestamp") from exc
    return _number(epoch, field, minimum=1)


def _normalized_metrics(
    *,
    price: Any,
    market_cap: Any,
    volume_24h: Any,
    percent_change_24h: Any,
    source_updated_at_epoch: Any,
) -> dict:
    return {
        "price_usd": _number(price, "price_usd", minimum=0.00000001),
        "market_cap_usd": _number(market_cap, "market_cap_usd", minimum=0),
        "volume_24h_usd": _number(volume_24h, "volume_24h_usd", minimum=0),
        "percent_change_24h": _number(
            percent_change_24h,
            "percent_change_24h",
            minimum=-100,
            maximum=1_000_000,
        ),
        "source_updated_at_epoch": _number(
            source_updated_at_epoch,
            "source_updated_at_epoch",
            minimum=1,
        ),
    }


class CoinGeckoClient:
    """CoinGecko Demo API client fixed to the `bitcoin` ID and USD."""

    def __init__(self, api_key: str, *, timeout: float = 10, transport=None):
        self._api_key = str(api_key or "")
        self._timeout = float(timeout)
        self._transport = transport or _JsonTransport()

    def fetch_bitcoin_usd(self) -> dict:
        payload = self._transport.get(
            COINGECKO_URL,
            params={
                "ids": "bitcoin",
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
            headers={
                "Accept": "application/json",
                "User-Agent": "bitcoin-bot-external-context/1.0",
                "x-cg-demo-api-key": self._api_key,
            },
            timeout=self._timeout,
        )
        if not isinstance(payload, dict):
            raise ProviderPayloadError("CoinGecko response root must be an object")
        if set(payload) != {"bitcoin"} or not isinstance(payload.get("bitcoin"), dict):
            raise ProviderPayloadError("CoinGecko identity mismatch")
        row = payload["bitcoin"]
        data = _normalized_metrics(
            price=row.get("usd"),
            market_cap=row.get("usd_market_cap"),
            volume_24h=row.get("usd_24h_vol"),
            percent_change_24h=row.get("usd_24h_change"),
            source_updated_at_epoch=row.get("last_updated_at"),
        )
        return {
            "identity": {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin"},
            **data,
        }


class CoinMarketCapClient:
    """CoinMarketCap client fixed to Bitcoin ID 1 and one USD conversion."""

    def __init__(self, api_key: str, *, timeout: float = 10, transport=None):
        self._api_key = str(api_key or "")
        self._timeout = float(timeout)
        self._transport = transport or _JsonTransport()

    def fetch_bitcoin_usd(self) -> dict:
        payload = self._transport.get(
            COINMARKETCAP_URL,
            params={"id": "1", "convert": "USD"},
            headers={
                "Accept": "application/json",
                "User-Agent": "bitcoin-bot-external-context/1.0",
                "X-CMC_PRO_API_KEY": self._api_key,
            },
            timeout=self._timeout,
        )
        # CoinMarketCap v3 wraps its CryptoQuoteV3DTO array in a response object.
        # Do not accept either the obsolete v2 ID-keyed object or the previously
        # assumed bare array: a schema mismatch must keep this optional provider
        # unavailable rather than publish unvalidated market context.
        if not isinstance(payload, dict):
            raise ProviderPayloadError("CoinMarketCap response is malformed")
        status = payload.get("status")
        if (
            not isinstance(status, dict)
            or type(status.get("error_code")) is not int
            or status.get("error_code") != 0
        ):
            raise ProviderPayloadError("CoinMarketCap request status is not successful")
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ProviderPayloadError("CoinMarketCap identity mismatch")
        row = rows[0]
        if not isinstance(row, dict):
            raise ProviderPayloadError("CoinMarketCap Bitcoin row is malformed")
        if (
            row.get("id") != 1
            or str(row.get("symbol", "")).upper() != "BTC"
            or str(row.get("slug", "")).lower() != "bitcoin"
            or str(row.get("name", "")).lower() != "bitcoin"
        ):
            raise ProviderPayloadError("CoinMarketCap identity mismatch")
        quotes = row.get("quote")
        if not isinstance(quotes, list) or len(quotes) != 1:
            raise ProviderPayloadError("CoinMarketCap USD quote mismatch")
        quote = quotes[0]
        if not isinstance(quote, dict):
            raise ProviderPayloadError("CoinMarketCap USD quote is malformed")
        if str(quote.get("symbol", "")).upper() != "USD":
            raise ProviderPayloadError("CoinMarketCap USD quote mismatch")
        data = _normalized_metrics(
            price=quote.get("price"),
            market_cap=quote.get("market_cap"),
            volume_24h=quote.get("volume_24h"),
            percent_change_24h=quote.get("percent_change_24h"),
            source_updated_at_epoch=_iso_epoch(quote.get("last_updated"), "last_updated"),
        )
        return {
            "identity": {"id": 1, "symbol": "BTC", "name": "Bitcoin"},
            **data,
        }
