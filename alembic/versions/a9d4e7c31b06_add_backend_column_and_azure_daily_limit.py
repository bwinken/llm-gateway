"""Add usage_logs.backend and users.azure_daily_limit_usd

Revision ID: a9d4e7c31b06
Revises: f3a91c5b8d27
Create Date: 2026-07-07

Splits Azure OpenAI spend from on-prem (vLLM) spend:

  - usage_logs.backend ("vllm" | "azure", default "vllm"): which
    downstream served the request. Written by _log_usage from the
    backend kwarg every billable path already passes.
  - users.azure_daily_limit_usd (nullable): optional Azure-specific
    daily sub-limit. Azure spend still counts toward the overall
    daily_limit_usd; this additionally caps the Azure portion. NULL
    (the default for every existing row) or <= 0 means "no separate
    Azure cap" — i.e. exactly the pre-migration behavior.

Live-service safety on a busy gateway:

  1. ``SET LOCAL lock_timeout`` (PostgreSQL only) makes the ALTERs fail
     fast instead of queueing behind a long-running reader while
     blocking every request's daily-limit query behind them. If the
     migration aborts with a lock timeout, just re-run it.
  2. On PostgreSQL 11+ both ADD COLUMN statements are metadata-only
     (constant default / nullable → no table rewrite), so the ACCESS
     EXCLUSIVE lock they need is held only for a catalog update.
  3. The backfill runs AFTER the schema transaction commits (autocommit
     block) and in small batches, so the exclusive lock from the ALTERs
     is released before any long row-touching work starts, and each
     batch only briefly row-locks the rows it updates. The batch
     predicate (backend = 'vllm' AND endpoint LIKE '/azure/%') makes it
     idempotent — safe to re-run any time to catch stragglers (e.g.
     rows written by old code between migration and deploy).

The '/azure/%' endpoint prefix is a faithful historical backend marker:
azure_proxy has always hard-coded '/azure/v1/...' endpoint labels, even
for requests dispatched from the unified /v1/* surface.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "a9d4e7c31b06"
down_revision: Union[str, Sequence[str], None] = "f3a91c5b8d27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BACKFILL_BATCH = 50_000

# Subquery batching keeps each UPDATE's row-lock footprint small and its
# transaction short. Portable enough for PG + SQLite (the only dialects
# this project runs).
_BATCH_SQL = f"""
UPDATE usage_logs SET backend = 'azure'
WHERE id IN (
    SELECT id FROM usage_logs
    WHERE backend = 'vllm' AND endpoint LIKE '/azure/%%'
    LIMIT {_BACKFILL_BATCH}
)
"""


def upgrade() -> None:
    is_pg = op.get_context().dialect.name == "postgresql"
    if is_pg and not context.is_offline_mode():
        # Fail fast if a long reader holds a lock on usage_logs instead of
        # queueing (which would block all traffic behind us). Transaction-
        # scoped; a timeout aborts the migration — safe to re-run.
        op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(
        "usage_logs",
        sa.Column(
            "backend",
            sa.String(),
            nullable=False,
            server_default="vllm",
        ),
    )
    op.add_column(
        "users",
        sa.Column("azure_daily_limit_usd", sa.Float(), nullable=True),
    )

    if context.is_offline_mode():
        # --sql mode can't loop on rowcount; emit the one-shot statement and
        # let the operator run it (or the batched variant) manually.
        op.execute(
            "UPDATE usage_logs SET backend = 'azure' WHERE endpoint LIKE '/azure/%'"
        )
        return

    # Commit the schema transaction first — releases the ACCESS EXCLUSIVE
    # lock from the ALTERs before the (potentially long) backfill starts.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        total = 0
        while True:
            result = conn.exec_driver_sql(_BATCH_SQL)
            if not result.rowcount:
                break
            total += result.rowcount
        print(f"Backfilled backend='azure' on {total} usage_logs rows")


def downgrade() -> None:
    op.drop_column("users", "azure_daily_limit_usd")
    op.drop_column("usage_logs", "backend")
