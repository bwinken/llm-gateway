"""merge app_owners and covering index branches

Revision ID: 5d538cdf0e8b
Revises: d5a2c3e67f04, e8b4f1a23c05
Create Date: 2026-03-15 23:59:28.724992

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d538cdf0e8b'
down_revision: Union[str, Sequence[str], None] = ('d5a2c3e67f04', 'e8b4f1a23c05')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
