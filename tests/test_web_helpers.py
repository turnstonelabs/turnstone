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

        html = '<link rel="stylesheet" href="/shared/katex-0.16.44/katex.min.css">'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hljs_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hljs-11.11.1/highlight.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_mermaid_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/mermaid-11.14.0/mermaid.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hls_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hls-1.6.15/hls.min.js"></script>'
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
            '<link rel="stylesheet" href="/shared/katex-0.16.44/katex.min.css">\n'
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
        assert '/shared/katex-0.16.44/katex.min.css"' in result
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
