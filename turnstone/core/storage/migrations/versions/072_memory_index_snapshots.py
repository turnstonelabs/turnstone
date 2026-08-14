"""Add immutable memory-index snapshots and approval-principal witnesses.

One first-admitted acting principal binds one index to each durable workstream
row. Access metadata is reset at the semantic boundary: from this revision
onward it records explicit full-body fetches only, never prompt injection,
list, search, or writes.

Revision ID: 072
Revises: 071
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "072"
down_revision = "071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intent_verdicts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "resolver_principal_id",
                sa.Text(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "execution_principal_id",
                sa.Text(),
                nullable=False,
                server_default="",
            )
        )
    op.create_table(
        "memory_index_snapshots",
        sa.Column("ws_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("project_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("project_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("visibility_key", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column(
            "invalid_description_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("captured_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("ws_id"),
    )
    op.execute("DELETE FROM system_settings WHERE key = 'memory.fetch_limit'")
    op.execute(
        "UPDATE structured_memories SET last_accessed = '', access_count = 0 "
        "WHERE last_accessed <> '' OR access_count <> 0"
    )


def downgrade() -> None:
    op.drop_table("memory_index_snapshots")
    with op.batch_alter_table("intent_verdicts") as batch_op:
        batch_op.drop_column("execution_principal_id")
        batch_op.drop_column("resolver_principal_id")
