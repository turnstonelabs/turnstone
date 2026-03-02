"""chat.py — Backward-compatibility shim.

All functionality has been moved to submodules:
  - turnstone.core.session: ChatSession, SessionUI
  - turnstone.core.tools: TOOLS, AGENT_TOOLS, TASK_AGENT_TOOLS
  - turnstone.core.edit: find_occurrences, pick_nearest
  - turnstone.core.sandbox: validate_math_code, execute_math_sandboxed
  - turnstone.core.safety: is_command_blocked, sanitize_command
  - turnstone.core.web: strip_html, check_ssrf
  - turnstone.core.memory: open_db, load_memories, save_message, etc.
  - turnstone.ui.colors: ANSI constants and helpers
  - turnstone.ui.markdown: MarkdownRenderer
  - turnstone.ui.spinner: Spinner
  - turnstone.cli: TerminalUI, main, detect_model
"""

# Re-export public API for backward compatibility
from turnstone.core.session import ChatSession, SessionUI  # noqa: F401
from turnstone.core.tools import TOOLS, AGENT_TOOLS, TASK_AGENT_TOOLS  # noqa: F401
from turnstone.core.edit import (
    find_occurrences as _find_occurrences,
    pick_nearest as _pick_nearest,
)  # noqa: F401
from turnstone.core.sandbox import (
    validate_math_code as _validate_math_code,
    auto_print_wrap as _auto_print_wrap,
    execute_math_sandboxed as _execute_math_sandboxed,
)  # noqa: F401
from turnstone.core.safety import (
    is_command_blocked,
    sanitize_command as _sanitize_command,
    BLOCKED_PATTERNS,
)  # noqa: F401
from turnstone.core.web import strip_html as _strip_html  # noqa: F401
from turnstone.core.memory import (  # noqa: F401
    open_db as _open_db,
    load_memories as _load_memories,
    save_message as _save_message,
    normalize_key as _normalize_key,
    search_history as _search_history,
    search_history_recent as _search_history_recent,
    escape_like as _escape_like,
    fts5_query as _fts5_query,
    get_tavily_key as _get_tavily_key,
    db_override as _db_override,
    db_initialized as _db_initialized,
)
from turnstone.ui.colors import *  # noqa: F401, F403
from turnstone.ui.markdown import MarkdownRenderer  # noqa: F401
from turnstone.ui.spinner import Spinner  # noqa: F401
from turnstone.cli import main, detect_model  # noqa: F401
