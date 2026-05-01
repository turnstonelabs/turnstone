/* coordinator.js — one-pane UI for console-hosted coordinator sessions.
 *
 * Connects to:
 *   GET  /v1/api/workstreams/{ws_id}/events  (SSE)
 *   GET  /v1/api/workstreams/{ws_id}/history (initial history)
 *   POST /v1/api/workstreams/{ws_id}/send
 *   POST /v1/api/workstreams/{ws_id}/approve
 *   POST /v1/api/workstreams/{ws_id}/cancel
 *   POST /v1/api/workstreams/{ws_id}/close
 *
 * Depends on shared/auth.js (authFetch, fetchWithCreds), shared/theme.js
 * (toggleTheme), shared/toast.js (toast.info / toast.error),
 * shared/utils.js (escapeHtml, linkify helpers).
 *
 * Assistant content goes through the shared shared_static/renderer.js
 * streaming helpers (streamingRender + streamingRenderFinalize): the
 * helper coalesces renderMarkdown calls through requestAnimationFrame
 * as tokens arrive, then runs the expensive post-render (hljs /
 * mermaid / KaTeX) once on stream_end.  Reasoning bubbles stay
 * text-only because they're transient and styled dim/italic.
 */
(function () {
  "use strict";

  const wsId = document.documentElement.dataset.wsId || "";
  if (!wsId) {
    // Static literal — class-name migration only; no XSS surface.
    const missing = document.createElement("div");
    missing.className = "msg error";
    missing.textContent = "Missing ws_id on <html> tag.";
    const host = document.getElementById("coord-messages");
    if (host) host.replaceChildren(missing);
    return;
  }

  const messagesEl = document.getElementById("coord-messages");
  const coordMain = document.getElementById("coord-main");
  const composerMount = document.getElementById("coord-composer-mount");
  const composer = new Composer(composerMount, {
    placeholder: "Message the coordinator\u2026",
    ariaLabel: "Coordinator input",
    attachments: {
      onAttach: function (file) {
        attachments.upload(file);
      },
    },
    stopBtn: true,
    queueWhileBusy: true,
    busyPlaceholder: "Queue a message\u2026 (!!! for urgent)",
    onSend: function () {
      coordSend();
    },
    onStop: function () {
      cancelGeneration();
    },
    // Coord sessions are short — tap-to-send via the on-screen Return
    // key is faster than tapping a Send button on touch.
    touchEnterSends: true,
    dragDrop: { targetEl: coordMain, dropClass: "coord-drop-target" },
  });
  const stopBtn = composer.stopBtn;
  const attachments = createAttachmentController({
    chipsEl: composer.chipsEl,
    getWsId: function () {
      return wsId;
    },
  });
  const queue = createQueueController({
    messagesEl: messagesEl,
    getWsId: function () {
      return wsId;
    },
    // Coord chat bubbles wrap content in a .msg-body div (appendMsg
    // below); the queue bubble matches so its border + padding align.
    wrapInBody: true,
    // Re-fetch attachments after a dequeue so the user can see (and
    // reuse) any reservations the server-side dequeue released. Trades
    // a small in-flight-placeholder clobbering window for the strictly
    // worse alternative of attachments lingering invisibly until the
    // next page load.
    onAfterDequeue: function () {
      attachments.rehydrate();
    },
    // Idle-edge cleanup of the cancel/force-stop timers — without
    // this they fire on the *next* busy turn, relabel Stop to "Force
    // Stop", and surface a misleading "Cancel didn't complete in
    // time" toast unrelated to the new turn.
    onIdle: function () {
      if (cancelTimeoutId) {
        clearTimeout(cancelTimeoutId);
        cancelTimeoutId = null;
      }
      if (forceTimeoutId) {
        clearTimeout(forceTimeoutId);
        forceTimeoutId = null;
      }
    },
  });
  let busy = false;
  let cancelTimeoutId = null;
  let forceTimeoutId = null;
  const statusEl = document.getElementById("coord-status");
  const sseEl = document.getElementById("coord-sse-status");
  const nameEl = document.getElementById("coord-name");
  const childrenTreeEl = document.getElementById("coord-children-tree");
  const childrenCountEl = document.getElementById("coord-children-count");
  const childrenRefreshBtn = document.getElementById("coord-children-refresh");
  const tasksEl = document.getElementById("coord-tasks");
  const tasksCountEl = document.getElementById("coord-tasks-count");
  const tasksRefreshBtn = document.getElementById("coord-tasks-refresh");
  // Off-screen aria-live="assertive" region — pending tool-batches
  // append into the polite messages log, which gets flipped to
  // aria-live="off" during streaming.  Routing the action-required
  // announcement through this dedicated region ensures SR users hear
  // the gate land regardless of streaming state.
  const srAnnouncerEl = document.getElementById("coord-sr-announcer");
  function _announceAssertive(text) {
    if (!srAnnouncerEl) return;
    // Briefly clearing then setting forces SR reading even if the
    // text is identical to the previous announcement (some SRs only
    // read on textContent change).
    srAnnouncerEl.textContent = "";
    requestAnimationFrame(() => {
      srAnnouncerEl.textContent = text;
    });
  }

  // Status bar — model alias, token / context-window usage, tool calls
  // this turn, conversation turn.  Driven by the connected + status
  // SSE events; mirrors the interactive pane (ui/static/app.js).
  const statusBarEl = document.getElementById("coord-status-bar");
  const sbModelEl = document.getElementById("coord-sb-model");
  const sbTokensEl = document.getElementById("coord-sb-tokens");
  const sbToolsEl = document.getElementById("coord-sb-tools");
  const sbTurnsEl = document.getElementById("coord-sb-turns");
  let coordModel = "";
  let coordModelAlias = "";
  let lastStatusEvt = null;

  let evtSource = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;

  // Cache of judge verdicts keyed by call_id.  intent_verdict and
  // approve_request are async and may arrive in either order; the
  // cache lets each handler apply data to the other without assuming
  // ordering.  Soft-capped at JUDGE_VERDICTS_CAP entries — Maps
  // preserve insertion order, so the oldest entry is the one yielded
  // by .keys().next() and is evicted when the cap is exceeded.  Cap
  // is generous because verdicts are small (~few hundred bytes each)
  // and the only consumer is the rare race where SSE re-fires
  // approve_request after the originally-cached entry has been
  // applied.
  const JUDGE_VERDICTS_CAP = 500;
  const judgeVerdicts = new Map();
  function _cacheJudgeVerdict(callId, verdict) {
    if (!callId) return;
    judgeVerdicts.set(callId, verdict);
    while (judgeVerdicts.size > JUDGE_VERDICTS_CAP) {
      const oldest = judgeVerdicts.keys().next().value;
      if (oldest === undefined) break;
      judgeVerdicts.delete(oldest);
    }
  }

  // ------------------------------------------------------------------
  // HTML escaping and safe ws_id linkification
  // ------------------------------------------------------------------

  function esc(s) {
    // shared/utils.js exposes the lowercase-h name; check that first.
    if (typeof escapeHtml === "function")
      return escapeHtml(String(s == null ? "" : s));
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ANSI-escape stripper — mirrors ui/static/app.js so a tool that
  // emits CSI sequences (rare on the coord tool surface, but bash
  // through MCP / the underlying child node still can) lands as
  // readable text inside the tool-batch result block.
  function stripAnsi(s) {
    return String(s == null ? "" : s).replace(
      // eslint-disable-next-line no-control-regex
      /\x1b(?:\[[0-9;?]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[()#][A-Za-z0-9]|.)/g,
      "",
    );
  }

  // Post-process tool-output JSON: wrap known ws_id + node_id pairs in a
  // link pointing at /node/{node_id}/?ws_id={child_ws_id}.  Only applies
  // when BOTH keys are present and look like valid hex ids.
  const WS_ID_RE = /^[a-f0-9]{8,64}$/i;
  const NODE_ID_RE = /^[A-Za-z0-9._-]{1,256}$/;

  // Auto-approve reason vocabulary — kept in lockstep with the
  // ``AutoApproveReason`` constants in turnstone/core/session_ui_base.py.
  // The pill renderer validates incoming reason strings against this
  // set so a server-side typo (or a future reason this build doesn't
  // know about) renders as "unknown" with a console.warn instead of
  // silently surfacing the typo verbatim in the operator-facing label.
  const KNOWN_AUTO_APPROVE_REASONS = new Set([
    "skill",
    "always",
    "policy",
    "blanket",
    "auto_approve_tools",
  ]);
  const UNKNOWN_AUTO_APPROVE_REASON = "unknown";

  function _normaliseAutoApproveReason(raw) {
    const r = raw || "auto_approve_tools";
    if (KNOWN_AUTO_APPROVE_REASONS.has(r)) return r;
    console.warn(
      "coord_ui: unknown auto_approve_reason from server:",
      JSON.stringify(raw),
    );
    return UNKNOWN_AUTO_APPROVE_REASON;
  }

  function renderToolOutput(rawText) {
    // Try parse JSON first — coordinator tool output is JSON-shaped.
    let parsed = null;
    try {
      parsed = JSON.parse(rawText);
    } catch (_) {
      /* fall through */
    }
    if (!parsed || typeof parsed !== "object") {
      return esc(rawText);
    }
    // Normalize to an array of rows we can linkify.
    let rows = [];
    if (Array.isArray(parsed.children)) {
      rows = parsed.children;
    } else if (parsed.ws_id && parsed.node_id) {
      rows = [parsed];
    }
    if (rows.length === 0) {
      return "<pre>" + esc(JSON.stringify(parsed, null, 2)) + "</pre>";
    }
    const lines = rows.map((row) => {
      const safeWs = row.ws_id && WS_ID_RE.test(row.ws_id) ? row.ws_id : null;
      const safeNode =
        row.node_id && NODE_ID_RE.test(row.node_id) ? row.node_id : null;
      let link = safeWs || "";
      if (safeWs && safeNode) {
        link =
          '<a class="coord-ws-link" target="_blank" rel="noopener"' +
          ' href="/node/' +
          encodeURIComponent(safeNode) +
          "/?ws_id=" +
          encodeURIComponent(safeWs) +
          '">' +
          esc(safeWs) +
          "</a>";
      }
      const meta = [];
      if (row.state) meta.push("state=" + esc(row.state));
      if (row.name) meta.push("name=" + esc(row.name));
      if (row.node_id) meta.push("node=" + esc(row.node_id));
      return (
        "  " + (link || esc("?")) + (meta.length ? "  " + meta.join(" ") : "")
      );
    });
    return "<pre>" + lines.join("\n") + "</pre>";
  }

  // ------------------------------------------------------------------
  // Message append helpers
  // ------------------------------------------------------------------

  // Coalesce scrollTop writes through requestAnimationFrame so the
  // bulk history-replay loop doesn't fire one synchronous reflow per
  // appended message — for histories with hundreds of turns the
  // un-coalesced version visibly stalls the page.  Live SSE streaming
  // also benefits: token-rate scrolls collapse into one paint.
  let _scrollPending = false;
  function _scheduleScroll() {
    if (_scrollPending) return;
    _scrollPending = true;
    requestAnimationFrame(() => {
      _scrollPending = false;
      messagesEl.scrollTop = messagesEl.scrollHeight;
    });
  }

  // Map raw role → .msg variant (DS primitives/message.css).  "error"
  // overloads the role slot for styling; opts.label still carries the
  // tool name so SR text like "error · bash" stays meaningful on the
  // data-ts-role / aria-label attributes when labels stop rendering
  // as DOM text.
  const _MSG_VARIANTS = {
    user: "user",
    assistant: "assistant",
    reasoning: "reasoning",
    tool: "tool",
    error: "error",
    info: "info",
  };

  function appendMsg(role, html, opts) {
    opts = opts || {};
    const el = document.createElement("div");
    const variant = _MSG_VARIANTS[role] || "assistant";
    el.className = "msg " + variant;
    // role="article" makes aria-label reliably announced by screen
    // readers — a generic <div> with no implicit role doesn't expose
    // aria-label on its own.  "article" fits: each message is a
    // self-contained content unit in the chat log.
    el.setAttribute("role", "article");
    if (opts.callId) el.dataset.callId = opts.callId;
    if (opts.label) {
      // The visible .role-label div is dropped in favour of
      // border-colour differentiation.  Preserve the role text as
      // data-ts-role + aria-label so AT and the SSE dedup-by-call-id
      // path continue to carry the tool name.
      el.setAttribute("data-ts-role", opts.label);
      el.setAttribute("aria-label", opts.label);
    }
    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = html;
    el.appendChild(body);
    messagesEl.appendChild(el);
    _scheduleScroll();
    return el;
  }

  function appendText(role, text, opts) {
    return appendMsg(role, esc(text), opts);
  }

  // Metacognitive reminder bubble (user-channel correction / denial /
  // resume / start / completion AND tool-channel tool_error / repeat).
  // Mirrors Pane.prototype.addUserReminder / addToolReminder in the
  // interactive UI — yellow themed bubble slotted directly below the
  // message it advises.  ``anchor`` is the DOM element to anchor below;
  // when null, append at the bottom of messagesEl.
  function appendReminderBubble(reminders, anchor) {
    if (!Array.isArray(reminders) || !reminders.length) return;
    let cursor = anchor;
    for (let i = 0; i < reminders.length; i++) {
      const r = reminders[i] || {};
      const el = document.createElement("div");
      el.className = "msg user-reminder";
      el.setAttribute("role", "article");
      el.setAttribute("data-ts-role", "metacognition");
      el.setAttribute("aria-label", "metacognition");
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
      if (cursor) {
        cursor.insertAdjacentElement("afterend", el);
        cursor = el;
      } else {
        messagesEl.appendChild(el);
      }
    }
    _scheduleScroll();
  }

  // Live SSE for user-channel reminders — anchors below the most
  // recent user message.  On a non-originating tab there may be no
  // user message rendered yet; we append and the next /history reload
  // corrects.  (Same caveat as the interactive UI; tracked there.)
  function appendUserReminderLive(reminders) {
    const userMsgs = messagesEl.querySelectorAll(".msg.user");
    const anchor = userMsgs.length ? userMsgs[userMsgs.length - 1] : null;
    appendReminderBubble(reminders, anchor);
  }

  // Live SSE for tool-channel reminders — anchors below the
  // .coord-tool-batch construct that produced the tool result.  Looks
  // up the row by data-call-id and walks to the parent batch; falls
  // back to the most recent batch if not found.
  function appendToolReminderLive(reminders, toolCallId) {
    let anchor = null;
    if (toolCallId) {
      const entry = toolRows.get(toolCallId);
      if (entry && entry.batch) {
        anchor = entry.batch;
      }
    }
    if (!anchor) {
      const batches = messagesEl.querySelectorAll(".coord-tool-batch");
      if (batches.length) anchor = batches[batches.length - 1];
    }
    appendReminderBubble(reminders, anchor);
  }

  // Build a tool-batch item from a persisted assistant
  // tool_call.  Live calls land here with header / preview already
  // computed by ChatSession._prepare_tool; history replay never sees
  // those (only `function.name` + `function.arguments`), so synthesise
  // the same fields so the rendered row reads the same on reload.
  //
  // Header rules mirror what _prepare_tool produces for the common
  // tools — bash gets a "$ <command>" header so the operator sees the
  // shell line at a glance; everything else gets "<name>: <key=val …>"
  // with values truncated.  The full pretty-printed args drop into the
  // preview block underneath, capped at a few hundred chars to match
  // the live preview's footprint.
  function synthesizeHistoricalToolCall(name, callId, parsedArgs, argsRaw) {
    let header = name;
    let preview = "";
    const argEntries =
      parsedArgs && typeof parsedArgs === "object" && !Array.isArray(parsedArgs)
        ? Object.entries(parsedArgs)
        : null;
    if (name === "bash" && argEntries && argEntries.length) {
      const cmd = String(
        parsedArgs.command || Object.values(parsedArgs)[0] || "",
      );
      header = "$ " + (cmd.length > 80 ? cmd.slice(0, 77) + "…" : cmd);
      preview = cmd.length > 80 ? cmd : "";
    } else if (argEntries && argEntries.length) {
      const summary = argEntries
        .slice(0, 3)
        .map(([k, v]) => {
          let valStr =
            v == null ? "null" : typeof v === "string" ? v : JSON.stringify(v);
          if (valStr.length > 60) valStr = valStr.slice(0, 57) + "…";
          return k + "=" + valStr;
        })
        .join(" ");
      header = name + ": " + summary;
      try {
        preview = JSON.stringify(parsedArgs, null, 2);
      } catch (_) {
        preview = argsRaw || "";
      }
      if (preview.length > 600) preview = preview.slice(0, 600) + "…";
    } else if (argsRaw) {
      // Malformed JSON or non-object args — show the raw payload
      // truncated.  Matches the interactive replay's substring(0, 100)
      // fallback at ui/static/app.js Pane.prototype.replayHistory.
      header = name;
      preview = argsRaw.length > 200 ? argsRaw.slice(0, 200) + "…" : argsRaw;
    }
    return {
      call_id: callId,
      func_name: name,
      header: header,
      preview: preview,
    };
  }

  function appendToolResult(name, callId, output, isError, opts) {
    if (callId && toolRows.has(callId)) {
      const entry = toolRows.get(callId);
      _appendResultToRow(entry.row, output, isError, opts);
      // Result blocks grow scrollHeight; without this the user pinned
      // at the bottom loses their pin when the row inflates.  appendMsg
      // already routes through _scheduleScroll on the legacy path; this
      // branch was the gap.
      _scheduleScroll();
      return entry.row;
    }
    const html = renderToolOutput(output);
    const el = appendMsg(isError ? "error" : "tool", html, {
      label: (isError ? "error · " : "") + (name || "tool"),
      callId: callId,
    });
    return el;
  }

  // ------------------------------------------------------------------
  // Tool batch construct — paired tool calls + approval + results
  //
  // Replaces the prior pinned approval dock + duplicate .msg.tool
  // bubble pattern.  One construct per dispatch turn:
  //   - solo (1 call, serial):  .coord-tool-batch--solo
  //   - parallel (≥2 calls):    .coord-tool-batch--parallel
  // The approval gate, judge verdicts, and tool results all render
  // inside the construct so the operator reads call → verdict → result
  // as one cohesive unit.
  // ------------------------------------------------------------------

  // call_id → { batch, row }.  Routes intent_verdict + tool_result
  // events to the correct row.  Holds DOM refs only; the originating
  // item payload is intentionally not retained (long sessions would
  // pin per-call preview / parsed-args memory for the page lifetime).
  const toolRows = new Map();

  // Most-recently-rendered batch with an open approval gate.  Used for
  // keyboard focus claiming and approval_resolved fallbacks.
  let activeBatch = null;

  function _formatBatchArgs(item) {
    let s = item.header || item.approval_label || item.preview || "";
    if (item.func_name && s.startsWith(item.func_name + ":")) {
      s = s.slice(item.func_name.length + 1).trim();
    } else if (item.func_name === "bash" && s.startsWith("$ ")) {
      s = s.slice(2);
    }
    return s;
  }

  // Single source of truth for the batch tier badge text.  Both the
  // initial-render path (_pickBatchTier reading from items[]) and the
  // live-refresh path (_refreshBatchTier reading from row dataset)
  // route through here so the literal label can't drift between
  // surfaces.
  function _formatTierLabel(llmModel, hasHeuristic) {
    if (llmModel !== null) {
      return "⚖ llm" + (llmModel ? ":" + llmModel : "");
    }
    if (hasHeuristic) {
      return "⚙ heuristic";
    }
    return "";
  }

  // Pick the highest-tier verdict across items for the initial batch
  // tier badge.  LLM beats heuristic; first LLM verdict's judge_model
  // wins (heterogeneous models within a single envelope is unusual but
  // we default to the leading one for stability).
  function _pickBatchTier(items) {
    let llmModel = null;
    let hasHeuristic = false;
    for (const it of items) {
      const v = it.judge_verdict || it.heuristic_verdict;
      if (!v) continue;
      const tier = v.tier || (it.judge_verdict ? "llm" : "heuristic");
      if (tier === "llm" && llmModel === null) {
        llmModel = v.judge_model || "";
      } else if (tier === "heuristic") {
        hasHeuristic = true;
      }
    }
    return _formatTierLabel(llmModel, hasHeuristic);
  }

  // Coalesce _refreshBatchTier scans across a microtask so a burst of
  // verdict updates on the same batch (e.g. 10 intent_verdict SSE
  // events for a 10-row fan-out arriving in the same tick) collapse
  // into ONE querySelectorAll + DOM compare.  Without this each
  // verdict triggered an O(N-rows) scan, yielding O(N²) DOM walks
  // per batch render or burst.
  const _tierDirtyBatches = new Set();
  let _tierFlushScheduled = false;
  function _refreshBatchTier(batch) {
    if (!batch) return;
    _tierDirtyBatches.add(batch);
    if (_tierFlushScheduled) return;
    _tierFlushScheduled = true;
    queueMicrotask(() => {
      _tierFlushScheduled = false;
      const dirty = Array.from(_tierDirtyBatches);
      _tierDirtyBatches.clear();
      dirty.forEach(_refreshBatchTierImmediate);
    });
  }

  // Synchronous tier-badge recompute.  Called from the microtask
  // flush; do not invoke directly from render-hot paths — go through
  // _refreshBatchTier for the burst-coalescing benefit.
  function _refreshBatchTierImmediate(batch) {
    if (!batch || !batch.isConnected) return;
    let llmModel = null;
    let hasHeuristic = false;
    batch.querySelectorAll(".coord-tool-row").forEach((r) => {
      const t = r.dataset.verdictTier;
      if (t === "llm" && llmModel === null) {
        llmModel = r.dataset.verdictModel || "";
      } else if (t === "heuristic") {
        hasHeuristic = true;
      }
    });
    const head = batch.querySelector(".coord-tool-batch-head");
    if (!head) return;
    let tierEl = head.querySelector(".coord-tool-batch-tier");
    const label = _formatTierLabel(llmModel, hasHeuristic);
    if (label) {
      if (!tierEl) {
        tierEl = document.createElement("span");
        tierEl.className = "coord-tool-batch-tier";
        head.appendChild(tierEl);
      }
      if (tierEl.textContent !== label) tierEl.textContent = label;
    } else if (tierEl) {
      tierEl.remove();
    }
  }

  // Apply / refresh per-row status state from an item payload:
  //  - data-needs-approval="1" when item.needs_approval && !item.error
  //  - .coord-tool-row-status--auto pill when item.auto_approved
  //  - .coord-tool-row-status--error pill + .error class when policy-
  //    blocked (item.error && !needs_approval)
  // Idempotent: clears any prior status pills + the data attribute
  // before re-applying so an upgrade-in-place (e.g. SSE folding tool
  // _info / approve_request items into a previously rendered
  // --running batch) doesn't leave stale markers.  Runtime errors
  // arriving via tool_result are tracked via row.classList.add("error")
  // in _appendResultToRow and intentionally not cleared here — they
  // reflect execution outcome, not the static item payload.
  function _refreshRowStatus(row, item) {
    if (!row || !item) return;
    const callLine = row.querySelector(".coord-tool-row-call");
    if (callLine) {
      callLine
        .querySelectorAll(".coord-tool-row-status")
        .forEach((p) => p.remove());
    }
    delete row.dataset.needsApproval;
    // Don't clear .error here when it came from a result (no
    // matching item.error); only clear the static-policy marker.
    // Approximate by re-deriving solely from item below.
    row.classList.remove("error");
    if (item.needs_approval && !item.error) {
      row.dataset.needsApproval = "1";
    }
    if (callLine && item.auto_approved && !item.needs_approval) {
      const pill = document.createElement("span");
      pill.className = "coord-tool-row-status coord-tool-row-status--auto";
      const reason = _normaliseAutoApproveReason(item.auto_approve_reason);
      pill.textContent = "✓ " + reason;
      pill.title = "auto-approved (no operator prompt) — reason: " + reason;
      callLine.appendChild(pill);
    }
    if (callLine && item.error && !item.needs_approval) {
      const errPill = document.createElement("span");
      errPill.className = "coord-tool-row-status coord-tool-row-status--error";
      errPill.textContent = "✗ " + (item.error || "blocked");
      callLine.appendChild(errPill);
      row.classList.add("error");
    }
    // If a runtime tool_result already landed an .error on this row,
    // re-derive it from the result block's presence so we don't
    // accidentally drop the cue when refreshing from a non-error
    // item shape.
    if (
      row.querySelector(".coord-tool-row-result.coord-tool-row-result--error")
    ) {
      row.classList.add("error");
    }
  }

  function _renderBatchRow(item, indexLabel) {
    const row = document.createElement("div");
    row.className = "coord-tool-row";
    if (item.call_id) row.dataset.callId = item.call_id;
    // Stamp the function name so the metacog dim rule for memory /
    // recall (coordinator.css `.coord-tool-row[data-tool-name=...]`)
    // has something to match.  Cheap; lets long workstreams with
    // heavy memory usage stay readable without per-call JS work.
    if (item.func_name) row.dataset.toolName = item.func_name;

    const callLine = document.createElement("div");
    callLine.className = "coord-tool-row-call";

    if (indexLabel) {
      const idx = document.createElement("span");
      idx.className = "coord-tool-row-idx";
      idx.textContent = indexLabel;
      callLine.appendChild(idx);
    }

    const name = document.createElement("span");
    name.className = "coord-tool-row-name";
    name.textContent = item.func_name || "(unknown tool)";
    callLine.appendChild(name);

    const args = document.createElement("span");
    args.className = "coord-tool-row-args";
    args.textContent = _formatBatchArgs(item);
    callLine.appendChild(args);

    row.appendChild(callLine);
    // Apply status pills + data-needs-approval through the shared
    // helper so initial render and upgrade-in-place can't drift.
    _refreshRowStatus(row, item);
    return row;
  }

  // Attach an output-guard finding chip to a specific .coord-tool-row.
  // Idempotent — replacing an existing chip in place lets the live
  // SSE handler upgrade severity without stacking duplicates when a
  // late event arrives after replay seeded an initial chip.
  function _attachOutputWarningChip(row, oa) {
    if (!row || !oa) return;
    const risk = String(oa.risk_level || "medium");
    const flags = oa.flags || [];
    const existing = row.querySelector(".coord-tool-row-warning");
    const chip = existing || document.createElement("div");
    chip.className = "coord-tool-row-warning coord-tool-row-warning--" + risk;
    chip.setAttribute("role", "status");
    chip.textContent = "";
    const label = document.createElement("span");
    label.className = "coord-tool-row-warning-label";
    label.textContent = "⚠ " + risk.toUpperCase();
    chip.appendChild(label);
    if (flags.length) {
      chip.appendChild(document.createTextNode(" " + flags.join(", ")));
    }
    if (oa.redacted) {
      const redacted = document.createElement("span");
      redacted.className = "coord-tool-row-warning-redacted";
      redacted.textContent = " (credentials redacted)";
      chip.appendChild(redacted);
    }
    if (!existing) row.appendChild(chip);
  }

  // Stable signature for a verdict — used to skip the DOM rebuild when
  // an SSE replay (or duplicate intent_verdict event) carries the same
  // verdict body we already painted.  Any field change (rec/risk/conf
  // /reasoning) flips the signature and re-renders.
  function _verdictSig(verdict) {
    if (!verdict) return "";
    // ``tier`` + ``judge_model`` are part of the signature because a
    // heuristic→llm transition can otherwise share the four core
    // fields (rec / risk / conf / reasoning).  ``dataset.verdictTier``
    // is only updated when the rebuild runs, so dropping the tier from
    // the signature would lock the header on ``⚙ heuristic`` even
    // after the LLM verdict lands.
    return [
      verdict.recommendation || "",
      verdict.risk_level || "",
      verdict.confidence != null ? String(verdict.confidence) : "",
      verdict.reasoning || "",
      verdict.tier || "",
      verdict.judge_model || "",
    ].join("");
  }

  function _appendVerdictLineTo(row, verdict) {
    // Dedupe by signature so reconnect storms don't tear down + rebuild
    // an unchanged verdict line.  _appendVerdictLineTo(row, null) is
    // used by _appendJudgePendingLineTo to clear and start fresh — that
    // path explicitly bypasses the dedupe (sig of null is "").
    const sig = _verdictSig(verdict);
    if (verdict && row.dataset.verdictSig === sig) {
      return row.querySelector(".coord-tool-row-verdict");
    }
    row.dataset.verdictSig = sig;

    let line = row.querySelector(".coord-tool-row-verdict");
    if (!line) {
      line = document.createElement("div");
      line.className = "coord-tool-row-verdict";
      const callEl = row.querySelector(".coord-tool-row-call");
      if (callEl && callEl.nextSibling) {
        row.insertBefore(line, callEl.nextSibling);
      } else {
        row.appendChild(line);
      }
    }
    line.replaceChildren();
    if (verdict) {
      const chip = document.createElement("code");
      const rec = verdict.recommendation || "?";
      const risk = verdict.risk_level || "?";
      chip.textContent = "judge: " + rec + " · risk: " + risk;
      if (rec === "approve") chip.classList.add("rec-approve");
      else if (rec === "review") chip.classList.add("rec-review");
      else if (rec === "deny") chip.classList.add("rec-deny");
      line.appendChild(chip);
      if (verdict.confidence != null) {
        const conf = document.createElement("code");
        const v = verdict.confidence;
        conf.textContent = typeof v === "number" ? v.toFixed(2) : String(v);
        line.appendChild(conf);
      }
    }
    const oldRationale = row.querySelector(".coord-tool-row-rationale");
    if (oldRationale) oldRationale.remove();
    if (verdict && verdict.reasoning) {
      const det = document.createElement("details");
      det.className = "coord-tool-row-rationale";
      const sum = document.createElement("summary");
      sum.textContent = "rationale";
      det.appendChild(sum);
      const body = document.createElement("div");
      body.className = "coord-tool-row-rationale-body";
      body.textContent = verdict.reasoning;
      det.appendChild(body);
      // Insert immediately after the verdict line so the rationale
      // stays adjacent to the chip even when a result block has
      // already landed below — DOM order otherwise reads as
      // [call, verdict, result, rationale], which visually disconnects
      // the rationale from the chip it explains.
      line.insertAdjacentElement("afterend", det);
    }
    // Persist the verdict's tier on the row so the batch's header
    // tier badge can escalate from ⚙ heuristic → ⚖ llm when a later
    // intent_verdict lands an LLM verdict.  Default to "heuristic"
    // when tier is absent — heuristic verdicts ship without an
    // explicit tier marker on every server emitter.
    if (verdict) {
      row.dataset.verdictTier = verdict.tier || "heuristic";
      if (verdict.judge_model) {
        row.dataset.verdictModel = verdict.judge_model;
      } else {
        delete row.dataset.verdictModel;
      }
    } else {
      delete row.dataset.verdictTier;
      delete row.dataset.verdictModel;
    }
    _refreshBatchTier(row.closest(".coord-tool-batch"));
    return line;
  }

  function _appendJudgePendingLineTo(row) {
    const line = _appendVerdictLineTo(row, null);
    const chip = document.createElement("code");
    chip.className = "judging";
    const spin = document.createElement("span");
    spin.className = "spin";
    spin.setAttribute("aria-hidden", "true");
    chip.appendChild(spin);
    chip.appendChild(document.createTextNode("judge evaluating…"));
    line.appendChild(chip);
  }

  function _appendResultToRow(row, output, isError, opts) {
    if (!row) return;
    const existing = row.querySelector(".coord-tool-row-result");
    if (existing) existing.remove();
    // Re-fires (cancel + rerun, error + retry) clear any prior
    // truncation pill so it doesn't stack on the new result.
    const existingTrunc = row.querySelector(".coord-tool-truncated");
    if (existingTrunc) existingTrunc.remove();
    if (isError) {
      row.classList.add("error");
      // Lift the row's error onto the enclosing batch so the left
      // stripe + status pill (--error) cue the operator at the batch
      // level too.  Idempotent — re-fires don't stack.
      const batch = row.closest(".coord-tool-batch");
      if (batch) batch.classList.add("coord-tool-batch--error");
    }
    const block = document.createElement("div");
    block.className = "coord-tool-row-result";
    // Marker class so _refreshRowStatus can preserve the row's
    // .error state across upgrade-in-place when the error came from
    // a tool_result (not a static item.error).
    if (isError) block.classList.add("coord-tool-row-result--error");
    const lead = document.createElement("span");
    lead.className = "coord-tool-row-result-lead";
    lead.textContent = isError ? "✗ error: " : "↳ result: ";
    block.appendChild(lead);
    // Pretty-print JSON when the output parses; coord tool surfaces
    // (list_nodes, tasks, spawn_workstream, ...) emit JSON by default,
    // and a single-line dump is unreadable for a multi-key result.
    // The parent .coord-tool-row-result has white-space: pre-wrap, so
    // a textContent assignment with embedded \n preserves the
    // formatting without needing a nested <pre>.
    const cleaned = stripAnsi(output || "");
    const body = document.createElement("span");
    let pretty = cleaned;
    // Pretty-print JSON only when:
    //  - the payload looks like JSON (first non-space char is { or [),
    //    so we don't waste a parse on plain text or HTML, AND
    //  - it's small enough that the parse + restringify is cheap
    //    (cap at 32 KiB).  A 100 KiB tool output can deepen into a
    //    multi-MB object graph and stall the main thread for hundreds
    //    of ms; the parent CSS is white-space: pre-wrap so raw text
    //    still wraps and stays readable past the cap.
    const PRETTY_PRINT_CAP = 32 * 1024;
    if (cleaned && cleaned.length <= PRETTY_PRINT_CAP) {
      const head = cleaned.charCodeAt(0);
      // 0x7B = '{', 0x5B = '['  — leading whitespace would also fail
      // the cheap heuristic; that's intentional, the pretty-print is
      // a UX nice-to-have not a contract.
      if (head === 0x7b || head === 0x5b) {
        try {
          const parsed = JSON.parse(cleaned);
          if (parsed && typeof parsed === "object") {
            pretty = JSON.stringify(parsed, null, 2);
          }
        } catch (_) {
          /* not JSON — fall through to raw text */
        }
      }
    }
    body.textContent = pretty;
    block.appendChild(body);
    row.appendChild(block);
    // Storage-truncation indicator — sibling pill (not text inside
    // the result body) so renderers / parsers / copy-as-text paths
    // see the unmodified output.  Same convention as interactive's
    // .tool-output-truncated; styled by .coord-tool-truncated in
    // coordinator.css.
    if (opts && opts.truncated) {
      const pill = document.createElement("span");
      pill.className = "coord-tool-truncated";
      pill.textContent = "… truncated in storage";
      pill.title =
        "Full tool output was sent to the model live; only the first 10000 characters are persisted to the conversation row.";
      row.appendChild(pill);
    }
  }

  function _makeActionButton(label, role, kbdHint, ariaLabel) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "act";
    btn.dataset.role = role;
    btn.appendChild(document.createTextNode(label));
    if (kbdHint) {
      const kbd = document.createElement("span");
      kbd.className = "kbd";
      kbd.setAttribute("aria-hidden", "true");
      kbd.textContent = kbdHint;
      btn.appendChild(kbd);
    }
    if (ariaLabel) btn.setAttribute("aria-label", ariaLabel);
    return btn;
  }

  // Concise, screen-reader friendly summary of a pending batch — used
  // both as the .coord-tool-batch[role=region] aria-label and as the
  // text fed to the off-screen assertive announcer when the gate
  // appears.  "Approval required: spawn_workstream + 9 more" reads
  // cleanly through a SR without revealing every nested arg.
  function _approvalAriaLabel(items) {
    const first =
      (items && items[0] && (items[0].func_name || items[0].approval_label)) ||
      "tool";
    const rest = items.length > 1 ? " + " + (items.length - 1) + " more" : "";
    return "Approval required: " + first + rest;
  }

  // Header kicker text for a pending batch.  Both the upgrade-in-place
  // and fresh-build paths in appendToolBatch render this; pulling the
  // string into one place keeps the two paths from drifting on a
  // future label tweak.
  function _pendingKickerText(items) {
    return items.length >= 2
      ? "⚠ Approval · Parallel " + items.length
      : "⚠ Approval";
  }

  function _buildBatchActions(batch, items) {
    const actions = document.createElement("div");
    actions.className = "coord-tool-actions";
    const spacer = document.createElement("div");
    spacer.className = "spacer";
    actions.appendChild(spacer);

    const denyBtn = _makeActionButton("Deny", "deny", "D", "Deny (D)");
    denyBtn.classList.add("danger");
    denyBtn.addEventListener("click", () =>
      _resolveBatchAction(batch, false, false),
    );
    actions.appendChild(denyBtn);

    const alwaysNames = items
      .filter(
        (it) =>
          it.needs_approval &&
          it.func_name &&
          it.func_name !== "__budget_override__" &&
          !it.error,
      )
      .map((it) => it.approval_label || it.func_name);
    if (alwaysNames.length > 0) {
      const ariaAlways = "Always approve " + alwaysNames.join(", ");
      const alwaysBtn = _makeActionButton("Always", "always", "⇧A", ariaAlways);
      alwaysBtn.classList.add("always");
      alwaysBtn.title = ariaAlways;
      alwaysBtn.addEventListener("click", () =>
        _resolveBatchAction(batch, true, true),
      );
      actions.appendChild(alwaysBtn);
    }

    const approveBtn = _makeActionButton(
      "Approve",
      "approve",
      "⏎",
      "Approve (Enter)",
    );
    approveBtn.classList.add("primary");
    approveBtn.addEventListener("click", () =>
      _resolveBatchAction(batch, true, false),
    );
    actions.appendChild(approveBtn);

    return actions;
  }

  async function _resolveBatchAction(batch, approved, always) {
    // Pick a call_id from a row that's actually in the server's
    // pending_items (data-needs-approval="1").  approve_request
    // envelopes carry the FULL items list including auto-approved /
    // policy-blocked siblings; resolving against one of those would
    // 409 since the server's pending_items wouldn't recognise it.
    const pendingRow = batch.querySelector(
      '.coord-tool-row[data-needs-approval="1"][data-call-id]',
    );
    const callId = pendingRow && pendingRow.dataset.callId;
    if (!callId) return;
    _setBatchActionsDisabled(batch, true);
    // Stash the "always" intent on the batch as a backward-compat
    // fallback for the approval_resolved SSE handler.  Server now
    // echoes `always` on the resolved event (post-PR-447) so peer
    // tabs render the right status pill in cross-tab scenarios; the
    // dataset is only consulted during a hot-deploy window where the
    // SSE event might briefly omit the field.  Also echoes back to
    // the operator that the click landed.
    batch.dataset.requestedAlways = always ? "1" : "";
    try {
      const resp = await approveWorkstream(wsId, {
        approved: !!approved,
        always: !!always,
        call_id: callId,
      });
      if (!resp.ok) throw new Error("approve failed: HTTP " + resp.status);
    } catch (e) {
      _setBatchActionsDisabled(batch, false);
      delete batch.dataset.requestedAlways;
      if (typeof toast !== "undefined" && toast.error) toast.error(String(e));
      else console.error(e);
      return;
    }
    // approval_resolved SSE event will morph the batch authoritatively.
  }

  function _setBatchActionsDisabled(batch, disabled) {
    batch.querySelectorAll(".coord-tool-actions button").forEach((b) => {
      b.disabled = !!disabled;
    });
  }

  // Build the resolved-state status pill.  Shared between the live
  // _morphBatchResolved path (post-approve, post-deny) and the
  // history-replay path inside appendToolBatch (renders resolved
  // batches without ever showing actions).
  function _buildStatusPill(opts) {
    const status = document.createElement("div");
    status.className = "coord-tool-status";
    status.classList.add(
      opts.approved
        ? "coord-tool-status--approved"
        : "coord-tool-status--denied",
    );
    const label = document.createElement("span");
    if (opts.approved) {
      label.textContent = opts.always ? "✓ approved · always" : "✓ approved";
    } else {
      label.textContent = "✗ denied";
    }
    status.appendChild(label);
    if (opts.feedback) {
      const fb = document.createElement("span");
      fb.className = "coord-tool-status-feedback";
      fb.textContent = "— " + opts.feedback;
      status.appendChild(fb);
    }
    return status;
  }

  function _morphBatchResolved(batch, opts) {
    if (!batch) return;
    batch.classList.remove("coord-tool-batch--pending");
    batch.classList.add(
      opts.approved ? "coord-tool-batch--approved" : "coord-tool-batch--denied",
    );
    // Drop the [role=region] approval landmark so the resolved batch
    // stops claiming "Approval required" in SR landmark navigation.
    batch.removeAttribute("role");
    batch.removeAttribute("aria-label");
    const actions = batch.querySelector(".coord-tool-actions");
    if (actions) actions.replaceWith(_buildStatusPill(opts));
    if (activeBatch === batch) activeBatch = null;
  }

  function _focusBatchPrimary(batch, prefer) {
    if (!batch) return;
    const role = prefer === "deny" ? "deny" : "approve";
    const btn = batch.querySelector(
      '.coord-tool-actions button[data-role="' + role + '"]',
    );
    if (btn) {
      try {
        btn.focus({ preventScroll: false });
      } catch (_) {
        /* noop */
      }
    }
  }

  // Build (or update) a batch construct for `items`.  Idempotent on
  // SSE reconnect: when every item's call_id already has a row in the
  // DOM, returns the existing batch + folds in any newly-cached
  // verdicts (and upgrades the batch's state class when SSE arrives
  // with a more specific state than history replay used).
  //
  // opts (mutually exclusive states):
  //   pending (bool)       — show approval action row, mark --pending
  //   auto (bool)          — mark --auto (no actions; auto-approved)
  //   running (bool)       — mark --running (no actions; replay-time
  //                          orphan with no result yet — could be
  //                          pending OR auto-approved + in-flight,
  //                          ambiguous until SSE clarifies)
  //   resolved ({approved,denied,feedback,always}) — historical
  //                          resolved batch (status pill prefilled)
  //   judgePending (bool)  — show "judge evaluating…" placeholders
  //                          on rows with needs_approval=true (only
  //                          meaningful with pending)
  function appendToolBatch(items, opts) {
    items = (items || []).filter(Boolean);
    if (items.length === 0) return null;
    opts = opts || {};

    const allMapped = items.every(
      (it) => it.call_id && toolRows.has(it.call_id),
    );
    // Partial-overlap guard.  If only SOME of the incoming call_ids
    // are mapped (i.e. they belong to a different prior batch),
    // overwriting toolRows in the create-new path below would orphan
    // those prior rows — they'd stay in their old batch's DOM but
    // tool_result / intent_verdict events for them would route into
    // the new batch.  This shape doesn't occur in normal operation
    // (the server never sends overlapping envelopes), so we log it +
    // unmap the stale entries before the new batch claims them.
    if (!allMapped) {
      const partial = items.filter(
        (it) => it.call_id && toolRows.has(it.call_id),
      );
      if (partial.length > 0) {
        console.warn(
          "coord_ui: partial-overlap envelope — unmapping",
          partial.length,
          "stale call_ids before new batch claims them",
        );
        partial.forEach((it) => toolRows.delete(it.call_id));
      }
    }
    if (allMapped) {
      const existing = toolRows.get(items[0].call_id).batch;
      // Upgrade-in-place: when SSE arrives with a more specific state
      // than the placeholder history replay rendered, morph the
      // existing shell instead of leaving stale chrome.  The two real
      // upgrade transitions:
      //   --running → --pending  (SSE approve_request fires for an
      //                            orphan turn that was actually
      //                            awaiting approval at reload)
      //   --running → --auto     (SSE tool_info fires for an orphan
      //                            turn that was actually auto-
      //                            approved + in-flight at reload)
      if (
        opts.pending &&
        !existing.classList.contains("coord-tool-batch--pending")
      ) {
        existing.classList.remove(
          "coord-tool-batch--approved",
          "coord-tool-batch--denied",
          "coord-tool-batch--auto",
          "coord-tool-batch--running",
          "coord-tool-batch--error",
        );
        existing.classList.add("coord-tool-batch--pending");
        existing.setAttribute("role", "region");
        existing.setAttribute("aria-label", _approvalAriaLabel(items));
        const kicker = existing.querySelector(".coord-tool-batch-kicker");
        if (kicker) {
          kicker.textContent = _pendingKickerText(items);
        }
        const statusEl = existing.querySelector(".coord-tool-status");
        const actionsEl = existing.querySelector(".coord-tool-actions");
        const newActions = _buildBatchActions(existing, items);
        if (statusEl) statusEl.replaceWith(newActions);
        else if (actionsEl) actionsEl.replaceWith(newActions);
        else existing.appendChild(newActions);
        activeBatch = existing;
        _announceAssertive(_approvalAriaLabel(items));
      } else if (
        opts.auto &&
        existing.classList.contains("coord-tool-batch--running")
      ) {
        existing.classList.remove("coord-tool-batch--running");
        existing.classList.add("coord-tool-batch--auto");
      } else if (opts.pending) {
        // Already pending — keep the action row, just refresh
        // activeBatch so kb shortcut + approval_resolved routing
        // target the right construct.  Don't re-announce; SR already
        // heard about this gate.
        activeBatch = existing;
      }
      items.forEach((it) => {
        const entry = toolRows.get(it.call_id);
        if (!entry) return;
        // Refresh per-row status from the authoritative SSE item:
        // clears any stale data-needs-approval / status pills the
        // earlier shell rendered (e.g. replay-time orphan rows had
        // no auto/error info; SSE tool_info / approve_request items
        // do).  Preserves runtime tool_result errors via the
        // .coord-tool-row-result--error marker.
        _refreshRowStatus(entry.row, it);
        const cached = judgeVerdicts.get(it.call_id);
        const v = it.judge_verdict || it.heuristic_verdict || cached;
        if (v) {
          _appendVerdictLineTo(entry.row, v);
        } else if (
          it.needs_approval &&
          opts.judgePending &&
          !entry.row.querySelector(".coord-tool-row-verdict")
        ) {
          _appendJudgePendingLineTo(entry.row);
        }
      });
      return existing;
    }

    const batch = document.createElement("div");
    batch.className = "coord-tool-batch";
    batch.classList.add(
      items.length >= 2
        ? "coord-tool-batch--parallel"
        : "coord-tool-batch--solo",
    );
    if (opts.pending) batch.classList.add("coord-tool-batch--pending");
    else if (opts.auto) batch.classList.add("coord-tool-batch--auto");
    else if (opts.running) batch.classList.add("coord-tool-batch--running");
    else if (opts.resolved) {
      batch.classList.add(
        opts.resolved.approved
          ? "coord-tool-batch--approved"
          : "coord-tool-batch--denied",
      );
    }

    const head = document.createElement("div");
    head.className = "coord-tool-batch-head";
    const kicker = document.createElement("span");
    kicker.className = "coord-tool-batch-kicker";
    if (opts.pending) {
      kicker.textContent = _pendingKickerText(items);
    } else if (opts.running) {
      kicker.textContent =
        items.length >= 2 ? "Running · Parallel " + items.length : "Running";
    } else if (items.length >= 2) {
      kicker.textContent = "Parallel · " + items.length + " tools";
    } else {
      kicker.textContent = "Tool";
    }
    head.appendChild(kicker);

    const summary = document.createElement("span");
    summary.className = "coord-tool-batch-summary";
    const firstName =
      items[0] && (items[0].func_name || items[0].approval_label)
        ? items[0].func_name || items[0].approval_label
        : "tool";
    summary.textContent =
      items.length >= 2
        ? firstName + " + " + (items.length - 1) + " more"
        : firstName;
    head.appendChild(summary);

    const tier = _pickBatchTier(items);
    if (tier) {
      const tierEl = document.createElement("span");
      tierEl.className = "coord-tool-batch-tier";
      tierEl.textContent = tier;
      head.appendChild(tierEl);
    }
    batch.appendChild(head);

    let anyRowError = false;
    const renderedRows = [];
    items.forEach((it, idx) => {
      const indexLabel = items.length >= 2 ? idx + 1 + "/" + items.length : "";
      const row = _renderBatchRow(it, indexLabel);
      batch.appendChild(row);
      renderedRows.push(row);
      if (it.call_id) {
        toolRows.set(it.call_id, { batch, row });
      }
      if (row.classList.contains("error")) anyRowError = true;
      const cached = it.call_id ? judgeVerdicts.get(it.call_id) : null;
      const verdict = it.judge_verdict || it.heuristic_verdict || cached;
      if (verdict) {
        _appendVerdictLineTo(row, verdict);
      } else if (it.needs_approval && opts.judgePending) {
        _appendJudgePendingLineTo(row);
      }
    });
    // Tuck the parallel rail 4px in from the first / last row's
    // top + bottom edges via class markers (CSS :first-of-type would
    // miss because the batch has other div siblings — head + actions
    // — coming before/after the row group).
    if (renderedRows.length > 0) {
      renderedRows[0].classList.add("coord-tool-row--first");
      renderedRows[renderedRows.length - 1].classList.add(
        "coord-tool-row--last",
      );
    }
    // Lift any policy-blocked row's error onto the enclosing batch so
    // the left stripe + status pill cue the operator at the batch
    // level too.  _appendResultToRow does the same for runtime errors
    // arriving via tool_result.
    if (anyRowError) batch.classList.add("coord-tool-batch--error");

    if (opts.pending) {
      batch.appendChild(_buildBatchActions(batch, items));
      // Mark the construct as a navigable landmark for SR users +
      // route the action-required announcement through the dedicated
      // assertive live region (the chat log itself is polite and
      // gets muted during streaming).
      batch.setAttribute("role", "region");
      batch.setAttribute("aria-label", _approvalAriaLabel(items));
      activeBatch = batch;
      _announceAssertive(_approvalAriaLabel(items));
    } else if (opts.resolved) {
      batch.appendChild(_buildStatusPill(opts.resolved));
    }

    messagesEl.appendChild(batch);
    _scheduleScroll();
    return batch;
  }

  // ------------------------------------------------------------------
  // Content streaming
  // ------------------------------------------------------------------

  let currentAssistantEl = null;
  let currentAssistantBuf = "";
  let currentReasoningEl = null;
  let currentReasoningBuf = "";

  function appendContentToken(text) {
    if (!currentAssistantEl) {
      currentAssistantEl = appendMsg("assistant", "", { label: "assistant" });
      currentAssistantBuf = "";
      // Mute the live region while tokens stream in so screen readers
      // don't re-announce the full buffer on every delta.  Restored on
      // stream_end.
      messagesEl.setAttribute("aria-live", "off");
    }
    currentAssistantBuf += text;
    // Re-render the buffer through the shared streaming helper on every
    // token so the user sees live-formatted markdown instead of a final
    // "pop" on stream_end.  Heavy post-processing (syntax highlighting,
    // mermaid, KaTeX) stays deferred to streamingRenderFinalize below.
    const body = currentAssistantEl.querySelector(".msg-body");
    if (body && typeof streamingRender === "function") {
      try {
        streamingRender(body, currentAssistantBuf);
      } catch (e) {
        console.warn("coordinator streamingRender failed", e);
        body.textContent = currentAssistantBuf;
      }
    } else if (body) {
      body.textContent = currentAssistantBuf;
    }
    _scheduleScroll();
  }

  function appendReasoningToken(text) {
    // Reasoning tokens arrive ahead of assistant content when the
    // coordinator model has reasoning_effort > none.  Rendering them in
    // a dimmed "role-reasoning" bubble avoids the "UI is hung" impression
    // of a silent delay.  A separate element means reasoning and content
    // don't mix in the main assistant buffer.
    if (!currentReasoningEl) {
      currentReasoningEl = appendMsg("reasoning", "", { label: "reasoning" });
      currentReasoningBuf = "";
      messagesEl.setAttribute("aria-live", "off");
    }
    currentReasoningBuf += text;
    const body = currentReasoningEl.querySelector(".msg-body");
    if (body) body.textContent = currentReasoningBuf;
    _scheduleScroll();
  }

  function finishAssistantStream() {
    // Finalize the streamed buffer through the shared helper — this also
    // runs postRenderMarkdown (syntax highlighting, mermaid, KaTeX) once
    // all tokens have arrived.  renderMarkdown escapes HTML internally so
    // the innerHTML assignment inside the helper is XSS-safe as long as
    // renderer.js is trusted — same contract as ui/static/app.js.
    if (currentAssistantEl && currentAssistantBuf) {
      const body = currentAssistantEl.querySelector(".msg-body");
      if (body && typeof streamingRenderFinalize === "function") {
        try {
          streamingRenderFinalize(body, currentAssistantBuf);
        } catch (e) {
          console.warn("coordinator streamingRenderFinalize failed", e);
        }
      }
    }
    currentAssistantEl = null;
    currentAssistantBuf = "";
    currentReasoningEl = null;
    currentReasoningBuf = "";
    messagesEl.setAttribute("aria-live", "polite");
  }

  // ------------------------------------------------------------------
  // Approval UI — entry point used by the approve_request SSE handler.
  // The legacy dock + its public surface (hideApproval / coordApprove)
  // were removed when approvals moved into the inline batch construct.
  // The SSE approval_resolved handler now drives _morphBatchResolved
  // directly, and inline action buttons drive _resolveBatchAction.
  // ------------------------------------------------------------------

  function showApproval(items, judgePending) {
    const list = (items || []).filter(Boolean);
    if (list.length === 0) return;
    const batch = appendToolBatch(list, {
      pending: true,
      judgePending: !!judgePending,
    });
    const firstPending = list.find((it) => it.needs_approval);
    if (batch && firstPending && firstPending.call_id) {
      const cached = judgeVerdicts.get(firstPending.call_id);
      if (cached) _focusBatchPrimary(batch, cached.recommendation);
    }
  }

  // Generic approve POST — usable for both the coord-self batch and
  // the per-child inline buttons in the children-tree.  Returns the
  // response so callers can inspect 409 (stale call_id) bodies and
  // refresh their local state.
  //
  // The path differs by target: the coord workstream is hosted on the
  // console process itself (lifted verbs at /v1/api/workstreams/
  // {coord_ws_id}/approve), but child workstreams live on cluster
  // nodes and need to round-trip through the routing proxy at
  // /v1/api/route/workstreams/{child_ws_id}/approve which resolves the
  // ws_id to its owning node and forwards the body verbatim.  Without
  // the /route/ prefix children always 404 because the console
  // doesn't host them.
  async function approveWorkstream(targetWsId, body) {
    const isSelf = targetWsId === wsId;
    const path = isSelf
      ? "/v1/api/workstreams/" + encodeURIComponent(targetWsId) + "/approve"
      : "/v1/api/route/workstreams/" +
        encodeURIComponent(targetWsId) +
        "/approve";
    return postJSON(path, body);
  }

  // ------------------------------------------------------------------
  // Send / cancel / close
  // ------------------------------------------------------------------

  // Busy reflects whether the worker is mid-turn. SSE state_change
  // events drive it (running/thinking/attention → busy; idle/error →
  // idle) so a server-side transition the user didn't initiate
  // (another tab, judge reset) still keeps the composer in sync.
  //
  // composer.setBusy runs unconditionally so the Stop button label /
  // dataset.forceCancel / placeholder stay canonical even on a
  // redundant call — that idempotent reset is the contract any future
  // caller relies on. queue.onIdleEdge runs only on the actual edge
  // (it's the heavier work — querySelectorAll-driven promote sweep
  // plus the cancel-timer cleanup wired via the onIdle hook above).
  function setBusy(b) {
    const next = !!b;
    composer.setBusy(next);
    const edge = next !== busy;
    busy = next;
    if (edge && !next) queue.onIdleEdge();
  }

  // Update the four-cell status bar from an on_status SSE event.
  // Delegates formatting to the shared StatusBar.paint helper
  // (shared_static/status_bar.js) so the interactive pane and this
  // dashboard render identical thresholds + suffix rules.
  function updateStatusBar(evt) {
    if (!evt) return;
    StatusBar.paint(
      {
        rootEl: statusBarEl,
        modelEl: sbModelEl,
        tokensEl: sbTokensEl,
        toolsEl: sbToolsEl,
        turnsEl: sbTurnsEl,
      },
      evt,
      { alias: coordModelAlias, model: coordModel },
    );
    lastStatusEvt = evt;
  }

  window.coordSend = function () {
    const text = composer.value;
    const trimmed = (text || "").trim();
    if (!trimmed) return false;

    const snap = attachments.snapshot();

    let queuedEl = null;
    if (busy) {
      // Server re-parses the !!! prefix to set queue priority — the
      // optimistic bubble strips it for display.
      let displayText = trimmed;
      let priority = "notice";
      if (trimmed.startsWith("!!!")) {
        displayText = trimmed.slice(3).trimStart();
        priority = "important";
      }
      queuedEl = queue.addQueuedMessage(displayText, priority);
    } else {
      setBusy(true);
      appendText("user", trimmed, { label: "you" });
    }
    composer.clear();

    authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: trimmed,
        attachment_ids: snap.attachment_ids,
      }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (data && data.status === "queued" && data.msg_id) {
          // Race: server returned queued but the client thought it was
          // idle (SSE state_change hadn't arrived yet on initial load /
          // reconnect). The optimistic user bubble is already in the
          // log; we can't bind msg_id to a queued bubble retroactively
          // without flipping the visual state mid-stream. Flip the busy
          // flag so any subsequent send takes the queue path correctly,
          // and accept the small UX gap (no in-UI dismiss for THIS
          // message). The server still delivers it on worker drain.
          if (queuedEl) queue.bind(queuedEl, data.msg_id);
          else setBusy(true);
          attachments.consume(data.attached_ids, data.dropped_attachment_ids);
        } else if (data && data.status === "busy") {
          if (queuedEl) queue.remove(queuedEl);
          appendText("error", "Server is busy. Please wait.", {
            label: "error",
          });
          if (!queuedEl) setBusy(false);
        } else if (data && data.status === "queue_full") {
          if (queuedEl) queue.remove(queuedEl);
          appendText("error", "Message queue full. Please wait.", {
            label: "error",
          });
        } else {
          attachments.consume(
            data && data.attached_ids,
            data && data.dropped_attachment_ids,
          );
        }
      })
      .catch((e) => {
        if (queuedEl) queue.remove(queuedEl);
        appendText(
          "error",
          "Connection error: " + (e && e.message ? e.message : e),
          { label: "error" },
        );
        if (!queuedEl) setBusy(false);
      });
    return false;
  };

  function cancelGeneration() {
    if (!busy || stopBtn.disabled) return;
    const force = stopBtn.dataset.forceCancel === "true";
    stopBtn.disabled = true;
    authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/cancel", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: force }),
    })
      .then(() => {
        if (force) {
          // Force cancel abandons the worker thread server-side; the
          // SSE state_change → idle may not arrive (the thread may be
          // stuck past the cancel checkpoint), so transition the UI
          // directly. setBusy(false) clears cancel/force timers and
          // composer.setBusy(false) resets the Stop button label,
          // aria-label, and dataset.forceCancel — so the next turn
          // starts in graceful-cancel mode without a stale Force Stop
          // primed for the first click.
          appendText("info", "Force stopped. Previous generation abandoned.", {
            label: "info",
          });
          setBusy(false);
        }
      })
      .catch((e) => {
        appendText(
          "error",
          "Cancel error: " + (e && e.message ? e.message : e),
          { label: "error" },
        );
        // Re-enable so the user can retry.
        if (busy) stopBtn.disabled = false;
      });
  }

  window.coordCloseSession = async function () {
    if (
      !window.confirm(
        "End this coordinator session? The server will terminate it.",
      )
    )
      return;
    // Suspend SSE reconnect first — the moment the server pops the ws
    // from coord_mgr the next reconnect would 404 and surface a stream
    // error toast right before the redirect, which reads as "the end
    // button broke" even though the close succeeded. On any failure
    // path we MUST resume SSE before returning so the user isn't left
    // staring at a stale page disconnected from a still-alive session.
    const resumeSse = () => {
      try {
        connectSSE();
      } catch (_) {
        /* connectSSE schedules its own reconnect on failure */
      }
    };
    try {
      if (evtSource) evtSource.close();
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    } catch (_) {
      /* best-effort suspension */
    }
    let resp;
    try {
      resp = await postJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/close",
        {},
      );
    } catch (e) {
      // authFetch throws Error("auth") and shows the login modal on
      // 401; other network failures land here too. Surface the cause
      // visibly — silent toast.error wasn't enough for operators
      // troubleshooting a stuck end-button.
      const msg =
        e && e.message === "auth"
          ? "Sign-in required to end this session."
          : "Close request failed: " + (e && e.message ? e.message : e);
      if (typeof toast !== "undefined" && toast.error) toast.error(msg);
      else window.alert(msg);
      resumeSse();
      return;
    }
    if (!resp.ok) {
      let detail = "HTTP " + resp.status;
      try {
        const body = await resp.json();
        if (body && body.error) detail += " — " + body.error;
      } catch (_) {
        /* non-JSON body — fall back to status code */
      }
      const msg = "Could not end session: " + detail;
      if (typeof toast !== "undefined" && toast.error) toast.error(msg);
      else window.alert(msg);
      resumeSse();
      return;
    }
    window.location.href = "/";
  };

  // ------------------------------------------------------------------
  // HTTP helpers
  // ------------------------------------------------------------------

  function postJSON(url, body) {
    const fn = typeof authFetch === "function" ? authFetch : fetch;
    return fn(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  function getJSON(url) {
    const fn = typeof authFetch === "function" ? authFetch : fetch;
    return fn(url, { credentials: "include" }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // ------------------------------------------------------------------
  // SSE connection with reconnect
  // ------------------------------------------------------------------

  function setSseStatus(text, cls) {
    // Prepend a leading glyph so the state isn't conveyed by colour
    // alone (WCAG 1.4.1).  ● connected, ○ connecting, ⚠ disconnected.
    const glyph = cls === "ok" ? "● " : cls === "err" ? "⚠ " : "○ ";
    sseEl.textContent = glyph + text;
    // Keep .appbar-status (DS: mono 11px --ink-3) as the base; layer the
    // semantic colour via a data-state attribute so the glyph-prefixed
    // label remains high-contrast while the text colour tracks OK / ERR.
    sseEl.className = "appbar-status";
    sseEl.dataset.state = cls || "";
    if (cls === "ok") {
      sseEl.style.color = "var(--ok)";
    } else if (cls === "err") {
      sseEl.style.color = "var(--err)";
    } else {
      sseEl.style.color = "";
    }
  }

  function connectSSE() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (evtSource) {
      try {
        evtSource.close();
      } catch (_) {
        /* noop */
      }
    }
    setSseStatus("connecting…", "");
    // Snapshot whether this is a reconnect BEFORE resetting
    // reconnectAttempts in onopen — child_ws_* events dispatched while
    // we were disconnected aren't replayed by the events SSE handler,
    // so the client has to pull authoritative state after any gap.
    const wasReconnecting = reconnectAttempts > 0;
    const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/events";
    evtSource = new EventSource(url, { withCredentials: true });
    evtSource.onopen = function () {
      reconnectAttempts = 0;
      setSseStatus("live", "ok");
      // Lift the disconnected dim treatment + restore the last known
      // counters; the replay phase will overwrite with authoritative
      // server-side values on the next yields.  When no prior status
      // event has been seen (a fresh coord that disconnected before
      // its first turn), the onerror branch wrote "Reconnecting…"
      // into the tokens cell — reset to the placeholder so the dim
      // copy doesn't persist past a successful reconnect when the
      // session never produces a status tick.
      statusBarEl.classList.remove("ws-sb-disconnected");
      if (lastStatusEvt) updateStatusBar(lastStatusEvt);
      else StatusBar.resetTokensPlaceholder(sbTokensEl);
      if (wasReconnecting) {
        // Replace-mode refresh: the server is authoritative after a
        // gap; any SSE-only rows the client accumulated before
        // disconnect are stale.
        loadChildren({ replace: true });
        loadTasks();
        // Drop any in-flight wait entries — a wait_ended dropped
        // during the SSE gap would otherwise pin the header badge
        // forever.  The server's SSE replay doesn't cover our
        // per-call wait_* events, so we clear and let fresh events
        // repopulate.  #bug-4.  Both ``activeWaits`` and
        // ``_renderWaitIndicator`` are defined below in the same
        // IIFE — hoisted function decl + const-in-outer-scope — so
        // they're always reachable by the time onopen fires.
        activeWaits.clear();
        _renderWaitIndicator();
        // Drop the live-badge cache too — entries within the 5s TTL
        // can carry stale pending_approval_detail (the child may
        // have resolved its approval during the SSE gap).  Without
        // this clear, inline approve/deny buttons could render on
        // a row whose approval was resolved elsewhere; the next
        // scheduleLiveFetch from loadChildren's finally branch
        // (which fires for every visible row) repopulates with
        // authoritative state. Preserve `permanent: true` entries
        // (set on 403/404 — denied by permission/identity, not by
        // state) so a user lacking admin.cluster.inspect doesn't
        // pay one 403 per denied id on every reconnect.
        for (const [id, c] of liveBadgeCache) {
          if (!c || !c.permanent) _liveBadgeCacheDelete(id);
        }
      }
    };
    evtSource.onerror = function () {
      setSseStatus("disconnected", "err");
      // Dim the status bar so a stale reading doesn't read as live.
      statusBarEl.classList.add("ws-sb-disconnected");
      sbTokensEl.textContent = "Reconnecting…";
      try {
        evtSource.close();
      } catch (_) {
        /* noop */
      }
      // Probe the authed detail endpoint to distinguish an expired
      // session (401) from a transient network error.  On 401, prompt
      // for login via the shared auth.js overlay instead of spinning
      // in backoff forever — match the console / server-UI pattern.
      // On any other outcome, fall through to the normal reconnect
      // schedule.
      var probe = typeof authFetch === "function" ? authFetch : fetch;
      probe("/v1/api/workstreams/" + encodeURIComponent(wsId))
        .then(function (r) {
          if (r.status === 401 && typeof showLogin === "function") {
            showLogin("Session expired. Please sign in to reconnect.");
            return;
          }
          scheduleReconnect();
        })
        .catch(function () {
          scheduleReconnect();
        });
    };
    evtSource.onmessage = function (event) {
      let data = null;
      try {
        data = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      handleEvent(data);
    };
  }

  function scheduleReconnect() {
    const base = Math.min(30000, 1000 * Math.pow(2, reconnectAttempts));
    const jitter = Math.floor(Math.random() * 500);
    reconnectAttempts += 1;
    reconnectTimer = setTimeout(connectSSE, base + jitter);
  }

  // ------------------------------------------------------------------
  // SSE event router
  // ------------------------------------------------------------------

  function handleEvent(ev) {
    switch (ev.type) {
      case "content":
        appendContentToken(ev.text || "");
        break;
      case "reasoning":
        appendReasoningToken(ev.text || "");
        break;
      case "stream_end":
        finishAssistantStream();
        break;
      case "tool_result":
        appendToolResult(
          ev.name || "tool",
          ev.call_id || "",
          ev.output || "",
          !!ev.is_error,
        );
        // tasks mutations change persisted state the sidebar reads
        // from GET /tasks — re-fetch so the operator sees
        // add/update/remove/reorder without clicking the refresh icon.
        // list is a read-only action; skip to avoid redundant fetches.
        // Debounced so a burst of mutations coalesces into one fetch.
        if (ev.name === "tasks" && !ev.is_error) {
          loadTasksDebounced();
        }
        break;
      case "approve_request":
        // appendToolBatch is idempotent on call_ids — the console replays
        // _pending_approval into every new SSE subscriber, so reconnect
        // won't double-render the construct.
        showApproval(ev.items, !!ev.judge_pending);
        break;
      case "approval_resolved": {
        // Server-driven resolution.  Morph the active pending batch
        // (the construct that posted the approve POST).  The server
        // event carries `approved` + `feedback` only — the "always"
        // intent.  Server now echoes ``always`` on the SSE payload
        // (post-PR-447) so cross-tab resolution renders the right
        // status pill on every subscribed tab — not just the one
        // that clicked.  Fall back to this tab's stashed dataset
        // flag for backward compat with a server hot-deploy where
        // the SSE event might briefly omit the field.
        // Fall back to a DOM lookup if activeBatch was never set
        // (e.g. cross-tab resolution where this tab never rendered
        // the approval gate before the resolved event landed).
        const target =
          activeBatch ||
          messagesEl.querySelector(
            ".coord-tool-batch.coord-tool-batch--pending",
          );
        if (target) {
          const wasAlways =
            ev.always === true ||
            (ev.always === undefined && target.dataset.requestedAlways === "1");
          _morphBatchResolved(target, {
            approved: ev.approved !== false,
            always: wasAlways,
            feedback: ev.feedback || null,
          });
        }
        break;
      }
      case "intent_verdict":
        // Cache the verdict so a late-arriving approve_request (or a
        // SSE replay reorder) still surfaces it.  _cacheJudgeVerdict
        // soft-caps the Map to bound long-session memory growth.
        // intent_verdict is the LLM judge's signal by definition, so
        // tag the cache entry as tier="llm" — _refreshBatchTier reads
        // this off the row dataset to escalate the header badge from
        // ⚙ heuristic → ⚖ llm when the late verdict lands.
        if (ev.call_id) {
          _cacheJudgeVerdict(ev.call_id, {
            recommendation: ev.recommendation,
            risk_level: ev.risk_level,
            confidence: ev.confidence,
            reasoning: ev.reasoning,
            tier: ev.tier || "llm",
            judge_model: ev.judge_model || "",
          });
          const entry = toolRows.get(ev.call_id);
          if (entry) {
            _appendVerdictLineTo(entry.row, judgeVerdicts.get(ev.call_id));
            // Focus the construct's primary action once the verdict
            // gives the reviewer context to act on — judge=deny defaults
            // focus to Deny, otherwise Approve.
            if (entry.batch.classList.contains("coord-tool-batch--pending")) {
              _focusBatchPrimary(entry.batch, ev.recommendation);
            }
            break;
          }
        }
        // Fallback — no matching pending row (approval already resolved
        // or call_id missing).  Surface as a chat message so the verdict
        // isn't silently dropped.
        appendText(
          "tool",
          "[judge] " +
            (ev.recommendation || "?") +
            " (risk=" +
            (ev.risk_level || "?") +
            ")",
          { label: "judge" },
        );
        break;
      case "output_warning":
        // Anchor the finding to the specific .coord-tool-row that
        // tripped the guard so the operator reads call → finding
        // adjacency on both live and replay surfaces.  Falls back
        // to a chat line only when the call_id no longer maps to a
        // row (e.g. event arrived after the row was evicted).
        if (ev.call_id && toolRows.has(ev.call_id)) {
          _attachOutputWarningChip(toolRows.get(ev.call_id).row, {
            risk_level: ev.risk_level,
            flags: ev.flags,
            redacted: ev.redacted,
          });
        } else {
          appendText(
            "info",
            "[output guard] " +
              (ev.risk_level || "?") +
              ": " +
              (ev.flags || []).join(","),
            { label: "warning" },
          );
        }
        break;
      case "error":
        appendText("error", ev.message || "(unknown error)", {
          label: "error",
        });
        break;
      case "info":
        // .msg.info (think-indigo) is the intended variant for info
        // events; prior routing to "tool" gave them accent-tinted tool
        // styling which mis-categorised them as tool calls.
        appendText("info", ev.message || "", { label: "info" });
        break;
      case "user_reminder":
        // Metacognitive user-channel nudge — render below the most
        // recent user message as a yellow themed bubble.  Same shape
        // as the interactive UI's case.
        if (Array.isArray(ev.reminders) && ev.reminders.length) {
          appendUserReminderLive(ev.reminders);
        }
        break;
      case "tool_reminder":
        // Metacognitive tool-channel nudge — render below the
        // .coord-tool-batch that produced the tool result identified
        // by ev.tool_call_id.
        if (Array.isArray(ev.reminders) && ev.reminders.length) {
          appendToolReminderLive(ev.reminders, ev.tool_call_id || "");
        }
        break;
      case "connected":
        // First yield from _coord_events_replay — populates the
        // status bar's model cell before any history arrives.  Also
        // re-fires on every SSE reconnect because the replay phase
        // runs unconditionally on subscribe.
        coordModel = ev.model || "";
        coordModelAlias = ev.model_alias || ev.model || "";
        sbModelEl.textContent = coordModelAlias || coordModel || "—";
        sbModelEl.title = coordModel || "";
        break;
      case "status":
        // Live token / context / tool / turn counters.  Replayed once
        // on reconnect when last_usage is available, then ticked by
        // SessionUI.on_status on every turn.
        updateStatusBar(ev);
        break;
      case "state_change":
        statusEl.textContent = ev.state || "";
        // Drive the composer's busy state from the canonical
        // server-side workstream state so the Stop button + queue
        // mode follow whatever the worker is doing — including
        // transitions we didn't initiate (cross-tab cancel, judge
        // reset, idle-after-error). Mirrors the interactive pane.
        if (ev.state === "idle" || ev.state === "error") {
          setBusy(false);
        } else if (
          ev.state === "running" ||
          ev.state === "thinking" ||
          ev.state === "attention"
        ) {
          setBusy(true);
        }
        break;
      case "rename":
        nameEl.textContent = ev.name || "";
        break;
      case "message_queued":
        // Server confirms the queued slot — the optimistic bubble
        // already showed it; nothing to render here. (Earlier this
        // surfaced an extra info row, which doubled up with the
        // queued bubble once the composer started rendering one.)
        break;
      case "busy_error":
        // Worker is still alive after a cancel attempt; re-arm the
        // Stop button so the user can try again (or escalate to
        // force-stop after the 2s window).
        appendText("error", ev.message || "Server is busy.", {
          label: "error",
        });
        if (busy) {
          stopBtn.disabled = false;
          stopBtn.textContent = "■ Stop";
          stopBtn.setAttribute("aria-label", "Stop generation");
          delete stopBtn.dataset.forceCancel;
        }
        break;
      case "cancelled":
        // Cancel was accepted; the worker may still be finishing
        // (tool call in flight). Show "Cancelling…" and offer a
        // Force Stop after 2s. state_change → idle is what actually
        // clears busy; the 10s safety timer covers the connection-drop
        // case.
        if (!busy) break;
        clearTimeout(cancelTimeoutId);
        clearTimeout(forceTimeoutId);
        stopBtn.disabled = true;
        stopBtn.textContent = "Cancelling…";
        stopBtn.setAttribute("aria-label", "Cancelling generation");
        cancelTimeoutId = setTimeout(() => {
          if (busy) {
            stopBtn.disabled = false;
            stopBtn.textContent = "⚠ Force Stop";
            stopBtn.setAttribute("aria-label", "Force stop generation");
            stopBtn.dataset.forceCancel = "true";
          }
        }, 2000);
        forceTimeoutId = setTimeout(() => {
          if (busy) {
            appendText(
              "info",
              "Cancel didn't complete in time. You may need to resend your last message.",
              { label: "info" },
            );
            setBusy(false);
          }
        }, 10000);
        break;
      case "tool_info":
        // Renamed from ``tools_auto_approved`` when ``approve_tools``
        // unified onto SessionUIBase — the shared body emits ``tool_info``
        // for both kinds, matching the interactive payload name.  All
        // items in a single ``tool_info`` envelope share a dispatch
        // turn, so render them as one batch construct (parallel when
        // ≥2, solo otherwise) rather than N separate bubbles.
        appendToolBatch(ev.items || [], { auto: true });
        break;
      // Child-workstream fan-out routed through the coordinator's own
      // SSE stream.  CoordinatorManager filters the cluster event bus
      // by known child ws_ids so we never see unrelated noise here.
      case "child_ws_created":
        handleChildCreated(ev);
        break;
      case "child_ws_state":
        handleChildState(ev);
        break;
      case "child_ws_closed":
        handleChildClosed(ev);
        break;
      case "child_ws_rename":
        handleChildRename(ev);
        break;
      // Stage 3 Step 7 — explicit verdict + approval-resolved events
      // arrive without piggybacking on cluster_state, so verdicts that
      // land while a child is in `attention` (no state transition)
      // reach the parent's tree UI promptly. Reducer writes
      // liveBadgeCache directly (bypassing scheduleLiveFetch) so
      // off-screen rows update too.
      case "child_ws_intent_verdict":
        handleChildIntentVerdict(ev);
        break;
      case "child_ws_approval_resolved":
        handleChildApprovalResolved(ev);
        break;
      case "child_ws_approve_request":
        handleChildApproveRequest(ev);
        break;
      // wait_for_workstream observability (#14) — the worker thread
      // can block up to 600s inside the tool; these events drive a
      // sidebar indicator so operators see the coordinator is alive.
      case "wait_started":
        handleWaitStarted(ev);
        break;
      case "wait_progress":
        handleWaitProgress(ev);
        break;
      case "wait_ended":
        handleWaitEnded(ev);
        break;
      default:
        // Unknown event type — ignore silently.
        break;
    }
  }

  // ------------------------------------------------------------------
  // wait_for_workstream progress indicator (#14)
  // ------------------------------------------------------------------
  //
  // In-flight waits keyed by call_id so overlapping / nested waits
  // each get their own badge.  Cleared on wait_ended, and on SSE
  // reconnect (see evtSource.onopen above) — a wait_ended dropped
  // during the gap would otherwise pin the badge indefinitely.
  const activeWaits = new Map();

  function _waitIndicatorEl() {
    let el = document.getElementById("coord-wait-indicator");
    if (el) return el;
    // Only attach to the coord header vocabulary — don't fall back to
    // document.body, which would plant a floating badge at the page
    // root on any template variant where the header hasn't rendered
    // yet (#q-7).  Return null so callers skip rendering; the next
    // event will retry.  Mount into #coord-header (appbar container)
    // NOT #coord-status — statusEl.textContent = ev.state on every
    // state_change event clobbers all children of #coord-status, which
    // would delete the wait indicator on the next state tick.  As a
    // sibling inside the appbar it stays alive across state updates.
    const host = document.getElementById("coord-header");
    if (!host) return null;
    el = document.createElement("span");
    el.id = "coord-wait-indicator";
    el.className = "appbar-status coord-wait-indicator";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.style.display = "none";
    el.style.marginLeft = "0.5em";
    host.appendChild(el);
    return el;
  }

  function _renderWaitIndicator() {
    const el = _waitIndicatorEl();
    if (!el) return; // header not rendered yet — retry on next event
    if (activeWaits.size === 0) {
      el.style.display = "none";
      el.textContent = "";
      return;
    }
    let totalWs = 0;
    let maxElapsed = 0;
    activeWaits.forEach((w) => {
      totalWs += Array.isArray(w.ws_ids) ? w.ws_ids.length : 0;
      if (typeof w.elapsed === "number" && w.elapsed > maxElapsed) {
        maxElapsed = w.elapsed;
      }
    });
    const fragments = [];
    if (activeWaits.size > 1) fragments.push(activeWaits.size + " waits");
    if (totalWs > 0) fragments.push(totalWs + " ws");
    if (maxElapsed > 0) fragments.push(Math.round(maxElapsed) + "s");
    el.textContent =
      "\u29D7 waiting" +
      (fragments.length ? " · " + fragments.join(" · ") : "");
    el.style.display = "";
  }

  function handleWaitStarted(ev) {
    const cid = ev.call_id;
    if (!cid) return;
    activeWaits.set(cid, {
      ws_ids: Array.isArray(ev.ws_ids) ? ev.ws_ids.slice() : [],
      mode: ev.mode || "any",
      timeout: typeof ev.timeout === "number" ? ev.timeout : 60,
      elapsed: 0,
    });
    _renderWaitIndicator();
  }

  function handleWaitProgress(ev) {
    const cid = ev.call_id;
    if (!cid) return;
    const entry = activeWaits.get(cid);
    if (!entry) return;
    if (typeof ev.elapsed === "number") entry.elapsed = ev.elapsed;
    _renderWaitIndicator();
  }

  function handleWaitEnded(ev) {
    const cid = ev.call_id;
    if (!cid) return;
    activeWaits.delete(cid);
    _renderWaitIndicator();
  }

  // ------------------------------------------------------------------
  // Children tree + task list — right sidebar
  // ------------------------------------------------------------------

  // ws_id -> child row snapshot.  Updated on initial /children load +
  // SSE child_ws_* events so the tree can be re-rendered cheaply.
  const childrenState = new Map();
  // ws_id -> {live: <dict>, fetched: <ms>} for the 5s TTL live-badge cache.
  const liveBadgeCache = new Map();
  // Incrementally-maintained set of ws_ids currently flagged
  // ``pending_approval`` in the cache. Updated at every cache mutation
  // via ``_liveBadgeCacheSet`` / ``_liveBadgeCacheDelete`` /
  // ``_liveBadgeCacheClear`` below so the sidebar pending-count read
  // is O(1) instead of an O(N) walk over the cache on every render
  // (caught by /review perf-1).
  const pendingApprovalIds = new Set();
  // The helpers reach into the Map's prototype directly so a future
  // edit can't accidentally rewrite the body's ``liveBadgeCache.set``
  // into the helper name and create infinite recursion (already
  // happened once when bulk-replacing call sites — caught at runtime
  // as "InternalError: too much recursion").
  const _mapSet = Map.prototype.set;
  const _mapDelete = Map.prototype.delete;
  const _mapClear = Map.prototype.clear;
  function _liveBadgeCacheSet(id, entry) {
    _mapSet.call(liveBadgeCache, id, entry);
    if (entry && entry.live && entry.live.pending_approval) {
      pendingApprovalIds.add(id);
    } else {
      pendingApprovalIds.delete(id);
    }
  }
  function _liveBadgeCacheDelete(id) {
    _mapDelete.call(liveBadgeCache, id);
    pendingApprovalIds.delete(id);
  }
  function _liveBadgeCacheClear() {
    _mapClear.call(liveBadgeCache);
    pendingApprovalIds.clear();
  }
  // ws_ids currently visible in the viewport — only these trigger
  // live-fetch on SSE state changes.  Populated by an
  // IntersectionObserver attached to each rendered .ch-row so a
  // coordinator with hundreds of off-screen children doesn't burn
  // HTTP round-trips for rows nobody can see.
  const visibleChildIds = new Set();
  // ws_id -> monotonic timestamp of last update (childrenState Map value
  // + SSE event).  Used by the periodic pruner to drop terminal-state
  // rows that have sat idle past the grace window so long-lived
  // operator tabs don't accumulate unbounded map entries.
  const childrenLastSeen = new Map();
  const TERMINAL_CHILD_STATES = new Set(["closed", "deleted"]);
  const LIVE_BADGE_TTL_MS = 5000;
  const LIVE_BADGE_DEBOUNCE_MS = 250;
  // After handleChildState mutates liveBadgeCache from a child_ws_state
  // SSE event, a bulk-poll landing within this window must NOT
  // overwrite the SSE-supplied pending_approval / _detail fields with
  // its own (potentially stale) snapshot — the upstream node
  // /dashboard cache has its own ~2s TTL so a poll right after a
  // transition can carry pre-transition state.  3s covers the worst
  // case (upstream TTL + console TTL minus a margin).
  const SSE_AUTHORITATIVE_MS = 3000;
  // Debounce window for /tasks refreshes triggered by ``tasks``
  // tool_result SSE events.  Without it, a model that runs
  // ``add → list`` (or any back-to-back mutation pair) double-fetches
  // the same envelope.  150ms is short enough to feel instant to a
  // human watching the sidebar and long enough to coalesce realistic
  // tool-batch sequences (which fire within tens of ms of each other).
  const TASKS_REFRESH_DEBOUNCE_MS = 150;
  let tasksRefreshTimer = null;
  // Sweep every 60s; drop terminal-state entries older than 10min so
  // an operator who scrolls past them later still sees them briefly
  // (they won't vanish mid-read) but we cap the long-tail growth.
  const CHILDREN_PRUNE_INTERVAL_MS = 60 * 1000;
  const CHILDREN_TERMINAL_GRACE_MS = 10 * 60 * 1000;
  const CHILDREN_HARD_CAP = 2000;

  let tasksState = { version: 1, tasks: [] };

  function stateGlyph(state) {
    // cls comes from the shared .ui-glyph-* vocabulary in ui-base.css
    // so the colour treatment matches wherever ui-glyph is used.
    switch (state) {
      case "running":
        return { glyph: "\u25CF", cls: "ui-glyph ui-glyph-running" };
      case "thinking":
        return { glyph: "\u25D0", cls: "ui-glyph ui-glyph-thinking" };
      case "attention":
        return { glyph: "\u26A0", cls: "ui-glyph ui-glyph-attention" };
      case "error":
        return { glyph: "\u2717", cls: "ui-glyph ui-glyph-error" };
      case "closed":
      case "deleted":
      case "idle":
      default:
        return { glyph: "\u25CB", cls: "ui-glyph ui-glyph-idle" };
    }
  }

  function safeAttr(value, re) {
    return value && re.test(value) ? value : null;
  }

  // Build child rows using DOM methods only (no innerHTML) — keeps the
  // XSS surface to zero even for attacker-controlled name strings.
  function renderChildRow(child) {
    const state = child.state || "idle";
    const g = stateGlyph(state);
    const safeWs = safeAttr(child.ws_id, WS_ID_RE);
    const safeNode = safeAttr(child.node_id, NODE_ID_RE);
    const row = document.createElement("div");
    row.className = "ch-row";
    row.setAttribute("role", "listitem");
    if (state === "closed" || state === "deleted") row.classList.add("closed");
    if (child.ws_id) row.dataset.wsId = child.ws_id;

    const a = document.createElement("a");
    a.className = "ws-link";
    if (safeWs && safeNode) {
      a.href =
        "/node/" +
        encodeURIComponent(safeNode) +
        "/?ws_id=" +
        encodeURIComponent(safeWs);
      a.target = "_blank";
      a.rel = "noopener";
    } else {
      a.href = "#";
    }
    const glyphSpan = document.createElement("span");
    glyphSpan.className = g.cls;
    glyphSpan.textContent = g.glyph;
    a.appendChild(glyphSpan);
    const nameSpan = document.createElement("span");
    nameSpan.className = "name";
    nameSpan.textContent = child.name || child.ws_id || "?";
    a.appendChild(nameSpan);
    row.appendChild(a);

    const meta = document.createElement("div");
    meta.className = "meta";
    if (child.node_id) {
      const s = document.createElement("span");
      s.textContent = "node=" + child.node_id;
      meta.appendChild(s);
    }
    if (state) {
      const s = document.createElement("span");
      s.textContent = "state=" + state;
      meta.appendChild(s);
    }
    const cached = liveBadgeCache.get(child.ws_id);
    if (cached && cached.live) {
      if (typeof cached.live.tokens === "number" && cached.live.tokens > 0) {
        const s = document.createElement("span");
        s.textContent = "tokens=" + cached.live.tokens;
        meta.appendChild(s);
      }
      if (cached.live.pending_approval) {
        const s = document.createElement("span");
        s.className = "badge-attention";
        s.textContent = "\u2691 approval";
        meta.appendChild(s);
      }
    }
    row.appendChild(meta);
    // Inline approve/deny block \u2014 the detail arrives via the bulk
    // fetch triggered by handleChildState off the activity_state="approval"
    // transition. Verdicts that land later arrive via
    // child_ws_intent_verdict and update the cached detail in place;
    // resolution arrives via child_ws_approval_resolved and clears it.
    // While the bulk fetch is in flight (~250ms debounce + ~100ms HTTP)
    // we render a loading placeholder so the row keeps its height
    // stable AND so a screen reader has a labelled region to land on
    // \u2014 without it the operator sees a "demand for action" badge
    // with no actionable content.
    if (cached && cached.live && cached.live.pending_approval) {
      const detail = cached.live.pending_approval_detail;
      const block = detail
        ? renderApprovalBlock(child, detail)
        : renderApprovalPlaceholder(child);
      if (block) row.appendChild(block);
    }
    // Recent auto-approves — tools that bypassed the operator gate
    // (skill ``allowed_tools`` allowlist / blanket / admin policy /
    // explicit "Always" click).  Without this pill the operator sees
    // the child run tools they never approved with no explanation.
    // The buffer is bounded server-side at 10 entries so this stays
    // O(1) per render.
    if (
      cached &&
      cached.live &&
      Array.isArray(cached.live.recent_auto_approvals) &&
      cached.live.recent_auto_approvals.length > 0
    ) {
      const pill = renderAutoApprovedPill(cached.live.recent_auto_approvals);
      if (pill) row.appendChild(pill);
    }
    return row;
  }

  // Build a compact pill summarising the row's recent auto-approves.
  // Format: "auto-approved (skill): bash, edit_file +2".  Tooltip
  // expands the full list with reasons.  Returns null when the
  // server hasn't surfaced any entries — defensive against a missing
  // field on older node payloads.
  function renderAutoApprovedPill(entries) {
    if (!Array.isArray(entries) || entries.length === 0) return null;
    const pill = document.createElement("div");
    pill.className = "ch-auto-approved-pill";
    // Group by reason for the lead label; show the most-common reason
    // when the buffer mixes (e.g. skill + always after the operator
    // hit "Approve + Always" on a tool the skill template missed).
    const reasonCounts = new Map();
    for (const e of entries) {
      const r = _normaliseAutoApproveReason(e && e.auto_approve_reason);
      reasonCounts.set(r, (reasonCounts.get(r) || 0) + 1);
    }
    let topReason = "auto_approve_tools";
    let topCount = 0;
    for (const [r, n] of reasonCounts) {
      if (n > topCount) {
        topReason = r;
        topCount = n;
      }
    }
    const names = entries
      .map((e) => (e && (e.approval_label || e.func_name)) || "")
      .filter(Boolean);
    const visible = names.slice(0, 3).join(", ");
    const more = names.length > 3 ? " +" + (names.length - 3) : "";
    const label = document.createElement("span");
    label.className = "ch-auto-approved-label";
    label.textContent =
      "✓ auto-approved (" + topReason + "): " + visible + more;
    pill.appendChild(label);
    // Full breakdown in the tooltip — operator can hover to see
    // every tool name + its specific reason without expanding any
    // additional UI.  Includes timestamps so a recent ad-hoc
    // approval can be told apart from the skill-template baseline.
    const tooltip = entries
      .map((e) => {
        const name = (e && (e.approval_label || e.func_name)) || "(unknown)";
        const reason = _normaliseAutoApproveReason(e && e.auto_approve_reason);
        const ts =
          e && typeof e.ts === "number"
            ? new Date(e.ts * 1000).toLocaleTimeString()
            : "";
        return ts ? `${ts}  ${name}  (${reason})` : `${name}  (${reason})`;
      })
      .join("\n");
    pill.title = tooltip;
    return pill;
  }

  // Build the inline approval block: severity pill, intent summary +
  // judge reasoning, and approve/deny buttons.  Returns a DOM node or
  // null if the detail is unusable (defensive \u2014 server is supposed to
  // emit None when no items).  Stays DOM-method-only to match the
  // zero-innerHTML XSS posture of the rest of the row template.
  // Risk-level → numeric severity for max-across-items computation.
  // Values are ordinal: higher integer = higher severity. Both "crit"
  // and "critical" map to 3 because production emitters disagree:
  // turnstone/core/judge.py validates against ('low','medium','high',
  // 'critical') and the heuristic seeds emit the full word, but
  // earlier dock UI history used the abbreviation. Accept both.
  // Unknown / malformed risk_level falls back to "high" rank so a
  // schema drift fails *safe* (over-alert) rather than silently
  // downgrading to a green pill — fixes the failure mode where
  // "critical" was treated as unknown and rendered as low.
  const RISK_SEVERITY = {
    low: 0,
    medium: 1,
    med: 1,
    high: 2,
    crit: 3,
    critical: 3,
  };
  const UNKNOWN_RISK_RANK = 2; // fail-safe: treat unknown as "high"

  function _riskRank(verdict) {
    if (!verdict) return -1;
    const risk = (verdict.risk_level || "").toLowerCase();
    return RISK_SEVERITY[risk] != null
      ? RISK_SEVERITY[risk]
      : UNKNOWN_RISK_RANK;
  }

  // Pick the item carrying the highest risk_level — pill colour and
  // body display follow the worst tool in the envelope so a low-risk
  // item[0] can't visually mask a crit item[2].
  function _maxSeverityItem(items) {
    let best = items[0];
    let bestRank = _riskRank(best.judge_verdict || best.heuristic_verdict);
    for (let i = 1; i < items.length; i += 1) {
      const v = items[i].judge_verdict || items[i].heuristic_verdict;
      const r = _riskRank(v);
      if (r > bestRank) {
        best = items[i];
        bestRank = r;
      }
    }
    return best;
  }

  function _evidenceLineText(line) {
    if (typeof line === "string") return line;
    try {
      return JSON.stringify(line);
    } catch (_) {
      return String(line);
    }
  }

  function _renderSubItem(item) {
    const sub = document.createElement("div");
    sub.className = "approval-sub-item";
    const head = document.createElement("div");
    head.className = "approval-sub-head";
    const name = document.createElement("span");
    name.className = "approval-tool";
    name.textContent = item.func_name || item.approval_label || "(tool)";
    head.appendChild(name);
    const v = item.judge_verdict || item.heuristic_verdict;
    if (v) {
      const tier = document.createElement("span");
      tier.className = "approval-tier";
      const tierLabel = v.tier || (item.judge_verdict ? "llm" : "heuristic");
      tier.textContent = (tierLabel === "llm" ? "⚖" : "⚙") + " " + tierLabel;
      head.appendChild(tier);
    }
    sub.appendChild(head);
    if (v && v.intent_summary) {
      const p = document.createElement("div");
      p.className = "approval-summary";
      p.textContent = v.intent_summary;
      sub.appendChild(p);
    }
    if (item.preview) {
      const pre = document.createElement("pre");
      pre.className = "approval-preview";
      pre.textContent = item.preview;
      sub.appendChild(pre);
    }
    return sub;
  }

  // Loading-state placeholder rendered while the bulk fetch is
  // in-flight. Same outer ``.approval-block`` class so the row's
  // height stays stable when the real content swaps in (no shove of
  // sibling rows mid-mouse-movement). Carries an aria-label so a
  // screen reader announces the pending approval; the actual
  // assertive ``announceApproval`` call in handleChildState fires
  // once on the rising edge so the operator hears the demand even
  // before the bulk fetch lands.
  function renderApprovalPlaceholder(child) {
    const block = document.createElement("div");
    block.className = "approval-block approval-block-loading";
    block.setAttribute("role", "region");
    block.setAttribute(
      "aria-label",
      "Approval required for " +
        (child.name || child.ws_id || "child") +
        " — loading details",
    );
    const header = document.createElement("div");
    header.className = "approval-header";
    const pill = document.createElement("span");
    pill.className = "approval-pill approval-pill-pending";
    const spin = document.createElement("span");
    spin.className = "approval-loading-spin";
    pill.appendChild(spin);
    pill.appendChild(document.createTextNode(" loading…"));
    header.appendChild(pill);
    block.appendChild(header);
    return block;
  }

  function renderApprovalBlock(child, detail) {
    if (!detail || !Array.isArray(detail.items) || detail.items.length === 0) {
      return null;
    }
    const items = detail.items;
    // Pill + body display follow the highest-risk item; tool-name
    // summary still leads with item[0] (envelope-level approve resolves
    // them all so leading with [0] keeps the operator's mental model
    // anchored on "what the LLM dispatched first").
    const primary = items[0];
    const severityItem = _maxSeverityItem(items);
    const judge = severityItem.judge_verdict || null;
    const heuristic = severityItem.heuristic_verdict || null;
    const verdict = judge || heuristic;
    // Pending pill should only show when there's *no* verdict to
    // display — if a heuristic verdict is already present, the body
    // renders intent_summary/reasoning from it and a "judge running"
    // pill would contradict that. Only the judge-tier upgrade is
    // genuinely pending; the heuristic itself is already final.
    const judgePending = !!detail.judge_pending && !verdict;
    // Tool-policy denial detection — any item with .error set and
    // !needs_approval is server-blocked. Drives a banner instead of
    // buttons (clicking either would no-op since the call won't run).
    const policyBlocked = items.some((it) => it.error && !it.needs_approval);
    const judgeUnavailable = !verdict && !judgePending && !policyBlocked;

    const block = document.createElement("div");
    block.className = "approval-block";
    block.setAttribute("role", "region");
    block.setAttribute(
      "aria-label",
      "Approval required for " +
        (child.name || child.ws_id || "child") +
        " — " +
        (primary && primary.header
          ? primary.header
          : items.length + " tool calls"),
    );

    // Header line: pill + tool name(s) + tier:model
    const header = document.createElement("div");
    header.className = "approval-header";

    // Pill \u2014 risk-level drives colour (.risk.low/.med/.high/.crit
    // from shared_static/design/primitives/pills.css), recommendation
    // lives in the disclosure footer per the plan. Special pills for
    // policy-blocked, judge-pending, and judge-unavailable matrix
    // rows so every state has a visible header signal.
    const pill = document.createElement("span");
    pill.className = "approval-pill";
    if (policyBlocked) {
      pill.classList.add("risk", "crit");
      pill.textContent = "POLICY-BLOCKED";
    } else if (judgePending) {
      pill.classList.add("approval-pill-pending");
      pill.textContent = "\u23f3 judge running\u2026";
    } else if (judgeUnavailable) {
      pill.classList.add("approval-pill-pending");
      pill.textContent = "(judge unavailable)";
    } else if (verdict) {
      const risk = (verdict.risk_level || "").toLowerCase();
      // Map verdict.risk_level → CSS class. Production emitters use
      // both "crit" and "critical"; pills.css only defines .risk.crit
      // so collapse the alias here. Unknown risk falls back to .high
      // (matching UNKNOWN_RISK_RANK) — fail-safe over-alert.
      const riskCls =
        risk === "crit" || risk === "critical"
          ? "crit"
          : risk === "high"
            ? "high"
            : risk === "medium" || risk === "med"
              ? "med"
              : risk === "low"
                ? "low"
                : "high";
      pill.classList.add("risk", riskCls);
      const conf = verdict.confidence;
      const confStr = typeof conf === "number" ? " " + conf.toFixed(2) : "";
      pill.textContent = (verdict.risk_level || "").toUpperCase() + confStr;
      // SR-friendly label: spell out the risk level + optional
      // confidence so a screen reader doesn't read "LOW 0.85" as
      // a string of letters and a number. Visual text stays compact.
      const riskWord =
        riskCls === "crit"
          ? "critical"
          : riskCls === "high"
            ? "high"
            : riskCls === "med"
              ? "medium"
              : "low";
      const confLabel =
        typeof conf === "number"
          ? ", confidence " + Math.round(conf * 100) + "%"
          : "";
      pill.setAttribute("aria-label", riskWord + " risk" + confLabel);
    }
    header.appendChild(pill);

    // Tool-name summary \u2014 first item, plus "+ N more" for envelopes.
    const toolName = document.createElement("span");
    toolName.className = "approval-tool";
    const baseName = primary.func_name || primary.approval_label || "(tool)";
    toolName.textContent =
      items.length > 1
        ? baseName + " + " + (items.length - 1) + " more"
        : baseName;
    header.appendChild(toolName);

    // Tier + judge_model (e.g. "\u2696 llm:gpt-5" or "\u2699 heuristic").
    if (verdict) {
      const tier = document.createElement("span");
      tier.className = "approval-tier";
      const tierLabel = verdict.tier || (judge ? "llm" : "heuristic");
      const glyph = tierLabel === "llm" ? "\u2696" : "\u2699";
      const model = verdict.judge_model ? ":" + verdict.judge_model : "";
      tier.textContent = glyph + " " + tierLabel + model;
      header.appendChild(tier);
    }

    block.appendChild(header);

    // Tool-policy denial: server-side policy already blocked at least
    // one call in the envelope; render a banner instead of buttons
    // (clicking either would no-op since the call won't run).
    if (policyBlocked) {
      const banner = document.createElement("div");
      banner.className = "approval-policy-block";
      const denied = items.find((it) => it.error && !it.needs_approval);
      banner.textContent =
        "\u26d4 " + ((denied && denied.error) || "blocked by tool policy");
      block.appendChild(banner);
      return block;
    }

    // Body: intent_summary (if any) + reasoning teaser + \u25b8 more.
    const summary = verdict && verdict.intent_summary;
    if (summary) {
      const p = document.createElement("div");
      p.className = "approval-summary";
      p.textContent = summary;
      block.appendChild(p);
    }
    const reasoning = verdict && verdict.reasoning;
    const evidence =
      verdict && Array.isArray(verdict.evidence) ? verdict.evidence : [];
    if (reasoning || evidence.length > 0 || items.length > 1) {
      // Reasoning teaser line \u2014 only rendered when reasoning is
      // present. Evidence-only is also possible (heuristic-only path
      // can carry evidence with no prose); evidence falls into the
      // disclosure below. Without this guard, an evidence-only
      // verdict would append an empty <div class="approval-reasoning">.
      if (reasoning) {
        const reasonLine = document.createElement("div");
        reasonLine.className = "approval-reasoning";
        const lead = document.createElement("span");
        lead.className = "approval-reasoning-lead";
        lead.textContent = "\u21b3 judge: ";
        reasonLine.appendChild(lead);
        const text = document.createElement("span");
        text.textContent = reasoning;
        reasonLine.appendChild(text);
        block.appendChild(reasonLine);
      }
      // Auto-expand for high/crit risk, recommendation=deny, or a
      // long preview (>4 lines) \u2014 the plan's \u00a7Frontend visual design
      // auto-expand rule. Operator sees the full context by default
      // at the moment they most need it.
      const risk = ((verdict && verdict.risk_level) || "").toLowerCase();
      const rec = (verdict && verdict.recommendation) || "";
      const previewLines = primary.preview
        ? primary.preview.split("\n").length
        : 0;
      const longPreview = previewLines > 4;
      const longReasoning = reasoning && reasoning.length > 240;
      const autoExpand =
        risk === "high" ||
        risk === "crit" ||
        risk === "critical" ||
        rec === "deny" ||
        longPreview;
      if (evidence.length > 0 || longReasoning || items.length > 1) {
        const disclosure = document.createElement("details");
        disclosure.className = "approval-disclosure";
        if (autoExpand) disclosure.open = true;
        const sum = document.createElement("summary");
        sum.textContent = "\u25b8 more";
        disclosure.appendChild(sum);
        // Recommendation chip footer \u2014 keeps recommendation surfacing
        // even though the pill colour is now risk-driven (per plan).
        if (rec) {
          const recChip = document.createElement("code");
          recChip.className =
            rec === "approve"
              ? "rec-approve"
              : rec === "deny"
                ? "rec-deny"
                : "rec-review";
          recChip.textContent = "judge recommends: " + rec;
          disclosure.appendChild(recChip);
        }
        if (evidence.length > 0) {
          const ul = document.createElement("ul");
          ul.className = "approval-evidence";
          evidence.forEach((line) => {
            const li = document.createElement("li");
            li.textContent = _evidenceLineText(line);
            ul.appendChild(li);
          });
          disclosure.appendChild(ul);
        }
        // Stack items 2..N inside the disclosure with their own
        // intent_summary + preview + tier badge so the operator can
        // see what every call in the envelope does (one approve
        // resolves them all per server semantics).
        if (items.length > 1) {
          const moreLabel = document.createElement("div");
          moreLabel.className = "approval-more-label";
          moreLabel.textContent =
            "\u25b8 " + (items.length - 1) + " more tools";
          disclosure.appendChild(moreLabel);
          for (let i = 1; i < items.length; i += 1) {
            disclosure.appendChild(_renderSubItem(items[i]));
          }
        }
        block.appendChild(disclosure);
      }
    }

    // Preview \u2014 what's actually being run for the primary item.
    if (primary.preview) {
      const pre = document.createElement("pre");
      pre.className = "approval-preview";
      pre.textContent = primary.preview;
      block.appendChild(pre);
    }

    // Action row: Deny + Approve.  Buttons are addEventListener-bound
    // (not inline onclick) since the row is dynamically created and
    // re-rendered. Both declared before listener wiring to avoid the
    // cross-reference TDZ-shaped read pattern.
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const denyBtn = document.createElement("button");
    const approveBtn = document.createElement("button");
    denyBtn.type = "button";
    denyBtn.className = "act danger sm";
    denyBtn.textContent = "Deny";
    approveBtn.type = "button";
    approveBtn.className = "act primary sm";
    approveBtn.textContent = "Approve";
    denyBtn.addEventListener("click", () =>
      submitChildApproval(child.ws_id, detail, false, denyBtn, approveBtn),
    );
    approveBtn.addEventListener("click", () =>
      submitChildApproval(child.ws_id, detail, true, denyBtn, approveBtn),
    );
    actions.appendChild(denyBtn);
    actions.appendChild(approveBtn);
    block.appendChild(actions);

    return block;
  }

  // Submit the approve POST + handle the result.  On success, locally
  // clear pending_approval_detail so the row re-renders without
  // buttons immediately (optimistic update \u2014 the next live-bulk poll
  // confirms).  On 409 (stale call_id), refresh the live block so the
  // row re-renders against the new round.
  async function submitChildApproval(
    targetWsId,
    detail,
    approved,
    denyBtn,
    approveBtn,
  ) {
    const callId =
      (detail && detail.call_id) ||
      (detail &&
        Array.isArray(detail.items) &&
        detail.items[0] &&
        detail.items[0].call_id) ||
      "";
    if (!callId) return;
    denyBtn.disabled = true;
    approveBtn.disabled = true;
    try {
      const resp = await approveWorkstream(targetWsId, {
        approved: !!approved,
        always: false,
        call_id: callId,
      });
      if (resp.status === 409) {
        // Stale call_id \u2014 server has rolled to a new round, or
        // (more commonly) the approval was already resolved on
        // another channel and this click raced. Keep both buttons
        // disabled until the refresh lands and re-renders the row:
        // the row is about to be replaced wholesale, so the disabled
        // DOM is dropped along with it. Re-enabling here was the bug
        // \u2014 it opened a window where rapid clicks hit the same
        // already-resolved approval, each producing a fresh 409,
        // looping until the live-bulk eventually cleared the row.
        // On the rare path where the refresh fails entirely, the
        // operator can hit the Refresh button on the children panel
        // to force a full reload. invalidateLiveBadge clears the
        // cached entry so the next scheduleLiveFetch falls through
        // the TTL gate (no cached entry to compare against); the
        // standard 250ms debounce batches with any other in-flight
        // pending ids.
        // Inline note so the operator's "did my click work?" question
        // gets answered without a noisy toast. Stays in the row until
        // the refresh replaces the whole approval block.
        const block = denyBtn.closest(".approval-block");
        if (block && !block.querySelector(".approval-stale-note")) {
          const note = document.createElement("div");
          note.className = "approval-stale-note";
          note.setAttribute("role", "status");
          note.textContent =
            "\u21bb already resolved elsewhere \u2014 refreshing\u2026";
          block.appendChild(note);
        }
        invalidateLiveBadge(targetWsId);
        scheduleLiveFetch(targetWsId);
        // Quiet console-warn for diagnostics; no toast \u2014 the
        // disappearing buttons / fresh row IS the operator-facing
        // signal, and a toast on every rapid-click 409 would just
        // add noise.
        console.warn("approval state changed for", targetWsId);
        return;
      }
      if (!resp.ok) {
        throw new Error("approve failed: HTTP " + resp.status);
      }
      // Optimistic clear \u2014 the next child_ws_state event will arrive
      // shortly and trigger a real refresh, but clearing locally
      // makes the buttons disappear immediately on click. The
      // ``sseUpdatedAt`` bump is load-bearing: ``flushLiveFetches``'s
      // merge guard preserves cleared pending_approval / _detail
      // against an in-flight bulk fetch only while
      // ``now - prev.sseUpdatedAt < SSE_AUTHORITATIVE_MS`` \u2014 without
      // bumping, a bulk fetch landing in the gap reverts the
      // cleared state from the upstream cache and the approve/deny
      // pill flickers back. (Caught by /review bug-4.)
      const cached = liveBadgeCache.get(targetWsId);
      if (cached && cached.live) {
        cached.live = Object.assign({}, cached.live, {
          pending_approval: false,
          pending_approval_detail: null,
        });
        cached.sseUpdatedAt = Date.now();
        _liveBadgeCacheSet(targetWsId, cached);
      }
      // Also clear the activity_state mirror so the row's "⚑ approval"
      // badge in .meta disappears immediately too — without this, the
      // row shows the badge with no buttons for ~50-150ms until the
      // child_ws_approval_resolved push or next state event lands.
      const childState = childrenState.get(targetWsId);
      if (childState && childState.activity_state === "approval") {
        childState.activity_state = "";
      }
      renderChildren();
    } catch (e) {
      denyBtn.disabled = false;
      approveBtn.disabled = false;
      if (typeof toast !== "undefined" && toast.error) toast.error(String(e));
      else console.error(e);
    }
  }

  // Coalesce repeated renderChildren() calls within a single frame so
  // SSE bursts (N child_ws_state events in quick succession) don't
  // trigger N full tree rebuilds.  rAF fires at most once per display
  // refresh, dropping ~60Hz of intra-frame churn to one render.
  let _renderChildrenScheduled = false;
  function renderChildren() {
    if (_renderChildrenScheduled) return;
    _renderChildrenScheduled = true;
    const raf =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame
        : (cb) => setTimeout(cb, 16);
    raf(() => {
      _renderChildrenScheduled = false;
      _renderChildrenNow();
    });
  }

  // IntersectionObserver singleton — tracks which .ch-row elements are
  // currently in the scroll viewport so scheduleLiveFetch skips
  // off-screen rows.  Lazy init: created on first render since the
  // observer api isn't guaranteed on ancient browsers and the tree
  // degrades to "all rows always considered visible" as a fallback.
  let _childObserver = null;
  function _getChildObserver() {
    if (_childObserver !== null) return _childObserver;
    if (typeof IntersectionObserver !== "function") {
      _childObserver = false; // sentinel: no-obs mode, treat all visible
      return _childObserver;
    }
    _childObserver = new IntersectionObserver(
      (entries) => {
        let anyNew = false;
        entries.forEach((ent) => {
          const el = ent.target;
          const wsKey = el && el.dataset ? el.dataset.wsId : "";
          if (!wsKey) return;
          if (ent.isIntersecting) {
            if (!visibleChildIds.has(wsKey)) {
              visibleChildIds.add(wsKey);
              anyNew = true;
            }
          } else {
            visibleChildIds.delete(wsKey);
          }
        });
        // Rows that just entered the viewport get their live-fetch
        // scheduled immediately — the observer-fire is the moment
        // scheduling became legal.
        if (anyNew) {
          visibleChildIds.forEach((wsKey) => scheduleLiveFetch(wsKey));
        }
      },
      { root: childrenTreeEl, threshold: 0.1 },
    );
    return _childObserver;
  }

  // Focus preservation helpers shared by _renderChildrenNow and
  // _updateChildRow. ``replaceChildren()`` / ``replaceWith()`` blow
  // away the focused element silently — without restore, the operator
  // gets bounced to <body> mid-Tab whenever any state event fires for
  // a row in the children tree.
  function _captureRowFocusKey(scopeEl) {
    const active = document.activeElement;
    if (!active || !scopeEl || !scopeEl.contains(active)) return null;
    const row = active.closest(".ch-row");
    if (!row || !row.dataset.wsId) return null;
    return {
      wsId: row.dataset.wsId,
      marker: active.className || active.tagName,
    };
  }

  function _restoreRowFocus(scopeEl, focusKey) {
    if (!focusKey || !scopeEl) return;
    const sel = '.ch-row[data-ws-id="' + cssEscape(focusKey.wsId) + '"]';
    const row = scopeEl.matches(sel) ? scopeEl : scopeEl.querySelector(sel);
    if (!row) return;
    let target = null;
    if (focusKey.marker) {
      // CSS.escape can't safely round-trip a class list with spaces,
      // so we walk focusables and string-compare. A future refactor
      // could swap to a stable ``data-focus-key`` attribute on each
      // focusable element to dodge class-string identity entirely
      // (see /review bug-2 — currently low risk because all
      // focusables in renderChildRow / renderApprovalBlock carry
      // single-class names).
      const candidates = row.querySelectorAll("button, [tabindex], a, summary");
      for (const el of candidates) {
        if ((el.className || el.tagName) === focusKey.marker) {
          target = el;
          break;
        }
      }
    }
    if (target) target.focus({ preventScroll: true });
  }

  function _renderChildrenNow() {
    const focusKey = _captureRowFocusKey(childrenTreeEl);
    childrenTreeEl.setAttribute("aria-busy", "false");
    const rows = Array.from(childrenState.values());
    // Sort: non-terminal states first, then by name.
    const terminal = { closed: 1, deleted: 1 };
    rows.sort((a, b) => {
      const ta = terminal[a.state] ? 1 : 0;
      const tb = terminal[b.state] ? 1 : 0;
      if (ta !== tb) return ta - tb;
      return (a.name || "").localeCompare(b.name || "");
    });
    // Disconnect + reset visibility set — each render rebuilds the
    // observed element set.  Observer retains its configuration.
    const obs = _getChildObserver();
    if (obs) {
      obs.disconnect();
      visibleChildIds.clear();
    }
    childrenTreeEl.replaceChildren();
    if (rows.length === 0) {
      const empty = document.createElement("div");
      empty.className = "sidebar-empty";
      empty.textContent = "no children spawned yet";
      childrenTreeEl.appendChild(empty);
    } else {
      rows.forEach((r) => {
        const rowEl = renderChildRow(r);
        childrenTreeEl.appendChild(rowEl);
        if (obs) obs.observe(rowEl);
        else visibleChildIds.add(r.ws_id); // fallback: treat all visible
      });
    }
    // Sidebar count: total + pending-approval annotation. The
    // pending count is maintained incrementally on cache mutations
    // (see ``pendingApprovalIds`` near the cache definition) so this
    // is O(1) per render rather than an O(N) walk over the cache.
    const pending = pendingApprovalIds.size;
    childrenCountEl.textContent = rows.length
      ? "(" +
        rows.length +
        (pending > 0 ? " · " + pending + " pending" : "") +
        ")"
      : "";
    _restoreRowFocus(childrenTreeEl, focusKey);
  }

  // Targeted single-row update — used by handlers that only touch
  // one row's state (verdict landing, approval resolved). Avoids
  // the tree-wide rebuild ``_renderChildrenNow`` does so a 200-child
  // tree doesn't re-paint 199 unaffected rows on every verdict
  // arrival. Falls back to the full render if the row isn't in the
  // DOM yet (first-time render). Preserves keyboard focus across
  // the row swap — the same invariant ``_renderChildrenNow`` keeps
  // for tree-wide rebuilds (caught by /review bug-3).
  function _updateChildRow(childId) {
    const sel = '.ch-row[data-ws-id="' + cssEscape(childId) + '"]';
    const row = childrenTreeEl.querySelector(sel);
    const entry = childrenState.get(childId);
    if (row && entry) {
      const focusKey = _captureRowFocusKey(row);
      const replacement = renderChildRow(entry);
      row.replaceWith(replacement);
      const obs = _getChildObserver();
      if (obs) obs.observe(replacement);
      _restoreRowFocus(replacement, focusKey);
    } else {
      renderChildren();
    }
  }

  function renderTaskRow(task) {
    const row = document.createElement("div");
    row.className = "task-row";
    row.setAttribute("role", "listitem");
    const status = task.status || "pending";
    const statusSpan = document.createElement("span");
    statusSpan.className = "status status-" + status;
    statusSpan.textContent = status;
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = task.title || "";
    const head = document.createElement("div");
    head.appendChild(statusSpan);
    head.appendChild(title);
    row.appendChild(head);
    if (task.child_ws_id && WS_ID_RE.test(task.child_ws_id)) {
      const link = document.createElement("div");
      link.className = "meta";
      const a = document.createElement("a");
      a.href = "#child-" + encodeURIComponent(task.child_ws_id);
      a.textContent = "\u2192 child " + task.child_ws_id.slice(0, 8);
      a.addEventListener("click", (e) => {
        e.preventDefault();
        const target = document.querySelector(
          '.ch-row[data-ws-id="' + cssEscape(task.child_ws_id) + '"]',
        );
        if (target && target.scrollIntoView) {
          target.scrollIntoView({ behavior: "smooth", block: "nearest" });
          target.classList.add("highlight");
          setTimeout(() => target.classList.remove("highlight"), 1200);
        }
      });
      link.appendChild(a);
      row.appendChild(link);
    }
    return row;
  }

  function renderTasks() {
    tasksEl.replaceChildren();
    const tasks = (tasksState && tasksState.tasks) || [];
    if (tasks.length === 0) {
      const empty = document.createElement("div");
      empty.className = "sidebar-empty";
      empty.textContent = "no tasks yet";
      tasksEl.appendChild(empty);
    } else {
      tasks.forEach((t) => tasksEl.appendChild(renderTaskRow(t)));
    }
    tasksCountEl.textContent = tasks.length ? "(" + tasks.length + ")" : "";
  }

  async function loadChildren({ replace = false } = {}) {
    childrenTreeEl.setAttribute("aria-busy", "true");
    try {
      const body = await getJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/children",
      );
      // Default (initial page load): merge rather than clear.  SSE
      // events may have arrived during the in-flight fetch and
      // `clear()` would wipe them before the merge.
      //
      // replace=true (operator hits Refresh): take the server snapshot
      // as authoritative — stale SSE-only rows disappear on demand.
      const fresh = new Map();
      (body.items || []).forEach((c) => {
        if (c && c.ws_id) fresh.set(c.ws_id, { ...c });
      });
      if (replace) {
        childrenState.clear();
        childrenLastSeen.clear();
      }
      const now = Date.now();
      fresh.forEach((v, k) => {
        childrenState.set(k, v);
        childrenLastSeen.set(k, now);
      });
    } catch (e) {
      console.warn("loadChildren failed", e);
    } finally {
      renderChildren();
      childrenState.forEach((_, ws) => scheduleLiveFetch(ws));
    }
  }

  async function loadTasks() {
    try {
      const body = await getJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/tasks",
      );
      tasksState = body || { version: 1, tasks: [] };
    } catch (e) {
      console.warn("loadTasks failed", e);
    } finally {
      renderTasks();
    }
  }

  // Debounced wrapper for SSE-triggered refreshes.  A burst of
  // tasks mutations (the model's typical add → update → list
  // pattern, or a coordinator that re-renders the whole list) lands
  // multiple tool_result events within tens of ms; without this each
  // one would fire its own /tasks fetch.  Coalescing into one fetch
  // per 150ms window keeps the sidebar responsive without amplifying
  // load.  Direct UI actions (refresh button, initial load) keep
  // calling ``loadTasks`` directly so user clicks are never delayed.
  function loadTasksDebounced() {
    if (tasksRefreshTimer !== null) {
      clearTimeout(tasksRefreshTimer);
    }
    tasksRefreshTimer = setTimeout(() => {
      tasksRefreshTimer = null;
      loadTasks();
    }, TASKS_REFRESH_DEBOUNCE_MS);
  }

  // Upper bound on ids per bulk request — matches the server-side
  // cap in cluster_ws_live_bulk.  A viewport with more visible rows
  // than the cap splits into multiple bulk calls, each ~one round-trip
  // per TTL window; still far cheaper than one-per-row.
  const LIVE_BADGE_BULK_CAP = 50;
  // Coalesce window — debounced per-row scheduling enqueues into
  // pendingLiveIds; the flush runs after this idle window collapses
  // into a single bulk request.  Matches the per-row debounce so a
  // burst of SSE ticks lands in one flush.
  const LIVE_BADGE_BULK_FLUSH_MS = LIVE_BADGE_DEBOUNCE_MS;
  const pendingLiveIds = new Set();
  let liveBadgeFlushTimer = null;

  function scheduleLiveFetch(childWsId) {
    if (!childWsId) return;
    // Skip terminal-state children entirely — their live block will
    // never change again; fetching just burns a round-trip and caches
    // a stale value.  Renderer already styles closed/deleted rows.
    const entry = childrenState.get(childWsId);
    if (entry && TERMINAL_CHILD_STATES.has(entry.state)) return;
    // Skip rows that aren't in the viewport.  The IntersectionObserver
    // calls scheduleLiveFetch when a row scrolls into view, so
    // off-screen rows sit idle until the operator scrolls to them —
    // a coordinator with 100+ children only fires ~visible-count
    // concurrent fetches on initial load instead of N.
    if (!visibleChildIds.has(childWsId)) return;
    const cached = liveBadgeCache.get(childWsId);
    if (cached) {
      // "permanent" cache entries (403/404 — the caller lacks the
      // admin.cluster.inspect permission, or the ws_id is unknown
      // cluster-wide) never re-fire.  Without this, every SSE state
      // change on any child triggers a fresh fetch → retry storm for
      // users who'll never have permission mid-session.
      if (cached.permanent) return;
      // TTL gate — slower-moving fields (tokens, context_ratio) refresh
      // on this cadence. Approval state is event-driven (intent_verdict
      // / approval_resolved push, plus the bulk fetch on initial
      // approval entry); callers wanting a forced re-fetch
      // (e.g. 409 stale-call_id retry) call invalidateLiveBadge first
      // so the cache is empty and this gate falls through.
      if (Date.now() - cached.fetched < LIVE_BADGE_TTL_MS) return;
    }
    if (!WS_ID_RE.test(childWsId)) return;
    pendingLiveIds.add(childWsId);
    if (liveBadgeFlushTimer !== null) return;
    liveBadgeFlushTimer = setTimeout(() => {
      liveBadgeFlushTimer = null;
      flushLiveFetches();
    }, LIVE_BADGE_BULK_FLUSH_MS);
  }

  async function flushLiveFetches() {
    if (pendingLiveIds.size === 0) return;
    const ids = Array.from(pendingLiveIds).slice(0, LIVE_BADGE_BULK_CAP);
    ids.forEach((id) => pendingLiveIds.delete(id));
    // Reschedule a follow-up flush if we overflowed the cap so the
    // excess ids still land — without this, a viewport bigger than the
    // cap would silently drop the tail every tick.
    if (pendingLiveIds.size > 0 && liveBadgeFlushTimer === null) {
      liveBadgeFlushTimer = setTimeout(() => {
        liveBadgeFlushTimer = null;
        flushLiveFetches();
      }, LIVE_BADGE_BULK_FLUSH_MS);
    }
    try {
      const url =
        "/v1/api/cluster/ws/live?ids=" + ids.map(encodeURIComponent).join(",");
      const body = await getJSON(url);
      const results = (body && body.results) || {};
      const denied = Array.isArray(body && body.denied) ? body.denied : [];
      const now = Date.now();
      ids.forEach((id) => {
        const live = Object.prototype.hasOwnProperty.call(results, id)
          ? results[id]
          : null;
        const wasDenied = denied.indexOf(id) !== -1;
        const prev = liveBadgeCache.get(id);
        // SSE-set pending_approval / _detail wins over a stale
        // bulk-poll snapshot for SSE_AUTHORITATIVE_MS after the
        // SSE update.  Without this guard, a poll landing right
        // after a child_ws_state transition can clobber freshly-
        // mutated approval state with pre-transition data from
        // the upstream /dashboard cache (which has its own ~2s
        // TTL).  Other fields (tokens, context_ratio) still track
        // the bulk response — only the approval surface is gated.
        let mergedLive = live;
        if (
          live &&
          prev &&
          prev.sseUpdatedAt &&
          now - prev.sseUpdatedAt < SSE_AUTHORITATIVE_MS &&
          prev.live
        ) {
          mergedLive = Object.assign({}, live, {
            pending_approval: prev.live.pending_approval,
            pending_approval_detail: prev.live.pending_approval_detail,
          });
        }
        _liveBadgeCacheSet(id, {
          live: mergedLive,
          fetched: now,
          // Denied ids are permission/identity misses — mark permanent
          // so SSE state ticks on those rows don't retry every window.
          permanent: wasDenied,
          sseUpdatedAt: prev ? prev.sseUpdatedAt || 0 : 0,
        });
        const row = childrenTreeEl.querySelector(
          '.ch-row[data-ws-id="' + cssEscape(id) + '"]',
        );
        if (row) {
          const entry = childrenState.get(id);
          if (entry) {
            const replacement = renderChildRow(entry);
            row.replaceWith(replacement);
          }
        }
      });
    } catch (e) {
      // 403 = caller lacks admin.cluster.inspect → mark every pending
      // id permanent so we don't retry every window.  Other failures
      // (5xx, network) take the normal TTL and recover on the next
      // schedule.
      const isPermanent = e && /HTTP 403/.test(e.message || "");
      const now = Date.now();
      ids.forEach((id) => {
        const prev = liveBadgeCache.get(id);
        _liveBadgeCacheSet(id, {
          live: null,
          fetched: now,
          permanent: isPermanent,
          sseUpdatedAt: prev ? prev.sseUpdatedAt || 0 : 0,
        });
      });
      if (!isPermanent) console.warn("flushLiveFetches failed", e);
    }
  }

  function invalidateLiveBadge(childWsId) {
    _liveBadgeCacheDelete(childWsId);
  }

  // --- SSE handlers for child_ws_* events ----------------------------

  function _touchChild(childId) {
    childrenLastSeen.set(childId, Date.now());
  }

  function handleChildCreated(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    childrenState.set(childId, {
      ws_id: childId,
      node_id: ev.node_id || "",
      name: ev.name || ev.title || childId.slice(0, 8),
      state: "idle",
      kind: "interactive",
    });
    _touchChild(childId);
    renderChildren();
    invalidateLiveBadge(childId);
    scheduleLiveFetch(childId);
  }

  function handleChildState(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    const existing = childrenState.get(childId) || {
      ws_id: childId,
      name: "",
    };
    existing.state = ev.state || existing.state;
    existing.activity_state =
      typeof ev.activity_state === "string"
        ? ev.activity_state
        : existing.activity_state || "";
    if (ev.node_id) existing.node_id = ev.node_id;
    childrenState.set(childId, existing);
    _touchChild(childId);
    // Track ``pending_approval`` flag from BOTH state and
    // activity_state. ``state="attention"`` is the canonical signal
    // that the workstream needs operator attention; ``activity_state
    // ="approval"`` is the secondary signal set by approve_tools.
    // We trust either: in practice the worker thread can fire the
    // state transition before approve_tools has updated activity_state
    // (the two writes happen on different lines under different
    // locks), so a state-attention event with empty activity_state
    // is a real and common case — checking only activity_state
    // misses 30+ children all sitting in attention. (Caught manual
    // testing: 30 children all state=attention rendered with no
    // approval blocks because pendingApproval was always false.)
    const pendingApproval =
      existing.state === "attention" || existing.activity_state === "approval";
    const cached = liveBadgeCache.get(childId);
    const cachedLive = (cached && cached.live) || {};
    // Rising-edge detection BEFORE we mutate the cache. The chat-pane
    // tool batches already announce assertively
    // (renderApprovalDock / appendToolBatch); the children-tree was
    // silent for SR users — fixing that here so a blind operator
    // hears the demand for action.
    const wasPendingApproval = cachedLive.pending_approval === true;
    if (pendingApproval && !wasPendingApproval) {
      _announceAssertive(
        "Approval required: " + (existing.name || childId.slice(0, 8)),
      );
    }
    const nextLive = Object.assign({}, cachedLive, {
      pending_approval: pendingApproval,
    });
    // Off-approval transition is the ONLY case here that
    // authoritatively writes a value the bulk fetch must not
    // resurrect (a stale bulk-fetch landing within
    // SSE_AUTHORITATIVE_MS would otherwise re-render the cleared
    // approval block). Setting ``pending_approval=true`` does NOT
    // claim cache authority — the bulk fetch is the source of
    // truth for ``pending_approval_detail``, and bumping
    // sseUpdatedAt here makes the merge guard in flushLiveFetches
    // preserve our stale (often null) detail over the bulk
    // fetch's actual data, leaving the row stuck on the loading
    // placeholder. (Caught when the screenshot showed buttons
    // briefly then loading replaced them.)
    const detailClearedAuthoritatively =
      !pendingApproval && cachedLive.pending_approval === true;
    if (!pendingApproval) {
      nextLive.pending_approval_detail = null;
    }
    _liveBadgeCacheSet(childId, {
      live: nextLive,
      // Preserve prior bulk-poll fetched timestamp so a fresh SSE
      // tick doesn't artificially extend the 5s TTL gate in
      // scheduleLiveFetch — the bulk-poll still drives slower-
      // moving fields (tokens, context_ratio) on its own schedule.
      fetched: cached ? cached.fetched : 0,
      permanent: !!(cached && cached.permanent),
      sseUpdatedAt: detailClearedAuthoritatively
        ? Date.now()
        : cached
          ? cached.sseUpdatedAt || 0
          : 0,
    });
    renderChildren();
    // Do NOT invalidateLiveBadge on routine state ticks — that
    // defeats the 5s TTL cache and devolves rate-limiting to the
    // 250ms debouncer.  The TTL check in scheduleLiveFetch handles
    // refresh cadence for slower-moving fields; identity-changing
    // events (created/rename/closed) still invalidate below.
    scheduleLiveFetch(childId);
  }

  function handleChildClosed(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    const existing = childrenState.get(childId);
    if (!existing) return;
    existing.state = ev.reason === "deleted" ? "deleted" : "closed";
    // Clearing the live cache eagerly on close prevents a stale
    // pending_approval_detail from continuing to render approve/deny
    // buttons on a closed row (its TTL would otherwise survive into
    // the closed/deleted lifecycle until natural expiry).
    invalidateLiveBadge(childId);
    childrenState.set(childId, existing);
    _touchChild(childId);
    renderChildren();
  }

  function handleChildRename(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    const existing = childrenState.get(childId);
    if (!existing) return;
    if (ev.name) existing.name = ev.name;
    childrenState.set(childId, existing);
    _touchChild(childId);
    renderChildren();
  }

  // Stage 3 Step 7 — verdict + approval reducer.
  //
  // Both write directly to liveBadgeCache and skip scheduleLiveFetch,
  // which sidesteps the visibility gate in that path so off-screen
  // rows pick up the new cache value the moment they scroll back in.
  // Both are idempotent — the bulk-fetch (single source of truth for
  // the items list) and the explicit intent_verdict event can deliver
  // overlapping data; receiving twice re-stamps the same value.
  //
  // Both call ``_updateChildRow`` (targeted single-row swap) instead
  // of ``renderChildren`` so a verdict landing on row 3 doesn't rebuild
  // every other row in a 200-child sidebar — which would also blow
  // away keyboard focus on whatever row the operator was tabbing to.

  function handleChildIntentVerdict(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    const verdict = ev.verdict || {};
    const callId = verdict.call_id || "";
    if (!callId) return;
    const cached = liveBadgeCache.get(childId);
    const cachedLive = (cached && cached.live) || {};
    const detail = cachedLive.pending_approval_detail;
    if (!detail) {
      // No pending_approval_detail to stamp the verdict onto. The
      // verdict is still durable in storage; the next bulk fetch
      // will hydrate the detail and include the verdict via the
      // existing serialize path.
      return;
    }
    // Stamp on the matching item (UI render reads judge_verdict per
    // item) AND on the by-call_id map (matches the
    // serialize_pending_approval_detail shape).
    const items = Array.isArray(detail.items) ? detail.items : [];
    for (const item of items) {
      if (item && item.call_id === callId) {
        item.judge_verdict = verdict;
        break;
      }
    }
    if (!detail.llm_verdicts) detail.llm_verdicts = {};
    detail.llm_verdicts[callId] = verdict;
    // judge_pending flips false once every item has a verdict —
    // matches the server-side serializer's logic.
    if (items.length > 0) {
      detail.judge_pending = !items.every((it) => it && it.judge_verdict);
    }
    _liveBadgeCacheSet(childId, {
      live: cachedLive,
      fetched: cached ? cached.fetched : 0,
      permanent: !!(cached && cached.permanent),
      sseUpdatedAt: Date.now(),
    });
    _updateChildRow(childId);
  }

  function handleChildApprovalResolved(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    const cached = liveBadgeCache.get(childId);
    const cachedLive = (cached && cached.live) || {};
    cachedLive.pending_approval = false;
    cachedLive.pending_approval_detail = null;
    _liveBadgeCacheSet(childId, {
      live: cachedLive,
      fetched: cached ? cached.fetched : 0,
      permanent: !!(cached && cached.permanent),
      sseUpdatedAt: Date.now(),
    });
    _updateChildRow(childId);
  }

  // Push path for the initial approval items — eliminates the
  // bulk-fetch race that previously left rows stuck on a loading
  // placeholder when the bulk fetch landed in the gap between the
  // state transition to ATTENTION and ``_pending_approval`` being
  // set inside ``approve_tools``. Stamps the items into the cache
  // directly; the bulk fetch remains as a reconnect / refresh
  // fallback. Idempotent — receiving the same approve_request twice
  // re-stamps the same detail.
  function handleChildApproveRequest(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    const detail = ev.detail || null;
    if (!detail) return;
    const cached = liveBadgeCache.get(childId);
    const cachedLive = (cached && cached.live) || {};
    cachedLive.pending_approval = true;
    cachedLive.pending_approval_detail = detail;
    _liveBadgeCacheSet(childId, {
      live: cachedLive,
      fetched: cached ? cached.fetched : 0,
      permanent: !!(cached && cached.permanent),
      sseUpdatedAt: Date.now(),
    });
    _updateChildRow(childId);
  }

  // Periodic sweep of stale terminal rows.  Operator tabs left open all
  // day would otherwise accumulate entries for every child the
  // coordinator ever spawned — rows the user can still see (state !=
  // terminal, or touched within the grace window) are kept; everything
  // else gets dropped along with its liveBadgeCache entry.  Also
  // enforces a hard cap as a belt-and-braces fallback.
  function _pruneChildren() {
    const now = Date.now();
    let removed = 0;
    for (const [id, entry] of childrenState) {
      const terminal = TERMINAL_CHILD_STATES.has(entry.state);
      const lastSeen = childrenLastSeen.get(id) || 0;
      if (terminal && now - lastSeen > CHILDREN_TERMINAL_GRACE_MS) {
        childrenState.delete(id);
        childrenLastSeen.delete(id);
        _liveBadgeCacheDelete(id);
        visibleChildIds.delete(id);
        removed += 1;
      }
    }
    // Hard cap — drop oldest-touched until under the limit.  Should
    // rarely fire in practice; defends against pathological churn.
    if (childrenState.size > CHILDREN_HARD_CAP) {
      const byAge = Array.from(childrenLastSeen.entries()).sort(
        (a, b) => a[1] - b[1],
      );
      const excess = childrenState.size - CHILDREN_HARD_CAP;
      for (let i = 0; i < excess && i < byAge.length; i += 1) {
        const id = byAge[i][0];
        childrenState.delete(id);
        childrenLastSeen.delete(id);
        _liveBadgeCacheDelete(id);
        visibleChildIds.delete(id);
        removed += 1;
      }
    }
    if (removed > 0) {
      renderChildren();
    }
  }
  setInterval(_pruneChildren, CHILDREN_PRUNE_INTERVAL_MS);

  if (childrenRefreshBtn) {
    childrenRefreshBtn.addEventListener("click", () => {
      _liveBadgeCacheClear();
      // Explicit refresh wipes SSE-discovered rows the server no
      // longer knows about — the operator asked for a clean snapshot.
      loadChildren({ replace: true });
    });
  }
  if (tasksRefreshBtn) {
    tasksRefreshBtn.addEventListener("click", () => {
      loadTasks();
    });
  }

  // Mobile-only sidebar toggle — wires the accordion collapse below 700px.
  // On desktop the button is display:none so the handler is a no-op.
  const sidebarEl = document.getElementById("coord-sidebar");
  const sidebarToggle = document.getElementById("coord-sidebar-toggle");
  const sidebarToggleGlyph = document.getElementById(
    "coord-sidebar-toggle-glyph",
  );
  if (sidebarEl && sidebarToggle) {
    sidebarToggle.addEventListener("click", () => {
      const expanded = sidebarEl.getAttribute("aria-expanded") !== "false";
      const next = !expanded;
      sidebarEl.setAttribute("aria-expanded", next ? "true" : "false");
      sidebarToggle.setAttribute("aria-expanded", next ? "true" : "false");
      if (sidebarToggleGlyph) {
        sidebarToggleGlyph.textContent = next ? "\u25BE" : "\u25B8"; // ▾ / ▸
      }
    });
  }

  // ------------------------------------------------------------------
  // Initial load — history then SSE
  // ------------------------------------------------------------------

  async function init() {
    let wsSnapshot = null;
    try {
      wsSnapshot = await getJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId),
      );
      nameEl.textContent = wsSnapshot.name || "";
      statusEl.textContent = wsSnapshot.state || "";
    } catch (e) {
      appendText("error", "Failed to load coordinator: " + e.message);
      return;
    }
    try {
      const hist = await getJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/history",
      );
      // Map call_id → tool name resolved from the most recent
      // assistant tool_calls.  Storage's `tool` rows carry only
      // tool_call_id + content; the function name lives on the
      // matching assistant entry — without this map every replayed
      // tool result rendered with the literal label "tool", which
      // looked like the tool calls had been replaced by raw JSON.
      const toolNameByCallId = new Map();

      // Pre-scan every tool message's tool_call_id so the
      // assistant.tool_calls branch below knows whether each call_id
      // already has a result persisted.  An assistant tool_calls turn
      // with NO matching tool result for some call_ids = orphan: the
      // tool was dispatched but didn't complete before the reload
      // captured this history snapshot.  Orphans are ambiguous — the
      // tool could have been (a) awaiting approval at reload, (b)
      // auto-approved + still in flight, or (c) approved + still in
      // flight.  We render orphans as a neutral --running shell with
      // no actions; SSE then upgrades to --pending when it replays
      // approve_request (case a) or to --auto when it replays
      // tool_info (case b), and tool_result events land in the rows
      // for case c.  Without this neutral state, painting Approve
      // buttons on a non-pending orphan was misleading and could
      // 409-on-submit because the call_id wasn't in pending_items.
      // Pre-scan classifies each tool message's content as
      // "denied" (server emits "Denied by user[: feedback]" /
      // "Blocked by tool policy ..." for any deny path), "error"
      // (Error: prefix from the standard tool error envelope), or
      // "ok" (anything else).  Map keys are call_ids; the assistant
      // tool_calls render below reads these to mark the resulting
      // batch as resolved-denied (vs the default resolved-approved)
      // and the tool-result render below propagates the error flag
      // to appendToolResult so the row gets the .error class /
      // --error stripe / "✗ error:" lead.  Without this both denied
      // and errored tools rendered as plain "✓ approved" successes
      // on every reload.
      const callOutcomes = new Map();
      (hist.messages || []).forEach((m) => {
        if ((m.role || "tool") !== "tool" || !m.tool_call_id) return;
        // Tool message content is usually a string but the multipart
        // shape (text + image_url parts) is technically allowed.
        let txt = "";
        if (typeof m.content === "string") {
          txt = m.content;
        } else if (Array.isArray(m.content)) {
          for (const part of m.content) {
            if (part && typeof part === "object" && part.type === "text") {
              txt += String(part.text || "");
            }
          }
        }
        // Server signals override prefix detection when present.
        let outcome = "ok";
        if (m.is_error) {
          outcome = "error";
        } else if (
          txt.startsWith("Denied by user") ||
          txt.startsWith("Blocked by tool policy")
        ) {
          outcome = "denied";
        } else if (txt.startsWith("Error:")) {
          outcome = "error";
        }
        callOutcomes.set(m.tool_call_id, outcome);
      });

      (hist.messages || []).forEach((m) => {
        const role = m.role || "tool";

        // Assistant tool_calls — synthesize one batch construct per
        // assistant turn so a parallel fan-out (tool_calls.length ≥ 2)
        // reads as one cohesive dispatch, matching how live SSE
        // renders the same flow via approve_request / tool_info.
        // Resolved when every call_id has a matching tool result;
        // otherwise --running (see the resolvedCallIds rationale
        // above).  SSE upgrades --running in place when it knows
        // more.
        if (
          role === "assistant" &&
          Array.isArray(m.tool_calls) &&
          m.tool_calls.length
        ) {
          const items = m.tool_calls.map((tc) => {
            const fn = (tc && tc.function) || {};
            const name = String(fn.name || "tool");
            const callId = String((tc && tc.id) || "");
            const argsRaw = String(fn.arguments || "");
            let parsedArgs = null;
            try {
              parsedArgs = JSON.parse(argsRaw || "{}");
            } catch (_) {
              /* malformed — fall back to raw string in preview */
            }
            if (callId) toolNameByCallId.set(callId, name);
            const item = synthesizeHistoricalToolCall(
              name,
              callId,
              parsedArgs,
              argsRaw,
            );
            // Server attaches the persisted intent_verdict to each
            // tc on /history (newest-wins per call_id; LLM upgrade
            // beats heuristic when both exist).  Stamp on the item
            // under the field name the render path already consumes
            // (judge_verdict for LLM tier, heuristic_verdict
            // otherwise) so the verdict pill paints on history rows
            // without a render-path fork.  Also seed the
            // judgeVerdicts cache so a later live SSE event for the
            // same call_id reads "already painted" and skips the
            // rebuild.
            if (tc && tc.verdict) {
              if (tc.verdict.tier === "llm") {
                item.judge_verdict = tc.verdict;
              } else {
                item.heuristic_verdict = tc.verdict;
              }
              if (callId) _cacheJudgeVerdict(callId, tc.verdict);
            }
            // Output-guard finding — surface as the same
            // "[output guard] ..." chat line the live handler emits
            // (case "output_warning" above).  Stamp on the item so
            // the post-batch loop below can read + emit; rendering
            // anchored next to the call gives the operator the same
            // adjacency they'd see live.
            if (tc && tc.output_assessment) {
              item.output_assessment = tc.output_assessment;
            }
            // needs_approval is unknown at replay time (the
            // assistant.tool_calls history payload doesn't persist
            // the bit).  Leave it unset; the upgrade-in-place path
            // refreshes per-row state via _refreshRowStatus from the
            // authoritative SSE item when approve_request /
            // tool_info actually arrives, so we never tag the wrong
            // row as needing approval.
            return item;
          });
          // Classify the batch as a whole:
          //   - any call_id without an outcome at all → orphan,
          //     render as --running (SSE will upgrade in place)
          //   - any call_id outcome === "denied" → resolved-denied
          //   - else → resolved-approved (a runtime error doesn't
          //     change the approval verdict; the per-row .error class
          //     comes from the tool_result branch below)
          const outcomes = items.map((it) =>
            it.call_id ? callOutcomes.get(it.call_id) : "ok",
          );
          const allResolved = outcomes.every((o) => o !== undefined);
          if (!allResolved) {
            appendToolBatch(items, { running: true });
          } else if (outcomes.some((o) => o === "denied")) {
            appendToolBatch(items, { resolved: { approved: false } });
          } else {
            appendToolBatch(items, { resolved: { approved: true } });
          }
          // Output-guard findings — render each one as a chip
          // anchored to the .coord-tool-row that tripped the guard
          // rather than a generic "[output guard]" chat line.
          // Anchored placement preserves per-call adjacency on
          // multi-tool batches (live + replay) and the chip's
          // severity styling makes the visual weight match the
          // verdict pill on the same row.
          for (let oi = 0; oi < items.length; oi++) {
            const oa = items[oi].output_assessment;
            if (!oa || !oa.risk_level || oa.risk_level === "none") continue;
            const cid = items[oi].call_id || "";
            if (!cid) continue;
            const entry = toolRows.get(cid);
            if (!entry || !entry.row) continue;
            _attachOutputWarningChip(entry.row, oa);
          }
        }

        // User messages with attachments arrive as multipart list
        // content (text + image_url/document parts) and may carry an
        // ``_attachments_meta`` side-channel with display metadata.
        // Extract the text portion + attachment count for a readable
        // history replay; chip-rendering parity with the interactive
        // pane is deferred (the coord dashboard is diagnostic-leaning
        // — primary use is monitoring, not authoring).
        let content;
        let attachmentCount = 0;
        if (typeof m.content === "string") {
          content = m.content;
        } else if (Array.isArray(m.content)) {
          const textParts = [];
          for (const part of m.content) {
            if (!part || typeof part !== "object") continue;
            if (part.type === "text") {
              textParts.push(String(part.text || ""));
            } else if (part.type === "image_url" || part.type === "document") {
              attachmentCount += 1;
            }
          }
          content = textParts.join("\n");
        } else {
          content = JSON.stringify(m.content || "");
        }
        const meta = Array.isArray(m._attachments_meta)
          ? m._attachments_meta
          : null;
        if (meta && meta.length > attachmentCount) {
          // Prefer the side-channel count when present — it covers
          // attachments whose multipart parts couldn't be reconstructed.
          attachmentCount = meta.length;
        }
        if (attachmentCount > 0) {
          const noun = attachmentCount === 1 ? "attachment" : "attachments";
          content =
            (content ? content + "\n\n" : "") +
            "📎 " +
            attachmentCount +
            " " +
            noun;
        }
        if (role === "tool") {
          // Tool result content can legitimately be empty (e.g. a
          // tool that returned ""); still render it so the call_id
          // pairing stays visible.  Resolve the tool name from the
          // matching assistant tool_call so the label reads e.g.
          // "bash" instead of "tool".  Pass isError when the
          // pre-scan classified this call_id as an error so the row
          // gets the .error class / --error stripe / "✗ error:" lead
          // — without it a failed tool reads on reload as a normal
          // successful result.  Denials still get the deny-resolved
          // batch state (no per-row error needed there; the row's
          // content reads "Denied by user").
          const callId = m.tool_call_id || "";
          const toolName =
            (callId && toolNameByCallId.get(callId)) || m.tool_name || "tool";
          const isError = callOutcomes.get(callId) === "error";
          // Storage truncation surfaces as a sibling pill next to
          // the result (see _appendResultToRow's opts.truncated
          // branch) rather than as text inside the result body — a
          // future "best-effort JSON repair" pass would otherwise
          // need to strip a marker string before parsing.
          appendToolResult(toolName, callId, content || "", isError, {
            truncated: !!m.truncated,
          });
          // Tool-channel metacog reminders ride the same _reminders
          // side-channel as the user channel; surface as a themed
          // bubble below the .coord-tool-batch construct.
          if (Array.isArray(m.reminders) && m.reminders.length) {
            appendToolReminderLive(m.reminders, callId);
          }
        } else if (role === "assistant") {
          // Empty content with tool_calls only means the assistant
          // turn was just tool dispatch — the synthesized tool-call
          // rows above already cover it; skip the empty bubble.
          if (!content) return;
          // Run assistant content through the markdown pipeline
          // (renderMarkdown + post-render hljs / mermaid / KaTeX) so a
          // reconnect / page-reload renders the same way a live stream
          // does.  appendText would only escape and dump the raw text —
          // markdown tables, code fences, math, and links would all
          // render as literal characters.
          const el = appendMsg(role, "", { label: role });
          const body = el.querySelector(".msg-body");
          if (body && typeof streamingRenderFinalize === "function") {
            try {
              streamingRenderFinalize(body, content);
            } catch (e) {
              console.warn("coordinator history render failed", e);
              body.textContent = content;
            }
          } else if (body) {
            body.textContent = content;
          }
        } else {
          if (!content) return;
          // user / reasoning / system / other roles render as plain
          // text on history replay — matches the live-streaming paths
          // (appendReasoningToken uses textContent; user/system are
          // typed verbatim and don't carry markdown structure).
          appendText(role, content, { label: role });
          // User-channel metacog reminders attach to the just-appended
          // user bubble (the most recent .msg.user in messagesEl).
          if (
            role === "user" &&
            Array.isArray(m.reminders) &&
            m.reminders.length
          ) {
            appendUserReminderLive(m.reminders);
          }
        }
      });
      // History alone can't tell whether an orphaned assistant
      // tool_calls turn is awaiting approval or merely still running.
      // The live workstream snapshot can: if pending_approval_detail is
      // present, upgrade the matching batch immediately so a reload
      // still exposes Approve/Deny even before SSE reconnects.
      const pendingDetail =
        wsSnapshot &&
        wsSnapshot.pending_approval &&
        wsSnapshot.pending_approval_detail &&
        Array.isArray(wsSnapshot.pending_approval_detail.items)
          ? wsSnapshot.pending_approval_detail
          : null;
      if (pendingDetail) {
        appendToolBatch(pendingDetail.items, {
          pending: true,
          judgePending: !!pendingDetail.judge_pending,
        });
      }
    } catch (e) {
      console.warn("history load failed", e);
    }
    // Load children + tasks in parallel — neither blocks SSE connection.
    loadChildren();
    loadTasks();
    // Pull any in-flight attachment reservations (page reload / cross-tab
    // switch) so the chips reappear instead of silently orphaning rows.
    attachments.rehydrate();
    connectSSE();
  }

  init();

  // When the user re-authenticates after a 401 (see SSE onerror above),
  // reset the backoff and force an immediate reconnect so the stream
  // resumes without waiting out the current Math.pow backoff window.
  window.onLoginSuccess = function () {
    reconnectAttempts = 0;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    connectSSE();
  };

  // Enter-to-send / Shift-Enter newline / IME-safe handling lives in
  // shared/composer.js; no duplicate listener here.
})();
