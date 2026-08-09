"""Intent validation judge — heuristic and LLM-based advisory verdicts.

Evaluates non-auto-approved tool calls to produce structured verdicts that
inform (but do not replace) the human approval decision.  The heuristic tier
is a fast, pure-function rule engine with zero external dependencies.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from turnstone.core.deadline import (
    DeadlineCancelledError,
    DeadlineExceededError,
    run_abortable_with_deadline,
)
from turnstone.core.log import get_logger
from turnstone.core.model_backend_auth import BackendAuthUnavailableError
from turnstone.core.model_registry import ModelClientConstructionError
from turnstone.core.model_turn import (
    ModelLane,
    ResolvedModelBinding,
    model_turn,
    require_lane_capabilities,
    resolve_lane,
    resolve_model_binding,
    same_model_lane_binding,
)
from turnstone.core.trajectory import Turn

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.deadline import StreamAbortRef
    from turnstone.core.model_registry import ModelConfig
    from turnstone.core.model_turn import ModelTurnResult

log = get_logger(__name__)

_MAX_PARALLEL_EVALUATIONS = 16
_JUDGE_READ_LIMIT = 32_768
_JUDGE_DIRECTORY_LIMIT = 200

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentVerdict:
    """Structured verdict from intent validation."""

    verdict_id: str
    call_id: str
    func_name: str
    intent_summary: str
    risk_level: str  # "low" | "medium" | "high" | "critical"
    confidence: float  # 0.0 - 1.0
    recommendation: str  # "approve" | "review" | "deny"
    reasoning: str
    func_args: str = ""  # JSON string of tool arguments
    evidence: list[str] = field(default_factory=list)
    tier: str = "heuristic"  # "heuristic" | "llm" | "arbitrated"
    judge_model: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        """Serialize for SSE/JSON transport."""
        return {
            "verdict_id": self.verdict_id,
            "call_id": self.call_id,
            "func_name": self.func_name,
            "func_args": self.func_args,
            "intent_summary": self.intent_summary,
            "risk_level": self.risk_level,
            "confidence": self.confidence,
            "recommendation": self.recommendation,
            "reasoning": self.reasoning,
            "evidence": list(self.evidence),
            "tier": self.tier,
            "judge_model": self.judge_model,
            "latency_ms": self.latency_ms,
        }


@dataclass
class JudgeConfig:
    """Configuration for the intent validation judge.

    The *timeout* value applies **per turn**, not as a total budget across
    all turns.  With the default of 120 s and a maximum of 5 turns, a
    single tool-call evaluation can take up to 600 s in the worst case
    (e.g. a multi-turn tool-use exchange with a slow local model).
    """

    enabled: bool = True
    model: str = ""  # empty = use session model
    smart_approvals: bool = False  # auto-approve high-confidence "approve" LLM verdicts
    confidence_threshold: float = 0.95  # Smart Approvals auto-approve bar (recommendation=approve)
    max_context_ratio: float = 0.5
    timeout: float = 120.0  # per-turn timeout in seconds (see class docstring)
    read_only_tools: bool = True
    output_guard: bool = True
    output_guard_budget_seconds: float = 30.0  # wall-clock budget for output_guard regex scan
    output_guard_llm: bool = False  # enable LLM stage on tool output (issue #560 mitigation #1)
    output_guard_model: str = ""  # alias for the LLM stage; empty = inherit session model
    output_guard_llm_timeout: float = 60.0  # wall-clock budget for the LLM stage
    redact_secrets: bool = True
    # True = the approval gate's resolution aborts remaining evaluations
    # (saves inference; undone items degrade to ``llm_fallback`` verdicts
    # carrying the heuristic content).
    # False (default) = the daemon runs every item to completion; only a
    # generation supersede (next batch) or session close aborts it.
    cancel_on_approval: bool = False
    # Maximum tool-call evaluations this judge may run concurrently within
    # one approval batch.  The selected model alias's admission limit remains
    # the process-wide ceiling across judge and non-judge traffic.
    parallel_evaluations: int = 1

    def __post_init__(self) -> None:
        if type(self.parallel_evaluations) is not int or not (
            1 <= self.parallel_evaluations <= _MAX_PARALLEL_EVALUATIONS
        ):
            raise ValueError(
                "judge.parallel_evaluations must be an integer between 1 and "
                f"{_MAX_PARALLEL_EVALUATIONS}"
            )


@dataclass
class _JudgeWorkOutcome:
    """One indexed worker result awaiting coordinator delivery."""

    index: int
    verdict: IntentVerdict | None
    fallback_reason: str
    acknowledged: threading.Event = field(default_factory=threading.Event)


class _JudgeBatchCancelEvent(threading.Event):
    """Private batch abort composed with the caller-owned cancel event."""

    def __init__(self, upstream: threading.Event | None) -> None:
        super().__init__()
        self._upstream = upstream

    def is_set(self) -> bool:
        return super().is_set() or bool(self._upstream and self._upstream.is_set())

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for either the private abort or the upstream event."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            super().wait(0.05 if remaining is None else min(0.05, remaining))
        return True


# ---------------------------------------------------------------------------
# Heuristic rule table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HeuristicRule:
    """A single heuristic pattern-matching rule."""

    name: str
    risk_level: str  # low/medium/high/critical
    confidence: float  # 0.0-1.0
    recommendation: str  # approve/review/deny
    tool_pattern: str  # fnmatch pattern for func_name/approval_label
    arg_patterns: list[str]  # regex patterns matched against stringified args
    intent_template: str  # may use {func_name}, {arg_snippet}
    reasoning_template: str


# -- Critical (confidence 0.90, deny) --------------------------------------

_CRITICAL_RULES: list[_HeuristicRule] = [
    _HeuristicRule(
        name="rm-root",
        risk_level="critical",
        confidence=0.90,
        recommendation="deny",
        tool_pattern="bash",
        arg_patterns=[
            r"rm\s+(-[a-z]*f[a-z]*\s+)?/(etc|usr|var|home|opt|root|boot|lib|bin|sbin|dev|proc|sys)\b",
            r"rm\s+(-[a-z]*f[a-z]*\s+)?/\s",  # bare "rm -rf / "
            r"rm\s+(-[a-z]*f[a-z]*\s+)?/$",  # bare "rm -rf /"
        ],
        intent_template="Destructive removal targeting system paths: {arg_snippet}",
        reasoning_template="Command attempts to remove files from critical system directories.",
    ),
    _HeuristicRule(
        name="disk-wipe",
        risk_level="critical",
        confidence=0.90,
        recommendation="deny",
        tool_pattern="bash",
        arg_patterns=[r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\{\s*:\|:&\s*\};:"],
        intent_template="Potentially destructive system command: {arg_snippet}",
        reasoning_template="Command matches a known destructive pattern (mkfs, dd, or fork bomb).",
    ),
    _HeuristicRule(
        name="pipe-to-shell",
        risk_level="critical",
        confidence=0.90,
        recommendation="deny",
        tool_pattern="bash",
        arg_patterns=[r"(curl|wget).*\|\s*(ba)?sh"],
        intent_template="Remote code execution via pipe to shell: {arg_snippet}",
        reasoning_template="Piping content from the internet directly into a shell interpreter.",
    ),
    _HeuristicRule(
        name="chmod-777-root",
        risk_level="critical",
        confidence=0.90,
        recommendation="deny",
        tool_pattern="bash",
        arg_patterns=[r"chmod\s+(-[a-zA-Z]*\s+)?[0-7]?777\s+/"],
        intent_template="Overly permissive chmod on root path: {arg_snippet}",
        reasoning_template="Setting 777 permissions on root-level paths is a serious security risk.",
    ),
    _HeuristicRule(
        name="write-system-path",
        risk_level="critical",
        confidence=0.90,
        recommendation="deny",
        tool_pattern="write_file",
        arg_patterns=[r"(/etc/|/usr/|~/\.ssh/|authorized_keys)"],
        intent_template="Write to sensitive system path: {arg_snippet}",
        reasoning_template="Writing to system configuration or SSH key paths.",
    ),
    _HeuristicRule(
        name="edit-system-path",
        risk_level="critical",
        confidence=0.90,
        recommendation="deny",
        tool_pattern="edit_file",
        arg_patterns=[r"(/etc/|/usr/|~/\.ssh/|authorized_keys)"],
        intent_template="Edit of sensitive system path: {arg_snippet}",
        reasoning_template="Editing system configuration or SSH key paths.",
    ),
    _HeuristicRule(
        name="download-exec",
        risk_level="critical",
        confidence=0.90,
        recommendation="deny",
        tool_pattern="bash",
        arg_patterns=[
            r"(curl|wget)\s+.*-o\s+\S+.*&&.*(chmod\s+\+x|bash|sh|python3?|node)(\s|$)",
            r"(curl|wget)\s+\S+.*&&\s*(bash|sh|python3?|node)(\s|$)",
        ],
        intent_template="Download-then-execute chain: {arg_snippet}",
        reasoning_template=(
            "Command downloads a remote file then executes it. "
            "This is a two-step variant of pipe-to-shell."
        ),
    ),
    _HeuristicRule(
        name="proc-environ-exfil",
        risk_level="critical",
        confidence=0.95,
        recommendation="deny",
        tool_pattern="bash",
        arg_patterns=[r"/proc/\d+/environ", r"/proc/self/environ"],
        intent_template="Process environment exfiltration: {arg_snippet}",
        reasoning_template=(
            "Reading /proc/*/environ exposes all environment variables of the "
            "target process, which may include database credentials, API keys, "
            "and JWT secrets. This is a credential exfiltration vector."
        ),
    ),
]

# -- High (confidence 0.80, review) ----------------------------------------

_HIGH_RULES: list[_HeuristicRule] = [
    _HeuristicRule(
        name="sudo-su",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[r"\bsudo\s", r"\bsu\s"],
        intent_template="Elevated privilege command: {arg_snippet}",
        reasoning_template="Command uses sudo or su to elevate privileges.",
    ),
    _HeuristicRule(
        name="kill-signal",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[r"\bkill\s+-9\b", r"\bkillall\b"],
        intent_template="Force-kill process: {arg_snippet}",
        reasoning_template="Sending SIGKILL or killall can cause data loss in running processes.",
    ),
    _HeuristicRule(
        name="destructive-git",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"\bgit\s+(reset\s+--hard|push\s+--force|push\s+-f|clean\s+-[a-z]*f)",
        ],
        intent_template="Destructive git operation: {arg_snippet}",
        reasoning_template="Command performs an irreversible git operation (reset --hard, force push, or clean).",
    ),
    _HeuristicRule(
        name="sql-destructive",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[r"DROP\s+TABLE", r"DROP\s+DATABASE", r"TRUNCATE\s+TABLE"],
        intent_template="Destructive SQL statement: {arg_snippet}",
        reasoning_template="Command contains a SQL statement that permanently deletes data.",
    ),
    _HeuristicRule(
        name="write-secrets",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="write_file",
        arg_patterns=[r"\.env\b", r"credentials", r"secret", r"\.pem\b", r"\.key\b"],
        intent_template="Write to sensitive file: {arg_snippet}",
        reasoning_template="Target path matches a pattern associated with secrets or credentials.",
    ),
    _HeuristicRule(
        name="edit-secrets",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="edit_file",
        arg_patterns=[r"\.env\b", r"credentials", r"secret", r"\.pem\b", r"\.key\b"],
        intent_template="Edit of sensitive file: {arg_snippet}",
        reasoning_template="Target path matches a pattern associated with secrets or credentials.",
    ),
    _HeuristicRule(
        name="http-mutation",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[r"curl\s+(-[a-zA-Z]*\s+)*-X\s+(POST|PUT|DELETE|PATCH)"],
        intent_template="HTTP mutation request: {arg_snippet}",
        reasoning_template="Command sends a non-GET HTTP request that may modify remote state.",
    ),
    _HeuristicRule(
        name="remote-access",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[r"\bssh\s", r"\bscp\s"],
        intent_template="Remote access command: {arg_snippet}",
        reasoning_template="Command initiates a remote SSH or SCP connection.",
    ),
    _HeuristicRule(
        name="credential-recon",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"/etc/passwd\b",
            r"/etc/shadow\b",
            r"/etc/master\.passwd\b",
            r"/etc/security/passwd\b",
        ],
        intent_template="Credential file access: {arg_snippet}",
        reasoning_template=(
            "Command accesses system credential files. Even read-only access "
            "to /etc/passwd or /etc/shadow is a reconnaissance pattern."
        ),
    ),
    _HeuristicRule(
        name="browser-data-export",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"(playwright|puppeteer|selenium|browser\.use).*(cookie|session|profile|export|sync|token)",
            r"(cookie|session|profile|export).*(playwright|puppeteer|selenium|browser\.use)",
        ],
        intent_template="Browser automation with data export: {arg_snippet}",
        reasoning_template=(
            "Combining browser automation with sensitive data access "
            "(cookies, sessions, profiles). This is operator-level capability."
        ),
    ),
    _HeuristicRule(
        name="transitive-install",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"\bnpx\s+skills\s+add\b",
            r"\bpip\s+install\s+git\+https?://",
            r"\bnpm\s+install\s+https?://",
            r"\bpip\s+install\s+--index-url\s",
        ],
        intent_template="Package install from untrusted source: {arg_snippet}",
        reasoning_template=(
            "Installing packages from URLs or git repos bypasses registry "
            "vetting. Supply chain risk is significantly higher than registry installs."
        ),
    ),
    _HeuristicRule(
        name="control-plane-mutation",
        risk_level="high",
        confidence=0.80,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"\bcrontab\s+(?!-[lhV]\b|--help\b|--version\b)",
            r"\bsystemctl\s+(enable|disable|start|stop|restart|mask|unmask)\b",
            r"\blaunchctl\s+(load|bootstrap|enable)\b",
        ],
        intent_template="Persistent system change: {arg_snippet}",
        reasoning_template=(
            "Command modifies cron schedules or systemd/launchd services. "
            "These changes persist beyond the current session."
        ),
    ),
]

# -- Medium (confidence 0.70, review) --------------------------------------

_MEDIUM_RULES: list[_HeuristicRule] = [
    _HeuristicRule(
        name="content-ingestion",
        risk_level="medium",
        confidence=0.70,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"(curl|wget)\s+\S+.*\|\s*(python3?|node|ruby|perl|php|jq)\b",
            r"(curl|wget)\s+\S+.*-O\s*-\s*\|\s*(python3?|node|ruby|perl|php|jq)\b",
        ],
        intent_template="Fetch-and-process pipeline: {arg_snippet}",
        reasoning_template=(
            "Fetching remote content and piping it into an interpreter. "
            "Third-party content can carry prompt injection or malicious payloads."
        ),
    ),
    _HeuristicRule(
        name="interpreter-exec",
        risk_level="medium",
        confidence=0.70,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"\bpython3?\s+\S+\.py\b",
            r"\bnode\s+\S+\.(js|mjs|ts)\b",
            r"\bruby\s+\S+\.rb\b",
            r"\b(ba)?sh\s+\S+\.sh\b",
        ],
        intent_template="Script execution: {arg_snippet}",
        reasoning_template=(
            "Running an interpreter on a script file whose content has not "
            "been inspected. The script may contain arbitrary operations."
        ),
    ),
    _HeuristicRule(
        name="cloud-infra-mutation",
        risk_level="medium",
        confidence=0.70,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"\b(az|gcloud)\s+(?:\S+\s+)*(apply|create|delete|destroy|scale|deploy|remove)\b",
            r"\bkubectl\s+(apply|create|delete|scale|rollout|drain|cordon)\b",
            r"\b(terraform|pulumi)\s+(apply|destroy|import)\b",
            r"\baws\s+\S+\s+(create|delete|destroy|terminate|put|remove|update|modify)\b",
        ],
        intent_template="Cloud infrastructure mutation: {arg_snippet}",
        reasoning_template=(
            "Command modifies cloud infrastructure via CLI. "
            "Distinguish from read-only cloud commands (list, show, get)."
        ),
    ),
    _HeuristicRule(
        name="package-install",
        risk_level="medium",
        confidence=0.70,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[
            r"\bpip\s+install\b",
            r"\bnpm\s+install\b",
            r"\bapt\s+install\b",
            r"\bbrew\s+install\b",
            r"\bcargo\s+install\b",
        ],
        intent_template="Package installation: {arg_snippet}",
        reasoning_template="Command installs a software package which may modify the environment.",
    ),
    _HeuristicRule(
        name="write-file-default",
        risk_level="medium",
        confidence=0.70,
        recommendation="review",
        tool_pattern="write_file",
        arg_patterns=[],  # matches any write_file call
        intent_template="File write: {arg_snippet}",
        reasoning_template="Creating or overwriting a file.",
    ),
    _HeuristicRule(
        name="mcp-tool",
        risk_level="medium",
        confidence=0.70,
        recommendation="review",
        tool_pattern="mcp__*",
        arg_patterns=[],
        intent_template="MCP tool call: {func_name}({arg_snippet})",
        reasoning_template="External MCP tool invocation requires review.",
    ),
    _HeuristicRule(
        name="docker-ops",
        risk_level="medium",
        confidence=0.70,
        recommendation="review",
        tool_pattern="bash",
        arg_patterns=[r"\bdocker\s+(run|exec|rm|stop|kill)\b"],
        intent_template="Docker container operation: {arg_snippet}",
        reasoning_template="Command performs a Docker operation that may affect running containers.",
    ),
]

# -- Low (confidence 0.85, approve) ----------------------------------------

_READ_COMMANDS_RE = re.compile(
    r"^\s*(?:ls|cat|head|tail|grep|find|echo|pwd|whoami|date|wc|file|stat|which|man)"
    r"(?:\s|$|;|\|)",
)

_LOW_RULES: list[_HeuristicRule] = [
    _HeuristicRule(
        name="read-file",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="read_file",
        arg_patterns=[],
        intent_template="Read file: {arg_snippet}",
        reasoning_template="Reading a file is a safe, read-only operation.",
    ),
    _HeuristicRule(
        name="bash-read-only",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="bash",
        arg_patterns=[],  # uses custom matcher (see _match_bash_read_only)
        intent_template="Read-only shell command: {arg_snippet}",
        reasoning_template="Command uses only read-only shell utilities.",
    ),
    _HeuristicRule(
        name="safe-builtins",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="recall",
        arg_patterns=[],
        intent_template="Memory recall: {arg_snippet}",
        reasoning_template="Recall is a read-only lookup operation.",
    ),
    _HeuristicRule(
        name="search-tool",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="search",
        arg_patterns=[],
        intent_template="Search: {arg_snippet}",
        reasoning_template="Search is a read-only operation.",
    ),
    _HeuristicRule(
        name="list-directory",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="list_directory",
        arg_patterns=[],
        intent_template="List directory: {arg_snippet}",
        reasoning_template="Listing directory contents is a read-only operation.",
    ),
    _HeuristicRule(
        name="use-prompt",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="use_prompt",
        arg_patterns=[],
        intent_template="MCP prompt: {arg_snippet}",
        reasoning_template="Using an MCP prompt template is a read-only operation.",
    ),
    _HeuristicRule(
        name="tool-search",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="tool_search",
        arg_patterns=[],
        intent_template="Tool search: {arg_snippet}",
        reasoning_template="Searching available tools is a read-only operation.",
    ),
    _HeuristicRule(
        name="read-resource",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="read_resource",
        arg_patterns=[],
        intent_template="MCP resource read: {arg_snippet}",
        reasoning_template="Reading an MCP resource is a read-only operation.",
    ),
    _HeuristicRule(
        name="web-search",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="web_search",
        arg_patterns=[],
        intent_template="Web search: {arg_snippet}",
        reasoning_template="Web search is a read-only query operation.",
    ),
]

# Ordered rule table: critical first, low last.  First match wins.
_HEURISTIC_RULES: list[_HeuristicRule] = _CRITICAL_RULES + _HIGH_RULES + _MEDIUM_RULES + _LOW_RULES


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _summarize_args(func_args: dict[str, object], max_len: int = 120) -> str:
    """Create a human-readable snippet of the tool arguments."""
    if not func_args:
        return ""

    # For bash, prefer the command text.
    if "command" in func_args:
        cmd = str(func_args["command"])
        return cmd[:max_len] + ("..." if len(cmd) > max_len else "")

    # For file tools, prefer the path.
    if "path" in func_args:
        path = str(func_args["path"])
        return path[:max_len] + ("..." if len(path) > max_len else "")

    # Generic: compact JSON.
    try:
        text = json.dumps(func_args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(func_args)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _match_tool(pattern: str, func_name: str, approval_label: str) -> bool:
    """Match a tool pattern against both func_name and approval_label."""
    return fnmatch.fnmatch(func_name, pattern) or fnmatch.fnmatch(approval_label, pattern)


def _get_arg_text(func_name: str, func_args: dict[str, object]) -> str:
    """Extract the primary text to match arg_patterns against.

    For bash tools this is the command string; for file tools the path;
    otherwise a compact JSON serialization of all args.
    """
    if func_name == "bash":
        return str(func_args.get("command", ""))
    if func_name in ("write_file", "edit_file"):
        path = str(func_args.get("path", ""))
        expanded = os.path.expanduser(path) if path else ""
        resolved = os.path.realpath(expanded) if expanded else ""
        return f"{path} {resolved}" if resolved != os.path.abspath(expanded) else path
    try:
        return json.dumps(func_args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(func_args)


def _match_bash_read_only(command: str) -> bool:
    """Return True if *command* consists only of read-only shell utilities.

    Handles simple pipelines (``cmd | cmd``) and command chains
    (``cmd && cmd``, ``cmd ; cmd``).  Each segment is checked individually.
    Rejects commands containing subshells or backtick substitutions.
    """
    # Reject subshells and backtick substitutions — can hide arbitrary commands.
    if "$(" in command or "`" in command:
        return False
    # Split on pipes, &&, ||, and semicolons.
    segments = re.split(r"\|{1,2}|&&|;", command)
    for segment in segments:
        stripped = segment.strip()
        if not stripped:
            continue
        if not _READ_COMMANDS_RE.match(stripped):
            return False
    return True


def _match_rule(
    rule: _HeuristicRule,
    func_name: str,
    func_args: dict[str, object],
    approval_label: str,
    arg_text: str,
) -> bool:
    """Return True if *rule* matches the given tool call."""
    # Tool pattern must match.
    if not _match_tool(rule.tool_pattern, func_name, approval_label):
        return False

    # Special case: bash-read-only uses a custom matcher instead of
    # arg_patterns and must NOT match when higher-severity bash rules
    # would fire.
    if rule.name == "bash-read-only":
        return _match_bash_read_only(str(func_args.get("command", "")))

    # If the rule has arg_patterns, at least one must match.
    if rule.arg_patterns:
        return any(re.search(pat, arg_text) for pat in rule.arg_patterns)

    # No arg_patterns means the tool pattern alone is sufficient.
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_heuristic(
    func_name: str,
    func_args: dict[str, object],
    approval_label: str,
    call_id: str = "",
    *,
    rules: list[_HeuristicRule] | tuple[Any, ...] | None = None,
) -> IntentVerdict:
    """Evaluate a tool call against the heuristic rule table.

    This is a pure function with no external dependencies.  It scans the
    rule table in priority order (critical -> low) and returns a verdict
    for the first matching rule.  If no rule matches, a default medium-risk
    verdict is returned.

    Args:
        func_name: The tool function name (e.g. ``"bash"``).
        func_args: Tool arguments as a dict.
        approval_label: Granular approval identifier (may differ from
            func_name for MCP tools).
        call_id: The tool call ID from the provider, used for correlation.
        rules: Optional rule list override. When provided, these rules
            are used instead of the built-in ``_HEURISTIC_RULES``.
            Accepts both ``_HeuristicRule`` and ``HeuristicRuleDef``
            instances (duck-typed on shared field names).

    Returns:
        An :class:`IntentVerdict` with tier ``"heuristic"``.
    """
    start = time.monotonic()

    arg_text = _get_arg_text(func_name, func_args)
    arg_snippet = _summarize_args(func_args)
    # Rule matching runs against the FULL args (arg_text / the func_args dict);
    # only the copy stored on the verdict — persisted + streamed, frontend-
    # unread — carries the OH CRAP backstop.  The judge prompt is unaffected.
    try:
        func_args_json = honest_truncate(
            json.dumps(func_args, ensure_ascii=False, separators=(",", ":")), _VERDICT_ARG_CAP
        )
    except (TypeError, ValueError):
        func_args_json = honest_truncate(str(func_args), _VERDICT_ARG_CAP)

    for rule in rules if rules is not None else _HEURISTIC_RULES:
        if _match_rule(rule, func_name, func_args, approval_label, arg_text):
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return IntentVerdict(
                verdict_id=uuid.uuid4().hex,
                call_id=call_id,
                func_name=func_name,
                func_args=func_args_json,
                intent_summary=rule.intent_template.format(
                    func_name=func_name,
                    arg_snippet=arg_snippet,
                ),
                risk_level=rule.risk_level,
                confidence=rule.confidence,
                recommendation=rule.recommendation,
                reasoning=rule.reasoning_template,
                evidence=[f"Matched rule: {rule.name}"],
                tier="heuristic",
                latency_ms=elapsed_ms,
            )

    # Default: no rule matched.
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return IntentVerdict(
        verdict_id=uuid.uuid4().hex,
        call_id=call_id,
        func_name=func_name,
        func_args=func_args_json,
        intent_summary=f"Unknown tool operation: {func_name}",
        risk_level="medium",
        confidence=0.5,
        recommendation="review",
        reasoning="No heuristic rule matched this tool call.",
        evidence=[],
        tier="heuristic",
        latency_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# LLM judge — constants and tool schemas
# ---------------------------------------------------------------------------

# Read-only tool definitions for the judge
_JUDGE_READ_ONLY_TOOLS: frozenset[str] = frozenset({"read_file", "list_directory"})

_JUDGE_BASH_ALLOWLIST: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "stat",
    "file",
    "wc",
    "diff",
    "git status",
    "git log",
    "git diff",
    "git show",
    "find",
    "grep",
)

_JUDGE_MAX_TURNS = 5

# Approximate characters per token for context budget estimation
_CHARS_PER_TOKEN = 3.5

# Fraction of the judge model's context window given to the pending call's
# argument surface (``func_args``) in the judge PROMPT.  Args get this slice
# (0.25); the conversation transcript gets ``max_context_ratio`` (0.5); and
# ``_prepare_context`` deducts the rendered args from the transcript budget so
# the two never jointly overrun the window.  Sized so a small-window local
# judge (a 9B model at ~40k → ~35 KB of args) still sees whole normal arguments
# and a 200k judge sees whole large file bodies — the budget scales with the
# model rather than a fixed cap that would starve one and overflow another;
# truncation happens only on genuine overflow of the real window.
_ARG_CONTEXT_RATIO = 0.25

# "OH CRAP" backstop on the argument copy that rides the VERDICT — persisted to
# ``intent_verdicts.func_args`` and streamed over SSE.  This is NOT the judge's
# view: the judge prompt lowers args whole up to its real context window (see
# ``arg_budget_chars`` / the projection in ``_evaluate_intent``).  This cap
# bounds only the record + stream, and only against a model emitting an insane
# payload — 16 KB is far above any realistic argument; beyond it the frontend
# never reads the field anyway, and the FULL args remain in the trajectory.
_VERDICT_ARG_CAP = 16384

# Conservative floor for a judge context window when no sane value resolves.
_DEFAULT_JUDGE_CONTEXT_WINDOW = 32_768


def _positive_window(*candidates: Any, floor: int = _DEFAULT_JUDGE_CONTEXT_WINDOW) -> int:
    """First positive-int context window among *candidates*, else *floor*.

    A ``context_window`` of 0 or a non-int would zero out every budget and make
    ``honest_truncate`` drop everything — a silent, total lowering failure.
    Defense-in-depth: the registry normalizes the ``0 = auto-detect`` sentinel
    to the inherited window at load time (both loader paths), so a 0 should not
    reach here — but a stray non-positive window from any source must never
    zero a budget.  Fall through such values to the next sane candidate
    (typically the session window), then a conservative floor.
    """
    for c in candidates:
        if isinstance(c, int) and c > 0:
            return c
    return floor


def _model_bindings_match(left: ResolvedModelBinding, right: ResolvedModelBinding) -> bool:
    """Whether two resolved bindings have the same judge-visible semantics.

    Registry generation is deliberately excluded: an unrelated alias edit bumps
    it without changing this judge.  Identity-sensitive plant handles retain the
    same comparison used by the session binding, while the frozen config and
    resolved lane facets catch capability, extra-parameter, and sampling changes.
    """
    return (
        same_model_lane_binding(left.lane, right.lane)
        and left.config == right.config
        and left.lane.capabilities == right.lane.capabilities
        and left.lane.extra_params == right.lane.extra_params
        and left.lane.temperature == right.lane.temperature
        and left.lane.reasoning_effort == right.lane.reasoning_effort
    )


def _config_store_version(config_store: Any | None) -> int | None:
    """Return a real ConfigStore version, or ``None`` for legacy test doubles.

    Some callers intentionally pass duck-typed stores.  In particular, a bare
    ``MagicMock`` manufactures a ``.version`` attribute on demand; treating
    that object as a generation would make every equality check depend on mock
    truthiness rather than on a monotone integer.
    """
    if config_store is None:
        return None
    try:
        version = config_store.version
    except Exception:
        log.debug("judge.config_version_read_failed", exc_info=True)
        return None
    return version if type(version) is int else None


def _judge_binding_from_session(
    session_binding: ResolvedModelBinding,
    config_store: Any | None,
) -> ResolvedModelBinding:
    """Build the session-model fallback lane with judge sampling semantics.

    Before judges held a frozen :class:`ResolvedModelBinding`, every
    evaluation called :func:`resolve_lane`: provider/client/model and
    capabilities stayed pinned to the session, while the operator sampling
    ladder was read from ConfigStore.  Rebuilding that lane at judge
    construction keeps those semantics without mutating an in-flight judge.
    """
    session_lane = session_binding.lane
    judge_lane = resolve_lane(
        session_lane.provider,
        session_lane.client,
        session_lane.model,
        alias=session_lane.alias,
        registry=session_lane.registry,
        capabilities=require_lane_capabilities(session_lane),
        cfg=session_binding.config,
        config_store=config_store,
        backend_auth_resolver=session_lane.backend_auth_resolver,
    )
    return replace(session_binding, lane=judge_lane)


@dataclass
class _JudgeBindingState:
    """Pinned judge binding plus mutable no-op generation stamps.

    The binding and lane never mutate.  The checked generations are freshness
    watermarks: after an unrelated registry or ConfigStore update resolves to
    the same binding, advancing them avoids repeating that work without
    replacing the judge or its lane.
    """

    binding: ResolvedModelBinding
    requested_alias: str
    resolved_explicitly: bool
    config_store: Any | None
    checked_registry_generation: int
    checked_config_version: int | None

    def is_current(
        self,
        session_binding: ResolvedModelBinding,
        *,
        requested_alias: str | None = None,
    ) -> bool:
        """Return whether constructing now would select this same binding.

        Explicit judge aliases are checked independently of the primary session
        alias.  An inherited/failed-alias judge instead follows the supplied
        session binding.  A generation-only unrelated edit advances only the
        watermark; a changed effective binding returns ``False`` so the session
        can replace the whole judge between evaluations.
        """
        desired = self.requested_alias if requested_alias is None else requested_alias.strip()
        if desired != self.requested_alias:
            return False

        registry = session_binding.lane.registry
        if registry is not self.binding.lane.registry:
            return False

        current_config_version = _config_store_version(self.config_store)
        if registry is None:
            current_generation = session_binding.registry_generation
        else:
            try:
                current_generation = registry.generation
            except Exception:
                log.debug("judge.binding_generation_read_failed", exc_info=True)
                return False

        # A primary /model switch need not bump the registry generation.  An
        # explicitly routed judge is independent of it; an inherited judge is
        # current only while the primary binding (projected through the judge's
        # sampling ladder) still matches.  ConfigStore has its own generation:
        # temperature/effort updates do not reload the model registry.
        generation_changed = current_generation != self.checked_registry_generation
        config_changed = current_config_version != self.checked_config_version
        if self.resolved_explicitly and not generation_changed and not config_changed:
            return True

        candidate = _judge_binding_from_session(session_binding, self.config_store)
        resolved_explicitly = False
        if registry is not None and desired and (self.resolved_explicitly or generation_changed):
            try:
                candidate = resolve_model_binding(
                    registry,
                    desired,
                    config_store=self.config_store,
                    backend_auth_resolver=session_binding.lane.backend_auth_resolver,
                )
                resolved_explicitly = True
            except (ModelClientConstructionError, ValueError, KeyError):
                # Reconstructing at this generation would take the documented
                # session-model fallback.  Compare that outcome below.
                candidate = _judge_binding_from_session(session_binding, self.config_store)
            except Exception:
                log.debug("judge.binding_refresh_failed", exc_info=True)
                return False

        if resolved_explicitly != self.resolved_explicitly:
            return False
        if not _model_bindings_match(self.binding, candidate):
            return False

        # Stamp the registry generation actually observed, not the fallback
        # candidate's session-binding stamp: an unrelated reload can leave the
        # effective primary binding unchanged while its caller-supplied stamp
        # still names the previous generation.  Re-read before committing so a
        # concurrent second reload cannot bless an unchecked generation.
        observed_registry_generation = current_generation
        if registry is not None:
            try:
                observed_registry_generation = registry.generation
            except Exception:
                log.debug("judge.binding_generation_read_failed", exc_info=True)
                return False
            if observed_registry_generation != current_generation:
                return False
        observed_config_version = _config_store_version(self.config_store)
        if observed_config_version != current_config_version:
            return False
        self.checked_registry_generation = observed_registry_generation
        self.checked_config_version = observed_config_version
        return True


def honest_truncate(text: str, budget: int) -> str:
    """Return *text* untouched when it fits *budget* characters, otherwise the
    leading ``budget`` characters followed by an explicit note of exactly how
    many characters were dropped.

    The judge reasons about the argument surface it is shown; a silent
    ``text[:400]`` slice reads to the model as *the whole argument*, which is
    how a legitimate 5-KB file write gets judged as if it were 400 bytes.  The
    marker makes the omission legible — the judge knows content continues and
    can weigh the truncation into its verdict rather than treating the fragment
    as complete.  Reason-neutral, since the same helper bounds both the judge
    prompt (fit the window) and the verdict record (the OH CRAP backstop).
    """
    if budget < 0:
        budget = 0
    if len(text) <= budget:
        return text
    omitted = len(text) - budget
    return f"{text[:budget]}…[{omitted:,} of {len(text):,} chars omitted]"


_JUDGE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file to verify what would be "
                "affected by the tool call under review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List the contents of a directory to understand what files would be affected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the directory.",
                    },
                },
                "required": ["path"],
            },
        },
    },
]

_JUDGE_SYSTEM_PROMPT = """\
You are an intent validation judge for an AI assistant's tool calls. \
Your role is to evaluate whether a proposed tool call is safe, appropriate, \
and aligned with the user's request.

You will receive:
1. Recent conversation history between a user and an AI assistant
2. A tool call the assistant wants to execute, pending human approval

You have access to read-only tools (read_file, list_directory) to gather \
evidence before rendering your verdict. Use them when the tool call involves \
file modifications — check what would be overwritten or affected.

Evaluate the tool call and respond with a JSON verdict:

```json
{
  "intent_summary": "One-sentence description of what the tool call does",
  "risk_level": "low|medium|high|critical",
  "confidence": 0.0-1.0,
  "recommendation": "approve|review|deny",
  "reasoning": "2-3 sentences explaining your assessment with specific evidence",
  "evidence": ["Quote or cite specific parts of conversation/files that informed your verdict"]
}
```

Risk level definitions:
- **low**: Read-only operations, safe modifications to expected files
- **medium**: File writes, package installs, environment changes within the project
- **high**: Destructive operations, credential access, network mutations, privileged commands
- **critical**: System-level destructive commands, root filesystem modifications, remote code execution

Recommendation guidelines:
- **approve**: Low risk, clearly aligned with user request, no concerns
- **review**: Medium risk or uncertain alignment — user should inspect carefully
- **deny**: High/critical risk with unclear justification, or clearly misaligned with user intent

Be precise and evidence-based. Do not hedge — give a clear recommendation. \
If you used read_file to check a target, cite what you found."""


# ---------------------------------------------------------------------------
# IntentJudge — session-scoped LLM judge
# ---------------------------------------------------------------------------


class IntentJudge:
    """Session-scoped LLM judge for intent validation.

    Evaluates tool calls using a three-tier pipeline:
    1. Heuristic (instant, free) — pattern-based risk classification
    2. LLM judge (async, multi-turn) — semantic evaluation with read-only tool access
    3. Arbitration — best verdict wins based on confidence

    The heuristic verdict is returned immediately. The LLM verdict arrives
    asynchronously via a callback, allowing progressive UI updates.
    """

    def __init__(
        self,
        config: JudgeConfig,
        session_binding: ResolvedModelBinding,
        rule_registry: Any | None = None,
        config_store: Any | None = None,
    ) -> None:
        self._config = config
        self._config_fingerprint = self._fingerprint_config(config)
        self._rule_registry = rule_registry
        session_caps = require_lane_capabilities(session_binding.lane)
        session_window = _positive_window(
            getattr(session_binding.config, "context_window", None),
            session_caps.context_window,
        )

        # Resolve judge model via ModelRegistry alias, otherwise self-
        # consistency on the session model.  ``judge.model`` is alias-only
        # — same contract as ``coordinator.model_alias`` /
        # ``model.task_alias``.  A non-alias value
        # used to be accepted as a raw model id pinned onto the session
        # provider, but that path silently broke whenever the session
        # provider didn't speak that model id (e.g. coordinator on
        # Anthropic, ``judge.model = "gpt-5-mini"`` → every judge call
        # returned ``llm_fallback``).  Operators register an alias
        # instead; an unknown value here logs a warning and inherits the
        # session model.
        requested_alias = str(config.model or "").strip()
        registry = session_binding.lane.registry
        config_version_at_start = _config_store_version(config_store)
        binding = _judge_binding_from_session(session_binding, config_store)
        resolved = False
        construction_error: ModelClientConstructionError | None = None
        if requested_alias and registry is not None:
            try:
                # One locked registry snapshot builds provider, client, model,
                # capabilities, extra params, and sampling facets from the SAME
                # ModelConfig.  No second config read can mix generations.
                binding = resolve_model_binding(
                    registry,
                    requested_alias,
                    config_store=config_store,
                    backend_auth_resolver=session_binding.lane.backend_auth_resolver,
                )
                resolved = True
            except ModelClientConstructionError as exc:
                construction_error = exc
            except (ValueError, KeyError):
                pass
            except Exception:
                log.debug("Model alias resolution failed for %r, falling back", requested_alias)

        if not resolved:
            if construction_error is not None:
                # The alias IS registered; its binding could not be built.
                # Same session-model fallback, but name the construction
                # cause — the register-the-alias advice below would
                # misdiagnose a row that is already registered.
                log.warning(
                    "judge.model=%r is registered but its client could not be "
                    "constructed (%s) — falling back to session model %r.",
                    requested_alias,
                    construction_error,
                    session_binding.lane.model,
                )
            elif requested_alias:
                log.warning(
                    "judge.model=%r is not a registered alias — falling back to "
                    "session model %r.  Register the model in the Models tab and "
                    "set judge.model to its alias.",
                    requested_alias,
                    session_binding.lane.model,
                )
            binding = _judge_binding_from_session(session_binding, config_store)

        self._binding_state = _JudgeBindingState(
            binding=binding,
            requested_alias=requested_alias,
            resolved_explicitly=resolved,
            config_store=config_store,
            checked_registry_generation=binding.registry_generation,
            checked_config_version=config_version_at_start,
        )
        # The semantic lane is pinned for the judge object's lifetime.  Intent
        # evaluations still substitute a fresh client per daemon worker for
        # thread isolation; no provider/model/config facet is re-resolved.
        self._lane = binding.lane
        self._model = self._lane.model
        self._capabilities = require_lane_capabilities(self._lane)
        self._client_factory_args = self._extract_client_config(
            self._lane.client,
            self._lane.provider.provider_name,
        )
        self._judge_context_window = _positive_window(
            getattr(binding.config, "context_window", None),
            session_window,
        )

    @staticmethod
    def _fingerprint_config(config: JudgeConfig) -> tuple[str, float, float, int, bool]:
        """Constructor-consumed behavior that requires a fresh judge object."""
        return (
            str(config.model or "").strip(),
            config.max_context_ratio,
            config.timeout,
            config.parallel_evaluations,
            config.read_only_tools,
        )

    def binding_is_current(
        self,
        session_binding: ResolvedModelBinding,
        config: JudgeConfig | None = None,
    ) -> bool:
        """Whether this judge can be reused for the next evaluation.

        In-flight daemon work keeps this object's pinned lane.  The session calls
        this only at the next evaluation boundary and replaces the whole object
        when it returns ``False``.
        """
        if config is not None and self._fingerprint_config(config) != self._config_fingerprint:
            return False
        return self._binding_state.is_current(session_binding)

    # -- Client lifecycle helpers -------------------------------------------

    @staticmethod
    def _extract_client_config(client: Any, provider_name: str) -> dict[str, str]:
        """Extract connection config from an existing SDK client for re-creation."""
        base_url = str(getattr(client, "base_url", getattr(client, "_base_url", "")))
        api_key = getattr(client, "api_key", "") or ""
        return {"provider_name": provider_name, "base_url": base_url, "api_key": api_key}

    def _create_client(self) -> Any:
        """Create a fresh HTTP client for a judge evaluation run."""
        from turnstone.core.providers import create_client

        return create_client(**self._client_factory_args)

    def evaluate(
        self,
        items: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        callback: Callable[[IntentVerdict], None],
        cancel_event: threading.Event | None = None,
        done_callback: Callable[[], None] | None = None,
        backend_auth_resolver: Callable[[str, ModelConfig | None], str | None] | None = None,
    ) -> list[IntentVerdict]:
        """Evaluate tool calls. Returns heuristic verdicts immediately.

        Spawns a daemon thread for the LLM judge. When the LLM verdict
        is ready, *callback* is invoked (from the daemon thread) with
        the final verdict for each item.

        Args:
            items: Prepared tool call items (each has ``func_name``,
                ``func_args``, ``approval_label``, ``call_id``).
            messages: Conversation history (OpenAI message format).
            callback: Called with each LLM verdict (or timeout/error fallback).
            cancel_event: Unconditional abort signal — when set, the
                daemon abandons remaining work and delivers
                ``llm_fallback`` verdicts (heuristic-derived) for every
                undone item.  The caller
                owns the firing policy: ChatSession fires it when a
                newer batch supersedes this generation, on session
                close, and — only when ``cancel_on_approval`` is
                enabled — as soon as the approval gate resolves.
            done_callback: Invoked exactly once from the daemon's
                ``finally`` when this generation finishes — normally or
                cancelled. Callback errors are isolated per item. ChatSession
                uses it to retire the generation's cancel event from its
                live set (parallel task agents each spawn a generation;
                ``close()`` aborts whatever is still live).
            backend_auth_resolver: Batch-scoped resolver whose closure pins
                the initiating principal.  The resolver remains on the
                batch's lane so each plant-call attempt mints after acquiring
                alias admission.  The mint cache keeps this inexpensive while
                preventing a queued bearer from expiring before dispatch.

        Returns:
            List of heuristic verdicts (one per item), available immediately.
        """
        heuristic_verdicts: list[IntentVerdict] = []
        for item in items:
            func_name = item.get("func_name", item.get("name", ""))
            func_args = item.get("func_args", {})
            if isinstance(func_args, str):
                try:
                    func_args = json.loads(func_args)
                except (json.JSONDecodeError, TypeError):
                    func_args = {}
            approval_label = item.get("approval_label", func_name)
            call_id = item.get("call_id", item.get("tool_call_id", ""))

            registry_rules = self._rule_registry.heuristic_rules if self._rule_registry else None
            verdict = evaluate_heuristic(
                func_name, func_args, approval_label, call_id, rules=registry_rules
            )
            heuristic_verdicts.append(verdict)

        # Spawn daemon thread for LLM judge
        thread = threading.Thread(
            target=self._run_judge,
            args=(
                items,
                messages,
                heuristic_verdicts,
                callback,
                cancel_event,
                done_callback,
                backend_auth_resolver,
            ),
            daemon=True,
            name="intent-judge",
        )
        thread.start()

        return heuristic_verdicts

    def _run_judge(
        self,
        items: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        heuristic_verdicts: list[IntentVerdict],
        callback: Callable[[IntentVerdict], None],
        cancel_event: threading.Event | None = None,
        done_callback: Callable[[], None] | None = None,
        backend_auth_resolver: Callable[[str, ModelConfig | None], str | None] | None = None,
    ) -> None:
        """Daemon coordinator: run bounded LLM evaluations and invoke callback.

        ``cancel_event`` is an unconditional abort signal: once it fires,
        in-flight work stops and every remaining item is delivered as an
        ``llm_fallback`` verdict — the heuristic verdict's content,
        relabeled (each call still gets exactly one
        verdict — Smart Approvals and the advisory UI both rely on the
        full set arriving).  Which actor fires the event is the CALLER's
        policy, not this loop's: ChatSession fires it at approval
        resolution only when ``cancel_on_approval`` is enabled, and
        always when the next batch supersedes this generation or the
        session closes.  With ``cancel_on_approval=False`` (default) and
        no supersede, every evaluation runs to completion so all
        verdicts are delivered.
        """
        try:
            if cancel_event and cancel_event.is_set():
                self._deliver_fallbacks(
                    items,
                    heuristic_verdicts,
                    callback,
                    "judge cancelled before evaluating this call",
                )
                return

            if not items:
                return

            # Preserve the caller-pinned principal on the batch lane, but let
            # model_turn resolve credentials only after alias admission.  An
            # admission backlog can outlive a bearer token; carrying the
            # resolver refreshes near-expiry tokens at the dispatch boundary
            # without allowing a later shared-workstream actor to take over.
            batch_lane = self._lane
            if backend_auth_resolver is not None:
                batch_lane = replace(
                    batch_lane,
                    backend_auth_resolver=backend_auth_resolver,
                )

            if cancel_event and cancel_event.is_set():
                self._deliver_fallbacks(
                    items,
                    heuristic_verdicts,
                    callback,
                    "judge cancelled before evaluating this call",
                )
                return

            width = min(
                max(1, int(self._config.parallel_evaluations)),
                _MAX_PARALLEL_EVALUATIONS,
                len(items),
            )
            alias_limit = batch_lane.admission.limit if batch_lane.admission is not None else 0
            if alias_limit > 0:
                width = min(width, alias_limit)
            log.info(
                "judge.batch.start",
                items=len(items),
                parallel_evaluations=width,
                configured_parallel_evaluations=self._config.parallel_evaluations,
                model_alias=batch_lane.alias,
                model_alias_limit=alias_limit,
            )
            work: queue.Queue[int | None] = queue.Queue()
            outcomes: queue.Queue[_JudgeWorkOutcome] = queue.Queue()
            batch_cancel = _JudgeBatchCancelEvent(cancel_event)
            abort_lock = threading.Lock()
            abort_reason = ""

            for idx in range(len(items)):
                work.put(idx)
            for _ in range(width):
                work.put(None)

            def _current_abort_reason() -> str:
                if cancel_event is not None and cancel_event.is_set():
                    return "judge cancelled before evaluating this call"
                with abort_lock:
                    return abort_reason

            def _abort_batch(reason: str) -> bool:
                nonlocal abort_reason
                with abort_lock:
                    first = not abort_reason
                    if first:
                        abort_reason = reason
                batch_cancel.set()
                return first

            def _worker() -> None:
                client: Any | None = None
                try:
                    while True:
                        idx = work.get()
                        if idx is None:
                            work.task_done()
                            return
                        outcome: _JudgeWorkOutcome
                        try:
                            reason = _current_abort_reason()
                            if reason:
                                outcome = _JudgeWorkOutcome(idx, None, reason)
                            else:
                                if client is None:
                                    try:
                                        client = self._create_client()
                                    except BaseException:  # noqa: BLE001 - contain daemon failure
                                        reason = "judge client initialization failed"
                                        if _abort_batch(reason):
                                            log.exception("Judge client initialization failed")
                                        outcome = _JudgeWorkOutcome(idx, None, reason)
                                if client is not None:
                                    worker_lane = replace(batch_lane, client=client)
                                    try:
                                        verdict = self._evaluate_single(
                                            items[idx],
                                            messages,
                                            batch_cancel,
                                            client,
                                            lane=worker_lane,
                                        )
                                        reason = _current_abort_reason()
                                        outcome = _JudgeWorkOutcome(
                                            idx,
                                            verdict,
                                            reason
                                            or (
                                                ""
                                                if verdict is not None
                                                else "LLM judge did not return a verdict"
                                            ),
                                        )
                                    except BackendAuthUnavailableError:
                                        reason = "judge backend authentication failed"
                                        if _abort_batch(reason):
                                            log.exception("Judge backend authentication failed")
                                        outcome = _JudgeWorkOutcome(idx, None, reason)
                                    except BaseException:  # noqa: BLE001 - contain daemon failure
                                        log.exception(
                                            "Judge evaluation failed for %s",
                                            items[idx].get("func_name", "?"),
                                        )
                                        outcome = _JudgeWorkOutcome(
                                            idx,
                                            None,
                                            "judge evaluation error",
                                        )
                        finally:
                            work.task_done()
                        outcomes.put(outcome)
                        # Callback delivery is the cancellation commit point.
                        # Do not refill this worker until the coordinator has
                        # invoked the callback and observed any resulting abort.
                        outcome.acknowledged.wait()
                finally:
                    try:
                        if client is not None and hasattr(client, "close"):
                            client.close()
                    except Exception:
                        log.debug("judge.client_close_failed", exc_info=True)

            workers: list[threading.Thread] = []
            try:
                for idx in range(width):
                    worker = threading.Thread(
                        target=_worker,
                        daemon=True,
                        name=f"intent-judge-eval-{idx + 1}",
                    )
                    worker.start()
                    workers.append(worker)
            except BaseException:  # noqa: BLE001 - preserve exact-once fallbacks
                reason = "judge worker initialization failed"
                _abort_batch(reason)
                log.exception("Judge worker initialization failed")
            if not workers:
                self._deliver_fallbacks(items, heuristic_verdicts, callback, abort_reason)
                return

            delivered: set[int] = set()
            for _ in range(len(items)):
                outcome = outcomes.get()
                try:
                    if outcome.index in delivered:
                        log.error("judge.verdict.duplicate", index=outcome.index)
                        continue
                    delivered.add(outcome.index)
                    h_verdict = heuristic_verdicts[outcome.index]
                    verdict = outcome.verdict or self._fallback_verdict(
                        h_verdict,
                        outcome.fallback_reason,
                    )
                    log.info(
                        "judge.verdict.llm"
                        if outcome.verdict is not None
                        else "judge.verdict.fallback",
                        recommendation=verdict.recommendation,
                        confidence=verdict.confidence,
                        call_id=verdict.call_id,
                    )
                    try:
                        callback(verdict)
                    except Exception:
                        log.debug("judge.verdict_delivery_failed", exc_info=True)
                finally:
                    outcome.acknowledged.set()

            for worker in workers:
                worker.join()
        finally:
            if done_callback is not None:
                try:
                    done_callback()
                except Exception:
                    log.debug("judge.done_callback_failed", exc_info=True)

    def _deliver_fallbacks(
        self,
        remaining_items: list[dict[str, Any]],
        remaining_verdicts: list[IntentVerdict],
        callback: Callable[[IntentVerdict], None],
        reason: str,
    ) -> None:
        """Deliver ``llm_fallback`` verdicts (heuristic content) for items the judge didn't complete."""
        for _item, h_verdict in zip(remaining_items, remaining_verdicts, strict=True):
            try:
                callback(self._fallback_verdict(h_verdict, reason))
            except Exception:
                log.debug("judge.verdict_delivery_failed", exc_info=True)

    def _fallback_verdict(self, h_verdict: IntentVerdict, reason: str) -> IntentVerdict:
        """Relabel one heuristic verdict as an LLM fallback."""
        return IntentVerdict(
            verdict_id=h_verdict.verdict_id,
            call_id=h_verdict.call_id,
            func_name=h_verdict.func_name,
            func_args=h_verdict.func_args,
            intent_summary=h_verdict.intent_summary,
            risk_level=h_verdict.risk_level,
            confidence=h_verdict.confidence,
            recommendation=h_verdict.recommendation,
            reasoning=h_verdict.reasoning + f" ({reason})",
            evidence=h_verdict.evidence,
            tier="llm_fallback",
            judge_model=self._model,
            latency_ms=h_verdict.latency_ms,
        )

    def _evaluate_single(
        self,
        item: dict[str, Any],
        messages: list[dict[str, Any]],
        cancel_event: threading.Event | None,
        client: Any | None,
        *,
        lane: ModelLane | None = None,
    ) -> IntentVerdict | None:
        """Run LLM judge for a single tool call. Returns verdict or None."""
        if lane is None:
            if client is None:
                raise ValueError("intent judge evaluation requires a client or pinned lane")
            lane = replace(self._lane, client=client)
        start = time.monotonic()
        func_name = item.get("func_name", item.get("name", ""))
        func_args = item.get("func_args", {})
        if isinstance(func_args, str):
            try:
                func_args = json.loads(func_args)
            except (json.JSONDecodeError, TypeError):
                func_args = {}
        call_id = item.get("call_id", item.get("tool_call_id", ""))
        # ``func_args_json`` is the verdict's record copy (persisted + streamed,
        # frontend-unread) — OH CRAP backstop only.  The judge PROMPT gets the
        # full window-scaled projection via ``_prepare_context(item, ...)``.
        try:
            func_args_json = honest_truncate(
                json.dumps(func_args, ensure_ascii=False, separators=(",", ":")), _VERDICT_ARG_CAP
            )
        except (TypeError, ValueError):
            func_args_json = honest_truncate(str(func_args), _VERDICT_ARG_CAP)

        # Prepare context (Turn IR — lowered per call inside model_turn)
        judge_turns = self._prepare_context(item, messages)

        # Prepare tools (only if read_only_tools enabled).
        # Raw OpenAI-format schemas — the provider adapter converts them.
        # These are γ-side instruments, deliberately OUTSIDE the persona
        # envelope: middle-rank config must not be able to blind the gate's
        # evidence gathering.  The old provider_name == "google" skip is gone:
        # the judge's trajectory now carries the provider-native lane
        # (thought_signature rides ``provider_blocks`` and is reconstructed by
        # the Google adapter), so the Gemini judge runs the same evidence loop
        # as every other provider.
        tools: list[dict[str, Any]] | None = None
        if self._config.read_only_tools:
            tools = list(_JUDGE_TOOL_SCHEMAS)

        # ``lane`` is the constructor-pinned binding with only the worker's
        # fresh client substituted.  No registry/config facet is re-resolved
        # inside an evaluation; window sizing and wire capabilities therefore
        # cannot disagree.

        # Multi-turn judge loop
        result = None  # will hold the last ModelTurnResult
        empty_retries = 0  # track consecutive empty responses for retry
        turn = 0

        while turn < _JUDGE_MAX_TURNS:
            log.info(
                "judge.turn.start",
                turn=turn + 1,
                max_turns=_JUDGE_MAX_TURNS,
                func_name=func_name,
                call_id=call_id[:8],
            )

            turn_start = time.monotonic()

            is_last_turn = turn == _JUDGE_MAX_TURNS - 1

            # On the last turn, strip tools and inject a forcing message
            # so the model knows it must render a verdict now.
            if is_last_turn:
                judge_turns.append(
                    Turn.user(
                        "You have gathered enough evidence. "
                        "You MUST now render your final verdict as JSON. "
                        "No more tool calls."
                    )
                )

            # Per-turn timeout: each turn gets a fresh budget so local
            # models aren't penalised for slow earlier turns.
            per_call_timeout = max(self._config.timeout, 5.0)  # at least 5s
            try:
                # Each turn runs on its own daemon worker (1s cancel polling).
                # A timeout or cancel abandons the call without pinning a
                # non-daemon thread that would block interpreter exit — the old
                # single-slot ThreadPoolExecutor left a stuck worker that
                # poisoned the pool, which is why the restart dance existed.
                # The abort wiring (fresh ref per turn) closes the abandoned
                # worker's HTTP stream so the read raises promptly.
                # Temperature is deliberately NOT pinned (house rule): the
                # lane inherits the judge model's configured temperature —
                # many modern models misbehave below 1.0, so the model's own
                # configuration beats a hard determinism pin.
                turn_tools = None if is_last_turn else tools

                # Bound default: the call runs synchronously within this
                # iteration; the binding makes the per-turn capture explicit
                # (and satisfies B023 in the loop).
                def _sample(
                    ref: StreamAbortRef, _tools: list[dict[str, Any]] | None = turn_tools
                ) -> ModelTurnResult:
                    return model_turn(
                        lane,
                        judge_turns,
                        tools=_tools,
                        max_tokens=2048,
                        cancel_ref=ref,
                    )

                result = run_abortable_with_deadline(
                    _sample,
                    timeout=per_call_timeout,
                    cancel_event=cancel_event,
                    thread_name="judge-api",
                )
            except DeadlineCancelledError:
                return None
            except DeadlineExceededError:
                log.info("judge.turn.timeout", turn=turn + 1, timeout=per_call_timeout)
                # Safety net: if we have a partial result from a previous turn,
                # try to parse a verdict from it before giving up.
                if result and result.content:
                    verdict = self._parse_verdict(
                        result.content,
                        func_name,
                        call_id,
                        int((time.monotonic() - start) * 1000),
                        func_args=func_args_json,
                    )
                    if verdict:
                        log.info("judge.verdict.from_partial", turn=turn + 1)
                        return verdict
                return None
            except BackendAuthUnavailableError:
                raise
            except Exception as e:
                log.info("judge.turn.failed", turn=turn + 1, error=str(e))
                return None

            turn_elapsed = time.monotonic() - turn_start
            log.info(
                "judge.turn.response",
                turn=turn + 1,
                chars=len(result.content or ""),
                tools=len(result.tool_calls or []),
                elapsed=round(turn_elapsed, 1),
            )

            # Reset empty-response counter after any non-empty response
            if result.content or result.tool_calls:
                empty_retries = 0

            # Check for tool calls
            if result.tool_calls:
                # Append the assistant turn — the native lane rides along
                # (Gemini thought_signature, Anthropic thinking, Responses
                # reasoning items), so the next lowering replays it and the
                # evidence loop keeps its reasoning continuity.  The judge
                # never mints ids: its trajectory is ephemeral and pinned to
                # one provider, so provider-original ids stay consistent
                # between the native blocks, the mirror, and the results.
                judge_turns.append(result.turn)
                for tc in result.tool_calls:
                    tc_func = tc.get("function", {})
                    tc_name = tc_func.get("name", "")
                    tc_args_str = tc_func.get("arguments", "{}")
                    try:
                        tc_args = (
                            json.loads(tc_args_str) if isinstance(tc_args_str, str) else tc_args_str
                        )
                    except (json.JSONDecodeError, TypeError):
                        tc_args = {}

                    tool_result = self._exec_read_only_tool(tc_name, tc_args)
                    judge_turns.append(Turn.tool(tc.get("id", ""), tool_result))
                turn += 1
                continue

            # No tool calls — parse the verdict from content
            if result.content:
                verdict = self._parse_verdict(
                    result.content,
                    func_name,
                    call_id,
                    int((time.monotonic() - start) * 1000),
                    func_args=func_args_json,
                )
                if verdict:
                    log.info(
                        "judge.verdict.success",
                        recommendation=verdict.recommendation,
                        confidence=verdict.confidence,
                    )
                    return verdict
                # Model produced text but no parseable verdict — on last turn
                # this means the model refused to comply with the forcing message.
                if is_last_turn:
                    log.warning(
                        "Judge returned unparseable response on final turn: %.200s",
                        result.content,
                    )
                    return None
                # On earlier turns, inject a nudge and continue
                judge_turns.append(result.turn)
                judge_turns.append(
                    Turn.user(
                        "Your response was not valid JSON. "
                        "Please respond ONLY with the JSON verdict object."
                    )
                )
                turn += 1
                continue

            # Empty response (0 chars, 0 tools).  If the model hit the
            # output token limit the finish_reason will be "length" — retrying
            # with the same prompt and max_tokens is pointless.
            if result.finish_reason == "length":
                log.info("judge.empty_response.length_stop", turn=turn + 1)
                return None

            # Transient empty response — retry up to 3 times without
            # consuming the turn budget.
            empty_retries += 1
            if empty_retries <= 3:
                log.info("judge.empty_response.retry", retry=empty_retries, max_retries=3)
                judge_turns.append(
                    Turn.user(
                        "You returned an empty response. "
                        "Please analyze the tool call and respond with "
                        "the JSON verdict object."
                    )
                )
                continue
            log.info("judge.empty_response.giving_up", retries=empty_retries)
            return None

        # Max turns reached without a final verdict
        log.warning(
            "Judge reached max turns (%d) without final verdict",
            _JUDGE_MAX_TURNS,
        )
        return None

    def arg_budget_chars(self) -> int:
        """Character budget for the pending call's projected ``func_args``.

        Scales with the judge model's context window (see
        :data:`_ARG_CONTEXT_RATIO`) so callers can truncate large argument
        fields — a file body, a batch of edits — to what *this* judge can
        actually read, rather than a fixed cap that starves a 200k model and
        overflows a 4k one.  Used by the session's projection step; the judge
        re-shares the same window in :meth:`_prepare_context`.  No fixed
        ceiling: args lower whole up to the judge's real window, and only a
        genuine window overflow forces an honest, marked truncation — the
        record/stream copy is bounded separately (see :data:`_VERDICT_ARG_CAP`).
        """
        return int(self._judge_context_window * _ARG_CONTEXT_RATIO * _CHARS_PER_TOKEN)

    def _prepare_context(
        self,
        item: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> list[Turn]:
        """Build the judge's opening trajectory with FIFO-truncated conversation.

        *messages* is the session's wire-dict history (read-only input: the
        judge observes the session, it does not join it); the output is Turn
        IR — the judge's own ephemeral trajectory, lowered per call by
        ``model_turn``.  The flattened single-user-message transcript is the
        judge's deliberate π-projection, not a lowering artifact: the judge
        evaluates a projection of the conversation, and strict providers
        reject the raw multi-turn role sequence out of context.
        """
        # Build user message with tool call details
        func_name = item.get("func_name", item.get("name", ""))
        func_args = item.get("func_args", {})
        if isinstance(func_args, str):
            try:
                func_args = json.loads(func_args)
            except (json.JSONDecodeError, TypeError):
                func_args = {}
        approval_label = item.get("approval_label", func_name)

        tool_detail = (
            f"Tool: {func_name}\n"
            f"Approval label: {approval_label}\n"
            f"Arguments:\n```json\n"
            f"{json.dumps(func_args, indent=2, ensure_ascii=False)}\n```"
        )

        # Calculate the character budget for conversation history.  The
        # arguments (``tool_detail``) and the transcript share the same
        # context slice, so deduct what the arguments already consume — a
        # large but budgeted write/edit shrinks the history it competes with
        # instead of pushing the prompt past the window.  Floor keeps at least
        # a minimal transcript even when the arguments are unusually large.
        budget_tokens = int(self._judge_context_window * self._config.max_context_ratio)
        budget_chars = int(budget_tokens * _CHARS_PER_TOKEN)
        budget_chars = max(budget_chars - len(tool_detail), budget_chars // 5)

        # Trim to messages from the last user message onward — the judge
        # only needs the immediate request context, not the full history.
        # This keeps latency bounded as conversations grow.
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break
        recent = messages[last_user_idx:] if last_user_idx is not None else messages

        # Apply FIFO budget cap on the trimmed context
        truncated: list[dict[str, Any]] = []
        total_chars = 0
        for msg in reversed(recent):
            content = msg.get("content", "") or ""
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            msg_chars = len(str(content)) + len(str(msg.get("role", "")))
            if total_chars + msg_chars > budget_chars:
                break
            truncated.append(msg)
            total_chars += msg_chars
        truncated.reverse()

        # Flatten history into a plaintext transcript inside a single user
        # message.  This avoids multi-turn role sequences (consecutive user/
        # assistant messages, tool results without matching tool_calls) that
        # strict providers like Google reject with schema validation errors.
        transcript_lines: list[str] = []
        for msg in truncated:
            role = msg["role"]
            content = msg.get("content", "")

            if content is not None:
                content_str = content if isinstance(content, str) else str(content)
            else:
                content_str = ""

            if role == "tool":
                transcript_lines.append(f"[Tool Result]:\n{content_str}")
                continue

            if msg.get("tool_calls"):
                calls = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    calls.append(f"[Tool Call -> {fn.get('name')}\nArgs: {fn.get('arguments')}]")
                if content_str:
                    content_str += "\n\n" + "\n".join(calls)
                else:
                    content_str = "\n".join(calls)

            transcript_lines.append(f"{role.upper()}:\n{content_str}")

        transcript = "\n\n".join(transcript_lines)

        return [
            Turn.system(_JUDGE_SYSTEM_PROMPT),
            Turn.user(
                f"Conversation context:\n\n{transcript}\n\n"
                "---\n\n"
                "Please evaluate the following tool call that is "
                "pending human approval:\n\n"
                f"{tool_detail}\n\n"
                "Render your verdict as JSON."
            ),
        ]

    # Paths the judge is never allowed to read (security hardening).
    _BLOCKED_ROOTS: tuple[Path, ...] = tuple(
        Path(root).resolve()
        for root in (
            "/etc",
            "/root",
            "/proc",
            "/sys",
            "/dev",
        )
    )
    _BLOCKED_PARTS: frozenset[str] = frozenset(
        {
            ".ssh",
            ".gnupg",
            ".aws",
            ".config",
        }
    )
    _BLOCKED_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")

    @staticmethod
    def _is_resolved_path_blocked(path: Path) -> bool:
        """Return whether an already-resolved path is protected."""
        if any(path == root or root in path.parents for root in IntentJudge._BLOCKED_ROOTS):
            return True
        if IntentJudge._BLOCKED_PARTS & set(path.parts):
            return True
        return path.suffix.lower() in IntentJudge._BLOCKED_SUFFIXES

    @staticmethod
    def _is_path_blocked(path: Path) -> bool:
        """Return True if *path* resolves to a protected location."""
        return IntentJudge._is_resolved_path_blocked(path.resolve())

    @staticmethod
    def _exec_read_only_tool(name: str, args: dict[str, Any]) -> str:
        """Execute a read-only tool directly (no session pipeline).

        Returns the tool result as a string, or an error message.
        """
        try:
            if name == "read_file":
                path = Path(str(args.get("path", "")))
                resolved_path = path.resolve()
                if IntentJudge._is_resolved_path_blocked(resolved_path):
                    return f"Error: access denied: {path}"
                if not resolved_path.is_file():
                    return f"Error: file not found: {path}"
                # Bound acquisition itself, not merely the returned string:
                # a parallel batch must not materialize one unbounded file per
                # worker before applying the judge context cap.
                with resolved_path.open("r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read(_JUDGE_READ_LIMIT + 1)
                if len(content) > _JUDGE_READ_LIMIT:
                    try:
                        total_bytes = resolved_path.stat().st_size
                        size_note = f", {total_bytes} bytes total"
                    except OSError:
                        size_note = ""
                    return content[:_JUDGE_READ_LIMIT] + f"\n... (truncated{size_note})"
                return content

            if name == "list_directory":
                path = Path(str(args.get("path", "")))
                resolved_path = path.resolve()
                if IntentJudge._is_resolved_path_blocked(resolved_path):
                    return f"Error: access denied: {path}"
                if not resolved_path.is_dir():
                    return f"Error: directory not found: {path}"
                # Bound collection before sorting so concurrent evidence
                # workers retain at most N+1 directory entries each.
                entries = sorted(
                    itertools.islice(resolved_path.iterdir(), _JUDGE_DIRECTORY_LIMIT + 1),
                    key=lambda entry: entry.name,
                )
                truncated = len(entries) > _JUDGE_DIRECTORY_LIMIT
                entries = entries[:_JUDGE_DIRECTORY_LIMIT]
                lines: list[str] = []
                for entry in entries:
                    suffix = "/" if entry.is_dir() else ""
                    lines.append(f"  {entry.name}{suffix}")
                if truncated:
                    lines.append("  ... (additional entries omitted)")
                return "\n".join(lines) or "(empty directory)"

            return f"Error: unknown tool: {name}"
        except Exception as exc:
            return f"Error executing {name}: {exc}"

    def _parse_verdict(
        self,
        content: str,
        func_name: str,
        call_id: str,
        latency_ms: int,
        func_args: str = "",
    ) -> IntentVerdict | None:
        """Parse a JSON verdict from the judge's response.

        Uses a multi-stage parsing strategy:
        1. Direct JSON parse
        2. Markdown code block extraction
        3. Brace-counting fallback
        4. Regex field extraction (last resort)
        """
        data = self._extract_json(content)
        if not data:
            log.warning("Judge returned unparseable response: %.200s", content)
            return None

        # Validate and normalize fields
        risk_level = str(data.get("risk_level", "medium")).lower()
        if risk_level not in ("low", "medium", "high", "critical"):
            risk_level = "medium"

        recommendation = str(data.get("recommendation", "review")).lower()
        if recommendation not in ("approve", "review", "deny"):
            recommendation = "review"

        confidence = 0.5
        try:
            parsed = float(data.get("confidence", 0.5))
            # Reject non-finite (NaN/inf): json.loads accepts NaN, and a NaN
            # would survive max/min clamping as 1.0 (comparisons with NaN are
            # all False) — keep the cautious 0.5 default instead so a NaN can't
            # masquerade as maximum confidence downstream (e.g. Smart Approvals).
            if math.isfinite(parsed):
                confidence = max(0.0, min(1.0, parsed))
        except (ValueError, TypeError):
            pass  # keeps default 0.5

        evidence = data.get("evidence", [])
        if isinstance(evidence, str):
            evidence = [evidence]
        elif not isinstance(evidence, list):
            evidence = []

        return IntentVerdict(
            verdict_id=uuid.uuid4().hex,
            call_id=call_id,
            func_name=func_name,
            func_args=func_args,
            intent_summary=str(data.get("intent_summary", f"Tool call: {func_name}")),
            risk_level=risk_level,
            confidence=confidence,
            recommendation=recommendation,
            reasoning=str(data.get("reasoning", "")),
            evidence=[str(e) for e in evidence],
            tier="llm",
            judge_model=self._model,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Extract a JSON object from text using multiple strategies."""
        # Strategy 1: Direct parse
        try:
            data = json.loads(text.strip())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass  # falls through to strategy 2

        # Strategy 2: Markdown code block
        md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if md_match:
            try:
                data = json.loads(md_match.group(1))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError):
                pass  # falls through to strategy 3

        # Strategy 3: Find first { and matching }
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start : i + 1])
                            if isinstance(data, dict):
                                return data
                        except (json.JSONDecodeError, ValueError):
                            pass  # falls through to regex extraction
                        break

        # Strategy 4: Regex field extraction (last resort)
        fields: dict[str, Any] = {}
        for key in (
            "intent_summary",
            "risk_level",
            "recommendation",
            "reasoning",
        ):
            m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if m:
                fields[key] = m.group(1)
        conf_m = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
        if conf_m:
            fields["confidence"] = float(conf_m.group(1))
        if fields:
            return fields

        return None
