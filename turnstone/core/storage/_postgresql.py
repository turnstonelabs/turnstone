"""PostgreSQL storage backend."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from turnstone.core.storage._schema import (
    api_tokens,
    conversations,
    memories,
    metadata,
    session_config,
    sessions,
    users,
    workstreams,
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

    def register_session(
        self,
        session_id: str,
        title: str | None = None,
        node_id: str | None = None,
        ws_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            # Use dialect-neutral upsert pattern
            existing = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.session_id == session_id)
            ).fetchone()
            if not existing:
                conn.execute(
                    sa.insert(sessions),
                    {
                        "session_id": session_id,
                        "title": title,
                        "node_id": node_id,
                        "ws_id": ws_id,
                        "user_id": user_id,
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

    # -- Workstream operations -------------------------------------------------

    def register_workstream(
        self,
        ws_id: str,
        node_id: str | None = None,
        name: str = "",
        state: str = "idle",
        user_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            existing = conn.execute(
                sa.select(workstreams.c.ws_id).where(workstreams.c.ws_id == ws_id)
            ).fetchone()
            if not existing:
                conn.execute(
                    sa.insert(workstreams),
                    {
                        "ws_id": ws_id,
                        "node_id": node_id,
                        "user_id": user_id,
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

    # -- Session lookup by workstream ------------------------------------------

    def get_session_id_by_ws(self, ws_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(sessions.c.session_id).where(sessions.c.ws_id == ws_id)
            ).fetchone()
            return str(row[0]) if row else None

    # -- User identity operations -----------------------------------------------

    def create_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            existing = conn.execute(
                sa.select(users.c.user_id).where(users.c.user_id == user_id)
            ).fetchone()
            if not existing:
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
            conn.commit()

    def create_first_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> bool:
        """Atomically create a user only if no users exist. Returns True if created."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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

    def delete_user(self, user_id: str) -> bool:
        from turnstone.core.storage._schema import channel_users

        with self._engine.connect() as conn:
            conn.execute(sa.delete(channel_users).where(channel_users.c.user_id == user_id))
            conn.execute(sa.delete(api_tokens).where(api_tokens.c.user_id == user_id))
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            result = conn.execute(sa.delete(api_tokens).where(api_tokens.c.token_id == token_id))
            conn.commit()
            return result.rowcount > 0

    # -- Channel user mapping ---------------------------------------------------

    def create_channel_user(self, channel_type: str, channel_user_id: str, user_id: str) -> None:
        from sqlalchemy.dialects import postgresql

        from turnstone.core.storage._schema import channel_users

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                postgresql.insert(channel_users)
                .values(
                    channel_type=channel_type,
                    channel_user_id=channel_user_id,
                    user_id=user_id,
                    created=now,
                )
                .on_conflict_do_nothing()
            )
            conn.commit()

    def get_channel_user(self, channel_type: str, channel_user_id: str) -> dict[str, str] | None:
        from turnstone.core.storage._schema import channel_users

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import channel_users

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import channel_users

        with self._engine.connect() as conn:
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
        from sqlalchemy.dialects import postgresql

        from turnstone.core.storage._schema import channel_routes

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                postgresql.insert(channel_routes)
                .values(
                    channel_type=channel_type,
                    channel_id=channel_id,
                    ws_id=ws_id,
                    node_id=node_id,
                    created=now,
                )
                .on_conflict_do_nothing()
            )
            conn.commit()

    def get_channel_route(self, channel_type: str, channel_id: str) -> dict[str, str] | None:
        from turnstone.core.storage._schema import channel_routes

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import channel_routes

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import channel_routes

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import channel_routes

        with self._engine.connect() as conn:
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
    ) -> None:
        from sqlalchemy.dialects import postgresql

        from turnstone.core.storage._schema import scheduled_tasks

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                postgresql.insert(scheduled_tasks)
                .values(
                    task_id=task_id,
                    name=name,
                    description=description,
                    schedule_type=schedule_type,
                    cron_expr=cron_expr,
                    at_time=at_time,
                    target_mode=target_mode,
                    model=model,
                    initial_message=initial_message,
                    auto_approve=1 if auto_approve else 0,
                    auto_approve_tools=",".join(auto_approve_tools),
                    enabled=1,
                    created_by=created_by,
                    next_run=next_run,
                    created=now,
                    updated=now,
                )
                .on_conflict_do_nothing()
            )
            conn.commit()

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        from turnstone.core.storage._schema import scheduled_tasks

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(scheduled_tasks).where(scheduled_tasks.c.task_id == task_id)
            ).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        from turnstone.core.storage._schema import scheduled_tasks

        with self._engine.connect() as conn:
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
            "enabled",
            "last_run",
            "next_run",
            "updated",
        }
    )

    def update_scheduled_task(self, task_id: str, **fields: Any) -> bool:
        from turnstone.core.storage._schema import scheduled_tasks

        fields = {k: v for k, v in fields.items() if k in self._UPDATABLE_TASK_FIELDS}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "auto_approve" in fields:
            fields["auto_approve"] = 1 if fields["auto_approve"] else 0
        if "auto_approve_tools" in fields and isinstance(fields["auto_approve_tools"], list):
            fields["auto_approve_tools"] = ",".join(fields["auto_approve_tools"])
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(scheduled_tasks)
                .where(scheduled_tasks.c.task_id == task_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_scheduled_task(self, task_id: str) -> bool:
        from turnstone.core.storage._schema import scheduled_task_runs, scheduled_tasks

        with self._engine.connect() as conn:
            conn.execute(
                sa.delete(scheduled_task_runs).where(scheduled_task_runs.c.task_id == task_id)
            )
            result = conn.execute(
                sa.delete(scheduled_tasks).where(scheduled_tasks.c.task_id == task_id)
            )
            conn.commit()
            return result.rowcount > 0

    def list_due_tasks(self, now: str) -> list[dict[str, Any]]:
        from turnstone.core.storage._schema import scheduled_tasks

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import scheduled_task_runs

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import scheduled_task_runs

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(scheduled_task_runs)
                .where(scheduled_task_runs.c.task_id == task_id)
                .order_by(scheduled_task_runs.c.started.desc())
                .limit(limit)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def prune_task_runs(self, retention_days: int = 90) -> int:
        from datetime import timedelta

        from turnstone.core.storage._schema import scheduled_task_runs

        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(scheduled_task_runs).where(scheduled_task_runs.c.started < cutoff)
            )
            conn.commit()
            return result.rowcount

    # -- Service registry ------------------------------------------------------

    def register_service(
        self, service_type: str, service_id: str, url: str, metadata: str = "{}"
    ) -> None:
        from turnstone.core.storage._schema import services

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(services).values(
                service_type=service_type,
                service_id=service_id,
                url=url,
                metadata=metadata,
                last_heartbeat=now,
                created=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[services.c.service_type, services.c.service_id],
                set_={"url": url, "metadata": metadata, "last_heartbeat": now},
            )
            conn.execute(stmt)
            conn.commit()

    def heartbeat_service(self, service_type: str, service_id: str) -> bool:
        from turnstone.core.storage._schema import services

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import services

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import services

        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(services).where(
                    (services.c.service_type == service_type)
                    & (services.c.service_id == service_id)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._engine.dispose()
