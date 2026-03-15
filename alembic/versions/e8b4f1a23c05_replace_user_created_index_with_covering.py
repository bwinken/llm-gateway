"""replace ix_usage_user_created with covering index including cost_usd

Revision ID: e8b4f1a23c05
Revises: c7f3e9b45a02
Create Date: 2026-03-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e8b4f1a23c05'
down_revision: Union[str, Sequence[str], None] = 'c7f3e9b45a02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use CONCURRENTLY to avoid locking usage_logs during index creation.
    # Must run outside a transaction (autocommit mode).
    op.execute("COMMIT")
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_usage_user_date_cost "
        "ON usage_logs (user_id, created_at, cost_usd)"
    )
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_usage_user_created"
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_usage_user_created "
        "ON usage_logs (user_id, created_at)"
    )
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_usage_user_date_cost"
    )
