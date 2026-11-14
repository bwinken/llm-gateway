"""Client rules: who trips which client check, and how they are told.

The gateway cannot change a user's Claude Code settings. What it can do is
notice which accounts send requests of a certain kind and reach those people:

- **Checks** are code (``client_checks.CHECKS``), each with a violation id
  (``CC001``, ...). An admin does not write conditions; per check they set
  on / off, the action, the wording, the settings patch and mail
  (``client_rule_settings``; no row = the defaults in code). Nothing a
  check decides with is in the database.
- **Violation** (``client_violations``): an account tripped an enabled
  check. For ``WINDOW_DAYS`` after its last one the account sees the notice
  on its dashboard, with the patch (environment variables for the ``env``
  block of ``~/.claude/settings.json``, nothing else; a different set for
  local-only, Azure and AWS accounts) as text and as a download
  (``.bat`` for Windows, ``.sh`` for Linux).
- **Resolved**: the account can say "I've applied it" on its notice. The
  notice goes away at once and it is no longer mailed; the next violating
  request reopens it (the gateway cannot see a settings file, only requests).
- **Reminder** (``action = "remind"``): the first violating request of each
  local day is answered with the message as an error, so it shows in the
  terminal; the retry goes through. App accounts are never reminded.
- **Mail** (``client_mail.py``): to ``<username>@<COMPANY_EMAIL_DOMAIN>``
  (``mailer``), sent by an admin's click or by the ``client_rule_mail`` job
  for checks with ``auto_email``. One mail per account, about everything it
  has open, with one patch that covers it all (``open_items``,
  ``combined_env``) and a link straight to its download.
- **One patch per account**: the dashboard and the mail offer a single
  download, everything the account has open merged (``combined_for``),
  never one per check.
- **Patch scripts** (``client_patch.py``): parsing a patch and rendering the
  ``.bat`` / ``.sh`` that applies it, and the API key patch (``key_env``),
  which sets the account's current key in settings.json after it was
  regenerated. Which variables an account gets is decided here
  (``effective_env``, ``combined_env``).
- **Versions** (``client_versions``): the Claude Code version in each
  Messages request's User-Agent, per account.

Everything on the request path reads a per-worker snapshot of the enabled
checks, writes at most hourly per account, and never fails a request: a
broken store or a check that raises means no notice, not an error. The one
deliberate exception is the reminder, which is an admin's explicit choice
per check.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select, update
from starlette.concurrency import run_in_threadpool

from app.core.config import APP_TITLE
from app.core.database import engine
from app.core.logger import logger
from app.core.timeutil import local_today
from app.models.schema import ClientRuleSetting, ClientVersion, ClientViolation, User
from app.services import client_checks, mailer
from app.services.client_checks import Check, Facts, version_key
from app.services.client_patch import dump_env, env_snippet, load_env, parse_env


# How long after an account's last violation its notice (and its place in
# the admin lists and mail runs) stays. An account that has not tripped a
# check for this long, because it fixed its settings without saying so or
# stopped using Claude Code, is left alone: no notice, no mail. When it
# trips the check again it starts over as a new case and is told again.
WINDOW_DAYS = 15


# One write per account per worker per hour, per check and per version.
RESOLUTION_S = 3600


# Other workers pick up a settings change within this long.
REFRESH_S = 15.0


# The version overview counts accounts seen in this many days.
VERSION_WINDOW_DAYS = 30


# A check with no violation for this long is suggested for retirement.
STALE_DAYS = 30


ACTIONS = ("notice", "remind")


CLIENT = "claude-code"


_UA_VERSION = re.compile(r"claude-cli/(\d+(?:\.\d+){1,3})", re.IGNORECASE)


# Who gets which variant of a check's patch: every account gets the base
# (``patch``); an account with Azure access also gets ``patch_azure`` on top,
# one with AWS access ``patch_aws`` on top of that (AWS wins where both set
# the same variable). A local-only account gets the base alone.
AUDIENCES = ("local", "azure", "aws")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_version(user_agent: str | None) -> str:
    """Claude Code's version from a User-Agent (``claude-cli/2.1.302 (...)``),
    or "" for any other client."""
    match = _UA_VERSION.search(user_agent or "")
    return match.group(1) if match else ""


def audience_of(user: User) -> tuple[bool, bool]:
    """``(azure, aws)``: which cloud backends the account may use, for the
    person looking at their own dashboard or API: an AuthCenter admin on the
    web UI (``user.is_admin`` as ``get_web_user`` set it from the token)
    sees both, as on the rest of the dashboard."""
    return bool(user.can_use_azure or user.is_admin), bool(user.can_use_bedrock or user.is_admin)


def stored_audience(user: User) -> tuple[bool, bool]:
    """``(azure, aws)`` for an account read from the database with nobody
    signed in (mail): its flags and nothing else. The stored ``is_admin`` is
    retired and grants nothing."""
    return bool(user.can_use_azure), bool(user.can_use_bedrock)


def baseline_env() -> dict[str, str | None]:
    """``client_checks.BASELINE_ENV``: what every patch also sets."""
    return parse_env(client_checks.BASELINE_ENV)


def effective_env(rule: Any, azure: bool, aws: bool) -> dict[str, str | None]:
    """The patch one account gets: the base, then the Azure variant if it
    has Azure access, then the AWS variant if it has AWS access, and, when
    that leaves anything to apply, the settings every patch carries
    (``baseline_env``; the check's own value wins). ``rule`` is anything
    with ``patch`` / ``patch_azure`` / ``patch_aws`` in stored form."""
    env = dict(load_env(rule.patch))
    if azure:
        env.update(load_env(rule.patch_azure))
    if aws:
        env.update(load_env(rule.patch_aws))
    if env:
        for name, value in baseline_env().items():
            env.setdefault(name, value)
    return env


# ── The checks as configured ──

@dataclass(frozen=True)
class Rule:
    """A check with its settings: the admin's row when there is one, the
    check's defaults otherwise."""
    check: Check
    enabled: bool
    action: str
    title: str
    message: str
    patch: str          # stored form (JSON), like the columns
    patch_azure: str
    patch_aws: str
    email_subject: str
    auto_email: bool
    email_repeat_days: int
    customized: bool    # an admin has saved this check's form
    updated_by: str

    @property
    def id(self) -> str:
        return self.check.id

    @property
    def name(self) -> str:
        return self.check.name

    @property
    def missing_patch(self) -> bool:
        """On, its fix is a setting, and there is no patch for everyone:
        its accounts are told to change something without being told what.
        A save refuses this; it can only come from an older row."""
        return self.enabled and self.check.needs_patch and not load_env(self.patch)

    @property
    def deprecated(self) -> str:
        """Why the check is on its way out ("" while it is not). A deprecated
        check still records and shows its notice, but never reminds or mails."""
        return self.check.deprecated


def _default_patch(lines: str) -> str:
    return dump_env(parse_env(lines))


def _rule(check: Check, row: ClientRuleSetting | None) -> Rule:
    if row is None:
        return Rule(
            check=check, enabled=check.enabled, action="notice",
            title=check.title, message=check.message, patch=_default_patch(check.env),
            patch_azure=_default_patch(check.env_azure), patch_aws=_default_patch(check.env_aws),
            email_subject="", auto_email=False, email_repeat_days=0, customized=False, updated_by="",
        )
    return Rule(
        check=check, enabled=bool(row.enabled), action=row.action if row.action in ACTIONS else "notice",
        title=row.title or check.title, message=row.message or "",
        patch=row.patch or "", patch_azure=row.patch_azure or "", patch_aws=row.patch_aws or "",
        email_subject=row.email_subject or "", auto_email=bool(row.auto_email),
        email_repeat_days=int(row.email_repeat_days or 0), customized=True, updated_by=row.updated_by or "",
    )


def rules(session: Session) -> list[Rule]:
    """Every check that is not retired, in the order of the code, as configured."""
    rows = {row.rule_id: row for row in session.exec(select(ClientRuleSetting)).all()}
    return [_rule(check, rows.get(check.id)) for check in client_checks.ACTIVE]


def get_rule(session: Session, rule_id: str) -> Rule | None:
    check = client_checks.BY_ID.get(rule_id)
    if check is None or check.retired:
        return None
    return _rule(check, session.get(ClientRuleSetting, rule_id))


# ── Snapshot of the enabled checks (per worker) ──

_snapshot: tuple[Rule, ...] = tuple(_rule(check, None) for check in client_checks.ACTIVE if check.enabled)


_lock = threading.Lock()


_last: dict[tuple, float] = {}


_reminded: dict[tuple[str, str], str] = {}


def refresh(bind=None) -> bool:
    """Reload this worker's snapshot. Blocking; never raises. On failure the
    previous snapshot stays in force."""
    global _snapshot
    try:
        with Session(bind or engine) as session:
            _snapshot = tuple(rule for rule in rules(session) if rule.enabled)
        return True
    except Exception as exc:
        logger.warning("Client rules refresh failed | error={}: {}", type(exc).__name__, exc)
        return False


def reset() -> None:
    """Forget the snapshot and the write throttles (tests)."""
    global _snapshot
    _snapshot = ()
    with _lock:
        _last.clear()
        _reminded.clear()


async def refresh_loop(interval: float = REFRESH_S) -> None:
    """Background task, one per worker (started from the lifespan)."""
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(refresh)


def _due(key: tuple, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with _lock:
        last = _last.get(key)
        if last is not None and now - last < RESOLUTION_S:
            return False
        if len(_last) > 50_000:  # a bound, not a cache policy
            _last.clear()
        _last[key] = now
        return True


# ── Request path ──

def _touch_version(session: Session, username: str, version: str, now: datetime) -> None:
    where = (ClientVersion.username == username, ClientVersion.client == CLIENT, ClientVersion.version == version)
    if session.execute(update(ClientVersion).where(*where).values(last_seen_at=now)).rowcount:
        session.commit()
        return
    session.add(ClientVersion(username=username, client=CLIENT, version=version, first_seen_at=now, last_seen_at=now))
    try:
        session.commit()
    except IntegrityError:  # another worker added it first
        session.rollback()


def _touch_violation(session: Session, rule_id: str, username: str, version: str, now: datetime) -> None:
    where = (ClientViolation.rule_id == rule_id, ClientViolation.username == username)
    values: dict[str, Any] = {"last_seen_at": now}
    if version:
        values["client_version"] = version
    # Back after dropping out of the window (the account had stopped, or
    # stopped using Claude Code): a new case. What was mailed, asked for or
    # reported fixed belongs to the old one, so the account is told again.
    fresh = {"mailed_at": None, "mail_attempted_at": None, "mail_error": "", "mail_requested_at": None, "resolved_at": None}
    gone_since = now - timedelta(days=WINDOW_DAYS)
    if session.execute(
        update(ClientViolation).where(*where, ClientViolation.last_seen_at < gone_since).values(**values, **fresh)
    ).rowcount:
        session.commit()
        return
    # Back after the account said it applied the fix: open again, and a new
    # case for mail (the mail it got was about the one it reported fixed).
    # ``resolved_at`` stays, so the admin's list can say it was reported fixed.
    reopened = {key: value for key, value in fresh.items() if key != "resolved_at"}
    if session.execute(
        update(ClientViolation)
        .where(*where, ClientViolation.resolved_at.is_not(None),  # type: ignore[union-attr]
               ClientViolation.resolved_at >= ClientViolation.last_seen_at, ClientViolation.resolved_at < now)
        .values(**values, **reopened)
    ).rowcount:
        session.commit()
        return
    if session.execute(update(ClientViolation).where(*where).values(**values)).rowcount:
        session.commit()
        return
    session.add(ClientViolation(
        rule_id=rule_id, username=username, client_version=version, first_seen_at=now, last_seen_at=now,
    ))
    try:
        session.commit()
    except IntegrityError:  # another worker added it first
        session.rollback()


def _record(username: str, version: str, record_version: bool, rule_ids: list[str]) -> None:
    now = utcnow()
    with Session(engine) as session:
        if record_version and version:
            _touch_version(session, username, version, now)
        for rule_id in rule_ids:
            _touch_violation(session, rule_id, username, version, now)


def _claim_reminder(rule_id: str, username: str, version: str, today: str) -> bool:
    """True for exactly one request per check, account and local day, across
    workers: the conditional UPDATE is the claim."""
    with Session(engine) as session:
        _touch_violation(session, rule_id, username, version, utcnow())
        claimed = session.execute(
            update(ClientViolation)
            .where(ClientViolation.rule_id == rule_id, ClientViolation.username == username,
                   ClientViolation.reminded_on != today)
            .values(reminded_on=today)
        ).rowcount == 1
        session.commit()
        return claimed


def _reminder_response(rule: Rule) -> JSONResponse:
    public_url = mailer.settings()["public_url"]
    where = f" Details and the fix: {public_url}/dashboard ." if public_url else " Details and the fix are on your gateway dashboard."
    text = (
        f"{APP_TITLE} notice [{rule.id}]: {rule.title}. {rule.message}".rstrip()
        + where
        + " This notice appears once a day: send your message again to continue."
    )
    return JSONResponse(
        status_code=400,
        content={"type": "error", "error": {"type": "invalid_request_error", "message": text}},
        headers={"X-Gateway-Reminder": rule.id},
    )


def _violated(rule: Rule, facts: dict[str, Any]) -> bool:
    try:
        return bool(rule.check.detect(Facts(**facts)))
    except Exception as exc:  # a broken check finds nothing; it never breaks a request
        if _due(("err", rule.id)):
            logger.warning("Client check {} failed | error={}: {}", rule.id, type(exc).__name__, exc)
        return False


async def _observe(request: Any, user: User) -> JSONResponse | None:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    username = user.username
    version = parse_version(request.headers.get("user-agent"))
    model = body.get("model")
    azure, aws = audience_of(user)
    facts = {
        "body": body, "model": model if isinstance(model, str) else "", "version": version,
        # The scheme the client used: uvicorn's proxy-headers handling sets
        # it from nginx's X-Forwarded-Proto (trusted peers only), like the
        # client address. Never read the header here: the client can send
        # its own.
        "scheme": request.url.scheme, "azure_access": azure, "aws_access": aws,
    }
    violated = [rule for rule in _snapshot if _violated(rule, facts)]

    record_version = bool(version) and _due(("v", username, version))
    rule_ids = [rule.id for rule in violated if _due(("h", rule.id, username))]
    if record_version or rule_ids:
        await run_in_threadpool(_record, username, version, record_version, rule_ids)

    if getattr(user, "is_service_account", False):
        return None  # a program: nobody reads a reminder
    today = local_today().isoformat()
    for rule in violated:
        if rule.action != "remind" or rule.deprecated:
            continue
        key = (rule.id, username)
        with _lock:
            if _reminded.get(key) == today:
                continue
            if len(_reminded) > 50_000:
                _reminded.clear()
            _reminded[key] = today
        if await run_in_threadpool(_claim_reminder, rule.id, username, version, today):
            logger.info("Client rule reminder | rule={} user={}", rule.id, username)
            return _reminder_response(rule)
    return None


async def observe_request(request: Any, user: User) -> JSONResponse | None:
    """Called at the top of every Messages route. Records the client version
    and violations; returns a response only for a due reminder. Never raises."""
    try:
        return await _observe(request, user)
    except Exception as exc:
        logger.warning(
            "Client rules check failed | user={} error={}: {}",
            getattr(user, "username", "-"), type(exc).__name__, exc,
        )
        return None


# ── Admin: settings of a check ──

def clean_fields(form: dict[str, Any], check: Check | None = None) -> dict[str, Any]:
    """Validate an admin's form for a check. Returns column values; raises
    ValueError. With ``check`` (a save, as opposed to a test mail of the
    form as it stands), a check whose fix is a setting cannot be turned on
    while its patch for everyone is empty."""
    title = str(form.get("title") or "").strip()
    if not title or len(title) > 200:
        raise ValueError("Give the notice a title (200 characters at most).")
    message = str(form.get("message") or "").strip()
    if len(message) > 4000:
        raise ValueError("The message is too long (4000 characters at most).")
    action = str(form.get("action") or "notice").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(ACTIONS)}.")
    try:
        repeat = int(str(form.get("email_repeat_days") or "0").strip() or 0)
    except ValueError:
        raise ValueError("Repeat days must be a whole number.")
    if not 0 <= repeat <= 365:
        raise ValueError("Repeat days must be between 0 and 365.")
    patch = dump_env(parse_env(str(form.get("patch") or "")))
    if check is not None and check.needs_patch and form.get("enabled") and not patch:
        raise ValueError(
            f"{check.id} tells people to change a setting, so it needs a settings patch: "
            "fill in the Everyone box (NAME=value) before turning it on."
        )
    return {
        "enabled": bool(form.get("enabled")), "action": action, "title": title, "message": message,
        "patch": patch,
        "patch_azure": dump_env(parse_env(str(form.get("patch_azure") or ""))),
        "patch_aws": dump_env(parse_env(str(form.get("patch_aws") or ""))),
        "email_subject": str(form.get("email_subject") or "").strip()[:200],
        "auto_email": bool(form.get("auto_email")), "email_repeat_days": repeat,
    }


def save_settings(session: Session, check: Check, values: dict[str, Any], updated_by: str = "") -> Rule:
    row = session.get(ClientRuleSetting, check.id)
    if row is None:
        row = ClientRuleSetting(rule_id=check.id, updated_at=utcnow())
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_by = updated_by
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _rule(check, row)


def reset_settings(session: Session, rule_id: str) -> None:
    """Back to the check's defaults in code. Its violations are kept."""
    row = session.get(ClientRuleSetting, rule_id)
    if row is not None:
        session.delete(row)
        session.commit()


# ── Violations ──

def _active_since(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=WINDOW_DAYS)


def violations(session: Session, rule_id: str, now: datetime | None = None) -> list[ClientViolation]:
    """A check's accounts inside the window, newest first."""
    return list(session.exec(
        select(ClientViolation)
        .where(ClientViolation.rule_id == rule_id, ClientViolation.last_seen_at >= _active_since(now))
        .order_by(ClientViolation.last_seen_at.desc())  # type: ignore[attr-defined]
    ).all())


def history(session: Session, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Per violation id, over all time: how many accounts ever had one, when
    the last one was seen and how many days ago. For judging when a check
    can be deprecated or retired; includes ids of retired checks."""
    now = now or utcnow()
    rows = session.exec(
        select(ClientViolation.rule_id, func.count(), func.max(ClientViolation.last_seen_at))
        .group_by(ClientViolation.rule_id)
    ).all()
    return {
        rule_id: {"accounts": int(count), "last_seen_at": last, "quiet_days": max((now - last).days, 0)}
        for rule_id, count, last in rows if last is not None
    }


def is_stale(rule: Rule, seen: dict[str, Any] | None) -> bool:
    """An enabled check nobody has tripped for ``STALE_DAYS``: a candidate
    for retirement. One that never had a violation says nothing either way."""
    return bool(rule.enabled and seen and seen["quiet_days"] >= STALE_DAYS)


def is_resolved(violation: ClientViolation) -> bool:
    """The account reported the fix and has not violated since."""
    return violation.resolved_at is not None and violation.resolved_at >= violation.last_seen_at


def resolve(session: Session, rule_id: str, username: str, now: datetime | None = None) -> bool:
    """Record that ``username`` says it applied the fix for ``rule_id``.
    False when it has no such violation. The next violating request reopens
    it (within the hour: ``last_seen_at`` is written at most hourly per worker)."""
    row = session.exec(
        select(ClientViolation).where(ClientViolation.rule_id == rule_id, ClientViolation.username == username)
    ).first()
    if row is None:
        return False
    # Never earlier than the last violation, so one recorded a moment ago
    # by another worker does not reopen it straight away.
    row.resolved_at = max(now or utcnow(), row.last_seen_at)
    session.add(row)
    session.commit()
    return True


def notices_for(session: Session, user: User, now: datetime | None = None) -> list[dict[str, Any]]:
    """What ``user``'s dashboard shows: the enabled checks it tripped inside
    the window and has not reported as fixed, each with the patch meant for
    this account."""
    rows = {
        row.rule_id: row for row in session.exec(
            select(ClientViolation)
            .where(ClientViolation.username == user.username, ClientViolation.last_seen_at >= _active_since(now))
        ).all() if not is_resolved(row)
    }
    if not rows:
        return []
    azure, aws = audience_of(user)
    found = []
    for rule in rules(session):
        if not rule.enabled or rule.id not in rows:
            continue
        env = effective_env(rule, azure, aws)
        found.append({
            "rule_id": rule.id, "title": rule.title, "message": rule.message,
            # ``patch`` is the text a person reads; ``env`` the same patch as
            # data (``GET /v1/notices``).
            "patch": env_snippet(env), "env": env,
            "seen_at": rows[rule.id].last_seen_at, "client_version": rows[rule.id].client_version,
        })
    return found


def audience_flags(audience: str) -> tuple[bool, bool]:
    """``(azure, aws)`` for an admin's preview of one audience: ``local``,
    ``azure`` (Azure only) or ``aws`` (AWS only)."""
    if audience not in AUDIENCES:
        raise ValueError(f"Audience must be one of: {', '.join(AUDIENCES)}.")
    return audience == "azure", audience == "aws"


def version_overview(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Each account's newest Claude Code version seen in the last
    ``VERSION_WINDOW_DAYS`` days, grouped by version (newest version first)."""
    now = now or utcnow()
    rows = session.exec(
        select(ClientVersion)
        .where(ClientVersion.client == CLIENT, ClientVersion.last_seen_at >= now - timedelta(days=VERSION_WINDOW_DAYS))
    ).all()
    latest: dict[str, ClientVersion] = {}
    for row in rows:
        best = latest.get(row.username)
        if best is None or (row.last_seen_at, version_key(row.version)) > (best.last_seen_at, version_key(best.version)):
            latest[row.username] = row
    groups: dict[str, list[str]] = {}
    for username, row in latest.items():
        groups.setdefault(row.version, []).append(username)
    versions = [
        {"version": version, "usernames": sorted(names), "count": len(names)}
        for version, names in sorted(groups.items(), key=lambda kv: version_key(kv[0]), reverse=True)
    ]
    return {"versions": versions, "accounts": len(latest), "days": VERSION_WINDOW_DAYS}


# ── Everything an account has open ──

def open_items(
    session: Session, now: datetime | None = None, usernames: list[str] | None = None,
) -> dict[str, list[tuple[Rule, ClientViolation]]]:
    """Per account, the enabled checks it has an open violation of (inside
    the window, not reported as fixed), in the order of the code. This is
    "what this person still has to change": the dashboard's combined patch
    and the mail are built from it, so one account is told once, about
    everything."""
    enabled = {rule.id: (index, rule) for index, rule in enumerate(rules(session)) if rule.enabled}
    query = select(ClientViolation).where(ClientViolation.last_seen_at >= _active_since(now))
    if usernames is not None:
        if not usernames:
            return {}
        query = query.where(ClientViolation.username.in_(usernames))  # type: ignore[attr-defined]
    grouped: dict[str, list[tuple[int, Rule, ClientViolation]]] = {}
    for row in session.exec(query).all():
        if row.rule_id in enabled and not is_resolved(row):
            index, rule = enabled[row.rule_id]
            grouped.setdefault(row.username, []).append((index, rule, row))
    return {
        username: [(rule, row) for _, rule, row in sorted(entries, key=lambda e: e[0])]
        for username, entries in grouped.items()
    }


def combined_env(rule_list: list[Any], azure: bool, aws: bool) -> dict[str, str | None]:
    """One patch for several checks: each check's patch for this account, in
    the order given (the code's). Where two set the same variable, the later
    check wins; ``env_conflicts`` tells the admin when that happens."""
    env: dict[str, str | None] = {}
    for rule in rule_list:
        env.update(effective_env(rule, azure, aws))
    return env


def env_conflicts(rule_list: list[Rule]) -> list[dict[str, Any]]:
    """Variables that enabled checks set to different values for the same
    kind of account, with the check that wins in a combined patch."""
    found: dict[tuple, dict[str, Any]] = {}
    for audience in AUDIENCES:
        azure, aws = audience_flags(audience)
        setters: dict[str, list[tuple[str, str | None]]] = {}
        for rule in rule_list:
            if rule.enabled:
                for name, value in effective_env(rule, azure, aws).items():
                    setters.setdefault(name, []).append((rule.id, value))
        for name, entries in setters.items():
            if len({value for _, value in entries}) > 1:
                key = (name, tuple(entries))
                found.setdefault(key, {"name": name, "rules": entries, "winner": entries[-1][0], "audiences": []})
                found[key]["audiences"].append(audience)
    return list(found.values())


def split_covered(rule_list: list[Any], azure: bool, aws: bool) -> tuple[list[str], list[str]]:
    """``(covered, manual)``: the ids of the checks whose fix is in this
    account's patch, and of those that need something a script cannot do
    (update Claude Code, ask for access), each in the order given."""
    covered: list[str] = []
    manual: list[str] = []
    for rule in rule_list:
        (covered if effective_env(rule, azure, aws) else manual).append(rule.id)
    return covered, manual


def combined_for(session: Session, user: User, now: datetime | None = None) -> dict[str, Any] | None:
    """The account's one patch: everything it has open, merged
    (``combined_env``), whether that is one check or several. ``covered`` /
    ``manual`` say which checks the patch fixes and which it cannot
    (``split_covered``). None when nothing is open."""
    items = open_items(session, now, [user.username]).get(user.username, [])
    if not items:
        return None
    rule_list = [rule for rule, _ in items]
    azure, aws = audience_of(user)
    env = combined_env(rule_list, azure, aws)
    covered, manual = split_covered(rule_list, azure, aws)
    return {"count": len(items), "env": env, "patch": env_snippet(env), "covered": covered, "manual": manual}


def resolve_all(session: Session, username: str, now: datetime | None = None) -> int:
    """"I've applied everything": resolve every open item of the account.
    Returns how many."""
    now = now or utcnow()
    items = open_items(session, now, [username]).get(username, [])
    for _, row in items:
        row.resolved_at = max(now, row.last_seen_at)
        session.add(row)
    session.commit()
    return len(items)
