"""Add a service account's description

Revision ID: c8f2a4b61d39
Revises: b6e3d2a81f47
Create Date: 2026-11-22

``users.description``: one line an admin writes to say what a service
account is ("HR chat page"). It is shown beside the account's name on its
owners' dashboards and on the dashboard of a person it sent requests for
(``X-On-Behalf-Of``), who otherwise sees only a name such as ``svc-hr-01``.
Empty for everyone until an admin fills it in.

Safe under live traffic: the column is added with a constant default
(metadata-only on PostgreSQL >= 11, no table rewrite), after
``SET LOCAL lock_timeout`` so a busy table fails the migration instead of
queueing requests behind it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c8f2a4b61d39"
down_revision: Union[str, Sequence[str], None] = "b6e3d2a81f47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "users",
        sa.Column("description", sa.String(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("users", "description")
