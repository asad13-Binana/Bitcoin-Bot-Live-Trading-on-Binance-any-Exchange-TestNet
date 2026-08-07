"""Loopback allow-list, constant-time Bearer auth, per-client rate limiting,
and mandatory audit logging for the read-only API."""
from __future__ import annotations

import hmac
import ipaddress
import json
import time
import uuid
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from .configuration import CONFIG
from .log_redaction import redact


class AuditUnavailable(RuntimeError):
    pass


_WINDOWS: dict[str, deque] = defaultdict(deque)


def audit(event: str, request_id: str, detail: str = "") -> None:
    record = {
        "ts": round(time.time(), 3),
        "event": str(event)[:64],
        "request_id": str(request_id)[:64],
        "detail": redact(str(detail))[:300],
    }
    try:
        CONFIG.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
    except OSError as exc:
        raise AuditUnavailable(type(exc).__name__) from exc


def _audit_or_503(event: str, rid: str, detail: str) -> None:
    try:
        audit(event, rid, detail)
    except AuditUnavailable as exc:
        raise HTTPException(
            503, {"error": "audit_unavailable", "request_id": rid}
        ) from exc


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _allowed(client: str) -> bool:
    try:
        address = ipaddress.ip_address(client)
        return any(address in network for network in CONFIG.allowed_networks())
    except ValueError:
        return False


def require_bearer(request: Request) -> str:
    rid = uuid.uuid4().hex[:12]
    path = request.url.path
    client = _client_ip(request)

    if not CONFIG.enabled:
        raise HTTPException(503, {"error": "monitor_disabled", "request_id": rid})
    if CONFIG.runtime_errors():
        raise HTTPException(503, {"error": "monitor_misconfigured", "request_id": rid})
    if not _allowed(client):
        _audit_or_503("ip_denied", rid, f"{client} {path}")
        raise HTTPException(403, {"error": "source_ip_denied", "request_id": rid})

    auth = request.headers.get("authorization", "")
    supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(supplied, CONFIG.token):
        _audit_or_503("auth_failed", rid, f"{client} {path}")
        raise HTTPException(401, {"error": "unauthorized", "request_id": rid})

    # Only authenticated traffic consumes the authorized-client quota. Invalid
    # local requests therefore cannot exhaust the valid client's budget.
    now = time.time()
    window = _WINDOWS[client]
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= CONFIG.rate_limit_per_minute:
        _audit_or_503("rate_limited", rid, f"{client} {path}")
        raise HTTPException(429, {"error": "rate_limited", "request_id": rid})
    window.append(now)
    _audit_or_503("authorized", rid, f"{client} {path}")
    return rid
