"""MCP pixels survive decoding, provider lowering, and tool-row persistence."""

from __future__ import annotations

import base64
import copy
import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import mcp.types as mcp_types
import pytest
from PIL import Image

from tests.conftest import _seed_static_state
from turnstone.core.mcp_client import MCPClientManager, _decode_tool_result
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.providers._google import GoogleProvider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider


def _image_bytes(mode="RGB", color="red", fmt="PNG"):
    buffer = BytesIO()
    options = {"lossless": True, "exact": True} if fmt == "WEBP" else {}
    Image.new(mode, (1, 1), color).save(buffer, format=fmt, **options)
    return buffer.getvalue()


PNG_1x1 = _image_bytes()
B64 = base64.b64encode(PNG_1x1).decode()
URI = f"data:image/png;base64,{B64}"


def _result(*, error=False, image_only=False):
    image = mcp_types.ImageContent(type="image", mime_type="image/png", data=B64)
    content = (
        [image] if image_only else [mcp_types.TextContent(type="text", text="snapshot"), image]
    )
    return mcp_types.CallToolResult(content=content, is_error=error)


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
    assert len(content) == 2
    assert content[0]["text"] == ("[1 image attached]" if image_only else "snapshot")


def test_decode_normalizes_interleaving_to_text_then_images():
    second = base64.b64encode(_image_bytes(color="blue")).decode()
    result = mcp_types.CallToolResult(
        content=[
            mcp_types.ImageContent(mime_type="image/png", data=B64),
            mcp_types.TextContent(text="first caption"),
            mcp_types.ImageContent(mime_type="image/png", data=second),
            mcp_types.TextContent(text="second caption"),
        ]
    )
    decoded = _decode_tool_result(result)
    assert decoded == [
        {"type": "text", "text": "first caption\nsecond caption"},
        {"type": "image_url", "image_url": {"url": URI}},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{second}"}},
    ]


@pytest.mark.parametrize("error", [False, True])
def test_text_only_compatibility(error):
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=t) for t in ("one", "two")],
        is_error=error,
    )
    assert _decode_tool_result(result) == ("Error: " if error else "") + "one\ntwo"
    assert _decode_tool_result(mcp_types.CallToolResult(content=[])) == "(no output)"


@pytest.mark.parametrize(
    "mime,data",
    [
        ("image/svg+xml", B64),
        ("image/gif", base64.b64encode(_image_bytes(fmt="GIF")).decode()),
        ("image/png", "%%%"),
        ("image/jpeg", B64),
        ("image/png", ""),
        (
            "image/png",
            base64.b64encode(bytes.fromhex("89504e470d0a1a0a") + b"broken-png-data").decode(),
        ),
    ],
)
def test_invalid_images_are_explicitly_omitted(mime, data):
    result = mcp_types.CallToolResult(
        content=[mcp_types.ImageContent(type="image", mime_type=mime, data=data)]
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
    assert "byte limit" in decoded[0]["text"]
    assert decoded[1]["type"] == "image_url"


def test_pixel_limit_prevents_decode_before_storage(monkeypatch):
    monkeypatch.setattr("turnstone.core.images._MAX_IMAGE_PIXELS", 0)
    with patch("PIL.PngImagePlugin.PngImageFile.load") as load:
        decoded = _decode_tool_result(_result(image_only=True))
    assert isinstance(decoded, str) and "MCP image omitted" in decoded
    load.assert_not_called()


def test_corrupt_compressed_pixels_are_omitted_even_with_valid_png_container():
    import struct
    import zlib

    raw = bytearray(PNG_1x1[:8])
    offset = 8
    while offset < len(PNG_1x1):
        size = int.from_bytes(PNG_1x1[offset : offset + 4], "big")
        kind = PNG_1x1[offset + 4 : offset + 8]
        data = PNG_1x1[offset + 8 : offset + 8 + size]
        if kind == b"IDAT":
            data = b"invalid compressed pixels"
        raw.extend(struct.pack(">I", len(data)) + kind + data)
        raw.extend(struct.pack(">I", zlib.crc32(kind + data)))
        offset += size + 12
    with Image.open(BytesIO(raw)) as image:
        image.verify()  # Container checks alone accept this payload.
    result = mcp_types.CallToolResult(
        content=[mcp_types.ImageContent(mime_type="image/png", data=base64.b64encode(raw).decode())]
    )
    decoded = _decode_tool_result(result)
    assert isinstance(decoded, str) and "MCP image omitted" in decoded


@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("kind", ["text", "image", "structured"])
def test_session_status_comes_from_sdk_flag_not_server_words(tmp_db, error, kind):
    from tests._session_helpers import make_session

    text = "Error: this is literal server output"
    result = mcp_types.CallToolResult(
        content=[] if kind == "structured" else [mcp_types.TextContent(text=text)],
        structured_content={"message": text} if kind == "structured" else None,
        is_error=error,
    )
    if kind == "image":
        result.content.extend(_result(image_only=True).content)
    session = make_session(ui=MagicMock())
    session._mcp_client = MagicMock()
    session._mcp_client.call_tool_sync.return_value = result
    with patch.object(session, "_report_tool_result", wraps=session._report_tool_result) as report:
        session._exec_mcp_tool(
            {
                "call_id": "c",
                "mcp_func_name": "mcp__lab__camera",
                "mcp_args": {},
                "_principal_id": "",
            }
        )
    assert report.call_args.kwargs["is_error"] is error
    assert session._tool_error_flags.get("c", False) is error


@pytest.mark.parametrize(("fmt", "mime"), [("PNG", "image/png"), ("WEBP", "image/webp")])
@pytest.mark.parametrize(("mode", "color"), [("RGBA", (255, 0, 0, 0)), ("LA", (128, 0))])
def test_transparency_is_flattened_before_attachment_hashing(fmt, mime, mode, color):
    from turnstone.core.session import ChatSession

    raw = _image_bytes(mode, color, fmt)
    with Image.open(BytesIO(raw)) as source:
        assert "A" in source.getbands()
    content = _decode_tool_result(
        mcp_types.CallToolResult(
            content=[
                mcp_types.ImageContent(
                    mime_type=mime,
                    data=base64.b64encode(raw).decode(),
                )
            ]
        )
    )
    _, attachments = ChatSession._tool_content_by_reference(content, "camera")
    assert len(attachments) == 1
    assert attachments[0].content != raw
    assert attachments[0].mime_type == mime
    with Image.open(BytesIO(attachments[0].content)) as image:
        assert image.format == fmt
        assert "A" not in image.getbands()
        assert image.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_jpeg_scan_limit_uses_the_upload_policy():
    from turnstone.core.images import MAX_JPEG_SCANS

    raw = bytes.fromhex("ffd8ff") + bytes.fromhex("ffda") * (MAX_JPEG_SCANS + 1)
    content = _decode_tool_result(
        mcp_types.CallToolResult(
            content=[
                mcp_types.ImageContent(
                    mime_type="image/jpeg",
                    data=base64.b64encode(raw).decode(),
                )
            ]
        )
    )
    assert "MCP image omitted" in content
    assert "passes" in content


def test_mcp_search_text_contract_does_not_stringify_pixels():
    from turnstone.core.web_search import MCPSearchClient

    manager = MagicMock()
    manager.call_tool_sync.return_value = _result()
    with patch(
        "turnstone.core.mcp_client.validate_image", side_effect=AssertionError("pixels decoded")
    ):
        result = MCPSearchClient(manager, "mcp__test__search").search("q")
    assert "snapshot" in result and "Image omitted" in result
    assert B64 not in result


def test_static_dispatch_preserves_the_sdk_result():
    manager = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
    session = MagicMock()
    _seed_static_state(manager, "test", session=session)
    manager._loop = MagicMock()
    manager._tool_map["mcp__test__camera"] = ("test", "camera")
    future = MagicMock()
    future.result.return_value = _result()
    with patch.object(manager, "_submit_root", return_value=future):
        assert manager.call_tool_sync("mcp__test__camera", {}) is future.result.return_value


def test_pool_sync_does_not_parse_images_as_consent_json():
    manager = MCPClientManager({})
    manager._loop = MagicMock()
    content = _result()
    with (
        patch.object(manager, "_run_pool_dispatch_attempt", return_value=_result()),
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
    import httpx2
    from openai import OpenAI

    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        # Stop at the transport boundary: no network or inference is needed.
        raise httpx2.ConnectError("captured request", request=request)

    messages = _messages()
    for msg in messages[2:]:
        msg["content"] = [{"type": "image", "attachment_id": "stored-image"}]
    provider = OpenAIResponsesProvider() if responses else OpenAIChatCompletionsProvider()
    with OpenAI(
        api_key="unit-test-only",
        base_url="https://unit.test/v1",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(capture), trust_env=False),
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
    session._mcp_client.call_tool_sync.return_value = _result(error=True)
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
@pytest.mark.parametrize("error", [False, True])
def test_send_persists_tool_pixels_and_replays_them(tmp_db, image_only, error):
    from tests._session_helpers import make_registered_session, make_result
    from tests.test_sse_reconnect_replay import _make_ui
    from turnstone.core import memory
    from turnstone.core.history_decoration import project_history_messages
    from turnstone.core.storage import get_storage
    from turnstone.core.trajectory import dicts_from_turns

    backend = get_storage()
    backend.create_user("u", "u", "User", "hash")
    ui = _make_ui()
    session = make_registered_session(ui=ui, user_id="u")
    session._title_generated = True
    tool_call = _messages()[1]["tool_calls"][0]
    session._mcp_client = MagicMock()
    result = _result(image_only=True, error=error)
    if not image_only:
        result.content += [
            mcp_types.TextContent(text="caption one"),
            *result.content,
            mcp_types.TextContent(text=""),
            mcp_types.TextContent(text="caption two"),
        ]
    session._mcp_client.call_tool_sync.return_value = result

    def execute(*args, **kwargs):
        return [
            session._exec_mcp_tool(
                {
                    "call_id": tool_call["id"],
                    "mcp_func_name": "mcp__lab__camera",
                    "mcp_args": {},
                    "_principal_id": "u",
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
    restored = dicts_from_turns(
        get_storage().load_message_turns(session._ws_id, checkpointed=False)
    )
    canonical = next(m for m in restored if m["role"] == "tool")
    assert any(p.get("attachment_id") == attachment_id for p in canonical["content"])
    assert B64 not in json.dumps(restored)
    live = next(turn for turn in session.messages if turn.role.value == "tool")
    loaded = next(
        turn for turn in backend.load_message_turns(session._ws_id) if turn.role.value == "tool"
    )
    assert live.content == loaded.content
    assert live.is_error is loaded.is_error is error
    assert live.effect_status is loaded.effect_status
    backend.register_workstream("fork", user_id="u", state="creating", kind="interactive")
    snapshot = backend.clone_workstream(session._ws_id, "fork", principal_id="u")
    forked = next(turn for turn in snapshot.turns if turn.role.value == "tool")
    assert forked.content == live.content
    assert forked.is_error is error
    assert forked.effect_status is live.effect_status
    assert forked.meta.extra["acting_principal"] == "u"
    assert backend.get_attachment(attachment_id)["refcount"] == (2 if image_only else 4)
    # Public history resolves metadata but never exposes inline image bytes.
    history = project_history_messages(get_storage().load_messages(session._ws_id))
    public = next(m for m in history if m["role"] == "tool")
    assert public["attachments"] == metadata
    expected_text = "" if image_only else "caption one\n\ncaption two"
    if error:
        expected_text = "Error: MCP tool returned an error." + (
            "\n" + expected_text if expected_text else ""
        )
    elif image_only:
        expected_text = "[1 image attached]"
    assert public["content"] == expected_text
    assert B64 not in json.dumps(history)
    # The next inference can materialize the same stored bytes after reload.
    resolved = session._resolve_attachments([attachment_id])
    assert URI in json.dumps(resolved)


def test_repeated_pixels_share_one_blob_across_retry_fork_and_deletion(storage_backend):
    from turnstone.core.session import ChatSession, _attachment_writes
    from turnstone.core.trajectory import dicts_from_turns

    backend = storage_backend
    backend.create_user("u", "u", "User", "hash")
    for ws_id in ("source", "fork"):
        backend.register_workstream(
            ws_id,
            user_id="u",
            state="idle" if ws_id == "source" else "creating",
            kind="interactive",
        )
    messages = _messages()
    backend.save_message("source", "user", "compare cameras")
    backend.save_message(
        "source", "assistant", "", tool_calls=json.dumps(messages[1]["tool_calls"])
    )
    attachment_ids = []
    for message in messages[2:]:
        content, images = ChatSession._tool_content_by_reference(message["content"], "camera")
        attachment_ids.append(images[0].attachment_id)
        assert content[-1] == {"type": "image", "attachment_id": images[0].attachment_id}
        kwargs = {"commit_key": message["tool_call_id"]}
        args = ("source", "snapshot", "camera", message["tool_call_id"], _attachment_writes(images))
        row_id = backend.save_tool_message_with_attachments(*args, **kwargs)
        assert backend.save_tool_message_with_attachments(*args, **kwargs) == row_id
    backend.save_message("source", "assistant", "comparison")

    assert attachment_ids[0] == attachment_ids[1]
    attachment_id = attachment_ids[0]
    blobs = backend.get_attachments(attachment_ids)
    assert len(blobs) == 1
    assert blobs[0]["content"] == PNG_1x1
    assert blobs[0]["refcount"] == 2

    snapshot = backend.clone_workstream("source", "fork", principal_id="u")
    restored = dicts_from_turns(snapshot.turns)
    assert B64 not in json.dumps(restored)
    tools = [m for m in restored if m["role"] == "tool"]
    assert len(tools) == 2
    assert all(m["_attachments_meta"][0]["attachment_id"] == attachment_id for m in tools)
    assert backend.get_attachment(attachment_id)["refcount"] == 4
    assert backend.delete_workstream("source")
    assert backend.get_attachment(attachment_id)["refcount"] == 2
    assert backend.delete_workstream("fork")
    assert backend.get_attachment(attachment_id) is None
