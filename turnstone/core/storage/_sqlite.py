"""SQLite storage backend."""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from turnstone.core.storage._notify import Notify, NotifyStream
    from turnstone.core.trajectory import Turn

from turnstone.core.log import get_logger
from turnstone.core.storage._protocol import (
    USER_SCOPED_AUTH_TYPES,
    MCPOAuthPendingState,
    MCPPendingConsentRow,
    MCPUserToken,
    MCPUserTokenMetadataRow,
    OIDCIdentity,
    OIDCPendingState,
    OIDCUserCredential,
)
from turnstone.core.storage._schema import (
    api_tokens,
    audit_events,
    channel_routes,
    channel_users,
    conversations,
    heuristic_rules,
    intent_verdicts,
    mcp_oauth_pending,
    mcp_pending_consent,
    mcp_servers,
    mcp_user_tokens,
    metadata,
    model_definitions,
    oidc_identities,
    oidc_pending_states,
    oidc_user_credentials,
    orgs,
    output_assessments,
    output_guard_patterns,
    personas,
    project_members,
    projects,
    prompt_templates,
    role_permission_overrides,
    roles,
    scheduled_task_runs,
    scheduled_tasks,
    services,
    skill_resources,
    skill_versions,
    structured_memories,
    system_settings,
    tls_account_keys,
    tls_ca,
    tls_certificates,
    tool_policies,
    usage_events,
    user_roles,
    users,
    watches,
    workstream_attachments,
    workstream_config,
    workstream_overrides,
    workstreams,
)
from turnstone.core.storage._schema import (
    prompt_policies as prompt_policies_t,
)
from turnstone.core.storage._utils import (
    COMPACTION_SOURCE as _COMPACTION_SOURCE,
)
from turnstone.core.storage._utils import (
    HEURISTIC_RULE_MUTABLE as _HEURISTIC_RULE_MUTABLE,
)
from turnstone.core.storage._utils import (
    HISTORY_CONTEXT_EXCLUSION_SQL as _HISTORY_EXCL_SQL,
)
from turnstone.core.storage._utils import (
    HISTORY_VISIBILITY_SCOPE_SQL as _HISTORY_SCOPE_SQL,
)
from turnstone.core.storage._utils import (
    LIKE_ESCAPE as _LIKE_ESCAPE,
)
from turnstone.core.storage._utils import (
    MCP_SERVER_MUTABLE as _MCP_SERVER_MUTABLE,
)
from turnstone.core.storage._utils import (
    MODEL_DEFINITION_MUTABLE as _MODEL_DEF_MUTABLE,
)
from turnstone.core.storage._utils import (
    ORG_MUTABLE as _ORG_MUTABLE,
)
from turnstone.core.storage._utils import (
    OUTPUT_GUARD_PATTERN_MUTABLE as _OGP_MUTABLE,
)
from turnstone.core.storage._utils import (
    PERSONA_MUTABLE as _PERSONA_MUTABLE,
)
from turnstone.core.storage._utils import (
    POLICY_MUTABLE as _POLICY_MUTABLE,
)
from turnstone.core.storage._utils import (
    PROJECT_MUTABLE as _PROJECT_MUTABLE,
)
from turnstone.core.storage._utils import (
    PROMPT_POLICY_MUTABLE as _PROMPT_POLICY_MUTABLE,
)
from turnstone.core.storage._utils import (
    ROLE_MUTABLE as _ROLE_MUTABLE,
)
from turnstone.core.storage._utils import (
    SKILL_MUTABLE as _SKILL_MUTABLE,
)
from turnstone.core.storage._utils import (
    VERDICT_MUTABLE as _VERDICT_MUTABLE,
)
from turnstone.core.storage._utils import (
    assert_single_default_persona as _assert_single_default_persona,
)
from turnstone.core.storage._utils import (
    build_attachments_by_msg as _build_attachments_by_msg,
)
from turnstone.core.storage._utils import (
    escape_like as _escape_like,
)
from turnstone.core.storage._utils import (
    find_orphan_conversations,
    parse_checkpoint_watermark,
    prepare_provider_data_for_save,
    purge_orphan_conversations,
    release_attachment_refs,
    sanitize_text,
    senders_from_user_meta,
)
from turnstone.core.storage._utils import (
    normalize_search_terms as _normalize_search_terms,
)
from turnstone.core.storage._utils import (
    parse_attachment_refs as _parse_attachment_refs,
)
from turnstone.core.storage._utils import (
    persona_row_to_dict as _persona_row_to_dict,
)
from turnstone.core.storage._utils import (
    reconstruct_messages as _reconstruct_messages,
)
from turnstone.core.storage._utils import (
    reconstruct_turns_checkpointed as _reconstruct_turns_checkpointed,
)
from turnstone.core.storage._utils import (
    recover_trajectory as _recover_trajectory,
)
from turnstone.core.storage._utils import (
    row_to_dict as _row_to_dict,
)
from turnstone.core.storage._utils import (
    scan_skill_content as _scan_skill_content,
)
from turnstone.core.storage._utils import (
    serialize_persona_fields as _serialize_persona_fields,
)
from turnstone.core.storage._utils import (
    split_perms as _split_perms,
)
from turnstone.core.storage._utils import (
    validate_and_clear_default_persona as _validate_and_clear_default_persona,
)
from turnstone.core.workstream import BULK_CLOSE_STATE_VALUES, WorkstreamKind

log = get_logger(__name__)


def _fts5_query(query: str) -> str:
    """Convert a plain search string into a safe FTS5 query."""
    terms = query.split()
    safe = []
    for t in terms:
        if t:
            safe.append(f'"{t.replace(chr(34), chr(34) + chr(34))}"')
    return " ".join(safe)


# Synthetic-sweep cadence for the SQLite ``listen`` fallback.  SQLite is
# the dev-only path where reactive latency isn't load-bearing — a single
# console process, no cross-process notify semantics to recover from.
# 300 s sits comfortably above the existing per-consumer timers
# (cluster collector's 60 s ``discovery_interval``, any future
# ConfigStore/scheduler reload cadences) so the sweep is a true backstop
# rather than a duplicate tick.  Future consumers that need tighter
# SQLite-mode reactive latency should pass a custom interval through
# :meth:`SQLiteBackend.listen` rather than lowering this default.
_SQLITE_NOTIFY_SWEEP_INTERVAL: float = 300.0


class _SQLiteNotifyStream:
    """SQLite ``listen`` stream — synthetic sweep + in-process fan-out.

    Each poll either drains queued in-process notifies (delivered by a
    same-process :meth:`SQLiteBackend.notify` call) or emits one
    synthetic ``Notify(channel, payload="sweep", pid=0)`` per subscribed
    channel once :attr:`_sweep_interval` has elapsed since the previous
    sweep, whichever happens first.  Consumers handle both shapes the
    same way: re-read the relevant rows on every wake-up.
    """

    def __init__(
        self,
        backend: SQLiteBackend,
        channels: list[str],
        sweep_interval: float,
    ) -> None:
        self._backend = backend
        self._channels = list(channels)
        self._sweep_interval = sweep_interval
        self._queue: queue.Queue[Any] = queue.Queue()
        self._closed = False
        self._last_sweep = time.monotonic()
        if self._channels:
            backend._notify_register(self._channels, self._queue)

    def poll(self, timeout: float) -> list[Notify]:
        from turnstone.core.storage._notify import Notify

        if self._closed:
            return []
        deadline = time.monotonic() + max(0.0, timeout)
        # Emit a synthetic-sweep tick on the first poll where the sweep
        # interval has elapsed.  Single tick per channel per interval —
        # PG-equivalent "one wake-up per change" semantics, not a burst.
        now = time.monotonic()
        if self._channels and now - self._last_sweep >= self._sweep_interval:
            self._last_sweep = now
            for ch in self._channels:
                with contextlib.suppress(Exception):
                    self._queue.put_nowait(Notify(channel=ch, payload="sweep", pid=0))
        out: list[Notify] = []
        try:
            while True:
                if self._closed:
                    break
                if out:
                    # Drain everything already queued without further
                    # blocking — produces "one poll returns the burst"
                    # semantics so the consumer reconciles once per wake.
                    item = self._queue.get_nowait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    item = self._queue.get(timeout=remaining)
                out.append(item)
        except queue.Empty:
            # End-of-drain: the blocking get hit its deadline OR a
            # get_nowait found the queue empty.  Either way we return
            # whatever was already collected.
            pass
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._channels:
            self._backend._notify_unregister(self._channels, self._queue)


class SQLiteBackend:
    """SQLite implementation of the StorageBackend protocol."""

    def __init__(self, path: str, *, create_tables: bool = True) -> None:
        self._path = path
        self._engine = sa.create_engine(
            f"sqlite:///{path}",
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        # Enable WAL mode for better concurrent read/write performance.
        @sa.event.listens_for(self._engine, "connect")
        def _set_wal(dbapi_conn: Any, _rec: Any) -> None:
            try:
                cursor = dbapi_conn.execute("PRAGMA journal_mode=WAL")
                mode = cursor.fetchone()
                cursor.close()
                if mode and mode[0] != "wal":
                    log.warning("SQLite WAL mode not enabled (got %s)", mode[0])
            except Exception:
                log.warning("Failed to set SQLite WAL mode", exc_info=True)

        self._fts5_available = False
        self._db_unavailable = False
        self._db_unavailable_lock = threading.Lock()
        # In-process notify fan-out: channel name -> list of stream queues.
        # SQLite has no cross-process LISTEN/NOTIFY, so notifications are
        # delivered synchronously to any open ``listen`` stream in the same
        # process.  Streams register on open + unregister on close; the
        # synthetic-sweep timer below covers consumers that need a periodic
        # wake regardless of producer activity (matching the PG-side
        # discovery-loop cadence).
        self._notify_lock = threading.Lock()
        self._notify_subs: dict[str, list[queue.Queue[Any]]] = {}
        if create_tables:
            self._init_schema()

    def _init_schema(self) -> None:
        """Create tables and FTS5 index."""
        metadata.create_all(self._engine)
        # Try to set up FTS5 for full-text search
        with self._engine.connect() as conn:
            try:
                fts_exists = conn.execute(
                    sa.text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='conversations_fts'"
                    )
                ).fetchone()
                if not fts_exists:
                    conn.execute(
                        sa.text(
                            "CREATE VIRTUAL TABLE conversations_fts "
                            "USING fts5(content, content=conversations, content_rowid=id)"
                        )
                    )
                    conn.execute(
                        sa.text(
                            "INSERT INTO conversations_fts(conversations_fts) VALUES('rebuild')"
                        )
                    )
                    conn.commit()
                self._fts5_available = True
            except Exception:
                self._fts5_available = False

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sa.engine.Connection]:
        """Acquire a DB connection with clean logging on connectivity errors."""
        from turnstone.core.storage._registry import StorageUnavailableError

        try:
            conn_cm = self._engine.connect()
        except sa.exc.OperationalError as exc:
            with self._db_unavailable_lock:
                if not self._db_unavailable:
                    self._db_unavailable = True
                    log.error("database.unavailable", path=self._path)
            raise StorageUnavailableError(str(exc)) from exc

        with conn_cm as conn:
            with self._db_unavailable_lock:
                if self._db_unavailable:
                    self._db_unavailable = False
                    log.info("database.connection_restored")
            yield conn

    # -- Core conversation operations ------------------------------------------

    def save_message(
        self,
        ws_id: str,
        role: str,
        content: str | None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        provider_data: str | None = None,
        tool_calls: str | None = None,
        source: str | None = None,
        event_id: int | None = None,
        is_error: bool = False,
        producer: str | None = None,
        meta: str | None = None,
    ) -> int:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        content = sanitize_text(content)
        provider_data = prepare_provider_data_for_save(
            role, sanitize_text(provider_data), tool_calls, producer
        )
        source = sanitize_text(source)
        with self._conn() as conn:
            result = conn.execute(
                sa.insert(conversations),
                {
                    "ws_id": ws_id,
                    "timestamp": now,
                    "role": role,
                    "content": content,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "provider_data": provider_data,
                    "tool_calls": tool_calls,
                    "_source": source,
                    "event_id": event_id,
                    "is_error": is_error,
                    "meta": meta,
                },
            )
            if result.lastrowid is None:
                # Should be unreachable under SQLite + autoincrement PKs.
                raise RuntimeError("save_message: lastrowid missing after insert")
            rowid = int(result.lastrowid)
            # FTS5 indexing
            if self._fts5_available and content:
                try:
                    conn.execute(
                        sa.text(
                            "INSERT INTO conversations_fts(rowid, content) VALUES (:rowid, :content)"
                        ),
                        {"rowid": rowid, "content": content},
                    )
                except Exception:
                    self._fts5_available = False
            # Bump workstream updated timestamp
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(updated=now)
            )
            conn.commit()
            return rowid

    def list_message_senders(self, ws_id: str) -> list[str]:
        # DISTINCT on the raw meta blob: a user row's meta carries only
        # {"sender": ...}, so distinct blobs ≈ distinct senders and the JSON
        # parse (shared, backend-neutral) runs on a handful of rows.
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(conversations.c.meta)
                .distinct()
                .where(
                    conversations.c.ws_id == ws_id,
                    conversations.c.role == "user",
                    conversations.c.meta.is_not(None),
                )
            ).fetchall()
        return senders_from_user_meta(meta for (meta,) in rows)

    def save_messages_bulk(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        # Single timestamp for all rows — ordering is preserved by auto-increment id.
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        insert_rows = []
        ws_ids: set[str] = set()
        for row in rows:
            ws_ids.add(row["ws_id"])
            insert_rows.append(
                {
                    "ws_id": row["ws_id"],
                    "timestamp": now,
                    "role": row["role"],
                    "content": sanitize_text(row["content"]),
                    "tool_name": row.get("tool_name"),
                    "tool_call_id": row.get("tool_call_id"),
                    "provider_data": prepare_provider_data_for_save(
                        row["role"],
                        sanitize_text(row.get("provider_data")),
                        row.get("tool_calls"),
                        row.get("producer"),
                    ),
                    "tool_calls": row.get("tool_calls"),
                    "_source": sanitize_text(row.get("source")),
                    "is_error": bool(row.get("is_error", False)),
                    "meta": row.get("meta"),
                }
            )
        with self._conn() as conn:
            conn.execute(sa.insert(conversations), insert_rows)
            for wid in ws_ids:
                conn.execute(
                    sa.update(workstreams).where(workstreams.c.ws_id == wid).values(updated=now)
                )
            # Rebuild FTS5 index so bulk-inserted messages are searchable.
            if self._fts5_available:
                try:
                    conn.execute(
                        sa.text(
                            "INSERT INTO conversations_fts(conversations_fts) VALUES ('rebuild')"
                        )
                    )
                except Exception:
                    self._fts5_available = False
            conn.commit()

    def _conversation_rows(
        self, ws_id: str, limit: int | None
    ) -> tuple[list[tuple[Any, ...]], dict[int, list[dict[str, Any]]] | None]:
        """Fetch a ws's conversation rows + resolved attachment map.

        Shared by :meth:`load_messages` (→ dicts, resolved for display) and
        :meth:`load_message_turns` (→ canonical Turns for resume).  The trailing
        ``attachments`` ref-list column is split off to resolve blobs and is NOT
        part of the positional tuple ``reconstruct_*`` unpacks (id..meta).
        """
        _cols = (
            conversations.c.id,
            conversations.c.role,
            conversations.c.content,
            conversations.c.tool_name,
            conversations.c.tool_call_id,
            conversations.c.provider_data,
            conversations.c.tool_calls,
            conversations.c._source,
            conversations.c.event_id,
            conversations.c.is_error,
            conversations.c.meta,
            conversations.c.attachments,
        )
        with self._conn() as conn:
            if limit is not None and limit > 0:
                # Tail-N: fetch the last `limit` rows via DESC + LIMIT
                # then reverse so the reconstructed output stays in
                # chronological order.  Bounds memory on long histories.
                rows = conn.execute(
                    sa.select(*_cols)
                    .where(conversations.c.ws_id == ws_id)
                    .order_by(conversations.c.id.desc())
                    .limit(limit)
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = conn.execute(
                    sa.select(*_cols)
                    .where(conversations.c.ws_id == ws_id)
                    .order_by(conversations.c.id)
                ).fetchall()

        attachments = self._resolve_row_attachments(rows)
        msg_rows = [tuple(r)[:11] for r in rows]
        return msg_rows, (attachments or None)

    def load_messages(
        self,
        ws_id: str,
        *,
        limit: int | None = None,
        repair: bool = True,
        include_compaction: bool = False,
    ) -> list[dict[str, Any]]:
        msg_rows, attachments = self._conversation_rows(ws_id, limit)
        return _reconstruct_messages(
            msg_rows, ws_id, attachments, repair=repair, include_compaction=include_compaction
        )

    def load_message_turns(self, ws_id: str, *, checkpointed: bool = True) -> list[Turn]:
        """Load the conversation as canonical ``Turn``s (unresolved AttachmentRef).

        The resume path: ``session.messages`` holds the by-reference content;
        bytes are materialized at each output (wire / display), never here.

        Checkpoint-aware (``checkpointed=True``, the resume default): if the
        conversation carries a persisted compaction marker, only ``[summary] +
        [rows after its watermark]`` rehydrate (the bounded view the live session
        held when it compacted) — not the full pre-compaction transcript, which
        can overflow the model window on reopen.  ``checkpointed=False`` returns
        the full transcript (markers dropped) for export/audit consumers that
        must not lose pre-compaction history.
        """
        msg_rows, attachments = self._conversation_rows(ws_id, None)
        return _recover_trajectory(
            _reconstruct_turns_checkpointed(msg_rows, ws_id, attachments, checkpoint=checkpointed)
        )

    def _resolve_row_attachments(self, rows: Sequence[Any]) -> dict[int, list[dict[str, Any]]]:
        """Build the ``reconstruct_messages`` attachment map from row ref-lists.

        Each row's trailing ``attachments`` column (last element) is the
        content-addressed ref-list; collect every referenced id, bulk-fetch
        the blobs in one query, and group them back per row id in ref-list
        order.  No referenced ids → no query.
        """
        attachment_refs: dict[int, list[str]] = {}
        all_ids: set[str] = set()
        for r in rows:
            ids = _parse_attachment_refs(r[11])
            if ids:
                attachment_refs[r[0]] = ids
                all_ids.update(ids)
        if not all_ids:
            return {}
        # Preview-pane blobs (kind='preview', see core.preview.PREVIEW_BLOB_KIND)
        # ride ref-lists only for GC + the serving gate; reconstruction skips
        # them, so don't pull their multi-MB content off disk on every load.
        blobs = self.get_attachments(list(all_ids), exclude_kinds=("preview",))
        rows_by_id = {str(b["attachment_id"]): b for b in blobs}
        return _build_attachments_by_msg(attachment_refs, rows_by_id)

    def get_max_event_id(self, ws_id: str) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(sa.func.max(conversations.c.event_id)).where(
                    conversations.c.ws_id == ws_id
                )
            ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def get_compaction_watermark(self, ws_id: str, preserve_tail: int = 0) -> int | None:
        """Boundary id for a compaction checkpoint: the max conversation ``id``
        among the rows a compaction would summarize.

        With ``preserve_tail=0`` (the auto/overflow path) every current row is
        summarized, so this is the newest real id.  With ``preserve_tail=N`` the
        newest ``N`` rows are kept verbatim, so the boundary is the ``(N+1)``-th
        newest id — counting REAL rows from the newest.  Compaction markers are
        excluded: they are summary artifacts written as new rows but never part
        of the preserved in-memory tail, so counting them would skew the boundary
        and drop real tail rows on resume.  ``None`` when there are no rows.
        """
        with self._conn() as conn:
            row = conn.execute(
                sa.select(conversations.c.id)
                .where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        sa.or_(
                            conversations.c._source.is_(None),
                            conversations.c._source != _COMPACTION_SOURCE,
                        ),
                    )
                )
                .order_by(conversations.c.id.desc())
                .limit(1)
                .offset(max(0, preserve_tail))
            ).fetchone()
        return int(row[0]) if row is not None else None

    def count_messages(self, ws_id: str) -> int:
        """Total conversation rows for ``ws_id`` (compaction markers included)."""
        with self._conn() as conn:
            n = conn.execute(
                sa.select(sa.func.count())
                .select_from(conversations)
                .where(conversations.c.ws_id == ws_id)
            ).scalar()
        return int(n or 0)

    def get_compaction_floor(self, ws_id: str) -> int:
        """Rows that back the latest compaction summary and must survive a
        rewind/retry: every row with ``id <= the latest marker's id`` (the
        summarized prefix plus the marker).  ``0`` when the ws never compacted.

        rewind/retry trim the conversation TAIL, but after a compaction the
        in-memory summary turns no longer map 1:1 to storage rows, so a delete
        keyed on ``len(self.messages)`` would keep the oldest summarized rows and
        drop the marker.  Flooring the delete at this count keeps the summary's
        backing intact.
        """
        with self._conn() as conn:
            marker_id = conn.execute(
                sa.select(sa.func.max(conversations.c.id)).where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        conversations.c._source == _COMPACTION_SOURCE,
                    )
                )
            ).scalar()
            if marker_id is None:
                return 0
            n = conn.execute(
                sa.select(sa.func.count())
                .select_from(conversations)
                .where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        conversations.c.id <= marker_id,
                    )
                )
            ).scalar()
        return int(n or 0)

    def get_compaction_checkpoint(self, ws_id: str) -> int | None:
        """Latest persisted marker's watermark — see the protocol docstring.
        ``None`` = never compacted / malformed meta (whole ws is live)."""
        with self._conn() as conn:
            row = conn.execute(
                sa.select(conversations.c.meta)
                .where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        conversations.c._source == _COMPACTION_SOURCE,
                    )
                )
                .order_by(conversations.c.id.desc())
                .limit(1)
            ).fetchone()
        return parse_checkpoint_watermark(row[0]) if row is not None else None

    def delete_messages_after(self, ws_id: str, keep_count: int) -> int:
        with self._conn() as conn:
            # Find the id of the first row to delete (the row at offset keep_count)
            cutoff_row = conn.execute(
                sa.select(conversations.c.id)
                .where(conversations.c.ws_id == ws_id)
                .order_by(conversations.c.id)
                .limit(1)
                .offset(keep_count)
            ).fetchone()
            if cutoff_row is None:
                return 0  # nothing to delete
            cutoff_id = cutoff_row[0]
            # Refcount GC: read the doomed rows' content-addressed ref-lists,
            # decrement each blob's refcount once per reference, and prune
            # blobs that hit 0 — so a deduped blob still referenced by a kept
            # turn survives.  Replaces the old message_id-cascade delete.
            doomed = conn.execute(
                sa.select(conversations.c.attachments).where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        conversations.c.id >= cutoff_id,
                        conversations.c.attachments.is_not(None),
                    )
                )
            ).fetchall()
            doomed_ids: list[str] = []
            for (refs,) in doomed:
                doomed_ids.extend(_parse_attachment_refs(refs))
            release_attachment_refs(conn, doomed_ids)
            # Remove FTS5 entries first (external content table doesn't auto-sync)
            if self._fts5_available:
                try:
                    conn.execute(
                        sa.text(
                            "DELETE FROM conversations_fts WHERE rowid IN "
                            "(SELECT id FROM conversations "
                            " WHERE ws_id = :ws_id AND id >= :cutoff_id)"
                        ),
                        {"ws_id": ws_id, "cutoff_id": cutoff_id},
                    )
                except Exception:
                    self._fts5_available = False
            result = conn.execute(
                sa.delete(conversations).where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        conversations.c.id >= cutoff_id,
                    )
                )
            )
            conn.commit()
            return result.rowcount

    # -- Workstream management -------------------------------------------------

    def list_workstreams_with_history(
        self,
        limit: int = 20,
        *,
        kind: WorkstreamKind | str | None = None,
        user_id: str | None = None,
        state: str | None = None,
        offset: int = 0,
    ) -> list[Any]:
        # ``kind`` filter applied at the SQL layer so coordinator rows
        # (which persist conversation history the same way interactive
        # workstreams do) don't leak into the interactive UI's "saved
        # workstreams" sidebar.  Default None preserves legacy
        # all-kinds behaviour for callers that want both.
        # ``user_id`` pushes tenancy into SQL so the "saved" endpoint
        # can't accidentally leak another tenant's workstreams.  None
        # = cluster-wide (service callers); empty string is a separate
        # filter value the caller chose deliberately.
        # ``state`` filter — coordinator-saved surface passes "closed"
        # so deleted / currently-active rows don't end up in the saved
        # cards (which would 404 on click or duplicate the active list).
        params: dict[str, Any] = {"limit": limit, "offset": max(0, offset)}
        kind_clause = ""
        user_clause = ""
        state_clause = ""
        if kind is not None:
            params["kind"] = WorkstreamKind(kind).value
            kind_clause = "AND w.kind = :kind "
        if user_id is not None:
            params["user_id"] = user_id
            user_clause = "AND w.user_id = :user_id "
        if state is not None:
            params["state"] = state
            state_clause = "AND w.state = :state "
        with self._conn() as conn:
            return list(
                conn.execute(
                    sa.text(
                        "SELECT w.ws_id, w.alias, w.title, w.name, w.created, w.updated, "
                        "(SELECT COUNT(*) FROM conversations c "
                        " WHERE c.ws_id = w.ws_id), "
                        "w.node_id, w.state, w.kind, "
                        "wcm.value, wcs.value, "
                        "(SELECT COUNT(*) FROM workstreams ch "
                        " WHERE ch.parent_ws_id = w.ws_id), "
                        "(SELECT ue.prompt_tokens FROM usage_events ue "
                        " WHERE ue.ws_id = w.ws_id "
                        " ORDER BY ue.timestamp DESC LIMIT 1), "
                        "md.context_window, w.project_id, w.user_id, w.persona "
                        "FROM workstreams w "
                        "LEFT JOIN workstream_config wcm "
                        "  ON wcm.ws_id = w.ws_id AND wcm.key = 'model_alias' "
                        "LEFT JOIN workstream_config wcs "
                        "  ON wcs.ws_id = w.ws_id AND wcs.key = 'skill' "
                        "LEFT JOIN model_definitions md ON md.alias = wcm.value "
                        "WHERE EXISTS "
                        "  (SELECT 1 FROM conversations c WHERE c.ws_id = w.ws_id) "
                        f"{kind_clause}"
                        f"{user_clause}"
                        f"{state_clause}"
                        "ORDER BY w.updated DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                ).fetchall()
            )

    def prune_workstreams(self, retention_days: int = 90) -> tuple[int, int]:
        orphans = stale = 0
        with self._conn() as conn:
            # 1. Remove workstreams with no messages
            orphan_ids = [
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT ws_id FROM workstreams "
                        "WHERE NOT EXISTS "
                        "  (SELECT 1 FROM conversations c "
                        "   WHERE c.ws_id = workstreams.ws_id)"
                    )
                ).fetchall()
            ]
            if orphan_ids:
                chunk_size = 500
                for i in range(0, len(orphan_ids), chunk_size):
                    chunk = orphan_ids[i : i + chunk_size]
                    placeholders = ",".join([":p" + str(j) for j in range(len(chunk))])
                    params = {f"p{j}": oid for j, oid in enumerate(chunk)}
                    conn.execute(
                        sa.text(f"DELETE FROM workstream_config WHERE ws_id IN ({placeholders})"),
                        params,
                    )
                    result = conn.execute(
                        sa.text(f"DELETE FROM workstreams WHERE ws_id IN ({placeholders})"),
                        params,
                    )
                    orphans += result.rowcount

            # 2. Remove old unnamed workstreams
            if retention_days > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                stale_ids = [
                    row[0]
                    for row in conn.execute(
                        sa.text(
                            "SELECT ws_id FROM workstreams "
                            "WHERE alias IS NULL AND updated < :cutoff"
                        ),
                        {"cutoff": cutoff},
                    ).fetchall()
                ]
                if stale_ids:
                    chunk_size = 500
                    for i in range(0, len(stale_ids), chunk_size):
                        chunk = stale_ids[i : i + chunk_size]
                        placeholders = ",".join([":p" + str(j) for j in range(len(chunk))])
                        params = {f"p{j}": sid for j, sid in enumerate(chunk)}
                        conn.execute(
                            sa.text(
                                f"DELETE FROM workstream_config WHERE ws_id IN ({placeholders})"
                            ),
                            params,
                        )
                        conn.execute(
                            sa.text(f"DELETE FROM conversations WHERE ws_id IN ({placeholders})"),
                            params,
                        )
                        result = conn.execute(
                            sa.text(f"DELETE FROM workstreams WHERE ws_id IN ({placeholders})"),
                            params,
                        )
                        stale += result.rowcount

            conn.commit()
        return (orphans, stale)

    def resolve_workstream(self, alias_or_id: str) -> str | None:
        with self._conn() as conn:
            # 1. Exact alias match
            row = conn.execute(
                sa.select(workstreams.c.ws_id).where(workstreams.c.alias == alias_or_id)
            ).fetchone()
            if row:
                return str(row[0])
            # 2. Exact ws_id match
            row = conn.execute(
                sa.select(workstreams.c.ws_id).where(workstreams.c.ws_id == alias_or_id)
            ).fetchone()
            if row:
                return str(row[0])
            # 3. ws_id prefix match
            rows = conn.execute(
                sa.select(workstreams.c.ws_id).where(workstreams.c.ws_id.like(alias_or_id + "%"))
            ).fetchall()
            if len(rows) == 1:
                return str(rows[0][0])
            return None

    # -- Workstream config -----------------------------------------------------

    def save_workstream_config(self, ws_id: str, config: dict[str, str]) -> None:
        if not config:
            return
        with self._conn() as conn:
            conn.execute(
                sa.text(
                    "INSERT OR REPLACE INTO workstream_config "
                    "(ws_id, key, value) VALUES (:wid, :key, :value)"
                ),
                [{"wid": ws_id, "key": key, "value": value} for key, value in config.items()],
            )
            conn.commit()

    def load_workstream_config(self, ws_id: str) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(workstream_config.c.key, workstream_config.c.value).where(
                    workstream_config.c.ws_id == ws_id
                )
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    # -- Workstream metadata ---------------------------------------------------

    def set_workstream_alias(self, ws_id: str, alias: str) -> bool:
        with self._conn() as conn:
            existing = conn.execute(
                sa.select(workstreams.c.ws_id).where(workstreams.c.alias == alias)
            ).fetchone()
            if existing and existing[0] != ws_id:
                return False
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(alias=alias)
            )
            conn.commit()
            return True

    def get_workstream_display_name(self, ws_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(workstreams.c.alias, workstreams.c.title, workstreams.c.name).where(
                    workstreams.c.ws_id == ws_id
                )
            ).fetchone()
            if row:
                value = row[0] or row[1] or row[2]
                return str(value) if value is not None else None
            return None

    def get_workstream_display_names(self, ws_ids: list[str]) -> dict[str, str | None]:
        if not ws_ids:
            return {}
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    workstreams.c.ws_id,
                    workstreams.c.alias,
                    workstreams.c.title,
                    workstreams.c.name,
                ).where(workstreams.c.ws_id.in_(ws_ids))
            ).fetchall()
        result: dict[str, str | None] = dict.fromkeys(ws_ids)
        for r in rows:
            value = r[1] or r[2] or r[3]
            result[r[0]] = str(value) if value is not None else None
        return result

    def get_workstream_owner(self, ws_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(workstreams.c.user_id).where(workstreams.c.ws_id == ws_id)
            ).fetchone()
        if row is None:
            return None
        # Column is nullable; returning "" vs None lets callers distinguish
        # "ws exists but unowned" from "ws not found".
        return row[0] or ""

    def get_workstream_metadata(self, ws_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    workstreams.c.ws_id,
                    workstreams.c.alias,
                    workstreams.c.title,
                    workstreams.c.name,
                    workstreams.c.node_id,
                    workstreams.c.skill_id,
                    workstreams.c.skill_version,
                ).where(workstreams.c.ws_id == ws_id)
            ).fetchone()
            if row:
                return {
                    "ws_id": row[0],
                    "alias": row[1],
                    "title": row[2],
                    "name": row[3],
                    "node_id": row[4],
                    "skill_id": row[5],
                    "skill_version": row[6],
                }
            return None

    def get_workstream(self, ws_id: str) -> dict[str, Any] | None:
        """Return the full workstreams row as a dict, or None if missing.

        Delegates to ``get_workstreams_batch`` so the 13-column projection
        + row→dict mapping live in one place — a future migration
        adding/renaming a column only has to be applied once per
        backend instead of in two parallel selects that can drift.
        """
        return self.get_workstreams_batch([ws_id]).get(ws_id)

    def update_workstream_title(self, ws_id: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(title=title)
            )
            conn.commit()

    # -- Workstream operations -------------------------------------------------

    def register_workstream(
        self,
        ws_id: str,
        node_id: str | None = None,
        name: str = "",
        state: str = "idle",
        user_id: str | None = None,
        alias: str | None = None,
        title: str | None = None,
        skill_id: str = "",
        skill_version: int = 0,
        kind: WorkstreamKind | str = WorkstreamKind.INTERACTIVE,
        parent_ws_id: str | None = None,
        project_id: str | None = None,
        persona: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        # Kind validation at the storage edge — third of three layers
        # (HTTP handler in server.py returns 400, SessionManager.create
        # raises for in-process callers, here we reject direct storage
        # callers: SDK inserts, restore paths, test doubles).  Each layer
        # targets a different audience; trimming any one of them opens a
        # corresponding path to silently corrupt the NOT NULL column.
        norm_kind = WorkstreamKind(kind).value
        # Normalize empty-string parent to NULL so WHERE parent_ws_id IS NULL
        # filters remain correct.
        norm_parent = parent_ws_id if parent_ws_id else None
        norm_project = project_id if project_id else None
        norm_persona = persona if persona else None
        with self._conn() as conn:
            conn.execute(
                sa.insert(workstreams).prefix_with("OR IGNORE"),
                {
                    "ws_id": ws_id,
                    "node_id": node_id,
                    "user_id": user_id,
                    "alias": alias,
                    "title": title,
                    "name": name,
                    "state": state,
                    "skill_id": skill_id,
                    "skill_version": skill_version,
                    "kind": norm_kind,
                    "parent_ws_id": norm_parent,
                    "project_id": norm_project,
                    "persona": norm_persona,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.ws_id == ws_id)
                .values(state=state, updated=now)
            )
            conn.commit()

    def bulk_close_stale_orphans(
        self,
        kind: WorkstreamKind | str,
        cutoff: str,
        exclude_ws_ids: list[str],
        live_node_ids: list[str] | None = None,
    ) -> list[str]:
        norm_kind = WorkstreamKind(kind).value
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        # SQLite has no RETURNING precedent in this file — do SELECT-then-
        # UPDATE in one transaction, with the SAME WHERE predicates re-applied
        # to the UPDATE.  Re-application defends against a same-process race:
        # ``SessionManager.open()`` calls ``touch_workstream`` between the
        # SELECT and the UPDATE could have bumped a row's ``updated`` past
        # ``cutoff`` (or ``set_state`` could have flipped its state out of
        # the bulk-close set).  Without the re-applied WHERE the UPDATE
        # closes those rows anyway; with it, the UPDATE skips rows that
        # became ineligible after the SELECT and the row stays open.
        # Chunked through ``_in_chunks`` so the ``IN`` clause never exceeds
        # SQLite's bind-parameter limit (default 999) on a large reap.
        candidate_conditions = [
            workstreams.c.kind == norm_kind,
            workstreams.c.state.in_(BULK_CLOSE_STATE_VALUES),
            workstreams.c.updated < cutoff,
        ]
        if live_node_ids is not None and live_node_ids:
            # Protect rows owned by heartbeating services.  NULL node_id is
            # always eligible.  Empty list means "no nodes alive" — every
            # row is unprotected; the absence of this predicate is
            # equivalent to "match all," so we just skip it.
            candidate_conditions.append(
                sa.or_(
                    workstreams.c.node_id.is_(None),
                    ~workstreams.c.node_id.in_(live_node_ids),
                )
            )
        if exclude_ws_ids:
            candidate_conditions.append(~workstreams.c.ws_id.in_(exclude_ws_ids))
        select_stmt = sa.select(workstreams.c.ws_id).where(*candidate_conditions)
        closed: list[str] = []
        # Match the chunk size used by ``prune_workstreams`` (line 453) — keeps
        # ``IN`` clauses well below SQLite's default 999-bind-param limit even
        # on very large reaps.
        chunk_size = 500
        with self._conn() as conn:
            candidate_ids = [row[0] for row in conn.execute(select_stmt)]
            for i in range(0, len(candidate_ids), chunk_size):
                chunk = candidate_ids[i : i + chunk_size]
                # Re-apply the eligibility predicates on the UPDATE so a row
                # that became fresh between the SELECT and the UPDATE is not
                # clobbered.  Then SELECT back by ``state='closed' AND updated=now``
                # to determine which rows actually transitioned this commit —
                # the returned list reflects reality even when re-application
                # filters out some candidates.
                conn.execute(
                    sa.update(workstreams)
                    .where(workstreams.c.ws_id.in_(chunk), *candidate_conditions)
                    .values(state="closed", updated=now)
                )
                actually_closed = [
                    row[0]
                    for row in conn.execute(
                        sa.select(workstreams.c.ws_id).where(
                            workstreams.c.ws_id.in_(chunk),
                            workstreams.c.state == "closed",
                            workstreams.c.updated == now,
                        )
                    )
                ]
                closed.extend(actually_closed)
            conn.commit()
            return closed

    def touch_workstream(self, ws_id: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(updated=now)
            )
            conn.commit()

    def update_workstream_name(self, ws_id: str, name: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.ws_id == ws_id)
                .values(name=name, updated=now)
            )
            conn.commit()

    def delete_workstream(self, ws_id: str) -> bool:
        with self._conn() as conn:
            # Refcount GC over every referenced blob (content-addressed ids are
            # global, so a deduped blob may be shared with another workstream —
            # decrement, don't blanket-delete by ws_id).  Blobs that hit 0 are
            # pruned; any still referenced elsewhere survive.
            referenced = conn.execute(
                sa.select(conversations.c.attachments).where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        conversations.c.attachments.is_not(None),
                    )
                )
            ).fetchall()
            ref_ids: list[str] = []
            for (refs,) in referenced:
                ref_ids.extend(_parse_attachment_refs(refs))
            release_attachment_refs(conn, ref_ids)
            conn.execute(sa.delete(conversations).where(conversations.c.ws_id == ws_id))
            conn.execute(sa.delete(workstream_config).where(workstream_config.c.ws_id == ws_id))
            conn.execute(
                sa.delete(workstream_overrides).where(workstream_overrides.c.ws_id == ws_id)
            )
            # Null-out parent_ws_id on children before dropping the row —
            # otherwise a deleted coordinator leaves orphaned pointers and
            # ``list_workstreams(parent_ws_id=<deleted>)`` keeps returning
            # ghost-parented rows.  Cheaper than a schema-level FK with
            # ON DELETE SET NULL and avoids rewriting the workstreams
            # table on SQLite.
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.parent_ws_id == ws_id)
                .values(parent_ws_id=None)
            )
            result = conn.execute(sa.delete(workstreams).where(workstreams.c.ws_id == ws_id))
            conn.commit()
            return result.rowcount > 0

    def list_orphan_conversations(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return find_orphan_conversations(conn)

    def delete_orphan_conversations(self, ws_ids: list[str]) -> dict[str, int]:
        with self._conn() as conn:
            result = purge_orphan_conversations(conn, ws_ids)
            conn.commit()
            return result

    # -- Workstream attachments (content-addressed, refcounted) ----------------

    def save_attachment(
        self,
        attachment_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        kind: str,
        content: bytes,
        origin: str = "upload",
    ) -> None:
        """Write a content-addressed blob (INSERT-OR-IGNORE) and ``refcount += 1``.

        ``attachment_id`` is the content hash (the caller computes it).  The
        first reference writes the row at ``refcount = 1``; every subsequent
        reference (a re-upload of identical bytes, or a second message
        referencing the same blob) finds the PK present and only bumps the
        count — so a stored blob is always referenced (born at ≥ 1) and dedupes
        across messages / workstreams.  Idempotent on the bytes, never on the
        count.
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            # INSERT-OR-IGNORE the blob, then unconditionally bump the count.
            # Splitting insert (ignore-on-conflict) from the increment keeps
            # the +1 correct whether or not the row already existed.
            stmt = sqlite_insert(workstream_attachments).values(
                attachment_id=attachment_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=size_bytes,
                kind=kind,
                content=content,
                created=now,
                refcount=0,
                origin=origin,
            )
            conn.execute(stmt.on_conflict_do_nothing(index_elements=["attachment_id"]))
            conn.execute(
                sa.update(workstream_attachments)
                .where(workstream_attachments.c.attachment_id == attachment_id)
                .values(refcount=workstream_attachments.c.refcount + 1)
            )
            conn.commit()

    def set_message_attachments(
        self, ws_id: str, message_id: int, attachment_ids: list[str]
    ) -> None:
        """Record a turn's ordered content-addressed ref-list on its row.

        Writes the JSON id-list onto ``conversations.attachments`` for the
        ``(ws_id, message_id)`` row — the sole message->blob link.  Empty
        input is a no-op (the column stays NULL).  Scoped to ``ws_id`` as
        defense-in-depth against a cross-ws message id.
        """
        if not attachment_ids or not message_id:
            return
        with self._conn() as conn:
            conn.execute(
                sa.update(conversations)
                .where(
                    sa.and_(
                        conversations.c.id == message_id,
                        conversations.c.ws_id == ws_id,
                    )
                )
                .values(attachments=json.dumps(list(attachment_ids)))
            )
            conn.commit()

    def get_attachments(
        self, attachment_ids: list[str], exclude_kinds: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        if not attachment_ids:
            return []
        with self._conn() as conn:
            stmt = sa.select(workstream_attachments).where(
                workstream_attachments.c.attachment_id.in_(attachment_ids)
            )
            if exclude_kinds:
                stmt = stmt.where(workstream_attachments.c.kind.notin_(exclude_kinds))
            rows = conn.execute(stmt).fetchall()
            return [dict(r._mapping) for r in rows]

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(workstream_attachments).where(
                    workstream_attachments.c.attachment_id == attachment_id
                )
            ).fetchone()
            return dict(row._mapping) if row else None

    def attachment_referenced_in_ws(self, attachment_id: str, ws_id: str) -> bool:
        """True iff some conversations row in ``ws_id`` references ``attachment_id``.

        The committed-attachment ownership gate: the ``ws_id``/``user_id``
        scope columns are gone, so a ``get_content`` for a committed blob is
        authorised by proving the requester (already gated to own ``ws_id``)
        has a turn in that workstream whose ref-list names the id.  Uses a
        JSON-array substring match on the ``attachments`` column —
        content-addressed ids are 64-char sha256 hex, so a quoted-id substring
        cannot collide with another id.
        """
        needle = f'%"{_escape_like(attachment_id)}"%'
        with self._conn() as conn:
            row = conn.execute(
                sa.select(conversations.c.id)
                .where(
                    sa.and_(
                        conversations.c.ws_id == ws_id,
                        conversations.c.attachments.is_not(None),
                        conversations.c.attachments.like(needle, escape=_LIKE_ESCAPE),
                    )
                )
                .limit(1)
            ).fetchone()
            return row is not None

    def list_workstreams(
        self,
        node_id: str | None = None,
        limit: int = 100,
        *,
        parent_ws_id: str | None = None,
        kind: WorkstreamKind | str | None = None,
        user_id: str | None = None,
    ) -> list[Any]:
        with self._conn() as conn:
            q = (
                sa.select(
                    workstreams.c.ws_id,
                    workstreams.c.node_id,
                    workstreams.c.name,
                    workstreams.c.state,
                    workstreams.c.created,
                    workstreams.c.updated,
                    workstreams.c.kind,
                    workstreams.c.parent_ws_id,
                    workstreams.c.skill_id,
                    workstreams.c.skill_version,
                    workstreams.c.user_id,
                    # Appended after ``user_id`` so positional fallbacks in
                    # consumers (``_coord_children_row`` et al.) that index
                    # up to row[9] stay valid; ``_coordinator_rows`` reads
                    # these by name to surface the persisted display title.
                    workstreams.c.title,
                    workstreams.c.alias,
                    # project_id + persona ride at the tail (read by name) so
                    # the persisted coordinator lane can carry its project
                    # group and persona label.
                    workstreams.c.project_id,
                    workstreams.c.persona,
                )
                .order_by(workstreams.c.updated.desc())
                .limit(limit)
            )
            if node_id is not None:
                q = q.where(workstreams.c.node_id == node_id)
            if parent_ws_id is not None:
                q = q.where(workstreams.c.parent_ws_id == parent_ws_id)
            if kind is not None:
                q = q.where(workstreams.c.kind == WorkstreamKind(kind).value)
            if user_id is not None:
                q = q.where(workstreams.c.user_id == user_id)
            return list(conn.execute(q).fetchall())

    def count_workstreams_by_state(
        self,
        *,
        parent_ws_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, int]:
        """Return ``{state: count}`` for workstreams matching the filters.

        Aggregate query — avoids pulling every row just to group by
        state in Python (#perf-1).  Empty filters mean cluster-wide
        (caller must gate on their own authz).
        """
        with self._conn() as conn:
            q = sa.select(workstreams.c.state, sa.func.count()).group_by(workstreams.c.state)
            if parent_ws_id is not None:
                q = q.where(workstreams.c.parent_ws_id == parent_ws_id)
            if user_id is not None:
                q = q.where(workstreams.c.user_id == user_id)
            rows = conn.execute(q).fetchall()
        return {str(state or ""): int(count) for state, count in rows}

    def count_workstreams_since(
        self,
        since: str,
        *,
        parent_ws_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        """Return the count of workstream rows whose ``created`` is >= ``since``.

        ``since`` is an ISO-8601 string matching the storage format
        ("YYYY-MM-DDTHH:MM:SS", UTC).  Lex compare is safe for the
        same-offset timestamps storage writes (#perf-1).
        """
        with self._conn() as conn:
            q = (
                sa.select(sa.func.count())
                .select_from(workstreams)
                .where(workstreams.c.created >= since)
            )
            if parent_ws_id is not None:
                q = q.where(workstreams.c.parent_ws_id == parent_ws_id)
            if user_id is not None:
                q = q.where(workstreams.c.user_id == user_id)
            row = conn.execute(q).fetchone()
        return int(row[0]) if row else 0

    # -- Conversation search ---------------------------------------------------

    def search_history(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        *,
        user_id: str | None = None,
        exclude_ws_id: str | None = None,
        exclude_after: int | None = None,
    ) -> list[Any]:
        if not query or not query.strip():
            return []
        capped = min(int(limit), 100)
        capped_offset = max(0, int(offset))
        # Project-tenancy scope (see HISTORY_VISIBILITY_SCOPE_SQL) and the
        # live-context exclusion (HISTORY_CONTEXT_EXCLUSION_SQL): applied in
        # SQL, not post-filtered in Python, so limit/offset pagination stays
        # honest — a page never silently shrinks because hidden rows were
        # fetched then dropped.
        scope_sql = _HISTORY_SCOPE_SQL if user_id is not None else ""
        scope_params: dict[str, Any] = {"scope_user": user_id} if user_id is not None else {}
        if exclude_ws_id is not None:
            scope_sql += _HISTORY_EXCL_SQL
            # exclude_after=None → never compacted → the whole ws is live
            # context; ids start at 1, so -1 excludes every row.
            scope_params["excl_ws"] = exclude_ws_id
            scope_params["excl_after"] = -1 if exclude_after is None else exclude_after
        with self._conn() as conn:
            if self._fts5_available:
                return list(
                    conn.execute(
                        sa.text(
                            "SELECT c.timestamp, c.ws_id, c.role, c.content, c.tool_name "
                            "FROM conversations_fts f "
                            "JOIN conversations c ON c.id = f.rowid "
                            "WHERE conversations_fts MATCH :query "
                            # Exclude compaction-checkpoint markers (resume-only
                            # summary artifacts); normal rows store _source NULL,
                            # so the filter must be NULL-safe or it drops everything.
                            "AND (c._source IS NULL OR c._source <> :compaction_source) "
                            + scope_sql
                            + "ORDER BY f.rank ASC LIMIT :limit OFFSET :offset"
                        ),
                        {
                            "query": _fts5_query(query),
                            "compaction_source": _COMPACTION_SOURCE,
                            "limit": capped,
                            "offset": capped_offset,
                            **scope_params,
                        },
                    ).fetchall()
                )
            return list(
                conn.execute(
                    sa.text(
                        "SELECT c.timestamp, c.ws_id, c.role, c.content, c.tool_name "
                        "FROM conversations c WHERE c.content LIKE :pattern ESCAPE '\\' "
                        "AND (c._source IS NULL OR c._source <> :compaction_source) "
                        + scope_sql
                        + "ORDER BY c.timestamp DESC LIMIT :limit OFFSET :offset"
                    ),
                    {
                        "pattern": f"%{_escape_like(query)}%",
                        "compaction_source": _COMPACTION_SOURCE,
                        "limit": capped,
                        "offset": capped_offset,
                        **scope_params,
                    },
                ).fetchall()
            )

    def search_history_recent(self, limit: int = 20, *, user_id: str | None = None) -> list[Any]:
        capped = min(limit, 100)
        scope_sql = _HISTORY_SCOPE_SQL if user_id is not None else ""
        scope_params = {"scope_user": user_id} if user_id is not None else {}
        with self._conn() as conn:
            return list(
                conn.execute(
                    sa.text(
                        "SELECT c.timestamp, c.ws_id, c.role, c.content, c.tool_name "
                        "FROM conversations c "
                        "WHERE (c._source IS NULL OR c._source <> :compaction_source) "
                        + scope_sql
                        + "ORDER BY c.timestamp DESC LIMIT :limit"
                    ),
                    {"limit": capped, "compaction_source": _COMPACTION_SOURCE, **scope_params},
                ).fetchall()
            )

    # -- User identity operations -----------------------------------------------

    def create_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(users).prefix_with("OR IGNORE"),
                {
                    "user_id": user_id,
                    "username": username,
                    "display_name": display_name,
                    "password_hash": password_hash,
                    "created": now,
                },
            )
            conn.commit()

    def create_first_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> bool:
        """Atomically create a user only if no users exist. Returns True if created."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.text(
                    "INSERT INTO users (user_id, username, display_name, password_hash, created) "
                    "SELECT :user_id, :username, :display_name, :password_hash, :created "
                    "WHERE NOT EXISTS (SELECT 1 FROM users)"
                ),
                {
                    "user_id": user_id,
                    "username": username,
                    "display_name": display_name,
                    "password_hash": password_hash,
                    "created": now,
                },
            )
            conn.commit()
            return result.rowcount > 0

    def get_user(self, user_id: str) -> dict[str, str] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    users.c.user_id,
                    users.c.username,
                    users.c.display_name,
                    users.c.password_hash,
                    users.c.created,
                ).where(users.c.user_id == user_id)
            ).fetchone()
            if row:
                return {
                    "user_id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "password_hash": row[3],
                    "created": row[4],
                }
            return None

    def get_user_by_username(self, username: str) -> dict[str, str] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    users.c.user_id,
                    users.c.username,
                    users.c.display_name,
                    users.c.password_hash,
                    users.c.created,
                ).where(users.c.username == username)
            ).fetchone()
            if row:
                return {
                    "user_id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "password_hash": row[3],
                    "created": row[4],
                }
            return None

    def list_users(self) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    users.c.user_id,
                    users.c.username,
                    users.c.display_name,
                    users.c.created,
                ).order_by(users.c.created.desc())
            ).fetchall()
            return [
                {"user_id": r[0], "username": r[1], "display_name": r[2], "created": r[3]}
                for r in rows
            ]

    def count_users(self) -> int:
        with self._conn() as conn:
            n = conn.execute(sa.select(sa.func.count()).select_from(users)).scalar()
            return int(n or 0)

    def find_existing_usernames(self, candidates: list[str]) -> set[str]:
        if not candidates:
            return set()
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(users.c.username).where(users.c.username.in_(candidates))
            ).fetchall()
            return {r[0] for r in rows}

    def delete_user(self, user_id: str) -> bool:

        with self._conn() as conn:
            conn.execute(sa.delete(user_roles).where(user_roles.c.user_id == user_id))
            conn.execute(sa.delete(channel_users).where(channel_users.c.user_id == user_id))
            conn.execute(sa.delete(api_tokens).where(api_tokens.c.user_id == user_id))
            conn.execute(sa.delete(oidc_identities).where(oidc_identities.c.user_id == user_id))
            conn.execute(
                sa.delete(oidc_user_credentials).where(oidc_user_credentials.c.user_id == user_id)
            )
            conn.execute(sa.delete(mcp_user_tokens).where(mcp_user_tokens.c.user_id == user_id))
            conn.execute(sa.delete(mcp_oauth_pending).where(mcp_oauth_pending.c.user_id == user_id))
            result = conn.execute(sa.delete(users).where(users.c.user_id == user_id))
            conn.commit()
            return result.rowcount > 0

    def create_api_token(
        self,
        token_id: str,
        token_hash: str,
        token_prefix: str,
        user_id: str,
        name: str,
        scopes: str,
        expires: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(api_tokens),
                {
                    "token_id": token_id,
                    "token_hash": token_hash,
                    "token_prefix": token_prefix,
                    "user_id": user_id,
                    "name": name,
                    "scopes": scopes,
                    "created": now,
                    "expires": expires,
                },
            )
            conn.commit()

    def get_api_token_by_hash(self, token_hash: str) -> dict[str, str] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    api_tokens.c.token_id,
                    api_tokens.c.token_prefix,
                    api_tokens.c.user_id,
                    api_tokens.c.name,
                    api_tokens.c.scopes,
                    api_tokens.c.created,
                    api_tokens.c.expires,
                ).where(api_tokens.c.token_hash == token_hash)
            ).fetchone()
            if row:
                result: dict[str, str] = {
                    "token_id": row[0],
                    "token_prefix": row[1],
                    "user_id": row[2],
                    "name": row[3],
                    "scopes": row[4],
                    "created": row[5],
                }
                if row[6] is not None:
                    result["expires"] = row[6]
                return result
            return None

    def list_api_tokens(self, user_id: str) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    api_tokens.c.token_id,
                    api_tokens.c.token_prefix,
                    api_tokens.c.user_id,
                    api_tokens.c.name,
                    api_tokens.c.scopes,
                    api_tokens.c.created,
                    api_tokens.c.expires,
                )
                .where(api_tokens.c.user_id == user_id)
                .order_by(api_tokens.c.created.desc())
            ).fetchall()
            result = []
            for r in rows:
                entry: dict[str, str] = {
                    "token_id": r[0],
                    "token_prefix": r[1],
                    "user_id": r[2],
                    "name": r[3],
                    "scopes": r[4],
                    "created": r[5],
                }
                if r[6] is not None:
                    entry["expires"] = r[6]
                result.append(entry)
            return result

    def delete_api_token(self, token_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(sa.delete(api_tokens).where(api_tokens.c.token_id == token_id))
            conn.commit()
            return result.rowcount > 0

    # -- Channel user mapping ---------------------------------------------------

    def create_channel_user(self, channel_type: str, channel_user_id: str, user_id: str) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(channel_users).prefix_with("OR IGNORE"),
                {
                    "channel_type": channel_type,
                    "channel_user_id": channel_user_id,
                    "user_id": user_id,
                    "created": now,
                },
            )
            conn.commit()

    def get_channel_user(self, channel_type: str, channel_user_id: str) -> dict[str, str] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    channel_users.c.channel_type,
                    channel_users.c.channel_user_id,
                    channel_users.c.user_id,
                    channel_users.c.created,
                ).where(
                    (channel_users.c.channel_type == channel_type)
                    & (channel_users.c.channel_user_id == channel_user_id)
                )
            ).fetchone()
            if row:
                return {
                    "channel_type": row[0],
                    "channel_user_id": row[1],
                    "user_id": row[2],
                    "created": row[3],
                }
            return None

    def list_channel_users_by_user(self, user_id: str) -> list[dict[str, str]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    channel_users.c.channel_type,
                    channel_users.c.channel_user_id,
                    channel_users.c.user_id,
                    channel_users.c.created,
                )
                .where(channel_users.c.user_id == user_id)
                .order_by(channel_users.c.created.desc())
            ).fetchall()
            return [
                {
                    "channel_type": r[0],
                    "channel_user_id": r[1],
                    "user_id": r[2],
                    "created": r[3],
                }
                for r in rows
            ]

    def delete_channel_user(self, channel_type: str, channel_user_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(channel_users).where(
                    (channel_users.c.channel_type == channel_type)
                    & (channel_users.c.channel_user_id == channel_user_id)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- Channel routing -------------------------------------------------------

    def create_channel_route(
        self, channel_type: str, channel_id: str, ws_id: str, node_id: str = ""
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(channel_routes).prefix_with("OR IGNORE"),
                {
                    "channel_type": channel_type,
                    "channel_id": channel_id,
                    "ws_id": ws_id,
                    "node_id": node_id,
                    "created": now,
                },
            )
            conn.commit()

    def get_channel_route(self, channel_type: str, channel_id: str) -> dict[str, str] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    channel_routes.c.channel_type,
                    channel_routes.c.channel_id,
                    channel_routes.c.ws_id,
                    channel_routes.c.node_id,
                    channel_routes.c.created,
                ).where(
                    (channel_routes.c.channel_type == channel_type)
                    & (channel_routes.c.channel_id == channel_id)
                )
            ).fetchone()
            if row:
                return {
                    "channel_type": row[0],
                    "channel_id": row[1],
                    "ws_id": row[2],
                    "node_id": row[3],
                    "created": row[4],
                }
            return None

    def get_channel_route_by_ws(self, ws_id: str) -> dict[str, str] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    channel_routes.c.channel_type,
                    channel_routes.c.channel_id,
                    channel_routes.c.ws_id,
                    channel_routes.c.node_id,
                    channel_routes.c.created,
                ).where(channel_routes.c.ws_id == ws_id)
            ).fetchone()
            if row:
                return {
                    "channel_type": row[0],
                    "channel_id": row[1],
                    "ws_id": row[2],
                    "node_id": row[3],
                    "created": row[4],
                }
            return None

    def list_channel_routes_by_type(self, channel_type: str) -> list[dict[str, str]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    channel_routes.c.channel_type,
                    channel_routes.c.channel_id,
                    channel_routes.c.ws_id,
                    channel_routes.c.node_id,
                    channel_routes.c.created,
                )
                .where(channel_routes.c.channel_type == channel_type)
                .order_by(channel_routes.c.created.desc())
            ).fetchall()
            return [
                {
                    "channel_type": r[0],
                    "channel_id": r[1],
                    "ws_id": r[2],
                    "node_id": r[3],
                    "created": r[4],
                }
                for r in rows
            ]

    def delete_channel_route(self, channel_type: str, channel_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(channel_routes).where(
                    (channel_routes.c.channel_type == channel_type)
                    & (channel_routes.c.channel_id == channel_id)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- Scheduled tasks -------------------------------------------------------

    def create_scheduled_task(
        self,
        task_id: str,
        name: str,
        description: str,
        schedule_type: str,
        cron_expr: str,
        at_time: str,
        target_mode: str,
        model: str,
        initial_message: str,
        auto_approve: bool,
        auto_approve_tools: list[str],
        created_by: str,
        next_run: str,
        skill: str = "",
        notify_targets: str = "[]",
        persona: str = "",
        project_id: str = "",
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(scheduled_tasks).prefix_with("OR IGNORE"),
                {
                    "task_id": task_id,
                    "name": name,
                    "description": description,
                    "schedule_type": schedule_type,
                    "cron_expr": cron_expr,
                    "at_time": at_time,
                    "target_mode": target_mode,
                    "model": model,
                    "initial_message": initial_message,
                    "auto_approve": 1 if auto_approve else 0,
                    "auto_approve_tools": ",".join(auto_approve_tools),
                    "skill": skill,
                    "persona": persona,
                    "project_id": project_id,
                    "notify_targets": notify_targets,
                    "enabled": 1,
                    "created_by": created_by,
                    "next_run": next_run,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(scheduled_tasks).where(scheduled_tasks.c.task_id == task_id)
            ).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(scheduled_tasks).order_by(scheduled_tasks.c.created.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    _UPDATABLE_TASK_FIELDS = frozenset(
        {
            "name",
            "description",
            "schedule_type",
            "cron_expr",
            "at_time",
            "target_mode",
            "model",
            "initial_message",
            "auto_approve",
            "auto_approve_tools",
            "skill",
            "persona",
            "project_id",
            "notify_targets",
            "enabled",
            # created_by is only ever set by the update handler adopting an
            # orphaned (pre-fix "") schedule's owner from auth_result — never
            # sourced from the request body, so this is not a spoofing surface.
            "created_by",
            "last_run",
            "next_run",
            "updated",
        }
    )

    def update_scheduled_task(self, task_id: str, **fields: Any) -> bool:

        fields = {k: v for k, v in fields.items() if k in self._UPDATABLE_TASK_FIELDS}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        # Normalize boolean → int for auto_approve
        if "auto_approve" in fields:
            fields["auto_approve"] = 1 if fields["auto_approve"] else 0
        if "auto_approve_tools" in fields and isinstance(fields["auto_approve_tools"], list):
            fields["auto_approve_tools"] = ",".join(fields["auto_approve_tools"])
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        with self._conn() as conn:
            result = conn.execute(
                sa.update(scheduled_tasks)
                .where(scheduled_tasks.c.task_id == task_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_scheduled_task(self, task_id: str) -> bool:

        with self._conn() as conn:
            conn.execute(
                sa.delete(scheduled_task_runs).where(scheduled_task_runs.c.task_id == task_id)
            )
            result = conn.execute(
                sa.delete(scheduled_tasks).where(scheduled_tasks.c.task_id == task_id)
            )
            conn.commit()
            return result.rowcount > 0

    def list_due_tasks(self, now: str) -> list[dict[str, Any]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(scheduled_tasks)
                .where(
                    (scheduled_tasks.c.enabled == 1)
                    & (scheduled_tasks.c.next_run <= now)
                    & (scheduled_tasks.c.next_run != "")
                )
                .order_by(scheduled_tasks.c.next_run)
                .limit(100)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def record_task_run(
        self,
        run_id: str,
        task_id: str,
        node_id: str,
        ws_id: str,
        correlation_id: str,
        started: str,
        status: str,
        error: str,
    ) -> None:

        with self._conn() as conn:
            conn.execute(
                sa.insert(scheduled_task_runs),
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "node_id": node_id,
                    "ws_id": ws_id,
                    "correlation_id": correlation_id,
                    "started": started,
                    "status": status,
                    "error": error,
                },
            )
            conn.commit()

    def list_task_runs(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(scheduled_task_runs)
                .where(scheduled_task_runs.c.task_id == task_id)
                .order_by(scheduled_task_runs.c.started.desc())
                .limit(limit)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def prune_task_runs(self, retention_days: int = 90) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(scheduled_task_runs).where(scheduled_task_runs.c.started < cutoff)
            )
            conn.commit()
            return result.rowcount

    # -- Watches ---------------------------------------------------------------

    def create_watch(
        self,
        watch_id: str,
        ws_id: str,
        node_id: str,
        name: str,
        command: str,
        interval_secs: float,
        stop_on: str | None,
        max_polls: int,
        created_by: str,
        next_poll: str,
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(watches).prefix_with("OR IGNORE"),
                {
                    "watch_id": watch_id,
                    "ws_id": ws_id,
                    "node_id": node_id,
                    "name": name,
                    "command": command,
                    "interval_secs": interval_secs,
                    "stop_on": stop_on,
                    "max_polls": max_polls,
                    "poll_count": 0,
                    "active": 1,
                    "created_by": created_by,
                    "next_poll": next_poll,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_watch(self, watch_id: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(sa.select(watches).where(watches.c.watch_id == watch_id)).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def is_watch_active(self, watch_id: str) -> bool:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(watches.c.active).where(watches.c.watch_id == watch_id)
            ).fetchone()
            if row is None:
                return False
            return bool(row[0])

    def list_watches_for_ws(self, ws_id: str) -> list[dict[str, Any]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(watches)
                .where((watches.c.ws_id == ws_id) & (watches.c.active == 1))
                .order_by(watches.c.created.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def find_watch_by_name(self, ws_id: str, name_or_prefix: str) -> dict[str, Any] | None:

        if not name_or_prefix:
            return None
        like_pattern = _escape_like(name_or_prefix) + "%"
        with self._conn() as conn:
            row = conn.execute(
                sa.select(watches)
                .where(
                    (watches.c.ws_id == ws_id)
                    & (
                        (watches.c.name == name_or_prefix)
                        | watches.c.watch_id.like(like_pattern, escape=_LIKE_ESCAPE)
                    )
                )
                # Active rows win over inactive ones with the same name.
                # _prepare_watch's duplicate-name guard filters active=1,
                # so a model can recreate a name after the previous one
                # auto-cancelled; a cancel-by-name request on the live
                # row must not be shadowed by the older completed row.
                .order_by(watches.c.active.desc(), watches.c.created.desc())
                .limit(1)
            ).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_watches_for_node(self, node_id: str) -> list[dict[str, Any]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(watches)
                .where((watches.c.node_id == node_id) & (watches.c.active == 1))
                .order_by(watches.c.created.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def list_due_watches(self, now: str) -> list[dict[str, Any]]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(watches)
                .where(
                    (watches.c.active == 1)
                    & (watches.c.next_poll <= now)
                    & (watches.c.next_poll != "")
                )
                .order_by(watches.c.next_poll)
                .limit(100)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    _UPDATABLE_WATCH_FIELDS = frozenset(
        {
            "name",
            "poll_count",
            "last_output",
            "last_exit_code",
            "last_poll",
            "next_poll",
            "active",
            "updated",
        }
    )

    def update_watch(self, watch_id: str, **fields: Any) -> bool:

        fields = {k: v for k, v in fields.items() if k in self._UPDATABLE_WATCH_FIELDS}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "active" in fields:
            fields["active"] = 1 if fields["active"] else 0
        with self._conn() as conn:
            result = conn.execute(
                sa.update(watches).where(watches.c.watch_id == watch_id).values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_watch(self, watch_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(sa.delete(watches).where(watches.c.watch_id == watch_id))
            conn.commit()
            return result.rowcount > 0

    def delete_watches_for_ws(self, ws_id: str) -> int:

        with self._conn() as conn:
            result = conn.execute(sa.delete(watches).where(watches.c.ws_id == ws_id))
            conn.commit()
            return result.rowcount

    # -- Service registry ------------------------------------------------------

    def register_service(
        self, service_type: str, service_id: str, url: str, metadata: str = "{}"
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        stmt = sqlite_insert(services).values(
            service_type=service_type,
            service_id=service_id,
            url=url,
            metadata=metadata,
            last_heartbeat=now,
            created=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["service_type", "service_id"],
            set_={"url": url, "metadata": metadata, "last_heartbeat": now},
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def heartbeat_service(self, service_type: str, service_id: str) -> bool:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.update(services)
                .where(
                    (services.c.service_type == service_type)
                    & (services.c.service_id == service_id)
                )
                .values(last_heartbeat=now)
            )
            conn.commit()
            return result.rowcount > 0

    def list_services(self, service_type: str, max_age_seconds: int = 120) -> list[dict[str, str]]:

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(services)
                .where(
                    (services.c.service_type == service_type)
                    & (services.c.last_heartbeat >= cutoff)
                )
                .order_by(services.c.last_heartbeat.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def deregister_service(self, service_type: str, service_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(services).where(
                    (services.c.service_type == service_type)
                    & (services.c.service_id == service_id)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- Cross-process notifications -------------------------------------------

    def notify(self, channel: str, payload: str = "") -> None:
        """In-process broadcast — SQLite has no cross-process channel.

        SQLite deployments are single-process by design (no shared backend
        across nodes); the storage layer delivers to any ``listen`` stream
        open in the same process.  Cross-process consumers wouldn't be
        served regardless — the synthetic-sweep wake-up in :meth:`listen`
        is the parity fallback so consumer code stays backend-agnostic.
        """
        from turnstone.core.storage._notify import Notify

        with self._notify_lock:
            subs = list(self._notify_subs.get(channel, ()))
        for q in subs:
            with contextlib.suppress(Exception):
                q.put(Notify(channel=channel, payload=payload, pid=0))

    @contextlib.contextmanager
    def listen(
        self,
        channels: Iterable[str],
        *,
        sweep_interval: float = _SQLITE_NOTIFY_SWEEP_INTERVAL,
    ) -> Iterator[NotifyStream]:
        """Subscribe to channels — synthetic-sweep + in-process fan-out.

        The returned stream wakes every ``sweep_interval`` seconds with
        one ``Notify(channel, payload="sweep", pid=0)`` per subscribed
        channel; the default (:data:`_SQLITE_NOTIFY_SWEEP_INTERVAL`)
        suits a dev backstop with a 60 s consumer-side timer.  Callers
        that need a tighter cadence (e.g. a future consumer without its
        own polling timer) pass a smaller value here.  In-process
        :meth:`notify` calls deliver immediately on top of the sweep.
        Either path produces a wake-up; consumers reconcile by re-reading
        the relevant rows.

        Channel names are de-duplicated so callers passing the same name
        twice don't double-deliver each notify to a single stream.
        """
        # de-dupe + preserve insertion order — passing the same channel
        # twice would otherwise register the stream's queue against that
        # channel twice and deliver each notify multiple times.
        ch_list = list(dict.fromkeys(str(c) for c in channels if c))
        stream = _SQLiteNotifyStream(self, ch_list, sweep_interval=sweep_interval)
        try:
            yield stream
        finally:
            stream.close()

    def _notify_register(self, channels: list[str], q: queue.Queue[Any]) -> None:
        """Subscribe a stream's queue to in-process notifies on ``channels``."""
        with self._notify_lock:
            for ch in channels:
                self._notify_subs.setdefault(ch, []).append(q)

    def _notify_unregister(self, channels: list[str], q: queue.Queue[Any]) -> None:
        """Detach a stream's queue from in-process notifies on ``channels``."""
        with self._notify_lock:
            for ch in channels:
                subs = self._notify_subs.get(ch)
                if subs is None:
                    continue
                with contextlib.suppress(ValueError):
                    subs.remove(q)
                if not subs:
                    self._notify_subs.pop(ch, None)

    # -- Node metadata ---------------------------------------------------------

    def get_node_metadata(self, node_id: str) -> list[dict[str, Any]]:
        from turnstone.core.storage._schema import node_metadata

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(node_metadata)
                .where(node_metadata.c.node_id == node_id)
                .order_by(node_metadata.c.key)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def get_all_node_metadata(self) -> dict[str, list[dict[str, Any]]]:
        from turnstone.core.storage._schema import node_metadata

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(node_metadata).order_by(node_metadata.c.node_id, node_metadata.c.key)
            ).fetchall()
            result: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                d = dict(r._mapping)
                result.setdefault(d["node_id"], []).append(d)
            return result

    def set_node_metadata(self, node_id: str, key: str, value: str, source: str = "user") -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from turnstone.core.storage._schema import node_metadata

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        stmt = sqlite_insert(node_metadata).values(
            node_id=node_id,
            key=key,
            value=value,
            source=source,
            created=now,
            updated=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["node_id", "key"],
            set_={"value": value, "source": source, "updated": now},
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def set_node_metadata_bulk(self, node_id: str, entries: list[tuple[str, str, str]]) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from turnstone.core.storage._schema import node_metadata

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            for key, value, source in entries:
                stmt = sqlite_insert(node_metadata).values(
                    node_id=node_id,
                    key=key,
                    value=value,
                    source=source,
                    created=now,
                    updated=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["node_id", "key"],
                    set_={"value": value, "source": source, "updated": now},
                )
                conn.execute(stmt)
            conn.commit()

    def delete_node_metadata(self, node_id: str, key: str) -> bool:
        from turnstone.core.storage._schema import node_metadata

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(node_metadata).where(
                    (node_metadata.c.node_id == node_id) & (node_metadata.c.key == key)
                )
            )
            conn.commit()
            return result.rowcount > 0

    def delete_node_metadata_by_source(self, node_id: str, source: str) -> int:
        from turnstone.core.storage._schema import node_metadata

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(node_metadata).where(
                    (node_metadata.c.node_id == node_id) & (node_metadata.c.source == source)
                )
            )
            conn.commit()
            return result.rowcount

    def filter_nodes_by_metadata(self, filters: dict[str, str]) -> set[str]:
        from turnstone.core.storage._schema import node_metadata

        if not filters:
            return set()
        conditions = [
            sa.and_(node_metadata.c.key == k, node_metadata.c.value == v)
            for k, v in filters.items()
        ]
        stmt = (
            sa.select(node_metadata.c.node_id)
            .where(sa.or_(*conditions))
            .group_by(node_metadata.c.node_id)
            .having(sa.func.count() == len(filters))
        )
        with self._conn() as conn:
            rows = conn.execute(stmt).fetchall()
            return {r[0] for r in rows}

    # -- Routing overrides -----------------------------------------------------

    def set_workstream_override(self, ws_id: str, node_id: str, reason: str = "targeted") -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        stmt = sqlite_insert(workstream_overrides).values(
            ws_id=ws_id, node_id=node_id, reason=reason, created=now, updated=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ws_id"],
            set_={"node_id": node_id, "reason": reason, "updated": now},
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def delete_workstream_override(self, ws_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(workstream_overrides).where(workstream_overrides.c.ws_id == ws_id)
            )
            conn.commit()
            return result.rowcount > 0

    def list_workstream_overrides(self) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(workstream_overrides).order_by(workstream_overrides.c.ws_id)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    # -- Roles -----------------------------------------------------------------

    def create_role(
        self,
        role_id: str,
        name: str,
        display_name: str,
        permissions: str,
        builtin: bool,
        org_id: str = "",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(roles).prefix_with("OR IGNORE"),
                {
                    "role_id": role_id,
                    "name": name,
                    "display_name": display_name,
                    "permissions": permissions,
                    "builtin": 1 if builtin else 0,
                    "org_id": org_id,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_role(self, role_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(roles).where(roles.c.role_id == role_id)).fetchone()
            if row:
                return _row_to_dict(row, "builtin")
            return None

    def get_role_by_name(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(roles).where(roles.c.name == name)).fetchone()
            if row:
                return _row_to_dict(row, "builtin")
            return None

    def list_roles(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(roles).order_by(roles.c.name.asc())
            if org_id:
                q = q.where(roles.c.org_id == org_id)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "builtin") for r in rows]

    def update_role(self, role_id: str, **fields: Any) -> bool:
        dropped = set(fields) - _ROLE_MUTABLE
        if dropped:
            log.warning("update_role: ignoring unknown fields: %s", dropped)
        fields = {k: v for k, v in fields.items() if k in _ROLE_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.update(roles).where(roles.c.role_id == role_id).values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_role(self, role_id: str) -> bool:
        with self._conn() as conn:
            conn.execute(sa.delete(user_roles).where(user_roles.c.role_id == role_id))
            # No FK on role_permission_overrides (migration 057 omitted
            # to match the rest of the governance schema), so clean up
            # by hand.  Orphan rows would otherwise apply silently if
            # a role_id were ever reused — deterministic for builtins
            # on schema reseed.
            conn.execute(
                sa.delete(role_permission_overrides).where(
                    role_permission_overrides.c.role_id == role_id
                )
            )
            result = conn.execute(sa.delete(roles).where(roles.c.role_id == role_id))
            conn.commit()
            return result.rowcount > 0

    def assign_role(self, user_id: str, role_id: str, assigned_by: str = "") -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(user_roles).prefix_with("OR IGNORE"),
                {
                    "user_id": user_id,
                    "role_id": role_id,
                    "assigned_by": assigned_by,
                    "created": now,
                },
            )
            conn.commit()

    def unassign_role(self, user_id: str, role_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(user_roles).where(
                    (user_roles.c.user_id == user_id) & (user_roles.c.role_id == role_id)
                )
            )
            conn.commit()
            return result.rowcount > 0

    def list_user_roles(self, user_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    roles.c.role_id,
                    roles.c.name,
                    roles.c.display_name,
                    roles.c.permissions,
                    roles.c.builtin,
                    roles.c.org_id,
                    roles.c.created,
                    roles.c.updated,
                    user_roles.c.assigned_by,
                    user_roles.c.created.label("assignment_created"),
                )
                .select_from(user_roles.join(roles, user_roles.c.role_id == roles.c.role_id))
                .where(user_roles.c.user_id == user_id)
            ).fetchall()
            return [_row_to_dict(r, "builtin") for r in rows]

    def replace_oidc_roles(
        self, user_id: str, desired_role_ids: set[str]
    ) -> tuple[set[str], set[str]]:
        # Double-check pattern: the steady-state OIDC re-login (claims
        # unchanged from the last login) is overwhelmingly the common
        # case, and SQLite's `BEGIN IMMEDIATE` takes the database-wide
        # write lock — serialising every unrelated writer in the
        # process. Acquiring it for a no-op diff is pure waste.
        #
        # Phase 1 reads under the default deferred transaction (no
        # write lock) and bails out cheaply when the diff is empty.
        # Phase 2 only fires when work is needed: it commits the read
        # txn, escalates to `BEGIN IMMEDIATE`, and re-reads + re-diffs
        # under the lock. The re-read is required — between the two
        # reads any concurrent writer (admin-ui assignment, another
        # racing OIDC callback) could have changed the row set, and
        # acting on the optimistic snapshot would clobber that change.
        # The post-lock diff is what we return so callers see the
        # actual transition that hit the table.
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            existing_rows = conn.execute(
                sa.select(user_roles.c.role_id, user_roles.c.assigned_by).where(
                    user_roles.c.user_id == user_id
                )
            ).fetchall()
            current_oidc: set[str] = {r[0] for r in existing_rows if r[1] == "oidc"}
            # Roles assigned by any other source (admin-ui, oidc-default, etc.)
            # are off-limits to OIDC reconciliation per apply_role_mapping's contract.
            blocked: set[str] = {r[0] for r in existing_rows if r[1] != "oidc"}

            effective_desired = desired_role_ids - blocked
            added = effective_desired - current_oidc
            removed = current_oidc - effective_desired
            if not added and not removed:
                # Steady state: claims unchanged from prior login. Skip
                # the write lock entirely — this is the perf win.
                return set(), set()

            # Mutation needed. Release the implicit read txn, acquire
            # the SQLite write lock, and re-read so the diff reflects
            # any state change that landed between the two reads.
            conn.commit()
            conn.execute(sa.text("BEGIN IMMEDIATE"))
            existing_rows = conn.execute(
                sa.select(user_roles.c.role_id, user_roles.c.assigned_by).where(
                    user_roles.c.user_id == user_id
                )
            ).fetchall()
            current_oidc = {r[0] for r in existing_rows if r[1] == "oidc"}
            blocked = {r[0] for r in existing_rows if r[1] != "oidc"}

            effective_desired = desired_role_ids - blocked
            added = effective_desired - current_oidc
            removed = current_oidc - effective_desired

            if added:
                conn.execute(
                    sa.insert(user_roles).prefix_with("OR IGNORE"),
                    [
                        {
                            "user_id": user_id,
                            "role_id": role_id,
                            "assigned_by": "oidc",
                            "created": now,
                        }
                        for role_id in added
                    ],
                )
            if removed:
                conn.execute(
                    sa.delete(user_roles).where(
                        (user_roles.c.user_id == user_id)
                        & (user_roles.c.assigned_by == "oidc")
                        & (user_roles.c.role_id.in_(removed))
                    )
                )
            conn.commit()
            return added, removed

    def get_user_permissions(self, user_id: str) -> set[str]:
        with self._conn() as conn:
            role_rows = conn.execute(
                sa.select(roles.c.role_id, roles.c.permissions, roles.c.builtin)
                .select_from(user_roles.join(roles, user_roles.c.role_id == roles.c.role_id))
                .where(user_roles.c.user_id == user_id)
            ).fetchall()
            if not role_rows:
                return set()
            builtin_role_ids = [r[0] for r in role_rows if r[2]]
            grants: dict[str, set[str]] = {}
            revokes: dict[str, set[str]] = {}
            if builtin_role_ids:
                ov_rows = conn.execute(
                    sa.select(
                        role_permission_overrides.c.role_id,
                        role_permission_overrides.c.permission,
                        role_permission_overrides.c.action,
                    ).where(role_permission_overrides.c.role_id.in_(builtin_role_ids))
                ).fetchall()
                for rid, perm, action in ov_rows:
                    if action == "grant":
                        grants.setdefault(rid, set()).add(perm)
                    elif action == "revoke":
                        revokes.setdefault(rid, set()).add(perm)
            perms: set[str] = set()
            for rid, perms_str, builtin in role_rows:
                role_perms = _split_perms(perms_str)
                if builtin:
                    role_perms = (role_perms | grants.get(rid, set())) - revokes.get(rid, set())
                perms |= role_perms
            return perms

    def users_with_permission(
        self,
        permission: str,
        *,
        exclude_role_id: str | None = None,
    ) -> set[str]:
        with self._conn() as conn:
            q = sa.select(
                user_roles.c.user_id,
                user_roles.c.role_id,
                roles.c.permissions,
                roles.c.builtin,
            ).select_from(user_roles.join(roles, user_roles.c.role_id == roles.c.role_id))
            if exclude_role_id:
                q = q.where(user_roles.c.role_id != exclude_role_id)
            rows = conn.execute(q).fetchall()
            if not rows:
                return set()
            builtin_role_ids = {r[1] for r in rows if r[3]}
            grants: dict[str, set[str]] = {}
            revokes: dict[str, set[str]] = {}
            if builtin_role_ids:
                ov_rows = conn.execute(
                    sa.select(
                        role_permission_overrides.c.role_id,
                        role_permission_overrides.c.permission,
                        role_permission_overrides.c.action,
                    ).where(role_permission_overrides.c.role_id.in_(builtin_role_ids))
                ).fetchall()
                for rid, perm, action in ov_rows:
                    if action == "grant":
                        grants.setdefault(rid, set()).add(perm)
                    elif action == "revoke":
                        revokes.setdefault(rid, set()).add(perm)
            holders: set[str] = set()
            for user_id, role_id, perms_str, builtin in rows:
                eff = _split_perms(perms_str)
                if builtin:
                    eff = (eff | grants.get(role_id, set())) - revokes.get(role_id, set())
                if permission in eff:
                    holders.add(user_id)
            return holders

    def list_role_overrides(self, role_id: str) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(role_permission_overrides)
                .where(role_permission_overrides.c.role_id == role_id)
                .order_by(
                    role_permission_overrides.c.action,
                    role_permission_overrides.c.permission,
                )
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def set_role_overrides(
        self,
        role_id: str,
        grants: set[str],
        revokes: set[str],
        created_by: str = "",
    ) -> None:
        if grants & revokes:
            raise ValueError("grants and revokes must be disjoint")
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.delete(role_permission_overrides).where(
                    role_permission_overrides.c.role_id == role_id
                )
            )
            rows = [
                {
                    "role_id": role_id,
                    "permission": p,
                    "action": "grant",
                    "created": now,
                    "created_by": created_by,
                }
                for p in sorted(grants)
            ] + [
                {
                    "role_id": role_id,
                    "permission": p,
                    "action": "revoke",
                    "created": now,
                    "created_by": created_by,
                }
                for p in sorted(revokes)
            ]
            if rows:
                conn.execute(sa.insert(role_permission_overrides), rows)
            conn.commit()

    def clear_role_overrides(self, role_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                sa.delete(role_permission_overrides).where(
                    role_permission_overrides.c.role_id == role_id
                )
            )
            conn.commit()

    def effective_role_permissions(self, role_id: str) -> dict[str, list[str]]:
        with self._conn() as conn:
            role_row = conn.execute(
                sa.select(roles.c.permissions, roles.c.builtin).where(roles.c.role_id == role_id)
            ).fetchone()
            if role_row is None:
                return {"baseline": [], "grants": [], "revokes": [], "effective": []}
            baseline = _split_perms(role_row[0])
            grants: set[str] = set()
            revokes: set[str] = set()
            if role_row[1]:
                ov_rows = conn.execute(
                    sa.select(
                        role_permission_overrides.c.permission,
                        role_permission_overrides.c.action,
                    ).where(role_permission_overrides.c.role_id == role_id)
                ).fetchall()
                for perm, action in ov_rows:
                    if action == "grant":
                        grants.add(perm)
                    elif action == "revoke":
                        revokes.add(perm)
            effective = (baseline | grants) - revokes
            return {
                "baseline": sorted(baseline),
                "grants": sorted(grants),
                "revokes": sorted(revokes),
                "effective": sorted(effective),
            }

    def effective_role_permissions_bulk(
        self, role_ids: list[str]
    ) -> dict[str, dict[str, list[str]]]:
        if not role_ids:
            return {}
        with self._conn() as conn:
            role_rows = conn.execute(
                sa.select(roles.c.role_id, roles.c.permissions, roles.c.builtin).where(
                    roles.c.role_id.in_(role_ids)
                )
            ).fetchall()
            if not role_rows:
                return {}
            builtin_role_ids = [r[0] for r in role_rows if r[2]]
            grants: dict[str, set[str]] = {}
            revokes: dict[str, set[str]] = {}
            if builtin_role_ids:
                ov_rows = conn.execute(
                    sa.select(
                        role_permission_overrides.c.role_id,
                        role_permission_overrides.c.permission,
                        role_permission_overrides.c.action,
                    ).where(role_permission_overrides.c.role_id.in_(builtin_role_ids))
                ).fetchall()
                for rid, perm, action in ov_rows:
                    if action == "grant":
                        grants.setdefault(rid, set()).add(perm)
                    elif action == "revoke":
                        revokes.setdefault(rid, set()).add(perm)
            out: dict[str, dict[str, list[str]]] = {}
            for rid, perms_str, builtin in role_rows:
                baseline = _split_perms(perms_str)
                role_grants = grants.get(rid, set()) if builtin else set()
                role_revokes = revokes.get(rid, set()) if builtin else set()
                effective = (baseline | role_grants) - role_revokes
                out[rid] = {
                    "baseline": sorted(baseline),
                    "grants": sorted(role_grants),
                    "revokes": sorted(role_revokes),
                    "effective": sorted(effective),
                }
            return out

    # -- Organizations ---------------------------------------------------------

    def create_org(self, org_id: str, name: str, display_name: str, settings: str = "{}") -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(orgs).prefix_with("OR IGNORE"),
                {
                    "org_id": org_id,
                    "name": name,
                    "display_name": display_name,
                    "settings": settings,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(orgs).where(orgs.c.org_id == org_id)).fetchone()
            if row:
                return _row_to_dict(row)
            return None

    def list_orgs(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(sa.select(orgs).order_by(orgs.c.name)).fetchall()
            return [_row_to_dict(r) for r in rows]

    def update_org(self, org_id: str, **fields: Any) -> bool:
        dropped = set(fields) - _ORG_MUTABLE
        if dropped:
            log.warning("update_org: ignoring unknown fields: %s", dropped)
        fields = {k: v for k, v in fields.items() if k in _ORG_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(sa.update(orgs).where(orgs.c.org_id == org_id).values(**fields))
            conn.commit()
            return result.rowcount > 0

    # -- Tool policies ---------------------------------------------------------

    def create_tool_policy(
        self,
        policy_id: str,
        name: str,
        tool_pattern: str,
        action: str,
        priority: int,
        org_id: str = "",
        enabled: bool = True,
        created_by: str = "",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(tool_policies),
                {
                    "policy_id": policy_id,
                    "name": name,
                    "tool_pattern": tool_pattern,
                    "action": action,
                    "priority": priority,
                    "org_id": org_id,
                    "enabled": 1 if enabled else 0,
                    "created_by": created_by,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()
        # Drop both the org-specific slot AND the default ``""`` slot.
        # ``list_tool_policies("")`` returns rows for every org_id (no
        # WHERE filter when org_id is falsy), and the default
        # evaluators (``SessionUIBase.approve_tools`` / ``cli.py``) use
        # ``org_id=""``, so an org-scoped insert that only invalidated
        # the org slot would leave the default slot serving stale data
        # until the TTL window expired.
        from turnstone.core.policy import invalidate_policy_cache

        invalidate_policy_cache(org_id)
        if org_id != "":
            invalidate_policy_cache("")

    def get_tool_policy(self, policy_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(tool_policies).where(tool_policies.c.policy_id == policy_id)
            ).fetchone()
            if row:
                return _row_to_dict(row, "enabled")
            return None

    def list_tool_policies(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(tool_policies).order_by(tool_policies.c.priority.desc())
            if org_id:
                q = q.where(tool_policies.c.org_id == org_id)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "enabled") for r in rows]

    def update_tool_policy(self, policy_id: str, **fields: Any) -> bool:
        dropped = set(fields) - _POLICY_MUTABLE
        if dropped:
            log.warning("update_tool_policy: ignoring unknown fields: %s", dropped)
        fields = {k: v for k, v in fields.items() if k in _POLICY_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "enabled" in fields:
            fields["enabled"] = int(fields["enabled"])
        with self._conn() as conn:
            result = conn.execute(
                sa.update(tool_policies)
                .where(tool_policies.c.policy_id == policy_id)
                .values(**fields)
            )
            conn.commit()
            updated = result.rowcount > 0
        if updated:
            # Invalidate every org slot — the update doesn't expose
            # the row's org_id without a re-read, and policy mutations
            # are admin-rate so a global drop is fine.
            from turnstone.core.policy import invalidate_policy_cache

            invalidate_policy_cache()
        return updated

    def delete_tool_policy(self, policy_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(tool_policies).where(tool_policies.c.policy_id == policy_id)
            )
            conn.commit()
            deleted = result.rowcount > 0
        if deleted:
            from turnstone.core.policy import invalidate_policy_cache

            invalidate_policy_cache()
        return deleted

    # -- Prompt templates ------------------------------------------------------

    def create_prompt_template(
        self,
        template_id: str,
        name: str,
        category: str,
        content: str,
        variables: str = "[]",
        is_default: bool = False,
        org_id: str = "",
        created_by: str = "",
        origin: str = "manual",
        mcp_server: str = "",
        readonly: bool = False,
        description: str = "",
        tags: str = "[]",
        source_url: str = "",
        version: str = "1.0.0",
        author: str = "",
        activation: str = "named",
        token_estimate: int = 0,
        model: str = "",
        auto_approve: bool = False,
        temperature: float | None = None,
        reasoning_effort: str = "",
        max_tokens: int | None = None,
        token_budget: int = 0,
        agent_max_turns: int | None = None,
        notify_on_complete: str = "[]",
        enabled: bool = True,
        allowed_tools: str = "[]",
        skill_license: str = "",
        compatibility: str = "",
        priority: int = 0,
        kind: str = "any",
        paths: str = "[]",
        hidden_from_menu: bool = False,
        arguments: str = "[]",
        argument_hint: str = "",
    ) -> None:
        # Sync is_default from activation when activation is explicitly set
        if activation == "default":
            is_default = True
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

        # Scan skill content for risk signals
        risk_level, scan_report, scan_version = _scan_skill_content(content, allowed_tools)

        from turnstone.core.storage._protocol import StorageConflictError

        with self._conn() as conn:
            try:
                conn.execute(
                    sa.insert(prompt_templates),
                    {
                        "template_id": template_id,
                        "name": name,
                        "category": category,
                        "content": content,
                        "variables": variables,
                        "is_default": 1 if is_default else 0,
                        "org_id": org_id,
                        "created_by": created_by,
                        "origin": origin,
                        "mcp_server": mcp_server,
                        "readonly": 1 if readonly else 0,
                        "description": description,
                        "tags": tags,
                        "source_url": source_url,
                        "version": version,
                        "author": author,
                        "activation": activation,
                        "token_estimate": token_estimate,
                        "allowed_tools": allowed_tools,
                        "license": skill_license,
                        "compatibility": compatibility,
                        "kind": kind,
                        "risk_level": risk_level,
                        "scan_report": scan_report,
                        "scan_version": scan_version,
                        "model": model,
                        "auto_approve": 1 if auto_approve else 0,
                        "temperature": temperature,
                        "reasoning_effort": reasoning_effort,
                        "max_tokens": max_tokens,
                        "token_budget": token_budget,
                        "agent_max_turns": agent_max_turns,
                        "notify_on_complete": notify_on_complete,
                        "enabled": 1 if enabled else 0,
                        "priority": priority,
                        "paths": paths,
                        "hidden_from_menu": 1 if hidden_from_menu else 0,
                        "arguments": arguments,
                        "argument_hint": argument_hint,
                        "created": now,
                        "updated": now,
                    },
                )
            except sa.exc.IntegrityError as exc:
                conn.rollback()
                msg = str(exc.orig) if exc.orig is not None else str(exc)
                raise StorageConflictError(
                    f"prompt_template conflict ({template_id}/{name}): {msg}"
                ) from exc
            conn.commit()

    def get_prompt_template(self, template_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(prompt_templates).where(prompt_templates.c.template_id == template_id)
            ).fetchone()
            if row:
                return _row_to_dict(
                    row, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
            return None

    def get_prompt_template_by_name(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(prompt_templates).where(prompt_templates.c.name == name)
            ).fetchone()
            if row:
                return _row_to_dict(
                    row, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
            return None

    def list_prompt_templates(
        self, org_id: str = "", limit: int = 0, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(prompt_templates).order_by(prompt_templates.c.name)
            if org_id:
                q = q.where(prompt_templates.c.org_id == org_id)
            if offset > 0:
                q = q.offset(offset)
            if limit > 0:
                q = q.limit(limit)
            rows = conn.execute(q).fetchall()
            return [
                _row_to_dict(
                    r, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
                for r in rows
            ]

    def count_prompt_templates(self, org_id: str = "") -> int:
        with self._conn() as conn:
            q = sa.select(sa.func.count()).select_from(prompt_templates)
            if org_id:
                q = q.where(prompt_templates.c.org_id == org_id)
            return conn.execute(q).scalar() or 0

    def list_default_templates(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = (
                sa.select(prompt_templates)
                .where(prompt_templates.c.is_default == 1)
                .where(prompt_templates.c.enabled == 1)
                .order_by(prompt_templates.c.priority, prompt_templates.c.name)
            )
            if org_id:
                q = q.where(prompt_templates.c.org_id == org_id)
            rows = conn.execute(q).fetchall()
            return [
                _row_to_dict(
                    r, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
                for r in rows
            ]

    def list_prompt_templates_by_origin(self, origin: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(prompt_templates)
                .where(prompt_templates.c.origin == origin)
                .order_by(prompt_templates.c.name)
            ).fetchall()
            return [
                _row_to_dict(
                    r, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
                for r in rows
            ]

    def update_prompt_template(self, template_id: str, **fields: Any) -> bool:
        dropped = set(fields) - _SKILL_MUTABLE
        if dropped:
            log.warning("update_prompt_template: ignoring unknown fields: %s", dropped)
        fields = {k: v for k, v in fields.items() if k in _SKILL_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "is_default" in fields:
            fields["is_default"] = int(fields["is_default"])
        # Keep activation and is_default in sync
        if "activation" in fields and "is_default" not in fields:
            fields["is_default"] = 1 if fields["activation"] == "default" else 0
        if "is_default" in fields and "activation" not in fields:
            fields["activation"] = "default" if fields["is_default"] else "named"
        if "auto_approve" in fields:
            fields["auto_approve"] = int(fields["auto_approve"])
        if "enabled" in fields:
            fields["enabled"] = int(fields["enabled"])
        if "hidden_from_menu" in fields:
            fields["hidden_from_menu"] = int(fields["hidden_from_menu"])
        # Re-scan if content or allowed_tools changed
        if "content" in fields or "allowed_tools" in fields:
            content = fields.get("content")
            allowed_tools = fields.get("allowed_tools")
            if content is None or allowed_tools is None:
                existing = self.get_prompt_template(template_id)
                if existing is None:
                    pass  # template not found — skip scan, update will be no-op
                else:
                    if content is None:
                        content = existing.get("content", "")
                    if allowed_tools is None:
                        allowed_tools = existing.get("allowed_tools", "[]")
            if content is not None:
                risk_level, scan_report, scan_version = _scan_skill_content(
                    content, allowed_tools or "[]"
                )
                fields["risk_level"] = risk_level
                fields["scan_report"] = scan_report
                fields["scan_version"] = scan_version
        with self._conn() as conn:
            result = conn.execute(
                sa.update(prompt_templates)
                .where(prompt_templates.c.template_id == template_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def unlock_skill(self, template_id: str, snapshot: str, changed_by: str) -> int | None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                sa.select(prompt_templates.c.template_id).where(
                    prompt_templates.c.template_id == template_id
                )
            ).first()
            if row is None:
                return None
            current_max = conn.execute(
                sa.select(sa.func.coalesce(sa.func.max(skill_versions.c.version), 0)).where(
                    skill_versions.c.skill_id == template_id
                )
            ).scalar()
            next_version = int(current_max or 0) + 1
            conn.execute(
                sa.insert(skill_versions),
                {
                    "skill_id": template_id,
                    "version": next_version,
                    "snapshot": snapshot,
                    "changed_by": changed_by,
                    "created": now,
                },
            )
            conn.execute(
                sa.update(prompt_templates)
                .where(prompt_templates.c.template_id == template_id)
                .values(readonly=0, updated=now)
            )
            conn.commit()
            return next_version

    def delete_prompt_template(self, template_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(prompt_templates).where(prompt_templates.c.template_id == template_id)
            )
            conn.commit()
            return result.rowcount > 0

    def list_skills_by_activation(
        self,
        activation: str,
        *,
        enabled_only: bool = False,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = (
                sa.select(prompt_templates)
                .where(prompt_templates.c.activation == activation)
                .order_by(prompt_templates.c.priority, prompt_templates.c.name)
            )
            if enabled_only:
                q = q.where(prompt_templates.c.enabled == 1)
            if limit > 0:
                q = q.limit(limit)
            rows = conn.execute(q).fetchall()
            return [
                _row_to_dict(
                    r, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
                for r in rows
            ]

    def list_skills_filtered(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        risk_level: str | None = None,
        kinds: list[str] | None = None,
        enabled_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(prompt_templates).order_by(
                prompt_templates.c.priority, prompt_templates.c.name
            )
            if category:
                q = q.where(prompt_templates.c.category == category)
            if risk_level:
                q = q.where(prompt_templates.c.risk_level == risk_level)
            if kinds:
                q = q.where(prompt_templates.c.kind.in_(kinds))
            if enabled_only:
                q = q.where(prompt_templates.c.enabled == 1)
            if tag:
                # True JSON-array containment via SQLite's JSON1
                # ``json_each`` table-valued function (built into SQLite
                # 3.38+; the runtime here is 3.46+).  Replaces the
                # earlier quote-bracketed LIKE pattern, which broke as
                # soon as a tag value contained a ``"`` character (or
                # any value the JSON encoder escaped) and could be
                # subverted by carefully-crafted neighbouring tags.
                # Lateral expansion (vs. SQLite's JSON containment ``@>``
                # operator, which doesn't ship with the JSON1 build) so
                # ``lower(value) = :tag_lower`` runs case-insensitively
                # without case-folding the JSON literal at the call site.
                q = q.where(
                    sa.text(
                        "EXISTS (SELECT 1 FROM json_each(prompt_templates.tags) "
                        "WHERE lower(value) = :tag_lower)"
                    ).bindparams(tag_lower=tag.lower())
                )
            if limit > 0:
                q = q.limit(limit)
            rows = conn.execute(q).fetchall()
            return [
                _row_to_dict(
                    r, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
                for r in rows
            ]

    def get_skill_by_name(self, name: str) -> dict[str, Any] | None:
        return self.get_prompt_template_by_name(name)

    def get_skill_by_source_url(self, source_url: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(prompt_templates).where(prompt_templates.c.source_url == source_url)
            ).fetchone()
            if row:
                return _row_to_dict(
                    row, "is_default", "readonly", "auto_approve", "enabled", "hidden_from_menu"
                )
            return None

    def list_installed_skill_urls(self) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    prompt_templates.c.source_url,
                    prompt_templates.c.template_id,
                    prompt_templates.c.risk_level,
                ).where(prompt_templates.c.source_url != "")
            ).fetchall()
            return [
                {
                    "source_url": r._mapping["source_url"],
                    "template_id": r._mapping["template_id"],
                    "risk_level": r._mapping["risk_level"] or "",
                }
                for r in rows
            ]

    # -- Skill resources -------------------------------------------------------

    def create_skill_resource(
        self,
        resource_id: str,
        skill_id: str,
        path: str,
        content: str,
        content_type: str = "text/plain",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(skill_resources),
                {
                    "resource_id": resource_id,
                    "skill_id": skill_id,
                    "path": path,
                    "content": content,
                    "content_type": content_type,
                    "created": now,
                },
            )
            conn.commit()

    def list_skill_resources(self, skill_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(skill_resources)
                .where(skill_resources.c.skill_id == skill_id)
                .order_by(skill_resources.c.path)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def get_skill_resource(self, skill_id: str, path: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(skill_resources)
                .where(skill_resources.c.skill_id == skill_id)
                .where(skill_resources.c.path == path)
            ).fetchone()
            if row:
                return dict(row._mapping)
            return None

    def delete_skill_resources(self, skill_id: str) -> int:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(skill_resources).where(skill_resources.c.skill_id == skill_id)
            )
            conn.commit()
            return result.rowcount

    def delete_skill_resource_by_path(self, skill_id: str, path: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(skill_resources).where(
                    sa.and_(
                        skill_resources.c.skill_id == skill_id,
                        skill_resources.c.path == path,
                    )
                )
            )
            conn.commit()
            return result.rowcount > 0

    def count_skill_resources_bulk(self, skill_ids: list[str]) -> dict[str, int]:
        if not skill_ids:
            return {}
        result: dict[str, int] = {}
        # Chunk to stay under SQLite's max variable limit (999)
        chunk_size = 900
        with self._conn() as conn:
            for i in range(0, len(skill_ids), chunk_size):
                chunk = skill_ids[i : i + chunk_size]
                rows = conn.execute(
                    sa.select(
                        skill_resources.c.skill_id,
                        sa.func.count().label("cnt"),
                    )
                    .where(skill_resources.c.skill_id.in_(chunk))
                    .group_by(skill_resources.c.skill_id)
                ).fetchall()
                for r in rows:
                    result[r[0]] = r[1]
        return result

    # -- Skill versions --------------------------------------------------------

    def create_skill_version(
        self,
        skill_id: str,
        version: int,
        snapshot: str,
        changed_by: str = "",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(skill_versions),
                {
                    "skill_id": skill_id,
                    "version": version,
                    "snapshot": snapshot,
                    "changed_by": changed_by,
                    "created": now,
                },
            )
            conn.commit()

    def list_skill_versions(self, skill_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(skill_versions)
                .where(skill_versions.c.skill_id == skill_id)
                .order_by(skill_versions.c.version.desc())
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def count_skill_versions(self, skill_id: str) -> int:
        """Return the count of skill-version rows for ``skill_id``.

        Hot path on coordinator create (resolving the next version to
        persist) — a COUNT query avoids pulling every full version row
        just to take the length (#perf-2).
        """
        with self._conn() as conn:
            row = conn.execute(
                sa.select(sa.func.count())
                .select_from(skill_versions)
                .where(skill_versions.c.skill_id == skill_id)
            ).fetchone()
        return int(row[0]) if row else 0

    def delete_skill_versions(self, skill_id: str) -> int:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(skill_versions).where(skill_versions.c.skill_id == skill_id)
            )
            conn.commit()
            return result.rowcount

    # -- Usage events ----------------------------------------------------------

    def record_usage_event(
        self,
        event_id: str,
        user_id: str = "",
        ws_id: str = "",
        node_id: str = "",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        tool_calls_count: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(usage_events),
                {
                    "event_id": event_id,
                    "timestamp": now,
                    "user_id": user_id,
                    "ws_id": ws_id,
                    "node_id": node_id,
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "tool_calls_count": tool_calls_count,
                    "cache_creation_tokens": cache_creation_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "created": now,
                },
            )
            conn.commit()

    def query_usage(
        self,
        since: str,
        until: str = "",
        user_id: str = "",
        model: str = "",
        group_by: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["timestamp >= :since"]
        params: dict[str, Any] = {"since": since}
        if until:
            clauses.append("timestamp <= :until")
            params["until"] = until
        if user_id:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if model:
            clauses.append("model = :model")
            params["model"] = model
        where = " AND ".join(clauses)

        if group_by == "day":
            key_expr = "substr(timestamp, 1, 10)"
        elif group_by == "hour":
            key_expr = "substr(timestamp, 1, 13)"
        elif group_by == "model":
            key_expr = "model"
        elif group_by == "user":
            key_expr = "user_id"
        else:
            # No grouping — single summary row
            sql = (
                f"SELECT SUM(prompt_tokens), SUM(completion_tokens), "
                f"SUM(tool_calls_count), SUM(cache_creation_tokens), "
                f"SUM(cache_read_tokens) FROM usage_events WHERE {where}"
            )
            with self._conn() as conn:
                row = conn.execute(sa.text(sql), params).fetchone()
                if row:
                    return [
                        {
                            "prompt_tokens": row[0] or 0,
                            "completion_tokens": row[1] or 0,
                            "tool_calls_count": row[2] or 0,
                            "cache_creation_tokens": row[3] or 0,
                            "cache_read_tokens": row[4] or 0,
                        }
                    ]
                return [
                    {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "tool_calls_count": 0,
                        "cache_creation_tokens": 0,
                        "cache_read_tokens": 0,
                    }
                ]

        sql = (
            f"SELECT {key_expr} AS key, SUM(prompt_tokens), SUM(completion_tokens), "
            f"SUM(tool_calls_count), SUM(cache_creation_tokens), "
            f"SUM(cache_read_tokens) FROM usage_events WHERE {where} "
            f"GROUP BY {key_expr} ORDER BY key ASC"
        )
        with self._conn() as conn:
            rows = conn.execute(sa.text(sql), params).fetchall()
            return [
                {
                    "key": r[0],
                    "prompt_tokens": r[1] or 0,
                    "completion_tokens": r[2] or 0,
                    "tool_calls_count": r[3] or 0,
                    "cache_creation_tokens": r[4] or 0,
                    "cache_read_tokens": r[5] or 0,
                }
                for r in rows
            ]

    def prune_usage_events(self, retention_days: int = 90) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(sa.delete(usage_events).where(usage_events.c.timestamp < cutoff))
            conn.commit()
            return result.rowcount

    def sum_workstream_tokens(self, ws_id: str) -> int:
        if not ws_id:
            return 0
        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    sa.func.coalesce(
                        sa.func.sum(
                            usage_events.c.prompt_tokens + usage_events.c.completion_tokens
                        ),
                        0,
                    )
                ).where(usage_events.c.ws_id == ws_id)
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def sum_workstream_tokens_batch(self, ws_ids: list[str]) -> dict[str, int]:
        if not ws_ids:
            return {}
        # Drop empty / non-string ids defensively — same shape the caller
        # already enforces, but a missing guard would land an empty
        # parameter in the IN clause and quietly skew the result.
        clean = [w for w in ws_ids if isinstance(w, str) and w]
        out: dict[str, int] = {w: 0 for w in clean}
        if not clean:
            return out
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    usage_events.c.ws_id,
                    sa.func.sum(usage_events.c.prompt_tokens + usage_events.c.completion_tokens),
                )
                .where(usage_events.c.ws_id.in_(clean))
                .group_by(usage_events.c.ws_id)
            ).fetchall()
        for r in rows:
            if r[0] is not None and r[1] is not None:
                out[r[0]] = int(r[1])
        return out

    def get_workstreams_batch(self, ws_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        if not ws_ids:
            return {}
        clean = [w for w in ws_ids if isinstance(w, str) and w]
        out: dict[str, dict[str, Any] | None] = {w: None for w in clean}
        if not clean:
            return out
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    workstreams.c.ws_id,
                    workstreams.c.node_id,
                    workstreams.c.user_id,
                    workstreams.c.alias,
                    workstreams.c.title,
                    workstreams.c.name,
                    workstreams.c.state,
                    workstreams.c.skill_id,
                    workstreams.c.skill_version,
                    workstreams.c.kind,
                    workstreams.c.parent_ws_id,
                    workstreams.c.created,
                    workstreams.c.updated,
                    workstreams.c.project_id,
                    workstreams.c.persona,
                ).where(workstreams.c.ws_id.in_(clean))
            ).fetchall()
        for r in rows:
            out[r[0]] = {
                "ws_id": r[0],
                "node_id": r[1],
                "user_id": r[2],
                "alias": r[3],
                "title": r[4],
                "name": r[5],
                "state": r[6],
                "skill_id": r[7],
                "skill_version": r[8],
                "kind": r[9],
                "parent_ws_id": r[10],
                "created": r[11],
                "updated": r[12],
                "project_id": r[13],
                "persona": r[14],
            }
        return out

    # -- Audit events ----------------------------------------------------------

    def record_audit_event(
        self,
        event_id: str,
        user_id: str = "",
        action: str = "",
        resource_type: str = "",
        resource_id: str = "",
        detail: str = "{}",
        ip_address: str = "",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(audit_events),
                {
                    "event_id": event_id,
                    "timestamp": now,
                    "user_id": user_id,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "detail": detail,
                    "ip_address": ip_address,
                    "created": now,
                },
            )
            conn.commit()

    def list_audit_events(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
        resource_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(
                audit_events.c.event_id,
                audit_events.c.timestamp,
                audit_events.c.user_id,
                audit_events.c.action,
                audit_events.c.resource_type,
                audit_events.c.resource_id,
                audit_events.c.detail,
                audit_events.c.ip_address,
                audit_events.c.created,
            ).order_by(audit_events.c.timestamp.desc(), audit_events.c.event_id.desc())
            if action:
                q = q.where(audit_events.c.action == action)
            if user_id:
                q = q.where(audit_events.c.user_id == user_id)
            if since:
                q = q.where(audit_events.c.timestamp >= since)
            if until:
                q = q.where(audit_events.c.timestamp <= until)
            if resource_id:
                q = q.where(audit_events.c.resource_id == resource_id)
            q = q.limit(limit).offset(offset)
            rows = conn.execute(q).fetchall()
            return [
                {
                    "event_id": r[0],
                    "timestamp": r[1],
                    "user_id": r[2],
                    "action": r[3],
                    "resource_type": r[4],
                    "resource_id": r[5],
                    "detail": r[6],
                    "ip_address": r[7],
                    "created": r[8],
                }
                for r in rows
            ]

    def count_audit_events(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
    ) -> int:
        with self._conn() as conn:
            q = sa.select(sa.func.count()).select_from(audit_events)
            if action:
                q = q.where(audit_events.c.action == action)
            if user_id:
                q = q.where(audit_events.c.user_id == user_id)
            if since:
                q = q.where(audit_events.c.timestamp >= since)
            if until:
                q = q.where(audit_events.c.timestamp <= until)
            row = conn.execute(q).fetchone()
            return row[0] if row else 0

    def prune_audit_events(self, retention_days: int = 365) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(sa.delete(audit_events).where(audit_events.c.timestamp < cutoff))
            conn.commit()
            return result.rowcount

    # -- Intent verdicts -------------------------------------------------------

    def create_intent_verdict(
        self,
        verdict_id: str,
        ws_id: str,
        call_id: str,
        func_name: str,
        func_args: str,
        intent_summary: str,
        risk_level: str,
        confidence: float,
        recommendation: str,
        reasoning: str,
        evidence: str,
        tier: str,
        judge_model: str,
        latency_ms: int,
        user_decision: str = "pending",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(intent_verdicts),
                {
                    "verdict_id": verdict_id,
                    "ws_id": ws_id,
                    "call_id": call_id,
                    "func_name": func_name,
                    "func_args": func_args,
                    "intent_summary": intent_summary,
                    "risk_level": risk_level,
                    "confidence": confidence,
                    "recommendation": recommendation,
                    "reasoning": reasoning,
                    "evidence": evidence,
                    "tier": tier,
                    "judge_model": judge_model,
                    "latency_ms": latency_ms,
                    "user_decision": user_decision,
                    "created": now,
                },
            )
            conn.commit()

    def upsert_intent_verdict(
        self,
        verdict_id: str,
        ws_id: str,
        call_id: str,
        func_name: str,
        func_args: str,
        intent_summary: str,
        risk_level: str,
        confidence: float,
        recommendation: str,
        reasoning: str,
        evidence: str,
        tier: str,
        judge_model: str,
        latency_ms: int,
        user_decision: str = "pending",
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        stmt = sqlite_insert(intent_verdicts).values(
            verdict_id=verdict_id,
            ws_id=ws_id,
            call_id=call_id,
            func_name=func_name,
            func_args=func_args,
            intent_summary=intent_summary,
            risk_level=risk_level,
            confidence=confidence,
            recommendation=recommendation,
            reasoning=reasoning,
            evidence=evidence,
            tier=tier,
            judge_model=judge_model,
            latency_ms=latency_ms,
            user_decision=user_decision,
            created=now,
        )
        # On verdict_id conflict, update only the three fields that
        # genuinely change between heuristic and llm_fallback.  See the
        # protocol docstring for the full exclusion rationale —
        # ``user_decision`` exclusion in particular is load-bearing.
        stmt = stmt.on_conflict_do_update(
            index_elements=["verdict_id"],
            set_={
                "tier": tier,
                "reasoning": reasoning,
                "judge_model": judge_model,
            },
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def create_intent_verdicts_bulk(self, verdicts: list[dict[str, Any]]) -> None:
        # ON CONFLICT DO NOTHING — see the protocol docstring for the
        # daemon-races-the-bulk-write rationale.
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        if not verdicts:
            return
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        rows = [
            {
                "verdict_id": v.get("verdict_id", ""),
                "ws_id": v.get("ws_id", ""),
                "call_id": v.get("call_id", ""),
                "func_name": v.get("func_name", ""),
                "func_args": v.get("func_args", ""),
                "intent_summary": v.get("intent_summary", ""),
                "risk_level": v.get("risk_level", "medium"),
                "confidence": v.get("confidence", 0.5),
                "recommendation": v.get("recommendation", "review"),
                "reasoning": v.get("reasoning", ""),
                "evidence": v.get("evidence", ""),
                "tier": v.get("tier", "heuristic"),
                "judge_model": v.get("judge_model", ""),
                "latency_ms": v.get("latency_ms", 0),
                "user_decision": v.get("user_decision", "pending"),
                "created": now,
            }
            for v in verdicts
        ]
        with self._conn() as conn:
            conn.execute(
                sqlite_insert(intent_verdicts).on_conflict_do_nothing(
                    index_elements=["verdict_id"]
                ),
                rows,
            )
            conn.commit()

    def get_intent_verdict(self, verdict_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(intent_verdicts).where(intent_verdicts.c.verdict_id == verdict_id)
            ).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_intent_verdicts(
        self,
        ws_id: str = "",
        since: str = "",
        until: str = "",
        risk_level: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(intent_verdicts).order_by(
                intent_verdicts.c.created.desc(), intent_verdicts.c.verdict_id.desc()
            )
            if ws_id:
                q = q.where(intent_verdicts.c.ws_id == ws_id)
            if since:
                q = q.where(intent_verdicts.c.created >= since)
            if until:
                q = q.where(intent_verdicts.c.created <= until)
            if risk_level:
                q = q.where(intent_verdicts.c.risk_level == risk_level)
            q = q.limit(limit).offset(offset)
            rows = conn.execute(q).fetchall()
            return [dict(r._mapping) for r in rows]

    def update_intent_verdict(self, verdict_id: str, **fields: Any) -> bool:
        fields = {k: v for k, v in fields.items() if k in _VERDICT_MUTABLE}
        if not fields:
            return False
        with self._conn() as conn:
            result = conn.execute(
                sa.update(intent_verdicts)
                .where(intent_verdicts.c.verdict_id == verdict_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def count_intent_verdicts(
        self,
        ws_id: str = "",
        since: str = "",
        until: str = "",
        risk_level: str = "",
    ) -> int:
        with self._conn() as conn:
            q = sa.select(sa.func.count()).select_from(intent_verdicts)
            if ws_id:
                q = q.where(intent_verdicts.c.ws_id == ws_id)
            if since:
                q = q.where(intent_verdicts.c.created >= since)
            if until:
                q = q.where(intent_verdicts.c.created <= until)
            if risk_level:
                q = q.where(intent_verdicts.c.risk_level == risk_level)
            row = conn.execute(q).fetchone()
            return row[0] if row else 0

    # -- Output assessments ----------------------------------------------------

    def record_output_assessment(
        self,
        assessment_id: str,
        ws_id: str,
        call_id: str,
        func_name: str,
        flags: str,
        risk_level: str,
        annotations: str,
        output_length: int,
        redacted: bool,
        *,
        tier: str = "heuristic",
        reasoning: str = "",
        judge_model: str = "",
        latency_ms: int = 0,
        confidence: float = 0.0,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(output_assessments),
                {
                    "assessment_id": assessment_id,
                    "ws_id": ws_id,
                    "call_id": call_id,
                    "func_name": func_name,
                    "flags": flags,
                    "risk_level": risk_level,
                    "annotations": annotations,
                    "output_length": output_length,
                    "redacted": int(redacted),
                    "created": now,
                    "tier": tier,
                    "reasoning": reasoning,
                    "judge_model": judge_model,
                    "latency_ms": latency_ms,
                    "confidence": confidence,
                },
            )
            conn.commit()

    def list_output_assessments(
        self,
        ws_id: str = "",
        risk_level: str = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            # ``created`` is second-resolution, so the heuristic and llm rows
            # for the same call_id (written within ms of each other) commonly
            # tie.  The ``tier`` tie-breaker encodes the design intent — LLM
            # wins when it ran — so downstream consumers like history
            # decoration see the acted verdict first on identical timestamps.
            q = sa.select(output_assessments).order_by(
                output_assessments.c.created.desc(),
                sa.case((output_assessments.c.tier == "llm", 0), else_=1),
                output_assessments.c.assessment_id.desc(),
            )
            if ws_id:
                q = q.where(output_assessments.c.ws_id == ws_id)
            if risk_level:
                q = q.where(output_assessments.c.risk_level == risk_level)
            if since:
                q = q.where(output_assessments.c.created >= since)
            if until:
                q = q.where(output_assessments.c.created <= until)
            q = q.limit(limit).offset(offset)
            rows = conn.execute(q).fetchall()
            return [dict(r._mapping) for r in rows]

    def count_output_assessments(
        self,
        ws_id: str = "",
        risk_level: str = "",
        since: str = "",
        until: str = "",
    ) -> int:
        with self._conn() as conn:
            q = sa.select(sa.func.count()).select_from(output_assessments)
            if ws_id:
                q = q.where(output_assessments.c.ws_id == ws_id)
            if risk_level:
                q = q.where(output_assessments.c.risk_level == risk_level)
            if since:
                q = q.where(output_assessments.c.created >= since)
            if until:
                q = q.where(output_assessments.c.created <= until)
            row = conn.execute(q).fetchone()
            return row[0] if row else 0

    # -- Structured memories ---------------------------------------------------

    def create_structured_memory(
        self,
        memory_id: str,
        name: str,
        description: str,
        mem_type: str,
        scope: str,
        scope_id: str,
        content: str,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(structured_memories),
                {
                    "memory_id": memory_id,
                    "name": name,
                    "description": description,
                    "type": mem_type,
                    "scope": scope,
                    "scope_id": scope_id,
                    "content": content,
                    "created": now,
                    "updated": now,
                    "last_accessed": now,
                    "access_count": 0,
                },
            )
            conn.commit()

    def upsert_structured_memory(
        self,
        memory_id: str,
        name: str,
        description: str | None,
        mem_type: str | None,
        scope: str,
        scope_id: str,
        content: str,
    ) -> tuple[dict[str, str], bool]:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        insert_stmt = sqlite_insert(structured_memories).values(
            memory_id=memory_id,
            name=name,
            description="" if description is None else description,
            type="general" if mem_type is None else mem_type,
            scope=scope,
            scope_id=scope_id,
            content=content,
            created=now,
            updated=now,
            last_accessed=now,
            access_count=0,
        )
        # On conflict, refresh content + timestamps.  description/type are
        # overwritten only when the caller supplied them; None means "unset" ->
        # keep the stored value.  created and access_count are left untouched.
        set_: dict[str, Any] = {
            "content": insert_stmt.excluded.content,
            "updated": now,
            "last_accessed": now,
        }
        if description is not None:
            set_["description"] = insert_stmt.excluded.description
        if mem_type is not None:
            set_["type"] = insert_stmt.excluded.type
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["name", "scope", "scope_id"],
            set_=set_,
        ).returning(structured_memories)
        with self._conn() as conn:
            row = conn.execute(stmt).fetchone()
            conn.commit()
            if row is None:  # unreachable: ON CONFLICT DO UPDATE returns one row
                return {}, False
            result = dict(row._mapping)
            return result, result["memory_id"] != memory_id

    def get_structured_memory(self, memory_id: str) -> dict[str, str] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(structured_memories).where(structured_memories.c.memory_id == memory_id)
            ).fetchone()
            return dict(row._mapping) if row else None

    def get_structured_memory_by_name(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> dict[str, str] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(structured_memories).where(
                    sa.and_(
                        structured_memories.c.name == name,
                        structured_memories.c.scope == scope,
                        structured_memories.c.scope_id == scope_id,
                    )
                )
            ).fetchone()
            return dict(row._mapping) if row else None

    def delete_structured_memory(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(structured_memories).where(
                    sa.and_(
                        structured_memories.c.name == name,
                        structured_memories.c.scope == scope,
                        structured_memories.c.scope_id == scope_id,
                    )
                )
            )
            conn.commit()
            return result.rowcount > 0

    def delete_structured_memory_by_id(self, memory_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(structured_memories).where(structured_memories.c.memory_id == memory_id)
            )
            conn.commit()
            return result.rowcount > 0

    def list_structured_memories(
        self,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        with self._conn() as conn:
            q = sa.select(structured_memories).order_by(
                structured_memories.c.updated.desc(),
                structured_memories.c.memory_id.asc(),
            )
            if mem_type:
                q = q.where(structured_memories.c.type == mem_type)
            if scope:
                q = q.where(structured_memories.c.scope == scope)
            if scope_id and scope:
                q = q.where(structured_memories.c.scope_id == scope_id)
            q = q.limit(limit)
            rows = conn.execute(q).fetchall()
            return [dict(r._mapping) for r in rows]

    def search_structured_memories(
        self,
        query: str,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """OR-of-terms LIKE search; ranking is the caller's job (BM25 downstream)."""
        if not query or not query.strip():
            return self.list_structured_memories(
                mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
            )
        terms = _normalize_search_terms(query)
        if not terms:
            return self.list_structured_memories(
                mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
            )
        with self._conn() as conn:
            clauses = []
            params: dict[str, str] = {}
            for i, t in enumerate(terms):
                escaped = _escape_like(t)
                clauses.append(
                    f"(name LIKE :n{i} ESCAPE '\\' "
                    f"OR description LIKE :d{i} ESCAPE '\\' "
                    f"OR content LIKE :c{i} ESCAPE '\\')"
                )
                params[f"n{i}"] = f"%{escaped}%"
                params[f"d{i}"] = f"%{escaped}%"
                params[f"c{i}"] = f"%{escaped}%"
            term_clause = " OR ".join(clauses)
            scope_filters = ""
            if mem_type:
                scope_filters += " AND type = :type_filter"
                params["type_filter"] = mem_type
            if scope:
                scope_filters += " AND scope = :scope_filter"
                params["scope_filter"] = scope
            if scope_id and scope:
                scope_filters += " AND scope_id = :scope_id_filter"
                params["scope_id_filter"] = scope_id
            rows = conn.execute(
                sa.text(
                    f"SELECT * FROM structured_memories WHERE ({term_clause}){scope_filters} "
                    f"ORDER BY updated DESC, memory_id ASC LIMIT :lim"
                ),
                {**params, "lim": limit},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def list_visible_structured_memories(
        self,
        scopes: list[tuple[str, str]],
        mem_type: str = "",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """Single-query union across visible (scope, scope_id) pairs."""
        if not scopes:
            return []
        with self._conn() as conn:
            scope_clauses, params = self._build_scope_or_clause(scopes)
            extra = ""
            if mem_type:
                extra = " AND type = :type_filter"
                params["type_filter"] = mem_type
            rows = conn.execute(
                sa.text(
                    f"SELECT * FROM structured_memories WHERE ({scope_clauses}){extra} "
                    f"ORDER BY updated DESC, memory_id ASC LIMIT :lim"
                ),
                {**params, "lim": limit},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def search_visible_structured_memories(
        self,
        query: str,
        scopes: list[tuple[str, str]],
        mem_type: str = "",
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """OR-of-terms search joined with a single visibility OR-group."""
        if not scopes:
            return []
        if not query or not query.strip():
            return self.list_visible_structured_memories(scopes, mem_type=mem_type, limit=limit)
        terms = _normalize_search_terms(query)
        if not terms:
            return self.list_visible_structured_memories(scopes, mem_type=mem_type, limit=limit)
        with self._conn() as conn:
            scope_clauses, params = self._build_scope_or_clause(scopes)
            term_clauses = []
            for i, t in enumerate(terms):
                escaped = _escape_like(t)
                term_clauses.append(
                    f"(name LIKE :n{i} ESCAPE '\\' "
                    f"OR description LIKE :d{i} ESCAPE '\\' "
                    f"OR content LIKE :c{i} ESCAPE '\\')"
                )
                params[f"n{i}"] = f"%{escaped}%"
                params[f"d{i}"] = f"%{escaped}%"
                params[f"c{i}"] = f"%{escaped}%"
            term_clause = " OR ".join(term_clauses)
            extra = ""
            if mem_type:
                extra = " AND type = :type_filter"
                params["type_filter"] = mem_type
            rows = conn.execute(
                sa.text(
                    f"SELECT * FROM structured_memories "
                    f"WHERE ({scope_clauses}) AND ({term_clause}){extra} "
                    f"ORDER BY updated DESC, memory_id ASC LIMIT :lim"
                ),
                {**params, "lim": limit},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    @staticmethod
    def _build_scope_or_clause(
        scopes: list[tuple[str, str]],
    ) -> tuple[str, dict[str, str]]:
        """Build a parameterized OR-group of (scope[, scope_id]) predicates."""
        params: dict[str, str] = {}
        clauses: list[str] = []
        for i, (s, sid) in enumerate(scopes):
            params[f"sc{i}"] = s
            if sid:
                params[f"sid{i}"] = sid
                clauses.append(f"(scope = :sc{i} AND scope_id = :sid{i})")
            else:
                clauses.append(f"scope = :sc{i}")
        return " OR ".join(clauses), params

    def touch_structured_memories(self, keys: list[tuple[str, str, str]]) -> int:
        """Batch-touch multiple memories by (name, scope, scope_id)."""
        if not keys:
            return 0
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        total = 0
        with self._conn() as conn:
            for name, scope, scope_id in keys:
                result = conn.execute(
                    sa.update(structured_memories)
                    .where(
                        sa.and_(
                            structured_memories.c.name == name,
                            structured_memories.c.scope == scope,
                            structured_memories.c.scope_id == scope_id,
                        )
                    )
                    .values(
                        last_accessed=now,
                        access_count=structured_memories.c.access_count + 1,
                    )
                )
                total += result.rowcount
            conn.commit()
        return total

    def count_structured_memories(
        self, mem_type: str = "", scope: str = "", scope_id: str = ""
    ) -> int:
        with self._conn() as conn:
            q = sa.select(sa.func.count()).select_from(structured_memories)
            if mem_type:
                q = q.where(structured_memories.c.type == mem_type)
            if scope:
                q = q.where(structured_memories.c.scope == scope)
            if scope_id and scope:
                q = q.where(structured_memories.c.scope_id == scope_id)
            result = conn.execute(q).scalar()
            return int(result or 0)

    # -- System settings -------------------------------------------------------

    def get_system_setting(self, key: str, node_id: str = "") -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(system_settings).where(
                    sa.and_(
                        system_settings.c.key == key,
                        system_settings.c.node_id == node_id,
                    )
                )
            ).fetchone()
            return dict(row._mapping) if row else None

    def list_system_settings(self, node_id: str = "") -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(system_settings).order_by(system_settings.c.key)
            if node_id:
                # Return both global and node-specific
                q = q.where(
                    sa.or_(
                        system_settings.c.node_id == "",
                        system_settings.c.node_id == node_id,
                    )
                )
            return [dict(r._mapping) for r in conn.execute(q).fetchall()]

    def upsert_system_setting(
        self,
        key: str,
        value: str,
        node_id: str = "",
        is_secret: bool = False,
        changed_by: str = "",
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        secret_val = 1 if is_secret else 0
        stmt = sqlite_insert(system_settings).values(
            key=key,
            value=value,
            node_id=node_id,
            is_secret=secret_val,
            changed_by=changed_by,
            created=now,
            updated=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key", "node_id"],
            set_={
                "value": value,
                "is_secret": secret_val,
                "changed_by": changed_by,
                "updated": now,
            },
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def delete_system_setting(self, key: str, node_id: str = "") -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(system_settings).where(
                    sa.and_(
                        system_settings.c.key == key,
                        system_settings.c.node_id == node_id,
                    )
                )
            )
            conn.commit()
            return result.rowcount > 0

    def get_system_settings_bulk(self, node_id: str = "") -> dict[str, str]:
        with self._conn() as conn:
            if not node_id:
                # Global only
                rows = conn.execute(
                    sa.select(system_settings.c.key, system_settings.c.value).where(
                        system_settings.c.node_id == ""
                    )
                ).fetchall()
                return {r.key: r.value for r in rows}
            # Global + node overrides in one query; node_id sorts after ""
            # so node-specific values overwrite globals in the dict
            rows = conn.execute(
                sa.select(system_settings.c.key, system_settings.c.value)
                .where(
                    sa.or_(
                        system_settings.c.node_id == "",
                        system_settings.c.node_id == node_id,
                    )
                )
                .order_by(system_settings.c.node_id)
            ).fetchall()
            return {r.key: r.value for r in rows}

    # -- MCP server definitions ------------------------------------------------

    def create_mcp_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: str = "",
        args: str = "[]",
        url: str = "",
        headers: str = "{}",
        env: str = "{}",
        auto_approve: bool = False,
        enabled: bool = True,
        created_by: str = "",
        registry_name: str | None = None,
        registry_version: str = "",
        registry_meta: str = "{}",
        auth_type: str = "static",
        oauth_client_id: str | None = None,
        oauth_client_secret_ct: bytes | None = None,
        oauth_scopes: str | None = None,
        oauth_audience: str | None = None,
        oauth_registration_mode: str | None = None,
        oauth_authorization_server_url: str | None = None,
        oauth_as_issuer_cached: str | None = None,
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(mcp_servers).prefix_with("OR IGNORE"),
                {
                    "server_id": server_id,
                    "name": name,
                    "transport": transport,
                    "command": command,
                    "args": args,
                    "url": url,
                    "headers": headers,
                    "env": env,
                    "auto_approve": 1 if auto_approve else 0,
                    "enabled": 1 if enabled else 0,
                    "created_by": created_by,
                    "registry_name": registry_name,
                    "registry_version": registry_version,
                    "registry_meta": registry_meta,
                    "auth_type": auth_type,
                    "oauth_client_id": oauth_client_id,
                    "oauth_client_secret_ct": oauth_client_secret_ct,
                    "oauth_scopes": oauth_scopes,
                    "oauth_audience": oauth_audience,
                    "oauth_registration_mode": oauth_registration_mode,
                    "oauth_authorization_server_url": oauth_authorization_server_url,
                    "oauth_as_issuer_cached": oauth_as_issuer_cached,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_mcp_server(self, server_id: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(mcp_servers).where(mcp_servers.c.server_id == server_id)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "auto_approve", "enabled")

    def get_mcp_server_by_name(self, name: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(sa.select(mcp_servers).where(mcp_servers.c.name == name)).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "auto_approve", "enabled")

    def get_mcp_server_by_registry_name(self, registry_name: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(mcp_servers).where(mcp_servers.c.registry_name == registry_name)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "auto_approve", "enabled")

    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:

        with self._conn() as conn:
            q = sa.select(mcp_servers).order_by(mcp_servers.c.name)
            if enabled_only:
                q = q.where(mcp_servers.c.enabled == 1)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "auto_approve", "enabled") for r in rows]

    def update_mcp_server(self, server_id: str, **fields: Any) -> bool:

        fields = {k: v for k, v in fields.items() if k in _MCP_SERVER_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "auto_approve" in fields:
            fields["auto_approve"] = 1 if fields["auto_approve"] else 0
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        with self._conn() as conn:
            result = conn.execute(
                sa.update(mcp_servers).where(mcp_servers.c.server_id == server_id).values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_mcp_server(self, server_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(mcp_servers).where(mcp_servers.c.server_id == server_id)
            )
            conn.commit()
            return result.rowcount > 0

    # -- MCP OAuth: client-secret + per-(user, server) tokens ------------------

    def set_mcp_oauth_client_secret_ct(self, server_id: str, secret_ct: bytes | None) -> bool:
        """Update only the encrypted OAuth client-secret column.

        Returns True when a row was updated. ``None`` clears the column.
        Bypasses ``MCP_SERVER_MUTABLE`` deliberately.
        """
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.update(mcp_servers)
                .where(mcp_servers.c.server_id == server_id)
                .values(oauth_client_secret_ct=secret_ct, updated=now)
            )
            conn.commit()
            return result.rowcount > 0

    def create_mcp_user_token(
        self,
        user_id: str,
        server_name: str,
        *,
        access_token_ct: bytes,
        refresh_token_ct: bytes | None,
        expires_at: str | None,
        scopes: str | None,
        as_issuer: str,
        audience: str,
    ) -> None:
        """Insert a new per-(user, server) token row. No-op on conflict."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(mcp_user_tokens).prefix_with("OR IGNORE"),
                {
                    "user_id": user_id,
                    "server_name": server_name,
                    "access_token_ct": access_token_ct,
                    "refresh_token_ct": refresh_token_ct,
                    "expires_at": expires_at,
                    "scopes": scopes,
                    "as_issuer": as_issuer,
                    "audience": audience,
                    "created": now,
                    "last_refreshed": None,
                },
            )
            conn.commit()

    def get_mcp_user_token(self, user_id: str, server_name: str) -> MCPUserToken | None:
        """Return the per-(user, server) token row or None."""
        with self._conn() as conn:
            row = conn.execute(
                sa.select(mcp_user_tokens).where(
                    (mcp_user_tokens.c.user_id == user_id)
                    & (mcp_user_tokens.c.server_name == server_name)
                )
            ).fetchone()
            if row is None:
                return None
            m = row._mapping
            return MCPUserToken(
                user_id=m["user_id"],
                server_name=m["server_name"],
                access_token_ct=bytes(m["access_token_ct"]),
                refresh_token_ct=(
                    bytes(m["refresh_token_ct"]) if m["refresh_token_ct"] is not None else None
                ),
                expires_at=m["expires_at"],
                scopes=m["scopes"],
                as_issuer=m["as_issuer"],
                audience=m["audience"],
                created=m["created"],
                last_refreshed=m["last_refreshed"],
            )

    def update_mcp_user_token_after_refresh(
        self,
        user_id: str,
        server_name: str,
        *,
        access_token_ct: bytes,
        refresh_token_ct: bytes | None,
        expires_at: str | None,
    ) -> bool:
        """Rewrite token columns + ``last_refreshed`` after an AS refresh.

        Preserves columns this method does not rewrite (``scopes``,
        ``as_issuer``, ``audience``, ``created``). Returns True when a
        row was updated.
        """
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.update(mcp_user_tokens)
                .where(
                    (mcp_user_tokens.c.user_id == user_id)
                    & (mcp_user_tokens.c.server_name == server_name)
                )
                .values(
                    access_token_ct=access_token_ct,
                    refresh_token_ct=refresh_token_ct,
                    expires_at=expires_at,
                    last_refreshed=now,
                )
            )
            conn.commit()
            return result.rowcount > 0

    def delete_mcp_user_token(self, user_id: str, server_name: str) -> bool:
        """Delete the per-(user, server) token row. Returns True if existed."""
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(mcp_user_tokens).where(
                    (mcp_user_tokens.c.user_id == user_id)
                    & (mcp_user_tokens.c.server_name == server_name)
                )
            )
            conn.commit()
            return result.rowcount > 0

    def list_mcp_user_token_metadata_by_user(self, user_id: str) -> list[MCPUserTokenMetadataRow]:
        """Return non-secret metadata rows for ``user_id``, ordered by ``created`` ASC.

        Projects metadata columns at the SQL boundary so ciphertext
        blobs (``access_token_ct`` / ``refresh_token_ct``) never cross
        the wire on the settings-list path.
        """
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    mcp_user_tokens.c.user_id,
                    mcp_user_tokens.c.server_name,
                    mcp_user_tokens.c.expires_at,
                    mcp_user_tokens.c.scopes,
                    mcp_user_tokens.c.as_issuer,
                    mcp_user_tokens.c.audience,
                    mcp_user_tokens.c.created,
                    mcp_user_tokens.c.last_refreshed,
                )
                .where(mcp_user_tokens.c.user_id == user_id)
                .order_by(mcp_user_tokens.c.created)
            ).fetchall()
            out: list[MCPUserTokenMetadataRow] = []
            for row in rows:
                m = row._mapping
                out.append(
                    MCPUserTokenMetadataRow(
                        user_id=m["user_id"],
                        server_name=m["server_name"],
                        expires_at=m["expires_at"],
                        scopes=m["scopes"],
                        as_issuer=m["as_issuer"],
                        audience=m["audience"],
                        created=m["created"],
                        last_refreshed=m["last_refreshed"],
                    )
                )
            return out

    def list_mcp_user_token_reconcile_targets(self) -> list[tuple[str, str, str | None]]:
        """Return ``(user_id, server_name, COALESCE(last_refreshed, created))`` per
        token row — the freshness sweep's drive set + keepalive-refresh signal.

        Unfiltered by expiry: an expired access token with a live refresh token
        is still a consented grant the sweep must keep hot. Only ``oauth_user``
        servers write these rows; no ciphertext is projected.
        """
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    mcp_user_tokens.c.user_id,
                    mcp_user_tokens.c.server_name,
                    sa.func.coalesce(mcp_user_tokens.c.last_refreshed, mcp_user_tokens.c.created),
                )
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def delete_mcp_oauth_rows_by_server_name(self, server_name: str) -> int:
        """Purge user tokens + pending OAuth state for *server_name*."""
        with self._conn() as conn:
            tokens_result = conn.execute(
                sa.delete(mcp_user_tokens).where(mcp_user_tokens.c.server_name == server_name)
            )
            pending_result = conn.execute(
                sa.delete(mcp_oauth_pending).where(mcp_oauth_pending.c.server_name == server_name)
            )
            conn.commit()
            return int(tokens_result.rowcount or 0) + int(pending_result.rowcount or 0)

    def get_mcp_oauth_client_secret_ct(self, server_id: str) -> bytes | None:
        """Return the encrypted OAuth client secret column or None."""
        with self._conn() as conn:
            row = conn.execute(
                sa.select(mcp_servers.c.oauth_client_secret_ct).where(
                    mcp_servers.c.server_id == server_id
                )
            ).fetchone()
            if row is None or row[0] is None:
                return None
            return bytes(row[0])

    # -- MCP OAuth pending state (per-(user, server) flow) ---------------------

    def create_mcp_oauth_pending_state(
        self,
        state: str,
        user_id: str,
        server_name: str,
        code_verifier: str,
        return_url: str,
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(mcp_oauth_pending),
                {
                    "state": state,
                    "user_id": user_id,
                    "server_name": server_name,
                    "code_verifier": code_verifier,
                    "return_url": return_url,
                    "created_at": now,
                },
            )
            conn.commit()

    def pop_mcp_oauth_pending_state(
        self, state: str, max_age_seconds: int = 600
    ) -> MCPOAuthPendingState | None:

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._conn() as conn:
            # Acquire write lock before SELECT to prevent TOCTOU race
            conn.execute(sa.text("BEGIN IMMEDIATE"))
            row = conn.execute(
                sa.select(
                    mcp_oauth_pending.c.state,
                    mcp_oauth_pending.c.user_id,
                    mcp_oauth_pending.c.server_name,
                    mcp_oauth_pending.c.code_verifier,
                    mcp_oauth_pending.c.return_url,
                    mcp_oauth_pending.c.created_at,
                ).where(
                    (mcp_oauth_pending.c.state == state) & (mcp_oauth_pending.c.created_at > cutoff)
                )
            ).fetchone()
            # Always delete the row (whether valid, expired, or missing is fine)
            conn.execute(sa.delete(mcp_oauth_pending).where(mcp_oauth_pending.c.state == state))
            conn.commit()
            if not row:
                return None
            return MCPOAuthPendingState(
                state=row[0],
                user_id=row[1],
                server_name=row[2],
                code_verifier=row[3],
                return_url=row[4],
                created_at=row[5],
            )

    def cleanup_expired_mcp_oauth_pending_states(self, max_age_seconds: int = 600) -> int:

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(mcp_oauth_pending).where(mcp_oauth_pending.c.created_at < cutoff)
            )
            conn.commit()
            return result.rowcount

    # -- MCP pending-consent (Phase 9) ----------------------------------------

    def upsert_mcp_pending_consent(
        self,
        user_id: str,
        server_name: str,
        error_code: str,
        scopes_required: str | None,
        now_iso: str,
    ) -> None:
        from sqlalchemy.dialects import sqlite as sa_sqlite

        stmt = sa_sqlite.insert(mcp_pending_consent).values(
            user_id=user_id,
            server_name=server_name,
            error_code=error_code,
            scopes_required=scopes_required,
            first_seen_at=now_iso,
            last_seen_at=now_iso,
            occurrence_count=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "server_name"],
            set_={
                "error_code": stmt.excluded.error_code,
                "scopes_required": stmt.excluded.scopes_required,
                "last_seen_at": stmt.excluded.last_seen_at,
                "occurrence_count": mcp_pending_consent.c.occurrence_count + 1,
            },
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def list_mcp_pending_consent_by_user(self, user_id: str) -> list[MCPPendingConsentRow]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(mcp_pending_consent)
                .where(mcp_pending_consent.c.user_id == user_id)
                .order_by(mcp_pending_consent.c.last_seen_at.desc())
            ).fetchall()
        out: list[MCPPendingConsentRow] = []
        for r in rows:
            m = r._mapping
            out.append(
                MCPPendingConsentRow(
                    user_id=m["user_id"],
                    server_name=m["server_name"],
                    error_code=m["error_code"],
                    scopes_required=m["scopes_required"],
                    first_seen_at=m["first_seen_at"],
                    last_seen_at=m["last_seen_at"],
                    occurrence_count=m["occurrence_count"],
                )
            )
        return out

    def delete_mcp_pending_consent(self, user_id: str, server_name: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(mcp_pending_consent).where(
                    (mcp_pending_consent.c.user_id == user_id)
                    & (mcp_pending_consent.c.server_name == server_name)
                )
            )
            conn.commit()
            return bool(result.rowcount)

    def delete_all_mcp_pending_consent_by_user(self, user_id: str) -> int:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(mcp_pending_consent).where(mcp_pending_consent.c.user_id == user_id)
            )
            conn.commit()
            return int(result.rowcount or 0)

    def count_mcp_consented_users_by_server(self, server_name: str) -> int:
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.select(sa.func.count(sa.distinct(mcp_user_tokens.c.user_id)))
                .where(mcp_user_tokens.c.server_name == server_name)
                .where(
                    sa.or_(
                        mcp_user_tokens.c.expires_at.is_(None),
                        mcp_user_tokens.c.expires_at > now_iso,
                    )
                )
            ).scalar()
        return int(result or 0)

    def count_mcp_consented_users_grouped_by_server(self) -> dict[str, int]:
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    mcp_user_tokens.c.server_name,
                    sa.func.count(sa.distinct(mcp_user_tokens.c.user_id)),
                )
                .where(
                    sa.or_(
                        mcp_user_tokens.c.expires_at.is_(None),
                        mcp_user_tokens.c.expires_at > now_iso,
                    )
                )
                .group_by(mcp_user_tokens.c.server_name)
            ).fetchall()
        return {row[0]: int(row[1] or 0) for row in rows}

    def any_user_scoped_mcp_servers(self) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.select(sa.literal(1))
                .select_from(mcp_servers)
                .where(mcp_servers.c.auth_type.in_(sorted(USER_SCOPED_AUTH_TYPES)))
                .limit(1)
            ).scalar()
        return result is not None

    # -- Model definitions -----------------------------------------------------

    def create_model_definition(
        self,
        definition_id: str,
        alias: str,
        model: str,
        provider: str = "openai",
        base_url: str = "",
        api_key: str = "",
        context_window: int = 32768,
        capabilities: str = "{}",
        enabled: bool = True,
        created_by: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        surface_persisted_reasoning: bool = True,
        replay_reasoning_to_model: bool = False,
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(model_definitions).prefix_with("OR IGNORE"),
                {
                    "definition_id": definition_id,
                    "alias": alias,
                    "model": model,
                    "provider": provider,
                    "base_url": base_url,
                    "api_key": api_key,
                    "context_window": context_window,
                    "capabilities": capabilities,
                    "enabled": 1 if enabled else 0,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "reasoning_effort": reasoning_effort,
                    "surface_persisted_reasoning": 1 if surface_persisted_reasoning else 0,
                    "replay_reasoning_to_model": (1 if replay_reasoning_to_model else 0),
                    "created_by": created_by,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_model_definition(self, definition_id: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(model_definitions).where(
                    model_definitions.c.definition_id == definition_id
                )
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(
                row, "enabled", "surface_persisted_reasoning", "replay_reasoning_to_model"
            )

    def get_model_definition_by_alias(self, alias: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(model_definitions).where(model_definitions.c.alias == alias)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(
                row, "enabled", "surface_persisted_reasoning", "replay_reasoning_to_model"
            )

    def list_model_definitions(self, enabled_only: bool = False) -> list[dict[str, Any]]:

        with self._conn() as conn:
            q = sa.select(model_definitions).order_by(model_definitions.c.alias)
            if enabled_only:
                q = q.where(model_definitions.c.enabled == 1)
            rows = conn.execute(q).fetchall()
            return [
                _row_to_dict(
                    r, "enabled", "surface_persisted_reasoning", "replay_reasoning_to_model"
                )
                for r in rows
            ]

    def update_model_definition(self, definition_id: str, **fields: Any) -> bool:

        fields = {k: v for k, v in fields.items() if k in _MODEL_DEF_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if "surface_persisted_reasoning" in fields:
            fields["surface_persisted_reasoning"] = (
                1 if fields["surface_persisted_reasoning"] else 0
            )
        if "replay_reasoning_to_model" in fields:
            fields["replay_reasoning_to_model"] = 1 if fields["replay_reasoning_to_model"] else 0
        with self._conn() as conn:
            result = conn.execute(
                sa.update(model_definitions)
                .where(model_definitions.c.definition_id == definition_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_model_definition(self, definition_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(model_definitions).where(
                    model_definitions.c.definition_id == definition_id
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- Projects --------------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        name: str,
        owner_id: str,
        visibility: str = "private",
        state: str = "active",
        parent_project_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(projects).prefix_with("OR IGNORE"),
                {
                    "project_id": project_id,
                    "name": name,
                    "owner_id": owner_id,
                    "visibility": visibility,
                    "state": state,
                    "parent_project_id": parent_project_id,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(projects).where(projects.c.project_id == project_id)
            ).fetchone()
            return _row_to_dict(row) if row is not None else None

    def list_projects_for_user(
        self, user_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            member_subq = sa.select(project_members.c.project_id).where(
                project_members.c.user_id == user_id
            )
            cond = sa.or_(
                projects.c.owner_id == user_id,
                projects.c.visibility == "public",
                projects.c.project_id.in_(member_subq),
            )
            q = sa.select(projects).where(cond)
            if not include_archived:
                q = q.where(projects.c.state == "active")
            rows = conn.execute(q.order_by(projects.c.name)).fetchall()
            return [_row_to_dict(r) for r in rows]

    def update_project(self, project_id: str, **fields: Any) -> bool:
        fields = {k: v for k, v in fields.items() if k in _PROJECT_MUTABLE}
        if not fields:
            return False
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.update(projects).where(projects.c.project_id == project_id).values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_project(self, project_id: str) -> bool:
        with self._conn() as conn:
            # No FK cascade in the schema family, so purge the project's scoped
            # memory + member rows explicitly (same transaction) before the
            # project row — honouring the "destroys the container AND its scoped
            # memory" contract the endpoint + UI promise.
            conn.execute(
                sa.delete(structured_memories).where(
                    sa.and_(
                        structured_memories.c.scope == "project",
                        structured_memories.c.scope_id == project_id,
                    )
                )
            )
            conn.execute(
                sa.delete(project_members).where(project_members.c.project_id == project_id)
            )
            result = conn.execute(sa.delete(projects).where(projects.c.project_id == project_id))
            conn.commit()
            return result.rowcount > 0

    def add_project_member(self, project_id: str, user_id: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(project_members).prefix_with("OR IGNORE"),
                {"project_id": project_id, "user_id": user_id, "created": now},
            )
            conn.commit()

    def remove_project_member(self, project_id: str, user_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(project_members).where(
                    project_members.c.project_id == project_id,
                    project_members.c.user_id == user_id,
                )
            )
            conn.commit()
            return result.rowcount > 0

    def list_project_members(self, project_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(project_members.c.user_id)
                .where(project_members.c.project_id == project_id)
                .order_by(project_members.c.user_id)
            ).fetchall()
            return [str(r[0]) for r in rows]

    def is_project_member(self, project_id: str, user_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.select(sa.literal(1))
                .select_from(project_members)
                .where(
                    project_members.c.project_id == project_id,
                    project_members.c.user_id == user_id,
                )
                .limit(1)
            ).scalar()
        return result is not None

    def list_workstreams_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    workstreams.c.ws_id,
                    workstreams.c.name,
                    workstreams.c.title,
                    workstreams.c.state,
                    workstreams.c.kind,
                    workstreams.c.updated,
                    workstreams.c.node_id,
                    workstreams.c.user_id,
                )
                .where(workstreams.c.project_id == project_id)
                .order_by(workstreams.c.updated.desc())
            ).fetchall()
            return [
                {
                    "ws_id": r[0],
                    "name": r[1],
                    "title": r[2],
                    "state": r[3],
                    "kind": r[4],
                    "updated": r[5],
                    "node_id": r[6],
                    "user_id": r[7],
                }
                for r in rows
            ]

    def list_project_attachments(self, project_id: str) -> list[dict[str, Any]]:
        """Committed attachments referenced by any turn in the project's
        workstreams — metadata only (never the content blob), each with the
        first referencing ws_id (content serving is ws-scoped, so the caller
        needs a ws to build a download URL against).
        """
        with self._conn() as conn:
            ref_rows = conn.execute(
                sa.select(conversations.c.ws_id, conversations.c.attachments)
                .select_from(
                    conversations.join(workstreams, workstreams.c.ws_id == conversations.c.ws_id)
                )
                .where(
                    workstreams.c.project_id == project_id,
                    conversations.c.attachments.is_not(None),
                )
                .order_by(conversations.c.id)
            ).fetchall()
            first_ws: dict[str, str] = {}
            for ws_id, raw in ref_rows:
                try:
                    ids = json.loads(raw) if raw else []
                except (TypeError, ValueError):
                    continue
                if not isinstance(ids, list):
                    continue
                for aid in ids:
                    if isinstance(aid, str) and aid and aid not in first_ws:
                        first_ws[aid] = ws_id
            if not first_ws:
                return []
            # Chunk the IN() — a project can reference more distinct
            # blobs than the driver's bind-parameter cap.
            meta: dict[str, Any] = {}
            ids = list(first_ws)
            for i in range(0, len(ids), 500):
                meta_rows = conn.execute(
                    sa.select(
                        workstream_attachments.c.attachment_id,
                        workstream_attachments.c.filename,
                        workstream_attachments.c.mime_type,
                        workstream_attachments.c.size_bytes,
                        workstream_attachments.c.kind,
                        workstream_attachments.c.created,
                    ).where(workstream_attachments.c.attachment_id.in_(ids[i : i + 500]))
                ).fetchall()
                for r in meta_rows:
                    meta[r[0]] = r
            out: list[dict[str, Any]] = []
            for aid, ws_id in first_ws.items():
                m = meta.get(aid)
                if m is None:
                    # Ref-list names a pruned blob (refcount GC) — skip.
                    continue
                out.append(
                    {
                        "attachment_id": m[0],
                        "filename": m[1],
                        "mime_type": m[2],
                        "size_bytes": m[3],
                        "kind": m[4],
                        "created": m[5],
                        "ws_id": ws_id,
                    }
                )
            return out

    # -- OIDC identity ---------------------------------------------------------

    def create_oidc_user(
        self,
        user_id: str,
        username: str,
        display_name: str,
        password_hash: str,
        issuer: str,
        subject: str,
        email: str,
        oid: str = "",
        tid: str = "",
    ) -> None:
        from turnstone.core.storage._protocol import StorageConflictError

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            try:
                # BEGIN IMMEDIATE upgrades to a write lock up front so the
                # two inserts can't be interleaved with a parallel writer.
                conn.execute(sa.text("BEGIN IMMEDIATE"))
                conn.execute(
                    sa.insert(users),
                    {
                        "user_id": user_id,
                        "username": username,
                        "display_name": display_name,
                        "password_hash": password_hash,
                        "created": now,
                    },
                )
                conn.execute(
                    sa.insert(oidc_identities),
                    {
                        "issuer": issuer,
                        "subject": subject,
                        "user_id": user_id,
                        "email": email,
                        "created": now,
                        "last_login": now,
                        "oid": oid,
                        "tid": tid,
                    },
                )
            except sa.exc.IntegrityError as exc:
                conn.rollback()
                msg = str(exc.orig) if exc.orig is not None else str(exc)
                if "users.username" in msg:
                    raise StorageConflictError(f"username already taken: {username}") from exc
                if "oidc_identities" in msg:
                    raise StorageConflictError(
                        f"OIDC identity already linked: ({issuer}, {subject})"
                    ) from exc
                raise StorageConflictError(f"OIDC user provisioning conflict: {msg}") from exc
            conn.commit()

    def create_oidc_identity(self, issuer: str, subject: str, user_id: str, email: str) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(oidc_identities).prefix_with("OR IGNORE"),
                {
                    "issuer": issuer,
                    "subject": subject,
                    "user_id": user_id,
                    "email": email,
                    "created": now,
                    "last_login": now,
                },
            )
            conn.commit()

    def get_oidc_identity(self, issuer: str, subject: str) -> OIDCIdentity | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(
                    oidc_identities.c.issuer,
                    oidc_identities.c.subject,
                    oidc_identities.c.user_id,
                    oidc_identities.c.email,
                    oidc_identities.c.created,
                    oidc_identities.c.last_login,
                    oidc_identities.c.oid,
                    oidc_identities.c.tid,
                ).where(
                    (oidc_identities.c.issuer == issuer) & (oidc_identities.c.subject == subject)
                )
            ).fetchone()
            if row:
                return OIDCIdentity(
                    issuer=row[0],
                    subject=row[1],
                    user_id=row[2],
                    email=row[3],
                    created=row[4],
                    last_login=row[5],
                    oid=row[6],
                    tid=row[7],
                )
            return None

    def update_oidc_identity_login(
        self, issuer: str, subject: str, oid: str = "", tid: str = ""
    ) -> bool:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        # last_login always; oid/tid only when supplied, so a login that omits
        # them can't wipe a value captured on an earlier login.
        values: dict[str, str] = {"last_login": now}
        if oid:
            values["oid"] = oid
        if tid:
            values["tid"] = tid
        with self._conn() as conn:
            result = conn.execute(
                sa.update(oidc_identities)
                .where(
                    (oidc_identities.c.issuer == issuer) & (oidc_identities.c.subject == subject)
                )
                .values(**values)
            )
            conn.commit()
            return result.rowcount > 0

    def list_oidc_identities_for_user(self, user_id: str) -> list[OIDCIdentity]:

        with self._conn() as conn:
            rows = conn.execute(
                sa.select(
                    oidc_identities.c.issuer,
                    oidc_identities.c.subject,
                    oidc_identities.c.user_id,
                    oidc_identities.c.email,
                    oidc_identities.c.created,
                    oidc_identities.c.last_login,
                    oidc_identities.c.oid,
                    oidc_identities.c.tid,
                )
                .where(oidc_identities.c.user_id == user_id)
                .order_by(oidc_identities.c.created.desc())
            ).fetchall()
            return [
                OIDCIdentity(
                    issuer=r[0],
                    subject=r[1],
                    user_id=r[2],
                    email=r[3],
                    created=r[4],
                    last_login=r[5],
                    oid=r[6],
                    tid=r[7],
                )
                for r in rows
            ]

    def delete_oidc_identity(self, issuer: str, subject: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(oidc_identities).where(
                    (oidc_identities.c.issuer == issuer) & (oidc_identities.c.subject == subject)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- OIDC user credential (single-credential MCP minting, #551) -------------

    def upsert_oidc_user_credential(
        self, user_id: str, issuer: str, *, refresh_token_ct: bytes
    ) -> None:
        """Create or replace the user's captured IdP refresh token."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            stmt = sqlite_insert(oidc_user_credentials).values(
                user_id=user_id,
                issuer=issuer,
                refresh_token_ct=refresh_token_ct,
                created=now,
                last_refreshed=now,
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["user_id", "issuer"],
                    set_={"refresh_token_ct": refresh_token_ct, "last_refreshed": now},
                )
            )
            conn.commit()

    def get_oidc_user_credential(self, user_id: str, issuer: str) -> OIDCUserCredential | None:
        """Return the captured credential row or None."""
        with self._conn() as conn:
            row = conn.execute(
                sa.select(oidc_user_credentials).where(
                    (oidc_user_credentials.c.user_id == user_id)
                    & (oidc_user_credentials.c.issuer == issuer)
                )
            ).fetchone()
            if row is None:
                return None
            m = row._mapping
            return OIDCUserCredential(
                user_id=m["user_id"],
                issuer=m["issuer"],
                refresh_token_ct=bytes(m["refresh_token_ct"]),
                created=m["created"],
                last_refreshed=m["last_refreshed"],
            )

    def update_oidc_user_credential_refresh(
        self, user_id: str, issuer: str, *, refresh_token_ct: bytes
    ) -> bool:
        """Persist the newest refresh token after a rotating redemption."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            result = conn.execute(
                sa.update(oidc_user_credentials)
                .where(
                    (oidc_user_credentials.c.user_id == user_id)
                    & (oidc_user_credentials.c.issuer == issuer)
                )
                .values(refresh_token_ct=refresh_token_ct, last_refreshed=now)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_oidc_user_credential(self, user_id: str, issuer: str) -> bool:
        """Remove the captured credential. Returns True if existed."""
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(oidc_user_credentials).where(
                    (oidc_user_credentials.c.user_id == user_id)
                    & (oidc_user_credentials.c.issuer == issuer)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- OIDC pending state ----------------------------------------------------

    def create_oidc_pending_state(
        self, state: str, nonce: str, code_verifier: str, audience: str
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(oidc_pending_states),
                {
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "audience": audience,
                    "created_at": now,
                },
            )
            conn.commit()

    def pop_oidc_pending_state(
        self, state: str, max_age_seconds: int = 300
    ) -> OIDCPendingState | None:

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._conn() as conn:
            # Acquire write lock before SELECT to prevent TOCTOU race
            conn.execute(sa.text("BEGIN IMMEDIATE"))
            row = conn.execute(
                sa.select(
                    oidc_pending_states.c.state,
                    oidc_pending_states.c.nonce,
                    oidc_pending_states.c.code_verifier,
                    oidc_pending_states.c.audience,
                    oidc_pending_states.c.created_at,
                ).where(
                    (oidc_pending_states.c.state == state)
                    & (oidc_pending_states.c.created_at > cutoff)
                )
            ).fetchone()
            # Always delete the row (whether valid, expired, or missing is fine)
            conn.execute(sa.delete(oidc_pending_states).where(oidc_pending_states.c.state == state))
            conn.commit()
            if not row:
                return None
            return OIDCPendingState(
                state=row[0],
                nonce=row[1],
                code_verifier=row[2],
                audience=row[3],
                created_at=row[4],
            )

    def cleanup_expired_oidc_states(self, max_age_seconds: int = 300) -> int:

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(oidc_pending_states).where(oidc_pending_states.c.created_at < cutoff)
            )
            conn.commit()
            return result.rowcount

    # -- Personas ---------------------------------------------------------------

    def list_personas(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        with self._conn() as conn:
            q = sa.select(personas).order_by(personas.c.name)
            if not include_disabled:
                q = q.where(personas.c.enabled == 1)
            rows = conn.execute(q).fetchall()
            return [_persona_row_to_dict(r) for r in rows]

    def get_persona(self, persona_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(personas).where(personas.c.persona_id == persona_id)
            ).fetchone()
            return _persona_row_to_dict(row) if row is not None else None

    def get_persona_by_name(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(personas).where(personas.c.name == name)).fetchone()
            return _persona_row_to_dict(row) if row is not None else None

    def get_default_persona(self, kind: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(personas).where(
                    sa.and_(personas.c.is_default == 1, personas.c.enabled == 1)
                )
            ).fetchall()
        for row in rows:
            d = _persona_row_to_dict(row)
            if kind in d["applies_to_kinds"]:
                return d
        return None

    def create_persona(self, persona: dict[str, Any]) -> None:
        values = _serialize_persona_fields(persona)
        if not values.get("persona_id") or not values.get("name"):
            raise ValueError("persona requires persona_id and name")
        # base_prompt_file is code-only — set only by the migration seeds, never
        # via this operator-facing path.  Drop it so a caller can't smuggle a
        # file ref past the guard: the INSERT omits the column, so a supplied
        # base_prompt_file would otherwise satisfy this check yet trip the CHECK,
        # surfaced as a misleading name-collision.  Operators supply base_prompt.
        values.pop("base_prompt_file", None)
        if not values.get("base_prompt"):
            raise ValueError("persona requires a base_prompt")
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            existing = conn.execute(
                sa.select(personas.c.persona_id).where(personas.c.name == values["name"])
            ).fetchone()
            if existing is not None:
                raise ValueError(f"persona name already exists: {values['name']}")
            default_kinds = persona.get("applies_to_kinds") or ["interactive"]
            if values.get("is_default"):
                _validate_and_clear_default_persona(
                    conn,
                    personas,
                    persona_id=values["persona_id"],
                    kinds=default_kinds,
                    enabled=persona.get("enabled", True),
                    now=now,
                )
            try:
                conn.execute(
                    sa.insert(personas),
                    {
                        "persona_id": values["persona_id"],
                        "name": values["name"],
                        "display_name": values.get("display_name", ""),
                        "description": values.get("description", ""),
                        "base_prompt": values.get("base_prompt"),
                        "tool_allowlist": values.get("tool_allowlist"),
                        "mcp_enabled": values.get("mcp_enabled", 1),
                        "memory_enabled": values.get("memory_enabled", 1),
                        "applies_to_kinds": values.get("applies_to_kinds", '["interactive"]'),
                        "is_default": values.get("is_default", 0),
                        "enabled": values.get("enabled", 1),
                        "org_id": values.get("org_id", ""),
                        "created_by": values.get("created_by", ""),
                        "created": now,
                        "updated": now,
                    },
                )
            except sa.exc.IntegrityError as exc:
                # SELECT-then-INSERT loser on unique(name): surface the
                # same ValueError the pre-check raises so callers map one
                # error shape (400), not an opaque 500.
                raise ValueError(f"persona name already exists: {values['name']}") from exc
            if values.get("is_default"):
                _assert_single_default_persona(conn, personas, default_kinds[0])
            conn.commit()

    def update_persona(self, persona_id: str, **fields: Any) -> bool:
        fields = {k: v for k, v in fields.items() if k in _PERSONA_MUTABLE}
        if not fields:
            return False
        # Validate/serialize BEFORE the invariant checks so malformed input
        # (explicit-None kinds, wrong types) surfaces as the serializer's
        # precise ValueError instead of a TypeError escaping the routes'
        # 400 mapping as a 500.
        values = _serialize_persona_fields(fields)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                sa.select(personas).where(personas.c.persona_id == persona_id)
            ).fetchone()
            if row is None:
                return False
            current = _persona_row_to_dict(row)
            builtin = bool(current.get("base_prompt_file"))
            # Built-ins are code-owned: their base_prompt (override) is editable,
            # but the origin marker blocks archiving them.  Operator personas
            # have no file to fall back on, so their only source can't be cleared.
            if builtin and "enabled" in fields and not fields["enabled"]:
                raise ValueError("cannot archive a built-in persona")
            if not builtin and "base_prompt" in values and not values.get("base_prompt"):
                raise ValueError("cannot clear base_prompt on an operator persona")
            if current["is_default"]:
                if "enabled" in fields and not fields["enabled"]:
                    raise ValueError("the default persona cannot be archived")
                if "is_default" in fields and not fields["is_default"]:
                    raise ValueError(
                        "cannot unset is_default directly; set it on the successor persona instead"
                    )
                if "applies_to_kinds" in fields and sorted(
                    fields["applies_to_kinds"] or []
                ) != sorted(current["applies_to_kinds"]):
                    raise ValueError("cannot change applies_to_kinds of the default persona")
            promote = bool(fields.get("is_default")) and not current["is_default"]
            promote_kinds = fields.get("applies_to_kinds", current["applies_to_kinds"])
            if promote:
                _validate_and_clear_default_persona(
                    conn,
                    personas,
                    persona_id=persona_id,
                    kinds=promote_kinds,
                    enabled=fields.get("enabled", current["enabled"]),
                    now=now,
                )
            values["updated"] = now
            conn.execute(
                sa.update(personas).where(personas.c.persona_id == persona_id).values(**values)
            )
            if promote:
                _assert_single_default_persona(conn, personas, promote_kinds[0])
            conn.commit()
            return True

    # -- Prompt policies -------------------------------------------------------

    def list_prompt_policies(self, org_id: str = "") -> list[dict[str, Any]]:

        with self._conn() as conn:
            q = sa.select(prompt_policies_t).order_by(prompt_policies_t.c.priority)
            if org_id:
                q = q.where(prompt_policies_t.c.org_id == org_id)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "enabled") for r in rows]

    def get_prompt_policy(self, policy_id: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(prompt_policies_t).where(prompt_policies_t.c.policy_id == policy_id)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled")

    def upsert_prompt_policy(self, policy: dict[str, Any]) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            existing = conn.execute(
                sa.select(prompt_policies_t).where(
                    prompt_policies_t.c.policy_id == policy["policy_id"]
                )
            ).fetchone()
            if existing:
                fields = {k: v for k, v in policy.items() if k in _PROMPT_POLICY_MUTABLE}
                fields["updated"] = now
                if "enabled" in fields:
                    fields["enabled"] = 1 if fields["enabled"] else 0
                conn.execute(
                    sa.update(prompt_policies_t)
                    .where(prompt_policies_t.c.policy_id == policy["policy_id"])
                    .values(**fields)
                )
            else:
                conn.execute(
                    sa.insert(prompt_policies_t),
                    {
                        "policy_id": policy["policy_id"],
                        "name": policy["name"],
                        "content": policy["content"],
                        "tool_gate": policy.get("tool_gate", ""),
                        "priority": policy.get("priority", 0),
                        "enabled": 1 if policy.get("enabled", True) else 0,
                        "org_id": policy.get("org_id", ""),
                        "created_by": policy.get("created_by", ""),
                        "created": now,
                        "updated": now,
                    },
                )
            conn.commit()

    def delete_prompt_policy(self, policy_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(prompt_policies_t).where(prompt_policies_t.c.policy_id == policy_id)
            )
            conn.commit()
            return result.rowcount > 0

    # -- Heuristic rules -------------------------------------------------------

    def create_heuristic_rule(
        self,
        rule_id: str,
        name: str,
        risk_level: str,
        confidence: float,
        recommendation: str,
        tool_pattern: str,
        arg_patterns: str = "[]",
        intent_template: str = "",
        reasoning_template: str = "",
        tier: str = "medium",
        priority: int = 0,
        builtin: bool = False,
        enabled: bool = True,
        created_by: str = "",
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(heuristic_rules).prefix_with("OR IGNORE"),
                {
                    "rule_id": rule_id,
                    "name": name,
                    "risk_level": risk_level,
                    "confidence": confidence,
                    "recommendation": recommendation,
                    "tool_pattern": tool_pattern,
                    "arg_patterns": arg_patterns,
                    "intent_template": intent_template,
                    "reasoning_template": reasoning_template,
                    "tier": tier,
                    "priority": priority,
                    "builtin": 1 if builtin else 0,
                    "enabled": 1 if enabled else 0,
                    "created_by": created_by,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_heuristic_rule(self, rule_id: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(heuristic_rules).where(heuristic_rules.c.rule_id == rule_id)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled", "builtin")

    def get_heuristic_rule_by_name(self, name: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(heuristic_rules).where(heuristic_rules.c.name == name)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled", "builtin")

    def list_heuristic_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:

        tier_order = sa.case(
            (heuristic_rules.c.tier == "critical", 0),
            (heuristic_rules.c.tier == "high", 1),
            (heuristic_rules.c.tier == "medium", 2),
            (heuristic_rules.c.tier == "low", 3),
            else_=4,
        )
        with self._conn() as conn:
            q = sa.select(heuristic_rules).order_by(tier_order, heuristic_rules.c.priority.desc())
            if enabled_only:
                q = q.where(heuristic_rules.c.enabled == 1)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "enabled", "builtin") for r in rows]

    def update_heuristic_rule(self, rule_id: str, **fields: Any) -> bool:

        fields = {k: v for k, v in fields.items() if k in _HEURISTIC_RULE_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if "builtin" in fields:
            fields["builtin"] = 1 if fields["builtin"] else 0
        with self._conn() as conn:
            result = conn.execute(
                sa.update(heuristic_rules)
                .where(heuristic_rules.c.rule_id == rule_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_heuristic_rule(self, rule_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(heuristic_rules).where(heuristic_rules.c.rule_id == rule_id)
            )
            conn.commit()
            return result.rowcount > 0

    # -- Output guard patterns -------------------------------------------------

    def create_output_guard_pattern(
        self,
        pattern_id: str,
        name: str,
        category: str,
        risk_level: str,
        pattern: str,
        flag_name: str,
        annotation: str,
        pattern_flags: str = "",
        is_credential: bool = False,
        redact_label: str = "",
        priority: int = 0,
        builtin: bool = False,
        enabled: bool = True,
        created_by: str = "",
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                sa.insert(output_guard_patterns).prefix_with("OR IGNORE"),
                {
                    "pattern_id": pattern_id,
                    "name": name,
                    "category": category,
                    "risk_level": risk_level,
                    "pattern": pattern,
                    "pattern_flags": pattern_flags,
                    "flag_name": flag_name,
                    "annotation": annotation,
                    "is_credential": 1 if is_credential else 0,
                    "redact_label": redact_label,
                    "priority": priority,
                    "builtin": 1 if builtin else 0,
                    "enabled": 1 if enabled else 0,
                    "created_by": created_by,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_output_guard_pattern(self, pattern_id: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(output_guard_patterns).where(
                    output_guard_patterns.c.pattern_id == pattern_id
                )
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled", "builtin", "is_credential")

    def get_output_guard_pattern_by_name(self, name: str) -> dict[str, Any] | None:

        with self._conn() as conn:
            row = conn.execute(
                sa.select(output_guard_patterns).where(output_guard_patterns.c.name == name)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled", "builtin", "is_credential")

    def list_output_guard_patterns(self, enabled_only: bool = False) -> list[dict[str, Any]]:

        with self._conn() as conn:
            q = sa.select(output_guard_patterns).order_by(
                output_guard_patterns.c.category, output_guard_patterns.c.priority.desc()
            )
            if enabled_only:
                q = q.where(output_guard_patterns.c.enabled == 1)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "enabled", "builtin", "is_credential") for r in rows]

    def update_output_guard_pattern(self, pattern_id: str, **fields: Any) -> bool:

        fields = {k: v for k, v in fields.items() if k in _OGP_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if "builtin" in fields:
            fields["builtin"] = 1 if fields["builtin"] else 0
        if "is_credential" in fields:
            fields["is_credential"] = 1 if fields["is_credential"] else 0
        with self._conn() as conn:
            result = conn.execute(
                sa.update(output_guard_patterns)
                .where(output_guard_patterns.c.pattern_id == pattern_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_output_guard_pattern(self, pattern_id: str) -> bool:

        with self._conn() as conn:
            result = conn.execute(
                sa.delete(output_guard_patterns).where(
                    output_guard_patterns.c.pattern_id == pattern_id
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- TLS / ACME ------------------------------------------------------------

    def save_tls_account_key(self, key_id: str, key_pem: str) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        stmt = sqlite_insert(tls_account_keys).values(id=key_id, key_pem=key_pem, created=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"key_pem": key_pem},
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def load_tls_account_key(self, key_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(tls_account_keys.c.key_pem).where(tls_account_keys.c.id == key_id)
            ).first()
            return row[0] if row else None

    def save_tls_ca(self, name: str, cert_pem: str, key_pem: str) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        stmt = sqlite_insert(tls_ca).values(
            name=name, cert_pem=cert_pem, key_pem=key_pem, created=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={"cert_pem": cert_pem, "key_pem": key_pem},
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def load_tls_ca(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sa.select(tls_ca).where(tls_ca.c.name == name)).first()
            if not row:
                return None
            return _row_to_dict(row)

    def save_tls_cert(
        self,
        domain: str,
        cert_pem: str,
        fullchain_pem: str,
        key_pem: str,
        issued_at: str,
        expires_at: str,
        meta: str | None = None,
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(tls_certificates).values(
            domain=domain,
            cert_pem=cert_pem,
            fullchain_pem=fullchain_pem,
            key_pem=key_pem,
            issued_at=issued_at,
            expires_at=expires_at,
            meta=meta,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["domain"],
            set_={
                "cert_pem": cert_pem,
                "fullchain_pem": fullchain_pem,
                "key_pem": key_pem,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "meta": meta,
            },
        )
        with self._conn() as conn:
            conn.execute(stmt)
            conn.commit()

    def load_tls_cert(self, domain: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                sa.select(tls_certificates).where(tls_certificates.c.domain == domain)
            ).first()
            if not row:
                return None
            return _row_to_dict(row)

    def list_tls_certs(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                sa.select(tls_certificates).order_by(tls_certificates.c.domain)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def delete_tls_cert(self, domain: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                sa.delete(tls_certificates).where(tls_certificates.c.domain == domain)
            )
            conn.commit()
            return result.rowcount > 0

    # -- Cross-node serialization ----------------------------------------------

    def acquire_advisory_lock_sync(self, key_text: str) -> contextlib.AbstractContextManager[None]:
        """SQLite is single-node — in-process ``asyncio.Lock`` suffices."""
        del key_text  # silenced: signature parity with the Postgres impl
        return contextlib.nullcontext()

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._engine.dispose()
