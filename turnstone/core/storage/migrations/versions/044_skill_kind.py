"""Add ``prompt_templates.kind`` classifier (interactive / coordinator / any).

Adds a ``kind`` column so the coordinator's ``list_skills`` tool can
hide skills meant only for interactive child workstreams (and vice
versa).  Allowed values: ``interactive`` / ``coordinator`` / ``any``.
``any`` is the server-default so every pre-existing row keeps its
cluster-wide visibility on both sides after upgrade; skill authors
opt in to the narrower buckets only when they want the classifier
to filter.

Revision ID: 044
Revises: 043
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch:
        batch.add_column(sa.Column("kind", sa.Text, nullable=False, server_default="any"))


def downgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch:
        batch.drop_column("kind")
