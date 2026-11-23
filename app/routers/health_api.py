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
    /metrics  Prometheus exposition (app/services/metrics.py), only when
              METRICS_ENABLED; 404 otherwise. Aggregates, no user names —
              but keep it off the public listener (see the nginx example).
    /v1/version  the API versions this gateway serves and the status of
              each (app/core/api_versions.py). No key: a frontend or a tool
              asks before it signs in whether it still fits this gateway.

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
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core import api_docs, api_versions
from app.core.config import APP_TITLE
from app.core.database import engine
from app.core.logger import logger
from app.core.server_state import all_health
from app.services import metrics

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


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    """Prometheus scrape target, summed over every worker. Plain ``def``:
    collecting reads the workers' metric files."""
    rendered = metrics.render()
    if rendered is None:
        return Response(status_code=404)
    body, content_type = rendered
    return Response(content=body, media_type=content_type)


@router.get(
    "/v1/version", summary="The API versions this gateway serves", responses=api_docs.answers(api_docs.Versions),
)
async def api_version():
    """Which versions of the API this gateway serves, and how far along each is: what a tool or a frontend released apart from the gateway asks first.

    **Sign-in.** None: no API key and no sign-in, so it can be a client's first call.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `version` |
    | `gateway` | string | The gateway's own release (`0.1.0`), for a bug report |
    | `stable` | string | The version to build against (`v1`) |
    | `statuses` | array of string | Every status there is now: `stable`, `deprecated`, `nightly` |
    | `versions` | array | Every version served |
    | `versions[].version`, `status` | string | The version, and its status |
    | `versions[].sunset`, `successor` | string | On a `deprecated` version: its last day (`YYYY-MM-DD`) and the version to move to. Absent otherwise |
    | `versions[].note` | string | A line for people. Absent when there is none |

    **Limits and things to know**

    - `stable`: served, and changed by addition only. `deprecated`: still served until `sunset`.
      `nightly`: for trying out; it may change or go without notice.
    - A version that is not listed is not served.
    - Another status may be added later: treat one you do not know like `nightly`.
    - Never compare `gateway` to decide what is supported. To know whether a key of an answer is there,
      look for it: a value the gateway does not have is a key it leaves out.
    - It says nothing about accounts, models or settings. No database read.

    **Errors.** None of its own.
    """
    return api_versions.payload()
