"""
The site's other pages (Jinja2 HTML): the welcome page and the Setup page.

The user dashboard is a package of its own (``app/routers/dashboard``). It
takes this module's template environment and base context for the one page
it still renders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models.schema import User

from app.core import api_docs
from app.core.auth import get_web_user
from app.core.config import (
    APP_TITLE,
    api_base_url,
    get_site_links,
    setup_page_enabled,
)
from app.core.static_assets import static_url
from app.services import dashboard_view

router = APIRouter(tags=["web"], responses=api_docs.DASHBOARD_ERRORS)
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
# base.html reads the admin-editable links (support bot / install guide) on
# every page, so expose them as a template global instead of threading them
# through every render context.
templates.env.globals["get_site_links"] = get_site_links
# Same for the Setup page switch (SETUP_PAGE_ENABLED): the navbar link is on
# every page.
templates.env.globals["setup_page_enabled"] = setup_page_enabled
# And the URLs of the pages' own stylesheet, scripts and fonts.
templates.env.globals["static_url"] = static_url


def gateway_base(request: Request) -> str:
    """The API base URL to show this caller (``https://host/v1``):
    ``config.api_base_url``, which ``GET /v1/me`` gives a signed-in person
    too."""
    return api_base_url(request)


def _common_ctx(request: Request, **extra) -> dict:
    """Base context shared by all pages."""
    return {
        "request": request,
        "app_title": APP_TITLE,
        "current_year": datetime.now(timezone.utc).year,
        "gateway_base": gateway_base(request),
        **extra,
    }


@router.get("/", response_class=HTMLResponse, summary="The welcome page", responses=api_docs.PAGE_200)
def index(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """The welcome page: how to call the gateway, with the models there are and a first example.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Response.** The page, as HTML.

    **Limits and things to know**

    - A page for a browser, not an API. The models as data are `GET /v1/models` and `GET /v1/status`.
    - It lists the on-prem models an admin has not hidden. The cloud models a person may use are on
      their dashboard.

    **Errors.** Only the group's (401, 403).
    """
    # Models by type (hidden ones skipped), each with the optional admin-set
    # metadata for the capability badges and its context window.
    models_by_type, first_model = dashboard_view.welcome_models()
    return templates.TemplateResponse(
        request,
        "welcome.html",
        _common_ctx(
            request,
            title="Welcome",
            user=user,
            display_name=user.display_name or user.username,
            org_code=user.org_code,
            models_by_type=models_by_type,
            first_model=first_model or "your-model",
        ),
    )


# ── Setup page (requires SSO login) ──

@router.get(
    "/setup", response_class=HTMLResponse, summary="The Setup page (Claude Code for Windows)",
    responses={**api_docs.PAGE_200, 404: {"description": "The Setup page is switched off (`SETUP_PAGE_ENABLED=false`)."}},
)
def setup_page(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """The Setup page: how to install Claude Code on Windows, with the installer to download.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Response.** The page, as HTML.

    **Limits and things to know**

    - For Windows users: the installer it offers is `/dashboard/install-claude-code.bat`.
    - A site without Windows users turns it off (`SETUP_PAGE_ENABLED=false`): the page, the installer
      and every link to them are then gone. `site.setup_page` in `GET /v1/me` says which it is.

    **Errors**

    | Status | When |
    |---|---|
    | 404 | The Setup page is switched off |
    """
    if not setup_page_enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    claude_code_available = dashboard_view.installer_available()
    return templates.TemplateResponse(
        request,
        "setup.html",
        _common_ctx(
            request,
            title="Setup",
            claude_code_available=claude_code_available,
            user=user,
            display_name=user.display_name or user.username,
            org_code=user.org_code,
        ),
    )
