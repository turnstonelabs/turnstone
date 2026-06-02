"""Shared utilities for storage backends."""

from __future__ import annotations

import base64
import contextlib
import json
import re
from typing import Any

from turnstone.core.attachments import unreadable_placeholder
from turnstone.core.log import get_logger

log = get_logger(__name__)


def _attachment_to_content_part(att: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a stored attachment row into an OpenAI-style content part.

    Returns ``None`` if the attachment's ``kind`` / ``content`` cannot be
    turned into a content part (logged but non-fatal so history still renders).
    """
    kind = att.get("kind")
    raw = att.get("content")
    mime = att.get("mime_type") or "application/octet-stream"
    if kind == "image" and isinstance(raw, bytes):
        b64 = base64.b64encode(raw).decode("ascii")
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
    return None


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
STRUCTURED_MEMORY_MUTABLE = frozenset({"content", "description", "type"})
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
PROMPT_POLICY_MUTABLE = frozenset({"name", "content", "tool_gate", "priority", "enabled"})
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

    Each *row* is an 8- or 9-tuple ``(id, role, content, tool_name,
    tool_call_id, provider_data, tool_calls_json, source [, event_id])``,
    ordered chronologically by row id.  ``source`` is rehydrated as the
    ``_source`` side channel.  (The legacy ``_reminders`` column that used to
    ride here was dropped in migration 060 — operator context lives in
    first-class ``system`` turns now.)  The optional 9th element
    ``event_id`` (migration 059, the per-ws SSE ``Last-Event-ID`` resume
    cursor) is surfaced as the ``_event_id`` side-channel; legacy 9-tuple
    fixtures omit it (handled by the defensive unpack below).

    When ``attachments_by_msg`` is provided, any user row whose id has
    attachments is rebuilt with multipart list content (text +
    image_url/document parts).

    When ``repair`` is True (default) the result is post-processed to
    produce a wire-shape valid for an LLM round-trip: the trailing
    ``assistant(tool_calls)`` turn is dropped if not all tool_call ids
    have a matching tool result (trailing operator-context ``system``
    turns are looked through and stripped with it), and any
    mid-conversation orphaned tool_calls are filled with synthetic
    cancellation results.  Callers
    that consume the messages as LLM context (e.g. ``session.resume``)
    must keep this on.  Callers reading for *display* (the ``/history``
    REST endpoint) should pass ``repair=False`` so the user sees the
    actual partial state — refreshing during tool execution otherwise
    silently drops the trailing turn from the UI.
    """
    messages: list[dict[str, Any]] = []
    for row in rows:
        (
            row_id,
            role,
            content,
            _tool_name,
            tc_id,
            provider_data,
            tool_calls_json,
            source,
        ) = row[:8]
        # ``event_id`` (9th column, migration 059) is the per-ws SSE
        # ring-buffer high-water mark stamped at save time — the
        # ``Last-Event-ID`` resume cursor space.  Surfaced as the
        # ``_event_id`` side-channel so ``make_history_handler`` can
        # compute the resume cursor + locate the in-flight-turn boundary.
        # Defensive length check keeps pre-event_id 8-tuple fixtures valid.
        event_id = row[8] if len(row) > 8 else None

        if role == "user":
            parts: list[dict[str, Any]] = []
            meta: list[dict[str, Any]] = []
            if attachments_by_msg and row_id is not None:
                for att in attachments_by_msg.get(row_id, []):
                    part = _attachment_to_content_part(att)
                    if part is not None:
                        parts.append(part)
                    # Track display-oriented metadata even when a part
                    # itself can't be reconstructed — keeps filenames
                    # available for history replay (e.g. image pills).
                    meta.append(
                        {
                            "kind": str(att.get("kind") or ""),
                            "filename": str(att.get("filename") or ""),
                            "mime_type": str(att.get("mime_type") or ""),
                        }
                    )
            if parts:
                user_content: list[dict[str, Any]] = [{"type": "text", "text": content or ""}]
                user_content.extend(parts)
                umsg: dict[str, Any] = {"role": "user", "content": user_content}
                if meta:
                    umsg["_attachments_meta"] = meta
            else:
                umsg = {"role": "user", "content": content or ""}
            if source:
                umsg["_source"] = str(source)
            if event_id is not None:
                umsg["_event_id"] = int(event_id)
            messages.append(umsg)

        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if provider_data:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    msg["_provider_content"] = json.loads(provider_data)
            if tool_calls_json:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    msg["tool_calls"] = json.loads(tool_calls_json)
            if event_id is not None:
                msg["_event_id"] = int(event_id)
            messages.append(msg)

        elif role == "tool":
            tmsg: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": tc_id or "",
                "content": content or "",
            }
            if event_id is not None:
                tmsg["_event_id"] = int(event_id)
            messages.append(tmsg)

        elif role in ("system", "developer"):
            # First-class operator-context turn (advisory / nudge /
            # interjection — see tool_advisory.make_system_turn), persisted
            # mid-history.  The base system prompt is never stored (it is
            # recomposed by _init_system_messages), so any system/developer
            # row here is operator context; ``_source`` classifies it for the
            # fold-or-keep wire pass and UI replay.
            smsg: dict[str, Any] = {"role": role, "content": content or ""}
            if source:
                smsg["_source"] = str(source)
            if event_id is not None:
                smsg["_event_id"] = int(event_id)
            messages.append(smsg)
        # Genuinely unknown roles are intentionally dropped (no ``else``): the
        # roles above are exhaustive for stored conversations, so an
        # unrecognised role is anomalous and must not be forwarded to a
        # provider.  ``system``/``developer`` are handled above precisely so
        # they are NOT dropped — that silent drop was the bug this fixes.

    if not repair:
        # Both passes below are LLM-context corrections — trailing-turn
        # strip and orphan synthesis.  Display callers want neither; see
        # the reconstruct_messages docstring.
        return messages

    # Repair: strip trailing incomplete tool call turns.  Walk back past
    # trailing tool results AND operator-context system turns (which follow
    # the turn they relate to) to locate the turn's assistant head; if its
    # tool calls are incomplete, strip from the assistant onward — dropping
    # the trailing tools and system turns with it.  Skipping system turns keeps
    # the strip working when a nudge/interjection was appended after an
    # interrupted tool-call turn (otherwise the orphaned assistant survives).
    while messages:
        tail_tools = 0
        idx = len(messages) - 1
        while idx >= 0:
            tail_role = messages[idx].get("role")
            if tail_role == "tool":
                tail_tools += 1
                idx -= 1
            elif tail_role in ("system", "developer"):
                idx -= 1
            else:
                break
        asst_idx = idx
        if asst_idx < 0:
            break
        asst = messages[asst_idx]
        if asst.get("role") != "assistant" or not asst.get("tool_calls"):
            break
        if tail_tools >= len(asst["tool_calls"]):
            break
        del messages[asst_idx:]

    # Repair: synthesize tool results for mid-conversation orphaned tool calls.
    # This happens when a cancel interrupts tool execution — the assistant
    # message with tool_calls is saved to DB but GenerationCancelled prevents
    # tool results from being created.  Both Anthropic (strict) and OpenAI
    # (lenient today, may tighten) benefit from well-formed histories.
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected_ids = [tc.get("id", "") for tc in msg["tool_calls"] if tc.get("id")]
            # Collect the tool-result ids that follow, looking *through* any
            # operator-context system/developer turns interspersed in the block
            # (they follow the turn they relate to and must not be mistaken for
            # the end of the tool-result run — the same skip pass 1 applies).
            # ``insert_at`` tracks the slot right after the last real tool
            # result so synthesized results stay contiguous with the real ones
            # (Anthropic requires every tool_result adjacent to its tool_use);
            # without this, a synthetic spliced after a trailing system turn
            # would split the block.
            j = i + 1
            result_ids: set[str] = set()
            insert_at = i + 1
            while j < len(messages) and messages[j].get("role") in (
                "tool",
                "system",
                "developer",
            ):
                if messages[j].get("role") == "tool":
                    tc_id = messages[j].get("tool_call_id", "")
                    if tc_id:
                        result_ids.add(tc_id)
                    insert_at = j + 1
                j += 1
            # Synthesize results for any missing IDs
            orphaned = [uid for uid in expected_ids if uid not in result_ids]
            if orphaned:
                synthetic = [
                    {
                        "role": "tool",
                        "tool_call_id": uid,
                        "content": "Tool execution was cancelled.",
                        "is_error": True,
                    }
                    for uid in orphaned
                ]
                messages[insert_at:insert_at] = synthetic
            if orphaned:
                i = j + len(orphaned)  # skip past the (now longer) block
            elif j > i + 1:
                i = j  # skip past existing tool block
            else:
                i += 1  # no tools followed; just advance
        else:
            i += 1

    return messages
