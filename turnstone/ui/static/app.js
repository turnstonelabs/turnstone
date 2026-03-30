// ===========================================================================
//  turnstone server UI — app.js
//  Split-pane layout with per-workstream Pane instances and binary layout tree
// ===========================================================================

// ===========================================================================
//  1. Pane class — per-workstream UI state
// ===========================================================================

var _paneCounter = 0;

function Pane(wsId) {
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
  this._cancelTimeout = null;
  this._forceTimeout = null;
  this._pendingEditSend = null;
  this._createDOM();
}

Pane.prototype._createDOM = function () {
  var self = this;

  this.el = document.createElement("div");
  this.el.className = "pane";
  this.el.dataset.paneId = this.id;

  // Focus on mousedown (before child clicks)
  this.el.addEventListener("mousedown", function () {
    setFocusedPane(self.id);
  });
  // Also track keyboard focus moving into this pane (e.g. Tab into textarea)
  this.el.addEventListener(
    "focusin",
    function () {
      setFocusedPane(self.id);
    },
    true,
  );

  // Right-click context menu for split/close actions — skip interactive
  // elements (textareas, links, buttons) so native copy/paste works
  this.el.addEventListener("contextmenu", function (e) {
    var tag = e.target.tagName;
    if (
      tag === "TEXTAREA" ||
      tag === "INPUT" ||
      tag === "A" ||
      tag === "BUTTON" ||
      e.target.isContentEditable
    )
      return;
    var sel = window.getSelection();
    if (sel && sel.toString().length > 0) return;
    e.preventDefault();
    setFocusedPane(self.id);
    showPaneContextMenu(e.clientX, e.clientY, self.id);
  });

  // Pane header (visible only in multi-pane mode)
  this.headerEl = document.createElement("div");
  this.headerEl.className = "pane-header";

  var wsName = document.createElement("span");
  wsName.className = "pane-ws-name";
  wsName.textContent = this.wsId
    ? (workstreams[this.wsId] && workstreams[this.wsId].name) ||
      this.wsId.substring(0, 8)
    : "";
  this.headerEl.appendChild(wsName);

  var actions = document.createElement("div");
  actions.className = "pane-actions";

  var splitRightBtn = document.createElement("button");
  splitRightBtn.className = "pane-action-btn";
  splitRightBtn.title = "Split right";
  splitRightBtn.setAttribute("aria-label", "Split right");
  splitRightBtn.textContent = "\u2502";
  splitRightBtn.onclick = function (e) {
    e.stopPropagation();
    splitPane(self.id, "horizontal");
  };
  actions.appendChild(splitRightBtn);

  var splitDownBtn = document.createElement("button");
  splitDownBtn.className = "pane-action-btn";
  splitDownBtn.title = "Split down";
  splitDownBtn.setAttribute("aria-label", "Split down");
  splitDownBtn.textContent = "\u2500";
  splitDownBtn.onclick = function (e) {
    e.stopPropagation();
    splitPane(self.id, "vertical");
  };
  actions.appendChild(splitDownBtn);

  var closeBtn = document.createElement("button");
  closeBtn.className = "pane-action-btn pane-close-btn";
  closeBtn.title = "Close pane";
  closeBtn.setAttribute("aria-label", "Close pane");
  closeBtn.textContent = "\u00d7";
  closeBtn.onclick = function (e) {
    e.stopPropagation();
    if (countLeaves(splitRoot) > 1) closePane(self.id);
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

  // Input area
  var inputArea = document.createElement("div");
  inputArea.className = "pane-input-area";

  this.inputEl = document.createElement("textarea");
  this.inputEl.className = "pane-input";
  this.inputEl.rows = 1;
  this.inputEl.placeholder = "Type a message\u2026 (Shift+Enter for newline)";
  this.inputEl.setAttribute("aria-label", "Message input");
  this.inputEl.addEventListener("input", function () {
    self._autoResize();
  });
  this.inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      self.sendMessage();
    }
  });
  inputArea.appendChild(this.inputEl);

  this.sendBtn = document.createElement("button");
  this.sendBtn.className = "pane-send";
  this.sendBtn.textContent = "Send";
  this.sendBtn.onclick = function () {
    self.sendMessage();
  };
  inputArea.appendChild(this.sendBtn);

  this.stopBtn = document.createElement("button");
  this.stopBtn.className = "pane-stop";
  this.stopBtn.style.display = "none";
  this.stopBtn.textContent = "\u25a0 Stop";
  this.stopBtn.setAttribute("aria-label", "Stop generation");
  this.stopBtn.onclick = function () {
    self.cancelGeneration();
  };
  inputArea.appendChild(this.stopBtn);

  this.el.appendChild(inputArea);
};

Pane.prototype.reset = function () {
  this.currentAssistantEl = null;
  this.currentReasoningEl = null;
  this.contentBuffer = "";
  this.setBusy(false);
  this.pendingApproval = false;
  this.approvalBlockEl = null;
  this._pendingEditSend = null;
  this.inputEl.disabled = false;
};

Pane.prototype.updateWsName = function () {
  var nameEl = this.headerEl.querySelector(".pane-ws-name");
  if (nameEl) {
    nameEl.textContent = this.wsId
      ? (workstreams[this.wsId] && workstreams[this.wsId].name) ||
        this.wsId.substring(0, 8)
      : "";
  }
};

Pane.prototype.disconnectSSE = function () {
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
};

Pane.prototype.setBusy = function (b) {
  this.busy = b;
  this.messagesEl.dataset.busy = b ? "true" : "false";
  this.sendBtn.disabled = b;
  this.sendBtn.style.display = b ? "none" : "";
  this.stopBtn.style.display = b ? "" : "none";
  this.stopBtn.disabled = !b;
  this.stopBtn.textContent = "\u25a0 Stop";
  this.stopBtn.setAttribute("aria-label", "Stop generation");
  delete this.stopBtn.dataset.forceCancel;
};

Pane.prototype.showEmptyState = function () {
  if (!this.messagesEl.querySelector(".empty-state")) {
    var el = document.createElement("div");
    el.className = "empty-state";
    el.textContent = "Type a message to start";
    this.messagesEl.appendChild(el);
  }
};

Pane.prototype.removeEmptyState = function () {
  var el = this.messagesEl.querySelector(".empty-state");
  if (el) el.remove();
};

Pane.prototype.connectSSE = function (wsId) {
  var self = this;
  this.disconnectSSE();
  this.wsId = wsId;

  this.evtSource = new EventSource(
    "/v1/api/events?ws_id=" + encodeURIComponent(wsId),
  );

  this.evtSource.onopen = function () {
    self.retryDelay = 1000;
    self.statusBarEl.classList.remove("ws-sb-disconnected");
    if (self._lastStatusEvt) self.updateStatus(self._lastStatusEvt);
  };

  this.evtSource.onmessage = function (e) {
    var data = JSON.parse(e.data);
    self.handleEvent(data);
  };

  this.evtSource.onerror = function () {
    self.evtSource.close();
    self.evtSource = null;
    var loginOverlay = document.getElementById("login-overlay");
    if (loginOverlay && loginOverlay.style.display !== "none") return;
    self.statusBarEl.classList.add("ws-sb-disconnected");
    self._sbTokens.textContent = "Reconnecting\u2026";
    // Only the focused pane refreshes the global workstream list to avoid
    // race conditions when multiple panes disconnect simultaneously.
    if (self.id === focusedPaneId) {
      fetch("/v1/api/workstreams")
        .then(function (r) {
          if (r.status === 401) {
            showLogin();
            return;
          }
          return r.json().then(function (data) {
            workstreams = {};
            (data.workstreams || []).forEach(function (ws) {
              workstreams[ws.id] = { name: ws.name, state: ws.state };
            });
            renderTabBar();
            // Reconnect all disconnected panes, reassigning stale ws_ids
            for (var pid in panes) {
              var p = panes[pid];
              if (p.wsId && !workstreams[p.wsId]) {
                var ids = Object.keys(workstreams);
                if (ids.length) {
                  p.wsId = ids[0];
                  p.messagesEl.innerHTML = "";
                  p.showEmptyState();
                  p.updateWsName();
                } else {
                  showDashboard();
                  return;
                }
              }
              if (pid === focusedPaneId) currentWsId = p.wsId;
              if (!p.evtSource) {
                setTimeout(
                  (function (pp) {
                    return function () {
                      pp.connectSSE(pp.wsId);
                    };
                  })(p),
                  self.retryDelay,
                );
              }
            }
            self.retryDelay = Math.min(self.retryDelay * 2, 30000);
          });
        })
        .catch(function () {
          setTimeout(function () {
            self.connectSSE(self.wsId);
          }, self.retryDelay);
          self.retryDelay = Math.min(self.retryDelay * 2, 30000);
        });
    } else {
      // Non-focused pane: just retry own connection after delay
      setTimeout(function () {
        self.connectSSE(self.wsId);
      }, self.retryDelay);
      self.retryDelay = Math.min(self.retryDelay * 2, 30000);
    }
  };
};

Pane.prototype.handleEvent = function (evt) {
  var self = this;
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
        this.currentReasoningEl.className = "msg msg-assistant reasoning";
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
        this.currentAssistantEl.className = "msg msg-assistant";
        this.messagesEl.appendChild(this.currentAssistantEl);
      }
      this.contentBuffer += evt.text;
      this.currentAssistantEl.innerHTML = renderMarkdown(this.contentBuffer);
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
      if (this.currentAssistantEl && this.contentBuffer) {
        this.currentAssistantEl.innerHTML = renderMarkdown(this.contentBuffer); // sanitized by renderMarkdown — see renderer.js
        postRenderMarkdown(this.currentAssistantEl);
      }
      this.currentAssistantEl = null;
      this.currentReasoningEl = null;
      this.contentBuffer = "";
      this.scrollToBottom(true);
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

    case "info":
      this.addInfoMessage(evt.message);
      break;

    case "error":
      // Show the error but don't change busy state — state_change
      // handles idle/error transitions.  on_error fires for non-terminal
      // errors (tool parse failures, truncation) mid-turn too.
      this.addErrorMessage(evt.message);
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
      var self = this;
      this._cancelTimeout = setTimeout(function () {
        if (self.busy) {
          self.stopBtn.disabled = false;
          self.stopBtn.textContent = "\u26a0 Force Stop";
          self.stopBtn.setAttribute("aria-label", "Force stop generation");
          self.stopBtn.dataset.forceCancel = "true";
        }
      }, 2000);
      this._forceTimeout = setTimeout(function () {
        if (self.busy) {
          self.addInfoMessage(
            "Cancel didn\u2019t complete in time. You may need to resend your last message.",
          );
          self.setBusy(false);
        }
      }, 10000);
      break;

    case "connected":
      this.model = evt.model || "";
      this.modelAlias = evt.model_alias || evt.model || "";
      this._sbModel.textContent = this.modelAlias || this.model || "";
      this._sbModel.title = this.model || "";
      if (evt.skip_permissions) {
        var existing = document.querySelector(".skip-permissions-warning");
        if (!existing) {
          var warn = document.createElement("div");
          warn.className = "skip-permissions-warning";
          warn.textContent =
            "\u26a0 Running with --skip-permissions: all tool calls are auto-approved";
          document.getElementById("header").appendChild(warn);
        }
      }
      break;

    case "history":
      this.replayHistory(evt.messages);
      // Dispatch pending edit-and-resend after rewind history arrives
      if (this._pendingEditSend) {
        var editText = this._pendingEditSend;
        this._pendingEditSend = null;
        this.setBusy(true);
        this.addUserMessage(editText);
        authFetch("/v1/api/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: editText, ws_id: self.wsId }),
        }).catch(function (err) {
          self.addErrorMessage("Connection error: " + err.message);
          self.setBusy(false);
        });
      }
      break;

    case "clear_ui":
      this.messagesEl.innerHTML = "";
      break;
  }
};

Pane.prototype.addThinkingIndicator = function () {
  if (this.messagesEl.querySelector(".thinking-indicator")) return;
  var el = document.createElement("div");
  el.className = "thinking-indicator";
  el.textContent = "Thinking";
  this.messagesEl.appendChild(el);
  this.scrollToBottom();
};

Pane.prototype.removeThinkingIndicator = function () {
  var el = this.messagesEl.querySelector(".thinking-indicator");
  if (el) el.remove();
};

Pane.prototype.addUserMessage = function (text) {
  this.removeEmptyState();
  var el = document.createElement("div");
  el.className = "msg msg-user";
  el.textContent = text;
  this._addUserMsgActions(el, text);
  this.messagesEl.appendChild(el);
  this.scrollToBottom(true);
};

Pane.prototype._addUserMsgActions = function (el, text) {
  var self = this;
  var bar = document.createElement("div");
  bar.className = "msg-actions";
  bar.setAttribute("role", "toolbar");
  bar.setAttribute("aria-label", "Message actions");
  // Edit button
  var editBtn = document.createElement("button");
  editBtn.className = "msg-action-btn";
  editBtn.title = "Edit & resend";
  editBtn.setAttribute("aria-label", "Edit and resend this message");
  var editIcon = document.createElement("span");
  editIcon.className = "icon-edit";
  editIcon.setAttribute("aria-hidden", "true");
  editBtn.appendChild(editIcon);
  editBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    self._startEdit(el, text);
  });
  bar.appendChild(editBtn);
  // Rewind-to-here button
  var rewindBtn = document.createElement("button");
  rewindBtn.className = "msg-action-btn";
  rewindBtn.title = "Rewind to before this message";
  rewindBtn.setAttribute(
    "aria-label",
    "Rewind conversation to before this message",
  );
  var rewindIcon = document.createElement("span");
  rewindIcon.className = "icon-rewind";
  rewindIcon.setAttribute("aria-hidden", "true");
  rewindBtn.appendChild(rewindIcon);
  rewindBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    self._rewindToMessage(el);
  });
  bar.appendChild(rewindBtn);
  el.appendChild(bar);
};

Pane.prototype._addRetryAction = function (el) {
  var self = this;
  var bar = el.querySelector(".msg-actions");
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Message actions");
    el.appendChild(bar);
  }
  var btn = document.createElement("button");
  btn.className = "msg-action-btn";
  btn.title = "Retry (regenerate response)";
  btn.setAttribute("aria-label", "Retry last response");
  var icon = document.createElement("span");
  icon.className = "icon-retry";
  icon.setAttribute("aria-hidden", "true");
  btn.appendChild(icon);
  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    self._retryLast();
  });
  bar.insertBefore(btn, bar.firstChild);
};

Pane.prototype._retryLast = function () {
  if (this.busy) return;
  var self = this;
  authFetch("/v1/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command: "/retry", ws_id: this.wsId }),
  }).catch(function (err) {
    self.addErrorMessage("Retry failed: " + err.message);
  });
};

Pane.prototype._rewindToMessage = function (msgEl) {
  if (this.busy) return;
  var self = this;
  // Count how many user messages come at or after this one
  var userMsgs = this.messagesEl.querySelectorAll(".msg-user");
  var idx = Array.prototype.indexOf.call(userMsgs, msgEl);
  if (idx < 0) return;
  var turnsToRewind = userMsgs.length - idx;
  if (turnsToRewind < 1) return;
  authFetch("/v1/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      command: "/rewind " + turnsToRewind,
      ws_id: this.wsId,
    }),
  }).catch(function (err) {
    self.addErrorMessage("Rewind failed: " + err.message);
  });
};

Pane.prototype._startEdit = function (msgEl, originalText) {
  if (this.busy) return;
  var self = this;
  // Save current child nodes for cancel restoration
  var savedNodes = [];
  while (msgEl.firstChild) {
    savedNodes.push(msgEl.removeChild(msgEl.firstChild));
  }
  msgEl.classList.add("msg-editing");

  var form = document.createElement("div");
  form.className = "msg-edit-form";

  var textarea = document.createElement("textarea");
  textarea.className = "msg-edit-textarea";
  textarea.setAttribute("aria-label", "Edit message text");
  textarea.value = originalText;
  textarea.rows = Math.min(originalText.split("\n").length + 1, 8);
  form.appendChild(textarea);

  var actions = document.createElement("div");
  actions.className = "msg-edit-actions";

  var cancelBtn = document.createElement("button");
  cancelBtn.className = "msg-edit-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", function () {
    // Restore original nodes
    while (msgEl.firstChild) msgEl.removeChild(msgEl.firstChild);
    savedNodes.forEach(function (n) {
      msgEl.appendChild(n);
    });
    msgEl.classList.remove("msg-editing");
  });
  actions.appendChild(cancelBtn);

  var sendBtn = document.createElement("button");
  sendBtn.className = "msg-edit-btn msg-edit-btn-send";
  sendBtn.textContent = "Send";
  sendBtn.addEventListener("click", function () {
    var newText = textarea.value.trim();
    if (!newText) return;
    self._editAndResend(msgEl, newText);
  });
  actions.appendChild(sendBtn);

  // Ctrl+Enter to send, Escape to cancel
  textarea.addEventListener("keydown", function (e) {
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
};

Pane.prototype._editAndResend = function (msgEl, newText) {
  if (this.busy) return;
  var self = this;
  // Count turns to rewind (from this message onward)
  var userMsgs = this.messagesEl.querySelectorAll(".msg-user");
  var idx = Array.prototype.indexOf.call(userMsgs, msgEl);
  if (idx < 0) return;
  var turnsToRewind = userMsgs.length - idx;

  this.setBusy(true);
  // Store pending send — dispatched when the rewind history event arrives
  this._pendingEditSend = newText;
  authFetch("/v1/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      command: "/rewind " + turnsToRewind,
      ws_id: self.wsId,
    }),
  })
    .then(function (r) {
      if (r && !r.ok) {
        self._pendingEditSend = null;
        self.setBusy(false);
        self.addErrorMessage(
          "Rewind failed (HTTP " + r.status + " " + r.statusText + ")",
        );
      }
    })
    .catch(function (err) {
      self._pendingEditSend = null;
      self.addErrorMessage("Rewind failed: " + err.message);
      self.setBusy(false);
    });
};

Pane.prototype.replayHistory = function (messages) {
  var self = this;
  this.messagesEl.innerHTML = "";
  if (!messages.length) {
    this.showEmptyState();
    return;
  }
  var lastToolBlock = null;
  for (var i = 0; i < messages.length; i++) {
    var msg = messages[i];
    if (msg.role === "user") {
      this.addUserMessage(msg.content || "");
      lastToolBlock = null;
    } else if (msg.role === "assistant") {
      if (msg.tool_calls && msg.tool_calls.length) {
        if (msg.pending) {
          lastToolBlock = null;
        } else {
          var wasDenied = !!msg.denied;
          var block = document.createElement("div");
          block.className =
            "msg approval-block " + (wasDenied ? "denied" : "approved");
          msg.tool_calls.forEach(function (tc) {
            var div = document.createElement("div");
            div.className = "approval-tool";
            div.dataset.funcName = tc.name;
            div.dataset.callId = tc.id || "";
            var nameEl = document.createElement("div");
            nameEl.className = "tool-name";
            nameEl.textContent = tc.name;
            div.appendChild(nameEl);
            var cmd = document.createElement("div");
            cmd.className = "tool-cmd";
            try {
              var args = JSON.parse(tc.arguments);
              var preview = Object.values(args)[0] || "";
              if (tc.name === "bash") {
                cmd.innerHTML =
                  '<span class="dollar">$ </span>' +
                  escapeHtml(String(preview));
              } else {
                cmd.textContent = String(preview).substring(0, 200);
              }
            } catch (e) {
              cmd.textContent = tc.arguments.substring(0, 100);
            }
            div.appendChild(cmd);
            block.appendChild(div);
          });
          var badge = document.createElement("div");
          badge.setAttribute("role", "status");
          if (wasDenied) {
            badge.className = "approval-badge badge-denied";
            badge.textContent = "\u2717 denied";
          } else {
            badge.className = "approval-badge badge-approved";
            badge.textContent = "\u2713 approved";
          }
          block.appendChild(badge);
          self.messagesEl.appendChild(block);
          lastToolBlock = block;
        }
      }
      if (msg.content) {
        var el = document.createElement("div");
        el.className = "msg msg-assistant";
        el.innerHTML = renderMarkdown(msg.content);
        postRenderMarkdown(el);
        self.messagesEl.appendChild(el);
        lastToolBlock = null;
      }
    } else if (msg.role === "tool") {
      if (lastToolBlock) {
        var stripped = stripAnsi(msg.content || "").trim();
        var isDenied =
          msg.denied ||
          /^Denied by user/.test(stripped) ||
          /^Blocked/.test(stripped);
        var isToolError = !!msg.is_error;
        if (stripped && !isDenied) {
          var out = document.createElement("div");
          out.className =
            "tool-output" + (isToolError ? " tool-output-error" : "");
          out.textContent = stripped;
          if (stripped.split("\n").length > 10) {
            makeCollapsible(out);
          }
          var bdg = lastToolBlock.querySelector(".approval-badge");
          if (bdg) lastToolBlock.insertBefore(out, bdg);
          else lastToolBlock.appendChild(out);
        }
        if (isToolError && !lastToolBlock.classList.contains("denied")) {
          lastToolBlock.classList.add("error");
          var errorBdg = lastToolBlock.querySelector(".approval-badge");
          if (errorBdg) {
            errorBdg.className = "approval-badge badge-error";
            errorBdg.textContent = "\u2717 error";
          }
        }
      }
    }
  }
  this._attachRetryToLastAssistant();
  this.scrollToBottom();
};

Pane.prototype._attachRetryToLastAssistant = function () {
  // Remove any previous retry buttons
  var old = this.messagesEl.querySelectorAll(".msg-assistant .msg-actions");
  for (var i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
  // Find the last assistant message with content and add retry
  var assistants = this.messagesEl.querySelectorAll(".msg-assistant");
  if (assistants.length) {
    var last = assistants[assistants.length - 1];
    // Only add if it's not a reasoning block
    if (!last.classList.contains("reasoning")) {
      this._addRetryAction(last);
    }
  }
};

Pane.prototype.showInlineToolBlock = function (
  items,
  autoApproved,
  judgePending,
) {
  var self = this;
  var block = document.createElement("div");
  block.className = "msg approval-block" + (autoApproved ? " approved" : "");
  if (!autoApproved) {
    block.setAttribute("role", "alertdialog");
    block.setAttribute("aria-label", "Tool approval required");
  }

  // Track the highest-priority recommendation for glow
  var glowRec = null;

  items.forEach(function (item) {
    block.appendChild(buildToolDiv(item));
    // Render verdict badge if present
    if (item.verdict) {
      block.insertAdjacentHTML(
        "beforeend",
        renderVerdictBadge(item.verdict, judgePending),
      );
      var rec = item.verdict.recommendation || "review";
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
    var badge = document.createElement("div");
    badge.setAttribute("role", "status");
    badge.className = "approval-badge badge-approved";
    badge.textContent = "\u2713 auto-approved";
    block.appendChild(badge);
  } else {
    var prompt = document.createElement("div");
    prompt.className = "approval-prompt";

    // Apply verdict glow on initial heuristic verdict
    if (glowRec) {
      if (glowRec === "approve") prompt.classList.add("verdict-glow-approve");
      else if (glowRec === "deny") prompt.classList.add("verdict-glow-deny");
      else prompt.classList.add("verdict-glow-review");
    }

    var alwaysNames = items
      .filter(function (it) {
        return (
          it.needs_approval &&
          it.func_name &&
          it.func_name !== "__budget_override__" &&
          !it.error
        );
      })
      .map(function (it) {
        return it.approval_label || it.func_name;
      });
    block.dataset.alwaysNames = JSON.stringify(alwaysNames);
    var alwaysTitle = alwaysNames.length
      ? "Always approve " + alwaysNames.join(", ")
      : "Always approve this tool type";

    var actionsDiv = document.createElement("div");
    actionsDiv.className = "approval-actions";

    var approveBtn = document.createElement("button");
    approveBtn.className = "approval-btn btn-approve";
    approveBtn.innerHTML = '<span class="key">y</span> Approve';
    approveBtn.onclick = function () {
      self.resolveApproval(true, false, self.getFeedback());
    };
    actionsDiv.appendChild(approveBtn);

    var denyBtn = document.createElement("button");
    denyBtn.className = "approval-btn btn-deny";
    denyBtn.innerHTML = '<span class="key">n</span> Deny';
    denyBtn.onclick = function () {
      self.resolveApproval(false, false, self.getFeedback());
    };
    actionsDiv.appendChild(denyBtn);

    if (alwaysNames.length) {
      var alwaysBtn = document.createElement("button");
      alwaysBtn.className = "approval-btn btn-always";
      alwaysBtn.title = alwaysTitle;
      alwaysBtn.setAttribute("aria-label", alwaysTitle);
      alwaysBtn.innerHTML = '<span class="key">a</span> Always';
      alwaysBtn.onclick = function () {
        self.resolveApproval(true, true, self.getFeedback());
      };
      actionsDiv.appendChild(alwaysBtn);
    }

    prompt.appendChild(actionsDiv);

    var fbInput = document.createElement("input");
    fbInput.type = "text";
    fbInput.className = "approval-feedback-input";
    fbInput.placeholder = "feedback (optional)";
    prompt.appendChild(fbInput);

    block.appendChild(prompt);
    this.pendingApproval = true;
    this.approvalBlockEl = block;
    this.inputEl.disabled = true;
    this.sendBtn.disabled = true;
    requestAnimationFrame(function () {
      fbInput.focus();
    });
  }

  this.messagesEl.appendChild(block);
  this.scrollToBottom();
};

Pane.prototype.resolveApproval = function (
  approved,
  always,
  feedback,
  skipPost,
) {
  if (!this.approvalBlockEl) return;
  this.pendingApproval = false;

  // Remove prompt
  var prompt = this.approvalBlockEl.querySelector(".approval-prompt");
  if (prompt) prompt.remove();

  // Add badge
  var badge = document.createElement("div");
  badge.setAttribute("role", "status");
  if (approved) {
    badge.className = "approval-badge badge-approved";
    var label = "\u2713 approved";
    if (always) {
      var raw = this.approvalBlockEl.dataset.alwaysNames;
      var names = raw ? JSON.parse(raw) : [];
      label = names.length
        ? "\u2713 always approve " + names.join(", ")
        : "\u2713 always approve";
    }
    badge.textContent = feedback ? label + ": " + feedback : label;
    this.approvalBlockEl.classList.add("approved");
  } else {
    badge.className = "approval-badge badge-denied";
    badge.textContent = "\u2717 denied" + (feedback ? ": " + feedback : "");
    this.approvalBlockEl.classList.add("denied");
  }
  this.approvalBlockEl.appendChild(badge);
  this.approvalBlockEl = null;

  // Re-enable input
  this.inputEl.disabled = false;
  this.sendBtn.disabled = this.busy;
  this.inputEl.focus();

  // POST to server with ws_id (skip when server already resolved, e.g. timeout)
  if (!skipPost) {
    var self = this;
    authFetch("/v1/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved: approved,
        feedback: feedback || null,
        always: !!always,
        ws_id: this.wsId,
      }),
    }).catch(function (err) {
      self.addErrorMessage("Connection error: " + err.message);
    });
  }

  this.scrollToBottom();
};

Pane.prototype.getFeedback = function () {
  if (!this.approvalBlockEl) return null;
  var inp = this.approvalBlockEl.querySelector(".approval-feedback-input");
  return inp && inp.value.trim() ? inp.value.trim() : null;
};

Pane.prototype.appendToolOutputChunk = function (callId, chunk) {
  if (!chunk) return;
  var stripped = stripAnsi(chunk);
  if (!stripped) return;

  var escapedId = callId ? CSS.escape(callId) : "";
  var el = escapedId
    ? this.messagesEl.querySelector(
        '.tool-output-stream[data-call-id="' + escapedId + '"]',
      )
    : null;
  if (!el) {
    var target = escapedId
      ? this.messagesEl.querySelector(
          '.approval-tool[data-call-id="' + escapedId + '"]',
        )
      : null;
    if (!target) {
      var blocks = this.messagesEl.querySelectorAll(".approval-block");
      if (!blocks.length) return;
      var block = blocks[blocks.length - 1];
      var tools = block.querySelectorAll(
        '.approval-tool[data-func-name="bash"]',
      );
      target = tools.length ? tools[tools.length - 1] : null;
      if (!target) {
        var allTools = block.querySelectorAll(".approval-tool");
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
};

Pane.prototype.appendToolOutput = function (callId, name, output, isError) {
  var escapedId = callId ? CSS.escape(callId) : "";
  var target = escapedId
    ? this.messagesEl.querySelector(
        '.approval-tool[data-call-id="' + escapedId + '"]',
      )
    : null;
  if (!target) {
    var blocks = this.messagesEl.querySelectorAll(".approval-block");
    if (!blocks.length) return;
    var block = blocks[blocks.length - 1];
    var tools = block.querySelectorAll(".approval-tool");
    for (var i = tools.length - 1; i >= 0; i--) {
      if (tools[i].dataset.funcName === name) {
        target = tools[i];
        break;
      }
    }
    if (!target && tools.length) target = tools[tools.length - 1];
  }
  if (!target) return;

  // Remove the streaming output element for this tool
  var streamEl = null;
  if (escapedId) {
    streamEl = this.messagesEl.querySelector(
      '.tool-output-stream[data-call-id="' + escapedId + '"]',
    );
  } else {
    var next = target.nextElementSibling;
    if (next && next.classList.contains("tool-output-stream")) {
      streamEl = next;
    }
  }
  if (streamEl) streamEl.remove();

  var stripped = stripAnsi(output || "").trim();
  if (!stripped) return;

  // Style tool output as error when indicated by isError flag
  var out = document.createElement("div");
  out.className = "tool-output" + (isError ? " tool-output-error" : "");
  out.textContent = stripped;

  // Mark the parent approval block as errored
  if (isError) {
    var parentBlock = target.closest(".approval-block");
    if (parentBlock && !parentBlock.classList.contains("denied")) {
      parentBlock.classList.add("error");
      var badge = parentBlock.querySelector(".approval-badge");
      if (badge) {
        badge.className = "approval-badge badge-error";
        badge.textContent = "\u2717 error";
      }
    }
  }

  if (stripped.split("\n").length > 10) {
    makeCollapsible(out);
  }

  target.after(out);
  this.scrollToBottom();
};

Pane.prototype.showOutputWarning = function (evt) {
  if (!evt.call_id || evt.risk_level === "none") return;
  var escapedId = CSS.escape(evt.call_id);
  var toolDiv = this.messagesEl.querySelector(
    '.approval-tool[data-call-id="' + escapedId + '"]',
  );
  if (!toolDiv) return;
  var risk = evt.risk_level || "medium";
  var flags = evt.flags || [];
  var warning = document.createElement("div");
  warning.className = "output-warning output-warning-" + risk;
  warning.setAttribute("role", "alert");
  warning.innerHTML =
    '<span class="output-warning-label">\u26a0 ' +
    escapeHtml(risk.toUpperCase()) +
    "</span> " +
    flags.map(escapeHtml).join(", ");
  if (evt.redacted) {
    warning.innerHTML +=
      ' <span class="output-warning-redacted">(credentials redacted)</span>';
  }
  var nextEl = toolDiv.nextElementSibling;
  if (nextEl && nextEl.classList.contains("tool-output")) {
    nextEl.insertAdjacentElement("afterend", warning);
  } else {
    toolDiv.insertAdjacentElement("afterend", warning);
  }
};

Pane.prototype.updateVerdictBadge = function (verdict) {
  if (!verdict || !verdict.call_id) return;
  var escapedId = CSS.escape(verdict.call_id);
  var badge = this.messagesEl.querySelector(
    '.verdict-badge[data-call-id="' + escapedId + '"]',
  );
  if (!badge) return;

  var risk = verdict.risk_level || "medium";
  badge.className = "verdict-badge verdict-" + risk;

  var riskEl = badge.querySelector(".verdict-risk");
  var recEl = badge.querySelector(".verdict-rec");
  var confEl = badge.querySelector(".verdict-conf");
  if (riskEl) riskEl.textContent = risk.toUpperCase();
  if (recEl) recEl.textContent = verdict.recommendation || "review";
  if (confEl)
    confEl.textContent = Math.round((verdict.confidence || 0) * 100) + "%";

  var spinner = badge.querySelector(".verdict-judge-spinner");
  if (spinner) spinner.remove();

  var detail = badge.nextElementSibling;
  if (detail && detail.classList.contains("verdict-detail")) {
    var summaryEl = detail.querySelector(".verdict-summary");
    var reasonEl = detail.querySelector(".verdict-reasoning");
    var tierEl = detail.querySelector(".verdict-tier");
    if (summaryEl) summaryEl.textContent = verdict.intent_summary || "";
    if (reasonEl) reasonEl.textContent = verdict.reasoning || "";
    if (tierEl)
      tierEl.textContent =
        (verdict.tier || "llm") +
        " tier" +
        (verdict.judge_model ? " | " + verdict.judge_model : "");
    var evidenceEl = detail.querySelector(".verdict-evidence");
    if (verdict.evidence && verdict.evidence.length) {
      if (!evidenceEl) {
        evidenceEl = document.createElement("div");
        evidenceEl.className = "verdict-evidence";
        var tierDiv = detail.querySelector(".verdict-tier");
        if (tierDiv) detail.insertBefore(evidenceEl, tierDiv);
        else detail.appendChild(evidenceEl);
      }
      evidenceEl.innerHTML = verdict.evidence
        .map(function (e) {
          return "<div>\u2022 " + escapeHtml(e) + "</div>";
        })
        .join("");
    } else if (evidenceEl) {
      evidenceEl.remove();
    }
  }

  this.updateVerdictGlow(verdict.recommendation);
};

Pane.prototype.updateVerdictGlow = function (recommendation) {
  if (!this.approvalBlockEl) return;
  var prompt = this.approvalBlockEl.querySelector(".approval-prompt");
  if (!prompt) return;
  prompt.classList.remove(
    "verdict-glow-approve",
    "verdict-glow-deny",
    "verdict-glow-review",
  );
  if (recommendation === "approve")
    prompt.classList.add("verdict-glow-approve");
  else if (recommendation === "deny") prompt.classList.add("verdict-glow-deny");
  else prompt.classList.add("verdict-glow-review");
};

Pane.prototype.addInfoMessage = function (text) {
  var el = document.createElement("div");
  el.className = "msg msg-info";
  el.textContent = stripAnsi(text);
  this.messagesEl.appendChild(el);
  this.scrollToBottom();
};

Pane.prototype.addErrorMessage = function (text) {
  var el = document.createElement("div");
  el.className = "msg msg-error";
  el.setAttribute("role", "alert");
  el.textContent = stripAnsi(text);
  this.messagesEl.appendChild(el);
  this.scrollToBottom();
};

Pane.prototype.updateStatus = function (evt) {
  this._sbModel.textContent = this.modelAlias || this.model || "";
  this._sbModel.title = this.model || "";

  var tokenText =
    evt.total_tokens.toLocaleString() +
    " / " +
    evt.context_window.toLocaleString() +
    " (" +
    evt.pct +
    "%)";
  if (evt.effort && evt.effort !== "medium")
    tokenText += " \u00b7 " + evt.effort;
  if (evt.pct >= 95) tokenText = "\u26a0 " + tokenText;
  else if (evt.pct >= 80) tokenText = "\u25b2 " + tokenText;
  this._sbTokens.textContent = tokenText;

  var tc = evt.tool_calls_this_turn || 0;
  this._sbTools.textContent = tc + " tool" + (tc !== 1 ? "s" : "");

  var turns = evt.turn_count || 0;
  this._sbTurns.textContent = "turn " + turns;

  this.statusBarEl.classList.toggle("ws-sb-warn", evt.pct >= 80);
  this.statusBarEl.classList.toggle("ws-sb-danger", evt.pct >= 95);

  this._lastStatusEvt = evt;
};

Pane.prototype.isNearBottom = function () {
  return (
    this.messagesEl.scrollHeight -
      this.messagesEl.scrollTop -
      this.messagesEl.clientHeight <
    80
  );
};

Pane.prototype.scrollToBottom = function (force) {
  if (force || this.isNearBottom()) {
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }
};

Pane.prototype.sendMessage = function () {
  var text = this.inputEl.value.trim();
  if (!text || this.busy) return;

  if (text.startsWith("/")) {
    authFetch("/v1/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: text, ws_id: this.wsId }),
    });
    this.addUserMessage(text);
    this.inputEl.value = "";
    this._autoResize();
    return;
  }

  var self = this;
  this.setBusy(true);
  this.addUserMessage(text);
  this.inputEl.value = "";
  this._autoResize();

  authFetch("/v1/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, ws_id: this.wsId }),
  }).catch(function (err) {
    self.addErrorMessage("Connection error: " + err.message);
    self.setBusy(false);
  });
};

Pane.prototype.cancelGeneration = function () {
  if (!this.busy || !this.wsId || this.stopBtn.disabled) return;
  var self = this;
  var isForce = this.stopBtn.dataset.forceCancel === "true";
  this.stopBtn.disabled = true;
  authFetch("/v1/api/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ws_id: this.wsId, force: isForce }),
  })
    .then(function () {
      if (isForce) {
        // Force cancel abandons the worker — transition immediately.
        // Clear timeouts to prevent stale timers firing on next send.
        if (self._cancelTimeout) {
          clearTimeout(self._cancelTimeout);
          self._cancelTimeout = null;
        }
        if (self._forceTimeout) {
          clearTimeout(self._forceTimeout);
          self._forceTimeout = null;
        }
        self.addInfoMessage("Force stopped. Previous generation abandoned.");
        self.setBusy(false);
      }
    })
    .catch(function (err) {
      self.addErrorMessage("Cancel error: " + err.message);
      self.stopBtn.disabled = false;
    });
};

Pane.prototype._autoResize = function () {
  this.inputEl.style.height = "auto";
  this.inputEl.style.height = Math.min(this.inputEl.scrollHeight, 200) + "px";
};

// ===========================================================================
//  2. Layout tree + rendering
// ===========================================================================

var panes = {};
var focusedPaneId = null;
var splitRoot = null;
var MAX_PANES = 6;

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
  var p = new Pane(wsId);
  panes[p.id] = p;
  return p;
}

function updatePaneHeaders() {
  var root = document.getElementById("split-root");
  var leafCount = countLeaves(splitRoot);
  if (leafCount > 1) {
    root.classList.add("multi-pane");
  } else {
    root.classList.remove("multi-pane");
  }
  // Hide tab-bar split button when already in multi-pane mode
  var splitBtn = document.getElementById("split-btn");
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
  for (var i = 0; i < node.children.length; i++) {
    var result = findLeafAndParent(node.children[i], paneId, node, i);
    if (result) return result;
  }
  return null;
}

function countLeaves(node) {
  if (!node) return 0;
  if (node.type === "leaf") return 1;
  var count = 0;
  for (var i = 0; i < node.children.length; i++) {
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
    for (var i = 0; i < tree.children.length; i++) {
      if (tree.children[i] === target) {
        tree.children[i] = replacement;
        return tree;
      }
      var result = replaceNode(tree.children[i], target, replacement);
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
  var root = document.getElementById("split-root");
  var minDim = direction === "horizontal" ? 200 : 150;
  var available =
    direction === "horizontal" ? root.clientWidth : root.clientHeight;
  if (available < minDim * 2 + 4) {
    showToast("Not enough space to split");
    return;
  }
  var found = findLeafAndParent(splitRoot, paneId, null, -1);
  if (!found) return;

  // Find a workstream not already shown in any pane
  var wsIds = Object.keys(workstreams);
  var newWsId = null;
  for (var i = 0; i < wsIds.length; i++) {
    var inUse = false;
    for (var pid in panes) {
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

  var newPane = createPane(newWsId);
  var newLeaf = { type: "leaf", pane: newPane };
  var newSplit = {
    type: "split",
    direction: direction,
    children: [found.node, newLeaf],
    ratio: 0.5,
  };

  splitRoot = replaceNode(splitRoot, found.node, newSplit);
  renderLayout();
  setFocusedPane(newPane.id);
  newPane.showEmptyState();
  newPane.connectSSE(newWsId);
}

function closePane(paneId) {
  if (countLeaves(splitRoot) <= 1) return;
  var found = findLeafAndParent(splitRoot, paneId, null, -1);
  if (!found || !found.parent) {
    // paneId is the root leaf — shouldn't happen if count > 1
    // but handle: root must be a split
    if (splitRoot.type === "split") {
      // Find which child contains our pane
      for (var ci = 0; ci < splitRoot.children.length; ci++) {
        var childFound = findLeafAndParent(
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
  var siblingIdx = found.childIndex === 0 ? 1 : 0;
  var sibling = found.parent.children[siblingIdx];

  // Replace parent split with sibling
  splitRoot = replaceNode(splitRoot, found.parent, sibling);

  // Cleanup the closed pane
  var closedPane = panes[paneId];
  if (closedPane) {
    closedPane.disconnectSSE();
    delete panes[paneId];
  }

  // If focused pane was closed, focus first available
  if (focusedPaneId === paneId) {
    var first = getFirstLeaf(splitRoot);
    if (first) {
      focusedPaneId = null; // reset so setFocusedPane triggers
      setFocusedPane(first.id);
    }
  }

  renderLayout();
}

function renderLayout() {
  var root = document.getElementById("split-root");

  // Save scroll positions before clearing
  var scrollPositions = {};
  for (var pid in panes) {
    scrollPositions[pid] = panes[pid].messagesEl.scrollTop;
  }

  // Clear and rebuild
  root.innerHTML = "";
  if (splitRoot) {
    _renderLayoutNode(splitRoot, root);
  }

  // Restore scroll positions
  for (var pid2 in panes) {
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
  var splitContainer = document.createElement("div");
  splitContainer.className = "split-container split-" + node.direction;

  var child0 = document.createElement("div");
  child0.className = "split-child";
  child0.style.flex = String(node.ratio);
  _renderLayoutNode(node.children[0], child0);
  splitContainer.appendChild(child0);

  var handle = document.createElement("div");
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

  var child1 = document.createElement("div");
  child1.className = "split-child";
  child1.style.flex = String(1 - node.ratio);
  _renderLayoutNode(node.children[1], child1);
  splitContainer.appendChild(child1);

  container.appendChild(splitContainer);
  setupDragHandle(handle, node, [child0, child1]);
}

function _dragBounds(node, handle) {
  // Compute min/max ratio from container size and CSS min dimensions
  var container = handle.parentElement;
  var totalSize =
    node.direction === "horizontal"
      ? container.clientWidth
      : container.clientHeight;
  var minPx = node.direction === "horizontal" ? 200 : 150; // match CSS min-width/min-height
  var handlePx = 4;
  var usable = totalSize - handlePx;
  var minRatio = usable > 0 ? Math.max(0.05, minPx / usable) : 0.1;
  var maxRatio = usable > 0 ? Math.min(0.95, 1 - minPx / usable) : 0.9;
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
    var startRatio = node.ratio;
    var bounds = _dragBounds(node, handle);
    var startPos = node.direction === "horizontal" ? e.clientX : e.clientY;
    document.body.style.cursor =
      node.direction === "horizontal" ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";

    var onMove = function (e2) {
      var delta =
        (node.direction === "horizontal" ? e2.clientX : e2.clientY) - startPos;
      var newRatio = Math.max(
        bounds.minRatio,
        Math.min(bounds.maxRatio, startRatio + delta / bounds.totalSize),
      );
      _applyRatio(node, children, handle, newRatio);
    };
    var onUp = function () {
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
    var bounds = _dragBounds(node, handle);
    var step = e.shiftKey ? 0.1 : 0.02;
    var delta = 0;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") delta = step;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") delta = -step;
    else if (e.key === "Home") delta = -(node.ratio - bounds.minRatio);
    else if (e.key === "End") delta = bounds.maxRatio - node.ratio;
    else return;
    e.preventDefault();
    var newRatio = Math.max(
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
    var p = createPane(data.wsId);
    return { type: "leaf", pane: p };
  }
  if (data.type === "split") {
    var left = deserializeLayout(data.children[0], _seen);
    var right = deserializeLayout(data.children[1], _seen);
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
    var data = serializeLayout(splitRoot);
    if (data) {
      localStorage.setItem("turnstone_split_layout", JSON.stringify(data));
    }
  } catch (e) {
    // localStorage may be unavailable
  }
}

function restoreLayout() {
  try {
    var raw = localStorage.getItem("turnstone_split_layout");
    if (!raw) return false;
    var data = JSON.parse(raw);
    var tree = deserializeLayout(data);
    if (!tree) return false;
    splitRoot = tree;
    var first = getFirstLeaf(splitRoot);
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

var _ctxMenu = null;
var _ctxCloseHandler = null;
var _ctxTriggerElement = null;

function showPaneContextMenu(x, y, paneId) {
  closePaneContextMenu();
  _ctxTriggerElement = document.activeElement;

  var menu = document.createElement("div");
  menu.className = "pane-ctx-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Pane actions");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  var canClose = splitRoot && countLeaves(splitRoot) > 1;
  // Can split only if under pane limit AND there's an unused workstream
  var usedWs = {};
  for (var pid in panes) usedWs[panes[pid].wsId] = true;
  var hasUnused = Object.keys(workstreams).some(function (id) {
    return !usedWs[id];
  });
  var canSplit = countLeaves(splitRoot) < MAX_PANES && hasUnused;

  var items = [
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
      var sep = document.createElement("div");
      sep.className = "pane-ctx-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    var btn = document.createElement("button");
    btn.className = "pane-ctx-item";
    btn.setAttribute("role", "menuitem");
    btn.setAttribute("tabindex", "-1");
    btn.disabled = !!item.disabled;
    var labelSpan = document.createElement("span");
    labelSpan.className = "pane-ctx-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    if (item.key) {
      var keySpan = document.createElement("span");
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
  var rect = menu.getBoundingClientRect();
  var mx = x;
  var my = y;
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
        var btns = Array.from(
          menu.querySelectorAll(".pane-ctx-item:not(:disabled)"),
        );
        if (!btns.length) return;
        var idx = btns.indexOf(document.activeElement);
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
    var first = menu.querySelector(".pane-ctx-item:not(:disabled)");
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

// ===========================================================================
//  4. Global state
// ===========================================================================

var workstreams = {};
var currentWsId = null;
var globalEvtSource = null;
var globalRetryDelay = 1000;
var dashboardVisible = false;
var _historyNavigation = false;
var _lastHealth = null;

var STATE_DISPLAY = {
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
      var mcpEl = document.getElementById("mcp-status");
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
      var el = document.getElementById("health-indicator");
      if (!el) return;
      if (data.status === "degraded") {
        el.textContent = "backend down";
        el.className = "health-degraded";
        el.title =
          "Circuit: " +
          ((data.backend && data.backend.circuit_state) || "unknown");
        el.setAttribute(
          "aria-label",
          "Backend degraded. Circuit: " +
            ((data.backend && data.backend.circuit_state) || "unknown"),
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
        var el = document.getElementById("health-indicator");
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
  for (var id in panes) {
    panes[id].disconnectSSE();
    delete panes[id];
  }
  splitRoot = null;
  focusedPaneId = null;
  workstreams = {};
  currentWsId = null;
  document.getElementById("split-root").innerHTML = "";
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
};

// ===========================================================================
//  7. Theme toggle
// ===========================================================================

window.onThemeChange = function (next) {
  var btn = document.getElementById("theme-toggle");
  if (btn) {
    var isLight = next === "light";
    btn.textContent = isLight ? "\u2600" : "\u263E";
    btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark theme" : "Switch to light theme",
    );
  }
  reRenderAllMermaid();
};
(function () {
  var btn = document.getElementById("theme-toggle");
  if (btn) {
    var isLight = document.documentElement.dataset.theme === "light";
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

var tabBar = document.getElementById("tab-bar");
var tabList = document.getElementById("tab-list");
var newTabBtn = document.getElementById("new-tab-btn");

function renderTabBar() {
  tabList.querySelectorAll(".ws-tab").forEach(function (t) {
    t.remove();
  });

  var wsIds = Object.keys(workstreams);
  wsIds.forEach(function (wsId) {
    var ws = workstreams[wsId];
    var tab = document.createElement("div");
    tab.className = "ws-tab" + (wsId === currentWsId ? " active" : "");
    tab.dataset.wsId = wsId;
    tab.setAttribute("role", "tab");
    tab.setAttribute("tabindex", "0");
    tab.setAttribute("aria-selected", wsId === currentWsId ? "true" : "false");
    tab.onclick = function (e) {
      if (e.target.classList.contains("tab-close")) return;
      switchTab(wsId);
    };
    tab.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        switchTab(wsId);
      }
    };

    var indicator = document.createElement("span");
    indicator.className = "tab-indicator";
    indicator.dataset.state = ws.state || "idle";
    indicator.setAttribute("aria-label", ws.state || "idle");
    tab.appendChild(indicator);

    var name = document.createElement("span");
    name.className = "tab-name";
    name.textContent = ws.name || wsId.substring(0, 6);
    tab.appendChild(name);

    if (wsIds.length > 1) {
      var close = document.createElement("button");
      close.className = "tab-close";
      close.innerHTML = "&times;";
      close.title = "Close workstream";
      close.onclick = function (e) {
        e.stopPropagation();
        closeWorkstream(wsId);
      };
      tab.appendChild(close);
    }

    tabList.appendChild(tab);
  });
}

function updateTabIndicator(wsId, state, extra) {
  workstreams[wsId] = workstreams[wsId] || {};
  workstreams[wsId].state = state;
  var tab = tabBar.querySelector('.ws-tab[data-ws-id="' + wsId + '"]');
  if (tab) {
    var ind = tab.querySelector(".tab-indicator");
    if (ind) ind.dataset.state = state;
  }
  var row = document.querySelector(
    '#dash-ws-table .dash-row[data-ws-id="' + wsId + '"]',
  );
  if (row) {
    var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
    row.dataset.state = state;
    var dot = row.querySelector(".dash-state-dot");
    if (dot) dot.dataset.state = state;
    var label = row.querySelector(".dash-state-label");
    if (label) {
      label.dataset.state = state;
      label.textContent = sd.symbol + " " + sd.label;
    }
    if (extra) {
      if (extra.tokens !== undefined) {
        var tokEl = row.querySelector(".dash-cell-tokens");
        if (tokEl) tokEl.textContent = formatTokens(extra.tokens);
      }
      if (extra.context_ratio !== undefined) {
        var ctxEl = row.querySelector(".dash-cell-ctx");
        if (ctxEl) {
          ctxEl.className = "dash-cell-ctx " + ctxClass(extra.context_ratio);
          ctxEl.textContent =
            extra.context_ratio > 0
              ? Math.round(extra.context_ratio * 100) + "%"
              : "";
        }
      }
      if (extra.activity !== undefined) {
        var sub = row.querySelector(".dash-row-sub");
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
  var pane = getFocusedPane();
  if (!pane) return;
  if (wsId === pane.wsId && !dashboardVisible) return;

  // In multi-pane mode, focus an existing pane showing this ws
  if (splitRoot && countLeaves(splitRoot) > 1) {
    for (var pid in panes) {
      if (panes[pid].wsId === wsId && pid !== focusedPaneId) {
        setFocusedPane(pid);
        return;
      }
    }
  }

  pane.reset();
  pane.wsId = wsId;
  currentWsId = wsId;
  pane.messagesEl.innerHTML = "";
  pane.showEmptyState();
  pane.updateWsName();
  renderTabBar();
  pane.connectSSE(wsId);

  if (!_historyNavigation) {
    history.pushState({ turnstone: "workstream", wsId: wsId }, "");
  }
}

// ===========================================================================
//  9. New workstream modal
// ===========================================================================

var _newWsTrapHandler = null;

function newWorkstream() {
  showNewWsModal();
}

function showNewWsModal() {
  var overlay = document.getElementById("new-ws-overlay");
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";

  overlay.onclick = function (e) {
    if (e.target === overlay) hideNewWsModal();
  };

  // Populate model dropdown
  var modelSelect = document.getElementById("new-ws-model");
  var fp = getFocusedPane();
  var curModel = fp ? fp.modelAlias || fp.model || "" : "";
  modelSelect.textContent = "";
  var defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = curModel
    ? "Default (" + curModel + ")"
    : "Default model";
  modelSelect.appendChild(defaultOpt);
  authFetch("/v1/api/models")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.models || []).forEach(function (m) {
        var opt = document.createElement("option");
        opt.value = m.alias;
        opt.textContent =
          m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
        modelSelect.appendChild(opt);
      });
    })
    .catch(function () {
      /* ignore — default model still works */
    });

  var tplSelect = document.getElementById("new-ws-skill");
  tplSelect.innerHTML = '<option value="">Use defaults</option>';
  authFetch("/v1/api/skills")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.skills || []).forEach(function (t) {
        var opt = document.createElement("option");
        opt.value = t.name;
        var label = t.name;
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
  var errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";
  errEl.textContent = "";
  var submitBtn = document.getElementById("new-ws-submit");
  submitBtn.disabled = false;
  submitBtn.textContent = "Create";

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
    var box = document.getElementById("new-ws-box");
    var focusable = box.querySelectorAll(
      'input, select, button, [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable.length) return;
    var first = focusable[0],
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
  document.getElementById("new-ws-overlay").style.display = "none";
  document.body.style.overflow = "";
  if (_newWsTrapHandler) {
    document.removeEventListener("keydown", _newWsTrapHandler);
    _newWsTrapHandler = null;
  }
  document.getElementById("new-tab-btn").focus();
}

function submitNewWs() {
  var submitBtn = document.getElementById("new-ws-submit");
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  submitBtn.textContent = "Creating\u2026";

  var body = {};
  var name = document.getElementById("new-ws-name").value.trim();
  var model = document.getElementById("new-ws-model").value.trim();
  var skill = document.getElementById("new-ws-skill").value;
  if (name) body.name = name;
  if (model) body.model = model;
  if (skill) body.skill = skill;

  var errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";

  authFetch("/v1/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.error) {
        errEl.textContent = data.error;
        errEl.style.display = "block";
        submitBtn.disabled = false;
        submitBtn.textContent = "Create";
        return;
      }
      if (data.ws_id) {
        workstreams[data.ws_id] = { name: data.name, state: "idle" };
        hideNewWsModal();
        switchTab(data.ws_id);
      }
    })
    .catch(function () {
      errEl.textContent = "Failed to create workstream";
      errEl.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = "Create";
    });
}

function _reassignPanesForClosedWs(closedWsId) {
  var remaining = Object.keys(workstreams);
  // Collect panes showing the closed ws
  var affected = [];
  for (var pid in panes) {
    if (panes[pid].wsId === closedWsId) affected.push(pid);
  }
  if (!affected.length) return;

  // Build set of ws_ids already shown by non-affected panes
  var usedWsIds = {};
  for (var pid2 in panes) {
    if (affected.indexOf(pid2) === -1) usedWsIds[panes[pid2].wsId] = true;
  }

  for (var i = 0; i < affected.length; i++) {
    var p = panes[affected[i]];
    // Find an unused ws_id for this pane
    var newWsId = null;
    for (var j = 0; j < remaining.length; j++) {
      if (!usedWsIds[remaining[j]]) {
        newWsId = remaining[j];
        break;
      }
    }
    if (newWsId) {
      // Reassign pane to an unused workstream
      p.disconnectSSE();
      p.wsId = newWsId;
      p.messagesEl.innerHTML = "";
      p.showEmptyState();
      p.updateWsName();
      p.connectSSE(newWsId);
      usedWsIds[newWsId] = true;
    } else if (countLeaves(splitRoot) > 1) {
      // No unused workstream available — close redundant pane
      closePane(affected[i]);
    } else {
      // Last pane — reassign to first remaining ws (will duplicate, but no choice)
      p.disconnectSSE();
      if (remaining.length) {
        p.wsId = remaining[0];
        p.messagesEl.innerHTML = "";
        p.showEmptyState();
        p.updateWsName();
        p.connectSSE(remaining[0]);
      }
    }
  }
  if (focusedPaneId && panes[focusedPaneId]) {
    currentWsId = panes[focusedPaneId].wsId;
  }
}

function closeWorkstream(wsId) {
  authFetch("/v1/api/workstreams/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ws_id: wsId }),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.status === "ok") {
        delete workstreams[wsId];
        renderTabBar();
        _reassignPanesForClosedWs(wsId);
        if (!Object.keys(workstreams).length) showDashboard();
      }
    });
}

// ===========================================================================
//  10. Dashboard
// ===========================================================================

function showDashboard() {
  dashboardVisible = true;
  document.getElementById("dashboard").classList.add("active");
  document.getElementById("header").inert = true;
  document.getElementById("tab-bar").inert = true;
  document.getElementById("split-root").inert = true;
  loadDashboard();
  setTimeout(function () {
    document.getElementById("dashboard-input").focus();
  }, 50);
}

function hideDashboard() {
  dashboardVisible = false;
  document.getElementById("dashboard").classList.remove("active");
  document.getElementById("header").inert = false;
  document.getElementById("tab-bar").inert = false;
  document.getElementById("split-root").inert = false;
  document.getElementById("dashboard-input").value = "";
  var pane = getFocusedPane();
  if (pane) pane.inputEl.focus();
}

function toggleDashboard() {
  if (dashboardVisible) hideDashboard();
  else showDashboard();
}

function loadDashboard() {
  var tableEl = document.getElementById("dash-ws-table");
  tableEl.innerHTML = '<div class="dashboard-empty">Loading\u2026</div>';
  document.getElementById("dashboard-saved-cards").innerHTML =
    '<div class="dashboard-empty">Loading\u2026</div>';
  var dashP = authFetch("/v1/api/dashboard").then(function (r) {
    return r.json();
  });
  var sessP = authFetch("/v1/api/workstreams/saved").then(function (r) {
    return r.json();
  });
  Promise.all([dashP, sessP])
    .then(function (res) {
      var dashData = res[0];
      var wsList = dashData.workstreams || [];
      var agg = dashData.aggregate || {};
      renderDashboardTable(wsList, agg);
      var activeWsIds = {};
      wsList.forEach(function (ws) {
        activeWsIds[ws.id] = true;
      });
      var savedList = (res[1].workstreams || []).filter(function (s) {
        return !activeWsIds[s.ws_id];
      });
      renderSavedWorkstreams(savedList);
    })
    .catch(function () {
      tableEl.innerHTML = '<div class="dashboard-empty">Failed to load</div>';
      document.getElementById("dashboard-saved-cards").innerHTML =
        '<div class="dashboard-empty">Failed to load</div>';
    });
}

function renderDashboardTable(wsList, agg) {
  var activeCount = wsList.filter(function (w) {
    return w.state !== "idle";
  }).length;
  document.getElementById("dash-summary").textContent =
    activeCount + " active \u00b7 " + wsList.length + " total";
  var table = document.getElementById("dash-ws-table");
  table.innerHTML = "";
  if (!wsList.length) {
    table.innerHTML =
      '<div class="dashboard-empty">No active workstreams</div>';
    updateDashFooter(agg);
    return;
  }
  wsList.forEach(function (ws) {
    var liveState =
      (workstreams[ws.id] && workstreams[ws.id].state) || ws.state || "idle";
    var liveName =
      (workstreams[ws.id] && workstreams[ws.id].name) || ws.name || ws.id;
    var sd = STATE_DISPLAY[liveState] || STATE_DISPLAY.idle;

    var row = document.createElement("div");
    row.className = "dash-row";
    row.dataset.wsId = ws.id;
    row.dataset.state = liveState;
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    var ariaLabel = liveName + " \u2014 " + sd.label;
    if (ws.model_alias || ws.model)
      ariaLabel += ", model: " + (ws.model_alias || ws.model);
    if (ws.title) ariaLabel += ", task: " + ws.title;
    if (ws.tokens) ariaLabel += ", " + formatTokens(ws.tokens) + " tokens";
    if (ws.context_ratio > 0)
      ariaLabel += ", " + Math.round(ws.context_ratio * 100) + "% context";
    row.setAttribute("aria-label", ariaLabel);

    var main = document.createElement("div");
    main.className = "dash-row-main";

    var stateCell = document.createElement("span");
    stateCell.className = "dash-cell-state";
    stateCell.innerHTML =
      '<span class="dash-state-dot" data-state="' +
      escapeHtml(liveState) +
      '" aria-hidden="true"></span>' +
      '<span class="dash-state-label" data-state="' +
      escapeHtml(liveState) +
      '">' +
      sd.symbol +
      " " +
      sd.label +
      "</span>";
    main.appendChild(stateCell);

    var nameCell = document.createElement("span");
    nameCell.className = "dash-cell-name";
    nameCell.textContent = liveName;
    main.appendChild(nameCell);

    var modelCell = document.createElement("span");
    modelCell.className = "dash-cell-model";
    modelCell.textContent = ws.model_alias || ws.model || "";
    if (ws.model) modelCell.title = ws.model;
    main.appendChild(modelCell);

    var nodeCell = document.createElement("span");
    nodeCell.className = "dash-cell-node";
    nodeCell.textContent = ws.node || "local";
    if (ws.node) nodeCell.title = ws.node;
    main.appendChild(nodeCell);

    var taskCell = document.createElement("span");
    taskCell.className = "dash-cell-task";
    taskCell.textContent = ws.title || "";
    main.appendChild(taskCell);

    var tokensCell = document.createElement("span");
    tokensCell.className = "dash-cell-tokens";
    tokensCell.textContent = ws.tokens ? formatTokens(ws.tokens) : "";
    main.appendChild(tokensCell);

    var ctxCell = document.createElement("span");
    ctxCell.className = "dash-cell-ctx " + ctxClass(ws.context_ratio);
    ctxCell.textContent =
      ws.context_ratio > 0 ? Math.round(ws.context_ratio * 100) + "%" : "";
    main.appendChild(ctxCell);

    row.appendChild(main);

    var sub = document.createElement("div");
    sub.className = "dash-row-sub";
    if (ws.activity_state === "approval") sub.classList.add("sub-attention");
    sub.textContent = ws.activity || "";
    row.appendChild(sub);

    row.onclick = function () {
      dashboardSwitchWorkstream(ws.id);
    };
    row.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dashboardSwitchWorkstream(ws.id);
      }
    };

    table.appendChild(row);
  });
  updateDashFooter(agg);
  table.onkeydown = function (e) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    var rows = Array.from(table.querySelectorAll(".dash-row"));
    var idx = rows.indexOf(document.activeElement);
    if (idx === -1) return;
    if (e.key === "ArrowDown" && idx < rows.length - 1) rows[idx + 1].focus();
    if (e.key === "ArrowUp" && idx > 0) rows[idx - 1].focus();
  };
}

function updateDashFooter(agg) {
  if (!agg) return;
  var nodesEl = document.getElementById("dash-footer-nodes");
  var statsEl = document.getElementById("dash-footer-stats");
  nodesEl.innerHTML =
    '<span class="dash-footer-node-dot"></span> ' +
    escapeHtml((agg.node || "local") + " (" + (agg.total_count || 0) + " ws)");
  var parts = [];
  if (agg.total_tokens) parts.push(formatTokens(agg.total_tokens) + " tokens");
  if (agg.total_tool_calls) parts.push(agg.total_tool_calls + " tool calls");
  if (agg.uptime_seconds)
    parts.push(formatUptime(agg.uptime_seconds) + " uptime");
  statsEl.textContent = parts.join(" \u00b7 ");
  if (_lastHealth && _lastHealth.status === "degraded") {
    statsEl.textContent +=
      " \u00b7 backend down (circuit " +
      (_lastHealth.backend && _lastHealth.backend.circuit_state
        ? _lastHealth.backend.circuit_state
        : "unknown") +
      ")";
  }
}

function renderSavedWorkstreams(items) {
  var c = document.getElementById("dashboard-saved-cards");
  c.innerHTML = "";
  if (!items.length) {
    c.innerHTML = '<div class="dashboard-empty">No saved workstreams</div>';
    return;
  }
  items.forEach(function (sess) {
    var card = document.createElement("div");
    card.className = "dashboard-card";
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    var label = sess.alias || sess.title || sess.ws_id;
    card.setAttribute("aria-label", "Resume: " + label);
    card.onclick = function () {
      dashboardResumeSession(sess.ws_id);
    };
    card.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dashboardResumeSession(sess.ws_id);
      }
    };
    var title = sess.alias || sess.title || sess.ws_id.substring(0, 12);
    var meta = sess.message_count + " msgs";
    if (sess.updated) meta += " \u00b7 " + formatRelativeTime(sess.updated);
    card.innerHTML =
      '<div class="card-title">' +
      escapeHtml(title) +
      "</div>" +
      '<div class="card-meta">' +
      escapeHtml(meta) +
      "</div>";
    c.appendChild(card);
  });
}

function formatRelativeTime(iso) {
  if (!iso) return "";
  var s = iso.replace(" ", "T");
  if (!s.endsWith("Z") && !s.includes("+")) s += "Z";
  var d = new Date(s);
  if (isNaN(d)) return "";
  var now = new Date();
  var ms = now - d;
  var min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return min + "m ago";
  var hr = Math.floor(min / 60);
  if (hr < 24) return hr + "h ago";
  var day = Math.floor(hr / 24);
  if (day < 30) return day + "d ago";
  return d.toLocaleDateString();
}

function dashboardSwitchWorkstream(wsId) {
  if (workstreams[wsId]) {
    hideDashboard();
    switchTab(wsId);
  } else loadDashboard();
}

function dashboardResumeSession(wsId) {
  authFetch("/v1/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resume_ws: wsId }),
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
      showToast("Failed to resume workstream", "error");
    });
}

function dashboardNewChat() {
  hideDashboard();
  newWorkstream();
}

function dashboardSendMessage() {
  var input = document.getElementById("dashboard-input");
  var text = input.value.trim();
  if (!text) return;
  input.disabled = true;
  var btn = document.querySelector(".dashboard-new-btn");
  btn.disabled = true;
  authFetch("/v1/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (!data.ws_id) {
        input.disabled = false;
        btn.disabled = false;
        return;
      }
      workstreams[data.ws_id] = { name: data.name, state: "idle" };
      switchTab(data.ws_id);
      hideDashboard();
      input.disabled = false;
      btn.disabled = false;
      var pane = getFocusedPane();
      if (pane) {
        pane.setBusy(true);
        pane.addUserMessage(text);
      }
      authFetch("/v1/api/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, ws_id: data.ws_id }),
      }).catch(function (err) {
        var p = getFocusedPane();
        if (p) {
          p.addErrorMessage("Connection error: " + err.message);
          p.setBusy(false);
        }
      });
    })
    .catch(function () {
      input.disabled = false;
      btn.disabled = false;
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
  globalEvtSource = new EventSource("/v1/api/events/global");
  globalEvtSource.onopen = function () {
    globalRetryDelay = 1000;
  };
  globalEvtSource.onmessage = function (e) {
    var data = JSON.parse(e.data);
    if (data.type === "ws_state") {
      updateTabIndicator(data.ws_id, data.state, {
        tokens: data.tokens,
        context_ratio: data.context_ratio,
        activity: data.activity,
        activity_state: data.activity_state,
      });
    } else if (data.type === "ws_activity") {
      var row = document.querySelector(
        '#dash-ws-table .dash-row[data-ws-id="' + data.ws_id + '"]',
      );
      if (row) {
        var sub = row.querySelector(".dash-row-sub");
        if (sub) {
          sub.textContent = data.activity || "";
          if (data.activity_state === "approval")
            sub.classList.add("sub-attention");
          else sub.classList.remove("sub-attention");
        }
      }
    } else if (data.type === "ws_rename") {
      if (workstreams[data.ws_id]) workstreams[data.ws_id].name = data.name;
      var nameEl = document.querySelector(
        '[data-ws-id="' + data.ws_id + '"] .tab-name',
      );
      if (nameEl) nameEl.textContent = data.name;
      // Update all panes showing this workstream
      for (var id in panes) {
        if (panes[id].wsId === data.ws_id) panes[id].updateWsName();
      }
    } else if (data.type === "ws_closed") {
      var wsId = data.ws_id;
      delete workstreams[wsId];
      renderTabBar();
      if (data.reason === "evicted") {
        showToast(
          "Evicted" + (data.name ? ": " + data.name : "") + " (capacity)",
        );
      }
      _reassignPanesForClosedWs(wsId);
      if (!Object.keys(workstreams).length) showDashboard();
    }
  };
  globalEvtSource.onerror = function () {
    globalEvtSource.close();
    globalEvtSource = null;
    fetch("/v1/api/workstreams")
      .then(function (r) {
        if (r.status === 401) {
          showLogin();
          return;
        }
        setTimeout(connectGlobalSSE, globalRetryDelay);
        globalRetryDelay = Math.min(globalRetryDelay * 2, 30000);
      })
      .catch(function () {
        setTimeout(connectGlobalSSE, globalRetryDelay);
        globalRetryDelay = Math.min(globalRetryDelay * 2, 30000);
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
  var div = document.createElement("div");
  div.className = "approval-tool";
  div.dataset.funcName = item.func_name || "";
  div.dataset.callId = item.call_id || "";

  var name = document.createElement("div");
  name.className = "tool-name";
  name.textContent = item.func_name || "";
  if (item.error) name.style.color = "var(--red)";
  div.appendChild(name);

  var cmd = document.createElement("div");
  cmd.className = "tool-cmd";
  var headerText = stripAnsi(item.header || "");
  var cleaned = headerText.replace(/^[^\s]+\s+\w+:\s*/, "");
  if (item.func_name === "bash" && cleaned) {
    cmd.innerHTML = '<span class="dollar">$ </span>' + escapeHtml(cleaned);
  } else {
    cmd.textContent = cleaned || headerText;
  }
  div.appendChild(cmd);

  if (item.preview) {
    var diff = document.createElement("div");
    diff.className = "tool-diff";
    var lines = stripAnsi(item.preview).split("\n");
    diff.innerHTML = lines
      .map(function (line) {
        var trimmed = line.trim();
        if (trimmed.startsWith("-"))
          return '<span class="diff-del">' + escapeHtml(line) + "</span>";
        if (trimmed.startsWith("+"))
          return '<span class="diff-add">' + escapeHtml(line) + "</span>";
        if (trimmed.startsWith("Warning:"))
          return '<span class="diff-warn">' + escapeHtml(line) + "</span>";
        return escapeHtml(line);
      })
      .join("\n");
    div.appendChild(diff);
  }

  return div;
}

function renderVerdictBadge(verdict, judgePending) {
  if (!verdict) return "";
  var risk = verdict.risk_level || "medium";
  var rec = verdict.recommendation || "review";
  var conf = Math.round((verdict.confidence || 0) * 100);
  var summary = verdict.intent_summary || "";
  var spinnerHtml = "";
  if (judgePending) {
    spinnerHtml =
      '<span class="verdict-judge-spinner">' +
      '<span class="judge-spinner-dot"></span> judge analyzing\u2026</span>';
  }
  var callId = escapeHtml(verdict.call_id || "");
  return (
    '<div class="verdict-badge verdict-' +
    escapeHtml(risk) +
    '" data-call-id="' +
    callId +
    '">' +
    '<span class="verdict-risk">' +
    escapeHtml(risk.toUpperCase()) +
    "</span>" +
    '<span class="verdict-rec">' +
    escapeHtml(rec) +
    "</span>" +
    '<span class="verdict-conf">' +
    conf +
    "%</span>" +
    spinnerHtml +
    '<button class="verdict-expand" onclick="toggleVerdictDetail(this)">details</button>' +
    "</div>" +
    '<div class="verdict-detail" style="display:none">' +
    '<div class="verdict-summary">' +
    escapeHtml(summary) +
    "</div>" +
    '<div class="verdict-reasoning">' +
    escapeHtml(verdict.reasoning || "") +
    "</div>" +
    ((verdict.evidence || []).length
      ? '<div class="verdict-evidence">' +
        (verdict.evidence || [])
          .map(function (e) {
            return "<div>\u2022 " + escapeHtml(e) + "</div>";
          })
          .join("") +
        "</div>"
      : "") +
    '<div class="verdict-tier">' +
    escapeHtml(verdict.tier || "heuristic") +
    " tier" +
    (verdict.judge_model ? " | " + escapeHtml(verdict.judge_model) : "") +
    "</div>" +
    "</div>"
  );
}

function toggleVerdictDetail(btn) {
  var badge = btn.closest(".verdict-badge");
  var detail = badge ? badge.nextElementSibling : null;
  if (detail && detail.classList.contains("verdict-detail")) {
    var isHidden = detail.style.display === "none";
    detail.style.display = isHidden ? "block" : "none";
    btn.textContent = isHidden ? "hide" : "details";
  }
}

function makeCollapsible(el) {
  el.classList.add("collapsed");
  el.setAttribute("tabindex", "0");
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", "Tool output (collapsed). Activate to expand.");
  var handler = function () {
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
//  13. Plan review dialog
// ===========================================================================

var _planContent = "";
var _planPaneId = null;

function showPlanDialog(content) {
  _planContent = content;
  _planPaneId = focusedPaneId;
  document.getElementById("plan-content").textContent = content;
  var feedbackEl = document.getElementById("plan-feedback");
  feedbackEl.value = "";
  _updatePlanRejectBtn();

  // Disable focused pane input
  var pane = panes[_planPaneId];
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
  var btn = document.getElementById("btn-plan-reject");
  var hasFeedback =
    document.getElementById("plan-feedback").value.trim().length > 0;
  btn.innerHTML = hasFeedback
    ? '<span class="key">Esc</span> Amend'
    : '<span class="key">Esc</span> Reject';
  btn.style.background = hasFeedback ? "var(--accent)" : "";
  btn.style.color = hasFeedback ? "var(--on-color)" : "";
  btn.onclick = function () {
    resolvePlan(hasFeedback ? "" : "reject");
  };
}

function resolvePlan(defaultFeedback) {
  var feedback = document.getElementById("plan-feedback").value.trim();
  if (!feedback && defaultFeedback) feedback = defaultFeedback;
  document.getElementById("plan-overlay").classList.remove("active");

  var pane = panes[_planPaneId];
  if (pane) {
    pane.inputEl.disabled = false;
    pane.sendBtn.disabled = false;
    pane.inputEl.focus();
  }

  // Critical: fire the API call first — this unblocks the server.
  var wsId = pane ? pane.wsId : currentWsId;
  authFetch("/v1/api/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback: feedback, ws_id: wsId }),
  }).catch(function (err) {
    if (pane) pane.addErrorMessage("Connection error: " + err.message);
  });

  // Render plan inline in the chat (best-effort)
  try {
    var isReject = feedback === "reject";
    var isAmend = feedback && !isReject;
    var action = isReject ? "rejected" : isAmend ? "amending" : "approved";
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

function _addInlinePlan(content, action, feedback) {
  if (!content) return;
  var pane = panes[_planPaneId];
  if (!pane) return;

  var wrapper = document.createElement("div");
  wrapper.className = "plan-inline";

  var header = document.createElement("div");
  header.className = "plan-inline-header";
  var label =
    action === "rejected"
      ? "Plan rejected"
      : action === "amending"
        ? "Plan \u2014 amending"
        : "Plan approved";
  header.innerHTML =
    '<span class="plan-inline-label plan-' + action + '">' + label + "</span>";
  wrapper.appendChild(header);

  var body = document.createElement("div");
  body.className = "plan-inline-body";
  try {
    body.innerHTML = renderMarkdown(content);
    postRenderMarkdown(body);
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
    var fb = document.createElement("div");
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

document
  .getElementById("dashboard-input")
  .addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      dashboardSendMessage();
    }
  });

document.addEventListener("keydown", function (e) {
  // Defer to modal's own keydown handler when new-ws modal is open
  var nwsOverlay = document.getElementById("new-ws-overlay");
  if (nwsOverlay && nwsOverlay.style.display !== "none") return;

  if (e.key === "Escape" && dashboardVisible) {
    e.preventDefault();
    hideDashboard();
    return;
  }

  // Get focused pane for approval / busy checks
  var pane = getFocusedPane();

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
    var idx = parseInt(e.key) - 1;
    var wsIds = Object.keys(workstreams);
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
  // Ctrl+W: close current workstream tab
  if (e.ctrlKey && !e.shiftKey && e.key === "w") {
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
    var paneIds = [];
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
      var ci = paneIds.indexOf(focusedPaneId);
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
    var fbInput =
      pane.approvalBlockEl &&
      pane.approvalBlockEl.querySelector(".approval-feedback-input");
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
      var details = pane.approvalBlockEl
        ? pane.approvalBlockEl.querySelectorAll(".verdict-detail")
        : [];
      details.forEach(function (d) {
        var isHidden = d.style.display === "none";
        d.style.display = isHidden ? "block" : "none";
        var btn2 = d.previousElementSibling
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
      var hasFb =
        document.getElementById("plan-feedback").value.trim().length > 0;
      resolvePlan(hasFb ? "" : "reject");
    } else if (e.key === "Tab") {
      var focusable = document.querySelectorAll(
        "#plan-dialog input, #plan-dialog button",
      );
      var first = focusable[0],
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
//  15. Init
// ===========================================================================

function initWorkstreams() {
  authFetch("/v1/api/workstreams")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      data.workstreams.forEach(function (ws) {
        workstreams[ws.id] = { name: ws.name, state: ws.state };
      });
      connectGlobalSSE();
      var wsIds = Object.keys(workstreams);
      if (!wsIds.length) {
        renderTabBar();
        showDashboard();
        return;
      }
      if (!Object.keys(panes).length) {
        if (!restoreLayout()) {
          var p = createPane(wsIds[0]);
          splitRoot = { type: "leaf", pane: p };
          setFocusedPane(p.id);
        }
        renderLayout();
      }
      renderTabBar();
      for (var id in panes) {
        if (!panes[id].evtSource) {
          panes[id].showEmptyState();
          panes[id].connectSSE(panes[id].wsId);
        }
      }
      var params = new URLSearchParams(location.search);
      var targetWs = params.get("ws_id");
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
initWorkstreams();

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
