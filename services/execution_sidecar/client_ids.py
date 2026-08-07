from __future__ import annotations
"""Collision-resistant Binance client-order identifiers."""

import re
import secrets
import threading
import time


_SAFE = re.compile(r"^[A-Z0-9_-]{1,12}$")


class ClientOrderIds:
    def __init__(self, prefix: str = "BTCB"):
        prefix = str(prefix).upper()
        if not _SAFE.fullmatch(prefix):
            raise ValueError("client-order prefix must match [A-Z0-9_-]{1,12}")
        self.prefix = prefix
        self._counter = 0
        self._lock = threading.Lock()

    def new(self, role: str) -> str:
        role = str(role).upper()
        if not _SAFE.fullmatch(role):
            raise ValueError("client-order role must match [A-Z0-9_-]{1,12}")
        with self._lock:
            self._counter = (self._counter + 1) & 0xFFFF
            counter = self._counter
        # Random entropy is primary; clock/counter aid operations and fixed-clock tests.
        stamp = int(time.time_ns()) & 0xFFFFFFFF
        random_part = secrets.token_hex(6).upper()
        value = f"{self.prefix}-{role}-{stamp:08X}{counter:04X}{random_part}"
        return value[:36]


def looks_owned(value: str, prefix: str = "BTCB") -> bool:
    """Diagnostic only; the durable intent ledger is the ownership authority."""
    return str(value or "").startswith(str(prefix).upper() + "-")
