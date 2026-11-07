"""admin.js: how the MCP Servers view shows the tools a server's catalog leaves out.

A tool whose input schema is not valid JSON Schema is left out of its server's catalog, and the
server's status lists it under ``skipped_tools``. The list marks a server whose connected nodes
leave tools out, the detail panel names each one with the reason on each node, and the connection
test lists the tools a save would leave out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"

_REASON = (
    "inputSchema.properties.Offset.type is not one of "
    "['array', 'boolean', 'integer', 'null', 'number', 'object', 'string'] (2 more errors)"
)

_LIST_HARNESS = """
const els = {};
function stub(id) {
  return {
    id,
    value: "",
    textContent: "",
    html: "",
    classList: { add() {}, remove() {} },
    querySelectorAll: () => [],
    focus() {},
  };
}
globalThis.document = {
  getElementById: (id) => els[id] || (els[id] = stub(id)),
};
globalThis.window = {
  TurnstoneHatch: { openShelf() {}, closeShelf() {}, setBusy() {} },
};
function setSafeHtml(el, html) {
  el.html = html;
}
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
}
function _kebabMenu() {
  return "";
}
function _wireMcpTokenDropButtons() {}
__FUNCTIONS__
const skipped = { name: "getOpportunities", reason: __REASON__ };
const markup = { name: "<b>x</b>", reason: "inputSchema.type is not of type 'string'" };
const node = (extra) => ({
  connected: true,
  tools: 27,
  resources: 0,
  prompts: 0,
  error: "",
  ...extra,
});
const server = (name, status) => ({
  server_id: "id-" + name,
  name,
  transport: "streamable-http",
  url: "https://" + name + ".example.com/mcp",
  enabled: true,
  status,
});
const out = {};
_renderMcpServers([
  // The same tool left out on two nodes is one tool.
  server("core", {
    n1: node({ skipped_tools: [skipped] }),
    n2: node({ skipped_tools: [skipped] }),
  }),
  server("two", { n1: node({ skipped_tools: [skipped, markup] }) }),
  server("clean", { n1: node({ skipped_tools: [] }) }),
  // A read-scoped status carries no list.
  server("unlisted", { n1: node({}) }),
  // Only a connected node's catalog counts.
  server("down", { n1: node({ connected: false, skipped_tools: [skipped] }) }),
  // The title names ten tools and counts the rest.
  server("many", {
    n1: node({
      skipped_tools: Array.from({ length: 12 }, (_, i) => ({ name: "t" + i, reason: "r" })),
    }),
  }),
]);
out.rows = els["admin-mcp-table"].html.split('role="listitem"').slice(1);
_openMcpDetail(
  server("core", {
    n1: node({ skipped_tools: [skipped, markup] }),
    n2: node({ skipped_tools: [] }),
  }),
);
out.detail = els["mcp-detail-content"].html;
// A node on an earlier release reports no list at all.
_openMcpDetail(server("unlisted", { n1: node({}) }));
out.unlistedDetail = els["mcp-detail-content"].html;
console.log(JSON.stringify(out));
"""

_TEST_HARNESS = """
const els = {};
function stub(id) {
  return {
    id,
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    textContent: "",
    dataset: {},
    children: [],
    classList: { add() {}, remove() {} },
    addEventListener() {},
    appendChild(child) {
      this.children.push(child);
    },
    setAttribute() {},
    removeAttribute() {},
  };
}
globalThis.document = {
  getElementById: (id) => els[id] || (els[id] = stub(id)),
  getElementsByName: () => [{ value: "static", checked: true }],
  querySelector: () => null,
  createElement: () => ({ style: {}, textContent: "" }),
};
let _mcpTestSeq = 0;
let body = null;
globalThis.authFetch = () => Promise.resolve({ ok: true, json: async () => body });
__FUNCTIONS__
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function main() {
  document.getElementById("mcp-transport").value = "streamable-http";
  document.getElementById("mcp-url").value = "https://core.example.com/mcp";
  const out = {};
  const cases = [
    ["skipped", { skipped_tools: [{ name: "getOpportunities", reason: __REASON__ }] }],
    ["clean", { skipped_tools: [] }],
    ["unlisted", {}],
  ];
  for (const [label, extra] of cases) {
    delete els["mcp-test-result"];
    body = { ok: true, tools: ["good"], resources: 0, prompts: 0, ...extra };
    testMcpConnection();
    await tick();
    out[label] = els["mcp-test-result"].children.map((line) => [
      line.textContent,
      line.style.color || "",
    ]);
  }
  console.log(JSON.stringify(out));
}
main().catch((e) => {
  console.error(e.stack || String(e));
  process.exit(1);
});
"""


def _detail_panel(source: str) -> str:
    """``_openMcpDetail``, sliced up to the next function: the shared brace walker reads the
    ``//`` in its website URL regex as a comment and loses the function's end."""
    start = source.index("function _openMcpDetail(s) {")
    return source[start : source.index("function hideMcpDetailModal() {", start)]


def _run(harness: str, functions: str) -> Any:
    source = harness.replace("__FUNCTIONS__", functions).replace("__REASON__", json.dumps(_REASON))
    result = run_node_source(source)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _badge(row: str) -> tuple[str, str] | None:
    """The skipped-tools badge in a list row, as (title, text), or None."""
    found = re.search(r'title="([^"]*)">(\d+ tools? skipped)</span>', row)
    return (found.group(1), found.group(2)) if found else None


def test_the_list_marks_a_server_whose_connected_nodes_leave_tools_out() -> None:
    source = _ADMIN.read_text()
    functions = "\n".join(
        [
            extract_braced(source, "function _renderMcpServers(items) {"),
            extract_braced(source, "function _mcpCount(n, noun) {"),
            _detail_panel(source),
        ]
    )

    results = _run(_LIST_HARNESS, functions)

    badges = [_badge(row) for row in results["rows"]]
    assert badges == [
        (
            "Left out because the input schema is not valid JSON Schema: getOpportunities. "
            "Open the server for details.",
            "1 tool skipped",
        ),
        (
            "Left out because the input schema is not valid JSON Schema: getOpportunities, "
            "&lt;b>x&lt;/b>. Open the server for details.",
            "2 tools skipped",
        ),
        None,
        None,
        None,
        (
            "Left out because the input schema is not valid JSON Schema: t0, t1, t2, t3, t4, t5, "
            "t6, t7, t8, t9 and 2 more. Open the server for details.",
            "12 tools skipped",
        ),
    ]


def test_the_detail_panel_names_each_tool_left_out_with_the_reason() -> None:
    source = _ADMIN.read_text()
    functions = "\n".join(
        [
            extract_braced(source, "function _renderMcpServers(items) {"),
            extract_braced(source, "function _mcpCount(n, noun) {"),
            _detail_panel(source),
        ]
    )

    results = _run(_LIST_HARNESS, functions)
    detail = results["detail"]

    # Node n1 leaves two tools out and says so once; node n2 leaves none out.
    assert detail.count("left out: input schema not valid JSON Schema") == 1
    assert "2 tools left out: input schema not valid JSON Schema" in detail
    assert f"getOpportunities: {_REASON}" in detail
    assert "&lt;b>x&lt;/b>: inputSchema.type is not of type 'string'" in detail
    assert "<b>x</b>" not in detail
    # A node that reports no list still opens, with nothing left out.
    assert ">connected</span>" in results["unlistedDetail"]
    assert "left out" not in results["unlistedDetail"]


def test_the_connection_test_lists_the_tools_a_save_would_leave_out() -> None:
    source = _ADMIN.read_text()
    functions = "\n".join(
        extract_braced(source, signature)
        for signature in (
            "function testMcpConnection() {",
            "function _clearMcpTestResult() {",
            "function _selectedMcpAuthType() {",
            "function _mcpTlsSkipVerifyApplies() {",
            "function _parseMcpHeaders() {",
            "function _mcpCount(n, noun) {",
            "function _detectResultLine(text, color) {",
        )
    )

    results = _run(_TEST_HARNESS, functions)

    connected = ["✓ Connected: 1 tool, 0 resources, 0 prompts", "var(--green)"]
    names = ["good", ""]
    where = ["Tested from the console; nodes connect from their own network.", "var(--fg-dim)"]
    assert results == {
        "skipped": [
            connected,
            names,
            ["⚠ 1 tool left out: input schema not valid JSON Schema", "var(--yellow)"],
            [f"getOpportunities: {_REASON}", ""],
            where,
        ],
        "clean": [connected, names, where],
        "unlisted": [connected, names, where],
    }
