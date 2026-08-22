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

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CSS = _ROOT / "turnstone/shared_static/conversation.css"
_CHAT_CSS = _ROOT / "turnstone/shared_static/chat.css"
_INTERACTIVE_CSS = _ROOT / "turnstone/shared_static/interactive.css"
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


def test_pane_messages_pins_children_flex_shrink() -> None:
    """Regression guard: tool cards collapsing to a ~2px empty stripe.  The
    interactive message list is a SCROLLING flex column, and .conv-batch sets
    ``overflow:hidden`` — whose flex ``min-height:auto`` resolves to 0, so
    without an explicit ``flex-shrink:0`` the tool batch gets squished to just
    its (left-)border once the column fills.  Plain .msg blocks (overflow
    visible) are immune, which is why the bug looked interactive-only.  Don't
    drop the pin."""
    css = _INTERACTIVE_CSS.read_text(encoding="utf-8")
    sel = ".pane--embedded .pane-messages > *"
    assert sel in css, f"interactive.css must pin {sel} so cards don't collapse"
    block = css[css.index(sel) : css.index(sel) + 120]
    assert "flex-shrink: 0" in block, f"{sel} must set flex-shrink: 0"


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


_COMPACT_FAIL_OPEN_TOKENS = (
    ".conv-batch--pending",
    ".conv-batch--running",
    ".conv-batch--denied",
    ".conv-batch--error",
    '[aria-busy="true"]',
    ".conv-actions",
    ".conv-verdict-spinner",
    ".conv-warning",
    ".conv-row.error",
    ".conv-row-result--error",
    ".conv-row-status--error",
    ".conv-status--error",
    '.conv-row[data-tool-name="task_agent"]',
    '.conv-agent[data-state="running"]',
    '[data-agent-step-exceptional="true"]',
    ".compaction-running",
    ".conv-agent-compaction-notice",
    '[data-output-review-incomplete="true"]',
    ".conv-verdict--high",
    ".conv-verdict--critical",
    ".conv-verdict-rec--deny",
    ".conv-verdict-rec--review",
    '[data-effect-status="committed"]',
)


def _compact_section(css: str) -> str:
    start = css.index("/* Compact transcript presentation.")
    end = css.index("/* Row container.", start)
    return css[start:end]


def _assert_compact_fail_open_contract(section: str) -> None:
    assert ':root[data-transcript-presentation="compact"]' in section
    assert "[data-transcript-root]" in section
    assert '[data-results-settled="true"]' in section
    assert '[data-compact-folded="true"]' in section
    assert "> :not(.conv-batch-head)" in section
    for token in _COMPACT_FAIL_OPEN_TOKENS:
        assert section.count(token) >= 2, f"compact selectors lost mirrored fail-open term {token}"


def test_compact_batch_fold_is_explicit_scoped_and_fail_open() -> None:
    _assert_compact_fail_open_contract(_compact_section(_css()))


@pytest.mark.parametrize("token", _COMPACT_FAIL_OPEN_TOKENS)
def test_compact_contract_guard_detects_each_missing_exclusion(token: str) -> None:
    section = _compact_section(_css()).replace(token, "")
    with pytest.raises(AssertionError):
        _assert_compact_fail_open_contract(section)


def test_compact_message_density_cannot_match_shell_status_messages() -> None:
    css = re.sub(r"\s+", " ", _CHAT_CSS.read_text(encoding="utf-8"))
    prefix = ':root[data-transcript-presentation="compact"] [data-transcript-root]'
    assert prefix + " .msg {" in css
    assert prefix + " .msg.reasoning {" in css
    assert prefix + ' .msg.reasoning[data-reasoning-active="true"] {' in css
    assert prefix + ' .msg.reasoning[data-reasoning-active="true"] > .msg-body {' in css
    assert (
        prefix + ' .msg.reasoning[data-reasoning-active="true"] > .reasoning-activity-status {'
        in css
    )
    assert prefix + ' .msg.reasoning[data-reasoning-active="true"] > * {' not in css
    assert ".reasoning-activity-status {" in css
    assert "@keyframes transcript-reasoning-spin" in css
    assert "prefers-reduced-motion: reduce" in css
    assert ':root[data-transcript-presentation="compact"] .msg {' not in css


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
