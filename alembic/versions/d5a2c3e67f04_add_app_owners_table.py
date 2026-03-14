"""add app_owners many-to-many table

Revision ID: d5a2c3e67f04
Revises: c7f3e9b45a02
Create Date: 2026-03-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5a2c3e67f04'
down_revision: Union[str, None] = 'c7f3e9b45a02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_owners',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
    )
    op.create_index('ix_app_owners_app_id', 'app_owners', ['app_id'])
    op.create_index('ix_app_owners_owner_id', 'app_owners', ['owner_id'])

    # Migrate existing owner_id data to the new table
    op.execute(
        "INSERT INTO app_owners (app_id, owner_id) "
        "SELECT id, owner_id FROM users WHERE owner_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index('ix_app_owners_owner_id', 'app_owners')
    op.drop_index('ix_app_owners_app_id', 'app_owners')
    op.drop_table('app_owners')
