"""The browser fakes in tests/_js_harness_helpers.py, checked under node.

FAKE_EVENT_SOURCE is the instrument the stream tests rest on, so it is held to
a written contract (the ES rules in the docstring beside it).  The contract is
one script, EVENT_SOURCE_CONTRACT, parameterized by a driver that makes "the
server" act; here the driver is the fake's own test-side controls, and a browser
harness (the ``es-contract`` scenario in scripts/recovery_e2e.py) is meant to
run the same script against a real EventSource.  The clock, fetch and
visibility fakes get direct checks of the behaviors the pane tests use.
"""

from __future__ import annotations

from tests._js_harness_helpers import (
    EVENT_SOURCE_CONTRACT,
    FAKE_CLOCK,
    FAKE_DOM,
    FAKE_EVENT_SOURCE,
    FAKE_FETCH,
    FAKE_VISIBILITY,
    PANE_DOM,
    node_skip,
    run_node_source,
)

pytestmark = node_skip

_ALL_RULES = [f"ES{n}" for n in range(1, 14)]

# The node driver: every server action is the matching FakeEventSource control.
_FAKE_DRIVER = r"""
const streamUrl = name => '/contract/' + name + '?stream=' + name;
const sourceFor = name => {
  const found = FakeEventSource.instances.filter(es => es.url.endsWith(streamUrl(name)));
  if (!found.length) throw new Error('no source for stream ' + name);
  return found.at(-1);
};
const driver = {
  url: streamUrl,
  open: name => { sourceFor(name).open(); },
  send: (name, frame) => {
    sourceFor(name).deliver(frame.data, {id: frame.id, event: frame.event});
  },
  drop: name => { sourceFor(name).drop(); },
  fail: name => { sourceFor(name).fail(); },
  lastEventIdHeader: name => sourceFor(name).attempts.at(-1).lastEventIdHeader,
  settle: () => Promise.resolve(),
};
"""


def _run(script: str) -> None:
    proc = run_node_source(script)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}\n{proc.stdout}"


def test_fake_event_source_meets_the_contract() -> None:
    """Every ES rule holds for the fake, driven through its own controls, and
    the script checks all of them (a rule dropped from the script would leave
    the fake unchecked on it)."""
    _run(
        FAKE_EVENT_SOURCE
        + EVENT_SOURCE_CONTRACT
        + _FAKE_DRIVER
        + f"""
const checked = await checkEventSourceContract(driver);
const expected = {_ALL_RULES!r};
if (JSON.stringify([...checked].sort()) !== JSON.stringify([...expected].sort()))
  throw new Error('contract checks ran for ' + checked.join(',') +
    ', expected ' + expected.join(','));
if (reportedErrors.length) throw new Error('listener errors: ' + reportedErrors.length);
"""
    )


def test_contract_script_catches_a_source_that_exposes_last_event_id() -> None:
    """The contract fails a fake carrying the mistake all three clients once made
    (reading ``lastEventId`` off the source object), so a fake that grew the
    property could not pass silently."""
    _run(
        FAKE_EVENT_SOURCE
        + EVENT_SOURCE_CONTRACT
        + _FAKE_DRIVER
        + r"""
Object.defineProperty(FakeEventSource.prototype, 'lastEventId', {get() { return ''; }});
let message = '';
try {
  await checkEventSourceContract(driver);
} catch (err) {
  message = err.message;
}
if (!message.includes('ES3')) throw new Error('expected an ES3 failure, got: ' + message);
"""
    )


def test_contract_script_catches_a_source_that_resets_the_id() -> None:
    """The contract fails a fake that forgets the last event ID between frames,
    the spec rule the clients' cursor capture depends on."""
    _run(
        FAKE_EVENT_SOURCE
        + EVENT_SOURCE_CONTRACT
        + _FAKE_DRIVER
        + r"""
const deliver = FakeEventSource.prototype.deliver;
FakeEventSource.prototype.deliver = function(data, options = {}) {
  return deliver.call(this, data, {...options, id: options.id === undefined ? '' : options.id});
};
let message = '';
try {
  await checkEventSourceContract(driver);
} catch (err) {
  message = err.message;
}
if (!message.includes('ES5')) throw new Error('expected an ES5 failure, got: ' + message);
"""
    )


def test_fake_event_source_test_side_record_and_misuse() -> None:
    """The test-side record (attempts, closeCalled, init), read-only accessors as
    in a browser, listener throws reported rather than raised, and controls used
    out of order throw as test bugs."""
    _run(
        FAKE_EVENT_SOURCE
        + r"""
const fail = message => { throw new Error(message); };
const es = new EventSource('/stream?x=1', {withCredentials: true});
if (es.init.withCredentials !== true) fail('init dictionary not recorded');
if (FakeEventSource.latest() !== es || FakeEventSource.live().length !== 1) fail('discovery');
let threw = false;
try { es.readyState = 1; } catch (err) { threw = err instanceof TypeError; }
if (!threw || es.readyState !== 0) fail('readyState must be read-only');
const misused = control => {
  try { control(); } catch (err) { return /needs/.test(err.message); }
  return false;
};
if (!misused(() => es.deliver('x'))) fail('deliver() before open() must throw');
es.open();
if (!misused(() => es.open())) fail('open() on an open source must throw');
if (!misused(() => es.fail())) fail('fail() on an open source must throw: drop() first');
const seen = [];
es.onmessage = () => { throw new Error('handler bug'); };
es.addEventListener('message', event => seen.push(event.data));
es.deliver('after-throw', {id: 4});
if (seen.join() !== 'after-throw') fail('a throwing handler must not stop later listeners');
if (reportedErrors.length !== 1 || !/handler bug/.test(reportedErrors[0].err.message))
  fail('the throw must be reported, not raised');
es.drop();
if (JSON.stringify(es.attempts) !== JSON.stringify([
  {lastEventIdHeader: null}, {lastEventIdHeader: '4'}]))
  fail('attempts: ' + JSON.stringify(es.attempts));
const refused = new EventSource('/refused');
refused.fail();
if (refused.closeCalled || refused.readyState !== 2) fail('fail() is not a client close');
es.close();
if (!es.closeCalled || FakeEventSource.live().length !== 0) fail('close() not recorded');
if (es.open() !== false || es.deliver('late') !== false) fail('controls on CLOSED are no-ops');
"""
    )


def test_fake_clock_fires_due_timers_in_order() -> None:
    """advance() fires timers in due order with ties in arming order, runs timers
    armed by callbacks when they fall inside the window, repeats intervals,
    honors clearTimeout, drains microtasks between firings, and moves Date.now."""
    _run(
        FAKE_DOM
        + PANE_DOM
        + FAKE_CLOCK
        + r"""
const order = [];
const startedAt = Date.now();
const first = setTimeout(() => order.push('a@' + clock.elapsed), 100);
setTimeout(() => order.push('b@' + clock.elapsed), 100);
const dropped = setTimeout(() => order.push('dropped'), 50);
clearTimeout(dropped);
setTimeout(() => {
  order.push('c@' + clock.elapsed);
  Promise.resolve().then(() => order.push('micro@' + clock.elapsed));
  setTimeout(() => order.push('nested@' + clock.elapsed), 30);
}, 120);
const tick = setInterval(() => order.push('tick@' + clock.elapsed), 70);
if (first !== 1) throw new Error('timer ids must start at 1, got ' + first);
await clock.advance(149);
assert(order.join() === 'tick@70,a@100,b@100,c@120,micro@120,tick@140',
  'first window: ' + order.join());
await clock.advance(1);
assert(order.at(-1) === 'nested@150', 'a timer armed by a callback fires inside the window');
clearInterval(tick);
await clock.advance(1000);
assert(order.length === 7, 'cleared interval kept firing: ' + order.join());
assert(Date.now() - startedAt === 1150 && clock.elapsed === 1150, 'Date.now follows the clock');
pinRandom(0.25);
assert(Math.random() === 0.25, 'pinRandom');
setTimeout(() => { throw new Error('timer bug'); }, 1);
await clock.advance(1);
assert(reportedErrors.length === 1, 'a throwing timer is reported, not raised');
"""
    )


def test_fake_clock_clamps_nested_zero_timeouts() -> None:
    """The HTML nesting clamp: a callback that re-arms a 0 ms timeout fires six
    times at the same instant (nesting levels 1-6), then every 4 ms, so
    advance() returns; a 0 ms interval is clamped the same way."""
    _run(
        FAKE_DOM
        + PANE_DOM
        + FAKE_CLOCK
        + r"""
const fired = [];
let handle = 0;
const loop = () => { fired.push(clock.elapsed); handle = setTimeout(loop, 0); };
setTimeout(loop, 0);
await clock.advance(20);
clearTimeout(handle);
assert(fired.join() === '0,0,0,0,0,0,4,8,12,16,20', 'nested 0 ms timeouts: ' + fired.join());
const ticks = [];
const tick = setInterval(() => ticks.push(clock.elapsed - 20), 0);
await clock.advance(8);
clearInterval(tick);
assert(ticks.join() === '0,0,0,0,0,0,4,8', 'a 0 ms interval: ' + ticks.join());
assert(reportedErrors.length === 0, 'no callback threw');
"""
    )


def test_fake_clock_names_a_timer_that_never_lets_time_move() -> None:
    """A 0 ms timer re-armed from a microtask sits outside the nesting clamp, so
    virtual time never moves; advance() throws an error naming the timer instead
    of spinning forever."""
    _run(
        FAKE_DOM
        + PANE_DOM
        + FAKE_CLOCK
        + r"""
function spinForever() {
  Promise.resolve().then(() => setTimeout(spinForever, 0));
}
setTimeout(spinForever, 0);
let message = '';
try {
  await clock.advance(10);
} catch (err) {
  message = err.message;
}
assert(message.includes('"spinForever"') && message.includes('1000 times'),
  'advance must name the runaway timer, got: ' + message);
"""
    )


def test_fake_event_source_dispatches_in_registration_order() -> None:
    """Direct check of ES12 and ES13 on the fake, beside the contract run: the
    handler's place is fixed when it is first set, a replaced handler keeps it,
    a listener removed mid-dispatch is skipped and one added mid-dispatch waits
    for the next event."""
    _run(
        FAKE_EVENT_SOURCE
        + r"""
const fail = message => { throw new Error(message); };
const es = new EventSource('/order');
es.open();
const order = [];
es.addEventListener('message', () => order.push('first'));
es.onmessage = () => order.push('handler');
const removed = () => order.push('removed');
es.addEventListener('message', () => {
  order.push('remover');
  es.removeEventListener('message', removed);
  es.addEventListener('message', () => order.push('added'));
});
es.addEventListener('message', removed);
es.deliver('one');
if (order.join() !== 'first,handler,remover') fail('first dispatch: ' + order.join());
order.length = 0;
es.onmessage = () => order.push('replaced');
es.deliver('two');
if (!order.join().startsWith('first,replaced,remover,added'))
  fail('replaced handler keeps its place: ' + order.join());
if (es.onmessage === null) fail('onmessage must read back its handler');
es.onmessage = null;
order.length = 0;
es.deliver('three');
if (order.includes('replaced')) fail('a null handler must not run: ' + order.join());
"""
    )


def test_fake_fetch_queues_requests_and_honors_abort() -> None:
    """Requests wait for the test's reply or failure, responses are real Response
    objects, an aborted request rejects and discards a late reply, and an abort
    after the reply errors the unread body, as a browser does."""
    _run(
        FAKE_FETCH
        + r"""
const fail = message => { throw new Error(message); };
const ok = fetch('/v1/api/thing/history?x=1');
const broken = fetch('/v1/api/thing/events', {method: 'post'}).then(() => null, err => err);
const ctrl = new AbortController();
const aborted = fetch('/v1/api/thing/history', {signal: ctrl.signal});
if (network.pending('/history').length !== 2) fail('path-suffix matching ignores the query');
if (network.seen(/events/)[0].method !== 'POST') fail('method recorded upper-case');
await network.reply('/history', {messages: [1]}, {headers: {'Retry-After': '3'}});
const response = await ok;
if (!response.ok || response.headers.get('retry-after') !== '3') fail('response shape');
if ((await response.clone().json()).messages[0] !== 1) fail('json body');
await network.fail('/events');
if (!((await broken) instanceof TypeError)) fail('a network failure rejects with TypeError');
const request = network.take('/history');
ctrl.abort();
let abortError = null;
try { await aborted; } catch (err) { abortError = err; }
if (!abortError || abortError.name !== 'AbortError' || request.state !== 'aborted') fail('abort');
if (request.reply({late: true}) !== false) fail('a reply to an aborted request is discarded');
const status = fetch('/v1/api/thing/history');
await network.reply('/history', null, {status: 500});
if ((await status).status !== 500) fail('status');
const late = new AbortController();
const lateCall = fetch('/v1/api/thing/late', {signal: late.signal});
await network.reply('/late', {messages: []});
const lateResponse = await lateCall;
late.abort();
let bodyError = null;
try { await lateResponse.json(); } catch (err) { bodyError = err; }
if (!bodyError || bodyError.name !== 'AbortError')
  fail('an abort after the reply must error the body, got: ' + bodyError);
const read = new AbortController();
const readCall = fetch('/v1/api/thing/read', {signal: read.signal});
await network.reply('/read', {kept: 1});
const kept = await (await readCall).json();
read.abort();
if (kept.kept !== 1) fail('a body read before the abort is unaffected');
let missing = '';
try { network.take('/nothing'); } catch (err) { missing = err.message; }
if (!missing.includes('seen: GET /v1/api/thing/history?x=1 (replied)'))
  fail('take() names what was seen');
"""
    )


def test_fake_visibility_dispatches_on_change_only() -> None:
    """hide() and show() flip document.hidden and dispatch visibilitychange once
    per real change; a listener registers once per function and can be removed,
    directly or by aborting the signal it was added with."""
    _run(
        FAKE_DOM
        + PANE_DOM
        + FAKE_VISIBILITY
        + r"""
const seen = [];
const listener = () => seen.push(document.hidden + '/' + document.visibilityState);
document.addEventListener('visibilitychange', listener);
document.addEventListener('visibilitychange', listener);
assert(visibility.listenerCount() === 1, 'a function registers once');
visibility.hide();
visibility.hide();
visibility.show();
assert(seen.join() === 'true/hidden,false/visible', 'dispatch on change only: ' + seen.join());
document.removeEventListener('visibilitychange', listener);
visibility.hide();
assert(seen.length === 2 && visibility.listenerCount() === 0, 'removed listener still fired');
const lifetime = new AbortController();
document.addEventListener('visibilitychange', listener, {signal: lifetime.signal});
assert(visibility.listenerCount() === 1, 'a signalled listener registers');
lifetime.abort();
visibility.show();
assert(seen.length === 2 && visibility.listenerCount() === 0, 'an aborted signal removes it');
document.addEventListener('visibilitychange', listener, {signal: lifetime.signal});
assert(visibility.listenerCount() === 0, 'an already-aborted signal adds nothing');
"""
    )
