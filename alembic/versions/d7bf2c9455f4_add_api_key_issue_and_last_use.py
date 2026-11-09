"""Add users.api_key_created_at and users.api_key_last_used_at

Revision ID: d7bf2c9455f4
Revises: f1c6a7d30e94
Create Date: 2026-11-09

For access reviews: when each account's current API key was issued and when
it last authenticated (stamped by deps._record_key_use, at most hourly).

Both columns are nullable without a default: a metadata-only ADD COLUMN.
Then two backfills:

- ``api_key_created_at`` from the timestamp embedded in keys of the old
  ``sk-internal-<unix time>-<8 hex>`` format; other keys stay NULL (unknown).
- ``api_key_last_used_at`` from the account's newest ``usage_logs`` row, one
  backward scan of ``ix_usage_user_date_cost`` (user_id, created_at, ...)
  per account. It is the account's last use, possibly with a key since
  regenerated; close enough for "unused for 90 days".

The UPDATE row-locks ``users`` until commit; ``lock_timeout`` bounds the wait
for other writers, and the API path only reads ``users``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "d7bf2c9455f4"
down_revision: Union[str, Sequence[str], None] = "f1c6a7d30e94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("api_key_created_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("api_key_last_used_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE users SET api_key_created_at = "
        "(to_timestamp(substring(api_key FROM '^sk-internal-([0-9]{9,11})-[0-9a-f]{8}$')::bigint) "
        "AT TIME ZONE 'UTC') "
        "WHERE api_key ~ '^sk-internal-[0-9]{9,11}-[0-9a-f]{8}$'"
    )
    op.execute(
        "UPDATE users AS u SET api_key_last_used_at = l.last_used "
        "FROM (SELECT u2.id, (SELECT max(ul.created_at) FROM usage_logs ul WHERE ul.user_id = u2.id) AS last_used "
        "      FROM users u2) AS l "
        "WHERE l.id = u.id AND l.last_used IS NOT NULL"
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("users", "api_key_last_used_at")
    op.drop_column("users", "api_key_created_at")
