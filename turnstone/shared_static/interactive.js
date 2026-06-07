// ===========================================================================
//  turnstone interactive pane — shared module (shared_static/interactive.js)
//
//  The per-workstream conversational `Pane` (chat + approval cards + composer +
//  voice), extracted from `ui/static/app.js` so BOTH deployments can mount it:
//  the standalone `turnstone-server` UI (its split-pane shell stays in app.js
//  and constructs panes via `new Pane(wsId, opts)`) AND the console L-shell
//  (which proxies a node-hosted interactive session through a per-pane Tier-2
//  stream — see `createInteractivePane`).  The one transport difference is the
//  `opts.base` URL prefix: "" locally, "/node/{id}" when the console proxies a
//  session living on a cluster node (the LOCALITY invariant).
//
//  ES module — the first legacy pane lifted into a real module (step 5a; the
//  coordinator pane followed in 5e.0).  The shared substrate it leans on —
//  composer/renderer/etc. — is still classic, consumed as globals.  The console
//  shell.js imports the factory directly; the standalone loads it as
//  `<script type="module">`.  It also publishes `window.InteractivePane` /
//  `window.createInteractivePane` so the still-classic standalone `app.js`
//  shell can read the class — safe because app.js builds panes only AFTER the
//  workstream fetch resolves, well after this deferred module has executed.
//
//  House style: programmatic DOM, NO innerHTML (the renderer is the sole
//  sanctioned exception); pane code root-scopes to its own element.
// ===========================================================================

import {
  stripAnsi,
  buildWatchResultCard,
  buildSystemNudgeMarker,
  buildConvBatchShell,
  buildConvRow,
  buildConvCmd,
  buildConvVerdict,
  buildConvWarning,
  buildConvActions,
  buildConvStatus,
} from "./conversation.js";

let _paneCounter = 0;

// Voice-role availability comes from /v1/api/models (stt_default_alias /
// tts_default_alias — present only when an audio-capable model role is
// configured).  Memoized so all panes share a single fetch; affordances stay
// hidden until it resolves.
// Memoized per transport base — a console pane proxies a node-hosted session,
// so its voice roles come from THAT node's /v1/api/models (base "/node/{id}"),
// not the console's; standalone panes use base "" and share one fetch.
const _voiceRolesPromises = {};
function getVoiceRoles(base) {
  base = base || "";
  if (!_voiceRolesPromises[base]) {
    _voiceRolesPromises[base] = authFetch(base + "/v1/api/models")
      .then((r) => (r.ok ? r.json() : {}))
      .then((d) => ({
        stt: !!(d && d.stt_default_alias),
        tts: !!(d && d.tts_default_alias),
      }))
      .catch(() => ({ stt: false, tts: false }));
  }
  return _voiceRolesPromises[base];
}

// Visually-hidden polite live region for voice status (recording / playback)
// so screen-reader users perceive state changes otherwise conveyed only by
// color/icon. Errors go through showToast (already a live region). Single
// shared node; clear-then-set so repeated identical messages re-announce.
let _voiceStatusEl = null;
function voiceAnnounce(msg) {
  if (!_voiceStatusEl) {
    _voiceStatusEl = document.createElement("div");
    _voiceStatusEl.className = "sr-only";
    _voiceStatusEl.setAttribute("role", "status");
    _voiceStatusEl.setAttribute("aria-live", "polite");
    document.body.appendChild(_voiceStatusEl);
  }
  _voiceStatusEl.textContent = "";
  window.setTimeout(() => {
    if (_voiceStatusEl) _voiceStatusEl.textContent = msg;
  }, 30);
}

// Visually-hidden POLITE live region for the tool-call early paint
// (tool_pending) so screen-reader users hear a committed call land — and that
// they can Stop it — even though messagesEl is flipped to aria-live="off"
// during the token streaming that immediately precedes the call.  Polite (not
// assertive): a committed call is worth surfacing but isn't the action-required
// human gate, which keeps its own assertive announcement.  Separate node from
// the voice region so the two never clobber each other.  Single shared node;
// clear-then-set so repeated identical messages re-announce.
let _toolStatusEl = null;
function toolAnnounce(msg) {
  if (!msg) return;
  if (!_toolStatusEl) {
    _toolStatusEl = document.createElement("div");
    _toolStatusEl.className = "sr-only";
    _toolStatusEl.setAttribute("role", "status");
    _toolStatusEl.setAttribute("aria-live", "polite");
    document.body.appendChild(_toolStatusEl);
  }
  _toolStatusEl.textContent = "";
  window.setTimeout(() => {
    if (_toolStatusEl) _toolStatusEl.textContent = msg;
  }, 30);
}

// Terse SR summary for a committed tool batch: tool name(s) (capped at 3) +
// the fact it's being judged and can be stopped.  Empty string when there are
// no named tools (toolAnnounce then no-ops).
function _toolAnnounceText(items) {
  const names = (items || []).map((it) => it && it.func_name).filter(Boolean);
  if (!names.length) return "";
  const n = names.length;
  const shown = names.slice(0, 3).join(", ");
  const list = n > 3 ? shown + ", and " + (n - 3) + " more" : shown;
  const head =
    n === 1 ? "Tool call pending: " + list : n + " tool calls pending: " + list;
  // No "judge evaluating" claim — tool_pending fires unconditionally, so the
  // intent judge may be disabled.  "Pending + you can stop it" is always true.
  return head + ". You can stop " + (n === 1 ? "it" : "them") + ".";
}

// Default host adapter — console-safe no-ops so a bare ``new Pane(wsId)`` never
// throws on a shell-only seam.  The standalone shell (app.js) and the console
// factory (createInteractivePane) each pass a richer host.
const INTERACTIVE_DEFAULT_HOST = {
  // Workstream display name — the surrounding shell knows it; a bare pane falls
  // back to a short id (see updateWsName).
  getWsName() {
    return null;
  },
  // Is THIS pane the user's current focus?  Gates focus-stealing so a caret is
  // never yanked into a backgrounded pane.  A lone pane is always focused.
  isFocused() {
    return true;
  },
  // EventSource-error policy.  Native auto-reconnect handles transient drops; a
  // bare/console pane relies on it, so this is a no-op.  The standalone focused
  // pane additionally refetches + reassigns the ws list (see app.js).
  onStreamError() {},
  // Where the ``--skip-permissions`` banner lands (standalone: #ui-header).
  warningTarget(pane) {
    return pane.messagesEl;
  },
  // An MCP server needs (re-)consent — the standalone shell drives its
  // settings-gear badge; a bare/console pane surfaces it inline only.
  onConsentDetected() {},
};

class Pane {
  constructor(wsId, opts) {
    opts = opts || {};
    this.id = "p" + ++_paneCounter;
    this.wsId = wsId || null;
    // Transport + host seam.  ``base`` is the node-proxy URL prefix ("" for a
    // local session, "/node/{id}" when the console proxies a session that lives
    // on a cluster node — the LOCALITY invariant).  ``embedded`` drops the
    // standalone split-pane chrome (focus tracking, context menu, split/close
    // buttons) for the L-shell's tab + slim header.  ``host`` supplies the few
    // things only the surrounding shell knows (workstream name, which pane is
    // focused, the stream-error recovery policy, the warning-banner target);
    // see the standalone adapter in app.js and createInteractivePane below.
    this._base = opts.base || "";
    this._embedded = !!opts.embedded;
    this._host = opts.host || INTERACTIVE_DEFAULT_HOST;
    this._onClose = typeof opts.onClose === "function" ? opts.onClose : null;
    this.evtSource = null;
    this.el = null;
    this.headerEl = null;
    this.messagesEl = null;
    this.inputEl = null;
    this.sendBtn = null;
    this.stopBtn = null;
    this.currentAssistantEl = null;
    this.currentReasoningEl = null;
    this.contentBuffer = "";
    this.busy = false;
    this.isThinking = false;
    this.pendingApproval = false;
    this.approvalBlockEl = null;
    // Early-paint shell from a ``tool_pending`` event, awaiting its
    // authoritative ``tool_info`` / ``approve_request`` upgrade.
    this.announcedBlockEl = null;
    this.retryDelay = 1000;
    this.model = "";
    this.modelAlias = "";
    this._lastStatusEvt = null;
    this._historyLoadToken = 0;
    this._cancelTimeout = null;
    this._forceTimeout = null;
    this._pendingEditSend = null;
    // Voice I/O (mic STT + per-message TTS playback)
    this._voiceRoles = { stt: false, tts: false };
    this._micBtn = null;
    this._micIcon = null;
    this._micDenied = false;
    this._recorder = null;
    this._recordingStream = null;
    this._isRecording = false;
    this._discardRecording = false;
    this._recordAborted = false;
    this._recordTimer = null;
    this._recordStartMs = 0;
    this._ttsAudio = null;
    this._ttsBtnActive = null;
    this._ttsSeq = 0;
    this._createDOM();
  }

  reset() {
    this.currentAssistantEl = null;
    this.currentReasoningEl = null;
    this.contentBuffer = "";
    this.setBusy(false);
    this.pendingApproval = false;
    this.approvalBlockEl = null;
    this.announcedBlockEl = null;
    this._pendingEditSend = null;
    this.inputEl.disabled = false;
    this.attachments.clearChips();
    this._stopRecording(true);
    this._stopTTS();
  }

  updateWsName() {
    if (!this.headerEl) return; // no header in the embedded L-shell pane
    const nameEl = this.headerEl.querySelector(".pane-ws-name");
    if (nameEl) {
      nameEl.textContent = this.wsId
        ? this._host.getWsName(this.wsId) || this.wsId.substring(0, 8)
        : "";
    }
  }

  disconnectSSE() {
    if (this._cancelTimeout) {
      clearTimeout(this._cancelTimeout);
      this._cancelTimeout = null;
    }
    if (this._forceTimeout) {
      clearTimeout(this._forceTimeout);
      this._forceTimeout = null;
    }
    if (this.evtSource) {
      this.evtSource.close();
      this.evtSource = null;
    }
    this._stopRecording(true);
    this._stopTTS();
  }

  // composer.setBusy runs unconditionally so the Stop button label /
  // dataset.forceCancel / placeholder stay canonical even on a redundant
  // call (Pane.reset() and any future caller relies on that idempotent
  // reset). queue.onIdleEdge runs only on the actual edge — it carries
  // the heavier work (querySelectorAll-driven promote sweep + cancel-
  // timer cleanup wired via the queue's onIdle hook).
  setBusy(b) {
    const next = !!b;
    this.composer.setBusy(next);
    this.messagesEl.dataset.busy = next ? "true" : "false";
    const edge = next !== this.busy;
    this.busy = next;
    if (edge && !next) this.queue.onIdleEdge();
    // The mic produces composer input the user can only send when idle — keep
    // it in lockstep with the send button (which composer.setBusy gates).
    if (this._micBtn && !this._micDenied) {
      if (next) {
        this._stopRecording(true); // abandon any in-flight recording
        this._micBtn.disabled = true;
      } else if (!this._micBtn.classList.contains("is-busy")) {
        this._micBtn.disabled = false;
      }
    }
  }

  showEmptyState() {
    if (!this.messagesEl.querySelector(".empty-state")) {
      const el = document.createElement("div");
      el.className = "empty-state";
      el.textContent = "Type a message to start";
      this.messagesEl.appendChild(el);
    }
  }

  removeEmptyState() {
    const el = this.messagesEl.querySelector(".empty-state");
    if (el) el.remove();
  }

  addThinkingIndicator() {
    if (this.messagesEl.querySelector(".thinking-indicator")) return;
    const el = document.createElement("div");
    el.className = "thinking-indicator";
    el.textContent = "Thinking";
    this.messagesEl.appendChild(el);
    this.scrollToBottom();
  }

  removeThinkingIndicator() {
    const el = this.messagesEl.querySelector(".thinking-indicator");
    if (el) el.remove();
  }

  addSystemNudgeMarker() {
    // The marker DOM is shared (conversation.buildSystemNudgeMarker); the Pane
    // owns empty-state removal + placement.
    this.removeEmptyState();
    const el = buildSystemNudgeMarker();
    this.messagesEl.appendChild(el);
    return el;
  }

  addSystemContext(content, source, meta) {
    // First-class operator-context system turn — the consolidation of the
    // legacy metacognition reminders / user interjections / output-guard
    // notes into one role="system" trajectory turn.  Rendered as a distinct
    // operator bubble in sequence (it FOLLOWS the turn it advises);
    // `source` carries the kind (user_interjection / output_guard /
    // tool_error / ...) for the bubble label.  `watch_triggered` additionally
    // carries structured `meta` (watch_name / command / poll counters) → the
    // richer `.msg.watch-result` card instead of the plain operator bubble.
    this.removeEmptyState();
    if (source === "watch_triggered" && meta && typeof meta === "object") {
      const card = buildWatchResultCard(meta, content || "");
      this.messagesEl.appendChild(card);
      this.scrollToBottom(true);
      return card;
    }
    if (source === "output_guard" && meta && typeof meta === "object") {
      const card = _buildGuardFindingBubble(meta);
      this.messagesEl.appendChild(card);
      this.scrollToBottom(true);
      return card;
    }
    // user_interjection renders as a "queued message" bubble showing the user's
    // RAW words (`meta.message`) rather than the model-directed framing in
    // `content`, with brighter emphasis for `!!!`-important interjections.
    const isInterjection =
      source === "user_interjection" && meta && typeof meta === "object";
    const important = isInterjection && meta.priority === "important";
    const el = document.createElement("div");
    el.className =
      "msg system-context operator-context" + (important ? " important" : "");
    const body = document.createElement("div");
    body.className = "msg-body";
    const labelEl = document.createElement("span");
    labelEl.className = "msg-system-context-label";
    labelEl.textContent = isInterjection
      ? "queued message" + (important ? " · important" : "")
      : operatorSourceLabel(source);
    const textEl = document.createElement("span");
    textEl.className = "msg-system-context-text";
    textEl.textContent =
      isInterjection && meta.message != null
        ? String(meta.message)
        : content || "";
    body.appendChild(labelEl);
    body.appendChild(textEl);
    el.appendChild(body);
    this.messagesEl.appendChild(el);
    this.scrollToBottom(true);
    return el;
  }

  addUserMessage(text, attachments) {
    this.removeEmptyState();
    const el = document.createElement("div");
    el.className = "msg user";
    const textEl = document.createElement("div");
    textEl.className = "msg-user-text";
    textEl.textContent = text;
    el.appendChild(textEl);
    if (Array.isArray(attachments) && attachments.length > 0) {
      const pills = document.createElement("div");
      pills.className = "msg-user-attach";
      attachments.forEach(function (a) {
        const pill = document.createElement("span");
        pill.className =
          "msg-user-attach-pill msg-user-attach-pill-" + (a.kind || "other");
        const icon = document.createElement("span");
        icon.className = "msg-user-attach-icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = a.kind === "image" ? "\ud83d\uddbc" : "\ud83d\udcc4";
        pill.appendChild(icon);
        const nameEl = document.createElement("span");
        nameEl.className = "msg-user-attach-name";
        nameEl.textContent =
          a.filename || (a.kind === "image" ? "image" : "document");
        pill.appendChild(nameEl);
        pills.appendChild(pill);
      });
      el.appendChild(pills);
    }
    this._addUserMsgActions(el, text);
    this.messagesEl.appendChild(el);
    this.scrollToBottom(true);
  }

  getFeedback() {
    if (!this.approvalBlockEl) return null;
    const inp = this.approvalBlockEl.querySelector(".conv-feedback");
    return inp && inp.value.trim() ? inp.value.trim() : null;
  }

  appendToolOutputChunk(callId, chunk) {
    if (!chunk) return;
    const stripped = stripAnsi(chunk);
    if (!stripped) return;

    const escapedId = callId ? CSS.escape(callId) : "";
    let el = escapedId
      ? this.messagesEl.querySelector(
          '.tool-output-stream[data-call-id="' + escapedId + '"]',
        )
      : null;
    if (!el) {
      let target = escapedId
        ? this.messagesEl.querySelector(
            '.conv-row[data-call-id="' + escapedId + '"]',
          )
        : null;
      if (!target) {
        const blocks = this.messagesEl.querySelectorAll(".conv-batch");
        if (!blocks.length) return;
        const block = blocks[blocks.length - 1];
        const tools = block.querySelectorAll(
          '.conv-row[data-func-name="bash"]',
        );
        target = tools.length ? tools[tools.length - 1] : null;
        if (!target) {
          const allTools = block.querySelectorAll(".conv-row");
          target = allTools.length ? allTools[allTools.length - 1] : null;
        }
      }
      if (!target) return;

      el = document.createElement("pre");
      el.className = "tool-output tool-output-stream";
      el.dataset.callId = callId;
      el.setAttribute("aria-label", "Streaming command output");
      el.setAttribute("aria-live", "off");
      el.textContent = "";
      target.after(el);
    }

    el.appendChild(document.createTextNode(stripped));
    el.scrollTop = el.scrollHeight;
    this.scrollToBottom();
  }

  showOutputWarning(evt) {
    if (!evt.call_id || evt.risk_level === "none") return;
    const escapedId = CSS.escape(evt.call_id);
    const toolDiv = this.messagesEl.querySelector(
      '.conv-row[data-call-id="' + escapedId + '"]',
    );
    if (!toolDiv) return;
    // Shared DOM-builder with replayHistory \u2014 single source of truth for
    // role / class / escape semantics.  Argument shape mirrors the
    // server-side output_assessment dict AND the replay payload built by
    // build_merged_output_assessment_payload (both via the shared
    // merge_guard_display_payload), so the live chip and the refresh chip
    // render identically (tier / judge_risk / confidence / reasoning).
    const warning = _buildOutputWarningEl({
      risk_level: evt.risk_level,
      flags: evt.flags,
      redacted: evt.redacted,
      tier: evt.tier,
      judge_risk: evt.judge_risk,
      confidence: evt.confidence,
      reasoning: evt.reasoning,
      judge_model: evt.judge_model,
    });
    const nextEl = toolDiv.nextElementSibling;
    if (nextEl && nextEl.classList.contains("tool-output")) {
      nextEl.insertAdjacentElement("afterend", warning);
    } else {
      toolDiv.insertAdjacentElement("afterend", warning);
    }
  }

  updateVerdictBadge(verdict) {
    if (!verdict || !verdict.call_id) return;
    const escapedId = CSS.escape(verdict.call_id);
    const badge = this.messagesEl.querySelector(
      '.conv-verdict[data-call-id="' + escapedId + '"]',
    );
    if (!badge) {
      // Badge no longer in DOM (tool block replaced by output) — toast the
      // late-arriving verdict so the user still sees it.
      const conf = Math.round((verdict.confidence || 0) * 100);
      const rec = verdict.recommendation || "review";
      const func = verdict.func_name || "";
      showToast(
        "Judge verdict for " + func + ": " + rec + " (" + conf + "%)",
        rec === "approve" ? "success" : rec === "deny" ? "error" : "warning",
      );
      return;
    }
    // Replace the badge (+ its detail sibling) with a freshly-built one so the
    // landed LLM verdict, its risk stripe, and the detail all refresh at once.
    const detail = badge.nextElementSibling;
    if (detail && detail.classList.contains("conv-verdict-detail")) {
      detail.remove();
    }
    badge.replaceWith(buildConvVerdict(verdict, { judgePending: false }));

    this.updateVerdictGlow(verdict.recommendation);
  }

  updateVerdictGlow(recommendation) {
    if (!this.approvalBlockEl) return;
    const actions = this.approvalBlockEl.querySelector(".conv-actions");
    if (!actions) return;
    // Collect all verdict badges currently visible in this approval block.
    const badges = this.approvalBlockEl.querySelectorAll(".conv-verdict");
    let worst = recommendation;
    for (let i = 0; i < badges.length; i++) {
      const recEl = badges[i].querySelector(".conv-verdict-rec");
      if (recEl) {
        const r = recEl.textContent;
        if (r === "deny") {
          worst = "deny";
          break;
        }
        if (r === "review" && worst !== "deny") worst = "review";
      }
    }
    actions.classList.remove(
      "conv-verdict-glow--approve",
      "conv-verdict-glow--deny",
      "conv-verdict-glow--review",
    );
    if (worst === "approve")
      actions.classList.add("conv-verdict-glow--approve");
    else if (worst === "deny") actions.classList.add("conv-verdict-glow--deny");
    else actions.classList.add("conv-verdict-glow--review");
  }

  addInfoMessage(text) {
    const el = document.createElement("div");
    el.className = "msg info";
    el.textContent = stripAnsi(text);
    this.messagesEl.appendChild(el);
    this.scrollToBottom();
  }

  addErrorMessage(text) {
    const el = document.createElement("div");
    el.className = "msg error";
    el.setAttribute("role", "alert");
    el.textContent = stripAnsi(text);
    this.messagesEl.appendChild(el);
    this.scrollToBottom();
  }

  updateStatus(evt) {
    StatusBar.paint(
      {
        rootEl: this.statusBarEl,
        modelEl: this._sbModel,
        tokensEl: this._sbTokens,
        toolsEl: this._sbTools,
        turnsEl: this._sbTurns,
      },
      evt,
      { alias: this.modelAlias, model: this.model },
    );
    this._lastStatusEvt = evt;
  }

  isNearBottom() {
    return (
      this.messagesEl.scrollHeight -
        this.messagesEl.scrollTop -
        this.messagesEl.clientHeight <
      80
    );
  }

  scrollToBottom(force) {
    if (force || this.isNearBottom()) {
      this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    }
  }

  _createDOM() {
    this.el = document.createElement("div");
    this.el.className = "pane";
    if (this._embedded) this.el.classList.add("pane--embedded");
    this.el.dataset.paneId = this.id;

    // Approval keyboard shortcuts.  The converged card advertises y/n/a (+Enter/
    // Esc) kbd hints, so route those keys to resolveApproval when this pane has a
    // pending approval.  Pane-owned (on this.el) so it works for every embedded
    // L-shell pane without a global handler — the old standalone wired this via
    // the app.js global keydown + getFocusedPane, which the fork collapse retired.
    // The composer is disabled while pending, so the only typing surface is the
    // feedback field: there Enter approves (with feedback) / Esc denies and other
    // keys type; elsewhere y|Enter approve, n|Esc deny, a = approve-all.
    this.el.addEventListener("keydown", (e) => {
      if (!this.pendingApproval || !this.approvalBlockEl) return;
      const fb = this.approvalBlockEl.querySelector(".conv-feedback");
      if (fb && document.activeElement === fb) {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          this.resolveApproval(true, false, this.getFeedback());
        } else if (e.key === "Escape") {
          e.preventDefault();
          this.resolveApproval(false, false, this.getFeedback());
        }
        return;
      }
      const k = e.key.toLowerCase();
      if (k === "y" || e.key === "Enter") {
        e.preventDefault();
        this.resolveApproval(true, false, this.getFeedback());
      } else if (k === "n" || e.key === "Escape") {
        e.preventDefault();
        this.resolveApproval(false, false, this.getFeedback());
      } else if (k === "a") {
        e.preventDefault();
        this.resolveApproval(true, true, this.getFeedback());
      }
    });

    // Standalone split-pane affordances: focus tracking + the right-click
    // split/close menu.  In the L-shell the tab bar owns focus and the per-tab
    // action menu, so an embedded pane wires none of it.
    if (!this._embedded) {
      // Focus on mousedown (before child clicks)
      this.el.addEventListener("mousedown", () => {
        setFocusedPane(this.id);
      });
      // Also track keyboard focus moving into this pane (e.g. Tab into textarea)
      this.el.addEventListener(
        "focusin",
        () => {
          setFocusedPane(this.id);
        },
        true,
      );

      // Right-click context menu for split/close actions — skip interactive
      // elements (textareas, links, buttons) so native copy/paste works
      this.el.addEventListener("contextmenu", (e) => {
        const tag = e.target.tagName;
        if (
          tag === "TEXTAREA" ||
          tag === "INPUT" ||
          tag === "A" ||
          tag === "BUTTON" ||
          e.target.isContentEditable
        )
          return;
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) return;
        e.preventDefault();
        setFocusedPane(this.id);
        showPaneContextMenu(e.clientX, e.clientY, this.id);
      });
    }

    // No pane header in the L-shell (embedded): the workstream name, persona,
    // and state are shown by the tab and the rail (Workspaces).  The standalone
    // split-pane (retired in step 6) still builds a header for its split/close
    // actions.  The --skip-permissions banner lands in messagesEl now (see the
    // host warningTarget), not the header.
    if (!this._embedded) {
      this.headerEl = document.createElement("div");
      this.headerEl.className = "pane-header";

      const wsName = document.createElement("span");
      wsName.className = "pane-ws-name";
      wsName.textContent = this.wsId
        ? this._host.getWsName(this.wsId) || this.wsId.substring(0, 8)
        : "";
      this.headerEl.appendChild(wsName);

      const actions = document.createElement("div");
      actions.className = "pane-actions";

      const splitRightBtn = document.createElement("button");
      splitRightBtn.className = "pane-action-btn";
      splitRightBtn.title = "Split right";
      splitRightBtn.setAttribute("aria-label", "Split right");
      splitRightBtn.textContent = "\u2502";
      splitRightBtn.onclick = (e) => {
        e.stopPropagation();
        splitPane(this.id, "horizontal");
      };
      actions.appendChild(splitRightBtn);

      const splitDownBtn = document.createElement("button");
      splitDownBtn.className = "pane-action-btn";
      splitDownBtn.title = "Split down";
      splitDownBtn.setAttribute("aria-label", "Split down");
      splitDownBtn.textContent = "\u2500";
      splitDownBtn.onclick = (e) => {
        e.stopPropagation();
        splitPane(this.id, "vertical");
      };
      actions.appendChild(splitDownBtn);

      const closeBtn = document.createElement("button");
      closeBtn.className = "pane-action-btn pane-close-btn";
      closeBtn.title = "Close pane";
      closeBtn.setAttribute("aria-label", "Close pane");
      closeBtn.textContent = "\u00d7";
      closeBtn.onclick = (e) => {
        e.stopPropagation();
        if (countLeaves(splitRoot) > 1) closePane(this.id);
      };
      actions.appendChild(closeBtn);

      this.headerEl.appendChild(actions);
      this.el.appendChild(this.headerEl);
    }

    // Messages area
    this.messagesEl = document.createElement("div");
    this.messagesEl.className = "pane-messages";
    this.messagesEl.setAttribute("role", "log");
    this.messagesEl.setAttribute("aria-live", "polite");
    this.messagesEl.setAttribute("aria-label", "Chat messages");
    this.el.appendChild(this.messagesEl);

    // Per-workstream status bar (above input)
    this.statusBarEl = document.createElement("div");
    this.statusBarEl.className = "ws-status-bar";
    this.statusBarEl.setAttribute("role", "status");
    this.statusBarEl.setAttribute("aria-live", "polite");
    this.statusBarEl.setAttribute("aria-atomic", "true");
    this.statusBarEl.setAttribute("aria-label", "Workstream status");

    this._sbModel = document.createElement("span");
    this._sbModel.className = "ws-sb-model";
    this._sbModel.textContent = "\u2014";
    this._sbModel.setAttribute("aria-label", "Model");
    this._sbTokens = document.createElement("span");
    this._sbTokens.className = "ws-sb-tokens";
    this._sbTokens.textContent = "0 / \u2014";
    this._sbTokens.setAttribute("aria-label", "Token usage");
    this._sbTools = document.createElement("span");
    this._sbTools.className = "ws-sb-tools";
    this._sbTools.textContent = "0 tools";
    this._sbTools.setAttribute("aria-label", "Tool calls this turn");
    this._sbTurns = document.createElement("span");
    this._sbTurns.className = "ws-sb-turns";
    this._sbTurns.textContent = "turn 0";
    this._sbTurns.setAttribute("aria-label", "Conversation turn");

    this.statusBarEl.appendChild(this._sbModel);
    this.statusBarEl.appendChild(this._sbTokens);
    this.statusBarEl.appendChild(this._sbTools);
    this.statusBarEl.appendChild(this._sbTurns);
    this.el.appendChild(this.statusBarEl);

    // Input area — DOM + behavior comes from shared/composer.js.  The
    // pane keeps the attachment-upload pipeline (because attachments are
    // pane-specific state) and routes file events through the composer's
    // attach/paste/drop callbacks.
    this.composer = new Composer(this.el, {
      attachments: {
        onAttach: (file) => {
          this.attachments.upload(file);
        },
      },
      stopBtn: true,
      queueWhileBusy: true,
      busyPlaceholder: "Queue a message\u2026 (!!! for urgent)",
      onSend: () => {
        this.sendMessage();
      },
      onStop: () => {
        this.cancelGeneration();
      },
      dragDrop: { targetEl: this.el, dropClass: "pane-drop-target" },
    });
    this.inputEl = this.composer.inputEl;
    this.sendBtn = this.composer.sendBtn;
    this.stopBtn = this.composer.stopBtn;
    // Lazy wsId read \u2014 a tab swap (Pane re-bound to a new workstream)
    // changes the closure target without re-instantiating the controllers.
    this.attachments = createAttachmentController({
      chipsEl: this.composer.chipsEl,
      getWsId: () => {
        return this.wsId;
      },
      onError: (msg) => {
        showToast(msg);
      },
    });
    this.queue = createQueueController({
      messagesEl: this.messagesEl,
      getWsId: () => {
        return this.wsId;
      },
      onAfterDequeue: () => {
        this.attachments.rehydrate();
      },
      // Idle-edge cleanup of the cancel/force-stop timers — without
      // this they fire on the *next* busy turn, relabel Stop to "Force
      // Stop", and surface a misleading "Cancel didn't complete in
      // time" toast about a turn the user already moved past.
      onIdle: () => {
        if (this._cancelTimeout) {
          clearTimeout(this._cancelTimeout);
          this._cancelTimeout = null;
        }
        if (this._forceTimeout) {
          clearTimeout(this._forceTimeout);
          this._forceTimeout = null;
        }
      },
    });

    // Voice input: mic button, hidden until the STT role is confirmed
    // available (so it never appears when voice isn't configured).
    this._buildMicButton();
    getVoiceRoles(this._base).then((roles) => {
      this._voiceRoles = roles;
      if (this._micBtn) this._micBtn.style.display = roles.stt ? "" : "none";
    });
  }

  connectSSE(wsId) {
    this.disconnectSSE();
    const wsChanged = this.wsId !== wsId;
    this.wsId = wsId;
    if (wsChanged) {
      this.attachments.clearChips();
      this.attachments.rehydrate();
    }

    // Build the events URL with a ``?last_event_id=N`` query param
    // if we have a saved high-water mark from a prior connection.
    // The EventSource constructor can't set custom headers, so the
    // browser-native ``Last-Event-ID`` header isn't available here;
    // the server accepts both forms.  ``_lastEventId`` is captured
    // from the prior source's onmessage handler.
    let evtUrl =
      this._base +
      "/v1/api/workstreams/" +
      encodeURIComponent(wsId) +
      "/events";
    // ``!= null`` (not truthiness): a resume cursor of 0 is valid — the
    // ring buffer's first emitted event is id 1, so register_listener_with_replay(0)
    // replays the whole in-flight turn. A brand-new ws seeds _event_id at 0,
    // so its first user row (and thus a first-turn /history cursor) can be 0;
    // a truthiness gate would silently drop it and fall back to the lossy
    // fresh snapshot. Mirrors the ``data.cursor != null`` guard in _refetchHistory.
    if (this._lastEventId != null) {
      evtUrl += "?last_event_id=" + encodeURIComponent(this._lastEventId);
    }
    this.evtSource = new EventSource(evtUrl);

    this.evtSource.onopen = () => {
      this.retryDelay = 1000;
      this.statusBarEl.classList.remove("ws-sb-disconnected");
      if (this._lastStatusEvt) this.updateStatus(this._lastStatusEvt);
    };

    this.evtSource.onmessage = (e) => {
      // Capture lastEventId BEFORE JSON.parse so a (rare) malformed
      // event doesn't desync the manual-reconnect fallback from
      // native auto-reconnect (which advances lastEventId regardless
      // of whether we successfully process the data).  Server's
      // stamping contract: ``id:`` only on events sourced from the
      // per-ws ring buffer — synthetic replay events (history /
      // state_change / in_progress_snapshot) don't advance the
      // counter, so reconnect resumes from the last BUFFERED id (or
      // none on a truly-fresh connect that never received one).
      if (this.evtSource && this.evtSource.lastEventId) {
        this._lastEventId = this.evtSource.lastEventId;
      }
      const data = JSON.parse(e.data);
      // Tag the event with its own SSE id so the system_turn handler can
      // dedup a turn already painted from /history against the same turn
      // redelivered by an SSE replay.  e.lastEventId is this event's id;
      // buffered events (system_turn included) always carry one.
      if (e.lastEventId) data._event_id = e.lastEventId;
      this.handleEvent(data);
    };

    this.evtSource.onerror = () => {
      // Do NOT close evtSource for transient network errors — native
      // EventSource auto-reconnect handles them with the
      // ``Last-Event-ID`` header automatically (now that the server
      // emits ``id:`` on every buffered event).  Closing here would
      // force a CONNECTING -> CLOSED transition that defeats native
      // reconnect, which is exactly the reconnect-with-replay defect
      // PR-D ships to fix.  See
      // tests/test_app_js.py::test_pane_connectsse_onerror_preserves_native_reconnect.
      const loginOverlay = document.getElementById("login-overlay");
      if (loginOverlay && loginOverlay.style.display !== "none") return;
      this.statusBarEl.classList.add("ws-sb-disconnected");
      this._sbTokens.textContent = "Reconnecting\u2026";
      // Stream-error recovery is the host's policy.  Native EventSource
      // auto-reconnect transparently covers the transient case for every
      // pane; the standalone focused pane additionally refetches the global
      // workstream list to reassign a ws evicted during the gap (app.js),
      // while a console/embedded pane owns one ws and relies on native
      // reconnect alone.
      this._host.onStreamError(this);
    };
  }

  _loadHistoryThenConnect(wsId) {
    // Mirror coord's init() ordering: render history from REST first,
    // THEN open the live stream. Disconnect any existing stream up
    // front so stray events from the previously-assigned ws don't paint
    // into the pane mid-fetch. History is no longer replayed over SSE,
    // so this REST fetch is the sole first-paint source; connectSSE's
    // in_progress_snapshot still covers a generation that lands between
    // the fetch and the stream opening.
    this.disconnectSSE();
    // (Re)load of a (possibly different) ws: drop the per-ws SSE replay
    // cursor + cached status. Sending the previous ws's last_event_id to a
    // new ws mis-triggers the server's replay_ok path and skips the synthetic
    // replay (connected / status / in_progress_snapshot); the fresh connect
    // below gets the new ws's full initial state instead.
    this._lastEventId = null;
    this._lastStatusEvt = null;
    // Generation token — a slow refetch (e.g. a large resumed session) must
    // not render its history, reconnect its stream, or fire its resend after
    // the pane has switched to another ws. Newest load wins; older ones drop.
    const token = (this._historyLoadToken || 0) + 1;
    this._historyLoadToken = token;
    // ``seedCursor=true``: this is the initial-connect path (the
    // ``.finally`` reconnects), so a resume cursor from /history should
    // seed _lastEventId for that connect. The clear_ui / replay_truncated
    // re-render callers pass it false — they run on an already-live stream
    // and must NOT rewind _lastEventId off the live position.
    this._refetchHistory(wsId, token, true).finally(() => {
      if (token === this._historyLoadToken) this.connectSSE(wsId);
    });
  }

  async _refetchHistory(wsId, token, seedCursor = false) {
    // Fetch conversation history over REST. Used for first paint (before
    // connecting SSE) and to re-render after a clear_ui signal (rewind /
    // retry / resume / open). The FETCH is wrapped (network/parse failure
    // → empty pane); the render is deliberately OUTSIDE the catch so a
    // render bug surfaces loudly instead of being masked as an empty pane.
    //
    // ``seedCursor`` is true ONLY on the initial-connect path
    // (_loadHistoryThenConnect, which reconnects via .finally). The
    // re-render callers leave it false so a fast-forward cursor never
    // rewinds the live stream's _lastEventId backward (which would
    // double-render on a later transient reconnect, or — on a re-render
    // that trims an orphan with no reconnect — strand the omitted turn).
    const id = wsId || this.wsId;
    let data = null;
    try {
      const r = await authFetch(
        this._base +
          "/v1/api/workstreams/" +
          encodeURIComponent(id) +
          "/history",
      );
      if (r && r.ok) data = await r.json();
    } catch (err) {
      data = null;
    }
    // Drop a superseded load: a newer _loadHistoryThenConnect (ws switch)
    // bumped the token while this fetch was in flight, so rendering now would
    // paint the wrong ws's history into the pane.
    if (token !== undefined && token !== this._historyLoadToken) return;
    if (data) {
      // Fresh-connect fast-forward: when the trailing turn is an
      // executing in-flight tool batch the server can replay, /history
      // returns a non-null ``cursor`` (a Last-Event-ID) and OMITS that
      // turn from ``messages``. On the initial-connect path only
      // (``seedCursor``), seed ``_lastEventId`` so the connectSSE below
      // opens the initial stream with ?last_event_id=, taking the
      // replay_ok delta path that rebuilds the in-flight turn (tool
      // calls, results, prompts) through the live handlers — no synthetic
      // snapshot, no /history-vs-delta double-render. Null cursor leaves
      // _lastEventId untouched (fresh connect, already nulled by
      // _loadHistoryThenConnect); the re-render callers never seed.
      if (seedCursor && data.cursor != null) this._lastEventId = data.cursor;
      // The REST /history payload is already the canonical projected wire
      // shape (server-side projection in make_history_handler:
      // flat tool_calls, top-level source/reminders/attachments, collapsed
      // content, derived denied/is_error/pending) — feed it straight to
      // replayHistory. No client-side normalisation.
      this.replayHistory(data.messages || []);
    } else {
      this.showEmptyState();
    }
  }

  handleEvent(evt) {
    // Guard: drop events that belong to a different workstream.
    // This prevents cross-contamination during tab switches and reconnects.
    if (evt.ws_id && evt.ws_id !== this.wsId) return;
    switch (evt.type) {
      case "thinking_start":
        this.isThinking = true;
        this.setBusy(true);
        this.removeEmptyState();
        this.addThinkingIndicator();
        break;

      case "thinking_stop":
        this.isThinking = false;
        this.removeThinkingIndicator();
        break;

      case "reasoning":
        this.removeThinkingIndicator();
        if (!this.currentReasoningEl) {
          this.currentReasoningEl = document.createElement("div");
          this.currentReasoningEl.className = "msg reasoning";
          this.messagesEl.appendChild(this.currentReasoningEl);
        }
        this.currentReasoningEl.textContent += evt.text;
        this.scrollToBottom();
        break;

      case "content":
        this.removeThinkingIndicator();
        if (this.currentReasoningEl) {
          this.currentReasoningEl = null;
        }
        if (!this.currentAssistantEl) {
          this.currentAssistantEl = document.createElement("div");
          this.currentAssistantEl.className = "msg assistant";
          this.currentAssistantBodyEl = document.createElement("div");
          this.currentAssistantBodyEl.className = "msg-body";
          this.currentAssistantEl.appendChild(this.currentAssistantBodyEl);
          this.messagesEl.appendChild(this.currentAssistantEl);
        }
        this.contentBuffer += evt.text;
        streamingRender(this.currentAssistantBodyEl, this.contentBuffer);
        this.scrollToBottom();
        break;

      case "stream_end":
        if (this._cancelTimeout) {
          clearTimeout(this._cancelTimeout);
          this._cancelTimeout = null;
        }
        if (this._forceTimeout) {
          clearTimeout(this._forceTimeout);
          this._forceTimeout = null;
        }
        // Finalize the current streaming segment's markdown.  This fires
        // per-segment (between tool calls), NOT per-turn.  Busy state is
        // managed by state_change events instead.
        if (this.currentAssistantBodyEl && this.contentBuffer) {
          streamingRenderFinalize(
            this.currentAssistantBodyEl,
            this.contentBuffer,
          );
        }
        this.currentAssistantBodyEl = null;
        this.currentAssistantEl = null;
        this.currentReasoningEl = null;
        this.contentBuffer = "";
        this.scrollToBottom(true);
        break;

      case "in_progress_snapshot":
        // One-shot replay of the in-progress turn's reasoning + content
        // when this client connects mid-stream (page refresh while the
        // model is generating).  Both fields may be empty; render only
        // the non-empty halves.  Idempotent on EventSource auto-reconnect:
        // skip overwrite when the current buffer is already at-or-past
        // the snapshot length, so a stale replay can't reset the live-
        // streamed view back to a shorter prefix.
        this.removeThinkingIndicator();
        if (evt.reasoning) {
          if (!this.currentReasoningEl) {
            this.currentReasoningEl = document.createElement("div");
            this.currentReasoningEl.className = "msg reasoning";
            this.messagesEl.appendChild(this.currentReasoningEl);
          }
          const curReason = this.currentReasoningEl.textContent || "";
          if (curReason.length < evt.reasoning.length) {
            this.currentReasoningEl.textContent = evt.reasoning;
          }
        }
        if (evt.content) {
          // Content snapshot supersedes any reasoning bubble — matches
          // the "case content" invariant of clearing currentReasoningEl
          // when content begins.
          if (this.currentReasoningEl) {
            this.currentReasoningEl = null;
          }
          if (!this.currentAssistantEl) {
            this.currentAssistantEl = document.createElement("div");
            this.currentAssistantEl.className = "msg assistant";
            this.currentAssistantBodyEl = document.createElement("div");
            this.currentAssistantBodyEl.className = "msg-body";
            this.currentAssistantEl.appendChild(this.currentAssistantBodyEl);
            this.messagesEl.appendChild(this.currentAssistantEl);
          }
          if (this.contentBuffer.length < evt.content.length) {
            this.contentBuffer = evt.content;
            streamingRender(this.currentAssistantBodyEl, this.contentBuffer);
          }
        }
        this.scrollToBottom();
        break;

      case "state_change":
        if (evt.state === "idle" || evt.state === "error") {
          this.setBusy(false);
          this._attachRetryToLastAssistant();
          // Only steal focus if this is the active pane and no approval pending.
          if (this._host.isFocused(this) && !this.pendingApproval) {
            this.inputEl.focus();
          }
        } else if (
          evt.state === "thinking" ||
          evt.state === "running" ||
          evt.state === "attention"
        ) {
          this.setBusy(true);
        }
        break;

      case "tool_pending":
        // Early paint — render the pending call the instant the model
        // commits to it, before the judge verdict + approval gate resolve,
        // so the operator can Stop in an emergency.  The authoritative
        // tool_info / approve_request that follows upgrades THIS block in
        // place (matched by call_id) rather than appending a duplicate.
        this.announceToolBlock(evt.items);
        break;

      case "tool_info":
        this.showInlineToolBlock(evt.items, true);
        break;

      case "approve_request":
        this.showInlineToolBlock(evt.items, false, evt.judge_pending);
        break;

      case "intent_verdict":
        this.updateVerdictBadge(evt);
        break;

      case "output_warning":
        this.showOutputWarning(evt);
        break;

      case "approval_resolved":
        this.resolveApproval(evt.approved, false, evt.feedback, true);
        break;

      case "tool_output_chunk":
        this.appendToolOutputChunk(evt.call_id || "", evt.chunk);
        break;

      case "tool_result":
        this.appendToolOutput(
          evt.call_id || "",
          evt.name,
          evt.output,
          evt.is_error,
        );
        break;

      case "status":
        this.updateStatus(evt);
        break;

      case "info":
        this.addInfoMessage(evt.message);
        break;

      case "error":
        // Show the error but don't change busy state — state_change
        // handles idle/error transitions.  on_error fires for non-terminal
        // errors (tool parse failures, truncation) mid-turn too.
        this.addErrorMessage(evt.message);
        break;

      case "system_turn": {
        // First-class operator-context system turn (output-guard finding,
        // user interjection, metacognitive nudge — see
        // tool_advisory.make_system_turn).  Consolidates the legacy
        // user_reminder / tool_reminder events into one operator bubble
        // rendered in trajectory sequence (it FOLLOWS the turn it advises,
        // so by the time this SSE event arrives the related turn already
        // rendered).  ``evt.source`` carries the kind for the bubble label;
        // ``evt.meta`` the structured per-kind fields (watch-result card).
        // Dedup: if this turn was already painted from /history (its row id
        // matches this event's id) an SSE replay redelivered it — skip.  With
        // the resume-cursor fix this shouldn't recur, but the guard keeps the
        // /history+replay seam idempotent for system turns regardless.
        const sysEid = evt._event_id != null ? String(evt._event_id) : null;
        if (
          sysEid &&
          this._renderedSystemEventIds &&
          this._renderedSystemEventIds.has(sysEid)
        ) {
          break;
        }
        this.addSystemContext(
          evt.content || "",
          evt.source || "",
          evt.meta || null,
        );
        if (sysEid) {
          if (!this._renderedSystemEventIds)
            this._renderedSystemEventIds = new Set();
          this._renderedSystemEventIds.add(sysEid);
        }
        break;
      }

      case "message_queued":
        // Confirmation from server that a queued message was accepted.
        // The UI already showed the message optimistically in addQueuedMessage.
        break;

      case "busy_error":
        // Server is still busy — don't transition to send mode.
        // Re-enable the stop button so the user can try cancelling.
        this.addErrorMessage(evt.message);
        this.stopBtn.textContent = "\u25a0 Stop";
        this.stopBtn.setAttribute("aria-label", "Stop generation");
        delete this.stopBtn.dataset.forceCancel;
        this.stopBtn.disabled = false;
        break;

      case "cancelled":
        // Cancel requested but worker thread may still be finishing.
        // Show "Cancelling..." state; state_change will transition to ready.
        // If state_change already arrived (busy is false), the cancel is
        // already handled — don't re-enter the cancelling state.
        if (!this.busy) break;
        // Clear any prior timeouts first (duplicate cancelled events).
        clearTimeout(this._cancelTimeout);
        clearTimeout(this._forceTimeout);
        this.currentAssistantEl = null;
        this.currentReasoningEl = null;
        this.contentBuffer = "";
        this.stopBtn.disabled = true;
        this.stopBtn.textContent = "Cancelling\u2026";
        this.stopBtn.setAttribute("aria-label", "Cancelling generation");
        this.scrollToBottom(true);
        // After 2s, offer "Force Stop" for a harder cancel that abandons
        // the stuck worker thread.  Safety timeout at 10s auto-recovers
        // if state_change never arrives (connection drop).
        this._cancelTimeout = setTimeout(() => {
          if (this.busy) {
            this.stopBtn.disabled = false;
            this.stopBtn.textContent = "\u26a0 Force Stop";
            this.stopBtn.setAttribute("aria-label", "Force stop generation");
            this.stopBtn.dataset.forceCancel = "true";
          }
        }, 2000);
        this._forceTimeout = setTimeout(() => {
          if (this.busy) {
            this.addInfoMessage(
              "Cancel didn\u2019t complete in time. You may need to resend your last message.",
            );
            this.setBusy(false);
          }
        }, 10000);
        break;

      case "connected":
        this.model = evt.model || "";
        this.modelAlias = evt.model_alias || evt.model || "";
        this._sbModel.textContent = this.modelAlias || this.model || "—";
        this._sbModel.title = this.model || "";
        if (evt.skip_permissions) {
          const existing = document.querySelector(".skip-permissions-warning");
          if (!existing) {
            const warn = document.createElement("div");
            warn.className = "skip-permissions-warning";
            warn.textContent =
              "\u26a0 Running with --skip-permissions: all tool calls are auto-approved";
            this._host.warningTarget(this).appendChild(warn);
          }
        }
        break;

      case "clear_ui": {
        // Conversation was structurally reset (rewind / retry / resume /
        // open / fork). Empty the pane for immediate feedback, then
        // re-render from REST and dispatch any queued edit-and-resend
        // once the (possibly truncated) history lands. The resend keys
        // off this signal rather than an inline history SSE event. Capture
        // the load token so a ws switch mid-flight discards both the
        // re-render and the resend (no cross-ws send).
        const token = this._historyLoadToken;
        this.messagesEl.replaceChildren();
        this._refetchHistory(this.wsId, token)
          .then(() => {
            if (token !== this._historyLoadToken) return;
            if (!this._pendingEditSend) return;
            const editText = this._pendingEditSend;
            this._pendingEditSend = null;
            this.setBusy(true);
            this.addUserMessage(editText);
            authFetch(
              this._base +
                "/v1/api/workstreams/" +
                encodeURIComponent(this.wsId) +
                "/send",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: editText }),
              },
            ).catch((err) => {
              this.addErrorMessage("Connection error: " + err.message);
              this.setBusy(false);
            });
          })
          .catch((err) => {
            // The render runs outside _refetchHistory's try/catch by design;
            // if it throws, don't strand the queued edit-and-resend — clear
            // the latch + busy so the composer recovers.
            this._pendingEditSend = null;
            this.setBusy(false);
            this.addErrorMessage("Failed to reload history: " + err.message);
          });
        break;
      }

      case "replay_truncated":
        // Reconnect buffer evicted past our last-seen event id — the
        // live recovery replay no longer carries history, so re-sync
        // from REST. Skip while a turn is mid-stream: the recovery
        // floor's in_progress_snapshot already paints it, and an async
        // refetch's replaceChildren() would detach the live bubble so
        // content deltas render nowhere. Re-syncs on the next clean
        // (re)connect.
        if (!this.currentAssistantEl)
          this._refetchHistory(this.wsId, this._historyLoadToken);
        break;
    }
  }

  _addUserMsgActions(el, text) {
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Message actions");
    // Edit button
    const editBtn = document.createElement("button");
    editBtn.className = "msg-action-btn";
    editBtn.title = "Edit & resend";
    editBtn.setAttribute("aria-label", "Edit and resend this message");
    const editIcon = document.createElement("span");
    editIcon.className = "icon-edit";
    editIcon.setAttribute("aria-hidden", "true");
    editBtn.appendChild(editIcon);
    editBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      this._startEdit(el, text);
    });
    bar.appendChild(editBtn);
    // Rewind-to-here button
    const rewindBtn = document.createElement("button");
    rewindBtn.className = "msg-action-btn";
    rewindBtn.title = "Rewind to before this message";
    rewindBtn.setAttribute(
      "aria-label",
      "Rewind conversation to before this message",
    );
    const rewindIcon = document.createElement("span");
    rewindIcon.className = "icon-rewind";
    rewindIcon.setAttribute("aria-hidden", "true");
    rewindBtn.appendChild(rewindIcon);
    rewindBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      this._rewindToMessage(el);
    });
    bar.appendChild(rewindBtn);
    el.appendChild(bar);
  }

  _addRetryAction(el) {
    let bar = el.querySelector(".msg-actions");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "msg-actions";
      bar.setAttribute("role", "toolbar");
      bar.setAttribute("aria-label", "Message actions");
      el.appendChild(bar);
    }
    const btn = document.createElement("button");
    btn.className = "msg-action-btn";
    btn.title = "Retry (regenerate response)";
    btn.setAttribute("aria-label", "Retry last response");
    const icon = document.createElement("span");
    icon.className = "icon-retry";
    icon.setAttribute("aria-hidden", "true");
    btn.appendChild(icon);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      this._retryLast();
    });
    bar.insertBefore(btn, bar.firstChild);
  }

  // -------------------------------------------------------------------------
  // Voice I/O: microphone dictation (STT) + per-message playback (TTS)
  // -------------------------------------------------------------------------

  _buildMicButton() {
    if (!this.composer || !this.composer.actionsRowEl) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "composer-mic-btn";
    btn.style.display = "none"; // revealed once the STT role is confirmed
    btn.title = "Record speech to text";
    btn.setAttribute("aria-label", "Record speech to text");
    btn.setAttribute("aria-pressed", "false");
    const icon = document.createElement("span");
    icon.className = "composer-mic-icon icon-mic";
    icon.setAttribute("aria-hidden", "true");
    btn.appendChild(icon);
    this._micIcon = icon;
    btn.addEventListener("click", () => this._toggleRecording());
    this.composer.actionsRowEl.insertBefore(btn, this.sendBtn || null);
    this._micBtn = btn;
  }

  _syncMicButton() {
    if (!this._micBtn) return;
    const rec = !!this._isRecording;
    this._micBtn.classList.toggle("is-recording", rec);
    this._micBtn.setAttribute("aria-pressed", rec ? "true" : "false");
    if (this._micIcon) {
      this._micIcon.classList.toggle("icon-stop", rec);
      this._micIcon.classList.toggle("icon-mic", !rec);
    }
    const label = rec
      ? "Stop recording and transcribe"
      : "Record speech to text";
    this._micBtn.title = label;
    this._micBtn.setAttribute("aria-label", label);
  }

  _toggleRecording() {
    if (this._isRecording) {
      this._stopRecording(false);
    } else {
      this._startRecording();
    }
  }

  _startRecording() {
    if (this._isRecording || this.busy) return;
    if (
      !navigator.mediaDevices ||
      !navigator.mediaDevices.getUserMedia ||
      typeof MediaRecorder === "undefined"
    ) {
      showToast("Microphone capture is not supported in this browser", "error");
      return;
    }
    // Synchronous abort latch: if teardown (reset / pane switch) runs while the
    // permission prompt is open, the stream the promise later hands us must be
    // stopped instead of going hot after teardown.
    this._recordAborted = false;
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then((stream) => {
        if (this._recordAborted) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        this._recordingStream = stream;
        let mimeType = "";
        const candidates = [
          "audio/webm;codecs=opus",
          "audio/webm",
          "audio/ogg;codecs=opus",
          "audio/mp4",
        ];
        for (let i = 0; i < candidates.length; i++) {
          if (
            MediaRecorder.isTypeSupported &&
            MediaRecorder.isTypeSupported(candidates[i])
          ) {
            mimeType = candidates[i];
            break;
          }
        }
        const rec = mimeType
          ? new MediaRecorder(stream, { mimeType })
          : new MediaRecorder(stream);
        const chunks = [];
        rec.addEventListener("dataavailable", (e) => {
          if (e.data && e.data.size) chunks.push(e.data);
        });
        rec.addEventListener("stop", () => {
          this._teardownRecordingStream();
          this._isRecording = false;
          this._stopRecordTimer();
          this._syncMicButton();
          const discard = this._discardRecording;
          this._discardRecording = false;
          if (discard) return;
          const blob = new Blob(chunks, {
            type: rec.mimeType || mimeType || "audio/webm",
          });
          if (blob.size) this._uploadForTranscription(blob);
        });
        this._recorder = rec;
        this._isRecording = true;
        this._discardRecording = false;
        this._startRecordTimer();
        this._syncMicButton();
        voiceAnnounce(
          "Recording. Activate the microphone button again to stop.",
        );
        rec.start();
      })
      .catch(() => {
        // Denied / hardware unavailable: leave a persistent disabled state with
        // guidance — a hot button reads as dead once the browser blocks re-prompts.
        this._teardownRecordingStream();
        this._isRecording = false;
        this._stopRecordTimer();
        this._setMicDenied();
      });
  }

  _setMicDenied() {
    this._micDenied = true;
    showToast("Microphone access was denied", "error");
    this._syncMicButton();
    if (this._micBtn) {
      this._micBtn.disabled = true;
      const msg =
        "Microphone blocked — enable it in your browser's site settings";
      this._micBtn.title = msg;
      this._micBtn.setAttribute("aria-label", msg);
    }
  }

  _startRecordTimer() {
    this._recordStartMs = Date.now();
    this._stopRecordTimer();
    this._recordTimer = window.setInterval(() => this._tickRecordTimer(), 500);
  }

  _stopRecordTimer() {
    if (this._recordTimer) {
      window.clearInterval(this._recordTimer);
      this._recordTimer = null;
    }
  }

  _tickRecordTimer() {
    if (!this._isRecording || !this._micBtn) return;
    const secs = Math.max(
      0,
      Math.floor((Date.now() - this._recordStartMs) / 1000),
    );
    const mmss =
      Math.floor(secs / 60) + ":" + String(secs % 60).padStart(2, "0");
    this._micBtn.title = "Recording " + mmss + " — activate to stop";
  }

  _stopRecording(discard) {
    this._discardRecording = !!discard;
    this._recordAborted = true; // abort a getUserMedia still in flight
    if (this._recorder && this._recorder.state !== "inactive") {
      try {
        this._recorder.stop();
      } catch (e) {
        /* already stopped */
      }
      return;
    }
    this._teardownRecordingStream();
    this._stopRecordTimer();
    if (this._isRecording) {
      this._isRecording = false;
      this._syncMicButton();
    }
  }

  _teardownRecordingStream() {
    if (this._recordingStream) {
      this._recordingStream.getTracks().forEach((t) => t.stop());
      this._recordingStream = null;
    }
    this._recorder = null;
  }

  _uploadForTranscription(blob) {
    if (!this.wsId) return;
    const ext =
      blob.type.indexOf("ogg") !== -1
        ? "ogg"
        : blob.type.indexOf("mp4") !== -1
          ? "mp4"
          : "webm";
    const fd = new FormData();
    fd.append("audio", blob, "speech." + ext);
    if (this._micBtn) {
      this._micBtn.disabled = true;
      this._micBtn.classList.add("is-busy");
    }
    voiceAnnounce("Transcribing…");
    authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/speech-to-text",
      { method: "POST", body: fd },
    )
      .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
      .then((res) => {
        if (!res.ok) {
          showToast(
            (res.body && res.body.error) || "Transcription failed",
            "error",
          );
          return;
        }
        const text = (res.body && res.body.transcript) || "";
        if (text && this.inputEl) {
          const cur = this.inputEl.value || "";
          this.inputEl.value = cur
            ? cur.replace(/\s*$/, "") + " " + text
            : text;
          // Drive the composer's auto-resize + send-enable listeners.
          this.inputEl.dispatchEvent(new Event("input", { bubbles: true }));
          this.inputEl.focus();
          voiceAnnounce("Transcript added to message.");
        }
      })
      .catch((err) => {
        showToast(
          "Transcription failed: " + (err && err.message ? err.message : err),
          "error",
        );
      })
      .finally(() => {
        if (this._micBtn && !this._micDenied) {
          this._micBtn.disabled = !!this.busy;
          this._micBtn.classList.remove("is-busy");
        }
      });
  }

  _addTtsAction(el) {
    let bar = el.querySelector(".msg-actions");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "msg-actions";
      bar.setAttribute("role", "toolbar");
      bar.setAttribute("aria-label", "Message actions");
      el.appendChild(bar);
    }
    if (bar.querySelector(".msg-tts-btn")) return; // already added
    const btn = document.createElement("button");
    btn.className = "msg-action-btn msg-tts-btn";
    btn.title = "Play response aloud";
    btn.setAttribute("aria-label", "Play response aloud");
    btn.setAttribute("aria-pressed", "false");
    const icon = document.createElement("span");
    icon.className = "icon-speaker";
    icon.setAttribute("aria-hidden", "true");
    btn.appendChild(icon);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      this._playMessageTTS(el, btn);
    });
    bar.appendChild(btn);
  }

  // Strip code blocks / inline code / rendered math so TTS doesn't read source
  // or KaTeX accessibility text out character-by-character.
  _extractSpeakableText(bodyEl) {
    const clone = bodyEl.cloneNode(true);
    clone
      .querySelectorAll("pre, code, .katex, .katex-display")
      .forEach((n) =>
        n.replaceWith(document.createTextNode(" (code omitted) ")),
      );
    return (clone.textContent || "").replace(/\s+/g, " ").trim();
  }

  _playMessageTTS(el, btn) {
    // Toggle: clicking the active button (or any while playing) stops first.
    if (this._ttsAudio) {
      const wasThis = this._ttsBtnActive === btn;
      this._stopTTS();
      if (wasThis) return;
    }
    const bodyEl = el.querySelector(".msg-body") || el;
    const text = this._extractSpeakableText(bodyEl);
    if (!text) return;
    // Serialize: a monotonic token guards against an earlier (slower) request
    // resolving after a newer one — which would double-play and leak the blob.
    const token = ++this._ttsSeq;
    btn.classList.add("is-busy");
    btn.disabled = true;
    authFetch(this._base + "/v1/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    })
      .then((r) => {
        if (!r.ok) {
          return r.json().then((b) => {
            throw new Error((b && b.error) || "Speech synthesis failed");
          });
        }
        return r.blob();
      })
      .then((audioBlob) => {
        const url = URL.createObjectURL(audioBlob);
        if (token !== this._ttsSeq) {
          URL.revokeObjectURL(url); // superseded — don't play or leak
          return;
        }
        const audio = new Audio(url);
        this._ttsAudio = audio;
        this._ttsBtnActive = btn;
        btn.classList.add("is-playing");
        btn.setAttribute("aria-pressed", "true");
        voiceAnnounce("Playing response.");
        audio.addEventListener("ended", () => this._stopTTS());
        audio.addEventListener("error", () => this._stopTTS());
        audio.play().catch(() => this._stopTTS());
      })
      .catch((err) => {
        showToast(
          err && err.message ? err.message : "Speech synthesis failed",
          "error",
        );
      })
      .finally(() => {
        btn.classList.remove("is-busy");
        btn.disabled = false;
      });
  }

  _stopTTS() {
    this._ttsSeq++; // invalidate any in-flight request
    if (this._ttsAudio) {
      try {
        this._ttsAudio.pause();
      } catch (e) {
        /* ignore */
      }
      const src = this._ttsAudio.src || "";
      if (src.indexOf("blob:") === 0) URL.revokeObjectURL(src);
      this._ttsAudio = null;
    }
    if (this._ttsBtnActive) {
      this._ttsBtnActive.classList.remove("is-playing");
      this._ttsBtnActive.setAttribute("aria-pressed", "false");
      this._ttsBtnActive = null;
    }
  }

  _retryLast() {
    if (this.busy) return;
    // Path-keyed retry (#549). Truncation + re-dispatch happen
    // server-side; the clear_ui event drives the history refetch.
    authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/retry",
      { method: "POST" },
    ).catch((err) => {
      this.addErrorMessage("Retry failed: " + err.message);
    });
  }

  // Path-keyed rewind (#549) by absolute turn count. Shared by the
  // per-message rewind button and the hand-typed /rewind reroute.
  _rewindToTurns(turns) {
    if (this.busy) return;
    if (!Number.isInteger(turns) || turns < 1) return;
    authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/rewind",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turns }),
      },
    ).catch((err) => {
      this.addErrorMessage("Rewind failed: " + err.message);
    });
  }

  _rewindToMessage(msgEl) {
    if (this.busy) return;
    // Count how many user messages come at or after this one. Bare
    // ``.msg.user`` is intentional: system-nudge markers carry that
    // class and the server's _find_turn_boundaries counts them as
    // turns too, so this matches the server's rewind-N semantics.
    const userMsgs = this.messagesEl.querySelectorAll(".msg.user");
    const idx = Array.prototype.indexOf.call(userMsgs, msgEl);
    if (idx < 0) return;
    const turnsToRewind = userMsgs.length - idx;
    this._rewindToTurns(turnsToRewind);
  }

  _startEdit(msgEl, originalText) {
    if (this.busy) return;
    // Save current child nodes for cancel restoration
    const savedNodes = [];
    while (msgEl.firstChild) {
      savedNodes.push(msgEl.removeChild(msgEl.firstChild));
    }
    msgEl.classList.add("msg-editing");

    const form = document.createElement("div");
    form.className = "msg-edit-form";

    const textarea = document.createElement("textarea");
    textarea.className = "msg-edit-textarea";
    textarea.setAttribute("aria-label", "Edit message text");
    textarea.value = originalText;
    textarea.rows = Math.min(originalText.split("\n").length + 1, 8);
    form.appendChild(textarea);

    const actions = document.createElement("div");
    actions.className = "msg-edit-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "msg-edit-btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => {
      // Restore original nodes
      while (msgEl.firstChild) msgEl.removeChild(msgEl.firstChild);
      savedNodes.forEach((n) => {
        msgEl.appendChild(n);
      });
      msgEl.classList.remove("msg-editing");
    });
    actions.appendChild(cancelBtn);

    const sendBtn = document.createElement("button");
    sendBtn.className = "msg-edit-btn msg-edit-btn-send";
    sendBtn.textContent = "Send";
    sendBtn.addEventListener("click", () => {
      const newText = textarea.value.trim();
      if (!newText) return;
      this._editAndResend(msgEl, newText);
    });
    actions.appendChild(sendBtn);

    // Ctrl+Enter to send, Escape to cancel
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        sendBtn.click();
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancelBtn.click();
      }
    });

    form.appendChild(actions);
    msgEl.appendChild(form);
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  }

  _editAndResend(msgEl, newText) {
    if (this.busy) return;
    // Count turns to rewind (from this message onward). Bare
    // ``.msg.user`` matches the server's turn semantics — see
    // _rewindToMessage.
    const userMsgs = this.messagesEl.querySelectorAll(".msg.user");
    const idx = Array.prototype.indexOf.call(userMsgs, msgEl);
    if (idx < 0) return;
    const turnsToRewind = userMsgs.length - idx;

    this.setBusy(true);
    // Store pending send — dispatched from the clear_ui handler once
    // the rewind's truncated history is re-fetched over REST.
    this._pendingEditSend = newText;
    authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/rewind",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turns: turnsToRewind }),
      },
    )
      .then(async (r) => {
        if (r && !r.ok) {
          this._pendingEditSend = null;
          this.setBusy(false);
          this.addErrorMessage(
            "Rewind failed (HTTP " + r.status + " " + r.statusText + ")",
          );
          return;
        }
        // A 200 {"status":"busy"} means the rewind was rejected because a
        // generation is in flight — no clear_ui fires, so the pending-edit
        // latch + busy state would otherwise stay stuck (and the latch
        // would later resend into the next clear_ui). Clear them here.
        let data = null;
        try {
          data = await r.json();
        } catch {
          data = null;
        }
        if (data && data.status === "busy") {
          this._pendingEditSend = null;
          this.setBusy(false);
          this.addErrorMessage(
            "Cannot edit & resend while the workstream is processing.",
          );
        }
      })
      .catch((err) => {
        this._pendingEditSend = null;
        this.addErrorMessage("Rewind failed: " + err.message);
        this.setBusy(false);
      });
  }

  replayHistory(messages) {
    this.messagesEl.replaceChildren();
    // Reset the per-pane dedup set: ids of operator-context system turns
    // already painted from /history.  A later SSE replay that redelivers one
    // (resume-cursor overlap) is skipped by the system_turn handler.
    this._renderedSystemEventIds = new Set();
    if (!messages.length) {
      this.showEmptyState();
      return;
    }
    // Suppress the polite live region while we batch-build the replay
    // — messagesEl is aria-live="polite" so a fresh replay would otherwise
    // queue an announcement for every approved/denied/verdict pill we
    // insert.  Restored after the loop so live SSE updates announce
    // normally.  WCAG 4.1.3 — historical content should not behave like
    // real-time updates.
    this.messagesEl.setAttribute("aria-busy", "true");
    // pendingAssessments[call_id] = output_assessment dict.  Populated
    // from the assistant branch, consumed by the role==="tool" branch
    // (or after the loop, for legacy rows missing tool_call_id).
    // Replaces a JSON.stringify→dataset→JSON.parse round-trip with an
    // in-memory map keyed by call_id.
    const pendingAssessments = {};
    let lastToolBlock = null;
    for (let i = 0; i < messages.length; i++) {
      const msg = messages[i];
      if (msg.role === "user") {
        if (msg.source === "system_nudge") {
          // Wake-driven empty user turn: render the thin marker (replaces
          // the previously-skipped synthetic empty bubble).  The nudges it
          // carried are now first-class system turns that follow it and
          // render via their own `system` branch below.
          this.addSystemNudgeMarker();
          lastToolBlock = null;
          continue;
        }
        this.addUserMessage(msg.content || "", msg.attachments || null);
        lastToolBlock = null;
      } else if (msg.role === "assistant") {
        // Reasoning bubble (Phase 1 reasoning persistence) — render
        // BEFORE the content bubble so the visual order matches the
        // live SSE flow (reasoning_delta arrives before content_delta
        // for thinking-enabled models). Mirrors the live-stream
        // construction at the "case 'reasoning':" branch above. Only
        // surfaces when the active model's surface_persisted_reasoning flag is
        // true and the message round-tripped a thinking lane.
        if (msg.reasoning && msg.reasoning.length) {
          const reasonEl = document.createElement("div");
          reasonEl.className = "msg reasoning";
          reasonEl.textContent = msg.reasoning;
          this.messagesEl.appendChild(reasonEl);
          lastToolBlock = null;
        }
        // Render content BEFORE the tool block so the visual order
        // matches the live SSE flow (stream_text streams content first,
        // then tool_info / approve_request paints the tool block, then
        // tool_result fills it in).  Order also matters structurally:
        // the tool-result message in the NEXT iteration anchors via
        // lastToolBlock, which the tool-block branch sets last — so
        // content must run first to avoid clobbering that anchor.
        //
        // Whitespace-only content (e.g. "\n\n" from a reasoning-parser
        // model that strips <think>…</think> and leaves only trailing
        // newlines before the tool call) is treated as empty — the
        // live stream never accumulated a visible bubble for it, so
        // surfacing one on replay would be a phantom card that diverges
        // from what the originating tab saw.
        if (msg.content && msg.content.trim()) {
          const el = document.createElement("div");
          el.className = "msg assistant";
          const bodyEl = document.createElement("div");
          bodyEl.className = "msg-body";
          el.appendChild(bodyEl);
          setMarkdown(bodyEl, msg.content);
          this.messagesEl.appendChild(el);
          lastToolBlock = null;
        }
        if (msg.tool_calls && msg.tool_calls.length) {
          if (msg.pending) {
            lastToolBlock = null;
          } else {
            const wasDenied = !!msg.denied;
            const block = document.createElement("div");
            block.className =
              "conv-batch conv-batch--solo " +
              (wasDenied ? "conv-batch--denied" : "conv-batch--approved");
            block.appendChild(
              _convApprovalHead(
                "Tool",
                msg.tool_calls.map((tc) => ({ func_name: tc.name })),
              ),
            );
            msg.tool_calls.forEach((tc) => {
              // Synthesize the live `item` shape from the stored tool_call so
              // replay renders the SAME .conv-row as the live path.
              let header = "";
              try {
                const args = JSON.parse(tc.arguments);
                if (tc.name === "bash") {
                  header = String(Object.values(args)[0] || "");
                } else {
                  const parts = [];
                  const keys = Object.keys(args);
                  for (let k = 0; k < keys.length; k++) {
                    const val = args[keys[k]];
                    let valStr =
                      val === null || val === undefined ? "null" : String(val);
                    if (valStr.length > 80)
                      valStr = valStr.substring(0, 77) + "...";
                    parts.push(keys[k] + ": " + valStr);
                  }
                  header = parts.join("\n");
                }
              } catch (e) {
                header = String(tc.arguments || "").substring(0, 100);
              }
              const item = { func_name: tc.name, call_id: tc.id || "", header };
              const row = buildToolDiv(item);
              // Verdict anchored to THIS row; replay verdicts are final.
              if (tc.verdict) {
                row.appendChild(
                  buildConvVerdict(tc.verdict, { judgePending: false }),
                );
              }
              block.appendChild(row);
              // Output-guard finding — deferred until the tool result lands so
              // it anchors under the output (mirrors live showOutputWarning).
              if (
                tc.output_assessment &&
                tc.output_assessment.risk_level &&
                tc.output_assessment.risk_level !== "none"
              ) {
                pendingAssessments[tc.id || ""] = {
                  assessment: tc.output_assessment,
                  toolDiv: row,
                };
              }
            });
            block.appendChild(
              buildConvStatus(
                wasDenied ? { approved: false } : { approved: true },
              ),
            );
            this.messagesEl.appendChild(block);
            lastToolBlock = block;
          }
        }
      } else if (msg.role === "tool") {
        if (lastToolBlock) {
          const stripped = stripAnsi(msg.content || "").trim();
          const isDenied =
            msg.denied ||
            /^Denied by user/.test(stripped) ||
            /^Blocked/.test(stripped);
          const isToolError = !!msg.is_error;
          // Anchor the rendered output to the specific .conv-row
          // element matching this result's tool_call_id — mirrors the
          // live appendToolOutput path so multi-tool batches show
          // [hdr A][out A][hdr B][out B] rather than [A][B][out A][out B].
          // Falls back to "before badge" when tool_call_id is absent
          // (legacy rows pre-dating the wire-format addition).
          let resultTarget = null;
          if (msg.tool_call_id) {
            resultTarget = lastToolBlock.querySelector(
              '.conv-row[data-call-id="' + CSS.escape(msg.tool_call_id) + '"]',
            );
          }
          // Cursor-style append: cursor advances after each insert so
          // the next sibling lands AFTER the previous one.  Fixes the
          // bug where calling resultTarget.after(node) twice put the
          // second node BETWEEN resultTarget and the first (the second
          // .after call was always relative to the same anchor).
          // Resulting order with all present:
          //   [tool div][output][output-warning]
          let insertCursor = resultTarget;
          const insertChained = (node) => {
            if (insertCursor) {
              insertCursor.after(node);
              insertCursor = node;
            } else {
              const bdg = lastToolBlock.querySelector(".conv-status");
              if (bdg) lastToolBlock.insertBefore(node, bdg);
              else lastToolBlock.appendChild(node);
            }
          };
          if (stripped && !isDenied) {
            const media = !isToolError ? tryParseMedia(stripped) : null;
            if (media) {
              insertChained(buildMediaEmbed(media, stripped));
            } else {
              const out = renderToolOutput(stripped, isToolError);
              if (out.textContent.split("\n").length > 10) {
                makeCollapsible(out);
              }
              insertChained(out);
            }
          }
          if (
            isToolError &&
            !lastToolBlock.classList.contains("conv-batch--denied")
          ) {
            lastToolBlock.classList.add("conv-batch--error");
            appendToolErrorBadge(lastToolBlock);
          }
          // Output-guard warning — pull the assessment out of the
          // function-local pendingAssessments map (populated in the
          // assistant branch).  Skip when the tool result was denied —
          // the ✗ denied badge already signals the deny path.
          if (!isDenied && msg.tool_call_id) {
            const pending = pendingAssessments[msg.tool_call_id];
            if (pending) {
              insertChained(_buildOutputWarningEl(pending.assessment));
              delete pendingAssessments[msg.tool_call_id];
            }
          }
        }
      } else if (msg.role === "system") {
        // First-class operator-context turn (output-guard finding, user
        // interjection, metacognitive nudge — see make_system_turn).  These
        // now FOLLOW the turn they advise as their own rows; tool-channel
        // nudges and queued interjections that used to splice into the tool
        // result render here in sequence.  `source` is the kind; `meta` the
        // structured per-kind fields (watch-result card) from /history.
        this.addSystemContext(
          msg.content || "",
          msg.source || "",
          msg.meta || null,
        );
        if (msg.event_id != null) {
          this._renderedSystemEventIds.add(String(msg.event_id));
        }
        lastToolBlock = null;
      }
    }
    // Flush any output_assessments left in the map — these correspond
    // to assistant tool_calls whose tool result row didn't carry a
    // tool_call_id (legacy / migrated rows pre-dating the wire-format
    // addition).  Render the warning under the tool div itself rather
    // than dropping the safety information silently.
    const leftoverIds = Object.keys(pendingAssessments);
    for (let p = 0; p < leftoverIds.length; p++) {
      const leftover = pendingAssessments[leftoverIds[p]];
      if (!leftover) continue;
      leftover.toolDiv.insertAdjacentElement(
        "afterend",
        _buildOutputWarningEl(leftover.assessment),
      );
    }
    this._attachRetryToLastAssistant();
    this.scrollToBottom();
    // Focus the input so keyboard users land on the next-action target
    // after replay finishes — but only when this is the focused pane,
    // there's no pending approval competing for focus, and an input
    // element actually exists.  Skipping when not the focused pane
    // avoids stealing focus from another tab the user is interacting
    // with while a background replay completes.
    if (
      this._host.isFocused(this) &&
      !this.pendingApproval &&
      this.inputEl &&
      !this.busy
    ) {
      try {
        this.inputEl.focus({ preventScroll: true });
      } catch (_) {
        this.inputEl.focus();
      }
    }
    // Restore live-region semantics now that the batch build is done.
    this.messagesEl.removeAttribute("aria-busy");
  }

  _attachRetryToLastAssistant() {
    // Remove any previous retry buttons
    const old = this.messagesEl.querySelectorAll(".msg.assistant .msg-actions");
    for (let i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
    // Find the last assistant message with content and add retry.
    // Reasoning blocks emit as .msg.reasoning (distinct modifier) so the
    // .msg.assistant selector already excludes them — no extra guard needed.
    //
    // Skip retry attachment when the most recent semantic turn is
    // tool-only — last DOM child is a .conv-batch block.  Walk back past
    // operator-context rows (the plain system bubble AND the structured
    // watch-result / guard-finding cards — every operator row carries
    // .operator-context) which FOLLOW the tool batch they advise, so the
    // guard fires even when the tool turn carried a nudge / guard finding.
    // Keying on the shared marker (not any single card class) keeps the skip
    // correct as new card kinds are added.  Without it, retry lands on a
    // stale prior assistant content bubble belonging to an earlier turn.
    let lastChild = this.messagesEl.lastElementChild;
    while (lastChild && lastChild.classList.contains("operator-context")) {
      lastChild = lastChild.previousElementSibling;
    }
    if (lastChild && lastChild.classList.contains("conv-batch")) {
      return;
    }
    const assistants = this.messagesEl.querySelectorAll(".msg.assistant");
    if (assistants.length) {
      const lastAssistant = assistants[assistants.length - 1];
      this._addRetryAction(lastAssistant);
      if (this._voiceRoles && this._voiceRoles.tts) {
        this._addTtsAction(lastAssistant);
      }
    }
  }

  announceToolBlock(items) {
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    // Drop a previous un-consumed announce so shells don't pile up.
    if (this.announcedBlockEl) {
      this.announcedBlockEl.remove();
      this.announcedBlockEl = null;
    }
    const block = document.createElement("div");
    block.className = "conv-batch conv-batch--solo";
    // Indeterminate region until the judge/gate resolves; the upgrade in
    // showInlineToolBlock clears aria-busy.
    block.setAttribute("aria-busy", "true");
    block.dataset.callIds = JSON.stringify(
      list.map((it) => it.call_id).filter(Boolean),
    );
    block.appendChild(_convApprovalHead("Evaluating", list));
    list.forEach((item) => {
      block.appendChild(buildToolDiv(item));
      const verdict =
        item.judge_verdict || item.heuristic_verdict || item.verdict;
      if (verdict) {
        block.appendChild(buildConvVerdict(verdict, { judgePending: true }));
      }
    });
    this.announcedBlockEl = block;
    this.messagesEl.appendChild(block);
    this.scrollToBottom();
    toolAnnounce(_toolAnnounceText(list));
  }

  // Hand back the early-paint shell to upgrade in place if it matches this
  // batch's call_ids, else null (caller builds fresh).  Clears the tracking
  // ref so the shell is consumed exactly once.  Matching by id set is
  // defensive — the interactive pane is strictly serial, so an announce is
  // always followed by ITS approve_request / tool_info — but it guarantees a
  // stale shell can never capture a different batch.
  _takeAnnouncedBlock(items) {
    const block = this.announcedBlockEl;
    if (!block) return null;
    this.announcedBlockEl = null;
    const want = (items || [])
      .map((it) => it.call_id)
      .filter(Boolean)
      .sort();
    let have = [];
    try {
      have = JSON.parse(block.dataset.callIds || "[]");
    } catch (_e) {
      have = [];
    }
    have = have.slice().sort();
    const matches =
      want.length === have.length && want.every((id, i) => id === have[i]);
    if (!matches) {
      block.remove(); // stale orphan — discard, caller builds fresh
      return null;
    }
    return block;
  }

  showInlineToolBlock(items, autoApproved, judgePending) {
    // Reuse the early-paint announce shell if it's for this batch (upgrade in
    // place); else build fresh.
    const announced = this._takeAnnouncedBlock(items);
    const block = announced || document.createElement("div");
    if (announced) announced.replaceChildren();
    block.removeAttribute("aria-busy");
    block.className =
      "conv-batch conv-batch--solo" + (autoApproved ? " conv-batch--auto" : "");
    if (!autoApproved) {
      block.setAttribute("role", "alertdialog");
      block.setAttribute("aria-label", "Tool approval required");
    }
    block.appendChild(
      _convApprovalHead(
        autoApproved
          ? "Tool"
          : items.length >= 2
            ? "⚠ Approval · " + items.length + " tools"
            : "⚠ Approval",
        items,
      ),
    );

    let glowRec = null;
    items.forEach((item) => {
      block.appendChild(buildToolDiv(item));
      const verdict =
        item.judge_verdict || item.heuristic_verdict || item.verdict;
      if (verdict) {
        block.appendChild(buildConvVerdict(verdict, { judgePending }));
        const rec = verdict.recommendation || "review";
        if (
          !glowRec ||
          rec === "deny" ||
          (rec === "review" && glowRec === "approve")
        ) {
          glowRec = rec;
        }
      }
    });

    if (autoApproved) {
      block.appendChild(buildConvStatus({ auto: true }));
    } else {
      const alwaysNames = items
        .filter(
          (it) =>
            it.needs_approval &&
            it.func_name &&
            it.func_name !== "__budget_override__" &&
            !it.error,
        )
        .map((it) => it.approval_label || it.func_name);
      block.dataset.alwaysNames = JSON.stringify(alwaysNames);
      const actions = buildConvActions({
        kbd: { approve: "y", deny: "n", always: "a" },
        alwaysLabel: alwaysNames.length
          ? "Always approve " + alwaysNames.join(", ")
          : "",
        glowRec,
        withFeedback: true,
        onApprove: () => this.resolveApproval(true, false, this.getFeedback()),
        onDeny: () => this.resolveApproval(false, false, this.getFeedback()),
        onAlways: () => this.resolveApproval(true, true, this.getFeedback()),
      });
      block.appendChild(actions);
      this.pendingApproval = true;
      this.approvalBlockEl = block;
      this.inputEl.disabled = true;
      this.sendBtn.disabled = true;
      const fb = actions.querySelector(".conv-feedback");
      requestAnimationFrame(() => {
        if (fb) fb.focus();
      });
    }

    if (!announced) this.messagesEl.appendChild(block);
    this.scrollToBottom();
  }

  resolveApproval(approved, always, feedback, skipPost) {
    if (!this.approvalBlockEl) return;
    this.pendingApproval = false;

    const actions = this.approvalBlockEl.querySelector(".conv-actions");
    if (actions) actions.remove();

    this.approvalBlockEl.appendChild(
      buildConvStatus({ approved, always, feedback: feedback || "" }),
    );
    this.approvalBlockEl.classList.add(
      approved ? "conv-batch--approved" : "conv-batch--denied",
    );
    this.approvalBlockEl = null;

    this.inputEl.disabled = false;
    this.sendBtn.disabled = this.busy;
    this.inputEl.focus();

    // POST to server (skip when server already resolved, e.g. timeout).
    if (!skipPost) {
      authFetch(
        this._base +
          "/v1/api/workstreams/" +
          encodeURIComponent(this.wsId) +
          "/approve",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            approved: approved,
            feedback: feedback || null,
            always: !!always,
          }),
        },
      ).catch((err) => {
        this.addErrorMessage("Connection error: " + err.message);
      });
    }

    this.scrollToBottom();
  }

  appendToolOutput(callId, name, output, isError) {
    const escapedId = callId ? CSS.escape(callId) : "";
    let target = escapedId
      ? this.messagesEl.querySelector(
          '.conv-row[data-call-id="' + escapedId + '"]',
        )
      : null;
    if (!target) {
      const blocks = this.messagesEl.querySelectorAll(".conv-batch");
      if (!blocks.length) return;
      const block = blocks[blocks.length - 1];
      const tools = block.querySelectorAll(".conv-row");
      for (let i = tools.length - 1; i >= 0; i--) {
        if (tools[i].dataset.funcName === name) {
          target = tools[i];
          break;
        }
      }
      if (!target && tools.length) target = tools[tools.length - 1];
    }
    if (!target) return;

    // Remove the streaming output element for this tool
    let streamEl = null;
    if (escapedId) {
      streamEl = this.messagesEl.querySelector(
        '.tool-output-stream[data-call-id="' + escapedId + '"]',
      );
    } else {
      const next = target.nextElementSibling;
      if (next && next.classList.contains("tool-output-stream")) {
        streamEl = next;
      }
    }
    if (streamEl) streamEl.remove();

    const stripped = stripAnsi(output || "").trim();
    if (!stripped) return;

    // Skip rendering for denied/blocked tool results — the ✗ denied
    // badge from resolveApproval already shows the denial reason; the
    // SSE tool_result event would otherwise duplicate the text.  Mirror
    // the guard in the history-replay path (the live path used to be
    // safe because no tool_result event was ever emitted for denied
    // items, but we now emit one so _tool_error_flags gets set).
    const parentBlock = target.closest(".conv-batch");
    const isDenied =
      (parentBlock && parentBlock.classList.contains("conv-batch--denied")) ||
      /^Denied by user/.test(stripped) ||
      /^Blocked/.test(stripped);
    if (isDenied) return;

    // Detect structured media output and render interactive embed
    if (!isError) {
      const media = tryParseMedia(stripped);
      if (media) {
        const embed = buildMediaEmbed(media, stripped);
        target.after(embed);
        this.scrollToBottom();
        return;
      }
    }

    // Detect structured MCP error envelope and render an interactive
    // consent / re-consent / forbidden / operator card.  The existing
    // ✗ error badge from appendToolErrorBadge still fires below.
    if (isError) {
      const mcpErr = tryParseMcpError(stripped);
      if (mcpErr) {
        if (
          parentBlock &&
          !parentBlock.classList.contains("conv-batch--denied")
        ) {
          parentBlock.classList.add("conv-batch--error");
          appendToolErrorBadge(parentBlock);
        }
        target.after(
          buildMcpErrorEmbed(mcpErr, stripped, (s) =>
            this._host.onConsentDetected(s),
          ),
        );
        this.scrollToBottom();
        return;
      }
    }

    const out = renderToolOutput(stripped, isError);

    // Mark the parent approval block as errored
    if (
      isError &&
      parentBlock &&
      !parentBlock.classList.contains("conv-batch--denied")
    ) {
      parentBlock.classList.add("conv-batch--error");
      appendToolErrorBadge(parentBlock);
    }

    if (out.textContent.split("\n").length > 10) {
      makeCollapsible(out);
    }

    target.after(out);
    this.scrollToBottom();
  }

  sendMessage() {
    const text = this.inputEl.value.trim();
    if (!text) return;

    if (text.startsWith("/")) {
      if (this.busy) return; // commands not allowed while busy
      // /rewind and /retry were lifted to path-keyed endpoints (#549);
      // reroute hand-typed ones so they don't 400 against /command.
      const parts = text.split(/\s+/);
      const cmdWord = parts[0].toLowerCase();
      if (cmdWord === "/rewind") {
        const n = parseInt(parts[1], 10);
        if (!Number.isInteger(n) || n < 1) {
          this.addErrorMessage(
            "Usage: /rewind <N> — N must be a positive integer",
          );
          this.composer.clear();
          return;
        }
        this._rewindToTurns(n);
        this.composer.clear();
        return;
      }
      if (cmdWord === "/retry") {
        this._retryLast();
        this.composer.clear();
        return;
      }
      authFetch(this._base + "/v1/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: text, ws_id: this.wsId }),
      });
      this.addUserMessage(text);
      this.composer.clear();
      return;
    }

    const isBusy = this.busy;
    let queuedEl = null;
    const snap = this.attachments.snapshot();

    if (isBusy) {
      // Server re-parses the !!! prefix to set queue priority — the
      // optimistic bubble strips it for display.
      let displayText = text;
      let priority = "notice";
      if (text.startsWith("!!!")) {
        displayText = text.slice(3).trimStart();
        priority = "important";
      }
      this.removeEmptyState();
      queuedEl = this.queue.addQueuedMessage(displayText, priority);
    } else {
      this.setBusy(true);
      this.addUserMessage(text, snap.attachments);
    }
    this.composer.clear();

    authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/send",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          attachment_ids: snap.attachment_ids,
        }),
      },
    )
      .then((r) => {
        return r.json();
      })
      .then((data) => {
        if (data.status === "queued" && data.msg_id) {
          // queuedEl-present path: bind() handles the three known races
          // (pre-bind dismiss, promote sweep raced ahead, normal accept).
          // queuedEl-absent path: client thought it was idle but the
          // server saw a live worker (SSE state_change hadn't arrived
          // yet). Flip busy so subsequent sends queue correctly; the
          // optimistic user bubble is already in the log and the server
          // still delivers the message on worker drain — accept the
          // small UX gap (no in-UI dismiss for THIS message).
          if (queuedEl) this.queue.bind(queuedEl, data.msg_id);
          else this.setBusy(true);
          this.attachments.consume(
            data.attached_ids,
            data.dropped_attachment_ids,
          );
        } else if (data.status === "busy") {
          if (queuedEl) this.queue.remove(queuedEl);
          this.addErrorMessage("Server is busy. Please wait.");
          if (!isBusy) this.setBusy(false);
        } else if (data.status === "queue_full") {
          if (queuedEl) this.queue.remove(queuedEl);
          this.addErrorMessage("Message queue full. Please wait.");
        } else if (data.status === "attachments_busy") {
          // Attachments can't ride a queued user turn — server held the
          // chips' reservations long enough to bounce the request and
          // released them. Surface to the user; chips stay in the
          // composer so they can retry once the assistant finishes.
          if (queuedEl) this.queue.remove(queuedEl);
          this.addErrorMessage(
            "Attachments can't be sent while the assistant is working. " +
              "Send a text-only message now, or wait and resend with attachments.",
          );
        } else {
          this.attachments.consume(
            data.attached_ids,
            data.dropped_attachment_ids,
          );
        }
      })
      .catch((err) => {
        if (queuedEl) this.queue.remove(queuedEl);
        this.addErrorMessage("Connection error: " + err.message);
        if (!isBusy) this.setBusy(false);
      });
  }

  cancelGeneration() {
    if (!this.busy || !this.wsId || this.stopBtn.disabled) return;
    const isForce = this.stopBtn.dataset.forceCancel === "true";
    this.stopBtn.disabled = true;
    authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/cancel",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: isForce }),
      },
    )
      .then(() => {
        if (isForce) {
          // Force cancel abandons the worker — transition immediately.
          // Clear timeouts to prevent stale timers firing on next send.
          if (this._cancelTimeout) {
            clearTimeout(this._cancelTimeout);
            this._cancelTimeout = null;
          }
          if (this._forceTimeout) {
            clearTimeout(this._forceTimeout);
            this._forceTimeout = null;
          }
          this.addInfoMessage("Force stopped. Previous generation abandoned.");
          this.setBusy(false);
        }
      })
      .catch((err) => {
        this.addErrorMessage("Cancel error: " + err.message);
        this.stopBtn.disabled = false;
      });
  }
}

// Build a structured ``.msg.guard-finding`` card for an ``output_guard``
// operator-context system turn.  ``meta`` carries the structured finding
// ``{flags, risk_level, annotations, redacted}``.  Reuses the tool-result
// warning chip (``_buildOutputWarningEl``) for the risk / flags / redaction
// header so the operator-context finding speaks the same visual vocabulary,
// then appends the annotations (matched-pattern detail) — which the inline
// tool chip omits to stay terse.  All text via textContent.
function _buildGuardFindingBubble(meta) {
  const el = document.createElement("div");
  el.className = "msg guard-finding operator-context";
  el.setAttribute("role", "article");
  el.setAttribute("data-ts-role", "output_guard");
  el.setAttribute("aria-label", "output guard");
  el.appendChild(_buildOutputWarningEl(meta));
  const anns = Array.isArray(meta.annotations) ? meta.annotations : [];
  for (let i = 0; i < anns.length; i++) {
    const a = document.createElement("div");
    a.className = "msg-guard-annotation";
    a.textContent = String(anns[i]);
    el.appendChild(a);
  }
  return el;
}

// Shared output-warning DOM builder — used by both replayHistory
// (saved-workstream rendering) and the live appendToolOutput path
// via showOutputWarning.  Single source of truth keeps the two
// surfaces from drifting on role / class / escape semantics.
function _buildOutputWarningEl(assessment) {
  // Thin wrapper over the shared builder (risk normalized -> unknown=medium).
  return buildConvWarning(assessment);
}

function _formatRuntime(item) {
  let mins = 0;
  if (typeof item.runtime_minutes === "number") {
    mins = Math.round(item.runtime_minutes);
  } else if (typeof item.runtime_ticks === "number") {
    mins = Math.round(item.runtime_ticks / 600000000);
  }
  if (!mins) return "";
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return h > 0 ? h + "h " + m + "m" : m + "m";
}

function _redactApiKeys(text) {
  // Query-string style: api_key=VALUE
  let redacted = text.replace(
    /(?:api_key|apiKey|api-key|token)=[^&\s"]+/g,
    function (m) {
      return m.split("=")[0] + "=***";
    },
  );
  // JSON style: "api_key": "VALUE"
  redacted = redacted.replace(
    /(["'](?:api_key|apiKey|api-key|token)["']\s*:\s*["'])([^"']*)(['"])/gi,
    "$1***$3",
  );
  return redacted;
}

function _tryPrettyJson(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  return _redactApiKeys(JSON.stringify(obj, null, 2));
}

function buildMediaCard(item) {
  const card = document.createElement("div");
  card.className = "media-card";

  // Thumbnail
  const thumbUrl = item.thumbnail_url || item.image_url || "";
  if (thumbUrl) {
    const img = document.createElement("img");
    img.className = "media-card-thumb";
    img.loading = "lazy";
    img.alt = item.title || item.name || "Media thumbnail";
    img.onerror = function () {
      this.style.display = "none";
    };
    img.src = thumbUrl;
    card.appendChild(img);
  }

  // Info container
  const info = document.createElement("div");
  info.className = "media-card-info";

  // Title (Year)
  const title = document.createElement("div");
  title.className = "media-card-title";
  let titleText = item.title || item.name || "Untitled";
  if (item.year || item.production_year) {
    titleText += " (" + (item.year || item.production_year) + ")";
  }
  title.textContent = titleText;
  info.appendChild(title);

  // Metadata line: type, runtime, genres
  const metaParts = [];
  if (item.type || item.media_type) {
    metaParts.push(item.type || item.media_type);
  }
  const runtime = _formatRuntime(item);
  if (runtime) metaParts.push(runtime);
  if (item.genres && item.genres.length) {
    metaParts.push(item.genres.join(", "));
  }
  if (metaParts.length) {
    const meta = document.createElement("div");
    meta.className = "media-card-meta";
    meta.textContent = metaParts.join(" \u00b7 ");
    info.appendChild(meta);
  }

  card.appendChild(info);
  return card;
}

function buildPlayButton(media) {
  const btn = document.createElement("button");
  btn.className = "media-play-btn";
  btn.type = "button";
  btn.dataset.streamUrl = media.stream_url || "";
  btn.dataset.hlsUrl = media.hls_url || "";
  btn.dataset.audioOnly =
    media.audio_only === true ||
    (media.container &&
      /^(mp3|flac|ogg|aac|wma|wav|m4a|opus)$/i.test(media.container))
      ? "true"
      : "false";
  btn.dataset.directStream =
    media.supports_direct_play || media.supports_direct_stream
      ? "true"
      : "false";

  btn.setAttribute(
    "aria-label",
    "Play " + (media.title || media.name || "media"),
  );

  const icon = document.createElement("span");
  icon.textContent = "\u25b6";
  btn.appendChild(icon);
  const label = document.createElement("span");
  label.textContent = "Play";
  btn.appendChild(label);
  return btn;
}

function buildMediaResultsList(results, totalCount) {
  const container = document.createElement("div");
  container.className = "media-results-list";

  for (let i = 0; i < results.length; i++) {
    const item = results[i];
    const row = document.createElement("div");
    row.className = "media-result-row";

    // Small thumbnail
    const thumbUrl = item.thumbnail_url || item.image_url || "";
    if (thumbUrl) {
      const img = document.createElement("img");
      img.className = "media-result-thumb";
      img.loading = "lazy";
      img.alt = item.name || item.title || "Media thumbnail";
      img.onerror = function () {
        this.style.display = "none";
      };
      img.src = thumbUrl;
      row.appendChild(img);
    }

    // Title (Year)
    const titleSpan = document.createElement("span");
    titleSpan.className = "media-result-title";
    let titleText = item.name || item.title || "Untitled";
    if (item.year || item.production_year) {
      titleText += " (" + (item.year || item.production_year) + ")";
    }
    titleSpan.textContent = titleText;
    row.appendChild(titleSpan);

    // Metadata: type, runtime or season info
    const metaParts = [];
    if (item.type || item.media_type) {
      metaParts.push(item.type || item.media_type);
    }
    const runtime = _formatRuntime(item);
    if (runtime) metaParts.push(runtime);
    if (item.season_name) metaParts.push(item.season_name);
    if (
      typeof item.index_number === "number" &&
      typeof item.parent_index_number === "number"
    ) {
      metaParts.push(
        "S" +
          String(item.parent_index_number).padStart(2, "0") +
          "E" +
          String(item.index_number).padStart(2, "0"),
      );
    }
    if (metaParts.length) {
      const metaSpan = document.createElement("span");
      metaSpan.className = "media-result-meta";
      metaSpan.textContent = " \u00b7 " + metaParts.join(" \u00b7 ");
      row.appendChild(metaSpan);
    }

    container.appendChild(row);
  }

  // "showing X of Y results" footer
  if (typeof totalCount === "number" && totalCount > results.length) {
    const count = document.createElement("div");
    count.className = "media-results-count";
    count.textContent =
      "showing " + results.length + " of " + totalCount + " results";
    container.appendChild(count);
  }

  return container;
}

function _mcpErrorCategory(code) {
  if (code === "mcp_consent_required" || code === "mcp_insufficient_scope") {
    return "actionable";
  }
  if (
    code === "mcp_token_undecryptable_key_unknown" ||
    code === "mcp_oauth_url_insecure"
  ) {
    return "operator";
  }
  // Default for any other mcp_*_forbidden / unrecognised mcp_ code.
  return "forbidden";
}

function _mcpErrorTitle(err) {
  switch (err.code) {
    case "mcp_consent_required":
      return "Consent required";
    case "mcp_insufficient_scope":
      return "Re-consent required (insufficient scope)";
    case "mcp_token_undecryptable_key_unknown":
    case "mcp_oauth_url_insecure":
      return "Operator action required";
    default:
      return "Forbidden";
  }
}

// ---------------------------------------------------------------------------
// Conversational rendering helpers — tool output, media embeds, MCP-error
// cards.  Used only by the Pane.  The approval-card builders (rows, verdict,
// warning, actions, status) were unified into the shared conversation.js
// (steps 5e.1/5e.2); buildToolDiv + the wrappers below delegate to them so
// both panes emit the SAME .conv-* card.  The .tool-output / media result
// subsystem stays interactive-only (live stream / collapse / embeds).
// ---------------------------------------------------------------------------

// Converged approval-card head strip (kicker + summary).  Module-level so
// both showInlineToolBlock and announceToolBlock (and replay) build the same
// head as the coordinator's buildConvBatchShell, without a fresh shell element.
function _convApprovalHead(kickerText, items) {
  const head = document.createElement("div");
  head.className = "conv-batch-head";
  const k = document.createElement("span");
  k.className = "conv-batch-kicker";
  k.textContent = kickerText;
  head.appendChild(k);
  const first =
    items[0] && (items[0].func_name || items[0].approval_label)
      ? items[0].func_name || items[0].approval_label
      : "tool";
  const summary = document.createElement("span");
  summary.className = "conv-batch-summary";
  summary.textContent =
    items.length >= 2 ? first + " + " + (items.length - 1) + " more" : first;
  head.appendChild(summary);
  return head;
}

function buildToolDiv(item) {
  // Tool row for the converged card: the shared call line (name + auto tag)
  // plus the bash `$ cmd` / diff preview.  data-func-name is stamped for the
  // append-time finders (buildConvRow already stamps data-call-id/-tool-name).
  const row = buildConvRow(item, {});
  row.dataset.funcName = item.func_name || "";
  row.appendChild(buildConvCmd(item));
  return row;
}

function renderVerdictBadge(verdict, judgePending) {
  // Thin wrapper over the shared builder (returns a fragment [badge, detail]).
  return buildConvVerdict(verdict, { judgePending });
}

// Append an "✗ error" pill to an approval block as a sibling of the
// existing approved/denied/auto-approved pill, so the approval verdict
// stays visible alongside the execution outcome. Idempotent — re-fires
// (live + history rerender) do not stack badges.
function appendToolErrorBadge(blockEl) {
  if (!blockEl) return;
  if (blockEl.querySelector(".conv-status--error")) return;
  const errBadge = document.createElement("div");
  errBadge.setAttribute("role", "status");
  errBadge.className = "conv-status conv-status--error";
  errBadge.textContent = "✗ error";
  blockEl.appendChild(errBadge);
}

function makeCollapsible(el) {
  el.classList.add("collapsed");
  el.setAttribute("tabindex", "0");
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", "Tool output (collapsed). Activate to expand.");
  const handler = function () {
    this.classList.remove("collapsed");
    this.removeAttribute("tabindex");
    this.removeAttribute("role");
    this.removeAttribute("aria-label");
  };
  el.addEventListener("click", handler);
  el.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      handler.call(this);
    }
  });
}

function tryParseMedia(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  if (obj && typeof obj.stream_url === "string") return obj;
  if (obj && obj.name && obj.type && obj.id) return obj;
  if (obj && Array.isArray(obj.results) && obj.results.length > 0) return obj;
  if (obj && Array.isArray(obj.sessions)) return obj;
  return null;
}

function renderToolOutput(stripped, isError) {
  const out = document.createElement("div");
  out.className = "tool-output" + (isError ? " tool-output-error" : "");
  if (!isError) {
    const pretty = _tryPrettyJson(stripped);
    if (pretty) {
      out.textContent = pretty;
      return out;
    }
  }
  out.textContent = _redactApiKeys(stripped);
  return out;
}

function buildMediaEmbed(media, rawJson) {
  const wrapper = document.createElement("div");
  wrapper.className = "media-embed";

  if (media.stream_url) {
    const card = buildMediaCard(media);
    card.querySelector(".media-card-info").appendChild(buildPlayButton(media));
    wrapper.appendChild(card);
  } else if (media.results) {
    wrapper.appendChild(
      buildMediaResultsList(media.results, media.total_count),
    );
  } else if (media.sessions) {
    wrapper.appendChild(buildMediaResultsList(media.sessions, null));
  } else if (media.name && media.type && media.id) {
    wrapper.appendChild(buildMediaCard(media));
  }

  // Collapsed raw JSON for inspection (with redacted API keys)
  const raw = document.createElement("div");
  raw.className = "tool-output";
  raw.textContent = _tryPrettyJson(rawJson) || _redactApiKeys(rawJson);
  makeCollapsible(raw);
  wrapper.appendChild(raw);

  return wrapper;
}

function tryParseMcpError(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  if (!obj || typeof obj !== "object") return null;
  const err = obj.error;
  if (!err || typeof err !== "object") return null;
  if (typeof err.code !== "string" || err.code.indexOf("mcp_") !== 0)
    return null;
  return err;
}

function buildMcpErrorEmbed(err, rawJson, onConsent) {
  const category = _mcpErrorCategory(err.code);
  const wrapper = document.createElement("div");
  wrapper.className = "mcp-error-card mcp-error-" + category;

  const icon = document.createElement("div");
  icon.className = "mcp-error-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "⚠";
  wrapper.appendChild(icon);

  const body = document.createElement("div");
  body.className = "mcp-error-body";

  const title = document.createElement("div");
  title.className = "mcp-error-title";
  title.textContent = _mcpErrorTitle(err);
  body.appendChild(title);

  if (err.detail) {
    const detail = document.createElement("div");
    detail.className = "mcp-error-detail";
    detail.textContent = String(err.detail);
    body.appendChild(detail);
  }

  if (err.server) {
    const serverLine = document.createElement("div");
    serverLine.className = "mcp-error-server";
    serverLine.appendChild(document.createTextNode("server: "));
    const serverCode = document.createElement("code");
    serverCode.textContent = String(err.server);
    serverLine.appendChild(serverCode);
    body.appendChild(serverLine);
  }

  if (Array.isArray(err.scopes_required) && err.scopes_required.length) {
    const scopesLine = document.createElement("div");
    scopesLine.className = "mcp-error-scopes";
    scopesLine.appendChild(document.createTextNode("scopes: "));
    for (let i = 0; i < err.scopes_required.length; i++) {
      const pill = document.createElement("span");
      pill.className = "mcp-scope-pill";
      pill.textContent = String(err.scopes_required[i]);
      scopesLine.appendChild(pill);
    }
    body.appendChild(scopesLine);
  }

  if (category === "actionable") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mcp-error-action-btn";
    const serverLabel = err.server ? String(err.server) : "server";
    btn.textContent =
      err.code === "mcp_insufficient_scope"
        ? "Re-consent with new scopes →"
        : "Connect to " + serverLabel + " →";
    btn.setAttribute(
      "aria-label",
      err.code === "mcp_insufficient_scope"
        ? "Re-consent with new scopes for " + serverLabel
        : "Connect to " + serverLabel,
    );
    btn.addEventListener("click", function () {
      const consentUrl = err.consent_url;
      if (!consentUrl || typeof consentUrl !== "string") {
        // Defensive: should always be present per the dispatcher.  If a
        // path forgets to include it the user can still connect via the
        // Settings panel (gear icon).
        showToast("No consent URL available; open Settings to connect.");
        return;
      }
      // Defence-in-depth: reject anything that isn't path-relative to
      // the dispatcher's known prefix. ``_build_consent_url`` always
      // emits ``/v1/api/mcp/oauth/start?...`` — a non-prefix value
      // would indicate a future producer drift or a compromised
      // dispatcher, and ``window.open("javascript:...")`` would be
      // catastrophic. Never rely on the producer-side guarantee alone.
      if (!consentUrl.startsWith("/v1/api/mcp/oauth/start")) {
        showToast("Invalid consent URL");
        return;
      }
      const sep = consentUrl.indexOf("?") >= 0 ? "&" : "?";
      const url =
        consentUrl +
        sep +
        "return_url=" +
        encodeURIComponent(window.location.href);
      window.open(url, "_blank", "noopener");
    });
    body.appendChild(btn);
    if (onConsent) onConsent(err.server);
  }

  wrapper.appendChild(body);

  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "raw payload";
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.className = "tool-output";
  pre.textContent = _tryPrettyJson(rawJson) || _redactApiKeys(rawJson);
  details.appendChild(pre);
  wrapper.appendChild(details);

  return wrapper;
}

// ---------------------------------------------------------------------------
// createInteractivePane — the console L-shell factory.
//
// Mirrors createCoordinatorPane: it builds one embedded interactive Pane into a
// pane-host root and returns the lifecycle controller the shell drives
// (connect / deactivate / destroy / onLogin).  Transport is node-proxied — an
// interactive session lives on a cluster node, so every request is prefixed
// with /node/{nodeId} (the LOCALITY invariant).  No children/tasks affordance:
// an interactive persona has no spawn tools (that sidebar is the coordinator's).
// ---------------------------------------------------------------------------
function createInteractivePane(root, wsId, opts) {
  opts = opts || {};
  if (!wsId) {
    const missing = document.createElement("div");
    missing.className = "msg error";
    missing.textContent = "Missing ws_id.";
    root.replaceChildren(missing);
    return null;
  }
  // A console pane proxies a node-hosted session, so its transport base is
  // /node/{id}; opts.base ("" by default) is the pass-through for a future
  // local standalone mount.
  const base = opts.nodeId
    ? "/node/" + encodeURIComponent(opts.nodeId)
    : opts.base || "";

  let active = false;
  let connected = false;
  let recoverTimer = null;

  const host = {
    // Workstream name from the Tier-1 cluster snapshot the shell owns, else the
    // name the opener passed; Pane falls back to a short id.
    getWsName(id) {
      try {
        const TS = window.TS_APP;
        const cs = TS && TS.getClusterState && TS.getClusterState();
        if (cs && cs.nodes) {
          for (const nid in cs.nodes) {
            for (const ws of cs.nodes[nid].workstreams || []) {
              if (ws.id === id) return ws.name || ws.title || null;
            }
          }
        }
      } catch (e) {
        /* snapshot not ready yet */
      }
      return opts.name || null;
    },
    // Only the visible tab steals focus — never yank the caret into a
    // backgrounded pane mid background-replay.
    isFocused() {
      return active;
    },
    // Native EventSource auto-reconnect handles transient drops (connectSSE
    // deliberately does not close the source on error).  Guard the terminal
    // case: if the source is genuinely CLOSED after a beat, open a fresh
    // same-ws stream.  No global ws-list refetch — a console pane owns one ws.
    onStreamError(pane) {
      if (recoverTimer) clearTimeout(recoverTimer);
      recoverTimer = setTimeout(() => {
        recoverTimer = null;
        if (
          !pane.evtSource ||
          pane.evtSource.readyState === EventSource.CLOSED
        ) {
          pane.connectSSE(pane.wsId);
        }
      }, 5000);
    },
    // The --skip-permissions banner lands in the pane's own slim header.
    warningTarget(pane) {
      return pane.messagesEl;
    },
    // MCP re-consent surfaces inline in the pane card; the console has no
    // settings-gear badge to drive (a future console consent surface can hook
    // here).
    onConsentDetected() {},
  };

  const pane = new Pane(wsId, {
    embedded: true,
    base,
    host,
    onClose: opts.onClose,
  });
  root.appendChild(pane.el);

  return {
    wsId: wsId,
    pane: pane,
    // First activation opens the Tier-2 stream (REST history first, then live);
    // re-activations just re-mark focus.  Idempotent — the shell calls it on
    // every tab switch.
    connect() {
      active = true;
      if (!connected) {
        connected = true;
        pane.showEmptyState();
        pane._loadHistoryThenConnect(wsId);
      }
    },
    // Tab backgrounded — stop stealing focus, keep the stream live.
    deactivate() {
      active = false;
    },
    // Re-auth fan-out: reconnect the stream.
    onLogin() {
      if (connected) pane._loadHistoryThenConnect(wsId);
    },
    // Full teardown — close the stream + all timers/recording/tts, drop the
    // recovery timer, detach the DOM.  A backgrounded pane must not leak an
    // upstream node connection.
    destroy() {
      active = false;
      connected = false;
      if (recoverTimer) {
        clearTimeout(recoverTimer);
        recoverTimer = null;
      }
      pane.disconnectSSE();
      if (pane.el && pane.el.parentNode) {
        pane.el.parentNode.removeChild(pane.el);
      }
    },
  };
}

// --- Shared-module exports -------------------------------------------------
// ES module: the shell (shared_static/shell.js) imports the factory directly in
// BOTH deployments (console + standalone), so there is no window bridge — step 6
// retired the classic standalone app.js path that read window.InteractivePane.
export { Pane as InteractivePane, createInteractivePane };
