"""Add client_violations.mail_requested_at

Revision ID: b4e7a2d9c5f1
Revises: a8d4f1c6e3b9
Create Date: 2026-11-10

When an admin asked for an account to be mailed. A click no longer sends
to everyone at once: it queues the accounts and the mail job sends them in
batches, so the mail server never sees a burst.

A nullable ADD COLUMN without a default: metadata-only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "b4e7a2d9c5f1"
down_revision: Union[str, Sequence[str], None] = "a8d4f1c6e3b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("client_violations", sa.Column("mail_requested_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("client_violations", "mail_requested_at")
