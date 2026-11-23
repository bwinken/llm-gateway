"""
The dashboard as data: ``/dashboard/api/*``, JSON for a frontend of its own
(or anything else signed in by SSO).

``me``, ``status``, ``usage``, ``service-accounts`` and ``notices`` answer
what the account endpoints of the same name answer by API key
(``/v1/me`` ... ``/v1/notices``): both call the one function in
``app/services/account_view.py``, so a client learns one shape and the two
cannot disagree. On top of that each carries
what only a signed-in person may see: whether they are an admin, the masked
key, a service account's id and who it sent requests for.

A key is never part of those. The two routes that show one
(``api-key``, ``service-accounts/{id}/api-key``) do nothing else, are never
cached and are in the audit log (``audit._AUDITED_READS``), like the admin's
*View key*.

The same promise as the account endpoints: change by addition only, never
a ``null`` (a key without a value is left out). The frontend is released
apart from the gateway and asks ``GET /v1/version`` whether it still fits.
"""

from __future__ import annotations

from datetime import timezone

from fastapi import Depends, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session

from app.core.auth import get_web_user
from app.core.config import APP_TITLE, get_site_links, setup_page_enabled
from app.core.database import get_session
from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ, local_month_start
from app.models.schema import User, mask_api_key
from app.routers.dashboard.actions import owned_service_account
from app.routers.dashboard.common import router
from app.routers.web_ui import gateway_base
from app.services import account_view, client_rules, dashboard_view, model_catalog
from app.services.model_order import natural_key
from app.services.stats import FLOW_TOP_USERS, get_on_behalf_users, get_owned_apps_summary, get_withheld_apps

_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/dashboard/api/me")
async def me(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Who is signed in and today's budget: everything ``GET /v1/me``
    answers, plus what the dashboard's frame needs.

    Added to ``/v1/me``:

    - ``is_admin`` is true for an admin (the token's ``admin`` scope): show
      the link to the admin panel. By API key it is always false.
    - ``org_code``: the person's department, when their sign-in gave one.
    - ``api_key``: ``masked`` (enough to recognise the key, not to use it)
      and ``created_at``. The key itself is ``GET /dashboard/api/api-key``.
    - ``site``: ``title``, the ``api_base`` to show in examples
      (``https://host/v1``), the admin's ``links`` (only the ones that are
      set), whether the Setup page is on (``setup_page``) and whether the
      Windows installer can be downloaded (``installer``).
    """
    body = await run_in_threadpool(account_view.me, user)
    body["is_admin"] = bool(user.is_admin)
    if user.org_code:
        body["org_code"] = user.org_code
    key: dict[str, object] = {"masked": mask_api_key(user.api_key)}
    if user.api_key_created_at is not None:
        key["created_at"] = (
            user.api_key_created_at.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).isoformat(timespec="seconds")
        )
    body["api_key"] = key
    setup_page = setup_page_enabled()
    body["site"] = {
        "title": APP_TITLE,
        "api_base": gateway_base(request),
        "links": {name: url for name, url in get_site_links().items() if url},
        "setup_page": setup_page,
        "installer": setup_page and dashboard_view.installer_available(),
    }
    return body


@router.get("/dashboard/api/status")
async def status(user: User = Security(get_web_user, scopes=["read"])):
    """The models the person can use and whether they are up: exactly what
    ``GET /v1/status`` answers. An admin sees the cloud models whatever
    their own cloud access, as on the dashboard page. No database read."""
    return account_view.status(user)


@router.get("/dashboard/api/usage")
def usage(
    days: int = Query(7, ge=1, le=account_view.USAGE_MAX_DAYS, description="How many days back, today included."),
    user: User = Security(get_web_user, scopes=["read"]),
):
    """The person's usage per day, and today and this month by model and by
    the way it came in: exactly what ``GET /v1/usage`` answers, the figures
    behind the trend chart, *Cost by Model* and *Usage Flow*. Plain ``def``
    (database read)."""
    return account_view.usage(user, days)


def _sent_for(flow: dict) -> dict:
    """Who a service account sent requests for this month
    (``stats.get_on_behalf_users``), as data: ``users`` counts all of them,
    ``rows`` is one per user and backend for the ones named, then one per
    backend for the rest (no ``user``; ``others`` says how many it stands
    for)."""
    rows = []
    for row in flow["rows"]:
        entry: dict[str, object] = {}
        if row["user"]:
            entry["user"] = row["user"]
            if row["display_name"]:
                entry["display_name"] = row["display_name"]
        else:
            entry["others"] = row.get("others", 0)
        entry.update(
            backend=model_catalog.BACKEND_NAMES.get(row["backend"], row["backend"]),
            requests=row["requests"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
        )
        rows.append(entry)
    return {"users": flow["users"], "rows": rows}


@router.get("/dashboard/api/service-accounts")
def service_accounts(
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The service accounts the person owns: every account as
    ``GET /v1/service-accounts`` gives it, plus what only its signed-in
    owner sees.

    Added per account: its ``id`` (for the key routes), ``api_key_masked``,
    and ``sent_for`` when it sent requests for its users this month
    (``X-On-Behalf-Of``): how many ``users``, and ``rows`` of one user on
    one backend, the ``top_users`` by cost by name and the rest as one row
    per backend with ``others`` instead of ``user``.

    ``withheld`` names the accounts assigned to the person that they may
    not see now, each made for another ``department`` than theirs. An
    account's key is ``GET /dashboard/api/service-accounts/{id}/api-key``.
    Plain ``def`` (database read)."""
    accounts = sorted(get_owned_apps_summary(session, user.id), key=lambda a: natural_key(a["username"]))
    flows = get_on_behalf_users(session, [a["id"] for a in accounts if a["on_behalf"]["requests"]])
    rows = []
    for account in accounts:
        row = {
            "id": account["id"],
            **account_view.service_account_row(account),
            "api_key_masked": mask_api_key(account["api_key"]),
        }
        flow = flows.get(account["id"])
        if flow:
            row["sent_for"] = _sent_for(flow)
        rows.append(row)
    return {
        "object": "service_accounts",
        "month": local_month_start().strftime("%Y-%m"),
        "service_accounts": rows,
        "withheld": get_withheld_apps(session, user.id),
        "top_users": FLOW_TOP_USERS,
    }


@router.get("/dashboard/api/notices")
def notices(user: User = Security(get_web_user, scopes=["read"])):
    """The person's open client-rule notices with their patch, and beside
    them the model changes shown to them: exactly what ``GET /v1/notices``
    answers, plus ``quiet_days`` (how long a notice reported as applied is
    left alone).

    Reporting as applied is ``POST /dashboard/client-rules/all/resolved``
    or ``.../{id}/resolved`` for one notice, and
    ``POST /dashboard/model-changes/{id}/applied`` for a model change, each
    with the ``id`` given here. Plain ``def`` (database read)."""
    body = account_view.notices(user)
    body["quiet_days"] = client_rules.RESOLVED_QUIET_DAYS
    return body


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
    ``GET /dashboard/api/usage`` (``month.models``, ``today.models``) without
    the grouping; a frontend of its own reads that one."""
    if period not in ("month", "day"):
        raise HTTPException(status_code=400, detail="period must be 'month' or 'day'.")
    groups, total_cost = dashboard_view.model_breakdown(session, user, period=period)
    return JSONResponse({
        "period": period,
        "groups": groups,
        "total_cost_usd": total_cost,
    })
