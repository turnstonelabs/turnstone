"""Tests for structured memory storage backend operations."""


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
        assert row2["content"] == "v2"
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
        assert row["content"] == "v2"
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
        assert row["content"] == "v2"
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

    def test_search_by_content(self, backend):
        backend.create_structured_memory(
            "m1", "a", "Test memory", "general", "global", "", "postgresql host"
        )
        results = backend.search_structured_memories("postgresql")
        assert len(results) == 1

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

        results = backend.search_structured_memories("info", scope="workstream", scope_id="ws1")
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


class TestStableOrderingOnTimestampTies:
    """When two memories share an `updated` timestamp, secondary sort on
    memory_id keeps the order deterministic across calls.

    `updated` is second-precision, and touch_structured_memories() can bump
    a batch to identical timestamps — without a tie-breaker BM25 input
    order shuffles run-to-run, busting the LLM-side prompt cache.
    """

    def _seed_with_shared_timestamp(self, backend):
        # Create three memories then force their `updated` columns equal —
        # mirrors the real-world case where a touch_structured_memories
        # batch lands them in the same second.
        for mid in ("zebra_id", "apple_id", "mango_id"):
            backend.create_structured_memory(
                mid, f"name_{mid}", "Test memory", "general", "global", "", "shared content"
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
