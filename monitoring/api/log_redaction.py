"""Fail-closed secret redaction for every monitor-facing string and object."""
from __future__ import annotations

import re
from collections.abc import Mapping


_SENSITIVE_KEY = re.compile(
    r"(?i)^(?:.*[_-])?(?:api[_-]?key|api[_-]?secret|secret|token|password|"
    r"passwd|pwd|authorization|cookie|signature|hmac[_-]?key|private[_-]?key|"
    r"jwt|ws[_-]?token|listen[_-]?key)$"
)
_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer|basic|digest)\s+[^\s,;]+"
)
_KV = re.compile(
    r"(?ix)"
    r"((?:[\"'])?\b(?:(?:[a-z][a-z0-9]*[_-])*)(?:api[_-]?key|api[_-]?secret|secret|token|password|passwd|pwd|"
    r"authorization|auth|cookie|set-cookie|signature|hmac[_-]?key|"
    r"webhook[_-]?secret|private[_-]?key|jwt|ws[_-]?token|listen[_-]?key)\b"
    r"\b(?:[\"'])?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;&]+)"
)
_PUBLIC_HASH_KEYS = {
    "release_sha256", "config_sha256", "pair_state_hash", "request_sha256",
    "state_hash", "strategy_file_sha256",
}
_PLAIN = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
        r"-----END [A-Z ]*PRIVATE KEY-----"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]+"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]+"),
    re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:postgres|postgresql|mysql|redis|amqp|mongodb)://[^\s\"']+"),
    re.compile(r"(?i)([?&](?:signature|listenKey|apiKey|token)=)[^\s&\"']+"),
    re.compile(r"\b[A-Za-z0-9_-]{64,}\b"),
)


def redact(text):
    if not isinstance(text, str):
        return text
    out = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
    for pattern in _PLAIN:
        if pattern.groups:
            out = pattern.sub(r"\1[REDACTED]", out)
        else:
            out = pattern.sub("[REDACTED]", out)
    out = _KV.sub(r'\1"[REDACTED]"', out)
    return out


def redact_obj(value, key: str = ""):
    """Recursively redact both sensitive fields and free-form string values."""
    if key and _SENSITIVE_KEY.match(str(key)):
        return "[REDACTED]"
    if (str(key) in _PUBLIC_HASH_KEYS and isinstance(value, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", value)):
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(k): redact_obj(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_obj(item) for item in value)
    return value
