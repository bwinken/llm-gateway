"""
Admin panel: web UI + API endpoints for user management and model configuration.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, update
from sqlmodel import Session, func, select

from app.core.auth import get_web_user
from app.core.logger import logger
from app.core.config import (
    _MODEL_INTERNAL_KEYS,
    _MODEL_METADATA_KEYS,
    _MODEL_PRICING_KEYS,
    APP_TITLE,
    CONCURRENCY_MODES,
    DISPLAY_ORDER_BACKENDS,
    GUARDRAIL_ACTIONS,
    GUARDRAIL_BLOCK_MESSAGE_MAX,
    GUARDRAIL_CATEGORY_RULES,
    GUARDRAIL_ENDPOINTS,
    GUARDRAIL_MAX_CHARS_RANGE,
    GUARDRAIL_SCOPES,
    GUARDRAIL_THRESHOLDS,
    GUARDRAIL_TIMEOUT_RANGE_S,
    IP_ALLOWLIST_MODES,
    bedrock_shared_settings,
    get_cloud_budget_fallback,
    get_cloud_daily_limit,
    get_concurrency_settings,
    get_azure_models_snapshot,
    get_bedrock_models_snapshot,
    masked_config_data,
    resolve_secret,
    resolve_secret_refs,
    get_default_daily_limit,
    get_guardrail_settings,
    get_model_routing_snapshot,
    get_site_links,
    save_config,
    set_cloud_budget_fallback,
    set_concurrency_settings,
    set_cloud_daily_limit,
    set_default_daily_limit,
    set_guardrail_settings,
    set_ip_allowlist_settings,
    set_site_links,
)
from app.core.database import get_session
from app.core.timeutil import LOCAL_TZ, local_month_start
from app.models.schema import (
    AnomalyEvent,
    AppOwner,
    IpAllowRule,
    User,
    UsageLog,
    app_account_clause,
    is_app_account,
    is_legacy_api_key,
    legacy_api_key_clause,
    mask_api_key,
)
from app.services import concurrency, guardrail, ip_allowlist
from app.services.analytics import build_monthly_report, iter_months, parse_ym, user_backend_costs
from app.core.server_state import get_metrics, is_alive
from app.services.stats import (
    get_budget_pressure,
    get_today_totals,
    summarize_server_status,
    get_dau_trends,
    get_monthly_all_users_usage,
    get_monthly_overview,
)
from app.services.usage_export import build_workbook

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Security(get_web_user, scopes=["admin"])],
)
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
# base.html reads the admin-editable links (support bot / install guide) on
# every page — same global as web_ui's template environment.
templates.env.globals["get_site_links"] = get_site_links


# ── Web UI ──

_PAGE_SIZE = 15


# The per-backend (Azure / Bedrock) amounts are gone: cloud spend has one
# cap for both, cloud_daily_limit_usd. A script still sending them gets told
# rather than silently ignored.
_RETIRED_LIMIT_FIELDS: tuple[str, ...] = (
    "azure_daily_limit_usd", "bedrock_daily_limit_usd",
)


def _reject_retired_limits(body: dict) -> None:
    retired = [f for f in _RETIRED_LIMIT_FIELDS if f in body]
    if retired:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{', '.join(retired)} no longer exist: Azure and AWS share one cloud "
                "cap, cloud_daily_limit_usd (per user; blank follows the default "
                "in admin panel → Cloud Daily Limit)."
            ),
        )


def _limit_usd(raw: object, field: str) -> float:
    """A daily limit (or the cloud cap) in USD: a finite number >= 0.

    There is no "unlimited": 0 is a $0 budget (every request refused), and
    negative / inf / nan are rejected rather than quietly meaning "no cap".
    """
    if isinstance(raw, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a number.")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number.")
    if not math.isfinite(value) or value < 0:
        raise HTTPException(status_code=400, detail=f"{field} must be a number >= 0.")
    return value


# ── Users / Apps tables ──
#
# The two tables in the Users tab render from one partial
# (_admin_user_table.html). /admin renders both inside the full page;
# /admin/user-table renders one of them alone, so searching, sorting,
# filtering, paging and refreshing after a delete swap just that panel
# instead of re-running every report on the page. Both read the same query
# parameters, so a panel link is also a working /admin URL without JS.

_MAX_PAGE_SIZE = 200
USER_TABLE_SORTS: dict[str, str] = {
    "id": "Oldest first",
    "name": "Username",
    "spend": "Spend this month",
}
USER_TABLE_STATUSES: dict[str, str] = {
    "all": "All accounts",
    "special": "Any special access",
    "admin": "Admin",
    "azure": "Azure access",
    "bedrock": "AWS access",
    "cloud": "Cloud access",
    "cloud_cap": "Own cloud cap",
    "waived": "Concurrency waived",
    "waiver_changed": "Waiver not the default",
    "disabled": "Disabled",
    # Not a permission: tracks how many keys still predate the current format
    # (schema.is_legacy_api_key). Deliberately not in SPECIAL_ACCESS_KEYS.
    "legacy_key": "Older-format API key",
    # Access review: keys that have not authenticated in STALE_KEY_DAYS.
    "stale_key": "Key unused 90+ days",
}
STALE_KEY_DAYS = 90
# The "Special access" overview above the tables: one count per permission,
# each linking to its filter. The waiver counts only what differs from an
# account kind's default (users are created limited, app accounts waived).
SPECIAL_ACCESS_KEYS: tuple[str, ...] = (
    "admin", "azure", "bedrock", "cloud_cap", "waiver_changed", "disabled",
)


def _stale_key_clause():
    """Keys unused for STALE_KEY_DAYS: last use before the cutoff, or never
    used and issued before it (a key issued yesterday is not stale)."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=STALE_KEY_DAYS)
    used = User.api_key_last_used_at
    issued = User.api_key_created_at
    return (used < cutoff) | (  # type: ignore[operator]
        used.is_(None) & (issued.is_(None) | (issued < cutoff))  # type: ignore[union-attr,operator]
    )


def _is_stale(u: User, now: datetime) -> bool:
    cutoff = now - timedelta(days=STALE_KEY_DAYS)
    if u.api_key_last_used_at is not None:
        return u.api_key_last_used_at < cutoff
    return u.api_key_created_at is None or u.api_key_created_at < cutoff


def _key_use_label(last_used: datetime | None, now: datetime) -> str:
    if last_used is None:
        return "never used"
    days = (now - last_used).days
    if days <= 0:
        return "used today"
    return "used yesterday" if days == 1 else f"used {days} d ago"


def _status_clause(status: str, is_apps: bool):
    """The WHERE clause of a Users / Apps table filter (None for "all").
    ``special`` is every account with something other than the defaults of
    its kind: a flag granted, its own cloud cap, or the concurrency waiver
    flipped from what new accounts of that kind get."""
    waived_differs = (User.concurrency_waived == False) if is_apps else (User.concurrency_waived == True)  # noqa: E712
    clauses = {
        "admin": User.is_admin == True,  # noqa: E712
        "azure": User.can_use_azure == True,  # noqa: E712
        "bedrock": User.can_use_bedrock == True,  # noqa: E712
        "cloud": (User.can_use_azure == True) | (User.can_use_bedrock == True),  # noqa: E712
        "cloud_cap": User.cloud_daily_limit_usd.is_not(None),  # type: ignore[union-attr]
        "waived": User.concurrency_waived == True,  # noqa: E712
        "waiver_changed": waived_differs,
        "disabled": User.is_disabled == True,  # noqa: E712
        "legacy_key": legacy_api_key_clause(),
        "stale_key": _stale_key_clause(),
    }
    if status == "special":
        return (
            clauses["admin"] | clauses["cloud"] | clauses["cloud_cap"] | waived_differs
        )
    return clauses.get(status)


def _special_access_summary(session: Session) -> list[dict]:
    """Counts for the overview: ``[{key, label, users, apps, url}]``, from one
    aggregate per account kind."""
    keys = ("special", *SPECIAL_ACCESS_KEYS)
    counts: dict[str, dict[str, int]] = {key: {} for key in keys}
    for kind, is_apps in (("users", False), ("apps", True)):
        kind_clause = app_account_clause() if is_apps else ~app_account_clause()
        row = session.exec(
            select(*[
                func.coalesce(func.sum(case((_status_clause(key, is_apps), 1), else_=0)), 0)
                for key in keys
            ]).select_from(User).where(kind_clause)
        ).one()
        for key, n in zip(keys, row):
            counts[key][kind] = int(n)
    return [
        {"key": key, "label": USER_TABLE_STATUSES[key], **counts[key],
         "url": _admin_url(_STATE_DEFAULTS, status=key)}
        for key in keys
    ]


def _table_state(
    q: str = "",
    sort: str = "id",
    status: str = "all",
    offset: int = 0,
    limit: int = _PAGE_SIZE,
    app_offset: int = 0,
    app_limit: int = _PAGE_SIZE,
) -> dict:
    """The Users-tab query parameters, normalized (unknown values → defaults)."""
    return {
        "q": q.strip(),
        "sort": sort if sort in USER_TABLE_SORTS else "id",
        "status": status if status in USER_TABLE_STATUSES else "all",
        "offset": max(0, offset),
        "limit": min(max(1, limit), _MAX_PAGE_SIZE),
        "app_offset": max(0, app_offset),
        "app_limit": min(max(1, app_limit), _MAX_PAGE_SIZE),
    }


_STATE_DEFAULTS = _table_state()


def _admin_url(state: dict, **overrides) -> str:
    """``/admin?…#users`` for ``state`` with ``overrides``, URL-encoded and
    without parameters left at their defaults."""
    merged = {**state, **overrides}
    params = {k: v for k, v in merged.items() if v != _STATE_DEFAULTS[k]}
    return "/admin" + (f"?{urlencode(params)}" if params else "") + "#users"


def _user_table(
    session: Session,
    kind: str,
    state: dict,
    admin_user: User,
    monthly_map: dict[int, dict] | None = None,
) -> dict:
    """One page of the Users or Apps table, ready for the partial.

    ``monthly_map`` is the month's per-user usage when the caller already has
    it (the full admin page does); otherwise only this page's accounts are
    aggregated, or every account when sorting by spend."""
    is_apps = kind == "apps"
    offset_key, limit_key = ("app_offset", "app_limit") if is_apps else ("offset", "limit")
    limit = state[limit_key]

    clauses = [app_account_clause() if is_apps else ~app_account_clause()]
    if state["q"]:
        like = f"%{state['q']}%"
        clauses.append(
            User.username.ilike(like)  # type: ignore[attr-defined]
            | User.display_name.ilike(like)  # type: ignore[union-attr]
            | User.org_code.ilike(like)  # type: ignore[union-attr]
        )
    status = state["status"]
    status_clause = _status_clause(status, is_apps)
    if status_clause is not None:
        clauses.append(status_clause)

    total = session.exec(select(func.count()).select_from(User).where(*clauses)).one()
    offset = max(0, min(state[offset_key], max(total - 1, 0)))

    if state["sort"] == "spend":
        if monthly_map is None:
            monthly_map = get_monthly_all_users_usage(session)
        ids = list(session.exec(select(User.id).where(*clauses)).all())
        ids.sort(key=lambda i: (-monthly_map.get(i, {}).get("cost", 0.0), i))
        page_ids = ids[offset:offset + limit]
        by_id = {
            u.id: u for u in session.exec(select(User).where(User.id.in_(page_ids))).all()  # type: ignore[union-attr]
        } if page_ids else {}
        page = [by_id[i] for i in page_ids if i in by_id]
    else:
        order = (User.username, User.id) if state["sort"] == "name" else (User.id,)
        page = list(session.exec(
            select(User).where(*clauses).order_by(*order).offset(offset).limit(limit)
        ).all())
        if monthly_map is None:
            monthly_map = get_monthly_all_users_usage(session, user_ids=[u.id for u in page])

    # Owners of the apps on this page only.
    owners: dict[int, list[str]] = {}
    if is_apps and page:
        links = session.exec(
            select(AppOwner.app_id, User.username)
            .join(User, User.id == AppOwner.owner_id)  # type: ignore[arg-type]
            .where(AppOwner.app_id.in_([u.id for u in page]))  # type: ignore[attr-defined]
            .order_by(User.username)
        ).all()
        for app_id, owner_name in links:
            owners.setdefault(int(app_id), []).append(str(owner_name))

    empty = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0}
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for u in page:
        monthly = monthly_map.get(u.id, empty)
        rows.append({
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "org_code": u.org_code,
            "daily_limit_usd": u.daily_limit_usd,
            "cloud_daily_limit_usd": u.cloud_daily_limit_usd,
            "is_admin": u.is_admin,
            "is_self": u.id == admin_user.id,
            "is_disabled": u.is_disabled,
            "can_use_azure": u.can_use_azure,
            "can_use_bedrock": u.can_use_bedrock,
            "concurrency_waived": u.concurrency_waived,
            "key_legacy": is_legacy_api_key(u.api_key),
            "key_use": _key_use_label(u.api_key_last_used_at, now_naive),
            "key_stale": _is_stale(u, now_naive),
            "key_detail": (
                f"Key issued {_local_label(u.api_key_created_at) or 'before tracking began'}; "
                f"last used {_local_label(u.api_key_last_used_at) or 'never'} (Asia/Taipei)."
            ),
            "owners": owners.get(u.id, []),
            "monthly_cost": monthly["cost"],
            "monthly_tokens": monthly["input_tokens"] + monthly["output_tokens"],
            "monthly_reqs": monthly["requests"],
        })

    return {
        "kind": kind,
        "rows": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "first": offset + 1 if total else 0,
        "last": min(offset + limit, total),
        "prev_url": _admin_url(state, **{offset_key: max(offset - limit, 0)}) if offset > 0 else None,
        "next_url": _admin_url(state, **{offset_key: offset + limit}) if offset + limit < total else None,
        "filtered": bool(state["q"]) or status != "all",
    }


@router.get("", response_class=HTMLResponse)
def admin_page(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    app_limit: int = _PAGE_SIZE,
    app_offset: int = 0,
    q: str = "",
    sort: str = "id",
    status: str = "all",
):
    # Plain ``def``: FastAPI runs it in the threadpool. The aggregates below
    # take seconds on a busy month, and inside an ``async def`` they would
    # block the event loop, stalling every stream this worker is serving.

    # ── Month: totals, leaderboards, per-user and department usage from one
    # aggregate over usage_logs ──
    overview = get_monthly_overview(session)
    totals = overview["totals"]
    app_leaderboard = overview["app_leaderboard"]
    user_leaderboard = overview["user_leaderboard"]

    # ── User Management: both tables, reusing the month's per-user map ──
    state = _table_state(q, sort, status, offset, limit, app_offset, app_limit)
    users_table = _user_table(session, "users", state, admin_user, overview["per_user"])
    apps_table = _user_table(session, "apps", state, admin_user, overview["per_user"])

    # Report months are local (Asia/Taipei) months, like the figures under
    # them: a UTC label named the previous month for the first 8 hours.
    month_start = local_month_start()
    month_label = month_start.strftime("%B %Y")
    export_from = month_start
    for _ in range(5):
        export_from = (export_from - timedelta(days=1)).replace(day=1)

    dau_data = get_dau_trends(session)
    today_dau = dau_data[-1]["dau"] if dau_data else 0
    dept_usage = overview["departments"]

    # Open anomaly findings (populated by the gateway's hourly anomaly scan,
    # app/services/anomaly_scan.py — the card renders only when non-empty).
    anomaly_rows = session.exec(
        select(AnomalyEvent)
        .where(AnomalyEvent.status.in_(("new", "acknowledged")))  # type: ignore[attr-defined]
        .order_by(AnomalyEvent.status, AnomalyEvent.ratio.desc())  # type: ignore[attr-defined]
        .limit(20)
    ).all()
    anomaly_usernames: dict[int, str] = {}
    uids = [e.user_id for e in anomaly_rows if e.user_id is not None]
    if uids:
        for uid, uname in session.exec(select(User.id, User.username).where(User.id.in_(uids))).all():  # type: ignore[attr-defined]
            anomaly_usernames[int(uid)] = str(uname)
    import json as _json
    anomalies = []
    for e in anomaly_rows:
        try:
            summary = _json.loads(e.detail).get("summary", "")
        except Exception:
            summary = ""
        anomalies.append({
            "id": e.id, "rule": e.rule, "severity": e.severity, "status": e.status,
            "user_id": e.user_id, "model": e.model, "scope": e.scope,
            "observed": e.observed, "baseline": e.baseline, "ratio": e.ratio,
            "summary": summary,
        })

    # "Needs attention": what an admin should act on first, computed here so
    # the page can lead with it instead of burying it under the analytics.
    today_totals = get_today_totals(session)
    budget_pressure = get_budget_pressure(session)
    system_status = summarize_server_status([
        {"name": alias, "alive": is_alive(route["base_url"]),
         "waiting": (get_metrics(route["base_url"]) or {}).get("waiting")}
        for alias, route in get_model_routing_snapshot().items()
        if not route.get("hidden")
    ])

    concurrency_mode, concurrency_limit = get_concurrency_settings()
    try:
        concurrency_in_flight = concurrency.in_flight_summary()
    except Exception as exc:  # the card must not take the admin page down
        logger.warning("Concurrency in-flight summary failed | error={}: {}", type(exc).__name__, exc)
        concurrency_in_flight = None

    try:
        ip_allowlist_view = _ip_allowlist_view(session, request, admin_user)
    except Exception as exc:  # e.g. before `alembic upgrade`: the card says so, the page still renders
        session.rollback()
        logger.warning("IP allowlist view failed | error={}: {}", type(exc).__name__, exc)
        ip_allowlist_view = None

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "title": "Admin Panel",
            "user": admin_user,
            "display_name": admin_user.display_name or admin_user.username,
            "org_code": admin_user.org_code,
            "users_table": users_table,
            "apps_table": apps_table,
            "table_state": state,
            "user_table_sorts": USER_TABLE_SORTS,
            "user_table_statuses": USER_TABLE_STATUSES,
            "export_from_default": export_from.strftime("%Y-%m"),
            "export_to_default": month_start.strftime("%Y-%m"),
            "app_leaderboard": app_leaderboard,
            "user_leaderboard": user_leaderboard,
            "month_label": month_label,
            "monthly_total_cost": totals["cost"],
            "monthly_total_input": totals["input_tokens"],
            "monthly_total_output": totals["output_tokens"],
            "monthly_total_reqs": totals["requests"],
            "dept_usage": dept_usage,
            "anomalies": anomalies,
            "anomaly_usernames": anomaly_usernames,
            "current_year": month_start.year,
            "dau_data": dau_data,
            "today_dau": today_dau,
            "default_daily_limit": get_default_daily_limit(),
            "cloud_daily_limit": get_cloud_daily_limit(),
            "cloud_budget_fallback": get_cloud_budget_fallback(),
            "today_totals": today_totals,
            "budget_pressure": budget_pressure,
            "system_status": system_status,
            "concurrency_mode": concurrency_mode,
            "concurrency_limit": concurrency_limit,
            "concurrency_modes": CONCURRENCY_MODES,
            "concurrency_in_flight": concurrency_in_flight,
            "site_links": get_site_links(),
            "guardrail": _guardrail_view(),
            "ip_allowlist": ip_allowlist_view,
            "special_access": _special_access_summary(session),
            "guardrail_categories": guardrail.QWEN3GUARD_CATEGORIES,
            "guardrail_model_family": guardrail.GUARD_MODEL_FAMILY,
            "guardrail_endpoints": GUARDRAIL_ENDPOINTS,
            "guardrail_rules": GUARDRAIL_CATEGORY_RULES,
            "user_total": users_table["total"],
            "app_total": apps_table["total"],
        },
    )


@router.get("/user-table", response_class=HTMLResponse)
def user_table_panel(
    request: Request,
    kind: str = Query(..., pattern="^(users|apps)$"),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    app_limit: int = _PAGE_SIZE,
    app_offset: int = 0,
    q: str = "",
    sort: str = "id",
    status: str = "all",
):
    """One Users-tab panel (``kind`` = users | apps) as an HTML fragment.

    Takes the same query parameters as ``/admin``, whose page swaps the
    fragment in. Plain ``def`` for the same reason as ``admin_page``."""
    state = _table_state(q, sort, status, offset, limit, app_offset, app_limit)
    return templates.TemplateResponse(
        "_admin_user_table.html",
        {
            "request": request,
            "table": _user_table(session, kind, state, admin_user),
            "cloud_daily_limit": get_cloud_daily_limit(),
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

    user = User(username=username, concurrency_waived=True)  # app accounts start waived
    if daily_limit is not None:
        user.daily_limit_usd = _limit_usd(daily_limit, "daily_limit_usd")
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
        target.daily_limit_usd = _limit_usd(new_limit, "new_limit")
        session.add(target)
        session.commit()

    if _wants_json(request):
        return JSONResponse({"ok": True, "daily_limit_usd": target.daily_limit_usd})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/cloud-limit")
async def update_user_cloud_limit(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Set the user's own cloud cap (Azure + Bedrock per day).

    Empty value clears it (NULL → the user follows the gateway-wide default,
    [app].cloud_daily_limit_usd); 0 is a $0 cap.
    """
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    form = await request.form()
    raw = form.get("new_cloud_limit")
    if raw is not None:
        raw = str(raw).strip()
        if raw == "":
            target.cloud_daily_limit_usd = None
        else:
            target.cloud_daily_limit_usd = _limit_usd(raw, "new_cloud_limit")
        session.add(target)
        session.commit()

    if _wants_json(request):
        return JSONResponse({"ok": True, "cloud_daily_limit_usd": target.cloud_daily_limit_usd})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/default-limit")
async def update_default_limit(
    request: Request,
    session: Session = Depends(get_session),
):
    """Set default daily limit. Bumps any user below the new floor up to it.

    Users with daily_limit_usd = 0 (a deliberate $0 budget) are never modified.
    Users already above the floor are never modified.
    """
    form = await request.form()
    raw_value = form.get("new_default")
    if raw_value is None:
        raise HTTPException(status_code=400, detail="Missing new_default.")
    new_default = _limit_usd(raw_value, "new_default")

    set_default_daily_limit(new_default)

    # Bulk-bump users below the new floor (skip $0 accounts: blocked on purpose).
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


@router.post("/cloud-limit")
async def update_cloud_limit(request: Request):
    """Set the default cloud cap: each user's Azure + Bedrock spend per day,
    for every user with Azure or AWS access and no cap of their own
    (users.cloud_daily_limit_usd). Blank removes the default; 0 is a $0 cap."""
    form = await request.form()
    raw = str(form.get("new_cloud_limit") or "").strip()
    value = None if raw == "" else _limit_usd(raw, "new_cloud_limit")
    set_cloud_daily_limit(value)
    logger.info("Cloud daily limit set to {}", "none" if value is None else f"${value}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/site-links")
async def update_site_links(request: Request):
    """Set the web-UI site links (support bot / install guide).

    Both URLs are stored in config.toml [app]. An empty value hides the
    corresponding UI element (floating support-bot button / navbar
    "Install Guide" link). Only http(s) or site-relative URLs are accepted
    so an admin typo can never inject a javascript: link into every page.
    """
    form = await request.form()

    def _clean_url(field: str) -> str:
        value = str(form.get(field) or "").strip()
        if value and not value.lower().startswith(("http://", "https://", "/")):
            raise HTTPException(
                status_code=400,
                detail=f"{field} must start with http://, https:// or /.",
            )
        return value

    support_bot_url = _clean_url("support_bot_url")
    install_guide_url = _clean_url("install_guide_url")
    set_site_links(support_bot_url=support_bot_url, install_guide_url=install_guide_url)
    logger.info(
        "Site links updated | support_bot_url={} install_guide_url={}",
        support_bot_url or "(hidden)", install_guide_url or "(hidden)",
    )

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/cloud-budget-fallback")
async def update_cloud_budget_fallback(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Enable/disable the cloud-budget fallback (``[app].cloud_budget_fallback``).

    When on, a ``/v1/*`` request for an Azure/Bedrock alias whose daily
    cloud cap is exhausted is served by the on-prem vLLM default
    instead of being refused with 429. The overall ``daily_limit_usd`` is
    unaffected — once that is gone the request is refused as before.

    Form field ``enabled``: ``on`` / ``true`` / ``1`` → enabled; anything
    else (including the field being absent, as an unchecked checkbox is) →
    disabled. Returns JSON when the client asks for it (the admin page
    toggles in place); otherwise redirects back to ``/admin``.
    """
    form = await request.form()
    raw = str(form.get("enabled") or "").strip().lower()
    enabled = raw in ("on", "true", "1", "yes")
    set_cloud_budget_fallback(enabled)
    logger.info(
        "Cloud budget fallback {} | admin={}",
        "enabled" if enabled else "disabled", admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "cloud_budget_fallback": enabled})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/concurrency-limit")
async def update_concurrency_limit(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Set the per-user concurrency limit (``[app].concurrency_limit_mode`` /
    ``concurrency_limit``).

    Form fields: ``mode`` — ``off`` / ``monitor`` / ``enforce``; ``limit`` — an
    integer >= 1 (required in every mode, so switching back on later doesn't
    silently come up with a stale or missing number). "Unlimited" is
    ``mode=off``, never a magic limit value. Takes effect on the next request
    in every worker (config auto-reload); nothing in flight is interrupted.
    """
    form = await request.form()
    mode = str(form.get("mode") or "").strip().lower()
    if mode not in CONCURRENCY_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of: {', '.join(CONCURRENCY_MODES)}.",
        )
    raw_limit = str(form.get("limit") or "").strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        raise HTTPException(status_code=400, detail="limit must be a whole number.")
    if limit < 1:
        raise HTTPException(
            status_code=400,
            detail="limit must be at least 1 (use mode 'off' for no limit).",
        )
    set_concurrency_settings(mode, limit)
    logger.info(
        "Concurrency limit set | mode={} limit={} admin={}", mode, limit, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "mode": mode, "limit": limit})
    return RedirectResponse(url="/admin", status_code=303)


# ── API IP allowlist ──

_LOCAL_INPUT_FORMAT = "%Y-%m-%dT%H:%M"


def _local_input_to_utc(field: str, text: str) -> datetime | None:
    """A ``<input type="datetime-local">`` value, read as Asia/Taipei time,
    as naive UTC (the ``ip_allow_rules`` convention). Blank → None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        local = datetime.strptime(text[:16], _LOCAL_INPUT_FORMAT).replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be a date and time (YYYY-MM-DDTHH:MM).")
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _local_label(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def _ip_rule_view(rule, now: datetime) -> dict:
    remaining = ""
    if rule.expires_at is not None and rule.expires_at > now:
        mins = int((rule.expires_at - now).total_seconds() // 60)
        if mins >= 48 * 60:
            remaining = f"{mins // 1440}d {mins % 1440 // 60}h left"
        elif mins >= 60:
            remaining = f"{mins // 60}h {mins % 60:02d}m left"
        else:
            remaining = f"{max(mins, 1)}m left"
    return {
        "id": rule.id,
        "cidr": rule.cidr,
        "label": rule.label,
        "usernames": list(rule.usernames or []),
        "org_codes": list(rule.org_codes or []),
        "everyone": not rule.usernames and not rule.org_codes,
        "created_by": rule.created_by,
        "starts": _local_label(rule.starts_at),
        "expires": _local_label(rule.expires_at),
        "remaining": remaining,
    }


def _ip_allowlist_view(session: Session, request: Request, admin_user: User) -> dict:
    now = ip_allowlist.utcnow()
    groups = ip_allowlist.list_rules(session, now)
    addr = ip_allowlist.client_address(request)
    return {
        **ip_allowlist.settings(),
        "modes": IP_ALLOWLIST_MODES,
        "rules": {k: [_ip_rule_view(r, now) for r in rows] for k, rows in groups.items()},
        "your_ip": str(addr) if addr is not None else "",
        # Whether the admin's own API key would be admitted from here.
        "your_verdict": ip_allowlist.verdict(addr, admin_user.username, admin_user.org_code or "", now),
    }


@router.post("/ip-allowlist")
def update_ip_allowlist(
    request: Request,
    mode: str = Form(""),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Save the IP allowlist's mode (``[app].ip_allowlist_mode``)."""
    mode = mode.strip().lower()
    if mode not in IP_ALLOWLIST_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of: {', '.join(IP_ALLOWLIST_MODES)}.")
    set_ip_allowlist_settings(mode)
    logger.info("IP allowlist mode set | mode={} admin={}", mode, admin_user.username)
    if _wants_json(request):
        return JSONResponse({"ok": True, "mode": mode})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/ip-allowlist/rules")
def add_ip_allow_rule(
    request: Request,
    cidr: str = Form(""),
    label: str = Form(""),
    usernames: str = Form(""),
    org_codes: str = Form(""),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Add an allowlist rule: an address or CIDR range; for everyone (both
    ``usernames`` and ``org_codes`` empty) or for the listed accounts and
    org codes (comma-separated); permanent (no end time) or for one window
    (start optional, end in the future). Times are Asia/Taipei. Takes
    effect in this worker at once, in the others within
    ``ip_allowlist.REFRESH_S`` seconds."""
    start = _local_input_to_utc("Start", starts_at)
    end = _local_input_to_utc("End", expires_at)
    try:
        rule = ip_allowlist.add_rule(
            session, cidr, label=label, created_by=admin_user.username,
            usernames=ip_allowlist.split_names(usernames), org_codes=ip_allowlist.split_names(org_codes),
            starts_at=start, expires_at=end,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ip_allowlist.refresh()
    logger.info(
        "IP allowlist rule added | id={} cidr={} label={!r} for={} starts={} expires={} admin={}",
        rule.id, rule.cidr, rule.label, _who_text(rule), rule.starts_at or "now", rule.expires_at or "never",
        admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "rule": _ip_rule_view(rule, ip_allowlist.utcnow())})
    return RedirectResponse(url="/admin", status_code=303)


def _who_text(rule) -> str:
    if not rule.usernames and not rule.org_codes:
        return "everyone"
    return ",".join([*rule.usernames, *(f"org:{c}" for c in rule.org_codes)])


@router.post("/ip-allowlist/rules/{rule_id}/who")
def set_ip_allow_rule_who(
    rule_id: int,
    request: Request,
    usernames: str = Form(""),
    org_codes: str = Form(""),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Change who a rule admits (both empty = everyone), e.g. when a team
    changes. The address and window stay as they are."""
    rule = session.get(IpAllowRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found.")
    before = _who_text(rule)
    try:
        ip_allowlist.set_who(
            session, rule, ip_allowlist.split_names(usernames), ip_allowlist.split_names(org_codes),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ip_allowlist.refresh()
    logger.info(
        "IP allowlist rule edited | id={} cidr={} for: {} -> {} admin={}",
        rule.id, rule.cidr, before, _who_text(rule), admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "rule": _ip_rule_view(rule, ip_allowlist.utcnow())})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/ip-allowlist/rules/{rule_id}/end")
def end_ip_allow_rule(
    rule_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """End a rule now. It stays in the list as expired for a week; a rule
    that had not started is deleted."""
    rule = session.get(IpAllowRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found.")
    cidr, label = rule.cidr, rule.label
    ip_allowlist.end_rule(session, rule)
    ip_allowlist.refresh()
    logger.info(
        "IP allowlist rule ended | id={} cidr={} label={!r} admin={}",
        rule_id, cidr, label, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "ended": rule_id})
    return RedirectResponse(url="/admin", status_code=303)


# ── Guardrail ──

def _guardrail_view() -> dict:
    """Guardrail settings for the template: never the API key itself."""
    settings = get_guardrail_settings()
    view = {k: v for k, v in settings.items() if k != "api_key"}
    view["rules"] = {
        cat: guardrail.category_rule(settings, cat) for cat in guardrail.QWEN3GUARD_CATEGORIES
    }
    return view


def _guardrail_from_form(form) -> dict:
    """Validated ``[guardrail]`` values from the admin card's form. A blank
    API key keeps the stored one (the page never sees it); ``clear_api_key``
    removes it. Raises 400 on anything invalid."""
    current = get_guardrail_settings()

    def choice(field: str, allowed: tuple[str, ...]) -> str:
        value = str(form.get(field) or "").strip().lower()
        if value not in allowed:
            raise HTTPException(status_code=400, detail=f"{field} must be one of: {', '.join(allowed)}.")
        return value

    base_url = str(form.get("base_url") or "").strip().rstrip("/")
    if base_url and not base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http:// or https://.")

    api_key = str(form.get("api_key") or "").strip()
    if str(form.get("clear_api_key") or "").lower() in ("on", "true", "1"):
        api_key = ""
    elif not api_key:
        api_key = current["api_key"]

    lo, hi = GUARDRAIL_TIMEOUT_RANGE_S
    try:
        timeout_s = float(str(form.get("timeout_s") or "").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="timeout_s must be a number of seconds.")
    if not math.isfinite(timeout_s) or not lo <= timeout_s <= hi:
        raise HTTPException(status_code=400, detail=f"timeout_s must be between {lo:g} and {hi:g} seconds.")

    lo_c, hi_c = GUARDRAIL_MAX_CHARS_RANGE
    try:
        max_chars = int(str(form.get("max_input_chars") or "").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="max_input_chars must be a whole number.")
    if not lo_c <= max_chars <= hi_c:
        raise HTTPException(status_code=400, detail=f"max_input_chars must be between {lo_c} and {hi_c}.")

    rules: dict[str, str] = {}
    for cat in guardrail.QWEN3GUARD_CATEGORIES:
        rule = str(form.get(f"rule:{cat}") or guardrail.DEFAULT_CATEGORY_RULE).strip().lower()
        if rule not in GUARDRAIL_CATEGORY_RULES:
            raise HTTPException(status_code=400, detail=f"rule for {cat} must be one of: {', '.join(GUARDRAIL_CATEGORY_RULES)}.")
        if rule != guardrail.DEFAULT_CATEGORY_RULE:
            rules[cat] = rule

    endpoints = [e for e in form.getlist("endpoints") if e in GUARDRAIL_ENDPOINTS]
    exempt = []
    for part in str(form.get("exempt_users") or "").replace("\n", ",").split(","):
        name = part.strip()
        if name and name not in exempt:
            exempt.append(name)
    block_message = str(form.get("block_message") or "").strip()
    if len(block_message) > GUARDRAIL_BLOCK_MESSAGE_MAX:
        raise HTTPException(status_code=400, detail=f"block_message is limited to {GUARDRAIL_BLOCK_MESSAGE_MAX} characters.")

    return {
        # The card sends a hidden "off" plus the checkbox's "on" when ticked.
        "enabled": any(str(v).lower() in ("on", "true", "1", "yes") for v in form.getlist("enabled")),
        "base_url": base_url,
        "api_key": api_key,
        "model": str(form.get("model") or "").strip() or current["model"],
        "timeout_s": timeout_s,
        "action": choice("action", GUARDRAIL_ACTIONS),
        "block_threshold": choice("block_threshold", GUARDRAIL_THRESHOLDS),
        "category_rules": rules,
        "block_message": block_message,
        "apply_to": choice("apply_to", GUARDRAIL_SCOPES),
        "exempt_users": exempt,
        "endpoints": endpoints,
        "max_input_chars": max_chars,
    }


@router.post("/guardrail")
async def update_guardrail(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Save the ``[guardrail]`` section (admin panel → Guardrail card). Takes
    effect on the next request in every worker (config auto-reload)."""
    values = _guardrail_from_form(await request.form())
    set_guardrail_settings(values)
    logger.info(
        "Guardrail settings saved | enabled={} action={} threshold={} base_url={} admin={}",
        values["enabled"], values["action"], values["block_threshold"],
        values["base_url"] or "(none)", admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "guardrail": _guardrail_view()})
    return RedirectResponse(url="/admin", status_code=303)


_GUARDRAIL_TEST_TEXT = "Hello! Can you help me write a short poem about the sea?"


@router.post("/guardrail/test")
async def test_guardrail(request: Request):
    """Admin "Test connection": judge a sample text with the values currently
    in the form (saved or not; enabled or not) and return the verdict, so a
    base URL / key / model name can be checked before turning it on."""
    form = await request.form()
    values = _guardrail_from_form(form)
    if not values["base_url"]:
        raise HTTPException(status_code=400, detail="Set a base URL first.")
    text = str(form.get("text") or "").strip() or _GUARDRAIL_TEST_TEXT
    return JSONResponse(await guardrail.test_guard(values, text))


def _wants_json(request: Request) -> bool:
    """True when the caller asked for a JSON reply (``Accept: application/json``).

    The admin page's in-place toggles send this so a click updates one
    button instead of reloading (and re-scrolling) the whole page; a plain
    form submit keeps the classic POST → 303 → ``/admin`` round-trip.
    """
    return "application/json" in (request.headers.get("accept") or "").lower()


@router.post("/users/{user_id}/toggle-disable")
def toggle_disable(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's is_disabled flag. Refuses to disable yourself."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == admin_user.id and not target.is_disabled:
        raise HTTPException(status_code=400, detail="Cannot disable yourself.")
    target.is_disabled = not target.is_disabled
    session.add(target)
    session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "is_disabled": target.is_disabled})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-azure")
def toggle_azure(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's can_use_azure flag."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.can_use_azure = not target.can_use_azure
    session.add(target)
    session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "can_use_azure": target.can_use_azure})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-bedrock")
def toggle_bedrock(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's can_use_bedrock flag."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.can_use_bedrock = not target.can_use_bedrock
    session.add(target)
    session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "can_use_bedrock": target.can_use_bedrock})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-concurrency-waive")
def toggle_concurrency_waive(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's concurrency_waived flag (exempt from the concurrency
    limit). Requests already in flight keep their slot until they finish."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.concurrency_waived = not target.concurrency_waived
    session.add(target)
    session.commit()
    logger.info(
        "Concurrency limit {} | user={} admin={}",
        "waived" if target.concurrency_waived else "applied",
        target.username, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "concurrency_waived": target.concurrency_waived})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user(
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

    # Clear every row that references users.id, then the user. PostgreSQL
    # enforces these foreign keys, so a missed one fails the delete with a 500.
    # usage_logs and usage_daily are deliberately KEPT: they are the billing
    # and audit record (no foreign key, so nothing forces their removal), and
    # deleting only one of them would make reports disagree with each other.
    # The log line below keeps the id -> account mapping those rows refer to.
    session.execute(delete(AppOwner).where(
        (AppOwner.app_id == user_id) | (AppOwner.owner_id == user_id)
    ))
    # Legacy single-owner column: superseded by app_owners, but migration
    # d5a2c3e67f04 copied it without clearing it, so old app accounts still
    # point at their original owner.
    session.execute(update(User).where(User.owner_id == user_id).values(owner_id=None))
    session.execute(delete(AnomalyEvent).where(AnomalyEvent.user_id == user_id))
    concurrency.delete_user_leases(session, user_id)
    deleted = (target.id, target.username, target.display_name or "", target.org_code or "")
    session.delete(target)
    session.commit()
    logger.warning(
        "User deleted | admin={} user_id={} username={} display_name={} org_code={} "
        "(usage_logs / usage_daily rows kept)",
        admin_user.username, *deleted,
    )

    if _wants_json(request):
        return JSONResponse({"ok": True, "deleted": user_id})
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/users/{user_id}/api-key")
def reveal_user_key(
    user_id: int,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Return one account's full API key, one deliberate request at a time.

    The user/app listings only carry masked keys, so this is the single way
    an admin gets a usable credential out of the panel — one account per
    call, logged with who asked for whose key.
    """
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    logger.warning(
        "API key revealed | admin={} target={}", admin_user.username, target.username
    )
    return JSONResponse({"ok": True, "api_key": target.api_key})


@router.post("/users/{user_id}/refresh-key")
def refresh_user_key(
    user_id: int,
    session: Session = Depends(get_session),
):

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    from app.models.schema import issue_api_key
    issue_api_key(target)
    session.add(target)
    session.commit()
    session.refresh(target)

    return JSONResponse({"ok": True, "api_key": target.api_key})


# ── API endpoints (Bearer token auth) ──

@router.get("/users")
def list_users_api(
    session: Session = Depends(get_session),
):
    users = session.exec(select(User)).all()

    # Build owner lookup
    all_app_owners = session.exec(select(AppOwner)).all()
    app_owners_map: dict[int, list[int]] = {}
    for ao in all_app_owners:
        app_owners_map.setdefault(ao.app_id, []).append(ao.owner_id)

    return [
        {
            "id": u.id,
            "username": u.username,
            # Masked: a bulk listing must not carry usable credentials.
            # The modal fetches the real key from /users/{id}/api-key.
            "api_key": mask_api_key(u.api_key),
            "daily_limit_usd": u.daily_limit_usd,
            "cloud_daily_limit_usd": u.cloud_daily_limit_usd,
            "is_admin": u.is_admin,
            "concurrency_waived": u.concurrency_waived,
            "owner_ids": app_owners_map.get(u.id, []),
            "display_name": u.display_name,
            "org_code": u.org_code,
        }
        for u in users
    ]


@router.post("/users")
def create_user_api(
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

    _reject_retired_limits(body)
    # App accounts start waived from the concurrency limit; an explicit
    # "concurrency_waived" in the body (handled below) still wins.
    new_user = User(username=username, concurrency_waived=is_app_account(username))
    if "daily_limit_usd" in body:
        new_user.daily_limit_usd = _limit_usd(body["daily_limit_usd"], "daily_limit_usd")
    if "cloud_daily_limit_usd" in body:
        raw = body["cloud_daily_limit_usd"]
        new_user.cloud_daily_limit_usd = (
            None if raw is None else _limit_usd(raw, "cloud_daily_limit_usd")
        )
    if "is_admin" in body:
        new_user.is_admin = bool(body["is_admin"])
    if "concurrency_waived" in body:
        new_user.concurrency_waived = _strict_bool(body["concurrency_waived"], "concurrency_waived")
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


def _strict_bool(value: object, field: str) -> bool:
    """A JSON boolean, nothing else — ``bool("false")`` is True, which would
    silently waive a user whose admin sent the string by mistake."""
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be true or false.")
    return value


@router.patch("/users/{user_id}")
def update_user_api(
    user_id: int,
    body: dict,
    session: Session = Depends(get_session),
):
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    _reject_retired_limits(body)
    if "daily_limit_usd" in body:
        target.daily_limit_usd = _limit_usd(body["daily_limit_usd"], "daily_limit_usd")
    if "cloud_daily_limit_usd" in body:
        raw = body["cloud_daily_limit_usd"]
        target.cloud_daily_limit_usd = (
            None if raw is None else _limit_usd(raw, "cloud_daily_limit_usd")
        )
    if "is_admin" in body:
        target.is_admin = bool(body["is_admin"])
    if "concurrency_waived" in body:
        target.concurrency_waived = _strict_bool(body["concurrency_waived"], "concurrency_waived")
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


def _validate_reasoning_keys(label: str, alias: str, info: dict) -> None:
    """Type-check the per-model reasoning-effort compatibility declaration.

    `reasoning_efforts` is a list of effort names, `reasoning_effort_map` a
    table of name -> name (an empty target means "drop the field"). Both are
    rewrite policy, so a typo here would silently mangle every request the
    model serves — reject it at save time instead.
    """
    efforts = info.get("reasoning_efforts")
    if efforts is not None:
        if not isinstance(efforts, list) or not all(isinstance(v, str) for v in efforts):
            raise HTTPException(
                status_code=400,
                detail=f"{label} '{alias}' field 'reasoning_efforts' must be a list of strings.",
            )
    mapping = info.get("reasoning_effort_map")
    if mapping is not None:
        if not isinstance(mapping, dict) or not all(
            isinstance(k, str) and (v is None or isinstance(v, str))
            for k, v in mapping.items()
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label} '{alias}' field 'reasoning_effort_map' must be a "
                    "table of effort name -> effort name."
                ),
            )


def _validate_rate_limit_fallback(
    label: str, alias: str, info: dict, models: dict,
) -> None:
    """`rate_limit_fallback` must name another entry of the same backend
    that the chat/messages surfaces can serve (llm/vlm). Empty = unset."""
    target = info.get("rate_limit_fallback")
    if target in (None, ""):
        return
    if not isinstance(target, str):
        raise HTTPException(
            status_code=400,
            detail=f"{label} '{alias}' field 'rate_limit_fallback' must be an alias string.",
        )
    if target == alias:
        raise HTTPException(
            status_code=400,
            detail=f"{label} '{alias}' cannot be its own rate_limit_fallback.",
        )
    target_info = models.get(target)
    if not isinstance(target_info, dict):
        raise HTTPException(
            status_code=400,
            detail=f"{label} '{alias}' rate_limit_fallback references unknown alias '{target}'.",
        )
    if target_info.get("type", "llm") not in ("llm", "vlm"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} '{alias}' rate_limit_fallback '{target}' has type "
                f"'{target_info.get('type')}'; it must be llm or vlm."
            ),
        )


# ── Model Configuration ──

@router.get("/models", response_class=HTMLResponse)
def admin_models_page(
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
def get_config_api():
    # Secrets go out as references (config.masked_config_data), never values.
    return JSONResponse(masked_config_data())


def _clean_display_order(
    raw: object, maps: dict[str, dict | None],
) -> dict[str, list[str]] | None:
    """Validate ``display_order`` ({backend: [alias, ...]}) from a config PUT.

    ``None`` (omitted) leaves the section as it is. Aliases that are not
    models of that backend (renamed or deleted since the page loaded) and
    repeats are dropped rather than refused: the order is display-only.
    A backend whose models the payload omits is checked against the
    current config, which save_config leaves untouched."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="display_order must be an object of alias lists.")
    unknown = sorted(set(raw) - set(DISPLAY_ORDER_BACKENDS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"display_order: unknown backend(s) {unknown}.")
    current = {
        "vllm": get_model_routing_snapshot,
        "azure": get_azure_models_snapshot,
        "bedrock": get_bedrock_models_snapshot,
    }
    out: dict[str, list[str]] = {}
    for backend in DISPLAY_ORDER_BACKENDS:
        aliases = raw.get(backend, [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise HTTPException(status_code=400, detail=f"display_order.{backend} must be a list of aliases.")
        known = maps[backend] if maps[backend] is not None else current[backend]()
        out[backend] = list(dict.fromkeys(a for a in aliases if a in known))
    return out


_TEST_MODEL_TIMEOUT_S = 10.0


@router.post("/api/test-model")
async def test_model_connection(body: dict):
    """Model Config "Test connection" for a local (vLLM) entry, with the
    values in the editor (saved or not): ``{base_url, api_key, real_model,
    type}``. Asks the server for ``GET {base_url}/models`` (System One:
    its ``/health``) with the entry's key and reports whether it answered
    and whether ``real_model`` is among the models it serves. Read-only
    and free: no inference call, nothing billed."""
    import time

    import httpx

    from app.core.server_state import get_health_client
    from app.services.health import _SYSTEMONE_TYPE, _probe_systemone

    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    if not base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http:// or https://.")
    try:
        api_key = str(resolve_secret(body.get("api_key")) or "").strip()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"API key: {exc}")
    real_model = str(body.get("real_model") or "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client = get_health_client()
    t0 = time.monotonic()

    def ms() -> float:
        return round((time.monotonic() - t0) * 1000.0, 1)

    if body.get("type") == _SYSTEMONE_TYPE:
        try:
            ok = await _probe_systemone(client, base_url, headers)
        except Exception as exc:
            return JSONResponse({"ok": False, "latency_ms": ms(), "detail": type(exc).__name__})
        return JSONResponse({
            "ok": ok, "latency_ms": ms(),
            "detail": "health check passed" if ok else "health check failed",
        })

    try:
        resp = await client.get(f"{base_url}/models", headers=headers, timeout=_TEST_MODEL_TIMEOUT_S)
    except httpx.TimeoutException:
        return JSONResponse({"ok": False, "latency_ms": ms(), "detail": f"no answer within {_TEST_MODEL_TIMEOUT_S:g}s"})
    except Exception as exc:
        return JSONResponse({"ok": False, "latency_ms": ms(), "detail": f"cannot connect ({type(exc).__name__})"})
    latency = ms()
    if resp.status_code in (401, 403):
        return JSONResponse({"ok": False, "latency_ms": latency, "detail": f"HTTP {resp.status_code}: check the API key"})
    if resp.status_code != 200:
        return JSONResponse({"ok": False, "latency_ms": latency, "detail": f"HTTP {resp.status_code}"})
    served: list[str] = []
    try:
        for item in resp.json().get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                served.append(item["id"])
    except Exception:
        return JSONResponse({"ok": False, "latency_ms": latency, "detail": "/models did not return a model list"})
    found = bool(real_model) and real_model in served
    if not real_model:
        detail = "server reachable; set Real model to check it is served"
    elif found:
        detail = f"server reachable, serves {real_model}"
    else:
        detail = f"server reachable, but {real_model} is not among its models"
    return JSONResponse({
        "ok": found, "latency_ms": latency, "detail": detail, "served_models": served[:20],
    })


@router.put("/api/config")
async def save_config_api(
    request: Request,
):
    body = await request.json()

    models = body.get("models")
    pricing = body.get("pricing")
    fallback = body.get("fallback")
    azure_models = body.get("azure_models")
    azure_fallback = body.get("azure_fallback")
    # Optional: older admin payloads omit these, and save_config leaves the
    # [bedrock_*] sections untouched when they're None.
    bedrock_models = body.get("bedrock_models")
    bedrock_fallback = body.get("bedrock_fallback")

    if not isinstance(models, dict) or not isinstance(pricing, dict):
        raise HTTPException(status_code=400, detail="Invalid config format.")
    if azure_models is not None and not isinstance(azure_models, dict):
        raise HTTPException(status_code=400, detail="Invalid azure_models format.")
    if azure_fallback is not None and not isinstance(azure_fallback, dict):
        raise HTTPException(status_code=400, detail="Invalid azure_fallback format.")
    if bedrock_models is not None and not isinstance(bedrock_models, dict):
        raise HTTPException(status_code=400, detail="Invalid bedrock_models format.")
    if bedrock_fallback is not None and not isinstance(bedrock_fallback, dict):
        raise HTTPException(status_code=400, detail="Invalid bedrock_fallback format.")
    # The editor holds references to stored secrets, not the secrets.
    try:
        resolve_secret_refs(
            {"models": models, "azure_models": azure_models, "bedrock_models": bedrock_models}
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"API key: {exc}")
    display_order = _clean_display_order(
        body.get("display_order"),
        {"vllm": models, "azure": azure_models, "bedrock": bedrock_models},
    )

    # Aliases must be unique across backends — _build_config refuses to load
    # a config that repeats one, and it only runs AFTER save_config has
    # written the file, so catch it here instead of persisting a broken
    # config.toml.
    _seen_alias: dict[str, str] = {}
    for _label, _map in (
        ("vLLM", models), ("Azure", azure_models or {}), ("Bedrock", bedrock_models or {}),
    ):
        for _alias in _map:
            if _alias in _seen_alias:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Alias '{_alias}' is used by both a {_seen_alias[_alias]} "
                        f"and a {_label} model. Aliases must be unique across backends."
                    ),
                )
            _seen_alias[_alias] = _label

    # Validate model entries have required fields + sanity-check metadata
    _META_TYPES: dict[str, type | tuple[type, ...]] = {
        "display_name": str,
        "context_window": int,
        "max_output_tokens": int,
        "supports_tools": bool,
        "supports_vision": bool,
        "supports_prompt_caching": bool,
        "is_reasoning": bool,
    }
    _INTERNAL_TYPES: dict[str, type | tuple[type, ...]] = {
        "hidden": bool,
        "native_messages": bool,
    }

    def _check_internal(label: str, alias: str, info: dict) -> None:
        for key in _MODEL_INTERNAL_KEYS:
            if key in info and not isinstance(info[key], _INTERNAL_TYPES[key]):
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} '{alias}' field '{key}' has wrong type.",
                )
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
        _check_internal("Model", alias, info)
        _validate_reasoning_keys("Model", alias, info)
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
    if azure_fallback and not all(isinstance(v, str) for v in azure_fallback.values()):
        raise HTTPException(status_code=400, detail="Azure fallback values must be strings.")
    # Each azure_fallback alias must point to an existing Azure deployment
    # of the matching type. Catching the typo here is friendlier than letting
    # _resolve_azure silently ignore the entry at runtime.
    if azure_fallback and azure_models is not None:
        for type_key, alias in azure_fallback.items():
            if not alias:
                continue
            if alias not in azure_models:
                raise HTTPException(
                    status_code=400,
                    detail=f"Azure fallback for type '{type_key}' references unknown alias '{alias}'.",
                )
            entry_type = azure_models[alias].get("type", "llm")
            if entry_type != type_key:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Azure fallback for type '{type_key}' points to '{alias}' "
                        f"which has type '{entry_type}'."
                    ),
                )

    # Validate Bedrock fallback the same way as Azure's above.
    if bedrock_fallback:
        if not all(isinstance(v, str) for v in bedrock_fallback.values()):
            raise HTTPException(status_code=400, detail="Bedrock fallback values must be strings.")
        for type_key, alias in bedrock_fallback.items():
            if not alias:
                continue
            target = (bedrock_models or {}).get(alias)
            if bedrock_models is not None and target is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Bedrock fallback for type '{type_key}' references unknown alias '{alias}'.",
                )
            if target is not None and target.get("type", "llm") != type_key:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Bedrock fallback for type '{type_key}' points to '{alias}' "
                        f"which has type '{target.get('type', 'llm')}'."
                    ),
                )

    # Validate Azure + Bedrock model entries (same metadata / pricing /
    # reasoning / rate-limit rules; only the connection fields differ).
    for backend, cloud_models, required_fields in (
        ("Azure", azure_models, ("type", "endpoint", "deployment", "api_key")),
        ("Bedrock", bedrock_models, ("type", "model_id")),
    ):
        if not cloud_models:
            continue
        for alias, info in cloud_models.items():
            if not isinstance(info, dict):
                raise HTTPException(status_code=400, detail=f"Invalid {backend.lower()} model entry: {alias}")
            for required in required_fields:
                if not info.get(required):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' missing required field '{required}'.",
                    )
            if backend == "Bedrock":
                has_key_id = bool(info.get("aws_access_key_id"))
                has_secret = bool(info.get("aws_secret_access_key"))
                if has_key_id != has_secret:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Bedrock model '{alias}' needs both aws_access_key_id and aws_secret_access_key.",
                    )
                shared = bedrock_shared_settings()
                shared_iam = bool(shared["aws_access_key_id"] and shared["aws_secret_access_key"])
                if not (info.get("api_key") or has_key_id or shared_iam):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Bedrock model '{alias}' needs an api_key or aws_access_key_id + "
                            "aws_secret_access_key (or BEDROCK_AWS_ACCESS_KEY_ID / "
                            "BEDROCK_AWS_SECRET_ACCESS_KEY in .env)."
                        ),
                    )
            for key in _MODEL_METADATA_KEYS:
                if key not in info:
                    continue
                expected = _META_TYPES[key]
                value = info[key]
                if expected is int and isinstance(value, bool):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be an integer, got boolean.",
                    )
                if not isinstance(value, expected):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' has wrong type.",
                    )
                if expected is int and value < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be non-negative.",
                    )
            _validate_reasoning_keys(f"{backend} model", alias, info)
            _validate_rate_limit_fallback(f"{backend} model", alias, info, cloud_models)
            _check_internal(f"{backend} model", alias, info)
            # Bedrock's Anthropic endpoint only serves Claude; the proxy
            # would ignore the flag on any other model.
            if (
                backend == "Bedrock" and info.get("native_messages")
                and "anthropic" not in str(info.get("model_id") or "").lower()
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Bedrock model '{alias}': native_messages needs a Claude model_id "
                        "(one containing 'anthropic')."
                    ),
                )
            for key in _MODEL_PRICING_KEYS:
                if key not in info:
                    continue
                value = info[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be a number.",
                    )
                if value < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be non-negative.",
                    )

    save_config(
        models, pricing, fallback or {}, azure_models, azure_fallback,
        bedrock_models, bedrock_fallback, display_order,
    )
    return JSONResponse({"ok": True})


# ── Anomaly findings (populated by scripts/scan_anomalies.py) ──

@router.get("/api/anomalies")
def list_anomalies(
    status: str = "new",
    limit: int = 50,
    session: Session = Depends(get_session),
):
    """List anomaly findings. `status` = new | acknowledged | resolved | all."""
    stmt = select(AnomalyEvent).order_by(AnomalyEvent.created_at.desc())  # type: ignore[attr-defined]
    if status != "all":
        stmt = stmt.where(AnomalyEvent.status == status)
    rows = session.exec(stmt.limit(max(1, min(limit, 200)))).all()
    return JSONResponse([
        {
            "id": e.id, "scope": e.scope, "rule": e.rule, "severity": e.severity,
            "user_id": e.user_id, "model": e.model,
            "window_start": e.window_start.isoformat(), "window_end": e.window_end.isoformat(),
            "observed": e.observed, "baseline": e.baseline, "ratio": e.ratio,
            "detail": e.detail, "status": e.status,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ])


def _set_anomaly_status(session: Session, event_id: int, status: str) -> JSONResponse:
    ev = session.get(AnomalyEvent, event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Anomaly event not found.")
    ev.status = status
    ev.updated_at = datetime.now(timezone.utc)
    session.add(ev)
    session.commit()
    return JSONResponse({"ok": True, "id": event_id, "status": status})


@router.post("/api/anomalies/{event_id}/ack")
def ack_anomaly(event_id: int, session: Session = Depends(get_session)):
    return _set_anomaly_status(session, event_id, "acknowledged")


@router.post("/api/anomalies/{event_id}/resolve")
def resolve_anomaly(event_id: int, session: Session = Depends(get_session)):
    return _set_anomaly_status(session, event_id, "resolved")


# ── Usage Export ──

_MAX_EXPORT_MONTHS = 24


@router.get("/api/export/usage.xlsx")
def export_usage_report(
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


@router.get("/api/export/user-backend-costs.csv")
def export_user_backend_costs_csv(
    session: Session = Depends(get_session),
    from_: str = Query(..., alias="from", description="Start month YYYY-MM (inclusive)"),
    to: str = Query(..., description="End month YYYY-MM (inclusive)"),
):
    """Export per-account cost split by serving backend as CSV.

    One row per (month × account) with separate On-prem (vLLM) / Azure /
    AWS Bedrock cost columns — the flat-file counterpart of the xlsx
    "Cost by Backend" sheet, for feeding billing/chargeback systems.
    Includes both human users and app_* accounts (see the `type` column).
    """
    import csv
    import io

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

    months_rows = user_backend_costs(session, from_, to)

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM so Excel opens CJK correctly
    writer = csv.writer(buf)
    writer.writerow([
        "month", "username", "display_name", "org_code", "type",
        "requests", "input_tokens", "output_tokens",
        "vllm_cost_usd", "azure_cost_usd", "bedrock_cost_usd", "total_cost_usd",
    ])
    for month, rows in months_rows:
        for u in rows:
            writer.writerow([
                month,
                u["username"],
                u["display_name"],
                u["org_code"],
                "app" if u["is_app"] else "user",
                u["requests"],
                u["input_tokens"],
                u["output_tokens"],
                f"{u['vllm_cost_usd']:.6f}",
                f"{u['azure_cost_usd']:.6f}",
                f"{u['bedrock_cost_usd']:.6f}",
                f"{u['total_cost_usd']:.6f}",
            ])

    filename = f"user_backend_costs_{from_}_to_{to}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/export/users.csv")
def export_users_csv(session: Session = Depends(get_session)):
    """Export all real users (excluding app_* accounts) as CSV for mass mailing."""
    import csv
    import io

    # Last activity as a correlated subquery: one backwards probe of
    # ix_usage_user_date_cost per user, instead of a GROUP BY over the whole
    # (unbounded) usage_logs table.
    last_active_col = (
        select(func.max(UsageLog.created_at))
        .where(UsageLog.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    rows = session.exec(
        select(User, last_active_col)
        .where(~app_account_clause())
        .order_by(User.org_code, User.username)
    ).all()
    users = [u for u, _ in rows]
    last_active = {u.id: dt for u, dt in rows}

    monthly = get_monthly_all_users_usage(session)

    def fmt_dt(dt: datetime | None) -> str:
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM so Excel opens CJK correctly
    writer = csv.writer(buf)
    writer.writerow([
        "username", "display_name", "org_code", "is_admin", "is_disabled",
        "can_use_azure", "can_use_bedrock",
        "daily_limit_usd", "created_at",
        "monthly_cost_usd", "monthly_requests", "last_active_at",
    ])
    for u in users:
        m = monthly.get(u.id or 0, {})
        writer.writerow([
            u.username,
            u.display_name or "",
            u.org_code or "",
            "true" if u.is_admin else "false",
            "true" if u.is_disabled else "false",
            "true" if u.can_use_azure else "false",
            "true" if u.can_use_bedrock else "false",
            f"{u.daily_limit_usd:.2f}",
            fmt_dt(u.created_at),
            f"{m.get('cost', 0.0):.6f}",
            m.get("requests", 0),
            fmt_dt(last_active.get(u.id or 0)),
        ])

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"users_{today}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
