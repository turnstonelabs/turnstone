"""Create structured_memories table and migrate existing flat memories.

Revision ID: 014
Revises: 013
Create Date: 2026-03-13
"""

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "structured_memories",
        sa.Column("memory_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("type", sa.Text, nullable=False, server_default="project"),
        sa.Column("scope", sa.Text, nullable=False, server_default="global"),
        sa.Column("scope_id", sa.Text, nullable=False, server_default=""),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
        sa.Column("last_accessed", sa.Text, nullable=False, server_default=""),
        sa.Column("access_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_unique_constraint(
        "uq_smem_name_scope", "structured_memories", ["name", "scope", "scope_id"]
    )
    op.create_index("idx_smem_type", "structured_memories", ["type"])
    op.create_index("idx_smem_scope", "structured_memories", ["scope", "scope_id"])

    # Migrate existing flat memories into structured_memories
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO structured_memories "
            "(memory_id, name, description, type, scope, scope_id, content, created, updated) "
            "SELECT "
            "  'migrated-' || key, "
            "  key, "
            "  '', "
            "  'project', "
            "  'global', "
            "  '', "
            "  value, "
            "  created, "
            "  updated "
            "FROM memories"
        )
    )
    op.drop_table("memories")

    # Grant admin.memories permission to the built-in admin role
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.memories' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.memories%'"
        )
    )


def downgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO memories (key, value, created, updated) "
            "SELECT name, content, created, updated "
            "FROM structured_memories WHERE scope = 'global'"
        )
    )
    op.drop_table("structured_memories")
