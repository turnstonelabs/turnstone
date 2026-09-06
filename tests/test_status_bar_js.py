"""Behavioral harness for the shared status-bar formatter's pending-approval
chip (``turnstone/shared_static/status_bar.js``).

Both the interactive pane and the coordinator dashboard paint the chip
through ``StatusBar.paintApprovalChip`` and scroll to the card through
``StatusBar.scrollToApprovalTarget``; the wording, the hidden-at-zero rule,
and the reduced-motion handling live here once, so they are pinned here
once, under node with the shared fake DOM.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._js_harness_helpers import FAKE_DOM, node_skip, run_node_source

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parent.parent
_STATUS_BAR_JS = _ROOT / "turnstone/shared_static/status_bar.js"


def test_paint_approval_chip_counts_and_hides_at_zero() -> None:
    script = (
        FAKE_DOM
        + f"""
const {{ StatusBar }} = await import({json.dumps(_STATUS_BAR_JS.as_uri())});
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
const chip = new FakeElement("button");
chip.hidden = true;
html.appendChild(chip);
const els = {{ approvalEl: chip }};
const label = () => chip.children.map((c) => c.textContent).join("");

StatusBar.paintApprovalChip(els, 1);
assert(chip.hidden === false, "one live cycle must show the chip");
assert(label() === "⚠ 1 approval needed", "singular wording: " + label());
assert(chip.title === "Show the pending approval", "singular tooltip: " + chip.title);
assert(chip.children[0].getAttribute("aria-hidden") === "true", "glyph must be aria-hidden");

StatusBar.paintApprovalChip(els, 3);
assert(label() === "⚠ 3 approvals needed", "plural wording: " + label());
assert(chip.title === "Show the pending approvals", "plural tooltip: " + chip.title);
assert(chip.children.length === 2, "repaint must rewrite the label, not stack spans");

StatusBar.paintApprovalChip(els, 0);
assert(chip.hidden === true, "zero cycles must hide the chip");

// The generic painter takes the caller's words verbatim (the children
// heading chip says "2 pending", not "2 approvals needed").
StatusBar.paintWarnChip({{ chipEl: chip }}, 2, {{ label: "2 pending", title: "Show it" }});
assert(label() === "⚠ 2 pending", "generic label: " + label());
assert(chip.title === "Show it", "generic title: " + chip.title);

// Defensive inputs: a missing chip is a no-op, a negative count hides.
StatusBar.paintApprovalChip({{ approvalEl: null }}, 2);
StatusBar.paintApprovalChip(els, -1);
assert(chip.hidden === true, "negative count must hide the chip");
console.log("approval chip OK");
"""
    )
    proc = run_node_source(script)
    assert proc.returncode == 0, f"status bar harness failed:\n{proc.stderr}\n{proc.stdout}"


def test_hiding_a_focused_chip_hands_focus_to_the_composer() -> None:
    """A gate resolved from a peer tab (or a transcript rebuild) hides the
    chip while a keyboard user may be sitting on it.  Focus must move to the
    surface's composer input instead of dropping to the document body, and
    only when the chip actually held focus."""
    script = (
        FAKE_DOM
        + f"""
const {{ StatusBar }} = await import({json.dumps(_STATUS_BAR_JS.as_uri())});
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
const chip = new FakeElement("button");
chip.hidden = true;
html.appendChild(chip);
const input = new FakeElement("textarea");
html.appendChild(input);
let inputFocus = null;
input.focus = (opts) => {{ inputFocus = opts; document.activeElement = input; }};
const els = {{ approvalEl: chip, focusFallbackEl: input }};

// Focus elsewhere: hiding must not steal it.
StatusBar.paintApprovalChip(els, 1);
const elsewhere = new FakeElement("div");
html.appendChild(elsewhere);
elsewhere.focus();
StatusBar.paintApprovalChip(els, 0);
assert(document.activeElement === elsewhere, "hiding must not move focus from elsewhere");
assert(inputFocus === null, "composer must not be focused when the chip was not");

// Focus on the chip: hiding hands it to the composer without a scroll jump.
StatusBar.paintApprovalChip(els, 2);
chip.focus();
StatusBar.paintApprovalChip(els, 0);
assert(document.activeElement === input, "composer must take focus from a hidden chip");
assert(inputFocus && inputFocus.preventScroll === true, "fallback focus must not scroll");

// Already hidden: a repeat zero paint is a no-op even with focus on the chip.
inputFocus = null;
chip.focus();
StatusBar.paintApprovalChip(els, 0);
assert(inputFocus === null, "an already-hidden chip must not re-fire the fallback");
console.log("focus fallback OK");
"""
    )
    proc = run_node_source(script)
    assert proc.returncode == 0, f"status bar harness failed:\n{proc.stderr}\n{proc.stdout}"


def test_scroll_to_approval_target_honors_reduced_motion() -> None:
    script = (
        FAKE_DOM
        + f"""
const {{ StatusBar }} = await import({json.dumps(_STATUS_BAR_JS.as_uri())});
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
// A fake element without scrollIntoView (the harness default) is a no-op.
StatusBar.scrollToApprovalTarget(new FakeElement("div"));
StatusBar.scrollToApprovalTarget(null);

const calls = [];
const target = new FakeElement("div");
target.scrollIntoView = (opts) => calls.push(opts);
let reduce = false;
globalThis.window.matchMedia = (q) => ({{ matches: reduce && q.includes("reduced-motion") }});

StatusBar.scrollToApprovalTarget(target);
assert(calls.length === 1 && calls[0].behavior === "smooth", "default scroll must be smooth");
assert(calls[0].block === "center", "the card must be centred");

reduce = true;
StatusBar.scrollToApprovalTarget(target);
assert(calls[1].behavior === "auto", "reduced motion must drop the smooth scroll");

// A throwing matchMedia (some embedded views) must not block the scroll.
globalThis.window.matchMedia = () => {{ throw new Error("no media queries"); }};
StatusBar.scrollToApprovalTarget(target);
assert(calls.length === 3 && calls[2].behavior === "smooth", "matchMedia failure must fall back");
console.log("scroll target OK");
"""
    )
    proc = run_node_source(script)
    assert proc.returncode == 0, f"status bar harness failed:\n{proc.stderr}\n{proc.stdout}"
