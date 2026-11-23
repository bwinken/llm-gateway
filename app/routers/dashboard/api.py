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

from app.core import api_docs
from app.core.auth import get_web_user
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import User
from app.routers.dashboard.actions import owned_service_account
from app.routers.dashboard.common import router
from app.services import dashboard_view

_NO_STORE = {"Cache-Control": "no-store"}


@router.get(
    "/dashboard/api/api-key", summary="Show the caller's API key in full",
    responses=api_docs.answers(api_docs.ApiKeyShown),
)
def own_api_key(user: User = Security(get_web_user, scopes=["read"])):
    """The signed-in person's own API key, in full: what the dashboard's *My API Key* window shows.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `api_key` |
    | `username` | string | The account |
    | `api_key` | string | The key, in full |

    **Limits and things to know**

    - One key per call, and nothing else in the answer.
    - Never cached: `Cache-Control: no-store`.
    - Every call is in the audit log (the key itself is not).
    - The masked key, for showing which key it is without revealing it, is in `GET /v1/me`.

    **Errors.** Only the group's (401, 403).
    """
    logger.info("API key shown on the dashboard | user={}", user.username)
    return JSONResponse(
        {"object": "api_key", "username": user.username, "api_key": user.api_key}, headers=_NO_STORE,
    )


@router.get(
    "/dashboard/api/service-accounts/{app_id}/api-key", summary="Show an owned service account's API key in full",
    responses={
        **api_docs.answers(api_docs.ApiKeyShown),
        404: {"description": "No account with that id."},
    },
)
def service_account_api_key(
    app_id: int,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The API key of a service account the signed-in person owns, in full.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Path.** `app_id`: the account's `id`, as `GET /v1/service-accounts` gives it.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `api_key` |
    | `username` | string | The service account |
    | `api_key` | string | Its key, in full |

    **Limits and things to know**

    - Only for an owner an admin assigned, and only while they are in the department the account is
      for: the same check as regenerating it.
    - Never cached (`Cache-Control: no-store`), and every call is in the audit log with the account
      it was for.

    **Errors**

    | Status | When |
    |---|---|
    | 403 | The caller does not own the account, or it is for another department than theirs |
    | 404 | No account with that id |
    """
    target = owned_service_account(session, user, app_id)
    logger.info("Service account key shown on the dashboard | account={} user={}", target.username, user.username)
    return JSONResponse(
        {"object": "api_key", "username": target.username, "api_key": target.api_key}, headers=_NO_STORE,
    )


@router.get(
    "/dashboard/api/model-breakdown", summary="Cost by model, as the page draws it",
    responses={400: {"description": "`period` is neither `month` nor `day`."}},
)
def model_breakdown_api(
    period: str = "month",
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The dashboard page's *Cost by Model* card: usage per backend and model, each with its share of the period.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Query**

    | Name | Type | Default | Limits |
    |---|---|---|---|
    | `period` | string | `month` | `month` or `day` (the current Asia/Taipei month or day) |

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `period` | string | As asked |
    | `groups` | array | One per backend, each with its rows (a model, its tokens, cost and share) |
    | `total_cost_usd` | number | The period's cost |

    **Limits and things to know**

    - The page's own route: it backs the card's Month / Today switch and goes when the page does. Its
      shape follows the card, so it is no contract.
    - The same usage without the grouping is in `GET /v1/usage` (`month.models`, `today.models`): build
      against that one.

    **Errors**

    | Status | When |
    |---|---|
    | 400 | `period` is neither `month` nor `day` |
    """
    if period not in ("month", "day"):
        raise HTTPException(status_code=400, detail="period must be 'month' or 'day'.")
    groups, total_cost = dashboard_view.model_breakdown(session, user, period=period)
    return JSONResponse({
        "period": period,
        "groups": groups,
        "total_cost_usd": total_cost,
    })
