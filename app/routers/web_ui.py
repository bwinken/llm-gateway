"""
Web dashboard endpoints (Jinja2 HTML pages).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.models.schema import User

from app.core.config import APP_TITLE, MODEL_ROUTING
from app.core.database import get_session
from app.core.server_state import is_alive
from app.routers.auth_api import LOGIN_URL, get_session_user
from app.services.stats import get_daily_trends, get_owned_apps_summary, get_user_monthly_summary

router = APIRouter()
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _common_ctx(request: Request, **extra) -> dict:
    """Base context shared by all pages."""
    return {
        "request": request,
        "app_title": APP_TITLE,
        "current_year": datetime.now(timezone.utc).year,
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
async def login_page(request: Request, session: Session = Depends(get_session)):
    result = get_session_user(request, session)
    if result is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        _common_ctx(request, title="Sign In", login_url=LOGIN_URL),
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: Session = Depends(get_session),
):
    result = get_session_user(request, session)
    if result is None:
        return RedirectResponse(url="/", status_code=303)

    user, scopes = result
    if "read" not in scopes and "admin" not in scopes:
        raise HTTPException(status_code=403, detail="Insufficient scope: 'read' required.")
    session.expunge(user)
    user.is_admin = "admin" in scopes

    summary = get_user_monthly_summary(session, user.id)
    trend_data = get_daily_trends(session, user.id)
    owned_apps = get_owned_apps_summary(session, user.id)

    # Build grouped server status keyed by raw type
    server_groups: dict[str, list[dict]] = {}
    seen_urls: set[str] = set()
    for model_name, route in dict(MODEL_ROUTING).items():
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

    # Budget percentage (prevent division by zero)
    daily_limit = user.daily_limit_usd if user.daily_limit_usd > 0 else 1.0
    usage_percent = min(100.0, (summary["total_cost_usd"] / daily_limit) * 100)

    return templates.TemplateResponse(
        "dashboard.html",
        _common_ctx(
            request,
            title="Dashboard",
            user=user,
            total_reqs=summary["total_requests"],
            total_cost=summary["total_cost_usd"],
            my_input=summary["total_input_tokens"],
            my_output=summary["total_output_tokens"],
            usage_percent=round(usage_percent, 1),
            trend_data=trend_data,
            owned_apps=owned_apps,
            server_groups=server_groups,
        ),
    )


@router.post("/dashboard/refresh-key")
async def refresh_own_key(
    request: Request,
    session: Session = Depends(get_session),
):
    result = get_session_user(request, session)
    if result is None:
        raise HTTPException(status_code=401)
    user, scopes = result
    if "read" not in scopes and "admin" not in scopes:
        raise HTTPException(status_code=403, detail="Insufficient scope: 'read' required.")

    from app.models.schema import _generate_api_key
    user.api_key = _generate_api_key()
    session.add(user)
    session.commit()
    session.refresh(user)
    return JSONResponse({"ok": True, "api_key": user.api_key})


@router.post("/dashboard/app/{app_id}/refresh-key")
async def refresh_owned_app_key(
    app_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Refresh API key for an app account owned by the current user."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401)

    target = session.exec(select(User).where(User.id == app_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="App account not found.")

    if target.owner_id != user_id:
        raise HTTPException(status_code=403, detail="You do not own this app account.")

    target.api_key = f"sk-{uuid.uuid4().hex}"
    session.add(target)
    session.commit()
    session.refresh(target)
    return JSONResponse({"ok": True, "api_key": target.api_key})
