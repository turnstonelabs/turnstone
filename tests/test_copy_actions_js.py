"""Behavior tests for ``turnstone/shared_static/copy_actions.js``.

Same node + browser-shim approach as ``test_renderer_js.py``: the modules are
demodulized and evaluated with script semantics against stub DOM elements.
The harness records delegated listeners by type, so the tests drive the REAL
interaction layer — the mouseover/keydown delegation, the show/hide
lifecycle, and the pane-level busy gate — not just the exported resolvers.
Placement geometry (clamps, collision, scroll-follow coordinates) is
deliberately NOT asserted here: the stub rects are uniform; the livepass copy
harness (scripts/livepass.py, COPY-READY / COPY-KBD-READY verdicts) owns
rendered placement.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests._js_harness_helpers import demodulize, node_skip

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UTILS_JS = _REPO_ROOT / "turnstone/shared_static/utils.js"
_TOAST_JS = _REPO_ROOT / "turnstone/shared_static/toast.js"
_COPY_ACTIONS_JS = _REPO_ROOT / "turnstone/shared_static/copy_actions.js"


pytestmark = node_skip


# The three interpolated sources never change between tests — read,
# demodulize, and JSON-encode them once so each test costs exactly one
# node spawn.
_UTILS_SRC = json.dumps(demodulize(_UTILS_JS))
_TOAST_SRC = json.dumps(demodulize(_TOAST_JS))
_COPY_ACTIONS_SRC = json.dumps(demodulize(_COPY_ACTIONS_JS))


# The stub element models what the tested seams touch: class/attr/parent
# plumbing, a selector subset for closest()/matches (type, .class, [attr] and
# [attr="value"], comma lists — everything the module's selectors use),
# listener capture with a synthetic .click(), per-element rects, and a focus
# spy that tracks document.activeElement.  document.addEventListener records
# handlers by type so tests can fire delegated events (fireDoc merges extra
# event fields, e.g. {key} for keydown); the flash timer is real (node
# setTimeout) and the template exits the process after each body so the 1.4s
# revert never holds it open.
_HARNESS_TEMPLATE = """
const vm = require('vm');

class StubEl {}
global.Element = StubEl;

function hasClass(el, c) {
  return (
    (el._classes && el._classes.has(c)) ||
    (el.className || '').split(/\\s+/).includes(c)
  );
}

function matchesSel(el, sel) {
  return sel.split(',').some((s) => {
    s = s.trim();
    if (s.startsWith('.')) return hasClass(el, s.slice(1));
    if (s.startsWith('[')) {
      const body = s.slice(1, -1);
      const eq = body.indexOf('=');
      if (eq === -1) return el.getAttribute(body) !== null;
      let want = body.slice(eq + 1);
      if (want.startsWith('"') || want.startsWith("'")) want = want.slice(1, -1);
      return el.getAttribute(body.slice(0, eq)) === want;
    }
    return el.tagName === s.toUpperCase();
  });
}

function makeEl(tag) {
  const el = new StubEl();
  Object.assign(el, {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    attrs: {},
    style: {},
    listeners: {},
    _classes: new Set(),
    textContent: '',
    value: '',
    title: '',
    className: '',
    type: '',
    isConnected: true,
    parentNode: null,
    parentElement: null,
  });
  el.setAttribute = (n, v) => { el.attrs[n] = String(v); };
  el.getAttribute = (n) => (n in el.attrs ? el.attrs[n] : null);
  el.removeAttribute = (n) => { delete el.attrs[n]; };
  el.appendChild = (c) => {
    if (c.parentNode) {
      const i = c.parentNode.children.indexOf(c);
      if (i !== -1) c.parentNode.children.splice(i, 1);
    }
    el.children.push(c);
    c.parentNode = el;
    c.parentElement = el;
    return c;
  };
  el.remove = () => { el._removed = true; };
  el.select = () => {};
  // Focus succeeds only for focusable elements — mirroring the browser is
  // what lets tests catch focus plumbing that silently no-ops on a
  // non-focusable target (form controls, tabindex attr, or a programmatic
  // tabIndex assignment).
  el.tabIndex = undefined;
  el.focus = (opts) => {
    const focusable =
      el.tabIndex !== undefined ||
      'tabindex' in el.attrs ||
      ['INPUT', 'TEXTAREA', 'BUTTON', 'SELECT'].includes(el.tagName);
    if (!focusable) return;
    el._focused = (el._focused || 0) + 1;
    el._focusOpts = opts || null;
    global.document.activeElement = el;
  };
  el.hasAttribute = (n) => n in el.attrs;
  el.addEventListener = (t, fn) => {
    (el.listeners[t] = el.listeners[t] || []).push(fn);
  };
  el.click = (evt) =>
    (el.listeners.click || []).forEach((fn) =>
      fn(evt || { stopPropagation() {} }));
  el.querySelector = () => null;
  el.querySelectorAll = (sel) => {
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        if (matchesSel(c, sel)) out.push(c);
        walk(c);
      }
    };
    walk(el);
    return out;
  };
  el.closest = (sel) => {
    for (let n = el; n; n = n.parentElement) {
      if (matchesSel(n, sel)) return n;
    }
    return null;
  };
  el.contains = (x) => {
    for (let n = x; n; n = n.parentNode) if (n === el) return true;
    return false;
  };
  el.getBoundingClientRect = () =>
    el._rect || { top: 10, bottom: 110, left: 0, right: 200, width: 200, height: 100 };
  el.classList = {
    add: (...cs) => cs.forEach((c) => el._classes.add(c)),
    remove: (...cs) => cs.forEach((c) => el._classes.delete(c)),
    contains: (c) => hasClass(el, c),
  };
  return el;
}

const createdEls = [];
const toastEl = makeEl('div');
const docListeners = {};
// Ranges clone like the real Range: a restored clone must itself be
// cloneable, or the SECOND copy's save pass crashes on it.
function makeRange(marker, clone) {
  return {
    marker,
    clone: !!clone,
    cloneRange() { return makeRange(this.marker, true); },
  };
}
const prevRange = makeRange('prev', false);
const selection = {
  _ranges: [prevRange],
  get rangeCount() { return this._ranges.length; },
  getRangeAt(i) { return this._ranges[i]; },
  removeAllRanges() { this._ranges = []; },
  addRange(r) { this._ranges.push(r); },
};
global.__execResult = true;
global.document = {
  createElement: (tag) => {
    const el = makeEl(tag);
    createdEls.push(el);
    return el;
  },
  body: makeEl('body'),
  // The window-exit dismissal listens on the <html> element (document-
  // level mouseleave delivery is flaky on window exit), so the stub
  // carries a documentElement with its own listener map.
  documentElement: makeEl('html'),
  addEventListener: (t, fn) => {
    (docListeners[t] = docListeners[t] || []).push(fn);
  },
  getSelection: () => selection,
  getElementById: (id) => (id === 'toast' ? toastEl : null),
  querySelector: () => null,
  execCommand: () => global.__execResult,
  activeElement: undefined,
};
global.window = global;
global.innerWidth = 800;
global.innerHeight = 600;
global.getComputedStyle = () => ({ overflowY: 'visible' });
global.requestAnimationFrame = (fn) => { fn(); return 0; };
// Node >= 21 ships globalThis.navigator as a built-in accessor with no
// setter, so a plain `global.navigator = ...` silently no-ops and the
// secure-context branch would never see the stub clipboard.  Install by
// property definition instead.
function setNavigator(nav) {
  Object.defineProperty(global, 'navigator', {
    value: nav,
    configurable: true,
    writable: true,
  });
}
setNavigator({});
global.isSecureContext = false;

const fireDoc = (type, target, props) =>
  (docListeners[type] || []).forEach((fn) =>
    fn(Object.assign({ target }, props || {})));
const fabEl = () =>
  createdEls.find((e) => hasClass(e, 'block-copy-btn')) || null;
// The announce idiom is clear-then-set on a short timer; outcome
// announcements are only observable after it fires.
const settle = (ms) => new Promise((r) => setTimeout(r, ms == null ? 40 : ms));

vm.runInThisContext(%(utils_src)s);
vm.runInThisContext(%(toast_src)s);
vm.runInThisContext(%(copy_actions_src)s);
// The module's aria-live region is the first sr-only appended to body.
const liveRegion = document.body.children.find((c) => hasClass(c, 'sr-only'));

// A bubble fixture: .msg > (.msg-body > table.table-wrap) + bar with a
// copy button — the DOM shape both chat clients build, with the REAL
// layout's geometry: the bar hugs the bubble's top-right corner and the
// block sits below it (chat.css .msg-actions top/right 4px).  Overlapping
// default rects would make every reveal read as an unresolvable bar
// collision.
function makeBubble(sourceText) {
  const msg = makeEl('div');
  msg.className = 'msg assistant';
  msg._rect = { top: 0, bottom: 160, left: 0, right: 200, width: 200, height: 160 };
  const body = makeEl('div');
  body.className = 'msg-body';
  const table = makeEl('div');
  table._classes.add('table-wrap');
  table.setAttribute('data-md-source', sourceText || '| a |');
  table.setAttribute('tabindex', '0');
  table._rect = { top: 40, bottom: 150, left: 0, right: 200, width: 200, height: 110 };
  const bar = makeEl('div');
  bar.className = 'msg-actions';
  bar._rect = { top: 4, bottom: 28, left: 150, right: 196, width: 46, height: 24 };
  const barBtn = makeEl('button');
  barBtn.className = 'msg-action-btn msg-copy-btn';
  document.body.appendChild(msg);
  msg.appendChild(body);
  body.appendChild(table);
  msg.appendChild(bar);
  bar.appendChild(barBtn);
  return { msg, body, table, bar, barBtn };
}

// Wrap a bubble in a messages container carrying the pane-level busy
// stamp — the DOM fact both chat clients maintain (data-busy="true"
// while a turn is in flight, "false" at idle).
function makeBusyWrap(bubble, busy) {
  const wrap = makeEl('div');
  wrap.className = 'messages';
  wrap.setAttribute('data-busy', busy === false ? 'false' : 'true');
  document.body.appendChild(wrap);
  wrap.appendChild(bubble.msg);
  return wrap;
}

(async () => {
  const out = await (async () => { %(body)s })();
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})().catch((e) => {
  console.error((e && e.stack) || e);
  process.exit(1);
});
"""


def _run(body: str) -> dict[str, object]:
    harness = _HARNESS_TEMPLATE % {
        "utils_src": _UTILS_SRC,
        "toast_src": _TOAST_SRC,
        "copy_actions_src": _COPY_ACTIONS_SRC,
        "body": body,
    }
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    parsed: dict[str, object] = json.loads(result.stdout)
    return parsed


# ---------------------------------------------------------------------------
# Source resolution — what a copy click actually lifts
# ---------------------------------------------------------------------------


def test_block_copy_source_resolves_each_block_kind() -> None:
    """Each rendered block kind resolves to its SOURCE: mermaid containers
    via ``data-mermaid-source`` (the source the diagram was rendered from —
    the pre it replaced is gone from the DOM), tables via the render-time
    ``data-md-source`` stash, and code fences via ``code.textContent``
    (escapeHtml round-trips; hljs spans don't change text content)."""
    out = _run(
        """
      const mermaid = makeEl('div');
      mermaid._classes.add('mermaid-container');
      mermaid.attrs['data-mermaid-source'] = 'graph TD;\\n  A-->B';
      const table = makeEl('div');
      table._classes.add('table-wrap');
      table.attrs['data-md-source'] = '| a |\\n|---|\\n| 1 |';
      const code = makeEl('code');
      code.textContent = 'x = 1\\nprint(x)';
      const pre = makeEl('pre');
      pre.querySelector = (sel) => (sel === 'code' ? code : null);
      const barePre = makeEl('pre');
      barePre.textContent = 'no code child';
      return {
        mermaid: blockCopySource(mermaid),
        table: blockCopySource(table),
        pre: blockCopySource(pre),
        barePre: blockCopySource(barePre),
        missing: blockCopySource(null),
      };
    """
    )
    assert out["mermaid"] == "graph TD;\n  A-->B"
    assert out["table"] == "| a |\n|---|\n| 1 |"
    assert out["pre"] == "x = 1\nprint(x)"
    assert out["barePre"] == "no code child"
    assert out["missing"] == ""


def test_msg_copy_source_priority_chain() -> None:
    """A bubble copies the raw markdown the render pipeline stashed on its
    ``.msg-body`` (``_copySource`` — set unconditionally per applied
    frame, including frames whose markdown render threw and painted as
    plain text).  The visible textContent is the honest last resort for a
    body that never went through the render pipeline.  The stash is
    whole-source by contract: it may carry syntax the render does not
    display."""
    out = _run(
        """
      const body = makeEl('div');
      body._copySource = 'full **source**';
      body.textContent = 'rendered';
      const msg = makeEl('div');
      msg.querySelector = (sel) => (sel === '.msg-body' ? body : null);
      const bareBody = makeEl('div');
      bareBody.textContent = 'plain rendered text';
      const bare = makeEl('div');
      bare.querySelector = (sel) => (sel === '.msg-body' ? bareBody : null);
      return {
        copySourceWins: msgCopySource(msg),
        textFallback: msgCopySource(bare),
        noBody: msgCopySource(makeEl('div')),
      };
    """
    )
    assert out["copySourceWins"] == "full **source**"
    assert out["textFallback"] == "plain rendered text"
    assert out["noBody"] == ""


# ---------------------------------------------------------------------------
# Clipboard transport — secure-context API vs plain-HTTP fallback
# ---------------------------------------------------------------------------


def test_copy_text_fallback_restores_selection_and_focus() -> None:
    """Plain-HTTP LAN nodes have no ``navigator.clipboard``; the legacy
    hidden-textarea + execCommand path must carry the copy, surface the
    command's verdict, and leave the user's world as it found it: the
    prior selection re-added and the previously focused element
    re-focused (execCommand copies the textarea's selection, so both are
    disturbed mid-flight).  The textarea itself is marked as the
    clipboard shim so delegated UI listeners can ignore it."""
    out = _run(
        """
      const prevFocusEl = makeEl('input');
      document.activeElement = prevFocusEl;
      const okResult = await copyTextToClipboard('fence content');
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      global.__execResult = false;
      const failResult = await copyTextToClipboard('x');
      return {
        okResult,
        failResult,
        taValue: ta ? ta.value : null,
        taRemoved: ta ? !!ta._removed : null,
        taShimMarked: ta ? ta.getAttribute('data-clipboard-shim') !== null : null,
        rangesAfter: selection._ranges.map(
          (r) => r.marker + (r.clone ? ':clone' : '')),
        refocusCount: prevFocusEl._focused || 0,
        refocusPreventScroll: prevFocusEl._focusOpts
          ? prevFocusEl._focusOpts.preventScroll === true
          : false,
      };
    """
    )
    assert out["okResult"] is True
    assert out["failResult"] is False
    assert out["taValue"] == "fence content"
    assert out["taRemoved"] is True
    assert out["taShimMarked"] is True
    # Restore invariants: after both copies the selection holds exactly one
    # range carrying the original's content, and it is a CLONE — getRangeAt
    # returns live ranges the shim's select() can collapse in place, so a
    # live ref re-added here would "restore" the collapsed range.  No
    # accumulation across copies, and the previously focused element got
    # focus({preventScroll}) once per copy.
    assert out["rangesAfter"] == ["prev:clone"]
    assert out["refocusCount"] == 2
    assert out["refocusPreventScroll"] is True


def test_copy_text_uses_async_clipboard_in_secure_context() -> None:
    """On HTTPS / localhost the async Clipboard API is the transport; the
    legacy path must not run at all (no stray textarea, no execCommand)."""
    out = _run(
        """
      let wrote = null;
      let execCalled = false;
      global.isSecureContext = true;
      setNavigator({
        clipboard: {
          writeText: (v) => { wrote = v; return Promise.resolve(); },
        },
      });
      global.document.execCommand = () => { execCalled = true; return true; };
      const ok = await copyTextToClipboard('secret token');
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return { ok, wrote, execCalled, madeTextarea: !!ta };
    """
    )
    assert out["ok"] is True
    assert out["wrote"] == "secret token"
    assert out["execCalled"] is False
    assert out["madeTextarea"] is False


def test_copy_text_clipboard_rejection_falls_through_to_legacy() -> None:
    """A secure-context permission rejection is not a dead end — the legacy
    path still gets its shot, and its verdict is the caller's answer."""
    out = _run(
        """
      global.isSecureContext = true;
      setNavigator({
        clipboard: { writeText: () => Promise.reject(new Error('denied')) },
      });
      const ok = await copyTextToClipboard('salvaged');
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return { ok, taValue: ta ? ta.value : null };
    """
    )
    assert out["ok"] is True
    assert out["taValue"] == "salvaged"


# ---------------------------------------------------------------------------
# The bubble copy button — shape, click behavior, busy gate
# ---------------------------------------------------------------------------


def test_msg_copy_button_click_copies_and_flashes_copied() -> None:
    """A click lifts the stashed markdown through the clipboard helper and
    flashes the ✓ state (class + plain-language title); the button carries
    the toolbar chrome the .msg-actions bars expect, and the outcome is
    announced via the live region (clear-then-set, so it lands a beat
    after the flash)."""
    out = _run(
        """
      const body = makeEl('div');
      body._copySource = 'raw **markdown**';
      const msg = makeEl('div');
      msg.querySelector = (sel) => (sel === '.msg-body' ? body : null);
      const btn = buildMsgCopyButton(msg);
      btn.click();
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        className: btn.className,
        type: btn.type,
        aria: btn.attrs['aria-label'],
        iconClass: btn.children[0].className,
        flash: [...btn._classes],
        title: btn.title,
        copied: ta ? ta.value : null,
        announced: liveRegion ? liveRegion.textContent : null,
        toast: toastEl.textContent,
      };
    """
    )
    assert out["className"] == "msg-action-btn msg-copy-btn"
    assert out["type"] == "button"
    assert out["aria"] == "Copy message"
    assert out["iconClass"] == "icon-copy"
    assert out["flash"] == ["is-copied"]
    assert out["title"] == "Copied"
    assert out["copied"] == "raw **markdown**"
    assert out["announced"] == "Copied"
    assert out["toast"] == "", "success must not raise a toast"


def test_empty_source_copies_empty_string_with_honest_outcome() -> None:
    """A source that RESOLVES empty is copied like any other: the write is
    attempted (the clipboard genuinely ends up holding the empty string)
    and the flash reports the transport's verdict — never a manufactured
    failure whose "select the text" hint points at nothing."""
    out = _run(
        """
      const btn = buildMsgCopyButton(makeEl('div'));
      btn.click();
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        flash: [...btn._classes],
        title: btn.title,
        madeTextarea: !!ta,
        taValue: ta ? ta.value : null,
        announced: liveRegion ? liveRegion.textContent : null,
        toast: toastEl.textContent,
        liveRegionEager: !!liveRegion,
      };
    """
    )
    assert out["flash"] == ["is-copied"]
    assert out["title"] == "Copied"
    assert out["madeTextarea"] is True
    assert out["taValue"] == ""
    # Outcome surfaces at the button only — flash, title, and ONE
    # aria-live announcement.  No toast: identical behavior on all three
    # pages beats louder surfacing (the empty stub host is what makes
    # this a real assertion).
    assert out["toast"] == ""
    assert out["announced"] == "Copied"
    # The region must predate the first announcement — AT only announces
    # changes to a region already in the accessibility tree.
    assert out["liveRegionEager"] is True


def test_failed_copy_flashes_failure_state() -> None:
    """A transport failure (execCommand false on a plain-HTTP node) flashes
    the ✗ state with the manual-copy hint — never a false ✓."""
    out = _run(
        """
      global.__execResult = false;
      const body = makeEl('div');
      body._copySource = 'content';
      const msg = makeEl('div');
      msg.querySelector = (sel) => (sel === '.msg-body' ? body : null);
      const btn = buildMsgCopyButton(msg);
      btn.click();
      await settle();
      return { flash: [...btn._classes], title: btn.title };
    """
    )
    assert out["flash"] == ["is-copy-failed"]
    assert out["title"] == "Copy failed — select the text and copy manually"


def test_busy_pane_refuses_bubble_copy_click() -> None:
    """Every copy affordance is idle-only.  The bubble button's click path
    must refuse in JS while the messages container carries
    data-busy="true" — the chat.css pointer-events gate alone cannot stop
    Enter on a button that already holds focus.  No clipboard write, but
    the refusal ANSWERS: ✗ flash plus a busy-specific announcement — a
    keyboard user must be able to tell "refused while streaming" from
    "keystroke lost".  The same click works once the container returns
    to idle."""
    out = _run(
        """
      const a = makeBubble('| a |');
      const wrap = makeBusyWrap(a, true);
      const btn = buildMsgCopyButton(a.msg);
      a.bar.appendChild(btn);
      a.body._copySource = 'the reply';
      a.msg.querySelector = (sel) => (sel === '.msg-body' ? a.body : null);
      btn.click();
      await settle();
      const taBusy = createdEls.find((e) => e.tagName === 'TEXTAREA');
      const busyState = {
        madeTextarea: !!taBusy,
        flash: [...btn._classes],
        busyTitle: btn.title,
        announced: liveRegion.textContent,
      };
      wrap.setAttribute('data-busy', 'false');
      btn.click();
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        ...busyState,
        idleCopied: ta ? ta.value : null,
        idleFlash: [...btn._classes],
      };
    """
    )
    assert out["madeTextarea"] is False
    assert out["flash"] == ["is-copy-failed"]
    assert out["busyTitle"] == "Copy is available when the reply finishes"
    assert out["announced"] == "Copy is available when the reply finishes"
    assert out["idleCopied"] == "the reply"
    assert out["idleFlash"] == ["is-copied"]


# ---------------------------------------------------------------------------
# The action bar helpers — the shared ARIA contract
# ---------------------------------------------------------------------------


def test_ensure_msg_actions_bar_contract_and_reuse() -> None:
    """One helper owns the bar's ARIA contract for every adder in both
    clients: role=toolbar + accessible name, direct-child placement, and
    reuse of an existing bar instead of stacking a second one."""
    out = _run(
        """
      const msg = makeEl('div');
      msg.className = 'msg';
      const bar1 = ensureMsgActionsBar(msg);
      const bar2 = ensureMsgActionsBar(msg);
      return {
        sameBar: bar1 === bar2,
        cls: bar1.className,
        role: bar1.attrs['role'],
        label: bar1.attrs['aria-label'],
        isDirectChild: msg.children.includes(bar1),
        found: findMsgActionsBar(msg) === bar1,
        foundOnBare: findMsgActionsBar(makeEl('div')),
      };
    """
    )
    assert out["sameBar"] is True
    assert out["cls"] == "msg-actions"
    assert out["role"] == "toolbar"
    assert out["label"] == "Message actions"
    assert out["isDirectChild"] is True
    assert out["found"] is True
    assert out["foundOnBare"] is None


# ---------------------------------------------------------------------------
# The floating button — pointer-only reveal, unconditional dismissal
# ---------------------------------------------------------------------------


def test_hover_reveals_and_click_copies_block_source() -> None:
    """The pointer path end to end: hovering a block reveals the floating
    button (mounted once in <body>, permanently out of the tab order —
    keyboard has its own Enter path), and a click lands the block's
    SOURCE on the clipboard with a ✓ flash."""
    out = _run(
        """
      const a = makeBubble('| a |\\n|---|\\n| 1 |');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      fab.click();
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        visible: fab.classList.contains('is-visible'),
        inBody: fab.parentNode === document.body,
        tabindex: fab.getAttribute('tabindex'),
        copied: ta ? ta.value : null,
        flash: [...fab._classes].filter((c) => c.startsWith('is-cop')),
        announced: liveRegion.textContent,
      };
    """
    )
    assert out["visible"] is True
    assert out["inBody"] is True
    assert out["tabindex"] == "-1"
    assert out["copied"] == "| a |\n|---|\n| 1 |"
    assert out["flash"] == ["is-copied"]
    assert out["announced"] == "Copied"


def test_pointer_dismissal_is_unconditional() -> None:
    """Dismissal has no focus or keyboard gating left: a mouseover outside
    any block hides the button, mouseleave (pointer leaving the window)
    hides it, a scroll in its context hides it — even while an element
    inside the block-and-button world holds focus — and a fresh hover
    re-reveals after every dismissal.  Without these pins, deleting a
    dismissal listener ships green while the button strands painted over
    unrelated content."""
    out = _run(
        """
      const a = makeBubble('| a |');
      const outsider = makeEl('div');
      document.body.appendChild(outsider);
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      const shown = fab.classList.contains('is-visible');
      fireDoc('mouseover', outsider);
      const hiddenByMouseover = !fab.classList.contains('is-visible');
      fireDoc('mouseover', a.table);
      const reshown = fab.classList.contains('is-visible');
      (document.documentElement.listeners.mouseleave || []).forEach((fn) =>
        fn({ target: outsider }));
      const hiddenByMouseleave = !fab.classList.contains('is-visible');
      fireDoc('mouseover', a.table);
      fireDoc('scroll', document);
      const hiddenByScroll = !fab.classList.contains('is-visible');
      // Focus anywhere in the old "keyboard-owned" world must not spare
      // the reveal: pointer dismissal is unconditional now.
      fireDoc('mouseover', a.table);
      document.activeElement = a.table;
      fireDoc('mouseover', outsider);
      const hiddenDespiteBlockFocus = !fab.classList.contains('is-visible');
      fireDoc('mouseover', a.table);
      document.activeElement = fab;
      fireDoc('mouseover', outsider);
      const hiddenDespiteFabFocus = !fab.classList.contains('is-visible');
      return {
        shown,
        hiddenByMouseover,
        reshown,
        hiddenByMouseleave,
        hiddenByScroll,
        hiddenDespiteBlockFocus,
        hiddenDespiteFabFocus,
      };
    """
    )
    assert out["shown"] is True
    assert out["hiddenByMouseover"] is True
    assert out["reshown"] is True
    assert out["hiddenByMouseleave"] is True
    assert out["hiddenByScroll"] is True
    assert out["hiddenDespiteBlockFocus"] is True
    assert out["hiddenDespiteFabFocus"] is True


def test_clipboard_shim_hover_does_not_dismiss_button() -> None:
    """The legacy copy path's off-screen textarea sits at the viewport
    origin; a pointer event targeting it mid-copy must not read as "the
    pointer left the block" and dismiss the button whose outcome flash is
    about to land.  The shim marker (set in utils.js) exempts it."""
    out = _run(
        """
      const a = makeBubble('| a |');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      const shim = makeEl('textarea');
      shim.setAttribute('data-clipboard-shim', '');
      document.body.appendChild(shim);
      fireDoc('mouseover', shim);
      return { afterShimHover: fab.classList.contains('is-visible') };
    """
    )
    assert out["afterShimHover"] is True


def test_disconnected_target_click_flashes_failure() -> None:
    """The click guard is target-liveness only: when the revealed block has
    left the DOM by click time (transcript wipe under the pointer), the
    click fails with an honest ✗ — it never silently copies a stale node
    and never retargets to whatever now occupies the space, even when a
    same-kind replacement exists."""
    out = _run(
        """
      const a = makeBubble('| gone |');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      a.body.children.length = 0;
      a.table.isConnected = false;
      a.table.parentNode = null;
      a.table.parentElement = null;
      const fresh = makeEl('div');
      fresh._classes.add('table-wrap');
      fresh.setAttribute('data-md-source', '| fresh |');
      a.body.appendChild(fresh);
      fab.click();
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        flash: [...fab._classes].filter((c) => c.startsWith('is-cop')),
        madeTextarea: !!ta,
        toast: toastEl.textContent,
      };
    """
    )
    assert out["flash"] == ["is-copy-failed"]
    assert out["madeTextarea"] is False
    assert out["toast"] == ""


def test_nested_error_pre_lifts_to_container() -> None:
    """The mermaid ERROR state draws its message + source as a <pre> INSIDE
    the .mermaid-container.  That pre is a rendering detail, not a copy
    target: hovering it must reveal the button FOR THE CONTAINER (whose
    data-mermaid-source is the honest source), and an Enter keydown
    reaching the nested pre must copy the container too — both paths lift
    through the same helper."""
    out = _run(
        """
      const a = makeBubble('| t |');
      const mermaid = makeEl('div');
      mermaid._classes.add('mermaid-container');
      mermaid.setAttribute('data-mermaid-source', 'graph TD');
      mermaid.setAttribute('tabindex', '0');
      const errPre = makeEl('pre');
      errPre.textContent = 'mermaid error source';
      a.body.appendChild(mermaid);
      mermaid.appendChild(errPre);
      fireDoc('mouseover', errPre);
      const hoverTarget = blockCopySource(_fabTarget);
      fireDoc('keydown', errPre, { key: 'Enter' });
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        hoverTarget,
        kbdCopied: ta ? ta.value : null,
        flashOnContainer: mermaid.classList.contains('is-copied'),
        flashOnErrPre: errPre.classList.contains('is-copied'),
      };
    """
    )
    assert out["hoverTarget"] == "graph TD"
    assert out["kbdCopied"] == "graph TD"
    assert out["flashOnContainer"] is True
    assert out["flashOnErrPre"] is False


def test_busy_pane_gates_fab_reveal_and_click() -> None:
    """The floating button must never REVEAL while the pane is busy (the
    turn is mutating the transcript under it), and a click on a button
    revealed at idle whose pane went busy before the click refuses with
    an honest ✗ and the busy-specific announcement instead of copying —
    or silently swallowing — the gesture.  Idle again, the same hover
    reveals normally."""
    out = _run(
        """
      const a = makeBubble('| a |');
      const wrap = makeBusyWrap(a, true);
      fireDoc('mouseover', a.table);
      const revealedWhileBusy = !!fabEl() && fabEl().classList.contains('is-visible');
      wrap.setAttribute('data-busy', 'false');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      const revealedAtIdle = fab.classList.contains('is-visible');
      wrap.setAttribute('data-busy', 'true');
      fab.click();
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        revealedWhileBusy,
        revealedAtIdle,
        clickFlash: [...fab._classes].filter((c) => c.startsWith('is-cop')),
        announced: liveRegion.textContent,
        madeTextarea: !!ta,
      };
    """
    )
    assert out["revealedWhileBusy"] is False
    assert out["revealedAtIdle"] is True
    assert out["clickFlash"] == ["is-copy-failed"]
    assert out["announced"] == "Copy is available when the reply finishes"
    assert out["madeTextarea"] is False


def test_within_block_move_dismisses_fab_when_turn_starts() -> None:
    """The mouseover fast path (pointer moving WITHIN the already-targeted
    block) must still honor the busy gate: a turn starting under a shown
    button dismisses it on the NEXT pointer move — not only once the
    pointer eventually leaves the block."""
    out = _run(
        """
      const a = makeBubble('| a |');
      const wrap = makeBusyWrap(a, false);
      const cell = makeEl('span');
      a.table.appendChild(cell);
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      const revealedAtIdle = fab.classList.contains('is-visible');
      wrap.setAttribute('data-busy', 'true');
      fireDoc('mouseover', cell);
      return {
        revealedAtIdle,
        visibleAfterBusyMove: fab.classList.contains('is-visible'),
      };
    """
    )
    assert out["revealedAtIdle"] is True
    assert out["visibleAfterBusyMove"] is False


def test_scroll_dismissal_follows_containment() -> None:
    """Scroll hides the button exactly when the scrolled thing MOVES the
    block: the document, or any scrollable ancestor holding the block,
    dismisses; a pan INSIDE the block (a fence's internal horizontal
    scroll) and an unrelated scroller (a sidebar) keep it — regardless
    of whether the surface has an overflow container at all."""
    out = _run(
        """
      const a = makeBubble('| a |');
      const wrap = makeBusyWrap(a, false);
      const sidebar = makeEl('div');
      document.body.appendChild(sidebar);
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      const shown = fab.classList.contains('is-visible');
      fireDoc('scroll', a.table);
      const keptOnInternalPan = fab.classList.contains('is-visible');
      fireDoc('scroll', sidebar);
      const keptOnSidebar = fab.classList.contains('is-visible');
      fireDoc('scroll', wrap);
      const hiddenOnAncestor = !fab.classList.contains('is-visible');
      fireDoc('mouseover', a.table);
      fireDoc('scroll', document);
      const hiddenOnDocument = !fab.classList.contains('is-visible');
      return {
        shown,
        keptOnInternalPan,
        keptOnSidebar,
        hiddenOnAncestor,
        hiddenOnDocument,
      };
    """
    )
    assert out["shown"] is True
    assert out["keptOnInternalPan"] is True
    assert out["keptOnSidebar"] is True
    assert out["hiddenOnAncestor"] is True
    assert out["hiddenOnDocument"] is True


def test_inflight_copy_outcome_is_orphaned_by_retarget() -> None:
    """The clipboard write settles asynchronously; a re-target inside that
    gap must orphan the pending outcome — otherwise the ✓ and its
    announcement land on a block the user never copied."""
    out = _run(
        """
      global.isSecureContext = true;
      let resolveWrite = null;
      setNavigator({
        clipboard: {
          writeText: () => new Promise((r) => { resolveWrite = r; }),
        },
      });
      const a = makeBubble('| one |');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      fab.click();
      const b = makeBubble('| two |');
      fireDoc('mouseover', b.table);
      resolveWrite();
      await settle();
      return {
        flash: [...fab._classes].filter((c) => c.startsWith('is-cop')),
        announced: liveRegion.textContent,
      };
    """
    )
    assert out["flash"] == []
    assert out["announced"] == ""


def test_second_copy_during_flash_window_keeps_its_outcome() -> None:
    """A copy started while the previous outcome flash is still showing
    claims the element's outcome slot: the earlier flash's revert timer
    is cancelled at copy start.  Left armed, it fires mid-write, bumps
    the generation, and the settling write paints nothing and announces
    nothing — a silent copy, or worse a silent FAILURE the user reads as
    success.  The second test that waits out the real 1.4s timer."""
    out = _run(
        """
      global.isSecureContext = true;
      let resolveWrite = null;
      let calls = 0;
      setNavigator({
        clipboard: {
          writeText: () => {
            calls += 1;
            if (calls === 1) return Promise.resolve();
            return new Promise((r) => { resolveWrite = r; });
          },
        },
      });
      const a = makeBubble('| a |');
      const btn = buildMsgCopyButton(a.msg);
      a.bar.appendChild(btn);
      a.body._copySource = 'reply';
      a.msg.querySelector = (sel) => (sel === '.msg-body' ? a.body : null);
      btn.click();
      await settle();
      const firstFlash = [...btn._classes];
      btn.click();
      // Outlive the FIRST flash's 1400ms revert while the second write
      // is still in flight — with the timer cancelled, nothing fires.
      await settle(1600);
      const inflightClasses = [...btn._classes];
      liveRegion.textContent = '';
      resolveWrite();
      await settle();
      return {
        firstFlash,
        inflightClasses,
        finalFlash: [...btn._classes],
        announced: liveRegion.textContent,
      };
    """
    )
    assert out["firstFlash"] == ["is-copied"]
    # In flight, the element sits at idle (the new copy cleared the old
    # flash); the orphaning revert never fires.
    assert out["inflightClasses"] == []
    assert out["finalFlash"] == ["is-copied"]
    assert out["announced"] == "Copied"


def test_aborted_reveal_on_sliver_block_does_not_strand_state() -> None:
    """_showFabFor assigns the target refs BEFORE its sliver check; when
    the check aborts a reveal that started with the button hidden, the
    refs must still be cleared — stranded refs make the hover fast-path
    treat the block as already handled, permanently killing its copy
    affordance."""
    out = _run(
        """
      const a = makeBubble('| a |');
      // The block peeks 12px into the viewport: under the 32px sliver
      // threshold, so the reveal aborts while the fab is still hidden.
      a.table._rect = { top: 588, bottom: 700, left: 0, right: 200, width: 200, height: 112 };
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      const hiddenAfterSliver = !fab || !fab.classList.contains('is-visible');
      // The block scrolls fully into view; the SAME interaction must now
      // reveal the button.
      a.table._rect = { top: 100, bottom: 220, left: 0, right: 200, width: 200, height: 120 };
      fireDoc('mouseover', a.table);
      const revealsAfterHover = fabEl().classList.contains('is-visible');
      return { hiddenAfterSliver, revealsAfterHover };
    """
    )
    assert out["hiddenAfterSliver"] is True
    assert out["revealsAfterHover"] is True


def test_retarget_clears_previous_outcome_flash() -> None:
    """The singleton button must never carry one block's ✓/✗ onto another —
    a fresh reveal starts idle, even inside the 1.4s flash window."""
    out = _run(
        """
      const a = makeBubble('| one |');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      fab.click();
      await settle();
      const flashedAfterCopy = fab.classList.contains('is-copied');
      const b = makeBubble('| two |');
      fireDoc('mouseover', b.table);
      return {
        flashedAfterCopy,
        carried: [...fab._classes].filter((c) => c.startsWith('is-cop')),
        title: fab.title,
      };
    """
    )
    assert out["flashedAfterCopy"] is True
    assert out["carried"] == []
    assert out["title"] == "Copy block"


def test_unresolvable_bar_collision_hides_instead_of_stacking() -> None:
    """When the band clamp leaves nowhere below the action bar, the
    floating button must hide rather than paint on top of the bubble's own
    copy button — two stacked identical glyphs make a click that targets
    one silently hit the other."""
    out = _run(
        """
      const a = makeBubble('| a |');
      // The block hugs the bottom of the viewport band while the bar sits
      // just above the only admissible strip: the collision push has no
      // room below the bar.
      a.table._rect = { top: 560, bottom: 700, left: 0, right: 200, width: 200, height: 140 };
      a.bar._rect = { top: 555, bottom: 579, left: 150, right: 200, width: 50, height: 24 };
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      return { visible: !!fab && fab.classList.contains('is-visible') };
    """
    )
    assert out["visible"] is False


def test_failure_flash_orphans_prior_inflight_copy() -> None:
    """A failure flash is a NEW outcome: a still-pending copy from an
    earlier click must not settle afterwards and flip the ✗ (with its
    recovery title) back to an unearned ✓."""
    out = _run(
        """
      global.isSecureContext = true;
      let resolveWrite = null;
      setNavigator({
        clipboard: {
          writeText: () => new Promise((r) => { resolveWrite = r; }),
        },
      });
      const a = makeBubble('| slow |');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      fab.click();
      // The bubble empties (no replacement), so the second click finds a
      // disconnected target and flashes the failure.
      a.body.children.length = 0;
      a.table.isConnected = false;
      a.table.parentNode = null;
      a.table.parentElement = null;
      fab.click();
      const failedShown = fab.classList.contains('is-copy-failed');
      // The first click's slow write now settles successfully.
      resolveWrite(true);
      await settle();
      return {
        failedShown,
        stillFailed: fab.classList.contains('is-copy-failed'),
        notFlippedToCopied: !fab.classList.contains('is-copied'),
        title: fab.title,
      };
    """
    )
    assert out["failedShown"] is True
    assert out["stillFailed"] is True
    assert out["notFlippedToCopied"] is True
    assert out["title"] == "Copy failed — select the text and copy manually"


def test_click_survives_post_reveal_sliver_without_hiding() -> None:
    """A click on a still-connected target must not re-run placement — a
    reflow may have slivered the block since the reveal, and a reposition
    would hide the button while the copy proceeds, making its outcome
    flash invisible.  The click path copies, flashes, and leaves the
    button exactly where the user pressed it."""
    out = _run(
        """
      const a = makeBubble('| here |');
      fireDoc('mouseover', a.table);
      const fab = fabEl();
      // The block slivers AFTER the reveal (late reflow).
      a.table._rect = { top: 588, bottom: 700, left: 0, right: 200, width: 200, height: 112 };
      fab.click();
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        copied: ta ? ta.value : null,
        stillVisible: fab.classList.contains('is-visible'),
        flashed: fab.classList.contains('is-copied'),
      };
    """
    )
    assert out["copied"] == "| here |"
    assert out["stillVisible"] is True
    assert out["flashed"] is True


# ---------------------------------------------------------------------------
# The keyboard path — Enter on a focused block
# ---------------------------------------------------------------------------


def test_enter_on_block_copies_source_and_flashes_the_block() -> None:
    """Enter on a focused block copies that block's source directly: no
    floating button involved (none is created, let alone revealed), the
    outcome flashes on the BLOCK (is-copied), and the live region
    announces it.  Enter reaching the listener from a descendant of the
    block (a focusable child owns its own Enter semantics) and non-Enter
    keys are ignored."""
    out = _run(
        """
      const a = makeBubble('| kbd |\\n|---|\\n| 1 |');
      const cell = makeEl('a');
      a.table.appendChild(cell);
      fireDoc('keydown', cell, { key: 'Enter' });
      fireDoc('keydown', a.table, { key: 'a' });
      await settle();
      const taEarly = createdEls.find((e) => e.tagName === 'TEXTAREA');
      const ignored = {
        madeTextarea: !!taEarly,
        flash: [...a.table._classes].filter((c) => c.startsWith('is-cop')),
      };
      fireDoc('keydown', a.table, { key: 'Enter' });
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        ...ignored,
        copied: ta ? ta.value : null,
        blockFlash: [...a.table._classes].filter((c) => c.startsWith('is-cop')),
        blockTitle: a.table.title,
        announced: liveRegion.textContent,
        fabExists: !!fabEl(),
      };
    """
    )
    assert out["madeTextarea"] is False
    assert out["flash"] == []
    assert out["copied"] == "| kbd |\n|---|\n| 1 |"
    assert out["blockFlash"] == ["is-copied"]
    assert out["blockTitle"] == "Copied"
    assert out["announced"] == "Copied"
    assert out["fabExists"] is False


def test_enter_on_block_refuses_while_busy() -> None:
    """The keyboard path shares the pane-level busy gate: Enter on a
    focused block inside a data-busy="true" container copies nothing but
    ANSWERS — the block flashes the ✗ ring and the live region carries
    the busy-specific explanation, because a silent refusal is
    indistinguishable from a lost keystroke.  The same Enter works once
    the pane is idle."""
    out = _run(
        """
      const a = makeBubble('| kbd |');
      const wrap = makeBusyWrap(a, true);
      fireDoc('keydown', a.table, { key: 'Enter' });
      await settle();
      const taBusy = createdEls.find((e) => e.tagName === 'TEXTAREA');
      const busyState = {
        madeTextarea: !!taBusy,
        flash: [...a.table._classes].filter((c) => c.startsWith('is-cop')),
        announced: liveRegion.textContent,
      };
      wrap.setAttribute('data-busy', 'false');
      fireDoc('keydown', a.table, { key: 'Enter' });
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        ...busyState,
        idleCopied: ta ? ta.value : null,
        idleFlash: [...a.table._classes].filter((c) => c.startsWith('is-cop')),
      };
    """
    )
    assert out["madeTextarea"] is False
    assert out["flash"] == ["is-copy-failed"]
    assert out["announced"] == "Copy is available when the reply finishes"
    assert out["idleCopied"] == "| kbd |"
    assert out["idleFlash"] == ["is-copied"]


def test_enter_chords_and_repeat_do_not_copy() -> None:
    """Modifier-Enter chords belong to the browser / OS, and key repeat is
    never a deliberate copy — a copy overwrites the user's clipboard, so
    only a plain, single Enter fires."""
    out = _run(
        """
      const a = makeBubble('| kbd |');
      for (const props of [
        { key: 'Enter', ctrlKey: true },
        { key: 'Enter', altKey: true },
        { key: 'Enter', metaKey: true },
        { key: 'Enter', shiftKey: true },
        { key: 'Enter', repeat: true },
      ]) fireDoc('keydown', a.table, props);
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      const chordState = {
        madeTextarea: !!ta,
        flash: [...a.table._classes].filter((c) => c.startsWith('is-cop')),
      };
      fireDoc('keydown', a.table, { key: 'Enter' });
      await settle();
      const ta2 = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return { ...chordState, plainCopied: ta2 ? ta2.value : null };
    """
    )
    assert out["madeTextarea"] is False
    assert out["flash"] == []
    assert out["plainCopied"] == "| kbd |"


def test_enter_with_empty_source_copies_empty_string() -> None:
    """A focused block whose source resolves empty (a mermaid container
    stripped of its stash) still attempts the write: the block flashes the
    transport's verdict — never a manufactured failure whose "select the
    text" hint points at a block with nothing to select."""
    out = _run(
        """
      const a = makeBubble('| t |');
      const mermaid = makeEl('div');
      mermaid._classes.add('mermaid-container');
      mermaid.setAttribute('tabindex', '0');
      a.body.appendChild(mermaid);
      fireDoc('keydown', mermaid, { key: 'Enter' });
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        madeTextarea: !!ta,
        taValue: ta ? ta.value : null,
        flash: [...mermaid._classes].filter((c) => c.startsWith('is-cop')),
        announced: liveRegion.textContent,
      };
    """
    )
    assert out["madeTextarea"] is True
    assert out["taValue"] == ""
    assert out["flash"] == ["is-copied"]
    assert out["announced"] == "Copied"


def test_block_flash_reverts_clean_after_flash_ms() -> None:
    """The outcome flash on a keyboard-copied BLOCK is transient: after
    FLASH_MS the state class reverts and the title carries no residue — a
    block (which unlike the buttons has no idle tooltip of its own)
    returns to an empty title, never a literal "undefined".  The one test
    that waits out the real 1.4s revert timer."""
    out = _run(
        """
      const a = makeBubble('| kbd |');
      fireDoc('keydown', a.table, { key: 'Enter' });
      await settle();
      const flashTitle = a.table.title;
      const flashed = [...a.table._classes].filter((c) => c.startsWith('is-cop'));
      await settle(1500);
      return {
        flashed,
        flashTitle,
        idleClasses: [...a.table._classes].filter((c) => c.startsWith('is-cop')),
        idleTitle: a.table.title,
      };
    """
    )
    assert out["flashed"] == ["is-copied"]
    assert out["flashTitle"] == "Copied"
    assert out["idleClasses"] == []
    assert out["idleTitle"] == ""


def test_enter_outside_msg_body_is_ignored() -> None:
    """The keyboard path is scoped to blocks inside rendered chat bodies —
    a focusable pre on some other surface (admin panes, previews) keeps
    its own Enter semantics."""
    out = _run(
        """
      const stray = makeEl('pre');
      stray.setAttribute('tabindex', '0');
      stray.textContent = 'not transcript content';
      document.body.appendChild(stray);
      fireDoc('keydown', stray, { key: 'Enter' });
      await settle();
      const ta = createdEls.find((e) => e.tagName === 'TEXTAREA');
      return {
        madeTextarea: !!ta,
        flash: [...stray._classes].filter((c) => c.startsWith('is-cop')),
      };
    """
    )
    assert out["madeTextarea"] is False
    assert out["flash"] == []
