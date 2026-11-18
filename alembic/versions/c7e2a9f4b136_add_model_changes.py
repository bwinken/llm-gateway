"""Add model_changes and model_change_states

Revision ID: c7e2a9f4b136
Revises: f3a9c6d15e72
Create Date: 2026-11-18

A model that is being replaced, as an admin announces it
(app/services/model_changes.py): ``model_changes`` holds the notice, which
model names to replace with which (for everyone, and what Azure / AWS
accounts get on top) and until when it is shown on dashboards;
``model_change_states`` what one account did about one change (said it
switched) and its mail state. Neither references ``users``: ``username`` is
not a foreign key, so deleting an account never has to touch them.

Two CREATE TABLEs with their indexes: instant and safe under live traffic.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c7e2a9f4b136"
down_revision: Union[str, Sequence[str], None] = "f3a9c6d15e72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), server_default="", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("renames", sa.Text(), server_default="", nullable=False),
        sa.Column("renames_azure", sa.Text(), server_default="", nullable=False),
        sa.Column("renames_aws", sa.Text(), server_default="", nullable=False),
        sa.Column("email_subject", sa.String(), server_default="", nullable=False),
        sa.Column("show_until", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), server_default="", nullable=False),
        sa.Column("updated_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "model_change_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("change_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("mail_requested_at", sa.DateTime(), nullable=True),
        sa.Column("mail_attempted_at", sa.DateTime(), nullable=True),
        sa.Column("mailed_at", sa.DateTime(), nullable=True),
        sa.Column("mail_error", sa.String(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("change_id", "username", name="uq_model_change_states_change_user"),
    )
    op.create_index("ix_model_change_states_change_id", "model_change_states", ["change_id"], unique=False)
    op.create_index("ix_model_change_states_username", "model_change_states", ["username"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_model_change_states_username", table_name="model_change_states")
    op.drop_index("ix_model_change_states_change_id", table_name="model_change_states")
    op.drop_table("model_change_states")
    op.drop_table("model_changes")
