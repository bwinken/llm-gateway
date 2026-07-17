"""
Admin-only usage analytics: month-granular aggregations for the xlsx export.

Queries iterate month-by-month in Python to stay portable across
PostgreSQL (prod) and SQLite (tests) without date_trunc/to_char.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, func, select

from app.models.schema import AppOwner, UsageLog, User


@dataclass
class MonthBreakdown:
    month: str                                     # "YYYY-MM"
    summary: dict                                   # totals + DAU/MAU
    by_department: list[dict]
    by_app: list[dict]
    user_ranking: list[dict]                        # full human ranking (for top-N + delta)
    by_user_backend: list[dict] = field(default_factory=list)  # per-user cost split by backend


@dataclass
class MonthlyReport:
    from_ym: str
    to_ym: str
    breakdowns: list[MonthBreakdown] = field(default_factory=list)


# ── Month iteration / bounds ──

def parse_ym(value: str) -> tuple[int, int]:
    """Parse 'YYYY-MM' into (year, month). Raises ValueError on bad input."""
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"Expected YYYY-MM, got {value!r}")
    y, m = int(parts[0]), int(parts[1])
    if not (1 <= m <= 12):
        raise ValueError(f"Month out of range: {value!r}")
    return y, m


def iter_months(from_ym: str, to_ym: str) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) from start to end."""
    y1, m1 = parse_ym(from_ym)
    y2, m2 = parse_ym(to_ym)
    if (y1, m1) > (y2, m2):
        raise ValueError(f"from ({from_ym}) is after to ({to_ym})")
    months: list[tuple[int, int]] = []
    y, m = y1, m1
    while (y, m) <= (y2, m2):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


# ── Per-month aggregations ──

def _summary(session: Session, start: datetime, end: datetime) -> dict:
    # Totals over everything (humans + apps)
    totals_stmt = select(
        func.count(UsageLog.id),
        func.coalesce(func.sum(UsageLog.input_tokens), 0),
        func.coalesce(func.sum(UsageLog.output_tokens), 0),
        func.coalesce(func.sum(UsageLog.cost_usd), 0),
        func.count(func.distinct(UsageLog.user_id)),
    ).where(UsageLog.created_at >= start).where(UsageLog.created_at < end)
    row = session.exec(totals_stmt).first()
    reqs, inp, out, cost, distinct_users = row if row else (0, 0, 0, 0, 0)

    # MAU = distinct human users with activity this month
    mau_stmt = (
        select(func.count(func.distinct(UsageLog.user_id)))
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
        .where(~User.username.startswith("app_"))
    )
    mau = int(session.exec(mau_stmt).one() or 0)

    # Daily distinct-user counts (humans only) to compute avg/peak DAU.
    dau_stmt = (
        select(
            func.date(UsageLog.created_at),
            func.count(func.distinct(UsageLog.user_id)),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
        .where(~User.username.startswith("app_"))
        .group_by(func.date(UsageLog.created_at))
    )
    daily_counts = [int(r[1]) for r in session.exec(dau_stmt).all()]
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    avg_dau = (sum(daily_counts) / days_in_month) if days_in_month else 0.0
    peak_dau = max(daily_counts) if daily_counts else 0

    return {
        "requests": int(reqs or 0),
        "input_tokens": int(inp or 0),
        "output_tokens": int(out or 0),
        "cost_usd": round(float(cost or 0), 6),
        "distinct_users": int(distinct_users or 0),
        "mau": mau,
        "dau_avg": round(avg_dau, 2),
        "dau_peak": peak_dau,
        "active_days": len(daily_counts),
        "days_in_month": days_in_month,
    }


def _by_department(session: Session, start: datetime, end: datetime) -> list[dict]:
    stmt = (
        select(
            User.org_code,
            func.count(func.distinct(UsageLog.user_id)),
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
        .where(~User.username.startswith("app_"))
        .group_by(User.org_code)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = session.exec(stmt).all()
    return [
        {
            "department": (r[0] or "Unknown"),
            "users": int(r[1]),
            "requests": int(r[2]),
            "input_tokens": int(r[3]),
            "output_tokens": int(r[4]),
            "cost_usd": round(float(r[5]), 6),
        }
        for r in rows
    ]


def _by_app(session: Session, start: datetime, end: datetime) -> list[dict]:
    stmt = (
        select(
            User.id,
            User.username,
            User.org_code,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
        .where(User.username.startswith("app_"))
        .group_by(User.id, User.username, User.org_code)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = session.exec(stmt).all()
    app_ids = [int(r[0]) for r in rows]

    # Resolve owners per app (small second query, only for the apps in this month)
    owners_by_app: dict[int, list[str]] = {}
    if app_ids:
        owner_rows = session.exec(
            select(AppOwner.app_id, User.username)
            .join(User, User.id == AppOwner.owner_id)
            .where(AppOwner.app_id.in_(app_ids))  # type: ignore[attr-defined]
        ).all()
        for app_id, owner_name in owner_rows:
            owners_by_app.setdefault(int(app_id), []).append(str(owner_name))

    return [
        {
            "app_id": int(r[0]),
            "app": r[1],
            "org_code": r[2] or "",
            "owners": owners_by_app.get(int(r[0]), []),
            "requests": int(r[3]),
            "input_tokens": int(r[4]),
            "output_tokens": int(r[5]),
            "cost_usd": round(float(r[6]), 6),
        }
        for r in rows
    ]


def _user_ranking(session: Session, start: datetime, end: datetime) -> list[dict]:
    """Full ranked list of human users by cost for the month."""
    stmt = (
        select(
            User.id,
            User.username,
            User.display_name,
            User.org_code,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
        .where(~User.username.startswith("app_"))
        .group_by(User.id, User.username, User.display_name, User.org_code)
        .order_by(func.sum(UsageLog.cost_usd).desc(), User.id)
    )
    rows = session.exec(stmt).all()
    return [
        {
            "rank": idx + 1,
            "user_id": int(r[0]),
            "username": r[1],
            "display_name": r[2] or "",
            "org_code": r[3] or "",
            "requests": int(r[4]),
            "input_tokens": int(r[5]),
            "output_tokens": int(r[6]),
            "cost_usd": round(float(r[7]), 6),
        }
        for idx, r in enumerate(rows)
    ]


# Backends a usage_logs row can carry today. Rows written before backend
# tagging existed were backfilled/server-defaulted to "vllm", so unknown
# values can't occur in practice; the pivot still guards by counting any
# unrecognized value under "vllm" (on-prem) so a future backend added
# without updating this tuple degrades to a visible on-prem number rather
# than silently vanishing from the per-backend columns.
_KNOWN_BACKENDS = ("vllm", "azure", "bedrock")


def _by_user_backend(session: Session, start: datetime, end: datetime) -> list[dict]:
    """Per-user cost/token/request split by serving backend for one month.

    Covers BOTH human users and app_* accounts (the export needs a complete
    billing picture); `is_app` lets consumers separate them. One row per
    user with `vllm_cost_usd` / `azure_cost_usd` / `bedrock_cost_usd`
    columns pivoted from the `usage_logs.backend` tag, sorted by total cost
    descending.
    """
    stmt = (
        select(
            User.id,
            User.username,
            User.display_name,
            User.org_code,
            UsageLog.backend,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
        .group_by(User.id, User.username, User.display_name, User.org_code, UsageLog.backend)
    )
    rows = session.exec(stmt).all()

    by_user: dict[int, dict] = {}
    for r in rows:
        uid = int(r[0])
        entry = by_user.setdefault(uid, {
            "user_id": uid,
            "username": r[1],
            "display_name": r[2] or "",
            "org_code": r[3] or "",
            "is_app": str(r[1]).startswith("app_"),
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            **{f"{b}_cost_usd": 0.0 for b in _KNOWN_BACKENDS},
            "total_cost_usd": 0.0,
        })
        backend = r[4] if r[4] in _KNOWN_BACKENDS else "vllm"
        cost = float(r[8] or 0)
        entry["requests"] += int(r[5])
        entry["input_tokens"] += int(r[6])
        entry["output_tokens"] += int(r[7])
        entry[f"{backend}_cost_usd"] = round(entry[f"{backend}_cost_usd"] + cost, 6)
        entry["total_cost_usd"] = round(entry["total_cost_usd"] + cost, 6)

    return sorted(
        by_user.values(),
        key=lambda e: (-e["total_cost_usd"], e["user_id"]),
    )


def compute_month(session: Session, year: int, month: int) -> MonthBreakdown:
    start, end = _month_bounds(year, month)
    return MonthBreakdown(
        month=f"{year:04d}-{month:02d}",
        summary=_summary(session, start, end),
        by_department=_by_department(session, start, end),
        by_app=_by_app(session, start, end),
        user_ranking=_user_ranking(session, start, end),
        by_user_backend=_by_user_backend(session, start, end),
    )


def build_monthly_report(session: Session, from_ym: str, to_ym: str) -> MonthlyReport:
    """Compose a multi-month report for the xlsx export."""
    months = iter_months(from_ym, to_ym)
    breakdowns = [compute_month(session, y, m) for y, m in months]
    return MonthlyReport(from_ym=from_ym, to_ym=to_ym, breakdowns=breakdowns)


# ── Top-N with rank delta vs previous month ──

def top_users_with_delta(report: MonthlyReport, limit: int = 10) -> list[dict]:
    """Flatten the per-month rankings into top-N rows and attach prev-month rank."""
    prev_rank_by_user: dict[int, int] = {}
    rows: list[dict] = []
    for bd in report.breakdowns:
        curr_rank_by_user = {r["user_id"]: r["rank"] for r in bd.user_ranking}
        for r in bd.user_ranking[:limit]:
            prev_rank = prev_rank_by_user.get(r["user_id"])
            if prev_rank is None:
                delta: int | None = None  # new entrant
            else:
                # Negative delta = climbed up the ranking (lower number is higher rank)
                delta = prev_rank - r["rank"]
            rows.append({
                "month": bd.month,
                "rank": r["rank"],
                "prev_rank": prev_rank,
                "rank_delta": delta,
                "user_id": r["user_id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "org_code": r["org_code"],
                "requests": r["requests"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cost_usd": r["cost_usd"],
            })
        prev_rank_by_user = curr_rank_by_user
    return rows
