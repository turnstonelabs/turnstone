"""SDK model contracts for the strings and prompt messages exposed to the agent."""

import mcp.types as mcp_types
import pytest

from turnstone.core.mcp_client import (
    _decode_prompt_result,
    _decode_resource_result,
    _decode_tool_result,
)


@pytest.mark.parametrize(
    ("texts", "is_error", "expected"),
    [
        ([], False, "(no output)"),
        ([], True, "Error: (no output)"),
        ([""], False, ""),
        ([""], True, "Error: "),
        (["first", "second"], False, "first\nsecond"),
        (["first", "second"], True, "Error: first\nsecond"),
    ],
)
def test_tool_text_and_error_results(texts: list[str], is_error: bool, expected: str) -> None:
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text) for text in texts],
        isError=is_error,
    )
    assert _decode_tool_result(result) == expected


def test_tool_mixed_content_retains_order_and_mime_types() -> None:
    result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(type="text", text="before"),
            mcp_types.ImageContent(type="image", data="aGVsbG8=", mimeType="image/png"),
            mcp_types.TextContent(type="text", text="between"),
            mcp_types.AudioContent(type="audio", data="AQID", mimeType="audio/wav"),
        ]
    )
    assert _decode_tool_result(result) == (
        "before\n[image/png data, 8 bytes]\nbetween\n[audio/wav data, 4 bytes]"
    )


@pytest.mark.parametrize(
    "content",
    [
        mcp_types.ResourceLink(type="resource_link", name="readme", uri="file:///readme"),
        mcp_types.EmbeddedResource(
            type="resource",
            resource=mcp_types.TextResourceContents(uri="file:///readme", text="readme"),
        ),
        mcp_types.EmbeddedResource(
            type="resource",
            resource=mcp_types.BlobResourceContents(uri="file:///image", blob="AQID"),
        ),
    ],
)
def test_tool_resource_parts_keep_string_rendering(content: mcp_types.ContentBlock) -> None:
    assert _decode_tool_result(mcp_types.CallToolResult(content=[content])) == str(content)


def test_resource_text_and_blob_contents() -> None:
    result = mcp_types.ReadResourceResult(
        contents=[
            mcp_types.TextResourceContents(uri="file:///readme", text="readme"),
            mcp_types.BlobResourceContents(uri="file:///image", blob="AQID"),
        ]
    )
    assert _decode_resource_result(result) == "readme\nAQID"


def test_empty_resource() -> None:
    assert _decode_resource_result(mcp_types.ReadResourceResult(contents=[])) == "(empty resource)"


def test_prompt_roles_and_content() -> None:
    image = mcp_types.ImageContent(type="image", data="AQID", mimeType="image/png")
    result = mcp_types.GetPromptResult(
        messages=[
            mcp_types.PromptMessage(
                role="user", content=mcp_types.TextContent(type="text", text="Describe this")
            ),
            mcp_types.PromptMessage(role="user", content=image),
            mcp_types.PromptMessage(
                role="assistant", content=mcp_types.TextContent(type="text", text="An image")
            ),
        ]
    )
    assert _decode_prompt_result(result) == [
        {"role": "user", "content": "Describe this"},
        {"role": "user", "content": str(image)},
        {"role": "assistant", "content": "An image"},
    ]


def test_empty_prompt() -> None:
    assert _decode_prompt_result(mcp_types.GetPromptResult(messages=[])) == []
