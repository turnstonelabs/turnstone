"""admin.js: a model action's toast names the default model it stored (#1359)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"
_DASH = chr(0x2014)


def _toast(base: str, body: dict[str, Any]) -> dict[str, Any]:
    toast = extract_braced(_ADMIN.read_text(), "function _modelActionToast(baseMsg, body) {")
    harness = f"""
{toast}
console.log(JSON.stringify(_modelActionToast({json.dumps(base)}, {json.dumps(body)})));
"""
    result = run_node_source(harness)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_toast_names_a_stored_default() -> None:
    assert _toast("Model created", {"default_pinned": "chat"}) == {
        "message": "Model created; 'chat' is now the default model"
    }


def test_a_registry_warning_follows_the_stored_default() -> None:
    body = {"default_pinned": "chat", "registry_warning": "load skipped 'x'"}
    assert _toast("Model updated", body) == {
        "message": f"Model updated; 'chat' is now the default model {_DASH} load skipped 'x'",
        "type": "warn",
    }


def test_without_a_stored_default_the_message_is_unchanged() -> None:
    assert _toast("Model deleted", {"status": "ok"}) == {"message": "Model deleted"}


def test_a_refused_model_action_carries_the_servers_text() -> None:
    # The delete guard's 409 names the setting to change first (#1359).
    helper = extract_braced(_ADMIN.read_text(), "function _modelRefusal(r, fallback) {")
    harness = f"""
{helper}
const out = [];
for (const json of [
  async () => ({{error: "'old' is still named by model.default_alias.", code: "model_in_use"}}),
  async () => {{ throw Error("not json"); }},
]) {{
  try {{
    await _modelRefusal({{json}}, "Failed to delete model");
  }} catch (e) {{
    out.push([e.message, e.refusal]);
  }}
}}
console.log(JSON.stringify(out));
"""
    result = run_node_source(harness)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        ["'old' is still named by model.default_alias.", True],
        ["Failed to delete model", True],
    ]


def test_the_models_tab_delete_shows_a_refusals_own_text() -> None:
    # The delete flow itself, not only its helper: the guard's 409 names the
    # setting to change first, a body that isn't JSON or a network error
    # falls back, and a success toasts and reloads (#1359).
    source = _ADMIN.read_text()
    functions = "\n".join(
        extract_braced(source, signature)
        for signature in (
            "function _deleteModelDefinition(did) {",
            "function _modelRefusal(r, fallback) {",
            "function _modelActionToast(baseMsg, body) {",
        )
    )
    harness = f"""
const toasts = [];
let reloads = 0;
let authFetch;
const showToast = (message, type) => toasts.push([message, type || null]);
const _flagModelSyncPending = () => {{}};
const loadAdminModels = () => {{ reloads++; }};
{functions}
const refusal = "'old' is still named by model.default_alias. Point that setting at another model.";
const answers = [
  async () => ({{ok: false, status: 409, json: async () => ({{error: refusal, code: "model_in_use"}})}}),
  async () => ({{ok: false, status: 502, json: async () => {{ throw Error("html"); }}}}),
  async () => {{ throw TypeError("Failed to fetch"); }},
  async () => ({{ok: true, status: 200, json: async () => ({{status: "ok"}})}}),
];
for (const answer of answers) {{
  authFetch = answer;
  await _deleteModelDefinition("m1");
}}
console.log(JSON.stringify({{toasts, reloads}}));
"""
    result = run_node_source(harness)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "toasts": [
            [
                "'old' is still named by model.default_alias. Point that setting at another model.",
                None,
            ],
            ["Failed to delete model", None],
            ["Failed to delete model", None],
            ["Model deleted", None],
        ],
        "reloads": 1,
    }
