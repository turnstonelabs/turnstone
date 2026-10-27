"""admin.js: how the MCP Servers view shows a server that skips certificate verification.

The list marks it, and the detail panel says so beside the URL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"

_HARNESS = """
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
const server = (name, extra) => ({
  server_id: "id-" + name,
  name,
  transport: "streamable-http",
  url: "https://" + name + ".example.com/mcp",
  enabled: true,
  status: {},
  ...extra,
});
const out = {};
_renderMcpServers([
  server("kvm", { tls_skip_verify: true }),
  server("plain", { tls_skip_verify: false }),
]);
const rows = els["admin-mcp-table"].html.split('role="listitem"').slice(1);
out.badges = rows.map((row) => row.includes("no cert check"));
out.detail = [true, false].map((flag) => {
  _openMcpDetail(server("kvm", { tls_skip_verify: flag }));
  return els["mcp-detail-content"].html.includes("Certificate verification");
});
console.log(JSON.stringify(out));
"""


def _detail_panel(source: str) -> str:
    """``_openMcpDetail``, sliced up to the next function: the shared brace walker reads the
    ``//`` in its website URL regex as a comment and loses the function's end."""
    start = source.index("function _openMcpDetail(s) {")
    return source[start : source.index("function hideMcpDetailModal() {", start)]


def _run() -> Any:
    source = _ADMIN.read_text()
    functions = "\n".join(
        [extract_braced(source, "function _renderMcpServers(items) {"), _detail_panel(source)]
    )
    result = run_node_source(_HARNESS.replace("__FUNCTIONS__", functions))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_list_and_the_detail_panel_mark_a_server_that_skips_verification() -> None:
    results = _run()

    assert results["badges"] == [True, False]
    assert results["detail"] == [True, False]
