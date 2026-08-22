"""Behavior guards for the shared transcript-presentation preference."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._js_harness_helpers import node_skip

_ROOT = Path(__file__).resolve().parent.parent
_MODULE = _ROOT / "turnstone/shared_static/transcript_presentation.js"


def test_module_exposes_only_the_shared_presentation_seams() -> None:
    body = _MODULE.read_text(encoding="utf-8")
    for name in (
        "getTranscriptPresentation",
        "setTranscriptPresentation",
        "canAutoFoldTranscriptBatch",
        "mountTranscriptPresentationToggle",
        "preserveTranscriptBottomPin",
        "registerTranscriptScroller",
    ):
        assert f"export function {name}" in body
    assert "fetch(" not in body
    assert "innerHTML" not in body
    assert "turnstone_interface.transcript_presentation" in body


_FAKE_DOM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.parentNode = null;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.textContent = "";
    this.title = "";
    this.type = "";
    this.paused = true;
    this.ended = false;
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.clientHeight = 0;
    this._connected = false;
    this._listeners = new Map();
    this.classList = {
      contains: (name) => this.className.split(/\s+/).filter(Boolean).includes(name),
      add: (...names) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((name) => set.add(name));
        this.className = Array.from(set).join(" ");
      },
      remove: (...names) => {
        const drop = new Set(names);
        this.className = this.className
          .split(/\s+/)
          .filter((name) => name && !drop.has(name))
          .join(" ");
      },
      toggle: (name, force) => {
        const next = force == null ? !this.classList.contains(name) : !!force;
        if (next) this.classList.add(name);
        else this.classList.remove(name);
        return next;
      },
    };
  }
  get isConnected() { return this._connected; }
  _setConnected(value) {
    this._connected = value;
    this.children.forEach((child) => child._setConnected(value));
  }
  appendChild(child) {
    if (child.parentNode) child.remove();
    this.children.push(child);
    child.parentNode = this;
    child._setConnected(this._connected);
    return child;
  }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  remove() {
    if (!this.parentNode) return;
    const i = this.parentNode.children.indexOf(this);
    if (i >= 0) this.parentNode.children.splice(i, 1);
    this.parentNode = null;
    this._setConnected(false);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, fn, options) {
    if (!this._listeners.has(name)) this._listeners.set(name, []);
    this._listeners.get(name).push({ fn, once: !!(options && options.once) });
  }
  removeEventListener(name, fn) {
    const rows = this._listeners.get(name) || [];
    this._listeners.set(name, rows.filter((row) => row.fn !== fn));
  }
  dispatch(name) {
    const rows = [...(this._listeners.get(name) || [])];
    for (const row of rows) {
      row.fn({ target: this });
      if (row.once) this.removeEventListener(name, row.fn);
    }
  }
  click() { this.dispatch("click"); }
  focus() { document.activeElement = this; }
  contains(node) {
    for (let current = node; current; current = current.parentNode) {
      if (current === this) return true;
    }
    return false;
  }
  _matches(selector) {
    selector = selector.trim();
    const attr = selector.match(/^(?:\.([\w-]+))?\[([\w-]+)="([^"]*)"\]$/);
    if (attr) {
      if (attr[1] && !this.classList.contains(attr[1])) return false;
      const name = attr[2];
      const value = name.startsWith("data-")
        ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())]
        : this.getAttribute(name);
      return String(value) === attr[3];
    }
    if (selector.startsWith(".")) {
      return selector.slice(1).split(".").every((name) => this.classList.contains(name));
    }
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }
  closest(selector) {
    for (let current = this; current; current = current.parentNode) {
      if (current._matches(selector)) return current;
    }
    return null;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim());
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selectors.some((part) => child._matches(part))) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getClientRects() { return this._visible === false ? [] : [{}]; }
}

const html = new FakeElement("html");
html._setConnected(true);
globalThis.document = {
  documentElement: html,
  activeElement: null,
  createElement: (tag) => new FakeElement(tag),
};
const storage = new Map();
let failGet = false;
let failSet = false;
globalThis.localStorage = {
  getItem: (key) => {
    if (failGet) throw new Error("get blocked");
    return storage.has(key) ? storage.get(key) : null;
  },
  setItem: (key, value) => {
    if (failSet) throw new Error("set blocked");
    storage.set(key, String(value));
  },
};
const windowListeners = new Map();
globalThis.window = {
  addEventListener: (name, fn) => windowListeners.set(name, fn),
};
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
const resizeObservers = [];
globalThis.ResizeObserver = class {
  constructor(fn) {
    this.fn = fn;
    this.targets = new Set();
    this.disconnected = false;
    resizeObservers.push(this);
  }
  observe(target) { this.targets.add(target); }
  disconnect() { this.disconnected = true; this.targets.clear(); }
};
globalThis.triggerResize = (target) => {
  for (const observer of resizeObservers) {
    if (!observer.disconnected && observer.targets.has(target)) observer.fn([]);
  }
};
"""


def _run_node(body: str) -> None:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", body],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    ("stored", "expected", "root_value"),
    [
        ("compact", "compact", "compact"),
        ("default", "default", None),
        ("unknown", "default", None),
    ],
)
@node_skip
def test_initial_stored_value_is_normalized(
    stored: str, expected: str, root_value: str | None
) -> None:
    script = (
        _FAKE_DOM
        + f"""
const key = "turnstone_interface.transcript_presentation";
storage.set(key, {json.dumps(stored)});
const mod = await import({json.dumps(_MODULE.as_uri() + "?stored=" + stored)});
if (mod.getTranscriptPresentation() !== {json.dumps(expected)})
  throw new Error("stored mode did not normalize");
if (html.getAttribute("data-transcript-presentation") !== {json.dumps(root_value)})
  throw new Error("stored mode stamped the wrong root state");
"""
    )
    _run_node(script)


@node_skip
def test_preference_toggle_storage_lifecycle_and_viewport_behavior() -> None:
    script = (
        _FAKE_DOM
        + f"""
const mod = await import({json.dumps(_MODULE.as_uri())});
const key = "turnstone_interface.transcript_presentation";
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};

assert(mod.getTranscriptPresentation() === "default", "missing value was not Default");
assert(!html.hasAttribute("data-transcript-presentation"), "Default stamped root mode");

const controls = new FakeElement("div");
html.appendChild(controls);
const unmount = mod.mountTranscriptPresentationToggle(controls);
const button = controls.children[0];
assert(button.getAttribute("aria-label") === "Compact ledger presentation", "unstable name");
assert(button.getAttribute("aria-pressed") === "false", "initial pressed state");
assert(button.title === "Switch to compact ledger presentation", "default title drifted");
assert(button.getAttribute("aria-description").includes("hides model reasoning"), "description missing omission");
button.click();
assert(mod.getTranscriptPresentation() === "compact", "toggle did not compact");
assert(html.getAttribute("data-transcript-presentation") === "compact", "root not compact");
assert(storage.get(key) === "compact", "compact was not persisted");
assert(button.getAttribute("aria-pressed") === "true", "control did not synchronize");
assert(button.title === "Switch to default ledger presentation", "compact title drifted");
button.click();
assert(mod.getTranscriptPresentation() === "default", "toggle did not restore Default");
assert(!html.hasAttribute("data-transcript-presentation"), "Default root override remained");
assert(storage.get(key) === "default", "explicit Default was not persisted");

mod.setTranscriptPresentation("not-a-mode");
assert(mod.getTranscriptPresentation() === "default", "invalid direct value did not normalize");
const onStorage = windowListeners.get("storage");
onStorage({{ key: "unrelated", newValue: "compact" }});
assert(mod.getTranscriptPresentation() === "default", "unrelated storage event applied");
onStorage({{ key, newValue: "compact" }});
assert(mod.getTranscriptPresentation() === "compact", "valid storage event did not apply");
onStorage({{ key, newValue: "invalid" }});
assert(mod.getTranscriptPresentation() === "default", "invalid storage value did not default");
onStorage({{ key: null, newValue: null }});
assert(mod.getTranscriptPresentation() === "default", "storage clear did not default");

failSet = true;
mod.setTranscriptPresentation("compact");
assert(mod.getTranscriptPresentation() === "compact", "storage failure blocked page state");
failSet = false;

const scroller = new FakeElement("div");
scroller.scrollHeight = 500;
scroller.clientHeight = 100;
scroller.scrollTop = 400;
html.appendChild(scroller);
const unregister = mod.registerTranscriptScroller(scroller);
assert(scroller.hasAttribute("data-transcript-root"), "root marker missing");
mod.preserveTranscriptBottomPin(scroller, () => {{
  scroller.scrollHeight = 700;
}});
assert(scroller.scrollTop === 700, "late reflow lost a captured bottom pin");
scroller.scrollTop = 100;
scroller.dispatch("scroll");
mod.preserveTranscriptBottomPin(scroller, () => {{
  scroller.scrollHeight = 800;
}});
assert(scroller.scrollTop === 100, "late reflow moved a scrolled-away viewport");
scroller.scrollHeight = 500;
scroller.scrollTop = 400;
scroller.dispatch("scroll");
mod.setTranscriptPresentation("default", {{ persist: false }});
assert(scroller.scrollTop === 500, "bottom-following scroller was not repinned");
scroller.scrollTop = 100;
mod.setTranscriptPresentation("compact", {{ persist: false }});
assert(scroller.scrollTop === 100, "scrolled-away viewport was moved");

// A background pane has no measurable rect while the global mode change
// reflows it. Retain its last bottom-follow state and restore only when the
// pane becomes visible again.
scroller.scrollHeight = 600;
scroller.scrollTop = 500;
scroller.dispatch("scroll");
scroller._visible = false;
scroller.scrollHeight = 900;
mod.setTranscriptPresentation("default", {{ persist: false }});
assert(scroller.scrollTop === 500, "hidden scroller was measured or moved early");
scroller._visible = true;
triggerResize(scroller);
assert(scroller.scrollTop === 900, "background bottom pin was not restored on activation");

scroller.scrollTop = 100;
scroller.dispatch("scroll");
scroller._visible = false;
mod.setTranscriptPresentation("compact", {{ persist: false }});
scroller._visible = true;
triggerResize(scroller);
assert(scroller.scrollTop === 100, "background scrolled-away viewport was moved");

const batch = new FakeElement("div");
batch.className = "conv-batch conv-batch--approved";
batch.dataset.resultsSettled = "true";
batch.dataset.compactFolded = "true";
const head = new FakeElement("div");
head.className = "conv-batch-head";
const disclosure = new FakeElement("button");
disclosure.className = "conv-batch-disclosure";
head.appendChild(disclosure);
const detail = new FakeElement("button");
batch.append(head, detail);
scroller.appendChild(batch);
detail.focus();
mod.setTranscriptPresentation("default", {{ persist: false }});
mod.setTranscriptPresentation("compact", {{ persist: false }});
assert(!Object.hasOwn(batch.dataset, "compactFolded"), "focused detail was hidden");

batch.dataset.compactFolded = "true";
const media = new FakeElement("audio");
media.paused = false;
batch.appendChild(media);
document.activeElement = null;
mod.setTranscriptPresentation("default", {{ persist: false }});
mod.setTranscriptPresentation("compact", {{ persist: false }});
assert(!Object.hasOwn(batch.dataset, "compactFolded"), "playing media was hidden");

disclosure.focus();
mod.setTranscriptPresentation("default", {{ persist: false }});
assert(document.activeElement === head, "Default switch stranded disclosure focus");
assert(head.getAttribute("tabindex") === "-1", "focus target was not temporary-focusable");
head.dispatch("blur");
assert(!head.hasAttribute("tabindex"), "temporary tabindex was retained");

unregister();
assert(!scroller.hasAttribute("data-transcript-root"), "root marker survived teardown");
unmount();
assert(controls.children.length === 0, "control survived teardown");
"""
    )
    _run_node(script)


@node_skip
def test_hidden_auto_fold_and_deferred_restore_use_current_follow_state() -> None:
    queued_dom = _FAKE_DOM.replace(
        "globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };",
        """
const animationFrames = [];
globalThis.requestAnimationFrame = (fn) => {
  animationFrames.push(fn);
  return animationFrames.length;
};
globalThis.flushAnimationFrames = () => {
  const pending = animationFrames.splice(0);
  pending.forEach((fn) => fn());
};
""",
    )
    script = (
        queued_dom
        + f"""
const mod = await import({json.dumps(_MODULE.as_uri() + "?deferred-races=1")});
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};

const scroller = new FakeElement("div");
scroller.scrollHeight = 500;
scroller.clientHeight = 100;
scroller.scrollTop = 400;
scroller.getBoundingClientRect = () => ({{ top: 0, bottom: 100 }});
const batch = new FakeElement("div");
batch.className = "conv-batch";
batch.getBoundingClientRect = () => ({{ top: 150, bottom: 170 }});
scroller.appendChild(batch);
html.appendChild(scroller);
mod.registerTranscriptScroller(scroller);

scroller.scrollTop = 100;
scroller.dispatch("scroll");
assert(
  mod.canAutoFoldTranscriptBatch(scroller, batch, {{ atBottom: false }}),
  "visible below-viewport batch could not fold",
);
batch.getBoundingClientRect = () => ({{ top: 50, bottom: 70 }});
assert(
  !mod.canAutoFoldTranscriptBatch(scroller, batch, {{ atBottom: false }}),
  "visible intersecting batch folded for a scrolled-away user",
);

scroller.scrollTop = 400;
scroller.dispatch("scroll");
scroller._visible = false;
batch._visible = false;
scroller.getBoundingClientRect = () => ({{ top: 0, bottom: 0 }});
batch.getBoundingClientRect = () => ({{ top: 0, bottom: 0 }});
assert(
  mod.canAutoFoldTranscriptBatch(scroller, batch, {{ atBottom: false }}),
  "hidden bottom-following pane lost its cached fold state",
);
scroller._visible = true;
batch._visible = true;
scroller.scrollTop = 100;
scroller.dispatch("scroll");
scroller._visible = false;
batch._visible = false;
assert(
  !mod.canAutoFoldTranscriptBatch(scroller, batch, {{ atBottom: true }}),
  "hidden scrolled-away pane trusted zero geometry or a stale caller cache",
);

scroller._visible = true;
batch._visible = true;
scroller.scrollHeight = 500;
scroller.scrollTop = 400;
scroller.dispatch("scroll");
mod.preserveTranscriptBottomPin(scroller, () => {{
  scroller.scrollHeight = 700;
}});
scroller.scrollTop = 100;
scroller.dispatch("scroll");
flushAnimationFrames();
assert(scroller.scrollTop === 100, "newer user scroll was overwritten by rAF restore");

scroller.scrollHeight = 500;
scroller.scrollTop = 400;
scroller.dispatch("scroll");
scroller._visible = false;
mod.preserveTranscriptBottomPin(scroller, () => {{
  scroller.scrollHeight = 900;
}});
flushAnimationFrames();
scroller._visible = true;
scroller.scrollTop = 100;
scroller.dispatch("scroll");
triggerResize(scroller);
assert(scroller.scrollTop === 100, "newer scroll was overwritten by hidden restore");
"""
    )
    _run_node(script)


@node_skip
def test_unreadable_initial_storage_fails_to_default() -> None:
    script = (
        _FAKE_DOM
        + f"""
failGet = true;
const mod = await import({json.dumps(_MODULE.as_uri() + "?unreadable=1")});
if (mod.getTranscriptPresentation() !== "default") throw new Error("read failure was not Default");
if (html.hasAttribute("data-transcript-presentation")) throw new Error("read failure stamped root");
"""
    )
    _run_node(script)
