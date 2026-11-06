"""Tests for turnstone.core.web — strip_html."""

import time
from unittest.mock import patch

import httpx2
import pytest

from turnstone.core.web import LINK_TARGET_CHAR_CAP, _PageText, strip_html

_PAGE = "https://example.com/docs/page"
_UNBOUNDED = 10**12


def _strip(html: str, *, page_url: str = _PAGE, xml: bool = False) -> str:
    text, complete = strip_html(html, page_url=page_url, stop_after_chars=_UNBOUNDED, xml=xml)
    assert complete
    return text


class TestStripHtml:
    def test_removes_tags(self):
        assert _strip("<b>hello</b>") == "hello"

    def test_removes_nested_tags(self):
        assert _strip("<div><p>text</p></div>") == "text"

    def test_decodes_entities(self):
        assert _strip("&amp; &lt; &gt;") == "& < >"

    def test_collapses_whitespace(self):
        result = _strip("hello     world")
        assert result == "hello world"

    def test_collapses_blank_lines(self):
        result = _strip("a\n\n\n\n\nb")
        assert result == "a\n\nb"

    def test_empty_string(self):
        assert _strip("") == ""

    def test_strips_leading_trailing_whitespace(self):
        assert _strip("  hello  ") == "hello"

    def test_complex_html(self):
        html = "<html><body><h1>Title</h1><p>Some &amp; text</p></body></html>"
        result = _strip(html)
        assert "Title" in result
        assert "Some & text" in result
        assert "<" not in result

    def test_br_becomes_newline(self):
        # <br> is a line break, not a no-op: text must not glue together.
        assert _strip("hello<br/>world") == "hello\nworld"
        assert _strip("hello<br>world") == "hello\nworld"

    # -- invisible element stripping -----------------------------------------

    def test_strips_script_content(self):
        html = "<p>before</p><script>var x = 1;</script><p>after</p>"
        result = _strip(html)
        assert "var x" not in result
        assert "before" in result
        assert "after" in result

    def test_strips_style_content(self):
        html = "<style>.foo { color: red; }</style><p>visible</p>"
        result = _strip(html)
        assert "color" not in result
        assert "visible" in result

    def test_strips_template_content(self):
        html = "<template><div>hidden</div></template><p>shown</p>"
        result = _strip(html)
        assert "hidden" not in result
        assert "shown" in result

    def test_strips_noscript_content(self):
        html = "<noscript>Enable JS</noscript><p>content</p>"
        result = _strip(html)
        assert "Enable JS" not in result
        assert "content" in result

    def test_strips_multiple_script_blocks(self):
        html = "<script>a()</script><p>middle</p><script>b()</script>"
        result = _strip(html)
        assert "a()" not in result
        assert "b()" not in result
        assert "middle" in result

    def test_strips_multiline_script(self):
        html = "<script>\nfunction foo() {\n  return 1;\n}\n</script><p>ok</p>"
        result = _strip(html)
        assert "function" not in result
        assert "ok" in result

    def test_strips_script_case_insensitive(self):
        html = "<SCRIPT>code()</SCRIPT><p>text</p>"
        result = _strip(html)
        assert "code()" not in result
        assert "text" in result

    def test_strips_script_with_attributes(self):
        html = '<script type="text/javascript" src="app.js">init();</script><p>done</p>'
        result = _strip(html)
        assert "init()" not in result
        assert "done" in result


class TestStripHtmlBlockStructure:
    """Block-level boundaries become line breaks instead of gluing text together."""

    def test_paragraphs_separated(self):
        assert _strip("<p>a</p><p>b</p>") == "a\n\nb"

    def test_heading_not_glued_to_body(self):
        result = _strip("<h2>Title</h2><p>Body text</p>")
        assert "TitleBody" not in result
        assert result == "Title\n\nBody text"

    def test_list_items_separated(self):
        result = _strip("<ul><li>first</li><li>second</li></ul>")
        assert "firstsecond" not in result
        lines = [ln for ln in result.splitlines() if ln.strip()]
        assert lines == ["first", "second"]

    def test_table_cells_not_glued(self):
        # The motivating case: digits in adjacent cells must not run together.
        result = _strip("<tr><td>123</td><td>456</td></tr>")
        assert "123456" not in result
        assert "123" in result
        assert "456" in result

    def test_divs_separated(self):
        result = _strip("<div>one</div><div>two</div>")
        assert "onetwo" not in result

    def test_inline_tags_still_join_without_breaks(self):
        # Inline elements carry no block boundary and must not introduce newlines.
        assert _strip("<b>foo</b>bar") == "foobar"
        assert _strip("a<span>b</span>c") == "abc"

    def test_block_tags_with_attributes(self):
        result = _strip('<p class="x">a</p><p id="y">b</p>')
        assert result == "a\n\nb"

    def test_no_false_match_on_similar_tag_names(self):
        # <picture>/<param> are not newline tags; <p> is. Name lookup is exact,
        # so lookalike prefixes must not introduce breaks.
        assert _strip("<picture>img</picture>") == "img"
        assert _strip("<param>x</param>") == "x"

    def test_uppercase_tags_break(self):
        # Tag-name matching lowercases; real-world HTML often uses uppercase tags.
        assert _strip("<P>a</P><P>b</P>") == "a\n\nb"
        assert _strip("a<BR>b") == "a\nb"

    def test_br_with_attributes_breaks(self):
        # A <br> carrying attributes must still produce a line break, not glue.
        assert _strip("one<br clear='all'>two") == "one\ntwo"

    def test_pathological_whitespace_is_linear(self):
        # A '<' (or '<br') followed by a long whitespace run must not trigger
        # catastrophic backtracking. With a linear scan this is instant; a quadratic
        # pattern would make it crawl. Asserting it returns is the regression guard.
        assert isinstance(_strip("<" + " " * 50_000 + "x"), str)
        assert isinstance(_strip("<br" + " " * 50_000), str)


class TestStripHtmlLinks:
    """A link keeps its target, resolved to an absolute URL, so a model can copy it."""

    def test_relative_link_resolves_against_the_page(self):
        result = _strip('<p>See <a href="install">the install guide</a>.</p>')
        assert result == "See [the install guide](https://example.com/docs/install)."

    def test_root_and_scheme_relative_links_resolve(self):
        result = _strip('<a href="/faq">FAQ</a> <a href="//cdn.example.org/f.zip">zip</a>')
        assert result == "[FAQ](https://example.com/faq) [zip](https://cdn.example.org/f.zip)"

    def test_absolute_link_is_kept(self):
        assert _strip('<a href="https://example.net/x">x</a>') == "[x](https://example.net/x)"

    def test_page_url_is_the_served_address(self):
        # The caller passes the address after redirects: links resolve against it.
        result = _strip('<a href="next">n</a>', page_url="https://example.com/v2/start")
        assert result == "[n](https://example.com/v2/next)"

    def test_page_credentials_do_not_reach_links(self):
        page_url = "https://user:s3cret@wiki.example/docs/page"
        result = _strip('<a href="install">guide</a>', page_url=page_url)
        assert result == "[guide](https://wiki.example/docs/install)"

    def test_long_fragment_in_page_url_does_not_stop_the_conversion(self):
        # Percent-encoded, as the served URL arrives, this fragment passes httpx2's URL
        # length limit. Links resolve without the fragment, so they keep their targets.
        page_url = str(httpx2.URL("https://example.com/start#" + chr(0xE9) * 11_000))
        assert len(page_url) > 65_536
        result = _strip('<p>See <a href="x">X</a>.</p>', page_url=page_url)
        assert result == "See [X](https://example.com/x)."

    def test_long_page_address_keeps_every_target_that_fits(self):
        # Only a link whose own target passes the cap loses it.
        page_url = "https://example.com/" + "a" * LINK_TARGET_CHAR_CAP + "/page"
        html = (
            '<a href="x">rel</a> <a href="/about">root</a> <a href="#top">top</a> '
            '<a href="https://example.net/y">abs</a> <a href="mailto:a@example.com">mail</a>'
        )
        assert _strip(html, page_url=page_url) == (
            "rel [root](https://example.com/about) [top](#top) [abs](https://example.net/y) "
            "[mail](mailto:a@example.com)"
        )

    def test_page_address_that_does_not_parse_keeps_absolute_targets(self):
        html = '<a href="x">rel</a> <a href="https://example.net/y">abs</a>'
        assert _strip(html, page_url="https://[::1") == "rel [abs](https://example.net/y)"

    def test_base_href_sets_what_links_resolve_against(self):
        result = _strip('<base href="https://example.org/root/"><a href="x">X</a>')
        assert result == "[X](https://example.org/root/x)"

    def test_relative_base_href_resolves_against_the_page(self):
        assert _strip('<base href="/v2/"><a href="x">X</a>') == "[X](https://example.com/v2/x)"

    def test_only_the_first_base_href_counts(self):
        result = _strip('<base target="_top"><base href="/v2/"><base href="/v3/"><a href="x">X</a>')
        assert result == "[X](https://example.com/v2/x)"

    def test_base_href_that_is_not_a_web_url_is_ignored(self):
        result = _strip('<base href="javascript:void(0)"><a href="x">X</a>')
        assert result == "[X](https://example.com/docs/x)"

    def test_mailto_link_is_kept(self):
        assert _strip('<a href="mailto:a@example.com">mail</a>') == "[mail](mailto:a@example.com)"

    @pytest.mark.parametrize(
        "href",
        [
            "javascript:void(0)",
            "data:text/plain,hi",
            "tel:+15550100",
            "http://[::1",
            "http://[::1%]]/",
            "/" + chr(0xDFFF),
        ],
        ids=["javascript", "data", "tel", "invalid", "ipv6-zone", "unencodable"],
    )
    def test_link_without_a_usable_target_keeps_its_text(self, href):
        assert _strip(f'a <a href="{href}">b</a> c') == "a b c"

    @pytest.mark.parametrize(
        "href",
        [
            "..\\index.html",
            "//other.example\\@example.net/x",
            "///cdn.example.org/f.zip",
            "https:///cdn.example.org/f.zip",
            "http:foo",
            ":foo",
        ],
        ids=[
            "backslash",
            "backslash-authority",
            "three-slashes",
            "scheme-three-slashes",
            "scheme-without-slashes",
            "leading-colon",
        ],
    )
    def test_href_a_browser_resolves_differently_keeps_its_text(self, href):
        # httpx2 would resolve these to another host or path than a browser does.
        assert _strip(f'a <a href="{href}">b</a> c') == "a b c"

    @pytest.mark.parametrize("href", ["", "#", "page", "page#"])
    def test_link_to_the_page_itself_keeps_its_text(self, href):
        assert _strip(f'<a href="{href}">here</a>') == "here"

    @pytest.mark.parametrize(
        ("href", "target"),
        [("#re.sub", "#re.sub"), ("page#top", "#top"), ("#a(b)", "#a%28b%29")],
    )
    def test_link_to_a_named_place_in_the_page_shows_its_fragment(self, href, target):
        # The fragment resolves against the page's address without repeating it.
        assert _strip(f'<a href="{href}">here</a>') == f"[here]({target})"

    def test_page_fragment_does_not_make_in_page_links_absolute(self):
        html = '<a href="#other">here</a> <a href="#">top</a>'
        page_url = "https://example.com/docs/page#install"
        assert _strip(html, page_url=page_url) == "[here](#other) top"

    def test_fragment_under_another_base_is_another_page(self):
        result = _strip('<base href="https://example.org/root/"><a href="#x">X</a>')
        assert result == "[X](https://example.org/root/#x)"

    def test_link_with_another_query_is_another_page(self):
        assert (
            _strip('<a href="?page=2">next</a>') == "[next](https://example.com/docs/page?page=2)"
        )

    def test_link_without_text_shows_its_url(self):
        result = _strip('<a href="/icon"><svg><path d="M0 0"/></svg></a>')
        assert result == "<https://example.com/icon>"

    def test_link_whose_text_is_its_url_shows_it_once(self):
        result = _strip('<a href="https://example.com/u">https://example.com/u</a>')
        assert result == "<https://example.com/u>"

    def test_link_text_whitespace_collapses_outside_the_link(self):
        result = _strip('word<a href="/x"> spaced  out\t</a>word')
        assert result == "word [spaced out](https://example.com/x) word"

    def test_link_text_keeps_its_line_breaks(self):
        html = (
            '<a href="/c"><div><h3>Title</h3><p>Summary</p></div></a>'
            '<a href="/d"><div>Two</div></a>'
            '<a href="/addr">123 Main St<br>Springfield</a>'
        )
        result = _strip(html)
        assert result == (
            "[Title\nSummary](https://example.com/c)\n\n[Two](https://example.com/d)\n"
            "[123 Main St\nSpringfield](https://example.com/addr)"
        )

    def test_unclosed_link_keeps_the_lines_it_runs_over(self):
        html = "<p>Intro</p><a href='/x'>Start<h2>Heading</h2><p>Para one</p><ul><li>a</li></ul>"
        assert _strip(html) == "Intro\n[Start\nHeading\nPara one\na](https://example.com/x)"

    def test_target_cannot_spell_a_second_link(self):
        # Percent-encoding ( ) [ ] after the host keeps the whole href one link.
        result = _strip('<p>See <a href="/x)[Login](https://evil.example/login">Docs</a>.</p>')
        assert (
            result
            == "See [Docs](https://example.com/x%29%5BLogin%5D%28https://evil.example/login)."
        )

    def test_ipv6_host_keeps_its_brackets(self):
        result = _strip('<a href="http://[::1]:8080/p(a)">v6</a>')
        assert result == "[v6](http://[::1]:8080/p%28a%29)"

    def test_mailto_target_is_encoded_throughout(self):
        # A mailto URL has no host, so a "://" in its query is no reason to skip any of it.
        result = _strip('<a href="mailto:a@example.com?body=x://y[1](2)">m</a>')
        assert result == "[m](mailto:a@example.com?body=x://y%5B1%5D%282%29)"

    @pytest.mark.parametrize(
        "href",
        ["https://u)@example.org/p", "http://user:pa(ss@example.org/p", "https://example.org)/p"],
        ids=["userinfo-close", "userinfo-open", "host"],
    )
    def test_parenthesis_before_the_path_keeps_only_text(self, href):
        # httpx2 leaves ( ) raw there, where one would end the rendered link early.
        assert _strip(f'a <a href="{href}">b</a> c') == "a b c"

    @pytest.mark.parametrize(
        "href",
        ["//:80", "//a:-1/", "https://example.com:65536/", "http://[", "/a/%2e%2e/b"],
        ids=["empty-host", "negative-port", "port-too-large", "lone-bracket", "encoded-dots"],
    )
    def test_target_a_browser_refuses_or_rewrites_keeps_only_text(self, href):
        assert _strip(f'a <a href="{href}">b</a> c') == "a b c"

    @pytest.mark.parametrize(
        "href",
        [
            "http://xn--bcher-kva.my_site.example/",
            "http://xn--bcher-kva." + ".".join(["a" * 60] * 5) + ".example/",
        ],
        ids=["rejected-label", "too-long"],
    )
    def test_internationalized_host_its_decoder_rejects_still_converts(self, href):
        # httpx2 decodes a host holding "xn--" on each read of URL.host, and raises for these.
        assert _strip(f'<a href="{href}">x</a> ok') == f"[x]({href}) ok"

    def test_base_href_on_a_host_its_decoder_rejects_still_converts(self):
        result = _strip('<base href="http://xn--bcher-kva.my_site.example/r/"><a href="x">X</a>')
        assert result == "[X](http://xn--bcher-kva.my_site.example/r/x)"

    def test_page_served_from_an_internationalized_host_resolves_links(self):
        result = _strip('<a href="x">X</a>', page_url="https://xn--bcher-kva.example/docs/")
        assert result == "[X](https://xn--bcher-kva.example/docs/x)"

    def test_percent_encoded_base_host_leaves_relative_links_text(self):
        # A browser decodes the host; falling back to the page would render the wrong one.
        html = (
            '<base href="https://cdn.ex%61mple.org/root/"><a href="x">X</a> '
            '<a href="https://example.net/y">Y</a>'
        )
        assert _strip(html) == "X [Y](https://example.net/y)"

    def test_hrefs_settled_without_parsing_are_not_memoized(self):
        # The memo then holds no more entries than the resolution budget lets the scan
        # parse, whatever the page's size: a non-web scheme, a relative href with no base,
        # and an href first seen after the budget ran out are each settled without a parse.
        page = _PageText(_PAGE)
        for i in range(100):
            page.open(f'href="tel:{i}"')
        assert page._resolved == {}
        page.open('href="/x"')
        assert page._resolved == {"/x": "https://example.com/x"}
        no_base = _PageText("http://[::1")
        for i in range(100):
            no_base.open(f'href="/r{i}"')
        assert no_base._resolved == {}
        with patch("turnstone.core.web._RESOLVE_BUDGET_CHARS", 60):
            spent = _PageText(_PAGE)
        for i in range(100):
            spent.open(f'href="/r{i}"')
        assert len(spent._resolved) == 2  # the two hrefs the budget paid for

    @pytest.mark.parametrize(
        "base", ["https://cdn.example.org:99999/", "https://:80/"], ids=["port", "empty-host"]
    )
    def test_base_href_a_browser_refuses_leaves_the_page_as_base(self, base):
        html = f'<base href="{base}"><a href="x">rel</a> <a href="#frag">frag</a>'
        assert _strip(html) == "[rel](https://example.com/docs/x) [frag](#frag)"

    def test_target_longer_than_the_cap_keeps_only_its_text(self):
        href = "/" + "a" * LINK_TARGET_CHAR_CAP
        assert _strip(f'a <a href="{href}">b</a> c') == "a b c"

    def test_long_base_href_stays_the_base(self):
        # Falling back to the page would render targets the page does not link to.
        base = "https://cdn.example.org/" + "b" * LINK_TARGET_CHAR_CAP + "/"
        html = f'<base href="{base}"><a href="/x">root</a> <a href="x">rel</a>'
        assert _strip(html) == "[root](https://cdn.example.org/x) rel"

    def test_base_href_of_another_scheme_leaves_relative_links_text(self):
        html = (
            '<base href="ftp://files.example/pub/"><a href="x">X</a> '
            '<a href="https://example.net/y">Y</a>'
        )
        assert _strip(html) == "X [Y](https://example.net/y)"

    @pytest.mark.parametrize(
        "base", ["http://[::1", "http://[::1%]]/"], ids=["invalid", "ipv6-zone"]
    )
    def test_base_href_that_cannot_be_resolved_is_ignored(self, base):
        assert _strip(f'<base href="{base}"><a href="x">X</a>') == "[X](https://example.com/docs/x)"

    def test_base_href_after_a_link_applies_to_later_links_only(self):
        html = '<a href="x">A</a> <base href="/v2/"><a href="x">B</a>'
        assert _strip(html) == "[A](https://example.com/docs/x) [B](https://example.com/v2/x)"

    def test_anchor_inside_an_open_one_closes_it(self):
        result = _strip('<a href="/one">one <a href="/two">two</a> three')
        assert result == "[one](https://example.com/one) [two](https://example.com/two) three"

    def test_unclosed_link_ends_with_the_document(self):
        assert _strip('<a href="/x">tail') == "[tail](https://example.com/x)"

    def test_stray_end_tag_and_named_anchor_keep_text(self):
        assert _strip('a</a> <a name="top">b</a> c') == "a b c"

    def test_commented_out_link_is_not_rendered(self):
        html = '<p>a</p><!-- <p>x</p><a href="/old">Old</a> --><p>b</p>'
        assert _strip(html) == "a\n\nb"

    def test_link_inside_a_script_is_not_rendered(self):
        assert _strip('<script>"<a href=/x>x</a>"</script>ok') == "ok"


class TestStripHtmlHref:
    """The href is read as a browser reads it."""

    @pytest.mark.parametrize(
        "tag",
        ['<a href="/x">', "<a href='/x'>", "<a href=/x>", '<A HREF="/x">', '<a\nhref = "/x" >'],
        ids=["double", "single", "unquoted", "uppercase", "spaced"],
    )
    def test_attribute_forms(self, tag):
        assert _strip(f"{tag}X</a>") == "[X](https://example.com/x)"

    def test_quoted_gt_does_not_end_the_tag(self):
        assert _strip('<a title="1 > 0" href="/x">X</a>') == "[X](https://example.com/x)"

    def test_href_inside_another_attribute_is_not_read(self):
        result = _strip('<a title="href=/evil" href="/real">R</a>')
        assert result == "[R](https://example.com/real)"

    def test_data_href_is_not_an_href(self):
        assert _strip('<a data-href="/x">X</a>') == "X"

    def test_first_href_wins(self):
        assert _strip('<a href="/one" href="/two">X</a>') == "[X](https://example.com/one)"

    def test_character_references_decode_as_in_an_attribute(self):
        # A named reference without ";" stays literal before "=" or a letter or digit,
        # so query strings keep "&region", "&copy" and "&amp=" while "&amp;" decodes.
        html = '<a href="/s?a=1&amp;b=2&region=us&copy=3&amp=4&notit;5&#38;c=6&amp&d">q</a>'
        result = _strip(html)
        assert result == "[q](https://example.com/s?a=1&b=2&region=us&copy=3&amp=4&notit;5&c=6&&d)"

    def test_spaces_at_the_ends_and_newlines_inside_are_dropped(self):
        assert _strip('<a href=" /a\nb ">x</a>') == "[x](https://example.com/ab)"

    def test_quote_inside_an_unquoted_value_opens_nothing(self):
        # Only the "=" after an attribute's name can open a quoted value.
        html = '<p><a href=/x?q="y>text</a> more</p><p>Para <b class="c">bold</b> end</p>'
        assert _strip(html) == "[text](https://example.com/x?q=%22y) more\n\nPara bold end"

    def test_equals_without_a_name_starts_a_name(self):
        assert _strip('<p><a ="x>y" href=/a>link</a></p><p>after</p>') == (
            'y" href=/a>link\n\nafter'
        )

    def test_quote_in_a_name_belongs_to_that_name(self):
        result = _strip('<p><a x"href=/evil" href=/real>R</a></p>')
        assert result == "[R](https://example.com/real)"

    def test_quote_that_never_closes_ends_at_the_next_tag_end(self):
        # A browser would read the rest of the page as the value; here the tag ends at the
        # next ">", so one stray quote does not hide the page.
        assert _strip('<p>a</p><div class="x>shown?</div><p>tail</p>') == "a\n\nshown?\n\ntail"

    def test_href_whose_quote_never_closes_is_no_href(self):
        assert _strip('<p><a href="/x>Link</a></p><p>tail</p>') == "Link\n\ntail"

    def test_long_decimal_reference_becomes_the_replacement_character(self):
        refs = "&#" + "0" * 4400 + "65;" + "&#" + "1" * 5000 + ";"
        html = f'<p>{refs}</p><a href="/x?q={refs}">l</a>'
        assert _strip(html) == "A" + chr(0xFFFD) + "\n[l](https://example.com/x?q=A%EF%BF%BD)"


class TestStripHtmlMarkup:
    """Markup that shows nothing stays hidden, and a "<" that opens nothing is text."""

    def test_comment_is_hidden(self):
        assert _strip("a<!-- x > y -->b") == "ab"

    def test_unclosed_comment_hides_the_rest(self):
        assert _strip("a<!-- b") == "a"

    def test_doctype_and_processing_instruction_are_hidden(self):
        assert _strip('<!DOCTYPE html><?xml version="1.0"?><p>x</p>') == "x"

    def test_lt_that_opens_no_tag_is_text(self):
        assert _strip("1 < 2 and 3 > 2") == "1 < 2 and 3 > 2"

    def test_tag_left_open_at_the_end_is_dropped(self):
        assert _strip("text <b") == "text"

    def test_unclosed_script_hides_the_rest(self):
        assert _strip("<p>a</p><script>x") == "a"

    def test_script_ends_only_at_its_own_end_tag(self):
        assert _strip("<script>a</scripts>b</script>c") == "c"

    def test_non_ascii_lookalike_does_not_end_a_script(self):
        # Tag names match ASCII letters only; a long s folds to "s" under Unicode case rules.
        assert _strip(f"<script>a</{chr(0x17F)}cript>b</script>c") == "c"

    def test_quoted_attribute_holding_markup_does_not_leak(self):
        assert _strip('<div data-mw=\'{"html":"<b>x</b>"}\'>shown</div>') == "shown"

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            ("<p>a</p><!-- note --!><p>Visible.</p>", "a\n\nVisible."),
            ("a<!-->b", "ab"),
            ("a<!--->b", "ab"),
            ("a<!----!>b", "ab"),
            ("a<!--!>b<p>hidden</p>", "a"),
            ("a<!---!>b<p>hidden</p>", "a"),
        ],
        ids=["bang-close", "empty", "dash", "dash-bang", "bang-open", "dash-bang-open"],
    )
    def test_comment_ends_where_a_browser_ends_it(self, html, expected):
        assert _strip(html) == expected

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            ("<title>Tips: the <script> tag</title><p>Body</p>", "Tips: the <script> tag\nBody"),
            ("<title>A <!-- B</title><p>Body</p>", "A <!-- B\nBody"),
            ("<textarea>Put <b>&amp;</b> here</textarea>", "Put <b>&</b> here"),
            ("<xmp>a &amp; <b></xmp>", "a &amp; <b>"),
            ("<title>No end tag <p>still title", "No end tag <p>still title"),
        ],
        ids=["title-script", "title-comment", "textarea", "xmp", "no-end-tag"],
    )
    def test_title_textarea_and_xmp_content_is_text(self, html, expected):
        assert _strip(html) == expected

    def test_markup_inside_a_title_makes_no_base_or_link(self):
        html = (
            '<title><base href="https://attacker.example/"></title>'
            '<textarea><a href="https://evil.example/">x</a></textarea>'
            '<p><a href="/login">Log in</a></p>'
        )
        assert _strip(html) == (
            '<base href="https://attacker.example/"><a href="https://evil.example/">x</a>\n'
            "[Log in](https://example.com/login)"
        )

    @pytest.mark.parametrize("name", ["iframe", "noembed"])
    def test_fallback_content_a_browser_does_not_render_is_hidden(self, name):
        assert _strip(f'<{name}><a href="https://evil.example/">x</a></{name}>ok') == "ok"

    def test_noframes_content_shows(self):
        # On a frameset page it is the only text and link there are.
        html = (
            "<frameset cols='20%,80%'><frame src='nav.html'><frame src='main.html'>"
            "<noframes><p>This site uses frames. <a href='main.html'>Main page</a></p>"
            "</noframes></frameset>"
        )
        assert (
            _strip(html) == "This site uses frames. [Main page](https://example.com/docs/main.html)"
        )

    @pytest.mark.parametrize("element", ["<style/>", "<script/>", "<title/>"])
    def test_self_closed_element_inside_svg_is_empty(self, element):
        html = (
            f"<h1>Pricing</h1><svg viewBox='0 0 10 10'>{element}<path d='M0 0h10v10z'/></svg>"
            "<p>The Pro plan costs $20.</p>"
        )
        assert _strip(html) == "Pricing\n\nThe Pro plan costs $20."

    def test_svg_closes_before_html_reads_self_closing_again(self):
        # Outside svg and math, a "/" in a script's start tag means nothing, as in a browser.
        assert _strip("<svg><path/></svg><p>a</p><script/>hidden") == "a"

    def test_template_declaring_a_shadow_root_is_rendered(self):
        html = (
            '<product-card><template shadowrootmode="open"><h2>Widget Pro</h2></template>'
            "<p>In stock.</p></product-card><template><p>hidden</p></template>"
        )
        assert _strip(html) == "Widget Pro\n\nIn stock."

    def test_xhtml_self_closed_script_is_empty(self):
        html = (
            '<html><head><title>T</title><script src="/a.js"/><base href="/v2/"/></head>'
            '<body><p>Body text.</p><a href="x"/></body></html>'
        )
        assert _strip(html, xml=True) == "T\nBody text.\n<https://example.com/v2/x>"
        # Read as HTML, a browser ignores the "/" and the script hides the rest.
        assert _strip(html) == "T"


class TestStripHtmlLinearTime:
    """Pathological markup converts in linear time.

    The patterns before the forward scan searched again from every "<", so unclosed tags
    and scripts took time growing with the square of the page. The tests that time a
    conversion run with coverage paused (no_cover): the C tracer CI uses on Python 3.13
    slows them several times over, and inputs small enough to absorb that would no longer
    make a scan that copies the page at each tag overrun the bound.
    """

    @pytest.mark.no_cover
    @pytest.mark.parametrize(
        "html",
        [
            "<" * 1_000_000,
            "<script>" * 125_000,
            "<style x" * 125_000,
            "<a" * 500_000,
            "<!--" * 250_000,
            "</" * 500_000,
            "<!" * 500_000,
            '<a href="' * 100_000,
            '<a x="' + " y='" * 250_000,
            "<p>" * 330_000,
            '<a href="/x">x' * 10_000,
            "&amp" * 250_000,
            "<!-" * 330_000,
            "<title>" * 140_000,
            '<a x="=' * 140_000,
        ],
        ids=[
            "lt",
            "unclosed-script",
            "unclosed-style",
            "unclosed-tags",
            "unclosed-comments",
            "end-tag-starts",
            "declaration-starts",
            "unclosed-hrefs",
            "unclosed-quotes",
            "block-tags",
            "unclosed-links",
            "references",
            "comment-dashes",
            "unclosed-titles",
            "equals-quotes",
        ],
    )
    def test_conversion_is_linear(self, html):
        start = time.perf_counter()
        strip_html(html, page_url=_PAGE, stop_after_chars=_UNBOUNDED)
        assert time.perf_counter() - start < 2.0

    @pytest.mark.parametrize(
        ("html", "page_url"),
        [
            ('<base href="/' + "a" * 2000 + '/">' + "<a href>x</a>" * 20_000, _PAGE),
            # A non-empty query: Python 3.13's urljoin drops an empty "?", 3.14's keeps it.
            ("<a href=?q>x</a>" * 20_000, "https://example.com/" + "a" * 2000),
        ],
        ids=["long-base", "long-page-url"],
    )
    def test_scan_stops_soon_after_its_budget(self, html, page_url):
        # Each short link would write out the long address it resolves against; the scan
        # stops once the text passes its budget.
        start = time.perf_counter()
        text, complete = strip_html(html, page_url=page_url, stop_after_chars=100_000)
        assert time.perf_counter() - start < 2.0
        assert len(text) <= 100_000 + 2 * LINK_TARGET_CHAR_CAP
        assert not complete

    @pytest.mark.parametrize(
        ("html", "page_url"),
        [
            ("<a href=#>x</a>" * 200_000, _PAGE),
            ("<a href=javascript:x>x</a>" * 100_000, _PAGE),
        ],
        ids=["self-links", "script-links"],
    )
    @pytest.mark.no_cover
    def test_links_that_keep_only_their_text_cost_bounded_work(self, html, page_url):
        # These links add only their text, so the text budget cannot stop them: the memo
        # parses a repeated self-link once, and the scheme check refuses a script link with
        # no parse.
        start = time.perf_counter()
        strip_html(html, page_url=page_url, stop_after_chars=_UNBOUNDED)
        assert time.perf_counter() - start < 2.0

    @pytest.mark.parametrize(
        ("html", "page_url"),
        [
            (
                '<base href="/'
                + "%C3%A9" * 340
                + '/">'
                + "".join(f"<a href=x{i}>x</a>" for i in range(6_000)),
                _PAGE,
            ),
            (
                "".join(f"<a href=?q{i}>x</a>" for i in range(6_000)),
                "https://example.com/" + "a" * 2048,
            ),
        ],
        ids=["over-cap-under-long-base", "over-cap-under-long-page"],
    )
    def test_links_past_the_resolution_budget_cost_no_parse(self, html, page_url):
        # Each of these distinct links would be refused after a parse against a 2 KB base,
        # its target over the cap. Past the budget, a link not resolved before costs no
        # parse: about 50 of the 6,000 are parsed with this budget, where ignoring it, or
        # checking it only after the parse, would parse all 6,000.
        with (
            patch("turnstone.core.web._RESOLVE_BUDGET_CHARS", 100_000),
            patch.object(
                _PageText, "_resolve", autospec=True, side_effect=_PageText._resolve
            ) as parses,
        ):
            text, _ = strip_html(html, page_url=page_url, stop_after_chars=_UNBOUNDED)
        assert parses.call_count <= 100_000 // 2_000 + 1
        assert "shows only its text" in text

    def test_spent_resolution_budget_is_noted_in_the_text(self):
        # The shipped budget, charged a 2 KB base's length for each relative link under it.
        base = '<base href="/' + "%C3%A9" * 340 + '/">'
        links = "".join(f"<a href=x{i}>x</a>" for i in range(5_000))
        text, _ = strip_html(
            base + links + "<a href=/y>y</a>", page_url=_PAGE, stop_after_chars=_UNBOUNDED
        )
        assert "shows only its text" in text
        assert text.endswith("y")

    def test_refused_href_is_resolved_once(self):
        # A link to the page itself is refused after its parse; its repeats reuse that
        # result and spend none of the budget.
        with patch("turnstone.core.web._RESOLVE_BUDGET_CHARS", 1_000):
            text = _strip('<a href="#">x</a>' * 1_000)
        assert text == "x" * 1_000

    def test_budget_note_comes_before_the_first_link_it_affects(self):
        # A link already resolved keeps its target after the note, which says so.
        html = (
            '<a href="/home">Home</a> <a href="/u0">u0</a> <a href="/u1">u1</a> '
            '<a href="/u2">u2</a> <a href="/home">Home again</a> <a href="/new">New</a>'
        )
        with patch("turnstone.core.web._RESOLVE_BUDGET_CHARS", 60):
            text = _strip(html)
        before, note, after = text.partition("[From here on, a link not seen earlier")
        assert note
        assert before.strip() == "[Home](https://example.com/home) [u0](https://example.com/u0)"
        assert after.endswith("u1 u2 [Home again](https://example.com/home) New")

    def test_absolute_links_do_not_pay_for_a_long_page_address(self):
        page_url = "https://example.com/" + "a" * 2048 + "/page"
        html = "".join(f'<a href="https://example.net/item/{i}">i</a> ' for i in range(1_500))
        text = _strip(html, page_url=page_url)
        assert text.count("](https://example.net/item/") == 1_500
        assert "shows only its text" not in text

    def test_repeated_href_is_resolved_once(self):
        # Navigation repeats the same links; charging each repeat would spend the budget
        # on a page whose distinct links fit in it.
        with patch("turnstone.core.web._RESOLVE_BUDGET_CHARS", 1_000):
            text = _strip('<a href="/x">x</a>' * 1_000)
        assert text.count("[x](https://example.com/x)") == 1_000
        assert "shows only its text" not in text
