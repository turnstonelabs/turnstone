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
  const composerMount = document.getElementById("coord-composer-mount");
  const composer = new Composer(composerMount, {
    placeholder: "Message the coordinator\u2026",
    ariaLabel: "Coordinator input",
    onSend: function (text) {
      coordSend(text);
    },
    // Preserve the coordinator's pre-refactor Enter-on-touch behaviour
    // — coordinator sessions are short and tap-to-send via the
    // on-screen Return key is a quicker workflow than tapping a Send
    // button.
    touchEnterSends: true,
    // No attachments yet — coordinator-side attach mid-conversation
    // requires a backend ingest path that doesn't exist; defer.
    // No stopBtn — coordinator already has a header-mounted cancel
    // button (#coord-cancel-btn) that fires coordCancel().
  });
  const statusEl = document.getElementById("coord-status");
  const sseEl = document.getElementById("coord-sse-status");
  const nameEl = document.getElementById("coord-name");
  const approvalBar = document.getElementById("coord-approval-bar");
  const approvalTools = document.getElementById("coord-approval-tools");
  const cancelBtn = document.getElementById("coord-cancel-btn");
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

  window.coordApprove = async function (approved, always) {
    if (!pendingApprovalCallId) return; // no-op if bar already resolved
    const body = {
      approved: !!approved,
      always: !!always,
      call_id: pendingApprovalCallId,
    };
    setApprovalButtonsDisabled(true);
    try {
      const resp = await postJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/approve",
        body,
      );
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

  window.coordSend = function (text) {
    const msg = (text || "").trim();
    if (!msg) return false;
    composer.clear();
    composer.setBusy(true);
    postJSON("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send", {
      message: msg,
    })
      .then((resp) => {
        if (!resp.ok) {
          return resp.text().then((txt) => {
            throw new Error("send failed: " + resp.status + " " + txt);
          });
        }
        appendText("user", msg, { label: "you" });
      })
      .catch((e) => {
        if (typeof toast !== "undefined" && toast.error) toast.error(String(e));
        else console.error(e);
      })
      .finally(() => {
        composer.setBusy(false);
      });
    return false;
  };

  window.coordCancel = function () {
    postJSON(
      "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/cancel",
      {},
    ).catch((e) => console.error(e));
  };

  window.coordCloseSession = async function () {
    if (
      !window.confirm(
        "End this coordinator session? The server will terminate it.",
      )
    )
      return;
    try {
      const resp = await postJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/close",
        {},
      );
      if (!resp.ok) throw new Error("close failed: HTTP " + resp.status);
      window.location.href = "/";
    } catch (e) {
      if (typeof toast !== "undefined" && toast.error) toast.error(String(e));
      else console.error(e);
    }
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
    // we were disconnected aren't replayed by coordinator_events, so
    // the client has to pull authoritative state after any gap.
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
        // task_list mutations change persisted state the sidebar reads
        // from GET /tasks — re-fetch so the operator sees
        // add/update/remove/reorder without clicking the refresh icon.
        // list is a read-only action; skip to avoid redundant fetches.
        // Debounced so a burst of mutations coalesces into one fetch.
        if (ev.name === "task_list" && !ev.is_error) {
          loadTasksDebounced();
        }
        break;
      case "approve_request":
        showApproval(ev.items);
        // Surface each tool for context.  Dedupe by call_id: the console
        // replays _pending_approval into every new SSE subscriber (see
        // coordinator_events handler), so without this check an SSE
        // reconnect would render each tool row a second time.
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
        cancelBtn.style.display =
          ev.state === "running" || ev.state === "thinking" ? "" : "none";
        break;
      case "rename":
        nameEl.textContent = ev.name || "";
        break;
      case "tools_auto_approved":
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
  // Debounce window for /tasks refreshes triggered by ``task_list``
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
    return row;
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
  // task_list mutations (the model's typical add → update → list
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
    existing.state = ev.state || existing.state;
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
    scheduleLiveFetch(childId);
  }

  function handleChildClosed(ev) {
    const childId = ev.child_ws_id || ev.ws_id;
    if (!childId) return;
    const existing = childrenState.get(childId);
    if (!existing) return;
    existing.state = ev.reason === "deleted" ? "deleted" : "closed";
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
        const content =
          typeof m.content === "string"
            ? m.content
            : JSON.stringify(m.content || "");
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
