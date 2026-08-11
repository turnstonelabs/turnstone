// Small state primitives shared by the two browser TOOL reducers.
//
// Provider call ids are correlation hints, not globally unique row ids. A
// provider may reuse one in a later turn, and malformed same-batch duplicates
// still have to converge after the server requests a strong history repair.

export function enqueueToolOccurrence(queues, callId, value) {
  const key = String(callId || "");
  const pending = queues.get(key) || [];
  pending.push(value);
  queues.set(key, pending);
  return pending.length;
}

export function shiftToolOccurrence(queues, callId) {
  const key = String(callId || "");
  const pending = queues.get(key);
  if (!pending || !pending.length) return undefined;
  const value = pending.shift();
  if (!pending.length) queues.delete(key);
  return value;
}

export function indexHistoryToolOutcomes(messages) {
  const historyMessages = Array.isArray(messages) ? messages : [];
  const batches = new Map();
  historyMessages.forEach((message, assistantIndex) => {
    if (
      (message.role || "") !== "assistant" ||
      !Array.isArray(message.tool_calls)
    )
      return;
    const outcomes = new Array(message.tool_calls.length);
    const unmatched = new Map();
    message.tool_calls.forEach((toolCall, callIndex) => {
      enqueueToolOccurrence(
        unmatched,
        String((toolCall && toolCall.id) || ""),
        callIndex,
      );
    });
    for (let i = assistantIndex + 1; i < historyMessages.length; i++) {
      const result = historyMessages[i];
      const role = result.role || "tool";
      // The batch's result window ends at the next conversational turn
      // (assistant or user).  Other interleaved rows — a mid-turn system
      // message, a second writer's append (cross-node re-home overlap,
      // legacy NULL-key import order) — are skipped, not terminators:
      // treating them as terminators left every later result unmatched
      // and painted a fully-resolved batch as a permanent orphan shell.
      if (role === "assistant" || role === "user") break;
      if (role !== "tool") continue;
      const callIndex = shiftToolOccurrence(
        unmatched,
        String(result.tool_call_id || ""),
      );
      if (callIndex === undefined) continue;
      outcomes[callIndex] = result.denied
        ? "denied"
        : result.is_error
          ? "error"
          : "ok";
    }
    batches.set(message, outcomes);
  });
  return batches;
}

export function indexLatestToolRow(rows, resultOwners, callId, row) {
  const key = String(callId || "");
  if (!key || !row) return null;
  const prior = rows.get(key) || null;
  // Release the tracked result owner only when a prior row is superseded. A
  // first sighting has nothing to supersede, and the entry it would drop is
  // the orphan bubble this row's own result has yet to absorb.
  if (prior && prior !== row) resultOwners.delete(key);
  rows.set(key, row);
  return prior;
}

export function acceptedToolEventAlreadyRendered(renderedIds, event) {
  return !!(
    event &&
    event.accepted === true &&
    event._event_id != null &&
    renderedIds.has(String(event._event_id))
  );
}

export function recordAcceptedToolEvent(renderedIds, event) {
  if (event && event.accepted === true && event._event_id != null) {
    renderedIds.add(String(event._event_id));
  }
}

export function shouldRefreshTasksForToolResult(event, hadResult) {
  return !!(
    event &&
    event.name === "tasks" &&
    !event.is_error &&
    (event.accepted !== true || !hadResult)
  );
}
