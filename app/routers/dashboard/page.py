"""
The dashboard as a page the gateway renders (Jinja): ``GET /dashboard``.
After *I've applied it* or *I've switched* its script loads the page again,
so there is no route that serves a part of it.

This module is the frontend half of the dashboard. Its data is
``dashboard_view.page_context``, and everything it shows is also served as
JSON by ``api.py``, so a frontend of its own replaces this module and
nothing else.
"""

from __future__ import annotations

from fastapi import Depends, Request, Security
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.core import api_docs
from app.core.auth import get_web_user
from app.core.database import get_session
from app.models.schema import User
from app.routers.dashboard.common import router
from app.routers.web_ui import _common_ctx, templates
from app.services import dashboard_view


@router.get(
    "/dashboard", response_class=HTMLResponse, summary="The dashboard page", responses=api_docs.PAGE_200,
)
def dashboard(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The dashboard: the signed-in person's budget, models, usage, service accounts and notices, as one HTML page.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Response.** The whole page as HTML, with its data already in it.

    **Limits and things to know**

    - A page for a browser, not an API. The same data as JSON is the account endpoints (`GET /v1/me`,
      `/v1/status`, `/v1/usage`, `/v1/service-accounts`, `/v1/notices`), which take this sign-in too.
    - It is rendered whole on every load: no route serves a part of it. After an action the page loads
      itself again.
    - The page carries the person's API key, and the keys of the service accounts they own, in its HTML.

    **Errors.** Only the group's (401, 403).
    """
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _common_ctx(request, title="Dashboard", **dashboard_view.page_context(session, user)),
    )
