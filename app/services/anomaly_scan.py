"""Usage-anomaly scan, run hourly by the gateway itself (``SCAN_JOB`` via
app/services/scheduler.py; ``scripts/scan_anomalies.py`` runs it by hand).

Reads usage, applies a handful of statistics-based rules, and upserts
findings into anomaly_events. One finding is one row per scope × rule ×
local day: re-running updates it in place and keeps the day's peak, so the
scan is idempotent and an episode that lasts all afternoon is one row.

Detection only — it never blocks, throttles, or disables anyone. Admins
review findings in the admin panel (Anomalies card) and act manually.

An account is compared with its own *busy day*: the 90th percentile of its
days with usage in the prior BUSY_DAY_WINDOW_DAYS. Not the median: usage is
heavy-tailed, and a long working session is several times an ordinary day
without being unusual. Days without usage are not in the sample.

Rules (thresholds are module constants, tune freely):
  cost_spike      spend today > COST_SPIKE_RATIO × the account's busy day
                  (and above COST_SPIKE_FLOOR_USD — tiny accounts don't page
                  anyone). critical needs COST_SPIKE_CRITICAL_RATIO × and
                  COST_SPIKE_CRITICAL_USD: a ratio alone made $0.30 → $3
                  critical. With fewer than COST_SPIKE_MIN_DAYS days of
                  history there is no baseline to trust, so only
                  COST_SPIKE_NEW_ACCOUNT_USD in a day is a finding
  off_hours       human account with more requests between 00:00-06:00 local
                  time today than on its busy day, and more than
                  OFF_HOURS_MIN_REQUESTS. A fixed count cannot do this: an
                  agent left running makes thousands of requests a night,
                  which is ordinary for the people who do it
  burst_rate      human account with > BURST_PER_HOUR requests in the last
                  hour (sustained machine-speed usage on a human key)
  behavior_shift  account suddenly using a model_type it never touched in the
                  prior 30 days, > BEHAVIOR_SHIFT_MIN_REQUESTS times today.
                  Severity info: a first embedding or rerank job looks exactly
                  like this, so it is recorded but not put in front of an admin
  empty_turn_rate model-level: share of llm/vlm responses with <= 1 output
                  token over the last hour exceeds EMPTY_TURN_RATE_ABS and
                  3× the model's 30-day baseline, from at least
                  EMPTY_TURN_MIN_USERS accounts (smoke detector for model /
                  template / downstream regressions; one account's broken
                  client is not the model)

cost_spike and behavior_shift cover service accounts too (a leaked service
key shows as a spend spike); off_hours and burst_rate are people only, since
a program runs at night and at machine speed by design.

An open finding not seen again for OPEN_EXPIRY_DAYS is closed by the scan
(``close_stale``), or the admin's list only ever grows.

Baselines read through ``usage_rollup.aggregate``; the last-hour and today
windows read ``usage_logs``. On PostgreSQL every rule runs under a
``SET LOCAL statement_timeout`` (30 s) so a pathological query can never
linger on the gateway's pool; a rule that fails is logged and rolled back,
and the remaining rules and the persist step still run.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlmodel import Session, func, select

from app.core.logger import logger
from app.core.timeutil import local_day_start_utc, local_today
from app.models.schema import AnomalyEvent, UsageLog, User, service_account_clause
from app.services.scheduler import Job, env_seconds
from app.services.usage_rollup import aggregate

# ── Tunables ──
BUSY_DAY_WINDOW_DAYS = 60       # history an account's busy day is taken from (~40 working days)
BUSY_DAY_PCT = 0.90             # the busy day: this percentile of its days with usage
COST_SPIKE_RATIO = 2.0          # today > N × the account's busy day
COST_SPIKE_FLOOR_USD = 1.0      # ignore spikes below this absolute spend
COST_SPIKE_CRITICAL_RATIO = 4.0  # critical: this many times the busy day ...
COST_SPIKE_CRITICAL_USD = 10.0   # ... and at least this much today
COST_SPIKE_MIN_DAYS = 10        # fewer days with usage: no baseline to trust ...
COST_SPIKE_NEW_ACCOUNT_USD = 20.0  # ... so only this much in a day is a finding
OFF_HOURS_MIN_REQUESTS = 300    # never flag a night below this, whatever the account's history
OFF_HOURS_END = timedelta(hours=6)  # off-hours = local midnight to 06:00
BURST_PER_HOUR = 1200           # sustained ~20 req/min for an hour
BURST_CRITICAL_PER_HOUR = 3000
BEHAVIOR_SHIFT_MIN_REQUESTS = 100
EMPTY_TURN_RATE_ABS = 0.20      # >=20% empty turns in the last hour…
EMPTY_TURN_RATE_RATIO = 3.0     # …and 3× the model's 30d baseline
EMPTY_TURN_MIN_REQUESTS = 20    # need volume before the rate means anything
EMPTY_TURN_MIN_USERS = 2        # …from more than one account: one broken client is not the model
BASELINE_DAYS = 30
OPEN_EXPIRY_DAYS = 7            # an open finding not seen again for this long is closed
RESOLVED_RETENTION_DAYS = 90
STATEMENT_TIMEOUT_MS = 30_000


def _finding(scope: str, rule: str, *, severity: str, window_start: datetime,
             window_end: datetime, observed: float, baseline: float,
             detail: dict, user_id: int | None = None, model: str | None = None) -> dict:
    ratio = (observed / baseline) if baseline > 0 else float(observed > 0)
    return {
        "scope": scope, "rule": rule, "severity": severity,
        "user_id": user_id, "model": model,
        "window_start": window_start, "window_end": window_end,
        "observed": round(float(observed), 6), "baseline": round(float(baseline), 6),
        "ratio": round(float(ratio), 2), "detail": json.dumps(detail, ensure_ascii=False),
    }


def _human_user_ids(session: Session) -> dict[int, str]:
    rows = session.exec(select(User.id, User.username).where(~service_account_clause())).all()
    return {int(uid): str(name) for uid, name in rows}


def _baseline_window() -> tuple:
    """The ``BASELINE_DAYS`` local days before today, as ``aggregate`` bounds."""
    today = local_today()
    return today - timedelta(days=BASELINE_DAYS), today


def _percentile(values: list, pct: float):
    """Nearest-rank percentile; with fewer than ten values that is the largest."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(1, math.ceil(len(ordered) * pct)) - 1]


def _daily_history(session: Session, user_ids, measure: str) -> dict[int, list[float]]:
    """Per account, ``measure`` (``cost_usd`` / ``requests``) of each local day
    it had usage on, over the ``BUSY_DAY_WINDOW_DAYS`` before today. Days
    without usage are absent, so weekends and leave do not pull a figure down."""
    today = local_today()
    daily: dict[int, list[float]] = defaultdict(list)
    for (uid, _day), agg in aggregate(session, today - timedelta(days=BUSY_DAY_WINDOW_DAYS), today,
                                      ("user_id", "day"), user_ids=user_ids).items():
        daily[int(uid)].append(float(agg[measure]))
    return daily


def detect_cost_spike(session: Session, now: datetime) -> list[dict]:
    day_start = local_day_start_utc()

    rows = session.exec(
        select(UsageLog.user_id, func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.created_at >= day_start)
        .group_by(UsageLog.user_id)
    ).all()
    today = {int(uid): float(cost) for uid, cost in rows if float(cost) >= COST_SPIKE_FLOOR_USD}
    if not today:
        return []
    daily = _daily_history(session, today, "cost_usd")

    findings = []
    for uid, cost in today.items():
        days = daily.get(uid, [])
        busy_day = float(_percentile(days, BUSY_DAY_PCT))
        if len(days) < COST_SPIKE_MIN_DAYS:
            # A new or occasional account: a percentile of a few days says
            # nothing, and a first day on a cloud model is expensive anyway.
            if cost < COST_SPIKE_NEW_ACCOUNT_USD:
                continue
            severity = "warning"
            summary = f"today ${cost:.2f}; only {len(days)} day(s) of usage in the prior {BUSY_DAY_WINDOW_DAYS}d"
        else:
            if cost <= COST_SPIKE_RATIO * busy_day:
                continue
            critical = cost > COST_SPIKE_CRITICAL_RATIO * busy_day and cost >= COST_SPIKE_CRITICAL_USD
            severity = "critical" if critical else "warning"
            summary = f"today ${cost:.2f}; a busy day for this account is ${busy_day:.2f}"
        findings.append(_finding(
            f"user:{uid}", "cost_spike", severity=severity,
            window_start=day_start, window_end=now,
            observed=cost, baseline=busy_day, user_id=uid,
            detail={"summary": summary, "history_days": len(days)},
        ))
    return findings


def detect_off_hours(session: Session, now: datetime) -> list[dict]:
    humans = _human_user_ids(session)
    day_start = local_day_start_utc()
    # Counted in SQL over just the off-hours window: pulling every row of the
    # day into Python to look at its hour is the whole day's traffic.
    rows = session.exec(
        select(UsageLog.user_id, func.count(UsageLog.id))
        .where(UsageLog.created_at >= day_start)
        .where(UsageLog.created_at < day_start + OFF_HOURS_END)
        .group_by(UsageLog.user_id)
    ).all()
    counts = {int(uid): int(n) for uid, n in rows
              if int(uid) in humans and int(n) > OFF_HOURS_MIN_REQUESTS}
    if not counts:
        return []

    # What a busy whole day looks like for each of those accounts. Someone who
    # runs agents overnight has days of many thousands, so their nights are
    # nothing new; a night larger than the account's own busy day is.
    daily = _daily_history(session, counts, "requests")

    findings = []
    for uid, n in counts.items():
        busy_day = int(_percentile(daily.get(uid, []), BUSY_DAY_PCT))
        if n <= busy_day:
            continue
        summary = (f"{n} requests between 00:00-06:00 local; a busy whole day for this account is {busy_day}"
                   if busy_day else
                   f"{n} requests between 00:00-06:00 local; no usage in the prior {BUSY_DAY_WINDOW_DAYS}d")
        findings.append(_finding(
            f"user:{uid}", "off_hours", severity="warning",
            window_start=day_start, window_end=now,
            observed=n, baseline=busy_day, user_id=uid,
            detail={"summary": summary, "username": humans[uid]},
        ))
    return findings


def detect_burst_rate(session: Session, now: datetime) -> list[dict]:
    humans = _human_user_ids(session)
    # One row per account per local day (persist keeps the day's peak hour):
    # keyed on the hour, a burst that lasts the afternoon was a row per scan.
    day_start = local_day_start_utc()
    rows = session.exec(
        select(UsageLog.user_id, func.count(UsageLog.id))
        .where(UsageLog.created_at >= now - timedelta(hours=1))
        .group_by(UsageLog.user_id)
    ).all()
    findings = []
    for uid, n in rows:
        uid, n = int(uid), int(n)
        if uid not in humans or n <= BURST_PER_HOUR:
            continue
        findings.append(_finding(
            f"user:{uid}", "burst_rate",
            severity="critical" if n > BURST_CRITICAL_PER_HOUR else "warning",
            window_start=day_start, window_end=now,
            observed=n, baseline=BURST_PER_HOUR, user_id=uid,
            detail={"summary": f"{n} requests in one hour",
                    "username": humans[uid]},
        ))
    return findings


def detect_behavior_shift(session: Session, now: datetime) -> list[dict]:
    day_start = local_day_start_utc()

    seen: dict[int, set[str]] = defaultdict(set)
    for uid, mtype in aggregate(session, *_baseline_window(), ("user_id", "model_type")):
        seen[int(uid)].add(str(mtype))

    today = session.exec(
        select(UsageLog.user_id, UsageLog.model_type, func.count(UsageLog.id))
        .where(UsageLog.created_at >= day_start)
        .group_by(UsageLog.user_id, UsageLog.model_type)
    ).all()
    findings = []
    for uid, mtype, n in today:
        uid, mtype, n = int(uid), str(mtype), int(n)
        # Only meaningful for accounts with an established history.
        if not seen.get(uid) or mtype in seen[uid] or n <= BEHAVIOR_SHIFT_MIN_REQUESTS:
            continue
        findings.append(_finding(
            f"user:{uid}", "behavior_shift", severity="info",
            window_start=day_start, window_end=now,
            observed=n, baseline=0.0, user_id=uid,
            detail={"summary": f"{n} '{mtype}' requests today; never used this type in prior {BASELINE_DAYS}d",
                    "model_type": mtype},
        ))
    return findings


_CHATTY_TYPES = ("llm", "vlm")


def detect_empty_turn_rate(session: Session, now: datetime) -> list[dict]:
    day_start = local_day_start_utc()
    chatty = UsageLog.model_type.in_(_CHATTY_TYPES)  # type: ignore[attr-defined]
    empty = UsageLog.output_tokens <= 1
    empty_case = sa.case((empty, 1), else_=0)
    # Accounts the empty turns came from. DISTINCT is asked for the last hour
    # only: over a day's rows it sorts every one of them.
    empty_users = func.count(sa.distinct(sa.case((empty, UsageLog.user_id))))

    def rates(since: datetime, until: datetime | None = None, *extra):
        stmt = (
            select(UsageLog.model, func.count(UsageLog.id), func.sum(empty_case), *extra)
            .where(UsageLog.created_at >= since)
            .where(chatty)
            .group_by(UsageLog.model)
        )
        if until is not None:
            stmt = stmt.where(UsageLog.created_at < until)
        return session.exec(stmt).all()

    recent = rates(now - timedelta(hours=1), None, empty_users)
    # Baseline: the BASELINE_DAYS local days before today (rollup's
    # empty_turns where it has them), plus today up to an hour ago.
    base_n: dict[str, int] = defaultdict(int)
    base_e: dict[str, int] = defaultdict(int)
    for (model, mtype), agg in aggregate(session, *_baseline_window(), ("model", "model_type")).items():
        if mtype in _CHATTY_TYPES:
            base_n[str(model)] += int(agg["requests"])
            base_e[str(model)] += int(agg["empty_turns"])
    if now - timedelta(hours=1) > day_start:
        for m, n, e in rates(day_start, now - timedelta(hours=1)):
            base_n[str(m)] += int(n)
            base_e[str(m)] += int(e or 0)
    base_rate = {m: base_e[m] / n for m, n in base_n.items() if n > 0}

    findings = []
    for model, n, empties, users in recent:
        model, n, empties, users = str(model), int(n), int(empties or 0), int(users or 0)
        if n < EMPTY_TURN_MIN_REQUESTS or users < EMPTY_TURN_MIN_USERS:
            continue
        rate = empties / n
        b = base_rate.get(model, 0.0)
        if rate < EMPTY_TURN_RATE_ABS or rate < EMPTY_TURN_RATE_RATIO * max(b, 0.01):
            continue
        findings.append(_finding(
            f"model:{model}", "empty_turn_rate", severity="critical",
            window_start=day_start, window_end=now,
            observed=rate, baseline=b, model=model,
            detail={"summary": f"{empties}/{n} responses in one hour had <=1 output token, from {users} accounts",
                    "requests": n, "empty": empties, "accounts": users},
        ))
    return findings


def _bound_statements(session: Session) -> None:
    """Statement timeout for the rest of the current transaction (PostgreSQL
    only; ``SET LOCAL`` ends with the transaction, so it never leaks into the
    pooled connection)."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(sa.text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))


def detect_all(session: Session, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    findings: list[dict] = []
    for det in (detect_cost_spike, detect_off_hours, detect_burst_rate,
                detect_behavior_shift, detect_empty_turn_rate):
        try:
            _bound_statements(session)
            findings.extend(det(session, now))
        except Exception as exc:  # one broken rule must not kill the scan
            # A failed statement (e.g. the statement timeout) aborts the
            # PostgreSQL transaction; without the rollback every later rule
            # — and persist() — would fail with "transaction is aborted".
            session.rollback()
            logger.warning("Anomaly scan rule {} failed | error={}: {}",
                           det.__name__, type(exc).__name__, exc)
    # A deleted account's usage is kept (billing / audit record), but there
    # is no account to act on, and anomaly_events.user_id is a foreign key:
    # one such finding would fail the whole persist on PostgreSQL.
    existing = {int(uid) for uid in session.exec(select(User.id)).all()}
    return [f for f in findings if f["user_id"] is None or f["user_id"] in existing]


def persist(session: Session, findings: list[dict]) -> tuple[int, int]:
    """Upsert findings; returns (created, updated).

    A row is one scope × rule × window; within it the highest ``observed``
    stays (see below).

    Manual select-then-write keeps this portable across PostgreSQL and the
    SQLite test harness; the scheduler's claim guarantees a single writer per
    run, and the unique constraint backstops any overlap (a manual
    ``scripts/scan_anomalies.py --execute`` alongside the gateway).
    """
    created = updated = 0
    now = datetime.now(timezone.utc)
    for f in findings:
        existing = session.exec(
            select(AnomalyEvent)
            .where(AnomalyEvent.scope == f["scope"])
            .where(AnomalyEvent.rule == f["rule"])
            .where(AnomalyEvent.window_start == f["window_start"])
        ).first()
        if existing:
            # Still happening: the row says so (close_stale counts from
            # updated_at). Its figures are the window's peak: a later, lower
            # hour of the same day must not overwrite the worst one.
            existing.window_end = f["window_end"]
            existing.updated_at = now
            if f["observed"] >= existing.observed:
                existing.observed = f["observed"]
                existing.baseline = f["baseline"]
                existing.ratio = f["ratio"]
                existing.severity = f["severity"]
                existing.detail = f["detail"]
            session.add(existing)
            updated += 1
        else:
            session.add(AnomalyEvent(**f, created_at=now, updated_at=now))
            created += 1
            # NOTE: alert side-channels (email/webhook), when added, hook
            # HERE — on creation only, never on update.
    session.commit()
    return created, updated


def close_stale(session: Session) -> int:
    """Resolve open findings (new or acknowledged) the scan has not seen for
    ``OPEN_EXPIRY_DAYS``. Nothing closed them otherwise, so the admin's list
    filled with last month's. Kept as resolved until ``prune_resolved``."""
    now = datetime.now(timezone.utc)
    stale = session.exec(
        select(AnomalyEvent)
        .where(AnomalyEvent.status.in_(("new", "acknowledged")))  # type: ignore[attr-defined]
        .where(AnomalyEvent.updated_at < now - timedelta(days=OPEN_EXPIRY_DAYS))
    ).all()
    for ev in stale:
        try:
            detail = json.loads(ev.detail)
        except Exception:
            detail = {}
        if isinstance(detail, dict):
            detail["closed"] = f"by the scan: not seen again for {OPEN_EXPIRY_DAYS} days (was {ev.status})"
            ev.detail = json.dumps(detail, ensure_ascii=False)
        ev.status = "resolved"
        ev.updated_at = now
        session.add(ev)
    session.commit()
    return len(stale)


def prune_resolved(session: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RESOLVED_RETENTION_DAYS)
    stale = session.exec(
        select(AnomalyEvent)
        .where(AnomalyEvent.status == "resolved")
        .where(AnomalyEvent.updated_at < cutoff)
    ).all()
    for ev in stale:
        session.delete(ev)
    session.commit()
    return len(stale)


# ── In-gateway schedule ──

DEFAULT_INTERVAL_S = 3600.0


def interval_seconds() -> float:
    """``ANOMALY_SCAN_INTERVAL_S`` (default 3600); ``0`` turns the scan off."""
    return env_seconds("ANOMALY_SCAN_INTERVAL_S", DEFAULT_INTERVAL_S)


def run_scan(session: Session) -> str:
    """Detect, persist, close, prune: one scheduled run. Returns a summary line."""
    findings = detect_all(session)
    created, updated = persist(session, findings)
    closed = close_stale(session)
    pruned = prune_resolved(session)
    return (f"{len(findings)} finding(s): {created} new, {updated} updated; "
            f"closed {closed} not seen again; pruned {pruned} resolved")


SCAN_JOB = Job(name="anomaly_scan", interval=interval_seconds, run=run_scan)
