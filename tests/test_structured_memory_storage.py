"""Tests for structured memory storage backend operations."""

import threading
from typing import Any

import pytest
import sqlalchemy as sa

from turnstone.core.storage._postgresql import PostgreSQLBackend
from turnstone.core.storage._utils import ProjectMemoryAuthorizationError


@pytest.fixture(autouse=True)
def _registered_workstream_scopes(backend):
    """Workstream-scoped rows always have a live durable parent."""
    backend.register_workstream("ws1", user_id="u1")
    backend.register_workstream("ws2", user_id="u2")


class TestCreateAndGet:
    def test_create_requires_non_empty_description(self, backend):
        import pytest

        for description in (None, "", "   "):
            with pytest.raises(ValueError, match="description is required"):
                backend.create_structured_memory(
                    "m1", "test_key", description, "general", "global", "", "data"
                )

    def test_create_and_get_by_id(self, backend):
        backend.create_structured_memory("m1", "test_key", "desc", "general", "global", "", "data")
        mem = backend.get_structured_memory("m1")
        assert mem is not None
        assert mem["name"] == "test_key"
        assert mem["content"] == "data"
        assert mem["type"] == "general"

    def test_get_nonexistent(self, backend):
        assert backend.get_structured_memory("nope") is None

    def test_get_by_name(self, backend):
        backend.create_structured_memory("m1", "mykey", "d", "general", "global", "", "val")
        mem = backend.get_structured_memory_by_name("mykey", "global", "")
        assert mem is not None
        assert mem["memory_id"] == "m1"

    def test_get_by_name_scoped(self, backend):
        backend.create_structured_memory("m1", "key", "d", "general", "global", "", "g")
        backend.create_structured_memory("m2", "key", "d", "general", "workstream", "ws1", "w")
        g = backend.get_structured_memory_by_name("key", "global", "")
        w = backend.get_structured_memory_by_name("key", "workstream", "ws1")
        assert g["content"] == "g"
        assert w["content"] == "w"

    def test_exact_name_operations_never_wildcard_an_empty_scope_id(self, backend):
        backend.create_structured_memory(
            "m-u1", "shared", "u1 hook", "general", "user", "u1", "u1 body"
        )
        backend.create_structured_memory(
            "m-u2", "shared", "u2 hook", "general", "user", "u2", "u2 body"
        )

        assert backend.get_structured_memory_by_name("shared", "user", "") is None
        assert backend.get_and_touch_structured_memory_by_name("shared", "user", "") is None
        assert not backend.delete_structured_memory("shared", "user", "")

        assert backend.count_structured_memories(scope="user") == 2
        assert {row["scope_id"] for row in backend.list_structured_memories(scope="user")} == {
            "u1",
            "u2",
        }
        for memory_id in ("m-u1", "m-u2"):
            row = backend.get_structured_memory(memory_id)
            assert row is not None
            assert row["access_count"] == 0

    def test_get_and_touch_returns_the_updated_full_row(self, backend):
        backend.create_structured_memory(
            "m1", "key", "hook", "reference", "user", "u1", "private body"
        )

        first = backend.get_and_touch_structured_memory_by_name("key", "user", "u1")
        second = backend.get_and_touch_structured_memory("m1")

        assert first is not None
        assert first["memory_id"] == "m1"
        assert first["content"] == "private body"
        assert first["access_count"] == 1
        assert first["last_accessed"]
        assert second is not None
        assert second["access_count"] == 2
        assert second["last_accessed"]

    def test_get_and_touch_miss_changes_nothing(self, backend):
        backend.create_structured_memory("m1", "key", "hook", "general", "global", "", "body")

        assert backend.get_and_touch_structured_memory_by_name("missing", "global", "") is None
        assert backend.get_and_touch_structured_memory("missing") is None

        row = backend.get_structured_memory("m1")
        assert row is not None
        assert row["access_count"] == 0
        assert not row["last_accessed"]


def test_role_override_mutations_require_existing_role(backend):
    with pytest.raises(ValueError, match="does not exist"):
        backend.set_role_overrides("missing-role", {"project.read"}, set())
    with pytest.raises(ValueError, match="does not exist"):
        backend.clear_role_overrides("missing-role")
    assert backend.list_role_overrides("missing-role") == []


class TestSaveUpsert:
    """``save_structured_memory`` upserts by (name, scope, scope_id).

    A second save of the same key must UPDATE in place, not surface the
    ``uq_smem_name_scope`` unique-constraint violation.  These run on whichever
    backend ``--storage-backend`` selects, so the PostgreSQL path is covered in
    CI -- the session-level memory tests only exercise SQLite (via ``tmp_db``),
    which is where this path previously had no cross-backend coverage.
    """

    def test_duplicate_create_raises_integrity_error(self, backend):
        """The unique constraint the upsert's ON CONFLICT targets actually fires."""
        import pytest
        import sqlalchemy as sa

        backend.create_structured_memory("m1", "dup", "Test memory", "general", "global", "", "a")
        with pytest.raises(sa.exc.IntegrityError):
            backend.create_structured_memory(
                "m2", "dup", "Test memory", "general", "global", "", "b"
            )


class TestProjectActingPrincipalAuthorization:
    """Project ACL/RBAC is enforced by the memory transaction itself."""

    @staticmethod
    def _seed(backend):
        for user_id in ("owner", "reader", "member", "other"):
            backend.create_user(user_id, user_id, user_id.title(), "hash")
        backend.create_project("project-auth", "Project", "owner")
        backend.create_structured_memory(
            "project-memory",
            "runbook",
            "Project runbook",
            "reference",
            "project",
            "project-auth",
            "private body",
        )

    def test_owner_can_mutate_and_fetch(self, backend):
        self._seed(backend)
        row, was_update = backend.upsert_structured_memory(
            "replacement-id",
            "runbook",
            "Updated project runbook",
            None,
            "project",
            "project-auth",
            "updated body",
            acting_principal_id="owner",
        )
        assert was_update is True
        assert row["memory_id"] == "project-memory"

        fetched = backend.get_and_touch_structured_memory_by_name(
            "runbook",
            "project",
            "project-auth",
            acting_principal_id="owner",
        )
        assert fetched is not None
        assert fetched["content"] == "updated body"
        assert (
            backend.delete_structured_memory_returning(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="owner",
            )
            is not None
        )

    def test_read_only_member_cannot_write_or_delete(self, backend):
        self._seed(backend)
        backend.create_role("project-reader", "reader", "Reader", "project.read", False)
        backend.assign_role("reader", "project-reader")
        backend.add_project_member("project-auth", "reader")

        assert (
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="reader",
            )
            is not None
        )
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.upsert_structured_memory(
                "replacement-id",
                "runbook",
                "Changed",
                None,
                "project",
                "project-auth",
                "changed",
                acting_principal_id="reader",
            )
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.delete_structured_memory_returning(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="reader",
            )
        assert backend.get_structured_memory("project-memory") is not None

    def test_grants_revokes_and_write_policy_share_one_decision(self, backend):
        self._seed(backend)
        backend.create_role("builtin-project", "builtin-project", "Project", "", True)
        backend.assign_role("member", "builtin-project")
        backend.add_project_member("project-auth", "member")

        # A revoke cannot accidentally confer a permission absent from baseline.
        backend.set_role_overrides("builtin-project", set(), {"project.read"})
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="member",
            )

        # An override grant is realized by the guarded read transaction.
        backend.set_role_overrides("builtin-project", {"project.read"}, set())
        assert (
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="member",
            )
            is not None
        )
        backend.clear_role_overrides("builtin-project")
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="member",
            )

        # Read and write are independent effective capabilities. Replacement
        # removes the unrelated grant before installing the requested revoke.
        assert backend.update_role("builtin-project", permissions="project.read,project.write")
        backend.set_role_overrides("builtin-project", {"admin.audit"}, set())
        backend.set_role_overrides("builtin-project", set(), {"project.read"})
        overrides = backend.list_role_overrides("builtin-project")
        assert [(row["role_id"], row["permission"], row["action"]) for row in overrides] == [
            ("builtin-project", "project.read", "revoke")
        ]
        backend.upsert_structured_memory(
            "replacement-id",
            "runbook",
            "Write remains authorized",
            None,
            "project",
            "project-auth",
            "updated through write-only permission",
            acting_principal_id="member",
        )
        backend.set_role_overrides("builtin-project", set(), {"project.write"})
        assert (
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="member",
            )
            is not None
        )
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.upsert_structured_memory(
                "replacement-id",
                "runbook",
                "Denied write",
                None,
                "project",
                "project-auth",
                "must not land",
                acting_principal_id="member",
            )

    def test_role_delete_cleans_authority_without_touching_unrelated_role(self, backend):
        self._seed(backend)
        backend.create_role("doomed", "doomed", "Doomed", "project.read", True)
        backend.create_role("survivor", "survivor", "Survivor", "project.read", True)
        backend.assign_role("member", "doomed")
        backend.assign_role("other", "survivor")
        backend.add_project_member("project-auth", "member")
        backend.set_role_overrides("doomed", {"project.write"}, set())

        assert backend.delete_role("doomed") is True
        assert backend.get_role("doomed") is None
        assert backend.list_role_overrides("doomed") == []
        assert backend.list_user_roles("member") == []
        assert backend.get_role("survivor") is not None
        assert {row["role_id"] for row in backend.list_user_roles("other")} == {"survivor"}
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-auth",
                acting_principal_id="member",
            )

    @pytest.mark.parametrize(
        "operation",
        ["get", "find", "list", "search", "index", "count"],
    )
    def test_non_member_cannot_read_project_surface(self, backend, operation):
        self._seed(backend)
        scopes = [("global", ""), ("project", "project-auth")]

        with pytest.raises(ProjectMemoryAuthorizationError):
            if operation == "get":
                backend.get_and_touch_structured_memory_by_name(
                    "runbook",
                    "project",
                    "project-auth",
                    acting_principal_id="stranger",
                )
            elif operation == "find":
                backend.find_structured_memory_scopes(
                    "runbook",
                    scopes,
                    acting_principal_id="stranger",
                )
            elif operation == "list":
                backend.list_visible_structured_memories(
                    scopes,
                    acting_principal_id="stranger",
                )
            elif operation == "search":
                backend.search_visible_structured_memories(
                    "runbook",
                    scopes,
                    acting_principal_id="stranger",
                )
            elif operation == "index":
                backend.list_visible_memory_index_entries(
                    scopes,
                    acting_principal_id="stranger",
                )
            else:
                backend.count_structured_memories(
                    scope="project",
                    scope_id="project-auth",
                    acting_principal_id="stranger",
                )

        row = backend.get_structured_memory("project-memory")
        assert row is not None
        assert row["access_count"] == 0


class TestSaveUpsertBehavior:
    def test_save_same_key_updates_in_place(self, backend):
        from turnstone.core.memory import save_structured_memory

        row1, was_update1 = save_structured_memory(
            "upsert_key", "v1", description="first description", scope="global"
        )
        assert row1 and was_update1 is False  # inserted

        row2, was_update2 = save_structured_memory(
            "upsert_key", "v2", description="updated description", scope="global"
        )
        assert row2 and was_update2 is True  # updated in place
        assert row2["memory_id"] == row1["memory_id"]  # same row, not a duplicate
        assert "content" not in row2
        assert backend.get_structured_memory(row2["memory_id"])["content"] == "v2"
        names = [r["name"] for r in backend.list_structured_memories(scope="global")]
        assert names.count("upsert_key") == 1

    def test_save_same_key_requires_and_updates_description(self, backend):
        from turnstone.core.memory import save_structured_memory, save_structured_memory_strict

        save_structured_memory(
            "meta_key", "c1", description="orig desc", mem_type="fact", scope="global"
        )
        # Every update must describe the revised memory; type can still be omitted.
        import pytest

        with pytest.raises(ValueError, match="description is required"):
            save_structured_memory_strict("meta_key", "c2", description=None, scope="global")
        save_structured_memory("meta_key", "c2", description="revised description", scope="global")
        row = backend.get_structured_memory_by_name("meta_key", "global", "")
        assert row["content"] == "c2"
        assert row["description"] == "revised description"
        assert row["type"] == "fact"

    def test_upsert_method_updates_in_place_no_raise(self, backend):
        """The atomic storage primitive updates in place on a key conflict and
        returns (row, was_update) carrying the existing row's id -- no
        IntegrityError (which a second create_structured_memory would raise)."""
        backend.create_structured_memory("m1", "k", "desc", "fact", "global", "", "v1")
        row, was_update = backend.upsert_structured_memory(
            "m2", "k", "newdesc", "note", "global", "", "v2"
        )
        assert was_update is True
        assert row["memory_id"] == "m1"  # existing row id, not the supplied "m2"
        assert "content" not in row
        assert backend.get_structured_memory("m1")["content"] == "v2"
        assert row["description"] == "newdesc"
        assert row["type"] == "note"
        names = [r["name"] for r in backend.list_structured_memories(scope="global")]
        assert names.count("k") == 1

    def test_upsert_requires_description_and_preserves_omitted_type(self, backend):
        """Description is mandatory; an omitted type keeps the stored value."""
        import pytest

        backend.create_structured_memory("m1", "k", "keepdesc", "fact", "global", "", "v1")
        with pytest.raises(ValueError, match="description is required"):
            backend.upsert_structured_memory("m2", "k", None, None, "global", "", "v2")
        with pytest.raises(ValueError, match="description is required"):
            backend.upsert_structured_memory("m2", "k", "   ", None, "global", "", "v2")

        row, _ = backend.upsert_structured_memory(
            "m2", "k", "new description", None, "global", "", "v2"
        )
        assert "content" not in row
        assert backend.get_structured_memory("m1")["content"] == "v2"
        assert row["description"] == "new description"
        assert row["type"] == "fact"
        row2, _ = backend.upsert_structured_memory(
            "m3", "k", "final description", "general", "global", "", "v3"
        )
        assert row2["description"] == "final description"
        assert row2["type"] == "general"

    def test_active_project_guard_accepts_only_active_project(self, backend):
        import pytest

        backend.create_project("active", "Active", "u1")
        row, was_update = backend.upsert_structured_memory(
            "m1",
            "guarded",
            "guarded description",
            None,
            "project",
            "active",
            "value",
            require_active_project=True,
        )
        assert row["scope_id"] == "active"
        assert was_update is False

        backend.create_project("archived", "Archived", "u1", state="archived")
        for project_id in ("archived", "missing"):
            with pytest.raises(ValueError, match="missing, archived"):
                backend.upsert_structured_memory(
                    f"m-{project_id}",
                    "guarded",
                    "guarded description",
                    None,
                    "project",
                    project_id,
                    "value",
                    require_active_project=True,
                )
            assert backend.get_structured_memory_by_name("guarded", "project", project_id) is None

    def test_active_project_guard_rejects_non_project_scope(self, backend):
        import pytest

        with pytest.raises(ValueError, match="requires project scope"):
            backend.upsert_structured_memory(
                "m1",
                "guarded",
                "guarded description",
                None,
                "global",
                "",
                "value",
                require_active_project=True,
            )


class TestDelete:
    def test_delete_existing(self, backend):
        backend.create_structured_memory("m1", "k", "d", "general", "global", "", "data")
        assert backend.delete_structured_memory("k", "global", "")
        assert backend.get_structured_memory("m1") is None

    def test_delete_nonexistent(self, backend):
        assert not backend.delete_structured_memory("nope", "global", "")

    def test_delete_scoped(self, backend):
        backend.create_structured_memory("m1", "k", "d", "general", "workstream", "ws1", "data")
        assert not backend.delete_structured_memory("k", "global", "")
        assert backend.delete_structured_memory("k", "workstream", "ws1")

    def test_delete_returning_is_atomic_and_truthful(self, backend):
        backend.create_structured_memory(
            "m1", "k", "description", "reference", "user", "u1", "data"
        )

        deleted = backend.delete_structured_memory_returning("k", "user", "u1")

        assert deleted is not None
        assert deleted["memory_id"] == "m1"
        assert deleted["description"] == "description"
        assert deleted["type"] == "reference"
        assert backend.get_structured_memory("m1") is None
        assert backend.delete_structured_memory_returning("k", "user", "u1") is None

    def test_delete_by_id_returning_is_atomic_and_truthful(self, backend):
        backend.create_structured_memory("m1", "k", "Test memory", "general", "global", "", "data")

        deleted = backend.delete_structured_memory_by_id_returning("m1")

        assert deleted is not None
        assert deleted["name"] == "k"
        assert backend.get_structured_memory("m1") is None
        assert backend.delete_structured_memory_by_id_returning("m1") is None


class TestFindScopes:
    def test_finds_only_requested_same_name_scopes(self, backend):
        backend.create_structured_memory("m1", "same", "Test memory", "general", "global", "", "g")
        backend.create_structured_memory(
            "m2", "same", "Test memory", "general", "user", "u1", "own"
        )
        backend.create_structured_memory(
            "m3", "same", "Test memory", "general", "user", "victim", "secret"
        )
        backend.create_structured_memory(
            "m4", "other", "Test memory", "general", "workstream", "ws1", "other"
        )

        found = backend.find_structured_memory_scopes(
            "same", [("global", ""), ("user", "u1"), ("workstream", "ws1")]
        )

        assert set(found) == {("global", ""), ("user", "u1")}


class TestList:
    def test_list_all(self, backend):
        backend.create_structured_memory("m1", "a", "Test memory", "general", "global", "", "1")
        backend.create_structured_memory("m2", "b", "Test memory", "user", "global", "", "2")
        mems = backend.list_structured_memories()
        assert len(mems) == 2

    def test_list_by_type(self, backend):
        backend.create_structured_memory("m1", "a", "Test memory", "general", "global", "", "1")
        backend.create_structured_memory("m2", "b", "Test memory", "user", "global", "", "2")
        mems = backend.list_structured_memories(mem_type="user")
        assert len(mems) == 1
        assert mems[0]["name"] == "b"

    def test_list_by_scope(self, backend):
        backend.create_structured_memory("m1", "a", "Test memory", "general", "global", "", "1")
        backend.create_structured_memory(
            "m2", "b", "Test memory", "general", "workstream", "ws1", "2"
        )
        mems = backend.list_structured_memories(scope="workstream")
        assert len(mems) == 1

    def test_list_respects_limit(self, backend):
        for i in range(10):
            backend.create_structured_memory(
                f"m{i}", f"k{i}", "Test memory", "general", "global", "", f"{i}"
            )
        mems = backend.list_structured_memories(limit=3)
        assert len(mems) == 3


class TestSearch:
    def test_search_by_name(self, backend):
        backend.create_structured_memory(
            "m1", "database_config", "Test memory", "general", "global", "", "pg"
        )
        backend.create_structured_memory(
            "m2", "api_key", "Test memory", "general", "global", "", "secret"
        )
        results = backend.search_structured_memories("database")
        assert len(results) == 1
        assert results[0]["name"] == "database_config"

    def test_search_does_not_match_private_content(self, backend):
        backend.create_structured_memory(
            "m1", "a", "Test memory", "general", "global", "", "postgresql host"
        )
        results = backend.search_structured_memories("postgresql")
        assert results == []

    def test_search_empty_lists_all(self, backend):
        backend.create_structured_memory("m1", "a", "Test memory", "general", "global", "", "1")
        backend.create_structured_memory("m2", "b", "Test memory", "general", "global", "", "2")
        results = backend.search_structured_memories("")
        assert len(results) == 2


class TestCount:
    def test_count_all(self, backend):
        backend.create_structured_memory("m1", "a", "Test memory", "general", "global", "", "1")
        backend.create_structured_memory("m2", "b", "Test memory", "general", "global", "", "2")
        assert backend.count_structured_memories() == 2

    def test_count_by_scope(self, backend):
        backend.create_structured_memory("m1", "a", "Test memory", "general", "global", "", "1")
        backend.create_structured_memory(
            "m2", "b", "Test memory", "general", "workstream", "ws1", "2"
        )
        assert backend.count_structured_memories(scope="global") == 1
        assert backend.count_structured_memories(scope="workstream") == 1


class TestSearchOrOfTerms:
    """Verify that multi-word search uses OR-of-terms (any term matches → row included)."""

    def test_single_matching_term_in_multi_word_query(self, backend):
        """Memory with content 'apple' found when query is 'apple banana cherry'."""
        backend.create_structured_memory(
            "m1", "apple_mem", "Test memory", "general", "global", "", "apple"
        )
        backend.create_structured_memory(
            "m2", "other_mem", "Test memory", "general", "global", "", "grape"
        )

        results = backend.search_structured_memories("apple banana cherry")
        names = {r["name"] for r in results}
        assert "apple_mem" in names  # matches "apple" — OR-of-terms keeps it
        assert "other_mem" not in names  # "grape" matches nothing in the query

    def test_partial_overlap_across_memories(self, backend):
        """Each memory matches one of three terms; all three are returned."""
        backend.create_structured_memory(
            "m1", "alpha_doc", "Test memory", "general", "global", "", "alpha"
        )
        backend.create_structured_memory(
            "m2", "beta_doc", "Test memory", "general", "global", "", "beta"
        )
        backend.create_structured_memory(
            "m3", "gamma_doc", "Test memory", "general", "global", "", "gamma"
        )
        backend.create_structured_memory(
            "m4", "unrelated", "Test memory", "general", "global", "", "delta"
        )

        results = backend.search_structured_memories("alpha beta gamma")
        names = {r["name"] for r in results}
        assert "alpha_doc" in names
        assert "beta_doc" in names
        assert "gamma_doc" in names
        assert "unrelated" not in names  # "delta" doesn't appear in the query

    def test_scope_filter_preserved(self, backend):
        """OR-of-terms search still respects scope / scope_id filters."""
        backend.create_structured_memory(
            "m1", "ws1_note", "Test memory", "general", "workstream", "ws1", "info"
        )
        backend.create_structured_memory(
            "m2", "ws2_note", "Test memory", "general", "workstream", "ws2", "info"
        )
        backend.create_structured_memory(
            "m3", "global_note", "Test memory", "general", "global", "", "info"
        )

        results = backend.search_structured_memories("note", scope="workstream", scope_id="ws1")
        names = {r["name"] for r in results}
        assert "ws1_note" in names
        assert "ws2_note" not in names
        assert "global_note" not in names

    def test_term_cap_normalizes_unbounded_query(self, backend):
        """A multi-KB query collapses to <= MAX terms (de-dupe + length filter)."""
        backend.create_structured_memory(
            "m1", "alpha_doc", "Test memory", "general", "global", "", "alpha"
        )
        backend.create_structured_memory(
            "m2", "other_doc", "Test memory", "general", "global", "", "irrelevant"
        )

        # Build a noisy query: same word repeated, plus 1-char tokens that
        # the normalizer drops, plus the actual signal "alpha".
        noisy = " ".join(["x"] * 100 + ["alpha"] * 50)
        results = backend.search_structured_memories(noisy)
        names = {r["name"] for r in results}
        assert "alpha_doc" in names


class TestVisibleStructuredMemories:
    """Single-query union helpers used by the composition path."""

    def test_list_visible_unions_global_workstream_user(self, backend):
        backend.create_structured_memory(
            "m1", "g_note", "Test memory", "general", "global", "", "g"
        )
        backend.create_structured_memory(
            "m2", "ws_note", "Test memory", "general", "workstream", "ws1", "w"
        )
        backend.create_structured_memory(
            "m3", "u_note", "Test memory", "general", "user", "u1", "u"
        )
        backend.create_structured_memory(
            "m4", "other_ws", "Test memory", "general", "workstream", "ws2", "x"
        )

        scopes = [("global", ""), ("workstream", "ws1"), ("user", "u1")]
        rows = backend.list_visible_structured_memories(scopes)
        names = {r["name"] for r in rows}
        assert names == {"g_note", "ws_note", "u_note"}  # ws2 excluded

    def test_search_visible_unions_scopes_and_terms(self, backend):
        backend.create_structured_memory(
            "m1", "g_alpha", "Test memory", "general", "global", "", "alpha"
        )
        backend.create_structured_memory(
            "m2", "ws_beta", "Test memory", "general", "workstream", "ws1", "beta"
        )
        backend.create_structured_memory(
            "m3", "ws_other", "Test memory", "general", "workstream", "ws2", "alpha"
        )

        scopes = [("global", ""), ("workstream", "ws1")]
        rows = backend.search_visible_structured_memories("alpha beta", scopes)
        names = {r["name"] for r in rows}
        assert "g_alpha" in names  # global, matches "alpha"
        assert "ws_beta" in names  # ws1, matches "beta"
        assert "ws_other" not in names  # ws2 -> outside visibility

    def test_visible_helpers_handle_empty_scopes(self, backend):
        backend.create_structured_memory(
            "m1", "anything", "Test memory", "general", "global", "", "x"
        )
        assert backend.list_visible_structured_memories([]) == []
        assert backend.search_visible_structured_memories("x", []) == []

    def test_visible_helpers_never_wildcard_an_empty_scope_id(self, backend):
        backend.create_structured_memory(
            "m-u1", "shared", "u1 hook", "general", "user", "u1", "u1 body"
        )
        backend.create_structured_memory(
            "m-u2", "shared", "u2 hook", "general", "user", "u2", "u2 body"
        )

        assert backend.list_visible_structured_memories([("user", "")]) == []
        assert backend.search_visible_structured_memories("shared", [("user", "")]) == []

    def test_snapshot_capture_uses_the_same_exact_scope_pairs(self, backend):
        backend.create_structured_memory(
            "m-global", "canonical", "visible hook", "general", "global", "", "body"
        )
        backend.create_structured_memory(
            "m-malformed",
            "foreign_global",
            "must stay hidden",
            "general",
            "global",
            "tenant-b",
            "body",
        )

        visible = backend.list_visible_memory_index_entries([("global", "")])
        snapshot = backend.acquire_memory_index_snapshot("ws1", "u1")

        assert [row["memory_id"] for row in visible] == ["m-global"]
        assert snapshot is not None
        assert snapshot["entry_count"] == 1
        assert "canonical" in snapshot["content"]
        assert "foreign_global" not in snapshot["content"]

    def test_admin_search_retains_optional_scope_id_filter(self, backend):
        backend.create_structured_memory(
            "m-u1", "shared", "u1 hook", "general", "user", "u1", "u1 body"
        )
        backend.create_structured_memory(
            "m-u2", "shared", "u2 hook", "general", "user", "u2", "u2 body"
        )

        rows = backend.search_structured_memories("shared", scope="user", scope_id="")

        assert {row["memory_id"] for row in rows} == {"m-u1", "m-u2"}


class TestStableOrderingOnTimestampTies:
    """When two memories share an `updated` timestamp, secondary sort on
    memory_id keeps the order deterministic across calls.

    `updated` is second-precision, so independent writes can share a timestamp.
    Without a tie-breaker BM25 input order shuffles run-to-run, busting the
    LLM-side prompt cache.
    """

    def _seed_with_shared_timestamp(self, backend):
        # Create three memories then force their `updated` columns equal —
        # mirrors the real-world case where several writes land in one second.
        for mid in ("zebra_id", "apple_id", "mango_id"):
            backend.create_structured_memory(
                mid, f"name_{mid}", "shared memory", "general", "global", "", "private body"
            )
        import sqlalchemy as sa

        with backend._conn() as conn:
            conn.execute(sa.text("UPDATE structured_memories SET updated = '2024-01-01T00:00:00'"))
            conn.commit()

    def test_list_stable_order_under_tied_updated(self, backend):
        self._seed_with_shared_timestamp(backend)
        first = [r["memory_id"] for r in backend.list_structured_memories()]
        second = [r["memory_id"] for r in backend.list_structured_memories()]
        # Deterministic across calls AND sorted by memory_id ASC for ties
        assert first == second
        assert first == ["apple_id", "mango_id", "zebra_id"]

    def test_search_stable_order_under_tied_updated(self, backend):
        self._seed_with_shared_timestamp(backend)
        first = [r["memory_id"] for r in backend.search_structured_memories("shared")]
        second = [r["memory_id"] for r in backend.search_structured_memories("shared")]
        assert first == second
        assert first == ["apple_id", "mango_id", "zebra_id"]

    def test_visible_search_stable_order_under_tied_updated(self, backend):
        self._seed_with_shared_timestamp(backend)
        scopes = [("global", "")]
        first = [
            r["memory_id"] for r in backend.search_visible_structured_memories("shared", scopes)
        ]
        second = [
            r["memory_id"] for r in backend.search_visible_structured_memories("shared", scopes)
        ]
        assert first == second
        assert first == ["apple_id", "mango_id", "zebra_id"]


def _postgres_blocking_pids(backend: PostgreSQLBackend, pid: int) -> list[int]:
    with backend._engine.connect() as conn:
        return list(
            conn.execute(
                sa.text("SELECT pg_blocking_pids(:pid)"),
                {"pid": pid},
            ).scalar_one()
        )


def test_postgresql_delete_user_and_oidc_reconcile_share_lock_order(backend) -> None:
    """User deletion wins cleanly; OIDC reconciliation observes the deletion."""
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    backend.create_user("member", "member", "Member", "hash")
    backend.create_role("role-a", "role-a", "Role A", "project.read", False)
    backend.assign_role("member", "role-a", "oidc")

    delete_locked = threading.Event()
    release_delete = threading.Event()
    reconcile_attempted = threading.Event()
    delete_pid: list[int] = []
    reconcile_pid: list[int] = []
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []

    def before_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "oidc-reconcile"
            and "FROM users" in statement
            and "FOR UPDATE" in statement
        ):
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            reconcile_pid.append(int(cursor.connection.info.backend_pid))
            reconcile_attempted.set()

    def after_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "delete-user"
            and "FROM users" in statement
            and "FOR UPDATE" in statement
        ):
            delete_pid.append(int(cursor.connection.info.backend_pid))
            delete_locked.set()
            if not release_delete.wait(timeout=10):
                raise AssertionError("user deletion was not released")

    def delete() -> None:
        try:
            outcomes["deleted"] = backend.delete_user("member")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def reconcile() -> None:
        try:
            backend.replace_oidc_roles("member", {"role-a"})
        except ValueError as exc:
            outcomes["reconcile_error"] = str(exc)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    sa.event.listen(backend._engine, "before_cursor_execute", before_cursor_execute)
    sa.event.listen(backend._engine, "after_cursor_execute", after_cursor_execute)
    delete_thread = threading.Thread(target=delete, name="delete-user")
    reconcile_thread = threading.Thread(target=reconcile, name="oidc-reconcile")
    try:
        delete_thread.start()
        assert delete_locked.wait(timeout=10), "delete never acquired the user lock"
        reconcile_thread.start()
        assert reconcile_attempted.wait(timeout=10), "reconcile never attempted the user lock"
        assert delete_pid and reconcile_pid
        for _attempt in range(1_000):
            if delete_pid[0] in _postgres_blocking_pids(backend, reconcile_pid[0]):
                break
        else:
            raise AssertionError("OIDC reconcile was not blocked by user deletion")
    finally:
        release_delete.set()
        delete_thread.join(timeout=10)
        reconcile_thread.join(timeout=10)
        sa.event.remove(backend._engine, "before_cursor_execute", before_cursor_execute)
        sa.event.remove(backend._engine, "after_cursor_execute", after_cursor_execute)

    assert not delete_thread.is_alive()
    assert not reconcile_thread.is_alive()
    assert errors == []
    assert outcomes == {
        "deleted": True,
        "reconcile_error": "user 'member' does not exist",
    }
    assert backend.list_user_roles("member") == []


@pytest.mark.parametrize("winner", ["assign", "delete"])
def test_postgresql_assign_role_and_delete_user_share_lock_order(backend, winner) -> None:
    """Whichever user-anchored mutation wins leaves no orphan assignment."""
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    backend.create_user("member", "member", "Member", "hash")
    backend.create_role("role-a", "role-a", "Role A", "project.read", False)

    winner_locked = threading.Event()
    release_winner = threading.Event()
    loser_attempted = threading.Event()
    winner_pid: list[int] = []
    loser_pid: list[int] = []
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []
    winner_thread_name = f"{winner}-winner"
    loser_thread_name = "delete-loser" if winner == "assign" else "assign-loser"

    def before_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == loser_thread_name
            and "FROM users" in statement
            and "FOR UPDATE" in statement
        ):
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            loser_pid.append(int(cursor.connection.info.backend_pid))
            loser_attempted.set()

    def after_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == winner_thread_name
            and "FROM users" in statement
            and "FOR UPDATE" in statement
        ):
            winner_pid.append(int(cursor.connection.info.backend_pid))
            winner_locked.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError("winning authority mutation was not released")

    def assign() -> None:
        try:
            backend.assign_role("member", "role-a", "admin-ui")
            outcomes["assigned"] = True
        except ValueError as exc:
            outcomes["assign_error"] = str(exc)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def delete() -> None:
        try:
            outcomes["deleted"] = backend.delete_user("member")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    assign_thread = threading.Thread(
        target=assign,
        name="assign-winner" if winner == "assign" else "assign-loser",
    )
    delete_thread = threading.Thread(
        target=delete,
        name="delete-winner" if winner == "delete" else "delete-loser",
    )
    first_thread = assign_thread if winner == "assign" else delete_thread
    second_thread = delete_thread if winner == "assign" else assign_thread
    sa.event.listen(backend._engine, "before_cursor_execute", before_cursor_execute)
    sa.event.listen(backend._engine, "after_cursor_execute", after_cursor_execute)
    try:
        first_thread.start()
        assert winner_locked.wait(timeout=10), "winner never acquired the user lock"
        second_thread.start()
        assert loser_attempted.wait(timeout=10), "loser never attempted the user lock"
        assert winner_pid and loser_pid
        for _attempt in range(1_000):
            if winner_pid[0] in _postgres_blocking_pids(backend, loser_pid[0]):
                break
        else:
            raise AssertionError("loser was not blocked by the winning user lock")
    finally:
        release_winner.set()
        first_thread.join(timeout=10)
        second_thread.join(timeout=10)
        sa.event.remove(backend._engine, "before_cursor_execute", before_cursor_execute)
        sa.event.remove(backend._engine, "after_cursor_execute", after_cursor_execute)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    if winner == "assign":
        assert outcomes == {"assigned": True, "deleted": True}
    else:
        assert outcomes == {
            "deleted": True,
            "assign_error": "user 'member' does not exist",
        }
    assert backend.get_user("member") is None
    assert backend.list_user_roles("member") == []


def test_postgresql_permission_reader_locks_roles_in_canonical_order(backend) -> None:
    """A reader queues on later roles only after locking earlier role ids."""
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    backend.create_user("member", "member", "Member", "hash")
    # Insert in reverse lexical order so an unordered scan does not
    # accidentally inherit the canonical order from insertion order.
    backend.create_role("role-z", "role-z", "Role Z", "", False)
    backend.create_role("role-a", "role-a", "Role A", "project.read", False)
    backend.assign_role("member", "role-z", "oidc")
    backend.assign_role("member", "role-a", "oidc")
    backend.create_project("project-lock-order", "Project", "owner")
    backend.add_project_member("project-lock-order", "member")
    backend.create_structured_memory(
        "memory-lock-order",
        "runbook",
        "Runbook",
        "general",
        "project",
        "project-lock-order",
        "body",
    )

    blocker_conn = backend._engine.connect()
    blocker_tx = blocker_conn.begin()
    blocker_pid = int(blocker_conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one())
    blocker_conn.execute(
        sa.text("SELECT role_id FROM roles WHERE role_id = :role_id FOR UPDATE"),
        {"role_id": "role-z"},
    ).fetchone()

    reader_attempted = threading.Event()
    writer_attempted = threading.Event()
    reader_pid: list[int] = []
    writer_pid: list[int] = []
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []

    def before_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        thread_name = threading.current_thread().name
        if (
            thread_name == "permission-reader"
            and "FROM user_roles JOIN roles" in statement
            and "FOR SHARE OF roles" in statement
        ):
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            reader_pid.append(int(cursor.connection.info.backend_pid))
            reader_attempted.set()
        elif (
            thread_name == "oidc-writer"
            and "SELECT roles.role_id" in statement
            and "ORDER BY roles.role_id" in statement
            and "FOR UPDATE" in statement
        ):
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            writer_pid.append(int(cursor.connection.info.backend_pid))
            writer_attempted.set()

    def read() -> None:
        try:
            outcomes["memory"] = backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-lock-order",
                acting_principal_id="member",
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def reconcile() -> None:
        try:
            outcomes["reconcile"] = backend.replace_oidc_roles("member", {"role-a", "role-z"})
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    sa.event.listen(backend._engine, "before_cursor_execute", before_cursor_execute)
    reader_thread = threading.Thread(target=read, name="permission-reader")
    writer_thread = threading.Thread(target=reconcile, name="oidc-writer")
    try:
        reader_thread.start()
        assert reader_attempted.wait(timeout=10), "reader never attempted its role locks"
        assert reader_pid
        for _attempt in range(1_000):
            if blocker_pid in _postgres_blocking_pids(backend, reader_pid[0]):
                break
        else:
            raise AssertionError("reader was not blocked on the later role")

        writer_thread.start()
        assert writer_attempted.wait(timeout=10), "OIDC writer never attempted its role locks"
        assert writer_pid
        for _attempt in range(1_000):
            if reader_pid[0] in _postgres_blocking_pids(backend, writer_pid[0]):
                break
        else:
            raise AssertionError("writer was not blocked by the reader's earlier role lock")
    finally:
        if blocker_tx.is_active:
            blocker_tx.commit()
        blocker_conn.close()
        reader_thread.join(timeout=10)
        writer_thread.join(timeout=10)
        sa.event.remove(backend._engine, "before_cursor_execute", before_cursor_execute)

    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert errors == []
    assert outcomes["memory"] is not None
    assert outcomes["reconcile"] == (set(), set())


@pytest.mark.parametrize(
    (
        "scenario",
        "baseline",
        "initial_grants",
        "operation",
        "mutation",
        "expected_overrides",
    ),
    [
        (
            "absent-to-revoke",
            "project.read",
            set(),
            "read",
            "revoke-read",
            [("project.read", "revoke")],
        ),
        (
            "existing-grant-to-clear",
            "",
            {"project.read"},
            "read",
            "clear",
            [],
        ),
        (
            "unrelated-grant-to-revoke",
            "project.read",
            {"project.write"},
            "read",
            "revoke-read",
            [("project.read", "revoke")],
        ),
        (
            "write-guard",
            "project.write",
            set(),
            "write",
            "revoke-write",
            [("project.write", "revoke")],
        ),
        (
            "role-delete",
            "project.read",
            {"project.write"},
            "read",
            "delete",
            [],
        ),
    ],
)
def test_postgresql_role_mutation_waits_for_guarded_project_operation(
    backend,
    scenario: str,
    baseline: str,
    initial_grants: set[str],
    operation: str,
    mutation: str,
    expected_overrides: list[tuple[str, str]],
) -> None:
    """Concrete role rows serialize ACL decisions with override/delete writes."""
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    role_id = f"role-{scenario}"
    project_id = f"project-{scenario}"
    backend.create_user("member", "member", "Member", "hash")
    backend.create_role(role_id, role_id, role_id, baseline, True)
    backend.assign_role("member", role_id)
    backend.create_project(project_id, project_id, "owner")
    backend.add_project_member(project_id, "member")
    backend.create_structured_memory(
        f"memory-{scenario}",
        "runbook",
        "Runbook",
        "general",
        "project",
        project_id,
        "body",
    )
    if initial_grants:
        backend.set_role_overrides(role_id, initial_grants, set())

    auth_locked = threading.Event()
    release_auth = threading.Event()
    mutation_started = threading.Event()
    auth_pid: list[int] = []
    mutation_pid: list[int] = []
    errors: list[BaseException] = []
    outcome: dict[str, Any] = {}

    def before_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "role-mutation"
            and "SELECT roles.role_id" in statement
            and "FOR UPDATE" in statement
        ):
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            mutation_pid.append(int(cursor.connection.info.backend_pid))
            mutation_started.set()

    def after_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "role-authorization"
            and "FROM user_roles JOIN roles" in statement
            and "FOR SHARE OF roles" in statement
        ):
            auth_pid.append(int(cursor.connection.info.backend_pid))
            auth_locked.set()
            if not release_auth.wait(timeout=10):
                raise AssertionError("authorization role lock was not released")

    def authorize() -> None:
        try:
            if operation == "read":
                outcome["authorization"] = backend.get_and_touch_structured_memory_by_name(
                    "runbook",
                    "project",
                    project_id,
                    acting_principal_id="member",
                )
            else:
                outcome["authorization"] = backend.upsert_structured_memory(
                    "replacement-id",
                    "runbook",
                    "Updated runbook",
                    None,
                    "project",
                    project_id,
                    "updated body",
                    acting_principal_id="member",
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def mutate() -> None:
        try:
            if mutation == "clear":
                backend.clear_role_overrides(role_id)
                outcome["mutation"] = True
            elif mutation == "delete":
                outcome["mutation"] = backend.delete_role(role_id)
            else:
                permission = "project.write" if mutation == "revoke-write" else "project.read"
                backend.set_role_overrides(role_id, set(), {permission})
                outcome["mutation"] = True
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    sa.event.listen(backend._engine, "before_cursor_execute", before_cursor_execute)
    sa.event.listen(backend._engine, "after_cursor_execute", after_cursor_execute)
    auth_thread = threading.Thread(target=authorize, name="role-authorization")
    mutation_thread = threading.Thread(target=mutate, name="role-mutation")
    try:
        auth_thread.start()
        assert auth_locked.wait(timeout=10), "authorization never acquired the role share lock"
        mutation_thread.start()
        assert mutation_started.wait(timeout=10), "mutation never attempted the role update lock"
        assert auth_pid and mutation_pid
        for _attempt in range(1_000):
            if auth_pid[0] in _postgres_blocking_pids(backend, mutation_pid[0]):
                break
        else:
            raise AssertionError("role mutation was not blocked by authorization")
    finally:
        release_auth.set()
        auth_thread.join(timeout=10)
        mutation_thread.join(timeout=10)
        sa.event.remove(backend._engine, "before_cursor_execute", before_cursor_execute)
        sa.event.remove(backend._engine, "after_cursor_execute", after_cursor_execute)

    assert not auth_thread.is_alive()
    assert not mutation_thread.is_alive()
    assert errors == []
    assert outcome["authorization"] is not None
    assert outcome["mutation"] is True
    overrides = backend.list_role_overrides(role_id)
    assert [(row["permission"], row["action"]) for row in overrides] == expected_overrides

    if operation == "read":
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                project_id,
                acting_principal_id="member",
            )
    else:
        with pytest.raises(ProjectMemoryAuthorizationError):
            backend.upsert_structured_memory(
                "final-attempt",
                "runbook",
                "Denied",
                None,
                "project",
                project_id,
                "must not land",
                acting_principal_id="member",
            )


def test_postgresql_guarded_role_lock_does_not_block_unrelated_role(backend) -> None:
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    backend.create_user("member", "member", "Member", "hash")
    backend.create_role("role-a", "role-a", "Role A", "project.read", True)
    backend.create_role("role-b", "role-b", "Role B", "", True)
    backend.assign_role("member", "role-a")
    backend.create_project("project-a", "Project A", "owner")
    backend.add_project_member("project-a", "member")
    backend.create_structured_memory(
        "memory-a", "runbook", "Runbook", "general", "project", "project-a", "body"
    )
    auth_locked = threading.Event()
    release_auth = threading.Event()
    mutation_done = threading.Event()
    errors: list[BaseException] = []

    def after_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "role-a-authorization"
            and "FROM user_roles JOIN roles" in statement
            and "FOR SHARE OF roles" in statement
        ):
            auth_locked.set()
            if not release_auth.wait(timeout=10):
                raise AssertionError("role-a authorization was not released")

    def authorize_a() -> None:
        try:
            backend.get_and_touch_structured_memory_by_name(
                "runbook",
                "project",
                "project-a",
                acting_principal_id="member",
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def mutate_b() -> None:
        try:
            backend.set_role_overrides("role-b", {"project.read"}, set())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            mutation_done.set()

    sa.event.listen(backend._engine, "after_cursor_execute", after_cursor_execute)
    auth_thread = threading.Thread(target=authorize_a, name="role-a-authorization")
    mutation_thread = threading.Thread(target=mutate_b, name="role-b-mutation")
    try:
        auth_thread.start()
        assert auth_locked.wait(timeout=10)
        mutation_thread.start()
        assert mutation_done.wait(timeout=10), "unrelated role mutation was spuriously blocked"
    finally:
        release_auth.set()
        auth_thread.join(timeout=10)
        mutation_thread.join(timeout=10)
        sa.event.remove(backend._engine, "after_cursor_execute", after_cursor_execute)

    assert not auth_thread.is_alive()
    assert not mutation_thread.is_alive()
    assert errors == []
    assert [
        (row["permission"], row["action"]) for row in backend.list_role_overrides("role-b")
    ] == [("project.read", "grant")]


@pytest.mark.parametrize(
    "mutation",
    ["override-revoke", "unassign", "oidc-remove", "delete-user"],
)
def test_postgresql_snapshot_retries_when_role_assignment_revoke_wins(
    backend, mutation: str
) -> None:
    """A pre-lock RR snapshot cannot survive a winning authority update."""
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    role_id = "role-snapshot-retry"
    project_id = "project-snapshot-retry"
    ws_id = "ws-snapshot-retry"
    backend.create_user("member", "member", "Member", "hash")
    backend.create_role(role_id, role_id, role_id, "project.read", True)
    backend.assign_role("member", role_id, "oidc" if mutation == "oidc-remove" else "")
    backend.create_project(project_id, project_id, "owner")
    backend.add_project_member(project_id, "member")
    assert backend.register_workstream(ws_id, user_id="owner", project_id=project_id)
    backend.create_structured_memory(
        "memory-snapshot-retry",
        "runbook",
        "Project runbook",
        "general",
        "project",
        project_id,
        "private body",
    )

    mutation_locked = threading.Event()
    release_mutation = threading.Event()
    capture_attempted = threading.Event()
    mutation_pid: list[int] = []
    capture_pid: list[int] = []
    errors: list[BaseException] = []
    outcome: dict[str, Any] = {}

    def before_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "snapshot-authorization"
            and "FROM user_roles JOIN roles" in statement
            and "FOR SHARE OF roles" in statement
        ):
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            if not capture_pid:
                capture_pid.append(int(cursor.connection.info.backend_pid))
                capture_attempted.set()

    def after_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "snapshot-revoke"
            and "SELECT roles.role_id" in statement
            and "FOR UPDATE" in statement
        ):
            mutation_pid.append(int(cursor.connection.info.backend_pid))
            mutation_locked.set()
            if not release_mutation.wait(timeout=10):
                raise AssertionError("snapshot revoke was not released")

    def revoke() -> None:
        try:
            if mutation == "override-revoke":
                backend.set_role_overrides(role_id, set(), {"project.read"})
            elif mutation == "unassign":
                assert backend.unassign_role("member", role_id)
            elif mutation == "oidc-remove":
                assert backend.replace_oidc_roles("member", set()) == (set(), {role_id})
            else:
                assert backend.delete_user("member")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def capture() -> None:
        try:
            outcome["snapshot"] = backend.acquire_memory_index_snapshot(ws_id, "member")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    sa.event.listen(backend._engine, "before_cursor_execute", before_cursor_execute)
    sa.event.listen(backend._engine, "after_cursor_execute", after_cursor_execute)
    mutation_thread = threading.Thread(target=revoke, name="snapshot-revoke")
    capture_thread = threading.Thread(target=capture, name="snapshot-authorization")
    try:
        mutation_thread.start()
        assert mutation_locked.wait(timeout=10), "revoke never acquired the stable role lock"
        capture_thread.start()
        assert capture_attempted.wait(timeout=10), "capture never attempted the role share lock"
        assert mutation_pid and capture_pid
        for _attempt in range(1_000):
            if mutation_pid[0] in _postgres_blocking_pids(backend, capture_pid[0]):
                break
        else:
            raise AssertionError("snapshot capture was not blocked by the winning revoke")
    finally:
        release_mutation.set()
        mutation_thread.join(timeout=10)
        capture_thread.join(timeout=10)
        sa.event.remove(backend._engine, "before_cursor_execute", before_cursor_execute)
        sa.event.remove(backend._engine, "after_cursor_execute", after_cursor_execute)

    assert not mutation_thread.is_alive()
    assert not capture_thread.is_alive()
    assert errors == []
    snapshot = outcome["snapshot"]
    assert snapshot is not None
    assert snapshot["project_id"] == ""
    assert "runbook" not in snapshot["content"]
    if mutation == "override-revoke":
        assert [
            (row["permission"], row["action"]) for row in backend.list_role_overrides(role_id)
        ] == [("project.read", "revoke")]
    else:
        assert backend.list_user_roles("member") == []


def test_postgresql_first_snapshot_insert_collision_retries_complete_capture(backend) -> None:
    """Two RR first-captures converge on the first committed snapshot."""
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    ws_id = "ws-first-capture-race"
    assert backend.register_workstream(ws_id, user_id="owner")
    backend.create_structured_memory(
        "memory-first-capture-race",
        "runbook",
        "Runbook",
        "general",
        "global",
        "",
        "body",
    )

    first_at_empty_snapshot = threading.Event()
    release_first = threading.Event()
    second_attempted_workstream_lock = threading.Event()
    first_pid: list[int] = []
    second_pid: list[int] = []
    second_attempts = 0
    errors: list[BaseException] = []
    outcome: dict[str, Any] = {}

    def before_cursor_execute(
        _conn: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal second_attempts
        name = threading.current_thread().name
        if name == "snapshot-first" and "FROM user_roles JOIN roles" in statement:
            if not first_pid:
                first_pid.append(int(cursor.connection.info.backend_pid))
        elif name == "snapshot-second" and "FROM user_roles JOIN roles" in statement:
            second_attempts += 1
            if not second_pid:
                second_pid.append(int(cursor.connection.info.backend_pid))
        if (
            name == "snapshot-second"
            and "FROM workstreams" in statement
            and "FOR UPDATE" in statement
        ):
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            second_attempted_workstream_lock.set()

    def after_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "snapshot-first"
            and "FROM memory_index_snapshots" in statement
            and not first_at_empty_snapshot.is_set()
        ):
            first_at_empty_snapshot.set()
            if not release_first.wait(timeout=10):
                raise AssertionError("first capture was not released")

    def capture(label: str, principal: str) -> None:
        try:
            outcome[label] = backend.acquire_memory_index_snapshot(ws_id, principal)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    sa.event.listen(backend._engine, "before_cursor_execute", before_cursor_execute)
    sa.event.listen(backend._engine, "after_cursor_execute", after_cursor_execute)
    first_thread = threading.Thread(
        target=capture, args=("first", "first-principal"), name="snapshot-first"
    )
    second_thread = threading.Thread(
        target=capture, args=("second", "second-principal"), name="snapshot-second"
    )
    try:
        first_thread.start()
        assert first_at_empty_snapshot.wait(timeout=10), "first capture never observed an empty row"
        second_thread.start()
        assert second_attempted_workstream_lock.wait(timeout=10)
        assert first_pid and second_pid
        for _attempt in range(1_000):
            if first_pid[0] in _postgres_blocking_pids(backend, second_pid[0]):
                break
        else:
            raise AssertionError("second capture was not blocked by the first workstream lock")
    finally:
        release_first.set()
        first_thread.join(timeout=10)
        second_thread.join(timeout=10)
        sa.event.remove(backend._engine, "before_cursor_execute", before_cursor_execute)
        sa.event.remove(backend._engine, "after_cursor_execute", after_cursor_execute)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert second_attempts >= 2
    assert outcome["first"] == outcome["second"]
    assert outcome["first"]["principal_id"] == "first-principal"
