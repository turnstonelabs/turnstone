"""Tests for the SQLite storage backend."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from tests._storage_fakes import acquire_lease as _acquire
from tests._storage_fakes import expire_lease as _expire_lease
from turnstone.core.storage._protocol import WorkstreamLeaseLostError
from turnstone.core.storage._schema import workstreams

# -- Workstream registration ---------------------------------------------------


class TestRegisterWorkstream:
    def test_register_creates_workstream(self, backend):
        backend.register_workstream("s1", title="Test")
        name = backend.get_workstream_display_name("s1")
        assert name == "Test"

    def test_register_idempotent(self, backend):
        backend.register_workstream("s1", title="First")
        backend.register_workstream("s1", title="Second")
        name = backend.get_workstream_display_name("s1")
        assert name == "First"  # INSERT OR IGNORE preserves first


class TestSaveAndLoadMessages:
    def test_roundtrip(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello")
        backend.save_message("s1", "assistant", "world")
        msgs = backend.load_messages("s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "world"

    def test_system_turn_meta_roundtrip(self, backend):
        # The structured per-kind operator meta saved on a ``system`` row
        # round-trips back as ``_source_meta`` through the real insert + SELECT
        # (the ``conversations.meta`` column, migration 060).
        import json

        backend.register_workstream("s1")
        backend.save_message("s1", "user", "go")
        backend.save_message(
            "s1",
            "system",
            "ci failed",
            source="watch_triggered",
            meta=json.dumps({"watch_name": "ci", "command": "make test", "poll_count": 3}),
        )
        msgs = backend.load_messages("s1")
        assert msgs[1] == {
            "role": "system",
            "content": "ci failed",
            "_source": "watch_triggered",
            "_source_meta": {"watch_name": "ci", "command": "make test", "poll_count": 3},
        }

    def test_ordinary_row_has_null_meta(self, backend):
        # A non-operator row carries no meta — no ``_source_meta`` on reload.
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello")
        msgs = backend.load_messages("s1")
        assert "_source_meta" not in msgs[0]

    def test_tool_call_grouping(self, backend):
        import json

        backend.register_workstream("s1")
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                }
            ]
        )
        backend.save_message("s1", "user", "do something")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        backend.save_message("s1", "tool", "file.txt", tool_call_id="c1")
        backend.save_message("s1", "assistant", "done")
        msgs = backend.load_messages("s1")
        assert len(msgs) == 4
        assert msgs[1]["role"] == "assistant"
        assert len(msgs[1]["tool_calls"]) == 1
        assert msgs[1]["tool_calls"][0]["id"] == "c1"
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["content"] == "file.txt"

    def test_incomplete_turn_repair(self, backend):
        import json

        backend.register_workstream("s1")
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                },
            ]
        )
        backend.save_message("s1", "user", "do something")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        # Only 1 result for 2 calls — incomplete turn
        backend.save_message("s1", "tool", "ok", tool_call_id="c1")
        msgs = backend.load_messages("s1")
        # Incomplete turn should be stripped
        assert len(msgs) == 1  # only the user message remains

    def test_provider_data_preserved(self, backend):
        import json

        backend.register_workstream("s1")
        # The native lane is a block list (or the {producer, blocks} envelope);
        # it round-trips verbatim through _provider_content.
        blocks = [{"type": "thinking", "thinking": "secret", "signature": "s"}]
        backend.save_message("s1", "assistant", "hi", provider_data=json.dumps(blocks))
        msgs = backend.load_messages("s1")
        assert msgs[0].get("_provider_content") == blocks

    def test_empty_workstream_returns_empty(self, backend):
        assert backend.load_messages("nonexistent") == []


class TestListMessageSenders:
    def test_distinct_senders_from_user_rows_only(self, backend):
        import json

        backend.register_workstream("s1")
        backend.save_message("s1", "user", "a", meta=json.dumps({"sender": "alice"}))
        backend.save_message("s1", "user", "b", meta=json.dumps({"sender": "bob"}))
        backend.save_message("s1", "user", "c", meta=json.dumps({"sender": "alice"}))
        backend.save_message("s1", "user", "plain")  # unstamped: meta is NULL
        # A system row's meta rides the source_meta channel; even a stray
        # "sender" key there must never count as a participant.
        backend.save_message(
            "s1", "system", "note", source="watch_triggered", meta=json.dumps({"sender": "evil"})
        )
        backend.register_workstream("s2")
        backend.save_message("s2", "user", "x", meta=json.dumps({"sender": "carol"}))
        assert backend.list_message_senders("s1") == ["alice", "bob"]
        assert backend.list_message_senders("s2") == ["carol"]  # ws-scoped
        assert backend.list_message_senders("nope") == []

    def test_garbage_meta_is_skipped(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "a", meta="not json{")
        backend.save_message("s1", "user", "b", meta='"just a string"')
        backend.save_message("s1", "user", "c", meta='{"sender": "   "}')
        backend.save_message("s1", "user", "d", meta='{"sender": 7}')
        assert backend.list_message_senders("s1") == []


class TestLoadMessagesLimit:
    """Phase 3 added ``limit=N`` so cluster-inspect can avoid reading
    thousands of rows to return a tail-20 preview.  The contract: fetch
    the last N conversation rows (DESC + LIMIT at the SQL layer), then
    reverse into chronological order for reconstruction.  Approximate
    tail-N — a tool-call group straddling the cut produces an
    incomplete turn that the existing repair step strips."""

    def test_limit_none_fetches_all(self, backend):
        backend.register_workstream("s1")
        for i in range(10):
            backend.save_message("s1", "user", f"msg-{i}")
        msgs = backend.load_messages("s1", limit=None)
        assert len(msgs) == 10

    def test_limit_fetches_tail_in_chronological_order(self, backend):
        backend.register_workstream("s1")
        for i in range(10):
            backend.save_message("s1", "user", f"msg-{i:02d}")
        msgs = backend.load_messages("s1", limit=3)
        assert len(msgs) == 3
        # Chronological order preserved even though SQL fetched DESC.
        assert msgs[0]["content"] == "msg-07"
        assert msgs[1]["content"] == "msg-08"
        assert msgs[2]["content"] == "msg-09"

    def test_limit_exceeds_total_returns_all(self, backend):
        backend.register_workstream("s1")
        for i in range(5):
            backend.save_message("s1", "user", f"msg-{i}")
        msgs = backend.load_messages("s1", limit=100)
        assert len(msgs) == 5

    def test_limit_zero_fetches_all(self, backend):
        """limit<=0 matches the ``None`` branch — the SQL LIMIT is
        skipped, full history returned.  Belt-and-suspenders against
        callers that pass the clamped ``max(0, limit)`` result."""
        backend.register_workstream("s1")
        for i in range(5):
            backend.save_message("s1", "user", f"msg-{i}")
        assert len(backend.load_messages("s1", limit=0)) == 5

    def test_limit_boundary_straddles_tool_call_group(self, backend):
        """Document the approximate-tail-N semantics the ``load_messages``
        docstring warns about: when the tail slice opens mid-tool-call-
        group, the orphaned ``role=tool`` row is returned verbatim
        (the incomplete-turn repair at ``_reconstruct_messages`` only
        strips incomplete *assistant-with-tool_calls* groups, not
        orphaned tool-response rows).

        Callers that need strict tail-N semantics (e.g. re-hydrating a
        session to resume generation) must either request more than
        they need and post-filter, or do a full load.  The cluster-
        inspect preview path tolerates orphan tool rows because the
        UI renders them as standalone tool-output blocks.

        Seed: [user, assistant w/ 1 tool_call, tool result, assistant].
        Fetch tail=2 → [tool result, assistant].  Orphan tool row
        survives; this is expected behavior, not a bug."""
        import json

        backend.register_workstream("s1")
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                }
            ]
        )
        backend.save_message("s1", "user", "do it")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        backend.save_message("s1", "tool", "output", tool_call_id="c1")
        backend.save_message("s1", "assistant", "done")

        # Full load: 4 messages (complete turn, assistant reply).
        assert len(backend.load_messages("s1")) == 4

        # Tail=2: orphan tool row + final assistant reply.
        tail = backend.load_messages("s1", limit=2)
        assert len(tail) == 2
        assert tail[0]["role"] == "tool"
        assert tail[0]["content"] == "output"
        assert tail[1]["role"] == "assistant"
        assert tail[1]["content"] == "done"

    def test_limit_keeps_complete_tool_call_group_when_fully_contained(self, backend):
        """Tool-call groups entirely inside the tail slice survive intact."""
        import json

        backend.register_workstream("s1")
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"p":"a"}'},
                }
            ]
        )
        backend.save_message("s1", "user", "older message")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        backend.save_message("s1", "tool", "contents", tool_call_id="c1")
        backend.save_message("s1", "assistant", "summarized")

        # Tail=3 captures the full group + assistant reply (drops
        # only the oldest user message).
        tail = backend.load_messages("s1", limit=3)
        assert len(tail) == 3
        assert tail[0]["role"] == "assistant"
        assert len(tail[0]["tool_calls"]) == 1
        assert tail[1]["role"] == "tool"
        assert tail[1]["content"] == "contents"
        assert tail[2]["content"] == "summarized"

    def test_limit_bounds_attachment_scan(self, backend):
        """The content-addressed attachment resolution only fetches blobs the
        *fetched* rows reference — so a tail-N load that doesn't include the
        attachment-bearing row issues no blob fetch at all, and a full load
        fetches exactly the referenced ids.  This keeps the tail-N conversations
        LIMIT from being undone by a full-workstream attachment scan."""
        from unittest.mock import patch

        backend.register_workstream("s1")
        # Oldest row carries the attachment; then 20 plain rows after it.
        aid = "a" * 64
        att_msg_id = backend.save_message("s1", "user", "see attachment")
        backend.save_attachment(aid, "x.txt", "text/plain", 1, "text", b"x")
        backend.set_message_attachments("s1", att_msg_id, [aid])
        for i in range(20):
            backend.save_message("s1", "user", f"msg-{i:02d}")

        captured: list[list[str]] = []
        orig = backend.get_attachments

        def _spy(ids, exclude_kinds=()):
            captured.append(sorted(ids))
            return orig(ids, exclude_kinds=exclude_kinds)

        # Tail-N=5 fetches only the 5 newest rows (all plain) — the
        # attachment row is excluded, so NO blob fetch is issued.
        with patch.object(backend, "get_attachments", side_effect=_spy):
            backend.load_messages("s1", limit=5)
        assert captured == []

        # Full load resolves exactly the one referenced id (not a full scan).
        captured.clear()
        with patch.object(backend, "get_attachments", side_effect=_spy):
            backend.load_messages("s1")
        assert captured == [[aid]]


class TestSaveMessagesBulk:
    def test_bulk_roundtrip(self, backend):
        backend.register_workstream("s1")
        backend.save_messages_bulk(
            [
                {"ws_id": "s1", "role": "user", "content": "hello"},
                {"ws_id": "s1", "role": "assistant", "content": "hi there"},
                {"ws_id": "s1", "role": "user", "content": "bye"},
            ]
        )
        msgs = backend.load_messages("s1")
        assert len(msgs) == 3
        assert msgs[0]["content"] == "hello"
        assert msgs[2]["content"] == "bye"

    def test_bulk_preserves_tool_calls(self, backend):
        import json

        backend.register_workstream("s1")
        tc = json.dumps(
            [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]
        )
        backend.save_messages_bulk(
            [
                {"ws_id": "s1", "role": "user", "content": "do it"},
                {"ws_id": "s1", "role": "assistant", "content": None, "tool_calls": tc},
                {"ws_id": "s1", "role": "tool", "content": "ok", "tool_call_id": "c1"},
            ]
        )
        msgs = backend.load_messages("s1")
        assert len(msgs) == 3
        assert msgs[1]["tool_calls"][0]["id"] == "c1"

    def test_bulk_empty_is_noop(self, backend):
        backend.save_messages_bulk([])

    def test_bulk_updates_workstream_timestamp(self, backend):
        backend.register_workstream("s1")
        # Save a message to establish an initial updated timestamp
        backend.save_message("s1", "user", "seed")
        rows_before = backend.list_workstreams_with_history()
        updated_before = rows_before[0][5]  # updated column

        backend.save_messages_bulk([{"ws_id": "s1", "role": "user", "content": "bulk"}])
        rows_after = backend.list_workstreams_with_history()
        updated_after = rows_after[0][5]
        assert updated_after >= updated_before

    def test_bulk_missing_attachment_rolls_back_rows_and_refcount(self, backend):
        backend.register_workstream("s1")
        existing_id = "a" * 64
        missing_id = "b" * 64
        backend.save_attachment(existing_id, "known.txt", "text/plain", 5, "text", b"known")

        with pytest.raises(ValueError, match="cannot retain missing attachment blobs"):
            backend.save_messages_bulk(
                [
                    {
                        "ws_id": "s1",
                        "role": "user",
                        "content": "known attachment",
                        "attachment_ids": [existing_id],
                    },
                    {
                        "ws_id": "s1",
                        "role": "user",
                        "content": "missing attachment",
                        "attachment_ids": [missing_id],
                    },
                ]
            )

        assert backend.load_messages("s1") == []
        existing = backend.get_attachment(existing_id)
        assert existing is not None
        assert existing["refcount"] == 1

    def test_bulk_insert_failure_rolls_back_retained_refcount(self, backend):
        """A failure after retention rolls back the increment with the rows."""
        backend.register_workstream("s1")
        attachment_id = "c" * 64
        backend.save_attachment(
            attachment_id,
            "known.txt",
            "text/plain",
            5,
            "text",
            b"known",
        )

        with pytest.raises(sa.exc.IntegrityError):
            backend.save_messages_bulk(
                [
                    {
                        "ws_id": "s1",
                        "role": None,
                        "content": "invalid role",
                        "attachment_ids": [attachment_id],
                    }
                ]
            )

        assert backend.load_messages("s1") == []
        existing = backend.get_attachment(attachment_id)
        assert existing is not None
        assert existing["refcount"] == 1

    def test_bulk_attachment_ownership_and_refcount_balance(self, backend):
        import json

        from turnstone.core.storage._schema import conversations

        source_ws = "source"
        fork_ws = "fork"
        attachment_id = "c" * 64
        backend.register_workstream(source_ws)
        backend.register_workstream(fork_ws)
        source_message_id = backend.save_message(source_ws, "user", "original")
        backend.save_attachment(
            attachment_id,
            "shared.txt",
            "text/plain",
            6,
            "text",
            b"shared",
        )
        backend.set_message_attachments(source_ws, source_message_id, [attachment_id])

        backend.save_messages_bulk(
            [
                {
                    "ws_id": fork_ws,
                    "role": "user",
                    "content": "first copy",
                    "attachment_ids": [attachment_id],
                },
                {
                    "ws_id": fork_ws,
                    "role": "user",
                    "content": "second copy",
                    "attachment_ids": [attachment_id],
                },
            ]
        )

        attachment = backend.get_attachment(attachment_id)
        assert attachment is not None
        assert attachment["refcount"] == 3
        with backend._conn() as conn:
            copied_rows = conn.execute(
                sa.select(conversations.c.content, conversations.c.attachments)
                .where(conversations.c.ws_id == fork_ws)
                .order_by(conversations.c.id)
            ).all()
        assert [(content, json.loads(refs)) for content, refs in copied_rows] == [
            ("first copy", [attachment_id]),
            ("second copy", [attachment_id]),
        ]

        assert backend.delete_workstream(source_ws) is True
        attachment = backend.get_attachment(attachment_id)
        assert attachment is not None
        assert attachment["refcount"] == 2
        assert backend.delete_workstream(fork_ws) is True
        assert backend.get_attachment(attachment_id) is None


class TestListWorkstreamsWithHistory:
    def test_lists_workstreams_with_messages(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hi")
        backend.register_workstream("s2")  # no messages
        rows = backend.list_workstreams_with_history()
        assert len(rows) == 1
        assert rows[0][0] == "s1"

    def test_respects_limit(self, backend):
        for i in range(5):
            sid = f"s{i}"
            backend.register_workstream(sid)
            backend.save_message(sid, "user", f"msg {i}")
        rows = backend.list_workstreams_with_history(limit=3)
        assert len(rows) == 3

    def test_lists_every_kind(self, backend):
        """The CLI's listing spans kinds: coordinator rows persist history too."""
        from turnstone.core.workstream import WorkstreamKind

        backend.register_workstream("interactive-1", kind=WorkstreamKind.INTERACTIVE)
        backend.save_message("interactive-1", "user", "hi")
        backend.register_workstream("coord-1", kind=WorkstreamKind.COORDINATOR)
        backend.save_message("coord-1", "user", "plan something")

        rows = backend.list_workstreams_with_history()
        assert {r[0] for r in rows} == {"interactive-1", "coord-1"}


class TestDeleteWorkstream:
    def test_deletes_all_data(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hi")
        backend.save_workstream_config("s1", {"temp": "0.5"})
        assert backend.delete_workstream("s1")
        assert backend.load_messages("s1") == []
        assert backend.load_workstream_config("s1") == {}
        assert backend.get_workstream_display_name("s1") is None


class TestPruneWorkstreams:
    def test_orphan_removed(self, backend):
        import sqlalchemy as sa

        backend.register_workstream("orphan")
        # Age past the orphan grace; a fresh empty row is deliberately kept
        # (round-3 review) — pinned in test_sessions.py.
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'orphan'")
            )
            conn.commit()
        orphans, stale = backend.prune_workstreams()
        assert orphans == 1

    def test_stale_removed(self, backend):
        import sqlalchemy as sa

        backend.register_workstream("old")
        backend.save_message("old", "user", "hi")
        # Force old timestamp
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'old'")
            )
            conn.commit()
        _, stale = backend.prune_workstreams(retention_days=30)
        assert stale == 1

    def test_rows_a_holder_lapsed_on_recently_are_kept(self, backend):
        """A holder that only stopped renewing may still have the workstream
        open, with an ``updated`` as old as its conversation."""
        import sqlalchemy as sa

        backend.register_workstream("orphan-held", fork_reservation_token="tok-orphan-held")
        backend.register_workstream("stale-held", fork_reservation_token="tok-stale-held")
        backend.save_message("stale-held", "user", "hi")
        for ws_id in ("orphan-held", "stale-held"):
            _acquire(backend, ws_id)
            with backend._engine.connect() as conn:
                conn.execute(
                    sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = :w"),
                    {"w": ws_id},
                )
                conn.commit()
            _expire_lease(backend, ws_id, seconds_ago=1.0)
        assert backend.prune_workstreams(retention_days=30) == (0, 0)

        for ws_id in ("orphan-held", "stale-held"):
            _expire_lease(backend, ws_id)
        assert backend.prune_workstreams(retention_days=30) == (1, 1)


class TestResolveWorkstream:
    def test_exact_alias(self, backend):
        backend.register_workstream("s1")
        backend.set_workstream_alias("s1", "myalias")
        assert backend.resolve_workstream("myalias") == "s1"

    def test_exact_id(self, backend):
        backend.register_workstream("abc-123-def")
        assert backend.resolve_workstream("abc-123-def") == "abc-123-def"

    def test_prefix_match(self, backend):
        backend.register_workstream("abc-123-def")
        assert backend.resolve_workstream("abc") == "abc-123-def"

    def test_not_found(self, backend):
        assert backend.resolve_workstream("nonexistent") is None


# -- Workstream config ---------------------------------------------------------


class TestWorkstreamConfig:
    def test_roundtrip(self, backend):
        backend.register_workstream("s1")
        backend.save_workstream_config("s1", {"temperature": "0.7", "effort": "high"})
        cfg = backend.load_workstream_config("s1")
        assert cfg == {"temperature": "0.7", "effort": "high"}

    def test_empty_config(self, backend):
        assert backend.load_workstream_config("nonexistent") == {}


# -- Workstream metadata ------------------------------------------------------


class TestWorkstreamMetadata:
    def test_alias(self, backend):
        backend.register_workstream("s1")
        assert backend.set_workstream_alias("s1", "my-session")
        assert backend.get_workstream_display_name("s1") == "my-session"

    def test_alias_conflict(self, backend):
        backend.register_workstream("s1")
        backend.register_workstream("s2")
        backend.set_workstream_alias("s1", "taken")
        assert not backend.set_workstream_alias("s2", "taken")

    def test_title(self, backend):
        backend.register_workstream("s1")
        backend.update_workstream_title("s1", "My Title")
        assert backend.get_workstream_display_name("s1") == "My Title"

    def test_alias_preferred_over_title(self, backend):
        backend.register_workstream("s1")
        backend.update_workstream_title("s1", "Title")
        backend.set_workstream_alias("s1", "Alias")
        assert backend.get_workstream_display_name("s1") == "Alias"


# -- Conversation search -------------------------------------------------------


class TestSearch:
    def test_search_history(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello world")
        backend.save_message("s1", "user", "goodbye world")
        results = backend.search_history("hello")
        assert len(results) >= 1
        assert any("hello" in str(r[3]) for r in results)

    def test_search_recent(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "msg1")
        backend.save_message("s1", "user", "msg2")
        results = backend.search_history_recent(limit=1)
        assert len(results) == 1

    def test_search_history_survives_oversized_row(self, backend):
        # A multi-MB row of mostly-unique words: on PostgreSQL its full
        # tsvector exceeds the 1MB hard limit, which used to abort every
        # search_history scan ("string is too long for tsvector") — one
        # giant tool dump silently killed history recall entirely.
        backend.register_workstream("s1")
        giant = "gargantuan beacon " + " ".join(f"w{i}" for i in range(300_000))
        assert len(giant) > 2_000_000
        backend.save_message("s1", "tool", giant)
        backend.save_message("s1", "user", "hello world")

        results = backend.search_history("hello")
        assert any("hello" in str(r[3]) for r in results)

        # The oversized row itself stays findable by its head.
        results = backend.search_history("gargantuan beacon")
        assert any("gargantuan" in str(r[3]) for r in results)

    def test_search_history_fts_error_falls_back_to_ilike(self, request, backend, monkeypatch):
        # PostgreSQL only: a failed FTS statement aborts the connection's
        # autobegun transaction, and the ILIKE fallback runs on that same
        # connection — without a rollback first it dies with
        # InFailedSqlTransaction instead of returning results.
        if request.config.getoption("--storage-backend") != "postgresql":
            pytest.skip("exercises PostgreSQL aborted-transaction fallback")

        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello fallback world")

        real_execute = sa.engine.Connection.execute

        def failing_fts_execute(self, statement, *args, **kwargs):
            if "to_tsvector" in str(statement):
                # A genuine server-side error, so the transaction is aborted
                # exactly as when to_tsvector rejects a row.
                return real_execute(self, sa.text("SELECT 1/0"))
            return real_execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(sa.engine.Connection, "execute", failing_fts_execute)
        results = backend.search_history("fallback")
        assert any("fallback" in str(r[3]) for r in results)


# -- Workstream operations -----------------------------------------------------


class TestWorkstreams:
    def test_register_and_list(self, backend):
        backend.register_workstream("ws1", node_id="node-a", name="first")
        backend.register_workstream("ws2", node_id="node-a", name="second")
        rows = backend.list_workstreams()
        assert len(rows) == 2
        ws_ids = {r[0] for r in rows}
        assert ws_ids == {"ws1", "ws2"}

    def test_register_idempotent(self, backend):
        backend.register_workstream("ws1", name="first")
        backend.register_workstream("ws1", name="overwrite")
        rows = backend.list_workstreams()
        assert len(rows) == 1
        assert rows[0][2] == "first"  # name preserved from first insert

    def test_update_state(self, backend):
        backend.register_workstream("ws1")
        backend.update_workstream_state("ws1", "running")
        rows = backend.list_workstreams()
        assert rows[0][3] == "running"

    def test_update_name(self, backend):
        backend.register_workstream("ws1", name="old")
        backend.update_workstream_name("ws1", "new")
        rows = backend.list_workstreams()
        assert rows[0][2] == "new"

    def test_delete(self, backend):
        backend.register_workstream("ws1")
        assert backend.delete_workstream("ws1") is True
        assert backend.list_workstreams() == []
        assert backend.delete_workstream("ws1") is False

    def test_list_by_node(self, backend):
        backend.register_workstream("ws1", node_id="node-a")
        backend.register_workstream("ws2", node_id="node-b")
        rows = backend.list_workstreams(node_id="node-a")
        assert len(rows) == 1
        assert rows[0][0] == "ws1"

    def test_workstream_with_messages_in_history(self, backend):
        backend.register_workstream("ws1", node_id="node-a")
        backend.save_message("ws1", "user", "hello")
        rows = backend.list_workstreams_with_history()
        assert len(rows) == 1
        # Columns: ws_id, alias, title, name, created, updated, message_count
        assert rows[0][0] == "ws1"
        assert rows[0][6] == 1


# -- Per-workstream usage aggregation -----------------------------------------


class TestSumWorkstreamTokens:
    """``sum_workstream_tokens`` powers the inspect-time token fallback for
    idle children — a regression here would surface as wrong tokens in
    the coordinator's inspect output rather than a focused test failure,
    so guard it directly."""

    def test_empty_ws_id_returns_zero(self, backend):
        assert backend.sum_workstream_tokens("") == 0

    def test_no_events_returns_zero(self, backend):
        assert backend.sum_workstream_tokens("never-seen") == 0

    def test_sums_prompt_and_completion_across_events(self, backend):
        backend.record_usage_event(
            event_id="e1", ws_id="ws-a", prompt_tokens=10, completion_tokens=5
        )
        backend.record_usage_event(
            event_id="e2", ws_id="ws-a", prompt_tokens=200, completion_tokens=80
        )
        assert backend.sum_workstream_tokens("ws-a") == 10 + 5 + 200 + 80

    def test_scoped_to_requested_ws_id(self, backend):
        """Other workstreams' usage events must not leak into the sum."""
        backend.record_usage_event(
            event_id="e1", ws_id="ws-a", prompt_tokens=100, completion_tokens=50
        )
        backend.record_usage_event(
            event_id="e2", ws_id="ws-b", prompt_tokens=999, completion_tokens=999
        )
        assert backend.sum_workstream_tokens("ws-a") == 150
        assert backend.sum_workstream_tokens("ws-b") == 1998


class TestBatchPrimitives:
    """``get_workstreams_batch`` and ``sum_workstream_tokens_batch`` power
    ``wait_for_workstream``'s per-tick polling.  Direct backend coverage
    here so a regression surfaces as a focused failure rather than as
    wrong tokens / spurious denied states in a coordinator session."""

    def test_get_workstreams_batch_empty_input(self, backend):
        assert backend.get_workstreams_batch([]) == {}

    def test_get_workstreams_batch_returns_row_per_id(self, backend):
        backend.register_workstream("a", title="A", kind="interactive")
        backend.register_workstream("b", title="B", kind="interactive", parent_ws_id="a")
        result = backend.get_workstreams_batch(["a", "b"])
        assert set(result.keys()) == {"a", "b"}
        assert result["a"]["ws_id"] == "a"
        assert result["b"]["parent_ws_id"] == "a"

    def test_get_workstreams_batch_missing_id_returns_none(self, backend):
        backend.register_workstream("a")
        result = backend.get_workstreams_batch(["a", "missing"])
        assert result["a"] is not None
        assert result["missing"] is None

    def test_get_workstreams_batch_drops_empty_strings(self, backend):
        """Empty / non-string ids must not pollute the IN clause."""
        backend.register_workstream("a")
        result = backend.get_workstreams_batch(["a", "", "  "])
        # Only the non-empty id is kept; whitespace-only strings are
        # passed through (the helper only strips truly-empty entries).
        assert "a" in result
        assert result["a"] is not None

    def test_sum_workstream_tokens_batch_empty_input(self, backend):
        assert backend.sum_workstream_tokens_batch([]) == {}

    def test_sum_workstream_tokens_batch_aggregates_per_id(self, backend):
        backend.record_usage_event(event_id="e1", ws_id="a", prompt_tokens=10, completion_tokens=5)
        backend.record_usage_event(event_id="e2", ws_id="a", prompt_tokens=20, completion_tokens=10)
        backend.record_usage_event(
            event_id="e3", ws_id="b", prompt_tokens=100, completion_tokens=50
        )
        result = backend.sum_workstream_tokens_batch(["a", "b", "c"])
        assert result == {"a": 45, "b": 150, "c": 0}

    def test_sum_workstream_tokens_batch_missing_id_defaults_zero(self, backend):
        result = backend.sum_workstream_tokens_batch(["never-seen"])
        assert result == {"never-seen": 0}


# -- bulk_close_stale_orphans --------------------------------------------------


def _force_updated(backend: Any, ws_id: str, updated: str) -> None:
    """Stamp a workstream row's ``updated`` column directly.

    The public surface only sets ``updated`` to ``now``, which makes it
    impossible to fabricate a stale row through register/update calls.
    Reaches into ``backend._engine`` — same access pattern conftest uses
    for cross-backend cleanup.
    """
    with backend._engine.connect() as conn:
        conn.execute(
            sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(updated=updated)
        )
        conn.commit()


class TestBulkCloseStaleOrphans:
    def test_closes_stale_non_terminal_rows_of_kind(self, backend):
        backend.register_workstream("stale-idle", kind="interactive")
        backend.register_workstream("stale-thinking", kind="interactive")
        backend.update_workstream_state("stale-thinking", "thinking")
        backend.register_workstream("fresh-idle", kind="interactive")
        _force_updated(backend, "stale-idle", "2020-01-01T00:00:00")
        _force_updated(backend, "stale-thinking", "2020-01-01T00:00:00")
        # fresh-idle stays at registration time (effectively now)

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert set(closed) == {"stale-idle", "stale-thinking"}
        rows = backend.get_workstreams_batch(["stale-idle", "stale-thinking", "fresh-idle"])
        assert rows["stale-idle"]["state"] == "closed"
        assert rows["stale-thinking"]["state"] == "closed"
        assert rows["fresh-idle"]["state"] == "idle"

    def test_skips_already_closed(self, backend):
        backend.register_workstream("already-closed", kind="interactive")
        backend.update_workstream_state("already-closed", "closed")
        _force_updated(backend, "already-closed", "2020-01-01T00:00:00")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert closed == []

    def test_filters_by_kind(self, backend):
        backend.register_workstream("interactive-stale", kind="interactive")
        backend.register_workstream("coord-stale", kind="coordinator")
        _force_updated(backend, "interactive-stale", "2020-01-01T00:00:00")
        _force_updated(backend, "coord-stale", "2020-01-01T00:00:00")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert closed == ["interactive-stale"]
        rows = backend.get_workstreams_batch(["interactive-stale", "coord-stale"])
        assert rows["interactive-stale"]["state"] == "closed"
        assert rows["coord-stale"]["state"] == "idle"

    def test_excludes_loaded_ws_ids(self, backend):
        backend.register_workstream("ws-keep", kind="interactive")
        backend.register_workstream("ws-close", kind="interactive")
        _force_updated(backend, "ws-keep", "2020-01-01T00:00:00")
        _force_updated(backend, "ws-close", "2020-01-01T00:00:00")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=["ws-keep"]
        )

        assert closed == ["ws-close"]
        rows = backend.get_workstreams_batch(["ws-keep", "ws-close"])
        assert rows["ws-keep"]["state"] == "idle"
        assert rows["ws-close"]["state"] == "closed"

    def test_empty_exclude_list_does_not_break_sql(self, backend):
        backend.register_workstream("orphan", kind="interactive")
        _force_updated(backend, "orphan", "2020-01-01T00:00:00")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert closed == ["orphan"]

    def test_no_orphans_returns_empty(self, backend):
        backend.register_workstream("fresh", kind="interactive")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert closed == []

    def test_closes_all_non_terminal_states(self, backend):
        from turnstone.core.workstream import BULK_CLOSE_STATE_VALUES

        for state in sorted(BULK_CLOSE_STATE_VALUES):
            ws_id = f"o-{state}"
            backend.register_workstream(ws_id, kind="interactive")
            if state != "idle":
                backend.update_workstream_state(ws_id, state)
            _force_updated(backend, ws_id, "2020-01-01T00:00:00")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert set(closed) == {f"o-{state}" for state in BULK_CLOSE_STATE_VALUES}

    def test_keeps_updated_on_close(self, backend):
        """Closing an abandoned row is maintenance, not activity: ``updated``
        keeps the session's last real use, which the saved list sorts and
        shows and from which retention ages the row (#1268)."""
        stale_updated = "2020-01-01T00:00:00"
        backend.register_workstream("orphan", kind="interactive")
        _force_updated(backend, "orphan", stale_updated)

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert closed == ["orphan"]
        with backend._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.state, workstreams.c.updated).where(
                    workstreams.c.ws_id == "orphan"
                )
            ).one()
        assert tuple(row) == ("closed", stale_updated)

    def test_live_lease_protects_row_whatever_its_node(self, backend):
        """A workstream loaded by any live process carries a renewed owner
        lease, so bulk close never touches it.  The creating ``node_id``
        grants no protection of its own: a crashed process's rows are
        reclaimable whatever node id the next process carries."""
        for ws_id in ("leased", "unleased"):
            backend.register_workstream(
                ws_id, node_id="node-a", kind="interactive", fork_reservation_token=f"tok-{ws_id}"
            )
            _force_updated(backend, ws_id, "2020-01-01T00:00:00")
        _acquire(backend, "leased", holder="node-b/1")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert closed == ["unleased"]
        rows = backend.get_workstreams_batch(["leased", "unleased"])
        assert rows["leased"]["state"] == "idle"
        assert rows["unleased"]["state"] == "closed"

    def test_lease_that_lapsed_after_the_cutoff_still_protects(self, backend):
        """A holder that missed renewals may still have the row open, and an
        open row keeps an ``updated`` as old as its conversation."""
        from datetime import UTC, datetime, timedelta

        backend.register_workstream(
            "lapsed", kind="interactive", fork_reservation_token="tok-lapsed"
        )
        _acquire(backend, "lapsed", holder="node-a/1")
        _force_updated(backend, "lapsed", "2020-01-01T00:00:00")
        _expire_lease(backend, "lapsed", seconds_ago=1.0)
        cutoff = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")

        assert backend.bulk_close_stale_orphans("interactive", cutoff, []) == []
        assert backend.get_workstream("lapsed")["state"] == "idle"

        _expire_lease(backend, "lapsed")
        assert backend.bulk_close_stale_orphans("interactive", cutoff, []) == ["lapsed"]

    def test_expired_lease_is_closed_and_fenced_out(self, backend):
        """An expired lease no longer protects the row, and the closing
        statement retires it: the paused former holder cannot write after."""
        backend.register_workstream(
            "expired", kind="interactive", fork_reservation_token="tok-expired"
        )
        fence = _acquire(backend, "expired", holder="node-a/1")
        _force_updated(backend, "expired", "2020-01-01T00:00:00")
        _expire_lease(backend, "expired")

        closed = backend.bulk_close_stale_orphans(
            "interactive", cutoff="2024-01-01T00:00:00", exclude_ws_ids=[]
        )

        assert closed == ["expired"]
        assert backend.get_workstream("expired")["state"] == "closed"
        with backend._engine.connect() as conn:
            holder, epoch = conn.execute(
                sa.select(workstreams.c.lease_holder, workstreams.c.lease_epoch).where(
                    workstreams.c.ws_id == "expired"
                )
            ).one()
        assert holder is None
        assert epoch == fence.epoch + 1
        with pytest.raises(WorkstreamLeaseLostError):
            backend.update_workstream_state("expired", "running", lease=fence)
        assert backend.get_workstream("expired")["state"] == "closed"

    def test_combines_lease_and_exclude_ws_ids(self, backend):
        """Both filters stack as AND clauses on the UPDATE.  Covers the
        full 2x2 matrix to catch a future edit that replaces an AND with
        an OR or drops one of the filters: only the unloaded, unleased
        cell should be reaped."""
        for ws_id in ("loaded-leased", "loaded-free", "orphan-leased", "orphan-free"):
            backend.register_workstream(
                ws_id, kind="interactive", fork_reservation_token=f"tok-{ws_id}"
            )
            _force_updated(backend, ws_id, "2020-01-01T00:00:00")
        _acquire(backend, "loaded-leased", holder="node-a/1")
        _acquire(backend, "orphan-leased", holder="node-b/1")

        closed = backend.bulk_close_stale_orphans(
            "interactive",
            cutoff="2024-01-01T00:00:00",
            exclude_ws_ids=["loaded-leased", "loaded-free"],
        )

        assert closed == ["orphan-free"]
        rows = backend.get_workstreams_batch(
            ["loaded-leased", "loaded-free", "orphan-leased", "orphan-free"]
        )
        assert rows["loaded-leased"]["state"] == "idle"
        assert rows["loaded-free"]["state"] == "idle"
        assert rows["orphan-leased"]["state"] == "idle"
        assert rows["orphan-free"]["state"] == "closed"


# -- updated records conversation changes ---------------------------------------


class TestUpdatedRecordsConversationChanges:
    """``updated`` is the last change to a workstream's conversation (#1268):
    lifecycle and metadata writes leave it alone; saving or removing
    messages stamps it."""

    STALE = "2020-01-01T00:00:00"

    def _updated(self, backend: Any, ws_id: str) -> str:
        with backend._engine.connect() as conn:
            return conn.execute(
                sa.select(workstreams.c.updated).where(workstreams.c.ws_id == ws_id)
            ).scalar_one()

    def test_lifecycle_and_metadata_writes_leave_it(self, backend):
        backend.register_workstream(
            "ws-meta", kind="interactive", state="creating", fork_reservation_token="tok"
        )
        _force_updated(backend, "ws-meta", self.STALE)
        assert backend.publish_deferred_create("ws-meta", "tok") is True
        backend.update_workstream_state("ws-meta", "running")
        backend.update_workstream_state("ws-meta", "closed")
        backend.update_workstream_name("ws-meta", "renamed")
        backend.update_workstream_title("ws-meta", "A title")
        assert backend.set_workstream_alias("ws-meta", "meta-alias")
        assert self._updated(backend, "ws-meta") == self.STALE

    def test_saving_and_removing_messages_stamps_it(self, backend):
        backend.register_workstream("ws-chat", kind="interactive")
        _force_updated(backend, "ws-chat", self.STALE)
        backend.save_message("ws-chat", "user", "first")
        assert self._updated(backend, "ws-chat") > self.STALE
        backend.save_message("ws-chat", "assistant", "second")
        backend.save_message("ws-chat", "user", "third")

        _force_updated(backend, "ws-chat", self.STALE)
        assert backend.delete_messages_after("ws-chat", 5) == 0
        assert self._updated(backend, "ws-chat") == self.STALE, "nothing removed"
        assert backend.delete_messages_after("ws-chat", 2) == 1
        assert self._updated(backend, "ws-chat") > self.STALE

        _force_updated(backend, "ws-chat", self.STALE)
        assert backend.truncate_messages_tail("ws-chat", 1) == 1
        assert self._updated(backend, "ws-chat") > self.STALE


# -- MCP OAuth columns ---------------------------------------------------------


class TestMcpServerOauthColumns:
    def test_mcp_servers_oauth_columns_round_trip(self, backend: Any) -> None:
        """An oauth_user row round-trips through create -> get with all
        seven OAuth text columns intact."""
        sid = "oauth-srv-1"
        backend.create_mcp_server(
            server_id=sid,
            name="oauth-srv",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
            oauth_client_id="cli_abc123",
            oauth_scopes="openid profile",
            oauth_audience="https://mcp.example.com",
            oauth_registration_mode="preregistered",
            oauth_authorization_server_url="https://auth.example.com",
            oauth_as_issuer_cached="https://auth.example.com",
        )
        s = backend.get_mcp_server(sid)
        assert s is not None
        assert s["auth_type"] == "oauth_user"
        assert s["oauth_client_id"] == "cli_abc123"
        assert s["oauth_scopes"] == "openid profile"
        assert s["oauth_audience"] == "https://mcp.example.com"
        assert s["oauth_registration_mode"] == "preregistered"
        assert s["oauth_authorization_server_url"] == "https://auth.example.com"
        assert s["oauth_as_issuer_cached"] == "https://auth.example.com"
        # Phase 2 leaves the ciphertext slot NULL even when other oauth
        # fields are populated; Phase 3 wires the encryption write path.
        assert s["oauth_client_secret_ct"] is None

    def test_update_auth_type_static_to_oauth(self, backend: Any) -> None:
        sid = "oauth-srv-2"
        backend.create_mcp_server(
            server_id=sid,
            name="static-then-oauth",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
        )
        assert backend.get_mcp_server(sid)["auth_type"] == "static"

        ok = backend.update_mcp_server(
            sid,
            auth_type="oauth_user",
            oauth_client_id="cli_after",
            oauth_audience="https://mcp.example.com",
        )
        assert ok is True
        s = backend.get_mcp_server(sid)
        assert s is not None
        assert s["auth_type"] == "oauth_user"
        assert s["oauth_client_id"] == "cli_after"
        assert s["oauth_audience"] == "https://mcp.example.com"


# -- Lifecycle -----------------------------------------------------------------


class TestLifecycle:
    def test_close(self, backend):
        backend.close()  # Should not raise

    def test_isinstance_check(self, backend):
        from turnstone.core.storage._protocol import StorageBackend

        assert isinstance(backend, StorageBackend)
