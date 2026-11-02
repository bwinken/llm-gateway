"""Add users.can_self_allow_ip

Revision ID: b3d8e5f2a610
Revises: f1c6a7d30e94
Create Date: 2026-11-02

Per-user permission to open API access for one's own current address from
the dashboard (API IP allowlist self-service; app/services/ip_allowlist.py),
granted one account at a time from the admin Users table (the "IP" toggle).

ADD COLUMN with a constant default is a metadata-only change on PostgreSQL
>= 11 (no table rewrite), and ``lock_timeout`` makes the migration give up
rather than queue live traffic behind it if ``users`` is busy — re-run it.
Every existing account starts without the permission.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "b3d8e5f2a610"
down_revision: Union[str, Sequence[str], None] = "f1c6a7d30e94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(
        "users",
        sa.Column("can_self_allow_ip", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "can_self_allow_ip")
