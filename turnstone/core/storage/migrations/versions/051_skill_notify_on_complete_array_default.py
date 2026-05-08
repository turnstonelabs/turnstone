"""Backfill ``prompt_templates.notify_on_complete`` from ``'{}'`` to ``'[]'``.

The column was added in migration 011 with ``server_default='{}'`` (an empty
JSON object) and inherited that default through 021's lift into
``prompt_templates``. Every consumer of the field — the admin form, the
JSON-array validator in ``submitEditTemplate``, the
``_validate_notify_targets`` helper in ``server.py``, and the documented
shape (an array of channel/contact identifiers) — treats it as a JSON
array. The mismatch was silent for newly-installed remote skills until
the unlock action exposed them to the editor: opening the modal and
clicking Save tripped the array validator on the inherited ``'{}'`` and
the request never left the browser.

This migration:

* Rewrites every row whose ``notify_on_complete`` is the legacy ``'{}'``
  sentinel (or NULL despite the NOT NULL constraint, defensively) to the
  correct empty-array literal ``'[]'``.
* Does **not** touch rows where an operator has explicitly written a
  non-default value — even if that value is itself non-array, we leave
  it for the admin to fix via the UI rather than guess at intent.
* Pairs with a server-side default change: the column's
  ``server_default`` and every ``create_prompt_template`` /
  Pydantic-schema default are flipped to ``'[]'`` in the same PR so new
  rows land correct.

Revision ID: 051
Revises: 050
Create Date: 2026-05-08
"""

import sqlalchemy as sa
from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE prompt_templates "
            "SET notify_on_complete = '[]' "
            "WHERE notify_on_complete = '{}' OR notify_on_complete IS NULL"
        )
    )


def downgrade() -> None:
    # Restore the legacy ``'{}'`` sentinel only on rows that still hold the
    # post-migration empty-array literal — operator edits stay intact.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE prompt_templates SET notify_on_complete = '{}' WHERE notify_on_complete = '[]'"
        )
    )
