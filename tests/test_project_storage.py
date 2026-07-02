"""Tests for the projects / project_members storage layer and the project ACL.

Runs against whichever backend ``--storage-backend`` selects (the ``backend``
fixture), so the SQLite and PostgreSQL implementations are exercised by the
same assertions.  The ACL tests monkeypatch ``auth.user_has_permission`` to
isolate the per-project ACL composition from full RBAC role setup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.core import auth

if TYPE_CHECKING:
    import pytest


class TestProjectStore:
    def test_create_and_get(self, backend: Any) -> None:
        backend.create_project("p1", "Research", "u1")
        proj = backend.get_project("p1")
        assert proj is not None
        assert proj["name"] == "Research"
        assert proj["owner_id"] == "u1"
        assert proj["visibility"] == "private"
        assert proj["state"] == "active"
        assert proj["parent_project_id"] is None

    def test_get_missing(self, backend: Any) -> None:
        assert backend.get_project("nope") is None

    def test_create_is_idempotent(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        backend.create_project("p1", "B", "u2")  # OR IGNORE / on-conflict — no overwrite
        proj = backend.get_project("p1")
        assert proj is not None
        assert proj["name"] == "A"

    def test_update_mutable_fields(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        assert backend.update_project("p1", name="B", visibility="public", state="archived")
        proj = backend.get_project("p1")
        assert proj is not None
        assert proj["name"] == "B"
        assert proj["visibility"] == "public"
        assert proj["state"] == "archived"

    def test_update_ignores_immutable_and_unknown(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        # owner_id is immutable; bogus is unknown — neither persists → no-op → False.
        assert not backend.update_project("p1", owner_id="u2", bogus="x")
        proj = backend.get_project("p1")
        assert proj is not None
        assert proj["owner_id"] == "u1"

    def test_delete_removes_project_and_members(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        backend.add_project_member("p1", "u2")
        assert backend.delete_project("p1")
        assert backend.get_project("p1") is None
        assert backend.list_project_members("p1") == []
        assert not backend.delete_project("p1")  # already gone

    def test_delete_purges_scoped_memory_only(self, backend: Any) -> None:
        # No FK cascade in the schema family, so delete_project must purge the
        # project's scope='project' memory itself — and ONLY that project's, not
        # a sibling project's nor other scopes' rows.
        backend.create_project("p1", "A", "u1")
        backend.create_project("p2", "B", "u1")
        backend.create_structured_memory("m1", "k", "", "general", "project", "p1", "v")
        backend.create_structured_memory("m2", "k", "", "general", "project", "p2", "v")
        backend.create_structured_memory("m3", "k", "", "general", "user", "u1", "v")
        assert backend.delete_project("p1")
        assert backend.get_structured_memory("m1") is None  # purged
        assert backend.get_structured_memory("m2") is not None  # sibling project intact
        assert backend.get_structured_memory("m3") is not None  # other scope intact


class TestProjectMembers:
    def test_add_list_is_member(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        backend.add_project_member("p1", "u2")
        backend.add_project_member("p1", "u3")
        backend.add_project_member("p1", "u2")  # idempotent
        assert backend.list_project_members("p1") == ["u2", "u3"]
        assert backend.is_project_member("p1", "u2")
        assert not backend.is_project_member("p1", "u9")

    def test_remove_member(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        backend.add_project_member("p1", "u2")
        assert backend.remove_project_member("p1", "u2")
        assert not backend.is_project_member("p1", "u2")
        assert not backend.remove_project_member("p1", "u2")  # already gone


class TestListProjectsForUser:
    def test_owner_member_public_visible_private_other_hidden(self, backend: Any) -> None:
        backend.create_project("owned", "Owned", "u1")
        backend.create_project("member", "Member", "u2")
        backend.add_project_member("member", "u1")
        backend.create_project("pub", "Public", "u3", visibility="public")
        backend.create_project("other", "Other", "u3")  # private, u1 not a member
        backend.create_project("arch", "Archived", "u1", state="archived")

        ids = {p["project_id"] for p in backend.list_projects_for_user("u1")}
        assert ids == {"owned", "member", "pub"}  # excludes "other" and "arch"

    def test_include_archived(self, backend: Any) -> None:
        backend.create_project("arch", "Archived", "u1", state="archived")
        ids = {p["project_id"] for p in backend.list_projects_for_user("u1", include_archived=True)}
        assert "arch" in ids


class TestUserCanAccessProject:
    def test_owner_has_full_access(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        assert auth.user_can_access_project("u1", "p1", write=True, storage=backend)
        assert auth.user_can_access_project("u1", "p1", write=False, storage=backend)

    def test_fail_closed_on_empty_and_missing(self, backend: Any) -> None:
        assert not auth.user_can_access_project("", "p1", write=False, storage=backend)
        assert not auth.user_can_access_project("u1", "", write=False, storage=backend)
        assert not auth.user_can_access_project("u1", "nope", write=False, storage=backend)

    def test_member_read_requires_capability(
        self, backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend.create_project("p1", "A", "u1")
        backend.add_project_member("p1", "u2")
        # Member but no project.read capability → denied.
        monkeypatch.setattr(auth, "user_has_permission", lambda *a, **k: False)
        assert not auth.user_can_access_project("u2", "p1", write=False, storage=backend)
        # Member with project.read → allowed.
        monkeypatch.setattr(auth, "user_has_permission", lambda *a, **k: True)
        assert auth.user_can_access_project("u2", "p1", write=False, storage=backend)

    def test_public_read_needs_capability_not_membership(
        self, backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend.create_project("p1", "A", "u1", visibility="public")
        monkeypatch.setattr(auth, "user_has_permission", lambda *a, **k: True)
        # Non-member with project.read can READ a public project...
        assert auth.user_can_access_project("stranger", "p1", write=False, storage=backend)
        # ...but cannot WRITE without membership.
        assert not auth.user_can_access_project("stranger", "p1", write=True, storage=backend)

    def test_private_non_member_denied(self, backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        backend.create_project("p1", "A", "u1")  # private
        monkeypatch.setattr(auth, "user_has_permission", lambda *a, **k: True)
        assert not auth.user_can_access_project("stranger", "p1", write=False, storage=backend)

    def test_write_requires_membership_even_with_capability(
        self, backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend.create_project("p1", "A", "u1")
        backend.add_project_member("p1", "u2")
        monkeypatch.setattr(auth, "user_has_permission", lambda *a, **k: True)
        assert auth.user_can_access_project("u2", "p1", write=True, storage=backend)
        # Non-member with the write capability is still denied.
        assert not auth.user_can_access_project("u9", "p1", write=True, storage=backend)

    def test_resolve_returns_name_state_and_both_bits(self, backend: Any) -> None:
        # The single-fetch resolver behind the wrapper surfaces name + state (so
        # the session constructor needn't re-fetch them) and both access bits.
        backend.create_project("p1", "Research", "u1")
        backend.update_project("p1", state="archived")
        acc = auth.resolve_project_access("u1", "p1", storage=backend)  # owner
        assert acc.can_read and acc.can_write
        assert acc.name == "Research"
        assert acc.state == "archived"
        deny = auth.resolve_project_access("u1", "nope", storage=backend)
        assert not deny.can_read and not deny.can_write
        assert deny.name == "" and deny.state == ""


class TestWorkstreamProjectId:
    """Phase 5: project_id rides the register_workstream → get_workstream path."""

    def test_register_persists_project_id(self, backend: Any) -> None:
        backend.register_workstream("ws1", user_id="u1", project_id="p1")
        row = backend.get_workstream("ws1")
        assert row is not None
        assert row["project_id"] == "p1"

    def test_register_without_project_is_null(self, backend: Any) -> None:
        backend.register_workstream("ws2", user_id="u1")
        row = backend.get_workstream("ws2")
        assert row is not None
        assert row.get("project_id") in (None, "")

    def test_empty_project_normalizes_to_null(self, backend: Any) -> None:
        backend.register_workstream("ws3", user_id="u1", project_id="")
        row = backend.get_workstream("ws3")
        assert row is not None
        assert row.get("project_id") in (None, "")

    def test_list_workstreams_projection_carries_project_id(self, backend: Any) -> None:
        # Phase 6: the persisted coordinator lane (_coordinator_rows) reads
        # project_id by NAME off a list_workstreams row, so the projection must
        # surface it — without the column the persisted lane drops the project.
        backend.register_workstream("wsL", user_id="u1", project_id="p9")
        rows = backend.list_workstreams(user_id="u1")
        row = next(r for r in rows if r._mapping["ws_id"] == "wsL")
        assert row._mapping["project_id"] == "p9"


class TestMemoryScopeLabels:
    """The admin Memories view resolves a memory's scope_id to a human label
    (project / workstream name, username) rather than showing the raw hex id."""

    def test_enrich_resolves_names_and_falls_back(self, backend: Any) -> None:
        from turnstone.console.server import _enrich_memory_scope_labels

        backend.create_project("p1", "Research", "u1")
        backend.create_user("u1", "alice", "Alice", "x")
        backend.register_workstream("ws1", user_id="u1", name="planning chat")
        rows: list[dict[str, Any]] = [
            {"scope": "project", "scope_id": "p1"},
            {"scope": "user", "scope_id": "u1"},
            {"scope": "coordinator", "scope_id": "u1"},  # coord scope_id is the user_id
            {"scope": "workstream", "scope_id": "ws1"},
            {"scope": "global", "scope_id": ""},  # no id → no label
            {"scope": "project", "scope_id": "gone"},  # missing → falls back to the id
        ]
        labels = [r["scope_label"] for r in _enrich_memory_scope_labels(rows, backend)]
        assert labels == ["Research", "alice", "alice", "planning chat", "", "gone"]


class TestProjectResourceQueries:
    def test_list_workstreams_for_project_scoped_and_ordered(self, backend: Any) -> None:
        import sqlalchemy as sa

        backend.create_project("p1", "A", "u1")
        backend.register_workstream("w-old", name="old", user_id="u1", project_id="p1")
        backend.register_workstream("w-new", name="new", user_id="u1", project_id="p1")
        backend.register_workstream("w-out", name="out", user_id="u1")
        # Force a deterministic ``updated`` ordering directly — same-second
        # registration timestamps would otherwise make ORDER BY updated
        # DESC a coin flip and the ordering assertion vacuous.
        with backend._engine.connect() as conn:  # noqa: SLF001
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'w-old'")
            )
            conn.commit()
        rows = backend.list_workstreams_for_project("p1")
        assert [r["ws_id"] for r in rows] == ["w-new", "w-old"]
        assert {"ws_id", "name", "title", "state", "kind", "updated", "node_id", "user_id"} <= set(
            rows[0]
        )

    def test_list_project_attachments_dedupes_to_first_ws(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        backend.register_workstream("w1", user_id="u1", project_id="p1")
        backend.register_workstream("w2", user_id="u1", project_id="p1")
        backend.save_attachment("a" * 64, "one.txt", "text/plain", 3, "text", b"abc")
        backend.save_attachment("b" * 64, "two.png", "image/png", 4, "image", b"pngx")
        m1 = backend.save_message("w1", "user", "first")
        backend.set_message_attachments("w1", m1, ["a" * 64])
        # Same blob referenced again from w2 + a second blob.
        m2 = backend.save_message("w2", "user", "second")
        backend.set_message_attachments("w2", m2, ["a" * 64, "b" * 64])
        atts = backend.list_project_attachments("p1")
        by_id = {a["attachment_id"]: a for a in atts}
        assert set(by_id) == {"a" * 64, "b" * 64}
        assert by_id["a" * 64]["ws_id"] == "w1"  # first reference wins
        assert by_id["b" * 64]["ws_id"] == "w2"
        assert by_id["a" * 64]["filename"] == "one.txt"
        assert "content" not in by_id["a" * 64]

    def test_list_project_attachments_skips_pruned_blob(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        backend.register_workstream("w1", user_id="u1", project_id="p1")
        m1 = backend.save_message("w1", "user", "ref to a gone blob")
        backend.set_message_attachments("w1", m1, ["c" * 64])  # never saved
        assert backend.list_project_attachments("p1") == []

    def test_list_project_attachments_empty_project(self, backend: Any) -> None:
        backend.create_project("p1", "A", "u1")
        assert backend.list_project_attachments("p1") == []
