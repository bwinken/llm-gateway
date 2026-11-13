"""Add users.is_service_account and mark the existing app accounts

Revision ID: b2f7c9a41e68
Revises: a6d3f0c82e19
Create Date: 2026-11-13

App accounts become service accounts, and what makes an account one is this
column instead of an ``app_`` prefix in its name, so a new one can be named
anything.

- ``users.is_service_account``: ADD COLUMN with a constant default, a
  metadata-only change on PostgreSQL >= 11 (no table rewrite).
- Every account named ``app_*`` is marked: exactly the accounts the name
  test treated as app accounts until now, so every report, the concurrency
  waiver and the mail rules see the same accounts before and after.

``lock_timeout`` makes the migration give up rather than queue live traffic
behind it if ``users`` is busy: re-run it. Deploy order as always: this
migration, then the restart (the new code reads the column).

Downgrade drops the column. The ``app_*`` accounts are app accounts again by
their name; a service account created since without the prefix is then an
ordinary account by name.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "b2f7c9a41e68"
down_revision: Union[str, Sequence[str], None] = "a6d3f0c82e19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "users",
        sa.Column("is_service_account", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # "_" is a LIKE wildcard; escape it so only the literal "app_" prefix matches.
    op.execute(
        "UPDATE users SET is_service_account = true "
        "WHERE username LIKE 'app!_%' ESCAPE '!'"
    )


def downgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("users", "is_service_account")
