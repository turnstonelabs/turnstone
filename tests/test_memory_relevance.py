"""Tests for turnstone.core.memory_relevance — scoring, formatting, context extraction."""

from unittest.mock import patch

from turnstone.core.memory_relevance import (
    MemoryConfig,
    build_memory_context,
    extract_recent_context,
    score_memories,
)

# ---------------------------------------------------------------------------
# score_memories
# ---------------------------------------------------------------------------


class TestScoreMemories:
    def test_empty_memories(self):
        assert score_memories([], "query") == []

    def test_empty_query_returns_recent(self):
        mems = [
            {"name": "a", "description": "", "content": "alpha"},
            {"name": "b", "description": "", "content": "beta"},
            {"name": "c", "description": "", "content": "gamma"},
        ]
        result = score_memories(mems, "", k=2)
        assert len(result) == 2
        assert result[0]["name"] == "a"

    def test_whitespace_query_returns_recent(self):
        mems = [{"name": "a", "description": "", "content": "alpha"}]
        assert score_memories(mems, "   ", k=5) == mems

    def test_relevance_ranking(self):
        mems = [
            {"name": "cooking", "description": "recipes", "content": "pasta sauce tomato"},
            {"name": "python", "description": "programming", "content": "python file io disk"},
            {
                "name": "disk_io",
                "description": "file operations",
                "content": "read write file disk",
            },
        ]
        result = score_memories(mems, "file disk", k=2)
        names = [m["name"] for m in result]
        assert "disk_io" in names
        assert "python" in names

    def test_k_limits_results(self):
        mems = [{"name": f"m{i}", "description": "", "content": f"word{i}"} for i in range(10)]
        result = score_memories(mems, "word0 word1 word2", k=2)
        assert len(result) <= 2

    def test_no_match_returns_empty(self):
        mems = [{"name": "a", "description": "", "content": "hello world"}]
        result = score_memories(mems, "zzzznotfound")
        assert result == []

    def test_uses_name_for_scoring(self):
        mems = [
            {"name": "database_config", "description": "", "content": "host=localhost"},
            {"name": "unrelated", "description": "", "content": "nothing here"},
        ]
        result = score_memories(mems, "database", k=1)
        assert len(result) == 1
        assert result[0]["name"] == "database_config"

    def test_uses_description_for_scoring(self):
        mems = [
            {"name": "x", "description": "postgresql connection settings", "content": "host=db"},
            {"name": "y", "description": "unrelated", "content": "nothing"},
        ]
        result = score_memories(mems, "postgresql", k=1)
        assert result[0]["name"] == "x"


# ---------------------------------------------------------------------------
# build_memory_context
# ---------------------------------------------------------------------------


class TestBuildMemoryContext:
    def test_empty_memories(self):
        assert build_memory_context([]) == ""

    def test_single_memory(self):
        mems = [{"name": "test", "type": "project", "scope": "global", "content": "hello"}]
        ctx = build_memory_context(mems)
        assert "<memories>" in ctx
        assert "</memories>" in ctx
        assert 'name="test"' in ctx
        assert "hello" in ctx

    def test_html_escaping(self):
        mems = [
            {
                "name": "a<b",
                "type": "project",
                "scope": "global",
                "content": "x & y",
                "description": 'say "hi"',
            }
        ]
        ctx = build_memory_context(mems)
        assert "&lt;" in ctx
        assert "&amp;" in ctx
        assert "&quot;" in ctx

    def test_truncates_long_content(self):
        mems = [
            {
                "name": "long",
                "type": "project",
                "scope": "global",
                "content": "x" * 600,
            }
        ]
        ctx = build_memory_context(mems)
        assert "..." in ctx
        # Content should be truncated to 500 chars + "..."
        assert "x" * 501 not in ctx

    def test_description_attribute(self):
        mems = [
            {
                "name": "test",
                "type": "project",
                "scope": "global",
                "content": "data",
                "description": "some desc",
            }
        ]
        ctx = build_memory_context(mems)
        assert 'description="some desc"' in ctx

    def test_no_description_attribute_when_empty(self):
        mems = [{"name": "test", "type": "project", "scope": "global", "content": "data"}]
        ctx = build_memory_context(mems)
        assert "description=" not in ctx


# ---------------------------------------------------------------------------
# extract_recent_context
# ---------------------------------------------------------------------------


class TestExtractRecentContext:
    def test_extracts_user_messages(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "world"},
        ]
        ctx = extract_recent_context(msgs, max_messages=2)
        assert "world" in ctx
        assert "hello" in ctx

    def test_skips_non_user(self):
        msgs = [
            {"role": "assistant", "content": "ignored"},
            {"role": "user", "content": "included"},
        ]
        ctx = extract_recent_context(msgs, max_messages=5)
        assert "included" in ctx
        assert "ignored" not in ctx

    def test_respects_max_messages(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        ctx = extract_recent_context(msgs, max_messages=1)
        assert "third" in ctx
        assert "first" not in ctx

    def test_handles_list_content(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "multi-part"},
                    {"type": "image_url", "image_url": {"url": "http://example.com"}},
                ],
            }
        ]
        ctx = extract_recent_context(msgs, max_messages=1)
        assert "multi-part" in ctx

    def test_handles_string_parts_in_list(self):
        msgs = [{"role": "user", "content": ["plain string part"]}]
        ctx = extract_recent_context(msgs, max_messages=1)
        assert "plain string part" in ctx

    def test_empty_messages(self):
        assert extract_recent_context([]) == ""


# ---------------------------------------------------------------------------
# Composition candidate-selection (_init_system_messages)
# ---------------------------------------------------------------------------


def _make_mem(name: str, content: str = "", memory_id: str | None = None) -> dict[str, str]:
    return {
        "name": name,
        "memory_id": memory_id or f"mid_{name}",
        "type": "project",
        "scope": "global",
        "scope_id": "",
        "description": "",
        "content": content or name,
        "updated": "2024-01-01T00:00:00",
    }


def _make_session(fetch_limit: int = 5, relevance_k: int = 3, **kwargs: object):
    """Composition tests need a real ChatSession (constructor calls
    ``_init_system_messages`` once, unpatched, before the test gets a chance
    to install patches).  ``tmp_db`` initializes the storage singleton that
    constructor needs; tests then patch the visibility helpers and call
    ``_init_system_messages`` a second time to exercise the new logic.
    """
    from tests._helpers import make_chat_session

    return make_chat_session(
        memory_config=MemoryConfig(fetch_limit=fetch_limit, relevance_k=relevance_k),
        **kwargs,
    )


class TestCompositionCandidateSelection:
    """Verify the query-aware candidate set in _init_system_messages."""

    def test_recency_ceiling_regression(self, tmp_db):
        """Old relevant memory not in recency top-N still injected via search path."""
        session = _make_session(fetch_limit=5, relevance_k=3)
        session.messages = [{"role": "user", "content": "postgres database configuration"}]

        old_mem = _make_mem(
            "ancient_db_config",
            content="postgres database configuration connection host port",
            memory_id="m_old",
        )
        # Recency top-5 do not include old_mem
        recent = [_make_mem(f"recent_{i}", memory_id=f"mr{i}") for i in range(5)]

        with (
            patch.object(session, "_search_visible_memories", return_value=[old_mem]),
            patch.object(session, "_list_visible_memories", return_value=recent),
        ):
            session._init_system_messages()

        joined = "\n".join(m["content"] for m in session.system_messages if m["role"] == "system")
        # With the fix, old_mem enters the candidate pool via search and wins BM25
        assert "ancient_db_config" in joined

    def test_empty_query_falls_back_to_recency(self, tmp_db):
        """No user messages → empty context → recency path, search never called."""
        session = _make_session()
        session.messages = []  # extract_recent_context returns ""

        recency = [_make_mem("note_alpha"), _make_mem("note_beta")]

        with (
            patch.object(session, "_list_visible_memories", return_value=recency),
            patch.object(session, "_search_visible_memories") as search_mock,
        ):
            session._init_system_messages()

        search_mock.assert_not_called()
        joined = "\n".join(m["content"] for m in session.system_messages if m["role"] == "system")
        assert "note_alpha" in joined

    def test_sparse_match_union_fills_candidate_pool(self, tmp_db):
        """Search returning < fetch_limit results unions with recency fillers."""
        session = _make_session(fetch_limit=5, relevance_k=4)
        session.messages = [{"role": "user", "content": "unique_term xyzzy"}]

        hit_a = _make_mem("hit_alpha", content="unique_term xyzzy alpha", memory_id="m_ha")
        hit_b = _make_mem("hit_beta", content="unique_term xyzzy beta", memory_id="m_hb")
        search_hits = [hit_a, hit_b]  # 2 < fetch_limit=5 → triggers union

        # Recency overlaps on hit_a/hit_b and adds 3 fillers
        filler = [_make_mem(f"filler_{i}", memory_id=f"mf{i}") for i in range(3)]
        recency = [hit_a, hit_b] + filler

        with (
            patch.object(session, "_search_visible_memories", return_value=search_hits),
            patch.object(session, "_list_visible_memories", return_value=recency),
        ):
            session._init_system_messages()

        joined = "\n".join(m["content"] for m in session.system_messages if m["role"] == "system")
        # Both hits match "unique_term xyzzy" well → appear after BM25 ranking
        assert "hit_alpha" in joined
        assert "hit_beta" in joined

    def test_recency_preserved_when_search_returns_noise_above_relevance_k(self, tmp_db):
        """Pool guarantee: recency-50 always reaches BM25, even when search
        returns enough noise hits to clear ``relevance_k``.

        Closes the narrow regression vs. the original bug — without the
        ``fetch_limit`` threshold, a stopword-dominated cap-search that
        returned >= relevance_k irrelevant hits would short-circuit and
        evict the recency-only memory the bug had been surfacing.
        """
        session = _make_session(fetch_limit=10, relevance_k=3)
        session.messages = [{"role": "user", "content": "configure host"}]

        # Search returns relevance_k=3 noise hits — enough to skip recency
        # under the OLD threshold, not enough to fill fetch_limit=10.
        noise = [
            _make_mem(f"noise_{i}", content="generic content", memory_id=f"mn{i}") for i in range(3)
        ]
        # The memory the user actually wants — distinctive, in recency,
        # but its content doesn't share any token with the noise hits.
        wanted = _make_mem(
            "host_config_v2",
            content="host=localhost port=5432 db=production",
            memory_id="m_wanted",
        )
        recency = [wanted] + [_make_mem(f"recent_{i}", memory_id=f"mr{i}") for i in range(5)]

        with (
            patch.object(session, "_search_visible_memories", return_value=noise),
            patch.object(session, "_list_visible_memories", return_value=recency),
        ):
            session._init_system_messages()

        joined = "\n".join(m["content"] for m in session.system_messages if m["role"] == "system")
        # ``wanted`` reached BM25 via the union and matched "host" → injected.
        assert "host_config_v2" in joined

    def test_coord_scope_isolated_visibility(self, tmp_db):
        """Coord composition queries the coord scope alone, never the
        global/workstream/user union."""
        from turnstone.core.workstream import WorkstreamKind

        coord = _make_session(
            fetch_limit=5,
            relevance_k=3,
            ws_id="coord-1",
            user_id="user-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        scopes = coord._visible_scopes()
        assert scopes == [("coordinator", "coord-1")]
        # And: search uses those same scopes (no global/user fan-in)
        coord.messages = [{"role": "user", "content": "anything"}]
        with patch(
            "turnstone.core.session.search_visible_structured_memories",
            return_value=[],
        ) as search_mock:
            coord._search_visible_memories("anything", limit=5)
        search_mock.assert_called_once()
        # Second positional arg is the scopes list
        assert search_mock.call_args.args[1] == [("coordinator", "coord-1")]


class TestMemorySearchToolExecution:
    """End-to-end test of ``memory(action='search')`` through _exec_memory.

    Drives the actual tool dispatch (not just the storage facade) so the
    OR-of-terms fix and the coalesced ``memory.search`` log get exercised
    together.
    """

    def test_search_action_returns_or_of_terms_results(self, tmp_db):
        """Multi-word query returns rows where ANY term matches — not all."""
        from turnstone.core.memory import save_structured_memory

        save_structured_memory("postgres_notes", "host=localhost port=5432")
        save_structured_memory("redis_notes", "host=redis port=6379")
        save_structured_memory("unrelated", "completely different")

        session = _make_session()
        item = session._prepare_memory(
            "call-1",
            {"action": "search", "query": "postgres no_such_word_a no_such_word_b"},
        )
        # Sanity: prepare returned a search-ready dispatch (not an error item)
        assert item.get("action") == "search"

        call_id, msg = session._exec_memory(item)
        assert call_id == "call-1"
        assert "postgres_notes" in msg
        # Other memories don't match any query term
        assert "unrelated" not in msg


class TestPerTurnSearchCache:
    """The per-turn cache spares redundant SQL across mid-turn rebuilds."""

    def test_repeated_search_in_same_turn_hits_cache(self, tmp_db):
        from turnstone.core.memory import save_structured_memory

        save_structured_memory("hello_mem", "alpha beta gamma")
        session = _make_session()
        with patch(
            "turnstone.core.session.search_visible_structured_memories",
            return_value=[],
        ) as backend_mock:
            session._search_visible_memories("alpha beta", limit=5)
            session._search_visible_memories("alpha beta", limit=5)
            session._search_visible_memories("alpha beta", limit=5)
        # 3 calls but only 1 backend hit — cache absorbed the rest
        assert backend_mock.call_count == 1

    def test_user_turn_invalidates_cache(self, tmp_db):
        from turnstone.core.memory import save_structured_memory

        save_structured_memory("hello_mem", "alpha")
        session = _make_session()
        with patch(
            "turnstone.core.session.search_visible_structured_memories",
            return_value=[],
        ) as backend_mock:
            session._search_visible_memories("alpha", limit=5)
            session._invalidate_memory_cache()  # simulates new user turn
            session._search_visible_memories("alpha", limit=5)
        assert backend_mock.call_count == 2
