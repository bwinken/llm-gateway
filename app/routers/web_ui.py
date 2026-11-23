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

from app.core.auth import get_web_user
from app.core.config import (
    APP_TITLE,
    get_site_links,
    setup_page_enabled,
)
from app.core.static_assets import static_url
from app.services import dashboard_view

router = APIRouter(tags=["web"])
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
    """The API base URL to show this caller (``https://host/v1``), built
    from the Host header (reliable behind the reverse proxy)."""
    host = request.headers.get("host", request.url.hostname or "localhost")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme or "http")
    return f"{scheme}://{host}/v1"


def _common_ctx(request: Request, **extra) -> dict:
    """Base context shared by all pages."""
    return {
        "request": request,
        "app_title": APP_TITLE,
        "current_year": datetime.now(timezone.utc).year,
        "gateway_base": gateway_base(request),
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Getting-started welcome page with API guide and available models."""
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

@router.get("/setup", response_class=HTMLResponse)
def setup_page(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Setup page for the Claude Code installer (Windows). Requires login.
    404 when a site turned it off (``SETUP_PAGE_ENABLED=false``)."""
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
