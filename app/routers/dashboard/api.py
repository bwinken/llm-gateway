"""
What is left of ``/dashboard/api/*``: the routes only a person signed in
through the dashboard may call, and one that belongs to the page.

The dashboard's data (``me``, ``status``, ``usage``, ``service-accounts``,
``notices``) is not here. Those five were routes of this module that
answered what the account endpoints of the same name answer; they are the
account endpoints now (``GET /v1/me`` ... ``/v1/notices``,
``app/routers/account_api.py``), which take an SSO sign-in as well as an
API key. A frontend reads them there.

What stays is what a key must never be able to do, kept under
``/dashboard`` so the proxy's sign-in is in front of it as well as the
gateway's own check:

- ``api-key`` and ``service-accounts/{id}/api-key`` show a key in full.
  They do nothing else, are never cached and are in the audit log
  (``audit._AUDITED_READS``), like the admin's *View key*.
- ``model-breakdown`` is the page's own *Cost by Model* card, and goes
  when the page does.

The same promise as the account endpoints: change by addition only, never
a ``null`` (a key without a value is left out).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Security
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.core.auth import get_web_user
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import User
from app.routers.dashboard.actions import owned_service_account
from app.routers.dashboard.common import router
from app.services import dashboard_view

_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/dashboard/api/api-key")
def own_api_key(user: User = Security(get_web_user, scopes=["read"])):
    """The person's own API key, in full: what *My API Key* shows. One key
    per call, never cached, and in the audit log."""
    logger.info("API key shown on the dashboard | user={}", user.username)
    return JSONResponse(
        {"object": "api_key", "username": user.username, "api_key": user.api_key}, headers=_NO_STORE,
    )


@router.get("/dashboard/api/service-accounts/{app_id}/api-key")
def service_account_api_key(
    app_id: int,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The API key of a service account the person owns, in full. Refused
    (403) unless they are an owner and in the department it is for, like
    regenerating it. Never cached, and in the audit log."""
    target = owned_service_account(session, user, app_id)
    logger.info("Service account key shown on the dashboard | account={} user={}", target.username, user.username)
    return JSONResponse(
        {"object": "api_key", "username": target.username, "api_key": target.api_key}, headers=_NO_STORE,
    )


@router.get("/dashboard/api/model-breakdown")
def model_breakdown_api(
    period: str = "month",
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The page's *Cost by Model* card as it draws it: per backend a group
    with its rows, each with its share of the period. Backs the page's
    Month / Today toggle and refresh button. The same usage is in
    ``GET /v1/usage`` (``month.models``, ``today.models``) without the
    grouping; a frontend of its own reads that one."""
    if period not in ("month", "day"):
        raise HTTPException(status_code=400, detail="period must be 'month' or 'day'.")
    groups, total_cost = dashboard_view.model_breakdown(session, user, period=period)
    return JSONResponse({
        "period": period,
        "groups": groups,
        "total_cost_usd": total_cost,
    })
