"""Tests for turnstone.core.web_helpers — version_html() cache-busting."""

from __future__ import annotations

import os

import pytest


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

        html = '<link rel="stylesheet" href="/shared/katex-0.18.5/katex.min.css">'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hljs_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hljs-11.12.0/highlight.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_mermaid_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/mermaid-11.17.2/mermaid.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hls_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hls-1.7.2/hls.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendor_prefix_lookalike_is_still_versioned(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hls-2-player.js"></script>'
        result = version_html(html)
        assert "/shared/hls-2-player.js?v=" in result

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
            '<link rel="stylesheet" href="/shared/katex-0.18.5/katex.min.css">\n'
            '<link rel="stylesheet" href="/static/style.css">\n'
            '<script src="/shared/utils.js"></script>\n'
            '<script src="/shared/hljs-11.12.0/highlight.min.js"></script>\n'
            '<script src="/static/app.js"></script>'
        )
        result = version_html(html)
        assert f'/shared/base.css?v={__version__}"' in result
        assert f'/static/style.css?v={__version__}"' in result
        assert f'/shared/utils.js?v={__version__}"' in result
        assert f'/static/app.js?v={__version__}"' in result
        # Vendored libs unchanged
        assert '/shared/katex-0.18.5/katex.min.css"' in result
        assert '/shared/hljs-11.12.0/highlight.min.js"' in result

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


class TestRevalidatingStaticFiles:
    def test_same_versioned_url_revalidates_after_asset_changes(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone import __version__
        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        old_body = b"export const generation = 'old';"
        new_body = b"export const generation = 'new';"
        assert len(old_body) == len(new_body)
        asset.write_bytes(old_body)
        original_stat = asset.stat()
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            url = f"/static/app.js?v={__version__}"
            first = client.get(url)
            assert first.status_code == 200
            assert first.headers["cache-control"] == "no-cache"
            assert "last-modified" not in first.headers
            old_etag = first.headers["etag"]

            asset.write_bytes(new_body)
            os.utime(
                asset,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            changed_stat = asset.stat()
            assert changed_stat.st_size == original_stat.st_size
            assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns

            changed = client.get(url, headers={"If-None-Match": old_etag})
            assert changed.status_code == 200
            assert changed.content == new_body
            assert changed.headers["etag"] != old_etag
            assert changed.headers["cache-control"] == "no-cache"
            assert "last-modified" not in changed.headers

            stale_date_only = client.get(
                url,
                headers={"If-Modified-Since": "Wed, 31 Dec 9999 23:59:59 GMT"},
            )
            assert stale_date_only.status_code == 200
            assert stale_date_only.content == new_body

            unchanged = client.get(url, headers={"If-None-Match": changed.headers["etag"]})
            assert unchanged.status_code == 304
            assert unchanged.headers["cache-control"] == "no-cache"

    def test_version_named_vendor_asset_is_immutable(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone.core.web_helpers import RevalidatingStaticFiles

        vendor_dir = tmp_path / "katex-0.18.5"
        vendor_dir.mkdir()
        (vendor_dir / "katex.min.css").write_text(".katex {}", encoding="utf-8")
        app = Starlette(
            routes=[Mount("/shared", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            resp = client.get("/shared/katex-0.18.5/katex.min.css")

        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert resp.headers["etag"]

    def test_missing_asset_is_not_cached(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone.core.web_helpers import RevalidatingStaticFiles

        app = Starlette(
            routes=[Mount("/shared", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            resp = client.get("/shared/not-deployed-yet.js")

        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"


class TestStaticAssetCacheControl:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("interactive.js", "no-cache"),
            ("hls-2-player.js", "no-cache"),
            ("katex-0.18.5/katex.min.css", "public, max-age=31536000, immutable"),
            ("hljs-11.12.0/highlight.min.js", "public, max-age=31536000, immutable"),
            ("katex-0.18.5/../private.json", "no-store"),
            (r"katex-0.18.5\..\private.json", "no-store"),
            ("nested//asset.js", "no-store"),
        ],
    )
    def test_policy_requires_a_canonical_exact_vendor_path(self, path, expected):
        from turnstone.core.web_helpers import static_asset_cache_control

        assert static_asset_cache_control(path) == expected

    def test_every_packaged_versioned_vendor_directory_uses_the_shared_policy(self):
        import re
        from pathlib import Path

        import turnstone
        from turnstone.core.web_helpers import static_asset_cache_control, version_html

        shared_dir = Path(turnstone.__file__).resolve().parent / "shared_static"
        versioned_dir = re.compile(r"^[a-z][a-z0-9_-]*-\d+(?:\.\d+)+$")
        vendor_dirs = sorted(
            path.name
            for path in shared_dir.iterdir()
            if path.is_dir() and versioned_dir.fullmatch(path.name)
        )
        assert vendor_dirs

        for directory in vendor_dirs:
            asset_path = f"{directory}/asset.js"
            assert static_asset_cache_control(asset_path) == "public, max-age=31536000, immutable"
            html = f'<script src="/shared/{asset_path}"></script>'
            assert version_html(html) == html


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
