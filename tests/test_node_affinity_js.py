"""Execute the browser recovery and continuation flows with controlled HTTP."""

from pathlib import Path

from tests._js_harness_helpers import node_skip, run_node_source, slice_braced_block

pytestmark = node_skip
_APP = Path(__file__).resolve().parents[1] / "turnstone/console/static/app.js"


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
