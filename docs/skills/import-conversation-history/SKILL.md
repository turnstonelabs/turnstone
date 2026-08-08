---
name: import-conversation-history
description: Use this skill when the user wants to import or migrate conversation history from another LLM chat or coding tool (e.g. ChatGPT, Claude.ai, Cursor, Copilot Chat, Aider, Gemini, a custom JSON export) into Turnstone. The skill teaches Turnstone's destination contracts — workstream identity, the OpenAI-shaped message rows, tool-call/result pairing, provider-fidelity blobs, attachments, and archive-vs-resumable choice — so the agent can map any source format onto them. Trigger phrases: "import my chats", "migrate this transcript into Turnstone", "bring my Claude.ai history over", "load this export as a workstream".
version: 1.1.0
---

# Importing Conversation History into Turnstone

## Overview

Source formats vary; the destination does not. Your job is to translate whatever the user hands you (JSON dump, ZIP export, scraped HTML, screenshot OCR, raw transcript) into Turnstone's internal shape: **one workstream row** plus an ordered sequence of **conversation rows** in OpenAI message format. This skill documents the destination so you can write a correct mapper for any source.

Two questions to settle with the user before writing anything:

1. **Archive or resumable?** An archive is left closed and is read-only history. A resumable import is also kept closed and unloaded while rows are written, then explicitly opened after validation; this only works cleanly when the source LLM matches a Turnstone-supported provider/model and tool definitions still resolve.
2. **One workstream per source thread, or merge?** Default to one-to-one unless the user explicitly asks to merge.

Default to **archive** when in doubt — resuming a foreign transcript with mismatched tool schemas or stale provider signatures will fail at the next turn.

## Turnstone Data Model (the destination)

Two tables carry the conversation:

### `workstreams` (one row per imported thread)

| Column | Required | Notes |
|---|---|---|
| `ws_id` | yes | 32-char lowercase hex. Auto-generate with `secrets.token_hex(16)` if you don't already have one. The router hashes the **full ID** — see "Identity & Routing" below. |
| `name` | yes | Short title. Pull from source thread title; fall back to first ~60 chars of first user message. |
| `state` | yes | Register as `"closed"` while importing. Leave it closed for an archive; explicitly open it after commit for a resumable import. Never set `"running"` or `"creating"` directly. |
| `kind` | yes | `"interactive"` for normal threads. Do NOT use `"coordinator"` for imports — that's reserved for cluster-spawned coordinator workstreams. |
| `parent_ws_id` | no | Leave NULL. Only set if you're importing a coordinator-spawned subtree and re-parenting it; rare. |
| `user_id` | yes | Owner. Must exist in `users`; importer must know which Turnstone user owns the imported history. |
| `node_id` | no | Nullable creation-time service/liveness hint. It is not the routing key or durable owner and may become stale after membership changes. Let a routed create stamp it; a direct shared-storage import may leave it NULL. |
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
- Ordinary placement is rendezvous (Highest Random Weight, HRW) selection over
  the **full `ws_id`** and the current live server set. For each node, Turnstone
  computes 32-bit FNV-1a over the node ID, a NUL separator, and the full
  workstream ID; it then applies the node weight and selects the highest score.
  A live per-workstream override takes precedence.
- The live set comes from recent `services` heartbeats. Placement can therefore
  change when nodes join, leave, change weight, or an override changes. There
  is no stable prefix-derived placement to pre-compute or persist.
- `workstreams.node_id` is stamped at creation and is not updated as HRW
  placement changes. It supports display and liveness-safe cleanup; the console
  router does not use it as the ordinary ownership decision.
- For multi-node imports, create through the console routing proxy when the
  lifecycle must be published, or write the history once through the cluster's
  configured **shared storage backend**. Never partition rows across node-local
  databases by ID prefix or by a one-time HRW result: a later membership change
  can route the same full ID to another node.
- For single-node imports, HRW placement is degenerate; any valid `ws_id` works.
- **Do not reuse the source platform's IDs as `ws_id`** unless they happen to be 32-char hex. Generate fresh; if you need the old ID for traceability, store it in `workstream_config` under a key like `import.source_id`.

## Recommended Import Path

Three options, in order of preference:

### 1. Quiesced storage import (recommended for full history)

Use the current `turnstone.core.storage.StorageBackend` protocol against the
same shared backend as the cluster. The destination must remain absent from all
in-memory session managers while rows are changing: a loaded `ChatSession`
holds its own trajectory and will not observe conversation rows inserted behind
it.

The safe sequence is:

1. Normalize and validate the complete source transcript before writing.
2. Call `register_workstream(..., state="closed")` and require a `True` return;
   `False` means the caller-selected ID already exists, so abort rather than
   appending to an unrelated workstream.
3. Insert the ordered conversation rows and attachment references.
4. Load the saved rows back and run the validation checklist below.
5. Leave an archive closed. For a resumable import, only now invoke the normal
   `POST /v1/api/workstreams/{ws_id}/open` endpoint on the currently routed
   node so the session hydrates from the complete transcript.

Do **not** create the destination through the web/SDK create endpoint before a
direct bulk import. Create publishes an empty live session. If that already
happened, close the workstream and confirm the manager-authoritative live probe
returns false before writing, then explicitly open it again after validation.

For attachment-free history, `save_messages_bulk(rows)` is the canonical
single-transaction insert primitive and bypasses the LLM round-trip entirely.
New attachment bytes require the per-row path described under
[Attachments](#attachments).

```python
from turnstone.core.storage import get_storage  # initialized by the host/import entry point

storage = get_storage()

inserted = storage.register_workstream(
    ws_id=ws_id,
    user_id=user_id,
    name=name,
    state="closed",
    kind="interactive",
    ...
)
if not inserted:
    raise RuntimeError(f"destination already exists: {ws_id}")

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

`save_messages_bulk` handles `timestamp` and the workstream's `updated` column
internally, so you don't need to compute them per row. Verify the exact
`register_workstream` and message signatures in
`turnstone/core/storage/_protocol.py`; the Storage protocol, not the physical
table layout, is the source of truth.

**Multi-node note:** this path assumes `get_storage()` is connected to the
cluster's shared backend. Do not open a node-local database selected from the
current HRW result, and do not pre-create a live session through the console
routing proxy. After the shared-storage import commits, resolve the current
route and open the closed workstream on that node. Any stored `node_id`
describes creation-time placement, not a permanent shard that should receive a
separate copy.

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
- **Blob identity**: `attachment_id` is the lowercase SHA-256 hex digest of the
  bytes. `workstream_attachments` stores that content-addressed blob and its
  refcount; it has no workstream or message foreign key.
- **Message link**: the sole message-to-blob link is the ordered JSON ID list in
  `conversations.attachments`.
- **No persisted staging lifecycle**: pending upload bytes live only in a
  node's in-memory attachment buffer. The old persisted
  `pending → reserved → consumed` lifecycle does not apply to storage imports.

For new attachment bytes, preserve row order by calling `save_message()` for
each turn. It returns the `conversations.id`; for every attachment referenced by
that turn, call `save_attachment()` with its content hash and bytes, then call
`set_message_attachments(ws_id, message_id, ordered_ids)`. Each
`save_attachment()` call accounts for one reference, while
`set_message_attachments()` records the ordered link.

`save_messages_bulk(..., attachment_ids=[...])` is appropriate only when those
content-addressed blobs already exist: the bulk transaction retains their
references and writes the ordered lists. Do not first call `save_attachment()`
for a new reference and then pass the same reference to `save_messages_bulk()`;
both paths retain it and would double-count the refcount.

SDK multipart create remains useful only for attachments on a new first turn;
it publishes a live session and is not the full-history import path.

## Validation Checklist

Before declaring success, verify:

- [ ] `ws_id` is 32-char lowercase hex.
- [ ] The workstream remained closed and absent from every live manager while rows were written; archives stay closed and resumable imports are opened only after validation.
- [ ] `workstreams` row exists with the right `user_id` and `kind`.
- [ ] Conversation rows are inserted **in order** (autoincrement `id` will reflect insert order).
- [ ] Every assistant `tool_calls[].id` has a matching `role="tool"` row with the same `tool_call_id`.
- [ ] `tool_calls[].function.arguments` is a JSON-encoded **string**, not a parsed object.
- [ ] First message is typically `role="user"` (not `system`) — Turnstone composes its own system prompt at runtime.
- [ ] No empty assistant rows (`content=NULL` AND `tool_calls=NULL` is invalid).
- [ ] Every attachment ID is the SHA-256 of its stored bytes; each turn's ordered IDs are in `conversations.attachments`, and blob refcounts match message references.
- [ ] If multi-node: the row is in shared storage and the node selected by
  `ConsoleRouter.route(ws_id)` from the current live set can load it.
  `workstreams.node_id`, when present, is treated as a creation-time hint rather
  than asserted equal to the current HRW result.
- [ ] Round-trip test: run `Storage.load_messages(ws_id)` and confirm the reconstructed list matches what you inserted (modulo timestamps).

## Anti-patterns

- **Don't import the source provider's system prompt verbatim.** Provider boilerplate ("You are Claude...", "You are ChatGPT...") will conflict with Turnstone's composed system message and confuse the model on resume. Drop it; preserve only user-authored custom instructions.
- **Don't preserve foreign tool definitions as Turnstone tools.** If the source had custom tools that don't exist in Turnstone, the assistant rows that called them are still valid history (archive), but the workstream is **not resumable** — mark `state="closed"`.
- **Don't fabricate `tool_call_id`s without re-pairing.** Mismatched ids silently break the replay chain on the next turn.
- **Don't skip the `tool_name` field on `role="tool"` rows.** Some load paths use it for display and audit; NULL there will render as "unknown tool".
- **Don't write through the LLM (`send()` per turn) for full history.** It's expensive, rewrites assistant turns, and rate-limits will bite long imports.
- **Don't shard imported rows by an ID prefix or a one-time HRW result.** HRW
  uses the full ID and live membership; placement may move. In a cluster, write
  one copy to shared storage and let request routing select the live node.

## Quick Reference

| Task | Path |
|---|---|
| Generate ws_id | `secrets.token_hex(16)` |
| Multi-node placement | Full-ID 32-bit FNV-1a HRW over live servers; store rows once in shared storage |
| Bulk insert attachment-free messages | `Storage.save_messages_bulk(rows)` |
| Attach new bytes | `save_message()` → `save_attachment()` per reference → `set_message_attachments()` |
| Archive (read-only) | `state="closed"`, skip `provider_data` |
| Resumable | Register closed, import and validate while unloaded, then explicitly open; populate `provider_data` if same provider |
| Tool call id | OpenAI shape: `{"id": ..., "type": "function", "function": {"name": ..., "arguments": "<json string>"}}` |
| Tool result row | `role="tool"`, `tool_name`, `tool_call_id`, `content` |
| Source role → Turnstone role | See "Role Mapping" table |
| Per-thread metadata | Store source IDs in `workstream_config` under `import.*` keys |

## Files to read before writing the importer

- `turnstone/core/storage/_schema.py` — authoritative table definitions.
- `turnstone/core/storage/_protocol.py` — `register_workstream`, message, attachment, and load signatures.
- `turnstone/core/rendezvous.py` — authoritative full-ID FNV-1a HRW scoring.
- `turnstone/console/router.py` — live-node discovery, override precedence, and routing behavior.
- `turnstone/core/session.py` (around the message-save section) — how the runtime constructs in-memory message dicts; mirror this shape on import to round-trip cleanly.
- `turnstone/api/server_schemas.py` — Pydantic shapes for the SDK paths if you go through HTTP.
