"""Drop the model, sampling and task-agent turn columns from skills.

A skill no longer chooses the model alias, sets temperature, reasoning effort or
max tokens, or caps task-agent turns for the workstreams it starts: the alias the
user or operator chose supplies the first four, and the operator's
``tools.agent_max_turns`` setting the turn cap (#1292).  Migration 021 added these
columns when workstream templates merged into ``prompt_templates``.  Their values
are discarded.

Revision ID: 080
Revises: 079
Create Date: 2026-10-07
"""

from alembic import op

revision = "080"
down_revision = "079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.drop_column("model")
        batch_op.drop_column("temperature")
        batch_op.drop_column("reasoning_effort")
        batch_op.drop_column("max_tokens")
        batch_op.drop_column("agent_max_turns")


def downgrade() -> None:
    import sqlalchemy as sa

    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("model", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("temperature", sa.Float, nullable=True))
        batch_op.add_column(
            sa.Column("reasoning_effort", sa.Text, nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("max_tokens", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("agent_max_turns", sa.Integer, nullable=True))
