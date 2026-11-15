"""
Admin *Security* tab: the API IP allowlist, the guardrail and the audit log.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Form, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app.core.auth import get_web_user
from app.core.config import (
    GUARDRAIL_ACTIONS,
    GUARDRAIL_BLOCK_MESSAGE_MAX,
    GUARDRAIL_CATEGORY_RULES,
    GUARDRAIL_ENDPOINTS,
    GUARDRAIL_MAX_CHARS_RANGE,
    GUARDRAIL_SCOPES,
    GUARDRAIL_THRESHOLDS,
    GUARDRAIL_TIMEOUT_RANGE_S,
    IP_ALLOWLIST_MODES,
    get_guardrail_settings,
    set_guardrail_settings,
    set_ip_allowlist_settings,
)
from app.core.database import get_session
from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ
from app.models.schema import IpAllowRule, User
from app.routers.admin.common import _local_label, _wants_json, router
from app.services import guardrail, ip_allowlist


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


# ── Audit log ──

def _audit_summary(detail: dict) -> str:
    """One line for the admin card: target, then the submitted fields."""
    parts = []
    if detail.get("target"):
        parts.append(f"target={detail['target']}")
    req = detail.get("request")
    if isinstance(req, dict):
        parts.extend(f"{k}={v}" for k, v in list(req.items())[:6])
    if detail.get("config_changes"):
        parts.append("config: " + ", ".join(sorted(detail["config_changes"])))
    return " · ".join(str(p) for p in parts)[:300]


def _audit_rows(
    session: Session, *, limit: int, actor: str = "", before_id: int | None = None,
    since: datetime | None = None, about: User | None = None,
) -> list[dict]:
    """Newest first. ``about`` keeps the rows that concern one account:
    the admin's changes to it (``/admin/users/<id>/...``) and what it did
    itself (``actor``)."""
    import json as _json

    from sqlalchemy import or_

    from app.models.schema import AuditLog

    q = select(AuditLog)
    if actor:
        q = q.where(AuditLog.actor == actor)
    if about is not None:
        q = q.where(or_(
            AuditLog.path == f"/admin/users/{about.id}",
            AuditLog.path.like(f"/admin/users/{about.id}/%"),  # type: ignore[attr-defined]
            AuditLog.actor == about.username,
        ))
    if before_id is not None:
        q = q.where(AuditLog.id < before_id)  # type: ignore[operator]
    if since is not None:
        q = q.where(AuditLog.created_at >= since)  # type: ignore[operator]
    rows = session.exec(q.order_by(AuditLog.id.desc()).limit(limit)).all()  # type: ignore[union-attr]
    out = []
    for r in rows:
        try:
            detail = _json.loads(r.detail or "{}")
        except Exception:
            detail = {"raw": r.detail}
        out.append({
            "id": r.id, "at": _local_label(r.created_at), "created_at": r.created_at.isoformat() + "Z",
            "actor": r.actor, "method": r.method, "route": r.route, "path": r.path,
            "status": r.status, "client": r.client, "detail": detail,
            "summary": _audit_summary(detail),
        })
    return out


@router.get("/api/audit-log")
def audit_log_api(
    session: Session = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    before_id: int | None = None,
    actor: str = "",
):
    """Newest first; page with ``before_id`` = the last id received."""
    return JSONResponse({"entries": _audit_rows(session, limit=limit, actor=actor, before_id=before_id)})


@router.get("/api/export/audit-log.csv")
def export_audit_log_csv(
    session: Session = Depends(get_session),
    days: int = Query(365, ge=1, le=3650),
):
    """The audit trail of the last ``days`` days, newest first (this download
    is itself recorded)."""
    import csv
    import io
    import json as _json

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = _audit_rows(session, limit=1_000_000, since=since)
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM so Excel opens CJK correctly
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at_utc", "actor", "method", "route", "path", "status", "client", "detail"])
    for r in rows:
        writer.writerow([
            r["id"], r["created_at"], r["actor"], r["method"], r["route"], r["path"],
            r["status"], r["client"], _json.dumps(r["detail"], ensure_ascii=False),
        ])
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit_log_{today}.csv"'},
    )
