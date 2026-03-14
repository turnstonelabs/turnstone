"""Tests for turnstone.core.memory_relevance — scoring, formatting, context extraction."""

from turnstone.core.memory_relevance import (
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
