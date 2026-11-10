"""Add client_violations.resolved_at

Revision ID: a8d4f1c6e3b9
Revises: f6c3e9b2a7d4
Create Date: 2026-11-10

When the account said "I've applied the fix" on its dashboard. While that
is not older than ``last_seen_at`` the violation counts as resolved: no
notice, no mail. A later violation reopens it by itself.

A nullable ADD COLUMN without a default: metadata-only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "a8d4f1c6e3b9"
down_revision: Union[str, Sequence[str], None] = "f6c3e9b2a7d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("client_violations", sa.Column("resolved_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("client_violations", "resolved_at")
