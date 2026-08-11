"""Behavioral contracts for user-turn projection and handoff recovery JS."""

# Percent interpolation keeps the extracted JavaScript's many literal braces
# readable; converting these harnesses to ``str.format`` would require escaping
# every object/function body and make source-to-test review substantially harder.
# ruff: noqa: UP031

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._js_harness_helpers import strip_js_comments as _strip_comments

_ROOT = Path(__file__).resolve().parent.parent
_INTERACTIVE = _ROOT / "turnstone/shared_static/interactive.js"
_COORDINATOR = _ROOT / "turnstone/console/static/coordinator/coordinator.js"
_SHARED_HANDOFF = _ROOT / "turnstone/shared_static/history_handoff.js"
_QUEUE = _ROOT / "turnstone/shared_static/composer_queue.js"
_HANDOFF = _ROOT / "turnstone/shared_static/history_handoff.js"
_TOOL_PROJECTION = _ROOT / "turnstone/shared_static/tool_projection.js"
_PY_SDK = _ROOT / "turnstone/sdk/server.py"
_PY_CHANNEL = _ROOT / "turnstone/channels/_sse.py"
_TS_SDK = _ROOT / "sdk/typescript/src/server.ts"


def _run_module(tmp_path: Path, source: str) -> None:
    script = tmp_path / "contract.mjs"
    script.write_text(source, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", str(script)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("node binary not available on PATH")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def _extract_braced(source: str, signature: str) -> str:
    """Extract one JS function/method body without maintaining a source copy."""

    start = source.index(signature)
    brace = start + len(signature) - 1
    assert source[brace] == "{"
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    i = brace
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in {'"', "'", "`"}:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def _as_function(source: str, signature: str) -> str:
    extracted = _extract_braced(source, signature).strip()
    if extracted.startswith("function "):
        return extracted
    return "function " + extracted


def test_interactive_stale_backstop_waits_for_replay_tail_runtime(
    tmp_path: Path,
) -> None:
    """A replay prelude idle must not split its canonical backlog around history."""

    source = _INTERACTIVE.read_text(encoding="utf-8")
    begin_replay = _as_function(source, "  _beginReplayQuiesce(token) {")
    end_replay = _as_function(source, "  _endReplayQuiesce(token) {")
    defer_backstop = _as_function(source, "  _deferStaleHistoryBackstop() {")
    handle_event = _as_function(source, "  handleEvent(evt) {")

    # These are the race owners that can change after a synthetic replay_ok
    # idle queues the microtask but before the canonical backlog finishes.
    for guard in (
        "queueMicrotask(() => {",
        "this.wsId !== staleWs",
        "staleToken !== this._historyLoadToken",
        "!this._historyStale",
        "this._replayQueue",
        "this.busy",
        "this.currentAssistantEl",
        "this.currentReasoningEl",
        "this._pendingTruncatedResync",
        "this._truncatedFromCursor != null",
        "this._resyncTimer != null",
        "!this.el",
        "!this.el.isConnected",
    ):
        assert guard in defer_backstop

    script = """
function resetCompactionHolder() {}
function streamingRender(body, text) { body.textContent = text; }
function streamingRenderFinalize(body, text) { body.textContent = text; }
"""
    script += "\n" + begin_replay
    script += "\n" + end_replay
    script += "\n" + defer_backstop
    script += "\n" + handle_event
    script += r"""

const sentinel = "DUPE-runtime";
let backstops = 0;
let releaseHeal = null;
const pane = {
  wsId: "ws-1",
  _historyLoadToken: 7,
  _historyStale: true,
  _replayQueue: null,
  _staleBackstopMicrotaskPending: false,
  _pendingTruncatedResync: false,
  _truncatedFromCursor: null,
  _resyncTimer: null,
  busy: false,
  currentAssistantEl: null,
  currentAssistantBodyEl: null,
  currentReasoningEl: null,
  contentBuffer: "",
  _cancelTimeout: null,
  _forceTimeout: null,
  _compaction: {},
  _streamHealth: { renderThrows: 0 },
  _actingUserId: null,
  pendingApproval: false,
  el: { isConnected: true },
  inputEl: { focus() {} },
  _host: { isFocused() { return false; } },
  assistantRows: [],
  userRows: [],
  setBusy(value) { this.busy = value; },
  _attachRetryToLastAssistant() {},
  _acceptUserTurn(evt) { this.userRows.push(evt.content); },
  removeThinkingIndicator() {},
  removeEmptyState() {},
  scrollToBottom() {},
  _newAssistantBubble() {
    const body = { textContent: "" };
    this.currentAssistantBodyEl = body;
    this.currentAssistantEl = { body };
    this.assistantRows.push(body);
  },
  replayHistory() {
    this.assistantRows = [{ textContent: sentinel }];
    this._historyStale = false;
  },
  _refetchHistory(_wsId, token) {
    backstops += 1;
    return new Promise((resolve) => {
      releaseHeal = () => {
        this.replayHistory();
        this._endReplayQuiesce(token);
        resolve();
      };
    });
  },
  _beginReplayQuiesce: _beginReplayQuiesce,
  _endReplayQuiesce: _endReplayQuiesce,
  _deferStaleHistoryBackstop: _deferStaleHistoryBackstop,
  handleEvent: handleEvent,
};

// This is the production replay_ok ordering: its synthetic current-state idle
// precedes the canonical ring slice for the turn that landed during /history.
pane._replayQueue = {
  token: 7,
  events: [
    { type: "state_change", state: "idle" },
    { type: "user_turn", content: "gap" },
    { type: "content", text: sentinel },
    { type: "stream_end" },
    { type: "state_change", state: "idle" },
  ],
};
pane._endReplayQuiesce(7);

if (backstops !== 0)
  throw new Error("synthetic idle refetched before the canonical backlog tail");
if (pane._replayQueue !== null)
  throw new Error("canonical backlog was diverted into a replacement queue");
if (pane.userRows.join("|") !== "gap")
  throw new Error("canonical user turn was not delivered in FIFO order");
if (
  pane.assistantRows.length !== 1 ||
  pane.assistantRows[0].textContent !== sentinel
)
  throw new Error("canonical content did not paint exactly once before heal");

await Promise.resolve();
if (backstops !== 1)
  throw new Error("multiple idle edges did not collapse to one stale backstop");
if (!pane._replayQueue || pane._replayQueue.events.length !== 0)
  throw new Error("stale backstop did not quiesce its delayed history repaint");
if (typeof releaseHeal !== "function")
  throw new Error("delayed history repaint was not captured");

releaseHeal();
await Promise.resolve();
if (pane._replayQueue !== null)
  throw new Error("successful stale heal left replay quiesced");
if (
  pane.assistantRows.length !== 1 ||
  pane.assistantRows[0].textContent !== sentinel
)
  throw new Error("history repaint plus replay backlog duplicated assistant content");
"""
    _run_module(tmp_path, script)


def test_history_handoff_attempt_budget_runtime(tmp_path: Path) -> None:
    """Four automatic attempts park; explicit manual recovery remains usable."""

    _run_module(
        tmp_path,
        f"""
import {{
  createHistoryHandoffDeadline,
  HISTORY_HANDOFF_FETCH_TIMEOUT_MS,
  HISTORY_HANDOFF_MAX_ATTEMPTS,
  historyHandoffAttemptAllowed,
}} from {json.dumps(_HANDOFF.as_uri())};
if (HISTORY_HANDOFF_MAX_ATTEMPTS !== 4) throw new Error("attempt budget");
if (HISTORY_HANDOFF_FETCH_TIMEOUT_MS !== 15000) throw new Error("deadline");
for (let attempts = 0; attempts < 4; attempts++) {{
  if (!historyHandoffAttemptAllowed(attempts, false, false))
    throw new Error("automatic attempt " + attempts + " was blocked");
}}
if (historyHandoffAttemptAllowed(4, false, false))
  throw new Error("fifth automatic attempt was allowed");
if (historyHandoffAttemptAllowed(0, true, false))
  throw new Error("manual-only state failed open");
if (!historyHandoffAttemptAllowed(4, true, true))
  throw new Error("manual retry path was lost");

let expired = 0;
const deadline = createHistoryHandoffDeadline(() => expired++, 5);
const stalled = new Promise(() => {{}});
const result = await Promise.race([
  stalled,
  deadline.promise.then(() => "deadline"),
]);
if (result !== "deadline" || expired !== 1 || !deadline.state.expired)
  throw new Error("logical deadline did not settle an unresolved fetch");

// dispose() owns retirement: a cancel (expire + resolve) marks the attempt
// dead, releases the race immediately, and never fires onExpire; it is
// idempotent, and a natural-settle dispose() just drops the timer + slot.
let cancelledExpires = 0;
const cancelled = createHistoryHandoffDeadline(() => cancelledExpires++, 60000);
cancelled.dispose({{ expire: true, resolve: true }});
await cancelled.promise;
if (
  !cancelled.state.expired ||
  cancelled.state.timer != null ||
  cancelled.state.settle != null ||
  cancelledExpires !== 0
)
  throw new Error("cancel-dispose did not retire the deadline in place");
cancelled.dispose({{ expire: true, resolve: true }});

const settled = createHistoryHandoffDeadline(() => {{}}, 60000);
settled.dispose();
if (
  settled.state.expired ||
  settled.state.timer != null ||
  settled.state.settle != null
)
  throw new Error("natural-settle dispose must retire without expiring");
""",
    )


def test_tool_occurrence_pairing_runtime(tmp_path: Path) -> None:
    """Reused, duplicate, and tail-leading ids retain structural row identity."""

    _run_module(
        tmp_path,
        f"""
import {{
  enqueueToolOccurrence,
  indexHistoryToolOutcomes,
  indexLatestToolRow,
  shiftToolOccurrence,
}} from {json.dumps(_TOOL_PROJECTION.as_uri())};

const first = {{ role: "assistant", tool_calls: [{{ id: "c", name: "A" }}] }};
const second = {{ role: "assistant", tool_calls: [{{ id: "c", name: "B" }}] }};
const messages = [
  first,
  {{ role: "tool", tool_call_id: "c", content: "one" }},
  second,
  {{ role: "tool", tool_call_id: "c", content: "two", is_error: true }},
];
const indexed = indexHistoryToolOutcomes(messages);
if (indexed.get(first)[0] !== "ok" || indexed.get(second)[0] !== "error")
  throw new Error("reused id inherited another turn's outcome");

const duplicate = {{
  role: "assistant",
  tool_calls: [{{ id: "dup", name: "A" }}, {{ id: "dup", name: "B" }}],
}};
const duplicateMessages = [
  duplicate,
  {{ role: "tool", tool_call_id: "dup", content: "first" }},
  {{ role: "tool", tool_call_id: "dup", content: "second", denied: true }},
];
const duplicateOutcomes = indexHistoryToolOutcomes(duplicateMessages).get(duplicate);
if (duplicateOutcomes[0] !== "ok" || duplicateOutcomes[1] !== "denied")
  throw new Error("same-batch duplicate occurrences collapsed");

const bounded = {{ role: "assistant", tool_calls: [{{ id: "c", name: "new" }}] }};
const boundedMessages = [
  {{ role: "tool", tool_call_id: "c", content: "cut-off old", is_error: true }},
  bounded,
  {{ role: "tool", tool_call_id: "c", content: "new ok" }},
];
if (indexHistoryToolOutcomes(boundedMessages).get(bounded)[0] !== "ok")
  throw new Error("leading orphan poisoned bounded-history batch");

// Non-turn rows interleaved inside a batch (a mid-turn system message, a
// second writer's append) are skipped, not window terminators — a
// fully-resolved batch must never index as a permanent orphan.  Only the
// next conversational turn (assistant above; user here) ends the window.
const interleaved = {{
  role: "assistant",
  tool_calls: [{{ id: "i1", name: "A" }}, {{ id: "i2", name: "B" }}],
}};
const interleavedMessages = [
  interleaved,
  {{ role: "tool", tool_call_id: "i1", content: "one" }},
  {{ role: "system", content: "operator note" }},
  {{ role: "tool", tool_call_id: "i2", content: "two", is_error: true }},
];
const interleavedOutcomes =
  indexHistoryToolOutcomes(interleavedMessages).get(interleaved);
if (interleavedOutcomes[0] !== "ok" || interleavedOutcomes[1] !== "error")
  throw new Error("interleaved system row truncated the batch result window");

const userBounded = {{ role: "assistant", tool_calls: [{{ id: "u1", name: "A" }}] }};
const userBoundedMessages = [
  userBounded,
  {{ role: "user", content: "next turn" }},
  {{ role: "tool", tool_call_id: "u1", content: "stray twin" }},
];
if (indexHistoryToolOutcomes(userBoundedMessages).get(userBounded)[0] !== undefined)
  throw new Error("a user turn no longer bounds the batch result window");

const rowsById = new Map();
const resultsById = new Map();
const oldRow = {{ name: "A" }};
const newRow = {{ name: "B" }};
indexLatestToolRow(rowsById, resultsById, "c", oldRow);
resultsById.set("c", {{ output: "one" }});
indexLatestToolRow(rowsById, resultsById, "c", newRow);
if (rowsById.get("c") !== newRow || resultsById.has("c"))
  throw new Error("newest reused-id row did not take ownership");

const occurrences = new Map();
enqueueToolOccurrence(occurrences, "dup", {{ row: "r1", output: "first" }});
enqueueToolOccurrence(occurrences, "dup", {{ row: "r2", output: "second" }});
const paired1 = shiftToolOccurrence(occurrences, "dup");
const paired2 = shiftToolOccurrence(occurrences, "dup");
if (paired1.row !== "r1" || paired1.output !== "first" ||
    paired2.row !== "r2" || paired2.output !== "second" ||
    occurrences.has("dup"))
  throw new Error("occurrence queue did not preserve FIFO identity");
""",
    )


def test_browser_tool_projection_reducers_runtime(tmp_path: Path) -> None:
    """Both real browser reducers replace provisional output on the newest row."""

    source = _COORDINATOR.read_text(encoding="utf-8")
    handle_event = _as_function(source, "  function handleEvent(ev) {")
    append_result = _as_function(
        source,
        "  function appendToolResult(name, callId, output, isError, opts) {",
    )
    append_to_row = _as_function(
        source,
        "  function _appendResultToRow(row, output, isError, opts) {",
    )
    append_batch = _as_function(
        source,
        "  function appendToolBatch(items, opts) {",
    )
    _run_module(
        tmp_path,
        """
import {
  acceptedToolEventAlreadyRendered,
  indexLatestToolRow,
  recordAcceptedToolEvent,
  shouldRefreshTasksForToolResult,
} from %(projection)s;

function classList(...initial) {
  const values = new Set(initial);
  return {
    add(...names) { names.forEach((name) => values.add(name)); },
    remove(...names) { names.forEach((name) => values.delete(name)); },
    contains(name) { return values.has(name); },
  };
}
function makeNode(kind) {
  const node = {
    kind,
    output: "",
    isError: false,
    dataset: {},
    attributes: {},
    classList: classList(),
    parent: null,
    isConnected: false,
    children: [],
    appendChild(child) {
      child.parent = this;
      child.isConnected = true;
      this.children.push(child);
      return child;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    removeAttribute(name) { delete this.attributes[name]; },
    remove() {
      if (this.parent) {
        const at = this.parent.children.indexOf(this);
        if (at >= 0) this.parent.children.splice(at, 1);
      }
      this.isConnected = false;
    },
    querySelector(selector) {
      if (selector === ".conv-row-result")
        return this.children.find((child) => child.classList.contains("conv-row-result")) || null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === ".conv-row-result")
        return this.children.filter((child) => child.classList.contains("conv-row-result"));
      return [];
    },
    closest(selector) {
      return selector === ".conv-batch" ? this.parent : null;
    },
  };
  return node;
}
function makeRow(callId) {
  const batch = makeNode("batch");
  batch.classList = classList("conv-batch", "conv-batch--approved");
  const row = makeNode("row");
  row.dataset.callId = callId;
  batch.appendChild(row);
  return { batch, row };
}
function _tryMcpErrorBlock() { return null; }
function buildConvResult(output, opts) {
  const node = makeNode("output");
  node.output = output;
  node.isError = !!(opts && opts.isError);
  node.classList.add("conv-row-result");
  return node;
}
let previewOpens = 0;
function buildPreviewChip(preview, onOpen) {
  const node = makeNode("preview");
  node.preview = preview;
  node.open = onOpen;
  return node;
}
const window = {
  TS_SHELL: { openPreview() { previewOpens++; } },
  open() { previewOpens++; },
};
function _unsetBatchRunningIfAllResults() {}
function _scheduleScroll() {}
// The mapped-row scenarios below must never fall to the orphan path; the last
// scenario opts in explicitly to exercise it.
let allowOrphanResult = false;
function appendMsg(_role, html, opts) {
  if (!allowOrphanResult) throw new Error("unexpected orphan result path");
  const el = makeNode("msg");
  el.output = html;
  el.dataset.callId = (opts && opts.callId) || "";
  messagesEl.appendChild(el);
  return el;
}
function renderToolOutput(output) {
  if (!allowOrphanResult) throw new Error("unexpected orphan result path");
  return String(output || "");
}
function buildConvBatchShell() {
  const batch = makeNode("batch");
  batch.classList = classList("conv-batch");
  return batch;
}
function _renderBatchRow(item) {
  const row = makeNode("row");
  row.classList = classList("conv-row");
  row.dataset.callId = item.call_id || "";
  row.dataset.funcName = item.func_name || "tool";
  return row;
}
function _pickBatchTier() { return ""; }
function indexLabel() { return ""; }
function batchKicker() { return "Tool"; }
function _pendingKickerText() { return "Pending"; }
function _approvalAriaLabel() { return "Approval required"; }
function _buildBatchActions() { return makeNode("actions"); }
function _buildStatusPill() { return makeNode("status"); }
function _refreshRowStatus() {}
function _appendVerdictLineTo() {}
function _appendJudgePendingLineTo() {}
function _announceAssertive() {}
function _announcePolite() {}
function _toolAnnounceText() { return "tool"; }

const toolRows = new Map();
const latestToolRowElements = new Map();
const toolResultNodes = new Map();
const renderedToolEventIds = new Set();
const liveToolCalls = new Set();
const judgeVerdicts = new Map();
const messagesEl = makeNode("messages");
let activeBatch = null;
let taskRefreshes = 0;
function loadTasksDebounced() { taskRefreshes++; }
const _appendResultToRow = %(append_to_row)s;
const appendToolResult = %(append_result)s;
const appendToolBatch = %(append_batch)s;
const handleEvent = %(handle)s;

// Exercise the real live admission path. An unresolved replay is the same
// occurrence and upgrades in place; a completed row with a provider-reused id
// starts a fresh batch, whose accepted output cannot mutate the older row.
handleEvent({
  type: "tool_pending", items: [{ call_id: "live-reuse", func_name: "A" }],
});
const unresolvedBatch = toolRows.get("live-reuse").batch;
handleEvent({
  type: "tool_info", items: [{ call_id: "live-reuse", func_name: "A" }],
});
if (messagesEl.children.length !== 1 || toolRows.get("live-reuse").batch !== unresolvedBatch)
  throw new Error("coordinator unresolved occurrence did not upgrade in place");
handleEvent({
  type: "tool_result", accepted: true, _event_id: 1,
  call_id: "live-reuse", name: "A", output: "old final",
});
const oldLiveRow = toolRows.get("live-reuse").row;
const oldLiveOutput = oldLiveRow.children.find((node) => node.kind === "output");
handleEvent({
  type: "tool_info", items: [{ call_id: "live-reuse", func_name: "B" }],
});
const newLiveRow = toolRows.get("live-reuse").row;
if (messagesEl.children.length !== 2 || newLiveRow === oldLiveRow)
  throw new Error("coordinator completed reused id did not create a new live batch");
handleEvent({
  type: "tool_result", accepted: true, _event_id: 2,
  call_id: "live-reuse", name: "B", output: "new final",
});
if (!oldLiveOutput || oldLiveOutput.output !== "old final" || !oldLiveOutput.isConnected)
  throw new Error("coordinator reused live id mutated the completed prior row");
const newLiveOutput = newLiveRow.children.find((node) => node.kind === "output");
if (!newLiveOutput || newLiveOutput.output !== "new final")
  throw new Error("coordinator reused live id missed the newest row");

// /history calls the same admission function once per assistant occurrence.
// Resolved rows must always append even when the provider reuses the id.
const historyOne = appendToolBatch(
  [{ call_id: "history-reuse", func_name: "History A" }],
  { resolved: { approved: true } },
);
const historyOneRow = historyOne.children.find((node) => node.kind === "row");
_appendResultToRow(historyOneRow, "history old", false, { accepted: true });
const historyTwo = appendToolBatch(
  [{ call_id: "history-reuse", func_name: "History B" }],
  { resolved: { approved: true } },
);
const historyTwoRow = historyTwo.children.find((node) => node.kind === "row");
_appendResultToRow(historyTwoRow, "history new", true, { accepted: true });
if (historyOne === historyTwo || messagesEl.children.length !== 4)
  throw new Error("coordinator history reused id collapsed assistant occurrences");
if (historyOneRow.children.find((node) => node.kind === "output").output !== "history old" ||
    historyTwoRow.children.find((node) => node.kind === "output").output !== "history new")
  throw new Error("coordinator history reused id cross-attributed outputs");

const first = makeRow("c");
indexLatestToolRow(latestToolRowElements, toolResultNodes, "c", first.row);
toolRows.set("c", { batch: first.batch, row: first.row });
handleEvent({
  type: "tool_result", call_id: "c", name: "tasks", output: "one provisional",
});
if (taskRefreshes !== 1) throw new Error("tasks provisional did not refresh once");
handleEvent({
  type: "tool_result", accepted: true, _event_id: 10,
  call_id: "c", name: "tasks", output: "one final", is_error: true,
  preview: { title: "accepted error preview" }, effect_status: "unknown",
});
if (taskRefreshes !== 1) throw new Error("accepted tasks replacement refreshed twice");
if (previewOpens !== 0) throw new Error("coordinator accepted preview auto-opened");
const firstOutput = first.row.children.find((node) => node.kind === "output");
const firstPreview = first.row.children.find((node) => node.kind === "preview");
if (!firstOutput || firstOutput.output !== "one final" || !firstOutput.isError)
  throw new Error("coordinator accepted error did not replace provisional output");
if (!firstPreview || firstPreview.preview.title !== "accepted error preview")
  throw new Error("coordinator accepted error preview chip missing");
if (first.row.dataset.effectStatus !== "unknown")
  throw new Error("coordinator effect status was not retained");

const second = makeRow("c");
indexLatestToolRow(latestToolRowElements, toolResultNodes, "c", second.row);
toolRows.set("c", { batch: second.batch, row: second.row });
handleEvent({
  type: "tool_result", call_id: "c", name: "other", output: "two provisional",
});
handleEvent({
  type: "tool_result", accepted: true, _event_id: 20,
  call_id: "c", name: "other", output: "two final",
});
if (firstOutput.output !== "one final" || !firstOutput.isConnected)
  throw new Error("coordinator reused id mutated earlier turn");
const secondOutputs = second.row.children.filter((node) => node.kind === "output");
if (secondOutputs.length !== 1 || secondOutputs[0].output !== "two final")
  throw new Error("coordinator newest row did not own accepted replacement");

const acceptedOutput = secondOutputs[0];
handleEvent({
  type: "tool_result", accepted: true, _event_id: 20,
  call_id: "c", name: "other", output: "duplicate corruption",
});
if (second.row.children.find((node) => node.kind === "output") !== acceptedOutput)
  throw new Error("coordinator accepted event replay rendered twice");
renderedToolEventIds.add("7");
handleEvent({
  type: "tool_result", accepted: true, _event_id: 7,
  call_id: "c", name: "other", output: "history corruption",
});
if (second.row.children.some((node) => node.output === "history corruption"))
  throw new Error("coordinator history-seeded replay mutated newest row");

// A result whose batch has not rendered yet paints an orphan bubble (bounded
// /history window, replay edge). When that batch lands afterwards it adopts the
// occurrence, so the accepted result must upgrade the row AND retire the
// bubble — the removal guard can only fire while the orphan's ownership entry
// survives its batch being indexed.
allowOrphanResult = true;
handleEvent({
  type: "tool_result", call_id: "late-batch", name: "A", output: "orphan provisional",
});
const orphanBubble = messagesEl.children[messagesEl.children.length - 1];
const orphanOwner = toolResultNodes.get("late-batch");
if (orphanBubble.kind !== "msg" || !orphanOwner || orphanOwner.row !== null)
  throw new Error("coordinator unmapped result did not paint an orphan bubble");
handleEvent({
  type: "tool_info", items: [{ call_id: "late-batch", func_name: "A" }],
});
handleEvent({
  type: "tool_result", accepted: true, _event_id: 30,
  call_id: "late-batch", name: "A", output: "orphan final",
});
if (orphanBubble.isConnected)
  throw new Error("coordinator late batch left its orphan bubble beside the row");
const lateRow = toolRows.get("late-batch").row;
const lateOutputs = lateRow.children.filter((node) => node.kind === "output");
if (lateOutputs.length !== 1 || lateOutputs[0].output !== "orphan final")
  throw new Error("coordinator late batch row did not own the accepted result");
"""
        % {
            "projection": json.dumps(_TOOL_PROJECTION.as_uri()),
            "handle": handle_event,
            "append_result": append_result,
            "append_to_row": append_to_row,
            "append_batch": append_batch,
        },
    )
    source = _INTERACTIVE.read_text(encoding="utf-8")
    handle_event = _as_function(source, "  handleEvent(evt) {")
    append_output = _as_function(
        source,
        "  appendToolOutput(callId, name, output, isError, preview, opts = {}) {",
    )
    announce_block = _as_function(source, "  announceToolBlock(items) {")
    announce_key = _as_function(source, "  _announceKey(items) {")
    _run_module(
        tmp_path,
        """
import {
  acceptedToolEventAlreadyRendered,
  indexLatestToolRow,
  recordAcceptedToolEvent,
} from %(projection)s;

function classList(...initial) {
  const values = new Set(initial);
  return {
    add(...names) { names.forEach((name) => values.add(name)); },
    remove(...names) { names.forEach((name) => values.delete(name)); },
    contains(name) { return values.has(name); },
  };
}
function makeNode(kind) {
  const node = {
    kind,
    output: "",
    isError: false,
    dataset: {},
    attributes: {},
    classList: classList(),
    parent: null,
    isConnected: false,
    children: [],
    appendChild(child) {
      child.parent = this;
      child.isConnected = true;
      this.children.push(child);
      return child;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    removeAttribute(name) { delete this.attributes[name]; },
    after(child) {
      const siblings = this.parent.children;
      const at = siblings.indexOf(this);
      child.parent = this.parent;
      child.isConnected = true;
      siblings.splice(at + 1, 0, child);
    },
    remove() {
      if (this.parent) {
        const at = this.parent.children.indexOf(this);
        if (at >= 0) this.parent.children.splice(at, 1);
      }
      this.isConnected = false;
    },
    querySelector(selector) {
      if (selector === ".conv-agent") return null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === ".conv-batch")
        return this.children.filter((child) => child.classList.contains("conv-batch"));
      if (selector === ".conv-row")
        return this.children.filter((child) => child.classList.contains("conv-row"));
      return [];
    },
    closest(selector) {
      return selector === ".conv-batch" ? this.parent : null;
    },
    get nextElementSibling() {
      if (!this.parent) return null;
      return this.parent.children[this.parent.children.indexOf(this) + 1] || null;
    },
  };
  Object.defineProperty(node, "className", {
    get() { return this._className || ""; },
    set(value) {
      this._className = String(value);
      this.classList = classList(...String(value).split(/\\s+/).filter(Boolean));
    },
  });
  return node;
}
function makeRow(callId) {
  const batch = makeNode("batch");
  batch.classList = classList("conv-batch", "conv-batch--approved");
  const row = makeNode("row");
  row.dataset.callId = callId;
  row.dataset.funcName = "tool";
  batch.appendChild(row);
  return { batch, row };
}
function stripAnsi(value) { return String(value || ""); }
function tryParseMedia() { return null; }
function buildMediaEmbed(_media, raw) {
  const node = makeNode("media");
  node.output = raw;
  return node;
}
function tryParseMcpError() { return null; }
function buildMcpErrorEmbed() { throw new Error("unexpected MCP path"); }
function renderCollapsibleOutput(output, isError) {
  const node = makeNode("output");
  node.output = output;
  node.isError = !!isError;
  return node;
}
function appendToolErrorBadge(batch) { batch.errorBadges = 1; }
let previewOpens = 0;
function buildPreviewChip(preview, onOpen) {
  const node = makeNode("preview");
  node.preview = preview;
  node.open = onOpen;
  return node;
}
const document = { createElement() { return makeNode("element"); } };
function _convApprovalHead() { return makeNode("head"); }
function batchKicker() { return "Tool"; }
function buildToolDiv(item) {
  const row = makeNode("row");
  row.classList = classList("conv-row");
  row.dataset.callId = item.call_id || "";
  row.dataset.funcName = item.func_name || "tool";
  return row;
}
function indexLabel() { return ""; }
function buildConvVerdict() { return makeNode("verdict"); }
function toolAnnounce() {}
function _toolAnnounceText() { return "tool"; }

const handleEvent = %(handle)s;
const appendToolOutput = %(append)s;
const announceToolBlock = %(announce)s;
const _announceKey = %(announce_key)s;
const latestRows = new Map();
const messagesEl = makeNode("messages");
const pane = {
  _agentCards: null,
  _renderedToolEventIds: new Set(),
  _toolResultNodes: new Map(),
  _streamElIndex: new Map(),
  announcedBlocks: new Map(),
  messagesEl,
  _host: {
    isFocused() { return true; },
    onPreview() { previewOpens++; },
    onConsentDetected() {},
  },
  _toolRow(callId) { return latestRows.get(callId) || null; },
  _routeAgentItems() { return false; },
  _announceKey,
  _indexToolRows(block) {
    block.querySelectorAll(".conv-row").forEach((row) => {
      indexLatestToolRow(latestRows, this._toolResultNodes, row.dataset.callId, row);
    });
  },
  _relinkAgentCards() {},
  _streamEl() { return null; },
  isNearBottom() { return false; },
  scrollToBottom() {},
  appendToolOutput,
  announceToolBlock,
  handleEvent,
};

// Stop may accept a synthesized cancellation while only the early shell is
// present. Acceptance retires that exact shell's map ownership without
// deleting its committed DOM, so a later turn reusing the id paints anew.
pane.handleEvent({
  type: "tool_pending", items: [{ call_id: "cancel-reuse", func_name: "Cancel A" }],
});
const cancelledBatch = messagesEl.children[0];
if (!cancelledBatch || pane.announcedBlocks.size !== 1)
  throw new Error("interactive early tool shell was not announced");
pane.handleEvent({
  type: "tool_result", accepted: true, _event_id: 1,
  call_id: "cancel-reuse", name: "Cancel A", output: "cancelled", is_error: true,
});
if (pane.announcedBlocks.size !== 0 || !cancelledBatch.isConnected)
  throw new Error("interactive accepted cancellation did not retire exact shell ownership");
pane.handleEvent({
  type: "tool_pending", items: [{ call_id: "cancel-reuse", func_name: "Cancel B" }],
});
if (messagesEl.children.length !== 2 || messagesEl.children[0] !== cancelledBatch ||
    !cancelledBatch.isConnected)
  throw new Error("interactive reused pending id removed an accepted prior batch");

const first = makeRow("c");
indexLatestToolRow(latestRows, pane._toolResultNodes, "c", first.row);
pane.handleEvent({
  type: "tool_result", call_id: "c", name: "tool",
  output: "one provisional", preview: { title: "one" },
});
if (previewOpens !== 1) throw new Error("provisional preview did not auto-open once");
const firstOutput = first.batch.children.find((node) => node.kind === "output");

const second = makeRow("c");
indexLatestToolRow(latestRows, pane._toolResultNodes, "c", second.row);
pane.handleEvent({
  type: "tool_result", call_id: "c", name: "tool",
  output: "two provisional", preview: { title: "two" },
});
if (previewOpens !== 2) throw new Error("second provisional preview auto-open mismatch");
pane.handleEvent({
  type: "tool_result", accepted: true, _event_id: 20,
  call_id: "c", name: "tool", output: "two final", is_error: true,
  preview: { title: "two accepted" }, effect_status: "unknown",
});
if (previewOpens !== 2) throw new Error("accepted preview auto-opened a second time");
if (firstOutput.output !== "one provisional" || !firstOutput.isConnected)
  throw new Error("reused id mutated the older turn");
const secondOutputs = second.batch.children.filter((node) => node.kind === "output");
const secondPreviews = second.batch.children.filter((node) => node.kind === "preview");
if (secondOutputs.length !== 1 || secondOutputs[0].output !== "two final" ||
    secondOutputs[0].isError !== true)
  throw new Error("accepted output did not replace provisional output exactly");
if (secondPreviews.length !== 1 || secondPreviews[0].preview.title !== "two accepted")
  throw new Error("accepted error preview chip was lost or duplicated");
if (second.row.dataset.effectStatus !== "unknown")
  throw new Error("accepted effect status was not retained on the row");

const acceptedOutput = secondOutputs[0];
pane.handleEvent({
  type: "tool_result", accepted: true, _event_id: 20,
  call_id: "c", name: "tool", output: "duplicate corruption",
});
if (second.batch.children.filter((node) => node.kind === "output")[0] !== acceptedOutput)
  throw new Error("accepted event-id replay replaced the row twice");
pane._renderedToolEventIds.add("7");
pane.handleEvent({
  type: "tool_result", accepted: true, _event_id: 7,
  call_id: "c", name: "tool", output: "old history corruption",
});
if (second.batch.children.some((node) => node.output === "old history corruption"))
  throw new Error("history-seeded accepted replay mutated newest reused-id row");
"""
        % {
            "projection": json.dumps(_TOOL_PROJECTION.as_uri()),
            "handle": handle_event,
            "append": append_output,
            "announce": announce_block,
            "announce_key": announce_key,
        },
    )


def test_client_send_correlation_and_lost_ack_runtime(tmp_path: Path) -> None:
    """Repeated tokens match FIFO, and SSE acceptance dominates a lost HTTP ACK."""

    _run_module(
        tmp_path,
        f"""
import {{
  clientSendMaySettleForViewer,
  markAcceptedClientSendBubbles,
  sendBubbleWasAccepted,
  settleSendResponse,
}} from {json.dumps(_QUEUE.as_uri())};
const first = {{ dataset: {{ clientSendId: "repeat" }} }};
const second = {{ dataset: {{ clientSendId: "repeat" }} }};
const matched = markAcceptedClientSendBubbles(
  [first, second], ["repeat", "repeat"]
);
if (matched.length !== 2 || matched[0] !== first || matched[1] !== second)
  throw new Error("repeated correlation token collapsed distinct sends");
if (!sendBubbleWasAccepted(first) || !sendBubbleWasAccepted(second))
  throw new Error("acceptance proof missing");
const third = {{ dataset: {{ clientSendId: "repeat" }} }};
const queued = markAcceptedClientSendBubbles(
  [first, second, third], ["repeat"], true
);
if (queued.length !== 1 || queued[0] !== third)
  throw new Error("replayed message_queued re-accepted an earlier bubble");
if (clientSendMaySettleForViewer("mallory", "alice"))
  throw new Error("foreign sender settled this viewer's bubble");
if (!clientSendMaySettleForViewer("alice", "alice"))
  throw new Error("origin sender could not settle its bubble");
if (!clientSendMaySettleForViewer("", "alice"))
  throw new Error("legacy sender-less event lost compatibility");

let consumed = 0;
settleSendResponse(
  {{ addQueuedMessage() {{ throw new Error("recreated accepted bubble"); }} }},
  {{ status: "queue_full", attached_ids: ["a1"] }},
  {{
    queuedEl: null,
    optimisticEl: first,
    isBusy: false,
    displayText: "hello",
    priority: "notice",
    clientSendId: "repeat",
    setBusy() {{ throw new Error("accepted event changed busy"); }},
    busyIsOptimistic() {{ return true; }},
    paneIsBusy() {{ return false; }},
    renderError() {{ throw new Error("accepted event rendered an error"); }},
    consumeAttachments() {{ consumed++; }},
  }},
);
if (consumed !== 1) throw new Error("accepted event did not settle attachments");
""",
    )


def test_accept_user_turn_reducer_runtime(tmp_path: Path) -> None:
    """The one user_turn reducer both panes run: gate, settle, dedupe, nudge."""

    _run_module(
        tmp_path,
        f"""
import {{ acceptUserTurnEvent }} from {json.dumps(_QUEUE.as_uri())};
globalThis.sessionStorage = {{
  getItem(key) {{ return key === "ts.user_id" ? "alice" : null; }},
}};

function makeBubble(clientSendId, queued) {{
  return {{
    dataset: {{ clientSendId }},
    isConnected: true,
    classList: {{ contains: (name) => queued && name === "msg-queued" }},
    remove() {{ this.isConnected = false; }},
  }};
}}
function makeHost(bubbles) {{
  const removedByQueue = [];
  return {{
    renderedEventIds: new Set(),
    messagesEl: {{ querySelectorAll: () => bubbles }},
    queue: {{
      remove(el) {{
        removedByQueue.push(el);
        el.isConnected = false;
      }},
    }},
    removedByQueue,
    consumed: [],
    nudges: 0,
    painted: [],
    consumeAttachments(ids) {{ this.consumed.push(ids); }},
    renderNudgeMarker() {{ this.nudges++; }},
    renderUserTurn(content, attachments, opts) {{
      this.painted.push({{ content, attachments, opts }});
    }},
  }};
}}

// A peer's turn must not settle this viewer's optimistic bubble, and with no
// bubble settled there is no attachment handoff to make.
const foreign = makeBubble("tok", false);
let host = makeHost([foreign]);
acceptUserTurnEvent(
  {{ _event_id: 1, sender: "mallory", client_send_ids: ["tok"],
     attachments: [{{ attachment_id: "a1" }}], content: "hi" }},
  host,
);
if (!foreign.isConnected || host.consumed.length !== 0)
  throw new Error("a foreign sender settled this viewer's bubble");
if (host.painted.length !== 1 || host.painted[0].opts.viewer !== "alice")
  throw new Error("foreign turn did not paint with the resolved viewer");

// The viewer's own turn settles its queued chip THROUGH the queue controller
// and hands the attachment ids to the composer exactly once.
const mine = makeBubble("tok", true);
host = makeHost([mine]);
const own = {{
  _event_id: 2, sender: "alice", client_send_ids: ["tok"],
  attachments: [{{ attachment_id: "a1" }}, {{}}], content: "hi",
}};
acceptUserTurnEvent(own, host);
if (mine.isConnected || host.removedByQueue[0] !== mine)
  throw new Error("queued chip did not leave through the queue controller");
if (host.consumed.length !== 1 || host.consumed[0].join(",") !== "a1")
  throw new Error("settled send did not hand off exactly its attachment ids");

// Replay of the same event id is inert — the paint already happened.
acceptUserTurnEvent(own, host);
if (host.painted.length !== 1 || host.consumed.length !== 1)
  throw new Error("a replayed event id projected twice");

// A wake-driven turn paints the marker instead of a user bubble, and is still
// recorded so its own replay stays inert.
host = makeHost([]);
acceptUserTurnEvent({{ _event_id: 3, source: "system_nudge" }}, host);
acceptUserTurnEvent({{ _event_id: 3, source: "system_nudge" }}, host);
if (host.nudges !== 1 || host.painted.length !== 0)
  throw new Error("system_nudge did not project exactly one marker");

// An id-less turn cannot be deduped, so it must still paint.
host = makeHost([]);
acceptUserTurnEvent({{ content: "no id" }}, host);
acceptUserTurnEvent({{ content: "no id" }}, host);
if (host.painted.length !== 2)
  throw new Error("id-less turns were suppressed by the dedupe set");
""",
    )


@pytest.mark.parametrize(
    ("path", "accept_call", "accept_def", "render_call", "set_name", "next_case"),
    [
        (
            _INTERACTIVE,
            "this._acceptUserTurn(evt);",
            "  _acceptUserTurn(evt) {",
            "this.addUserMessage(",
            "this._renderedUserEventIds",
            'case "stream_overflow"',
        ),
        (
            _COORDINATOR,
            "acceptUserTurn(ev);",
            "  function acceptUserTurn(ev) {",
            "appendUserMessageWithAttachments(",
            "renderedUserEventIds",
            'case "tool_pending"',
        ),
    ],
    ids=["interactive", "coordinator"],
)
def test_user_turn_projects_exactly_once_without_rest_or_stream_reopen(
    path: Path,
    accept_call: str,
    accept_def: str,
    render_call: str,
    set_name: str,
    next_case: str,
) -> None:
    """An upgraded pane renders the live row once without refetch/redial."""

    body = path.read_text(encoding="utf-8")
    case_start = body.index('case "user_turn"')
    case_end = body.index('case "system_turn"', case_start)
    case = _strip_comments(body[case_start:case_end])
    assert accept_call in case
    assert "refetchHistory" not in case
    assert "connectSSE" not in case

    accept_start = body.index(accept_def)
    accept_end = body.index("\n  }", accept_start) + 4
    accept = _strip_comments(body[accept_start:accept_end])
    # The projection ORDER — dedupe check, then paint, then record — is pinned
    # once in the shared reducer now that both panes route through it; the pane
    # contributes only its dedupe set and its DOM writes. Pinning the order per
    # pane would have re-asserted the same three lines twice and left the one
    # place they actually live unpinned.
    assert "acceptUserTurnEvent(" in accept
    assert f"renderedEventIds: {set_name}" in accept
    assert render_call in accept
    assert "refetchHistory" not in accept
    assert "connectSSE" not in accept

    shared = _strip_comments(_QUEUE.read_text(encoding="utf-8"))
    reducer_start = shared.index("export function acceptUserTurnEvent(evt, host) {")
    reducer = shared[reducer_start : shared.index("\n}", reducer_start)]
    seen = reducer.index("host.renderedEventIds.has(eventId)")
    render = reducer.index("host.renderUserTurn(")
    record = reducer.index("host.renderedEventIds.add(eventId)")
    assert seen < render < record
    # An unpainted turn must stay replayable: the nudge branch is the only
    # other paint, and it sits inside the same record-after-paint window.
    assert reducer.index("host.renderNudgeMarker()") < record

    truncated_start = body.index('case "replay_truncated"')
    truncated_end = body.index(next_case, truncated_start)
    truncated = body[truncated_start:truncated_end]
    assert "projection_unsupported" not in truncated


@pytest.mark.parametrize(
    ("path", "append_call", "next_case"),
    [
        (
            _INTERACTIVE,
            "this.appendToolOutput(",
            'case "status"',
        ),
        (
            _COORDINATOR,
            "appendToolResult(",
            'case "approve_request"',
        ),
    ],
    ids=["interactive", "coordinator"],
)
def test_accepted_tool_turn_upserts_once_without_rest_or_stream_reopen(
    path: Path,
    append_call: str,
    next_case: str,
) -> None:
    """The final guarded TOOL row stays on the open stream's ordinary path."""

    body = path.read_text(encoding="utf-8")
    case_start = body.index('case "tool_result"')
    case_end = body.index(next_case, case_start)
    case = _strip_comments(body[case_start:case_end])
    seen = case.index("acceptedToolEventAlreadyRendered(")
    render = case.index(append_call)
    record = case.index("recordAcceptedToolEvent(")
    assert seen < render < record
    for forbidden in ("refetchHistory", "connectSSE", "EventSource"):
        assert forbidden not in case


def test_tool_turn_capability_is_browser_only_and_url_sticky() -> None:
    """Every browser redial opts in; SDK/channel consumers stay legacy-safe."""

    for path in (_INTERACTIVE, _COORDINATOR):
        body = _strip_comments(path.read_text(encoding="utf-8"))
        connect_start = body.index("connectSSE(")
        source_start = body.index("new EventSource", connect_start)
        connect = body[connect_start:source_start]
        assert '"user_turn=1"' in connect
        assert '"&tool_turn=1"' in connect

    for path in (_PY_SDK, _PY_CHANNEL, _TS_SDK):
        assert "tool_turn" not in _strip_comments(path.read_text(encoding="utf-8"))


def test_tool_turn_history_seed_and_reused_id_contracts_are_pinned() -> None:
    """History overlap cannot mutate a later row that reused a provider id."""

    interactive = _strip_comments(_INTERACTIVE.read_text(encoding="utf-8"))
    assert "this._renderedToolEventIds.add(String(msg.event_id))" in interactive
    assert "rows[rows.length - 1]" in interactive
    assert "this._toolResultNodes.delete(callId)" in interactive
    assert "target.dataset.effectStatus" in interactive
    assert "!accepted && !isError && this._host.isFocused(this)" in interactive
    assert "preview && !isDenied && (!isError || accepted)" in interactive

    coordinator = _strip_comments(_COORDINATOR.read_text(encoding="utf-8"))
    assert "renderedToolEventIds.add(String(m.event_id))" in coordinator
    assert "toolRows.set(it.call_id, { batch, row })" in coordinator
    assert "indexLatestToolRow(" in coordinator
    assert "indexHistoryToolOutcomes(historyMessages)" in coordinator
    assert "shiftToolOccurrence(pendingHistoryToolRows, callId)" in coordinator
    assert "row.dataset.effectStatus" in coordinator
    assert "buildPreviewChip(opts.preview" in coordinator


@pytest.mark.parametrize(
    ("path", "refetch_call"),
    [
        (_INTERACTIVE, "this._refetchHistory(this.wsId, token)"),
        (_COORDINATOR, "refetchHistory()"),
    ],
    ids=["pre-projection-interactive", "pre-projection-coordinator"],
)
def test_tokenless_bootstrap_uses_pre_projection_in_place_clear_ui_contract(
    path: Path,
    refetch_call: str,
) -> None:
    """The old clear_ui reducer repairs one open listener and cannot loop."""

    body = path.read_text(encoding="utf-8")
    start = body.index('case "clear_ui"')
    end = body.index('case "history_resync"', start)
    clear_ui = _strip_comments(body[start:end])
    assert refetch_call in clear_ui
    for forbidden in (
        "disconnectSSE",
        "suspendStream",
        "_loadHistoryThenConnect",
        "loadHistoryThenReconnect",
        "connectSSE",
    ):
        assert forbidden not in clear_ui


@pytest.mark.parametrize("path", [_INTERACTIVE, _COORDINATOR], ids=["interactive", "coordinator"])
def test_history_handoff_manual_state_is_fail_closed(path: Path) -> None:
    """Budget exhaustion exposes manual actions without constructing EventSource."""

    body = path.read_text(encoding="utf-8")
    # The attempt budget, the backoff, and the parked prompt live once in the
    # shared controller. Each pane must reach them THROUGH it: re-deriving any
    # of them pane-side is exactly the drift this consolidation removes, so
    # their absence from the pane is the pin.
    assert "createHistoryHandoffRepair(" in body
    for reimplemented in (
        "historyHandoffAttemptAllowed(",
        "HISTORY_HANDOFF_MAX_ATTEMPTS",
        "nextHistoryHandoffDelay(",
        "buildHistoryHandoffPrompt(",
    ):
        assert reimplemented not in body, f"{reimplemented} must stay shared"
    shared_body = _SHARED_HANDOFF.read_text(encoding="utf-8")
    assert "historyHandoffAttemptAllowed(" in shared_body
    assert "HISTORY_HANDOFF_MAX_ATTEMPTS" in shared_body
    assert "nextHistoryHandoffDelay(" in shared_body
    assert "buildHistoryHandoffPrompt(" in shared_body
    assert (
        '"Live updates are paused because conversation history could not be verified."'
        in shared_body
    )
    assert '"Retry now"' in shared_body
    assert '"Reload page"' in shared_body

    connect_start = body.index("connectSSE(")
    source_start = body.index("new EventSource", connect_start)
    connect_prefix = body[connect_start:source_start]
    assert "isRepairing(wsId)" in connect_prefix
    assert "schedule();" in connect_prefix
    assert "return;" in connect_prefix


def test_repair_mode_downgrades_completed_tokenless_render_to_bootstrap() -> None:
    """A rendered tokenless 200 clears the latch instead of parking the pane.

    The server's deliberate cold storage-only read carries no token; the
    repair settle must distinguish it (loader outcome "rendered") from a
    failed fetch/render and downgrade to the tokenless bootstrap — the
    cursorless connect whose convergence the server owns via clear_ui —
    rather than burning the attempt budget against a healthy response.

    The three-way verdict is pinned where it now lives — once, in the shared
    controller. Each pane is pinned only to produce the outcome and hand it
    over; the previous per-pane character-window checks re-asserted the same
    branch twice and could not see the branch ORDER at all.
    """
    for path in (_INTERACTIVE, _COORDINATOR):
        body = path.read_text(encoding="utf-8")
        assert 'return "rendered";' in body
        assert "historyRepair.settle({" in body
        # The proof token stays pane-owned; the pane reports only whether the
        # same response armed one.
        assert "hasToken: this._historyHandoffToken != null" in body or (
            "hasToken: historyHandoffToken != null" in body
        )
        assert 'outcome === "rendered"' not in body, "the verdict must stay shared"

    shared = _strip_comments(_SHARED_HANDOFF.read_text(encoding="utf-8"))
    settle_start = shared.index("settle({ outcome, hasToken, manualAttempt }) {")
    settle = shared[settle_start : shared.index("\n    },", settle_start)]
    # Token handoff and the deliberate tokenless downgrade share ONE success
    # body (round-3 review: the identical twin arms invited drift — a latch
    # added to one but not the other would split pane behavior by outcome).
    success_arm = settle.index('if (hasToken || outcome === "rendered")')
    fail_closed = settle.index("deps.setStale(pending)")
    assert success_arm < fail_closed
    # The success arm clears the latch and reconnects exactly once; only the
    # fail-closed arm may re-arm the budget, and it never reconnects.
    assert settle.count("clear();") == 1
    assert settle.count("deps.connect(target)") == 1
    assert "deps.connect" not in settle[fail_closed:]
    assert "showManual();" in settle[fail_closed:]
    assert "schedule();" in settle[fail_closed:]
