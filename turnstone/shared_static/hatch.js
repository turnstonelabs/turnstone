/* Service-hatch containers — behaviour for the two mounting points styled by
 * hatch.css:
 *
 *  - openShelf(dlg, opts): the pane-scoped, NON-modal create/edit/inspect
 *    shelf (`dialog.show()`).  The dialog must live inside its pane's
 *    `.hatch-host` element: the host is the containing block, gets a lazy
 *    `.pane-scrim` sibling, and its OTHER children are made `inert` while
 *    the shelf is open — the rail, tab bar and any other pane stay live
 *    (split panes work by construction; the shelf persists with its pane).
 *    Escape is controller-owned (non-modal dialogs have no native cancel)
 *    and defers to any document-modal dialog stacked above.
 *
 *  - openDialog(dlg, opts): the document-modal confirm / show-once tier
 *    (`showModal()`).  Native top layer, focus trap, Escape; backdrop click
 *    closes (geometry test — the dialog is the click target only outside
 *    its box).
 *
 *  Both: `[data-close]` descendants close the container; `setBusy(dlg, on)`
 *  toggles the `data-busy` lock (LED pulses, actions lock, dismissal is
 *  refused).  Focus returns to `opts.opener` (default: the element focused
 *  at open time).  `opts.onClose` fires exactly once per open.
 *
 *  Classic (non-module) scripts reach these via the `window.TurnstoneHatch`
 *  bridge at the bottom — only ever from event handlers, never at parse
 *  time (module execution is deferred; the bridge does not exist yet while
 *  a classic script's top level runs).
 */

"use strict";

/** dialog -> open-state for shelves; presence means "open". */
const _shelfState = new Map();
let _escInstalled = false;

function _hostOf(dlg) {
  const host = dlg.closest(".hatch-host") || dlg.parentElement;
  if (!host) throw new Error("hatch: shelf dialog has no host element");
  return host;
}

function _scrimFor(host) {
  let scrim = null;
  for (const el of host.children) {
    if (el.classList && el.classList.contains("pane-scrim")) {
      scrim = el;
      break;
    }
  }
  if (!scrim) {
    scrim = document.createElement("div");
    scrim.className = "pane-scrim";
    scrim.hidden = true;
    scrim.addEventListener("click", () => {
      for (const [dlg] of _shelfState) {
        if (dlg.hasAttribute("data-busy")) continue; // submit in flight
        if (_hostOf(dlg) === host) closeShelf(dlg);
      }
    });
    host.appendChild(scrim);
  }
  return scrim;
}

function _pruneDetached() {
  // A pane can be closed (PaneManager removes its element) while its shelf
  // is open — the entry would otherwise pin a detached dialog forever and
  // leave the document Escape listener targeting a ghost.
  for (const [dlg] of _shelfState) {
    if (!dlg.isConnected) closeShelf(dlg);
  }
}

function _onEscape(e) {
  if (e.key !== "Escape") return;
  // A document-modal dialog above (confirm-from-shelf) owns Escape natively.
  if (document.querySelector("dialog:modal")) return;
  _pruneDetached();
  let top = null;
  for (const [dlg] of _shelfState) top = dlg; // Map preserves insertion order
  if (!top) return;
  if (top.hasAttribute("data-busy")) return; // submit in flight — hold the door
  e.preventDefault();
  closeShelf(top);
}

function _wireCloseDelegation(dlg, isShelf) {
  if (dlg._hatchWired) return;
  dlg._hatchWired = true;
  // Busy is a HARD lock. pointer-events:none only stops the mouse — Enter on
  // the still-focused primary dispatches a synthetic click that would reach
  // the surface's submit handler and double-fire the request. Swallow every
  // non-[data-close] activation at capture before surface listeners see it
  // (and [data-close] is refused below anyway).
  dlg.addEventListener(
    "click",
    (e) => {
      if (dlg.hasAttribute("data-busy")) {
        e.stopPropagation();
        e.preventDefault();
      }
    },
    { capture: true },
  );
  // The dialog tier's native Escape arrives as `cancel` — hold that door too.
  if (!isShelf) {
    dlg.addEventListener("cancel", (e) => {
      if (dlg.hasAttribute("data-busy")) e.preventDefault();
    });
  }
  dlg.addEventListener("click", (e) => {
    if (dlg.hasAttribute("data-busy")) return;
    if (e.target.closest("[data-close]")) {
      isShelf ? closeShelf(dlg) : dlg.close();
      return;
    }
    if (!isShelf && e.target === dlg) {
      // Modal tier backdrop click: outside the box, the dialog is the target.
      const r = dlg.getBoundingClientRect();
      const inside =
        e.clientX >= r.left &&
        e.clientX <= r.right &&
        e.clientY >= r.top &&
        e.clientY <= r.bottom;
      if (!inside) dlg.close();
    }
  });
}

/**
 * Open a pane-scoped shelf.  `opts`:
 *   - opener:  focus target on close (default: document.activeElement now).
 *   - onClose: notification, fires exactly once.
 * Returns `{ close }`; `close()` is idempotent.
 */
export function openShelf(dlg, opts) {
  opts = opts || {};
  _pruneDetached();
  if (_shelfState.has(dlg)) return { close: () => closeShelf(dlg) };
  const host = _hostOf(dlg);
  // One shelf per pane: a second open() retargets the pane's shelf slot.
  for (const [other] of _shelfState) {
    if (other !== dlg && _hostOf(other) === host) closeShelf(other);
  }
  const scrim = _scrimFor(host);
  const inerted = [];
  for (const el of host.children) {
    if (el === dlg || el === scrim) continue;
    if (el.tagName === "DIALOG" && el.classList.contains("hatch")) continue;
    if (el.inert) continue; // already inert by someone else — leave it be
    el.inert = true;
    inerted.push(el);
  }
  _shelfState.set(dlg, {
    opener: opts.opener || document.activeElement,
    onClose: opts.onClose || null,
    inerted,
    scrim,
  });
  scrim.hidden = false;
  _wireCloseDelegation(dlg, true);
  dlg.show();
  const auto = dlg.querySelector("[autofocus]");
  if (auto) auto.focus();
  if (!_escInstalled) {
    document.addEventListener("keydown", _onEscape);
    _escInstalled = true;
  }
  return { close: () => closeShelf(dlg) };
}

/** Close a shelf opened by openShelf. Idempotent. */
export function closeShelf(dlg) {
  const state = _shelfState.get(dlg);
  if (!state) return;
  _shelfState.delete(dlg);
  if (dlg.isConnected && dlg.open) dlg.close();
  dlg.removeAttribute("data-busy");
  for (const el of state.inerted) el.inert = false;
  // Another shelf may still own this pane's scrim (retarget race) — only
  // hide it when no open shelf shares the host.
  let hostStillBusy = false;
  for (const [other] of _shelfState) {
    if (state.scrim.parentElement === _hostOf(other)) hostStillBusy = true;
  }
  if (!hostStillBusy) state.scrim.hidden = true;
  if (_shelfState.size === 0 && _escInstalled) {
    document.removeEventListener("keydown", _onEscape);
    _escInstalled = false;
  }
  if (state.opener && state.opener.isConnected) state.opener.focus();
  if (state.onClose) {
    const cb = state.onClose;
    state.onClose = null;
    cb();
  }
}

/**
 * Open a document-modal dialog (confirm / show-once tier).  Same opts as
 * openShelf.  Close via `[data-close]`, Escape (native), backdrop click,
 * or the returned `close()`.
 */
export function openDialog(dlg, opts) {
  opts = opts || {};
  if (dlg.open) return { close: () => dlg.close() };
  const opener = opts.opener || document.activeElement;
  const onClose = opts.onClose || null;
  _wireCloseDelegation(dlg, false);
  dlg.addEventListener(
    "close",
    () => {
      dlg.removeAttribute("data-busy");
      if (opener && opener.isConnected) opener.focus();
      if (onClose) onClose();
    },
    { once: true },
  );
  dlg.showModal();
  const auto = dlg.querySelector("[autofocus]");
  if (auto) auto.focus();
  return { close: () => dlg.close() };
}

/** Busy lock: LED pulses, actions lock, Escape/scrim/[data-close] refused. */
export function setBusy(dlg, busy) {
  if (busy) {
    dlg.setAttribute("data-busy", "");
    dlg.setAttribute("aria-busy", "true");
  } else {
    dlg.removeAttribute("data-busy");
    dlg.removeAttribute("aria-busy");
  }
}

/* Transitional bridge for the classic admin/governance scripts (the
 * toast.js `window.showToast` pattern).  Handler-time use only. */
window.TurnstoneHatch = { openShelf, closeShelf, openDialog, setBusy };
