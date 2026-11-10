"""Add users.auto_mode_server_seen_at

Revision ID: a3d5e8c1f2b6
Revises: 07899844c700
Create Date: 2026-11-10

When an account's Claude Code last asked for a server-side auto mode review
(app/services/auto_mode_notice.py). Feeds the dashboard notice and the admin
"Needs attention" list.

Nullable without a default: a metadata-only ADD COLUMN, safe under live
traffic. ``lock_timeout`` bounds the wait for the brief table lock.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "a3d5e8c1f2b6"
down_revision: Union[str, Sequence[str], None] = "07899844c700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("auto_mode_server_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("users", "auto_mode_server_seen_at")
