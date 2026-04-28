"""Tests for turnstone.core.memory — structured memory facade functions."""

from turnstone.core.memory import (
    count_structured_memories,
    delete_structured_memory,
    get_structured_memory_by_name,
    list_structured_memories,
    normalize_key,
    save_structured_memory,
    search_structured_memories,
)


class TestSaveStructuredMemory:
    def test_save_new(self, tmp_db):
        mid, old = save_structured_memory("test_key", "hello world")
        assert mid != ""
        assert old is None

    def test_save_upsert(self, tmp_db):
        save_structured_memory("test_key", "first")
        mid, old = save_structured_memory("test_key", "second")
        assert old == "first"
        assert mid != ""

    def test_save_normalizes_key(self, tmp_db):
        save_structured_memory("My-Key", "value")
        mems = list_structured_memories()
        assert any(m["name"] == "my_key" for m in mems)

    def test_save_with_type_and_scope(self, tmp_db):
        save_structured_memory("k", "v", mem_type="user", scope="workstream", scope_id="ws1")
        mems = list_structured_memories(scope="workstream", scope_id="ws1")
        assert len(mems) == 1
        assert mems[0]["type"] == "user"


class TestDeleteStructuredMemory:
    def test_delete_existing(self, tmp_db):
        save_structured_memory("mykey", "val")
        assert delete_structured_memory("mykey")

    def test_delete_nonexistent(self, tmp_db):
        assert not delete_structured_memory("nope")

    def test_delete_normalizes_key(self, tmp_db):
        save_structured_memory("my_key", "val")
        assert delete_structured_memory("My-Key")


class TestListStructuredMemories:
    def test_list_empty(self, tmp_db):
        assert list_structured_memories() == []

    def test_list_returns_saved(self, tmp_db):
        save_structured_memory("a", "alpha")
        save_structured_memory("b", "beta")
        mems = list_structured_memories()
        assert len(mems) == 2


class TestSearchStructuredMemories:
    def test_search_finds_match(self, tmp_db):
        save_structured_memory("db_host", "localhost", description="database hostname")
        save_structured_memory("api_url", "http://example.com")
        results = search_structured_memories("database")
        assert len(results) >= 1
        assert any(r["name"] == "db_host" for r in results)


class TestGetStructuredMemoryByName:
    def test_get_existing(self, tmp_db):
        save_structured_memory("my_mem", "full content here that is quite long")
        mem = get_structured_memory_by_name("my_mem", "global", "")
        assert mem is not None
        assert mem["content"] == "full content here that is quite long"
        assert mem["name"] == "my_mem"

    def test_get_nonexistent(self, tmp_db):
        assert get_structured_memory_by_name("nope", "global", "") is None

    def test_get_wrong_scope(self, tmp_db):
        save_structured_memory("ws_mem", "data", scope="workstream", scope_id="ws1")
        assert get_structured_memory_by_name("ws_mem", "global", "") is None
        assert get_structured_memory_by_name("ws_mem", "workstream", "ws1") is not None

    def test_get_normalizes_key(self, tmp_db):
        save_structured_memory("My-Key", "value")
        mem = get_structured_memory_by_name("My-Key", "global", "")
        assert mem is not None
        assert mem["name"] == "my_key"


class TestCountStructuredMemories:
    def test_count_zero(self, tmp_db):
        assert count_structured_memories() == 0

    def test_count_after_save(self, tmp_db):
        save_structured_memory("a", "1")
        save_structured_memory("b", "2")
        assert count_structured_memories() == 2


class TestNormalizeKey:
    def test_basic(self):
        assert normalize_key("My-Key Name") == "my_key_name"


class TestScopeIsolation:
    """Verify that list/search without scope only returns visible memories.

    Reproduces the cross-workstream leak: unscoped list/search must not
    return workstream-scoped memories from other workstreams or
    user-scoped memories from other users.
    """

    def _seed(self):
        """Create memories across multiple scopes."""
        save_structured_memory("global_note", "visible to all", scope="global")
        save_structured_memory("ws1_note", "belongs to ws1", scope="workstream", scope_id="ws1")
        save_structured_memory("ws2_note", "belongs to ws2", scope="workstream", scope_id="ws2")
        save_structured_memory("u1_note", "belongs to user1", scope="user", scope_id="u1")
        save_structured_memory("u2_note", "belongs to user2", scope="user", scope_id="u2")

    @staticmethod
    def _list_visible(ws_id: str, user_id: str, mem_type: str = "", limit: int = 50):
        """Replicate the scope-filtered list logic from ChatSession."""
        global_mems = list_structured_memories(mem_type=mem_type, scope="global", limit=limit)
        ws_mems = list_structured_memories(
            mem_type=mem_type, scope="workstream", scope_id=ws_id, limit=limit
        )
        user_mems = (
            list_structured_memories(mem_type=mem_type, scope="user", scope_id=user_id, limit=limit)
            if user_id
            else []
        )
        combined = global_mems + ws_mems + user_mems
        combined.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return combined[:limit]

    @staticmethod
    def _search_visible(query: str, ws_id: str, user_id: str, mem_type: str = "", limit: int = 20):
        """Replicate the scope-filtered search logic from ChatSession."""
        global_mems = search_structured_memories(
            query, mem_type=mem_type, scope="global", limit=limit
        )
        ws_mems = search_structured_memories(
            query, mem_type=mem_type, scope="workstream", scope_id=ws_id, limit=limit
        )
        user_mems = (
            search_structured_memories(
                query, mem_type=mem_type, scope="user", scope_id=user_id, limit=limit
            )
            if user_id
            else []
        )
        combined = global_mems + ws_mems + user_mems
        combined.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return combined[:limit]

    def test_unscoped_list_returns_all_scopes(self, tmp_db):
        """Demonstrate the leak: unscoped list returns everything."""
        self._seed()
        all_mems = list_structured_memories()
        assert len(all_mems) == 5  # no scope filter → all memories

    def test_visible_list_excludes_other_workstreams(self, tmp_db):
        """Scope-filtered list for ws1/u1 excludes ws2 and u2 memories."""
        self._seed()
        visible = self._list_visible("ws1", "u1")
        names = {m["name"] for m in visible}
        assert "global_note" in names
        assert "ws1_note" in names
        assert "u1_note" in names
        assert "ws2_note" not in names
        assert "u2_note" not in names

    def test_visible_list_no_user(self, tmp_db):
        """Scope-filtered list with no user_id excludes all user memories."""
        self._seed()
        visible = self._list_visible("ws1", "")
        names = {m["name"] for m in visible}
        assert "global_note" in names
        assert "ws1_note" in names
        assert "u1_note" not in names
        assert "u2_note" not in names

    def test_visible_search_no_user(self, tmp_db):
        """Scope-filtered search with no user_id excludes all user memories."""
        self._seed()
        visible = self._search_visible("belongs", "ws1", "")
        names = {m["name"] for m in visible}
        assert "ws1_note" in names
        assert "u1_note" not in names
        assert "u2_note" not in names

    def test_visible_search_excludes_other_workstreams(self, tmp_db):
        """Scope-filtered search for ws1/u1 excludes ws2 and u2 memories."""
        self._seed()
        visible = self._search_visible("belongs", "ws1", "u1")
        names = {m["name"] for m in visible}
        assert "ws1_note" in names
        assert "u1_note" in names
        assert "ws2_note" not in names
        assert "u2_note" not in names

    def test_explicit_scope_still_works(self, tmp_db):
        """Explicit scope filter continues to work as before."""
        self._seed()
        ws2_only = list_structured_memories(scope="workstream", scope_id="ws2")
        assert len(ws2_only) == 1
        assert ws2_only[0]["name"] == "ws2_note"


class TestSanitizeErrorText:
    """Verify error-text sanitisation strips credentials and caps length.

    Pairs with the ``persist_last_error`` writer — every persisted
    string flows through ``sanitize_error_text`` so a misconfigured
    provider URL or a quoted response body can't park credentials in
    storage where the coordinator LLM later inhales them via the
    inspect/wait surface.

    Sanitisation delegates to
    :func:`turnstone.core.output_guard.redact_credentials` so the
    pattern set is the same one audit logs and the post-tool guard
    use.  The tests below assert the *behaviour* (the secret is gone)
    rather than the exact replacement marker — output_guard owns the
    marker format and the regex catalog, and pinning the marker here
    would force two-place edits whenever output_guard adds a new
    redaction label.
    """

    def test_strips_url_userinfo(self):
        from turnstone.core.memory import sanitize_error_text

        # Misconfigured OPENAI_BASE_URL → httpx ConnectError carries
        # the userinfo verbatim in str(exc).
        msg = "ConnectError: connection failed to https://user:hunter2@api.example.com/v1/chat"
        out = sanitize_error_text(msg)
        # The password is gone but the host (useful for triage) stays.
        assert "hunter2" not in out
        assert "api.example.com" in out

    def test_strips_url_userinfo_http_too(self):
        from turnstone.core.memory import sanitize_error_text

        msg = "RequestError on http://admin:s3cret@internal.host/path"
        out = sanitize_error_text(msg)
        assert "s3cret" not in out
        assert "internal.host" in out

    def test_strips_db_connection_string(self):
        """Output_guard already covered DB connection-strings; assert
        the delegation surfaces that coverage so a leaked
        ``DATABASE_URL`` echoed in an error doesn't slip through."""
        from turnstone.core.memory import sanitize_error_text

        msg = "OperationalError: postgresql://app:topsecret@db.host/main"
        out = sanitize_error_text(msg)
        assert "topsecret" not in out

    def test_redacts_openai_keys(self):
        from turnstone.core.memory import sanitize_error_text

        msg = (
            "AuthenticationError: invalid api key sk-proj-AbCdEfGhIjKlMnOpQrStUv "
            "(echoed from request body)"
        )
        out = sanitize_error_text(msg)
        assert "sk-proj-AbCdEfGhIjKlMnOpQrStUv" not in out

    def test_redacts_bearer_tokens(self):
        from turnstone.core.memory import sanitize_error_text

        msg = "401 Unauthorized - Bearer eyJabcDEFghiJKLmnoPQRstuVWX rejected"
        out = sanitize_error_text(msg)
        assert "eyJabcDEFghiJKLmnoPQRstuVWX" not in out

    def test_redacts_github_tokens(self):
        from turnstone.core.memory import sanitize_error_text

        # The output_guard ghp pattern requires exactly 36 chars, so
        # use a realistic-shaped token.
        msg = "git push failed: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij not authorized"
        out = sanitize_error_text(msg)
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij" not in out

    def test_redacts_aws_access_keys(self):
        from turnstone.core.memory import sanitize_error_text

        msg = "S3 error: signature mismatch for AKIAIOSFODNN7EXAMPLE"
        out = sanitize_error_text(msg)
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_caps_length(self):
        from turnstone.core.memory import LAST_ERROR_MAX_LEN, sanitize_error_text

        msg = "X" * (LAST_ERROR_MAX_LEN * 2)
        out = sanitize_error_text(msg)
        assert len(out) <= LAST_ERROR_MAX_LEN
        # Truncation marker preserved.
        assert out.endswith("...")

    def test_passes_through_clean_text(self):
        from turnstone.core.memory import sanitize_error_text

        msg = "TimeoutError: provider did not respond within 60s"
        assert sanitize_error_text(msg) == msg

    def test_handles_empty(self):
        from turnstone.core.memory import sanitize_error_text

        assert sanitize_error_text("") == ""


class TestPersistLastError:
    """Direct unit tests for the writer-side helper.

    The reader-side tests in test_coordinator_client.py write to storage
    via the raw backend, so the writer's contract — sanitize, no-op on
    empty inputs, swallow storage failures, use the published constant
    key — is unexercised without these.
    """

    def test_round_trip_uses_constant_key(self, tmp_db):
        from turnstone.core.memory import (
            LAST_ERROR_CONFIG_KEY,
            load_last_error,
            persist_last_error,
            register_workstream,
        )

        # Pre-register a workstream so save_workstream_config has somewhere
        # to land — workstream_config rows reference the workstreams table.
        register_workstream("ws-1", user_id="u1")

        persist_last_error("ws-1", "TimeoutError: provider stalled")
        assert load_last_error("ws-1") == "TimeoutError: provider stalled"

        # The persisted row uses the published constant key — pinning
        # this catches future drift between the writer and the
        # coordinator_client.py readers that import the same constant.
        from turnstone.core.memory import load_workstream_config

        cfg = load_workstream_config("ws-1")
        assert LAST_ERROR_CONFIG_KEY in cfg

    def test_sanitises_before_persist(self, tmp_db):
        from turnstone.core.memory import (
            load_last_error,
            persist_last_error,
            register_workstream,
        )

        register_workstream("ws-1", user_id="u1")
        persist_last_error("ws-1", "ConnectError: https://user:secret@host/")
        stored = load_last_error("ws-1")
        # The secret is gone but the host (useful for triage) survives.
        # We don't pin the redaction marker — output_guard owns the
        # format and the assertion above is the behaviour we care about.
        assert "secret" not in stored
        assert "host/" in stored

    def test_noop_on_empty_ws_id(self, tmp_db):
        from turnstone.core.memory import persist_last_error

        # Must not raise; must not write anywhere observable.
        persist_last_error("", "anything")  # no-op

    def test_noop_on_empty_err_msg(self, tmp_db):
        from turnstone.core.memory import (
            load_last_error,
            persist_last_error,
            register_workstream,
        )

        register_workstream("ws-1", user_id="u1")
        persist_last_error("ws-1", "")
        # Empty err_msg is a no-op — the row stays absent rather than
        # being upserted with an empty string.
        assert load_last_error("ws-1") == ""

    def test_swallows_storage_failure(self, tmp_db, monkeypatch):
        """A storage failure must not propagate — error surfacing is
        advisory, not safety-critical.  The exception path of a worker
        thread already has enough trouble without this."""
        from turnstone.core import memory as memory_mod
        from turnstone.core.memory import persist_last_error

        class _BoomStorage:
            def save_workstream_config(self, *_args, **_kw):
                raise RuntimeError("simulated storage failure")

        monkeypatch.setattr(memory_mod, "get_storage", lambda: _BoomStorage())
        # Must not raise.
        persist_last_error("ws-1", "TimeoutError: x")


class TestClearLastError:
    """Verify clear_last_error wipes the row idempotently."""

    def test_clears_existing(self, tmp_db):
        from turnstone.core.memory import (
            clear_last_error,
            load_last_error,
            persist_last_error,
            register_workstream,
        )

        register_workstream("ws-1", user_id="u1")
        persist_last_error("ws-1", "RuntimeError: boom")
        assert load_last_error("ws-1") == "RuntimeError: boom"
        clear_last_error("ws-1")
        assert load_last_error("ws-1") == ""

    def test_clear_preserves_other_config_keys(self, tmp_db):
        """clear_last_error must not delete sibling config rows
        (close_reason, tasks).  It writes an empty string to the
        last_error key only — INSERT OR REPLACE per key, no row-wide
        delete."""
        from turnstone.core.memory import (
            clear_last_error,
            load_workstream_config,
            persist_last_error,
            register_workstream,
            save_workstream_config,
        )

        register_workstream("ws-1", user_id="u1")
        save_workstream_config("ws-1", {"close_reason": "user closed"})
        persist_last_error("ws-1", "RuntimeError: boom")

        clear_last_error("ws-1")
        cfg = load_workstream_config("ws-1")
        # close_reason untouched.
        assert cfg.get("close_reason") == "user closed"

    def test_noop_on_empty_ws_id(self, tmp_db):
        from turnstone.core.memory import clear_last_error

        clear_last_error("")  # must not raise
