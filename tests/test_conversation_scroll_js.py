"""Behavioral guards for conversation autoscroll and the shared pane wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._js_harness_helpers import FAKE_DOM, extract_braced, node_skip, run_node_source

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_INTERACTIVE = _SHARED / "interactive.js"
_COORDINATOR = _ROOT / "turnstone/console/static/coordinator/coordinator.js"

_HARNESS = (
    FAKE_DOM
    + f"""
const {{ mountConversationScroll }} = await import(
  {json.dumps((_SHARED / "conversation_scroll.js").as_uri())}
);
const presentation = await import(
  {json.dumps((_SHARED / "transcript_presentation.js").as_uri())}
);
"""
    + """
const assert = (ok, message) => { if (!ok) throw new Error(message); };
// Keep window listeners independently removable across multiple panes.
const windowEvents = new FakeElement('window');
window.addEventListener = windowEvents.addEventListener.bind(windowEvents);
window.removeEventListener = windowEvents.removeEventListener.bind(windowEvents);
function input(target, type, event = {}) {
  for (const { fn } of target._listeners.get(type) || []) fn({ target, ...event });
}
FakeElement.prototype.after = function (sibling) {
  const parent = this.parentNode;
  parent.children.splice(parent.children.indexOf(this) + 1, 0, sibling);
  sibling.parentNode = parent;
  sibling._setConnected(parent.isConnected);
};
let nextFrame = 0;
const frames = new Map();
globalThis.requestAnimationFrame = (fn) => { frames.set(++nextFrame, fn); return nextFrame; };
globalThis.cancelAnimationFrame = (id) => frames.delete(id);
function paint() {
  const pending = [...frames.values()];
  frames.clear();
  pending.forEach((fn) => fn());
}
function view() {
  const parent = document.createElement('div');
  const scroller = document.createElement('div');
  scroller.scrollHeight = 1000;
  scroller.clientHeight = 200;
  let top = 800;
  let writes = 0;
  Object.defineProperty(scroller, 'scrollTop', {
    get: () => top,
    set: (value) => {
      writes++;
      top = Math.max(0, Math.min(value, scroller.scrollHeight - scroller.clientHeight));
    },
  });
  html.appendChild(parent);
  parent.appendChild(scroller);
  const follow = mountConversationScroll(scroller);
  const button = parent.querySelector('.conv-scroll-latest');
  const scroll = (value) => {
    input(scroller, 'wheel', { deltaY: value - scroller.scrollTop });
    scroller.scrollTop = value;
    scroller.dispatch('scroll');
  };
  return { parent, scroller, follow, button, scroll, writes: () => writes };
}
"""
)


def _run(body: str) -> None:
    proc = run_node_source(_HARNESS + body)
    assert proc.returncode == 0, proc.stderr


@node_skip
def test_streaming_yields_to_small_upward_scroll_and_resumes_near_bottom() -> None:
    _run("""
const { scroller, follow, button, scroll, writes } = view();
scroller.scrollHeight += 500;
follow.schedule();
follow.schedule();
assert(frames.size === 1, 'streaming must coalesce pins');
paint();
assert(scroller.scrollTop === 1300 && writes() === 1, 'tall output lost the bottom pin');
// Content can arrive before the event from our own previous write.
scroller.scrollHeight += 200;
scroller.dispatch('scroll');
follow.schedule();
paint();
assert(scroller.scrollTop === 1500, 'delayed programmatic scroll event stopped following');

follow.schedule();
scroll(1496);
paint();
assert(scroller.scrollTop === 1496, 'a pending pin swallowed a small upward gesture');
assert(!follow.isFollowing() && !button.hidden, 'scrolling up must offer Jump to latest');
for (let i = 0; i < 5; i++) {
  scroller.scrollHeight += 400;
  follow.schedule();
  paint();
}
assert(scroller.scrollTop === 1496, 'continued streaming pulled the reader down');
scroll(3400);
assert(!follow.isFollowing(), 'following resumed too far from the bottom');
scroll(3452);
assert(follow.isFollowing() && button.hidden, 'downward scroll into the 48px band did not resume');
scroller.scrollHeight += 700;
follow.schedule();
paint();
assert(scroller.scrollTop === 4200, 'resumed following failed on a large append');
""")


@node_skip
def test_scroll_runs_after_markdown_render_even_when_a_new_shell_schedules_first() -> None:
    _run("""
const { scroller, follow } = view();
follow.schedule();
requestAnimationFrame(() => { scroller.scrollHeight += 600; });
follow.schedule();
paint();
assert(scroller.scrollTop === 1400, 'pin ran before the newly queued markdown render');
""")


@node_skip
def test_content_reflow_does_not_disengage_follow_before_the_next_pin() -> None:
    _run("""
const { scroller, follow, button } = view();
// Finishing a reasoning row can clamp the viewport upward. An info message
// arriving before its scroll event then leaves a gap below that position.
scroller.scrollTop -= 17;
scroller.scrollHeight += 81;
follow.schedule();
scroller.dispatch('scroll');
paint();
assert(follow.isFollowing() && button.hidden, 'content reflow was mistaken for reading up');
assert(scroller.scrollTop === 881, 'info message stranded the bottom pin');
// A tool result inserted above the viewport can anchor it downward while
// more output below it grows the gap. That is not a request to pause either.
scroller.scrollHeight += 500;
scroller.scrollTop += 300;
follow.schedule();
scroller.dispatch('scroll');
paint();
assert(follow.isFollowing() && scroller.scrollTop === 1381, 'tool growth stopped following');
""")


@node_skip
def test_scroll_anchoring_does_not_resume_a_paused_reader_near_the_bottom() -> None:
    _run("""
const { scroller, follow, button, scroll } = view();
scroll(796);
// Native anchoring preserves the visible text as an earlier tool row grows.
scroller.scrollHeight += 300;
scroller.scrollTop += 300;
scroller.dispatch('scroll');
assert(!follow.isFollowing() && !button.hidden, 'anchoring resumed a paused reader');
scroller.scrollHeight += 300;
follow.schedule();
paint();
assert(scroller.scrollTop === 1096, 'later output pulled the reader to the bottom');
scroll(1352);
assert(follow.isFollowing(), 'deliberate downward scrolling did not resume');
""")


@node_skip
def test_user_input_still_pauses_during_content_reflow() -> None:
    _run("""
for (const gesture of ['wheel', 'keyboard', 'touch', 'scrollbar']) {
  const { scroller, follow, button } = view();
  scroller.scrollHeight += 400;
  follow.schedule();
  if (gesture === 'wheel') input(scroller, 'wheel', { deltaY: -4 });
  if (gesture === 'keyboard') input(scroller, 'keydown', { key: 'ArrowUp' });
  if (gesture === 'touch') {
    input(scroller, 'touchstart', { touches: [{ clientY: 100 }] });
    input(scroller, 'touchmove', { touches: [{ clientY: 104 }] });
  }
  if (gesture === 'scrollbar') input(scroller, 'pointerdown');
  scroller.scrollTop -= 4;
  scroller.dispatch('scroll');
  input(windowEvents, 'pointerup');
  paint();
  assert(!follow.isFollowing() && !button.hidden, gesture + ' lost to reflow');
  assert(scroller.scrollTop === 796, gesture + ' lost to the pending pin');
  follow.destroy();
}
assert([...windowEvents._listeners.values()].every(rows => rows.length === 0), 'window listener leak');
""")


@node_skip
def test_jump_focus_newer_user_scroll_and_destroy() -> None:
    _run("""
const { scroller, follow, button, scroll, parent } = view();
scroll(200);
button.focus();
button.click();
paint();
assert(scroller.scrollTop === 800 && button.hidden, 'Jump to latest failed');
assert(document.activeElement === scroller, 'jump left focus on the hidden button');
assert(scroller.getAttribute('tabindex') === '-1', 'log did not receive temporary focus');
scroller.dispatch('blur');
assert(!scroller.hasAttribute('tabindex'), 'temporary focus target leaked');

scroll(200);
follow.jumpToLatest();
scroll(190);
paint();
assert(scroller.scrollTop === 190 && !follow.isFollowing(), 'newer scroll lost to explicit jump');
follow.jumpToLatest();
follow.destroy();
paint();
assert(scroller.scrollTop === 190, 'destroyed pane retained a pending pin');
assert(parent.children.length === 1, 'destroy leaked the jump control');
assert(scroller._listeners.get('scroll').length === 0, 'destroy leaked the scroll listener');
assert(resizeObservers.every((o) => o.disconnected), 'destroy leaked its observer');
follow.schedule();
follow.jumpToLatest();
assert(frames.size === 0, 'destroyed pane rearmed scrolling');
""")


@node_skip
def test_hidden_panes_and_resizing_keep_independent_follow_choices() -> None:
    _run("""
const a = view();
const b = view();
b.scroll(200);
for (const v of [a, b]) {
  v.scroller.clientHeight = 0;
  v.scroller._visible = false;
  v.scroller.scrollHeight = 1600;
  triggerResize(v.scroller);
  v.scroller.dispatch('scroll');
  v.follow.schedule();
}
paint();
assert(a.scroller.scrollTop === 800 && b.scroller.scrollTop === 200, 'hidden pane was scrolled');
assert(a.follow.isFollowing() && !b.follow.isFollowing(), 'hidden geometry replaced user intent');
for (const v of [a, b]) {
  v.scroller.clientHeight = 300;
  v.scroller._visible = true;
  triggerResize(v.scroller);
}
paint();
assert(a.scroller.scrollTop === 1300, 'following pane did not catch up on reveal');
assert(b.scroller.scrollTop === 200 && !b.button.hidden, 'reading pane moved on reveal');
a.scroller.clientHeight = 100;
triggerResize(a.scroller);
paint();
assert(a.scroller.scrollTop === 1500, 'shrinking the pane lost the bottom pin');
""")


@node_skip
def test_compact_transcript_reflows_respect_a_paused_follow_inside_threshold() -> None:
    _run("""
const v = view();
presentation.registerTranscriptScroller(v.scroller, {
  isFollowing: v.follow.isFollowing, scrollToBottom: v.follow.schedule,
});
v.scroll(796);
presentation.preserveTranscriptBottomPin(v.scroller, () => { v.scroller.scrollHeight += 400; });
presentation.setTranscriptPresentation('compact');
paint();
assert(v.scroller.scrollTop === 796, 'presentation reflow overrode a small upward scroll');
assert(!v.follow.isFollowing(), 'presentation silently resumed following');
v.follow.jumpToLatest();
paint();
presentation.preserveTranscriptBottomPin(v.scroller, () => { v.scroller.scrollHeight += 400; });
v.scroll(1190);
paint();
assert(v.scroller.scrollTop === 1190, 'late presentation restore defeated a newer scroll');
""")


@node_skip
def test_presentation_restores_share_the_panes_programmatic_scroll_tracking() -> None:
    _run("""
const v = view();
presentation.registerTranscriptScroller(v.scroller, {
  isFollowing: v.follow.isFollowing, scrollToBottom: v.follow.schedule,
});
presentation.preserveTranscriptBottomPin(v.scroller, () => { v.scroller.scrollHeight += 400; });
paint();
paint();
assert(v.scroller.scrollTop === 1200, 'presentation restore did not pin the bottom');
v.scroller.scrollHeight += 400;
v.scroller.dispatch('scroll');
v.follow.schedule();
paint();
assert(v.scroller.scrollTop === 1600, 'presentation scroll event disengaged streaming follow');
""")


@node_skip
def test_interactive_stream_end_and_system_updates_use_the_shared_follow_state() -> None:
    body = _INTERACTIVE.read_text()
    methods = "\n".join(
        extract_braced(body, marker)
        for marker in (
            "  scrollToBottom(force) {",
            "  handleEvent(evt) {",
            "  addSystemContext(content, source, meta) {",
        )
    )
    _run(
        """
const v = view();
const resetCompactionHolder = () => {};
const streamingRenderFinalize = () => { v.scroller.scrollHeight += 600; };
const buildCompactionCard = () => document.createElement('div');
const buildWatchResultCard = buildCompactionCard;
const _buildGuardFindingBubble = buildCompactionCard;
const operatorSourceLabel = (source) => source;
const pane = {
  _scrollFollow: v.follow, messagesEl: v.scroller,
  _reasoningActivity: { finish() {} }, _compaction: {},
  removeEmptyState() {},
"""
        + methods.replace("\n  }", "\n  },")
        + """
};
v.scroll(200);
pane.currentAssistantBodyEl = document.createElement('div');
pane.contentBuffer = 'finished answer';
pane.handleEvent({ type: 'stream_end' });
for (const source of ['compaction', 'watch_triggered', 'output_guard', 'user_interjection']) {
  pane.addSystemContext('update', source, { message: 'queued elsewhere' });
}
paint();
assert(v.scroller.scrollTop === 200, 'background completion/system output forced a jump');
pane.scrollToBottom(true);
paint();
assert(v.scroller.scrollTop === 1400, 'explicit local send did not resume following');
"""
    )


@node_skip
@pytest.mark.parametrize("surface", ["interactive", "coordinator"])
def test_edit_resend_resumes_following_even_if_history_refetch_fails(surface: str) -> None:
    common = """
const v = view();
const posts = [];
const STALE_RETRY_BASE_MS = 2000, STALE_RETRY_JITTER_MS = 0;
const mintClientSendId = () => 'edit-send-id';
const parsePriority = text => ({ displayText: text, priority: 'notice' });
const authFetch = (url, init) => {
  posts.push({ url, body: JSON.parse(init.body) });
  return Promise.resolve({ ok: true });
};
const postAndSettleSend = () => {};
const append = v.scroller.appendChild.bind(v.scroller);
v.scroller.appendChild = el => { v.scroller.scrollHeight += 200; return append(el); };
v.scroll(200);
"""
    if surface == "interactive":
        body = _INTERACTIVE.read_text()
        methods = "\n".join(
            extract_braced(body, marker)
            for marker in (
                "  scrollToBottom(force) {",
                "  addUserMessage(text, attachments, opts) {",
                "  handleEvent(evt) {",
            )
        )
        source = "class Pane {\n" + methods + "\n}\n" + common
        source += """
const pane = new Pane();
Object.assign(pane, {
  wsId: 'ws', _base: '', _historyLoadToken: 1, _pendingEditSend: 'edited local message',
  _scrollFollow: v.follow, messagesEl: v.scroller,
  _beginReplayQuiesce() {},
  // A failed refetch resolves and retains the old transcript; the committed edit still sends.
  _refetchHistory() { return Promise.resolve(); },
  removeEmptyState() {}, _addUserMsgActions() {},
  setBusy(value, source) { this.busy = value; this.busySource = source; },
  addErrorMessage(message) { throw Error(message); },
});
pane.handleEvent({ type: 'clear_ui' });
await Promise.resolve(); await Promise.resolve();
clearTimeout(pane._staleRetryTimer);
"""
    else:
        body = _COORDINATOR.read_text()
        source = common + "\n".join(
            extract_braced(body, marker)
            for marker in (
                "  function handleEvent(ev) {",
                "  function appendUserMessageWithAttachments(text, attachments, opts) {",
                "  function appendMsg(role, html, opts) {",
                "  function appendText(role, text, opts) {",
                "  function _scheduleScroll() {",
            )
        )
        source += """
const wsId = 'ws', queue = {}, composer = { value: '' };
const scrollFollow = v.follow, messagesEl = v.scroller;
const _MSG_VARIANTS = { user: 'user' }, esc = text => text;
const setSafeHtml = (el, html) => { el.textContent = html; };
const _addUserMsgActions = () => {};
const refetchHistory = () => Promise.resolve();
let historyStale = false, staleRetryTimer = null, visHandler = () => {};
let _pendingEditSend = 'edited local message', busy = false, busySource = '';
function setBusy(value, source) { busy = value; busySource = source; }
handleEvent({ type: 'clear_ui' });
await Promise.resolve(); await Promise.resolve();
clearTimeout(staleRetryTimer);
"""
    _run(
        source
        + """
paint();
assert(posts.length === 1 && posts[0].body.message === 'edited local message', 'edit did not send');
assert(v.follow.isFollowing() && v.button.hidden, 'local resend left following paused');
assert(v.scroller.scrollTop === 1000, 'local edited message was left out of view');
"""
    )


@node_skip
def test_coordinator_only_folds_live_results_outside_the_reading_viewport() -> None:
    append = extract_braced(
        _COORDINATOR.read_text(),
        "  function appendToolResult(name, callId, output, isError, opts) {",
    )
    _run(
        f"""
const {{ markConvRowResultSettled }} = await import(
  {json.dumps((_SHARED / "conversation.js").as_uri())}
);
"""
        + """
const { canAutoFoldTranscriptBatch, getTranscriptPresentation } = presentation;
const toolRows = new Map();
const toolResultNodes = new Map();
let messagesEl, scrollFollow;
function _appendResultToRow(row, output) {
  const result = document.createElement('div');
  result.className = 'conv-row-result';
  result.textContent = output;
  row.appendChild(result);
  return result;
}
function _unsetBatchRunningIfAllResults() {}
function _announcePolite() {}
function convBatchSummaryText() { return 'finished'; }
function _scheduleScroll() { scrollFollow.schedule(); }
"""
        + append
        + """
presentation.setTranscriptPresentation('compact');
for (const [paused, belowViewport, shouldFold] of [
  [true, false, false], [true, true, true], [false, false, true],
]) {
  const v = view();
  messagesEl = v.scroller;
  scrollFollow = v.follow;
  presentation.registerTranscriptScroller(messagesEl, {
    isFollowing: scrollFollow.isFollowing, scrollToBottom: scrollFollow.schedule,
  });
  messagesEl.getBoundingClientRect = () => ({ top: 0, bottom: 200 });
  const batch = document.createElement('div');
  batch.className = 'conv-batch conv-batch--auto';
  batch.getBoundingClientRect = () => ({ top: belowViewport ? 250 : 50, bottom: 350 });
  const row = document.createElement('div');
  row.className = 'conv-row';
  row.dataset.effectStatus = 'committed';
  batch.appendChild(row);
  messagesEl.appendChild(batch);
  toolRows.set('call', { batch, row });
  if (paused) v.scroll(700);
  appendToolResult('bash', 'call', 'Finished output', false, { accepted: true });
  assert(row.dataset.resultSettled === 'true', 'accepted result was not settled');
  assert((batch.dataset.compactFolded === 'true') === shouldFold,
    'coordinator folded a result being read, or lost safe automatic folding');
}
"""
    )


def test_both_panes_route_queue_and_lifecycle_through_the_shared_controller() -> None:
    interactive = _INTERACTIVE.read_text()
    coordinator = _COORDINATOR.read_text()
    assert 'from "./conversation_scroll.js"' in interactive
    assert 'from "/shared/conversation_scroll.js"' in coordinator
    assert "scroll: () => this.scrollToBottom()" in interactive
    assert "scroll: () => _scheduleScroll()" in coordinator
    assert "pane._scrollFollow.destroy()" in interactive
    assert "scrollFollow.destroy()" in coordinator
    schedule = extract_braced(coordinator, "  function _scheduleScroll() {")
    assert "scrollFollow.schedule()" in schedule
    queue = (_SHARED / "composer_queue.js").read_text()
    assert "scrollTop =" not in queue
