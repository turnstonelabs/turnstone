"""Tests for SQLite FTS5 query building and LIKE escaping."""

from turnstone.core.storage._sqlite import _escape_like, _fts5_query


class TestFts5Query:
    def test_single_word(self):
        result = _fts5_query("hello")
        assert result == '"hello"'

    def test_multiple_words_joined_with_and(self):
        result = _fts5_query("hello world")
        assert result == '"hello" "world"'

    def test_special_chars_safely_quoted(self):
        result = _fts5_query("test*")
        assert result == '"test*"'

    def test_dash_safely_quoted(self):
        result = _fts5_query("-negative")
        assert result == '"-negative"'

    def test_embedded_double_quotes(self):
        result = _fts5_query('say"hello')
        assert result == '"say""hello"'

    def test_empty_query(self):
        assert _fts5_query("") == ""

    def test_whitespace_only(self):
        assert _fts5_query("   ") == ""


class TestEscapeLike:
    def test_percent_escaped(self):
        assert _escape_like("100%") == "100\\%"

    def test_underscore_escaped(self):
        assert _escape_like("a_b") == "a\\_b"

    def test_backslash_escaped(self):
        assert _escape_like("a\\b") == "a\\\\b"

    def test_no_metacharacters(self):
        assert _escape_like("hello") == "hello"

    def test_combined(self):
        assert _escape_like("50%_off\\sale") == "50\\%\\_off\\\\sale"
