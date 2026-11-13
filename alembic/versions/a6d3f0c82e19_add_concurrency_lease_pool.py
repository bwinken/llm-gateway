"""Add concurrency_leases.pool

Revision ID: a6d3f0c82e19
Revises: c1a7e4b9d2f6
Create Date: 2026-11-13

The per-user concurrency limit counts local (vLLM) and cloud (Azure /
Bedrock) requests apart, each against its own limit
(app/services/concurrency.py). ``pool`` says which count a lease is in.

ADD COLUMN with a constant default is a metadata-only change on
PostgreSQL >= 11 (no table rewrite). Safe to apply while the gateway is
serving traffic: the old code inserts leases without the column and they
get ``local``, which is what every lease was until now.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "a6d3f0c82e19"
down_revision: Union[str, Sequence[str], None] = "c1a7e4b9d2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "concurrency_leases",
        sa.Column("pool", sa.String(), nullable=False, server_default="local"),
    )


def downgrade() -> None:
    op.drop_column("concurrency_leases", "pool")
