"""Add concurrency_leases table and users.concurrency_waived

Revision ID: d9f2a6b14c07
Revises: c2e5f8a91d33
Create Date: 2026-10-02

Per-user concurrency limit (app/services/concurrency.py):

- ``concurrency_leases`` — one row per in-flight limited request. Pure
  CREATE TABLE, touches nothing that exists.
- ``users.concurrency_waived`` — admin-set exemption. ADD COLUMN with a
  constant default is a metadata-only change on PostgreSQL >= 11 (no table
  rewrite), and ``lock_timeout`` makes the migration give up rather than
  queue live traffic behind it if ``users`` is busy — re-run it.

Safe to apply while the gateway is serving traffic: the code that reads
these defaults to mode "off" and the column defaults to false.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "d9f2a6b14c07"
down_revision: Union[str, Sequence[str], None] = "c2e5f8a91d33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(
        "users",
        sa.Column(
            "concurrency_waived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "concurrency_leases",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_concurrency_user_expires", "concurrency_leases", ["user_id", "expires_at"]
    )
    op.create_index(
        "ix_concurrency_leases_worker_id", "concurrency_leases", ["worker_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_concurrency_leases_worker_id", table_name="concurrency_leases")
    op.drop_index("ix_concurrency_user_expires", table_name="concurrency_leases")
    op.drop_table("concurrency_leases")
    op.drop_column("users", "concurrency_waived")
