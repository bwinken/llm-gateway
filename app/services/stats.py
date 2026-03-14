"""
Aggregate DB data for the web dashboard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.models.schema import AppOwner, UsageLog, User


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

    stmt = (
        select(
            func.date(UsageLog.created_at).label("day"),
            func.count(UsageLog.id).label("requests"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost"),
        )
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= cutoff)
        .group_by(func.date(UsageLog.created_at))
        .order_by(func.date(UsageLog.created_at))
    )
    rows = session.exec(stmt).all()

    return [
        {
            "date": str(row[0]),
            "reqs": int(row[1]),
            "cost": round(float(row[2]), 6),
        }
        for row in rows
    ]


def get_dau_trends(session: Session, days: int = 30) -> list[dict]:
    """Return daily active user counts for the last N days (excludes app accounts)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = (
        select(
            func.date(UsageLog.created_at).label("day"),
            func.count(func.distinct(UsageLog.user_id)).label("dau"),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= cutoff)
        .where(~User.username.startswith("app_"))
        .group_by(func.date(UsageLog.created_at))
        .order_by(func.date(UsageLog.created_at))
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


def get_model_breakdown(session: Session, user_id: int) -> list[dict]:
    """Per-model usage breakdown for the current month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            UsageLog.model,
            UsageLog.model_type,
            func.count(UsageLog.id).label("requests"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= month_start)
        .group_by(UsageLog.model, UsageLog.model_type)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = session.exec(stmt).all()
    return [
        {
            "model": row[0],
            "type": row[1],
            "requests": int(row[2]),
            "input_tokens": int(row[3]),
            "output_tokens": int(row[4]),
            "cost_usd": round(float(row[5]), 6),
        }
        for row in rows
    ]
