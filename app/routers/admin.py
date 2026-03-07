"""
Admin panel: web UI + API endpoints for user management.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.schema import User, UsageLog
from app.services.stats import get_all_users_usage, get_monthly_all_users_usage

router = APIRouter(prefix="/admin", tags=["admin"])
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _require_admin_session(request: Request, session: Session) -> User:
    """Check session-based admin auth for web pages (uses JWT scopes)."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    user = session.exec(select(User).where(User.id == user_id)).first()
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    scopes = request.session.get("scopes", [])
    if "admin" not in scopes:
        raise HTTPException(status_code=403, detail="Admin access required.")
    session.expunge(user)
    user.is_admin = True
    return user


# ── Web UI ──

@router.get("", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    session: Session = Depends(get_session),
):
    admin_user = _require_admin_session(request, session)

    all_users = session.exec(select(User).order_by(User.id)).all()
    usage_map = get_all_users_usage(session)
    monthly_map = get_monthly_all_users_usage(session)

    users = []
    app_leaderboard = []
    user_leaderboard = []
    monthly_total_cost = 0.0
    monthly_total_input = 0
    monthly_total_output = 0
    monthly_total_reqs = 0

    for u in all_users:
        usage = usage_map.get(u.id, {"total_cost": 0.0, "total_tokens": 0})
        monthly = monthly_map.get(u.id, {
            "cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0,
        })

        user_data = {
            "id": u.id,
            "username": u.username,
            "api_key": u.api_key,
            "daily_limit_usd": u.daily_limit_usd,
            "is_admin": u.is_admin,
            "total_cost": usage["total_cost"],
            "total_tokens": usage["total_tokens"],
            "monthly_cost": monthly["cost"],
            "monthly_input": monthly["input_tokens"],
            "monthly_output": monthly["output_tokens"],
            "monthly_reqs": monthly["requests"],
        }
        users.append(user_data)

        monthly_total_cost += monthly["cost"]
        monthly_total_input += monthly["input_tokens"]
        monthly_total_output += monthly["output_tokens"]
        monthly_total_reqs += monthly["requests"]

        if u.username.startswith("app_"):
            app_leaderboard.append(user_data)
        else:
            user_leaderboard.append(user_data)

    # Sort leaderboards by monthly cost descending
    app_leaderboard.sort(key=lambda x: x["monthly_cost"], reverse=True)
    user_leaderboard.sort(key=lambda x: x["monthly_cost"], reverse=True)

    now = datetime.now(timezone.utc)
    month_label = now.strftime("%B %Y")

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "title": "Admin Panel",
            "user": admin_user,
            "users": users,
            "app_leaderboard": app_leaderboard,
            "user_leaderboard": user_leaderboard,
            "month_label": month_label,
            "monthly_total_cost": round(monthly_total_cost, 4),
            "monthly_total_input": monthly_total_input,
            "monthly_total_output": monthly_total_output,
            "monthly_total_reqs": monthly_total_reqs,
            "current_year": datetime.now(timezone.utc).year,
        },
    )


@router.post("/users/{user_id}/limit")
async def update_user_limit(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    _require_admin_session(request, session)

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    form = await request.form()
    new_limit = form.get("new_limit")
    if new_limit is not None:
        target.daily_limit_usd = float(new_limit)
        session.add(target)
        session.commit()

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    admin_user = _require_admin_session(request, session)

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Prevent admin from deleting themselves
    if target.id == admin_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself.")

    # Bulk-delete usage logs, then the user
    session.execute(delete(UsageLog).where(UsageLog.user_id == user_id))
    session.delete(target)
    session.commit()

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/refresh-key")
async def refresh_user_key(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    _require_admin_session(request, session)

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    target.api_key = f"sk-{uuid.uuid4().hex}"
    session.add(target)
    session.commit()
    session.refresh(target)

    return JSONResponse({"ok": True, "api_key": target.api_key})


# ── API endpoints (Bearer token auth) ──

@router.get("/users")
async def list_users_api(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    users = session.exec(select(User)).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "api_key": u.api_key,
            "daily_limit_usd": u.daily_limit_usd,
            "is_admin": u.is_admin,
        }
        for u in users
    ]


@router.patch("/users/{user_id}")
async def update_user_api(
    user_id: int,
    body: dict,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if "daily_limit_usd" in body:
        target.daily_limit_usd = float(body["daily_limit_usd"])
    if "is_admin" in body:
        target.is_admin = bool(body["is_admin"])

    session.add(target)
    session.commit()
    session.refresh(target)
    return {"ok": True, "user_id": target.id}
