"""add display_name and org_code to users

Revision ID: b4e1f8a23d01
Revises: a3c2d266f101
Create Date: 2026-03-13 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e1f8a23d01'
down_revision: Union[str, Sequence[str], None] = 'a3c2d266f101'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('display_name', sa.String(), server_default='', nullable=False))
    op.add_column('users', sa.Column('org_code', sa.String(), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'org_code')
    op.drop_column('users', 'display_name')
