"""
Aggregate DB data for the web dashboard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case as sa_case
from sqlmodel import Session, func, select

from app.core.timeutil import local_day_start_utc
from app.models.schema import AppOwner, UsageLog, User


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
    """Return daily aggregates for the last N days as a list of dicts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

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

    return [
        {
            "date": str(row[0]),
            "reqs": int(row[1]),
            "cost": round(float(row[2]), 6),
            "input_tokens": int(row[3]),
            "output_tokens": int(row[4]),
        }
        for row in rows
    ]


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
        .where(~User.username.startswith("app_"))
        .group_by(day)
        .order_by(day)
    )
    rows = session.exec(stmt).all()
    return [{"date": str(row[0]), "dau": int(row[1])} for row in rows]


def get_all_users_usage(session: Session) -> dict[int, dict]:
    """Return total cost and total tokens per user for the current month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            UsageLog.user_id,
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.coalesce(
                func.sum(UsageLog.input_tokens) + func.sum(UsageLog.output_tokens), 0
            ),
        )
        .where(UsageLog.created_at >= month_start)
        .group_by(UsageLog.user_id)
    )
    rows = session.exec(stmt).all()
    return {
        int(row[0]): {
            "total_cost": round(float(row[1]), 6),
            "total_tokens": int(row[2]),
        }
        for row in rows
    }


def get_monthly_totals(session: Session) -> dict:
    """Return aggregate monthly totals across all users (single row query)."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = select(
        func.coalesce(func.sum(UsageLog.cost_usd), 0),
        func.coalesce(func.sum(UsageLog.input_tokens), 0),
        func.coalesce(func.sum(UsageLog.output_tokens), 0),
        func.count(UsageLog.id),
    ).where(UsageLog.created_at >= month_start)
    row = session.exec(stmt).first()
    return {
        "cost": round(float(row[0]), 4) if row else 0.0,
        "input_tokens": int(row[1]) if row else 0,
        "output_tokens": int(row[2]) if row else 0,
        "requests": int(row[3]) if row else 0,
    }


def get_leaderboard(
    session: Session, *, is_app: bool, limit: int = 10,
) -> list[dict]:
    """Return top-N users/apps by monthly cost with their usage stats."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    prefix_filter = (
        User.username.startswith("app_") if is_app
        else ~User.username.startswith("app_")
    )

    stmt = (
        select(
            User.id,
            User.username,
            User.display_name,
            User.org_code,
            User.is_admin,
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.count(UsageLog.id),
        )
        .join(UsageLog, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= month_start)
        .where(prefix_filter)
        .group_by(User.id, User.username, User.display_name, User.org_code, User.is_admin)
        .order_by(func.sum(UsageLog.cost_usd).desc())
        .limit(limit)
    )
    rows = session.exec(stmt).all()
    return [
        {
            "id": int(row[0]),
            "username": row[1],
            "display_name": row[2],
            "org_code": row[3],
            "is_admin": row[4],
            "monthly_cost": round(float(row[5]), 6),
            "monthly_input": int(row[6]),
            "monthly_output": int(row[7]),
            "monthly_reqs": int(row[8]),
        }
        for row in rows
    ]


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


def get_department_usage(session: Session) -> list[dict]:
    """Return aggregated monthly usage grouped by org_code (department)."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            func.coalesce(User.org_code, "Unknown"),
            func.count(func.distinct(UsageLog.user_id)),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.count(UsageLog.id),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= month_start)
        .where(~User.username.startswith("app_"))
        .group_by(func.coalesce(User.org_code, "Unknown"))
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = session.exec(stmt).all()
    return [
        {
            "department": str(row[0]) if row[0] else "Unknown",
            "user_count": int(row[1]),
            "cost": round(float(row[2]), 6),
            "input_tokens": int(row[3]),
            "output_tokens": int(row[4]),
            "requests": int(row[5]),
        }
        for row in rows
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
