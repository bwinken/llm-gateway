"""Add client_rules.plain_http

Revision ID: c9f4d2b6e8a1
Revises: b7e2c4a9d1f3
Create Date: 2026-11-10

A fourth rule condition: the request reached the gateway over ``http://``
(for finding the clients whose base URL still has to move to https).

One ADD COLUMN with a constant default: metadata-only, safe under live
traffic.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "c9f4d2b6e8a1"
down_revision: Union[str, Sequence[str], None] = "b7e2c4a9d1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("client_rules", sa.Column("plain_http", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("client_rules", "plain_http")
