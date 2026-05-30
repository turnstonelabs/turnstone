// ===========================================================================
//  turnstone server UI — app.js
//  Split-pane layout with per-workstream Pane instances and binary layout tree
// ===========================================================================

// ===========================================================================
//  1. Pane class — per-workstream UI state
// ===========================================================================

let _paneCounter = 0;

class Pane {
  constructor(wsId) {
    this.id = "p" + ++_paneCounter;
    this.wsId = wsId || null;
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
    this.retryDelay = 1000;
    this.model = "";
    this.modelAlias = "";
    this._lastStatusEvt = null;
    this._historyLoadToken = 0;
    this._cancelTimeout = null;
    this._forceTimeout = null;
    this._pendingEditSend = null;
    this._createDOM();
  }

  reset() {
    this.currentAssistantEl = null;
    this.currentReasoningEl = null;
    this.contentBuffer = "";
    this.setBusy(false);
    this.pendingApproval = false;
    this.approvalBlockEl = null;
    this._pendingEditSend = null;
    this.inputEl.disabled = false;
    this.attachments.clearChips();
  }

  updateWsName() {
    const nameEl = this.headerEl.querySelector(".pane-ws-name");
    if (nameEl) {
      nameEl.textContent = this.wsId
        ? (workstreams[this.wsId] && workstreams[this.wsId].name) ||
          this.wsId.substring(0, 8)
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
    // Thin .msg.user.system-nudge marker rendered as the anchor for
    // wake-driven reminder bubbles.  Replaces the previously-invisible
    // synthetic empty user turn with a visible-but-subtle DOM element so
    // the bubble below it lands in the right place even when the wake
    // fires long after the user's last real message.
    this.removeEmptyState();
    const el = document.createElement("div");
    el.className = "msg user system-nudge";
    el.setAttribute("data-source", "system_nudge");
    el.setAttribute("aria-label", "system nudge");
    el.textContent = "system nudge";
    this.messagesEl.appendChild(el);
    return el;
  }

  addUserReminder(reminders, source) {
    // Render each metacognitive reminder as its own bubble immediately
    // BELOW the user message it advises — semantically the reminder is
    // a hint to the model right before the assistant turn.  Always
    // called AFTER the corresponding addUserMessage (live: optimistic
    // local render ran before the SSE event arrived; replay:
    // replayHistory renders the user message first), so "most recent
    // .msg.user" is always THIS turn's bubble — insertAdjacentElement
    // afterend drops the reminder directly below it.  When no .msg.user
    // exists at all (e.g. a non-originating tab receiving a reminder
    // before any user turn has rendered) we append; the next /history
    // reload corrects any anchor anomaly.
    //
    // ``source === "system_nudge"`` is the wake-driven case: render
    // a thin .msg.user.system-nudge marker first and anchor below it.
    // ``watch_triggered`` reminders branch off into a structured
    // .msg.watch-result card.
    this.removeEmptyState();
    let anchor;
    if (source === "system_nudge") {
      anchor = this.addSystemNudgeMarker();
    } else {
      const userBubbles = this.messagesEl.querySelectorAll(
        ".msg.user:not(.system-nudge)",
      );
      anchor = userBubbles.length ? userBubbles[userBubbles.length - 1] : null;
    }
    for (let i = 0; i < reminders.length; i++) {
      const r = reminders[i] || {};
      const el =
        r.type === "watch_triggered"
          ? _buildWatchResultBubble(r)
          : _buildDefaultReminderBubble(r);
      if (anchor) {
        anchor.insertAdjacentElement("afterend", el);
        // Anchor advances so multiple reminders stack below the user
        // message in queued order (rather than each landing
        // immediately-after the user msg, which would reverse them).
        anchor = el;
      } else {
        this.messagesEl.appendChild(el);
      }
    }
    this.scrollToBottom(true);
  }

  addToolReminder(reminders, toolCallId) {
    // Render each metacognitive tool-channel reminder (tool_error /
    // repeat) as the same yellow themed bubble used for user-channel
    // reminders, anchored below the .ts-approval block that produced
    // the tool result.  toolCallId is the live-path anchor (SSE event
    // carries it); during replay it's an empty string and we fall back
    // to "last .ts-approval block in messagesEl", which is correct
    // because messages render in order — the assistant block carrying
    // the tool batch is always the most recent approval block by the
    // time we hit the tool message that owns the reminder.  Tool-channel
    // reminders also branch on r.type so a watch_triggered drained at
    // the tool seam (channel="any") renders the structured card.
    this.removeEmptyState();
    let anchor = null;
    if (toolCallId) {
      const escapedId = CSS.escape(toolCallId);
      const toolEl = this.messagesEl.querySelector(
        '.ts-approval-tool[data-call-id="' + escapedId + '"]',
      );
      if (toolEl) {
        anchor = toolEl.closest(".ts-approval");
      }
    }
    if (!anchor) {
      const blocks = this.messagesEl.querySelectorAll(".ts-approval");
      if (blocks.length) anchor = blocks[blocks.length - 1];
    }
    for (let i = 0; i < reminders.length; i++) {
      const r = reminders[i] || {};
      const el =
        r.type === "watch_triggered"
          ? _buildWatchResultBubble(r)
          : _buildDefaultReminderBubble(r);
      if (anchor) {
        anchor.insertAdjacentElement("afterend", el);
        anchor = el;
      } else {
        this.messagesEl.appendChild(el);
      }
    }
    this.scrollToBottom(true);
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
    const inp = this.approvalBlockEl.querySelector(".ts-approval-feedback");
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
            '.ts-approval-tool[data-call-id="' + escapedId + '"]',
          )
        : null;
      if (!target) {
        const blocks = this.messagesEl.querySelectorAll(".ts-approval");
        if (!blocks.length) return;
        const block = blocks[blocks.length - 1];
        const tools = block.querySelectorAll(
          '.ts-approval-tool[data-func-name="bash"]',
        );
        target = tools.length ? tools[tools.length - 1] : null;
        if (!target) {
          const allTools = block.querySelectorAll(".ts-approval-tool");
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
      '.ts-approval-tool[data-call-id="' + escapedId + '"]',
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
      '.verdict-badge[data-call-id="' + escapedId + '"]',
    );
    if (!badge) {
      // Badge no longer in DOM (tool block replaced by output) — show
      // a toast so the user still sees the late-arriving verdict.
      const conf = Math.round((verdict.confidence || 0) * 100);
      const rec = verdict.recommendation || "review";
      const func = verdict.func_name || "";
      showToast(
        "Judge verdict for " + func + ": " + rec + " (" + conf + "%)",
        rec === "approve" ? "success" : rec === "deny" ? "error" : "warning",
      );
      return;
    }

    const risk = verdict.risk_level || "medium";
    badge.className = "verdict-badge verdict-" + risk + " ts-verdict-badge";
    badge.setAttribute("data-risk", risk);

    const riskEl = badge.querySelector(".verdict-risk");
    const recEl = badge.querySelector(".verdict-rec");
    const confEl = badge.querySelector(".verdict-conf");
    if (riskEl) riskEl.textContent = risk.toUpperCase();
    if (recEl) recEl.textContent = verdict.recommendation || "review";
    if (confEl)
      confEl.textContent = Math.round((verdict.confidence || 0) * 100) + "%";

    const spinner = badge.querySelector(".verdict-judge-spinner");
    if (spinner) spinner.remove();

    const detail = badge.nextElementSibling;
    if (detail && detail.classList.contains("verdict-detail")) {
      const summaryEl = detail.querySelector(".verdict-summary");
      const reasonEl = detail.querySelector(".verdict-reasoning");
      const tierEl = detail.querySelector(".verdict-tier");
      if (summaryEl) summaryEl.textContent = verdict.intent_summary || "";
      if (reasonEl) reasonEl.textContent = verdict.reasoning || "";
      if (tierEl)
        tierEl.textContent =
          (verdict.tier || "llm") +
          " tier" +
          (verdict.judge_model ? " | " + verdict.judge_model : "");
      let evidenceEl = detail.querySelector(".verdict-evidence");
      if (verdict.evidence && verdict.evidence.length) {
        if (!evidenceEl) {
          evidenceEl = document.createElement("div");
          evidenceEl.className = "verdict-evidence";
          const tierDiv = detail.querySelector(".verdict-tier");
          if (tierDiv) detail.insertBefore(evidenceEl, tierDiv);
          else detail.appendChild(evidenceEl);
        }
        evidenceEl.replaceChildren(
          ...verdict.evidence.map(function (e) {
            const div = document.createElement("div");
            div.textContent = "\u2022 " + e;
            return div;
          }),
        );
      } else if (evidenceEl) {
        evidenceEl.remove();
      }
    }

    this.updateVerdictGlow(verdict.recommendation);
  }

  updateVerdictGlow(recommendation) {
    if (!this.approvalBlockEl) return;
    const prompt = this.approvalBlockEl.querySelector(".ts-approval-body");
    if (!prompt) return;

    // Collect all verdict badges currently visible in this approval block
    const badges = this.approvalBlockEl.querySelectorAll(".verdict-badge");
    let worst = recommendation;
    for (let i = 0; i < badges.length; i++) {
      const recEl = badges[i].querySelector(".verdict-rec");
      if (recEl) {
        const r = recEl.textContent;
        if (r === "deny") {
          worst = "deny";
          break;
        }
        if (r === "review" && worst !== "deny") worst = "review";
      }
    }

    prompt.classList.remove(
      "ts-verdict-glow--approve",
      "ts-verdict-glow--deny",
      "ts-verdict-glow--review",
    );
    if (worst === "approve") prompt.classList.add("ts-verdict-glow--approve");
    else if (worst === "deny") prompt.classList.add("ts-verdict-glow--deny");
    else prompt.classList.add("ts-verdict-glow--review");
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
    this.el.dataset.paneId = this.id;

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

    // Pane header (visible only in multi-pane mode)
    this.headerEl = document.createElement("div");
    this.headerEl.className = "pane-header";

    const wsName = document.createElement("span");
    wsName.className = "pane-ws-name";
    wsName.textContent = this.wsId
      ? (workstreams[this.wsId] && workstreams[this.wsId].name) ||
        this.wsId.substring(0, 8)
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
    let evtUrl = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/events";
    if (this._lastEventId) {
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
      // Focused-pane orthogonal trigger: refetch the global
      // workstream list so a workstream evicted while we were
      // disconnected gets reassigned across panes.  The reassignment
      // branch's explicit disconnectSSE + connectSSE on the new
      // wsId is correct (a different workstream genuinely needs a
      // fresh stream, not a same-stream replay).
      if (this.id === focusedPaneId) {
        this._refetchWorkstreamsAndReassign();
      }
      // Non-focused panes: native EventSource reconnect handles them
      // transparently — no per-pane retry needed.  The 30 s exp-backoff
      // ceiling on retryDelay is preserved inside
      // _refetchWorkstreamsAndReassign for the focused-pane path.
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
    this._refetchHistory(wsId, token).finally(() => {
      if (token === this._historyLoadToken) this.connectSSE(wsId);
    });
  }

  async _refetchHistory(wsId, token) {
    // Fetch conversation history over REST. Used for first paint (before
    // connecting SSE) and to re-render after a clear_ui signal (rewind /
    // retry / resume / open). The FETCH is wrapped (network/parse failure
    // → empty pane); the render is deliberately OUTSIDE the catch so a
    // render bug surfaces loudly instead of being masked as an empty pane.
    const id = wsId || this.wsId;
    let data = null;
    try {
      const r = await authFetch(
        "/v1/api/workstreams/" + encodeURIComponent(id) + "/history",
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

  _refetchWorkstreamsAndReassign() {
    // Lifted from the pre-PR-D ``onerror`` body.  Triggered when the
    // focused pane sees its EventSource enter the error state — pulls
    // the authoritative workstream list and reassigns stale wsIds.
    // Survives the onerror refactor as a separate concern from the
    // SSE reconnect mechanics: native EventSource handles the same-
    // workstream reconnect; this handles the workstream-evicted-
    // during-disconnect recovery.
    fetch("/v1/api/workstreams")
      .then((r) => {
        if (r.status === 401) {
          showLogin();
          return;
        }
        return r.json().then((data) => {
          workstreams = {};
          (data.workstreams || []).forEach((ws) => {
            workstreams[ws.ws_id] = { name: ws.name, state: ws.state };
          });
          renderTabBar();
          // Two passes: (1) reassign stale panes, (2) reconnect any
          // that ended up in CLOSED state.  Native reconnect covers
          // CONNECTING -> OPEN transitions transparently.
          const remaining = Object.keys(workstreams);
          if (!remaining.length) {
            showDashboard();
            return;
          }
          const usedWsIds = {};
          for (let pid in panes) {
            if (panes[pid].wsId && workstreams[panes[pid].wsId])
              usedWsIds[panes[pid].wsId] = true;
          }
          for (let pid2 in panes) {
            const p2 = panes[pid2];
            if (p2.wsId && !workstreams[p2.wsId]) {
              let newWsId = null;
              for (let ri = 0; ri < remaining.length; ri++) {
                if (!usedWsIds[remaining[ri]]) {
                  newWsId = remaining[ri];
                  break;
                }
              }
              if (newWsId) {
                p2.disconnectSSE();
                // Different workstream → drop saved id; replay is
                // per-ws so an id from ws-A is meaningless on ws-B.
                p2._lastEventId = null;
                p2.wsId = newWsId;
                usedWsIds[newWsId] = true;
                while (p2.messagesEl.firstChild)
                  p2.messagesEl.removeChild(p2.messagesEl.firstChild);
                p2.showEmptyState();
                p2.updateWsName();
              }
              // else: more panes than workstreams — leave pane stale,
              // connectSSE below picks it up or stays disconnected.
            }
          }
          // Pass 2: reconnect any pane whose EventSource ended up
          // truly CLOSED (not just transient — native reconnect
          // handles CONNECTING / OPEN).
          for (let pid3 in panes) {
            const p3 = panes[pid3];
            if (pid3 === focusedPaneId) currentWsId = p3.wsId;
            const dead =
              !p3.evtSource || p3.evtSource.readyState === EventSource.CLOSED;
            if (dead && p3.wsId && workstreams[p3.wsId]) {
              setTimeout(
                ((pp) => {
                  return () => {
                    pp._loadHistoryThenConnect(pp.wsId);
                  };
                })(p3),
                this.retryDelay,
              );
            }
          }
          this.retryDelay = Math.min(this.retryDelay * 2, 30000);
        });
      })
      .catch(() => {
        // Fetch failed (network) — schedule a same-pane reconnect
        // fallback in case the EventSource is genuinely dead.
        setTimeout(() => {
          if (
            !this.evtSource ||
            this.evtSource.readyState === EventSource.CLOSED
          ) {
            this.connectSSE(this.wsId);
          }
        }, this.retryDelay);
        this.retryDelay = Math.min(this.retryDelay * 2, 30000);
      });
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
          if (this.id === focusedPaneId && !this.pendingApproval) {
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

      case "plan_review":
        showPlanDialog(evt.content);
        break;

      case "plan_resolved":
        // Plan was resolved on another client (or by server-initiated cancel).
        // Only act if our modal is for this pane's workstream.
        if (_planWsId === this.wsId) {
          dismissPlanDialog(evt.feedback);
        }
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

      case "user_reminder":
        // Metacognitive nudges — render as their own bubble below the
        // user message they advise (semantically: a hint to the model
        // right before its turn).  The originating tab's optimistic
        // addUserMessage already ran when the user clicked send, so by
        // the time this SSE event arrives the just-sent user bubble is
        // at the bottom of messagesEl and addUserReminder's "anchor to
        // most recent .msg.user" lookup finds it correctly; the
        // insertAdjacentElement('afterend', el) call drops the bubble
        // immediately below.
        //
        // Wake-driven reminders (``evt.source === "system_nudge"``)
        // render the thin .msg.user.system-nudge marker first and
        // anchor below it, so non-originating tabs see the same shape
        // the originating tab does.
        //
        // Multi-tab caveat (non-wake): the server emits no user_message
        // SSE event for real user input today, so a non-originating tab
        // sees the reminder without a paired user-message render — the
        // anchor falls on a stale prior user bubble, mis-positioning
        // the reminder.  The next /history reload corrects it.
        // Acceptable cost for stage 1; closing the gap is a follow-up
        // that adds a user_message SSE event.
        if (Array.isArray(evt.reminders) && evt.reminders.length) {
          this.addUserReminder(evt.reminders, evt.source || "");
        }
        break;

      case "tool_reminder":
        // Metacognitive tool-channel nudge (tool_error / repeat) —
        // render as the same yellow themed bubble used for user-channel
        // reminders, anchored below the .ts-approval block whose tool
        // result triggered the batch's reminder.  evt.tool_call_id
        // identifies the specific tool element; addToolReminder walks
        // up to its parent approval block and inserts the bubble
        // immediately after.
        if (Array.isArray(evt.reminders) && evt.reminders.length) {
          this.addToolReminder(evt.reminders, evt.tool_call_id || "");
        }
        break;

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
            document.getElementById("ui-header").appendChild(warn);
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
              "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/send",
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

  _retryLast() {
    if (this.busy) return;
    // Path-keyed retry (#549). Truncation + re-dispatch happen
    // server-side; the clear_ui event drives the history refetch.
    authFetch(
      "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/retry",
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
      "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/rewind",
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
      "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/rewind",
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
          // Wake-driven empty user turn: render the thin marker
          // (replaces the previously-skipped synthetic empty bubble)
          // and anchor reminder bubbles below it.
          this.addUserReminder(
            Array.isArray(msg.reminders) ? msg.reminders : [],
            "system_nudge",
          );
          lastToolBlock = null;
          continue;
        }
        // addUserMessage first so addUserReminder's "anchor to most
        // recent .msg.user" lookup finds THIS message's bubble (not the
        // previous user message's, which would associate the reminder
        // with the wrong turn).  addUserReminder then drops the bubble
        // immediately below the just-rendered user message via
        // insertAdjacentElement('afterend', el).
        this.addUserMessage(msg.content || "", msg.attachments || null);
        if (Array.isArray(msg.reminders) && msg.reminders.length) {
          this.addUserReminder(msg.reminders);
        }
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
              "msg ts-approval ts-approval--inline " +
              (wasDenied ? "denied" : "approved");
            msg.tool_calls.forEach((tc) => {
              const div = document.createElement("div");
              div.className = "ts-approval-tool";
              div.dataset.funcName = tc.name;
              div.dataset.callId = tc.id || "";
              const nameEl = document.createElement("div");
              nameEl.className = "tool-name";
              nameEl.textContent = tc.name;
              div.appendChild(nameEl);
              const cmd = document.createElement("div");
              cmd.className = "tool-cmd";
              try {
                const args = JSON.parse(tc.arguments);
                if (tc.name === "bash") {
                  const preview = Object.values(args)[0] || "";
                  const dollar = document.createElement("span");
                  dollar.className = "dollar";
                  dollar.textContent = "$ ";
                  cmd.append(dollar, String(preview));
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
                  cmd.textContent = parts.join("\n");
                }
              } catch (e) {
                // Defensive: never let a non-string `arguments` (or a
                // parse failure) escalate into a render-aborting throw.
                cmd.textContent = String(tc.arguments || "").substring(0, 100);
              }
              div.appendChild(cmd);
              // Verdict badge — anchor to THIS tool's row (div) rather
              // than the whole block, so a multi-tool batch with one
              // flagged call doesn't drift the badge above unrelated
              // calls.  Same renderVerdictBadge helper as live; pass
              // judgePending=false because any verdict on replay is
              // final — no spinner.
              if (tc.verdict) {
                div.appendChild(renderVerdictBadge(tc.verdict, false));
              }
              block.appendChild(div);
              // Output-guard finding — defer insertion until the tool
              // result lands so the warning anchors under the output
              // (mirrors live showOutputWarning placement).  Stash in
              // a function-local map keyed by call_id so the
              // role==="tool" branch below can pick it up; legacy rows
              // missing tool_call_id are flushed at end-of-replay.
              if (
                tc.output_assessment &&
                tc.output_assessment.risk_level &&
                tc.output_assessment.risk_level !== "none"
              ) {
                pendingAssessments[tc.id || ""] = {
                  assessment: tc.output_assessment,
                  toolDiv: div,
                };
              }
            });
            const badge = document.createElement("div");
            badge.setAttribute("role", "status");
            if (wasDenied) {
              badge.className = "ts-approval-badge ts-approval-badge--denied";
              badge.textContent = "\u2717 denied";
            } else {
              badge.className = "ts-approval-badge ts-approval-badge--approved";
              badge.textContent = "\u2713 approved";
            }
            block.appendChild(badge);
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
          // Anchor the rendered output to the specific .ts-approval-tool
          // element matching this result's tool_call_id — mirrors the
          // live appendToolOutput path so multi-tool batches show
          // [hdr A][out A][hdr B][out B] rather than [A][B][out A][out B].
          // Falls back to "before badge" when tool_call_id is absent
          // (legacy rows pre-dating the wire-format addition).
          let resultTarget = null;
          if (msg.tool_call_id) {
            resultTarget = lastToolBlock.querySelector(
              '.ts-approval-tool[data-call-id="' +
                CSS.escape(msg.tool_call_id) +
                '"]',
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
              const bdg = lastToolBlock.querySelector(".ts-approval-badge");
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
          if (isToolError && !lastToolBlock.classList.contains("denied")) {
            lastToolBlock.classList.add("error");
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
        // Tool-channel metacog reminders (tool_error / repeat) attach
        // to the LAST tool message in a batch; on replay we render the
        // bubble immediately below the .ts-approval block that owns
        // the tool result.  addToolReminder's empty-toolCallId fallback
        // resolves to "last .ts-approval block" — which is exactly
        // lastToolBlock here.
        if (Array.isArray(msg.reminders) && msg.reminders.length) {
          this.addToolReminder(msg.reminders, "");
        }
        // Queued user messages spliced into the last tool-result envelope
        // (Seam 1) replay as proper user bubbles after the tool block.
        // ``decorate_history_messages`` extracts the user_interjection
        // advisory from the persisted envelope and the wire layer projects
        // it onto ``msg.advisories``; rendering through ``addUserMessage``
        // matches the live shape a Seam 2/3 message would produce.  The
        // walk/filter is shared via ``replayAdvisoriesAfterTool`` in
        // ``shared/utils.js`` so coord and interactive can never drift on
        // advisory-shape filtering.
        replayAdvisoriesAfterTool(msg.advisories, (text) => {
          this.addUserMessage(text, null);
          lastToolBlock = null;
        });
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
      this.id === focusedPaneId &&
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
    // tool-only — last DOM child is a .ts-approval block.  Walk back
    // past .user-reminder bubbles (added via addToolReminder /
    // addUserReminder AFTER the .ts-approval block they advise) so the
    // guard fires correctly even when the tool turn carried a metacog
    // reminder.  Without this skip, retry lands on a stale prior
    // assistant content bubble belonging to an earlier turn.
    let lastChild = this.messagesEl.lastElementChild;
    while (lastChild && lastChild.classList.contains("user-reminder")) {
      lastChild = lastChild.previousElementSibling;
    }
    if (lastChild && lastChild.classList.contains("ts-approval")) {
      return;
    }
    const assistants = this.messagesEl.querySelectorAll(".msg.assistant");
    if (assistants.length) {
      this._addRetryAction(assistants[assistants.length - 1]);
    }
  }

  showInlineToolBlock(items, autoApproved, judgePending) {
    const block = document.createElement("div");
    block.className =
      "msg ts-approval ts-approval--inline" + (autoApproved ? " approved" : "");
    if (!autoApproved) {
      block.setAttribute("role", "alertdialog");
      block.setAttribute("aria-label", "Tool approval required");
    }

    // Track the highest-priority recommendation for glow
    let glowRec = null;

    items.forEach((item) => {
      block.appendChild(buildToolDiv(item));
      // Render verdict badge if present.  Server emits the heuristic
      // verdict under ``heuristic_verdict`` (matches the api/server_schemas
      // PendingApprovalItem shape).  Falls back to the legacy ``verdict``
      // key in case a stale SSE payload arrives mid-deploy.
      const heuristic = item.heuristic_verdict || item.verdict;
      if (heuristic) {
        block.appendChild(renderVerdictBadge(heuristic, judgePending));
        const rec = heuristic.recommendation || "review";
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
      const badge = document.createElement("div");
      badge.setAttribute("role", "status");
      badge.className = "ts-approval-badge ts-approval-badge--approved";
      badge.textContent = "\u2713 auto-approved";
      block.appendChild(badge);
    } else {
      const prompt = document.createElement("div");
      prompt.className = "ts-approval-body";

      // Apply verdict glow on initial heuristic verdict
      if (glowRec) {
        if (glowRec === "approve")
          prompt.classList.add("ts-verdict-glow--approve");
        else if (glowRec === "deny")
          prompt.classList.add("ts-verdict-glow--deny");
        else prompt.classList.add("ts-verdict-glow--review");
      }

      const alwaysNames = items
        .filter((it) => {
          return (
            it.needs_approval &&
            it.func_name &&
            it.func_name !== "__budget_override__" &&
            !it.error
          );
        })
        .map((it) => {
          return it.approval_label || it.func_name;
        });
      block.dataset.alwaysNames = JSON.stringify(alwaysNames);
      const alwaysTitle = alwaysNames.length
        ? "Always approve " + alwaysNames.join(", ")
        : "Always approve this tool type";

      const actionsDiv = document.createElement("div");
      actionsDiv.className = "ts-approval-actions";

      const approveBtn = document.createElement("button");
      approveBtn.className = "ts-approval-btn ts-approval-btn--approve";
      approveBtn.append(makeKeyLabel("y", "Approve"));
      approveBtn.onclick = () => {
        this.resolveApproval(true, false, this.getFeedback());
      };
      actionsDiv.appendChild(approveBtn);

      const denyBtn = document.createElement("button");
      denyBtn.className = "ts-approval-btn ts-approval-btn--deny";
      denyBtn.append(makeKeyLabel("n", "Deny"));
      denyBtn.onclick = () => {
        this.resolveApproval(false, false, this.getFeedback());
      };
      actionsDiv.appendChild(denyBtn);

      if (alwaysNames.length) {
        const alwaysBtn = document.createElement("button");
        alwaysBtn.className = "ts-approval-btn ts-approval-btn--always";
        alwaysBtn.title = alwaysTitle;
        alwaysBtn.setAttribute("aria-label", alwaysTitle);
        alwaysBtn.append(makeKeyLabel("a", "Always"));
        alwaysBtn.onclick = () => {
          this.resolveApproval(true, true, this.getFeedback());
        };
        actionsDiv.appendChild(alwaysBtn);
      }

      prompt.appendChild(actionsDiv);

      const fbInput = document.createElement("input");
      fbInput.type = "text";
      fbInput.className = "ts-approval-feedback";
      fbInput.placeholder = "feedback (optional)";
      prompt.appendChild(fbInput);

      block.appendChild(prompt);
      this.pendingApproval = true;
      this.approvalBlockEl = block;
      this.inputEl.disabled = true;
      this.sendBtn.disabled = true;
      requestAnimationFrame(() => {
        fbInput.focus();
      });
    }

    this.messagesEl.appendChild(block);
    this.scrollToBottom();
  }

  resolveApproval(approved, always, feedback, skipPost) {
    if (!this.approvalBlockEl) return;
    this.pendingApproval = false;

    // Remove prompt
    const prompt = this.approvalBlockEl.querySelector(".ts-approval-body");
    if (prompt) prompt.remove();

    // Add badge
    const badge = document.createElement("div");
    badge.setAttribute("role", "status");
    if (approved) {
      badge.className = "ts-approval-badge ts-approval-badge--approved";
      let label = "\u2713 approved";
      if (always) {
        const raw = this.approvalBlockEl.dataset.alwaysNames;
        const names = raw ? JSON.parse(raw) : [];
        label = names.length
          ? "\u2713 always approve " + names.join(", ")
          : "\u2713 always approve";
      }
      badge.textContent = feedback ? label + ": " + feedback : label;
      this.approvalBlockEl.classList.add("approved");
    } else {
      badge.className = "ts-approval-badge ts-approval-badge--denied";
      badge.textContent = "\u2717 denied" + (feedback ? ": " + feedback : "");
      this.approvalBlockEl.classList.add("denied");
    }
    this.approvalBlockEl.appendChild(badge);
    this.approvalBlockEl = null;

    // Re-enable input
    this.inputEl.disabled = false;
    this.sendBtn.disabled = this.busy;
    this.inputEl.focus();

    // POST to server (skip when server already resolved, e.g. timeout)
    if (!skipPost) {
      authFetch(
        "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/approve",
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
          '.ts-approval-tool[data-call-id="' + escapedId + '"]',
        )
      : null;
    if (!target) {
      const blocks = this.messagesEl.querySelectorAll(".ts-approval");
      if (!blocks.length) return;
      const block = blocks[blocks.length - 1];
      const tools = block.querySelectorAll(".ts-approval-tool");
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
    const parentBlock = target.closest(".ts-approval");
    const isDenied =
      (parentBlock && parentBlock.classList.contains("denied")) ||
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
        if (parentBlock && !parentBlock.classList.contains("denied")) {
          parentBlock.classList.add("error");
          appendToolErrorBadge(parentBlock);
        }
        target.after(buildMcpErrorEmbed(mcpErr, stripped));
        this.scrollToBottom();
        return;
      }
    }

    const out = renderToolOutput(stripped, isError);

    // Mark the parent approval block as errored
    if (isError && parentBlock && !parentBlock.classList.contains("denied")) {
      parentBlock.classList.add("error");
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
      authFetch("/v1/api/command", {
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
      "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/send",
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
      "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/cancel",
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

// Build a structured ``.msg.watch-result`` card for a
// ``watch_triggered`` reminder — full-width treatment with command
// preview header + shell output body + poll counter footer.  Mirrors
// the coordinator pane's buildWatchResultBubble; first-pass functional
// rendering only.  All text goes through textContent so shell output
// containing angle brackets / scripts / steering bytes renders inertly.
function _buildWatchResultBubble(r) {
  const el = document.createElement("div");
  el.className = "msg watch-result";
  el.setAttribute("role", "article");
  el.setAttribute("data-ts-role", "watch");
  el.setAttribute("aria-label", "watch");
  const header = document.createElement("div");
  header.className = "msg-watch-header";
  header.textContent =
    "watch" + (r.watch_name ? " · " + String(r.watch_name) : "");
  el.appendChild(header);
  if (r.command) {
    const cmd = document.createElement("div");
    cmd.className = "msg-watch-cmd";
    cmd.textContent = "$ " + String(r.command);
    el.appendChild(cmd);
  }
  const body = document.createElement("pre");
  body.className = "msg-watch-body";
  body.textContent = r.text || "";
  el.appendChild(body);
  if (r.poll_count != null && r.max_polls != null) {
    const footer = document.createElement("div");
    footer.className = "msg-watch-footer";
    const finalSuffix = r.is_final ? " · final" : "";
    footer.textContent =
      "poll " + String(r.poll_count) + "/" + String(r.max_polls) + finalSuffix;
    el.appendChild(footer);
  }
  return el;
}

// Default ``.msg.user-reminder`` bubble — yellow themed advisory used
// for every metacog nudge other than ``watch_triggered``.
function _buildDefaultReminderBubble(r) {
  const el = document.createElement("div");
  el.className = "msg user-reminder";
  const body = document.createElement("div");
  body.className = "msg-body";
  const labelEl = document.createElement("span");
  labelEl.className = "msg-user-reminder-label";
  labelEl.textContent =
    "metacognition" + (r.type ? " · " + String(r.type) : "");
  const textEl = document.createElement("span");
  textEl.className = "msg-user-reminder-text";
  textEl.textContent = r.text || "";
  body.appendChild(labelEl);
  body.appendChild(textEl);
  el.appendChild(body);
  return el;
}

// Shared output-warning DOM builder — used by both replayHistory
// (saved-workstream rendering) and the live appendToolOutput path
// via showOutputWarning.  Single source of truth keeps the two
// surfaces from drifting on role / class / escape semantics.
function _buildOutputWarningEl(assessment) {
  const risk = (assessment && assessment.risk_level) || "medium";
  const flags = (assessment && assessment.flags) || [];
  const warning = document.createElement("div");
  warning.className = "output-warning output-warning-" + risk;
  // role="status" (polite) rather than "alert" (assertive) — these
  // are findings, not emergencies; the assertive announcement live
  // would interrupt the user mid-typing on a high-risk match, which
  // is more disruptive than informative.
  warning.setAttribute("role", "status");
  const labelEl = document.createElement("span");
  labelEl.className = "output-warning-label";
  labelEl.textContent = "⚠ " + String(risk).toUpperCase();
  warning.appendChild(labelEl);
  if (flags.length) {
    warning.appendChild(document.createTextNode(" " + flags.join(", ")));
  }
  if (assessment && assessment.redacted) {
    const redacted = document.createElement("span");
    redacted.className = "output-warning-redacted";
    redacted.textContent = " (credentials redacted)";
    warning.appendChild(redacted);
  }
  // LLM-judge attribution — tier "llm" means the judge returned a verdict
  // for this output (it may have escalated, agreed with, or cleared a
  // heuristic-positive), NOT that it owns the displayed risk. Show the
  // judge's own verdict + confidence so the operator can tell a regex match
  // from a model judgement and weigh any dissent. Mirrors the intent-verdict
  // badge's tier/confidence vocabulary.
  if (assessment && assessment.tier === "llm") {
    const tierEl = document.createElement("span");
    tierEl.className = "output-warning-tier";
    let t = "⚖ LLM";
    // Show the judge's OWN verdict when it differs from the displayed
    // (merged) risk — e.g. regex flagged MEDIUM but the judge said none.
    // Same-verdict cases stay terse ("⚖ LLM · 88%").
    if (assessment.judge_risk && assessment.judge_risk !== risk) {
      t += ": " + assessment.judge_risk;
    }
    if (assessment.confidence > 0) {
      t += " · " + Math.round(assessment.confidence * 100) + "%";
    }
    if (assessment.judge_model) t += " · " + assessment.judge_model;
    tierEl.textContent = t;
    warning.appendChild(tierEl);
  }
  // Rationale — the judge's one-line reasoning, surfaced as a muted second
  // line so the finding explains itself instead of showing a bare flag
  // list. Block element wraps below the header row.
  if (assessment && assessment.reasoning) {
    const reasonEl = document.createElement("div");
    reasonEl.className = "output-warning-reasoning";
    reasonEl.textContent = assessment.reasoning;
    warning.appendChild(reasonEl);
  }
  return warning;
}

// ===========================================================================
//  2. Layout tree + rendering
// ===========================================================================

const panes = {};
let focusedPaneId = null;
let splitRoot = null;
const MAX_PANES = 6;

function getFocusedPane() {
  return panes[focusedPaneId] || null;
}

function setFocusedPane(paneId) {
  if (focusedPaneId === paneId) return;
  // Remove focused class from old pane
  if (focusedPaneId && panes[focusedPaneId]) {
    panes[focusedPaneId].el.classList.remove("focused");
  }
  focusedPaneId = paneId;
  if (panes[paneId]) {
    panes[paneId].el.classList.add("focused");
    currentWsId = panes[paneId].wsId;
    renderTabBar();
  }
}

function createPane(wsId) {
  const p = new Pane(wsId);
  panes[p.id] = p;
  return p;
}

function updatePaneHeaders() {
  const root = document.getElementById("split-root");
  const leafCount = countLeaves(splitRoot);
  if (leafCount > 1) {
    root.classList.add("multi-pane");
  } else {
    root.classList.remove("multi-pane");
  }
  // Hide tab-bar split button when already in multi-pane mode
  const splitBtn = document.getElementById("split-btn");
  if (splitBtn) {
    if (leafCount > 1) {
      splitBtn.classList.add("hidden");
    } else {
      splitBtn.classList.remove("hidden");
    }
  }
}

function splitFocusedPane() {
  if (focusedPaneId) splitPane(focusedPaneId, "horizontal");
}

// --- Tree helpers ---

function findLeafAndParent(node, paneId, parent, childIndex) {
  if (!node) return null;
  if (node.type === "leaf") {
    if (node.pane.id === paneId) {
      return { node: node, parent: parent, childIndex: childIndex };
    }
    return null;
  }
  // split
  for (let i = 0; i < node.children.length; i++) {
    const result = findLeafAndParent(node.children[i], paneId, node, i);
    if (result) return result;
  }
  return null;
}

function countLeaves(node) {
  if (!node) return 0;
  if (node.type === "leaf") return 1;
  let count = 0;
  for (let i = 0; i < node.children.length; i++) {
    count += countLeaves(node.children[i]);
  }
  return count;
}

function getFirstLeaf(node) {
  if (!node) return null;
  if (node.type === "leaf") return node.pane;
  return getFirstLeaf(node.children[0]);
}

function replaceNode(tree, target, replacement) {
  if (tree === target) return replacement;
  if (tree.type === "split") {
    for (let i = 0; i < tree.children.length; i++) {
      if (tree.children[i] === target) {
        tree.children[i] = replacement;
        return tree;
      }
      const result = replaceNode(tree.children[i], target, replacement);
      if (result !== tree.children[i]) {
        tree.children[i] = result;
        return tree;
      }
    }
  }
  return tree;
}

function splitPane(paneId, direction) {
  if (countLeaves(splitRoot) >= MAX_PANES) return;
  // Guard: viewport too narrow/short to fit another pane
  const root = document.getElementById("split-root");
  const minDim = direction === "horizontal" ? 200 : 150;
  const available =
    direction === "horizontal" ? root.clientWidth : root.clientHeight;
  if (available < minDim * 2 + 4) {
    showToast("Not enough space to split");
    return;
  }
  const found = findLeafAndParent(splitRoot, paneId, null, -1);
  if (!found) return;

  // Find a workstream not already shown in any pane
  const wsIds = Object.keys(workstreams);
  let newWsId = null;
  for (let i = 0; i < wsIds.length; i++) {
    let inUse = false;
    for (let pid in panes) {
      if (panes[pid].wsId === wsIds[i]) {
        inUse = true;
        break;
      }
    }
    if (!inUse) {
      newWsId = wsIds[i];
      break;
    }
  }
  if (!newWsId) {
    showToast("No unused workstreams \u2014 create one first");
    return;
  }

  const newPane = createPane(newWsId);
  const newLeaf = { type: "leaf", pane: newPane };
  const newSplit = {
    type: "split",
    direction: direction,
    children: [found.node, newLeaf],
    ratio: 0.5,
  };

  splitRoot = replaceNode(splitRoot, found.node, newSplit);
  renderLayout();
  setFocusedPane(newPane.id);
  newPane.showEmptyState();
  newPane._loadHistoryThenConnect(newWsId);
}

function closePane(paneId) {
  if (countLeaves(splitRoot) <= 1) return;
  let found = findLeafAndParent(splitRoot, paneId, null, -1);
  if (!found || !found.parent) {
    // paneId is the root leaf — shouldn't happen if count > 1
    // but handle: root must be a split
    if (splitRoot.type === "split") {
      // Find which child contains our pane
      for (let ci = 0; ci < splitRoot.children.length; ci++) {
        const childFound = findLeafAndParent(
          splitRoot.children[ci],
          paneId,
          splitRoot,
          ci,
        );
        if (childFound) {
          found = childFound;
          break;
        }
      }
    }
    if (!found || !found.parent) return;
  }

  // Sibling is the other child
  const siblingIdx = found.childIndex === 0 ? 1 : 0;
  const sibling = found.parent.children[siblingIdx];

  // Replace parent split with sibling
  splitRoot = replaceNode(splitRoot, found.parent, sibling);

  // Cleanup the closed pane
  const closedPane = panes[paneId];
  if (closedPane) {
    closedPane.disconnectSSE();
    delete panes[paneId];
  }

  // If focused pane was closed, focus first available
  if (focusedPaneId === paneId) {
    const first = getFirstLeaf(splitRoot);
    if (first) {
      focusedPaneId = null; // reset so setFocusedPane triggers
      setFocusedPane(first.id);
    }
  }

  renderLayout();
}

function renderLayout() {
  const root = document.getElementById("split-root");

  // Save scroll positions before clearing
  const scrollPositions = {};
  for (let pid in panes) {
    scrollPositions[pid] = panes[pid].messagesEl.scrollTop;
  }

  // Clear and rebuild
  root.replaceChildren();
  if (splitRoot) {
    _renderLayoutNode(splitRoot, root);
  }

  // Restore scroll positions
  for (let pid2 in panes) {
    if (scrollPositions[pid2] !== undefined) {
      panes[pid2].messagesEl.scrollTop = scrollPositions[pid2];
    }
  }

  updatePaneHeaders();
  saveLayout();
}

function _renderLayoutNode(node, container) {
  if (node.type === "leaf") {
    container.appendChild(node.pane.el);
    return;
  }

  // split node
  const splitContainer = document.createElement("div");
  splitContainer.className = "split-container split-" + node.direction;

  const child0 = document.createElement("div");
  child0.className = "split-child";
  child0.style.flex = String(node.ratio);
  _renderLayoutNode(node.children[0], child0);
  splitContainer.appendChild(child0);

  const handle = document.createElement("div");
  handle.className = "split-handle";
  handle.setAttribute("role", "separator");
  handle.setAttribute("tabindex", "0");
  handle.setAttribute(
    "aria-orientation",
    node.direction === "horizontal" ? "vertical" : "horizontal",
  );
  handle.setAttribute("aria-valuenow", Math.round(node.ratio * 100));
  handle.setAttribute("aria-valuemin", "10");
  handle.setAttribute("aria-valuemax", "90");
  handle.setAttribute(
    "aria-label",
    node.direction === "horizontal"
      ? "Resize panes horizontally"
      : "Resize panes vertically",
  );
  splitContainer.appendChild(handle);

  const child1 = document.createElement("div");
  child1.className = "split-child";
  child1.style.flex = String(1 - node.ratio);
  _renderLayoutNode(node.children[1], child1);
  splitContainer.appendChild(child1);

  container.appendChild(splitContainer);
  setupDragHandle(handle, node, [child0, child1]);
}

function _dragBounds(node, handle) {
  // Compute min/max ratio from container size and CSS min dimensions
  const container = handle.parentElement;
  const totalSize =
    node.direction === "horizontal"
      ? container.clientWidth
      : container.clientHeight;
  const minPx = node.direction === "horizontal" ? 200 : 150; // match CSS min-width/min-height
  const handlePx = 4;
  const usable = totalSize - handlePx;
  const minRatio = usable > 0 ? Math.max(0.05, minPx / usable) : 0.1;
  const maxRatio = usable > 0 ? Math.min(0.95, 1 - minPx / usable) : 0.9;
  return { minRatio: minRatio, maxRatio: maxRatio, totalSize: totalSize };
}

function _applyRatio(node, children, handle, ratio) {
  node.ratio = ratio;
  children[0].style.flex = String(ratio);
  children[1].style.flex = String(1 - ratio);
  if (handle) {
    handle.setAttribute("aria-valuenow", Math.round(ratio * 100));
  }
}

function setupDragHandle(handle, node, children) {
  handle.addEventListener("pointerdown", function (e) {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    const startRatio = node.ratio;
    const bounds = _dragBounds(node, handle);
    const startPos = node.direction === "horizontal" ? e.clientX : e.clientY;
    document.body.style.cursor =
      node.direction === "horizontal" ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";

    const onMove = function (e2) {
      const delta =
        (node.direction === "horizontal" ? e2.clientX : e2.clientY) - startPos;
      const newRatio = Math.max(
        bounds.minRatio,
        Math.min(bounds.maxRatio, startRatio + delta / bounds.totalSize),
      );
      _applyRatio(node, children, handle, newRatio);
    };
    const onUp = function () {
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      saveLayout();
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });

  // Keyboard resizing (arrow keys)
  handle.addEventListener("keydown", function (e) {
    const bounds = _dragBounds(node, handle);
    const step = e.shiftKey ? 0.1 : 0.02;
    let delta = 0;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") delta = step;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") delta = -step;
    else if (e.key === "Home") delta = -(node.ratio - bounds.minRatio);
    else if (e.key === "End") delta = bounds.maxRatio - node.ratio;
    else return;
    e.preventDefault();
    const newRatio = Math.max(
      bounds.minRatio,
      Math.min(bounds.maxRatio, node.ratio + delta),
    );
    _applyRatio(node, children, handle, newRatio);
    saveLayout();
  });
}

// ===========================================================================
//  3. Layout persistence
// ===========================================================================

function serializeLayout(node) {
  if (!node) return null;
  if (node.type === "leaf") {
    return { type: "leaf", wsId: node.pane.wsId };
  }
  return {
    type: "split",
    direction: node.direction,
    ratio: node.ratio,
    children: [
      serializeLayout(node.children[0]),
      serializeLayout(node.children[1]),
    ],
  };
}

function deserializeLayout(data, _seen) {
  if (!_seen) _seen = {};
  if (!data) return null;
  if (data.type === "leaf") {
    if (!data.wsId || !workstreams[data.wsId] || _seen[data.wsId]) return null;
    if (Object.keys(panes).length >= MAX_PANES) return null;
    _seen[data.wsId] = true;
    const p = createPane(data.wsId);
    return { type: "leaf", pane: p };
  }
  if (data.type === "split") {
    const left = deserializeLayout(data.children[0], _seen);
    const right = deserializeLayout(data.children[1], _seen);
    if (!left && !right) return null;
    if (!left) return right;
    if (!right) return left;
    return {
      type: "split",
      direction: data.direction || "horizontal",
      ratio: data.ratio || 0.5,
      children: [left, right],
    };
  }
  return null;
}

function saveLayout() {
  try {
    const data = serializeLayout(splitRoot);
    if (data) {
      localStorage.setItem("turnstone_split_layout", JSON.stringify(data));
    }
  } catch (e) {
    // localStorage may be unavailable
  }
}

function restoreLayout() {
  try {
    const raw = localStorage.getItem("turnstone_split_layout");
    if (!raw) return false;
    const data = JSON.parse(raw);
    const tree = deserializeLayout(data);
    if (!tree) return false;
    splitRoot = tree;
    const first = getFirstLeaf(splitRoot);
    if (first) {
      setFocusedPane(first.id);
    }
    return true;
  } catch (e) {
    return false;
  }
}

// ===========================================================================
//  3b. Pane context menu
// ===========================================================================

let _ctxMenu = null;
let _ctxCloseHandler = null;
let _ctxTriggerElement = null;

let _tabDropdown = null;
let _tabDropdownCloseHandler = null;
let _tabDropdownTrigger = null;

function showPaneContextMenu(x, y, paneId) {
  closeTabDropdown();
  closePaneContextMenu();
  _ctxTriggerElement = document.activeElement;

  const menu = document.createElement("div");
  menu.className = "pane-ctx-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Pane actions");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  const canClose = splitRoot && countLeaves(splitRoot) > 1;
  // Can split only if under pane limit AND there's an unused workstream
  const usedWs = {};
  for (let pid in panes) usedWs[panes[pid].wsId] = true;
  const hasUnused = Object.keys(workstreams).some(function (id) {
    return !usedWs[id];
  });
  const canSplit = countLeaves(splitRoot) < MAX_PANES && hasUnused;

  const items = [
    {
      label: "Split Right",
      key: "Ctrl+\\",
      disabled: !canSplit,
      action: function () {
        splitPane(paneId, "horizontal");
      },
    },
    {
      label: "Split Down",
      key: "Ctrl+Shift+\\",
      disabled: !canSplit,
      action: function () {
        splitPane(paneId, "vertical");
      },
    },
    { separator: true },
    {
      label: "Close Pane",
      key: "Ctrl+Shift+W",
      disabled: !canClose,
      action: function () {
        closePane(paneId);
      },
    },
  ];

  items.forEach(function (item) {
    if (item.separator) {
      const sep = document.createElement("div");
      sep.className = "pane-ctx-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    const btn = document.createElement("button");
    btn.className = "pane-ctx-item";
    btn.setAttribute("role", "menuitem");
    btn.setAttribute("tabindex", "-1");
    btn.disabled = !!item.disabled;
    const labelSpan = document.createElement("span");
    labelSpan.className = "pane-ctx-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    if (item.key) {
      const keySpan = document.createElement("span");
      keySpan.className = "pane-ctx-key";
      keySpan.textContent = item.key;
      btn.appendChild(keySpan);
    }
    btn.onclick = function () {
      closePaneContextMenu();
      item.action();
    };
    menu.appendChild(btn);
  });

  // Position: ensure menu stays within viewport
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  let mx = x;
  let my = y;
  if (mx + rect.width > window.innerWidth)
    mx = window.innerWidth - rect.width - 4;
  if (my + rect.height > window.innerHeight)
    my = window.innerHeight - rect.height - 4;
  if (mx < 0) mx = 4;
  if (my < 0) my = 4;
  menu.style.left = mx + "px";
  menu.style.top = my + "px";
  _ctxMenu = menu;

  // Close on click outside, Escape, Tab; arrow key navigation
  _ctxCloseHandler = function (e) {
    if (e.type === "keydown") {
      if (e.key === "Escape" || e.key === "Tab") {
        e.preventDefault();
        closePaneContextMenu();
      } else if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Home" ||
        e.key === "End"
      ) {
        e.preventDefault();
        const btns = Array.from(
          menu.querySelectorAll(".pane-ctx-item:not(:disabled)"),
        );
        if (!btns.length) return;
        const idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        else if (e.key === "ArrowUp")
          btns[(idx - 1 + btns.length) % btns.length].focus();
        else if (e.key === "Home") btns[0].focus();
        else if (e.key === "End") btns[btns.length - 1].focus();
      }
    } else if (e.type === "mousedown" && !menu.contains(e.target)) {
      closePaneContextMenu();
    }
  };
  setTimeout(function () {
    document.addEventListener("mousedown", _ctxCloseHandler);
    document.addEventListener("keydown", _ctxCloseHandler);
    // Focus first enabled item
    const first = menu.querySelector(".pane-ctx-item:not(:disabled)");
    if (first) first.focus();
  }, 0);
}

function closePaneContextMenu() {
  if (_ctxMenu) {
    _ctxMenu.remove();
    _ctxMenu = null;
  }
  if (_ctxCloseHandler) {
    document.removeEventListener("mousedown", _ctxCloseHandler);
    document.removeEventListener("keydown", _ctxCloseHandler);
    _ctxCloseHandler = null;
  }
  if (_ctxTriggerElement && document.contains(_ctxTriggerElement)) {
    _ctxTriggerElement.focus();
    _ctxTriggerElement = null;
  }
}

// ---------------------------------------------------------------------------
//  3c. Tab dropdown menu (per-tab workstream actions)
// ---------------------------------------------------------------------------

function showTabDropdown(chevronEl, wsId) {
  closePaneContextMenu();
  closeTabDropdown();
  _tabDropdownTrigger = chevronEl;
  chevronEl.setAttribute("aria-expanded", "true");

  const menu = document.createElement("div");
  menu.className = "ws-tab-dropdown";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Workstream actions");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  const isLastWs = Object.keys(workstreams).length <= 1;
  const items = [
    {
      label: "Refresh title",
      cls: "mobile-hide",
      action: function () {
        refreshWorkstreamTitle(wsId);
      },
    },
    {
      label: "Edit title",
      key: "Ctrl+Shift+E",
      action: function () {
        editWorkstreamTitle(wsId);
      },
    },
    {
      label: "Fork",
      key: "Ctrl+Shift+F",
      action: function () {
        forkWorkstream(wsId);
      },
    },
    {
      label: "Export conversation",
      action: function () {
        exportWorkstreamDownload(wsId);
      },
    },
    {
      label: "Close",
      key: "Ctrl+W",
      disabled: isLastWs,
      action: function () {
        closeWorkstream(wsId);
      },
    },
    { separator: true },
    {
      label: "Delete",
      key: "Ctrl+Shift+X",
      cls: "destructive",
      disabled: isLastWs,
      action: function () {
        confirmDeleteWorkstream(wsId);
      },
    },
  ];

  items.forEach(function (item) {
    if (item.separator) {
      const sep = document.createElement("div");
      sep.className = "ws-tab-dropdown-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    const btn = document.createElement("button");
    btn.className = "ws-tab-dropdown-item" + (item.cls ? " " + item.cls : "");
    btn.setAttribute("role", "menuitem");
    btn.setAttribute("tabindex", "-1");
    if (item.disabled) {
      btn.setAttribute("aria-disabled", "true");
      btn.setAttribute(
        "title",
        "Cannot " + item.label.toLowerCase() + " the last workstream",
      );
    }
    const labelSpan = document.createElement("span");
    labelSpan.className = "ws-tab-dropdown-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    if (item.key) {
      const keySpan = document.createElement("span");
      keySpan.className = "ws-tab-dropdown-key";
      keySpan.textContent = item.key;
      keySpan.setAttribute("aria-hidden", "true");
      btn.appendChild(keySpan);
    }
    btn.onclick = function () {
      if (this.getAttribute("aria-disabled") === "true") return;
      closeTabDropdown();
      item.action();
    };
    menu.appendChild(btn);
  });

  document.body.appendChild(menu);

  // Position below chevron, right-aligned
  const cr = chevronEl.getBoundingClientRect();
  const mr = menu.getBoundingClientRect();
  let mx = cr.right - mr.width;
  let my = cr.bottom + 2;
  if (mx < 0) mx = 4;
  if (my + mr.height > window.innerHeight) my = cr.top - mr.height - 2;
  if (mx + mr.width > window.innerWidth) mx = window.innerWidth - mr.width - 4;
  menu.style.left = mx + "px";
  menu.style.top = my + "px";
  _tabDropdown = menu;

  // Keyboard handler is mirrored by the console node-picker shim in
  // turnstone/console/server.py (search for closeHandler in _JS_PROXY_SHIM).
  // If you change the keys or filter selector here, change them there.
  _tabDropdownCloseHandler = function (e) {
    if (e.type === "keydown") {
      if (e.key === "Escape" || e.key === "Tab") {
        e.preventDefault();
        closeTabDropdown();
      } else if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Home" ||
        e.key === "End"
      ) {
        e.preventDefault();
        const btns = Array.from(menu.querySelectorAll(".ws-tab-dropdown-item"));
        if (!btns.length) return;
        const idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        // idx <= 0 covers both "first item" (wrap to last) and "no
        // current focus" (idx === -1, which would otherwise yield
        // len-2 via the modulo).  Same shape as openSettingsMenu and
        // the proxy node-picker (turnstone/console/server.py:275).
        else if (e.key === "ArrowUp")
          btns[idx <= 0 ? btns.length - 1 : idx - 1].focus();
        else if (e.key === "Home") btns[0].focus();
        else if (e.key === "End") btns[btns.length - 1].focus();
      }
    } else if (
      e.type === "mousedown" &&
      !menu.contains(e.target) &&
      e.target !== chevronEl
    ) {
      closeTabDropdown();
    }
  };
  const closeHandler = _tabDropdownCloseHandler;
  const activeMenu = menu;
  setTimeout(function () {
    if (_tabDropdown !== activeMenu || !closeHandler) return;
    document.addEventListener("mousedown", closeHandler);
    document.addEventListener("keydown", closeHandler);
    const first = activeMenu.querySelector(".ws-tab-dropdown-item");
    if (first) first.focus();
  }, 0);
}

function closeTabDropdown() {
  if (_tabDropdown) {
    _tabDropdown.remove();
    _tabDropdown = null;
  }
  if (_tabDropdownCloseHandler) {
    document.removeEventListener("mousedown", _tabDropdownCloseHandler);
    document.removeEventListener("keydown", _tabDropdownCloseHandler);
    _tabDropdownCloseHandler = null;
  }
  if (_tabDropdownTrigger) {
    _tabDropdownTrigger.setAttribute("aria-expanded", "false");
    if (document.contains(_tabDropdownTrigger)) {
      _tabDropdownTrigger.focus();
    }
    _tabDropdownTrigger = null;
  }
}

// ===========================================================================
//  4. Global state
// ===========================================================================

let workstreams = {};
let currentWsId = null;
let globalEvtSource = null;
let globalRetryDelay = 1000;
// Saved high-water mark for the manual-reconnect path (the
// EventSource constructor can't set custom headers, so the
// browser-native ``Last-Event-ID`` header is unavailable on
// reconnect — we thread it via ``?last_event_id=N`` instead).  Updated
// from ``globalEvtSource.lastEventId`` on every onmessage; native
// auto-reconnect uses the header directly on the same source object.
let globalLastEventId = null;
let dashboardVisible = false;
let _historyNavigation = false;
let _lastHealth = null;

const STATE_DISPLAY = {
  running: { symbol: "\u25b8", label: "run" },
  thinking: { symbol: "\u25cc", label: "think" },
  attention: { symbol: "\u25c6", label: "attn" },
  idle: { symbol: "\u00b7", label: "idle" },
  error: { symbol: "\u2716", label: "err" },
};

// ===========================================================================
//  5. Health polling
// ===========================================================================

function pollHealth() {
  authFetch("/health")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      pollHealth._failCount = 0;
      _lastHealth = data;
      const mcpEl = document.getElementById("mcp-status");
      if (mcpEl) {
        if (data.mcp && data.mcp.servers > 0) {
          mcpEl.textContent =
            "MCP: " +
            data.mcp.servers +
            " server" +
            (data.mcp.servers !== 1 ? "s" : "");
          mcpEl.title =
            data.mcp.resources +
            " resources \u00b7 " +
            data.mcp.prompts +
            " prompts";
          mcpEl.style.opacity = "1";
        } else {
          mcpEl.textContent = "";
          mcpEl.title = "";
          mcpEl.style.opacity = "0";
        }
      }
      const el = document.getElementById("health-indicator");
      if (!el) return;
      if (data.status === "degraded") {
        el.textContent = "backend degraded";
        el.className = "health-degraded";
        el.title =
          "Backend: " + ((data.backend && data.backend.status) || "unknown");
        el.setAttribute(
          "aria-label",
          "Backend degraded: " +
            ((data.backend && data.backend.status) || "unknown"),
        );
      } else {
        el.textContent = "";
        el.className = "health-ok";
        el.title = "";
        el.removeAttribute("aria-label");
      }
    })
    .catch(function () {
      if (!pollHealth._failCount) pollHealth._failCount = 0;
      pollHealth._failCount++;
      if (pollHealth._failCount >= 2) {
        const el = document.getElementById("health-indicator");
        if (!el) return;
        el.textContent = "health unknown";
        el.className = "health-degraded";
        el.title = "Health endpoint unreachable";
      }
    });
}
setInterval(pollHealth, 30000);

// ===========================================================================
//  6. Auth hooks
// ===========================================================================

window.onLoginSuccess = function () {
  initWorkstreams();
};

window.onLogout = function () {
  for (let id in panes) {
    panes[id].disconnectSSE();
    delete panes[id];
  }
  splitRoot = null;
  focusedPaneId = null;
  workstreams = {};
  currentWsId = null;
  document.getElementById("split-root").replaceChildren();
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
};

// ===========================================================================
//  7. Theme toggle
// ===========================================================================

window.onThemeChange = function (next) {
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const isLight = next === "light";
    btn.textContent = isLight ? "\u2600" : "\u263E";
    btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark theme" : "Switch to light theme",
    );
  }
  reRenderAllMermaid();
  // Persist theme to server settings so it propagates to other clients
  const themeValue = next === "light" ? "light" : "dark";
  authFetch("/v1/api/admin/settings/interface.theme", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: themeValue }),
  }).catch(function () {});
};
(function () {
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const isLight = document.documentElement.dataset.theme === "light";
    btn.textContent = isLight ? "\u2600" : "\u263E";
    btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark theme" : "Switch to light theme",
    );
  }
})();

// ===========================================================================
//  8. Tab bar
// ===========================================================================

const tabBar = document.getElementById("tab-bar");
const tabList = document.getElementById("tab-list");
const newTabBtn = document.getElementById("new-tab-btn");

function renderTabBar() {
  closeTabDropdown();
  tabList.querySelectorAll(".ws-tab").forEach(function (t) {
    t.remove();
  });

  const wsIds = Object.keys(workstreams);
  wsIds.forEach(function (wsId) {
    const ws = workstreams[wsId];
    const tab = document.createElement("div");
    tab.className = "ws-tab" + (wsId === currentWsId ? " active" : "");
    tab.dataset.wsId = wsId;
    tab.setAttribute("role", "tab");
    tab.setAttribute("tabindex", "0");
    tab.setAttribute("aria-selected", wsId === currentWsId ? "true" : "false");
    tab.onclick = function (e) {
      if (e.target.classList.contains("tab-chevron")) return;
      switchTab(wsId);
    };
    tab.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        switchTab(wsId);
      }
    };

    const indicator = document.createElement("span");
    indicator.className = "tab-indicator";
    indicator.dataset.state = ws.state || "idle";
    indicator.setAttribute("aria-label", ws.state || "idle");
    tab.appendChild(indicator);

    const name = document.createElement("span");
    name.className = "tab-name";
    name.textContent = ws.name || wsId.substring(0, 6);
    tab.appendChild(name);

    const wsidBadge = document.createElement("span");
    wsidBadge.className = "tab-wsid";
    wsidBadge.textContent = wsId.substring(0, 7);
    tab.appendChild(wsidBadge);

    const chevron = document.createElement("button");
    chevron.className = "tab-chevron";
    chevron.textContent = "\u25BE";
    chevron.title = "Workstream actions";
    chevron.setAttribute(
      "aria-label",
      "Actions for " + (ws.name || wsId.substring(0, 6)),
    );
    chevron.setAttribute("aria-haspopup", "menu");
    chevron.setAttribute("aria-expanded", "false");
    chevron.onclick = function (e) {
      e.stopPropagation();
      if (_tabDropdown && _tabDropdownTrigger === chevron) {
        closeTabDropdown();
      } else {
        showTabDropdown(chevron, wsId);
      }
    };
    tab.appendChild(chevron);

    tabList.appendChild(tab);
  });
}

function updateTabIndicator(wsId, state, extra) {
  workstreams[wsId] = workstreams[wsId] || {};
  workstreams[wsId].state = state;
  const tab = tabBar.querySelector('.ws-tab[data-ws-id="' + wsId + '"]');
  if (tab) {
    const ind = tab.querySelector(".tab-indicator");
    if (ind) ind.dataset.state = state;
  }
  const row = document.querySelector(
    '#dash-ws-table .dash-row[data-ws-id="' + wsId + '"]',
  );
  if (row) {
    const sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
    row.dataset.state = state;
    const dot = row.querySelector(".dash-state-dot");
    if (dot) dot.dataset.state = state;
    const label = row.querySelector(".dash-state-label");
    if (label) {
      label.dataset.state = state;
      label.textContent = sd.symbol + " " + sd.label;
    }
    if (extra) {
      if (extra.tokens !== undefined) {
        const tokEl = row.querySelector(".dash-cell-tokens");
        if (tokEl) tokEl.textContent = formatTokens(extra.tokens);
      }
      if (extra.context_ratio !== undefined) {
        const ctxEl = row.querySelector(".dash-cell-ctx");
        if (ctxEl) {
          ctxEl.className = "dash-cell-ctx " + ctxClass(extra.context_ratio);
          ctxEl.textContent =
            extra.context_ratio > 0
              ? Math.round(extra.context_ratio * 100) + "%"
              : "";
        }
      }
      if (extra.activity !== undefined) {
        const sub = row.querySelector(".dash-row-sub");
        if (sub) {
          sub.textContent = extra.activity || "";
          if (extra.activity_state === "approval")
            sub.classList.add("sub-attention");
          else sub.classList.remove("sub-attention");
        }
      }
    }
  }
}

function switchTab(wsId) {
  closeTabDropdown();
  let pane = getFocusedPane();
  if (!pane) {
    // Bootstrap the first pane on a fresh-loaded page that had no
    // workstreams to render at init time. Without this, creating
    // or opening a workstream from the dashboard left switchTab
    // with nowhere to attach: it early-returned, no SSE connected,
    // the chat UI showed nothing, and only a refresh fixed it
    // (initWorkstreams creates the pane on a now-populated
    // workstreams list). Mirrors the bootstrap block in
    // initWorkstreams; renderLayout fires once so the pane DOM is
    // attached before the rest of switchTab connects SSE.
    pane = createPane(wsId);
    splitRoot = { type: "leaf", pane: pane };
    setFocusedPane(pane.id);
    renderLayout();
  }
  if (wsId === pane.wsId && !dashboardVisible) return;

  // Track last active for close_tab_action
  if (pane.wsId && workstreams[pane.wsId]) {
    _lastActiveWsId = pane.wsId;
  }

  // In multi-pane mode, focus an existing pane showing this ws
  if (splitRoot && countLeaves(splitRoot) > 1) {
    for (let pid in panes) {
      if (panes[pid].wsId === wsId && pid !== focusedPaneId) {
        setFocusedPane(pid);
        return;
      }
    }
  }

  pane.disconnectSSE();
  pane.reset();
  pane.wsId = wsId;
  currentWsId = wsId;
  while (pane.messagesEl.firstChild)
    pane.messagesEl.removeChild(pane.messagesEl.firstChild);
  pane.showEmptyState();
  pane.updateWsName();
  renderTabBar();
  pane._loadHistoryThenConnect(wsId);

  if (!_historyNavigation) {
    history.pushState({ turnstone: "workstream", wsId: wsId }, "");
  }
}

// ===========================================================================
//  9. New workstream modal
// ===========================================================================

let _newWsTrapHandler = null;
let _forkFromWsId = "";

// Staged files for the new-workstream modal.  Distinct from the pane's
// chip strip: there's no ws_id yet, so we hold File objects in memory
// and ship them all in one multipart create request on submit.
let _newWsStagedFiles = [];

// Per-kind size caps (mirrored from turnstone/core/attachments.py so the
// browser can fail fast before uploading).  Keep in sync.
const _NEW_WS_IMAGE_CAP = 4 * 1024 * 1024;
const _NEW_WS_TEXT_CAP = 512 * 1024;
const _NEW_WS_MAX_FILES = 10;

function _newWsRenderChips() {
  const chipsEl = document.getElementById("new-ws-attach-chips");
  if (!chipsEl) return;
  chipsEl.textContent = "";
  for (let i = 0; i < _newWsStagedFiles.length; i++) {
    (function (idx) {
      const f = _newWsStagedFiles[idx];
      const chip = document.createElement("span");
      chip.className = "new-ws-attach-chip";
      chip.setAttribute("role", "listitem");
      const label = document.createElement("span");
      label.className = "new-ws-attach-chip-name";
      label.textContent = f.name;
      label.title = f.name + " (" + f.size + " bytes)";
      chip.appendChild(label);
      const size = document.createElement("span");
      size.className = "new-ws-attach-chip-size";
      size.textContent = _formatAttachSize(f.size);
      chip.appendChild(size);
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "new-ws-attach-chip-remove";
      rm.setAttribute("aria-label", "Remove " + f.name);
      rm.textContent = "\u00d7";
      rm.onclick = function () {
        _newWsStagedFiles.splice(idx, 1);
        _newWsRenderChips();
      };
      chip.appendChild(rm);
      chipsEl.appendChild(chip);
    })(i);
  }
}

// Mirrors turnstone/server.py classifier — magic-byte image allowlist plus
// text/* MIMEs, allowlisted application/* MIMEs, and known text extensions.
// Surfaces unsupported types client-side so the user sees a clear error
// instead of a generic create failure after the server rejects.
const _ATTACH_IMAGE_MIMES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
];
const _ATTACH_TEXT_APP_MIMES = [
  "application/json",
  "application/xml",
  "application/x-yaml",
  "application/yaml",
  "application/toml",
];
const _ATTACH_TEXT_EXTENSIONS = [
  ".c",
  ".conf",
  ".cpp",
  ".css",
  ".go",
  ".h",
  ".hpp",
  ".html",
  ".ini",
  ".java",
  ".js",
  ".json",
  ".jsx",
  ".md",
  ".py",
  ".rs",
  ".sh",
  ".sql",
  ".toml",
  ".ts",
  ".tsx",
  ".txt",
  ".xml",
  ".yaml",
  ".yml",
];

function _isAttachmentAllowed(file) {
  const mime = (file.type || "").toLowerCase();
  if (_ATTACH_IMAGE_MIMES.indexOf(mime) !== -1) return true;
  if (mime.indexOf("text/") === 0) return true;
  if (_ATTACH_TEXT_APP_MIMES.indexOf(mime) !== -1) return true;
  const name = (file.name || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  if (dot >= 0 && _ATTACH_TEXT_EXTENSIONS.indexOf(name.substr(dot)) !== -1) {
    return true;
  }
  return false;
}

function _newWsAddFiles(files) {
  const errEl = document.getElementById("new-ws-error");
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (_newWsStagedFiles.length >= _NEW_WS_MAX_FILES) {
      errEl.textContent =
        "At most " + _NEW_WS_MAX_FILES + " attachments per workstream";
      errEl.style.display = "block";
      return;
    }
    if (!_isAttachmentAllowed(f)) {
      errEl.textContent =
        "Unsupported file type: " +
        f.name +
        " (allowed: png/jpeg/gif/webp images, text)";
      errEl.style.display = "block";
      return;
    }
    const isImage = (f.type || "").indexOf("image/") === 0;
    const cap = isImage ? _NEW_WS_IMAGE_CAP : _NEW_WS_TEXT_CAP;
    if (f.size > cap) {
      errEl.textContent =
        f.name + " exceeds the " + _formatAttachSize(cap) + " cap";
      errEl.style.display = "block";
      return;
    }
    _newWsStagedFiles.push(f);
  }
  errEl.style.display = "none";
  _newWsRenderChips();
}

function newWorkstream() {
  showNewWsModal();
}

function showNewWsModal(forkFromWsId) {
  _forkFromWsId = forkFromWsId || "";
  const overlay = document.getElementById("new-ws-overlay");
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";

  // Update title and button text based on mode
  const titleEl = document.getElementById("new-ws-title");
  const submitBtn = document.getElementById("new-ws-submit");
  if (_forkFromWsId) {
    titleEl.textContent = "Fork Workstream";
    submitBtn.textContent = "Fork";
  } else {
    titleEl.textContent = "New Workstream";
    submitBtn.textContent = "Create";
  }

  // Hide skill dropdown when forking (not relevant — fork copies history)
  const skillLabel = document.querySelector('label[for="new-ws-skill"]');
  const skillSelect = document.getElementById("new-ws-skill");
  if (_forkFromWsId) {
    if (skillLabel) skillLabel.style.display = "none";
    if (skillSelect) skillSelect.style.display = "none";
  } else {
    if (skillLabel) skillLabel.style.display = "";
    if (skillSelect) skillSelect.style.display = "";
  }

  overlay.onclick = function (e) {
    if (e.target === overlay) hideNewWsModal();
  };

  // Populate model dropdown
  const modelSelect = document.getElementById("new-ws-model");
  const judgeSelect = document.getElementById("new-ws-judge-model");
  const fp = getFocusedPane();
  const curModel = fp ? fp.modelAlias || fp.model || "" : "";
  modelSelect.textContent = "";
  judgeSelect.textContent = "";
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = curModel
    ? "Default (" + curModel + ")"
    : "Default model";
  modelSelect.appendChild(defaultOpt);
  const defJudgeOpt = document.createElement("option");
  defJudgeOpt.value = "";
  defJudgeOpt.textContent = "Default (agent model)";
  judgeSelect.appendChild(defJudgeOpt);
  authFetch("/v1/api/models")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.models || []).forEach(function (m) {
        const opt = document.createElement("option");
        opt.value = m.alias;
        opt.textContent =
          m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
        modelSelect.appendChild(opt);

        const judgeOpt = document.createElement("option");
        judgeOpt.value = m.alias;
        judgeOpt.textContent = opt.textContent;
        judgeSelect.appendChild(judgeOpt);
      });
    })
    .catch(function () {
      /* ignore — default model still works */
    });

  const tplSelect = document.getElementById("new-ws-skill");
  const tplDefaultOpt = document.createElement("option");
  tplDefaultOpt.value = "";
  tplDefaultOpt.textContent = "Use defaults";
  tplSelect.replaceChildren(tplDefaultOpt);
  authFetch("/v1/api/skills")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.skills || []).forEach(function (t) {
        const opt = document.createElement("option");
        opt.value = t.name;
        let label = t.name;
        if (t.is_default) label += " (default)";
        if (t.origin === "mcp") label += " [MCP]";
        opt.textContent = label;
        tplSelect.appendChild(opt);
      });
    })
    .catch(function () {
      /* ignore */
    });

  document.getElementById("new-ws-name").value = "";
  const initEl = document.getElementById("new-ws-initial-message");
  if (initEl) initEl.value = "";
  const errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";
  errEl.textContent = "";
  submitBtn.disabled = false;

  // Reset attachment staging.  Forks don't carry attachments —
  // disable the attach UI in that case (the fork inherits its
  // parent's history; new attachments go on the next manual send).
  _newWsStagedFiles = [];
  const attachRow = document.getElementById("new-ws-attach-row");
  const attachInput = document.getElementById("new-ws-attach-input");
  const attachBtn = document.getElementById("new-ws-attach-btn");
  if (attachRow) attachRow.style.display = _forkFromWsId ? "none" : "";
  if (attachInput) attachInput.value = "";
  _newWsRenderChips();
  if (attachBtn && attachInput) {
    attachBtn.onclick = function () {
      attachInput.click();
    };
    attachInput.onchange = function () {
      if (attachInput.files && attachInput.files.length) {
        _newWsAddFiles(attachInput.files);
      }
      attachInput.value = "";
    };
  }

  document.getElementById("new-ws-cancel").onclick = hideNewWsModal;
  submitBtn.onclick = submitNewWs;

  _newWsTrapHandler = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      hideNewWsModal();
      return;
    }
    if (
      e.key === "Enter" &&
      e.target.tagName !== "TEXTAREA" &&
      e.target.tagName !== "SELECT"
    ) {
      e.preventDefault();
      submitNewWs();
      return;
    }
    if (e.key !== "Tab") return;
    const box = document.getElementById("new-ws-box");
    const focusable = box.querySelectorAll(
      'input, select, button, [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable.length) return;
    const first = focusable[0],
      last = focusable[focusable.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _newWsTrapHandler);
  setTimeout(function () {
    document.getElementById("new-ws-name").focus();
  }, 50);
}

function hideNewWsModal() {
  _forkFromWsId = "";
  document.getElementById("new-ws-overlay").style.display = "none";
  document.body.style.overflow = "";
  if (_newWsTrapHandler) {
    document.removeEventListener("keydown", _newWsTrapHandler);
    _newWsTrapHandler = null;
  }
  document.getElementById("new-tab-btn").focus();
}

function submitNewWs() {
  const submitBtn = document.getElementById("new-ws-submit");
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  submitBtn.textContent = _forkFromWsId ? "Forking\u2026" : "Creating\u2026";

  const body = {};
  const name = document.getElementById("new-ws-name").value.trim();
  const model = document.getElementById("new-ws-model").value.trim();
  const judge_model = document
    .getElementById("new-ws-judge-model")
    .value.trim();
  const skill = document.getElementById("new-ws-skill").value;
  const initEl = document.getElementById("new-ws-initial-message");
  const initial_message = initEl ? initEl.value.trim() : "";
  if (name) body.name = name;
  if (model) body.model = model;
  if (judge_model) body.judge_model = judge_model;
  if (skill && !_forkFromWsId) body.skill = skill;
  if (_forkFromWsId) body.resume_ws = _forkFromWsId;
  if (initial_message) body.initial_message = initial_message;

  const errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";

  let fetchOpts;
  const staged = _forkFromWsId ? [] : _newWsStagedFiles.slice();
  if (staged.length > 0) {
    const form = new FormData();
    form.append("meta", JSON.stringify(body));
    for (let i = 0; i < staged.length; i++) {
      form.append("file", staged[i], staged[i].name);
    }
    // Don't set Content-Type — the browser adds the correct boundary.
    fetchOpts = { method: "POST", body: form };
  } else {
    fetchOpts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  authFetch("/v1/api/workstreams/new", fetchOpts)
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.error) {
        errEl.textContent = data.error;
        errEl.style.display = "block";
        submitBtn.disabled = false;
        submitBtn.textContent = _forkFromWsId ? "Fork" : "Create";
        return;
      }
      if (data.ws_id) {
        workstreams[data.ws_id] = { name: data.name, state: "idle" };
        _newWsStagedFiles = [];
        hideNewWsModal();
        switchTab(data.ws_id);
      }
    })
    .catch(function () {
      errEl.textContent = _forkFromWsId
        ? "Failed to fork workstream"
        : "Failed to create workstream";
      errEl.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = _forkFromWsId ? "Fork" : "Create";
    });
}

function _reassignPanesForClosedWs(closedWsId, tabIdsBeforeClose) {
  const remaining = Object.keys(workstreams);
  // Collect panes showing the closed ws
  const affected = [];
  for (let pid in panes) {
    if (panes[pid].wsId === closedWsId) affected.push(pid);
  }
  if (!affected.length) return;

  // Determine target ws based on close_tab_action setting
  let action = "last_used";
  try {
    action =
      localStorage.getItem("turnstone_interface.close_tab_action") ||
      "last_used";
  } catch (_) {}

  if (action === "dashboard" && remaining.length > 0) {
    // Show dashboard, but still need to reassign panes to valid ws
    for (let di = 0; di < affected.length; di++) {
      const dp = panes[affected[di]];
      dp.disconnectSSE();
      if (remaining.length) {
        dp.wsId = remaining[0];
        dp.messagesEl.replaceChildren();
        dp.showEmptyState();
        dp.updateWsName();
        dp._loadHistoryThenConnect(remaining[0]);
      }
    }
    if (focusedPaneId && panes[focusedPaneId]) {
      currentWsId = panes[focusedPaneId].wsId;
    }
    renderTabBar();
    showDashboard();
    loadDashboard();
    return;
  }

  // Determine preferred target ws_id
  let preferredWsId = null;
  if (action === "last_used") {
    if (
      _lastActiveWsId &&
      _lastActiveWsId !== closedWsId &&
      workstreams[_lastActiveWsId]
    ) {
      preferredWsId = _lastActiveWsId;
    }
  } else if (action === "nearest_left" || action === "nearest_right") {
    const idx = tabIdsBeforeClose ? tabIdsBeforeClose.indexOf(closedWsId) : -1;
    if (idx >= 0) {
      if (action === "nearest_left") {
        // Walk left, then right
        for (let li = idx - 1; li >= 0; li--) {
          if (workstreams[tabIdsBeforeClose[li]]) {
            preferredWsId = tabIdsBeforeClose[li];
            break;
          }
        }
        if (!preferredWsId) {
          for (let ri = idx + 1; ri < tabIdsBeforeClose.length; ri++) {
            if (workstreams[tabIdsBeforeClose[ri]]) {
              preferredWsId = tabIdsBeforeClose[ri];
              break;
            }
          }
        }
      } else {
        // Walk right, then left
        for (let ri2 = idx + 1; ri2 < tabIdsBeforeClose.length; ri2++) {
          if (workstreams[tabIdsBeforeClose[ri2]]) {
            preferredWsId = tabIdsBeforeClose[ri2];
            break;
          }
        }
        if (!preferredWsId) {
          for (let li2 = idx - 1; li2 >= 0; li2--) {
            if (workstreams[tabIdsBeforeClose[li2]]) {
              preferredWsId = tabIdsBeforeClose[li2];
              break;
            }
          }
        }
      }
    }
  }

  // Build set of ws_ids already shown by non-affected panes
  const usedWsIds = {};
  for (let pid2 in panes) {
    if (affected.indexOf(pid2) === -1) usedWsIds[panes[pid2].wsId] = true;
  }

  for (let i = 0; i < affected.length; i++) {
    const p = panes[affected[i]];
    // Try the preferred ws first, then fall back to first unused
    let newWsId = null;
    if (preferredWsId && !usedWsIds[preferredWsId]) {
      newWsId = preferredWsId;
    } else {
      for (let j = 0; j < remaining.length; j++) {
        if (!usedWsIds[remaining[j]]) {
          newWsId = remaining[j];
          break;
        }
      }
    }
    if (newWsId) {
      // Reassign pane to the target workstream
      p.disconnectSSE();
      p.wsId = newWsId;
      p.messagesEl.replaceChildren();
      p.showEmptyState();
      p.updateWsName();
      p._loadHistoryThenConnect(newWsId);
      usedWsIds[newWsId] = true;
    } else if (countLeaves(splitRoot) > 1) {
      // No unused workstream available — close redundant pane
      closePane(affected[i]);
    } else {
      // Last pane — reassign to first remaining ws (will duplicate, but no choice)
      p.disconnectSSE();
      if (remaining.length) {
        p.wsId = remaining[0];
        p.messagesEl.replaceChildren();
        p.showEmptyState();
        p.updateWsName();
        p._loadHistoryThenConnect(remaining[0]);
      }
    }
  }
  if (focusedPaneId && panes[focusedPaneId]) {
    currentWsId = panes[focusedPaneId].wsId;
  }
  renderTabBar();
  if (currentWsId && workstreams[currentWsId]) {
    switchTab(currentWsId);
  }
}

function closeWorkstream(wsId) {
  // Capture tab order from DOM (visual order) before deletion for close_tab_action=nearest_left/right
  const tabIdsBeforeClose = Array.from(
    document.querySelectorAll("#tab-list .ws-tab"),
  ).map(function (tab) {
    return tab.dataset.wsId;
  });

  authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.status === "ok") {
        delete workstreams[wsId];
        renderTabBar();
        _reassignPanesForClosedWs(wsId, tabIdsBeforeClose);
        const remaining = Object.keys(workstreams);
        if (remaining.length === 0) {
          loadDashboard();
          showDashboard();
        }
      } else if (data.error) {
        showToast(data.error, "warning");
      }
    });
}

// ===========================================================================
//  10. Dashboard
// ===========================================================================

function showDashboard() {
  dashboardVisible = true;
  document.getElementById("dashboard").classList.add("active");
  // ui-header stays interactive while the dashboard is open so the
  // theme toggle, settings menu, and the console proxy's node-picker
  // pill remain reachable.  See .dashboard-overlay { top: 48px } in
  // style.css for the matching layout offset.
  document.getElementById("tab-bar").inert = true;
  document.getElementById("split-root").inert = true;
  loadDashboard();
  _loadDashboardOptionsLists();
  _restoreDashboardOptionsState();
  _refreshDashboardOptionsSummary();
  _refreshDashboardSubmitLabel();
  setTimeout(function () {
    document.getElementById("dashboard-input").focus();
  }, 50);
}

function hideDashboard() {
  dashboardVisible = false;
  document.getElementById("dashboard").classList.remove("active");
  document.getElementById("tab-bar").inert = false;
  document.getElementById("split-root").inert = false;
  document.getElementById("dashboard-input").value = "";
  _dashboardStagedFiles = [];
  _renderDashboardChips();
  _refreshDashboardSubmitLabel();
  const pane = getFocusedPane();
  if (pane) pane.inputEl.focus();
}

function toggleDashboard() {
  if (dashboardVisible) hideDashboard();
  else showDashboard();
}

// Paint a transient message (loading / error) into the saved-workstreams
// area.  Clears any cards AND hides the pagination control \u2014 it's a sibling
// of the cards container, so a bare replaceChildren on the cards alone would
// leave stale Prev/Next visible and still wired to the previous list cache.
// A successful load re-shows both via _wsTable.setItems.
function _setSavedWsMessage(text) {
  document
    .getElementById("dashboard-saved-cards")
    .replaceChildren(makeEmptyState(text));
}

function loadDashboard() {
  const tableEl = document.getElementById("dash-ws-table");
  tableEl.replaceChildren(makeEmptyState("Loading\u2026"));
  _setSavedWsMessage("Loading\u2026");
  const dashP = authFetch("/v1/api/dashboard").then(function (r) {
    return r.json();
  });
  const sessP = authFetch("/v1/api/workstreams/saved").then(function (r) {
    return r.json();
  });
  Promise.all([dashP, sessP])
    .then(function (res) {
      const dashData = res[0];
      const wsList = dashData.workstreams || [];
      const agg = dashData.aggregate || {};
      renderDashboardTable(wsList, agg);
      const activeWsIds = {};
      wsList.forEach(function (ws) {
        activeWsIds[ws.ws_id] = true;
      });
      const savedList = (res[1].workstreams || []).filter(function (s) {
        return !activeWsIds[s.ws_id];
      });
      _wsTable.setItems(savedList);
    })
    .catch(function () {
      tableEl.replaceChildren(makeEmptyState("Failed to load"));
      _setSavedWsMessage("Failed to load");
    });
}

function renderDashboardTable(wsList, agg) {
  const activeCount = wsList.filter(function (w) {
    return w.state !== "idle";
  }).length;
  document.getElementById("dash-summary").textContent =
    activeCount + " active \u00b7 " + wsList.length + " total";
  const table = document.getElementById("dash-ws-table");
  table.replaceChildren();
  if (!wsList.length) {
    table.replaceChildren(makeEmptyState("No active workstreams"));
    updateDashFooter(agg);
    return;
  }
  wsList.forEach(function (ws) {
    const liveState =
      (workstreams[ws.ws_id] && workstreams[ws.ws_id].state) ||
      ws.state ||
      "idle";
    const liveName =
      (workstreams[ws.ws_id] && workstreams[ws.ws_id].name) ||
      ws.name ||
      ws.ws_id;
    const sd = STATE_DISPLAY[liveState] || STATE_DISPLAY.idle;

    const row = document.createElement("div");
    row.className = "dash-row";
    row.dataset.wsId = ws.ws_id;
    row.dataset.state = liveState;
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    let ariaLabel = liveName + " \u2014 " + sd.label;
    if (ws.model_alias || ws.model)
      ariaLabel += ", model: " + (ws.model_alias || ws.model);
    if (ws.title) ariaLabel += ", task: " + ws.title;
    if (ws.tokens) ariaLabel += ", " + formatTokens(ws.tokens) + " tokens";
    if (ws.context_ratio > 0)
      ariaLabel += ", " + Math.round(ws.context_ratio * 100) + "% context";
    row.setAttribute("aria-label", ariaLabel);

    const main = document.createElement("div");
    main.className = "dash-row-main";

    const stateCell = document.createElement("span");
    stateCell.className = "dash-cell-state";
    const stateDot = document.createElement("span");
    stateDot.className = "dash-state-dot";
    stateDot.setAttribute("data-state", liveState);
    stateDot.setAttribute("aria-hidden", "true");
    const stateLabel = document.createElement("span");
    stateLabel.className = "dash-state-label";
    stateLabel.setAttribute("data-state", liveState);
    stateLabel.textContent = sd.symbol + " " + sd.label;
    stateCell.append(stateDot, stateLabel);
    main.appendChild(stateCell);

    const nameCell = document.createElement("span");
    nameCell.className = "dash-cell-name";
    nameCell.textContent = liveName;
    main.appendChild(nameCell);

    const modelCell = document.createElement("span");
    modelCell.className = "dash-cell-model";
    modelCell.textContent = ws.model_alias || ws.model || "";
    if (ws.model) modelCell.title = ws.model;
    main.appendChild(modelCell);

    const nodeCell = document.createElement("span");
    nodeCell.className = "dash-cell-node";
    nodeCell.textContent = ws.node || "local";
    if (ws.node) nodeCell.title = ws.node;
    main.appendChild(nodeCell);

    const taskCell = document.createElement("span");
    taskCell.className = "dash-cell-task";
    taskCell.textContent = ws.title || "";
    main.appendChild(taskCell);

    const tokensCell = document.createElement("span");
    tokensCell.className = "dash-cell-tokens";
    tokensCell.textContent = ws.tokens ? formatTokens(ws.tokens) : "";
    main.appendChild(tokensCell);

    const ctxCell = document.createElement("span");
    ctxCell.className = "dash-cell-ctx " + ctxClass(ws.context_ratio);
    ctxCell.textContent =
      ws.context_ratio > 0 ? Math.round(ws.context_ratio * 100) + "%" : "";
    main.appendChild(ctxCell);

    row.appendChild(main);

    const sub = document.createElement("div");
    sub.className = "dash-row-sub";
    if (ws.activity_state === "approval") sub.classList.add("sub-attention");
    sub.textContent = ws.activity || "";
    row.appendChild(sub);

    row.onclick = function () {
      dashboardSwitchWorkstream(ws.ws_id);
    };
    row.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dashboardSwitchWorkstream(ws.ws_id);
      }
    };

    table.appendChild(row);
  });
  updateDashFooter(agg);
  table.onkeydown = function (e) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const rows = Array.from(table.querySelectorAll(".dash-row"));
    const idx = rows.indexOf(document.activeElement);
    if (idx === -1) return;
    if (e.key === "ArrowDown" && idx < rows.length - 1) rows[idx + 1].focus();
    if (e.key === "ArrowUp" && idx > 0) rows[idx - 1].focus();
  };
}

function updateDashFooter(agg) {
  if (!agg) return;
  const nodesEl = document.getElementById("dash-footer-nodes");
  const statsEl = document.getElementById("dash-footer-stats");
  const footerDot = document.createElement("span");
  footerDot.className = "dash-footer-node-dot";
  nodesEl.replaceChildren(
    footerDot,
    " " + (agg.node || "local") + " (" + (agg.total_count || 0) + " ws)",
  );
  const parts = [];
  if (agg.total_tokens) parts.push(formatTokens(agg.total_tokens) + " tokens");
  if (agg.total_tool_calls) parts.push(agg.total_tool_calls + " tool calls");
  if (agg.uptime_seconds)
    parts.push(formatUptime(agg.uptime_seconds) + " uptime");
  statsEl.textContent = parts.join(" \u00b7 ");
  if (_lastHealth && _lastHealth.status === "degraded") {
    statsEl.textContent += " \u00b7 backend degraded";
  }
}

// Saved Workstreams table.  The shared createSavedTable (/shared/cards.js)
// owns filter + sort + render and wraps the multi-select delete controller;
// the per-app inputs are the column spec, the DOM refs, and the path-keyed
// delete request.  Coordinators (console/static) use the same helper with a
// CHILDREN column instead of MSGS.
const WS_COLUMNS = [
  SavedColumns.name(),
  SavedColumns.model(),
  SavedColumns.count("message_count", "MSGS"),
  SavedColumns.ctx(),
  SavedColumns.last(),
  SavedColumns.id(),
];
const _wsTable = createSavedTable({
  headerEl: document.getElementById("ws-saved-colheaders"),
  bodyEl: document.getElementById("dashboard-saved-cards"),
  filterEl: document.getElementById("ws-filter"),
  footerEl: document.getElementById("ws-saved-footer"),
  columns: WS_COLUMNS,
  noun: "workstream",
  emptyText: "No saved workstreams",
  activateLabel: function (s) {
    return "Resume: " + (s.alias || s.title || s.ws_id);
  },
  onActivate: function (s) {
    dashboardResumeSession(s.ws_id);
  },
  delete: {
    idPrefix: "ws-delete",
    buttonId: "ws-delete-btn",
    buildDeleteRequest: function (wsId) {
      return {
        url: "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete",
        options: { method: "POST" },
      };
    },
    onClose: function () {
      loadDashboard();
    },
  },
});

// HTML inline-onclick wrappers — keep the global names the existing markup
// binds to (`onclick="startWsDeleteMode()"` etc.) and forward to the shared
// table's delete controller.
function startWsDeleteMode() {
  _wsTable.controller.start();
}
function cancelWsDeleteMode() {
  _wsTable.controller.cancel();
}
function toggleSelectAll() {
  _wsTable.controller.toggleAll();
}
function confirmWsDeleteSelection() {
  _wsTable.controller.confirmSelection();
}
function cancelWsDelete() {
  _wsTable.controller.closeModal();
}
function confirmWsDelete() {
  _wsTable.controller.confirm();
}

// --- Workstream title management ---

let _lastActiveWsId = null;

function refreshWorkstreamTitle(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;

  const url =
    "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/refresh-title";

  authFetch(url, { method: "POST" })
    .then(function (r) {
      if (!r.ok)
        throw new Error("Failed to refresh title (HTTP " + r.status + ")");
      return r.json();
    })
    .then(function (data) {
      showToast("Title regeneration started…", "info");
    })
    .catch(function (err) {
      showToast(err.message || "Failed to refresh title", "error");
    });
}

let _editTitleTrap = null;

function editWorkstreamTitle(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  let currentTitle = "";
  const tabEl = document.querySelector(
    '.ws-tab[data-ws-id="' + wsId + '"] .tab-name',
  );
  if (tabEl) currentTitle = tabEl.textContent.trim();

  const overlay = document.getElementById("edit-title-overlay");
  const input = document.getElementById("edit-title-input");
  input.value = currentTitle;
  overlay.style.display = "flex";
  overlay.onclick = function (e) {
    if (e.target === overlay) cancelEditTitle();
  };

  // Focus trap + Escape
  if (_editTitleTrap) document.removeEventListener("keydown", _editTitleTrap);
  _editTitleTrap = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelEditTitle();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("edit-title-box");
      const focusable = box.querySelectorAll("input, button");
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _editTitleTrap);

  setTimeout(function () {
    input.focus();
    input.select();
  }, 50);
}

function cancelEditTitle() {
  document.getElementById("edit-title-overlay").style.display = "none";
  if (_editTitleTrap) {
    document.removeEventListener("keydown", _editTitleTrap);
    _editTitleTrap = null;
  }
  const chevron = document.querySelector(".ws-tab.active .tab-chevron");
  if (chevron) chevron.focus();
}

function submitEditTitle() {
  const wsId = getCurrentWsId();
  if (!wsId) return;
  const input = document.getElementById("edit-title-input");
  const newTitle = input.value.trim();
  if (!newTitle) {
    showToast("Title cannot be empty", "warning");
    return;
  }

  const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/title";

  authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: newTitle }),
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to set title (HTTP " + r.status + ")");
      return r.json();
    })
    .then(function (data) {
      cancelEditTitle();
      // Optimistic update — SSE ws_rename will confirm
      const nameEls = document.querySelectorAll(
        '[data-ws-id="' + wsId + '"] .tab-name',
      );
      nameEls.forEach(function (el) {
        el.textContent = newTitle;
      });
      if (workstreams[wsId]) workstreams[wsId].name = newTitle;
      showToast("Title updated", "success");
    })
    .catch(function (err) {
      showToast(err.message || "Failed to set title", "error");
    });
}

// --- Workstream deletion ---

let _pendingDeleteWsId = null;
let _deleteWsTrap = null;

function confirmDeleteWorkstream(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  if (Object.keys(workstreams).length <= 1) return;
  const tabEl = document.querySelector(
    '.ws-tab[data-ws-id="' + wsId + '"] .tab-name',
  );
  const name = tabEl ? tabEl.textContent.trim() : wsId.substring(0, 12);

  _pendingDeleteWsId = wsId;
  const overlay = document.getElementById("delete-ws-overlay");
  const msg = document.getElementById("delete-ws-message");
  msg.textContent = 'Delete "' + name + '"? This cannot be undone.';
  overlay.style.display = "flex";

  // Focus trap + Escape
  if (_deleteWsTrap) document.removeEventListener("keydown", _deleteWsTrap);
  _deleteWsTrap = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelDeleteWs();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("delete-ws-box");
      const focusable = box.querySelectorAll("button");
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _deleteWsTrap);

  const cancelBtn = overlay.querySelector("button");
  if (cancelBtn) cancelBtn.focus();
}

function cancelDeleteWs() {
  _pendingDeleteWsId = null;
  document.getElementById("delete-ws-overlay").style.display = "none";
  if (_deleteWsTrap) {
    document.removeEventListener("keydown", _deleteWsTrap);
    _deleteWsTrap = null;
  }
  const chevron = document.querySelector(".ws-tab.active .tab-chevron");
  if (chevron) {
    chevron.focus();
  } else {
    const fallback = document.getElementById("new-tab-btn");
    if (fallback) fallback.focus();
  }
}

function executeDeleteWs() {
  const wsId = _pendingDeleteWsId;
  if (!wsId) return;
  cancelDeleteWs();

  const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete";

  authFetch(url, { method: "POST" })
    .then(function (r) {
      if (!r.ok)
        throw new Error("Failed to delete workstream (HTTP " + r.status + ")");
      return r.json();
    })
    .then(function () {
      // Update local state directly — don't call closeWorkstream which
      // would send a redundant POST to /close for an already-deleted ws.
      delete workstreams[wsId];
      renderTabBar();
      _reassignPanesForClosedWs(wsId, []);
      if (!Object.keys(workstreams).length) {
        loadDashboard();
        showDashboard();
      }
      showToast("Workstream deleted", "success");
    })
    .catch(function (err) {
      showToast(err.message || "Failed to delete workstream", "error");
    });
}

function getCurrentWsId() {
  const activeTab = document.querySelector(".ws-tab.active");
  if (activeTab) return activeTab.dataset.wsId || "";
  return "";
}

function forkWorkstream(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  showNewWsModal(wsId);
}

// formatRelativeTime moved to /shared/utils.js so both surfaces share it.

function dashboardSwitchWorkstream(wsId) {
  if (workstreams[wsId]) {
    hideDashboard();
    switchTab(wsId);
  } else loadDashboard();
}

function dashboardResumeSession(wsId) {
  authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (!data.ws_id) return;
      workstreams[data.ws_id] = { name: data.name, state: "idle" };
      switchTab(data.ws_id);
      hideDashboard();
    })
    .catch(function (err) {
      showToast("Failed to open workstream", "error");
    });
}

// Staged files for the dashboard composer. Reuses the same file-list pattern
// as the new-workstream modal but lives independently so the two flows don't
// stomp on each other's state.
let _dashboardStagedFiles = [];

// Per-kind size caps mirrored from turnstone/core/attachments.py — keep in sync.
const _DASH_IMAGE_CAP = 4 * 1024 * 1024;
const _DASH_TEXT_CAP = 512 * 1024;
const _DASH_MAX_FILES = 10;

function _renderDashboardChips() {
  const chipsEl = document.getElementById("dashboard-attach-chips");
  if (!chipsEl) return;
  chipsEl.textContent = "";
  for (let i = 0; i < _dashboardStagedFiles.length; i++) {
    (function (idx) {
      const f = _dashboardStagedFiles[idx];
      const chip = document.createElement("span");
      chip.className = "new-ws-attach-chip";
      chip.setAttribute("role", "listitem");
      const label = document.createElement("span");
      label.className = "new-ws-attach-chip-name";
      label.textContent = f.name;
      label.title = f.name + " (" + f.size + " bytes)";
      chip.appendChild(label);
      const size = document.createElement("span");
      size.className = "new-ws-attach-chip-size";
      size.textContent = _formatAttachSize(f.size);
      chip.appendChild(size);
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "new-ws-attach-chip-remove";
      rm.setAttribute("aria-label", "Remove " + f.name);
      rm.textContent = "\u00d7";
      rm.onclick = function () {
        _dashboardStagedFiles.splice(idx, 1);
        _renderDashboardChips();
        _refreshDashboardSubmitLabel();
      };
      chip.appendChild(rm);
      chipsEl.appendChild(chip);
    })(i);
  }
}

function _addDashboardFiles(files) {
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (_dashboardStagedFiles.length >= _DASH_MAX_FILES) {
      _dashboardError(
        "At most " + _DASH_MAX_FILES + " attachments per workstream",
      );
      return;
    }
    // Drag-drop bypasses the <input accept="..."> filter, so re-check
    // against the server's allowlist before the upload roundtrip.
    if (!_isAttachmentAllowed(f)) {
      _dashboardError(
        "Unsupported file type: " +
          f.name +
          " (allowed: png/jpeg/gif/webp images, text)",
      );
      return;
    }
    const isImage = (f.type || "").indexOf("image/") === 0;
    const cap = isImage ? _DASH_IMAGE_CAP : _DASH_TEXT_CAP;
    if (f.size > cap) {
      _dashboardError(
        f.name + " exceeds the " + _formatAttachSize(cap) + " cap",
      );
      return;
    }
    _dashboardStagedFiles.push(f);
  }
  _renderDashboardChips();
  _refreshDashboardSubmitLabel();
}

let _dashboardErrorTimer = null;

function _dashboardError(msg) {
  // Live-region message + outline.  title= alone is invisible to screen
  // readers and on touch devices, so we surface the message visibly
  // beneath the textarea via aria-live="polite".
  const input = document.getElementById("dashboard-input");
  const errEl = document.getElementById("dashboard-error");
  if (errEl) {
    errEl.textContent = msg;
  }
  if (input) {
    input.classList.add("dashboard-input-error");
  }
  if (_dashboardErrorTimer) clearTimeout(_dashboardErrorTimer);
  _dashboardErrorTimer = setTimeout(function () {
    if (input) input.classList.remove("dashboard-input-error");
    if (errEl) errEl.textContent = "";
    _dashboardErrorTimer = null;
  }, 5000);
}

function _refreshDashboardSubmitLabel() {
  const btn = document.getElementById("dashboard-submit-btn");
  if (!btn) return;
  const input = document.getElementById("dashboard-input");
  const hasText = input && input.value.trim().length > 0;
  const hasFiles = _dashboardStagedFiles.length > 0;
  btn.textContent = hasText || hasFiles ? "Send" : "Create";
}

// Format a resolved alias with its model suffix the same way as the
// dropdown rows ("alias (model)", or just "alias" when they coincide).
// Returns "" when alias is empty or unknown so callers fall back to a
// neutral placeholder.
function _resolveModelLabel(alias, models) {
  if (!alias) return "";
  for (let i = 0; i < (models || []).length; i++) {
    const m = models[i];
    if (m.alias === alias) {
      return m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
    }
  }
  return "";
}

function _loadDashboardOptionsLists() {
  // Models
  const modelSel = document.getElementById("dashboard-model");
  const judgeSel = document.getElementById("dashboard-judge-model");
  if (modelSel && modelSel.options.length <= 1) {
    authFetch("/v1/api/models")
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        (data.models || []).forEach(function (m) {
          const opt = document.createElement("option");
          opt.value = m.alias;
          opt.textContent =
            m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
          modelSel.appendChild(opt);
          if (judgeSel) {
            const jOpt = document.createElement("option");
            jOpt.value = m.alias;
            jOpt.textContent = opt.textContent;
            judgeSel.appendChild(jOpt);
          }
        });
        // Surface the resolved defaults in the placeholder rows so the
        // panel shows which model actually runs when left untouched —
        // mirrors the coordinator launcher.  The judge tracks the
        // per-workstream agent model unless judge.model is explicitly
        // configured, so keep the "(agent model)" wording in that case
        // rather than advertising a fixed alias the judge won't use.
        const modelDefault = _resolveModelLabel(
          data.default_alias || "",
          data.models || [],
        );
        modelSel.options[0].textContent = modelDefault
          ? "Default — " + modelDefault
          : "Default model";
        if (judgeSel) {
          const judgeDefault = _resolveModelLabel(
            data.judge_default_alias || "",
            data.models || [],
          );
          judgeSel.options[0].textContent = judgeDefault
            ? "Default — " + judgeDefault
            : "Default (agent model)";
        }
      })
      .catch(function () {
        /* default model still works */
      });
  }
  // Skills
  const skillSel = document.getElementById("dashboard-skill");
  if (skillSel && skillSel.options.length <= 1) {
    authFetch("/v1/api/skills")
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        (data.skills || []).forEach(function (t) {
          const opt = document.createElement("option");
          opt.value = t.name;
          let label = t.name;
          if (t.is_default) label += " (default)";
          if (t.origin === "mcp") label += " [MCP]";
          opt.textContent = label;
          skillSel.appendChild(opt);
        });
      })
      .catch(function () {
        /* ignore */
      });
  }
}

// localStorage key for the dashboard composer's Options-panel disclosure
// state — power users who set non-default model/skill repeatedly want the
// panel to stay open across reloads instead of clicking it every time.
const _DASH_OPTIONS_LS_KEY = "turnstone.dashboard.options_open";
// In-memory fallback for environments where localStorage throws (private
// mode, storage quota, embedded WebViews).  null means "no preference
// recorded this session yet — use the closed default".
let _dashOptionsOpenSession = null;

function _setDashboardOptionsOpen(open) {
  const panel = document.getElementById("dashboard-options");
  const btn = document.getElementById("dashboard-options-btn");
  if (!panel || !btn) return;
  if (open) {
    panel.removeAttribute("hidden");
    btn.setAttribute("aria-expanded", "true");
  } else {
    panel.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", "false");
  }
}

function _toggleDashboardOptions() {
  const panel = document.getElementById("dashboard-options");
  if (!panel) return;
  const nextOpen = panel.hasAttribute("hidden");
  _setDashboardOptionsOpen(nextOpen);
  _dashOptionsOpenSession = nextOpen;
  try {
    localStorage.setItem(_DASH_OPTIONS_LS_KEY, nextOpen ? "1" : "0");
  } catch (_) {
    /* localStorage unavailable — _dashOptionsOpenSession above keeps the
       state for this session so a hide/show cycle preserves the choice. */
  }
}

function _restoreDashboardOptionsState() {
  // Read order: localStorage (cross-session) → in-memory session value
  // → closed default.  Only override based on a genuinely-successful
  // localStorage read; on throw, fall back to the session value so the
  // panel stays where the user last put it within the same tab.
  let saved = null;
  let lsAvailable = true;
  try {
    saved = localStorage.getItem(_DASH_OPTIONS_LS_KEY);
  } catch (_) {
    lsAvailable = false;
  }
  let open;
  if (lsAvailable && saved !== null) {
    open = saved === "1";
  } else if (_dashOptionsOpenSession !== null) {
    open = _dashOptionsOpenSession;
  } else {
    open = false;
  }
  _setDashboardOptionsOpen(open);
}

// Update the inline summary chip beside the Options button when any of
// model / judge_model / skill is non-default.  Helps users see at a
// glance that they've overridden defaults — without having to expand
// the panel.  Hidden when everything is default.
function _refreshDashboardOptionsSummary() {
  const summary = document.getElementById("dashboard-options-summary");
  if (!summary) return;
  const bits = [];
  const modelSel = document.getElementById("dashboard-model");
  const judgeSel = document.getElementById("dashboard-judge-model");
  const skillSel = document.getElementById("dashboard-skill");
  if (modelSel && modelSel.value) bits.push(modelSel.value);
  if (judgeSel && judgeSel.value) bits.push("judge: " + judgeSel.value);
  if (skillSel && skillSel.value) bits.push(skillSel.value);
  if (bits.length === 0) {
    summary.textContent = "";
    summary.setAttribute("hidden", "");
    return;
  }
  summary.textContent = bits.join(" · ");
  summary.removeAttribute("hidden");
}

// Unified dashboard submit. Replaces the old "click button → modal" +
// "press Enter → quick-send-empty-config" split. One path: build the
// create payload from text + attachments + options, send it, switch.
function dashboardSubmit() {
  const input = document.getElementById("dashboard-input");
  const btn = document.getElementById("dashboard-submit-btn");
  const text = input.value.trim();
  const staged = _dashboardStagedFiles.slice();

  const body = {};
  const model = document.getElementById("dashboard-model").value.trim();
  const judge = document.getElementById("dashboard-judge-model").value.trim();
  const skill = document.getElementById("dashboard-skill").value;
  if (model) body.model = model;
  if (judge) body.judge_model = judge;
  if (skill) body.skill = skill;
  if (text) body.initial_message = text;

  input.disabled = true;
  btn.disabled = true;

  let fetchOpts;
  if (staged.length > 0) {
    const form = new FormData();
    form.append("meta", JSON.stringify(body));
    for (let i = 0; i < staged.length; i++) {
      form.append("file", staged[i], staged[i].name);
    }
    fetchOpts = { method: "POST", body: form };
  } else {
    fetchOpts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  authFetch("/v1/api/workstreams/new", fetchOpts)
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      input.disabled = false;
      btn.disabled = false;
      if (data.error || !data.ws_id) {
        _dashboardError(data.error || "Failed to create workstream");
        return;
      }
      workstreams[data.ws_id] = { name: data.name, state: "idle" };
      switchTab(data.ws_id);
      hideDashboard();
      // If we sent an initial_message, the server's worker thread already
      // dispatched it. Echo into the pane so the user sees their own text
      // immediately rather than waiting for SSE to backfill.
      if (text) {
        const pane = getFocusedPane();
        if (pane) {
          pane.setBusy(true);
          pane.addUserMessage(text);
        }
      }
    })
    .catch(function (err) {
      input.disabled = false;
      btn.disabled = false;
      // authFetch throws Error("auth") when the user is signed out and the
      // login modal has already been surfaced; suppress the redundant
      // error toast in that case.  Otherwise fall back to a generic
      // string so we never render "Connection error: undefined".
      if (err && err.message === "auth") return;
      const detail = (err && err.message) || "Unable to reach the server";
      _dashboardError("Connection error: " + detail);
    });
}

// ===========================================================================
//  11. Global SSE
// ===========================================================================

function connectGlobalSSE() {
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
  // Manual-reconnect path threads ``?last_event_id=N`` because the
  // EventSource constructor can't set headers; native auto-reconnect
  // on the same source uses the header directly.
  let globalUrl = "/v1/api/events/global";
  if (globalLastEventId) {
    globalUrl += "?last_event_id=" + encodeURIComponent(globalLastEventId);
  }
  globalEvtSource = new EventSource(globalUrl);
  globalEvtSource.onopen = function () {
    globalRetryDelay = 1000;
  };
  globalEvtSource.onmessage = function (e) {
    // Capture lastEventId BEFORE JSON.parse (see Pane.connectSSE
    // onmessage for full rationale).
    if (globalEvtSource && globalEvtSource.lastEventId) {
      globalLastEventId = globalEvtSource.lastEventId;
    }
    const data = JSON.parse(e.data);
    if (data.type === "ws_state") {
      updateTabIndicator(data.ws_id, data.state, {
        tokens: data.tokens,
        context_ratio: data.context_ratio,
        activity: data.activity,
        activity_state: data.activity_state,
      });
    } else if (data.type === "ws_activity") {
      const row = document.querySelector(
        '#dash-ws-table .dash-row[data-ws-id="' + data.ws_id + '"]',
      );
      if (row) {
        const sub = row.querySelector(".dash-row-sub");
        if (sub) {
          sub.textContent = data.activity || "";
          if (data.activity_state === "approval")
            sub.classList.add("sub-attention");
          else sub.classList.remove("sub-attention");
        }
      }
    } else if (data.type === "ws_rename") {
      if (workstreams[data.ws_id]) workstreams[data.ws_id].name = data.name;
      // Update ALL matching tab elements (not just first one)
      const nameEls = document.querySelectorAll(
        '[data-ws-id="' + data.ws_id + '"] .tab-name',
      );
      nameEls.forEach(function (el) {
        el.textContent = data.name;
      });
      // Update all panes showing this workstream
      for (let id in panes) {
        if (panes[id].wsId === data.ws_id) panes[id].updateWsName();
      }
    } else if (data.type === "ws_created") {
      workstreams[data.ws_id] = workstreams[data.ws_id] || {};
      workstreams[data.ws_id].name = data.name || data.ws_id.slice(0, 6);
      workstreams[data.ws_id].state = "idle";
      renderTabBar();
    } else if (data.type === "ws_closed") {
      const wsId = data.ws_id;
      // Capture tab order from DOM (visual order) before deletion for close_tab_action=nearest_left/right
      const sseTabIds = Array.from(
        document.querySelectorAll("#tab-list .ws-tab"),
      ).map(function (tab) {
        return tab.dataset.wsId;
      });
      // Disconnect per-ws SSE on affected panes immediately so stale
      // events from the dying workstream don't leak into reassigned panes.
      for (let cid in panes) {
        if (panes[cid].wsId === wsId) panes[cid].disconnectSSE();
      }
      delete workstreams[wsId];
      renderTabBar();
      if (data.reason === "evicted") {
        showToast(
          "Evicted" + (data.name ? ": " + data.name : "") + " (capacity)",
        );
      }
      _reassignPanesForClosedWs(wsId, sseTabIds);
      if (!Object.keys(workstreams).length) showDashboard();
    } else if (data.type === "settings_changed") {
      // Re-load interface settings and apply immediately
      loadInterfaceSettings();
    }
  };
  globalEvtSource.onerror = function () {
    // Do NOT close globalEvtSource for transient errors — native
    // EventSource auto-reconnect handles them with the
    // ``Last-Event-ID`` header automatically (now that the global
    // SSE handler emits ``id:`` on every buffered event).  Closing
    // here would defeat native reconnect.  See PR-D briefing § 3.3
    // and the per-pane handler above for the same pattern.
    //
    // The 401 probe stays — an authentication failure is a terminal
    // condition (the user must log in) and merits an explicit
    // close + showLogin.  ``_reconnectDeadSSEs`` (visibilitychange /
    // focus listener) covers the truly-CLOSED case.
    fetch("/v1/api/workstreams").then(function (r) {
      if (r.status === 401) {
        if (globalEvtSource) {
          globalEvtSource.close();
          globalEvtSource = null;
        }
        showLogin();
      }
    });
  };
}

// ===========================================================================
//  12. Utility functions
// ===========================================================================

function stripAnsi(s) {
  return s.replace(
    /\x1b(?:\[[0-9;?]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[()#][A-Za-z0-9]|.)/g,
    "",
  );
}

function buildToolDiv(item) {
  const div = document.createElement("div");
  div.className = "ts-approval-tool";
  div.dataset.funcName = item.func_name || "";
  div.dataset.callId = item.call_id || "";

  const name = document.createElement("div");
  name.className = "tool-name" + (item.error ? " tool-name--error" : "");
  name.textContent = item.func_name || "";
  // Inline auto-approve indicator — surfaces tools that bypassed the
  // operator approval gate (skill allowlist / blanket / admin policy /
  // explicit "Approve + Always") right next to the tool name.  The
  // coord-tree pill is bounded to the coord page; this small badge
  // gives the operator the same signal on the per-ws page they
  // navigated into.
  if (item.auto_approved) {
    const badge = document.createElement("span");
    badge.className = "tool-auto-approved";
    const reason = item.auto_approve_reason || "auto_approve_tools";
    badge.textContent = " auto: " + reason;
    badge.title = "Tool auto-approved (no operator prompt) — reason: " + reason;
    name.appendChild(badge);
  }
  div.appendChild(name);

  const cmd = document.createElement("div");
  cmd.className = "tool-cmd";
  const headerText = stripAnsi(item.header || "");
  const cleaned = headerText.replace(/^[^\s]+\s+\w+:\s*/, "");
  if (item.func_name === "bash" && cleaned) {
    const dollarSpan = document.createElement("span");
    dollarSpan.className = "dollar";
    dollarSpan.textContent = "$ ";
    cmd.append(dollarSpan, cleaned);
  } else {
    cmd.textContent = cleaned || headerText;
  }
  div.appendChild(cmd);

  if (item.preview) {
    const diff = document.createElement("div");
    diff.className = "tool-diff";
    const lines = stripAnsi(item.preview).split("\n");
    const diffNodes = [];
    lines.forEach(function (line, i) {
      if (i > 0) diffNodes.push("\n");
      const trimmed = line.trim();
      let cls = null;
      if (trimmed.startsWith("-")) cls = "diff-del";
      else if (trimmed.startsWith("+")) cls = "diff-add";
      else if (trimmed.startsWith("Warning:")) cls = "diff-warn";
      if (cls !== null) {
        const span = document.createElement("span");
        span.className = cls;
        span.textContent = line;
        diffNodes.push(span);
      } else {
        diffNodes.push(line);
      }
    });
    diff.append(...diffNodes);
    div.appendChild(diff);
  }

  return div;
}

function renderVerdictBadge(verdict, judgePending) {
  if (!verdict) return document.createDocumentFragment();
  const risk = verdict.risk_level || "medium";
  const rec = verdict.recommendation || "review";
  const conf = Math.round((verdict.confidence || 0) * 100);

  const badge = document.createElement("div");
  badge.className = "verdict-badge verdict-" + risk + " ts-verdict-badge";
  badge.setAttribute("data-risk", risk);
  if (verdict.call_id) badge.setAttribute("data-call-id", verdict.call_id);

  const riskSpan = document.createElement("span");
  riskSpan.className = "verdict-risk";
  riskSpan.textContent = risk.toUpperCase();

  const recSpan = document.createElement("span");
  recSpan.className = "verdict-rec";
  recSpan.textContent = rec;

  const confSpan = document.createElement("span");
  confSpan.className = "verdict-conf";
  confSpan.textContent = conf + "%";

  badge.append(riskSpan, recSpan, confSpan);

  if (judgePending) {
    const spinner = document.createElement("span");
    spinner.className = "verdict-judge-spinner";
    const dot = document.createElement("span");
    dot.className = "judge-spinner-dot";
    spinner.append(dot, " judge analyzing\u2026");
    badge.appendChild(spinner);
  }

  const expand = document.createElement("button");
  expand.className = "verdict-expand";
  expand.textContent = "details";
  expand.addEventListener("click", function () {
    toggleVerdictDetail(this);
  });
  badge.appendChild(expand);

  const detail = document.createElement("div");
  detail.className = "verdict-detail";
  detail.style.display = "none";

  const summaryEl = document.createElement("div");
  summaryEl.className = "verdict-summary";
  summaryEl.textContent = verdict.intent_summary || "";

  const reasoningEl = document.createElement("div");
  reasoningEl.className = "verdict-reasoning";
  reasoningEl.textContent = verdict.reasoning || "";

  detail.append(summaryEl, reasoningEl);

  const evidence = verdict.evidence || [];
  if (evidence.length) {
    const evEl = document.createElement("div");
    evEl.className = "verdict-evidence";
    for (const ev of evidence) {
      const row = document.createElement("div");
      row.textContent = "\u2022 " + ev;
      evEl.appendChild(row);
    }
    detail.appendChild(evEl);
  }

  const tierEl = document.createElement("div");
  tierEl.className = "verdict-tier";
  let tierText = (verdict.tier || "heuristic") + " tier";
  if (verdict.judge_model) tierText += " | " + verdict.judge_model;
  tierEl.textContent = tierText;
  detail.appendChild(tierEl);

  const frag = document.createDocumentFragment();
  frag.append(badge, detail);
  return frag;
}

function toggleVerdictDetail(btn) {
  const badge = btn.closest(".verdict-badge");
  const detail = badge ? badge.nextElementSibling : null;
  if (detail && detail.classList.contains("verdict-detail")) {
    const isHidden = detail.style.display === "none";
    detail.style.display = isHidden ? "block" : "none";
    btn.textContent = isHidden ? "hide" : "details";
  }
}

// Append an "✗ error" pill to an approval block as a sibling of the
// existing approved/denied/auto-approved pill, so the approval verdict
// stays visible alongside the execution outcome. Idempotent — re-fires
// (live + history rerender) do not stack badges.
function appendToolErrorBadge(blockEl) {
  if (!blockEl) return;
  if (blockEl.querySelector(".ts-approval-badge--error")) return;
  const errBadge = document.createElement("div");
  errBadge.setAttribute("role", "status");
  errBadge.className = "ts-approval-badge ts-approval-badge--error";
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

// ===========================================================================
//  12a. Media embed renderer (MCP tool output with stream_url / results)
// ===========================================================================

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

/**
 * Try to pretty-print JSON text with indentation and API key redaction.
 * Returns a formatted string if valid JSON, otherwise null.
 */
function _tryPrettyJson(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  return _redactApiKeys(JSON.stringify(obj, null, 2));
}

/**
 * Render tool output text into a DOM element.
 * If the text is valid JSON, pretty-prints it with indentation.
 * Otherwise renders as plain text. Always redacts API keys.
 */
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

// ===========================================================================
//  12b. MCP error embed (consent / scope / forbidden / operator)
// ===========================================================================

// Module-level set of servers with an unresolved consent prompt; drives the
// gear-icon badge so the user has a stable signal that re-consent is pending
// after the inline card scrolls out of view.
const _pendingConsentServers = new Set();

function _onConsentDetected(server) {
  if (typeof server === "string" && server) {
    _pendingConsentServers.add(server);
    _refreshConsentBadge();
  }
}

function _clearConsentBadge() {
  _pendingConsentServers.clear();
  _refreshConsentBadge();
}

// Hydrate the pending-consent badge from the Phase 9 persistence endpoint
// on dashboard load.  Closes the gap that pre-Phase-9 left open: a
// scheduled / channel-driven run that hit ``mcp_consent_required`` while
// the user wasn't online produced an in-flight SSE event that nobody saw.
// The endpoint short-circuits to ``{pending: 0}`` on installs with no
// ``auth_type=oauth_user`` MCP servers, so the call is cheap on local-
// auth deployments.  Failures are silent — the badge will be re-driven
// by the next in-flight tool error if any.
function loadPendingConsents() {
  authFetch("/v1/api/mcp/oauth/pending")
    .then(function (r) {
      if (!r.ok) return null;
      return r.json();
    })
    .then(function (data) {
      if (!data || !Array.isArray(data.servers)) return;
      for (let i = 0; i < data.servers.length; i++) {
        const row = data.servers[i];
        if (row && typeof row.server_name === "string") {
          _pendingConsentServers.add(row.server_name);
        }
      }
      _refreshConsentBadge();
    })
    .catch(function () {
      // Endpoint failures must not block dashboard init.
    });
}

function _refreshConsentBadge() {
  const btn = document.getElementById("settings-btn");
  if (!btn) return;
  let existing = btn.querySelector(".settings-consent-badge");
  const n = _pendingConsentServers.size;
  // Keep the visible badge and the accessible name in lockstep so screen-
  // reader users get the same pending-consent signal that sighted users
  // get from the red dot. The badge itself stays aria-hidden because the
  // count is already reflected in the button's aria-label/title.
  if (n === 0) {
    if (existing) existing.remove();
    btn.setAttribute("aria-label", "Settings");
    btn.setAttribute("title", "Settings");
    return;
  }
  if (!existing) {
    existing = document.createElement("span");
    existing.className = "settings-consent-badge";
    existing.setAttribute("aria-hidden", "true");
    btn.appendChild(existing);
  }
  existing.textContent = String(n);
  const label =
    "Settings (" + n + " MCP consent" + (n === 1 ? "" : "s") + " pending)";
  btn.setAttribute("aria-label", label);
  btn.setAttribute("title", label);
}

/**
 * Detect a structured MCP error envelope.  Returns the inner ``error``
 * object on shape match, null otherwise.  Recognised codes:
 *   - mcp_consent_required (carries optional consent_url + scopes_required)
 *   - mcp_insufficient_scope (carries consent_url + scopes_required)
 *   - mcp_tool_call_forbidden / mcp_resource_read_forbidden / mcp_prompt_get_forbidden
 *   - mcp_token_undecryptable_key_unknown (operator action)
 *   - mcp_oauth_url_insecure (operator action)
 */
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

/**
 * Render the action card for an MCP error envelope.  Mirrors the
 * media-embed pattern: visible card on top, collapsible raw JSON below.
 */
function buildMcpErrorEmbed(err, rawJson) {
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
    _onConsentDetected(err.server);
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
//  HLS lazy-loader (follows the mermaid.js lazy-load pattern in
//  /shared/renderer.js)
// ---------------------------------------------------------------------------
let _hlsState = "idle";
let _hlsQueue = [];

function _loadHls(callback) {
  if (_hlsState === "ready") {
    callback();
    return;
  }
  _hlsQueue.push(callback);
  if (_hlsState === "loading") return;
  _hlsState = "loading";
  const script = document.createElement("script");
  script.src = "/shared/hls-1.6.16/hls.min.js";
  script.onload = function () {
    _hlsState = "ready";
    const q = _hlsQueue;
    _hlsQueue = [];
    for (let i = 0; i < q.length; i++) q[i]();
  };
  script.onerror = function () {
    _hlsState = "idle";
    const q = _hlsQueue;
    _hlsQueue = [];
    // Fall through — _activatePlayer will use stream_url since Hls is undefined
    for (let i = 0; i < q.length; i++) q[i]();
  };
  document.head.appendChild(script);
}

function _isHlsUrl(url) {
  return typeof url === "string" && /\.m3u8(\?|$)/i.test(url);
}

// ---------------------------------------------------------------------------
//  Click-to-play delegated handler (follows img-placeholder pattern)
// ---------------------------------------------------------------------------
function _activatePlayer(btn) {
  const url = btn.dataset.streamUrl;
  const hlsUrl = btn.dataset.hlsUrl;
  const isAudio = btn.dataset.audioOnly === "true";
  const directStream = btn.dataset.directStream === "true";

  const player = document.createElement(isAudio ? "audio" : "video");
  player.controls = true;
  player.autoplay = true;
  player.className = "media-player";

  // Prefer direct stream when the source supports it; fall back to HLS
  // only when transcoding is needed.
  if (directStream && url) {
    player.src = url;
  } else if (
    hlsUrl &&
    !isAudio &&
    typeof Hls !== "undefined" &&
    Hls.isSupported()
  ) {
    const hls = new Hls();
    hls.loadSource(hlsUrl);
    hls.attachMedia(player);
  } else if (
    hlsUrl &&
    !isAudio &&
    player.canPlayType("application/vnd.apple.mpegurl")
  ) {
    player.src = hlsUrl;
  } else {
    player.src = url;
  }

  player.addEventListener("error", function () {
    const card = player.closest(".media-embed");
    const titleEl = card ? card.querySelector(".media-card-title") : null;
    const label = titleEl ? ": " + titleEl.textContent : "";

    const err = document.createElement("div");
    err.className = "media-player-error";
    err.setAttribute("role", "alert");
    err.textContent = "Failed to load stream" + label;

    const retry = document.createElement("button");
    retry.className = "media-play-btn";
    retry.type = "button";
    retry.dataset.streamUrl = url;
    retry.dataset.hlsUrl = hlsUrl || "";
    retry.dataset.audioOnly = String(isAudio);
    retry.dataset.directStream = String(directStream);
    retry.setAttribute("aria-label", "Retry" + label);
    retry.appendChild(document.createTextNode("\u25b6 Retry"));

    const container = document.createElement("div");
    container.appendChild(err);
    container.appendChild(retry);
    player.replaceWith(container);
  });

  btn.replaceWith(player);
}

document.addEventListener("click", function (e) {
  const btn = e.target.closest(".media-play-btn");
  if (!btn) return;
  e.preventDefault();
  btn.disabled = true;
  const labelEl = btn.querySelector("span:last-child");
  if (labelEl) {
    labelEl.textContent = "Loading\u2026";
  } else {
    btn.textContent = "\u25b6 Loading\u2026";
  }

  const hlsUrl = btn.dataset.hlsUrl;
  const isAudio = btn.dataset.audioOnly === "true";

  // If HLS URL present and not audio, ensure hls.js is loaded first
  if (hlsUrl && !isAudio && _isHlsUrl(hlsUrl)) {
    _loadHls(function () {
      _activatePlayer(btn);
    });
  } else {
    _activatePlayer(btn);
  }
});

document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter") return;
  const btn = e.target.closest(".media-play-btn");
  if (!btn) return;
  btn.click();
});

// ===========================================================================
//  13. Plan review dialog
// ===========================================================================

let _planContent = "";
let _planPaneId = null;
let _planWsId = null;

function showPlanDialog(content) {
  _planContent = content;
  _planPaneId = focusedPaneId;
  const paneNow = panes[_planPaneId];
  _planWsId = paneNow ? paneNow.wsId : currentWsId;
  document.getElementById("plan-content").textContent = content;
  const feedbackEl = document.getElementById("plan-feedback");
  feedbackEl.value = "";
  _updatePlanRejectBtn();

  // Disable focused pane input
  const pane = panes[_planPaneId];
  if (pane) {
    pane.inputEl.disabled = true;
    pane.sendBtn.disabled = true;
  }

  document.getElementById("plan-overlay").classList.add("active");
  setTimeout(function () {
    feedbackEl.focus();
  }, 50);
}

function _updatePlanRejectBtn() {
  const btn = document.getElementById("btn-plan-reject");
  const hasFeedback =
    document.getElementById("plan-feedback").value.trim().length > 0;
  btn.replaceChildren(makeKeyLabel("Esc", hasFeedback ? "Amend" : "Reject"));
  btn.style.background = hasFeedback ? "var(--accent)" : "";
  btn.style.color = hasFeedback ? "var(--on-color)" : "";
  btn.onclick = function () {
    resolvePlan(hasFeedback ? "" : "reject");
  };
}

function resolvePlan(defaultFeedback) {
  let feedback = document.getElementById("plan-feedback").value.trim();
  if (!feedback && defaultFeedback) feedback = defaultFeedback;
  // Removing 'active' synchronously is what lets dismissPlanDialog's
  // early-return guard treat the server's echoed plan_resolved as a no-op.
  document.getElementById("plan-overlay").classList.remove("active");

  const pane = panes[_planPaneId];
  if (pane) {
    pane.inputEl.disabled = false;
    pane.sendBtn.disabled = false;
    pane.inputEl.focus();
  }

  // Critical: fire the API call first — this unblocks the server.
  // Use the ws_id captured when the dialog opened, not the current pane
  // (user may have switched tabs while the dialog was open).
  const wsId = _planWsId || (pane ? pane.wsId : currentWsId);
  _planWsId = null;
  authFetch("/v1/api/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback: feedback, ws_id: wsId }),
  }).catch(function (err) {
    if (pane) pane.addErrorMessage("Connection error: " + err.message);
  });

  // Render plan inline in the chat (best-effort).  `action` is hoisted so
  // the catch handler can still build a fallback message if the inline
  // render throws after the action label was computed.
  let action;
  try {
    const isReject = feedback === "reject";
    const isAmend = feedback && !isReject;
    action = isReject ? "rejected" : isAmend ? "amending" : "approved";
    _addInlinePlan(_planContent, action, feedback);
  } catch (err) {
    console.error("Failed to render inline plan:", err);
    if (pane) pane.addInfoMessage("Plan " + action);
  }

  if (pane) {
    pane.setBusy(true);
    pane.addThinkingIndicator();
  }
}

function dismissPlanDialog(feedback) {
  // Sync-dismiss: another client (or the server) already resolved the plan.
  // Do NOT call /v1/api/plan — the server has already moved on.  The early
  // return also handles self-receipt: the client that called resolvePlan()
  // already removed the active class, so this is a no-op for that client.
  const overlay = document.getElementById("plan-overlay");
  if (!overlay.classList.contains("active")) return;
  overlay.classList.remove("active");

  const pane = panes[_planPaneId];
  if (pane) {
    pane.inputEl.disabled = false;
    pane.sendBtn.disabled = false;
    // Restore keyboard context — but skip on touch so we don't surprise the
    // mobile user with a soft-keyboard pop after a remote approval.
    if (!matchMedia("(pointer: coarse)").matches) pane.inputEl.focus();
  }

  const fb = feedback || "";
  const isReject = fb === "reject";
  const isAmend = fb && !isReject;
  const action = isReject ? "rejected" : isAmend ? "amending" : "approved";

  // Race fallback: if plan_resolved arrives before plan_review (e.g. SSE
  // reconnect ordering), _planContent is empty and _addInlinePlan early-
  // returns silently.  Surface a one-line info message so the user sees
  // what happened.
  if (_planContent) {
    try {
      _addInlinePlan(_planContent, action, fb, "remote");
    } catch (err) {
      console.error("Failed to render inline plan:", err);
      if (pane) pane.addInfoMessage("Plan " + action + " on another device");
    }
  } else if (pane) {
    pane.addInfoMessage("Plan " + action + " on another device");
  }

  // SR announcement (visible toast styling deferred — #toast already has
  // aria-live="polite" in markup, this just gives screen-reader parity).
  _announce("Plan " + action + " on another device");

  if (pane) {
    pane.setBusy(true);
    pane.addThinkingIndicator();
  }

  _planContent = "";
  _planPaneId = null;
  _planWsId = null;
}

function _announce(text) {
  const el = document.getElementById("toast");
  if (!el) return;
  // Re-set textContent in two ticks so screen readers re-announce even
  // when the message is identical to the previous one.
  el.textContent = "";
  setTimeout(function () {
    el.textContent = text;
  }, 50);
}

function _addInlinePlan(content, action, feedback, origin) {
  if (!content) return;
  const pane = panes[_planPaneId];
  if (!pane) return;

  const wrapper = document.createElement("div");
  wrapper.className = "plan-inline";

  const header = document.createElement("div");
  header.className = "plan-inline-header";
  let label =
    action === "rejected"
      ? "Plan rejected"
      : action === "amending"
        ? "Plan \u2014 amending"
        : "Plan approved";
  // Disambiguate remote dismissal — otherwise the desktop user sees "Plan
  // approved" with no attribution and may wonder if the agent self-approved.
  if (origin === "remote") label += " (synced)";
  const labelEl = document.createElement("span");
  labelEl.className = "plan-inline-label plan-" + action;
  labelEl.textContent = label;
  header.appendChild(labelEl);
  wrapper.appendChild(header);

  const body = document.createElement("div");
  body.className = "plan-inline-body";
  try {
    setMarkdown(body, content);
  } catch (e) {
    body.textContent = content;
  }
  if (content.split("\n").length > 12) {
    makeCollapsible(body);
    body.setAttribute(
      "aria-label",
      "Plan content (collapsed). Activate to expand.",
    );
  }
  wrapper.appendChild(body);

  if (feedback && action === "amending") {
    const fb = document.createElement("div");
    fb.className = "plan-inline-feedback";
    fb.textContent = "Feedback: " + feedback;
    wrapper.appendChild(fb);
  }

  pane.messagesEl.appendChild(wrapper);
  pane.scrollToBottom();
}

// ===========================================================================
//  14. Keyboard shortcuts
// ===========================================================================

document
  .getElementById("plan-feedback")
  .addEventListener("input", _updatePlanRejectBtn);

// Dashboard composer wiring — Enter (no shift) submits, input refreshes the
// button label, paperclip + drag-drop + paste-image stage files, options
// toggle expands the dropdown panel.
(function () {
  const input = document.getElementById("dashboard-input");
  const attachBtn = document.getElementById("dashboard-attach-btn");
  const attachInput = document.getElementById("dashboard-attach-input");
  const optionsBtn = document.getElementById("dashboard-options-btn");
  const composer = document.getElementById("dashboard-composer");
  if (!input) return;

  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      dashboardSubmit();
    }
  });
  input.addEventListener("input", _refreshDashboardSubmitLabel);
  input.addEventListener("paste", function (e) {
    if (!e.clipboardData) return;
    const items = e.clipboardData.items || [];
    const pasted = [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind === "file") {
        const f = items[i].getAsFile();
        if (f) pasted.push(f);
      }
    }
    if (pasted.length) {
      e.preventDefault();
      _addDashboardFiles(pasted);
    }
  });

  if (attachBtn && attachInput) {
    attachBtn.addEventListener("click", function () {
      attachInput.click();
    });
    attachInput.addEventListener("change", function () {
      if (attachInput.files && attachInput.files.length) {
        _addDashboardFiles(attachInput.files);
      }
      attachInput.value = "";
    });
  }
  if (optionsBtn) {
    optionsBtn.addEventListener("click", _toggleDashboardOptions);
  }
  // Keep the inline summary chip in sync with whichever non-default
  // model / judge / skill is selected.  Listening on the options panel
  // catches all three selects with one handler.
  const optionsPanel = document.getElementById("dashboard-options");
  if (optionsPanel) {
    optionsPanel.addEventListener("change", _refreshDashboardOptionsSummary);
  }
  if (composer) {
    composer.addEventListener("dragover", function (e) {
      if (
        e.dataTransfer &&
        Array.from(e.dataTransfer.types || []).includes("Files")
      ) {
        e.preventDefault();
        composer.classList.add("dashboard-composer-drop");
      }
    });
    composer.addEventListener("dragleave", function (e) {
      if (e.target === composer)
        composer.classList.remove("dashboard-composer-drop");
    });
    composer.addEventListener("drop", function (e) {
      composer.classList.remove("dashboard-composer-drop");
      if (
        e.dataTransfer &&
        e.dataTransfer.files &&
        e.dataTransfer.files.length
      ) {
        e.preventDefault();
        _addDashboardFiles(e.dataTransfer.files);
      }
    });
  }
})();

// ===========================================================================
//  15. MCP server connections settings panel
// ===========================================================================

let _pendingRevokeServer = null;
let _settingsTrap = null;
let _revokeMcpTrap = null;
let _settingsReturnFocus = null;

function openSettingsPanel() {
  const overlay = document.getElementById("settings-overlay");
  if (!overlay) return;
  _settingsReturnFocus = document.activeElement;
  overlay.style.display = "flex";

  if (_settingsTrap) document.removeEventListener("keydown", _settingsTrap);
  _settingsTrap = function (e) {
    if (e.key === "Escape") {
      // If the nested revoke confirmation is open, let its own trap
      // handle Escape — closing inner-first matches the delete-ws flow.
      const inner = document.getElementById("revoke-mcp-overlay");
      if (inner && inner.style.display !== "none") return;
      e.preventDefault();
      closeSettingsPanel();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("settings-box");
      if (!box) return;
      const focusable = box.querySelectorAll(
        "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _settingsTrap);

  loadMcpConnections();

  const closeBtn = document.getElementById("settings-close-btn");
  if (closeBtn) closeBtn.focus();
}

function closeSettingsPanel() {
  // If the nested revoke confirmation is still up, tear it down first
  // — otherwise hiding the parent panel would leave an orphan modal
  // overlay floating with its own keydown trap still attached. The
  // Escape-key path inside the parent's keydown trap defers to the
  // inner trap; this branch is the close-button path that doesn't go
  // through that trap.
  const inner = document.getElementById("revoke-mcp-overlay");
  if (inner && inner.style.display !== "none") {
    cancelRevokeMcp();
  }
  const overlay = document.getElementById("settings-overlay");
  if (overlay) overlay.style.display = "none";
  if (_settingsTrap) {
    document.removeEventListener("keydown", _settingsTrap);
    _settingsTrap = null;
  }
  if (
    _settingsReturnFocus &&
    typeof _settingsReturnFocus.focus === "function"
  ) {
    try {
      _settingsReturnFocus.focus();
    } catch (_) {}
  }
  _settingsReturnFocus = null;
}

// ---------------------------------------------------------------------------
//  Settings menu (gear icon dropdown — MCP connections + Logout)
// ---------------------------------------------------------------------------
//
// Reuses the .ws-tab-dropdown shell for visual + behavioural consistency
// with the workstream tab dropdown and the console proxy's node-picker.
// Keyboard handling matches the proxy node-picker (the APG-correct
// reference): Tab closes the menu WITHOUT preventDefault so focus
// moves naturally to the next focusable; Escape closes + refocuses
// the trigger.  showTabDropdown collapses Tab and Escape into a
// single preventDefault branch — that's a pre-existing divergence,
// tracked as a follow-up to align showTabDropdown to APG.  ArrowUp
// uses an `idx <= 0` guard (not modulo) so the no-focus case wraps
// to the last item rather than the second-to-last — same shape as
// showTabDropdown and the proxy node-picker.

let _settingsMenu = null;
let _settingsMenuCloseHandler = null;
// Cached at open time so closeSettingsMenu can reset ARIA without
// re-querying by id, and so the menu-item click path can refocus
// the trigger BEFORE close — that way openSettingsPanel captures
// the gear (not <body>) as _settingsReturnFocus.
let _settingsMenuTrigger = null;

function toggleSettingsMenu(triggerEl) {
  if (_settingsMenu) closeSettingsMenu();
  else openSettingsMenu(triggerEl);
}

function openSettingsMenu(triggerEl) {
  if (_settingsMenu) return;
  _settingsMenuTrigger = triggerEl;
  triggerEl.setAttribute("aria-expanded", "true");
  triggerEl.setAttribute("aria-controls", "settings-menu");

  const menu = document.createElement("div");
  menu.id = "settings-menu";
  menu.className = "ws-tab-dropdown";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Settings");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  const pendingCount = _pendingConsentServers.size;
  const items = [
    {
      label:
        "MCP connections" + (pendingCount ? " (" + pendingCount + ")" : ""),
      action: function () {
        openSettingsPanel();
      },
    },
    { separator: true },
    {
      label: "Logout",
      // Destructive styling matches Delete in the workstream tab dropdown.
      // Logout doesn't lose data, but it interrupts the session and the red
      // hover/focus tint reduces misclick risk on a dense menu.
      cls: "destructive",
      action: function () {
        logout();
      },
    },
  ];

  items.forEach(function (item) {
    if (item.separator) {
      const sep = document.createElement("div");
      sep.className = "ws-tab-dropdown-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ws-tab-dropdown-item" + (item.cls ? " " + item.cls : "");
    btn.setAttribute("role", "menuitem");
    btn.setAttribute("tabindex", "-1");
    const labelSpan = document.createElement("span");
    labelSpan.className = "ws-tab-dropdown-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    btn.onclick = function () {
      // Refocus the trigger BEFORE close — closeSettingsMenu removes
      // the menu DOM (including this button), and item.action() may
      // call openSettingsPanel which captures document.activeElement
      // as the eventual return-focus target.  Without this refocus,
      // activeElement falls back to <body> and focus restoration
      // sends the user nowhere when the panel later closes.
      if (_settingsMenuTrigger) _settingsMenuTrigger.focus();
      closeSettingsMenu();
      item.action();
    };
    menu.appendChild(btn);
  });

  document.body.appendChild(menu);

  // Right-align under the gear so the menu hangs off the right edge of
  // the appbar without overflowing the viewport.  Right-edge override
  // runs BEFORE the left-edge floor so a menu wider than the viewport
  // still gets clamped to mx=4 instead of going negative — matches the
  // proxy node-picker order in turnstone/console/server.py:307-309.
  const tr = triggerEl.getBoundingClientRect();
  const mr = menu.getBoundingClientRect();
  let mx = tr.right - mr.width;
  let my = tr.bottom + 4;
  if (my + mr.height > window.innerHeight) my = tr.top - mr.height - 4;
  if (mx + mr.width > window.innerWidth) mx = window.innerWidth - mr.width - 4;
  if (mx < 4) mx = 4;
  menu.style.left = mx + "px";
  menu.style.top = my + "px";
  _settingsMenu = menu;

  _settingsMenuCloseHandler = function (e) {
    if (e.type === "keydown") {
      if (e.key === "Escape") {
        e.preventDefault();
        closeSettingsMenu();
        triggerEl.focus();
      } else if (e.key === "Tab") {
        // Per WAI-ARIA APG menu pattern: Tab closes the menu AND lets
        // focus move naturally to the next focusable element — don't
        // preventDefault, otherwise Tab is a dead key inside the menu.
        closeSettingsMenu();
      } else if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Home" ||
        e.key === "End"
      ) {
        e.preventDefault();
        const btns = Array.from(menu.querySelectorAll(".ws-tab-dropdown-item"));
        if (!btns.length) return;
        const idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        // idx <= 0 covers both "first item" (wrap to last) and "no
        // current focus" (idx === -1, which would otherwise yield
        // len-2 via the modulo).  Matches showTabDropdown and the
        // proxy node-picker (turnstone/console/server.py:275).
        else if (e.key === "ArrowUp")
          btns[idx <= 0 ? btns.length - 1 : idx - 1].focus();
        else if (e.key === "Home") btns[0].focus();
        else if (e.key === "End") btns[btns.length - 1].focus();
      }
    } else if (
      e.type === "mousedown" &&
      !menu.contains(e.target) &&
      e.target !== triggerEl &&
      !triggerEl.contains(e.target)
    ) {
      closeSettingsMenu();
    }
  };

  // Attach the keydown listener synchronously so an Escape press
  // queued behind the opening click isn't silently dropped: the
  // global keydown handler at the bottom of this file returns early
  // when _settingsMenu is set (the dashboard-Escape-wipes-composer
  // guard), so without a synchronous menu-side listener there's a
  // brief window where Escape has no handler at all.  Mousedown +
  // initial focus stay deferred — mousedown to avoid the click that
  // opened the menu firing its own outside-click close, initial
  // focus because the menu DOM needs a tick to settle layout before
  // we call focus() on its first item.
  document.addEventListener("keydown", _settingsMenuCloseHandler);
  const activeMenu = menu;
  const closeHandler = _settingsMenuCloseHandler;
  setTimeout(function () {
    if (_settingsMenu !== activeMenu || !closeHandler) return;
    document.addEventListener("mousedown", closeHandler);
    const first = activeMenu.querySelector(".ws-tab-dropdown-item");
    if (first) first.focus();
  }, 0);
}

function closeSettingsMenu() {
  if (_settingsMenu) {
    _settingsMenu.remove();
    _settingsMenu = null;
  }
  if (_settingsMenuCloseHandler) {
    document.removeEventListener("mousedown", _settingsMenuCloseHandler);
    document.removeEventListener("keydown", _settingsMenuCloseHandler);
    _settingsMenuCloseHandler = null;
  }
  if (_settingsMenuTrigger) {
    _settingsMenuTrigger.setAttribute("aria-expanded", "false");
    _settingsMenuTrigger.removeAttribute("aria-controls");
    _settingsMenuTrigger = null;
  }
}

function loadMcpConnections() {
  const loadingEl = document.getElementById("settings-mcp-loading");
  const emptyEl = document.getElementById("settings-mcp-empty");
  const tableEl = document.getElementById("settings-mcp-table");
  const errorEl = document.getElementById("settings-mcp-error");
  if (!loadingEl || !emptyEl || !tableEl || !errorEl) return;
  loadingEl.style.display = "";
  emptyEl.style.display = "none";
  tableEl.style.display = "none";
  errorEl.style.display = "none";

  authFetch("/v1/api/mcp/oauth/connections")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      loadingEl.style.display = "none";
      const connections =
        data && Array.isArray(data.connections) ? data.connections : [];
      renderMcpConnections(connections);
      // Clear AFTER the table renders so the badge reflects "user has
      // seen current state" rather than "user opened the panel" — a
      // failed fetch keeps the pending-consent signal until the user
      // gets confirmation that consents are in fact reachable.
      _clearConsentBadge();
      // Phase 9: re-hydrate the badge from the persistent pending-
      // consent table.  Phase 8 cleared in-memory state on settings-
      // panel open (signal-acknowledged); Phase 9 records are
      // DB-backed, so we re-pull them now to keep the badge in sync
      // with what's actually pending across page lifetimes.
      loadPendingConsents();
    })
    .catch(function (err) {
      loadingEl.style.display = "none";
      errorEl.style.display = "";
      errorEl.textContent = "Failed to load connections: " + err.message;
    });
}

function _clearChildren(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function renderMcpConnections(list) {
  const emptyEl = document.getElementById("settings-mcp-empty");
  const tableEl = document.getElementById("settings-mcp-table");
  const tbody = document.getElementById("settings-mcp-tbody");
  if (!emptyEl || !tableEl || !tbody) return;
  if (!list.length) {
    tableEl.style.display = "none";
    emptyEl.style.display = "";
    return;
  }
  emptyEl.style.display = "none";
  tableEl.style.display = "";
  _clearChildren(tbody);
  for (let i = 0; i < list.length; i++) {
    const conn = list[i];
    const tr = document.createElement("tr");

    const serverTd = document.createElement("td");
    serverTd.textContent = conn.server_name || "";
    tr.appendChild(serverTd);

    const scopesTd = document.createElement("td");
    scopesTd.textContent = conn.scopes || "(none)";
    tr.appendChild(scopesTd);

    const createdTd = document.createElement("td");
    createdTd.textContent = _formatRelativeTimestamp(conn.created);
    createdTd.title = conn.created || "";
    tr.appendChild(createdTd);

    const refreshedTd = document.createElement("td");
    if (conn.last_refreshed) {
      refreshedTd.textContent = _formatRelativeTimestamp(conn.last_refreshed);
      refreshedTd.title = conn.last_refreshed;
    } else {
      refreshedTd.textContent = "—";
    }
    tr.appendChild(refreshedTd);

    const actionTd = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "settings-revoke-btn";
    btn.textContent = "Revoke";
    const serverNameForRevoke = conn.server_name || "";
    btn.setAttribute(
      "aria-label",
      "Revoke connection to " + serverNameForRevoke,
    );
    (function (name) {
      btn.addEventListener("click", function () {
        promptRevokeMcp(name);
      });
    })(serverNameForRevoke);
    actionTd.appendChild(btn);
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
}

function promptRevokeMcp(server) {
  if (!server) return;
  _pendingRevokeServer = server;
  const msg = document.getElementById("revoke-mcp-message");
  const overlay = document.getElementById("revoke-mcp-overlay");
  if (msg) {
    msg.textContent =
      "Disconnect " +
      server +
      "? Tools that need this server will require re-consent.";
  }
  if (overlay) overlay.style.display = "flex";

  if (_revokeMcpTrap) document.removeEventListener("keydown", _revokeMcpTrap);
  _revokeMcpTrap = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelRevokeMcp();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("revoke-mcp-box");
      if (!box) return;
      const focusable = box.querySelectorAll("button");
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _revokeMcpTrap);

  const cancelBtn = overlay
    ? overlay.querySelector("button:not(.danger)")
    : null;
  if (cancelBtn) cancelBtn.focus();
}

function cancelRevokeMcp() {
  _pendingRevokeServer = null;
  const overlay = document.getElementById("revoke-mcp-overlay");
  if (overlay) overlay.style.display = "none";
  if (_revokeMcpTrap) {
    document.removeEventListener("keydown", _revokeMcpTrap);
    _revokeMcpTrap = null;
  }
}

function confirmRevokeMcp() {
  const server = _pendingRevokeServer;
  if (!server) {
    cancelRevokeMcp();
    return;
  }
  authFetch("/v1/api/mcp/oauth/connections/" + encodeURIComponent(server), {
    method: "DELETE",
  })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      cancelRevokeMcp();
      showToast("Disconnected " + server);
      loadMcpConnections();
    })
    .catch(function (err) {
      cancelRevokeMcp();
      showToast("Failed to revoke: " + err.message);
    });
}

function _formatRelativeTimestamp(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const sec = Math.round(diffMs / 1000);
    if (sec < 60) return "just now";
    if (sec < 3600) return Math.round(sec / 60) + "m ago";
    if (sec < 86400) return Math.round(sec / 3600) + "h ago";
    return Math.round(sec / 86400) + "d ago";
  } catch (e) {
    return iso;
  }
}

document.addEventListener("keydown", function (e) {
  // Defer to modal's own keydown handler when any modal is open
  const modalIds = [
    "new-ws-overlay",
    "edit-title-overlay",
    "delete-ws-overlay",
    "ws-delete-overlay",
    "settings-overlay",
    "revoke-mcp-overlay",
  ];
  for (let mi = 0; mi < modalIds.length; mi++) {
    const modal = document.getElementById(modalIds[mi]);
    if (modal && modal.style.display !== "none") return;
  }
  // Settings menu is a transient dropdown, not a modal overlay, but
  // the global Escape handler must not reach hideDashboard() while
  // it's open — that would wipe the composer out from under the user
  // (hideDashboard clears dashboard-input.value and _dashboardStagedFiles).
  // The menu's own keydown handler (registered async via setTimeout(0)
  // in openSettingsMenu) handles Escape and Tab.
  if (_settingsMenu) return;

  if (e.key === "Escape" && dashboardVisible) {
    e.preventDefault();
    hideDashboard();
    return;
  }

  // Get focused pane for approval / busy checks
  const pane = getFocusedPane();

  // Escape: cancel generation when busy
  if (e.key === "Escape" && pane && pane.busy && !pane.pendingApproval) {
    e.preventDefault();
    pane.cancelGeneration();
    return;
  }

  // Ctrl+D: toggle dashboard
  if (e.ctrlKey && e.key === "d") {
    e.preventDefault();
    toggleDashboard();
    return;
  }
  // Ctrl+T: new tab
  if (e.ctrlKey && e.key === "t") {
    e.preventDefault();
    newWorkstream();
    return;
  }
  // Ctrl+1..9: switch tabs
  if (e.ctrlKey && e.key >= "1" && e.key <= "9") {
    e.preventDefault();
    const idx = parseInt(e.key) - 1;
    const wsIds = Object.keys(workstreams);
    if (idx < wsIds.length) switchTab(wsIds[idx]);
    return;
  }
  // Ctrl+Shift+W: close pane (must come before Ctrl+W)
  if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "w") {
    if (splitRoot && countLeaves(splitRoot) > 1) {
      e.preventDefault();
      closePane(focusedPaneId);
    }
    return;
  }
  // Workstream action shortcuts — only preventDefault when a workstream
  // is active, so native browser shortcuts (e.g. Ctrl+Shift+R hard reload)
  // still work when no workstream is focused.
  if (e.ctrlKey && e.shiftKey) {
    closeTabDropdown();
    const wsActionKey = e.key.toLowerCase();
    const activeWsId = !dashboardVisible && getCurrentWsId();
    if (wsActionKey === "e" && activeWsId) {
      e.preventDefault();
      editWorkstreamTitle();
      return;
    }
    if (wsActionKey === "f" && activeWsId) {
      e.preventDefault();
      forkWorkstream();
      return;
    }
    // X not D — D conflicts with Chrome DevTools
    if (
      wsActionKey === "x" &&
      activeWsId &&
      Object.keys(workstreams).length > 1
    ) {
      e.preventDefault();
      confirmDeleteWorkstream();
      return;
    }
  }
  // Ctrl+W: close current workstream tab
  if (e.ctrlKey && !e.shiftKey && e.key === "w") {
    closeTabDropdown();
    if (Object.keys(workstreams).length > 1) {
      e.preventDefault();
      closeWorkstream(currentWsId);
    }
    return;
  }

  // Ctrl+Alt+Arrow: cycle pane focus
  if (
    e.ctrlKey &&
    e.altKey &&
    (e.key === "ArrowLeft" || e.key === "ArrowRight")
  ) {
    e.preventDefault();
    const paneIds = [];
    (function collectIds(n) {
      if (!n) return;
      if (n.type === "leaf") {
        paneIds.push(n.pane.id);
      } else {
        collectIds(n.children[0]);
        collectIds(n.children[1]);
      }
    })(splitRoot);
    if (paneIds.length > 1) {
      let ci = paneIds.indexOf(focusedPaneId);
      if (e.key === "ArrowRight") ci = (ci + 1) % paneIds.length;
      else ci = (ci - 1 + paneIds.length) % paneIds.length;
      setFocusedPane(paneIds[ci]);
      panes[paneIds[ci]].inputEl.focus();
    }
    return;
  }

  // Ctrl+\: split pane
  if (e.ctrlKey && e.code === "Backslash") {
    e.preventDefault();
    if (e.shiftKey) splitPane(focusedPaneId, "vertical");
    else splitPane(focusedPaneId, "horizontal");
    return;
  }

  // Inline approval keybindings
  if (pane && pane.pendingApproval) {
    const fbInput =
      pane.approvalBlockEl &&
      pane.approvalBlockEl.querySelector(".ts-approval-feedback");
    if (fbInput && document.activeElement === fbInput) {
      if (e.key === "Enter") {
        e.preventDefault();
        pane.resolveApproval(true, false, pane.getFeedback());
      } else if (e.key === "Escape") {
        e.preventDefault();
        pane.resolveApproval(false, false, pane.getFeedback());
      }
      return;
    }
    // Not in feedback input — intercept shortcut keys
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "y" || e.key === "Enter") {
      pane.resolveApproval(true, false, pane.getFeedback());
    } else if (e.key === "n" || e.key === "Escape") {
      pane.resolveApproval(false, false, pane.getFeedback());
    } else if (e.key === "a") {
      pane.resolveApproval(true, true, pane.getFeedback());
    } else if (e.key === "d") {
      const details = pane.approvalBlockEl
        ? pane.approvalBlockEl.querySelectorAll(".verdict-detail")
        : [];
      details.forEach(function (d) {
        const isHidden = d.style.display === "none";
        d.style.display = isHidden ? "block" : "none";
        const btn2 = d.previousElementSibling
          ? d.previousElementSibling.querySelector(".verdict-expand")
          : null;
        if (btn2) btn2.textContent = isHidden ? "hide" : "details";
      });
    }
    return;
  }

  // Plan dialog
  if (document.getElementById("plan-overlay").classList.contains("active")) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      resolvePlan("");
    } else if (e.key === "Escape") {
      e.preventDefault();
      const hasFb =
        document.getElementById("plan-feedback").value.trim().length > 0;
      resolvePlan(hasFb ? "" : "reject");
    } else if (e.key === "Tab") {
      const focusable = document.querySelectorAll(
        "#plan-dialog input, #plan-dialog button",
      );
      const first = focusable[0],
        last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }
});

// ===========================================================================
//  16. Init
// ===========================================================================

function initWorkstreams() {
  authFetch("/v1/api/workstreams")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      data.workstreams.forEach(function (ws) {
        workstreams[ws.ws_id] = { name: ws.name, state: ws.state };
      });
      connectGlobalSSE();
      const wsIds = Object.keys(workstreams);
      if (!wsIds.length) {
        renderTabBar();
        showDashboard();
        return;
      }
      if (!Object.keys(panes).length) {
        if (!restoreLayout()) {
          const p = createPane(wsIds[0]);
          splitRoot = { type: "leaf", pane: p };
          setFocusedPane(p.id);
        }
        renderLayout();
      }
      renderTabBar();
      for (let id in panes) {
        if (!panes[id].evtSource) {
          panes[id].showEmptyState();
          panes[id]._loadHistoryThenConnect(panes[id].wsId);
        }
      }
      const params = new URLSearchParams(location.search);
      const targetWs = params.get("ws_id");
      if (targetWs && workstreams[targetWs]) {
        history.replaceState(
          { turnstone: "workstream", wsId: targetWs },
          "",
          location.pathname,
        );
        _historyNavigation = true;
        try {
          switchTab(targetWs);
        } finally {
          _historyNavigation = false;
        }
      } else {
        history.replaceState({ turnstone: "dashboard" }, "", location.pathname);
        showDashboard();
      }
    });
}

initLogin();
pollHealth();
loadInterfaceSettings();
initWorkstreams();
loadPendingConsents();

// Free the HTTP/1.1 6-connection-per-host budget before the refresh
// document fetch starts.  Each pane holds a long-lived per-ws SSE +
// the global SSE; at 5–6 panes the cap is hit and the new document
// load queues behind the existing connections.  Chrome leaves the
// document fetch in (pending) indefinitely; Firefox surfaces
// "interrupted while page was loading" and leaves the new page
// stuck on "Loading…".  Best-effort close on unload frees the slots.
//
// Per-pane teardown goes through `disconnectSSE()` instead of a bare
// `evtSource.close()` so the pane's `_cancelTimeout` / `_forceTimeout`
// timers also get cleared.  Otherwise — in the edge case where
// beforeunload fires but navigation is then cancelled (see the
// defensive-reconnect block below) — those timers can still fire on
// a now-disconnected pane and mutate UI state.
//
// Tactical only — the canonical fix is console-side SSE fan-in
// tracked at https://github.com/turnstonelabs/turnstone/issues/540.
window.addEventListener("beforeunload", function () {
  try {
    if (globalEvtSource) {
      globalEvtSource.close();
      globalEvtSource = null;
    }
    for (const id in panes) {
      if (panes[id]) panes[id].disconnectSSE();
    }
  } catch (_e) {
    /* best-effort — never block unload */
  }
});

// Defensive reconnect: covers the edge case where beforeunload fires but
// navigation is then cancelled (e.g. another beforeunload listener — present
// or future — sets returnValue and the user picks "Stay" in the dialog).
// In that path, our handler already disconnected the SSEs but the page is
// still alive with no automatic reconnect.  Both events are registered
// because they catch different cancellation shapes: visibilitychange fires
// on hide/show; focus fires when the window regains focus from a modal /
// browser-UI / OS-level interruption.  Idempotent — when SSEs are alive
// the check is a no-op, so this is also safe on every tab return.
//
// Reconnect condition handles both shapes the beforeunload handler can
// leave behind: `disconnectSSE()` nulls `evtSource`; older non-handler
// close paths may leave it non-null in CLOSED state.  Either way means
// "not actively streaming for a pane that has a workstream attached".
//
// Out of scope here: visibility-based DISCONNECT (close-on-hidden to
// support many tabs).  That belongs to the fan-in design — issue #540.
function _reconnectDeadSSEs() {
  if (!globalEvtSource || globalEvtSource.readyState === EventSource.CLOSED) {
    connectGlobalSSE();
  }
  for (const id in panes) {
    const p = panes[id];
    if (!p || !p.wsId) continue;
    const live =
      p.evtSource &&
      (p.evtSource.readyState === EventSource.OPEN ||
        p.evtSource.readyState === EventSource.CONNECTING);
    if (!live) p.connectSSE(p.wsId);
  }
}
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "visible") _reconnectDeadSSEs();
});
window.addEventListener("focus", _reconnectDeadSSEs);

function loadInterfaceSettings() {
  authFetch("/v1/api/admin/settings")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      const settings = data.settings || [];
      for (let i = 0; i < settings.length; i++) {
        const s = settings[i];
        if (s.key && s.key.indexOf("interface.") === 0) {
          const lsKey = "turnstone_" + s.key;
          try {
            // Only write server value if no local value exists — this
            // preserves the user's theme choice when switching between
            // nodes via the console proxy (each node may return a
            // different default).
            if (!localStorage.getItem(lsKey) && s.source === "storage") {
              localStorage.setItem(lsKey, s.value);
            }
          } catch (_) {}
        }
      }
      // Apply theme from localStorage (set by theme.js initTheme or
      // a previous toggle) — don't let a node's default override it.
      const theme = localStorage.getItem("turnstone_interface.theme");
      const currentTheme = document.documentElement.dataset.theme;
      if (theme) {
        const effectiveTheme = theme === "light" ? "light" : "";
        if (effectiveTheme !== currentTheme) {
          document.documentElement.dataset.theme = effectiveTheme;
          const btn = document.getElementById("theme-toggle");
          if (btn) {
            btn.textContent = theme === "light" ? "\u2600" : "\u263E";
            btn.title =
              theme === "light"
                ? "Switch to dark theme"
                : "Switch to light theme";
          }
          reRenderAllMermaid();
        }
      }
    })
    .catch(function (err) {
      // Silently ignore — settings are optional on load
    });
}

// Back/forward button: retrace dashboard -> tab navigation.
window.addEventListener("popstate", function (e) {
  _historyNavigation = true;
  try {
    if (e.state && e.state.turnstone === "workstream") {
      if (dashboardVisible) hideDashboard();
      if (e.state.wsId && workstreams[e.state.wsId]) switchTab(e.state.wsId);
    } else {
      if (!dashboardVisible) showDashboard();
    }
  } finally {
    _historyNavigation = false;
  }
});
