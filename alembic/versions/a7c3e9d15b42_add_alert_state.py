"""Add alert_state table

Revision ID: a7c3e9d15b42
Revises: c2d5a8f3e917
Create Date: 2026-10-27

Cross-worker dedupe for the optional Teams alerts (app/services/alerts.py).
Pure CREATE TABLE, touches nothing that exists: safe to apply while the
gateway is serving traffic, and unused unless TEAMS_WEBHOOK_URL is set.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a7c3e9d15b42"
down_revision: Union[str, Sequence[str], None] = "c2d5a8f3e917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_state",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("state", sa.String(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("alert_state")
