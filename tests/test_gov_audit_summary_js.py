"""governance.js: the Audit tab's one-line summary of an event's detail."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_GOV = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "governance.js"

_HARNESS = """
const els = {};
globalThis.document = {
  getElementById: (id) =>
    els[id] || (els[id] = { id, html: "", querySelector: () => null }),
};
function setSafeHtml(el, html) {
  el.html = html;
}
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function loadMoreAudit() {}
__FUNCTIONS__
const event = (detail) => ({
  timestamp: "2026-10-26T00:00:00",
  user_id: "u1",
  action: "mcp_server.update",
  resource_type: "mcp_server",
  resource_id: "abc",
  detail: JSON.stringify(detail),
});
_renderGovAudit(
  [
    event({ name: "kvm", header_references: { Authorization: "secret://vault/mcp" } }),
    event({ name: "kvm", url: "https://mcp.example.com:8443", enabled: false }),
    event({ imported: ["kvm", "plain"], skipped: [], tls_skip_verify: ["kvm"] }),
  ],
  3,
);
console.log(JSON.stringify(els["admin-audit-table"].html));
"""


def test_an_object_shows_as_json_and_a_list_stays_comma_joined() -> None:
    source = _GOV.read_text()
    functions = "\n".join(
        extract_braced(source, signature)
        for signature in (
            "function _relativeTime(isoStr) {",
            "function _renderGovAudit(events, total) {",
        )
    )
    result = run_node_source(_HARNESS.replace("__FUNCTIONS__", functions))
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)
    cells = re.findall(r'admin-col-adetail" title="[^"]*">([^<]*)</span>', html)

    assert cells == [
        "name=kvm, header_references={&quot;Authorization&quot;:&quot;secret://vau",
        "name=kvm, url=https://mcp.example.com:8443, enabled=false",
        # A list reads as it always did: only an object used to show [object Object].
        "imported=kvm,plain, skipped=, tls_skip_verify=kvm",
    ]
