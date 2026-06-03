"""Tests for alembic migration 060 (un-wrap legacy tool-output envelopes).

Drives ``command.upgrade`` from a programmatic Alembic config against an
isolated SQLite database per test, then asserts:

* a wrapped ``<tool_output>`` envelope row is rewritten to the bare tool
  output, dropping the embedded ``<system-reminder>`` advisory blocks;
* only ``&amp;`` → ``&`` is reversed — wrapper-tag entities stay escaped so a
  previously-defanged injection is not re-activated (sec-2);
* the tightened structural guard requires the *full* envelope signature, so a
  bare row that merely starts with ``<tool_output>`` — or even one with a
  matching ``</tool_output>`` close but no advisory — is left untouched (the
  known-issue #1 false positive);
* the dead ``_reminders`` side-channel column is dropped outright (not nulled
  and carried forward as a writable foot-gun);
* the migration is idempotent (a second run is a no-op);
* a plain non-envelope row is untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_row(conn: sa.Connection, **cols: object) -> None:
    defaults: dict[str, object] = {
        "ws_id": "ws1",
        "timestamp": "2026-06-01T00:00:00",
        "role": "tool",
        "content": None,
        "tool_name": None,
        "tool_call_id": None,
        "provider_data": None,
        "tool_calls": None,
        "_source": None,
        "_reminders": None,
    }
    defaults.update(cols)
    keys = ", ".join(defaults)
    binds = ", ".join(f":{k}" for k in defaults)
    conn.execute(sa.text(f"INSERT INTO conversations ({keys}) VALUES ({binds})"), defaults)


def _seed_attachment(conn: sa.Connection, **cols: object) -> None:
    """Insert a legacy ``workstream_attachments`` row at the 059 schema.

    Columns at 059: attachment_id, ws_id, user_id, filename, mime_type,
    size_bytes, kind, content, message_id, reserved_for_msg_id, reserved_at,
    created (no refcount / origin — those land in 060).
    """
    defaults: dict[str, object] = {
        "attachment_id": "att1",
        "ws_id": "ws1",
        "user_id": "u1",
        "filename": "f.txt",
        "mime_type": "text/plain",
        "size_bytes": 0,
        "kind": "text",
        "content": b"",
        "message_id": None,
        "reserved_for_msg_id": None,
        "reserved_at": None,
        "created": "2026-06-01T00:00:00",
    }
    defaults.update(cols)
    keys = ", ".join(defaults)
    binds = ", ".join(f":{k}" for k in defaults)
    conn.execute(sa.text(f"INSERT INTO workstream_attachments ({keys}) VALUES ({binds})"), defaults)


# A wrapped envelope exactly as ``wrap_tool_result`` produced it: the
# ``<tool_output>`` block, then ``"\n".join`` with a part that itself begins
# with ``\n<system-reminder>`` — yielding the ``</tool_output>\n\n<system-
# reminder>`` double-newline join the tightened guard requires.
_WRAPPED = (
    "<tool_output>\nclean tool output\n</tool_output>\n\n"
    "<system-reminder>\nThe user sent a message. User message: check logs\n</system-reminder>"
)


class TestMigration060:
    def test_unwraps_envelope_and_drops_advisories(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-unwrap.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=_WRAPPED, tool_call_id="call_a")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_a'")
                ).scalar_one()
            # Envelope stripped to the bare inner output; the advisory block
            # is gone (cosmetic loss accepted by the design).
            assert content == "clean tool output"
            assert "<tool_output>" not in content
            assert "<system-reminder>" not in content
        finally:
            engine.dispose()

    def test_provider_data_producer_backfill(self, tmp_path: Path) -> None:
        """Legacy bare-list provider_data is tagged {producer, blocks} by inferred provider.

        The inferred producer strings must match the live save's provider_name values
        (anthropic / google / openai / openai-compatible); un-inferable rows stay bare.
        """
        db_path = tmp_path / "060-producer.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")
        cases = {
            "a-anthropic": ([{"type": "thinking", "thinking": "t"}], "anthropic"),
            "a-google": (
                [{"type": "function", "function": {"name": "x"}, "thought_signature": "ts"}],
                "google",
            ),
            "a-openai": ([{"type": "reasoning", "summary": []}], "openai"),
            "a-chat": ([{"type": "reasoning_text", "text": "r"}], "openai-compatible"),
        }
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                for tcid, (blocks, _) in cases.items():
                    _seed_row(
                        conn,
                        role="assistant",
                        content="x",
                        tool_call_id=tcid,
                        provider_data=json.dumps(blocks),
                    )
                _seed_row(
                    conn,
                    role="assistant",
                    content="x",
                    tool_call_id="a-unknown",
                    provider_data=json.dumps([{"type": "mystery"}]),
                )

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                for tcid, (blocks, producer) in cases.items():
                    pd = conn.execute(
                        sa.text("SELECT provider_data FROM conversations WHERE tool_call_id = :t"),
                        {"t": tcid},
                    ).scalar_one()
                    assert json.loads(pd) == {"producer": producer, "blocks": blocks}
                # Un-inferable blocks are left bare (reconstruct dual-reads the legacy shape).
                unknown = conn.execute(
                    sa.text(
                        "SELECT provider_data FROM conversations WHERE tool_call_id = 'a-unknown'"
                    )
                ).scalar_one()
                assert json.loads(unknown) == [{"type": "mystery"}]
        finally:
            engine.dispose()

    def test_is_error_column_added_and_backfilled_false(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-iserr.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, role="tool", content="boom", tool_call_id="e1")
            command.upgrade(cfg, "060")
            with engine.connect() as conn:
                # Existing rows backfill to False via the server_default.
                val = conn.execute(
                    sa.text("SELECT is_error FROM conversations WHERE tool_call_id = 'e1'")
                ).scalar_one()
            assert not val
        finally:
            engine.dispose()

    def test_content_addressed_attachment_columns_added_and_lifecycle_dropped(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "060-ca-cols.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "060")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            insp = sa.inspect(engine)
            conv_cols = {c["name"] for c in insp.get_columns("conversations")}
            att_cols = {c["name"] for c in insp.get_columns("workstream_attachments")}
            # Added: the ref-list + the refcounted-blob columns.
            assert "attachments" in conv_cols
            assert {"refcount", "origin"} <= att_cols
            # Dropped: the retired upload-lifecycle columns.
            assert "message_id" not in att_cols
            assert "reserved_for_msg_id" not in att_cols
            assert "reserved_at" not in att_cols
            # Dropped: their indexes.
            idx_names = {i["name"] for i in insp.get_indexes("workstream_attachments")}
            assert "idx_ws_attachments_message" not in idx_names
            assert "idx_ws_attachments_pending" not in idx_names
            assert "idx_ws_attachments_reserved" not in idx_names
            assert "idx_ws_attachments_reserved_at" not in idx_names
        finally:
            engine.dispose()

    def test_ampersand_decoded_but_wrapper_tags_left_escaped(self, tmp_path: Path) -> None:
        """The un-wrap reverses only ``&amp;`` → ``&``.  Wrapper-tag entities are
        left escaped on purpose: re-activating ``&lt;system-reminder&gt;`` into a
        live tag would un-defang injection the old escape had neutralised."""
        db_path = tmp_path / "060-decode.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        # The old wrapper-tag escaping of ``see <tool_output> & <system-reminder>``
        # encoded ``&amp;`` first, then the tags.
        inner_escaped = "see &lt;tool_output&gt; &amp; &lt;system-reminder&gt;"
        wrapped = (
            f"<tool_output>\n{inner_escaped}\n</tool_output>\n\n"
            "<system-reminder>\nThe user sent a message. User message: x\n</system-reminder>"
        )

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=wrapped, tool_call_id="call_b")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_b'")
                ).scalar_one()
            # ``&amp;`` → ``&`` only; the wrapper-tag entities stay escaped.
            assert content == "see &lt;tool_output&gt; & &lt;system-reminder&gt;"
            assert "<system-reminder>" not in content
        finally:
            engine.dispose()

    def test_literal_prefix_without_close_untouched(self, tmp_path: Path) -> None:
        """A tool output that merely STARTS with a literal ``<tool_output>``
        line but has no matching close is not an envelope — left untouched."""
        db_path = tmp_path / "060-prefix.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        unmatched = "<tool_output>\nthis tool printed the open tag but never closed it"

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=unmatched, tool_call_id="call_c")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_c'")
                ).scalar_one()
            assert content == unmatched
        finally:
            engine.dispose()

    def test_tool_output_open_close_without_advisory_untouched(self, tmp_path: Path) -> None:
        """The known-issue #1 false positive: a bare tool output that genuinely
        starts with ``<tool_output>`` AND has a matching ``</tool_output>`` close
        but NO trailing ``<system-reminder>`` advisory is NOT a legacy envelope
        (those were only emitted with advisories).  The tightened guard leaves
        it byte-for-byte untouched — the loose open+close guard would have
        irreversibly mis-rewritten it to its inner text."""
        db_path = tmp_path / "060-noadvisory.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        # e.g. a tool that printed XML, or this project's own source/docs.
        bare = "<tool_output>\nls -la output here\n</tool_output>\nplus a trailing line"

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=bare, tool_call_id="call_g")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_g'")
                ).scalar_one()
            assert content == bare
        finally:
            engine.dispose()

    def test_missing_trailing_system_reminder_close_untouched(self, tmp_path: Path) -> None:
        """An open + join that lacks the trailing ``</system-reminder>`` close is
        not a complete envelope — left untouched rather than half-rewritten."""
        db_path = tmp_path / "060-notail.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        truncated = "<tool_output>\nx\n</tool_output>\n\n<system-reminder>\nno close here"

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=truncated, tool_call_id="call_h")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_h'")
                ).scalar_one()
            assert content == truncated
        finally:
            engine.dispose()

    def test_drops_reminders_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-reminders.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                # The column still exists at 059, so a legacy value can be seeded.
                _seed_row(
                    conn,
                    role="user",
                    content="hello",
                    tool_call_id="call_d",
                    _reminders='[{"type":"correction","text":"watch it"}]',
                )
            # At 059 the column is present.
            assert "_reminders" in {
                c["name"] for c in sa.inspect(engine).get_columns("conversations")
            }

            command.upgrade(cfg, "060")

            # 060 drops it outright (no dead column carried forward); the row
            # itself survives.
            cols = {c["name"] for c in sa.inspect(engine).get_columns("conversations")}
            assert "_reminders" not in cols
            assert "_source" in cols  # the live sibling stays
            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_d'")
                ).scalar_one()
            assert content == "hello"
        finally:
            engine.dispose()

    def test_non_envelope_row_untouched(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-plain.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content="just a normal tool result", tool_call_id="call_e")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_e'")
                ).scalar_one()
            assert content == "just a normal tool result"
        finally:
            engine.dispose()

    def test_idempotent(self, tmp_path: Path) -> None:
        """A second pass over the now-clean rows is a no-op.

        Alembic won't re-run a stamped revision, so the rewrite's
        stability is asserted directly on the migration's ``_unwrap_envelope``
        guard: a bare (already-unwrapped) output is not an envelope, so a
        second pass leaves it alone.
        """
        db_path = tmp_path / "060-idem.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=_WRAPPED, tool_call_id="call_f")

            command.upgrade(cfg, "060")
            with engine.connect() as conn:
                first = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_f'")
                ).scalar_one()
        finally:
            engine.dispose()

        # The migration module's filename (``060_...``) isn't a valid import
        # identifier, so load it by path to reuse its guard.
        import importlib.util

        mig_path = (
            Path(__file__).resolve().parent.parent
            / "turnstone"
            / "core"
            / "storage"
            / "migrations"
            / "versions"
            / "060_unwrap_tool_envelopes.py"
        )
        spec = importlib.util.spec_from_file_location("_mig_060", mig_path)
        assert spec is not None and spec.loader is not None
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        # Already-clean content is not an envelope → second pass is a no-op.
        assert mig._unwrap_envelope(first) is None


class TestMigration060AttachmentBackfill:
    """The content-addressing cutover backfill: re-key legacy consumed
    attachment rows to their content hash, dedup identical bytes into one
    refcounted blob, and build each message's ``conversations.attachments``
    ref-list from the old ``message_id`` link."""

    def test_rehash_reflist_and_refcount(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-att-backfill.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        content = b"hello world"
        new_id = hashlib.sha256(content).hexdigest()
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                # A user message and its consumed attachment (legacy uuid id).
                _seed_row(conn, role="user", content="see file", tool_call_id="m1")
                msg_id = conn.execute(
                    sa.text("SELECT id FROM conversations WHERE tool_call_id = 'm1'")
                ).scalar_one()
                _seed_attachment(
                    conn,
                    attachment_id="legacy-uuid-1",
                    content=content,
                    size_bytes=len(content),
                    message_id=msg_id,
                )

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                # The blob row is re-keyed to the content hash, refcount=1.
                row = conn.execute(
                    sa.text("SELECT attachment_id, refcount, origin FROM workstream_attachments")
                ).fetchall()
                assert len(row) == 1
                assert row[0][0] == new_id
                assert row[0][1] == 1
                assert row[0][2] == "upload"
                # The message's ref-list names the content hash.
                refs = conn.execute(
                    sa.text("SELECT attachments FROM conversations WHERE id = :i"),
                    {"i": msg_id},
                ).scalar_one()
                assert json.loads(refs) == [new_id]
        finally:
            engine.dispose()

    def test_dedup_identical_bytes_across_messages(self, tmp_path: Path) -> None:
        """Two messages whose attachments carry identical bytes collapse to one
        refcounted blob (refcount = 2); both messages reference the same hash."""
        db_path = tmp_path / "060-att-dedup.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        content = b"shared bytes"
        new_id = hashlib.sha256(content).hexdigest()
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, role="user", content="m one", tool_call_id="ma")
                _seed_row(conn, role="user", content="m two", tool_call_id="mb")
                ma = conn.execute(
                    sa.text("SELECT id FROM conversations WHERE tool_call_id = 'ma'")
                ).scalar_one()
                mb = conn.execute(
                    sa.text("SELECT id FROM conversations WHERE tool_call_id = 'mb'")
                ).scalar_one()
                _seed_attachment(
                    conn, attachment_id="uuid-a", content=content, size_bytes=12, message_id=ma
                )
                _seed_attachment(
                    conn, attachment_id="uuid-b", content=content, size_bytes=12, message_id=mb
                )

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text("SELECT attachment_id, refcount FROM workstream_attachments")
                ).fetchall()
                # Deduped to one blob, referenced by two messages.
                assert len(rows) == 1
                assert rows[0][0] == new_id
                assert rows[0][1] == 2
                for mid in (ma, mb):
                    refs = conn.execute(
                        sa.text("SELECT attachments FROM conversations WHERE id = :i"),
                        {"i": mid},
                    ).scalar_one()
                    assert json.loads(refs) == [new_id]
        finally:
            engine.dispose()

    def test_pending_legacy_rows_dropped(self, tmp_path: Path) -> None:
        """Pending (un-consumed, message_id IS NULL) legacy rows have no home in
        the content-addressed store and are dropped by the backfill."""
        db_path = tmp_path / "060-att-pending.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_attachment(
                    conn, attachment_id="pending-1", content=b"x", size_bytes=1, message_id=None
                )
            command.upgrade(cfg, "060")
            with engine.connect() as conn:
                n = conn.execute(
                    sa.text("SELECT COUNT(*) FROM workstream_attachments")
                ).scalar_one()
            assert n == 0
        finally:
            engine.dispose()

    def test_multiple_attachments_on_one_message_ordered(self, tmp_path: Path) -> None:
        """A message with two distinct attachments gets both content hashes in
        its ref-list, ordered by the legacy row's (created, attachment_id)."""
        db_path = tmp_path / "060-att-multi.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        c1, c2 = b"first", b"second"
        h1, h2 = hashlib.sha256(c1).hexdigest(), hashlib.sha256(c2).hexdigest()
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, role="user", content="two files", tool_call_id="mm")
                mm = conn.execute(
                    sa.text("SELECT id FROM conversations WHERE tool_call_id = 'mm'")
                ).scalar_one()
                _seed_attachment(
                    conn,
                    attachment_id="uuid-1",
                    content=c1,
                    size_bytes=5,
                    message_id=mm,
                    created="2026-06-01T00:00:01",
                )
                _seed_attachment(
                    conn,
                    attachment_id="uuid-2",
                    content=c2,
                    size_bytes=6,
                    message_id=mm,
                    created="2026-06-01T00:00:02",
                )

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                refs = conn.execute(
                    sa.text("SELECT attachments FROM conversations WHERE id = :i"), {"i": mm}
                ).scalar_one()
            assert json.loads(refs) == [h1, h2]
        finally:
            engine.dispose()
