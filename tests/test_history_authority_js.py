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
document.querySelectorAll = () => [];
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
const operator = {can_refresh:true, user_id:'operator', scopes:'read,write', permissions:'read,write'};
const reader = {can_refresh:true, user_id:'admin', scopes:'read', permissions:'admin.coordinator'};
function element(id) {
  const el = new FakeElement('div'); elements.set(id, el); return el;
}
function table() {
  return createSavedTable({
    columns: [], bodyEl: element('saved-coord-cards'), noun:'session',
    canActivate: () => hasScope('write'), canDelete: () => hasScope('write'),
    canDeleteAny: () => hasScope('write'),
    onActivate: () => { activations++; },
    delete: {idPrefix:'delete', buttonId:'delete-button', buildDeleteRequest: () => ({url:'/delete'})},
  });
}
let activations = 0;
"""


# The console's saved section as its markup has it, wired by the app's own table setup.
_CONSOLE_SAVED = r"""
function _consoleSaved() {
  ['saved-coordinators', 'coord-saved-colheaders', 'saved-coord-cards', 'coord-filter',
   'coord-saved-footer', 'coord-pagination', 'coord-saved-error', 'coord-saved-error-text']
    .forEach(element);
  _initSavedCoordTable();
}
"""
# The same for the node's dashboard; returns the rows' container.
_NODE_SAVED = r"""
function _nodeSaved() {
  ['dash-ws-table', 'ws-saved-colheaders', 'ws-filter', 'ws-saved-footer', 'ws-pagination',
   'ws-saved-error', 'ws-saved-error-text'].forEach(element);
  const cards = element('dashboard-saved-cards');
  _initSavedWsTable();
  return cards;
}
"""


def _run(body, *, console_loader=False, ui_loader=False):
    source = FAKE_DOM + _SETUP
    source += demodulize(_ROOT / "shared_static/auth.js")
    source += demodulize(_ROOT / "shared_static/cards.js")
    source += demodulize(_ROOT / "shared_static/list_cache.js")
    if console_loader:
        app = (_ROOT / "console/static/app.js").read_text()
        for signature in (
            "function loadSavedCoordinators() {",
            "function _savedAuthChanged() {",
            "function _canActOnSavedSession(session) {",
            "function _initSavedCoordTable() {",
        ):
            source += extract_braced(app, signature)
        source += "\nlet _coordTable = null;\n" + _CONSOLE_SAVED
    if ui_loader:
        app = (_ROOT / "ui/static/app.js").read_text()
        for signature in (
            "function _initSavedWsTable() {",
            "function loadDashboard() {",
            "function loadSavedWorkstreams() {",
        ):
            source += extract_braced(app, signature)
        source += "\nlet _wsTable = null, dashboardVisible = false;\n" + _NODE_SAVED
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
const one = {workstreams:[{ws_id:'one', name:'One'}], total:1};
saved.load(); reply(requests.shift(), one); await drain();
let row = elements.get('saved-coord-cards').children[0];
assert.equal(row.getAttribute('role'), 'group');
assert.equal(row.getAttribute('tabindex'), null);
assert.equal(row.getAttribute('aria-label'), 'One');
row.onclick(); row.onkeydown({key:'Enter', preventDefault(){}});
assert.equal(activations, 0);
assert.equal(button.style.display, 'none');
saved.controller.start(); assert.equal(saved.controller.inMode(), false);
_storePermissions(operator);
saved.load(); reply(requests.shift(), one); await drain();
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
sessionStorage.setItem('turnstone_can_refresh', 'true');
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


def test_sign_in_screen_closes_modal_dialogs_only():
    """A modal dialog sits in the top layer, above the sign-in overlay, and while it is open the
    sign-in form takes no input (#1332), so showing the overlay closes every modal dialog.  A
    non-modal shelf stays open: the opaque overlay already covers it, and closing it here would
    bypass the shelf controller's own bookkeeping (inert siblings, scrim, Escape listener)."""
    _run(r"""
const closed = [];
const modal = {close() { closed.push('modal'); }};
const shelf = {close() { closed.push('shelf'); }};
// Answers as the engine would: both are open dialogs, only the first is modal.
document.querySelectorAll = sel =>
  sel.includes(':modal') ? [modal] : sel.startsWith('dialog') ? [modal, shelf] : [];
element('login-overlay');
showLogin();
assert.deepEqual(closed, ['modal']);
assert.equal(elements.get('login-overlay').style.display, 'flex');
""")


def test_sign_in_screen_shows_on_an_engine_without_the_modal_selector():
    """An engine without :modal rejects the selector.  The sign-in screen must still show; its
    dialogs stay open, as they did before the screen closed them."""
    _run(r"""
document.querySelectorAll = sel => {
  if (sel.includes(':modal')) throw new SyntaxError(`'${sel}' is not a valid selector`);
  return [];
};
element('login-overlay');
showLogin();
assert.equal(elements.get('login-overlay').style.display, 'flex');
""")


def test_escape_in_the_sign_in_screen_stays_there():
    """Escape pressed in the sign-in screen clears its error and stops at the overlay.  Page
    shortcuts and an open shelf behind it must not act on it: the shelf controller closes the
    topmost shelf on any Escape that reaches the document."""
    _run(r"""
function escapeHtml(text) { return text; }
function setSafeHtml(el, html) { el.textContent = html; }
const append = document.body.appendChild.bind(document.body);
document.body.appendChild = el => { elements.set(el.id, el); return append(el); };
window.location = {search:'', pathname:'/'};
['login-box', 'toggle-token', 'setup-fields', 'login-fields', 'token-fields',
 'login-toggle', 'login-subtitle', 'login-submit'].forEach(element);
const error = element('login-error');
initLogin();
error.style.display = 'block'; error.textContent = 'Invalid credentials';
function press(key) {
  let stopped = 0;
  for (const {fn} of elements.get('login-overlay')._listeners.get('keydown'))
    fn({key, stopPropagation() { stopped++; }});
  return stopped;
}
assert.equal(press('a'), 0);
assert.equal(error.textContent, 'Invalid credentials');
assert.equal(press('Escape'), 1);
assert.equal(error.style.display, 'none');
assert.equal(error.textContent, '');
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
_consoleSaved(); onAuthChange(_savedAuthChanged);
const error = elements.get('coord-saved-error');
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
assert.equal(elements.get('saved-coordinators').style.display, '', 'the error stays in view');
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


def test_console_saved_loader_fetches_the_table_query():
    _run(
        r"""
reply(requests.shift(), operator); await drain();
_consoleSaved();
const sec = elements.get('saved-coordinators');
const filter = elements.get('coord-filter'), pager = elements.get('coord-pagination');
loadSavedCoordinators();
let request = requests.shift();
assert.equal(request.url, '/v1/api/workstreams/saved?limit=20&sort=updated&order=desc');
reply(request, {workstreams:[{ws_id:'a'}], total:30, limit:20, offset:0}); await drain();
assert.equal(sec.style.display, '');
pager.children[2].onclick();
request = requests.shift();
assert.equal(
  request.url, '/v1/api/workstreams/saved?limit=20&offset=20&sort=updated&order=desc');
reply(request, {workstreams:[{ws_id:'b'}], total:30, limit:20, offset:20}); await drain();
assert.equal(elements.get('saved-coord-cards').children[0].dataset.wsId, 'b');
globalThis.setTimeout = fn => { fn(); return 1; };
filter.value = 'zzz'; filter.dispatch('input');
request = requests.shift();
assert.equal(request.url, '/v1/api/workstreams/saved?limit=20&q=zzz&sort=updated&order=desc');
reply(request, {workstreams:[], total:0, limit:20, offset:0}); await drain();
assert.equal(sec.style.display, '', 'an empty search keeps its search box on screen');
filter.value = ''; filter.dispatch('input');
reply(requests.shift(), {workstreams:[], total:0, limit:20, offset:0}); await drain();
assert.equal(sec.style.display, 'none');
""",
        console_loader=True,
    )


def test_console_first_page_lands_in_the_columns_that_fit():
    """The section shows before the page is drawn, so the first paint already
    has the columns that fit, not every column of a hidden table."""
    _run(
        r"""
reply(requests.shift(), operator); await drain();
_consoleSaved();
const sec = elements.get('saved-coordinators');
sec.style.display = 'none';  // as the markup has it
Object.defineProperty(elements.get('saved-coord-cards'), 'clientWidth', {
  get: () => (sec.style.display === 'none' ? 0 : 400),
});
loadSavedCoordinators();
reply(requests.shift(), {workstreams:[{ws_id:'a', name:'A'}], total:1, limit:20, offset:0});
await drain();
assert.deepEqual(
  elements.get('coord-saved-colheaders').children.map(h => h.dataset.focusKey),
  ['sort:name', 'sort:context_ratio', 'sort:updated'],
);
""",
        console_loader=True,
    )


def test_standalone_saved_failure_for_an_abandoned_query_is_ignored():
    _run(
        r"""
const cards = _nodeSaved();
const pager = elements.get('ws-pagination'), filter = elements.get('ws-filter');
const error = elements.get('ws-saved-error');
reply(requests.shift(), operator); await drain();
reply(requests.shift(), {workstreams:[{ws_id:'live'}]});
const first = requests.shift();
assert.equal(first.url, '/v1/api/workstreams/saved?limit=20&sort=updated&order=desc');
reply(first, {workstreams:[{ws_id:'saved-1'}], total:25, limit:20, offset:0}); await drain();
pager.children[2].onclick();
const abandoned = requests.shift();
assert.equal(
  abandoned.url, '/v1/api/workstreams/saved?limit=20&offset=20&sort=updated&order=desc');
globalThis.setTimeout = fn => { fn(); return 1; };
filter.value = 'notes'; filter.dispatch('input');
assert.equal(requests.length, 0, 'the search waits for the request out');
reply(abandoned, {error:'busy'}, 503); await drain();
assert.notEqual(error.hidden, false, 'no error for a page the user left');
const current = requests.shift();
assert.equal(current.url, '/v1/api/workstreams/saved?limit=20&q=notes&sort=updated&order=desc');
reply(current, {workstreams:[{ws_id:'match'}], total:1, limit:20, offset:0}); await drain();
assert.equal(cards.children[0].dataset.wsId, 'match');
""",
        ui_loader=True,
    )


def test_standalone_saved_reloads_ask_once_more_after_the_request_out():
    _run(
        r"""
const cards = _nodeSaved();
reply(requests.shift(), operator); await drain();
reply(requests.shift(), {workstreams:[]});
const first = requests.shift();
loadSavedWorkstreams(); loadSavedWorkstreams();
assert.equal(requests.length, 0, 'one request at a time');
reply(first, {workstreams:[{ws_id:'old'}], total:1, limit:20, offset:0}); await drain();
assert.equal(cards.children[0].dataset.wsId, 'old');
const second = requests.shift();
assert.equal(second.url, first.url);
assert.equal(requests.length, 0);
reply(second, {workstreams:[{ws_id:'fresh'}], total:1, limit:20, offset:0}); await drain();
assert.equal(cards.children[0].dataset.wsId, 'fresh');
assert.equal(requests.length, 0);
""",
        ui_loader=True,
    )


def test_standalone_saved_reload_waits_for_delete_mode():
    _run(
        r"""
const cards = _nodeSaved();
reply(requests.shift(), operator); await drain();
reply(requests.shift(), {workstreams:[]});
reply(requests.shift(), {workstreams:[{ws_id:'a'}], total:1, limit:20, offset:0}); await drain();
_wsTable.controller.start();
loadDashboard();
assert.equal(requests.shift().url, '/v1/api/dashboard');
assert.equal(requests.length, 0, 'the saved page waits while rows are selected');
assert.equal(cards.children[0].dataset.wsId, 'a');
_wsTable.controller.cancel();
assert.equal(requests.shift().url, '/v1/api/workstreams/saved?limit=20&sort=updated&order=desc');
""",
        ui_loader=True,
    )


def test_console_saved_answers_for_an_abandoned_query_change_nothing():
    _run(
        r"""
reply(requests.shift(), operator); await drain();
_consoleSaved();
const sec = elements.get('saved-coordinators'), error = elements.get('coord-saved-error');
const filter = elements.get('coord-filter');
loadSavedCoordinators();
reply(requests.shift(), {workstreams:[{ws_id:'a'}], total:30, limit:20, offset:0}); await drain();
globalThis.setTimeout = fn => { fn(); return 1; };

// A reload of the unfiltered list is in flight when the user searches.
loadSavedCoordinators();
const unfiltered = requests.shift();
filter.value = 'zzz'; filter.dispatch('input');
assert.equal(requests.length, 0, 'the search waits for the request in flight');
// Its answer (everything deleted meanwhile) must not hide the search box.
reply(unfiltered, {workstreams:[], total:0, limit:20, offset:0}); await drain();
assert.equal(sec.style.display, '');
assert.equal(elements.get('saved-coord-cards').children[0].dataset.wsId, 'a');
const search = requests.shift();
assert.equal(search.url, '/v1/api/workstreams/saved?limit=20&q=zzz&sort=updated&order=desc');

// The user clears the search before it answers, and it fails.
filter.value = ''; filter.dispatch('input');
reply(search, {error:'down'}, 503); await drain();
assert.notEqual(error.hidden, false, 'no error for a search the user left');
assert.equal(elements.get('saved-coord-cards').children[0].dataset.wsId, 'a');
const current = requests.shift();
assert.equal(current.url, '/v1/api/workstreams/saved?limit=20&sort=updated&order=desc');
reply(current, {workstreams:[{ws_id:'b'}], total:1, limit:20, offset:0}); await drain();
assert.equal(elements.get('saved-coord-cards').children[0].dataset.wsId, 'b');
""",
        console_loader=True,
    )


def test_standalone_saved_failure_offers_retry_and_drops_the_rows():
    _run(
        r"""
const cards = _nodeSaved();
const footer = elements.get('ws-saved-footer'), pager = elements.get('ws-pagination');
const error = elements.get('ws-saved-error'), errorText = elements.get('ws-saved-error-text');
reply(requests.shift(), operator); await drain();
reply(requests.shift(), {workstreams:[]});
reply(requests.shift(), {workstreams:[{ws_id:'saved-1'}], total:45, limit:20, offset:0});
await drain();
pager.children[2].onclick();
reply(requests.shift(), {error:'busy'}, 503); await drain();
assert.equal(error.hidden, false);
assert.equal(errorText.textContent, 'Could not load saved workstreams (503).');
assert.equal(cards.style.display, 'none');
assert.equal(footer.style.display, 'none');
// Names arriving later re-render the table: the old rows stay gone.
_wsTable.render();
assert.equal(cards.children[0].dataset.wsId, undefined);
// Retry asks for the page that failed and clears the error once it lands.
loadSavedWorkstreams();
const retry = requests.shift();
assert.equal(retry.url, '/v1/api/workstreams/saved?limit=20&offset=20&sort=updated&order=desc');
reply(retry, {workstreams:[{ws_id:'saved-21'}], total:45, limit:20, offset:20}); await drain();
assert.equal(error.hidden, true);
assert.equal(cards.style.display, '');
assert.equal(cards.children[0].dataset.wsId, 'saved-21');
""",
        ui_loader=True,
    )


def test_console_refresh_waiting_on_delete_mode_runs_however_it_ends():
    _run(
        r"""
reply(requests.shift(), operator); await drain();
element('coord-delete-btn');
_consoleSaved();
loadSavedCoordinators();
reply(requests.shift(), {workstreams:[{ws_id:'a'}], total:1, limit:20, offset:0}); await drain();
_coordTable.controller.start();
loadSavedCoordinators();  // a session closed elsewhere
assert.equal(requests.length, 0, 'rows being selected stay put');
// The section toggle's own cancel, not the delete bar's.
_coordTable.controller.cancel();
assert.equal(requests.length, 1, 'the waiting refresh runs');
reply(requests.shift(), {workstreams:[{ws_id:'a'}, {ws_id:'b'}], total:2, limit:20, offset:0});
await drain();
assert.equal(elements.get('saved-coord-cards').children.length, 2);

// An answer that lands after delete mode began waits for it too.
loadSavedCoordinators();
const inFlight = requests.shift();
_coordTable.controller.start();
reply(inFlight, {workstreams:[{ws_id:'c'}], total:1, limit:20, offset:0}); await drain();
assert.equal(elements.get('saved-coord-cards').children.length, 2);
assert.equal(requests.length, 0);
_coordTable.controller.cancel();
assert.equal(requests.length, 1);
""",
        console_loader=True,
    )


def test_standalone_saved_failure_during_delete_mode_waits_for_it():
    _run(
        r"""
const cards = _nodeSaved();
element('ws-delete-btn');
const error = elements.get('ws-saved-error');
reply(requests.shift(), operator); await drain();
reply(requests.shift(), {workstreams:[]});
reply(requests.shift(), {workstreams:[{ws_id:'a'}, {ws_id:'b'}], total:2, limit:20, offset:0});
await drain();
loadSavedWorkstreams();
const inFlight = requests.shift();
_wsTable.controller.start(); _wsTable.controller.toggleAll();
reply(inFlight, {error:'busy'}, 503); await drain();
assert.equal(_wsTable.controller.isSelected('a'), true, 'the selection survives');
assert.notEqual(error.hidden, false);
assert.equal(cards.children.length, 2);
_wsTable.controller.cancel();
assert.equal(requests.length, 1, 'asked again once delete mode ends');
""",
        ui_loader=True,
    )


def test_console_saved_failure_during_delete_mode_waits_for_it():
    _run(
        r"""
reply(requests.shift(), operator); await drain();
element('coord-delete-btn');
_consoleSaved();
const error = elements.get('coord-saved-error');
loadSavedCoordinators();
reply(requests.shift(), {workstreams:[{ws_id:'a'}], total:1, limit:20, offset:0}); await drain();
loadSavedCoordinators();
const inFlight = requests.shift();
_coordTable.controller.start(); _coordTable.controller.toggleAll();
reply(inFlight, {error:'down'}, 503); await drain();
assert.equal(_coordTable.controller.isSelected('a'), true, 'the selection survives');
assert.notEqual(error.hidden, false);
_coordTable.controller.cancel();
assert.equal(requests.length, 1, 'asked again once delete mode ends');
""",
        console_loader=True,
    )


def test_standalone_first_page_waits_for_the_names_it_shows():
    _run(
        r"""
let names;
window.TurnstoneProjects = {
  refreshProjects: () => new Promise(resolve => { names = resolve; }),
  onProjectsChange() {},
};
const cards = _nodeSaved();
reply(requests.shift(), operator); await drain();
reply(requests.shift(), {workstreams:[]});
reply(requests.shift(), {workstreams:[{ws_id:'a'}], total:1, limit:20, offset:0}); await drain();
assert.equal(cards.children[0].textContent, 'Loading…');
names([]); await drain();
assert.equal(cards.children[0].dataset.wsId, 'a');
""",
        ui_loader=True,
    )


def test_standalone_reload_leaves_the_rows_on_screen():
    """Only a table with no page yet says it is loading; names arriving first
    do not turn that into an empty list, and a reload leaves the page on
    screen until the fresh one replaces it."""
    _run(
        r"""
const cards = _nodeSaved();
const footer = elements.get('ws-saved-footer');
reply(requests.shift(), operator); await drain();
assert.equal(cards.children[0].textContent, 'Loading…');
_wsTable.render();  // the project or persona names arrive first
assert.equal(cards.children[0].textContent, 'Loading…');
assert.equal(footer.textContent, '');
reply(requests.shift(), {workstreams:[]});
reply(requests.shift(), {workstreams:[{ws_id:'a'}], total:1, limit:20, offset:0}); await drain();
loadDashboard();
assert.equal(cards.children[0].dataset.wsId, 'a');
""",
        ui_loader=True,
    )
