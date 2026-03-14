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

from app.core.auth import get_web_user
from app.core.config import APP_TITLE, get_config_data, save_config
from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.schema import AppOwner, User, UsageLog
from app.services.stats import get_all_users_usage, get_dau_trends, get_department_usage, get_monthly_all_users_usage

router = APIRouter(prefix="/admin", tags=["admin"])
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _require_admin(
    request: Request, session: Session
) -> tuple[User, dict]:
    """Check JWT-based admin auth for web pages.

    Returns (user, jwt_payload).
    """
    user, scopes, payload = get_web_user(request, session)
    if "admin" not in scopes:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user, payload


# ── Web UI ──

@router.get("", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    session: Session = Depends(get_session),
):
    admin_user, payload = _require_admin(request, session)

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

    # Build user lookup and app->owners mapping
    user_lookup: dict[int, str] = {}
    potential_owners: list[dict] = []
    for u in all_users:
        user_lookup[u.id] = u.username
        if not u.username.startswith("app_"):
            potential_owners.append({"id": u.id, "username": u.username})

    # Build app_id -> list of owner usernames
    all_app_owners = session.exec(select(AppOwner)).all()
    app_owners_map: dict[int, list[str]] = {}
    for ao in all_app_owners:
        app_owners_map.setdefault(ao.app_id, []).append(
            user_lookup.get(ao.owner_id, str(ao.owner_id))
        )

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
            "owners": app_owners_map.get(u.id, []),
            "display_name": u.display_name,
            "org_code": u.org_code,
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

    dau_data = get_dau_trends(session)
    today_dau = dau_data[-1]["dau"] if dau_data else 0
    dept_usage = get_department_usage(session)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "title": "Admin Panel",
            "user": admin_user,
            "display_name": admin_user.display_name or admin_user.username,
            "org_code": admin_user.org_code,
            "users": users,
            "app_leaderboard": app_leaderboard,
            "user_leaderboard": user_leaderboard,
            "month_label": month_label,
            "monthly_total_cost": round(monthly_total_cost, 4),
            "monthly_total_input": monthly_total_input,
            "monthly_total_output": monthly_total_output,
            "monthly_total_reqs": monthly_total_reqs,
            "potential_owners": potential_owners,
            "dept_usage": dept_usage,
            "current_year": datetime.now(timezone.utc).year,
            "dau_data": dau_data,
            "today_dau": today_dau,
        },
    )


@router.post("/users/create")
async def create_app_account_web(
    request: Request,
    session: Session = Depends(get_session),
):
    """Create a new app account (web UI form)."""
    _require_admin(request, session)

    form = await request.form()
    username = (form.get("username") or "").strip()
    daily_limit = form.get("daily_limit_usd")
    owner_username = (form.get("owner_username") or "").strip()

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
    session.add(user)
    session.commit()
    session.refresh(user)

    # Add owner via AppOwner table
    if owner_username:
        owner = session.exec(select(User).where(User.username == owner_username)).first()
        if not owner:
            raise HTTPException(status_code=400, detail=f"Owner '{owner_username}' not found.")
        session.add(AppOwner(app_id=user.id, owner_id=owner.id))
        session.commit()

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/owner")
async def update_user_owner(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Replace app account owners (comma-separated usernames)."""
    _require_admin(request, session)

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    form = await request.form()
    raw = (form.get("owner_usernames") or "").strip()

    # Remove all existing owners for this app
    session.execute(delete(AppOwner).where(AppOwner.app_id == user_id))

    # Add new owners
    if raw:
        not_found = []
        for name in [n.strip() for n in raw.split(",") if n.strip()]:
            owner = session.exec(select(User).where(User.username == name)).first()
            if not owner:
                not_found.append(name)
            else:
                session.add(AppOwner(app_id=user_id, owner_id=owner.id))
        if not_found:
            raise HTTPException(
                status_code=400,
                detail=f"User(s) not found: {', '.join(not_found)}",
            )

    session.commit()

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/limit")
async def update_user_limit(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    _require_admin(request, session)

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
    admin_user, _payload = _require_admin(request, session)

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Prevent admin from deleting themselves
    if target.id == admin_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself.")

    # Bulk-delete ownership records, usage logs, then the user
    session.execute(delete(AppOwner).where(
        (AppOwner.app_id == user_id) | (AppOwner.owner_id == user_id)
    ))
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
    _require_admin(request, session)

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

    # Build owner lookup
    all_app_owners = session.exec(select(AppOwner)).all()
    user_lookup = {u.id: u.username for u in users}
    app_owners_map: dict[int, list[int]] = {}
    for ao in all_app_owners:
        app_owners_map.setdefault(ao.app_id, []).append(ao.owner_id)

    return [
        {
            "id": u.id,
            "username": u.username,
            "api_key": u.api_key,
            "daily_limit_usd": u.daily_limit_usd,
            "is_admin": u.is_admin,
            "owner_ids": app_owners_map.get(u.id, []),
            "display_name": u.display_name,
            "org_code": u.org_code,
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
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    # Add owners via AppOwner table
    owner_ids = body.get("owner_ids", [])
    if isinstance(owner_ids, list):
        for oid in owner_ids:
            owner = session.exec(select(User).where(User.id == int(oid))).first()
            if owner:
                session.add(AppOwner(app_id=new_user.id, owner_id=owner.id))
        session.commit()

    return {
        "ok": True,
        "id": new_user.id,
        "username": new_user.username,
        "api_key": new_user.api_key,
        "daily_limit_usd": new_user.daily_limit_usd,
        "owner_ids": owner_ids,
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
    if "owner_ids" in body:
        session.execute(delete(AppOwner).where(AppOwner.app_id == user_id))
        for oid in (body["owner_ids"] or []):
            owner = session.exec(select(User).where(User.id == int(oid))).first()
            if owner:
                session.add(AppOwner(app_id=user_id, owner_id=owner.id))

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
    admin_user, payload = _require_admin(request, session)
    return templates.TemplateResponse(
        "admin_models.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "title": "Model Configuration",
            "user": admin_user,
            "display_name": admin_user.display_name or admin_user.username,
            "org_code": admin_user.org_code,
            "current_year": datetime.now(timezone.utc).year,
        },
    )


@router.get("/api/config")
async def get_config_api(
    request: Request,
    session: Session = Depends(get_session),
):
    _require_admin(request, session)
    return JSONResponse(get_config_data())


@router.put("/api/config")
async def save_config_api(
    request: Request,
    session: Session = Depends(get_session),
):
    _require_admin(request, session)
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
