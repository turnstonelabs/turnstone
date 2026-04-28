/* status_bar.js — shared per-workstream status-bar formatter.
 *
 * Used by:
 *   - turnstone/ui/static/app.js                          (interactive pane)
 *   - turnstone/console/static/coordinator/coordinator.js (coord dashboard)
 *
 * Both surfaces consume the same on_status SSE event shape (see
 * turnstone/core/session_ui_base.py SessionUI.on_status) and render
 * the same four cells: model, token / context-window usage with
 * optional effort suffix, tool calls this turn, conversation turn.
 *
 * Single source of truth for warn / danger thresholds, prefix glyphs,
 * and effort-suffix rules.  Each surface owns its own DOM (different
 * element ids); the formatter takes the four span elements + the
 * status-bar root + the model strings + the SSE event.
 */
(function (root) {
  "use strict";

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
   * Repaint the four-cell status bar from an on_status SSE event.
   *
   * @param {Object} els — { rootEl, modelEl, tokensEl, toolsEl, turnsEl }
   * @param {Object} evt — on_status payload (total_tokens, context_window,
   *   pct, effort, tool_calls_this_turn, turn_count).
   * @param {Object} modelInfo — { alias, model } strings; alias falls
   *   back to model when empty, "—" when both empty.
   */
  function paintStatusBar(els, evt, modelInfo) {
    if (!els || !evt) return;
    var alias = (modelInfo && modelInfo.alias) || "";
    var model = (modelInfo && modelInfo.model) || "";
    if (els.modelEl) {
      els.modelEl.textContent = alias || model || "—";
      els.modelEl.title = model || "";
    }

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

  root.StatusBar = {
    paint: paintStatusBar,
    resetTokensPlaceholder: resetTokensPlaceholder,
    CTX_WARN_PCT: CTX_WARN_PCT,
    CTX_DANGER_PCT: CTX_DANGER_PCT,
  };
})(typeof window !== "undefined" ? window : globalThis);
