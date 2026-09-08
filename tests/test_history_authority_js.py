"""Execute shared auth, saved cards, and asynchronous history state under Node."""

from pathlib import Path

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
