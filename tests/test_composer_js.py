"""Behavioral guards for the shared composer's auto-resize (composer.js)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests._js_harness_helpers import FAKE_DOM, node_skip, run_node_source

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_COMPOSER = _SHARED / "composer.js"
_CHAT_CSS = _SHARED / "chat.css"


def _harness(native: bool) -> str:
    """The fake DOM plus composer.js imported under a CSS.supports stub —
    NATIVE_FIELD_SIZING is read once at module load, so each engine flavor
    needs its own node process."""
    return (
        FAKE_DOM
        + f"globalThis.CSS = {json.dumps(native)}\n"
        + '  ? { supports: (p, v) => p === "field-sizing" && v === "content" }\n'
        + "  : undefined;\n"
        + """
window.matchMedia = () => ({ matches: false });
Object.defineProperty(FakeElement.prototype, "style", {
  get() { return this._style || (this._style = {}); },
  configurable: true,
});
const assert = (ok, message) => { if (!ok) throw new Error(message); };
function composer(opts) {
  const c = new Composer(document.createElement("div"), { onSend() {}, ...opts });
  const probe = { reads: 0, height: 120 };
  Object.defineProperty(c.inputEl, "scrollHeight", {
    get() { probe.reads += 1; return probe.height; },
  });
  return { c, ta: c.inputEl, probe };
}
"""
        + f"const {{ Composer }} = await import({json.dumps(_COMPOSER.as_uri())});\n"
    )


def _run(native: bool, body: str) -> None:
    proc = run_node_source(_harness(native) + body)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_native_field_sizing_never_measures() -> None:
    """Typing lag in long sessions: the measuring path collapses the field to
    height:auto and reads scrollHeight on every keystroke — a forced layout
    that, on a multi-line draft, re-lays out the pane's whole message list twice
    per key.  Where the engine sizes the field itself (CSS field-sizing), the
    composer must tag it for chat.css and never read scrollHeight, whether the
    edit comes from typing, clear() or the value setter."""
    _run(
        True,
        """
const { c, ta, probe } = composer({});
assert(ta.classList.contains("composer-input--autosize"),
  "the native path must tag the field for chat.css content sizing");
ta.dispatch("input");
c.value = "two\\nlines";
c.clear();
assert(probe.reads === 0, "the native path must not read scrollHeight");
assert(!ta.style.height, "the native path must leave the height to CSS");
// A drag-resize leaves an inline height behind; the next edit drops it so
// content sizing resumes, as the measuring path overwrites it.
ta.style.height = "90px";
ta.dispatch("input");
assert(ta.style.height === "", "an edit must drop a drag-resize's inline height");
""",
    )


def test_measuring_fallback_without_field_sizing() -> None:
    """Engines without field-sizing keep the measuring path: no CSS tag, and
    every edit (typing, the value setter) fits the height to scrollHeight."""
    _run(
        False,
        """
const { c, ta, probe } = composer({});
assert(!ta.classList.contains("composer-input--autosize"),
  "the fallback must not claim CSS content sizing");
ta.dispatch("input");
assert(probe.reads === 1 && ta.style.height === "120px",
  "an edit must fit the height to scrollHeight, got " + ta.style.height);
probe.height = 90;
c.value = "shorter";
assert(probe.reads === 2 && ta.style.height === "90px",
  "the value setter must re-fit the height, got " + ta.style.height);
""",
    )


def test_multi_row_field_keeps_the_measuring_path() -> None:
    """Content sizing ignores `rows`, so a taller initial field (the home
    launcher's rows: 3) keeps the measuring path even where the engine could
    size it natively, and its rows stay the floor in every engine."""
    _run(
        True,
        """
const { ta, probe } = composer({ rows: 3 });
assert(!ta.classList.contains("composer-input--autosize"),
  "a multi-row field must not be content-sized by CSS");
ta.dispatch("input");
assert(probe.reads === 1 && ta.style.height === "120px",
  "a multi-row field must keep measuring, got " + ta.style.height);
""",
    )


def test_autoresize_opt_out_holds_on_both_paths() -> None:
    """autoResize:false keeps a fixed-height field on both paths: no CSS tag
    for content sizing and no measuring."""
    body = """
const { c, ta, probe } = composer({ autoResize: false });
assert(!ta.classList.contains("composer-input--autosize"),
  "an opted-out field must not be content-sized by CSS");
ta.dispatch("input");
c.value = "x";
assert(probe.reads === 0 && !ta.style.height, "an opted-out field must not be measured");
"""
    _run(True, body)
    _run(False, body)


def test_chat_css_content_sizes_the_tagged_field() -> None:
    """The class composer.js adds is the one chat.css content-sizes."""
    css = _CHAT_CSS.read_text(encoding="utf-8")
    rule = re.search(r"(?m)^\.composer-input--autosize \{([^}]*)\}", css)
    assert rule, "chat.css must carry the .composer-input--autosize rule"
    assert "field-sizing: content" in rule.group(1)
