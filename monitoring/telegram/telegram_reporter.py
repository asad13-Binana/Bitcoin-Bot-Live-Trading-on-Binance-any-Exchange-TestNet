#!/usr/bin/env python3
"""Send a bounded, plain-text read-only report to the monitor-only bot."""
from __future__ import annotations

import json
import os
import sys

import httpx

from monitoring.api.configuration import loopback_http_url, secret_is_configured


MAX_CHUNK = 3800


def _failure(reason: str) -> int:
    print(json.dumps({"delivery": "failed", "reason": reason}))
    return 1


def _format(report: dict) -> str:
    performance = report.get("execution_performance", {})
    bot = report.get("bot", {})
    websocket = report.get("websocket", {})
    latency = report.get("binance_rest", {})
    system = report.get("system", {})
    deployment = report.get("deployment", {})
    crashes = report.get("crashes", {})
    order_quality = report.get("order_quality", {})
    active_pair = report.get("active_pair", {})
    moneyflow = report.get("moneyflow", {})
    classification = moneyflow.get("classification", {})
    futures = moneyflow.get("futures", {})
    lines = [
        str(report.get("banner", "MODE: UNKNOWN")),
        f"Release: {deployment.get('release_tag') or deployment.get('release_sha256') or 'n/a'}",
        f"Bot services: {bot.get('status', 'n/a')}",
        f"Active pair: {active_pair.get('pair', 'n/a')} | pair state: {'valid' if active_pair.get('valid') else 'INVALID/UNKNOWN'}",
        f"Money flow: {moneyflow.get('status', 'n/a')} | decision: {classification.get('decision', 'n/a')} | matching futures: {'yes' if futures.get('available') else 'no'}",
        f"Closed: {performance.get('closed_trades', 'n/a')} | Open: {performance.get('open_trades', 'n/a')} | Win%: {performance.get('win_rate_pct', 'n/a')}",
        f"Net P/L: {performance.get('net_pnl_pct', 'n/a')}% | PF: {performance.get('profit_factor', 'n/a')} | MaxDD: {performance.get('max_drawdown_pct', 'n/a')}%",
        f"Orders rejected: {order_quality.get('rejected_orders', 'n/a')} | Signals rejected: {order_quality.get('rejected_signals', 'n/a')}",
        f"User stream: {'connected' if websocket.get('connected') and websocket.get('subscribed') else 'DOWN/UNKNOWN'} | reconnects {websocket.get('reconnect_count', 'n/a')}",
        f"Crashes: {crashes.get('crash_count', 'n/a')} ({crashes.get('window_hours', 'n/a')}h)",
        f"Binance REST: {'ok' if latency.get('reachable') else 'DOWN'} | median {latency.get('median_ms', 'n/a')}ms | p95 {latency.get('p95_ms', 'n/a')}ms | p99 {latency.get('p99_ms', 'n/a')}ms",
        f"CPU {system.get('cpu_pct', 'n/a')}% | MEM {system.get('mem_pct', 'n/a')}% | DISK {system.get('disk_used_pct', 'n/a')}%",
    ]
    if performance.get("error"):
        lines.append(f"Performance source note: {performance['error']}")
    return "\n".join(lines)


def _send(token: str, chat: str, text: str) -> None:
    for offset in range(0, len(text), MAX_CHUNK):
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[offset:offset + MAX_CHUNK]},
            timeout=20,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise httpx.HTTPError("telegram returned malformed JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise httpx.HTTPError("telegram rejected the request")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.getenv("TELEGRAM_REPORTS_ENABLED", "false").lower() != "true":
        return _failure("reports_disabled")
    days = 1
    if "--days" in argv:
        try:
            days = max(1, min(90, int(argv[argv.index("--days") + 1])))
        except (ValueError, IndexError):
            return _failure("invalid_days")

    url = os.getenv("MONITOR_URL", "").rstrip("/")
    monitor_token = os.getenv("MONITOR_TOKEN", "").strip()
    telegram_token = os.getenv("TELEGRAM_MONITOR_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_MONITOR_CHAT_ID", "").strip()
    if not loopback_http_url(url):
        return _failure("monitor_url_not_loopback")
    if not secret_is_configured(monitor_token):
        return _failure("monitor_token_not_configured")
    if not secret_is_configured(telegram_token):
        return _failure("telegram_token_not_configured")
    if not secret_is_configured(chat, minimum=1):
        return _failure("telegram_chat_not_configured")

    try:
        response = httpx.get(
            f"{url}/api/v1/report",
            params={"days": days},
            headers={"Authorization": f"Bearer {monitor_token}"},
            timeout=30,
        )
        response.raise_for_status()
        report = response.json()
        if not isinstance(report, dict):
            return _failure("monitor_invalid_response")
    except httpx.HTTPError:
        return _failure("monitor_unavailable")
    except ValueError:
        return _failure("monitor_malformed_json")

    try:
        _send(telegram_token, chat, _format(report))
    except httpx.HTTPError:
        return _failure("telegram_delivery_failed")
    except Exception:
        return _failure("telegram_unexpected_failure")
    print(json.dumps({"delivery": "ok", "days": days}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
