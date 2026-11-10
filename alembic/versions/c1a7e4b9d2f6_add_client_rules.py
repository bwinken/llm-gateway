"""Add client_rule_settings, client_violations, client_versions, client_mail_log

Revision ID: c1a7e4b9d2f6
Revises: 07899844c700
Create Date: 2026-11-10

Client rules (app/services/client_checks.py, client_rules.py). What each
rule detects is code, identified by a violation id (CC001, ...); the
database keeps:

- ``client_rule_settings``: an admin's settings for a check, keyed by its
  id (on / off, action, wording, env patch per audience, mail). No row =
  the defaults in code.
- ``client_violations``: which account tripped which check, with its
  reminder day, mail state (sent, attempted, requested by an admin) and
  when the account reported the fix.
- ``client_versions``: the Claude Code versions each account has used.
- ``client_mail_log``: every mail the gateway tried to send (account,
  address, checks covered, why, sent or failed with the error).

They hold usernames and check ids, not foreign keys, so deleting an
account or retiring a check never has to touch them. Four CREATE TABLEs:
instant and safe under live traffic.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1a7e4b9d2f6"
down_revision: Union[str, Sequence[str], None] = "07899844c700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "client_rule_settings",
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("action", sa.String(), server_default="notice", nullable=False),
        sa.Column("title", sa.String(), server_default="", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("patch", sa.Text(), server_default="", nullable=False),
        sa.Column("patch_azure", sa.Text(), server_default="", nullable=False),
        sa.Column("patch_aws", sa.Text(), server_default="", nullable=False),
        sa.Column("email_subject", sa.String(), server_default="", nullable=False),
        sa.Column("auto_email", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("email_repeat_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_by", sa.String(), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("rule_id"),
    )
    op.create_table(
        "client_violations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("client_version", sa.String(), server_default="", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("reminded_on", sa.String(), server_default="", nullable=False),
        sa.Column("mailed_at", sa.DateTime(), nullable=True),
        sa.Column("mail_attempted_at", sa.DateTime(), nullable=True),
        sa.Column("mail_error", sa.String(), server_default="", nullable=False),
        sa.Column("mail_requested_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "username", name="uq_client_violations_rule_user"),
    )
    op.create_index(op.f("ix_client_violations_rule_id"), "client_violations", ["rule_id"], unique=False)
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
    op.create_table(
        "client_mail_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("username", sa.String(), server_default="", nullable=False),
        sa.Column("address", sa.String(), server_default="", nullable=False),
        sa.Column("subject", sa.String(), server_default="", nullable=False),
        sa.Column("rule_ids", sa.String(), server_default="", nullable=False),
        sa.Column("kind", sa.String(), server_default="", nullable=False),
        sa.Column("ok", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error", sa.String(), server_default="", nullable=False),
        sa.Column("actor", sa.String(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_client_mail_log_created_at", "client_mail_log", ["created_at"], unique=False)
    op.create_index("ix_client_mail_log_username", "client_mail_log", ["username"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_client_mail_log_username", table_name="client_mail_log")
    op.drop_index("ix_client_mail_log_created_at", table_name="client_mail_log")
    op.drop_table("client_mail_log")
    op.drop_index(op.f("ix_client_versions_username"), table_name="client_versions")
    op.drop_table("client_versions")
    op.drop_index(op.f("ix_client_violations_rule_id"), table_name="client_violations")
    op.drop_table("client_violations")
    op.drop_table("client_rule_settings")
