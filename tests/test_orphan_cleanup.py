"""Orphan-conversation scan + purge (the ``turnstone-admin orphan-conversations`` verb).

Orphans are conversation rows whose ``workstreams`` row is gone — written by
historical unregistered paths or by the delete-during-inflight race (a late
tool-result save re-creating rows after ``delete_workstream``).
"""

from __future__ import annotations

import argparse
import hashlib

import pytest

from turnstone.admin import _cmd_orphan_conversations


def _orphan(backend, ws_id: str, n: int = 2) -> None:
    """Persist *n* conversation rows for *ws_id* WITHOUT registering it."""
    for i in range(n):
        backend.save_message(ws_id, "user" if i % 2 == 0 else "assistant", f"m{i}")


def _blob(backend, payload: bytes, origin: str = "upload") -> str:
    """Save a content-addressed attachment; each save bumps the refcount."""
    aid = hashlib.sha256(payload).hexdigest()
    backend.save_attachment(aid, "f.txt", "text/plain", len(payload), "text", payload, origin)
    return aid


class TestOrphanScan:
    def test_clean_db_has_no_orphans(self, backend):
        backend.register_workstream("live1")
        backend.save_message("live1", "user", "hello")
        assert backend.list_orphan_conversations() == []

    def test_orphan_reported_with_stats(self, backend):
        _orphan(backend, "ghost1", n=3)
        scan = backend.list_orphan_conversations()
        assert len(scan) == 1
        entry = scan[0]
        assert entry["ws_id"] == "ghost1"
        assert entry["rows"] == 3
        assert entry["first"] <= entry["last"]
        assert entry["attachment_refs"] == 0

    def test_scan_counts_attachment_refs(self, backend):
        _orphan(backend, "ghost2", n=1)
        msg_id = backend.save_message("ghost2", "user", "with attachment")
        aid = _blob(backend, b"orphan-bytes")
        backend.set_message_attachments("ghost2", msg_id, [aid])
        scan = backend.list_orphan_conversations()
        assert scan[0]["attachment_refs"] == 1

    def test_scan_is_oldest_first(self, backend):
        _orphan(backend, "newer")
        _orphan(backend, "older")
        # Timestamps are insertion-ordered ISO text; rewrite to force ordering.
        import sqlalchemy as sa

        from turnstone.core.storage._schema import conversations

        with backend._conn() as conn:
            conn.execute(
                sa.update(conversations)
                .where(conversations.c.ws_id == "older")
                .values(timestamp="2020-01-01T00:00:00")
            )
            conn.commit()
        scan = backend.list_orphan_conversations()
        assert [o["ws_id"] for o in scan] == ["older", "newer"]


class TestOrphanPurge:
    def test_purge_deletes_only_orphans(self, backend):
        backend.register_workstream("live1")
        backend.save_message("live1", "user", "keep me")
        _orphan(backend, "ghost1", n=4)
        result = backend.delete_orphan_conversations(["ghost1"])
        assert result == {"workstreams": 1, "rows": 4, "released_refs": 0, "skipped": 0}
        assert backend.list_orphan_conversations() == []
        assert len(backend.load_messages("live1")) == 1

    def test_purge_skips_reregistered_ws(self, backend):
        """A ws_id that gained a workstreams row between scan and purge survives."""
        _orphan(backend, "ghost1", n=2)
        scan = [o["ws_id"] for o in backend.list_orphan_conversations()]
        backend.register_workstream("ghost1")
        result = backend.delete_orphan_conversations(scan)
        assert result["skipped"] == 1
        assert result["workstreams"] == 0
        assert result["rows"] == 0
        assert len(backend.load_messages("ghost1")) == 2

    def test_purge_releases_refcounts_and_prunes_at_zero(self, backend):
        _orphan(backend, "ghost1", n=1)
        msg_id = backend.save_message("ghost1", "user", "img")
        aid = _blob(backend, b"only-orphan-referenced")
        backend.set_message_attachments("ghost1", msg_id, [aid])
        result = backend.delete_orphan_conversations(["ghost1"])
        assert result["released_refs"] == 1
        assert backend.get_attachment(aid) is None

    def test_purge_keeps_blob_shared_with_live_ws(self, backend):
        payload = b"shared-bytes"
        backend.register_workstream("live1")
        live_msg = backend.save_message("live1", "user", "live ref")
        aid_live = _blob(backend, payload)
        backend.set_message_attachments("live1", live_msg, [aid_live])

        _orphan(backend, "ghost1", n=1)
        ghost_msg = backend.save_message("ghost1", "user", "ghost ref")
        aid_ghost = _blob(backend, payload)  # same content hash; refcount -> 2
        backend.set_message_attachments("ghost1", ghost_msg, [aid_ghost])
        assert aid_live == aid_ghost

        result = backend.delete_orphan_conversations(["ghost1"])
        assert result["released_refs"] == 1
        row = backend.get_attachment(aid_live)
        assert row is not None
        assert row["refcount"] == 1

    def test_purge_sweeps_config_rows(self, backend):
        _orphan(backend, "ghost1", n=1)
        backend.save_workstream_config("ghost1", {"model": "x"})
        backend.delete_orphan_conversations(["ghost1"])
        import sqlalchemy as sa

        from turnstone.core.storage._schema import workstream_config

        with backend._conn() as conn:
            left = conn.execute(
                sa.select(sa.func.count()).where(workstream_config.c.ws_id == "ghost1")
            ).scalar()
        assert left == 0

    def test_purge_empty_list_is_noop(self, backend):
        assert backend.delete_orphan_conversations([]) == {
            "workstreams": 0,
            "rows": 0,
            "released_refs": 0,
            "skipped": 0,
        }


class TestAdminVerb:
    """The CLI handler over a real (ephemeral) backend."""

    def _args(self, **kw) -> argparse.Namespace:
        return argparse.Namespace(delete=False, yes=False, **kw)

    def test_scan_reports_and_does_not_delete(self, backend, monkeypatch, capsys):
        _orphan(backend, "ghost1", n=2)
        monkeypatch.setattr("turnstone.admin._get_storage", lambda args: backend)
        _cmd_orphan_conversations(self._args())
        out = capsys.readouterr().out
        assert "ghost1" in out
        assert "--delete" in out
        assert len(backend.load_messages("ghost1")) == 2

    def test_delete_yes_purges(self, backend, monkeypatch, capsys):
        _orphan(backend, "ghost1", n=2)
        monkeypatch.setattr("turnstone.admin._get_storage", lambda args: backend)
        ns = self._args()
        ns.delete = True
        ns.yes = True
        _cmd_orphan_conversations(ns)
        out = capsys.readouterr().out
        assert "Purged 2" in out
        assert backend.list_orphan_conversations() == []

    def test_delete_confirmation_abort(self, backend, monkeypatch, capsys):
        _orphan(backend, "ghost1", n=1)
        monkeypatch.setattr("turnstone.admin._get_storage", lambda args: backend)
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        ns = self._args()
        ns.delete = True
        with pytest.raises(SystemExit):
            _cmd_orphan_conversations(ns)
        assert len(backend.load_messages("ghost1")) == 1

    def test_delete_summary_reports_partial_skip(self, backend, monkeypatch, capsys):
        """Mixed batch: summary shows ACTUAL purge counts plus the skipped clause."""
        _orphan(backend, "ghost1", n=2)
        _orphan(backend, "ghost2", n=3)
        real_list = backend.list_orphan_conversations

        def list_then_register():
            scan = real_list()
            backend.register_workstream("ghost2")  # wins the scan-to-purge race
            return scan

        monkeypatch.setattr(backend, "list_orphan_conversations", list_then_register)
        monkeypatch.setattr("turnstone.admin._get_storage", lambda args: backend)
        ns = self._args()
        ns.delete = True
        ns.yes = True
        _cmd_orphan_conversations(ns)
        out = capsys.readouterr().out
        assert "Purged 2 row(s) across 1 workstream(s)" in out
        assert "skipped 1 re-registered" in out
        assert len(backend.load_messages("ghost2")) == 3

    def test_clean_db_message(self, backend, monkeypatch, capsys):
        monkeypatch.setattr("turnstone.admin._get_storage", lambda args: backend)
        _cmd_orphan_conversations(self._args())
        assert "No orphan conversation rows." in capsys.readouterr().out
