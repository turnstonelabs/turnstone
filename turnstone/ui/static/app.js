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
  // Map<attachment_id, {filename, size_bytes, mime_type, kind}>
  this.pendingAttachments = new Map();
  this.attachChipsEl = null;
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

  // Input area — DOM + behavior comes from shared/composer.js.  The
  // pane keeps the attachment-upload pipeline (because attachments are
  // pane-specific state) and routes file events through the composer's
  // attach/paste/drop callbacks.
  this.composer = new Composer(this.el, {
    attachments: {
      onAttach: function (file) {
        self.uploadAttachment(file);
      },
    },
    stopBtn: true,
    queueWhileBusy: true,
    busyPlaceholder: "Queue a message\u2026 (!!! for urgent)",
    onSend: function () {
      self.sendMessage();
    },
    onStop: function () {
      self.cancelGeneration();
    },
    dragDrop: { targetEl: this.el, dropClass: "pane-drop-target" },
  });
  this.attachChipsEl = this.composer.chipsEl;
  this.inputEl = this.composer.inputEl;
  this.sendBtn = this.composer.sendBtn;
  this.stopBtn = this.composer.stopBtn;
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
  this.clearAttachmentChips();
};

// ---------------------------------------------------------------------------
// Attachment handling
// ---------------------------------------------------------------------------

Pane.prototype.clearAttachmentChips = function () {
  if (this.pendingAttachments) this.pendingAttachments.clear();
  if (this.attachChipsEl) this.attachChipsEl.textContent = "";
};

function _formatAttachSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

Pane.prototype._renderAttachmentChip = function (info) {
  var self = this;
  var chip = document.createElement("span");
  chip.className =
    "ts-composer-chip ts-composer-chip-" + (info.kind || "other");
  chip.setAttribute("role", "listitem");
  chip.dataset.attachmentId = info.attachment_id;

  var icon = document.createElement("span");
  icon.className = "ts-composer-chip-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = info.kind === "image" ? "\ud83d\uddbc" : "\ud83d\udcc4";
  chip.appendChild(icon);

  var label = document.createElement("span");
  label.className = "ts-composer-chip-name";
  label.textContent = info.filename || "(unnamed)";
  label.title = info.filename || "";
  chip.appendChild(label);

  var size = document.createElement("span");
  size.className = "ts-composer-chip-size";
  size.textContent = _formatAttachSize(info.size_bytes || 0);
  chip.appendChild(size);

  var remove = document.createElement("button");
  remove.type = "button";
  remove.className = "ts-composer-chip-remove";
  remove.setAttribute(
    "aria-label",
    "Remove attachment " + (info.filename || ""),
  );
  remove.title = "Remove";
  remove.textContent = "\u00d7";
  remove.onclick = function () {
    self.removeAttachment(info.attachment_id);
  };
  chip.appendChild(remove);

  this.attachChipsEl.appendChild(chip);
};

Pane.prototype.uploadAttachment = function (file) {
  if (!this.wsId || !file) return;
  var self = this;
  var wsId = this.wsId;
  var fd = new FormData();
  fd.append("file", file, file.name);

  // Placeholder chip with upload-in-flight state
  var placeholderId = "__uploading_" + Date.now() + "_" + Math.random();
  this.pendingAttachments.set(placeholderId, {
    attachment_id: placeholderId,
    filename: file.name,
    size_bytes: file.size,
    mime_type: file.type || "",
    kind: (file.type || "").indexOf("image/") === 0 ? "image" : "text",
    uploading: true,
  });
  this._renderAttachmentChip(this.pendingAttachments.get(placeholderId));

  authFetch(
    "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/attachments",
    { method: "POST", body: fd },
  )
    .then(function (r) {
      return r.json().then(function (body) {
        return { ok: r.ok, status: r.status, body: body };
      });
    })
    .then(function (res) {
      if (!res.ok) {
        // Drop the placeholder; nothing to swap in.
        self._removeAttachmentChip(placeholderId);
        showToast((res.body && res.body.error) || "Upload failed");
        return;
      }
      // Swap placeholder → real id in place so chip and pending-Map
      // ordering reflect user selection, not upload-completion order.
      self._swapPlaceholderChip(placeholderId, res.body);
    })
    .catch(function (e) {
      // Always clean up the placeholder (including auth failures) so
      // an "uploading…" chip can't get stuck across re-auth.
      self._removeAttachmentChip(placeholderId);
      if ((e && e.message) !== "auth") {
        showToast("Upload failed");
      }
    });
};

Pane.prototype._removeAttachmentChip = function (id) {
  var chip = this.attachChipsEl.querySelector(
    '[data-attachment-id="' + id + '"]',
  );
  if (chip) chip.remove();
  this.pendingAttachments.delete(id);
};

Pane.prototype._swapPlaceholderChip = function (placeholderId, info) {
  // Rebuild pendingAttachments preserving insertion order, swapping
  // the placeholder key for the real attachment_id.  JS Map iteration
  // is insertion-ordered, so naïve delete+set would move the entry to
  // the end and reorder send().
  var rebuilt = new Map();
  this.pendingAttachments.forEach(function (val, key) {
    if (key === placeholderId) {
      rebuilt.set(info.attachment_id, info);
    } else {
      rebuilt.set(key, val);
    }
  });
  this.pendingAttachments = rebuilt;

  // Update the existing chip DOM in place so visual order matches.
  var chip = this.attachChipsEl.querySelector(
    '[data-attachment-id="' + placeholderId + '"]',
  );
  if (chip) {
    chip.dataset.attachmentId = info.attachment_id;
    var name = chip.querySelector(".ts-composer-chip-name");
    if (name) {
      name.textContent = info.filename || "(unnamed)";
      name.title = info.filename || "";
    }
    var size = chip.querySelector(".ts-composer-chip-size");
    if (size) size.textContent = _formatAttachSize(info.size_bytes || 0);
  } else {
    // Chip missing (user removed it mid-upload?); render fresh.
    this._renderAttachmentChip(info);
  }
};

Pane.prototype.removeAttachment = function (attachmentId) {
  var wsId = this.wsId;
  var info = this.pendingAttachments.get(attachmentId);
  if (!info) return;
  var chip = this.attachChipsEl.querySelector(
    '[data-attachment-id="' + attachmentId + '"]',
  );

  // Optimistic remove
  if (chip) chip.remove();
  this.pendingAttachments.delete(attachmentId);

  // In-flight placeholders have no server-side row yet
  if (info.uploading) return;

  authFetch(
    "/v1/api/workstreams/" +
      encodeURIComponent(wsId) +
      "/attachments/" +
      encodeURIComponent(attachmentId),
    { method: "DELETE" },
  ).catch(function (e) {
    if ((e && e.message) !== "auth") {
      showToast("Failed to remove attachment");
    }
  });
};

Pane.prototype.rehydrateAttachments = function () {
  if (!this.wsId) return;
  var self = this;
  var wsId = this.wsId;
  authFetch(
    "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/attachments",
    { method: "GET" },
  )
    .then(function (r) {
      if (!r.ok) return null;
      return r.json();
    })
    .then(function (body) {
      // Tab may have switched between fire and response
      if (!body || self.wsId !== wsId) return;
      self.clearAttachmentChips();
      (body.attachments || []).forEach(function (a) {
        self.pendingAttachments.set(a.attachment_id, a);
        self._renderAttachmentChip(a);
      });
    })
    .catch(function () {});
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
  // Composer owns send/stop button display, label rotation, and the
  // stop button's "■ Stop" / aria-label / dataset reset on every
  // transition (so cancelGeneration's transient "Cancelling…" label
  // doesn't persist into the next busy cycle).
  this.composer.setBusy(b);
  if (!b) this._promoteQueuedMessages();
};

Pane.prototype._promoteQueuedMessages = function () {
  var queuedMsgs = this.messagesEl.querySelectorAll(".msg-queued");
  for (var i = 0; i < queuedMsgs.length; i++) {
    var el = queuedMsgs[i];
    el.classList.remove("msg-queued", "msg-queued-important");
    delete el.dataset.msgId;
    el.removeAttribute("role");
    el.removeAttribute("aria-label");
    var badge = el.querySelector(".queued-badge");
    if (badge) badge.remove();
    var dismiss = el.querySelector(".queued-dismiss");
    if (dismiss) dismiss.remove();
  }
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
  var wsChanged = this.wsId !== wsId;
  this.wsId = wsId;
  if (wsChanged) {
    this.clearAttachmentChips();
    this.rehydrateAttachments();
  }

  this.evtSource = new EventSource(
    "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/events",
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
              workstreams[ws.ws_id] = { name: ws.name, state: ws.state };
            });
            renderTabBar();
            // Reconnect all disconnected panes, reassigning stale ws_ids.
            // Two passes: (1) reassign stale panes, (2) reconnect all.
            // Track assigned ws_ids to avoid multiple panes on the same ws.
            var remaining = Object.keys(workstreams);
            if (!remaining.length) {
              showDashboard();
              return;
            }
            var usedWsIds = {};
            for (var pid in panes) {
              if (panes[pid].wsId && workstreams[panes[pid].wsId])
                usedWsIds[panes[pid].wsId] = true;
            }
            for (var pid2 in panes) {
              var p2 = panes[pid2];
              if (p2.wsId && !workstreams[p2.wsId]) {
                var newWsId = null;
                for (var ri = 0; ri < remaining.length; ri++) {
                  if (!usedWsIds[remaining[ri]]) {
                    newWsId = remaining[ri];
                    break;
                  }
                }
                if (newWsId) {
                  p2.disconnectSSE();
                  p2.wsId = newWsId;
                  usedWsIds[newWsId] = true;
                  while (p2.messagesEl.firstChild)
                    p2.messagesEl.removeChild(p2.messagesEl.firstChild);
                  p2.showEmptyState();
                  p2.updateWsName();
                }
                // else: more panes than workstreams — leave pane stale,
                // connectSSE below will pick it up or it stays disconnected.
              }
            }
            // Pass 2: reconnect all panes and sync focused pane
            for (var pid3 in panes) {
              var p3 = panes[pid3];
              if (pid3 === focusedPaneId) currentWsId = p3.wsId;
              if (!p3.evtSource && p3.wsId && workstreams[p3.wsId]) {
                setTimeout(
                  (function (pp) {
                    return function () {
                      pp.connectSSE(pp.wsId);
                    };
                  })(p3),
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
  // Guard: drop events that belong to a different workstream.
  // This prevents cross-contamination during tab switches and reconnects.
  if (evt.ws_id && evt.ws_id !== this.wsId) return;
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
        this.currentReasoningEl.className =
          "ts-msg ts-msg--reasoning msg reasoning";
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
        this.currentAssistantEl.className =
          "ts-msg ts-msg--assistant msg assistant";
        // streamingRender targets a .ts-msg-body inner wrapper so the
        // shared markdown rules in chat.css (h1/h2/h3/code/blockquote
        // scoped to .ts-msg-body *) actually match on this page — the
        // coordinator page uses the same .ts-msg-body wrapper so the
        // two chat views stay visually aligned.
        this.currentAssistantBodyEl = document.createElement("div");
        this.currentAssistantBodyEl.className = "ts-msg-body msg-body";
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
          document.getElementById("ui-header").appendChild(warn);
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
        authFetch(
          "/v1/api/workstreams/" + encodeURIComponent(self.wsId) + "/send",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: editText }),
          },
        ).catch(function (err) {
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

Pane.prototype.addUserMessage = function (text, attachments) {
  this.removeEmptyState();
  var el = document.createElement("div");
  el.className = "ts-msg ts-msg--user msg user";
  var textEl = document.createElement("div");
  textEl.className = "msg-user-text";
  textEl.textContent = text;
  el.appendChild(textEl);
  if (Array.isArray(attachments) && attachments.length > 0) {
    var pills = document.createElement("div");
    pills.className = "msg-user-attach";
    attachments.forEach(function (a) {
      var pill = document.createElement("span");
      pill.className =
        "msg-user-attach-pill msg-user-attach-pill-" + (a.kind || "other");
      var icon = document.createElement("span");
      icon.className = "msg-user-attach-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = a.kind === "image" ? "\ud83d\uddbc" : "\ud83d\udcc4";
      pill.appendChild(icon);
      var nameEl = document.createElement("span");
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
};

Pane.prototype.addQueuedMessage = function (text, priority) {
  this.removeEmptyState();
  var self = this;
  var el = document.createElement("div");
  el.className = "ts-msg ts-msg--user msg user msg-queued";
  el.setAttribute("role", "status");
  if (priority === "important") {
    el.classList.add("msg-queued-important");
    el.setAttribute("aria-label", "Important message queued: " + text);
  } else {
    el.setAttribute("aria-label", "Message queued: " + text);
  }
  var badge = document.createElement("span");
  badge.className = "queued-badge";
  badge.setAttribute("aria-hidden", "true");
  badge.textContent = priority === "important" ? "queued (!!!) " : "queued ";
  el.appendChild(badge);
  el.appendChild(document.createTextNode(text));
  // Dismiss button — remove from queue before injection
  var dismiss = document.createElement("button");
  dismiss.className = "queued-dismiss";
  dismiss.title = "Remove from queue";
  dismiss.setAttribute("aria-label", "Remove queued message");
  dismiss.textContent = "\u00d7";
  dismiss.addEventListener("click", function (e) {
    e.stopPropagation();
    self._dequeueMessage(el);
  });
  el.appendChild(dismiss);
  this.messagesEl.appendChild(el);
  this.scrollToBottom(true);
  return el;
};

Pane.prototype._dequeueMessage = function (el) {
  var self = this;
  var msgId = el.dataset.msgId;
  if (!msgId) {
    // ID not yet set — mark for deferred DELETE when send response arrives
    el.dataset.pendingDismiss = "true";
    el.remove();
    return;
  }
  authFetch("/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/send", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ msg_id: msgId }),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.status === "removed") {
        el.remove();
      }
      // Either path warrants a chip-strip refresh: a successful remove
      // unreserves the attachments server-side, and a "not_found" means
      // dispatch raced us so the actual pending state may differ from
      // what the UI last saw.  Re-fetch to stay in sync.  (Leave the
      // message bubble visible on not_found — the promote loop strips
      // the queued styling on idle.)
      self.rehydrateAttachments();
    })
    .catch(function () {
      // Network error — don't remove, message may have been injected
    });
};

Pane.prototype._addUserMsgActions = function (el, text) {
  var self = this;
  var bar = document.createElement("div");
  bar.className = "msg-actions ts-msg-actions";
  bar.setAttribute("role", "toolbar");
  bar.setAttribute("aria-label", "Message actions");
  // Edit button
  var editBtn = document.createElement("button");
  editBtn.className = "msg-action-btn ts-msg-action-btn";
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
  rewindBtn.className = "msg-action-btn ts-msg-action-btn";
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
    bar.className = "msg-actions ts-msg-actions";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Message actions");
    el.appendChild(bar);
  }
  var btn = document.createElement("button");
  btn.className = "msg-action-btn ts-msg-action-btn";
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
  var userMsgs = this.messagesEl.querySelectorAll(".ts-msg--user");
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
  var userMsgs = this.messagesEl.querySelectorAll(".ts-msg--user");
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
      this.addUserMessage(msg.content || "", msg.attachments || null);
      lastToolBlock = null;
    } else if (msg.role === "assistant") {
      if (msg.tool_calls && msg.tool_calls.length) {
        if (msg.pending) {
          lastToolBlock = null;
        } else {
          var wasDenied = !!msg.denied;
          var block = document.createElement("div");
          block.className =
            "ts-msg ts-approval ts-approval--inline " +
            (wasDenied ? "denied" : "approved");
          msg.tool_calls.forEach(function (tc) {
            var div = document.createElement("div");
            div.className = "ts-approval-tool";
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
              if (tc.name === "bash") {
                var preview = Object.values(args)[0] || "";
                cmd.innerHTML =
                  '<span class="dollar">$ </span>' +
                  escapeHtml(String(preview));
              } else {
                var parts = [];
                var keys = Object.keys(args);
                for (var k = 0; k < keys.length; k++) {
                  var val = args[keys[k]];
                  var valStr =
                    val === null || val === undefined ? "null" : String(val);
                  if (valStr.length > 80)
                    valStr = valStr.substring(0, 77) + "...";
                  parts.push(keys[k] + ": " + valStr);
                }
                cmd.textContent = parts.join("\n");
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
            badge.className = "ts-approval-badge ts-approval-badge--denied";
            badge.textContent = "\u2717 denied";
          } else {
            badge.className = "ts-approval-badge ts-approval-badge--approved";
            badge.textContent = "\u2713 approved";
          }
          block.appendChild(badge);
          self.messagesEl.appendChild(block);
          lastToolBlock = block;
        }
      }
      if (msg.content) {
        var el = document.createElement("div");
        el.className = "ts-msg ts-msg--assistant msg assistant";
        var bodyEl = document.createElement("div");
        bodyEl.className = "ts-msg-body msg-body";
        var rendered = renderMarkdown(msg.content);
        bodyEl.innerHTML = rendered;
        el.appendChild(bodyEl);
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
          var media = !isToolError ? tryParseMedia(stripped) : null;
          if (media) {
            var embed = buildMediaEmbed(media, stripped);
            var bdg = lastToolBlock.querySelector(".ts-approval-badge");
            if (bdg) lastToolBlock.insertBefore(embed, bdg);
            else lastToolBlock.appendChild(embed);
          } else {
            var out = renderToolOutput(stripped, isToolError);
            if (out.textContent.split("\n").length > 10) {
              makeCollapsible(out);
            }
            var bdg = lastToolBlock.querySelector(".ts-approval-badge");
            if (bdg) lastToolBlock.insertBefore(out, bdg);
            else lastToolBlock.appendChild(out);
          }
        }
        if (isToolError && !lastToolBlock.classList.contains("denied")) {
          lastToolBlock.classList.add("error");
          appendToolErrorBadge(lastToolBlock);
        }
      }
    }
  }
  this._attachRetryToLastAssistant();
  this.scrollToBottom();
};

Pane.prototype._attachRetryToLastAssistant = function () {
  // Remove any previous retry buttons
  var old = this.messagesEl.querySelectorAll(".ts-msg--assistant .msg-actions");
  for (var i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
  // Find the last assistant message with content and add retry.
  // Reasoning blocks emit as .ts-msg--reasoning (distinct modifier)
  // so the .ts-msg--assistant selector already excludes them — no
  // extra guard needed.
  var assistants = this.messagesEl.querySelectorAll(".ts-msg--assistant");
  if (assistants.length) {
    this._addRetryAction(assistants[assistants.length - 1]);
  }
};

Pane.prototype.showInlineToolBlock = function (
  items,
  autoApproved,
  judgePending,
) {
  var self = this;
  var block = document.createElement("div");
  block.className =
    "ts-msg ts-approval ts-approval--inline" +
    (autoApproved ? " approved" : "");
  if (!autoApproved) {
    block.setAttribute("role", "alertdialog");
    block.setAttribute("aria-label", "Tool approval required");
  }

  // Track the highest-priority recommendation for glow
  var glowRec = null;

  items.forEach(function (item) {
    block.appendChild(buildToolDiv(item));
    // Render verdict badge if present.  Server emits the heuristic
    // verdict under ``heuristic_verdict`` (matches the api/server_schemas
    // PendingApprovalItem shape).  Falls back to the legacy ``verdict``
    // key in case a stale SSE payload arrives mid-deploy.
    var heuristic = item.heuristic_verdict || item.verdict;
    if (heuristic) {
      block.insertAdjacentHTML(
        "beforeend",
        renderVerdictBadge(heuristic, judgePending),
      );
      var rec = heuristic.recommendation || "review";
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
    badge.className = "ts-approval-badge ts-approval-badge--approved";
    badge.textContent = "\u2713 auto-approved";
    block.appendChild(badge);
  } else {
    var prompt = document.createElement("div");
    prompt.className = "ts-approval-body";

    // Apply verdict glow on initial heuristic verdict
    if (glowRec) {
      if (glowRec === "approve")
        prompt.classList.add("ts-verdict-glow--approve");
      else if (glowRec === "deny")
        prompt.classList.add("ts-verdict-glow--deny");
      else prompt.classList.add("ts-verdict-glow--review");
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
    actionsDiv.className = "ts-approval-actions";

    var approveBtn = document.createElement("button");
    approveBtn.className = "ts-approval-btn ts-approval-btn--approve";
    approveBtn.innerHTML = '<span class="key">y</span> Approve';
    approveBtn.onclick = function () {
      self.resolveApproval(true, false, self.getFeedback());
    };
    actionsDiv.appendChild(approveBtn);

    var denyBtn = document.createElement("button");
    denyBtn.className = "ts-approval-btn ts-approval-btn--deny";
    denyBtn.innerHTML = '<span class="key">n</span> Deny';
    denyBtn.onclick = function () {
      self.resolveApproval(false, false, self.getFeedback());
    };
    actionsDiv.appendChild(denyBtn);

    if (alwaysNames.length) {
      var alwaysBtn = document.createElement("button");
      alwaysBtn.className = "ts-approval-btn ts-approval-btn--always";
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
    fbInput.className = "ts-approval-feedback";
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
  var prompt = this.approvalBlockEl.querySelector(".ts-approval-body");
  if (prompt) prompt.remove();

  // Add badge
  var badge = document.createElement("div");
  badge.setAttribute("role", "status");
  if (approved) {
    badge.className = "ts-approval-badge ts-approval-badge--approved";
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
    var self = this;
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
    ).catch(function (err) {
      self.addErrorMessage("Connection error: " + err.message);
    });
  }

  this.scrollToBottom();
};

Pane.prototype.getFeedback = function () {
  if (!this.approvalBlockEl) return null;
  var inp = this.approvalBlockEl.querySelector(".ts-approval-feedback");
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
          '.ts-approval-tool[data-call-id="' + escapedId + '"]',
        )
      : null;
    if (!target) {
      var blocks = this.messagesEl.querySelectorAll(".ts-approval");
      if (!blocks.length) return;
      var block = blocks[blocks.length - 1];
      var tools = block.querySelectorAll(
        '.ts-approval-tool[data-func-name="bash"]',
      );
      target = tools.length ? tools[tools.length - 1] : null;
      if (!target) {
        var allTools = block.querySelectorAll(".ts-approval-tool");
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
        '.ts-approval-tool[data-call-id="' + escapedId + '"]',
      )
    : null;
  if (!target) {
    var blocks = this.messagesEl.querySelectorAll(".ts-approval");
    if (!blocks.length) return;
    var block = blocks[blocks.length - 1];
    var tools = block.querySelectorAll(".ts-approval-tool");
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

  // Detect structured media output and render interactive embed
  if (!isError) {
    var media = tryParseMedia(stripped);
    if (media) {
      var embed = buildMediaEmbed(media, stripped);
      target.after(embed);
      this.scrollToBottom();
      return;
    }
  }

  var out = renderToolOutput(stripped, isError);

  // Mark the parent approval block as errored
  if (isError) {
    var parentBlock = target.closest(".ts-approval");
    if (parentBlock && !parentBlock.classList.contains("denied")) {
      parentBlock.classList.add("error");
      appendToolErrorBadge(parentBlock);
    }
  }

  if (out.textContent.split("\n").length > 10) {
    makeCollapsible(out);
  }

  target.after(out);
  this.scrollToBottom();
};

Pane.prototype.showOutputWarning = function (evt) {
  if (!evt.call_id || evt.risk_level === "none") return;
  var escapedId = CSS.escape(evt.call_id);
  var toolDiv = this.messagesEl.querySelector(
    '.ts-approval-tool[data-call-id="' + escapedId + '"]',
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
  if (!badge) {
    // Badge no longer in DOM (tool block replaced by output) — show
    // a toast so the user still sees the late-arriving verdict.
    var conf = Math.round((verdict.confidence || 0) * 100);
    var rec = verdict.recommendation || "review";
    var func = verdict.func_name || "";
    showToast(
      "Judge verdict for " + func + ": " + rec + " (" + conf + "%)",
      rec === "approve" ? "success" : rec === "deny" ? "error" : "warning",
    );
    return;
  }

  var risk = verdict.risk_level || "medium";
  badge.className = "verdict-badge verdict-" + risk + " ts-verdict-badge";
  badge.setAttribute("data-risk", risk);

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
  var prompt = this.approvalBlockEl.querySelector(".ts-approval-body");
  if (!prompt) return;

  // Collect all verdict badges currently visible in this approval block
  var badges = this.approvalBlockEl.querySelectorAll(".verdict-badge");
  var worst = recommendation;
  for (var i = 0; i < badges.length; i++) {
    var recEl = badges[i].querySelector(".verdict-rec");
    if (recEl) {
      var r = recEl.textContent;
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
};

Pane.prototype.addInfoMessage = function (text) {
  var el = document.createElement("div");
  el.className = "ts-msg ts-msg--info msg info";
  el.textContent = stripAnsi(text);
  this.messagesEl.appendChild(el);
  this.scrollToBottom();
};

Pane.prototype.addErrorMessage = function (text) {
  var el = document.createElement("div");
  el.className = "ts-msg ts-msg--error msg error";
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
  if (!text) return;

  if (text.startsWith("/")) {
    if (this.busy) return; // commands not allowed while busy
    authFetch("/v1/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: text, ws_id: this.wsId }),
    });
    this.addUserMessage(text);
    this.composer.clear();
    return;
  }

  var self = this;
  var isBusy = this.busy;
  var queuedEl = null;

  // Snapshot attachments for this turn (stable-ids only — skip in-flight
  // placeholders, which may not have server-assigned ids yet).
  var attachmentList = [];
  var attachmentIds = [];
  this.pendingAttachments.forEach(function (info, id) {
    if (info && !info.uploading) {
      attachmentList.push(info);
      attachmentIds.push(id);
    }
  });

  if (isBusy) {
    // Queue message for injection at the next tool-result seam.
    // Strip !!! prefix for display, show priority badge instead.
    var displayText = text;
    var priority = "notice";
    if (text.startsWith("!!!")) {
      displayText = text.slice(3).trimStart();
      priority = "important";
    }
    queuedEl = this.addQueuedMessage(displayText, priority);
  } else {
    this.setBusy(true);
    this.addUserMessage(text, attachmentList);
  }
  this.composer.clear();

  authFetch("/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: text,
      attachment_ids: attachmentIds,
    }),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      // Clear only the chips that were actually reserved/attached on
      // the server; leftover (dropped) ones stay in the composer so
      // the user can see what's still pending.
      var _consumeChips = function () {
        var attached = Array.isArray(data.attached_ids)
          ? data.attached_ids
          : null;
        if (attached) {
          attached.forEach(function (id) {
            var chip = self.attachChipsEl.querySelector(
              '[data-attachment-id="' + id + '"]',
            );
            if (chip) chip.remove();
            self.pendingAttachments.delete(id);
          });
          if (
            Array.isArray(data.dropped_attachment_ids) &&
            data.dropped_attachment_ids.length
          ) {
            showToast(
              "Some attachments couldn't be included (" +
                data.dropped_attachment_ids.length +
                ") — they're still in your composer.",
            );
          }
        } else {
          self.clearAttachmentChips();
        }
      };

      if (data.status === "queued" && data.msg_id && queuedEl) {
        if (queuedEl.dataset.pendingDismiss) {
          // User dismissed before ID arrived — send deferred DELETE
          authFetch(
            "/v1/api/workstreams/" + encodeURIComponent(self.wsId) + "/send",
            {
              method: "DELETE",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ msg_id: data.msg_id }),
            },
          );
        } else {
          queuedEl.dataset.msgId = data.msg_id;
        }
        _consumeChips();
      } else if (data.status === "busy") {
        if (queuedEl) queuedEl.remove();
        self.addErrorMessage("Server is busy. Please wait.");
        if (!isBusy) self.setBusy(false);
      } else if (data.status === "queue_full") {
        if (queuedEl) queuedEl.remove();
        self.addErrorMessage("Message queue full. Please wait.");
      } else {
        _consumeChips();
      }
    })
    .catch(function (err) {
      if (queuedEl) queuedEl.remove();
      self.addErrorMessage("Connection error: " + err.message);
      if (!isBusy) self.setBusy(false);
    });
};

Pane.prototype.cancelGeneration = function () {
  if (!this.busy || !this.wsId || this.stopBtn.disabled) return;
  var self = this;
  var isForce = this.stopBtn.dataset.forceCancel === "true";
  this.stopBtn.disabled = true;
  authFetch(
    "/v1/api/workstreams/" + encodeURIComponent(this.wsId) + "/cancel",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: isForce }),
    },
  )
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

var _tabDropdown = null;
var _tabDropdownCloseHandler = null;
var _tabDropdownTrigger = null;

function showPaneContextMenu(x, y, paneId) {
  closeTabDropdown();
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

// ---------------------------------------------------------------------------
//  3c. Tab dropdown menu (per-tab workstream actions)
// ---------------------------------------------------------------------------

function showTabDropdown(chevronEl, wsId) {
  closePaneContextMenu();
  closeTabDropdown();
  _tabDropdownTrigger = chevronEl;
  chevronEl.setAttribute("aria-expanded", "true");

  var menu = document.createElement("div");
  menu.className = "ws-tab-dropdown";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Workstream actions");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  var isLastWs = Object.keys(workstreams).length <= 1;
  var items = [
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
      var sep = document.createElement("div");
      sep.className = "ws-tab-dropdown-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    var btn = document.createElement("button");
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
    var labelSpan = document.createElement("span");
    labelSpan.className = "ws-tab-dropdown-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    if (item.key) {
      var keySpan = document.createElement("span");
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
  var cr = chevronEl.getBoundingClientRect();
  var mr = menu.getBoundingClientRect();
  var mx = cr.right - mr.width;
  var my = cr.bottom + 2;
  if (mx < 0) mx = 4;
  if (my + mr.height > window.innerHeight) my = cr.top - mr.height - 2;
  if (mx + mr.width > window.innerWidth) mx = window.innerWidth - mr.width - 4;
  menu.style.left = mx + "px";
  menu.style.top = my + "px";
  _tabDropdown = menu;

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
        var btns = Array.from(menu.querySelectorAll(".ws-tab-dropdown-item"));
        if (!btns.length) return;
        var idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        else if (e.key === "ArrowUp")
          btns[(idx - 1 + btns.length) % btns.length].focus();
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
  var closeHandler = _tabDropdownCloseHandler;
  var activeMenu = menu;
  setTimeout(function () {
    if (_tabDropdown !== activeMenu || !closeHandler) return;
    document.addEventListener("mousedown", closeHandler);
    document.addEventListener("keydown", closeHandler);
    var first = activeMenu.querySelector(".ws-tab-dropdown-item");
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
  // Persist theme to server settings so it propagates to other clients
  var themeValue = next === "light" ? "light" : "dark";
  authFetch("/v1/api/admin/settings/interface.theme", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: themeValue }),
  }).catch(function () {});
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
  closeTabDropdown();
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
      if (e.target.classList.contains("tab-chevron")) return;
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

    var wsidBadge = document.createElement("span");
    wsidBadge.className = "tab-wsid";
    wsidBadge.textContent = wsId.substring(0, 7);
    tab.appendChild(wsidBadge);

    var chevron = document.createElement("button");
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
  closeTabDropdown();
  var pane = getFocusedPane();
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
    for (var pid in panes) {
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
  pane.connectSSE(wsId);

  if (!_historyNavigation) {
    history.pushState({ turnstone: "workstream", wsId: wsId }, "");
  }
}

// ===========================================================================
//  9. New workstream modal
// ===========================================================================

var _newWsTrapHandler = null;
var _forkFromWsId = "";

// Staged files for the new-workstream modal.  Distinct from the pane's
// chip strip: there's no ws_id yet, so we hold File objects in memory
// and ship them all in one multipart create request on submit.
var _newWsStagedFiles = [];

// Per-kind size caps (mirrored from turnstone/core/attachments.py so the
// browser can fail fast before uploading).  Keep in sync.
var _NEW_WS_IMAGE_CAP = 4 * 1024 * 1024;
var _NEW_WS_TEXT_CAP = 512 * 1024;
var _NEW_WS_MAX_FILES = 10;

function _newWsRenderChips() {
  var chipsEl = document.getElementById("new-ws-attach-chips");
  if (!chipsEl) return;
  chipsEl.textContent = "";
  for (var i = 0; i < _newWsStagedFiles.length; i++) {
    (function (idx) {
      var f = _newWsStagedFiles[idx];
      var chip = document.createElement("span");
      chip.className = "new-ws-attach-chip";
      chip.setAttribute("role", "listitem");
      var label = document.createElement("span");
      label.className = "new-ws-attach-chip-name";
      label.textContent = f.name;
      label.title = f.name + " (" + f.size + " bytes)";
      chip.appendChild(label);
      var size = document.createElement("span");
      size.className = "new-ws-attach-chip-size";
      size.textContent = _formatAttachSize(f.size);
      chip.appendChild(size);
      var rm = document.createElement("button");
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
var _ATTACH_IMAGE_MIMES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
];
var _ATTACH_TEXT_APP_MIMES = [
  "application/json",
  "application/xml",
  "application/x-yaml",
  "application/yaml",
  "application/toml",
];
var _ATTACH_TEXT_EXTENSIONS = [
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
  var mime = (file.type || "").toLowerCase();
  if (_ATTACH_IMAGE_MIMES.indexOf(mime) !== -1) return true;
  if (mime.indexOf("text/") === 0) return true;
  if (_ATTACH_TEXT_APP_MIMES.indexOf(mime) !== -1) return true;
  var name = (file.name || "").toLowerCase();
  var dot = name.lastIndexOf(".");
  if (dot >= 0 && _ATTACH_TEXT_EXTENSIONS.indexOf(name.substr(dot)) !== -1) {
    return true;
  }
  return false;
}

function _newWsAddFiles(files) {
  var errEl = document.getElementById("new-ws-error");
  for (var i = 0; i < files.length; i++) {
    var f = files[i];
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
    var isImage = (f.type || "").indexOf("image/") === 0;
    var cap = isImage ? _NEW_WS_IMAGE_CAP : _NEW_WS_TEXT_CAP;
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
  var overlay = document.getElementById("new-ws-overlay");
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";

  // Update title and button text based on mode
  var titleEl = document.getElementById("new-ws-title");
  var submitBtn = document.getElementById("new-ws-submit");
  if (_forkFromWsId) {
    titleEl.textContent = "Fork Workstream";
    submitBtn.textContent = "Fork";
  } else {
    titleEl.textContent = "New Workstream";
    submitBtn.textContent = "Create";
  }

  // Hide skill dropdown when forking (not relevant — fork copies history)
  var skillLabel = document.querySelector('label[for="new-ws-skill"]');
  var skillSelect = document.getElementById("new-ws-skill");
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
  var modelSelect = document.getElementById("new-ws-model");
  var judgeSelect = document.getElementById("new-ws-judge-model");
  var fp = getFocusedPane();
  var curModel = fp ? fp.modelAlias || fp.model || "" : "";
  modelSelect.textContent = "";
  judgeSelect.textContent = "";
  var defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = curModel
    ? "Default (" + curModel + ")"
    : "Default model";
  modelSelect.appendChild(defaultOpt);
  var defJudgeOpt = document.createElement("option");
  defJudgeOpt.value = "";
  defJudgeOpt.textContent = "Default (agent model)";
  judgeSelect.appendChild(defJudgeOpt);
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

        var judgeOpt = document.createElement("option");
        judgeOpt.value = m.alias;
        judgeOpt.textContent = opt.textContent;
        judgeSelect.appendChild(judgeOpt);
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
  var initEl = document.getElementById("new-ws-initial-message");
  if (initEl) initEl.value = "";
  var errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";
  errEl.textContent = "";
  var submitBtn = document.getElementById("new-ws-submit");
  submitBtn.disabled = false;
  submitBtn.textContent = _forkFromWsId ? "Fork" : "Create";

  // Reset attachment staging.  Forks don't carry attachments —
  // disable the attach UI in that case (the fork inherits its
  // parent's history; new attachments go on the next manual send).
  _newWsStagedFiles = [];
  var attachRow = document.getElementById("new-ws-attach-row");
  var attachInput = document.getElementById("new-ws-attach-input");
  var attachBtn = document.getElementById("new-ws-attach-btn");
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
  var submitBtn = document.getElementById("new-ws-submit");
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  submitBtn.textContent = _forkFromWsId ? "Forking\u2026" : "Creating\u2026";

  var body = {};
  var name = document.getElementById("new-ws-name").value.trim();
  var model = document.getElementById("new-ws-model").value.trim();
  var judge_model = document.getElementById("new-ws-judge-model").value.trim();
  var skill = document.getElementById("new-ws-skill").value;
  var initEl = document.getElementById("new-ws-initial-message");
  var initial_message = initEl ? initEl.value.trim() : "";
  if (name) body.name = name;
  if (model) body.model = model;
  if (judge_model) body.judge_model = judge_model;
  if (skill && !_forkFromWsId) body.skill = skill;
  if (_forkFromWsId) body.resume_ws = _forkFromWsId;
  if (initial_message) body.initial_message = initial_message;

  var errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";

  var fetchOpts;
  var staged = _forkFromWsId ? [] : _newWsStagedFiles.slice();
  if (staged.length > 0) {
    var form = new FormData();
    form.append("meta", JSON.stringify(body));
    for (var i = 0; i < staged.length; i++) {
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
  var remaining = Object.keys(workstreams);
  // Collect panes showing the closed ws
  var affected = [];
  for (var pid in panes) {
    if (panes[pid].wsId === closedWsId) affected.push(pid);
  }
  if (!affected.length) return;

  // Determine target ws based on close_tab_action setting
  var action = "last_used";
  try {
    action =
      localStorage.getItem("turnstone_interface.close_tab_action") ||
      "last_used";
  } catch (_) {}

  if (action === "dashboard" && remaining.length > 0) {
    // Show dashboard, but still need to reassign panes to valid ws
    for (var di = 0; di < affected.length; di++) {
      var dp = panes[affected[di]];
      dp.disconnectSSE();
      if (remaining.length) {
        dp.wsId = remaining[0];
        dp.messagesEl.innerHTML = "";
        dp.showEmptyState();
        dp.updateWsName();
        dp.connectSSE(remaining[0]);
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
  var preferredWsId = null;
  if (action === "last_used") {
    if (
      _lastActiveWsId &&
      _lastActiveWsId !== closedWsId &&
      workstreams[_lastActiveWsId]
    ) {
      preferredWsId = _lastActiveWsId;
    }
  } else if (action === "nearest_left" || action === "nearest_right") {
    var idx = tabIdsBeforeClose ? tabIdsBeforeClose.indexOf(closedWsId) : -1;
    if (idx >= 0) {
      if (action === "nearest_left") {
        // Walk left, then right
        for (var li = idx - 1; li >= 0; li--) {
          if (workstreams[tabIdsBeforeClose[li]]) {
            preferredWsId = tabIdsBeforeClose[li];
            break;
          }
        }
        if (!preferredWsId) {
          for (var ri = idx + 1; ri < tabIdsBeforeClose.length; ri++) {
            if (workstreams[tabIdsBeforeClose[ri]]) {
              preferredWsId = tabIdsBeforeClose[ri];
              break;
            }
          }
        }
      } else {
        // Walk right, then left
        for (var ri2 = idx + 1; ri2 < tabIdsBeforeClose.length; ri2++) {
          if (workstreams[tabIdsBeforeClose[ri2]]) {
            preferredWsId = tabIdsBeforeClose[ri2];
            break;
          }
        }
        if (!preferredWsId) {
          for (var li2 = idx - 1; li2 >= 0; li2--) {
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
  var usedWsIds = {};
  for (var pid2 in panes) {
    if (affected.indexOf(pid2) === -1) usedWsIds[panes[pid2].wsId] = true;
  }

  for (var i = 0; i < affected.length; i++) {
    var p = panes[affected[i]];
    // Try the preferred ws first, then fall back to first unused
    var newWsId = null;
    if (preferredWsId && !usedWsIds[preferredWsId]) {
      newWsId = preferredWsId;
    } else {
      for (var j = 0; j < remaining.length; j++) {
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
  renderTabBar();
  if (currentWsId && workstreams[currentWsId]) {
    switchTab(currentWsId);
  }
}

function closeWorkstream(wsId) {
  // Capture tab order from DOM (visual order) before deletion for close_tab_action=nearest_left/right
  var tabIdsBeforeClose = Array.from(
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
        var remaining = Object.keys(workstreams);
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
  document.getElementById("ui-header").inert = true;
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
  document.getElementById("ui-header").inert = false;
  document.getElementById("tab-bar").inert = false;
  document.getElementById("split-root").inert = false;
  document.getElementById("dashboard-input").value = "";
  _dashboardStagedFiles = [];
  _renderDashboardChips();
  _refreshDashboardSubmitLabel();
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
        activeWsIds[ws.ws_id] = true;
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
      (workstreams[ws.ws_id] && workstreams[ws.ws_id].state) ||
      ws.state ||
      "idle";
    var liveName =
      (workstreams[ws.ws_id] && workstreams[ws.ws_id].name) ||
      ws.name ||
      ws.ws_id;
    var sd = STATE_DISPLAY[liveState] || STATE_DISPLAY.idle;

    var row = document.createElement("div");
    row.className = "dash-row";
    row.dataset.wsId = ws.ws_id;
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
    statsEl.textContent += " \u00b7 backend degraded";
  }
}

var _wsDeleteMode = false;
var _wsDeleteSelected = {};
var _wsSavedItems = [];

function renderSavedWorkstreams(items) {
  _wsSavedItems = items;
  var c = document.getElementById("dashboard-saved-cards");
  c.replaceChildren();
  if (!items.length) {
    var empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = "No saved workstreams";
    c.appendChild(empty);
    return;
  }
  items.forEach(function (sess) {
    // Default card shape (title + meta + wsid + Resume click) comes from
    // the shared /shared/cards.js helper so console (Saved Coordinators)
    // and ui/static (Saved Workstreams) stay in lock-step.  Delete mode
    // is interactive-only; we layer the checkbox + selection wiring on
    // top of the shared card after construction.
    var card = renderSessionCard(sess, {
      ariaLabel: function (s) {
        var label = s.alias || s.title || s.ws_id;
        return _wsDeleteMode ? "Select: " + label : "Resume: " + label;
      },
      onActivate: function (s) {
        // Suppressed in delete mode \u2014 the layered checkbox handler below
        // owns clicks while delete-mode is active.
        if (_wsDeleteMode) return;
        dashboardResumeSession(s.ws_id);
      },
    });

    if (_wsDeleteMode) {
      card.classList.add("ws-delete-mode");
      card.removeAttribute("role"); // becomes a checkbox host, not a button
      var chk = document.createElement("input");
      chk.type = "checkbox";
      chk.className = "ws-card-check";
      chk.checked = !!_wsDeleteSelected[sess.ws_id];
      var label = sess.alias || sess.title || sess.ws_id;
      chk.setAttribute("aria-label", "Select " + label + " for deletion");
      chk.onclick = function (e) {
        e.stopPropagation();
        if (chk.checked) _wsDeleteSelected[sess.ws_id] = true;
        else delete _wsDeleteSelected[sess.ws_id];
        card.classList.toggle("ws-selected", chk.checked);
        updateWsDeleteBar();
      };
      card.insertBefore(chk, card.firstChild);
      // Override the shared helper's onclick/onkeydown \u2014 in delete mode
      // a card click toggles the checkbox instead of activating Resume.
      card.onclick = function (e) {
        if (e.target === chk) return;
        chk.checked = !chk.checked;
        chk.onclick(e);
      };
      card.onkeydown = function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          chk.checked = !chk.checked;
          chk.onclick(e);
        }
      };
      if (_wsDeleteSelected[sess.ws_id]) card.classList.add("ws-selected");
    }

    c.appendChild(card);
  });
}

function startWsDeleteMode() {
  _wsDeleteMode = true;
  _wsDeleteSelected = {};
  renderSavedWorkstreams(_wsSavedItems);
  var btn = document.getElementById("ws-delete-btn");
  if (btn) {
    btn.textContent = "\u2715 Cancel";
    btn.onclick = cancelWsDeleteMode;
  }
  var bar = document.getElementById("ws-delete-bar");
  if (bar) bar.classList.add("visible");
}

function cancelWsDeleteMode() {
  _wsDeleteMode = false;
  _wsDeleteSelected = {};
  renderSavedWorkstreams(_wsSavedItems);
  var btn = document.getElementById("ws-delete-btn");
  if (btn) {
    btn.innerHTML = "&#x1f5d1; Delete";
    btn.onclick = startWsDeleteMode;
  }
  var bar = document.getElementById("ws-delete-bar");
  if (bar) bar.classList.remove("visible");
}

function updateWsDeleteBar() {
  var count = Object.keys(_wsDeleteSelected).length;
  var label = document.getElementById("ws-delete-bar-count");
  if (label) label.textContent = count + " selected";
  var delBtn = document.getElementById("ws-delete-bar-delete");
  if (delBtn) delBtn.disabled = count === 0;
  var selBtn = document.getElementById("ws-delete-bar-select-all");
  if (selBtn) {
    var allSelected =
      count === _wsSavedItems.length && _wsSavedItems.length > 0;
    selBtn.textContent = allSelected ? "Deselect All" : "Select All";
  }
}

function toggleSelectAll() {
  var allSelected =
    Object.keys(_wsDeleteSelected).length === _wsSavedItems.length &&
    _wsSavedItems.length > 0;
  if (allSelected) {
    _wsDeleteSelected = {};
  } else {
    _wsSavedItems.forEach(function (s) {
      _wsDeleteSelected[s.ws_id] = true;
    });
  }
  renderSavedWorkstreams(_wsSavedItems);
  updateWsDeleteBar();
}

var _wsDeleteBatchTrap = null;

function confirmWsDeleteSelection() {
  var selected = Object.keys(_wsDeleteSelected);
  if (!selected.length) {
    showToast("No workstreams selected", "warning");
    return;
  }
  var overlay = document.getElementById("ws-delete-overlay");
  var countEl = document.getElementById("ws-delete-count");
  var listEl = document.getElementById("ws-delete-list");
  var errorEl = document.getElementById("ws-delete-error");
  errorEl.textContent = "";
  countEl.textContent =
    selected.length + " workstream(s) will be permanently deleted:";
  listEl.innerHTML = "";
  selected.forEach(function (wsId) {
    var item = _wsSavedItems.find(function (s) {
      return s.ws_id === wsId;
    });
    var name = item ? item.alias || item.title || wsId : wsId;
    var div = document.createElement("div");
    div.className = "ws-delete-item";
    div.textContent = name;
    listEl.appendChild(div);
  });
  // Reset confirm button handler (may have been overwritten to "Close" by previous run)
  var delBtn = document.getElementById("ws-delete-confirm-btn");
  if (delBtn) {
    delBtn.textContent = "Delete";
    delBtn.disabled = false;
    delBtn.classList.remove("ws-delete-close");
    delBtn.onclick = confirmWsDelete;
  }
  var cancelBtn = document.getElementById("ws-delete-cancel-btn");
  if (cancelBtn) cancelBtn.disabled = false;
  overlay.style.display = "flex";

  // Focus trap + Escape
  if (_wsDeleteBatchTrap)
    document.removeEventListener("keydown", _wsDeleteBatchTrap);
  _wsDeleteBatchTrap = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelWsDelete();
      return;
    }
    if (e.key === "Tab") {
      var box = document.getElementById("ws-delete-box");
      var focusable = box.querySelectorAll("button:not(:disabled)");
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _wsDeleteBatchTrap);
  if (cancelBtn) cancelBtn.focus();
}

function cancelWsDelete() {
  document.getElementById("ws-delete-overlay").style.display = "none";
  if (_wsDeleteBatchTrap) {
    document.removeEventListener("keydown", _wsDeleteBatchTrap);
    _wsDeleteBatchTrap = null;
  }
}

function confirmWsDelete() {
  var selected = Object.keys(_wsDeleteSelected);
  if (!selected.length) return;
  var overlay = document.getElementById("ws-delete-overlay");
  var errorEl = document.getElementById("ws-delete-error");
  var listEl = document.getElementById("ws-delete-list");
  var countEl = document.getElementById("ws-delete-count");
  var delBtn = document.getElementById("ws-delete-confirm-btn");
  var cancelBtn = document.getElementById("ws-delete-cancel-btn");
  errorEl.textContent = "";

  // Disable buttons during deletion
  if (delBtn) {
    delBtn.disabled = true;
    delBtn.textContent = "Deleting...";
  }
  if (cancelBtn) cancelBtn.disabled = true;

  var results = [];
  var promises = selected.map(function (wsId) {
    var shortId = wsId.substring(0, 8);
    var item = _wsSavedItems.find(function (s) {
      return s.ws_id === wsId;
    });
    var name = item ? item.alias || item.title || wsId : wsId;
    var url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete";

    return authFetch(url, { method: "POST" })
      .then(function (r) {
        var status = r.status;
        var contentType = r.headers.get("content-type") || "";
        if (r.ok) {
          results.push({ name: name, shortId: shortId, ok: true });
          return;
        }
        // Read body as text first to avoid JSON parse errors
        return r.text().then(function (body) {
          var errMsg = shortId + ": HTTP " + status;
          if (contentType.includes("json")) {
            try {
              var j = JSON.parse(body);
              if (j.error) errMsg = shortId + ": " + j.error;
            } catch (_) {
              /* fall through */
            }
          } else if (body) {
            errMsg = shortId + ": " + body.substring(0, 200);
          }
          results.push({
            name: name,
            shortId: shortId,
            ok: false,
            error: errMsg,
          });
        });
      })
      .catch(function (err) {
        results.push({
          name: name,
          shortId: shortId,
          ok: false,
          error: shortId + ": " + err.message,
        });
      });
  });

  Promise.all(promises).then(function () {
    // Rebuild the list with results
    listEl.innerHTML = "";
    results.forEach(function (r) {
      var div = document.createElement("div");
      div.className = "ws-delete-item" + (r.ok ? "" : " ws-delete-error");
      div.textContent =
        (r.ok ? "\u2713 " : "\u2717 ") +
        r.name +
        (r.error ? " — " + r.error : "");
      listEl.appendChild(div);
    });

    var okCount = results.filter(function (r) {
      return r.ok;
    }).length;
    var failCount = results.filter(function (r) {
      return !r.ok;
    }).length;
    countEl.textContent = okCount + " deleted, " + failCount + " failed";

    if (delBtn) {
      delBtn.disabled = false;
      delBtn.textContent = "Close";
      delBtn.classList.add("ws-delete-close");
      delBtn.onclick = function () {
        cancelWsDelete();
        cancelWsDeleteMode();
        loadDashboard();
      };
    }
    if (cancelBtn) cancelBtn.disabled = false;
  });
}

// --- Workstream title management ---

var _lastActiveWsId = null;

function refreshWorkstreamTitle(optWsId) {
  var wsId = optWsId || getCurrentWsId();
  if (!wsId) return;

  var url =
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

var _editTitleTrap = null;

function editWorkstreamTitle(optWsId) {
  var wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  var currentTitle = "";
  var tabEl = document.querySelector(
    '.ws-tab[data-ws-id="' + wsId + '"] .tab-name',
  );
  if (tabEl) currentTitle = tabEl.textContent.trim();

  var overlay = document.getElementById("edit-title-overlay");
  var input = document.getElementById("edit-title-input");
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
      var box = document.getElementById("edit-title-box");
      var focusable = box.querySelectorAll("input, button");
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
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
  var chevron = document.querySelector(".ws-tab.active .tab-chevron");
  if (chevron) chevron.focus();
}

function submitEditTitle() {
  var wsId = getCurrentWsId();
  if (!wsId) return;
  var input = document.getElementById("edit-title-input");
  var newTitle = input.value.trim();
  if (!newTitle) {
    showToast("Title cannot be empty", "warning");
    return;
  }

  var url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/title";

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
      var nameEls = document.querySelectorAll(
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

var _pendingDeleteWsId = null;
var _deleteWsTrap = null;

function confirmDeleteWorkstream(optWsId) {
  var wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  if (Object.keys(workstreams).length <= 1) return;
  var tabEl = document.querySelector(
    '.ws-tab[data-ws-id="' + wsId + '"] .tab-name',
  );
  var name = tabEl ? tabEl.textContent.trim() : wsId.substring(0, 12);

  _pendingDeleteWsId = wsId;
  var overlay = document.getElementById("delete-ws-overlay");
  var msg = document.getElementById("delete-ws-message");
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
      var box = document.getElementById("delete-ws-box");
      var focusable = box.querySelectorAll("button");
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
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

  var cancelBtn = overlay.querySelector("button");
  if (cancelBtn) cancelBtn.focus();
}

function cancelDeleteWs() {
  _pendingDeleteWsId = null;
  document.getElementById("delete-ws-overlay").style.display = "none";
  if (_deleteWsTrap) {
    document.removeEventListener("keydown", _deleteWsTrap);
    _deleteWsTrap = null;
  }
  var chevron = document.querySelector(".ws-tab.active .tab-chevron");
  if (chevron) {
    chevron.focus();
  } else {
    var fallback = document.getElementById("new-tab-btn");
    if (fallback) fallback.focus();
  }
}

function executeDeleteWs() {
  var wsId = _pendingDeleteWsId;
  if (!wsId) return;
  cancelDeleteWs();

  var url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete";

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
  var activeTab = document.querySelector(".ws-tab.active");
  if (activeTab) return activeTab.dataset.wsId || "";
  return "";
}

function forkWorkstream(optWsId) {
  var wsId = optWsId || getCurrentWsId();
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
var _dashboardStagedFiles = [];

// Per-kind size caps mirrored from turnstone/core/attachments.py — keep in sync.
var _DASH_IMAGE_CAP = 4 * 1024 * 1024;
var _DASH_TEXT_CAP = 512 * 1024;
var _DASH_MAX_FILES = 10;

function _renderDashboardChips() {
  var chipsEl = document.getElementById("dashboard-attach-chips");
  if (!chipsEl) return;
  chipsEl.textContent = "";
  for (var i = 0; i < _dashboardStagedFiles.length; i++) {
    (function (idx) {
      var f = _dashboardStagedFiles[idx];
      var chip = document.createElement("span");
      chip.className = "new-ws-attach-chip";
      chip.setAttribute("role", "listitem");
      var label = document.createElement("span");
      label.className = "new-ws-attach-chip-name";
      label.textContent = f.name;
      label.title = f.name + " (" + f.size + " bytes)";
      chip.appendChild(label);
      var size = document.createElement("span");
      size.className = "new-ws-attach-chip-size";
      size.textContent = _formatAttachSize(f.size);
      chip.appendChild(size);
      var rm = document.createElement("button");
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
  for (var i = 0; i < files.length; i++) {
    var f = files[i];
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
    var isImage = (f.type || "").indexOf("image/") === 0;
    var cap = isImage ? _DASH_IMAGE_CAP : _DASH_TEXT_CAP;
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

var _dashboardErrorTimer = null;

function _dashboardError(msg) {
  // Live-region message + outline.  title= alone is invisible to screen
  // readers and on touch devices, so we surface the message visibly
  // beneath the textarea via aria-live="polite".
  var input = document.getElementById("dashboard-input");
  var errEl = document.getElementById("dashboard-error");
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
  var btn = document.getElementById("dashboard-submit-btn");
  if (!btn) return;
  var input = document.getElementById("dashboard-input");
  var hasText = input && input.value.trim().length > 0;
  var hasFiles = _dashboardStagedFiles.length > 0;
  btn.textContent = hasText || hasFiles ? "Send" : "Create";
}

function _loadDashboardOptionsLists() {
  // Models
  var modelSel = document.getElementById("dashboard-model");
  var judgeSel = document.getElementById("dashboard-judge-model");
  if (modelSel && modelSel.options.length <= 1) {
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
          modelSel.appendChild(opt);
          if (judgeSel) {
            var jOpt = document.createElement("option");
            jOpt.value = m.alias;
            jOpt.textContent = opt.textContent;
            judgeSel.appendChild(jOpt);
          }
        });
      })
      .catch(function () {
        /* default model still works */
      });
  }
  // Skills
  var skillSel = document.getElementById("dashboard-skill");
  if (skillSel && skillSel.options.length <= 1) {
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
var _DASH_OPTIONS_LS_KEY = "turnstone.dashboard.options_open";
// In-memory fallback for environments where localStorage throws (private
// mode, storage quota, embedded WebViews).  null means "no preference
// recorded this session yet — use the closed default".
var _dashOptionsOpenSession = null;

function _setDashboardOptionsOpen(open) {
  var panel = document.getElementById("dashboard-options");
  var btn = document.getElementById("dashboard-options-btn");
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
  var panel = document.getElementById("dashboard-options");
  if (!panel) return;
  var nextOpen = panel.hasAttribute("hidden");
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
  var saved = null;
  var lsAvailable = true;
  try {
    saved = localStorage.getItem(_DASH_OPTIONS_LS_KEY);
  } catch (_) {
    lsAvailable = false;
  }
  var open;
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
  var summary = document.getElementById("dashboard-options-summary");
  if (!summary) return;
  var bits = [];
  var modelSel = document.getElementById("dashboard-model");
  var judgeSel = document.getElementById("dashboard-judge-model");
  var skillSel = document.getElementById("dashboard-skill");
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
  var input = document.getElementById("dashboard-input");
  var btn = document.getElementById("dashboard-submit-btn");
  var text = input.value.trim();
  var staged = _dashboardStagedFiles.slice();

  var body = {};
  var model = document.getElementById("dashboard-model").value.trim();
  var judge = document.getElementById("dashboard-judge-model").value.trim();
  var skill = document.getElementById("dashboard-skill").value;
  if (model) body.model = model;
  if (judge) body.judge_model = judge;
  if (skill) body.skill = skill;
  if (text) body.initial_message = text;

  input.disabled = true;
  btn.disabled = true;

  var fetchOpts;
  if (staged.length > 0) {
    var form = new FormData();
    form.append("meta", JSON.stringify(body));
    for (var i = 0; i < staged.length; i++) {
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
        var pane = getFocusedPane();
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
      var detail = (err && err.message) || "Unable to reach the server";
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
      // Update ALL matching tab elements (not just first one)
      var nameEls = document.querySelectorAll(
        '[data-ws-id="' + data.ws_id + '"] .tab-name',
      );
      nameEls.forEach(function (el) {
        el.textContent = data.name;
      });
      // Update all panes showing this workstream
      for (var id in panes) {
        if (panes[id].wsId === data.ws_id) panes[id].updateWsName();
      }
    } else if (data.type === "ws_created") {
      workstreams[data.ws_id] = workstreams[data.ws_id] || {};
      workstreams[data.ws_id].name = data.name || data.ws_id.slice(0, 6);
      workstreams[data.ws_id].state = "idle";
      renderTabBar();
    } else if (data.type === "ws_closed") {
      var wsId = data.ws_id;
      // Capture tab order from DOM (visual order) before deletion for close_tab_action=nearest_left/right
      var sseTabIds = Array.from(
        document.querySelectorAll("#tab-list .ws-tab"),
      ).map(function (tab) {
        return tab.dataset.wsId;
      });
      // Disconnect per-ws SSE on affected panes immediately so stale
      // events from the dying workstream don't leak into reassigned panes.
      for (var cid in panes) {
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
  div.className = "ts-approval-tool";
  div.dataset.funcName = item.func_name || "";
  div.dataset.callId = item.call_id || "";

  var name = document.createElement("div");
  name.className = "tool-name" + (item.error ? " tool-name--error" : "");
  name.textContent = item.func_name || "";
  // Inline auto-approve indicator — surfaces tools that bypassed the
  // operator approval gate (skill allowlist / blanket / admin policy /
  // explicit "Approve + Always") right next to the tool name.  The
  // coord-tree pill is bounded to the coord page; this small badge
  // gives the operator the same signal on the per-ws page they
  // navigated into.
  if (item.auto_approved) {
    var badge = document.createElement("span");
    badge.className = "tool-auto-approved";
    var reason = item.auto_approve_reason || "auto_approve_tools";
    badge.textContent = " auto: " + reason;
    badge.title = "Tool auto-approved (no operator prompt) — reason: " + reason;
    name.appendChild(badge);
  }
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
    ' ts-verdict-badge" data-risk="' +
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

// Append an "✗ error" pill to an approval block as a sibling of the
// existing approved/denied/auto-approved pill, so the approval verdict
// stays visible alongside the execution outcome. Idempotent — re-fires
// (live + history rerender) do not stack badges.
function appendToolErrorBadge(blockEl) {
  if (!blockEl) return;
  if (blockEl.querySelector(".ts-approval-badge--error")) return;
  var errBadge = document.createElement("div");
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
//  12a. Media embed renderer (MCP tool output with stream_url / results)
// ===========================================================================

function tryParseMedia(text) {
  try {
    var obj = JSON.parse(text);
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
  var mins = 0;
  if (typeof item.runtime_minutes === "number") {
    mins = Math.round(item.runtime_minutes);
  } else if (typeof item.runtime_ticks === "number") {
    mins = Math.round(item.runtime_ticks / 600000000);
  }
  if (!mins) return "";
  var h = Math.floor(mins / 60);
  var m = mins % 60;
  return h > 0 ? h + "h " + m + "m" : m + "m";
}

function _redactApiKeys(text) {
  // Query-string style: api_key=VALUE
  var redacted = text.replace(
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
  try {
    var obj = JSON.parse(text);
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
  var out = document.createElement("div");
  out.className = "tool-output" + (isError ? " tool-output-error" : "");
  if (!isError) {
    var pretty = _tryPrettyJson(stripped);
    if (pretty) {
      out.textContent = pretty;
      return out;
    }
  }
  out.textContent = _redactApiKeys(stripped);
  return out;
}

function buildMediaEmbed(media, rawJson) {
  var wrapper = document.createElement("div");
  wrapper.className = "media-embed";

  if (media.stream_url) {
    var card = buildMediaCard(media);
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
  var raw = document.createElement("div");
  raw.className = "tool-output";
  raw.textContent = _tryPrettyJson(rawJson) || _redactApiKeys(rawJson);
  makeCollapsible(raw);
  wrapper.appendChild(raw);

  return wrapper;
}

function buildMediaCard(item) {
  var card = document.createElement("div");
  card.className = "media-card";

  // Thumbnail
  var thumbUrl = item.thumbnail_url || item.image_url || "";
  if (thumbUrl) {
    var img = document.createElement("img");
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
  var info = document.createElement("div");
  info.className = "media-card-info";

  // Title (Year)
  var title = document.createElement("div");
  title.className = "media-card-title";
  var titleText = item.title || item.name || "Untitled";
  if (item.year || item.production_year) {
    titleText += " (" + (item.year || item.production_year) + ")";
  }
  title.textContent = titleText;
  info.appendChild(title);

  // Metadata line: type, runtime, genres
  var metaParts = [];
  if (item.type || item.media_type) {
    metaParts.push(item.type || item.media_type);
  }
  var runtime = _formatRuntime(item);
  if (runtime) metaParts.push(runtime);
  if (item.genres && item.genres.length) {
    metaParts.push(item.genres.join(", "));
  }
  if (metaParts.length) {
    var meta = document.createElement("div");
    meta.className = "media-card-meta";
    meta.textContent = metaParts.join(" \u00b7 ");
    info.appendChild(meta);
  }

  card.appendChild(info);
  return card;
}

function buildPlayButton(media) {
  var btn = document.createElement("button");
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

  var icon = document.createElement("span");
  icon.textContent = "\u25b6";
  btn.appendChild(icon);
  var label = document.createElement("span");
  label.textContent = "Play";
  btn.appendChild(label);
  return btn;
}

function buildMediaResultsList(results, totalCount) {
  var container = document.createElement("div");
  container.className = "media-results-list";

  for (var i = 0; i < results.length; i++) {
    var item = results[i];
    var row = document.createElement("div");
    row.className = "media-result-row";

    // Small thumbnail
    var thumbUrl = item.thumbnail_url || item.image_url || "";
    if (thumbUrl) {
      var img = document.createElement("img");
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
    var titleSpan = document.createElement("span");
    titleSpan.className = "media-result-title";
    var titleText = item.name || item.title || "Untitled";
    if (item.year || item.production_year) {
      titleText += " (" + (item.year || item.production_year) + ")";
    }
    titleSpan.textContent = titleText;
    row.appendChild(titleSpan);

    // Metadata: type, runtime or season info
    var metaParts = [];
    if (item.type || item.media_type) {
      metaParts.push(item.type || item.media_type);
    }
    var runtime = _formatRuntime(item);
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
      var metaSpan = document.createElement("span");
      metaSpan.className = "media-result-meta";
      metaSpan.textContent = " \u00b7 " + metaParts.join(" \u00b7 ");
      row.appendChild(metaSpan);
    }

    container.appendChild(row);
  }

  // "showing X of Y results" footer
  if (typeof totalCount === "number" && totalCount > results.length) {
    var count = document.createElement("div");
    count.className = "media-results-count";
    count.textContent =
      "showing " + results.length + " of " + totalCount + " results";
    container.appendChild(count);
  }

  return container;
}

// ---------------------------------------------------------------------------
//  HLS lazy-loader (follows the mermaid.js lazy-load pattern in
//  /shared/renderer.js)
// ---------------------------------------------------------------------------
var _hlsState = "idle";
var _hlsQueue = [];

function _loadHls(callback) {
  if (_hlsState === "ready") {
    callback();
    return;
  }
  _hlsQueue.push(callback);
  if (_hlsState === "loading") return;
  _hlsState = "loading";
  var script = document.createElement("script");
  script.src = "/shared/hls-1.6.16/hls.min.js";
  script.onload = function () {
    _hlsState = "ready";
    var q = _hlsQueue;
    _hlsQueue = [];
    for (var i = 0; i < q.length; i++) q[i]();
  };
  script.onerror = function () {
    _hlsState = "idle";
    var q = _hlsQueue;
    _hlsQueue = [];
    // Fall through — _activatePlayer will use stream_url since Hls is undefined
    for (var i = 0; i < q.length; i++) q[i]();
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
  var url = btn.dataset.streamUrl;
  var hlsUrl = btn.dataset.hlsUrl;
  var isAudio = btn.dataset.audioOnly === "true";
  var directStream = btn.dataset.directStream === "true";

  var player = document.createElement(isAudio ? "audio" : "video");
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
    var hls = new Hls();
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
    var card = player.closest(".media-embed");
    var titleEl = card ? card.querySelector(".media-card-title") : null;
    var label = titleEl ? ": " + titleEl.textContent : "";

    var err = document.createElement("div");
    err.className = "media-player-error";
    err.setAttribute("role", "alert");
    err.textContent = "Failed to load stream" + label;

    var retry = document.createElement("button");
    retry.className = "media-play-btn";
    retry.type = "button";
    retry.dataset.streamUrl = url;
    retry.dataset.hlsUrl = hlsUrl || "";
    retry.dataset.audioOnly = String(isAudio);
    retry.dataset.directStream = String(directStream);
    retry.setAttribute("aria-label", "Retry" + label);
    retry.appendChild(document.createTextNode("\u25b6 Retry"));

    var container = document.createElement("div");
    container.appendChild(err);
    container.appendChild(retry);
    player.replaceWith(container);
  });

  btn.replaceWith(player);
}

document.addEventListener("click", function (e) {
  var btn = e.target.closest(".media-play-btn");
  if (!btn) return;
  e.preventDefault();
  btn.disabled = true;
  var labelEl = btn.querySelector("span:last-child");
  if (labelEl) {
    labelEl.textContent = "Loading\u2026";
  } else {
    btn.textContent = "\u25b6 Loading\u2026";
  }

  var hlsUrl = btn.dataset.hlsUrl;
  var isAudio = btn.dataset.audioOnly === "true";

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
  var btn = e.target.closest(".media-play-btn");
  if (!btn) return;
  btn.click();
});

// ===========================================================================
//  13. Plan review dialog
// ===========================================================================

var _planContent = "";
var _planPaneId = null;
var _planWsId = null;

function showPlanDialog(content) {
  _planContent = content;
  _planPaneId = focusedPaneId;
  var paneNow = panes[_planPaneId];
  _planWsId = paneNow ? paneNow.wsId : currentWsId;
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
  // Removing 'active' synchronously is what lets dismissPlanDialog's
  // early-return guard treat the server's echoed plan_resolved as a no-op.
  document.getElementById("plan-overlay").classList.remove("active");

  var pane = panes[_planPaneId];
  if (pane) {
    pane.inputEl.disabled = false;
    pane.sendBtn.disabled = false;
    pane.inputEl.focus();
  }

  // Critical: fire the API call first — this unblocks the server.
  // Use the ws_id captured when the dialog opened, not the current pane
  // (user may have switched tabs while the dialog was open).
  var wsId = _planWsId || (pane ? pane.wsId : currentWsId);
  _planWsId = null;
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

function dismissPlanDialog(feedback) {
  // Sync-dismiss: another client (or the server) already resolved the plan.
  // Do NOT call /v1/api/plan — the server has already moved on.  The early
  // return also handles self-receipt: the client that called resolvePlan()
  // already removed the active class, so this is a no-op for that client.
  var overlay = document.getElementById("plan-overlay");
  if (!overlay.classList.contains("active")) return;
  overlay.classList.remove("active");

  var pane = panes[_planPaneId];
  if (pane) {
    pane.inputEl.disabled = false;
    pane.sendBtn.disabled = false;
    // Restore keyboard context — but skip on touch so we don't surprise the
    // mobile user with a soft-keyboard pop after a remote approval.
    if (!matchMedia("(pointer: coarse)").matches) pane.inputEl.focus();
  }

  var fb = feedback || "";
  var isReject = fb === "reject";
  var isAmend = fb && !isReject;
  var action = isReject ? "rejected" : isAmend ? "amending" : "approved";

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
  var el = document.getElementById("toast");
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
  // Disambiguate remote dismissal — otherwise the desktop user sees "Plan
  // approved" with no attribution and may wonder if the agent self-approved.
  if (origin === "remote") label += " (synced)";
  var labelEl = document.createElement("span");
  labelEl.className = "plan-inline-label plan-" + action;
  labelEl.textContent = label;
  header.appendChild(labelEl);
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

// Dashboard composer wiring — Enter (no shift) submits, input refreshes the
// button label, paperclip + drag-drop + paste-image stage files, options
// toggle expands the dropdown panel.
(function () {
  var input = document.getElementById("dashboard-input");
  var attachBtn = document.getElementById("dashboard-attach-btn");
  var attachInput = document.getElementById("dashboard-attach-input");
  var optionsBtn = document.getElementById("dashboard-options-btn");
  var composer = document.getElementById("dashboard-composer");
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
    var items = e.clipboardData.items || [];
    var pasted = [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === "file") {
        var f = items[i].getAsFile();
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
  var optionsPanel = document.getElementById("dashboard-options");
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

document.addEventListener("keydown", function (e) {
  // Defer to modal's own keydown handler when any modal is open
  var modalIds = [
    "new-ws-overlay",
    "edit-title-overlay",
    "delete-ws-overlay",
    "ws-delete-overlay",
  ];
  for (var mi = 0; mi < modalIds.length; mi++) {
    var modal = document.getElementById(modalIds[mi]);
    if (modal && modal.style.display !== "none") return;
  }

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
  // Workstream action shortcuts — only preventDefault when a workstream
  // is active, so native browser shortcuts (e.g. Ctrl+Shift+R hard reload)
  // still work when no workstream is focused.
  if (e.ctrlKey && e.shiftKey) {
    closeTabDropdown();
    var wsActionKey = e.key.toLowerCase();
    var activeWsId = !dashboardVisible && getCurrentWsId();
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
        workstreams[ws.ws_id] = { name: ws.name, state: ws.state };
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
loadInterfaceSettings();
initWorkstreams();

function loadInterfaceSettings() {
  authFetch("/v1/api/admin/settings")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      var settings = data.settings || [];
      for (var i = 0; i < settings.length; i++) {
        var s = settings[i];
        if (s.key && s.key.indexOf("interface.") === 0) {
          var lsKey = "turnstone_" + s.key;
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
      var theme = localStorage.getItem("turnstone_interface.theme");
      var currentTheme = document.documentElement.dataset.theme;
      if (theme) {
        var effectiveTheme = theme === "light" ? "light" : "";
        if (effectiveTheme !== currentTheme) {
          document.documentElement.dataset.theme = effectiveTheme;
          var btn = document.getElementById("theme-toggle");
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
