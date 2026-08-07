from __future__ import annotations
"""Central redaction for audit, health, and owner-visible diagnostics."""

import os
import re
from collections.abc import Mapping


REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|api_?secret|secret|signature|token|password|passwd|"
    r"authorization|listen_?key|private_?key|hmac_?key)(?:$|_)", re.IGNORECASE)
_INLINE = re.compile(
    r"(?i)(\b(?:signature|api[_-]?key|api[_-]?secret|secret|token|password|"
    r"authorization|listen[_-]?key)\b\s*[:=]\s*)([^\s,;\"'&}]+)")
_QUOTED_KV = re.compile(
    r"(?i)([\"'](?:api[_-]?key|api[_-]?secret|secret|signature|token|password|"
    r"authorization|listen[_-]?key|hmac[_-]?key|private[_-]?key)[\"']\s*:\s*)"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;}]+)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_TELEGRAM_URL = re.compile(r"(?i)(api\.telegram\.org/bot)[^/\s]+")


def _configured_secrets() -> tuple[str, ...]:
    names = (
        "BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN",
        "FREQTRADE_API_PASSWORD", "FREQTRADE_API_JWT_SECRET",
        "FREQTRADE_API_WS_TOKEN", "MONITOR_API_TOKEN", "COMMAND_HMAC_KEY",
        "SIGNAL_HMAC_KEY", "LIVE_EVIDENCE_PRIVATE_KEY", "COINGECKO_API_KEY",
        "COINMARKETCAP_API_KEY",
    )
    values = {str(os.getenv(name, "")) for name in names}
    return tuple(sorted((value for value in values if len(value) >= 8), key=len, reverse=True))


def redact_text(value) -> str:
    text = str(value)
    for secret in _configured_secrets():
        text = text.replace(secret, REDACTED)
    text = _TELEGRAM_URL.sub(r"\1" + REDACTED, text)
    text = _BEARER.sub(r"\1" + REDACTED, text)
    text = _QUOTED_KV.sub(r'\1"' + REDACTED + '"', text)
    return _INLINE.sub(r"\1" + REDACTED, text)


def redact(value):
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
