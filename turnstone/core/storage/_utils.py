"""Shared utilities for storage backends."""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

if TYPE_CHECKING:
    from collections.abc import Iterable

from turnstone.core.attachments import AUDIO_MIME_TO_FORMAT, unreadable_placeholder
from turnstone.core.log import get_logger
from turnstone.core.storage._schema import (
    conversations,
    workstream_attachments,
    workstream_config,
    workstream_overrides,
    workstreams,
)
from turnstone.core.trajectory import (
    AttachmentRef,
    ContentBlock,
    ProviderNative,
    Role,
    TextBlock,
    ToolCall,
    Turn,
    TurnMeta,
    dicts_from_turns,
    resolve_attachment_parts,
)

log = get_logger(__name__)


# Client tool-call block types across providers, used to enforce the
# native↔tool_calls mirror (see ``normalize_native_for_save``).  Anthropic emits
# ``tool_use``; OpenAI Responses ``function_call``; Google's OpenAI-compat lane
# ``function``.  Server-side tool blocks (``server_tool_use`` /
# ``web_search_tool_result`` / ``web_search_call`` / ``*_call`` results) and
# reasoning blocks are deliberately absent: they carry no client ``tool_result`` to
# orphan and must round-trip verbatim.
_CLIENT_TOOL_CALL_BLOCK_TYPES: frozenset[str] = frozenset({"tool_use", "function_call", "function"})


def strip_orphan_client_tool_blocks(blocks: list[Any]) -> list[Any]:
    """Drop client tool-call blocks from a provider-native block list.

    Used when an assistant turn carries no ``tool_calls``: a client tool-call block
    left in the native lane would replay as an orphan ``tool_use`` / ``function_call``
    with no matching ``tool_result`` and be rejected by the API on a same-provider
    resume.  Reasoning / server-tool / web-search blocks are preserved.  Returns a new
    list; the input is not mutated.
    """
    return [
        b
        for b in blocks
        if not (isinstance(b, dict) and b.get("type") in _CLIENT_TOOL_CALL_BLOCK_TYPES)
    ]


def _has_tool_calls(tool_calls_json: str | None) -> bool:
    """True when the stored ``tool_calls`` JSON encodes a non-empty list."""
    if not tool_calls_json:
        return False
    try:
        parsed = json.loads(tool_calls_json)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, list) and len(parsed) > 0


def normalize_native_for_save(
    role: str | None, provider_data: str | None, tool_calls_json: str | None
) -> str | None:
    """Enforce the native↔tool_calls mirror at the persistence boundary.

    A persisted ``assistant`` row must never carry a *client* tool-call block in its
    verbatim native lane (``provider_data``) without a matching entry in ``tool_calls``;
    otherwise a same-provider resume replays an orphan ``tool_use`` / ``function_call``
    with no ``tool_result`` and the provider API rejects the turn (the
    truncated-mid-tool_use hole).  Enforcing the mirror here — once, for every save path
    and both backends — is what lets the orphan-repair pass read ``tool_calls`` alone
    (and lets the Anthropic ``pc_tool_ids`` fallback be retired).

    When ``tool_calls`` is empty, client tool-call blocks in ``provider_data`` are
    orphaned and dropped (reasoning / server-tool / web-search blocks kept).  Returns the
    (possibly rewritten) ``provider_data`` JSON, ``None`` if nothing survives, or the input
    unchanged for non-assistant / well-formed rows.
    """
    if role != "assistant" or not provider_data or _has_tool_calls(tool_calls_json):
        return provider_data
    try:
        blocks = json.loads(provider_data)
    except (json.JSONDecodeError, TypeError):
        return provider_data
    if not isinstance(blocks, list):
        return provider_data
    kept = strip_orphan_client_tool_blocks(blocks)
    if len(kept) == len(blocks):
        return provider_data
    return json.dumps(kept) if kept else None


def wrap_provider_data(provider_data: str | None, producer: str | None) -> str | None:
    """Wrap a bare native-block list in the storage envelope ``{producer, blocks}``.

    ``producer`` (the provider that generated the turn) lets the lowering layer replay the
    native lane verbatim only to its producing provider.  Storage-only: the envelope is
    unwrapped back to a bare block list by :func:`reconstruct_messages`, so every
    ``_provider_content`` consumer still sees a plain list.  Input that is empty, already
    wrapped, unparseable, or has no ``producer`` is returned unchanged (the last keeps the
    legacy bare-list shape, which reconstruct dual-reads).
    """
    if not provider_data or not producer:
        return provider_data
    try:
        blocks = json.loads(provider_data)
    except (json.JSONDecodeError, TypeError):
        return provider_data
    if isinstance(blocks, dict) and "blocks" in blocks:
        return provider_data
    return json.dumps({"producer": producer, "blocks": blocks})


def prepare_provider_data_for_save(
    role: str | None,
    provider_data: str | None,
    tool_calls_json: str | None,
    producer: str | None,
) -> str | None:
    """Save-boundary preparation of the native lane: enforce the mirror, then wrap.

    The single entry point both backends' save paths call:
    :func:`normalize_native_for_save` (the native↔tool_calls mirror, on the bare block
    list) followed by :func:`wrap_provider_data` (the ``{producer, blocks}`` envelope).
    """
    return wrap_provider_data(
        normalize_native_for_save(role, provider_data, tool_calls_json), producer
    )


def release_attachment_refs(conn: Any, attachment_ids: list[str]) -> None:
    """Decrement each referenced blob's refcount, prune any that reach 0.

    The single dialect-agnostic owner of the content-addressed GC decrement —
    both backends call this on rewind/retry (``delete_messages_after``) and on
    workstream delete; the increment twin is each backend's ``save_attachment``
    (INSERT-OR-IGNORE + unconditional ``+1``).  Counts duplicate ids in the
    input — a turn references an id once, but a batch may span several turns that
    each reference the same deduped blob — so the decrement matches the
    references actually removed.  One UPDATE (a searched CASE maps each id to its
    decrement; portable across SQLite/Postgres, unlike a VALUES join) plus one
    prune DELETE, rather than a query per distinct id.  Caller holds the
    connection / transaction.
    """
    if not attachment_ids:
        return
    counts = Counter(attachment_ids)
    ids = list(counts)
    decrement = sa.case(
        *((workstream_attachments.c.attachment_id == aid, n) for aid, n in counts.items()),
        else_=0,
    )
    conn.execute(
        sa.update(workstream_attachments)
        .where(workstream_attachments.c.attachment_id.in_(ids))
        .values(refcount=workstream_attachments.c.refcount - decrement)
    )
    conn.execute(
        sa.delete(workstream_attachments).where(
            sa.and_(
                workstream_attachments.c.attachment_id.in_(ids),
                workstream_attachments.c.refcount <= 0,
            )
        )
    )


def find_orphan_conversations(conn: Any) -> list[dict[str, Any]]:
    """Conversation ws_ids that have no ``workstreams`` row, with row stats.

    Orphans come from writers that persisted without a registered workstream:
    historically the pre-unification CLI/server paths, and the
    delete-during-inflight race (a late tool-result save re-creating rows
    after ``delete_workstream``).  Read-only; ordered oldest-first.  Each
    entry carries the attachment-ref count so a purge's refcount release is
    visible before it happens.
    """
    anti_join = conversations.outerjoin(workstreams, conversations.c.ws_id == workstreams.c.ws_id)
    rows = conn.execute(
        sa.select(
            conversations.c.ws_id,
            sa.func.count().label("row_count"),
            sa.func.min(conversations.c.timestamp).label("first"),
            sa.func.max(conversations.c.timestamp).label("last"),
        )
        .select_from(anti_join)
        .where(workstreams.c.ws_id.is_(None))
        .group_by(conversations.c.ws_id)
        .order_by(sa.func.min(conversations.c.timestamp))
    ).fetchall()
    # Ref counts in ONE pass over the orphan rows that carry attachments —
    # not a query per orphan workstream, so the scan stays proportional to
    # orphan ROW count.
    ref_counts: dict[str, int] = {}
    ref_rows = conn.execute(
        sa.select(conversations.c.ws_id, conversations.c.attachments)
        .select_from(anti_join)
        .where(
            sa.and_(
                workstreams.c.ws_id.is_(None),
                conversations.c.attachments.is_not(None),
            )
        )
    ).fetchall()
    for ws_id, refs in ref_rows:
        ref_counts[ws_id] = ref_counts.get(ws_id, 0) + len(parse_attachment_refs(refs))
    return [
        {
            "ws_id": ws_id,
            "rows": int(row_count),
            "first": first,
            "last": last,
            "attachment_refs": ref_counts.get(ws_id, 0),
        }
        for ws_id, row_count, first, last in rows
    ]


# IN-list chunk size for the purge statements — mirrors the storage layer's
# existing bulk chunking (SQLite bind-parameter limits).
_PURGE_CHUNK = 500


def purge_orphan_conversations(conn: Any, ws_ids: list[str]) -> dict[str, int]:
    """Delete conversation rows for the *ws_ids* that are STILL orphans.

    Orphan-ness is enforced INSIDE the DELETE itself (a correlated
    ``NOT EXISTS`` against ``workstreams``), and the refcounts to release
    come from the DELETE's ``RETURNING`` — so refs are released for exactly
    the rows that were deleted.  A ws_id registered at any point before the
    DELETE statement keeps both its rows AND its refcounts; there is no
    pre-count/delete window to underflow.  (Needs ``DELETE .. RETURNING``:
    PostgreSQL, or SQLite ≥ 3.35.)

    Input is de-duplicated and all IN-lists are chunked.  ``skipped`` =
    distinct input ws_ids not purged (registered before/during the purge, or
    no rows).  The purged ws_ids' ``workstream_config`` /
    ``workstream_overrides`` rows are swept.  Caller owns commit.
    """
    distinct = list(dict.fromkeys(ws_ids))
    if not distinct:
        return {"workstreams": 0, "rows": 0, "released_refs": 0, "skipped": 0}
    ref_ids: list[str] = []
    purged_ws: set[str] = set()
    rows_deleted = 0
    for i in range(0, len(distinct), _PURGE_CHUNK):
        chunk = distinct[i : i + _PURGE_CHUNK]
        returned = conn.execute(
            sa.delete(conversations)
            .where(
                sa.and_(
                    conversations.c.ws_id.in_(chunk),
                    ~sa.exists(
                        sa.select(workstreams.c.ws_id).where(
                            workstreams.c.ws_id == conversations.c.ws_id
                        )
                    ),
                )
            )
            .returning(conversations.c.ws_id, conversations.c.attachments)
        ).fetchall()
        for ws_id, refs in returned:
            purged_ws.add(ws_id)
            rows_deleted += 1
            if refs:
                ref_ids.extend(parse_attachment_refs(refs))
    release_attachment_refs(conn, ref_ids)
    swept = sorted(purged_ws)
    for i in range(0, len(swept), _PURGE_CHUNK):
        chunk = swept[i : i + _PURGE_CHUNK]
        conn.execute(sa.delete(workstream_config).where(workstream_config.c.ws_id.in_(chunk)))
        conn.execute(sa.delete(workstream_overrides).where(workstream_overrides.c.ws_id.in_(chunk)))
    return {
        "workstreams": len(purged_ws),
        "rows": rows_deleted,
        "released_refs": len(ref_ids),
        "skipped": len(distinct) - len(purged_ws),
    }


def attachment_to_content_part(att: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a stored attachment row into an OpenAI-style content part.

    Returns ``None`` if the attachment's ``kind`` / ``content`` cannot be
    turned into a content part (logged but non-fatal so history still renders).
    """
    kind = att.get("kind")
    raw = att.get("content")
    mime = att.get("mime_type") or "application/octet-stream"
    if kind == "image" and isinstance(raw, bytes):
        from turnstone.core.images import normalize_image_orientation

        # Bake EXIF orientation into the pixels — the model's image decoder, like
        # Pillow, ignores the orientation tag, so a phone photo would otherwise be
        # perceived sideways.  Unrotated images pass through untouched.
        b64 = base64.b64encode(normalize_image_orientation(raw)).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }
    if kind == "text" and isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            log.warning(
                "attachment id=%s stored as text but not valid UTF-8",
                att.get("attachment_id"),
            )
            return unreadable_placeholder(att.get("filename") or "")
        return {
            "type": "document",
            "document": {
                "name": att.get("filename") or "",
                "media_type": mime,
                "data": text,
            },
        }
    if kind == "pdf" and isinstance(raw, bytes):
        # PDF rides as a ``document`` part discriminated by media_type:
        # base64 bytes (vs. a text doc's utf-8 ``data``).  Per-provider
        # translators branch on ``application/pdf`` (Phase 2); the client-side
        # fallback for non-PDF models lands in Phase 3.
        b64 = base64.b64encode(raw).decode("ascii")
        return {
            "type": "document",
            "document": {
                "name": att.get("filename") or "",
                "media_type": "application/pdf",
                "data": b64,
            },
        }
    if kind == "audio" and isinstance(raw, bytes):
        # OpenAI-style ``input_audio`` part — passes through the openai-compat
        # lane untouched (omni models); other lanes translate / fall back in
        # Phase 2/3.  ``format`` is the bare codec token derived from the MIME.
        b64 = base64.b64encode(raw).decode("ascii")
        fmt = AUDIO_MIME_TO_FORMAT.get(mime) or (mime.split("/", 1)[-1] if "/" in mime else "wav")
        return {
            "type": "input_audio",
            "input_audio": {"data": b64, "format": fmt},
        }
    return None


def parse_attachment_refs(raw: str | None) -> list[str]:
    """Decode a ``conversations.attachments`` ref-list column into id strings.

    The column stores a JSON array of content-addressed ``attachment_id``s in
    turn order (NULL / empty for turns with no attachments).  Malformed or
    non-list JSON decodes to an empty list (defensive — a corrupt column must
    never crash a history load); non-string elements are dropped.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if isinstance(x, str) and x]


def build_attachments_by_msg(
    attachment_refs: dict[int, list[str]],
    rows_by_id: dict[str, dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Assemble the ``reconstruct_messages`` attachment map from ref-lists.

    ``attachment_refs`` maps a conversations row id to its ordered list of
    content-addressed ids (from :func:`parse_attachment_refs`); ``rows_by_id``
    maps an attachment id to its resolved blob row (incl. ``content`` bytes).
    Returns ``{row_id: [att_row, ...]}`` preserving ref-list order, skipping
    ids whose blob is missing (pruned / never written).  Empty lists are
    omitted so the caller can pass ``result or None`` unchanged.
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    for mid, ids in attachment_refs.items():
        resolved = [rows_by_id[aid] for aid in ids if aid in rows_by_id]
        if resolved:
            grouped[mid] = resolved
    return grouped


def _reconstruct_attachment_refs(
    attachments_by_msg: dict[int, list[dict[str, Any]]] | None,
    row_id: int | None,
) -> tuple[list[AttachmentRef], list[dict[str, Any]]]:
    """Build ``(attachment_refs, attachments_meta)`` for a row from its ref-list.

    ``attachments_by_msg`` maps a conversations row id to the ordered list of
    content-addressed attachment rows referenced by that row's ``attachments``
    column.  Returns the by-reference content blocks (:class:`AttachmentRef`, in
    ref-list order — bytes resolve at the consumer, never carried in the Turn)
    and the display-oriented ``_attachments_meta`` siblings (kind / filename /
    mime_type) so history replay keeps filenames available (e.g. image pills).
    Shared by the user- and tool-row reconstruction.
    """
    refs: list[AttachmentRef] = []
    meta: list[dict[str, Any]] = []
    if not attachments_by_msg or row_id is None:
        return refs, meta
    for att in attachments_by_msg.get(row_id, []):
        # AttachmentRef.kind is the by-reference placeholder kind: the stored
        # blob kind verbatim for image / pdf / audio, else 'document' for a
        # stored 'text' blob (so the placeholder type can't collide with a real
        # text content part on the dict round-trip).  The blob kind drives the
        # actual resolution; preserving pdf / audio here keeps the reloaded
        # placeholder type ({type:pdf} / {type:audio}) consistent with the live
        # injection path, which already emits those.
        kind_str = str(att.get("kind") or "")
        if kind_str == "preview":
            # Preview-pane blobs ride the ref-list ONLY for refcount GC and
            # the serving-route ownership gate — they are frontend content,
            # addressed by the tool turn's meta descriptor, and must never
            # become a content block a wire materialization could inline.
            continue
        ref_kind = kind_str if kind_str in ("image", "pdf", "audio") else "document"
        refs.append(
            AttachmentRef(
                attachment_id=str(att.get("attachment_id") or ""),
                kind=ref_kind,
            )
        )
        meta.append(
            {
                "kind": str(att.get("kind") or ""),
                "filename": str(att.get("filename") or ""),
                "mime_type": str(att.get("mime_type") or ""),
                # Doc-budget proxy for the by-reference placeholder (which carries
                # no inline bytes): the token estimator reads this so a reloaded
                # document turn isn't counted as ~free.  See ``_msg_text_chars``.
                "size_bytes": int(att.get("size_bytes") or 0),
            }
        )
    return refs, meta


# ---------------------------------------------------------------------------
# Search-term normalization
# ---------------------------------------------------------------------------

# Composition can hand a multi-KB pasted user message to ILIKE-based search;
# without a cap, every distinct token would emit one unindexable predicate
# per scope-fanned query, producing hundreds of seq-scan clauses on a single
# rebuild.  Cap + dedupe + length filter keeps the SQL bounded.
_MAX_SEARCH_TERMS = 16
_MIN_TERM_LEN = 2

# Streaming tokenizer — finditer doesn't allocate a full list up front,
# so a multi-KB pasted query stops being scanned the moment the cap is
# hit instead of after splitting every token.
_TOKEN_RE = re.compile(r"\S+")


def normalize_search_terms(query: str) -> list[str]:
    """De-dupe (case-insensitive), drop short tokens, and cap at MAX terms."""
    seen: set[str] = set()
    terms: list[str] = []
    for match in _TOKEN_RE.finditer(query):
        raw = match.group()
        lowered = raw.lower()
        if len(lowered) < _MIN_TERM_LEN or lowered in seen:
            continue
        seen.add(lowered)
        terms.append(raw)
        if len(terms) >= _MAX_SEARCH_TERMS:
            break
    return terms


# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------


def sanitize_text(value: str | None) -> str | None:
    """Strip NUL bytes that PostgreSQL text fields cannot store.

    SQLite tolerates NUL in TEXT but they cause downstream issues (API
    payloads, web UI rendering), so both backends use this.
    """
    if value and "\x00" in value:
        return value.replace("\x00", "")
    return value


# ---------------------------------------------------------------------------
# SQL LIKE escaping
# ---------------------------------------------------------------------------

# The escape character paired with :func:`escape_like`.  Callers MUST
# pass ``escape=LIKE_ESCAPE`` to SQLAlchemy's ``.like()`` — without
# that kwarg, ``.like()`` uses no escape character at all and the
# ``\%`` / ``\_`` sequences produced by :func:`escape_like` would be
# interpreted as a literal backslash followed by a wildcard.  ``\``
# is the SQL standard escape character and works identically on SQLite
# and PostgreSQL when passed explicitly.
LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Escape ``%`` and ``_`` (and the escape character itself) so the
    string can be safely embedded in a SQL ``LIKE`` pattern.

    Pair with ``column.like(escape_like(prefix) + "%", escape=LIKE_ESCAPE)``
    to do a true prefix match against caller-supplied input.  Without
    this, untrusted text containing ``%`` or ``_`` is interpreted as a
    wildcard — e.g. a model-supplied watch name of ``"%"`` would match
    every row in the queried partition.
    """
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


# ---------------------------------------------------------------------------
# Row helper
# ---------------------------------------------------------------------------


def row_to_dict(row: Any, *bool_fields: str) -> dict[str, Any]:
    """Convert a SQLAlchemy row to a dict, casting named fields to bool."""
    d = dict(row._mapping)
    for key in bool_fields:
        if key in d:
            d[key] = bool(d[key])
    return d


def split_perms(value: str | None) -> set[str]:
    """Split the comma-separated ``roles.permissions`` column into a set."""
    if not value:
        return set()
    return {p.strip() for p in value.split(",") if p.strip()}


# ---------------------------------------------------------------------------
# Field allowlists for governance update methods
# ---------------------------------------------------------------------------

ROLE_MUTABLE = frozenset({"display_name", "permissions"})
ORG_MUTABLE = frozenset({"display_name", "settings"})
POLICY_MUTABLE = frozenset({"name", "tool_pattern", "action", "priority", "enabled"})
SKILL_MUTABLE = frozenset(
    {
        "name",
        "content",
        "category",
        "variables",
        "is_default",
        "description",
        "tags",
        "source_url",
        "version",
        "author",
        "activation",
        "token_estimate",
        "model",
        "auto_approve",
        "temperature",
        "reasoning_effort",
        "max_tokens",
        "token_budget",
        "agent_max_turns",
        "notify_on_complete",
        "enabled",
        "allowed_tools",
        "license",
        "compatibility",
        "scan_version",
        "risk_level",
        "scan_report",
        "priority",
        "kind",
        # SKILL.md spec uplift (migration 056)
        "paths",
        "hidden_from_menu",
        "arguments",
        "argument_hint",
    }
)
# ``oauth_client_secret_ct`` is intentionally absent from this set.  It has
# its own dedicated writer (``StorageBackend.set_mcp_oauth_client_secret_ct``)
# so the encrypt/None-to-clear semantics — owned by
# :class:`turnstone.core.mcp_crypto.MCPTokenStore` — live in one place and
# the generic ``update_mcp_server`` cannot accept a raw ciphertext blob.
MCP_SERVER_MUTABLE = frozenset(
    {
        "name",
        "transport",
        "command",
        "args",
        "url",
        "headers",
        "env",
        "auto_approve",
        "enabled",
        "registry_name",
        "registry_version",
        "registry_meta",
        "auth_type",
        "oauth_client_id",
        "oauth_scopes",
        "oauth_audience",
        "oauth_registration_mode",
        "oauth_authorization_server_url",
        "oauth_as_issuer_cached",
    }
)
MODEL_DEFINITION_MUTABLE = frozenset(
    {
        "alias",
        "model",
        "provider",
        "base_url",
        "api_key",
        "context_window",
        "capabilities",
        "enabled",
        "temperature",
        "max_tokens",
        "reasoning_effort",
        "surface_persisted_reasoning",
        "replay_reasoning_to_model",
    }
)
PROJECT_MUTABLE = frozenset({"name", "visibility", "state", "parent_project_id"})
PROMPT_POLICY_MUTABLE = frozenset({"name", "content", "tool_gate", "priority", "enabled"})
# ``name`` (the slug create requests reference) is deliberately immutable —
# edit display_name instead.  Workstream snapshots are self-contained so a
# rename wouldn't break them, but stable slugs keep audit rows and operator
# muscle memory honest.
PERSONA_MUTABLE = frozenset(
    {
        "display_name",
        "description",
        "base_prompt",
        "tool_allowlist",
        "mcp_enabled",
        "memory_enabled",
        "applies_to_kinds",
        "is_default",
        "enabled",
    }
)

PERSONA_KINDS = frozenset({"interactive", "coordinator"})


def persona_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a personas row to the Python-typed dict shape the Protocol
    documents: JSON columns parsed, 0/1 columns as bool.  ``tool_allowlist``
    keeps its tri-state — None (unrestricted) vs [] (hard empty) vs [names].

    Raises ValueError on a corrupt row: a malformed allowlist or kinds
    column must fail loudly (mirroring ``snapshot_from_config``), never
    decode into a garbage envelope or mask a broken invariant.
    """
    d = row_to_dict(row, "mcp_enabled", "memory_enabled", "is_default", "enabled")

    def _load_json(column: str, raw_value: Any) -> Any:
        # Re-raise parser errors with the persona named — a bare
        # JSONDecodeError message doesn't say WHICH row is corrupt.
        try:
            return json.loads(raw_value)
        except ValueError as exc:
            raise ValueError(f"corrupt {column} on persona {d.get('persona_id')!r}: {exc}") from exc

    raw = d.get("tool_allowlist")
    if raw is None:
        d["tool_allowlist"] = None
    else:
        tools = _load_json("tool_allowlist", raw)
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValueError(f"corrupt tool_allowlist on persona {d.get('persona_id')!r}")
        d["tool_allowlist"] = tools
    kinds_raw = d.get("applies_to_kinds")
    kinds = _load_json("applies_to_kinds", kinds_raw) if kinds_raw else None
    if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
        raise ValueError(f"corrupt applies_to_kinds on persona {d.get('persona_id')!r}")
    d["applies_to_kinds"] = kinds
    return d


# Storage-layer size bounds for operator-authored persona fields.  The
# console route truncates its inputs to the same shape, but the storage
# edge is the layer every future ingress (SDK-direct, admin CLI) inherits —
# reject rather than silently truncate here.
PERSONA_FIELD_CAPS: dict[str, int] = {
    "display_name": 128,
    "description": 1024,
    "base_prompt": 32768,
}
PERSONA_ALLOWLIST_MAX_ENTRIES = 512
PERSONA_ALLOWLIST_MAX_NAME_LEN = 256


def serialize_persona_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate + serialize Python-typed persona fields to column values.

    Shared by both backends so the tri-state allowlist encoding, the kinds
    validation, and the size bounds can't drift between them.  Raises
    ValueError on malformed or oversized input; unknown keys pass through
    (callers filter to the mutable set first where that matters).
    """
    out = dict(fields)
    for key, cap in PERSONA_FIELD_CAPS.items():
        val = out.get(key)
        if val is not None and key in out and len(str(val)) > cap:
            raise ValueError(f"{key} exceeds {cap} characters")
    # An empty inline prompt is not a source: normalise "" to NULL so the
    # storage CHECK (base_prompt OR base_prompt_file) and the coalesce
    # resolution (base_prompt ?? file) agree on what "unset" means.
    bp = out.get("base_prompt")
    if "base_prompt" in out and isinstance(bp, str) and not bp.strip():
        out["base_prompt"] = None
    if "applies_to_kinds" in out:
        kinds = out["applies_to_kinds"]
        if not isinstance(kinds, list) or not kinds or not set(kinds) <= PERSONA_KINDS:
            raise ValueError(
                f"applies_to_kinds must be a non-empty subset of {sorted(PERSONA_KINDS)}"
            )
        out["applies_to_kinds"] = json.dumps(kinds)
    if "tool_allowlist" in out and out["tool_allowlist"] is not None:
        tools = out["tool_allowlist"]
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValueError("tool_allowlist must be None or a list of tool names")
        if len(tools) > PERSONA_ALLOWLIST_MAX_ENTRIES:
            raise ValueError(f"tool_allowlist exceeds {PERSONA_ALLOWLIST_MAX_ENTRIES} entries")
        if any(len(t) > PERSONA_ALLOWLIST_MAX_NAME_LEN for t in tools):
            raise ValueError(
                f"tool_allowlist entries are capped at {PERSONA_ALLOWLIST_MAX_NAME_LEN} chars"
            )
        out["tool_allowlist"] = json.dumps(tools)
    for key in ("mcp_enabled", "memory_enabled", "is_default", "enabled"):
        if key in out:
            out[key] = 1 if out[key] else 0
    return out


def validate_and_clear_default_persona(
    conn: Any,
    personas_table: Any,
    *,
    persona_id: str,
    kinds: list[str],
    enabled: Any,
    now: str,
) -> None:
    """Enforce the default-persona invariants and demote the incumbent,
    inside the caller's transaction.

    Shared by both backends (fully dialect-neutral) so the invariants —
    exactly one default per kind, single-kind, enabled — cannot drift.
    A corrupt incumbent row raises rather than being skipped: silently
    not-demoting it would commit two defaults, the exact state this
    helper exists to prevent.  Concurrency: the PostgreSQL backend
    serializes promotions with an advisory xact lock before calling this;
    ``assert_single_default_persona`` runs post-promote as the backstop.
    """
    if not isinstance(kinds, list) or len(kinds) != 1:
        raise ValueError("a default persona must apply to exactly one kind")
    if not enabled:
        raise ValueError("a disabled persona cannot be the default")
    kind = kinds[0]
    others = conn.execute(
        sa.select(personas_table.c.persona_id, personas_table.c.applies_to_kinds).where(
            sa.and_(
                personas_table.c.is_default == 1,
                personas_table.c.persona_id != persona_id,
            )
        )
    ).fetchall()
    for oid, okinds_raw in others:
        okinds = json.loads(okinds_raw) if okinds_raw else None
        if not isinstance(okinds, list):
            raise ValueError(f"corrupt applies_to_kinds on persona {oid!r}")
        if kind in okinds:
            conn.execute(
                sa.update(personas_table)
                .where(personas_table.c.persona_id == oid)
                .values(is_default=0, updated=now)
            )


def assert_single_default_persona(conn: Any, personas_table: Any, kind: str) -> None:
    """Post-promote backstop: raise (rolling back the enclosing
    transaction) if more than one enabled default applies to *kind* — a
    concurrent promotion that slipped past serialization must fail loudly,
    never commit a nondeterministic default."""
    rows = conn.execute(
        sa.select(personas_table.c.persona_id, personas_table.c.applies_to_kinds).where(
            personas_table.c.is_default == 1
        )
    ).fetchall()
    holders = [oid for oid, kr in rows if kind in (json.loads(kr) if kr else [])]
    if len(holders) > 1:
        raise ValueError(f"concurrent default-persona change detected for kind {kind!r}; retry")


HEURISTIC_RULE_MUTABLE = frozenset(
    {
        "name",
        "risk_level",
        "confidence",
        "recommendation",
        "tool_pattern",
        "arg_patterns",
        "intent_template",
        "reasoning_template",
        "tier",
        "priority",
        "builtin",
        "enabled",
    }
)
OUTPUT_GUARD_PATTERN_MUTABLE = frozenset(
    {
        "name",
        "category",
        "risk_level",
        "pattern",
        "pattern_flags",
        "flag_name",
        "annotation",
        "is_credential",
        "redact_label",
        "priority",
        "builtin",
        "enabled",
    }
)
VERDICT_MUTABLE = frozenset(
    {
        "user_decision",
        "intent_summary",
        "risk_level",
        "confidence",
        "recommendation",
        "reasoning",
        "evidence",
        "tier",
        "judge_model",
        "latency_ms",
    }
)


# ---------------------------------------------------------------------------
# Skill scanning helper
# ---------------------------------------------------------------------------


def scan_skill_content(content: str, allowed_tools: str) -> tuple[str, str, str]:
    """Run the skill scanner and return ``(risk_level, scan_report_json, scanner_version)``.

    Uses a lazy import to avoid circular dependencies.  Silently returns
    empty results on import or scan errors so skill creation is never
    blocked by a scanner bug.
    """
    try:
        from turnstone.core.skill_scanner import SCANNER_VERSION, scan_skill

        tools: list[str] | None = None
        if allowed_tools and allowed_tools.strip() != "[]":
            try:
                parsed = json.loads(allowed_tools)
                if isinstance(parsed, list):
                    tools = [str(x) for x in parsed if isinstance(x, str)]
                    if not tools:
                        tools = None
            except (json.JSONDecodeError, TypeError):
                pass  # falls back to None (no tool filter)
        result = scan_skill(content, tools)
        return result.tier, json.dumps(result.to_dict(), ensure_ascii=False), SCANNER_VERSION
    except Exception:
        log.debug("skill_scanner: scan failed", exc_info=True)
        return "", "{}", ""


# ---------------------------------------------------------------------------
# Message reconstruction
# ---------------------------------------------------------------------------


def reconstruct_messages(
    rows: list[Any],
    ws_id: str,
    attachments_by_msg: dict[int, list[dict[str, Any]]] | None = None,
    *,
    repair: bool = True,
) -> list[dict[str, Any]]:
    """Reconstruct OpenAI message format from stored conversation rows.

    Each *row* is an 8-to-11-tuple ``(id, role, content, tool_name,
    tool_call_id, provider_data, tool_calls_json, source [, event_id [, is_error
    [, meta]]])``, ordered chronologically by row id.  ``source`` is rehydrated
    as the ``_source`` side channel.  (The legacy ``_reminders`` column that used
    to ride here was dropped in migration 060 — operator context lives in
    first-class ``system`` turns now.)  The trailing optional elements —
    ``event_id`` (migration 059, the per-ws SSE ``Last-Event-ID`` resume cursor →
    ``_event_id``), ``is_error`` (migration 060, the persisted tool-result error
    flag), and ``meta`` (migration 060, an operator-context turn's structured
    ``_source_meta``) — are handled by the defensive unpack so shorter legacy
    fixtures stay valid.

    When ``attachments_by_msg`` is provided (keyed by row id, each value an
    ordered list of content-addressed attachment rows resolved from the
    ``conversations.attachments`` ref-list), any ``user`` *or* ``tool`` row
    whose id has attachments is rebuilt with multipart list content (text +
    image_url/document parts).  Tool rows carry persisted vision output
    (``read_file`` on an image) this way — they would otherwise reload as the
    flattened text alone.

    When ``repair`` is True (default) the trailing ``assistant(tool_calls)``
    turn is dropped if not all tool_call ids have a matching tool result
    (trailing operator-context ``system`` turns are looked through and stripped
    with it) — boot-crash recovery so a half-finished turn never replays.
    Mid-conversation orphaned tool_calls are *not* filled here; that is the
    send-time repair (:func:`turnstone.core.lowering.repair_wire_messages`), the
    single place the wire path synthesizes cancellation results.  Callers that
    consume the messages as LLM context via the session send path
    (``session.resume``) get that repair for free; a consumer that bypasses it
    (``export``) runs ``repair_wire_messages`` itself.  Callers reading for
    *display* (the ``/history`` REST endpoint) should pass ``repair=False`` so
    the user sees the actual partial state — refreshing during tool execution
    otherwise silently drops the trailing turn from the UI.
    """
    # Drop compaction checkpoint markers: they are resume-only artifacts (the
    # persisted summary that lets a reopened session rehydrate a bounded context,
    # see reconstruct_turns_checkpointed), not real conversation turns, so
    # /history, export, and search show the true transcript without an injected
    # summary.
    rows = [r for r in rows if not _is_compaction_marker(r)]
    turns = reconstruct_turns(rows, ws_id, attachments_by_msg)
    if repair:
        turns = recover_trajectory(turns)
    # Dict consumers (display, export) want materialized content, so resolve the
    # by-reference attachments to inline parts using the blob rows already in
    # hand.  ``load_message_turns`` is the unresolved canonical path for resume.
    dicts = dicts_from_turns(turns)
    if attachments_by_msg:
        parts_by_id = {
            str(att.get("attachment_id") or ""): part
            for atts in attachments_by_msg.values()
            for att in atts
            if (part := attachment_to_content_part(att)) is not None
        }
        if parts_by_id:
            dicts = resolve_attachment_parts(dicts, parts_by_id)
    return dicts


def _content_blocks(text: str | None, refs: list[AttachmentRef]) -> tuple[ContentBlock, ...]:
    """Build typed content blocks from a row's text column + attachment refs.

    A row with attachments becomes a leading text block plus one
    :class:`AttachmentRef` per attachment (``read_file`` vision output, user
    uploads — bytes resolve at the consumer); a text-only row is a single text
    block, or empty.
    """
    if refs:
        return (TextBlock(text or ""), *refs)
    if text:
        return (TextBlock(text),)
    return ()


def _native_from_provider_data(
    provider_data: str | None, tool_calls_json: str | None
) -> ProviderNative | None:
    """Decode the stored ``provider_data`` lane into a :class:`ProviderNative`.

    The storage envelope is ``{producer, blocks}`` (new) or a bare block list
    (legacy, no producer).  A decode failure or non-list/non-envelope payload
    yields ``None`` (the lane is dropped — matching the prior best-effort decode).

    Load-side mirror self-heal: when the row carries no ``tool_calls`` (empty or
    absent column), any *client* tool-call block in the native lane is an orphan —
    a legacy truncated-mid-``tool_use`` row predating the save-time
    ``normalize_native_for_save`` chokepoint — that would replay on a same-provider
    resume with no matching ``tool_result`` (Anthropic 400; Google resurrects it
    into ``tool_calls`` via its fidelity lane).  Strip those blocks here so every
    legacy row heals on read regardless of whether migration 060 tagged it
    (reasoning / server-tool / web-search blocks kept).  Mirrors
    ``normalize_native_for_save``'s gate, including its ``None`` when nothing
    survives.  The healthy (mirror-holds) path is byte-identical to a plain decode.
    """
    if not provider_data:
        return None
    try:
        parsed = json.loads(provider_data)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and "blocks" in parsed:
        producer = parsed.get("producer") or ""
        blocks = list(parsed["blocks"])
    elif isinstance(parsed, list):
        producer = ""
        blocks = parsed
    else:
        return None
    if not _has_tool_calls(tool_calls_json):
        blocks = strip_orphan_client_tool_blocks(blocks)
        if not blocks:
            return None
    return ProviderNative(producer=producer, blocks=tuple(blocks))


def _source_meta_from_json(meta_json: str | None) -> dict[str, Any] | None:
    """Decode the stored ``meta`` column into an operator-context meta dict.

    The persisted twin of ``Turn.meta.extra["source_meta"]`` — a first-class
    ``system`` turn's structured per-kind fields (e.g. ``watch_triggered``'s
    ``watch_name`` / ``command`` / poll counters).  A decode failure or a
    non-object payload yields ``None`` (the meta is dropped — the human-readable
    body still lives in ``content``).
    """
    if not meta_json:
        return None
    try:
        parsed = json.loads(meta_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _tool_calls_from_json(tool_calls_json: str | None) -> tuple[ToolCall, ...]:
    """Decode the stored ``tool_calls`` column into typed :class:`ToolCall`s."""
    if not tool_calls_json:
        return ()
    try:
        parsed = json.loads(tool_calls_json)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(
        ToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", ""),
        )
        for tc in parsed
        if isinstance(tc, dict)
    )


def reconstruct_turns(
    rows: list[Any],
    ws_id: str,
    attachments_by_msg: dict[int, list[dict[str, Any]]] | None = None,
) -> list[Turn]:
    """Deserialize stored conversation rows into canonical ``Turn``s (pure).

    The ``row → Turn`` boundary: one positional unpack of the row tuple, one
    ``Turn`` per row, no wire-validity correction (the lowering layer owns that
    — see :func:`recover_trajectory` for the load-time strip).  The legacy
    ``tool_name`` column (position 3) is unpacked but never used.  Unknown roles
    are dropped (the roles below are exhaustive for stored conversations);
    ``developer`` collapses into ``Role.SYSTEM``.
    """
    turns: list[Turn] = []
    for row in rows:
        (row_id, role, content, _tool_name, tc_id, provider_data, tool_calls_json, source) = row[:8]
        # event_id (col 9, migration 059) — the per-ws SSE Last-Event-ID cursor;
        # is_error (col 10, migration 060); meta (col 11, also migration 060 — the
        # operator-context per-kind ``source_meta``) rides last.  Defensive
        # length checks keep pre-event_id / pre-is_error / pre-meta fixtures valid.
        event_id = int(row[8]) if len(row) > 8 and row[8] is not None else None
        is_error = bool(row[9]) if len(row) > 9 else False
        meta = TurnMeta(event_id=event_id)
        raw_meta = _source_meta_from_json(row[10]) if len(row) > 10 else None
        if raw_meta is not None:
            # The ``meta`` column is role-exclusive: a TOOL row carries the
            # typed ``{"effect_status": ..., "preview": ...}`` envelope (each
            # key optional); a SYSTEM row carries operator-context
            # ``source_meta``. Route so a tool's disposition doesn't land
            # under source_meta (and vice versa). Legacy SYSTEM rows (bare
            # source_meta dict, no tool keys) fall through.
            if role == "tool" and ("effect_status" in raw_meta or "preview" in raw_meta):
                if "effect_status" in raw_meta:
                    meta.extra["effect_status"] = raw_meta["effect_status"]
                if "preview" in raw_meta:
                    meta.extra["preview"] = raw_meta["preview"]
            elif role == "user" and "sender" in raw_meta:
                # Per-message sender identity (shared-workstream attribution).
                # A USER row's meta blob carries only ``{"sender": ...}`` — route
                # it to its own key so history replay re-attributes each turn to
                # the human who sent it (source_meta rides SYSTEM turns, never
                # user turns, so there is no collision).
                meta.extra["sender"] = raw_meta["sender"]
            else:
                meta.extra["source_meta"] = raw_meta
        src = str(source) if source else None

        if role == "user":
            refs, am = _reconstruct_attachment_refs(attachments_by_msg, row_id)
            if am:
                meta.extra["attachments_meta"] = am
            turns.append(Turn(Role.USER, _content_blocks(content, refs), source=src, meta=meta))
        elif role == "assistant":
            turns.append(
                Turn(
                    Role.ASSISTANT,
                    _content_blocks(content, []),
                    tool_calls=_tool_calls_from_json(tool_calls_json),
                    native=_native_from_provider_data(provider_data, tool_calls_json),
                    meta=meta,
                )
            )
        elif role == "tool":
            trefs, _tmeta = _reconstruct_attachment_refs(attachments_by_msg, row_id)
            turns.append(
                Turn(
                    Role.TOOL,
                    _content_blocks(content, trefs),
                    tool_call_id=tc_id or "",
                    is_error=is_error,
                    meta=meta,
                )
            )
        elif role in ("system", "developer"):
            turns.append(Turn(Role.SYSTEM, _content_blocks(content, []), source=src, meta=meta))
    return turns


def recover_trajectory(turns: list[Turn]) -> list[Turn]:
    """Strip a trailing incomplete tool-call turn (boot-crash recovery).

    The load-time orphan policy: walk back past trailing tool results AND
    operator-context system turns (which follow the turn they relate to) to the
    turn's assistant head; if its tool_calls are not all answered, drop from the
    assistant onward.  Mid-conversation orphans are left for the send-time
    repair (``lowering.repair_wire_messages``).  Returns a new list; the input is
    not mutated.
    """
    turns = list(turns)
    while turns:
        tail_tools = 0
        idx = len(turns) - 1
        while idx >= 0:
            tail_role = turns[idx].role
            if tail_role is Role.TOOL:
                tail_tools += 1
                idx -= 1
            elif tail_role is Role.SYSTEM:
                idx -= 1
            else:
                break
        asst_idx = idx
        if asst_idx < 0:
            break
        asst = turns[asst_idx]
        if asst.role is not Role.ASSISTANT or not asst.tool_calls:
            break
        if tail_tools >= len(asst.tool_calls):
            break
        del turns[asst_idx:]
    return turns


# -- Compaction checkpoints ---------------------------------------------------
#
# When a live session compacts, it swaps its in-memory history for a summary but
# leaves the full transcript in storage. A persisted *checkpoint marker* lets a
# reopened session rehydrate that same bounded view instead of the full history
# (which can exceed the model window — e.g. a long session, or one switched to a
# smaller-context model — and deadlock the first send). The marker is one
# ``assistant`` row tagged ``_source="compaction"`` whose content is the summary
# and whose ``meta`` carries ``{"watermark": <id>}``: every conversation row with
# id <= the watermark was folded into the summary; rows after it are still live.

COMPACTION_SOURCE = "compaction"
COMPACTION_SUMMARY_LABEL = "[Conversation summary]"

# ---------------------------------------------------------------------------
# History-search tenancy scope
# ---------------------------------------------------------------------------
# SQL mirror of ``WorkstreamProjectVisibility`` (core.auth) — THE statement of
# who may see a workstream's rows.  A conversation row is hidden from
# ``:scope_user`` only when its workstream links to an EXISTING project whose
# visibility is 'private' and the user is neither the workstream creator, the
# project owner, nor a member.  No project link, a dangling link (project row
# deleted), and non-private projects all stay visible — the trusted-team
# default.  ``COALESCE(w.user_id, '')`` makes a NULL creator hide (not leak):
# plain ``<>`` would go NULL and drop the row from the hide-subquery.  Callers
# never pass an empty ``:scope_user`` (empty scopes to None = unscoped), so
# the COALESCE sentinel cannot collide with a real principal.  Portable across
# SQLite and PostgreSQL; expects the conversations table aliased ``c``.
# ``tests/test_search_history_visibility.py`` pins parity with the Python
# predicate — change either side only in lockstep.

HISTORY_VISIBILITY_SCOPE_SQL = (
    "AND NOT EXISTS ("
    "    SELECT 1 FROM workstreams w"
    "    JOIN projects p ON p.project_id = w.project_id"
    "    WHERE w.ws_id = c.ws_id"
    "      AND p.visibility = 'private'"
    "      AND COALESCE(w.user_id, '') <> :scope_user"
    "      AND p.owner_id <> :scope_user"
    "      AND NOT EXISTS ("
    "          SELECT 1 FROM project_members pm"
    "          WHERE pm.project_id = w.project_id AND pm.user_id = :scope_user"
    "      )"
    ") "
)

# Live-context exclusion for the model-facing recall tool: drop rows of ONE
# workstream (the caller's own) above its compaction checkpoint — those rows
# are the live segment, already in the model's context, and returning them
# wastes result slots on duplicates.  Rows at or below the checkpoint are the
# summarized-away past: exactly what recall exists to re-derive.
# ``:excl_after`` = the checkpoint boundary, or ``-1`` for a never-compacted
# workstream — the whole conversation is live then, so the whole workstream
# is excluded.  Human-facing surfaces (the /history command) deliberately do
# NOT apply this: a person browsing history has no "context" to duplicate.

HISTORY_CONTEXT_EXCLUSION_SQL = "AND NOT (c.ws_id = :excl_ws AND c.id > :excl_after) "


def _is_compaction_marker(row: Any) -> bool:
    """True when a stored row is a compaction checkpoint marker (``_source`` = row index 7)."""
    return len(row) > 7 and row[7] == COMPACTION_SOURCE


def parse_checkpoint_watermark(meta_json: str | None) -> int | None:
    """Parse a compaction marker's ``meta`` JSON into its watermark id.

    The single decoder for the checkpoint boundary — shared by the resume
    slice (:func:`reconstruct_turns_checkpointed` via
    :func:`_compaction_watermark`) and the backends'
    ``get_compaction_checkpoint``.  Returns ``None`` for a marker that
    predates the watermark field or whose meta is malformed (callers fall
    back to safe behavior: resume loads the full transcript, recall excludes
    the whole workstream — never *less* safe than the honest answer)."""
    meta = _source_meta_from_json(meta_json)
    wm = meta.get("watermark") if meta else None
    return wm if isinstance(wm, int) and not isinstance(wm, bool) else None


def _compaction_watermark(row: Any) -> int | None:
    """Read a marker row's checkpoint watermark from its ``meta`` column (row index 10)."""
    return parse_checkpoint_watermark(row[10] if len(row) > 10 else None)


def reconstruct_turns_checkpointed(
    rows: list[Any],
    ws_id: str,
    attachments_by_msg: dict[int, list[dict[str, Any]]] | None = None,
    *,
    checkpoint: bool = True,
) -> list[Turn]:
    """Resume-path reconstruction that honors a persisted compaction checkpoint.

    The full-history twin :func:`reconstruct_turns` rehydrates every row — correct
    for a session that never compacted, but on a compacted one it reloads the
    whole pre-compaction transcript the live session had already summarized away,
    which can overflow the context window on reopen and deadlock the first send.

    If a compaction marker is present, load only ``[summary] + [rows after its
    watermark]`` — the in-memory view the session held when it compacted. The
    summarized prefix and any older markers (id <= watermark) are dropped; the
    full history stays in storage for ``/history``/export/audit. The marker
    reconstructs as an ``assistant`` turn re-tagged ``source="compaction"``
    (``reconstruct_turns`` drops ``_source`` for assistant rows), and a
    synthetic ``[Conversation summary]`` user label — tagged likewise — is
    prepended to match what ``session._compact_messages`` builds in memory
    (and to satisfy the leading-user-turn wire contract).  The tags keep
    provenance-testing consumers (``_find_turn_boundaries``, title gen)
    working identically across a reopen.

    Falls back to the full reconstruction when there is no marker or its watermark
    is absent/corrupt, so every pre-checkpoint session loads exactly as before.

    ``checkpoint=False`` (export/audit): return the FULL transcript as Turns —
    every real row, no watermark slice — but still drop marker rows, since
    :func:`reconstruct_turns` does not filter them and a leaked marker would land
    as a stray ``assistant`` summary turn mid-history. This is the Turn-path twin
    of the marker filter :func:`reconstruct_messages` already applies on the dict
    (display) path; resume passes the default ``True``.
    """
    marker = max((r for r in rows if _is_compaction_marker(r)), key=lambda r: r[0], default=None)
    watermark = _compaction_watermark(marker) if marker is not None else None
    if not checkpoint or marker is None or watermark is None:
        # Full transcript, marker rows dropped — three cases collapse here:
        # ``checkpoint=False`` (export/audit), no marker, and a malformed/legacy
        # watermark.  ``reconstruct_turns`` does not filter markers, so a corrupt
        # marker would otherwise leak its summary as a stray ``assistant`` turn
        # mid-history (and a malformed marker must NOT slice — losing real
        # messages is worse than reloading the whole transcript).
        return reconstruct_turns(
            [r for r in rows if not _is_compaction_marker(r)], ws_id, attachments_by_msg
        )
    # Keep the marker (the summary) plus every non-marker row written after the
    # watermark — the preserved tail and everything since. Reconstruct the two
    # slices separately so the summary leads regardless of row-id ordering (a
    # preserved tail kept verbatim sits at a *lower* id than the marker).
    tail = [r for r in rows if r[0] > watermark and not _is_compaction_marker(r)]
    label = Turn(Role.USER, _content_blocks(COMPACTION_SUMMARY_LABEL, []), source=COMPACTION_SOURCE)
    marker_turns = reconstruct_turns([marker], ws_id, attachments_by_msg)
    for t in marker_turns:
        t.source = COMPACTION_SOURCE
    return [
        label,
        *marker_turns,
        *reconstruct_turns(tail, ws_id, attachments_by_msg),
    ]


def senders_from_user_meta(metas: Iterable[str | None]) -> list[str]:
    """Distinct, stripped sender ids from USER-row ``meta`` JSON blobs.

    A user row's ``meta`` column carries only ``{"sender": ...}`` (the
    role-exclusive routing in :func:`reconstruct_turns`); reuses
    :func:`_source_meta_from_json` — this file's one safe-decode-tolerate-
    garbage helper for this column — so a future change to its tolerance rules
    (e.g. a new error type to swallow) doesn't need a second, divergent
    implementation kept in sync here. Anything unparsable, non-dict, or
    sender-less is skipped so one stray blob cannot poison the participant set.
    Sorted for deterministic output across backends.
    """
    senders: set[str] = set()
    for raw in metas:
        parsed = _source_meta_from_json(raw)
        sender = parsed.get("sender") if parsed else None
        if isinstance(sender, str) and sender.strip():
            senders.add(sender.strip())
    return sorted(senders)
