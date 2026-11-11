"""Guards for where the decoration of rendered markdown lives.

The server UI and the console each serve their own file at ``/static/style.css``
(``turnstone/ui/static`` and ``turnstone/console/static``), while the server UI,
the console and the coordinator all run the one shared ``renderer.js``.  A rule
that styles the renderer's output from either ``style.css`` reaches one surface
only: the syntax-highlighting theme once lived in the server's file and code
rendered unhighlighted on the console and coordinator, and later the table,
callout, heading and emphasis rules did the same.  Both times nothing failed;
the output just rendered bare.  These tests hold the two files to "none of it
lives here" and pin the rules to the sheet every page links.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._css_rules import class_names, css_rules, css_selectors

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_CHAT_CSS = _SHARED / "chat.css"
_RENDERER_JS = _SHARED / "renderer.js"
_PER_SERVER_SHEETS = (
    _ROOT / "turnstone/ui/static/style.css",
    _ROOT / "turnstone/console/static/style.css",
)
_PAGES = (
    _ROOT / "turnstone/console/static/index.html",
    _ROOT / "turnstone/console/static/coordinator/index.html",
    _ROOT / "turnstone/ui/static/index.html",
)

# The two ways a rule addresses an assistant bubble's rendered content.
_CONTAINERS = (".msg.assistant", ".msg-body")
# Class prefixes the vendored renderers stamp on their own output (syntax
# highlighting tokens, math, diagrams); renderer.js never spells these out.
_VENDOR_PREFIXES = ("hljs", "katex", "mermaid")


def _renderer_classes() -> tuple[set[str], set[str]]:
    """Class names renderer.js writes, as (whole names, prefixes).

    Read from the markup literals and the classList calls in the source, so a
    class the renderer gains later is covered without touching this file.  A
    literal that ends mid-name (``'callout-' + kind``) yields a prefix.
    """
    src = _RENDERER_JS.read_text(encoding="utf-8")
    tokens: set[str] = set()
    for m in re.finditer(r'class="([^"\']*)', src):
        tokens.update(m[1].split())
    for m in re.finditer(r'(?:className\s*=\s*|classList\.\w+\()((?:\s*"[^"]*",?)+)', src):
        for literal in re.findall(r'"([^"]*)"', m[1]):
            tokens.update(literal.split())
    return (
        {t for t in tokens if not t.endswith("-")},
        {t for t in tokens if t.endswith("-")} | set(_VENDOR_PREFIXES),
    )


def test_renderer_class_scan_finds_the_known_output() -> None:
    """The scan feeds the guard below; if a renderer refactor stops it finding
    anything, that guard would pass on nothing."""
    names, prefixes = _renderer_classes()
    assert {
        "table-wrap",
        "callout",
        "callout-title",
        "img-placeholder",
        "footnotes",
        "fn-ref",
        "mermaid-container",
        "code-terminal",
    } <= names
    assert {"callout-", "align-"} <= prefixes


@pytest.mark.parametrize("sheet", _PER_SERVER_SHEETS, ids=lambda p: p.parent.parent.name)
def test_per_server_sheet_does_not_style_rendered_markdown(sheet: Path) -> None:
    """Each page gets only ONE of these files, so anything here that styles the
    shared renderer's output styles it on that page alone."""
    names, prefixes = _renderer_classes()
    offenders = []
    for selector in css_selectors(sheet):
        classes = class_names(selector)
        if any(container in selector for container in _CONTAINERS) or any(
            c in names or c.startswith(tuple(prefixes)) for c in classes
        ):
            offenders.append(selector)
    assert not offenders, (
        f"{sheet.relative_to(_ROOT)} styles rendered markdown, which only one surface "
        f"would get; move these to shared_static/chat.css: {offenders}"
    )


@pytest.mark.parametrize(
    "selector",
    [
        ".msg.assistant h1",
        ".msg.assistant em",
        ".msg.assistant table",
        ".msg.assistant th",
        ".msg.assistant td",
        ".msg.assistant .table-wrap",
        ".msg.assistant .callout",
        ".msg.assistant .callout-warning",
        ".msg.assistant .callout-body",
        ".msg.assistant details",
        ".msg.assistant kbd",
        ".msg.assistant mark",
        ".msg.assistant dl",
        ".msg.assistant hr",
        ".msg.assistant .footnotes",
        ".msg.assistant .img-placeholder",
        ".hljs-keyword",
        ".msg.assistant .katex-display",
        ".msg.assistant .mermaid-container",
    ],
)
def test_shared_chat_sheet_carries_the_decoration(selector: str) -> None:
    """The constructs that rendered bare while their rules sat in one server's
    ``style.css``.  Not a list of everything the sheet styles."""
    assert selector in css_selectors(_CHAT_CSS), f"chat.css lost the rule for {selector}"


def test_every_page_links_the_shared_chat_sheet() -> None:
    for page in _PAGES:
        html = page.read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="/shared/chat.css" />' in html, (
            f"{page.relative_to(_ROOT)} must link chat.css: it renders markdown"
        )


def test_only_the_focus_ring_and_the_copy_flash_outline_a_copyable_block() -> None:
    """Enter on a focused fence, table or diagram copies it and flashes the
    outcome as an outline on the block (``.is-copied`` / ``.is-copy-failed``),
    over the focus ring it already wears.  Any other rule that draws an outline
    on those blocks competes with the flash: a ring scoped to ``.msg.assistant``
    once out-ranked both, so a copied table kept its focus colour and never
    flashed.  Where the outline sits (``outline-offset``) is not part of the
    ``outline`` shorthand and competes with nothing, so a rule may move it.
    """
    subject = re.compile(r"(?:^|[\s>])pre\b|\.table-wrap|\.mermaid-container")
    found: dict[str, list[str]] = {}
    for sheet in sorted(_SHARED.glob("*.css")) + list(_PER_SERVER_SHEETS):
        for selectors, declarations in css_rules(sheet.read_text(encoding="utf-8")):
            if not re.search(r"(?:^|;)\s*outline(?:-(?:color|style|width))?\s*:", declarations):
                continue
            for selector in selectors:
                if subject.search(selector):
                    found.setdefault(sheet.name, []).append(selector)
    blocks = (".msg-body pre", ".table-wrap", ".mermaid-container")
    states = (":focus-visible", ".is-copied", ".is-copy-failed")
    assert found == {"chat.css": [block + state for state in states for block in blocks]}, found


def test_outlines_in_a_clipping_details_box_are_drawn_inside() -> None:
    """The ``<details>`` box clips at its edge, and its summary, and a table,
    fence or diagram placed straight into it, reach that edge.  An outline
    drawn outside any of them is cut off there: a focused summary showed no
    ring at all while closed.  So each draws its outline inside itself."""
    rules = css_rules(_CHAT_CSS.read_text(encoding="utf-8"))
    box = "".join(decl for selectors, decl in rules if ".msg.assistant details" in selectors)
    assert re.search(r"overflow:\s*hidden", box), (
        "the details box no longer clips; the inset outlines below may be dropped with it"
    )
    inset = {
        selector
        for selectors, declarations in rules
        if re.search(r"outline-offset:\s*-\d", declarations)
        for selector in selectors
    }
    assert {
        ".msg.assistant details summary",
        ".msg.assistant details > pre",
        ".msg.assistant details > .mermaid-container",
        ".msg.assistant .table-wrap",
    } <= inset, inset
