"""Live-context exclusion for the model-facing recall tool.

After a compaction the summary is a cache over the originals, not their
replacement — recall is the re-derivation path back into them.  Scoping it:

- ``get_compaction_checkpoint`` reads the latest persisted marker's watermark
  (distinct from ``get_compaction_watermark``, which computes what a NEW
  compaction would use).
- ``search_history(exclude_ws_id=…, exclude_after=…)`` drops the excluded
  workstream's rows ABOVE the boundary — the live segment already in the
  model's context — while rows at or below it (the summarized-away past)
  stay searchable.  ``exclude_after=None`` excludes the whole workstream:
  never compacted means everything is live.
- ``_exec_recall`` passes its own workstream with a boundary read fresh at
  execution time, and labels own-conversation hits so the model knows it is
  re-reading its compacted past.
- The exclusion composes with the #745 tenancy scope, and the resume nudge
  teaches the model the path exists.

Other workstreams are untouched — recall remains the cross-conversation
search tool.  The /history command deliberately has no exclusion: a human
browsing history has no "context" to duplicate.
"""

from __future__ import annotations

import json

from tests._session_helpers import make_session
from turnstone.core.metacognition import NUDGE_COMPACTION_RESUME
from turnstone.core.session import COMPACTION_SOURCE

NEEDLE = "quillfeather"


def _fill(st, ws: str, owner: str = "u1") -> list[int]:
    """Register ``ws`` and write four searchable rows; return their ids."""
    st.register_workstream(ws, user_id=owner, title="t", kind="interactive")
    return [st.save_message(ws, "user", f"{NEEDLE} row{i} in {ws}") for i in range(4)]


def _mark(st, ws: str, watermark: int | None, content: str = "SUMMARY") -> int:
    """Write a compaction marker with ``watermark`` (None = malformed/legacy meta)."""
    meta = json.dumps({"watermark": watermark}) if watermark is not None else None
    return st.save_message(ws, "assistant", content, source=COMPACTION_SOURCE, meta=meta)


def _hits(st, **kwargs) -> set[str]:
    return {r[3] for r in st.search_history(NEEDLE, limit=50, **kwargs)}


# ---------------------------------------------------------------------------
# get_compaction_checkpoint
# ---------------------------------------------------------------------------


class TestGetCompactionCheckpoint:
    def test_none_when_never_compacted(self, storage_backend):
        _fill(storage_backend, "ws1")
        assert storage_backend.get_compaction_checkpoint("ws1") is None

    def test_reads_marker_watermark(self, storage_backend):
        st = storage_backend
        ids = _fill(st, "ws1")
        _mark(st, "ws1", ids[1])
        assert st.get_compaction_checkpoint("ws1") == ids[1]

    def test_latest_marker_wins(self, storage_backend):
        st = storage_backend
        ids = _fill(st, "ws1")
        _mark(st, "ws1", ids[0])
        _mark(st, "ws1", ids[2])
        assert st.get_compaction_checkpoint("ws1") == ids[2]

    def test_malformed_meta_reads_none(self, storage_backend):
        """A legacy/corrupt marker must read as 'whole ws live' (exclude all),
        never as a garbage boundary."""
        st = storage_backend
        _fill(st, "ws1")
        _mark(st, "ws1", None)
        assert st.get_compaction_checkpoint("ws1") is None


# ---------------------------------------------------------------------------
# search_history live-context exclusion
# ---------------------------------------------------------------------------


class TestLiveContextExclusion:
    def test_excludes_live_segment_keeps_compacted_past(self, storage_backend):
        st = storage_backend
        ids = _fill(st, "ws1")  # rows 0..3
        boundary = ids[1]  # rows 0-1 compacted away; 2-3 live
        found = _hits(st, exclude_ws_id="ws1", exclude_after=boundary)
        assert found == {f"{NEEDLE} row0 in ws1", f"{NEEDLE} row1 in ws1"}

    def test_never_compacted_ws_fully_excluded(self, storage_backend):
        st = storage_backend
        _fill(st, "ws1")
        assert _hits(st, exclude_ws_id="ws1", exclude_after=None) == set()

    def test_other_workstreams_unaffected(self, storage_backend):
        st = storage_backend
        _fill(st, "ws1")
        _fill(st, "ws2")
        found = _hits(st, exclude_ws_id="ws1", exclude_after=None)
        assert found == {f"{NEEDLE} row{i} in ws2" for i in range(4)}

    def test_no_exclusion_without_ws(self, storage_backend):
        """The /history command path: no exclude args → everything searchable."""
        st = storage_backend
        _fill(st, "ws1")
        assert len(_hits(st)) == 4

    def test_composes_with_tenancy_scope(self, storage_backend):
        """Exclusion and the #745 private-project predicate BOTH drop rows in
        one query: a mid-conversation boundary leaves ws_mine rows 2-3 live
        (excluded) and 0-1 compacted (kept), while the tenancy predicate
        hides dave's private-project row from carol — deleting either
        fragment fails this test."""
        st = storage_backend
        st.create_project("P", "P", owner_id="alice", visibility="private")
        ids = _fill(st, "ws_mine", owner="alice")
        st.register_workstream("ws_priv", user_id="dave", title="t", project_id="P")
        st.save_message("ws_priv", "user", f"{NEEDLE} private row")
        boundary = ids[1]  # rows 0-1 compacted past; rows 2-3 live context
        _mark(st, "ws_mine", boundary)
        found = _hits(st, user_id="carol", exclude_ws_id="ws_mine", exclude_after=boundary)
        assert found == {f"{NEEDLE} row0 in ws_mine", f"{NEEDLE} row1 in ws_mine"}


# ---------------------------------------------------------------------------
# _exec_recall plumbing + labeling
# ---------------------------------------------------------------------------


class TestRecallExecScope:
    def _run_recall(self, session, rows, monkeypatch, checkpoint=7):
        calls: dict = {}

        def fake_search_history(query, limit=20, offset=0, **kwargs):
            calls.update(kwargs)
            return rows

        monkeypatch.setattr("turnstone.core.session.search_history", fake_search_history)
        monkeypatch.setattr(
            "turnstone.core.session.get_compaction_checkpoint", lambda ws: checkpoint
        )
        item = session._prepare_recall("c1", {"query": "x"})
        _, output = session._exec_recall(item)
        return calls, output

    def test_passes_own_ws_and_fresh_boundary(self, monkeypatch):
        session = make_session(user_id="owner")
        session._ws_id = "ws-self"
        calls, _ = self._run_recall(session, [], monkeypatch, checkpoint=42)
        assert calls["exclude_ws_id"] == "ws-self"
        assert calls["exclude_after"] == 42

    def test_no_exclusion_without_registered_ws(self, monkeypatch):
        session = make_session(user_id="owner")
        session._ws_id = ""
        calls, _ = self._run_recall(session, [], monkeypatch)
        assert calls["exclude_ws_id"] is None
        assert calls["exclude_after"] is None

    def test_own_conversation_hits_are_labeled(self, monkeypatch):
        session = make_session(user_id="owner")
        session._ws_id = "ws-self"
        rows = [
            ("2026-07-02T10:00:00", "ws-self", "user", "old detail", None),
            ("2026-07-02T11:00:00", "ws-other", "user", "other detail", None),
        ]
        _, output = self._run_recall(session, rows, monkeypatch)
        own_line = next(line for line in output.splitlines() if "old detail" in line)
        other_line = next(line for line in output.splitlines() if "other detail" in line)
        assert "(earlier in this conversation, compacted)" in own_line
        assert "(earlier in this conversation, compacted)" not in other_line


def test_resume_nudge_teaches_recall():
    """The model is told the summary is a digest and recall reaches the
    compacted portion — the pointer that makes the instrumented form usable."""
    assert "recall tool" in NUDGE_COMPACTION_RESUME
    assert "compacted portion" in NUDGE_COMPACTION_RESUME
