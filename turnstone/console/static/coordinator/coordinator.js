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
// ---------------------------------------------------------------------------
// Coordinator chrome — built programmatically (createElement, no innerHTML) so
// the SAME markup serves the standalone page (root = document.body) and a
// console pane (root = the pane body).  `opts.standalone` adds the page-level
// bits a pane doesn't want: the "Console" back-link, the theme toggle, and the
// shared #toast (the console shell already provides theme + toast).
// ---------------------------------------------------------------------------
import {
  buildWatchResultCard,
  buildCompactionCard,
  applyCompactionEvent,
  resetCompactionHolder,
  buildSystemNudgeMarker,
  maxSeverityItem,
  buildConvBatchShell,
  buildConvRow,
  buildConvVerdict,
  buildConvWarning,
  buildConvActions,
  buildConvStatus,
  buildConvResult,
  batchKicker,
  indexLabel,
} from "/shared/conversation.js";
import { redactCredentials } from "/shared/redact_credentials.js";
import { tryParseMcpError, buildMcpErrorEmbed } from "/shared/mcp_error.js";
import {
  createQueueController,
  parsePriority,
  settleSendResponse,
} from "/shared/composer_queue.js";
import {
  OVERFLOW_TRIP_COUNT,
  OVERFLOW_TRIP_WINDOW_MS,
  DEGRADED_COOLDOWN_BASE_MS,
  DEGRADED_COOLDOWN_MAX_MS,
  DEGRADED_COOLDOWN_RESET_MS,
  TRUNCATED_RESYNC_JITTER_MS,
  overflowWindowTripped,
  degradedCooldownStep,
} from "/shared/sse_overflow.js";

// Standalone-page pending-consent chip (#874): the rail-less coordinator
// page's counterpart of the L-shell rail badge.  The pending set is
// USER-scoped server truth (the Phase 9 mcp_oauth_pending table), not
// per-workstream state — the wording says "awaiting consent", never "this
// workstream".  Inert until mount() (the L-shell pane never mounts it; the
// rail badge carries the signal there).
const _consentChip = (function () {
  const pending = new Set();
  let chipEl = null;
  function paint() {
    if (!chipEl) return;
    const n = pending.size;
    chipEl.hidden = n === 0;
    if (n > 0) {
      // Glyph is aria-hidden so screen readers announce only the plain
      // sentence (an aria-label on a role-less span is ignored by
      // several of them, which then voice the raw glyph).
      chipEl.textContent = "";
      const glyph = document.createElement("span");
      glyph.setAttribute("aria-hidden", "true");
      glyph.textContent = "⚠ ";
      chipEl.appendChild(glyph);
      chipEl.appendChild(
        document.createTextNode(
          n + " MCP server" + (n === 1 ? "" : "s") + " awaiting consent",
        ),
      );
    }
  }
  let hydrateInFlight = false;
  let hydrateQueued = false;
  function hydrate() {
    // Merge-on-success: entries the server no longer lists are dropped
    // ONLY if they predate this fetch (the preFetch snapshot) — a
    // detection add()ed while the fetch was in flight survives, since
    // its DB row may postdate the server's read.  A failed fetch
    // changes nothing, so a possibly-valid warning is never blanked.
    // Single-flight: overlapping hydrates (mount + rapid visibility
    // edges) can resolve out of order, and every guard short of
    // exclusion re-admits some interleaving; one flight at a time makes
    // the class structurally impossible, and an edge firing mid-flight
    // queues exactly one rerun so the freshest truth still lands.
    if (hydrateInFlight) {
      hydrateQueued = true;
      return;
    }
    // Bounded flight: a stalled fetch would otherwise hold the gate
    // shut forever (the .finally below never runs, freezing self-heal);
    // on timeout the chain rejects → .catch → .finally clears the gate
    // and drains any queued rerun.  Feature-detected like the
    // codebase's AbortController guards — old runtimes just run
    // unbounded, as before — and computed BEFORE the gate is set so no
    // throw can wedge it (or escape mount()).
    const signal =
      typeof AbortSignal !== "undefined" && AbortSignal.timeout
        ? AbortSignal.timeout(10000)
        : undefined;
    hydrateInFlight = true;
    const preFetch = new Set(pending);
    authFetch("/v1/api/mcp/oauth/pending", { signal: signal })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || !Array.isArray(data.servers)) return;
        const fetched = new Set();
        for (let i = 0; i < data.servers.length; i++) {
          const row = data.servers[i];
          if (row && typeof row.server_name === "string") {
            fetched.add(row.server_name);
          }
        }
        preFetch.forEach(function (s) {
          if (!fetched.has(s)) pending.delete(s);
        });
        fetched.forEach(function (s) {
          pending.add(s);
        });
        paint();
      })
      .catch(function () {})
      .finally(function () {
        hydrateInFlight = false;
        if (hydrateQueued) {
          hydrateQueued = false;
          hydrate();
        }
      });
  }
  return {
    mount: function (elRef) {
      chipEl = elRef;
      paint();
      // Hydrate so a consent wall hit while nobody watched still
      // surfaces here...
      hydrate();
      // ...and self-heal after the popup: the consent flow opens
      // noopener (no cross-window channel exists), and the user lands
      // back on this tab right after finishing there — so re-pull server
      // truth on every visibility-show edge.  mount() runs once per
      // page, so this registers exactly one listener.
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") hydrate();
      });
    },
    add: function (server) {
      if (typeof server === "string" && server) {
        pending.add(server);
        paint();
      }
    },
  };
})();

// Consent-detection fan-out for the card's onConsent hook (#874): in the
// L-shell the console app exposes the same window.TS_APP.onConsentDetected
// seam the node dashboard does (driving the rail's MCP-row badge); the
// standalone page mounts the status-bar chip instead.  Both are no-ops
// when their surface is absent.
function _notifyConsentDetected(server) {
  const app = window.TS_APP;
  if (app && typeof app.onConsentDetected === "function") {
    app.onConsentDetected(server);
  }
  _consentChip.add(server);
}

// Shared MCP-error detection for both result paths (#725): parse + build
// ONLY — wrapper concerns stay with each call site (the live-row path
// adds the conv-row-result marker classes; the orphan path appends into
// .msg-body).  Consent threading lands here once, for both (#874).
function _tryMcpErrorBlock(isError, output) {
  if (!isError) return null;
  const mcpErr = tryParseMcpError(output);
  return mcpErr
    ? buildMcpErrorEmbed(mcpErr, output, _notifyConsentDetected)
    : null;
}

function buildCoordChrome(root, opts) {
  opts = opts || {};
  root.classList.add("coord-chrome-root");
  function el(tag, props, kids) {
    const n = document.createElement(tag);
    if (props) {
      for (const k in props) {
        if (k === "class") n.className = props[k];
        else if (k === "text") n.textContent = props[k];
        else n.setAttribute(k, props[k]);
      }
    }
    if (kids)
      for (let i = 0; i < kids.length; i++) if (kids[i]) n.append(kids[i]);
    return n;
  }
  const SR =
    "position:absolute;left:-10000px;width:1px;height:1px;overflow:hidden";

  // No pane header — name + state are shown by the tab + the rail (Workspaces);
  // a coordinator is always a pane in the L-shell.  The conversation reclaims
  // the full pane height.  (The busy/wait indicator self-disables without
  // #coord-header; the SSE-connection indicator is dropped — reconnect handles
  // transient drops, matching the interactive pane.)

  // ----- main column (messages + off-screen announcers + status bar + composer) -----
  const statusBar = el(
    "div",
    {
      id: "coord-status-bar",
      class: "ws-status-bar",
      role: "status",
      "aria-live": "polite",
      "aria-atomic": "true",
      "aria-label": "Coordinator status",
    },
    [
      el("span", {
        id: "coord-sb-tokens",
        class: "ws-sb-tokens",
        "aria-label": "Token usage",
        text: "0 / —",
      }),
      el("span", {
        id: "coord-sb-tools",
        class: "ws-sb-tools",
        "aria-label": "Tool calls this turn",
        text: "0 tools",
      }),
      el("span", {
        id: "coord-sb-turns",
        class: "ws-sb-turns",
        "aria-label": "Conversation turn",
        text: "turn 0",
      }),
    ],
  );
  const main = el("div", { id: "coord-main" }, [
    el("div", { id: "coord-messages", role: "log", "aria-live": "polite" }),
    el("div", {
      id: "coord-sr-announcer",
      role: "status",
      "aria-live": "assertive",
      "aria-atomic": "true",
      style: SR,
    }),
    el("div", {
      id: "coord-sr-announcer-polite",
      role: "status",
      "aria-live": "polite",
      "aria-atomic": "true",
      style: SR,
    }),
    statusBar,
    el("div", { id: "coord-composer-mount" }),
  ]);

  // ----- sidebar (children + tasks) -----
  function sideSection(
    wrapId,
    headingId,
    heading,
    countId,
    refreshId,
    refreshLabel,
    bodyId,
  ) {
    return el("div", { id: wrapId, class: "side-section" }, [
      el("div", { class: "coord-sidebar-head" }, [
        el("h2", { id: headingId, class: "side-label", text: heading }),
        el("span", { id: countId, class: "side-count" }),
        el("button", {
          type: "button",
          class: "ghost",
          id: refreshId,
          "aria-label": refreshLabel,
          title: refreshLabel,
          text: "↻",
        }),
      ]),
      el("div", {
        id: bodyId,
        class: "coord-sidebar-body",
        role: "list",
        "aria-labelledby": headingId,
      }),
    ]);
  }
  const sidebar = el(
    "aside",
    {
      id: "coord-sidebar",
      class: "sidebar",
      "aria-expanded": "true",
      "aria-label": "Coordinator children and tasks",
    },
    [
      el(
        "button",
        {
          id: "coord-sidebar-toggle",
          type: "button",
          "aria-controls": "coord-children-wrap coord-tasks-wrap",
          "aria-expanded": "true",
        },
        [
          el("span", {
            id: "coord-sidebar-toggle-glyph",
            "aria-hidden": "true",
            text: "▾",
          }),
          el("span", { text: "Children & tasks" }),
        ],
      ),
      sideSection(
        "coord-children-wrap",
        "coord-children-heading",
        "Children",
        "coord-children-count",
        "coord-children-refresh",
        "Refresh children",
        "coord-children-tree",
      ),
      sideSection(
        "coord-tasks-wrap",
        "coord-tasks-heading",
        "Tasks",
        "coord-tasks-count",
        "coord-tasks-refresh",
        "Refresh tasks",
        "coord-tasks",
      ),
    ],
  );

  root.append(el("div", { id: "coord-body" }, [main, sidebar]));
  if (opts.standalone) {
    root.append(
      el("div", { id: "toast", role: "status", "aria-live": "polite" }),
    );
  }
}

function createCoordinatorPane(root, wsId, opts) {
  "use strict";
  if (!wsId) {
    const missing = document.createElement("div");
    missing.className = "msg error";
    missing.textContent = "Missing ws_id.";
    root.replaceChildren(missing);
    return null;
  }
  buildCoordChrome(root, opts);

  if (opts && opts.standalone) {
    // Rail-less page: mount the pending-consent chip in the status bar —
    // the persistent signal the L-shell gets from the rail badge (#874).
    const sb = root.querySelector("#coord-status-bar");
    if (sb) {
      // Marker class scopes the narrow-width wrap rules to bars that
      // actually carry the chip (the L-shell pane's bar never does).
      sb.classList.add("ws-sb-has-consent");
      const chip = document.createElement("span");
      chip.id = "coord-sb-consent";
      chip.className = "ws-sb-consent";
      chip.hidden = true;
      sb.appendChild(chip);
      _consentChip.mount(chip);
    }
  }

  const messagesEl = root.querySelector("#coord-messages");
  const coordMain = root.querySelector("#coord-main");
  const composerMount = root.querySelector("#coord-composer-mount");
  const composer = new Composer(composerMount, {
    sendGlyph: "\u2191",
    layout: "stacked",
    modelChip: true,
    projectChip: true,
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
    // Re-sync the staged-attachment chips after a confirmed dequeue so the
    // composer view matches server truth. Queued messages are text-only, so
    // this isn't reclaiming a reservation (there is none) — it's a cheap
    // correctness refresh, fired by the controller only on the `removed`
    // verdict. Trades a small in-flight-placeholder clobbering window for the
    // strictly worse alternative of attachments lingering invisibly until the
    // next page load.
    onAfterDequeue: function () {
      attachments.rehydrate();
    },
    // Surface dequeue feedback in the chat log (coord has no toast): the
    // "already sent" / "couldn't remove" / "no longer available" notices the
    // controller raises. Reuses the transient "info" row (cf. force-stop).
    onNotice: function (msg) {
      appendText("info", msg, { label: "info" });
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
  // Provenance of the current busy=true (see setBusy): "server" |
  // "optimistic" | null when idle.
  let busySource = null;
  // Acting user (turn initiator) of the in-flight turn, from state_change
  // events; drives the shared-workstream cross-user send gate. Carries the
  // owner id even single-user (the gate just no-ops — it equals this viewer);
  // null when idle/error or when the backend sends no acting id
  // (unauthenticated / older backend). Mirrors the interactive pane.
  let actingUserId = null;
  // Edit-and-resend latch (#549): set by _editAndResend, consumed by the
  // clear_ui SSE handler once the rewind's truncated history is re-fetched.
  let _pendingEditSend = null;
  let cancelTimeoutId = null;
  let forceTimeoutId = null;
  const statusEl = root.querySelector("#coord-status");
  const sseEl = root.querySelector("#coord-sse-status");
  const nameEl = root.querySelector("#coord-name");
  const childrenTreeEl = root.querySelector("#coord-children-tree");
  const childrenCountEl = root.querySelector("#coord-children-count");
  const childrenRefreshBtn = root.querySelector("#coord-children-refresh");
  const tasksEl = root.querySelector("#coord-tasks");
  const tasksCountEl = root.querySelector("#coord-tasks-count");
  const tasksRefreshBtn = root.querySelector("#coord-tasks-refresh");

  // Child ws links — rendered into the children tree (renderChildRow) and into
  // linkified tool output (renderToolOutput) — open the child as a node-proxied
  // interactive pane in the console L-shell.  Delegated on the pane root so it
  // survives every re-render; the link's href is the standalone fallback (the
  // standalone coordinator page has no PaneManager, so the new-tab nav stands).
  root.addEventListener("click", function (e) {
    const link =
      e.target.closest && e.target.closest(".ws-link, .coord-ws-link");
    if (!link) return;
    const childWs = link.dataset.wsId;
    const childNode = link.dataset.nodeId;
    if (!childWs || !childNode) return;
    const pm = window.TS_SHELL && window.TS_SHELL.panes;
    if (pm && pm.openPaneBeside) {
      e.preventDefault();
      // Beside, not instead: the child lands in a cell to the RIGHT of the
      // coordinator (you are usually cross-checking the child against the
      // tree that spawned it, so the parent stays on screen).  The click's
      // pointerdown already focused this coordinator's cell, so "beside the
      // focused cell" is beside THIS pane; a denied split (cell cap / narrow
      // viewport) degrades to the old focused-cell swap.
      pm.openPaneBeside("interactive", childWs, { nodeId: childNode });
    }
  });
  // Approval keyboard shortcuts (designer P2 — the console twin of the
  // interactive.js fix): when a tool-batch is awaiting approval, route the
  // card's kbd hints (Enter approve / D deny / Shift+A approve-all) to the
  // resolve path.  Pane-owned on `root`, no global handler.  A focus guard lets
  // the composer / any input keep its own keys; _currentPendingBatch skips a
  // batch whose actions are already disabled (the in-flight double-fire guard).
  // No feedback field here (unlike interactive), so no feedback special-case.
  root.addEventListener("keydown", function (e) {
    const ae = document.activeElement;
    if (
      ae &&
      (ae.tagName === "TEXTAREA" ||
        ae.tagName === "INPUT" ||
        ae.isContentEditable)
    )
      return;
    // Ignore browser/OS accelerators (Cmd+D bookmark, Ctrl+D, Alt+D) — only bare
    // keys + Shift+A resolve, else a stray accelerator silently denies the batch.
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const batch = _currentPendingBatch();
    if (!batch) return;
    if (e.key === "Enter") {
      e.preventDefault();
      _resolveBatchAction(batch, true, false);
    } else if (e.key === "Escape" || e.key.toLowerCase() === "d") {
      e.preventDefault();
      _resolveBatchAction(batch, false, false);
    } else if (e.shiftKey && e.key.toLowerCase() === "a") {
      e.preventDefault();
      _resolveBatchAction(batch, true, true);
    }
  });
  // Off-screen aria-live="assertive" region — pending tool-batches
  // append into the polite messages log, which gets flipped to
  // aria-live="off" during streaming.  Routing the action-required
  // announcement through this dedicated region ensures SR users hear
  // the gate land regardless of streaming state.
  const srAnnouncerEl = root.querySelector("#coord-sr-announcer");
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

  // Off-screen aria-live="polite" sibling — the tool-call early paint
  // (tool_pending) routes here so SR users hear a committed call land (and
  // that they can Stop it) without the interrupt the assertive gate uses.
  const srPoliteEl = root.querySelector("#coord-sr-announcer-polite");
  function _announcePolite(text) {
    if (!srPoliteEl || !text) return;
    srPoliteEl.textContent = "";
    requestAnimationFrame(() => {
      srPoliteEl.textContent = text;
    });
  }

  // Terse SR summary for a committed tool batch: tool name(s) (capped at 3)
  // + that it's being judged and can be stopped.  "" when nothing is named.
  function _toolAnnounceText(items) {
    const names = (items || []).map((it) => it && it.func_name).filter(Boolean);
    if (!names.length) return "";
    const n = names.length;
    const shown = names.slice(0, 3).join(", ");
    const list = n > 3 ? shown + ", and " + (n - 3) + " more" : shown;
    const head =
      n === 1
        ? "Tool call pending: " + list
        : n + " tool calls pending: " + list;
    // No "judge evaluating" claim — tool_pending fires unconditionally, so the
    // intent judge may be disabled.  "Pending + you can stop it" is always true.
    return head + ". You can stop " + (n === 1 ? "it" : "them") + ".";
  }

  // Status bar — model alias, token / context-window usage, tool calls
  // this turn, conversation turn.  Driven by the connected + status
  // SSE events; mirrors the interactive pane (ui/static/app.js).
  const statusBarEl = root.querySelector("#coord-status-bar");
  const sbTokensEl = root.querySelector("#coord-sb-tokens");
  const sbToolsEl = root.querySelector("#coord-sb-tools");
  const sbTurnsEl = root.querySelector("#coord-sb-turns");
  let coordModel = "";
  let coordModelAlias = "";
  let coordEffort = "";
  let coordProjectName = "";
  let lastStatusEvt = null;

  let evtSource = null;
  let reconnectAttempts = 0;
  // Wall-clock start of the CURRENT gap (0 = no gap in progress).  Stamped by
  // markStreamGap (from onerror and every deliberate suspend), cleared by
  // onopen.  A non-zero value IS the "did we just recover from a gap?" flag
  // that drives onopen's replace-mode children/tasks/badge refresh — no
  // separate boolean is kept in lockstep with it.  Kept at the EARLIEST mark
  // so repeated onerror fires during one outage don't shrink the measured gap.
  // (The scheduleReconnect-after-CLOSED path bumps reconnectAttempts instead;
  // wasReconnecting ORs the two — that path is rarely hit now that native
  // reconnect handles transient errors.)
  let disconnectedAt = 0;
  // Reconnect-replay trust window for the sidebar.  child_ws_* / task events
  // are ordinary ring-buffer entries, so a cursor reconnect (replay_ok)
  // redelivers them and the sidebar heals without any REST refetch; gaps the
  // ring could NOT cover announce themselves via replay_truncated.  The one
  // blind spot is a stale cursor the server no longer recognises (process
  // restart resets event ids; the empty/reset ring reports replay_ok and
  // silently skips the gap).  Past this gap length we stop trusting the cursor
  // and pull authoritative /children + /tasks state; a FASTER restart (under
  // the threshold) is caught instead by the backwards-event-id check in
  // onmessage (a live id below our saved cursor == the counter reset).
  // Momentary blur/focus cycles stay well below the threshold — no rebuild
  // flicker on an alt-tab.
  const GAP_REFRESH_THRESHOLD_MS = 60000;
  // True when THIS connection's onopen already ran refreshSidebarAfterGap —
  // lets the replay_truncated handler (first frame after open) skip a
  // back-to-back duplicate of the refresh it would otherwise trigger.
  let gapRefreshedAtOpen = false;
  // Set when replay_truncated arrives while a turn is mid-stream (refetching
  // then would detach the live bubble); consumed on the next state_change=idle.
  // Mirrors interactive.js's _pendingTruncatedResync — the deferral keeps a
  // ring-evicted gap from going unrepaired for the rest of the session, and
  // also catches a turn stranded by close-on-hide (finished while hidden, its
  // stream_end evicted before the show-edge reconnect).
  let pendingTruncatedResync = false;
  // The cursor position a replay_truncated envelope was received AT — i.e.
  // "a gap of lost events exists BELOW this cursor".  Keep-oldest: set only
  // when null (repeated envelopes for the same unrepaired gap must not
  // advance it), cleared by any successful full-history render
  // (refetchHistory — the truncated resync and a clear_ui rebuild both
  // repair the gap).  NOT cleared by teardown or turn boundaries, and —
  // unlike interactive.js — never dropped for a ws switch: this pane is
  // single-wsId for life (a ws-switch feature would have to reset it
  // alongside lastEventId).  The connectSSE chokepoint is its
  // cursor-presenting consumer: while set, every manual (re)connect
  // presents THIS cursor (not the since-advanced live lastEventId), so the
  // server re-answers replay_truncated and the resync re-arms no matter
  // which teardown cancelled the pending jittered timer — the gap-repair
  // guarantee survives any interleaving of hide/show, degraded cooldowns,
  // CLOSED-state retries, and failed /history fetches.  Three more sites
  // read it purely as a null-sentinel for "is an unrepaired gap on
  // record": the replay_truncated case (dedup the sidebar refresh per
  // gap), loadHistoryThenReconnect's heal-time sidebar refresh, and
  // onopen's post-gap sidebar-refresh gate (stands down while a gap is on
  // record — the gap machinery owns recovery).  Cleared — together with
  // the deferred latch and any pending resync timer — only by
  // refetchHistory's success-path supersession.  Mirrors interactive.js's
  // _truncatedFromCursor.
  let truncatedFromCursor = null;
  // Saved high-water mark for the manual-reconnect path.  The
  // EventSource constructor can't set custom headers, so when we
  // construct a fresh source we thread ``?last_event_id=N`` instead
  // of the browser-native ``Last-Event-ID`` header.  Native
  // auto-reconnect on the SAME source object uses the header
  // automatically; this fallback covers the cases where we open a
  // brand-new EventSource (initial connect, scheduleReconnect after
  // close).
  let lastEventId = null;
  let reconnectTimer = null;
  // --- SSE overflow-recovery state (client half — mirrors interactive.js) ---
  // Field instrumentation for the two distinct "output stops while the backend
  // is healthy" causes: server-signalled overflow closes (dropped-events
  // class) vs client dispatch/render throws (wedge class).  The console line
  // at each increment carries the running count, so a field report shows which
  // class fired without a debugger attached.
  // ``truncatedGaps`` counts replay-window misses (truncated envelopes acted
  // on), NOT resyncs performed — a degraded-catchup trip records the gap but
  // skips its resync.  Pairs with the node-side ws.events.replay_truncated
  // log line for field forensics.
  const streamHealth = {
    overflows: 0,
    renderThrows: 0,
    malformedFrames: 0,
    truncatedGaps: 0,
  };
  // Rolling timestamps of heavyweight-recovery churn — stream_overflow closes
  // AND truncated resyncs (both via recordChurnAndMaybeTrip) — feeding the
  // degraded-catchup limiter (overflowWindowTripped); plus the cooldown-ladder
  // state, keyed off the last-trip timestamp via degradedCooldownStep — never
  // off overflowTimes (enterDegradedCatchup clears it each trip).  See
  // enterDegradedCatchup.
  const overflowTimes = [];
  let degradedTimer = null;
  // Pending jittered truncated-resync (see scheduleTruncatedResync).
  // Cancelled by closeStreamTransport alongside degradedTimer, so any path
  // that tears the transport down (redial, close-on-hide, degraded entry,
  // close-session, destroy) also discards a resync scheduled for the OLD
  // stream — the next connect's own truncated envelope reschedules if the
  // gap still exists (truncatedFromCursor re-presents it).
  let truncatedResyncTimer = null;
  let degradedCooldownMs = DEGRADED_COOLDOWN_BASE_MS;
  let lastDegradedAt = 0;
  // Close-on-hide / replay-on-show bookkeeping.  A hidden tab's throttled event
  // loop is the likeliest too-slow SSE consumer, so the visibilitychange
  // handler closes the stream on hide and reconnects with the saved lastEventId
  // on show (replay_ok covers the gap).  hiddenDisconnect marks that WE closed
  // for hide, so show never resurrects a stream closed deliberately elsewhere.
  let visHandler = null;
  let hiddenDisconnect = false;
  // Ids of operator-context system turns already painted from /history.  A
  // later SSE replay that redelivers one (resume-cursor overlap) is skipped
  // by the system_turn handler — reset per refetchHistory.  Mirrors
  // ui/static/app.js's per-pane _renderedSystemEventIds.
  const renderedSystemEventIds = new Set();

  // Compaction lifecycle holder for the shared reducer
  // (conversation.applyCompactionEvent); `card` is the in-progress card
  // between start and end, nulled wherever the transcript is wiped — live
  // events re-create it defensively.
  const compactionHolder = { card: null, cid: null };

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
    "smart_approval",
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
      return esc(redactCredentials(rawText));
    }
    // Normalize to an array of rows we can linkify.
    let rows = [];
    if (Array.isArray(parsed.children)) {
      rows = parsed.children;
    } else if (parsed.ws_id && parsed.node_id) {
      rows = [parsed];
    }
    if (rows.length === 0) {
      return (
        "<pre>" +
        esc(redactCredentials(JSON.stringify(parsed, null, 2))) +
        "</pre>"
      );
    }
    const lines = rows.map((row) => {
      const safeWs = row.ws_id && WS_ID_RE.test(row.ws_id) ? row.ws_id : null;
      const safeNode =
        row.node_id && NODE_ID_RE.test(row.node_id) ? row.node_id : null;
      let link = safeWs || "";
      if (safeWs && safeNode) {
        link =
          '<a class="coord-ws-link" target="_blank" rel="noopener"' +
          ' data-ws-id="' +
          esc(safeWs) +
          '" data-node-id="' +
          esc(safeNode) +
          '" href="/node/' +
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
    // First-class operator-context system turn — styled by the shared
    // `.msg.system-context` rule (turnstone/shared_static/chat.css).  The
    // generic history-replay branch already renders unknown roles via
    // appendText("system", …); this variant gives it the operator styling.
    // The `operator-context` marker is shared by every operator row (this
    // bubble + the watch-result / guard-finding / idle-children cards) so the
    // retry-skip walk in _refreshRetryButton can skip them all uniformly.
    system: "system-context operator-context",
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
    setSafeHtml(body, html);
    el.appendChild(body);
    messagesEl.appendChild(el);
    _scheduleScroll();
    return el;
  }

  function appendText(role, text, opts) {
    return appendMsg(role, esc(text), opts);
  }

  // Structured ``.msg.watch-result`` card for a ``watch_triggered``
  // operator-context system turn — command-preview header + shell-output body
  // + poll-counter footer.  ``content`` is the formatted watch body (the system
  // turn's content); ``meta`` carries the structured fields (``watch_name`` /
  // ``command`` / ``poll_count`` / ``max_polls`` / ``is_final``) delivered live
  // on the ``system_turn`` SSE event and on the ``/history`` projection.  All
  // text goes through ``textContent`` so shell output containing angle brackets
  // / scripts / steering bytes renders inertly.  Delegates to the shared
  // conversation.buildWatchResultCard; this wrapper appends + scrolls.
  function appendWatchResult(meta, content) {
    const el = buildWatchResultCard(meta, content);
    messagesEl.appendChild(el);
    _scheduleScroll();
    return el;
  }

  // Structured ``.msg.guard-finding`` card for an ``output_guard``
  // operator-context system turn.  ``meta`` carries ``{flags, risk_level,
  // annotations, redacted}``.  Reuses the tool-row warning chip's risk / flags
  // / redaction vocabulary (``.conv-warning``) so guard findings read
  // identically wherever they surface, then appends the annotations the inline
  // tool chip omits.  All text via textContent.  Mirrors the interactive pane's
  // _buildGuardFindingBubble.
  function appendGuardFinding(meta) {
    const el = document.createElement("div");
    el.className = "msg guard-finding operator-context";
    el.setAttribute("role", "article");
    el.setAttribute("data-ts-role", "output_guard");
    el.setAttribute("aria-label", "output guard");
    const warn = buildConvWarning(meta);
    el.appendChild(warn);
    const anns = Array.isArray(meta.annotations) ? meta.annotations : [];
    for (let i = 0; i < anns.length; i++) {
      const a = document.createElement("div");
      a.className = "msg-guard-annotation";
      a.textContent = String(anns[i]);
      el.appendChild(a);
    }
    messagesEl.appendChild(el);
    _scheduleScroll();
    return el;
  }

  // Structured ``.msg.idle-children`` card for the coordinator-only
  // ``idle_children`` operator-context system turn — lists the child
  // workstreams still running while the coordinator went idle.  ``meta.children``
  // is ``[{ws_id, name, state}]`` (names already ``sanitize_name``-cleaned at the
  // producer); rendered via textContent so a hostile workstream name is inert.
  function appendIdleChildren(meta) {
    const el = document.createElement("div");
    el.className = "msg idle-children operator-context";
    el.setAttribute("role", "article");
    el.setAttribute("data-ts-role", "idle_children");
    el.setAttribute("aria-label", "idle children");
    const children = Array.isArray(meta.children) ? meta.children : [];
    const header = document.createElement("div");
    header.className = "msg-idle-header";
    header.textContent =
      "idle · " +
      children.length +
      (children.length === 1
        ? " child still running"
        : " children still running");
    el.appendChild(header);
    const list = document.createElement("ul");
    list.className = "msg-idle-list";
    for (let i = 0; i < children.length; i++) {
      const c = children[i] || {};
      const li = document.createElement("li");
      li.className = "msg-idle-child";
      const name = document.createElement("span");
      name.className = "msg-idle-child-name";
      name.textContent = String(c.name || c.ws_id || "child");
      li.appendChild(name);
      if (c.state) {
        const state = document.createElement("span");
        state.className = "msg-idle-child-state";
        state.textContent = String(c.state);
        li.appendChild(state);
      }
      list.appendChild(li);
    }
    el.appendChild(list);
    messagesEl.appendChild(el);
    _scheduleScroll();
    return el;
  }

  // "queued message" bubble for a ``user_interjection`` system turn — shows the
  // user's raw words (``meta.message``) rather than the model-directed framing
  // baked into ``content``, with brighter emphasis for ``!!!``-important
  // interjections.  Reuses ``appendText`` and adds ``.important`` for ``!!!`` ones.
  function appendInterjection(meta, content) {
    const important = meta && meta.priority === "important";
    const text =
      meta && meta.message != null ? String(meta.message) : content || "";
    const el = appendText("system", text, {
      label: important ? "queued message · important" : "queued message",
    });
    if (important) el.classList.add("important");
    return el;
  }

  // Dispatch a first-class operator-context system turn to the right renderer.
  // Shared by the live ``system_turn`` SSE handler and history replay so the
  // two can't drift on which kinds get structured cards.  ``watch_triggered`` /
  // ``output_guard`` / ``idle_children`` carry structured ``meta`` → cards;
  // ``user_interjection`` → a "queued message" bubble; everything else → the
  // labeled operator bubble.
  function renderSystemTurn(source, content, meta) {
    const m = meta && typeof meta === "object" ? meta : null;
    // /history projection of a persisted compaction marker — same result
    // card the live `compaction` end event paints (shared builder).
    if (source === "compaction") {
      const card = buildCompactionCard(m, content || "");
      messagesEl.appendChild(card);
      _scheduleScroll();
      return card;
    }
    if (source === "watch_triggered" && m)
      return appendWatchResult(m, content || "");
    if (source === "output_guard" && m) return appendGuardFinding(m);
    if (source === "idle_children" && m) return appendIdleChildren(m);
    if (source === "user_interjection")
      return appendInterjection(m, content || "");
    return appendText("system", content || "", {
      label: operatorSourceLabel(source),
    });
  }

  // User-message bubble with attachment-pill cluster appended below
  // the text.  Mirrors Pane.addUserMessage in the interactive UI so
  // live-send and history-replay both render the same chip strip the
  // composer staged on submit.  Attachments is a list of
  // {kind, filename}; falsy/empty falls through to plain text.
  function appendUserMessageWithAttachments(text, attachments, opts) {
    const el = appendText("user", text, opts);
    // Per-message edit + rewind affordance (#549) on every user turn,
    // matching the interactive pane. Attached before the early-return so
    // image-only sends (no attachments) still get the action bar.
    _addUserMsgActions(el, text || "");
    if (!Array.isArray(attachments) || attachments.length === 0) return el;
    const pills = document.createElement("div");
    pills.className = "msg-user-attach";
    pills.setAttribute("role", "list");
    attachments.forEach((a) => {
      const kind = (a && a.kind) || "other";
      const pill = document.createElement("span");
      pill.className = "msg-user-attach-pill msg-user-attach-pill-" + kind;
      pill.setAttribute("role", "listitem");
      const icon = document.createElement("span");
      icon.className = "msg-user-attach-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent =
        typeof window.kindIcon === "function"
          ? window.kindIcon(kind)
          : kind === "image"
            ? "🖼"
            : kind === "audio"
              ? "🎵"
              : "📄";
      pill.appendChild(icon);
      const name = document.createElement("span");
      name.className = "msg-user-attach-name";
      name.textContent =
        (a && a.filename) ||
        (kind === "image" ? "image" : kind === "audio" ? "audio" : "document");
      pill.appendChild(name);
      // Inline preview (image/pdf thumbnail, audio player) — the same affordance
      // the interactive pane renders, shared via the buildAttachmentPreview
      // window bridge composer_attachments.js installs.  No-ops on history
      // replay (the /history projection omits attachment_id), matching interactive.
      const prev =
        typeof window.buildAttachmentPreview === "function"
          ? window.buildAttachmentPreview({
              kind: kind,
              wsId: wsId,
              attachmentId: a && a.attachment_id,
              filename: a && a.filename,
            })
          : null;
      if (prev) {
        if (kind === "image" || kind === "pdf") icon.replaceWith(prev);
        else pill.appendChild(prev);
      }
      pills.appendChild(pill);
    });
    el.appendChild(pills);
    _scheduleScroll();
    return el;
  }

  // Thin ``.msg.user.system-nudge`` marker rendered for a wake-driven
  // empty user turn.  Replaces the previously-invisible synthetic empty
  // user turn with a visible-but-subtle DOM element; the nudges it
  // carried are now first-class operator-context ``system`` turns that
  // follow it and render via the ``system`` ``_MSG_VARIANTS`` styling.
  function appendSystemNudgeMarker() {
    const el = buildSystemNudgeMarker();
    messagesEl.appendChild(el);
    return el;
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
      // truncated.  Matches the interactive replay's fallback in
      // shared_static/interactive.js replayHistory (the live parity
      // source; the old ui/static/app.js Pane.replayHistory moved
      // there in the L-shell step-5a lift).
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
      // The batch may have been --running (live tool_info auto path,
      // approval_resolved approved path, or replay-time orphan).
      // Drop --running once every row in the batch has a result so
      // the kicker text + visual style flip back to the post-execution
      // state.  Per-row check (not a counter) keeps the logic
      // resilient to out-of-order replay + late SSE deliveries.
      _unsetBatchRunningIfAllResults(entry.batch);
      // Result blocks grow scrollHeight; without this the user pinned
      // at the bottom loses their pin when the row inflates.  appendMsg
      // already routes through _scheduleScroll on the legacy path; this
      // branch was the gap.
      _scheduleScroll();
      return entry.row;
    }
    // Orphan result (no live row — replay edge): still render the MCP
    // error card rather than the raw envelope (#725).
    const orphanCard = _tryMcpErrorBlock(isError, output);
    if (orphanCard) {
      const el = appendMsg("error", "", {
        label: "error · " + (name || "tool"),
        callId: callId,
      });
      el.querySelector(".msg-body").appendChild(orphanCard);
      return el;
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
  //   - solo (1 call, serial):  .conv-batch--solo
  //   - parallel (≥2 calls):    .conv-batch--parallel
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
    batch.querySelectorAll(".conv-row").forEach((r) => {
      const t = r.dataset.verdictTier;
      if (t === "llm" && llmModel === null) {
        llmModel = r.dataset.verdictModel || "";
      } else if (t === "heuristic") {
        hasHeuristic = true;
      }
    });
    const head = batch.querySelector(".conv-batch-head");
    if (!head) return;
    let tierEl = head.querySelector(".conv-batch-tier");
    const label = _formatTierLabel(llmModel, hasHeuristic);
    if (label) {
      if (!tierEl) {
        tierEl = document.createElement("span");
        tierEl.className = "conv-batch-tier";
        head.appendChild(tierEl);
      }
      if (tierEl.textContent !== label) tierEl.textContent = label;
    } else if (tierEl) {
      tierEl.remove();
    }
  }

  // Apply / refresh per-row status state from an item payload:
  //  - data-needs-approval="1" when item.needs_approval && !item.error
  //  - .conv-row-status--auto pill when item.auto_approved
  //  - .conv-row-status--error pill + .error class when policy-
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
    const callLine = row.querySelector(".conv-row-call");
    if (callLine) {
      callLine.querySelectorAll(".conv-row-status").forEach((p) => p.remove());
    }
    delete row.dataset.needsApproval;
    // Don't clear .error here when it came from a result (no
    // matching item.error); only clear the static-policy marker.
    // Approximate by re-deriving solely from item below.
    row.classList.remove("error");
    if (item.needs_approval && !item.error) {
      row.dataset.needsApproval = "1";
    }
    // Auto-approve indicator: reconcile the inline name tag
    // (.conv-row-auto) from item.auto_approved so an upgrade-in-place (SSE
    // folding auto state into an already-rendered row) adds it; buildConvRow
    // adds it at initial render, so this just keeps the two in sync.
    const nameEl = callLine && callLine.querySelector(".conv-row-name");
    if (nameEl) {
      const existingAuto = nameEl.querySelector(".conv-row-auto");
      if (item.auto_approved && !item.needs_approval) {
        if (!existingAuto) {
          const auto = document.createElement("span");
          auto.className = "conv-row-auto";
          const reason = _normaliseAutoApproveReason(item.auto_approve_reason);
          auto.textContent = " auto: " + reason;
          auto.title = "auto-approved (no operator prompt) — reason: " + reason;
          nameEl.appendChild(auto);
        }
      } else if (existingAuto) {
        existingAuto.remove();
      }
    }
    if (callLine && item.error && !item.needs_approval) {
      const errPill = document.createElement("span");
      errPill.className = "conv-row-status conv-row-status--error";
      errPill.textContent = "✗ " + (item.error || "blocked");
      callLine.appendChild(errPill);
      row.classList.add("error");
    }
    // If a runtime tool_result already landed an .error on this row,
    // re-derive it from the result block's presence so we don't
    // accidentally drop the cue when refreshing from a non-error
    // item shape.
    if (row.querySelector(".conv-row-result.conv-row-result--error")) {
      row.classList.add("error");
    }
  }

  function _renderBatchRow(item, indexLabel) {
    const row = buildConvRow(item, {
      indexLabel: indexLabel || "",
      argsText: _formatBatchArgs(item),
    });
    // Apply status pills + data-needs-approval through the shared helper so
    // initial render and upgrade-in-place can't drift.
    _refreshRowStatus(row, item);
    return row;
  }

  // Attach an output-guard finding chip to a specific .conv-row.
  // Idempotent — replacing an existing chip in place lets the live
  // SSE handler upgrade severity without stacking duplicates when a
  // late event arrives after replay seeded an initial chip.
  function _attachOutputWarningChip(row, oa) {
    // Idempotent: replace any existing chip in place so a late SSE upgrade
    // (severity escalation) doesn't stack duplicates next to the replay seed.
    // The chip's risk / flags / redaction / tier / reasoning are all built by
    // the shared buildConvWarning (reasoning is now inline, not a <details>).
    if (!row || !oa) return;
    const existing = row.querySelector(".conv-warning");
    const chip = buildConvWarning(oa);
    if (existing) existing.replaceWith(chip);
    else row.appendChild(chip);
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
    // Joined on U+001F (unit separator) — a control char that can't appear in
    // any of these fields, so the signature can't collide across differing
    // splits. Built via fromCharCode to keep the source ASCII-clean (no raw
    // control byte in the file).
    return [
      verdict.recommendation || "",
      verdict.risk_level || "",
      verdict.confidence != null ? String(verdict.confidence) : "",
      verdict.reasoning || "",
      verdict.tier || "",
      verdict.judge_model || "",
    ].join(String.fromCharCode(0x1f));
  }

  function _appendVerdictLineTo(row, verdict) {
    // Dedupe by signature so reconnect storms don't tear down + rebuild
    // an unchanged verdict line.  _appendVerdictLineTo(row, null) is
    // used by _appendJudgePendingLineTo to clear and start fresh — that
    // path explicitly bypasses the dedupe (sig of null is "").
    const sig = _verdictSig(verdict);
    if (verdict && row.dataset.verdictSig === sig) {
      return row.querySelector(".conv-verdict");
    }
    row.dataset.verdictSig = sig;

    // Drop any prior verdict badge + its detail sibling before rebuilding.
    const prevBadge = row.querySelector(".conv-verdict");
    if (prevBadge) {
      const prevDetail = prevBadge.nextElementSibling;
      if (prevDetail && prevDetail.classList.contains("conv-verdict-detail")) {
        prevDetail.remove();
      }
      prevBadge.remove();
    }
    if (verdict) {
      const frag = buildConvVerdict(verdict);
      const callEl = row.querySelector(".conv-row-call");
      if (callEl && callEl.nextSibling) {
        row.insertBefore(frag, callEl.nextSibling);
      } else {
        row.appendChild(frag);
      }
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
    _refreshBatchTier(row.closest(".conv-batch"));
    return row.querySelector(".conv-verdict");
  }

  function _appendJudgePendingLineTo(row) {
    // Clear any prior verdict, then render the spinner-only badge (neutral
    // stripe -- risk isn't known until the judge lands).
    _appendVerdictLineTo(row, null);
    const frag = buildConvVerdict(null, { judgePending: true });
    const callEl = row.querySelector(".conv-row-call");
    if (callEl && callEl.nextSibling) {
      row.insertBefore(frag, callEl.nextSibling);
    } else {
      row.appendChild(frag);
    }
  }

  function _appendResultToRow(row, output, isError, opts) {
    if (!row) return;
    const existing = row.querySelector(".conv-row-result");
    if (existing) existing.remove();
    if (isError) {
      row.classList.add("error");
      // Lift the row's error onto the enclosing batch so the left
      // stripe + status pill (--error) cue the operator at the batch
      // level too.  Idempotent — re-fires don't stack.
      const batch = row.closest(".conv-batch");
      if (batch) batch.classList.add("conv-batch--error");
    }
    // Structured MCP error envelope (consent / re-consent / forbidden /
    // operator) renders as the shared card instead of raw JSON — the same
    // dispatch the interactive pane does (#725).  The card's Connect popup
    // works here unchanged: the console hosts /v1/api/mcp/oauth/start and
    // consent_url is relative.  The conv-row-result marker keeps the
    // replace-existing logic above and _refreshRowStatus working when the
    // result is the card.
    let block = _tryMcpErrorBlock(isError, output);
    if (block) block.classList.add("conv-row-result");
    if (!block) block = buildConvResult(output, { isError });
    // Marker class so _refreshRowStatus can preserve the row's .error state
    // across upgrade-in-place when the error came from a tool_result.
    if (isError) block.classList.add("conv-row-result--error");
    row.appendChild(block);
  }

  // Concise, screen-reader friendly summary of a pending batch — used
  // both as the .conv-batch[role=region] aria-label and as the
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
    return batchKicker("pending", items.length);
  }

  function _buildBatchActions(batch, items) {
    const alwaysNames = items
      .filter(
        (it) =>
          it.needs_approval &&
          it.func_name &&
          it.func_name !== "__budget_override__" &&
          !it.error,
      )
      .map((it) => it.approval_label || it.func_name);
    const alwaysLabel = alwaysNames.length
      ? "Always approve " + alwaysNames.join(", ")
      : "";
    return buildConvActions({
      kbd: { approve: "⏎", deny: "D", always: "⇧A" },
      alwaysLabel,
      onApprove: () => _resolveBatchAction(batch, true, false),
      onDeny: () => _resolveBatchAction(batch, false, false),
      onAlways: () => _resolveBatchAction(batch, true, true),
    });
  }

  async function _resolveBatchAction(batch, approved, always) {
    // Pick a call_id from a row that's actually in the server's
    // pending_items (data-needs-approval="1").  approve_request
    // envelopes carry the FULL items list including auto-approved /
    // policy-blocked siblings; resolving against one of those would
    // 409 since the server's pending_items wouldn't recognise it.
    const pendingRow = batch.querySelector(
      '.conv-row[data-needs-approval="1"][data-call-id]',
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
        cycle_id: batch.dataset.cycleId || null,
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
    batch.querySelectorAll(".conv-actions button").forEach((b) => {
      b.disabled = !!disabled;
    });
  }

  // The tool-batch currently awaiting an approval decision (for the keyboard
  // shortcuts): the last .conv-batch with a still-pending row whose actions
  // aren't already disabled — i.e. not mid-resolve, so a key never double-fires.
  function _currentPendingBatch() {
    const batches = messagesEl.querySelectorAll(".conv-batch");
    for (let i = batches.length - 1; i >= 0; i--) {
      const b = batches[i];
      if (!b.querySelector('.conv-row[data-needs-approval="1"][data-call-id]'))
        continue;
      const btn = b.querySelector(".conv-actions button");
      if (btn && btn.disabled) continue; // mid-resolve — don't double-fire
      return b;
    }
    return null;
  }

  // Build the resolved-state status pill.  Shared between the live
  // _morphBatchResolved path (post-approve, post-deny) and the
  // history-replay path inside appendToolBatch (renders resolved
  // batches without ever showing actions).
  function _buildStatusPill(opts) {
    return buildConvStatus(opts);
  }

  function _setBatchRunning(batch) {
    if (!batch) return;
    batch.classList.add("conv-batch--running");
    const kicker = batch.querySelector(".conv-batch-kicker");
    if (kicker) {
      const rowCount = batch.querySelectorAll(".conv-row").length;
      kicker.textContent = batchKicker("running", rowCount);
    }
  }

  function _unsetBatchRunningIfAllResults(batch) {
    // Remove ``--running`` once every row in the batch has rendered a
    // result block.  Caller invokes after each tool_result; the test
    // is "did THIS result complete the batch?" — cheap DOM walk over
    // the same handful of rows we already track.
    if (!batch) return;
    if (!batch.classList.contains("conv-batch--running")) return;
    const rows = batch.querySelectorAll(".conv-row");
    for (const row of rows) {
      if (!row.querySelector(".conv-row-result")) return;
    }
    batch.classList.remove("conv-batch--running");
    // Clear the SR busy state too — a batch that completes via tool_result only
    // (judge + gate both bypassed, early-paint on) never hits the approve_request
    // / tool_info paths that remove aria-busy, so without this it keeps announcing
    // "busy" after completion.
    batch.removeAttribute("aria-busy");
    const kicker = batch.querySelector(".conv-batch-kicker");
    if (kicker && !batch.classList.contains("conv-batch--pending")) {
      const rowCount = rows.length;
      kicker.textContent = batchKicker("done", rowCount);
    }
  }

  function _morphBatchResolved(batch, opts) {
    if (!batch) return;
    batch.classList.remove("conv-batch--pending");
    batch.classList.add(
      opts.approved ? "conv-batch--approved" : "conv-batch--denied",
    );
    // Drop the [role=region] approval landmark so the resolved batch
    // stops claiming "Approval required" in SR landmark navigation.
    batch.removeAttribute("role");
    batch.removeAttribute("aria-label");
    const actions = batch.querySelector(".conv-actions");
    if (actions) actions.replaceWith(_buildStatusPill(opts));
    if (activeBatch === batch) activeBatch = null;
  }

  function _focusBatchPrimary(batch, prefer) {
    if (!batch) return;
    const role = prefer === "deny" ? "deny" : "approve";
    const btn = batch.querySelector(
      '.conv-actions button[data-role="' + role + '"]',
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
      // Late cycle identity (SSE approve_request upgrading a replay /
      // early-paint shell) — stamp it so the approve POST can route.
      if (opts.cycleId) existing.dataset.cycleId = opts.cycleId;
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
      if (opts.pending && !existing.classList.contains("conv-batch--pending")) {
        existing.classList.remove(
          "conv-batch--approved",
          "conv-batch--denied",
          "conv-batch--auto",
          "conv-batch--running",
          "conv-batch--error",
        );
        existing.classList.add("conv-batch--pending");
        existing.removeAttribute("aria-busy"); // announce resolved → human gate
        existing.setAttribute("role", "region");
        existing.setAttribute("aria-label", _approvalAriaLabel(items));
        const kicker = existing.querySelector(".conv-batch-kicker");
        if (kicker) {
          kicker.textContent = _pendingKickerText(items);
        }
        const statusEl = existing.querySelector(".conv-status");
        const actionsEl = existing.querySelector(".conv-actions");
        const newActions = _buildBatchActions(existing, items);
        if (statusEl) statusEl.replaceWith(newActions);
        else if (actionsEl) actionsEl.replaceWith(newActions);
        else existing.appendChild(newActions);
        activeBatch = existing;
        _announceAssertive(_approvalAriaLabel(items));
      } else if (
        opts.auto &&
        existing.classList.contains("conv-batch--running") &&
        !existing.classList.contains("conv-batch--auto")
      ) {
        // SSE tool_info clarifies an existing --running batch as
        // auto-approved.  Keep --running (the tool is still in
        // flight; tool_result will remove it) and add --auto so the
        // batch reflects BOTH "auto-approved" + "running" — historical
        // behaviour swapped --running out, which lost the running
        // indicator the moment tool_info clarified the approval state.
        existing.classList.add("conv-batch--auto");
        existing.removeAttribute("aria-busy"); // announce resolved → auto-running
        // An early-paint (announce) batch's kicker reads "Evaluating";
        // the tool is now actually in flight, so swap it to the running
        // indicator.  No-op for a true replay orphan whose placeholder
        // kicker already read "Running".
        const runKicker = existing.querySelector(".conv-batch-kicker");
        if (runKicker) {
          runKicker.textContent = batchKicker("running", items.length);
        }
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
        // .conv-row-result--error marker.
        _refreshRowStatus(entry.row, it);
        const cached = judgeVerdicts.get(it.call_id);
        const v = it.judge_verdict || it.heuristic_verdict || cached;
        if (v) {
          _appendVerdictLineTo(entry.row, v);
        } else if (
          it.needs_approval &&
          opts.judgePending &&
          !entry.row.querySelector(".conv-verdict")
        ) {
          _appendJudgePendingLineTo(entry.row);
        }
      });
      return existing;
    }

    let kickerText;
    if (opts.pending) {
      kickerText = _pendingKickerText(items);
    } else if (opts.announce) {
      kickerText = batchKicker("evaluating", items.length);
    } else if (opts.running) {
      kickerText = batchKicker("running", items.length);
    } else if (items.length >= 2) {
      kickerText = batchKicker("done", items.length);
    } else {
      kickerText = "Tool";
    }
    const firstName =
      items[0] && (items[0].func_name || items[0].approval_label)
        ? items[0].func_name || items[0].approval_label
        : "tool";
    const summaryText =
      items.length >= 2
        ? firstName + " + " + (items.length - 1) + " more"
        : firstName;
    const batch = buildConvBatchShell({
      parallel: items.length >= 2,
      kickerText,
      summaryText,
      tierText: _pickBatchTier(items),
    });
    // Approval-cycle identity — _resolveBatchAction posts it back so
    // the decision lands on exactly this round when several batches
    // are pending (parallel task agents).
    if (opts.cycleId) batch.dataset.cycleId = opts.cycleId;
    if (opts.pending) batch.classList.add("conv-batch--pending");
    else if (opts.auto) batch.classList.add("conv-batch--auto");
    else if (opts.resolved) {
      batch.classList.add(
        opts.resolved.approved ? "conv-batch--approved" : "conv-batch--denied",
      );
    }
    // ``running`` is additive -- coexists with ``auto`` or stands alone
    // (replay-time orphan before SSE clarifies the approval state).
    if (opts.running) batch.classList.add("conv-batch--running");
    // Early-paint shell is an indeterminate region until the judge/gate
    // resolves; the --pending / --auto upgrade clears it below.
    if (opts.announce) batch.setAttribute("aria-busy", "true");

    let anyRowError = false;
    const renderedRows = [];
    items.forEach((it, idx) => {
      const idxLabel = indexLabel(idx, items.length);
      const row = _renderBatchRow(it, idxLabel);
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
      renderedRows[0].classList.add("conv-row--first");
      renderedRows[renderedRows.length - 1].classList.add("conv-row--last");
    }
    // Lift any policy-blocked row's error onto the enclosing batch so
    // the left stripe + status pill cue the operator at the batch
    // level too.  _appendResultToRow does the same for runtime errors
    // arriving via tool_result.
    if (anyRowError) batch.classList.add("conv-batch--error");

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
        noteRenderThrow("streamingRender", e);
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
          noteRenderThrow("streamingRenderFinalize", e);
        }
      }
    }
    currentAssistantEl = null;
    currentAssistantBuf = "";
    currentReasoningEl = null;
    currentReasoningBuf = "";
    messagesEl.setAttribute("aria-live", "polite");
    // Move the retry affordance onto the just-completed last assistant turn
    // (#549). No-op when the turn ended tool-only (see _refreshRetryButton).
    _refreshRetryButton();
  }

  // ------------------------------------------------------------------
  // Approval UI — entry point used by the approve_request SSE handler.
  // The legacy dock + its public surface (hideApproval / coordApprove)
  // were removed when approvals moved into the inline batch construct.
  // The SSE approval_resolved handler now drives _morphBatchResolved
  // directly, and inline action buttons drive _resolveBatchAction.
  // ------------------------------------------------------------------

  function showApproval(items, judgePending, cycleId) {
    const list = (items || []).filter(Boolean);
    if (list.length === 0) return;
    const batch = appendToolBatch(list, {
      pending: true,
      judgePending: !!judgePending,
      cycleId: cycleId || "",
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
  function setBusy(b, source) {
    const next = !!b;
    // Who asserted busy: "server" (default — state events and every
    // existing/future writer) or "optimistic" (ONLY coordSend's pre-POST
    // flip). The deferred/queue_full settle arms may clear busy solely
    // while it is still this send's own optimistic flip — a server-
    // stamped busy is a real turn and must never be clobbered.
    // Centralized HERE so an unstamped future writer fails safe.
    busySource = next ? source || "server" : null;
    composer.setBusy(next);
    // Greys out the per-message edit/rewind/retry buttons while a generation
    // is in flight (CSS: [data-busy="true"] .msg-action-btn).
    messagesEl.setAttribute("data-busy", next ? "true" : "false");
    const edge = next !== busy;
    busy = next;
    reconcileSendBlock();
    if (edge && !next) queue.onIdleEdge();
  }

  // Shared-workstream send gate: block this viewer's send while another
  // participant's turn is in flight (their credentials, not this viewer's,
  // would run any MCP tool an interjection triggers, and the message would be
  // misattributed to them). The server also rejects it with a 409; this is
  // the proactive UX half. No-ops on a single-user coordinator (acting user is
  // this viewer) or when the acting id is unknown. Mirrors the interactive
  // pane's _reconcileSendBlock.
  function reconcileSendBlock() {
    let me = null;
    try {
      me = sessionStorage.getItem("ts.user_id");
    } catch (_e) {
      me = null;
    }
    const blocked = !!busy && !!actingUserId && !!me && actingUserId !== me;
    composer.setSendBlocked(
      blocked,
      blocked
        ? "Another participant's turn is in progress - wait for it to finish."
        : "",
    );
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
        tokensEl: sbTokensEl,
        toolsEl: sbToolsEl,
        turnsEl: sbTurnsEl,
      },
      evt,
    );
    // Model moved out of the status bar into the composer chip; capture the
    // (optional) effort off the status event and repaint the chip.  evt is
    // guaranteed non-null by the early return at the top of updateStatusBar.
    coordEffort = evt.effort || "";
    paintCoordModelChip();
    lastStatusEvt = evt;
  }

  // Paint the composer's "model · effort" read-out chip.  SILENT_EFFORTS
  // mirror (status_bar.js): "medium" is the implicit default and "" means no
  // knob — neither is worth showing, so only a non-medium effort appends the
  // "· effort" suffix.  An empty alias resets the chip to its placeholder.
  function paintCoordModelChip() {
    if (!composer || !composer.setModel) return;
    const alias = coordModelAlias || coordModel || "";
    const eff =
      coordEffort && coordEffort !== "medium" ? " · " + coordEffort : "";
    composer.setModel(alias ? alias + eff : "");
  }

  // Paint the composer's "has a project" badge from the connected event's
  // project_name ("" = none → hidden).
  function paintCoordProjectChip() {
    if (composer && composer.setProject)
      composer.setProject(coordProjectName || "");
  }

  function coordSend() {
    const text = composer.value;
    const trimmed = (text || "").trim();
    if (!trimmed) return false;

    const snap = attachments.snapshot();

    let queuedEl = null;
    let optimisticEl = null;
    const isBusy = busy;
    // Display-only strip of the !!! prefix (the server re-parses it
    // authoritatively); shared parse so the settle helper's retro-convert
    // renders the same chip either pane would have built pre-POST.
    const { displayText, priority } = parsePriority(trimmed);
    if (isBusy) {
      queuedEl = queue.addQueuedMessage(displayText, priority);
    } else {
      // "optimistic": no server state event asserted this — the settle
      // arms may undo it if the send turns out deferred/refused (see
      // setBusy's busySource contract).
      setBusy(true, "optimistic");
      // snap.attachments carries the chip metadata (kind + filename)
      // for every stable chip the composer holds; pass it through so
      // the optimistic user bubble shows the same pill cluster the
      // history-replay path renders below.
      optimisticEl = appendUserMessageWithAttachments(
        trimmed,
        snap.attachments,
        {
          label: "you",
        },
      );
    }
    composer.clear();

    // Bound the send POST with an AbortController + ~15s timeout (mirrors
    // composer_queue.js _deleteRequest) so a wedged proxied node can't leave a
    // pre-bind-dismissed card frozen forever — bind/promote/remove only run
    // off this response, so the .catch must always eventually fire.
    const sendCtrl =
      typeof AbortController === "function" ? new AbortController() : null;
    const sendInit = {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: trimmed,
        attachment_ids: snap.attachment_ids,
      }),
    };
    let sendTimer = null;
    if (sendCtrl) {
      sendInit.signal = sendCtrl.signal;
      // Same policy as the interactive composer: flat wedged-node bound —
      // every /send answers within RTT now (dispatched, queued, or
      // deferred-with-msg_id during a command window; the server parks
      // nothing against this POST).  Dismissal is bind() → DELETE.
      sendTimer = setTimeout(() => sendCtrl.abort(), 15000);
    }
    let sendReq = authFetch(
      "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send",
      sendInit,
    );
    if (sendTimer) sendReq = sendReq.finally(() => clearTimeout(sendTimer));
    sendReq
      .then((r) => {
        // A rejected send (4xx/5xx) carries {error}, not {status}; without
        // this guard it falls through to the "unknown status" branch and gets
        // promote()'d — a server-refused message shown as delivered (with a
        // false "already sent" notice if it was dismissed). Route it to the
        // .catch (removes the bubble + shows the error) instead, surfacing the
        // server's {error} text ("No session", a rate-limit reason, etc.)
        // rather than a bare status code. A wedged proxy can answer non-JSON
        // (502/504 HTML); the parse-failure arm falls back to the status code
        // so that can't surface as an "Unexpected token <" error.
        if (!r.ok) {
          // 409 = the server-side cross-user interjection block; convert to a
          // handled status so it routes to the clean branch below (not the
          // generic error). Reactive fallback for the race where the send
          // button wasn't yet disabled.
          if (r.status === 409) {
            return r.json().then(
              (b) => ({
                status: "cross_user_interjection",
                error: (b && b.error) || "",
              }),
              () => ({ status: "cross_user_interjection", error: "" }),
            );
          }
          return r.json().then(
            (b) => {
              throw new Error((b && b.error) || "send_http_" + r.status);
            },
            () => {
              throw new Error("send_http_" + r.status);
            },
          );
        }
        return r.json();
      })
      .then((data) => {
        // The full status dispatch (queued/retro-convert, busy,
        // queue_full, attachments_busy, cross_user, unknown-ok) lives in
        // the shared helper — ONE settle matrix for both panes; see
        // settleSendResponse's contract for the arm semantics.
        settleSendResponse(queue, data, {
          queuedEl,
          optimisticEl,
          isBusy,
          displayText,
          priority,
          setBusy: (b) => setBusy(b),
          busyIsOptimistic: () => busy && busySource === "optimistic",
          paneIsBusy: () => busy,
          renderError: (msg) => appendText("error", msg, { label: "error" }),
          consumeAttachments: (attached, droppedIds) =>
            attachments.consume(attached, droppedIds),
        });
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
  }

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

  async function coordCloseSession() {
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
      suspendStream();
      // Session teardown owns the stream from here: a tab hide→show while
      // the /close POST is in flight must NOT reopen a stream against the
      // workstream the server is tearing down (404 / reconnect churn against
      // a dead session).  connectSSE reinstalls the handler, so the failure
      // paths below get close-on-hide back for free via resumeSse.
      removeVisibilityHandler();
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
    // Pane-hosted (console): close the tab + tear the controller down via the
    // PaneManager.  The standalone page passes no onClose → console redirect.
    if (opts && opts.onClose) opts.onClose();
    else window.location.href = "/";
  }

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
    if (!sseEl) return; // no header -> no SSE indicator (reconnect still runs)
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

  // The transport-teardown chokepoint: close + null the EventSource and
  // cancel the pending retry timers (reconnect backoff, degraded catch-up,
  // truncated resync).  Every teardown path routes through here —
  // connectSSE's redial prologue, suspendStream (overflow / hide /
  // close-session / truncated resync), destroy — so a new transport timer
  // gets cancelled in one place instead of by hand at each call site.
  // Cancelling the degraded timer on redial also keeps a pending catch-up
  // retry from firing mid-stream and double-opening; enterDegradedCatchup
  // re-arms AFTER its own suspend, so this never cancels the timer it is
  // about to set.  Likewise the truncated-resync fire path nulls its own
  // handle BEFORE calling loadHistoryThenReconnect, so the teardown inside
  // that flow never cancels the work it is part of; cancelling a merely
  // PENDING resync here is always safe because truncatedFromCursor (not the
  // timer) is the durable repair state — the next connect re-presents it.
  // Gap accounting stays OUT of this helper — a redial is not itself a gap
  // (suspendStream layers markStreamGap on top).
  function closeStreamTransport() {
    if (evtSource) {
      try {
        evtSource.close();
      } catch (_) {
        /* noop */
      }
      evtSource = null;
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (degradedTimer) {
      clearTimeout(degradedTimer);
      degradedTimer = null;
    }
    if (truncatedResyncTimer) {
      clearTimeout(truncatedResyncTimer);
      truncatedResyncTimer = null;
    }
  }

  function connectSSE() {
    closeStreamTransport();
    // Snapshot whether this connect attempt follows a prior disconnect
    // BEFORE onopen resets the flags — onopen's post-gap sidebar recovery
    // keys off it.  Native EventSource auto-reconnect no longer routes
    // through scheduleReconnect on the transient-error path, so the legacy
    // ``reconnectAttempts > 0`` check is always false after PR-D — use
    // ``disconnectedAt`` (stamped by markStreamGap from onerror and from every
    // deliberate suspend, cleared by onopen below) as the authoritative
    // "was-gap" flag.  Falls back to the legacy semantic for the genuinely
    // manual case (scheduleReconnect-driven reconnect after CLOSED state).
    const wasReconnecting = disconnectedAt !== 0 || reconnectAttempts > 0;
    let url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/events";
    // A recorded truncation gap OVERRIDES the live cursor: while
    // truncatedFromCursor is set (gap detected, no full render yet), every
    // manual (re)connect presents the truncation-time cursor, so the server
    // re-answers replay_truncated and the resync re-arms — whatever teardown
    // cancelled the pending jittered resync in the meantime (close-on-hide,
    // degraded entry, CLOSED-state retry, close-session resume).  This
    // connect chokepoint is what makes the gap-repair guarantee
    // interleaving-proof; a cancellable timer alone silently loses the
    // repair when a hide/show cycle lands inside the jitter window after
    // the live cursor has advanced past the truncation point.  Safe against
    // double-render: ring eviction is forward-only, so a once-truncated
    // cursor can never re-enter replay range — this connect always draws
    // the envelope, never a bulk replay.  (Native EventSource
    // auto-reconnects bypass this — the browser's header carries the
    // advanced id — but they never run closeStreamTransport, so the pending
    // resync timer survives them.)
    //
    // ``!= null`` (not truthiness): a resume cursor of 0 is valid (the
    // ring buffer's first emitted event is id 1), and a brand-new ws's
    // first-turn /history cursor can be 0 — a truthiness gate would drop
    // it to the lossy fresh-snapshot path.  Mirrors ui/static/app.js.
    const connectCursor =
      truncatedFromCursor != null ? truncatedFromCursor : lastEventId;
    if (connectCursor != null) {
      url += "?last_event_id=" + encodeURIComponent(connectCursor);
    }
    // Close-on-hide / replay-on-show: install once per pane, removed by
    // destroy().  A hidden tab's throttled drain is the likeliest slow consumer
    // behind a server-side queue overflow, and an idle hidden tab holds a node
    // connection for nothing — closing on hide removes both, and the saved
    // lastEventId makes the show-edge reconnect lossless (replay_ok).
    if (!visHandler) {
      // onVisibilityChange is a plain closure function (no `this` to bind), so
      // it serves directly as the once-install sentinel AND the
      // add/removeEventListener handle — no wrapper needed.
      visHandler = onVisibilityChange;
      document.addEventListener("visibilitychange", visHandler);
    }
    // Never open an EventSource into a hidden (throttled) tab — including a
    // FIRST connect in a background tab, where the close-on-hide handler never
    // fires because there was no open stream to close.  This single connect
    // chokepoint backstops every caller (init, scheduleReconnect, degraded
    // retry, show edge); the saved lastEventId + the handler installed just
    // above make the show-edge reconnect replay the gap.  The deferral IS a
    // gap — everything until the show edge is missed exactly as if the
    // transport had dropped — so mark it like one: without the mark, a pane
    // first opened in a background tab skipped onopen's post-gap recovery and
    // the sidebar silently missed every child/task the backend created while
    // hidden.  And say so honestly — "connecting…" (set below, only when an
    // attempt really starts) used to pin here forever with nothing in flight.
    if (document.hidden) {
      markStreamGap();
      hiddenDisconnect = true;
      setSseStatus("paused — tab hidden", "");
      return;
    }
    setSseStatus("connecting…", "");
    evtSource = new EventSource(url, { withCredentials: true });
    evtSource.onopen = function () {
      reconnectAttempts = 0;
      // Measure the gap this open just closed, then clear it (disconnectedAt is
      // the was-gap flag).  A gap with no start stamp (legacy scheduleReconnect
      // path) reads as Infinity — unknown length means we can't argue the
      // replay covered it, so refresh below.
      const gapMs = disconnectedAt ? Date.now() - disconnectedAt : Infinity;
      disconnectedAt = 0;
      gapRefreshedAtOpen = false;
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
      // Post-gap sidebar recovery.  child_ws_* / task-mutating events are
      // ordinary ring-buffer entries, so a cursor reconnect (replay_ok)
      // redelivers them and the normal handlers heal the sidebar — no REST
      // refetch, no replace-mode rebuild flicker on a momentary blur/focus.
      // Refresh eagerly only when the replay CANNOT vouch for the gap: no
      // cursor to resume from (the fresh path's synthetic replay carries no
      // child events), or a gap long enough that a stale cursor could be
      // lying (see GAP_REFRESH_THRESHOLD_MS).  The ring-evicted case
      // announces itself — the replay_truncated handler runs the same
      // refresh on arrival (gapRefreshedAtOpen keeps the two from stacking).
      //
      // While a truncation gap is ON RECORD, the gap machinery owns sidebar
      // recovery outright — the envelope refreshed at gap start and the
      // heal-time refresh covers everything since — so this arm stands
      // down.  Without that gate, the failed-resync retry loop re-fires
      // this refresh once per reconnect: loadHistoryThenReconnect nulls
      // lastEventId, a failed /history never re-seeds it, and every retry
      // reconnect then reads as "no cursor to resume from" — the onopen
      // half of the same un-jittered /children + /tasks stampede the
      // envelope's per-gap dedup stops.  (On the clean cursorless heal the
      // record is already cleared before the reconnect, so the one
      // intended heal refresh still lands here.)
      if (
        wasReconnecting &&
        truncatedFromCursor == null &&
        (lastEventId == null || gapMs > GAP_REFRESH_THRESHOLD_MS)
      ) {
        refreshSidebarAfterGap();
        gapRefreshedAtOpen = true;
      }
    };
    evtSource.onerror = function () {
      // Do NOT close evtSource for transient errors — native
      // EventSource auto-reconnect handles them with the
      // ``Last-Event-ID`` header automatically (now that the server
      // emits ``id:`` on every buffered event).  Closing here would
      // force a CONNECTING -> CLOSED transition that defeats native
      // reconnect, which is exactly the reconnect-with-replay defect
      // PR-D ships to fix.  See
      // tests/test_app_js.py::test_coord_connectsse_onerror_preserves_native_reconnect.
      markStreamGap();
      setSseStatus("disconnected", "err");
      // Dim the status bar so a stale reading doesn't read as live.
      statusBarEl.classList.add("ws-sb-disconnected");
      sbTokensEl.textContent = "Reconnecting…";
      // 401 probe: expired session is a terminal condition (user
      // must log in), so we DO close + showLogin in that branch.
      // Transient errors (network blips, intermediary timeouts) just
      // let native reconnect run — no scheduleReconnect needed
      // because the source isn't dead.
      // Raw fetch (not authFetch) — need to inspect status before throwing.
      // authFetch never RESOLVES with a 401 (it calls showLogin() itself and
      // throws Error("auth")), so probing through it made this branch dead
      // code: the close/cancel-timer handling below never ran and the
      // CLOSED-state recovery kept cycling scheduleReconnect behind the
      // login overlay — exactly the loop this branch exists to prevent.
      // Mirrors the app.js dashboard probe.  ``.catch``: a network-dead
      // probe is the transient case; native/manual reconnect owns it.
      //
      // The 401 body is inspected BEFORE the generic-expiry handling: a
      // code=version_mismatch body must take auth.js's upgrade path
      // (reload-after-re-login flag + "upgrade" overlay).  The old authFetch
      // probe did that as a side effect of authFetch's own 401 handling; a
      // raw fetch must do it explicitly or a server upgrade leaves stale
      // pre-upgrade JS running after sign-in.  NOTE the positive-form guard
      // (r.status === 401) directly above the close(): the reconnect-
      // contract pin (test_app_js._onerror_preserves_native_reconnect) keys
      // on that marker within a short window to allow a terminal close.
      fetch("/v1/api/workstreams/" + encodeURIComponent(wsId))
        .then(function (r) {
          if (!(r.status === 401 && typeof showLogin === "function")) return;
          return r
            .json()
            .catch(function () {
              return null;
            })
            .then(function (body) {
              try {
                if (evtSource) evtSource.close();
              } catch (_) {
                /* noop */
              }
              evtSource = null;
              // Cancel the pending CLOSED-state recovery timer (set
              // below).  Without this, 5 s later the timer would
              // observe ``!evtSource`` and call ``scheduleReconnect``,
              // which would open a new EventSource that gets 401 again
              // → infinite reconnect loop while the login overlay is
              // up.  The login flow re-arms ``connectSSE`` after a
              // successful sign-in via its own callback path.
              if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
              }
              if (
                body &&
                body.code === "version_mismatch" &&
                typeof noteVersionMismatch === "function"
              ) {
                noteVersionMismatch();
              } else {
                showLogin("Session expired. Please sign in to reconnect.");
              }
            });
        })
        .catch(function () {
          /* transient network failure — reconnect machinery handles it */
        });
      // CLOSED-state recovery: native auto-reconnect covers the
      // transient case (source stays in CONNECTING and eventually
      // re-opens).  But if the browser gives up — hard 4xx after
      // retries, intermediary tearing the connection down with
      // prejudice, etc. — the source transitions to CLOSED and
      // there is no further native recovery.  Schedule a delayed
      // check that calls scheduleReconnect if the source is still
      // CLOSED at that point; scheduleReconnect's exp-backoff +
      // jitter then opens a new EventSource (threading the saved
      // lastEventId via the URL query param, so replay still
      // works across the manual reconnect).  Cancel/replace the
      // existing timer so successive onerror fires don't pile up
      // multiple checks for the same source.  The 401 branch above
      // ALSO cancels this timer when it fires — see comment there.
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        if (!evtSource || evtSource.readyState === EventSource.CLOSED) {
          scheduleReconnect();
        }
      }, 5000);
    };
    evtSource.onmessage = function (event) {
      // Capture lastEventId BEFORE JSON.parse so a malformed event
      // doesn't desync the manual-reconnect fallback from native
      // auto-reconnect.
      // ``lastEventId`` lives on the MESSAGE EVENT — EventSource exposes
      // no such property, so the pre-2026-07 read off the source OBJECT
      // was a dead conditional: the cursor never tracked live traffic
      // and the counter-reset detector below never fired in a real
      // browser.  ``!== ""``: no-id frames carry "" per spec.  (A
      // DOMString — the valid id "0" is truthy, so truthiness would
      // behave identically; the explicit form matches interactive.js's
      // canonical capture.)
      if (event.lastEventId != null && event.lastEventId !== "") {
        // A live event id BELOW our saved cursor means the server's per-ws
        // event counter reset — a coordinator process restart with a fresh,
        // empty ring.  The replay path can't flag that (a cursor at/above the
        // ring's earliest id reports replay_ok even when it's past the new
        // max), so the gap is silent and the sidebar's pre-restart rows go
        // stale with nothing to replay them.  Catch it here and pull
        // authoritative state — deduped per open against onopen's /
        // replay_truncated's own refresh.  (Gaps that DON'T reset the counter
        // are handled at onopen by the cursor-trust window.)
        if (
          lastEventId != null &&
          !gapRefreshedAtOpen &&
          Number(event.lastEventId) < Number(lastEventId)
        ) {
          refreshSidebarAfterGap();
          gapRefreshedAtOpen = true;
        }
        lastEventId = event.lastEventId;
      }
      let data = null;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        streamHealth.malformedFrames += 1;
        console.warn(
          "coordinator: dropping malformed SSE frame (total " +
            streamHealth.malformedFrames +
            ")",
          err,
        );
        return;
      }
      // Tag the event with its own SSE id so the system_turn handler can dedup
      // a turn already painted from /history (mirrors ui/static/app.js).
      if (event.lastEventId) data._event_id = event.lastEventId;
      // Guard the dispatch: an exception escaping onmessage does NOT close the
      // EventSource, so an unhandled throw here leaves the streaming refs stale
      // and every later turn paints into the poisoned segment — the "output
      // stops while the backend is healthy" wedge.  Count it (render-throw
      // class) so a field report tells it apart from a dropped-events gap.
      try {
        handleEvent(data);
      } catch (err) {
        streamHealth.renderThrows += 1;
        console.error(
          "coordinator: handleEvent failed for " +
            (data && data.type) +
            " (render-throw total " +
            streamHealth.renderThrows +
            ")",
          err,
        );
      }
    };
  }

  function scheduleReconnect() {
    const base = Math.min(30000, 1000 * Math.pow(2, reconnectAttempts));
    const jitter = Math.floor(Math.random() * 500);
    reconnectAttempts += 1;
    reconnectTimer = setTimeout(connectSSE, base + jitter);
  }

  // ------------------------------------------------------------------
  // SSE overflow recovery (client half) — mirrors interactive.js.  The
  // trip threshold + cooldown-ladder math is shared via sse_overflow.js;
  // the stateful glue below is coupled to this closure's evtSource seam.
  // ------------------------------------------------------------------

  // Transport-only stream suspension for the overflow / visibility /
  // close-session / truncated-resync paths: the closeStreamTransport
  // teardown WITHOUT the full destroy() cleanup (observers, task timers),
  // plus gap accounting.  connectSSE re-opens from the saved lastEventId
  // (or the recorded truncation cursor), so this is lossless for committed
  // turns.
  function suspendStream() {
    closeStreamTransport();
    // A deliberate suspend is still a gap.  The transient-error path marks
    // it via onerror; the overflow/hide/close-session/resync paths
    // self-close (no onerror fires after .close()), so mark it here instead.
    markStreamGap();
  }

  // Open a gap in the stream's coverage: stamp its wall-clock start, which
  // doubles as the was-reconnecting flag the next onopen snapshots (see
  // connectSSE).  Keep the EARLIEST stamp when marks pile up (repeated onerror
  // fires, hide followed by a deferred connect) so onopen measures the whole
  // outage, not just its last slice.  onopen clears it.
  function markStreamGap() {
    if (!disconnectedAt) disconnectedAt = Date.now();
  }

  // Replace-mode sidebar re-sync after a gap the reconnect replay could not
  // (or might not) have covered — the server is authoritative; any SSE-only
  // child/task rows accumulated before the disconnect are stale.  Four
  // callers: onopen (no-cursor / over-threshold gaps, standing down while a
  // truncation gap is on record), the onmessage counter-reset detector
  // (live id below the saved cursor = process restart), the
  // replay_truncated handler (NEW ring-evicted gaps — deduped per gap), and
  // loadHistoryThenReconnect's heal-time refresh (retry-window staleness on
  // a cursor-seeded heal).  Ordinary short gaps need none of them — the
  // ring replay redelivers child_ws_* / task events itself.
  function refreshSidebarAfterGap() {
    loadChildren({ replace: true });
    loadTasks();
    // Drop the live-badge cache too — entries within the 5s TTL can carry
    // stale pending_approval_details (the child may have resolved its
    // approval during the SSE gap).  Without this clear, inline approve/deny
    // buttons could render on a row whose approval was resolved elsewhere;
    // the next scheduleLiveFetch from loadChildren's finally branch (which
    // fires for every visible row) repopulates with authoritative state.
    // Preserve `permanent: true` entries (set on 403/404 — denied by
    // permission/identity, not by state) so a user lacking
    // admin.cluster.inspect doesn't pay one 403 per denied id on every
    // refresh.
    for (const [id, c] of liveBadgeCache) {
      if (!c || !c.permanent) _liveBadgeCacheDelete(id);
    }
  }

  // Count + log a caught render throw (wedge-class instrumentation).  The
  // running total rides in the log line so a field report shows which class
  // fired — dropped events vs render wedge — without a debugger attached.
  // console.warn, not error: every caller recovers (plain-text fallback or
  // keeping the already-streamed text).  The onmessage dispatch catch keeps
  // its own inline increment — that one is console.error (the whole event is
  // dropped, nothing recovers it) and names the event type.
  function noteRenderThrow(where, err) {
    streamHealth.renderThrows += 1;
    console.warn(
      "coordinator " +
        where +
        " failed (render-throw total " +
        streamHealth.renderThrows +
        ")",
      err,
    );
  }

  // The ONE rolling-window churn accounting step for the degraded ladder —
  // stream_overflow closes (noteStreamOverflow) AND truncated resyncs
  // (noteTruncatedResync) land here, so the trip parameters and the
  // degraded-entry call have a single implementation that cannot silently
  // diverge.  The cooldown-ladder reset lives in enterDegradedCatchup
  // (keyed off lastDegradedAt), NOT here — this only counts and trips.
  // Returns true when this entry ENTERED degraded catch-up.
  function recordChurnAndMaybeTrip() {
    const now = Date.now();
    overflowTimes.push(now);
    if (
      overflowWindowTripped(
        overflowTimes,
        now,
        OVERFLOW_TRIP_COUNT,
        OVERFLOW_TRIP_WINDOW_MS,
      )
    ) {
      enterDegradedCatchup();
      return true;
    }
    return false;
  }

  function noteStreamOverflow() {
    streamHealth.overflows += 1;
    console.warn(
      "coordinator: server closed the stream after a send-queue overflow " +
        "(total " +
        streamHealth.overflows +
        "); reconnect will replay the gap",
    );
    recordChurnAndMaybeTrip();
  }

  // The one increment+log step for the replay-window class — EVERY
  // truncatedGaps bump goes through here so the console invariant documented
  // at the streamHealth initializer holds (each increment carries the running
  // count).  Class-of-event wording only: the recovery that follows differs
  // by caller (jittered fresh-connect, idle-edge fresh-connect, or a
  // degraded-catchup skip), so the line must not assert an action a trip may
  // skip.  Churn accounting is deliberately NOT here — the idle-edge caller
  // records the gap without feeding the degraded ladder.
  function recordTruncatedGap() {
    streamHealth.truncatedGaps += 1;
    console.warn(
      "coordinator: replay window could not cover the reconnect gap " +
        "(total " +
        streamHealth.truncatedGaps +
        ")",
    );
  }

  // Count a truncated-triggered full resync into the SAME rolling churn
  // window as overflow closes (overflowTimes): both are "this consumer
  // needed heavyweight recovery", and the degraded ladder is the bound for
  // either loop.  Returns true when this trip ENTERED degraded catch-up —
  // the caller must then SKIP starting its resync: the cooldown ladder just
  // disconnected the stream, and a loadHistoryThenReconnect started now
  // would reconnect from its .finally and defeat the cooldown it triggered.
  // The wake-up reconnect re-arrives here via a fresh replay_truncated (the
  // chokepoint re-presents the still-stale truncatedFromCursor), so the
  // resync is deferred, not lost.
  function noteTruncatedResync() {
    recordTruncatedGap();
    return recordChurnAndMaybeTrip();
  }

  // Start the truncated-triggered fresh connect after a random
  // 0..TRUNCATED_RESYNC_JITTER_MS spread.  The truncated envelope is
  // herd-shaped — a node restart makes every stale-cursor tab reconnect
  // inside the EventSource retry jitter and each answers with a /history
  // fetch — and the per-tab churn limiter cannot see a cross-tab herd (one
  // resync per tab never trips it), so the spread is applied here, before
  // the fetch.  During the wait the pane keeps painting live events from
  // the still-open stream; the gap predates the resync either way, so the
  // only cost is a delayed backfill.  closeStreamTransport owns
  // cancellation (redial, hide, degraded entry, close-session, destroy —
  // see the field comment on truncatedResyncTimer).
  function scheduleTruncatedResync() {
    if (truncatedResyncTimer != null) return; // one pending resync at a time
    truncatedResyncTimer = setTimeout(() => {
      // Null the handle BEFORE loading — loadHistoryThenReconnect tears the
      // transport down (closeStreamTransport cancels this timer), and a
      // still-set handle would cancel the work it is part of.
      truncatedResyncTimer = null;
      loadHistoryThenReconnect();
    }, Math.random() * TRUNCATED_RESYNC_JITTER_MS);
  }

  // The dead-stream truncated resync (#882): mirror of interactive.js's
  // _loadHistoryThenConnect, minus the ws-switch machinery — this pane is
  // single-wsId for life, so there is no load token and no cross-ws
  // truncated-cursor drop (see the clear_ui handler's ruling on the
  // same-ws render race).  The reference's lastEventId reset is KEPT —
  // not as ws-switch machinery but for reborn-ring convergence (see the
  // note in the body; deleting it reintroduces the envelope→resync loop
  // the coord-restart e2e scenario exists to catch).  Order matters:
  //   1. suspendStream — tear the transport down FIRST so no live events
  //      paint into the pane mid-rebuild, and mark the gap so a slow
  //      /history fetch (>60s) re-enters onopen's cursor-trust-window
  //      refresh on reconnect.
  //   2. Null the streaming refs + buffers: refetchHistory replaceChildren()s
  //      the DOM but does NOT null them, and the envelope's own synthetic
  //      in_progress_snapshot may have re-created a live bubble between
  //      scheduling and this fire — a stale ref would strand the rebuilt
  //      turn's tokens into a detached node, and stale buffers would make
  //      the reconnect's snapshot length-guards skip the repaint.  Safe
  //      here because the transport is already down (no delta can race the
  //      null).
  //   3. refetchHistory(true): full render + adopt hist.cursor (/history
  //      trims the trailing in-flight turn whenever it returns a cursor,
  //      expecting the caller to replay from it — adoption is the #882 fix
  //      core).  A successful render also clears truncatedFromCursor.
  //   4. Reconnect in .finally — runs on success, failure, AND a render
  //      throw.  The failed-fetch retry rides the connect chokepoint, not a
  //      callback: a failure leaves truncatedFromCursor set (only a
  //      successful render clears it), so this reconnect presents the
  //      truncation-time cursor, draws replay_truncated again, and the
  //      resync retries — bounded by the churn limiter + degraded ladder.
  //      The old transcript survives a failed fetch (the wipe sits below
  //      refetchHistory's !hist guard), so the retry window shows
  //      stale-but-real content, not an empty pane.
  //      ``if (visHandler)``: destroy() and coordCloseSession release the
  //      stream lifecycle by removing the visibility handler — the same
  //      sentinel that keeps a show edge from resurrecting a deliberately
  //      closed stream keeps this async tail from reopening one on a
  //      destroyed pane (close-session's own resumeSse re-arms on its
  //      failure paths).
  function loadHistoryThenReconnect() {
    suspendStream();
    currentAssistantEl = null;
    currentAssistantBuf = "";
    currentReasoningEl = null;
    currentReasoningBuf = "";
    // Drop the live cursor for the reconnect: the full render below
    // supersedes it (refetchHistory re-seeds from hist.cursor when the
    // server hands one back).  This is NOT just interactive parity — it is
    // what makes the resync CONVERGE against a reborn ring: after a node
    // restart, a successful /history on an idle ws returns NO cursor, and
    // without this null the reconnect re-presents the frozen pre-restart
    // cursor against the empty reseeded ring, which honestly answers
    // replay_truncated — again and again, an envelope→resync→envelope loop
    // that trips the churn ladder and parks the pane in degraded cooldown
    // cycles forever (an idle ws emits no frames to ever advance the
    // cursor).  Caught by scripts/recovery_e2e.py --scenario coord-restart;
    // invisible to source-pattern tests.  On the FAILED-fetch leg the
    // reconnect still retries correctly: truncatedFromCursor (not
    // lastEventId) is the durable repair state the chokepoint presents.
    lastEventId = null;
    // A pending deferred resync is superseded by this full refetch — without
    // this clear, the next idle edge would run a second, pointless full
    // resync.
    pendingTruncatedResync = false;
    refetchHistory(true)
      .then(() => {
        // Successful heal only (a failed fetch resolves too, but leaves the
        // record set; a render throw skips .then entirely): refresh the
        // sidebar once.  The envelope refreshed it at gap START; this
        // covers child_ws_* / task events that landed in the retry loop's
        // suspend→fetch→reconnect windows, which no SSE replay can
        // redeliver (each retry reconnect presents the stale record
        // cursor) — the sidebar heals here exactly the way the transcript
        // heals via /history.  Gated to the cursor-SEEDED heal
        // (lastEventId != null): the cursorless heal reconnects fresh and
        // onopen's own no-cursor arm runs this same refresh — skipping
        // here keeps it to one refresh per heal on both shapes.
        // Slow-heal exclusivity: when the fetch itself outlived the
        // cursor-trust window, onopen's over-threshold arm WILL refresh —
        // measured off the same disconnectedAt read here — so stand down.
        // The implication holds up to the connect handshake: a fetch
        // landing within one handshake of the threshold measures under it
        // here and over it at onopen and double-fires (accepted:
        // replace-mode loads are last-writer-wins, and closing the
        // handshake-wide window at a 60s boundary would take a heal-scoped
        // flag that survives onopen's per-connection reset — more state
        // than the dedup is worth).  A heal followed by a long hidden
        // deferral before the reconnect legitimately gets both: the hide
        // opened a NEW coverage gap after this one healed.
        //
        // RULED (2026-07-21, #882 review): on a ZERO-retry seeded heal
        // this refresh duplicates what the seeded replay_ok reconnect
        // redelivers from the ring anyway.  Accepted: telling K=0 from
        // K>=1 needs a per-episode attempt counter whose reset lifecycle
        // spans schedule/trip/latch/failure paths, and ~3 jitter-spread
        // REST per healed gap is cheaper than that state; the deeper fix
        // for restart-herd sidebar load is server-side coalescing (#884).
        // visHandler: same destroyed/close-session sentinel as the
        // reconnect below.
        const onopenWillRefresh =
          disconnectedAt !== 0 &&
          Date.now() - disconnectedAt > GAP_REFRESH_THRESHOLD_MS;
        if (
          visHandler &&
          truncatedFromCursor == null &&
          lastEventId != null &&
          !onopenWillRefresh
        ) {
          refreshSidebarAfterGap();
        }
      })
      .finally(() => {
        if (visHandler) connectSSE();
      });
  }

  function enterDegradedCatchup() {
    // Repeated overflow closes inside one window: this consumer cannot keep up
    // with live streaming right now, and each reconnect round just stalls
    // rendering behind the retry before re-saturating.  Stop the churn: close
    // the stream, say so in plain language, and come back after a (doubling)
    // cooldown — that reconnect replays the gap from the server's ring buffer,
    // or falls to the replay_truncated → /history resync floor once the gap
    // has outgrown it.  Either path is lossless for committed turns.
    const now = Date.now();
    // Escalate the cooldown when trips recur; reset to base only after a
    // genuine quiet gap.  Keyed off lastDegradedAt (a timestamp), NOT
    // overflowTimes — this clears that array below, so keying the reset off it
    // would restart the ladder on the next storm's first overflow and the
    // doubling (15→30→60→120s) would never take effect.
    const step = degradedCooldownStep(
      degradedCooldownMs,
      lastDegradedAt,
      now,
      DEGRADED_COOLDOWN_BASE_MS,
      DEGRADED_COOLDOWN_MAX_MS,
      DEGRADED_COOLDOWN_RESET_MS,
    );
    lastDegradedAt = now;
    degradedCooldownMs = step.nextCooldownMs;
    overflowTimes.length = 0;
    suspendStream(); // also cancels any earlier degraded timer
    setSseStatus("catching up…", "err");
    statusBarEl.classList.add("ws-sb-disconnected");
    sbTokensEl.textContent = "Connection is slow — catching up…";
    const cooldown = step.cooldown;
    degradedTimer = setTimeout(function () {
      degradedTimer = null;
      if (document.hidden) {
        // Reopening into a throttled hidden tab would overflow again — defer to
        // the visibilitychange show edge instead.
        hiddenDisconnect = true;
        return;
      }
      connectSSE();
    }, cooldown);
  }

  function onVisibilityChange() {
    if (document.hidden) {
      // Closing beats letting the hidden tab's throttled event loop starve the
      // drain until the server-side queue overflows.  The streaming buffers
      // (currentAssistantBuf / currentAssistantEl) survive — suspendStream is
      // transport-only — so the visible tail is intact when the tab returns.
      if (evtSource) {
        suspendStream();
        hiddenDisconnect = true;
      }
    } else if (hiddenDisconnect) {
      hiddenDisconnect = false;
      connectSSE();
    }
  }

  function removeVisibilityHandler() {
    if (visHandler) {
      document.removeEventListener("visibilitychange", visHandler);
      visHandler = null;
    }
    hiddenDisconnect = false;
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
      case "in_progress_snapshot":
        // One-shot replay of the in-progress turn's reasoning + content
        // when this client connects mid-stream (page refresh while the
        // model is generating).  Idempotent on EventSource auto-reconnect:
        // skip overwrite when the current buffer is already at-or-past
        // the snapshot length, so a stale replay can't reset the live-
        // streamed view back to a shorter prefix.
        if (ev.reasoning && ev.reasoning.length > currentReasoningBuf.length) {
          if (!currentReasoningEl) {
            currentReasoningEl = appendMsg("reasoning", "", {
              label: "reasoning",
            });
            messagesEl.setAttribute("aria-live", "off");
          }
          currentReasoningBuf = ev.reasoning;
          var rbody = currentReasoningEl.querySelector(".msg-body");
          if (rbody) rbody.textContent = currentReasoningBuf;
          _scheduleScroll();
        }
        if (ev.content && ev.content.length > currentAssistantBuf.length) {
          if (!currentAssistantEl) {
            currentAssistantEl = appendMsg("assistant", "", {
              label: "assistant",
            });
            messagesEl.setAttribute("aria-live", "off");
          }
          currentAssistantBuf = ev.content;
          var abody = currentAssistantEl.querySelector(".msg-body");
          if (abody && typeof streamingRender === "function") {
            try {
              streamingRender(abody, currentAssistantBuf);
            } catch (e) {
              noteRenderThrow("in_progress_snapshot render", e);
              abody.textContent = currentAssistantBuf;
            }
          } else if (abody) {
            abody.textContent = currentAssistantBuf;
          }
          _scheduleScroll();
        }
        break;
      case "stream_end":
        // A live in-progress compaction card here means a FORCE stop
        // abandoned the compaction worker (the lifecycle wrapper otherwise
        // always retires the card with an end event before any stream_end
        // can follow) — remove it instead of leaving a frozen bar.
        resetCompactionHolder(compactionHolder);
        finishAssistantStream();
        break;
      case "stream_overflow":
        // The server poisoned this listener at its first queue overflow and
        // closes the stream right after this id-less frame.  lastEventId still
        // points below the gap, so native EventSource reconnect replays it
        // losslessly from the ring buffer.  Count the close: a persistently
        // slow consumer trips the degraded catch-up instead of churning
        // reconnects.
        noteStreamOverflow();
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
        // every live cycle's card into every new SSE subscriber, so
        // reconnect won't double-render the construct.
        showApproval(ev.items, !!ev.judge_pending, ev.cycle_id || "");
        break;
      case "approval_resolved": {
        // Server-driven resolution.  Route to the batch whose rows
        // carry one of the resolved call_ids — several batches can be
        // pending at once (parallel task agents), and morphing "the
        // active" one would resolve the wrong construct.  Server now
        // echoes ``always`` on the SSE payload (post-PR-447) so
        // cross-tab resolution renders the right status pill on every
        // subscribed tab — not just the one that clicked.  Fall back
        // to this tab's stashed dataset flag for backward compat with
        // a server hot-deploy where the SSE event might briefly omit
        // the field.  Legacy events without call_ids fall back to
        // activeBatch / the last pending batch (single-cycle server).
        let target = null;
        const resolvedIds = Array.isArray(ev.call_ids) ? ev.call_ids : [];
        for (const cid of resolvedIds) {
          const mapped = toolRows.get(cid);
          if (mapped && mapped.batch) {
            target = mapped.batch;
            break;
          }
        }
        if (!target) {
          target =
            activeBatch ||
            messagesEl.querySelector(".conv-batch.conv-batch--pending");
        }
        if (target) {
          const wasAlways =
            ev.always === true ||
            (ev.always === undefined && target.dataset.requestedAlways === "1");
          const approved = ev.approved !== false;
          _morphBatchResolved(target, {
            approved,
            always: wasAlways,
            feedback: ev.feedback || null,
          });
          // Approved batches start running the moment the user clicks
          // approve — mirror the auto path so the live RUNNING
          // indicator shows during execution (not just on refresh).
          // Denied batches don't run at all, so no --running.
          if (approved) {
            _setBatchRunning(target);
          }
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
            if (entry.batch.classList.contains("conv-batch--pending")) {
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
        // Anchor the finding to the specific .conv-row that
        // tripped the guard so the operator reads call → finding
        // adjacency on both live and replay surfaces.  Falls back
        // to a chat line only when the call_id no longer maps to a
        // row (e.g. event arrived after the row was evicted).
        if (ev.call_id && toolRows.has(ev.call_id)) {
          _attachOutputWarningChip(toolRows.get(ev.call_id).row, {
            risk_level: ev.risk_level,
            flags: ev.flags,
            redacted: ev.redacted,
            tier: ev.tier,
            judge_risk: ev.judge_risk,
            confidence: ev.confidence,
            reasoning: ev.reasoning,
            judge_model: ev.judge_model,
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
      case "system_turn": {
        // First-class operator-context system turn (output-guard finding,
        // user interjection, metacognitive nudge, watch result — see
        // make_system_turn).  Rendered in trajectory sequence (it FOLLOWS the
        // turn it advises).  ``renderSystemTurn`` routes by ``ev.source`` to the
        // structured card (watch / guard / idle-children) or the operator bubble
        // (carrying ``ev.meta`` so cards rebuild identically live and on replay).
        // Dedup: skip a turn already painted from /history (matched by id) and
        // redelivered by an SSE replay past the resume cursor.  With the
        // row/event id-alignment fix this shouldn't recur, but keeps the
        // /history+replay seam idempotent.  Mirrors ui/static/app.js.
        const sysEid = ev._event_id != null ? String(ev._event_id) : null;
        if (sysEid && renderedSystemEventIds.has(sysEid)) break;
        renderSystemTurn(ev.source || "", ev.content || "", ev.meta);
        if (sysEid) renderedSystemEventIds.add(sysEid);
        break;
      }
      case "compaction":
        // Context-compaction lifecycle — the shared reducer
        // (conversation.applyCompactionEvent) is the one state machine for
        // this viewer and the interactive pane, so the two can't drift.
        // reason="error" ends render via the paired `error` event (red row);
        // the reducer emits only the non-error failure notices here.
        applyCompactionEvent(compactionHolder, ev, {
          container: messagesEl,
          renderedIds: renderedSystemEventIds,
          onNotice: (msg) => appendText("info", msg, { label: "info" }),
          scroll: () => _scheduleScroll(),
        });
        break;
      case "connected":
        // First yield from _coord_events_replay — populates the
        // status bar's model cell before any history arrives.  Also
        // re-fires on every SSE reconnect because the replay phase
        // runs unconditionally on subscribe.
        coordModel = ev.model || "";
        coordModelAlias = ev.model_alias || ev.model || "";
        paintCoordModelChip();
        coordProjectName = ev.project_name || "";
        paintCoordProjectChip();
        break;
      case "status":
        // Live token / context / tool / turn counters.  Replayed once
        // on reconnect when last_usage is available, then ticked by
        // SessionUI.on_status on every turn.
        updateStatusBar(ev);
        break;
      case "state_change":
        if (statusEl) statusEl.textContent = ev.state || "";
        // Track who holds the in-flight turn so the send gate can compare
        // against this viewer; cleared when the turn settles. Present on busy
        // transitions from a shared coordinator, absent otherwise (gate then
        // never engages).
        if (ev.state === "idle" || ev.state === "error") {
          actingUserId = null;
        } else if (ev.acting_user_id) {
          actingUserId = ev.acting_user_id;
        }
        // Drive the composer's busy state from the canonical
        // server-side workstream state so the Stop button + queue
        // mode follow whatever the worker is doing — including
        // transitions we didn't initiate (cross-tab cancel, judge
        // reset, idle-after-error). Mirrors the interactive pane.
        if (ev.state === "idle" || ev.state === "error") {
          setBusy(false);
          // Deferred replay_truncated re-sync: the truncation arrived while a
          // turn was mid-stream (refetching then would have detached the live
          // bubble), so repair the ring-evicted gap now that the turn is
          // settled and /history is complete.  Also repairs a turn stranded by
          // close-on-hide — hidden mid-turn, its stream_end evicted, so the
          // show-edge replay_truncated latched the flag and the live bubble
          // never finalized.  Same dead-stream flow as the immediate branch
          // (full fresh connect, cursor adoption; the streaming-ref reset
          // lives inside loadHistoryThenReconnect) — but NOT counted into
          // the degraded-churn window, and NOT herd-jittered: this branch
          // fires once per latch, and the latch only re-arms via a NEW
          // truncated envelope followed by a full turn-settle — inherently
          // rate-limited AND per-pane staggered by turn timing, unlike the
          // immediate branch's reconnect loop (which the limiter bounds and
          // scheduleTruncatedResync spreads).  Usually /history returns no
          // cursor here (no orphan at idle) and the reconnect is a plain
          // fresh connect.  Mirrors interactive.js.
          //
          // RULED (2026-07-21, #882 review): the turn-timing stagger does
          // NOT cover the restart herd — N mid-turn tabs all latch, then
          // all consume when the reconnect's synthetic idle replay lands
          // inside the same retry window — but this branch stays
          // un-jittered anyway: it is byte-for-byte the converged
          // interactive.js shape, a coordinator-only spread would fork the
          // shared design for a one-shot-per-latch peak (identical total
          // volume), and the tracked fix for restart-herd /history load is
          // server-side coalescing (#884).  If that herd ever measures
          // hot, sweep BOTH clients' idle-edge consumers together — do not
          // patch just this one.
          if (pendingTruncatedResync) {
            pendingTruncatedResync = false;
            recordTruncatedGap();
            loadHistoryThenReconnect();
          }
        } else if (
          ev.state === "running" ||
          ev.state === "thinking" ||
          ev.state === "attention"
        ) {
          setBusy(true);
        }
        break;
      case "rename":
        if (nameEl) nameEl.textContent = ev.name || "";
        break;
      case "message_queued":
        // Server confirms the queued slot — the optimistic bubble
        // already showed it; nothing to render here. (Earlier this
        // surfaced an extra info row, which doubled up with the
        // queued bubble once the composer started rendering one.)
        break;
      case "message_dispatched":
        // A deferred send left the parked list: fresh spawn (promote the
        // chip — the ×'s window is over) or interjection fold-in
        // (folded: true — only the deferred flag clears; the chip resumes
        // the normal queued lifecycle). No-op when this tab holds no
        // matching chip.
        queue.settleDeferred(ev.msg_id, !!ev.folded);
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
      case "clear_ui": {
        // Conversation was structurally reset (rewind / retry). Re-render
        // from REST, then dispatch any queued edit-and-resend once the (now
        // truncated) history lands. Coord is single-wsId per page, so no
        // load-token guard is needed (unlike the interactive pane): a
        // cross-ws misroute cannot happen, and the resend below always
        // belongs to this pane.  The remaining same-ws exposure — this
        // seedless live re-render racing a truncated resync's seeded one —
        // is RULED accepted without a token: both renders are
        // server-authoritative snapshots milliseconds apart, last-writer
        // wins, and refetchHistory's success-path supersession (record +
        // deferred latch + pending timer) eliminates the dominant vector (a
        // resync still armed when this rebuild lands); the residual
        // fetch-overlap window is transient-cosmetic and heals on the next
        // full render or chokepoint retry.
        refetchHistory()
          .then(() => {
            // Repair-intent supersession (pending resync timer + deferred
            // latch + gap record) lives inside refetchHistory's success
            // path — a successful rebuild here clears all three, a FAILED
            // fetch (which also resolves) clears none, and a mid-render
            // throw skips this .then entirely — so the pending resync
            // stays the repair owner exactly when the pane still needs it.
            if (!_pendingEditSend) return;
            const editText = _pendingEditSend;
            _pendingEditSend = null;
            setBusy(true);
            appendUserMessageWithAttachments(editText, [], { label: "you" });
            authFetch(
              "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send",
              {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: editText }),
              },
            ).catch((err) => {
              appendText("error", "Connection error: " + err.message);
              setBusy(false);
            });
          })
          .catch((err) => {
            // Render runs outside refetchHistory's fetch try/catch by design;
            // if it throws, don't strand the queued edit — clear latch + busy.
            _pendingEditSend = null;
            setBusy(false);
            appendText("error", "Failed to reload history: " + err.message);
          });
        break;
      }
      case "replay_truncated": {
        // The stream just admitted losing events past recovery — treat the
        // connection as DEAD and run the full fresh-connect flow
        // (loadHistoryThenReconnect: disconnect first, REST /history,
        // rebuild, adopt the returned resume cursor, reconnect).  The cursor
        // adoption is the point (#882 — ruled with interactive.js, do not
        // revert to an in-place refetch): /history TRIMS the trailing
        // in-flight turn whenever it hands back a cursor
        // (_resume_cursor_and_trim), on the assumption the caller replays
        // from that cursor.  The old in-place refetch here discarded the
        // cursor, so a mid-run truncation wiped the executing turn's rows
        // with no redelivery — later tool results then orphaned into
        // standalone top-level bubbles.  Disconnect-first also removes the
        // flush-vs-reconnect ambiguity: no live events arrive during the
        // rebuild, so nothing double-renders.  The rehydrate-triggered
        // truncation (empty reborn ring) rides the same flow — a rehydrated
        // ws has no in-flight orphan to trim, so /history returns no cursor
        // and the reconnect is a plain fresh connect (the load drops the
        // stale cursor — see loadHistoryThenReconnect's reborn-ring note).
        //
        // Skip while a turn is mid-stream (BOTH a content bubble and a
        // reasoning-only one are detachable): an async refetch's
        // replaceChildren() would detach the live bubble so deltas render
        // nowhere.  Mid-stream the resync is DEFERRED, not dropped —
        // skipping outright left the ring-evicted gap unrepaired for the
        // rest of the session; the idle edge consumes the flag.
        //
        // Record WHERE the gap sits before either branch: the envelope
        // arrived at the connect cursor, but by the time the resync's
        // /history fetch runs (after the jitter, or at the idle edge) the
        // live stream has advanced lastEventId well past it — the
        // failed-fetch retry needs this truncation-time value, not the
        // advanced one (see truncatedFromCursor's field comment).
        // Keep-oldest: a retry reconnect re-delivers an envelope for the
        // SAME unrepaired gap and must not advance the record.  Whether
        // this envelope OPENED the gap (vs. re-announcing one already on
        // record) also drives the sidebar-refresh dedup below.
        const isNewTruncationGap = truncatedFromCursor == null;
        if (isNewTruncationGap) {
          truncatedFromCursor = lastEventId;
        }
        if (!currentAssistantEl && !currentReasoningEl) {
          // Limiter check BEFORE scheduling the resync — a trip has just
          // disconnected the stream for a cooldown, and a resync started
          // now would reconnect from its .finally and defeat the cooldown
          // (see noteTruncatedResync).  No trip → the fresh connect starts
          // after a herd-spreading jitter (scheduleTruncatedResync).
          if (!noteTruncatedResync()) {
            scheduleTruncatedResync();
          }
        } else {
          pendingTruncatedResync = true;
        }
        // The evicted slice may have carried child_ws_* / task events the
        // sidebar will never see replayed — this is the server saying the
        // gap was NOT covered, so pull authoritative state.  Deduped two
        // ways: per open against onopen's refresh (gapRefreshedAtOpen —
        // milliseconds apart on the same reconnect), and per GAP against
        // the failed-resync retry loop — each retry reconnect re-draws the
        // envelope for the SAME unrepaired gap (keep-oldest record still
        // set), and re-refreshing then would hit /children + /tasks + the
        // live-badge bulk fetch un-jittered once per retry, across every
        // open tab of a console that is already struggling (only /history
        // rides the herd spread).  Sidebar staleness accrued DURING the
        // retry loop's teardown windows is covered by the heal-time
        // refresh in loadHistoryThenReconnect.
        if (isNewTruncationGap && !gapRefreshedAtOpen) refreshSidebarAfterGap();
        break;
      }
      case "tool_pending":
        // Early paint — render the batch the instant the model commits to
        // a tool call, BEFORE the intent judge / Smart Approvals verdict and
        // the approval gate resolve, so the operator can Stop in an emergency
        // without waiting on the judge.  appendToolBatch is idempotent on
        // call_ids: the authoritative approve_request (→ --pending) or
        // tool_info (→ --auto) that follows morphs THIS construct in place.
        // Reuses the --running placeholder (subtle accent rail, no actions)
        // the replay path already knows how to upgrade; the ``announce`` flag
        // only swaps the kicker to "Evaluating" while the judge runs.
        appendToolBatch(ev.items || [], {
          announce: true,
          running: true,
          judgePending: true,
        });
        // Polite SR announcement (the messages log is flipped to
        // aria-live="off" during the streaming that precedes this, so the
        // appended batch alone wouldn't be heard).
        _announcePolite(_toolAnnounceText(ev.items || []));
        break;
      case "tool_info":
        // Renamed from ``tools_auto_approved`` when ``approve_tools``
        // unified onto SessionUIBase — the shared body emits ``tool_info``
        // for both kinds, matching the interactive payload name.  All
        // items in a single ``tool_info`` envelope share a dispatch
        // turn, so render them as one batch construct (parallel when
        // ≥2, solo otherwise) rather than N separate bubbles.  ``auto``
        // marks the approval-state class; ``running`` marks "in flight"
        // — the tool starts executing the moment auto-approval lands,
        // and the batch should show the same RUNNING indicator the
        // replay path renders for an unresolved committed turn.
        appendToolBatch(ev.items || [], { auto: true, running: true });
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
      a.dataset.wsId = safeWs;
      a.dataset.nodeId = safeNode;
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
      // One block per live cycle — a child running parallel task agents
      // can gate several batches at once, each independently resolvable.
      const details = _liveApprovalDetails(cached.live);
      if (details.length) {
        details.forEach((detail) => {
          const block = renderApprovalBlock(child, detail);
          if (block) row.appendChild(block);
        });
      } else {
        const block = renderApprovalPlaceholder(child);
        if (block) row.appendChild(block);
      }
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
  // Risk-level severity ranking moved to the shared conversation.js
  // (maxSeverityItem / riskRank, imported above) so the coordinator and
  // interactive panes can't drift on the fallback.  Unknown / malformed
  // risk_level ranks "medium" (step 5e.1b: this pane's old rank used "high").

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

  // Normalize the live block's approval payload to a list of cycle
  // details (``pending_approval_details``).  Defensive against a
  // malformed entry: non-arrays fold to [].
  function _liveApprovalDetails(live) {
    if (!live) return [];
    return Array.isArray(live.pending_approval_details)
      ? live.pending_approval_details.filter(Boolean)
      : [];
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
    const severityItem = maxSeverityItem(items);
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
      // so collapse the alias here. Unknown / unrecognized risk folds to
      // .med — the canonical unknown->medium (5e.1b decision); the
      // separate "(judge unavailable)" pill already covers no-verdict.
      const riskCls =
        risk === "crit" || risk === "critical"
          ? "crit"
          : risk === "high"
            ? "high"
            : risk === "medium" || risk === "med"
              ? "med"
              : risk === "low"
                ? "low"
                : "med";
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
  // clear the resolved cycle from pending_approval_details so the row re-renders without
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
        cycle_id: (detail && detail.cycle_id) || null,
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
        // Optimistically remove ONLY the resolved cycle — sibling
        // cycles (parallel task agents) keep their buttons.  The
        // pending flag clears only when no cycles remain.
        const remaining = _liveApprovalDetails(cached.live).filter(
          (d) => d !== detail && d.cycle_id !== (detail && detail.cycle_id),
        );
        cached.live = Object.assign({}, cached.live, {
          pending_approval: remaining.length > 0,
          pending_approval_details: remaining,
        });
        cached.sseUpdatedAt = Date.now();
        _liveBadgeCacheSet(targetWsId, cached);
      }
      // Also clear the activity_state mirror so the row's "⚑ approval"
      // badge in .meta disappears immediately too — without this, the
      // row shows the badge with no buttons for ~50-150ms until the
      // child_ws_approval_resolved push or next state event lands.
      // Only when NO cycles remain: a sibling prompt keeps the badge.
      const cachedAfter = liveBadgeCache.get(targetWsId);
      const anyLeft =
        cachedAfter &&
        cachedAfter.live &&
        _liveApprovalDetails(cachedAfter.live).length > 0;
      const childState = childrenState.get(targetWsId);
      if (childState && childState.activity_state === "approval" && !anyLeft) {
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
    _refreshChildrenCount();
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
      if (obs) {
        // Release the detached row from the persistent observer — this is
        // now the hot path (every child_ws_state tick), and observed-but-
        // detached rows are strong refs that would accumulate without bound
        // between full renders (which reset targets via disconnect()).
        obs.unobserve(row);
        obs.observe(replacement);
      }
      _restoreRowFocus(replacement, focusKey);
      // Keep the "(N · x pending)" annotation live on the targeted path —
      // approval edges arrive as state ticks now that child_ws_state no
      // longer takes the full render.
      _refreshChildrenCount();
    } else {
      renderChildren();
    }
  }

  function _refreshChildrenCount() {
    const total = childrenState.size;
    const pending = pendingApprovalIds.size;
    childrenCountEl.textContent = total
      ? "(" + total + (pending > 0 ? " · " + pending + " pending" : "") + ")"
      : "";
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
        const target = childrenTreeEl.querySelector(
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
            pending_approval_details: prev.live.pending_approval_details,
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
    // Terminal-bucket membership BEFORE the mutation: the tree sort keys on
    // it (non-terminal first), so a state tick that crosses the boundary
    // needs the full re-sorting render; everything else takes the targeted
    // single-row path below.
    const wasTerminal =
      existing.state === "closed" || existing.state === "deleted";
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
    // The ws-level activity signal is COARSE under parallel task
    // agents: one gate resolving (activity flips to "tool") while a
    // sibling is still parked would read as "no approval pending".
    // The per-cycle details list is the authoritative surface — a
    // non-empty list keeps the row pending regardless of the
    // activity flicker; per-cycle removal happens in
    // handleChildApprovalResolved, and the bulk fetch reconciles a
    // dropped resolution event within its ~2s TTL.
    const coarsePending =
      existing.state === "attention" || existing.activity_state === "approval";
    const cached = liveBadgeCache.get(childId);
    const cachedLive = (cached && cached.live) || {};
    const pendingApproval =
      coarsePending || _liveApprovalDetails(cachedLive).length > 0;
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
    // approval blocks). Setting ``pending_approval=true`` does NOT
    // claim cache authority — the bulk fetch is the source of
    // truth for ``pending_approval_details``, and bumping
    // sseUpdatedAt here makes the merge guard in flushLiveFetches
    // preserve our stale (often empty) details over the bulk
    // fetch's actual data, leaving the row stuck on the loading
    // placeholder. (Caught when the screenshot showed buttons
    // briefly then loading replaced them.)
    const detailClearedAuthoritatively =
      !pendingApproval && cachedLive.pending_approval === true;
    if (!pendingApproval) {
      nextLive.pending_approval_details = [];
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
    // child_ws_state is the HIGHEST-frequency child event (a tick per state/
    // activity change of every child) — route it through the targeted
    // single-row update instead of the full-tree rebuild.  The full render
    // (sort + replaceChildren + observer re-observe of every row) is
    // reserved for membership/sort-order changes: a terminal-bucket
    // crossing here, and created/closed/rename in their own handlers.
    // _updateChildRow falls back to renderChildren() itself when the row
    // isn't painted yet (a brand-new child).
    const isTerminal =
      existing.state === "closed" || existing.state === "deleted";
    if (wasTerminal !== isTerminal) renderChildren();
    else _updateChildRow(childId);
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
    // Clearing the live cache eagerly on close prevents stale
    // pending_approval_details from continuing to render approve/deny
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
    // Stamp onto the CYCLE containing this call_id — several details
    // can be live at once (parallel task agents).
    const detail = _liveApprovalDetails(cachedLive).find(
      (d) =>
        Array.isArray(d.items) &&
        d.items.some((it) => it && it.call_id === callId),
    );
    if (!detail) {
      // No pending detail to stamp the verdict onto. The verdict is
      // still durable in storage; the next bulk fetch will hydrate
      // the details and include the verdict via the existing
      // serialize path.
      return;
    }
    // Stamp on the matching item (UI render reads judge_verdict per
    // item) AND on the by-call_id map (matches the
    // serialize_pending_approval_details shape).
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
    // Remove ONLY the resolved cycle; siblings keep their buttons.
    // Legacy events without a cycle_id (pre-multi-cycle node) clear
    // everything, matching the old single-slot behavior.
    const details = _liveApprovalDetails(cachedLive);
    const remaining = ev.cycle_id
      ? details.filter((d) => d.cycle_id !== ev.cycle_id)
      : [];
    cachedLive.pending_approval = remaining.length > 0;
    cachedLive.pending_approval_details = remaining;
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
    // Append (or replace, keyed by cycle_id) — several cycles can be
    // outstanding under parallel task agents; a sibling's push must
    // not clobber ours.  Legacy nodes without cycle_id fold to a
    // single-slot replace via the shared "" key.
    const key = detail.cycle_id || "";
    const details = _liveApprovalDetails(cachedLive).filter(
      (d) => (d.cycle_id || "") !== key,
    );
    details.push(detail);
    cachedLive.pending_approval = true;
    cachedLive.pending_approval_details = details;
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
  const pruneTimer = setInterval(_pruneChildren, CHILDREN_PRUNE_INTERVAL_MS);

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

  // Export + "end" actions moved to the pane tab dropdown (step 7) — the
  // coordinator header is now a slim pane header.  The logic stays reachable:
  // export via exportWorkstreamDownload(wsId) (utils.js), close via the pane's
  // closeSession() API (coordCloseSession, on the factory return).

  // Mobile-only sidebar toggle — wires the accordion collapse below 700px.
  // On desktop the button is display:none so the handler is a no-op.
  const sidebarEl = root.querySelector("#coord-sidebar");
  const sidebarToggle = root.querySelector("#coord-sidebar-toggle");
  const sidebarToggleGlyph = root.querySelector("#coord-sidebar-toggle-glyph");
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
  // Per-message rewind / edit / retry affordance (#549)
  //
  // Mirrors the interactive pane (ui/static/app.js): edit + rewind buttons
  // on every user bubble, a retry button on the last assistant turn. The
  // bare ``.msg.user`` turn-count matches the server's _find_turn_boundaries
  // (which counts system-nudge user turns), so the N we POST equals the N
  // the server's rewind(n) cuts.
  // ------------------------------------------------------------------

  function _rewindToTurns(turns) {
    if (busy) return;
    if (!Number.isInteger(turns) || turns < 1) return;
    authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/rewind", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turns }),
    }).catch((err) => {
      appendText("error", "Rewind failed: " + err.message);
    });
  }

  function _rewindToMessage(msgEl) {
    // KNOWN GAP (#894, do not re-derive): from clear_ui arrival until
    // the next SUCCESSFUL refetchHistory render — the fetch window AND
    // the failed-fetch aftermath — the stale transcript is visible
    // with busy false, so this DOM count can over-rewind.  Port
    // interactive.js's #890 shape: a transcript-staleness latch set at
    // clear_ui, cleared only by the full render, gating the mutating
    // affordances (a plain refetch-in-flight flag is NOT enough — it
    // reopens on the failed exit, the bug interactive hit).  The
    // retry leg needs a quiesce-free variant here.
    if (busy) return;
    const userMsgs = messagesEl.querySelectorAll(".msg.user");
    const idx = Array.prototype.indexOf.call(userMsgs, msgEl);
    if (idx < 0) return;
    const turnsToRewind = userMsgs.length - idx;
    _rewindToTurns(turnsToRewind);
  }

  function _retryLast() {
    if (busy) return;
    authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/retry", {
      method: "POST",
      credentials: "include",
    }).catch((err) => {
      appendText("error", "Retry failed: " + err.message);
    });
  }

  function _editAndResend(msgEl, newText) {
    if (busy) return;
    const userMsgs = messagesEl.querySelectorAll(".msg.user");
    const idx = Array.prototype.indexOf.call(userMsgs, msgEl);
    if (idx < 0) return;
    const turnsToRewind = userMsgs.length - idx;
    setBusy(true);
    // Latch the edited text — the clear_ui SSE handler dispatches it once the
    // rewind's truncated history is re-fetched over REST.
    _pendingEditSend = newText;
    authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/rewind", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turns: turnsToRewind }),
    })
      .then(async (r) => {
        if (r && !r.ok) {
          _pendingEditSend = null;
          setBusy(false);
          appendText(
            "error",
            "Rewind failed (HTTP " + r.status + " " + r.statusText + ")",
          );
          return;
        }
        // A 200 {"status":"busy"} means the rewind was rejected (a
        // generation is in flight) — no clear_ui fires, so clear the latch +
        // busy here or the composer wedges and the latch fires on the next
        // unrelated clear_ui.
        let data = null;
        try {
          data = await r.json();
        } catch {
          data = null;
        }
        if (data && data.status === "busy") {
          _pendingEditSend = null;
          setBusy(false);
          appendText(
            "error",
            "Cannot edit & resend while the coordinator is processing.",
          );
        }
      })
      .catch((err) => {
        _pendingEditSend = null;
        appendText("error", "Rewind failed: " + err.message);
        setBusy(false);
      });
  }

  function _startEdit(msgEl, originalText) {
    if (busy) return;
    // Save current child nodes so Cancel can restore them.
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
      _editAndResend(msgEl, newText);
    });
    actions.appendChild(sendBtn);

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

  function _addUserMsgActions(el, text) {
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Message actions");
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
      _startEdit(el, text);
    });
    bar.appendChild(editBtn);
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
      _rewindToMessage(el);
    });
    bar.appendChild(rewindBtn);
    el.appendChild(bar);
  }

  function _addRetryAction(el) {
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
      _retryLast();
    });
    bar.insertBefore(btn, bar.firstChild);
  }

  function _refreshRetryButton() {
    const old = messagesEl.querySelectorAll(".msg.assistant .msg-actions");
    for (let i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
    // Skip retry when the most recent semantic turn is tool-only (last DOM
    // child is a .conv-batch construct); walk back past operator-context
    // rows first — the plain system bubble AND the structured watch-result /
    // guard-finding / idle-children cards all carry .operator-context — so the
    // guard still fires when the tool turn carried a nudge / guard finding.
    // Keying on the shared marker (not any single card class) keeps the skip
    // correct as new card kinds are added. (Both personas now render the
    // shared .conv-batch construct; this pane's DOM only holds its own.)
    let lastChild = messagesEl.lastElementChild;
    while (lastChild && lastChild.classList.contains("operator-context")) {
      lastChild = lastChild.previousElementSibling;
    }
    if (lastChild && lastChild.classList.contains("conv-batch")) {
      return;
    }
    const assistants = messagesEl.querySelectorAll(".msg.assistant");
    if (assistants.length) {
      _addRetryAction(assistants[assistants.length - 1]);
    }
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
      if (nameEl) nameEl.textContent = wsSnapshot.name || "";
      if (statusEl) statusEl.textContent = wsSnapshot.state || "";
    } catch (e) {
      appendText("error", "Failed to load coordinator: " + e.message);
      return;
    }
    await refetchHistory(true);
    // History alone can't tell whether an orphaned assistant tool_calls turn
    // is awaiting approval or merely still running; the live workstream
    // snapshot can.  First-paint only — a mid-session clear_ui refetch does
    // NOT re-run this (SSE re-delivers approve_request live, and replaying a
    // stale pre-rewind pending batch would be wrong).
    try {
      const pendingDetails =
        wsSnapshot &&
        wsSnapshot.pending_approval &&
        Array.isArray(wsSnapshot.pending_approval_details)
          ? wsSnapshot.pending_approval_details
          : [];
      pendingDetails.forEach((pendingDetail) => {
        if (!pendingDetail || !Array.isArray(pendingDetail.items)) return;
        appendToolBatch(pendingDetail.items, {
          pending: true,
          judgePending: !!pendingDetail.judge_pending,
          cycleId: pendingDetail.cycle_id || "",
        });
      });
    } catch (e) {
      console.warn("pending-approval replay failed", e);
    }
    // Load children + tasks in parallel — neither blocks SSE connection.
    loadChildren();
    loadTasks();
    // Pull any in-flight attachment reservations (page reload / cross-tab
    // switch) so the chips reappear instead of silently orphaning rows.
    attachments.rehydrate();
    connectSSE();
  }

  // Fetch /history and (re)render the message column from scratch.  Used for
  // first paint, the clear_ui re-render after a rewind/retry truncates the
  // conversation, and the truncated resync (via loadHistoryThenReconnect,
  // which seeds the cursor and reconnects).  The fetch is wrapped; the
  // render runs after the clear so a render bug surfaces loudly instead of
  // masking as an empty pane.  The message column + tool-tracking state
  // (toolRows / activeBatch, which the render rebuilds) reset alongside the
  // wipe — after the fetch guard — so a mid-session re-render leaves no stale
  // call_id→row mappings pointing at detached DOM, while a FAILED fetch
  // leaves both DOM and maps untouched.  On first paint they're already
  // empty — harmless no-ops.
  async function refetchHistory(seedCursor = false) {
    let hist = null;
    try {
      hist = await getJSON(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/history",
      );
    } catch (e) {
      console.warn("coord history fetch failed", e);
      hist = null;
    }
    // A FAILED fetch keeps the pane intact: the wipe + tracking resets
    // run only once a payload actually arrived.  (Pre-#882 the wipe ran
    // before this guard, so a failed /history — likeliest during the
    // restart windows that trigger truncated resyncs — left an EMPTY
    // pane on a live stream with no retry record.  Stale-but-real
    // content beats blank, and the tool-row/batch maps stay valid
    // against the untouched DOM.  On the clear_ui caller a failure now
    // shows the pre-rewind transcript instead of an empty pane — also
    // stale-but-real; the rewound truth lands on the next successful
    // refetch.)  Success ordering is unchanged: the wipe always ran
    // after the await, never as immediate feedback.
    if (!hist) return;
    messagesEl.replaceChildren();
    // A full committed-history render repairs any recorded truncation gap —
    // whether this render came from the truncated resync itself or from an
    // unrelated clear_ui rebuild — so it supersedes ALL pending repair
    // intent in one place: the retry cursor (a LATER failed reload must not
    // rewind onto a gap that no longer exists), the deferred mid-turn
    // latch, and any still-pending jittered timer.  Leaving the latch or
    // timer armed past a heal fired a phantom resync against the repaired
    // gap — a false truncatedGaps bump plus a needless teardown, and on
    // that redundant load's FAILED-fetch leg the cursor had been dropped
    // with no record set, so the reconnect came back cursorless with
    // nothing armed to re-cover the suspend window.  (Only a render clears
    // any of these — a failed fetch returned above with the record intact,
    // which is what arms the connect chokepoint's retry; and cancelling
    // the timer here is safe precisely because the gap it was scheduled
    // for was just rendered away.)
    truncatedFromCursor = null;
    pendingTruncatedResync = false;
    if (truncatedResyncTimer) {
      clearTimeout(truncatedResyncTimer);
      truncatedResyncTimer = null;
    }
    toolRows.clear();
    activeBatch = null;
    renderedSystemEventIds.clear();
    resetCompactionHolder(compactionHolder);
    // Fresh-connect fast-forward: when the trailing turn is an executing
    // in-flight tool batch the server can replay, /history returns a
    // non-null ``cursor`` and OMITS that turn. Seed ``lastEventId`` so the
    // connectSSE on the reconnecting paths (seedCursor=true: init first
    // paint AND the truncated resync's loadHistoryThenReconnect) opens with
    // ?last_event_id= and the replay_ok delta rebuilds the in-flight turn
    // — otherwise the trimmed turn would be neither in /history nor
    // replayed. The clear_ui re-render caller (the one remaining seedless
    // live-stream caller) leaves seedCursor false: it runs on a live stream
    // with no reconnect and must NOT rewind lastEventId off the live
    // position. Mirrors ui/static/app.js.
    if (seedCursor && hist.cursor != null) lastEventId = hist.cursor;
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
    const callOutcomes = new Map();
    (hist.messages || []).forEach((m) => {
      if ((m.role || "tool") !== "tool" || !m.tool_call_id) return;
      // The server-side /history projection derives denied / is_error on
      // each tool message (content-prefix heuristic + persisted flags),
      // so we read those fields directly rather than re-sniffing content
      // here.  "Denied by user" / "Blocked" -> denied; the error-prefix
      // set (Error, Command timed out, ...) -> is_error.  The
      // assistant.tool_calls render below reads this map to mark a batch
      // resolved-denied (vs the default resolved-approved) and to
      // propagate the error flag to appendToolResult; a call_id absent
      // from the map is an orphan (no result yet) -> --running.
      let outcome = "ok";
      if (m.denied) {
        outcome = "denied";
      } else if (m.is_error) {
        outcome = "error";
      }
      callOutcomes.set(m.tool_call_id, outcome);
    });

    // Render an assistant turn's tool_calls as a single batch
    // construct.  Synthesises one batch per assistant turn so a
    // parallel fan-out (tool_calls.length ≥ 2) reads as one cohesive
    // dispatch, matching how live SSE renders the same flow via
    // approve_request / tool_info.  Resolved when every call_id has
    // a matching tool result; otherwise --running (see the
    // resolvedCallIds rationale above).  SSE upgrades --running in
    // place when it knows more.
    function renderAssistantToolBatch(m) {
      const items = m.tool_calls.map((tc) => {
        // tool_calls arrive flattened by the server /history projection:
        // {id, name, arguments} (no nested `function` wrapper).
        const name = String((tc && tc.name) || "tool");
        const callId = String((tc && tc.id) || "");
        const argsRaw = String((tc && tc.arguments) || "");
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
      // anchored to the .conv-row that tripped the guard
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

    (hist.messages || []).forEach((m) => {
      const role = m.role || "tool";

      // The server /history projection collapses multipart user content
      // to a plain string and surfaces a structured ``attachments`` list
      // ({kind, filename, mime_type}); coord renders the same pill
      // cluster the interactive pane shows from those fields.  (Content
      // is a string for every role post-projection; the guard is purely
      // defensive.)
      const content = typeof m.content === "string" ? m.content : "";
      const userAttachments = [];
      if (Array.isArray(m.attachments)) {
        for (const a of m.attachments) {
          if (!a || typeof a !== "object") continue;
          userAttachments.push({
            kind: String(a.kind || "other"),
            filename: String(a.filename || ""),
          });
        }
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
        appendToolResult(toolName, callId, content || "", isError);
        // Tool-channel metacog nudges + queued interjections that used to
        // splice into the tool result now follow it as first-class
        // operator-context ``system`` rows and render via the ``system``
        // branch below in sequence.
      } else if (role === "assistant") {
        // Reasoning bubble (Phase 1 reasoning persistence) — render
        // BEFORE the content card so the visual order matches the
        // live SSE flow (reasoning_delta arrives before content_delta
        // for thinking-enabled models). Mirrors the live ":1524" /
        // snapshot ":2021" call sites — same appendMsg("reasoning")
        // helper, just driven from history-render rather than the
        // SSE handler. Only present when the active model's
        // surface_persisted_reasoning flag is true and the message round-tripped
        // a thinking lane.
        if (typeof m.reasoning === "string" && m.reasoning.length) {
          const rEl = appendMsg("reasoning", "", { label: "reasoning" });
          const rBody = rEl && rEl.querySelector(".msg-body");
          if (rBody) rBody.textContent = m.reasoning;
        }
        // Render content BEFORE the tool batch so DOM order matches
        // chronological order (the model emits text first, then
        // dispatches tools).  Whitespace-only content (e.g. "\n\n"
        // from a reasoning-parser model that strips <think>…</think>
        // and leaves only trailing newlines before the tool call) is
        // treated as empty — without the .trim() guard it would
        // render a visible-but-empty .msg.assistant card on replay,
        // which the live stream never showed (the live path didn't
        // accumulate the trailing whitespace as a visible bubble).
        if (content && content.trim()) {
          // Run assistant content through the markdown pipeline
          // (renderMarkdown + post-render hljs / mermaid / KaTeX) so
          // a reconnect / page-reload renders the same way a live
          // stream does.  appendText would only escape and dump the
          // raw text — markdown tables, code fences, math, and links
          // would all render as literal characters.
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
        }
        // Tool batch comes after the content card so the DOM matches
        // the chronological order the model emitted (text → dispatch).
        // Hoisting this out of the role-agnostic top of the loop —
        // the prior shape rendered tool_calls before the assistant
        // text that announced them, putting parallel batches
        // visually above their narrating message on rehydrate.
        if (Array.isArray(m.tool_calls) && m.tool_calls.length) {
          renderAssistantToolBatch(m);
        }
      } else {
        // user / reasoning / system / other roles render as plain
        // text on history replay — matches the live-streaming paths
        // (appendReasoningToken uses textContent; user/system are
        // typed verbatim and don't carry markdown structure).  User
        // bubbles additionally render the pill strip beneath the
        // text when the message carried attachments — even when the
        // text portion is empty (image-only sends).
        if (role === "user") {
          const isSystemNudge = m.source === "system_nudge";
          if (isSystemNudge) {
            // Wake-driven empty user turn: render the thin marker
            // (replaces the previously-skipped synthetic empty bubble).
            // The nudges it carried are now first-class operator-context
            // ``system`` rows that follow it and render below.
            appendSystemNudgeMarker();
            return;
          }
          if (!content && userAttachments.length === 0) return;
          appendUserMessageWithAttachments(content, userAttachments, {
            label: role,
          });
        } else if (role === "system") {
          // First-class operator-context system turn — ``renderSystemTurn``
          // routes by ``m.source`` to the structured card (watch / guard /
          // idle-children) or the operator bubble, reading ``m.meta`` from the
          // ``/history`` projection so replay matches the live render exactly.
          if (!content) return;
          renderSystemTurn(m.source || "", content, m.meta);
          if (m.event_id != null)
            renderedSystemEventIds.add(String(m.event_id));
        } else {
          if (!content) return;
          appendText(role, content, { label: role });
        }
      }
    });
    // Attach the retry affordance to the last assistant turn on every render
    // (first paint, the clear_ui re-render, and the truncated resync's
    // rebuild via loadHistoryThenReconnect), matching the
    // interactive replayHistory() path. finishAssistantStream only covers the
    // live-turn-ends case — without this a reloaded or rewound coordinator
    // showed assistant turns with no retry button.
    _refreshRetryButton();
  }

  // Re-arm the stream after a 401 re-auth: reset backoff + reconnect now.
  // (Standalone wires this to the page login hook; the console shell fans
  //  login out to every open pane.)
  function onLogin() {
    reconnectAttempts = 0;
    // connectSSE's closeStreamTransport prologue cancels any pending
    // transport retry timers (reconnect / degraded / truncated resync)
    // before dialling.
    connectSSE();
  }

  // Symmetric teardown for a pane close — the IIFE had no close path; a
  // per-instance pane must release the stream + every timer/observer or a
  // backgrounded pane keeps an SSE open and fires renders into detached DOM.
  function destroy() {
    // Stream + the retry timers (reconnect backoff, degraded catch-up,
    // truncated resync).
    closeStreamTransport();
    [
      cancelTimeoutId,
      forceTimeoutId,
      tasksRefreshTimer,
      liveBadgeFlushTimer,
    ].forEach((t) => t && clearTimeout(t));
    cancelTimeoutId = forceTimeoutId = null;
    tasksRefreshTimer = liveBadgeFlushTimer = null;
    if (pruneTimer) clearInterval(pruneTimer);
    // The document-level visibilitychange listener holds a strong ref to this
    // closure — leaving it registered would both leak the pane and let a show
    // edge reopen a stream for a destroyed pane.
    removeVisibilityHandler();
    if (_childObserver && _childObserver.disconnect)
      _childObserver.disconnect();
  }

  // Reconnect a DEAD stream NOW (reset backoff), leaving a live one alone —
  // OPEN is healthy and CONNECTING means native retry / a fresh connect is
  // already working the problem.  The shell calls this on an explicit re-open
  // of an already-open pane (saved-list resume POSTs /open first): a
  // coordinator whose session was closed under the pane sits in the capped
  // retry loop — this short-circuits straight to a fresh /events against the
  // reopened session (a stale replay cursor degrades to the server's
  // fresh-replay path).
  function reconnect() {
    if (evtSource && evtSource.readyState !== EventSource.CLOSED) return;
    onLogin();
  }

  // Enter-to-send / Shift-Enter newline / IME-safe handling lives in
  // shared/composer.js; no duplicate listener here.
  return {
    wsId: wsId,
    connect: init,
    destroy: destroy,
    onLogin: onLogin,
    reconnect: reconnect,
    closeSession: coordCloseSession,
  };
}

// Imported by the console shell (shell.js) and the standalone coordinator page's
// bootstrap (coordinator/index.html), the same way shell.js imports the
// interactive pane.  No `window.*` bridge here: unlike interactive.js — whose
// classic standalone ui/static/app.js still consumes the global — the
// coordinator has no classic consumer, so the bare ESM export is the only seam.
export { createCoordinatorPane };
