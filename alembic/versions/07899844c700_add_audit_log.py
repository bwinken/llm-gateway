"""Add audit_log (append-only)

Revision ID: 07899844c700
Revises: d7bf2c9455f4
Create Date: 2026-11-09

The audit trail of Web UI / admin actions, credential reads and exports
(app/services/audit.py). A new table: instant and safe under live traffic.

Append-only at the database too: a trigger refuses UPDATE and DELETE on every
row (TRUNCATE has its own statement trigger). Removing entries is therefore a
deliberate act by someone who can drop the trigger, never a code path.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "07899844c700"
down_revision: Union[str, Sequence[str], None] = "d7bf2c9455f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("route", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_actor_created", "audit_log", ["actor", "created_at"])
    op.execute(
        """
        CREATE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()"
    )


def downgrade() -> None:
    op.drop_table("audit_log")   # drops its triggers and indexes
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
