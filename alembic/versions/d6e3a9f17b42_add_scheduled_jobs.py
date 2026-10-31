"""Add scheduled_jobs

Revision ID: d6e3a9f17b42
Revises: b81f4c2a6d35
Create Date: 2026-10-31

Claim table for the jobs the gateway now runs itself instead of systemd
timers (anomaly scan, usage-log retention; app/services/scheduler.py).
Pure CREATE TABLE, instant and safe under live traffic. Rows are created by
the gateway on first use.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d6e3a9f17b42"
down_revision: Union[str, Sequence[str], None] = "b81f4c2a6d35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_jobs",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(), nullable=False, server_default=""),
        sa.Column("last_detail", sa.String(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("scheduled_jobs")
