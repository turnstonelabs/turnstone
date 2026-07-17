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
//  coordinator pane followed in 5e.0, and the shared substrate it leans on —
//  composer/renderer/auth/etc. — became modules too, imported below).  The
//  console shell.js imports the factory directly; the standalone loads it as
//  `<script type="module">`.
//
//  House style: programmatic DOM, NO innerHTML (the renderer is the sole
//  sanctioned exception); pane code root-scopes to its own element.
// ===========================================================================

import {
  stripAnsi,
  buildWatchResultCard,
  buildCompactionCard,
  applyCompactionEvent,
  resetCompactionHolder,
  sendAbortMs,
  buildSystemNudgeMarker,
  buildConvBatchShell,
  buildConvRow,
  buildConvCmd,
  buildConvVerdict,
  buildConvWarning,
  buildConvActions,
  buildConvStatus,
  buildAgentCardBody,
  buildPreviewChip,
  batchKicker,
  indexLabel,
} from "./conversation.js";
import { redactCredentials } from "./redact_credentials.js";
import { authFetch } from "./auth.js";
import { showToast } from "./toast.js";
import { Composer } from "./composer.js";
import {
  createAttachmentController,
  kindIcon,
} from "./composer_attachments.js";
import { createQueueController } from "./composer_queue.js";
import { StatusBar } from "./status_bar.js";
import { streamingRender, streamingRenderFinalize } from "./renderer.js";
import { setMarkdown, operatorSourceLabel } from "./utils.js";
import {
  OVERFLOW_TRIP_COUNT,
  OVERFLOW_TRIP_WINDOW_MS,
  DEGRADED_COOLDOWN_BASE_MS,
  DEGRADED_COOLDOWN_MAX_MS,
  DEGRADED_COOLDOWN_RESET_MS,
  overflowWindowTripped,
  degradedCooldownStep,
} from "./sse_overflow.js";

let _paneCounter = 0;

// Voice-role availability comes from /v1/api/models (stt_default_alias /
// tts_default_alias — present only when an audio-capable model role is
// configured).  Memoized so all panes share a single fetch; affordances stay
// hidden until it resolves.
// Memoized per transport base — a console pane proxies a node-hosted session,
// so its voice roles come from THAT node's /v1/api/models (base "/node/{id}"),
// not the console's; standalone panes use base "" and share one fetch.
const _voiceRolesPromises = {};

// Max child steps held per task agent while its parent row is unpainted (see
// InteractivePane._bufferAgentOrphan).  A real agent's whole sub-trajectory is
// well under this; the cap only bounds a pathological never-arriving parent.
const _AGENT_ORPHAN_CAP = 256;

// Grace window before a child step whose task_agent row never paints (an id-
// correlation mismatch, or an agent that aborted before its row painted) is
// escaped to a top-level row so it stays VISIBLE rather than buffered forever.
// The ordering race the buffer targets resolves within a frame, far inside it.
const _AGENT_ORPHAN_GRACE_MS = 500;

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
  // Is THIS pane the user's current focus?  Gates focus-stealing so a caret is
  // never yanked into a backgrounded pane.  A lone pane is always focused.
  isFocused() {
    return true;
  },
  // EventSource-error policy.  Native auto-reconnect handles transient drops; a
  // bare/console pane relies on it, so this is a no-op.  The standalone focused
  // pane additionally refetches + reassigns the ws list (see app.js).
  onStreamError() {},
  // EventSource (re)opened — the dual of onStreamError.  The console pane host
  // uses it to reset its terminal-failure counter (see createInteractivePane).
  onStreamOpen() {},
  // Where the ``--skip-permissions`` banner lands (standalone: #ui-header).
  warningTarget(pane) {
    return pane.messagesEl;
  },
  // An MCP server needs (re-)consent — the standalone shell drives its
  // settings-gear badge; a bare/console pane surfaces it inline only.
  onConsentDetected() {},
  // A tool result carried a preview-pane descriptor — the L-shell host opens
  // the preview pane beside this one; a bare pane keeps the transcript chip
  // as the only affordance.
  onPreview() {},
};

// The SSE overflow-recovery limiter (storm-guard threshold, cooldown-ladder
// constants, and the two pure helpers overflowWindowTripped /
// degradedCooldownStep) lives in ./sse_overflow.js — imported above and shared
// with the coordinator pane so the trip math has a single source of truth.
// The stateful glue (_noteStreamOverflow / _enterDegradedCatchup below) stays
// here because it is coupled to this class's disconnectSSE/connectSSE seam.

class Pane {
  constructor(wsId, opts) {
    opts = opts || {};
    this.id = "p" + ++_paneCounter;
    this.wsId = wsId || null;
    // Transport + host seam.  ``base`` is the node-proxy URL prefix ("" for a
    // local session, "/node/{id}" when the console proxies a session that lives
    // on a cluster node — the LOCALITY invariant).  ``host`` supplies the few
    // things only the surrounding shell knows (which pane is focused, the
    // stream-error recovery policy, the warning-banner target); see
    // createInteractivePane below.  Every pane is L-shell-hosted — the
    // standalone split-pane chrome (focus tracking, context menu, header with
    // split/close buttons) was retired with the step-6 fork collapse.
    this._base = opts.base || "";
    this._host = opts.host || INTERACTIVE_DEFAULT_HOST;
    this._onClose = typeof opts.onClose === "function" ? opts.onClose : null;
    this.evtSource = null;
    this.el = null;
    this.messagesEl = null;
    this.inputEl = null;
    this.sendBtn = null;
    this.stopBtn = null;
    this.currentAssistantEl = null;
    this.currentReasoningEl = null;
    this.contentBuffer = "";
    this.busy = false;
    this.isThinking = false;
    // Acting user (turn initiator) of the in-flight turn, from state_change
    // events; drives the shared-workstream cross-user send gate. Carries the
    // owner id even single-user (the gate just no-ops — it equals this viewer);
    // null when idle/error or when the backend sends no acting id
    // (unauthenticated / older backend).
    this._actingUserId = null;
    // Live approval cycles, cycleId → {blockEls, callIds}.  Parallel task
    // agents gate concurrently, so several approve_request cards can be
    // outstanding at once; each resolves independently by its cycle_id.
    // Insertion order = arrival order — keyboard shortcuts act on the
    // OLDEST (first) entry.  pendingApproval / approvalBlockEl are
    // DERIVED views maintained by _syncApprovalState(): the boolean for
    // the many "is anything pending" readers, the element pointing at
    // the active (oldest) cycle's card for keyboard/feedback routing.
    this.approvalCycles = new Map();
    this.pendingApproval = false;
    // Cross-user send gate (another participant's turn is in flight),
    // recomputed in _reconcileSendBlock. Stored on the App because the
    // App — not the Composer — owns sendBtn.disabled (see
    // _reconcileSendDisabled + the composer's externalDisable option).
    this._crossUserBlocked = false;
    this.approvalBlockEl = null;
    // Early-paint shells from ``tool_pending`` events, keyed by their
    // sorted call_id set, awaiting the authoritative ``tool_info`` /
    // ``approve_request`` upgrade.  A Map (not a single slot): with
    // parallel task agents several announces can be in flight, and a
    // sibling's announce must not discard ours.
    this.announcedBlocks = new Map();
    this.retryDelay = 1000;
    this.model = "";
    this.modelAlias = "";
    this.projectName = "";
    this._lastStatusEvt = null;
    this._historyLoadToken = 0;
    // Event backlog while a clear_ui / replay_truncated rebuild is in
    // flight — see _beginReplayQuiesce.  {token, events[]} or null.
    this._replayQueue = null;
    // Hot-path caches — all invalidated by _clearAgentTracking/replayHistory.
    // _nearBottom mirrors the scroller position via a passive scroll listener
    // (no per-token geometry reads); the two Maps make per-event row/stream
    // lookups O(1) instead of whole-transcript attribute-selector scans.
    this._nearBottom = true;
    this._scrollPinPending = false;
    this._scrollPinForce = false;
    this._thinkingEl = null;
    // Compaction lifecycle holder for the shared reducer
    // (conversation.applyCompactionEvent); `card` is the in-progress card
    // between start and end, nulled wherever the transcript DOM is wiped.
    this._compaction = { card: null, cid: null };
    this._retryHolderEl = null;
    this._toolRowIndex = new Map();
    this._streamElIndex = new Map();
    this._resizeObs = null;
    // Set when replay_truncated arrives mid-stream (refetching then would
    // detach the live bubble); consumed on the next idle edge.
    this._pendingTruncatedResync = false;
    // Field instrumentation for the two distinct "output stops while the
    // backend is healthy" causes: server-signalled overflow closes
    // (dropped-events class) vs client dispatch/render throws (wedge
    // class).  The console lines at each increment carry the running
    // count, so a field report shows which class fired without a
    // debugger attached.
    this._streamHealth = { overflows: 0, renderThrows: 0, malformedFrames: 0 };
    // Rolling timestamps of stream_overflow closes — input to the
    // degraded catch-up limiter (see overflowWindowTripped above).
    this._overflowTimes = [];
    this._degradedTimer = null;
    this._degradedCooldownMs = DEGRADED_COOLDOWN_BASE_MS;
    // Timestamp of the last degraded-catchup trip; drives the cooldown
    // ladder's escalate-vs-reset decision independently of _overflowTimes.
    this._lastDegradedAt = 0;
    // Close-on-hide bookkeeping.  A hidden tab's throttled event loop is
    // the likeliest too-slow SSE consumer, so the visibilitychange
    // handler closes the stream on hide and reconnects with the saved
    // Last-Event-ID on show (replay_ok covers the gap).
    // _hiddenDisconnect marks that WE closed for hide, so show never
    // resurrects a stream that was closed deliberately elsewhere.
    this._visHandler = null;
    this._hiddenDisconnect = false;
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
    this.approvalCycles = new Map();
    this.pendingApproval = false;
    this.approvalBlockEl = null;
    this.announcedBlocks = new Map();
    this._pendingEditSend = null;
    this.inputEl.disabled = false;
    // setBusy(false) above reconciled sendBtn.disabled while
    // pendingApproval was still stale-true; re-reconcile now that the
    // cycle state is cleared so a reset-from-pending re-enables send.
    this._reconcileSendDisabled();
    this.attachments.clearChips();
    this._stopRecording(true);
    this._stopTTS();
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
    // Any pending degraded-catch-up reconnect is owned by the stream
    // lifecycle: whoever closes the stream (ws switch, giveUp, destroy,
    // or a fresh manual connect — connectSSE's first line lands here)
    // supersedes it.  _enterDegradedCatchup re-arms AFTER its own
    // disconnect, so this never cancels the timer it is about to set.
    if (this._degradedTimer) {
      clearTimeout(this._degradedTimer);
      this._degradedTimer = null;
    }
    if (this.evtSource) {
      this.evtSource.close();
      this.evtSource = null;
    }
    // Deliberately NOT cleared here: _agentCards/_agentOrphans and any armed
    // _replayQueue.  disconnectSSE also runs for transport-only reconnects
    // (connectSSE's first line, the host's 5s recovery beat) where the DOM
    // survives — wiping the card map there made the next child event build a
    // DUPLICATE agent card beside the still-attached one, and cancelling
    // orphan grace timers silently dropped buffered steps.  Ws-switch and
    // full-reload cleanup happens in _loadHistoryThenConnect; terminal
    // cleanup in the factory's destroy().
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
    // Re-evaluate the shared-workstream cross-user send gate on every busy
    // edge (the acting-user id is tracked from state_change events).
    this._reconcileSendBlock();
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

  // Shared-workstream send gate: while another participant's turn is in flight,
  // disable this viewer's send. A mid-turn interjection would run under the
  // initiator's identity (their MCP credentials, not this viewer's) and be
  // misattributed to them, so the server rejects it with a 409 — this is the
  // proactive UX half. No-ops on a single-user workstream (the acting user is
  // this viewer) or when the acting id is unknown (older backend). Compares
  // opaque user_id uuids: the acting id from state_change vs the viewer's own
  // id retained from /whoami (ts.user_id).
  _reconcileSendBlock() {
    let me = null;
    try {
      me = sessionStorage.getItem("ts.user_id");
    } catch (_e) {
      me = null;
    }
    const blocked =
      !!this.busy && !!this._actingUserId && !!me && this._actingUserId !== me;
    this._crossUserBlocked = blocked;
    // Still drive the Composer's placeholder / title hint — externalDisable
    // suppresses only its disabled write, not the hint.
    this.composer.setSendBlocked(
      blocked,
      blocked
        ? "Another participant's turn is in progress - wait for it to finish."
        : "",
    );
    this._reconcileSendDisabled();
  }

  // Single owner of ``sendBtn.disabled``.  The Composer runs with
  // ``externalDisable``, so it never writes the flag itself (it still
  // rotates the Send/Queue label + placeholder + stop button on
  // ``setBusy``); this combines every disable axis instead, so two
  // writers can't clobber each other on event ordering.
  //
  // ``busy`` is deliberately NOT an axis: this composer is
  // ``queueWhileBusy``, so a running turn keeps Send clickable as
  // "Queue".  The old ``pendingApproval || this.busy`` both defeated
  // that affordance (Send disabled while merely busy) and let
  // ``composer.setBusy`` — which re-enables in queue mode — race the
  // approval disable back off when a ``state_change`` fired after the
  // approve_request card rendered.
  _reconcileSendDisabled() {
    this.sendBtn.disabled = this.pendingApproval || this._crossUserBlocked;
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
    // Instance ref, not a container query: removeThinkingIndicator runs on
    // EVERY content/reasoning delta, and a class-selector miss walks the
    // whole transcript subtree — O(N) per streamed token at 5000 messages.
    if (this._compaction.card) return; // the compaction card owns the affordance
    if (this._thinkingEl) return;
    const el = document.createElement("div");
    el.className = "thinking-indicator";
    el.textContent = "Thinking";
    this._thinkingEl = el;
    this.messagesEl.appendChild(el);
    this.scrollToBottom();
  }

  removeThinkingIndicator() {
    if (!this._thinkingEl) return;
    this._thinkingEl.remove();
    this._thinkingEl = null;
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
    // The /history projection of a persisted compaction marker (an in-place
    // source="compaction" system row) — render the same result card the live
    // `compaction` end event paints, so a reload reproduces the transcript.
    if (source === "compaction") {
      const card = buildCompactionCard(meta, content || "");
      this.messagesEl.appendChild(card);
      this.scrollToBottom(true);
      return card;
    }
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

  handleCompactionEvent(evt) {
    // Shared reducer (conversation.applyCompactionEvent) — one lifecycle
    // state machine for this pane and the coordinator viewer.  The dedup
    // set is the same one the system_turn path uses: the persisted marker
    // row is stamped with the ok-end event's id, so whichever of /history
    // repaint or live/replayed event renders first wins.  reason="error"
    // ends render through the paired `error` event (red row), not here.
    this.removeEmptyState();
    if (!this._renderedSystemEventIds) {
      this._renderedSystemEventIds = new Set();
    }
    applyCompactionEvent(this._compaction, evt, {
      container: this.messagesEl,
      renderedIds: this._renderedSystemEventIds,
      onNotice: (msg) => this.addInfoMessage(msg),
      scroll: (force) => this.scrollToBottom(force),
    });
  }

  addCommandEcho(text) {
    // A slash command is control-plane input, not a conversational turn —
    // echo it as a distinct command chip (styled like a prompt line), not a
    // user bubble.  It is deliberately NOT persisted: commands don't join
    // the trajectory, so a bubble that vanished on reload was a lie.
    this.removeEmptyState();
    const el = document.createElement("div");
    el.className = "msg command-echo";
    el.setAttribute("aria-label", "command");
    el.textContent = text;
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
      const attachWsId = this.wsId;
      const attachBase = this._base;
      attachments.forEach(function (a) {
        const pill = document.createElement("span");
        pill.className = "msg-user-attach-pill";
        const icon = document.createElement("span");
        icon.className = "msg-user-attach-icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = kindIcon(a.kind);
        pill.appendChild(icon);
        const nameEl = document.createElement("span");
        nameEl.className = "msg-user-attach-name";
        nameEl.textContent =
          a.filename ||
          (a.kind === "image"
            ? "image"
            : a.kind === "audio"
              ? "audio"
              : "document");
        pill.appendChild(nameEl);
        const prev =
          typeof window.buildAttachmentPreview === "function"
            ? window.buildAttachmentPreview({
                kind: a.kind,
                wsId: attachWsId,
                base: attachBase,
                attachmentId: a.attachment_id,
                filename: a.filename,
              })
            : null;
        if (prev) {
          if (a.kind === "image" || a.kind === "pdf") icon.replaceWith(prev);
          else pill.appendChild(prev);
        }
        pills.appendChild(pill);
      });
      el.appendChild(pills);
    }
    this._addUserMsgActions(el, text);
    this.messagesEl.appendChild(el);
    this.scrollToBottom(true);
  }

  // --- Approval-cycle bookkeeping -----------------------------------------
  // The backend registers one ApprovalCycle per human-gated batch; parallel
  // task agents make several live at once.  Cards register here on paint and
  // deregister on resolution; the composer stays disabled while ANY cycle is
  // live.

  _syncApprovalState() {
    // Prune orphaned cycles whose block elements are no longer in the DOM.
    // A clear_ui / replay_truncated / re-render that wipes the conversation
    // subtree (messagesEl.replaceChildren()) also clears approvalCycles via
    // _resetStreamingRefs.  But if an approve_request event is processed
    // between the wipe and the refetch, its cycle card is in a detached
    // subtree and the matching approval_resolved may never arrive — leaving
    // pendingApproval=true and the send button disabled forever.
    for (const [cid, entry] of this.approvalCycles) {
      if (entry.blockEls && !entry.blockEls.some((el) => el.isConnected)) {
        this.approvalCycles.delete(cid);
      }
    }
    const first = this.approvalCycles.values().next();
    const active = first.done ? null : first.value;
    this.pendingApproval = this.approvalCycles.size > 0;
    this.approvalBlockEl = active ? active.blockEls[0] : null;
    this.inputEl.disabled = this.pendingApproval;
    this._reconcileSendDisabled();
  }

  _oldestCycleId() {
    const first = this.approvalCycles.keys().next();
    return first.done ? null : first.value;
  }

  _registerApprovalCycle(cycleId, blockEls, items) {
    const callIds = (items || []).map((it) => it && it.call_id).filter(Boolean);
    this.approvalCycles.set(cycleId, { blockEls, callIds });
    this._syncApprovalState();
  }

  // Cycle id for pre-multi-cycle servers that omit it: synthesize a stable
  // key from the batch's first call_id so the Map still routes uniquely.
  _cycleKey(evt) {
    if (evt.cycle_id) return evt.cycle_id;
    const first = ((evt.items || [])[0] || {}).call_id || "";
    return "legacy:" + first;
  }

  getFeedback(blockEl) {
    const el = blockEl || this.approvalBlockEl;
    if (!el) return null;
    const inp = el.querySelector(".conv-feedback");
    return inp && inp.value.trim() ? inp.value.trim() : null;
  }

  appendToolOutputChunk(callId, chunk) {
    if (!chunk) return;
    const stripped = stripAnsi(chunk);
    if (!stripped) return;
    // Capture pin before the chunk grows the stream block — see
    // announceToolBlock.
    const stick = this.isNearBottom();

    let el = this._streamEl(callId);
    if (!el) {
      let target = this._toolRow(callId);
      if (!target) {
        // A minted sub-agent child id ("<parent>::r{run}s{step}::<id>") whose row hasn't
        // nested yet must NOT graft its stream onto the last top-level batch —
        // that mislabels a sub-tool's output as a main-harness tool's.  Its row
        // arrives via the orphan flush; skip the chunk until then.
        if (callId && callId.includes("::")) return;
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
      if (callId) this._streamElIndex.set(callId, el);
    }

    el.appendChild(document.createTextNode(stripped));
    // rAF-coalesced inner pin: the eager scrollTop=scrollHeight after every
    // text append forced one whole-page reflow per chunk (geometry read on a
    // just-dirtied layout).  One pin per frame is visually identical.
    if (!el._pinPending) {
      el._pinPending = true;
      requestAnimationFrame(() => {
        el._pinPending = false;
        el.scrollTop = el.scrollHeight;
      });
    }
    this.scrollToBottom(stick);
  }

  showOutputWarning(evt) {
    if (!evt.call_id || evt.risk_level === "none") return;
    const toolDiv = this._toolRow(evt.call_id);
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
    // Badges anchor either inside the row (solo verdicts, replay) or at the
    // batch-block level (judge-pending panels) — scope the query to the
    // row's batch, which covers both, instead of scanning the whole
    // transcript per verdict event.  Row-less lookups (row already replaced
    // by output) fall back to the container scan so the late-verdict toast
    // path keeps working.
    const vRow = this._toolRow(verdict.call_id);
    const vScope = (vRow && vRow.closest(".conv-batch")) || this.messagesEl;
    const badge = vScope.querySelector(
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

    this.updateVerdictGlow(
      verdict.recommendation,
      vRow ? vRow.closest(".conv-batch") : null,
    );
  }

  updateVerdictGlow(recommendation, batchEl) {
    // Glow is a PER-BATCH aggregate: score the batch that owns the
    // verdict, not whichever cycle happens to be oldest.  With
    // concurrent approval cycles a sibling's verdict must neither
    // recolor the oldest card (the old single-El read seeded `worst`
    // with the sibling's recommendation) nor leave its own card
    // stale.  Batch-less rows (legacy replay shapes) fall back to the
    // oldest live cycle — the pre-multi-cycle behavior.
    const scope = batchEl || this.approvalBlockEl;
    if (!scope) return;
    const actions = scope.querySelector(".conv-actions");
    if (!actions) return;
    // Collect all verdict badges currently visible in this approval block.
    const badges = scope.querySelectorAll(".conv-verdict");
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
        tokensEl: this._sbTokens,
        toolsEl: this._sbTools,
        turnsEl: this._sbTurns,
      },
      evt,
    );
    // Model moved out of the status bar into the composer chip; capture the
    // (optional) effort off the status event and repaint the chip.
    this._effort = (evt && evt.effort) || "";
    this._paintModelChip();
    this._lastStatusEvt = evt;
  }

  // Paint the composer's "model · effort" read-out chip.  SILENT_EFFORTS
  // mirror (status_bar.js): "medium" is the implicit default and "" means no
  // knob — neither is worth showing, so only a non-medium effort appends the
  // "· effort" suffix.  An empty alias resets the chip to its placeholder.
  _paintModelChip() {
    if (!this.composer || !this.composer.setModel) return;
    const alias = this.modelAlias || this.model || "";
    const eff =
      this._effort && this._effort !== "medium" ? " · " + this._effort : "";
    this.composer.setModel(alias ? alias + eff : "");
  }

  // Paint the composer's "has a project" badge from the connected event's
  // project_name ("" = no project → the chip stays hidden).
  _paintProjectChip() {
    if (this.composer && this.composer.setProject)
      this.composer.setProject(this.projectName || "");
  }

  isNearBottom() {
    // Cached from the passive scroll listener (_createDOM) instead of read
    // from geometry: the old scrollHeight/scrollTop/clientHeight triplet
    // forced a synchronous layout of the whole transcript, and this runs on
    // every streamed token and every tool chunk.  Content growth without a
    // scroll leaves the cache untouched — which is the DESIRED semantics:
    // "pinned" is a statement about where the user last scrolled to, not
    // about the current pixel distance (the old post-append measurement is
    // exactly what used to silently disengage auto-follow at tool time).
    return this._nearBottom;
  }

  scrollToBottom(force) {
    if (force) this._scrollPinForce = true;
    else if (!this._nearBottom) return;
    // rAF-coalesced pin: at most one scrollHeight read + scrollTop write per
    // frame no matter how many deltas arrived.  The pin re-checks
    // _nearBottom AT FIRE TIME: a user wheel-scroll can land between the
    // schedule (when the cached flag was still true) and the rAF — pinning
    // anyway would yank them back to the bottom, and the programmatic
    // scroll's own event would re-mark the flag true, trapping them there
    // for the rest of the stream.  Scroll events fire before rAF callbacks
    // within a frame, so the re-check sees the user's disengage.  Force
    // requests latch across the coalescing window (a forced pin must win
    // even if a non-forced schedule got there first).
    if (this._scrollPinPending) return;
    this._scrollPinPending = true;
    requestAnimationFrame(() => {
      this._scrollPinPending = false;
      const forced = this._scrollPinForce;
      this._scrollPinForce = false;
      if (forced || this._nearBottom) {
        this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
      }
    });
  }

  _createDOM() {
    this.el = document.createElement("div");
    this.el.className = "pane pane--embedded";
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
      // Keyboard acts on the OLDEST live cycle (the one approvalBlockEl
      // tracks); sibling cards from parallel task agents resolve by
      // their own buttons or become oldest in turn.  When the feedback
      // field getting the keystroke belongs to a DIFFERENT cycle's
      // card, route to THAT cycle instead — the user is clearly acting
      // on the card they're typing into.
      let targetId = this._oldestCycleId();
      let targetBlock = this.approvalBlockEl;
      const ae = document.activeElement;
      if (ae && ae.classList && ae.classList.contains("conv-feedback")) {
        for (const [cid, entry] of this.approvalCycles) {
          if (entry.blockEls.some((el) => el.contains(ae))) {
            targetId = cid;
            targetBlock = entry.blockEls[0];
            break;
          }
        }
      }
      const fb = targetBlock
        ? targetBlock.querySelector(".conv-feedback")
        : null;
      if (ae && fb && ae === fb) {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          this.resolveApproval(
            true,
            false,
            this.getFeedback(targetBlock),
            false,
            targetId,
          );
        } else if (e.key === "Escape") {
          e.preventDefault();
          this.resolveApproval(
            false,
            false,
            this.getFeedback(targetBlock),
            false,
            targetId,
          );
        }
        return;
      }
      const k = e.key.toLowerCase();
      if (k === "y" || e.key === "Enter") {
        e.preventDefault();
        this.resolveApproval(
          true,
          false,
          this.getFeedback(targetBlock),
          false,
          targetId,
        );
      } else if (k === "n" || e.key === "Escape") {
        e.preventDefault();
        this.resolveApproval(
          false,
          false,
          this.getFeedback(targetBlock),
          false,
          targetId,
        );
      } else if (k === "a") {
        e.preventDefault();
        this.resolveApproval(
          true,
          true,
          this.getFeedback(targetBlock),
          false,
          targetId,
        );
      }
    });

    // Click-to-play for media embeds.  Pane-owned (on this.el) and root-scoped
    // via closest(".media-play-btn") so every embedded L-shell pane activates
    // its own players — the old standalone wired this via a document-level
    // delegated listener in app.js, which the console host never loaded (so the
    // Play button was dead in console-hosted panes).  Enter on a focused button
    // routes through the same path, mirroring the approval keydown above.
    this.el.addEventListener("click", (e) => {
      const btn = e.target.closest(".media-play-btn");
      if (!btn) return;
      e.preventDefault();
      activateMediaPlayButton(btn);
    });
    this.el.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const btn = e.target.closest(".media-play-btn");
      if (!btn || btn.disabled) return;
      // Single-path activation: preventDefault stops the browser's native
      // Enter-to-click from dispatching a second activation behind ours.
      e.preventDefault();
      activateMediaPlayButton(btn);
    });

    // No pane header: the workstream name, persona, and state are shown by the
    // tab and the rail (Workspaces); the --skip-permissions banner lands in
    // messagesEl (see the host warningTarget).  The standalone split-pane
    // chrome that used to live here (focus tracking, right-click context menu,
    // a header with split/close buttons) was retired with the step-6 fork
    // collapse.

    // Messages area
    this.messagesEl = document.createElement("div");
    this.messagesEl.className = "pane-messages";
    this.messagesEl.setAttribute("role", "log");
    this.messagesEl.setAttribute("aria-live", "polite");
    this.messagesEl.setAttribute("aria-label", "Chat messages");
    // Track "pinned to bottom" from actual scrolls (user or programmatic)
    // instead of reading scroller geometry per event — see isNearBottom().
    // Passive: never blocks the compositor thread.
    this.messagesEl.addEventListener(
      "scroll",
      () => {
        this._nearBottom =
          this.messagesEl.scrollHeight -
            this.messagesEl.scrollTop -
            this.messagesEl.clientHeight <
          80;
      },
      { passive: true },
    );
    // Layout changes that move the bottom WITHOUT a scroll event (window
    // resize, split-drag, orientation change) would leave the cached flag
    // stale — a user visually back at the bottom after growing the pane
    // stayed disengaged until they nudged the scroller.  Resizes are rare,
    // so the geometry read here is off the hot path by construction.
    if (typeof ResizeObserver === "function") {
      this._resizeObs = new ResizeObserver(() => {
        this._nearBottom =
          this.messagesEl.scrollHeight -
            this.messagesEl.scrollTop -
            this.messagesEl.clientHeight <
          80;
      });
      this._resizeObs.observe(this.messagesEl);
    }
    this.el.appendChild(this.messagesEl);

    // Per-workstream status bar (above input)
    this.statusBarEl = document.createElement("div");
    this.statusBarEl.className = "ws-status-bar";
    this.statusBarEl.setAttribute("role", "status");
    this.statusBarEl.setAttribute("aria-live", "polite");
    this.statusBarEl.setAttribute("aria-atomic", "true");
    this.statusBarEl.setAttribute("aria-label", "Workstream status");

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

    this.statusBarEl.appendChild(this._sbTokens);
    this.statusBarEl.appendChild(this._sbTools);
    this.statusBarEl.appendChild(this._sbTurns);
    this.el.appendChild(this.statusBarEl);

    // Input area — DOM + behavior comes from shared/composer.js.  The
    // pane keeps the attachment-upload pipeline (because attachments are
    // pane-specific state) and routes file events through the composer's
    // attach/paste/drop callbacks.
    this.composer = new Composer(this.el, {
      sendGlyph: "\u2191",
      layout: "stacked",
      modelChip: true,
      projectChip: true,
      attachments: {
        onAttach: (file) => {
          this.attachments.upload(file);
        },
      },
      stopBtn: true,
      queueWhileBusy: true,
      // The App owns sendBtn.disabled: it has to combine three axes the
      // Composer can't see together \u2014 a live approval cycle, the
      // cross-user send gate, and busy \u2014 so the Composer's own
      // disabled write (which re-enables in queueWhileBusy mode) would
      // otherwise race the approval disable back off on event ordering.
      externalDisable: true,
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
      getBase: () => {
        return this._base;
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
      // Node-proxy prefix — same seam the attachment controller uses, so
      // the dequeue DELETE reaches the node owning the session (a proxied
      // remote-node workstream has base "/node/{id}"; without this the
      // x-delete hit the console root and silently failed).
      getBase: () => {
        return this._base;
      },
      onAfterDequeue: () => {
        this.attachments.rehydrate();
      },
      onNotice: (msg) => {
        showToast(msg);
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
      // Right-align the mic next to send (instead of stranded by the model
      // chip) only when STT is actually available; .has-mic drives the CSS.
      if (this.composer && this.composer.actionsRowEl)
        this.composer.actionsRowEl.classList.toggle("has-mic", !!roles.stt);
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
    // Close-on-hide / replay-on-show: installed once per pane, removed
    // by the factory's destroy().  A hidden tab's throttled drain is the
    // likeliest slow consumer behind server-side queue overflow, and an
    // idle hidden tab holds a node connection for nothing — closing on
    // hide removes both, and the saved _lastEventId makes the show-edge
    // reconnect lossless (replay_ok from the server's ring buffer).
    if (!this._visHandler) {
      this._visHandler = () => this._onVisibilityChange();
      document.addEventListener("visibilitychange", this._visHandler);
    }
    // Never open an EventSource into a hidden (throttled) tab — including on a
    // FIRST load in a background tab, where the close-on-hide handler never
    // fires because there was no open stream to close (a throttled hidden tab
    // is the worst-case slow consumer that overflows the server send queue).
    // This is the single connect chokepoint, so it backstops every caller —
    // fresh connect, degraded retry, recover beat, show edge; the wsId + the
    // visibilitychange handler installed just above make the show edge
    // reconnect (replay_ok from the ring). The timer callbacks keep their own
    // pre-checks (the recover beat's also gates failCount), so this is the
    // net that closes the fresh-connect gap they never covered.
    if (document.hidden) {
      this._hiddenDisconnect = true;
      return;
    }
    this.evtSource = new EventSource(evtUrl);

    this.evtSource.onopen = () => {
      this.retryDelay = 1000;
      this.statusBarEl.classList.remove("ws-sb-disconnected");
      if (this._lastStatusEvt) this.updateStatus(this._lastStatusEvt);
      this._host.onStreamOpen(this);
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
      // Guarded parse + dispatch.  onmessage is the pane's whole event
      // pipeline: an exception escaping it doesn't close the EventSource, so
      // pre-guard a single malformed frame (or one throwing handler case)
      // left the streaming refs (currentAssistantEl / contentBuffer) stale
      // and every later turn painted into the poisoned segment — the
      // "output stops rendering while the backend is healthy" wedge.
      let data = null;
      try {
        data = JSON.parse(e.data);
      } catch (err) {
        this._streamHealth.malformedFrames += 1;
        console.warn(
          "interactive: dropping malformed SSE frame (total " +
            this._streamHealth.malformedFrames +
            ")",
          err,
        );
        return;
      }
      // Tag the event with its own SSE id so the system_turn handler can
      // dedup a turn already painted from /history against the same turn
      // redelivered by an SSE replay.  e.lastEventId is this event's id;
      // buffered events (system_turn included) always carry one.
      if (e.lastEventId) data._event_id = e.lastEventId;
      try {
        this.handleEvent(data);
      } catch (err) {
        this._streamHealth.renderThrows += 1;
        console.error(
          "interactive: handleEvent failed for " +
            (data && data.type) +
            " (render-throw total " +
            this._streamHealth.renderThrows +
            ")",
          err,
        );
      }
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

  _onVisibilityChange() {
    if (document.hidden) {
      // Closing beats letting the hidden tab's throttled event loop
      // starve the drain until the server-side queue overflows.  The
      // streaming refs (contentBuffer / currentAssistantEl) survive —
      // disconnectSSE is transport-only — so the visible tail is intact
      // when the tab comes back.
      if (this.evtSource) {
        this.disconnectSSE();
        this._hiddenDisconnect = true;
      }
    } else if (this._hiddenDisconnect) {
      this._hiddenDisconnect = false;
      if (this.wsId) this.connectSSE(this.wsId);
    }
  }

  _removeVisibilityHandler() {
    if (this._visHandler) {
      document.removeEventListener("visibilitychange", this._visHandler);
      this._visHandler = null;
    }
    this._hiddenDisconnect = false;
  }

  _noteStreamOverflow() {
    this._streamHealth.overflows += 1;
    const now = Date.now();
    this._overflowTimes.push(now);
    console.warn(
      "interactive: server closed the stream after a send-queue overflow " +
        "(total " +
        this._streamHealth.overflows +
        "); reconnect will replay the gap",
    );
    // Trip when OVERFLOW_TRIP_COUNT closes land inside the rolling window.
    // The cooldown-ladder reset lives in _enterDegradedCatchup (keyed off
    // _lastDegradedAt), NOT here — this method only counts and trips.
    if (
      overflowWindowTripped(
        this._overflowTimes,
        now,
        OVERFLOW_TRIP_COUNT,
        OVERFLOW_TRIP_WINDOW_MS,
      )
    ) {
      this._enterDegradedCatchup();
    }
  }

  _enterDegradedCatchup() {
    // Repeated overflow closes inside one window: this consumer cannot
    // keep up with live streaming right now, and each reconnect round
    // just stalls rendering behind the 2.5-4.5 s retry before
    // re-saturating.  Stop the churn: close the stream, say so in plain
    // language, and come back after a (doubling) cooldown — that
    // reconnect replays the gap from the server's ring buffer, or falls
    // to the replay_truncated → /history resync floor once the gap has
    // outgrown it.  Either path is lossless for committed turns.
    const now = Date.now();
    // Escalate the cooldown when trips recur; reset to base only after a
    // genuine quiet gap.  Keyed off _lastDegradedAt (a timestamp), NOT
    // _overflowTimes — this method clears that array below, so keying the
    // reset off it would restart the ladder on the next storm's first
    // overflow and the doubling (15→30→60→120s) would never take effect.
    const step = degradedCooldownStep(
      this._degradedCooldownMs,
      this._lastDegradedAt,
      now,
      DEGRADED_COOLDOWN_BASE_MS,
      DEGRADED_COOLDOWN_MAX_MS,
      DEGRADED_COOLDOWN_RESET_MS,
    );
    this._lastDegradedAt = now;
    this._degradedCooldownMs = step.nextCooldownMs;
    this._overflowTimes.length = 0;
    this.disconnectSSE(); // also cancels any earlier degraded timer
    this.statusBarEl.classList.add("ws-sb-disconnected");
    this._sbTokens.textContent = "Connection is slow — catching up…";
    const cooldown = step.cooldown;
    this._degradedTimer = setTimeout(() => {
      this._degradedTimer = null;
      if (document.hidden) {
        // Reopening into a throttled hidden tab would overflow again —
        // defer to the visibilitychange show edge instead.
        this._hiddenDisconnect = true;
        return;
      }
      if (this.wsId) this.connectSSE(this.wsId);
    }, cooldown);
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
    // Full-reload cleanup (NOT in disconnectSSE — transport-only reconnects
    // must preserve these): a stale quiesce queue would wedge the new load's
    // events behind a flush that never comes, stale agent tracking points at
    // the DOM this load is about to replace, and a pending truncated-resync
    // is superseded by the full refetch below.
    this._replayQueue = null;
    this._clearAgentTracking();
    this._pendingTruncatedResync = false;
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
    if (token !== undefined && token !== this._historyLoadToken) {
      this._endReplayQuiesce(token);
      return;
    }
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
      // replayHistory. No client-side normalisation.  The quiesce release
      // rides a finally so a loud replay throw (deliberately uncaught, see
      // above) can't strand the event queue and wedge the pane.
      try {
        this.replayHistory(data.messages || []);
      } finally {
        this._endReplayQuiesce(token);
      }
    } else {
      // Failure path never reaches replayHistory — reset the streaming refs
      // here too, or the flushed backlog and resumed live events would paint
      // into the subtree clear_ui already wiped.
      this._resetStreamingRefs();
      this.showEmptyState();
      this._endReplayQuiesce(token);
    }
  }

  _beginReplayQuiesce(token) {
    // Arm the handleEvent queue for a full re-render (clear_ui /
    // replay_truncated).  Token-owned: a newer load's quiesce replaces this
    // one wholesale — events queued before the newer snapshot was fetched
    // are covered by that snapshot, so dropping them is lossless.
    this._replayQueue = { token: token, events: [] };
  }

  _endReplayQuiesce(token) {
    const q = this._replayQueue;
    if (!q || q.token !== token) return;
    this._replayQueue = null;
    // Replay the backlog in arrival order.  A queued clear_ui re-arms the
    // quiesce mid-flush and the remainder queues behind ITS rebuild.  Each
    // dispatch is guarded like onmessage: one bad event must not drop the
    // rest of the backlog.
    for (const evt of q.events) {
      try {
        this.handleEvent(evt);
      } catch (err) {
        console.error("interactive: queued event replay failed", err);
      }
    }
  }

  _clearAgentTracking() {
    // Release task-agent bookkeeping ahead of (or after) a full rebuild.
    // Entries left in _agentCards would pin every replaced card subtree as
    // reachable detached DOM — unbounded growth across an hours-long
    // session's rewinds/compaction re-syncs — and a stale _agentOrphans
    // grace timer would escape buffered steps into the rebuilt pane.
    if (this._agentCards) this._agentCards.clear();
    if (this._agentOrphans) {
      for (const entry of this._agentOrphans.values()) {
        if (entry.timer != null) clearTimeout(entry.timer);
      }
      this._agentOrphans.clear();
    }
    // The row/stream lookup caches share this exact lifecycle (entries are
    // DOM refs into the subtree being replaced) — drop them together.
    if (this._toolRowIndex) this._toolRowIndex.clear();
    if (this._streamElIndex) this._streamElIndex.clear();
  }

  _toolRow(callId) {
    // O(1) call_id → .conv-row resolution with a self-healing cache: a hit
    // is validated for liveness (isConnected + id match) so a row replaced
    // by the pending→resolved upgrade or a batch rebuild falls back to one
    // scoped query and re-caches.  The old per-event attribute-selector
    // scan walked the whole transcript — O(N) per tool event.
    if (!callId) return null;
    let row = this._toolRowIndex.get(callId);
    if (row && row.isConnected && row.dataset.callId === callId) return row;
    row = this.messagesEl.querySelector(
      '.conv-row[data-call-id="' + CSS.escape(callId) + '"]',
    );
    if (row) this._toolRowIndex.set(callId, row);
    else this._toolRowIndex.delete(callId);
    return row;
  }

  _streamEl(callId) {
    // Same cache discipline as _toolRow for the per-tool streaming <pre> —
    // resolved on every tool_output_chunk, the chattiest event in an agent
    // session.
    if (!callId) return null;
    let el = this._streamElIndex.get(callId);
    if (el && el.isConnected && el.dataset.callId === callId) return el;
    el = this.messagesEl.querySelector(
      '.tool-output-stream[data-call-id="' + CSS.escape(callId) + '"]',
    );
    if (el) this._streamElIndex.set(callId, el);
    else this._streamElIndex.delete(callId);
    return el;
  }

  handleEvent(evt) {
    // Guard: drop events that belong to a different workstream.
    // This prevents cross-contamination during tab switches and reconnects.
    if (evt.ws_id && evt.ws_id !== this.wsId) return;
    // While a clear_ui / replay_truncated rebuild is in flight, live events
    // must not paint into a DOM the imminent replaceChildren() will wipe —
    // anything painted in the [snapshot-fetch → rebuild] window is lost with
    // no redelivery (the re-render callers never rewind _lastEventId).  Queue
    // them; _endReplayQuiesce replays the backlog once the rebuild lands.
    if (this._replayQueue) {
      this._replayQueue.events.push(evt);
      return;
    }
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

      case "stream_end": {
        if (this._cancelTimeout) {
          clearTimeout(this._cancelTimeout);
          this._cancelTimeout = null;
        }
        if (this._forceTimeout) {
          clearTimeout(this._forceTimeout);
          this._forceTimeout = null;
        }
        // A live in-progress compaction card at stream_end means a FORCE
        // stop abandoned the compaction worker (the lifecycle wrapper
        // otherwise always retires the card with an end event before any
        // stream_end can follow) — remove it now instead of leaving a
        // frozen bar until the abandoned worker notices at its next
        // checkpoint.
        resetCompactionHolder(this._compaction);
        // Reset the segment state BEFORE the finalize render, and guard the
        // render with a plain-text fallback (mirrors coordinator.js).  With
        // the old order a finalize throw skipped these clears, so every
        // later content delta appended into the poisoned buffer/bubble and
        // no new assistant segment ever painted — the permanent-wedge shape
        // of "output stops rendering while the backend stays healthy".
        const doneBodyEl = this.currentAssistantBodyEl;
        const doneBuffer = this.contentBuffer;
        this.currentAssistantBodyEl = null;
        this.currentAssistantEl = null;
        this.currentReasoningEl = null;
        this.contentBuffer = "";
        // Finalize the completed streaming segment's markdown.  This fires
        // per-segment (between tool calls), NOT per-turn.  Busy state is
        // managed by state_change events instead.
        if (doneBodyEl && doneBuffer) {
          try {
            streamingRenderFinalize(doneBodyEl, doneBuffer);
          } catch (err) {
            this._streamHealth.renderThrows += 1;
            console.warn(
              "interactive: streamingRenderFinalize failed (render-throw total " +
                this._streamHealth.renderThrows +
                ")",
              err,
            );
            doneBodyEl.textContent = doneBuffer;
          }
        }
        this.scrollToBottom(true);
        break;
      }

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
        // Track who holds the in-flight turn (acting user) so the send gate
        // can compare against this viewer. Present on busy transitions from a
        // shared workstream; absent on single-user / older backends (then the
        // gate simply never engages). Cleared when the turn settles.
        if (evt.state === "idle" || evt.state === "error") {
          this._actingUserId = null;
        } else if (evt.acting_user_id) {
          this._actingUserId = evt.acting_user_id;
        }
        if (evt.state === "idle" || evt.state === "error") {
          this.setBusy(false);
          this._attachRetryToLastAssistant();
          // Deferred replay_truncated re-sync: the truncation arrived while
          // a segment was streaming (refetching then would have detached the
          // live bubble), so repair the lost-event gap now that the turn is
          // settled and /history is complete.
          if (this._pendingTruncatedResync) {
            this._pendingTruncatedResync = false;
            const rsToken = this._historyLoadToken;
            this._beginReplayQuiesce(rsToken);
            this._refetchHistory(this.wsId, rsToken);
          }
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
        // A sub-agent's steps (parent_call_id set) nest in the task card.
        if (!this._routeAgentItems(evt.items, "pending")) {
          this.announceToolBlock(evt.items);
        }
        break;

      case "tool_info":
        // A sub-agent's auto-resolved step (policy / "Always" auto-approval, or
        // a policy block) arrives as a tool_info with parent_call_id stamped —
        // route it into the task card like tool_pending / approve_request,
        // instead of painting a duplicate top-level row.  Top-level tool_info
        // (no parent_call_id) falls through to the normal inline block.
        if (!this._routeAgentItems(evt.items, "info")) {
          this.showInlineToolBlock(evt.items, true);
        }
        break;

      case "approve_request":
        if (
          !this._routeAgentItems(
            evt.items,
            "approve",
            evt.judge_pending,
            this._cycleKey(evt),
          )
        ) {
          this.showInlineToolBlock(
            evt.items,
            false,
            evt.judge_pending,
            this._cycleKey(evt),
          );
        }
        break;

      case "intent_verdict":
        this.updateVerdictBadge(evt);
        break;

      case "output_warning":
        this.showOutputWarning(evt);
        break;

      case "approval_resolved":
        // Route to the resolved cycle; an event without a cycle_id
        // (pre-multi-cycle server) falls back to the oldest.
        this.resolveApproval(
          evt.approved,
          false,
          evt.feedback,
          true,
          evt.cycle_id || this._oldestCycleId(),
        );
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
          evt.preview,
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

      case "compaction":
        // Context-compaction lifecycle: start paints the in-progress card,
        // progress drives its bar, end swaps it for the result card (or a
        // failure notice).  See handleCompactionEvent.
        this.handleCompactionEvent(evt);
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
        this._paintModelChip();
        this.projectName = evt.project_name || "";
        this._paintProjectChip();
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
        this._beginReplayQuiesce(token);
        this.messagesEl.replaceChildren();
        this._resetStreamingRefs();
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
        // (re)connect.  The guard covers BOTH streaming targets — a
        // reasoning-only segment (currentReasoningEl without a content
        // bubble yet) is just as detachable as a content one.  Mid-stream
        // the resync is DEFERRED, not dropped: skipping outright left the
        // lost-event gap unrepaired for the rest of the session (no clean
        // reconnect may come for hours); the idle edge consumes the flag.
        if (!this.currentAssistantEl && !this.currentReasoningEl) {
          const rtToken = this._historyLoadToken;
          this._beginReplayQuiesce(rtToken);
          this._refetchHistory(this.wsId, rtToken);
        } else {
          this._pendingTruncatedResync = true;
        }
        break;

      case "stream_overflow":
        // The server poisoned this listener at its first queue overflow
        // and closes the stream right after this frame.  The frame is
        // id-less, so lastEventId still points below the gap and the
        // native EventSource reconnect replays it losslessly from the
        // ring buffer.  Count the close: a persistently slow consumer
        // trips the degraded catch-up instead of churning reconnects.
        this._noteStreamOverflow();
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
    const resetMic = () => {
      if (this._micBtn && !this._micDenied) {
        this._micBtn.disabled = !!this.busy;
        this._micBtn.classList.remove("is-busy");
      }
    };
    authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/speech-to-text/stream",
      { method: "POST", body: fd },
    )
      .then(async (r) => {
        if (!r.ok) {
          let msg = "Transcription failed";
          try {
            const body = await r.json();
            if (body && body.error) msg = body.error;
          } catch (_e) {
            /* non-JSON error body */
          }
          showToast(msg, "error");
          return;
        }
        // Stream transcript deltas into the composer as they arrive (first word
        // in ~0.3s) instead of waiting for the whole transcript.
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let started = false;
        let got = false;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          if (!chunk || !this.inputEl) continue;
          got = true;
          if (!started) {
            // Read the composer's value now (not before the await) so text the
            // user typed while transcribing isn't clobbered.
            const cur = this.inputEl.value || "";
            this.inputEl.value = cur
              ? cur.replace(/\s*$/, "") + " " + chunk
              : chunk;
            started = true;
          } else {
            this.inputEl.value += chunk;
          }
          // Drive the composer's auto-resize + send-enable listeners.
          this.inputEl.dispatchEvent(new Event("input", { bubbles: true }));
        }
        if (got) {
          if (this.inputEl) this.inputEl.focus();
          voiceAnnounce("Transcript added to message.");
        } else {
          showToast("No speech detected", "error");
        }
      })
      .catch((err) => {
        showToast(
          "Transcription failed: " + (err && err.message ? err.message : err),
          "error",
        );
      })
      .finally(resetMic);
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

  _resetStreamingRefs() {
    // Null every ref that can point into a wiped subtree, so the next event
    // creates fresh targets instead of writing invisibly into detached
    // nodes.  Called wherever the transcript DOM is (or is about to be)
    // replaced — replayHistory, the clear_ui immediate wipe, and the
    // refetch-FAILURE path (which shows the empty state without ever
    // reaching replayHistory; leaving refs stale there made the retried
    // generation's whole first segment stream into a detached bubble).
    this.currentAssistantEl = null;
    this.currentAssistantBodyEl = null;
    this.currentReasoningEl = null;
    this.contentBuffer = "";
    // In-progress compaction card: the transcript wipe orphaned it; live
    // events re-create it defensively (see handleCompactionEvent).
    resetCompactionHolder(this._compaction);
    // Approval cycles + announce shells point into the wiped subtree too;
    // the replayed history / detail snapshot re-registers live ones.
    this.approvalCycles = new Map();
    this.announcedBlocks = new Map();
    this._syncApprovalState();
    this._thinkingEl = null;
    this._retryHolderEl = null;
  }

  replayHistory(messages) {
    this.messagesEl.replaceChildren();
    // The rebuild just orphaned any in-flight streaming targets — reset them,
    // and release the agent-card/orphan maps whose entries now point at
    // replaced subtrees (detached-DOM retention).
    this._resetStreamingRefs();
    this._clearAgentTracking();
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
    // Task-agent recall: call_id -> card .conv-agent wrap, so the tool-result
    // branch can flip the card's done/error state from the task's own result
    // (mirroring the live appendToolOutput), not from sub-step errors.
    const agentCardWraps = {};
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
              "conv-batch " +
              (msg.tool_calls.length >= 2
                ? "conv-batch--parallel "
                : "conv-batch--solo ") +
              (wasDenied ? "conv-batch--denied" : "conv-batch--approved");
            block.appendChild(
              _convApprovalHead(
                batchKicker("done", msg.tool_calls.length),
                msg.tool_calls.map((tc) => ({ func_name: tc.name })),
              ),
            );
            msg.tool_calls.forEach((tc, idx) => {
              // Synthesize the live `item` shape from the stored tool_call so
              // replay renders the SAME .conv-row as the live path.
              const row = buildToolDiv(
                synthToolItem(tc),
                indexLabel(idx, msg.tool_calls.length),
              );
              // Verdict anchored to THIS row; replay verdicts are final.
              if (tc.verdict) {
                row.appendChild(
                  buildConvVerdict(tc.verdict, { judgePending: false }),
                );
              }
              block.appendChild(row);
              // Task-agent recall: rebuild the collapsible card under this row
              // from its stashed sub-trajectory (the /history `agent_steps`
              // overlay).  Absent ⇒ flat parent row (cold / not-retained).
              if (tc.agent_steps && tc.agent_steps.length) {
                const wrap = this._replayAgentCard(row, tc.agent_steps);
                if (tc.id) agentCardWraps[tc.id] = wrap;
              }
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
              insertChained(renderCollapsibleOutput(stripped, isToolError));
            }
          }
          // Replayed preview descriptor: chip only — a reload must never
          // auto-open panes for every historical preview (the live path's
          // focused auto-open already happened when it was current).  Error
          // turns keep their chip: a cancelled BATCH synthesizes an error
          // result for an open_preview whose content committed fine.
          if (msg.preview && !isDenied) {
            insertChained(
              buildPreviewChip(msg.preview, (d) => this._host.onPreview(d)),
            );
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
          // Task-agent recall: flip its card done/error from the task's OWN
          // result (matching the live appendToolOutput) — NOT from sub-step
          // errors, since a sub-tool can fail and the agent still synthesize.
          if (msg.tool_call_id && agentCardWraps[msg.tool_call_id]) {
            agentCardWraps[msg.tool_call_id].dataset.state = msg.is_error
              ? "error"
              : "done";
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
    // Remove the previous holder's action bar via the tracked ref — the old
    // whole-transcript ".msg.assistant .msg-actions" sweep was O(N) per
    // busy→idle edge.  At most one assistant bar exists (this method is its
    // only writer); a holder detached by a rebuild no-ops harmlessly.
    if (this._retryHolderEl) {
      const oldBar = this._retryHolderEl.querySelector(".msg-actions");
      if (oldBar) oldBar.remove();
      this._retryHolderEl = null;
    }
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
    // Walk backwards from the tail for the last assistant bubble — the
    // match is at (or near) the end of the transcript, so this touches a
    // handful of siblings instead of collecting all N assistant rows.
    let lastAssistant = this.messagesEl.lastElementChild;
    while (
      lastAssistant &&
      !(
        lastAssistant.classList.contains("msg") &&
        lastAssistant.classList.contains("assistant")
      )
    ) {
      lastAssistant = lastAssistant.previousElementSibling;
    }
    if (lastAssistant) {
      this._addRetryAction(lastAssistant);
      if (this._voiceRoles && this._voiceRoles.tts) {
        this._addTtsAction(lastAssistant);
      }
      this._retryHolderEl = lastAssistant;
    }
  }

  announceToolBlock(items) {
    const list = (items || []).filter(Boolean);
    if (!list.length) return;
    // Re-pin to the bottom only if we were already there — captured BEFORE the
    // block grows scrollHeight.  A tool batch is a tall one-shot append; if
    // isNearBottom() were measured after appendChild (as scrollToBottom does on
    // its own) the freshly-added height would read >80px from the new bottom,
    // so auto-follow would silently disengage at exactly tool-call time.  Token
    // streaming stays pinned without this because each append is sub-threshold.
    const stick = this.isNearBottom();
    // Key the shell by its call_id set.  A re-announce of the SAME batch
    // replaces its own shell; shells of OTHER batches stay — parallel
    // task agents announce concurrently and must not discard each other.
    // Bounded: shells whose upgrade never arrives (dropped SSE) are
    // evicted oldest-first past the cap so they can't pile up forever.
    const key = this._announceKey(items);
    const prior = this.announcedBlocks.get(key);
    if (prior) {
      prior.remove();
      this.announcedBlocks.delete(key);
    }
    while (this.announcedBlocks.size >= 8) {
      const oldestKey = this.announcedBlocks.keys().next().value;
      const oldest = this.announcedBlocks.get(oldestKey);
      if (oldest) oldest.remove();
      this.announcedBlocks.delete(oldestKey);
    }
    const block = document.createElement("div");
    block.className =
      "conv-batch " +
      (list.length >= 2 ? "conv-batch--parallel" : "conv-batch--solo");
    // Indeterminate region until the judge/gate resolves; the upgrade in
    // showInlineToolBlock clears aria-busy.
    block.setAttribute("aria-busy", "true");
    block.dataset.callIds = JSON.stringify(
      list.map((it) => it.call_id).filter(Boolean),
    );
    block.appendChild(
      _convApprovalHead(batchKicker("evaluating", list.length), list),
    );
    list.forEach((item, idx) => {
      block.appendChild(buildToolDiv(item, indexLabel(idx, list.length)));
      const verdict =
        item.judge_verdict || item.heuristic_verdict || item.verdict;
      if (verdict) {
        block.appendChild(buildConvVerdict(verdict, { judgePending: true }));
      }
    });
    this.announcedBlocks.set(key, block);
    this.messagesEl.appendChild(block);
    this._relinkAgentCards(list);
    this.scrollToBottom(stick);
    toolAnnounce(_toolAnnounceText(list));
  }

  _announceKey(items) {
    return JSON.stringify(
      (items || [])
        .map((it) => it && it.call_id)
        .filter(Boolean)
        .sort(),
    );
  }

  // Hand back the early-paint shell to upgrade in place if one exists for
  // this batch's call_id set, else null (caller builds fresh).  Deletes the
  // entry so the shell is consumed exactly once.  Keyed lookup (not a single
  // slot): parallel task agents keep several shells in flight, and taking
  // one must not disturb the others.
  _takeAnnouncedBlock(items) {
    const key = this._announceKey(items);
    const block = this.announcedBlocks.get(key);
    if (!block) return null;
    this.announcedBlocks.delete(key);
    return block;
  }

  showInlineToolBlock(items, autoApproved, judgePending, cycleId) {
    // Capture pin before _takeAnnouncedBlock/append change scrollHeight — see
    // announceToolBlock for why post-append measurement breaks here.
    const stick = this.isNearBottom();
    // Reuse the early-paint announce shell if it's for this batch (upgrade in
    // place); else build fresh.
    const announced = this._takeAnnouncedBlock(items);
    const block = announced || document.createElement("div");
    if (announced) announced.replaceChildren();
    block.removeAttribute("aria-busy");
    block.className =
      "conv-batch " +
      (items.length >= 2 ? "conv-batch--parallel" : "conv-batch--solo") +
      (autoApproved ? " conv-batch--auto" : "");
    if (!autoApproved) {
      block.setAttribute("role", "alertdialog");
      block.setAttribute("aria-label", "Tool approval required");
    }
    block.appendChild(
      _convApprovalHead(
        autoApproved
          ? batchKicker("done", items.length)
          : batchKicker("pending", items.length),
        items,
      ),
    );

    let glowRec = null;
    items.forEach((item, idx) => {
      block.appendChild(buildToolDiv(item, indexLabel(idx, items.length)));
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
        onApprove: () =>
          this.resolveApproval(
            true,
            false,
            this.getFeedback(block),
            false,
            cycleId,
          ),
        onDeny: () =>
          this.resolveApproval(
            false,
            false,
            this.getFeedback(block),
            false,
            cycleId,
          ),
        onAlways: () =>
          this.resolveApproval(
            true,
            true,
            this.getFeedback(block),
            false,
            cycleId,
          ),
      });
      block.appendChild(actions);
    }

    // Append BEFORE registering the approval cycle — the orphan-prune in
    // _syncApprovalState checks that no blockEls are connected
    // (!blockEls.some(el => el.isConnected)), so a freshly-built block
    // (not yet in the DOM) would be mistaken for an orphan and immediately
    // pruned if we registered before appending.
    if (!announced) this.messagesEl.appendChild(block);
    if (!autoApproved) {
      this._registerApprovalCycle(cycleId, [block], items);
      const fb = block.querySelector(".conv-feedback");
      // Focus the feedback field only for the FIRST (oldest) live cycle —
      // a sibling card arriving while the user is typing into another
      // cycle's field must not steal focus mid-word.
      if (this._oldestCycleId() === cycleId) {
        requestAnimationFrame(() => {
          if (fb) fb.focus();
        });
      }
    }
    this._relinkAgentCards(items);
    this.scrollToBottom(stick);
  }

  resolveApproval(approved, always, feedback, skipPost, cycleId) {
    // Resolve exactly ONE cycle — parallel task agents can have several
    // cards live; a decision must never bleed onto a sibling's.  No
    // cycleId (legacy caller) → the oldest.
    const id = cycleId || this._oldestCycleId();
    if (!id) return;
    const entry = this.approvalCycles.get(id);
    if (!entry) return; // already resolved (peer tab / server race) — idempotent
    this.approvalCycles.delete(id);

    // Capture pin before the status badge reflows the block — see
    // announceToolBlock.
    const stick = this.isNearBottom();

    entry.blockEls.forEach((el) => {
      const actions = el.querySelector(".conv-actions");
      if (actions) actions.remove();
    });
    const statusHost = entry.blockEls[0];
    if (statusHost) {
      statusHost.appendChild(
        buildConvStatus({ approved, always, feedback: feedback || "" }),
      );
      statusHost.classList.add(
        approved ? "conv-batch--approved" : "conv-batch--denied",
      );
    }

    this._syncApprovalState();
    if (!this.pendingApproval) this.inputEl.focus();

    // POST to server (skip when server already resolved, e.g. timeout).
    // cycle_id pins the decision to THIS round server-side; call_id
    // rides along as defense-in-depth (the server 409s a stale pair).
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
            cycle_id: id.startsWith("legacy:") ? null : id,
            call_id: entry.callIds[0] || null,
          }),
        },
      ).catch((err) => {
        this.addErrorMessage("Connection error: " + err.message);
      });
    }

    this.scrollToBottom(stick);
  }

  // --- Task-agent card: nest a sub-agent's sub-tool steps under its row -----
  // Child events (tool_pending / approve_request) carry parent_call_id; route
  // them into the task_agent row's collapsible body.  tool_result /
  // tool_output_chunk / intent_verdict / output_warning need no special-casing
  // — their handlers find the row by call_id anywhere under messagesEl,
  // including inside the card body.  Returns true when handled (the caller then
  // skips the top-level rendering path).
  _routeAgentItems(items, mode, judgePending, cycleId) {
    if (!items || !items.length) return false;
    const parentId = items[0] && items[0].parent_call_id;
    if (!parentId) return false;
    const card = this._ensureAgentCard(parentId);
    if (!card) {
      // Parent task_agent row isn't painted yet: under the 4-wide tool pool a
      // sub-tool's SSE event can be handled before its task_agent row commits.
      // Buffer the child step keyed by parent so it nests when the parent row
      // lands (see _flushAgentOrphans), instead of escaping to a top-level row
      // that looks like the main harness issued it.  An approval prompt is NOT
      // buffered — it must stay visible to unblock the gate — so "approve"
      // falls through to the top-level paint.
      if (mode !== "approve") {
        this._bufferAgentOrphan(parentId, items, mode);
        return true;
      }
      return false; // parent row not painted yet — fall back top-level
    }
    if (mode === "approve") {
      // Cards default to collapsed, but a pending approval is BLOCKING — it
      // can't hide behind the toggle or the turn stalls on a prompt the user
      // never sees.  Force the card open (sync aria with the data attribute).
      card.wrap.dataset.collapsed = "false";
      const toggle = card.wrap.querySelector(".conv-agent-toggle");
      if (toggle) toggle.setAttribute("aria-expanded", "true");
    }
    const stick = this.isNearBottom();
    const approveRows = [];
    items.forEach((item) => {
      if (!item || !item.parent_call_id) return;
      const escId = item.call_id ? CSS.escape(item.call_id) : "";
      let row = escId
        ? card.body.querySelector('.conv-row[data-call-id="' + escId + '"]')
        : null;
      if (!row) {
        row = buildToolDiv(item, "");
        card.body.appendChild(row);
      }
      if (mode === "approve") {
        const verdict =
          item.judge_verdict || item.heuristic_verdict || item.verdict;
        if (verdict) {
          row.appendChild(buildConvVerdict(verdict, { judgePending }));
        }
        const actions = buildConvActions({
          kbd: { approve: "y", deny: "n" },
          glowRec: verdict && verdict.recommendation,
          withFeedback: true,
          onApprove: () =>
            this.resolveApproval(
              true,
              false,
              this.getFeedback(row),
              false,
              cycleId,
            ),
          onDeny: () =>
            this.resolveApproval(
              false,
              false,
              this.getFeedback(row),
              false,
              cycleId,
            ),
        });
        row.appendChild(actions);
        approveRows.push(row);
      }
    });
    if (mode === "approve" && approveRows.length) {
      // Register this nested batch as its own approval cycle — the
      // backend gates each sub-agent batch independently, so buttons
      // must resolve THIS cycle, not "the" pending one (parallel task
      // agents can have several nested prompts live at once).
      this._registerApprovalCycle(cycleId, approveRows, items);
    }
    this._updateAgentLabel(card);
    this.scrollToBottom(stick);
    return true;
  }

  _ensureAgentCard(parentCallId) {
    // _toolRow cache: a busy task agent resolves its parent row once per
    // child event — the uncached scan was O(transcript) per step.
    const parentRow = this._toolRow(parentCallId);
    if (!parentRow) return null;
    if (!this._agentCards) this._agentCards = new Map();
    let card = this._agentCards.get(parentCallId);
    if (card) {
      if (parentRow.contains(card.wrap)) return card;
      if (!card.wrap.isConnected) {
        // Same-turn row rebuild: showInlineToolBlock's replaceChildren on the
        // pending->resolved upgrade detached our card.  Re-attach the SAME card
        // so its already-rendered steps survive the upgrade.
        parentRow.appendChild(card.wrap);
        return card;
      }
      // The cached card is still attached to a DIFFERENT (earlier) row — a
      // call_id reused across turns (some local providers recycle ids).  Fall
      // through to build a fresh card for THIS row rather than stealing the
      // prior agent's steps.  (Full cross-turn id correctness is the task-agent
      // id-consistency follow-up.)
    }
    card = buildAgentCardBody();
    card.wrap.dataset.state = "running";
    parentRow.appendChild(card.wrap);
    this._agentCards.set(parentCallId, card);
    return card;
  }

  _bufferAgentOrphan(parentId, items, mode) {
    // Hold a child step whose task_agent row hasn't painted yet, keyed by
    // parent, so it nests when the row lands (_flushAgentOrphans).  Bounded two
    // ways so a parent row that never arrives (an id-correlation mismatch, or
    // an agent aborted before its row painted) can't leak or hide steps: the
    // per-parent queue is capped (oldest dropped), and a grace timer escapes
    // the queue to a top-level row (_escapeAgentOrphans) so the steps stay
    // VISIBLE rather than buffered forever.
    if (!this._agentOrphans) this._agentOrphans = new Map();
    const entry = this._agentOrphans.get(parentId) || {
      queue: [],
      timer: null,
    };
    entry.queue.push({ items, mode });
    while (entry.queue.length > _AGENT_ORPHAN_CAP) entry.queue.shift();
    if (entry.timer == null) {
      entry.timer = setTimeout(
        () => this._escapeAgentOrphans(parentId),
        _AGENT_ORPHAN_GRACE_MS,
      );
    }
    this._agentOrphans.set(parentId, entry);
  }

  _flushAgentOrphans(parentIds) {
    // Drain buffered child steps for the just-painted parents (their card now
    // exists) — targeted by parentId so an unrelated tool paint doesn't replay
    // every parent's queue.  A step that still can't nest re-buffers (with a
    // fresh grace timer) for the next paint / escape.
    if (!this._agentOrphans || !this._agentOrphans.size) return;
    parentIds.forEach((pid) => {
      const entry = this._agentOrphans.get(pid);
      if (!entry) return;
      this._agentOrphans.delete(pid);
      if (entry.timer != null) clearTimeout(entry.timer);
      entry.queue.forEach((o) => this._routeAgentItems(o.items, o.mode));
    });
  }

  _escapeAgentOrphans(parentId) {
    // The parent row never painted within the grace window — render the
    // buffered steps at top-level so they stay visible (the pre-buffer
    // behaviour) instead of vanishing.  Batched per mode to avoid the
    // announce-shell churn of one paint per step.
    const entry = this._agentOrphans && this._agentOrphans.get(parentId);
    if (!entry) return;
    this._agentOrphans.delete(parentId);
    const pending = [];
    const info = [];
    entry.queue.forEach((o) =>
      (o.mode === "info" ? info : pending).push(...o.items),
    );
    if (pending.length) this.announceToolBlock(pending);
    if (info.length) this.showInlineToolBlock(info, true);
  }

  _relinkAgentCards(items) {
    // After a top-level tool row is (re)painted: re-attach any agent card the
    // rebuild detached, then drain buffered child steps for the parents that
    // just painted.  Closes the parallel-pool ordering race and keeps a task
    // agent's nested card intact across its parent's pending->resolved upgrade.
    const paintedIds = [];
    (items || []).forEach((it) => {
      if (!it || !it.call_id) return;
      paintedIds.push(it.call_id);
      if (this._agentCards && this._agentCards.has(it.call_id)) {
        this._ensureAgentCard(it.call_id);
      }
    });
    if (paintedIds.length) this._flushAgentOrphans(paintedIds);
  }

  _updateAgentLabel(card) {
    const n = card.body.querySelectorAll(".conv-row").length;
    card.label.textContent = n === 1 ? "1 step" : n + " steps";
  }

  _replayAgentCard(row, steps) {
    // Rebuild a finished task agent's card from its recalled step items
    // (/history `agent_steps`), mirroring the live _routeAgentItems nesting so a
    // reload looks identical: a collapsed body of step rows, each with its
    // result.  Recall is terminal — no live approval affordances.  Card state
    // defaults to "done"; the caller flips it to "error" from the task's OWN
    // result (the role==="tool" branch), matching the live path — sub-step
    // errors don't decide it (an agent can recover and synthesize fine).
    const card = buildAgentCardBody();
    steps.forEach((step) => {
      card.body.appendChild(buildToolDiv(synthToolItem(step), ""));
      const out = stripAnsi(String(step.output || "")).trim();
      if (out) {
        card.body.appendChild(renderCollapsibleOutput(out, !!step.is_error));
      }
    });
    card.wrap.dataset.state = "done";
    this._updateAgentLabel(card);
    row.appendChild(card.wrap);
    return card.wrap;
  }

  appendToolOutput(callId, name, output, isError, preview) {
    // Capture pin before the streamEl removal + result insertion change
    // scrollHeight — see announceToolBlock.  The result block is the other
    // tall one-shot append in the tool flow (up to 10 lines before collapse).
    const stick = this.isNearBottom();
    // A task_agent's OWN result completing flips its card running -> done/error
    // (child sub-tool results carry namespaced ids, never keys of _agentCards).
    // The entry deliberately SURVIVES the result: a late child event (SSE
    // replay overlap) re-entering _ensureAgentCard with no Map entry would
    // build a duplicate empty card beside the finished one.  Entries hold
    // attached DOM (not a leak); the detached-retention hazard is rebuilds,
    // which _clearAgentTracking covers in replayHistory.
    if (this._agentCards && this._agentCards.has(callId)) {
      this._agentCards.get(callId).wrap.dataset.state = isError
        ? "error"
        : "done";
    }
    let target = this._toolRow(callId);
    if (!target) {
      // A minted sub-agent child id ("<parent>::r{run}s{step}::<id>") whose row hasn't
      // nested yet must NOT graft its output onto the last top-level batch row
      // — that mislabels a sub-tool's result as a main-harness tool's.  Its row
      // arrives via the orphan flush; skip until then.
      if (callId && callId.includes("::")) return;
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
    let streamEl = this._streamEl(callId);
    if (!streamEl) {
      const next = target.nextElementSibling;
      if (next && next.classList.contains("tool-output-stream")) {
        streamEl = next;
      }
    }
    if (streamEl) {
      streamEl.remove();
      if (callId) this._streamElIndex.delete(callId);
    }

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
        this.scrollToBottom(stick);
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
        this.scrollToBottom(stick);
        return;
      }
    }

    // The media / MCP-error dispatch above both early-return, so by here it's
    // the plain-output path — the shared helper applies (test_app_js pins that
    // tryParseMcpError precedes this renderer call).
    const out = renderCollapsibleOutput(stripped, isError);

    // Mark the parent approval block as errored
    if (
      isError &&
      parentBlock &&
      !parentBlock.classList.contains("conv-batch--denied")
    ) {
      parentBlock.classList.add("conv-batch--error");
      appendToolErrorBadge(parentBlock);
    }

    target.after(out);
    // Preview descriptor (open_preview): chip in the transcript always; the
    // pane auto-opens only while THIS pane is the user's focus — a
    // backgrounded session must not commandeer the split, and the chip
    // remains the deliberate reopen for that case (and for replay).
    if (preview && !isError) {
      const chip = buildPreviewChip(preview, (d) => this._host.onPreview(d));
      out.after(chip);
      if (this._host.isFocused(this)) this._host.onPreview(preview);
    }
    this.scrollToBottom(stick);
  }

  sendMessage() {
    const text = this.inputEl.value.trim();
    if (!text) return;

    if (text.startsWith("/")) {
      if (this.busy) {
        // Was a silent return — say why nothing happened.
        this.addInfoMessage("Session is busy — commands can't run mid-turn.");
        return;
      }
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
      })
        .then((r) =>
          r.json().then(
            (b) => b || {},
            () => ({}),
          ),
        )
        .then((body) => {
          // /compact dispatched onto an already-busy worker reports
          // {status: "busy"} — surface it (the optimistic busy guard
          // above can lose that race).
          if (body.status === "busy") {
            this.addInfoMessage(
              body.error || "Session is busy — try again shortly.",
            );
          }
        })
        .catch(() => {});
      // Echo as a command chip, not addUserMessage — a slash command is
      // control-plane input, not a conversational user turn.
      this.addCommandEcho(text);
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

    // Bound the send POST with an AbortController + ~15s timeout (mirrors
    // composer_queue.js _deleteRequest) so a wedged proxied node can't leave a
    // pre-bind-dismissed card frozen forever — bind/promote/remove only run
    // off this response, so the .catch must always eventually fire.
    const sendCtrl =
      typeof AbortController === "function" ? new AbortController() : null;
    const sendInit = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        attachment_ids: snap.attachment_ids,
      }),
    };
    let sendTimer = null;
    if (sendCtrl) {
      sendInit.signal = sendCtrl.signal;
      // Long bound while a compaction card is live (the send is parked
      // server-side for the command window); wedged-node default
      // otherwise — see sendAbortMs.
      sendTimer = setTimeout(
        () => sendCtrl.abort(),
        sendAbortMs(this._compaction),
      );
      // An × on the queued bubble BEFORE the response arrives (pre-bind)
      // must also kill the parked POST — otherwise the dismissed message
      // dispatches anyway when the command window closes, minutes later.
      if (queuedEl) queuedEl._sendAbort = () => sendCtrl.abort();
    }
    let sendReq = authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/send",
      sendInit,
    );
    if (sendTimer) sendReq = sendReq.finally(() => clearTimeout(sendTimer));
    sendReq
      .then((r) => {
        // A rejected send (4xx/5xx) carries {error}, not {status}; without
        // this guard it falls through to the "unknown status" branch and gets
        // promote()'d — a server-refused message shown as delivered (with a
        // false "already sent" toast if it was dismissed). Route it to the
        // .catch (removes the bubble + shows the error) instead, surfacing the
        // server's {error} text ("No session", a rate-limit reason, etc.)
        // rather than a bare status code. A wedged proxy can answer non-JSON
        // (502/504 HTML); the parse-failure arm falls back to the status code
        // so that can't surface as an "Unexpected token <" error.
        if (!r.ok) {
          // 409 = the server-side cross-user interjection block (another
          // participant's turn is in flight). Convert to a handled status
          // object so it routes to the clean branch below instead of the
          // generic "Connection error" catch — this is the reactive fallback
          // for the race where the button wasn't yet disabled.
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
        } else if (data.status === "cross_user_interjection") {
          // Another participant's turn is in flight; the server refused the
          // interjection so it can't run under their credentials or be
          // misattributed. The send gate normally disables the button first;
          // this handles the race where the click beat the state_change.
          if (queuedEl) this.queue.remove(queuedEl);
          this.addErrorMessage(
            data.error ||
              "Another participant's turn is in progress. Wait for it to " +
                "finish, then send your message.",
          );
          if (!isBusy) this.setBusy(false);
        } else {
          // Unknown / "ok" status (e.g. the stale-busy race: the client
          // optimistically queued but the server ran the send on a fresh
          // worker). Settle the optimistic bubble as a normal sent message
          // so a pre-bind × can't strand it in the dismissing state;
          // promote() notifies "already sent" if it was dismissed.
          if (queuedEl) this.queue.promote(queuedEl);
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

function _tryPrettyJson(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  return redactCredentials(JSON.stringify(obj, null, 2));
}

// ---------------------------------------------------------------------------
//  HLS lazy-loader + click-to-play (lifted from the standalone app.js so
//  console-hosted panes activate media too).  Follows the mermaid.js
//  lazy-load pattern in /shared/renderer.js: the vendor is fetched by absolute
//  /shared/ URL on first use, so it resolves in BOTH the standalone server and
//  the console (where /shared is mounted at the root and node-proxied panes
//  also reach it via /node/{id}/shared/).
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

function _activatePlayer(btn) {
  const url = btn.dataset.streamUrl;
  const hlsUrl = btn.dataset.hlsUrl;
  const isAudio = btn.dataset.audioOnly === "true";
  const directStream = btn.dataset.directStream === "true";

  const player = document.createElement(isAudio ? "audio" : "video");
  player.controls = true;
  player.autoplay = true;
  player.className = "media-player";

  // Held so the error handler can tear the instance down before the player
  // node is replaced — otherwise its listeners/loader timers run detached.
  let hls = null;

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
    hls = new Hls();
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
    if (hls) {
      hls.destroy();
      hls = null; // media error events can repeat — never double-destroy
    }
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
    retry.appendChild(document.createTextNode("▶ Retry"));

    const container = document.createElement("div");
    container.appendChild(err);
    container.appendChild(retry);
    player.replaceWith(container);
  });

  btn.replaceWith(player);
}

// Activate a clicked/Enter-pressed play button: show the loading affordance,
// then ensure hls.js is loaded before swapping in the player when the source
// needs it.  The pane wires this from a root-scoped this.el listener.
function activateMediaPlayButton(btn) {
  btn.disabled = true;
  const labelEl = btn.querySelector("span:last-child");
  if (labelEl) {
    labelEl.textContent = "Loading…";
  } else {
    btn.textContent = "▶ Loading…";
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
  if (code === "mcp_refresh_unavailable") {
    // Soft, retryable state — a transient refresh failure, not a hard denial.
    return "transient";
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
    case "mcp_refresh_unavailable":
      return "Temporarily unavailable";
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

function buildToolDiv(item, indexLabel) {
  // Tool row for the converged card: the shared call line (name + auto tag)
  // plus the bash `$ cmd` / diff preview.  data-func-name is stamped for the
  // append-time finders (buildConvRow already stamps data-call-id/-tool-name).
  // indexLabel ("1/3") numbers parallel rows so the head's "+ N more" reads
  // as a label, not hidden calls (mirrors the coordinator).
  const row = buildConvRow(item, indexLabel ? { indexLabel } : {});
  row.dataset.funcName = item.func_name || "";
  row.appendChild(buildConvCmd(item));
  return row;
}

// Synthesize the live `item` shape ({func_name, call_id, header}) from a stored
// tool_call ({name, id, arguments}) so /history replay AND task-agent card
// recall render the SAME .conv-row as the live path.  Header derivation mirrors
// the live serialize: bash shows the command; other tools show `key: value`
// lines, values clipped at 80 chars; unparseable args fall back to a raw clip.
function synthToolItem(tc) {
  tc = tc || {};
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
        let valStr = val === null || val === undefined ? "null" : String(val);
        if (valStr.length > 80) valStr = valStr.substring(0, 77) + "...";
        parts.push(keys[k] + ": " + valStr);
      }
      header = parts.join("\n");
    }
  } catch (e) {
    header = String(tc.arguments || "").substring(0, 100);
  }
  return { func_name: tc.name, call_id: tc.id || "", header };
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
  out.textContent = redactCredentials(stripped);
  return out;
}

// Shared recipe: render tool output and auto-collapse past 10 lines.  The one
// source for the live appendToolOutput's plain-output path, the /history
// tool-result replay, and the task-agent card recall — so the collapse
// threshold can't drift between them.
function renderCollapsibleOutput(stripped, isError) {
  const out = renderToolOutput(stripped, isError);
  if (out.textContent.split("\n").length > 10) makeCollapsible(out);
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
  raw.textContent = _tryPrettyJson(rawJson) || redactCredentials(rawJson);
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

  // Render the Connect / Re-consent button only when the dispatcher actually
  // supplied a per-server consent URL. An "actionable"-category code with no
  // consent_url means there is no per-server consent flow for this server —
  // sign-in passthrough (oauth_obo) mints from the user's Turnstone sign-in and
  // is deliberately absent from the Settings connections list and rejected by
  // /start — so a button here would dead-end ("no consent URL; open Settings"
  // pointing at a panel with nothing to connect). In that case the honest
  // remedy is the detail text (sign in again / ask your administrator), so we
  // show the card without a broken affordance.
  //
  // This never wrongly hides a needed button for oauth_user: the backend
  // invariant is that _build_consent_url returns a /v1/api/mcp/oauth/start URL
  // for EVERY oauth_user row and None only for non-oauth_user auth types, so an
  // oauth_user actionable error always carries a valid consent_url and always
  // renders its button. The removed click-time "open Settings" fallback guarded
  // a producer path that that invariant makes unreachable.
  const consentUrl = err.consent_url;
  const hasConsentAffordance =
    typeof consentUrl === "string" &&
    consentUrl.startsWith("/v1/api/mcp/oauth/start");
  if (category === "actionable" && hasConsentAffordance) {
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
      // Defence-in-depth: the render gate already proved the prefix, but
      // re-check at click time — a non-prefix value would indicate producer
      // drift or a compromised dispatcher, and window.open("javascript:...")
      // would be catastrophic. Never rely on the producer-side guarantee alone.
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
  pre.textContent = _tryPrettyJson(rawJson) || redactCredentials(rawJson);
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
  // Terminal-failure tracking.  Native EventSource auto-reconnect (plus the 5s
  // CLOSED-state recovery below) covers transient drops — but a session that is
  // GONE from its node (closed/evicted, node restarted, re-homed) 404s every
  // reconnect forever.  After 3 consecutive CLOSED checks the controller gives
  // up: stream closed, timers dropped, status bar terminal, `opts.onDead()`
  // fired ONCE so the shell can paint its reconnect affordance.  Recovery is
  // the shell's revive path (re-resolve node + POST /open + rebuild) — never
  // automatic, so a deliberately-closed session is not resurrected by a timer.
  // A successful stream open resets the counter (host.onStreamOpen).
  let dead = false;
  let failCount = 0;

  const giveUp = function () {
    if (dead) return;
    dead = true;
    failCount = 0;
    if (recoverTimer) {
      clearTimeout(recoverTimer);
      recoverTimer = null;
    }
    // Invalidate any in-flight history load: its .finally would otherwise
    // reopen a stream for a session we just declared dead.
    pane._historyLoadToken = (pane._historyLoadToken || 0) + 1;
    pane.disconnectSSE();
    // Detach the visibility handler and clear the hide-close marker: a dead
    // controller must NOT be resurrected by a tab-visibility change.  Without
    // this, a tab hidden BEFORE the give-up (which set _hiddenDisconnect) would,
    // on return, fire _onVisibilityChange and connectSSE() the closed ws —
    // reopening a stream the shell has declared gone and 404-reconnecting it
    // forever (onStreamError early-returns when dead, so nothing stops it).
    // Idempotent with destroy()'s own _removeVisibilityHandler call.
    pane._removeVisibilityHandler();
    // Terminal wording — the transient error path says "Reconnecting…".
    pane.statusBarEl.classList.add("ws-sb-disconnected");
    pane._sbTokens.textContent = "Disconnected";
    if (typeof opts.onDead === "function") {
      try {
        opts.onDead();
      } catch (e) {
        console.error("interactive pane: onDead callback failed", e);
      }
    }
  };

  const host = {
    // Only the visible tab steals focus — never yank the caret into a
    // backgrounded pane mid background-replay.
    isFocused() {
      return active;
    },
    // Native EventSource auto-reconnect handles transient drops (connectSSE
    // deliberately does not close the source on error).  Guard the terminal
    // case: if the source is genuinely CLOSED after a beat, open a fresh
    // same-ws stream.  No global ws-list refetch — a console pane owns one ws.
    // Three consecutive CLOSED beats = the session is gone, not flaky: give up
    // (a dead session would otherwise be 404-polled every 5s indefinitely).
    onStreamError(pane) {
      if (dead) return;
      if (recoverTimer) clearTimeout(recoverTimer);
      recoverTimer = setTimeout(() => {
        recoverTimer = null;
        if (document.hidden) {
          // Don't reopen an EventSource into a hidden (throttled) tab —
          // that re-creates the slow-consumer overflow close-on-hide
          // exists to prevent.  Mark it so the visibilitychange show edge
          // owns the reconnect (a hide close normally set this already;
          // set it defensively for the error-before-hide ordering).
          pane._hiddenDisconnect = true;
          return;
        }
        if (
          pane.evtSource &&
          pane.evtSource.readyState !== EventSource.CLOSED
        ) {
          return; // native reconnect is still working the problem
        }
        failCount += 1;
        if (failCount >= 3) giveUp();
        else pane.connectSSE(pane.wsId);
      }, 5000);
    },
    // Stream (re)opened — the session is reachable again; reset the give-up
    // counter so unrelated future blips get a fresh allowance.
    onStreamOpen() {
      failCount = 0;
    },
    // The --skip-permissions banner lands in the pane's own slim header.
    warningTarget(pane) {
      return pane.messagesEl;
    },
    // MCP re-consent surfaces inline in the pane card; the STANDALONE additionally
    // drives a Manage-row attention badge — bridged through the TS_APP seam so the
    // shared factory stays deployment-agnostic (the console doesn't define the
    // hook, so this stays a no-op there).
    onConsentDetected(server) {
      if (
        window.TS_APP &&
        typeof window.TS_APP.onConsentDetected === "function"
      ) {
        window.TS_APP.onConsentDetected(server);
      }
    },
    // Preview descriptors open the shell's preview pane beside this one.
    // Bridged through the TS_SHELL seam (mountShell defines it) with THIS
    // pane's transport context attached, so the preview pane fetches blob
    // content from the same workstream through the same node proxy the
    // session streams from.
    onPreview(descriptor) {
      if (
        window.TS_SHELL &&
        typeof window.TS_SHELL.openPreview === "function"
      ) {
        window.TS_SHELL.openPreview(descriptor, { base: base, wsId: wsId });
      }
    },
  };

  const pane = new Pane(wsId, {
    base,
    host,
    onClose: opts.onClose,
  });
  root.appendChild(pane.el);

  return {
    wsId: wsId,
    pane: pane,
    // The transport base this controller talks through ("" local, "/node/{id}"
    // proxied) — the shell's tab-menu verbs aim at the SAME backend the pane
    // streams from.
    base: base,
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
    // Re-auth fan-out: reconnect the stream.  A dead controller stays dead —
    // recovery is the shell's revive path (which may need a different node).
    onLogin() {
      if (connected && !dead) pane._loadHistoryThenConnect(wsId);
    },
    // Terminal-state surface for the shell: `isDead()` gates revive-vs-connect
    // on activate/reopen; `markDead()` lets Tier-1 lifecycle (ws_closed) stop
    // the retry loop NOW instead of after three failed beats.
    isDead() {
      return dead;
    },
    markDead: giveUp,
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
      // The document-level visibilitychange listener holds a strong ref
      // to the pane — leaving it registered would both leak the pane and
      // let a show edge reopen a stream for a destroyed controller.
      pane._removeVisibilityHandler();
      // Terminal cleanup that transport-only reconnects must NOT do (see
      // disconnectSSE): cancel orphan grace timers so a post-destroy escape
      // can't paint into the detached pane / shared announcer, release the
      // card maps, and stop observing the detached scroller.
      pane._clearAgentTracking();
      pane._replayQueue = null;
      if (pane._resizeObs) {
        pane._resizeObs.disconnect();
        pane._resizeObs = null;
      }
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
