from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Index
from sqlmodel import Field, SQLModel


def _generate_api_key() -> str:
    ts = int(datetime.now(timezone.utc).timestamp())
    short_hex = uuid.uuid4().hex[:8]
    return f"sk-internal-{ts}-{short_hex}"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str = Field(default="")
    api_key: str = Field(index=True, unique=True, default_factory=_generate_api_key)
    daily_limit_usd: float = Field(default=10.0)
    is_admin: bool = Field(default=False)
    is_disabled: bool = Field(default=False)
    can_use_azure: bool = Field(default=False)
    owner_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    display_name: str = Field(default="")
    org_code: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AppOwner(SQLModel, table=True):
    """Many-to-many: which users own which app accounts."""
    __tablename__ = "app_owners"

    id: int | None = Field(default=None, primary_key=True)
    app_id: int = Field(foreign_key="users.id", index=True)
    owner_id: int = Field(foreign_key="users.id", index=True)


class UsageLog(SQLModel, table=True):
    __tablename__ = "usage_logs"
    __table_args__ = (
        Index("ix_usage_user_date_cost", "user_id", "created_at", "cost_usd"),
        Index("ix_usage_created_at", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field()
    model: str = Field(default="")
    model_type: str = Field(default="")
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cost_usd: Decimal = Field(default=Decimal("0"), sa_column=sa.Column(sa.Numeric(12, 6), nullable=False, default=0))
    endpoint: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
