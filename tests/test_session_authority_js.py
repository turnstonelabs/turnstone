"""Execute refresh eligibility and availability behavior in the shared auth client."""

from tests._js_harness_helpers import node_skip
from tests.test_history_authority_js import _run

pytestmark = node_skip


def test_api_session_does_not_schedule_or_attempt_refresh():
    _run(r"""
const timers = [];
globalThis.setTimeout = (fn, delay) => { timers.push({fn, delay}); return timers.length; };
const fixed = {...reader, can_refresh:false, exp:Date.now()/1000+3600};
reply(requests.shift(), fixed); await drain();
assert.equal(timers.length, 0);
assert.equal(await _tryRefresh(), false); assert.equal(requests.length, 0);
// A sibling refresh notification rechecks whoami but cannot make this session renewable.
_scheduleRefreshFromWhoami(); reply(requests.shift(), fixed); await drain();
assert.equal(timers.length, 0);
const overlay = element('login-overlay'); overlay.style.display = 'none';
const request = authFetch('/saved').catch(e=>e.message);
reply(requests.shift(), {error:'expired'}, 401); await drain();
assert.equal(await request, 'auth');
assert.equal(overlay.style.display, 'flex');
assert.deepEqual(requests.map(r=>r.url), ['/v1/api/auth/status']);
""")


def test_unknown_session_can_still_open_login_without_refresh():
    _run(r"""
const overlay = element('login-overlay'); overlay.style.display = 'none';
reply(requests.shift(), {error:'expired'}, 401); await drain();
assert.equal(overlay.style.display, 'flex');
assert.deepEqual(requests.map(r=>r.url), ['/v1/api/auth/status']);
""")


def test_refresh_503_keeps_authority_and_retries_before_original_expiry():
    _run(r"""
let now = 2000000000000, serial = 0; const timers = new Map();
Date.now = () => now;
globalThis.setTimeout = (fn, delay) => { timers.set(++serial, {fn, delay}); return serial; };
globalThis.clearTimeout = id => timers.delete(id);
const expiry = now/1000+600;
reply(requests.shift(), {...operator, exp:expiry}); await drain();
const overlay = element('login-overlay'); overlay.style.display = 'none';
const generation = authGeneration();
const api = authFetch('/saved').catch(e=>e.message);
reply(requests.shift(), {error:'expired'}, 401); await drain();
assert.equal(requests[0].url, '/v1/api/auth/refresh');
reply(requests.shift(), {error:'Permission storage unavailable'}, 503); await drain();
assert.match(await api, /temporarily unavailable/);
assert.equal(authGeneration(), generation);
assert.equal(sessionStorage.getItem('ts.user_id'), 'operator');
assert.equal(hasScope('write'), true); assert.equal(overlay.style.display, 'none');
assert.equal(timers.size, 1);
const [id, timer] = [...timers][0];
assert.equal(timer.delay, 30000);
timers.delete(id); now += timer.delay; timer.fn();
assert.equal(requests.length, 1);
reply(requests.shift(), {...operator, scopes:'read', permissions:'read', exp:expiry+600});
await drain(); assert.equal(hasScope('write'), false); assert.equal(hasScope('read'), true);
assert.equal(overlay.style.display, 'none');
""")


def test_delayed_retry_stops_at_expiry_and_logout_cancels_it():
    _run(r"""
let now = 2000000000000, serial = 0; const timers = new Map();
Date.now = () => now;
globalThis.setTimeout = (fn, delay) => { timers.set(++serial, {fn, delay}); return serial; };
globalThis.clearTimeout = id => timers.delete(id);
const expiry = now/1000+600;
reply(requests.shift(), {...operator, exp:expiry}); await drain();
const renewal = _tryRefresh(); reply(requests.shift(), {}, 503);
assert.equal(await renewal, null);
const [id, timer] = [...timers][0]; timers.delete(id);
now = expiry*1000+1; timer.fn();
assert.equal(requests.length, 0);
_scheduleRefreshAt(expiry+600);
const pending = _tryRefresh(), old = requests.shift();
_invalidateAuth(); reply(old, {}, 503);
assert.equal(await pending, false);
assert.equal(timers.size, 0);
assert.equal(sessionStorage.getItem('turnstone_can_refresh'), null);
""")


def test_authoritative_refresh_refusal_does_not_schedule_a_retry():
    _run(r"""
reply(requests.shift(), operator); await drain();
const overlay = element('login-overlay'); overlay.style.display = 'none';
const timers = [];
globalThis.setTimeout = (fn, delay) => { timers.push({fn, delay}); return 1; };
const renewal = _tryRefresh(); reply(requests.shift(), {error:'No active permissions'}, 403);
assert.equal(await renewal, false);
assert.equal(timers.length, 0);
// The existing cookie retains its original lifetime; a later rejected API request opens login.
assert.equal(overlay.style.display, 'none'); assert.equal(hasScope('read'), true);
""")
