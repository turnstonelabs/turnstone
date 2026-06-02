"""Un-wrap legacy ``<tool_output>`` advisory envelopes; drop ``_reminders``.

Operator-context (output-guard findings, user interjections, metacognitive
nudges) used to ride two transient carriers baked into stored rows: a
``<tool_output>`` / ``<system-reminder>`` envelope spliced into tool-message
``content`` (``tool_advisory.wrap_tool_result``), and a JSON ``_reminders``
side-channel column.  Both are replaced by first-class ``{"role":"system"}``
turns, so the legacy carriers are now dead on the read path.

This migration retires both carriers:

* **Envelopes** — every tool row whose ``content`` is a wrapped envelope is
  rewritten to the bare tool output.  The embedded ``<system-reminder>``
  advisories are intentionally dropped (cosmetic loss of historical nudge /
  interjection bubbles — the design accepts this rather than resurrecting them
  as new rows).  The structural guard requires the *complete* legacy envelope
  signature (``<tool_output>`` open, the exact ``</tool_output>\\n\\n<system-
  reminder>\\n`` join, and a trailing ``</system-reminder>``) — not merely a
  ``<tool_output>`` open + close — so a bare tool row that resembles the open
  cannot be irreversibly mis-rewritten.  Only the ``&amp;`` → ``&`` half of the
  original escape is reversed: re-activating the wrapper-tag escapes
  (``&lt;system-reminder&gt;`` → ``<system-reminder>``) would un-defang
  injection the old escape had neutralised, so those entities are left as-is.
* **Reminders** — the ``conversations._reminders`` column is dropped outright
  (``batch_alter_table``, per migration 027).  Nothing writes it and the read
  path no longer reads it, so carrying it forward would only leave a writable
  dead column as a foot-gun.

``downgrade()`` re-adds the (empty) ``_reminders`` column so the schema matches
the 059 state, but does NOT reverse the envelope un-wrap — that is lossy (the
advisory blocks are discarded), so the original wrapped rows cannot be
reconstructed.

Revision ID: 060
Revises: 059
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


# Batch size for the per-row envelope rewrite — keeps the working set bounded
# on a large conversations table without holding one giant transaction.
_BATCH = 500


def _decode_ampersand(text: str) -> str:
    """Reverse only the ``&`` → ``&amp;`` half of ``escape_wrapper_tags``.

    The original escape encoded ``&`` first, then the wrapper tags.  We
    deliberately do NOT reverse the wrapper-tag half: a stored
    ``&lt;system-reminder&gt;`` is content the old escape *neutralised* from
    attacker tool output, and turning it back into a live ``<system-reminder>``
    here would re-activate a previously-defanged injection in replayable
    history (and ``downgrade`` cannot undo it).  Decoding ``&amp;`` → ``&`` is
    safe and keeps genuine ampersands (and pre-existing ``&lt;…&gt;`` literals)
    round-tripping; the cost is purely cosmetic — a tool output that truly
    contained the literal text ``<tool_output>`` stays shown as the entity.
    """
    if "&amp;" not in text:
        return text
    return text.replace("&amp;", "&")


# The legacy envelope (``tool_advisory.wrap_tool_result``) was emitted ONLY when
# advisories were present, as ``"\n".join`` of a ``<tool_output>`` block and one
# or more ``<system-reminder>`` blocks.  The exact bytes are therefore:
#
#     <tool_output>\n{escaped output}\n</tool_output>\n\n<system-reminder>\n …
#       … \n</system-reminder>[\n\n<system-reminder>\n … \n</system-reminder>]*
#
# We match that full signature — open prefix AND the tool-output close followed
# immediately by the ``\n\n<system-reminder>\n`` join AND a trailing
# ``</system-reminder>`` — not just the ``<tool_output>`` open + close.  A bare
# tool output that merely happens to start with ``<tool_output>`` (or even one
# that contains a matching close) lacks the trailing advisory structure and is
# left untouched, so a legit row can never be mis-rewritten.
_TOOL_OPEN = "<tool_output>\n"
_ENVELOPE_JOIN = "\n</tool_output>\n\n<system-reminder>\n"
_ENVELOPE_TAIL = "\n</system-reminder>"


def _unwrap_envelope(content: str) -> str | None:
    """Return the bare tool output for a wrapped legacy envelope, else ``None``.

    Requires the complete envelope signature (see the module-level constants),
    not merely a ``<tool_output>`` open + close — the loose guard could
    irreversibly mis-rewrite a bare tool row that resembled the open.  The
    embedded ``<system-reminder>`` advisory blocks are intentionally dropped
    (documented cosmetic loss); only the bare tool output is recovered.

    The escape guarantees the body cannot contain a literal ``\\n</tool_output>``
    (it was entity-encoded), so the first ``_ENVELOPE_JOIN`` is the real close.
    """
    if not content.startswith(_TOOL_OPEN):
        return None
    join = content.find(_ENVELOPE_JOIN)
    if join == -1:
        return None
    if not content.endswith(_ENVELOPE_TAIL):
        return None
    inner = content[len(_TOOL_OPEN) : join]
    return _decode_ampersand(inner)


def upgrade() -> None:
    bind = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Integer),
        sa.column("content", sa.Text),
    )

    # (1) Un-wrap legacy ``<tool_output>`` envelopes in place.  Only rows whose
    #     content begins with the envelope open are candidates; the per-row
    #     guard rejects a literal-prefix-without-close so a legit tool output
    #     is never corrupted.  Paged so a large table stays bounded.
    last_id = 0
    while True:
        rows = bind.execute(
            sa.select(conversations.c.id, conversations.c.content)
            .where(
                sa.and_(
                    conversations.c.content.like("<tool_output>%"),
                    conversations.c.id > last_id,
                )
            )
            .order_by(conversations.c.id)
            .limit(_BATCH)
        ).fetchall()
        if not rows:
            break
        for row_id, content in rows:
            last_id = row_id
            if not isinstance(content, str):
                continue
            unwrapped = _unwrap_envelope(content)
            if unwrapped is None or unwrapped == content:
                continue
            bind.execute(
                sa.update(conversations)
                .where(conversations.c.id == row_id)
                .values(content=unwrapped)
            )

    # (2) Drop the dead ``_reminders`` column outright.  Operator context now
    #     lives in first-class ``system`` turns; nothing writes the column and
    #     ``reconstruct_messages`` no longer reads it.  Dropping it (rather than
    #     nulling and carrying it forward) removes the foot-gun of a writable
    #     dead column.  ``batch_alter_table`` so SQLite (table rebuild) and
    #     PostgreSQL (native ALTER) both work — see migration 027.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("_reminders")


def downgrade() -> None:
    # Re-add the (empty) column so the schema matches the 059 state.  The
    # envelope un-wrap (step 1) is NOT reversed — it discards the advisory
    # blocks, so the original wrapped rows cannot be reconstructed; the
    # re-added column is therefore always NULL.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("_reminders", sa.Text, nullable=True))
