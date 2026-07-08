"""Tests for turnstone.core.web_helpers — version_html() cache-busting."""

from __future__ import annotations


class TestVersionHtml:
    def test_app_css_gets_version(self):
        from turnstone.core.web_helpers import version_html

        html = '<link rel="stylesheet" href="/shared/base.css">'
        result = version_html(html)
        assert "?v=" in result
        assert "/shared/base.css?v=" in result

    def test_app_js_gets_version(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js"></script>'
        result = version_html(html)
        assert "/static/app.js?v=" in result

    def test_shared_js_gets_version(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/utils.js"></script>'
        result = version_html(html)
        assert "/shared/utils.js?v=" in result

    def test_vendored_katex_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<link rel="stylesheet" href="/shared/katex-0.17.0/katex.min.css">'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hljs_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hljs-11.11.1/highlight.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_mermaid_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/mermaid-11.16.0/mermaid.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hls_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hls-1.6.16/hls.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_external_urls_not_modified(self):
        from turnstone.core.web_helpers import version_html

        html = (
            '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono" rel="stylesheet">'
        )
        result = version_html(html)
        assert result == html  # unchanged

    def test_docs_link_not_modified(self):
        from turnstone.core.web_helpers import version_html

        html = '<a href="/docs#/System:%20Settings" target="_blank">docs</a>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_multiple_tags(self):
        from turnstone import __version__
        from turnstone.core.web_helpers import version_html

        html = (
            '<link rel="stylesheet" href="/shared/base.css">\n'
            '<link rel="stylesheet" href="/shared/katex-0.17.0/katex.min.css">\n'
            '<link rel="stylesheet" href="/static/style.css">\n'
            '<script src="/shared/utils.js"></script>\n'
            '<script src="/shared/hljs-11.11.1/highlight.min.js"></script>\n'
            '<script src="/static/app.js"></script>'
        )
        result = version_html(html)
        assert f'/shared/base.css?v={__version__}"' in result
        assert f'/static/style.css?v={__version__}"' in result
        assert f'/shared/utils.js?v={__version__}"' in result
        assert f'/static/app.js?v={__version__}"' in result
        # Vendored libs unchanged
        assert '/shared/katex-0.17.0/katex.min.css"' in result
        assert '/shared/hljs-11.11.1/highlight.min.js"' in result

    def test_version_matches_package(self):
        from turnstone import __version__
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js"></script>'
        result = version_html(html)
        assert f"?v={__version__}" in result

    def test_double_apply_is_idempotent(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js"></script>'
        once = version_html(html)
        twice = version_html(once)
        assert once == twice
        assert twice.count("?v=") == 1

    def test_existing_query_string_preserved(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js?foo=bar"></script>'
        result = version_html(html)
        assert result == html  # unchanged — already has query string


class TestLatin1SafeFilename:
    """Content-Disposition filename sanitizer — must yield a value that is
    both latin-1 encodable (Starlette) and control-char free (h11)."""

    def _assert_wire_safe(self, out: str) -> None:
        # Independent oracle — deliberately does NOT reuse the impl's
        # isprintable() gate (that would pass by construction). Every char
        # must be printable ASCII (0x20..0x7e) and neither quoted-string
        # metacharacter, so the value is latin-1 clean, control-free, and
        # safely quotable.
        assert all(0x20 <= ord(c) <= 0x7E and c not in '"\\' for c in out)

    def test_plain_ascii_unchanged(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        assert latin1_safe_filename("report_2026.md") == "report_2026.md"

    def test_non_latin1_folds_to_question_marks(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # CJK + em dash (U+2014) are printable but non-latin-1 → fold to '?'.
        out = latin1_safe_filename("文書 — v1.md")
        assert out == "?? ? v1.md"
        self._assert_wire_safe(out)

    def test_latin1_but_control_chars_are_stripped(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # All latin-1 encodable, so the old strip/fold left them in the header
        # and the HTTP server layer then 500'd (h11 rejects NUL/CR/LF/FF/VT;
        # httptools is stricter). NUL / form-feed / DEL / TAB / VT / C1-NEL
        # (0x85) must all be dropped, not merely folded.
        out = latin1_safe_filename("a\x00b\x0cc\x7fd\te\x0bf\x85g.md")
        assert out == "abcdefg.md"
        self._assert_wire_safe(out)

    def test_crlf_and_quote_stripped(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        out = latin1_safe_filename('a"\r\nX-Evil: 1.md')
        assert "\r" not in out and "\n" not in out and '"' not in out
        self._assert_wire_safe(out)

    def test_backslash_stripped(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # Backslash is the RFC 6266 quoted-pair escape inside filename="..." —
        # a trailing '\' would escape the closing quote, and '\x' mid-name
        # becomes a spurious escape. Both must be dropped (Windows-origin
        # uploads legitimately carry '\').
        assert latin1_safe_filename("dir\\file.md") == "dirfile.md"
        assert latin1_safe_filename("trailing\\") == "trailing"
        self._assert_wire_safe(latin1_safe_filename("a\\b\\c"))

    def test_empty_after_sanitizing_uses_fallback(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # A name of only quotes / controls sanitizes to empty → fallback,
        # never ``filename=""``.
        assert latin1_safe_filename('"""') == "attachment"
        assert latin1_safe_filename("\x00\x0c\x7f") == "attachment"
        assert latin1_safe_filename("", fallback="preview") == "preview"

    def test_all_non_latin1_stays_non_empty_no_fallback(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # An all-CJK name folds to '???' (truthy) — must NOT hit the fallback.
        assert latin1_safe_filename("日本語", fallback="preview") == "???"

    def test_fallback_is_also_sanitized(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # The fallback fires only when the name sanitizes to empty, and it is
        # cleaned by the SAME rules — a caller can't reintroduce the crash /
        # corruption through an unsafe fallback.
        assert latin1_safe_filename("", fallback="—\x00.txt") == "?.txt"
        self._assert_wire_safe(latin1_safe_filename("", fallback="bad\\\x00name"))
        # If even the fallback sanitizes to empty, a safe constant backs it —
        # never filename="".
        assert latin1_safe_filename("", fallback='"\x00') == "download"
