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
if (img.alt !== images[0].filename || link.rel !== 'noopener noreferrer') throw Error('unsafe metadata');
// Setting details.open queues a browser toggle event. It must not load twice.
view.dispatch('toggle');
if (view.children[1].children[0] !== link) throw Error('initial toggle rerendered');
for (const delay of [250, 750, 2000]) {{
  img.dispatch('error');
  if (link.textContent.includes('unavailable')) throw Error('transient failure shown too soon');
  tick(delay);
}}
img.dispatch('error');
if (!link.textContent.includes('reopen')) throw Error('missing retry explanation');
if (timers.size) throw Error('retries must be bounded');
view.open = false;
view.dispatch('toggle');
view.open = true;
view.dispatch('toggle');
if (view.children[1].children.length !== 1 || view.children[1].children[0] === link) throw Error('retry must replace');
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
if (timers.size || recovered.link.textContent.includes('unavailable')) throw Error('recovery failed');
const success = fixture();
success.img.dispatch('error');
success.img.dispatch('load');
if (timers.size) throw Error('success must cancel pending retry');
for (const action of ['close', 'remove', 'replace']) {{
  const f = fixture();
  f.img.dispatch('error');
  if (action === 'remove') f.view.remove();
  else {{
    f.view.open = false;
    f.view.dispatch('toggle');
    if (action === 'replace') {{
      f.view.open = true;
      f.view.dispatch('toggle');
    }}
  }}
  tick(250);
  if (f.writes() || timers.size) throw Error(action + ' must stop old image retries');
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


def test_live_and_history_surfaces_pass_attachment_metadata():
    interactive = (ROOT / "turnstone/shared_static/interactive.js").read_text()
    coordinator = (ROOT / "turnstone/console/static/coordinator/coordinator.js").read_text()
    assert "attachments: evt.attachments" in interactive
    assert "buildToolImages(msg.attachments" in interactive
    assert "buildToolImages(opts.attachments" in interactive
    assert "attachments: ev.attachments" in coordinator
    assert "attachments: m.attachments" in coordinator
    assert "buildToolImages(opts && opts.attachments" in coordinator
