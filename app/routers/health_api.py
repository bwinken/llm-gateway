"""
Liveness and readiness probes for the gateway process itself.

Deliberately unauthenticated: probes come from nginx / an external load
balancer / a container runtime, none of which carry an API key or an SSO
session. Neither endpoint exposes anything an unauthenticated caller could
not already learn by watching the service respond at all — no model names,
no user data, no downstream URLs.

    /healthz  liveness  — the process is up and the event loop is turning.
                          Zero I/O, always 200. Restart the worker if this
                          stops answering.
    /readyz   readiness — the process can actually serve requests, i.e. the
                          database answers. 200 or 503.

`/readyz` gates on the database only. The vLLM health cache is reported for
operators to look at but never fails the probe, for two reasons: it is
populated by a background loop that has not run yet during the first ~30 s
after boot (a fresh worker would report itself unready), and a fleet-wide
downstream outage would take every gateway instance out of the load
balancer at once — turning a degraded service (Azure/Bedrock still route,
clients still get a real error) into a total one.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core.config import APP_TITLE
from app.core.database import engine
from app.core.logger import logger
from app.core.server_state import all_health

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness: the process is alive. No I/O, so it can never block."""
    return JSONResponse({"status": "ok", "app": APP_TITLE})


def _ping_db() -> None:
    """Round-trip the DB with the cheapest possible statement (sync driver)."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


@router.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness: the database answers. 503 when it does not."""
    health = all_health()
    downstreams = {
        "alive": sum(1 for alive in health.values() if alive),
        "total": len(health),
    }

    try:
        await run_in_threadpool(_ping_db)
    except Exception as exc:  # noqa: BLE001 — any DB failure means not ready
        logger.warning("Readiness probe failed: database unreachable ({})", exc)
        return JSONResponse(
            {
                "status": "unavailable",
                "database": "error",
                "downstreams": downstreams,
            },
            status_code=503,
        )

    return JSONResponse(
        {"status": "ok", "database": "ok", "downstreams": downstreams}
    )
