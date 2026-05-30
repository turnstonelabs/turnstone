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

    def test_br_becomes_newline(self):
        # <br> is a line break, not a no-op: text must not glue together.
        assert strip_html("hello<br/>world") == "hello\nworld"
        assert strip_html("hello<br>world") == "hello\nworld"

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


class TestStripHtmlBlockStructure:
    """Block-level boundaries become line breaks instead of gluing text together."""

    def test_paragraphs_separated(self):
        assert strip_html("<p>a</p><p>b</p>") == "a\n\nb"

    def test_heading_not_glued_to_body(self):
        result = strip_html("<h2>Title</h2><p>Body text</p>")
        assert "TitleBody" not in result
        assert result == "Title\n\nBody text"

    def test_list_items_separated(self):
        result = strip_html("<ul><li>first</li><li>second</li></ul>")
        assert "firstsecond" not in result
        lines = [ln for ln in result.splitlines() if ln.strip()]
        assert lines == ["first", "second"]

    def test_table_cells_not_glued(self):
        # The motivating case: digits in adjacent cells must not run together.
        result = strip_html("<tr><td>123</td><td>456</td></tr>")
        assert "123456" not in result
        assert "123" in result
        assert "456" in result

    def test_divs_separated(self):
        result = strip_html("<div>one</div><div>two</div>")
        assert "onetwo" not in result

    def test_inline_tags_still_join_without_breaks(self):
        # Inline elements carry no block boundary and must not introduce newlines.
        assert strip_html("<b>foo</b>bar") == "foobar"
        assert strip_html("a<span>b</span>c") == "abc"

    def test_block_tags_with_attributes(self):
        result = strip_html('<p class="x">a</p><p id="y">b</p>')
        assert result == "a\n\nb"

    def test_no_false_match_on_similar_tag_names(self):
        # <picture>/<param> are not newline tags; <p> is. Name lookup is exact,
        # so lookalike prefixes must not introduce breaks.
        assert strip_html("<picture>img</picture>") == "img"
        assert strip_html("<param>x</param>") == "x"

    def test_uppercase_tags_break(self):
        # Tag-name matching lowercases; real-world HTML often uses uppercase tags.
        assert strip_html("<P>a</P><P>b</P>") == "a\n\nb"
        assert strip_html("a<BR>b") == "a\nb"

    def test_br_with_attributes_breaks(self):
        # A <br> carrying attributes must still produce a line break, not glue.
        assert strip_html("one<br clear='all'>two") == "one\ntwo"

    def test_pathological_whitespace_is_linear(self):
        # A '<' (or '<br') followed by a long whitespace run must not trigger
        # catastrophic backtracking. With a linear scan this is instant; a quadratic
        # pattern would make it crawl. Asserting it returns is the regression guard.
        assert isinstance(strip_html("<" + " " * 50_000 + "x"), str)
        assert isinstance(strip_html("<br" + " " * 50_000), str)
