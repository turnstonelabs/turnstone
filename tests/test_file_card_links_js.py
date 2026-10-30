"""File result links through the actual live and replayed conversation renderers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._js_harness_helpers import FAKE_DOM, node_skip, run_node_source

_SHARED = Path(__file__).resolve().parents[1] / "turnstone/shared_static"
pytestmark = node_skip


def _run(scenario: str) -> None:
    source = (
        FAKE_DOM
        + r"""
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
const permissions = new Map([
  ['turnstone_scopes', 'read,write'], ['turnstone_permissions', 'files.read'],
]);
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
Object.defineProperty(FakeElement.prototype, 'firstElementChild', {get() {return this.children[0] || null;}});
Object.defineProperty(FakeElement.prototype, 'lastElementChild', {get() {return this.children.at(-1) || null;}});
Object.defineProperty(FakeElement.prototype, 'innerHTML', {
  get() {return this._html ?? this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');},
  set(value) {this.replaceChildren(); this._html = value;},
});
document.createDocumentFragment = () => document.createElement('fragment');
document.createTextNode = text => {
  const node = document.createElement('text'); node.textContent = String(text); return node;
};
globalThis.DOMParser = class {parseFromString(text) {
  return {body: {childNodes: [document.createTextNode(text)]}};
}};
"""
        + f"const {{InteractivePane}} = await import({json.dumps((_SHARED / 'interactive.js').as_uri())});\n"
        + r"""
InteractivePane.prototype._createDOM = function() {
  this.el = document.createElement('div');
  this.messagesEl = document.createElement('div');
  this.el.appendChild(this.messagesEl);
  html.appendChild(this.el);
  this.inputEl = document.createElement('textarea');
  this.sendBtn = document.createElement('button');
};
const pane = new InteractivePane('ws/2', {base: '/node/lab'});
pane.isNearBottom = () => false;
pane.scrollToBottom = pane.removeEmptyState = pane.removeThinkingIndicator = pane.setBusy = () => {};
const opened = [];
const previews = [];
pane._host = {...pane._host, onFiles: path => opened.push(path), onPreview: d => previews.push(d)};
const root = pane.messagesEl;
const receive = event => pane.handleEvent(event);
const chips = () => root.querySelectorAll('.conv-file-chip');
function start(name, path, id = 'file') {
  const item = {call_id: id, func_name: name, header: name, file_path: path, needs_approval: false};
  receive({type: 'tool_pending', items: [item]});
  receive({type: 'tool_info', items: [{...item, auto_approved: true}]});
  return item;
}
"""
        + scenario
    )
    proc = run_node_source(source)
    assert proc.returncode == 0, f"file link harness failed:\n{proc.stderr}\n{proc.stdout}"


@pytest.mark.parametrize("name", ["write_file", "edit_file"])
def test_successful_live_result_links_once_and_accepted_failure_removes_it(name: str) -> None:
    _run(
        f"const name = {json.dumps(name)};\n"
        + r"""
const path = '/workspace/' + 'nested/'.repeat(15) + '<script>.js';
start(name, path);
assert(chips().length === 0, 'pending tool must not advertise a completed file');
receive({type: 'tool_result', call_id: 'file', name, output: 'File updated', is_error: false});
assert(chips().length === 1, 'successful live result needs one file link');
assert(opened.length === 0, 'file results must not auto-open a pane');
const label = chips()[0].querySelector('.conv-content-title');
assert(label.textContent === '<script>.js' && label.children.length === 0, 'filename must remain visible and inert');
assert(chips()[0].title.endsWith(path), 'tooltip must preserve the full path');
assert(!chips()[0].hidden, 'authorized operator must see the file link');
chips()[0].click();
assert(opened.length === 1 && opened[0] === path, 'click must open the exact full path');
const accepted = {type: 'tool_result', call_id: 'file', name, output: 'File updated',
  accepted: true, effect_status: 'committed', _event_id: 101};
receive(accepted);
assert(chips().length === 1, 'accepted result must replace the provisional link');
const acceptedChip = chips()[0];
receive(accepted);
assert(chips().length === 1 && chips()[0] === acceptedChip, 'duplicate accepted event must not rebuild the link');
receive({...accepted, output: 'File update failed', is_error: true, _event_id: 102});
assert(chips().length === 0, 'accepted failure must remove the previous success link');
"""
    )


@pytest.mark.parametrize(
    "result",
    [
        {"is_error": True, "output": "File update failed"},
        {"output": "Denied by user"},
        {"output": "Blocked by tool policy"},
        {"effect_status": "none"},
        {"effect_status": "unknown"},
        {"effect_status": "partial"},
        {"effect_status": "rolled_back"},
    ],
)
def test_unsuccessful_results_do_not_get_file_links(result: dict[str, object]) -> None:
    _run(
        r"""
start('write_file', '/workspace/report.html');
"""
        + "receive({type: 'tool_result', call_id: 'file', name: 'write_file', output: 'result', "
        + "accepted: true, _event_id: 101, ..."
        + json.dumps(result)
        + "});\n"
        + "assert(chips().length === 0, 'unsuccessful result advertised a file');\n"
    )


def test_history_and_nested_replay_keep_full_file_targets() -> None:
    _run(r"""
const path = '/workspace/' + 'directory/'.repeat(12) + 'report.html';
const nestedPath = '/tmp/<script>.txt';
const calls = [
  {id: 'write', name: 'write_file', arguments: JSON.stringify({path, content: '<h1>Report</h1>'})},
  {id: 'agent', name: 'task_agent', arguments: '{}', agent_steps: [
    {id: 'child', name: 'edit_file', arguments: '{"path": "truncated', file_path: nestedPath,
      output: 'File updated', is_error: false},
    {id: 'failed-child', name: 'write_file', arguments: '{"path":"/tmp/failed"}',
      output: 'Failed', is_error: true},
    {id: 'pending-child', name: 'write_file', arguments: '{"path":"/tmp/pending"}',
      output: '', is_error: false},
  ]},
];
pane.replayHistory([
  {role: 'assistant', content: '', tool_calls: calls},
  {role: 'tool', tool_call_id: 'write', content: 'File updated', is_error: false},
  {role: 'tool', tool_call_id: 'agent', content: 'Done', is_error: false},
]);
assert(chips().length === 2, 'history must link successful parent and nested file results only');
assert(opened.length === 0, 'history must never auto-open files');
for (const chip of chips()) chip.click();
assert(opened.includes(path), 'replayed long path must not inherit the header truncation');
assert(opened.includes(nestedPath), 'nested path must survive clipped arguments JSON');
const nested = root.querySelector('.conv-agent');
assert(nested && nested.querySelector('.conv-file-chip'), 'nested result link must remain inside its agent card');
""")


def test_unmatched_result_does_not_publish_a_file_link_for_another_call() -> None:
    _run(r"""
start('write_file', '/workspace/pending.txt', 'pending');
receive({type: 'tool_result', call_id: 'other', name: 'write_file', output: 'File updated'});
assert(chips().length === 0, 'unmatched result must not advertise a pending file');
""")


@pytest.mark.parametrize("scopes, permissions", [("read", "files.read"), ("read,write", "")])
def test_file_links_require_both_scope_and_permission(scopes: str, permissions: str) -> None:
    _run(
        f"permissions.set('turnstone_scopes', {json.dumps(scopes)});\n"
        f"permissions.set('turnstone_permissions', {json.dumps(permissions)});\n"
        + r"""
start('write_file', '/workspace/report.html');
receive({type: 'tool_result', call_id: 'file', name: 'write_file', output: 'File updated'});
assert(chips().length === 1 && chips()[0].hidden, 'unauthorized file link must be hidden');
"""
    )


def test_file_and_preview_builders_share_accessible_controls() -> None:
    _run(
        f"const {{buildFileChip, buildPreviewChip}} = await import({json.dumps((_SHARED / 'conversation.js').as_uri())});\n"
        + r"""
const path = '/workspace/<img src=x>.html';
const descriptor = {title: 'A <report>', source: 'https://user:secret@example.com/report', kind: 'web'};
const file = buildFileChip(path, target => opened.push(target));
const preview = buildPreviewChip(descriptor, target => previews.push(target));
for (const button of [file, preview]) {
  assert(button.tagName === 'BUTTON' && button.type === 'button', 'open action must be a native non-submit button');
  assert(button.classList.contains('conv-content-chip'), 'both actions need the shared styling class');
  const glyphs = button.querySelectorAll('.conv-content-glyph');
  assert(glyphs.length === 2 && glyphs.every(g => g.getAttribute('aria-hidden') === 'true'), 'decorative symbols must be hidden from assistive technology');
  assert(glyphs[1].textContent === '↗', 'open indicator must be shared');
  assert(button.querySelector('.conv-content-title').children.length === 0, 'labels must never become markup');
}
assert(file.getAttribute('aria-label') === 'Open file: ' + path, 'file action needs an explicit accessible name');
assert(preview.getAttribute('aria-label') === 'Open preview: A <report>', 'preview action needs an explicit accessible name');
assert(!preview.title.includes('secret'), 'preview tooltip must redact credentials');
file.click(); preview.click();
assert(opened[0] === path && previews[0] === descriptor, 'shared styling must preserve distinct callback targets');
"""
    )
