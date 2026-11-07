"""admin.js: a Node Metadata section header counts its node's keys in words that agree."""

from __future__ import annotations

from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"


def test_header_says_key_for_one_and_keys_otherwise() -> None:
    render = extract_braced(_ADMIN.read_text(), "function _renderNodeMetadata() {")
    result = run_node_source(
        "import assert from 'node:assert/strict';\n"
        + render
        + """
const escapeHtml = (s) => String(s);
let html = "";
const setSafeHtml = (el, value) => { html = value; };
const container = {querySelectorAll: () => []};
globalThis.document = {getElementById: () => container};
const key = (k) => ({key: k, value: "v", source: "manual"});
const _nodeMetaCache = {a: [], b: [key("gpu")], c: [key("gpu"), key("zone")]};
_renderNodeMetadata();
const counts = [...html.matchAll(/<small>\\(([^)]*)\\)<\\/small>/g)].map((m) => m[1]);
assert.deepEqual(counts, ["0 keys", "1 key", "2 keys"]);
"""
    )
    assert result.returncode == 0, result.stderr
