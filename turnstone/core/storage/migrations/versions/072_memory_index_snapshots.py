"""Add immutable memory-index snapshots and durable workstream-id uniqueness.

One first-admitted acting principal binds one index to each globally unique
workstream id. Published ids remain reserved after hard deletion. Access
metadata is reset at the semantic boundary: from this revision onward it
records explicit full-body fetches only, never prompt injection, list, search,
or writes.

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
        "workstream_id_registry",
        sa.Column("ws_id", sa.Text(), primary_key=True),
        sa.Column("created", sa.Text(), nullable=False),
    )
    # A workstream row is not the only durable witness that an id has already
    # named a logical session. Older cleanup paths could leave conversation,
    # routing, config, memory, or governance records after removing the live
    # row. Reserve every such id so a later registration cannot inherit that
    # historical state. ``MIN`` preserves the earliest available timestamp.
    op.execute(
        """
        INSERT INTO workstream_id_registry (ws_id, created)
        SELECT historical.ws_id, MIN(historical.created)
        FROM (
            SELECT ws_id, created FROM workstreams
            UNION ALL
            SELECT ws_id, timestamp AS created FROM conversations
            UNION ALL
            SELECT scope_id AS ws_id, created
            FROM structured_memories
            WHERE scope = 'workstream'
            UNION ALL
            SELECT ws_id, created FROM channel_routes
            UNION ALL
            SELECT ws_id, started AS created FROM scheduled_task_runs
            UNION ALL
            SELECT ws_id, created FROM watches
            UNION ALL
            SELECT ws_id, created FROM workstream_overrides
            UNION ALL
            SELECT ws_id, created FROM usage_events
            UNION ALL
            SELECT ws_id, created FROM intent_verdicts
            UNION ALL
            SELECT ws_id, created FROM output_assessments
        ) AS historical
        WHERE historical.ws_id IS NOT NULL AND historical.ws_id <> ''
        GROUP BY historical.ws_id
        """
    )
    # workstream_config has no timestamp. Add config-only ids at migration
    # time without letting that synthetic value replace a real earlier date.
    op.execute(
        """
        INSERT INTO workstream_id_registry (ws_id, created)
        SELECT config.ws_id, CAST(CURRENT_TIMESTAMP AS TEXT)
        FROM workstream_config AS config
        WHERE config.ws_id <> ''
          AND NOT EXISTS (
              SELECT 1 FROM workstream_id_registry AS registry
              WHERE registry.ws_id = config.ws_id
          )
        GROUP BY config.ws_id
        """
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
    op.execute("UPDATE structured_memories SET last_accessed = '', access_count = 0")


def downgrade() -> None:
    op.drop_table("memory_index_snapshots")
    op.drop_table("workstream_id_registry")
    with op.batch_alter_table("intent_verdicts") as batch_op:
        batch_op.drop_column("execution_principal_id")
        batch_op.drop_column("resolver_principal_id")
