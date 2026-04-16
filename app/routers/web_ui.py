"""
Web dashboard endpoints (Jinja2 HTML pages).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.models.schema import AppOwner, User

from app.core.auth import get_web_user
from app.core.config import APP_TITLE, get_model_routing_snapshot
from app.core.database import get_session
from app.core.server_state import is_alive
from app.services.stats import get_daily_trends, get_owned_apps_summary, get_user_daily_cost, get_user_monthly_summary

router = APIRouter()
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _common_ctx(request: Request, **extra) -> dict:
    """Base context shared by all pages."""
    # Build gateway base URL from Host header (reliable behind reverse proxy)
    host = request.headers.get("host", request.url.hostname or "localhost")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme or "http")
    gateway_base = f"{scheme}://{host}/v1"
    return {
        "request": request,
        "app_title": APP_TITLE,
        "current_year": datetime.now(timezone.utc).year,
        "gateway_base": gateway_base,
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Getting-started welcome page with API guide and available models."""

    display_name = user.display_name or user.username
    org_code = user.org_code

    # Group models by type for display (skip hidden models)
    models_by_type: dict[str, list[str]] = {}
    first_model = ""
    for alias, route in get_model_routing_snapshot().items():
        if route.get("hidden"):
            continue
        model_type = route["type"]
        if model_type not in models_by_type:
            models_by_type[model_type] = []
        models_by_type[model_type].append(alias)
        if not first_model and model_type in ("llm", "vlm"):
            first_model = alias

    return templates.TemplateResponse(
        "welcome.html",
        _common_ctx(
            request,
            title="Welcome",
            user=user,
            display_name=display_name,
            org_code=org_code,
            models_by_type=models_by_type,
            first_model=first_model or "your-model",
        ),
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):

    display_name = user.display_name or user.username
    org_code = user.org_code

    summary = get_user_monthly_summary(session, user.id)
    trend_data = get_daily_trends(session, user.id)
    owned_apps = get_owned_apps_summary(session, user.id)

    # Build grouped server status keyed by raw type (skip hidden models)
    server_groups: dict[str, list[dict]] = {}
    seen_urls: set[str] = set()
    for model_name, route in get_model_routing_snapshot().items():
        if route.get("hidden"):
            continue
        base_url = route["base_url"]
        model_type = route["type"]
        if model_type not in server_groups:
            server_groups[model_type] = []
        if base_url not in seen_urls:
            seen_urls.add(base_url)
            server_groups[model_type].append(
                {
                    "name": model_name,
                    "base_url": base_url,
                    "alive": is_alive(base_url),
                }
            )

    # Budget percentage based on today's cost vs daily limit
    today_cost = get_user_daily_cost(session, user.id)
    daily_limit = user.daily_limit_usd if user.daily_limit_usd > 0 else 1.0
    usage_percent = min(100.0, (today_cost / daily_limit) * 100)

    return templates.TemplateResponse(
        "dashboard.html",
        _common_ctx(
            request,
            title="Dashboard",
            user=user,
            display_name=display_name,
            org_code=org_code,
            total_reqs=summary["total_requests"],
            total_cost=summary["total_cost_usd"],
            my_input=summary["total_input_tokens"],
            my_output=summary["total_output_tokens"],
            today_cost=round(today_cost, 4),
            usage_percent=round(usage_percent, 1),
            trend_data=trend_data,
            now_utc=datetime.now(timezone.utc),
            owned_apps=owned_apps,
            server_groups=server_groups,
        ),
    )


@router.post("/dashboard/refresh-key")
async def refresh_own_key(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):

    # Re-fetch from DB for write operation (user was expunged in get_web_user)
    db_user = session.get(User, user.id)
    from app.models.schema import _generate_api_key
    db_user.api_key = _generate_api_key()
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return JSONResponse({"ok": True, "api_key": db_user.api_key})


@router.post("/dashboard/app/{app_id}/refresh-key")
async def refresh_owned_app_key(
    app_id: int,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Refresh API key for an app account owned by the current user."""

    target = session.exec(select(User).where(User.id == app_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="App account not found.")

    ownership = session.exec(
        select(AppOwner).where(AppOwner.app_id == app_id, AppOwner.owner_id == user.id)
    ).first()
    if not ownership:
        raise HTTPException(status_code=403, detail="You do not own this app account.")

    from app.models.schema import _generate_api_key
    target.api_key = _generate_api_key()
    session.add(target)
    session.commit()
    session.refresh(target)
    return JSONResponse({"ok": True, "api_key": target.api_key})
