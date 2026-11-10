"""Drop client_rule_settings.param

Revision ID: f6c3e9b2a7d4
Revises: e5b1d7a4c2f8
Create Date: 2026-11-10

A check's thresholds (e.g. the minimum Claude Code version) are constants
in app/services/client_checks.py now: nothing a rule decides with is kept
in the database. A version carried into ``param`` by e5b1d7a4c2f8 is
dropped with the column.

A metadata-only DROP COLUMN on a table of a few rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "f6c3e9b2a7d4"
down_revision: Union[str, Sequence[str], None] = "e5b1d7a4c2f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("client_rule_settings", "param")


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("client_rule_settings", sa.Column("param", sa.String(), server_default="", nullable=False))
