"""Settings registry — code-defined catalog of database-storable configuration settings.

Every setting that can be stored in the ``system_settings`` table must
have an entry here.  Unknown keys are rejected at the API boundary.
Bootstrap settings (database, auth, server/console bind) are
excluded — they are needed before storage is available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SettingDef:
    """Definition of a single configuration setting."""

    key: str  # dotted path: "memory.relevance_k"
    type: str  # "int" | "float" | "str" | "bool"
    default: Any
    description: str
    section: str  # TOML section name
    is_secret: bool = False
    min_value: float | None = None
    max_value: float | None = None
    choices: list[str] | None = field(default=None, hash=False)
    restart_required: bool = False
    help: str = ""  # plain-English explanation for non-experts
    reference_url: str = ""  # link to arXiv, docs, or provider reference


def _build_registry() -> dict[str, SettingDef]:
    """Build the settings registry from declarative definitions."""
    defs: list[SettingDef] = [
        # -- model ----------------------------------------------------------
        SettingDef(
            "model.default_alias",
            "str",
            "",
            "Default model alias for new sessions (empty = use config.toml [model].default)",
            "model",
            help="Which named model alias to use for new sessions. When empty, falls back to "
            "the [model].default setting in config.toml (which defaults to 'default'). "
            "Change this at runtime to switch all new sessions to a different model "
            "without restarting.",
        ),
        SettingDef(
            "model.temperature",
            "float",
            0.5,
            "Default sampling temperature (overridden by per-model settings)",
            "model",
            min_value=0.0,
            max_value=2.0,
            help="Default sampling temperature for models without a per-model override. "
            "Controls randomness in responses. Lower values (0.0\u20130.3) give focused, "
            "deterministic output; higher values (0.7\u20131.5) make responses more creative "
            "and varied. Per-model overrides can be set in the Models tab.",
            reference_url="https://arxiv.org/abs/1904.09751",
        ),
        SettingDef(
            "model.max_tokens",
            "int",
            32768,
            "Default max output tokens (overridden by per-model settings)",
            "model",
            min_value=1,
            help="Default max output tokens for models without a per-model override. "
            "Upper limit on how long each response can be. One token is roughly 4 characters "
            "of English text. Per-model overrides can be set in the Models tab.",
        ),
        SettingDef(
            "model.reasoning_effort",
            "str",
            "medium",
            "Default reasoning effort (overridden by per-model settings)",
            "model",
            choices=["", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
            help="Default reasoning effort for models without a per-model override. "
            "Controls how much internal \u2018thinking\u2019 the model does before responding. "
            "Higher effort improves quality on complex tasks but is slower and uses more "
            "tokens. Per-model overrides can be set in the Models tab.",
        ),
        SettingDef(
            "model.plan_alias",
            "str",
            "",
            "Model alias for plan_agent (empty = inherit from config / session)",
            "model",
            help="Which model the plan_agent sub-agent uses. When empty, falls back to "
            "[model].plan_model in config.toml, then [model].agent_model, then the session "
            "model. Plan_agent runs rarely but benefits from a stronger model for "
            "high-quality plans \u2014 point this at your strongest reasoner.",
        ),
        SettingDef(
            "model.task_alias",
            "str",
            "",
            "Model alias for task_agent (empty = inherit from config / session)",
            "model",
            help="Which model the task_agent sub-agent uses. When empty, falls back to "
            "[model].task_model in config.toml, then [model].agent_model, then the session "
            "model. Task_agent fires frequently for autonomous subtasks \u2014 point this "
            "at a cheaper/faster model than your plan_agent.",
        ),
        SettingDef(
            "model.plan_effort",
            "str",
            "",
            "Reasoning effort for plan_agent (empty = inherit from config; default \u2018high\u2019)",
            "model",
            choices=["", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
            help="Reasoning effort for plan_agent specifically. When empty, falls back to "
            "[model].plan_effort in config.toml, then to the built-in default \u2018high\u2019. "
            "Use \u2018xhigh\u2019 or \u2018max\u2019 with models that support deeper reasoning "
            "for higher-quality plans. (Empty here means \u201cinherit\u201d \u2014 use "
            "\u2018none\u2019 to actually disable reasoning.)",
        ),
        SettingDef(
            "model.task_effort",
            "str",
            "",
            "Reasoning effort for task_agent (empty = inherit from config / session)",
            "model",
            choices=["", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
            help="Reasoning effort for task_agent specifically. When empty, falls back to "
            "[model].task_effort in config.toml, then inherits the session\u2019s effort. "
            "Set to \u2018low\u2019 or \u2018minimal\u2019 if your task_agent runs many "
            "fast subtasks where deep reasoning is wasteful. (Empty here means \u201cinherit\u201d "
            "\u2014 use \u2018none\u2019 to actually disable reasoning.)",
        ),
        # -- session --------------------------------------------------------
        SettingDef(
            "session.instructions",
            "str",
            "",
            "Default system instructions (applied before skills)",
            "session",
            help="Text that tells the model how to behave (e.g. \u2018You are a helpful coding assistant\u2019). "
            "Applied to every conversation before any skills.",
        ),
        SettingDef(
            "session.retention_days",
            "int",
            90,
            "Days to retain conversation history (0 = disabled)",
            "session",
            min_value=0,
        ),
        SettingDef(
            "session.compact_max_tokens",
            "int",
            32768,
            "Max tokens for compaction summary",
            "session",
            min_value=0,
            help="When conversation history is compacted (summarized to save space), this limits "
            "how long the summary can be.",
        ),
        SettingDef(
            "session.auto_compact_pct",
            "float",
            0.8,
            "Auto-compact at this fraction of context window (0 = disabled)",
            "session",
            min_value=0.0,
            max_value=1.0,
            help="Automatically summarize older messages when the conversation fills this percentage "
            "of the context window. For example, 0.8 means compact when 80% full. This prevents "
            "conversations from hitting the context limit and losing information.",
        ),
        # -- tools ----------------------------------------------------------
        SettingDef(
            "tools.timeout",
            "int",
            120,
            "Tool execution timeout in seconds",
            "tools",
            min_value=1,
            max_value=3600,
        ),
        SettingDef(
            "tools.truncation",
            "int",
            0,
            "Tool output truncation limit in chars (0 = auto, 50% of context window)",
            "tools",
            min_value=0,
            help="Limits how much output from a tool (e.g. a long command result) gets sent back "
            "to the model. Prevents large outputs from consuming the entire context window.",
        ),
        SettingDef(
            "tools.agent_max_turns",
            "int",
            -1,
            "Max turns for plan/task agents (-1 = unlimited)",
            "tools",
            min_value=-1,
            max_value=200,
            help="Limits how many back-and-forth steps a sub-agent can take when executing a plan "
            "or task. Prevents runaway agents from consuming excessive tokens.",
        ),
        SettingDef(
            "tools.skip_permissions",
            "bool",
            False,
            "Skip tool approval prompts",
            "tools",
            help="When enabled, all tool calls are auto-approved without asking the user. "
            "Use with caution \u2014 the model will be able to run commands, write files, "
            "and take actions without human review.",
        ),
        SettingDef(
            "tools.search",
            "str",
            "auto",
            "Tool search mode (auto = enable when tool count exceeds threshold)",
            "tools",
            choices=["auto", "on", "off"],
            help="When many tools are available (e.g. from MCP servers), the model sees only "
            "a subset and searches for the right tool when needed. This reduces cost and "
            "improves accuracy by avoiding information overload.",
        ),
        SettingDef(
            "tools.search_threshold",
            "int",
            20,
            "Min tool count to activate search in auto mode",
            "tools",
            min_value=1,
        ),
        SettingDef(
            "tools.search_max_results",
            "int",
            5,
            "Max tool search results",
            "tools",
            min_value=1,
            max_value=50,
        ),
        SettingDef(
            "tools.tavily_api_key",
            "str",
            "",
            "Tavily API key for web search (write-only)",
            "tools",
            is_secret=True,
            help="API key for the Tavily web search service. When set, enables the Tavily "
            "backend for web_search tool calls (higher quality than DuckDuckGo). "
            "Overrides $TAVILY_API_KEY and config.toml [api] tavily_key.",
            reference_url="https://tavily.com",
        ),
        SettingDef(
            "tools.web_search_backend",
            "str",
            "",
            "Web search backend: '' (auto), 'tavily', 'ddg', or 'mcp:server:tool'",
            "tools",
            help="Controls which service handles web_search calls when the model lacks native "
            "search support. Empty string auto-detects (Tavily if key present, else DuckDuckGo "
            "if installed). 'ddg' uses DuckDuckGo (free, no API key). 'tavily' forces Tavily. "
            "'mcp:server:tool' routes to an MCP server (e.g. 'mcp:ddg:search').",
        ),
        # -- server ---------------------------------------------------------
        SettingDef(
            "server.workstream_idle_timeout",
            "int",
            120,
            "Idle timeout for workstream eviction in minutes (0 = disabled)",
            "server",
            min_value=0,
            restart_required=True,
            help="A workstream is an independent conversation thread. Idle workstreams are "
            "evicted (paused and saved) after this timeout to free up resources. They can "
            "be resumed later.",
        ),
        SettingDef(
            "server.max_workstreams",
            "int",
            50,
            "Max concurrent workstreams",
            "server",
            min_value=1,
            restart_required=True,
            help="Maximum number of active conversation threads on this server node. "
            "When the limit is reached, the oldest idle workstream is evicted to make room. "
            "Each workstream uses memory proportional to its conversation history.",
        ),
        # -- cluster --------------------------------------------------------
        SettingDef(
            "cluster.node_fan_out_limit",
            "int",
            200,
            "Max concurrent outbound requests during cluster-wide operations",
            "cluster",
            min_value=10,
            max_value=1000,
            restart_required=True,
            help="Controls how many nodes the console queries in parallel during "
            "fan-out operations (watch listing, MCP status, reload notifications). "
            "Higher values speed up large-cluster admin operations at the cost of "
            "more concurrent connections. The httpx proxy pool is sized to match "
            "this value (requires console restart to take effect).",
        ),
        SettingDef(
            "cluster.mcp_max_servers",
            "int",
            200,
            "Max MCP server definitions in the cluster",
            "cluster",
            min_value=1,
            max_value=2000,
            help="Hard cap on the total number of MCP server definitions stored in the "
            "database. Each node only connects to the servers it needs, so this "
            "limit is on definitions, not active connections.",
        ),
        # -- channels -------------------------------------------------------
        SettingDef(
            "channels.default_model_alias",
            "str",
            "",
            "Default model alias for channel workstreams (empty = use server default)",
            "channels",
            help="Which model alias to use when a channel adapter (Discord, etc.) "
            "creates a new workstream without an explicit model. When empty, falls "
            "back to the server-wide model.default_alias.",
        ),
        # -- mcp ------------------------------------------------------------
        SettingDef(
            "mcp.config_path",
            "str",
            "",
            "Path to MCP server configuration file",
            "mcp",
            restart_required=True,
            help="Model Context Protocol (MCP) lets the AI connect to external tool servers. "
            "This points to a JSON file listing which MCP servers to connect to on startup. "
            "Tip: use the MCP Servers tab to manage servers via the database instead.",
            reference_url="https://modelcontextprotocol.io",
        ),
        SettingDef(
            "mcp.refresh_interval",
            "int",
            14400,
            "MCP resource/prompt refresh interval in seconds (0 = disabled, default 4h)",
            "mcp",
            min_value=0,
        ),
        SettingDef(
            "mcp.registry_url",
            "str",
            "",
            "MCP Registry URL (empty = official registry)",
            "mcp",
            help="Override the MCP Registry URL for enterprise/private registries. "
            "Leave empty to use the official registry at registry.modelcontextprotocol.io.",
            reference_url="https://registry.modelcontextprotocol.io",
        ),
        # -- ratelimit ------------------------------------------------------
        SettingDef(
            "ratelimit.enabled",
            "bool",
            False,
            "Enable per-IP rate limiting",
            "ratelimit",
            restart_required=True,
            help="Limits how fast any single user can make requests, preventing abuse or "
            "accidental overload. Uses a token bucket algorithm.",
        ),
        SettingDef(
            "ratelimit.requests_per_second",
            "float",
            10.0,
            "Max requests per second per IP",
            "ratelimit",
            min_value=1.0,
            restart_required=True,
        ),
        SettingDef(
            "ratelimit.burst",
            "int",
            20,
            "Burst allowance above rate limit",
            "ratelimit",
            min_value=1,
            restart_required=True,
            help="Allows short bursts of requests above the rate limit. For example, a user "
            "can send 20 rapid requests before being throttled, then must stay under the "
            "per-second limit.",
        ),
        SettingDef(
            "ratelimit.trusted_proxies",
            "str",
            "",
            "Trusted proxy CIDRs for X-Forwarded-For parsing (comma-separated)",
            "ratelimit",
            restart_required=True,
            help="If your server is behind a load balancer or reverse proxy, list its IP "
            "ranges here so rate limiting applies to the real client IP, not the proxy.",
        ),
        # -- health ---------------------------------------------------------
        SettingDef(
            "health.failure_threshold",
            "int",
            5,
            "Consecutive failures before backend is marked degraded",
            "health",
            min_value=1,
            help="If the AI backend fails this many times in a row, it is marked as degraded. "
            "Degraded backends are deprioritised in the fallback chain but requests are never "
            "blocked. The backend recovers automatically when a request succeeds.",
        ),
        # -- judge ----------------------------------------------------------
        SettingDef(
            "judge.enabled",
            "bool",
            True,
            "Enable intent validation judge",
            "judge",
            help="Before the AI runs a tool (shell command, file write, etc.), a second evaluation "
            "assesses whether the action is safe. This shows a risk verdict alongside the "
            "approval prompt so you can make informed decisions.",
        ),
        SettingDef(
            "judge.model",
            "str",
            "",
            "Model for LLM judge (empty = same as session)",
            "judge",
            help="The judge can use a different AI model than the main conversation. Leave empty "
            "to use the same model (self-consistency), or specify a different model for "
            "cross-model evaluation.",
        ),
        SettingDef(
            "judge.confidence_threshold",
            "float",
            0.7,
            "Min confidence for judge verdict",
            "judge",
            min_value=0.0,
            max_value=1.0,
            help="The judge reports how confident it is in its safety assessment (0\u20131). "
            "Verdicts below this threshold are flagged as low-confidence. Future versions "
            "can use this for auto-approval of high-confidence safe verdicts.",
        ),
        SettingDef(
            "judge.max_context_ratio",
            "float",
            0.5,
            "Max fraction of context window for judge",
            "judge",
            min_value=0.1,
            max_value=1.0,
            help="How much of the conversation history to show the judge. Lower values are cheaper "
            "and faster but give the judge less context to evaluate intent.",
        ),
        SettingDef(
            "judge.timeout",
            "float",
            60.0,
            "Judge evaluation timeout in seconds",
            "judge",
            min_value=5.0,
        ),
        SettingDef(
            "judge.read_only_tools",
            "bool",
            True,
            "Restrict judge to read-only tools",
            "judge",
            help="The judge can inspect files and directories to gather evidence for its verdict. "
            "When enabled, it can only read \u2014 not modify \u2014 the filesystem.",
        ),
        SettingDef(
            "judge.output_guard",
            "bool",
            True,
            "Evaluate tool output for security signals",
            "judge",
            help="When enabled, tool execution results are scanned for prompt injection "
            "payloads, credential leakage, and encoded payloads before entering the "
            "conversation context. Warnings are surfaced via the UI.",
        ),
        SettingDef(
            "judge.redact_secrets",
            "bool",
            True,
            "Auto-redact credentials in tool output",
            "judge",
            help="When enabled alongside output_guard, detected credentials (API keys, "
            "private keys, connection strings) are replaced with [REDACTED] markers "
            "before tool output enters the conversation.",
        ),
        SettingDef(
            "judge.cancel_on_approval",
            "bool",
            False,
            "Cancel remaining judge evaluations when user approves",
            "judge",
            help="When enabled, the judge stops evaluating remaining tool calls as soon as "
            "you approve or deny. This saves inference resources but means you won't see "
            "verdicts for later tool calls. When disabled (default), the judge evaluates "
            "every tool call to completion so all verdicts are available for later review.",
        ),
        # -- interface --------------------------------------------------------
        SettingDef(
            "interface.close_tab_action",
            "str",
            "last_used",
            "Action when closing a workstream tab",
            "interface",
            choices=["last_used", "nearest_left", "nearest_right", "dashboard"],
            help="Determines which workstream to switch to after closing a tab. "
            "'last_used' goes to the most recently active tab, 'nearest_left/right' "
            "goes to the adjacent tab, 'dashboard' returns to the saved workstreams view.",
        ),
        SettingDef(
            "interface.theme",
            "str",
            "dark",
            "Current UI theme",
            "interface",
            choices=["dark", "light"],
            help="Controls the visual theme of the user interface.",
        ),
        # -- skills ---------------------------------------------------------
        SettingDef(
            "skills.discovery_url",
            "str",
            "",
            "Skills discovery API URL (empty = skills.sh)",
            "skills",
            help="Override the skills discovery URL for enterprise or private skill registries. "
            "Leave empty to use the default skills.sh registry.",
        ),
        # -- memory ---------------------------------------------------------
        SettingDef(
            "memory.relevance_k",
            "int",
            5,
            "Top-K memories for relevance injection",
            "memory",
            min_value=1,
            max_value=50,
            help="How many saved memories to automatically include in each conversation. "
            "Memories are ranked by text relevance and the top K are injected into the "
            "model's context so it can recall past information.",
        ),
        SettingDef(
            "memory.fetch_limit",
            "int",
            50,
            "Max memories fetched from storage",
            "memory",
            min_value=1,
            max_value=500,
            help="How many memories to load from the database for ranking. The top relevance_k "
            "are selected from this pool. Higher values find better matches but cost more.",
        ),
        SettingDef(
            "memory.max_content",
            "int",
            32768,
            "Max memory content size in characters",
            "memory",
            min_value=100,
            max_value=65536,
        ),
        SettingDef(
            "memory.nudge_cooldown",
            "int",
            300,
            "Seconds between metacognitive nudges",
            "memory",
            min_value=0,
            help="Metacognitive nudges are gentle reminders to the AI to save useful information "
            "from the conversation (e.g. user preferences, project decisions). This controls "
            "the minimum time between nudges to avoid being repetitive.",
        ),
        SettingDef(
            "memory.nudges",
            "bool",
            True,
            "Enable metacognitive nudges",
            "memory",
            help="When enabled, the system periodically reminds the AI to save important "
            "information from conversations into long-term memory. This helps the AI "
            "remember context across separate conversations.",
        ),
        # -- tls ----------------------------------------------------------------
        SettingDef(
            "tls.enabled",
            "bool",
            False,
            "Enable mTLS for inter-service communication",
            "tls",
            restart_required=True,
            help="When enabled, the console runs an internal Certificate Authority and "
            "ACME server. All cluster services (servers, channels) auto-provision "
            "short-lived certificates for mutual TLS. Requires lacme: pip install turnstone[tls]",
        ),
        SettingDef(
            "tls.acme_directory",
            "str",
            "",
            "External ACME CA URL for the console's frontend HTTPS cert",
            "tls",
            restart_required=True,
            help="Set to a public ACME directory URL (e.g. https://acme-v02.api.letsencrypt.org/"
            "directory) to get a publicly trusted certificate for the console's HTTPS endpoint. "
            "Leave empty to self-issue from the internal CA (use when behind a reverse proxy).",
        ),
        # -- node ---------------------------------------------------------------
        SettingDef(
            "node.weight",
            "int",
            1,
            "Node weight for rendezvous routing",
            "node",
            min_value=1,
            max_value=100,
            help="Relative capacity of this server node. A node with weight 2 wins "
            "roughly twice as many ws_id rendezvous selections (and therefore "
            "receives twice as many workstreams) as a node with weight 1.",
        ),
        # -- coordinator --------------------------------------------------------
        SettingDef(
            "coordinator.model_alias",
            "str",
            "",
            "Model alias used for coordinator workstreams",
            "coordinator",
            help="Which model alias the console-hosted coordinator sessions run on. "
            "Must match an entry in the Models tab. Coordinator sessions create and "
            "drive child workstreams on your server nodes; point this at a capable "
            "model so the orchestration reasoning is solid. When empty, "
            "POST /v1/api/coordinator/new returns 503 with a remediation message.",
        ),
        SettingDef(
            "coordinator.reasoning_effort",
            "str",
            "medium",
            "Reasoning effort for coordinator sessions",
            "coordinator",
            choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
            help="Reasoning effort for coordinator sessions. Coordinators benefit from "
            "medium-or-higher effort when juggling multiple child workstreams. Use "
            "'low' only when your coordinator handles simple, one-off dispatch "
            "workflows.",
        ),
        SettingDef(
            "coordinator.max_active",
            "int",
            5,
            "Maximum concurrent coordinator sessions",
            "coordinator",
            min_value=1,
            max_value=100,
            help="Cap on how many coordinator workstreams can run at once on this "
            "console. When the limit is reached, POST /v1/api/coordinator/new either "
            "evicts the oldest idle coordinator (matching SessionManager.close_idle "
            "semantics) or returns 429 if every slot is non-idle.",
        ),
        SettingDef(
            "coordinator.session_jwt_ttl_seconds",
            "int",
            300,
            "TTL (seconds) of the per-session coordinator JWT",
            "coordinator",
            min_value=30,
            max_value=3600,
            help="Lifetime of the JWT the console mints for each coordinator session's "
            "outbound tool calls. The token is refreshed lazily before expiry. Keep "
            "this short (5 minutes default) to limit blast radius if the process "
            "is compromised; longer values reduce JWT re-mint frequency at minor risk.",
        ),
    ]
    return {d.key: d for d in defs}


SETTINGS: dict[str, SettingDef] = _build_registry()

# Sections that are NOT in the registry (bootstrap-critical)
BOOTSTRAP_SECTIONS: frozenset[str] = frozenset(
    {
        "api",
        "database",
        "auth",
        "console",
    },
)


def validate_key(key: str) -> SettingDef:
    """Return the SettingDef for *key*, or raise ValueError if unknown."""
    defn = SETTINGS.get(key)
    if defn is None:
        raise ValueError(f"Unknown setting: {key}")
    return defn


def validate_value(key: str, raw_value: Any) -> Any:
    """Coerce and validate *raw_value* against the setting definition.

    Returns the typed value.  Raises ValueError on invalid input.
    """
    defn = validate_key(key)

    # Type coercion
    try:
        if defn.type == "int":
            typed: Any = int(raw_value)
        elif defn.type == "float":
            typed = float(raw_value)
        elif defn.type == "bool":
            if isinstance(raw_value, bool):
                typed = raw_value
            elif isinstance(raw_value, str):
                low = raw_value.lower()
                if low in ("true", "1", "yes"):
                    typed = True
                elif low in ("false", "0", "no"):
                    typed = False
                else:
                    raise ValueError(f"Cannot convert {raw_value!r} to bool for {key}")
            else:
                typed = bool(raw_value)
        else:  # str
            typed = "" if raw_value is None else str(raw_value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot convert {raw_value!r} to {defn.type} for {key}") from exc

    # Range validation
    if typed is not None:
        if (
            defn.min_value is not None
            and isinstance(typed, (int, float))
            and typed < defn.min_value
        ):
            raise ValueError(f"{key}: {typed} < minimum {defn.min_value}")
        if (
            defn.max_value is not None
            and isinstance(typed, (int, float))
            and typed > defn.max_value
        ):
            raise ValueError(f"{key}: {typed} > maximum {defn.max_value}")

    # Choices validation
    if defn.choices is not None and typed not in defn.choices:
        raise ValueError(f"{key}: {typed!r} not in {defn.choices}")

    return typed


def serialize_value(value: Any) -> str:
    """JSON-encode a typed value for storage."""
    return json.dumps(value)


def deserialize_value(key: str, json_str: str) -> Any:
    """JSON-decode and type-coerce against registry."""
    raw = json.loads(json_str)
    defn = SETTINGS.get(key)
    if defn is None:
        return raw  # Unknown key — return raw
    return validate_value(key, raw)
