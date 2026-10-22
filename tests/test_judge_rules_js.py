"""Judge configuration controls display warnings on persisted edits."""

import json
from pathlib import Path

import pytest

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip
_SOURCE = Path(__file__).resolve().parents[1] / "turnstone/console/static/governance.js"


@pytest.mark.parametrize(
    ("name", "arguments", "success"),
    [
        ("toggleHeuristicRule", ["rule", True], "Rule enabled"),
        ("deleteHeuristicRule", ["rule"], "Rule deleted"),
        ("resetHeuristicRule", ["rule"], "Rule reset"),
        ("disableBuiltinHeuristicRule", ["rule"], "Built-in rule disabled"),
        ("_submitHRShelf", [], "Rule created"),
        ("toggleOGPattern", ["pattern", True], "Pattern enabled"),
        ("deleteOGPattern", ["pattern"], "Pattern deleted"),
        ("resetOGPattern", ["pattern"], "Pattern reset"),
        ("disableBuiltinOGPattern", ["pattern"], "Built-in pattern disabled"),
        ("_submitOGPShelf", [], "Pattern created"),
    ],
)
@pytest.mark.parametrize(
    "warning",
    ["", "Change saved, but rule configuration could not be loaded."],
    ids=["saved", "warning"],
)
def test_saved_edit_displays_its_warning(name, arguments, success, warning):
    script = _SOURCE.read_text()
    signature = next(line for line in script.splitlines() if line.startswith(f"function {name}("))
    source = extract_braced(script, signature)
    harness = """
import assert from 'node:assert/strict';
const toasts = [];
const showToast = text => toasts.push(text);
let reloads = 0;
const loadJudgeHeuristicRules = () => { reloads++; };
const loadJudgeOGPatterns = () => { reloads++; };
const hideCreateHRModal = () => {};
const hideCreateOGPModal = () => {};
const showConfirmModal = (_title, _body, _action, confirm) => confirm();
const _judgeHeuristicRules = [{name: 'rule', rule_id: 'rule'}];
const _judgeOGPatterns = [{name: 'pattern', pattern_id: 'pattern'}];
globalThis.window = {TurnstoneHatch: {setBusy: () => {}}};
globalThis.document = {getElementById: id => ({
  value: id.endsWith('-id') ? '' : 'test',
  checked: false,
  classList: {remove: () => {}, add: () => {}},
})};
const authFetch = async () => ({ok: true, json: async () => response});
"""
    harness += "const response = " + json.dumps({"warning": warning} if warning else {}) + ";"
    harness += source + name + "(..." + json.dumps(arguments) + ");"
    harness += """
for (let i = 0; i < 3; i++) await new Promise(setImmediate);
assert.equal(reloads, 1);
assert.equal(toasts.length, 1);
"""
    if warning:
        harness += "assert.equal(toasts[0], " + json.dumps(warning) + ");"
    else:
        harness += "assert.ok(toasts[0].startsWith(" + json.dumps(success) + "));"
    result = run_node_source(harness)
    assert result.returncode == 0, result.stderr
