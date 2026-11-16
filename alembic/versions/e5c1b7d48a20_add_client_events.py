"""Add client_events

Revision ID: e5c1b7d48a20
Revises: a4d8e2f61b93
Create Date: 2026-11-16

What a setup tool reports about its user's machine (``POST /v1/events``,
app/services/client_events.py): which account installed, updated, removed
or configured what, in which version, on which operating system. One row
per event, appended and never changed. ``username`` is not a foreign key,
so deleting an account never has to touch the table.

One CREATE TABLE with two indexes: instant and safe under live traffic.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e5c1b7d48a20"
down_revision: Union[str, Sequence[str], None] = "a4d8e2f61b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "client_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("ok", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("tool_version", sa.String(), server_default="", nullable=False),
        sa.Column("kind", sa.String(), server_default="app", nullable=False),
        sa.Column("component", sa.String(), nullable=False),
        sa.Column("client", sa.String(), server_default="", nullable=False),
        sa.Column("component_version", sa.String(), server_default="", nullable=False),
        sa.Column("previous_version", sa.String(), server_default="", nullable=False),
        sa.Column("method", sa.String(), server_default="", nullable=False),
        sa.Column("os", sa.String(), nullable=False),
        sa.Column("os_version", sa.String(), server_default="", nullable=False),
        sa.Column("arch", sa.String(), server_default="", nullable=False),
        sa.Column("hostname", sa.String(), server_default="", nullable=False),
        sa.Column("message", sa.String(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_client_events_created_at", "client_events", ["created_at"], unique=False)
    op.create_index(
        "ix_client_events_username_created_at", "client_events", ["username", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_client_events_username_created_at", table_name="client_events")
    op.drop_index("ix_client_events_created_at", table_name="client_events")
    op.drop_table("client_events")
