"""Add acting for a user: the service account's switch and the usage mark

Revision ID: b6e3d2a81f47
Revises: a4d8e1f62b90
Create Date: 2026-11-19

A service account can send requests for the people using it
(``X-On-Behalf-Of``, app/core/deps.py): the request is then the named
person's, billed to them, and the service account is recorded as the way it
came in.

- ``users.acts_for_users``: this service account must name its user on every
  request (false for everyone until an admin turns it on).
- ``usage_logs.via_user_id``: the service account that sent the request for
  ``user_id``, 0 when the account used its own key.
- ``usage_daily.via_user_id``: the same, as part of the rollup's key, so
  reports can tell the two apart on rolled days too.

Safe under live traffic: every column is added with a constant default
(metadata-only on PostgreSQL >= 11, no table rewrite, ``usage_logs``
included), after ``SET LOCAL lock_timeout`` so a busy table fails the
migration instead of queueing requests behind it. ``usage_daily`` gets a new
primary key, which rebuilds its index under a lock; that table is small (a
few rows per account per day) and no request reads it, only the reports.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b6e3d2a81f47"
down_revision: Union[str, Sequence[str], None] = "a4d8e1f62b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DAILY_KEY = ("day", "user_id", "backend", "model", "model_type", "endpoint", "overflow")
_DAILY_SUMS = ("requests", "input_tokens", "output_tokens", "cost_usd", "empty_turns")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "users",
        sa.Column("acts_for_users", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "usage_logs",
        sa.Column("via_user_id", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "usage_daily",
        sa.Column("via_user_id", sa.Integer(), server_default="0", nullable=False),
    )
    op.drop_constraint("usage_daily_pkey", "usage_daily", type_="primary")
    op.create_primary_key("usage_daily_pkey", "usage_daily", [*_DAILY_KEY, "via_user_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # The old key has no ``via_user_id``: a user's rows of one day / model
    # that differ only in the service account they came through (or in
    # having come through none) become one row, so no usage is lost.
    key = ", ".join(_DAILY_KEY)
    same_key = " AND ".join(f"d.{c} = f.{c}" for c in _DAILY_KEY)
    sums = ", ".join(f"sum({c}) AS {c}" for c in _DAILY_SUMS)
    op.execute(
        f"CREATE TEMP TABLE usage_daily_fold ON COMMIT DROP AS "
        f"SELECT {key}, {sums} FROM usage_daily d "
        f"WHERE EXISTS (SELECT 1 FROM usage_daily f WHERE f.via_user_id <> 0 AND {same_key}) "
        f"GROUP BY {key}"
    )
    op.execute(f"DELETE FROM usage_daily d USING usage_daily_fold f WHERE {same_key}")
    op.execute(
        f"INSERT INTO usage_daily ({key}, via_user_id, {', '.join(_DAILY_SUMS)}) "
        f"SELECT {key}, 0, {', '.join(_DAILY_SUMS)} FROM usage_daily_fold"
    )
    op.drop_constraint("usage_daily_pkey", "usage_daily", type_="primary")
    op.drop_column("usage_daily", "via_user_id")
    op.create_primary_key("usage_daily_pkey", "usage_daily", list(_DAILY_KEY))
    op.drop_column("usage_logs", "via_user_id")
    op.drop_column("users", "acts_for_users")
