"""
Admin *Settings* tab: the gateway-wide limits (daily, cloud, concurrency,
cloud-budget fallback), the weekly reports and the site links.
"""

from __future__ import annotations

import html as _html

from fastapi import Depends, Form, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session

from app.core.auth import get_web_user
from app.core.config import (
    CONCURRENCY_MODES,
    get_concurrency_cloud_limit,
    parse_hhmm,
    set_cloud_budget_fallback,
    set_concurrency_offpeak,
    set_concurrency_settings,
    set_cloud_daily_limit,
    set_default_daily_limit,
    set_site_links,
)
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import ReportSchedule, User
from app.routers.admin.common import _limit_usd, _wants_json, router
from app.services import audit, concurrency, mailer, langfuse_reader, weekly_reports


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


# ── Weekly reports ──

def _weekly_reports_view(session: Session) -> dict:
    return {
        "reports": weekly_reports.schedules(session),
        "mail": mailer.status(),
        "langfuse": langfuse_reader.monitoring(),
        "weekdays": weekly_reports.WEEKDAY_NAMES,
        "max_recipients": weekly_reports.MAX_RECIPIENTS,
    }


def _report_or_404(name: str) -> weekly_reports.Report:
    report = weekly_reports.REPORTS.get(name)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown report.")
    return report


@router.post("/reports/{name}")
def save_report_schedule(
    name: str,
    request: Request,
    enabled: str = Form("off"),
    weekday: str = Form("0"),
    send_time: str = Form(weekly_reports.DEFAULT_TIME),
    recipients: str = Form(""),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Save one weekly report's schedule: on / off, the day and time
    (Asia/Taipei) and who gets it. Recipients are usernames or addresses of
    the company domain. A report can only be turned on while Langfuse
    monitoring is on. Saving never sends: when this week's time has already
    passed, the first automatic mail is next week's."""
    report = _report_or_404(name)
    try:
        row = weekly_reports.save_schedule(
            session, report.name, enabled=enabled.strip().lower() == "on",
            weekday=weekly_reports.parse_weekday(weekday), send_time=weekly_reports.parse_time(send_time),
            recipients=weekly_reports.parse_recipients(recipients), updated_by=admin_user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info(
        "Weekly report schedule saved | report={} enabled={} weekday={} time={} recipients={} admin={}",
        row.name, row.enabled, row.weekday, row.send_time, row.recipients, admin_user.username,
    )
    if _wants_json(request):
        view = next(r for r in weekly_reports.schedules(session) if r["name"] == report.name)
        return JSONResponse({"ok": True, **view})
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/reports/{name}/preview", response_class=HTMLResponse)
def preview_report(
    name: str,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """The report about last week as the mail would show it, built now from
    live data (its charts inlined). Nothing is sent."""
    report = _report_or_404(name)
    rendered = weekly_reports.render(session, report.name)
    problems = "".join(f"<li>{_html.escape(problem)}</li>" for problem in rendered.problems)
    banner = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;background:#f4f3ef;color:#52514e;'
        'padding:10px 14px;margin-bottom:18px">'
        f"<b>Preview.</b> Subject: {_html.escape(rendered.subject)}"
        + (f'<ul style="margin:6px 0 0 18px;padding:0;color:#d03b3b">{problems}</ul>' if problems else "")
        + "</div>"
    )
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{_html.escape(report.title)} preview</title></head>"
        f'<body style="background:#ffffff;margin:24px">{banner}{weekly_reports.preview_html(rendered)}</body></html>'
    )


@router.post("/reports/{name}/send")
def send_report_now(
    name: str,
    to: str = Form("me"),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Send the report about last week now: to the admin's own mailbox
    (``to=me``, marked [TEST]) or to the report's saved recipients
    (``to=recipients``). The schedule is left as it is."""
    report = _report_or_404(name)
    if not mailer.is_enabled():
        raise HTTPException(status_code=400, detail="Mail is not configured (see .env).")
    monitoring = langfuse_reader.monitoring()
    if not monitoring["on"]:
        raise HTTPException(status_code=400, detail=f"{monitoring['reason']} Nothing is sent until it is on.")
    to = to.strip().lower()
    if to == "recipients":
        addresses = weekly_reports.recipients_of(session.get(ReportSchedule, report.name))
        if not addresses:
            raise HTTPException(status_code=400, detail="This report has no saved recipients.")
        kind, prefix = "requested", ""
    else:
        addresses = [mailer.address_for(admin_user.username)]
        if not mailer.in_company_domain(addresses[0]):
            raise HTTPException(status_code=400, detail=f"Mail goes to @{mailer.settings()['domain']} addresses only.")
        kind, prefix = "test", "[TEST] "
    ok, detail, _rendered = weekly_reports.send(
        session, report.name, addresses, kind=kind, actor=admin_user.username, subject_prefix=prefix,
    )
    audit.note(report=report.name, report_to=", ".join(addresses), report_ok=ok)
    return JSONResponse({"ok": ok, "to": addresses, "detail": detail})


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
    ``concurrency_limit`` / ``concurrency_cloud_limit``).

    Form fields: ``mode`` — ``off`` / ``monitor`` / ``enforce``; ``limit`` — an
    integer >= 1 (required in every mode, so switching back on later doesn't
    silently come up with a stale or missing number), for local requests;
    ``cloud_limit`` — the same for Azure / Bedrock requests, counted apart
    (optional: left out, the stored value stays). "Unlimited" is
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
    raw_cloud = str(form.get("cloud_limit") or "").strip()
    if raw_cloud:
        try:
            cloud_limit = int(raw_cloud)
        except ValueError:
            raise HTTPException(status_code=400, detail="cloud_limit must be a whole number.")
        if cloud_limit < 1:
            raise HTTPException(
                status_code=400,
                detail="cloud_limit must be at least 1 (use mode 'off' for no limit).",
            )
        set_concurrency_settings(mode, limit, cloud_limit=cloud_limit)
    else:
        cloud_limit = get_concurrency_cloud_limit()
        set_concurrency_settings(mode, limit)
    logger.info(
        "Concurrency limit set | mode={} limit={} cloud_limit={} admin={}",
        mode, limit, cloud_limit, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "mode": mode, "limit": limit, "cloud_limit": cloud_limit})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/concurrency-offpeak")
async def update_concurrency_offpeak(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Set the off-peak hours (``[app].concurrency_offpeak_start`` / ``_end``
    / ``_weekends``), during which local requests are not held to the local
    concurrency limit (cloud requests keep theirs).

    Form fields: ``start`` / ``end`` — Asia/Taipei ``HH:MM``, both or neither
    (both empty = no daily window; a window may cross midnight, end is
    exclusive; start == end is refused, it would be ambiguous);
    ``weekends`` — ``on`` makes all of Saturday and Sunday off-peak (any
    other value or absent = off). Takes effect on the next request.
    """
    form = await request.form()
    raw_start = str(form.get("start") or "").strip()
    raw_end = str(form.get("end") or "").strip()
    weekends = str(form.get("weekends") or "").strip().lower() in ("on", "true", "1", "yes")
    if bool(raw_start) != bool(raw_end):
        raise HTTPException(
            status_code=400,
            detail="Set both the start and the end of the off-peak hours, or neither.",
        )
    start = end = ""
    if raw_start:
        start, end = parse_hhmm(raw_start), parse_hhmm(raw_end)
        if start is None or end is None:
            raise HTTPException(status_code=400, detail="Off-peak times must be HH:MM (00:00–23:59).")
        if start == end:
            raise HTTPException(
                status_code=400,
                detail="Off-peak start and end must differ (leave both empty for no daily window).",
            )
    set_concurrency_offpeak(start, end, weekends)
    logger.info(
        "Concurrency off-peak hours set | start={} end={} weekends={} admin={}",
        start or "-", end or "-", weekends, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({
            "ok": True, "start": start, "end": end, "weekends": weekends,
            "active": concurrency.is_offpeak(),
        })
    return RedirectResponse(url="/admin", status_code=303)
