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
