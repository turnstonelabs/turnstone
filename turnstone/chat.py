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
from turnstone.cli import detect_model, main  # noqa: F401
from turnstone.core.memory import (  # noqa: F401
    open_db as _open_db,
)
from turnstone.core.session import ChatSession, SessionUI  # noqa: F401
from turnstone.core.tools import AGENT_TOOLS, TASK_AGENT_TOOLS, TOOLS  # noqa: F401
from turnstone.core.web import strip_html as _strip_html  # noqa: F401
from turnstone.ui.colors import (  # noqa: F401
    BLUE,
    BOLD,
    CYAN,
    DIM,
    GRAY,
    GREEN,
    ITALIC,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    bold,
    cyan,
    dim,
    green,
    red,
    yellow,
)
from turnstone.ui.markdown import MarkdownRenderer  # noqa: F401
from turnstone.ui.spinner import Spinner  # noqa: F401
