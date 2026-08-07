#!/usr/bin/env python3
"""Twelve read-only MCP tools backed only by the loopback monitor API."""
from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from monitoring.api.configuration import loopback_http_url, secret_is_configured
from monitoring.api.log_redaction import redact


URL = os.getenv("MONITOR_URL", "http://127.0.0.1:8090").rstrip("/")
TOKEN = os.getenv("MONITOR_TOKEN", "")


def _base_url() -> str:
    if not loopback_http_url(URL):
        raise ValueError("MONITOR_URL must be an HTTP loopback URL")
    parsed = urlparse(URL)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("MONITOR_URL must not contain a path, query, or fragment")
    return URL


def _get(path: str, **params) -> dict:
    if not secret_is_configured(TOKEN):
        return {"ok": False, "error": "monitor_token_not_configured"}
    try:
        response = httpx.get(
            f"{_base_url()}/api/v1{path}",
            params=params,
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=30,
        )
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else {"ok": False, "error": "invalid_response_shape"}
    except ValueError as exc:
        return {"ok": False, "error": redact(str(exc))}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": "monitor_http_error", "status": exc.response.status_code}
    except (httpx.HTTPError, ValueError):
        return {"ok": False, "error": "monitor_unavailable"}


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def create_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("bitcoin-bot-readonly-monitor")

    @server.tool()
    def bot_health() -> dict:
        """Bot services, containers, databases, and log freshness."""
        return _get("/health")

    @server.tool()
    def bot_status() -> dict:
        """Combined operational status with execution and WebSocket state."""
        return _get("/status")

    @server.tool()
    def bot_performance(days: int = 1) -> dict:
        """Real execution P/L plus separately labelled signal-engine results."""
        return _get("/performance", days=_clamp(days, 1, 90, 1))

    @server.tool()
    def bot_recent_trades(limit: int = 20) -> dict:
        """Recent realised execution records and current execution state."""
        return _get("/trades", limit=_clamp(limit, 1, 200, 20))

    @server.tool()
    def bot_errors(lines: int = 200) -> dict:
        """Secret-redacted recent errors and security warnings."""
        return _get("/errors", lines=_clamp(lines, 1, 500, 200))

    @server.tool()
    def bot_crashes(hours: int = 24) -> dict:
        """Secret-redacted crash blocks within an explicit time window."""
        return _get("/crashes", hours=_clamp(hours, 1, 168, 24))

    @server.tool()
    def bot_binance_latency(samples: int = 5) -> dict:
        """Read-only Binance ping reachability and latency percentiles."""
        return _get("/latency", samples=_clamp(samples, 1, 10, 5))

    @server.tool()
    def bot_system_resources() -> dict:
        """Host CPU, memory, disk, monitor uptime, and container snapshot."""
        return _get("/system")

    @server.tool()
    def bot_deployment_status() -> dict:
        """Release identity, deployment/rollback, validation, and secret scan."""
        return _get("/deployment")

    @server.tool()
    def bot_market_context() -> dict:
        """Active BTC pair plus bounded Spot/USD-M money-flow context."""
        return _get("/moneyflow")

    @server.tool()
    def bot_daily_report() -> dict:
        """Combined one-day read-only report."""
        return _get("/report", days=1)

    @server.tool()
    def bot_custom_report(days: int = 7) -> dict:
        """Combined read-only report for 1 through 90 days."""
        return _get("/report", days=_clamp(days, 1, 90, 7))

    return server


def main() -> int:
    create_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
