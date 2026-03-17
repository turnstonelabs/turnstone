"""Add output_assessments table for output guard persistence and scan_version
column on prompt_templates.

Revision ID: 022
Revises: 021
Create Date: 2026-03-16
"""

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "output_assessments",
        sa.Column("assessment_id", sa.Text, primary_key=True),
        sa.Column("ws_id", sa.Text, nullable=False),
        sa.Column("call_id", sa.Text, nullable=False),
        sa.Column("func_name", sa.Text, nullable=False),
        sa.Column("flags", sa.Text, nullable=False, server_default="[]"),
        sa.Column("risk_level", sa.Text, nullable=False, server_default="none"),
        sa.Column("annotations", sa.Text, nullable=False, server_default="[]"),
        sa.Column("output_length", sa.Integer, nullable=False, server_default="0"),
        sa.Column("redacted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("ix_oa_ws_id", "output_assessments", ["ws_id"])
    op.create_index("ix_oa_created", "output_assessments", ["created"])
    op.create_index("ix_oa_risk", "output_assessments", ["risk_level"])

    op.add_column(
        "prompt_templates",
        sa.Column("scan_version", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("prompt_templates", "scan_version")
    op.drop_index("ix_oa_risk", table_name="output_assessments")
    op.drop_index("ix_oa_created", table_name="output_assessments")
    op.drop_index("ix_oa_ws_id", table_name="output_assessments")
    op.drop_table("output_assessments")
