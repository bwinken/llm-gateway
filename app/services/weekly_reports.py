"""Weekly report mails: when they go out, to whom, and sending them.

Two reports (``REPORTS``), each about the last full Asia/Taipei week
(Monday to Sunday): ``usage`` (tokens by backend, latency p50 / p95 per
model, error rate) and ``guardrail`` (verdicts, categories, accounts to
look at). What they say is ``report_content``; this module is the engine.

Each report has its own row in ``report_schedules``: on / off, a weekday and
time (Asia/Taipei) and its recipients, set on the admin's *Weekly reports*
card. A report without a row is off.

``JOB`` ticks every few minutes inside the gateway (``scheduler``), so one
worker looks at a time. A report is due once this week's weekday and time
have passed and the week before has not been reported: the job claims it by
moving ``last_period`` forward with a conditional UPDATE, then builds and
sends it. So:

* a gateway that was down at the scheduled minute sends when it is back,
  any time that week;
* a week is mailed once, however many workers and hosts run;
* a failed send is tried again after ``RETRY_S``, in the same week only;
* turning a report on (or moving its time) after this week's time has
  passed does not mail at once: the first automatic mail is next week's,
  and *Send now* is there for the one wanted today.

A report is one mail to all its recipients, so there is no burst to pace.
Recipients are mailboxes of the company domain only, like test mail: the
gateway cannot be made to mail usage figures or account names elsewhere.
Every attempt is written to the mail log (``client_rules.record_mail``),
one row per recipient.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, update
from sqlmodel import Session, select

from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ
from app.models.schema import ReportSchedule
from app.services import client_rules, langfuse_reader, mailer, report_content
from app.services.report_content import Period, Rendered, week_of
from app.services.scheduler import Job, env_seconds

MAX_RECIPIENTS = 20
RETRY_S = 6 * 3600
DEFAULT_WEEKDAY = 0
DEFAULT_TIME = "09:00"
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Report:
    name: str
    title: str
    summary: str
    build: Callable[[Session, Period], Rendered]
    # Nothing to say without Langfuse: the job skips it instead of mailing an empty report.
    needs_langfuse: bool = False


REPORTS: dict[str, Report] = {
    "usage": Report(
        name="usage",
        title="Usage report",
        summary=(
            "Last week's tokens by backend (local / Azure / AWS) with a chart per day, p50 and p95 latency "
            "per model as line charts, and the error rate."
        ),
        build=report_content.usage_report,
    ),
    "guardrail": Report(
        name="guardrail",
        title="Guardrail report",
        summary=(
            "Last week's guardrail verdicts and categories, what changed against the week before, "
            "and the accounts worth a look."
        ),
        build=report_content.guardrail_report,
        needs_langfuse=True,
    ),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local(now: datetime) -> datetime:
    """A naive-UTC instant as Asia/Taipei."""
    return now.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


# ── Settings ──

def parse_time(text: str) -> str:
    match = _TIME.match((text or "").strip())
    if not match:
        raise ValueError("Time must be HH:MM (24-hour, Asia/Taipei).")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def parse_weekday(value: Any) -> int:
    try:
        weekday = int(value)
    except (TypeError, ValueError):
        raise ValueError("Pick a day of the week.")
    if not 0 <= weekday <= 6:
        raise ValueError("Pick a day of the week.")
    return weekday


def parse_recipients(text: str) -> list[str]:
    """Usernames or addresses, separated by commas, semicolons or whitespace,
    as the mailboxes they name. Raises ValueError for one outside the
    company domain, or for too many."""
    out: list[str] = []
    for token in re.split(r"[,;\s]+", text or ""):
        token = token.strip()
        if not token:
            continue
        address = mailer.address_for(token).lower()
        if not mailer.in_company_domain(address):
            domain = mailer.settings()["domain"]
            raise ValueError(
                f"{token!r} is not a mailbox of @{domain}." if domain
                else "Mail is not configured (COMPANY_EMAIL_DOMAIN), so no recipient can be checked."
            )
        if address not in out:
            out.append(address)
    if len(out) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients per report.")
    return out


def recipients_of(row: ReportSchedule | None) -> list[str]:
    return [a for a in (row.recipients if row else "").split(",") if a]


def slot(period: Period, weekday: int, send_time: str) -> datetime:
    """When the report about ``period`` is scheduled: the given weekday and
    time of the week after it (aware, Asia/Taipei)."""
    hour, minute = (int(part) for part in send_time.split(":"))
    return datetime.combine(period.end + timedelta(days=weekday), time(hour, minute), tzinfo=LOCAL_TZ)


def current_period(now: datetime | None = None) -> Period:
    """The week the next report is about: the last full one."""
    return week_of(_local(now or utcnow()).date()).previous


def save_schedule(
    session: Session, name: str, *, enabled: bool, weekday: int, send_time: str, recipients: list[str],
    updated_by: str = "", now: datetime | None = None,
) -> ReportSchedule:
    """Store a report's schedule. An enabled report needs a recipient. When
    this week's time has already passed and the week has not been reported,
    the week is marked as skipped, so saving never mails by itself."""
    if name not in REPORTS:
        raise ValueError("Unknown report.")
    if enabled and not recipients:
        raise ValueError("Add at least one recipient before turning the report on.")
    now = now or utcnow()
    row = session.get(ReportSchedule, name) or ReportSchedule(name=name)
    row.enabled, row.weekday, row.send_time = enabled, weekday, send_time
    row.recipients = ",".join(recipients)
    row.updated_by, row.updated_at = updated_by, now
    period = current_period(now)
    if enabled and _local(now) >= slot(period, weekday, send_time) and (row.last_period is None or row.last_period < period.start):
        row.last_period = period.start
        row.last_status = "skipped"
        row.last_detail = "Saved after this week's time: the first automatic mail is next week's."
        row.last_attempt_at = now
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def schedules(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every report with its schedule, for the admin card."""
    now = now or utcnow()
    rows = {row.name: row for row in session.exec(select(ReportSchedule)).all()}
    period = current_period(now)
    out = []
    for report in REPORTS.values():
        row = rows.get(report.name)
        weekday = row.weekday if row else DEFAULT_WEEKDAY
        send_time = row.send_time if row else DEFAULT_TIME
        upcoming = None
        if row and row.enabled:
            done = row.last_period is not None and row.last_period >= period.start
            target = period if not done else Period(period.start + timedelta(days=7))
            upcoming = slot(target, weekday, send_time).strftime("%a %Y-%m-%d %H:%M")
        out.append({
            "name": report.name, "title": report.title, "summary": report.summary,
            "needs_langfuse": report.needs_langfuse,
            "enabled": bool(row and row.enabled), "weekday": weekday, "send_time": send_time,
            "recipients": recipients_of(row),
            "last_status": row.last_status if row else "", "last_detail": row.last_detail if row else "",
            "last_period": Period(row.last_period).label if row and row.last_period else "",
            "last_attempt": _local(row.last_attempt_at).strftime("%Y-%m-%d %H:%M") if row and row.last_attempt_at else "",
            "next": upcoming,
        })
    return out


# ── Rendering and sending ──

def render(session: Session, name: str, period: Period | None = None) -> Rendered:
    report = REPORTS[name]
    return report.build(session, period or current_period())


def preview_html(rendered: Rendered) -> str:
    """The mail as a browser shows it: the ``cid:`` images inlined."""
    body = rendered.html
    for cid, content in rendered.images.items():
        body = body.replace(f'src="cid:{cid}"', f'src="data:image/png;base64,{base64.b64encode(content).decode()}"')
    return body


def send(
    session: Session, name: str, recipients: list[str], *, period: Period | None = None, kind: str = "report",
    actor: str = "", subject_prefix: str = "",
) -> tuple[bool, str, Rendered | None]:
    """Build the report and send it as one mail to ``recipients``. Blocking.
    ``(ok, detail, rendered)``; never raises. Every attempt goes to the mail
    log, one row per recipient."""
    if not recipients:
        return False, "No recipients.", None
    if not mailer.is_enabled():
        return False, "Mail is not configured.", None
    try:
        rendered = render(session, name, period)
    except Exception as exc:
        session.rollback()
        logger.error("Weekly report failed to build | report={} error={}: {}", name, type(exc).__name__, exc)
        return False, f"Could not build the report: {type(exc).__name__}", None
    subject = subject_prefix + rendered.subject
    ok, error = mailer.send(recipients, subject, rendered.html, rendered.images)
    for address in recipients:
        client_rules.record_mail(
            session, address=address, subject=subject, ok=ok, error=error, rule_ids=[f"report:{name}"],
            kind=kind, actor=actor,
        )
    detail = error if not ok else "; ".join(rendered.problems)
    return ok, detail, rendered


# ── The job ──

def _claim(session: Session, name: str, period: Period, now: datetime) -> bool:
    """Take this week's report for this worker: true for exactly one caller,
    and again after ``RETRY_S`` when the attempt before it failed."""
    result = session.execute(
        update(ReportSchedule)
        .where(ReportSchedule.name == name)
        .where(ReportSchedule.enabled.is_(True))  # type: ignore[attr-defined]
        .where(or_(
            ReportSchedule.last_period.is_(None),  # type: ignore[union-attr]
            ReportSchedule.last_period < period.start,  # type: ignore[operator]
            and_(
                ReportSchedule.last_period == period.start,
                ReportSchedule.last_status == "failed",
                ReportSchedule.last_attempt_at <= now - timedelta(seconds=RETRY_S),  # type: ignore[operator]
            ),
        ))
        .values(last_period=period.start, last_status="sending", last_detail="", last_attempt_at=now)
    )
    session.commit()
    return result.rowcount == 1


def _finish(session: Session, name: str, status: str, detail: str) -> None:
    session.execute(
        update(ReportSchedule).where(ReportSchedule.name == name)
        .values(last_status=status, last_detail=(detail or "")[:500])
    )
    session.commit()


def run_due(session: Session, now: datetime | None = None) -> str:
    """The ``weekly_reports`` job: send every report whose time has come."""
    now = now or utcnow()
    period = current_period(now)
    done: list[str] = []
    for row in session.exec(select(ReportSchedule).where(ReportSchedule.enabled.is_(True))).all():  # type: ignore[attr-defined]
        report = REPORTS.get(row.name)
        if report is None or _local(now) < slot(period, row.weekday, row.send_time):
            continue
        name, recipients = row.name, recipients_of(row)
        if not mailer.is_enabled():
            done.append(f"{name}=mail not configured")
            continue
        if not _claim(session, name, period, now):
            continue
        if report.needs_langfuse and not langfuse_reader.is_configured():
            _finish(session, name, "skipped", "Langfuse is not configured: there is nothing to report.")
            done.append(f"{name}=skipped")
            continue
        ok, detail, _rendered = send(session, name, recipients, period=period)
        _finish(session, name, "sent" if ok else "failed", detail)
        done.append(f"{name}={'sent' if ok else 'failed'}")
    return " ".join(done) or "nothing due"


JOB = Job(
    name="weekly_reports",
    interval=lambda: env_seconds("WEEKLY_REPORT_TICK_S", 300.0),
    run=run_due,
)
