"""Add users.cloud_daily_limit_usd

Revision ID: c2d5a8f3e917
Revises: e4b7c2d91a58
Create Date: 2026-10-19

Per-user cloud cap: the most a user may spend on Azure + Bedrock together
per day. NULL (every existing row) follows the gateway-wide default,
[app].cloud_daily_limit_usd in config.toml; a value, 0 included, replaces
it for that user. Cloud spend still counts toward the overall
daily_limit_usd.

Nullable ADD COLUMN is metadata-only on PostgreSQL 11+ (no table rewrite);
the usual lock_timeout guard keeps a busy gateway from queueing behind a
long reader.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "c2d5a8f3e917"
down_revision: Union[str, Sequence[str], None] = "e4b7c2d91a58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(
        "users",
        sa.Column("cloud_daily_limit_usd", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "cloud_daily_limit_usd")
