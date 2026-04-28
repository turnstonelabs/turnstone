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
  const approvalBar = document.getElementById("coord-approval-bar");
  const approvalTools = document.getElementById("coord-approval-tools");
  const childrenTreeEl = document.getElementById("coord-children-tree");
  const childrenCountEl = document.getElementById("coord-children-count");
  const childrenRefreshBtn = document.getElementById("coord-children-refresh");
  const tasksEl = document.getElementById("coord-tasks");
  const tasksCountEl = document.getElementById("coord-tasks-count");
  const tasksRefreshBtn = document.getElementById("coord-tasks-refresh");

  let pendingApprovalCallId = null;
  let evtSource = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;

  // Cache of judge verdicts keyed by call_id.  Judge events (intent_verdict)
  // and approval events (approve_request) are async and can arrive in either
  // order; the cache lets each handler apply data to the other without
  // assuming ordering.
  const judgeVerdicts = new Map();

  // Approval focus is deferred until the judge returns a verdict so the
  // Approve button doesn't pre-emptively light up (could read as "already
  // approved").  No fallback — if the judge never responds (disabled /
  // slow), focus simply never moves; keyboard users tab from the
  // composer to reach the buttons manually.  A fallback would produce
  // an ambiguous focus ring that could be misread as "judge approved."
  let approvalFocusClaimed = false;

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
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function appendText(role, text, opts) {
    return appendMsg(role, esc(text), opts);
  }

  function appendToolCall(item) {
    const label = item.func_name || "tool";
    const html =
      "<strong>" +
      esc(item.header || label) +
      "</strong>" +
      (item.preview ? "<pre>" + esc(item.preview) + "</pre>" : "");
    return appendMsg("tool", html, { label: label, callId: item.call_id });
  }

  function appendToolResult(name, callId, output, isError) {
    const html = renderToolOutput(output);
    const el = appendMsg(isError ? "error" : "tool", html, {
      label: (isError ? "error · " : "") + (name || "tool"),
      callId: callId,
    });
    return el;
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
    messagesEl.scrollTop = messagesEl.scrollHeight;
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
    messagesEl.scrollTop = messagesEl.scrollHeight;
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
  // Approval UI
  // ------------------------------------------------------------------

  function showApproval(items) {
    approvalTools.replaceChildren();
    const pending = (items || []).filter((it) => it.needs_approval);
    // Count badge — shown in the .dhead row's trailing .dcount slot.
    // The "Approval required" kicker is static in the HTML so the
    // screen-reader label stays stable across open/close cycles.
    const countEl = document.getElementById("coord-approval-count");
    if (countEl) {
      countEl.textContent = pending.length ? pending.length + " pending" : "";
    }
    let firstCallId = null;
    pending.forEach((it, idx) => {
      if (!firstCallId) firstCallId = it.call_id;
      // Each pending tool call renders as a .dcall row — the DS pattern
      // frames this like a mini inspectable call line.  If we've already
      // received a judge verdict for this call_id, its risk_level takes
      // precedence; otherwise default to "low" until a verdict arrives.
      const row = document.createElement("div");
      row.className = "dcall";
      if (it.call_id) row.dataset.callId = it.call_id;
      if (pending.length > 1) {
        const idx_ = document.createElement("span");
        idx_.className = "risk low";
        idx_.textContent = idx + 1 + "/" + pending.length;
        row.appendChild(idx_);
      }
      const fn = document.createElement("span");
      fn.className = "dfn";
      fn.textContent = it.func_name || "(unknown tool)";
      row.appendChild(fn);
      const args = document.createElement("span");
      args.className = "dargs";
      const preview = it.header || it.approval_label || it.preview || "";
      args.textContent = preview;
      row.appendChild(args);
      approvalTools.appendChild(row);
      // If the judge already delivered a verdict for this call before the
      // approval surfaced, render it into the dock immediately.  Otherwise
      // render a spinner placeholder so the reviewer sees "the judge is
      // still thinking" instead of silence.
      if (it.call_id && judgeVerdicts.has(it.call_id)) {
        applyJudgeVerdictToRow(row, judgeVerdicts.get(it.call_id));
      } else {
        applyJudgePendingToRow(row);
      }
    });
    pendingApprovalCallId = firstCallId;
    approvalBar.hidden = false;
    // Defer focus until the judge returns a verdict.  If the verdict is
    // already cached (rare race), claim focus immediately.  Otherwise
    // wait for intent_verdict — no fallback.
    approvalFocusClaimed = false;
    if (firstCallId && judgeVerdicts.has(firstCallId)) {
      claimApprovalFocusForVerdict(judgeVerdicts.get(firstCallId));
    }
  }

  // Claim approval-bar focus for a specific button.  Idempotent — further
  // calls after the first are noops so we don't bounce focus across
  // multiple buttons as verdicts arrive for a batch.
  function claimApprovalFocus(btnId) {
    if (approvalFocusClaimed) return;
    approvalFocusClaimed = true;
    const btn = document.getElementById(btnId);
    if (btn) {
      try {
        btn.focus({ preventScroll: false });
      } catch (_) {
        /* noop */
      }
    }
  }

  // Focus the appropriate action based on the judge's recommendation:
  // deny → Deny button (judge is warning; reviewer should default to
  // blocking), approve/review/other → Approve button.
  function claimApprovalFocusForVerdict(verdict) {
    const rec = (verdict && verdict.recommendation) || "";
    claimApprovalFocus(rec === "deny" ? "coord-deny-btn" : "coord-approve-btn");
  }

  // Ensure a .dctx sibling exists for the given .dcall row, tagged with
  // the row's call_id.  Returns the .dctx element ready to be populated.
  function ensureDctxAfterRow(row) {
    if (!row) return null;
    const callId = row.dataset.callId;
    let dctx = row.nextElementSibling;
    if (
      !dctx ||
      !dctx.classList.contains("dctx") ||
      dctx.dataset.forCall !== callId
    ) {
      dctx = document.createElement("div");
      dctx.className = "dctx";
      if (callId) dctx.dataset.forCall = callId;
      row.insertAdjacentElement("afterend", dctx);
    }
    return dctx;
  }

  // Remove any existing .drationale sibling for the given call_id so
  // repeated verdicts don't stack.
  function removeRationale(callId) {
    if (!callId) return;
    const existing = approvalTools.querySelector(
      '.drationale[data-for-call="' + cssEscape(callId) + '"]',
    );
    if (existing) existing.remove();
  }

  // Render a "judge evaluating…" placeholder into the .dctx so the
  // reviewer sees the judge is still thinking while awaiting verdict.
  function applyJudgePendingToRow(row) {
    const dctx = ensureDctxAfterRow(row);
    if (!dctx) return;
    removeRationale(row.dataset.callId);
    dctx.replaceChildren();
    const chip = document.createElement("code");
    chip.className = "judging";
    const spin = document.createElement("span");
    spin.className = "spin";
    spin.setAttribute("aria-hidden", "true");
    chip.appendChild(spin);
    chip.appendChild(document.createTextNode("judge evaluating…"));
    dctx.appendChild(chip);
  }

  // Render a judge verdict into a specific .dcall row.  Builds:
  //   .dctx    <code>judge: rec (risk: lvl)</code> + optional confidence
  //   .drationale  judge.reasoning text (wrapped prose block)
  // The judge chip colour-codes by recommendation: approve=ok/green,
  // review=warn/amber, deny=err/red — so reviewers can triage at a
  // glance without reading.  Repeated calls for the same row (e.g.
  // re-evaluation) replace prior content.
  function applyJudgeVerdictToRow(row, verdict) {
    if (!row || !verdict) return;
    const callId = row.dataset.callId;
    const dctx = ensureDctxAfterRow(row);
    removeRationale(callId);
    dctx.replaceChildren();
    const chip = document.createElement("code");
    const rec = verdict.recommendation || "?";
    const risk = verdict.risk_level || "?";
    chip.textContent = "judge: " + rec + " (risk: " + risk + ")";
    if (rec === "approve") chip.classList.add("rec-approve");
    else if (rec === "review") chip.classList.add("rec-review");
    else if (rec === "deny") chip.classList.add("rec-deny");
    dctx.appendChild(chip);
    if (verdict.confidence != null) {
      const conf = document.createElement("code");
      conf.textContent = "confidence: " + verdict.confidence;
      dctx.appendChild(conf);
    }
    if (verdict.reasoning) {
      const rationale = document.createElement("div");
      rationale.className = "drationale";
      if (callId) rationale.dataset.forCall = callId;
      rationale.textContent = verdict.reasoning;
      dctx.insertAdjacentElement("afterend", rationale);
    }
  }

  function hideApproval() {
    approvalBar.hidden = true;
    approvalTools.replaceChildren();
    const countEl = document.getElementById("coord-approval-count");
    if (countEl) countEl.textContent = "";
    pendingApprovalCallId = null;
    approvalFocusClaimed = false;
    // Prune the verdict cache — verdicts are only used while the dock
    // is visible, so keeping them across resolve cycles would leak
    // memory over long sessions.
    if (judgeVerdicts && typeof judgeVerdicts.clear === "function") {
      judgeVerdicts.clear();
    }
    setApprovalButtonsDisabled(false);
    // Return focus to the composer for keyboard users.  Only if the
    // approval bar itself was the focus holder — don't steal focus from
    // e.g. a user who clicked into the history log.
    if (
      document.activeElement &&
      approvalBar.contains(document.activeElement)
    ) {
      try {
        composer.focus();
      } catch (_) {
        /* noop */
      }
    }
  }

  function setApprovalButtonsDisabled(disabled) {
    // Disable all three approval buttons during an in-flight POST to
    // prevent double-submit via double-click / Enter-hold.  The bar
    // either dismisses on success or re-enables on error.
    ["coord-approve-btn", "coord-approve-always-btn", "coord-deny-btn"].forEach(
      (id) => {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = !!disabled;
      },
    );
  }

  // Generic approve POST — usable for both the coord-self dock and
  // the per-child inline buttons in the children-tree. Returns the
  // response so callers can inspect 409 (stale call_id) bodies and
  // refresh their local state.
  //
  // The path differs by target: the coord workstream is hosted on
  // the console process itself (lifted verbs at /v1/api/workstreams/
  // {coord_ws_id}/approve), but child workstreams live on cluster
  // nodes and need to round-trip through the routing proxy at
  // /v1/api/route/workstreams/{child_ws_id}/approve which resolves
  // the ws_id to its owning node and forwards the body verbatim.
  // Without the /route/ prefix children always 404 because the
  // console doesn't host them.
  async function approveWorkstream(targetWsId, body) {
    const isSelf = targetWsId === wsId;
    const path = isSelf
      ? "/v1/api/workstreams/" + encodeURIComponent(targetWsId) + "/approve"
      : "/v1/api/route/workstreams/" +
        encodeURIComponent(targetWsId) +
        "/approve";
    return postJSON(path, body);
  }

  window.coordApprove = async function (approved, always) {
    if (!pendingApprovalCallId) return; // no-op if bar already resolved
    const body = {
      approved: !!approved,
      always: !!always,
      call_id: pendingApprovalCallId,
    };
    setApprovalButtonsDisabled(true);
    try {
      const resp = await approveWorkstream(wsId, body);
      if (!resp.ok) throw new Error("approve failed: HTTP " + resp.status);
    } catch (e) {
      setApprovalButtonsDisabled(false);
      if (typeof toast !== "undefined" && toast.error) toast.error(String(e));
      else console.error(e);
      return;
    }
    hideApproval();
  };

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
          if (!c || !c.permanent) liveBadgeCache.delete(id);
        }
      }
    };
    evtSource.onerror = function () {
      setSseStatus("disconnected", "err");
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
        showApproval(ev.items);
        // Surface each tool for context.  Dedupe by call_id: the console
        // replays _pending_approval into every new SSE subscriber (the
        // events SSE handler's coord-side replay), so without this check
        // an SSE reconnect would render each tool row a second time.
        (ev.items || []).forEach((it) => {
          if (!it.needs_approval) return;
          if (
            it.call_id &&
            document.querySelector(
              '.msg[data-call-id="' + cssEscape(it.call_id) + '"]',
            )
          ) {
            return; // already rendered in this pane — skip
          }
          appendToolCall(it);
        });
        break;
      case "approval_resolved":
        hideApproval();
        break;
      case "intent_verdict":
        // Prefer rendering the verdict inside the approval dock — the
        // judge always evaluates a specific call_id, and the verdict is
        // decision context for that pending approval, not a chat
        // message.  Cache the verdict so late-arriving approve_request
        // events can also pick it up (see showApproval).
        if (ev.call_id) {
          judgeVerdicts.set(ev.call_id, {
            recommendation: ev.recommendation,
            risk_level: ev.risk_level,
            confidence: ev.confidence,
            reasoning: ev.reasoning,
          });
          const row = approvalTools.querySelector(
            '.dcall[data-call-id="' + cssEscape(ev.call_id) + '"]',
          );
          if (row) {
            applyJudgeVerdictToRow(row, judgeVerdicts.get(ev.call_id));
            // Claim focus now that the reviewer has context to act on.
            // Only for the first-pending call so batch approvals don't
            // fight over focus as verdicts trickle in.
            if (ev.call_id === pendingApprovalCallId && !approvalFocusClaimed) {
              claimApprovalFocusForVerdict(judgeVerdicts.get(ev.call_id));
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
        appendText(
          "error",
          "[output guard] " +
            (ev.risk_level || "?") +
            ": " +
            (ev.flags || []).join(","),
          { label: "warning" },
        );
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
        // for both kinds, matching the interactive payload name.
        (ev.items || []).forEach((it) => appendToolCall(it));
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
    // Inline approve/deny block \u2014 shown only when the live block
    // carries pending_approval_detail (the rich payload added by the
    // server-side dashboard projection).  A "\u2691 approval" badge alone
    // means the child is in attention state but the rich detail hasn't
    // arrived yet (urgent live-bulk fetch is in flight); the row gets
    // re-rendered when it lands.
    if (cached && cached.live && cached.live.pending_approval_detail) {
      const detail = cached.live.pending_approval_detail;
      const block = renderApprovalBlock(child, detail);
      if (block) row.appendChild(block);
      // Late-arriving LLM judge: see comment above
      // _maybeStartJudgePoll for the why. Hooks into a single
      // global poller (not per-row) so off-screen rows still
      // refresh and one bulk request covers every pending row.
      _maybeStartJudgePoll();
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

  // Late-judge polling — single global timer driving a bulk
  // re-fetch of every row whose `pending_approval_detail.judge_pending`
  // is true and not every item has a `judge_verdict` yet.  Necessary
  // because the LLM judge runs async on the child node and never
  // pushes a signal that reaches the coord; without polling the
  // operator would stare at a "heuristic"-tier pill forever.
  //
  // Why a global poller instead of per-row:
  //  1. Per-row would invalidate the cache and call scheduleLiveFetch
  //     for off-screen rows, but scheduleLiveFetch returns early on
  //     non-visible ids — leaving the cache empty AND no fetch in
  //     flight.  Result: off-screen rows stuck on stale heuristic.
  //  2. A single bulk request covers every pending row in one
  //     round-trip; the per-row design would issue N microtask-
  //     batched fetches that share the bulk path anyway.
  const JUDGE_POLL_INTERVAL_MS = 2000;
  // Cap by total wall-clock time, not attempts. Real LLM judges
  // (esp. with reasoning effort) can exceed 30s — a 6-attempt /
  // 12s cap was prematurely giving up.
  const JUDGE_POLL_MAX_DURATION_MS = 90_000;
  let judgePollTimer = null;
  let judgePollStartedAt = 0;

  function _maybeStartJudgePoll() {
    if (judgePollTimer !== null) return; // already polling
    judgePollStartedAt = Date.now();
    judgePollTimer = setTimeout(_judgePollTick, JUDGE_POLL_INTERVAL_MS);
  }

  function _judgePollTick() {
    judgePollTimer = null;
    if (Date.now() - judgePollStartedAt > JUDGE_POLL_MAX_DURATION_MS) {
      // Failed / timed-out judge — give up so we don't poll forever.
      // Operator can hit the Refresh button to force a fresh fetch.
      return;
    }
    // Walk the full childrenState — not just visible — so off-screen
    // rows still get refreshed.  scheduleLiveFetch's visibility gate
    // exists to keep idle rows from burning round-trips; here we
    // explicitly want every pending-judge row in the next bulk
    // regardless of viewport.
    let stillPending = false;
    for (const [wsId, entry] of childrenState) {
      if (TERMINAL_CHILD_STATES.has(entry.state)) continue;
      const cached = liveBadgeCache.get(wsId);
      if (!cached || !cached.live) continue;
      const detail = cached.live.pending_approval_detail;
      if (!detail || !detail.judge_pending) continue;
      const items = Array.isArray(detail.items) ? detail.items : [];
      const allHaveJudge =
        items.length > 0 && items.every((it) => it.judge_verdict);
      if (allHaveJudge) continue;
      // Bypass scheduleLiveFetch entirely (skips visibility + TTL
      // gates) by adding to pendingLiveIds directly. flushLiveFetches
      // will batch every pending row into one request.
      if (WS_ID_RE.test(wsId)) {
        pendingLiveIds.add(wsId);
        stillPending = true;
      }
    }
    if (!stillPending) return; // every verdict landed — done
    // Cancel any debounce that's still pending so our flush runs
    // now instead of waiting for it. Then flush directly so the
    // bulk request fires before the next tick re-arms.
    if (liveBadgeFlushTimer !== null) {
      clearTimeout(liveBadgeFlushTimer);
      liveBadgeFlushTimer = null;
    }
    flushLiveFetches();
    judgePollTimer = setTimeout(_judgePollTick, JUDGE_POLL_INTERVAL_MS);
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
        // disabled until the urgent refresh lands and re-renders
        // the row: the row is about to be replaced wholesale, so
        // the disabled DOM is dropped along with it. Re-enabling
        // here was the bug \u2014 it opened a window where rapid clicks
        // hit the same already-resolved approval, each producing a
        // fresh 409, looping until the live-bulk eventually cleared
        // the row. On the rare path where the urgent refresh fails
        // entirely, the operator can hit the Refresh button on the
        // children panel to force a full reload.
        invalidateLiveBadge(targetWsId);
        scheduleLiveFetch(targetWsId, { urgent: true });
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
      // makes the buttons disappear immediately on click.
      const cached = liveBadgeCache.get(targetWsId);
      if (cached && cached.live) {
        cached.live = Object.assign({}, cached.live, {
          pending_approval: false,
          pending_approval_detail: null,
        });
        liveBadgeCache.set(targetWsId, cached);
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

  function _renderChildrenNow() {
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
    childrenCountEl.textContent = rows.length ? "(" + rows.length + ")" : "";
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
  // Urgent-flush coalesce flag — N urgent calls in the same JS tick
  // would otherwise issue N single-id bulk fetches (bulk endpoint
  // accepts up to LIVE_BADGE_BULK_CAP ids per request). queueMicrotask
  // batches them into one request that drains pendingLiveIds.
  let urgentFlushScheduled = false;

  function scheduleLiveFetch(childWsId, opts) {
    if (!childWsId) return;
    const urgent = !!(opts && opts.urgent);
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
      // Urgent fetches bypass the TTL — used when a child enters
      // approval state and the row needs the rich pending_approval_detail
      // payload (call_id, items, judge_verdict) to render inline buttons.
      // Waiting for the next 5s TTL window would leave the operator
      // staring at "⚑ approval" with no way to act.
      if (!urgent && Date.now() - cached.fetched < LIVE_BADGE_TTL_MS) return;
    }
    if (!WS_ID_RE.test(childWsId)) return;
    pendingLiveIds.add(childWsId);
    // Urgent: cancel the pending debounce and schedule a flush on
    // the next microtask so N urgent calls in the same tick coalesce
    // into one bulk request. Without the microtask hop, each urgent
    // caller would drain pendingLiveIds with a single id and fire a
    // separate fetch — defeating the bulk endpoint that accepts up
    // to LIVE_BADGE_BULK_CAP ids per request.
    if (urgent) {
      if (liveBadgeFlushTimer !== null) {
        clearTimeout(liveBadgeFlushTimer);
        liveBadgeFlushTimer = null;
      }
      if (!urgentFlushScheduled) {
        urgentFlushScheduled = true;
        const flush = () => {
          urgentFlushScheduled = false;
          flushLiveFetches();
        };
        if (typeof queueMicrotask === "function") {
          queueMicrotask(flush);
        } else {
          setTimeout(flush, 0);
        }
      }
      return;
    }
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
        liveBadgeCache.set(id, {
          live: live,
          fetched: now,
          // Denied ids are permission/identity misses — mark permanent
          // so SSE state ticks on those rows don't retry every window.
          permanent: wasDenied,
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
        liveBadgeCache.set(id, {
          live: null,
          fetched: now,
          permanent: isPermanent,
        });
      });
      if (!isPermanent) console.warn("flushLiveFetches failed", e);
    }
  }

  function invalidateLiveBadge(childWsId) {
    liveBadgeCache.delete(childWsId);
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
    const prevActivity = existing.activity_state || "";
    existing.state = ev.state || existing.state;
    existing.activity_state =
      typeof ev.activity_state === "string"
        ? ev.activity_state
        : existing.activity_state || "";
    if (ev.node_id) existing.node_id = ev.node_id;
    childrenState.set(childId, existing);
    _touchChild(childId);
    renderChildren();
    // Do NOT invalidateLiveBadge on routine state ticks — that defeats
    // the 5s TTL cache and devolves rate-limiting to the 250ms
    // debouncer, hitting cluster_ws_detail ~4 req/s per chatty child.
    // The TTL check in scheduleLiveFetch will refresh the badge on its
    // own schedule; identity-changing events (created/rename/closed)
    // still invalidate below.
    //
    // Two activity_state transitions warrant an *urgent* (TTL-bypassing)
    // fetch so the row carries pending_approval_detail in lockstep with
    // the child's true state:
    //   - "" / "tool" / "thinking" → "approval"  (need rich payload now
    //     so the inline approve/deny buttons can render)
    //   - "approval" → anything else            (need to drop the
    //     stale payload so the buttons disappear; without this the
    //     5s TTL leaves stale buttons on a row whose approval was
    //     resolved elsewhere — e.g. the child's own UI tab)
    const enteredApproval =
      existing.activity_state === "approval" && prevActivity !== "approval";
    const leftApproval =
      prevActivity === "approval" && existing.activity_state !== "approval";
    if (enteredApproval || leftApproval) {
      scheduleLiveFetch(childId, { urgent: true });
    } else {
      scheduleLiveFetch(childId);
    }
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
        liveBadgeCache.delete(id);
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
        liveBadgeCache.delete(id);
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
      liveBadgeCache.clear();
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
    try {
      const data = await getJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId),
      );
      nameEl.textContent = data.name || "";
      statusEl.textContent = data.state || "";
    } catch (e) {
      appendText("error", "Failed to load coordinator: " + e.message);
      return;
    }
    try {
      const hist = await getJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/history",
      );
      (hist.messages || []).forEach((m) => {
        const role = m.role || "tool";
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
        if (!content) return;
        if (role === "tool") {
          appendToolResult(
            m.tool_name || "tool",
            m.tool_call_id || "",
            content,
            false,
          );
        } else if (role === "assistant") {
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
          // user / reasoning / system / other roles render as plain
          // text on history replay — matches the live-streaming paths
          // (appendReasoningToken uses textContent; user/system are
          // typed verbatim and don't carry markdown structure).
          appendText(role, content, { label: role });
        }
      });
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
