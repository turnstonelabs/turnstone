"""Core chat session — UI-agnostic engine for multi-turn LLM interaction.

The ChatSession class drives the conversation loop (send, stream, tool
execution) while delegating all user-facing I/O through the SessionUI
protocol.  Any frontend (terminal, web, test harness) implements SessionUI
to receive events and handle approval prompts.
"""

from __future__ import annotations

import base64
import collections
import concurrent.futures
import contextlib
import copy
import dataclasses
import difflib
import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid
from datetime import UTC, datetime
from html import escape as _html_escape
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from turnstone.core.attachments import (
    IMAGE_SIZE_CAP as _ATTACH_IMAGE_SIZE_CAP,
)
from turnstone.core.attachments import (
    Attachment,
    unreadable_placeholder,
)
from turnstone.core.config import get_tavily_key
from turnstone.core.edit import find_occurrences, pick_nearest
from turnstone.core.log import get_logger
from turnstone.core.memory import (
    count_structured_memories,
    delete_messages_after,
    delete_structured_memory,
    delete_workstream,
    get_attachments,
    get_skill_by_name,
    get_structured_memory_by_name,
    get_workstream_display_name,
    list_default_skills,
    list_skills_by_activation,
    list_structured_memories,
    list_workstreams_with_history,
    load_messages,
    load_workstream_config,
    mark_attachments_consumed,
    normalize_key,
    resolve_workstream,
    save_message,
    save_messages_bulk,
    save_structured_memory,
    save_workstream_config,
    search_history,
    search_history_recent,
    search_structured_memories,
    set_workstream_alias,
    unreserve_attachments,
    update_workstream_title,
)
from turnstone.core.memory_relevance import (
    MemoryConfig,
    build_memory_context,
    extract_recent_context,
    score_memories,
)
from turnstone.core.metacognition import (
    detect_completion,
    detect_correction,
    format_nudge,
    should_nudge,
)
from turnstone.core.providers import create_provider
from turnstone.core.safety import is_command_blocked, sanitize_command
from turnstone.core.sandbox import execute_math_sandboxed
from turnstone.core.storage._registry import get_storage
from turnstone.core.tool_search import ToolSearchManager
from turnstone.core.tools import (
    AGENT_AUTO_TOOLS,
    AGENT_TOOLS,
    BUILTIN_TOOL_NAMES,
    COORDINATOR_TOOLS,
    INTERACTIVE_TOOLS,
    PRIMARY_KEY_MAP,
    TASK_AGENT_TOOLS,
    TASK_AUTO_TOOLS,
    merge_mcp_tools,
)
from turnstone.core.web import check_ssrf, strip_html
from turnstone.core.workstream import WorkstreamKind
from turnstone.prompts import ClientType, SessionContext, compose_system_message
from turnstone.ui.colors import DIM, GRAY, GREEN, RED, RESET, YELLOW, bold, cyan, dim

log = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from turnstone.core.config_store import ConfigStore
    from turnstone.core.healthcheck import BackendHealthTracker, HealthTrackerRegistry
    from turnstone.core.judge import IntentJudge, JudgeConfig
    from turnstone.core.mcp_client import MCPClientManager
    from turnstone.core.model_registry import ModelConfig, ModelRegistry
    from turnstone.core.output_guard import OutputAssessment
    from turnstone.core.providers import (
        CompletionResult,
        LLMProvider,
        ModelCapabilities,
        StreamChunk,
    )
    from turnstone.core.tool_advisory import ToolAdvisory
    from turnstone.core.web_search import WebSearchClient

# ---------------------------------------------------------------------------
# Cancellation support
# ---------------------------------------------------------------------------


class GenerationCancelled(BaseException):
    """Raised when generation is cancelled via ``ChatSession.cancel()``.

    Subclasses ``BaseException`` so that broad ``except Exception`` handlers
    in tool execution code do not accidentally swallow it.
    """


class _CancelRef(list[Any]):
    """List proxy used for ``ChatSession._cancel_ref``.

    Providers call ``cancel_ref.append(stream_handle)`` eagerly — the HTTP
    call and registration happen before the iterator is returned to the
    caller.  By overriding ``append`` we update ``ChatSession._cancel_stream``
    immediately.  If cancellation was already requested before the stream
    was created (e.g. cancel during retry backoff), the stream is closed
    on arrival so the blocked iteration is unblocked.
    """

    __slots__ = ("_session",)

    def __init__(self, session: ChatSession) -> None:
        super().__init__()
        self._session = session

    def append(self, stream: Any) -> None:
        super().append(stream)
        self._session._cancel_stream = stream
        # If cancel was requested before the first chunk arrived (the worker
        # thread is blocked inside the provider generator waiting for the HTTP
        # response), close the stream immediately to unblock it.
        if self._session._cancel_event.is_set():
            with contextlib.suppress(Exception):
                stream.close()


# Image extensions handled as vision content (SVG excluded — it's XML text)
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".ico"}
)

# Alias for back-compat (existing tests import ``_IMAGE_SIZE_CAP``
# from this module).  Single source of truth lives in
# turnstone.core.attachments so the server upload cap and the
# in-session read cap can't drift.
_IMAGE_SIZE_CAP = _ATTACH_IMAGE_SIZE_CAP


def _encode_image_data_uri(raw: bytes, mime: str) -> str:
    """Wrap raw image bytes as a ``data:{mime};base64,...`` URI."""
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


# Upper bound on total skill content injected into system messages
_MAX_SKILL_CONTENT: int = 32768

# Matches resource paths referenced in skill content (scripts/foo.py, etc.)
_RESOURCE_PATH_RE = re.compile(
    r"(?<![/\w-])(?:scripts|references|assets)/[\w./-]+\."
    r"(?:json|yaml|yml|toml|cfg|ini|py|sh|js|ts|md|txt)"
    r"(?=[\s)\]}'\"`,;:\x60]|$)"
)


_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _without_tool(tools: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Return *tools* with the named tool removed."""
    return [t for t in tools if t.get("function", {}).get("name") != name]


def _render_template(content: str, context: dict[str, str]) -> str:
    """Replace ``{{variable}}`` placeholders in a single pass.

    Unresolvable placeholders are kept as-is.  Single-pass avoids
    cross-variable injection (e.g. a model name containing ``{{ws_id}}``).
    """

    def _replace(m: re.Match[str]) -> str:
        return context.get(m.group(1), m.group(0))

    return _TEMPLATE_VAR_RE.sub(_replace, content)


# ---------------------------------------------------------------------------
# SessionUI protocol — the contract every frontend must implement
# ---------------------------------------------------------------------------


class SessionUI(Protocol):
    def on_thinking_start(self) -> None: ...
    def on_thinking_stop(self) -> None: ...
    def on_reasoning_token(self, text: str) -> None: ...
    def on_content_token(self, text: str) -> None: ...
    def on_stream_end(self) -> None: ...
    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]: ...
    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
    ) -> None: ...
    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None: ...
    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None: ...
    def on_plan_review(self, content: str) -> str: ...
    def on_info(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_state_change(self, state: str) -> None: ...
    def on_rename(self, name: str) -> None: ...
    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Called when the LLM judge produces a verdict for a pending approval."""
        ...

    def on_output_warning(self, call_id: str, assessment: dict[str, Any]) -> None:
        """Called when the output guard detects risk signals in tool output."""
        ...


# ---------------------------------------------------------------------------
# Notify auth helper (module-level, lazy-init)
# ---------------------------------------------------------------------------

_notify_token_manager: Any = None
_notify_token_lock = threading.Lock()


def _notify_auth_headers() -> dict[str, str]:
    """Return Authorization headers for outbound notify requests."""
    global _notify_token_manager

    # Static token from env takes precedence
    static_token = os.environ.get("TURNSTONE_CHANNEL_AUTH_TOKEN", "").strip()
    if static_token:
        return {"Authorization": f"Bearer {static_token}"}

    # JWT via ServiceTokenManager
    jwt_secret = os.environ.get("TURNSTONE_JWT_SECRET", "").strip()
    if not jwt_secret:
        return {}

    with _notify_token_lock:
        if _notify_token_manager is None:
            from turnstone.core.auth import JWT_AUD_CHANNEL, ServiceTokenManager

            _notify_token_manager = ServiceTokenManager(
                user_id="system",
                scopes=frozenset({"write"}),
                source="service",
                secret=jwt_secret,
                audience=JWT_AUD_CHANNEL,
            )
    header: dict[str, str] = _notify_token_manager.bearer_header
    return header


# ---------------------------------------------------------------------------
# ChatSession — the core engine
# ---------------------------------------------------------------------------


class ChatSession:
    _QUEUE_MAX = 10

    def __init__(
        self,
        client: Any,
        model: str,
        ui: SessionUI,
        instructions: str | None,
        temperature: float,
        max_tokens: int,
        tool_timeout: int,
        reasoning_effort: str = "medium",
        context_window: int = 32768,
        compact_max_tokens: int = 32768,
        auto_compact_pct: float = 0.8,
        agent_max_turns: int = -1,
        tool_truncation: int = 0,
        mcp_client: MCPClientManager | None = None,
        registry: ModelRegistry | None = None,
        model_alias: str | None = None,
        health_registry: HealthTrackerRegistry | None = None,
        node_id: str | None = None,
        ws_id: str | None = None,
        tool_search: str = "auto",
        tool_search_threshold: int = 20,
        tool_search_max_results: int = 5,
        skill: str | None = None,
        judge_config: JudgeConfig | None = None,
        user_id: str = "",
        memory_config: MemoryConfig | None = None,
        config_store: ConfigStore | None = None,
        web_search_backend: str = "",
        client_type: ClientType = ClientType.CLI,
        username: str = "",
        kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
        parent_ws_id: str | None = None,
        coord_client: Any = None,
    ):
        self.client = client
        self.model = model
        # Coordinator plumbing: populated by the console's session factory
        # only — ``kind == COORDINATOR`` sessions run COORDINATOR_TOOLS
        # and dispatch tool execs through ``coord_client``.
        self._kind = kind
        self._parent_ws_id = parent_ws_id if parent_ws_id else None
        self._coord_client: Any = coord_client
        self._registry = registry
        self._model_alias = model_alias
        self._health_registry = health_registry
        # Resolve provider for the current model
        self._provider: LLMProvider = (
            registry.get_provider(model_alias)
            if registry and model_alias
            else create_provider("openai-compatible")
        )
        self._cached_capabilities: ModelCapabilities | None = None
        self.ui = ui
        self.instructions = instructions
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.tool_timeout = tool_timeout
        self.reasoning_effort = reasoning_effort
        self.context_window = context_window if context_window > 0 else 32768
        self.compact_max_tokens = compact_max_tokens
        self.auto_compact_pct = auto_compact_pct
        self.agent_max_turns = agent_max_turns
        self._chars_per_token = 4.0  # calibrated from API usage
        # Tool output truncation: 0 means auto (50% of context_window in chars)
        self._manual_tool_truncation = tool_truncation > 0
        if tool_truncation > 0:
            self.tool_truncation = tool_truncation
        else:
            self.tool_truncation = int(context_window * self._chars_per_token * 0.5)
        self.show_reasoning = True
        self.debug = False
        self.auto_approve = False
        self._node_id = node_id
        self._user_id = user_id
        self._username = username
        self._client_type = client_type
        self._config_store = config_store
        # Initialize rule registry for configurable judge rules
        self._rule_registry = None
        if config_store is not None:
            try:
                from turnstone.core.rule_registry import RuleRegistry

                self._rule_registry = RuleRegistry(storage=config_store.storage)
            except Exception:
                log.debug("rule_registry.init_failed", exc_info=True)
        self._memory_config = memory_config or MemoryConfig()
        self._ws_id = ws_id or uuid.uuid4().hex
        self._title_generated = False
        self._read_files: set[str] = set()
        self.messages: list[dict[str, Any]] = []
        self._last_usage: dict[str, int] | None = None
        self._msg_tokens: list[int] = []  # parallel to self.messages
        self._system_tokens = 0  # tokens for system_messages
        # Workstream template metadata
        self._token_budget: int = 0
        self._budget_warned: bool = False
        self._budget_exhausted: bool = False
        self._notify_on_complete: str = "{}"
        self._applied_skill_id: str = ""
        self._applied_skill_version: int = 0
        self._applied_skill_content: str = ""  # inline prompt from applied skill
        self._assistant_pending_tokens = 0
        self._calibrated_msg_count = 0  # len(messages) at last _update_token_table
        self.creative_mode = False
        self._notify_count = 0
        # Watch support: server-level runner injected via set_watch_runner()
        self._watch_runner: Any = None  # WatchRunner | None
        self._watch_pending: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=20)
        self._watch_dispatch_depth = 0
        # Metacognitive nudges: ephemeral prompts for proactive memory use
        self._metacog_state: dict[str, float] = {}
        self._pending_nudge: list[tuple[str, str]] = []  # (type, text)
        # User message queue: messages sent while model is executing.
        # OrderedDict preserves FIFO order and supports O(1) removal by ID.
        #
        # Entry shape: ``(cleaned_text, priority, attachment_ids)``.
        # Attachment lifecycle:
        #     pending   — uploaded, not tied to any turn
        #     reserved  — soft-locked at queue time (reserved_for_msg_id = queue id)
        #     consumed  — committed to a saved message (message_id = conv row id)
        # queue_message transitions pending → reserved for its attachments;
        # _flush_queued_messages (dequeue) transitions reserved → consumed.
        self._queued_messages: collections.OrderedDict[str, tuple[str, str, tuple[str, ...]]] = (
            collections.OrderedDict()
        )
        self._queued_lock = threading.Lock()
        # Repeat detection: track recent tool call signatures
        self._recent_tool_sigs: set[str] = set()
        # Tool error tracking: call_id → is_error for message persistence
        self._tool_error_flags: dict[str, bool] = {}
        # Cooperative cancellation: set from outside to stop generation
        self._cancel_event = threading.Event()
        self._cancel_ref: _CancelRef = _CancelRef(self)  # provider appends SDK stream here
        self._cancel_stream: Any = None  # closeable SDK stream handle
        self._generation: int = 0  # monotonic counter; orphaned threads skip cleanup
        self._active_procs: set[subprocess.Popen[str]] = set()  # for force-kill
        self._procs_lock = threading.Lock()
        self._cancelled_partial_msg: dict[str, Any] | None = None
        self._pending_retry: str | None = None
        # Intent validation judge (lazy-initialized)
        self._judge_config: JudgeConfig | None = judge_config
        self._judge: IntentJudge | None = None
        self._judge_cancel_event: threading.Event | None = None
        # MCP tool integration: merge external tools with built-in
        self._mcp_client = mcp_client
        self._mcp_refresh_cb: Any = None  # Callable | None (avoid import)
        self._mcp_resource_cb: Any = None
        self._mcp_prompt_cb: Any = None
        # Tool-set selection is kind-aware:
        #   * coordinator — fixed COORDINATOR_TOOLS, no MCP surface.
        #     Coordinators are meta-orchestrators that spawn child
        #     workstreams; MCP tools / resources / prompts live on the
        #     children.  Giving the coordinator direct MCP access
        #     defeats the child-spawning pattern, so we don't merge
        #     MCP tools and don't register MCP listeners either.
        #   * interactive + mcp — INTERACTIVE_TOOLS ∪ mcp tools; MCP
        #     listeners register so tool/resource/prompt refreshes flow
        #     through to this session.
        #   * interactive (no mcp) — INTERACTIVE_TOOLS.
        if kind == WorkstreamKind.COORDINATOR:
            self._tools = list(COORDINATOR_TOOLS)
            self._task_tools = []
            self._agent_tools = []
        elif mcp_client:
            mcp_tools = mcp_client.get_tools()
            self._tools = merge_mcp_tools(INTERACTIVE_TOOLS, mcp_tools)
            self._task_tools = merge_mcp_tools(TASK_AGENT_TOOLS, mcp_tools)
            self._agent_tools = merge_mcp_tools(AGENT_TOOLS, mcp_tools)
            # Register for tool-change notifications from MCP servers
            self._mcp_refresh_cb = self._on_mcp_tools_changed
            mcp_client.add_listener(self._mcp_refresh_cb)
            # Register for resource-change notifications
            self._mcp_resource_cb = self._on_mcp_resources_changed
            mcp_client.add_resource_listener(self._mcp_resource_cb)
            # Register for prompt-change notifications
            self._mcp_prompt_cb = self._on_mcp_prompts_changed
            mcp_client.add_prompt_listener(self._mcp_prompt_cb)
        else:
            self._tools = INTERACTIVE_TOOLS
            self._task_tools = TASK_AGENT_TOOLS
            self._agent_tools = AGENT_TOOLS
        # Inject the live alias list into plan_agent / task_agent tool
        # descriptions so the calling LLM sees its `model` parameter options.
        # Replaces affected tool dicts with deep copies — module-level
        # constants are not mutated.
        self._render_agent_tool_descriptions()
        # Web search backend (pluggable: auto/tavily/ddg/mcp:server:tool)
        self._web_search_backend = web_search_backend
        # Dynamic tool search: defer MCP tools when tool count is high
        self._tool_search_setting = tool_search
        self._tool_search_threshold = tool_search_threshold
        self._tool_search_max_results = tool_search_max_results
        self._tool_search: ToolSearchManager | None = None
        if tool_search == "on" or (
            tool_search == "auto" and len(self._tools) > tool_search_threshold
        ):
            # always_on_names is the set of builtin tools present in
            # *this* session — kind-aware, so coordinator sessions never
            # keep interactive tool names "always on" and vice versa.
            builtin_in_session = {
                t["function"]["name"]
                for t in self._tools
                if t["function"]["name"] in BUILTIN_TOOL_NAMES
            }
            self._tool_search = ToolSearchManager(
                self._tools,
                always_on_names=builtin_in_session,
                max_results=tool_search_max_results,
            )
        # Skill: explicit name overrides is_default skills
        self._skill_name: str | None = skill
        self._skill_content: str | None = None
        self._skill_resources: dict[str, str] = {}
        self._skill_resources_dir: str | None = None
        self._load_skills()
        self._init_system_messages()
        self._save_config()

    @property
    def ws_id(self) -> str:
        return self._ws_id

    @property
    def model_alias(self) -> str | None:
        return self._model_alias

    @property
    def _mem_cfg(self) -> MemoryConfig:
        """Live memory config — reads from ConfigStore when available."""
        cs = getattr(self, "_config_store", None)
        if cs is None:
            return self._memory_config
        return MemoryConfig(
            relevance_k=cs.get("memory.relevance_k"),
            fetch_limit=cs.get("memory.fetch_limit"),
            max_content=cs.get("memory.max_content"),
            nudge_cooldown=cs.get("memory.nudge_cooldown"),
            nudges=cs.get("memory.nudges"),
        )

    @property
    def _judge_cfg(self) -> JudgeConfig | None:
        """Live judge behavioral config — reads from ConfigStore when available.

        The model alias stays frozen
        from session creation time since changing them would require tearing
        down and rebuilding the IntentJudge instance.
        """
        jc = self._judge_config
        if jc is None:
            return None
        cs = getattr(self, "_config_store", None)
        if cs is None:
            return jc
        from turnstone.core.judge import JudgeConfig

        return JudgeConfig(
            enabled=cs.get("judge.enabled"),
            model=jc.model,
            confidence_threshold=cs.get("judge.confidence_threshold"),
            max_context_ratio=cs.get("judge.max_context_ratio"),
            timeout=cs.get("judge.timeout"),
            read_only_tools=cs.get("judge.read_only_tools"),
            output_guard=cs.get("judge.output_guard"),
            redact_secrets=cs.get("judge.redact_secrets"),
            cancel_on_approval=cs.get("judge.cancel_on_approval"),
        )

    def _get_web_search_backend(self) -> str:
        """Effective web search backend — reads from ConfigStore when available."""
        cs = getattr(self, "_config_store", None)
        if cs is not None:
            val = cs.get("tools.web_search_backend")
            if val:
                return str(val)
        return self._web_search_backend

    def _resolve_search_client(self) -> WebSearchClient | None:
        """Return a web search client for the configured backend, or None."""
        from turnstone.core.web_search import resolve_web_search_client

        # ConfigStore (DB) takes precedence over config.toml / env var
        tavily_key: str | None = None
        cs = getattr(self, "_config_store", None)
        if cs is not None:
            db_key = cs.get("tools.tavily_api_key")
            if db_key:
                tavily_key = str(db_key)
        if not tavily_key:
            tavily_key = get_tavily_key()

        return resolve_web_search_client(
            backend=self._get_web_search_backend(),
            tavily_key=tavily_key,
            mcp_client=self._mcp_client,
            timeout=self.tool_timeout,
        )

    def _resolve_capabilities(
        self,
        provider: LLMProvider,
        model: str,
        alias: str | None = None,
    ) -> ModelCapabilities:
        """Get model capabilities, applying config.toml overrides if present."""
        caps = provider.get_capabilities(model)
        if self._registry and alias:
            cfg: ModelConfig = self._registry.get_config(alias)
            if cfg.capabilities:
                fields = {f.name for f in dataclasses.fields(type(caps))}
                overrides = {k: v for k, v in cfg.capabilities.items() if k in fields}
                if overrides:
                    caps = dataclasses.replace(caps, **overrides)
        return caps

    def _get_capabilities(self, provider: Any = None, model: str = "") -> ModelCapabilities:
        """Get capabilities for a model. Cached for the primary session model."""
        p = provider or self._provider
        m = model or self.model
        # Only use cache for the primary session model — fallback models bypass.
        if p is self._provider and m == self.model:
            if self._cached_capabilities is None:
                self._cached_capabilities = self._resolve_capabilities(p, m, self._model_alias)
            return self._cached_capabilities
        return self._resolve_capabilities(p, m, "")

    def _save_config(self) -> None:
        """Persist LLM-affecting config so resumed workstreams behave identically."""
        save_workstream_config(
            self._ws_id,
            {
                "model": self.model,
                "model_alias": self._model_alias or "",
                "temperature": str(self.temperature),
                "reasoning_effort": self.reasoning_effort,
                "max_tokens": str(self.max_tokens),
                "instructions": self.instructions or "",
                "creative_mode": str(self.creative_mode),
                "skill": self._skill_name or "",
                "token_budget": str(self._token_budget),
                "applied_skill_id": self._applied_skill_id,
                "applied_skill_version": str(self._applied_skill_version),
                # Snapshot isolation: skill content is persisted per-workstream so that
                # edits to the skill between sessions don't break resume. This duplicates
                # up to 32KB per active workstream — acceptable trade-off for correctness.
                "applied_skill_content": self._applied_skill_content,
                "notify_on_complete": self._notify_on_complete,
            },
        )

    def _load_skills(self) -> None:
        """Load skills from storage.  Called once at init and on /skill."""
        context = {
            "model": self.model,
            "ws_id": self._ws_id,
            "node_id": self._node_id or "",
        }
        if self._skill_name:
            skill_data = get_skill_by_name(self._skill_name)
            if skill_data:
                self._skill_content = _render_template(skill_data["content"], context)
                self._check_skill_budget(skill_data)
                self._skill_resources = self._load_skill_resources(
                    skill_data.get("template_id", "")
                )
                if skill_data.get("scan_status") in ("high", "critical"):
                    scan_tier = skill_data["scan_status"]
                    log.warning(
                        "skill.high_risk_loaded",
                        skill=skill_data["name"],
                        scan_status=scan_tier,
                    )
                    self.ui.on_info(
                        f"⚠ Skill '{skill_data['name']}' has scan status: {scan_tier}. "
                        f"Review scan report in admin panel before enabling in production."
                    )
            else:
                log.warning("skill.not_found", name=self._skill_name)
                self._skill_content = None
                self._skill_resources = {}
        else:
            defaults = list_default_skills()
            if defaults:
                parts = [_render_template(t["content"], context) for t in defaults]
                self._skill_content = "\n\n".join(parts)
            else:
                self._skill_content = None
            self._skill_resources = {}
        self._materialize_skill_resources()
        self._validate_skill_resources()

    def set_skill(self, name: str | None) -> None:
        """Set or clear the active skill."""
        self._skill_name = name
        self._load_skills()
        self._init_system_messages()
        self._save_config()

    def _check_skill_budget(self, skill: dict[str, Any]) -> None:
        """Log warning if skill content exceeds 25% of context window."""
        if skill.get("token_estimate", 0) > self.context_window * 0.25:
            log.warning(
                "skill.token_budget_warning",
                skill=skill.get("name", ""),
                estimate=skill["token_estimate"],
                context_window=self.context_window,
            )

    def _load_skill_resources(self, skill_id: str) -> dict[str, str]:
        """Load bundled resources for a skill and return {path: content}."""
        if not skill_id:
            return {}
        try:
            storage = get_storage()
            rows = storage.list_skill_resources(skill_id)
            return {r["path"]: r.get("content", "") for r in rows}
        except Exception:
            log.warning("skill_resources.load_failed", skill_id=skill_id, exc_info=True)
            return {}

    def _cleanup_skill_resources(self) -> None:
        """Remove materialized skill resources from disk."""
        d = self._skill_resources_dir
        if d is not None:
            shutil.rmtree(d, ignore_errors=True)
            self._skill_resources_dir = None

    def _materialize_skill_resources(self) -> None:
        """Write skill resources to a temp directory for subprocess access."""
        self._cleanup_skill_resources()
        if not self._skill_resources:
            return
        base = tempfile.mkdtemp(prefix=f"skill-{self._ws_id[:8]}-")
        written = 0
        for rel_path, content in self._skill_resources.items():
            normed = os.path.normpath(rel_path)
            if not normed or normed == "." or normed.startswith(("..", "/")):
                log.warning("skill_resources.bad_path", path=rel_path)
                continue
            if ".." in normed.split(os.sep):
                log.warning("skill_resources.bad_path", path=rel_path)
                continue
            full = os.path.join(base, normed)
            if not os.path.realpath(full).startswith(os.path.realpath(base)):
                log.warning("skill_resources.path_escape", path=rel_path)
                continue
            try:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(content)
                if normed.startswith("scripts/"):
                    os.chmod(full, 0o755)
                written += 1
            except Exception:
                log.warning("skill_resources.write_failed", path=rel_path, exc_info=True)
        if written == 0:
            shutil.rmtree(base, ignore_errors=True)
            return
        self._skill_resources_dir = base
        log.info(
            "skill_resources.materialized",
            dir=base,
            count=written,
        )

    def _skill_resource_env(self) -> dict[str, str]:
        """Return extra env vars for bash when skill resources are materialized."""
        if not self._skill_resources_dir:
            return {}
        env: dict[str, str] = {"SKILL_RESOURCES_DIR": self._skill_resources_dir}
        scripts_dir = os.path.join(self._skill_resources_dir, "scripts")
        if os.path.isdir(scripts_dir):
            current_path = os.environ.get("PATH")
            if current_path:
                env["PATH"] = scripts_dir + os.pathsep + current_path
            else:
                env["PATH"] = scripts_dir
        return env

    def _validate_skill_resources(self) -> None:
        """Warn if skill content references resource paths not in skill_resources."""
        if not self._skill_content or not self._skill_name:
            return
        referenced = {os.path.normpath(p) for p in _RESOURCE_PATH_RE.findall(self._skill_content)}
        if not referenced:
            return
        available = {os.path.normpath(p) for p in self._skill_resources}
        missing = sorted(referenced - available)
        if missing:
            log.warning("skill_resources.missing", skill=self._skill_name, paths=missing)
            self.ui.on_info(
                f"Skill '{self._skill_name}' references {len(missing)} resource(s) "
                f"not bundled: {', '.join(missing)}"
            )

    # -- MCP tool refresh ----------------------------------------------------

    def _on_mcp_tools_changed(self) -> None:
        """Callback from MCPClientManager when the tool list changes.

        Rebuilds merged tool lists and reconstructs ToolSearchManager.
        Called on the MCP background thread.  The work is O(n) where *n* is
        the MCP tool count — ``merge_mcp_tools`` is list concatenation and
        ``BM25Index`` construction over <50 tools completes in microseconds,
        so this does not meaningfully block the MCP event loop.

        Thread safety: each assignment creates a new object (copy-on-write).
        Under CPython's GIL, individual reference assignments are atomic.
        ``_try_stream`` captures tools at call time, so a concurrent refresh
        between turns is safe; mid-stream the LLM request already holds
        the old snapshot.
        """
        if not self._mcp_client:
            return
        # Coordinator sessions don't consume MCP tools — the tool set
        # is fixed at COORDINATOR_TOOLS.  Ignore MCP server changes.
        if self._kind == WorkstreamKind.COORDINATOR:
            return
        mcp_tools = self._mcp_client.get_tools()
        self._tools = merge_mcp_tools(INTERACTIVE_TOOLS, mcp_tools)
        self._task_tools = merge_mcp_tools(TASK_AGENT_TOOLS, mcp_tools)
        self._agent_tools = merge_mcp_tools(AGENT_TOOLS, mcp_tools)
        self._render_agent_tool_descriptions()
        self._rebuild_tool_search()

    def _render_agent_tool_descriptions(self) -> None:
        """Inject the live alias list into the ``model`` parameter description
        on plan_agent / task_agent tools.

        Lets the calling LLM see which aliases are valid right now.
        Called on session init and on registry reload (via
        ``refresh_agent_tool_schemas``).  No-op when no registry is
        configured (CLI single-model case).

        Replaces affected tool dicts with deep copies so the module-level
        tool-list constants stay untouched across sessions.

        plan_agent and task_agent live in ``self._tools`` (the main session's
        tool set) — not in ``self._agent_tools`` / ``self._task_tools``,
        which are what *sub-agents* see (sub-agents don't get delegation
        tools to avoid infinite recursion).
        """
        if self._registry is None:
            return
        aliases = sorted(self._registry.list_aliases())
        if not aliases:
            return
        aliases_str = ", ".join(f"`{a}`" for a in aliases)

        new_tools: list[dict[str, Any]] = []
        for tool in self._tools:
            fn = tool.get("function") or {}
            name = fn.get("name", "")
            if name not in ("plan_agent", "task_agent"):
                new_tools.append(tool)
                continue
            kind = "plan model" if name == "plan_agent" else "task model"
            new_tool = copy.deepcopy(tool)
            props = new_tool.get("function", {}).get("parameters", {}).get("properties", {})
            if "model" in props:
                props["model"]["description"] = (
                    f"Optional model alias to run this {name} on. "
                    f"Omit to use the operator-configured {kind}. "
                    f"Available aliases: {aliases_str}."
                )
            new_tools.append(new_tool)
        self._tools = new_tools

    def refresh_agent_tool_schemas(self) -> None:
        """Public entry point: re-render plan_agent / task_agent tool
        descriptions to reflect the current ModelRegistry state, and
        rebuild the BM25 tool-search index so its text matches.

        Called by the server after a registry reload (sync-to-nodes /
        admin model edits) so active sessions pick up the new alias
        list on their next LLM turn.

        ``_on_mcp_tools_changed`` calls ``_render_agent_tool_descriptions``
        directly (not this) because it already rebuilds the tool-search
        index right after — calling this wrapper would do that twice.
        """
        self._render_agent_tool_descriptions()
        if getattr(self, "_tool_search", None) is not None:
            self._rebuild_tool_search()

    def _on_mcp_resources_changed(self) -> None:
        """Callback from MCPClientManager when the resource list changes.

        Rebuilds the system message to update the resource catalog.
        Called on the MCP background thread.
        """
        self._init_system_messages()

    def _on_mcp_prompts_changed(self) -> None:
        """Callback from MCPClientManager when the prompt list changes.

        Rebuilds the system message to update the prompt catalog.
        Called on the MCP background thread.
        """
        self._init_system_messages()

    def _refresh_model_from_registry(self) -> None:
        """Re-resolve model from registry if the backend changed.

        Called at the top of ``send()`` — two string compares when nothing
        changed, full re-resolve when the health monitor detected a model swap.
        """
        if not self._registry or not self._model_alias:
            return
        try:
            if not self._registry.has_alias(self._model_alias):
                return
            cfg = self._registry.get_config(self._model_alias)
            if cfg.model == self.model:
                return
            client, model_name, new_cfg = self._registry.resolve(self._model_alias)
        except (ValueError, KeyError):
            return  # alias disappeared during concurrent reload
        self.client = client
        self.model = model_name
        self._provider = self._registry.get_provider(self._model_alias)
        self._cached_capabilities = None
        if new_cfg.context_window and new_cfg.context_window != self.context_window:
            self.context_window = new_cfg.context_window
            # Recompute auto tool truncation for new context window
            if not self._manual_tool_truncation:
                self.tool_truncation = int(new_cfg.context_window * self._chars_per_token * 0.5)
        # Reset judge so it picks up the new model/provider
        if self._judge is not None:
            self._judge = None
        self._init_system_messages()
        log.info(
            "session.model_updated ws=%s model=%s ctx=%d",
            self._ws_id,
            model_name,
            self.context_window,
        )

    def _rebuild_tool_search(self) -> None:
        """Reconstruct ToolSearchManager, preserving expanded tools."""
        old_expanded = self._tool_search.get_expanded_names() if self._tool_search else []
        if self._tool_search_setting == "on" or (
            self._tool_search_setting == "auto" and len(self._tools) > self._tool_search_threshold
        ):
            self._tool_search = ToolSearchManager(
                self._tools,
                always_on_names=set(BUILTIN_TOOL_NAMES),
                max_results=self._tool_search_max_results,
            )
            # Restore previously expanded tools that still exist
            if old_expanded:
                self._tool_search.expand_visible(old_expanded)
        else:
            self._tool_search = None

    def set_watch_runner(self, runner: Any, dispatch_fn: Any = None) -> None:
        """Inject the server-level WatchRunner (called after workstream setup).

        If *dispatch_fn* is provided (the server passes one that can start
        worker threads), it is registered directly.  Otherwise a simple
        enqueue fallback is used — suitable only when ``send()`` is already
        active (Path A).
        """
        self._watch_runner = runner
        if dispatch_fn is not None:
            runner.set_dispatch_fn(self._ws_id, dispatch_fn)
        else:
            pending = self._watch_pending

            def _enqueue(msg: str) -> None:
                try:
                    pending.put_nowait({"message": msg})
                except queue.Full:
                    log.warning(
                        "Watch pending queue full, dropping result for ws_id=%s", self._ws_id
                    )

            runner.set_dispatch_fn(self._ws_id, _enqueue)

    def close(self) -> None:
        """Release resources (listener registrations, etc.)."""
        if self._judge_cancel_event is not None:
            self._judge_cancel_event.set()
        if self._mcp_client and self._mcp_refresh_cb:
            self._mcp_client.remove_listener(self._mcp_refresh_cb)
            self._mcp_refresh_cb = None
        if self._mcp_client and self._mcp_resource_cb:
            self._mcp_client.remove_resource_listener(self._mcp_resource_cb)
            self._mcp_resource_cb = None
        if self._mcp_client and self._mcp_prompt_cb:
            self._mcp_client.remove_prompt_listener(self._mcp_prompt_cb)
            self._mcp_prompt_cb = None
        if self._watch_runner:
            self._watch_runner.remove_dispatch_fn(self._ws_id)
        if self._coord_client is not None and hasattr(self._coord_client, "close"):
            try:
                self._coord_client.close()
            except Exception:
                log.debug("chat_session.coord_client_close_failed", exc_info=True)
        self._cleanup_skill_resources()

    def _handle_mcp_refresh(self, arg: str) -> None:
        """Handle ``/mcp refresh [server]``."""
        assert self._mcp_client is not None
        tokens = arg.split(None, 1)  # ["refresh"] or ["refresh", "server"]
        server_name: str | None = tokens[1] if len(tokens) > 1 else None

        if server_name and server_name not in self._mcp_client.server_names:
            known = ", ".join(self._mcp_client.server_names) or "(none)"
            self.ui.on_error(f"Unknown MCP server: {server_name}. Known servers: {known}")
            return

        try:
            results = self._mcp_client.refresh_sync(server_name)
        except Exception as exc:
            self.ui.on_error(f"MCP refresh failed: {exc}")
            return

        lines: list[str] = []
        for srv, (added, removed) in sorted(results.items()):
            if added or removed:
                summary: list[str] = []
                if added:
                    summary.append(f"+{len(added)} added")
                if removed:
                    summary.append(f"-{len(removed)} removed")
                lines.append(f"  {srv}: {', '.join(summary)}")
                for name in added:
                    lines.append(f"    {GREEN}+ {name}{RESET}")
                for name in removed:
                    lines.append(f"    {RED}- {name}{RESET}")
            else:
                lines.append(f"  {srv}: {dim('no changes')}")

        header = "MCP refresh complete:"
        self.ui.on_info(
            "\n".join([header, *lines]) if lines else "MCP refresh complete: no servers to refresh."
        )

    def _report_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
        """Notify the UI and record error flag for message persistence."""
        if is_error:
            self._tool_error_flags[call_id] = True
        self.ui.on_tool_result(call_id, name, output, is_error=is_error)

    def _remaining_token_budget(self) -> int:
        """Estimate how many tokens are available for new content.

        When provider-reported usage is available, uses the last API
        call's ``prompt_tokens`` as ground truth and only estimates the
        delta (messages added since that call).  Falls back to pure
        local estimates otherwise.

        Reserves a response budget (capped at 25% of context window, since
        ``max_tokens`` is an upper bound, not guaranteed consumption) plus
        a 5% safety margin.  Returns at least 0.
        """
        used = self._system_tokens + sum(self._msg_tokens)
        if self._last_usage:
            # Provider-reported tokens from the last API call
            base = self._last_usage["prompt_tokens"]
            # Only estimate tokens for messages added AFTER calibration.
            # Clamp index to prevent stale _calibrated_msg_count from
            # over-slicing after compaction or message list mutations.
            start = min(self._calibrated_msg_count, len(self._msg_tokens))
            new_msg_tokens = sum(self._msg_tokens[start:])
            used = base + new_msg_tokens
        response_reserve = min(self.max_tokens, self.context_window // 4)
        safety_margin = int(self.context_window * 0.05)
        return max(0, self.context_window - used - response_reserve - safety_margin)

    def _truncate_output(self, output: str, remaining_budget_tokens: int | None = None) -> str:
        """Truncate tool output, keeping head + tail.

        The effective limit is the *minimum* of:
        - ``self.tool_truncation`` (fixed cap, defaults to 50% of context)
        - ``remaining_budget_tokens`` converted to chars (if provided)

        This ensures a single tool result cannot overflow the context window
        even when the conversation is already partially full.
        """
        limit = self.tool_truncation
        if remaining_budget_tokens is not None:
            budget_chars = int(remaining_budget_tokens * self._chars_per_token)
            limit = min(limit, budget_chars)
        if limit <= 0:
            return f"[Output truncated — {len(output)} chars exceeded context budget]"
        if len(output) <= limit:
            return output
        half = limit // 2
        omitted = len(output) - limit
        return (
            output[:half]
            + f"\n\n... [{omitted} chars truncated — output exceeded "
            + f"{limit} char limit] ...\n\n"
            + output[-half:]
        )

    def request_title_refresh(self, current_title: str = "") -> None:
        """Request a title regeneration (thread-safe public API).

        Resets the title-generated flag and spawns a background thread
        to produce a new title via LLM.  Safe to call from server endpoints.
        """
        self._title_generated = False
        import threading

        threading.Thread(
            target=self._generate_title,
            args=(current_title,),
            daemon=True,
        ).start()

    def _generate_title(self, current_title: str = "") -> None:
        """Generate a short title for this session via a background LLM call.

        When *current_title* is provided (e.g. during a refresh), the prompt
        asks the LLM to produce a **different** title.
        """
        ws_id = self._ws_id  # Capture before async work
        log.info("ws.title.gen_start", ws_id=ws_id[:8])
        try:
            # Gather first user message and first assistant reply
            user_msg = ""
            asst_msg = ""
            for m in self.messages:
                content = m.get("content") or ""
                # Handle multi-part content (vision messages)
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
                if m["role"] == "user" and not user_msg:
                    user_msg = content[:300]
                elif m["role"] == "assistant" and not asst_msg:
                    asst_msg = content[:200]
                if user_msg and asst_msg:
                    break
            if not user_msg:
                log.info("ws.title.gen_skip", ws_id=ws_id[:8], reason="no_user_message")
                # Broadcast current name so UI resets any "refreshing" indicator
                if current_title and self._ws_id == ws_id:
                    self.ui.on_rename(current_title)
                return
            log.info(
                "ws.title.gen_messages",
                ws_id=ws_id[:8],
                user_msg=user_msg[:100],
                asst_msg=asst_msg[:100],
            )
            snippet = f"Generate a title for this conversation:\n\nUser: {user_msg}"
            if asst_msg:
                snippet += f"\nAssistant: {asst_msg}"
            if current_title:
                snippet += (
                    f'\n\nThe current title is: "{current_title}"\n'
                    "The user wants a DIFFERENT title. Generate a new, distinct title "
                    "that is NOT the same as the current one."
                )
            snippet += "\n\nTitle:"
            log.info("ws.title.llm_call_start", ws_id=ws_id[:8])

            # Use slightly higher temperature for refreshes to encourage variety
            temp = 0.7 if current_title else 0.3

            result = self._utility_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "# Instructions\n\n"
                            "You are a conversation title generator. "
                            "The user will show you the opening of a conversation. "
                            "Respond with ONLY a short title (3-8 words). "
                            "Do NOT answer the conversation. Do NOT explain. "
                            "Output ONLY the title text, nothing else."
                        ),
                    },
                    {"role": "user", "content": snippet},
                ],
                max_tokens=200,
                temperature=temp,
            )
            raw = (result.content or "").strip()
            log.info("ws.title.llm_response", ws_id=ws_id[:8], raw=raw[:200])
            # Take first line, strip quotes
            title = raw.split("\n")[0].strip().strip('"').strip("'")
            if title and self._ws_id == ws_id:
                log.info("ws.title.updating", ws_id=ws_id[:8], title=title)
                update_workstream_title(ws_id, title[:80])
                self.ui.on_rename(title[:80])
                log.info("ws.title.success", ws_id=ws_id[:8], title=title)
            else:
                log.info(
                    "ws.title.skip",
                    ws_id=ws_id[:8],
                    reason="empty_title_or_ws_changed",
                    title=title,
                )
                # Broadcast current name so the UI resets the "refreshing" indicator
                if current_title and self._ws_id == ws_id:
                    self.ui.on_rename(current_title)
        except Exception as e:
            # Only reset if ws_id hasn't changed (e.g., via /resume) to
            # avoid re-enabling titling for a different workstream.
            if self._ws_id == ws_id:
                self._title_generated = False
                # Broadcast current name so the UI resets the "refreshing" indicator
                if current_title:
                    self.ui.on_rename(current_title)
            log.warning("ws.title.failed", ws_id=ws_id[:8], error=str(e), exc_info=True)

    def resume(self, ws_id: str, *, fork: bool = False) -> bool:
        """Load messages from a previous workstream and resume it.

        When *fork* is ``False`` (default), replaces the current
        conversation with the loaded messages **and adopts the old
        ws_id** so new messages continue in the same workstream.

        When *fork* is ``True``, the messages are copied but
        ``self._ws_id`` is **kept unchanged** — the fork gets its own
        identity while inheriting the conversation history.

        Restores persisted config (temperature, reasoning_effort, etc.)
        so the resumed/forked workstream behaves identically to the
        original.  Returns True on success.
        """
        messages = load_messages(ws_id)
        if not messages:
            return False
        if not fork:
            self._ws_id = ws_id
        self.messages = messages
        self._read_files.clear()
        self._recent_tool_sigs.clear()
        self._last_usage = None
        self._calibrated_msg_count = 0
        self._title_generated = True  # don't re-title resumed workstreams
        self._msg_tokens = [
            max(1, int(self._msg_char_count(m) / self._chars_per_token)) for m in self.messages
        ]
        log.info(
            "Resuming ws=%s: %d messages, provider=%s, model=%s",
            ws_id,
            len(messages),
            type(self._provider).__name__,
            self.model,
        )
        # Restore persisted config
        config = load_workstream_config(ws_id)
        if config:
            # Restore model via registry (same path as /model command)
            saved_alias = config.get("model_alias", "")
            saved_model = config.get("model", "")
            if saved_alias and self._registry and self._registry.has_alias(saved_alias):
                client, model_name, cfg = self._registry.resolve(saved_alias)
                self.client = client
                self.model = model_name
                self._model_alias = saved_alias
                self._provider = self._registry.get_provider(saved_alias)
                self._cached_capabilities = None
                self._judge = None  # re-create with new client/model
                self.context_window = cfg.context_window
                if not self._manual_tool_truncation:
                    self.tool_truncation = int(cfg.context_window * self._chars_per_token * 0.5)
                log.info(
                    "Resume: resolved alias=%s → provider=%s, model=%s, ctx=%d",
                    saved_alias,
                    type(self._provider).__name__,
                    model_name,
                    cfg.context_window,
                )
            elif saved_model and saved_model != self.model:
                # No alias or alias no longer in registry — at least set the model name
                self.model = saved_model
                self._model_alias = None
                self._cached_capabilities = None
                log.warning(
                    "Resume: alias %r not in registry, keeping default provider=%s for model=%s",
                    saved_alias,
                    type(self._provider).__name__,
                    saved_model,
                )
            if "temperature" in config:
                self.temperature = float(config["temperature"])
            if "reasoning_effort" in config:
                self.reasoning_effort = config["reasoning_effort"]
            if "max_tokens" in config:
                self.max_tokens = int(config["max_tokens"])
            if "instructions" in config:
                self.instructions = config["instructions"] or None
            if "creative_mode" in config:
                self.creative_mode = config["creative_mode"] == "True"
            if "skill" in config or "template" in config:
                self._skill_name = config.get("skill") or config.get("template") or None
                self._load_skills()
            if "token_budget" in config:
                self._token_budget = int(config["token_budget"] or "0")
            if "applied_skill_id" in config:
                self._applied_skill_id = config["applied_skill_id"]
            if "applied_skill_version" in config:
                self._applied_skill_version = int(config["applied_skill_version"] or "0")
            if "applied_skill_content" in config:
                self._applied_skill_content = config["applied_skill_content"]
                if self._applied_skill_content:
                    self._skill_content = self._applied_skill_content
                    self._skill_name = None
            if "notify_on_complete" in config:
                self._notify_on_complete = config["notify_on_complete"]
        # When forking, persist the copied messages and restored config under
        # the fork's own ws_id so they survive restarts.
        if fork:
            # Bulk-insert all messages in a single transaction for performance.
            bulk_rows: list[dict[str, Any]] = []
            for msg in self.messages:
                tc = msg.get("tool_calls")
                tc_json = json.dumps(tc) if tc else None
                pd = msg.get("provider_data")
                try:
                    pd_str = json.dumps(pd) if pd and not isinstance(pd, str) else pd
                except (TypeError, ValueError):
                    pd_str = None
                bulk_rows.append(
                    {
                        "ws_id": self._ws_id,
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", ""),
                        "tool_name": msg.get("name"),
                        "tool_call_id": msg.get("tool_call_id"),
                        "tool_calls": tc_json,
                        "provider_data": pd_str,
                    }
                )
            save_messages_bulk(bulk_rows)
            self._save_config()
            self._title_generated = False  # allow auto-title for the fork
            log.info(
                "ws.fork.messages_copied",
                source_ws_id=ws_id[:8],
                fork_ws_id=self._ws_id[:8],
                message_count=len(self.messages),
            )

        if self._mem_cfg.nudges and should_nudge(
            "resume",
            self._metacog_state,
            message_count=len(self.messages),
            memory_count=self._visible_memory_count(),
            cooldown_secs=self._mem_cfg.nudge_cooldown,
        ):
            self._pending_nudge.append(("resume", format_nudge("resume")))
        self._init_system_messages()
        return True

    def _init_system_messages(self) -> None:
        """Build the system/developer prefix messages.

        Developer message contains tool patterns (or creative writing
        instructions when creative_mode is on), plus any user-supplied
        instructions and memory reminders.

        Uses copy-on-write: builds new lists locally, then assigns
        atomically so concurrent readers (e.g. background thread
        callbacks) never see a partially-built system message.
        """
        new_system_messages: list[dict[str, Any]] = []

        # -- Chat template kwargs --
        self._chat_template_kwargs_base: dict[str, Any] = {
            "reasoning_effort": self.reasoning_effort,
        }

        # -- Developer message --
        if self.creative_mode:
            dev_parts = [
                "# Instructions",
                "",
                (
                    "You are a creative writing partner. Use the analysis channel to "
                    "think through structure, voice, and intent before drafting."
                ),
                "",
                "Craft principles:",
                "- Ground scenes in concrete sensory detail — what is seen, heard, felt.",
                (
                    "- Vary rhythm. Short sentences hit hard. Longer ones carry the reader "
                    "through texture and nuance, building toward something."
                ),
                (
                    "- Dialogue should do at least two things: reveal character AND advance "
                    "plot or tension. Cut anything that's just exchanging information."
                ),
                (
                    "- Earn your abstractions. Don't say 'she felt sad' — show the thing "
                    "that makes the reader feel it."
                ),
                "- Trust subtext. Leave room for the reader.",
                "",
                (
                    "Match the user's genre and tone. If they want literary fiction, write "
                    "literary fiction. If they want pulp, write pulp with conviction. "
                    "Never condescend to the form."
                ),
            ]
        else:
            # Compose system message from modular components
            tool_names = frozenset(t["function"]["name"] for t in self._tools if "function" in t)
            # Load DB prompt policies if storage is available
            db_policies: list[dict[str, Any]] = []
            try:
                storage = get_storage()
                if storage:
                    db_policies = storage.list_prompt_policies()
            except Exception:
                log.debug("Failed to load prompt policies from storage", exc_info=True)
            now = datetime.now().astimezone()
            ctx = SessionContext(
                current_datetime=now.strftime("%Y-%m-%dT%H:%M"),
                timezone=now.tzname() or "UTC",
                username=self._username or self._user_id or "unknown",
            )
            composed = compose_system_message(
                client_type=self._client_type,
                context=ctx,
                available_tools=tool_names,
                policies=["web_search"],
                db_policies=db_policies,
                kind=self._kind,
            )
            dev_parts = [composed]
        # Tool search hint (client-side mode only — native mode needs no hint).
        # Uses _resolve_capabilities directly rather than _get_capabilities so
        # we don't populate self._cached_capabilities during __init__; that
        # would make later patches of provider.get_capabilities (common in
        # tests) silently no-op for the primary session model.  The flag we
        # read here is cheap to recompute; no caching is required.
        if self._tool_search:
            caps = self._resolve_capabilities(self._provider, self.model, self._model_alias)
            if not caps.supports_tool_search:
                dev_parts.append(
                    "\n\nAdditional tools are available via tool_search. "
                    "Use it when you need a capability not in your current tool set."
                )
        # MCP resource catalog (lets the model know what's available for read_resource)
        if self._mcp_client:
            all_resources = self._mcp_client.get_resources()
            concrete = [r for r in all_resources if not r.get("template")]
            templates = [r for r in all_resources if r.get("template")]
            if concrete or templates:
                lines = ["\n<mcp-resources>"]
                for r in concrete[:50]:
                    safe_uri = _html_escape(r["uri"])
                    desc = r.get("description", "")
                    if desc:
                        desc = f"  {_html_escape(desc[:100])}"
                    lines.append(f"  {safe_uri}{desc}")
                if templates:
                    lines.append("")
                    lines.append("Resource templates (construct a URI and use read_resource):")
                    for t in templates[:20]:
                        safe_uri = _html_escape(t["uri"])
                        desc = t.get("description", "")
                        if desc:
                            desc = f"  {_html_escape(desc[:100])}"
                        lines.append(f"  {safe_uri}{desc}")
                lines.append("</mcp-resources>")
                lines.append("Use read_resource(uri='...') to access the resources listed above.")
                dev_parts.append("\n".join(lines))
        # MCP prompt catalog (lets the model know what's available for use_prompt)
        if self._mcp_client:
            prompts = self._mcp_client.get_prompts()
            if prompts:
                lines = ["<mcp-prompts>"]
                for p in prompts[:30]:
                    # Names/args are NOT escaped — model must use exact strings
                    # in use_prompt(). Only description (display-only) is escaped.
                    arg_names = ", ".join(a["name"] for a in p.get("arguments", []))
                    desc = _html_escape(p.get("description", "")[:100])
                    lines.append(f"  {p['name']}({arg_names})  {desc}")
                lines.append("</mcp-prompts>")
                lines.append(
                    "Use use_prompt(name='...', arguments={...}) "
                    "to invoke the prompts listed above."
                )
                dev_parts.append("\n".join(lines))
        if self._skill_content:
            tpl = self._skill_content
            if len(tpl) > _MAX_SKILL_CONTENT:
                log.warning("skill_content.truncated", length=len(tpl))
                tpl = tpl[:_MAX_SKILL_CONTENT]
            dev_parts.append("")
            dev_parts.append(tpl)
            if self._skill_resources:
                lines = ["<skill-resources>"]
                total_size = 0
                for rpath, rcontent in sorted(self._skill_resources.items()):
                    size_kb = f"{len(rcontent) / 1024:.1f}KB"
                    total_size += len(rcontent)
                    lines.append(f"- {rpath} ({size_kb})")
                if total_size <= 8192:
                    for rpath, rcontent in sorted(self._skill_resources.items()):
                        lines.append(f"\n--- {rpath} ---")
                        lines.append(rcontent)
                else:
                    lines.append(
                        "Resource content omitted (total exceeds 8KB). "
                        "Resource files are listed above by path and size."
                    )
                if self._skill_resources_dir:
                    lines.append(
                        "\nResource files are materialized on disk. "
                        "Scripts in scripts/ are on PATH and can be run by name. "
                        "All files are under $SKILL_RESOURCES_DIR."
                    )
                lines.append("</skill-resources>")
                dev_parts.append("\n".join(lines))
        # Skill catalog: disclose search-activated skills so the model
        # knows they exist (Agent Skills standard progressive disclosure).
        try:
            search_skills = list_skills_by_activation("search", enabled_only=True, limit=30)
        except Exception:
            log.warning("session.skill_catalog_failed", exc_info=True)
            search_skills = []
        # Exclude the already-applied skill from the catalog so the model
        # doesn't suggest activating a skill that is already loaded.
        applied_name = self._skill_name or ""
        search_skills = [sk for sk in search_skills if sk.get("name", "") != applied_name]
        if search_skills:
            catalog_lines = ["<available-skills>"]
            for sk in search_skills[:30]:
                sk_name = _html_escape(sk.get("name", ""))
                sk_desc = _html_escape(sk.get("description", "")[:200])
                catalog_lines.append(
                    f"  <skill><name>{sk_name}</name><description>{sk_desc}</description></skill>"
                )
            catalog_lines.append("</available-skills>")
            catalog_lines.append(
                "Additional skills are available. When a task matches a skill "
                "description, ask the user to activate it with `/skill <name>`, "
                "or use `/skill search <query>` to find relevant skills."
            )
            dev_parts.append("\n".join(catalog_lines))
        if self.instructions:
            dev_parts.append("")
            dev_parts.append(self.instructions)
        visible_mems = self._list_visible_memories(limit=self._mem_cfg.fetch_limit)
        if visible_mems:
            context = extract_recent_context(self.messages)
            relevant = score_memories(visible_mems, context, k=self._mem_cfg.relevance_k)
            if relevant:
                dev_parts.append("")
                dev_parts.append(build_memory_context(relevant))
            # Only advertise the memory(...) tool invocations when the
            # memory tool is actually in the session's schema.  The
            # coordinator kind doesn't register memory; the preamble
            # previously told the model to call a tool it doesn't have,
            # producing "I don't have access to a memory tool" apologies
            # or hallucinated calls.
            if "memory" in tool_names:
                dev_parts.append("")
                dev_parts.append(
                    f"You have {len(visible_mems)} memories in scope. "
                    "Use memory(action='search') or memory(action='list') for more."
                )
        if self._pending_nudge:
            types = []
            for nudge_type, nudge_text in self._pending_nudge:
                dev_parts.append("")
                dev_parts.append(nudge_text)
                types.append(nudge_type)
            # Surface nudge activity to the user so they can see
            # the metacognitive prompts being applied.
            self.ui.on_info(f"{GRAY}[metacognition: nudge injected — {', '.join(types)}]{RESET}")
            self._pending_nudge.clear()
        new_system_messages.append({"role": "system", "content": "\n".join(dev_parts)})
        # Atomic swap — readers see either old or new, never partial
        self.system_messages = new_system_messages
        # Agent prefix: system + developer only (no memories)
        self._agent_system_messages = list(new_system_messages)

    def _full_messages(self) -> list[dict[str, Any]]:
        """System messages + conversation history."""
        return self.system_messages + self.messages

    def _emit_state(self, state: str) -> None:
        """Notify UI of a workstream state transition."""
        self.ui.on_state_change(state)

    def _provider_extra_params(
        self,
        reasoning_effort: str | None = None,
        provider: LLMProvider | None = None,
        model_alias: str | None = None,
    ) -> dict[str, Any] | None:
        """Build provider-specific extra parameters.

        ``chat_template_kwargs`` is only meaningful for local model servers
        (``openai-compatible``).  Commercial OpenAI rejects it as an unknown
        parameter, and handles ``reasoning_effort`` natively.

        Merges server workarounds (``skip_special_tokens``, etc.) from
        ``ModelConfig.server_compat`` into the request's ``extra_body``.
        Thinking-mode params (``enable_thinking``) are handled separately
        by the provider based on ``ModelCapabilities.thinking_mode``.

        *model_alias* controls which model config supplies server compat
        settings.  When ``None``, defaults to the session's primary alias.
        """
        from turnstone.core.server_compat import merge_server_compat

        prov = provider or self._provider
        if prov.provider_name == "openai-compatible":
            ctk_base = dict(self._chat_template_kwargs_base)
            if reasoning_effort:
                ctk_base["reasoning_effort"] = reasoning_effort
            return merge_server_compat(
                ctk_base,
                self._get_server_compat(model_alias),
            )
        return None

    def _get_server_compat(self, model_alias: str | None = None) -> dict[str, Any]:
        """Get server compatibility settings from a model config.

        *model_alias* selects the config to read.  Falls back to the
        session's primary alias when ``None``.
        """
        alias = model_alias or self._model_alias
        if self._registry and alias:
            try:
                cfg = self._registry.get_config(alias)
                return dict(cfg.server_compat)
            except (ValueError, KeyError):
                pass
        return {}

    def _utility_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        reasoning_effort: str = "low",
    ) -> CompletionResult:
        """Run a lightweight internal completion (title gen, compaction, extraction).

        Threads ``reasoning_effort`` through both the direct keyword (for
        commercial providers) and ``extra_params`` (for local model servers)
        so callers don't need to duplicate it.  ``max_tokens`` is clamped to
        the model's advertised output limit so small models don't error.
        """
        caps = self._get_capabilities()
        clamped = min(max_tokens, caps.max_output_tokens) if caps.max_output_tokens else max_tokens
        return self._provider.create_completion(
            client=self.client,
            model=self.model,
            messages=messages,
            max_tokens=clamped,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            extra_params=self._provider_extra_params(reasoning_effort=reasoning_effort),
            capabilities=caps,
        )

    # -- tool search helpers --------------------------------------------------

    def _get_active_tools(self) -> list[dict[str, Any]] | None:
        """Return the tool list to send to the LLM.

        When tool search is active:
        - Native mode (provider supports it): send all tools (provider
          marks deferred ones with defer_loading).
        - Client-side fallback: send visible tools + synthetic tool_search.

        Without tool search: return self._tools unchanged.

        Web search gating: ``web_search`` is removed when the model has
        no native search support and no search backend is available
        (Tavily, DDG, or MCP — see ``_resolve_search_client``).

        MCP tool gating: ``read_resource`` is removed when no MCP servers
        expose resources; ``use_prompt`` is removed when none expose prompts.
        """
        if self.creative_mode:
            return None
        caps = self._get_capabilities()
        if not self._tool_search:
            tools = self._tools
        else:
            if caps.supports_tool_search:
                # Provider handles defer_loading — send all tools
                tools = self._tools
            else:
                # Client-side fallback: visible tools + search tool
                visible = self._tool_search.get_visible_tools()
                tools = visible + [self._tool_search.get_search_tool_definition()]

        # Gate web_search: only include when a backend exists
        if not caps.supports_web_search and not self._resolve_search_client():
            tools = _without_tool(tools, "web_search")

        # Gate MCP tools: only include when relevant MCP servers are connected
        if not self._mcp_client or not self._mcp_client.resource_count:
            tools = _without_tool(tools, "read_resource")
        if not self._mcp_client or not self._mcp_client.prompt_count:
            tools = _without_tool(tools, "use_prompt")

        return tools

    def _get_deferred_names(self) -> frozenset[str] | None:
        """Return names of deferred tools for native provider search, or None."""
        if not self._tool_search:
            return None
        caps = self._get_capabilities()
        if not caps.supports_tool_search:
            return None  # Client-side mode — no deferred names for provider
        deferred = self._tool_search.get_deferred_tools()
        return frozenset(name for t in deferred if (name := t.get("function", {}).get("name", "")))

    # Retryable error names are now provided by LLMProvider.retryable_error_names.
    _MAX_RETRIES = 3
    _RETRY_BASE_DELAY = 1.0  # seconds

    def _get_health_tracker(self) -> BackendHealthTracker | None:
        """Get the health tracker for this session's current backend.

        Uses a read-only lookup — only returns trackers that were already
        created eagerly at startup or during model reload.

        Returns ``None`` when no health registry is configured, the model
        alias is unknown, or no tracker exists for this backend yet.
        """
        if not self._health_registry or not self._registry or not self._model_alias:
            return None
        return self._health_registry.get_tracker_for_alias(self._registry, self._model_alias)

    def _create_stream_with_retry(self, msgs: list[dict[str, Any]]) -> Iterator[StreamChunk]:
        """Create a streaming request with retry on transient errors.

        If all retries fail and a fallback chain is configured, tries each
        fallback model in order before giving up.  Records success/failure
        on the per-backend health tracker for observability.
        """
        tracker = self._get_health_tracker()

        try:
            result = self._try_stream(self.client, self.model, msgs)
            if tracker:
                tracker.record_success()
            return result
        except Exception as primary_err:
            if tracker:
                tracker.record_failure()
            if not self._registry or not self._registry.fallback:
                raise
            # Try each fallback model.  Prefer non-degraded backends first,
            # but still try degraded ones as a last resort.
            degraded_fallbacks: list[str] = []
            for alias in self._registry.fallback:
                if alias == self._model_alias:
                    continue
                # Skip degraded backends on the first pass
                if self._health_registry:
                    fb_tracker = self._health_registry.get_tracker_for_alias(self._registry, alias)
                    if fb_tracker and fb_tracker.is_degraded:
                        degraded_fallbacks.append(alias)
                        continue
                stream = self._try_fallback(alias, msgs)
                if stream is not None:
                    return stream
            # Second pass: try degraded backends as last resort
            for alias in degraded_fallbacks:
                self.ui.on_info(f"[Fallback {alias} is degraded, trying anyway]")
                stream = self._try_fallback(alias, msgs)
                if stream is not None:
                    return stream
            raise primary_err

    def _try_fallback(self, alias: str, msgs: list[dict[str, Any]]) -> Iterator[StreamChunk] | None:
        """Attempt a single fallback model. Returns stream or None.

        Records success/failure on the fallback's health tracker so
        the two-pass ordering (healthy-first, then degraded) learns
        across request cycles.

        Caller must ensure ``self._registry`` is not ``None``.
        """
        assert self._registry is not None
        fb_tracker = (
            self._health_registry.get_tracker_for_alias(self._registry, alias)
            if self._health_registry
            else None
        )
        try:
            fb_client, fb_model, _ = self._registry.resolve(alias)
            fb_provider = self._registry.get_provider(alias)
            fb_caps = self._resolve_capabilities(fb_provider, fb_model, alias)
            self.ui.on_info(f"[Primary model failed, falling back to {alias}]")
            result = self._try_stream(
                fb_client,
                fb_model,
                msgs,
                provider=fb_provider,
                capabilities=fb_caps,
                model_alias=alias,
            )
            if fb_tracker:
                fb_tracker.record_success()
            return result
        except Exception as fb_err:
            if fb_tracker:
                fb_tracker.record_failure()
            self.ui.on_info(f"[Fallback {alias} also failed: {fb_err}]")
            return None

    def _try_stream(
        self,
        client: Any,
        model: str,
        msgs: list[dict[str, Any]],
        provider: LLMProvider | None = None,
        capabilities: ModelCapabilities | None = None,
        model_alias: str | None = None,
    ) -> Iterator[StreamChunk]:
        """Attempt a streaming API call with retries on transient errors."""
        prov = provider or self._provider
        raw_url = str(getattr(client, "base_url", getattr(client, "_base_url", "?")))
        safe_url = raw_url.split("?")[0]  # strip query params (may contain keys)
        msg_count = len(msgs)
        role_counts: dict[str, int] = {}
        for m in msgs:
            r = m.get("role", "?")
            role_counts[r] = role_counts.get(r, 0) + 1
        log.debug(
            "API call: provider=%s model=%s base_url=%s msgs=%d roles=%s",
            type(prov).__name__,
            model,
            safe_url,
            msg_count,
            role_counts,
        )
        last_err: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            self._check_cancelled()
            self._cancel_ref.clear()  # discard stale handle from prior attempt
            try:
                return prov.create_streaming(
                    client=client,
                    model=model,
                    messages=msgs,
                    tools=self._get_active_tools(),
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    reasoning_effort=self.reasoning_effort,
                    extra_params=self._provider_extra_params(
                        provider=prov, model_alias=model_alias
                    ),
                    deferred_names=self._get_deferred_names(),
                    cancel_ref=self._cancel_ref,
                    capabilities=capabilities or self._get_capabilities(prov, model),
                )
            except Exception as e:
                ename = type(e).__name__
                cause_name = (
                    type(e.__cause__).__name__
                    if e.__cause__
                    else (type(e.__context__).__name__ if e.__context__ else "None")
                )
                log.warning(
                    "API error (attempt %d/%d): %s (cause=%s) "
                    "provider=%s model=%s base_url=%s msgs=%d",
                    attempt + 1,
                    self._MAX_RETRIES + 1,
                    ename,
                    cause_name,
                    type(prov).__name__,
                    model,
                    safe_url,
                    msg_count,
                )
                log.debug(
                    "API error details (attempt %d/%d)",
                    attempt + 1,
                    self._MAX_RETRIES + 1,
                    exc_info=True,
                )
                if ename not in prov.retryable_error_names or attempt == self._MAX_RETRIES:
                    raise
                last_err = e
                delay = self._RETRY_BASE_DELAY * (2**attempt)
                self.ui.on_info(f"[Retrying in {delay:.0f}s: {ename}]")
                time.sleep(delay)
        assert last_err is not None  # unreachable, but satisfies type checker
        raise last_err

    # -- Cancellation -------------------------------------------------------

    def cancel(self) -> None:
        """Request cancellation of the current generation.

        Thread-safe — may be called from any thread (e.g. an HTTP handler)
        while the worker thread is inside ``send()``.
        """
        self._cancel_event.set()
        # Close the underlying SDK stream to unblock the iteration
        # immediately.  Without this the worker thread stays blocked in
        # ``for chunk in stream`` until the next SSE chunk arrives from
        # the LLM provider (can be seconds during extended thinking).
        s = self._cancel_stream
        if s is not None:
            with contextlib.suppress(Exception):
                s.close()
        # Kill all tracked subprocesses (bash tool).  This is the
        # last line of defense — ensures destructive commands are
        # stopped even if the worker thread is stuck.
        with self._procs_lock:
            procs = list(self._active_procs)
        for proc in procs:
            if proc.poll() is not None:
                continue  # already exited
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                with contextlib.suppress(OSError, ProcessLookupError):
                    proc.kill()

    def _check_cancelled(self, my_generation: int = 0) -> None:
        """Raise ``GenerationCancelled`` if cancellation has been requested
        or if this thread belongs to an orphaned generation (force cancel).
        """
        if self._cancel_event.is_set():
            raise GenerationCancelled()
        if my_generation and my_generation != self._generation:
            raise GenerationCancelled()

    def _append_user_turn(
        self,
        user_input: str,
        attachments: list[Attachment] | tuple[Attachment, ...],
        send_id: str | None = None,
    ) -> int:
        """Append a user turn (plain or multipart) and persist it.

        When ``attachments`` is non-empty the in-memory message carries
        list content (text + image_url + document parts); the DB
        conversations row stores only the text — attachments link back
        via ``workstream_attachments.message_id``.  Returns the saved
        conversations row id (0 on save failure, per the storage
        wrapper's no-raise contract).

        ``send_id`` (when provided) is the reservation token; the
        consume step adds it to the WHERE clause so a stale send can't
        steal rows reserved to a different one.
        """
        user_content: str | list[dict[str, Any]]
        if attachments:
            parts: list[dict[str, Any]] = [{"type": "text", "text": user_input}]
            for att in attachments:
                if att.is_image:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": _encode_image_data_uri(att.content, att.mime_type),
                            },
                        }
                    )
                elif att.is_text:
                    try:
                        text = att.content.decode("utf-8")
                    except UnicodeDecodeError:
                        log.warning(
                            "attachment id=%s is not valid UTF-8; injecting placeholder",
                            att.attachment_id,
                        )
                        parts.append(unreadable_placeholder(att.filename))
                        continue
                    parts.append(
                        {
                            "type": "document",
                            "document": {
                                "name": att.filename,
                                "media_type": att.mime_type,
                                "data": text,
                            },
                        }
                    )
                else:
                    log.warning(
                        "attachment id=%s has unknown kind=%r; injecting placeholder",
                        att.attachment_id,
                        att.kind,
                    )
                    parts.append(unreadable_placeholder(att.filename))
            user_content = parts
        else:
            user_content = user_input

        user_msg: dict[str, Any] = {"role": "user", "content": user_content}
        if attachments:
            # Sibling metadata so live history replay has the same shape
            # as reloaded-from-DB (filenames are not recoverable from an
            # image_url data URI).  sanitize_messages strips leading-
            # underscore keys before the wire call so this is safe.
            user_msg["_attachments_meta"] = [
                {
                    "kind": a.kind,
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                }
                for a in attachments
            ]
        self.messages.append(user_msg)
        self._msg_tokens.append(max(1, int(self._msg_char_count(user_msg) / self._chars_per_token)))
        # DB row stores the raw text only; attachments are joined back in
        # from workstream_attachments on load via message_id.  Save →
        # consume are two separate transactions; a crash between them
        # leaves pending rows that the UI's chip rehydration can still
        # surface so the user can clear or resend them.
        message_id = save_message(self._ws_id, "user", user_input)
        if attachments and message_id:
            mark_attachments_consumed(
                [a.attachment_id for a in attachments],
                message_id,
                self._ws_id,
                self._user_id,
                reserved_for_msg_id=send_id,
            )
        return message_id

    # -- Main generation loop ------------------------------------------------

    def send(
        self,
        user_input: str,
        attachments: list[Attachment] | None = None,
        send_id: str | None = None,
    ) -> None:
        """Send user input and handle the response loop (including tool calls).

        When ``attachments`` is provided the in-memory user message carries
        multipart list content (text + image_url + document parts) while
        the DB conversations row stores only the text — attachments are
        linked via ``message_id`` in the workstream_attachments table.

        ``send_id`` is the server-side reservation token for the
        attachments; on consume, the storage layer matches it against
        ``reserved_for_msg_id`` so a stale send can't steal rows
        reserved to a different one.
        """
        self._refresh_model_from_registry()
        # Token budget approval gate
        if self._budget_exhausted:
            approved, _ = self.ui.approve_tools(
                [
                    {
                        "func_name": "__budget_override__",
                        "preview": (
                            f"Token budget ({self._token_budget:,}) exhausted. Approve to continue."
                        ),
                        "needs_approval": True,
                    }
                ]
            )
            if not approved:
                self.ui.on_error("Token budget exhausted. Approval required to continue.")
                return
            self._budget_exhausted = False
            self._budget_warned = False
        self._notify_count = 0
        self._generation += 1
        my_generation = self._generation
        # Fresh cancel event per generation.  The old event object stays
        # set for any abandoned thread — _exec_bash captures a local
        # reference so subprocesses from old generations are still killed.
        self._cancel_event = threading.Event()
        self._cancelled_partial_msg = None

        self._append_user_turn(user_input, attachments or (), send_id=send_id)

        # Metacognitive nudge: check for correction/completion signals
        nudge = self._check_metacognitive_nudge(user_input)
        if nudge:
            self._pending_nudge.append(nudge)
            self._init_system_messages()

        try:
            while True:
                self._check_cancelled(my_generation)
                msgs = self._full_messages()

                if self.debug:
                    self._debug_print_request(msgs)

                self._emit_state("thinking")
                self.ui.on_thinking_start()
                try:
                    try:
                        stream = self._create_stream_with_retry(msgs)
                    except Exception as ctx_err:
                        # Context overflow recovery: if the API rejects the
                        # request due to exceeding the context window, compact
                        # the conversation and retry once.
                        err_text = str(ctx_err).lower()
                        is_ctx_overflow = any(
                            s in err_text
                            for s in (
                                "context length",
                                "maximum context",
                                "too many tokens",
                                "prompt is too long",
                                "input tokens",
                            )
                        )
                        if not is_ctx_overflow:
                            raise
                        log.warning(
                            "Context overflow detected (%s), compacting and retrying",
                            type(ctx_err).__name__,
                        )
                        self.ui.on_info("\n[Context overflow — auto-compacting and retrying]")
                        # Stop thinking indicator before compact (which has
                        # its own thinking start/stop) to avoid nested spinners.
                        self.ui.on_thinking_stop()
                        try:
                            self._compact_messages(auto=True)
                            msgs = self._full_messages()
                            self.ui.on_thinking_start()
                            stream = self._create_stream_with_retry(msgs)
                        except Exception:
                            log.warning(
                                "Compact-and-retry failed, raising original error",
                                exc_info=True,
                            )
                            raise ctx_err from None
                    assistant_msg = self._stream_response(stream, my_generation)
                finally:
                    # Only clear if this generation is still active —
                    # an orphaned thread must not clobber a newer stream.
                    if self._generation == my_generation:
                        self._cancel_stream = None
                        self._cancel_ref.clear()
                    self.ui.on_thinking_stop()

                # Bail if this generation was superseded (force cancel).
                if self._generation != my_generation:
                    return

                self._update_token_table(assistant_msg)
                self._print_status_line()  # Report usage for EVERY API call
                self.messages.append(assistant_msg)
                self._msg_tokens.append(
                    self._assistant_pending_tokens
                    or max(
                        1,
                        int(self._msg_char_count(assistant_msg) / self._chars_per_token),
                    )
                )

                # Log assistant message to conversation history
                content = assistant_msg.get("content", "")
                tc = assistant_msg.get("tool_calls")
                provider_data = None
                if assistant_msg.get("_provider_content"):
                    provider_data = json.dumps(assistant_msg["_provider_content"])

                # Build tool_calls JSON (excluding memory tools)
                tool_calls_json: str | None = None
                if tc:
                    filtered_tc = [
                        call
                        for call in tc
                        if call.get("function", {}).get("name", "") not in ("memory", "recall")
                    ]
                    if filtered_tc:
                        tool_calls_json = json.dumps(filtered_tc)

                # Save assistant message atomically (content + tool_calls in one row)
                if content or provider_data is not None or tool_calls_json:
                    save_message(
                        self._ws_id,
                        "assistant",
                        content,
                        provider_data=provider_data,
                        tool_calls=tool_calls_json,
                    )

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    # Auto-compact when prompt exceeds threshold
                    if (
                        self._last_usage
                        and self._last_usage["prompt_tokens"]
                        > self.context_window * self.auto_compact_pct
                    ):
                        pct_display = int(self.auto_compact_pct * 100)
                        self.ui.on_info(
                            f"\n[Auto-compacting: prompt exceeds {pct_display}% of context window]"
                        )
                        self._compact_messages(auto=True)
                        # Update status bar with post-compaction token counts
                        self._print_status_line()
                    # Auto-title session after first exchange
                    if not self._title_generated:
                        self._title_generated = True
                        threading.Thread(target=self._generate_title, daemon=True).start()
                    # Flush any queued messages that weren't injected
                    # (no tool calls → no advisory seam to inject at).
                    self._flush_queued_messages()
                    self._emit_state("idle")
                    # Dispatch any pending watch results (chains into
                    # a new send() within the same worker thread).
                    self._dispatch_pending_watch(self._watch_dispatch_depth)
                    break

                # Execute tool calls (potentially in parallel)
                self._emit_state("running")
                results, user_feedback = self._execute_tools(tool_calls)

                # Bail if generation was superseded during tool execution.
                if self._generation != my_generation:
                    return

                # Repeat detection: warn when a tool is called with identical args.
                # Skip error outputs — retrying a failed tool is valid.
                # Skip JSON outputs (MCP structured results) — appending
                # text would corrupt the payload.
                _tc_by_id = {c["id"]: c for c in tool_calls}
                _repeat_detected = False
                _error_prefixes = (
                    "Error",
                    "JSON parse error",
                    "Unknown tool",
                    "Command timed out",
                    "Blocked:",
                    "Denied",
                )

                # Clear dedup sigs when a write tool executed successfully —
                # the state has changed so re-running a read tool is valid.
                _write_tools = frozenset({"write_file", "edit_file", "bash"})
                if any(
                    tc["function"]["name"] in _write_tools
                    and not any(
                        cid == tc["id"] and isinstance(out, str) and out.startswith(_error_prefixes)
                        for cid, out in results
                    )
                    for tc in tool_calls
                ):
                    self._recent_tool_sigs.clear()
                for i, (tc_id, output) in enumerate(results):
                    tc = _tc_by_id.get(tc_id)
                    if tc and isinstance(output, str) and not output.startswith(_error_prefixes):
                        raw = tc["function"]["name"] + ":" + tc["function"]["arguments"]
                        sig = hashlib.sha256(raw.encode()).hexdigest()
                        is_json = output.lstrip().startswith(("{", "["))
                        if sig in self._recent_tool_sigs:
                            _repeat_detected = True
                            if not is_json:
                                output += (
                                    "\n\n⚠ Warning: this is an identical repeat of a "
                                    "previous tool call. The result is the same. "
                                    "Try a different approach."
                                )
                                results[i] = (tc_id, output)
                            self.ui.on_info(
                                f"{GRAY}[repeat: {tc['function']['name']}() "
                                f"called with same arguments]{RESET}"
                            )
                        self._recent_tool_sigs.add(sig)
                if _repeat_detected:
                    # Reset so the model gets a clean slate after the warning.
                    # If it repeats again, a new warning fires.
                    self._recent_tool_sigs.clear()
                    if self._mem_cfg.nudges and should_nudge(
                        "repeat",
                        self._metacog_state,
                        message_count=len(self.messages),
                        cooldown_secs=self._mem_cfg.nudge_cooldown,
                    ):
                        self._pending_nudge.append(("repeat", format_nudge("repeat")))
                        self._init_system_messages()

                # Map tool_call_id → tool name for logging
                from turnstone.core.tool_advisory import wrap_tool_result

                _tc_names = {c["id"]: c.get("function", {}).get("name", "") for c in tool_calls}
                _last_idx = len(results) - 1
                for _ri, (tc_id, output) in enumerate(results):
                    # Output guard: evaluate tool result before it enters context
                    assessment: OutputAssessment | None = None
                    if self._judge_cfg and self._judge_cfg.output_guard:
                        if isinstance(output, str):
                            output, assessment = self._evaluate_output(
                                tc_id, output, _tc_names.get(tc_id, "")
                            )
                        elif isinstance(output, list):
                            # Image/structured output — evaluate each text part
                            # independently so credentials in any part get redacted.
                            for p in output:
                                if (
                                    isinstance(p, dict)
                                    and p.get("type") == "text"
                                    and p.get("text")
                                ):
                                    p["text"], _part_assess = self._evaluate_output(
                                        tc_id, p["text"], _tc_names.get(tc_id, "")
                                    )
                                    if _part_assess is not None:
                                        assessment = _part_assess

                    # Safety truncation: clamp output to remaining context budget
                    # so a single large result cannot overflow the context window.
                    if isinstance(output, str):
                        budget = self._remaining_token_budget()
                        output = self._truncate_output(output, remaining_budget_tokens=budget)

                    # Capture raw output for DB storage before advisory wrapping
                    raw_output = output

                    # Advisory injection: wrap tool output with advisories
                    # (output guard findings, queued user messages, etc.)
                    advisories = self._collect_advisories(
                        assessment, _tc_names.get(tc_id, ""), _ri == _last_idx
                    )
                    if isinstance(output, str):
                        output = wrap_tool_result(output, advisories)
                    elif isinstance(output, list) and advisories:
                        # Structured/image output — append advisories as a
                        # text part so they aren't silently dropped.
                        output = [
                            *output,
                            {"type": "text", "text": wrap_tool_result("", advisories)},
                        ]

                    tool_msg: dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": output,
                    }
                    if self._tool_error_flags.pop(tc_id, False):
                        tool_msg["is_error"] = True
                    self.messages.append(tool_msg)

                    # Token estimation — image content uses a fixed heuristic
                    if isinstance(output, list):
                        text_chars = sum(
                            len(p.get("text", "")) for p in output if p.get("type") == "text"
                        )
                        image_count = sum(1 for p in output if p.get("type") == "image_url")
                        tok_est = max(
                            1,
                            int(text_chars / self._chars_per_token) + image_count * 1000,
                        )
                    else:
                        tok_est = max(1, int(len(output) / self._chars_per_token))
                    self._msg_tokens.append(tok_est)

                    # Log tool result (skip memory tools to avoid noise).
                    # Use raw_output (pre-advisory-wrap) so DB stores clean
                    # tool output without ephemeral advisory XML.
                    _tname = _tc_names.get(tc_id, "")
                    if _tname not in (
                        "memory",
                        "recall",
                    ):
                        if isinstance(raw_output, list):
                            store_text = " ".join(
                                p.get("text", "") for p in raw_output if p.get("type") == "text"
                            )[:2000]
                        else:
                            store_text = raw_output[:2000]
                        save_message(
                            self._ws_id,
                            "tool",
                            store_text,
                            _tname,
                            tool_call_id=tc_id,
                        )
                # Metacognitive nudge: check memories on tool error
                if (
                    self._mem_cfg.nudges
                    and any(
                        isinstance(out, str)
                        and (
                            out.startswith("Error")
                            or " error: " in out[:50]
                            or out.startswith("Command timed out")
                            or out.startswith("Unknown tool:")
                        )
                        for _, out in results
                    )
                    and should_nudge(
                        "tool_error",
                        self._metacog_state,
                        message_count=len(self.messages),
                        memory_count=self._visible_memory_count(),
                        cooldown_secs=self._mem_cfg.nudge_cooldown,
                    )
                ):
                    self._pending_nudge.append(("tool_error", format_nudge("tool_error")))
                    self._init_system_messages()
                # Inject user feedback from approval prompt (e.g. "y, use full path")
                if user_feedback:
                    self.messages.append({"role": "user", "content": user_feedback})
                    self._msg_tokens.append(max(1, int(len(user_feedback) / self._chars_per_token)))

                # Mid-turn compaction: prevent context overflow during long
                # tool chains.  Uses local estimates since _last_usage reflects
                # the previous API call, not the tool results just appended.
                estimated_prompt = self._system_tokens + sum(self._msg_tokens)
                if estimated_prompt > self.context_window * self.auto_compact_pct:
                    pct_display = int(self.auto_compact_pct * 100)
                    self.ui.on_info(
                        f"\n[Auto-compacting mid-turn: estimated prompt "
                        f"exceeds {pct_display}% of context window]"
                    )
                    self._compact_messages(auto=True)
        except GenerationCancelled:
            # If a newer send() has started (force cancel), this thread is
            # orphaned — skip all message mutations and state changes.
            if self._generation != my_generation:
                return
            # Cooperative cancellation — preserve partial content if available.
            if self._cancelled_partial_msg:
                # _stream_response was interrupted — save partial assistant msg
                msg = self._cancelled_partial_msg
                self._cancelled_partial_msg = None
                self.messages.append(msg)
                tok_est = max(
                    1,
                    int(self._msg_char_count(msg) / self._chars_per_token),
                )
                self._msg_tokens.append(tok_est)
                content = msg.get("content", "")
                if content:
                    save_message(self._ws_id, "assistant", content)
            else:
                # Cancelled during tool execution — synthesize cancelled
                # tool_result for any tool_calls that lack a matching result.
                # This keeps the conversation valid for both providers while
                # preserving the full tool call structure in history.
                self._synthesize_cancelled_results("Cancelled by user.")
            # Drain any queued user messages so they appear in the
            # conversation and are visible on the next send().
            self._flush_queued_messages()
            # No need to clear _cancel_event — it's replaced per-generation
            # in send(), so this generation's event is simply discarded.
            self.ui.on_info("[Generation cancelled]")
            self._emit_state("idle")
            # Do NOT re-raise — return normally so server worker thread
            # completes cleanly.
        except KeyboardInterrupt:
            self._synthesize_cancelled_results("Interrupted by user.")
            self._flush_queued_messages()
            self._emit_state("error")
            raise
        except Exception:
            self._flush_queued_messages()
            self._emit_state("error")
            raise

    def _synthesize_cancelled_results(self, reason: str) -> None:
        """Synthesize tool_result messages for orphaned tool_calls after cancel.

        Finds the last assistant message with tool_calls, collects the IDs of
        tool_calls that already have matching tool results, and synthesizes
        cancelled results for any that don't.  This keeps the conversation
        valid (both providers require matching tool_results) while preserving
        the full tool call structure so the model knows what was attempted.
        """
        # Find the last assistant message with tool_calls
        assistant_idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                assistant_idx = i
                break
        if assistant_idx is None:
            return

        # Collect tool_call IDs that already have results
        answered_ids: set[str] = set()
        for msg in self.messages[assistant_idx + 1 :]:
            if msg.get("role") == "tool":
                answered_ids.add(msg.get("tool_call_id", ""))

        # Synthesize results for unanswered tool_calls
        for tc in self.messages[assistant_idx].get("tool_calls", []):
            tc_id = tc.get("id", "")
            func_name = tc.get("function", {}).get("name", "")
            if tc_id and tc_id not in answered_ids:
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": reason,
                        "is_error": True,
                    }
                )
                self._msg_tokens.append(1)
                save_message(self._ws_id, "tool", reason, func_name, tool_call_id=tc_id)

    # -- Rewind / retry -------------------------------------------------------

    def _find_turn_boundaries(self) -> list[int]:
        """Return indices of user messages in self.messages (turn start positions)."""
        return [i for i, m in enumerate(self.messages) if m["role"] == "user"]

    def rewind(self, n: int) -> int:
        """Drop the last *n* complete turns from the conversation.

        A turn = user message + all assistant/tool messages until the next
        user message.  Returns the number of messages removed.  Updates
        both in-memory state and the persistent database.
        """
        if n < 1:
            return 0
        boundaries = self._find_turn_boundaries()
        if not boundaries:
            return 0
        n = min(n, len(boundaries))
        cut_index = boundaries[-n]
        removed_count = len(self.messages) - cut_index
        del self.messages[cut_index:]
        del self._msg_tokens[cut_index:]
        delete_messages_after(self._ws_id, len(self.messages))
        return removed_count

    def retry(self) -> str | None:
        """Drop the last assistant response and return the user message to re-send.

        The caller is responsible for calling ``send()`` with the returned
        message.  Returns ``None`` if there is nothing to retry.
        """
        boundaries = self._find_turn_boundaries()
        if not boundaries:
            return None
        last_user_idx = boundaries[-1]
        content = self.messages[last_user_idx].get("content")
        # Multipart messages (vision/images) have list-type content;
        # retry only supports plain text.
        if not isinstance(content, str) or not content:
            return None
        # Drop everything from (and including) the user message onward;
        # send() will re-append the user message.
        del self.messages[last_user_idx:]
        del self._msg_tokens[last_user_idx:]
        delete_messages_after(self._ws_id, len(self.messages))
        return content

    @staticmethod
    def _strip_reasoning(text: str) -> str:
        """Remove <think>/<reasoning> tags and their content."""
        for open_t, close_t in [
            ("<think>", "</think>"),
            ("<reasoning>", "</reasoning>"),
        ]:
            while open_t in text:
                start = text.find(open_t)
                end = text.find(close_t, start)
                text = text[:start] + text[end + len(close_t) :] if end != -1 else text[:start]
        return text.strip()

    # Tags that delimit reasoning blocks in content stream.
    # Checked in order; first match wins.
    _THINK_OPEN_TAGS = ("<think>", "<reasoning>")
    _THINK_CLOSE_TAGS = ("</think>", "</reasoning>")
    _MAX_TAG_LEN = max(len(t) for t in _THINK_OPEN_TAGS + _THINK_CLOSE_TAGS)

    def _stream_response(
        self, stream: Iterator[StreamChunk], my_generation: int = 0
    ) -> dict[str, Any]:
        """Stream response, dispatching tokens to the UI as they arrive.

        Handles two reasoning delivery mechanisms:
        1. The `reasoning_delta` field (e.g. vLLM with --reasoning-parser)
        2. <think>...</think> tags in regular content (common default)

        Calls self.ui.on_thinking_stop() on the first received delta.

        Returns the complete assistant message as a dict suitable for
        appending to self.messages.
        """
        # Reset so this API call captures fresh usage — prevents stale
        # completion_tokens from a prior tool-chain iteration leaking
        # through the max() accumulator.
        self._last_usage = None

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        provider_blocks: list[dict[str, Any]] = []
        first_token = True
        in_think = False  # inside a <think>...</think> block
        path1_reasoning = False  # last reasoning came via reasoning_delta field
        pending = ""  # buffer for partial tag detection

        def _flush_text(text: str, is_reasoning: bool) -> None:
            """Dispatch text to the appropriate UI callback."""
            if not text:
                return
            if is_reasoning:
                reasoning_parts.append(text)
                if self.show_reasoning:
                    self.ui.on_reasoning_token(text)
            else:
                content_parts.append(text)
                self.ui.on_content_token(text)

        def _drain_pending() -> None:
            """Process the pending buffer, flushing content and detecting tags."""
            nonlocal pending, in_think

            while pending:
                if in_think:
                    # Look for any close tag
                    best_idx, best_tag = None, None
                    for tag in self._THINK_CLOSE_TAGS:
                        idx = pending.find(tag)
                        if idx != -1 and (best_idx is None or idx < best_idx):
                            best_idx, best_tag = idx, tag

                    if best_idx is not None:
                        assert best_tag is not None
                        _flush_text(pending[:best_idx], True)
                        pending = pending[best_idx + len(best_tag) :]
                        in_think = False
                        continue

                    # No close tag found — check if tail could be a partial tag
                    safe = len(pending) - self._MAX_TAG_LEN
                    if safe > 0:
                        _flush_text(pending[:safe], True)
                        pending = pending[safe:]
                    break
                else:
                    # Look for any open tag
                    best_idx, best_tag = None, None
                    for tag in self._THINK_OPEN_TAGS:
                        idx = pending.find(tag)
                        if idx != -1 and (best_idx is None or idx < best_idx):
                            best_idx, best_tag = idx, tag

                    if best_idx is not None:
                        assert best_tag is not None
                        _flush_text(pending[:best_idx], False)
                        pending = pending[best_idx + len(best_tag) :]
                        in_think = True
                        continue

                    # No open tag found — flush all but potential partial tag
                    safe = len(pending) - self._MAX_TAG_LEN
                    if safe > 0:
                        _flush_text(pending[:safe], False)
                        pending = pending[safe:]
                    break

        def _stop_spinner_once() -> None:
            """Stop the spinner on first real content. Call is idempotent."""
            nonlocal first_token
            if first_token:
                self.ui.on_thinking_stop()
                first_token = False

        finish_reason = None
        try:
            for chunk in stream:
                # _cancel_stream is set eagerly by _CancelRef.append() when the
                # provider creates the SDK stream handle (before the first chunk
                # is returned).  This fallback handles providers that use a
                # plain list for cancel_ref (e.g. some test fakes).
                if self._cancel_ref and self._cancel_stream is None:
                    self._cancel_stream = self._cancel_ref[0]
                self._check_cancelled(my_generation)
                # Track finish_reason (e.g. "stop", "length", "tool_calls")
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason

                # Accumulate usage (Anthropic sends prompt tokens in message_start
                # and completion tokens in message_delta as separate events)
                if chunk.usage:
                    if self._last_usage is None:
                        self._last_usage = {
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                            "total_tokens": chunk.usage.total_tokens,
                            "cache_creation_tokens": chunk.usage.cache_creation_tokens,
                            "cache_read_tokens": chunk.usage.cache_read_tokens,
                        }
                    else:
                        self._last_usage["prompt_tokens"] = max(
                            self._last_usage["prompt_tokens"], chunk.usage.prompt_tokens
                        )
                        self._last_usage["completion_tokens"] = max(
                            self._last_usage["completion_tokens"], chunk.usage.completion_tokens
                        )
                        self._last_usage["total_tokens"] = (
                            self._last_usage["prompt_tokens"]
                            + self._last_usage["completion_tokens"]
                        )
                        self._last_usage["cache_creation_tokens"] = max(
                            self._last_usage.get("cache_creation_tokens", 0),
                            chunk.usage.cache_creation_tokens,
                        )
                        self._last_usage["cache_read_tokens"] = max(
                            self._last_usage.get("cache_read_tokens", 0),
                            chunk.usage.cache_read_tokens,
                        )

                if self.debug:
                    parts = []
                    if chunk.content_delta:
                        parts.append(f"content={chunk.content_delta!r}")
                    if chunk.reasoning_delta:
                        parts.append(f"reasoning={chunk.reasoning_delta!r}")
                    if chunk.tool_call_deltas:
                        parts.append("tool_calls=...")
                    if parts:
                        self.ui.on_info(f"{GRAY}[delta: {', '.join(parts)}]{RESET}")

                # Path 1: reasoning field (provider-normalized reasoning_delta)
                if chunk.reasoning_delta:
                    _stop_spinner_once()
                    reasoning_parts.append(chunk.reasoning_delta)
                    in_think = True
                    path1_reasoning = True
                    if self.show_reasoning:
                        self.ui.on_reasoning_token(chunk.reasoning_delta)

                # Path 2: regular content (may contain <think> tags)
                if chunk.content_delta:
                    _stop_spinner_once()
                    # Close reasoning if transitioning from Path 1 reasoning
                    if path1_reasoning:
                        path1_reasoning = False
                        in_think = False
                    pending += chunk.content_delta
                    _drain_pending()

                # Handle tool call deltas
                if chunk.tool_call_deltas:
                    _stop_spinner_once()
                    # Flush any buffered content — model has moved to tool calls,
                    # so pending text cannot be a partial <think> tag.
                    if pending:
                        _flush_text(pending, in_think)
                        pending = ""
                    # Close reasoning if transitioning from reasoning
                    if in_think:
                        in_think = False
                    for tcd in chunk.tool_call_deltas:
                        idx = tcd.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        tc = tool_calls_acc[idx]
                        if tcd.id:
                            tc["id"] = tcd.id
                        if tcd.name:
                            tc["function"]["name"] = tcd.name
                        if tcd.arguments_delta:
                            tc["function"]["arguments"] += tcd.arguments_delta

                # Informational messages (e.g. server-side web search status)
                if chunk.info_delta:
                    _stop_spinner_once()
                    self.ui.on_info(f"{GRAY}{chunk.info_delta}{RESET}")

                # Raw provider content blocks (for multi-turn preservation)
                if chunk.provider_blocks:
                    provider_blocks = chunk.provider_blocks
        except GenerationCancelled:
            # Flush whatever was buffered and build a partial message
            if pending:
                _flush_text(pending, in_think)
            self.ui.on_stream_end()
            partial: dict[str, Any] = {"role": "assistant"}
            partial_content = "".join(content_parts)
            partial["content"] = partial_content or ""
            # Deliberately omit tool_calls — they are incomplete
            if provider_blocks:
                partial["_provider_content"] = provider_blocks
            self._cancelled_partial_msg = partial
            raise
        except Exception:
            # cancel() closed the underlying SDK stream, aborting the HTTP
            # connection.  The blocked next() call on the iterator raises a
            # transport-level error (httpx, httpcore, etc.).  Convert to
            # GenerationCancelled if a cancel was requested.
            if self._cancel_event.is_set():
                if pending:
                    _flush_text(pending, in_think)
                self.ui.on_stream_end()
                partial = {"role": "assistant"}
                partial["content"] = "".join(content_parts) or ""
                if provider_blocks:
                    partial["_provider_content"] = provider_blocks
                self._cancelled_partial_msg = partial
                raise GenerationCancelled() from None
            raise

        # Flush any remaining buffered text
        if pending:
            _flush_text(pending, in_think)

        # Warn on non-standard finish reasons
        if finish_reason == "length":
            self.ui.on_error(
                f"Warning: response truncated (hit {self.max_tokens} token limit). "
                f"Use --max-tokens to increase, or /compact to free context."
            )
            log.warning(
                "stream.truncated",
                finish_reason=finish_reason,
                max_tokens=self.max_tokens,
                had_tool_calls=bool(tool_calls_acc),
            )
            # Drop partial tool calls — they'll have malformed JSON
            if tool_calls_acc:
                dropped = [tool_calls_acc[i]["function"]["name"] for i in sorted(tool_calls_acc)]
                self.ui.on_error("Discarding partial tool calls from truncated response.")
                log.warning(
                    "stream.tool_calls_discarded",
                    reason="truncated",
                    dropped_tools=dropped,
                    count=len(dropped),
                )
                tool_calls_acc.clear()
        elif finish_reason == "content_filter":
            self.ui.on_error("Warning: response blocked by content filter.")

        # Log stream completion for diagnostics
        log.debug(
            "stream.finished",
            finish_reason=finish_reason,
            has_content=bool(content_parts),
            tool_call_count=len(tool_calls_acc),
            content_length=sum(len(p) for p in content_parts),
        )

        # Signal end of stream to the UI
        self.ui.on_stream_end()

        # Build assistant message dict
        msg: dict[str, Any] = {"role": "assistant"}

        content = "".join(content_parts)
        msg["content"] = content or ""

        if tool_calls_acc:
            self._ensure_tool_call_ids(tool_calls_acc)
            msg["tool_calls"] = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
            log.info(
                "stream.tool_calls",
                count=len(tool_calls_acc),
                tools=[tool_calls_acc[i]["function"]["name"] for i in sorted(tool_calls_acc)],
            )

        # Store raw provider content blocks for multi-turn preservation
        # (e.g. Anthropic web_search_tool_result with encrypted_content)
        if provider_blocks:
            msg["_provider_content"] = provider_blocks

        return msg

    _print_lock = threading.Lock()

    # -- Debug ----------------------------------------------------------------

    def _debug_print_request(self, msgs: list[dict[str, Any]]) -> None:
        """Print the full API request payload when debug mode is on."""
        lines = []
        lines.append(f"\n{GRAY}{'=' * 60}{RESET}")
        lines.append(
            f"{GRAY}[request] model={self.model}  "
            f"max_tokens={self.max_tokens}  temp={self.temperature}  "
            f"reasoning={self.reasoning_effort}  "
            f"tools={0 if self.creative_mode else len(self._get_active_tools() or [])}"
            f"{' (search)' if self._tool_search else ''}{RESET}"
        )
        lines.append(f"{GRAY}[request] {len(msgs)} messages:{RESET}")
        for i, m in enumerate(msgs):
            role = m["role"]
            content = m.get("content") or ""
            tool_calls = m.get("tool_calls")
            tc_id = m.get("tool_call_id")

            # Flatten list content (image tool results) for display
            if isinstance(content, list):
                parts = []
                for p in content:
                    if p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif p.get("type") == "image_url":
                        parts.append("[image]")
                content = " ".join(parts)

            # Truncate long content for readability
            if len(content) > 300:
                display = content[:200] + f"...({len(content)} chars)..." + content[-50:]
            else:
                display = content
            # Escape newlines for compact display
            display = display.replace("\n", "\\n")

            header = f"  [{i}] {role}"
            if tc_id:
                header += f" (tool_call_id={tc_id})"

            lines.append(f"{GRAY}{header}: {display}{RESET}")

            if tool_calls:
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", "?")
                    args = tc.get("function", {}).get("arguments", "")
                    if len(args) > 200:
                        args = args[:150] + f"...({len(args)} chars)"
                    lines.append(f"{GRAY}    -> {name}({args}){RESET}")

        lines.append(f"{GRAY}{'=' * 60}{RESET}")
        self.ui.on_info("\n".join(lines))

    # -- Token tracking & status ----------------------------------------------

    # Fixed token count per image (provider-agnostic average).
    _IMAGE_TOKENS = 1000

    @staticmethod
    def _msg_text_chars(msg: dict[str, Any]) -> tuple[int, int, int]:
        """Return ``(text_chars, image_count, doc_chars)`` for a message.

        Counts textual content + structural overhead (role, tool_call
        IDs, tool call names/arguments).  Images are counted separately
        so the calibration can subtract their fixed token cost from
        prompt_tokens.  Document-part content (``data`` + ``name`` +
        ``media_type``) is counted in a third bucket so it contributes
        to the token budget without polluting the ``chars_per_token``
        calibration — provider-native document blocks (Anthropic) and
        inlined text (OpenAI/Google) tokenize differently, so it's
        safer to exclude them from the text calibration.
        """
        content = msg.get("content")
        n = 0
        images = 0
        doc_chars = 0
        if isinstance(content, list):
            for p in content:
                ptype = p.get("type")
                if ptype == "text":
                    n += len(p.get("text", ""))
                elif ptype == "image_url":
                    images += 1
                elif ptype == "document":
                    d = p.get("document", {})
                    doc_chars += len(d.get("data", ""))
                    doc_chars += len(d.get("name", ""))
                    doc_chars += len(d.get("media_type", ""))
        else:
            n += len(content or "")
        for tc in msg.get("tool_calls", []):
            n += len(tc.get("id", ""))
            n += len(tc.get("function", {}).get("name", ""))
            n += len(tc.get("function", {}).get("arguments", ""))
        # Structural overhead: role, tool_call_id
        n += len(msg.get("role", ""))
        n += len(msg.get("tool_call_id", ""))
        return n, images, doc_chars

    def _msg_char_count(self, msg: dict[str, Any]) -> int:
        """Count characters in a message, including structural overhead.

        Includes role markers, tool_call IDs, image placeholders, and
        document-part characters so that the budget estimate reflects
        the full payload the provider sees.
        """
        text_chars, images, doc_chars = self._msg_text_chars(msg)
        return text_chars + doc_chars + int(images * self._IMAGE_TOKENS * self._chars_per_token)

    def _update_token_table(self, assistant_msg: dict[str, Any]) -> None:
        """Update per-message token estimates using API usage data."""
        if not self._last_usage:
            return

        prompt_tok = self._last_usage["prompt_tokens"]
        compl_tok = self._last_usage["completion_tokens"]

        # Calibrate chars_per_token ratio from actual usage.
        # Images get a fixed token budget (subtracted).  Documents
        # tokenize non-linearly depending on provider — excluded from
        # calibration so they don't skew the text ratio.
        all_msgs = self._full_messages()  # system + self.messages (before append)
        active_tools = self._get_active_tools() or []
        tool_def_chars = sum(len(json.dumps(t)) for t in active_tools)
        text_chars = 0
        image_count = 0
        for m in all_msgs:
            tc, ic, _doc = self._msg_text_chars(m)
            text_chars += tc
            image_count += ic
        text_chars += tool_def_chars
        image_tokens = image_count * self._IMAGE_TOKENS
        text_prompt_tok = prompt_tok - image_tokens
        if text_prompt_tok <= 0:
            log.debug(
                "Image token estimate (%d) >= prompt_tokens (%d), skipping calibration",
                image_tokens,
                prompt_tok,
            )
        elif text_chars > 0:
            self._chars_per_token = text_chars / text_prompt_tok

        # Compute system_tokens (stable after first call)
        sys_chars = sum(self._msg_char_count(m) for m in self.system_messages)
        self._system_tokens = max(1, int(sys_chars / self._chars_per_token))

        # Re-estimate all message token counts with calibrated ratio
        self._msg_tokens = [
            max(1, int(self._msg_char_count(m) / self._chars_per_token)) for m in self.messages
        ]

        # Stash completion_tokens for the assistant message about to be appended
        self._assistant_pending_tokens = compl_tok

        # Record how many messages were in context at calibration time so
        # _remaining_token_budget() can estimate only the delta.
        self._calibrated_msg_count = len(self.messages)

        # Token budget tracking
        if self._token_budget > 0:
            total = prompt_tok + compl_tok
            if not self._budget_warned and total >= self._token_budget * 0.8:
                self._budget_warned = True
                self.ui.on_info(f"Token budget 80% consumed ({total:,}/{self._token_budget:,})")
            if total >= self._token_budget:
                self._budget_exhausted = True

    def _print_status_line(self) -> None:
        """Emit status info via the UI."""
        if not self._last_usage:
            return
        usage: dict[str, Any] = {**self._last_usage, "model": self.model}
        self.ui.on_status(usage, self.context_window, self.reasoning_effort)

    # -- Conversation compaction ------------------------------------------------

    def _format_messages_for_summary(self, messages: list[dict[str, Any]]) -> str:
        """Format messages into a readable string for the summarization prompt."""
        # Build tool_call_id → tool_name lookup for labeling tool results
        tc_names: dict[str, str] = {}
        for m in messages:
            for tc in m.get("tool_calls", []):
                tc_id = tc.get("id", "")
                tc_name = tc.get("function", {}).get("name", "unknown")
                if tc_id:
                    tc_names[tc_id] = tc_name

        parts = []
        for m in messages:
            role = m["role"].upper()
            content = m.get("content") or ""

            # Flatten list content (image tool results) to text for summary
            if isinstance(content, list):
                text_parts = []
                for p in content:
                    if p.get("type") == "text":
                        text_parts.append(p["text"])
                    elif p.get("type") == "image_url":
                        text_parts.append("[image]")
                content = " ".join(text_parts)

            if m.get("tool_calls"):
                calls = []
                for tc in m["tool_calls"]:
                    name = tc.get("function", {}).get("name", "?")
                    args = tc.get("function", {}).get("arguments", "")
                    calls.append(f"{name}({args})")
                content += "\n[Called: " + ", ".join(calls) + "]"

            # Label tool results with the tool name
            if role == "TOOL":
                tc_id = m.get("tool_call_id", "")
                name = tc_names.get(tc_id, "tool")
                role = f"TOOL[{name}]"

            if content:
                if len(content) > 2000:
                    content = content[:1000] + "\n...[truncated]...\n" + content[-500:]
                parts.append(f"{role}: {content}")
        return "\n\n".join(parts)

    def _compact_messages(self, auto: bool = False) -> None:
        """Compact conversation history by summarizing all messages.

        Summarizes the entire conversation via a separate model call,
        budget-fitted to 80% of the context window.

        When auto=True (triggered by context limit), appends a continuation
        hint with the last user message so the model can resume seamlessly.
        """
        if len(self.messages) < 2:
            self.ui.on_info("Not enough messages to compact.")
            return

        # Find the last user message for the continuation hint
        last_user_content = None
        if auto:
            for m in reversed(self.messages):
                if m["role"] == "user":
                    last_user_content = m.get("content") or ""
                    break

        to_summarize = self.messages

        # Budget: fit as many messages as possible into summary request
        summary_max_tokens = self.compact_max_tokens
        prompt_budget = (
            int(self.context_window * self.auto_compact_pct)
            - summary_max_tokens
            - self._system_tokens
        )
        selected = []
        running = 0
        for i, msg in enumerate(to_summarize):
            msg_tok = (
                self._msg_tokens[i]
                if i < len(self._msg_tokens)
                else max(1, int(self._msg_char_count(msg) / self._chars_per_token))
            )
            if running + msg_tok > prompt_budget:
                break
            selected.append(msg)
            running += msg_tok

        if not selected:
            self.ui.on_info("Messages too large to fit in summary context.")
            return

        # Build summary prompt and call model
        formatted = self._format_messages_for_summary(selected)
        summary_msgs = [
            {
                "role": "system",
                "content": (
                    "# Conversation Compactor\n\n"
                    "Your output REPLACES the conversation history — the assistant "
                    "will continue from your summary with no access to the original messages.\n\n"
                    "1. **Output format** — use these exact sections, omit any that are empty:\n"
                    "   - **## Decisions**: Choices made (architecture, libraries, approaches).\n"
                    "   - **## Files**: Files read, created, or modified, with brief notes.\n"
                    "   - **## Key code**: Exact function names, class names, variable names, "
                    "and short code snippets the assistant will need. "
                    "Preserve identifiers verbatim — do NOT paraphrase.\n"
                    "   - **## Tool results**: Important tool outputs (errors, search matches, "
                    "file contents) that inform ongoing work.\n"
                    "   - **## Open tasks**: What the user asked for that is not yet done, "
                    "with enough context to continue.\n"
                    "   - **## User preferences**: Workflow preferences, constraints, or "
                    "instructions the user stated.\n"
                    "   - **## Memories to save**: Corrections, preferences, or learnings "
                    "the user expressed that should be persisted across sessions. "
                    "Format each as: `name: description — content`. "
                    "Only include items the user explicitly stated, not inferences.\n\n"
                    "2. **Density rules:**\n"
                    "   - Every token should carry information.\n"
                    "   - Preserve exact paths, identifiers, and numbers — never paraphrase these.\n"
                    "   - Omit pleasantries, acknowledgments, and reasoning that led to dead ends.\n"
                    "   - If a tool call's result was an error that was later resolved, "
                    "keep only the resolution.\n\n"
                    "3. **Common mistakes to avoid:**\n"
                    "   - Paraphrasing file paths, function names, or variable names\n"
                    "   - Including dead-end explorations or superseded decisions\n"
                    "   - Omitting the open tasks section when work remains\n"
                    "   - Being verbose — this is a summary, not a transcript"
                ),
            },
            {
                "role": "user",
                "content": ("Compact the following conversation:\n\n" + formatted),
            },
        ]

        self.ui.on_thinking_start()
        try:
            result: CompletionResult | None = None
            for attempt in range(self._MAX_RETRIES + 1):
                try:
                    result = self._utility_completion(
                        summary_msgs,
                        max_tokens=summary_max_tokens,
                    )
                    break
                except Exception as e:
                    ename = type(e).__name__
                    if (
                        ename not in self._provider.retryable_error_names
                        or attempt == self._MAX_RETRIES
                    ):
                        raise
                    delay = self._RETRY_BASE_DELAY * (2**attempt)
                    self.ui.on_info(f"[Compact retrying in {delay:.0f}s: {ename}]")
                    time.sleep(delay)
            assert result is not None
            summary = result.content or ""
            # Strip any <think>/<reasoning> tags the summarizer may emit
            summary = self._strip_reasoning(summary)
            if result.finish_reason == "length":
                self.ui.on_info("[Warning: compaction summary was truncated]")
        except Exception as e:
            self.ui.on_error(f"Compaction failed: {e}")
            return
        finally:
            self.ui.on_thinking_stop()

        # Append continuation hint for auto-compact
        if last_user_content:
            # Truncate very long user messages
            if len(last_user_content) > 500:
                last_user_content = last_user_content[:400] + "..."
            summary += (
                f"\n\n## Continue\n"
                f"The user's last message was: {last_user_content}\n"
                f"Continue assisting from where we left off."
            )

        # Replace messages
        before_tokens = self._system_tokens + sum(self._msg_tokens)
        summary_user = {"role": "user", "content": "[Conversation summary]"}
        summary_asst = {"role": "assistant", "content": summary}
        self.messages = [summary_user, summary_asst]
        # File contents are gone after compaction — force re-read before edit_file
        self._read_files.clear()
        self._recent_tool_sigs.clear()

        # Rebuild token table
        su_tok = max(1, int(self._msg_char_count(summary_user) / self._chars_per_token))
        sa_tok = max(1, int(self._msg_char_count(summary_asst) / self._chars_per_token))
        self._msg_tokens = [su_tok, sa_tok]
        self._calibrated_msg_count = len(self.messages)  # anchored to compacted state
        after_tokens = self._system_tokens + sum(self._msg_tokens)

        # Update usage estimate so the status bar reflects post-compaction state
        if self._last_usage:
            self._last_usage = {
                **self._last_usage,
                "prompt_tokens": after_tokens,
                "total_tokens": after_tokens,
            }

        self.ui.on_info(f"[compacted: ~{before_tokens:,} -> ~{after_tokens:,} tokens]")
        separator = "\u2500" * 60
        lines = [separator]
        for line in summary.splitlines():
            lines.append(f"  {line}")
        lines.append(separator)
        self.ui.on_info("\n".join(lines))

    # -- Intent validation --------------------------------------------------------

    def _ensure_judge(self) -> IntentJudge | None:
        """Lazily initialize the intent judge if configured.

        Re-checks the live ``enabled`` flag every call so disabling the
        judge via admin settings takes immediate effect on existing sessions.
        """
        if not self._judge_cfg or not self._judge_cfg.enabled:
            return None
        if self._judge is not None:
            return self._judge
        # Frozen config required for IntentJudge init (LLM client fields).
        # _judge_cfg already returns None when _judge_config is None, but
        # this guard makes the dependency explicit for type narrowing.
        if self._judge_config is None:
            return None
        try:
            from turnstone.core.judge import IntentJudge

            caps = self._get_capabilities()
            self._judge = IntentJudge(
                config=self._judge_config,
                session_provider=self._provider,
                session_client=self.client,
                session_model=self.model,
                context_window=caps.context_window,
                rule_registry=self._rule_registry,
                model_registry=self._registry,
            )
        except Exception:
            log.warning("judge.init_failed", exc_info=True)
        return self._judge

    def _evaluate_intent(
        self,
        items: list[dict[str, Any]],
    ) -> threading.Event | None:
        """Run intent validation on pending approval items.

        Attaches heuristic verdicts to items immediately.  Spawns the
        async LLM judge that delivers final verdicts via UI callback.

        Returns a cancel event that, when set, tells the daemon judge
        thread to abandon remaining work.  Callers should set this
        after the user has made an approval decision.
        """
        judge = self._ensure_judge()
        if not judge:
            return None

        # Only evaluate items that need approval and aren't errors
        pending = [it for it in items if it.get("needs_approval") and not it.get("error")]
        if not pending:
            return None

        # Build func_args from tool-specific item keys so the heuristic
        # engine can pattern-match on argument content.
        for it in pending:
            name = it.get("func_name", "")
            if name == "bash":
                it["func_args"] = {"command": it.get("command", "")}
            elif name in ("write_file", "edit_file", "read_file"):
                it["func_args"] = {"path": it.get("path", "")}
            elif name == "web_fetch":
                it["func_args"] = {"url": it.get("url", ""), "question": it.get("question", "")}
            elif name == "web_search":
                it["func_args"] = {"query": it.get("query", ""), "topic": it.get("topic", "")}
            elif name == "skill":
                it["func_args"] = {"action": it.get("action", ""), "name": it.get("name", "")}
            elif name == "watch":
                it["func_args"] = {
                    "action": it.get("action", ""),
                    "command": it.get("command", ""),
                    "name": it.get("watch_name", ""),
                }
            elif name == "notify":
                it["func_args"] = {"message": it.get("message", "")[:200]}
            elif name == "task_agent":
                it["func_args"] = {"prompt": it.get("prompt", "")[:200]}
            elif name == "plan_agent":
                it["func_args"] = {"goal": it.get("prompt", "")[:200]}
            elif it.get("mcp_args"):
                it["func_args"] = it["mcp_args"]

        def _on_verdict(verdict: object) -> None:
            """Callback from the daemon judge thread."""
            try:
                self.ui.on_intent_verdict(verdict.to_dict())  # type: ignore[attr-defined]
            except Exception:
                log.debug("judge.verdict_delivery_failed", exc_info=True)

        cancel_event = threading.Event()
        heuristic_verdicts = judge.evaluate(
            pending,
            list(self.messages),  # snapshot — daemon thread must not see mutations
            callback=_on_verdict,
            cancel_event=cancel_event,
        )

        # Attach heuristic verdicts to items for the approval UI
        for item, verdict in zip(pending, heuristic_verdicts, strict=True):
            item["_heuristic_verdict"] = verdict.to_dict()

        return cancel_event

    def _evaluate_output(
        self, call_id: str, output: str, func_name: str
    ) -> tuple[str, OutputAssessment | None]:
        """Run the output guard on tool result text.

        Returns ``(possibly_sanitized_output, assessment)``.  The assessment
        is ``None`` when risk_level is ``"none"``.  Surfaces warnings via
        ``ui.on_output_warning`` and logs at debug level.
        """
        from turnstone.core.output_guard import evaluate_output

        og_patterns = None
        rule_reg = self._rule_registry
        if rule_reg is not None:
            og_patterns = rule_reg.output_patterns
        assessment = evaluate_output(
            output, func_name=func_name, call_id=call_id, patterns=og_patterns
        )
        if assessment.risk_level == "none":
            return output, None

        log.debug(
            "output_guard.flagged",
            call_id=call_id,
            func_name=func_name,
            risk=assessment.risk_level,
            flags=assessment.flags,
        )
        try:
            d = assessment.to_dict()  # excludes sanitized by default
            d["func_name"] = func_name
            d["output_length"] = len(output)
            d["redacted"] = assessment.sanitized is not None
            self.ui.on_output_warning(call_id, d)
        except Exception:
            log.debug("output_guard.callback_failed", exc_info=True)

        if assessment.sanitized is not None and self._judge_cfg and self._judge_cfg.redact_secrets:
            return assessment.sanitized, assessment
        return output, assessment

    # -- User message queue -----------------------------------------------------

    def queue_message(
        self,
        text: str,
        attachment_ids: list[str] | tuple[str, ...] | None = None,
        queue_msg_id: str | None = None,
    ) -> tuple[str, str, str]:
        """Queue a user message for injection at the next tool-result seam.

        Thread-safe — called from the HTTP handler while the worker thread
        is executing.  Returns ``(cleaned_text, priority, msg_id)``.
        Raises ``queue.Full`` if the queue is saturated.

        ``attachment_ids`` (ordered) are resolved and consumed at dequeue
        time so queued multimodal turns don't silently lose their files.
        ``queue_msg_id`` lets the caller supply the id (so it matches the
        attachment-reservation token already taken server-side) — when
        omitted, an id is generated.
        """
        from turnstone.core.tool_advisory import parse_priority

        cleaned, priority = parse_priority(text)
        # Cap individual message length to prevent context bloat
        if len(cleaned) > 2000:
            cleaned = cleaned[:2000] + "..."
        # Full UUID hex (128 bits) rather than a truncated prefix — this
        # id doubles as a cross-table reservation token on
        # workstream_attachments, and a 48-bit truncation narrows the
        # birthday bound unnecessarily.
        msg_id = queue_msg_id or uuid.uuid4().hex
        att_ids = tuple(attachment_ids or ())
        with self._queued_lock:
            if len(self._queued_messages) >= self._QUEUE_MAX:
                raise queue.Full()
            self._queued_messages[msg_id] = (cleaned, priority, att_ids)
        return cleaned, priority, msg_id

    def dequeue_message(self, msg_id: str) -> bool:
        """Remove a queued message by ID.  Returns True if removed.

        Releases any attachment reservation held by the queued message
        so the user can re-use or delete those files.
        """
        with self._queued_lock:
            popped = self._queued_messages.pop(msg_id, None)
        if popped is None:
            return False
        # popped == (cleaned, priority, attachment_ids_tuple)
        if popped[2]:
            unreserve_attachments(msg_id, self._ws_id, self._user_id)
        return True

    def _resolve_attachment_ids(
        self,
        attachment_ids: tuple[str, ...] | list[str],
        allow_reserved_for: str | None = None,
    ) -> list[Attachment]:
        """Fetch+scope-check attachment ids, preserving request order.

        Silently drops ids that don't belong to this session's ws+user,
        are already consumed, or are reserved for a different queued
        message.  When ``allow_reserved_for`` is set, attachments whose
        ``reserved_for_msg_id`` matches are accepted (dequeue path
        passes the originating queue msg id so its own reservation
        releases cleanly).
        """
        ids = [str(x) for x in attachment_ids if x]
        if not ids:
            return []
        rows = get_attachments(ids)
        by_id = {str(r["attachment_id"]): r for r in rows}
        resolved: list[Attachment] = []
        for aid in ids:
            r = by_id.get(aid)
            if (
                not r
                or r.get("ws_id") != self._ws_id
                or r.get("user_id") != self._user_id
                or r.get("message_id") is not None
            ):
                continue
            reserved = r.get("reserved_for_msg_id")
            if reserved and reserved != allow_reserved_for:
                continue
            content = r.get("content")
            if not isinstance(content, bytes):
                continue
            resolved.append(
                Attachment(
                    attachment_id=str(r["attachment_id"]),
                    filename=str(r.get("filename") or ""),
                    mime_type=str(r.get("mime_type") or "application/octet-stream"),
                    kind=str(r.get("kind") or ""),
                    content=content,
                )
            )
        return resolved

    def _flush_queued_messages(self) -> None:
        """Drain queued messages.

        Items without attachments are combined into a single user turn
        to avoid back-to-back user messages that some models handle
        poorly.  Items with attachments flush as separate multipart user
        turns (combining text+files across distinct queued sends would
        misrepresent ordering).
        """
        from turnstone.core.tool_advisory import PRIORITY_IMPORTANT

        with self._queued_lock:
            # .items() so we keep the queue msg id for reservation lookup
            items = list(self._queued_messages.items())
            self._queued_messages.clear()
        if not items:
            return

        # Collapse contiguous attachment-free items into one combined text
        # to preserve the prior behaviour; flush attachment-bearing items
        # inline as their own multipart turns.
        text_run: list[tuple[str, str]] = []

        def _flush_text_run() -> None:
            if not text_run:
                return
            parts = [
                f"[IMPORTANT] {msg}" if pri == PRIORITY_IMPORTANT else msg for msg, pri in text_run
            ]
            combined = "\n\n".join(parts)
            self._append_user_turn(combined, ())
            text_run.clear()

        for queue_msg_id, (cleaned, priority, att_ids) in items:
            if att_ids:
                _flush_text_run()
                text = f"[IMPORTANT] {cleaned}" if priority == PRIORITY_IMPORTANT else cleaned
                resolved = self._resolve_attachment_ids(att_ids, allow_reserved_for=queue_msg_id)
                self._append_user_turn(text, resolved, send_id=queue_msg_id)
            else:
                text_run.append((cleaned, priority))
        _flush_text_run()

    def _collect_advisories(
        self,
        assessment: OutputAssessment | None,
        func_name: str,
        is_last_in_batch: bool,
    ) -> list[ToolAdvisory]:
        """Gather advisories to attach to a tool result message.

        Returns an empty list when no advisories apply (common case).
        Guard advisories attach per-result; user messages drain on the
        last result in the batch only.
        """
        from turnstone.core.tool_advisory import GuardAdvisory, UserInterjection

        caps = self._get_capabilities()

        # When the model doesn't support advisory tags, still drain queued
        # messages so they aren't silently orphaned — flush them as regular
        # user messages instead.
        if not caps.supports_tool_advisories:
            if is_last_in_batch:
                self._flush_queued_messages()
            return []

        advisories: list[ToolAdvisory] = []

        # Output guard advisory
        if assessment is not None:
            advisories.append(GuardAdvisory(assessment=assessment, func_name=func_name))

        # Drain queued user messages on the last result in the batch.
        # Attachment-bearing items fall back to a full multipart user
        # turn (advisories are text-only and can't carry image blocks).
        if is_last_in_batch:
            with self._queued_lock:
                items = list(self._queued_messages.items())
                self._queued_messages.clear()
            attachment_items: list[tuple[str, str, str, tuple[str, ...]]] = []
            for queue_msg_id, (msg, priority, att_ids) in items:
                if att_ids:
                    attachment_items.append((queue_msg_id, msg, priority, att_ids))
                else:
                    advisories.append(UserInterjection(message=msg, priority=priority))
            if attachment_items:
                from turnstone.core.tool_advisory import PRIORITY_IMPORTANT

                for queue_msg_id, msg, priority, att_ids in attachment_items:
                    text = f"[IMPORTANT] {msg}" if priority == PRIORITY_IMPORTANT else msg
                    resolved = self._resolve_attachment_ids(
                        att_ids, allow_reserved_for=queue_msg_id
                    )
                    self._append_user_turn(text, resolved, send_id=queue_msg_id)

        return advisories

    # -- Two-phase tool execution -----------------------------------------------
    #
    # Phase 1 — prepare: parse args, validate, build preview text (serial)
    # Phase 2 — approve: display all previews, single prompt (serial)
    # Phase 3 — execute: run approved tools (parallel if multiple)

    def _execute_tools(
        self, tool_calls: list[dict[str, Any]]
    ) -> tuple[list[tuple[str, str | list[dict[str, Any]]]], str | None]:
        """Execute tool calls with batch preview and approval.

        Returns (results, user_feedback) where user_feedback is an optional
        message the user typed alongside their approval (e.g. "y, use full path").
        """
        # Phase 1: prepare all tool calls
        items = [self._prepare_tool(tc) for tc in tool_calls]

        # Intent validation (advisory, non-blocking).
        # Cancel any prior judge thread before spawning a new one.
        if self._judge_cancel_event is not None:
            self._judge_cancel_event.set()
        judge_cancel = self._evaluate_intent(items)
        self._judge_cancel_event = judge_cancel  # track for close()

        # Phase 2: approve via UI
        self._emit_state("attention")
        try:
            approved, user_feedback = self.ui.approve_tools(items)
        finally:
            if judge_cancel:
                judge_cancel.set()  # user decided (or disconnected) — stop judge
        self._emit_state("running")
        if not approved:
            # Mark all pending items as denied
            for item in items:
                if item.get("needs_approval") and not item.get("error"):
                    item["denied"] = True
                    item["denial_msg"] = (
                        f"Denied by user: {user_feedback}" if user_feedback else "Denied by user"
                    )
            user_feedback = None  # feedback is in the denial_msg
            if self._mem_cfg.nudges and should_nudge(
                "denial",
                self._metacog_state,
                message_count=len(self.messages),
                memory_count=self._visible_memory_count(),
                cooldown_secs=self._mem_cfg.nudge_cooldown,
            ):
                self._pending_nudge.append(("denial", format_nudge("denial")))
                self._init_system_messages()

        # Phase 3: execute (check cancellation before starting)
        self._check_cancelled()

        def run_one(
            item: dict[str, Any],
        ) -> tuple[str, str | list[dict[str, Any]]]:
            self._check_cancelled()
            if item.get("error"):
                self._report_tool_result(
                    item["call_id"],
                    item.get("func_name", "unknown"),
                    item["error"],
                    is_error=True,
                )
                return item["call_id"], item["error"]
            if item.get("denied"):
                return item["call_id"], item.get("denial_msg", "Denied by user")
            try:
                result: tuple[str, str | list[dict[str, Any]]] = item["execute"](item)
                return result
            except (KeyboardInterrupt, GenerationCancelled):
                raise
            except Exception as e:
                func = item.get("func_name", "unknown")
                msg = f"Error executing {func}: {e}"
                log.warning("tool_exec.failed", tool=func, error=str(e), exc_info=True)
                self._report_tool_result(item["call_id"], func, msg, is_error=True)
                return item["call_id"], msg

        if len(items) == 1:
            results = [run_one(items[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(run_one, items))

        # Post-plan gate: iterative review loop.  When the user gives
        # feedback the plan agent re-runs and the revised plan is shown
        # again, up to _MAX_PLAN_REFINEMENTS rounds.
        for i, item in enumerate(items):
            if item.get("func_name") != "plan_agent" or item.get("error") or item.get("denied"):
                continue

            cid, output = results[i]
            if not isinstance(output, str):
                raise TypeError(f"plan_agent must return str, got {type(output).__name__}")
            plan_path = f".plan-{self._ws_id}.md"

            if not self.auto_approve:
                original_goal = item.get("prompt", "")

                refinement_round = 0
                while True:
                    self._emit_state("attention")
                    resp = self.ui.on_plan_review(output)
                    self._emit_state("running")

                    if resp.lower() in ("n", "no", "reject"):
                        output += (
                            "\n\n---\nUser REJECTED this plan. Do not "
                            "proceed with implementation. Ask the user "
                            "what they want instead."
                        )
                        break
                    elif not resp:
                        break  # empty response = approve
                    elif refinement_round >= self._MAX_PLAN_REFINEMENTS:
                        self.ui.on_info("[plan] max refinement rounds reached")
                        break
                    else:
                        # Re-run plan agent with user feedback.
                        # Strip any internal warning prefix so the
                        # agent sees the raw plan content.
                        raw = output
                        _warn = "[Warning: plan may be incomplete or poorly structured]\n\n"
                        if raw.startswith(_warn):
                            raw = raw[len(_warn) :]
                        try:
                            output = self._refine_plan(
                                raw,
                                original_goal,
                                resp,
                            )
                            refinement_round += 1
                        except (KeyboardInterrupt, GenerationCancelled):
                            output += "\n\n---\n(plan refinement interrupted)"
                            break
                        except Exception as e:
                            self.ui.on_info(f"[plan refinement error] {e}")
                            output += f"\n\n---\nUser feedback: {resp}"
                            break
                        # Loop continues → show revised plan to user

                # Write final version to disk (overwrites initial write)
                try:
                    with open(plan_path, "w") as f:
                        f.write(output)
                except OSError:
                    log.warning("Failed to write plan to %s", plan_path, exc_info=True)
                    output += "\n\n---\nPlan could not be saved to disk."
                    results[i] = (cid, output)
                    continue

            # Always include file path in the tool result so the
            # outer model knows where the plan lives on disk.
            output += f"\n\n---\nPlan saved to `{plan_path}`"
            results[i] = (cid, output)

        return results, user_feedback

    @staticmethod
    def _ensure_tool_call_ids(tool_calls: list[dict[str, Any]] | dict[int, dict[str, Any]]) -> None:
        """Fill in missing tool call IDs with synthetic UUIDs.

        Some local servers (llama.cpp, older vLLM) omit or leave the id
        blank; an empty tool_call_id corrupts subsequent turns because
        the matching tool-result message can't reference the call.
        """
        items = tool_calls.values() if isinstance(tool_calls, dict) else tool_calls
        for tc in items:
            if not tc.get("id"):
                tc["id"] = f"call_{uuid.uuid4().hex}"

    def _prepare_tool(self, tc: dict[str, Any]) -> dict[str, Any]:
        """Parse a tool call and prepare preview info for display."""
        call_id = tc["id"]
        func_name = tc["function"]["name"].strip()
        raw_args = tc["function"]["arguments"]

        # Some providers emit an empty string when the model invokes a
        # tool with no arguments (all params optional → no JSON object
        # produced).  Treat that as ``{}`` rather than feeding the
        # empty string to json.loads which raises and drops the call
        # into the malformed-args error branch.
        if raw_args == "" or raw_args is None:
            raw_args = "{}"

        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            args = None
            # Fallback 1: regex-extract a known key from malformed JSON.
            # Keep this list focused on primary/identifying keys — the
            # model will see the salvaged minimal-args result and
            # resubmit with correct JSON on the next turn.  Coordinator
            # keys (ws_id, message, initial_message, parent_ws_id) are
            # included so malformed coordinator tool calls aren't a
            # dead-end.
            for key in (
                "action",
                "command",
                "code",
                "content",
                "initial_message",
                "message",
                "name",
                "page",
                "parent_ws_id",
                "path",
                "pattern",
                "prompt",
                "query",
                "status",
                "task_id",
                "title",
                "uri",
                "url",
                "ws_id",
            ):
                m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
                if m:
                    try:
                        val = json.loads('"' + m.group(1) + '"')
                    except (json.JSONDecodeError, Exception):
                        val = m.group(1)
                    args = {key: val}
                    break
            # Fallback 2: bare string (no JSON wrapper at all)
            if args is None and raw_args.strip() and not raw_args.strip().startswith("{"):
                pk = PRIMARY_KEY_MAP.get(func_name)
                if pk:
                    args = {pk: raw_args}
            if args is None:
                preview = raw_args[:4000] + ("..." if len(raw_args) > 4000 else "")
                # Surface to user so they can see what the model produced
                self.ui.on_error(
                    f"Malformed tool call from model: {func_name}() — "
                    f"could not parse arguments as JSON.\n"
                    f"  Raw: {raw_args[:200]}"
                )
                # Build a hint for the model including expected parameter
                # names so it can self-correct on retry.
                expected = PRIMARY_KEY_MAP.get(func_name, "")
                hint = (
                    f' Expected valid JSON, e.g. {{"{expected}": "..."}}'
                    if expected
                    else " Arguments must be valid JSON."
                )
                return {
                    "call_id": call_id,
                    "func_name": func_name,
                    "header": f"\u2717 {func_name}: {exc}",
                    "preview": f"    {preview}",
                    "needs_approval": False,
                    "error": (
                        f"JSON parse error for tool '{func_name}': {exc}\n"
                        f"Raw arguments: {raw_args[:500]}\n"
                        f"{hint}\n"
                        f"Please retry with correctly formatted JSON arguments."
                    ),
                }

        preparers = {
            "bash": self._prepare_bash,
            "read_file": self._prepare_read_file,
            "search": self._prepare_search,
            "diff_file": self._prepare_diff,
            "write_file": self._prepare_write_file,
            "edit_file": self._prepare_edit_file,
            "math": self._prepare_math,
            "man": self._prepare_man,
            "web_fetch": self._prepare_web_fetch,
            "web_search": self._prepare_web_search,
            "tool_search": self._prepare_tool_search,
            "task_agent": self._prepare_task,
            "plan_agent": self._prepare_plan,
            "memory": self._prepare_memory,
            "recall": self._prepare_recall,
            "notify": self._prepare_notify,
            "watch": self._prepare_watch,
            "read_resource": self._prepare_read_resource,
            "use_prompt": self._prepare_use_prompt,
            "skill": self._prepare_skill,
            # Coordinator tools: only reachable when this session was
            # constructed with kind="coordinator" (COORDINATOR_TOOLS set).
            "spawn_workstream": self._prepare_spawn_workstream,
            "inspect_workstream": self._prepare_inspect_workstream,
            "send_to_workstream": self._prepare_send_to_workstream,
            "close_workstream": self._prepare_close_workstream,
            "cancel_workstream": self._prepare_cancel_workstream,
            "delete_workstream": self._prepare_delete_workstream,
            "list_workstreams": self._prepare_list_workstreams,
            "list_nodes": self._prepare_list_nodes,
            "list_skills": self._prepare_list_skills,
            "task_list": self._prepare_task_list,
            "wait_for_workstream": self._prepare_wait_for_workstream,
        }
        preparer = preparers.get(func_name)
        if not preparer:
            # Check if this is an MCP tool
            if self._mcp_client and self._mcp_client.is_mcp_tool(func_name):
                return self._prepare_mcp_tool(call_id, func_name, args)
            self.ui.on_error(f"Model called unknown tool: {func_name!r}")
            available = list(preparers)
            if self._mcp_client:
                available.extend(sorted(self._mcp_client._tool_map))
            return {
                "call_id": call_id,
                "func_name": func_name,
                "header": f"\u2717 Unknown tool: {func_name}",
                "preview": "",
                "needs_approval": False,
                "error": (
                    f"Unknown tool: {func_name!r}. "
                    f"Available tools: {', '.join(available)}. "
                    f"Use one of the listed tool names exactly."
                ),
            }
        assert args is not None  # guaranteed by the early return on args is None above
        return preparer(call_id, args)

    # -- Prepare methods (build preview, validate, no side effects) ------------

    def _prepare_bash(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        command = sanitize_command(args.get("command", ""))
        if not command:
            return {
                "call_id": call_id,
                "func_name": "bash",
                "header": "\u2717 bash: empty command",
                "preview": "",
                "needs_approval": False,
                "error": "Error: empty command",
            }
        blocked = is_command_blocked(command)
        if blocked:
            return {
                "call_id": call_id,
                "func_name": "bash",
                "header": f"\u2717 {blocked}",
                "preview": "",
                "needs_approval": False,
                "error": blocked,
            }
        display_cmd = command.split("\n")[0]
        is_multiline = "\n" in command
        if is_multiline:
            extra = command.count(chr(10))
            display_cmd += f" ... ({extra} more {'line' if extra == 1 else 'lines'})"
        timeout = args.get("timeout")
        try:
            timeout = int(timeout) if timeout is not None else None
        except (ValueError, TypeError):
            timeout = None
        if timeout is not None:
            timeout = max(1, min(timeout, 600))  # clamp 1-600s

        # Show full command in preview for multi-line scripts
        preview = ""
        if is_multiline:
            preview = f"{DIM}{textwrap.indent(command, '    ')}{RESET}"

        return {
            "call_id": call_id,
            "func_name": "bash",
            "header": (
                f"\u2699 bash ({timeout}s): {display_cmd}"
                if timeout is not None
                else f"\u2699 bash: {display_cmd}"
            ),
            "preview": preview,
            "needs_approval": True,
            "approval_label": "bash",
            "execute": self._exec_bash,
            "command": command,
            "timeout": timeout,
            "stop_on_error": args.get("stop_on_error") is True,
        }

    def _prepare_read_file(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        if not path:
            return {
                "call_id": call_id,
                "func_name": "read_file",
                "header": "\u2717 read_file: missing path",
                "preview": "",
                "needs_approval": False,
                "error": "Error: missing path",
            }
        path = os.path.expanduser(path)
        resolved = os.path.realpath(path)
        offset = args.get("offset")  # 1-based line number, or None
        limit = args.get("limit")  # max lines, or None
        # Coerce to int safely (model may send strings or floats)
        try:
            if offset is not None:
                offset = int(offset)
            if limit is not None:
                limit = int(limit)
        except (ValueError, TypeError):
            return {
                "call_id": call_id,
                "func_name": "read_file",
                "header": "\u2717 read_file: invalid offset/limit",
                "preview": "",
                "needs_approval": False,
                "error": (
                    f"Error: offset/limit must be integers "
                    f"(got offset={args.get('offset')!r}, "
                    f"limit={args.get('limit')!r})"
                ),
            }
        if offset is not None and offset < 1:
            return {
                "call_id": call_id,
                "func_name": "read_file",
                "header": "\u2717 read_file: offset must be >= 1",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: offset must be >= 1 (got {offset})",
            }
        if limit is not None and limit < 1:
            return {
                "call_id": call_id,
                "func_name": "read_file",
                "header": "\u2717 read_file: limit must be >= 1",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: limit must be >= 1 (got {limit})",
            }
        # Register early so a same-batch edit_file can pass the read guard.
        self._read_files.add(resolved)
        # Build header showing range if specified
        header = f"\u2699 read_file: {path}"
        if offset is not None or limit is not None:
            start = offset or 1
            if limit is not None:
                header += f" (lines {start}-{start + limit - 1})"
            else:
                header += f" (from line {start})"
        return {
            "call_id": call_id,
            "func_name": "read_file",
            "header": header,
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_read_file,
            "path": path,
            "offset": offset,
            "limit": limit,
        }

    def _prepare_search(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        pattern = args.get("query", "")
        if not pattern:
            return {
                "call_id": call_id,
                "func_name": "search",
                "header": "\u2717 search: missing query",
                "preview": "",
                "needs_approval": False,
                "error": "Error: missing query",
            }
        path = os.path.expanduser(args.get("path", "") or ".")
        preview = f"    {DIM}/{pattern}/ in {path}{RESET}"
        return {
            "call_id": call_id,
            "func_name": "search",
            "header": f"\u2699 search: /{pattern}/ in {path}",
            "preview": preview,
            "needs_approval": False,
            "execute": self._exec_search,
            "pattern": pattern,
            "path": path,
        }

    def _prepare_diff(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        path_a = args.get("path_a", "")
        path_b = args.get("path_b", "")
        content_b = args.get("content_b")
        if not path_a:
            return {
                "call_id": call_id,
                "func_name": "diff_file",
                "header": "\u2717 diff_file: missing path_a",
                "preview": "",
                "needs_approval": False,
                "error": "Error: path_a is required",
            }
        if path_b and content_b is not None:
            return {
                "call_id": call_id,
                "func_name": "diff_file",
                "header": "\u2717 diff_file: ambiguous params",
                "preview": "",
                "needs_approval": False,
                "error": "Error: provide path_b or content_b, not both",
            }
        if not path_b and content_b is None:
            return {
                "call_id": call_id,
                "func_name": "diff_file",
                "header": "\u2717 diff_file: missing comparison target",
                "preview": "",
                "needs_approval": False,
                "error": "Error: provide path_b (another file) or content_b (string to compare against)",
            }
        ctx = args.get("context_lines")
        try:
            ctx = int(ctx) if ctx is not None else 3
        except (ValueError, TypeError):
            ctx = 3
        ctx = max(0, min(ctx, 20))
        path_a = os.path.expanduser(path_a)
        path_b = os.path.expanduser(path_b) if path_b else ""
        if path_b:
            header = f"\u2699 diff_file: {path_a} vs {path_b}"
        else:
            header = f"\u2699 diff_file: {path_a} vs provided content"
        return {
            "call_id": call_id,
            "func_name": "diff_file",
            "header": header,
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_diff,
            "path_a": path_a,
            "path_b": path_b,
            "content_b": content_b,
            "context_lines": ctx,
        }

    def _prepare_write_file(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return {
                "call_id": call_id,
                "func_name": "write_file",
                "header": "\u2717 write_file: missing path",
                "preview": "",
                "needs_approval": False,
                "error": "Error: missing path",
            }
        path = os.path.expanduser(path)
        resolved = os.path.realpath(path)
        is_symlink = os.path.abspath(path) != resolved
        exists = os.path.exists(resolved)
        raw_mode = args.get("mode")
        mode = str(raw_mode).strip().lower() if raw_mode else "overwrite"
        if mode not in ("overwrite", "append"):
            mode = "overwrite"
        is_append = mode == "append"
        is_overwrite = exists and resolved not in self._read_files and not is_append

        # Build preview
        preview_parts = []
        if is_symlink:
            preview_parts.append(f"    {YELLOW}Warning: symlink — actual target: {resolved}{RESET}")
        if is_overwrite:
            preview_parts.append(
                f"    {YELLOW}Warning: overwriting existing file not previously read{RESET}"
            )
        if is_append:
            preview_parts.append(f"    {YELLOW}(append mode){RESET}")
        preview_parts.append(f"{DIM}{textwrap.indent(content, '    ')}{RESET}")

        verb = "append" if is_append else "write"
        header = f"\u2699 write_file ({verb}): {path} ({len(content)} chars)"
        if is_symlink:
            header = f"\u2699 write_file ({verb}): {path} \u2192 {resolved} ({len(content)} chars)"

        return {
            "call_id": call_id,
            "func_name": "write_file",
            "header": header,
            "preview": "\n".join(preview_parts),
            "needs_approval": True,
            "approval_label": "append_file"
            if is_append
            else ("overwrite_file" if is_overwrite else "write_file"),
            "execute": self._exec_write_file,
            "path": path,
            "resolved": resolved,
            "content": content,
            "append": is_append,
        }

    def _validate_edit_entry(self, e: dict[str, Any], idx: int | None) -> dict[str, Any] | None:
        """Validate a single edit entry. Returns an error dict or None."""
        label = f"edits[{idx}]: " if idx is not None else ""
        old = e.get("old_string", "")
        new = e.get("new_string", "")
        if not old:
            return {
                "call_id": "",
                "func_name": "edit_file",
                "header": f"\u2717 edit_file: {label}missing old_string",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: {label}missing old_string",
            }
        if old == new:  # deletion (new_string="") is fine
            return {
                "call_id": "",
                "func_name": "edit_file",
                "header": f"\u2717 edit_file: {label}no-op",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: {label}old_string and new_string are identical",
            }
        return None

    @staticmethod
    def _normalize_edit_entry(e: dict[str, Any]) -> dict[str, Any]:
        """Normalize a single edit entry into a canonical dict."""
        nl = e.get("near_line")
        if isinstance(nl, str):
            try:
                nl = int(nl)
            except ValueError:
                nl = None
        return {
            "old_string": e.get("old_string", ""),
            "new_string": e.get("new_string", ""),
            "near_line": nl,
        }

    def _prepare_edit_file(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        if not path:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": "\u2717 edit_file: missing path",
                "preview": "",
                "needs_approval": False,
                "error": "Error: missing path",
            }

        # Normalize into a list of edit dicts: old_string + new_string [+ near_line]
        raw_edits = args.get("edits")
        has_single = bool(args.get("old_string"))
        has_batch = bool(raw_edits and isinstance(raw_edits, list))
        if has_single and has_batch:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": "\u2717 edit_file: ambiguous params",
                "preview": "",
                "needs_approval": False,
                "error": "Error: provide old_string/new_string or edits array, not both",
            }
        if has_batch:
            # raw_edits is guaranteed to be a list by the has_batch check above
            batch_edits: list[Any] = raw_edits  # type: ignore[assignment]
            edits: list[dict[str, Any]] = []
            for i, e in enumerate(batch_edits):
                if not isinstance(e, dict):
                    return {
                        "call_id": call_id,
                        "func_name": "edit_file",
                        "header": f"\u2717 edit_file: edits[{i}] not an object",
                        "preview": "",
                        "needs_approval": False,
                        "error": f"Error: edits[{i}] must be an object with old_string and new_string",
                    }
                err = self._validate_edit_entry(e, i)
                if err:
                    err["call_id"] = call_id
                    return err
                edits.append(self._normalize_edit_entry(e))
        else:
            err = self._validate_edit_entry(args, None)
            if err:
                err["call_id"] = call_id
                return err
            edits = [self._normalize_edit_entry(args)]

        replace_all = bool(args.get("replace_all"))
        if replace_all and has_batch:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": "\u2717 edit_file: invalid params",
                "preview": "",
                "needs_approval": False,
                "error": "Error: replace_all cannot be used with edits array",
            }
        if replace_all and edits[0].get("near_line") is not None:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": "\u2717 edit_file: invalid params",
                "preview": "",
                "needs_approval": False,
                "error": "Error: replace_all cannot be used with near_line",
            }

        path = os.path.expanduser(path)
        resolved = os.path.realpath(path)
        is_symlink = os.path.abspath(path) != resolved

        if resolved not in self._read_files:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": f"\u2717 edit_file: {path}",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: must read_file {path} before editing it",
            }

        # Pre-read to validate all edits and build diff preview
        try:
            with open(resolved) as f:
                content = f.read()

            for i, edit in enumerate(edits):
                old = edit["old_string"]
                nl = edit.get("near_line")
                label = f"edits[{i}]: " if len(edits) > 1 else ""
                occurrences = find_occurrences(content, old)
                if len(occurrences) == 0:
                    return {
                        "call_id": call_id,
                        "func_name": "edit_file",
                        "header": f"\u2717 edit_file: {path}",
                        "preview": "",
                        "needs_approval": False,
                        "error": (
                            f"Error: {label}old_string not found in {path}. "
                            "The file may have changed — re-read it before retrying."
                        ),
                    }
                if len(occurrences) > 1 and nl is None and not replace_all:
                    line_list = ", ".join(str(ln) for ln in occurrences)
                    return {
                        "call_id": call_id,
                        "func_name": "edit_file",
                        "header": f"\u2717 edit_file: {path}",
                        "preview": "",
                        "needs_approval": False,
                        "error": (
                            f"Error: {label}old_string found {len(occurrences)} times "
                            f"at lines {line_list} — use near_line or replace_all"
                        ),
                    }
        except FileNotFoundError:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": f"\u2717 edit_file: {path}",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: {path} not found",
            }
        except Exception as e:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": f"\u2717 edit_file: {path}",
                "preview": "",
                "needs_approval": False,
                "error": f"Error editing {path}: {e}",
            }

        # Build diff preview
        preview_parts = []
        if is_symlink:
            preview_parts.append(f"    {YELLOW}Warning: symlink — actual target: {resolved}{RESET}")
        if replace_all:
            occ = content.count(edits[0]["old_string"])
            preview_parts.append(f"    {YELLOW}(replace_all: {occ} occurrences){RESET}")
        for i, edit in enumerate(edits):
            if len(edits) > 1:
                preview_parts.append(f"    {YELLOW}--- edit {i + 1}/{len(edits)} ---{RESET}")
            for line in edit["old_string"].splitlines():
                preview_parts.append(f"    {RED}- {line}{RESET}")
            if edit["new_string"]:
                for line in edit["new_string"].splitlines():
                    preview_parts.append(f"    {GREEN}+ {line}{RESET}")
            else:
                n = len(edit["old_string"])
                preview_parts.append(f"    {YELLOW}(deletion — {n} chars removed){RESET}")

        count = len(edits)
        if is_symlink:
            header = (
                f"\u2699 edit_file: {path} \u2192 {resolved} ({count} edits)"
                if count > 1
                else f"\u2699 edit_file: {path} \u2192 {resolved}"
            )
        else:
            header = (
                f"\u2699 edit_file: {path} ({count} edits)"
                if count > 1
                else f"\u2699 edit_file: {path}"
            )
        return {
            "call_id": call_id,
            "func_name": "edit_file",
            "header": header,
            "preview": "\n".join(preview_parts),
            "needs_approval": True,
            "approval_label": "edit_file",
            "execute": self._exec_edit_file,
            "path": path,
            "resolved": resolved,
            "edits": edits,
            "replace_all": replace_all,
        }

    def _prepare_math(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        code = args.get("code", "")
        if isinstance(code, list):
            code = "\n".join(code)
        if not code:
            return {
                "call_id": call_id,
                "func_name": "math",
                "header": "\u2717 math: empty code",
                "preview": "",
                "needs_approval": False,
                "error": "Error: no code provided",
            }
        # Show code preview
        preview = f"{DIM}{textwrap.indent(code, '    ')}{RESET}"
        return {
            "call_id": call_id,
            "func_name": "math",
            "header": f"\u2699 math: ({len(code)} chars)",
            "preview": preview,
            "needs_approval": True,
            "approval_label": "math",
            "execute": self._exec_math,
            "code": code,
        }

    def _prepare_man(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a man/info page lookup."""
        page = (args.get("page") or "").strip()
        if not page:
            return {
                "call_id": call_id,
                "func_name": "man",
                "header": "\u2717 man: empty page",
                "preview": "",
                "needs_approval": False,
                "error": "Error: no page name provided",
            }
        # Sanitize: only allow alphanumeric, dash, underscore, dot
        if not re.match(r"^[a-zA-Z0-9._-]+$", page):
            return {
                "call_id": call_id,
                "func_name": "man",
                "header": "\u2717 man: invalid page name",
                "preview": f"    {page}",
                "needs_approval": False,
                "error": f"Error: invalid page name {page!r}",
            }
        section = (args.get("section") or "").strip()
        if section and not re.match(r"^[1-9][a-z]?$", section):
            section = ""
        label = f"{page}({section})" if section else page
        preview = f"    {DIM}{label}{RESET}"
        return {
            "call_id": call_id,
            "func_name": "man",
            "header": f"\u2699 man: {label}",
            "preview": preview,
            "needs_approval": False,
            "execute": self._exec_man,
            "page": page,
            "section": section,
        }

    def _prepare_web_fetch(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url", "").strip()
        question = args.get("question", "").strip()
        if not url:
            return {
                "call_id": call_id,
                "func_name": "web_fetch",
                "header": "\u2717 web_fetch: empty url",
                "preview": "",
                "needs_approval": False,
                "error": "Error: no URL provided",
            }
        if not question:
            return {
                "call_id": call_id,
                "func_name": "web_fetch",
                "header": "\u2717 web_fetch: empty question",
                "preview": "",
                "needs_approval": False,
                "error": "Error: no question provided",
            }
        if not url.startswith(("http://", "https://")):
            return {
                "call_id": call_id,
                "func_name": "web_fetch",
                "header": "\u2717 web_fetch: invalid url",
                "preview": f"    {url}",
                "needs_approval": False,
                "error": f"Error: URL must start with http:// or https:// (got {url!r})",
            }
        # SSRF protection: reject private/link-local/metadata IPs
        ssrf_err = check_ssrf(url)
        if ssrf_err:
            return {
                "call_id": call_id,
                "func_name": "web_fetch",
                "header": "\u2717 web_fetch: blocked (private network)",
                "preview": f"    {url}",
                "needs_approval": False,
                "error": f"Error: {ssrf_err}",
            }
        q_preview = question[:200] + ("..." if len(question) > 200 else "")
        preview = f"    {url}\n    Q: {q_preview}"
        return {
            "call_id": call_id,
            "func_name": "web_fetch",
            "header": f"\u2699 web_fetch: {url[:80]}",
            "preview": preview,
            "needs_approval": True,
            "approval_label": "web_fetch",
            "execute": self._exec_web_fetch,
            "url": url,
            "question": question,
        }

    def _prepare_web_search(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a web search via Tavily for approval."""
        query = (args.get("query") or "").strip()
        if not query:
            return {
                "call_id": call_id,
                "func_name": "web_search",
                "header": "\u2717 web_search: empty query",
                "preview": "",
                "needs_approval": False,
                "error": "Error: no query provided",
            }
        if not self._resolve_search_client():
            return {
                "call_id": call_id,
                "func_name": "web_search",
                "header": "\u2717 web_search: no backend available",
                "preview": "",
                "needs_approval": False,
                "error": (
                    "Error: No web search backend available. "
                    "Install the ddg extra (`pip install turnstone[ddg]`), "
                    "configure a Tavily API key, or set tools.web_search_backend."
                ),
            }
        try:
            max_results = min(max(int(args.get("max_results") or 5), 1), 20)
        except (ValueError, TypeError):
            max_results = 5
        topic = args.get("topic", "general") or "general"
        if topic not in ("general", "news", "finance"):
            topic = "general"
        q_preview = query[:200] + ("..." if len(query) > 200 else "")
        preview = f"    {q_preview}"
        return {
            "call_id": call_id,
            "func_name": "web_search",
            "header": f"\u2699 web_search: {query[:80]}",
            "preview": preview,
            "needs_approval": True,
            "approval_label": "web_search",
            "execute": self._exec_web_search,
            "query": query,
            "max_results": max_results,
            "topic": topic,
        }

    def _prepare_tool_search(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a tool search query (client-side BM25 fallback)."""
        query = (args.get("query") or "").strip()
        if not query:
            return {
                "call_id": call_id,
                "func_name": "tool_search",
                "header": "\u2717 tool_search: empty query",
                "preview": "",
                "needs_approval": False,
                "error": "Error: no query provided",
            }
        if not self._tool_search:
            return {
                "call_id": call_id,
                "func_name": "tool_search",
                "header": "\u2717 tool_search: not active",
                "preview": "",
                "needs_approval": False,
                "error": "Tool search is not active.",
            }
        return {
            "call_id": call_id,
            "func_name": "tool_search",
            "header": f"\u2699 tool_search: {query[:80]}",
            "preview": f"    {query}",
            "needs_approval": False,
            "execute": self._exec_tool_search,
            "query": query,
        }

    def _exec_tool_search(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute a client-side tool search and expand visible tools."""
        assert self._tool_search is not None
        query = item["query"]
        results = self._tool_search.search(query)
        # Expand discovered tools into the visible set
        names = [t.get("function", {}).get("name", "") for t in results]
        self._tool_search.expand_visible(names)
        output = self._tool_search.format_search_results(results)
        return item["call_id"], output

    def _validate_agent_model_override(
        self, call_id: str, func_name: str, args: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Pull and validate the optional `model` arg for plan/task agents.

        Returns (alias, error_item).  When the caller passed a `model` and
        it isn't in the registry, returns an error_item shaped like the
        existing _prepare_* error dicts so the LLM gets corrective guidance
        and retries.  When no override was passed, returns (None, None).
        """
        raw = args.get("model")
        if raw is None or raw == "":
            return None, None
        alias = str(raw).strip()
        if not alias:
            return None, None
        if self._registry is None or not self._registry.has_alias(alias):
            available = sorted(self._registry.list_aliases()) if self._registry is not None else []
            available_str = ", ".join(available) if available else "(no registry configured)"
            return None, {
                "call_id": call_id,
                "func_name": func_name,
                "header": f"\u2717 {func_name}: unknown model alias",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: unknown model alias '{alias}'. Available: {available_str}",
            }
        return alias, None

    def _prepare_task(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a general-purpose sub-agent task for approval."""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {
                "call_id": call_id,
                "func_name": "task_agent",
                "header": "\u2717 task_agent: empty prompt",
                "preview": "",
                "needs_approval": False,
                "error": "Error: empty prompt",
            }
        model_override, err = self._validate_agent_model_override(call_id, "task_agent", args)
        if err is not None:
            return err
        preview_text = prompt[:300] + ("..." if len(prompt) > 300 else "")
        return {
            "call_id": call_id,
            "func_name": "task_agent",
            "header": "\u2699 task_agent (autonomous agent)",
            "preview": f"    {preview_text}",
            "needs_approval": True,
            "approval_label": "task_agent",
            "execute": self._exec_task,
            "prompt": prompt,
            "model_override": model_override,
        }

    def _prepare_plan(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a planning agent for approval."""
        goal = args.get("goal", "").strip()
        if not goal:
            return {
                "call_id": call_id,
                "func_name": "plan_agent",
                "header": "\u2717 plan_agent: empty goal",
                "preview": "",
                "needs_approval": False,
                "error": "Error: empty goal",
            }
        model_override, err = self._validate_agent_model_override(call_id, "plan_agent", args)
        if err is not None:
            return err
        preview_text = goal[:300] + ("..." if len(goal) > 300 else "")
        return {
            "call_id": call_id,
            "func_name": "plan_agent",
            "header": "\u2699 plan_agent (planning agent)",
            "preview": f"    {preview_text}",
            "needs_approval": True,
            "approval_label": "plan_agent",
            "execute": self._exec_plan,
            "prompt": goal,
            "model_override": model_override,
        }

    def _resolve_scope_id(self, scope: str) -> str:
        """Map a scope name to its scope_id."""
        if scope == "workstream":
            return self._ws_id
        if scope == "user":
            return self._user_id
        return ""

    def _validate_scope(self, scope: str, call_id: str) -> dict[str, Any] | None:
        """Return an error dict if scope is invalid, None if OK."""
        if scope == "user" and not self._user_id:
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": "\u2717 memory: user scope requires authentication",
                "preview": "",
                "needs_approval": False,
                "error": "Error: 'user' scope requires authenticated user identity",
            }
        return None

    def _visible_memory_count(self) -> int:
        """Count memories visible to this session (cheap — counts only)."""
        n = count_structured_memories(scope="global")
        n += count_structured_memories(scope="workstream", scope_id=self._ws_id)
        if self._user_id:
            n += count_structured_memories(scope="user", scope_id=self._user_id)
        return n

    def _list_visible_memories(self, mem_type: str = "", limit: int = 50) -> list[dict[str, str]]:
        """List memories visible to this session with optional type filter."""
        global_mems = list_structured_memories(mem_type=mem_type, scope="global", limit=limit)
        ws_mems = list_structured_memories(
            mem_type=mem_type, scope="workstream", scope_id=self._ws_id, limit=limit
        )
        user_mems: list[dict[str, str]] = []
        if self._user_id:
            user_mems = list_structured_memories(
                mem_type=mem_type, scope="user", scope_id=self._user_id, limit=limit
            )
        combined = global_mems + ws_mems + user_mems
        combined.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return combined[:limit]

    def _search_visible_memories(
        self, query: str, mem_type: str = "", limit: int = 20
    ) -> list[dict[str, str]]:
        """Search memories visible to this session (scope-filtered)."""
        global_mems = search_structured_memories(
            query, mem_type=mem_type, scope="global", limit=limit
        )
        ws_mems = search_structured_memories(
            query, mem_type=mem_type, scope="workstream", scope_id=self._ws_id, limit=limit
        )
        user_mems: list[dict[str, str]] = []
        if self._user_id:
            user_mems = search_structured_memories(
                query, mem_type=mem_type, scope="user", scope_id=self._user_id, limit=limit
            )
        combined = global_mems + ws_mems + user_mems
        combined.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return combined[:limit]

    def _check_metacognitive_nudge(self, user_message: str) -> tuple[str, str] | None:
        """Check if a metacognitive nudge should be injected.

        Returns (nudge_type, nudge_text) or None.
        """
        if not self._mem_cfg.nudges:
            return None
        mem_count = self._visible_memory_count()
        msg_count = len(self.messages)
        cd = self._mem_cfg.nudge_cooldown

        if should_nudge(
            "start",
            self._metacog_state,
            message_count=msg_count,
            memory_count=mem_count,
            cooldown_secs=cd,
        ):
            return ("start", format_nudge("start"))

        if detect_correction(user_message) and should_nudge(
            "correction",
            self._metacog_state,
            message_count=msg_count,
            memory_count=mem_count,
            cooldown_secs=cd,
        ):
            return ("correction", format_nudge("correction"))

        if detect_completion(user_message) and should_nudge(
            "completion",
            self._metacog_state,
            message_count=msg_count,
            memory_count=mem_count,
            cooldown_secs=cd,
        ):
            return ("completion", format_nudge("completion"))

        return None

    # ------------------------------------------------------------------
    # Coordinator tools — reachable only when ``kind == "coordinator"``.
    # All six dispatch through ``self._coord_client`` which is None when
    # the session is interactive, so the prepare methods guard defensively
    # and return an error item on misuse.
    # ------------------------------------------------------------------

    def _coord_tool_error(self, call_id: str, func_name: str, msg: str) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "func_name": func_name,
            "header": f"\u2717 {func_name}: {msg}",
            "preview": "",
            "needs_approval": False,
            "error": f"Error: {msg}",
        }

    @staticmethod
    def _coord_str_arg(args: dict[str, Any], key: str, default: str = "") -> str:
        """Return ``args[key]`` if it's a string, else ``default``.

        Coordinator tool args come from an LLM and may be ill-typed
        (int / list / dict in a string slot).  A naive
        ``(args.get(key) or "").strip()`` raises ``AttributeError`` on
        such inputs and kills the whole tool call; this guard lets the
        prepare layer fall through to its own "required" validation and
        produce a clean error item instead.
        """
        val = args.get(key)
        return val if isinstance(val, str) else default

    @staticmethod
    def _coord_bool_arg(args: dict[str, Any], key: str, default: bool = False) -> bool:
        """Return ``args[key]`` as a bool with robust string coercion.

        Plain ``bool(x)`` treats ``"false"`` as truthy (non-empty string).
        Accept actual bools verbatim; parse common string forms; return
        ``default`` for anything else.
        """
        val = args.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in ("true", "1", "yes", "on"):
                return True
            if normalized in ("false", "0", "no", "off", ""):
                return False
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return bool(val)
        return default

    def _prepare_spawn_workstream(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "spawn_workstream", "coordinator client unavailable"
            )
        # Empty initial_message is allowed — creates an idle child
        # workstream ready to receive the first turn via
        # send_to_workstream.  The tool JSON advertises this explicitly.
        initial_message = (args.get("initial_message") or "").strip()
        skill = (args.get("skill") or "").strip()
        name = (args.get("name") or "").strip()
        model = (args.get("model") or "").strip()
        target_node = (args.get("target_node") or "").strip()
        if initial_message:
            first_line = initial_message.splitlines()[0]
            preview_line = first_line[:120] + ("..." if len(first_line) > 120 else "")
            header_bits = [f"\u2699 spawn_workstream: {preview_line}"]
            preview_body = f"{DIM}{textwrap.indent(initial_message, '    ')}{RESET}"
        else:
            header_bits = ["\u2699 spawn idle workstream"]
            preview_body = ""
        if skill:
            header_bits.append(f"skill={skill}")
        if target_node:
            header_bits.append(f"node={target_node}")
        header = " ".join(header_bits)
        return {
            "call_id": call_id,
            "func_name": "spawn_workstream",
            "header": header,
            "preview": preview_body,
            "needs_approval": True,
            "approval_label": "spawn_workstream",
            "execute": self._exec_spawn_workstream,
            "initial_message": initial_message,
            "skill": skill,
            "name": name,
            "model": model,
            "target_node": target_node,
        }

    def _exec_spawn_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.spawn(
                initial_message=item["initial_message"],
                parent_ws_id=self._ws_id,
                user_id=self._user_id,
                skill=item["skill"],
                name=item["name"],
                model=item["model"],
                target_node=item["target_node"],
            )
        except Exception as e:
            msg = f"Error: spawn_workstream failed: {e}"
            self._report_tool_result(call_id, "spawn_workstream", msg, is_error=True)
            return call_id, msg
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "spawn_workstream", msg, is_error=True)
            return call_id, msg
        # Successful spawn — surface ws_id + node_id + name + routing
        # strategy so the coordinator can follow up with inspect / send
        # and explain why a given node was chosen.
        summary = json.dumps(
            {
                "ws_id": result.get("ws_id"),
                "name": result.get("name"),
                "node_id": result.get("node_id"),
                "routing_strategy": result.get("routing_strategy"),
                "status": result.get("status"),
            },
            separators=(",", ":"),
        )
        self._report_tool_result(call_id, "spawn_workstream", f"spawned {result.get('ws_id', '?')}")
        return call_id, summary

    def _prepare_inspect_workstream(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "inspect_workstream", "coordinator client unavailable"
            )
        ws_id = (args.get("ws_id") or "").strip()
        if not ws_id:
            return self._coord_tool_error(call_id, "inspect_workstream", "ws_id is required")
        try:
            message_limit = int(args.get("message_limit") or 20)
        except (TypeError, ValueError):
            message_limit = 20
        message_limit = max(1, min(message_limit, 200))
        include_provider_content = bool(args.get("include_provider_content"))
        return {
            "call_id": call_id,
            "func_name": "inspect_workstream",
            "header": f"\u2699 inspect_workstream: {ws_id}",
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_inspect_workstream,
            "ws_id": ws_id,
            "message_limit": message_limit,
            "include_provider_content": include_provider_content,
        }

    def _exec_inspect_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        ws_id = item["ws_id"]
        try:
            result = self._coord_client.inspect(
                ws_id,
                message_limit=item["message_limit"],
                include_provider_content=item.get("include_provider_content", False),
            )
        except Exception as e:
            msg = f"Error: inspect_workstream failed: {e}"
            self._report_tool_result(call_id, "inspect_workstream", msg, is_error=True)
            return call_id, msg
        output = json.dumps(result, default=str, separators=(",", ":"))
        # Summary for UI: state + message count
        desc = f"{result.get('state', '?')} ({len(result.get('messages', []))} msgs)"
        self._report_tool_result(call_id, "inspect_workstream", desc)
        return call_id, self._truncate_output(output)

    def _prepare_send_to_workstream(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "send_to_workstream", "coordinator client unavailable"
            )
        ws_id = (args.get("ws_id") or "").strip()
        message = args.get("message") or ""
        if not ws_id:
            return self._coord_tool_error(call_id, "send_to_workstream", "ws_id is required")
        if not message.strip():
            return self._coord_tool_error(call_id, "send_to_workstream", "message is required")
        first_line = message.splitlines()[0]
        preview_line = first_line[:120] + ("..." if len(first_line) > 120 else "")
        header = f"\u2699 send_to_workstream {ws_id}: {preview_line}"
        preview_body = f"{DIM}{textwrap.indent(message, '    ')}{RESET}"
        return {
            "call_id": call_id,
            "func_name": "send_to_workstream",
            "header": header,
            "preview": preview_body,
            "needs_approval": True,
            "approval_label": "send_to_workstream",
            "execute": self._exec_send_to_workstream,
            "ws_id": ws_id,
            "message": message,
        }

    def _exec_send_to_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.send(item["ws_id"], item["message"])
        except Exception as e:
            msg = f"Error: send_to_workstream failed: {e}"
            self._report_tool_result(call_id, "send_to_workstream", msg, is_error=True)
            return call_id, msg
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "send_to_workstream", msg, is_error=True)
            return call_id, msg
        output = json.dumps(
            {"ws_id": item["ws_id"], "status": result.get("status", "ok")},
            separators=(",", ":"),
        )
        self._report_tool_result(call_id, "send_to_workstream", f"sent to {item['ws_id']}")
        return call_id, output

    def _prepare_close_workstream(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "close_workstream", "coordinator client unavailable"
            )
        ws_id = (args.get("ws_id") or "").strip()
        if not ws_id:
            return self._coord_tool_error(call_id, "close_workstream", "ws_id is required")
        reason = (args.get("reason") or "").strip()
        header = f"\u2699 close_workstream: {ws_id}"
        if reason:
            header += f" ({reason[:80]})"
        return {
            "call_id": call_id,
            "func_name": "close_workstream",
            "header": header,
            "preview": "",
            "needs_approval": True,
            "approval_label": "close_workstream",
            "execute": self._exec_close_workstream,
            "ws_id": ws_id,
            "reason": reason,
        }

    def _exec_close_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        reason = item.get("reason", "") or ""
        try:
            result = self._coord_client.close_workstream(item["ws_id"], reason=reason)
        except Exception as e:
            msg = f"Error: close_workstream failed: {e}"
            self._report_tool_result(call_id, "close_workstream", msg, is_error=True)
            return call_id, msg
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "close_workstream", msg, is_error=True)
            return call_id, msg
        # Include reason in the tool-result payload so the coordinator's
        # own message stream records why the close happened.  The schema
        # advertises "Recorded in the message stream for audit" — this
        # is the seam that satisfies that contract.
        summary_payload: dict[str, Any] = {
            "ws_id": item["ws_id"],
            "closed": True,
            "status": result.get("status"),
        }
        if reason:
            summary_payload["reason"] = reason
        output = json.dumps(summary_payload, separators=(",", ":"))
        desc = f"closed {item['ws_id']}"
        if reason:
            desc += f" ({reason[:60]})"
        self._report_tool_result(call_id, "close_workstream", desc)
        return call_id, output

    def _prepare_cancel_workstream(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "cancel_workstream", "coordinator client unavailable"
            )
        ws_id = (args.get("ws_id") or "").strip()
        if not ws_id:
            return self._coord_tool_error(call_id, "cancel_workstream", "ws_id is required")
        return {
            "call_id": call_id,
            "func_name": "cancel_workstream",
            "header": f"\u2699 cancel_workstream: {ws_id}",
            "preview": "",
            "needs_approval": True,
            "approval_label": "cancel_workstream",
            "execute": self._exec_cancel_workstream,
            "ws_id": ws_id,
        }

    def _exec_cancel_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.cancel(item["ws_id"])
        except Exception as e:
            msg = f"Error: cancel_workstream failed: {e}"
            self._report_tool_result(call_id, "cancel_workstream", msg, is_error=True)
            return call_id, msg
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "cancel_workstream", msg, is_error=True)
            return call_id, msg
        output = json.dumps(
            {"ws_id": item["ws_id"], "cancelled": True, "status": result.get("status")},
            separators=(",", ":"),
        )
        self._report_tool_result(call_id, "cancel_workstream", f"cancelled {item['ws_id']}")
        return call_id, output

    def _prepare_delete_workstream(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "delete_workstream", "coordinator client unavailable"
            )
        ws_id = (args.get("ws_id") or "").strip()
        if not ws_id:
            return self._coord_tool_error(call_id, "delete_workstream", "ws_id is required")
        return {
            "call_id": call_id,
            "func_name": "delete_workstream",
            "header": f"\u2699 delete_workstream: {ws_id} (irreversible)",
            "preview": "",
            "needs_approval": True,
            "approval_label": "delete_workstream",
            "execute": self._exec_delete_workstream,
            "ws_id": ws_id,
        }

    def _exec_delete_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.delete(item["ws_id"])
        except Exception as e:
            msg = f"Error: delete_workstream failed: {e}"
            self._report_tool_result(call_id, "delete_workstream", msg, is_error=True)
            return call_id, msg
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "delete_workstream", msg, is_error=True)
            return call_id, msg
        output = json.dumps(
            {"ws_id": item["ws_id"], "deleted": True, "status": result.get("status")},
            separators=(",", ":"),
        )
        self._report_tool_result(call_id, "delete_workstream", f"deleted {item['ws_id']}")
        return call_id, output

    def _prepare_list_workstreams(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "list_workstreams", "coordinator client unavailable"
            )
        # parent_ws_id: omit or empty → caller's own ws_id (self).
        # Tool docstring documents this.
        parent_raw = args.get("parent_ws_id")
        if parent_raw is None or parent_raw == "":
            parent_ws_id = self._ws_id
        else:
            parent_ws_id = str(parent_raw).strip() or self._ws_id
        state = (args.get("state") or "").strip() or None
        skill = (args.get("skill") or "").strip() or None
        try:
            limit = int(args.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        include_closed = bool(args.get("include_closed"))
        header_bits = [f"\u2699 list_workstreams: parent={parent_ws_id}"]
        if state:
            header_bits.append(f"state={state}")
        if skill:
            header_bits.append(f"skill={skill}")
        if include_closed:
            header_bits.append("include_closed")
        header = " ".join(header_bits)
        return {
            "call_id": call_id,
            "func_name": "list_workstreams",
            "header": header,
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_list_workstreams,
            "parent_ws_id": parent_ws_id,
            "state": state,
            "skill": skill,
            "limit": limit,
            "include_closed": include_closed,
        }

    def _exec_list_workstreams(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.list_children(
                item["parent_ws_id"],
                state=item["state"],
                skill=item["skill"],
                limit=item["limit"],
                include_closed=item.get("include_closed", False),
            )
        except Exception as e:
            msg = f"Error: list_workstreams failed: {e}"
            self._report_tool_result(call_id, "list_workstreams", msg, is_error=True)
            return call_id, msg
        children = result.get("children", [])
        truncated = bool(result.get("truncated"))
        output = json.dumps(
            {
                "parent_ws_id": item["parent_ws_id"],
                "children": children,
                "truncated": truncated,
            },
            separators=(",", ":"),
            default=str,
        )
        summary = f"{len(children)} children"
        if truncated:
            summary += (
                " (truncated — more may exist; re-run with a narrower filter or larger limit)"
            )
        self._report_tool_result(call_id, "list_workstreams", summary)
        return call_id, self._truncate_output(output)

    def _prepare_list_nodes(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(call_id, "list_nodes", "coordinator client unavailable")
        raw_filters = args.get("filters")
        # Metadata values are JSON-encoded at rest; the client handles the
        # encode/decode so preserve the model's natural types (``4`` stays
        # an int, ``"gpu"`` stays a string) rather than stringifying here.
        filters: dict[str, Any] = {}
        if isinstance(raw_filters, dict):
            for k, v in raw_filters.items():
                if isinstance(k, str) and k and isinstance(v, (str, int, float, bool)):
                    filters[k] = v
        try:
            limit = int(args.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        include_network_detail = bool(args.get("include_network_detail"))
        include_inactive = bool(args.get("include_inactive"))
        header_bits = ["\u2699 list_nodes"]
        if filters:
            header_bits.append(
                "filters=" + ",".join(f"{k}={v}" for k, v in sorted(filters.items()))
            )
        if include_network_detail:
            header_bits.append("network=detail")
        if include_inactive:
            header_bits.append("include_inactive")
        return {
            "call_id": call_id,
            "func_name": "list_nodes",
            "header": " ".join(header_bits),
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_list_nodes,
            "filters": filters,
            "limit": limit,
            "include_network_detail": include_network_detail,
            "include_inactive": include_inactive,
        }

    def _exec_list_nodes(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.list_nodes(
                filters=item["filters"] or None,
                limit=item["limit"],
                include_network_detail=item.get("include_network_detail", False),
                include_inactive=item.get("include_inactive", False),
            )
        except Exception as e:
            msg = f"Error: list_nodes failed: {e}"
            self._report_tool_result(call_id, "list_nodes", msg, is_error=True)
            return call_id, msg
        nodes = result.get("nodes", [])
        truncated = bool(result.get("truncated"))
        output = json.dumps(
            {"nodes": nodes, "truncated": truncated},
            separators=(",", ":"),
            default=str,
        )
        summary = f"{len(nodes)} nodes"
        if truncated:
            summary += " (truncated — narrow filters or raise limit)"
        self._report_tool_result(call_id, "list_nodes", summary)
        return call_id, self._truncate_output(output)

    def _prepare_list_skills(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(call_id, "list_skills", "coordinator client unavailable")
        category = self._coord_str_arg(args, "category").strip() or None
        tag = self._coord_str_arg(args, "tag").strip() or None
        scan_status = self._coord_str_arg(args, "scan_status").strip() or None
        enabled_only = self._coord_bool_arg(args, "enabled_only")
        try:
            limit = int(args.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        header_bits = ["\u2699 list_skills"]
        if category:
            header_bits.append(f"category={category}")
        if tag:
            header_bits.append(f"tag={tag}")
        if scan_status:
            header_bits.append(f"scan_status={scan_status}")
        if enabled_only:
            header_bits.append("enabled_only=true")
        return {
            "call_id": call_id,
            "func_name": "list_skills",
            "header": " ".join(header_bits),
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_list_skills,
            "category": category,
            "tag": tag,
            "scan_status": scan_status,
            "enabled_only": enabled_only,
            "limit": limit,
        }

    def _exec_list_skills(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.list_skills(
                category=item["category"],
                tag=item["tag"],
                scan_status=item["scan_status"],
                enabled_only=item["enabled_only"],
                limit=item["limit"],
            )
        except Exception as e:
            msg = f"Error: list_skills failed: {e}"
            self._report_tool_result(call_id, "list_skills", msg, is_error=True)
            return call_id, msg
        skills = result.get("skills", [])
        truncated = bool(result.get("truncated"))
        output = json.dumps(
            {"skills": skills, "truncated": truncated},
            separators=(",", ":"),
            default=str,
        )
        summary = f"{len(skills)} skills"
        if truncated:
            summary += " (truncated — narrow filters or raise limit)"
        self._report_tool_result(call_id, "list_skills", summary)
        return call_id, self._truncate_output(output)

    def _prepare_task_list(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a task_list action — list is auto-approved, mutations gated."""
        if self._coord_client is None:
            return self._coord_tool_error(call_id, "task_list", "coordinator client unavailable")
        action = self._coord_str_arg(args, "action").strip().lower()
        if action not in {"add", "update", "remove", "reorder", "list"}:
            return self._coord_tool_error(
                call_id,
                "task_list",
                "action must be one of: add, update, remove, reorder, list",
            )
        if action == "list":
            return {
                "call_id": call_id,
                "func_name": "task_list",
                "header": "\u2699 task_list list",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_task_list,
                "action": "list",
            }
        # --- mutating actions -------------------------------------------------
        item: dict[str, Any] = {
            "call_id": call_id,
            "func_name": "task_list",
            "needs_approval": True,
            "execute": self._exec_task_list,
            "action": action,
        }
        if action == "add":
            # Reject non-string title / status / child_ws_id up front so
            # a malformed model call (``title=42``) produces a clean tool
            # error rather than an AttributeError during ``.strip()``.
            for field_name in ("title", "status", "child_ws_id"):
                raw = args.get(field_name)
                if raw is not None and not isinstance(raw, str):
                    return self._coord_tool_error(
                        call_id, "task_list", f"add: {field_name} must be a string"
                    )
            title = self._coord_str_arg(args, "title").strip()
            if not title:
                return self._coord_tool_error(call_id, "task_list", "add: title is required")
            status = self._coord_str_arg(args, "status", "pending").strip() or "pending"
            child_ws_id = self._coord_str_arg(args, "child_ws_id").strip()
            item["header"] = f"\u2699 task_list add: {title[:60]}"
            item["preview"] = f"status={status} child_ws_id={child_ws_id or '-'}"
            item["title"] = title
            item["status"] = status
            item["child_ws_id"] = child_ws_id
        elif action == "update":
            task_id = self._coord_str_arg(args, "task_id").strip()
            if not task_id:
                return self._coord_tool_error(call_id, "task_list", "update: task_id is required")
            # Reject non-string field values outright — avoids a
            # preview/execute divergence where the approver sees
            # ``title=42`` but the coercion below drops it to ``None`` and
            # the mutation silently no-ops on that field.  Local names
            # distinct from the ``add`` branch so mypy doesn't try to
            # unify ``str`` and ``Any | None`` across mutually-exclusive
            # branches.
            upd_title: Any = args.get("title")
            upd_status: Any = args.get("status")
            upd_child: Any = args.get("child_ws_id")
            for field_name, field_val in (
                ("title", upd_title),
                ("status", upd_status),
                ("child_ws_id", upd_child),
            ):
                if field_val is not None and not isinstance(field_val, str):
                    return self._coord_tool_error(
                        call_id,
                        "task_list",
                        f"update: {field_name} must be a string",
                    )
            if upd_title is None and upd_status is None and upd_child is None:
                return self._coord_tool_error(
                    call_id,
                    "task_list",
                    "update: at least one of title / status / child_ws_id is required",
                )
            item["header"] = f"\u2699 task_list update: {task_id}"
            bits: list[str] = []
            if upd_title is not None:
                bits.append(f"title={upd_title[:60]}")
            if upd_status is not None:
                bits.append(f"status={upd_status}")
            if upd_child is not None:
                bits.append(f"child_ws_id={upd_child or '-'}")
            item["preview"] = " ".join(bits)
            item["task_id"] = task_id
            item["title"] = upd_title
            item["status"] = upd_status
            item["child_ws_id"] = upd_child
        elif action == "remove":
            task_id = self._coord_str_arg(args, "task_id").strip()
            if not task_id:
                return self._coord_tool_error(call_id, "task_list", "remove: task_id is required")
            item["header"] = f"\u2699 task_list remove: {task_id}"
            item["preview"] = ""
            item["task_id"] = task_id
        elif action == "reorder":
            raw_ids = args.get("task_ids")
            if not isinstance(raw_ids, list) or not all(isinstance(x, str) for x in raw_ids):
                return self._coord_tool_error(
                    call_id, "task_list", "reorder: task_ids must be a list of strings"
                )
            item["header"] = f"\u2699 task_list reorder: {len(raw_ids)} ids"
            item["preview"] = ",".join(raw_ids[:6]) + ("..." if len(raw_ids) > 6 else "")
            item["task_ids"] = raw_ids
        return item

    def _exec_task_list(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        action = item["action"]
        try:
            if action == "list":
                envelope = self._coord_client.task_list_get(self._ws_id)
                tasks = envelope.get("tasks", [])
                truncated = len(tasks) > 200
                tasks = tasks[:200]
                result: dict[str, Any] = {"tasks": tasks, "truncated": truncated}
            elif action == "add":
                result = self._coord_client.task_list_add(
                    self._ws_id,
                    title=item["title"],
                    status=item["status"],
                    child_ws_id=item["child_ws_id"],
                )
            elif action == "update":
                result = self._coord_client.task_list_update(
                    self._ws_id,
                    task_id=item["task_id"],
                    title=item["title"],
                    status=item["status"],
                    child_ws_id=item["child_ws_id"],
                )
            elif action == "remove":
                result = self._coord_client.task_list_remove(self._ws_id, task_id=item["task_id"])
            elif action == "reorder":
                result = self._coord_client.task_list_reorder(
                    self._ws_id, task_ids=item["task_ids"]
                )
            else:  # unreachable — _prepare validated the enum
                result = {"error": f"unknown action: {action}"}
        except Exception as e:
            msg = f"Error: task_list {action} failed: {e}"
            self._report_tool_result(call_id, "task_list", msg, is_error=True)
            return call_id, msg
        output = json.dumps(result, separators=(",", ":"), default=str)
        if action == "list":
            total = len(result.get("tasks", []))
            summary = f"{total} tasks"
            if result.get("truncated"):
                summary += " (truncated at 200)"
        elif "error" in result:
            summary = f"{action} error: {result['error']}"
        elif action == "add":
            summary = f"added task {result.get('id', '?')}"
        elif action == "update":
            summary = f"updated task {result.get('id', item.get('task_id', '?'))}"
        elif action == "remove":
            summary = f"removed task {item.get('task_id', '?')}"
        elif action == "reorder":
            summary = f"reordered {len(item.get('task_ids', []))} tasks"
        else:
            summary = action
        is_error = "error" in result
        self._report_tool_result(call_id, "task_list", summary, is_error=is_error)
        return call_id, self._truncate_output(output)

    def _prepare_wait_for_workstream(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Thin pass-through to ``CoordinatorClient.wait_for_workstream``.

        The client owns input validation (mode whitelist, ws_ids
        dedup + cap, timeout coerce + clamp) — keeping it as the single
        source of truth means the rules can't drift between layers.
        Bad input surfaces at exec time as a normal tool error via the
        ``result.get("error")`` branch below.
        """
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "wait_for_workstream", "coordinator client unavailable"
            )
        # Best-effort header — uses raw args so the approval UI shows
        # the model's stated request even if validation will reject it.
        raw_ids = args.get("ws_ids") or []
        ws_count = len(raw_ids) if isinstance(raw_ids, list) else 0
        raw_mode = args.get("mode") or "any"
        mode_label = raw_mode.strip().lower() if isinstance(raw_mode, str) else str(raw_mode)
        try:
            to_label = int(float(args.get("timeout") or 60.0))
        except (TypeError, ValueError):
            to_label = 60
        header = (
            f"\u2699 wait_for_workstream: {ws_count} ws (mode={mode_label}, timeout={to_label}s)"
        )
        return {
            "call_id": call_id,
            "func_name": "wait_for_workstream",
            "header": header,
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_wait_for_workstream,
            "ws_ids": raw_ids if isinstance(raw_ids, list) else [],
            "timeout": args.get("timeout"),
            "mode": args.get("mode"),
        }

    def _exec_wait_for_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        try:
            result = self._coord_client.wait_for_workstream(
                item["ws_ids"],
                timeout=item["timeout"] if item["timeout"] is not None else 60.0,
                mode=item["mode"] if item["mode"] is not None else "any",
            )
        except Exception as e:
            msg = f"Error: wait_for_workstream failed: {e}"
            self._report_tool_result(call_id, "wait_for_workstream", msg, is_error=True)
            return call_id, msg
        # Surface client-side validation errors as tool errors rather
        # than rendering them as a "successful" wait result.
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "wait_for_workstream", msg, is_error=True)
            return call_id, msg
        output = json.dumps(result, separators=(",", ":"), default=str)
        elapsed = result.get("elapsed", 0.0)
        complete = result.get("complete", False)
        # Count children that genuinely finished work (real terminals
        # only — ``denied`` is a rejection, not a resolution).  Earlier
        # versions counted any non-empty ``state`` and inverted the
        # truth on timeout (rendered as ``"timeout (N/N resolved)"``).
        # Inline import — ``turnstone.core`` shouldn't import from
        # ``turnstone.console`` at module load (layering), so the
        # tool-exec read pulls the canonical state set lazily.
        from turnstone.console.coordinator_client import CoordinatorClient

        results_dict = result.get("results") or {}
        resolved_count = sum(
            1
            for snap in results_dict.values()
            if isinstance(snap, dict)
            and snap.get("state") in CoordinatorClient._WAIT_REAL_TERMINAL_STATES
        )
        verb = "complete" if complete else "timeout"
        # Denominator = polled set (not raw item['ws_ids']) so the ratio
        # stays coherent with what the client actually tracked after dedup.
        summary = f"{verb} after {elapsed}s ({resolved_count}/{len(results_dict)} resolved)"
        self._report_tool_result(call_id, "wait_for_workstream", summary)
        return call_id, self._truncate_output(output)

    def _prepare_memory(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a memory tool action (save/get/search/delete/list)."""
        action = (args.get("action") or "").strip().lower()

        if action == "save":
            name = (args.get("name") or args.get("key") or "").strip()
            content = (args.get("content") or args.get("value") or "").strip()
            name = normalize_key(name)
            if not name:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory save: missing name",
                    "preview": "",
                    "needs_approval": False,
                    "error": "Error: 'name' is required for save",
                }
            if not content:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory save: missing content",
                    "preview": "",
                    "needs_approval": False,
                    "error": "Error: 'content' must be non-empty for save",
                }
            if len(content) > self._mem_cfg.max_content:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory save: content too large",
                    "preview": "",
                    "needs_approval": False,
                    "error": f"Error: content exceeds {self._mem_cfg.max_content} character limit",
                }
            description = (args.get("description") or "").strip()
            mem_type = (args.get("type") or "project").strip().lower()
            if mem_type not in ("user", "project", "feedback", "reference"):
                mem_type = "project"
            scope = (args.get("scope") or "global").strip().lower()
            if scope not in ("global", "workstream", "user"):
                scope = "global"
            scope_err = self._validate_scope(scope, call_id)
            if scope_err:
                return scope_err
            scope_id = self._resolve_scope_id(scope)
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": f"\u2699 memory save: {name}",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_memory,
                "action": "save",
                "name": name,
                "content": content,
                "description": description,
                "mem_type": mem_type,
                "scope": scope,
                "scope_id": scope_id,
            }

        if action == "get":
            name = normalize_key((args.get("name") or args.get("key") or "").strip())
            if not name:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory get: missing name",
                    "preview": "",
                    "needs_approval": False,
                    "error": "Error: 'name' is required for get",
                }
            explicit_scope = (args.get("scope") or "").strip().lower()
            valid_scopes = ("global", "workstream", "user")
            if explicit_scope and explicit_scope not in valid_scopes:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory get: invalid scope",
                    "preview": "",
                    "needs_approval": False,
                    "error": f"Error: invalid scope '{explicit_scope}'. Valid: {', '.join(valid_scopes)}",
                }
            if explicit_scope:
                scope_err = self._validate_scope(explicit_scope, call_id)
                if scope_err:
                    return scope_err
                scopes_to_try = [(explicit_scope, self._resolve_scope_id(explicit_scope))]
            else:
                scopes_to_try = []
                for s in ("workstream", "user", "global"):
                    sid = self._resolve_scope_id(s)
                    if sid or s == "global":
                        scopes_to_try.append((s, sid))
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": f"\u2699 memory get: {name}",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_memory,
                "action": "get",
                "name": name,
                "scopes_to_try": scopes_to_try,
            }

        if action == "delete":
            name = normalize_key((args.get("name") or args.get("key") or "").strip())
            if not name:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory delete: empty name",
                    "preview": "",
                    "needs_approval": False,
                    "error": "Error: name is required for delete",
                }
            explicit_scope = (args.get("scope") or "").strip().lower()
            valid_scopes = ("global", "workstream", "user")
            if explicit_scope and explicit_scope not in valid_scopes:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory delete: invalid scope",
                    "preview": "",
                    "needs_approval": False,
                    "error": (
                        f"Error: invalid scope '{explicit_scope}'. "
                        f"Valid scopes: {', '.join(valid_scopes)}"
                    ),
                }
            if explicit_scope:
                scope_err = self._validate_scope(explicit_scope, call_id)
                if scope_err:
                    return scope_err
                scope_id = self._resolve_scope_id(explicit_scope)
                scopes_to_try = [(explicit_scope, scope_id)]
            else:
                # No scope specified — try narrowest first: workstream → user → global
                scopes_to_try = []
                for s in ("workstream", "user", "global"):
                    sid = self._resolve_scope_id(s)
                    if sid or s == "global":
                        scopes_to_try.append((s, sid))
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": f"\u2699 memory delete: {name}",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_memory,
                "action": "delete",
                "name": name,
                "scopes_to_try": scopes_to_try,
            }

        if action == "search":
            query = (args.get("query") or "").strip()
            mem_type = (args.get("type") or "").strip().lower()
            if mem_type and mem_type not in ("user", "project", "feedback", "reference"):
                mem_type = ""
            scope = (args.get("scope") or "").strip().lower()
            if scope and scope not in ("global", "workstream", "user"):
                scope = ""
            if scope:
                scope_err = self._validate_scope(scope, call_id)
                if scope_err:
                    return scope_err
            scope_id = self._resolve_scope_id(scope) if scope else ""
            limit = args.get("limit", 20)
            if isinstance(limit, str):
                try:
                    limit = int(limit)
                except ValueError:
                    limit = 20
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": f"\u2699 memory search{': ' + query[:80] if query else ''}",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_memory,
                "action": "search",
                "query": query,
                "mem_type": mem_type,
                "scope": scope,
                "scope_id": scope_id,
                "limit": max(1, min(limit, 50)),
            }

        if action == "list":
            mem_type = (args.get("type") or "").strip().lower()
            if mem_type and mem_type not in ("user", "project", "feedback", "reference"):
                mem_type = ""
            scope = (args.get("scope") or "").strip().lower()
            if scope and scope not in ("global", "workstream", "user"):
                scope = ""
            if scope:
                scope_err = self._validate_scope(scope, call_id)
                if scope_err:
                    return scope_err
            scope_id = self._resolve_scope_id(scope) if scope else ""
            limit = args.get("limit", 20)
            if isinstance(limit, str):
                try:
                    limit = int(limit)
                except ValueError:
                    limit = 20
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": "\u2699 memory list",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_memory,
                "action": "list",
                "mem_type": mem_type,
                "scope": scope,
                "scope_id": scope_id,
                "limit": max(1, min(limit, 50)),
            }

        return {
            "call_id": call_id,
            "func_name": "memory",
            "header": "\u2717 memory: invalid action",
            "preview": "",
            "needs_approval": False,
            "error": f"Error: action must be save/get/search/delete/list, got '{action}'",
        }

    def _prepare_recall(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a conversation history search."""
        query = (args.get("query") or "").strip()
        if not query:
            return {
                "call_id": call_id,
                "func_name": "recall",
                "header": "\u2717 recall: requires query",
                "preview": "",
                "needs_approval": False,
                "error": "Error: query is required",
            }
        try:
            limit = int(args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = int(args.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        return {
            "call_id": call_id,
            "func_name": "recall",
            "header": f"\u2699 recall: {query[:80]}",
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_recall,
            "query": query,
            "limit": max(1, min(limit, 50)),
            "offset": max(0, offset),
        }

    # -- skill prepare/execute -------------------------------------------------

    def _prepare_skill(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a skill action (load or search)."""
        action = (args.get("action") or "").strip().lower()

        if action == "load":
            name = (args.get("name") or "").strip()
            if not name:
                return {
                    "call_id": call_id,
                    "func_name": "skill",
                    "header": "\u2717 skill: name is required",
                    "preview": "",
                    "needs_approval": False,
                    "error": "Error: 'name' is required for load action",
                }
            return {
                "call_id": call_id,
                "func_name": "skill",
                "header": f"\u2699 skill: {name}",
                "preview": "",
                "needs_approval": True,
                "approval_label": f"skill__{name}",
                "execute": self._exec_skill,
                "action": "load",
                "name": name,
            }

        if action == "search":
            query = (args.get("query") or "").strip()
            return {
                "call_id": call_id,
                "func_name": "skill",
                "header": f"\u2699 skill search{': ' + query[:80] if query else ''}",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_skill,
                "action": "search",
                "query": query,
            }

        return {
            "call_id": call_id,
            "func_name": "skill",
            "header": "\u2717 skill: invalid action",
            "preview": "",
            "needs_approval": False,
            "error": f"Error: action must be 'load' or 'search', got '{action}'",
        }

    def _exec_skill(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute a skill action."""
        call_id = item["call_id"]
        action = item["action"]

        if action == "load":
            name = item["name"]
            skill_data = get_skill_by_name(name)
            if not skill_data or not skill_data.get("enabled", True):
                msg = f"Error: skill '{name}' not found"
                self._report_tool_result(call_id, "skill", msg, is_error=True)
                return call_id, msg

            if self._skill_name == name:
                msg = f"Skill '{name}' is already active"
                self._report_tool_result(call_id, "skill", msg)
                return call_id, msg

            self.set_skill(name)

            desc = skill_data.get("description", "")
            scan = skill_data.get("scan_status", "")
            parts = [f"Loaded skill '{name}'"]
            if desc:
                parts.append(f"Description: {desc}")
            if scan:
                parts.append(f"Security tier: {scan}")
            msg = "\n".join(parts)
            self._report_tool_result(call_id, "skill", msg)
            return call_id, msg

        # action == "search"
        query = item.get("query", "")
        try:
            from turnstone.core.storage._registry import get_storage

            rows = get_storage().list_prompt_templates(limit=50)
        except Exception:
            log.warning("skill.search_storage_error", exc_info=True)
            rows = []

        # Filter out disabled skills
        rows = [r for r in rows if r.get("enabled", True)]

        if query:
            import json as _json

            from turnstone.core.bm25 import BM25Index

            def _tags_text(raw: str) -> str:
                """Parse JSON tags string into space-separated text."""
                try:
                    parsed = _json.loads(raw)
                    if isinstance(parsed, list):
                        return " ".join(str(t) for t in parsed)
                except (ValueError, TypeError):
                    pass  # falls back to raw string
                return raw

            # Build corpus from name + description + tags + category
            corpus = [
                " ".join(
                    filter(
                        None,
                        [
                            r.get("name", ""),
                            r.get("description", ""),
                            _tags_text(r.get("tags", "[]")),
                            r.get("category", ""),
                        ],
                    )
                )
                for r in rows
            ]
            index = BM25Index(corpus)
            top_indices = index.search(query, k=10)
            rows = [rows[i] for i in top_indices]
        else:
            rows = rows[:10]

        if not rows:
            msg = "No skills found" + (f" matching '{query}'" if query else "")
            self._report_tool_result(call_id, "skill", msg)
            return call_id, msg

        lines = [f"Found {len(rows)} skill(s):", ""]
        for r in rows:
            name_val = r.get("name", "")
            desc_val = r.get("description", "")
            cat_val = r.get("category", "")
            scan_val = r.get("scan_status", "")
            activation = r.get("activation", "named")
            line = f"- {name_val}"
            if cat_val:
                line += f" [{cat_val}]"
            if scan_val:
                line += f" ({scan_val})"
            if activation != "named":
                line += f" activation={activation}"
            if desc_val:
                line += f" — {desc_val[:120]}"
            lines.append(line)

        msg = "\n".join(lines)
        self._report_tool_result(call_id, "skill", msg)
        return call_id, msg

    # -- MCP tool prepare/execute ----------------------------------------------

    def _prepare_mcp_tool(
        self, call_id: str, func_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Prepare an MCP tool call for approval."""
        # Parse prefixed name for display: mcp__github__search → github/search
        parts = func_name.split("__", 2)
        display = f"{parts[1]}/{parts[2]}" if len(parts) == 3 else func_name

        preview_lines = []
        for key, val in args.items():
            val_str = str(val)
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            preview_lines.append(f"    {key}: {val_str}")
        preview = "\n".join(preview_lines) if preview_lines else "    (no arguments)"

        return {
            "call_id": call_id,
            "func_name": func_name,
            "header": f"\u2699 mcp:{display}",
            "preview": preview,
            "needs_approval": True,
            "approval_label": func_name,
            "execute": self._exec_mcp_tool,
            "mcp_func_name": func_name,
            "mcp_args": args,
        }

    def _exec_mcp_tool(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute an MCP tool call via the MCPClientManager."""
        self._check_cancelled()
        call_id: str = item["call_id"]
        func_name: str = item["mcp_func_name"]
        args: dict[str, Any] = item["mcp_args"]

        assert self._mcp_client is not None
        mcp_error = False
        try:
            output = self._mcp_client.call_tool_sync(func_name, args, timeout=self.tool_timeout)
        except TimeoutError:
            output = f"MCP tool timed out after {self.tool_timeout}s"
            mcp_error = True
            self.ui.on_error(output)
        except Exception as e:
            output = f"MCP tool error: {e}"
            mcp_error = True
            self.ui.on_error(output)

        output = self._truncate_output(output)
        self._report_tool_result(call_id, func_name, output, is_error=mcp_error)
        return call_id, output

    @staticmethod
    def _normalize_resource_uri(uri: str) -> str:
        """Normalize a resource URI for policy matching.

        Decodes percent-encoded path segments (e.g. ``%2e%2e`` → ``..``)
        then resolves ``..`` to prevent traversal bypasses where
        ``file:///docs/%2e%2e/etc/passwd`` would match a policy
        allowing ``mcp_resource__file:///docs/*``.
        """
        import posixpath
        from urllib.parse import quote, unquote, urlparse, urlunparse

        parsed = urlparse(uri)
        if parsed.path:
            decoded = unquote(parsed.path)
            normalized = posixpath.normpath(decoded)
            if parsed.path.startswith("/") and not normalized.startswith("/"):
                normalized = "/" + normalized
            parsed = parsed._replace(path=quote(normalized, safe="/"))
        return urlunparse(parsed)

    def _prepare_read_resource(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare an MCP resource read."""
        uri = args.get("uri", "")
        if not uri:
            return {
                "call_id": call_id,
                "func_name": "read_resource",
                "header": "\u2717 read_resource: missing uri",
                "preview": "",
                "needs_approval": False,
                "error": "Missing required parameter: uri",
            }
        if not self._mcp_client:
            return {
                "call_id": call_id,
                "func_name": "read_resource",
                "header": "\u2717 read_resource: no MCP servers",
                "preview": "",
                "needs_approval": False,
                "error": "No MCP servers configured",
            }
        return {
            "call_id": call_id,
            "func_name": "read_resource",
            "header": "\u2699 read_resource",
            "preview": f"    uri: {uri}",
            "needs_approval": True,
            "approval_label": f"mcp_resource__{self._normalize_resource_uri(uri)}",
            "execute": self._exec_read_resource,
            "resource_uri": uri,
        }

    def _exec_read_resource(self, item: dict[str, Any]) -> tuple[str, str]:
        """Read an MCP resource by URI."""
        self._check_cancelled()
        call_id: str = item["call_id"]
        uri: str = item["resource_uri"]

        assert self._mcp_client is not None
        mcp_error = False
        try:
            output = self._mcp_client.read_resource_sync(uri, timeout=self.tool_timeout)
        except TimeoutError:
            output = f"MCP resource read timed out after {self.tool_timeout}s"
            mcp_error = True
            self.ui.on_error(output)
        except Exception:
            log.warning("MCP resource read failed for %s", uri, exc_info=True)
            output = "MCP resource error: failed to read resource"
            mcp_error = True
            self.ui.on_error(output)

        output = self._truncate_output(output)
        self._report_tool_result(call_id, "read_resource", output, is_error=mcp_error)
        return call_id, output

    def _prepare_use_prompt(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare an MCP prompt invocation."""
        name = args.get("name", "")
        if not name:
            return {
                "call_id": call_id,
                "func_name": "use_prompt",
                "header": "\u2717 use_prompt: missing name",
                "preview": "",
                "needs_approval": False,
                "error": "Missing required parameter: name",
            }
        if not self._mcp_client:
            return {
                "call_id": call_id,
                "func_name": "use_prompt",
                "header": "\u2717 use_prompt: no MCP servers",
                "preview": "",
                "needs_approval": False,
                "error": "No MCP servers configured",
            }
        if not self._mcp_client.is_mcp_prompt(name):
            return {
                "call_id": call_id,
                "func_name": "use_prompt",
                "header": f"\u2717 use_prompt: unknown prompt '{name}'",
                "preview": "",
                "needs_approval": False,
                "error": f"Unknown MCP prompt: {name}",
            }
        raw_arguments = args.get("arguments") or {}
        if not isinstance(raw_arguments, dict):
            return {
                "call_id": call_id,
                "func_name": "use_prompt",
                "header": "\u2717 use_prompt: arguments must be an object",
                "preview": "",
                "needs_approval": False,
                "error": "arguments must be a JSON object with string values",
            }
        arguments = {str(k): str(v) for k, v in raw_arguments.items()}
        preview_parts = [f"    {DIM}name: {name}"]
        if arguments:
            preview_parts.append(f"    arguments: {arguments}")
        preview_parts.append(RESET)
        return {
            "call_id": call_id,
            "func_name": "use_prompt",
            "header": "\u2699 use_prompt",
            "preview": "\n".join(preview_parts),
            "needs_approval": True,
            "approval_label": name,
            "execute": self._exec_use_prompt,
            "prompt_name": name,
            "prompt_arguments": arguments,
        }

    def _exec_use_prompt(self, item: dict[str, Any]) -> tuple[str, str]:
        """Invoke an MCP prompt and return expanded messages."""
        self._check_cancelled()
        call_id: str = item["call_id"]
        name: str = item["prompt_name"]
        arguments: dict[str, str] = item["prompt_arguments"]

        assert self._mcp_client is not None
        mcp_error = False
        try:
            messages = self._mcp_client.get_prompt_sync(
                name, arguments or None, timeout=self.tool_timeout
            )
            output = "\n\n".join(f"[{m['role']}]: {m['content']}" for m in messages)
        except TimeoutError:
            output = f"MCP prompt timed out after {self.tool_timeout}s"
            mcp_error = True
            self.ui.on_error(output)
        except Exception:
            log.warning("MCP prompt invocation failed for %s", name, exc_info=True)
            output = "MCP prompt error: failed to invoke prompt"
            mcp_error = True
            self.ui.on_error(output)

        output = self._truncate_output(output)
        self._report_tool_result(call_id, "use_prompt", output, is_error=mcp_error)
        return call_id, output

    # -- Execute methods (do the work, report output via UI) -------------------

    def _exec_bash(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute a bash command via temp script, streaming stdout."""
        self._check_cancelled()
        # Capture cancel event locally so force-cancel (which replaces
        # _cancel_event with a fresh instance) doesn't disarm this check.
        cancel = self._cancel_event
        call_id, command = item["call_id"], item["command"]
        timeout = item.get("timeout") or self.tool_timeout
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
                preamble = "set -o pipefail\n"
                if item.get("stop_on_error"):
                    preamble += "set -e\n"
                f.write(preamble + command)
                script_path = f.name
            try:
                from turnstone.core.env import scrubbed_env

                proc = subprocess.Popen(
                    ["bash", script_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    env=scrubbed_env(extra=self._skill_resource_env()),
                )
                with self._procs_lock:
                    self._active_procs.add(proc)
                # Drain stderr in background thread to avoid pipe deadlock
                stderr_lines: list[str] = []

                def drain_stderr() -> None:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        stderr_lines.append(line)

                stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
                stderr_thread.start()

                # Stream stdout line-by-line with process-group timeout
                stdout_parts: list[str] = []
                timed_out = threading.Event()

                def _on_timeout() -> None:
                    if proc.poll() is not None:
                        return  # process already exited
                    timed_out.set()
                    with contextlib.suppress(OSError, ProcessLookupError):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except OSError:
                            with contextlib.suppress(OSError, ProcessLookupError):
                                proc.kill()

                timer = threading.Timer(timeout, _on_timeout)
                timer.start()
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        stdout_parts.append(line)
                        try:
                            self.ui.on_tool_output_chunk(call_id, line)
                        except Exception:
                            log.debug("UI callback error during tool output", exc_info=True)
                        # Check cancellation during long-running commands
                        if cancel.is_set():
                            with contextlib.suppress(OSError, ProcessLookupError):
                                try:
                                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                                except OSError:
                                    with contextlib.suppress(OSError, ProcessLookupError):
                                        proc.kill()
                            raise GenerationCancelled()
                finally:
                    timer.cancel()

                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log.warning("Process did not exit after SIGKILL, pid=%d", proc.pid)
                stderr_thread.join(timeout=5)
            finally:
                with self._procs_lock:
                    self._active_procs.discard(proc)
                os.unlink(script_path)

            if timed_out.is_set():
                raise subprocess.TimeoutExpired(cmd="bash", timeout=timeout)

            # Distinguish user cancel from unexpected SIGKILL.
            # Popen.returncode is negative of the signal number when killed.
            if cancel.is_set() and proc.returncode == -signal.SIGKILL:
                msg = "Cancelled by user."
                self._report_tool_result(call_id, "bash", msg)
                return call_id, msg

            output = "".join(stdout_parts)
            if stderr_lines:
                tagged = "".join(f"[stderr] {line}" for line in stderr_lines)
                output += ("\n" if output else "") + tagged
            output = output.strip()
            output = self._truncate_output(output)

            # With stop_on_error, any non-zero exit is a real failure (set -e
            # killed the script).  Without it, exit code 1 is often benign
            # (e.g. grep no-match).
            if item.get("stop_on_error"):
                bash_error = proc.returncode != 0
            else:
                bash_error = proc.returncode not in (0, 1)
            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"

            self._report_tool_result(call_id, "bash", output, is_error=bash_error)

            return call_id, output if output else "(no output)"

        except subprocess.TimeoutExpired:
            msg = f"Command timed out after {timeout}s"
            self._report_tool_result(call_id, "bash", msg, is_error=True)
            return call_id, msg
        except Exception as e:
            msg = f"Error executing command: {e}"
            self._report_tool_result(call_id, "bash", msg, is_error=True)
            return call_id, msg

    @staticmethod
    def _read_text_lines(path: str) -> tuple[list[str], str, str | None]:
        """Read a text file with binary detection and symlink resolution.

        Returns (lines, resolved_path, error_msg).  On success error_msg is
        None.  On failure lines is empty and error_msg describes the problem.
        """
        resolved = os.path.realpath(os.path.expanduser(path))
        try:
            with open(resolved, "rb") as fb:
                sample = fb.read(8192)
            if b"\x00" in sample:
                return (
                    [],
                    resolved,
                    (
                        f"Error: {path} appears to be a binary file "
                        "(contains null bytes). Use bash to inspect binary files."
                    ),
                )
            with open(resolved) as f:
                return f.readlines(), resolved, None
        except FileNotFoundError:
            return [], resolved, f"Error: {path} not found"
        except Exception as e:
            return [], resolved, f"Error reading {path}: {e}"

    def _exec_read_file(self, item: dict[str, Any]) -> tuple[str, str | list[dict[str, Any]]]:
        """Read a file and return numbered lines, or image content parts."""
        call_id, path = item["call_id"], item["path"]
        offset = item.get("offset")  # 1-based, or None
        limit = item.get("limit")  # max lines, or None
        resolved = os.path.realpath(path)

        # Image file detection (branch before text open)
        ext = os.path.splitext(path)[1].lower()
        if ext in _IMAGE_EXTENSIONS:
            return self._exec_read_image(call_id, path, resolved)

        all_lines, _, err = self._read_text_lines(path)
        if err:
            self._read_files.discard(resolved)
            self._report_tool_result(call_id, "read_file", err, is_error=True)
            return call_id, err

        self._read_files.add(resolved)
        total_lines = len(all_lines)

        # Slice if offset/limit specified
        start = max(1, offset or 1)
        if limit is not None:
            lines = all_lines[start - 1 : start - 1 + limit]
        else:
            lines = all_lines[start - 1 :]

        numbered = []
        for i, line in enumerate(lines, start=start):
            numbered.append(f"{i:>4}\t{line.rstrip()}")
        output = "\n".join(numbered)
        output = self._truncate_output(output)

        desc = f"{len(lines)} lines"
        if offset is not None or limit is not None:
            end = start + len(lines) - 1
            desc += f" (lines {start}-{end} of {total_lines})"
        self._report_tool_result(call_id, "read_file", desc)

        return call_id, output if output else "(empty file)"

    def _exec_read_image(
        self, call_id: str, path: str, resolved: str
    ) -> tuple[str, str | list[dict[str, Any]]]:
        """Read an image file and return as base64 content parts for vision."""
        caps = self._get_capabilities()
        if not caps.supports_vision:
            try:
                size = os.path.getsize(resolved)
            except OSError as e:
                self._read_files.discard(resolved)
                msg = f"Error: {path}: {e}"
                self._report_tool_result(call_id, "read_file", msg, is_error=True)
                return call_id, msg
            self._read_files.add(resolved)
            desc = f"image (no vision, {size:,} bytes)"
            self._report_tool_result(call_id, "read_file", desc)
            return call_id, (
                f"Binary image file: {path} ({size:,} bytes). "
                "Current model does not support vision."
            )

        try:
            with open(resolved, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            self._read_files.discard(resolved)
            msg = f"Error: {path} not found"
            self._report_tool_result(call_id, "read_file", msg, is_error=True)
            return call_id, msg
        except Exception as e:
            self._read_files.discard(resolved)
            msg = f"Error reading {path}: {e}"
            self._report_tool_result(call_id, "read_file", msg, is_error=True)
            return call_id, msg

        if len(raw) > _IMAGE_SIZE_CAP:
            self._read_files.discard(resolved)
            size_mb = len(raw) / (1024 * 1024)
            cap_mb = _IMAGE_SIZE_CAP / (1024 * 1024)
            msg = (
                f"Error: image {path} is {size_mb:.1f} MB, "
                f"exceeds {cap_mb:.0f} MB limit for vision."
            )
            self._report_tool_result(call_id, "read_file", msg, is_error=True)
            return call_id, msg

        self._read_files.add(resolved)
        mime, _ = mimetypes.guess_type(path)
        if not mime:
            mime = "image/png"

        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": f"Image file: {path} ({len(raw):,} bytes)"},
            {"type": "image_url", "image_url": {"url": _encode_image_data_uri(raw, mime)}},
        ]

        self._report_tool_result(call_id, "read_file", f"image ({len(raw):,} bytes)")
        return call_id, content_parts

    def _exec_search(self, item: dict[str, Any]) -> tuple[str, str]:
        """Search file contents for a regex pattern using grep."""
        call_id = item["call_id"]
        pattern, path = item["pattern"], item["path"]
        try:
            from turnstone.core.env import scrubbed_env

            result = subprocess.run(
                [
                    "grep",
                    "-rn",
                    "-I",
                    "-E",
                    "-m",
                    "200",  # max matches per file
                    "--color=never",  # no ANSI codes in output
                    # Skip common build/vendor/VCS directories
                    "--exclude-dir=.git",
                    "--exclude-dir=node_modules",
                    "--exclude-dir=target",
                    "--exclude-dir=__pycache__",
                    "--exclude-dir=.mypy_cache",
                    "--exclude-dir=.ruff_cache",
                    "--exclude-dir=.pytest_cache",
                    "--exclude-dir=dist",
                    "--exclude-dir=build",
                    "--exclude-dir=*.egg-info",
                    "--exclude-dir=.tox",
                    "--exclude-dir=.venv",
                    "--exclude-dir=venv",
                    "--exclude-dir=vendor",
                    "--",
                    pattern,
                    path,  # -- prevents pattern as flag
                ],
                capture_output=True,
                text=True,
                timeout=self.tool_timeout,
                env=scrubbed_env(),
            )
            output = result.stdout.strip()
            if result.returncode == 1:
                output = "(no matches)"
            elif result.returncode > 1:
                output = result.stderr.strip() or f"grep error (exit {result.returncode})"

            # Count matches and files BEFORE truncation
            match_count = output.count("\n") + 1 if result.returncode == 0 and output else 0
            if match_count:
                files = {line.split(":", 1)[0] for line in output.splitlines() if ":" in line}
                file_count = len(files)
            else:
                file_count = 0

            # Append summary footer before truncation so it counts toward the limit
            original_len = len(output)
            if match_count:
                output += f"\n\n({match_count} matches across {file_count} files)"
            output = self._truncate_output(output)

            desc = f"{match_count} matches" if match_count else "no matches"
            if original_len > 500:
                desc += f" ({original_len} chars)"
            self._report_tool_result(call_id, "search", desc)

            return call_id, output

        except subprocess.TimeoutExpired:
            msg = f"Search timed out after {self.tool_timeout}s"
            self._report_tool_result(call_id, "search", msg, is_error=True)
            return call_id, msg
        except Exception as e:
            msg = f"Error: search failed: {e}"
            self._report_tool_result(call_id, "search", msg, is_error=True)
            return call_id, msg

    def _exec_diff(self, item: dict[str, Any]) -> tuple[str, str]:
        """Show unified diff between two files or a file and provided content."""
        call_id = item["call_id"]
        path_a = item["path_a"]
        path_b = item.get("path_b", "")
        content_b = item.get("content_b")
        ctx = item.get("context_lines", 3)

        lines_a, resolved_a, err = self._read_text_lines(path_a)
        if err:
            self._report_tool_result(call_id, "diff_file", err, is_error=True)
            return call_id, err
        self._read_files.add(resolved_a)

        if path_b:
            label_b = path_b
            lines_b, resolved_b, err = self._read_text_lines(path_b)
            if err:
                self._report_tool_result(call_id, "diff_file", err, is_error=True)
                return call_id, err
            self._read_files.add(resolved_b)
        else:
            label_b = "(provided content)"
            lines_b = (content_b or "").splitlines(keepends=True)

        # When content_b is a baseline, swap so diff reads as "what changed"
        # (--- old/baseline, +++ new/current file).
        if content_b is not None:
            lines_a, lines_b = lines_b, lines_a
            path_a, label_b = label_b, path_a

        # Stream diff with early cutoff to avoid large allocations
        max_chars = self.tool_truncation or 262_144
        chunks: list[str] = []
        total_chars = 0
        line_count = 0
        for line in difflib.unified_diff(lines_a, lines_b, fromfile=path_a, tofile=label_b, n=ctx):
            line_count += 1
            if total_chars < max_chars:
                chunks.append(line)
                total_chars += len(line)
        output = "".join(chunks) if chunks else "(no differences)"
        output = self._truncate_output(output)
        desc = f"{line_count} diff lines" if line_count else "identical"
        self._report_tool_result(call_id, "diff_file", desc)
        return call_id, output

    def _run_agent(
        self,
        agent_messages: list[dict[str, Any]],
        label: str = "agent",
        tools: list[dict[str, Any]] | None = None,
        auto_tools: set[str] | None = None,
        reasoning_effort: str | None = None,
        agent_alias: str | None = None,
    ) -> str:
        """Run an autonomous agent loop.

        Args:
            agent_messages: Pre-built message list (system + developer + user).
            label: Display prefix for progress lines ("agent" or "plan").
            tools: Tool definitions to send to the API. Defaults to AGENT_TOOLS (read-only).
            auto_tools: Set of tool names the agent may execute. Defaults to AGENT_AUTO_TOOLS.
            reasoning_effort: Override reasoning effort for this agent.
            agent_alias: Per-call model alias override (the LLM passed
                ``model="<alias>"`` to plan_agent/task_agent).  Wins over
                the registry's per-kind resolution when set.  Caller is
                expected to have validated the alias against the registry;
                an unknown alias here raises ``ValueError``.

        Returns:
            Final content string from the agent.
        """
        if tools is None:
            tools = self._agent_tools
        if auto_tools is None:
            auto_tools = AGENT_AUTO_TOOLS
        max_tool_turns = self.agent_max_turns

        # Resolve agent model: explicit per-call override wins, then per-kind
        # registry override (plan_model/task_model), then the legacy single-
        # knob agent_model, then the session's primary model.
        if agent_alias is not None:
            if self._registry is None or not self._registry.has_alias(agent_alias):
                raise ValueError(f"Unknown agent_alias '{agent_alias}'")
        else:
            agent_alias = self._registry.resolve_agent_alias(label) if self._registry else None
        if self._registry and agent_alias:
            agent_client, agent_model, _ = self._registry.resolve(agent_alias)
            agent_provider = self._registry.get_provider(agent_alias)
        else:
            agent_client = self.client
            agent_model = self.model
            agent_provider = self._provider

        # Per-kind reasoning effort.  Explicit caller arg wins; otherwise
        # delegate to the registry which knows the per-kind default (plan
        # gets the back-compat "high", task returns None to inherit the
        # session).  When no registry exists, apply the plan back-compat
        # default directly so single-process callers keep prior behaviour.
        if reasoning_effort is None:
            if self._registry:
                reasoning_effort = self._registry.resolve_agent_effort(label)
            elif label == "plan":
                from turnstone.core.model_registry import ModelRegistry

                reasoning_effort = ModelRegistry.PLAN_DEFAULT_EFFORT

        # Gate web_search: remove when no backend exists for the agent model
        agent_caps = self._resolve_capabilities(agent_provider, agent_model, agent_alias)
        if not agent_caps.supports_web_search and not self._resolve_search_client():
            tools = _without_tool(tools, "web_search")

        # Build extra params for agent calls — resolve server compat from the
        # agent's own model alias, not the session's primary model.
        agent_extra = self._provider_extra_params(
            reasoning_effort=reasoning_effort,
            provider=agent_provider,
            model_alias=agent_alias,
        )

        def _api_call(
            messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]] | None = tools,
        ) -> CompletionResult:
            last_err: Exception | None = None
            for attempt in range(self._MAX_RETRIES + 1):
                try:
                    return agent_provider.create_completion(
                        client=agent_client,
                        model=agent_model,
                        messages=messages,
                        tools=_tools,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                        reasoning_effort=reasoning_effort or self.reasoning_effort,
                        extra_params=agent_extra,
                        capabilities=agent_caps,
                    )
                except Exception as e:
                    ename = type(e).__name__
                    if (
                        ename not in agent_provider.retryable_error_names
                        or attempt == self._MAX_RETRIES
                    ):
                        raise
                    last_err = e
                    delay = self._RETRY_BASE_DELAY * (2**attempt)
                    self.ui.on_info(f"[{label} retrying in {delay:.0f}s: {ename}]")
                    time.sleep(delay)
            assert last_err is not None  # unreachable
            raise last_err

        turn = 0
        while max_tool_turns < 0 or turn < max_tool_turns:
            self._check_cancelled()
            try:
                result = _api_call(agent_messages)
            except Exception as e:
                # Context-exceeded or other non-retryable API error.
                # Return what we have so far rather than crashing.
                err_str = str(e).lower()
                if "context" in err_str or "token" in err_str:
                    self.ui.on_info(f"[{label}] context limit reached, stopping early")
                    # Find the last assistant content we have
                    for msg in reversed(agent_messages):
                        if msg.get("role") == "assistant" and msg.get("content"):
                            return str(msg["content"])
                    return f"({label} stopped: context limit exceeded)"
                raise

            # Handle truncation or content filter — stop agent early
            if result.finish_reason == "length":
                self.ui.on_info(f"[{label}] response truncated, stopping early")
                return result.content or "(truncated)"
            if result.finish_reason == "content_filter":
                self.ui.on_info(f"[{label}] blocked by content filter")
                return "(content filter)"

            # Build message dict for agent history
            msg_dict: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or "",
            }
            if result.tool_calls:
                self._ensure_tool_call_ids(result.tool_calls)
                msg_dict["tool_calls"] = result.tool_calls
            agent_messages.append(msg_dict)

            if not result.tool_calls:
                content = result.content or "(no output)"
                self.ui.on_info(f"[{label} done] {len(content)} chars")
                return content

            # Execute tools sequentially (not parallel) to avoid
            # concurrent _read_files mutation from worker threads.
            tool_names = {t["function"]["name"] for t in tools}
            for tc_dict in result.tool_calls:
                self._check_cancelled()
                tool_name = tc_dict["function"]["name"].strip()

                # Guard 1: block recursive agent calls.
                if tool_name in ("task_agent", "plan_agent"):
                    output = "Error: agents cannot spawn further agents"
                # Guard 2: tool not in this agent's API tool list.
                elif tool_name not in tool_names:
                    output = (
                        f"Error: tool '{tool_name}' is not available in "
                        f"agent mode. "
                        f"Available: {', '.join(sorted(tool_names))}"
                    )
                else:
                    prepared = self._prepare_tool(tc_dict)

                    lbl = prepared.get("header", tool_name)
                    self.ui.on_info(f"[{label} turn {turn + 1}] {lbl}")

                    if prepared.get("error"):
                        output = prepared["error"]
                    # Auto-execute tools in the auto_tools set.
                    elif tool_name in auto_tools:
                        _, output = prepared["execute"](prepared)
                    # Tools not in auto_tools require user approval.
                    elif "execute" in prepared:
                        approved, _ = self.ui.approve_tools([prepared])
                        if not approved:
                            prepared["denied"] = True
                            prepared["denial_msg"] = "Denied by user"
                        if prepared.get("denied"):
                            output = prepared.get("denial_msg", "Denied by user")
                        else:
                            _, output = prepared["execute"](prepared)
                    else:
                        output = f"Unknown tool: {tool_name}"

                # Output guard: evaluate before truncation so the guard
                # sees full output (credentials split by truncation would
                # evade detection).  Agent outputs are always str.
                if self._judge_cfg and self._judge_cfg.output_guard and isinstance(output, str):
                    output, _ = self._evaluate_output(tc_dict["id"], output, tool_name)

                # Truncate large tool outputs to avoid blowing context limits.
                # Agents operate autonomously; they can refine their queries
                # if truncation loses important detail.
                if isinstance(output, str) and len(output) > 16000:
                    output = output[:16000] + f"\n\n... (truncated from {len(output)} chars)"

                agent_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_dict["id"],
                        "content": output,
                    }
                )
            turn += 1

        # Exhausted tool turns — force a final synthesis response.
        self.ui.on_info(f"[{label}] turn limit reached, requesting synthesis...")
        agent_messages.append(
            {
                "role": "user",
                "content": (
                    "You have reached the tool call limit. "
                    "Provide your complete response now using "
                    "the information you have gathered so far."
                ),
            }
        )
        result = _api_call(agent_messages, _tools=[])
        content = result.content or "(no output)"
        self.ui.on_info(f"[{label} done] {len(content)} chars")
        return content

    def _exec_task(self, item: dict[str, Any]) -> tuple[str, str]:
        """Delegate to a general-purpose autonomous sub-agent."""
        call_id, prompt = item["call_id"], item["prompt"]
        task_instruction = {
            "role": "system",
            "content": (
                "# Task Agent\n\n"
                "You are an autonomous task agent with full tool access. "
                "You can use bash, read_file, write_file, edit_file, search, "
                "math, web_fetch, and web_search.\n\n"
                "1. **Follow through on actions:** Do not describe changes — "
                "use the tools to make them. After read_file, call edit_file "
                "or write_file.\n\n"
                "2. **Tool selection:**\n"
                "   - Use read_file before edit_file on existing files.\n"
                "   - Use write_file for new files (not bash).\n"
                "   - Use bash for shell commands (git, python, tests).\n"
                "   - Use search to find code across files.\n\n"
                "3. **Complete the task fully.** Do not ask follow-up "
                "questions — execute the work as described in the prompt."
            ),
        }
        # Task agent gets the base system prompt (tool patterns) merged
        # with its own identity in a single system message. No conversation
        # history — it's an autonomous sub-agent. Merged to avoid
        # multi-system-message errors on models like Qwen.
        base = self._agent_system_messages[0]["content"] if self._agent_system_messages else ""
        agent_messages = [
            {"role": "system", "content": base + "\n\n" + task_instruction["content"]},
            {"role": "user", "content": prompt},
        ]
        try:
            return call_id, self._run_agent(
                agent_messages,
                label="task",
                tools=self._task_tools,
                auto_tools=TASK_AUTO_TOOLS,
                agent_alias=item.get("model_override"),
            )
        except (KeyboardInterrupt, GenerationCancelled):
            return call_id, "(task interrupted by user)"
        except Exception as e:
            self.ui.on_info(f"[task error] {e}")
            return call_id, f"Task error: {e}"

    _PLAN_IDENTITY = (
        "You are a planning agent. Explore the codebase with read_file and search, "
        "then write a plan with these sections: "
        "## Goal (1-2 sentences), "
        "## Current State (files/line numbers found), "
        "## Plan (numbered steps naming exact files and functions), "
        "## Risks (edge cases and unknowns). "
        "Never guess at structure — verify first. Be specific: name files, line numbers, "
        "and functions in every step."
    )

    def _plan_system_content(self) -> str:
        """Plan agent system message: skill guardrails + plan identity."""
        if not self._skill_content:
            return self._PLAN_IDENTITY
        tpl = self._skill_content
        if len(tpl) > _MAX_SKILL_CONTENT:
            log.warning("skill_content.truncated", length=len(tpl), agent="plan")
            tpl = tpl[:_MAX_SKILL_CONTENT]
        return tpl + "\n\n" + self._PLAN_IDENTITY

    _MIN_PLAN_LENGTH = 100
    _PLAN_REQUIRED_SECTIONS = ("## goal", "## current state", "## plan", "## risks")
    _MIN_PLAN_SECTIONS = 2
    _MAX_PLAN_REFINEMENTS = 5

    @staticmethod
    def _validate_plan(content: str, goal: str) -> tuple[bool, list[str]]:
        """Check if plan output meets minimum quality bar.

        Returns ``(valid, issues)`` where *issues* is a list of
        human-readable problem descriptions (empty when valid).
        """
        issues: list[str] = []
        stripped = content.strip()
        stripped_lower = stripped.lower()

        # 1. Minimum length
        if len(stripped) < ChatSession._MIN_PLAN_LENGTH:
            issues.append(
                f"too short ({len(stripped)} chars, minimum {ChatSession._MIN_PLAN_LENGTH})"
            )

        # 2. Section structure
        found_sections = sum(
            1 for section in ChatSession._PLAN_REQUIRED_SECTIONS if section in stripped_lower
        )
        if found_sections < ChatSession._MIN_PLAN_SECTIONS:
            issues.append(
                f"missing plan sections (found {found_sections}/"
                f"{len(ChatSession._PLAN_REQUIRED_SECTIONS)}, "
                f"need at least {ChatSession._MIN_PLAN_SECTIONS})"
            )

        # 3. Echo detection: plan is basically just the goal repeated
        goal_stripped = goal.strip().lower()
        if (
            goal_stripped
            and len(stripped) < len(goal_stripped) * 2
            and goal_stripped in stripped_lower
        ):
            issues.append("plan appears to echo the goal without elaboration")

        # 4. Refusal detection
        refusal_starts = (
            "i cannot",
            "i'm sorry",
            "i am sorry",
            "error:",
            "i can't",
        )
        if any(stripped_lower.startswith(r) for r in refusal_starts):
            issues.append("plan appears to be a refusal or error")

        return (len(issues) == 0, issues)

    def _exec_plan(self, item: dict[str, Any]) -> tuple[str, str]:
        """Run a planning agent and write the result to .plan-<ws_id>.md."""
        call_id, prompt = item["call_id"], item["prompt"]
        plan_path = f".plan-{self._ws_id}.md"

        # If plan was called before in this session, the previous assistant
        # tool_call + tool result are already in self.messages — pass them
        # directly to the inner agent so it refines rather than restarts.
        prior_plan_msgs: list[dict[str, Any]] = []
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == "plan_agent":
                        tc_id = tc["id"]
                        for j in range(i + 1, len(self.messages)):
                            if (
                                self.messages[j].get("role") == "tool"
                                and self.messages[j].get("tool_call_id") == tc_id
                            ):
                                prior_plan_msgs = [msg, self.messages[j]]
                                break

        # Plan agent gets template guardrails + its own identity — no tool
        # patterns, MCP resources, or general conversation history (only
        # prior plan tool_call/result pairs are forwarded for refinement).
        agent_messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._plan_system_content()},
        ]
        agent_messages.extend(prior_plan_msgs)
        agent_messages.append({"role": "user", "content": prompt})

        plan_alias = item.get("model_override")
        try:
            content = self._run_agent(
                agent_messages,
                label="plan",
                agent_alias=plan_alias,
            )
        except (KeyboardInterrupt, GenerationCancelled):
            return call_id, "(plan interrupted by user)"
        except Exception as e:
            self.ui.on_info(f"[plan error] {e}")
            return call_id, f"Plan error: {e}"

        # Validate plan quality — retry once with coaching on failure
        valid, issues = self._validate_plan(content, prompt)
        if not valid:
            self.ui.on_info(f"[plan] quality issues: {', '.join(issues)}")
            preview = content[:200] + ("..." if len(content) > 200 else "")
            coaching = (
                "Your previous response did not follow the required plan "
                "format. A valid plan should include at least two of "
                "these markdown sections:\n"
                "## Goal (1-2 sentences)\n"
                "## Current State (files/line numbers found)\n"
                "## Plan (numbered steps with file names and functions)\n"
                "## Risks (edge cases and unknowns)\n\n"
                f'Your previous response was: "{preview}"\n\n'
                "Please try again. Explore the codebase first, then write "
                "the plan."
            )
            agent_messages.append({"role": "user", "content": coaching})
            try:
                content = self._run_agent(
                    agent_messages,
                    label="plan",
                    agent_alias=plan_alias,
                )
            except (KeyboardInterrupt, GenerationCancelled):
                return call_id, "(plan interrupted by user)"
            except Exception as e:
                self.ui.on_info(f"[plan retry error] {e}")
                return call_id, f"Plan error: {e}"

            valid2, issues2 = self._validate_plan(content, prompt)
            if not valid2:
                self.ui.on_info(f"[plan] still has issues after retry: {', '.join(issues2)}")
                content = "[Warning: plan may be incomplete or poorly structured]\n\n" + content

        # Write to file separately — always return content even if write fails
        try:
            with open(plan_path, "w") as f:
                f.write(content)
            self.ui.on_info(f"Plan written to {plan_path}")
        except OSError as e:
            self.ui.on_info(f"[plan] could not write {plan_path}: {e}")

        return call_id, content

    def _refine_plan(
        self,
        original_content: str,
        original_goal: str,
        feedback: str,
    ) -> str:
        """Re-run the plan agent incorporating user feedback."""
        tc_id = f"plan_refine_{uuid.uuid4().hex[:8]}"
        agent_messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._plan_system_content()},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": "plan_agent",
                            "arguments": json.dumps({"goal": original_goal}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": original_content,
            },
            {
                "role": "user",
                "content": (
                    "The user reviewed this plan and provided feedback:\n\n"
                    f"{feedback}\n\n"
                    "Please revise the plan accordingly. Keep the same "
                    "format (## Goal, ## Current State, ## Plan, ## Risks) "
                    "and address the feedback."
                ),
            },
        ]

        self.ui.on_info("[plan] revising based on feedback...")
        content = self._run_agent(
            agent_messages,
            label="plan",
        )

        valid, issues = self._validate_plan(content, original_goal)
        if not valid:
            self.ui.on_info(f"[plan] revised plan has issues: {', '.join(issues)}")

        return content

    def _exec_memory(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute a memory tool action."""
        call_id = item["call_id"]
        action = item["action"]

        try:
            if action == "save":
                memory_id, old = save_structured_memory(
                    item["name"],
                    item["content"],
                    description=item["description"],
                    mem_type=item["mem_type"],
                    scope=item["scope"],
                    scope_id=item["scope_id"],
                )
                if not memory_id:
                    msg = f"Error: failed to save memory '{item['name']}'"
                    self._report_tool_result(call_id, "memory", msg, is_error=True)
                    return call_id, msg
                self._init_system_messages()
                if old is not None:
                    msg = f"Updated memory '{item['name']}' (type={item['mem_type']}, scope={item['scope']})"
                else:
                    msg = f"Saved memory '{item['name']}' (type={item['mem_type']}, scope={item['scope']})"
                self._report_tool_result(call_id, "memory", msg)
                return call_id, msg

            if action == "get":
                scopes = item["scopes_to_try"]
                mem = None
                found_scope = ""
                for scope, scope_id in scopes:
                    mem = get_structured_memory_by_name(item["name"], scope, scope_id)
                    if mem:
                        found_scope = scope
                        break
                if mem:
                    content = mem.get("content", "")
                    desc = mem.get("description", "")
                    mem_type = mem.get("type", "")
                    header = f"[{mem_type}:{found_scope}] {item['name']}"
                    if desc:
                        header += f" — {desc}"
                    msg = f"{header}\n\n{content}"
                else:
                    tried = ", ".join(s for s, _ in scopes)
                    msg = f"Error: memory '{item['name']}' not found (searched scopes: {tried})"
                self._report_tool_result(call_id, "memory", msg, is_error=mem is None)
                return call_id, msg

            if action == "delete":
                scopes = item["scopes_to_try"]
                deleted = False
                deleted_scope = ""
                for scope, scope_id in scopes:
                    if delete_structured_memory(item["name"], scope, scope_id):
                        deleted = True
                        deleted_scope = scope
                        break
                if not deleted:
                    tried = ", ".join(s for s, _ in scopes)
                    msg = f"Error: memory '{item['name']}' not found (searched scopes: {tried})"
                    self._report_tool_result(call_id, "memory", msg, is_error=True)
                else:
                    self._init_system_messages()
                    msg = f"Deleted memory '{item['name']}' (scope={deleted_scope})"
                    self._report_tool_result(call_id, "memory", msg)
                return call_id, msg

            if action == "search":
                scope = item.get("scope", "")
                scope_id = item.get("scope_id", "")
                # Defense-in-depth: reject scoped queries with empty scope_id
                if scope in ("user", "workstream") and not scope_id:
                    msg = f"Error: '{scope}' scope requires a valid identity"
                    self._report_tool_result(call_id, "memory", msg, is_error=True)
                    return call_id, msg
                if scope:
                    rows = search_structured_memories(
                        item["query"],
                        mem_type=item.get("mem_type", ""),
                        scope=scope,
                        scope_id=scope_id,
                        limit=item["limit"],
                    )
                else:
                    rows = self._search_visible_memories(
                        item["query"],
                        mem_type=item.get("mem_type", ""),
                        limit=item["limit"],
                    )
                if rows:
                    lines = []
                    for m in rows:
                        desc = f" — {m['description']}" if m.get("description") else ""
                        preview = m["content"][:200]
                        if len(m["content"]) > 200:
                            preview += "..."
                        lines.append(
                            f"  [{m['type']}:{m['scope']}] {m['name']}{desc}\n    {preview}"
                        )
                    msg = f"Memories ({len(rows)} results):\n" + "\n".join(lines)
                    msg += "\n\nUse memory(action='get', name='...') for full content."
                else:
                    msg = (
                        f"No memories found for '{item['query']}'."
                        if item["query"]
                        else "No memories stored."
                    )
                self._report_tool_result(call_id, "memory", msg)
                return call_id, msg

            if action == "list":
                scope = item.get("scope", "")
                scope_id = item.get("scope_id", "")
                if scope in ("user", "workstream") and not scope_id:
                    msg = f"Error: '{scope}' scope requires a valid identity"
                    self._report_tool_result(call_id, "memory", msg, is_error=True)
                    return call_id, msg
                if scope:
                    rows = list_structured_memories(
                        mem_type=item.get("mem_type", ""),
                        scope=scope,
                        scope_id=scope_id,
                        limit=item["limit"],
                    )
                else:
                    rows = self._list_visible_memories(
                        mem_type=item.get("mem_type", ""),
                        limit=item["limit"],
                    )
                if rows:
                    lines = []
                    for m in rows:
                        desc = f" — {m['description']}" if m.get("description") else ""
                        preview = m["content"][:200]
                        if len(m["content"]) > 200:
                            preview += "..."
                        lines.append(
                            f"  [{m['type']}:{m['scope']}] {m['name']}{desc}\n    {preview}"
                        )
                    msg = f"Memories ({len(rows)}):\n" + "\n".join(lines)
                    msg += "\n\nUse memory(action='get', name='...') for full content."
                else:
                    msg = "No memories stored."
                self._report_tool_result(call_id, "memory", msg)
                return call_id, msg

        except Exception as e:
            msg = f"Error: {e}"
            self._report_tool_result(call_id, "memory", msg, is_error=True)
            return call_id, msg

        msg = "Error: unexpected action"
        self._report_tool_result(call_id, "memory", msg, is_error=True)
        return call_id, msg

    def _exec_recall(self, item: dict[str, Any]) -> tuple[str, str]:
        """Search conversation history."""
        call_id = item["call_id"]
        query, limit, offset = item["query"], item["limit"], item.get("offset", 0)

        conv_rows = search_history(query, limit, offset)
        if conv_rows:
            lines = []
            for ts, sid, role, content, tool_name in conv_rows:
                label = f"{role}({tool_name})" if tool_name else role
                text = (content or "")[:2000]
                if content and len(content) > 2000:
                    text += f"... ({len(content)} chars total)"
                lines.append(f"[{ts} {sid}] {label}: {text}")
            header = f"Conversations ({len(conv_rows)} matches"
            if offset:
                header += f", offset {offset}"
            header += "):"
            output = header + "\n" + "\n".join(lines)
        else:
            output = f"No conversation history found for '{query}'."

        output = self._truncate_output(output)
        self._report_tool_result(call_id, "recall", output)
        return call_id, output

    # -- Notify tool -----------------------------------------------------------

    def _prepare_notify(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a channel notification."""
        message = (args.get("message") or "").strip()
        if not message:
            return {
                "call_id": call_id,
                "func_name": "notify",
                "header": "\u2717 notify: empty message",
                "preview": "",
                "needs_approval": False,
                "error": "Error: message is required",
            }
        if len(message) > 2000:
            return {
                "call_id": call_id,
                "func_name": "notify",
                "header": "\u2717 notify: message too long",
                "preview": "",
                "needs_approval": False,
                "error": "Error: message exceeds 2000 character limit",
            }

        username = (args.get("username") or "").strip()
        channel_type = (args.get("channel_type") or "").strip()
        channel_id = (args.get("channel_id") or "").strip()
        title = (args.get("title") or "").strip()

        has_username = bool(username)
        has_direct = bool(channel_type and channel_id)

        if has_username and has_direct:
            return {
                "call_id": call_id,
                "func_name": "notify",
                "header": "\u2717 notify: ambiguous target",
                "preview": "",
                "needs_approval": False,
                "error": "Error: provide either username or channel_type+channel_id, not both",
            }
        if channel_type and not channel_id:
            return {
                "call_id": call_id,
                "func_name": "notify",
                "header": "\u2717 notify: incomplete target",
                "preview": "",
                "needs_approval": False,
                "error": "Error: channel_id is required when channel_type is provided",
            }
        if channel_id and not channel_type:
            return {
                "call_id": call_id,
                "func_name": "notify",
                "header": "\u2717 notify: incomplete target",
                "preview": "",
                "needs_approval": False,
                "error": "Error: channel_type is required when channel_id is provided",
            }
        if not has_username and not has_direct:
            return {
                "call_id": call_id,
                "func_name": "notify",
                "header": "\u2717 notify: no target",
                "preview": "",
                "needs_approval": False,
                "error": "Error: provide username or channel_type+channel_id",
            }

        target_desc = f"@{username}" if has_username else f"{channel_type}:{channel_id}"

        preview = message[:120] + ("..." if len(message) > 120 else "")
        return {
            "call_id": call_id,
            "func_name": "notify",
            "header": f"\u2709 notify \u2192 {target_desc}",
            "preview": preview,
            "needs_approval": False,
            "execute": self._exec_notify,
            "message": message,
            "username": username,
            "channel_type": channel_type,
            "channel_id": channel_id,
            "title": title,
        }

    _NOTIFY_MAX_RETRIES = 2
    _NOTIFY_RETRY_DELAYS = (1.0, 3.0)

    def _exec_notify(self, item: dict[str, Any]) -> tuple[str, str]:
        """Send a notification directly to the channel gateway via HTTP."""
        self._check_cancelled()
        call_id = item["call_id"]

        if self._notify_count >= 5:
            msg = "Error: notification rate limit exceeded (max 5 per turn)"
            self._report_tool_result(call_id, "notify", msg, is_error=True)
            return call_id, msg

        target: dict[str, str] = {}
        if item.get("username"):
            target["username"] = item["username"]
        else:
            target["channel_type"] = item["channel_type"]
            target["channel_id"] = item["channel_id"]

        payload = {
            "target": target,
            "message": item["message"],
            "title": item.get("title", ""),
            "ws_id": self._ws_id,
        }

        # Build auth headers for service-to-service call
        auth_headers = _notify_auth_headers()

        # Retry loop: attempt delivery, re-query services on each retry
        # in case a gateway comes back online between attempts.
        for attempt in range(1 + self._NOTIFY_MAX_RETRIES):
            storage = get_storage()
            services = storage.list_services("channel", max_age_seconds=120)
            if not services:
                if attempt < self._NOTIFY_MAX_RETRIES:
                    delay = self._NOTIFY_RETRY_DELAYS[attempt]
                    log.warning(
                        "notify.no_services",
                        attempt=attempt + 1,
                        max_retries=self._NOTIFY_MAX_RETRIES,
                        retry_delay=delay,
                    )
                    time.sleep(delay)
                    continue
                log.warning("notify.no_services_exhausted")
                msg = "Error: no channel gateway services available"
                self._report_tool_result(call_id, "notify", msg, is_error=True)
                return call_id, msg

            # Try first healthy gateway, fall back to next
            last_error: str = ""
            for svc in services:
                url = svc["url"].rstrip("/") + "/v1/api/notify"
                # SSRF guard: only allow http(s) URLs
                if not url.startswith(("http://", "https://")):
                    continue
                try:
                    resp = httpx.post(url, json=payload, timeout=10, headers=auth_headers)
                    if resp.status_code < 300:
                        # Check that at least one target was actually delivered
                        try:
                            data = resp.json()
                        except Exception:
                            last_error = "invalid gateway response"
                            continue
                        results = data.get("results") if isinstance(data, dict) else None
                        if isinstance(results, list) and any(
                            isinstance(r, dict) and r.get("status") == "sent" for r in results
                        ):
                            self._notify_count += 1
                            msg = "Notification sent successfully"
                            self._report_tool_result(call_id, "notify", msg)
                            return call_id, msg
                        last_error = "no successful deliveries"
                        continue
                    last_error = f"HTTP {resp.status_code}"
                except Exception as exc:
                    last_error = type(exc).__name__
                    continue  # try next gateway

            # All gateways failed this attempt — retry if we have attempts left
            if attempt < self._NOTIFY_MAX_RETRIES:
                delay = self._NOTIFY_RETRY_DELAYS[attempt]
                log.warning(
                    "notify.all_gateways_failed",
                    attempt=attempt + 1,
                    max_retries=self._NOTIFY_MAX_RETRIES,
                    last_error=last_error,
                    gateway_count=len(services),
                    retry_delay=delay,
                )
                time.sleep(delay)
            else:
                log.warning(
                    "notify.delivery_failed",
                    last_error=last_error,
                    gateway_count=len(services),
                )

        msg = "Error: notification delivery failed"
        self._report_tool_result(call_id, "notify", msg, is_error=True)
        return call_id, msg

    # -- Watch tool ----------------------------------------------------------

    def _prepare_watch(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        from turnstone.core.watch import (
            MAX_INTERVAL,
            MAX_WATCHES_PER_WS,
            MIN_INTERVAL,
            parse_duration,
            validate_condition,
        )

        action = args.get("action", "")
        if action == "list":
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": "\u23f1 watch: list",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_watch,
                "action": "list",
            }
        if action == "cancel":
            name = args.get("name", "")
            if not name:
                return {
                    "call_id": call_id,
                    "func_name": "watch",
                    "header": "\u2717 watch cancel: missing name",
                    "preview": "",
                    "needs_approval": False,
                    "error": "Error: 'name' is required for cancel",
                }
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": f'\u23f1 watch: cancel "{name}"',
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_watch,
                "action": "cancel",
                "watch_name": name,
            }
        if action != "create":
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": f"\u2717 watch: unknown action '{action}'",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: unknown action '{action}'. Use create, list, or cancel.",
            }

        # --- action=create ---
        command = sanitize_command(args.get("command", ""))
        if not command:
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": "\u2717 watch create: missing command",
                "preview": "",
                "needs_approval": False,
                "error": "Error: 'command' is required for create",
            }
        blocked = is_command_blocked(command)
        if blocked:
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": f"\u2717 {blocked}",
                "preview": "",
                "needs_approval": False,
                "error": blocked,
            }

        # Parse poll interval
        poll_every_str = args.get("poll_every", "5m")
        try:
            interval_secs = parse_duration(poll_every_str)
        except ValueError as exc:
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": f"\u2717 watch: invalid poll_every: {exc}",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: invalid poll_every: {exc}",
            }
        if interval_secs < MIN_INTERVAL:
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": f"\u2717 watch: interval too short (min {MIN_INTERVAL}s)",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: minimum poll interval is {MIN_INTERVAL}s",
            }
        if interval_secs > MAX_INTERVAL:
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": f"\u2717 watch: interval too long (max {MAX_INTERVAL}s)",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: maximum poll interval is {MAX_INTERVAL}s",
            }

        # Validate stop condition
        stop_on = args.get("stop_on")
        if stop_on is not None:
            err = validate_condition(stop_on)
            if err:
                return {
                    "call_id": call_id,
                    "func_name": "watch",
                    "header": f"\u2717 watch: {err}",
                    "preview": "",
                    "needs_approval": False,
                    "error": f"Error: {err}",
                }

        # Check max watches limit and duplicate names
        storage = get_storage()
        existing: list[dict[str, Any]] = []
        if storage:
            existing = storage.list_watches_for_ws(self._ws_id)
            if len(existing) >= MAX_WATCHES_PER_WS:
                return {
                    "call_id": call_id,
                    "func_name": "watch",
                    "header": f"\u2717 watch: limit reached ({MAX_WATCHES_PER_WS})",
                    "preview": "",
                    "needs_approval": False,
                    "error": f"Error: maximum {MAX_WATCHES_PER_WS} active watches per workstream",
                }

        name = args.get("name", "")
        if not name:
            name = f"watch-{uuid.uuid4().hex[:4]}"
        elif storage and any(w["name"] == name for w in existing):
            return {
                "call_id": call_id,
                "func_name": "watch",
                "header": f'\u2717 watch: name "{name}" already in use',
                "preview": "",
                "needs_approval": False,
                "error": f'Error: a watch named "{name}" already exists in this workstream',
            }
        max_polls = args.get("max_polls", 100)
        try:
            max_polls = int(max_polls)
        except (ValueError, TypeError):
            max_polls = 100

        display_cmd = command.split("\n")[0]
        condition_display = f", stop_on={stop_on}" if stop_on else ", on change"
        return {
            "call_id": call_id,
            "func_name": "watch",
            "header": f'\u23f1 watch: "{name}" every {poll_every_str}',
            "preview": f"    {display_cmd}{condition_display}",
            "needs_approval": True,
            "approval_label": "watch",
            "execute": self._exec_watch,
            "action": "create",
            "command": command,
            "interval_secs": interval_secs,
            "stop_on": stop_on,
            "watch_name": name,
            "max_polls": max_polls,
        }

    def _exec_watch(self, item: dict[str, Any]) -> tuple[str, str]:
        from datetime import datetime, timedelta

        call_id = item["call_id"]
        action = item["action"]
        storage = get_storage()

        if action == "list":
            if not storage:
                msg = "No watches (storage unavailable)"
                self._report_tool_result(call_id, "watch", msg)
                return call_id, msg
            watches = storage.list_watches_for_ws(self._ws_id)
            if not watches:
                msg = "No active watches."
                self._report_tool_result(call_id, "watch", msg)
                return call_id, msg
            from turnstone.core.watch import format_interval

            lines = []
            for w in watches:
                condition = w.get("stop_on") or "on change"
                lines.append(
                    f"  {w['name']} ({w['watch_id'][:8]}): "
                    f"every {format_interval(w['interval_secs'])}, "
                    f"poll #{w['poll_count']}/{w['max_polls']}, "
                    f"condition: {condition}, "
                    f"cmd: {w['command'][:60]}"
                )
            msg = "Active watches:\n" + "\n".join(lines)
            self._report_tool_result(call_id, "watch", msg)
            return call_id, msg

        if action == "cancel":
            name = item.get("watch_name", "")
            if not storage:
                msg = "Error: storage unavailable"
                self._report_tool_result(call_id, "watch", msg, is_error=True)
                return call_id, msg
            watches = storage.list_watches_for_ws(self._ws_id)
            target = None
            for w in watches:
                if w["name"] == name or w["watch_id"].startswith(name):
                    target = w
                    break
            if target is None:
                msg = f'Watch "{name}" not found.'
                self._report_tool_result(call_id, "watch", msg, is_error=True)
                return call_id, msg
            storage.update_watch(target["watch_id"], active=False, next_poll="")
            msg = f'Watch "{target["name"]}" cancelled.'
            self._report_tool_result(call_id, "watch", msg)
            return call_id, msg

        # action == "create"
        if not storage:
            msg = "Error: storage unavailable"
            self._report_tool_result(call_id, "watch", msg, is_error=True)
            return call_id, msg

        watch_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        next_poll = now + timedelta(seconds=item["interval_secs"])
        storage.create_watch(
            watch_id=watch_id,
            ws_id=self._ws_id,
            node_id=self._node_id or "",
            name=item["watch_name"],
            command=item["command"],
            interval_secs=item["interval_secs"],
            stop_on=item.get("stop_on"),
            max_polls=item["max_polls"],
            created_by="model",
            next_poll=next_poll.strftime("%Y-%m-%dT%H:%M:%S"),
        )

        from turnstone.core.watch import format_interval

        stop_desc = f"stop_on: {item['stop_on']}" if item.get("stop_on") else "on output change"
        msg = (
            f'Watch "{item["watch_name"]}" created.\n'
            f"  Polling every {format_interval(item['interval_secs'])}, "
            f"max {item['max_polls']} polls\n"
            f"  Command: {item['command']}\n"
            f"  Condition: {stop_desc}"
        )
        self._report_tool_result(call_id, "watch", msg)
        return call_id, msg

    _MAX_WATCH_CHAIN = 5  # max consecutive watch dispatches per worker thread

    def _dispatch_pending_watch(self, depth: int = 0) -> None:
        """Dispatch one pending watch result as a new send() turn.

        Each ``send()`` chains back here on IDLE, so multiple queued results
        are processed sequentially.  Depth is capped to prevent unbounded
        stack growth.
        """
        if depth >= self._MAX_WATCH_CHAIN:
            self._watch_dispatch_depth = 0
            return
        try:
            result = self._watch_pending.get_nowait()
        except queue.Empty:
            self._watch_dispatch_depth = 0  # chain ended — reset for next user turn
            return
        message = result.get("message", "")
        if message:
            self._watch_dispatch_depth = depth + 1
            self.send(message)

    def _exec_write_file(self, item: dict[str, Any]) -> tuple[str, str]:
        """Write content to a file, creating parent directories as needed."""
        self._check_cancelled()
        call_id = item["call_id"]
        path, content, resolved = item["path"], item["content"], item["resolved"]
        is_append = item.get("append", False)
        try:
            os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
            with open(resolved, "a" if is_append else "w") as f:
                f.write(content)
            self._read_files.add(resolved)
            verb = "Appended" if is_append else "Wrote"
            msg = f"{verb} {len(content)} chars to {path}"
            self._report_tool_result(call_id, "write_file", msg)
            return call_id, msg
        except Exception as e:
            msg = f"Error writing {path}: {e}"
            self._report_tool_result(call_id, "write_file", msg, is_error=True)
            return call_id, msg

    def _exec_edit_file(self, item: dict[str, Any]) -> tuple[str, str]:
        """Apply one or more edits to a file (re-reads to avoid TOCTOU).

        Batch edits are resolved to character offsets, checked for overlap,
        and applied in reverse order so earlier offsets stay valid.
        """
        self._check_cancelled()
        call_id = item["call_id"]
        path = item["path"]
        resolved = item.get("resolved", os.path.realpath(os.path.expanduser(path)))
        edits: list[dict[str, Any]] = item["edits"]
        try:
            with open(resolved) as f:
                content = f.read()

            # replace_all mode: simple str.replace, skip offset logic
            do_replace_all = item.get("replace_all", False)
            if do_replace_all and len(edits) == 1:
                old = edits[0]["old_string"]
                new = edits[0]["new_string"]
                count = content.count(old)
                if count == 0:
                    msg = f"Error: old_string not found in {path}"
                    self._report_tool_result(call_id, "edit_file", msg, is_error=True)
                    return call_id, msg
                content = content.replace(old, new)
                with open(resolved, "w") as f:
                    f.write(content)
                msg = f"Edited {path}: replaced {count} occurrences"
                self._report_tool_result(call_id, "edit_file", msg)
                return call_id, msg

            # Resolve each edit to a (start_idx, end_idx, new_string) replacement
            replacements: list[tuple[int, int, str]] = []
            for i, edit in enumerate(edits):
                new = edit["new_string"]
                label = f"edits[{i}]: " if len(edits) > 1 else ""

                old = edit["old_string"]
                nl = edit.get("near_line")
                occurrences = find_occurrences(content, old)
                if len(occurrences) == 0:
                    msg = f"Error: {label}old_string no longer found in {path} (file changed)"
                    self._report_tool_result(call_id, "edit_file", msg, is_error=True)
                    return call_id, msg
                if len(occurrences) > 1 and nl is None:
                    line_list = ", ".join(str(ln) for ln in occurrences)
                    msg = (
                        f"Error: {label}old_string found {len(occurrences)} times "
                        f"at lines {line_list} (file changed)"
                    )
                    self._report_tool_result(call_id, "edit_file", msg, is_error=True)
                    return call_id, msg
                if nl is not None and len(occurrences) > 1:
                    idx = pick_nearest(content, old, nl)
                else:
                    idx = content.index(old)
                replacements.append((idx, idx + len(old), new))

            # Check for overlapping edits
            replacements.sort(key=lambda r: r[0])
            for j in range(len(replacements) - 1):
                if replacements[j][1] > replacements[j + 1][0]:
                    msg = "Error: edits overlap — two edits modify the same region"
                    self._report_tool_result(call_id, "edit_file", msg, is_error=True)
                    return call_id, msg

            # Apply in reverse order so offsets stay valid
            for start, end, new in reversed(replacements):
                content = content[:start] + new + content[end:]

            with open(resolved, "w") as f:
                f.write(content)
            count = len(replacements)
            noun = "edit" if count == 1 else "edits"
            msg = f"Edited {path}: applied {count} {noun}"
            self._report_tool_result(call_id, "edit_file", msg)
            return call_id, msg
        except Exception as e:
            msg = f"Error writing {path}: {e}"
            self._report_tool_result(call_id, "edit_file", msg, is_error=True)
            return call_id, msg

    def _exec_math(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute Python code in sandboxed subprocess."""
        call_id, code = item["call_id"], item["code"]
        output, is_error = execute_math_sandboxed(code, timeout=self.tool_timeout)
        output = self._truncate_output(output)

        result_msg = f"Error:\n{output}" if is_error else output if output else "(no output)"
        self._report_tool_result(call_id, "math", result_msg, is_error=is_error)
        return call_id, result_msg

    def _exec_man(self, item: dict[str, Any]) -> tuple[str, str]:
        """Look up a man or info page."""
        self._check_cancelled()
        call_id = item["call_id"]
        page = item["page"]
        section = item.get("section", "")

        # Try man first, fall back to info
        cmd = ["man"]
        if section:
            cmd.append(section)
        cmd.append(page)

        text = ""
        try:
            from turnstone.core.env import scrubbed_env

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env=scrubbed_env(extra={"MANWIDTH": "80", "MAN_KEEP_FORMATTING": "0"}),
            )
            if result.returncode == 0 and result.stdout.strip():
                # Strip formatting: backspace overstrikes and ANSI escapes
                text = re.sub(r".\x08", "", result.stdout)
                text = re.sub(r"\x1b\[[0-9;]*m", "", text)
            else:
                # Fall back to info
                result = subprocess.run(
                    ["info", page],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=scrubbed_env(),
                )
                if result.returncode == 0 and result.stdout.strip():
                    text = result.stdout
                else:
                    msg = f"No man or info page found for '{page}'"
                    self._report_tool_result(call_id, "man", msg)
                    return call_id, msg
        except FileNotFoundError:
            msg = "Error: man command not available"
            self._report_tool_result(call_id, "man", msg, is_error=True)
            return call_id, msg
        except subprocess.TimeoutExpired:
            msg = "Error: man page lookup timed out"
            self._report_tool_result(call_id, "man", msg, is_error=True)
            return call_id, msg

        text = self._truncate_output(text)

        self._report_tool_result(call_id, "man", f"{len(text)} chars")

        return call_id, text

    def _exec_web_fetch(self, item: dict[str, Any]) -> tuple[str, str]:
        """Fetch a URL, then summarize/extract using an API call."""
        self._check_cancelled()
        call_id, url = item["call_id"], item["url"]
        question = item.get("question", "Summarize the key content of this page.")

        # Phase 1: fetch the URL
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": "turnstone/1.0"},
                timeout=self.tool_timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            text = resp.text
            if "html" in ct:
                text = strip_html(text)
            # Cap at 10 MB
            if len(text) > 10 * 1024 * 1024:
                text = text[: 10 * 1024 * 1024]

        except httpx.HTTPStatusError as e:
            msg = f"Error: fetch failed: HTTP {e.response.status_code}"
            self._report_tool_result(call_id, "web_fetch", msg, is_error=True)
            return call_id, msg
        except (httpx.RequestError, ValueError) as e:
            msg = f"Error: fetch failed: {e}"
            self._report_tool_result(call_id, "web_fetch", msg, is_error=True)
            return call_id, msg
        except Exception as e:
            msg = f"Error fetching URL: {e}"
            self._report_tool_result(call_id, "web_fetch", msg, is_error=True)
            return call_id, msg

        if not text.strip():
            msg = "Error: fetch returned empty response"
            self._report_tool_result(call_id, "web_fetch", msg, is_error=True)
            return call_id, msg

        original_len = len(text)
        self.ui.on_info(f"fetched {original_len} chars, extracting...")

        # Phase 2: truncate for summarization context.
        # Reserve ~25% of the context window for the extraction prompt
        # overhead (system message, URL, question) and response tokens.
        # Convert token budget to chars using the calibrated ratio.
        max_content = int(self.context_window * self._chars_per_token * 0.75)
        max_content = min(max(max_content, 50_000), 500_000)  # 50k–500k
        if len(text) > max_content:
            # Prefer the beginning — page content is usually top-heavy.
            text = text[:max_content] + f"\n\n... [{len(text) - max_content} chars truncated] ...\n"

        # Phase 3: summarization API call.
        # Use a generous max_tokens so thinking models don't starve the
        # visible answer, and pass reasoning_effort="low" to avoid wasting
        # budget on deep reasoning for a simple extraction task.
        try:
            result = self._utility_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a web content extraction assistant. "
                            "Answer the user's question using ONLY the "
                            "provided page content. Be concise and factual. "
                            "If the content doesn't contain the answer, say so."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Page URL: {url}\n"
                            f"Page content ({original_len} chars):\n\n"
                            f"{text}\n\n---\n"
                            f"Question: {question}"
                        ),
                    },
                ],
                max_tokens=8192,
                temperature=0.2,
            )
            answer = result.content or ""
            if not answer:
                answer = "Error: extraction returned no answer"
        except Exception as e:
            answer = f"Extraction failed (page was fetched but summarization errored): {e}"

        self._report_tool_result(
            call_id,
            "web_fetch",
            answer,
            is_error=answer.startswith(("Error:", "Extraction failed")),
        )

        return call_id, answer

    def _exec_web_search(self, item: dict[str, Any]) -> tuple[str, str]:
        """Search the web via the configured backend (Tavily, DDG, or MCP)."""
        self._check_cancelled()
        call_id = item["call_id"]
        query = item["query"]
        max_results = item.get("max_results", 5)
        topic = item.get("topic", "general")

        client = self._resolve_search_client()
        if not client:
            msg = "Error: web search backend not available"
            self._report_tool_result(call_id, "web_search", msg, is_error=True)
            return call_id, msg

        try:
            output = client.search(query, max_results=max_results, topic=topic)
        except Exception as e:
            msg = f"Error: web search failed: {e}"
            self._report_tool_result(call_id, "web_search", msg, is_error=True)
            return call_id, msg

        output = self._truncate_output(output)
        self._report_tool_result(call_id, "web_search", output)
        return call_id, output

    def handle_command(self, cmd_line: str) -> bool:
        """Handle slash commands. Returns True if should exit."""
        parts = cmd_line.strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/exit", "/quit", "/q"):
            return True

        elif cmd == "/instructions":
            if not arg:
                if self.instructions:
                    self.ui.on_info(f"Current instructions: {self.instructions[:100]}...")
                else:
                    self.ui.on_info("No instructions set. Usage: /instructions <text>")
            else:
                self.instructions = arg.strip()
                self._init_system_messages()
                self._save_config()
                self.ui.on_info("Instructions updated.")

        elif cmd == "/skill":
            if not arg:
                if self._skill_name:
                    self.ui.on_info(f"Active skill: {self._skill_name}")
                else:
                    self.ui.on_info("Using defaults. Usage: /skill <name> or /skill clear")
            elif arg.strip().lower() == "clear":
                self.set_skill(None)
                self.ui.on_info("Skill cleared; using defaults.")
            else:
                tpl = get_skill_by_name(arg.strip())
                if tpl:
                    self.set_skill(tpl["name"])
                    self.ui.on_info(f"Skill set: {tpl['name']}")
                else:
                    self.ui.on_error(f"Skill not found: {arg.strip()}")

        elif cmd == "/clear":
            self.messages.clear()
            self._read_files.clear()
            self._recent_tool_sigs.clear()
            self._last_usage = None
            self._calibrated_msg_count = 0
            self._msg_tokens = []
            self.ui.on_info("Context cleared (messages preserved in database).")

        elif cmd == "/new":
            from turnstone.core.memory import register_workstream

            self.messages.clear()
            self._read_files.clear()
            self._recent_tool_sigs.clear()
            self._last_usage = None
            self._calibrated_msg_count = 0
            self._msg_tokens = []
            self._ws_id = uuid.uuid4().hex
            self._title_generated = False
            register_workstream(self._ws_id, node_id=self._node_id)
            self._save_config()
            self.ui.on_info("New workstream started.")

        elif cmd == "/workstreams":
            rows = list_workstreams_with_history(limit=20)
            if not rows:
                self.ui.on_info("No saved workstreams.")
            else:
                lines = ["Workstreams:\n"]
                for wid, alias, title, _created, updated, count, *_extra in rows:
                    display_name = alias or wid
                    display_title = f"  {title}" if title else ""
                    marker = " *" if wid == self._ws_id else "  "
                    lines.append(
                        f" {marker} {bold(display_name)}{display_title}  "
                        f"{dim(f'{count} msgs, {updated}')}"
                    )
                self.ui.on_info("\n".join(lines))

        elif cmd == "/resume":
            if not arg:
                self.ui.on_info(
                    "Usage: /resume <alias_or_ws_id>\nUse /workstreams to list available workstreams."
                )
            else:
                target_id = resolve_workstream(arg.strip())
                if not target_id:
                    self.ui.on_info(f"Workstream not found: {arg.strip()}")
                elif target_id == self._ws_id:
                    self.ui.on_info("Already in that workstream.")
                elif self.resume(target_id):
                    self.ui.on_info(
                        f"Resumed {bold(target_id)} ({len(self.messages)} messages loaded)"
                    )
                    name = get_workstream_display_name(target_id)
                    if name:
                        self.ui.on_rename(name)
                else:
                    self.ui.on_info(f"Workstream {arg.strip()} has no messages.")

        elif cmd == "/name":
            if not arg:
                self.ui.on_info(f"Current workstream: {self._ws_id}")
            elif set_workstream_alias(self._ws_id, arg.strip()):
                self.ui.on_info(f"Workstream named: {bold(arg.strip())}")
                self.ui.on_rename(arg.strip())
            else:
                self.ui.on_info(f"Alias '{arg.strip()}' is already in use.")

        elif cmd == "/delete":
            if not arg:
                self.ui.on_info(
                    "Usage: /delete <alias_or_ws_id>\nUse /workstreams to list workstreams."
                )
            else:
                target_id = resolve_workstream(arg.strip())
                if not target_id:
                    self.ui.on_info(f"Workstream not found: {arg.strip()}")
                elif target_id == self._ws_id:
                    self.ui.on_info("Cannot delete the active workstream.")
                elif delete_workstream(target_id):
                    self.ui.on_info(f"Deleted workstream {arg.strip()}")
                else:
                    self.ui.on_info(f"Failed to delete workstream {arg.strip()}")

        elif cmd == "/history":
            query = arg.strip() if arg else None
            if query:
                rows = search_history(query, limit=20)
                if not rows:
                    self.ui.on_info(f"No results for {query!r}")
                else:
                    lines = [f"Found {len(rows)} result(s) for {query!r}:\n"]
                    for ts, sid, role, content, tool_name in rows:
                        label = tool_name if tool_name else role
                        text = (content or "")[:200]
                        lines.append(f"  {dim(ts)} {dim(sid)} {bold(label)}: {text}")
                    self.ui.on_info("\n".join(lines))
            else:
                # Show recent conversations (last 20 messages)
                rows = search_history_recent(limit=20)
                if not rows:
                    self.ui.on_info("No conversation history yet.")
                else:
                    lines = ["Recent history:\n"]
                    for ts, sid, role, content, tool_name in rows:
                        label = tool_name if tool_name else role
                        text = (content or "")[:200]
                        lines.append(f"  {dim(ts)} {dim(sid)} {bold(label)}: {text}")
                    self.ui.on_info("\n".join(lines))

        elif cmd == "/model":
            if not arg:
                info = f"Model: {cyan(self.model)}"
                if self._model_alias:
                    info += f" ({self._model_alias})"
                if self._registry and self._registry.count > 1:
                    avail = ", ".join(self._registry.list_aliases())
                    info += f"\nAvailable: {avail}"
                    if self._registry.fallback:
                        info += f"\nFallback: {', '.join(self._registry.fallback)}"
                    if self._registry.agent_model:
                        info += f"\nAgent model: {self._registry.agent_model}"
                self.ui.on_info(info)
            elif self._registry and self._registry.has_alias(arg):
                client, model_name, cfg = self._registry.resolve(arg)
                self.client = client
                self.model = model_name
                self._model_alias = arg
                self._provider = self._registry.get_provider(arg)
                self._cached_capabilities = None
                self.context_window = cfg.context_window
                if not self._manual_tool_truncation:
                    self.tool_truncation = int(cfg.context_window * self._chars_per_token * 0.5)
                # Apply per-model sampling overrides, falling back to global
                # defaults — mirrors session_factory() resolution logic so
                # switching away from a model with overrides doesn't leak them.
                cs = self._config_store
                self.temperature = (
                    cfg.temperature
                    if cfg.temperature is not None
                    else (cs.get("model.temperature") if cs else self.temperature)
                )
                self.max_tokens = (
                    cfg.max_tokens
                    if cfg.max_tokens is not None
                    else (cs.get("model.max_tokens") if cs else self.max_tokens)
                )
                self.reasoning_effort = (
                    cfg.reasoning_effort
                    if cfg.reasoning_effort is not None
                    else (cs.get("model.reasoning_effort") if cs else self.reasoning_effort)
                )
                self._init_system_messages()
                self._save_config()
                self.ui.on_info(f"Switched to {cyan(arg)}: {model_name}")
            else:
                available = ""
                if self._registry:
                    available = f" Available: {', '.join(self._registry.list_aliases())}"
                self.ui.on_info(f"Unknown model alias: {arg}.{available}")

        elif cmd == "/raw":
            self.show_reasoning = not self.show_reasoning
            state = "on" if self.show_reasoning else "off"
            self.ui.on_info(f"Reasoning display: {bold(state)}")

        elif cmd == "/reason":
            valid = ("low", "medium", "high")
            aliases = {"med": "medium", "lo": "low", "hi": "high"}
            if not arg:
                self.ui.on_info(f"Reasoning effort: {cyan(self.reasoning_effort)}")
            else:
                value = aliases.get(arg.lower(), arg.lower())
                if value in valid:
                    self.reasoning_effort = value
                    self._init_system_messages()
                    self._save_config()
                    self.ui.on_info(f"Reasoning effort set to {cyan(self.reasoning_effort)}")
                else:
                    self.ui.on_info(f"Invalid. Choose from: {', '.join(valid)}")

        elif cmd == "/compact":
            self._compact_messages()

        elif cmd == "/creative":
            self.creative_mode = not self.creative_mode
            self._init_system_messages()
            self._save_config()
            # Clear history when toggling ON if it contains tool messages,
            # because the API rejects tool-call history without tool definitions
            if self.creative_mode and any(
                m.get("tool_calls") or m.get("role") == "tool" for m in self.messages
            ):
                self.messages.clear()
                self._read_files.clear()
                self._msg_tokens.clear()
                self.ui.on_info(
                    "[history cleared — creative mode is incompatible with tool history]"
                )
            state = "on" if self.creative_mode else "off"
            self.ui.on_info(
                f"Creative mode: {bold(state)} (tools {'disabled' if self.creative_mode else 'enabled'})"
            )

        elif cmd == "/debug":
            self.debug = not self.debug
            state = "on" if self.debug else "off"
            self.ui.on_info(f"Debug mode: {bold(state)} (prints raw SSE deltas)")

        elif cmd == "/mcp":
            if not self._mcp_client:
                self.ui.on_info("No MCP servers configured.")
            elif arg and arg.split()[0] == "refresh":
                self._handle_mcp_refresh(arg)
            else:
                tools = self._mcp_client.get_tools()
                resources = self._mcp_client.get_resources()
                prompts = self._mcp_client.get_prompts()
                mcp_lines = []
                if tools:
                    mcp_lines.append(f"MCP tools ({len(tools)}):")
                    for t in tools:
                        name = t["function"]["name"]
                        desc = t["function"].get("description", "")[:80]
                        mcp_lines.append(f"  {name}  {dim(desc)}")
                if resources:
                    if mcp_lines:
                        mcp_lines.append("")
                    mcp_lines.append(f"MCP resources ({len(resources)}):")
                    for r in resources:
                        prefix = "[template] " if r.get("template") else ""
                        desc = r.get("description", "")[:80]
                        mcp_lines.append(f"  {prefix}{r['uri']}  {dim(desc)}")
                if prompts:
                    if mcp_lines:
                        mcp_lines.append("")
                    mcp_lines.append(f"MCP prompts ({len(prompts)}):")
                    for p in prompts:
                        arg_names = ", ".join(a["name"] for a in p.get("arguments", []))
                        desc = p.get("description", "")[:60]
                        mcp_lines.append(f"  {p['name']}({arg_names})  {dim(desc)}")
                if not mcp_lines:
                    self.ui.on_info(
                        "MCP client connected but no tools, resources, or prompts available."
                    )
                else:
                    self.ui.on_info("\n".join(mcp_lines))

        elif cmd == "/retry":
            user_msg = self.retry()
            if user_msg is None:
                self.ui.on_info("Nothing to retry.")
            else:
                self._pending_retry = user_msg
                self.ui.on_info(f"Retrying: {user_msg[:80]}...")

        elif cmd == "/rewind":
            if not arg:
                self.ui.on_info("Usage: /rewind <N> — drop the last N turns")
            else:
                try:
                    n = int(arg)
                except ValueError:
                    self.ui.on_info("Usage: /rewind <N> — N must be a positive integer")
                else:
                    if n < 1:
                        self.ui.on_info("N must be at least 1.")
                    else:
                        turns_available = len(self._find_turn_boundaries())
                        actual_n = min(n, turns_available)
                        removed = self.rewind(n)
                        if removed == 0:
                            self.ui.on_info("No turns to rewind.")
                        else:
                            self.ui.on_info(
                                f"Rewound {actual_n} turn(s) ({removed} messages removed). "
                                f"{len(self.messages)} messages remain."
                            )

        elif cmd == "/help":
            self.ui.on_info(
                "\n".join(
                    [
                        "── Slash Commands ─────────────────────────────────────",
                        "  /instructions <text>   Set developer instructions",
                        "  /skill [name|clear]    Set/show/clear active skill",
                        "  /clear                 Clear context (workstream preserved in database)",
                        "  /new                   Start a new workstream (old one stays resumable)",
                        "",
                        "  /workstreams           List saved workstreams",
                        "  /resume <id|alias>     Resume a previous workstream",
                        "  /name <alias>          Name the current workstream",
                        "  /delete <id|alias>     Delete a saved workstream",
                        "",
                        "  /history [query]       Search conversation history (or show recent)",
                        "  /compact               Compact conversation (summarize old messages)",
                        "  /retry                 Re-send the last user message for a new response",
                        "  /rewind <N>            Drop the last N turns (user + response)",
                        "",
                        "  /model [alias]         Show/switch model (alias from config)",
                        "  /raw                   Toggle reasoning content display",
                        "  /reason [low|med|high] Set/show reasoning effort",
                        "  /creative              Toggle creative writing mode (no tools)",
                        "  /debug                 Toggle raw SSE delta logging",
                        "  /mcp [refresh [server]] List or refresh MCP tools, resources, and prompts",
                        "  /help                  Show this help",
                        "  /exit                  Exit (also: Ctrl+D)",
                        "────────────────────────────────────────────────────────",
                    ]
                )
            )

        else:
            self.ui.on_info(f"Unknown command: {cmd}. Type /help for available commands.")

        return False
