"""Tool images are expanded, lazy-loaded and routed on every chat surface."""

import json
from pathlib import Path

import pytest

from tests._js_harness_helpers import FAKE_DOM, run_node_source

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "turnstone/shared_static/composer_attachments.js"
STYLE_DOM = """
const create = document.createElement;
document.createElement = (tag) => { const el = create(tag); el.style = {}; return el; };
"""
RETRY_CLOCK = """
let nextTimerId = 0;
const timers = new Map();
globalThis.setTimeout = (fn, delay) => {
  const id = ++nextTimerId;
  timers.set(id, {fn, delay});
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
function tick(delay) {
  if (timers.size !== 1) throw Error('expected exactly one pending retry');
  const [id, timer] = timers.entries().next().value;
  if (timer.delay !== delay) throw Error('wrong retry delay');
  timers.delete(id);
  timer.fn();
}
"""


def test_images_are_expanded_lazy_scoped_and_retryable():
    script = (
        FAKE_DOM
        + STYLE_DOM
        + RETRY_CLOCK
        + f"""
const {{ buildToolImages }} = await import({str(MODULE.as_uri())!r});
const images = [{{kind: 'image', attachment_id: 'id/1', filename: '<img onerror=bad>'}}];
const opts = {{wsId: 'ws/2', base: '/node/lab'}};
const view = buildToolImages(images, opts);
document.documentElement.appendChild(view);
if (!view.open || view.tagName !== 'DETAILS') throw Error('must start expanded');
if (view.children[1].children.length !== 1) throw Error('must render without a click');
const link = view.children[1].children[0];
const img = link.children[0];
if (img.src !== '/node/lab/v1/api/workstreams/ws%2F2/attachments/id%2F1/content') throw Error('wrong scope');
if (link.href !== img.src) throw Error('full image link must use the same route');
if (img.loading !== 'lazy' || img.decoding !== 'async') throw Error('offscreen loading must stay lazy');
if (!img.width || !img.height || img.style.height !== 'auto') throw Error('missing reserved geometry');
if (img.alt !== images[0].filename || link.rel !== 'noopener noreferrer') throw Error('unsafe metadata');
// Setting details.open queues a browser toggle event. It must not load twice.
view.dispatch('toggle');
if (view.children[1].children[0] !== link) throw Error('initial toggle rerendered');
for (const delay of [250, 750, 2000]) {{
  img.dispatch('error');
  if (!link.children[1].hidden) throw Error('transient failure shown too soon');
  tick(delay);
}}
img.dispatch('error');
if (!link.children[1].textContent.includes('reopen')) throw Error('missing retry explanation');
if (timers.size) throw Error('retries must be bounded');
view.open = false;
view.dispatch('toggle');
view.open = true;
view.dispatch('toggle');
if (view.children[1].children.length !== 1 || view.children[1].children[0] !== link) throw Error('retry must retain nodes');
if (img.hidden || !link.children[1].hidden) throw Error('retry must restore the image');
if (buildToolImages([], opts) !== null) throw Error('empty');
if (buildToolImages([{{kind:'image', url:'https://evil.test/image'}}], opts) !== null) throw Error('remote URL');
if (buildToolImages(images, {{wsId:''}}) !== null) throw Error('missing scope');
const multiple = buildToolImages([...images, ...images], opts);
if (!multiple.open || multiple.children[0].textContent !== 'View 2 images') throw Error('multi-image expansion');
if (multiple.children[1].children.length !== 2) throw Error('missing second image');
"""
    )
    result = run_node_source(script)
    assert result.returncode == 0, result.stderr


def test_image_retries_recover_and_stop_after_success_close_or_removal():
    script = (
        FAKE_DOM
        + STYLE_DOM
        + RETRY_CLOCK
        + f"""
const {{ buildToolImages }} = await import({str(MODULE.as_uri())!r});
function fixture() {{
  const view = buildToolImages([{{kind:'image', attachment_id:'frame'}}], {{wsId:'ws'}});
  document.documentElement.appendChild(view);
  const link = view.children[1].children[0];
  const img = link.children[0];
  let writes = 0;
  const url = img.src;
  Object.defineProperty(img, 'src', {{
    get: () => url,
    set: (value) => {{
      if (value !== url) throw Error('retry must keep the exact scoped image URL');
      writes++;
    }},
  }});
  return {{view, link, img, writes: () => writes}};
}}
const recovered = fixture();
recovered.img.dispatch('error');
recovered.img.dispatch('error'); // duplicate errors must not schedule twice
tick(250);
if (recovered.writes() !== 1) throw Error('retry did not reload');
recovered.img.dispatch('load');
if (timers.size || !recovered.link.children[1].hidden) throw Error('recovery failed');
const success = fixture();
success.img.dispatch('error');
success.img.dispatch('load');
if (timers.size) throw Error('success must cancel pending retry');
success.view.open = false;
success.view.dispatch('toggle');
success.view.open = true;
success.view.dispatch('toggle');
if (success.writes() || success.link.children[0] !== success.img) throw Error('success must not refetch');
for (const action of ['close', 'remove']) {{
  const f = fixture();
  f.img.dispatch('error');
  if (action === 'remove') f.view.remove();
  else {{
    f.view.open = false;
    f.view.dispatch('toggle');
  }}
  if (action === 'remove') tick(250);
  if (f.writes() || timers.size) throw Error(action + ' must stop old image retries');
  if (action === 'close') {{
    f.view.open = true;
    f.view.dispatch('toggle');
    if (f.writes() !== 1) throw Error('reopen must restart an interrupted failure');
    f.img.dispatch('error');
    tick(250);
    if (f.writes() !== 2) throw Error('reopen must reset the retry budget');
    f.img.dispatch('load');
  }}
}}
"""
    )
    result = run_node_source(script)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "proxied,explicit_base,expected_base",
    [
        pytest.param(False, "", "", id="standalone-or-coordinator"),
        pytest.param(False, "/node/lab", "/node/lab", id="console-interactive-pane"),
        pytest.param(True, "", "/node/lab", id="proxied-worker-page"),
        pytest.param(True, "/node/lab", "/node/lab", id="explicit-prefix-not-doubled"),
        pytest.param(True, "/node/other", "/node/other", id="explicit-target-wins"),
    ],
)
def test_tool_images_follow_real_proxy_shim(proxied, explicit_base, expected_base):
    from turnstone.console.server import _JS_PROXY_SHIM

    shim = _JS_PROXY_SHIM.replace('"PREFIX_PLACEHOLDER"', json.dumps("/node/lab"))
    script = (
        FAKE_DOM
        + STYLE_DOM
        + f"""
globalThis.window = {{
  fetch: (url) => url,
  EventSource: function(url) {{ this.url = url; }},
}};
document.readyState = 'loading';
document.addEventListener = () => {{}};
if ({json.dumps(proxied)}) {{
  new Function({json.dumps(shim)})();
  if (window.fetch('/v1/api/status') !== '/node/lab/v1/api/status')
    throw Error('fixture must run real fetch shim');
}}
const {{ buildToolImages }} = await import({str(MODULE.as_uri())!r});
const view = buildToolImages([{{kind:'image', attachment_id:'id/1'}}], {{
  wsId: 'ws/2', base: {json.dumps(explicit_base)},
}});
view.open = true;
view.dispatch('toggle');
const link = view.children[1].children[0];
const expected = {json.dumps(expected_base)} + '/v1/api/workstreams/ws%2F2/attachments/id%2F1/content';
if (link.children[0].src !== expected || link.href !== expected)
  throw Error('native image/link bypass fetch shim: ' + link.children[0].src + ' != ' + expected);
"""
    )
    result = run_node_source(script)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("surface", ["interactive", "coordinator"])
def test_live_and_history_surfaces_render_scoped_images(tmp_path, surface):
    """Drive actual pane renderers with browser/host scaffolding and projected history."""
    shared = ROOT / "turnstone/shared_static"
    coordinator = (ROOT / "turnstone/console/static/coordinator/coordinator.js").read_text()
    coordinator = coordinator.replace('from "/shared/', 'from "' + shared.as_uri() + "/")
    needle = "    wsId: wsId," + chr(10) + "    connect: init,"
    assert coordinator.count(needle) == 1
    coordinator = coordinator.replace(
        needle,
        "    _imageTest: {receive: handleEvent, replay: refetchHistory}," + chr(10) + needle,
    )
    module = tmp_path / "coordinator.mjs"
    module.write_text(coordinator)
    script = (
        FAKE_DOM
        + STYLE_DOM
        + r"""
document.body = html;
document.addEventListener = document.removeEventListener = () => {};
document.querySelector = s => html.querySelector(s);
document.getElementById = s => html.querySelector('#' + s);
window.matchMedia = () => ({matches: false, addEventListener() {}});
window.removeEventListener = () => {};
window.setTimeout = globalThis.setTimeout = () => 0;
window.clearTimeout = globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 0;
globalThis.clearInterval = globalThis.cancelAnimationFrame = () => {};
globalThis.BroadcastChannel = class {postMessage() {} addEventListener() {} close() {}};
globalThis.CSS = {escape: value => value};
const matches = FakeElement.prototype._matches;
FakeElement.prototype._matches = function(s) {
  return s.startsWith('#') ? (this.id || this.getAttribute('id')) === s.slice(1) : matches.call(this, s);
};
FakeElement.prototype.after = function(node) {
  const parent = this.parentNode;
  if (!parent || node === this) return;
  if (node.parentNode) node.remove();
  parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
  node.parentNode = parent; node._setConnected(parent.isConnected);
};
FakeElement.prototype.insertBefore = function(node, before) {
  if (!before) return this.appendChild(node);
  if (node === before) return node;
  if (node.parentNode) node.remove();
  this.children.splice(this.children.indexOf(before), 0, node);
  node.parentNode = this; node._setConnected(this.isConnected);
  return node;
};
Object.defineProperty(FakeElement.prototype, 'nextElementSibling', {get() {
  return this.parentNode ? this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null : null;
}});
Object.defineProperty(FakeElement.prototype, 'firstElementChild', {get() {return this.children[0] || null;}});
Object.defineProperty(FakeElement.prototype, 'lastElementChild', {get() {return this.children.at(-1) || null;}});
Object.defineProperty(FakeElement.prototype, 'innerHTML', {
  get() {return this._html ?? this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');},
  set(value) {this.replaceChildren(); this._html = value;},
});
document.createDocumentFragment = () => document.createElement('fragment');
document.createTextNode = text => {const node = document.createElement('text'); node.textContent = String(text); return node;};
// Image insertion uses DOM nodes; prose HTML parsing is outside this fixture's scope.
globalThis.DOMParser = class {parseFromString(text) {
  return {body: {childNodes: [document.createTextNode(text)]}};
}};
const surface = __SURFACE__;
const wsId = 'ws/2';
let pane, root, receive, replay, destroy = () => {};
if (surface === 'interactive') {
  const {InteractivePane} = await import(__INTERACTIVE__);
  InteractivePane.prototype._createDOM = function() {
    this.el = document.createElement('div'); this.messagesEl = document.createElement('div');
    this.el.appendChild(this.messagesEl); html.appendChild(this.el);
    this.inputEl = document.createElement('textarea'); this.sendBtn = document.createElement('button');
  };
  pane = new InteractivePane(wsId, {base: '/node/lab'});
  pane.isNearBottom = () => false;
  pane.scrollToBottom = pane.removeEmptyState = pane.removeThinkingIndicator = pane.setBusy = () => {};
  root = pane.messagesEl;
  receive = event => pane.handleEvent(event);
  replay = async messages => pane.replayHistory(messages);
} else {
  for (const name of ['utils', 'auth', 'composer_attachments', 'composer_queue', 'renderer', 'status_bar']) {
    Object.assign(globalThis, await import(__SHARED__ + '/' + name + '.js'));
  }
  globalThis.Composer = class {
    constructor() {
      this.stopBtn = document.createElement('button'); this.chipsEl = document.createElement('div');
      this.inputEl = document.createElement('textarea');
    }
    setBusy() {} setSendBlocked() {} destroy() {}
  };
  const {createCoordinatorPane} = await import(__COORDINATOR__);
  root = document.createElement('div'); html.appendChild(root);
  pane = createCoordinatorPane(root, wsId, {});
  receive = pane._imageTest.receive;
  replay = async messages => {
    globalThis.fetch = async url => {
      if (!url.endsWith('/history')) throw Error('unexpected request: ' + url);
      return {ok: true, status: 200, json: async () => ({messages, history_revision: 1})};
    };
    await pane._imageTest.replay(true);
  };
  destroy = () => pane.destroy();
}
const attachment = id => [{kind: 'image', attachment_id: id, filename: 'camera.png', mime_type: 'image/png'}];
const url = id => (surface === 'interactive' ? '/node/lab' : '') +
  '/v1/api/workstreams/ws%2F2/attachments/' + encodeURIComponent(id) + '/content';
function expectImages(ids) {
  const images = root.querySelectorAll('img');
  if (JSON.stringify(images.map(i => i.src)) !== JSON.stringify(ids.map(url))) throw Error('rendered image URLs differ');
  return images;
}
const item = {call_id: 'camera', func_name: 'mcp__lab__camera', header: 'Camera', needs_approval: false};
receive({type: 'tool_pending', items: [item]});
receive({type: 'tool_info', items: [{...item, auto_approved: true}]});
receive({type: 'tool_result', call_id: 'camera', name: item.func_name, output: 'received'});
expectImages([]);
const accepted = {type: 'tool_result', call_id: 'camera', name: item.func_name, output: '', accepted: true,
  attachments: attachment('image/1'), _event_id: 101};
receive(accepted);
expectImages(['image/1']);
const replacement = {...accepted, attachments: attachment('image/2'), _event_id: 102};
receive(replacement);
const image = expectImages(['image/2'])[0];
receive(replacement);
if (expectImages(['image/2'])[0] !== image) throw Error('duplicate event rebuilt the image');
await replay([
  {role: 'assistant', content: '', tool_calls: [{id: 'camera', name: item.func_name, arguments: '{}'}]},
  {role: 'tool', content: '', tool_call_id: 'camera', attachments: attachment('image/3'), event_id: 103},
]);
expectImages(['image/3']);
if (surface === 'coordinator') {
  receive({...accepted, call_id: 'orphan', attachments: attachment('image/4'), _event_id: 104});
  expectImages(['image/3', 'image/4']);
  const error = JSON.stringify({error: {code: 'mcp_tool_call_forbidden', server: 'lab', detail: 'Forbidden'}});
  receive({...accepted, call_id: 'orphan-error', is_error: true, output: error,
    attachments: attachment('image/5'), _event_id: 105});
  expectImages(['image/3', 'image/4', 'image/5']);
}
destroy();
"""
    )
    replacements = {
        "__SURFACE__": surface,
        "__SHARED__": shared.as_uri(),
        "__INTERACTIVE__": (shared / "interactive.js").as_uri(),
        "__COORDINATOR__": module.as_uri(),
    }
    for marker, value in replacements.items():
        script = script.replace(marker, json.dumps(value))
    result = run_node_source(script)
    assert result.returncode == 0, result.stderr
