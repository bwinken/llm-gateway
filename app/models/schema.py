from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def _generate_api_key() -> str:
    ts = int(datetime.now(timezone.utc).timestamp())
    short_hex = uuid.uuid4().hex[:8]
    return f"sk-internal-{ts}-{short_hex}"


def mask_api_key(key: str) -> str:
    """Render a key for display: enough to identify it, not enough to use it.

    Admin listings show every account at once, so shipping full keys there
    hands whoever loads the page (or the JSON behind it) every credential in
    the org. The last four characters are enough to match against what a
    user reports; the full key is fetched one account at a time from
    `GET /admin/users/{id}/api-key`.
    """
    if not key:
        return ""
    if len(key) <= 12:
        return "\u2026"
    return f"{key[:11]}\u2026{key[-4:]}"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str = Field(default="")
    api_key: str = Field(index=True, unique=True, default_factory=_generate_api_key)
    daily_limit_usd: float = Field(default=10.0)
    # Optional Azure-specific sub-limit. Azure spend still counts toward
    # daily_limit_usd (the overall budget); this additionally caps the Azure
    # portion on its own. NULL or <= 0 → no separate Azure cap (current
    # behavior for every existing user).
    azure_daily_limit_usd: float | None = Field(default=None)
    # Same convention for AWS Bedrock (the /aws/v1/* surface).
    bedrock_daily_limit_usd: float | None = Field(default=None)
    is_admin: bool = Field(default=False)
    is_disabled: bool = Field(default=False)
    can_use_azure: bool = Field(default=False)
    can_use_bedrock: bool = Field(default=False)
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
    # Which downstream served the request: "vllm" (on-prem), "azure", or
    # "bedrock". Lets billing/limits/reports split cloud spend from on-prem
    # spend without parsing the endpoint label.
    backend: str = Field(default="vllm", sa_column=sa.Column(sa.String, nullable=False, server_default="vllm"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AnomalyEvent(SQLModel, table=True):
    """Findings from the periodic usage-anomaly scan (scripts/scan_anomalies.py).

    One row per (scope, rule, window_start) — the scan upserts, so re-running
    the same window is idempotent and an ongoing anomaly keeps updating one
    row (window_end / observed grow) instead of spawning a row per run.
    `scope` is "user:<id>" for per-account rules and "model:<alias>" for
    model-level rules (empty-turn rate), which keeps the uniqueness key
    non-null; `user_id` / `model` are denormalized copies for joins and
    display. Alert side-channels must fire only when a row is CREATED, never
    on updates, so a long-running anomaly alerts exactly once.
    """
    __tablename__ = "anomaly_events"
    __table_args__ = (
        UniqueConstraint("scope", "rule", "window_start", name="uq_anomaly_scope_rule_window"),
        Index("ix_anomaly_status_created", "status", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field()                    # "user:<id>" | "model:<alias>"
    rule: str = Field()                     # cost_spike | off_hours | burst_rate | behavior_shift | empty_turn_rate
    severity: str = Field(default="warning")  # info | warning | critical
    user_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    model: str | None = Field(default=None)
    window_start: datetime = Field()
    window_end: datetime = Field()
    observed: float = Field(default=0.0)
    baseline: float = Field(default=0.0)
    ratio: float = Field(default=0.0)
    detail: str = Field(default="{}")       # JSON: summary + structured context
    status: str = Field(default="new")      # new | acknowledged | resolved
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
