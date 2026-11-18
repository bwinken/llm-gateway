"""Mail about client-rule violations: one mail per account about everything it
has open, sent paced, with every attempt in the mail log.

- **What is sent**: ``render_digest`` (``render_mail`` for one check), from
  the account's ``client_rules.open_items`` and one ``combined_env`` patch.
- **Who is due**: an admin's click queues (``request_mail`` for one check's
  accounts, ``request_mail_all`` for everyone with something open), a check's
  ``auto_email`` and its reminder, and the monthly reminder
  (``queue_monthly``). ``due_list`` is the one answer.
- **How it goes out**: ``send_due``, one batch per run (``mail_pace``), from
  the ``client_rule_mail`` job (``MAIL_JOB``) or right after a click
  (``send_batch``). ``_send_digests`` claims each account before sending, so
  two senders never mail the same one.
- **The mail log** (``client_mail_log``): ``record_mail`` writes every
  attempt, test mail and the weekly reports' included; it never raises.

- **Model changes** (``model_changes.py``): the mail about a model that is
  being replaced (``render_model_changes``) goes out through the same
  ``send_due``, in what is left of the batch after the notices, so the two
  together never exceed the pace.

What a violation is, whether it still counts and which patch an account gets
are ``client_rules``'; who a model change is for and who is waiting for its
mail are ``model_changes``'. This module only reads them.
"""

from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, func, select, update

from app.core import config
from app.core.config import APP_TITLE
from app.core.database import engine
from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ
from app.models.schema import (
    ClientMailLog,
    ClientViolation,
    User,
    service_account_clause,
    service_account_names,
)
from app.services import mailer, model_changes
from app.services.client_patch import env_snippet
from app.services.client_rules import (
    audience_flags,
    combined_env,
    get_rule,
    is_resolved,
    open_items,
    rules,
    split_covered,
    stored_audience,
    utcnow,
    violations,
)
from app.services.scheduler import Job, env_seconds


# The monthly reminder (when an admin turned it on): on this day of the
# month, from this hour, Asia/Taipei.
MONTHLY_DAY = 1


MONTHLY_HOUR = 9


# A failed send is retried after this long.
MAIL_RETRY_S = 6 * 3600


# The mail log (client_mail_log): why a mail went out, rows per page in the
# admin's list, and the days its "sent / failed" counts cover.
MAIL_LOG_PAGE = 15


MAIL_LOG_DAYS = 30


def mail_pace() -> dict[str, Any]:
    """How fast mail goes out, so a long list never reaches the mail server
    as one burst: at most ``batch`` mails per run of the mail job, ``pause_s``
    apart, one run every ``interval_s``. Whatever is left waits for the next
    run. An admin's click is paced the same way as automatic mail."""
    return {
        "batch": max(1, int(env_seconds("CLIENT_RULE_MAIL_BATCH", 20))),
        "pause_s": env_seconds("CLIENT_RULE_MAIL_PAUSE_S", 2.0),
        "interval_s": env_seconds("CLIENT_RULE_MAIL_INTERVAL_S", 600.0),
    }


# ── Mail: one per account, covering everything it has open ──

def render_digest(
    username: str, rule_list: list[Any], env: dict[str, str | None], manual: list[str] | None = None,
) -> tuple[str, str]:
    """``(subject, html)`` of the mail to one account about the checks in
    ``rule_list`` (each needs ``id``, ``title``, ``message``,
    ``email_subject``), with ``env`` as the one patch that covers them all
    and ``manual`` the ids of those it cannot fix (``split_covered``). With
    ``PUBLIC_BASE_URL`` set the mail links straight to the account's patch
    download, so the fix is one click from the inbox. Everything an admin
    wrote is escaped."""
    public_url = mailer.settings()["public_url"]
    count = len(rule_list)
    manual = list(manual or [])
    if count == 1:
        subject = (rule_list[0].email_subject or f"[{APP_TITLE}] {rule_list[0].title}").strip()
    else:
        subject = f"[{APP_TITLE}] {count} things to change in your Claude Code setup"
    parts = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;line-height:1.6">',
        f"<p>Hi {html.escape(username)},</p>",
    ]
    if count > 1:
        intro = f"Requests from your account show {count} things to change in your Claude Code setup."
        if env and not manual:
            intro += " One script fixes them all: see the end of this mail."
        elif env:
            intro += (
                f" One script fixes {count - len(manual)} of them (see the end of this mail); "
                f"{html.escape(', '.join(manual))} need{'s' if len(manual) == 1 else ''} something else, described below."
            )
        parts.append(f"<p>{intro}</p>")
    for number, rule in enumerate(rule_list, 1):
        heading = f"{number}. {rule.title}" if count > 1 else rule.title
        parts.append(f'<p style="font-size:16px;font-weight:600;margin-bottom:4px">{html.escape(heading)}</p>')
        if rule.message:
            parts.append("<p>" + html.escape(rule.message).replace("\n", "<br>") + "</p>")
    notice = "these notices" if count > 1 else "this notice"
    if env:
        if public_url:
            # One click from the inbox: the same download the dashboard offers
            # (the account's patch as it stands when the link is opened).
            base = html.escape(f"{public_url}/dashboard/client-rules/all/patch", quote=True)
            parts.append(
                '<p style="font-size:16px;font-weight:600;margin-bottom:4px">The quickest fix: one script</p>'
                "<p>Download it, run it and restart Claude Code: "
                f'<a href="{base}.bat">Windows (.bat)</a> &middot; <a href="{base}.sh">Linux (.sh)</a>. '
                "It sets only the variables listed below, leaves the rest of the file alone and keeps a backup. "
                "You may be asked to sign in first.</p>"
            )
            lead = "Or by hand: everything in one block, for" if count > 1 else "Or by hand: the environment settings for"
        else:
            lead = "Everything above in one block, for" if count > 1 else "Environment settings for"
        parts.append(
            f"<p>{lead} <code>~/.claude/settings.json</code>:</p>"
            '<pre style="background:#f4f4f4;padding:10px;border-radius:4px;font-size:13px">'
            + html.escape(env_snippet(env)) + "</pre>"
        )
    if public_url:
        link = html.escape(f"{public_url}/dashboard", quote=True)
        done = "them" if count > 1 else "it"
        parts.append(f'<p><a href="{link}">Open your dashboard</a> to see {notice} and mark {done} as done.</p>')
    references = ", ".join(html.escape(rule.id) for rule in rule_list)
    parts.append(_footer(public_url, "sent because of requests from your account", references))
    return subject, "".join(parts)


def _support_url(public_url: str) -> str:
    """The admin's support link as something a mail can link to: an
    ``http(s)://`` URL as it is, a site-relative one under ``PUBLIC_BASE_URL``
    (nothing without it). Never raises."""
    try:
        url = (config.get_site_links().get("support_bot_url") or "").strip()
    except Exception:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/") and not url.startswith("//") and public_url:
        return public_url + url
    return ""


def _footer(public_url: str, why: str, references: str) -> str:
    """The closing lines of every mail to an account: the sender is a system
    mailbox nobody reads, so say so, and say where help is when the admin
    has set a support link."""
    help_line = ""
    support = _support_url(public_url)
    if support:
        help_line = f' For help, use <a href="{html.escape(support, quote=True)}">the support page</a>.'
    return (
        '<p style="color:#888;font-size:12px;border-top:1px solid #e3e6ea;padding-top:10px;margin-top:18px">'
        f"This is an automated message from {html.escape(APP_TITLE)}, {why}. "
        f"Please do not reply to it: replies are not read.{help_line} Reference: {references}.</p></div>"
    )


def render_model_changes(
    username: str, changes: list[Any], renames: dict[str, str],
) -> tuple[str, str]:
    """``(subject, html)`` of the mail to one account about the model
    changes in ``changes`` (each needs ``id``, ``title``, ``message``,
    ``email_subject``, ``show_until``), with ``renames`` as what this
    account replaces (``model_changes.account_renames``). With
    ``PUBLIC_BASE_URL`` set it links straight to the account's one patch
    download. Everything an admin wrote is escaped."""
    public_url = mailer.settings()["public_url"]
    count = len(changes)
    if count == 1:
        subject = (changes[0].email_subject or f"[{APP_TITLE}] {changes[0].title}").strip()
    else:
        subject = f"[{APP_TITLE}] {count} model changes: please update your Claude Code setup"
    parts = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;line-height:1.6">',
        f"<p>Hi {html.escape(username)},</p>",
    ]
    for number, change in enumerate(changes, 1):
        heading = f"{number}. {change.title}" if count > 1 else change.title
        parts.append(f'<p style="font-size:16px;font-weight:600;margin-bottom:4px">{html.escape(heading)}</p>')
        if change.message:
            parts.append("<p>" + html.escape(change.message).replace("\n", "<br>") + "</p>")
    rows = "".join(
        '<tr><td style="padding:4px 14px 4px 0;font-family:Consolas,monospace">' + html.escape(old) + "</td>"
        '<td style="padding:4px 14px 4px 0;color:#888">&rarr;</td>'
        '<td style="padding:4px 0;font-family:Consolas,monospace;font-weight:600">' + html.escape(new) + "</td></tr>"
        for old, new in renames.items()
    )
    parts.append(
        '<p style="font-size:16px;font-weight:600;margin-bottom:4px">What to change</p>'
        "<p>Wherever your Claude Code setup names a model on the left, name the one on the right instead:</p>"
        f'<table style="border-collapse:collapse;font-size:13px;margin-bottom:8px">{rows}</table>'
    )
    if public_url:
        base = html.escape(f"{public_url}/dashboard/client-rules/all/patch", quote=True)
        parts.append(
            '<p style="font-size:16px;font-weight:600;margin-bottom:4px">The quickest way: one script</p>'
            "<p>Download it, run it on each machine you use Claude Code on and restart Claude Code: "
            f'<a href="{base}.bat">Windows (.bat)</a> &middot; <a href="{base}.sh">Linux (.sh)</a>. '
            "It only replaces these names where <code>~/.claude/settings.json</code> has them "
            "(the <code>model</code> setting and the model variables in <code>env</code>), leaves a model you chose "
            "yourself alone and keeps a backup. You may be asked to sign in first.</p>"
        )
    parts.append(
        "<p>By hand: open <code>~/.claude/settings.json</code> and replace the name in <code>model</code> and in "
        "the <code>env</code> variables that hold a model (<code>ANTHROPIC_MODEL</code>, "
        "<code>ANTHROPIC_SMALL_FAST_MODEL</code>, ...). A project's own <code>.claude/settings.json</code> and "
        "other tools that name the model need the same change; no script reaches those.</p>"
    )
    until = max(change.show_until for change in changes)
    if public_url:
        link = html.escape(f"{public_url}/dashboard", quote=True)
        parts.append(
            f'<p><a href="{link}">Your dashboard</a> shows this notice and the script until '
            f"{html.escape(model_changes.local_day(until))}.</p>"
        )
    references = ", ".join(html.escape(model_changes.ref(change.id)) for change in changes)
    parts.append(_footer(public_url, "sent to the accounts this model change is for", references))
    return subject, "".join(parts)


def render_mail(rule: Any, username: str, env: dict[str, str | None]) -> tuple[str, str]:
    """The mail about one check alone (the admin's test mail)."""
    return render_digest(username, [rule], env)


def render_digest_preview(session: Session, username: str, audience: str = "local") -> tuple[str, str, list[str]]:
    """``(subject, html, rule ids)`` of the mail an account of ``audience``
    would get with every check open that can mail (enabled, not deprecated),
    as saved: the admin's test of the mail about several things at once,
    with the one merged patch. Raises ValueError for an unknown audience or
    when no check is on."""
    azure, aws = audience_flags(audience)
    rule_list = [rule for rule in rules(session) if rule.enabled and not rule.deprecated]
    if not rule_list:
        raise ValueError("No rule is on, so there is nothing to mail.")
    subject, body = render_digest(
        username, rule_list, combined_env(rule_list, azure, aws), split_covered(rule_list, azure, aws)[1],
    )
    return subject, body, [rule.id for rule in rule_list]


def mailable(session: Session, rows: list[ClientViolation]) -> list[ClientViolation]:
    """Violations still worth a mail: the account exists, has a mailbox (not
    a service account) and has not reported the fix."""
    names = [v.username for v in rows if not is_resolved(v)]
    if not names:
        return []
    known = set(session.exec(
        select(User.username).where(User.username.in_(names), ~service_account_clause())  # type: ignore[attr-defined]
    ).all())
    return [v for v in rows if v.username in known and not is_resolved(v)]


def mail_targets(session: Session, rule_id: str, only_unmailed: bool, now: datetime | None = None) -> list[ClientViolation]:
    """Who a manual send from a check reaches: its accounts inside the window
    (nobody for a deprecated or unknown check)."""
    rule = get_rule(session, rule_id)
    if rule is None or rule.deprecated:
        return []
    rows = mailable(session, violations(session, rule_id, now))
    return [v for v in rows if v.mailed_at is None] if only_unmailed else rows


def record_mail(
    session: Session, *, address: str, subject: str, ok: bool, error: str = "", username: str = "",
    rule_ids: list[str] | None = None, kind: str = "requested", actor: str = "", now: datetime | None = None,
) -> None:
    """Write one mail attempt to the mail log. Never raises: a mail that
    went out is not undone by its record failing, so a failed write is
    logged whole instead."""
    try:
        session.add(ClientMailLog(
            created_at=now or utcnow(), username=username, address=address, subject=subject[:300],
            rule_ids=",".join(rule_ids or []), kind=kind, ok=ok, error=(error or "")[:300], actor=actor,
        ))
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error(
            "Mail log write failed | username={} address={} rules={} kind={} ok={} mail_error={!r} error={}: {}",
            username, address, ",".join(rule_ids or []), kind, ok, error, type(exc).__name__, exc,
        )


def mail_log(
    session: Session, status: str = "", q: str = "", page: int = 1, per_page: int = MAIL_LOG_PAGE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The mail log, newest first, one page of it. ``status`` narrows it to
    ``sent``, ``failed`` or ``test`` mails (anything else: all of them);
    ``q`` to accounts or addresses containing it. ``sent`` / ``failed`` are
    the counts of the last ``MAIL_LOG_DAYS`` days, whatever the filter."""
    now = now or utcnow()
    clauses = []
    if status == "sent":
        clauses.append(ClientMailLog.ok.is_(True))  # type: ignore[attr-defined]
    elif status == "failed":
        clauses.append(ClientMailLog.ok.is_(False))  # type: ignore[attr-defined]
    elif status == "test":
        clauses.append(ClientMailLog.kind == "test")
    needle = (q or "").strip().lower()
    if needle:
        clauses.append(
            func.lower(ClientMailLog.username).contains(needle, autoescape=True)
            | func.lower(ClientMailLog.address).contains(needle, autoescape=True)
        )
    total = session.exec(select(func.count()).select_from(ClientMailLog).where(*clauses)).one()
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    rows = session.exec(
        select(ClientMailLog).where(*clauses)
        .order_by(ClientMailLog.created_at.desc(), ClientMailLog.id.desc())  # type: ignore[attr-defined,union-attr]
        .offset((page - 1) * per_page).limit(per_page)
    ).all()
    recent = dict(session.exec(
        select(ClientMailLog.ok, func.count()).where(ClientMailLog.created_at >= now - timedelta(days=MAIL_LOG_DAYS))
        .group_by(ClientMailLog.ok)
    ).all())
    return {
        "rows": list(rows), "total": total, "page": page, "pages": pages, "per_page": per_page,
        "sent": recent.get(True, 0), "failed": recent.get(False, 0), "days": MAIL_LOG_DAYS,
    }


def _send_digests(
    session: Session, usernames: list[str], now: datetime | None = None, pause_s: float = 0.0,
    kinds: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Mail each account once about everything it has open (deprecated
    checks left out), ``pause_s`` apart, stamp the outcome on every
    violation the mail covered and write it to the mail log (``kinds``:
    why each account is being mailed, ``requested`` when not given).
    Returns (sent, failed) in accounts."""
    usernames = list(dict.fromkeys(usernames))
    if not usernames:
        return 0, 0
    users = {
        u.username: u
        for u in session.exec(select(User).where(User.username.in_(usernames))).all()  # type: ignore[attr-defined]
        if not u.is_service_account  # no mailbox
    }
    by_account = open_items(session, now, list(users))
    jobs = []
    for username in usernames:
        user = users.get(username)
        items = [(rule, row) for rule, row in by_account.get(username, []) if not rule.deprecated]
        if user is None or not items:
            continue
        rule_list = [rule for rule, _ in items]
        azure, aws = stored_audience(user)
        subject, body = render_digest(
            username, rule_list, combined_env(rule_list, azure, aws), split_covered(rule_list, azure, aws)[1],
        )
        jobs.append((
            username, [row.id for _, row in items], items[0][1].mail_attempted_at, subject, body,
            [rule.id for rule in rule_list],
        ))
    session.commit()                # nothing is held open while mail is being sent
    sent = failed = 0
    for position, (username, violation_ids, attempted_before, subject, body, rule_ids) in enumerate(jobs):
        # The caller's clock when it gave one (a run "as of" a time), else the real one.
        stamp = now if now is not None else utcnow()
        # Claim the account before sending: a compare-and-set on its first
        # violation, so the job and an admin's click (or two workers) never
        # both mail the same person.
        first = ClientViolation.id == violation_ids[0]
        unchanged = (
            ClientViolation.mail_attempted_at.is_(None) if attempted_before is None  # type: ignore[union-attr]
            else ClientViolation.mail_attempted_at == attempted_before
        )
        claimed = session.execute(
            update(ClientViolation).where(first, unchanged).values(mail_attempted_at=stamp)
        ).rowcount == 1
        session.commit()
        if not claimed:
            continue
        if position and pause_s > 0:
            time.sleep(pause_s)
        address = mailer.address_for(username)
        ok, error = mailer.send(address, subject, body)
        stamp = now if now is not None else utcnow()
        values: dict[str, Any] = {"mail_attempted_at": stamp, "mail_error": "" if ok else error}
        if ok:
            values["mailed_at"] = stamp
            sent += 1
        else:
            failed += 1
        session.execute(update(ClientViolation).where(ClientViolation.id.in_(violation_ids)).values(**values))  # type: ignore[union-attr]
        session.commit()
        record_mail(
            session, username=username, address=address, subject=subject, rule_ids=rule_ids,
            kind=(kinds or {}).get(username, "requested"), ok=ok, error=error, now=stamp,
        )
    return sent, failed


def _send_model_changes(
    session: Session, usernames: list[str], now: datetime | None = None, pause_s: float = 0.0,
    pause_first: bool = False,
) -> tuple[int, int]:
    """Mail each account once about every model change it is waiting for a
    mail about (``model_changes.mail_items``), ``pause_s`` apart, stamp the
    outcome on each state the mail covered and write it to the mail log.
    ``pause_first``: mail went out just before this call, so the first one
    here waits too. Returns (sent, failed) in accounts."""
    usernames = list(dict.fromkeys(usernames))
    if not usernames:
        return 0, 0
    by_account = model_changes.mail_items(session, now, MAIL_RETRY_S, usernames)
    users = {
        u.username: u
        for u in session.exec(select(User).where(User.username.in_(list(by_account)))).all()  # type: ignore[attr-defined]
    }
    jobs = []
    for username in usernames:
        items = by_account.get(username)
        user = users.get(username)
        if not items or user is None:
            continue
        changes = [change for change, _ in items]
        renames = model_changes.account_renames(user, changes)
        if not renames:      # its access changed since it was queued: the change is no longer for it
            continue
        subject, body = render_model_changes(username, changes, renames)
        # Read now: the commit below expires these rows, and the claim must
        # compare with the attempt as it was when this mail was prepared.
        jobs.append((
            username, [state.id for _, state in items], items[0][1].mail_attempted_at, subject, body,
            [model_changes.ref(c.id) for c in changes],
        ))
    session.commit()                # nothing is held open while mail is being sent
    sent = failed = 0
    for position, (username, state_ids, attempted_before, subject, body, refs) in enumerate(jobs):
        stamp = now if now is not None else utcnow()
        if not model_changes.claim_mail(session, state_ids[0], attempted_before, stamp):
            continue
        if (position or pause_first) and pause_s > 0:
            time.sleep(pause_s)
        address = mailer.address_for(username)
        ok, error = mailer.send(address, subject, body)
        stamp = now if now is not None else utcnow()
        model_changes.stamp_mail(session, state_ids, stamp, ok, error)
        sent += ok
        failed += not ok
        record_mail(
            session, username=username, address=address, subject=subject, rule_ids=refs,
            kind="model_change", ok=ok, error=error, now=stamp,
        )
    return sent, failed


def _auto_due(violation: ClientViolation, repeat_days: int, now: datetime) -> bool:
    if violation.mail_attempted_at is None:
        return True
    if violation.mail_attempted_at > now - timedelta(seconds=MAIL_RETRY_S):
        return False
    if violation.mail_error or violation.mailed_at is None:
        return True  # the last attempt failed
    # A reminder is for an account still doing it: seen again since its last mail.
    return (
        repeat_days > 0 and violation.mailed_at <= now - timedelta(days=repeat_days)
        and violation.last_seen_at > violation.mailed_at
    )


def is_queued(violation: ClientViolation, now: datetime | None = None) -> bool:
    """An admin asked for this account to be mailed and it has not happened
    yet (a failed attempt waits ``MAIL_RETRY_S`` before the next)."""
    requested = violation.mail_requested_at
    if requested is None or is_resolved(violation):
        return False
    if violation.mailed_at is not None and violation.mailed_at >= requested:
        return False
    return True


def _request_due(violation: ClientViolation, now: datetime) -> bool:
    if not is_queued(violation, now):
        return False
    attempted = violation.mail_attempted_at
    if attempted is not None and attempted >= violation.mail_requested_at:
        return attempted <= now - timedelta(seconds=MAIL_RETRY_S)   # tried since the request, and failed
    return True


def _queue_accounts(session: Session, usernames: list[str], now: datetime) -> int:
    """Queue ``usernames`` for a mail: stamp the request on everything each
    has open (deprecated checks left out), because the one mail it gets
    covers all of it. Every check's list then shows the account as queued,
    not only the list the admin clicked in. Returns accounts queued."""
    queued = 0
    for items in open_items(session, now, list(dict.fromkeys(usernames))).values():
        rows = [row for rule, row in items if not rule.deprecated]
        for row in rows:
            row.mail_requested_at = now
            session.add(row)
        queued += bool(rows)
    session.commit()
    return queued


def request_mail(session: Session, rule_id: str, only_unmailed: bool = True, now: datetime | None = None) -> int:
    """An admin's click on a check's mail button: queue its accounts (those
    not mailed yet, or all of them). Nothing is sent here; the mail job
    sends the queue in batches. Returns how many were queued."""
    now = now or utcnow()
    targets = mail_targets(session, rule_id, only_unmailed, now)
    return _queue_accounts(session, [row.username for row in targets], now)


def mail_all_targets(session: Session, only_unmailed: bool = True, now: datetime | None = None) -> list[str]:
    """Who "mail everyone" reaches: the accounts with a mailbox that have
    anything open under any check (deprecated ones left out); with
    ``only_unmailed``, those with something open they were never mailed about."""
    names = []
    for username, items in open_items(session, now).items():
        rows = [row for rule, row in items if not rule.deprecated]
        if not rows:
            continue
        if only_unmailed and all(row.mailed_at is not None for row in rows):
            continue
        names.append(username)
    if not names:
        return []
    known = set(session.exec(
        select(User.username).where(User.username.in_(names), ~service_account_clause())  # type: ignore[attr-defined]
    ).all())
    return sorted(name for name in names if name in known)


def request_mail_all(session: Session, only_unmailed: bool = True, now: datetime | None = None) -> int:
    """An admin's click on "mail everyone": queue every account of
    ``mail_all_targets``, whatever the check. Nothing is sent here."""
    now = now or utcnow()
    return _queue_accounts(session, mail_all_targets(session, only_unmailed, now), now)


def monthly_mail() -> dict[str, Any]:
    """The monthly reminder's switch and when it runs next (local time)."""
    local = datetime.now(LOCAL_TZ)
    run = local.replace(day=MONTHLY_DAY, hour=MONTHLY_HOUR, minute=0, second=0, microsecond=0)
    if local.day != MONTHLY_DAY:      # this month's day has passed (or the hour, on the day: it runs until midnight)
        run = (run.replace(day=28) + timedelta(days=4)).replace(day=MONTHLY_DAY)
    return {
        "enabled": config.get_client_monthly_mail(), "day": MONTHLY_DAY, "hour": MONTHLY_HOUR,
        "next": run.strftime("%Y-%m-%d %H:%M"),
    }


def queue_monthly(session: Session, now: datetime | None = None) -> int:
    """The monthly reminder: on ``MONTHLY_DAY`` from ``MONTHLY_HOUR``
    (Asia/Taipei), when an admin turned it on, queue every account that
    still has something open. Each violation is queued once a month, and
    only if the account has been seen doing it since it was last mailed.
    The queue then goes out in the usual batches. Returns accounts queued."""
    if not config.get_client_monthly_mail():
        return 0
    now = now or utcnow()
    local = now.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    if local.day != MONTHLY_DAY or local.hour < MONTHLY_HOUR:
        return 0
    month_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    queued = 0
    service = service_account_names(session)
    for username, items in open_items(session, now).items():
        if username in service:
            continue
        touched = False
        for rule, row in items:
            if rule.deprecated:
                continue
            if row.mail_requested_at is not None and row.mail_requested_at >= month_start:
                continue      # already queued this month
            if row.mailed_at is not None and row.last_seen_at <= row.mailed_at:
                continue      # told already, and not seen doing it since
            row.mail_requested_at = now
            session.add(row)
            touched = True
        queued += touched
    session.commit()
    return queued


def due_list(session: Session, now: datetime | None = None) -> list[tuple[str, str]]:
    """Accounts waiting for a mail, in the order they will get it, each with
    why (the mail log's ``kind``): first those an admin or the monthly
    reminder asked for (``requested``, oldest request first), then those
    due by a check's automatic mail (``automatic``). Deprecated checks never
    make an account due."""
    now = now or utcnow()
    requested: list[tuple[datetime, str]] = []
    automatic: list[str] = []
    service = service_account_names(session)
    for username, items in open_items(session, now).items():
        if username in service:
            continue
        live = [(rule, row) for rule, row in items if not rule.deprecated]
        asked = [row.mail_requested_at for _, row in live if _request_due(row, now)]
        if asked:
            requested.append((min(asked), username))
        elif any(rule.auto_email and _auto_due(row, rule.email_repeat_days, now) for rule, row in live):
            automatic.append(username)
    ordered = [(username, "requested") for _, username in sorted(requested)]
    ordered += [(username, "automatic") for username in sorted(automatic)]
    if not ordered:
        return []
    names = [username for username, _ in ordered]
    known = set(session.exec(select(User.username).where(User.username.in_(names))).all())  # type: ignore[attr-defined]
    return [(username, why) for username, why in ordered if username in known]


def due_accounts(session: Session, now: datetime | None = None) -> list[str]:
    """The accounts of ``due_list``, in its order."""
    return [username for username, _ in due_list(session, now)]


def send_due(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Send one batch: the first ``mail_pace()["batch"]`` accounts that are
    waiting, a pause apart: those due a mail about their notices first, then,
    in what is left of the batch, those waiting for a mail about a model
    change. ``waiting`` is what is left for the next run."""
    pace = mail_pace()
    due = due_list(session, now)
    batch = due[:pace["batch"]]
    sent, failed = _send_digests(session, [username for username, _ in batch], now, pace["pause_s"], dict(batch))
    changes_due = model_changes.due_mail(session, now, MAIL_RETRY_S)
    changes_batch = changes_due[:pace["batch"] - len(batch)]
    more_sent, more_failed = _send_model_changes(session, changes_batch, now, pace["pause_s"], pause_first=bool(batch))
    return {
        "sent": sent + more_sent, "failed": failed + more_failed,
        "waiting": len(due) - len(batch) + len(changes_due) - len(changes_batch),
    }


def send_batch(bind=None) -> dict[str, int]:
    """One batch, right now, in its own session: run after an admin's click
    so the first mails do not wait for the job's next run. Never raises."""
    try:
        if not mailer.is_enabled():
            return {"sent": 0, "failed": 0, "waiting": 0}
        with Session(bind or engine) as session:
            result = send_due(session)
        logger.info("Client rule mail batch | sent={} failed={} waiting={}", result["sent"], result["failed"], result["waiting"])
        return result
    except Exception as exc:
        logger.warning("Client rule mail batch failed | error={}: {}", type(exc).__name__, exc)
        return {"sent": 0, "failed": 0, "waiting": 0}


def send_rule_mail(rule_id: str, only_unmailed: bool = True, bind=None) -> dict[str, int]:
    """Queue a check's accounts and send the first batch: what an admin's
    click does, in one call. Returns ``{"sent", "failed"}`` of that batch."""
    try:
        with Session(bind or engine) as session:
            if get_rule(session, rule_id) is None or not mailer.is_enabled():
                return {"sent": 0, "failed": 0}
            request_mail(session, rule_id, only_unmailed)
        result = send_batch(bind)
        return {"sent": result["sent"], "failed": result["failed"]}
    except Exception as exc:
        logger.warning("Client rule mail failed | rule={} error={}: {}", rule_id, type(exc).__name__, exc)
        return {"sent": 0, "failed": 0}


def run_auto_mail(session: Session, now: datetime | None = None) -> str:
    """The ``client_rule_mail`` job: one batch per run. On the monthly
    reminder's day it first queues everyone who still has something open
    (``queue_monthly``). An account is due
    when an admin (or the monthly reminder) asked for it, or when one of its open checks has
    ``auto_email`` and has not been mailed (or is due again by that check's
    ``email_repeat_days``: weekly, monthly, ...). It gets one mail about
    everything it has open, which stamps all of it, so the shortest repeat
    among its checks sets the pace. Deprecated checks mail nobody."""
    if not mailer.is_enabled():
        return "mail not configured"
    monthly = queue_monthly(session, now)
    result = send_due(session, now)
    detail = f"sent={result['sent']} failed={result['failed']} waiting={result['waiting']}"
    return f"{detail} monthly_queued={monthly}" if monthly else detail


MAIL_JOB = Job(
    name="client_rule_mail",
    interval=lambda: env_seconds("CLIENT_RULE_MAIL_INTERVAL_S", 600.0),
    run=run_auto_mail,
)
