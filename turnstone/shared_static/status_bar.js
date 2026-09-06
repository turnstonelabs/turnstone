/* status_bar.js — shared per-workstream status-bar formatter.
 *
 * Used by:
 *   - turnstone/ui/static/app.js                          (interactive pane)
 *   - turnstone/console/static/coordinator/coordinator.js (coord dashboard)
 *
 * Both surfaces consume the same on_status SSE event shape (see
 * turnstone/core/session_ui_base.py SessionUI.on_status) and render
 * the same three cells: token / context-window usage with optional
 * effort suffix, tool calls this turn, conversation turn.  (The model
 * cell moved out — both surfaces now show model/effort in the composer chip.)
 * Beside them sits the pending-approval chip: a sticky, clickable count
 * of the human gates open in the pane's own transcript, painted from
 * each surface's approval bookkeeping rather than from an SSE event.
 * It is one use of the generic warn-chip painter below, which takes its
 * label and title from the caller; the coordinator's Children heading
 * paints another (its pending child-approval count).
 *
 * Single source of truth for warn / danger thresholds, prefix glyphs,
 * effort-suffix rules, the warn-chip build (glyph, hidden-at-zero,
 * focus hand-off) and the status-bar approval chip's own wording.  Each
 * surface owns its DOM (different element ids); the formatters take the
 * elements + the SSE event / count.
 *
 * ES module; window bridge below for the still-classic consumers.
 */
// Context-percent thresholds for the warn / danger paint.  Mirrored
// by the .ws-sb-warn / .ws-sb-danger CSS toggles in chat.css.
var CTX_WARN_PCT = 80;
var CTX_DANGER_PCT = 95;
var WARN_PREFIX = "▲ "; // ▲
var DANGER_PREFIX = "⚠ "; // ⚠
// Effort values that should NOT surface as a suffix on the tokens
// cell.  "medium" is the implicit default; "" / null means the
// model doesn't expose a reasoning_effort knob.
var SILENT_EFFORTS = { medium: 1, "": 1 };

/**
 * Repaint the three-cell status bar from an on_status SSE event.
 *
 * @param {Object} els — { rootEl, tokensEl, toolsEl, turnsEl }
 * @param {Object} evt — on_status payload (total_tokens, context_window,
 *   pct, effort, tool_calls_this_turn, turn_count).
 */
function paintStatusBar(els, evt) {
  if (!els || !evt) return;

  var totalTokens = evt.total_tokens || 0;
  var contextWindow = evt.context_window || 0;
  var pct = evt.pct || 0;
  var tokenText =
    totalTokens.toLocaleString() +
    " / " +
    (contextWindow ? contextWindow.toLocaleString() : "—") +
    (contextWindow ? " (" + pct + "%)" : "");
  var effort = evt.effort || "";
  if (effort && !(effort in SILENT_EFFORTS)) {
    tokenText += " · " + effort;
  }
  if (pct >= CTX_DANGER_PCT) tokenText = DANGER_PREFIX + tokenText;
  else if (pct >= CTX_WARN_PCT) tokenText = WARN_PREFIX + tokenText;
  if (els.tokensEl) els.tokensEl.textContent = tokenText;

  var tc = evt.tool_calls_this_turn || 0;
  if (els.toolsEl) {
    els.toolsEl.textContent = tc + " tool" + (tc !== 1 ? "s" : "");
  }
  var turns = evt.turn_count || 0;
  if (els.turnsEl) els.turnsEl.textContent = "turn " + turns;

  if (els.rootEl) {
    els.rootEl.classList.toggle("ws-sb-warn", pct >= CTX_WARN_PCT);
    els.rootEl.classList.toggle("ws-sb-danger", pct >= CTX_DANGER_PCT);
  }
}

/**
 * Reset the tokens cell to its placeholder text.  Called by the
 * coord dashboard on SSE reconnect when no prior status event has
 * been seen, so the transient "Reconnecting…" copy doesn't stick.
 */
function resetTokensPlaceholder(tokensEl) {
  if (tokensEl) tokensEl.textContent = "0 / —";
}

/**
 * Repaint a clickable warn chip: a count of things waiting on the
 * operator, hidden at zero (the [hidden] attribute; the chips' CSS sets
 * no display so the UA rule keeps winning).  The surface owns the click.
 *
 * The glyph rides an aria-hidden span so screen readers voice only the
 * plain sentence (an aria-label on the element would be ignored by
 * several of them, which then read the raw glyph).  Glyph + label are
 * built once; repaints only rewrite the label text.
 *
 * The count can drop to zero out from under a focused chip (a peer tab
 * resolves the gate, a transcript rebuild); hiding it then would drop
 * focus to the document body and restart Tab from the top.
 * `focusFallbackEl` (the surface's composer input) takes the focus
 * instead.
 *
 * @param {Object} els — { chipEl, focusFallbackEl }
 * @param {number} count — 0 hides the chip.
 * @param {Object} text — { label, title } for this count (plain words,
 *   already pluralized by the caller).
 */
function paintWarnChip(els, count, text) {
  var chip = els && els.chipEl;
  if (!chip) return;
  var n = count > 0 ? count : 0;
  if (n === 0) {
    var fallback = els.focusFallbackEl;
    if (
      !chip.hidden &&
      fallback &&
      typeof fallback.focus === "function" &&
      typeof chip.contains === "function" &&
      chip.contains(document.activeElement)
    ) {
      fallback.focus({ preventScroll: true });
    }
    chip.hidden = true;
    return;
  }
  chip.hidden = false;
  chip.title = text.title;
  var label = chip.querySelector(".warn-chip-label");
  if (!label) {
    var glyph = document.createElement("span");
    glyph.setAttribute("aria-hidden", "true");
    glyph.textContent = "⚠ ";
    label = document.createElement("span");
    label.className = "warn-chip-label";
    chip.appendChild(glyph);
    chip.appendChild(label);
  }
  label.textContent = text.label;
}

/**
 * Repaint the status bar's pending-approval chip.  `count` is the number
 * of live human-gated approval cycles in THIS pane's transcript —
 * parallel task agents make several live at once, and the card that
 * needs the click can sit far off-screen.  The surface's click scrolls
 * to the card its keyboard shortcuts would act on, so click target and
 * key target agree.
 *
 * @param {Object} els — { approvalEl, focusFallbackEl }
 * @param {number} count — live approval cycles (0 hides the chip).
 */
function paintApprovalChip(els, count) {
  if (!els) return;
  var n = count > 0 ? count : 0;
  var s = n === 1 ? "" : "s";
  paintWarnChip({ chipEl: els.approvalEl, focusFallbackEl: els.focusFallbackEl }, n, {
    label: n + " approval" + s + " needed",
    title: "Show the pending approval" + s,
  });
}

/**
 * Bring an approval card into view for the chip click.  Smooth unless
 * the viewer asked for reduced motion; centred so a tall card's action
 * row and feedback field land on-screen together.  Guarded for the
 * node harness's fake elements, which have no scrollIntoView.
 */
function scrollToApprovalTarget(el) {
  if (!el || typeof el.scrollIntoView !== "function") return;
  var reduce = false;
  try {
    reduce =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (_) {
    reduce = false;
  }
  el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
}

export const StatusBar = {
  paint: paintStatusBar,
  resetTokensPlaceholder: resetTokensPlaceholder,
  paintWarnChip: paintWarnChip,
  paintApprovalChip: paintApprovalChip,
  scrollToApprovalTarget: scrollToApprovalTarget,
  CTX_WARN_PCT: CTX_WARN_PCT,
  CTX_DANGER_PCT: CTX_DANGER_PCT,
};

// --- Legacy window bridge ---------------------------------------------------
// Still-classic consumers reach this as a global at event/boot time (after
// this deferred module evaluated).  New module code imports instead.
window.StatusBar = StatusBar;
