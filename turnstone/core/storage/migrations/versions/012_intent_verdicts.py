"""Create intent_verdicts table for LLM judge verdicts.

Revision ID: 012
Revises: 011
Create Date: 2026-03-13
"""

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intent_verdicts",
        sa.Column("verdict_id", sa.Text, primary_key=True),
        sa.Column("ws_id", sa.Text, nullable=False),
        sa.Column("call_id", sa.Text, nullable=False),
        sa.Column("func_name", sa.Text, nullable=False),
        sa.Column("func_args", sa.Text, nullable=False, server_default=""),
        sa.Column("intent_summary", sa.Text, nullable=False),
        sa.Column("risk_level", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("recommendation", sa.Text, nullable=False),
        sa.Column("reasoning", sa.Text, nullable=False),
        sa.Column("evidence", sa.Text, nullable=False, server_default="[]"),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column("judge_model", sa.Text, nullable=False, server_default=""),
        sa.Column("user_decision", sa.Text, nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_intent_verdicts_ws", "intent_verdicts", ["ws_id"])
    op.create_index("idx_intent_verdicts_created", "intent_verdicts", ["created"])
    op.create_index("idx_intent_verdicts_risk", "intent_verdicts", ["risk_level"])

    # Grant admin.judge permission to the built-in admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.judge' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.judge%'"
        )
    )


def downgrade() -> None:
    # Remove admin.judge permission from builtin-admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',admin.judge', '') "
            "WHERE role_id = 'builtin-admin'"
        )
    )
    op.drop_table("intent_verdicts")
