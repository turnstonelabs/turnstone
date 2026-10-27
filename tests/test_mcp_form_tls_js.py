"""admin.js: the MCP server form's Skip certificate verification toggle.

The toggle and its warning show only for a Streamable HTTP server with no authorization or
static headers, and a save or a connection test sends ``tls_skip_verify`` true only from that
visible, checked toggle: per-user OAuth hides it and sends false, and a stdio save leaves the
field out. A new server's form starts with it off, and editing loads the row's setting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"

_FORM_FUNCTIONS = (
    "function toggleMcpAuthFields() {",
    "function _selectedMcpAuthType() {",
    "function _mcpTlsSkipVerifyApplies() {",
    "function _clearMcpTestResult() {",
    "function _parseMcpHeaders() {",
)

_HARNESS = """
const els = {};
function el(id, props) {
  els[id] = Object.assign({ value: "" }, props);
}
let radios = [];
globalThis.document = {
  getElementById: (id) => els[id] || null,
  getElementsByName: () => radios,
  querySelector: () => null,
};
let _mcpTestSeq = 0;
__FUNCTIONS__
function form(transport, auth, checked) {
  radios = ["none", "static", "oauth_user", "oauth_obo"].map((value) => ({
    value,
    checked: value === auth,
  }));
  for (const id of [
    "mcp-headers",
    "mcp-command",
    "mcp-args",
    "mcp-env",
    "mcp-oauth-as-url",
    "mcp-oauth-registration",
    "mcp-oauth-client-id",
    "mcp-oauth-client-secret",
    "mcp-oauth-scopes",
  ]) {
    el(id, {});
  }
  el("mcp-transport", { value: transport });
  el("mcp-name", { value: "kvm" });
  el("mcp-url", { value: "https://kvm.example.com/mcp" });
  el("mcp-oauth-audience", { value: "api://kvm" });
  el("mcp-auto-approve", { checked: false });
  el("mcp-enabled", { checked: true });
  el("mcp-tls-skip-verify", { checked });
  el("mcp-tls-skip-verify-row", { hidden: true });
  el("mcp-tls-skip-verify-hint", { hidden: true });
  toggleMcpAuthFields();
  const payload = _parseMcpForm();
  return {
    shown: !els["mcp-tls-skip-verify-row"].hidden,
    hint: !els["mcp-tls-skip-verify-hint"].hidden,
    sent: "tls_skip_verify" in payload ? payload.tls_skip_verify : "absent",
  };
}
console.log(JSON.stringify(__CASES__.map((c) => form(...c))));
"""

# Every element the reset, edit-load and test functions touch is made on first use.
_LIFECYCLE_HARNESS = """
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
    classList: { add() {}, remove() {} },
    addEventListener() {},
    appendChild() {},
    setAttribute() {},
    removeAttribute() {},
  };
}
let radios = [];
globalThis.document = {
  getElementById: (id) => els[id] || (els[id] = stub(id)),
  getElementsByName: () => radios,
  querySelector: () => null,
};
let _mcpTestSeq = 0;
let fetched = [];
let row = null;
globalThis.authFetch = (url, init) => {
  fetched.push({ url, init });
  const body = row || { ok: true, tools: [], resources: 0, prompts: 0 };
  return Promise.resolve({ ok: true, json: async () => body });
};
function _mcpWire() {}
function _mcpOpen() {}
function toggleMcpTransport() {}
function showToast(message) {
  throw new Error("toast: " + message);
}
function _detectResultLine() {
  return {};
}
function _mcpCount() {
  return "";
}
__FUNCTIONS__
function setAuth(auth) {
  radios = ["none", "static", "oauth_user", "oauth_obo"].map((value) => ({
    value,
    checked: value === auth,
  }));
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function main() {
  const box = document.getElementById("mcp-tls-skip-verify");
  const out = {};
  box.checked = true;
  setAuth("static");
  _mcpResetForm();
  out.reset = box.checked;
  out.edit = [];
  for (const flag of [true, false, undefined]) {
    row = {
      name: "kvm",
      transport: "streamable-http",
      url: "https://kvm.example.com/mcp",
      auth_type: "static",
    };
    if (flag !== undefined) row.tls_skip_verify = flag;
    box.checked = flag !== true;
    showEditMcpModal("id1");
    await tick();
    out.edit.push(box.checked);
  }
  row = null;
  out.test = [];
  for (const [auth, checked] of [
    ["static", true],
    ["none", true],
    ["static", false],
    ["oauth_user", true],
  ]) {
    fetched = [];
    document.getElementById("mcp-transport").value = "streamable-http";
    document.getElementById("mcp-url").value = "https://kvm.example.com/mcp";
    setAuth(auth);
    box.checked = checked;
    testMcpConnection();
    await tick();
    const body = JSON.parse(fetched[0].init.body);
    out.test.push("tls_skip_verify" in body ? body.tls_skip_verify : "absent");
  }
  console.log(JSON.stringify(out));
}
main().catch((e) => {
  console.error(e.stack || String(e));
  process.exit(1);
});
"""


def _functions(*signatures: str) -> str:
    source = _ADMIN.read_text()
    return "\n".join(extract_braced(source, signature) for signature in signatures)


def _run(harness: str) -> Any:
    result = run_node_source(harness)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_toggle_shows_and_sends_only_where_it_applies() -> None:
    cases = [
        ("streamable-http", "static", True),
        ("streamable-http", "none", True),
        ("streamable-http", "static", False),
        ("streamable-http", "oauth_user", True),
        ("streamable-http", "oauth_obo", True),
        ("stdio", "static", True),
    ]
    functions = _functions(*_FORM_FUNCTIONS, "function _parseMcpForm() {")

    results = _run(
        _HARNESS.replace("__FUNCTIONS__", functions).replace("__CASES__", json.dumps(cases))
    )

    assert results == [
        {"shown": True, "hint": True, "sent": True},
        {"shown": True, "hint": True, "sent": True},
        {"shown": True, "hint": True, "sent": False},
        {"shown": False, "hint": False, "sent": False},
        {"shown": False, "hint": False, "sent": False},
        {"shown": False, "hint": False, "sent": "absent"},
    ]


def test_a_new_form_starts_off_an_edit_loads_the_row_and_a_test_sends_it() -> None:
    functions = _functions(
        *_FORM_FUNCTIONS,
        "function _mcpResetForm() {",
        "function showEditMcpModal(serverId) {",
        "function testMcpConnection() {",
    )

    results = _run(_LIFECYCLE_HARNESS.replace("__FUNCTIONS__", functions))

    assert results == {
        # A checked toggle left by an earlier edit does not carry into a new server.
        "reset": False,
        # Only a row whose setting is true loads checked; false and absent load clear.
        "edit": [True, False, False],
        # Per-user OAuth sends false even with the box checked.
        "test": [True, True, False, False],
    }
