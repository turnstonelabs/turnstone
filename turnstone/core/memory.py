"""SQLite database for persistent memories and conversation history."""

import os
import sqlite3
from datetime import datetime, timedelta

TURNSTONE_DB = os.path.join(os.getcwd(), ".turnstone.db")
db_override: str | None = None
db_initialized: set[str] = set()
_fts5_available: bool = False

_tavily_key: str | None = None
_tavily_key_loaded: bool = False


def get_tavily_key() -> str | None:
    """Load Tavily API key (cached after first call).

    Precedence: config.toml [api] tavily_key → $TAVILY_API_KEY
    """
    global _tavily_key, _tavily_key_loaded
    if _tavily_key_loaded:
        return _tavily_key
    _tavily_key_loaded = True
    from turnstone.core.config import load_config

    cfg_key = load_config("api").get("tavily_key", "").strip()
    if cfg_key:
        _tavily_key = cfg_key
        return _tavily_key
    env_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if env_key:
        _tavily_key = env_key
    return _tavily_key


def open_db() -> sqlite3.Connection:
    """Open the turnstone database, creating tables on first use per path."""
    global _fts5_available
    path = db_override or TURNSTONE_DB
    conn = sqlite3.connect(path)
    if path not in db_initialized:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "created TEXT NOT NULL, updated TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversations "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, timestamp TEXT NOT NULL, "
            "role TEXT NOT NULL, content TEXT, "
            "tool_name TEXT, tool_args TEXT)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id)"
        )
        # Migration: add tool_call_id column if missing (for session resume)
        try:
            conn.execute("SELECT tool_call_id FROM conversations LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE conversations ADD COLUMN tool_call_id TEXT")
            conn.commit()
        # Sessions table — maps session_id to human-friendly alias/title
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions "
            "(session_id TEXT PRIMARY KEY, alias TEXT UNIQUE, "
            "title TEXT, created TEXT NOT NULL, updated TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_alias ON sessions(alias)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated)"
        )
        try:
            # Check if FTS table already exists
            fts_exists = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='conversations_fts'"
            ).fetchone()
            if not fts_exists:
                conn.execute(
                    "CREATE VIRTUAL TABLE conversations_fts "
                    "USING fts5(content, content=conversations, content_rowid=id)"
                )
                conn.execute(
                    "INSERT INTO conversations_fts(conversations_fts) VALUES('rebuild')"
                )
                conn.commit()
            _fts5_available = True
        except Exception:
            _fts5_available = False
        db_initialized.add(path)
    return conn


def normalize_key(key: str) -> str:
    """Normalize a memory key for consistent lookup."""
    return key.lower().replace("-", "_").replace(" ", "_")


def load_memories() -> list[tuple[str, str]]:
    """Return all (key, value) pairs sorted by key."""
    try:
        conn = open_db()
        try:
            return conn.execute(
                "SELECT key, value FROM memories ORDER BY key"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def save_message(
    session_id: str,
    role: str,
    content: str | None,
    tool_name: str | None = None,
    tool_args: str | None = None,
    tool_call_id: str | None = None,
) -> None:
    """Log a message to the conversations table."""
    global _fts5_available
    try:
        conn = open_db()
        try:
            conn.execute(
                "INSERT INTO conversations (session_id, timestamp, role, content, "
                "tool_name, tool_args, tool_call_id) "
                "VALUES (?, datetime('now'), ?, ?, ?, ?, ?)",
                (session_id, role, content, tool_name, tool_args, tool_call_id),
            )
            if _fts5_available and content:
                try:
                    rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        "INSERT INTO conversations_fts(rowid, content) VALUES (?, ?)",
                        (rowid, content),
                    )
                except Exception:
                    _fts5_available = False  # degrade to LIKE for rest of session
            # Bump session updated timestamp
            conn.execute(
                "UPDATE sessions SET updated = datetime('now') WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Don't let logging failures break the session


def escape_like(s: str) -> str:
    """Escape LIKE metacharacters for use with ESCAPE '\\\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fts5_query(query: str) -> str:
    """Convert a plain search string into a safe FTS5 query.

    Quotes each term so FTS5 special characters (*, -, etc.) are treated
    as literals, then joins with implicit AND.  Embedded double quotes
    are doubled per FTS5 quoting convention.
    """
    terms = query.split()
    safe = []
    for t in terms:
        if t:
            safe.append(f'"{t.replace(chr(34), chr(34) + chr(34))}"')
    return " ".join(safe)


def search_history(query: str, limit: int = 20) -> list[tuple]:
    """Search conversation history. Returns (timestamp, session_id, role, content, tool_name)."""
    if not query or not query.strip():
        return []
    try:
        conn = open_db()
        try:
            if _fts5_available:
                return conn.execute(
                    "SELECT c.timestamp, c.session_id, c.role, c.content, c.tool_name "
                    "FROM conversations_fts f "
                    "JOIN conversations c ON c.id = f.rowid "
                    "WHERE conversations_fts MATCH ? "
                    "ORDER BY f.rank ASC LIMIT ?",
                    (fts5_query(query), min(limit, 100)),
                ).fetchall()
            return conn.execute(
                "SELECT timestamp, session_id, role, content, tool_name "
                "FROM conversations WHERE content LIKE ? ESCAPE '\\' "
                "ORDER BY timestamp DESC LIMIT ?",
                (f"%{escape_like(query)}%", min(limit, 100)),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def search_history_recent(limit: int = 20) -> list[tuple]:
    """Return most recent conversation messages."""
    try:
        conn = open_db()
        try:
            return conn.execute(
                "SELECT timestamp, session_id, role, content, tool_name "
                "FROM conversations ORDER BY timestamp DESC LIMIT ?",
                (min(limit, 100),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


# ── Session management ────────────────────────────────────────────────


def register_session(session_id: str, title: str | None = None) -> None:
    """Create a sessions row for a new session (no-op if already exists)."""
    try:
        conn = open_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(session_id, title, created, updated) "
                "VALUES (?, ?, datetime('now'), datetime('now'))",
                (session_id, title),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def update_session_title(session_id: str, title: str) -> None:
    """Set or update the auto-generated title for a session."""
    try:
        conn = open_db()
        try:
            conn.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ?",
                (title, session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def set_session_alias(session_id: str, alias: str) -> bool:
    """Set a human-friendly alias for a session. Returns False if alias is taken."""
    try:
        conn = open_db()
        try:
            existing = conn.execute(
                "SELECT session_id FROM sessions WHERE alias = ?", (alias,)
            ).fetchone()
            if existing and existing[0] != session_id:
                return False
            conn.execute(
                "UPDATE sessions SET alias = ? WHERE session_id = ?",
                (alias, session_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def get_session_name(session_id: str) -> str | None:
    """Return the alias (or title if no alias) for a session, or None if unset."""
    try:
        conn = open_db()
        try:
            row = conn.execute(
                "SELECT alias, title FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return row[0] or row[1] or None
        finally:
            conn.close()
    except Exception:
        pass
    return None


def resolve_session(alias_or_id: str) -> str | None:
    """Resolve an alias or session_id (or prefix) to a full session_id."""
    try:
        conn = open_db()
        try:
            # 1. Exact alias match
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE alias = ?",
                (alias_or_id,),
            ).fetchone()
            if row:
                return row[0]
            # 2. Exact session_id match
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?",
                (alias_or_id,),
            ).fetchone()
            if row:
                return row[0]
            # 3. Session_id prefix match
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id LIKE ?",
                (alias_or_id + "%",),
            ).fetchall()
            if len(rows) == 1:
                return rows[0][0]
            # 4. Fallback: check conversations table for legacy sessions
            row = conn.execute(
                "SELECT DISTINCT session_id FROM conversations "
                "WHERE session_id = ? LIMIT 1",
                (alias_or_id,),
            ).fetchone()
            if row:
                # Auto-register legacy session
                conn.execute(
                    "INSERT OR IGNORE INTO sessions "
                    "(session_id, created, updated) VALUES ("
                    "?, "
                    "(SELECT MIN(timestamp) FROM conversations WHERE session_id = ?), "
                    "(SELECT MAX(timestamp) FROM conversations WHERE session_id = ?))",
                    (row[0], row[0], row[0]),
                )
                conn.commit()
                return row[0]
            return None
        finally:
            conn.close()
    except Exception:
        return None


def prune_sessions(
    retention_days: int = 90,
    log_fn=None,
) -> tuple[int, int]:
    """Prune orphaned and stale sessions.

    Removes:
      - Sessions with no messages (orphaned registrations from process startup).
      - Sessions whose ``updated`` timestamp is older than ``retention_days``
        **and** that have no alias (named sessions are kept indefinitely).

    Pass ``retention_days=0`` to skip age-based pruning (only orphans removed).

    Returns:
        (orphans_removed, stale_removed)
    """
    orphans = stale = 0
    try:
        conn = open_db()
        try:
            # 1. Remove sessions that have no messages at all.
            cur = conn.execute(
                "DELETE FROM sessions "
                "WHERE NOT EXISTS "
                "  (SELECT 1 FROM conversations c WHERE c.session_id = sessions.session_id)"
            )
            orphans = cur.rowcount

            # 2. Remove old unnamed sessions.
            if retention_days > 0:
                cutoff = (datetime.utcnow() - timedelta(days=retention_days)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                cur = conn.execute(
                    "DELETE FROM sessions WHERE alias IS NULL AND updated < ?",
                    (cutoff,),
                )
                stale = cur.rowcount

            conn.commit()
        finally:
            conn.close()
    except Exception:
        return (0, 0)

    if log_fn and (orphans or stale):
        parts = []
        if orphans:
            parts.append(f"{orphans} empty session{'s' if orphans != 1 else ''}")
        if stale:
            parts.append(
                f"{stale} session{'s' if stale != 1 else ''} "
                f"older than {retention_days} days"
            )
        log_fn(f"[turnstone] Session cleanup: removed {', '.join(parts)}.")

    return (orphans, stale)


def list_sessions(limit: int = 20) -> list[tuple]:
    """List recent sessions.

    Returns (session_id, alias, title, created, updated, msg_count)
    ordered by updated DESC.
    """
    try:
        conn = open_db()
        try:
            return conn.execute(
                "SELECT s.session_id, s.alias, s.title, s.created, s.updated, "
                "(SELECT COUNT(*) FROM conversations c "
                " WHERE c.session_id = s.session_id) "
                "FROM sessions s "
                "WHERE EXISTS "
                "  (SELECT 1 FROM conversations c WHERE c.session_id = s.session_id) "
                "ORDER BY s.updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def load_session_messages(session_id: str) -> list[dict]:
    """Load messages for a session and reconstruct OpenAI message format.

    Handles tool_call / tool_result rows by grouping consecutive tool_call
    rows into one assistant message with tool_calls, then pairing subsequent
    tool_result rows as tool messages.
    """
    try:
        conn = open_db()
        try:
            rows = conn.execute(
                "SELECT role, content, tool_name, tool_args, tool_call_id "
                "FROM conversations WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    messages: list[dict] = []
    i = 0
    while i < len(rows):
        role, content, tool_name, tool_args, tc_id = rows[i]

        if role == "user":
            messages.append({"role": "user", "content": content or ""})
            i += 1

        elif role == "assistant":
            messages.append({"role": "assistant", "content": content})
            i += 1

        elif role == "tool_call":
            # Collect consecutive tool_call rows into one assistant message.
            # If the previous message was an assistant with content (text +
            # tool calls in the same turn), merge tool_calls into it.
            assistant_msg: dict = {
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
                _, _, tn, ta, stored_tc_id = rows[i]
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
                _, result_content, _, _, result_tc_id = rows[i]
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

    return messages


def delete_session(session_id: str) -> bool:
    """Delete a session and all its messages. Returns True on success."""
    try:
        conn = open_db()
        try:
            conn.execute(
                "DELETE FROM conversations WHERE session_id = ?",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False
