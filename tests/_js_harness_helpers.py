"""Shared helpers for the Python-driven node harnesses that evaluate the
``shared_static`` ES modules with script semantics."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


FAKE_DOM = r"""
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


def has_node() -> bool:
    return shutil.which("node") is not None


def run_node_source(source: str, *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run ``source`` as an ES module under node and return the process.

    The one home for the write-temp-file / run / unlink plumbing the
    slice-and-run suites share (a sliced console function plus ``throw``
    checks); skips the test when node is absent.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", delete=False) as f:
        f.write(source)
        tmp = f.name
    try:
        return subprocess.run(["node", tmp], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        pytest.skip("node binary not available on PATH")
    finally:
        os.unlink(tmp)


# Module-level ``pytestmark = node_skip`` in each harness suite — the node
# detection lives here once, so a future change (version floor, env
# override) cannot land in one suite and silently miss another.
node_skip = pytest.mark.skipif(not has_node(), reason="node not available")


def demodulize(path: Path) -> str:
    """Strip ES-module syntax so ``vm.runInThisContext`` (script semantics)
    can evaluate the file: imports drop (the harness loads the whole
    dependency set into one shared context, so cross-file bindings resolve
    as context globals, exactly like the pre-module classic scripts), and
    ``export`` keywords peel off their declarations.

    Single-sourced here for every JS harness: a new module syntax form
    (``export default``, re-exports) must be handled once, not per suite —
    a divergence between per-file copies surfaces as a confusing
    ``vm.runInThisContext`` SyntaxError in whichever suite lagged.
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"^import\s+\{[\s\S]*?\}\s+from\s+\"[^\"]+\";\s*$", "", src, flags=re.M)
    src = re.sub(r"^import\s+[^;\n]+;\s*$", "", src, flags=re.M)
    src = re.sub(
        r"^export\s+(?=(?:async\s+)?(?:function|const|let|var|class)\b)", "", src, flags=re.M
    )
    src = re.sub(r"^export\s*\{[^}]*\};\s*$", "", src, flags=re.M)
    return src


def slice_braced_block(source: str, anchor: int) -> str | None:
    """Slice the ``{ … }`` block starting at/just after ``anchor``.

    THE brace walker every JS harness suite shares (the comment-AND-
    string-aware superset of the per-suite predecessors, which disagreed
    on comment handling and window bounds — the same source
    reorganization could pass one suite's structural pin while breaking
    the other's with a slice-dependent failure).  Comment awareness makes
    it correct on raw AND pre-stripped input alike.  Returns ``None``
    when no ``{`` opens within 200 chars of ``anchor`` (a missing brace
    must not silently slice some later unrelated block) or the block is
    unterminated.
    """
    start = source.find("{", anchor)
    if start == -1 or start - anchor > 200:
        return None
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    i = start
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in {'"', "'", "`"}:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    return None


def extract_braced(source: str, signature: str) -> str:
    """Extract one JS function/method (signature included) — raising form.

    ``signature`` must end at its opening ``{``.  The loud sibling of
    :func:`slice_braced_block` for suites that treat a missing or
    unterminated function as a hard failure rather than a skip.
    """
    start = source.index(signature)
    brace = start + len(signature) - 1
    if source[brace] != "{":
        raise AssertionError(f"signature does not end at an opening brace: {signature}")
    block = slice_braced_block(source, brace)
    if block is None:
        raise AssertionError(f"unterminated JavaScript function: {signature}")
    return source[start:brace] + block


def strip_js_comments(source: str) -> str:
    """Strip ``//`` and ``/* */`` comments for source-pattern assertions —
    the single implementation every JS harness suite shares.

    STRING-AWARE and OFFSET-PRESERVING (comments become spaces, byte
    length identical): a ``//`` inside a string literal (``"https://…"``)
    is content, not a comment — a string-blind scanner truncates the rest
    of the line, and pattern pins then silently assert against corrupted
    text (a ``not in`` guard passes vacuously after the pattern it
    polices was reintroduced).  Length preservation keeps downstream
    offset math (brace walkers, ``.index`` comparisons) valid.  This is
    the strict superset of every per-suite predecessor, hoisted so the
    suites cannot diverge again.

    Limitation — regex literals (``/pattern/flags``) are not detected: a
    ``//`` inside one would be misread as a line comment.  Safe for
    every region currently scanned; extend the tracker before scanning a
    region with regex literals.
    """
    out: list[str] = []
    n = len(source)
    i = 0
    in_str: str | None = None
    while i < n:
        ch = source[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        # Line comment: replace with spaces up to newline (preserve
        # length so downstream offset math still works).
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            j = source.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment: replace with spaces up to closing */.
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                i = n
                continue
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
        out.append(ch)
        i += 1
    return "".join(out)
