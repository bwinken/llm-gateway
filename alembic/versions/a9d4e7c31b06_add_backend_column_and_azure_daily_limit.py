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

Live-service safety: on PostgreSQL 11+ both ADD COLUMN statements are
metadata-only (constant default / nullable → no table rewrite, only a
brief ACCESS EXCLUSIVE lock for the catalog update). The backfill
UPDATE touches only rows whose endpoint starts with '/azure/' — the
Azure proxy has always hard-coded '/azure/v1/...' endpoint labels
(even for requests dispatched from the unified /v1/* surface), so the
prefix is a faithful historical backend marker.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a9d4e7c31b06"
down_revision: Union[str, Sequence[str], None] = "f3a91c5b8d27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
    # Backfill: historical Azure rows are identifiable by their endpoint
    # label. Runs after the column add so new rows written mid-migration
    # already carry the correct value from the application.
    op.execute(
        "UPDATE usage_logs SET backend = 'azure' WHERE endpoint LIKE '/azure/%'"
    )


def downgrade() -> None:
    op.drop_column("users", "azure_daily_limit_usd")
    op.drop_column("usage_logs", "backend")
