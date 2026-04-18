"""Backfill empty ``prompt_templates.description`` rows.

The admin create/update surface now rejects an empty ``description``
(skills with no description fail ``list_skills`` discoverability).
Legacy rows may carry ``description=''`` — this migration backfills
them with ``"Skill: <name>"`` so the new admin-layer constraint does
not strand rows an operator inherited.

Placeholder (not rejection) is deliberate: an upgrade on a busy
cluster should not be blocked by empty descriptions on skills the
operator did not author.  Operators can later rewrite the placeholder
via the admin UI; the audit trail shows who edited each row.

Revision ID: 043
Revises: 042
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE prompt_templates "
            "SET description = 'Skill: ' || name "
            "WHERE description IS NULL OR description = ''"
        )
    )


def downgrade() -> None:
    # The placeholder format is `Skill: <name>`; undo only the rows
    # whose description still matches that exact pattern — an operator
    # who has since rewritten the description should keep their edit.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE prompt_templates SET description = '' WHERE description = 'Skill: ' || name"
        )
    )
