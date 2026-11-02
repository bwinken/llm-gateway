"""Add ip_allow_rules

Revision ID: f1c6a7d30e94
Revises: d6e3a9f17b42
Create Date: 2026-11-02

The API IP allowlist (app/services/ip_allowlist.py): permanent and
time-limited CIDR rules, added by admins, each for every account or for
listed usernames / org codes. Pure CREATE TABLE, instant and safe under live traffic.
The allowlist stays off ([app].ip_allowlist_mode = "off") until an admin
turns it on, so an empty table admits everyone.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f1c6a7d30e94"
down_revision: Union[str, Sequence[str], None] = "d6e3a9f17b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ip_allow_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cidr", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("usernames", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("org_codes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ip_allow_rules")
