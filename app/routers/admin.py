"""
Admin panel: web UI + API endpoints for user management and model configuration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete
from sqlmodel import Session, func, select

from app.core.auth import get_web_user
from app.core.logger import logger
from app.core.config import (
    _MODEL_INTERNAL_KEYS,
    _MODEL_METADATA_KEYS,
    _MODEL_PRICING_KEYS,
    APP_TITLE,
    get_config_data,
    get_default_daily_limit,
    save_config,
    set_default_daily_limit,
)
from app.core.database import get_session
from app.models.schema import AppOwner, User, UsageLog
from app.services.monitor import add_monitor, list_monitored, remove_monitor, is_monitored
from app.services.analytics import build_monthly_report, iter_months, parse_ym
from app.services.stats import (
    get_all_users_usage,
    get_dau_trends,
    get_department_usage,
    get_leaderboard,
    get_monthly_all_users_usage,
    get_monthly_totals,
)
from app.services.usage_export import build_workbook

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Security(get_web_user, scopes=["admin"])],
)
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


# ── Web UI ──

_PAGE_SIZE = 15


@router.get("", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    app_limit: int = _PAGE_SIZE,
    app_offset: int = 0,
    q: str = "",
):

    # ── Monthly totals (single aggregate query) ──
    totals = get_monthly_totals(session)

    # ── Leaderboards (top-N queries) ──
    app_leaderboard = get_leaderboard(session, is_app=True, limit=10)
    user_leaderboard = get_leaderboard(session, is_app=False, limit=10)

    # ── User Management: paginated queries with optional search ──
    search = q.strip()
    user_base = select(User).where(~User.username.startswith("app_"))
    app_base = select(User).where(User.username.startswith("app_"))

    if search:
        like = f"%{search}%"
        search_filter = (
            User.username.ilike(like)
            | User.display_name.ilike(like)
            | User.org_code.ilike(like)
        )
        user_base = user_base.where(search_filter)
        app_base = app_base.where(search_filter)

    user_total = session.exec(select(func.count()).select_from(user_base.subquery())).one()
    app_total = session.exec(select(func.count()).select_from(app_base.subquery())).one()

    # Clamp offset
    offset = max(0, min(offset, max(user_total - 1, 0)))
    app_offset = max(0, min(app_offset, max(app_total - 1, 0)))

    paged_users = session.exec(
        user_base.order_by(User.id).offset(offset).limit(limit)
    ).all()
    paged_apps = session.exec(
        app_base.order_by(User.id).offset(app_offset).limit(app_limit)
    ).all()

    # Usage maps for paginated users only
    paged_ids = [u.id for u in paged_users] + [u.id for u in paged_apps]
    usage_map = get_all_users_usage(session)
    monthly_map = get_monthly_all_users_usage(session)

    # Owner lookup for app accounts
    all_app_owners = session.exec(select(AppOwner)).all()
    user_lookup: dict[int, str] = {}
    for u in paged_users:
        user_lookup[u.id] = u.username
    # Also need usernames of owners who may not be in the paged set
    owner_ids = {ao.owner_id for ao in all_app_owners}
    if owner_ids:
        owners = session.exec(select(User).where(User.id.in_(owner_ids))).all()
        for o in owners:
            user_lookup[o.id] = o.username

    app_owners_map: dict[int, list[str]] = {}
    for ao in all_app_owners:
        app_owners_map.setdefault(ao.app_id, []).append(
            user_lookup.get(ao.owner_id, str(ao.owner_id))
        )

    def _build_user_data(u: User) -> dict:
        usage = usage_map.get(u.id, {"total_cost": 0.0, "total_tokens": 0})
        monthly = monthly_map.get(u.id, {
            "cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0,
        })
        return {
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

    users = [_build_user_data(u) for u in paged_users]
    apps = [_build_user_data(u) for u in paged_apps]

    now = datetime.now(timezone.utc)
    month_label = now.strftime("%B %Y")

    dau_data = get_dau_trends(session)
    today_dau = dau_data[-1]["dau"] if dau_data else 0
    dept_usage = get_department_usage(session)

    # Currently monitored user IDs (for toggle button state)
    monitored_ids = {m["user_id"] for m in list_monitored()}

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
            "apps": apps,
            "app_leaderboard": app_leaderboard,
            "user_leaderboard": user_leaderboard,
            "month_label": month_label,
            "monthly_total_cost": totals["cost"],
            "monthly_total_input": totals["input_tokens"],
            "monthly_total_output": totals["output_tokens"],
            "monthly_total_reqs": totals["requests"],
            "dept_usage": dept_usage,
            "current_year": now.year,
            "dau_data": dau_data,
            "today_dau": today_dau,
            "monitored_ids": monitored_ids,
            "default_daily_limit": get_default_daily_limit(),
            # Pagination state
            "limit": limit,
            "offset": offset,
            "user_total": user_total,
            "app_limit": app_limit,
            "app_offset": app_offset,
            "app_total": app_total,
            "q": search,
        },
    )


@router.post("/users/create")
async def create_app_account_web(
    request: Request,
    session: Session = Depends(get_session),
):
    """Create a new app account (web UI form)."""

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

    return JSONResponse({"ok": True})


@router.post("/users/{user_id}/limit")
async def update_user_limit(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):

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


@router.post("/default-limit")
async def update_default_limit(
    request: Request,
    session: Session = Depends(get_session),
):
    """Set default daily limit. Bumps any user below the new floor up to it.

    Users with daily_limit_usd = 0 (unlimited) are never modified.
    Users already above the floor are never modified.
    """
    form = await request.form()
    raw_value = form.get("new_default")
    if raw_value is None:
        raise HTTPException(status_code=400, detail="Missing new_default.")
    try:
        new_default = float(raw_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="new_default must be a number.")
    if new_default < 0:
        raise HTTPException(status_code=400, detail="new_default must be non-negative.")

    set_default_daily_limit(new_default)

    # Bulk-bump users below the new floor (skip unlimited users).
    from sqlalchemy import update as sql_update
    stmt = (
        sql_update(User)
        .where(User.daily_limit_usd > 0)
        .where(User.daily_limit_usd < new_default)
        .values(daily_limit_usd=new_default)
    )
    result = session.exec(stmt)
    session.commit()
    bumped = result.rowcount or 0
    logger.info("Default daily limit set to ${} — bumped {} users", new_default, bumped)

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):

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
    session: Session = Depends(get_session),
):

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
    session: Session = Depends(get_session),
):
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
    session: Session = Depends(get_session),
):
    """Create a new user account via API (requires admin JWT)."""

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
    session: Session = Depends(get_session),
):
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
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
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
async def get_config_api():
    return JSONResponse(get_config_data())


@router.put("/api/config")
async def save_config_api(
    request: Request,
):
    body = await request.json()

    models = body.get("models")
    pricing = body.get("pricing")
    fallback = body.get("fallback")

    if not isinstance(models, dict) or not isinstance(pricing, dict):
        raise HTTPException(status_code=400, detail="Invalid config format.")

    # Validate model entries have required fields + sanity-check metadata
    _META_TYPES: dict[str, type | tuple[type, ...]] = {
        "display_name": str,
        "context_window": int,
        "max_output_tokens": int,
        "supports_tools": bool,
        "supports_vision": bool,
        "supports_prompt_caching": bool,
    }
    _INTERNAL_TYPES: dict[str, type | tuple[type, ...]] = {
        "hidden": bool,
    }
    for alias, info in models.items():
        if not isinstance(info, dict):
            raise HTTPException(status_code=400, detail=f"Invalid model entry: {alias}")
        if not info.get("base_url") or not info.get("type"):
            raise HTTPException(status_code=400, detail=f"Model '{alias}' missing base_url or type.")
        for key in _MODEL_METADATA_KEYS:
            if key not in info:
                continue
            expected = _META_TYPES[key]
            value = info[key]
            # JSON booleans arrive as Python bool which is also an int, so
            # check bool BEFORE int to avoid accepting True as context_window.
            if expected is int and isinstance(value, bool):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be an integer, got boolean.",
                )
            if not isinstance(value, expected):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' has wrong type.",
                )
            if expected is int and value < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be non-negative.",
                )
        for key in _MODEL_INTERNAL_KEYS:
            if key not in info:
                continue
            expected = _INTERNAL_TYPES[key]
            value = info[key]
            if not isinstance(value, expected):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' has wrong type.",
                )
        for key in _MODEL_PRICING_KEYS:
            if key not in info:
                continue
            value = info[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be a number.",
                )
            if value < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be non-negative.",
                )

    # Validate fallback values are strings
    if fallback and not all(isinstance(v, str) for v in fallback.values()):
        raise HTTPException(status_code=400, detail="Fallback values must be strings.")

    save_config(models, pricing, fallback or {})
    return JSONResponse({"ok": True})


# ── Usage Export ──

_MAX_EXPORT_MONTHS = 24


@router.get("/api/export/usage.xlsx")
async def export_usage_report(
    session: Session = Depends(get_session),
    from_: str = Query(..., alias="from", description="Start month YYYY-MM (inclusive)"),
    to: str = Query(..., description="End month YYYY-MM (inclusive)"),
):
    """Export a 4-sheet xlsx usage report for the given closed month range."""
    try:
        parse_ym(from_)
        parse_ym(to)
        months = iter_months(from_, to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if len(months) > _MAX_EXPORT_MONTHS:
        raise HTTPException(
            status_code=400,
            detail=f"Range too wide: {len(months)} months (max {_MAX_EXPORT_MONTHS}).",
        )

    report = build_monthly_report(session, from_, to)
    content = build_workbook(report)
    filename = f"usage_{from_}_to_{to}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Request Monitoring ──

@router.post("/users/{user_id}/monitor")
async def toggle_monitor(
    user_id: int,
    session: Session = Depends(get_session),
):
    """Toggle request monitoring for a user/app."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if is_monitored(user_id):
        remove_monitor(user_id)
        return JSONResponse({"ok": True, "monitoring": False, "username": target.username})
    else:
        add_monitor(user_id, target.username)
        return JSONResponse({"ok": True, "monitoring": True, "username": target.username})


@router.get("/monitor")
async def get_monitor_status():
    """Return currently monitored users with per-type request counts."""
    return JSONResponse({"monitored": list_monitored()})
