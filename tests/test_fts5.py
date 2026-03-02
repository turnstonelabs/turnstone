"""Tests for turnstone.core.memory — fts5_query and escape_like."""

from turnstone.core.memory import fts5_query, escape_like


class TestFts5Query:
    def test_single_word(self):
        result = fts5_query("hello")
        assert result == '"hello"'

    def test_multiple_words_joined_with_and(self):
        result = fts5_query("hello world")
        # Each word is quoted; space between = implicit AND
        assert result == '"hello" "world"'

    def test_special_chars_safely_quoted(self):
        result = fts5_query("test*")
        assert result == '"test*"'

    def test_dash_safely_quoted(self):
        result = fts5_query("-negative")
        assert result == '"-negative"'

    def test_embedded_double_quotes(self):
        # Double quotes inside a term are doubled per FTS5 convention
        result = fts5_query('say"hello')
        assert result == '"say""hello"'

    def test_empty_query(self):
        assert fts5_query("") == ""

    def test_whitespace_only(self):
        assert fts5_query("   ") == ""


class TestEscapeLike:
    def test_percent_escaped(self):
        assert escape_like("100%") == "100\\%"

    def test_underscore_escaped(self):
        assert escape_like("a_b") == "a\\_b"

    def test_backslash_escaped(self):
        assert escape_like("a\\b") == "a\\\\b"

    def test_no_metacharacters(self):
        assert escape_like("hello") == "hello"

    def test_combined(self):
        assert escape_like("50%_off\\sale") == "50\\%\\_off\\\\sale"
