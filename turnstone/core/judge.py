"""Intent validation judge — heuristic and LLM-based advisory verdicts.

Evaluates non-auto-approved tool calls to produce structured verdicts that
inform (but do not replace) the human approval decision.  The heuristic tier
is a fast, pure-function rule engine with zero external dependencies.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.providers._protocol import LLMProvider

log = get_logger(__name__)

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
    all turns.  With the default of 60 s and a maximum of 5 turns, a
    single tool-call evaluation can take up to 300 s in the worst case
    (e.g. a multi-turn tool-use exchange with a slow local model).
    """

    enabled: bool = True
    model: str = ""  # empty = use session model
    confidence_threshold: float = 0.7
    max_context_ratio: float = 0.5
    timeout: float = 60.0  # per-turn timeout in seconds (see class docstring)
    read_only_tools: bool = True
    output_guard: bool = True
    redact_secrets: bool = True
    cancel_on_approval: bool = False  # True = abort remaining items on user approval


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
        name="man-tool",
        risk_level="low",
        confidence=0.85,
        recommendation="approve",
        tool_pattern="man",
        arg_patterns=[],
        intent_template="Manual page lookup: {arg_snippet}",
        reasoning_template="Looking up a man page is a read-only operation.",
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
    try:
        func_args_json = json.dumps(func_args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        func_args_json = str(func_args)

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


class _ExecutorPoisonedError(Exception):
    """Raised when a timeout leaves the executor's worker thread stuck."""


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
        session_provider: LLMProvider,
        session_client: Any,
        session_model: str,
        context_window: int = 200_000,
        rule_registry: Any | None = None,
        model_registry: Any | None = None,
    ) -> None:
        self._config = config
        self._context_window = context_window
        self._rule_registry = rule_registry

        # Resolve judge model via ModelRegistry alias, falling back to session
        resolved = False
        if config.model and model_registry is not None:
            try:
                if model_registry.has_alias(config.model):
                    client, model_name, _ = model_registry.resolve(config.model)
                    self._provider = model_registry.get_provider(config.model)
                    self._client_factory_args = self._extract_client_config(
                        client,
                        self._provider.provider_name,
                    )
                    self._model = model_name
                    caps = self._provider.get_capabilities(self._model)
                    self._judge_context_window = caps.context_window
                    resolved = True
            except Exception:
                log.debug("Model alias resolution failed for %r, falling back", config.model)

        if not resolved and config.model:
            # Model name override with session provider
            self._provider = session_provider
            self._client_factory_args = self._extract_client_config(
                session_client,
                session_provider.provider_name,
            )
            self._model = config.model
            caps = self._provider.get_capabilities(self._model)
            self._judge_context_window = caps.context_window
        elif not resolved:
            # Self-consistency: same model as session
            self._provider = session_provider
            self._client_factory_args = self._extract_client_config(
                session_client,
                session_provider.provider_name,
            )
            self._model = session_model
            self._judge_context_window = context_window

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
            cancel_event: When set, the daemon judge thread abandons
                remaining work.  Callers should set this after the user
                has already made an approval decision so the judge does
                not keep consuming inference resources.

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
            args=(items, messages, heuristic_verdicts, callback, cancel_event),
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
    ) -> None:
        """Daemon thread: run LLM judge for each item and invoke callback.

        When ``cancel_on_approval`` is True, remaining evaluations are
        aborted as soon as the user approves/denies.  When False (default),
        every evaluation runs to completion so all verdicts are delivered.
        """
        client = self._create_client()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judge-api")
        try:
            for idx, (item, h_verdict) in enumerate(zip(items, heuristic_verdicts, strict=True)):
                if cancel_event and cancel_event.is_set() and self._config.cancel_on_approval:
                    log.info("judge.cancelled", remaining=len(items) - idx)
                    self._deliver_fallbacks(
                        items[idx:],
                        heuristic_verdicts[idx:],
                        callback,
                        "judge cancelled by user approval",
                    )
                    return
                try:
                    llm_verdict = self._evaluate_single(
                        item,
                        messages,
                        cancel_event,
                        executor,
                        client,
                    )
                    if llm_verdict:
                        log.info(
                            "judge.verdict.llm",
                            recommendation=llm_verdict.recommendation,
                            confidence=llm_verdict.confidence,
                            call_id=llm_verdict.call_id,
                        )
                        callback(llm_verdict)
                    else:
                        fallback = IntentVerdict(
                            verdict_id=h_verdict.verdict_id,
                            call_id=h_verdict.call_id,
                            func_name=h_verdict.func_name,
                            func_args=h_verdict.func_args,
                            intent_summary=h_verdict.intent_summary,
                            risk_level=h_verdict.risk_level,
                            confidence=h_verdict.confidence,
                            recommendation=h_verdict.recommendation,
                            reasoning=h_verdict.reasoning + " (LLM judge did not return a verdict)",
                            evidence=h_verdict.evidence,
                            tier="llm_fallback",
                            judge_model=self._model,
                            latency_ms=h_verdict.latency_ms,
                        )
                        log.info(
                            "judge.verdict.fallback",
                            recommendation=fallback.recommendation,
                            confidence=fallback.confidence,
                            call_id=fallback.call_id,
                        )
                        callback(fallback)
                    # After delivering this item's verdict, check if we should
                    # abort remaining items due to user approval.
                    if cancel_event and cancel_event.is_set() and self._config.cancel_on_approval:
                        log.info("judge.cancelled.after_eval", call_id=item.get("call_id", ""))
                        self._deliver_fallbacks(
                            items[idx + 1 :],
                            heuristic_verdicts[idx + 1 :],
                            callback,
                            "judge cancelled by user approval",
                        )
                        return
                except _ExecutorPoisonedError:
                    executor.shutdown(wait=False, cancel_futures=True)
                    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judge-api")
                except Exception:
                    log.exception(
                        "Judge evaluation failed for %s",
                        item.get("func_name", "?"),
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            try:
                if hasattr(client, "close"):
                    client.close()
            except Exception:
                pass

    def _deliver_fallbacks(
        self,
        remaining_items: list[dict[str, Any]],
        remaining_verdicts: list[IntentVerdict],
        callback: Callable[[IntentVerdict], None],
        reason: str,
    ) -> None:
        """Deliver heuristic fallback verdicts for items the judge didn't complete."""
        for _item, h_verdict in zip(remaining_items, remaining_verdicts, strict=True):
            fallback = IntentVerdict(
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
            callback(fallback)

    def _evaluate_single(
        self,
        item: dict[str, Any],
        messages: list[dict[str, Any]],
        cancel_event: threading.Event | None,
        executor: ThreadPoolExecutor,
        client: Any,
    ) -> IntentVerdict | None:
        """Run LLM judge for a single tool call. Returns verdict or None."""
        start = time.monotonic()
        func_name = item.get("func_name", item.get("name", ""))
        func_args = item.get("func_args", {})
        if isinstance(func_args, str):
            try:
                func_args = json.loads(func_args)
            except (json.JSONDecodeError, TypeError):
                func_args = {}
        call_id = item.get("call_id", item.get("tool_call_id", ""))
        try:
            func_args_json = json.dumps(func_args, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            func_args_json = str(func_args)

        # Prepare context
        judge_messages = self._prepare_context(item, messages)

        # Prepare tools (only if read_only_tools enabled).
        # Pass raw OpenAI-format schemas — create_completion handles conversion.
        # Google's API requires thought_signature in function call round-trips
        # which our normalized tool_calls don't preserve, so skip tools for Google.
        tools: list[dict[str, Any]] | None = None
        if self._config.read_only_tools and self._provider.provider_name != "google":
            tools = list(_JUDGE_TOOL_SCHEMAS)

        # Multi-turn judge loop
        result = None  # will hold the last CompletionResult
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
                judge_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have gathered enough evidence. "
                            "You MUST now render your final verdict as JSON. "
                            "No more tool calls."
                        ),
                    }
                )

            # Per-turn timeout: each turn gets a fresh budget so local
            # models aren't penalised for slow earlier turns.
            per_call_timeout = max(self._config.timeout, 5.0)  # at least 5s
            try:
                future = executor.submit(
                    self._provider.create_completion,
                    client=client,
                    model=self._model,
                    messages=judge_messages,
                    tools=None if is_last_turn else tools,
                    max_tokens=2048,
                    temperature=0.0,
                    reasoning_effort="medium",
                )
                # Poll in 1s increments so we notice cancellation promptly
                # instead of blocking for the full per_call_timeout.
                deadline = time.monotonic() + per_call_timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if cancel_event and cancel_event.is_set():
                        future.cancel()
                        return None
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        result = future.result(timeout=min(remaining, 1.0))
                        break
                    except TimeoutError:
                        pass  # loop back to check remaining/cancel
            except TimeoutError:
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
                raise _ExecutorPoisonedError from None
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
                # Execute read-only tools and append results
                judge_messages.append(
                    {
                        "role": "assistant",
                        "content": result.content or None,
                        "tool_calls": result.tool_calls,
                    }
                )
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
                    judge_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": tool_result,
                        }
                    )
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
                judge_messages.append({"role": "assistant", "content": result.content})
                judge_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your response was not valid JSON. "
                            "Please respond ONLY with the JSON verdict object."
                        ),
                    }
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
                judge_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You returned an empty response. "
                            "Please analyze the tool call and respond with "
                            "the JSON verdict object."
                        ),
                    }
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

    def _prepare_context(
        self,
        item: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build the judge's message list with FIFO-truncated conversation."""
        # Calculate token budget for conversation history
        budget_tokens = int(self._judge_context_window * self._config.max_context_ratio)
        budget_chars = int(budget_tokens * _CHARS_PER_TOKEN)

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
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Conversation context:\n\n{transcript}\n\n"
                    "---\n\n"
                    "Please evaluate the following tool call that is "
                    "pending human approval:\n\n"
                    f"{tool_detail}\n\n"
                    "Render your verdict as JSON."
                ),
            },
        ]

    # Paths the judge is never allowed to read (security hardening).
    _BLOCKED_PREFIXES: tuple[str, ...] = (
        "/etc/",
        "/root/",
        "/proc/",
        "/sys/",
        "/dev/",
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
    def _is_path_blocked(path: Path) -> bool:
        """Return True if *path* should not be readable by the judge."""
        resolved = str(path.resolve())
        if any(resolved.startswith(p) for p in IntentJudge._BLOCKED_PREFIXES):
            return True
        if IntentJudge._BLOCKED_PARTS & set(path.parts):
            return True
        return path.suffix.lower() in IntentJudge._BLOCKED_SUFFIXES

    @staticmethod
    def _exec_read_only_tool(name: str, args: dict[str, Any]) -> str:
        """Execute a read-only tool directly (no session pipeline).

        Returns the tool result as a string, or an error message.
        """
        try:
            if name == "read_file":
                path = Path(str(args.get("path", "")))
                if IntentJudge._is_path_blocked(path):
                    return f"Error: access denied: {path}"
                if not path.is_file():
                    return f"Error: file not found: {path}"
                content = path.read_text(encoding="utf-8", errors="replace")
                # Cap at 32KB to avoid blowing context
                if len(content) > 32768:
                    return content[:32768] + f"\n... (truncated, {len(content)} bytes total)"
                return content

            if name == "list_directory":
                path = Path(str(args.get("path", "")))
                if IntentJudge._is_path_blocked(path):
                    return f"Error: access denied: {path}"
                if not path.is_dir():
                    return f"Error: directory not found: {path}"
                entries = sorted(path.iterdir())[:200]  # cap at 200 entries
                lines: list[str] = []
                for entry in entries:
                    suffix = "/" if entry.is_dir() else ""
                    lines.append(f"  {entry.name}{suffix}")
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
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
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
