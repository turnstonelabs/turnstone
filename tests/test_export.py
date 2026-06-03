"""Unit tests for the workstream export serializer (issue #613).

Drives through a REAL storage backend (the ``backend`` fixture is a
SQLite ``StorageBackend``): seed workstreams / messages / attachments,
call :func:`export_workstream`, parse the returned bytes, and assert
structural facts.  No hand-built message dicts are injected straight
into the serializer as the sole gate — the pipeline order (attach
reasoning → sanitize) is what these tests guard.
"""

from __future__ import annotations

import io
import json
import zipfile

from turnstone.core.export import (
    WorkstreamNotFoundError,
    _attach_reasoning_content,
    _build_openai_json,
    export_workstream,
)

USER = "u1"


def _assistants(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("role") == "assistant"]


def _parse_messages(data: bytes) -> list[dict]:
    return json.loads(data)["messages"]


def _seed_interactive_turn(backend, ws_id: str) -> None:
    """user + assistant(tool_call) + tool + assistant."""
    tc = [
        {
            "id": "call_a1",
            "type": "function",
            "function": {"name": "run", "arguments": "{}"},
        }
    ]
    backend.register_workstream(ws_id, user_id=USER, title="T", kind="interactive")
    backend.save_message(ws_id, "user", "go")
    backend.save_message(ws_id, "assistant", "working", tool_calls=json.dumps(tc))
    backend.save_message(ws_id, "tool", "ran ok", tool_name="run", tool_call_id="call_a1")
    backend.save_message(ws_id, "assistant", "done")


def test_openai_json_envelope_shape(backend):
    _seed_interactive_turn(backend, "ws1")
    result = export_workstream(backend, "ws1")

    assert result.content_type == "application/json"
    assert result.filename == "ws1.json"
    top_keys = sorted(json.loads(result.data).keys())
    assert top_keys == ["messages"]


def test_reasoning_content_present_thinking(backend):
    pc = [{"type": "thinking", "thinking": "R1", "signature": "sig"}]
    backend.register_workstream("ws1", user_id=USER, kind="interactive")
    backend.save_message("ws1", "user", "go")
    backend.save_message("ws1", "assistant", "ok", provider_data=json.dumps(pc))

    messages = _parse_messages(export_workstream(backend, "ws1").data)
    reasoning = [m.get("reasoning_content") for m in _assistants(messages)]
    assert reasoning == ["R1"]


def test_reasoning_content_present_reasoning_text(backend):
    pc = [{"type": "reasoning_text", "text": "R2", "source": "synth"}]
    backend.register_workstream("ws1", user_id=USER, kind="interactive")
    backend.save_message("ws1", "user", "go")
    backend.save_message("ws1", "assistant", "ok", provider_data=json.dumps(pc))

    messages = _parse_messages(export_workstream(backend, "ws1").data)
    reasoning = [m.get("reasoning_content") for m in _assistants(messages)]
    assert reasoning == ["R2"]


def test_reasoning_content_present_responses(backend):
    pc = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "R3"}]}]
    backend.register_workstream("ws1", user_id=USER, kind="interactive")
    backend.save_message("ws1", "user", "go")
    backend.save_message("ws1", "assistant", "ok", provider_data=json.dumps(pc))

    messages = _parse_messages(export_workstream(backend, "ws1").data)
    reasoning_text = _assistants(messages)[0].get("reasoning_content")
    assert reasoning_text is not None
    assert "R3" in reasoning_text


def test_no_underscore_keys_leak(backend):
    pc = [{"type": "thinking", "thinking": "R1", "signature": "sig"}]
    _seed_interactive_turn(backend, "ws1")
    backend.save_message("ws1", "assistant", "more", provider_data=json.dumps(pc))

    messages = _parse_messages(export_workstream(backend, "ws1").data)
    leaked = sorted({k for m in messages for k in m if isinstance(k, str) and k.startswith("_")})
    assert leaked == []


def test_image_url_kept_document_inlined(backend):
    backend.register_workstream("ws1", user_id=USER, kind="interactive")
    msg_id = backend.save_message("ws1", "user", "see attached")
    backend.save_attachment("att_img", "ws1", USER, "pic.png", "image/png", 4, "image", b"\x89PNG")
    backend.save_attachment("att_doc", "ws1", USER, "notes.txt", "text/plain", 5, "text", b"hello")
    backend.set_message_attachments("ws1", msg_id, ["att_img", "att_doc"])
    backend.save_message("ws1", "assistant", "got it")

    messages = _parse_messages(export_workstream(backend, "ws1").data)
    user_msg = next(m for m in messages if m.get("role") == "user")
    parts = user_msg["content"]
    part_types = [p.get("type") for p in parts]
    document_texts = [
        p.get("text", "")
        for p in parts
        if p.get("type") == "text" and "<document name=" in p.get("text", "")
    ]

    assert "image_url" in part_types
    assert document_texts != []


def test_assistant_without_reasoning_has_no_reasoning_content(backend):
    backend.register_workstream("ws1", user_id=USER, kind="interactive")
    backend.save_message("ws1", "user", "go")
    backend.save_message("ws1", "assistant", "ok")

    messages = _parse_messages(export_workstream(backend, "ws1").data)
    assistant = _assistants(messages)[0]
    assert "reasoning_content" not in assistant


def test_coordinator_zip_parent_plus_children(backend):
    backend.register_workstream("coord", user_id=USER, title="C", kind="coordinator")
    backend.save_message("coord", "user", "coordinate")
    backend.save_message("coord", "assistant", "spawning")
    backend.register_workstream("c1", user_id=USER, kind="interactive", parent_ws_id="coord")
    backend.register_workstream("c2", user_id=USER, kind="interactive", parent_ws_id="coord")
    for child in ("c1", "c2"):
        backend.save_message(child, "user", "do x")
        backend.save_message(child, "assistant", "x done")

    result = export_workstream(backend, "coord", children=True)
    assert result.content_type == "application/zip"
    assert result.filename == "coord.zip"

    zf = zipfile.ZipFile(io.BytesIO(result.data))
    names = sorted(zf.namelist())
    expected = sorted(["coord.json", "children/c1.json", "children/c2.json"])
    assert names == expected

    top_keys = [sorted(json.loads(zf.read(name)).keys()) for name in names]
    assert top_keys == [["messages"], ["messages"], ["messages"]]


def test_coordinator_default_parent_only(backend):
    backend.register_workstream("coord", user_id=USER, title="C", kind="coordinator")
    backend.save_message("coord", "user", "coordinate")
    backend.save_message("coord", "assistant", "done")
    backend.register_workstream("c1", user_id=USER, kind="interactive", parent_ws_id="coord")

    result = export_workstream(backend, "coord", children=False)
    assert result.content_type == "application/json"
    assert result.filename == "coord.json"


def test_export_unknown_ws_raises(backend):
    try:
        export_workstream(backend, "does-not-exist")
    except WorkstreamNotFoundError as exc:
        assert "does-not-exist" in str(exc)
    else:
        raise AssertionError("expected WorkstreamNotFoundError")


def test_attach_reasoning_runs_before_sanitize(backend):
    pc = [{"type": "thinking", "thinking": "R1", "signature": "sig"}]
    backend.register_workstream("ws1", user_id=USER, kind="interactive")
    backend.save_message("ws1", "user", "go")
    backend.save_message("ws1", "assistant", "ok", provider_data=json.dumps(pc))

    attached = _attach_reasoning_content(backend.load_messages("ws1", repair=True))
    assistant = _assistants(attached)[0]
    # Pre-sanitize: reasoning stamped AND the raw provider lane still present.
    assert assistant.get("reasoning_content") == "R1"
    assert "_provider_content" in assistant

    # Full pipeline output: provider lane is gone, reasoning survives.
    messages = _parse_messages(_build_openai_json(backend, "ws1"))
    leaked = [k for m in messages for k in m if isinstance(k, str) and k.startswith("_")]
    assert leaked == []
    assert _assistants(messages)[0].get("reasoning_content") == "R1"
