"""Waive the concurrency limit for existing app accounts

Revision ID: e4b7c2d91a58
Revises: d9f2a6b14c07
Create Date: 2026-10-02

App accounts (``app_*``) are service accounts: batch jobs that legitimately
run many requests at once. New ones are created with ``concurrency_waived``
set; this brings existing ones in line. Data-only, one UPDATE on ``users``.

Downgrade is a no-op: it can't tell which rows were waived here and which an
admin waived by hand, and the column itself is dropped by d9f2a6b14c07's
downgrade anyway.
"""

from typing import Sequence, Union

from alembic import context, op


revision: str = "e4b7c2d91a58"
down_revision: Union[str, Sequence[str], None] = "d9f2a6b14c07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    # "_" is a LIKE wildcard; escape it so only the literal "app_" prefix matches.
    op.execute(
        "UPDATE users SET concurrency_waived = true "
        "WHERE username LIKE 'app!_%' ESCAPE '!' AND concurrency_waived = false"
    )


def downgrade() -> None:
    pass
