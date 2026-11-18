"""Add peak overflow: the permission, the usage mark and its three tables

Revision ID: a4d8e1f62b90
Revises: c7e2a9f4b136
Create Date: 2026-11-18

Peak overflow (app/services/overflow.py) serves some accounts' requests for
a congested on-prem model from the cloud.

- ``users.can_overflow_cloud``: may this account be moved (true for
  everyone until an admin turns it off).
- ``usage_logs.overflow``: this cloud row is one the gateway chose, not the
  account.
- ``usage_daily.overflow``: the same, as part of the rollup's key, so
  reports can tell the two apart on rolled days too.
- ``overflow_servers`` (one row per on-prem server: its queue and streaks),
  ``overflow_stays`` (one row per stay of an account on the cloud),
  ``overflow_budget`` (one row per local day: what overflow has spent).

Safe under live traffic: the three new columns are added with a constant
default (metadata-only on PostgreSQL >= 11, no table rewrite), after
``SET LOCAL lock_timeout`` so a busy table fails the migration instead of
queueing requests behind it. ``usage_daily`` gets a new primary key, which
rebuilds its index under a lock; that table is small (a few rows per account
per day) and no request reads it, only the reports.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4d8e1f62b90"
down_revision: Union[str, Sequence[str], None] = "c7e2a9f4b136"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DAILY_KEY = ("day", "user_id", "backend", "model", "model_type", "endpoint")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "users",
        sa.Column("can_overflow_cloud", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "usage_logs",
        sa.Column("overflow", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "usage_daily",
        sa.Column("overflow", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.drop_constraint("usage_daily_pkey", "usage_daily", type_="primary")
    op.create_primary_key("usage_daily_pkey", "usage_daily", [*_DAILY_KEY, "overflow"])

    op.create_table(
        "overflow_servers",
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("waiting", sa.Integer(), nullable=True),
        sa.Column("running", sa.Integer(), nullable=True),
        sa.Column("over_streak", sa.Integer(), nullable=False),
        sa.Column("under_streak", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("base_url"),
    )
    op.create_table(
        "overflow_stays",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("reason_in", sa.String(), nullable=False),
        sa.Column("reason_out", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_overflow_stays_username", "overflow_stays", ["username"], unique=False)
    op.create_index("ix_overflow_stays_open", "overflow_stays", ["released_at", "base_url"], unique=False)
    op.create_index("ix_overflow_stays_entered", "overflow_stays", ["entered_at"], unique=False)
    op.create_table(
        "overflow_budget",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("spent_usd", sa.Numeric(16, 6), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("overflow_budget")
    op.drop_index("ix_overflow_stays_entered", table_name="overflow_stays")
    op.drop_index("ix_overflow_stays_open", table_name="overflow_stays")
    op.drop_index("ix_overflow_stays_username", table_name="overflow_stays")
    op.drop_table("overflow_stays")
    op.drop_table("overflow_servers")

    # The old key has no ``overflow``: fold each overflow row into the row
    # of the same day / account / model it would collide with, so no usage
    # is lost, then drop the column.
    same_key = " AND ".join(f"d.{c} = o.{c}" for c in _DAILY_KEY)
    op.execute(
        "UPDATE usage_daily d SET "
        "requests = d.requests + o.requests, input_tokens = d.input_tokens + o.input_tokens, "
        "output_tokens = d.output_tokens + o.output_tokens, cost_usd = d.cost_usd + o.cost_usd, "
        "empty_turns = d.empty_turns + o.empty_turns "
        f"FROM usage_daily o WHERE o.overflow AND NOT d.overflow AND {same_key}"
    )
    op.execute(
        "DELETE FROM usage_daily o USING usage_daily d "
        f"WHERE o.overflow AND NOT d.overflow AND {same_key}"
    )
    op.drop_constraint("usage_daily_pkey", "usage_daily", type_="primary")
    op.drop_column("usage_daily", "overflow")
    op.create_primary_key("usage_daily_pkey", "usage_daily", list(_DAILY_KEY))
    op.drop_column("usage_logs", "overflow")
    op.drop_column("users", "can_overflow_cloud")
