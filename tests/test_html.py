"""Tests for turnstone.core.web — strip_html."""

from turnstone.core.web import strip_html


class TestStripHtml:
    def test_removes_tags(self):
        assert strip_html("<b>hello</b>") == "hello"

    def test_removes_nested_tags(self):
        assert strip_html("<div><p>text</p></div>") == "text"

    def test_decodes_entities(self):
        assert strip_html("&amp; &lt; &gt;") == "& < >"

    def test_collapses_whitespace(self):
        result = strip_html("hello     world")
        assert result == "hello world"

    def test_collapses_blank_lines(self):
        result = strip_html("a\n\n\n\n\nb")
        assert result == "a\n\nb"

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_strips_leading_trailing_whitespace(self):
        assert strip_html("  hello  ") == "hello"

    def test_complex_html(self):
        html = "<html><body><h1>Title</h1><p>Some &amp; text</p></body></html>"
        result = strip_html(html)
        assert "Title" in result
        assert "Some & text" in result
        assert "<" not in result

    def test_self_closing_tags(self):
        result = strip_html("hello<br/>world")
        assert result == "helloworld"
