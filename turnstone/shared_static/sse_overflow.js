// Client half of the SSE overflow recovery — the storm-guard math and the
// degraded-catchup cooldown ladder, shared by every pane that consumes a
// per-workstream SSE stream (the interactive pane and the coordinator pane).
//
// The server closes a stream whose listener queue overflowed — after an
// id-less `stream_overflow` frame — and the native EventSource reconnect
// replays the gap from the server's ring buffer.  For a consumer that STAYS
// too slow that cycle would churn forever (reconnect → replay burst →
// re-saturate → re-close, stalling ~3-5 s per retry round), so after
// OVERFLOW_TRIP_COUNT overflow closes inside a rolling OVERFLOW_TRIP_WINDOW_MS
// the pane drops to a degraded catch-up: stop live streaming, say so plainly,
// and re-try after a doubling cooldown — the eventual reconnect replays the
// gap, or falls to the replay_truncated → /history floor once the gap
// outgrows the ring.
//
// The two surfaces keep their OWN transport/DOM glue (interactive.js is a
// class whose methods hang off `this` and route through disconnectSSE;
// coordinator.js is a closure with an inline stream close) but share these two
// pure helpers + the constants, so the trip threshold and the escalation
// ladder — the review-hardened, drift-prone part — have a single source of
// truth instead of two copies that silently diverge.
export const OVERFLOW_TRIP_COUNT = 3;
export const OVERFLOW_TRIP_WINDOW_MS = 60000;
export const DEGRADED_COOLDOWN_BASE_MS = 15000;
export const DEGRADED_COOLDOWN_MAX_MS = 120000;
// Reset the cooldown ladder back to base only after this long WITHOUT a
// degraded trip.  Deliberately larger than DEGRADED_COOLDOWN_MAX_MS: a
// persistently-slow consumer trips again roughly one cooldown apart, so keying
// the reset off the (shorter) trip window would let the gap between
// top-of-ladder trips exceed the window and oscillate the cooldown back to
// base — the escalation must survive its own backoff.
export const DEGRADED_COOLDOWN_RESET_MS = 300000;

// Rolling-window trip check.  Prunes `times` in place (entries older than
// windowMs against nowMs) and reports whether count-or-more remain.  A
// standalone pure function so the trip logic can be lifted verbatim into a
// bare `node` runtime probe (see tests/test_sse_overflow_js.py).
export function overflowWindowTripped(times, nowMs, count, windowMs) {
  while (times.length && nowMs - times[0] > windowMs) times.shift();
  return times.length >= count;
}

// Cooldown-ladder step for degraded catch-up.  Escalates (double, capped at
// maxMs) when a trip recurs within resetMs of the last one; resets to baseMs
// only after a genuine quiet gap.  Pure — keyed off the last-trip TIMESTAMP,
// never the overflow-window array (which the caller clears each trip, so
// keying off it would reset the ladder on every storm's first overflow and
// the doubling would never escalate).  Split out so the escalation is
// runtime-testable without a DOM-bound pane.
export function degradedCooldownStep(
  prevCooldownMs,
  lastTripAtMs,
  nowMs,
  baseMs,
  maxMs,
  resetMs,
) {
  const cooldown = nowMs - lastTripAtMs > resetMs ? baseMs : prevCooldownMs;
  return { cooldown, nextCooldownMs: Math.min(cooldown * 2, maxMs) };
}
