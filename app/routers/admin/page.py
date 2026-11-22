"""
``GET /admin``: the whole admin page. Each tab's cards are built by the
module that owns the tab (``users``, ``clients``, ``security``, ``settings``).
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, Request, Security
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.core.auth import get_web_user
from app.core.config import (
    APP_TITLE,
    CONCURRENCY_MODES,
    GUARDRAIL_CATEGORY_RULES,
    GUARDRAIL_ENDPOINTS,
    get_cloud_budget_fallback,
    get_cloud_daily_limit,
    get_concurrency_cloud_limit,
    get_concurrency_offpeak,
    get_concurrency_settings,
    get_default_daily_limit,
    get_model_routing_snapshot,
    get_site_links,
)
from app.core.database import get_session
from app.core.logger import logger
from app.core.server_state import get_metrics, is_alive
from app.core.timeutil import local_month_start
from app.models.schema import AnomalyEvent, User
from app.routers.admin.clients import _client_installs_view, _client_rules_view, _model_changes_view
from app.routers.admin.common import _local_label, router, templates
from app.routers.admin.security import _audit_rows, _guardrail_view, _ip_allowlist_view
from app.routers.admin.settings import _weekly_reports_view
from app.routers.admin.users import (
    DESCRIPTION_MAX,
    known_departments,
    USER_TABLE_PAGE_SIZES,
    USER_TABLE_SORTS,
    USER_TABLE_STATUSES,
    _PAGE_SIZE,
    _special_access_summary,
    _table_state,
    _user_table,
)
from app.services import client_rules, client_settings, concurrency, guardrail, overflow
from app.services.stats import (
    get_budget_pressure,
    owners_to_review,
    get_today_totals,
    summarize_server_status,
    get_dau_trends,
    get_spend_trends,
    get_monthly_overview,
)


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
    spend_data = get_spend_trends(session)
    today_dau = dau_data[-1]["dau"] if dau_data else 0
    dept_usage = overview["departments"]

    # Open anomaly findings (populated by the gateway's hourly anomaly scan,
    # app/services/anomaly_scan.py — the card renders only when non-empty).
    anomaly_rows = session.exec(
        select(AnomalyEvent)
        .where(AnomalyEvent.status.in_(("new", "acknowledged")))  # type: ignore[attr-defined]
        # "info" findings are a record (the API and the account drawer show
        # them), not something to act on: they stay out of Needs attention.
        .where(AnomalyEvent.severity != "info")
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
    try:
        client_rules_view = _client_rules_view(session, admin_user)
    except Exception as exc:  # e.g. before `alembic upgrade`: the card says so, the page still renders
        session.rollback()
        logger.warning("Client rules view failed | error={}: {}", type(exc).__name__, exc)
        client_rules_view = None
    try:
        model_changes_view = _model_changes_view(session, admin_user)
    except Exception as exc:  # e.g. before `alembic upgrade`
        session.rollback()
        logger.warning("Model changes view failed | error={}: {}", type(exc).__name__, exc)
        model_changes_view = None
    try:
        client_installs_view = _client_installs_view(session)
    except Exception as exc:  # e.g. before `alembic upgrade`
        session.rollback()
        logger.warning("Client installs view failed | error={}: {}", type(exc).__name__, exc)
        client_installs_view = None
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

    try:
        weekly_reports_view = _weekly_reports_view(session)
    except Exception as exc:  # before `alembic upgrade`: the card says so
        session.rollback()
        logger.warning("Weekly reports view failed | error={}: {}", type(exc).__name__, exc)
        weekly_reports_view = None

    try:
        overflow_view = overflow.status(session)
    except Exception as exc:  # before `alembic upgrade`: the card says so
        session.rollback()
        logger.warning("Cloud overflow view failed | error={}: {}", type(exc).__name__, exc)
        overflow_view = None

    return templates.TemplateResponse(
        request,
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
            "user_table_page_sizes": USER_TABLE_PAGE_SIZES,
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
            "spend_data": spend_data,
            "today_dau": today_dau,
            "default_daily_limit": get_default_daily_limit(),
            "cloud_daily_limit": get_cloud_daily_limit(),
            "description_max": DESCRIPTION_MAX,
            "departments": known_departments(session),
            "owner_review": owners_to_review(session),
            "cloud_budget_fallback": get_cloud_budget_fallback(),
            "today_totals": today_totals,
            "budget_pressure": budget_pressure,
            "client_rules": client_rules_view,
            "model_changes": model_changes_view,
            "client_setup": client_settings.status(),
            "client_installs": client_installs_view,
            "client_rule_attention": [
                {"id": r["id"], "name": r["name"], "count": r["open"]}
                for r in (client_rules_view or {}).get("rules", []) if r["enabled"] and r["open"]
            ],
            "client_rule_days": client_rules.WINDOW_DAYS,
            "system_status": system_status,
            "concurrency_mode": concurrency_mode,
            "concurrency_limit": concurrency_limit,
            "concurrency_cloud_limit": get_concurrency_cloud_limit(),
            "concurrency_modes": CONCURRENCY_MODES,
            "concurrency_in_flight": concurrency_in_flight,
            "concurrency_offpeak": get_concurrency_offpeak(),
            "concurrency_offpeak_now": concurrency.is_offpeak(),
            "site_links": get_site_links(),
            "audit_recent": _audit_rows(session, limit=20),
            "guardrail": _guardrail_view(),
            "ip_allowlist": ip_allowlist_view,
            "weekly_reports": weekly_reports_view,
            "overflow": overflow_view,
            "overflow_local": _local_label,
            "special_access": _special_access_summary(session),
            "guardrail_categories": guardrail.QWEN3GUARD_CATEGORIES,
            "guardrail_model_family": guardrail.GUARD_MODEL_FAMILY,
            "guardrail_endpoints": GUARDRAIL_ENDPOINTS,
            "guardrail_rules": GUARDRAIL_CATEGORY_RULES,
            "user_total": users_table["total"],
            "app_total": apps_table["total"],
        },
    )
