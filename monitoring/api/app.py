"""Canonical, versioned, read-only monitoring API for the Bitcoin bot."""
from __future__ import annotations

import datetime as dt

from fastapi import Depends, FastAPI, Query

from . import metrics
from .authentication import require_bearer
from .configuration import CONFIG
from .log_redaction import redact_obj


_docs = (
    {"docs_url": "/docs", "redoc_url": None}
    if CONFIG.enable_docs
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)
app = FastAPI(title="Bitcoin Bot Read-only Monitor", version="4.0", **_docs)
API = "/api/v1"


def _base(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": CONFIG.bot_mode,
        "banner": CONFIG.banner(),
    }


def _payload(request_id: str, **sections):
    return redact_obj({**_base(request_id), **sections})


@app.get(API + "/health")
def health(request_id: str = Depends(require_bearer)):
    return _payload(
        request_id,
        monitor={"status": "ok", "enabled": CONFIG.enabled},
        bot=metrics.runtime_health(),
        containers=metrics.container_state(),
        databases=metrics.databases(),
        log=metrics.log_freshness(),
        active_pair=metrics.active_pair_status(),
        moneyflow=metrics.moneyflow_status(),
    )


@app.get(API + "/status")
def status(request_id: str = Depends(require_bearer)):
    return _payload(
        request_id,
        bot=metrics.runtime_health(),
        containers=metrics.container_state(),
        databases=metrics.databases(),
        execution=metrics.execution_state(),
        order_quality=metrics.order_quality(1),
        websocket=metrics.websocket_status(),
        active_pair=metrics.active_pair_status(),
        moneyflow=metrics.moneyflow_status(),
        deployment=metrics.deployment_info(),
    )


@app.get(API + "/performance")
def performance(
    days: int = Query(1, ge=1, le=CONFIG.MAX_REPORT_DAYS),
    request_id: str = Depends(require_bearer),
):
    return _payload(
        request_id,
        execution_performance=metrics.performance(days),
        signal_engine_performance=metrics.signal_performance(days),
        order_quality=metrics.order_quality(days),
    )


@app.get(API + "/trades")
def trades(
    limit: int = Query(20, ge=1, le=CONFIG.MAX_TRADES),
    request_id: str = Depends(require_bearer),
):
    return _payload(
        request_id,
        recent_execution_trades=metrics.recent_trades(limit),
        execution_state=metrics.execution_state(limit),
    )


@app.get(API + "/errors")
def errors(
    lines: int = Query(200, ge=1, le=CONFIG.MAX_LOG_LINES),
    request_id: str = Depends(require_bearer),
):
    return _payload(
        request_id,
        errors=metrics.error_lines(lines),
        security_warnings=metrics.recent_security_warnings(),
    )


@app.get(API + "/crashes")
def crashes(
    hours: int = Query(24, ge=1, le=CONFIG.MAX_CRASH_HOURS),
    request_id: str = Depends(require_bearer),
):
    return _payload(request_id, crashes=metrics.crash_blocks(hours))


@app.get(API + "/latency")
def latency(
    samples: int = Query(5, ge=1, le=CONFIG.MAX_LATENCY_SAMPLES),
    request_id: str = Depends(require_bearer),
):
    return _payload(request_id, binance_rest=metrics.binance_latency(samples))


@app.get(API + "/system")
def system(request_id: str = Depends(require_bearer)):
    return _payload(
        request_id,
        system=metrics.system_resources(),
        containers=metrics.container_state(),
    )


@app.get(API + "/deployment")
def deployment(request_id: str = Depends(require_bearer)):
    return _payload(request_id, deployment=metrics.deployment_info())


@app.get(API + "/pair")
def pair(request_id: str = Depends(require_bearer)):
    return _payload(request_id, active_pair=metrics.active_pair_status())


@app.get(API + "/moneyflow")
def moneyflow(request_id: str = Depends(require_bearer)):
    return _payload(
        request_id,
        active_pair=metrics.active_pair_status(),
        moneyflow=metrics.moneyflow_status(),
    )


@app.get(API + "/report")
def report(
    days: int = Query(1, ge=1, le=CONFIG.MAX_REPORT_DAYS),
    request_id: str = Depends(require_bearer),
):
    """Combined daily/custom report; every section fails independently."""
    sections = {}
    collectors = (
        ("bot", metrics.runtime_health, ()),
        ("containers", metrics.container_state, ()),
        ("databases", metrics.databases, ()),
        ("execution_performance", metrics.performance, (days,)),
        ("signal_engine_performance", metrics.signal_performance, (days,)),
        ("order_quality", metrics.order_quality, (days,)),
        ("websocket", metrics.websocket_status, ()),
        ("active_pair", metrics.active_pair_status, ()),
        ("moneyflow", metrics.moneyflow_status, ()),
        ("binance_rest", metrics.binance_latency, (5,)),
        ("system", metrics.system_resources, ()),
        ("crashes", metrics.crash_blocks, (24,)),
        ("deployment", metrics.deployment_info, ()),
        ("security_warnings", metrics.recent_security_warnings, ()),
    )
    for key, collector, args in collectors:
        try:
            sections[key] = collector(*args)
        except Exception as exc:  # no one broken source may kill the report
            sections[key] = {"error": type(exc).__name__}
    return _payload(request_id, **sections)
