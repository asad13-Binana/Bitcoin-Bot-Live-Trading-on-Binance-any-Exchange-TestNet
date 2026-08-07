from __future__ import annotations
"""Narrow authenticated Binance Spot gateway.

No margin, futures, transfer or withdrawal method exists in this class.
"""

import logging
import os
import time

import requests

from services.common.config_bounds import env_float, env_int


log = logging.getLogger("bitcoin-spot-gateway")


class SubmissionClass:
    DEFINITE_REJECT = "DEFINITE_REJECT"
    AMBIGUOUS = "AMBIGUOUS"


class BinanceSpotGateway:
    def __init__(self, *, testnet: bool):
        key, secret = os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_API_SECRET", "")
        if not key or not secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET are required")
        try:
            from binance.client import Client
            from binance.exceptions import (
                BinanceAPIException, BinanceOrderException, BinanceRequestException,
            )
        except ImportError as exc:
            raise RuntimeError("python-binance is required by the execution sidecar") from exc
        self.api_exception = BinanceAPIException
        self.order_exception = BinanceOrderException
        self.request_exception = BinanceRequestException
        self.http_timeout = env_float(
            "BINANCE_HTTP_TIMEOUT_SECONDS", 10, 1, 30
        )
        # Binance recommends a signed-request recvWindow of 5000 ms or less.
        self.recv_window_ms = env_int(
            "BINANCE_RECV_WINDOW_MS", 5000, 1000, 5000
        )
        self.max_time_sync_rtt_ms = env_int(
            "BINANCE_TIME_SYNC_MAX_RTT_MS", 2000, 100, 5000
        )
        self.client = Client(
            key,
            secret,
            requests_params={"timeout": self.http_timeout},
            testnet=bool(testnet),
        )
        self.client.REQUEST_TIMEOUT = self.http_timeout
        self.client.REQUEST_RECVWINDOW = self.recv_window_ms
        self.testnet = bool(testnet)
        self.last_limit_usage: dict[str, str] = {}
        self._install_weight_hook()
        self._sync_server_time()

    def _sync_server_time(self) -> None:
        """Set the signed-request clock offset using an RTT midpoint sample."""
        started_ns = time.time_ns()
        payload = self.client.get_server_time()
        finished_ns = time.time_ns()
        if finished_ns < started_ns:
            raise RuntimeError("system monotonicity failure during Binance time sync")
        rtt_ms = (finished_ns - started_ns) / 1_000_000
        if rtt_ms > self.max_time_sync_rtt_ms:
            raise RuntimeError(
                f"Binance time-sync RTT {rtt_ms:.1f} ms exceeds "
                f"{self.max_time_sync_rtt_ms} ms"
            )
        if not isinstance(payload, dict):
            raise RuntimeError("Binance server-time response is malformed")
        try:
            server_ms = int(payload["serverTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Binance server-time response lacks a valid serverTime") from exc
        if server_ms <= 0:
            raise RuntimeError("Binance serverTime must be positive")
        midpoint_ms = ((started_ns + finished_ns) // 2) // 1_000_000
        self.client.timestamp_offset = server_ms - midpoint_ms
        log.info(
            "Binance signed-request clock synchronized (offset=%d ms, RTT=%.1f ms)",
            self.client.timestamp_offset,
            rtt_ms,
        )

    def signed_timestamp_ms(self) -> int:
        """Return a current millisecond timestamp using the synchronized offset."""
        return int(time.time() * 1000 + int(self.client.timestamp_offset))

    def _install_weight_hook(self):
        try:
            session = self.client.session
            hooks = session.hooks.get("response") or []
            if not isinstance(hooks, list):
                hooks = [hooks]

            def record(response, *_args, **_kwargs):
                try:
                    limits = {
                        str(name): str(value)
                        for name, value in response.headers.items()
                        if str(name).upper().startswith((
                            "X-MBX-USED-WEIGHT-", "X-MBX-ORDER-COUNT-"
                        )) or str(name).upper() == "RETRY-AFTER"
                    }
                    if limits:
                        self.last_limit_usage = limits
                        log.debug("Binance rate-limit headers: %s", limits)
                    if int(response.status_code) in (418, 429):
                        log.warning(
                            "Binance returned HTTP %s; authenticated POSTs are not "
                            "retried automatically (Retry-After=%s)",
                            response.status_code,
                            response.headers.get("Retry-After"),
                        )
                except Exception:
                    pass
                return response

            hooks.append(record)
            session.hooks["response"] = hooks
        except Exception:
            pass

    def classify_submission_error(self, exc: Exception) -> str:
        """Classify a POST failure without ever guessing that it was rejected.

        Only explicit Binance validation/auth/order-rejection responses are
        definite. Transport failures, malformed responses, server failures and
        unknown exception types may follow an accepted matching-engine request,
        so they remain AMBIGUOUS until a deterministic client-ID lookup proves
        the outcome.
        """
        if isinstance(exc, (requests.RequestException, self.request_exception)):
            return SubmissionClass.AMBIGUOUS
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        try:
            status = int(status) if status is not None else None
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            return SubmissionClass.AMBIGUOUS
        if status in (418, 429) or (status is not None and status >= 500):
            return SubmissionClass.AMBIGUOUS
        if code in (-1006, -1007):
            return SubmissionClass.AMBIGUOUS
        if isinstance(exc, self.order_exception):
            return SubmissionClass.DEFINITE_REJECT
        definite_codes = {
            -1013, -1015, -1021, -1022, -2010, -2011, -2014, -2015,
            -2021, -2022,
        }
        if isinstance(exc, self.api_exception) and (
            code in definite_codes or (code is not None and -1135 <= code <= -1100)
            or status in (401, 403)
        ):
            return SubmissionClass.DEFINITE_REJECT
        return SubmissionClass.AMBIGUOUS

    def symbol_info(self, symbol: str) -> dict:
        info = self.client.get_exchange_info()
        rows = [row for row in (info.get("symbols") or []) if row.get("symbol") == symbol]
        if len(rows) != 1:
            raise RuntimeError(f"exchangeInfo did not return exactly one {symbol} row")
        return rows[0]

    def ticker_price(self, symbol: str):
        return self.client.get_symbol_ticker(symbol=symbol)

    def order_book(self, symbol: str, limit: int = 5):
        return self.client.get_order_book(symbol=symbol, limit=limit)

    def account(self):
        return self.client.get_account()

    def open_orders(self, symbol: str | None = None):
        return self.client.get_open_orders(**({"symbol": symbol} if symbol else {}))

    def open_order_lists(self):
        return self.client._get("openOrderList", True, data={})

    def get_order(self, symbol: str, *, order_id=None, client_id: str = ""):
        data = {"symbol": symbol}
        if order_id is not None:
            data["orderId"] = order_id
        elif client_id:
            data["origClientOrderId"] = client_id
        else:
            raise ValueError("order_id or client_id is required")
        return self.client.get_order(**data)

    def get_order_list(self, *, order_list_id=None, list_client_id: str = ""):
        if order_list_id is not None:
            data = {"orderListId": order_list_id}
        elif list_client_id:
            # Official GET orderList uses origClientOrderId for listClientOrderId lookup.
            data = {"origClientOrderId": list_client_id}
        else:
            raise ValueError("order_list_id or list_client_id is required")
        return self.client._get("orderList", True, data=data)

    def place(self, endpoint: str, params: dict):
        if endpoint == "order":
            return self.client.create_order(**params)
        if endpoint not in {"orderList/oto", "orderList/otoco", "orderList/oco"}:
            raise ValueError(f"unsupported Spot placement endpoint: {endpoint}")
        return self.client._post(endpoint, True, data=params)

    def cancel_order(self, symbol: str, order_id: int):
        return self.client.cancel_order(symbol=symbol, orderId=int(order_id))

    def cancel_order_list(self, symbol: str, order_list_id: int):
        return self.client._delete("orderList", True, data={
            "symbol": symbol, "orderListId": int(order_list_id)})
