"""The web cards' complete view of a tool call and of a long result.

"full call" opens the arguments exactly as the model wrote them (the live
events' ``arguments``, the same text ``/history`` serves), and a result
longer than its box gets its own "show all" control, so checking an argument
never unfolds a large result.  These run the real shared builders, the real
interactive pane, and the coordinator's result placement under node.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._js_harness_helpers import FAKE_DOM, node_skip, run_node_source
from tests._js_harness_helpers import extract_braced as _extract_braced

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1]
_SHARED = _ROOT / "turnstone/shared_static"
_COORDINATOR = _ROOT / "turnstone/console/static/coordinator/coordinator.js"

# The DOM the pane and the builders reach for, past the shared FAKE_DOM.
_DOM_EXTRAS = r"""
const assert = (condition, message) => { if (!condition) throw Error(message); };
const create = document.createElement;
document.createElement = tag => { const el = create(tag); el.style = {}; return el; };
document.body = html;
document.addEventListener = document.removeEventListener = () => {};
document.querySelector = selector => html.querySelector(selector);
document.getElementById = id => html.querySelector('#' + id);
window.matchMedia = () => ({matches: false, addEventListener() {}});
window.removeEventListener = () => {};
window.setTimeout = globalThis.setTimeout = () => 0;
window.clearTimeout = globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 0;
globalThis.clearInterval = globalThis.cancelAnimationFrame = () => {};
globalThis.BroadcastChannel = class {postMessage() {} addEventListener() {} close() {}};
globalThis.CSS = {escape: value => value};
const permissions = new Map([['turnstone_scopes', 'read,write']]);
globalThis.sessionStorage = {getItem: key => permissions.get(key) || null};
const matches = FakeElement.prototype._matches;
FakeElement.prototype._matches = function(selector) {
  return selector.startsWith('#')
    ? (this.id || this.getAttribute('id')) === selector.slice(1)
    : matches.call(this, selector);
};
FakeElement.prototype.after = function(node) {
  const parent = this.parentNode;
  if (!parent || node === this) return;
  if (node.parentNode) node.remove();
  parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
  node.parentNode = parent;
  node._setConnected(parent.isConnected);
};
FakeElement.prototype.insertAdjacentElement = function(where, node) {
  if (where !== 'afterend') throw Error('unexpected insertAdjacentElement ' + where);
  this.after(node);
  return node;
};
FakeElement.prototype.insertBefore = function(node, before) {
  if (!before) return this.appendChild(node);
  if (node === before) return node;
  if (node.parentNode) node.remove();
  this.children.splice(this.children.indexOf(before), 0, node);
  node.parentNode = this;
  node._setConnected(this.isConnected);
  return node;
};
Object.defineProperty(FakeElement.prototype, 'nextElementSibling', {get() {
  return this.parentNode
    ? this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null : null;
}});
Object.defineProperty(FakeElement.prototype, 'nextSibling', {get() {
  return this.nextElementSibling;
}});
Object.defineProperty(FakeElement.prototype, 'firstElementChild', {get() {return this.children[0] || null;}});
Object.defineProperty(FakeElement.prototype, 'lastElementChild', {get() {return this.children.at(-1) || null;}});
Object.defineProperty(FakeElement.prototype, 'innerHTML', {
  get() {return this._html ?? this.textContent;},
  set(value) {this.replaceChildren(); this._html = value;},
});
const append = FakeElement.prototype.append;
FakeElement.prototype.append = function(...nodes) {
  append.apply(this, nodes.map(node => typeof node === 'string' ? document.createTextNode(node) : node));
};
document.createDocumentFragment = () => document.createElement('fragment');
document.createTextNode = text => {
  const node = document.createElement('text'); node.textContent = String(text); return node;
};
globalThis.DOMParser = class {parseFromString(text) {
  return {body: {childNodes: [document.createTextNode(text)]}};
}};
FakeElement.prototype.dispatchEvent = function(event) {
  for (let cur = this; cur; cur = event.bubbles ? cur.parentNode : null) {
    for (const entry of [...(cur._listeners.get(event.type) || [])]) entry.fn(event);
  }
  return true;
};
const kids = (el, cls) => el.children.filter(child => child.classList.contains(cls));
const text = el => el.textContent + el.children.map(text).join('');
"""


def _shared(name: str) -> str:
    return json.dumps((_SHARED / name).as_uri())


def _run(body: str) -> None:
    proc = run_node_source(FAKE_DOM + _DOM_EXTRAS + body)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}\n{proc.stdout}"


def _builders(scenario: str) -> None:
    _run(
        f"const conv = await import({_shared('conversation.js')});\n"
        "const row = (name, args) => {\n"
        "  const el = conv.buildConvRow({func_name: name, call_id: 'c-' + name});\n"
        "  html.appendChild(el);\n"
        "  conv.attachConvFullCall(el, args);\n"
        "  return el;\n"
        "};\n"
        "const open = el => {\n"
        "  el.querySelector('.conv-row-full-toggle').click();\n"
        "  return el.querySelector('.conv-row-full');\n"
        "};\n"
        "const args = panel => panel.querySelectorAll('.conv-full-arg').map(arg => [\n"
        "  (arg.querySelector('.conv-full-arg-name') || {textContent: null}).textContent,\n"
        "  arg.querySelector('.conv-full-arg-value').textContent,\n"
        "]);\n" + scenario
    )


def test_full_call_shows_every_argument_as_written() -> None:
    _builders(
        r"""
const query = 'is:open label:bug ' + 'repo:example/turnstone '.repeat(20);
const call = JSON.stringify({query, body: 'line one\nline two', page: {size: 50}});
const el = row('mcp__docs__search', call);
const toggle = el.querySelector('.conv-row-full-toggle');
const panel = el.querySelector('.conv-row-full');
assert(toggle.parentNode.classList.contains('conv-row-call'), 'the control sits on the call line');
assert(toggle.getAttribute('aria-expanded') === 'false' && panel.hidden, 'starts closed');
assert(panel.children.length === 0, 'a closed call holds no DOM');
assert(toggle.getAttribute('aria-controls') === panel.id, 'the control names its panel');
assert(toggle.getAttribute('aria-label') === 'Full call: mcp__docs__search', 'named for the tool');
open(el);
const caption = panel.querySelector('.conv-full-args-caption');
assert(caption.textContent === 'arguments · as written', 'the panel says what it shows');
assert(panel.getAttribute('role') === 'group' && panel.getAttribute('aria-labelledby') === caption.id,
  'a group named by its caption');
assert(!panel.hidden && toggle.getAttribute('aria-expanded') === 'true', 'opens');
assert(el.dataset.fullCall === 'open', 'the row says it is open');
const shown = args(panel);
assert(shown.length === 3, 'one entry per argument: ' + JSON.stringify(shown));
assert(shown[0][0] === 'query' && shown[0][1] === query, 'a long value is whole');
assert(shown[1][1] === 'line one\nline two', 'a string keeps its own line breaks');
assert(shown[2][1] === JSON.stringify({size: 50}, null, 2), 'other values print as JSON');
toggle.click();
assert(panel.hidden && panel.children.length === 0, 'closing empties the panel');
assert(el.dataset.fullCall === undefined, 'and the row says so');
conv.attachConvFullCall(el, '{}');
assert(el.querySelectorAll('.conv-row-full-toggle').length === 1, 'attaching twice adds nothing');
"""
    )


def test_full_call_keeps_numbers_and_shows_unparsed_text_as_written() -> None:
    _builders(
        r"""
const ids = row('send', '{"channel": 123456789012345678901, "ratio": 1.0, "nested": [1.50]}');
const shown = args(open(ids));
assert(shown[0][1] === '123456789012345678901', 'a large integer keeps its digits: ' + shown[0][1]);
assert(shown[1][1] === '1.0', 'a float keeps its form: ' + shown[1][1]);
assert(shown[2][1].includes('1.50'), 'nested numbers too: ' + shown[2][1]);

const broken = row('bash', '{"command": "ls -la');
const raw = args(open(broken));
assert(raw.length === 1 && raw[0][0] === null, 'unparsed text has no names');
assert(raw[0][1] === '{"command": "ls -la', 'and shows as written');

const list = args(open(row('odd', '[1, 2]')));
assert(list.length === 1 && list[0][1] === JSON.stringify([1, 2], null, 2), 'other JSON prints');

for (const empty of ['', '{}', ' { } ']) {
  assert(!row('none', empty).querySelector('.conv-row-full-toggle'),
    'nothing to open for ' + JSON.stringify(empty));
}
assert(conv.buildConvFullCallArguments('{}').querySelector('.conv-full-args-none').textContent
  === '(no arguments)', 'the builder still says so');

const plain = conv.buildConvRow({func_name: 'legacy', call_id: 'x'});
conv.attachConvFullCall(plain, undefined);
assert(!plain.querySelector('.conv-row-full-toggle'), 'an item without arguments keeps the summary');
"""
    )


def test_full_call_redacts_named_and_escaped_secrets() -> None:
    _builders(
        r"""
const key = 'sk-' + 'a'.repeat(30);
const escaped = String.fromCharCode(92) + 'u0073k-' + 'b'.repeat(30);
const esc = String.fromCharCode(92) + 'u001b';
const call = '{"password": "hunter2-not-a-key", "note": "token ' + key + '", "hidden": "' + escaped + '", "color": "' + esc + '[31mred' + esc + '[0m"}';
const shown = args(open(row('login', call)));
const all = JSON.stringify(shown);
assert(!all.includes('hunter2-not-a-key'), 'a value under a secret name is redacted: ' + all);
assert(!all.includes(key), 'a key-shaped value is redacted: ' + all);
assert(!all.includes('b'.repeat(30)), 'a secret behind a JSON escape is redacted: ' + all);
assert(shown[3][1] === 'red', 'escape sequences are stripped: ' + JSON.stringify(shown[3][1]));
"""
    )


def test_open_full_calls_survive_a_rebuild() -> None:
    _builders(
        r"""
const block = document.createElement('div');
html.appendChild(block);
const first = conv.buildConvRow({func_name: 'bash', call_id: 'a'});
conv.attachConvFullCall(first, '{"command": "true"}');
const second = conv.buildConvRow({func_name: 'bash', call_id: 'b'});
conv.attachConvFullCall(second, '{"command": "false"}');
block.append(first, second);
first.querySelector('.conv-row-full-toggle').click();
second.querySelector('.conv-row-full-toggle').focus();
const kept = conv.captureConvFullCalls(block);
block.replaceChildren();
const again = conv.buildConvRow({func_name: 'bash', call_id: 'a'});
conv.attachConvFullCall(again, '{"command": "true"}');
const other = conv.buildConvRow({func_name: 'bash', call_id: 'b'});
conv.attachConvFullCall(other, '{"command": "false"}');
block.append(again, other);
conv.restoreConvFullCalls(block, kept);
assert(again.dataset.fullCall === 'open', 'the open call reopens');
assert(other.dataset.fullCall === undefined, 'a closed one stays closed');
assert(document.activeElement === other.querySelector('.conv-row-full-toggle'), 'focus returns');
"""
    )


def test_show_all_is_offered_only_past_the_box() -> None:
    _builders(
        r"""
const box = () => { const el = document.createElement('div'); html.appendChild(el); return el; };
assert(conv.buildConvResultExpand(box(), {text: 'a\nb', boxLines: 10}) === null, 'a short result fits');
const maybe = conv.buildConvResultExpand(box(), {text: 'x'.repeat(4000), boxLines: 10});
assert(maybe && maybe.hidden, 'a result that may fit waits, hidden, for a measurement');

const long = Array.from({length: 3000}, (_, i) => 'line ' + i).join('\n');
const folded = box();
const control = conv.buildConvResultExpand(folded, {text: long, boxLines: 10, fold: true});
assert(control.textContent + text(control) !== '', 'a long result gets the control');
assert(text(control).includes('show all · 3,000 lines'), 'it counts the lines: ' + text(control));
assert(folded.classList.contains('collapsed'), 'a folding box starts folded');
assert(control.getAttribute('aria-controls') === folded.id, 'the control names its box');
assert(control.dataset.expanded === 'false' && !control.hasAttribute('aria-expanded'),
  'not shown in full yet, said by the label alone');
folded.click();
assert(folded.dataset.expanded === 'true' && !folded.classList.contains('collapsed'),
  'a click on the fold shows all');
assert(text(control).includes('collapse'), 'then the control collapses');
control.click();
assert(folded.dataset.expanded === undefined && folded.classList.contains('collapsed'), 'and folds again');
folded.click();
control.click();
folded.click();
assert(folded.dataset.expanded === 'true', 'the fold opens again on a click');

const wide = box();
const byChars = conv.buildConvResultExpand(wide, {text: 'y'.repeat(4001), boxLines: 14});
assert(text(byChars).includes('show all · 4,001 chars'), 'long lines count by characters');
byChars.click();
assert(wide.dataset.expanded === 'true' && !wide.classList.contains('collapsed'),
  'a box that does not fold still lifts its cap');
"""
    )


def test_coordinator_result_past_the_display_cut_shows_every_character() -> None:
    _builders(
        r"""
const full = '{' + '"k": "v",'.repeat(9000) + '"secret": "sk-' + 'c'.repeat(30) + '"}';
const block = conv.buildConvResult(full, {isError: false});
const body = block.children.at(-1);
assert(body.textContent.includes('truncated for display'), 'the box shows the cut');
const control = conv.convResultExpandControl(block);
assert(control, 'a cut result offers show all');
control.click();
assert(body.textContent.length >= full.length - 40, 'show all renders every character');
assert(!body.textContent.includes('truncated for display'), 'without the cut notice');
assert(!body.textContent.includes('c'.repeat(30)), 'still redacted');
assert(block.dataset.expanded === 'true', 'and lifts the height cap');
control.click();
assert(body.textContent.includes('truncated for display'), 'collapse restores the cut');
assert(conv.convResultExpandControl(conv.buildConvResult('ok', {})) === null, 'short results have none');
"""
    )


def test_open_details_hold_off_an_automatic_fold() -> None:
    _builders(
        r"""
const batch = conv.buildConvBatchShell({});
html.appendChild(batch);
const el = conv.buildConvRow({func_name: 'bash', call_id: 'z'});
conv.attachConvFullCall(el, '{"command": "true"}');
batch.appendChild(el);
el.querySelector('.conv-row-full-toggle').click();
assert(conv.setConvBatchExpanded(batch, false) === false, 'an automatic fold waits');
assert(batch.dataset.compactFolded === undefined, 'the call stays visible');
el.querySelector('.conv-row-full-toggle').click();
const result = document.createElement('div');
result.dataset.expanded = 'true';
batch.appendChild(result);
assert(conv.setConvBatchExpanded(batch, false) === false, 'so does a result shown in full');
assert(conv.setConvBatchExpanded(batch, false, {manual: true}) === true, 'a manual fold still folds');
"""
    )


def _pane(scenario: str) -> None:
    _run(
        f"const {{InteractivePane}} = await import({_shared('interactive.js')});\n"
        + r"""
InteractivePane.prototype._createDOM = function() {
  this.el = document.createElement('div');
  this.messagesEl = document.createElement('div');
  this.el.appendChild(this.messagesEl);
  html.appendChild(this.el);
  this.inputEl = document.createElement('textarea');
  this.sendBtn = document.createElement('button');
};
const pane = new InteractivePane('ws-full', {base: ''});
pane.isNearBottom = () => false;
pane.scrollToBottom = pane.removeEmptyState = pane.removeThinkingIndicator = pane.setBusy = () => {};
const root = pane.messagesEl;
const receive = event => pane.handleEvent(event);
const rowFor = id => root.querySelector('.conv-row[data-call-id="' + id + '"]');
"""
        + scenario
    )


def test_live_rows_open_the_full_call_and_keep_it_through_the_upgrade() -> None:
    _pane(
        r"""
const raw = JSON.stringify({url: 'https://example.com/' + 'p/'.repeat(80), question: 'what?'});
const item = {call_id: 'w1', func_name: 'web_fetch', header: 'web_fetch: https://example.com/p',
  preview: '', needs_approval: false, arguments: raw};
receive({type: 'tool_pending', items: [item]});
rowFor('w1').querySelector('.conv-row-full-toggle').click();
assert(rowFor('w1').dataset.fullCall === 'open', 'the pending call opens');
receive({type: 'tool_info', items: [{...item, auto_approved: true}]});
const upgraded = rowFor('w1');
assert(upgraded.dataset.fullCall === 'open', 'the upgraded card keeps it open');
const value = upgraded.querySelector('.conv-full-arg-value').textContent;
assert(value === 'https://example.com/' + 'p/'.repeat(80), 'with the whole URL: ' + value);
"""
    )


def test_live_long_result_gets_show_all_and_the_warning_lands_after_it() -> None:
    _pane(
        r"""
const item = {call_id: 'r1', func_name: 'read_file', header: 'read_file: /srv/a.py',
  preview: '', needs_approval: false, arguments: '{"path": "/srv/a.py"}'};
receive({type: 'tool_pending', items: [item]});
receive({type: 'tool_info', items: [{...item, auto_approved: true}]});
const lines = Array.from({length: 40}, (_, i) => 'line ' + i).join('\n');
receive({type: 'tool_result', call_id: 'r1', name: 'read_file', output: lines});
const row = rowFor('r1');
const wrap = row.nextElementSibling;
assert(wrap && wrap.classList.contains('conv-result-wrap'), 'a long result travels with its control');
const [box, control] = wrap.children;
assert(box.classList.contains('tool-output') && box.classList.contains('collapsed'), 'folded');
assert(control.classList.contains('conv-result-expand'), 'with show all under it');
row.querySelector('.conv-row-full-toggle').click();
assert(box.classList.contains('collapsed'), 'opening the call leaves the result folded');
control.click();
assert(box.dataset.expanded === 'true', 'show all opens the result');
receive({type: 'output_warning', call_id: 'r1', risk_level: 'medium', flags: ['f']});
assert(wrap.nextElementSibling && wrap.nextElementSibling.classList.contains('conv-warning'),
  'the guard chip lands after the result and its control');
"""
    )


def test_replayed_rows_open_the_stored_call() -> None:
    _pane(
        r"""
const stored = JSON.stringify({command: 'echo ' + 'z'.repeat(300)});
pane.replayHistory([
  {role: 'user', content: 'go'},
  {role: 'assistant', content: '', tool_calls: [{id: 'h1', name: 'bash', arguments: stored}]},
  {role: 'tool', tool_call_id: 'h1', content: Array.from({length: 30}, (_, i) => i).join('\n')},
]);
const row = rowFor('h1');
row.querySelector('.conv-row-full-toggle').click();
const value = row.querySelector('.conv-full-arg-value').textContent;
assert(value === 'echo ' + 'z'.repeat(300), 'the replayed call is whole: ' + value.length);
assert(row.nextElementSibling.classList.contains('conv-result-wrap'), 'its long result has show all');
"""
    )


def test_coordinator_places_show_all_under_its_result_and_replaces_it() -> None:
    source = _COORDINATOR.read_text(encoding="utf-8")
    append_to_row = _extract_braced(
        source, "  function _appendResultToRow(row, output, isError, opts) {"
    )
    assert append_to_row, "coordinator _appendResultToRow not found"
    _run(
        f"const conv = await import({_shared('conversation.js')});\n"
        + r"""
const {buildConvResult, convResultExpandControl, setConvBatchExpanded,
  clearConvVerdictPending, setToolOutputReviewState, buildPreviewChip} = conv;
const _tryMcpErrorBlock = () => null;
const buildToolImages = () => null;
const wsId = 'ws-coord';
const batch = conv.buildConvBatchShell({});
html.appendChild(batch);
const row = conv.buildConvRow({func_name: 'list_workstreams', call_id: 'q1'});
batch.appendChild(row);
"""
        + "const appendResult = "
        + append_to_row.strip()
        + ";\n"
        + r"""
const long = Array.from({length: 200}, (_, i) => 'ws ' + i).join('\n');
const first = appendResult(row, long, false, {});
const control = first.nextElementSibling;
assert(control && control.classList.contains('conv-result-expand'), 'show all sits under the result');
assert(row.children.at(-1) === control, 'at the end of the row');
appendResult(row, long + '\nmore', false, {accepted: true});
assert(row.querySelectorAll('.conv-result-expand').length === 1, 'the old control leaves with its result');
assert(row.querySelectorAll('.conv-row-result').length === 1, 'one result stays');
appendResult(row, 'short', false, {accepted: true});
assert(row.querySelectorAll('.conv-result-expand').length === 0, 'a short result has none');

const pending = conv.buildConvRow({func_name: 'write_file', call_id: 'q2'});
batch.appendChild(pending);
const content = Array.from({length: 100}, (_, i) => 'line ' + i).join('\n');
conv.attachConvFullCall(pending, JSON.stringify({path: 'a.py', content}));
pending.querySelector('.conv-row-full-toggle').click();
const valueControl = pending.querySelector('.conv-row-full').querySelector('.conv-result-expand');
assert(valueControl, 'the open call boxes its long value');
appendResult(pending, long, false, {});
assert(valueControl.isConnected, "a result arriving keeps the open call's value control");
appendResult(pending, long + '\nmore', false, {accepted: true});
assert(valueControl.isConnected, 'so does the accepted result that replaces it');
"""
    )


@pytest.mark.parametrize(
    "needle",
    [
        "attachConvFullCall(row, item.arguments);",
        "attachConvFullCall(entry.row, it.arguments);",
        "arguments: argsRaw,",
    ],
)
def test_coordinator_rows_carry_the_full_call(needle: str) -> None:
    """First paint, the upgrade that first brings the arguments, and /history
    replay all hand the call's text to the row."""
    assert needle in _COORDINATOR.read_text(encoding="utf-8")


def _handler(source: str, anchor: str) -> str:
    start = source.index(anchor) + len(anchor)
    body = _extract_braced(source[start:], "{")
    assert body and body.startswith("{"), f"handler after {anchor!r} not found"
    return body


def test_enter_on_a_focused_control_never_approves_in_the_interactive_pane() -> None:
    """While a call awaits approval, Enter on a focused button runs that button: on
    Deny it denies, on "full call" it opens the call.  It used to approve."""
    handler = _handler(
        (_SHARED / "interactive.js").read_text(encoding="utf-8"),
        'this.el.addEventListener("keydown", (e) => ',
    )
    _pane(
        f"const {{closeOpenConvDetail}} = await import({_shared('conversation.js')});\n"
        + r"""
FakeElement.prototype.value = '';
const decisions = [];
pane.resolveApproval = approved => decisions.push(approved);
const item = {call_id: 'k1', func_name: 'bash', header: 'bash: rm -rf /tmp/x', preview: '',
  needs_approval: true, arguments: '{"command": "rm -rf /tmp/x"}'};
receive({type: 'approve_request', cycle_id: 'cy1', items: [item]});
const closest = FakeElement.prototype.closest;
FakeElement.prototype.closest = function(selector) {
  if (selector !== 'button, a[href]') return closest.call(this, selector);
  for (let cur = this; cur; cur = cur.parentNode) if (cur.tagName === 'BUTTON') return cur;
  return null;
};
const keydown = (function() { return (e) => """
        + handler
        + r"""; }).call(pane);
const press = (target, key) => { target.focus(); keydown({key, target, shiftKey: false, preventDefault() {}}); };
press(root.querySelector('.conv-btn--deny'), 'Enter');
press(root.querySelector('.conv-row-full-toggle'), 'Enter');
assert(decisions.length === 0, 'Enter on a button must not approve: ' + JSON.stringify(decisions));
press(root.querySelector('.conv-row-full-toggle'), 'y');
press(rowFor('k1'), 'Enter');
assert(JSON.stringify(decisions) === '[true,true]', 'letters, and Enter elsewhere, still approve');
const toggle = root.querySelector('.conv-row-full-toggle');
toggle.click();
press(toggle, 'Escape');
assert(decisions.length === 2 && rowFor('k1').dataset.fullCall === undefined,
  'Escape on an open full call closes it and decides nothing');
press(toggle, 'Escape');
assert(JSON.stringify(decisions) === '[true,true,false]', 'a second Escape denies as before');
"""
    )


def test_enter_on_a_focused_control_never_approves_in_the_coordinator() -> None:
    handler = _handler(
        _COORDINATOR.read_text(encoding="utf-8"),
        'root.addEventListener("keydown", function (e) ',
    )
    _run(
        f"const {{closeOpenConvDetail}} = await import({_shared('conversation.js')});\n"
        + r"""
const resolved = [];
const batch = document.createElement('div');
const sbApprovalEl = document.createElement('button');
const _currentPendingBatch = () => batch;
const _resolveBatchAction = (b, approved, always) => resolved.push([approved, always]);
const closest = FakeElement.prototype.closest;
FakeElement.prototype.closest = function(selector) {
  if (selector !== 'button, a[href]') return closest.call(this, selector);
  for (let cur = this; cur; cur = cur.parentNode) if (cur.tagName === 'BUTTON') return cur;
  return null;
};
const keydown = function (e) """
        + handler
        + r""";
const press = (target, key, extra = {}) => {
  document.activeElement = target;
  keydown({key, target, preventDefault() {}, ...extra});
};
const deny = document.createElement('button');
const toggle = document.createElement('button');
const row = document.createElement('div');
batch.append(deny, toggle, row);
press(deny, 'Enter');
press(toggle, 'Enter');
assert(resolved.length === 0, 'Enter on a button must not approve: ' + JSON.stringify(resolved));
press(toggle, 'd');
press(row, 'Enter');
assert(JSON.stringify(resolved) === '[[false,false],[true,false]]', 'D and Enter elsewhere still resolve');
toggle.className = 'conv-row-full-toggle';
toggle.setAttribute('aria-expanded', 'true');
let closed = 0;
toggle.addEventListener('click', () => { closed += 1; toggle.setAttribute('aria-expanded', 'false'); });
press(toggle, 'Escape');
assert(closed === 1 && resolved.length === 2, 'Escape on an open full call closes it and decides nothing');
press(toggle, 'Escape');
assert(JSON.stringify(resolved.at(-1)) === '[false,false]', 'a second Escape denies as before');
"""
    )


def test_a_long_argument_value_gets_a_box_of_its_own() -> None:
    _builders(
        r"""
const content = Array.from({length: 400}, (_, i) => 'line ' + i).join('\n');
const el = row('write_file', JSON.stringify({path: '/srv/a.py', content}));
const [pathArg, contentArg] = open(el).querySelectorAll('.conv-full-arg');
assert(!pathArg.querySelector('.conv-result-expand'), 'a short value has no control');
const control = contentArg.querySelector('.conv-result-expand');
assert(control && !control.hidden && control.textContent === 'show all · 400 lines',
  'a long value says how long: ' + (control && control.textContent));
const value = contentArg.querySelector('.conv-full-arg-value');
assert(value.textContent === content, 'the box holds every character');
control.click();
assert(value.dataset.expanded === 'true' && control.textContent === 'collapse', 'show all opens it');

const huge = 'z'.repeat(70 * 1024);
const big = open(row('write_huge', JSON.stringify({content: huge})));
const bigValue = big.querySelector('.conv-full-arg-value');
assert(bigValue.textContent.includes('truncated for display') && bigValue.textContent.length < huge.length,
  'a value past the display cut shows its head');
big.querySelector('.conv-result-expand').click();
assert(bigValue.textContent === huge, 'show all renders every character');
"""
    )


def test_show_all_follows_a_measurement_of_the_box() -> None:
    _builders(
        r"""
const root = document.createElement('div');
html.appendChild(root);
const place = () => { const el = document.createElement('div'); root.appendChild(el); return el; };
const prose = 'word '.repeat(600);
const clipped = place();
const tight = conv.buildConvResultExpand(clipped, {text: prose, boxLines: 13});
const fits = place();
const loose = conv.buildConvResultExpand(fits, {text: prose, boxLines: 13});
assert(tight.hidden && loose.hidden, 'one long line is only a guess until measured');
clipped.clientHeight = 228; clipped.scrollHeight = 540;
fits.clientHeight = 228; fits.scrollHeight = 200;
const folded = place();
const json = conv.buildConvResultExpand(folded, {text: '{\n  "a": 1\n}', boxLines: 0, fold: true});
assert(folded.classList.contains('collapsed') && !json.hidden, 'raw JSON starts folded');
folded.clientHeight = 106; folded.scrollHeight = 106;
const unlaid = place();
const guess = conv.buildConvResultExpand(unlaid, {text: 'y\n'.repeat(40), boxLines: 13});
root.append(tight, loose, json, guess);
conv.refitConvResults(root);
assert(!tight.hidden, 'a box that clips gets its control');
assert(loose.hidden, 'a box that fits does not');
assert(json.hidden && !folded.classList.contains('collapsed'), 'a fold that fits opens and drops its control');
assert(!guess.hidden, 'a box with no layout keeps the line-count guess');
"""
    )


def test_the_panel_follows_the_summary_and_precedes_a_verdict() -> None:
    _builders(
        r"""
const el = conv.buildConvRow({func_name: 'edit_file', call_id: 'p1'});
for (const cls of ['conv-row-cmd', 'conv-row-diff', 'conv-verdict']) {
  const part = document.createElement('div');
  part.className = cls;
  el.appendChild(part);
}
conv.attachConvFullCall(el, '{"path": "a.py"}');
const order = el.children.map(child => child.className.split(' ')[0]);
assert(JSON.stringify(order) ===
  JSON.stringify(['conv-row-call', 'conv-row-cmd', 'conv-row-diff', 'conv-row-full', 'conv-verdict']),
  'after the summary, before the verdict: ' + JSON.stringify(order));
assert(conv.convFullCallPanel(el) === el.children[3], 'and findable for whatever comes after it');
"""
    )


def test_opening_details_by_hand_stops_the_pane_following() -> None:
    _run(
        f"const conv = await import({_shared('conversation.js')});\n"
        f"const {{mountConversationScroll}} = await import({_shared('conversation_scroll.js')});\n"
        + r"""
const scroller = document.createElement('div');
html.appendChild(scroller);
scroller.clientHeight = 400; scroller.clientWidth = 800; scroller.scrollHeight = 400;
const follow = mountConversationScroll(scroller);
const opened = [];
scroller.addEventListener('conv-detail-open', () => opened.push(1));
const batch = conv.buildConvBatchShell({});
scroller.appendChild(batch);
const el = conv.buildConvRow({func_name: 'bash', call_id: 'f1'});
conv.attachConvFullCall(el, '{"command": "ls"}');
batch.appendChild(el);
conv.restoreConvFullCalls(batch, {open: new Set(['f1']), focused: null});
assert(opened.length === 0 && follow.isFollowing(), 'a restore is not a reader opening it');
el.querySelector('.conv-row-full-toggle').click();
el.querySelector('.conv-row-full-toggle').click();
assert(opened.length === 1, 'opening by hand says so, closing does not');
assert(!follow.isFollowing(), 'and the pane stops following new output');

follow.jumpToLatest();
const box = document.createElement('div');
batch.appendChild(box);
const control = conv.buildConvResultExpand(box, {text: 'r\n'.repeat(30), boxLines: 10, fold: true});
batch.appendChild(control);
box.click();
assert(opened.length === 2 && !follow.isFollowing(), 'unfolding a result by hand says so too');
control.click();
assert(opened.length === 2, 'collapsing does not');

box.clientHeight = 150; box.scrollHeight = 900;
const prose = document.createElement('div');
batch.appendChild(prose);
const later = conv.buildConvResultExpand(prose, {text: 'p '.repeat(1500), boxLines: 13});
batch.appendChild(later);
prose.clientHeight = 228; prose.scrollHeight = 600;
assert(later.hidden, 'measured only once the pane asks');
scroller.clientWidth = 420;
triggerResize(scroller);
assert(!later.hidden, 'a new pane width measures every result again');

const hiddenProse = document.createElement('div');
batch.appendChild(hiddenProse);
const unseen = conv.buildConvResultExpand(hiddenProse, {text: 'h '.repeat(1500), boxLines: 13});
batch.appendChild(unseen);
scroller.clientHeight = 0;
triggerResize(scroller);
hiddenProse.clientHeight = 228; hiddenProse.scrollHeight = 600;
scroller.clientHeight = 400;
triggerResize(scroller);
assert(!unseen.hidden, 'a pane shown again at the same width measures its results');
"""
    )


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        (_COORDINATOR, 'callLine.querySelector(".conv-row-full-toggle")'),
        (_COORDINATOR, 'convFullCallPanel(row) || row.querySelector(".conv-row-call")'),
        (_COORDINATOR, 'if (e.key === "Escape" && closeOpenConvDetail(ae)) {'),
        (_SHARED / "interactive.js", 'if (e.key === "Escape" && closeOpenConvDetail(e.target)) {'),
    ],
)
def test_panes_route_pills_verdicts_and_escape_around_the_full_call(
    path: Path, needle: str
) -> None:
    """Error pills sit before "full call" (it keeps the line's right edge), verdicts after
    the panel, and Escape closes an open detail before it can deny."""
    assert needle in path.read_text(encoding="utf-8")


def test_each_escape_closes_one_level_of_an_open_call() -> None:
    _builders(
        r"""
const content = Array.from({length: 80}, (_, i) => 'line ' + i).join('\n');
const el = row('write_file', JSON.stringify({path: 'a.py', content}));
const toggle = el.querySelector('.conv-row-full-toggle');
toggle.click();
const valueControl = el.querySelector('.conv-row-full').querySelector('.conv-result-expand');
valueControl.click();
valueControl.focus();
assert(conv.closeOpenConvDetail(valueControl), 'the first Escape collapses the value');
assert(el.dataset.fullCall === 'open', 'the call stays open');
assert(conv.closeOpenConvDetail(valueControl), 'the second closes the call');
assert(el.dataset.fullCall === undefined, 'which is closed');
assert(document.activeElement === toggle, 'with focus back on its toggle');
assert(!conv.closeOpenConvDetail(toggle), 'the third has nothing to close, so it may deny');
"""
    )


def test_back_to_default_presentation_measures_the_results_compact_hid() -> None:
    _run(
        f"const conv = await import({_shared('conversation.js')});\n"
        f"const tp = await import({_shared('transcript_presentation.js')});\n"
        + r"""
const scroller = document.createElement('div');
html.appendChild(scroller);
tp.registerTranscriptScroller(scroller, {});
tp.setTranscriptPresentation('compact', {persist: false});
const box = document.createElement('div');
scroller.appendChild(box);
const control = conv.buildConvResultExpand(box, {text: 'c '.repeat(1500), boxLines: 13});
scroller.appendChild(control);
assert(control.hidden, 'folded away by compact presentation, there was nothing to measure');
box.clientHeight = 228; box.scrollHeight = 600;
tp.setTranscriptPresentation('default', {persist: false});
assert(!control.hidden, 'back in default presentation the result is measured');
"""
    )
