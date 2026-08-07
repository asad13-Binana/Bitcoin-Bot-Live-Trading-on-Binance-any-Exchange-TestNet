from __future__ import annotations
"""Public (unauthenticated) Binance Spot market-data client.

This exact-market client handles public API faults explicitly:

  * honors Retry-After on 429 (rate limit) and 418 (IP ban) with bounded,
    jittered backoff and a persistent in-process ban-until latch;
  * retries 5xx/network faults a bounded number of times;
  * never retries 4xx client errors other than 429/418;
  * exposes helpers only for the exact, explicitly selected Bitcoin market.

No authenticated endpoint is ever called from this module and no order can be
placed through it.
"""
import logging
import os
import random
import re
import time

import requests

from services.common.audit import audit
from services.common.config_bounds import env_float, env_int

log = logging.getLogger('binance-public')

DEFAULT_BASE = 'https://api.binance.com'
SPOT_TESTNET_BASE = 'https://testnet.binance.vision'


class BinancePublicError(RuntimeError):
    pass


class BinanceRateLimitError(BinancePublicError):
    def __init__(self, status: int, retry_after: float):
        super().__init__(f'Binance rate limit (HTTP {status}); retry after {retry_after:.0f}s')
        self.status = status
        self.retry_after = retry_after


class BinancePublicClient:
    def __init__(self, base: str | None = None, *, timeout: float | None = None,
                 max_attempts: int | None = None, user_agent: str = 'binance-freqtrade-v101/1.0'):
        self.base = (base or os.getenv('BINANCE_PUBLIC_BASE', DEFAULT_BASE)).rstrip('/')
        self.timeout = timeout if timeout is not None else env_float('HTTP_TIMEOUT_SECONDS', 15, 1, 120)
        self.max_attempts = max_attempts if max_attempts is not None else env_int('PUBLIC_HTTP_MAX_ATTEMPTS', 4, 1, 10)
        self.session = requests.Session()
        self.session.headers['User-Agent'] = user_agent
        self._banned_until = 0.0

    @staticmethod
    def _retry_after_seconds(response, default: float, maximum: float) -> float:
        try:
            value = float(response.headers.get('Retry-After', default))
        except Exception:
            value = default
        return max(1.0, min(value, maximum))

    def get(self, path: str, params: dict | None = None):
        remaining_ban = self._banned_until - time.time()
        if remaining_ban > 0:
            raise BinanceRateLimitError(418, remaining_ban)
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self.session.get(self.base + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
            else:
                status = response.status_code
                if status == 418:
                    retry_after = self._retry_after_seconds(response, 3600, 259_200)
                    self._banned_until = time.time() + retry_after
                    audit('binance_public_ip_ban', severity='CRITICAL',
                          details={'path': path, 'retry_after': retry_after})
                    raise BinanceRateLimitError(418, retry_after)
                if status == 429:
                    retry_after = self._retry_after_seconds(response, 2, 120)
                    audit('binance_public_rate_limited', severity='WARNING',
                          details={'path': path, 'retry_after': retry_after, 'attempt': attempt + 1})
                    if attempt == self.max_attempts - 1:
                        raise BinanceRateLimitError(429, retry_after)
                    time.sleep(retry_after + random.uniform(0, 0.5))
                    continue
                if 500 <= status < 600:
                    last_error = BinancePublicError(f'HTTP {status} from {path}')
                else:
                    response.raise_for_status()
                    return response.json()
            if attempt == self.max_attempts - 1:
                break
            backoff = min(2.0 * (2 ** attempt), 30.0) + random.uniform(0, 0.5)
            log.warning('public GET %s attempt %d failed (%s); backoff %.1fs',
                        path, attempt + 1, last_error, backoff)
            time.sleep(backoff)
        raise BinancePublicError(f'public GET {path} failed after {self.max_attempts} attempts: {last_error}')

    # ---- reference-data helpers ----
    def exchange_info(self, symbol: str | None = None) -> dict:
        if symbol is None:
            return self.get('/api/v3/exchangeInfo', {'showPermissionSets': 'false'})
        symbol = str(symbol or '').upper()
        if not re.fullmatch(r'BTC[A-Z0-9]{2,12}', symbol):
            raise BinancePublicError('exchangeInfo requires one exact BTC quote symbol')
        return self.get('/api/v3/exchangeInfo', {'symbol': symbol})

    def execution_rules(self, symbol: str) -> dict:
        """Return current execution-time rules for one exact BTC Spot symbol."""
        symbol = str(symbol or '').upper()
        if not re.fullmatch(r'BTC[A-Z0-9]{2,12}', symbol):
            raise BinancePublicError('executionRules requires one exact BTC quote symbol')
        data = self.get('/api/v3/executionRules', {'symbol': symbol})
        if not isinstance(data, dict):
            raise BinancePublicError(f'executionRules for {symbol} returned malformed data')
        return data

    def ticker_price(self, symbol: str) -> dict:
        return self.get('/api/v3/ticker/price', {'symbol': symbol})

    # Sentinel used only for the documented no-reference-price fallback.
    NO_REFERENCE_PRICE = object()

    def reference_price(self, symbol: str):
        """Return Binance's current Spot reference price or a fallback sentinel."""
        try:
            data = self.get('/api/v3/referencePrice', {'symbol': symbol})
        except requests.HTTPError as exc:
            response = getattr(exc, 'response', None)
            if response is not None:
                try:
                    body = response.json()
                except ValueError as payload_exc:
                    raise BinancePublicError(
                        f'referencePrice for {symbol} returned malformed error JSON'
                    ) from payload_exc
                if isinstance(body, dict) and body.get('code') == -2043:
                    return self.NO_REFERENCE_PRICE
            raise BinancePublicError(f'referencePrice for {symbol} failed') from exc
        if not isinstance(data, dict):
            raise BinancePublicError(f'referencePrice for {symbol} returned malformed data')
        if data.get('code') == -2043:
            return self.NO_REFERENCE_PRICE
        if 'referencePrice' not in data:
            raise BinancePublicError(f'referencePrice for {symbol} lacks referencePrice')
        value = data.get('referencePrice')
        return self.NO_REFERENCE_PRICE if value is None else value

    def exact_spot_symbol(self, symbol: str) -> dict:
        """Return one exact exchangeInfo row; never enumerate/rank markets."""
        info = self.exchange_info(str(symbol).upper())
        rows = info.get('symbols') if isinstance(info, dict) else None
        if not isinstance(rows, list) or len(rows) != 1:
            raise BinancePublicError(f'exchangeInfo did not return exactly one {symbol} row')
        return rows[0]
