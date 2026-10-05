"""Execute the browser recovery and continuation flows with controlled HTTP."""

from pathlib import Path

from tests._js_harness_helpers import node_skip, run_node_source, slice_braced_block

pytestmark = node_skip
_APP = Path(__file__).resolve().parents[1] / "turnstone/console/static/app.js"
_UTILS = Path(__file__).resolve().parents[1] / "turnstone/shared_static/utils.js"
_NODE_APP = Path(__file__).resolve().parents[1] / "turnstone/ui/static/app.js"


def _refusal_helpers() -> str:
    """``leaseRefusalWhere`` and ``readRefusal`` from the shared utils module, as plain functions."""
    utils = _UTILS.read_text()
    out = ""
    for name, params in (("leaseRefusalWhere", "status, data"), ("readRefusal", "r")):
        body = slice_braced_block(utils, utils.index(f"export function {name}("))
        assert body is not None
        out += f"function {name}({params}) {body}\n"
    return out


def test_browser_recovery_preserves_required_node_and_refreshes_stale_hints():
    source = _APP.read_text()
    seam = source[
        source.index("window.TS_APP.resolveInteractiveNode = function") : source.index(
            "// === MCP consent badge"
        )
    ]
    result = run_node_source(
        """
import assert from 'node:assert/strict';
const window = {TS_APP:{}};
let authFetch;
"""
        + _refusal_helpers()
        + seam
        + """
const response = (status, body={}) => ({status, ok:status===200, json:async()=>body});
for (const hintStatus of [404, 502, 409]) {
  const calls=[];
  authFetch=async(url)=>{
    calls.push(url);
    if(calls.length===1) return response(hintStatus, hintStatus===409 ? {code:'wrong_execution_node',required_node_id:'host-1'} : {});
    if(url.startsWith('/v1/api/route?')) return response(503, {code:'required_node_unavailable',required_node_id:'host-1'});
    throw Error('An unavailable required node must not fall back to another executor');
  };
  const result=await window.TS_APP.resolveInteractiveNode('saved','old-hint');
  assert.equal(result.code,'required_node_unavailable');
  assert.equal(result.requiredNodeId,'host-1');
  assert.equal(result.canContinue,true);
  assert.equal(calls.length,2);
}
for(const hintStatus of [200,403,429]) {
  for(const code of [undefined,'wrong_execution_node','required_node_unavailable']) {
    let calls=0;
    authFetch=async()=>{calls++;return response(hintStatus,{code,required_node_id:'host-1'})};
    const result=await window.TS_APP.resolveInteractiveNode('saved','host-1');
    assert.equal(calls,1);
    assert.equal(Boolean(result.nodeId),hintStatus===200);
    assert.equal(Boolean(result.canContinue),false);
  }
}
for(const required of [false,true]) {
  const calls=[];
  authFetch=async(url)=>{
    calls.push(url);
    if(calls.length===1) return response(required?409:502,required?{code:'wrong_execution_node',required_node_id:'host-1'}:{});
    if(calls.length===2) return response(200,{node_id:required?'host-1':'node-1'});
    return response(200);
  };
  const result=await window.TS_APP.resolveInteractiveNode('saved','stale');
  assert.equal(result.nodeId,required?'host-1':'node-1');
  assert.equal(calls.length,3);
}
"""
    )
    assert result.returncode == 0, result.stderr


def test_explicit_continuation_uses_launcher_with_new_identity():
    source = _APP.read_text()
    functions = "\n".join(
        source[
            source.index("function " + name + "(") : source.index(
                "{", source.index("function " + name + "(")
            )
        ]
        + slice_braced_block(source, source.index("function " + name + "("))
        for name in (
            "_resolveNodePlacement",
            "_createWorkstreamFetchOpts",
            "_createInteractive",
        )
    )
    seam_start = source.index("window.TS_APP.continueInteractiveElsewhere = function")
    seam = source[seam_start : source.index("// === MCP consent badge", seam_start)]
    result = run_node_source(
        """
import assert from 'node:assert/strict';
let busy=false, posted=[], opened=[];
const dlg={open:false,hasAttribute:()=>busy,close(){this.open=false}};
const select={options:[],value:'',replaceChildren(...rows){this.options=rows;this.value=''},add(row){this.options.push(row)}};
const error={textContent:''}, submit={};
const elements={'continue-session-dialog':dlg,'continue-session-node':select,'continue-session-error':error,'continue-session-submit':submit};
const document={getElementById:id=>elements[id]};
function Option(text,value){this.text=text;this.value=value}
const clusterState={nodes:{'host-1':{},'node-1':{},'offline':{reachable:false},console:{}}};
const window={TS_APP:{},TurnstoneHatch:{openDialog(d){d.open=true},setBusy(d,b){busy=b}},TS_SHELL:{panes:{openPane(...args){opened.push(args)}}}};
const authFetch=async(url,opts)=>{posted.push({url,body:JSON.parse(opts.body)});return{ok:true,status:200,json:async()=>({correlation_id:'new-id',target_node:'node-1'})}};
"""
        + functions
        + seam
        + """
window.TS_APP.continueInteractiveElsewhere('saved-id','host-1');
assert.deepEqual(select.options.map(x=>x.value),['','node-1']);
submit.onclick();
assert.equal(posted.length,0);
assert.match(error.textContent,/Choose a node/);
select.value='node-1';
submit.onclick();
submit.onclick(); // Busy guard prevents a duplicate continuation.
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(posted.length,1);
assert.deepEqual(posted[0],{url:'/v1/api/cluster/workstreams/new',body:{node_id:'node-1',required_node_id:'node-1',resume_ws:'saved-id',resume_ws_exact:true}});
assert.deepEqual(opened,[['interactive','new-id',{nodeId:'node-1'}]]);
assert.equal(dlg.open,false);
for(const nodeId of ['auto','pool']) {
  clusterState.nodes[nodeId]={};
  window.TS_APP.continueInteractiveElsewhere('saved-id','host-1');
  select.value=nodeId;
  submit.onclick();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(posted.at(-1).body.node_id,nodeId);
  assert.equal(posted.at(-1).body.required_node_id,nodeId);
}
"""
    )
    assert result.returncode == 0, result.stderr


def test_browser_recovery_follows_a_lease_refusal_to_the_holder():
    source = _APP.read_text()
    seam = source[
        source.index("window.TS_APP.resolveInteractiveNode = function") : source.index(
            "// === MCP consent badge"
        )
    ]
    result = run_node_source(
        """
import assert from 'node:assert/strict';
const window = {TS_APP:{}};
let authFetch;
"""
        + _refusal_helpers()
        + seam
        + """
const response = (status, body={}) => ({status, ok:status===200, json:async()=>body});
const held = {code:'workstream_lease_held', holder_node_id:'node-b', retry_after_ms:1200};
{
  const calls=[];
  authFetch=async(url)=>{
    calls.push(url);
    if(calls.length===1) return response(409, held);
    if(url.startsWith('/v1/api/route?')) return response(200, {node_id:'node-b'});
    return response(200);
  };
  const result=await window.TS_APP.resolveInteractiveNode('moved','node-a');
  assert.equal(result.nodeId,'node-b');
  assert.deepEqual(calls.map(url=>url.split('/v1/')[0]), ['/node/node-a', '', '/node/node-b']);
}
for (const holder of ['node-b', '<img src=x>', undefined]) {
  authFetch=async(url)=>{
    if(url.startsWith('/v1/api/route?')) return response(200, {node_id:'node-a'});
    return response(409, {...held, holder_node_id:holder});
  };
  const result=await window.TS_APP.resolveInteractiveNode('moved','node-a');
  assert.equal(result.nodeId,undefined);
  assert.match(result.error, holder==='node-b' ? /open on node 'node-b'/ : /open in another process/);
}
"""
    )
    assert result.returncode == 0, result.stderr


def test_only_a_lease_refusal_reads_as_open_elsewhere() -> None:
    """A wrong-node 409 must not tell the user the workstream is open elsewhere."""
    result = run_node_source(
        """
import assert from 'node:assert/strict';
"""
        + _refusal_helpers()
        + """
const held = {code:'workstream_lease_held', holder_node_id:'node-b', retry_after_ms:1200};
assert.equal(leaseRefusalWhere(409, held), "on node 'node-b'");
assert.equal(leaseRefusalWhere(409, {...held, holder_node_id:'<img src=x>'}), 'in another process');
assert.equal(leaseRefusalWhere(409, {code:'workstream_lease_held'}), 'in another process');
assert.equal(leaseRefusalWhere(409, {code:'wrong_execution_node', error:'requires host-1'}), null);
assert.equal(leaseRefusalWhere(409, {error:'name taken'}), null);
assert.equal(leaseRefusalWhere(404, held), null);
assert.equal(leaseRefusalWhere(409, null), null);
"""
    )
    assert result.returncode == 0, result.stderr


def test_a_refusal_is_read_once_whatever_its_body() -> None:
    result = run_node_source(
        """
import assert from 'node:assert/strict';
"""
        + _refusal_helpers()
        + """
const response = (status, json) => ({status, json});
const held = {code:'workstream_lease_held', holder_node_id:'node-b', error:'open on node-b'};
let reads = 0;
const once = await readRefusal(response(409, async()=>{reads++; return held;}));
assert.deepEqual(once, {data: held, where: "on node 'node-b'"});
assert.equal(reads, 1);
assert.deepEqual(await readRefusal(response(409, async()=>{throw Error('not json')})), {data:{}, where:null});
assert.deepEqual(await readRefusal(response(409, ()=>{throw Error('sync')})), {data:{}, where:null});
assert.deepEqual(await readRefusal(response(409, async()=>null)), {data:{}, where:null});
assert.deepEqual(await readRefusal(response(409, async()=>'text')), {data:{}, where:null});
const other = {code:'wrong_execution_node', error:'requires host-1'};
assert.deepEqual(await readRefusal(response(409, async()=>other)), {data: other, where: null});
"""
    )
    assert result.returncode == 0, result.stderr


def test_console_drops_an_unloaded_workstream_from_that_node_only():
    """``ws_unloaded``: the workstream did not close, so panes and the saved list stay."""
    app = _APP.read_text()
    handlers = ""
    for name in ("patchClusterState", "handleClusterEvent"):
        body = slice_braced_block(app, app.index(f"function {name}("))
        assert body is not None
        handlers += f"function {name}(data) {body}\n"
    result = run_node_source(
        """
import assert from 'node:assert/strict';
let renders = 0, savedReloads = 0, paneCloses = 0;
const window = { TS_SHELL: { notifySessionClosed: () => paneCloses++ } };
const scheduleRender = () => renders++;
const loadSavedCoordinators = () => savedReloads++;
const showToast = () => {};
const applySnapshot = () => {};
let clusterState = { nodes: {
  "node-a": { node_id: "node-a", workstreams: [{ id: "ws1" }, { id: "ws2" }] },
  "node-b": { node_id: "node-b", workstreams: [{ id: "ws1" }] },
} };
"""
        + handlers
        + """
handleClusterEvent({ type: "ws_unloaded", ws_id: "ws1", node_id: "node-a" });
assert.deepEqual(clusterState.nodes["node-a"].workstreams.map((w) => w.id), ["ws2"]);
assert.deepEqual(clusterState.nodes["node-b"].workstreams.map((w) => w.id), ["ws1"]);
assert.equal(renders, 1);
assert.equal(savedReloads + paneCloses, 0);
"""
    )
    assert result.returncode == 0, result.stderr


def test_node_ui_drops_an_unloaded_workstream_without_closing_its_pane():
    app = _NODE_APP.read_text()
    anchor = app.index("globalEvtSource.onmessage = function (e) ")
    body = slice_braced_block(app, anchor)
    assert body is not None
    result = run_node_source(
        """
import assert from 'node:assert/strict';
let globalLastEventId = null, renders = 0, paneCloses = 0, toasts = 0, dashboards = 0;
const workstreams = { ws1: { name: "a" }, ws2: { name: "b" } };
const fireRender = () => renders++;
const closeSessionPane = () => paneCloses++;
const showToast = () => toasts++;
const showDashboard = () => dashboards++;
const resyncRoster = () => {};
const onGlobal = function (e) """
        + body
        + """;
onGlobal({ data: JSON.stringify({ type: "ws_unloaded", ws_id: "ws1" }), lastEventId: "" });
assert.deepEqual(Object.keys(workstreams), ["ws2"]);
assert.equal(renders, 1);
assert.equal(paneCloses + toasts + dashboards, 0);
"""
    )
    assert result.returncode == 0, result.stderr
