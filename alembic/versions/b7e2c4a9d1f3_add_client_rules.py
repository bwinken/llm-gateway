"""Add client_rules, client_rule_hits, client_versions

Revision ID: b7e2c4a9d1f3
Revises: a3d5e8c1f2b6
Create Date: 2026-11-10

Admin-written client rules (app/services/client_rules.py): which requests
they pick, who matched, and the Claude Code versions accounts use.

The Claude Code auto mode notice of a3d5e8c1f2b6 becomes the first rule
(request has ``safeguards``), so ``users.auto_mode_server_seen_at`` goes:
accounts that were stamped match again on their next request.

Three CREATE TABLEs and a metadata-only DROP COLUMN, safe under live
traffic; ``lock_timeout`` bounds the wait for the brief lock on ``users``.
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "b7e2c4a9d1f3"
down_revision: Union[str, Sequence[str], None] = "a3d5e8c1f2b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AUTO_MODE_MESSAGE = (
    "Your Claude Code asks for a server-side safety check that this gateway's models do not provide, "
    "so auto mode refuses every action (\"Auto mode is unavailable: its safety check does not run for "
    "this session\").\n"
    "Apply the setting below and restart Claude Code: it then runs the check itself, as ordinary requests "
    "that count toward your usage. To choose the model that runs it, also set CLAUDE_CODE_AUTO_MODE_MODEL "
    "to a model name from your dashboard (Claude Code 2.1.302 or later)."
)
_AUTO_MODE_PATCH = '{\n  "env": {\n    "CLAUDE_CODE_AUTO_MODE_SERVER": "0"\n  }\n}'


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    rules = op.create_table(
        "client_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("body_field", sa.String(), server_default="", nullable=False),
        sa.Column("version_below", sa.String(), server_default="", nullable=False),
        sa.Column("models", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("usernames", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("org_codes", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("title", sa.String(), server_default="", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("patch", sa.Text(), server_default="", nullable=False),
        sa.Column("action", sa.String(), server_default="notice", nullable=False),
        sa.Column("email_subject", sa.String(), server_default="", nullable=False),
        sa.Column("auto_email", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("email_repeat_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "client_rule_hits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("client_version", sa.String(), server_default="", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("reminded_on", sa.String(), server_default="", nullable=False),
        sa.Column("mailed_at", sa.DateTime(), nullable=True),
        sa.Column("mail_attempted_at", sa.DateTime(), nullable=True),
        sa.Column("mail_error", sa.String(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(["rule_id"], ["client_rules.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "username", name="uq_client_rule_hits_rule_user"),
    )
    op.create_index(op.f("ix_client_rule_hits_rule_id"), "client_rule_hits", ["rule_id"], unique=False)
    op.create_table(
        "client_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", "client", "version", name="uq_client_versions_user_client_version"),
    )
    op.create_index(op.f("ix_client_versions_username"), "client_versions", ["username"], unique=False)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    op.bulk_insert(rules, [{
        "name": "Claude Code auto mode",
        "body_field": "safeguards",
        "title": "Claude Code auto mode needs a setting to work here",
        "message": _AUTO_MODE_MESSAGE,
        "patch": _AUTO_MODE_PATCH,
        "created_by": "migration",
        "created_at": now,
        "updated_at": now,
    }])
    op.drop_column("users", "auto_mode_server_seen_at")


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("auto_mode_server_seen_at", sa.DateTime(), nullable=True))
    op.drop_index(op.f("ix_client_versions_username"), table_name="client_versions")
    op.drop_table("client_versions")
    op.drop_index(op.f("ix_client_rule_hits_rule_id"), table_name="client_rule_hits")
    op.drop_table("client_rule_hits")
    op.drop_table("client_rules")
