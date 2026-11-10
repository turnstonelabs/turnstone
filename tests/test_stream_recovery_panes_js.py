"""Workstream stream and recovery behavior, executed through each pane's public surface.

Both panes (``shared_static/interactive.js`` and the console coordinator) run whole under node
with the browser fakes from tests/_js_harness_helpers.py, and both run on the shared sync core
(``shared_static/workstream_sync.js``).  A test touches only the pane's construction and its
public controller, the EventSource fake (the frames it delivers, its errors, the URLs it was
opened with), the fetch queue, the virtual clock, tab visibility and the rendered DOM; no test
reads or calls a private field or method of either pane.  Most tests run for both clients; a
surface only one pane has (the interactive empty state, the coordinator's snapshot GET, sidebar
and controller) gets a single-client test.  A failed expectation, a harness error, or a throw
inside a pane callback fails the test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests._js_harness_helpers import (
    FAKE_CLOCK,
    FAKE_DOM,
    FAKE_EVENT_SOURCE,
    FAKE_FETCH,
    FAKE_VISIBILITY,
    FULL_SELECTORS,
    PANE_DOM,
    REMOVE_CHILD,
    coordinator_module_for_node,
    node_skip,
    run_node_source,
)
from tests._resume_stream_contract import resumed_stream_frames

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1]
_SHARED = _ROOT / "turnstone/shared_static"
_CLIENTS = ("interactive", "coordinator")


class PaneExpectationError(AssertionError):
    """The pane missed a behavior expectation of the test (the harness itself ran)."""


class PaneHarnessError(Exception):
    """The pane script failed outside a behavior expectation."""


# The per-client driver: mounts the pane through its public factory and gives
# every test the same verbs.  ``beforeHistory`` answers the requests a client
# makes ahead of /history on first paint (the coordinator's workstream snapshot
# request).
_DRIVER = r"""
const WS = 'ws/2';
const WS_PATH = '/v1/api/workstreams/ws%2F2';
const textOf = node => {
  if (!node) return '';
  let text = (node.textContent || '') + (node._html || '');
  for (const child of node.children || []) text += ' ' + textOf(child);
  return text;
};
const stream = source => {
  const url = new URL(source.url);
  return {path: url.pathname, params: url.searchParams, keys: [...url.searchParams.keys()]};
};
const sources = () => FakeEventSource.instances;
const overflow = await import(SHARED + '/sse_overflow.js');
const root = document.createElement('div');
html.appendChild(root);
let pane;
if (CLIENT === 'interactive') {
  const {createInteractivePane} = await import(SHARED + '/interactive.js');
  const ctl = createInteractivePane(root, WS, {nodeId: 'lab'});
  pane = {
    base: '/node/lab',
    start: async () => { ctl.connect(); await flush(); },
    beforeHistory: async () => {},
    login: async () => { ctl.onLogin(); await flush(); },
    destroy: async () => { ctl.destroy(); await flush(); },
    transcript: () => root.querySelector('.pane-messages'),
  };
} else {
  // The standalone coordinator page script-tags these modules and the pane
  // reads their window bridges as globals.
  for (const name of ['utils', 'toast', 'auth', 'composer', 'composer_attachments',
    'composer_queue', 'renderer', 'status_bar']) {
    Object.assign(globalThis, await import(SHARED + '/' + name + '.js'));
  }
  const {createCoordinatorPane} = await import(COORDINATOR);
  const controller = createCoordinatorPane(root, WS, {});
  pane = {
    base: '',
    start: async () => { controller.connect(); await flush(); },
    beforeHistory: async () => {
      await network.reply(WS_PATH, {ws_id: WS, name: 'coordinator', state: 'idle'});
    },
    login: async () => { controller.onLogin(); await flush(); },
    destroy: async () => { controller.destroy(); await flush(); },
    transcript: () => root.querySelector('#coord-messages'),
  };
}
const transcriptText = () => textOf(pane.transcript());
// A user send through the composer DOM: type, then click Send.
pane.send = async text => {
  root.querySelector('.composer-input').value = text;
  root.querySelector('.composer-send').click();
  await flush();
};
// First paint through to the stream: answers /history with ``history`` and
// returns the stream the pane opened.
const firstPaint = async (history = {messages: []}) => {
  await pane.start();
  await pane.beforeHistory();
  await network.reply('/history', history);
  return FakeEventSource.latest();
};
const user = content => ({role: 'user', content});
// A click as a browser dispatches it: the listeners get an event they can stop.
const press = button => button.dispatchEvent({type: 'click', bubbles: true, target: button,
  stopPropagation() {}, preventDefault() {}});
"""

# Outside the body's try block: the verdict needs both after the body threw.
# ``expect(condition, label, detail)``: the label is fixed text, unique within a
# test; the optional detail carries the values seen.
_VERDICT_JS = r"""
class Expectation extends Error {
  constructor(label, detail) {
    super(label);
    this.label = label;
    this.detail = detail;
  }
}
const expect = (condition, label, detail) => {
  if (!condition) throw new Expectation(label, detail);
};
const reportUncaught = () => {
  if (!reportedErrors.length) return;
  for (const {where, err} of reportedErrors) console.error('uncaught in ' + where, err);
  console.error('PANE-HARNESS: ' + reportedErrors.length + ' uncaught error(s) in pane callbacks');
  process.exit(4);
};
"""


def _script(client: str, body: str, params: dict[str, Any], coordinator: Path) -> str:
    prelude = (
        f"const CLIENT = {json.dumps(client)};\n"
        f"const SHARED = {json.dumps(_SHARED.as_uri())};\n"
        f"const COORDINATOR = {json.dumps(coordinator.as_uri())};\n"
        f"const PARAMS = {json.dumps(params)};\n"
    )
    return (
        FAKE_DOM
        + PANE_DOM
        + FULL_SELECTORS
        + REMOVE_CHILD
        + FAKE_CLOCK
        + FAKE_VISIBILITY
        + FAKE_FETCH
        + FAKE_EVENT_SOURCE
        + prelude
        + _VERDICT_JS
        + "try {\n"
        + _DRIVER
        + body
        + "\n} catch (err) {\n"
        + "  reportUncaught();\n"
        + "  if (!(err instanceof Expectation)) throw err;\n"
        + "  console.error('PANE-EXPECTATION-FAILED: ' + err.label);\n"
        + "  if (err.detail !== undefined) {\n"
        + "    console.error('PANE-EXPECTATION-DETAIL: ' + err.detail);\n"
        + "  }\n"
        + "  process.exit(3);\n"
        + "}\n"
        + "reportUncaught();\n"
    )


def _verdict(proc: Any) -> None:
    if proc.returncode == 0:
        return
    output = f"{proc.stderr}\n{proc.stdout}"
    match = re.search(r"^PANE-EXPECTATION-FAILED: (.*)$", proc.stderr, re.M)
    if match is None:
        raise PaneHarnessError(f"pane script failed outside an expectation:\n{output}")
    raise PaneExpectationError(f"{match.group(1)}\n{output}")


@pytest.fixture
def run_pane(tmp_path: Path) -> Callable[..., None]:
    """Run a test body against one client.  The coordinator module is served
    from a copy whose ``/shared/`` imports point at the shared directory on disk
    (the only rewrite; no internals are spliced in)."""
    coordinator = coordinator_module_for_node(tmp_path)

    def run(client: str, body: str, *, params: dict[str, Any] | None = None) -> None:
        proc = run_node_source(_script(client, body, params or {}, coordinator), timeout=60)
        _verdict(proc)

    return run


@pytest.mark.parametrize("cursor", [7, 0, None], ids=["cursor-7", "cursor-0", "no-cursor"])
@pytest.mark.parametrize("client", _CLIENTS)
def test_first_paint_loads_history_then_opens_the_stream_at_its_cursor(
    run_pane: Callable[..., None], client: str, cursor: int | None
) -> None:
    """First paint fetches /history, renders it, and only then opens the stream,
    whose URL carries the /history cursor (0 included: a valid cursor), the two
    capability params, and the handoff token last."""
    run_pane(
        client,
        r"""
await pane.start();
await pane.beforeHistory();
const pending = network.pending('/history');
expect(pending.length === 1, 'first paint requests /history once');
expect(pending[0].url === pane.base + WS_PATH + '/history', 'the history URL carries the base',
  pending[0].url);
expect(sources().length === 0, 'no stream opens before /history answers');
const textAtOpen = [];
const Recording = class extends FakeEventSource {
  constructor(...args) { super(...args); textAtOpen.push(transcriptText()); }
};
globalThis.EventSource = Recording;
await network.reply('/history', {messages: [user('first question')], cursor: PARAMS.cursor,
  handoff_token: 'tok-1'});
expect(sources().length === 1, 'one stream opens after /history', sources().length);
expect(textAtOpen[0].includes('first question'), 'the history rendered before the stream opened');
const opened = stream(sources()[0]);
expect(opened.path === pane.base + WS_PATH + '/events', 'the stream path carries the base',
  opened.path);
if (PARAMS.cursor === null) {
  expect(!opened.params.has('last_event_id'), 'no cursor, no last_event_id');
} else {
  expect(opened.params.get('last_event_id') === String(PARAMS.cursor),
    'the stream presents the /history cursor', opened.params.get('last_event_id'));
}
expect(opened.params.get('user_turn') === '1' && opened.params.get('tool_turn') === '1',
  'capability params on the stream URL');
expect(opened.keys.at(-1) === 'history_token' && opened.params.get('history_token') === 'tok-1',
  'the handoff token is the last param', opened.keys.join(','));
""",
        params={"cursor": cursor},
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_malformed_frame_is_dropped_but_advances_the_cursor(
    run_pane: Callable[..., None], client: str
) -> None:
    """A frame whose data is not JSON renders nothing and stops nothing, but its
    id still becomes the cursor the next connect presents.  The malformed frame
    is the last one before the reconnect, so only its own id can explain the
    cursor (an id-less frame would carry the previous id anyway)."""
    run_pane(
        client,
        r"""
const es = await firstPaint();
es.open();
es.deliver({type: 'content', text: 'alpha'}, {id: 4});
es.deliver({type: 'content', text: ' beta'});
await flush();
expect(transcriptText().includes('alpha') && transcriptText().includes('beta'),
  'frames before the malformed one render');
const before = transcriptText();
es.deliver('{not json', {id: 5});
await flush();
expect(transcriptText() === before, 'the malformed frame renders nothing');
visibility.hide();
visibility.show();
await flush();
expect(sources().length === 2, 'hide then show reconnects');
expect(stream(sources()[1]).params.get('last_event_id') === '5',
  'the malformed frame advanced the cursor', stream(sources()[1]).params.get('last_event_id'));
sources()[1].open();
sources()[1].deliver({type: 'content', text: ' gamma'}, {id: 6});
await flush();
expect(transcriptText().includes('gamma'), 'the pipeline still renders after a malformed frame');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_hide_closes_the_stream_and_show_reopens_at_the_last_cursor(
    run_pane: Callable[..., None], client: str
) -> None:
    """Hiding the tab closes the stream and no timer reopens it while hidden;
    showing it opens one new stream presenting the last id seen (an id-less frame
    after it keeps that id)."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [], cursor: 2});
es.open();
es.deliver({type: 'content', text: 'one'}, {id: 5});
es.deliver({type: 'stream_end'}, {id: 6});
es.deliver({type: 'content', text: 'two'});
visibility.hide();
await flush();
expect(es.closeCalled && es.readyState === EventSource.CLOSED, 'hide closes the stream');
await clock.advance(60000);
expect(sources().length === 1, 'no stream opens while the tab is hidden');
visibility.show();
await flush();
expect(sources().length === 2, 'show opens one stream', sources().length);
expect(stream(sources()[1]).params.get('last_event_id') === '6',
  'show presents the last cursor', stream(sources()[1]).params.get('last_event_id'));
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_transient_error_keeps_the_same_stream(run_pane: Callable[..., None], client: str) -> None:
    """A drop the browser is retrying is not closed by the client: the same
    EventSource reconnects, no second stream opens, and it keeps rendering."""
    run_pane(
        client,
        r"""
const es = await firstPaint();
es.open();
es.deliver({type: 'content', text: 'before drop'}, {id: 3});
es.drop();
await flush();
expect(!es.closeCalled, 'a transient error does not close the stream');
await clock.advance(1000);
es.open();
await clock.advance(30000);
expect(sources().length === 1 && !es.closeCalled, 'the same stream survives the error');
es.deliver({type: 'content', text: ' after reconnect'}, {id: 4});
await flush();
expect(transcriptText().includes('after reconnect'), 'the reconnected stream renders');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_stream_error_does_not_request_the_workstream(
    run_pane: Callable[..., None], client: str
) -> None:
    """A stream error never requests the workstream resource: for a writer that
    GET reopens an unloaded workstream, closed ones included, and the stream
    layer must not reopen a closed one. The console's coordinator /events route
    resumes a coordinator that was live at a restart, and refuses a closed one."""
    run_pane(
        client,
        r"""
const es = await firstPaint();
es.open();
const before = network.seen(WS_PATH).length;
es.drop();
await clock.advance(6000);
es.open();
expect(network.seen(WS_PATH).length === before,
  'a stream error does not request the workstream resource');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_three_overflow_closes_enter_the_degraded_cooldown(
    run_pane: Callable[..., None], client: str
) -> None:
    """Three overflow closes inside the window close the stream for the cooldown:
    no stream opens until it elapses, then exactly one.  Each overflow is
    followed by the server ending the stream and the browser reconnecting, as on
    the wire."""
    run_pane(
        client,
        r"""
const es = await firstPaint();
es.open();
for (let i = 0; i < 2; i += 1) {
  es.deliver({type: 'stream_overflow'});
  es.drop();
  await clock.advance(1000);
  es.open();
}
es.deliver({type: 'stream_overflow'});
es.drop();
await flush();
expect(es.closeCalled, 'the third overflow closes the stream');
await clock.advance(overflow.DEGRADED_COOLDOWN_BASE_MS - 1);
expect(sources().length === 1, 'no new stream before the cooldown elapses', sources().length);
await clock.advance(1);
expect(sources().length === 2, 'one stream after the cooldown', sources().length);
""",
    )


@pytest.mark.parametrize("outcome", ["rendered", "failed"])
@pytest.mark.parametrize("client", _CLIENTS)
def test_replay_truncated_resyncs_after_jitter(
    run_pane: Callable[..., None], client: str, outcome: str
) -> None:
    """A truncation envelope on an idle pane waits out its jitter with the stream
    still up, then closes it and loads /history; a rendered resync reconnects at
    the /history cursor, a failed one keeps the transcript and re-presents the
    cursor the envelope arrived at, not the live one that moved on since."""
    run_pane(
        client,
        r"""
pinRandom(0.5);
const jitter = 0.5 * overflow.TRUNCATED_RESYNC_JITTER_MS;
const es = await firstPaint({messages: [user('old question')]});
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 5});
es.deliver({type: 'replay_truncated'});
es.deliver({type: 'state_change', state: 'idle'}, {id: 6});
es.deliver({type: 'state_change', state: 'idle'}, {id: 7});
await clock.advance(jitter - 1);
expect(network.pending('/history').length === 0, 'the resync waits for its jitter');
expect(!es.closeCalled, 'the stream stays up during the jitter');
await clock.advance(1);
expect(network.pending('/history').length === 1, 'the resync loads /history after the jitter');
expect(es.closeCalled && sources().length === 1, 'the resync closes the stream while it loads');
if (PARAMS.outcome === 'rendered') {
  await network.reply('/history', {messages: [user('new question')], cursor: 9});
  expect(sources().length === 2, 'the resync reconnects');
  expect(stream(sources()[1]).params.get('last_event_id') === '9',
    'the resync adopts the /history cursor', stream(sources()[1]).params.get('last_event_id'));
  expect(transcriptText().includes('new question') && !transcriptText().includes('old question'),
    'the resync re-rendered the transcript');
} else {
  await network.reply('/history', {detail: 'unavailable'}, {status: 500});
  expect(sources().length === 2, 'a failed resync still reconnects');
  expect(stream(sources()[1]).params.get('last_event_id') === '5',
    'a failed resync re-presents the truncation cursor',
    stream(sources()[1]).params.get('last_event_id'));
  expect(transcriptText().includes('old question'), 'a failed resync keeps the transcript');
}
""",
        params={"outcome": outcome},
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_no_stream_opens_while_a_resync_loads(run_pane: Callable[..., None], client: str) -> None:
    """While a truncated resync's /history is in flight the stream stays down,
    even when a transient error shortly before armed the closed-stream check."""
    run_pane(
        client,
        r"""
pinRandom(0);
const es = await firstPaint();
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 5});
es.drop();
await clock.advance(1000);
es.open();
es.deliver({type: 'replay_truncated'});
await clock.advance(0);
expect(network.pending('/history').length === 1 && es.closeCalled, 'the resync is loading');
await clock.advance(10000);
expect(sources().length === 1, 'no stream opens while the resync loads', sources().length);
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_content_during_a_clear_ui_refetch_is_held_until_the_render(
    run_pane: Callable[..., None], client: str
) -> None:
    """Content arriving while a ``clear_ui`` refetch is in flight is held, then
    replayed once after the rebuilt transcript."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('before rewind')]});
es.open();
es.deliver({type: 'clear_ui'}, {id: 4});
await flush();
expect(network.pending('/history').length === 1, 'clear_ui refetches /history');
es.deliver({type: 'content', text: 'live tail'}, {id: 5});
await flush();
expect(!transcriptText().includes('live tail'),
  'content during the refetch is held, not painted live');
await network.reply('/history', {messages: [user('after rewind')]});
const text = transcriptText();
expect(text.includes('after rewind') && !text.includes('before rewind'), 'the refetch rendered');
expect(text.indexOf('live tail') > text.indexOf('after rewind'),
  'the held content replays after the render');
expect(text.split('live tail').length === 2, 'the held content renders once');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_reconnect_inside_a_seedless_refetch_declines_its_render(
    run_pane: Callable[..., None], client: str
) -> None:
    """A stream that drops and re-opens while a ``clear_ui`` refetch is awaiting
    /history reads OPEN again, but its replay may already carry what the
    snapshot holds: the render is declined and the transcript stays."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('before rewind')]});
es.open();
es.deliver({type: 'clear_ui'}, {id: 4});
await flush();
expect(network.pending('/history').length === 1, 'clear_ui refetches /history');
es.drop();
await clock.advance(1000);
es.open();
await network.reply('/history', {messages: [user('after rewind')]});
expect(!transcriptText().includes('after rewind'), 'a render across a reconnect is declined');
expect(transcriptText().includes('before rewind'), 'the transcript before the reconnect stays');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_send_inside_a_seedless_refetch_keeps_its_row(
    run_pane: Callable[..., None], client: str
) -> None:
    """A user send made while a ``clear_ui`` refetch awaits /history paints its
    row at once; the snapshot may not hold that row yet, so the render is
    declined instead of wiping it."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('before rewind')]});
es.open();
es.deliver({type: 'clear_ui'}, {id: 4});
await flush();
expect(network.pending('/history').length === 1, 'clear_ui refetches /history');
await pane.send('my new message');
expect(network.pending('/send').length === 1, 'the send posts');
expect(transcriptText().includes('my new message'), 'the send paints its row at once');
await network.reply('/history', {messages: [user('after rewind')]});
expect(transcriptText().includes('my new message'),
  'a send inside the refetch keeps its row: the render is declined');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_a_refetch_of_a_trimmed_history_renders_seeded_and_reconnects_from_its_cursor(
    run_pane: Callable[..., None], client: str
) -> None:
    """/history returns a cursor when it trimmed a running turn it expects the caller to replay
    from that cursor, so a ``clear_ui`` refetch of it renders as a seeded load does: the trimmed
    history replaces the transcript and the stream is rebuilt at the cursor, whose replay
    restores the turn."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('before rewind')]});
es.open();
es.deliver({type: 'clear_ui'}, {id: 4});
await flush();
await network.reply('/history', {messages: [user('after rewind')], cursor: 3});
expect(transcriptText().includes('after rewind') && !transcriptText().includes('before rewind'),
  'the trimmed /history renders');
const next = FakeEventSource.latest();
expect(next !== es && stream(next).params.get('last_event_id') === '3',
  'the stream is rebuilt at the cursor /history returned', next.url);
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_destroy_during_the_first_history_load_opens_no_stream(
    run_pane: Callable[..., None], client: str
) -> None:
    """Destroying the pane while its first /history is in flight leaves no
    stream and no visibility listener behind, whether or not the pane aborted
    the request (a late reply to an aborted request is discarded)."""
    run_pane(
        client,
        r"""
await pane.start();
await pane.beforeHistory();
const history = network.take('/history');
await pane.destroy();
history.reply({messages: [user('late history')], cursor: 3});
await clock.advance(60000);
expect(sources().length === 0, 'no stream opens after destroy');
expect(visibility.listenerCount() === 0, 'no visibility listener survives destroy');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_destroy_closes_the_stream_and_releases_visibility(
    run_pane: Callable[..., None], client: str
) -> None:
    """Destroying a live pane closes its stream and drops its visibility
    listener; no later show edge or timer opens another stream."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('kept question')], cursor: 3});
es.open();
expect(visibility.listenerCount() === 1, 'a live pane listens for visibility changes');
await pane.destroy();
expect(es.closeCalled && es.readyState === EventSource.CLOSED, 'destroy closes the stream');
expect(visibility.listenerCount() === 0, 'destroy removes the visibility listener');
visibility.hide();
visibility.show();
await clock.advance(120000);
expect(sources().length === 1, 'nothing reopens a destroyed pane', sources().length);
""",
    )


def test_interactive_failed_first_paint_keeps_the_empty_state(
    run_pane: Callable[..., None],
) -> None:
    """Interactive only (the coordinator shows no empty state): a first /history
    that fails still leaves the empty-state placeholder, which the pane seeds
    before the load because the failure path paints nothing.  The
    stream still opens after the failed load."""
    run_pane(
        "interactive",
        r"""
await pane.start();
expect(transcriptText().includes('Type a message to start'),
  'the placeholder shows during the load');
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(transcriptText().includes('Type a message to start'),
  'a failed first paint keeps the placeholder');
expect(sources().length === 1, 'the stream opens after a failed first paint');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_approval_cards_keep_working_after_a_failed_reload(
    run_pane: Callable[..., None], client: str
) -> None:
    """A reload drops the reply bubble refs and nothing else, so an
    approval card still on screen after the reload's /history failed still resolves its cycle
    (its click posts the decision) instead of going dead under the user's cursor."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('run it')]});
es.open();
es.deliver({type: 'approve_request', cycle_id: 'cy1',
  items: [{call_id: 'c1', func_name: 'bash', needs_approval: true, header: 'bash',
    preview: 'ls'}]}, {id: 4});
await flush();
const approve = pane.transcript().querySelector('.conv-btn--approve');
expect(approve, 'the approval card is on screen');
es.deliver({type: 'history_resync'});
await flush();
expect(network.pending('/history').length === 1, 'the repair reloads /history');
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
expect(approve.isConnected, 'a failed reload leaves the card on screen');
// The fake DOM's inputs carry no value; a browser's empty feedback box reads "".
for (const input of pane.transcript().querySelectorAll('input')) input.value = '';
approve.click();
await flush();
expect(network.pending('/approve').length === 1, 'the card still posts its decision',
  network.seen('/approve').length + ' approve requests');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_a_server_busy_turn_is_not_a_send_in_flight(
    run_pane: Callable[..., None], client: str
) -> None:
    """The seedless render steps aside for the user's own send, not for any
    busy pane.  A server turn with no reply bubble yet (busy, nothing streaming) still lets a
    clear_ui refetch render."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('before rewind')]});
es.open();
es.deliver({type: 'state_change', state: 'running'}, {id: 4});
await flush();
es.deliver({type: 'clear_ui'}, {id: 5});
await flush();
await network.reply('/history', {messages: [user('after rewind')]});
expect(transcriptText().includes('after rewind') && !transcriptText().includes('before rewind'),
  'a server-busy pane with no reply streaming renders the refetch');
""",
    )


def test_interactive_dead_controller_contract(run_pane: Callable[..., None]) -> None:
    """Interactive only: the factory surface the shell drives when the stream gives up.  The
    controller reports ``isDead()``, fires ``onDead`` exactly once (the shell paints its
    reconnect banner), reads "Disconnected" in a dim status bar, keeps exposing its transport
    base, and ignores re-login and visibility: its recovery is the shell's revive, which may
    need a different node."""
    run_pane(
        "interactive",
        r"""
pinRandom(0);
const {createInteractivePane} = await import(SHARED + '/interactive.js');
const deadRoot = document.createElement('div');
html.appendChild(deadRoot);
let deadCalls = 0;
const deadCtl = createInteractivePane(deadRoot, WS, {nodeId: 'lab', onDead: () => {
  deadCalls += 1;
}});
expect(deadCtl.base === '/node/lab', 'the controller exposes its transport base', deadCtl.base);
deadCtl.connect();
await flush();
await network.reply('/history', {messages: []});
const es = FakeEventSource.latest();
es.open();
es.drop();
es.fail();
for (let i = 0; i < 400 && !deadCtl.isDead(); i += 1) {
  await clock.advance(500);
  const current = FakeEventSource.latest();
  if (current.readyState === EventSource.CONNECTING) current.fail();
}
expect(deadCtl.isDead(), 'refused redials leave the controller dead');
expect(deadCalls === 1, 'onDead fires once', String(deadCalls));
const tokens = deadRoot.querySelector('.ws-sb-tokens');
expect(tokens.textContent === 'Disconnected', 'the status bar reads Disconnected',
  tokens.textContent);
expect(deadRoot.querySelector('.ws-sb-disconnected') !== null, 'the status bar stays dim');
const count = sources().length;
deadCtl.onLogin();
visibility.hide();
visibility.show();
await clock.advance(120000);
expect(sources().length === count && deadCalls === 1,
  'a dead controller ignores re-login and visibility');
expect(visibility.listenerCount() === 0, 'a dead controller holds no visibility listener');
""",
    )


def test_coordinator_dead_controller_contract(run_pane: Callable[..., None]) -> None:
    """Coordinator only: the controller surface a host drives when the stream gives up, as the
    interactive pane's.  It reports ``isDead()``, fires ``onDead`` exactly once (the host paints
    its reconnect affordance), reads "Disconnected" in a dim status bar, and ignores re-login and
    visibility: its recovery is rebuilding the pane."""
    run_pane(
        "coordinator",
        r"""
pinRandom(0);
const {createCoordinatorPane} = await import(COORDINATOR);
const deadRoot = document.createElement('div');
html.appendChild(deadRoot);
let deadCalls = 0;
const deadCtl = createCoordinatorPane(deadRoot, WS, {onDead: () => { deadCalls += 1; }});
deadCtl.connect();
await flush();
await network.reply(WS_PATH, {ws_id: WS, name: 'coordinator', state: 'idle'});
await network.reply('/history', {messages: []});
const es = FakeEventSource.latest();
es.open();
es.drop();
es.fail();
for (let i = 0; i < 400 && !deadCtl.isDead(); i += 1) {
  await clock.advance(500);
  const current = FakeEventSource.latest();
  if (current.readyState === EventSource.CONNECTING) current.fail();
}
expect(deadCtl.isDead() && deadCtl.sync.dead, 'refused redials leave the controller dead');
expect(deadCalls === 1, 'onDead fires once', String(deadCalls));
const tokens = deadRoot.querySelector('.ws-sb-tokens');
expect(tokens.textContent === 'Disconnected', 'the status bar reads Disconnected',
  tokens.textContent);
expect(deadRoot.querySelector('.ws-sb-disconnected') !== null, 'the status bar stays dim');
const count = sources().length;
deadCtl.onLogin();
visibility.hide();
visibility.show();
await clock.advance(120000);
expect(sources().length === count && deadCalls === 1,
  'a dead controller ignores re-login and visibility');
expect(visibility.listenerCount() === 0, 'a dead controller holds no visibility listener');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_a_reasoning_bubble_counts_as_a_streaming_reply(
    run_pane: Callable[..., None], client: str
) -> None:
    """A reasoning-only segment (no content bubble yet) is as detachable as
    a content one, so a clear_ui refetch declines to render under it."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('before rewind')]});
es.open();
es.deliver({type: 'reasoning', text: 'thinking it over'}, {id: 4});
await flush();
es.deliver({type: 'clear_ui'}, {id: 5});
await flush();
await network.reply('/history', {messages: [user('after rewind')]});
expect(!transcriptText().includes('after rewind') && transcriptText().includes('thinking it over'),
  'the render is declined under a reasoning-only reply');
""",
    )


def test_interactive_events_reach_a_handle_event_installed_after_construction(
    run_pane: Callable[..., None],
) -> None:
    """Interactive only: the pane hands its sync core a dispatch that goes through
    ``handleEvent`` on every call, so a wrapper installed after construction (the browser
    harness wraps it) sees every event."""
    run_pane(
        "interactive",
        r"""
const {createInteractivePane} = await import(SHARED + '/interactive.js');
const ownRoot = document.createElement('div');
html.appendChild(ownRoot);
const ownCtl = createInteractivePane(ownRoot, WS, {nodeId: 'lab'});
const wrapped = [];
const handleEvent = ownCtl.pane.handleEvent.bind(ownCtl.pane);
ownCtl.pane.handleEvent = event => {
  wrapped.push(event.type);
  return handleEvent(event);
};
ownCtl.connect();
await flush();
await network.reply('/history', {messages: []});
const es = FakeEventSource.latest();
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 3});
await flush();
expect(wrapped.join(',') === 'state_change', 'the late wrapper saw the event', wrapped.join(','));
""",
    )


def test_coordinator_destroy_during_its_snapshot_opens_no_stream(
    run_pane: Callable[..., None],
) -> None:
    """Coordinator only: its first paint begins with a workstream snapshot GET
    the interactive pane does not make.  A destroy during that GET must not let
    the rest of first paint open a stream."""
    run_pane(
        "coordinator",
        r"""
await pane.start();
const snapshot = network.take(WS_PATH);
await pane.destroy();
snapshot.reply({ws_id: WS, name: 'coordinator', state: 'idle'});
await flush();
for (const history of network.pending('/history')) history.reply({messages: []});
await clock.advance(60000);
expect(sources().length === 0, 'no stream opens after a destroy during the snapshot request');
""",
    )


@pytest.mark.parametrize("failure", ["status-500", "network"])
@pytest.mark.parametrize("client", _CLIENTS)
def test_failed_clear_ui_refetch_keeps_the_transcript(
    run_pane: Callable[..., None], client: str, failure: str
) -> None:
    """A ``clear_ui`` whose /history fails leaves the transcript on screen:
    stale but real beats a blank pane.  The refetch is in place on the live
    stream, which it never closes or reopens."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('kept question'),
  {role: 'assistant', content: 'kept answer'}]});
es.open();
es.deliver({type: 'clear_ui'}, {id: 4});
await flush();
if (PARAMS.failure === 'network') await network.fail('/history');
else await network.reply('/history', {detail: 'boom'}, {status: 500});
const text = transcriptText();
expect(text.includes('kept question') && text.includes('kept answer'),
  'a failed refetch leaves the transcript on screen');
expect(!es.closeCalled && sources().length === 1, 'clear_ui never closes or reopens the stream');
""",
        params={"failure": failure},
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_handoff_token_is_presented_on_the_first_connect_only(
    run_pane: Callable[..., None], client: str
) -> None:
    """The /history handoff token rides the first connect only: a show-edge
    reconnect and a redial after a closed stream present the cursor alone."""
    run_pane(
        client,
        r"""
pinRandom(0);
const es = await firstPaint({messages: [], cursor: 4, handoff_token: 'tok-1'});
expect(stream(es).params.get('history_token') === 'tok-1', 'the first connect presents the token');
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 6});
visibility.hide();
visibility.show();
await flush();
const second = sources()[1];
expect(second && !stream(second).params.has('history_token'),
  'a show-edge reconnect does not present the token');
expect(stream(second).params.get('last_event_id') === '6', 'the reconnect presents the cursor');
second.open();
second.drop();
second.fail();
await clock.advance(40000);
const third = sources()[2];
expect(third && !stream(third).params.has('history_token'),
  'a redial after a closed stream does not present the token');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_first_paint_in_a_hidden_tab_defers_the_stream_to_the_show_edge(
    run_pane: Callable[..., None], client: str
) -> None:
    """A pane first painted in a background tab loads /history but opens no
    stream until the tab is shown; that connect still presents the cursor and
    the unused handoff token."""
    run_pane(
        client,
        r"""
visibility.hide();
await pane.start();
await pane.beforeHistory();
await network.reply('/history', {messages: [user('hidden paint')], cursor: 5,
  handoff_token: 'tok-1'});
expect(transcriptText().includes('hidden paint'), 'the history renders while hidden');
await clock.advance(30000);
expect(sources().length === 0, 'no stream opens into a hidden tab');
visibility.show();
await flush();
expect(sources().length === 1, 'show opens the stream');
const opened = stream(sources()[0]);
expect(opened.params.get('last_event_id') === '5' && opened.params.get('history_token') === 'tok-1',
  'the deferred connect presents the cursor and the token', opened.keys.join(','));
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_a_refused_stream_redials_after_the_first_backoff_step(
    run_pane: Callable[..., None], client: str
) -> None:
    """A stream the browser gave up on (CLOSED when its error arrives) is redialled after the
    first backoff step alone (1 s, plus 250 ms of jitter at the pinned random): the error
    decides, no later check."""
    run_pane(
        client,
        r"""
pinRandom(0.5);
const es = await firstPaint();
es.open();
es.drop();
es.fail();
await clock.advance(1249);
expect(sources().length === 1, 'no redial inside the first backoff step');
await clock.advance(1);
expect(sources().length === 2, 'one redial after the first backoff step', sources().length);
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_a_refused_stream_stops_at_the_seventh_refusal(
    run_pane: Callable[..., None], client: str
) -> None:
    """A stream refused on every redial stops at the seventh consecutive refusal: the first
    stream plus six redials, about a minute, then nothing."""
    run_pane(
        client,
        r"""
pinRandom(0);
const es = await firstPaint();
es.open();
es.drop();
es.fail();
for (let i = 0; i < 600; i += 1) {
  await clock.advance(500);
  const latest = FakeEventSource.latest();
  if (latest.readyState === EventSource.CONNECTING) latest.fail();
}
expect(sources().length === 7, 'a refused stream redials six times then stops',
  sources().length + ' streams');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_a_stream_the_server_resumed_after_refused_dials_reloads_and_goes_live(
    run_pane: Callable[..., None], client: str
) -> None:
    """The console restarts under an open pane: its redials are refused while it is down, then
    one is answered by a resumed session, with the frames the console's coordinator events
    route sends after a resume (tests/_resume_stream_contract.py, which the handler test holds
    the real route to). The pane reloads its history, goes live again on the reconnect, and
    never tells its host it is dead; the coordinator pane requests its workstream snapshot only
    on first paint."""
    run_pane(
        client,
        r"""
pinRandom(0);
let deadCalls = 0;
const host = document.createElement('div');
html.appendChild(host);
let ctl;
if (CLIENT === 'interactive') {
  const {createInteractivePane} = await import(SHARED + '/interactive.js');
  ctl = createInteractivePane(host, WS, {nodeId: 'lab', onDead: () => { deadCalls += 1; }});
} else {
  const {createCoordinatorPane} = await import(COORDINATOR);
  ctl = createCoordinatorPane(host, WS, {onDead: () => { deadCalls += 1; }});
}
ctl.connect();
await flush();
if (CLIENT === 'coordinator') {
  await network.reply(WS_PATH, {ws_id: WS, name: 'coordinator', state: 'idle'});
}
await network.reply('/history', {messages: [user('before the restart')], cursor: 10});
const first = FakeEventSource.latest();
first.open();
first.deliver({type: 'state_change', state: 'idle'}, {id: 11});
await flush();
// The console stops: the stream drops, and each redial is refused while it is down.
first.drop();
first.fail();
let refused = 1;
while (refused < 5) {
  await clock.advance(500);
  const current = FakeEventSource.latest();
  if (current.readyState === EventSource.CONNECTING) {
    current.fail();
    refused += 1;
  }
}
while (FakeEventSource.latest().readyState !== EventSource.CONNECTING) await clock.advance(500);
const resumed = FakeEventSource.latest();
expect(stream(resumed).params.get('last_event_id') === '11', 'the redial presents its cursor',
  stream(resumed).params.get('last_event_id'));
resumed.open();
for (const frame of PARAMS.frames) {
  resumed.deliver(frame.data, frame.id === undefined ? {} : {id: frame.id});
}
await clock.advance(0);
expect(network.pending('/history').length === 1, 'the envelope reloads the history');
await network.reply('/history', {messages: [user('before the restart'),
  user('while it was down')], cursor: 14, handoff_token: 'tok-2'});
const reconnected = FakeEventSource.latest();
expect(reconnected !== resumed && stream(reconnected).params.get('last_event_id') === '14',
  'the reload reconnects at its cursor', stream(reconnected).url);
reconnected.open();
await flush();
expect(textOf(host).includes('while it was down'), 'the reloaded history rendered');
expect(host.querySelector('.ws-sb-disconnected') === null, 'the status is live again',
  host.querySelector('.ws-sb-tokens').textContent);
expect(!ctl.isDead() && deadCalls === 0, 'the host is never told the pane is dead',
  String(deadCalls));
if (CLIENT === 'coordinator') {
  expect(network.seen(WS_PATH).length === 1, 'no workstream snapshot after the first',
    String(network.seen(WS_PATH).length));
}
""",
        params={
            "frames": resumed_stream_frames("ws/2", lost_count=0, earliest_available_id=12),
        },
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_relogin_leaves_a_healthy_stream_alone(run_pane: Callable[..., None], client: str) -> None:
    """The login fan-out resumes a stream that needs it and leaves a live one
    alone: no close, no new stream, no /history reload."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('kept question')]});
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 3});
const historyLoads = network.seen('/history').length;
await pane.login();
await clock.advance(1000);
expect(!es.closeCalled && sources().length === 1, 'a re-login leaves a healthy stream alone');
expect(network.seen('/history').length === historyLoads, 'a re-login does not reload /history');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_dead_mid_reasoning_removes_the_reasoning_indicator(
    run_pane: Callable[..., None], client: str
) -> None:
    """Dead ends the reasoning clock with the turn's other timers, so a
    reasoning indicator showing when the stream gives up is removed, not left ticking."""
    run_pane(
        client,
        r"""
pinRandom(0);
const es = await firstPaint({messages: [user('think about it')]});
es.open();
es.deliver({type: 'state_change', state: 'thinking'}, {id: 4});
es.deliver({type: 'reasoning', text: 'thinking it over'}, {id: 5});
await flush();
expect(root.querySelector('.reasoning-activity-status') !== null,
  'the reasoning indicator shows while the model reasons');
es.drop();
es.fail();
const tokens = root.querySelector('.ws-sb-tokens');
for (let i = 0; i < 400 && tokens.textContent !== 'Disconnected'; i += 1) {
  await clock.advance(500);
  const current = FakeEventSource.latest();
  if (current.readyState === EventSource.CONNECTING) current.fail();
}
expect(tokens.textContent === 'Disconnected', 'the stream is dead', tokens.textContent);
expect(root.querySelector('.reasoning-activity-status') === null,
  'dead removes the reasoning indicator');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_retry_with_a_tool_batch_starting_before_history_answers(
    run_pane: Callable[..., None], client: str
) -> None:
    """An ordinary Retry: the click posts /retry, the server truncates, publishes clear_ui and
    re-dispatches the turn, whose tool batch starts before /history answers, trimmed, with a
    cursor.  The refetch is promoted to a seeded render: the trimmed history renders (the
    replaced reply goes), the stream is rebuilt presenting the cursor, and its replay shows the
    regenerated batch exactly once."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('list the files'),
  {role: 'assistant', content: 'the replaced reply'}]});
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 4});
await flush();
const retry = pane.transcript().querySelector('.msg-retry-btn');
expect(retry, 'the last reply offers Retry');
press(retry);
await flush();
expect(network.pending('/retry').length === 1, 'Retry posts /retry');
await network.reply('/retry', {status: 'ok'});
es.deliver({type: 'clear_ui'}, {id: 5});
await flush();
const batch = [[6, {type: 'state_change', state: 'running'}],
  [7, {type: 'tool_info', items: [{call_id: 'c1', func_name: 'bash', header: 'bash',
    preview: 'ls -la'}]}]];
for (const [id, event] of batch) es.deliver(event, {id});
await flush();
await network.reply('/history', {messages: [user('list the files')], cursor: 5});
expect(!transcriptText().includes('the replaced reply'), 'the replaced reply is gone');
const next = FakeEventSource.latest();
expect(next !== es && stream(next).params.get('last_event_id') === '5',
  'the stream is rebuilt presenting the cursor', next.url);
next.open();
for (const [id, event] of batch) next.deliver(event, {id});
await flush();
const count = pane.transcript().querySelectorAll('.conv-batch').length;
expect(count === 1, 'the regenerated batch shows once', String(count));
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_rewind_end_to_end(run_pane: Callable[..., None], client: str) -> None:
    """Rewind from a user message: the click posts /rewind with the turn count, the server's
    clear_ui refetches /history, and the rewound transcript replaces the old one."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('first question'),
  {role: 'assistant', content: 'first answer'}, user('second question'),
  {role: 'assistant', content: 'second answer'}]});
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 4});
await flush();
const second = pane.transcript().querySelectorAll('.msg.user')[1];
const rewind = second.querySelectorAll('.msg-action-btn')
  .find(button => button.querySelector('.icon-rewind'));
expect(rewind, 'the user message offers Rewind');
press(rewind);
await flush();
const post = network.pending('/rewind')[0];
expect(post && JSON.parse(post.init.body).turns === 1, 'Rewind posts the turn count',
  post && post.init.body);
await network.reply('/rewind', {status: 'ok'});
es.deliver({type: 'clear_ui'}, {id: 5});
await flush();
await network.reply('/history', {messages: [user('first question'),
  {role: 'assistant', content: 'first answer'}]});
expect(transcriptText().includes('first answer') && !transcriptText().includes('second'),
  'the rewound transcript renders');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_edit_and_resend_end_to_end(run_pane: Callable[..., None], client: str) -> None:
    """Edit and resend: the edit posts /rewind, the server's clear_ui refetches /history, and
    once that refetch settles the edited text is sent (one /send carrying it) and shows on the
    rewound transcript."""
    run_pane(
        client,
        r"""
FakeElement.prototype.setSelectionRange = () => {};
const es = await firstPaint({messages: [user('original question'),
  {role: 'assistant', content: 'original answer'}]});
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 4});
await flush();
const message = pane.transcript().querySelectorAll('.msg.user')[0];
const edit = message.querySelectorAll('.msg-action-btn')
  .find(button => button.querySelector('.icon-edit'));
expect(edit, 'the user message offers Edit');
press(edit);
await flush();
message.querySelector('.msg-edit-textarea').value = 'edited question';
press(message.querySelector('.msg-edit-btn-send'));
await flush();
const post = network.pending('/rewind')[0];
expect(post && JSON.parse(post.init.body).turns === 1, 'the edit posts /rewind',
  post && post.init.body);
await network.reply('/rewind', {status: 'ok'});
es.deliver({type: 'clear_ui'}, {id: 5});
await flush();
expect(network.seen('/send').length === 0, 'nothing is sent before the refetch settles');
await network.reply('/history', {messages: []});
const sends = network.seen('/send');
expect(sends.length === 1 && JSON.parse(sends[0].init.body).message === 'edited question',
  'the edited text is sent once the refetch settles', sends.map(r => r.init.body).join(' | '));
expect(transcriptText().includes('edited question') && !transcriptText().includes('original'),
  'the edit shows on the rewound transcript');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_nothing_from_the_earlier_stream_paints_after_teardown(
    run_pane: Callable[..., None], client: str
) -> None:
    """The shell closes a pane client-side when a different user signs in; after that teardown
    nothing the earlier stream still carries paints: not a reply, not a clear_ui, and not the
    /history it had in flight."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('the earlier question')]});
es.open();
es.deliver({type: 'clear_ui'}, {id: 4});
await flush();
const transcript = pane.transcript();
const before = textOf(transcript);
await pane.destroy();
es.deliver({type: 'content', text: 'a late reply'}, {id: 5});
es.deliver({type: 'clear_ui'}, {id: 6});
await flush();
for (const request of network.pending('/history')) {
  request.reply({messages: [user('a late history')]});
}
await flush();
await clock.advance(60000);
expect(textOf(transcript) === before, 'nothing paints after teardown', textOf(transcript));
expect(sources().length === 1, 'no stream reopens');
""",
    )


def test_coordinator_missed_reloads_the_sidebar_and_a_covered_gap_does_not(
    run_pane: Callable[..., None],
) -> None:
    """Coordinator only: the sidebar (children, tasks, live badges) is re-read on the core's
    ``missed`` event, a reconnect whose replay cannot vouch for the gap (no cursor to present).
    A gap the cursor covers, however long, reloads nothing: the ring replay redelivers the
    child and task events itself."""
    run_pane(
        "coordinator",
        r"""
const sidebar = () => network.seen('/children').length + network.seen('/tasks').length;
const es = await firstPaint();
es.open();
await flush();
const afterFirstPaint = sidebar();
expect(afterFirstPaint === 2, 'first paint loads children and tasks once', String(afterFirstPaint));
es.drop();
await clock.advance(1000);
es.open();
await flush();
expect(sidebar() === afterFirstPaint + 2, 'a reconnect with no cursor reloads the sidebar',
  String(sidebar()));
es.deliver({type: 'status'}, {id: 7});
await flush();
const covered = sidebar();
es.drop();
await clock.advance(120000);
es.open();
await flush();
expect(sidebar() === covered, 'a gap the cursor covers reloads nothing', String(sidebar()));
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_relogin_redials_a_refused_stream_at_once(
    run_pane: Callable[..., None], client: str
) -> None:
    """The login fan-out (and the coordinator host's explicit re-open of a live pane, which calls
    the same ``onLogin``) redials a refused stream at once instead of waiting out its backoff."""
    run_pane(
        client,
        r"""
pinRandom(0);
const es = await firstPaint();
es.open();
await flush();
const count = sources().length;
es.drop();
es.fail();
expect(sources().length === count, 'a refused stream waits for its backoff');
await pane.login();
expect(sources().length === count + 1, 're-login redials at once', String(sources().length));
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_the_status_bar_shows_the_phase_and_is_dim_until_live(
    run_pane: Callable[..., None], client: str
) -> None:
    """Both panes paint the stream's phase the same way: dim with the phase's words while the
    stream is not live; live lifts the dim and shows the counters, or the placeholder when no
    status event ever arrived."""
    run_pane(
        client,
        r"""
const es = await firstPaint();
const tokens = root.querySelector('.ws-sb-tokens');
const dim = () => root.querySelector('.ws-sb-disconnected') !== null;
expect(dim() && tokens.textContent === 'Connecting' + String.fromCharCode(0x2026),
  'connecting is dim and says so', tokens.textContent);
es.open();
await flush();
expect(!dim() && tokens.textContent === '0 / ' + String.fromCharCode(0x2014),
  'live lifts the dim and shows the placeholder', tokens.textContent);
es.drop();
await flush();
expect(dim() && tokens.textContent === 'Reconnecting' + String.fromCharCode(0x2026),
  'a drop dims and says reconnecting', tokens.textContent);
es.open();
await flush();
expect(!dim(), 'the reopen lifts the dim again');
""",
    )


def test_coordinator_a_failed_snapshot_is_dead_to_its_host(run_pane: Callable[..., None]) -> None:
    """Coordinator only: a first paint whose workstream snapshot GET fails has nothing to stream,
    so the pane reports dead the way a stream that gave up does (the host's reconnect rebuilds
    it and runs the GET again): ``isDead()``, ``onDead`` once, "Disconnected", and no /history
    or stream.  A pane closed during the GET reports nothing."""
    run_pane(
        "coordinator",
        r"""
const {createCoordinatorPane} = await import(COORDINATOR);
const mount = (onDead) => {
  const host = document.createElement('div');
  html.appendChild(host);
  return {host, ctl: createCoordinatorPane(host, WS, {onDead})};
};
let deadCalls = 0;
const failed = mount(() => { deadCalls += 1; });
failed.ctl.connect();
await flush();
await network.reply(WS_PATH, {detail: 'unavailable'}, {status: 503});
await flush();
expect(failed.ctl.isDead() && deadCalls === 1, 'a failed snapshot is dead, announced once',
  deadCalls);
expect(failed.host.querySelector('.ws-sb-tokens').textContent === 'Disconnected',
  'the status bar says so');
expect(network.seen('/history').length === 0 && sources().length === 0,
  'nothing loads or streams');
let closedCalls = 0;
const closed = mount(() => { closedCalls += 1; });
closed.ctl.connect();
await flush();
closed.ctl.destroy();
await network.reply(WS_PATH, {detail: 'unavailable'}, {status: 503});
await flush();
expect(closedCalls === 0, 'a pane closed during the GET reports nothing', closedCalls);
""",
    )


def test_coordinator_a_rebuilt_pane_on_the_same_root_answers_a_key_once(
    run_pane: Callable[..., None],
) -> None:
    """Coordinator only: a host rebuilds a dead pane into the same root, so the destroyed pane's
    root listeners must go with it; one approval key press then sends one decision."""
    run_pane(
        "coordinator",
        r"""
const {createCoordinatorPane} = await import(COORDINATOR);
const host = document.createElement('div');
html.appendChild(host);
const pending = cycle => ({type: 'approve_request', cycle_id: cycle,
  items: [{call_id: 'c-' + cycle, func_name: 'bash', needs_approval: true, header: 'bash',
    preview: 'ls'}]});
const build = async () => {
  const ctl = createCoordinatorPane(host, WS, {});
  ctl.connect();
  await flush();
  await network.reply(WS_PATH, {ws_id: WS, name: 'coordinator', state: 'idle'});
  await network.reply('/history', {messages: []});
  return ctl;
};
const first = await build();
FakeEventSource.latest().open();
FakeEventSource.latest().deliver(pending('one'), {id: 4});
await flush();
first.destroy();
host.replaceChildren();
await build();
FakeEventSource.latest().open();
FakeEventSource.latest().deliver(pending('two'), {id: 5});
await flush();
host.dispatchEvent({type: 'keydown', key: 'd', bubbles: false, target: host,
  preventDefault() {}});
await flush();
const posts = network.seen('/approve');
expect(posts.length === 1, 'one key press sends one decision', String(posts.length));
expect(JSON.parse(posts[0].init.body).cycle_id === 'two', 'from the live pane',
  posts[0].init.body);
""",
    )


def test_coordinator_a_hidden_first_paint_reloads_the_sidebar_when_the_stream_opens(
    run_pane: Callable[..., None],
) -> None:
    """Coordinator only: a first paint in a hidden tab defers the stream to the show edge, so the
    sidebar read at first paint may have moved on by the time the stream opens; that late first
    open is ``missed`` and the sidebar is read again."""
    run_pane(
        "coordinator",
        r"""
const sidebar = () => network.seen('/children').length + network.seen('/tasks').length;
visibility.hide();
await pane.start();
await pane.beforeHistory();
await network.reply('/history', {messages: []});
expect(sidebar() === 2 && sources().length === 0, 'first paint reads the sidebar, no stream yet',
  String(sidebar()));
visibility.show();
await flush();
FakeEventSource.latest().open();
await flush();
expect(sidebar() === 4, 'the late first open reads it again', String(sidebar()));
""",
    )


def test_coordinator_first_paint_replays_a_pending_approval_below_the_history(
    run_pane: Callable[..., None],
) -> None:
    """Coordinator only: the snapshot's pending approval is replayed after the history render
    (below it, not wiped by it) and before the stream connects, so the live approve_request for
    the same cycle upgrades that batch in place instead of adding a second one."""
    run_pane(
        "coordinator",
        r"""
await pane.start();
const item = {call_id: 'c1', func_name: 'bash', needs_approval: true, header: 'bash',
  preview: 'ls'};
await network.reply(WS_PATH, {ws_id: WS, name: 'coordinator', state: 'running',
  pending_approval: true,
  pending_approval_details: [{items: [item], cycle_id: 'cy1', judge_pending: false}]});
expect(sources().length === 0, 'no stream before the history renders');
await network.reply('/history', {messages: [user('run it')]});
const transcript = pane.transcript();
const rows = [...transcript.children];
const userRow = rows.findIndex(row => row.classList.contains('user'));
const batchRow = rows.findIndex(row => row.classList.contains('conv-batch'));
expect(userRow >= 0 && batchRow > userRow, 'the replayed batch sits below the history',
  userRow + ' ' + batchRow);
const es = FakeEventSource.latest();
es.open();
es.deliver({type: 'approve_request', cycle_id: 'cy1', items: [item]}, {id: 4});
await flush();
expect(transcript.querySelectorAll('.conv-batch').length === 1,
  'the live approve_request upgrades the replayed batch in place');
""",
    )


@pytest.mark.parametrize("client", _CLIENTS)
def test_an_edit_composed_before_a_sign_in_change_is_not_sent(
    run_pane: Callable[..., None], client: str
) -> None:
    """The latched edit-and-resend belongs to the identity that composed it: when the sign-in
    changes before the rewind's refetch settles, the rewind stands but the edit is not sent as
    whoever is signed in now; the text goes back to the composer with a one-line note."""
    run_pane(
        client,
        r"""
FakeElement.prototype.setSelectionRange = () => {};
sessionStorage.setItem = () => {};
sessionStorage.removeItem = () => {};
const auth = await import(SHARED + '/auth.js');
const es = await firstPaint({messages: [user('original question'),
  {role: 'assistant', content: 'original answer'}]});
es.open();
es.deliver({type: 'state_change', state: 'idle'}, {id: 4});
await flush();
const message = pane.transcript().querySelectorAll('.msg.user')[0];
press(message.querySelectorAll('.msg-action-btn').find(b => b.querySelector('.icon-edit')));
await flush();
message.querySelector('.msg-edit-textarea').value = 'edited question';
press(message.querySelector('.msg-edit-btn-send'));
await flush();
await network.reply('/rewind', {status: 'ok'});
const before = auth.authGeneration();
auth.showLogin();
expect(auth.authGeneration() !== before, 'the sign-in changed');
es.deliver({type: 'clear_ui'}, {id: 5});
await flush();
for (const request of network.pending('/history')) request.reply({messages: []});
await flush();
expect(network.seen('/send').length === 0, 'the edit is not sent as the new identity');
expect(root.querySelector('.composer-input').value === 'edited question',
  'the text is back in the composer', root.querySelector('.composer-input').value);
expect(transcriptText().includes('Edit not sent: the signed-in user changed.'), 'the pane says so');
""",
    )
