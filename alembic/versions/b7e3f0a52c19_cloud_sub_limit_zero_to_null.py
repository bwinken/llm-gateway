"""Clear Azure/Bedrock sub-limits of 0 or less to NULL

Revision ID: b7e3f0a52c19
Revises: e4b7c2d91a58
Create Date: 2026-10-13

A sub-limit (``users.azure_daily_limit_usd`` / ``bedrock_daily_limit_usd``)
of 0 or less used to mean "no separate cap", the same as NULL. It now means
a $0 cap: that backend is refused. Rows set to <= 0 under the old meaning
become NULL so they keep behaving as before. Data-only, one UPDATE on
``users``.

Downgrade is a no-op: NULL meant "no separate cap" under the old rules too.
"""

from typing import Sequence, Union

from alembic import context, op


revision: str = "b7e3f0a52c19"
down_revision: Union[str, Sequence[str], None] = "e4b7c2d91a58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE users SET azure_daily_limit_usd = NULL WHERE azure_daily_limit_usd <= 0"
    )
    op.execute(
        "UPDATE users SET bedrock_daily_limit_usd = NULL WHERE bedrock_daily_limit_usd <= 0"
    )


def downgrade() -> None:
    pass
