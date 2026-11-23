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

from app.core.auth import get_web_user
from app.core.database import get_session
from app.models.schema import User
from app.routers.dashboard.common import router
from app.routers.web_ui import _common_ctx, templates
from app.services import dashboard_view


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _common_ctx(request, title="Dashboard", **dashboard_view.page_context(session, user)),
    )
