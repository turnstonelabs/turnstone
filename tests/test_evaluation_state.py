"""The ``evaluation`` workstream state, and every place that lists the states.

``evaluation`` means the intent judge holds a tool batch (a Smart Approvals
wait) and may still approve it with no person.  Most per-state tables derive
from ``WorkstreamState`` (Python) or from ``STATE_DISPLAY`` (the console);
the checks below run those through the real code.  The tables that must stay
hand-written (labels, glyphs, CSS, sort ranks, the TypeScript types, the
coordinator's tool description, the reaper index) are walked state by state,
so the next state cannot be forgotten in one of them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source
from turnstone.core.workstream import BULK_CLOSE_STATE_VALUES, WorkstreamState

_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ROOT / "turnstone"
_RAIL_JS = _PKG / "shared_static/rail.js"
_NODE_APP_JS = _PKG / "ui/static/app.js"
_CONSOLE_APP_JS = _PKG / "console/static/app.js"
_COORD_JS = _PKG / "console/static/coordinator/coordinator.js"
_INTERACTIVE_JS = _PKG / "shared_static/interactive.js"
_BASE_CSS = _PKG / "shared_static/base.css"
_UI_BASE_CSS = _PKG / "shared_static/ui-base.css"
_TS_EVENTS = _ROOT / "sdk/typescript/src/events.ts"
_TS_TYPES = _ROOT / "sdk/typescript/src/types.ts"
_LIST_WORKSTREAMS_TOOL = _PKG / "tools/list_workstreams.json"

_STATES = [member.value for member in WorkstreamState]


def _ts_interface(name: str) -> str:
    source = _TS_TYPES.read_text()
    return extract_braced(source, f"export interface {name} {{")


def _js_object_keys(source: str, anchor: str) -> dict[str, str]:
    """``{key: value-text}`` of the JS object literal that opens at *anchor*."""
    body = extract_braced(source, anchor)
    return dict(re.findall(r"^\s*(\w+):\s*(.*?),?\s*$", body, re.MULTILINE))


# ---------------------------------------------------------------------------
# Tables derived from the enum, checked through the real code
# ---------------------------------------------------------------------------


def test_server_and_collector_counts_carry_every_state():
    from turnstone.console.collector import ClusterCollector, NodeSnapshot
    from turnstone.server import _count_ws_states

    assert set(_count_ws_states([])) == set(_STATES)

    collector = ClusterCollector(storage=MagicMock(), discovery_interval=999)
    collector._nodes["node-a"] = NodeSnapshot(
        node_id="node-a",
        server_url="http://node-a",
        workstreams={
            "ws1": {"id": "ws1", "state": "evaluation"},
            "ws2": {"id": "ws2", "state": "attention"},
        },
    )
    assert set(collector.get_overview()["states"]) == set(_STATES)
    with collector._lock:
        assert set(collector._build_snapshot_locked()["overview"]["states"]) == set(_STATES)
    nodes, _total = collector.get_nodes()
    assert {f"ws_{state}" for state in _STATES} <= set(nodes[0])
    assert (nodes[0]["ws_evaluation"], nodes[0]["ws_attention"]) == (1, 1)


def test_health_counts_carry_evaluation():
    from turnstone.server import _count_ws_states

    workstreams: list[Any] = []
    for state in (WorkstreamState.EVALUATION, WorkstreamState.ATTENTION):
        ws = MagicMock()
        ws.state = state
        workstreams.append(ws)
    counts = _count_ws_states(workstreams)
    assert (counts["evaluation"], counts["attention"]) == (1, 1)


def test_cli_reads_and_lists_every_state():
    from turnstone.cli import _STATE_DISPLAY, _state_from_wire

    assert set(_STATE_DISPLAY) == set(WorkstreamState)
    for member in WorkstreamState:
        assert _state_from_wire(member.value) is member
    # ``closed`` rows and anything unknown read as idle, as before.
    assert _state_from_wire("closed") is WorkstreamState.IDLE


def test_nodes_with_judge_held_work_rank_as_active():
    from turnstone.console.collector import ClusterCollector, NodeSnapshot

    collector = ClusterCollector(storage=MagicMock(), discovery_interval=999)
    for node_id, state in (("node-a", "idle"), ("node-b", "evaluation")):
        collector._nodes[node_id] = NodeSnapshot(
            node_id=node_id,
            server_url=f"http://{node_id}",
            workstreams={"ws-" + node_id: {"id": "ws-" + node_id, "state": state}},
        )
    nodes, _total = collector.get_nodes(sort_by="activity")
    assert [n["node_id"] for n in nodes] == ["node-b", "node-a"]


# ---------------------------------------------------------------------------
# Hand-written tables, walked state by state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", _STATES)
def test_every_schema_and_sdk_type_lists_the_state(state: str):
    from turnstone.api.console_schemas import ClusterNodeInfo, StateCounts
    from turnstone.api.schemas import WorkstreamState as ApiWorkstreamState
    from turnstone.api.server_schemas import WorkstreamCounts

    assert ApiWorkstreamState(state).value == state
    assert state in WorkstreamCounts.model_fields
    assert state in StateCounts.model_fields
    assert f"ws_{state}" in ClusterNodeInfo.model_fields
    console_spec = (_PKG / "api/console_spec.py").read_text()
    assert re.search(r'enum=\[[^\]]*"' + state + r'"', console_spec), "cluster state filter"
    assert f'"{state}"' in _TS_EVENTS.read_text().split("StateChangeEvent", 1)[1][:400]
    assert re.search(rf"\b{state}\?: number;", _ts_interface("WorkstreamCounts"))
    assert re.search(rf"\b{state}\?: number;", _ts_interface("StateCounts"))
    assert re.search(rf"\bws_{state}: number;", _ts_interface("ClusterNodeInfo"))


@pytest.mark.parametrize("state", _STATES)
def test_every_client_display_table_lists_the_state(state: str):
    rail = _RAIL_JS.read_text()
    assert state in _js_object_keys(rail, "const GLYPH = {"), "rail glyph"
    assert state in _js_object_keys(rail, "const STATE_LABEL = {"), "rail label"
    for path in (_NODE_APP_JS, _CONSOLE_APP_JS):
        assert state in _js_object_keys(path.read_text(), "const STATE_DISPLAY = {"), path
    glyph_fn = extract_braced(_COORD_JS.read_text(), "function stateGlyph(state) {")
    assert f'case "{state}":' in glyph_fn
    assert f".ui-glyph-{state}" in _UI_BASE_CSS.read_text()
    base = _BASE_CSS.read_text()
    for selector in ("dash-row", "dash-state-dot", "dash-state-label"):
        assert f'.{selector}[data-state="{state}"]' in base, selector


def test_the_two_state_sort_ranks_agree():
    """The cluster list sorts on the server (``sort=state``) and in the
    console; both rank every state, the same way."""
    from turnstone.console.collector import _STATE_SORT_RANK

    console = _CONSOLE_APP_JS.read_text()
    console_rank = {k: int(v) for k, v in _js_object_keys(console, "const stateOrder = {").items()}
    assert console_rank == _STATE_SORT_RANK
    assert set(_STATE_SORT_RANK) == set(_STATES)
    assert _STATE_SORT_RANK["evaluation"] == _STATE_SORT_RANK["attention"]


def test_the_coordinator_model_is_told_every_state():
    """``list_workstreams`` is where a coordinator model learns the states it
    can filter on and meet in results."""
    tool = json.loads(_LIST_WORKSTREAMS_TOOL.read_text())
    description = json.dumps(tool)
    for state in _STATES:
        assert f"'{state}'" in description, state


def test_evaluation_is_a_live_state_for_reaping_and_children():
    """Like ``attention``, a workstream the judge holds is mid-turn: the
    orphan reap may close it once stale (migration 081 rebuilds the reaper's
    index to match), and a coordinator counts the child as active."""
    from turnstone.console.coordinator_idle_observer import _ACTIVE_CHILD_STATES

    assert "evaluation" in BULK_CLOSE_STATE_VALUES
    assert "evaluation" in _ACTIVE_CHILD_STATES


@pytest.mark.parametrize(
    ("path", "anchor"),
    [
        (_INTERACTIVE_JS, 'if (evt.state === "idle" || evt.state === "error") {'),
        (_COORD_JS, 'if (ev.state === "idle" || ev.state === "error") {'),
    ],
    ids=["interactive-pane", "coordinator-pane"],
)
def test_panes_are_busy_in_every_state_but_idle_and_error(path: Path, anchor: str):
    """The composer must not unlock while the batch waits on the judge (or in
    any state a later change adds): everything but idle/error is busy."""
    source = path.read_text()
    # The second idle/error branch is the busy split (the first tracks the
    # acting user).  What follows its block must be a plain ``else``.
    start = source.index(anchor, source.index(anchor) + 1)
    settled = extract_braced(source[start:], anchor)
    after = source[start + len(settled) :].lstrip()
    assert after.startswith("else {"), after[:60]
    assert "setBusy(true)" in after[:400]


# ---------------------------------------------------------------------------
# Counts and labels, run under node
# ---------------------------------------------------------------------------


@node_skip
def test_console_summary_and_node_info_count_evaluation():
    console = _CONSOLE_APP_JS.read_text()
    source = "\n".join(
        [
            extract_braced(console, "const STATE_DISPLAY = {") + ";",
            extract_braced(console, "function zeroStateCounts() {"),
            "let clusterState = {nodes: {",
            "  n1: {node_id: 'n1', aggregate: {}, workstreams: [",
            "    {state: 'evaluation'}, {state: 'attention'}, {state: 'running'},",
            "  ]},",
            "}};",
            extract_braced(console, "function recomputeOverview() {"),
            extract_braced(console, "function buildNodeInfoFromSnapshot(node) {"),
            "recomputeOverview();",
            "const info = buildNodeInfoFromSnapshot(clusterState.nodes.n1);",
            "console.log(JSON.stringify([clusterState.overview.states, info]));",
        ]
    )
    proc = run_node_source(source)
    assert proc.returncode == 0, proc.stderr
    states, info = json.loads(proc.stdout)
    assert set(states) == set(_STATES)
    assert (states["evaluation"], states["attention"], states["running"]) == (1, 1, 1)
    # Every state's count rides on the node row, as on the collector's rows.
    assert {f"ws_{state}" for state in _STATES} <= set(info)
    assert (info["ws_attention"], info["ws_evaluation"]) == (1, 1)


@node_skip
def test_node_row_glyph_ranks_attention_over_evaluation():
    rail = _RAIL_JS.read_text()
    source = (
        extract_braced(rail, "function nodeState(info) {")
        + "\nconst base = {reachable: true, health: {}};\n"
        + "console.log(JSON.stringify(["
        + "nodeState({...base, ws_attention: 1, ws_evaluation: 1}),"
        + "nodeState({...base, ws_evaluation: 2}),"
        + "nodeState({...base})]));\n"
    )
    proc = run_node_source(source)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["attention", "evaluation", "idle"]
    assert 'for (const st of ["attention", "evaluation", "error"])' in rail


@node_skip
def test_dashboard_row_label_speaks_the_current_state():
    """The spoken label names the state in words, and every paint (first
    render and live update) composes it from the parts stored on the row."""
    node = _NODE_APP_JS.read_text()
    source = "\n".join(
        [
            "function formatTokens(n) { return String(n); }",
            "const PERSISTENCE_DISPLAY = {};",
            extract_braced(node, "function setPersistenceRowAria(row, persistenceState) {"),
            extract_braced(node, "function paintDashRowAria(row, sd, persistenceState) {"),
            "const attrs = {};",
            "const row = {dataset: {ariaName: 'db-migrate', ariaModel: 'opus', ariaTask: '',",
            "  ariaTokens: '1200', ariaCtx: '0.25'},",
            "  setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k]; }};",
            "paintDashRowAria(row, {label: 'eval', aria: 'judge evaluation'}, 'healthy');",
            "console.log(attrs['aria-label']);",
        ]
    )
    proc = run_node_source(source)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (
        "db-migrate — judge evaluation, model: opus, 1200 tokens, 25% context"
    )
    update = extract_braced(node, "function updateTabIndicator(wsId, state, extra) {")
    assert "paintDashRowAria(" in update
