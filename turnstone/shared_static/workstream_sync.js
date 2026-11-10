// Workstream stream and recovery: one implementation for every pane that shows a workstream.
//
// A WorkstreamSync keeps one pane's transcript in step with one workstream, for the life of the
// pane. It renders /history, holds the per-workstream event stream, and repairs the transcript
// after anything the stream could not replay: a dropped or refused connection, a truncated
// replay, a send-queue overflow, a conversation restructure (clear_ui), a stale history handoff.
// The pane supplies a view (how to apply one event, how to rebuild from /history) and listens to
// the events below; when to fetch, when to connect and what to present is decided here, once.
//
// It issues exactly two requests, GET .../history and GET .../events, and never opens, reopens or
// rehydrates a workstream. GET .../workstreams/{id} rehydrates an unloaded workstream for a
// writer, so probing it from a timer would resurrect a session someone closed on purpose. Once
// the instance is `dead`, recovery is the pane's: destroy this instance and build a new pane,
// which is where a deliberate reopen (possibly on another node) belongs.
//
// The transcript-staleness latch (historyStale) is cleared at the render step, before the pane's
// render: a fetch that fails leaves it set, and a render that throws after its wipe leaves it
// clear, because a fresh transcript, at worst partial, under-counts rows (the safe direction for
// rewind and edit) where the stale one could over-count them.
//
// Globals (EventSource, document, setTimeout, clearTimeout, Date.now, queueMicrotask) are read at
// call time so tests and the browser harness can replace them; nothing touches the DOM at import.
// View methods are called through the view object on every call, so a wrapper installed after
// construction takes effect.
//
// View contract (the same definitions in every pane):
//   dispatch(event)           apply one content event to the transcript
//   renderHistory(messages)   wipe the transcript and rebuild it from /history, synchronously;
//                             the wipe is its first act, and everything per-transcript (tool rows,
//                             dedup sets, approvals, agent tracking) resets inside it; throws on
//                             a render bug
//   resetLiveState()          drop the reply bubble refs and their buffers, nothing else
//   liveState()               { streaming, busy, sending }: a reply or reasoning bubble ref is
//                             set; the pane is busy; busy because of the user's own optimistic send
//   placeRepairPrompt(el)     attach the parked-repair prompt and scroll to it
//
// Events (on(name, fn) returns an unsubscribe; a throwing listener is logged and never stops
// another listener or the core):
//   status          the phase, on every change
//   missed          replay cannot vouch for continuity, so state the stream does not carry (a
//                   sidebar) should be re-read
//   reloaded        { first, outcome } after a seeded load settles, before the connect (a
//                   promoted refetch is a render, not a load, and emits none)
//   reset-settled   { outcome, error? } exactly once per clear_ui, also after destroy and also for
//                   a clear_ui still queued when a reload, a promotion or a teardown drops the
//                   queue; outcome is rendered (seedless or promoted), declined, failed,
//                   superseded or threw

import {
  DEGRADED_COOLDOWN_BASE_MS,
  DEGRADED_COOLDOWN_MAX_MS,
  DEGRADED_COOLDOWN_RESET_MS,
  OVERFLOW_TRIP_COUNT,
  OVERFLOW_TRIP_WINDOW_MS,
  STALE_RETRY_BASE_MS,
  STALE_RETRY_JITTER_MS,
  TRUNCATED_RESYNC_JITTER_MS,
  degradedCooldownStep,
  overflowWindowTripped,
} from "./sse_overflow.js";
import {
  HISTORY_HANDOFF_FETCH_TIMEOUT_MS,
  createHistoryHandoffDeadline,
  createHistoryHandoffRepair,
} from "./history_handoff.js";

// One status line per phase, so each string exists once and both panes show the same words. An
// empty string means the pane shows its own counters (cached usage, or its placeholder).
export const STREAM_STATUS_TEXT = Object.freeze({
  idle: "",
  loading: "Loading history…",
  connecting: "Connecting…",
  live: "",
  reconnecting: "Reconnecting…",
  backoff: "Reconnecting…",
  paused: "Paused while the tab is hidden",
  degraded: "Connection is slow — catching up…",
  repairing: "History out of date — retrying…",
  parked: "Live updates paused",
  auth: "Sign in to reconnect",
  dead: "Disconnected",
  destroyed: "",
});

// A refused connection (the browser gave up: CLOSED) is one strike and the core redials after a
// backoff; a connection the browser is still re-establishing on its own is left to it.
const REDIAL_BASE_MS = 1000;
const REDIAL_MAX_MS = 30000;
const REDIAL_JITTER_MS = 500;
// Consecutive refusals before the instance is dead: the first stream plus six redials (1, 2, 4, 8,
// 16 and 30 s apart), about a minute. That rides out a node restart of up to a minute; a
// workstream gone for longer needs the pane rebuilt, and possibly reopened on another node.
const STRIKE_LIMIT = 7;
// Every /history request is bounded, starting at 15 s; each expiry in a row doubles the next
// bound up to this cap, and any payload resets it. The server finishes a reconstruction even
// when the client aborts, so a retry that cannot outlast the server's latency only multiplies
// its work.
const HISTORY_DEADLINE_MAX_MS = 120000;

const EVENT_NAMES = new Set(["status", "missed", "reloaded", "reset-settled"]);
const VIEW_METHODS = [
  "dispatch",
  "renderHistory",
  "resetLiveState",
  "liveState",
  "placeRepairPrompt",
];
const LOG = "workstream sync: ";

function workstreamPath(base, wsId) {
  return (base || "") + "/v1/api/workstreams/" + encodeURIComponent(wsId);
}

// The /events URL for one connect. The cursor gate is `!= null`, not truthiness: cursor 0 is
// valid (the ring's first event is id 1, so replaying from 0 rebuilds a brand-new workstream's
// first turn; a truthiness gate would fall back to the lossy fresh snapshot). The capability
// params ride every URL so the browser's own reconnects keep them; without them the server
// projects user_turn and accepted tool rows into repair frames for reducers that lack them. The
// handoff token goes last.
export function buildEventsUrl({ base = "", wsId, cursor = null, token = null }) {
  let url = workstreamPath(base, wsId) + "/events?";
  if (cursor != null) url += "last_event_id=" + encodeURIComponent(cursor) + "&";
  url += "user_turn=1&tool_turn=1";
  if (token != null) url += "&history_token=" + encodeURIComponent(token);
  return url;
}

// Backoff before redialling after the strike-th consecutive refusal: 1 s doubling to a 30 s
// cap, plus up to 500 ms of spread so tabs that lost the same node do not redial in lockstep.
// `random` is a number in [0, 1), passed in so the policy stays pure.
export function redialDelayMs(strike, random) {
  const doublings = Math.max(0, strike - 1);
  return (
    Math.min(REDIAL_MAX_MS, REDIAL_BASE_MS * 2 ** doublings) +
    Math.floor(random * REDIAL_JITTER_MS)
  );
}

// Whether a refetched /history may replace the transcript under a live stream (a seedless render,
// which leaves the stream cursor where it is, or a promoted one). Each term guards a different way
// the snapshot and the stream can disagree:
// - open: a stream that is down, reconnecting or CLOSED has a frozen cursor, so a render would
//   paint rows the cursor still sits below and the next connect would replay them on top.
// - sameConnection: a stream that dropped and finished re-establishing during the fetch reads
//   OPEN again, but its replay may already carry what the snapshot holds (#900). Only a
//   connection counter sees it: a native reconnect reuses the same EventSource object.
// - streaming: the render would wipe the bubble a reply is streaming into.
// - sending: the user's own send is not a stream event, so its row may be newer than the snapshot.
// A response that carries a cursor is not a term: when every term holds it renders too, promoted to
// the seeded path (#promote). A declined render is not lost work: the staleness latch stays set and
// the stale retry or the idle backstop renders later. Known false decline, accepted: a reconnect
// inside the fetch that answered fresh or truncated carried no ring content, so its render would
// have been safe.
// KNOWN RESIDUAL, blocked server-side (#903): a refetch dispatched between `open` and the arrival
// of the replay slice sees the counter already bumped and still renders past the frozen cursor;
// replay_ok sends no end-of-replay marker, so nothing on the client can gate on it.
export function seedlessRenderAllowed({ open, sameConnection, streaming, sending }) {
  return !!open && !!sameConnection && !streaming && !sending;
}

// Named timeouts. A timer's handle is cleared before its callback runs, so work a timer starts can
// tear the transport down (which cancels timers by name) without cancelling itself.
class NamedTimers {
  #handles = new Map();

  arm(name, delayMs, callback) {
    this.cancel(name);
    const handle = setTimeout(() => {
      this.#handles.delete(name);
      callback();
    }, delayMs);
    this.#handles.set(name, handle);
  }

  armed(name) {
    return this.#handles.has(name);
  }

  cancel(...names) {
    for (const name of names) {
      if (!this.#handles.has(name)) continue;
      clearTimeout(this.#handles.get(name));
      this.#handles.delete(name);
    }
  }

  cancelAll() {
    this.cancel(...this.#handles.keys());
  }
}

export class WorkstreamSync {
  #wsId;
  #base;
  #fetch;
  #view;
  #listeners = new Map();
  #timers = new NamedTimers();
  // Owns the document visibility listener; aborted by the terminal teardown.
  #lifetime = new AbortController();
  // The visibility listener is installed and not yet removed.
  #listening = false;
  // { controller, deadline } for every /history request in flight. A request leaves the set only
  // through its deadline's dispose, the one retirement path, so none outlives the instance.
  #inflight = new Set();
  #repair;

  // The phase, written only by #setPhase:
  //   idle          constructed, start() not called
  //   loading       a seeded reload is in flight; the stream is down on purpose
  //   connecting    a stream was built and has not opened (straight from live when a seedless
  //                 refetch is promoted)
  //   live          the stream is open
  //   reconnecting  the stream dropped and the browser is re-establishing it on its own
  //   backoff       the browser refused or gave up (CLOSED); the redial timer is armed
  //   paused        the tab is hidden; the next connect waits for the show edge
  //   degraded      overflow cooldown; the stream is down on purpose
  //   repairing     the handoff repair owns recovery; no stream until it settles
  //   parked        the repair budget is spent; the prompt in the transcript owns recovery
  //   auth          the stream closed while the login overlay is up
  //   dead          seven consecutive refusals; terminal, still inspectable
  //   destroyed     terminal
  #phase = "idle";

  #source = null;
  // Whether the current source presents a cursor to the server: its URL carried one, or an
  // id-bearing frame arrived on it (the browser then sends Last-Event-ID on its reconnects).
  #sourcePresentsCursor = false;
  // The last event id seen, captured from each MessageEvent (or seeded from /history). Ids can
  // arrive lower than the cursor without any reset (the server fans out after stamping), so the
  // cursor simply follows the last id seen and nothing infers a gap from the numbers.
  #cursor = null;
  // Where a replay_truncated envelope arrived: a gap of lost events exists below this cursor.
  // Keep-oldest: a retry's envelope for the same unrepaired gap must not advance it. Only the
  // render step clears it (see the latch). Its one consumer is the connect chokepoint, which
  // presents it instead of the live cursor, so the server re-answers replay_truncated and the
  // resync re-arms whichever teardown cancelled the pending one (hide, cooldown, failed fetch).
  #gapCursor = null;
  // One-shot REST to stream proof (#1005): the revision the last seeded /history render showed.
  // The next stream built consumes it; the browser's own reconnects on that source carry
  // Last-Event-ID instead, and no later source reuses it.
  #handoffToken = null;
  // Bumped by every seeded reload and by the terminal teardown. Every continuation after an await
  // compares it, so the newest load wins and a dead or destroyed instance renders, connects and
  // arms nothing. Every future terminal path must bump it too.
  #generation = 0;
  // Connection counter (#900), bumped in the open listener and nowhere else: `open` is the only
  // signal that fires for the browser's own reconnects as well as ours, and a deferred connect
  // establishes nothing. Must stay a number; undefined would decline every seedless render.
  #epoch = 0;
  #strikes = 0;
  #historyDeadlineMs = HISTORY_HANDOFF_FETCH_TIMEOUT_MS;
  // Events held while a seedless refetch is in flight (null otherwise). Armed synchronously before
  // the fetch's await, owned by the refetch that armed it, and flushed through the router after
  // the render (or the decline) in a finally, so a throw cannot wedge it; a promotion drops it
  // instead, since the replay from the adopted cursor redelivers its events. Because every seedless
  // refetch arms it first and everything else waits behind it, two seedless refetches never
  // overlap in one generation, which is why no per-dispatch sequence stamp exists; add one only if
  // a seedless refetch ever starts without arming this queue.
  #queue = null;
  // A replay_truncated arrived while a reply was streaming: resync at the next idle edge instead
  // of rebuilding under the live bubble.
  #resyncPending = false;
  // Transcript-staleness latch (#890, #894): the transcript no longer matches the server's
  // conversation structure. Set when clear_ui arrives (before the refetch) and by the handoff
  // repair; cleared only at the render step, just before the pane's render (the header says why:
  // a failed fetch keeps it set, a render that throws after its wipe does not re-close the pane).
  // Mutating row affordances (rewind, edit, edit-and-resend) gate on it alongside busy, through
  // the historyStale getter. A seeded reload does not set it: first paint renders into an empty
  // pane, the truncated resync heals through its recorded cursor. Accepted residual: after a
  // double fetch failure it stays set until the next idle-edge heal (safe-closed beats
  // destructive-open), and nothing a reconnect replays may clear it, since a replay heals only
  // the turn in flight, never the committed structure the latch tracks.
  #historyStale = false;
  #backstopQueued = false;
  // Overflow ladder state; the trip and cooldown math lives in sse_overflow.js.
  #overflowTimes = [];
  #degradedCooldownMs = DEGRADED_COOLDOWN_BASE_MS;
  #lastDegradedAt = 0;
  // Field counters. Each log line at an increment carries the running total, so a field report
  // shows which class fired: overflows (the server dropped events) versus renderThrows (a
  // dispatch threw), truncatedGaps (replay-window misses acted on, not resyncs performed) and
  // foreignFrames (frames stamped for another workstream). A render throw the pane catches itself
  // (finalizing a reply segment) never reaches the core, so the pane counts it.
  #health = {
    opens: 0,
    malformedFrames: 0,
    renderThrows: 0,
    overflows: 0,
    truncatedGaps: 0,
    foreignFrames: 0,
  };

  constructor({ wsId, base = "", fetch, view } = {}) {
    if (!wsId) throw new TypeError("WorkstreamSync: wsId is required");
    if (typeof fetch !== "function") throw new TypeError("WorkstreamSync: fetch is required");
    for (const name of VIEW_METHODS) {
      if (!view || typeof view[name] !== "function") {
        throw new TypeError("WorkstreamSync: view." + name + " is required");
      }
    }
    this.#wsId = wsId;
    this.#base = base || "";
    this.#fetch = fetch;
    this.#view = view;
    // The repair policy (budget, backoff, prompt, verdict) is shared in history_handoff.js; the
    // core supplies the transport and the transcript.
    this.#repair = createHistoryHandoffRepair({
      baseDelayMs: STALE_RETRY_BASE_MS,
      jitterMs: STALE_RETRY_JITTER_MS,
      maxMs: DEGRADED_COOLDOWN_MAX_MS,
      isAlive: () => !this.#terminal(),
      load: (_scope, manualAttempt) => this.#reload({ manual: manualAttempt }),
      connect: () => this.#connect(),
      setStale: (stale) => {
        this.#historyStale = stale;
      },
      deferToShowEdge: () => this.#setPhase("paused"),
      showPaused: () => this.#setPhase("parked"),
      placePrompt: (prompt) => {
        // The parked phase announced just before may have torn the instance down.
        if (!this.#terminal()) this.#view.placeRepairPrompt(prompt);
      },
    });
  }

  get status() {
    return this.#phase;
  }

  get dead() {
    return this.#phase === "dead";
  }

  get historyStale() {
    return this.#historyStale;
  }

  // A read-only snapshot for tests and the browser harness.
  inspect() {
    return {
      source: this.#source,
      cursor: this.#cursor,
      status: this.#phase,
      historyStale: this.#historyStale,
      quiesced: this.#queue != null,
      listening: this.#listening,
      health: { ...this.#health },
    };
  }

  on(name, listener) {
    if (!EVENT_NAMES.has(name)) throw new TypeError("WorkstreamSync: no event " + name);
    if (!this.#listeners.has(name)) this.#listeners.set(name, new Set());
    this.#listeners.get(name).add(listener);
    return () => this.#listeners.get(name).delete(listener);
  }

  // First paint: the seeded reload, then the connect, whatever the load's outcome.
  start() {
    if (this.#phase !== "idle") return;
    // Installed before anything can defer a connect to the show edge, so the deferral has an edge
    // to land on.
    document.addEventListener("visibilitychange", () => this.#onVisibilityChange(), {
      signal: this.#lifetime.signal,
    });
    this.#listening = true;
    this.#reload({ first: true });
  }

  // "The stream may work now": the login fan-out, or the user asked to reconnect. It redials only
  // where the stream is down because it failed, never where it is down on purpose (a reload, a
  // cooldown, a repair, a hidden tab) and never on a dead instance, whose recovery is the pane
  // rebuild (it may need a different node). A healthy stream is left alone: an open EventSource
  // survives cookie expiry. But a live stream whose transcript is latched (its refetches failed
  // while signed out) gets the one stale retry, which fires only on an open, idle stream: no
  // reload, no close.
  retryNow() {
    if (this.#phase === "backoff" || this.#phase === "auth") {
      this.#strikes = 0;
      this.#connect();
      return;
    }
    if (this.#phase === "live") this.#armStaleRetry(this.#generation);
  }

  destroy() {
    if (this.#phase === "destroyed") return;
    this.#terminate("destroyed");
  }

  // --- phase and events ------------------------------------------------------------------------

  // A status listener may tear the instance down, so work after a phase change re-checks the
  // source, the generation or the terminal state (most transitions make the change their last
  // act). A terminal phase is never left, except that a dead instance may still be destroyed.
  #setPhase(next) {
    if (this.#phase === next) return;
    if (this.#terminal() && !(this.#phase === "dead" && next === "destroyed")) return;
    this.#phase = next;
    this.#emit("status", next);
  }

  #terminal() {
    return this.#phase === "dead" || this.#phase === "destroyed";
  }

  #emit(name, payload) {
    const listeners = this.#listeners.get(name);
    if (!listeners) return;
    for (const listener of [...listeners]) {
      try {
        listener(payload);
      } catch (error) {
        console.error(LOG + "a " + name + " listener failed", error);
      }
    }
  }

  // Dead and destroyed share one teardown. Dead, not merely inert: the generation bump makes
  // every pending continuation a no-op, and the teardown also releases what would otherwise
  // outlive the instance (timers, requests, the repair, the queue). The visibility listener goes
  // too, or a tab hidden before the end would reconnect the workstream on show. The queue drops
  // after the phase change, so a reset-settled listener finds the instance already terminal and
  // cannot reopen a stream from inside the teardown.
  #terminate(phase) {
    this.#generation += 1;
    this.#abortFetches();
    this.#repair.clear();
    this.#closeTransport();
    this.#timers.cancelAll();
    this.#lifetime.abort();
    this.#listening = false;
    this.#setPhase(phase);
    this.#dropQueue();
  }

  // Drop the held events. The snapshot of whatever replaces them covers their content, but a
  // queued clear_ui still settles (its rewind committed server-side, and an edit may wait on it),
  // so reset-settled stays exactly once per clear_ui.
  #dropQueue() {
    const queue = this.#queue;
    this.#queue = null;
    if (queue) this.#settleDropped(queue);
  }

  #settleDropped(events) {
    for (const event of events) {
      if (event.type === "clear_ui") this.#emit("reset-settled", { outcome: "superseded" });
    }
  }

  // --- transport -------------------------------------------------------------------------------

  // Transport teardown: close the stream and cancel the timers that would reopen it (the redial,
  // the cooldown, a pending jittered resync); a torn-down stream's scheduled resync must not fire
  // against whatever connects next, and if the gap still exists the next connect's envelope
  // reschedules it. Deliberately kept: the stale retry and the event queue. The retry is a REST
  // heal, not transport state, so a transport-only redial keeps that intent, which is why its
  // fire guard requires an open stream; the queue is released only by the refetch that owns it.
  // Terminal cleanup is #terminate's.
  #closeTransport() {
    this.#timers.cancel("redial", "degraded", "resync");
    if (this.#source) {
      this.#source.close();
      this.#source = null;
    }
  }

  // The connect chokepoint: every stream is built here, so every rule below backstops every
  // caller (first paint, show edge, redial, cooldown, resync, repair).
  #connect() {
    if (this.#terminal()) return;
    // Teardown first: a pending cooldown, resync or redial must not open a second stream.
    this.#closeTransport();
    // Never stream into a hidden tab: its throttled event loop is the likeliest slow consumer to
    // overflow the server's send queue, and an idle hidden tab holds a connection for nothing.
    // The show edge connects.
    if (document.hidden) {
      this.#setPhase("paused");
      return;
    }
    // Never fail open from a history mismatch into a cursorless or tokenless stream: only a
    // completed /history render ends the repair and supplies the token for the next stream.
    if (this.#repair.isRepairing(this.#wsId)) {
      this.#repair.schedule();
      this.#setPhase("repairing");
      return;
    }
    // A recorded truncation gap overrides the live cursor. Safe against a double render because
    // ring eviction is forward-only: a cursor once truncated never re-enters replay range, so
    // this connect always draws the envelope, never a bulk replay.
    const cursor = this.#gapCursor != null ? this.#gapCursor : this.#cursor;
    const url = buildEventsUrl({
      base: this.#base,
      wsId: this.#wsId,
      cursor,
      token: this.#handoffToken,
    });
    let source;
    try {
      source = new EventSource(url);
    } catch (error) {
      // Counted like a refused connection, so the strike budget bounds it.
      console.error(LOG + "could not open the event stream", error);
      this.#refused();
      return;
    }
    // The token is consumed only once a source exists to carry it; a deferred or refused connect
    // keeps it for the next real one.
    this.#handoffToken = null;
    this.#source = source;
    this.#sourcePresentsCursor = cursor != null;
    source.addEventListener("open", () => this.#onOpen(source));
    source.addEventListener("message", (event) => this.#onMessage(source, event));
    source.addEventListener("error", () => this.#onError(source));
    this.#setPhase("connecting");
  }

  #onOpen(source) {
    if (source !== this.#source) return;
    this.#epoch += 1;
    const firstOpen = this.#health.opens === 0;
    this.#health.opens += 1;
    this.#strikes = 0;
    // A reconnect that presented no cursor got a fresh replay, which cannot vouch for what
    // happened while the stream was down. With a gap on record the connect presented the gap
    // cursor, and the envelope already said so.
    const missed = !firstOpen && !this.#sourcePresentsCursor && this.#gapCursor == null;
    this.#setPhase("live");
    if (missed && source === this.#source) this.#emit("missed");
  }

  // Never close the source on an error: the browser's own reconnect resumes with Last-Event-ID
  // and the server replays the gap, and closing would force CLOSED and defeat it. No timer has to
  // look again later: the browser marks a failed connection CLOSED before it dispatches the error
  // (the harness's browser contract run checks that rule), so CONNECTING here means the browser
  // is re-establishing the stream itself.
  #onError(source) {
    if (source !== this.#source) return;
    if (source.readyState !== EventSource.CLOSED) {
      this.#setPhase("reconnecting");
      return;
    }
    this.#refused();
  }

  // The browser refused or gave up on the stream, or none could be built. While the login overlay
  // is up that is neither retried nor a strike: the page's auth layer owns the overlay, and
  // retryNow() resumes after the login. Otherwise it is one strike: dead at the limit, else a
  // redial after the backoff.
  #refused() {
    this.#source = null;
    if (this.#loginOverlayShown()) {
      this.#setPhase("auth");
      return;
    }
    this.#strikes += 1;
    if (this.#strikes >= STRIKE_LIMIT) {
      this.#terminate("dead");
      return;
    }
    this.#timers.arm("redial", redialDelayMs(this.#strikes, Math.random()), () => this.#connect());
    this.#setPhase("backoff");
  }

  #loginOverlayShown() {
    const overlay = document.getElementById("login-overlay");
    return !!overlay && overlay.style.display !== "none";
  }

  // Close on hide, reconnect on show. Hide acts only when a stream exists: while a reload, a
  // cooldown or a repair holds the transport down, their own timers defer to the show edge when
  // they fire hidden. Show acts only in `paused`, so it cannot open a stream any of them hold
  // down. The reply refs survive the teardown, so the visible tail is intact on return.
  #onVisibilityChange() {
    if (document.hidden) {
      if (!this.#source) return;
      this.#closeTransport();
      this.#setPhase("paused");
    } else if (this.#phase === "paused") {
      this.#connect();
    }
  }

  // --- incoming frames -------------------------------------------------------------------------

  #onMessage(source, event) {
    if (source !== this.#source) return;
    // The cursor comes from the MessageEvent, before parsing: the id lives on the event, never on
    // the source object, and a malformed frame still advanced the browser's own Last-Event-ID, so
    // the cursor of a manual reconnect must advance with it. Frames without an id carry "".
    const id = event.lastEventId;
    if (id != null && id !== "") {
      this.#cursor = id;
      this.#sourcePresentsCursor = true;
    }
    let data = null;
    try {
      data = JSON.parse(event.data);
    } catch (error) {
      this.#dropMalformedFrame(error);
      return;
    }
    if (data === null || typeof data !== "object") {
      this.#dropMalformedFrame(new TypeError("frame data is not an object"));
      return;
    }
    // The server fills in a missing ws_id with the stream's own, and no per-workstream event sets
    // another (child events on a coordinator stream carry the child in child_ws_id), so a different
    // ws_id means a server fault, and painting another workstream's event into this pane is worse
    // than dropping it.
    if (data.ws_id && data.ws_id !== this.#wsId) {
      this.#health.foreignFrames += 1;
      console.warn(
        LOG +
          "dropping a frame stamped for workstream " +
          data.ws_id +
          " (total " +
          this.#health.foreignFrames +
          ")",
      );
      return;
    }
    // The event's own id, for dedup against rows /history already painted.
    if (id) data._event_id = id;
    // Neither reads view state, so both act on arrival, never queued behind a render.
    if (data.type === "stream_overflow") {
      this.#noteStreamOverflow();
      return;
    }
    if (data.type === "history_resync") {
      // The revision changed after /history rendered and before this stream registered. A
      // numeric replay is no substitute for a committed row, and the repair survives failed
      // fetches: no cursorless or tokenless stream reopens until a fresh proof renders.
      this.#repair.begin(this.#wsId);
      return;
    }
    if (data.type === "replay_truncated") {
      this.#recordTruncationGap();
      // A `missed` listener may have torn the instance down.
      if (source !== this.#source) return;
    }
    this.#routeGuarded(data);
  }

  #dropMalformedFrame(error) {
    this.#health.malformedFrames += 1;
    console.warn(
      LOG + "dropping malformed SSE frame (total " + this.#health.malformedFrames + ")",
      error,
    );
  }

  // Record where the gap sits on arrival, before anything is queued: by the time the resync runs
  // (after the jitter, or at the idle edge) the live cursor has moved past it.
  #recordTruncationGap() {
    if (this.#gapCursor != null) return;
    this.#gapCursor = this.#cursor;
    this.#emit("missed");
  }

  // Every routed event is guarded: one throwing handler must not drop the events after it, and an
  // exception escaping a stream listener would leave the reply refs stale so every later turn
  // painted into the poisoned segment.
  #routeGuarded(event) {
    try {
      this.#route(event);
    } catch (error) {
      this.#health.renderThrows += 1;
      console.error(
        LOG +
          "dispatch failed for " +
          event.type +
          " (render-throw total " +
          this.#health.renderThrows +
          ")",
        error,
      );
    }
  }

  // Content order: while a seedless refetch is in flight everything waits in the queue, so
  // nothing paints into a transcript the render is about to wipe; the core's own decisions that
  // read view state (clear_ui, the truncated-resync choice) wait with it.
  #route(event) {
    if (this.#queue) {
      this.#queue.push(event);
      return;
    }
    if (event.type === "replay_truncated") {
      this.#decideTruncatedResync();
      return;
    }
    if (event.type === "clear_ui") {
      this.#onClearUi();
      return;
    }
    try {
      this.#view.dispatch(event);
    } finally {
      if (event.type === "state_change" && (event.state === "idle" || event.state === "error")) {
        this.#idleEdge();
      }
    }
  }

  // --- truncation and overflow -----------------------------------------------------------------

  // A truncated replay is a dead connection: tear down, load /history, adopt its cursor and
  // reconnect (ruled 2026-07-20, #882; never an in-place refetch). /history trims a running turn
  // whenever it returns a cursor, expecting the caller to replay from it, so an in-place render
  // wiped the executing turn with no redelivery. Mid-stream the resync is deferred to the idle
  // edge, not dropped: a rebuild would detach the live bubble (reasoning-only included).
  #decideTruncatedResync() {
    if (this.#view.liveState().streaming) {
      this.#resyncPending = true;
      return;
    }
    this.#countTruncationGap();
    // The limiter runs first: a trip just took the stream down for a cooldown, and a resync
    // started now would reconnect and defeat it. The wake-up connect presents the gap cursor, so
    // the envelope returns and the resync is deferred, not lost.
    if (this.#recordChurnAndMaybeTrip()) return;
    this.#scheduleTruncatedResync();
  }

  // The one counter-and-log step for replay-window misses. Churn is deliberately not counted
  // here: the idle-edge resync counts the gap without feeding the overflow ladder.
  #countTruncationGap() {
    this.#health.truncatedGaps += 1;
    console.warn(
      LOG +
        "replay window could not cover the reconnect gap (total " +
        this.#health.truncatedGaps +
        ")",
    );
  }

  // Herd spread: a node restart makes every tab with a stale cursor draw the envelope inside the
  // browser's retry window, and the per-tab limiter cannot see a cross-tab herd, so the resync
  // waits a random 0..TRUNCATED_RESYNC_JITTER_MS first. The stream keeps painting meanwhile; the
  // gap predates the resync either way. One pending resync at a time.
  #scheduleTruncatedResync() {
    if (this.#timers.armed("resync")) return;
    this.#timers.arm("resync", Math.random() * TRUNCATED_RESYNC_JITTER_MS, () => this.#reload());
  }

  #noteStreamOverflow() {
    this.#health.overflows += 1;
    console.warn(
      LOG +
        "server closed the stream after a send-queue overflow (total " +
        this.#health.overflows +
        "); reconnect will replay the gap",
    );
    this.#recordChurnAndMaybeTrip();
  }

  // The one rolling-window churn step for the overflow ladder: overflow closes and truncated
  // resyncs both land here. Returns true when this entry entered the degraded cooldown.
  #recordChurnAndMaybeTrip() {
    const now = Date.now();
    this.#overflowTimes.push(now);
    if (
      !overflowWindowTripped(this.#overflowTimes, now, OVERFLOW_TRIP_COUNT, OVERFLOW_TRIP_WINDOW_MS)
    ) {
      return false;
    }
    this.#enterDegraded(now);
    return true;
  }

  // A consumer that keeps overflowing cannot keep up right now, and each reconnect round only
  // stalls behind the retry before saturating again: stop streaming, say so, and come back after
  // a doubling cooldown, whose connect replays the gap or falls to the truncated resync.
  #enterDegraded(now) {
    // The ladder step is keyed off the last trip's timestamp, never the overflow window, which is
    // cleared below; keying off it would restart the ladder at every storm's first overflow.
    const step = degradedCooldownStep(
      this.#degradedCooldownMs,
      this.#lastDegradedAt,
      now,
      DEGRADED_COOLDOWN_BASE_MS,
      DEGRADED_COOLDOWN_MAX_MS,
      DEGRADED_COOLDOWN_RESET_MS,
    );
    this.#lastDegradedAt = now;
    this.#degradedCooldownMs = step.nextCooldownMs;
    this.#overflowTimes.length = 0;
    // Teardown before arming, so it never cancels the timer it is about to set.
    this.#closeTransport();
    this.#timers.arm("degraded", step.cooldown, () => this.#connect());
    this.#setPhase("degraded");
  }

  // --- idle edge -------------------------------------------------------------------------------

  // After a state_change to idle or error. A deferred truncated resync runs now, neither
  // jittered nor counted into churn (ruled 2026-07-21, #882): it fires once per envelope-then-
  // settle, so turn timing rate-limits and staggers it. Otherwise a set latch gets the backstop.
  // The two never double up: the resync's render clears the latch too.
  #idleEdge() {
    if (this.#resyncPending) {
      this.#resyncPending = false;
      this.#countTruncationGap();
      this.#reload();
    } else if (this.#historyStale && !this.#queue) {
      this.#deferStaleBackstop();
    }
  }

  // The staleness backstop: a clear_ui refetch and its retry both failed, the latch is still set,
  // and a turn just settled. It is transport-free by design (ruled; never a reload): a reload's
  // fresh connect draws a synthetic idle back into this branch, which with /history down makes a
  // zero-backoff disconnect/refetch/reconnect storm. A seedless refetch emits no stream events,
  // so that loop cannot form, and no client-side limiter is added (#884 coalesces server-side).
  // Accepted liveness lag: a pane left idle stays latched until the next settle.
  //
  // Deferred to a microtask so it runs at the tail of the current backlog: a replay's leading
  // synthetic idle must not start a refetch that diverts the rest of the slice into a new queue
  // (those rows would paint twice). One flag collapses several idle edges into one attempt, and
  // every owner is re-checked at the tail.
  #deferStaleBackstop() {
    if (this.#backstopQueued) return;
    this.#backstopQueued = true;
    const gen = this.#generation;
    queueMicrotask(() => {
      this.#backstopQueued = false;
      if (
        gen !== this.#generation ||
        !this.#historyStale ||
        this.#queue ||
        this.#resyncPending ||
        this.#gapCursor != null ||
        this.#timers.armed("resync")
      ) {
        return;
      }
      const { busy, streaming } = this.#view.liveState();
      if (busy || streaming) return;
      this.#refetchLoudly();
    });
  }

  // --- history ---------------------------------------------------------------------------------

  // The seeded reload (first paint, truncated resync, handoff repair): render from /history with
  // the stream down, adopt its cursor, then reconnect.
  async #reload({ manual = false, first = false } = {}) {
    if (this.#terminal()) return;
    // Admission before any work: false means an attempt is already in flight, or the parked
    // prompt owns recovery.
    const repairing = this.#repair.isRepairing(this.#wsId);
    if (repairing && !this.#repair.admitAttempt(manual)) return;
    const gen = ++this.#generation;
    this.#abortFetches();
    // The stream goes down first: no live event can paint into the pane mid-fetch, and a seeded
    // render needs no transport gate because it adopts the cursor it renders from.
    this.#closeTransport();
    // Dropping the cursor is what lets the resync converge after a node restart: the reborn ring
    // re-issued ids, so the cursor from before the restart would keep drawing the envelope
    // (envelope, resync, envelope...). Reconnect from /history's cursor, or fresh.
    this.#cursor = null;
    this.#handoffToken = null;
    this.#resyncPending = false;
    this.#setPhase(repairing ? "repairing" : "loading");
    if (gen !== this.#generation) return;
    // After the phase change, so a reset-settled listener for a dropped clear_ui finds a loading
    // instance (a retryNow() there redials nothing).
    this.#dropQueue();
    if (gen !== this.#generation) return;
    // Reply refs survive a reload only when the reconnect can resume into them: a truncation gap
    // is on record, so the reconnect replays into the bubble that was streaming during the
    // jitter. Every other reload resets them, or a stale ref would concatenate the next turn into
    // the earlier bubble. KNOWN RESIDUAL (ruled, do not re-derive): keeping them is one route
    // into the in_progress_snapshot stale-ref case, where a snapshot of a newer turn reuses the
    // stale bubble until the next resync; it is deferred to a snapshot sweep of the event
    // handlers.
    const hadGap = this.#gapCursor != null;
    const attemptId = repairing
      ? this.#repair.startAttempt(manual, () => this.#abortFetches())
      : null;
    let outcome;
    try {
      if (!hadGap) this.#view.resetLiveState();
      outcome = await this.#loadSeeded(gen, first, hadGap);
    } catch (error) {
      // A render throw ends like a failed load (the connect still follows, the repair fails
      // closed), but stays loud.
      console.error(LOG + "history load/render failed", error);
      outcome = "threw";
    } finally {
      // Before the generation check, so a superseded attempt still frees the repair's slot.
      if (repairing) this.#repair.endAttempt(attemptId);
    }
    if (gen !== this.#generation) return;
    this.#emit("reloaded", { first, outcome });
    if (gen !== this.#generation) return;
    if (repairing) {
      // The verdict is the shared controller's: a token or a completed render ends the repair.
      this.#repair.settle({
        outcome,
        hasToken: this.#handoffToken != null,
        manualAttempt: manual,
      });
      return;
    }
    // First paint and truncation recovery reconnect whatever happened: a failed truncated
    // resync re-presents the recorded gap, so the envelope and the resync come back.
    this.#connect();
  }

  async #loadSeeded(gen, first, hadGap) {
    const data = await this.#fetchHistory();
    if (gen !== this.#generation) return "superseded";
    if (!data) return "failed";
    this.#render(data, true);
    // Replay from the adopted cursor covers the transcript, not what the stream missed while it
    // was down. A gap already on record announced itself when it arrived.
    if (!first && !hadGap && data.cursor != null) this.#emit("missed");
    return "rendered";
  }

  // The seedless refetch (clear_ui, the stale retry, the idle backstop) on a live stream: the
  // cursor stays where the stream is, unless the response carries one (#promote). The outcome is
  // rendered (seedless or promoted), declined (a good payload the gate refused), failed (no
  // payload) or superseded (a newer generation); a render throw rejects.
  async #refetch() {
    const gen = this.#generation;
    // Captured at dispatch, not beside the await: work that touches the transport inserted
    // before the fetch can then only decline more, the safe direction.
    const epoch = this.#epoch;
    const queue = [];
    this.#queue = queue;
    try {
      const data = await this.#fetchHistory();
      // The reload that superseded this one dropped the queue; its snapshot covers those events.
      if (gen !== this.#generation) return "superseded";
      // A failed or declined fetch changes nothing: the transcript, the reply refs, the gap
      // record and the latch all stay. Stale but real beats a blank pane on a live stream.
      if (!data) return "failed";
      const { streaming, sending } = this.#view.liveState();
      const allowed = seedlessRenderAllowed({
        open: this.#sourceOpen(),
        sameConnection: this.#epoch === epoch,
        streaming,
        sending,
      });
      if (!allowed) return "declined";
      if (data.cursor != null) {
        this.#promote(data);
        return "rendered";
      }
      this.#render(data, false);
      return "rendered";
    } finally {
      this.#flush(queue, gen);
    }
  }

  // A seedless refetch whose /history carries a cursor is promoted to the seeded path, at once and
  // with no decline. /history returns a cursor only when it trimmed a running turn it expects the
  // caller to replay from that cursor. A seedless render cannot do that; a seeded one does exactly
  // that, and it is the path first paint already takes when a turn is running. The reconnect from
  // the adopted cursor replays the trimmed turn through the live handlers, so it does not matter
  // whether the turn's events were painted before the refetch, are sitting in the queue or came
  // from a snapshot: the history holds everything up to the cursor and the replay delivers
  // everything after it. Dropping the queue is lossless for the same reason. If the ring can no
  // longer replay from the cursor, the server answers with the truncation envelope and the
  // ordinary resync takes over. One rule for clear_ui, the stale retry and the backstop, with no
  // per-pane or per-connection state.
  //
  // Like a reload it starts a new generation, so no older continuation acts afterwards, and takes
  // the transport down before the render. Unlike one it emits neither `reloaded` nor `missed`: it
  // is a render, and the stream was live until now, so nothing was missed. The connect follows
  // even when the render throws, as after a seeded load, and the queue drops last, once the phase
  // has moved on, so a reset-settled listener for a dropped clear_ui finds a connecting instance.
  #promote(data) {
    this.#generation += 1;
    this.#closeTransport();
    this.#handoffToken = null;
    try {
      this.#render(data, true);
    } finally {
      this.#connect();
      this.#dropQueue();
    }
  }

  // The stale retry and the backstop are fire-and-forget: no composer state rides them to
  // un-strand, so a render throw is only logged, loudly.
  #refetchLoudly() {
    this.#refetch().catch((error) => console.error(LOG + "history load/render failed", error));
  }

  // Replay the backlog in arrival order through the router. A queued clear_ui arms a new queue
  // mid-flush and the remainder queues behind it; a reload started mid-flush supersedes the rest,
  // which its snapshot covers.
  #flush(queue, gen) {
    if (this.#queue !== queue) return;
    this.#queue = null;
    for (let index = 0; index < queue.length; index += 1) {
      if (gen !== this.#generation) {
        this.#settleDropped(queue.slice(index));
        return;
      }
      this.#routeGuarded(queue[index]);
    }
  }

  // The render step both paths share.
  #render(data, seeded) {
    // Seeded only: a seedless render must not rewind the live cursor (a later reconnect would
    // replay rows twice). The cursor is seeded before the render, so a throw still reconnects at
    // it, without a token.
    if (seeded && data.cursor != null) this.#cursor = data.cursor;
    // A render supersedes all repair intent in one place: the gap record, the deferred resync, the
    // pending jittered resync. A seedless render may cancel a resync because it never renders a
    // /history that trimmed a running turn: a cursor promotes the refetch to a seeded render, which
    // reconnects from it. Then the latch, its only clear site, and the retry that exists to produce
    // this render. These clears run before the pane's render, as the header's latch rule says: a
    // render that throws after its wipe leaves a fresh transcript that at worst under-counts, so it
    // must not re-close the pane. Every render is post-rewind truth: the queue keeps a pre-clear_ui
    // fetch from landing after the latch was set, and #884's flight key carries the truncation
    // generation server-side.
    this.#gapCursor = null;
    this.#resyncPending = false;
    this.#timers.cancel("resync");
    this.#historyStale = false;
    this.#timers.cancel("staleRetry");
    this.#view.resetLiveState();
    this.#view.renderHistory(Array.isArray(data.messages) ? data.messages : []);
    // Armed only after the whole render: a throw must not open a stream claiming an incomplete
    // transcript matches this revision.
    if (seeded) {
      this.#handoffToken =
        typeof data.handoff_token === "string" && data.handoff_token ? data.handoff_token : null;
    }
  }

  // clear_ui: the server restructured the conversation (rewind, retry, resume, open, fork). No
  // pre-wipe (#890): the render runs only on a completed fetch, so a failed /history keeps the
  // transcript. The latch is set first (#894), before the fetch.
  async #onClearUi() {
    this.#historyStale = true;
    const gen = this.#generation;
    const settled = { outcome: "threw" };
    try {
      settled.outcome = await this.#refetch();
      this.#armStaleRetry(gen);
    } catch (error) {
      settled.error = error;
      console.error(LOG + "history load/render failed", error);
    } finally {
      // Exactly once per clear_ui, on every path and also after destroy: the rewind already
      // committed server-side, so a pending edit must still send unless the render threw.
      this.#emit("reset-settled", settled);
    }
  }

  // The one bounded retry per clear_ui, armed after the refetch settled (never from the failure
  // path, so a retry's own failure cannot arm another) and only while this generation is current.
  // At most one is pending. The spread is additive over the floor (jitter may grow, the floor may
  // not shrink: the e2e non-occurrence windows size on it). It fires only turn-free, never into a
  // pending refetch's queue, and only on an open stream: the transport may be down when it fires
  // (hidden, cooldown, CLOSED), and a frozen cursor would make the render paint rows twice.
  #armStaleRetry(gen) {
    if (!this.#historyStale || gen !== this.#generation) return;
    const delay = STALE_RETRY_BASE_MS + Math.random() * STALE_RETRY_JITTER_MS;
    this.#timers.arm("staleRetry", delay, () => {
      if (gen !== this.#generation || !this.#historyStale || this.#queue || !this.#sourceOpen()) {
        return;
      }
      const { busy, streaming } = this.#view.liveState();
      if (busy || streaming) return;
      this.#refetchLoudly();
    });
  }

  #sourceOpen() {
    return !!this.#source && this.#source.readyState === EventSource.OPEN;
  }

  // GET .../history, raced against a deadline; every load is bounded, so a slow one fails over to
  // the ordinary failure path (#905). The bound starts at 15 s, doubles after each expiry in a row
  // up to HISTORY_DEADLINE_MAX_MS, and resets on any payload: the server finishes a
  // reconstruction even when the client aborts, so a retry must outlast the server's latency or
  // it only multiplies work. Only the deadline's own expiry grows it; a supersession or teardown
  // abort does not. The race is load-bearing: the injected fetch may sleep on a Retry-After that
  // ignores the abort signal, and the deadline still ends the attempt. Resolves the payload, or
  // null for any failure (network, status, body, deadline, abort); the render is the caller's,
  // outside this catch, so a render bug stays loud. Known limits, accepted: a /history slower
  // than the cap never renders, and the bound re-requests during a Retry-After longer than it.
  async #fetchHistory() {
    const controller = new AbortController();
    let expired = false;
    const deadline = createHistoryHandoffDeadline(() => {
      expired = true;
      controller.abort();
    }, this.#historyDeadlineMs);
    const entry = { controller, deadline };
    this.#inflight.add(entry);
    try {
      const data = await Promise.race([this.#requestHistory(controller.signal), deadline.promise]);
      if (data) {
        this.#historyDeadlineMs = HISTORY_HANDOFF_FETCH_TIMEOUT_MS;
      } else if (expired) {
        this.#historyDeadlineMs = Math.min(this.#historyDeadlineMs * 2, HISTORY_DEADLINE_MAX_MS);
      }
      return data;
    } finally {
      this.#inflight.delete(entry);
      deadline.dispose();
    }
  }

  async #requestHistory(signal) {
    const request = this.#fetch;
    try {
      const response = await request(workstreamPath(this.#base, this.#wsId) + "/history", {
        signal,
      });
      if (!response || !response.ok) return null;
      const data = await response.json();
      return data !== null && typeof data === "object" ? data : null;
    } catch {
      return null;
    }
  }

  // Supersession and the terminal teardown end every request in flight: abort it, mark the
  // attempt dead and release its race now.
  #abortFetches() {
    for (const { controller, deadline } of this.#inflight) {
      controller.abort();
      deadline.dispose({ expire: true, resolve: true });
    }
    this.#inflight.clear();
  }
}
