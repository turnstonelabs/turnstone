/* Shared viewer-local transcript presentation preference.

   This module owns only browser presentation state.  It does not fetch,
   mutate history, or write Turnstone settings.  Both conversational panes
   register their message scroller; the L-shell and standalone coordinator
   mount the same two-state control. */

import { setConvBatchExpanded } from "./conversation.js";

const STORAGE_KEY = "turnstone_interface.transcript_presentation";
const MODE_ATTRIBUTE = "data-transcript-presentation";
const TRANSCRIPT_ROOT_ATTRIBUTE = "data-transcript-root";
const BOTTOM_THRESHOLD_PX = 48;
const controls = new Set();
const scrollers = new Map();

function normalizeMode(value) {
  return value === "compact" ? "compact" : "default";
}

function readStoredMode() {
  try {
    return normalizeMode(localStorage.getItem(STORAGE_KEY));
  } catch (_) {
    return "default";
  }
}

let currentMode = readStoredMode();

function applyRootMode(mode) {
  const root = document.documentElement;
  if (mode === "compact") root.setAttribute(MODE_ATTRIBUTE, "compact");
  else root.removeAttribute(MODE_ATTRIBUTE);
}

function syncControl(button) {
  const compact = currentMode === "compact";
  button.setAttribute("aria-pressed", compact ? "true" : "false");
  button.title = compact
    ? "Switch to default ledger presentation"
    : "Switch to compact ledger presentation";
  button.classList.toggle("is-compact", compact);
}

function syncControls() {
  for (const button of controls) {
    if (!button.isConnected) {
      controls.delete(button);
      continue;
    }
    syncControl(button);
  }
}

function isVisibleScroller(scroller) {
  if (!scroller || !scroller.isConnected) return false;
  if (typeof scroller.getClientRects !== "function") return true;
  return scroller.getClientRects().length > 0;
}

function scrollerIsAtBottom(scroller) {
  const distance =
    scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  return distance <= BOTTOM_THRESHOLD_PX;
}

function releaseScroller(scroller, state, removeMarker) {
  if (scrollers.get(scroller) !== state) return;
  scrollers.delete(scroller);
  scroller.removeEventListener("scroll", state.onScroll);
  if (state.resizeObserver) state.resizeObserver.disconnect();
  if (removeMarker) scroller.removeAttribute(TRANSCRIPT_ROOT_ATTRIBUTE);
}

function refreshScrollerFollowState(state) {
  const scroller = state.scroller;
  if (!isVisibleScroller(scroller)) return;
  if (state.pendingBottomRestoreRevision != null) {
    const restoreRevision = state.pendingBottomRestoreRevision;
    state.pendingBottomRestoreRevision = null;
    if (restoreRevision !== state.scrollRevision) {
      state.atBottom = scrollerIsAtBottom(scroller);
      return;
    }
    scroller.scrollTop = scroller.scrollHeight;
    state.atBottom = true;
    return;
  }
  state.atBottom = scrollerIsAtBottom(scroller);
}

function isPlaying(media) {
  return media.paused === false && media.ended !== true;
}

function batchHead(batch) {
  return Array.from(batch.children || []).find((child) =>
    child.classList.contains("conv-batch-head"),
  );
}

function focusBatchHead(batch) {
  const head = batchHead(batch);
  if (!head || typeof head.focus !== "function") return;
  const temporary = !head.hasAttribute("tabindex");
  if (temporary) head.setAttribute("tabindex", "-1");
  head.focus({ preventScroll: true });
  if (temporary) {
    head.addEventListener(
      "blur",
      () => {
        head.removeAttribute("tabindex");
      },
      { once: true },
    );
  }
}

function prepareFocusForMode(mode) {
  const active = document.activeElement;
  if (mode === "default" && active) {
    const disclosure = active.closest
      ? active.closest(".conv-batch-disclosure")
      : null;
    const batch = disclosure ? disclosure.closest(".conv-batch") : null;
    if (batch) focusBatchHead(batch);
    return;
  }
  if (mode !== "compact") return;

  for (const scroller of scrollers.keys()) {
    if (!scroller.isConnected) continue;
    if (active && scroller.contains(active)) {
      const batch = active.closest ? active.closest(".conv-batch") : null;
      const head = batch ? batchHead(batch) : null;
      if (batch && (!head || !head.contains(active))) {
        setConvBatchExpanded(batch, true);
      }
    }
    for (const media of scroller.querySelectorAll("audio, video")) {
      if (!isPlaying(media)) continue;
      const batch = media.closest(".conv-batch");
      if (batch) setConvBatchExpanded(batch, true);
    }
  }
}

function captureScrollers() {
  const snapshots = [];
  for (const [scroller, state] of scrollers) {
    if (!scroller.isConnected) {
      releaseScroller(scroller, state, false);
      continue;
    }
    if (isVisibleScroller(scroller)) {
      state.atBottom = scrollerIsAtBottom(scroller);
    }
    snapshots.push({
      scroller,
      state,
      atBottom: state.atBottom,
      scrollRevision: state.scrollRevision,
    });
  }
  return snapshots;
}

function restoreBottomPins(snapshots) {
  const schedule =
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame
      : (callback) => callback();
  schedule(() => {
    for (const snapshot of snapshots) {
      if (
        !snapshot.atBottom ||
        !snapshot.scroller.isConnected ||
        scrollers.get(snapshot.scroller) !== snapshot.state ||
        snapshot.state.scrollRevision !== snapshot.scrollRevision
      ) {
        continue;
      }
      if (!isVisibleScroller(snapshot.scroller)) {
        snapshot.state.pendingBottomRestoreRevision = snapshot.scrollRevision;
        continue;
      }
      snapshot.state.pendingBottomRestoreRevision = null;
      snapshot.scroller.scrollTop = snapshot.scroller.scrollHeight;
      snapshot.state.atBottom = true;
    }
  });
}

applyRootMode(currentMode);

export function getTranscriptPresentation() {
  return currentMode;
}

// Decide whether a newly-settled live batch may fold without changing the
// user's viewport. Visible panes use the caller's pre-mutation follow snapshot
// plus real geometry. Hidden panes have no meaningful rectangles, so only the
// registered last-visible follow state may authorize a fold.
export function canAutoFoldTranscriptBatch(scroller, batch, options) {
  options = options || {};
  const state = scrollers.get(scroller);
  if (!state || !scroller.isConnected) return false;
  if (!isVisibleScroller(scroller)) return state.atBottom;
  const atBottom = Object.hasOwn(options, "atBottom")
    ? options.atBottom === true
    : state.atBottom;
  if (atBottom) return true;
  if (
    !batch ||
    !batch.isConnected ||
    !isVisibleScroller(batch) ||
    typeof batch.getBoundingClientRect !== "function" ||
    typeof scroller.getBoundingClientRect !== "function"
  ) {
    return false;
  }
  const batchRect = batch.getBoundingClientRect();
  const scrollerRect = scroller.getBoundingClientRect();
  return batchRect.top >= scrollerRect.bottom;
}

export function setTranscriptPresentation(mode, options) {
  options = options || {};
  const next = normalizeMode(mode);
  if (next !== currentMode) {
    const snapshots = captureScrollers();
    prepareFocusForMode(next);
    currentMode = next;
    applyRootMode(currentMode);
    syncControls();
    restoreBottomPins(snapshots);
  }
  if (options.persist !== false) {
    try {
      localStorage.setItem(STORAGE_KEY, currentMode);
    } catch (_) {
      // The current page still changes when storage is unavailable.
    }
  }
  return currentMode;
}

export function mountTranscriptPresentationToggle(container, options) {
  options = options || {};
  const button = document.createElement("button");
  button.type = "button";
  button.className =
    "transcript-presentation-toggle" +
    (options.className ? " " + options.className : "");
  button.setAttribute("aria-label", "Compact ledger presentation");
  button.setAttribute(
    "aria-description",
    "Compact hides model reasoning and folds completed successful tool details.",
  );
  const glyph = document.createElement("span");
  glyph.className = "transcript-presentation-glyph";
  glyph.setAttribute("aria-hidden", "true");
  glyph.textContent = "≡";
  button.appendChild(glyph);
  const onClick = () => {
    setTranscriptPresentation(
      currentMode === "compact" ? "default" : "compact",
    );
  };
  button.addEventListener("click", onClick);
  container.appendChild(button);
  controls.add(button);
  syncControl(button);

  return () => {
    controls.delete(button);
    button.removeEventListener("click", onClick);
    button.remove();
  };
}

// Preserve follow state across a synchronous transcript reflow (for example,
// reopening a folded batch when a late exceptional verdict lands). The
// registered scroller owns the cached hidden-pane state; visible panes are
// remeasured immediately before mutation so users who scrolled away are never
// pulled back. Restoration is deferred until layout reflects the mutation.
export function preserveTranscriptBottomPin(element, mutate) {
  if (typeof mutate !== "function") return undefined;
  const state = scrollers.get(element);
  let snapshot = null;
  if (state && element.isConnected) {
    if (isVisibleScroller(element)) {
      state.atBottom = scrollerIsAtBottom(element);
    }
    snapshot = {
      scroller: element,
      state,
      atBottom: state.atBottom,
      scrollRevision: state.scrollRevision,
    };
  }
  try {
    return mutate();
  } finally {
    if (snapshot) restoreBottomPins([snapshot]);
  }
}

export function registerTranscriptScroller(element) {
  if (!element) return () => {};
  const prior = scrollers.get(element);
  if (prior) releaseScroller(element, prior, false);
  element.setAttribute(TRANSCRIPT_ROOT_ATTRIBUTE, "");
  const state = {
    scroller: element,
    atBottom: isVisibleScroller(element)
      ? scrollerIsAtBottom(element)
      : true,
    pendingBottomRestoreRevision: null,
    scrollRevision: 0,
    onScroll: null,
    resizeObserver: null,
  };
  state.onScroll = () => {
    state.scrollRevision += 1;
    state.pendingBottomRestoreRevision = null;
    if (isVisibleScroller(element)) {
      state.atBottom = scrollerIsAtBottom(element);
    }
  };
  element.addEventListener("scroll", state.onScroll, { passive: true });
  if (typeof ResizeObserver === "function") {
    state.resizeObserver = new ResizeObserver(() => {
      refreshScrollerFollowState(state);
    });
    state.resizeObserver.observe(element);
  }
  scrollers.set(element, state);
  return () => {
    releaseScroller(element, state, true);
  };
}

if (typeof window !== "undefined" && window.addEventListener) {
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY && event.key !== null) return;
    setTranscriptPresentation(event.newValue, { persist: false });
  });
}
