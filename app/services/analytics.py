"""
Admin-only usage analytics: month-granular aggregations for the xlsx export.

Months are local (Asia/Taipei) calendar months. Usage is read through
``usage_rollup.aggregate`` (closed days from ``usage_daily``, the rest from
``usage_logs``), month by month, which stays portable across PostgreSQL
(prod) and SQLite (tests) without date_trunc/to_char.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

from sqlmodel import Session, select

from app.core.timeutil import next_month_first_day
from app.models.schema import AppOwner, User, is_app_account
from app.services.usage_rollup import aggregate


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


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """Local (Asia/Taipei) days ``[first of month, first of next month)``."""
    return date(year, month, 1), next_month_first_day(year, month)


# ── Per-month aggregations ──
#
# A month is read ONCE, grouped by (user, backend) — ``_month_usage`` —
# and every section of the report is derived from those per-user rows in
# Python. Only the DAU figures need a second, per-(day, user) read. Both go
# through ``aggregate``, so a closed month costs a few thousand
# ``usage_daily`` rows rather than millions of ``usage_logs`` rows.

# Backends a usage_logs row can carry today. Rows written before backend
# tagging existed were backfilled/server-defaulted to "vllm", so unknown
# values can't occur in practice; the pivot still guards by counting any
# unrecognized value under "vllm" (on-prem) so a future backend added
# without updating this tuple degrades to a visible on-prem number rather
# than silently vanishing from the per-backend columns.
_KNOWN_BACKENDS = ("vllm", "azure", "bedrock")


@dataclass
class _UserInfo:
    username: str
    display_name: str
    org_code: str

    @property
    def is_app(self) -> bool:
        return is_app_account(self.username)


def _load_users(session: Session) -> dict[int, _UserInfo]:
    """Every account's identity columns (``users`` is small)."""
    return {
        int(uid): _UserInfo(str(username), display_name or "", org_code or "")
        for uid, username, display_name, org_code in session.exec(
            select(User.id, User.username, User.display_name, User.org_code)
        ).all()
    }


def _month_usage(session: Session, start: date, end: date) -> dict[int, dict]:
    """Per-user totals for one month, with cost split by backend: the
    month's single usage read. ``cost`` is unrounded."""
    per_user: dict[int, dict] = {}
    for (uid, backend), agg in aggregate(session, start, end, ("user_id", "backend")).items():
        entry = per_user.setdefault(int(uid), {
            "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
            "backend_cost": {b: 0.0 for b in _KNOWN_BACKENDS},
        })
        cost = float(agg["cost_usd"])
        entry["requests"] += int(agg["requests"])
        entry["input_tokens"] += int(agg["input_tokens"])
        entry["output_tokens"] += int(agg["output_tokens"])
        entry["cost"] += cost
        entry["backend_cost"][backend if backend in _KNOWN_BACKENDS else "vllm"] += cost
    return per_user


def _daily_active_humans(
    session: Session, start: date, end: date, users: dict[int, _UserInfo],
) -> list[int]:
    """Distinct existing human users per local day with any activity in the month."""
    per_day: dict[date, int] = {}
    for day, uid in aggregate(session, start, end, ("day", "user_id")):
        info = users.get(int(uid))
        if info is not None and not info.is_app:
            per_day[day] = per_day.get(day, 0) + 1
    return [n for _, n in sorted(per_day.items())]


def _summary(
    usage: dict[int, dict], users: dict[int, _UserInfo],
    daily_counts: list[int], start: date,
) -> dict:
    # Totals over everything (humans + apps)
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    avg_dau = (sum(daily_counts) / days_in_month) if days_in_month else 0.0
    return {
        "requests": sum(u["requests"] for u in usage.values()),
        "input_tokens": sum(u["input_tokens"] for u in usage.values()),
        "output_tokens": sum(u["output_tokens"] for u in usage.values()),
        "cost_usd": round(sum(u["cost"] for u in usage.values()), 6),
        "distinct_users": len(usage),
        # MAU = distinct human users with activity this month
        "mau": sum(1 for uid in usage if uid in users and not users[uid].is_app),
        "dau_avg": round(avg_dau, 2),
        "dau_peak": max(daily_counts, default=0),
        "active_days": len(daily_counts),
        "days_in_month": days_in_month,
    }


def _by_department(usage: dict[int, dict], users: dict[int, _UserInfo]) -> list[dict]:
    depts: dict[str, dict] = {}
    for uid, u in usage.items():
        info = users.get(uid)
        if info is None or info.is_app:
            continue
        name = info.org_code or "Unknown"
        d = depts.setdefault(name, {
            "department": name, "users": 0, "requests": 0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        })
        d["users"] += 1
        d["requests"] += u["requests"]
        d["input_tokens"] += u["input_tokens"]
        d["output_tokens"] += u["output_tokens"]
        d["cost_usd"] += u["cost"]
    out = sorted(depts.values(), key=lambda d: d["cost_usd"], reverse=True)
    for d in out:
        d["cost_usd"] = round(d["cost_usd"], 6)
    return out


def _by_app(session: Session, usage: dict[int, dict], users: dict[int, _UserInfo]) -> list[dict]:
    app_ids = sorted(
        (uid for uid in usage if uid in users and users[uid].is_app),
        key=lambda uid: usage[uid]["cost"], reverse=True,
    )

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
            "app_id": uid,
            "app": users[uid].username,
            "org_code": users[uid].org_code,
            "owners": owners_by_app.get(uid, []),
            "requests": usage[uid]["requests"],
            "input_tokens": usage[uid]["input_tokens"],
            "output_tokens": usage[uid]["output_tokens"],
            "cost_usd": round(usage[uid]["cost"], 6),
        }
        for uid in app_ids
    ]


def _user_ranking(usage: dict[int, dict], users: dict[int, _UserInfo]) -> list[dict]:
    """Full ranked list of human users by cost for the month."""
    humans = sorted(
        (uid for uid in usage if uid in users and not users[uid].is_app),
        key=lambda uid: (-usage[uid]["cost"], uid),
    )
    return [
        {
            "rank": idx + 1,
            "user_id": uid,
            "username": users[uid].username,
            "display_name": users[uid].display_name,
            "org_code": users[uid].org_code,
            "requests": usage[uid]["requests"],
            "input_tokens": usage[uid]["input_tokens"],
            "output_tokens": usage[uid]["output_tokens"],
            "cost_usd": round(usage[uid]["cost"], 6),
        }
        for idx, uid in enumerate(humans)
    ]


def _by_user_backend(usage: dict[int, dict], users: dict[int, _UserInfo]) -> list[dict]:
    """Per-user cost/token/request split by serving backend for one month.

    Covers BOTH human users and app_* accounts (the export needs a complete
    billing picture); `is_app` lets consumers separate them. One row per
    user with `vllm_cost_usd` / `azure_cost_usd` / `bedrock_cost_usd`
    columns pivoted from the `usage_logs.backend` tag, sorted by total cost
    descending.
    """
    rows = [
        {
            "user_id": uid,
            "username": users[uid].username,
            "display_name": users[uid].display_name,
            "org_code": users[uid].org_code,
            "is_app": users[uid].is_app,
            "requests": u["requests"],
            "input_tokens": u["input_tokens"],
            "output_tokens": u["output_tokens"],
            **{f"{b}_cost_usd": round(c, 6) for b, c in u["backend_cost"].items()},
            "total_cost_usd": round(u["cost"], 6),
        }
        for uid, u in usage.items()
        if uid in users
    ]
    return sorted(rows, key=lambda e: (-e["total_cost_usd"], e["user_id"]))


def compute_month(
    session: Session, year: int, month: int,
    users: dict[int, _UserInfo] | None = None,
) -> MonthBreakdown:
    start, end = _month_bounds(year, month)
    if users is None:
        users = _load_users(session)
    usage = _month_usage(session, start, end)
    return MonthBreakdown(
        month=f"{year:04d}-{month:02d}",
        summary=_summary(usage, users, _daily_active_humans(session, start, end, users), start),
        by_department=_by_department(usage, users),
        by_app=_by_app(session, usage, users),
        user_ranking=_user_ranking(usage, users),
        by_user_backend=_by_user_backend(usage, users),
    )


def build_monthly_report(session: Session, from_ym: str, to_ym: str) -> MonthlyReport:
    """Compose a multi-month report for the xlsx export."""
    users = _load_users(session)
    breakdowns = [compute_month(session, y, m, users) for y, m in iter_months(from_ym, to_ym)]
    return MonthlyReport(from_ym=from_ym, to_ym=to_ym, breakdowns=breakdowns)


def user_backend_costs(session: Session, from_ym: str, to_ym: str) -> list[tuple[str, list[dict]]]:
    """``(month, by_user_backend rows)`` per month — only the part of the
    report the per-backend CSV needs (one scan per month, no DAU query)."""
    users = _load_users(session)
    out = []
    for y, m in iter_months(from_ym, to_ym):
        start, end = _month_bounds(y, m)
        out.append((f"{y:04d}-{m:02d}", _by_user_backend(_month_usage(session, start, end), users)))
    return out


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
