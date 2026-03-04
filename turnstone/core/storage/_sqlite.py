"""SQLite storage backend."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from turnstone.core.storage._schema import (
    conversations,
    memories,
    metadata,
    session_config,
    sessions,
    workstreams,
)

log = logging.getLogger(__name__)


def _escape_like(s: str) -> str:
    """Escape LIKE metacharacters for use with ESCAPE '\\\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts5_query(query: str) -> str:
    """Convert a plain search string into a safe FTS5 query."""
    terms = query.split()
    safe = []
    for t in terms:
        if t:
            safe.append(f'"{t.replace(chr(34), chr(34) + chr(34))}"')
    return " ".join(safe)


class SQLiteBackend:
    """SQLite implementation of the StorageBackend protocol."""

    def __init__(self, path: str, *, create_tables: bool = True) -> None:
        self._path = path
        self._engine = sa.create_engine(
            f"sqlite:///{path}",
            pool_pre_ping=True,
            connect_args={"check_same_thread": False},
        )
        self._fts5_available = False
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

    # -- Core session operations -----------------------------------------------

    def register_session(
        self,
        session_id: str,
        title: str | None = None,
        node_id: str | None = None,
        ws_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                sa.insert(sessions).prefix_with("OR IGNORE"),
                {
                    "session_id": session_id,
                    "title": title,
                    "node_id": node_id,
                    "ws_id": ws_id,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str | None,
        tool_name: str | None = None,
        tool_args: str | None = None,
        tool_call_id: str | None = None,
        provider_data: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.insert(conversations),
                {
                    "session_id": session_id,
                    "timestamp": now,
                    "role": role,
                    "content": content,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_call_id": tool_call_id,
                    "provider_data": provider_data,
                },
            )
            # FTS5 indexing
            if self._fts5_available and content:
                try:
                    rowid = result.lastrowid
                    conn.execute(
                        sa.text(
                            "INSERT INTO conversations_fts(rowid, content) VALUES (:rowid, :content)"
                        ),
                        {"rowid": rowid, "content": content},
                    )
                except Exception:
                    self._fts5_available = False
            # Bump session updated timestamp
            conn.execute(
                sa.update(sessions).where(sessions.c.session_id == session_id).values(updated=now)
            )
            conn.commit()

    def load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    conversations.c.role,
                    conversations.c.content,
                    conversations.c.tool_name,
                    conversations.c.tool_args,
                    conversations.c.tool_call_id,
                    conversations.c.provider_data,
                )
                .where(conversations.c.session_id == session_id)
                .order_by(conversations.c.id)
            ).fetchall()

        return _reconstruct_messages(list(rows), session_id)

    # -- Session management ----------------------------------------------------

    def list_sessions(self, limit: int = 20) -> list[Any]:
        with self._engine.connect() as conn:
            return list(
                conn.execute(
                    sa.text(
                        "SELECT s.session_id, s.alias, s.title, s.created, s.updated, "
                        "(SELECT COUNT(*) FROM conversations c "
                        " WHERE c.session_id = s.session_id), "
                        "s.node_id, s.ws_id "
                        "FROM sessions s "
                        "WHERE EXISTS "
                        "  (SELECT 1 FROM conversations c WHERE c.session_id = s.session_id) "
                        "ORDER BY s.updated DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                ).fetchall()
            )

    def delete_session(self, session_id: str) -> bool:
        with self._engine.connect() as conn:
            conn.execute(sa.delete(conversations).where(conversations.c.session_id == session_id))
            conn.execute(sa.delete(session_config).where(session_config.c.session_id == session_id))
            conn.execute(sa.delete(sessions).where(sessions.c.session_id == session_id))
            conn.commit()
            return True

    def prune_sessions(self, retention_days: int = 90) -> tuple[int, int]:
        orphans = stale = 0
        with self._engine.connect() as conn:
            # 1. Remove sessions with no messages
            orphan_ids = [
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT session_id FROM sessions "
                        "WHERE NOT EXISTS "
                        "  (SELECT 1 FROM conversations c "
                        "   WHERE c.session_id = sessions.session_id)"
                    )
                ).fetchall()
            ]
            if orphan_ids:
                placeholders = ",".join([":p" + str(i) for i in range(len(orphan_ids))])
                params = {f"p{i}": oid for i, oid in enumerate(orphan_ids)}
                conn.execute(
                    sa.text(f"DELETE FROM session_config WHERE session_id IN ({placeholders})"),
                    params,
                )
                result = conn.execute(
                    sa.text(f"DELETE FROM sessions WHERE session_id IN ({placeholders})"),
                    params,
                )
                orphans = result.rowcount

            # 2. Remove old unnamed sessions
            if retention_days > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                stale_ids = [
                    row[0]
                    for row in conn.execute(
                        sa.text(
                            "SELECT session_id FROM sessions "
                            "WHERE alias IS NULL AND updated < :cutoff"
                        ),
                        {"cutoff": cutoff},
                    ).fetchall()
                ]
                if stale_ids:
                    placeholders = ",".join([":p" + str(i) for i in range(len(stale_ids))])
                    params = {f"p{i}": sid for i, sid in enumerate(stale_ids)}
                    conn.execute(
                        sa.text(f"DELETE FROM session_config WHERE session_id IN ({placeholders})"),
                        params,
                    )
                    result = conn.execute(
                        sa.text(f"DELETE FROM sessions WHERE session_id IN ({placeholders})"),
                        params,
                    )
                    stale = result.rowcount

            conn.commit()
        return (orphans, stale)

    def resolve_session(self, alias_or_id: str) -> str | None:
        with self._engine.connect() as conn:
            # 1. Exact alias match
            row = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.alias == alias_or_id)
            ).fetchone()
            if row:
                return str(row[0])
            # 2. Exact session_id match
            row = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.session_id == alias_or_id)
            ).fetchone()
            if row:
                return str(row[0])
            # 3. Session_id prefix match
            rows = conn.execute(
                sa.select(sessions.c.session_id).where(
                    sessions.c.session_id.like(alias_or_id + "%")
                )
            ).fetchall()
            if len(rows) == 1:
                return str(rows[0][0])
            # 4. Legacy: check conversations table
            row = conn.execute(
                sa.text(
                    "SELECT DISTINCT session_id FROM conversations WHERE session_id = :sid LIMIT 1"
                ),
                {"sid": alias_or_id},
            ).fetchone()
            if row:
                # Auto-register legacy session
                conn.execute(
                    sa.text(
                        "INSERT OR IGNORE INTO sessions "
                        "(session_id, created, updated) VALUES ("
                        ":sid, "
                        "(SELECT MIN(timestamp) FROM conversations WHERE session_id = :sid), "
                        "(SELECT MAX(timestamp) FROM conversations WHERE session_id = :sid))"
                    ),
                    {"sid": row[0]},
                )
                conn.commit()
                return str(row[0])
            return None

    # -- Session config --------------------------------------------------------

    def save_session_config(self, session_id: str, config: dict[str, str]) -> None:
        with self._engine.connect() as conn:
            for key, value in config.items():
                conn.execute(
                    sa.text(
                        "INSERT OR REPLACE INTO session_config "
                        "(session_id, key, value) VALUES (:sid, :key, :value)"
                    ),
                    {"sid": session_id, "key": key, "value": value},
                )
            conn.commit()

    def load_session_config(self, session_id: str) -> dict[str, str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(session_config.c.key, session_config.c.value).where(
                    session_config.c.session_id == session_id
                )
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    # -- Session metadata ------------------------------------------------------

    def set_session_alias(self, session_id: str, alias: str) -> bool:
        with self._engine.connect() as conn:
            existing = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.alias == alias)
            ).fetchone()
            if existing and existing[0] != session_id:
                return False
            conn.execute(
                sa.update(sessions).where(sessions.c.session_id == session_id).values(alias=alias)
            )
            conn.commit()
            return True

    def get_session_name(self, session_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(sessions.c.alias, sessions.c.title).where(
                    sessions.c.session_id == session_id
                )
            ).fetchone()
            if row:
                value = row[0] or row[1]
                return str(value) if value is not None else None
        return None

    def update_session_title(self, session_id: str, title: str) -> None:
        with self._engine.connect() as conn:
            conn.execute(
                sa.update(sessions).where(sessions.c.session_id == session_id).values(title=title)
            )
            conn.commit()

    # -- Generic key-value store -----------------------------------------------

    def kv_get(self, key: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(memories.c.value).where(memories.c.key == key)).fetchone()
            return str(row[0]) if row else None

    def kv_set(self, key: str, value: str) -> str | None:
        with self._engine.connect() as conn:
            existing = conn.execute(
                sa.select(memories.c.value).where(memories.c.key == key)
            ).fetchone()
            old_value = str(existing[0]) if existing else None
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                sa.text(
                    "INSERT OR REPLACE INTO memories (key, value, created, updated) "
                    "VALUES (:key, :value, "
                    "COALESCE((SELECT created FROM memories WHERE key = :key), :now), "
                    ":now)"
                ),
                {"key": key, "value": value, "now": now},
            )
            conn.commit()
            return old_value

    def kv_delete(self, key: str) -> bool:
        with self._engine.connect() as conn:
            result = conn.execute(sa.delete(memories).where(memories.c.key == key))
            conn.commit()
            return result.rowcount > 0

    def kv_list(self) -> list[tuple[str, str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(memories.c.key, memories.c.value).order_by(memories.c.key)
            ).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

    def kv_search(self, query: str) -> list[tuple[str, str]]:
        if not query or not query.strip():
            return self.kv_list()
        terms = query.split()
        with self._engine.connect() as conn:
            # Build WHERE clause: each term must match key OR value
            clauses = []
            params: dict[str, str] = {}
            for i, t in enumerate(terms):
                escaped = _escape_like(t)
                clauses.append(f"(key LIKE :k{i} ESCAPE '\\' OR value LIKE :v{i} ESCAPE '\\')")
                params[f"k{i}"] = f"%{escaped}%"
                params[f"v{i}"] = f"%{escaped}%"
            rows = conn.execute(
                sa.text(
                    "SELECT key, value FROM memories WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY key"
                ),
                params,
            ).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

    # -- Workstream operations -------------------------------------------------

    def register_workstream(
        self,
        ws_id: str,
        node_id: str | None = None,
        name: str = "",
        state: str = "idle",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                sa.insert(workstreams).prefix_with("OR IGNORE"),
                {
                    "ws_id": ws_id,
                    "node_id": node_id,
                    "name": name,
                    "state": state,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.ws_id == ws_id)
                .values(state=state, updated=now)
            )
            conn.commit()

    def update_workstream_name(self, ws_id: str, name: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.ws_id == ws_id)
                .values(name=name, updated=now)
            )
            conn.commit()

    def delete_workstream(self, ws_id: str) -> bool:
        with self._engine.connect() as conn:
            result = conn.execute(sa.delete(workstreams).where(workstreams.c.ws_id == ws_id))
            conn.commit()
            return result.rowcount > 0

    def list_workstreams(self, node_id: str | None = None, limit: int = 100) -> list[Any]:
        with self._engine.connect() as conn:
            q = (
                sa.select(
                    workstreams.c.ws_id,
                    workstreams.c.node_id,
                    workstreams.c.name,
                    workstreams.c.state,
                    workstreams.c.created,
                    workstreams.c.updated,
                )
                .order_by(workstreams.c.updated.desc())
                .limit(limit)
            )
            if node_id is not None:
                q = q.where(workstreams.c.node_id == node_id)
            return list(conn.execute(q).fetchall())

    # -- Conversation search ---------------------------------------------------

    def search_history(self, query: str, limit: int = 20) -> list[Any]:
        if not query or not query.strip():
            return []
        capped = min(limit, 100)
        with self._engine.connect() as conn:
            if self._fts5_available:
                return list(
                    conn.execute(
                        sa.text(
                            "SELECT c.timestamp, c.session_id, c.role, c.content, c.tool_name "
                            "FROM conversations_fts f "
                            "JOIN conversations c ON c.id = f.rowid "
                            "WHERE conversations_fts MATCH :query "
                            "ORDER BY f.rank ASC LIMIT :limit"
                        ),
                        {"query": _fts5_query(query), "limit": capped},
                    ).fetchall()
                )
            return list(
                conn.execute(
                    sa.text(
                        "SELECT timestamp, session_id, role, content, tool_name "
                        "FROM conversations WHERE content LIKE :pattern ESCAPE '\\' "
                        "ORDER BY timestamp DESC LIMIT :limit"
                    ),
                    {"pattern": f"%{_escape_like(query)}%", "limit": capped},
                ).fetchall()
            )

    def search_history_recent(self, limit: int = 20) -> list[Any]:
        capped = min(limit, 100)
        with self._engine.connect() as conn:
            return list(
                conn.execute(
                    sa.text(
                        "SELECT timestamp, session_id, role, content, tool_name "
                        "FROM conversations ORDER BY timestamp DESC LIMIT :limit"
                    ),
                    {"limit": capped},
                ).fetchall()
            )

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._engine.dispose()


def _reconstruct_messages(rows: list[Any], session_id: str) -> list[dict[str, Any]]:
    """Reconstruct OpenAI message format from stored conversation rows.

    Handles tool_call / tool_result grouping and incomplete turn repair.
    """
    messages: list[dict[str, Any]] = []
    i = 0
    while i < len(rows):
        role, content, tool_name, tool_args, tc_id, provider_data = rows[i]

        if role == "user":
            messages.append({"role": "user", "content": content or ""})
            i += 1

        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": content}
            if provider_data:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    msg["_provider_content"] = json.loads(provider_data)
            messages.append(msg)
            i += 1

        elif role == "tool_call":
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
            if (
                messages
                and messages[-1]["role"] == "assistant"
                and not messages[-1].get("tool_calls")
            ):
                assistant_msg = messages.pop()
                assistant_msg["tool_calls"] = []

            while i < len(rows) and rows[i][0] == "tool_call":
                _, _, tn, ta, stored_tc_id, _ = rows[i]
                call_id = stored_tc_id or f"call_{session_id}_{i}"
                assistant_msg["tool_calls"].append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tn or "", "arguments": ta or ""},
                    }
                )
                i += 1
            messages.append(assistant_msg)

            # Consume matching tool_result rows
            result_idx = 0
            while i < len(rows) and rows[i][0] == "tool_result":
                _, result_content, _, _, result_tc_id, _ = rows[i]
                if result_tc_id:
                    tc_id_to_use = result_tc_id
                elif result_idx < len(assistant_msg["tool_calls"]):
                    tc_id_to_use = assistant_msg["tool_calls"][result_idx]["id"]
                else:
                    tc_id_to_use = f"call_orphan_{i}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id_to_use,
                        "content": result_content or "",
                    }
                )
                result_idx += 1
                i += 1

        elif role == "tool_result":
            # Orphaned tool_result (no preceding tool_call) — skip
            i += 1
        else:
            i += 1

    # Repair: strip trailing incomplete tool call turns
    while messages:
        tail_tools = 0
        for j in range(len(messages) - 1, -1, -1):
            if messages[j].get("role") == "tool":
                tail_tools += 1
            else:
                break
        asst_idx = len(messages) - 1 - tail_tools
        if asst_idx < 0:
            break
        asst = messages[asst_idx]
        if asst.get("role") != "assistant" or not asst.get("tool_calls"):
            break
        if tail_tools >= len(asst["tool_calls"]):
            break
        del messages[asst_idx:]

    return messages
