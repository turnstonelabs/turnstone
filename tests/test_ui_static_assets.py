"""Guards for where the web UI pages get their static assets (#1229).

The node UI, console and coordinator pages reference every resource in their
markup and stylesheets on their own origin.  A resource from another host
exposes each viewer's address and the deployment's origin to that host, and a
render-blocking one leaves the page blank for as long as the host takes to
fail.  The typefaces named by the design tokens in base.css are therefore
vendored under turnstone/shared_static/ and declared in fonts.css there.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
# The node UI, console and coordinator pages: every HTML file under a static
# tree.  shared_static holds only assets, and the vendored libraries in it are
# not pages.
_PAGES = tuple(sorted((_ROOT / "turnstone").glob("**/static/**/*.html")))
_FONT_CSS = _SHARED / "fonts.css"
# The vendored font directories, each with a digest of its woff2 names and bytes.
# The directories are cached as immutable, so changed font files must ship under
# a new directory name; record a new digest only with that rename.
_FONT_DIR_DIGESTS = {
    "inter-4.001.1": "81d8ebce9d72a335e3c85a73112d0d0a1100d47e5d179d04a01bc5cc396b1001",
    "jetbrains-mono-2.304": "0610110bc72e079bdb1d93e4bb6618f5e2da8ed40f8e296622fefff591764c26",
    "dejavu-sans-2.37": "2bf4c5921d35072ff9029f5e8c20c5463c7378359a4ac3dbcf6cf7e9549f4612",
}
# The attribute through which each tag fetches a resource as the page loads.
_RESOURCE_ATTRS = {
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "img": "src",
    "link": "href",
    "object": "data",
    "script": "src",
    "source": "src",
    "track": "src",
    "video": "src",
}
# CSS comments, dropped before scanning so prose that mentions url() is ignored.
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# Where CSS names a resource: url( or @import, then an optional url( and quote.
_CSS_REF = re.compile(r"""(?:url\(|@import)\s*(?:url\(\s*)?["']?""", re.IGNORECASE)
# How a local CSS reference starts: a data: URI, a fragment, a root-relative
# path or a relative path.  Anything else (a scheme, "//", a backslash, an
# escape) fails closed, since browsers resolve such spellings in more ways than
# a pattern can list.
_CSS_LOCAL_TARGET = re.compile(r"""data:|#|/[\w.-]|[\w.-]+[/)"'\s]""")
# The same rule for tag attributes, where the pages use only root-relative
# paths and data: URLs.
_LOCAL_URL = re.compile(r"/[\w.-]|data:")


class _ResourceCollector(HTMLParser):
    """Collects resource URLs, and the inline CSS of style attributes and
    <style> blocks."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.css: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        wanted = _RESOURCE_ATTRS.get(tag)
        for name, value in attrs:
            if name == wanted and value:
                self.urls.append(value)
            if name == "style" and value:
                self.css.append(value)
        if tag == "style":
            self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.css.append(data)


def _collect(page: Path) -> _ResourceCollector:
    collector = _ResourceCollector()
    collector.feed(page.read_text(encoding="utf-8"))
    collector.close()
    return collector


def _is_offsite(url: str) -> bool:
    return not _LOCAL_URL.match(url)


def _offsite_css_refs(css: str) -> list[str]:
    """The start of each url() or @import target in *css* that is not local."""
    css = _CSS_COMMENT.sub("", css)
    return [
        css[match.end() : match.end() + 40]
        for match in _CSS_REF.finditer(css)
        if not _CSS_LOCAL_TARGET.match(css, match.end())
    ]


def test_page_discovery_finds_every_known_page() -> None:
    """An empty glob would turn the page guards below into silent skips."""
    found = {page.relative_to(_ROOT).as_posix() for page in _PAGES}
    assert {
        "turnstone/ui/static/index.html",
        "turnstone/console/static/index.html",
        "turnstone/console/static/coordinator/index.html",
    } <= found


@pytest.mark.parametrize(
    ("url", "offsite"),
    [
        ("/shared/base.css", False),
        ("data:image/svg+xml,%3Csvg%3E", False),
        ("https://cdn.example.com/x.css", True),
        ("//cdn.example.com/x.css", True),
        ("///cdn.example.com/x.css", True),
        ("/" + chr(92) + "cdn.example.com/x.css", True),
        ("/" + chr(9) + "//cdn.example.com/x.css", True),
        (" https://cdn.example.com/x.css", True),
        ("static/app.js", True),
    ],
)
def test_offsite_classification(url: str, offsite: bool) -> None:
    assert _is_offsite(url) is offsite


@pytest.mark.parametrize(
    ("css", "offsite"),
    [
        ("a { background: url(https://cdn.example.com/x.png) }", True),
        ("a { background: url( '//cdn.example.com/x.png') }", True),
        ("a { background: URL(HTTPS://CDN.EXAMPLE.COM/X.PNG) }", True),
        ("a { background: url(///cdn.example.com/x.png) }", True),
        ("a { background: url(/" + chr(92) + "cdn.example.com/x.png) }", True),
        ('@import "https://cdn.example.com/x.css";', True),
        ("@import'//cdn.example.com/x.css';", True),
        ("@import url(//cdn.example.com/x.css);", True),
        ('a { src: url("/shared/inter-4.001.1/inter-normal.woff2") }', False),
        ("a { src: url(fonts/KaTeX_Main-Regular.woff2) }", False),
        ("a { fill: url(#gradient) }", False),
        (
            'a { background: url("data:image/svg+xml,%3Csvg xmlns=%27http://www.example.com/s%27/%3E") }',
            False,
        ),
        ("/* url(https://cdn.example.com/x.png) is only prose here */", False),
    ],
)
def test_css_reference_classification(css: str, offsite: bool) -> None:
    assert bool(_offsite_css_refs(css)) is offsite


@pytest.mark.parametrize("page", _PAGES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_page_loads_resources_only_from_its_own_origin(page: Path) -> None:
    """Covers resource-loading tags and the page's inline CSS."""
    collected = _collect(page)
    assert collected.urls, "resource collector found nothing -- parser rotted?"
    offsite = [url for url in collected.urls if _is_offsite(url)]
    assert not offsite, f"{page.name} loads from another origin: {offsite}"
    inline = [ref for css in collected.css for ref in _offsite_css_refs(css)]
    assert not inline, f"{page.name} inline CSS loads from another origin: {inline}"


def test_inline_css_is_read_from_style_contexts_only() -> None:
    """Style attributes and <style> blocks are scanned as CSS; markup and script
    text are not, so a glob in prose or a script call cannot read as CSS."""
    collector = _ResourceCollector()
    collector.feed(
        '<div style="background: url(//cdn.example.com/a.png)"></div>'
        "<style>b { background: url(https://cdn.example.com/b.png) }</style>"
        '<input accept="text/*"><p>match **/*.py</p><script>new URL(x, y)</script>'
    )
    collector.close()
    refs = [ref for css in collector.css for ref in _offsite_css_refs(css)]
    assert len(refs) == 2, refs


def test_stylesheets_load_resources_only_from_their_own_origin() -> None:
    """A url() or @import target that is not local fetches from another host on
    every page that links the sheet, vendored sheets included."""
    sheets = sorted((_ROOT / "turnstone").rglob("*.css"))
    assert sheets
    offsite = [
        str(sheet.relative_to(_ROOT))
        for sheet in sheets
        if _offsite_css_refs(sheet.read_text(encoding="utf-8"))
    ]
    assert not offsite, f"stylesheets fetching from another origin: {offsite}"


@pytest.mark.parametrize("page", _PAGES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_page_links_the_font_stylesheet(page: Path) -> None:
    """Without this link the design-token typefaces silently fall back to
    system fonts."""
    assert "/shared/fonts.css" in _collect(page).urls, f"{page.name} must link fonts.css"


def test_font_stylesheet_references_exactly_the_shipped_fonts() -> None:
    """Each url() names a vendored font file once, by root-absolute path, and
    every font file in the vendored directories is referenced, so neither a
    typo nor a stray file ships.  Root-absolute paths let a node page proxied
    through the console share the console's cached copy."""
    referenced = re.findall(r'url\("([^"]+)"\)', _FONT_CSS.read_text(encoding="utf-8"))
    assert referenced, "no url() found -- pattern rotted?"
    shipped = sorted(
        f"/shared/{directory}/{font.name}"
        for directory in _FONT_DIR_DIGESTS
        for font in (_SHARED / directory).glob("*.woff2")
    )
    assert sorted(referenced) == shipped
    font_dirs = {font.parent.name for font in _SHARED.glob("*/*.woff2")}
    assert font_dirs == set(_FONT_DIR_DIGESTS), f"font directories on disk: {sorted(font_dirs)}"
    for directory in _FONT_DIR_DIGESTS:
        assert (_SHARED / directory / "LICENSE").is_file()


def test_font_directories_keep_their_recorded_files() -> None:
    """A file replaced in place keeps its immutable URL, so returning browsers
    would go on using the old copy for a year."""
    for directory, expected in _FONT_DIR_DIGESTS.items():
        digest = hashlib.sha256()
        for font in sorted((_SHARED / directory).glob("*.woff2")):
            digest.update(font.name.encode())
            digest.update(font.read_bytes())
        assert digest.hexdigest() == expected, (
            f"{directory} changed; changed font files must ship under a new directory name"
        )


def test_font_families_match_the_design_tokens() -> None:
    """The vendored families are the first choices of --font-ui and --font-mono;
    renaming either side would drop the pages to the fallback fonts."""
    base = (_SHARED / "base.css").read_text(encoding="utf-8")
    tokens = set(re.findall(r'--font-(?:ui|mono):\s*"([^"]+)"', base))
    assert len(tokens) == 2, f"expected the --font-ui and --font-mono tokens, got {tokens}"
    css = _FONT_CSS.read_text(encoding="utf-8")
    assert set(re.findall(r'font-family:\s*"([^"]+)"', css)) == tokens | {"Turnstone Symbols"}
    for token in ["ui", "mono"]:
        stack = re.search(r"--font-" + token + r":\s*([^;]+);", base)
        assert stack and '"Turnstone Symbols"' in stack[1]
