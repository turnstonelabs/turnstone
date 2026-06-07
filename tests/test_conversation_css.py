"""Guards for the shared conversational-pane card sheet
(``turnstone/shared_static/conversation.css``).

Born in step 5e.2a: the ONE neutral ``.conv-*`` approval-card vocabulary both
panes emit, converging the forked ``.coord-tool-*`` (coordinator.css) and
``.ts-approval-*`` / ``.verdict-*`` (chat.css + interactive.css) cards.  These
pin the load-bearing invariants — the DS button rule (approve == --ok, never
--warn), the core selector set, a self-contained spinner keyframe, and the
three-page link wiring — so a regression fails loudly here.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CSS = _ROOT / "turnstone/shared_static/conversation.css"
_PAGES = (
    _ROOT / "turnstone/console/static/index.html",
    _ROOT / "turnstone/console/static/coordinator/index.html",
    _ROOT / "turnstone/ui/static/index.html",
)


def _css() -> str:
    return _CSS.read_text(encoding="utf-8")


def test_core_selectors_present() -> None:
    """The card's structural vocabulary — drop one and the matching builder's
    output goes unstyled in both panes."""
    body = _css()
    for sel in (
        ".conv-batch",
        ".conv-batch-head",
        ".conv-row",
        ".conv-row-call",
        ".conv-verdict",
        ".conv-verdict-detail",
        ".conv-warning",
        ".conv-actions",
        ".conv-btn",
        ".conv-status",
    ):
        assert sel + " " in body or sel + "," in body or sel + "{" in body, (
            f"conversation.css missing {sel}"
        )


def test_approve_uses_ok_not_warn() -> None:
    """Load-bearing DS hard-rule (base.css:84): the Approve button is GREEN
    (--ok), never amber (--warn).  Pin the whole button trio's semantics:
    Approve = --ok fill, Approve all = dashed --ok ghost, Deny = --err."""
    body = _css()
    approve = _rule_body(body, ".conv-btn--approve")
    assert "--ok" in approve, "Approve button must use --ok"
    assert "--warn" not in approve, "Approve button must NOT use --warn (DS rule)"

    always = _rule_body(body, ".conv-btn--always")
    assert "dashed" in always, "Approve all must be a dashed ghost"
    assert "--ok" in always, "Approve all must use --ok (it is an approve action)"

    deny = _rule_body(body, ".conv-btn--deny")
    assert "--err" in deny, "Deny button must use --err"


def test_state_stripe_vocabulary() -> None:
    """The batch state left-stripe — the primary non-text WCAG 1.4.1 cue."""
    body = _css()
    assert "--warn" in _rule_body(body, ".conv-batch--pending")
    assert "--ok" in _rule_body(body, ".conv-batch--approved")
    assert "--err" in (
        _rule_body(body, ".conv-batch--denied") + _rule_body(body, ".conv-batch--error")
    )


def test_spinner_keyframe_is_self_contained() -> None:
    """The verdict spinner must NOT depend on coord-chrome.css's ``ts-spin``
    keyframe — that sheet isn't loaded by the standalone interactive pane.  The
    sheet defines + uses its own namespaced ``conv-spin``."""
    body = _css()
    assert "@keyframes conv-spin" in body
    assert "animation: conv-spin" in body
    # The comment may NAME ts-spin to explain the namespacing; what must not
    # appear is an actual dependency on it (a reference or a redefinition).
    assert "animation: ts-spin" not in body
    assert "@keyframes ts-spin" not in body


def test_linked_by_console_and_both_standalone_pages() -> None:
    """Loaded everywhere a ``.conv-*`` emitter renders: the console (hosts both
    panes), the standalone coordinator page, and the standalone interactive page
    (ui/static, driven by the same interactive.js)."""
    for page in _PAGES:
        html = page.read_text(encoding="utf-8")
        assert "/shared/conversation.css" in html, f"{page.name} must link conversation.css"


def _rule_body(css: str, selector: str) -> str:
    """Return the declaration block for a selector (first match).

    Tolerates a grouped selector list (``.conv-batch--denied,\\n.conv-batch--error
    {...}``): the optional ``,...`` clause lets the queried selector sit anywhere
    in the list.  A descendant rule (``.conv-batch--denied .conv-row {...}``) is
    skipped — a space (not a comma) before the next token fails both the optional
    group and the bare ``{``, so ``search`` advances to the real rule.
    """
    pattern = re.compile(
        re.escape(selector) + r"(?:\s*,\s*[^{]+)?\s*\{([^}]*)\}",
    )
    m = pattern.search(css)
    assert m, f"selector {selector} not found as a rule"
    return m.group(1)
