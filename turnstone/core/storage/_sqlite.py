"""SQLite storage backend."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from turnstone.core.log import get_logger
from turnstone.core.storage._schema import (
    api_tokens,
    audit_events,
    bucket_stats,
    channel_routes,
    channel_users,
    conversations,
    hash_ring_buckets,
    intent_verdicts,
    mcp_servers,
    metadata,
    model_definitions,
    oidc_identities,
    oidc_pending_states,
    orgs,
    output_assessments,
    prompt_templates,
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
    workstream_config,
    workstream_overrides,
    workstreams,
)
from turnstone.core.storage._schema import (
    prompt_policies as prompt_policies_t,
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
    POLICY_MUTABLE as _POLICY_MUTABLE,
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
    STRUCTURED_MEMORY_MUTABLE as _SMEM_MUTABLE,
)
from turnstone.core.storage._utils import (
    VERDICT_MUTABLE as _VERDICT_MUTABLE,
)
from turnstone.core.storage._utils import (
    reconstruct_messages as _reconstruct_messages,
)
from turnstone.core.storage._utils import (
    row_to_dict as _row_to_dict,
)
from turnstone.core.storage._utils import sanitize_text
from turnstone.core.storage._utils import (
    scan_skill_content as _scan_skill_content,
)

log = get_logger(__name__)


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
        tool_call_id: str | None = None,
        provider_data: str | None = None,
        tool_calls: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        content = sanitize_text(content)
        provider_data = sanitize_text(provider_data)
        with self._engine.connect() as conn:
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
                    conversations.c.tool_call_id,
                    conversations.c.provider_data,
                    conversations.c.tool_calls,
                )
                .where(conversations.c.ws_id == ws_id)
                .order_by(conversations.c.id)
            ).fetchall()

        return _reconstruct_messages(list(rows), ws_id)

    def delete_messages_after(self, ws_id: str, keep_count: int) -> int:
        with self._engine.connect() as conn:
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
        if not config:
            return
        with self._engine.connect() as conn:
            conn.execute(
                sa.text(
                    "INSERT OR REPLACE INTO workstream_config "
                    "(ws_id, key, value) VALUES (:wid, :key, :value)"
                ),
                [{"wid": ws_id, "key": key, "value": value} for key, value in config.items()],
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
        skill_id: str = "",
        skill_version: int = 0,
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
                    "skill_id": skill_id,
                    "skill_version": skill_version,
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
            conn.execute(
                sa.delete(workstream_overrides).where(workstream_overrides.c.ws_id == ws_id)
            )
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

    def search_history(self, query: str, limit: int = 20, offset: int = 0) -> list[Any]:
        if not query or not query.strip():
            return []
        capped = min(int(limit), 100)
        capped_offset = max(0, int(offset))
        with self._engine.connect() as conn:
            if self._fts5_available:
                return list(
                    conn.execute(
                        sa.text(
                            "SELECT c.timestamp, c.ws_id, c.role, c.content, c.tool_name "
                            "FROM conversations_fts f "
                            "JOIN conversations c ON c.id = f.rowid "
                            "WHERE conversations_fts MATCH :query "
                            "ORDER BY f.rank ASC LIMIT :limit OFFSET :offset"
                        ),
                        {"query": _fts5_query(query), "limit": capped, "offset": capped_offset},
                    ).fetchall()
                )
            return list(
                conn.execute(
                    sa.text(
                        "SELECT timestamp, ws_id, role, content, tool_name "
                        "FROM conversations WHERE content LIKE :pattern ESCAPE '\\' "
                        "ORDER BY timestamp DESC LIMIT :limit OFFSET :offset"
                    ),
                    {
                        "pattern": f"%{_escape_like(query)}%",
                        "limit": capped,
                        "offset": capped_offset,
                    },
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

        with self._engine.connect() as conn:
            conn.execute(sa.delete(user_roles).where(user_roles.c.user_id == user_id))
            conn.execute(sa.delete(channel_users).where(channel_users.c.user_id == user_id))
            conn.execute(sa.delete(api_tokens).where(api_tokens.c.user_id == user_id))
            conn.execute(sa.delete(oidc_identities).where(oidc_identities.c.user_id == user_id))
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
        skill: str = "",
    ) -> None:

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
                    "skill": skill,
                    "enabled": 1,
                    "created_by": created_by,
                    "next_run": next_run,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(scheduled_tasks).where(scheduled_tasks.c.task_id == task_id)
            ).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:

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
            "skill",
            "enabled",
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
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(scheduled_tasks)
                .where(scheduled_tasks.c.task_id == task_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_scheduled_task(self, task_id: str) -> bool:

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

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(scheduled_task_runs)
                .where(scheduled_task_runs.c.task_id == task_id)
                .order_by(scheduled_task_runs.c.started.desc())
                .limit(limit)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def prune_task_runs(self, retention_days: int = 90) -> int:
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

        with self._engine.connect() as conn:
            row = conn.execute(sa.select(watches).where(watches.c.watch_id == watch_id)).fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_watches_for_ws(self, ws_id: str) -> list[dict[str, Any]]:

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(watches)
                .where((watches.c.ws_id == ws_id) & (watches.c.active == 1))
                .order_by(watches.c.created.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def list_watches_for_node(self, node_id: str) -> list[dict[str, Any]]:

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(watches)
                .where((watches.c.node_id == node_id) & (watches.c.active == 1))
                .order_by(watches.c.created.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def list_due_watches(self, now: str) -> list[dict[str, Any]]:

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

        with self._engine.connect() as conn:
            result = conn.execute(sa.delete(watches).where(watches.c.watch_id == watch_id))
            conn.commit()
            return result.rowcount > 0

    def delete_watches_for_ws(self, ws_id: str) -> int:

        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    def heartbeat_service(self, service_type: str, service_id: str) -> bool:

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

        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(services).where(
                    (services.c.service_type == service_type)
                    & (services.c.service_id == service_id)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- Hash ring routing -----------------------------------------------------

    def list_ring_buckets(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(hash_ring_buckets).order_by(hash_ring_buckets.c.bucket)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def seed_ring_buckets(self, assignments: list[tuple[int, str]]) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        chunk_size = 500
        with self._engine.connect() as conn:
            for i in range(0, len(assignments), chunk_size):
                chunk = assignments[i : i + chunk_size]
                stmt = sqlite_insert(hash_ring_buckets).values(
                    [{"bucket": b, "node_id": n} for b, n in chunk]
                )
                stmt = stmt.on_conflict_do_nothing(index_elements=["bucket"])
                conn.execute(stmt)
            conn.commit()

    def assign_buckets(self, buckets: list[int], node_id: str) -> int:
        if not buckets:
            return 0
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(hash_ring_buckets)
                .where(hash_ring_buckets.c.bucket.in_(buckets))
                .values(node_id=node_id)
            )
            conn.commit()
            return result.rowcount

    def increment_bucket_count(self, bucket: int, active: bool = False) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        with self._engine.connect() as conn:
            stmt = sqlite_insert(bucket_stats).values(
                bucket=bucket,
                ws_count=1,
                active_count=1 if active else 0,
            )
            set_ = {"ws_count": bucket_stats.c.ws_count + 1}
            if active:
                set_["active_count"] = bucket_stats.c.active_count + 1
            stmt = stmt.on_conflict_do_update(index_elements=["bucket"], set_=set_)
            conn.execute(stmt)
            conn.commit()

    def decrement_bucket_count(self, bucket: int, active: bool = False) -> None:
        vals: dict[str, Any] = {
            "ws_count": sa.case(
                (bucket_stats.c.ws_count > 0, bucket_stats.c.ws_count - 1),
                else_=0,
            )
        }
        if active:
            vals["active_count"] = sa.case(
                (bucket_stats.c.active_count > 0, bucket_stats.c.active_count - 1),
                else_=0,
            )
        with self._engine.connect() as conn:
            conn.execute(
                sa.update(bucket_stats).where(bucket_stats.c.bucket == bucket).values(**vals)
            )
            conn.commit()

    def adjust_bucket_active(self, bucket: int, delta: int) -> None:
        with self._engine.connect() as conn:
            conn.execute(
                sa.update(bucket_stats)
                .where(bucket_stats.c.bucket == bucket)
                .values(
                    active_count=sa.case(
                        (
                            bucket_stats.c.active_count + sa.literal(delta) >= 0,
                            bucket_stats.c.active_count + sa.literal(delta),
                        ),
                        else_=0,
                    )
                )
            )
            conn.commit()

    def list_bucket_stats(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(bucket_stats)
                .where(bucket_stats.c.ws_count > 0)
                .order_by(bucket_stats.c.bucket)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def set_bucket_stat(self, bucket: int, ws_count: int, active_count: int) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(bucket_stats).values(
            bucket=bucket, ws_count=ws_count, active_count=active_count
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["bucket"],
            set_={"ws_count": ws_count, "active_count": active_count},
        )
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

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
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    def delete_workstream_override(self, ws_id: str) -> bool:
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(workstream_overrides).where(workstream_overrides.c.ws_id == ws_id)
            )
            conn.commit()
            return result.rowcount > 0

    def list_workstream_overrides(self) -> list[dict[str, str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(workstream_overrides).order_by(workstream_overrides.c.ws_id)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def list_workstream_routing_data(self) -> list[tuple[str, str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT w.ws_id, w.state FROM workstreams w "
                    "WHERE w.ws_id NOT IN (SELECT ws_id FROM workstream_overrides)"
                )
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

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
        notify_on_complete: str = "{}",
        enabled: bool = True,
        allowed_tools: str = "[]",
        skill_license: str = "",
        compatibility: str = "",
        priority: int = 0,
    ) -> None:
        # Sync is_default from activation when activation is explicitly set
        if activation == "default":
            is_default = True
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

        # Scan skill content for risk signals
        scan_status, scan_report, scan_version = _scan_skill_content(content, allowed_tools)

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
                    "scan_status": scan_status,
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
                return _row_to_dict(row, "is_default", "readonly", "auto_approve", "enabled")
            return None

    def get_prompt_template_by_name(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(prompt_templates).where(prompt_templates.c.name == name)
            ).fetchone()
            if row:
                return _row_to_dict(row, "is_default", "readonly", "auto_approve", "enabled")
            return None

    def list_prompt_templates(
        self, org_id: str = "", limit: int = 0, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            q = sa.select(prompt_templates).order_by(prompt_templates.c.name)
            if org_id:
                q = q.where(prompt_templates.c.org_id == org_id)
            if offset > 0:
                q = q.offset(offset)
            if limit > 0:
                q = q.limit(limit)
            rows = conn.execute(q).fetchall()
            return [
                _row_to_dict(r, "is_default", "readonly", "auto_approve", "enabled") for r in rows
            ]

    def count_prompt_templates(self, org_id: str = "") -> int:
        with self._engine.connect() as conn:
            q = sa.select(sa.func.count()).select_from(prompt_templates)
            if org_id:
                q = q.where(prompt_templates.c.org_id == org_id)
            return conn.execute(q).scalar() or 0

    def list_default_templates(self, org_id: str = "") -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
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
                _row_to_dict(r, "is_default", "readonly", "auto_approve", "enabled") for r in rows
            ]

    def list_prompt_templates_by_origin(self, origin: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(prompt_templates)
                .where(prompt_templates.c.origin == origin)
                .order_by(prompt_templates.c.name)
            ).fetchall()
            return [
                _row_to_dict(r, "is_default", "readonly", "auto_approve", "enabled") for r in rows
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
                scan_status, scan_report, scan_version = _scan_skill_content(
                    content, allowed_tools or "[]"
                )
                fields["scan_status"] = scan_status
                fields["scan_report"] = scan_report
                fields["scan_version"] = scan_version
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

    def list_skills_by_activation(
        self,
        activation: str,
        *,
        enabled_only: bool = False,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
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
                _row_to_dict(r, "is_default", "readonly", "auto_approve", "enabled") for r in rows
            ]

    def get_skill_by_name(self, name: str) -> dict[str, Any] | None:
        return self.get_prompt_template_by_name(name)

    def get_skill_by_source_url(self, source_url: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(prompt_templates).where(prompt_templates.c.source_url == source_url)
            ).fetchone()
            if row:
                return _row_to_dict(row, "is_default", "readonly", "auto_approve", "enabled")
            return None

    def list_installed_skill_urls(self) -> list[dict[str, str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    prompt_templates.c.source_url,
                    prompt_templates.c.template_id,
                    prompt_templates.c.scan_status,
                ).where(prompt_templates.c.source_url != "")
            ).fetchall()
            return [
                {
                    "source_url": r[0],
                    "template_id": r[1],
                    "scan_status": r[2] or "",
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(skill_resources)
                .where(skill_resources.c.skill_id == skill_id)
                .order_by(skill_resources.c.path)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def get_skill_resource(self, skill_id: str, path: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(skill_resources)
                .where(skill_resources.c.skill_id == skill_id)
                .where(skill_resources.c.path == path)
            ).fetchone()
            if row:
                return dict(row._mapping)
            return None

    def delete_skill_resources(self, skill_id: str) -> int:
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(skill_resources).where(skill_resources.c.skill_id == skill_id)
            )
            conn.commit()
            return result.rowcount

    def delete_skill_resource_by_path(self, skill_id: str, path: str) -> bool:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(skill_versions)
                .where(skill_versions.c.skill_id == skill_id)
                .order_by(skill_versions.c.version.desc())
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def delete_skill_versions(self, skill_id: str) -> int:
        with self._engine.connect() as conn:
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
            with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
    ) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            q = sa.select(output_assessments).order_by(
                output_assessments.c.created.desc(),
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
        with self._engine.connect() as conn:
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
            if scope_id and scope:
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

    def touch_structured_memories(self, keys: list[tuple[str, str, str]]) -> int:
        """Batch-touch multiple memories by (name, scope, scope_id)."""
        if not keys:
            return 0
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        total = 0
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    def delete_system_setting(self, key: str, node_id: str = "") -> bool:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
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
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_mcp_server(self, server_id: str) -> dict[str, Any] | None:

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(mcp_servers).where(mcp_servers.c.server_id == server_id)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "auto_approve", "enabled")

    def get_mcp_server_by_name(self, name: str) -> dict[str, Any] | None:

        with self._engine.connect() as conn:
            row = conn.execute(sa.select(mcp_servers).where(mcp_servers.c.name == name)).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "auto_approve", "enabled")

    def get_mcp_server_by_registry_name(self, registry_name: str) -> dict[str, Any] | None:

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(mcp_servers).where(mcp_servers.c.registry_name == registry_name)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "auto_approve", "enabled")

    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:

        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(mcp_servers).where(mcp_servers.c.server_id == server_id).values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_mcp_server(self, server_id: str) -> bool:

        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(mcp_servers).where(mcp_servers.c.server_id == server_id)
            )
            conn.commit()
            return result.rowcount > 0

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
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
                    "created_by": created_by,
                    "created": now,
                    "updated": now,
                },
            )
            conn.commit()

    def get_model_definition(self, definition_id: str) -> dict[str, Any] | None:

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(model_definitions).where(
                    model_definitions.c.definition_id == definition_id
                )
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled")

    def get_model_definition_by_alias(self, alias: str) -> dict[str, Any] | None:

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(model_definitions).where(model_definitions.c.alias == alias)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled")

    def list_model_definitions(self, enabled_only: bool = False) -> list[dict[str, Any]]:

        with self._engine.connect() as conn:
            q = sa.select(model_definitions).order_by(model_definitions.c.alias)
            if enabled_only:
                q = q.where(model_definitions.c.enabled == 1)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "enabled") for r in rows]

    def update_model_definition(self, definition_id: str, **fields: Any) -> bool:

        fields = {k: v for k, v in fields.items() if k in _MODEL_DEF_MUTABLE}
        fields["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(model_definitions)
                .where(model_definitions.c.definition_id == definition_id)
                .values(**fields)
            )
            conn.commit()
            return result.rowcount > 0

    def delete_model_definition(self, definition_id: str) -> bool:

        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(model_definitions).where(
                    model_definitions.c.definition_id == definition_id
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- OIDC identity ---------------------------------------------------------

    def create_oidc_identity(self, issuer: str, subject: str, user_id: str, email: str) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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

    def get_oidc_identity(self, issuer: str, subject: str) -> dict[str, str] | None:

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    oidc_identities.c.issuer,
                    oidc_identities.c.subject,
                    oidc_identities.c.user_id,
                    oidc_identities.c.email,
                    oidc_identities.c.created,
                    oidc_identities.c.last_login,
                ).where(
                    (oidc_identities.c.issuer == issuer) & (oidc_identities.c.subject == subject)
                )
            ).fetchone()
            if row:
                return {
                    "issuer": row[0],
                    "subject": row[1],
                    "user_id": row[2],
                    "email": row[3],
                    "created": row[4],
                    "last_login": row[5],
                }
            return None

    def update_oidc_identity_login(self, issuer: str, subject: str) -> bool:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.update(oidc_identities)
                .where(
                    (oidc_identities.c.issuer == issuer) & (oidc_identities.c.subject == subject)
                )
                .values(last_login=now)
            )
            conn.commit()
            return result.rowcount > 0

    def list_oidc_identities_for_user(self, user_id: str) -> list[dict[str, str]]:

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    oidc_identities.c.issuer,
                    oidc_identities.c.subject,
                    oidc_identities.c.user_id,
                    oidc_identities.c.email,
                    oidc_identities.c.created,
                    oidc_identities.c.last_login,
                )
                .where(oidc_identities.c.user_id == user_id)
                .order_by(oidc_identities.c.created.desc())
            ).fetchall()
            return [
                {
                    "issuer": r[0],
                    "subject": r[1],
                    "user_id": r[2],
                    "email": r[3],
                    "created": r[4],
                    "last_login": r[5],
                }
                for r in rows
            ]

    def delete_oidc_identity(self, issuer: str, subject: str) -> bool:

        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(oidc_identities).where(
                    (oidc_identities.c.issuer == issuer) & (oidc_identities.c.subject == subject)
                )
            )
            conn.commit()
            return result.rowcount > 0

    # -- OIDC pending state ----------------------------------------------------

    def create_oidc_pending_state(
        self, state: str, nonce: str, code_verifier: str, audience: str
    ) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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
    ) -> dict[str, str] | None:

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._engine.connect() as conn:
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
            return {
                "state": row[0],
                "nonce": row[1],
                "code_verifier": row[2],
                "audience": row[3],
                "created_at": row[4],
            }

    def cleanup_expired_oidc_states(self, max_age_seconds: int = 300) -> int:

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(oidc_pending_states).where(oidc_pending_states.c.created_at < cutoff)
            )
            conn.commit()
            return result.rowcount

    # -- Prompt policies -------------------------------------------------------

    def list_prompt_policies(self, org_id: str = "") -> list[dict[str, Any]]:

        with self._engine.connect() as conn:
            q = sa.select(prompt_policies_t).order_by(prompt_policies_t.c.priority)
            if org_id:
                q = q.where(prompt_policies_t.c.org_id == org_id)
            rows = conn.execute(q).fetchall()
            return [_row_to_dict(r, "enabled") for r in rows]

    def get_prompt_policy(self, policy_id: str) -> dict[str, Any] | None:

        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(prompt_policies_t).where(prompt_policies_t.c.policy_id == policy_id)
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, "enabled")

    def upsert_prompt_policy(self, policy: dict[str, Any]) -> None:

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        with self._engine.connect() as conn:
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

        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(prompt_policies_t).where(prompt_policies_t.c.policy_id == policy_id)
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
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    def load_tls_account_key(self, key_id: str) -> str | None:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    def load_tls_ca(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
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
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    def load_tls_cert(self, domain: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(tls_certificates).where(tls_certificates.c.domain == domain)
            ).first()
            if not row:
                return None
            return _row_to_dict(row)

    def list_tls_certs(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(tls_certificates).order_by(tls_certificates.c.domain)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def delete_tls_cert(self, domain: str) -> bool:
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.delete(tls_certificates).where(tls_certificates.c.domain == domain)
            )
            conn.commit()
            return result.rowcount > 0

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._engine.dispose()
