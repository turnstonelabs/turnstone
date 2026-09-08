"""Execute shared auth, saved cards, and asynchronous history state under Node."""

from pathlib import Path

import pytest

from tests._js_harness_helpers import (
    FAKE_DOM,
    demodulize,
    extract_braced,
    node_skip,
    run_node_source,
)

pytestmark = node_skip
_ROOT = Path(__file__).resolve().parents[1] / "turnstone"
_SETUP = r"""
import assert from 'node:assert/strict';
globalThis.window = {};
globalThis.BroadcastChannel = undefined;
globalThis.AbortController = undefined;
const elements = new Map();
document.getElementById = id => elements.get(id) || null;
document.addEventListener = document.removeEventListener = () => {};
document.body = new FakeElement('body');
document.createTextNode = text => Object.assign(new FakeElement('text'), {textContent: text});
globalThis.Node = FakeElement;
Object.defineProperty(FakeElement.prototype, 'style', {get() {
  return this._style ||= {setProperty(name, value) { this[name] = value; }};
}});
FakeElement.prototype.insertBefore = function(child) { return this.appendChild(child); };
const session = new Map();
globalThis.sessionStorage = {
  getItem: key => session.get(key) ?? null,
  setItem: (key, value) => session.set(key, value),
  removeItem: key => session.delete(key),
};
const requests = [];
globalThis.fetch = url => new Promise(resolve => requests.push({url, resolve}));
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
function showToast() {}
function formatRelativeTime() { return ''; }
const drain = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };
function reply(request, data, status = 200) {
  request.resolve(new Response(JSON.stringify(data), {status}));
}
const operator = {user_id:'operator', scopes:'read,write', permissions:'read,write'};
const reader = {user_id:'admin', scopes:'read', permissions:'admin.coordinator'};
function element(id) {
  const el = new FakeElement('div'); elements.set(id, el); return el;
}
function table() {
  return createSavedTable({
    columns: [], bodyEl: element('saved-coord-cards'), noun:'session',
    canActivate: () => hasScope('write'), canDelete: () => hasScope('write'),
    onActivate: () => { activations++; },
    delete: {idPrefix:'delete', buttonId:'delete-button', buildDeleteRequest: () => ({url:'/delete'})},
  });
}
let activations = 0;
"""


def _run(body, *, console_loader=False, ui_loader=False):
    source = FAKE_DOM + _SETUP
    source += demodulize(_ROOT / "shared_static/auth.js")
    source += demodulize(_ROOT / "shared_static/cards.js")
    source += demodulize(_ROOT / "shared_static/list_cache.js")
    if console_loader:
        app = (_ROOT / "console/static/app.js").read_text()
        for name in ("loadSavedCoordinators", "_setSavedError", "_savedAuthChanged"):
            source += extract_braced(
                app,
                f"function {name}() {{"
                if name != "_setSavedError"
                else "function _setSavedError(message) {",
            )
        source += (
            "\nlet _savedCoordsInFlight = false, _savedCoordsRetry = false, _coordTable = null;\n"
        )
    if ui_loader:
        app = (_ROOT / "ui/static/app.js").read_text()
        for signature in (
            "function _initSavedWsTable() {",
            "function loadDashboard() {",
            "function _setSavedWsMessage(text) {",
        ):
            source += extract_braced(app, signature)
        source += "\nlet _wsTable = null, dashboardVisible = false;\n"
        source += "function makeEmptyState(text) { return new FakeElement('div'); }\n"
        source += "function renderDashboardTable() {}\n"
    result = run_node_source(source + "\n" + body)
    assert result.returncode == 0, result.stderr


def test_scope_actions_and_selection_reset():
    _run(r"""
reply(requests.shift(), reader); await drain();
assert.equal(hasPermission('admin.coordinator'), true);
assert.equal(hasScope('write'), false);
const button = element('delete-button');
const saved = table();
onAuthChange(() => saved.reset());
saved.setItems([{ws_id:'one', name:'One'}]);
let row = elements.get('saved-coord-cards').children[0];
assert.equal(row.getAttribute('role'), 'group');
assert.equal(row.getAttribute('tabindex'), null);
assert.equal(row.getAttribute('aria-label'), 'One');
row.onclick(); row.onkeydown({key:'Enter', preventDefault(){}});
assert.equal(activations, 0);
assert.equal(button.style.display, 'none');
saved.controller.start(); assert.equal(saved.controller.inMode(), false);
_storePermissions(operator);
saved.setItems([{ws_id:'one', name:'One'}]);
row = elements.get('saved-coord-cards').children[0];
assert.equal(row.getAttribute('role'), 'button'); row.onclick();
assert.equal(activations, 1); assert.equal(button.style.display, '');
saved.controller.start(); saved.controller.toggleAll();
assert.equal(saved.controller.isSelected('one'), true);
_storePermissions({...operator, scopes:'read'});
assert.equal(saved.controller.inMode(), false);
assert.equal(saved.controller.isSelected('one'), false);
assert.equal(button.style.display, 'none');
""")


def test_auth_response_ordering_without_abort_controller():
    _run(r"""
const oldWhoami = requests.shift();
_scheduleRefreshFromWhoami(); const newWhoami = requests.shift();
reply(newWhoami, operator); await drain();
reply(oldWhoami, reader); await drain();
assert.equal(sessionStorage.getItem('ts.user_id'), 'operator');
const oldRefresh = _tryRefresh(); const oldRequest = requests.shift();
_invalidateAuth(); _loggedOut = false; _storePermissions(reader);
const newRefresh = _tryRefresh(); const newRequest = requests.shift();
reply(oldRequest, operator); await drain();
assert.equal(await oldRefresh, false);
assert.equal(sessionStorage.getItem('ts.user_id'), 'admin');
const coalesced = _tryRefresh(); assert.equal(requests.length, 0);
reply(newRequest, {...reader, exp:0});
assert.equal(await newRefresh, true); assert.equal(await coalesced, true);
const pending = authFetch('/saved').catch(e => e.message);
const stale = requests.shift(); let finishBody;
stale.resolve({status:401, clone:() => ({json:() => new Promise(r => {finishBody = r;})})});
await drain();
_invalidateAuth(); _loggedOut = false; _storePermissions(operator);
const generation = authGeneration();
finishBody({code:'version_mismatch'});
assert.equal(await pending, 'auth changed');
assert.equal(authGeneration(), generation);
assert.equal(sessionStorage.getItem('ts.user_id'), 'operator');
""")


@pytest.mark.parametrize("cached_identity", [False, True])
@pytest.mark.parametrize("refresh_ok", [False, True])
def test_startup_whoami_401_recovers_pending_requests(cached_identity, refresh_ok):
    _run(
        f"const cachedIdentity = {str(cached_identity).lower()};\n"
        f"const refreshOK = {str(refresh_ok).lower()};\n"
        + r"""
if (cachedIdentity) {
  sessionStorage.setItem('ts.user_id', operator.user_id);
  sessionStorage.setItem('turnstone_scopes', operator.scopes);
  sessionStorage.setItem('turnstone_permissions', operator.permissions);
}
const overlay = element('login-overlay'); overlay.style.display = 'none';
const whoami = requests.shift();
const pending = authFetch('/saved').catch(e => e.message);
const initial = requests.shift();
reply(whoami, {error:'expired'}, 401); await drain();
assert.equal(requests.length, 1, 'whoami must initiate authentication recovery');
const refresh = requests.shift(); assert.equal(refresh.url, '/v1/api/auth/refresh');
reply(initial, {error:'expired'}, 401); await drain();
assert.equal(requests.length, 0, 'parallel 401s must share one refresh');
reply(refresh, refreshOK ? {...operator, exp:0} : {error:'expired'}, refreshOK ? 200 : 401);
await drain();
if (refreshOK) {
  assert.equal(overlay.style.display, 'none');
  assert.equal(sessionStorage.getItem('ts.user_id'), 'operator');
  assert.equal(hasScope('write'), true);
  const retry = requests.shift(); assert.equal(retry.url, '/saved');
  reply(retry, {workstreams:[]}); assert.equal((await pending).ok, true);
} else {
  assert.equal(overlay.style.display, 'flex');
  assert.equal(sessionStorage.getItem('ts.user_id'), null);
  assert.equal(hasScope('read'), false);
  assert.match(await pending, /^auth( changed)?$/);
  assert.equal(requests.shift().url, '/v1/api/auth/status');
}
assert.equal(requests.length, 0);
"""
    )


def test_whoami_auth_loss_before_login_mount_preserves_upgrade_prompt():
    _run(r"""
function escapeHtml(text) { return text; }
function setSafeHtml(el, html) { el.textContent = html; }
document.querySelectorAll = () => [];
const append = document.body.appendChild.bind(document.body);
document.body.appendChild = el => { elements.set(el.id, el); return append(el); };
window.location = {search:'', pathname:'/', reload:() => { reloads++; }};
let reloads = 0;
['login-box', 'toggle-token', 'setup-fields', 'login-fields', 'token-fields',
 'login-toggle', 'login-subtitle', 'login-submit'].forEach(element);
sessionStorage.setItem('ts.user_id', 'operator');
reply(requests.shift(), {code:'version_mismatch'}, 401); await drain();
assert.equal(sessionStorage.getItem('ts.user_id'), null);
assert.equal(requests.length, 0);
initLogin();
assert.equal(elements.get('login-overlay').style.display, 'flex');
const status = requests.shift(); assert.equal(status.url, '/v1/api/auth/status');
reply(status, {setup_required:false}); await drain();
assert.match(elements.get('login-subtitle').textContent, /server was updated/);
_onSuccess(); assert.equal(reloads, 1);
""")


@pytest.mark.parametrize("stage", ["body", "refresh"])
def test_superseded_whoami_401_cannot_clear_unchanged_authority(stage):
    _run(
        f"const stage = '{stage}';\n"
        + r"""
reply(requests.shift(), operator); await drain();
const overlay = element('login-overlay'); overlay.style.display = 'none';
_scheduleRefreshFromWhoami(); const oldWhoami = requests.shift();
let finishBody, oldRefresh;
if (stage === 'body') {
  oldWhoami.resolve({status:401, clone:() => ({json:() => new Promise(r => {finishBody = r;})})});
  await drain();
} else {
  reply(oldWhoami, {error:'expired'}, 401); await drain();
  oldRefresh = requests.shift(); assert.equal(oldRefresh.url, '/v1/api/auth/refresh');
}
const generation = authGeneration();
_scheduleRefreshFromWhoami();
reply(requests.shift(), operator); await drain();
assert.equal(authGeneration(), generation, 'unchanged authority does not advance generation');
if (finishBody) finishBody({code:'version_mismatch'});
else reply(oldRefresh, {error:'expired'}, 401);
await drain();
assert.equal(overlay.style.display, 'none');
assert.equal(sessionStorage.getItem('ts.user_id'), 'operator');
assert.equal(hasScope('write'), true);
assert.equal(requests.length, 0, 'superseded whoami must not refresh or open login');
"""
    )


def test_saved_requests_cannot_cross_auth_generations():
    _run(
        r"""
reply(requests.shift(), operator); await drain();
element('saved-coordinators'); element('coord-saved-footer');
const error = element('coord-saved-error'); element('coord-saved-error-text');
_coordTable = table(); onAuthChange(_savedAuthChanged);
loadSavedCoordinators(); loadSavedCoordinators();
assert.equal(requests.length, 1);
reply(requests.shift(), {workstreams:[{ws_id:'old'}]}); await drain();
assert.equal(requests.length, 1); const old = requests.shift();
_invalidateAuth(); _loggedOut = false; _storePermissions({...operator, user_id:'new'});
assert.equal(requests.length, 1); const current = requests.shift();
reply(old, {workstreams:[{ws_id:'stale'}]}); await drain();
loadSavedCoordinators(); assert.equal(requests.length, 0);
reply(current, {workstreams:[{ws_id:'current'}]}); await drain();
assert.equal(elements.get('saved-coord-cards').children[0].dataset.wsId, 'current');
assert.equal(requests.length, 1);
reply(requests.shift(), {error:'private diagnostic'}, 503); await drain();
assert.equal(error.hidden, false);
assert.equal(elements.get('saved-coord-cards').style.display, 'none');
loadSavedCoordinators(); reply(requests.shift(), {workstreams:[{ws_id:'recovered'}]});
await drain(); assert.equal(error.hidden, true);
assert.equal(elements.get('saved-coord-cards').children[0].dataset.wsId, 'recovered');
""",
        console_loader=True,
    )


def test_superseded_refresh_preserves_current_same_user_authority():
    _run(r"""
reply(requests.shift(), operator); await drain();
const pending = authFetch('/saved').catch(e => e.message);
reply(requests.shift(), {error:'expired'}, 401); await drain();
const oldRefresh = requests.shift();
_scheduleRefreshFromWhoami();
const reduced = {...operator, scopes:'read', permissions:'read'};
reply(requests.shift(), reduced); await drain();
reply(oldRefresh, {...reduced, exp:0});
assert.equal(await pending, 'auth changed');
assert.equal(sessionStorage.getItem('ts.user_id'), 'operator');
assert.equal(hasScope('write'), false);
assert.equal(hasScope('read'), true);
""")


def test_standalone_initial_whoami_reloads_saved_history():
    _run(
        r"""
element('dashboard-saved-cards'); element('ws-saved-footer');
element('ws-pagination'); element('dash-ws-table');
_initSavedWsTable();
const whoami = requests.shift();
loadDashboard();
const oldDashboard = requests.shift(), oldSaved = requests.shift();
reply(whoami, operator); await drain();
assert.equal(requests.length, 2);
reply(oldDashboard, {workstreams:[]});
reply(oldSaved, {workstreams:[{ws_id:'stale'}]}); await drain();
reply(requests.shift(), {workstreams:[]});
reply(requests.shift(), {workstreams:[{ws_id:'current'}]}); await drain();
assert.equal(elements.get('dashboard-saved-cards').children[0].dataset.wsId, 'current');
""",
        ui_loader=True,
    )


def test_project_cache_reset_discards_rows_and_pending_refetches():
    _run(r"""
reply(requests.shift(), operator); await drain();
const cache = makeListCache({url:'/projects', dataKey:'projects', name:'projects', keyField:'id', fpRow:p=>p.id});
const oldLoad = cache.refresh(); const old = requests.shift();
const oldTrailing = cache.refresh({force:true});
cache.reset();
const newLoad = cache.refresh(); const current = requests.shift();
reply(old, {projects:[{id:'secret', name:'Old principal project'}]});
await oldLoad; await oldTrailing;
assert.equal(cache.getByKey('secret'), null);
assert.equal(cache.refresh(), newLoad);
assert.equal(requests.length, 0);
reply(current, {projects:[{id:'new'}]}); await newLoad;
assert.deepEqual(cache.get().map(p=>p.id), ['new']);
cache.reset(); assert.deepEqual(cache.get(), []); assert.equal(cache.loaded(), false);
""")
