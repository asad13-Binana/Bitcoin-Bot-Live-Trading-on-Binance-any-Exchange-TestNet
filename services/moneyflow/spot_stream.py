from __future__ import annotations

"""Credential-free Binance Spot stream with bounded rolling flow windows."""

import json
import logging
import math
import re
import threading
import time
from collections import deque
from decimal import Decimal, InvalidOperation

log = logging.getLogger("spot-market-stream")
WINDOW_SECONDS = (15, 30, 60)
MARKET_STREAM_BASE = "wss://data-stream.binance.vision/stream"


def _decimal(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


class SpotMarketStream:
    """Maintain exact receive-time windows for one approved Binance Spot symbol.

    The stream has no credential, account, or order capability. Sequence gaps
    clear the rolling sample and force a complete warm-up before stream data can
    replace the existing REST fallback.
    """

    def __init__(self, *, stale_after_seconds: int = 10):
        stale = int(stale_after_seconds)
        if not 2 <= stale <= 60:
            raise ValueError("Spot stream stale threshold must be between 2 and 60 seconds")
        self.stale_after_seconds = stale
        self._lock = threading.RLock()
        self._symbol = ""
        self._trades = deque()
        self._book = None
        self._started_monotonic = None
        self._last_message_monotonic = None
        self._last_message_epoch = None
        self._last_aggregate_id = None
        self._duplicates_ignored = 0
        self._sequence_gap_count = 0
        self._malformed_frames = 0
        self._connected = False
        self._reconnect_count = 0
        self._last_error = None
        self._generation = 0
        self._ws = None
        self._stop_event = None

    @staticmethod
    def endpoint(symbol: str) -> str:
        normalized = str(symbol).upper()
        if not re.fullmatch(r"BTC[A-Z0-9]{3,12}", normalized):
            raise ValueError("Spot stream symbol must be one BTC-base Binance symbol")
        stream = normalized.lower()
        return f"{MARKET_STREAM_BASE}?streams={stream}@aggTrade/{stream}@bookTicker"

    def _reset_sample(self, now_monotonic: float) -> None:
        self._trades.clear()
        self._book = None
        self._started_monotonic = float(now_monotonic)
        self._last_message_monotonic = None
        self._last_message_epoch = None
        self._last_aggregate_id = None

    def ensure_symbol(self, symbol: str) -> None:
        normalized = str(symbol).upper()
        endpoint = self.endpoint(normalized)
        with self._lock:
            if self._symbol == normalized and self._stop_event is not None:
                return
            old_event, old_ws = self._stop_event, self._ws
            self._generation += 1
            generation = self._generation
            self._symbol = normalized
            self._connected = False
            self._last_error = None
            self._reset_sample(time.monotonic())
            stop_event = threading.Event()
            self._stop_event = stop_event
        if old_event is not None:
            old_event.set()
        if old_ws is not None:
            try:
                old_ws.close()
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown of a replaced socket
                log.debug("replaced Spot socket close failed: %s", type(exc).__name__)
        thread = threading.Thread(
            target=self._run,
            args=(generation, normalized, endpoint, stop_event),
            daemon=True,
            name=f"binance-spot-market-{normalized.lower()}",
        )
        thread.start()

    def _run(self, generation: int, symbol: str, endpoint: str, stop_event) -> None:
        import websocket

        backoff = 1.0
        first_connection = True
        while not stop_event.is_set():
            app = websocket.WebSocketApp(
                endpoint,
                on_open=lambda ws, initial=first_connection: self._on_open(
                    ws, generation, initial
                ),
                on_message=lambda ws, raw: self._on_message(ws, raw, generation, symbol),
                on_error=lambda ws, error: self._on_error(ws, error, generation),
                on_close=lambda ws, *args: self._on_close(ws, generation, *args),
                on_pong=lambda ws, payload: self._on_pong(ws, payload, generation),
            )
            with self._lock:
                if generation != self._generation or stop_event.is_set():
                    return
                self._ws = app
            try:
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001 - reconnect after any transport failure
                self._on_error(app, exc, generation)
            if stop_event.is_set() or generation != self._generation:
                return
            first_connection = False
            with self._lock:
                self._reconnect_count += 1
            if stop_event.wait(min(backoff, 60.0)):
                return
            backoff = min(backoff * 2.0, 60.0)

    def _on_open(self, _ws, generation: int, first_connection: bool = False) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._connected = True
            self._last_error = None
            if not first_connection:
                self._reset_sample(time.monotonic())

    def _on_pong(self, _ws, _payload, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._last_message_monotonic = time.monotonic()
                self._last_message_epoch = time.time()

    def _on_error(self, _ws, error, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._last_error = type(error).__name__
        log.warning("Binance Spot market stream error: %s", type(error).__name__)

    def _on_close(self, _ws, generation: int, *_args) -> None:
        with self._lock:
            if generation == self._generation:
                self._connected = False

    def _on_message(self, _ws, raw, generation: int, symbol: str) -> None:
        try:
            message = json.loads(raw)
            event = message.get("data", message)
            if not isinstance(event, dict) or event.get("s") != symbol:
                raise ValueError("Spot stream frame identity mismatch")
            received_monotonic = time.monotonic()
            received_epoch = time.time()
            if event.get("e") == "aggTrade":
                self.ingest_trade(
                    event,
                    received_monotonic=received_monotonic,
                    received_epoch=received_epoch,
                    generation=generation,
                )
            elif {"b", "B", "a", "A"}.issubset(event):
                self.ingest_book(
                    event,
                    received_monotonic=received_monotonic,
                    received_epoch=received_epoch,
                    generation=generation,
                )
            else:
                raise ValueError("unsupported Spot stream frame")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
            with self._lock:
                if generation == self._generation:
                    self._malformed_frames += 1
                    self._last_error = "malformed_frame"

    def ingest_trade(
        self,
        event: dict,
        *,
        received_monotonic: float | None = None,
        received_epoch: float | None = None,
        generation: int | None = None,
    ) -> bool:
        now_mono = time.monotonic() if received_monotonic is None else float(received_monotonic)
        now_epoch = time.time() if received_epoch is None else float(received_epoch)
        identifier = int(event["a"])
        price = _decimal(event["p"], "aggregate trade price")
        quantity = _decimal(event["q"], "aggregate trade quantity")
        event_time = int(event["T"])
        if identifier < 0 or event_time <= 0 or not math.isfinite(now_mono + now_epoch):
            raise ValueError("aggregate trade identifiers or timestamps are invalid")
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            if event.get("s") not in (None, self._symbol):
                raise ValueError("aggregate trade symbol mismatch")
            if self._started_monotonic is None:
                self._started_monotonic = now_mono
            if self._last_aggregate_id is not None and identifier <= self._last_aggregate_id:
                self._duplicates_ignored += 1
                return False
            if self._last_aggregate_id is not None and identifier != self._last_aggregate_id + 1:
                self._sequence_gap_count += 1
                self._reset_sample(now_mono)
            self._trades.append({
                "id": identifier,
                "received_monotonic": now_mono,
                "event_time_ms": event_time,
                "quote": price * quantity,
                "buyer_is_maker": bool(event.get("m")),
            })
            self._last_aggregate_id = identifier
            self._last_message_monotonic = now_mono
            self._last_message_epoch = now_epoch
            self._prune(now_mono)
            return True

    def ingest_book(
        self,
        event: dict,
        *,
        received_monotonic: float | None = None,
        received_epoch: float | None = None,
        generation: int | None = None,
    ) -> None:
        now_mono = time.monotonic() if received_monotonic is None else float(received_monotonic)
        now_epoch = time.time() if received_epoch is None else float(received_epoch)
        bid, bid_qty = _decimal(event["b"], "best bid"), _decimal(event["B"], "best bid quantity")
        ask, ask_qty = _decimal(event["a"], "best ask"), _decimal(event["A"], "best ask quantity")
        if bid >= ask or not math.isfinite(now_mono + now_epoch):
            raise ValueError("Spot book ticker is crossed or has an invalid timestamp")
        mid = (bid + ask) / 2
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            if event.get("s") not in (None, self._symbol):
                raise ValueError("book ticker symbol mismatch")
            self._book = {
                "best_bid": float(bid),
                "best_ask": float(ask),
                "bid_quantity": float(bid_qty),
                "ask_quantity": float(ask_qty),
                "spread_bps": float((ask - bid) / mid * Decimal(10_000)),
                "received_monotonic": now_mono,
                "received_at_epoch": now_epoch,
            }
            self._last_message_monotonic = now_mono
            self._last_message_epoch = now_epoch

    def _prune(self, now_monotonic: float) -> None:
        floor = float(now_monotonic) - max(WINDOW_SECONDS)
        while self._trades and self._trades[0]["received_monotonic"] < floor:
            self._trades.popleft()

    @staticmethod
    def _window(rows: list[dict]) -> dict:
        buy = sum((row["quote"] for row in rows if not row["buyer_is_maker"]), Decimal(0))
        sell = sum((row["quote"] for row in rows if row["buyer_is_maker"]), Decimal(0))
        total = buy + sell
        return {
            "taker_buy_quote": float(buy),
            "taker_sell_quote": float(sell),
            "taker_buy_ratio": float(buy / total) if total > 0 else 0.0,
            "cvd_quote": float(buy - sell),
            "trade_count": len(rows),
            "first_aggregate_id": rows[0]["id"] if rows else None,
            "last_aggregate_id": rows[-1]["id"] if rows else None,
        }

    def snapshot(self, *, now_monotonic: float | None = None) -> dict:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        with self._lock:
            self._prune(now)
            age = (
                None if self._last_message_monotonic is None
                else max(0.0, now - self._last_message_monotonic)
            )
            book_age = (
                None if self._book is None
                else max(0.0, now - self._book["received_monotonic"])
            )
            uptime = (
                0.0 if self._started_monotonic is None
                else max(0.0, now - self._started_monotonic)
            )
            windows = {}
            for seconds in WINDOW_SECONDS:
                floor = now - seconds
                metrics = self._window([
                    row for row in self._trades if row["received_monotonic"] >= floor
                ])
                metrics["window_seconds"] = seconds
                metrics["ready"] = uptime >= seconds
                windows[str(seconds)] = metrics
            fresh = bool(
                self._connected
                and age is not None
                and age <= self.stale_after_seconds
                and book_age is not None
                and book_age <= self.stale_after_seconds
            )
            selected = dict(windows[str(max(WINDOW_SECONDS))])
            ready = bool(fresh and selected["ready"] and selected["trade_count"] > 0)
            book = None if self._book is None else {
                key: value for key, value in self._book.items()
                if key != "received_monotonic"
            }
            return {
                "source": "binance_spot_websocket",
                "symbol": self._symbol,
                "connected": self._connected,
                "fresh": fresh,
                "ready": ready,
                "last_message_age_seconds": age,
                "last_message_at_epoch": self._last_message_epoch,
                "uptime_seconds": uptime,
                "book_ticker": book,
                "flow": selected,
                "windows": windows,
                "sequence": {
                    "last_aggregate_id": self._last_aggregate_id,
                    "duplicates_ignored": self._duplicates_ignored,
                    "gap_count": self._sequence_gap_count,
                    "malformed_frames": self._malformed_frames,
                },
                "reconnect_count": self._reconnect_count,
                "last_error": self._last_error,
            }

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._connected = False
            event, ws = self._stop_event, self._ws
            self._stop_event = None
            self._ws = None
        if event is not None:
            event.set()
        if ws is not None:
            try:
                ws.close()
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                log.debug("Spot socket close failed: %s", type(exc).__name__)
