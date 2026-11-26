"""Add users.sso_disabled_at

Revision ID: a4d81e6c2f95
Revises: c8f2a4b61d39
Create Date: 2026-11-26

``users.sso_disabled_at``: when the offboarding job
(``app/services/offboarding.py``) first saw the identity provider report a
person's sign-in as disabled. NULL for everyone until the job is switched on
(``OFFBOARDING_MODE``) and finds someone. The admin page lists the accounts
that have it and are still active; nothing on the request path reads it.

Safe under live traffic: a nullable column without a default is
metadata-only on PostgreSQL (no table rewrite), added after
``SET LOCAL lock_timeout`` so a busy table fails the migration instead of
queueing requests behind it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4d81e6c2f95"
down_revision: Union[str, Sequence[str], None] = "c8f2a4b61d39"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("sso_disabled_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("users", "sso_disabled_at")
