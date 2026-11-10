"""WorkstreamSync (``shared_static/workstream_sync.js``), executed under node.

The one stream and recovery layer both workstream panes sit on.  Every test drives a real
instance through its public surface only: the constructor, ``start`` / ``retryNow`` /
``destroy``, the events it emits, ``inspect()`` and its getters, and the browser fakes from
tests/_js_harness_helpers.py (EventSource, fetch, the virtual clock, tab visibility).  No test
reads a private field.  The view is a small recorder defined here that keeps the view contract:
it logs every call in order and derives its live-state flags from the events it is handed, as a
pane's would.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests._js_harness_helpers import (
    FAKE_CLOCK,
    FAKE_DOM,
    FAKE_EVENT_SOURCE,
    FAKE_FETCH,
    FAKE_VISIBILITY,
    extract_braced,
    node_skip,
    run_node_source,
    strip_js_comments,
)

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1]
_MODULE = _ROOT / "turnstone/shared_static/workstream_sync.js"
_INTERACTIVE = _ROOT / "turnstone/shared_static/interactive.js"
_COORDINATOR = _ROOT / "turnstone/console/static/coordinator/coordinator.js"

# The page around the core: a login overlay the auth tests show and hide (auth.js shows it
# with display "flex" and hides it with "none").
_DOCUMENT = r"""
const loginOverlay = {style: {display: 'none'}};
document.getElementById = id => (id === 'login-overlay' ? loginOverlay : null);
"""

_DRIVER = r"""
const {WorkstreamSync, buildEventsUrl, redialDelayMs, seedlessRenderAllowed, STREAM_STATUS_TEXT} =
  await import(MODULE);
const WS = 'ws/2';
const BASE = '/node/lab';
const WS_PATH = BASE + '/v1/api/workstreams/ws%2F2';
const ELLIPSIS = String.fromCharCode(0x2026);
const DASH = String.fromCharCode(0x2014);
const expect = (condition, label, detail) => {
  if (condition) return;
  throw new Error('EXPECTATION FAILED: ' + label + (detail === undefined ? '' : ' [' + detail + ']'));
};
const transcript = document.createElement('div');
html.appendChild(transcript);
// Every console warning and error, as one string each (still forwarded).
const logged = [];
for (const level of ['warn', 'error']) {
  const forward = console[level].bind(console);
  console[level] = (...args) => {
    logged.push(args.map(arg => (arg instanceof Error ? arg.message : String(arg))).join(' '));
    forward(...args);
  };
}
// A reply streams from its first content or reasoning delta until stream_end or a settle; the
// pane is busy from a non-idle state_change until the settle.
const view = {
  calls: [],
  dispatched: [],
  rendered: [],
  prompts: [],
  live: {streaming: false, busy: false, sending: false},
  dispatch(event) {
    view.calls.push('dispatch');
    view.dispatched.push(event);
    if (event.type === 'content' || event.type === 'reasoning') view.live.streaming = true;
    if (event.type === 'stream_end') view.live.streaming = false;
    if (event.type === 'state_change') {
      const settled = event.state === 'idle' || event.state === 'error';
      view.live.busy = !settled;
      if (settled) {
        view.live.streaming = false;
        view.live.sending = false;
      }
    }
  },
  renderHistory(messages) {
    view.calls.push('renderHistory');
    view.rendered.push(messages);
  },
  resetLiveState() {
    view.calls.push('resetLiveState');
    view.live.streaming = false;
  },
  liveState() {
    return {...view.live};
  },
  placeRepairPrompt(prompt) {
    view.calls.push('placeRepairPrompt');
    view.prompts.push(prompt);
    transcript.appendChild(prompt);
  },
};
const newSync = (options = {}) =>
  new WorkstreamSync({wsId: WS, base: BASE, fetch: (url, init) => fetch(url, init), view, ...options});
const sync = newSync();
const statuses = [];
const missed = [];
const reloaded = [];
const settled = [];
sync.on('status', status => statuses.push(status));
sync.on('missed', () => missed.push(clock.elapsed));
sync.on('reloaded', payload => reloaded.push(payload));
sync.on('reset-settled', payload => settled.push(payload));
const sources = () => FakeEventSource.instances;
const latest = () => FakeEventSource.latest();
const params = source => new URL(source.url).searchParams;
const cursorOf = source => params(source).get('last_event_id');
const historyCount = () => network.seen('/history').length;
const renders = () => view.rendered.length;
const dispatchedTypes = () => view.dispatched.map(event => event.type).join(',');
const outcomes = () => settled.map(payload => payload.outcome).join(',');
const user = content => ({role: 'user', content});
const send = async (source, data, id) => {
  source.deliver(data, id === undefined ? {} : {id});
  await flush();
};
const firstPaint = async (history = {messages: []}) => {
  sync.start();
  await flush();
  await network.reply('/history', history);
  return latest();
};
const live = async history => {
  const es = await firstPaint(history);
  es.open();
  await flush();
  return es;
};
// Refuse every redial the core makes, stepping the clock, until `until()` holds; returns the
// times (from the call) at which redials appeared.
const refuseEveryRedial = async ({until = () => sync.status === 'dead', step = 100} = {}) => {
  const start = clock.elapsed;
  const times = [];
  let seen = sources().length;
  for (let i = 0; i < 5000 && !until(); i += 1) {
    await clock.advance(step);
    if (sources().length > seen) {
      seen = sources().length;
      times.push(clock.elapsed - start);
      latest().fail();
      await flush();
    }
  }
  return times;
};
// A clear_ui refetch and its one retry both fail, so the staleness latch stays set.
const latchAfterTwoFailures = async es => {
  await send(es, {type: 'clear_ui'}, 4);
  await network.reply('/history', {detail: 'unavailable'}, {status: 500});
  await clock.advance(2500);
  await network.reply('/history', {detail: 'unavailable'}, {status: 500});
  expect(sync.historyStale, 'the latch survives both failures');
};
// Nothing the instance owns is left behind.
const expectReleased = label => {
  expect(clock.pending().length === 0, label + ': no timer left armed', JSON.stringify(clock.pending()));
  expect(visibility.listenerCount() === 0, label + ': no visibility listener');
  expect(network.pending().length === 0, label + ': no request left pending');
  expect(sync.inspect().source === null, label + ': no stream held');
};
"""

_CODA = r"""
if (reportedErrors.length) {
  for (const {where, err} of reportedErrors) console.error('uncaught in ' + where, err);
  throw new Error(reportedErrors.length + ' uncaught error(s) in callbacks');
}
"""


def _script(body: str) -> str:
    return (
        FAKE_DOM
        + _DOCUMENT
        + FAKE_VISIBILITY
        + FAKE_CLOCK
        + FAKE_FETCH
        + FAKE_EVENT_SOURCE
        + f"const MODULE = {json.dumps(_MODULE.as_uri())};\n"
        + _DRIVER
        + body
        + _CODA
    )


def _run(body: str) -> None:
    proc = run_node_source(_script(body), timeout=60)
    assert proc.returncode == 0, f"core script failed:\n{proc.stderr}\n{proc.stdout}"


# --- module surface --------------------------------------------------------------------------


def test_module_loads_in_bare_node_without_touching_a_document() -> None:
    """Importing and constructing touch no DOM: the module links (every named import resolves)
    in a bare node with no document."""
    proc = run_node_source(
        f"const MODULE = {json.dumps(_MODULE.as_uri())};\n"
        + r"""
const {WorkstreamSync} = await import(MODULE);
if (typeof globalThis.document !== 'undefined') throw new Error('this run must have no document');
const view = {dispatch() {}, renderHistory() {}, resetLiveState() {}, placeRepairPrompt() {},
  liveState: () => ({streaming: false, busy: false, sending: false})};
const sync = new WorkstreamSync({wsId: 'w', fetch: () => null, view});
if (sync.status !== 'idle') throw new Error('a new instance is idle, got ' + sync.status);
"""
    )
    assert proc.returncode == 0, proc.stderr


def test_module_imports_only_named_sibling_modules() -> None:
    """Sibling ``./x.js`` modules through named imports only, and never auth.js (the standalone
    coordinator page script-tags it, so a module import would make a second instance)."""
    source = strip_js_comments(_MODULE.read_text(encoding="utf-8"))
    imports = re.findall(r"^import\b[^;]*;", source, re.MULTILINE)
    assert imports
    for statement in imports:
        assert re.fullmatch(r'import\s*\{[^}]*\}\s*from\s*"\./[\w-]+\.js";', statement), statement
    assert "auth.js" not in source


def test_constructor_requires_a_workstream_a_fetch_and_every_view_method() -> None:
    """Constructor inputs are data and the view contract is required whole, so a pane missing a
    method fails at construction, not at the first event that needs it."""
    _run(
        r"""
const fails = build => {
  try {
    build();
  } catch (err) {
    return err instanceof TypeError;
  }
  return false;
};
expect(fails(() => new WorkstreamSync({fetch, view})), 'a workstream id is required');
expect(fails(() => new WorkstreamSync({wsId: WS, view})), 'a fetch is required');
for (const name of ['dispatch', 'renderHistory', 'resetLiveState', 'liveState',
  'placeRepairPrompt']) {
  const partial = {...view};
  delete partial[name];
  expect(fails(() => new WorkstreamSync({wsId: WS, fetch, view: partial})),
    'view.' + name + ' is required');
}
"""
    )


def test_on_rejects_an_unknown_event_and_returns_an_unsubscribe() -> None:
    """A misspelt event name fails loudly instead of never firing, and ``on`` hands back a
    function that removes the listener."""
    _run(
        r"""
let threw = false;
try {
  sync.on('reset_settled', () => {});
} catch (err) {
  threw = err instanceof TypeError;
}
expect(threw, 'an unknown event name throws');
const heard = [];
const off = sync.on('status', status => heard.push(status));
sync.start();
await flush();
off();
await network.reply('/history', {messages: []});
expect(heard.join(',') === 'loading', 'an unsubscribed listener hears nothing more', heard.join(','));
"""
    )


def test_a_throwing_listener_stops_neither_another_listener_nor_the_core() -> None:
    """Listeners are isolated: a throwing status listener does not keep the next one from
    hearing the phase, and a throwing ``reloaded`` listener does not stop the connect after it."""
    _run(
        r"""
const heard = [];
sync.on('status', () => { throw new Error('listener bug'); });
sync.on('status', status => heard.push(status));
sync.on('reloaded', () => { throw new Error('listener bug'); });
await firstPaint({messages: []});
expect(heard.join(',') === 'loading,connecting', 'the second listener heard every phase',
  heard.join(','));
expect(sources().length === 1, 'the connect after a throwing reloaded listener still ran');
"""
    )


def test_inspect_reports_whether_the_visibility_listener_is_installed() -> None:
    """``listening`` is false before ``start``, true while the instance listens, and false once
    the terminal teardown removed the listener (dead or destroyed)."""
    _run(
        r"""
pinRandom(0);
expect(sync.inspect().listening === false, 'not before start');
const es = await live();
expect(sync.inspect().listening === true && visibility.listenerCount() === 1, 'while live');
es.drop();
es.fail();
await refuseEveryRedial();
expect(sync.status === 'dead' && sync.inspect().listening === false &&
  visibility.listenerCount() === 0, 'not once dead');
const other = newSync();
other.start();
await flush();
expect(other.inspect().listening === true, 'a second instance listens');
other.destroy();
expect(other.inspect().listening === false && visibility.listenerCount() === 0,
  'not once destroyed');
"""
    )


def test_view_methods_are_called_through_the_view_on_every_call() -> None:
    """A wrapper installed on the view after construction takes effect (the harness wraps
    ``dispatch``), and a throwing dispatch is counted while the stream keeps dispatching."""
    _run(
        r"""
const es = await live();
const dispatch = view.dispatch;
const wrapped = [];
view.dispatch = event => {
  wrapped.push(event.type);
  if (event.boom) throw new Error('render bug');
  dispatch(event);
};
await send(es, {type: 'content', text: 'a', boom: true}, 4);
await send(es, {type: 'content', text: 'b'}, 5);
expect(wrapped.join(',') === 'content,content', 'the late wrapper saw both events', wrapped.join(','));
expect(sync.inspect().health.renderThrows === 1, 'the throw is counted',
  sync.inspect().health.renderThrows);
expect(view.dispatched.map(event => event.text).join('') === 'b', 'the next event still dispatched');
expect(logged.some(line => line.startsWith('workstream sync ws/2: dispatch failed for content ' +
  '(render-throw total 1)')), 'the dispatch line carries the running total', logged.join(' | '));
"""
    )


# --- pure policy -----------------------------------------------------------------------------


def test_build_events_url_orders_cursor_capabilities_and_token() -> None:
    """The cursor comes first (0 included), both capability params always, the token last."""
    _run(
        r"""
const cases = [
  [{base: BASE, wsId: WS}, WS_PATH + '/events?user_turn=1&tool_turn=1'],
  [{wsId: 'w', cursor: 7, token: 'a b'},
    '/v1/api/workstreams/w/events?last_event_id=7&user_turn=1&tool_turn=1&history_token=a%20b'],
  [{wsId: 'w', cursor: 0}, '/v1/api/workstreams/w/events?last_event_id=0&user_turn=1&tool_turn=1'],
  [{wsId: 'w', cursor: '12', token: null},
    '/v1/api/workstreams/w/events?last_event_id=12&user_turn=1&tool_turn=1'],
  [{base: '', wsId: 'w', token: 'tok'},
    '/v1/api/workstreams/w/events?user_turn=1&tool_turn=1&history_token=tok'],
];
for (const [input, url] of cases) {
  expect(buildEventsUrl(input) === url, 'the events URL for ' + JSON.stringify(input),
    buildEventsUrl(input));
}
"""
    )


def test_redial_delay_doubles_from_one_second_to_a_thirty_second_cap() -> None:
    """1 s, 2 s, 4 s ... capped at 30 s, plus up to 500 ms of floored spread."""
    _run(
        r"""
const steps = [1, 2, 3, 4, 5, 6, 7].map(strike => redialDelayMs(strike, 0));
expect(steps.join(',') === '1000,2000,4000,8000,16000,30000,30000', 'doubling to the cap',
  steps.join(','));
expect(redialDelayMs(1, 0.999) === 1499, 'up to 500 ms of spread', redialDelayMs(1, 0.999));
expect(redialDelayMs(0, 0) === 1000, 'a strike below one is the first step');
"""
    )


def test_seedless_render_allowed_requires_every_term() -> None:
    """Open stream, same connection, no streaming reply, no send in flight.  A cursor in the
    /history response is not a term: it promotes the render to the seeded path instead."""
    _run(
        r"""
const ok = {open: true, sameConnection: true, streaming: false, sending: false};
expect(seedlessRenderAllowed(ok), 'every term holds');
expect(seedlessRenderAllowed({...ok, cursor: 3}), 'a cursor is not a term');
const failing = {open: false, sameConnection: false, streaming: true, sending: true};
for (const [term, value] of Object.entries(failing)) {
  expect(!seedlessRenderAllowed({...ok, [term]: value}), 'declined when ' + term + ' fails');
}
"""
    )


def test_status_copy_names_every_phase_once() -> None:
    """One frozen string per phase, so both panes show the same words; an empty string means the
    pane shows its own counters."""
    _run(
        r"""
const phases = ['idle', 'loading', 'connecting', 'live', 'reconnecting', 'backoff', 'paused',
  'degraded', 'repairing', 'parked', 'auth', 'dead', 'destroyed'];
const keys = Object.keys(STREAM_STATUS_TEXT);
expect(keys.slice().sort().join(',') === phases.slice().sort().join(','), 'one entry per phase',
  keys.join(','));
expect(Object.isFrozen(STREAM_STATUS_TEXT), 'the copy is frozen');
expect(STREAM_STATUS_TEXT.reconnecting === 'Reconnecting' + ELLIPSIS, 'reconnecting copy');
expect(STREAM_STATUS_TEXT.degraded === 'Connection is slow ' + DASH + ' catching up' + ELLIPSIS,
  'degraded copy');
expect(STREAM_STATUS_TEXT.repairing === 'History out of date ' + DASH + ' retrying' + ELLIPSIS,
  'repairing copy');
expect(STREAM_STATUS_TEXT.parked === 'Live updates paused', 'parked copy');
expect(STREAM_STATUS_TEXT.dead === 'Disconnected', 'dead copy');
expect(STREAM_STATUS_TEXT.live === '' && STREAM_STATUS_TEXT.idle === '', 'live shows the counters');
"""
    )


# --- the phase table -------------------------------------------------------------------------


def test_start_acts_only_in_idle() -> None:
    """``start`` begins the seeded reload once; a second call during the load or on a live
    stream fetches and opens nothing."""
    _run(
        r"""
sync.start();
await flush();
expect(sync.status === 'loading', 'start begins the seeded reload', sync.status);
sync.start();
await flush();
expect(historyCount() === 1, 'a second start during the load is ignored', historyCount());
await network.reply('/history', {messages: [user('q')]});
expect(sources().length === 1 && sync.status === 'connecting', 'the load connects', sync.status);
latest().open();
await flush();
sync.start();
await flush();
expect(historyCount() === 1 && sources().length === 1, 'start on a live stream is ignored');
expect(statuses.join(',') === 'loading,connecting,live', 'the phases so far', statuses.join(','));
"""
    )


def test_retry_now_redials_from_backoff_at_once_with_the_strikes_reset() -> None:
    """In backoff, ``retryNow`` cancels the pending redial and connects now; the strike count
    restarts, so the next refusal waits only the first backoff step."""
    _run(
        r"""
pinRandom(0);
const es = await live();
es.drop();
es.fail();
expect(sync.status === 'backoff', 'a refused stream backs off at once', sync.status);
await clock.advance(500);
sync.retryNow();
expect(sources().length === 2 && sync.status === 'connecting', 'retryNow redials at once');
latest().fail();
expect(sync.status === 'backoff' && sources().length === 2,
  'the refusal backs off and the cancelled redial never fired', sync.status);
await clock.advance(999);
expect(sources().length === 2, 'still inside the first backoff step');
await clock.advance(1);
expect(sources().length === 3, 'the first step again: retryNow reset the strikes', sources().length);
"""
    )


def test_retry_now_resumes_from_auth() -> None:
    """A stream that closed while the login overlay is up waits in ``auth`` with no redial at
    all; ``retryNow`` (the login fan-out) connects again."""
    _run(
        r"""
const es = await live({messages: [], cursor: 3});
loginOverlay.style.display = 'flex';
es.drop();
es.fail();
expect(sync.status === 'auth', 'a CLOSED stream behind the overlay is auth at once', sync.status);
await clock.advance(600000);
expect(sources().length === 1 && sync.status === 'auth', 'nothing redials behind the overlay');
loginOverlay.style.display = 'none';
sync.retryNow();
expect(sources().length === 2 && sync.status === 'connecting', 'retryNow resumes after the login');
expect(cursorOf(latest()) === '3', 'at the cursor', cursorOf(latest()));
"""
    )


def test_auth_is_not_a_strike() -> None:
    """With six strikes spent, a refusal while the login overlay is up parks in ``auth`` instead
    of counting the seventh strike and ending the instance."""
    _run(
        r"""
pinRandom(0);
const es = await live();
es.drop();
es.fail();
await refuseEveryRedial({until: () => sources().length === 6});
expect(sync.status === 'backoff', 'six strikes so far', sync.status);
await clock.advance(30000);
expect(sources().length === 7, 'the sixth redial', sources().length);
loginOverlay.style.display = 'flex';
latest().fail();
expect(sync.status === 'auth', 'the would-be seventh strike is auth, not dead', sync.status);
"""
    )


def test_retry_now_opens_nothing_during_a_reload() -> None:
    """A login or reconnect click during a seeded reload must not open a stream under it."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await clock.advance(0);
expect(sync.status === 'loading' && es.closeCalled, 'the resync is loading', sync.status);
sync.retryNow();
await flush();
expect(sources().length === 1 && sync.status === 'loading', 'retryNow during a reload opens nothing');
await network.reply('/history', {messages: [], cursor: 9});
expect(sources().length === 2, 'the reload connects when it settles');
"""
    )


def test_retry_now_opens_nothing_during_the_degraded_cooldown() -> None:
    """The overflow cooldown keeps the stream down for its whole length, retryNow included."""
    _run(
        r"""
const es = await live();
for (let i = 0; i < 3; i += 1) await send(es, {type: 'stream_overflow'});
expect(sync.status === 'degraded', 'three overflows enter the cooldown', sync.status);
sync.retryNow();
await flush();
expect(sources().length === 1, 'retryNow during the cooldown opens nothing');
await clock.advance(15000 - 1);
expect(sources().length === 1, 'nothing before the cooldown elapses');
await clock.advance(1);
expect(sources().length === 2, 'the cooldown reconnects');
"""
    )


def test_retry_now_opens_nothing_while_repairing() -> None:
    """While the handoff repair owns recovery no stream opens, during an attempt or between
    attempts."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
expect(sync.status === 'repairing', 'history_resync starts the repair', sync.status);
sync.retryNow();
await flush();
expect(sources().length === 1, 'retryNow during a repair attempt opens nothing');
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
sync.retryNow();
await flush();
expect(sources().length === 1 && sync.status === 'repairing',
  'retryNow between repair attempts opens nothing', sync.status);
"""
    )


def test_retry_now_opens_nothing_on_a_dead_or_destroyed_instance() -> None:
    """Recovery from dead is rebuilding the pane (possibly on another node), never a redial."""
    _run(
        r"""
pinRandom(0);
const es = await live();
es.drop();
es.fail();
await refuseEveryRedial();
expect(sync.status === 'dead', 'seven refusals', sync.status);
const count = sources().length;
sync.retryNow();
await flush();
expect(sources().length === count, 'retryNow on a dead instance opens nothing');
sync.destroy();
sync.retryNow();
await flush();
expect(sources().length === count && sync.status === 'destroyed',
  'retryNow on a destroyed instance opens nothing');
"""
    )


def test_retry_now_leaves_a_live_stream_alone() -> None:
    """The login fan-out does not touch a healthy stream: an open EventSource survives cookie
    expiry, so there is nothing to redial and no /history to reload."""
    _run(
        r"""
const es = await live({messages: [user('kept')]});
await send(es, {type: 'state_change', state: 'idle'}, 3);
sync.retryNow();
await clock.advance(1000);
expect(!es.closeCalled && sources().length === 1, 'no close and no new stream');
expect(historyCount() === 1, 'no /history reload', historyCount());
"""
    )


def test_hide_closes_the_stream_and_show_reconnects_at_the_cursor() -> None:
    """Hide closes a live stream and pauses; nothing reopens it while hidden; show connects at the
    last id seen."""
    _run(
        r"""
const es = await live({messages: [], cursor: 3});
await send(es, {type: 'state_change', state: 'idle'}, 6);
const callsBeforeHide = view.calls.length;
visibility.hide();
expect(es.closeCalled && sync.status === 'paused', 'hide closes and pauses', sync.status);
await clock.advance(120000);
expect(sources().length === 1, 'nothing opens while hidden');
visibility.show();
await flush();
expect(sources().length === 2 && cursorOf(latest()) === '6', 'show reconnects at the cursor',
  cursorOf(latest()));
expect(!view.calls.slice(callsBeforeHide).includes('resetLiveState'),
  'hide and show leave the reply refs alone (the visible tail survives)', view.calls.join(','));
"""
    )


def test_hide_without_a_stream_keeps_the_phase_and_show_outside_paused_opens_nothing() -> None:
    """While a reload holds the stream down, hide has nothing to close and show has nothing to
    resume: the reload's own connect decides."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await clock.advance(0);
expect(sync.status === 'loading', 'the resync is loading', sync.status);
visibility.hide();
expect(sync.status === 'loading', 'hide without a stream leaves the phase', sync.status);
visibility.show();
await flush();
expect(sources().length === 1 && sync.status === 'loading', 'show outside paused opens nothing');
await network.reply('/history', {messages: [], cursor: 9});
expect(sources().length === 2 && sync.status === 'connecting', 'the reload connects', sync.status);
"""
    )


def test_a_load_that_settles_while_hidden_waits_for_the_show_edge() -> None:
    """A connect due while the tab is hidden pauses instead (a hidden tab is the likeliest slow
    consumer); show connects at the /history cursor."""
    _run(
        r"""
sync.start();
await flush();
visibility.hide();
expect(sync.status === 'loading', 'hide during the first load changes nothing', sync.status);
await network.reply('/history', {messages: [], cursor: 5});
expect(sources().length === 0 && sync.status === 'paused', 'the connect defers to the show edge',
  sync.status);
await clock.advance(60000);
expect(sources().length === 0, 'nothing opens while hidden');
visibility.show();
await flush();
expect(sources().length === 1 && cursorOf(latest()) === '5', 'show connects at the cursor');
"""
    )


def test_a_cooldown_that_ends_while_hidden_waits_for_the_show_edge() -> None:
    """A timer that would connect while the tab is hidden goes to ``paused`` instead."""
    _run(
        r"""
const es = await live();
for (let i = 0; i < 3; i += 1) await send(es, {type: 'stream_overflow'});
visibility.hide();
expect(sync.status === 'degraded', 'hide during the cooldown changes nothing', sync.status);
await clock.advance(15000);
expect(sources().length === 1 && sync.status === 'paused', 'the cooldown ends paused', sync.status);
visibility.show();
await flush();
expect(sources().length === 2 && sync.status === 'connecting', 'show connects', sync.status);
"""
    )


def test_a_repair_attempt_due_while_hidden_waits_and_show_reschedules_it() -> None:
    """A repair attempt due while hidden pauses; the show edge goes through the connect
    chokepoint, which reschedules the repair instead of opening a stream."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
visibility.hide();
await clock.advance(2000);
expect(sync.status === 'paused' && historyCount() === 2, 'the attempt due while hidden waits',
  sync.status + ' ' + historyCount());
visibility.show();
await flush();
expect(sync.status === 'repairing' && sources().length === 1 && historyCount() === 2,
  'show schedules the repair, not a stream', sync.status);
await clock.advance(4000);
expect(historyCount() === 3, 'the rescheduled attempt runs after its backoff', historyCount());
await network.reply('/history', {messages: [], cursor: 9, handoff_token: 'tok-2'});
expect(sources().length === 2 && params(latest()).get('history_token') === 'tok-2',
  'a completed repair connects with its token');
"""
    )


def test_an_error_is_reconnecting_and_the_reopen_is_live() -> None:
    """An error on a stream the browser is re-establishing is ``reconnecting``; its reopen is
    ``live``, and nothing redials behind it."""
    _run(
        r"""
const es = await live();
es.drop();
expect(sync.status === 'reconnecting', 'an error is reconnecting', sync.status);
await clock.advance(3000);
es.open();
await flush();
expect(sync.status === 'live', 'the reopen is live', sync.status);
await clock.advance(10000);
expect(sources().length === 1 && sync.status === 'live', 'nothing redialled');
"""
    )


def test_a_refusal_backs_off_at_once_including_a_refused_redial() -> None:
    """The browser marks a failed connection CLOSED before it dispatches the error, so the error
    itself decides: a refusal is a strike and a backoff at once, and so is a redial refused
    outright; a drop the browser re-establishes is left to it."""
    _run(
        r"""
pinRandom(0);
const es = await live();
es.drop();
expect(sync.status === 'reconnecting', 'a drop is reconnecting', sync.status);
es.fail();
expect(sync.status === 'backoff' && sync.inspect().source === null,
  'the refusal backs off at once and drops the dead source', sync.status);
await clock.advance(999);
expect(sources().length === 1, 'inside the first step');
await clock.advance(1);
expect(sources().length === 2, 'the first redial', sources().length);
latest().fail();
expect(sync.status === 'backoff', 'a redial refused outright is the next strike', sync.status);
await clock.advance(1999);
expect(sources().length === 2, 'the second step is 2 s');
await clock.advance(1);
expect(sources().length === 3, 'the second redial', sources().length);
"""
    )


def test_a_stream_the_browser_is_reestablishing_is_left_to_it() -> None:
    """A dropped stream the browser is reconnecting itself (CONNECTING) is no strike and gets no
    redial, however long it takes; its open makes it live again."""
    _run(
        r"""
const es = await live();
es.drop();
await clock.advance(60000);
expect(sync.status === 'reconnecting' && sources().length === 1 && !es.closeCalled,
  'no strike, no redial, no close', sync.status);
es.open();
await flush();
expect(sync.status === 'live', 'the native reconnect opens', sync.status);
"""
    )


def test_a_refused_stream_backs_off_and_dies_on_the_seventh_refusal() -> None:
    """Refused every time: redials 1, 2, 4, 8, 16 and 30 s apart, then the seventh refusal ends
    the instance (about a minute) with nothing left armed."""
    _run(
        r"""
pinRandom(0);
const es = await live();
es.drop();
es.fail();
const start = clock.elapsed;
const times = await refuseEveryRedial();
expect(times.join(',') === '1000,3000,7000,15000,31000,61000', 'the redial times', times.join(','));
expect(sync.status === 'dead' && clock.elapsed - start === 61000, 'dead at the seventh refusal',
  sync.status + ' at ' + (clock.elapsed - start));
expect(sources().length === 7, 'the first stream plus six redials', sources().length);
expect(statuses.at(-1) === 'dead', 'dead is announced', statuses.join(','));
expectReleased('dead');
"""
    )


def test_an_open_resets_the_strikes() -> None:
    """A stream that opens clears the strike count, so a later refusal starts the backoff over."""
    _run(
        r"""
pinRandom(0);
const es = await live();
es.drop();
es.fail();
await refuseEveryRedial({until: () => sources().length === 3});
await clock.advance(4000);
expect(sources().length === 4, 'the third backoff step redials', sources().length);
latest().open();
await flush();
latest().drop();
latest().fail();
await clock.advance(999);
expect(sources().length === 4, 'inside the first step after the reset');
await clock.advance(1);
expect(sources().length === 5, 'the first step again', sources().length);
"""
    )


def test_three_overflows_close_the_stream_for_the_cooldown() -> None:
    """Three overflow closes inside the window close the stream for the base cooldown, each
    followed by the server's close and the browser's reconnect as on the wire; then one stream."""
    _run(
        r"""
const es = await live();
for (let i = 0; i < 2; i += 1) {
  await send(es, {type: 'stream_overflow'});
  es.drop();
  await clock.advance(1000);
  es.open();
  await flush();
}
await send(es, {type: 'stream_overflow'});
es.drop();
await flush();
expect(es.closeCalled && sync.status === 'degraded', 'the third overflow enters the cooldown');
await clock.advance(15000 - 1);
expect(sources().length === 1, 'no stream before the cooldown elapses', sources().length);
await clock.advance(1);
expect(sources().length === 2 && sync.status === 'connecting', 'one stream after the cooldown');
expect(sync.inspect().health.overflows === 3, 'three overflows counted');
"""
    )


def test_a_dead_instance_stays_inspectable_and_never_reopens() -> None:
    """Dead is terminal: no show edge, timer or retry reopens it, yet ``inspect`` and the getters
    still answer; destroy then ends it."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [], cursor: 2});
es.drop();
es.fail();
await refuseEveryRedial();
expect(sync.dead && sync.inspect().status === 'dead', 'dead and inspectable');
expect(sync.inspect().health.opens === 1, 'the counters survive', sync.inspect().health.opens);
expectReleased('dead');
const count = sources().length;
visibility.hide();
visibility.show();
await clock.advance(600000);
expect(sources().length === count && historyCount() === 1, 'nothing reopens a dead instance');
sync.destroy();
expect(sync.status === 'destroyed' && !sync.dead, 'destroy ends a dead instance', sync.status);
"""
    )


def test_destroy_ends_a_live_stream_for_good() -> None:
    """Destroy closes the stream, drops the listener and every timer; nothing reopens it, and a
    second destroy changes nothing."""
    _run(
        r"""
const es = await live({messages: [], cursor: 3});
sync.destroy();
expect(es.closeCalled && sync.status === 'destroyed', 'destroy closes the stream', sync.status);
expectReleased('destroyed');
visibility.hide();
visibility.show();
await clock.advance(120000);
expect(sources().length === 1, 'nothing reopens a destroyed instance');
sync.destroy();
expect(statuses.filter(status => status === 'destroyed').length === 1, 'a second destroy is a no-op');
"""
    )


# --- one behavior where the clients differed ----------------------------------------------------


def test_content_during_a_seedless_refetch_is_held_and_flushed_in_order_after_the_render() -> None:
    """While a ``clear_ui`` refetch is in flight every event waits; after the render they
    dispatch in arrival order, each with its own id."""
    _run(
        r"""
const es = await live({messages: [user('before')]});
await send(es, {type: 'clear_ui'}, 4);
expect(historyCount() === 2 && sync.inspect().quiesced, 'clear_ui refetches with the queue armed');
await send(es, {type: 'content', text: 'one'}, 5);
await send(es, {type: 'tool_info', items: []}, 6);
await send(es, {type: 'content', text: 'two'}, 7);
expect(view.dispatched.length === 0, 'nothing dispatches during the refetch');
const before = view.calls.length;
await network.reply('/history', {messages: [user('after')]});
const after = view.calls.slice(before).join(',');
expect(after === 'resetLiveState,renderHistory,dispatch,dispatch,dispatch',
  'the held events dispatch after the render', after);
expect(view.dispatched.map(event => event.type + ':' + (event.text || '')).join(',') ===
  'content:one,tool_info:,content:two', 'in arrival order');
expect(view.dispatched.map(event => event._event_id).join(',') === '5,6,7', 'each with its own id');
expect(!sync.inspect().quiesced, 'the queue is released');
"""
    )


def test_a_seedless_render_is_declined_when_the_stream_is_down() -> None:
    """A stream torn down during the refetch has a frozen cursor: the render is declined, the
    transcript stays and the latch stays set."""
    _run(
        r"""
const es = await live({messages: [user('before')]});
await send(es, {type: 'clear_ui'}, 4);
visibility.hide();
await network.reply('/history', {messages: [user('after')]});
expect(renders() === 1 && outcomes() === 'declined', 'declined', outcomes());
expect(sync.historyStale, 'the latch stays set');
"""
    )


def test_a_seedless_render_is_declined_across_a_reconnect() -> None:
    """A stream that dropped and reopened during the refetch reads OPEN again, but its replay may
    already carry what the snapshot holds (#904): the connection counter declines the render."""
    _run(
        r"""
const es = await live({messages: [user('before')]});
await send(es, {type: 'clear_ui'}, 4);
es.drop();
await clock.advance(1000);
es.open();
await flush();
await network.reply('/history', {messages: [user('after')]});
expect(renders() === 1 && outcomes() === 'declined', 'declined across the reconnect', outcomes());
"""
    )


def test_a_seedless_render_is_declined_while_a_reply_streams() -> None:
    """A render would wipe the bubble a reply is streaming into."""
    _run(
        r"""
const es = await live({messages: [user('before')]});
await send(es, {type: 'clear_ui'}, 4);
view.live.streaming = true;
await network.reply('/history', {messages: [user('after')]});
expect(renders() === 1 && outcomes() === 'declined', 'declined under a streaming reply', outcomes());
"""
    )


def test_a_seedless_render_is_declined_while_the_users_send_is_in_flight() -> None:
    """The user's own send is not a stream event, so its row may be newer than the snapshot."""
    _run(
        r"""
const es = await live({messages: [user('before')]});
await send(es, {type: 'clear_ui'}, 4);
view.live.busy = true;
view.live.sending = true;
await network.reply('/history', {messages: [user('after')]});
expect(renders() === 1 && outcomes() === 'declined', 'declined during a send', outcomes());
"""
    )


def test_a_refetch_whose_history_carries_a_cursor_is_promoted_to_a_seeded_render() -> None:
    """/history returns a cursor only when it trimmed a running turn it expects the caller to
    replay from that cursor, so the refetch renders as a seeded load does: the stream goes down,
    the trimmed history renders with the cursor and token adopted, and the stream is rebuilt at
    the cursor, straight from live to connecting, with neither reloaded nor missed."""
    _run(
        r"""
const es = await live({messages: [user('before')], cursor: 2});
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {messages: [user('after')], cursor: 3, handoff_token: 'tok-p'});
expect(outcomes() === 'rendered' && renders() === 2 && !sync.historyStale,
  'the trimmed history renders and clears the latch', outcomes());
expect(es.closeCalled && sources().length === 2, 'the stream is rebuilt', String(sources().length));
expect(cursorOf(latest()) === '3' && params(latest()).get('history_token') === 'tok-p',
  'presenting the adopted cursor and the token', latest().url);
expect(statuses.slice(-2).join(',') === 'live,connecting', 'straight from live to connecting',
  statuses.join(','));
expect(reloaded.length === 1 && missed.length === 0, 'neither reloaded nor missed',
  reloaded.length + ' ' + missed.length);
latest().open();
await flush();
expect(sync.status === 'live' && missed.length === 0, 'the reopen presents a cursor: nothing missed');
"""
    )


def test_a_seedless_render_proceeds_when_every_term_holds_and_keeps_the_cursor() -> None:
    """The render replaces the transcript and clears the latch, but never rewinds the live
    cursor (a later reconnect would replay rows twice)."""
    _run(
        r"""
const es = await live({messages: [user('before')], cursor: 2});
await send(es, {type: 'clear_ui'}, 4);
expect(sync.historyStale, 'clear_ui sets the latch before the refetch');
await network.reply('/history', {messages: [user('after')]});
expect(renders() === 2 && outcomes() === 'rendered', 'rendered', outcomes());
expect(!sync.historyStale, 'the render clears the latch');
expect(clock.pending().length === 0, 'and arms no retry', JSON.stringify(clock.pending()));
expect(sync.inspect().cursor === '4' && !es.closeCalled, 'the stream and its cursor stay');
"""
    )


def test_a_seeded_render_ignores_live_state_and_resets_it_right_before_rendering() -> None:
    """The seeded render's only gate is the generation: a reply that started during the resync
    jitter does not hold it off, and live state is reset immediately before the render."""
    _run(
        r"""
pinRandom(0.5);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'content', text: 'turn'}, 6);
expect(view.live.streaming, 'a reply is streaming during the jitter');
await clock.advance(5000);
const before = view.calls.length;
await network.reply('/history', {messages: [user('fresh')], cursor: 9});
const after = view.calls.slice(before).join(',');
expect(after === 'resetLiveState,renderHistory', 'reset right before the render', after);
expect(reloaded.at(-1).outcome === 'rendered', 'the seeded render proceeded');
"""
    )


def test_a_reload_without_a_gap_resets_live_state_before_its_fetch() -> None:
    """A reload with no truncation gap on record cannot resume into the reply refs, so it drops
    them up front, and a failed fetch then renders nothing."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'content', text: 'reply'}, 4);
const before = view.calls.length;
await send(es, {type: 'history_resync'});
expect(view.calls.slice(before).join(',') === 'resetLiveState', 'reset before the fetch',
  view.calls.slice(before).join(','));
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
expect(view.calls.slice(before).join(',') === 'resetLiveState', 'a failed fetch renders nothing',
  view.calls.slice(before).join(','));
"""
    )


def test_a_reload_with_a_gap_keeps_live_state_through_a_failed_fetch() -> None:
    """With a gap on record the reconnect resumes into the reply that streamed during the jitter,
    so a failed resync leaves the refs alone and reconnects at the gap."""
    _run(
        r"""
pinRandom(0.5);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'content', text: 'turn'}, 6);
const before = view.calls.length;
await clock.advance(5000);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(view.calls.slice(before).join(',') === '', 'no reset and no render',
  view.calls.slice(before).join(','));
expect(cursorOf(latest()) === '5', 'the reconnect presents the gap', cursorOf(latest()));
"""
    )


def test_start_after_destroy_opens_nothing() -> None:
    """A pane that calls ``start`` after its own destroy (its snapshot request resolved late)
    gets no fetch, no stream and no listener."""
    _run(
        r"""
sync.destroy();
sync.start();
await clock.advance(60000);
expect(historyCount() === 0 && sources().length === 0 && visibility.listenerCount() === 0,
  'nothing after destroy');
expect(sync.status === 'destroyed', 'still destroyed', sync.status);
"""
    )


def test_a_history_load_that_never_answers_fails_at_fifteen_seconds() -> None:
    """Every /history is bounded: at 15 s the request is aborted and the load fails, and first
    paint still connects."""
    _run(
        r"""
sync.start();
await flush();
const request = network.take('/history');
await clock.advance(14999);
expect(sources().length === 0, 'still waiting at 14.999 s');
await clock.advance(1);
expect(request.state === 'aborted', 'the deadline aborts the request', request.state);
expect(sources().length === 1 && JSON.stringify(reloaded) === '[{"first":true,"outcome":"failed"}]',
  'a failed load still connects', JSON.stringify(reloaded));
"""
    )


def test_the_deadline_holds_when_fetch_ignores_the_abort_signal() -> None:
    """The bound is a race, not only an abort: a fetch sleeping on a Retry-After that ignores the
    signal still loses at 15 s."""
    _run(
        r"""
const deaf = newSync({fetch: () => new Promise(() => {})});
const seen = [];
deaf.on('reloaded', payload => seen.push(payload.outcome));
deaf.start();
await clock.advance(14999);
expect(seen.length === 0, 'still waiting at 14.999 s');
await clock.advance(1);
expect(seen.join(',') === 'failed' && sources().length === 1, 'the race ends the attempt',
  seen.join(','));
"""
    )


def test_a_seedless_refetch_that_never_answers_fails_at_fifteen_seconds() -> None:
    """The bound covers seedless refetches too; the held events then flush onto the kept
    transcript."""
    _run(
        r"""
const es = await live({messages: [user('kept')]});
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'content', text: 'held'}, 5);
await clock.advance(14999);
expect(settled.length === 0 && view.dispatched.length === 0, 'still waiting, still holding');
await clock.advance(1);
expect(outcomes() === 'failed', 'the refetch fails at 15 s', outcomes());
expect(dispatchedTypes() === 'content' && renders() === 1, 'the held event flushes');
"""
    )


def test_first_paint_reports_reloaded_before_it_connects() -> None:
    """``reloaded`` fires after the load settles and before the stream is built, so a pane's
    first-paint work runs ahead of the stream."""
    _run(
        r"""
const builtAtReloaded = [];
sync.on('reloaded', () => builtAtReloaded.push(sources().length));
await firstPaint({messages: [user('q')], cursor: 4});
expect(JSON.stringify(reloaded) === '[{"first":true,"outcome":"rendered"}]', 'reloaded',
  JSON.stringify(reloaded));
expect(builtAtReloaded.join(',') === '0' && sources().length === 1, 'before the stream is built');
"""
    )


def test_first_paint_connects_after_a_failed_load() -> None:
    """A failed first /history renders nothing and still connects, cursorless and tokenless (the
    server then sends its bootstrap clear_ui)."""
    _run(
        r"""
sync.start();
await flush();
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(reloaded[0].outcome === 'failed' && renders() === 0, 'failed, nothing rendered');
expect(sources().length === 1, 'the stream still opens');
expect(!params(latest()).has('last_event_id') && !params(latest()).has('history_token'),
  'cursorless and tokenless', latest().url);
"""
    )


def test_first_paint_connects_after_a_render_throw() -> None:
    """A render throw does not strand the pane: the stream opens at the seeded cursor, without
    the token a partial render must not vouch for."""
    _run(
        r"""
view.renderHistory = () => { throw new Error('render bug'); };
sync.start();
await flush();
await network.reply('/history', {messages: [user('q')], cursor: 4, handoff_token: 'tok-1'});
expect(reloaded[0].outcome === 'threw' && sources().length === 1, 'a render throw still connects');
expect(cursorOf(latest()) === '4' && !params(latest()).has('history_token'),
  'at the seeded cursor, without the token', latest().url);
"""
    )


def test_the_backstop_refetches_at_the_tail_of_the_backlog() -> None:
    """An idle edge with the latch set refetches only after the rest of the backlog it arrived in
    has dispatched, so the refetch cannot divert those events into its own queue."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('kept')]});
await latchAfterTwoFailures(es);
await send(es, {type: 'clear_ui'}, 5);
await send(es, {type: 'state_change', state: 'idle'}, 6);
await send(es, {type: 'status', n: 1}, 7);
await send(es, {type: 'status', n: 2}, 8);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(dispatchedTypes() === 'state_change,status,status', 'the whole backlog dispatched',
  dispatchedTypes());
expect(network.pending('/history').length === 1 && sync.inspect().quiesced,
  'then the backstop refetched', network.pending('/history').length);
expect(!es.closeCalled, 'transport-free');
"""
    )


def test_the_backstop_collapses_several_idle_edges_into_one_refetch() -> None:
    """Several idle edges in one backlog make one backstop attempt."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('kept')]});
await latchAfterTwoFailures(es);
await send(es, {type: 'clear_ui'}, 5);
for (let id = 6; id < 9; id += 1) await send(es, {type: 'state_change', state: 'idle'}, id);
const loads = historyCount();
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(historyCount() === loads + 1, 'one backstop refetch', historyCount() - loads);
"""
    )


def test_the_backstop_waits_out_a_replay_backlog_and_heals_once() -> None:
    """replay_ok's synthetic current-state idle precedes the canonical ring slice.  Held behind
    a refetch, that backlog dispatches whole and in order before the one backstop refetch (its
    idle edges collapse); the backstop's render then heals the latch, and the slice's content
    was dispatched exactly once."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('kept')]});
await latchAfterTwoFailures(es);
await send(es, {type: 'clear_ui'}, 5);
await send(es, {type: 'state_change', state: 'idle'});
await send(es, {type: 'user_turn', content: 'gap'}, 6);
await send(es, {type: 'content', text: 'DUPE-runtime'}, 7);
await send(es, {type: 'stream_end'}, 8);
await send(es, {type: 'state_change', state: 'idle'}, 9);
const loads = historyCount();
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(dispatchedTypes() === 'state_change,user_turn,content,stream_end,state_change',
  'the canonical backlog dispatched whole, in order', dispatchedTypes());
expect(historyCount() === loads + 1 && sync.inspect().quiesced,
  'then exactly one backstop refetch, holding new events', historyCount() - loads);
const before = renders();
await network.reply('/history', {messages: [user('kept'),
  {role: 'assistant', content: 'DUPE-runtime'}]});
expect(renders() === before + 1 && !sync.historyStale && !sync.inspect().quiesced,
  'the backstop rendered once and healed the latch');
expect(view.dispatched.filter(event => event.type === 'content').length === 1,
  'the slice content dispatched once');
"""
    )


def test_the_backstop_stands_aside_while_a_truncation_gap_is_on_record() -> None:
    """A recorded gap belongs to the seeded resync: the transport-free backstop does not race it
    with a seedless render."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await latchAfterTwoFailures(es);
await send(es, {type: 'replay_truncated'});
await clock.advance(0);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
const reconnect = latest();
reconnect.open();
await flush();
const loads = historyCount();
await send(reconnect, {type: 'state_change', state: 'idle'}, 9);
await clock.advance(1000);
expect(historyCount() === loads, 'no backstop while the gap is on record', historyCount() - loads);
"""
    )


def test_the_backstop_stands_aside_when_a_later_event_starts_a_turn() -> None:
    """Every owner is re-checked at the tail: a turn that started later in the same backlog holds
    the backstop off."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await latchAfterTwoFailures(es);
await send(es, {type: 'clear_ui'}, 5);
await send(es, {type: 'state_change', state: 'idle'}, 6);
await send(es, {type: 'state_change', state: 'thinking'}, 7);
const loads = historyCount();
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(historyCount() === loads, 'no backstop under a running turn', historyCount() - loads);
"""
    )


def test_no_stream_opens_while_a_resync_loads_after_a_recent_error() -> None:
    """A stream error just before the resync fires (the browser re-establishing the stream); the
    reload's teardown closes that stream, so the stream stays down for the whole load."""
    _run(
        r"""
pinRandom(0.3);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await clock.advance(1000);
es.drop();
await clock.advance(2000);
expect(sync.status === 'loading' && network.pending('/history').length === 1, 'the resync loads',
  sync.status);
await clock.advance(10000);
expect(sources().length === 1, 'no stream opens while the resync loads', sources().length);
"""
    )


def test_stream_errors_request_nothing_but_history() -> None:
    """The core never asks for the workstream resource (that GET rehydrates a closed session for
    a writer): through errors, redials and death it requests only /history and /events."""
    _run(
        r"""
pinRandom(0);
const es = await live();
es.drop();
await clock.advance(6000);
es.open();
await flush();
es.drop();
es.fail();
await refuseEveryRedial();
const urls = network.requests.map(request => request.method + ' ' + request.url.split('?')[0]);
expect(urls.join(',') === 'GET ' + WS_PATH + '/history', 'only the first-paint /history',
  urls.join(', '));
expect(sources().every(source => new URL(source.url).pathname === WS_PATH + '/events'),
  'and /events');
"""
    )


def test_missed_fires_once_for_a_new_truncation_gap() -> None:
    """A new gap is missed once and the same gap re-announced by a retry adds nothing.  The
    resync that heals it by adopting a cursor is missed again: the envelope said the gap opened,
    and whatever the stream does not carry whole (a sidebar) moved on before the heal, so the
    pane re-reads it then too.  The reconnect after the heal presents the cursor and adds
    nothing."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
expect(missed.length === 1, 'a new gap is missed', missed.length);
await clock.advance(0);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
const retry = latest();
retry.open();
await flush();
await send(retry, {type: 'replay_truncated'});
expect(missed.length === 1, 'the same gap again is not new', missed.length);
await clock.advance(0);
await network.reply('/history', {messages: [], cursor: 12});
latest().open();
await flush();
expect(missed.length === 2, 'the heal that adopts a cursor is missed too', missed.length);
"""
    )


def test_a_first_stream_that_opens_late_is_missed_and_a_prompt_one_is_not() -> None:
    """The first open follows first paint closely enough to vouch for it only when it was prompt:
    a hidden first paint, a hide while the first dial is in flight, a refused or errored first
    dial and the login park each make the first open late, and late is a miss (the pane re-reads
    what the stream does not carry whole)."""
    _run(
        r"""
pinRandom(0);
// Prompt: no miss.
const es = await live({messages: []});
expect(missed.length === 0, 'a prompt first open is not missed', missed.length);
sync.destroy();

// Hidden first paint: the connect waits for the show edge.
const hidden = newSync();
const hiddenMissed = [];
hidden.on('missed', () => hiddenMissed.push(1));
visibility.hide();
hidden.start();
await flush();
await network.reply('/history', {messages: []});
expect(hidden.status === 'paused', 'a hidden first paint defers the stream', hidden.status);
visibility.show();
await flush();
latest().open();
await flush();
expect(hiddenMissed.length === 1, 'a first open after the show edge is missed',
  hiddenMissed.length);
hidden.destroy();

// Hidden while the first dial is still connecting: the show edge dials again.
const hiddenDialing = newSync();
const hiddenDialingMissed = [];
hiddenDialing.on('missed', () => hiddenDialingMissed.push(1));
hiddenDialing.start();
await flush();
await network.reply('/history', {messages: []});
expect(hiddenDialing.status === 'connecting', 'the first dial is in flight',
  hiddenDialing.status);
visibility.hide();
await clock.advance(60000);
visibility.show();
await flush();
latest().open();
await flush();
expect(hiddenDialingMissed.length === 1, 'a first open after a hide mid-dial is missed',
  hiddenDialingMissed.length);
hiddenDialing.destroy();

// Refused first dial: the redial's open is late.
const refused = newSync();
const refusedMissed = [];
refused.on('missed', () => refusedMissed.push(1));
refused.start();
await flush();
await network.reply('/history', {messages: []});
latest().fail();
await clock.advance(1000);
latest().open();
await flush();
expect(refusedMissed.length === 1, 'a first open after a refused dial is missed',
  refusedMissed.length);
refused.destroy();

// Errored first dial the browser re-establishes itself.
const errored = newSync();
const erroredMissed = [];
errored.on('missed', () => erroredMissed.push(1));
errored.start();
await flush();
await network.reply('/history', {messages: []});
latest().drop();
await clock.advance(3000);
latest().open();
await flush();
expect(erroredMissed.length === 1, 'a first open after an errored dial is missed',
  erroredMissed.length);
errored.destroy();

// Parked behind the login overlay.
const parked = newSync();
const parkedMissed = [];
parked.on('missed', () => parkedMissed.push(1));
parked.start();
await flush();
await network.reply('/history', {messages: []});
loginOverlay.style.display = 'flex';
latest().fail();
expect(parked.status === 'auth', 'the refusal parks behind the login', parked.status);
loginOverlay.style.display = 'none';
parked.retryNow();
latest().open();
await flush();
expect(parkedMissed.length === 1, 'a first open after the login park is missed',
  parkedMissed.length);
parked.destroy();
"""
    )


def test_each_failed_history_request_warns_once_and_an_abort_says_nothing() -> None:
    """One warning per failed /history (a status, a network error, a deadline expiry), each
    naming the workstream; an abort for supersession or teardown is not a failure and logs
    nothing."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('v1')]});
const warnings = () => logged.filter(line => line.includes('history request'));
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
expect(warnings().length === 1 &&
  warnings()[0] === 'workstream sync ws/2: history request failed (status 503)',
  'a status failure warns once', warnings().join(' | '));
await clock.advance(2000);
await network.fail('/history');
expect(warnings().length === 2 && warnings()[1].startsWith('workstream sync ws/2: history ' +
  'request failed Failed to fetch'), 'a network error warns once', warnings().join(' | '));
await send(es, {type: 'clear_ui'}, 5);
await clock.advance(15000);
expect(warnings().length === 3 &&
  warnings()[2] === 'workstream sync ws/2: history request gave up after 15000 ms',
  'a deadline expiry warns once', warnings().join(' | '));
await clock.advance(2000);
expect(network.pending('/history').length === 1, 'the stale retry is in flight');
await send(es, {type: 'history_resync'});
expect(warnings().length === 3, 'a supersession says nothing', warnings().join(' | '));
sync.destroy();
await flush();
expect(warnings().length === 3, 'a teardown says nothing', warnings().join(' | '));

// A fetch that ignores the abort and answers with a status after the deadline: the expiry
// already reported that request.
let answerLate;
const deaf = newSync({fetch: () => new Promise(resolve => { answerLate = resolve; })});
deaf.start();
await clock.advance(15000);
expect(warnings().length === 4, 'the expiry warns', warnings().join(' | '));
answerLate({ok: false, status: 503});
await flush();
expect(warnings().length === 4, 'a reply after the abort says nothing', warnings().join(' | '));
deaf.destroy();
"""
    )


def test_missed_fires_when_a_reopen_presents_no_cursor() -> None:
    """A reconnect with no cursor to present gets a fresh replay that cannot vouch for the gap;
    the first open of the instance is first paint, not a miss."""
    _run(
        r"""
const es = await live({messages: []});
expect(missed.length === 0, 'the first open is not a miss');
await send(es, {type: 'status'});
es.drop();
await clock.advance(1000);
es.open();
await flush();
expect(missed.length === 1, 'a cursorless reopen is a miss', missed.length);
"""
    )


def test_no_missed_when_a_reopen_presents_a_cursor() -> None:
    """Replay from a cursor covers the gap (the browser sends Last-Event-ID once an id arrived,
    and a manual connect presents the cursor), however long the gap."""
    _run(
        r"""
const es = await live({messages: []});
await send(es, {type: 'status'}, 4);
es.drop();
await clock.advance(120000);
es.open();
await flush();
visibility.hide();
await clock.advance(120000);
visibility.show();
await flush();
latest().open();
await flush();
expect(missed.length === 0, 'replay from a cursor vouches for the gap', missed.length);
"""
    )


def test_missed_fires_after_a_seeded_reload_that_adopts_a_cursor() -> None:
    """A non-first seeded render that adopted a cursor covers the transcript, not what the
    stream missed while down; its reconnect, at that cursor, adds nothing."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'history_resync'});
await network.reply('/history', {messages: [], cursor: 9, handoff_token: 'tok-2'});
expect(missed.length === 1, 'the cursor-adopting reload is a miss', missed.length);
latest().open();
await flush();
expect(missed.length === 1, 'its reconnect adds nothing', missed.length);
"""
    )


def test_missed_fires_once_after_a_seeded_reload_without_a_cursor() -> None:
    """A seeded render with no cursor reconnects cursorless, and that reopen is the one miss."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'history_resync'});
await network.reply('/history', {messages: [], handoff_token: 'tok-2'});
expect(missed.length === 0, 'not yet', missed.length);
latest().open();
await flush();
expect(missed.length === 1, 'the cursorless reopen is the miss', missed.length);
"""
    )


def test_lower_ids_are_not_a_gap_and_the_cursor_follows_the_last_one() -> None:
    """Ids can arrive lower without any reset (the server fans out after stamping): nothing is
    inferred from the numbers, and the cursor is simply the last id seen."""
    _run(
        r"""
const es = await live({messages: [], cursor: 2});
for (const id of [5, 3, 9]) await send(es, {type: 'status'}, id);
expect(missed.length === 0 && historyCount() === 1 && sync.status === 'live',
  'lower ids are not a gap');
expect(sync.inspect().cursor === '9', 'the cursor follows the last id', sync.inspect().cursor);
await send(es, {type: 'status'}, 7);
expect(sync.inspect().cursor === '7', 'even when it is lower', sync.inspect().cursor);
"""
    )


def test_a_frame_stamped_for_another_workstream_is_dropped_and_counted() -> None:
    """The server stamps every per-workstream frame with the stream's own id (a coordinator's
    child events carry the child in ``child_ws_id``), so a different ``ws_id`` is a server fault:
    the frame is counted and dropped before any routing, with the running total logged."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'child_ws_state', ws_id: WS, child_ws_id: 'c1'}, 4);
await send(es, {type: 'clear_ui', ws_id: 'ws/other'}, 5);
await send(es, {type: 'content', text: 'theirs', ws_id: 'ws/other'}, 6);
expect(dispatchedTypes() === 'child_ws_state',
  'only the frame stamped for this workstream dispatches', dispatchedTypes());
expect(historyCount() === 1 && sync.inspect().health.foreignFrames === 2,
  'the foreign frames are dropped before routing, and counted', historyCount());
expect(sync.inspect().cursor === '6',
  'their ids still moved the cursor: they arrived on this stream');
expect(logged.some(line => line.includes(
  'workstream sync ws/2: dropping a frame stamped for workstream ws/other (total 2)')),
  'the warning carries the running total', logged.join(' | '));
"""
    )


def test_a_spent_repair_budget_parks_with_the_prompt_and_a_manual_retry_recovers() -> None:
    """Four failed repair attempts park the repair (status ``parked``) with the prompt placed by
    the view; nothing automatic follows, and the prompt's retry recovers by hand."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
for (const delay of [0, 2000, 4000, 8000]) {
  await clock.advance(delay);
  await network.reply('/history', {detail: 'unavailable'}, {status: 503});
}
expect(sync.status === 'parked' && view.prompts.length === 1, 'parked with the prompt', sync.status);
await clock.advance(600000);
expect(historyCount() === 5 && sources().length === 1, 'nothing automatic while parked');
view.prompts[0].querySelector('.history-handoff-retry').click();
await flush();
expect(sync.status === 'repairing' && historyCount() === 6, 'the prompt retries by hand',
  sync.status);
await network.reply('/history', {messages: [], cursor: 9, handoff_token: 'tok-2'});
expect(sources().length === 2 && !view.prompts[0].isConnected,
  'a completed attempt connects and removes the prompt');
"""
    )


def test_the_stream_is_built_with_the_default_init() -> None:
    """Same-origin streams send cookies either way; no init dictionary is passed."""
    _run(
        r"""
await firstPaint();
expect(latest().init === undefined && latest().withCredentials === false, 'default init');
"""
    )


# --- carried unchanged -------------------------------------------------------------------------


def test_a_malformed_frame_is_dropped_but_advances_the_cursor() -> None:
    """The cursor is captured from the event before parsing, so a frame that is not a JSON object
    dispatches nothing yet still moves the cursor in step with the browser's Last-Event-ID."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'content', text: 'a'}, 4);
es.deliver('7', {id: 5});
await flush();
es.deliver('{not json', {id: 6});
await flush();
expect(dispatchedTypes() === 'content', 'malformed frames dispatch nothing', dispatchedTypes());
expect(sync.inspect().health.malformedFrames === 2 && sync.inspect().cursor === '6',
  'counted, and the cursor moved', JSON.stringify(sync.inspect().health));
expect(logged.some(line =>
  line.startsWith('workstream sync ws/2: dropping malformed SSE frame (total 2)')),
  'the malformed line carries the running total', logged.join(' | '));
visibility.hide();
visibility.show();
await flush();
expect(cursorOf(latest()) === '6', 'the next connect presents it', cursorOf(latest()));
latest().open();
await send(latest(), {type: 'content', text: 'b'}, 7);
expect(dispatchedTypes() === 'content,content', 'the stream still dispatches');
"""
    )


def test_a_transient_error_never_closes_the_stream() -> None:
    """Closing on an error would force CLOSED and defeat the browser's own reconnect, which
    resumes with Last-Event-ID."""
    _run(
        r"""
const es = await live();
es.drop();
await clock.advance(30000);
expect(!es.closeCalled && sources().length === 1, 'the same stream survives the error');
es.open();
await send(es, {type: 'content', text: 'after'}, 4);
expect(dispatchedTypes() === 'content' && sync.inspect().health.opens === 2,
  'the reconnected stream dispatches', JSON.stringify(sync.inspect().health));
"""
    )


def test_a_failed_truncated_resync_re_presents_the_arrival_cursor_keep_oldest() -> None:
    """The gap record keeps the oldest cursor and overrides the live one at every connect: a
    failed resync, a retry's envelope for the same gap, and a hide during the next jitter all
    present the cursor the first envelope arrived at."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('old')]});
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'state_change', state: 'idle'}, 6);
await send(es, {type: 'state_change', state: 'idle'}, 7);
await clock.advance(0);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(renders() === 1, 'a failed resync keeps the transcript');
const second = latest();
expect(cursorOf(second) === '5', 'it re-presents the arrival cursor', cursorOf(second));
second.open();
await flush();
await send(second, {type: 'replay_truncated'});
await send(second, {type: 'state_change', state: 'idle'}, 8);
visibility.hide();
visibility.show();
await flush();
expect(cursorOf(latest()) === '5', 'the same gap again keeps the oldest cursor', cursorOf(latest()));
const loads = historyCount();
await clock.advance(1000);
expect(historyCount() === loads, 'the hide cancelled the pending resync', historyCount() - loads);
"""
    )


def test_a_rendered_resync_without_a_history_cursor_reconnects_cursorless() -> None:
    """A reload drops the live cursor: after a node restart the reborn ring re-issued ids, and
    its earlier cursor would keep drawing the envelope (envelope, resync, envelope...)."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [], cursor: 3});
await send(es, {type: 'state_change', state: 'idle'}, 7);
await send(es, {type: 'replay_truncated'});
await clock.advance(0);
await network.reply('/history', {messages: [user('reborn')]});
expect(!params(latest()).has('last_event_id'), 'joined fresh, not at the stale cursor',
  latest().url);
"""
    )


def test_cursor_zero_is_presented() -> None:
    """Cursor 0 is a valid cursor, from /history and from a frame's id."""
    _run(
        r"""
const es = await live({messages: [], cursor: 0});
expect(cursorOf(es) === '0', 'the /history cursor 0 is presented', es.url);
await send(es, {type: 'status'}, 0);
visibility.hide();
visibility.show();
await flush();
expect(cursorOf(latest()) === '0', 'an id of 0 is presented', latest().url);
"""
    )


def test_the_handoff_token_rides_only_the_first_stream_built() -> None:
    """A hidden first paint keeps its token for the show-edge connect, where it goes last; no
    later stream presents it."""
    _run(
        r"""
visibility.hide();
sync.start();
await flush();
await network.reply('/history', {messages: [], cursor: 4, handoff_token: 'tok-1'});
expect(sources().length === 0 && sync.status === 'paused', 'a hidden first paint defers');
visibility.show();
await flush();
const first = latest();
expect([...params(first).keys()].at(-1) === 'history_token' &&
  params(first).get('history_token') === 'tok-1', 'the deferred connect presents the token last',
  first.url);
first.open();
await flush();
visibility.hide();
visibility.show();
await flush();
expect(!params(latest()).has('history_token'), 'a later stream never reuses it', latest().url);
"""
    )


def test_a_stream_that_cannot_be_built_keeps_the_token_and_counts_a_strike() -> None:
    """The token is consumed only once a source exists; a constructor that throws is handled like
    a refused connection (a strike and a backoff), so the strike budget bounds it."""
    _run(
        r"""
pinRandom(0);
let throwsLeft = 1;
const Real = EventSource;
globalThis.EventSource = class extends Real {
  constructor(...args) {
    if (throwsLeft > 0) {
      throwsLeft -= 1;
      throw new SyntaxError('bad url');
    }
    super(...args);
  }
};
sync.start();
await flush();
await network.reply('/history', {messages: [], cursor: 4, handoff_token: 'tok-1'});
expect(sources().length === 0 && sync.status === 'backoff', 'counted like a refusal',
  sync.status);
await clock.advance(1000);
expect(sources().length === 1 && params(latest()).get('history_token') === 'tok-1',
  'the token waited for a stream that was built', latest() && latest().url);
"""
    )


def test_the_degraded_cooldown_escalates_and_resets_after_a_quiet_gap() -> None:
    """15 s, 30 s, 60 s for trips in a row; after more than five quiet minutes, 15 s again (the
    ladder is keyed off the last trip, not the overflow window)."""
    _run(
        r"""
const es = await live();
const trip = async () => {
  for (let i = 0; i < 3; i += 1) await send(latest(), {type: 'stream_overflow'});
  expect(sync.status === 'degraded', 'tripped', sync.status);
};
const cooldown = async () => {
  const count = sources().length;
  const start = clock.elapsed;
  while (sources().length === count) await clock.advance(1000);
  latest().open();
  await flush();
  return clock.elapsed - start;
};
const steps = [];
for (let i = 0; i < 3; i += 1) {
  await trip();
  steps.push(await cooldown());
}
await clock.advance(300001);
await trip();
steps.push(await cooldown());
expect(steps.join(',') === '15000,30000,60000,15000', 'the ladder', steps.join(','));
"""
    )


def test_a_truncated_resync_waits_out_its_jitter_then_reloads_at_the_history_cursor() -> None:
    """The resync waits a random spread (herd protection) with the stream still up, then closes
    it, loads /history and reconnects at the cursor it returned."""
    _run(
        r"""
pinRandom(0.5);
const es = await live({messages: [user('old')]});
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'state_change', state: 'idle'}, 6);
await clock.advance(4999);
expect(historyCount() === 1 && !es.closeCalled, 'the stream stays up during the jitter');
await clock.advance(1);
expect(network.pending('/history').length === 1 && es.closeCalled && sync.status === 'loading',
  'then it closes and loads', sync.status);
await network.reply('/history', {messages: [user('new')], cursor: 9});
expect(sources().length === 2 && cursorOf(latest()) === '9', 'and adopts the /history cursor',
  latest().url);
expect(sync.inspect().health.truncatedGaps === 1, 'one gap counted');
"""
    )


def test_truncated_resyncs_count_toward_the_overflow_ladder() -> None:
    """Truncated resyncs share the churn window with overflow closes; the trip that results
    skips the resync, whose envelope returns after the cooldown."""
    _run(
        r"""
pinRandom(0.9);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
for (let i = 0; i < 3; i += 1) await send(es, {type: 'replay_truncated'});
expect(sync.status === 'degraded' && es.closeCalled, 'the third resync decision trips', sync.status);
await clock.advance(9000);
expect(historyCount() === 1, 'the trip cancelled the pending resync', historyCount());
expect(sync.inspect().health.truncatedGaps === 3, 'each decision counted the gap');
"""
    )


def test_the_idle_edge_resync_runs_at_once_and_is_not_churn() -> None:
    """A truncation that arrives mid-reply resyncs at the next idle edge, without the jitter and
    without feeding the overflow ladder."""
    _run(
        r"""
pinRandom(0.9);
const es = await live();
await send(es, {type: 'content', text: 'reply'}, 5);
await send(es, {type: 'replay_truncated'});
await clock.advance(20000);
expect(historyCount() === 1, 'no resync while the reply streams');
await send(es, {type: 'state_change', state: 'idle'}, 6);
expect(network.pending('/history').length === 1 && sync.status === 'loading',
  'the idle edge reloads at once', sync.status);
expect(sync.inspect().health.truncatedGaps === 1, 'the gap is counted');
await network.reply('/history', {messages: [], cursor: 9});
const next = latest();
next.open();
await flush();
await send(next, {type: 'stream_overflow'});
await send(next, {type: 'stream_overflow'});
expect(sync.status === 'live', 'two overflows do not trip: the resync was not churn', sync.status);
"""
    )


def test_the_stale_retry_runs_once_after_a_failed_clear_ui_refetch() -> None:
    """One bounded retry per clear_ui, at the 2 s floor, seedless; its own failure arms nothing,
    and the latch stays set."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('kept')]});
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(sync.historyStale && historyCount() === 2, 'the latch survives the failure');
await clock.advance(1999);
expect(historyCount() === 2, 'the retry waits for its floor');
await clock.advance(1);
expect(historyCount() === 3 && sync.inspect().quiesced, 'one seedless retry');
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
await clock.advance(120000);
expect(historyCount() === 3 && sync.historyStale, 'bounded: its failure arms nothing');
expect(renders() === 1 && !es.closeCalled, 'the transcript and the stream stay');
"""
    )


def test_the_stale_retry_skips_a_stream_that_is_not_open() -> None:
    """A reconnecting stream has a frozen cursor; the retry stands aside and the latch waits for
    the idle backstop."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
es.drop();
await clock.advance(2000);
expect(historyCount() === 2, 'no retry against a reconnecting stream', historyCount());
"""
    )


def test_the_stale_retry_skips_while_a_turn_runs() -> None:
    """The retry fires only turn-free; a running turn leaves the heal to its settle."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
await send(es, {type: 'state_change', state: 'thinking'}, 5);
await clock.advance(2000);
expect(historyCount() === 2, 'no retry under a running turn', historyCount());
"""
    )


def test_the_stale_retry_yields_to_a_refetch_in_flight() -> None:
    """The retry never fires into a seedless refetch's queue."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
await clock.advance(1000);
await send(es, {type: 'clear_ui'}, 5);
await clock.advance(1000);
expect(historyCount() === 3, 'the retry stood aside', historyCount());
"""
    )


def test_a_transport_teardown_keeps_the_stale_retry() -> None:
    """The retry is a REST heal, not transport state: a hide and show in between leave it armed,
    and it fires on the new stream."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [], cursor: 3});
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
await clock.advance(1000);
visibility.hide();
visibility.show();
await flush();
latest().open();
await flush();
await clock.advance(1000);
expect(historyCount() === 3, 'the retry survived the redial', historyCount());
"""
    )


def test_a_transport_teardown_keeps_the_queue() -> None:
    """Only the refetch that armed the queue releases it: a hide during the refetch leaves the
    held events in place, and the declined refetch flushes them."""
    _run(
        r"""
const es = await live({messages: [user('kept')]});
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'content', text: 'held'}, 5);
visibility.hide();
expect(sync.inspect().quiesced, 'the hide leaves the queue armed');
await network.reply('/history', {messages: [user('new')]});
expect(outcomes() === 'declined' && dispatchedTypes() === 'content', 'declined, then flushed',
  outcomes() + ' ' + dispatchedTypes());
"""
    )


def test_a_history_resync_repairs_with_the_stream_closed_until_a_render_completes() -> None:
    """The repair keeps the stream closed across failed attempts (no cursorless or tokenless
    stream until a fresh proof renders), backs off, and connects with the new token."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
expect(es.closeCalled && sync.status === 'repairing' && sync.historyStale,
  'the repair closes the stream and sets the latch', sync.status);
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
expect(sources().length === 1 && sync.status === 'repairing', 'a failed attempt opens nothing');
await clock.advance(1999);
expect(network.pending('/history').length === 0, 'the next attempt waits its backoff');
await clock.advance(1);
expect(network.pending('/history').length === 1, 'then runs');
await network.reply('/history', {messages: [user('fresh')], cursor: 9, handoff_token: 'tok-2'});
expect(sources().length === 2 && params(latest()).get('history_token') === 'tok-2' &&
  cursorOf(latest()) === '9', 'connects with the new token and cursor', latest().url);
expect(!sync.historyStale, 'the render cleared the latch');
"""
    )


def test_a_completed_tokenless_repair_render_downgrades_to_a_cursorless_connect() -> None:
    """A completed render without a token is the server's deliberate cold read: the repair ends
    and the connect is the tokenless bootstrap."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'history_resync'});
await network.reply('/history', {messages: [user('cold')]});
expect(sources().length === 2 && sync.status === 'connecting', 'the repair ended', sync.status);
expect(!params(latest()).has('history_token') && !params(latest()).has('last_event_id'),
  'cursorless and tokenless', latest().url);
"""
    )


# --- event routing ---------------------------------------------------------------------------


def test_a_clear_ui_held_in_the_queue_runs_after_the_flush_and_holds_the_rest() -> None:
    """The flush goes back through the router: a queued clear_ui starts its own refetch and the
    events behind it wait for that render."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'status', n: 1}, 5);
await send(es, {type: 'clear_ui'}, 6);
await send(es, {type: 'status', n: 2}, 7);
expect(historyCount() === 2, 'the second clear_ui waits in the queue');
await network.reply('/history', {messages: [user('v2')]});
expect(view.dispatched.map(event => event.n).join(',') === '1',
  'the flush dispatched up to the queued clear_ui', view.dispatched.map(event => event.n).join(','));
expect(historyCount() === 3 && sync.inspect().quiesced, 'which refetches and holds the rest');
await network.reply('/history', {messages: [user('v3')]});
expect(view.dispatched.map(event => event.n).join(',') === '1,2', 'the rest after the second render');
expect(outcomes() === 'rendered,rendered', 'both settled', outcomes());
"""
    )


def test_replay_truncated_held_in_the_queue_keeps_the_cursor_it_arrived_at() -> None:
    """The gap is recorded when the envelope arrives, not when the queue flushes: later frames
    have moved the live cursor on by then."""
    _run(
        r"""
pinRandom(0.5);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'clear_ui'});
es.drop();
await clock.advance(1000);
es.open();
await flush();
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'status'}, 6);
await send(es, {type: 'status'}, 7);
expect(sync.inspect().cursor === '7', 'the live cursor moved on');
await network.reply('/history', {messages: []});
expect(outcomes() === 'declined', 'the reconnect declined the render', outcomes());
visibility.hide();
visibility.show();
await flush();
expect(cursorOf(latest()) === '5', 'the gap cursor is the arrival one', cursorOf(latest()));
"""
    )


def test_replay_truncated_held_in_the_queue_defers_behind_a_turn_that_started_ahead_of_it() -> None:
    """The resync-now-or-at-idle decision is made in order: a reply held ahead of the envelope
    is streaming by the time the envelope is decided, so the resync waits for the idle edge."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'clear_ui'});
await send(es, {type: 'content', text: 'turn'}, 6);
es.drop();
await clock.advance(1000);
es.open();
await flush();
await send(es, {type: 'replay_truncated'});
await network.reply('/history', {messages: []});
expect(view.live.streaming, 'the held reply dispatched first');
await clock.advance(20000);
expect(historyCount() === 2, 'no resync under the streaming reply', historyCount());
await send(es, {type: 'state_change', state: 'idle'}, 7);
expect(network.pending('/history').length === 1 && sync.status === 'loading',
  'the idle edge runs the deferred resync', sync.status);
"""
    )


def test_stream_overflow_acts_on_arrival_while_quiesced() -> None:
    """Overflow accounting is never queued behind a render: the trip happens on arrival, the
    queue stays armed, and the declined refetch flushes the content it held."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'status'}, 5);
for (let i = 0; i < 3; i += 1) await send(es, {type: 'stream_overflow'});
expect(sync.status === 'degraded' && es.closeCalled, 'the third overflow trips on arrival',
  sync.status);
expect(sync.inspect().quiesced && view.dispatched.length === 0, 'with the queue still armed');
await network.reply('/history', {messages: []});
expect(outcomes() === 'declined' && dispatchedTypes() === 'status',
  'the overflows were never queued', dispatchedTypes());
"""
    )


def test_history_resync_acts_on_arrival_and_supersedes_a_refetch() -> None:
    """A history_resync during a seedless refetch starts the repair at once: the refetch is
    aborted and settles superseded, and its queue is dropped (the repair snapshot covers it)."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
const refetch = network.take('/history');
await send(es, {type: 'status'}, 5);
await send(es, {type: 'history_resync'});
expect(sync.status === 'repairing' && es.closeCalled, 'the repair starts on arrival', sync.status);
expect(refetch.state === 'aborted', 'the refetch is aborted', refetch.state);
expect(outcomes() === 'superseded', 'its clear_ui settles superseded', outcomes());
await network.reply('/history', {messages: [user('v2')], cursor: 9, handoff_token: 'tok-2'});
expect(view.dispatched.length === 0, 'the superseded queue was dropped');
expect(sources().length === 2 && params(latest()).get('history_token') === 'tok-2',
  'the repair connects');
"""
    )


# --- interleavings -----------------------------------------------------------------------------


def test_a_throwing_live_state_still_flushes_the_queue() -> None:
    """The render gate runs inside the refetch's try, so a throwing ``liveState`` cannot wedge
    the queue: the clear_ui settles ``threw`` and the held events still dispatch."""
    _run(
        r"""
const es = await live({messages: [user('kept')]});
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'status'}, 5);
const liveState = view.liveState;
view.liveState = () => {
  view.liveState = liveState;
  throw new Error('view bug');
};
await network.reply('/history', {messages: [user('new')]});
expect(outcomes() === 'threw' && settled[0].error.message === 'view bug', 'threw', outcomes());
expect(dispatchedTypes() === 'status' && !sync.inspect().quiesced, 'the queue still flushed');
"""
    )


def test_a_server_error_frame_on_a_live_stream_then_a_trip_opens_nothing_early() -> None:
    """A proxied upstream failure arrives as an ``error`` frame on an open stream (still
    ``reconnecting``, not a refusal); a trip after it tears the stream down, and nothing opens
    before the cooldown ends."""
    _run(
        r"""
const es = await live();
es.deliver('Upstream returned status 404', {event: 'error'});
await flush();
expect(sync.status === 'reconnecting', 'an error frame on an open stream is reconnecting',
  sync.status);
for (let i = 0; i < 3; i += 1) await send(es, {type: 'stream_overflow'});
expect(sync.status === 'degraded', 'the trip', sync.status);
await clock.advance(15000 - 1);
expect(sources().length === 1 && sync.status === 'degraded', 'nothing opens during the cooldown');
await clock.advance(1);
expect(sources().length === 2, 'the cooldown reconnects', sources().length);
"""
    )


# --- reset-settled ----------------------------------------------------------------------------


def test_reset_settled_reports_rendered_exactly_once() -> None:
    """One clear_ui, one ``reset-settled``."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {messages: [user('v2')]});
await clock.advance(60000);
expect(JSON.stringify(settled) === '[{"outcome":"rendered"}]', 'rendered once',
  JSON.stringify(settled));
"""
    )


def test_reset_settled_reports_declined() -> None:
    """A good payload the gate refused is ``declined``, not ``failed``."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'content', text: 'streaming'}, 4);
await send(es, {type: 'clear_ui'}, 5);
await network.reply('/history', {messages: [user('v2')]});
expect(JSON.stringify(settled) === '[{"outcome":"declined"}]', 'declined', JSON.stringify(settled));
"""
    )


def test_reset_settled_reports_failed() -> None:
    """A network failure is ``failed``; the pane still sends a pending edit (the rewind already
    committed server-side)."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
await network.fail('/history');
expect(JSON.stringify(settled) === '[{"outcome":"failed"}]', 'failed', JSON.stringify(settled));
"""
    )


def test_reset_settled_reports_superseded_when_a_resync_replaces_the_refetch() -> None:
    """A jittered resync that reloads while a clear_ui refetch waits supersedes it; the
    clear_ui still settles, once."""
    _run(
        r"""
pinRandom(0.5);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await clock.advance(1000);
await send(es, {type: 'clear_ui'}, 6);
await clock.advance(4000);
expect(sync.status === 'loading', 'the resync reloads', sync.status);
expect(JSON.stringify(settled) === '[{"outcome":"superseded"}]', 'superseded',
  JSON.stringify(settled));
await clock.advance(60000);
expect(settled.length === 1, 'exactly once', settled.length);
"""
    )


def test_reset_settled_reports_threw_with_the_error() -> None:
    """A render throw is ``threw`` with the error, so the pane drops the edit and says why; the
    queue still releases and no retry is armed."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
view.renderHistory = () => { throw new Error('render bug'); };
await network.reply('/history', {messages: [user('v2')]});
expect(settled.length === 1 && settled[0].outcome === 'threw' &&
  settled[0].error.message === 'render bug', 'threw with the error', JSON.stringify(settled));
expect(!sync.inspect().quiesced, 'the queue released');
await clock.advance(60000);
expect(historyCount() === 2, 'no retry after a throw', historyCount());
"""
    )


def test_reset_settled_still_fires_after_destroy() -> None:
    """Destroying the pane mid-refetch still settles the clear_ui (a committed rewind's edit must
    still send), while nothing renders, flushes or stays armed."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'content', text: 'held'}, 5);
sync.destroy();
await flush();
expect(JSON.stringify(settled) === '[{"outcome":"superseded"}]', 'settled after destroy',
  JSON.stringify(settled));
expect(view.dispatched.length === 0 && renders() === 1, 'nothing flushed or rendered');
expectReleased('destroyed mid-refetch');
"""
    )


# --- destroy and dead at each await point ----------------------------------------------------------


def test_destroy_during_the_first_history_load() -> None:
    """The request is aborted, a late reply is discarded, and nothing renders, connects or stays
    armed."""
    _run(
        r"""
sync.start();
await flush();
const request = network.take('/history');
sync.destroy();
await flush();
expect(request.state === 'aborted', 'the request is aborted', request.state);
expectReleased('destroyed during first paint');
expect(request.reply({messages: [user('late')], cursor: 3}) === false, 'a late reply is discarded');
await clock.advance(60000);
expect(sources().length === 0 && renders() === 0 && reloaded.length === 0, 'nothing after destroy');
"""
    )


def test_destroy_between_the_history_reply_and_its_render() -> None:
    """A reply that already arrived but has not been processed when destroy runs renders
    nothing."""
    _run(
        r"""
sync.start();
await flush();
network.take('/history').reply({messages: [user('q')], cursor: 3});
sync.destroy();
await flush();
expectReleased('destroyed after the reply');
await clock.advance(60000);
expect(sources().length === 0 && renders() === 0 && reloaded.length === 0, 'nothing after destroy');
"""
    )


def test_destroy_during_a_resync_load() -> None:
    """The truncated resync's load is abandoned: no render, no reconnect."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 5);
await send(es, {type: 'replay_truncated'});
await clock.advance(0);
const request = network.take('/history');
sync.destroy();
await flush();
expectReleased('destroyed during a resync');
request.reply({messages: [], cursor: 9});
await clock.advance(60000);
expect(sources().length === 1 && renders() === 1, 'no render and no reconnect');
"""
    )


def test_destroy_during_a_repair_attempt() -> None:
    """The repair attempt is abandoned with its backoff and prompt: no render, no stream."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
const request = network.take('/history');
sync.destroy();
await flush();
expectReleased('destroyed during a repair');
request.reply({messages: [], cursor: 9, handoff_token: 'tok-2'});
await clock.advance(600000);
expect(sources().length === 1 && renders() === 1 && view.prompts.length === 0,
  'no render, no stream, no prompt');
"""
    )


def test_destroy_between_repair_attempts_leaves_no_backoff_armed() -> None:
    """Destroy clears the repair with the rest: its backoff timer does not outlive the
    instance."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
expect(clock.pending().length === 1, 'the next attempt is scheduled', clock.pending().length);
sync.destroy();
await flush();
expectReleased('destroyed between repair attempts');
"""
    )


def test_destroy_while_parked_removes_the_prompt() -> None:
    """A parked repair's prompt belongs to the instance and leaves with it."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
for (const delay of [0, 2000, 4000, 8000]) {
  await clock.advance(delay);
  await network.reply('/history', {detail: 'unavailable'}, {status: 503});
}
expect(sync.status === 'parked' && view.prompts[0].isConnected, 'parked with the prompt');
sync.destroy();
expect(!view.prompts[0].isConnected, 'destroy removes the prompt');
expectReleased('destroyed while parked');
"""
    )


def test_destroy_cancels_an_armed_stale_retry() -> None:
    """Dead, not merely inert: a stale retry armed when the pane is destroyed is cancelled, not
    left to fire into a destroyed instance."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(clock.pending().length === 1, 'the retry is armed', clock.pending().length);
sync.destroy();
expectReleased('destroyed with a retry armed');
"""
    )


def test_destroy_during_a_backstop_refetch() -> None:
    """The idle backstop's refetch is abandoned: no render, no flush."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await latchAfterTwoFailures(es);
await send(es, {type: 'state_change', state: 'idle'}, 5);
expect(network.pending('/history').length === 1, 'the backstop refetches');
const before = renders();
sync.destroy();
await flush();
expectReleased('destroyed during the backstop');
await clock.advance(60000);
expect(renders() === before, 'no render after destroy');
"""
    )


def test_destroy_during_a_stale_retry_refetch() -> None:
    """The stale retry's refetch is abandoned: no render, nothing re-armed."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
await clock.advance(2000);
expect(network.pending('/history').length === 1, 'the retry refetches');
sync.destroy();
await flush();
expectReleased('destroyed during the retry');
await clock.advance(60000);
expect(renders() === 1, 'no render after destroy');
"""
    )


def test_dead_during_a_seedless_refetch() -> None:
    """A chain of queued clear_ui refetches outlives the strike budget: the seventh refusal
    lands while the third refetch waits (its deadline grew to 60 s), and dead makes it inert
    exactly as destroy would; the two clear_ui still queued settle superseded.  (A seeded reload
    cannot be in flight at a strike: it holds the stream down, so no error arrives.)"""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('kept')]});
for (let id = 4; id < 9; id += 1) await send(es, {type: 'clear_ui'}, id);
expect(historyCount() === 2, 'one refetch in flight, four clear_ui queued behind it');
es.drop();
es.fail();
await refuseEveryRedial();
expect(sync.status === 'dead' && historyCount() === 4, 'dead during the third refetch',
  sync.status + ' ' + historyCount());
const last = network.seen('/history').at(-1);
expect(last.state === 'aborted', 'dead aborts the refetch', last.state);
expect(outcomes() === 'failed,failed,superseded,superseded,superseded',
  'every clear_ui settled once', outcomes());
expect(renders() === 1, 'nothing rendered');
expectReleased('dead during a refetch');
"""
    )


# --- the history deadline, idle at dispatch, login, dropped clear_ui, the latch, phases -----------


def test_a_history_slower_than_the_first_deadline_renders_on_a_later_attempt() -> None:
    """The server finishes a reconstruction even when the client aborts, so a retry that cannot
    outlast it only multiplies work: an expiry doubles the next bound, and a /history slower
    than 15 s renders on the next attempt."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
const first = network.take('/history');
await clock.advance(14999);
expect(first.state === 'pending', 'the first attempt waits 15 s', first.state);
await clock.advance(1);
expect(first.state === 'aborted' && outcomes() === 'failed', 'and expires',
  first.state + ' ' + outcomes());
await clock.advance(2000);
const second = network.take('/history');
await clock.advance(20000);
expect(second.state === 'pending', 'the next attempt outlasts the first deadline', second.state);
await network.reply('/history', {messages: [user('v2')]});
expect(renders() === 2 && !sync.historyStale, 'the slow history renders on the later attempt');
"""
    )


def test_a_payload_resets_the_history_deadline() -> None:
    """After any payload the bound is back to 15 s."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
await clock.advance(15000);
await clock.advance(2000);
await network.reply('/history', {messages: [user('v2')]});
expect(renders() === 2, 'the retry rendered');
await send(es, {type: 'clear_ui'}, 5);
const third = network.take('/history');
await clock.advance(15000);
expect(third.state === 'aborted', 'the payload reset the deadline to 15 s', third.state);
"""
    )


def test_a_superseded_history_request_does_not_grow_the_deadline() -> None:
    """Only the deadline's own expiry grows it: an abort for supersession leaves it at 15 s.  The
    repair's first attempt is created before the superseded request settles, so the bound is
    read off its second attempt, after a failure that neither grows nor resets it."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
const refetch = network.take('/history');
await send(es, {type: 'history_resync'});
expect(refetch.state === 'aborted' && outcomes() === 'superseded', 'the repair supersedes it',
  refetch.state);
await network.reply('/history', {detail: 'unavailable'}, {status: 503});
await clock.advance(2000);
const second = network.take('/history');
await clock.advance(14999);
expect(second.state === 'pending', 'the next repair attempt waits 15 s', second.state);
await clock.advance(1);
expect(second.state === 'aborted', 'and no longer: the supersession grew nothing', second.state);
"""
    )


def test_consecutive_expiries_grow_the_deadline_to_its_cap() -> None:
    """Expiries in a row double the bound, 15, 30, 60, 120 s, and it stays at 120 s; the
    repair's four attempts take them in turn and a manual retry from the prompt inherits the
    cap."""
    _run(
        r"""
pinRandom(0);
const es = await live();
await send(es, {type: 'history_resync'});
const lifetime = async request => {
  const started = clock.elapsed;
  while (request.state === 'pending') await clock.advance(1000);
  return clock.elapsed - started;
};
const lifetimes = [];
for (let attempt = 0; attempt < 4; attempt += 1) {
  while (network.pending('/history').length === 0) await clock.advance(1000);
  lifetimes.push(await lifetime(network.take('/history')));
}
expect(lifetimes.join(',') === '15000,30000,60000,120000', 'the bound doubles per expiry',
  lifetimes.join(','));
expect(sync.status === 'parked', 'four attempts park the repair', sync.status);
view.prompts[0].querySelector('.history-handoff-retry').click();
await flush();
expect(await lifetime(network.take('/history')) === 120000, 'the bound stops at its cap');
"""
    )


def test_retry_promotes_and_the_regenerated_turn_arrives_by_replay_once() -> None:
    """Retry: the server truncated, published clear_ui and re-dispatched the turn, whose leading
    frames queue before /history answers, trimmed, with a cursor.  The promoted render shows the
    trimmed history (the replaced reply is gone), the queue is dropped, and the stream is rebuilt
    at the cursor, whose replay delivers the turn's rows once."""
    _run(
        r"""
const es = await live({messages: [user('ask'), {role: 'assistant', content: 'first reply'}]});
await send(es, {type: 'state_change', state: 'idle'}, 4);
await send(es, {type: 'clear_ui'}, 5);
await send(es, {type: 'state_change', state: 'running'}, 6);
await send(es, {type: 'tool_info', items: [{call_id: 'c1'}]}, 7);
await network.reply('/history', {messages: [user('ask')], cursor: 5});
expect(outcomes() === 'rendered' && view.rendered.at(-1).length === 1,
  'the trimmed history renders and the replaced reply is gone', outcomes());
expect(dispatchedTypes() === 'state_change', 'the queued frames are dropped, not played',
  dispatchedTypes());
const next = latest();
expect(next !== es && cursorOf(next) === '5', 'the stream is rebuilt presenting the cursor',
  next.url);
next.open();
await flush();
await send(next, {type: 'state_change', state: 'running'}, 6);
await send(next, {type: 'tool_info', items: [{call_id: 'c1'}]}, 7);
const rows = view.dispatched.filter(event => event.type === 'tool_info').length;
expect(rows === 1, 'the turn arrives by replay, once', String(rows));
"""
    )


def test_a_bootstrap_clear_ui_mid_batch_is_promoted_and_loses_no_rows() -> None:
    """The server's bootstrap clear_ui is a fresh connection's first frame, even while a tool
    batch runs, so the batch's earlier rows are in neither the trimmed /history nor the queue.
    The promotion reconnects from the cursor, and the replay delivers them."""
    _run(
        r"""
sync.start();
await flush();
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
const es = latest();
es.open();
await flush();
await send(es, {type: 'clear_ui', reason: 'tokenless_history_bootstrap'});
await send(es, {type: 'tool_output_chunk', call_id: 'c1', text: 'line 3'}, 10);
await network.reply('/history', {messages: [user('ask')], cursor: 7});
expect(outcomes() === 'rendered', 'the bootstrap refetch renders', outcomes());
const next = latest();
expect(next !== es && cursorOf(next) === '7', 'and reconnects from the cursor', next.url);
next.open();
await flush();
for (const [id, event] of [[8, {type: 'state_change', state: 'running'}],
  [9, {type: 'tool_info', items: [{call_id: 'c1'}]}],
  [10, {type: 'tool_output_chunk', call_id: 'c1', text: 'line 3'}]]) {
  await send(next, event, id);
}
expect(dispatchedTypes() === 'state_change,tool_info,tool_output_chunk',
  'the batch from its start, once each', dispatchedTypes());
"""
    )


def test_an_earlier_connections_queued_idle_cannot_mislead_a_later_bootstrap() -> None:
    """An earlier connection's idle, queued behind a refetch and flushed after a later
    connection opened, cannot make that connection's bootstrap refetch lose the batch it carries:
    no per-connection state decides it, and the cursor promotes the render."""
    _run(
        r"""
sync.start();
await flush();
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
const es = latest();
es.open();
await flush();
await send(es, {type: 'clear_ui', reason: 'tokenless_history_bootstrap'});
await send(es, {type: 'state_change', state: 'idle'});
es.drop();
await flush();
es.open();
await flush();
await send(es, {type: 'clear_ui', reason: 'tokenless_history_bootstrap'});
await send(es, {type: 'state_change', state: 'running'});
await send(es, {type: 'tool_info', items: [{call_id: 'c1'}]});
await network.reply('/history', {messages: [user('ask')]});
await network.reply('/history', {messages: [user('ask')], cursor: 9});
expect(outcomes() === 'declined,rendered', 'the first refetch is declined (a new connection), the '
  + 'second promoted', outcomes());
expect(sources().length === 2 && cursorOf(latest()) === '9', 'the stream is rebuilt at the cursor',
  latest().url);
expect(!dispatchedTypes().includes('tool_info'), 'the queued batch frames wait for the replay',
  dispatchedTypes());
"""
    )


def test_a_promoted_render_that_throws_still_reconnects() -> None:
    """The transport is already down when the render runs, so a render throw still connects, at
    the adopted cursor and without a token, and the clear_ui settles ``threw``."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
view.renderHistory = () => { throw new Error('render bug'); };
await network.reply('/history', {messages: [user('v2')], cursor: 3, handoff_token: 'tok'});
expect(outcomes() === 'threw' && settled[0].error.message === 'render bug', 'the clear_ui threw',
  outcomes());
const next = latest();
expect(next !== es && cursorOf(next) === '3' && params(next).get('history_token') === null,
  'the connect still follows, at the cursor, without a token', next.url);
expect(sync.status === 'connecting', 'connecting', sync.status);
"""
    )


def test_a_clear_ui_queued_at_a_promotion_settles_once_on_a_connecting_instance() -> None:
    """The promotion drops the queue last: a clear_ui held there settles ``superseded`` once,
    with the instance already connecting (a retryNow() from its listener builds nothing), and
    fetches nothing of its own; the promoted one then settles ``rendered``."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'clear_ui'}, 5);
const seen = [];
sync.on('reset-settled', payload => {
  seen.push(payload.outcome + ':' + sync.status);
  sync.retryNow();
});
await network.reply('/history', {messages: [user('v2')], cursor: 4});
expect(outcomes() === 'superseded,rendered', 'each clear_ui settles once', outcomes());
expect(seen[0] === 'superseded:connecting', 'the dropped one settles on a connecting instance',
  seen.join(','));
expect(sources().length === 2 && historyCount() === 2, 'nothing more was built or fetched',
  sources().length + ' ' + historyCount());
"""
    )


def test_a_promotion_while_hidden_connects_on_the_show_edge() -> None:
    """The connect chokepoint handles a tab hidden by the time the promoted render ran: the
    phase is paused and the show edge connects at the adopted cursor."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
const render = view.renderHistory;
view.renderHistory = messages => {
  render(messages);
  visibility.hide();
};
await network.reply('/history', {messages: [user('v2')], cursor: 3});
view.renderHistory = render;
expect(outcomes() === 'rendered' && sync.status === 'paused' && sources().length === 1,
  'the render lands and the connect waits for the show edge', sync.status);
visibility.show();
await flush();
expect(sources().length === 2 && cursorOf(latest()) === '3', 'the show edge connects at the cursor',
  latest().url);
"""
    )


def test_the_stale_retry_and_the_backstop_promote_a_cursor_too() -> None:
    """One rule for every seedless refetch: the stale retry's and the backstop's responses with a
    cursor are promoted like clear_ui's."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
await clock.advance(2000);
await network.reply('/history', {messages: [user('v2')], cursor: 4});
expect(renders() === 2 && !sync.historyStale && sources().length === 2 && cursorOf(latest()) === '4',
  'the stale retry is promoted', latest().url);
const second = latest();
second.open();
await flush();
await latchAfterTwoFailures(second);
await send(second, {type: 'state_change', state: 'idle'}, 6);
expect(historyCount() === 6, 'the idle edge fires the backstop', String(historyCount()));
await network.reply('/history', {messages: [user('v3')], cursor: 6});
expect(renders() === 3 && !sync.historyStale && sources().length === 3 && cursorOf(latest()) === '6',
  'the backstop is promoted', latest().url);
"""
    )


def test_a_reload_drops_the_queue_after_its_phase_change() -> None:
    """A reload drops the queue after it changed phase: a reset-settled listener for a dropped
    clear_ui finds a loading instance, and its retryNow() builds no stream mid-load."""
    _run(
        r"""
pinRandom(0.05);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 4);
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'clear_ui'}, 5);
await send(es, {type: 'clear_ui'}, 6);
const seen = [];
sync.on('reset-settled', payload => {
  if (payload.outcome !== 'superseded') return;
  seen.push(sync.status);
  sync.retryNow();
});
es.drop();
es.fail();
expect(sync.status === 'backoff', 'refused', sync.status);
await clock.advance(600);
expect(sync.status === 'loading', 'the jittered resync reloads', sync.status);
expect(seen[0] === 'loading', 'the dropped clear_ui settles on a loading instance', seen.join(','));
expect(sources().length === 1 && sync.inspect().source === null, 'no stream is built mid-load',
  String(sources().length));
"""
    )


def test_retry_now_heals_a_latched_live_stream_through_the_stale_retry() -> None:
    """A login on a live stream whose transcript stayed latched (its refetches failed while
    signed out) arms the one stale retry: a seedless refetch on the open, idle stream, no reload
    and no close."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('v1')]});
await latchAfterTwoFailures(es);
const loads = historyCount();
sync.retryNow();
await clock.advance(1999);
expect(historyCount() === loads, 'the retry waits for its floor');
await clock.advance(1);
expect(historyCount() === loads + 1, 'one seedless refetch', historyCount() - loads);
await network.reply('/history', {messages: [user('v2')]});
expect(!sync.historyStale && !es.closeCalled && sources().length === 1,
  'healed without touching the stream');
"""
    )


def test_a_reload_settles_each_queued_clear_ui_once() -> None:
    """A reload drops the queue; a clear_ui held in it still settles (superseded), so a pending
    edit is never stranded and reset-settled stays exactly once per clear_ui."""
    _run(
        r"""
pinRandom(0.5);
const es = await live();
await send(es, {type: 'state_change', state: 'idle'}, 4);
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'clear_ui'}, 5);
await send(es, {type: 'clear_ui'}, 6);
await clock.advance(5000);
expect(sync.status === 'loading', 'the resync reloads', sync.status);
expect(outcomes() === 'superseded,superseded', 'both clear_ui settle', outcomes());
await clock.advance(60000);
expect(settled.length === 2, 'exactly once each', String(settled.length));
"""
    )


def test_destroy_settles_a_queued_clear_ui() -> None:
    """Destroy settles the clear_ui queued behind the one in flight inside the teardown itself,
    and the one in flight once its aborted request returns."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'clear_ui'}, 4);
await send(es, {type: 'clear_ui'}, 5);
sync.destroy();
expect(outcomes() === 'superseded', 'the queued clear_ui settles inside destroy', outcomes());
await flush();
expect(outcomes() === 'superseded,superseded', 'both settle after destroy', outcomes());
"""
    )


def test_a_reload_started_mid_flush_settles_the_clear_ui_it_cuts_off() -> None:
    """A deferred resync that reloads from the middle of a flush supersedes the rest of the
    backlog; a clear_ui in that rest still settles."""
    _run(
        r"""
const es = await live();
await send(es, {type: 'content', text: 'reply'}, 4);
await send(es, {type: 'replay_truncated'});
await send(es, {type: 'clear_ui'}, 5);
await send(es, {type: 'state_change', state: 'idle'}, 6);
await send(es, {type: 'clear_ui'}, 7);
await network.reply('/history', {detail: 'unavailable'}, {status: 500});
expect(sync.status === 'loading', 'the idle edge in the backlog reloads', sync.status);
expect(outcomes().split(',').sort().join(',') === 'failed,superseded',
  'the refetch failed and the cut-off clear_ui settled', outcomes());
"""
    )


def test_a_render_that_throws_after_its_wipe_leaves_the_latch_clear_and_arms_no_retry() -> None:
    """The latch clears at the render step, before the pane's render: a render that throws
    after its wipe leaves a fresh transcript that at worst under-counts rows, so the pane is not
    re-closed and no retry is armed."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
view.renderHistory = () => { throw new Error('render bug'); };
await network.reply('/history', {messages: [user('v2')]});
expect(outcomes() === 'threw' && !sync.historyStale, 'the latch is clear after the throw');
expect(clock.pending().length === 0, 'and no retry is armed', JSON.stringify(clock.pending()));
"""
    )


def test_the_latch_is_cleared_before_the_pane_renders() -> None:
    """The order of the carried rule: the clear comes first, then the pane's render."""
    _run(
        r"""
const es = await live({messages: [user('v1')]});
await send(es, {type: 'clear_ui'}, 4);
const render = view.renderHistory;
const seen = [];
view.renderHistory = messages => {
  seen.push(sync.historyStale);
  render(messages);
};
await network.reply('/history', {messages: [user('v2')]});
expect(seen.join(',') === 'false', 'the latch was already clear when the pane rendered',
  seen.join(','));
"""
    )


def test_a_status_listener_that_destroys_on_live_gets_no_missed() -> None:
    """Work after a phase change re-checks the source: an instance a status listener destroyed
    on ``live`` announces nothing more."""
    _run(
        r"""
const es = await live({messages: []});
await send(es, {type: 'status'});
sync.on('status', status => { if (status === 'live') sync.destroy(); });
es.drop();
await clock.advance(1000);
es.open();
await flush();
expect(sync.status === 'destroyed' && missed.length === 0, 'no missed after the teardown',
  String(missed.length));
"""
    )


def test_a_status_listener_that_destroys_on_parked_places_no_prompt() -> None:
    """A repair whose parked phase tore the instance down places no prompt into the pane."""
    _run(
        r"""
pinRandom(0);
const es = await live();
sync.on('status', status => { if (status === 'parked') sync.destroy(); });
await send(es, {type: 'history_resync'});
for (const delay of [0, 2000, 4000, 8000]) {
  await clock.advance(delay);
  await network.reply('/history', {detail: 'unavailable'}, {status: 503});
}
expect(sync.status === 'destroyed' && view.prompts.length === 0,
  'no prompt for a torn-down instance', String(view.prompts.length));
"""
    )


def test_a_queued_clear_ui_settles_after_the_terminal_phase() -> None:
    """The teardown drops the queue after the phase change: a reset-settled listener finds the
    instance already terminal, so it cannot reopen a stream from inside the teardown, and a
    destroy from there ends the dead instance destroyed."""
    _run(
        r"""
pinRandom(0);
const es = await live({messages: [user('kept')]});
for (let id = 4; id < 9; id += 1) await send(es, {type: 'clear_ui'}, id);
const seen = [];
sync.on('reset-settled', payload => {
  if (payload.outcome !== 'superseded') return;
  seen.push(sync.status);
  sync.retryNow();
  sync.destroy();
});
es.drop();
es.fail();
await refuseEveryRedial({until: () => sync.status === 'dead' || sync.status === 'destroyed'});
expect(seen[0] === 'dead', 'the first queued clear_ui settles with the instance already dead',
  seen.join(','));
expect(sources().length === 7, 'nothing reopened from inside the teardown',
  String(sources().length));
expect(statuses.slice(-2).join(',') === 'dead,destroyed',
  'the destroy from inside the teardown is honored', statuses.join(','));
"""
    )


# --- source pins: rulings about absence or uniqueness ----------------------------------------
#
# A behavior test cannot see these: each says something must happen in exactly one place, or
# nowhere.  Every pin also asserts its anchor exists, so a rename fails loudly.


def _code(path: Path) -> str:
    return strip_js_comments(path.read_text(encoding="utf-8"))


def test_the_connection_epoch_is_bumped_once_inside_the_open_listener() -> None:
    """The counter that lets a seedless render see a reconnect (#900) is written in the open
    listener and nowhere else: a native reconnect fires only ``open``, and a deferred connect
    establishes nothing, so a write anywhere else misses the case or bumps falsely."""
    code = _code(_MODULE)
    assert "#epoch = 0;" in code, "the counter must stay initialised (undefined declines all)"
    writes = re.findall(r"this\.#epoch\s*(\+\+|--|[-+*/]?=(?!=))", code)
    assert writes == ["+="], f"the epoch is written outside the open listener: {writes}"
    assert "this.#epoch += 1;" in extract_braced(code, "  #onOpen(source) {")


def test_the_staleness_latch_is_cleared_only_by_the_render_step() -> None:
    """Only a completed /history render may reopen rewind and edit (#890, #894): the latch has
    exactly one clear, inside the shared render step."""
    code = _code(_MODULE)
    clears = re.findall(r"this\.#historyStale\s*=\s*false", code)
    assert len(clears) == 1, f"{len(clears)} latch clears"
    assert "this.#historyStale = false;" in extract_braced(code, "  #render(data, seeded) {")


def test_the_cursor_comes_only_off_the_message_event_and_infers_nothing() -> None:
    """The event id lives on the MessageEvent, never on the source object; and since ids can
    arrive lower without any reset, nothing does arithmetic on a cursor to infer a gap.  Both
    panes are on the core, so neither holds or compares a cursor of its own."""
    code = _code(_MODULE)
    readers = re.findall(r"(\w+)\.lastEventId", code)
    assert readers == ["event"], f"lastEventId read off {readers}"
    assert "this.#cursor = id;" in code
    for field in ("#cursor", "#gapCursor"):
        assert not re.search(r"this\." + re.escape(field) + r"\s*[-+]\s*\d", code), field
    for pane in (_INTERACTIVE, _COORDINATOR):
        assert "lastEventId" not in _code(pane), pane.name


def test_the_overflow_ladder_math_has_one_source() -> None:
    """The trip threshold and cooldown ladder live in sse_overflow.js: the core imports them,
    and neither the core nor either pane keeps a copy."""
    code = _code(_MODULE)
    clause = re.search(r'import\s*\{([^}]*)\}\s*from\s*"\./sse_overflow\.js";', code)
    assert clause is not None, "the core no longer imports sse_overflow.js"
    names = sorted(name.strip() for name in clause.group(1).split(",") if name.strip())
    assert names == sorted(
        [
            "DEGRADED_COOLDOWN_BASE_MS",
            "DEGRADED_COOLDOWN_MAX_MS",
            "DEGRADED_COOLDOWN_RESET_MS",
            "OVERFLOW_TRIP_COUNT",
            "OVERFLOW_TRIP_WINDOW_MS",
            "STALE_RETRY_BASE_MS",
            "STALE_RETRY_JITTER_MS",
            "TRUNCATED_RESYNC_JITTER_MS",
            "degradedCooldownStep",
            "overflowWindowTripped",
        ]
    ), names
    for path in (_MODULE, _INTERACTIVE, _COORDINATOR):
        body = _code(path)
        for fork in (
            "function overflowWindowTripped(",
            "function degradedCooldownStep(",
            "const OVERFLOW_TRIP_COUNT =",
            "const DEGRADED_COOLDOWN_BASE_MS =",
        ):
            assert fork not in body, f"{path.name} forks the ladder: {fork}"


def test_the_repair_policy_and_its_verdict_stay_in_history_handoff() -> None:
    """The core drives the shared repair controller and never re-derives its budget, backoff,
    prompt or verdict (the outcome is handed over, never judged here)."""
    code = _code(_MODULE)
    assert "createHistoryHandoffRepair(" in code
    for reimplemented in (
        "historyHandoffAttemptAllowed(",
        "HISTORY_HANDOFF_MAX_ATTEMPTS",
        "nextHistoryHandoffDelay(",
        "buildHistoryHandoffPrompt(",
        'outcome === "rendered"',
    ):
        assert reimplemented not in code, reimplemented
