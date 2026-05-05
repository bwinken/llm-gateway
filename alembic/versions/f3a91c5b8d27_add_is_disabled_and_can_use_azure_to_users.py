"""Add is_disabled and can_use_azure to users

Revision ID: f3a91c5b8d27
Revises: 5d538cdf0e8b
Create Date: 2026-05-05

Adds two boolean access-control columns to the users table:

  - is_disabled (default false): when true, both API key auth and JWT
    web auth reject the user with 403.
  - can_use_azure (default false): gates access to /azure/v1/* endpoints.

Both default to false so existing users behave exactly as before — no
one is auto-disabled, and no one is auto-granted Azure access. Admins
flip the flags via the admin panel afterwards.

PostgreSQL 11+ adds BOOLEAN columns with a constant default in
metadata-only fashion (no table rewrite), so this migration is fast
and lock-light even on large user tables.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f3a91c5b8d27"
down_revision: Union[str, Sequence[str], None] = "5d538cdf0e8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "can_use_azure",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "can_use_azure")
    op.drop_column("users", "is_disabled")
