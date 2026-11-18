"""Peak overflow: while an on-prem server is congested, serve some accounts'
requests for its models from the cloud.

The unit is the **account**, never the request. A conversation that moved
between an on-prem model and a cloud model turn by turn would pay full price
on the cloud every turn (no prompt-cache hit) and a full prefill on-prem
every time it came back. So an account is moved as a whole, stays for a
while, and is moved back when it has gone quiet.

How it runs:

    health loop (every worker)   vLLM /metrics -> waiting queue per server
    overflow job (one worker a round, ``JOB``)
        reads the queue, counts readings in a row above / below the two
        thresholds (``overflow_servers``), moves accounts onto the cloud
        (``overflow_stays``) or back, sums today's overflow spend
        (``overflow_budget``)
    request path (``target_for``)
        reads this worker's snapshot only, never the database

A stay is account x server: an account moved because server A is congested
keeps using server B on-prem. Only models that name a cloud model in
``overflow_to`` (Admin -> Models) ever overflow, and only for accounts with
``users.can_overflow_cloud`` (true unless an admin turned it off). Service
accounts are never moved.

Everything fails toward on-prem: a stay is a lease the job renews each
round, so a job that stops (or a server whose queue cannot be read) lets the
stays lapse; a broken snapshot or setting moves nobody; the daily budget
defaults to $0. The request path never raises.

The overflowed request is an ordinary cloud request for billing: the cloud
model's price, on the account's daily limit, ``usage_logs.overflow`` saying
why it went there. It skips the guardrail, which screens local traffic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlmodel import Session, func, select

from app.core import config
from app.core.config import get_overflow_settings
from app.core.database import engine
from app.core.logger import logger
from app.core.server_state import get_metrics
from app.core.timeutil import local_day_start_utc, local_today
from app.models.schema import (
    OverflowBudget,
    OverflowServer,
    OverflowStay,
    ScheduledJob,
    UsageLog,
    User,
    service_account_clause,
)
from app.services.scheduler import Job, env_seconds

JOB_NAME = "cloud_overflow"
DEFAULT_TICK_S = 30.0
# How often a worker reloads who is on the cloud.
REFRESH_S = 15.0
# A stay the job has not renewed for this long no longer counts: with the
# job stopped, everyone is back on-prem within this time.
LEASE = timedelta(minutes=10)
# Who is using a congested server: accounts with a finished request on it
# in the last few minutes, busiest first.
RECENT = timedelta(minutes=5)
# The job's reads of usage_logs are small (minutes of rows, or the moved
# accounts' rows of today); bounded anyway, as every job on the shared pool.
_STATEMENT_TIMEOUT_MS = 10_000
# The model types the chat / responses / messages endpoints serve.
_TYPES = ("llm", "vlm")
_ADMIN_HISTORY = 20


def utcnow() -> datetime:
    """Naive UTC, like every timestamp column."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def settings() -> dict[str, Any]:
    """The admin's settings (``config.get_overflow_settings``). Every reader
    goes through here, so one patch keeps overflow off in the tests."""
    return get_overflow_settings()


def tick_seconds() -> float:
    """Seconds between rounds of the job (``OVERFLOW_TICK_S``, default 30:
    the health loop's own pace; ``0`` turns the job off)."""
    return env_seconds("OVERFLOW_TICK_S", DEFAULT_TICK_S)


# ── Which servers can overflow, and to what ──

def cloud_backend(alias: str | None) -> str | None:
    """``"azure"`` / ``"bedrock"`` when ``alias`` is a configured cloud
    model the chat endpoints can serve, else None."""
    if not alias or not isinstance(alias, str):
        return None
    for backend, models in (
        ("azure", config.get_azure_models_snapshot()),
        ("bedrock", config.get_bedrock_models_snapshot()),
    ):
        entry = models.get(alias)
        if entry is not None and entry.get("type", "llm") in _TYPES:
            return backend
    return None


@dataclass
class _Server:
    base_url: str
    aliases: list[str] = field(default_factory=list)
    high: int = 0
    low: int = 0


def servers(s: dict[str, Any] | None = None) -> dict[str, _Server]:
    """The on-prem servers that can overflow: those with at least one chat
    model whose ``overflow_to`` names a configured cloud model. A server's
    thresholds are the gateway-wide pair unless one of its models sets its
    own (the lowest of them, should two models of one server disagree)."""
    s = s or settings()
    out: dict[str, _Server] = {}
    own: dict[str, dict[str, list[int]]] = {}
    for alias, route in config.get_model_routing_snapshot().items():
        if route.get("type") not in _TYPES or cloud_backend(route.get("overflow_to")) is None:
            continue
        url = route.get("base_url") or ""
        if not url:
            continue
        out.setdefault(url, _Server(base_url=url)).aliases.append(alias)
        for key in ("high", "low"):
            value = route.get(f"overflow_waiting_{key}")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                own.setdefault(url, {}).setdefault(key, []).append(value)
    for url, server in out.items():
        high = min(own.get(url, {}).get("high") or [s["waiting_high"]])
        low = min(own.get(url, {}).get("low") or [s["waiting_low"]])
        if high < 1 or low >= high:  # a server's own pair that makes no sense
            high, low = s["waiting_high"], s["waiting_low"]
        server.high, server.low = high, low
    return out


# ── The per-worker snapshot the request path reads ──

@dataclass(frozen=True)
class _Snapshot:
    # (username, base_url) -> when the stay lapses unless renewed
    stays: dict[tuple[str, str], datetime] = field(default_factory=dict)
    spent_usd: float = 0.0
    day: Any = None


_snapshot = _Snapshot()


def _load(session: Session, now: datetime) -> _Snapshot:
    rows = session.exec(
        select(OverflowStay.username, OverflowStay.base_url, OverflowStay.expires_at)
        .where(OverflowStay.released_at.is_(None))  # type: ignore[union-attr]
        .where(OverflowStay.mode == "enforce")
        .where(OverflowStay.expires_at > now)
    ).all()
    today = local_today()
    budget = session.get(OverflowBudget, today)
    return _Snapshot(
        stays={(str(name), str(url)): expires for name, url, expires in rows},
        spent_usd=float(budget.spent_usd) if budget is not None else 0.0,
        day=today,
    )


def refresh(bind=None) -> bool:
    """Reload this worker's snapshot. Blocking; never raises. While overflow
    is not enforcing, nobody is on the cloud and the database is not read.
    On failure the previous snapshot stays (its stays lapse on their own)."""
    global _snapshot
    try:
        if settings()["mode"] != "enforce":
            _snapshot = _Snapshot()
            return True
        with Session(bind or engine) as session:
            _snapshot = _load(session, utcnow())
        return True
    except Exception as exc:
        logger.warning("Overflow refresh failed | error={}: {}", type(exc).__name__, exc)
        return False


def reset() -> None:
    """Forget the snapshot (tests)."""
    global _snapshot
    _snapshot = _Snapshot()


async def refresh_loop(interval: float = REFRESH_S) -> None:
    """Background task, one per worker (started from the lifespan)."""
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(refresh)


def budget_state(s: dict[str, Any], spent: float) -> str:
    """``"open"``, ``"closing"`` (past the stop share: nobody new is moved,
    those on the cloud stay until they are released) or ``"spent"`` (the
    budget is gone: nothing overflows). A $0 budget is spent."""
    budget = float(s["daily_budget_usd"])
    if spent >= budget:
        return "spent"
    if spent >= budget * s["budget_stop_pct"] / 100.0:
        return "closing"
    return "open"


def target_for(user: User, route: dict[str, Any], now: datetime | None = None) -> str | None:
    """The cloud alias a request of ``user`` for the on-prem model behind
    ``route`` goes to instead, or None: the request-path check. Reads the
    snapshot only; never raises (anything unexpected means on-prem).

    The account must be on the cloud for this server right now (an unlapsed
    stay), allowed to overflow, not a service account, the model must name
    an ``overflow_to``, overflow must be enforcing and today's budget not
    spent. The caller still checks that the alias is a cloud model its
    endpoint can dispatch to.
    """
    try:
        target = route.get("overflow_to")
        if not target or not _snapshot.stays:
            return None
        if user.is_service_account or not getattr(user, "can_overflow_cloud", True):
            return None
        expires = _snapshot.stays.get((user.username, route.get("base_url") or ""))
        if expires is None or expires <= (now or utcnow()):
            return None
        s = settings()
        if s["mode"] != "enforce":
            return None
        spent = _snapshot.spent_usd if _snapshot.day == local_today() else 0.0
        if budget_state(s, spent) == "spent":
            return None
        return str(target)
    except Exception as exc:
        logger.warning("Overflow check failed (served on-prem) | error={}: {}", type(exc).__name__, exc)
        return None


# ── The job ──

def how_many(waiting: int, high: int, grab_max: int) -> int:
    """Accounts to move this round: one just over the threshold, rising to
    ``grab_max`` at twice the threshold."""
    if waiting < high:
        return 0
    return 1 + (grab_max - 1) * min(waiting - high, high) // max(high, 1)


def _bound(session: Session) -> None:
    """Bound the current transaction's statements (PostgreSQL)."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))


def _open_stays(session: Session) -> list[OverflowStay]:
    return list(session.exec(
        select(OverflowStay)
        .where(OverflowStay.released_at.is_(None))  # type: ignore[union-attr]
        .order_by(OverflowStay.entered_at, OverflowStay.id)  # type: ignore[arg-type]
    ).all())


def _release(session: Session, stay: OverflowStay, reason: str, now: datetime) -> None:
    stay.released_at = now
    stay.reason_out = reason[:200]
    session.add(stay)
    logger.info(
        "Overflow: {} back on-prem | server={} reason={} mode={}",
        stay.username, stay.base_url, reason, stay.mode,
    )


def release_all(session: Session, reason: str, now: datetime | None = None) -> int:
    """Move everyone back (overflow switched off). Commits."""
    now = now or utcnow()
    stays = _open_stays(session)
    for stay in stays:
        _release(session, stay, reason, now)
    session.commit()
    return len(stays)


def release_account(session: Session, username: str, reason: str, now: datetime | None = None) -> int:
    """Move one account back at once (an admin turned its overflow off).
    Commits."""
    now = now or utcnow()
    stays = [s for s in _open_stays(session) if s.username == username]
    for stay in stays:
        _release(session, stay, reason, now)
    session.commit()
    return len(stays)


def spent_today(session: Session) -> Decimal:
    """Today's (Asia/Taipei) overflow spend: the day's ``usage_logs`` rows
    marked ``overflow``, of the accounts that were on the cloud today (only
    they can have such rows, and their rows are an index range each)."""
    day_start = local_day_start_utc()
    names = set(session.exec(
        select(OverflowStay.username)
        .where(OverflowStay.mode == "enforce")
        .where(sa.or_(
            OverflowStay.released_at.is_(None),  # type: ignore[union-attr]
            OverflowStay.released_at >= day_start.replace(tzinfo=None),  # type: ignore[operator]
        ))
    ).all())
    if not names:
        return Decimal("0")
    ids = list(session.exec(select(User.id).where(User.username.in_(names))).all())  # type: ignore[attr-defined]
    if not ids:
        return Decimal("0")
    total = session.exec(
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.user_id.in_(ids))  # type: ignore[attr-defined]
        .where(UsageLog.created_at >= day_start)
        .where(UsageLog.overflow.is_(True))  # type: ignore[attr-defined]
    ).one()
    return Decimal(str(total or 0))


def _record_budget(session: Session, spent: Decimal, now: datetime) -> None:
    today = local_today()
    row = session.get(OverflowBudget, today)
    if row is None:
        row = OverflowBudget(day=today)
    row.spent_usd = spent
    row.updated_at = now
    session.add(row)


def _read_queue(session: Session, server: _Server, now: datetime) -> OverflowServer:
    """Store this round's reading of one server and move its two streaks.
    An unreadable queue (``/metrics`` unreachable, server down) ends both."""
    row = session.get(OverflowServer, server.base_url)
    if row is None:
        row = OverflowServer(base_url=server.base_url)
    metrics = get_metrics(server.base_url)
    if metrics is None:
        row.waiting, row.running = None, None
        row.over_streak = row.under_streak = 0
    else:
        row.waiting = int(metrics.get("waiting", 0))
        row.running = int(metrics.get("running", 0))
        if row.waiting >= server.high:
            row.over_streak, row.under_streak = row.over_streak + 1, 0
        elif row.waiting <= server.low:
            row.over_streak, row.under_streak = 0, row.under_streak + 1
        else:
            row.over_streak = row.under_streak = 0
    row.updated_at = now
    session.add(row)
    return row


def _eligible(session: Session, usernames: set[str]) -> set[str]:
    """Those of ``usernames`` that may be on the cloud: an existing person,
    not disabled, with overflow allowed."""
    if not usernames:
        return set()
    return set(session.exec(
        select(User.username)
        .where(User.username.in_(usernames))  # type: ignore[attr-defined]
        .where(~service_account_clause())
        .where(User.is_disabled == False)  # noqa: E712
        .where(User.can_overflow_cloud == True)  # noqa: E712
    ).all())


def _usage_time(value: datetime) -> datetime:
    """A naive UTC instant as ``usage_logs.created_at`` is compared
    everywhere else: timezone-aware UTC, the way its rows are written."""
    return value.replace(tzinfo=timezone.utc)


def _recently_active(session: Session, usernames: set[str], since: datetime) -> set[str]:
    """Those of ``usernames`` with a finished request since ``since``."""
    if not usernames:
        return set()
    since = _usage_time(since)
    return set(session.exec(
        select(User.username)
        .where(User.username.in_(usernames))  # type: ignore[attr-defined]
        .where(
            select(UsageLog.id)
            .where(UsageLog.user_id == User.id)
            .where(UsageLog.created_at >= since)
            .exists()
        )
    ).all())


def _candidates(session: Session, server: _Server, since: datetime, skip: set[str], limit: int) -> list[str]:
    """The accounts to move for a congested server: people allowed to
    overflow who used its models since ``since``, most tokens first."""
    if limit <= 0:
        return []
    since = _usage_time(since)
    tokens = func.sum(UsageLog.input_tokens + UsageLog.output_tokens)
    stmt = (
        select(User.username)
        .join(UsageLog, UsageLog.user_id == User.id)  # type: ignore[arg-type]
        .where(UsageLog.created_at >= since)
        .where(UsageLog.backend == "vllm")
        .where(UsageLog.model.in_(server.aliases))  # type: ignore[attr-defined]
        .where(~service_account_clause())
        .where(User.is_disabled == False)  # noqa: E712
        .where(User.can_overflow_cloud == True)  # noqa: E712
        .group_by(User.username)
        .order_by(tokens.desc(), User.username)
    )
    if skip:
        stmt = stmt.where(User.username.not_in(skip))  # type: ignore[attr-defined]
    return [str(name) for name in session.exec(stmt.limit(limit)).all()]


def tick(session: Session, now: datetime | None = None) -> str:
    """One round of the job: read the queues, move accounts onto the cloud
    or back, record today's spend. Returns a one-line summary. Commits, then
    reloads this worker's snapshot (the others follow within ``REFRESH_S``).

    Per server, in this order:
      release  the queue has been at or below the lower threshold for
               ``consecutive`` readings: move ``release_per_round`` accounts
               back, among those past their minimum stay, idle ones first
               (no finished request for ``idle_min``), then longest on the
               cloud first;
      grab     at or above the upper threshold for ``consecutive`` readings
               and the budget open: move up to ``grab_max`` accounts
               (``how_many``), the busiest on that server first.
    Between the thresholds nothing moves.
    """
    now = now or utcnow()
    s = settings()
    mode = s["mode"]
    open_stays = _open_stays(session)

    if mode == "off":
        if open_stays:
            release_all(session, "overflow switched off", now)
            refresh(session.bind)
        return "off"

    _bound(session)
    known = servers(s)
    readings = {url: _read_queue(session, server, now) for url, server in known.items()}
    # Monitor moves no request, so it spends nothing and has no budget to hit.
    spent, budget = Decimal("0"), "open"
    if mode == "enforce":
        spent = spent_today(session)
        _record_budget(session, spent, now)
        budget = budget_state(s, float(spent))

    released = grabbed = 0
    allowed = _eligible(session, {stay.username for stay in open_stays})
    staying: list[OverflowStay] = []
    for stay in open_stays:
        reason = None
        if stay.mode != mode:
            reason = f"mode changed to {mode}"
        elif stay.expires_at <= now:
            reason = "lapsed (not renewed in time)"
        elif stay.base_url not in known:
            reason = "server no longer overflows"
        elif stay.username not in allowed:
            reason = "account may not overflow"
        elif budget == "spent":
            reason = "daily budget spent"
        if reason:
            _release(session, stay, reason, now)
            released += 1
        else:
            staying.append(stay)

    min_stay = timedelta(minutes=s["min_stay_min"])
    busy = _recently_active(
        session, {stay.username for stay in staying}, now - timedelta(minutes=s["idle_min"]),
    )
    for url, server in known.items():
        reading = readings[url]
        here = [stay for stay in staying if stay.base_url == url]
        if reading.waiting is None:
            continue  # unreadable: not renewed, so these lapse on their own
        for stay in here:
            stay.expires_at = now + LEASE
            session.add(stay)
        if reading.under_streak >= s["consecutive"]:
            due = [stay for stay in here if stay.entered_at <= now - min_stay]
            due.sort(key=lambda stay: (stay.username in busy, stay.entered_at, stay.id or 0))
            for stay in due[: s["release_per_round"]]:
                idle = "idle" if stay.username not in busy else "longest on the cloud"
                _release(session, stay, f"queue {reading.waiting} <= {server.low}; {idle}", now)
                released += 1
        elif reading.over_streak >= s["consecutive"] and budget == "open":
            count = how_many(reading.waiting, server.high, s["grab_max"])
            names = _candidates(session, server, now - RECENT, {stay.username for stay in here}, count)
            for name in names:
                session.add(OverflowStay(
                    username=name, base_url=url, mode=mode, entered_at=now, expires_at=now + LEASE,
                    reason_in=f"queue {reading.waiting} >= {server.high}",
                ))
                grabbed += 1
                logger.info(
                    "Overflow: {} onto the cloud | server={} waiting={} threshold={} mode={}",
                    name, url, reading.waiting, server.high, mode,
                )
    session.commit()
    refresh(session.bind)

    on_cloud = len(staying) - sum(1 for stay in staying if stay.released_at is not None) + grabbed
    return (
        f"mode={mode} servers={len(known)} on_cloud={on_cloud} grabbed={grabbed} released={released} "
        f"spent=${float(spent):.2f}/{s['daily_budget_usd']:g} budget={budget}"
    )


# Quiet: a round every 30 s is not worth a log line; each move has its own.
JOB = Job(name=JOB_NAME, interval=tick_seconds, run=tick, quiet=True)


# ── The admin card ──

def _aliases_by_server() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for alias, route in config.get_model_routing_snapshot().items():
        out.setdefault(route.get("base_url") or "", []).append(alias)
    return out


def status(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """What the admin's *Cloud Overflow* card shows: the settings, today's
    spend against the budget, each overflowing server's queue against its
    thresholds, who is on the cloud now (with what it cost today), the last
    few stays that ended, and the job's last round."""
    now = now or utcnow()
    s = settings()
    known = servers(s)
    names = _aliases_by_server()
    rows = {row.base_url: row for row in session.exec(select(OverflowServer)).all()}

    budget_row = session.get(OverflowBudget, local_today())
    spent = float(budget_row.spent_usd) if budget_row is not None else 0.0
    budget = float(s["daily_budget_usd"])

    open_stays = _open_stays(session)
    cost_by_name: dict[str, float] = {}
    if open_stays:
        day_start = local_day_start_utc()
        for name, cost in session.exec(
            select(User.username, func.coalesce(func.sum(UsageLog.cost_usd), 0))
            .join(UsageLog, UsageLog.user_id == User.id)  # type: ignore[arg-type]
            .where(User.username.in_({stay.username for stay in open_stays}))  # type: ignore[attr-defined]
            .where(UsageLog.created_at >= day_start)
            .where(UsageLog.overflow.is_(True))  # type: ignore[attr-defined]
            .group_by(User.username)
        ).all():
            cost_by_name[str(name)] = float(cost or 0)

    def stay_view(stay: OverflowStay) -> dict[str, Any]:
        return {
            "username": stay.username,
            "models": names.get(stay.base_url, []),
            "mode": stay.mode,
            "entered_at": stay.entered_at,
            "released_at": stay.released_at,
            "minutes": int(((stay.released_at or now) - stay.entered_at).total_seconds() // 60),
            "reason_in": stay.reason_in,
            "reason_out": stay.reason_out,
            "lapsed": stay.released_at is None and stay.expires_at <= now,
            "cost_today": cost_by_name.get(stay.username, 0.0),
        }

    recent = session.exec(
        select(OverflowStay)
        .where(OverflowStay.released_at.is_not(None))  # type: ignore[union-attr]
        .order_by(OverflowStay.released_at.desc(), OverflowStay.id.desc())  # type: ignore[union-attr]
        .limit(_ADMIN_HISTORY)
    ).all()
    job = session.get(ScheduledJob, JOB_NAME)
    routing = config.get_model_routing_snapshot()
    return {
        "settings": s,
        "modes": config.OVERFLOW_MODES,
        "spent_usd": spent,
        "budget_usd": budget,
        "budget_pct": min(100.0, spent / budget * 100.0) if budget > 0 else (100.0 if s["mode"] == "enforce" else 0.0),
        "budget_state": budget_state(s, spent),
        "servers": [
            {
                "models": [
                    {"alias": alias, "to": routing[alias].get("overflow_to")}
                    for alias in server.aliases
                ],
                "high": server.high,
                "low": server.low,
                "waiting": rows[url].waiting if url in rows else None,
                "running": rows[url].running if url in rows else None,
                "over_streak": rows[url].over_streak if url in rows else 0,
                "under_streak": rows[url].under_streak if url in rows else 0,
                "on_cloud": sum(1 for stay in open_stays if stay.base_url == url),
            }
            for url, server in known.items()
        ],
        "on_cloud": [stay_view(stay) for stay in open_stays],
        "recent": [stay_view(stay) for stay in recent],
        "job": {
            "last_started_at": job.last_started_at if job else None,
            "last_status": job.last_status if job else "",
            "last_detail": job.last_detail if job else "",
            "tick_s": tick_seconds(),
        },
    }
