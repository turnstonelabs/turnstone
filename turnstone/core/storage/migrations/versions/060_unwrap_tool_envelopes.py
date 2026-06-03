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

This migration also carries the additive front of the canonical-trajectory storage cut:
it adds the ``is_error`` column (persisting the tool-result error flag, previously an
in-memory-only message key) and tags legacy bare-list ``provider_data`` rows with their
generating provider as the ``{producer, blocks}`` envelope (producer inferred from block
types — see ``_infer_producer``, which must match the live save's ``provider_name``
values), so the lowering layer can replay the native lane verbatim only to its producer.

Finally it performs the **attachment content-addressing cutover**: after adding
``conversations.attachments`` (the ref-list) and ``workstream_attachments.refcount`` /
``origin``, it re-keys every legacy *consumed* attachment row to its content hash
(``sha256(content)``), dedups identical bytes into one refcounted blob, builds each
message's ``attachments`` ref-list from the old ``message_id`` link, and then drops the
retired upload-lifecycle columns ``message_id`` / ``reserved_for_msg_id`` /
``reserved_at`` (and their indexes).  Pending (un-consumed) legacy rows are dropped —
pending uploads now live in the per-node in-memory buffer, not in storage.

``downgrade()`` re-adds the (empty) ``_reminders`` column so the schema matches
the 059 state, but does NOT reverse the envelope un-wrap — that is lossy (the
advisory blocks are discarded), so the original wrapped rows cannot be
reconstructed.

Revision ID: 060
Revises: 059
Create Date: 2026-06-01
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

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
    """Reverse only the ``&`` → ``&amp;`` half of the old wrapper-tag escaping.

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


# Block-type → producer inference for the legacy provider_data backfill.  Must yield the
# same provider_name strings the live save writes (anthropic / google / openai /
# openai-compatible) so a backfilled row and a freshly-saved row compare equal under the
# lowering layer's ``producer == active_provider`` rule.  xAI is byte-identical to
# OpenAI-Responses in the stored blocks, so legacy xAI rows are deliberately (and
# self-healingly) tagged ``openai``.
_ANTHROPIC_BLOCK_TYPES = frozenset(
    {"thinking", "redacted_thinking", "tool_use", "server_tool_use", "web_search_tool_result"}
)
_OPENAI_RESPONSES_BLOCK_TYPES = frozenset(
    {
        "reasoning",
        "function_call",
        "web_search_call",
        "file_search_call",
        "code_interpreter_call",
        "message",
    }
)


def _infer_producer(blocks: list[Any]) -> str | None:
    """Infer the generating provider from a legacy bare ``provider_data`` block list."""
    # Google's fidelity shape: a client tool-call block carrying ``thought_signature``
    # (generic ``function`` type, so the signature field is the distinguishing signal).
    if any(
        isinstance(b, dict) and b.get("type") == "function" and "thought_signature" in b
        for b in blocks
    ):
        return "google"
    types = {b.get("type") for b in blocks if isinstance(b, dict)}
    if types & _ANTHROPIC_BLOCK_TYPES:
        return "anthropic"
    if types & _OPENAI_RESPONSES_BLOCK_TYPES:
        return "openai"
    if "reasoning_text" in types:
        return "openai-compatible"
    return None


def upgrade() -> None:
    bind = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Integer),
        sa.column("content", sa.Text),
        sa.column("provider_data", sa.Text),
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

    # (1b) Tag legacy bare-list ``provider_data`` with its producer → the
    #      ``{producer, blocks}`` envelope.  Only bare-list rows (``[%``) are
    #      candidates; already-wrapped or un-inferable rows are left as-is
    #      (reconstruct dual-reads the legacy bare-list shape).  Paged like (1).
    last_id = 0
    while True:
        rows = bind.execute(
            sa.select(conversations.c.id, conversations.c.provider_data)
            .where(
                sa.and_(
                    conversations.c.provider_data.like("[%"),
                    conversations.c.id > last_id,
                )
            )
            .order_by(conversations.c.id)
            .limit(_BATCH)
        ).fetchall()
        if not rows:
            break
        for row_id, pdata in rows:
            last_id = row_id
            if not isinstance(pdata, str):
                continue
            try:
                blocks = json.loads(pdata)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(blocks, list):
                continue
            producer = _infer_producer(blocks)
            if producer is None:
                continue
            bind.execute(
                sa.update(conversations)
                .where(conversations.c.id == row_id)
                .values(provider_data=json.dumps({"producer": producer, "blocks": blocks}))
            )

    # (2) Drop the dead ``_reminders`` column outright.  Operator context now
    #     lives in first-class ``system`` turns; nothing writes the column and
    #     ``reconstruct_messages`` no longer reads it.  Dropping it (rather than
    #     nulling and carrying it forward) removes the foot-gun of a writable
    #     dead column.  ``batch_alter_table`` so SQLite (table rebuild) and
    #     PostgreSQL (native ALTER) both work — see migration 027.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("_reminders")
        # Persist the tool-result error flag (was an in-memory-only message key);
        # existing rows backfill to False via the server_default.
        batch_op.add_column(
            sa.Column("is_error", sa.Boolean, nullable=False, server_default=sa.false())
        )
        # Content-addressed attachment ref-list (canonical-trajectory cut); the
        # backfill below (step 3) fills it from the legacy message_id link and
        # then drops message_id/reserved_* (step 4).
        batch_op.add_column(sa.Column("attachments", sa.Text, nullable=True))
    with op.batch_alter_table("workstream_attachments") as batch_op:
        batch_op.add_column(
            sa.Column("refcount", sa.Integer, nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("origin", sa.Text, nullable=False, server_default=sa.text("'upload'"))
        )

    # (3) Backfill the content-addressed model from the legacy message_id link,
    #     then drop the retired lifecycle columns.  Must run AFTER the additive
    #     columns above exist (refcount / origin / attachments) and BEFORE the
    #     drop below (it reads message_id).
    _backfill_content_addressed_attachments(bind)

    # (4) Drop the retired upload-lifecycle columns + their indexes.  The
    #     content-addressed model keys blobs by content hash and links them via
    #     the conversations.attachments ref-list, so message_id /
    #     reserved_for_msg_id / reserved_at (and the indexes over them) are dead.
    #     Drop the dependent indexes FIRST on both dialects: SQLite's
    #     ``batch_alter_table`` rebuilds the table from the reflected schema and
    #     would otherwise try to re-create these indexes against the
    #     now-missing columns; PostgreSQL needs them gone before the columns.
    for idx in (
        "idx_ws_attachments_pending",
        "idx_ws_attachments_message",
        "idx_ws_attachments_reserved",
        "idx_ws_attachments_reserved_at",
    ):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {idx}"))
    with op.batch_alter_table("workstream_attachments") as batch_op:
        batch_op.drop_column("message_id")
        batch_op.drop_column("reserved_for_msg_id")
        batch_op.drop_column("reserved_at")


def _backfill_content_addressed_attachments(bind: sa.engine.Connection) -> None:
    """Re-key legacy consumed attachments to their content hash + build ref-lists.

    Legacy rows linked an attachment to a message via
    ``workstream_attachments.message_id``.  The content-addressed model keys a
    blob by ``sha256(content)`` and links it via the ordered
    ``conversations.attachments`` ref-list.  For every *consumed* legacy row
    (``message_id IS NOT NULL``):

    * compute the content hash and dedup — identical bytes collapse to one row
      whose PK is re-keyed to the hash; duplicate legacy rows are deleted;
    * set ``refcount`` = the number of distinct messages referencing that
      content, and ``origin = 'upload'``;
    * build each referencing message's ``conversations.attachments`` as the
      ordered list of content hashes (legacy per-message order preserved by the
      attachment row's ``created`` then ``attachment_id``).

    Pending (un-consumed) legacy rows (``message_id IS NULL``) are dropped: they
    were transient upload state and the content-addressed model holds no pending
    blobs in storage (they live in the per-node buffer now).
    """
    wa = sa.table(
        "workstream_attachments",
        sa.column("attachment_id", sa.Text),
        sa.column("message_id", sa.Integer),
        sa.column("content", sa.LargeBinary),
        sa.column("created", sa.Text),
        sa.column("refcount", sa.Integer),
        sa.column("origin", sa.Text),
    )
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Integer),
        sa.column("attachments", sa.Text),
    )

    # Read every consumed legacy row in (message_id, created, attachment_id)
    # order so each message's ref-list preserves the original attachment order.
    rows = bind.execute(
        sa.select(wa.c.attachment_id, wa.c.message_id, wa.c.content)
        .where(wa.c.message_id.is_not(None))
        .order_by(wa.c.message_id, wa.c.created, wa.c.attachment_id)
    ).fetchall()

    # new_id (content hash) -> canonical old id kept as that blob's row.
    canonical_old_id: dict[str, str] = {}
    # new_id -> set of distinct message ids referencing it (refcount source).
    refcounting: dict[str, set[int]] = {}
    # message_id -> ordered list of new_ids (de-duped within the message).
    per_message: dict[int, list[str]] = {}
    # old ids to delete (duplicates that collapsed into a canonical row).
    drop_old_ids: list[str] = []

    for old_id, message_id, content in rows:
        raw = content if isinstance(content, (bytes, bytearray)) else b""
        new_id = hashlib.sha256(bytes(raw)).hexdigest()
        if new_id not in canonical_old_id:
            canonical_old_id[new_id] = old_id
            refcounting[new_id] = set()
        elif old_id != canonical_old_id[new_id]:
            # A distinct legacy row carrying identical bytes — collapse it.
            drop_old_ids.append(old_id)
        refcounting[new_id].add(int(message_id))
        bucket = per_message.setdefault(int(message_id), [])
        if new_id not in bucket:
            bucket.append(new_id)

    # Re-key each canonical row's PK to its content hash and set refcount/origin.
    # Re-key first (while the duplicates still hold their old PKs), then delete
    # the duplicates, so a re-key can't collide with a not-yet-deleted dup.
    for new_id, old_id in canonical_old_id.items():
        bind.execute(
            sa.update(wa)
            .where(wa.c.attachment_id == old_id)
            .values(
                attachment_id=new_id,
                refcount=len(refcounting[new_id]),
                origin="upload",
            )
        )
    for old_id in drop_old_ids:
        bind.execute(sa.delete(wa).where(wa.c.attachment_id == old_id))

    # Drop any remaining pending (un-consumed) legacy rows — no storage home.
    bind.execute(sa.delete(wa).where(wa.c.message_id.is_(None)))

    # Write each message's content-addressed ref-list.
    for message_id, new_ids in per_message.items():
        bind.execute(
            sa.update(conversations)
            .where(conversations.c.id == message_id)
            .values(attachments=json.dumps(new_ids))
        )


def downgrade() -> None:
    # Re-add the (empty) column so the schema matches the 059 state.  The
    # envelope un-wrap (step 1) is NOT reversed — it discards the advisory
    # blocks, so the original wrapped rows cannot be reconstructed; the
    # re-added column is therefore always NULL.  The content-addressing
    # backfill (step 3) is likewise NOT reversed: the retired columns are
    # re-added empty (the old message_id links / reservation tokens cannot be
    # reconstructed from the content-addressed ref-list).
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("_reminders", sa.Text, nullable=True))
        batch_op.drop_column("is_error")
        batch_op.drop_column("attachments")
    with op.batch_alter_table("workstream_attachments") as batch_op:
        batch_op.add_column(sa.Column("message_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("reserved_for_msg_id", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("reserved_at", sa.Text, nullable=True))
        batch_op.drop_column("refcount")
        batch_op.drop_column("origin")
    # Re-create the indexes over the re-added columns to match the 059 schema.
    op.create_index(
        "idx_ws_attachments_pending",
        "workstream_attachments",
        ["ws_id", "user_id", "message_id"],
    )
    op.create_index(
        "idx_ws_attachments_message",
        "workstream_attachments",
        ["message_id"],
    )
    op.create_index(
        "idx_ws_attachments_reserved",
        "workstream_attachments",
        ["ws_id", "user_id", "reserved_for_msg_id"],
    )
