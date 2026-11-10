"""Client rules: checks live in code, the database keeps settings and violations

Revision ID: e5b1d7a4c2f8
Revises: d2a8f5c3b7e9
Create Date: 2026-11-10

What each client rule detects is now code (app/services/client_checks.py),
identified by a violation id (CC001, ...). The database keeps:

- ``client_rule_settings``: an admin's settings for a check, keyed by its id
  (no row = the defaults in code);
- ``client_violations``: which account tripped which check.

They replace ``client_rules`` (admin-written conditions) and
``client_rule_hits``. Rows of the old tables that correspond to a built-in
check are carried over: the auto mode rule (request has ``safeguards``) to
CC001, a plain-http rule to CC002, a version rule to CC003 (its version
becomes the parameter), with their recorded accounts. The untouched seeded
auto mode rule is not carried over, so CC001 starts from its defaults in
code. Any other admin-written rule has no counterpart and is dropped.

``client_versions`` is unchanged. Small tables only; nothing here touches
``users`` or the usage tables.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "e5b1d7a4c2f8"
down_revision: Union[str, Sequence[str], None] = "d2a8f5c3b7e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Old rule -> check id: the first (lowest id) rule of each recognizable shape.
_MAPPING = """
    SELECT DISTINCT ON (check_id) id AS old_id, check_id FROM (
        SELECT id, CASE
            WHEN body_field = 'safeguards' AND version_below = '' AND NOT plain_http
                 AND models::text = '[]' THEN 'CC001'
            WHEN plain_http AND body_field = '' AND version_below = '' AND models::text = '[]' THEN 'CC002'
            WHEN version_below <> '' AND body_field = '' AND NOT plain_http AND models::text = '[]' THEN 'CC003'
        END AS check_id
        FROM client_rules
        WHERE usernames::text = '[]' AND org_codes::text = '[]'
    ) m WHERE check_id IS NOT NULL ORDER BY check_id, id
"""


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "client_rule_settings",
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("action", sa.String(), server_default="notice", nullable=False),
        sa.Column("param", sa.String(), server_default="", nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "username", name="uq_client_violations_rule_user"),
    )
    op.create_index(op.f("ix_client_violations_rule_id"), "client_violations", ["rule_id"], unique=False)

    op.execute(
        "INSERT INTO client_rule_settings (rule_id, enabled, action, param, title, message, patch, patch_azure, "
        "patch_aws, email_subject, auto_email, email_repeat_days, updated_by, updated_at) "
        "SELECT m.check_id, r.enabled, r.action, CASE WHEN m.check_id = 'CC003' THEN r.version_below ELSE '' END, "
        "r.title, r.message, r.patch, r.patch_azure, r.patch_aws, r.email_subject, r.auto_email, "
        "r.email_repeat_days, r.created_by, r.updated_at "
        f"FROM client_rules r JOIN ({_MAPPING}) m ON m.old_id = r.id "
        "WHERE NOT (r.created_by = 'migration' AND r.updated_at = r.created_at)"
    )
    op.execute(
        "INSERT INTO client_violations (rule_id, username, client_version, first_seen_at, last_seen_at, "
        "reminded_on, mailed_at, mail_attempted_at, mail_error) "
        "SELECT m.check_id, h.username, h.client_version, h.first_seen_at, h.last_seen_at, h.reminded_on, "
        "h.mailed_at, h.mail_attempted_at, h.mail_error "
        f"FROM client_rule_hits h JOIN ({_MAPPING}) m ON m.old_id = h.rule_id"
    )

    op.drop_index(op.f("ix_client_rule_hits_rule_id"), table_name="client_rule_hits")
    op.drop_table("client_rule_hits")
    op.drop_table("client_rules")


def downgrade() -> None:
    """Recreates the old tables empty: settings and violations do not map
    back onto admin-written rules."""
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "client_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("body_field", sa.String(), server_default="", nullable=False),
        sa.Column("version_below", sa.String(), server_default="", nullable=False),
        sa.Column("plain_http", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("models", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("usernames", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("org_codes", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("title", sa.String(), server_default="", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("patch", sa.Text(), server_default="", nullable=False),
        sa.Column("patch_azure", sa.Text(), server_default="", nullable=False),
        sa.Column("patch_aws", sa.Text(), server_default="", nullable=False),
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
    op.drop_index(op.f("ix_client_violations_rule_id"), table_name="client_violations")
    op.drop_table("client_violations")
    op.drop_table("client_rule_settings")
