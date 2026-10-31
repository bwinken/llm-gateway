"""Add usage_daily rollup and usage_rollup_state

Revision ID: b81f4c2a6d35
Revises: c2d5a8f3e917
Create Date: 2026-10-31

Per-local-day rollup of usage_logs (app/services/usage_rollup.py), filled by
scripts/rollup_usage_daily.py. Pure CREATE TABLE: touches no existing table
and is instant regardless of usage_logs size, so it is safe to apply while
the gateway serves traffic. The tables start empty and reports keep reading
usage_logs until the first rollup run backfills them (no watermark row means
"nothing rolled yet").
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b81f4c2a6d35"
down_revision: Union[str, Sequence[str], None] = "c2d5a8f3e917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("model_type", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(16, 6), nullable=False),
        sa.Column("empty_turns", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("day", "user_id", "backend", "model", "model_type", "endpoint"),
    )
    op.create_index("ix_usage_daily_user_day", "usage_daily", ["user_id", "day"])
    op.create_table(
        "usage_rollup_state",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("rolled_through", sa.Date(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("usage_rollup_state")
    op.drop_index("ix_usage_daily_user_day", table_name="usage_daily")
    op.drop_table("usage_daily")
