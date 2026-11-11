"""Guards for the workstream pane's rules that every hosting page must get.

The interactive pane (``shared_static/interactive.js``) mounts on the server's
own page and on the console page, and each page serves a different file at
``/static/style.css``.  Rules for what the pane builds that sat in the server's
file left the console's pane different: your own messages dimmer, an info
line's line breaks folded away, no overlay while a file is dragged over the
pane, the composer's buttons still animating under reduced motion, and a chip
restyled on one page only.  ``tests/test_rendered_markdown_css.py`` guards the
same trap for rendered markdown.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._css_rules import class_names, css_declarations, css_selectors

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_CHAT_CSS = _SHARED / "chat.css"
_INTERACTIVE_CSS = _SHARED / "interactive.css"
_PER_SERVER_SHEETS = (
    _ROOT / "turnstone/ui/static/style.css",
    _ROOT / "turnstone/console/static/style.css",
)
# The shared modules that build message bubbles for the pane.
_BUBBLE_BUILDERS = ("interactive.js", "conversation.js", "composer_queue.js")


def _message_kinds() -> set[str]:
    """The ``msg <kind>`` bubbles the shared pane code builds, read from the
    class strings in its source so a kind added later is covered."""
    kinds: set[str] = set()
    for name in _BUBBLE_BUILDERS:
        src = (_SHARED / name).read_text(encoding="utf-8")
        kinds.update(m[1] for m in re.finditer(r"""["']msg ([a-z][\w-]*)""", src))
    return kinds


def _compounds(selector: str) -> list[str]:
    return [part for part in re.split(r"[\s>+~]+", selector) if part]


def test_message_kind_scan_finds_the_known_kinds() -> None:
    """The scan feeds the guard below; finding nothing would pass it vacuously."""
    assert {"user", "assistant", "info", "error", "reasoning"} <= _message_kinds()


@pytest.mark.parametrize("sheet", _PER_SERVER_SHEETS, ids=lambda p: p.parent.parent.name)
def test_per_server_sheet_does_not_style_a_bubble_the_pane_builds(sheet: Path) -> None:
    """A page gets only ONE of these files, and the pane's bubbles render on
    both pages."""
    kinds = _message_kinds()
    offenders = [
        selector
        for selector in css_selectors(sheet)
        for compound in _compounds(selector)
        if "msg" in class_names(compound) and kinds & set(class_names(compound))
    ]
    assert not offenders, (
        f"{sheet.relative_to(_ROOT)} styles a message bubble the shared pane builds; "
        f"move these to shared_static/chat.css: {offenders}"
    )


def test_user_and_info_bubbles_get_their_rules_from_the_shared_sheet() -> None:
    assert "--fg-bright" in css_declarations(_CHAT_CSS, ".msg.user"), (
        "your own messages read brighter than a reply, on every page"
    )
    assert re.search(r"white-space:\s*pre-wrap", css_declarations(_CHAT_CSS, ".msg.info")), (
        "an info line is written as plain text and keeps its line breaks"
    )


def test_drop_overlay_ships_with_the_positioned_pane_root_it_covers() -> None:
    """The overlay covers the pane with ``inset: 0``, so the pane root has to be
    positioned in the same sheet; apart, the overlay sizes to some ancestor."""
    overlay = css_declarations(_INTERACTIVE_CSS, ".pane.pane-drop-target::after")
    assert re.search(r"position:\s*absolute", overlay) and re.search(r"inset:\s*0", overlay)
    root = css_declarations(_INTERACTIVE_CSS, ".pane--embedded")
    assert re.search(r"position:\s*relative", root), "the overlay's containing block"
    for sheet in _PER_SERVER_SHEETS:
        assert "pane-drop-target" not in sheet.read_text(encoding="utf-8"), (
            f"{sheet.relative_to(_ROOT)}: the overlay belongs to the shared pane sheet"
        )


@pytest.mark.parametrize("sheet", _PER_SERVER_SHEETS, ids=lambda p: p.parent.parent.name)
def test_per_server_sheet_does_not_restyle_the_composer_chip(sheet: Path) -> None:
    """The chip's layout (thumbnail, name, size, remove button on one row; a
    text preview on the next) is tuned to the shared chrome.  A page that
    restyled the chip as a 280px pill wrapped the remove button onto its own
    row and clipped the preview.  A rule scoped to one page's own mount (an id)
    may still size a part of the chip for that mount."""
    offenders = [
        selector
        for selector in css_selectors(sheet)
        if "#" not in selector
        and any(name.startswith("composer-chip") for name in class_names(selector))
    ]
    assert not offenders, f"{sheet.relative_to(_ROOT)} restyles the composer chip: {offenders}"


def test_reduced_motion_block_stills_the_composer_buttons() -> None:
    """The shared sheet keeps its reduced-motion overrides in ONE block at the
    end of the file; the composer's buttons (mic, stop, chip remove) are in it."""
    css = _CHAT_CSS.read_text(encoding="utf-8")
    block = css[css.rindex("@media (prefers-reduced-motion: reduce)") :]
    assert css.rstrip().endswith(block.rstrip()), "the block is the last thing in the file"
    assert re.search(r"\.composer button\s*[,{]", block), block
    assert re.search(r"transition:\s*none", block)
