"""SQLite storage backend."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from turnstone.core.storage._schema import (
    api_tokens,
    conversations,
    memories,
    metadata,
    users,
    workstream_config,
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

    # -- Core conversation operations ------------------------------------------

    def save_message(
        self,
        ws_id: str,
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
                    "ws_id": ws_id,
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
            # Bump workstream updated timestamp
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(updated=now)
            )
            conn.commit()

    def load_messages(self, ws_id: str) -> list[dict[str, Any]]:
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
                .where(conversations.c.ws_id == ws_id)
                .order_by(conversations.c.id)
            ).fetchall()

        return _reconstruct_messages(list(rows), ws_id)

    # -- Workstream management -------------------------------------------------

    def list_workstreams_with_history(self, limit: int = 20) -> list[Any]:
        with self._engine.connect() as conn:
            return list(
                conn.execute(
                    sa.text(
                        "SELECT w.ws_id, w.alias, w.title, w.created, w.updated, "
                        "(SELECT COUNT(*) FROM conversations c "
                        " WHERE c.ws_id = w.ws_id), "
                        "w.node_id "
                        "FROM workstreams w "
                        "WHERE EXISTS "
                        "  (SELECT 1 FROM conversations c WHERE c.ws_id = w.ws_id) "
                        "ORDER BY w.updated DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                ).fetchall()
            )

    def prune_workstreams(self, retention_days: int = 90) -> tuple[int, int]:
        orphans = stale = 0
        with self._engine.connect() as conn:
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
                placeholders = ",".join([":p" + str(i) for i in range(len(orphan_ids))])
                params = {f"p{i}": oid for i, oid in enumerate(orphan_ids)}
                conn.execute(
                    sa.text(f"DELETE FROM workstream_config WHERE ws_id IN ({placeholders})"),
                    params,
                )
                result = conn.execute(
                    sa.text(f"DELETE FROM workstreams WHERE ws_id IN ({placeholders})"),
                    params,
                )
                orphans = result.rowcount

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
                    placeholders = ",".join([":p" + str(i) for i in range(len(stale_ids))])
                    params = {f"p{i}": sid for i, sid in enumerate(stale_ids)}
                    conn.execute(
                        sa.text(f"DELETE FROM workstream_config WHERE ws_id IN ({placeholders})"),
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
                    stale = result.rowcount

            conn.commit()
        return (orphans, stale)

    def resolve_workstream(self, alias_or_id: str) -> str | None:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            for key, value in config.items():
                conn.execute(
                    sa.text(
                        "INSERT OR REPLACE INTO workstream_config "
                        "(ws_id, key, value) VALUES (:wid, :key, :value)"
                    ),
                    {"wid": ws_id, "key": key, "value": value},
                )
            conn.commit()

    def load_workstream_config(self, ws_id: str) -> dict[str, str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(workstream_config.c.key, workstream_config.c.value).where(
                    workstream_config.c.ws_id == ws_id
                )
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    # -- Workstream metadata ---------------------------------------------------

    def set_workstream_alias(self, ws_id: str, alias: str) -> bool:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.alias, workstreams.c.title).where(
                    workstreams.c.ws_id == ws_id
                )
            ).fetchone()
            if row:
                value = row[0] or row[1]
                return str(value) if value is not None else None
        return None

    def update_workstream_title(self, ws_id: str, title: str) -> None:
        with self._engine.connect() as conn:
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(title=title)
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
        user_id: str | None = None,
        alias: str | None = None,
        title: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
            conn.execute(sa.delete(conversations).where(conversations.c.ws_id == ws_id))
            conn.execute(sa.delete(workstream_config).where(workstream_config.c.ws_id == ws_id))
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
                            "SELECT c.timestamp, c.ws_id, c.role, c.content, c.tool_name "
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
                        "SELECT timestamp, ws_id, role, content, tool_name "
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
                        "SELECT timestamp, ws_id, role, content, tool_name "
                        "FROM conversations ORDER BY timestamp DESC LIMIT :limit"
                    ),
                    {"limit": capped},
                ).fetchall()
            )

    # -- User identity operations -----------------------------------------------

    def create_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import channel_users

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import channel_routes

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import scheduled_tasks

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
                    "enabled": 1,
                    "created_by": created_by,
                    "next_run": next_run,
                    "created": now,
                    "updated": now,
                },
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
        # Normalize boolean → int for auto_approve
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
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from turnstone.core.storage._schema import services

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
        with self._engine.connect() as conn:
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


def _reconstruct_messages(rows: list[Any], ws_id: str) -> list[dict[str, Any]]:
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
                call_id = stored_tc_id or f"call_{ws_id}_{i}"
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
