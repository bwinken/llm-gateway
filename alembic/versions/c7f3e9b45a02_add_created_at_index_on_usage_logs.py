"""add created_at index on usage_logs for DAU queries

Revision ID: c7f3e9b45a02
Revises: b4e1f8a23d01
Create Date: 2026-03-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c7f3e9b45a02'
down_revision: Union[str, Sequence[str], None] = 'b4e1f8a23d01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_usage_created_at', 'usage_logs', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_usage_created_at', table_name='usage_logs')
