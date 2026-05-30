"""Workstream export serializer (issue #613).

Pure transform from a storage handle to an OpenAI-style conversation
envelope (``{"messages": [...]}``).  The storage handle is injected by
the caller — this module NEVER imports a storage singleton — so the
admin CLI and the HTTP handler share one serializer.

A single workstream exports as JSON bytes; a coordinator exported with
``children=True`` exports as a zip bundling the parent at ``<ws_id>.json``
and each child at ``children/<child_id>.json`` (no manifest).

The reasoning lane is surfaced before sanitization: each assistant
message's stored ``_provider_content`` is run through the pure
:func:`extract_reasoning_text_from_provider_content` primitive and, when
non-empty, stamped onto a ``reasoning_content`` field.  That ordering is
load-bearing — :func:`sanitize_messages` strips every ``_``-prefixed key,
so reasoning must be lifted out of ``_provider_content`` first.  The
``reasoning_content`` key follows the chat-completions convention
(vLLM / DeepSeek) and is deliberately distinct from the ``/history`` UI
surface, which stamps the same text onto a ``reasoning`` field
(see ``history_decoration.extract_reasoning_for_history``).
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turnstone.core.history_decoration import (
    extract_reasoning_text_from_provider_content,
)
from turnstone.core.providers._openai_common import sanitize_messages

if TYPE_CHECKING:
    from turnstone.core.storage import StorageBackend

# ``list_workstreams`` defaults to limit=100 and exposes no offset/cursor,
# so the child walk passes an effectively-unbounded ceiling to avoid
# silently truncating a coordinator's children from the export.
_CHILD_EXPORT_LIMIT = 1_000_000


@dataclass(frozen=True)
class ExportResult:
    """The serialized export and the HTTP metadata for serving it."""

    data: bytes
    content_type: str
    filename: str


class WorkstreamNotFoundError(Exception):
    """Raised by :func:`export_workstream` when ``ws_id`` has no row."""


def _attach_reasoning_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``reasoning_content`` on assistant messages from stored reasoning.

    Returns a NEW list.  For each assistant message, the stored
    ``_provider_content`` is dispatched through
    :func:`extract_reasoning_text_from_provider_content`; when it yields
    non-empty text a new dict carrying ``reasoning_content`` is emitted,
    otherwise the message passes through unchanged.  Must run BEFORE
    :func:`sanitize_messages`, which strips ``_provider_content``.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            text = extract_reasoning_text_from_provider_content(msg.get("_provider_content"))
            if text:
                out.append({**msg, "reasoning_content": text})
                continue
        out.append(msg)
    return out


def _build_openai_json(storage: StorageBackend, ws_id: str) -> bytes:
    """Serialize one workstream's history as an OpenAI envelope (JSON bytes)."""
    messages = sanitize_messages(
        _attach_reasoning_content(storage.load_messages(ws_id, repair=True))
    )
    return json.dumps({"messages": messages}, ensure_ascii=False, indent=2).encode("utf-8")


def _list_child_ws_ids(storage: StorageBackend, ws_id: str) -> list[str]:
    """Return the ws_ids of every workstream whose parent is ``ws_id``."""
    return [
        r._mapping["ws_id"]
        for r in storage.list_workstreams(parent_ws_id=ws_id, limit=_CHILD_EXPORT_LIMIT)
    ]


def export_workstream(
    storage: StorageBackend, ws_id: str, *, children: bool = False
) -> ExportResult:
    """Export a workstream as an OpenAI-style conversation envelope.

    With ``children=False`` (default) the result is JSON bytes for the
    single workstream.  With ``children=True`` the result is a zip
    bundling the parent and each child workstream (one JSON entry each).

    Raises :class:`WorkstreamNotFoundError` when ``ws_id`` has no row.
    """
    if storage.get_workstream(ws_id) is None:
        raise WorkstreamNotFoundError(ws_id)

    parent_bytes = _build_openai_json(storage, ws_id)
    if not children:
        return ExportResult(parent_bytes, "application/json", f"{ws_id}.json")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{ws_id}.json", parent_bytes)
        for child_id in _list_child_ws_ids(storage, ws_id):
            zf.writestr(f"children/{child_id}.json", _build_openai_json(storage, child_id))
    return ExportResult(buf.getvalue(), "application/zip", f"{ws_id}.zip")
