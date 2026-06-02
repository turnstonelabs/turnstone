"""Un-wrap legacy ``<tool_output>`` advisory envelopes; null ``_reminders``.

Operator-context (output-guard findings, user interjections, metacognitive
nudges) used to ride two transient carriers baked into stored rows: a
``<tool_output>`` / ``<system-reminder>`` envelope spliced into tool-message
``content`` (``tool_advisory.wrap_tool_result``), and a JSON ``_reminders``
side-channel column.  Both are replaced by first-class ``{"role":"system"}``
turns, so the legacy carriers are now dead on the read path.

This migration drains the carriers in place — UPDATE only, no row insertion:

* **Envelopes** — every tool row whose ``content`` is a wrapped envelope is
  rewritten to the bare (entity-decoded) tool output.  The embedded
  ``<system-reminder>`` advisories are intentionally dropped (cosmetic loss of
  historical nudge / interjection bubbles — the design accepts this rather than
  resurrecting them as new rows).  The structural-match guard (open prefix AND a
  matching close) is copied inline from the now-deleted
  ``history_decoration.extract_advisories_from_tool_envelope`` so a tool output
  that merely *starts with* a literal ``<tool_output>`` line — with no matching
  close — is left untouched.
* **Reminders** — ``conversations._reminders`` is nulled wholesale; nothing
  writes the column anymore and the read path no longer projects it.

``downgrade()`` is a documented no-op: the un-wrap is lossy (the advisory
blocks and the reminder JSON are discarded), so the original rows cannot be
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


def _entity_decode_wrapper_tags(text: str) -> str:
    """Reverse ``tool_advisory.escape_wrapper_tags`` (copied inline).

    Decodes ``&amp;`` last so a body containing the literal string
    ``&lt;tool_output&gt;`` round-trips identically to its source.
    """
    if "&" not in text:
        return text
    return (
        text.replace("&lt;/tool_output&gt;", "</tool_output>")
        .replace("&lt;tool_output&gt;", "<tool_output>")
        .replace("&lt;system-reminder&gt;", "<system-reminder>")
        .replace("&lt;/system-reminder&gt;", "</system-reminder>")
        .replace("&amp;", "&")
    )


def _unwrap_envelope(content: str) -> str | None:
    """Return the bare tool output for a wrapped envelope, else ``None``.

    Structural-match guard (copied inline from the deleted
    ``extract_advisories_from_tool_envelope``): the content must start with
    the exact ``<tool_output>\\n`` open AND contain a matching
    ``\\n</tool_output>`` close.  A tool output that merely starts with a
    literal ``<tool_output>`` line but has no matching close is NOT an
    envelope — return ``None`` so the caller leaves it untouched.
    """
    if not content.startswith("<tool_output>\n"):
        return None
    close = content.find("\n</tool_output>")
    if close == -1:
        return None
    inner = content[len("<tool_output>\n") : close]
    return _entity_decode_wrapper_tags(inner)


def upgrade() -> None:
    bind = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Integer),
        sa.column("content", sa.Text),
        sa.column("_reminders", sa.Text),
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

    # (2) Null the dead ``_reminders`` side-channel column wholesale.
    bind.execute(
        sa.update(conversations)
        .where(conversations.c._reminders.isnot(None))
        .values(_reminders=None)
    )


def downgrade() -> None:
    # No-op: the un-wrap discards the advisory blocks and the reminder JSON,
    # so the original wrapped rows cannot be reconstructed.
    pass
