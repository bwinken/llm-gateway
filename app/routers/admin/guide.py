"""
Admin *Guide* page (``GET /admin/guide``): what every setting does, why it
exists and what was traded for what. The text is ``app/services/admin_guide``;
this module adds each setting's current value. It reads, and changes nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request, Security
from fastapi.responses import HTMLResponse

from app.core.auth import get_web_user
from app.core.config import (
    APP_TITLE,
    get_client_monthly_mail,
    get_cloud_budget_fallback,
    get_cloud_daily_limit,
    get_concurrency_cloud_limit,
    get_concurrency_offpeak,
    get_concurrency_settings,
    get_default_daily_limit,
    get_guardrail_settings,
    get_ip_allowlist_settings,
)
from app.core.logger import logger
from app.models.schema import User
from app.routers.admin.common import router, templates
from app.services import admin_guide, overflow


def _usd(value: float | None) -> str:
    return "none" if value is None else f"${value:g}"


def _current_values() -> dict[str, str]:
    """``{entry id: the value in force now}`` for the settings that have one
    gateway-wide value. A setting that cannot be read is left out: the page
    is for reading the reasons, and must open even with a broken config."""
    readers = {
        "concurrency": _concurrency,
        "spending": _spending,
        "overflow": _overflow,
        "other": _other,
    }
    out: dict[str, str] = {}
    for name, read in readers.items():
        try:
            out.update(read())
        except Exception as exc:
            logger.warning("Admin guide: current {} settings unreadable | error={}: {}", name, type(exc).__name__, exc)
    return out


def _concurrency() -> dict[str, str]:
    mode, limit = get_concurrency_settings()
    offpeak = get_concurrency_offpeak()
    hours = []
    if offpeak.get("start"):
        hours.append(f"{offpeak['start']}–{offpeak['end']} daily")
    if offpeak.get("weekends"):
        hours.append("all weekend")
    return {
        "concurrency-mode": mode,
        "concurrency-local": str(limit),
        "concurrency-cloud": str(get_concurrency_cloud_limit()),
        "concurrency-offpeak": " and ".join(hours) or "none",
    }


def _spending() -> dict[str, str]:
    return {
        "default-daily-limit": _usd(get_default_daily_limit()),
        "cloud-daily-limit": _usd(get_cloud_daily_limit()),
        "cloud-budget-fallback": "on" if get_cloud_budget_fallback() else "off",
    }


def _overflow() -> dict[str, str]:
    s = overflow.settings()
    return {
        "overflow-mode": {"enforce": "on"}.get(s["mode"], s["mode"]),
        "overflow-budget": _usd(s["daily_budget_usd"]),
        "overflow-stop": f"{s['budget_stop_pct']}%",
        "overflow-thresholds": f"{s['waiting_high']} / {s['waiting_low']}",
        "overflow-consecutive": str(s["consecutive"]),
        "overflow-pace": f"{s['grab_max']} / {s['release_per_round']}",
        "overflow-stay": f"{s['min_stay_min']} minutes",
        "overflow-idle": f"{s['idle_min']} minutes",
    }


def _other() -> dict[str, str]:
    guard = get_guardrail_settings()
    return {
        "ip-allowlist-mode": get_ip_allowlist_settings()["mode"],
        "guardrail-action": f"{'on' if guard.get('enabled') else 'off'} · {guard.get('action', 'monitor')}",
        "client-monthly-mail": "on" if get_client_monthly_mail() else "off",
    }


@router.get("/guide", response_class=HTMLResponse)
def admin_guide_page(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Every admin setting with its reason and its trade-offs, and the value
    in force now."""
    return templates.TemplateResponse(
        request,
        "admin_guide.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "title": "Admin Guide",
            "user": admin_user,
            "display_name": admin_user.display_name or admin_user.username,
            "org_code": admin_user.org_code,
            "current_year": datetime.now(timezone.utc).year,
            "sections": admin_guide.SECTIONS,
            "now": _current_values(),
        },
    )
