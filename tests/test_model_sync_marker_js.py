"""admin.js: the Models tab's "Sync pending" marker clears itself once the timed reloads ran."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"


def _run(auto_sync_seconds: float) -> dict[str, Any]:
    source = _ADMIN.read_text()
    flag = extract_braced(source, "function _flagModelSyncPending() {")
    clear = extract_braced(source, "function _clearModelSyncPending() {")
    harness = f"""
const classes = new Set();
const btn = {{ classList: {{ add: (c) => classes.add(c), remove: (c) => classes.delete(c) }} }};
globalThis.document = {{ getElementById: (id) => (id === "model-sync-btn" ? btn : null) }};
const timers = [];
globalThis.setTimeout = (fn, ms) => {{ timers.push({{ fn, ms }}); return timers.length; }};
globalThis.clearTimeout = (id) => {{ if (id) timers[id - 1].fn = null; }};
let _modelAutoSyncSeconds = {auto_sync_seconds};
let _modelSyncPendingTimer = null;
{flag}
{clear}
_flagModelSyncPending();
_flagModelSyncPending();  // a second save restarts the wait
const live = timers.filter((t) => t.fn);
const pendingBefore = classes.has("model-sync-pending");
for (const t of live) t.fn();
console.log(JSON.stringify({{
  pendingBefore,
  pendingAfter: classes.has("model-sync-pending"),
  waits: live.map((t) => t.ms),
}}));
"""
    result = run_node_source(harness)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_marker_clears_once_every_node_had_a_timed_reload() -> None:
    outcome = _run(300)
    assert outcome == {"pendingBefore": True, "pendingAfter": False, "waits": [345000]}


def test_without_timed_reloads_the_marker_waits_for_sync() -> None:
    outcome = _run(0)
    assert outcome == {"pendingBefore": True, "pendingAfter": True, "waits": []}
