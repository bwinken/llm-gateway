"""client_rules: environment-only patches, with Azure and AWS variants

Revision ID: d2a8f5c3b7e9
Revises: c9f4d2b6e8a1
Create Date: 2026-11-10

A rule's settings patch now only sets environment variables (the ``env``
block of settings.json), and Azure / AWS accounts can get their own on top
of the base: ``patch_azure`` and ``patch_aws``.

``patch`` changes shape from ``{"env": {...}, ...}`` to the flat
``{NAME: value}``: the ``env`` part of each stored patch is kept, anything
else it carried is dropped.

Two ADD COLUMNs with a constant default (metadata-only) and an UPDATE of
the few rule rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "d2a8f5c3b7e9"
down_revision: Union[str, Sequence[str], None] = "c9f4d2b6e8a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("client_rules", sa.Column("patch_azure", sa.Text(), server_default="", nullable=False))
    op.add_column("client_rules", sa.Column("patch_aws", sa.Text(), server_default="", nullable=False))
    op.execute(
        "UPDATE client_rules SET patch = CASE "
        "WHEN jsonb_typeof(patch::jsonb -> 'env') = 'object' AND (patch::jsonb -> 'env') <> '{}'::jsonb "
        "THEN (patch::jsonb -> 'env')::text ELSE '' END "
        "WHERE patch <> ''"
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE client_rules SET patch = jsonb_build_object('env', patch::jsonb)::text WHERE patch <> ''"
    )
    op.drop_column("client_rules", "patch_aws")
    op.drop_column("client_rules", "patch_azure")
