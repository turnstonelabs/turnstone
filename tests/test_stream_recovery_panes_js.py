"""Workstream stream and recovery behavior, executed through each pane's public surface.

Both panes (``shared_static/interactive.js`` and the console coordinator) run
whole under node with the browser fakes from tests/_js_harness_helpers.py.  A
test may touch only what survives the planned replacement of both stream layers
by one shared module: the pane's construction and its public controller
(interactive: ``createInteractivePane`` and its ``connect`` / ``onLogin`` /
``destroy``; coordinator: ``createCoordinatorPane`` and the same three), the
EventSource fake (the frames it delivers, its errors, the URLs it was opened
with), the fetch queue, the virtual clock, tab visibility and the rendered DOM.
No test reads or calls a private field or method of either pane.

Every test runs against both clients.  Where a client does not yet have the
behavior the shared module will give both, its case is a strict xfail whose
reason names the behavior and the client that lacks it, so it flips loudly when
the rewrite lands.  A strict xfail only counts when the pane fails the one
expectation named for that gap, matched by its whole label: any other failed
expectation, a harness error, or a throw inside a pane callback fails the test
outright.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests._js_harness_helpers import (
    FAKE_CLOCK,
    FAKE_DOM,
    FAKE_EVENT_SOURCE,
    FAKE_FETCH,
    FAKE_VISIBILITY,
    PANE_DOM,
    REMOVE_CHILD,
    coordinator_module_for_node,
    node_skip,
    run_node_source,
)

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


@dataclass(frozen=True)
class Gap:
    """A behavior a client does not have yet: the xfail reason (the behavior and
    the client lacking it), and the whole label of the one expectation that must
    be the failing one."""

    reason: str
    expectation: str


def _clients(gaps: dict[str, Gap]) -> list[Any]:
    return [
        pytest.param(
            client,
            marks=pytest.mark.xfail(
                strict=True, raises=PaneExpectationError, reason=gaps[client].reason
            ),
        )
        if client in gaps
        else client
        for client in _CLIENTS
    ]


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
"""

# Outside the body's try block: the verdict needs both after the body threw.
# ``expect(condition, label, detail)``: the label is fixed text, unique within a
# test, and is what a gap matches; the optional detail carries the values seen.
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


def _verdict(proc: Any, gap: Gap | None) -> None:
    if proc.returncode == 0:
        return
    output = f"{proc.stderr}\n{proc.stdout}"
    match = re.search(r"^PANE-EXPECTATION-FAILED: (.*)$", proc.stderr, re.M)
    if match is None:
        raise PaneHarnessError(f"pane script failed outside an expectation:\n{output}")
    label = match.group(1)
    if gap is not None and label != gap.expectation:
        raise PaneHarnessError(
            f"expected the gap ({gap.reason}) at '{gap.expectation}', "
            f"but the pane failed at: {label}\n{output}"
        )
    raise PaneExpectationError(f"{label}\n{output}")


@pytest.fixture
def run_pane(tmp_path: Path) -> Callable[..., None]:
    """Run a test body against one client.  The coordinator module is served
    from a copy whose ``/shared/`` imports point at the shared directory on disk
    (the only rewrite; no internals are spliced in)."""
    coordinator = coordinator_module_for_node(tmp_path)

    def run(
        client: str,
        body: str,
        *,
        gaps: dict[str, Gap] | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        gap = (gaps or {}).get(client)
        if gap is not None and body.count("'" + gap.expectation + "'") != 1:
            raise PaneHarnessError(
                f"the gap label {gap.expectation!r} must appear exactly once in the test body"
            )
        proc = run_node_source(_script(client, body, params or {}, coordinator), timeout=60)
        _verdict(proc, gap)

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


_NO_WORKSTREAM_PROBE = {
    "coordinator": Gap(
        "coordinator does not yet stop requesting the workstream on a stream error "
        "(that GET rehydrates a closed session)",
        "a stream error does not request the workstream resource",
    )
}


@pytest.mark.parametrize("client", _clients(_NO_WORKSTREAM_PROBE))
def test_stream_error_does_not_request_the_workstream(
    run_pane: Callable[..., None], client: str
) -> None:
    """A stream error never requests the workstream resource: for a writer that
    GET rehydrates an unloaded workstream, and the stream layer must not open,
    reopen or rehydrate one."""
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
        gaps=_NO_WORKSTREAM_PROBE,
    )


_DEGRADED_COOLDOWN = {
    "interactive": Gap(
        "interactive does not yet keep the stream closed for the whole overflow cooldown "
        "(its 5 s closed-stream beat reopens it)",
        "no new stream before the cooldown elapses",
    )
}


@pytest.mark.parametrize("client", _clients(_DEGRADED_COOLDOWN))
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
        gaps=_DEGRADED_COOLDOWN,
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


_RESYNC_LOAD_WINDOW = {
    "interactive": Gap(
        "interactive does not yet keep the stream down while a resync loads /history "
        "(its 5 s closed-stream beat reopens it)",
        "no stream opens while the resync loads",
    )
}


@pytest.mark.parametrize("client", _clients(_RESYNC_LOAD_WINDOW))
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
        gaps=_RESYNC_LOAD_WINDOW,
    )


_HELD_DURING_REFETCH = {
    "coordinator": Gap(
        "coordinator does not yet hold content events during a refetch",
        "content during the refetch is held, not painted live",
    )
}


@pytest.mark.parametrize("client", _clients(_HELD_DURING_REFETCH))
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
        gaps=_HELD_DURING_REFETCH,
    )


_RECONNECT_GENERATION = {
    "coordinator": Gap(
        "coordinator does not yet decline a refetch render across a reconnect (#904)",
        "a render across a reconnect is declined",
    )
}


@pytest.mark.parametrize("client", _clients(_RECONNECT_GENERATION))
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
        gaps=_RECONNECT_GENERATION,
    )


_SEND_INSIDE_REFETCH = {
    "interactive": Gap(
        "interactive does not yet decline a refetch render while the user's send is in flight",
        "a send inside the refetch keeps its row: the render is declined",
    )
}


@pytest.mark.parametrize("client", _clients(_SEND_INSIDE_REFETCH))
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
        gaps=_SEND_INSIDE_REFETCH,
    )


_TRIMMED_SEEDLESS = {
    client: Gap(
        f"{client} does not yet decline a refetch render when /history returns a cursor",
        "a seedless render of a trimmed /history is declined",
    )
    for client in _CLIENTS
}


@pytest.mark.parametrize("client", _clients(_TRIMMED_SEEDLESS))
def test_seedless_refetch_of_a_trimmed_history_is_declined(
    run_pane: Callable[..., None], client: str
) -> None:
    """/history returns a cursor when it trimmed a running turn that only a
    seeded load can restore, so a ``clear_ui`` refetch does not render it, and
    the transcript already on screen stays (a wipe is not a decline)."""
    run_pane(
        client,
        r"""
const es = await firstPaint({messages: [user('before rewind')]});
es.open();
es.deliver({type: 'clear_ui'}, {id: 4});
await flush();
await network.reply('/history', {messages: [user('after rewind')], cursor: 3});
expect(!transcriptText().includes('after rewind'),
  'a seedless render of a trimmed /history is declined');
expect(transcriptText().includes('before rewind'),
  'the transcript stays on screen when the trimmed render is declined');
""",
        gaps=_TRIMMED_SEEDLESS,
    )


_DESTROY_DURING_FIRST_PAINT = {
    "coordinator": Gap(
        "coordinator does not yet stop first paint from opening a stream after destroy",
        "no stream opens after destroy",
    )
}


@pytest.mark.parametrize("client", _clients(_DESTROY_DURING_FIRST_PAINT))
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
        gaps=_DESTROY_DURING_FIRST_PAINT,
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


_DESTROY_DURING_SNAPSHOT = Gap(
    "coordinator does not yet stop first paint from opening a stream after a destroy "
    "during its workstream snapshot request",
    "no stream opens after a destroy during the snapshot request",
)


@pytest.mark.xfail(strict=True, raises=PaneExpectationError, reason=_DESTROY_DURING_SNAPSHOT.reason)
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
        gaps={"coordinator": _DESTROY_DURING_SNAPSHOT},
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


_CLOSED_STREAM_BACKOFF = {
    "interactive": Gap(
        "interactive does not yet back off before redialling a closed stream "
        "(it redials at the 5 s check)",
        "no redial before the check plus the first backoff step",
    )
}


@pytest.mark.parametrize("client", _clients(_CLOSED_STREAM_BACKOFF))
def test_closed_stream_redials_after_the_check_and_backoff(
    run_pane: Callable[..., None], client: str
) -> None:
    """A stream the browser gave up on (CLOSED) is redialled once the 5 s check
    finds it closed and the first backoff step (1 s plus up to 500 ms) passes."""
    run_pane(
        client,
        r"""
pinRandom(0.5);
const es = await firstPaint();
es.open();
es.drop();
es.fail();
await clock.advance(5999);
expect(sources().length === 1, 'no redial before the check plus the first backoff step');
await clock.advance(501);
expect(sources().length === 2, 'one redial after the check and backoff', sources().length);
""",
        gaps=_CLOSED_STREAM_BACKOFF,
    )


_STRIKE_BUDGET = {
    "interactive": Gap(
        "interactive does not yet allow six strikes before giving up on a refused stream "
        "(it stops after three)",
        "a refused stream redials five times then stops",
    ),
    "coordinator": Gap(
        "coordinator does not yet stop redialling a refused stream (it never gives up)",
        "a refused stream redials five times then stops",
    ),
}


@pytest.mark.parametrize("client", _clients(_STRIKE_BUDGET))
def test_closed_stream_stops_after_six_strikes(run_pane: Callable[..., None], client: str) -> None:
    """A stream refused on every redial stops after the sixth consecutive closed
    check: the first stream plus five redials, about a minute, then nothing."""
    run_pane(
        client,
        r"""
pinRandom(0);
const es = await firstPaint();
es.open();
es.drop();
es.fail();
for (let i = 0; i < 40; i += 1) {
  await clock.advance(15000);
  const latest = FakeEventSource.latest();
  if (latest.readyState === EventSource.CONNECTING) latest.fail();
}
expect(sources().length === 6, 'a refused stream redials five times then stops',
  sources().length + ' streams');
""",
        gaps=_STRIKE_BUDGET,
    )


_RELOGIN_HEALTHY = {
    "interactive": Gap(
        "interactive does not yet leave a healthy stream alone on re-login (it reloads /history)",
        "a re-login leaves a healthy stream alone",
    ),
    "coordinator": Gap(
        "coordinator does not yet leave a healthy stream alone on re-login (it redials)",
        "a re-login leaves a healthy stream alone",
    ),
}


@pytest.mark.parametrize("client", _clients(_RELOGIN_HEALTHY))
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
        gaps=_RELOGIN_HEALTHY,
    )
