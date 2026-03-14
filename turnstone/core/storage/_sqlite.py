"""SQLite storage backend."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from turnstone.core.storage._schema import (
    api_tokens,
    audit_events,
    conversations,
    intent_verdicts,
    metadata,
    orgs,
    prompt_templates,
    roles,
    structured_memories,
    tool_policies,
    usage_events,
    user_roles,
    users,
    workstream_config,
    workstream_template_versions,
    workstream_templates,
    workstreams,
)
from turnstone.core.storage._utils import (
    ORG_MUTABLE as _ORG_MUTABLE,
)
from turnstone.core.storage._utils import (
    POLICY_MUTABLE as _POLICY_MUTABLE,
)
from turnstone.core.storage._utils import (
    ROLE_MUTABLE as _ROLE_MUTABLE,
)
from turnstone.core.storage._utils import (
    STRUCTURED_MEMORY_MUTABLE as _SMEM_MUTABLE,
)
from turnstone.core.storage._utils import (
    TEMPLATE_MUTABLE as _TEMPLATE_MUTABLE,
)
from turnstone.core.storage._utils import (
    VERDICT_MUTABLE as _VERDICT_MUTABLE,
)
from turnstone.core.storage._utils import (
    WS_TEMPLATE_MUTABLE as _WS_TEMPLATE_MUTABLE,
)
from turnstone.core.storage._utils import (
    reconstruct_messages as _reconstruct_messages,
)
from turnstone.core.storage._utils import (
    row_to_dict as _row_to_dict,
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
        tool_calls: str | None = None,
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
                    "tool_calls": tool_calls,
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
                    conversations.c.tool_calls,
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
        ws_template_id: str = "",
        ws_template_version: int = 0,
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
                    "ws_template_id": ws_template_id,
                    "ws_template_version": ws_template_version,
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

    def update_workstream_template(
        self, ws_id: str, ws_template_id: str, ws_template_version: int
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.ws_id == ws_id)
                .values(
                    ws_template_id=ws_template_id,
                    ws_template_version=ws_template_version,
                    updated=now,
                )
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
            conn.execute(sa.delete(user_roles).where(user_roles.c.user_id == user_id))
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
        template: str = "",
        ws_template: str = "",
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
                    "template": template,
                    "ws_template": ws_template,
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
            "template",
            "ws_template",
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
        from turnstone.core.storage._schema import watches

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import watches

        with self._engine.connect() as conn:
            row = conn.execute(sa.select(watches).where(watches.c.watch_id == watch_id)).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_watches_for_ws(self, ws_id: str) -> list[dict[str, Any]]:
        from turnstone.core.storage._schema import watches

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(watches)
                .where((watches.c.ws_id == ws_id) & (watches.c.active == 1))
                .order_by(watches.c.created.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def list_watches_for_node(self, node_id: str) -> list[dict[str, Any]]:
        from turnstone.core.storage._schema import watches

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(watches)
                .where((watches.c.node_id == node_id) & (watches.c.active == 1))
                .order_by(watches.c.created.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def list_due_watches(self, now: str) -> list[dict[str, Any]]:
        from turnstone.core.storage._schema import watches

        with self._engine.connect() as conn:
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
        from turnstone.core.storage._schema import watches

        fields = {k: v for k, v in fields.items() if k in self._UPDATABLE_WATCH_FIELDS}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "active" in fields:
            fields["active"] = 1 if fields["active"] else 0
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(watches).where(watches.c.watch_id == watch_id).values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_watch(self, watch_id: str) -> bool:
        from turnstone.core.storage._schema import watches

        with self._engine.connect() as conn:
            result = conn.execute(sa.delete(watches).where(watches.c.watch_id == watch_id))
            conn.commit()
            return result.rowcount > 0

    def delete_watches_for_ws(self, ws_id: str) -> int:
        from turnstone.core.storage._schema import watches

        with self._engine.connect() as conn:
            result = conn.execute(sa.delete(watches).where(watches.c.ws_id == ws_id))
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(roles).where(roles.c.role_id == role_id)).fetchone()
            if row:
                return _row_to_dict(row, "builtin")
            return None

    def get_role_by_name(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(roles).where(roles.c.name == name)).fetchone()
            if row:
                return _row_to_dict(row, "builtin")
            return None

    def list_roles(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(roles).where(roles.c.role_id == role_id).values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_role(self, role_id: str) -> bool:
        with self._engine.connect() as conn:
            conn.execute(sa.delete(user_roles).where(user_roles.c.role_id == role_id))
            result = conn.execute(sa.delete(roles).where(roles.c.role_id == role_id))
            conn.commit()
            return result.rowcount > 0

    def assign_role(self, user_id: str, role_id: str, assigned_by: str = "") -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(user_roles).where(
                    (user_roles.c.user_id == user_id) & (user_roles.c.role_id == role_id)
                )
            )
            conn.commit()
            return result.rowcount > 0

    def list_user_roles(self, user_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
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

    def get_user_permissions(self, user_id: str) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(roles.c.permissions)
                .select_from(user_roles.join(roles, user_roles.c.role_id == roles.c.role_id))
                .where(user_roles.c.user_id == user_id)
            ).fetchall()
            perms: set[str] = set()
            for r in rows:
                if r[0]:
                    for p in r[0].split(","):
                        p = p.strip()
                        if p:
                            perms.add(p)
            return perms

    # -- Organizations ---------------------------------------------------------

    def create_org(self, org_id: str, name: str, display_name: str, settings: str = "{}") -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(orgs).where(orgs.c.org_id == org_id)).fetchone()
            if row:
                return _row_to_dict(row)
            return None

    def list_orgs(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(sa.select(orgs).order_by(orgs.c.name)).fetchall()
            return [_row_to_dict(r) for r in rows]

    def update_org(self, org_id: str, **fields: Any) -> bool:
        dropped = set(fields) - _ORG_MUTABLE
        if dropped:
            log.warning("update_org: ignoring unknown fields: %s", dropped)
        fields = {k: v for k, v in fields.items() if k in _ORG_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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

    def get_tool_policy(self, policy_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(tool_policies).where(tool_policies.c.policy_id == policy_id)
            ).fetchone()
            if row:
                return _row_to_dict(row, "enabled")
            return None

    def list_tool_policies(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(tool_policies)
                .where(tool_policies.c.policy_id == policy_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_tool_policy(self, policy_id: str) -> bool:
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(tool_policies).where(tool_policies.c.policy_id == policy_id)
            )
            conn.commit()
            return result.rowcount > 0

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
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_prompt_template(self, template_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(prompt_templates).where(prompt_templates.c.template_id == template_id)
            ).fetchone()
            if row:
                return _row_to_dict(row, "is_default", "readonly")
            return None

    def get_prompt_template_by_name(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(prompt_templates).where(prompt_templates.c.name == name)
            ).fetchone()
            if row:
                return _row_to_dict(row, "is_default", "readonly")
            return None

    def list_prompt_templates(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            q = sa.select(prompt_templates).order_by(prompt_templates.c.name)
            if org_id:
                q = q.where(prompt_templates.c.org_id == org_id)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "is_default", "readonly") for r in rows]

    def list_default_templates(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            q = (
                sa.select(prompt_templates)
                .where(prompt_templates.c.is_default == 1)
                .order_by(prompt_templates.c.name)
            )
            if org_id:
                q = q.where(prompt_templates.c.org_id == org_id)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "is_default", "readonly") for r in rows]

    def list_prompt_templates_by_origin(self, origin: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(prompt_templates)
                .where(prompt_templates.c.origin == origin)
                .order_by(prompt_templates.c.name)
            ).fetchall()
            return [_row_to_dict(r, "is_default", "readonly") for r in rows]

    def update_prompt_template(self, template_id: str, **fields: Any) -> bool:
        dropped = set(fields) - _TEMPLATE_MUTABLE
        if dropped:
            log.warning("update_prompt_template: ignoring unknown fields: %s", dropped)
        fields = {k: v for k, v in fields.items() if k in _TEMPLATE_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "is_default" in fields:
            fields["is_default"] = int(fields["is_default"])
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(prompt_templates)
                .where(prompt_templates.c.template_id == template_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_prompt_template(self, template_id: str) -> bool:
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(prompt_templates).where(prompt_templates.c.template_id == template_id)
            )
            conn.commit()
            return result.rowcount > 0

    # -- Workstream templates --------------------------------------------------

    def create_ws_template(
        self,
        ws_template_id: str,
        name: str,
        description: str = "",
        system_prompt: str = "",
        prompt_template: str = "",
        prompt_template_hash: str = "",
        model: str = "",
        auto_approve: bool = False,
        auto_approve_tools: str = "",
        temperature: float | None = None,
        reasoning_effort: str = "",
        max_tokens: int | None = None,
        token_budget: int = 0,
        agent_max_turns: int | None = None,
        notify_on_complete: str = "{}",
        org_id: str = "",
        created_by: str = "",
        enabled: bool = True,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                sa.insert(workstream_templates),
                {
                    "ws_template_id": ws_template_id,
                    "name": name,
                    "description": description,
                    "system_prompt": system_prompt,
                    "prompt_template": prompt_template,
                    "prompt_template_hash": prompt_template_hash,
                    "model": model,
                    "auto_approve": 1 if auto_approve else 0,
                    "auto_approve_tools": auto_approve_tools,
                    "temperature": temperature,
                    "reasoning_effort": reasoning_effort,
                    "max_tokens": max_tokens,
                    "token_budget": token_budget,
                    "agent_max_turns": agent_max_turns,
                    "notify_on_complete": notify_on_complete,
                    "org_id": org_id,
                    "created_by": created_by,
                    "enabled": 1 if enabled else 0,
                    "version": 1,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_ws_template(self, ws_template_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstream_templates).where(
                    workstream_templates.c.ws_template_id == ws_template_id
                )
            ).fetchone()
            if row:
                return _row_to_dict(row, "auto_approve", "enabled")
            return None

    def get_ws_template_by_name(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstream_templates).where(workstream_templates.c.name == name)
            ).fetchone()
            if row:
                return _row_to_dict(row, "auto_approve", "enabled")
            return None

    def list_ws_templates(
        self, org_id: str = "", enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            q = sa.select(workstream_templates).order_by(workstream_templates.c.name)
            if org_id:
                q = q.where(workstream_templates.c.org_id == org_id)
            if enabled_only:
                q = q.where(workstream_templates.c.enabled == 1)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "auto_approve", "enabled") for r in rows]

    def update_ws_template(self, ws_template_id: str, changed_by: str = "", **fields: Any) -> bool:
        with self._engine.connect() as conn:
            # Snapshot current state before updating
            current = conn.execute(
                sa.select(workstream_templates).where(
                    workstream_templates.c.ws_template_id == ws_template_id
                )
            ).fetchone()
            if not current:
                return False
            cur = _row_to_dict(current, "auto_approve", "enabled")

            # Filter to allowed fields — skip snapshot if no effective changes
            dropped = set(fields) - _WS_TEMPLATE_MUTABLE
            if dropped:
                log.warning("update_ws_template: ignoring unknown fields: %s", dropped)
            fields = {k: v for k, v in fields.items() if k in _WS_TEMPLATE_MUTABLE}
            if not fields:
                return True  # Nothing to update

            # Create version snapshot
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                sa.insert(workstream_template_versions),
                {
                    "ws_template_id": ws_template_id,
                    "version": cur["version"],
                    "snapshot": json.dumps(cur, default=str),
                    "changed_by": changed_by,
                    "created": now,
                },
            )

            fields["updated"] = now
            fields["version"] = cur["version"] + 1
            if "auto_approve" in fields:
                fields["auto_approve"] = int(fields["auto_approve"])
            if "enabled" in fields:
                fields["enabled"] = int(fields["enabled"])

            result = conn.execute(
                sa.update(workstream_templates)
                .where(workstream_templates.c.ws_template_id == ws_template_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_ws_template(self, ws_template_id: str) -> bool:
        with self._engine.connect() as conn:
            # Cascade-delete versions first
            conn.execute(
                sa.delete(workstream_template_versions).where(
                    workstream_template_versions.c.ws_template_id == ws_template_id
                )
            )
            result = conn.execute(
                sa.delete(workstream_templates).where(
                    workstream_templates.c.ws_template_id == ws_template_id
                )
            )
            conn.commit()
            return result.rowcount > 0

    def create_ws_template_version(
        self,
        ws_template_id: str,
        version: int,
        snapshot: str,
        changed_by: str = "",
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            conn.execute(
                sa.insert(workstream_template_versions),
                {
                    "ws_template_id": ws_template_id,
                    "version": version,
                    "snapshot": snapshot,
                    "changed_by": changed_by,
                    "created": now,
                },
            )
            conn.commit()

    def list_ws_template_versions(self, ws_template_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(workstream_template_versions)
                .where(workstream_template_versions.c.ws_template_id == ws_template_id)
                .order_by(workstream_template_versions.c.version.desc())
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

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
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
                f"SUM(tool_calls_count) FROM usage_events WHERE {where}"
            )
            with self._engine.connect() as conn:
                row = conn.execute(sa.text(sql), params).fetchone()
                if row:
                    return [
                        {
                            "prompt_tokens": row[0] or 0,
                            "completion_tokens": row[1] or 0,
                            "tool_calls_count": row[2] or 0,
                        }
                    ]
                return [{"prompt_tokens": 0, "completion_tokens": 0, "tool_calls_count": 0}]

        sql = (
            f"SELECT {key_expr} AS key, SUM(prompt_tokens), SUM(completion_tokens), "
            f"SUM(tool_calls_count) FROM usage_events WHERE {where} "
            f"GROUP BY {key_expr} ORDER BY key ASC"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).fetchall()
            return [
                {
                    "key": r[0],
                    "prompt_tokens": r[1] or 0,
                    "completion_tokens": r[2] or 0,
                    "tool_calls_count": r[3] or 0,
                }
                for r in rows
            ]

    def prune_usage_events(self, retention_days: int = 90) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            result = conn.execute(sa.delete(usage_events).where(usage_events.c.timestamp < cutoff))
            conn.commit()
            return result.rowcount

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
        with self._engine.connect() as conn:
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
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
                    "created": now,
                },
            )
            conn.commit()

    def get_intent_verdict(self, verdict_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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

    def get_structured_memory(self, memory_id: str) -> dict[str, str] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(structured_memories).where(structured_memories.c.memory_id == memory_id)
            ).fetchone()
            return dict(row._mapping) if row else None

    def get_structured_memory_by_name(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> dict[str, str] | None:
        with self._engine.connect() as conn:
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

    def update_structured_memory(self, memory_id: str, **fields: str) -> bool:
        fields = {k: v for k, v in fields.items() if k in _SMEM_MUTABLE}
        if not fields:
            return False
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        fields["updated"] = now
        fields["last_accessed"] = now
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(structured_memories)
                .where(structured_memories.c.memory_id == memory_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_structured_memory(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> bool:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            q = sa.select(structured_memories).order_by(structured_memories.c.updated.desc())
            if mem_type:
                q = q.where(structured_memories.c.type == mem_type)
            if scope:
                q = q.where(structured_memories.c.scope == scope)
            if scope_id:
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
        if not query or not query.strip():
            return self.list_structured_memories(
                mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
            )
        terms = query.split()
        with self._engine.connect() as conn:
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
            where = " AND ".join(clauses)
            if mem_type:
                where += " AND type = :type_filter"
                params["type_filter"] = mem_type
            if scope:
                where += " AND scope = :scope_filter"
                params["scope_filter"] = scope
            if scope_id:
                where += " AND scope_id = :scope_id_filter"
                params["scope_id_filter"] = scope_id
            rows = conn.execute(
                sa.text(
                    f"SELECT * FROM structured_memories WHERE {where} "
                    f"ORDER BY updated DESC LIMIT :lim"
                ),
                {**params, "lim": limit},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def count_structured_memories(
        self, mem_type: str = "", scope: str = "", scope_id: str = ""
    ) -> int:
        with self._engine.connect() as conn:
            q = sa.select(sa.func.count()).select_from(structured_memories)
            if mem_type:
                q = q.where(structured_memories.c.type == mem_type)
            if scope:
                q = q.where(structured_memories.c.scope == scope)
            if scope_id:
                q = q.where(structured_memories.c.scope_id == scope_id)
            result = conn.execute(q).scalar()
            return int(result or 0)

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._engine.dispose()
