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
  buildSystemNudgeMarker,
  buildConvBatchShell,
  buildConvBatchDisclosure,
  buildConvRow,
  buildConvCmd,
  attachConvFullCall,
  captureConvFullCalls,
  restoreConvFullCalls,
  convKeyBelongsToControl,
  convFocusIsFree,
  buildConvResultExpand,
  buildConvVerdict,
  buildConvWarning,
  buildConvActions,
  buildConvStatus,
  buildAgentCardBody,
  formatAgentContextTokens,
  agentContextIsWarning,
  buildPreviewChip,
  buildFileChip,
  clearConvVerdictPending,
  convBatchSummaryText,
  isConvVerdictCompactBlocker,
  markConvRowResultSettled,
  createReasoningActivity,
  setConvBatchExpanded,
  setToolOutputReviewState,
  batchKicker,
  indexLabel,
} from "./conversation.js";
import {
  canAutoFoldTranscriptBatch,
  getTranscriptPresentation,
  preserveTranscriptBottomPin,
  registerTranscriptScroller,
} from "./transcript_presentation.js";
import { redactCredentials, tryPrettyJson } from "./redact_credentials.js";
import { tryParseMcpError, buildMcpErrorEmbed } from "./mcp_error.js";
import { authFetch, authGeneration, hasScope, hasPermission } from "./auth.js";
import { showToast } from "./toast.js";
import { Composer } from "./composer.js";
import {
  buildToolImages,
  createAttachmentController,
  kindIcon,
} from "./composer_attachments.js";
import {
  acceptUserTurnEvent,
  clientSendMaySettleForViewer,
  createQueueController,
  mergeRejectedComposerText,
  mintClientSendId,
  parsePriority,
  postAndSettleSend,
  settleAcceptedClientSends,
  viewerUserId,
} from "./composer_queue.js";
import { StatusBar } from "./status_bar.js";
import { mountConversationScroll } from "./conversation_scroll.js";
import { streamingRender, streamingRenderFinalize } from "./renderer.js";
import {
  buildMsgCopyButton,
  buildMsgRetryButton,
  ensureMsgActionsBar,
  findMsgActionsBar,
} from "./copy_actions.js";
import { makeAnnouncer, operatorSourceLabel } from "./utils.js";
import {
  acceptedToolEventAlreadyRendered,
  enqueueToolOccurrence,
  indexLatestToolRow,
  recordAcceptedToolEvent,
  shiftToolOccurrence,
} from "./tool_projection.js";
import { STREAM_STATUS_TEXT, WorkstreamSync } from "./workstream_sync.js";

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
// color/icon. Errors go through showToast (already a live region).
const voiceAnnounce = makeAnnouncer();

// Visually-hidden POLITE live region for the tool-call early paint
// (tool_pending) so screen-reader users hear a committed call land — and that
// they can Stop it — even though messagesEl is flipped to aria-live="off"
// during the token streaming that immediately precedes the call.  Polite (not
// assertive): a committed call is worth surfacing but isn't the action-required
// human gate, which keeps its own assertive announcement.  Separate region
// from the voice one so the two never clobber each other.
const _toolStatus = makeAnnouncer();
// The wrapper earns its keep with the empty-message guard —
// _toolAnnounceText returns "" for a batch with no named tools.
function toolAnnounce(msg) {
  if (!msg) return;
  _toolStatus(msg);
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
  // Where the ``--skip-permissions`` banner lands.
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
  onFiles() {},
};

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
    this.el = null;
    this.messagesEl = null;
    this.inputEl = null;
    this.sendBtn = null;
    this.stopBtn = null;
    this.currentAssistantEl = null;
    this.currentReasoningEl = null;
    this.contentBuffer = "";
    this.busy = false;
    // Provenance of the current busy=true (see setBusy): "server" |
    // "optimistic" | null when idle.
    this.busySource = null;
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
    this.model = "";
    this.modelAlias = "";
    this.projectName = "";
    // The last status event, repainted when the stream is live again.
    this._lastStatusEvt = null;
    // Hot-path caches — all invalidated by _clearAgentTracking/replayHistory.
    // The Maps make per-event row/stream lookups O(1) instead of
    // whole-transcript attribute-selector scans.
    this._reasoningActivity = createReasoningActivity();
    // Compaction lifecycle holder for the shared reducer
    // (conversation.applyCompactionEvent); `card` is the in-progress card
    // between start and end, nulled wherever the transcript DOM is wiped.
    this._compaction = { card: null, cid: null };
    // Rendered-event-id dedup for system turns and compaction markers
    // (/history repaint vs live/replayed event — whichever renders first
    // wins).  replayHistory assigns a FRESH Set on every transcript wipe
    // so pre-wipe ids can't suppress re-painted rows; this is the
    // first-load init.
    this._renderedSystemEventIds = new Set();
    this._renderedUserEventIds = new Set();
    this._renderedToolEventIds = new Set();
    this._retryHolderEl = null;
    this._toolRowIndex = new Map();
    this._streamElIndex = new Map();
    // call_id -> { row, nodes }. Only result-owned siblings live here
    // (output/media/MCP card/preview chip), so an accepted row can replace a
    // provisional receipt without disturbing output-guard warnings.
    this._toolResultNodes = new Map();
    // Latest running task-agent prompt usage by parent call id. Context can
    // arrive before its parent row during the parallel tool-pool race; the map
    // buffers that reading until _relinkAgentCards paints the row. Repeated
    // live/synthetic events replace one reading and update one badge.
    this._agentContexts = new Map();
    // parent call id -> shared-reducer holder for a task agent's transient
    // compaction progress. Parallel task agents compact independently, so one
    // foreground holder cannot represent them.
    this._agentCompactions = new Map();
    // Throws caught while finalizing a reply segment's render; each console
    // line carries the running total.  (A throwing dispatch is the sync core's
    // to count: inspect().health.renderThrows.)
    this._finalizeThrows = 0;
    this._cancelTimeout = null;
    this._forceTimeout = null;
    // The latched edit-and-resend text and the auth generation it was composed
    // under, so an edit is never sent as a different signed-in user.
    this._pendingEditSend = null;
    this._pendingEditGeneration = 0;
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
    this._ttsAbort = null; // AbortController of the play being synthesized
    this._createDOM();
    // The workstream's stream and history recovery: the core decides when to
    // load /history, when to connect and what the status line says; this pane
    // applies events and renders history through the view below.  dispatch
    // goes through the instance on every call, so a wrapper installed on
    // handleEvent later still sees every event.
    this.sync = new WorkstreamSync({
      wsId: this.wsId,
      base: this._base,
      fetch: authFetch,
      view: {
        dispatch: (event) => this.handleEvent(event),
        renderHistory: (messages) => this.replayHistory(messages),
        resetLiveState: () => this._resetLiveState(),
        liveState: () => ({
          streaming: !!(this.currentAssistantEl || this.currentReasoningEl),
          busy: this.busy,
          sending: this.busy && this.busySource === "optimistic",
        }),
        placeRepairPrompt: (prompt) => {
          this.messagesEl.appendChild(prompt);
          this.scrollToBottom();
        },
      },
    });
    this.sync.on("status", (phase) => this._renderStreamStatus(phase));
    this.sync.on("reset-settled", (settled) => this._onResetSettled(settled));
  }

  reset() {
    this.currentAssistantEl = null;
    this._reasoningActivity.finish();
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

  // composer.setBusy runs unconditionally so the Stop button label /
  // dataset.forceCancel / placeholder stay canonical even on a redundant
  // call (Pane.reset() and any future caller relies on that idempotent
  // reset). queue.onIdleEdge runs only on the actual edge — it carries
  // the heavier work (querySelectorAll-driven promote sweep + cancel-
  // timer cleanup wired via the queue's onIdle hook).
  setBusy(b, source) {
    const next = !!b;
    if (!next) this._reasoningActivity.finish();
    // Who asserted busy: "server" (default — state events, thinking_start,
    // every existing/future writer) or "optimistic" (ONLY the send flow's
    // pre-POST flip). The deferred/queue_full settle arms may clear busy
    // solely when it is still this send's own optimistic flip — a server-
    // stamped busy is a real turn and must never be clobbered. Centralized
    // HERE so an unstamped future writer fails safe as "server".
    this.busySource = next ? source || "server" : null;
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
    if (this._compaction.card) return; // the compaction card owns the affordance
    this._reasoningActivity.start(this.messagesEl);
    this.scrollToBottom();
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
      this.scrollToBottom();
      return card;
    }
    if (source === "watch_triggered" && meta && typeof meta === "object") {
      const card = buildWatchResultCard(meta, content || "");
      this.messagesEl.appendChild(card);
      this.scrollToBottom();
      return card;
    }
    if (source === "output_guard" && meta && typeof meta === "object") {
      const card = _buildGuardFindingBubble(meta);
      this.messagesEl.appendChild(card);
      this.scrollToBottom();
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
    this.scrollToBottom();
    return el;
  }

  handleCompactionEvent(evt) {
    if ((evt.target || "workstream") === "task_agent") {
      this._handleAgentCompaction(evt);
      return;
    }
    // Shared reducer (conversation.applyCompactionEvent) — one lifecycle
    // state machine for this pane and the coordinator viewer.  The dedup
    // set is the same one the system_turn path uses: the persisted marker
    // row is stamped with the ok-end event's id, so whichever of /history
    // repaint or live/replayed event renders first wins.  reason="error"
    // ends render through the paired `error` event (red row), not here.
    this.removeEmptyState();
    applyCompactionEvent(this._compaction, evt, {
      container: this.messagesEl,
      renderedIds: this._renderedSystemEventIds,
      onNotice: (msg) => this.addInfoMessage(msg),
      scroll: () => this.scrollToBottom(),
    });
    // Manual compaction's busy state can start reasoning before this card.
    if (this._compaction.card) this._reasoningActivity.finish();
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

  addUserMessage(text, attachments, opts) {
    opts = opts || {};
    this.removeEmptyState();
    const el = document.createElement("div");
    el.className = "msg user";
    if (opts.clientSendId) el.dataset.clientSendId = opts.clientSendId;
    if (opts.eventId != null) el.dataset.eventId = String(opts.eventId);
    if (opts.sender) el.dataset.sender = opts.sender;
    if (opts.source) el.dataset.source = opts.source;
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
    this.scrollToBottom();
    // Returned so the send flow can retro-convert the optimistic bubble
    // into a queued chip when the server answers queued+deferred.
    return el;
  }

  _markAcceptedClientSends(clientSendIds, remove, skipAlreadyAccepted = false) {
    return settleAcceptedClientSends(
      this.messagesEl,
      this.queue,
      clientSendIds,
      remove,
      skipAlreadyAccepted,
    );
  }

  _acceptUserTurn(evt) {
    acceptUserTurnEvent(evt, {
      renderedEventIds: this._renderedUserEventIds,
      messagesEl: this.messagesEl,
      queue: this.queue,
      consumeAttachments: (ids) => this.attachments.consume(ids, []),
      renderNudgeMarker: () => this.addSystemNudgeMarker(),
      renderUserTurn: (content, attachments, opts) =>
        this.addUserMessage(content, attachments || null, opts),
    });
  }

  // --- Approval-cycle bookkeeping -----------------------------------------
  // The backend registers one ApprovalCycle per human-gated batch; parallel
  // task agents make several live at once.  Cards register here on paint and
  // deregister on resolution; the composer stays disabled while ANY cycle is
  // live.

  _syncApprovalState() {
    // Prune orphaned cycles whose block elements are no longer in the DOM.
    // A rebuild that wipes the conversation subtree (clear_ui re-render,
    // truncated resync — messagesEl.replaceChildren()) also clears
    // approvalCycles in replayHistory.  But if an approve_request
    // event is processed between the wipe and the refetch, its cycle card
    // is in a detached
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
    StatusBar.paintApprovalChip(
      { approvalEl: this._sbApproval, focusFallbackEl: this.inputEl },
      this.approvalCycles.size,
    );
  }

  _oldestCycleId() {
    const first = this.approvalCycles.keys().next();
    return first.done ? null : first.value;
  }

  // Status-bar chip click: bring the card the keyboard shortcuts act on
  // (the OLDEST live cycle — see the keydown handler) into view and hand
  // focus to its feedback field, exactly as a freshly painted first cycle
  // does.  The card may have scrolled far away or, for a task-agent
  // sub-batch, sit inside an agent card the user re-collapsed after the
  // approve paint forced it open — unfold both layers first, otherwise
  // scrollIntoView on a display:none row is a no-op.
  revealPendingApproval() {
    const id = this._oldestCycleId();
    const entry = id ? this.approvalCycles.get(id) : null;
    const block = entry
      ? entry.blockEls.find((el) => el && el.isConnected)
      : null;
    if (!block) return;
    const agentCard = block.closest(".conv-agent");
    if (agentCard) {
      agentCard.dataset.collapsed = "false";
      const toggle = agentCard.querySelector(".conv-agent-toggle");
      if (toggle) toggle.setAttribute("aria-expanded", "true");
    }
    const batch = block.closest(".conv-batch");
    if (batch) setConvBatchExpanded(batch, true, { blocker: true });
    StatusBar.scrollToApprovalTarget(block);
    const fb = block.querySelector(".conv-feedback");
    // preventScroll: the smooth scroll above owns the viewport; a focus
    // jump would cut it short and land the card at the viewport edge.
    if (fb) fb.focus({ preventScroll: true });
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
    this.scrollToBottom();
  }

  showOutputWarning(evt) {
    if (!evt.call_id || evt.risk_level === "none") return;
    const toolDiv = this._toolRow(evt.call_id);
    if (!toolDiv) return;
    const warningBatch = toolDiv.closest(".conv-batch");
    if (warningBatch) {
      setConvBatchExpanded(warningBatch, true, { blocker: true });
    }
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
    if (
      nextEl &&
      (nextEl.classList.contains("tool-output") ||
        nextEl.classList.contains("conv-result-wrap"))
    ) {
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
    const verdictBatch = vRow ? vRow.closest(".conv-batch") : null;
    preserveTranscriptBottomPin(this.messagesEl, () => {
      if (verdictBatch && isConvVerdictCompactBlocker(verdict)) {
        setConvBatchExpanded(verdictBatch, true, { blocker: true });
      }
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
          rec === "approve" ? "success" : rec === "deny" ? "error" : "warn",
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

      this.updateVerdictGlow(verdict.recommendation, verdictBatch);
    });
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
    return this._scrollFollow.isFollowing();
  }

  scrollToBottom(force) {
    if (force) this._scrollFollow.jumpToLatest();
    else this._scrollFollow.schedule();
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
      // Status-bar and content buttons keep their own keyboard activation;
      // approval shortcuts must not swallow a click or approve another card.
      if (e.target === this._sbApproval) return;
      if (e.target === this._sbFiles) return;
      if (e.target.closest(".conv-content-chip")) return;
      // Enter on a focused control runs that control (on Deny it denies), and
      // Escape on a full call or "show all" does nothing: neither approves or
      // denies from there.  Letters still reach the shortcuts below.
      if (convKeyBelongsToControl(e.key, e.target)) return;
      // A key in any other text field (the composer, disabled while a call is
      // pending but perhaps still focused) belongs to that field.
      const t = e.target;
      if (
        (t.tagName === "TEXTAREA" ||
          t.tagName === "INPUT" ||
          t.isContentEditable) &&
        !t.classList.contains("conv-feedback")
      )
        return;
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
      // Browser and OS accelerators (Ctrl+A select-all, Cmd+A, Alt+N) are not
      // shortcuts, as in the coordinator.
      if (e.ctrlKey || e.metaKey || e.altKey) return;
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
    this.el.appendChild(this.messagesEl);
    this._scrollFollow = mountConversationScroll(this.messagesEl);
    this._unregisterTranscriptScroller = registerTranscriptScroller(
      this.messagesEl,
      {
        isFollowing: () => this.isNearBottom(),
        scrollToBottom: () => this.scrollToBottom(),
      },
    );

    // Per-workstream status bar (above input)
    this.statusBarEl = document.createElement("div");
    this.statusBarEl.className = "ws-status-bar ws-sb-has-files";
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
    // Pending-approval chip: sticky count of the live human gates in this
    // transcript, painted by _syncApprovalState (the one approval-state
    // chokepoint) and hidden at zero.  A button, so it tabs and clicks:
    // the click brings the card the keyboard shortcuts act on into view.
    this._sbApproval = document.createElement("button");
    this._sbApproval.type = "button";
    this._sbApproval.className = "warn-chip ws-sb-approval";
    this._sbApproval.hidden = true;
    this._sbApproval.addEventListener("click", () =>
      this.revealPendingApproval(),
    );

    this._sbFiles = document.createElement("button");
    this._sbFiles.type = "button";
    this._sbFiles.className = "ws-sb-files";
    this._sbFiles.textContent = "Files";
    const filesOpen = document.createElement("span");
    filesOpen.textContent = "↗";
    filesOpen.setAttribute("aria-hidden", "true");
    this._sbFiles.appendChild(filesOpen);
    this._sbFiles.hidden = !(hasScope("write") && hasPermission("files.read"));
    this._sbFiles.title = "Open Files pane";
    this._sbFiles.setAttribute("aria-label", "Browse workspace files");
    this._sbFiles.addEventListener("click", () => this._host.onFiles());

    this.statusBarEl.appendChild(this._sbTokens);
    this.statusBarEl.appendChild(this._sbTools);
    this.statusBarEl.appendChild(this._sbApproval);
    this.statusBarEl.appendChild(this._sbTurns);
    this.statusBarEl.appendChild(this._sbFiles);
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
      scroll: () => this.scrollToBottom(),
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

  // Show the stream's phase in the status bar with the painter every pane
  // uses: dim while the stream is not live, the phase's words in the tokens
  // cell; live repaints the last status counters, or the placeholder when none
  // ever arrived.  Dead also ends the turn's Stop-button timers, voice I/O and
  // reasoning clock, which nothing will settle now.
  _renderStreamStatus(phase) {
    StatusBar.paintStreamPhase(
      { barEl: this.statusBarEl, tokensEl: this._sbTokens },
      phase,
      STREAM_STATUS_TEXT[phase],
      this._lastStatusEvt,
      (evt) => this.updateStatus(evt),
    );
    if (phase === "dead") this._stopTurnActivity();
  }

  // The Stop button's Force Stop offer and its 10 s auto-recover, and the
  // reasoning clock, belong to a turn this pane can still watch settle, and
  // voice I/O to a pane still on screen: only the pane's end (destroy) or the
  // stream's (dead) stops them, never a redial or a hidden tab.
  _stopTurnActivity() {
    clearTimeout(this._cancelTimeout);
    clearTimeout(this._forceTimeout);
    this._cancelTimeout = null;
    this._forceTimeout = null;
    this._reasoningActivity.finish();
    this._stopRecording(true);
    this._stopTTS();
  }

  // A clear_ui (rewind, retry, resume, open, fork) settled.  The rewind
  // already committed server-side, so a queued edit-and-resend still sends
  // whether the refetch rendered, failed, was declined or was superseded,
  // and also after the pane was torn down; its bubble lands on whatever
  // transcript is showing and the rewound truth arrives with the next
  // completed render.  A render throw drops the edit, releases the composer
  // and says why; so does a changed sign-in, handing the text back.
  _onResetSettled({ outcome, error }) {
    if (outcome === "threw") {
      this._pendingEditSend = null;
      this.setBusy(false);
      this.addErrorMessage("Failed to reload history: " + error.message);
      return;
    }
    if (!this._pendingEditSend) return;
    const editText = this._pendingEditSend;
    this._pendingEditSend = null;
    // Signed in as someone else (or signed out) since the edit was composed: the
    // rewind stands, but the text is not sent as whoever is signed in now.  A
    // mounted pane hands it back.
    if (authGeneration() !== this._pendingEditGeneration) {
      this.setBusy(false);
      if (this.sync.status !== "destroyed") {
        this.composer.value = mergeRejectedComposerText(
          editText,
          this.composer.value,
        );
        this.addInfoMessage(
          "Edit not sent: the signed-in user changed. Your text is back in the composer.",
        );
      }
      return;
    }
    const editClientSendId = mintClientSendId();
    const editPriority = parsePriority(editText);
    this.setBusy(true, "optimistic");
    const editEl = this.addUserMessage(editText, null, {
      clientSendId: editClientSendId,
    });
    this.scrollToBottom(true);
    postAndSettleSend(
      this.queue,
      authFetch(
        this._base +
          "/v1/api/workstreams/" +
          encodeURIComponent(this.wsId) +
          "/send",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: editText,
            client_send_id: editClientSendId,
          }),
        },
      ),
      {
        queuedEl: null,
        optimisticEl: editEl,
        isBusy: false,
        displayText: editPriority.displayText,
        priority: editPriority.priority,
        clientSendId: editClientSendId,
        setBusy: (value) => this.setBusy(value),
        busyIsOptimistic: () => this.busy && this.busySource === "optimistic",
        paneIsBusy: () => this.busy,
        restoreInput: () => {
          this.composer.value = mergeRejectedComposerText(
            editText,
            this.composer.value,
          );
        },
        renderError: (message) => this.addErrorMessage(message),
        consumeAttachments: () => {},
      },
    );
  }

  _clearAgentTracking() {
    // Release task-agent bookkeeping ahead of (or after) a full rebuild.
    // Entries left in _agentCards would pin every replaced card subtree as
    // reachable detached DOM — unbounded growth across an hours-long
    // session's rewinds/compaction re-syncs — and a stale _agentOrphans
    // grace timer would escape buffered steps into the rebuilt pane.
    if (this._agentCards) this._agentCards.clear();
    if (this._agentContexts) this._agentContexts.clear();
    if (this._agentCompactions) {
      for (const holder of this._agentCompactions.values()) {
        resetCompactionHolder(holder);
      }
      this._agentCompactions.clear();
    }
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
    if (this._toolResultNodes) this._toolResultNodes.clear();
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
    const rows = this.messagesEl.querySelectorAll(
      '.conv-row[data-call-id="' + CSS.escape(callId) + '"]',
    );
    row = rows.length ? rows[rows.length - 1] : null;
    if (row) this._toolRowIndex.set(callId, row);
    else this._toolRowIndex.delete(callId);
    return row;
  }

  _indexToolRows(root) {
    if (!root) return;
    root.querySelectorAll(".conv-row[data-call-id]").forEach((row) => {
      const callId = row.dataset.callId || "";
      if (!callId) return;
      // Reused provider ids are valid across turns. The newest rendered batch
      // owns future result events; release only the old tracking reference,
      // never the old turn's still-visible DOM.
      indexLatestToolRow(
        this._toolRowIndex,
        this._toolResultNodes,
        callId,
        row,
      );
    });
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
    switch (evt.type) {
      case "thinking_start":
        this.setBusy(true);
        this.removeEmptyState();
        this.addThinkingIndicator();
        break;

      case "thinking_stop":
        this._reasoningActivity.hideWaiting();
        break;

      case "reasoning":
        let reasoningBody = null;
        if (!this.currentReasoningEl) {
          this.currentReasoningEl = document.createElement("div");
          this.currentReasoningEl.className = "msg reasoning";
          reasoningBody = document.createElement("div");
          reasoningBody.className = "msg-body";
          this.currentReasoningEl.appendChild(reasoningBody);
          this.messagesEl.appendChild(this.currentReasoningEl);
        } else {
          reasoningBody = this.currentReasoningEl.querySelector(".msg-body");
        }
        this._reasoningActivity.attach(this.currentReasoningEl);
        if (reasoningBody) reasoningBody.textContent += evt.text;
        this.scrollToBottom();
        break;

      case "content":
        this._reasoningActivity.finish();
        this.currentReasoningEl = null;
        if (!this.currentAssistantEl) {
          this._newAssistantBubble();
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
        this._reasoningActivity.finish();
        this.currentReasoningEl = null;
        this.contentBuffer = "";
        // Finalize the completed streaming segment's markdown.  This fires
        // per-segment (between tool calls), NOT per-turn.  Busy state is
        // managed by state_change events instead.
        if (doneBodyEl && doneBuffer) {
          try {
            streamingRenderFinalize(doneBodyEl, doneBuffer);
          } catch (err) {
            this._finalizeThrows += 1;
            console.warn(
              "interactive: streamingRenderFinalize failed (finalize-throw total " +
                this._finalizeThrows +
                ")",
              err,
            );
            doneBodyEl.textContent = doneBuffer;
          }
        }
        this.scrollToBottom();
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
        if (evt.reasoning || evt.content) this._reasoningActivity.hideWaiting();
        if (evt.reasoning) {
          let snapshotReasoningBody = null;
          if (!this.currentReasoningEl) {
            this.currentReasoningEl = document.createElement("div");
            this.currentReasoningEl.className = "msg reasoning";
            snapshotReasoningBody = document.createElement("div");
            snapshotReasoningBody.className = "msg-body";
            this.currentReasoningEl.appendChild(snapshotReasoningBody);
            this.messagesEl.appendChild(this.currentReasoningEl);
          } else {
            snapshotReasoningBody =
              this.currentReasoningEl.querySelector(".msg-body");
          }
          this._reasoningActivity.attach(this.currentReasoningEl);
          const curReason = snapshotReasoningBody
            ? snapshotReasoningBody.textContent || ""
            : "";
          if (
            snapshotReasoningBody &&
            curReason.length < evt.reasoning.length
          ) {
            snapshotReasoningBody.textContent = evt.reasoning;
          }
        }
        if (evt.content) {
          this._reasoningActivity.finish();
          // Content snapshot supersedes any reasoning bubble — matches
          // the "case content" invariant of clearing currentReasoningEl
          // when content begins.
          this.currentReasoningEl = null;
          if (!this.currentAssistantEl) {
            this._newAssistantBubble();
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
          this._reasoningActivity.finish();
          this.setBusy(false);
          // A context event received while its call id still resolved to a
          // prior terminal row is retained briefly for a possible successor
          // row with a recycled provider id. If no such row appeared before
          // the turn settled, the reading belonged to the terminal occurrence.
          for (const [parentId, reading] of this._agentContexts.entries()) {
            if (reading.blockedByTerminalRow) {
              this._agentContexts.delete(parentId);
            }
          }
          for (const [parentId, holder] of this._agentCompactions.entries()) {
            if (holder.blockedByTerminalRow) {
              resetCompactionHolder(holder);
              this._agentCompactions.delete(parentId);
            }
          }
          this._attachRetryToLastAssistant();
          // Only steal focus if this is the active pane and no approval pending.
          if (this._host.isFocused(this) && !this.pendingApproval) {
            this.inputEl.focus();
          }
        } else {
          // Every other state is a turn in flight (thinking, running, or
          // waiting on the judge or a person), as the acting-user check above
          // already assumes: a new transient state needs no edit here.
          this.setBusy(true);
          // A reconnect before the first token has only the current state,
          // not the earlier thinking_start. Restore its waiting affordance.
          if (
            evt.state === "thinking" &&
            !this.currentAssistantEl &&
            !this.currentReasoningEl
          ) {
            this.addThinkingIndicator();
          }
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

      case "agent_context":
        this._updateAgentContext(evt);
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
        if (acceptedToolEventAlreadyRendered(this._renderedToolEventIds, evt)) {
          break;
        }
        // Record the accepted event as rendered ONLY when a paint actually
        // happened: a false return (no target row — clear_ui wipe with the
        // refetch in flight, a fresh mid-turn join) must leave the id
        // unrecorded so the ring's later replay of the same event can paint
        // it instead of being deduped into a permanently missing output.
        if (
          this.appendToolOutput(
            evt.call_id || "",
            evt.name,
            evt.output,
            evt.is_error,
            evt.preview,
            {
              accepted: evt.accepted === true,
              eventId: evt._event_id,
              effectStatus: evt.effect_status,
              attachments: evt.attachments,
            },
          )
        ) {
          recordAcceptedToolEvent(this._renderedToolEventIds, evt);
        }
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

      case "user_turn":
        this._acceptUserTurn(evt);
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
        if (sysEid && this._renderedSystemEventIds.has(sysEid)) {
          break;
        }
        if (
          evt.source === "user_interjection" &&
          evt.meta &&
          evt.meta.client_send_id &&
          clientSendMaySettleForViewer(evt.meta.sender, viewerUserId())
        ) {
          this._markAcceptedClientSends([evt.meta.client_send_id], true);
        }
        this.addSystemContext(
          evt.content || "",
          evt.source || "",
          evt.meta || null,
        );
        if (sysEid) this._renderedSystemEventIds.add(sysEid);
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
        // Mark the exact optimistic chip as admitted before the HTTP response:
        // if that ACK is lost, the catch must not erase/report an unsent row.
        if (
          evt.client_send_id &&
          clientSendMaySettleForViewer(evt.sender, viewerUserId())
        )
          this._markAcceptedClientSends([evt.client_send_id], false, true);
        break;

      case "message_dispatched":
        // A deferred send left the parked list: fresh spawn (promote the
        // chip — the ×'s window is over) or interjection fold-in
        // (folded: true — only the deferred flag clears; the chip resumes
        // the normal queued lifecycle). No-op when this tab holds no
        // matching chip.
        this.queue.settleDeferred(evt.msg_id, !!evt.folded);
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
        this._reasoningActivity.finish();
        this.currentReasoningEl = null;
        this.contentBuffer = "";
        this.stopBtn.disabled = true;
        this.stopBtn.textContent = "Cancelling\u2026";
        this.stopBtn.setAttribute("aria-label", "Cancelling generation");
        this.scrollToBottom();
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

  // The one creation path for live-streamed assistant bubbles (the
  // content and in_progress_snapshot branches): bubble + body + the
  // persistent copy action, with the streaming refs assigned as a unit
  // so the two branches cannot drift.
  _newAssistantBubble() {
    this.currentAssistantEl = document.createElement("div");
    this.currentAssistantEl.className = "msg assistant";
    this.currentAssistantBodyEl = document.createElement("div");
    this.currentAssistantBodyEl.className = "msg-body";
    this.currentAssistantEl.appendChild(this.currentAssistantBodyEl);
    this._addCopyAction(this.currentAssistantEl);
    this.messagesEl.appendChild(this.currentAssistantEl);
  }

  // Every assistant bubble gets a persistent copy button at creation —
  // O(1) per message, unlike the removed whole-transcript sweep this
  // file's retry attach deliberately avoids (see
  // _attachRetryToLastAssistant).  The bar is shared with the transient
  // retry / TTS buttons the holder mechanism adds and removes.  Callers
  // pass a JUST-CREATED bubble (both stream branches via
  // _newAssistantBubble, and replay) — no dedup needed against an empty
  // bar.
  _addCopyAction(el) {
    ensureMsgActionsBar(el).appendChild(buildMsgCopyButton(el));
  }

  _addRetryAction(el) {
    const bar = ensureMsgActionsBar(el);
    bar.insertBefore(
      buildMsgRetryButton(() => this._retryLast()),
      bar.firstChild,
    );
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
    const bar = ensureMsgActionsBar(el);
    if (bar.querySelector(".msg-tts-btn")) return; // already added
    // Inserted BEFORE the persistent copy button (below) so DOM order,
    // tab order and visual order agree: [retry, tts, copy], with copy
    // anchoring the corner on every bubble.
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
    bar.insertBefore(btn, bar.querySelector(".msg-copy-btn"));
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
    // A newer play replaces one still being synthesized: abort that request so
    // the node stops working on a clip nobody will hear.
    if (this._ttsAbort) this._ttsAbort.abort();
    const controller = new AbortController();
    this._ttsAbort = controller;
    btn.classList.add("is-busy");
    btn.disabled = true;
    authFetch(this._base + "/v1/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal: controller.signal,
    })
      .then((r) => {
        if (!r.ok) {
          // A proxy in front may answer with an HTML error page.
          return r
            .json()
            .catch(() => null)
            .then((b) => {
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
        // Only the clip still playing may stop playback: a replaced clip's late
        // events would otherwise stop, and abort, the play that replaced it.
        const stopIfCurrent = () => {
          if (this._ttsAudio === audio) this._stopTTS();
        };
        audio.addEventListener("ended", stopIfCurrent);
        audio.addEventListener("error", stopIfCurrent);
        audio.play().catch(stopIfCurrent);
      })
      .catch((err) => {
        if (controller.signal.aborted) return; // replaced or stopped, at any step
        showToast(
          err && err.message ? err.message : "Speech synthesis failed",
          "error",
        );
      })
      .finally(() => {
        if (this._ttsAbort === controller) this._ttsAbort = null;
        btn.classList.remove("is-busy");
        btn.disabled = false;
      });
  }

  _stopTTS() {
    this._ttsSeq++; // invalidate any in-flight request
    if (this._ttsAbort) {
      this._ttsAbort.abort(); // and stop the node synthesizing it
      this._ttsAbort = null;
    }
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
    if (this.busy || this.sync.historyStale) return;
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
    if (this.busy || this.sync.historyStale) return;
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
    if (this.busy || this.sync.historyStale) return;
    // Count turns to rewind (from this message onward). Bare
    // ``.msg.user`` matches the server's turn semantics — see
    // _rewindToMessage.
    const userMsgs = this.messagesEl.querySelectorAll(".msg.user");
    const idx = Array.prototype.indexOf.call(userMsgs, msgEl);
    if (idx < 0) return;
    const turnsToRewind = userMsgs.length - idx;

    this.setBusy(true);
    // Store pending send — dispatched by _onResetSettled once the
    // rewind's clear_ui refetch of /history settles.
    this._pendingEditSend = newText;
    this._pendingEditGeneration = authGeneration();
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

  // Drop the reply bubble refs and their buffers, nothing else, so the next
  // reply delta creates a fresh bubble instead of writing into a detached or
  // superseded one.  Per-transcript state (approval cycles, announce shells,
  // the compaction and retry holders) resets with the transcript in
  // replayHistory, so a reload whose fetch fails leaves the approval cards on
  // screen working.
  _resetLiveState() {
    this.currentAssistantEl = null;
    this.currentAssistantBodyEl = null;
    this._reasoningActivity.finish();
    this.currentReasoningEl = null;
    this.contentBuffer = "";
  }

  replayHistory(messages) {
    const scrollTop = this.messagesEl.scrollTop;
    this.messagesEl.replaceChildren();
    // Everything that pointed into the wiped subtree goes with it: the reply
    // refs, the in-progress compaction card (live events re-create it), the
    // approval cycles and announce shells (the replayed history and the
    // stream's replay re-register live ones), the retry holder, and the
    // agent-card and orphan maps (detached-DOM retention).
    this._resetLiveState();
    resetCompactionHolder(this._compaction);
    this.approvalCycles = new Map();
    this.announcedBlocks = new Map();
    this._syncApprovalState();
    this._retryHolderEl = null;
    this._clearAgentTracking();
    // Reset the per-pane dedup set: ids of operator-context system turns
    // already painted from /history.  A later SSE replay that redelivers one
    // (resume-cursor overlap) is skipped by the system_turn handler.
    this._renderedSystemEventIds = new Set();
    this._renderedUserEventIds = new Set();
    this._renderedToolEventIds = new Set();
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
    // Occurrence queues, not one-value call-id maps: providers may reuse an id
    // in a later turn, and malformed same-batch duplicates deliberately take
    // the strong /history repair path. The repair renderer must therefore pair
    // each sequential TOOL row with the matching sequential call occurrence.
    const pendingToolRows = new Map();
    const pendingAssessments = new Map();
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
        this.addUserMessage(msg.content || "", msg.attachments || null, {
          eventId: msg.event_id,
          sender: msg.sender || "",
          source: msg.source || "",
        });
        if (msg.event_id != null)
          this._renderedUserEventIds.add(String(msg.event_id));
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
          // Same render mechanism as the coordinator's history path: the
          // finalize helper renders + post-renders AND stashes the raw
          // markdown on the body for the copy affordance — replayed
          // bubbles must copy identically to live-streamed ones.  The
          // copy action attaches BEFORE the render, matching the
          // streaming path's attach-then-fill order.
          this._addCopyAction(el);
          streamingRenderFinalize(bodyEl, msg.content);
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
              if (tc.id) {
                enqueueToolOccurrence(pendingToolRows, tc.id, row);
                indexLatestToolRow(
                  this._toolRowIndex,
                  this._toolResultNodes,
                  tc.id,
                  row,
                );
              }
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
                const assessmentKey = tc.id || "";
                enqueueToolOccurrence(pendingAssessments, assessmentKey, {
                  assessment: tc.output_assessment,
                  toolDiv: row,
                });
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
            resultTarget = shiftToolOccurrence(
              pendingToolRows,
              msg.tool_call_id,
            );
            if (!resultTarget) {
              resultTarget = lastToolBlock.querySelector(
                '.conv-row[data-call-id="' +
                  CSS.escape(msg.tool_call_id) +
                  '"]',
              );
            }
          }
          // Cursor-style append: cursor advances after each insert so
          // the next sibling lands AFTER the previous one.  Fixes the
          // bug where calling resultTarget.after(node) twice put the
          // second node BETWEEN resultTarget and the first (the second
          // .after call was always relative to the same anchor).
          // Resulting order with all present:
          //   [tool div][output][output-warning]
          let insertCursor = resultTarget;
          const resultNodes = [];
          const insertChained = (node, resultOwned = true) => {
            if (insertCursor) {
              insertCursor.after(node);
              insertCursor = node;
            } else {
              const bdg = lastToolBlock.querySelector(".conv-status");
              if (bdg) lastToolBlock.insertBefore(node, bdg);
              else lastToolBlock.appendChild(node);
            }
            if (resultOwned) resultNodes.push(node);
          };
          if (stripped && !isDenied) {
            const media = !isToolError ? tryParseMedia(stripped) : null;
            if (media) {
              insertChained(buildMediaEmbed(media, stripped));
            } else {
              insertChained(renderCollapsibleOutput(stripped, isToolError));
            }
          }
          if (!isDenied) {
            const images = buildToolImages(msg.attachments, {
              wsId: this.wsId,
              base: this._base,
            });
            if (images) insertChained(images);
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
          const fileChip = this._fileResultChip(
            resultTarget, isToolError, isDenied, msg.effect_status,
          );
          if (fileChip) insertChained(fileChip);
          // A row naming a call_id this batch does not own is an ORPHAN
          // (a result for an earlier batch, or a second writer's append)
          // — it must not stamp THIS batch as failed.  The shared outcome
          // index skips unmatched occurrences for the same reason.  A row
          // with no call_id at all is the legacy positional case and does
          // belong to the preceding batch, so it still stamps.
          const isOrphanResult = !!msg.tool_call_id && !resultTarget;
          if (
            isToolError &&
            !isOrphanResult &&
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
            const assessment = shiftToolOccurrence(
              pendingAssessments,
              msg.tool_call_id,
            );
            if (assessment) {
              insertChained(
                _buildOutputWarningEl(assessment.assessment),
                false,
              );
            }
          }
          if (resultTarget) {
            setToolOutputReviewState(resultTarget, msg.content);
          }
          if (msg.tool_call_id && resultTarget) {
            if (msg.effect_status) {
              resultTarget.dataset.effectStatus = String(msg.effect_status);
            } else {
              delete resultTarget.dataset.effectStatus;
            }
            this._toolResultNodes.set(msg.tool_call_id, {
              row: resultTarget,
              nodes: resultNodes,
            });
          }
          // Task-agent recall: flip its card done/error from the task's OWN
          // result (matching the live appendToolOutput) — NOT from sub-step
          // errors, since a sub-tool can fail and the agent still synthesize.
          const resultAgentWrap =
            (resultTarget && resultTarget.querySelector(".conv-agent")) ||
            (msg.tool_call_id && agentCardWraps[msg.tool_call_id]);
          if (resultAgentWrap) {
            resultAgentWrap.dataset.state = msg.is_error ? "error" : "done";
            this._syncAgentToggleLabel(resultAgentWrap);
          }
          if (resultTarget) {
            // Replay is already historical: stage a fold even while Default is
            // selected, but never announce completion from the replay loop.
            markConvRowResultSettled(resultTarget, { autoFold: true });
          }
        }
        if (msg.event_id != null) {
          this._renderedToolEventIds.add(String(msg.event_id));
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
        // Deliberately does NOT null lastToolBlock: a system row is not a
        // turn boundary.  The batch's result window ends at the next
        // assistant/user row (both reset the anchor in their own
        // branches) — same rule as indexHistoryToolOutcomes.  Nulling
        // here made every tool result AFTER an interleaved system row
        // (mid-turn operator context, a second writer's append) silently
        // vanish from this pane while the coordinator rendered it.
      }
    }
    // Flush any output_assessments left in the map — these correspond
    // to assistant tool_calls whose tool result row didn't carry a
    // tool_call_id (legacy / migrated rows pre-dating the wire-format
    // addition).  Render the warning under the tool div itself rather
    // than dropping the safety information silently.
    for (const leftovers of pendingAssessments.values()) {
      for (const leftover of leftovers) {
        leftover.toolDiv.insertAdjacentElement(
          "afterend",
          _buildOutputWarningEl(leftover.assessment),
        );
      }
    }
    this._attachRetryToLastAssistant();
    this.messagesEl.scrollTop = scrollTop;
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
    // Remove the previous holder's retry / TTS buttons via the tracked ref —
    // the old whole-transcript ".msg.assistant .msg-actions" sweep was O(N)
    // per busy→idle edge.  The bar itself stays: every assistant bubble owns
    // one for its persistent copy button (_addCopyAction, at creation), and
    // only the transient retry / TTS buttons move with the holder.  At most
    // one bubble carries them (this method is their only writer); a holder
    // detached by a rebuild no-ops harmlessly.
    if (this._retryHolderEl) {
      const oldBar = findMsgActionsBar(this._retryHolderEl);
      if (oldBar) {
        oldBar
          .querySelectorAll(".msg-retry-btn, .msg-tts-btn")
          .forEach((b) => b.remove());
      }
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
    this._indexToolRows(block);
    this._relinkAgentCards(list);
    this.scrollToBottom();
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
    // Who held focus before this card empties a shell or disables the
    // composer: the feedback field below takes focus only if no one did.
    const focusBefore = document.activeElement;
    // Reuse the early-paint announce shell if it's for this batch (upgrade in
    // place); else build fresh.
    const announced = this._takeAnnouncedBlock(items);
    const block = announced || document.createElement("div");
    const openFullCalls = announced ? captureConvFullCalls(announced) : null;
    if (announced) {
      announced.replaceChildren();
      delete announced.dataset.resultsSettled;
      delete announced.dataset.compactFolded;
    }
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
        .filter((it) => it.needs_approval && it.func_name && !it.error)
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
    this._indexToolRows(block);
    if (!autoApproved) {
      this._registerApprovalCycle(cycleId, [block], items);
      const fb = block.querySelector(".conv-feedback");
      // Focus the feedback field only for the FIRST (oldest) live cycle, in
      // the pane the user is on, and only from no one.  Focus anyone holds
      // (another card's feedback field, a "full call" or "show all", the
      // composer with text in it, a field in another pane) stays put, or
      // their next keys would land in this card and Enter would approve it.
      // Where sending a message leaves focus (the empty composer, or the Send
      // button) is free too: this card is what comes next.  A pane scrolled
      // back to read is not pulled down to it; the status-bar chip brings it
      // up.
      if (this._oldestCycleId() === cycleId) {
        const free = (el) =>
          convFocusIsFree(el, this.el, this.messagesEl) ||
          ((el === this.inputEl || el === this.sendBtn) && !this.inputEl.value);
        requestAnimationFrame(() => {
          if (!fb || !this.isNearBottom() || !this._host.isFocused(this))
            return;
          if (!free(focusBefore) || !free(document.activeElement)) return;
          fb.focus();
        });
      }
    }
    this._relinkAgentCards(items);
    restoreConvFullCalls(block, openFullCalls);
    this.scrollToBottom();
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

    entry.blockEls.forEach((el) => {
      const actions = el.querySelector(".conv-actions");
      if (actions) actions.remove();
    });
    const statusHost = entry.blockEls[0];
    if (statusHost) {
      if (!approved) {
        this._markAgentStepExceptional(statusHost, {
          denied: true,
          effectStatus: "none",
        });
      }
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

    this.scrollToBottom();
  }

  // --- Task-agent card: nest a sub-agent's sub-tool steps under its row -----
  _agentTransientRoute(parentId) {
    const parentRow = this._toolRow(parentId);
    const terminal = this._toolResultNodes.get(parentId);
    if (!parentRow || !terminal || terminal.row !== parentRow) {
      return { state: "current", row: parentRow };
    }
    // A fresh/truncated snapshot or live event can race a successor whose
    // provider recycled the same public call id. While busy, hold the transient
    // away from the earlier terminal occurrence; the next painted row relinks
    // it. At idle there can be no successor, so the event is stale.
    return this.busy
      ? { state: "blocked", row: parentRow }
      : { state: "drop", row: parentRow };
  }

  _handleAgentCompaction(evt) {
    const parentId =
      typeof evt.parent_call_id === "string" ? evt.parent_call_id : "";
    if (!parentId) return;
    let holder = this._agentCompactions.get(parentId);
    const route = this._agentTransientRoute(parentId);
    if (route.state === "drop") {
      if (holder) resetCompactionHolder(holder);
      this._agentCompactions.delete(parentId);
      return;
    }
    if (!holder) {
      // A terminal edge with no locally active attempt has no task-context
      // result to render. In particular, do not manufacture an empty
      // "0 steps · running" task card when a replay begins after start.
      if (evt.phase === "end") return;
      holder = { card: null, cid: null, pending: null };
      this._agentCompactions.set(parentId, holder);
    }
    const pendingCid =
      holder.pending && holder.pending.compaction_id != null
        ? String(holder.pending.compaction_id)
        : null;
    const activeCid = holder.cid != null ? holder.cid : pendingCid;
    const incomingCid =
      evt.compaction_id != null ? String(evt.compaction_id) : null;
    if (
      evt.phase === "end" &&
      activeCid != null &&
      incomingCid != null &&
      incomingCid !== activeCid
    ) {
      // The shared reducer also rejects this stale end. Return before the
      // wrapper's terminal cleanup so it cannot delete the newer holder.
      return;
    }
    holder.pending = evt;
    if (route.state === "blocked") {
      holder.blockedByTerminalRow = route.row;
      if (evt.phase === "end") {
        resetCompactionHolder(holder);
        this._agentCompactions.delete(parentId);
      }
      return;
    }
    delete holder.blockedByTerminalRow;
    const card = this._ensureAgentCard(parentId, true);
    if (card) this._syncAgentCompaction(parentId, card);
    if (evt.phase === "end") {
      // _syncAgentCompaction consumes a visible end. If the parent row has not
      // painted, there is no live task card to retire or historical result to
      // render, so drop the buffered terminal edge too.
      resetCompactionHolder(holder);
      this._agentCompactions.delete(parentId);
    }
  }

  _syncAgentCompaction(parentId, card) {
    const holder = this._agentCompactions.get(parentId);
    if (!holder || !holder.pending || holder.blockedByTerminalRow) return;
    const evt = holder.pending;
    holder.pending = null;
    card.wrap.hidden = false;
    if (evt.phase === "start") {
      const parentBatch = card.wrap.closest(".conv-batch");
      if (parentBatch) {
        setConvBatchExpanded(parentBatch, true, { blocker: true });
      }
      const prior = card.wrap.querySelector(".conv-agent-compaction-notice");
      if (prior) prior.remove();
    }
    applyCompactionEvent(holder, evt, {
      container: card.wrap,
      renderedIds: this._renderedSystemEventIds,
      renderResult: false,
      // Keep progress visible when the step body is collapsed.
      append: (node) => card.wrap.insertBefore(node, card.body),
      // Keep a task-local failure inside its card. It has no unscoped typed
      // error twin, and escaping it into the workstream transcript would
      // misattribute a non-fatal nested controller failure to the parent turn.
      onNotice: (message) => {
        let notice = card.wrap.querySelector(".conv-agent-compaction-notice");
        if (!notice) {
          notice = document.createElement("div");
          notice.className = "conv-agent-compaction-notice";
          notice.setAttribute("role", "status");
          card.wrap.insertBefore(notice, card.body);
        }
        notice.textContent = stripAnsi(message);
      },
      scroll: () => this.scrollToBottom(),
    });
  }

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
      const parentBatch = card.wrap.closest(".conv-batch");
      if (parentBatch) {
        setConvBatchExpanded(parentBatch, true, { blocker: true });
      }
      // Cards default to collapsed, but a pending approval is BLOCKING — it
      // can't hide behind the toggle or the turn stalls on a prompt the user
      // never sees.  Force the card open (sync aria with the data attribute).
      card.wrap.dataset.collapsed = "false";
      const toggle = card.wrap.querySelector(".conv-agent-toggle");
      if (toggle) toggle.setAttribute("aria-expanded", "true");
    }
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
      this._markAgentStepExceptional(row, {
        isError: !!(item.is_error || item.error),
        denied: !!item.denied,
        effectStatus: item.effect_status,
      });
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
    if (card.body.querySelector(".conv-row")) {
      // A context event can create the card before the agent emits its first
      // sub-tool. Once a real step lands the card is ordinary transcript UI;
      // reveal it even if a terminal result raced ahead and hid the former
      // context-only shell.
      delete card.wrap.dataset.contextOnly;
      card.wrap.hidden = false;
    }
    if (mode === "approve" && approveRows.length) {
      // Register this nested batch as its own approval cycle — the
      // backend gates each sub-agent batch independently, so buttons
      // must resolve THIS cycle, not "the" pending one (parallel task
      // agents can have several nested prompts live at once).
      this._registerApprovalCycle(cycleId, approveRows, items);
    }
    this._updateAgentLabel(card);
    this.scrollToBottom();
    return true;
  }

  _ensureAgentCard(parentCallId, contextOnly = false) {
    // _toolRow cache: a busy task agent resolves its parent row once per
    // child event — the uncached scan was O(transcript) per step.
    const parentRow = this._toolRow(parentCallId);
    if (!parentRow) return null;
    if (!this._agentCards) this._agentCards = new Map();
    let card = this._agentCards.get(parentCallId);
    if (card) {
      if (parentRow.contains(card.wrap)) {
        this._syncAgentContext(parentCallId, card);
        this._syncAgentCompaction(parentCallId, card);
        return card;
      }
      if (!card.wrap.isConnected) {
        // Same-turn row rebuild: showInlineToolBlock's replaceChildren on the
        // pending->resolved upgrade detached our card.  Re-attach the SAME card
        // so its already-rendered steps survive the upgrade.
        if (card.wrap.dataset.state === "running") {
          const parentBatch = parentRow.closest(".conv-batch");
          if (parentBatch) {
            setConvBatchExpanded(parentBatch, true, { blocker: true });
          }
        }
        parentRow.appendChild(card.wrap);
        this._syncAgentContext(parentCallId, card);
        this._syncAgentCompaction(parentCallId, card);
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
    if (contextOnly) card.wrap.dataset.contextOnly = "true";
    const parentBatch = parentRow.closest(".conv-batch");
    if (parentBatch) {
      setConvBatchExpanded(parentBatch, true, { blocker: true });
    }
    parentRow.appendChild(card.wrap);
    this._agentCards.set(parentCallId, card);
    this._syncAgentToggleLabel(card);
    this._syncAgentContext(parentCallId, card);
    this._syncAgentCompaction(parentCallId, card);
    return card;
  }

  _markAgentStepExceptional(row, details) {
    details = details || {};
    const effectStatus =
      details.effectStatus == null ? "" : String(details.effectStatus);
    if (
      !details.isError &&
      !details.denied &&
      !details.exceptional &&
      (!effectStatus || effectStatus === "committed")
    ) {
      return false;
    }
    const card = row && row.closest ? row.closest(".conv-agent") : null;
    if (!card) return false;
    const parentBatch = card.closest(".conv-batch");
    if (parentBatch) {
      setConvBatchExpanded(parentBatch, true, { blocker: true });
    }
    const changed = card.dataset.agentStepExceptional !== "true";
    card.dataset.agentStepExceptional = "true";
    const issue = card.querySelector(".conv-agent-step-issue");
    if (issue) issue.hidden = false;
    this._syncAgentToggleLabel(card);
    return changed;
  }

  _syncAgentToggleLabel(card) {
    const wrap = card.wrap || card;
    const toggle = card.toggle || wrap.querySelector(".conv-agent-toggle");
    const label = card.label || wrap.querySelector(".conv-agent-label");
    const context = card.context || wrap.querySelector(".conv-agent-context");
    const issue = card.issue || wrap.querySelector(".conv-agent-step-issue");
    if (!toggle || !label) return;
    const parts = [
      "Show or hide sub-agent steps",
      label.textContent || "0 steps",
    ];
    const state = wrap.dataset.state;
    if (state === "running") parts.push("running");
    else if (state === "done") parts.push("done");
    else if (state === "error") parts.push("failed");
    if (context && !context.hidden && context.title) {
      parts.push(
        context.classList.contains("conv-agent-context--warning")
          ? "Warning: " + context.title
          : context.title,
      );
    }
    if (issue && !issue.hidden) {
      parts.push("contains an exceptional child step");
    }
    toggle.setAttribute("aria-label", parts.join(". "));
  }

  _syncAgentContext(parentCallId, card) {
    const reading = this._agentContexts.get(parentCallId);
    if (!reading || card.wrap.dataset.state !== "running") return;
    const used = formatAgentContextTokens(reading.promptTokens);
    const total = formatAgentContextTokens(reading.contextWindow);
    if (!used || !total) return;
    const warning = agentContextIsWarning(
      reading.promptTokens,
      reading.contextWindow,
    );
    card.context.textContent = used + " / " + total;
    card.context.hidden = false;
    card.context.classList.toggle("conv-agent-context--warning", warning);
    const full =
      reading.promptTokens.toLocaleString() +
      " of " +
      reading.contextWindow.toLocaleString() +
      " context tokens used";
    card.context.title = full;
    this._syncAgentToggleLabel(card);
  }

  _resetAgentContextBadge(card) {
    card.context.hidden = true;
    card.context.textContent = "";
    card.context.classList.remove("conv-agent-context--warning");
    card.context.removeAttribute("title");
    this._syncAgentToggleLabel(card);
  }

  _updateAgentContext(evt) {
    const parentId =
      typeof evt.parent_call_id === "string" ? evt.parent_call_id : "";
    const promptTokens = Number(evt.prompt_tokens);
    const contextWindow = Number(evt.context_window);
    if (
      !parentId ||
      !Number.isInteger(promptTokens) ||
      promptTokens < 0 ||
      !Number.isInteger(contextWindow) ||
      contextWindow <= 0
    ) {
      return;
    }

    const route = this._agentTransientRoute(parentId);
    if (route.state === "drop") {
      this._agentContexts.delete(parentId);
      return;
    }
    if (route.state === "blocked") {
      this._agentContexts.set(parentId, {
        promptTokens,
        contextWindow,
        blockedByTerminalRow: route.row,
      });
      return;
    }

    this._agentContexts.set(parentId, { promptTokens, contextWindow });
    const card = this._ensureAgentCard(parentId, true);
    if (card && card.wrap.dataset.state !== "running") {
      this._agentContexts.delete(parentId);
    }
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
      const row = this._toolRow(it.call_id);
      const reading = this._agentContexts.get(it.call_id);
      const compaction = this._agentCompactions.get(it.call_id);
      if (
        (reading && reading.blockedByTerminalRow === row) ||
        (compaction && compaction.blockedByTerminalRow === row)
      ) {
        return;
      }
      if (reading && reading.blockedByTerminalRow) {
        delete reading.blockedByTerminalRow;
      }
      if (compaction && compaction.blockedByTerminalRow) {
        delete compaction.blockedByTerminalRow;
      }
      if (
        (this._agentCards && this._agentCards.has(it.call_id)) ||
        this._agentContexts.has(it.call_id) ||
        this._agentCompactions.has(it.call_id)
      ) {
        this._ensureAgentCard(
          it.call_id,
          this._agentContexts.has(it.call_id) ||
            this._agentCompactions.has(it.call_id),
        );
      }
    });
    if (paintedIds.length) this._flushAgentOrphans(paintedIds);
  }

  _updateAgentLabel(card) {
    const n = card.body.querySelectorAll(".conv-row").length;
    card.label.textContent = n === 1 ? "1 step" : n + " steps";
    this._syncAgentToggleLabel(card);
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
      const stepRow = buildToolDiv(synthToolItem(step), "");
      card.body.appendChild(stepRow);
      this._markAgentStepExceptional(stepRow, {
        isError: !!step.is_error,
        denied: !!step.denied,
        exceptional: !!step.contains_exceptional,
        effectStatus: step.effect_status,
      });
      const out = stripAnsi(String(step.output || "")).trim();
      if (out) {
        card.body.appendChild(renderCollapsibleOutput(out, !!step.is_error));
        const denied = step.denied || /^(Denied by user|Blocked)/.test(out);
        const fileChip = this._fileResultChip(
          stepRow, step.is_error, denied, step.effect_status,
        );
        if (fileChip) card.body.appendChild(fileChip);
      }
    });
    card.wrap.dataset.state = "done";
    this._updateAgentLabel(card);
    row.appendChild(card.wrap);
    return card.wrap;
  }

  _fileResultChip(row, isError, denied, effectStatus) {
    if (
      !row || !row._filePath || isError || denied ||
      (effectStatus && effectStatus !== "committed")
    ) {
      return null;
    }
    const chip = buildFileChip(row._filePath, (path) => this._host.onFiles(path));
    chip.hidden = !(hasScope("write") && hasPermission("files.read"));
    return chip;
  }

  appendToolOutput(callId, name, output, isError, preview, opts = {}) {
    const accepted = opts.accepted === true;
    // Folding uses the pre-mutation follow state; the deferred scroll also
    // checks for a newer user scroll before it moves the viewport.
    const stick = this.isNearBottom();
    // A task_agent's OWN result completing flips its card running -> done/error
    // (child sub-tool results carry namespaced ids, never keys of _agentCards).
    // The entry deliberately SURVIVES the result: a late child event (SSE
    // replay overlap) re-entering _ensureAgentCard with no Map entry would
    // build a duplicate empty card beside the finished one.  Entries hold
    // attached DOM (not a leak); the detached-retention hazard is rebuilds,
    // which _clearAgentTracking covers in replayHistory.
    if (this._agentCards && this._agentCards.has(callId)) {
      const agentCard = this._agentCards.get(callId);
      agentCard.wrap.dataset.state = isError ? "error" : "done";
      this._resetAgentContextBadge(agentCard);
      if (
        agentCard.wrap.dataset.contextOnly === "true" &&
        !agentCard.body.querySelector(".conv-row")
      ) {
        // A one-turn agent can report usage without ever invoking a sub-tool.
        // Retain the hidden card in the id-ownership map so a late child event
        // reuses it, but do not leave an empty "0 steps · done" card visible.
        agentCard.wrap.hidden = true;
      }
    }
    this._agentContexts.delete(callId);
    if (this._agentCompactions && this._agentCompactions.has(callId)) {
      resetCompactionHolder(this._agentCompactions.get(callId));
      this._agentCompactions.delete(callId);
    }
    let target = this._toolRow(callId);
    if (!target) {
      // A minted sub-agent child id ("<parent>::r{run}s{step}::<id>") whose row hasn't
      // nested yet must NOT graft its output onto the last top-level batch row
      // — that mislabels a sub-tool's result as a main-harness tool's.  Its row
      // arrives via the orphan flush; skip until then.
      if (callId && callId.includes("::")) return false;
      const blocks = this.messagesEl.querySelectorAll(".conv-batch");
      if (!blocks.length) return false;
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
    if (!target) return false;
    const parentBlock = target.closest(".conv-batch");
    const stripped = stripAnsi(output || "").trim();
    const isDenied =
      target.classList.contains("conv-batch--denied") ||
      (parentBlock && parentBlock.classList.contains("conv-batch--denied")) ||
      /^Denied by user/.test(stripped) ||
      /^Blocked/.test(stripped);
    this._markAgentStepExceptional(target, {
      isError: !!isError,
      denied: isDenied,
      effectStatus: opts.effectStatus,
    });

    if (accepted) {
      clearConvVerdictPending(target);
      setToolOutputReviewState(target, output);
      // Stop can synthesize a final result after tool_pending but before the
      // authoritative tool_info/approval event consumes the early shell. Once
      // this exact shell owns an accepted row it is committed transcript DOM,
      // not replaceable early paint. Retire map ownership without removing it;
      // a later turn may legitimately reuse the same provider call id.
      if (parentBlock && this.announcedBlocks) {
        for (const [key, announced] of this.announcedBlocks.entries()) {
          if (announced === parentBlock) {
            this.announcedBlocks.delete(key);
            break;
          }
        }
      }
      if (opts.effectStatus) {
        if (opts.effectStatus !== "committed" && parentBlock) {
          setConvBatchExpanded(parentBlock, true, { blocker: true });
        }
        target.dataset.effectStatus = String(opts.effectStatus);
      } else {
        delete target.dataset.effectStatus;
      }
    }

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

    // A preliminary receipt and its final accepted row own the same result
    // slot. Remove only nodes we created for THIS row; warnings and approval
    // badges are independent siblings and remain intact. A provider may reuse
    // call ids in a later turn, so never remove nodes tracked against an older
    // still-visible row.
    const priorResult = callId ? this._toolResultNodes.get(callId) : null;
    if (priorResult && priorResult.row === target) {
      priorResult.nodes.forEach((node) => {
        if (node && node.isConnected) node.remove();
      });
    } else if (callId) {
      this._toolResultNodes.delete(callId);
    }

    // Skip rendering for denied/blocked tool results — the ✗ denied
    // badge from resolveApproval already shows the denial reason; the
    // SSE tool_result event would otherwise duplicate the text.  Mirror
    // the guard in the history-replay path (the live path used to be
    // safe because no tool_result event was ever emitted for denied
    // items, but we now emit one so _tool_error_flags gets set).
    const resultNodes = [];
    let insertCursor = target;
    const insertResult = (node) => {
      insertCursor.after(node);
      insertCursor = node;
      resultNodes.push(node);
    };

    if (!isDenied && stripped) {
      let resultNode = null;
      // Detect structured media output and render interactive embed.
      if (!isError) {
        const media = tryParseMedia(stripped);
        if (media) resultNode = buildMediaEmbed(media, stripped);
      }

      // Detect structured MCP error envelopes before the plain renderer. The
      // shared consent card remains the canonical error presentation.
      if (!resultNode && isError) {
        const mcpErr = tryParseMcpError(stripped);
        if (mcpErr) {
          resultNode = buildMcpErrorEmbed(mcpErr, stripped, (s) =>
            this._host.onConsentDetected(s),
          );
        }
      }

      if (!resultNode) {
        resultNode = renderCollapsibleOutput(stripped, isError);
      }
      insertResult(resultNode);
    }

    if (!isDenied) {
      const images = buildToolImages(opts.attachments, {
        wsId: this.wsId,
        base: this._base,
      });
      if (images) insertResult(images);
    }

    // Mark the parent approval block as errored. Idempotent badge construction
    // preserves the approval verdict beside the error disposition.
    if (
      isError &&
      !isDenied &&
      parentBlock &&
      !parentBlock.classList.contains("conv-batch--denied")
    ) {
      setConvBatchExpanded(parentBlock, true, { blocker: true });
      parentBlock.classList.add("conv-batch--error");
      appendToolErrorBadge(parentBlock);
    }

    // Accepted preview rows mirror /history: the reopen chip survives even on
    // a cancelled/error result, but accepted publication never auto-opens a
    // pane. The provisional receipt remains the sole focused auto-open edge.
    if (preview && !isDenied && (!isError || accepted)) {
      const chip = buildPreviewChip(preview, (d) => this._host.onPreview(d));
      insertResult(chip);
      if (!accepted && !isError && this._host.isFocused(this)) {
        this._host.onPreview(preview);
      }
    }
    const fileChip =
      !callId || target.dataset.callId === callId
        ? this._fileResultChip(target, isError, isDenied, opts.effectStatus)
        : null;
    if (fileChip) insertResult(fileChip);

    if (callId) {
      this._toolResultNodes.set(callId, { row: target, nodes: resultNodes });
    }
    let settlement = null;
    if (accepted) {
      const compact = getTranscriptPresentation() === "compact";
      const allowAutoFold =
        !compact ||
        canAutoFoldTranscriptBatch(this.messagesEl, parentBlock, {
          atBottom: stick,
        });
      settlement = markConvRowResultSettled(target, {
        autoFold: allowAutoFold,
      });
      if (compact && settlement.autoFolded) {
        toolAnnounce("Completed: " + convBatchSummaryText(parentBlock));
      }
    }
    if (resultNodes.length || (settlement && settlement.autoFolded)) {
      this.scrollToBottom();
    }
    return true;
  }

  sendMessage() {
    const text = this.inputEl.value.trim();
    if (!text) {
      if (!this.attachments.isEmpty()) {
        this.addInfoMessage("Add a message to send with this attachment.");
      }
      return;
    }

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
          // Thread {ok, status} alongside the parsed body — the loud
          // arms below ride NON-2xx codes (busy = 409, error = 503), so
          // an /send-style throw-on-!ok pre-gate would wrongly reroute
          // them into the transport catch.
          r.json().then(
            (b) => ({ ok: r.ok, status: r.status, body: b || {} }),
            () => ({ ok: r.ok, status: r.status, body: {} }),
          ),
        )
        .then(({ ok, status, body }) => {
          // /compact dispatched onto an already-busy worker reports
          // {status: "busy"} — surface it (the optimistic busy guard
          // above can lose that race).
          if (body.status === "busy") {
            this.addInfoMessage(
              body.error || "Session is busy — try again shortly.",
            );
          } else if (body.status === "running") {
            // The command outlived the endpoint's completion backstop
            // (kept under the console proxy's client bound precisely so
            // this answer can traverse a proxied pane). The worker is
            // still going; its output and pane refreshes arrive over SSE.
            this.addInfoMessage(
              "Command is still running — results will appear here when it finishes.",
            );
          } else if (body.status === "error") {
            // 503: the command worker could not be started (thread
            // exhaustion) — the command did NOT run. Loud, like the busy
            // arm: silence here reads as success.
            this.addErrorMessage(
              body.error || "Command failed to start — retry shortly.",
            );
          } else if (!ok) {
            // Status-less non-2xx (404 unknown workstream, proxy 502
            // HTML): the same silence-reads-as-success rule as the arms
            // above. Rendered directly — not thrown into the catch,
            // which would double-prefix the message.
            this.addErrorMessage(
              body.error || "Command failed (HTTP " + status + ")",
            );
          }
        })
        .catch((err) => {
          // Transport failure (network drop, abort): the command-echo
          // chip is already rendered — say the command did not run.
          this.addErrorMessage("Command failed: " + err.message);
        });
      // Echo as a command chip, not addUserMessage — a slash command is
      // control-plane input, not a conversational user turn.
      this.addCommandEcho(text);
      this.composer.clear();
      return;
    }

    // Attachments cannot ride the live turn's text-only interjection queue.
    // Refuse before the optimistic clear so the user's companion text and
    // staged chips remain ready to send once the worker is idle.
    if (this.busy && !this.attachments.isEmpty()) {
      this.addInfoMessage(
        "Attachments can't be sent while the assistant is working. Wait for it to finish, then send again.",
      );
      return;
    }

    const isBusy = this.busy;
    let queuedEl = null;
    let optimisticEl = null;
    const clientSendId = mintClientSendId();
    const snap = this.attachments.snapshot();
    if (snap.uploading) {
      this.addInfoMessage(
        "Wait for attachments to finish uploading before sending.",
      );
      return;
    }

    // Display-only strip of the !!! prefix (the server re-parses it
    // authoritatively); shared parse so the settle helper's retro-convert
    // renders the same chip either pane would have built pre-POST.
    const { displayText, priority } = parsePriority(text);

    if (isBusy) {
      this.removeEmptyState();
      queuedEl = this.queue.addQueuedMessage(
        displayText,
        priority,
        clientSendId,
      );
    } else {
      // "optimistic": no server state event asserted this — the settle
      // arms may undo it if the send turns out deferred/refused (see
      // setBusy's busySource contract).
      this.setBusy(true, "optimistic");
      optimisticEl = this.addUserMessage(text, snap.attachments, {
        clientSendId,
      });
    }
    this.scrollToBottom(true);
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
        client_send_id: clientSendId,
      }),
    };
    let sendTimer = null;
    if (sendCtrl) {
      sendInit.signal = sendCtrl.signal;
      // Flat wedged-node bound: every /send answers within RTT now —
      // dispatched, queued, or deferred-with-msg_id during a command
      // window (the server parks nothing against this POST), so a
      // response slower than this means a wedged node, not a long
      // command.  Dismissal is bind() → DELETE, never a POST abort.
      sendTimer = setTimeout(() => sendCtrl.abort(), 15000);
    }
    let sendReq = authFetch(
      this._base +
        "/v1/api/workstreams/" +
        encodeURIComponent(this.wsId) +
        "/send",
      sendInit,
    );
    if (sendTimer) sendReq = sendReq.finally(() => clearTimeout(sendTimer));
    // Response normalization, the full status dispatch (queued/retro-convert,
    // busy, queue_full, attachments_busy, cross_user, unknown-ok) and the
    // accepted-guarded transport catch all live in the shared helper — ONE
    // send settle for both panes and both of each pane's send flows.
    postAndSettleSend(this.queue, sendReq, {
      queuedEl,
      optimisticEl,
      isBusy,
      displayText,
      priority,
      clientSendId,
      setBusy: (b) => this.setBusy(b),
      busyIsOptimistic: () => this.busy && this.busySource === "optimistic",
      paneIsBusy: () => this.busy,
      restoreInput: () => {
        this.composer.value = mergeRejectedComposerText(
          text,
          this.composer.value,
        );
      },
      renderError: (msg) => this.addErrorMessage(msg),
      consumeAttachments: (attached, droppedIds) =>
        this.attachments.consume(attached, droppedIds),
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
// ``{flags, risk_level, annotations, redacted}``, and ``result`` when it is
// about one of a step's several results.  Reuses the tool-result warning chip
// (``_buildOutputWarningEl``) for the risk / flags / result / redaction header
// so the operator-context finding speaks the same visual vocabulary, then
// appends the annotations (matched-pattern detail) — which the inline tool
// chip omits to stay terse.  All text via textContent.
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
  script.src = "/shared/hls-1.7.4/hls.min.js";
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
  head.appendChild(buildConvBatchDisclosure());
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
  if (
    ["write_file", "edit_file"].includes(item.func_name) &&
    typeof item.file_path === "string" &&
    item.file_path.length > 0 && item.file_path.length <= 4096
  ) {
    row._filePath = item.file_path;
  }
  row.appendChild(buildConvCmd(item));
  attachConvFullCall(row, item.arguments);
  return row;
}

// Synthesize the live `item` shape ({func_name, call_id, header, arguments})
// from a stored tool_call ({name, id, arguments}) so /history replay AND
// task-agent card recall render the SAME .conv-row as the live path.  Header
// derivation mirrors the live serialize: bash shows the command; other tools
// show `key: value` lines, values clipped at 80 chars; unparseable args fall
// back to a raw clip.  The whole text rides along as `arguments` for the
// row's "full call".
function synthToolItem(tc) {
  tc = tc || {};
  let header = "";
  let filePath = tc.file_path;
  try {
    const args = JSON.parse(tc.arguments);
    if (!filePath && args) filePath = args.path;
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
  return {
    func_name: tc.name,
    call_id: tc.id || "",
    header,
    file_path: filePath,
    arguments: typeof tc.arguments === "string" ? tc.arguments : undefined,
  };
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

// An output box with its "show all" control when the output runs past
// `foldLines` lines (a long line counting as several): it starts folded to a
// short preview (0: any output does).  Shorter output shows whole.  Box and
// control travel as one node (.conv-result-wrap), since results are inserted
// and replaced whole.
function withResultExpand(out, foldLines) {
  const expand = buildConvResultExpand(out, {
    text: out.textContent,
    boxLines: foldLines,
    fold: true,
  });
  if (!expand) return out;
  const wrap = document.createElement("div");
  wrap.className = "conv-result-wrap";
  wrap.append(out, expand);
  return wrap;
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
    const pretty = tryPrettyJson(stripped);
    if (pretty) {
      out.textContent = pretty;
      return out;
    }
  }
  out.textContent = redactCredentials(stripped);
  return out;
}

// Shared recipe: render tool output and fold it past 10 lines (a long line
// counting as several) behind "show all".  The one source for the live
// appendToolOutput's plain-output path, the /history tool-result replay, and
// the task-agent card recall — so the threshold can't drift between them.
function renderCollapsibleOutput(stripped, isError) {
  return withResultExpand(renderToolOutput(stripped, isError), 10);
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
  raw.textContent = tryPrettyJson(rawJson) || redactCredentials(rawJson);
  wrapper.appendChild(withResultExpand(raw, 0));

  return wrapper;
}

// tryParseMcpError / buildMcpErrorEmbed moved to the shared mcp_error.js
// module (#725) so the coordinator pane renders the same consent /
// forbidden / operator card — imported at the top of this file.

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

  const host = {
    // Only the visible tab steals focus — never yank the caret into a
    // backgrounded pane mid background-replay.
    isFocused() {
      return active;
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
    onFiles(path = null) {
      if (window.TS_SHELL && typeof window.TS_SHELL.openFiles === "function") {
        window.TS_SHELL.openFiles({ base: base, wsId: wsId }, path);
      }
    },
  };

  const pane = new Pane(wsId, {
    base,
    host,
    onClose: opts.onClose,
  });
  root.appendChild(pane.el);
  // The stream gave up for good (dead is terminal and announced once): the
  // shell paints its reconnect affordance.  Recovery is the shell's revive
  // (re-resolve the node, reopen, rebuild the pane), never automatic, so a
  // deliberately closed session is not resurrected by a timer; a dead pane
  // also ignores re-login, because its recovery may need a different node.
  pane.sync.on("status", (phase) => {
    if (phase !== "dead" || typeof opts.onDead !== "function") return;
    try {
      opts.onDead();
    } catch (e) {
      console.error("interactive pane: onDead callback failed", e);
    }
  });

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
        // Load-bearing pre-seed (#890): a failed first load renders nothing,
        // so a FAILED first paint shows this placeholder only because it is
        // seeded here, before the load.  Removing it would leave a genuinely
        // blank pane when the first /history fails.
        pane.showEmptyState();
        pane.sync.start();
      }
    },
    // Tab backgrounded — stop stealing focus, keep the stream live.
    deactivate() {
      active = false;
    },
    // Re-auth fan-out: the stream may work again.  The core resumes only a
    // stream that failed (signed out, backing off); on a live stream whose
    // transcript is latched (its refetches failed while signed out) it arms
    // the one stale retry.  A healthy live stream, a reload, a cooldown, a
    // repair and a dead pane are left alone.
    onLogin() {
      pane.sync.retryNow();
    },
    // Terminal-state surface for the shell: `isDead()` gates revive-vs-connect
    // on activate and reopen.
    isDead() {
      return pane.sync.dead;
    },
    // Full teardown: the stream core (stream, timers, requests, listener),
    // the turn's timers, voice I/O and reasoning clock, then the DOM.  A
    // backgrounded pane must not leak an upstream node connection.
    destroy() {
      active = false;
      connected = false;
      pane.sync.destroy();
      pane._stopTurnActivity();
      // Cancel orphan grace timers so a post-destroy escape can't paint into
      // the detached pane / shared announcer, release the card maps, and stop
      // observing the detached scroller.
      pane._clearAgentTracking();
      pane._scrollFollow.destroy();
      if (pane._unregisterTranscriptScroller) {
        pane._unregisterTranscriptScroller();
        pane._unregisterTranscriptScroller = null;
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
