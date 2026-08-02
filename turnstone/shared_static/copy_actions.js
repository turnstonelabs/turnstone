/* copy_actions.js — copy-to-clipboard affordances for rendered chat content.

   Three affordances share this module, and every one is IDLE-ONLY: while
   a turn is in flight (the chat clients stamp data-busy="true" on their
   messages container) nothing here copies.  A busy transcript is mutating
   under the affordance — streaming replaces bubble bodies per rAF tick,
   and a mid-stream whole-message copy would land a silent prefix under a
   success flash — so the gate is one pane-level fact, enforced in JS at
   every click/activation path (the chat.css grey-out alone would be
   keyboard-bypassable) and reflected visually by chat.css.

   * Bubble copy: buildMsgCopyButton() builds the per-message copy button
     the chat clients mount in their ``.msg-actions`` bars.

   * Block copy, pointer: one floating button (``.block-copy-btn``) that
     appears over the hovered markdown block — code fence, mermaid
     diagram, or table — and copies that block's SOURCE.  Pointer-only:
     pointer entering the block reveals it; pointer leaving it, or any
     scroll in its context, hides it, unconditionally.  It sits out of
     the tab order (tabindex -1) and never carries keyboard state.
     Nothing is ever injected into rendered block DOM: streamingRender
     replaces bubble innerHTML wholesale on every rAF tick, and
     postRenderHljs caches code innerHTML by source, so an embedded
     button would be wiped per tick or captured into the cache.  Touch
     has no hover: block copy is desktop-first, and touch keeps the
     always-visible bubble copy.

   * Block copy, keyboard: Enter on a FOCUSED block (pre, .table-wrap and
     .mermaid-container all carry tabindex=0) copies that block directly
     — the floating button is never involved.  The outcome flashes on the
     block itself (is-copied / is-copy-failed, chat.css) and the live
     region announces it.

   Copy resolves to SOURCE, not rendered text — pasting a lifted snippet
   into an editor or another chat must keep the fences and pipes:

   * whole message — the raw markdown the streaming pipeline stashes on
     ``.msg-body`` (``_copySource``, renderer.js _streamingRenderApply);
   * code fence — ``code.textContent`` (escapeHtml round-trips, and hljs
     wraps tokens in spans without changing text content);
   * mermaid — the container's ``data-mermaid-source`` (the autoquoted
     source the diagram was actually rendered from);
   * table — the ``data-md-source`` the table pass stashes at render time
     (pipes and alignment markers are unrecoverable from rendered cells).

   ES module; renderer.js imports it for side effects so the affordance
   lands on every surface that renders markdown.  Window bridge at the
   bottom for the still-classic consumers (coordinator.js). */

import { copyTextToClipboard, makeAnnouncer } from "./utils.js";

const FLASH_MS = 1400;
const FAIL_TEXT = "Copy failed — select the text and copy manually";
const BUSY_TEXT = "Copy is available when the reply finishes";

// Shared polite live region announcing copy outcomes to screen readers —
// the visual outcome flash is not announced on its own.  (Eager region
// creation and the clear-then-set idiom live in makeAnnouncer.)
const _announce = makeAnnouncer();

// The pane-level busy gate.  Both chat clients maintain data-busy on
// their messages container for the whole turn (interactive Pane.setBusy,
// coordinator setBusy), so any node inside a busy transcript resolves the
// same one fact.
function _isBusy(el) {
  return !!el.closest('[data-busy="true"]');
}

// Return a flashed element — a copy button, or the block a keyboard copy
// targeted — to its idle presentation: outcome classes off, idle title
// back, any pending revert timer cancelled, and any in-flight copy
// orphaned (its settle must not paint an outcome the element no longer
// owns).  Shared by the flash revert and the floating button's re-target
// reset so the two cannot drift.
function _clearFlash(el) {
  el._copyGen = (el._copyGen || 0) + 1;
  if (el._copyFlashTimer) clearTimeout(el._copyFlashTimer);
  el._copyFlashTimer = 0;
  el.classList.remove("is-copied", "is-copy-failed");
  // Buttons restore their idle tooltip; flashed BLOCKS (the keyboard
  // copy path) have none — restore to empty rather than stamping a
  // literal "undefined".
  el.title = el._copyIdleTitle || "";
}

// Flash an outcome: the state class swaps a button's icon to a check /
// cross (or tints a keyboard-copied block), the title carries the
// plain-language explanation, the live region announces the same
// string, and everything reverts after FLASH_MS.  ✓/✗ + prose only — no
// partial states, and deliberately no toast or other notification
// chrome: the outcome surfaces identically on every page, at the
// element the user acted on.
function _flashOutcome(el, ok, text) {
  // Every flash is a NEW outcome: bump the generation so any still-in-
  // flight copy on this element is orphaned — a slow success settling
  // after a later failure must not flip the ✗ (and its recovery title)
  // back to a ✓ the second interaction never earned.
  el._copyGen = (el._copyGen || 0) + 1;
  el.classList.remove("is-copied", "is-copy-failed");
  el.classList.add(ok ? "is-copied" : "is-copy-failed");
  el.title = text;
  _announce(text);
  if (el._copyFlashTimer) clearTimeout(el._copyFlashTimer);
  el._copyFlashTimer = setTimeout(function () {
    _clearFlash(el);
  }, FLASH_MS);
}
function _flashCopyResult(el, ok) {
  _flashOutcome(el, ok, ok ? "Copied" : FAIL_TEXT);
}
// A busy refusal is an OUTCOME, not a silent no-op: every activation
// path answers, or a keyboard user cannot distinguish "refused because
// the reply is streaming" from "keystroke lost".  FAIL_TEXT's manual-
// copy recovery would be misleading here — the content is still being
// written — so the refusal carries its own explanation.
function _flashBusyRefusal(el) {
  _flashOutcome(el, false, BUSY_TEXT);
}

function _copyAndFlash(el, text) {
  // The write is attempted for EVERY resolved source, empty included: a
  // legitimately empty block copies the empty string, and the flash
  // reports the transport's verdict — a manufactured failure (with its
  // "select the text" recovery hint) on a block that has nothing to
  // select would be a false error.
  //
  // A new copy claims the element's outcome slot outright: _clearFlash
  // cancels a still-armed revert timer from the PREVIOUS flash — left
  // running, it would fire mid-write, bump the generation, and orphan
  // this copy (clipboard written, zero feedback) — and returns the
  // element to idle while the write is in flight.
  _clearFlash(el);
  // Generation-stamped: the clipboard write settles asynchronously, and a
  // re-target in that gap must orphan this copy's outcome — otherwise the
  // ✓ (and its announcement) lands on a block the user never copied.
  const gen = (el._copyGen = (el._copyGen || 0) + 1);
  copyTextToClipboard(text).then(function (ok) {
    if (el._copyGen === gen) _flashCopyResult(el, ok);
  });
}

// The ASSISTANT-bubble action-bar adders (copy/retry/TTS in both chat
// clients) share this pair so their ARIA contract cannot drift between
// bubbles.  (The user-bubble adders predate it and still build their bars
// in place.)  The bar is always a DIRECT child of .msg, so the finder
// scans children instead of the whole rendered subtree (a bubble holding
// a large table is thousands of nodes).
export function findMsgActionsBar(el) {
  for (const c of el.children) {
    if (c.classList.contains("msg-actions")) return c;
  }
  return null;
}

export function ensureMsgActionsBar(el) {
  let bar = findMsgActionsBar(el);
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Message actions");
    el.appendChild(bar);
  }
  return bar;
}

// Resolve the copyable source for a rendered markdown block.
export function blockCopySource(el) {
  if (!el) return "";
  if (el.classList.contains("mermaid-container")) {
    return el.getAttribute("data-mermaid-source") || "";
  }
  if (el.classList.contains("table-wrap")) {
    return el.getAttribute("data-md-source") || "";
  }
  const code = el.querySelector("code");
  return (code || el).textContent || "";
}

// Resolve the copyable source for a whole message bubble.  _copySource is
// the render pipeline's unconditional stash — set for every applied frame,
// including ones whose markdown render threw and painted as plain text.
// The contract is WHOLE-SOURCE: the clipboard carries exactly what the
// model wrote, including syntax the renderer does not display (comment
// constructs, over-wide table cells, reference definitions) — what is
// SHOWN is the render's decision, what is COPIED is the source.  Falls
// back to the rendered text only for a body that never went through the
// render pipeline at all: the visible text, an honest degrade.
export function msgCopySource(msgEl) {
  const body = msgEl && msgEl.querySelector(".msg-body");
  if (!body) return "";
  return body._copySource != null ? body._copySource : body.textContent || "";
}

// Glyph span for an action button.  aria-hidden: the button's accessible
// name lives in its aria-label — the icon is decoration.
function _icon(cls) {
  const icon = document.createElement("span");
  icon.className = cls;
  icon.setAttribute("aria-hidden", "true");
  return icon;
}

// Build the transient retry button both chat clients prepend to the last
// assistant bubble's bar.  Lives here so the button's chrome — the
// load-bearing ``msg-retry-btn`` class (both teardown sweeps select on
// it), title and accessible name — cannot drift between clients.
export function buildMsgRetryButton(onRetry) {
  const btn = document.createElement("button");
  btn.className = "msg-action-btn msg-retry-btn";
  btn.title = "Retry (regenerate response)";
  btn.setAttribute("aria-label", "Retry last response");
  btn.appendChild(_icon("icon-retry"));
  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    onRetry();
  });
  return btn;
}

// Build the per-message copy button for a ``.msg-actions`` bar.  The
// source resolves at CLICK time, so a button attached when the bubble is
// created copies the final streamed content, not a creation-time snapshot.
export function buildMsgCopyButton(msgEl) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "msg-action-btn msg-copy-btn";
  btn.title = "Copy message";
  btn._copyIdleTitle = "Copy message";
  btn.setAttribute("aria-label", "Copy message");
  btn.appendChild(_icon("icon-copy"));
  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    // The busy refusal runs HERE, not just in chat.css: pointer-events
    // cannot stop Enter on a button that already holds focus — and it
    // answers (✗ + announce) rather than silently dropping a keystroke
    // on a button assistive tech presents as enabled.
    if (_isBusy(msgEl)) {
      _flashBusyRefusal(btn);
      return;
    }
    _copyAndFlash(btn, msgCopySource(msgEl));
  });
  return btn;
}

// ---------------------------------------------------------------------------
//  Block copy — pointer path (floating button) + keyboard path (Enter)
// ---------------------------------------------------------------------------
const BLOCK_SELECTOR = "pre, .mermaid-container, .table-wrap";

// The button's box for the placement clamps below.  The CSS is the source
// (chat.css .block-copy-btn width/height); these are its pinned mirror —
// a test asserts the two stay equal, so a CSS resize cannot silently
// desync the clamps.  FAB_GAP is placement-only and has no CSS twin.
const FAB_W = 28;
const FAB_H = 24;
const FAB_GAP = 4;

let _fab = null;
// The ONE module-level DOM ref.  Everything else placement needs — the
// owning bubble, its scroller, the action bar — is derived from the
// target at use time, so hide has exactly one ref to drop and a wiped
// transcript leaves nothing else to strand.
let _fabTarget = null;

// A block nested inside another block is a rendering detail, not a copy
// target: the mermaid error state draws its message + source as a <pre>
// INSIDE the .mermaid-container, and copying that pre would lift the
// error prose instead of the diagram source.
function _isNestedBlock(el) {
  return !!(el.parentElement && el.parentElement.closest(BLOCK_SELECTOR));
}

// Outermost copyable block for an event target: a hit on a nested block
// lifts to its containing block, so the pointer and keyboard paths agree
// on one copy target per rendered block.
function _liftToOuterBlock(el) {
  let block = el;
  while (block && _isNestedBlock(block)) {
    block = block.parentElement.closest(BLOCK_SELECTOR);
  }
  return block;
}

function _ensureFab() {
  if (_fab) return _fab;
  _fab = document.createElement("button");
  _fab.type = "button";
  _fab.className = "block-copy-btn";
  _fab.title = "Copy block";
  _fab._copyIdleTitle = "Copy block";
  _fab.setAttribute("aria-label", "Copy block");
  // Pointer-only: out of the tab order entirely.  Keyboard users copy
  // with Enter on the focused block (the delegated keydown below), so a
  // tab stop here would only add a hover-dependent phantom to the page's
  // focus order.
  _fab.setAttribute("tabindex", "-1");
  _fab.appendChild(_icon("icon-copy"));
  _fab.addEventListener("click", function (e) {
    e.stopPropagation();
    // Reveal is idle-gated, so the DOM under a visible button is stable;
    // what remains is a transcript wipe under the pointer (disconnected
    // target → the generic ✗) or a turn starting between reveal and
    // click (busy → its own refusal message).  Both answer — never a
    // silent no-op on a button that looks live, and never a hide before
    // the outcome lands.
    const target = _fabTarget;
    if (!target || !target.isConnected) {
      _flashCopyResult(_fab, false);
      return;
    }
    if (_isBusy(target)) {
      _flashBusyRefusal(_fab);
      return;
    }
    _copyAndFlash(_fab, blockCopySource(target));
  });
  document.body.appendChild(_fab);
  return _fab;
}

// Hide is stateless and unconditional: the pointer path carries no focus
// or keyboard state to preserve, so hiding is the class off plus dropping
// the target ref — a module-level ref must not pin a wiped transcript's
// detached bubble.  Runs for every dismissal (pointer exit, scroll,
// window leave) and for the placement aborts in _showFabFor, so a reveal
// aborted while the button was still hidden cannot strand a stale ref
// that would dead-end the show fast-path for that block.
function _hideFab() {
  if (!_fab) return;
  _fab.classList.remove("is-visible");
  _fabTarget = null;
}

// Nearest scrolling ancestor — the band the button must stay inside so it
// hugs the visible part of a tall block instead of pinning to the viewport
// (where it detaches from its block and lands over pane chrome).
// Memoized per bubble (WeakMap, so a wiped bubble is still collectable):
// the walk reads computed styles per ancestor, and the hide/re-show churn
// of an ordinary prose↔block pointer sweep would otherwise re-pay it on
// every crossing.
const _scrollerMemo = new WeakMap();
function _scrollerOf(el) {
  let sc = el.parentElement;
  while (sc && sc !== document.body) {
    const o = getComputedStyle(sc).overflowY;
    if (o === "auto" || o === "scroll" || o === "overlay") return sc;
    sc = sc.parentElement;
  }
  return null;
}
function _scrollerFor(host) {
  let sc = _scrollerMemo.get(host);
  if (sc === undefined) {
    sc = _scrollerOf(host);
    _scrollerMemo.set(host, sc);
  }
  return sc;
}

function _showFabFor(target) {
  const fab = _ensureFab();
  if (target !== _fabTarget) {
    // A new target never inherits the previous one's outcome flash — a ✓
    // carried across blocks would assert a copy the user never made there.
    _clearFlash(fab);
    _fabTarget = target;
  }
  const host = target.closest(".msg") || document.body;
  const scroller = _scrollerFor(host);
  const rect = target.getBoundingClientRect();
  const band = scroller
    ? scroller.getBoundingClientRect()
    : { top: 0, bottom: window.innerHeight, left: 0, right: window.innerWidth };
  // The button lives in the visible intersection of block and scroller; a
  // sliver under button height means there is nothing sensible to anchor
  // to, so hide instead of hovering over unrelated chrome.
  const visTop = Math.max(rect.top, band.top);
  const visBottom = Math.min(rect.bottom, band.bottom);
  if (visBottom - visTop < FAB_H + 2 * FAB_GAP) {
    _hideFab();
    return;
  }
  let top = Math.min(visTop + FAB_GAP, visBottom - FAB_H - FAB_GAP);
  let left = Math.max(rect.right - FAB_W - FAB_GAP, rect.left + FAB_GAP);
  left = Math.min(
    left,
    window.innerWidth - FAB_W - 2 * FAB_GAP,
    band.right - FAB_W - 2 * FAB_GAP,
  );
  left = Math.max(left, band.left + FAB_GAP);
  // A block that opens the bubble puts this button on top of the bubble's
  // own hover-revealed copy button (same glyph, different scope) — drop
  // below the action bar when the two would collide.  Looked up live (a
  // few direct children), never cached: the retry/TTS holder grows the
  // bar after the fact.  When the band clamp leaves nowhere below the
  // bar, hide instead: two stacked identical copy glyphs make a click
  // that targets one silently hit the other.
  const bar = host === document.body ? null : findMsgActionsBar(host);
  if (bar) {
    const br = bar.getBoundingClientRect();
    if (
      br.width > 0 &&
      top < br.bottom + FAB_GAP &&
      top + FAB_H > br.top &&
      left < br.right + FAB_GAP &&
      left + FAB_W > br.left
    ) {
      top = Math.min(br.bottom + FAB_GAP, visBottom - FAB_H - FAB_GAP);
      if (top < br.bottom + FAB_GAP && top + FAB_H > br.top) {
        _hideFab();
        return;
      }
    }
  }
  fab.style.top = top + "px";
  fab.style.left = left + "px";
  fab.classList.add("is-visible");
}

// Delegated hover: reveal over the hovered block, hide otherwise.  Scoped
// to blocks inside rendered chat bodies (``.msg-body``) — other surfaces
// (composer previews, admin panes) keep their own affordances — and gated
// on the pane being idle: the button never REVEALS while a turn is in
// flight, and a pointer move over a busy transcript hides any reveal a
// just-started turn overtook.
document.addEventListener("mouseover", function (e) {
  const t = e.target;
  if (!(t instanceof Element)) return;
  if (_fab && _fab.contains(t)) return; // hovering the button itself
  // Fast path: the pointer moving WITHIN the targeted block is the
  // dominant case (hljs wraps every token in a span, so a sweep across a
  // fence is hundreds of events) — a containment check plus the busy
  // gate, no selector walks.  The busy check rides the fast path too: a
  // turn starting under a shown button dismisses it on the NEXT pointer
  // move, not only once the pointer leaves the block.
  if (_fabTarget && _fabTarget.contains(t)) {
    if (_isBusy(_fabTarget)) _hideFab();
    return;
  }
  // The clipboard fallback's off-screen textarea (utils.js) must not
  // count as "pointer left the block" — copying would dismiss its own
  // button mid-copy.  It is childless, so it is always the event target
  // itself: a plain attribute check, no ancestor walk.
  if (t.hasAttribute("data-clipboard-shim")) return;
  const block = _liftToOuterBlock(t.closest(BLOCK_SELECTOR));
  if (block && block.closest(".msg-body") && !_isBusy(block)) {
    if (block !== _fabTarget) _showFabFor(block);
  } else {
    _hideFab();
  }
});

// A scroll that MOVES the block out from under the fixed-position button
// — the document itself, or any scrollable ancestor holding the block —
// hides it rather than chasing (the pointer re-reveals in place).  A
// scroll INSIDE the block (a fence's internal horizontal pan, a nested
// scrollable) pans content within it and keeps the button, and a
// scroller that does not contain the block (a sidebar) moves nothing the
// button is anchored to.  Containment is the whole rule — no scroller
// identity to memoize or get wrong.
document.addEventListener(
  "scroll",
  function (e) {
    if (!_fab || !_fab.classList.contains("is-visible")) return;
    const movesBlock =
      !_fabTarget ||
      e.target === document ||
      (e.target instanceof Element &&
        e.target !== _fabTarget &&
        e.target.contains(_fabTarget));
    if (movesBlock) _hideFab();
  },
  true,
);

// Pointer leaving the window would otherwise strand the button painted
// over the page with nothing left to dismiss it.  The listener sits on
// documentElement rather than document: engines deliver the leave event
// to the <html> element reliably, while document-level delivery on
// window exit is historically flaky.
document.documentElement.addEventListener("mouseleave", function () {
  _hideFab();
});

// Keyboard path: Enter on a focused block copies it directly — the
// floating button is never involved.  Only the block ITSELF as the event
// target counts (a focusable descendant, e.g. a link in a table cell,
// keeps its own Enter semantics), a hit on a nested block lifts to its
// container, and the busy gate applies like every other copy path.  The
// outcome flashes on the block (is-copied / is-copy-failed, chat.css)
// and the live region announces it.
document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter") return;
  // Chords belong to the browser / OS (Ctrl+Enter, Alt+Enter…) and key
  // repeat is never a deliberate copy — a copy overwrites the user's
  // clipboard, so only a plain, single Enter qualifies.
  if (e.repeat || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
  const t = e.target;
  if (!(t instanceof Element)) return;
  if (t.closest(BLOCK_SELECTOR) !== t) return;
  const block = _liftToOuterBlock(t);
  if (!block || !block.closest(".msg-body")) return;
  if (_isBusy(block)) {
    _flashBusyRefusal(block);
    return;
  }
  _copyAndFlash(block, blockCopySource(block));
});

// --- Legacy window bridge ---------------------------------------------------
// coordinator.js (still classic) reaches these as globals at message-append
// time, well after this deferred module evaluated.  Only its consumers are
// bridged; module code imports instead.
Object.assign(window, {
  buildMsgCopyButton,
  buildMsgRetryButton,
  ensureMsgActionsBar,
});
