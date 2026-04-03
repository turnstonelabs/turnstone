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

    # -- invisible element stripping -----------------------------------------

    def test_strips_script_content(self):
        html = "<p>before</p><script>var x = 1;</script><p>after</p>"
        result = strip_html(html)
        assert "var x" not in result
        assert "before" in result
        assert "after" in result

    def test_strips_style_content(self):
        html = "<style>.foo { color: red; }</style><p>visible</p>"
        result = strip_html(html)
        assert "color" not in result
        assert "visible" in result

    def test_strips_template_content(self):
        html = "<template><div>hidden</div></template><p>shown</p>"
        result = strip_html(html)
        assert "hidden" not in result
        assert "shown" in result

    def test_strips_noscript_content(self):
        html = "<noscript>Enable JS</noscript><p>content</p>"
        result = strip_html(html)
        assert "Enable JS" not in result
        assert "content" in result

    def test_strips_multiple_script_blocks(self):
        html = "<script>a()</script><p>middle</p><script>b()</script>"
        result = strip_html(html)
        assert "a()" not in result
        assert "b()" not in result
        assert "middle" in result

    def test_strips_multiline_script(self):
        html = "<script>\nfunction foo() {\n  return 1;\n}\n</script><p>ok</p>"
        result = strip_html(html)
        assert "function" not in result
        assert "ok" in result

    def test_strips_script_case_insensitive(self):
        html = "<SCRIPT>code()</SCRIPT><p>text</p>"
        result = strip_html(html)
        assert "code()" not in result
        assert "text" in result

    def test_strips_script_with_attributes(self):
        html = '<script type="text/javascript" src="app.js">init();</script><p>done</p>'
        result = strip_html(html)
        assert "init()" not in result
        assert "done" in result
