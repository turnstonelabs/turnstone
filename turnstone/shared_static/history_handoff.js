/* Shared fail-closed history-handoff repair policy.
 *
 * DOM, fetch, and EventSource lifecycles stay pane-owned; the safety-critical
 * attempt budget and deadline live here so interactive and coordinator cannot
 * drift. A manual click is always allowed after automatic recovery parks.
 */

export const HISTORY_HANDOFF_MAX_ATTEMPTS = 4;
export const HISTORY_HANDOFF_FETCH_TIMEOUT_MS = 15000;

export function historyHandoffAttemptAllowed(
  attempts,
  manualOnly,
  manualAttempt = false,
) {
  return (
    !!manualAttempt || (!manualOnly && attempts < HISTORY_HANDOFF_MAX_ATTEMPTS)
  );
}

export function createHistoryHandoffDeadline(
  onExpire,
  timeoutMs = HISTORY_HANDOFF_FETCH_TIMEOUT_MS,
) {
  const state = { expired: false, timer: null, settle: null };
  const promise = new Promise((resolve) => {
    state.settle = resolve;
    state.timer = setTimeout(() => {
      state.expired = true;
      if (typeof onExpire === "function") onExpire();
      resolve(null);
    }, timeoutMs);
  });
  return {
    state,
    promise,
    /* The one retirement path, owned here so the panes cannot drift on
     * slot order: stop the timer (no late expiry), drop the settle slot,
     * and — when cancelling an attempt rather than recording its natural
     * settlement — mark it dead (`expire`, the render-inert flag) and
     * release the race (`resolve`, so no awaiter or timer closure
     * outlives the attempt).  Idempotent; `state.expired` stays readable
     * but panes never write state slots directly. */
    dispose({ expire = false, resolve = false } = {}) {
      if (expire) state.expired = true;
      if (state.timer != null) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      const settle = state.settle;
      state.settle = null;
      if (resolve && settle) settle(null);
    },
  };
}

/* One repair-backoff step: the jittered delay to sleep now plus the doubled
 * base for the next step, both capped. Both panes must pace identically. */
export function nextHistoryHandoffDelay(currentMs, { jitterMs, maxMs }) {
  return {
    delayMs: Math.min(currentMs + Math.random() * jitterMs, maxMs),
    nextBaseMs: Math.min(currentMs * 2, maxMs),
  };
}

/* The parked-repair prompt. Copy, structure, and class hooks are part of the
 * shared contract (tests and both panes key off them); placement, scroll, and
 * retry gating stay pane-owned. */
export function buildHistoryHandoffPrompt({ onRetry, onReload }) {
  const prompt = document.createElement("div");
  prompt.className = "msg error history-handoff-repair";
  prompt.setAttribute("role", "alert");
  const copy = document.createElement("div");
  copy.textContent =
    "Live updates are paused because conversation history could not be verified.";
  prompt.appendChild(copy);
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "msg-action-btn history-handoff-retry";
  retry.textContent = "Retry now";
  retry.addEventListener("click", onRetry);
  const reload = document.createElement("button");
  reload.type = "button";
  reload.className = "msg-action-btn history-handoff-reload";
  reload.textContent = "Reload page";
  reload.addEventListener(
    "click",
    onReload || (() => window.location.reload()),
  );
  actions.appendChild(retry);
  actions.appendChild(reload);
  prompt.appendChild(actions);
  return prompt;
}

/* The repair state machine itself, owned once.
 *
 * Both panes held the same seven slots — pending latch, backoff timer, backoff
 * base, in-flight attempt, attempt count, manual-only latch, parked prompt —
 * and ran the same clear / park / begin / schedule / settle logic over them.
 * Everything that differs arrives through deps: where the prompt goes, how
 * history loads, how the transport reopens, what the status line reads, and
 * whether the pane is still alive. The pane keeps its own handoff proof token;
 * this owns only the recovery policy.
 *
 * `scope` is the workstream the repair intent belongs to. A pane reassignment
 * supersedes the intent rather than repairing the wrong transcript.
 *
 * deps:
 *   load(scope, manualAttempt)  run one /history attempt for that scope
 *   connect(scope)              reopen the transport once the latch clears
 *   placePrompt(prompt)         attach the parked prompt and scroll to it
 *   showPaused()                pane status display for a parked repair
 *   setStale(stale)             the transcript mutation latch
 *   deferToShowEdge()           record that a hidden tab skipped an attempt
 *   isAlive()                   false once the stream lifecycle is released
 *   baseDelayMs / jitterMs / maxMs   backoff pacing (pane-supplied constants)
 */
export function createHistoryHandoffRepair(deps) {
  let pending = false;
  let scope = null;
  let timer = null;
  let delayMs = deps.baseDelayMs;
  let attempts = 0;
  let manualOnly = false;
  let promptEl = null;
  /* Identity of the attempt currently in flight (null when none), so a settle
   * that lost its race against a clear/restart cannot retire a live one. */
  let inFlightId = null;
  let nextAttemptId = 0;
  let attemptTeardown = null;

  function clear() {
    if (timer != null) {
      clearTimeout(timer);
      timer = null;
    }
    if (attemptTeardown) {
      const teardown = attemptTeardown;
      attemptTeardown = null;
      teardown();
    }
    if (promptEl) {
      promptEl.remove();
      promptEl = null;
    }
    pending = false;
    scope = null;
    delayMs = deps.baseDelayMs;
    attempts = 0;
    manualOnly = false;
    inFlightId = null;
  }

  function showManual() {
    manualOnly = true;
    deps.showPaused();
    let prompt = promptEl;
    if (!prompt || !prompt.isConnected) {
      prompt = buildHistoryHandoffPrompt({
        onRetry: () => {
          if (pending && inFlightId == null && scope) deps.load(scope, true);
        },
      });
      deps.placePrompt(prompt);
      promptEl = prompt;
    }
    const retry = prompt.querySelector(".history-handoff-retry");
    if (retry) retry.disabled = inFlightId != null;
  }

  function schedule() {
    if (!pending || timer != null || inFlightId != null || !scope) return;
    if (!deps.isAlive()) return;
    if (!historyHandoffAttemptAllowed(attempts, manualOnly)) {
      showManual();
      return;
    }
    const target = scope;
    const { delayMs: delay, nextBaseMs } = nextHistoryHandoffDelay(delayMs, {
      jitterMs: deps.jitterMs,
      maxMs: deps.maxMs,
    });
    delayMs = nextBaseMs;
    timer = setTimeout(() => {
      timer = null;
      if (!pending || scope !== target || !deps.isAlive()) return;
      if (document.hidden) {
        // There is intentionally no EventSource while this repair is pending,
        // so the ordinary hide handler has nothing to close. Mark the deferral
        // explicitly; the show edge re-enters connect, whose repair chokepoint
        // schedules the next bounded attempt.
        deps.deferToShowEdge();
        return;
      }
      deps.load(target, false);
    }, delay);
  }

  return {
    clear: clear,
    showManual: showManual,
    schedule: schedule,

    isRepairing(forScope) {
      return pending && scope === forScope;
    },

    /* A real pane reassignment supersedes a repair intent: the new workstream
     * performs its own history bootstrap. */
    supersede(nextScope) {
      if (pending && scope !== nextScope) clear();
    },

    begin(nextScope) {
      if (pending && scope === nextScope) return;
      clear();
      pending = true;
      scope = nextScope;
      // Reuse the transcript mutation latch rather than adding another
      // affordance gate. It clears only on a completed render.
      deps.setStale(pending);
      deps.load(nextScope, false);
    },

    /* Admission for one attempt, before the caller commits to any work. False
     * means "do not load": either an attempt is already in flight, or the
     * budget is spent and the parked prompt now owns recovery. */
    admitAttempt(manualAttempt) {
      if (inFlightId != null) return false;
      if (!historyHandoffAttemptAllowed(attempts, manualOnly, manualAttempt)) {
        showManual();
        return false;
      }
      if (timer != null) {
        clearTimeout(timer);
        timer = null;
      }
      return true;
    },

    /* Charge the attempt to the budget and park the retry affordance for its
     * duration. `teardown` releases whatever fetch-bounding resources the pane
     * armed for it, and runs if a clear cancels the attempt mid-flight. */
    startAttempt(manualAttempt, teardown) {
      inFlightId = ++nextAttemptId;
      attempts += 1;
      if (manualAttempt) manualOnly = true;
      attemptTeardown = teardown || null;
      const retry = promptEl
        ? promptEl.querySelector(".history-handoff-retry")
        : null;
      if (retry) retry.disabled = true;
      return inFlightId;
    },

    endAttempt(attemptId) {
      if (inFlightId !== attemptId) return;
      inFlightId = null;
      attemptTeardown = null;
    },

    /* The one repair verdict, for both panes. */
    settle({ outcome, hasToken, manualAttempt }) {
      if (hasToken || outcome === "rendered") {
        // A token means the full render completed and armed the proof from
        // that same response; the reopened transport carries it. A COMPLETED
        // render without a token is the server's deliberate tokenless read
        // (cold storage-only, or a route with no storage handle): downgrade
        // to the tokenless bootstrap, whose cursorless connect gets the
        // server's clear_ui convergence instead of a cursor handoff — same
        // contract as a pre-handoff server. Either way the repair is over.
        const target = scope;
        clear();
        deps.connect(target);
        return;
      }
      // Fetch/JSON failure, a render throw, a superseded render, and deadline
      // expiry all fail closed. Keep the stale transcript visible, keep its
      // mutation latch closed, and retry at a bounded rate without opening an
      // unverified stream.
      deps.setStale(pending);
      if (manualAttempt || attempts >= HISTORY_HANDOFF_MAX_ATTEMPTS) {
        showManual();
      } else {
        schedule();
      }
    },
  };
}
