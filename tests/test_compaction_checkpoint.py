"""Tests for persisted compaction checkpoints (rehydration-deadlock fix).

Compaction swaps a session's in-memory history for a summary but leaves the full
transcript in storage.  Without a durable marker, ``resume()`` reloaded the full
pre-compaction history, which on a long session — or one switched to a smaller-
context model — exceeds the window and deadlocks the first send.

The fix persists one ``_source="compaction"`` marker (summary + watermark) so
resume rehydrates ``[summary] + [rows after the watermark]`` while the full
history stays in storage for ``/history``/export.  Covered here:

- ``get_compaction_watermark`` — the boundary id (max-summarized), with and
  without a preserved tail, and on an empty workstream.
- ``load_message_turns`` (resume) — checkpoint-aware slice, latest-marker-wins,
  preserved-tail handling, and the full-history fallbacks (no marker, malformed
  marker) that keep every pre-checkpoint session loading exactly as before.
- ``load_messages`` (display) — markers stay invisible to ``/history``.
- End-to-end: ``_compact_messages`` writes the marker and a fresh ``resume()``
  rehydrates the bounded view, not the full transcript.
"""

from __future__ import annotations

import json

import pytest

from tests._session_helpers import make_session
from turnstone.core.trajectory import turns_from_dicts


def _marker_meta(watermark: int | None) -> str | None:
    """The marker's stored ``meta`` JSON (``None`` simulates a legacy/malformed marker)."""
    return json.dumps({"watermark": watermark}) if watermark is not None else None


def _register(st, ws: str = "ws1") -> str:
    st.register_workstream(ws, user_id="u1", title="t", kind="interactive")
    return ws


# ---------------------------------------------------------------------------
# get_compaction_watermark
# ---------------------------------------------------------------------------


class TestWatermark:
    def test_preserve_tail_zero_is_max_id(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        ids = [st.save_message(ws, "user", f"m{i}") for i in range(5)]
        assert st.get_compaction_watermark(ws, 0) == max(ids)

    def test_preserve_tail_n_is_nth_newest(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        ids = sorted(st.save_message(ws, "user", f"m{i}") for i in range(5))
        # Keep the newest 2 verbatim → boundary is the 3rd-newest id.
        assert st.get_compaction_watermark(ws, 2) == ids[-3]

    def test_preserve_tail_ignores_existing_markers(self, storage_backend):
        # A compaction marker is saved as a NEW row but is not part of the
        # preserved in-memory tail, so it must not shift the (preserve_tail+1)
        # boundary — without the exclusion, this returns ids[-1] (the marker
        # consumes an offset slot) and resume would drop a real tail row.
        st = storage_backend
        ws = _register(st)
        ids = [st.save_message(ws, "user", f"m{i}") for i in range(5)]
        st.save_message(ws, "assistant", "SUM", source="compaction", meta=_marker_meta(max(ids)))
        st.save_message(ws, "user", "m5")
        # Real rows newest-first: m5, m4, m3, ... → 3rd-newest real row is m3.
        assert st.get_compaction_watermark(ws, 2) == ids[-2]

    def test_empty_workstream_is_none(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        assert st.get_compaction_watermark(ws, 0) is None

    def test_preserve_tail_exceeding_row_count_is_none(self, storage_backend):
        # Fewer rows than the preserved tail → no boundary, so compaction skips
        # the marker rather than writing a watermark that points past the history.
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "only")
        assert st.get_compaction_watermark(ws, 5) is None


# ---------------------------------------------------------------------------
# load_message_turns — checkpoint-aware resume
# ---------------------------------------------------------------------------


class TestCheckpointResume:
    def test_loads_summary_plus_tail_not_full_history(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        for i in range(5):
            st.save_message(ws, "user" if i % 2 == 0 else "assistant", f"old{i}")
        watermark = st.get_compaction_watermark(ws, 0)
        st.save_message(
            ws, "assistant", "THE SUMMARY", source="compaction", meta=_marker_meta(watermark)
        )
        st.save_message(ws, "user", "new question")
        st.save_message(ws, "assistant", "new answer")

        texts = [t.text for t in st.load_message_turns(ws)]
        assert texts == ["[Conversation summary]", "THE SUMMARY", "new question", "new answer"]
        assert not any("old" in x for x in texts)  # summarized prefix is gone

    def test_preserved_tail_kept_after_summary(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        ids = sorted(st.save_message(ws, "user", f"m{i}") for i in range(4))
        # Mid-turn compaction keeps the newest row (m3) verbatim.
        watermark = st.get_compaction_watermark(ws, 1)
        assert watermark == ids[-2]
        st.save_message(ws, "assistant", "SUM", source="compaction", meta=_marker_meta(watermark))

        texts = [t.text for t in st.load_message_turns(ws)]
        assert texts == ["[Conversation summary]", "SUM", "m3"]

    def test_latest_marker_wins(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "old")
        st.save_message(
            ws,
            "assistant",
            "SUMMARY 1",
            source="compaction",
            meta=_marker_meta(st.get_compaction_watermark(ws, 0)),
        )
        st.save_message(ws, "user", "mid")
        st.save_message(
            ws,
            "assistant",
            "SUMMARY 2",
            source="compaction",
            meta=_marker_meta(st.get_compaction_watermark(ws, 0)),
        )
        st.save_message(ws, "user", "after")

        texts = [t.text for t in st.load_message_turns(ws)]
        assert texts == ["[Conversation summary]", "SUMMARY 2", "after"]
        assert "SUMMARY 1" not in texts and "old" not in texts and "mid" not in texts

    def test_no_marker_loads_full_history(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        for i in range(3):
            st.save_message(ws, "user", f"m{i}")
        assert [t.text for t in st.load_message_turns(ws)] == ["m0", "m1", "m2"]

    def test_malformed_marker_falls_back_to_full_history(self, storage_backend):
        # A marker with no watermark (legacy/corrupt) must NOT slice — losing
        # real messages is worse than reloading more than necessary.
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "a")
        st.save_message(ws, "assistant", "SUMMARY", source="compaction", meta=None)
        st.save_message(ws, "user", "b")
        texts = [t.text for t in st.load_message_turns(ws)]
        assert "a" in texts and "b" in texts  # no real message dropped


# ---------------------------------------------------------------------------
# load_messages — display path keeps markers invisible
# ---------------------------------------------------------------------------


class TestDisplayPath:
    def test_history_excludes_marker(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "q")
        st.save_message(ws, "assistant", "a")
        st.save_message(
            ws,
            "assistant",
            "SUMMARY",
            source="compaction",
            meta=_marker_meta(st.get_compaction_watermark(ws, 0)),
        )

        contents = [m.get("content") for m in st.load_messages(ws)]
        assert "SUMMARY" not in contents
        assert contents == ["q", "a"]  # true transcript, no injected summary

    def test_include_compaction_projects_marker_as_system_row(self, storage_backend):
        """The /history display path (include_compaction=True) surfaces the
        marker IN PLACE as a first-class system row — source="compaction",
        meta = the marker's stored fields — so the UI re-renders its
        compaction card after a reload.  Export/search (default False)
        stay on the drop path pinned above."""
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "q")
        st.save_message(ws, "assistant", "a")
        wm = st.get_compaction_watermark(ws, 0)
        st.save_message(
            ws,
            "assistant",
            "SUMMARY",
            source="compaction",
            meta=json.dumps(
                {"watermark": wm, "before_tokens": 900, "after_tokens": 80, "trigger": "manual"}
            ),
        )
        st.save_message(ws, "user", "later question")

        msgs = st.load_messages(ws, include_compaction=True)
        assert [m.get("content") for m in msgs] == ["q", "a", "SUMMARY", "later question"]
        marker = msgs[2]
        assert marker["role"] == "system"  # display row, not a fake assistant turn
        assert marker.get("_source") == "compaction"
        meta = marker.get("_source_meta")
        assert meta == {
            "watermark": wm,
            "before_tokens": 900,
            "after_tokens": 80,
            "trigger": "manual",
        }


# ---------------------------------------------------------------------------
# End-to-end: compaction writes the marker, resume is bounded
# ---------------------------------------------------------------------------


def test_compaction_persists_checkpoint_and_resume_is_bounded(tmp_db, mock_openai_client):
    """The deadlock-fix proof: a session compacts, a fresh session reopens it,
    and resume rehydrates [summary]+[tail] — never the full pre-compaction
    transcript that would overflow the window on reopen."""
    from unittest.mock import patch

    from turnstone.core.memory import register_workstream, save_message

    ws = "wsE2E"
    register_workstream(ws, user_id="u1", name="t")
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(6)
    ]
    for h in history:
        save_message(ws, h["role"], h["content"])

    sess = make_session(client=mock_openai_client, context_window=10_000, max_tokens=1_000)
    sess._ws_id = ws
    sess.messages = turns_from_dicts(history)
    sess._msg_tokens = [1] * len(history)
    with patch.object(sess, "_summarize_blocks", return_value="DENSE SUMMARY"):
        assert sess._compact_messages(auto=False) is True

    # Conversation continues after the compaction.
    save_message(ws, "user", "after compaction")

    # A fresh session reopens the workstream.
    sess2 = make_session(client=mock_openai_client, context_window=10_000, max_tokens=1_000)
    assert sess2.resume(ws) is True
    texts = [t.text for t in sess2.messages]

    assert texts[:2] == ["[Conversation summary]", "DENSE SUMMARY"]
    assert "after compaction" in texts
    assert not any(t.startswith("turn ") for t in texts)  # full history NOT reloaded


# ---------------------------------------------------------------------------
# Malformed / edge-case markers — the watermark guards and the empty tail
# ---------------------------------------------------------------------------


class TestMarkerEdges:
    @pytest.mark.parametrize(
        "meta",
        [
            json.dumps({"watermark": "5"}),  # non-int (string)
            json.dumps({"watermark": True}),  # bool — True is an int subclass
            json.dumps({}),  # key absent
            json.dumps({"watermark": None}),  # null
        ],
    )
    def test_non_int_watermark_falls_back_to_full_history(self, storage_backend, meta):
        # A watermark that isn't a real int must NOT slice (a True watermark
        # would otherwise cut at id 1 and drop real history).
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "a")
        st.save_message(ws, "assistant", "b")
        st.save_message(ws, "assistant", "SUMMARY", source="compaction", meta=meta)
        st.save_message(ws, "user", "c")
        texts = [t.text for t in st.load_message_turns(ws)]
        assert "a" in texts and "b" in texts and "c" in texts  # nothing sliced away
        # ...and the malformed marker is DROPPED, not leaked as a stray summary turn.
        assert "SUMMARY" not in texts

    def test_marker_as_final_row_yields_empty_tail(self, storage_backend):
        # watermark == max id, marker is the last row → resume is just the summary.
        st = storage_backend
        ws = _register(st)
        for i in range(3):
            st.save_message(ws, "user", f"old{i}")
        wm = st.get_compaction_watermark(ws, 0)
        st.save_message(ws, "assistant", "SUMMARY", source="compaction", meta=_marker_meta(wm))
        assert [t.text for t in st.load_message_turns(ws)] == ["[Conversation summary]", "SUMMARY"]


# ---------------------------------------------------------------------------
# checkpointed=False — export/audit gets the FULL transcript (markers dropped)
# ---------------------------------------------------------------------------


class TestFullHistoryLoad:
    def test_checkpointed_false_returns_full_history_without_marker(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        for i in range(4):
            st.save_message(ws, "user" if i % 2 == 0 else "assistant", f"old{i}")
        wm = st.get_compaction_watermark(ws, 0)
        st.save_message(ws, "assistant", "SUMMARY", source="compaction", meta=_marker_meta(wm))
        st.save_message(ws, "user", "after")

        # Resume (default) is bounded; export (checkpointed=False) is full + marker-free.
        assert [t.text for t in st.load_message_turns(ws)] == [
            "[Conversation summary]",
            "SUMMARY",
            "after",
        ]
        full = [t.text for t in st.load_message_turns(ws, checkpointed=False)]
        assert full == ["old0", "old1", "old2", "old3", "after"]
        assert "SUMMARY" not in full and "[Conversation summary]" not in full


# ---------------------------------------------------------------------------
# search — compaction markers stay out of search results
# ---------------------------------------------------------------------------


class TestSearchExclusion:
    def test_search_history_excludes_markers(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "findme apple")
        st.save_message(
            ws,
            "assistant",
            "findme SUMMARY banana",
            source="compaction",
            meta=_marker_meta(st.get_compaction_watermark(ws, 0)),
        )
        contents = [r[3] for r in st.search_history("findme")]
        assert any("apple" in (c or "") for c in contents)  # real row matched
        assert not any("SUMMARY" in (c or "") for c in contents)  # marker excluded
        # ...and normal rows (whose _source is NULL) are NOT dropped by the filter.
        assert contents

    def test_search_history_recent_excludes_markers(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "real")
        st.save_message(
            ws,
            "assistant",
            "SUMMARY",
            source="compaction",
            meta=_marker_meta(st.get_compaction_watermark(ws, 0)),
        )
        recent = [r[3] for r in st.search_history_recent(10)]
        assert "real" in recent and "SUMMARY" not in recent


# ---------------------------------------------------------------------------
# rewind / retry — compaction-safe truncation (never delete the summary backing)
# ---------------------------------------------------------------------------


class TestCompactionFloor:
    def test_floor_and_count(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        for i in range(3):
            st.save_message(ws, "user", f"old{i}")  # summarized prefix
        wm = st.get_compaction_watermark(ws, 0)
        st.save_message(ws, "assistant", "SUMMARY", source="compaction", meta=_marker_meta(wm))
        st.save_message(ws, "user", "tail1")
        st.save_message(ws, "assistant", "tail2")
        assert st.get_compaction_floor(ws) == 4  # 3 prefix + 1 marker
        assert st.count_messages(ws) == 6

    def test_floor_zero_without_marker(self, storage_backend):
        st = storage_backend
        ws = _register(st)
        st.save_message(ws, "user", "x")
        assert st.get_compaction_floor(ws) == 0


def test_rewind_after_compaction_never_deletes_summary_backing(tmp_db, mock_openai_client):
    """The review's major rewind finding: after a compaction, a tail-trim must
    delete from the storage TAIL and floor at the marker, not keep the oldest
    summarized rows and drop the marker."""
    from turnstone.core.memory import get_storage, register_workstream, save_message

    ws = "wsRW"
    register_workstream(ws, user_id="u1", name="t")
    for i in range(3):
        save_message(ws, "user", f"old{i}")  # prefix
    st = get_storage()
    wm = st.get_compaction_watermark(ws, 0)
    save_message(
        ws, "assistant", "SUMMARY", source="compaction", meta=json.dumps({"watermark": wm})
    )
    save_message(ws, "user", "q1")  # tail
    save_message(ws, "assistant", "a1")  # tail
    assert st.get_compaction_floor(ws) == 4 and st.count_messages(ws) == 6

    sess = make_session(client=mock_openai_client, context_window=10_000, max_tokens=1_000)
    sess._ws_id = ws

    # Trim one tail turn → keep = max(floor 4, total 6 - 1) = 5 → deletes only "a1".
    sess._persist_truncation(1)
    assert st.count_messages(ws) == 5
    survived = [t.text for t in st.load_message_turns(ws)]
    assert survived[:2] == ["[Conversation summary]", "SUMMARY"]  # marker + prefix intact
    assert "q1" in survived

    # Over-deep trim → clamps at the floor; the marker + prefix still survive.
    sess._persist_truncation(100)
    assert st.count_messages(ws) == 4  # floored at prefix + marker
    after = [t.text for t in st.load_message_turns(ws)]
    assert after == ["[Conversation summary]", "SUMMARY"]  # summary backing never deleted


def test_persist_truncation_uncompacted_matches_plain_tail_delete(tmp_db, mock_openai_client):
    """With no compaction (floor 0), the new path is identical to the old
    keep=len(self.messages) tail delete."""
    from turnstone.core.memory import get_storage, register_workstream, save_message

    ws = "wsPlain"
    register_workstream(ws, user_id="u1", name="t")
    for i in range(5):
        save_message(ws, "user", f"m{i}")
    st = get_storage()
    assert st.get_compaction_floor(ws) == 0

    sess = make_session(client=mock_openai_client, context_window=10_000, max_tokens=1_000)
    sess._ws_id = ws
    sess._persist_truncation(2)  # remove the last 2
    assert st.count_messages(ws) == 3


def test_persist_truncation_skips_delete_when_count_unavailable(tmp_db, mock_openai_client):
    """count_messages==0 (the storage-error sentinel) must NOT delete — a wrong
    truncation would lose user history."""
    from unittest.mock import patch

    from turnstone.core.memory import get_storage, register_workstream, save_message

    ws = "wsCnt"
    register_workstream(ws, user_id="u1", name="t")
    for i in range(4):
        save_message(ws, "user", f"m{i}")
    st = get_storage()
    sess = make_session(client=mock_openai_client)
    sess._ws_id = ws
    with patch("turnstone.core.session.count_messages", return_value=0):
        sess._persist_truncation(2)
    assert st.count_messages(ws) == 4  # nothing deleted


def test_persist_truncation_skips_delete_when_floor_unavailable(tmp_db, mock_openai_client):
    """get_compaction_floor==-1 (the storage-error sentinel) must NOT delete — a 0
    floor on a compacted ws could otherwise drop the marker on an over-deep trim."""
    from unittest.mock import patch

    from turnstone.core.memory import get_storage, register_workstream, save_message

    ws = "wsFloor"
    register_workstream(ws, user_id="u1", name="t")
    for i in range(4):
        save_message(ws, "user", f"m{i}")
    st = get_storage()
    sess = make_session(client=mock_openai_client)
    sess._ws_id = ws
    with patch("turnstone.core.session.get_compaction_floor", return_value=-1):
        sess._persist_truncation(2)
    assert st.count_messages(ws) == 4  # nothing deleted
