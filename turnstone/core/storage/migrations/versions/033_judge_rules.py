"""Create heuristic_rules and output_guard_patterns tables for configurable judge.

Revision ID: 033
Revises: 032
Create Date: 2026-04-04
"""

import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "heuristic_rules",
        sa.Column("rule_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("risk_level", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("recommendation", sa.Text, nullable=False),
        sa.Column("tool_pattern", sa.Text, nullable=False),
        sa.Column("arg_patterns", sa.Text, nullable=False, server_default="[]"),
        sa.Column("intent_template", sa.Text, nullable=False),
        sa.Column("reasoning_template", sa.Text, nullable=False),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("builtin", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_heuristic_rules_enabled", "heuristic_rules", ["enabled"])
    op.create_index("idx_heuristic_rules_tier", "heuristic_rules", ["tier"])

    op.create_table(
        "output_guard_patterns",
        sa.Column("pattern_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("risk_level", sa.Text, nullable=False),
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column("pattern_flags", sa.Text, nullable=False, server_default=""),
        sa.Column("flag_name", sa.Text, nullable=False),
        sa.Column("annotation", sa.Text, nullable=False),
        sa.Column("is_credential", sa.Integer, nullable=False, server_default="0"),
        sa.Column("redact_label", sa.Text, nullable=False, server_default=""),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("builtin", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_ogp_enabled", "output_guard_patterns", ["enabled"])
    op.create_index("idx_ogp_category", "output_guard_patterns", ["category"])

    # Grant admin.judge permission to builtin-admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.judge' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.judge%'"
        )
    )


def downgrade() -> None:
    op.drop_table("output_guard_patterns")
    op.drop_table("heuristic_rules")
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',admin.judge', '') "
            "WHERE role_id = 'builtin-admin'"
        )
    )
