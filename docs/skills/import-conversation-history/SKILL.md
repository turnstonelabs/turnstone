---
name: import-conversation-history
description: Use this skill when the user wants to import or migrate conversation history from another LLM chat or coding tool (e.g. ChatGPT, Claude.ai, Cursor, Copilot Chat, Aider, Gemini, a custom JSON export) into Turnstone. The skill teaches Turnstone's destination contracts — workstream identity, the OpenAI-shaped message rows, tool-call/result pairing, provider-fidelity blobs, attachments, and archive-vs-resumable choice — so the agent can map any source format onto them. Trigger phrases: "import my chats", "migrate this transcript into Turnstone", "bring my Claude.ai history over", "load this export as a workstream".
version: 1.0.0
---

# Importing Conversation History into Turnstone

## Overview

Source formats vary; the destination does not. Your job is to translate whatever the user hands you (JSON dump, ZIP export, scraped HTML, screenshot OCR, raw transcript) into Turnstone's internal shape: **one workstream row** plus an ordered sequence of **conversation rows** in OpenAI message format. This skill documents the destination so you can write a correct mapper for any source.

Two questions to settle with the user before writing anything:

1. **Archive or resumable?** An archive ("saved" workstream — `state="closed"`) is read-only history. A resumable workstream (`state="idle"`) lets the user continue the conversation; this only works cleanly when the source LLM matches a Turnstone-supported provider/model and tool definitions still resolve.
2. **One workstream per source thread, or merge?** Default to one-to-one unless the user explicitly asks to merge.

Default to **archive** when in doubt — resuming a foreign transcript with mismatched tool schemas or stale provider signatures will fail at the next turn.

## Turnstone Data Model (the destination)

Two tables carry the conversation:

### `workstreams` (one row per imported thread)

| Column | Required | Notes |
|---|---|---|
| `ws_id` | yes | 32-char lowercase hex. Auto-generate with `secrets.token_hex(16)` if you don't already have one. **First 4 hex chars are the routing bucket** — see "Identity & Routing" below. |
| `name` | yes | Short title. Pull from source thread title; fall back to first ~60 chars of first user message. |
| `state` | yes | `"closed"` for archive, `"idle"` for resumable. Never set `"running"` on import. |
| `kind` | yes | `"interactive"` for normal threads. Do NOT use `"coordinator"` for imports — that's reserved for cluster-spawned coordinator workstreams. |
| `parent_ws_id` | no | Leave NULL. Only set if you're importing a coordinator-spawned subtree and re-parenting it; rare. |
| `user_id` | yes | Owner. Must exist in `users`; importer must know which Turnstone user owns the imported history. |
| `node_id` | yes (multi-node) | Denormalized cache of the node that owns this `ws_id`'s bucket. Single-node deployments can leave it NULL or set it to the only node. |
| `alias` | no | Human-typeable short name. Optional; must be unique cluster-wide if set. |
| `title` | no | Auto-titled later by the LLM; safe to leave NULL on import. |
| `skill_id`, `skill_version` | yes | Default `""` and `0` unless the source thread was scoped to a Turnstone skill. |
| `created`, `updated` | yes | ISO8601 strings. Use the source's first/last message timestamps when available. |

### `conversations` (many rows per thread, ordered by `id`/`timestamp`)

| Column | Notes |
|---|---|
| `ws_id` | The workstream this row belongs to. |
| `timestamp` | ISO8601 string. Preserve source timestamps; fall back to monotonically increasing values if unknown. **Order is canonical via `id` (autoincrement), not `timestamp`** — but always insert in conversational order so both agree. |
| `role` | One of `system`, `user`, `assistant`, `tool`, `developer`. See role mapping below. |
| `content` | Text. May be NULL for assistant rows that are *only* tool calls. |
| `tool_name` | Set on `role="tool"` rows (the tool whose result this is). NULL otherwise. |
| `tool_call_id` | Set on `role="tool"` rows (matches the assistant row's `tool_calls[].id`). NULL otherwise. |
| `tool_calls` | JSON-encoded list, on `role="assistant"` rows that issued tool calls. OpenAI shape — see "Tool Calls" below. |
| `provider_data` | JSON blob preserving provider-native content blocks (Anthropic `signature`, Gemini `thought_signature`, etc.). Optional; only matters for **resumable** imports against the same provider. Skip for archives. |

The internal format is **OpenAI-shaped**, even when the source was Anthropic or Gemini. Providers translate at their own API boundary; storage stays uniform.

## Identity & Routing (`ws_id`)

- `ws_id` is **32-char lowercase hex** (i.e. `secrets.token_hex(16)`).
- The **routing bucket** is `int(ws_id[:4], 16)` — the first 4 hex chars place this workstream on a specific node via the consistent hash ring.
- For multi-node imports: either insert through the console's routing proxy (which forwards to the owning node), or generate `ws_id`s and write directly to each node's database in batches grouped by bucket.
- For single-node imports: bucket math is irrelevant; any `ws_id` works.
- **Do not reuse the source platform's IDs as `ws_id`** unless they happen to be 32-char hex. Generate fresh; if you need the old ID for traceability, store it in `workstream_config` under a key like `import.source_id`.

## Recommended Import Path

Three options, in order of preference:

### 1. Storage protocol (recommended for full history)

Use `turnstone.core.storage.Storage.save_messages_bulk(rows)`. This is the canonical bulk-insert primitive and bypasses the LLM round-trip entirely.

```python
from turnstone.core.storage import get_storage  # construct via the same path the server uses

storage = get_storage(...)  # see turnstone.core.storage.__init__ for the project's wiring

storage.create_workstream(  # or whatever the project's exposed creator is — check turnstone/core/storage/_protocol.py
    ws_id=ws_id,
    user_id=user_id,
    name=name,
    state="closed",
    kind="interactive",
    ...
)

storage.save_messages_bulk([
    {"ws_id": ws_id, "role": "user", "content": "Hello"},
    {"ws_id": ws_id, "role": "assistant", "content": "Hi! What can I help with?"},
    {"ws_id": ws_id, "role": "assistant", "content": None,
     "tool_calls": json.dumps([{"id": "call_1", "type": "function",
                                "function": {"name": "search", "arguments": "{\"q\":\"x\"}"}}])},
    {"ws_id": ws_id, "role": "tool", "tool_name": "search", "tool_call_id": "call_1",
     "content": "result text"},
    # ...
])
```

`save_messages_bulk` handles `timestamp` and the workstream's `updated` column internally, so you don't need to compute them per row. **Verify the exact creator signature** by reading `turnstone/core/storage/_protocol.py` — table layout has shifted across migrations and the Storage protocol is the source of truth.

### 2. SDK `create_workstream(resume_ws=...)` (when the source is already a Turnstone workstream)

Only useful for *Turnstone → Turnstone* re-parenting. Not relevant for foreign sources.

### 3. SDK `create_workstream(initial_message=...)` + `send()` per turn (last resort)

Only fits archives where the source had **no tool calls** and you don't care about preserving assistant turns verbatim. Each `send()` triggers a real LLM round-trip, which is expensive and rewrites assistant content. Don't use this for full history.

## Role Mapping

Common source-role conventions and how they map to Turnstone:

| Source role | Turnstone `role` | Notes |
|---|---|---|
| `user`, `human` | `user` | Direct map. |
| `assistant`, `ai`, `model`, `bot` | `assistant` | Direct map. |
| `system` | `system` | Preserve only if it's content the user wrote (custom instructions). Drop boilerplate provider preambles — Turnstone composes its own system message. |
| `developer` (OpenAI o-series) | `developer` | Preserve. |
| `tool`, `function`, `tool_result` | `tool` | Must carry `tool_name` and `tool_call_id` matching the prior assistant row's `tool_calls[].id`. |
| `tool_use` (Anthropic) | `assistant` with `tool_calls` | Anthropic emits tool calls *inside* an assistant message; flatten to OpenAI shape. |
| `human_feedback`, `revision` | `user` | Treat as a follow-up user turn. |

## Tool Calls (the most error-prone part)

Turnstone stores tool calls in OpenAI's nested-function shape on the assistant row, and matches them with `role="tool"` result rows by `tool_call_id`.

### Assistant row with tool calls

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "search_web",
        "arguments": "{\"query\":\"turnstone import\"}"
      }
    }
  ]
}
```

`tool_calls[].function.arguments` is **a JSON-encoded string**, not an object. Source formats commonly get this wrong — Anthropic stores arguments as a parsed object, Gemini as a struct. Always re-serialize to a string.

### Tool result row

```json
{
  "role": "tool",
  "tool_name": "search_web",
  "tool_call_id": "call_abc123",
  "content": "..."
}
```

Pairing rules:
- Every assistant `tool_calls[].id` MUST be followed by exactly one `role="tool"` row with the matching `tool_call_id`, before the next user/assistant turn.
- If the source dropped the tool result (cut-off transcript), insert a synthetic `role="tool"` row with `content="[tool result missing in source]"` to keep the chain valid. An assistant row with an unanswered `tool_calls[].id` will break replay and any LLM round-trip.
- Multi-tool assistant turns: one `role="tool"` row per call, in any order, all before the next non-tool row.

### Tool ID generation

If the source used opaque tool IDs that aren't unique within a thread (some platforms reuse them), regenerate with a stable scheme like `f"call_{i}"` where `i` is a per-thread counter. Update both the assistant and tool rows together.

## Provider Fidelity (`provider_data`)

Skip this entirely for **archive** imports.

For **resumable** imports against the same provider, populate `provider_data` to preserve provider-specific tool-call metadata that the next API round-trip will require:

- **Anthropic**: `signature` field on thinking blocks; required for round-tripping extended-thinking responses.
- **Gemini**: `thought_signature` on tool calls; required for fidelity.
- **OpenAI**: typically nothing to preserve.

The runtime-side dict key is `_provider_content` (a list of provider-native blocks); the persisted column is `provider_data` (the same list, JSON-encoded). If you don't have provider-native blocks from the source — and you usually won't, because a foreign export won't include them — leave `provider_data` NULL. The first new turn will succeed without it, but the previous assistant turn's reasoning won't replay back to the model.

## Attachments

If the source thread had image or file attachments:

- **Size limits**: images ≤ 4 MiB, text documents ≤ 512 KiB. Reject or downsample anything bigger.
- **Allowed types**: server validates magic bytes for images and UTF-8-decodes for text. Binary blobs that aren't images won't pass.
- **Lifecycle**: pending → reserved → consumed. For imports, the cleanest path is to upload as pending and immediately consume by attaching to the relevant `conversations.id`.

Two import paths:

1. **Bulk-insert + post-attach**: insert messages first, get back the assistant/user `conversations.id`, then write `workstream_attachments` rows linking the file to `message_id`.
2. **SDK multipart create**: `create_workstream(attachments=[...], initial_message=...)` for the *first* turn only — the server reserves and consumes them onto that turn. Doesn't help for mid-thread attachments.

For full-history imports with multiple attachments at different turns, path (1) is the only option.

## Validation Checklist

Before declaring success, verify:

- [ ] `ws_id` is 32-char lowercase hex.
- [ ] `workstreams` row exists with the right `user_id`, `state`, `kind`.
- [ ] Conversation rows are inserted **in order** (autoincrement `id` will reflect insert order).
- [ ] Every assistant `tool_calls[].id` has a matching `role="tool"` row with the same `tool_call_id`.
- [ ] `tool_calls[].function.arguments` is a JSON-encoded **string**, not a parsed object.
- [ ] First message is typically `role="user"` (not `system`) — Turnstone composes its own system prompt at runtime.
- [ ] No empty assistant rows (`content=NULL` AND `tool_calls=NULL` is invalid).
- [ ] If multi-node: the `ws_id`'s bucket maps to a node that exists; `workstreams.node_id` matches.
- [ ] Round-trip test: run `Storage.load_messages(ws_id)` and confirm the reconstructed list matches what you inserted (modulo timestamps).

## Anti-patterns

- **Don't import the source provider's system prompt verbatim.** Provider boilerplate ("You are Claude...", "You are ChatGPT...") will conflict with Turnstone's composed system message and confuse the model on resume. Drop it; preserve only user-authored custom instructions.
- **Don't preserve foreign tool definitions as Turnstone tools.** If the source had custom tools that don't exist in Turnstone, the assistant rows that called them are still valid history (archive), but the workstream is **not resumable** — mark `state="closed"`.
- **Don't fabricate `tool_call_id`s without re-pairing.** Mismatched ids silently break the replay chain on the next turn.
- **Don't skip the `tool_name` field on `role="tool"` rows.** Some load paths use it for display and audit; NULL there will render as "unknown tool".
- **Don't write through the LLM (`send()` per turn) for full history.** It's expensive, rewrites assistant turns, and rate-limits will bite long imports.

## Quick Reference

| Task | Path |
|---|---|
| Generate ws_id | `secrets.token_hex(16)` |
| Bulk insert messages | `Storage.save_messages_bulk(rows)` |
| Archive (read-only) | `state="closed"`, skip `provider_data` |
| Resumable | `state="idle"`, populate `provider_data` if same provider |
| Tool call id | OpenAI shape: `{"id": ..., "type": "function", "function": {"name": ..., "arguments": "<json string>"}}` |
| Tool result row | `role="tool"`, `tool_name`, `tool_call_id`, `content` |
| Source role → Turnstone role | See "Role Mapping" table |
| Per-thread metadata | Store source IDs in `workstream_config` under `import.*` keys |

## Files to read before writing the importer

- `turnstone/core/storage/_schema.py` — authoritative table definitions.
- `turnstone/core/storage/_protocol.py` — `save_message`, `save_messages_bulk`, `load_messages` signatures.
- `turnstone/core/session.py` (around the message-save section) — how the runtime constructs in-memory message dicts; mirror this shape on import to round-trip cleanly.
- `turnstone/api/server_schemas.py` — Pydantic shapes for the SDK paths if you go through HTTP.
