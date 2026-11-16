"""
Admin *Clients* tab: client rules (settings, mail, mail log, patch preview),
client setup (``client_settings.json``) and client installs (what setup tools
reported, ``client_events``).
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import BackgroundTasks, Depends, Form, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session

from app.core.auth import get_web_user
from app.core.config import get_client_monthly_mail, set_client_monthly_mail
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import User
from app.routers.admin.common import _local_label, _wants_json, router
from app.services import (
    audit, client_checks, client_events, client_mail, client_patch, client_rules, client_settings, mailer,
)


# ── Client rules (app/services/client_rules.py; the checks are code, in client_checks.py) ──

def _client_rule_view(session: Session, rule, seen: dict | None) -> dict:
    rows = client_rules.violations(session, rule.id)
    mailable = {v.username for v in client_mail.mailable(session, rows)}
    check = rule.check
    return {
        "id": rule.id, "name": rule.name, "description": check.description, "enabled": rule.enabled,
        "deprecated": rule.deprecated,
        "needs_patch": check.needs_patch, "missing_patch": rule.missing_patch,
        # Over all time, for judging when the check can go: see client_rules.history.
        "last_seen": _local_label(seen["last_seen_at"]) if seen else "",
        "quiet_days": seen["quiet_days"] if seen else None,
        "ever_accounts": seen["accounts"] if seen else 0,
        "stale": client_rules.is_stale(rule, seen),
        "title": rule.title, "message": rule.message,
        "patch": client_patch.env_lines(client_patch.load_env(rule.patch)),
        "patch_azure": client_patch.env_lines(client_patch.load_env(rule.patch_azure)),
        "patch_aws": client_patch.env_lines(client_patch.load_env(rule.patch_aws)),
        "has_patch": bool(rule.patch or rule.patch_azure or rule.patch_aws),
        "action": rule.action, "email_subject": rule.email_subject,
        "auto_email": rule.auto_email, "email_repeat_days": rule.email_repeat_days,
        "customized": rule.customized, "updated_by": rule.updated_by,
        "hits": [
            {
                "username": v.username, "version": v.client_version,
                "first_seen": _local_label(v.first_seen_at), "last_seen": _local_label(v.last_seen_at),
                "mailed": _local_label(v.mailed_at), "mail_error": v.mail_error,
                "mailable": v.username in mailable,
                # The account said it applied the fix and has not violated since.
                "resolved": _local_label(v.resolved_at) if client_rules.is_resolved(v) else "",
                # It said so, then violated again: open, and a new case for mail.
                "reopened": _local_label(v.resolved_at) if v.resolved_at and not client_rules.is_resolved(v) else "",
                # An admin asked for a mail that has not gone out yet.
                "queued": client_mail.is_queued(v),
            }
            for v in rows
        ],
        "open": sum(1 for v in rows if not client_rules.is_resolved(v)),
        "mailable": len(mailable),
        "unmailed": sum(1 for v in rows if v.username in mailable and v.mailed_at is None),
    }


def _mail_log_view(log: dict) -> dict:
    """A page of the mail log as the admin page and its JSON API show it
    (times in Asia/Taipei)."""
    return {
        **log,
        "rows": [
            {
                "at": _local_label(row.created_at), "username": row.username, "address": row.address,
                "rules": [rule_id for rule_id in row.rule_ids.split(",") if rule_id],
                "kind": row.kind, "ok": row.ok, "error": row.error, "subject": row.subject, "actor": row.actor,
            }
            for row in log["rows"]
        ],
    }


def _client_rules_view(session: Session, admin_user: User) -> dict:
    seen = client_rules.history(session)
    all_rules = client_rules.rules(session)
    return {
        "rules": [_client_rule_view(session, r, seen.get(r.id)) for r in all_rules],
        # Variables two enabled checks set differently: the combined patch
        # and mail take the later check's value, and the admin should know.
        "conflicts": [
            {
                "name": c["name"], "winner": c["winner"],
                "rules": [{"id": rid, "value": "(removed)" if value is None else value} for rid, value in c["rules"]],
                "audiences": [{"local": "local-only", "azure": "Azure", "aws": "AWS"}[a] for a in c["audiences"]],
            }
            for c in client_rules.env_conflicts(all_rules)
        ],
        "retired": [
            {
                "id": c.id, "name": c.name, "reason": c.retired,
                "ever_accounts": seen[c.id]["accounts"] if c.id in seen else 0,
                "last_seen": _local_label(seen[c.id]["last_seen_at"]) if c.id in seen else "",
            }
            for c in client_checks.RETIRED
        ],
        "stale_days": client_rules.STALE_DAYS,
        "versions": client_rules.version_overview(session),
        # What every non-empty patch also sets (a constant in client_checks.py).
        "baseline": client_patch.env_lines(client_rules.baseline_env()),
        "mail": mailer.status(),
        "mail_pace": {**client_mail.mail_pace(), "waiting": len(client_mail.due_accounts(session))},
        "monthly": {**client_mail.monthly_mail(), "enabled": get_client_monthly_mail()},
        # The card's "mail everyone" buttons: accounts with anything open, under any rule.
        "mail_all": {
            "open": len(client_mail.mail_all_targets(session, only_unmailed=False)),
            "unmailed": len(client_mail.mail_all_targets(session, only_unmailed=True)),
        },
        "my_address": mailer.address_for(admin_user.username),
        "mail_log": _mail_log_view(client_mail.mail_log(session)),
        "actions": client_rules.ACTIONS,
        "window_days": client_rules.WINDOW_DAYS,
    }


_OS_LABELS = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}


def _client_installs_view(session: Session) -> dict:
    """The *Client installs* card: what setup tools reported
    (``client_events``), times in Asia/Taipei."""
    def row(event) -> dict:
        return {
            "at": _local_label(event.created_at), "username": event.username, "event": event.event, "ok": event.ok,
            "tool": event.tool, "tool_version": event.tool_version,
            "component": event.component, "component_version": event.component_version,
            "previous_version": event.previous_version, "method": event.method,
            "os_label": _OS_LABELS.get(event.os, event.os), "os_version": event.os_version, "arch": event.arch,
            "machine_id": event.machine_id, "message": event.message,
        }

    found = client_events.overview(session)
    events = [row(event) for event in client_events.recent(session)]
    return {
        "installs": [row(event) for event in found["installs"]],
        "accounts": found["accounts"], "days": found["days"], "versions": found["versions"],
        "os": [{"label": _OS_LABELS[o["os"]], "machines": o["machines"]} for o in found["os"]],
        "events": events, "failed": sum(1 for event in events if not event["ok"]),
        "max_per_hour": client_events.MAX_PER_HOUR,
    }


def _client_rule_form(
    enabled: str = Form(""),
    action: str = Form("notice"),
    title: str = Form(""),
    message: str = Form(""),
    patch: str = Form(""),
    patch_azure: str = Form(""),
    patch_aws: str = Form(""),
    email_subject: str = Form(""),
    auto_email: str = Form(""),
    email_repeat_days: str = Form("0"),
) -> dict:
    return {
        "enabled": enabled == "on", "action": action, "title": title, "message": message,
        "patch": patch, "patch_azure": patch_azure, "patch_aws": patch_aws,
        "email_subject": email_subject, "auto_email": auto_email == "on",
        "email_repeat_days": email_repeat_days,
    }


def _client_rule_or_404(session: Session, rule_id: str):
    rule = client_rules.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="No such client check.")
    return rule


@router.post("/client-rules/monthly-mail")
def set_client_rules_monthly_mail(
    request: Request,
    enabled: str = Form(""),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Turn the monthly reminder on or off (``[app].client_rules_monthly_mail``).
    While on, on the 1st of every month from 09:00 (Asia/Taipei) the mail
    job queues every account that still has something open, and sends the
    queue in its usual batches. Nothing is sent by this call."""
    on = enabled.strip().lower() == "on"
    set_client_monthly_mail(on)
    logger.info("Client rules monthly mail {} | admin={}", "on" if on else "off", admin_user.username)
    if _wants_json(request):
        return JSONResponse({"ok": True, "enabled": on})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/client-rules/mail-all")
def send_client_rules_mail_all(
    request: Request,
    background: BackgroundTasks,
    only_unmailed: str = Form("on"),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Queue a mail to every account with anything open, whatever the
    check: those with something they were never mailed about (default) or
    all of them. Each gets one mail covering everything it has open. Paced
    like every other send: the first batch goes out after this reply, the
    mail job sends the rest."""
    if not mailer.is_enabled():
        raise HTTPException(status_code=400, detail="Mail is not configured (see .env).")
    unmailed_only = only_unmailed == "on"
    queued = client_mail.request_mail_all(session, unmailed_only)
    if queued:
        background.add_task(client_mail.send_batch)
    pace = client_mail.mail_pace()
    logger.info(
        "Client rule mail queued for every rule | accounts={} only_unmailed={} admin={}",
        queued, unmailed_only, admin_user.username,
    )
    audit.note(mail_queued=queued)
    if _wants_json(request):
        return JSONResponse({
            "ok": True, "queued": queued, "batch": pace["batch"],
            "interval_minutes": round(pace["interval_s"] / 60, 1),
        })
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/client-rules/mail-log")
def client_rules_mail_log(
    status: str = "",
    q: str = "",
    page: int = 1,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """One page of the mail log, newest first: every client-rule mail the
    gateway tried to send (test mail included), with the address and how it
    ended. ``status`` = ``sent`` / ``failed`` / ``test``; ``q`` matches the
    account or the address."""
    return JSONResponse(_mail_log_view(client_mail.mail_log(session, status.strip().lower(), q, page)))


@router.post("/client-rules/mail-test-all")
def test_client_rules_mail_all(
    to: str = Form(""),
    audience: str = Form("local"),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Send, to one address, the mail an account gets when it has every
    rule that is on open at once: all of them listed, with the one merged
    patch (as saved). ``to`` and ``audience`` as for a rule's own test mail."""
    if not mailer.is_enabled():
        raise HTTPException(status_code=400, detail="Mail is not configured (see .env).")
    to = to.strip() or mailer.address_for(admin_user.username)
    if not mailer.in_company_domain(to):
        raise HTTPException(status_code=400, detail=f"Test mail goes to @{mailer.settings()['domain']} addresses only.")
    try:
        subject, body, rule_ids = client_mail.render_digest_preview(
            session, to.partition("@")[0], audience.strip().lower() or "local",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    subject = "[TEST] " + subject
    ok, error = mailer.send(to, subject, body)
    client_mail.record_mail(
        session, address=to, subject=subject, rule_ids=rule_ids, kind="test", ok=ok, error=error,
        actor=admin_user.username,
    )
    audit.note(mail_test_to=to, mail_test_ok=ok)
    return JSONResponse({"ok": ok, "to": to, "detail": error, "rules": rule_ids})


@router.post("/client-rules/{rule_id}/mail-test")
def test_client_rule_mail(
    rule_id: str,
    form: dict = Depends(_client_rule_form),
    to: str = Form(""),
    audience: str = Form("local"),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Send the mail of a check as its form stands (saved or not) to one
    address, so the Exchange settings and the wording can be checked before
    anyone else gets it. ``to`` defaults to the admin's own mailbox and must
    be in the company domain; ``audience`` picks whose patch the mail shows
    (a local-only, an Azure or an AWS account)."""
    rule = _client_rule_or_404(session, rule_id)
    if not mailer.is_enabled():
        raise HTTPException(status_code=400, detail="Mail is not configured (see .env).")
    to = to.strip() or mailer.address_for(admin_user.username)
    if not mailer.in_company_domain(to):
        raise HTTPException(status_code=400, detail=f"Test mail goes to @{mailer.settings()['domain']} addresses only.")
    try:
        values = client_rules.clean_fields(form)
        draft = SimpleNamespace(id=rule.id, **values)
        env = client_rules.effective_env(draft, *client_rules.audience_flags(audience.strip().lower() or "local"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    subject, body = client_mail.render_mail(draft, to.partition("@")[0], env)
    subject = "[TEST] " + subject
    ok, error = mailer.send(to, subject, body)
    client_mail.record_mail(
        session, address=to, subject=subject, rule_ids=[rule.id], kind="test", ok=ok, error=error,
        actor=admin_user.username,
    )
    audit.note(mail_test_to=to, mail_test_ok=ok)
    return JSONResponse({"ok": ok, "to": to, "detail": error})


@router.post("/client-rules/{rule_id}")
def save_client_rule(
    rule_id: str,
    request: Request,
    form: dict = Depends(_client_rule_form),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Save a check's settings: on / off, action, notice, patch and mail.
    What the check detects, thresholds included, is code and not editable. Takes
    effect in this worker at once, in the others within
    ``client_rules.REFRESH_S`` seconds."""
    rule = _client_rule_or_404(session, rule_id)
    try:
        rule = client_rules.save_settings(
            session, rule.check, client_rules.clean_fields(form, rule.check), updated_by=admin_user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    client_rules.refresh()
    logger.info(
        "Client rule saved | id={} enabled={} action={} auto_email={} admin={}",
        rule.id, rule.enabled, rule.action, rule.auto_email, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "id": rule.id})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/client-rules/{rule_id}/reset")
def reset_client_rule(
    rule_id: str,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Drop a check's saved settings: it goes back to the defaults in code.
    Its recorded violations are kept."""
    rule = _client_rule_or_404(session, rule_id)
    client_rules.reset_settings(session, rule.id)
    client_rules.refresh()
    logger.info("Client rule reset to defaults | id={} admin={}", rule.id, admin_user.username)
    if _wants_json(request):
        return JSONResponse({"ok": True, "id": rule.id})
    return RedirectResponse(url="/admin", status_code=303)


# ── Client setup (app/services/client_settings.py): what GET /v1/settings offers ──

def _client_setup_reply() -> dict:
    state = client_settings.status()
    return {
        "ok": True, "fields": state["fields"], "names": state["names"], "customized": state["customized"],
        "unknown_models": state["unknown_models"], "preview": state["preview"], "text": state["text"],
    }


@router.post("/client-settings")
def save_client_settings(
    request: Request,
    settings: str = Form(""),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Save the client settings file (``client_settings.json`` in
    ``CONFIG_DIR``): the fields ``GET /v1/settings`` offers, with their
    default and options per tier. The text is validated first; an invalid
    one is refused and nothing is written. Every worker reads the new file
    at its next request. Plain ``def``: file I/O, no await."""
    try:
        fields = client_settings.save(settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.note(client_settings_fields=[field.name for field in fields])
    logger.info("Client settings saved | fields={} admin={}", len(fields), admin_user.username)
    if _wants_json(request):
        return JSONResponse(_client_setup_reply())
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/client-settings/check")
def check_client_settings(settings: str = Form("")):
    """What a settings text would offer each tier, without saving it: the
    card's live view while the admin edits. Validated by the same
    ``client_settings.parse``; an invalid text is a 400 saying why. Writes
    nothing (and so is not audited)."""
    try:
        return JSONResponse({"ok": True, **client_settings.check(settings)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/client-settings/reset")
def reset_client_settings(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
):
    """Remove the client settings file: the shipped
    ``client_settings.json.example`` is offered again."""
    try:
        client_settings.reset()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("Client settings reset to the shipped example | admin={}", admin_user.username)
    if _wants_json(request):
        return JSONResponse(_client_setup_reply())
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/client-rules/{rule_id}/mail")
def send_client_rule_mail(
    rule_id: str,
    request: Request,
    background: BackgroundTasks,
    only_unmailed: str = Form("on"),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Queue a mail to the accounts currently violating a check: those not
    mailed yet (default) or all of them. Mail never goes out as one burst:
    the first batch is sent after this reply and the mail job sends the
    rest, a batch per run (``client_mail.mail_pace``). Each account's state
    (queued, sent, error) shows in the check's list."""
    rule = _client_rule_or_404(session, rule_id)
    if rule.deprecated:
        raise HTTPException(status_code=400, detail=f"{rule.id} is deprecated: its accounts are no longer mailed.")
    if not mailer.is_enabled():
        raise HTTPException(status_code=400, detail="Mail is not configured (see .env).")
    unmailed_only = only_unmailed == "on"
    queued = client_mail.request_mail(session, rule.id, unmailed_only)
    if queued:
        background.add_task(client_mail.send_batch)
    pace = client_mail.mail_pace()
    logger.info(
        "Client rule mail queued | id={} accounts={} only_unmailed={} admin={}",
        rule.id, queued, unmailed_only, admin_user.username,
    )
    audit.note(mail_queued=queued)
    if _wants_json(request):
        return JSONResponse({
            "ok": True, "queued": queued, "batch": pace["batch"],
            "interval_minutes": round(pace["interval_s"] / 60, 1),
        })
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/client-rules/{rule_id}/patch.{fmt}")
def preview_client_rule_patch(
    rule_id: str,
    fmt: str,
    audience: str = Query("local"),
    session: Session = Depends(get_session),
):
    """The patch script one kind of account downloads (``audience``:
    ``local``, ``azure`` or ``aws``; ``fmt``: ``bat`` for Windows, ``sh``
    for Linux / macOS), for the admin to check."""
    rule = _client_rule_or_404(session, rule_id)
    if fmt not in client_patch.PATCH_FORMATS:
        raise HTTPException(status_code=404, detail="No such patch format.")
    try:
        env = client_rules.effective_env(rule, *client_rules.audience_flags(audience))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not env:
        raise HTTPException(status_code=404, detail="This check has no settings patch for that audience.")
    filename = f"claude-code-patch-{rule.id}-{audience}.{fmt}"
    return Response(
        content=client_patch.render_patch(fmt, f"{rule.id} {rule.name}", env, filename).encode("utf-8"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
