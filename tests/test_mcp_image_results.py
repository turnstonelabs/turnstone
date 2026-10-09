"""MCP pixels survive decoding, provider lowering, and tool-row persistence."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types
import pytest

from tests.conftest import _seed_static_state
from tests.test_mcp_client import _dispatch_stub
from tests.test_session_attachments import PNG_1x1
from turnstone.core.mcp_client import MCPClientManager, _decode_tool_result
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.providers._google import GoogleProvider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

B64 = base64.b64encode(PNG_1x1).decode()
URI = f"data:image/png;base64,{B64}"


def _result(*, error=False, image_only=False):
    image = mcp_types.ImageContent(type="image", mimeType="image/png", data=B64)
    content = (
        [image] if image_only else [mcp_types.TextContent(type="text", text="snapshot"), image]
    )
    return mcp_types.CallToolResult(content=content, isError=error)


def _messages(*, image_only=False):
    calls = [
        {"id": cid, "type": "function", "function": {"name": "camera", "arguments": "{}"}}
        for cid in ("camera-a", "camera-b")
    ]
    return [
        {"role": "user", "content": "compare cameras"},
        {"role": "assistant", "content": "", "tool_calls": calls},
        {
            "role": "tool",
            "tool_call_id": "camera-a",
            "content": _decode_tool_result(_result(image_only=image_only)),
        },
        {"role": "tool", "tool_call_id": "camera-b", "content": _decode_tool_result(_result())},
    ]


@pytest.mark.parametrize("image_only", [False, True])
def test_decode_preserves_image_and_order(image_only):
    content = _decode_tool_result(_result(image_only=image_only))
    assert isinstance(content, list)
    assert content[-1] == {"type": "image_url", "image_url": {"url": URI}}
    assert base64.b64decode(content[-1]["image_url"]["url"].split(",")[1]) == PNG_1x1
    assert len(content) == (1 if image_only else 2)


@pytest.mark.parametrize("error", [False, True])
def test_text_only_compatibility(error):
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=t) for t in ("one", "two")],
        isError=error,
    )
    assert _decode_tool_result(result) == ("Error: " if error else "") + "one\ntwo"
    assert _decode_tool_result(mcp_types.CallToolResult(content=[])) == "(no output)"


@pytest.mark.parametrize(
    "mime,data",
    [("image/svg+xml", B64), ("image/png", "%%%"), ("image/jpeg", B64), ("image/png", "")],
)
def test_invalid_images_are_explicitly_omitted(mime, data):
    result = mcp_types.CallToolResult(
        content=[mcp_types.ImageContent(type="image", mimeType=mime, data=data)]
    )
    decoded = _decode_tool_result(result)
    assert isinstance(decoded, str)
    assert "MCP image omitted" in decoded
    assert B64 not in decoded


def test_aggregate_image_budget(monkeypatch):
    monkeypatch.setattr("turnstone.core.mcp_client.IMAGE_SIZE_CAP", len(PNG_1x1))
    result = _result(image_only=True)
    result.content += result.content
    decoded = _decode_tool_result(result)
    assert isinstance(decoded, list)
    assert decoded[0]["type"] == "image_url"
    assert "byte limit" in decoded[1]["text"]


def test_mcp_search_text_contract_does_not_stringify_pixels():
    from turnstone.core.web_search import MCPSearchClient

    manager = MagicMock()
    manager.call_tool_sync.return_value = _decode_tool_result(_result())
    result = MCPSearchClient(manager, "mcp__test__search").search("q")
    assert "snapshot" in result and "Image omitted" in result
    assert B64 not in result


def test_static_dispatch_returns_multipart():
    manager = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
    session = MagicMock()
    _seed_static_state(manager, "test", session=session)
    manager._loop = MagicMock()
    manager._tool_map["mcp__test__camera"] = ("test", "camera")
    future = MagicMock()
    future.result.return_value = _result()
    with patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(future)):
        assert manager.call_tool_sync("mcp__test__camera", {})[-1]["image_url"]["url"] == URI


def test_pooled_dispatch_returns_multipart():
    manager = MCPClientManager({})
    with patch.object(
        manager, "_dispatch_pool_with_entry_call", new=AsyncMock(return_value=_result())
    ):
        decoded = asyncio.run(
            manager._dispatch_pool_with_entry(
                entry=MagicMock(),
                key=("u", "camera"),
                cfg={},
                access_token="test-token",
                original_name="camera",
                arguments={},
            )
        )
    assert decoded[-1]["image_url"]["url"] == URI


def test_pool_sync_does_not_parse_images_as_consent_json():
    manager = MCPClientManager({})
    manager._loop = MagicMock()
    content = _decode_tool_result(_result())
    with (
        patch.object(manager, "_run_pool_dispatch_attempt", return_value=content),
        patch.object(manager, "_clear_pending_consent_sync") as clear,
    ):
        result = manager._dispatch_pool_sync(
            user_id="u",
            server_name="camera",
            original_name="camera",
            arguments={},
            server_row={},
            timeout=5,
        )
    assert result == content
    clear.assert_called_once_with("u", "camera")


@pytest.mark.parametrize("provider", [OpenAIChatCompletionsProvider, GoogleProvider])
@pytest.mark.parametrize("image_only", [False, True])
def test_chat_images_follow_complete_parallel_tool_block(provider, image_only):
    messages = _messages(image_only=image_only)
    messages.append({"role": "assistant", "content": "comparison"})
    before = copy.deepcopy(messages)
    wire = provider()._prepare_messages(messages)
    assert messages == before
    assert [m["role"] for m in wire] == ["user", "assistant", "tool", "tool", "user", "assistant"]
    assert [m["tool_call_id"] for m in wire[2:4]] == ["camera-a", "camera-b"]
    assert all(p["type"] == "text" for m in wire[2:4] for p in m["content"])
    assert [p["image_url"]["url"] for p in wire[4]["content"] if p["type"] == "image_url"] == [
        URI,
        URI,
    ]
    labels = [p["text"] for p in wire[4]["content"] if p["type"] == "text"]
    assert "camera-a" in labels[0] and "camera-b" in labels[1]
    assert all("untrusted tool output" in text for text in labels)


def test_chat_flushes_at_end_and_text_only_stays_unchanged():
    messages = _messages()
    assert OpenAIChatCompletionsProvider()._prepare_messages(messages)[-1]["role"] == "user"
    for m in messages[2:]:
        m["content"] = "just text"
    assert OpenAIChatCompletionsProvider()._prepare_messages(messages) == messages


def test_responses_native_multimodal_function_output():
    _, items = OpenAIResponsesProvider._convert_messages(_messages())
    outputs = [i for i in items if i["type"] == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["camera-a", "camera-b"]
    for out in outputs:
        assert out["output"] == [
            {"type": "input_text", "text": "snapshot"},
            {"type": "input_image", "image_url": URI},
        ]


@pytest.mark.parametrize("responses", [False, True])
def test_serialized_sdk_request_contains_native_pixels(responses):
    import httpx
    from openai import OpenAI

    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        # Stop at the transport boundary: no network or inference is needed.
        raise RuntimeError("captured request")

    messages = _messages()
    for msg in messages[2:]:
        msg["content"] = [{"type": "image", "attachment_id": "stored-image"}]
    provider = OpenAIResponsesProvider() if responses else OpenAIChatCompletionsProvider()
    with OpenAI(
        api_key="unit-test-only",
        base_url="https://unit.test/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
    ) as client:
        # The SDK wraps the deliberately aborted transport in APIConnectionError.
        from openai import APIConnectionError

        with pytest.raises(APIConnectionError):
            list(
                provider.create_streaming(
                    client=client,
                    model="test-model",
                    messages=messages,
                    resolve_attachments=lambda ids: {
                        "stored-image": {"type": "image_url", "image_url": {"url": URI}}
                    },
                )
            )
    assert len(captured) == 1
    payload = captured[0]
    if responses:
        outputs = [p for p in payload["input"] if p.get("type") == "function_call_output"]
        assert [p["call_id"] for p in outputs] == ["camera-a", "camera-b"]
        assert all(p["output"] == [{"type": "input_image", "image_url": URI}] for p in outputs)
    else:
        assert [m["role"] for m in payload["messages"]] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "user",
        ]
        assert [
            p["image_url"]["url"]
            for p in payload["messages"][-1]["content"]
            if p["type"] == "image_url"
        ] == [URI, URI]


def test_anthropic_native_image_and_error():
    messages = _messages()
    messages[2]["content"] = _decode_tool_result(_result(error=True))
    messages[2]["is_error"] = True
    _, wire = AnthropicProvider()._convert_messages(messages)
    results = wire[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["camera-a", "camera-b"]
    assert results[0]["is_error"] is True
    assert results[0]["content"][-1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": B64},
    }


def test_session_receipt_contains_no_pixels_and_preserves_error(tmp_db):
    from tests._session_helpers import make_session

    session = make_session(ui=MagicMock())
    session._mcp_client = MagicMock()
    session._mcp_client.call_tool_sync.return_value = _decode_tool_result(_result(error=True))
    with patch.object(session, "_report_tool_result") as receipt:
        _, output = session._exec_mcp_tool(
            {
                "call_id": "c",
                "mcp_func_name": "mcp__lab__camera",
                "mcp_args": {},
                "_principal_id": "",
            }
        )
    assert output[-1]["image_url"]["url"] == URI
    assert "[image attached]" in receipt.call_args.args[2]
    assert B64 not in receipt.call_args.args[2]
    assert receipt.call_args.kwargs["is_error"] is True


def test_accepted_event_only_exposes_attachment_metadata():
    from tests.test_sse_reconnect_replay import _make_ui

    ui = _make_ui()
    with patch.object(ui, "_enqueue") as enqueue:
        ui.on_tool_turn_accepted(
            "c",
            "camera",
            "snapshot",
            attachments=[
                {
                    "attachment_id": "a" * 64,
                    "kind": "image",
                    "filename": "snapshot.png",
                    "mime_type": "image/png",
                    "data": B64,
                }
            ],
        )
    event = enqueue.call_args.args[0]
    assert event["attachments"][0]["attachment_id"] == "a" * 64
    assert B64 not in json.dumps(event)


@pytest.mark.parametrize("image_only", [False, True])
def test_send_persists_tool_pixels_and_replays_them(tmp_db, image_only):
    from tests._session_helpers import make_registered_session, make_result
    from tests.test_sse_reconnect_replay import _make_ui
    from turnstone.core import memory
    from turnstone.core.history_decoration import project_history_messages
    from turnstone.core.trajectory import dicts_from_turns

    ui = _make_ui()
    session = make_registered_session(ui=ui)
    session._title_generated = True
    tool_call = _messages()[1]["tool_calls"][0]
    session._mcp_client = MagicMock()
    session._mcp_client.call_tool_sync.return_value = _decode_tool_result(
        _result(image_only=image_only)
    )

    def execute(*args, **kwargs):
        return [
            session._exec_mcp_tool(
                {
                    "call_id": tool_call["id"],
                    "mcp_func_name": "mcp__lab__camera",
                    "mcp_args": {},
                    "_principal_id": "",
                }
            )
        ], None

    with (
        patch.object(
            session,
            "_stream_response",
            side_effect=[make_result("", tool_calls=[tool_call]), make_result("done")],
        ),
        patch.object(session, "_execute_tools", side_effect=execute),
        patch.object(session, "_full_messages", return_value=[]),
        patch.object(session, "_update_token_table"),
        patch.object(session, "_print_status_line"),
        patch.object(session, "_emit_state"),
        patch.object(ui, "on_tool_turn_accepted", wraps=ui.on_tool_turn_accepted) as accepted,
    ):
        session.send("look at the camera")
    metadata = accepted.call_args.kwargs["attachments"]
    attachment_id = metadata[0]["attachment_id"]
    assert memory.get_attachment(attachment_id)["content"] == PNG_1x1
    restored = dicts_from_turns(memory.load_message_turns(session._ws_id, checkpointed=False))
    canonical = next(m for m in restored if m["role"] == "tool")
    assert any(p.get("attachment_id") == attachment_id for p in canonical["content"])
    assert B64 not in json.dumps(restored)
    # Public history resolves metadata but never exposes inline image bytes.
    history = project_history_messages(memory.load_messages(session._ws_id))
    public = next(m for m in history if m["role"] == "tool")
    assert public["attachments"] == metadata
    assert public["content"] == ("" if image_only else "snapshot")
    assert B64 not in json.dumps(history)
    # The next inference can materialize the same stored bytes after reload.
    resolved = session._resolve_attachments([attachment_id])
    assert URI in json.dumps(resolved)
