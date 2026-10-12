"""admin.js: the model form's capability lookup and effort-ladder preview (#1366).

An id with no table row shows the provider's default row on the capability tiles,
without the provenance banner or a context-window autofill.  The Thinking mode and
Effort param fields move between a stored definition and the form only on the two
compat lanes: one helper lifts them out at edit-load, and one lays them and the
explicit tiles over the raw JSON for the save and the ladder preview alike.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"

_ELEMENTS = """
const els = {};
function el(id, props) {
  els[id] = Object.assign({ value: "" }, props);
}
globalThis.document = { getElementById: (id) => els[id] || null };
"""


def _functions(*signatures: str) -> str:
    source = _ADMIN.read_text()
    return "\n".join(extract_braced(source, signature) for signature in signatures)


def _run(harness: str) -> Any:
    result = run_node_source(harness)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _baseline(payload: dict[str, Any]) -> dict[str, Any]:
    harness = (
        _ELEMENTS
        + """
el("model-shelf", { open: true });
el("model-provider", { value: "anthropic" });
el("model-name", { value: "claude-unknown-9" });
el("model-autofill", { hidden: true });
el("model-autofill-name", { textContent: "" });
el("model-ctx-window", { value: "0" });
const _MODEL_CAP_KEYS = ["supports_vision", "supports_temperature", "supports_effort"];
let _modelCapsSeq = 0;
let _modelCapsBaseline = {};
let rendered = 0;
function _modelRenderTiles() {
  rendered += 1;
}
const payload = __PAYLOAD__;
function authFetch() {
  return Promise.resolve({ json: () => payload });
}
__FUNCTIONS__
_modelCapsRefreshBaseline();
setTimeout(() => {
  console.log(JSON.stringify({
    baseline: _modelCapsBaseline,
    bannerHidden: els["model-autofill"].hidden,
    ctx: els["model-ctx-window"].value,
    name: els["model-autofill-name"].textContent,
    rendered,
  }));
}, 0);
"""
    )
    harness = harness.replace("__PAYLOAD__", json.dumps(payload))
    harness = harness.replace("__FUNCTIONS__", _functions("function _modelCapsRefreshBaseline() {"))
    return _run(harness)


def _preview(provider: str, explicit: dict[str, bool], surface: str = "") -> dict[str, Any]:
    """The body the effort-ladder preview posts."""
    harness = (
        _ELEMENTS
        + """
el("model-shelf", { open: true });
el("model-reasoning-effort", {});
el("model-provider", { value: __PROVIDER__ });
el("model-name", { value: "m" });
el("model-capabilities", { value: "" });
el("model-thinking-mode", { value: "manual" });
el("model-thinking-param", { value: "enable_thinking" });
el("model-effort-param", { value: "reasoning_effort" });
el("model-api-surface", { value: __SURFACE__ });
let _effortLadderSeq = 0;
const _modelCapsExplicit = __EXPLICIT__;
function _modelGetTile(key) {
  return _modelCapsExplicit[key];
}
function _annotateEffortSelect() {}
let sent = null;
function authFetch(url, init) {
  sent = JSON.parse(init.body);
  return Promise.resolve({ ok: false });
}
__FUNCTIONS__
_refreshModelEffortLadder();
console.log(JSON.stringify(sent));
"""
    )
    harness = harness.replace("__PROVIDER__", json.dumps(provider))
    harness = harness.replace("__EXPLICIT__", json.dumps(explicit))
    harness = harness.replace("__SURFACE__", json.dumps(surface))
    harness = harness.replace(
        "__FUNCTIONS__",
        _functions(
            "function _isPlainObject(v) {",
            "function _isCompatLane(provider) {",
            "function _overlayModelFormCaps(caps, provider) {",
            "function _refreshModelEffortLadder() {",
        ),
    )
    return _run(harness)


def _edit_and_save(
    provider: str, stored: dict[str, Any], typed: dict[str, str] | None = None
) -> dict[str, Any]:
    """Edit-load *stored* into the form, type *typed* into its fields, then
    save: the fields, the raw JSON left after the lift, and what is saved."""
    harness = (
        _ELEMENTS
        + """
el("model-thinking-mode", {});
el("model-thinking-param", {});
el("model-effort-param", {});
const _modelCapsExplicit = {};
function _modelGetTile(key) {
  return _modelCapsExplicit[key];
}
__FUNCTIONS__
const provider = __PROVIDER__;
const caps = __STORED__;
_liftModelTemplateFields(caps, provider);
const raw = JSON.parse(JSON.stringify(caps));
const fields = {
  thinking_mode: els["model-thinking-mode"].value,
  thinking_param: els["model-thinking-param"].value,
  effort_param: els["model-effort-param"].value,
};
const typed = __TYPED__;
Object.keys(typed).forEach((id) => {
  els[id].value = typed[id];
});
const saved = JSON.parse(JSON.stringify(raw));
_overlayModelFormCaps(saved, provider);
console.log(JSON.stringify({ fields, raw, saved }));
"""
    )
    harness = harness.replace("__PROVIDER__", json.dumps(provider))
    harness = harness.replace("__STORED__", json.dumps(stored))
    harness = harness.replace("__TYPED__", json.dumps(typed or {}))
    harness = harness.replace(
        "__FUNCTIONS__",
        _functions(
            "function _isCompatLane(provider) {",
            "function _liftModelTemplateFields(capsObj, provider) {",
            "function _overlayModelFormCaps(caps, provider) {",
        ),
    )
    return _run(harness)


_TEMPLATE_FIELDS = {
    "thinking_mode": "manual",
    "thinking_param": "thinking",
    "effort_param": "reasoning_effort",
}


def test_an_unlisted_id_shows_the_default_row_without_banner_or_autofill() -> None:
    outcome = _baseline(
        {
            "known": False,
            "capabilities": {},
            "defaults": {
                "supports_vision": True,
                "supports_temperature": False,
                "supports_effort": True,
                "context_window": 200000,
            },
        }
    )
    assert outcome == {
        "baseline": {
            "supports_vision": True,
            "supports_temperature": False,
            "supports_effort": True,
        },
        "bannerHidden": True,
        "ctx": "0",
        "name": "",
        "rendered": 1,
    }


def test_a_listed_row_keeps_the_banner_and_the_window_autofill() -> None:
    outcome = _baseline(
        {
            "known": True,
            "capabilities": {"supports_vision": True, "context_window": 1000000},
            "defaults": {},
        }
    )
    assert outcome == {
        "baseline": {"supports_vision": True},
        "bannerHidden": False,
        "ctx": 1000000,
        "name": "claude-unknown-9",
        "rendered": 1,
    }


def test_the_preview_sends_the_template_fields_only_on_compat_lanes() -> None:
    assert _preview("anthropic", {})["capabilities"] == {}
    assert _preview("anthropic-compatible", {})["capabilities"] == {
        "thinking_mode": "manual",
        "thinking_param": "enable_thinking",
        "effort_param": "reasoning_effort",
    }


def test_the_preview_carries_an_unchecked_effort_tile() -> None:
    assert _preview("anthropic", {"supports_effort": False})["capabilities"] == {
        "supports_effort": False
    }


def test_the_preview_sends_the_api_surface_only_on_openai_compatible() -> None:
    """The save writes the API surface only for openai-compatible, so a value a
    provider switch leaves in the hidden field must not steer the preview."""
    assert _preview("google", {}, "responses")["api_surface"] == ""
    assert _preview("openai-compatible", {}, "responses")["api_surface"] == "responses"


@pytest.mark.parametrize(
    "provider", ["anthropic", "openai", "openai-compatible", "anthropic-compatible"]
)
@pytest.mark.parametrize(
    "stored",
    [
        _TEMPLATE_FIELDS,
        {"thinking_mode": "budget"},  # a mode the dropdown cannot show
        {"thinking_param": "thinking"},  # a param with no mode
    ],
)
def test_an_unrelated_edit_save_keeps_the_stored_template_fields(
    provider: str, stored: dict[str, str]
) -> None:
    assert _edit_and_save(provider, stored)["saved"] == stored


def test_the_template_fields_move_into_the_form_only_on_compat_lanes() -> None:
    native = _edit_and_save("anthropic", _TEMPLATE_FIELDS)
    assert native["fields"] == {"thinking_mode": "", "thinking_param": "", "effort_param": ""}
    assert native["raw"] == _TEMPLATE_FIELDS
    compat = _edit_and_save("anthropic-compatible", _TEMPLATE_FIELDS)
    assert compat["fields"] == _TEMPLATE_FIELDS
    assert compat["raw"] == {}


def test_a_value_left_in_a_hidden_template_field_is_not_saved() -> None:
    typed = {"model-" + key.replace("_", "-"): value for key, value in _TEMPLATE_FIELDS.items()}
    assert _edit_and_save("anthropic", {}, typed)["saved"] == {}
    assert _edit_and_save("anthropic-compatible", {}, typed)["saved"] == _TEMPLATE_FIELDS


def test_the_form_paths_reach_the_template_fields_only_through_the_helpers() -> None:
    """The round trips above run the helpers; this ties them to the three paths.
    Before #1366 the preview read Effort param on every provider and sent no tiles,
    while the save path gated Effort param and sent the tiles."""
    source = _ADMIN.read_text()
    for signature, helper in (
        ("function showEditModelModal(definitionId) {", "_liftModelTemplateFields("),
        ("function submitCreateModel() {", "_overlayModelFormCaps("),
        ("function _refreshModelEffortLadder() {", "_overlayModelFormCaps("),
    ):
        body = extract_braced(source, signature)
        assert helper in body, signature
        for field in ("model-thinking-mode", "model-thinking-param", "model-effort-param"):
            assert f'"{field}"' not in body, (signature, field)
