"""Scheduled tasks and run history tables.

Revision ID: 004
Revises: 003
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("schedule_type", sa.Text, nullable=False),
        sa.Column("cron_expr", sa.Text, nullable=False, server_default=""),
        sa.Column("at_time", sa.Text, nullable=False, server_default=""),
        sa.Column("target_mode", sa.Text, nullable=False, server_default="auto"),
        sa.Column("model", sa.Text, nullable=False, server_default=""),
        sa.Column("initial_message", sa.Text, nullable=False),
        sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
        sa.Column("auto_approve_tools", sa.Text, nullable=False, server_default=""),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("last_run", sa.Text),
        sa.Column("next_run", sa.Text),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_scheduled_tasks_enabled", "scheduled_tasks", ["enabled"])
    op.create_index("idx_scheduled_tasks_next_run", "scheduled_tasks", ["next_run"])

    op.create_table(
        "scheduled_task_runs",
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("task_id", sa.Text, nullable=False),
        sa.Column("node_id", sa.Text, nullable=False, server_default=""),
        sa.Column("ws_id", sa.Text, nullable=False, server_default=""),
        sa.Column("correlation_id", sa.Text, nullable=False, server_default=""),
        sa.Column("started", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="dispatched"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
    )
    op.create_index("idx_scheduled_task_runs_task_id", "scheduled_task_runs", ["task_id"])
    op.create_index("idx_scheduled_task_runs_started", "scheduled_task_runs", ["started"])


def downgrade() -> None:
    op.drop_index("idx_scheduled_task_runs_started", "scheduled_task_runs")
    op.drop_index("idx_scheduled_task_runs_task_id", "scheduled_task_runs")
    op.drop_table("scheduled_task_runs")
    op.drop_index("idx_scheduled_tasks_next_run", "scheduled_tasks")
    op.drop_index("idx_scheduled_tasks_enabled", "scheduled_tasks")
    op.drop_table("scheduled_tasks")
