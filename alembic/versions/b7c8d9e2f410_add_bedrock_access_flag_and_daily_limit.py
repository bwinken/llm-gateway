"""Add users.can_use_bedrock and users.bedrock_daily_limit_usd

Revision ID: b7c8d9e2f410
Revises: a9d4e7c31b06
Create Date: 2026-07-17

AWS Bedrock backend (served via /aws/v1/*), mirroring the Azure access
model:

  - users.can_use_bedrock (bool, default false): gates the /aws/v1/*
    surface and the unified /v1/* Bedrock dispatch. Admins bypass.
  - users.bedrock_daily_limit_usd (nullable): optional Bedrock-specific
    daily sub-limit. Bedrock spend still counts toward the overall
    daily_limit_usd; NULL (the default for every existing row) or <= 0
    means "no separate Bedrock cap".

usage_logs.backend already exists (a9d4e7c31b06) and is a free-form
string — "bedrock" rows need no schema change and there is no historical
data to backfill (the /aws/* endpoints did not exist before this).

Both ADD COLUMNs are metadata-only on PostgreSQL 11+ (constant default /
nullable → no table rewrite); the same lock_timeout guard as the previous
migration keeps a busy gateway from queueing behind a long reader.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "b7c8d9e2f410"
down_revision: Union[str, Sequence[str], None] = "a9d4e7c31b06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(
        "users",
        sa.Column(
            "can_use_bedrock",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("bedrock_daily_limit_usd", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "bedrock_daily_limit_usd")
    op.drop_column("users", "can_use_bedrock")
