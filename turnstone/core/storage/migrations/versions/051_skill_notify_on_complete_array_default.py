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

Downgrade is a deliberate **no-op**: pre-migration ``'{}'`` rows and
operator-written ``'[]'`` rows are indistinguishable after upgrade, and
``'[]'`` is the correct shape under every consumer's interpretation, so
leaving the data untouched on downgrade is strictly safer than reversing
it. (The known-invalid ``'{}'`` sentinel would otherwise re-emerge and
re-trigger the original validator failures.)

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
    # Intentional no-op. The data state cannot be cleanly inverted: a
    # pre-migration ``'{}'`` row and an operator-written ``'[]'`` row both
    # look like ``'[]'`` after upgrade, and rewriting every ``'[]'`` row back
    # to ``'{}'`` would (a) destroy legitimate operator intent and
    # (b) reintroduce the known-invalid sentinel that every consumer of
    # ``notify_on_complete`` rejects. ``'[]'`` is the correct shape for the
    # column under any consumer's interpretation, so leaving the data
    # untouched on downgrade is strictly safer than reversing it.
    pass
