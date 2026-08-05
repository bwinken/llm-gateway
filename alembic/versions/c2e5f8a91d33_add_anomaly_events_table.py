"""Add anomaly_events table

Revision ID: c2e5f8a91d33
Revises: b7c8d9e2f410
Create Date: 2026-08-05

Findings from the periodic usage-anomaly scan (scripts/scan_anomalies.py).
Pure CREATE TABLE — touches no existing table, takes no lock anything else
cares about, and is instant regardless of usage_logs size. Safe to apply
while the gateway is serving traffic.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c2e5f8a91d33"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e2f410"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "anomaly_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("rule", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False, server_default="warning"),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("observed", sa.Float(), nullable=False, server_default="0"),
        sa.Column("baseline", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column("detail", sa.String(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("scope", "rule", "window_start", name="uq_anomaly_scope_rule_window"),
    )
    op.create_index("ix_anomaly_status_created", "anomaly_events", ["status", "created_at"])
    op.create_index("ix_anomaly_events_user_id", "anomaly_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_anomaly_events_user_id", table_name="anomaly_events")
    op.drop_index("ix_anomaly_status_created", table_name="anomaly_events")
    op.drop_table("anomaly_events")
