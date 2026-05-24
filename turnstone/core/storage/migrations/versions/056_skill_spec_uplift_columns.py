"""Add SKILL.md spec-uplift columns to prompt_templates.

The SKILL.md frontmatter spec defines four fields
that map to new columns on ``prompt_templates``:

* ``paths`` — JSON list of glob patterns that gate model-initiated
  autoload.  Consumed by issue #569 (filter logic deferred to a
  follow-up PR pending the workstream-CWD design).
* ``hidden_from_menu`` — boolean; corresponds to the spec's
  ``user-invocable: false``.  When true, hide from the ``/``-menu
  skill picker.  Consumed by issue #571.
* ``arguments`` — JSON list of named positional-argument slots that
  pair with the spec's ``$<name>`` substitution in skill bodies.
  Consumed by issue #572.
* ``argument_hint`` — display string for autocomplete (e.g.
  ``[issue-number]``).  Consumed by issue #572.

Boolean fields stored as INTEGER to match the existing convention
(``is_default``, ``readonly``, ``auto_approve``, ``enabled``).  JSON
list fields default to ``"[]"`` to match ``allowed_tools`` /
``notify_on_complete``.

Revision ID: 056
Revises: 055
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from alembic import op

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("paths", sa.Text, nullable=False, server_default="[]"))
        batch_op.add_column(
            sa.Column("hidden_from_menu", sa.Integer, nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("arguments", sa.Text, nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("argument_hint", sa.Text, nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.drop_column("argument_hint")
        batch_op.drop_column("arguments")
        batch_op.drop_column("hidden_from_menu")
        batch_op.drop_column("paths")
