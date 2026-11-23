"""
The dashboard as a page the gateway renders (Jinja): ``GET /dashboard`` and
the two panels its script swaps in after a click.

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
from app.services import client_rules, dashboard_view


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


@router.get("/dashboard/client-notices", response_class=HTMLResponse)
def client_notices_panel(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The dashboard's notices panel alone (empty when the account has
    nothing open): the page swaps it in after *I've applied it*, so the one
    patch and what it covers always match what is still open."""
    return templates.TemplateResponse(
        request,
        "_client_notices.html",
        {
            "client_notices": dashboard_view.client_notices(session, user),
            "client_combined": dashboard_view.client_combined(session, user),
            "client_quiet_days": client_rules.RESOLVED_QUIET_DAYS,
        },
    )


@router.get("/dashboard/model-changes", response_class=HTMLResponse)
def model_changes_panel(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The dashboard's model-change panel alone (empty when no change is
    being shown to the account): the page swaps it in after *I've switched*."""
    return templates.TemplateResponse(
        request, "_model_changes.html", {"model_changes": dashboard_view.shown_model_changes(session, user)},
    )
