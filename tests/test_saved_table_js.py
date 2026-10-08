"""Drive the server-paged saved-session table (``shared_static/cards.js``) under Node.

The table holds the query (page, search, sort) and fetches each page itself; the
server pages, searches and sorts (#1268). These tests answer its requests by
hand to pin the query each control asks for, that one request is out at a time,
that an answer for an abandoned query is dropped, that a page emptied by
deletions steps back, that delete mode holds every fetch until it ends, and what
the user sees and hears while a page loads: the pending rows, where keyboard
focus lands, and the screen-reader text.
"""

import json
import re
from pathlib import Path

from tests._js_harness_helpers import (
    FAKE_DOM,
    demodulize,
    extract_braced,
    node_skip,
    run_node_source,
)
from turnstone.core.workstream import SAVED_SEARCH_MAX_CHARS, SAVED_WORKSTREAM_SORT_KEYS

pytestmark = node_skip
_ROOT = Path(__file__).resolve().parents[1] / "turnstone"
_SETUP = r"""
import assert from 'node:assert/strict';
globalThis.window = {};
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
let timers = [];
globalThis.setTimeout = fn => { timers.push(fn); return timers.length; };
globalThis.clearTimeout = () => {};
const flushTimers = () => { const due = timers; timers = []; due.forEach(fn => fn()); };
function showToast() {}
function formatRelativeTime() { return ''; }
// utils.js escapes the quote and backslash a focus key could carry; so does this.
const cssEscape = value => JSON.stringify(String(value)).slice(1, -1);
// The saved-list requests the table makes, answered by each test; deletes
// succeed at once.
const requests = [];
function authFetch(url) {
  if (!url.startsWith('/v1/api/workstreams/saved')) {
    return Promise.resolve({ok: true, status: 200, headers: {get: () => ''}});
  }
  return new Promise(resolve => requests.push({url, resolve}));
}
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(setImmediate); };
async function answer(page, total, request = requests.shift()) {
  request.resolve({ok: true, status: 200, json: async () => ({workstreams: page, total})});
  await settle();
}
async function fail(status, request = requests.shift()) {
  request.resolve({ok: false, status, json: async () => ({})});
  await settle();
}
const asked = ({offset = 0, q = '', sort = 'updated', order = 'desc'} = {}) =>
  savedListUrl({limit: 20, offset, q, sort, order});
function element(id) { const el = new FakeElement('div'); elements.set(id, el); return el; }
const rows = (from, count) =>
  Array.from({length: count}, (_, i) => ({ws_id: 'ws-' + (from + i), name: 'n' + (from + i)}));
let mayDelete = true;
const table = createSavedTable({
  headerEl: element('headers'), bodyEl: element('body'), footerEl: element('footer'),
  paginationEl: element('pager'), filterEl: element('filter'),
  errorEl: element('error'), errorTextEl: element('error-text'),
  columns: [SavedColumns.name(), SavedColumns.last()], noun: 'session',
  canDeleteAny: () => mayDelete,
  delete: {idPrefix: 'del', buttonId: 'del-button', buildDeleteRequest: () => ({url: '/x'})},
});
// The markup's error block starts hidden, with its Retry button inside.
const retryButton = Object.assign(new FakeElement('button'), {textContent: 'Retry'});
elements.get('error').hidden = true;
elements.get('error').appendChild(retryButton);
const pager = elements.get('pager');
const body = elements.get('body');
const footer = elements.get('footer');
const pageLabel = () => pager.children[1].textContent;
const next = () => pager.children[2].onclick();
const pending = () => body.classList.contains('is-pending') && body.getAttribute('aria-busy') === 'true';
async function open(page, total) { table.load(); await answer(page, total); }
"""


def _run(body: str) -> None:
    source = FAKE_DOM + _SETUP.replace(
        "let mayDelete", demodulize(_ROOT / "shared_static/cards.js") + "\nlet mayDelete", 1
    )
    result = run_node_source(source + "\n" + body)
    assert result.returncode == 0, result.stderr


def test_pager_asks_for_the_next_page():
    _run(r"""
table.load();
assert.equal(body.children[0].textContent, 'Loading…');
assert.equal(footer.textContent, '');
assert.equal(requests[0].url, '/v1/api/workstreams/saved?limit=20&sort=updated&order=desc');
await answer(rows(0, 20), 45);
assert.equal(body.children.length, 20);
assert.equal(footer.textContent, 'Showing 1–20 of 45 sessions');
assert.equal(pageLabel(), '1 / 3');

next();
assert.equal(requests[0].url, '/v1/api/workstreams/saved?limit=20&offset=20&sort=updated&order=desc');
// Until the new page arrives the pager still describes the rows on screen.
assert.equal(pageLabel(), '1 / 3');
await answer(rows(20, 20), 45);
assert.equal(pageLabel(), '2 / 3');
assert.equal(footer.textContent, 'Showing 21–40 of 45 sessions');
assert.equal(body.children[0].dataset.wsId, 'ws-20');
""")


def test_one_request_is_out_at_a_time():
    """Refreshes coalesce into one more request; a click waits for the one
    out, and an answer for a query the user left is dropped."""
    _run(r"""
await open(rows(0, 20), 45);
table.load(); table.load(); table.load();
assert.equal(requests.length, 1);
await answer(rows(0, 20), 45);
assert.equal(requests.length, 1, 'one more after it, however many calls came');
await answer(rows(0, 20), 45);
assert.equal(requests.length, 0);

// The user pages while a refresh is out: the click waits for it.
table.load();
const refresh = requests.shift();
next();
assert.equal(requests.length, 0);
// A repeat of the click asks for nothing more.
next();
await answer([{ws_id: 'stale'}], 45, refresh);
assert.equal(body.children[0].dataset.wsId, 'ws-0', 'the refresh was for the page left');
assert.equal(requests.length, 1);
assert.equal(requests[0].url, asked({offset: 20}));
await answer(rows(20, 20), 45);
assert.equal(pageLabel(), '2 / 3');
assert.equal(requests.length, 0);
""")


def test_search_and_sort_start_from_the_first_page():
    _run(r"""
await open(rows(0, 20), 45);
next(); await answer(rows(20, 20), 45);

const filter = elements.get('filter');
filter.value = '  notes ';
filter.dispatch('input'); flushTimers();
assert.equal(requests[0].url, '/v1/api/workstreams/saved?limit=20&q=notes&sort=updated&order=desc');
await answer([], 0);
assert.equal(body.children[0].textContent, 'No sessions match “notes”');
// The live footer says so to screen readers only.
assert.equal(footer.textContent, '');
assert.equal(footer.children.at(-1).className, 'sr-only');
assert.equal(footer.children.at(-1).textContent, 'No sessions match “notes”');
// The same settled text is not a new search.
filter.dispatch('input'); flushTimers();
assert.equal(requests.length, 0);

table.load();
await answer(rows(0, 3), 3);
assert.equal(footer.textContent, '3 sessions match “notes”');
elements.get('headers').children[0].onclick();  // NAME: text sorts A→Z first
assert.equal(requests[0].url, asked({q: 'notes', sort: 'name', order: 'asc'}));
// A button does not expose aria-sort, so the name carries the order.
const name = elements.get('headers').children[0];
assert.equal(name.getAttribute('aria-sort'), null);
assert.equal(name.getAttribute('aria-label'), 'Sort by NAME, sorted ascending');
assert.equal(elements.get('headers').children[1].getAttribute('aria-label'), 'Sort by LAST');
""")


def test_a_page_emptied_by_deletion_steps_back():
    _run(r"""
await open(rows(0, 20), 41);
next(); await answer(rows(20, 20), 41);
next(); await answer(rows(40, 1), 41);
assert.equal(pageLabel(), '3 / 3');
// Its only row was deleted elsewhere: the third page no longer exists.
table.load();
await answer([], 40);
assert.equal(requests[0].url, asked({offset: 20}));
await answer(rows(20, 20), 40);
assert.equal(pageLabel(), '2 / 2');
""")


def test_an_emptied_list_steps_straight_back_to_the_first_page():
    _run(r"""
await open(rows(0, 20), 45);
next(); await answer(rows(20, 20), 45);
next(); await answer(rows(40, 5), 45);
// Everything was deleted elsewhere: one request, straight to the first page.
table.load();
await answer([], 0);
assert.equal(requests.length, 1);
assert.equal(requests[0].url, asked());
await answer([], 0);
assert.equal(requests.length, 0);
assert.equal(body.children[0].textContent, 'No saved items');
""")


def test_an_empty_page_the_count_still_covers_is_not_refetched():
    """Rows can vanish between the count and the page query; show the empty
    page rather than asking for the same page again and again."""
    _run(r"""
await open(rows(0, 20), 45);
next(); await answer(rows(20, 20), 45);
next();
// The count still reaches this last page, but its rows are gone.
await answer([], 45);
assert.equal(requests.length, 0);
assert.equal(footer.textContent, '45 sessions');
// Not "no saved sessions": the list goes on, only not on this page.
assert.equal(body.children[0].textContent, 'No sessions on this page');
""")


def test_a_short_total_never_hides_rows_on_screen():
    _run(r"""
await open(rows(0, 3), undefined);
assert.equal(body.children.length, 3);
assert.equal(footer.textContent, '3 sessions');
""")


def test_a_failed_load_shows_the_error_and_drops_the_rows():
    _run(r"""
await open(rows(0, 20), 45);
next();
await fail(503);
assert.equal(elements.get('error').hidden, false);
assert.equal(elements.get('error-text').textContent, 'Could not load saved sessions (503).');
assert.equal(body.style.display, 'none');
assert.equal(footer.style.display, 'none');
// A re-render behind the error (names arriving) shows no old rows.
table.render();
assert.equal(body.children[0].dataset.wsId, undefined);
// Retry asks for the page that failed, says so while it is out, and clears
// the error once the page lands.
table.load();
assert.equal(requests[0].url, asked({offset: 20}));
assert.equal(retryButton.textContent, 'Retrying…');
assert.equal(elements.get('error').getAttribute('aria-busy'), 'true');
await fail(503);
assert.equal(retryButton.textContent, 'Retry', 'a second failure says Retry again');
assert.equal(elements.get('error').getAttribute('aria-busy'), null);
table.load();
await answer(rows(20, 20), 45);
assert.equal(elements.get('error').hidden, true);
assert.equal(retryButton.textContent, 'Retry');
// A refresh with no error showing leaves the hidden button alone.
table.load();
assert.equal(retryButton.textContent, 'Retry');
await answer(rows(20, 20), 45);
assert.equal(body.style.display, '');
assert.equal(body.children[0].dataset.wsId, 'ws-20');
""")


def test_delete_mode_holds_every_fetch_until_it_ends():
    _run(r"""
element('del-button');
await open(rows(0, 20), 45);
table.controller.start();
assert.equal(elements.get('filter').disabled, true);
assert.equal(pager.style.display, 'none');
const header = elements.get('headers').children[0];
assert.equal(header.getAttribute('aria-disabled'), 'true');
// Both say why they stopped responding.
const hint = 'Search and sort pause while you select rows to delete';
assert.equal(elements.get('filter').title, hint);
assert.equal(header.title, hint);
header.onclick();
// A refresh (a session closed elsewhere) waits as well.
table.load();
assert.equal(requests.length, 0);
table.controller.cancel();
assert.equal(elements.get('filter').disabled, false);
assert.equal(elements.get('filter').title, '');
assert.equal(elements.get('headers').children[0].getAttribute('aria-disabled'), null);
assert.equal(elements.get('headers').children[0].title, '');
assert.equal(requests.length, 1, 'one fetch once delete mode ends');
assert.equal(requests[0].url, asked(), 'the sort did not change');
table.render();
assert.equal(requests.length, 1);

// An answer that lands after delete mode began waits for it too.
await answer(rows(0, 20), 45);
table.load();
const inFlight = requests.shift();
table.controller.start(); table.controller.toggleAll();
await answer([{ws_id: 'other'}], 1, inFlight);
assert.equal(table.controller.isSelected('ws-0'), true, 'the selection survives');
assert.equal(body.children.length, 20);
assert.equal(requests.length, 0);
table.controller.cancel();
assert.equal(requests.length, 1);
// So does a failure.
table.controller.start();
await fail(503);
assert.equal(elements.get('error').hidden, true);
assert.equal(body.children.length, 20);
table.controller.cancel();
assert.equal(requests.length, 1, 'asked again once delete mode ends');
""")


def test_a_search_settling_in_delete_mode_waits_for_it_to_end():
    _run(r"""
element('del-button');
await open(rows(0, 20), 45);
const filter = elements.get('filter');
filter.value = 'notes'; filter.dispatch('input');
table.controller.start();
flushTimers();  // the search settles after rows were selected
assert.equal(requests.length, 0);
table.controller.cancel();
assert.equal(requests.length, 1, 'one fetch once delete mode ends');
assert.equal(requests[0].url, asked({q: 'notes'}));
""")


def test_reset_forgets_the_search_page_and_answers_still_out():
    _run(r"""
await open(rows(0, 20), 45);
next(); await answer(rows(20, 20), 45);
const filter = elements.get('filter');
filter.value = 'notes'; filter.dispatch('input'); flushTimers();
await answer(rows(0, 3), 3);
table.reset();
assert.equal(filter.value, '');
assert.equal(body.children[0].textContent, 'Loading…');
table.load();
assert.equal(requests[0].url, asked(), 'the search and page are gone');
await answer(rows(0, 20), 45);

// A refresh is out when the identity changes again.  Its answer, though for
// the very query the table asks for next, changes nothing and asks for nothing.
table.load();
const old = requests.shift();
table.reset();
table.load();
await answer([{ws_id: 'stale'}], 1, old);
assert.equal(body.children[0].textContent, 'Loading…');
assert.equal(requests.length, 1);
await answer([{ws_id: 'current'}], 1);
assert.equal(body.children[0].dataset.wsId, 'current');
assert.equal(requests.length, 0);
""")


def test_load_waits_for_what_the_first_page_needs():
    _run(r"""
let names;
table.load(new Promise(resolve => { names = resolve; }));
await answer(rows(0, 3), 3);
assert.equal(body.children[0].textContent, 'Loading…');
names(); await settle();
assert.equal(body.children.length, 3);
""")


def test_rows_are_pending_until_their_page_arrives():
    _run(r"""
await open(rows(0, 20), 45);
assert.equal(pending(), false);
// A refresh leaves the rows on screen as they are.
table.load();
assert.equal(pending(), false);
await answer(rows(0, 20), 45);
next();
assert.equal(pending(), true, 'the rows on screen are about to be replaced');
await answer(rows(20, 20), 45);
assert.equal(pending(), false);
assert.equal(body.getAttribute('aria-busy'), null);

// A failed fetch drops them along with the rows.
next();
await fail(503);
assert.equal(pending(), false);

// Rows selected for deletion never look pending; their fetch waits.
element('del-button');
table.load(); await answer(rows(40, 5), 45);
pager.children[0].onclick();
assert.equal(pending(), true);
table.controller.start();
assert.equal(pending(), false);
""")


def test_prev_and_next_step_from_the_page_on_screen():
    """A request still out must not move where they lead."""
    _run(r"""
await open(rows(0, 20), 80);
next(); await answer(rows(20, 20), 80);
assert.equal(pageLabel(), '2 / 4');
next();
assert.equal(requests[0].url, asked({offset: 40}));
next();
assert.equal(requests.length, 1, 'Next again asks for the page already out');
pager.children[0].onclick();
// The page out is one the user has left; Prev led back from the page on screen.
await answer(rows(40, 20), 80);
assert.equal(requests[0].url, asked());
await answer(rows(0, 20), 80);
assert.equal(pageLabel(), '1 / 4');
""")


def test_focus_moves_to_the_rebuilt_control():
    _run(r"""
await open(rows(0, 20), 45);
pager.children[2].focus();
next();
await answer(rows(20, 20), 45);
assert.equal(document.activeElement, pager.children[2], 'focus stays on Next');
next();
await answer(rows(40, 5), 45);
assert.equal(pager.children[2].disabled, true);
assert.equal(document.activeElement, pager.children[0], 'the last page hands focus to Prev');

// Sorting from the keyboard keeps focus on the header.
const headers = elements.get('headers');
headers.children[0].focus();
headers.children[0].onkeydown({key: 'Enter', preventDefault() {}});
assert.equal(document.activeElement, headers.children[0]);
assert.equal(document.activeElement.getAttribute('aria-label'), 'Sort by NAME, sorted ascending');
await answer(rows(0, 20), 45);
assert.equal(document.activeElement, headers.children[0]);

// A refresh that reorders the rows keeps focus on the same row.
body.children[3].focus();
table.load();
await answer(rows(0, 20).reverse(), 45);
assert.equal(document.activeElement, body.children[16]);
assert.equal(document.activeElement.dataset.wsId, 'ws-3');

// Focus outside the table is left alone.
const filter = elements.get('filter');
filter.focus();
table.render();
assert.equal(document.activeElement, filter);
""")


def test_the_delete_button_follows_the_list_not_the_page():
    """Its place in the toolbar depends only on the viewer, so the search box
    never moves; whether it shows depends on the list, not the page."""
    _run(r"""
const button = element('del-button');
const shown = () => button.style.display === '' && button.style.visibility === '';
await open(rows(0, 20), 45);
assert.equal(shown(), true);
// A search that matches nothing keeps it.
const filter = elements.get('filter');
filter.value = 'zzz'; filter.dispatch('input'); flushTimers();
await answer([], 0);
assert.equal(shown(), true);
// An empty list hides it but keeps its place; so does a failed load.
filter.value = ''; filter.dispatch('input'); flushTimers();
await answer([], 0);
assert.equal(button.style.display, '');
assert.equal(button.style.visibility, 'hidden');
table.load(); await answer(rows(0, 20), 45);
table.load(); await fail(503);
assert.equal(button.style.display, '');
assert.equal(button.style.visibility, 'hidden');
// A viewer who may not delete gets no button, and no place for one.
mayDelete = false; table.render();
assert.equal(button.style.display, 'none');
""")


def test_search_waits_for_an_input_method_to_commit():
    _run(r"""
await open(rows(0, 20), 45);
const filter = elements.get('filter');
const onInput = filter._listeners.get('input')[0].fn;
filter.value = 'nih';
onInput({isComposing: true}); flushTimers();
assert.equal(requests.length, 0, 'text still being composed is not a search');
filter.value = 'nihon';
filter.dispatch('compositionend'); flushTimers();
assert.equal(requests.length, 1);
assert.equal(requests[0].url, asked({q: 'nihon'}));
""")


_DELETE_DIALOG = r"""
element('del-button'); element('del-dialog');
['bar', 'bar-count', 'bar-delete', 'bar-select-all', 'error', 'count', 'list', 'meta',
 'confirm-btn'].forEach(id => element('del-' + id));
let closeResults = null;
window.TurnstoneHatch = {
  openDialog(dialog, options) { closeResults = options.onClose; },
  setBusy() {},
};
const click = row => row.onclick({target: null, stopPropagation() {}});
const deleteSelected = async () => {
  table.controller.confirmSelection();
  table.controller.confirm();
  for (let i = 0; i < 20; i++) await Promise.resolve();
  assert.equal(requests.length, 0, 'nothing is fetched while the results are open');
  closeResults();
};
"""


def test_closing_the_delete_results_drops_the_rows_and_fetches_once():
    _run(
        _DELETE_DIALOG
        + r"""
await open(rows(0, 20), 45);
table.controller.start();
table.load();  // a session closed elsewhere while rows are selected
click(body.children[0]); click(body.children[1]);
await deleteSelected();
assert.equal(table.controller.inMode(), false);
assert.equal(requests.length, 1, 'one fetch covers the waiting refresh and the deletions');
assert.equal(body.children.length, 18);
assert.equal(body.children[0].dataset.wsId, 'ws-2');
assert.equal(footer.textContent, 'Showing 1–18 of 43 sessions');
assert.equal(pending(), true, 'the rest wait for the fresh page');
"""
    )


def test_closing_the_delete_results_fetches_even_with_nothing_waiting():
    _run(
        _DELETE_DIALOG
        + r"""
await open(rows(0, 20), 45);
table.controller.start();
click(body.children[0]);
await deleteSelected();
assert.equal(requests.length, 1, 'the deletion alone asks for the page again');
"""
    )


def test_deleting_a_whole_last_page_asks_for_the_page_before_it():
    _run(
        _DELETE_DIALOG
        + r"""
await open(rows(0, 20), 45);
next(); await answer(rows(20, 20), 45);
next(); await answer(rows(40, 5), 45);
table.controller.start(); table.controller.toggleAll();
await deleteSelected();
assert.equal(requests.length, 1, 'one fetch');
assert.equal(requests[0].url, asked({offset: 20}), 'for the last page that still exists');
assert.equal(pageLabel(), '2 / 2');
assert.equal(body.children[0].textContent, 'Loading…');
assert.equal(footer.textContent, '40 sessions');
"""
    )


def test_deleting_a_page_left_by_a_click_names_no_page_past_the_end():
    """Prev was clicked but its page had not landed: the rows deleted are the
    last page's, and the pager must not name a page that no longer exists."""
    _run(
        _DELETE_DIALOG
        + r"""
await open(rows(0, 20), 45);
next(); await answer(rows(20, 20), 45);
next(); await answer(rows(40, 5), 45);
pager.children[0].onclick();
const prev = requests.shift();
table.controller.start(); table.controller.toggleAll();
await answer(rows(20, 20), 45, prev);
await deleteSelected();
assert.equal(pageLabel(), '2 / 2');
assert.equal(requests[0].url, asked({offset: 20}));
"""
    )


def test_columns_drop_by_the_table_width_not_the_window():
    """A side rail or split pane narrows the table as much as a phone does.
    Columns drop one at a time, lowest rank first, until NAME has room for a
    long name and a skill chip; ID goes early, and the sorted column goes
    last."""
    _run(r"""
let resized = null;
globalThis.ResizeObserver = class {
  constructor(callback) { resized = callback; }
  observe() {}
};
globalThis.requestAnimationFrame = fn => { fn(); return 1; };
const wide = createSavedTable({
  headerEl: element('w-headers'), bodyEl: element('w-body'), footerEl: element('w-footer'),
  columns: [
    SavedColumns.name(), SavedColumns.persona(), SavedColumns.project(), SavedColumns.model(),
    SavedColumns.count('message_count', 'MSGS'), SavedColumns.ctx(), SavedColumns.last(),
    SavedColumns.id(),
  ],
  noun: 'session', canDeleteAny: () => true,
  delete: {idPrefix: 'w-del', buttonId: 'w-del-button', buildDeleteRequest: () => ({url: '/x'})},
});
const wheaders = elements.get('w-headers');
const headers = () => wheaders.children.map(h => h.dataset.focusKey.slice(5));
const header = key => wheaders.children.find(h => h.dataset.focusKey === 'sort:' + key);
const wbody = elements.get('w-body');
// An empty table shows no columns, so a resize leaves it alone.
resized([{contentRect: {width: 300}}]);
assert.equal(wbody.children.length, 0);
resized([{contentRect: {width: 1200}}]);
wide.load(); await answer(rows(0, 3), 3);
const all = ['name', 'persona', 'project', 'model', 'message_count', 'context_ratio', 'updated',
  'ws_id'];
assert.deepEqual(headers(), all);
// 646px of fixed columns, 53px of row overhead (padding, border and the
// delete-mode gutter): ID stays while NAME keeps 280px, the rest while it
// keeps 200px.
const at = width => { wbody.clientWidth = width; wide.render(); return headers(); };
const without = (...gone) => all.filter(key => !gone.includes(key));
assert.deepEqual(at(979), all);
assert.deepEqual(at(978), without('ws_id'));
assert.deepEqual(at(823), without('ws_id'));
assert.deepEqual(at(822), without('ws_id', 'message_count'));
assert.deepEqual(at(751), without('ws_id', 'message_count'));
assert.deepEqual(at(750), without('ws_id', 'message_count', 'persona'));
assert.deepEqual(at(641), without('ws_id', 'message_count', 'persona'));
assert.deepEqual(at(640), without('ws_id', 'message_count', 'persona', 'project'));
assert.deepEqual(at(521), without('ws_id', 'message_count', 'persona', 'project'));
assert.deepEqual(at(520), ['name', 'context_ratio', 'updated']);
// NAME, CTX and LAST never go.
assert.deepEqual(at(120), ['name', 'context_ratio', 'updated']);

// A sort that keeps the columns redraws only the headers until its page lands.
at(1200);
const firstRow = wbody.children[0];
header('name').onclick();
assert.equal(wbody.children[0], firstRow);
await answer(rows(0, 3), 3);
// The sorted column goes last: PROJECT leaves before PERSONA now.
header('persona').onclick();
await answer(rows(0, 3), 3);
const wfooter = elements.get('w-footer');
assert.deepEqual(at(700), without('ws_id', 'message_count', 'project'));
assert.equal(wfooter.textContent, '3 sessions');
assert.deepEqual(at(400), ['name', 'context_ratio', 'updated']);
// Once it is gone too, the footer says what order the rows are in.
assert.equal(wfooter.textContent, '3 sessions · sorted by persona, ascending');
// Sorting by another column lets it go, and the rows redraw with the headers.
at(700);
header('updated').onclick();
assert.deepEqual(headers(), without('ws_id', 'message_count', 'persona'));
assert.equal(wbody.style['--saved-grid'], wheaders.style.gridTemplateColumns);
assert.equal(wbody.children[0].dataset.wsId, 'ws-0');
await answer(rows(0, 3), 3);

// Resizing alone re-renders when the columns that fit change.
wbody.clientWidth = 0;
resized([{contentRect: {width: 1200}}]);
assert.deepEqual(headers(), all);
resized([{contentRect: {width: 300}}]);
assert.equal(headers().length, 3);
resized([{contentRect: {width: 0}}]);  // hidden: keeps its last width
wide.render();
assert.equal(headers().length, 3);
""")


def test_a_skill_chip_names_its_skill():
    _run(r"""
await open([{ws_id: 'ws-1', title: 'A long title', launch_skill: 'code-review'}], 1);
const chip = body.querySelector('.skill-chip');
assert.equal(chip.title, 'code-review');
assert.equal(chip.querySelector('.skill-chip-label').textContent, 'code-review');
// A screen reader hears the skill with the row's name, and with its checkbox
// while rows are selected for deletion.
assert.equal(body.children[0].getAttribute('aria-label'), 'Resume: A long title (skill: code-review)');
element('del-button');
table.controller.start();
assert.equal(
  body.querySelector('.ws-card-check').getAttribute('aria-label'),
  'Select A long title (skill: code-review) for deletion',
);
""")


def test_dashboards_ask_only_for_what_the_server_accepts():
    """Every column either dashboard sorts by is a server sort key, and both
    search boxes stop at the server's search limit, so no click or search
    can draw a 400."""
    node_app = (_ROOT / "ui/static/app.js").read_text()
    console_app = (_ROOT / "console/static/app.js").read_text()
    source = (
        FAKE_DOM
        + "globalThis.window = {};\ndocument.getElementById = () => null;\n"
        + "function formatRelativeTime() { return ''; }\n"
        + demodulize(_ROOT / "shared_static/cards.js")
        + extract_braced(node_app, "function _initSavedWsTable() {")
        + extract_braced(console_app, "function _initSavedCoordTable() {")
        + r"""
let _wsTable = null, _coordTable = null;
function onAuthChange() {}
function _canActOnSavedSession() { return true; }
const keys = new Set();
createSavedTable = opts => {
  opts.columns.forEach(column => keys.add(column.key));
  return {render() {}, reset() {}, controller: {}};
};
_initSavedWsTable(); _initSavedCoordTable();
console.log(JSON.stringify([...keys]));
"""
    )
    result = run_node_source(source)
    assert result.returncode == 0, result.stderr
    keys = set(json.loads(result.stdout))
    assert "kind" in keys and "message_count" in keys and "child_count" in keys
    assert keys <= SAVED_WORKSTREAM_SORT_KEYS

    for page in ("ui/static/index.html", "console/static/index.html"):
        assert f'maxlength="{SAVED_SEARCH_MAX_CHARS}"' in (_ROOT / page).read_text(), page


def test_row_overhead_is_what_the_row_css_adds():
    """The column fitting reserves the row's padding, its left border and the
    delete-mode checkbox gutter; those live in CSS, so a change there must
    change ROW_OVERHEAD too."""
    cards_css = (_ROOT / "shared_static/cards.css").read_text()
    base_css = (_ROOT / "shared_static/base.css").read_text()
    cards_js = (_ROOT / "shared_static/cards.js").read_text()

    def rule(css: str, selector: str) -> str:
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert match, selector
        return match.group(1)

    padding = re.search(r"padding:\s*\d+px\s+(\d+)px", rule(cards_css, ".saved-row .dash-row-main"))
    border = re.search(r"border-left:\s*(\d+)px", rule(base_css, ".dash-row "))
    gutter = re.search(
        r"padding-left:\s*(\d+)px", rule(cards_css, ".dash-row.ws-delete-mode .dash-row-main")
    )
    overhead = re.search(r"var ROW_OVERHEAD = (\d+);", cards_js)
    assert padding and border and gutter and overhead
    side = int(padding.group(1))
    assert int(overhead.group(1)) == 2 * side + int(border.group(1)) + int(gutter.group(1)) - side
