# Images returned by MCP tools

MCP `tools/call` results can contain text and `ImageContent` blocks together,
or images alone. Turnstone preserves supported images as attachments and sends
their pixels to the model on the next inference request. A textual placeholder
such as `[image/jpeg data, … bytes]` is not an image input.

## Model and transport behavior

- OpenAI Responses: native `function_call_output.output` content parts, including
  `input_image`. See the [official function calling documentation](https://developers.openai.com/api/docs/guides/function-calling).
- Anthropic: native image blocks inside the corresponding `tool_result`.
- Chat Completions (including OpenAI-compatible local servers and Google's
  compatibility endpoint): text stays in the tool result; images follow the
  **complete** tool-result batch in a user-role image envelope labelled with
  their originating tool-call IDs and their status as untrusted tool output.
  This is a wire-only adaptation: it does not create a user turn in history.

The selected model still needs vision support. Existing model-capability and
perception-fallback settings apply; this change does not grant vision to a
text-only model, configure a fallback, or silently select another provider.
Local deployments must configure the appropriate vision-capable model/router.

PNG, JPEG, GIF and WebP use the same MIME/magic-byte policy as uploaded images.
The combined decoded images in one MCP result are limited to 4 MiB (the shared
image attachment cap). Invalid, unsupported or over-budget images produce an
explicit omission notice; valid text and other images remain. Images are not
fetched from arbitrary resource links. Audio and embedded resources retain
their existing handling. The built-in text-only MCP web-search adapter keeps
text and an omission notice; use the MCP tool directly for image results.

## Viewing and persistence

Expand the tool result in the conversation, then choose **View image** (or
**View N images**). Images stay collapsed until requested. Clicking an image
opens its full-size stored version. This works in the interactive pane and
coordinator, both live and after reopening a conversation.

Image bytes are content-addressed attachments, committed with the tool result.
Only attachment IDs and display metadata travel in tool-result events and
public history JSON. The browser uses the existing authenticated, workstream-
scoped attachment endpoints, including the owning node's proxy prefix. It does
not render URLs supplied by the MCP server or embed base64 in the event ring.
An image whose storage commit is still pending can be retried by closing and
reopening its disclosure. Pruned or inaccessible attachments remain unavailable.

Text-only MCP tools, approval flows and tool-call IDs retain their existing
behavior. An MCP error result remains an error even when it includes images.
Historical placeholder-only responses cannot be repaired retroactively: take
a new snapshot after installing the fix.

## Validation

The regression tests cover SDK decoding (static and pooled dispatch), invalid
images and aggregate byte limits, parallel provider tool-result ordering,
native Responses/Anthropic images, error receipts, persistence/reload,
metadata-only accepted events, and lazy scoped browser image retrieval:

```sh
pytest tests/test_mcp_image_results.py tests/test_tool_images_js.py
```

For a deployment smoke test, use a vision-capable model, request a fresh MCP
snapshot, and ask about something visible only in the image. Inspect the next
provider request for a native image content part, not a base64 string within
text. Check **View image**, reload the conversation, and check it again.
