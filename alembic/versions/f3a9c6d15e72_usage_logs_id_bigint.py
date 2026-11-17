"""usage_logs.id: integer -> bigint

Revision ID: f3a9c6d15e72
Revises: e5c1b7d48a20
Create Date: 2026-11-17

``usage_logs`` gets one row per request and its id is a serial number that
is never reused: a 32-bit id ends at 2,147,483,647 requests, however many
rows retention has deleted since. Past that every usage insert fails, and
because a failed usage write never fails the request, the gateway would go
on serving without billing.

**Unlike the other migrations, this one is not safe under live traffic on a
large table.** Changing the width of a column rewrites the whole table and
rebuilds its indexes under an ACCESS EXCLUSIVE lock. API-key auth reads
``usage_logs`` on every request (the daily limit), so the API stalls for as
long as the rewrite takes, and the rewrite needs free disk for a second copy
of the table and its indexes. On a small table that is a moment; above
``_SMALL_BYTES`` the migration refuses until the operator has planned for it
and says so with ``USAGE_LOGS_REWRITE_OK=1`` (deploy/README.md, "Widening
usage_logs.id"). The rewrite also returns the space of deleted rows to the
operating system, so run retention first when both are planned.

The sequence is widened separately: a serial column's sequence keeps its own
type and maximum when the column changes.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "f3a9c6d15e72"
down_revision: Union[str, Sequence[str], None] = "e5c1b7d48a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONFIRM_ENV = "USAGE_LOGS_REWRITE_OK"
_SMALL_BYTES = 1024 ** 3  # table + indexes; below this the rewrite is a moment


def _retype(column_type: str, sequence_type: str) -> None:
    if context.is_offline_mode():
        # --sql cannot look at the table: the operator reads the script.
        op.execute(f"ALTER TABLE usage_logs ALTER COLUMN id TYPE {column_type}")
        op.execute(f"ALTER SEQUENCE usage_logs_id_seq AS {sequence_type}")
        return

    bind = op.get_bind()
    size = int(bind.execute(sa.text("SELECT pg_total_relation_size('usage_logs')")).scalar() or 0)
    if size > _SMALL_BYTES and os.getenv(_CONFIRM_ENV, "").strip() != "1":
        raise RuntimeError(
            f"usage_logs is {size / 1024 ** 3:.1f} GB. Changing its id to {column_type} rewrites the "
            "table and its indexes under an exclusive lock: the API stalls until it is done, and it "
            "needs that much free disk again. Plan a maintenance window, stop the gateway, then run "
            f"this migration with {_CONFIRM_ENV}=1 (deploy/README.md, 'Widening usage_logs.id'). "
            "Nothing was changed."
        )

    # Wait for the lock briefly, never queue behind a long reader with every
    # request queueing behind us. Transaction-scoped; a timeout aborts the
    # migration and it is safe to run again.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"ALTER TABLE usage_logs ALTER COLUMN id TYPE {column_type}")
    sequence = bind.execute(sa.text("SELECT pg_get_serial_sequence('usage_logs', 'id')")).scalar()
    if sequence:
        op.execute(f"ALTER SEQUENCE {sequence} AS {sequence_type}")


def upgrade() -> None:
    _retype("bigint", "bigint")


def downgrade() -> None:
    # Fails, changing nothing, once an id no longer fits 32 bits.
    _retype("integer", "integer")
