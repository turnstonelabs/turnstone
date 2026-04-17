/* coordinator.js — one-pane UI for console-hosted coordinator sessions.
 *
 * Connects to:
 *   GET  /v1/api/coordinator/{ws_id}/events  (SSE)
 *   GET  /v1/api/coordinator/{ws_id}/history (initial history)
 *   POST /v1/api/coordinator/{ws_id}/send
 *   POST /v1/api/coordinator/{ws_id}/approve
 *   POST /v1/api/coordinator/{ws_id}/cancel
 *   POST /v1/api/coordinator/{ws_id}/close
 *
 * Depends on shared/auth.js (authFetch, fetchWithCreds), shared/theme.js
 * (toggleTheme), shared/toast.js (toast.info / toast.error),
 * shared/utils.js (escapeHtml, linkify helpers).
 *
 * Assistant content follows the renderer.js pipeline from the server UI
 * (ui/static/app.js): buffer the raw markdown as tokens stream in, keep
 * the visible text plain during streaming, and swap to the rendered
 * innerHTML + postRenderMarkdown() once the stream ends.  Reasoning
 * bubbles stay text-only because they're transient and styled dim/italic.
 * See turnstone/ui/static/app.js (search for renderMarkdown) for the
 * canonical pattern we mirror.
 */
(function () {
  "use strict";

  const wsId = document.documentElement.dataset.wsId || "";
  if (!wsId) {
    document.getElementById("coord-messages").innerHTML =
      '<div class="coord-msg role-error">Missing ws_id on &lt;html&gt; tag.</div>';
    return;
  }

  const messagesEl = document.getElementById("coord-messages");
  const inputEl = document.getElementById("coord-input");
  const sendBtn = document.getElementById("coord-send-btn");
  const statusEl = document.getElementById("coord-status");
  const sseEl = document.getElementById("coord-sse-status");
  const nameEl = document.getElementById("coord-name");
  const approvalBar = document.getElementById("coord-approval-bar");
  const approvalTools = document.getElementById("coord-approval-tools");
  const cancelBtn = document.getElementById("coord-cancel-btn");

  let pendingApprovalCallId = null;
  let evtSource = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;

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

  function appendMsg(role, html, opts) {
    opts = opts || {};
    const el = document.createElement("div");
    el.className = "coord-msg role-" + role;
    if (opts.callId) el.dataset.callId = opts.callId;
    if (opts.label) {
      const labelEl = document.createElement("div");
      labelEl.className = "role-label";
      labelEl.textContent = opts.label;
      el.appendChild(labelEl);
    }
    const body = document.createElement("div");
    body.className = "coord-body";
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
    // During streaming we keep the visible text as textContent (plain
    // string, not rendered markdown).  Re-running renderMarkdown on every
    // token would be expensive and also produces half-formed output for
    // partial code fences / lists.  The raw buffer is stashed on the
    // element so finishAssistantStream() can do one final render pass.
    // Mirrors ui/static/app.js's `this.contentBuffer` approach — we just
    // keep the buffer in a closure variable instead of on `this`.
    const body = currentAssistantEl.querySelector(".coord-body");
    if (body) {
      body.textContent = currentAssistantBuf;
      body._rawMarkdown = currentAssistantBuf;
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
    const body = currentReasoningEl.querySelector(".coord-body");
    if (body) body.textContent = currentReasoningBuf;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function finishAssistantStream() {
    // Promote the streamed plaintext buffer to rendered markdown (code
    // fences, lists, KaTeX, mermaid, syntax highlighting).  renderMarkdown
    // escapes HTML internally so innerHTML here is XSS-safe as long as
    // renderer.js is trusted — same contract as ui/static/app.js.  Guard
    // the call in a try/catch so a renderer bug can never brick the
    // coordinator UI; on failure the visible textContent stays put.
    if (currentAssistantEl && currentAssistantBuf) {
      const body = currentAssistantEl.querySelector(".coord-body");
      if (body && typeof renderMarkdown === "function") {
        try {
          body.innerHTML = renderMarkdown(currentAssistantBuf);
          if (typeof postRenderMarkdown === "function") {
            postRenderMarkdown(body);
          }
        } catch (e) {
          console.warn("coordinator renderMarkdown failed", e);
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
    // Header row — "Approve N tool calls" clarifies that the batch
    // approval applies to every row, not just the focused one.
    if (pending.length > 0) {
      const header = document.createElement("div");
      header.className = "tool-row approval-header";
      header.textContent =
        pending.length === 1
          ? "Approve 1 tool call:"
          : "Approve " + pending.length + " tool calls (batch):";
      approvalTools.appendChild(header);
    }
    let firstCallId = null;
    pending.forEach((it, idx) => {
      if (!firstCallId) firstCallId = it.call_id;
      const row = document.createElement("div");
      row.className = "tool-row";
      const label =
        it.header || it.approval_label || it.func_name || "(unknown tool)";
      row.textContent = pending.length > 1 ? idx + 1 + ". " + label : label;
      approvalTools.appendChild(row);
    });
    pendingApprovalCallId = firstCallId;
    approvalBar.classList.add("visible");
    // Move focus to the approve button — non-modal region, so keyboard
    // users don't have to Shift+Tab back from the composer.  One-time
    // focus shift; subsequent Tab/Shift+Tab navigates normally.
    const approveBtn = document.getElementById("coord-approve-btn");
    if (approveBtn) {
      try {
        approveBtn.focus({ preventScroll: false });
      } catch (_) {
        /* noop */
      }
    }
  }

  function hideApproval() {
    approvalBar.classList.remove("visible");
    approvalTools.replaceChildren();
    pendingApprovalCallId = null;
    setApprovalButtonsDisabled(false);
    // Return focus to the composer for keyboard users.  Only if the
    // approval bar itself was the focus holder — don't steal focus from
    // e.g. a user who clicked into the history log.
    if (
      document.activeElement &&
      approvalBar.contains(document.activeElement)
    ) {
      try {
        inputEl.focus();
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
        "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/approve",
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

  window.coordSend = function (evt) {
    if (evt && evt.preventDefault) evt.preventDefault();
    const msg = (inputEl.value || "").trim();
    if (!msg) return false;
    inputEl.value = "";
    sendBtn.disabled = true;
    postJSON("/v1/api/coordinator/" + encodeURIComponent(wsId) + "/send", {
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
        sendBtn.disabled = false;
      });
    return false;
  };

  window.coordCancel = function () {
    postJSON(
      "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/cancel",
      {},
    ).catch((e) => console.error(e));
  };

  window.coordCloseSession = async function () {
    if (!window.confirm("Close this coordinator session?")) return;
    try {
      const resp = await postJSON(
        "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/close",
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
    sseEl.className = cls || "";
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
    const url = "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/events";
    evtSource = new EventSource(url, { withCredentials: true });
    evtSource.onopen = function () {
      reconnectAttempts = 0;
      setSseStatus("live", "ok");
    };
    evtSource.onerror = function () {
      setSseStatus("disconnected", "err");
      try {
        evtSource.close();
      } catch (_) {
        /* noop */
      }
      scheduleReconnect();
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
              '.coord-msg[data-call-id="' + CSS.escape(it.call_id) + '"]',
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
        // Minimal surfacing — risk_level + recommendation.
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
        appendText("tool", ev.message || "", { label: "info" });
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
      default:
        // Unknown event type — ignore silently.
        break;
    }
  }

  // ------------------------------------------------------------------
  // Initial load — history then SSE
  // ------------------------------------------------------------------

  async function init() {
    try {
      const data = await getJSON(
        "/v1/api/coordinator/" + encodeURIComponent(wsId),
      );
      nameEl.textContent = data.name || "";
      statusEl.textContent = data.state || "";
    } catch (e) {
      appendText("error", "Failed to load coordinator: " + e.message);
      return;
    }
    try {
      const hist = await getJSON(
        "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/history",
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
        } else {
          appendText(role, content, { label: role });
        }
      });
    } catch (e) {
      console.warn("history load failed", e);
    }
    connectSSE();
  }

  init();

  // Plain Enter submits; Shift+Enter inserts a newline.  IME composition
  // (isComposing) is respected so users typing in CJK/other IMEs aren't
  // cut off mid-word.
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      coordSend(e);
    }
  });
})();
