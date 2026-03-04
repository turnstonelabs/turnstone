"""PostgreSQL storage backend."""

from __future__ import annotations

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
)
from turnstone.core.storage._sqlite import _reconstruct_messages

log = logging.getLogger(__name__)


class PostgreSQLBackend:
    """PostgreSQL implementation of the StorageBackend protocol."""

    def __init__(
        self, url: str, pool_size: int = 5, max_overflow: int = 10, *, create_tables: bool = True
    ) -> None:
        self._engine = sa.create_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
        )
        if create_tables:
            metadata.create_all(self._engine)

    # -- Core session operations -----------------------------------------------

    def register_session(self, session_id: str, title: str | None = None) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            # Use dialect-neutral upsert pattern
            existing = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.session_id == session_id)
            ).fetchone()
            if not existing:
                conn.execute(
                    sa.insert(sessions),
                    {"session_id": session_id, "title": title, "created": now, "updated": now},
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
            conn.execute(
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
                        " WHERE c.session_id = s.session_id) "
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
            orphan_rows = conn.execute(
                sa.text(
                    "SELECT session_id FROM sessions "
                    "WHERE NOT EXISTS "
                    "  (SELECT 1 FROM conversations c "
                    "   WHERE c.session_id = sessions.session_id)"
                )
            ).fetchall()
            orphan_ids = [r[0] for r in orphan_rows]
            if orphan_ids:
                conn.execute(
                    sa.delete(session_config).where(session_config.c.session_id.in_(orphan_ids))
                )
                result = conn.execute(
                    sa.delete(sessions).where(sessions.c.session_id.in_(orphan_ids))
                )
                orphans = result.rowcount

            # 2. Remove old unnamed sessions
            if retention_days > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                stale_rows = conn.execute(
                    sa.select(sessions.c.session_id).where(
                        sessions.c.alias.is_(None),
                        sessions.c.updated < cutoff,
                    )
                ).fetchall()
                stale_ids = [r[0] for r in stale_rows]
                if stale_ids:
                    conn.execute(
                        sa.delete(session_config).where(session_config.c.session_id.in_(stale_ids))
                    )
                    result = conn.execute(
                        sa.delete(sessions).where(sessions.c.session_id.in_(stale_ids))
                    )
                    stale = result.rowcount

            conn.commit()
        return (orphans, stale)

    def resolve_session(self, alias_or_id: str) -> str | None:
        with self._engine.connect() as conn:
            # 1. Exact alias
            row = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.alias == alias_or_id)
            ).fetchone()
            if row:
                return str(row[0])
            # 2. Exact session_id
            row = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.session_id == alias_or_id)
            ).fetchone()
            if row:
                return str(row[0])
            # 3. Prefix match
            rows = conn.execute(
                sa.select(sessions.c.session_id).where(
                    sessions.c.session_id.like(alias_or_id + "%")
                )
            ).fetchall()
            if len(rows) == 1:
                return str(rows[0][0])
            # 4. Legacy: check conversations
            row = conn.execute(
                sa.select(sa.distinct(conversations.c.session_id))
                .where(conversations.c.session_id == alias_or_id)
                .limit(1)
            ).fetchone()
            if row:
                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
                existing = conn.execute(
                    sa.select(sessions.c.session_id).where(sessions.c.session_id == row[0])
                ).fetchone()
                if not existing:
                    conn.execute(
                        sa.insert(sessions),
                        {"session_id": row[0], "created": now, "updated": now},
                    )
                    conn.commit()
                return str(row[0])
            return None

    # -- Session config --------------------------------------------------------

    def save_session_config(self, session_id: str, config: dict[str, str]) -> None:
        with self._engine.connect() as conn:
            for key, value in config.items():
                # Upsert: delete + insert
                conn.execute(
                    sa.delete(session_config).where(
                        session_config.c.session_id == session_id,
                        session_config.c.key == key,
                    )
                )
                conn.execute(
                    sa.insert(session_config),
                    {"session_id": session_id, "key": key, "value": value},
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
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            existing = conn.execute(
                sa.select(memories.c.value, memories.c.created).where(memories.c.key == key)
            ).fetchone()
            old_value = str(existing[0]) if existing else None
            created = str(existing[1]) if existing else now
            # Delete + insert for cross-dialect upsert
            conn.execute(sa.delete(memories).where(memories.c.key == key))
            conn.execute(
                sa.insert(memories),
                {"key": key, "value": value, "created": created, "updated": now},
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
            clauses = []
            params: dict[str, str] = {}
            for i, t in enumerate(terms):
                clauses.append(f"(key ILIKE :k{i} OR value ILIKE :v{i})")
                params[f"k{i}"] = f"%{t}%"
                params[f"v{i}"] = f"%{t}%"
            rows = conn.execute(
                sa.text(
                    "SELECT key, value FROM memories WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY key"
                ),
                params,
            ).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

    # -- Conversation search ---------------------------------------------------

    def search_history(self, query: str, limit: int = 20) -> list[Any]:
        if not query or not query.strip():
            return []
        capped = min(limit, 100)
        with self._engine.connect() as conn:
            # Use PostgreSQL full-text search if search_vector column exists
            try:
                return list(
                    conn.execute(
                        sa.text(
                            "SELECT c.timestamp, c.session_id, c.role, c.content, c.tool_name "
                            "FROM conversations c "
                            "WHERE to_tsvector('english', COALESCE(c.content, '')) "
                            "   @@ plainto_tsquery('english', :query) "
                            "ORDER BY ts_rank(to_tsvector('english', COALESCE(c.content, '')), "
                            "   plainto_tsquery('english', :query)) DESC "
                            "LIMIT :limit"
                        ),
                        {"query": query, "limit": capped},
                    ).fetchall()
                )
            except Exception:
                # Fallback to ILIKE
                return list(
                    conn.execute(
                        sa.text(
                            "SELECT timestamp, session_id, role, content, tool_name "
                            "FROM conversations WHERE content ILIKE :pattern "
                            "ORDER BY timestamp DESC LIMIT :limit"
                        ),
                        {"pattern": f"%{query}%", "limit": capped},
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
