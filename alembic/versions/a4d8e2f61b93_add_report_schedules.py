"""Add report_schedules

Revision ID: a4d8e2f61b93
Revises: b2f7c9a41e68
Create Date: 2026-11-13

When each weekly report mail goes out and to whom
(app/services/weekly_reports.py): one row per report, written by the admin's
Weekly reports card. Pure CREATE TABLE, instant and safe under live traffic.
A report without a row is off, so nothing is mailed until an admin turns
one on.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4d8e2f61b93"
down_revision: Union[str, Sequence[str], None] = "b2f7c9a41e68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "report_schedules",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("weekday", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_time", sa.String(), nullable=False, server_default="09:00"),
        sa.Column("recipients", sa.String(), nullable=False, server_default=""),
        sa.Column("last_period", sa.Date(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(), nullable=False, server_default=""),
        sa.Column("last_detail", sa.String(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("report_schedules")
