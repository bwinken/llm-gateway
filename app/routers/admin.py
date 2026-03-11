"""
Admin panel: web UI + API endpoints for user management and model configuration.
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

from app.core.config import get_config_data, save_config
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

    # Build owner lookup and potential owners list for the dropdown
    owner_lookup: dict[int | None, str] = {}
    potential_owners: list[dict] = []
    for u in all_users:
        if not u.username.startswith("app_"):
            potential_owners.append({"id": u.id, "username": u.username})
            owner_lookup[u.id] = u.username

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
            "owner_id": u.owner_id,
            "owner_name": owner_lookup.get(u.owner_id, ""),
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
            "potential_owners": potential_owners,
            "current_year": datetime.now(timezone.utc).year,
        },
    )


@router.post("/users/create")
async def create_app_account_web(
    request: Request,
    session: Session = Depends(get_session),
):
    """Create a new app account (web UI form)."""
    _require_admin_session(request, session)

    form = await request.form()
    username = (form.get("username") or "").strip()
    daily_limit = form.get("daily_limit_usd")
    owner_id = form.get("owner_id")

    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")
    if not username.startswith("app_"):
        username = f"app_{username}"

    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"User '{username}' already exists.")

    user = User(username=username)
    if daily_limit is not None:
        user.daily_limit_usd = float(daily_limit)
    if owner_id:
        owner = session.exec(select(User).where(User.id == int(owner_id))).first()
        if owner:
            user.owner_id = owner.id
    session.add(user)
    session.commit()

    return RedirectResponse(url="/admin", status_code=303)


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

    from app.models.schema import _generate_api_key
    target.api_key = _generate_api_key()
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
            "owner_id": u.owner_id,
        }
        for u in users
    ]


@router.post("/users")
async def create_user_api(
    body: dict,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Create a new user account via API (requires admin Bearer token)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")

    username = (body.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")

    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"User '{username}' already exists.")

    new_user = User(username=username)
    if "daily_limit_usd" in body:
        new_user.daily_limit_usd = float(body["daily_limit_usd"])
    if "is_admin" in body:
        new_user.is_admin = bool(body["is_admin"])
    if "owner_id" in body:
        owner = session.exec(select(User).where(User.id == int(body["owner_id"]))).first()
        if not owner:
            raise HTTPException(status_code=400, detail="Owner user not found.")
        new_user.owner_id = owner.id
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    return {
        "ok": True,
        "id": new_user.id,
        "username": new_user.username,
        "api_key": new_user.api_key,
        "daily_limit_usd": new_user.daily_limit_usd,
        "owner_id": new_user.owner_id,
    }


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
    if "owner_id" in body:
        if body["owner_id"] is None:
            target.owner_id = None
        else:
            owner = session.exec(select(User).where(User.id == int(body["owner_id"]))).first()
            if not owner:
                raise HTTPException(status_code=400, detail="Owner user not found.")
            target.owner_id = owner.id

    session.add(target)
    session.commit()
    session.refresh(target)
    return {"ok": True, "user_id": target.id}


# ── Model Configuration ──

@router.get("/models", response_class=HTMLResponse)
async def admin_models_page(
    request: Request,
    session: Session = Depends(get_session),
):
    admin_user = _require_admin_session(request, session)
    return templates.TemplateResponse(
        "admin_models.html",
        {
            "request": request,
            "title": "Model Configuration",
            "user": admin_user,
            "current_year": datetime.now(timezone.utc).year,
        },
    )


@router.get("/api/config")
async def get_config_api(
    request: Request,
    session: Session = Depends(get_session),
):
    _require_admin_session(request, session)
    return JSONResponse(get_config_data())


@router.put("/api/config")
async def save_config_api(
    request: Request,
    session: Session = Depends(get_session),
):
    _require_admin_session(request, session)
    body = await request.json()

    models = body.get("models")
    pricing = body.get("pricing")
    fallback = body.get("fallback")

    if not isinstance(models, dict) or not isinstance(pricing, dict):
        raise HTTPException(status_code=400, detail="Invalid config format.")

    # Validate model entries have required fields
    for alias, info in models.items():
        if not isinstance(info, dict):
            raise HTTPException(status_code=400, detail=f"Invalid model entry: {alias}")
        if not info.get("base_url") or not info.get("type"):
            raise HTTPException(status_code=400, detail=f"Model '{alias}' missing base_url or type.")

    # Validate fallback values are strings
    if fallback and not all(isinstance(v, str) for v in fallback.values()):
        raise HTTPException(status_code=400, detail="Fallback values must be strings.")

    save_config(models, pricing, fallback or {})
    return JSONResponse({"ok": True})
