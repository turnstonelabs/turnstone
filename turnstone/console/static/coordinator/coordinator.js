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

  // Map raw role → ts-msg modifier.  "error" overloads the role slot
  // for styling; opts.label still carries the tool name so SR text
  // like "error · bash" stays meaningful on the data-ts-role /
  // aria-label attributes when labels stop rendering as DOM text.
  const _TS_ROLE_VARIANTS = {
    user: "ts-msg--user",
    assistant: "ts-msg--assistant",
    reasoning: "ts-msg--reasoning",
    tool: "ts-msg--tool",
    error: "ts-msg--error",
  };

  function appendMsg(role, html, opts) {
    opts = opts || {};
    const el = document.createElement("div");
    const variant = _TS_ROLE_VARIANTS[role] || "ts-msg--assistant";
    el.className = "coord-msg role-" + role + " ts-msg " + variant;
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
    body.className = "coord-body ts-msg-body";
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
    const body = currentAssistantEl.querySelector(".coord-body");
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
    const body = currentReasoningEl.querySelector(".coord-body");
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
      const body = currentAssistantEl.querySelector(".coord-body");
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
    // Header row — "Approve N tool calls" clarifies that the batch
    // approval applies to every row, not just the focused one.
    if (pending.length > 0) {
      const header = document.createElement("div");
      header.className = "tool-row approval-header ts-approval-header";
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
      row.className = "tool-row ts-approval-tool";
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
    if (
      !window.confirm(
        "End this coordinator session? The server will terminate it.",
      )
    )
      return;
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
    // Preserve the base .ts-header-status class + add the BEM modifier
    // variant so chat.css's .ts-header-status--ok / --err colour rules
    // actually win; setting className to just "ok"/"err" drops the
    // base class and the green / red colour never applies.
    var base = "ts-header-status";
    sseEl.className = cls ? base + " " + base + "--" + cls : base;
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
    const url = "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/events";
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
      probe("/v1/api/coordinator/" + encodeURIComponent(wsId))
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
              '.coord-msg[data-call-id="' + cssEscape(it.call_id) + '"]',
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
      default:
        // Unknown event type — ignore silently.
        break;
    }
  }

  // ------------------------------------------------------------------
  // Children tree + task list — right sidebar
  // ------------------------------------------------------------------

  // ws_id -> child row snapshot.  Updated on initial /children load +
  // SSE child_ws_* events so the tree can be re-rendered cheaply.
  const childrenState = new Map();
  // ws_id -> {live: <dict>, fetched: <ms>} for the 5s TTL live-badge cache.
  const liveBadgeCache = new Map();
  // ws_id -> timeout id, for 250ms debounce on live-inspect fetches.
  const liveBadgeDebounce = new Map();
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
        "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/children",
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
        "/v1/api/coordinator/" + encodeURIComponent(wsId) + "/tasks",
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
    if (liveBadgeDebounce.has(childWsId)) return;
    const tid = setTimeout(() => {
      liveBadgeDebounce.delete(childWsId);
      fetchLiveBadge(childWsId);
    }, LIVE_BADGE_DEBOUNCE_MS);
    liveBadgeDebounce.set(childWsId, tid);
  }

  async function fetchLiveBadge(childWsId) {
    if (!WS_ID_RE.test(childWsId)) return;
    try {
      const body = await getJSON(
        "/v1/api/cluster/ws/" + encodeURIComponent(childWsId) + "/detail",
      );
      liveBadgeCache.set(childWsId, {
        live: body && body.live ? body.live : null,
        fetched: Date.now(),
      });
      const row = childrenTreeEl.querySelector(
        '.ch-row[data-ws-id="' + cssEscape(childWsId) + '"]',
      );
      if (row) {
        const entry = childrenState.get(childWsId);
        if (entry) {
          const replacement = renderChildRow(entry);
          row.replaceWith(replacement);
        }
      }
    } catch (e) {
      // Cache failures too — otherwise 403 / 404 paths burn one HTTP
      // round-trip per SSE child_ws_state event.  403/404 are marked
      // permanent (permission/identity won't change mid-session); 5xx
      // + network errors take the normal 5s TTL so transient blips
      // recover on the next schedule.
      const isTerminal = e && /HTTP 40[34]/.test(e.message || "");
      liveBadgeCache.set(childWsId, {
        live: null,
        fetched: Date.now(),
        permanent: isTerminal,
      });
      if (!isTerminal) {
        console.warn("fetchLiveBadge failed", e);
      }
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
