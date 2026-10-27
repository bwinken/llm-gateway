"""
Aggregate DB data for the web dashboard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case as sa_case
from sqlmodel import Session, func, select

from app.core.timeutil import LOCAL_TZ, local_day_start_utc
from app.models.schema import AppOwner, UsageLog, User, app_account_clause, is_app_account


def _local_date(session: Session, col):
    """SQL expression: Asia/Taipei calendar date of a UTC-wallclock timestamp column.

    Daily aggregates would otherwise bucket by UTC date, splitting Taipei-morning
    traffic into the previous day's row.
    """
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return func.date(col, "+8 hours")
    return func.date(col + timedelta(hours=8))


def get_user_daily_summary(session: Session, user_id: int) -> dict:
    """Total tokens, cost, and request count for today (Asia/Taipei day).

    Also splits today's cost by backend (``azure_cost_usd`` /
    ``bedrock_cost_usd`` — the remainder is on-prem/vLLM) so the dashboard
    can show the per-cloud sub-budgets.
    """
    today_start = local_day_start_utc()
    azure_case = sa_case((UsageLog.backend == "azure", UsageLog.cost_usd), else_=0)
    bedrock_case = sa_case((UsageLog.backend == "bedrock", UsageLog.cost_usd), else_=0)
    stmt = (
        select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("total_input"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("total_output"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            func.count(UsageLog.id).label("total_requests"),
            func.coalesce(func.sum(azure_case), 0).label("azure_cost"),
            func.coalesce(func.sum(bedrock_case), 0).label("bedrock_cost"),
        )
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= today_start)
    )
    row = session.exec(stmt).first()
    total_cost = float(row[2]) if row else 0.0
    azure_cost = float(row[4]) if row else 0.0
    bedrock_cost = float(row[5]) if row else 0.0
    return {
        "total_input_tokens": int(row[0]) if row else 0,
        "total_output_tokens": int(row[1]) if row else 0,
        "total_cost_usd": total_cost,
        "total_requests": int(row[3]) if row else 0,
        "azure_cost_usd": azure_cost,
        "bedrock_cost_usd": bedrock_cost,
        "vllm_cost_usd": total_cost - azure_cost - bedrock_cost,
    }


def get_user_monthly_summary(session: Session, user_id: int) -> dict:
    """Total tokens and cost for the current calendar month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("total_input"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("total_output"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            func.count(UsageLog.id).label("total_requests"),
        )
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= month_start)
    )
    row = session.exec(stmt).first()
    return {
        "total_input_tokens": int(row[0]) if row else 0,
        "total_output_tokens": int(row[1]) if row else 0,
        "total_cost_usd": float(row[2]) if row else 0.0,
        "total_requests": int(row[3]) if row else 0,
    }


def get_daily_trends(session: Session, user_id: int, days: int = 30) -> list[dict]:
    """Return one row per local day for the last ``days`` days, today included.

    Days without usage are present with zeros. The chart plots one point per
    row on an evenly spaced axis, so a skipped day would draw the line
    straight across a quiet weekend and put non-adjacent days side by side.
    """
    first_day_start = local_day_start_utc() - timedelta(days=days - 1)
    cutoff = first_day_start

    day = _local_date(session, UsageLog.created_at)
    stmt = (
        select(
            day.label("day"),
            func.count(UsageLog.id).label("requests"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
        )
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= cutoff)
        .group_by(day)
        .order_by(day)
    )
    rows = session.exec(stmt).all()
    by_day = {
        str(row[0]): {
            "date": str(row[0]),
            "reqs": int(row[1]),
            "cost": round(float(row[2]), 6),
            "input_tokens": int(row[3]),
            "output_tokens": int(row[4]),
        }
        for row in rows
    }
    first = datetime.now(LOCAL_TZ).date() - timedelta(days=days - 1)
    out = []
    for i in range(days):
        day_str = (first + timedelta(days=i)).isoformat()
        out.append(by_day.get(day_str) or {
            "date": day_str, "reqs": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
        })
    return out


def get_dau_trends(session: Session, days: int = 30) -> list[dict]:
    """Return daily active user counts for the last N days (excludes app accounts)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    day = _local_date(session, UsageLog.created_at)
    stmt = (
        select(
            day.label("day"),
            func.count(func.distinct(UsageLog.user_id)).label("dau"),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= cutoff)
        .where(~app_account_clause())
        .group_by(day)
        .order_by(day)
    )
    rows = session.exec(stmt).all()
    return [{"date": str(row[0]), "dau": int(row[1])} for row in rows]


def get_monthly_all_users_usage(session: Session) -> dict[int, dict]:
    """Return per-user usage for the current calendar month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            UsageLog.user_id,
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.count(UsageLog.id),
        )
        .where(UsageLog.created_at >= month_start)
        .group_by(UsageLog.user_id)
    )
    rows = session.exec(stmt).all()
    return {
        int(row[0]): {
            "cost": round(float(row[1]), 6),
            "input_tokens": int(row[2]),
            "output_tokens": int(row[3]),
            "requests": int(row[4]),
        }
        for row in rows
    }


def get_monthly_overview(session: Session, leaderboard_size: int = 10) -> dict:
    """Everything the admin page shows about the current month, from ONE
    per-user aggregate over ``usage_logs`` (``get_monthly_all_users_usage``)
    plus a ``users`` lookup.

    The month window is the largest scan the page does — millions of rows on
    a busy month — so the totals, both leaderboards and the department table
    are derived here in Python instead of each re-scanning it. Returns:

      ``totals``            cost / input_tokens / output_tokens / requests
      ``per_user``          the per-user map (``get_monthly_all_users_usage``)
      ``app_leaderboard`` / ``user_leaderboard``  top-N by monthly cost
      ``departments``       people (app accounts excluded) grouped by
                            ``org_code`` ("Unknown" when blank), by cost desc
    """
    per_user = get_monthly_all_users_usage(session)
    # Every user, unfiltered: ``users`` is small next to ``usage_logs``, and an
    # IN list of every active id would hit bind-parameter limits.
    users = {
        int(uid): (username, display_name, org_code, is_admin)
        for uid, username, display_name, org_code, is_admin in session.exec(
            select(User.id, User.username, User.display_name, User.org_code, User.is_admin)
        ).all()
    } if per_user else {}

    totals = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0}
    apps: list[dict] = []
    people: list[dict] = []
    departments: dict[str, dict] = {}
    for uid, u in per_user.items():
        totals["cost"] += u["cost"]
        totals["input_tokens"] += u["input_tokens"]
        totals["output_tokens"] += u["output_tokens"]
        totals["requests"] += u["requests"]
        if uid not in users:
            continue  # rows of a since-deleted account count only in the totals
        username, display_name, org_code, is_admin = users[uid]
        row = {
            "id": uid,
            "username": username,
            "display_name": display_name,
            "org_code": org_code,
            "is_admin": is_admin,
            "monthly_cost": u["cost"],
            "monthly_input": u["input_tokens"],
            "monthly_output": u["output_tokens"],
            "monthly_reqs": u["requests"],
        }
        if is_app_account(username):
            apps.append(row)
            continue
        people.append(row)
        dept = departments.setdefault(org_code or "Unknown", {
            "department": org_code or "Unknown", "user_count": 0, "cost": 0.0,
            "input_tokens": 0, "output_tokens": 0, "requests": 0,
        })
        dept["user_count"] += 1
        dept["cost"] += u["cost"]
        dept["input_tokens"] += u["input_tokens"]
        dept["output_tokens"] += u["output_tokens"]
        dept["requests"] += u["requests"]

    def _top(rows: list[dict]) -> list[dict]:
        return sorted(rows, key=lambda r: r["monthly_cost"], reverse=True)[:leaderboard_size]

    totals["cost"] = round(totals["cost"], 4)
    for dept in departments.values():
        dept["cost"] = round(dept["cost"], 6)
    return {
        "totals": totals,
        "per_user": per_user,
        "app_leaderboard": _top(apps),
        "user_leaderboard": _top(people),
        "departments": sorted(departments.values(), key=lambda d: d["cost"], reverse=True),
    }


def get_owned_apps_summary(session: Session, owner_id: int) -> list[dict]:
    """Return app accounts owned by this user, with their monthly usage."""
    app_ids_stmt = select(AppOwner.app_id).where(AppOwner.owner_id == owner_id)
    apps = session.exec(select(User).where(User.id.in_(app_ids_stmt))).all()  # type: ignore[attr-defined]
    if not apps:
        return []

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    app_ids = [a.id for a in apps]
    stmt = (
        select(
            UsageLog.user_id,
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.count(UsageLog.id),
        )
        .where(UsageLog.user_id.in_(app_ids))
        .where(UsageLog.created_at >= month_start)
        .group_by(UsageLog.user_id)
    )
    rows = session.exec(stmt).all()
    usage_by_id = {
        int(row[0]): {
            "cost": round(float(row[1]), 6),
            "input_tokens": int(row[2]),
            "output_tokens": int(row[3]),
            "requests": int(row[4]),
        }
        for row in rows
    }

    return [
        {
            "id": app.id,
            "username": app.username,
            "api_key": app.api_key,
            "daily_limit_usd": app.daily_limit_usd,
            "monthly_cost": usage_by_id.get(app.id, {}).get("cost", 0.0),
            "monthly_input": usage_by_id.get(app.id, {}).get("input_tokens", 0),
            "monthly_output": usage_by_id.get(app.id, {}).get("output_tokens", 0),
            "monthly_reqs": usage_by_id.get(app.id, {}).get("requests", 0),
        }
        for app in apps
    ]


def get_model_breakdown(
    session: Session, user_id: int, period: str = "month",
) -> list[dict]:
    """Per-model usage breakdown, split by backend.

    Grouped by (model alias, backend) so the dashboard can show which models
    the user's spend went to, with on-prem (vLLM) and cloud (Azure / Bedrock)
    portions kept separate. ``period`` picks the window: ``"month"`` is UTC
    month start — the same window as ``get_user_monthly_summary`` so the
    per-model costs sum to the Monthly Cost card; ``"day"`` is the local
    (Asia/Taipei) day start — the same window as ``get_user_daily_summary``
    and the Today's Cost card.
    """
    if period == "day":
        since = local_day_start_utc()
    else:
        now = datetime.now(timezone.utc)
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            UsageLog.model,
            UsageLog.model_type,
            UsageLog.backend,
            func.count(UsageLog.id).label("requests"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= since)
        .group_by(UsageLog.model, UsageLog.model_type, UsageLog.backend)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = session.exec(stmt).all()
    return [
        {
            "model": row[0],
            "type": row[1],
            "backend": row[2] or "vllm",
            "requests": int(row[3]),
            "input_tokens": int(row[4]),
            "output_tokens": int(row[5]),
            "cost_usd": round(float(row[6]), 6),
        }
        for row in rows
    ]


def get_today_totals(session: Session) -> dict:
    """All-account totals for today (local day): cost, requests, and how many
    people (app accounts excluded) made at least one request."""
    today_start = local_day_start_utc()
    row = session.exec(
        select(
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.count(UsageLog.id),
        ).where(UsageLog.created_at >= today_start)
    ).first()
    active = session.exec(
        select(func.count(func.distinct(UsageLog.user_id)))
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= today_start)
        .where(~app_account_clause())
    ).one()
    return {
        "cost": round(float(row[0]), 4) if row else 0.0,
        "requests": int(row[1]) if row else 0,
        "active_users": int(active or 0),
    }


def get_budget_pressure(session: Session, threshold: float = 0.9) -> list[dict]:
    """Enabled accounts whose spend today has reached ``threshold`` of their
    daily limit. Accounts with a $0 limit are left out: they are blocked on
    purpose, not running out.
    Sorted by share used, highest first — the "about to get 429s" list."""
    today_start = local_day_start_utc()
    spent = (
        select(UsageLog.user_id, func.sum(UsageLog.cost_usd).label("cost"))
        .where(UsageLog.created_at >= today_start)
        .group_by(UsageLog.user_id)
        .subquery()
    )
    rows = session.exec(
        select(User.id, User.username, User.display_name, User.daily_limit_usd, spent.c.cost)
        .join(spent, spent.c.user_id == User.id)
        .where(User.daily_limit_usd > 0)
        .where(~User.is_disabled)
    ).all()
    out = []
    for uid, username, display_name, limit, cost in rows:
        cost = float(cost or 0)
        share = cost / float(limit)
        if share >= threshold:
            out.append({
                "id": uid, "username": username, "display_name": display_name or "",
                "cost": round(cost, 4), "limit": float(limit), "percent": round(share * 100, 1),
            })
    out.sort(key=lambda r: r["percent"], reverse=True)
    return out


QUEUE_OVERLOADED = 10  # waiting requests at which a server is shown as overloaded


def summarize_server_status(servers: list[dict]) -> dict:
    """Roll per-model on-prem status up for a page header.

    ``servers`` items carry ``name``, ``alive`` and ``waiting`` (None when the
    server's /metrics is unavailable). Returns counts plus the names behind
    each non-healthy bucket, so a page can say *which* models need a look.
    """
    down = [s["name"] for s in servers if not s["alive"]]
    queueing = [
        {"name": s["name"], "waiting": s["waiting"]}
        for s in servers
        if s["alive"] and s.get("waiting")
    ]
    queueing.sort(key=lambda s: s["waiting"], reverse=True)
    return {
        "total": len(servers),
        "online": len(servers) - len(down),
        "down": down,
        "queueing": queueing,
        "overloaded": [s for s in queueing if s["waiting"] >= QUEUE_OVERLOADED],
    }
