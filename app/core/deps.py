"""
Dependency: extract and validate API key from Authorization header.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, func, select

from app.core.database import get_session
from app.models.schema import UsageLog, User

_bearer = HTTPBearer(auto_error=False)


def _check_daily_limit(session: Session, user: User) -> None:
    """Reject if user has exceeded their daily spending limit."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    stmt = (
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.user_id == user.id)
        .where(UsageLog.created_at >= today_start)
    )
    today_cost = float(session.exec(stmt).one())
    if today_cost >= user.daily_limit_usd:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily spending limit (${user.daily_limit_usd}) exceeded. Today: ${today_cost:.4f}.",
        )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    session: Session = Depends(get_session),
) -> User:
    # Support both `Authorization: Bearer <key>` (OpenAI-style) and
    # `x-api-key: <key>` (Anthropic-style) for client compatibility.
    api_key: str | None = None
    if credentials is not None:
        api_key = credentials.credentials
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide Authorization: Bearer <key> or x-api-key header.",
        )
    user = session.exec(select(User).where(User.api_key == api_key)).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )
    _check_daily_limit(session, user)
    return user
