"""Usage-anomaly scan, run hourly by the gateway itself (``SCAN_JOB`` via
app/services/scheduler.py; ``scripts/scan_anomalies.py`` runs it by hand).

Reads usage, applies a handful of statistics-based rules, and upserts
findings into anomaly_events (one row per scope × rule × window — re-running
a window updates in place, so the scan is idempotent and safe to re-run).

Detection only — it never blocks, throttles, or disables anyone. Admins
review findings in the admin panel (Anomalies card) and act manually.

Rules (thresholds are module constants, tune freely):
  cost_spike      user's spend today > COST_SPIKE_RATIO × their 30-day daily
                  median (and above COST_SPIKE_FLOOR_USD — tiny accounts
                  don't page anyone)
  off_hours       human (non app_*) account with > OFF_HOURS_MIN_REQUESTS
                  requests between 00:00-06:00 local time today
  burst_rate      human account with > BURST_PER_HOUR requests in the last
                  hour (sustained machine-speed usage on a human key)
  behavior_shift  user suddenly using a model_type they never touched in the
                  prior 30 days, > BEHAVIOR_SHIFT_MIN_REQUESTS times today
  empty_turn_rate model-level: share of llm/vlm responses with <= 1 output
                  token over the last hour exceeds EMPTY_TURN_RATE_ABS and
                  3× the model's 30-day baseline (smoke detector for model /
                  template / downstream regressions)

30-day baselines read through ``usage_rollup.aggregate``; the last-hour and
today windows read ``usage_logs``. On PostgreSQL every rule runs under a
``SET LOCAL statement_timeout`` (30 s) so a pathological query can never
linger on the gateway's pool; a rule that fails is logged and rolled back,
and the remaining rules and the persist step still run.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

import sqlalchemy as sa
from sqlmodel import Session, func, select

from app.core.logger import logger
from app.core.timeutil import local_day_start_utc, local_today
from app.models.schema import AnomalyEvent, UsageLog, User, is_app_account
from app.services.scheduler import Job, env_seconds
from app.services.usage_rollup import aggregate

# ── Tunables ──
COST_SPIKE_RATIO = 5.0          # today > N × 30d daily median
COST_SPIKE_FLOOR_USD = 1.0      # ignore spikes below this absolute spend
OFF_HOURS_MIN_REQUESTS = 50     # 00:00-06:00 local requests to flag a human
OFF_HOURS_END = timedelta(hours=6)  # off-hours = local midnight to 06:00
BURST_PER_HOUR = 600            # sustained ~10 req/min for an hour
BURST_CRITICAL_PER_HOUR = 1500
BEHAVIOR_SHIFT_MIN_REQUESTS = 100
EMPTY_TURN_RATE_ABS = 0.20      # >=20% empty turns in the last hour…
EMPTY_TURN_RATE_RATIO = 3.0     # …and 3× the model's 30d baseline
EMPTY_TURN_MIN_REQUESTS = 20    # need volume before the rate means anything
BASELINE_DAYS = 30
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
    rows = session.exec(select(User.id, User.username)).all()
    return {int(uid): name for uid, name in rows if not is_app_account(str(name))}


def _baseline_window() -> tuple:
    """The ``BASELINE_DAYS`` local days before today, as ``aggregate`` bounds."""
    today = local_today()
    return today - timedelta(days=BASELINE_DAYS), today


def detect_cost_spike(session: Session, now: datetime) -> list[dict]:
    day_start = local_day_start_utc()

    today = session.exec(
        select(UsageLog.user_id, func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.created_at >= day_start)
        .group_by(UsageLog.user_id)
    ).all()

    # Per-user daily cost history for the median baseline (users × 30 rows),
    # by local day, from the rollup where it has the days.
    daily: dict[int, list[float]] = defaultdict(list)
    for (uid, _day), agg in aggregate(session, *_baseline_window(), ("user_id", "day")).items():
        daily[int(uid)].append(float(agg["cost_usd"]))

    findings = []
    for uid, cost in today:
        uid, cost = int(uid), float(cost)
        base = median(daily[uid]) if daily.get(uid) else 0.0
        if cost < COST_SPIKE_FLOOR_USD:
            continue
        # No history at all → new account ramping up; flag only huge spends.
        if base <= 0:
            if cost < COST_SPIKE_FLOOR_USD * 10:
                continue
            base = 0.0
        elif cost < COST_SPIKE_RATIO * base:
            continue
        findings.append(_finding(
            f"user:{uid}", "cost_spike",
            severity="critical" if base > 0 and cost > 10 * base else "warning",
            window_start=day_start, window_end=now,
            observed=cost, baseline=base, user_id=uid,
            detail={"summary": f"today ${cost:.2f} vs 30d median ${base:.2f}"},
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
    counts = {int(uid): int(n) for uid, n in rows if int(uid) in humans}

    return [
        _finding(
            f"user:{uid}", "off_hours", severity="warning",
            window_start=day_start, window_end=now,
            observed=n, baseline=OFF_HOURS_MIN_REQUESTS, user_id=uid,
            detail={"summary": f"{n} requests between 00:00-06:00 local",
                    "username": humans[uid]},
        )
        for uid, n in counts.items() if n > OFF_HOURS_MIN_REQUESTS
    ]


def detect_burst_rate(session: Session, now: datetime) -> list[dict]:
    humans = _human_user_ids(session)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
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
            window_start=hour_start, window_end=now,
            observed=n, baseline=BURST_PER_HOUR, user_id=uid,
            detail={"summary": f"{n} requests in the last hour",
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
            f"user:{uid}", "behavior_shift", severity="warning",
            window_start=day_start, window_end=now,
            observed=n, baseline=0.0, user_id=uid,
            detail={"summary": f"{n} '{mtype}' requests today; never used this type in prior {BASELINE_DAYS}d",
                    "model_type": mtype},
        ))
    return findings


_CHATTY_TYPES = ("llm", "vlm")


def detect_empty_turn_rate(session: Session, now: datetime) -> list[dict]:
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    chatty = UsageLog.model_type.in_(_CHATTY_TYPES)  # type: ignore[attr-defined]
    empty_case = sa.case((UsageLog.output_tokens <= 1, 1), else_=0)

    def rates(since: datetime, until: datetime | None = None):
        stmt = (
            select(
                UsageLog.model,
                func.count(UsageLog.id),
                func.sum(empty_case),
            )
            .where(UsageLog.created_at >= since)
            .where(chatty)
            .group_by(UsageLog.model)
        )
        if until is not None:
            stmt = stmt.where(UsageLog.created_at < until)
        return session.exec(stmt).all()

    recent = rates(now - timedelta(hours=1))
    # Baseline: the BASELINE_DAYS local days before today (rollup's
    # empty_turns where it has them), plus today up to an hour ago.
    base_n: dict[str, int] = defaultdict(int)
    base_e: dict[str, int] = defaultdict(int)
    for (model, mtype), agg in aggregate(session, *_baseline_window(), ("model", "model_type")).items():
        if mtype in _CHATTY_TYPES:
            base_n[str(model)] += int(agg["requests"])
            base_e[str(model)] += int(agg["empty_turns"])
    day_start = local_day_start_utc()
    if now - timedelta(hours=1) > day_start:
        for m, n, e in rates(day_start, now - timedelta(hours=1)):
            base_n[str(m)] += int(n)
            base_e[str(m)] += int(e or 0)
    base_rate = {m: base_e[m] / n for m, n in base_n.items() if n > 0}

    findings = []
    for model, n, empty in recent:
        model, n, empty = str(model), int(n), int(empty or 0)
        if n < EMPTY_TURN_MIN_REQUESTS:
            continue
        rate = empty / n
        b = base_rate.get(model, 0.0)
        if rate < EMPTY_TURN_RATE_ABS or rate < EMPTY_TURN_RATE_RATIO * max(b, 0.01):
            continue
        findings.append(_finding(
            f"model:{model}", "empty_turn_rate", severity="critical",
            window_start=hour_start, window_end=now,
            observed=rate, baseline=b, model=model,
            detail={"summary": f"{empty}/{n} responses in the last hour had <=1 output token",
                    "requests": n, "empty": empty},
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
            existing.window_end = f["window_end"]
            existing.observed = f["observed"]
            existing.baseline = f["baseline"]
            existing.ratio = f["ratio"]
            existing.severity = f["severity"]
            existing.detail = f["detail"]
            existing.updated_at = now
            session.add(existing)
            updated += 1
        else:
            session.add(AnomalyEvent(**f, created_at=now, updated_at=now))
            created += 1
            # NOTE: alert side-channels (email/webhook), when added, hook
            # HERE — on creation only, never on update.
    session.commit()
    return created, updated


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
    """Detect, persist, prune: one scheduled run. Returns a summary line."""
    findings = detect_all(session)
    created, updated = persist(session, findings)
    pruned = prune_resolved(session)
    return f"{len(findings)} finding(s): {created} new, {updated} updated; pruned {pruned} resolved"


SCAN_JOB = Job(name="anomaly_scan", interval=interval_seconds, run=run_scan)
