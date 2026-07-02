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
import contextvars
import copy
import dataclasses
import difflib
import functools
import hashlib
import json
import mimetypes
import os
import queue
import re
import shlex
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
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

import httpx

from turnstone.core import fence
from turnstone.core.attachment_buffer import get_attachment_buffer
from turnstone.core.attachments import (
    IMAGE_SIZE_CAP as _ATTACH_IMAGE_SIZE_CAP,
)
from turnstone.core.attachments import (
    Attachment,
    safe_attachment_label,
    unreadable_placeholder,
)
from turnstone.core.config import get_searxng_engines, get_searxng_url
from turnstone.core.edit import find_occurrences, pick_nearest
from turnstone.core.history_decoration import (
    attach_vllm_chat_reasoning_field,
)
from turnstone.core.log import get_logger
from turnstone.core.lowering import (
    TIMEOUT_OUTCOME_CLAUSE,
    UNOBSERVED_OUTCOME_CLAUSE,
    drop_empty_user_turns,
    fold_system_turns,
    repair_wire_messages,
)
from turnstone.core.memory import (
    count_messages,
    count_structured_memories,
    delete_messages_after,
    delete_structured_memory_by_id,
    delete_workstream,
    get_attachments,
    get_compaction_checkpoint,
    get_compaction_floor,
    get_compaction_watermark,
    get_skill_by_name,
    get_structured_memory_by_name,
    get_workstream_display_name,
    list_default_skills,
    list_skills_by_activation,
    list_structured_memories,
    list_visible_structured_memories,
    list_workstreams_with_history,
    load_message_turns,
    load_workstream_config,
    normalize_key,
    resolve_workstream,
    save_attachment,
    save_message,
    save_messages_bulk,
    save_structured_memory,
    save_workstream_config,
    search_history,
    search_history_recent,
    search_structured_memories,
    search_visible_structured_memories,
    set_message_attachments,
    set_workstream_alias,
    touch_structured_memories,
    update_workstream_title,
)
from turnstone.core.memory_relevance import (
    MemoryConfig,
    build_memory_context,
    extract_recent_context,
    score_memories,
)
from turnstone.core.metacognition import (
    NUDGE_COMPACTION_RESUME,
    RepeatDetector,
    detect_completion,
    detect_correction,
    format_nudge,
    sanitize_payload,
    should_nudge,
)
from turnstone.core.nudge_queue import TOOL_DRAIN, USER_DRAIN, NudgeQueue
from turnstone.core.providers import create_provider
from turnstone.core.ratelimit import TokenBucket
from turnstone.core.safety import is_command_blocked, sanitize_command
from turnstone.core.settings_registry import DEFAULT_AUTO_COMPACT_PCT
from turnstone.core.skill_field_validation import SKILL_RUNTIME_CONFIG_FIELDS
from turnstone.core.skill_parser import MAX_SKILL_DESCRIPTION_LEN
from turnstone.core.storage._registry import get_storage
from turnstone.core.storage._utils import (
    COMPACTION_SOURCE,
    COMPACTION_SUMMARY_LABEL,
    attachment_to_content_part,
    normalize_search_terms,
    strip_orphan_client_tool_blocks,
)
from turnstone.core.tool_advisory import (
    make_system_turn,
    render_output_guard_text,
    render_user_interjection,
)
from turnstone.core.tool_search import ToolSearchManager
from turnstone.core.tools import (
    BUILTIN_TOOL_NAMES,
    COORDINATOR_TOOLS,
    INTERACTIVE_TOOLS,
    PRIMARY_KEY_MAP,
    TASK_AGENT_TOOLS,
    TASK_AUTO_TOOLS,
    merge_mcp_tools,
)
from turnstone.core.trajectory import (
    EffectStatus,
    Role,
    TextBlock,
    ToolCall,
    Turn,
    dicts_from_turns,
    turn_from_dict,
    turn_to_dict,
    turns_from_dicts,
)
from turnstone.core.watch import WATCH_REMINDER_OPTIONAL_KEYS
from turnstone.core.web import check_ssrf, strip_html
from turnstone.core.workstream import WorkstreamKind
from turnstone.prompts import (
    INTERACTIVE_CONSENT_CLIENT_TYPES,
    ClientType,
    SessionContext,
    build_operator_instruction_declaration,
    build_shared_workstream_declaration,
    compose_system_message,
)
from turnstone.ui.colors import DIM, GRAY, GREEN, RED, RESET, YELLOW, bold, cyan, dim

log = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from turnstone.core.config_store import ConfigStore
    from turnstone.core.healthcheck import BackendHealthTracker, HealthTrackerRegistry
    from turnstone.core.judge import IntentJudge, JudgeConfig
    from turnstone.core.mcp_client import MCPClientManager
    from turnstone.core.model_registry import ModelConfig, ModelRegistry
    from turnstone.core.output_guard import OutputAssessment
    from turnstone.core.output_guard_judge import OutputGuardJudge, OutputJudgeVerdict
    from turnstone.core.providers import (
        CompletionResult,
        LLMProvider,
        ModelCapabilities,
        StreamChunk,
    )
    from turnstone.core.rerank import RerankClient, Reranker
    from turnstone.core.web_search import WebSearchClient

# ---------------------------------------------------------------------------
# Cancellation support
# ---------------------------------------------------------------------------


class GenerationCancelled(BaseException):
    """Raised when generation is cancelled via ``ChatSession.cancel()``.

    Subclasses ``BaseException`` so that broad ``except Exception`` handlers
    in tool execution code do not accidentally swallow it.
    """


class AttachmentsNotQueueableError(Exception):
    """Raised by ``ChatSession.queue_message`` when called with non-empty
    ``attachment_ids``.

    Queued messages drain via ``_flush_queued_messages`` after the tool
    batch completes, joining all queued items into a single text-only
    user turn (see the ``"\\n\\n".join(parts)`` shape).  That join can't
    carry image / file blocks, so an attachment-bearing queued item
    would either be silently dropped or force a per-item separate user
    turn — the latter would inject extra ``user`` rows between the tool
    batch and the next assistant turn, expanding the strict-template
    role-ordering surface (Mistral / Anthropic) that the post-batch
    drain already balances.  Rejecting attachments at queue time keeps
    the single-combined-turn invariant intact.

    Callers surface this to the user as "wait for the current turn
    before attaching".
    """


class _CompactionIrreducibleError(Exception):
    """Raised by ``ChatSession._summarize_blocks`` when chunked summarisation
    cannot shrink the input — a recursion level fails to reduce the block count,
    or the depth ceiling is hit.

    ``ChatSession._compact_messages`` turns it into the existing ``return False``
    bail rather than fabricate a summary.  (Chunked compaction never drops or
    fabricates whole turns; its one lossy path is ``_truncate_block`` head/tail-
    truncating a single oversized block as summary *input*.)
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


def _prefix_sender_label(content: Any, sender: str, nonce: str) -> Any:
    """Return *content* with an authenticated sender-label block prepended.

    The label ``message from <sender>`` is wrapped in a nonce-delimited
    ``[start sender-label_{nonce}]`` … ``[end sender-label_{nonce}]`` fence
    (:func:`turnstone.core.fence.wrap`) whose token lives only in the cached
    system prefix, so a participant cannot forge another sender's attribution by
    typing a look-alike marker in their own message; any such marker already in
    *content* is defanged first (:func:`~turnstone.core.fence.neutralize` with
    ``opening=True`` — forge-in defence).  ``fence.wrap`` also neutralises the
    *closing* marker inside the label body, so even a hostile display name
    cannot break out of the fence.

    Handles the plain-string and multipart (text + attachment) content shapes:
    every text part is neutralised, and the label rides the first text part (or
    a new leading text part when the content is attachment-only).  Returns a new
    object; the input is never mutated (the caller works on a transient wire
    copy, not the canonical ``self.messages``)."""
    label = fence.wrap(f"message from {sender}", nonce, fence.SENDER_LABEL_TAG)
    tag = fence.SENDER_LABEL_TAG
    if isinstance(content, str):
        return f"{label}\n{fence.neutralize(content, tag, opening=True)}"
    if isinstance(content, list):
        out: list[Any] = []
        labelled = False
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                np = dict(part)
                safe = fence.neutralize(str(np.get("text", "")), tag, opening=True)
                np["text"] = f"{label}\n{safe}" if not labelled else safe
                labelled = True
                out.append(np)
            else:
                out.append(part)
        if not labelled:
            return [{"type": "text", "text": label}, *out]
        return out
    return content


def _encode_image_data_uri(raw: bytes, mime: str) -> str:
    """Wrap raw image bytes as a ``data:{mime};base64,...`` URI."""
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


# Upper bound on total skill content injected into system messages
_MAX_SKILL_CONTENT: int = 32768

# Cap on a sub-agent tool's RAW output before it enters the trajectory (the
# in-loop truncation in _run_agent) — bounds what the sub-agent's own model sees
# on its next turn.  Distinct from (and larger than) the recall per-step cap.
_AGENT_TOOL_OUTPUT_CAP: int = 16000

# Per-step output/arguments cap for a recalled task-agent sub-trajectory (the
# projected step items /history attaches for the card rebuild).  Keeps the
# recall payload small — the card shows a summary, not the full tool output.
_AGENT_STEP_OUTPUT_CAP: int = 2000

# Cap on the NUMBER of recalled steps per task agent.  With the per-step output
# cap above, this bounds each stash entry's size (the LRU caps the agent count,
# not per-entry bytes), so a 100+-tool agent can't blow the memory budget; the
# overflow is replaced by one honest "(+N not retained)" marker step.
_AGENT_STEP_COUNT_CAP: int = 100

# Per-task-agent file-read tracking.  ``_read_files`` (the blind-overwrite guard's
# memory of "files this agent has read") is a single set on the session, but the
# parent runs task agents in a 4-wide pool — sharing it lets a sibling's read
# suppress another agent's overwrite guard (a real blind-overwrite hazard).
# ``_exec_task`` installs a fresh per-run set in this ContextVar for the sub-
# agent's duration; ``_current_read_files`` reads it.  ``None`` outside a sub-
# agent → the main session's set.  A ContextVar (not threading.local) so the
# set/reset is balanced per ``_exec_task`` call and survives pool-thread reuse.
_active_read_files: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "turnstone_active_read_files", default=None
)

# Cap on the *content portion* (text after ``path:lineno:``) of an
# emitted search result line. Defends the context budget against
# pathological lines (minified blobs, base64 data, etc.).
_MAX_SEARCH_LINE_LENGTH: int = 1024
# Margin over the per-line cap before re-truncating, so backend-supplied
# preview markers (e.g. ripgrep's ``[... omitted end of long line]``) pass
# through cleanly without redundant " ...[truncated]" stacking.
_SEARCH_LINE_MARGIN: int = 128
_SEARCH_TRUNCATION_SUFFIX: str = f"...[truncated, line length > {_MAX_SEARCH_LINE_LENGTH}]"
_SEARCH_ALL_TRUNCATED_MSG: str = (
    "(all matches returned were malformed -- re-check your search query, "
    "if the issue persists there may be a problem with the search backend or the filesystem)"
)
# Total search-output budget (chars). Chosen well under ``tool_truncation``
# (typically 256 KB+) so the head+tail ``_truncate_output`` strategy never
# kicks in for search results — that strategy silently drops middle files
# alphabetically, which is exactly the wrong shape for a grep result.
_SEARCH_OUTPUT_BUDGET: int = 32_768
# Hard cap on raw bytes read from the search subprocess. Defends against
# pathological single-line files (multi-GB JSONL training records, etc.)
# that would otherwise OOM the parent process via ``subprocess.run``.
_SEARCH_RAW_BYTE_CAP: int = 4 * 1024 * 1024
# Files larger than this are skipped entirely (ripgrep only — grep has no
# native equivalent and falls back to the byte cap above).
_SEARCH_MAX_FILESIZE: str = "10M"
# Per-file sample-count ladder for Tier 2 degradation. Each step is tried in
# order; the first K whose total emission fits the budget wins. The full
# 5/3/1 curve documents the degradation: prefer 5 samples per file, fall to
# 3, then a single representative sample before giving up to Tier 3.
_SEARCH_TIER2_SAMPLE_LADDER: tuple[int, ...] = (5, 3, 1)
# Bytes reserved at the end of the Tier 3 body for the
# "(plus N more files with M matches between them)" tail line, so we don't
# blow the budget when the count list itself is enormous.
_SEARCH_TIER3_TAIL_RESERVE: int = 80
# Stderr-drain knobs for ``_search_capture``: bound the captured stderr so
# a hostile child can't grow the buffer indefinitely, and drain in
# moderate-sized chunks so the OS pipe buffer doesn't deadlock the child.
_SEARCH_STDERR_CAP: int = 64 * 1024
_SEARCH_DRAIN_CHUNK: int = 8192
# How long we wait for the stderr drain thread to finish after the child
# exits. The thread reads from a closed pipe at that point; a small
# timeout keeps shutdown bounded if the OS hasn't propagated EOF yet.
_SEARCH_DRAIN_JOIN_TIMEOUT: float = 2.0
# Excluded directory patterns — hit by both backends. ripgrep also respects
# ``.gitignore`` and skips hidden directories by default, so most of these
# are belt-and-suspenders for the rg path; they're load-bearing for grep.
_SEARCH_EXCLUDE_DIRS: tuple[str, ...] = (
    ".git",
    "node_modules",
    "target",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    "*.egg-info",
    ".tox",
    ".venv",
    "venv",
    "vendor",
)


@functools.cache
def _detect_search_backend() -> str:
    """Return ``'rg'`` if ripgrep is on PATH, else ``'grep'``. Cached."""
    return "rg" if shutil.which("rg") else "grep"


def _build_search_args(pattern: str, path: str, backend: str) -> list[str]:
    """Build subprocess args for the chosen search backend.

    The ripgrep flag set is the load-bearing one: ``--max-columns`` +
    ``--max-columns-preview`` bound per-line bytes natively (no Python-side
    re-search needed), ``--max-filesize`` skips multi-MB JSONL/training
    files entirely, and ``--max-count`` matches grep's ``-m`` per-file cap.
    """
    if backend == "rg":
        args = [
            "rg",
            "-n",  # line numbers
            "-H",  # always show filename
            "--no-heading",  # path:line:content format like grep
            "--color=never",
            "--no-config",  # ignore ~/.ripgreprc for reproducibility
            "--no-messages",  # suppress filesystem error noise
            "--max-count",
            "100",
            "--max-columns",
            str(_MAX_SEARCH_LINE_LENGTH),
            "--max-columns-preview",  # show first N cols + omitted-marker
            "--max-filesize",
            _SEARCH_MAX_FILESIZE,
        ]
        for d in _SEARCH_EXCLUDE_DIRS:
            args.extend(["-g", f"!{d}"])
        # ``-e`` protects the pattern from being parsed as a flag; ``--``
        # protects the path the same way. Without ``--`` an attacker who
        # can prompt-inject the agent could pass ``path="--pre=COMMAND"``
        # and ripgrep would execute COMMAND as a per-file preprocessor.
        args.extend(["-e", pattern, "--", path])
        return args
    # grep fallback
    args = ["grep", "-rn", "-I", "-E", "-m", "100", "--color=never"]
    for d in _SEARCH_EXCLUDE_DIRS:
        args.append(f"--exclude-dir={d}")
    args.extend(["--", pattern, path])
    return args


def _parse_search_records(stdout: bytes) -> list[tuple[str, str, str]]:
    """Parse ``path:lineno:content`` records from search backend stdout.

    Drops malformed lines (need ≥2 colons, numeric line-number, non-empty
    path). Decodes bytes leniently for display. Lines that exceed the
    per-line cap *plus* a small margin for backend-supplied truncation
    markers are re-truncated with ``_SEARCH_TRUNCATION_SUFFIX``; this is
    the load-bearing defense for the grep fallback (rg already enforces
    ``--max-columns`` upstream).
    """
    cap = _MAX_SEARCH_LINE_LENGTH
    margin = _SEARCH_LINE_MARGIN
    # Decode the whole buffer once rather than per-line — a cap-hit
    # invocation can yield ~50K lines, and the per-line ``decode()`` was
    # showing up in profiles.
    text = stdout.decode("utf-8", errors="replace")
    records: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        path, sep1, rest = line.partition(":")
        if not sep1 or not path:
            continue
        lineno, sep2, content = rest.partition(":")
        if not sep2 or not lineno.isdigit():
            continue
        if len(content) > cap + margin:
            content = content[:cap] + _SEARCH_TRUNCATION_SUFFIX
        records.append((path, lineno, content))
    return records


def _format_search_results(
    records: list[tuple[str, str, str]],
    capped: bool,
) -> str:
    """Format match records with tiered degradation when output > budget.

    Tier 1: full ``path:line:content`` lines, stream-emitted with a running
    cost check that short-circuits as soon as the budget would be exceeded.
    Tier 2: K samples per file plus an ``…and N more in <path>`` note. K is
    seeded from a per-file size estimate, then stepped down through the
    (5, 3, 1) ladder from the highest rung ≤ the estimate until the
    emission fits. This guarantees every file is at least mentioned, which
    prevents the alphabetic-bias dropout that head+tail truncation produced.
    Tier 3 (fallback): per-file counts only, sorted by descending count.
    """
    by_file: dict[str, list[tuple[str, str]]] = {}
    for path, lineno, content in records:
        by_file.setdefault(path, []).append((lineno, content))
    total = len(records)
    files = len(by_file)
    if not total:
        # Caller distinguishes "no matches" from "all malformed" via rc.
        return _SEARCH_ALL_TRUNCATED_MSG

    summary = f"\n\n({total} matches across {files} files)"
    if capped:
        summary += " (raw output exceeded byte cap; results may be incomplete)"

    chunks: list[str] = []
    used = 0
    overflow = False
    for path, matches in by_file.items():
        for lineno, content in matches:
            line = f"{path}:{lineno}:{content}"
            cost = len(line) + 1
            if used + cost + len(summary) > _SEARCH_OUTPUT_BUDGET:
                overflow = True
                break
            chunks.append(line)
            used += cost
        if overflow:
            break
    if not overflow:
        return "\n".join(chunks) + summary

    # Tier 2 header is added on return; budget for it up front so the
    # final emission stays strictly within ``_SEARCH_OUTPUT_BUDGET`` and
    # ``_truncate_output``'s head+tail strategy never kicks in (that
    # strategy silently drops middle files alphabetically — exactly the
    # shape we're trying to avoid for search results).
    def _tier2_header(k_value: int) -> str:
        h = (
            f"({total} matches across {files} files — "
            f"showing first {k_value}/file. Narrow the query or read_file "
            f"a specific path for full content.)"
        )
        if capped:
            h += " (raw output capped; counts may underreport.)"
        return h

    # Sample the first ~32 records for an emitted-line-length estimate,
    # then seed K from ``budget / (files * avg)`` so we usually skip
    # ladder rungs that won't fit in one pass. Floor avg at 80 so a
    # corpus of unusually short lines doesn't push K artificially high
    # (the estimate would underweight the per-line newline + trailing
    # "...and N more in <path>" notes). The ladder iteration below is
    # the safety net — the estimate is approximate.
    sample = records[:32]
    avg = max(
        80,
        sum(len(p) + len(ln) + len(c) + 3 for p, ln, c in sample) // max(1, len(sample)),
    )
    estimated_k = max(1, _SEARCH_OUTPUT_BUDGET // max(1, files * (avg + 1)))
    # Iterate the ladder starting from the highest rung that's ≤ our
    # estimate. If the chosen K's actual emission doesn't fit (the
    # estimate ignored the header and over-counts compression from
    # shared paths), step down to the next rung instead of jumping
    # straight to Tier 3. The ``or [...]`` is defence against future
    # changes to the ladder constant; with the current (5, 3, 1) it
    # never fires because ``estimated_k`` is floored at 1 above.
    candidates = [k for k in _SEARCH_TIER2_SAMPLE_LADDER if k <= estimated_k] or [
        _SEARCH_TIER2_SAMPLE_LADDER[-1]
    ]
    for k in candidates:
        header = _tier2_header(k)
        # Budget for header + the "\n\n" separator on return.
        body_budget = _SEARCH_OUTPUT_BUDGET - len(header) - 2
        chunks2: list[str] = []
        used2 = 0
        fit = True
        for path, matches in by_file.items():
            head = matches[:k]
            for lineno, content in head:
                line = f"{path}:{lineno}:{content}"
                chunks2.append(line)
                used2 += len(line) + 1
            if len(matches) > k:
                note = f"  ...and {len(matches) - k} more in {path}"
                chunks2.append(note)
                used2 += len(note) + 1
            if used2 > body_budget:
                fit = False
                break
        if fit:
            return header + "\n\n" + "\n".join(chunks2)

    counts = sorted(by_file.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    tier3_header = (
        f"({total} matches across {files} files — too many to show inline. "
        f"Counts only; narrow the query or read_file a specific path.)"
    )
    if capped:
        tier3_header += " (raw output capped; counts may underreport.)"
    # Budget for header + the "\n\n" separator + the trailing
    # "(plus N more files)" line so the final emission stays within
    # ``_SEARCH_OUTPUT_BUDGET`` even when the count list is enormous.
    tier3_body_budget = _SEARCH_OUTPUT_BUDGET - len(tier3_header) - 2 - _SEARCH_TIER3_TAIL_RESERVE
    body_lines: list[str] = []
    body_used = 0
    shown = 0
    for p, m in counts:
        line = f"{p}: {len(m)} matches"
        if body_used + len(line) + 1 > tier3_body_budget:
            break
        body_lines.append(line)
        body_used += len(line) + 1
        shown += 1
    if shown < files:
        omitted_matches = sum(len(m) for _p, m in counts[shown:])
        body_lines.append(
            f"(plus {files - shown} more files with {omitted_matches} matches between them)"
        )
    body = "\n".join(body_lines)
    return tier3_header + "\n\n" + body


# Memory scopes accepted by the ``memory`` tool's preparer + executor.
# Single source of truth — every action validator imports this rather
# than literal-listing the values, so adding a scope is a one-site
# change.  ``coordinator`` is COORDINATOR-only and ``project`` needs the
# workstream attached to an accessible project (see
# :meth:`ChatSession._validate_scope`); the others are kind-agnostic.
_VALID_MEMORY_SCOPES: tuple[str, ...] = (
    "global",
    "workstream",
    "user",
    "coordinator",
    "project",
)

# Implicit-scope walk for INTERACTIVE ``memory(action='get'/'delete')``
# when no scope is specified.  Narrowest → widest so the most
# session-specific row wins on a name collision.  Coord sessions use a
# different walk (just ``("coordinator",)``) — see
# :meth:`ChatSession._implicit_scope_walk`.
_IMPLICIT_SCOPE_WALK: tuple[str, ...] = ("workstream", "user", "global")

# ``list_nodes`` reserves four top-level kwargs for control parameters
# (filters / paging / output verbosity / liveness toggle).  Anything
# else the model passes at the top level is treated as a flat filter
# entry — see :meth:`ChatSession._prepare_list_nodes`.
_LIST_NODES_RESERVED_ARGS: frozenset[str] = frozenset(
    {"filters", "limit", "include_network_detail", "include_inactive"}
)


# ``tasks`` action classifier — partitions actions into read vs write
# so the parallel-batch guard can permit homogeneous batches (all
# writes serialise under the per-ws lock and converge to a consistent
# result; all reads can't race) and reject only the mixed read+write
# shape where ``tasks(list)`` paralleled with ``tasks(add=...)`` has
# unspecified ordering inside ``_execute_tools``'s ThreadPoolExecutor.
_TASKS_READ_ACTIONS: frozenset[str] = frozenset({"list"})
_TASKS_WRITE_ACTIONS: frozenset[str] = frozenset({"add", "update", "remove", "reorder"})

# Matches resource paths referenced in skill content (scripts/foo.py, etc.)
_RESOURCE_PATH_RE = re.compile(
    r"(?<![/\w-])(?:scripts|references|assets)/[\w./-]+\."
    r"(?:json|yaml|yml|toml|cfg|ini|py|sh|js|ts|md|txt)"
    r"(?=[\s)\]}'\"`,;:\x60]|$)"
)


_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


# SKILL.md spec placeholders.  Single combined
# regex so substitution is one-pass — a positional arg whose VALUE
# happens to contain ``$ARGUMENTS`` (etc.) doesn't get re-expanded
# on a second sweep.  The verbose form keeps the alternation
# readable; precedence is left-to-right, so the bracketed
# ``$ARGUMENTS[N]`` form is tried before the bare ``$ARGUMENTS``.
_SPEC_PLACEHOLDER_RE = re.compile(
    r"""
    \$ARGUMENTS\[(?P<idx_bracket>\d+)\]            # $ARGUMENTS[N]
    | \$ARGUMENTS\b(?!\[)                             # $ARGUMENTS (bare)
    | \$\{(?P<env>CLAUDE_[A-Z_]+)\}                   # ${CLAUDE_*}
    | \$(?P<idx_short>\d+)\b                          # $N
    | \$(?P<named>[A-Za-z_][A-Za-z0-9_]*)\b           # $name
    """,
    re.VERBOSE,
)

# Argument names sourced from SKILL.md frontmatter ``arguments:`` must
# match this same identifier pattern so the substitution regex can find
# them.  ``/review`` on PR #578 caught the mismatch where the parser
# accepted hyphenated names like ``issue-number`` that the regex would
# only partially match (``$issue`` consumes the prefix, leaving
# ``-number`` as literal text).  Parser validates at extraction time
# and drops non-conforming names with a warning.
_SKILL_ARG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Detector for "did the original body include the bare ``$ARGUMENTS``
# placeholder?" — used to decide whether to append ``ARGUMENTS: ...``
# at the end per spec when the user passed args.  Negative lookahead
# excludes the ``$ARGUMENTS[N]`` form which is a different placeholder.
_SPEC_ARGUMENTS_LITERAL_RE = re.compile(r"\$ARGUMENTS\b(?!\[)")

# Auto-title generation runs on the session's own (often thinking-capable)
# utility model, and there is no portable way to turn reasoning off:
# ``enable_thinking`` is Qwen-only and commercial providers each differ.  A
# small ``max_tokens`` lets the reasoning pass swallow the entire budget so the
# title text never lands (``finish_reason=length``, empty ``content`` → skip).
# This hits auto-title and refresh alike (shared path) on any thinking model.
# So give the think pass room (``_TITLE_MAX_TOKENS``), then recover the title
# from ``content``: reuse :meth:`ChatSession._strip_reasoning` (the canonical
# ``<think>``/``<reasoning>`` remover, for lanes that leave reasoning inline
# rather than in ``reasoning_content``), take the first non-empty line (a model
# that appends an explanation shouldn't fold prose into the title), then peel a
# ``Title:`` label and wrapping markdown/quote decoration.  Internal punctuation
# is preserved so ``.NET``, ``CI/CD``, ``v1.6.0`` survive.
_TITLE_MAX_TOKENS = 2048
# Match the manual-rename (alias) cap so generated and hand-set titles share
# one length bound.
_TITLE_MAX_CHARS = 80
_TITLE_LABEL_RE = re.compile(r"(?i)^\s*title\s*[:\-—]\s*")
# Wrapping decoration peeled off both ends of a generated title.
_TITLE_WRAP_CHARS = "*`\"' "


# Soft cap on ``"watch_triggered"`` entries in the per-session NudgeQueue.
# The pull-model path batches N watch fires into ONE envelope splice on
# the next drain seam (vs N successive ``send`` turns under the old
# ``_dispatch_pending_watch`` chain), so a per-call recursion cap is no
# longer the bound on unbounded accumulation — but a runaway noisy watch
# should still not pile up unbounded entries.  50 = 10x the prior
# ``_watch_pending`` ``maxsize=20``, applied producer-side via
# :meth:`NudgeQueue.drop_oldest_by_type` so other nudge producers
# (idle_children, advisories) stay unaffected.  Drop policy is
# drop-OLDEST: a drowning watch is most useful with its latest output,
# not its earliest.
_WATCH_QUEUE_SOFT_CAP = 50

# Bounded budget charge for a by-reference pdf/audio attachment.  Its source
# blob can be multi-MB, but the form the model actually sees (perception / STT /
# extracted text, or rasterized pages) is far smaller and its exact size isn't
# known until wire build — so the trimming budget charges min(size_bytes, this).
# Sized to the perception describe cap (max_tokens ~4096 -> ~16K chars).
_DOC_BUDGET_CHAR_CAP = 16_000

_RERANK_TIMEOUT_CAP_S = 15.0  # reranking <=50 short docs is fast; cap so a hung
# endpoint falls back to BM25 in seconds, not up to tools.timeout (120s default).
# Per-turn memory rerank makes the long timeout a turn-stall hazard.


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


def _substitute_skill_args(
    content: str,
    *,
    arguments_str: str,
    arg_names: list[str],
    ws_id: str,
    effort: str,
) -> str:
    """Apply SKILL.md spec placeholder substitution.

    Handles every spec form except ``${CLAUDE_SKILL_DIR}`` (which
    requires a filesystem-shaped skill layout Turnstone doesn't have
    yet — see #572 for the deferral note):

    * ``$ARGUMENTS`` — full argument string as the user typed it
    * ``$ARGUMENTS[N]`` / ``$N`` — Nth positional arg (0-indexed),
      shell-quoted at parse time so ``"hello world" second`` yields
      ``$0='hello world'``, ``$1='second'``
    * ``$<name>`` — named arg from the SKILL.md ``arguments:``
      frontmatter list, paired with the positional arg at the same
      index
    * ``${CLAUDE_SESSION_ID}`` — current workstream id
    * ``${CLAUDE_EFFORT}`` — current reasoning_effort

    Single-pass: a value containing a placeholder token (e.g.
    ``$0='$ARGUMENTS'``) does not get re-expanded.  Matches the
    spec's "Substitution runs once over the original file" rule.

    Spec rule: when *arguments_str* is non-empty and the body has
    no bare ``$ARGUMENTS`` placeholder, the full string is appended
    at the end as ``ARGUMENTS: ...`` so the model still sees what
    the user typed.  Indexed forms (``$ARGUMENTS[N]``) don't count
    as the bare placeholder for this purpose.

    Unresolvable placeholders (e.g. ``$unknown`` when ``unknown``
    isn't in *arg_names*, or ``${CLAUDE_FOO}`` when ``CLAUDE_FOO``
    isn't a known env key) are left as literals — matches the
    forgiving behaviour of :func:`_render_template`.
    """
    try:
        positional = shlex.split(arguments_str) if arguments_str else []
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split so a
        # typo in user-supplied args doesn't make the entire
        # substitution a no-op (and silently leave ``$ARGUMENTS``
        # placeholders in the prompt).
        positional = arguments_str.split() if arguments_str else []

    name_to_idx = {name: i for i, name in enumerate(arg_names)}
    env = {"CLAUDE_SESSION_ID": ws_id, "CLAUDE_EFFORT": effort}

    def _at(idx_str: str) -> str:
        idx = int(idx_str)  # regex guarantees digits
        return positional[idx] if 0 <= idx < len(positional) else ""

    def _replace(m: re.Match[str]) -> str:
        if m.group("idx_bracket") is not None:
            return _at(m.group("idx_bracket"))
        if m.group("env") is not None:
            return env.get(m.group("env"), m.group(0))
        if m.group("idx_short") is not None:
            return _at(m.group("idx_short"))
        if m.group("named") is not None:
            name = m.group("named")
            idx = name_to_idx.get(name)
            if idx is None:
                return m.group(0)
            return positional[idx] if idx < len(positional) else ""
        # Match is the bare $ARGUMENTS literal (no named groups).
        return arguments_str or ""

    had_literal_arguments = bool(_SPEC_ARGUMENTS_LITERAL_RE.search(content))
    rendered = _SPEC_PLACEHOLDER_RE.sub(_replace, content)

    if arguments_str and not had_literal_arguments:
        rendered = f"{rendered}\n\nARGUMENTS: {arguments_str}"

    return rendered


# Block types that carry reasoning content across providers.  Used by
# ``ChatSession._maybe_synth_reasoning_block`` to decide whether
# captured ``reasoning_parts`` need a synthetic ``reasoning_text``
# block: if any of these types already appear in ``provider_blocks``,
# native lane handles persistence and synthesis is a no-op.
# - ``thinking`` / ``redacted_thinking`` — Anthropic native
# - ``reasoning`` — OpenAI Responses native
# - ``reasoning_text`` — synthetic (path-3 capture; included so
#   re-running this code path against an already-synthesized list is
#   idempotent).
_REASONING_BEARING_BLOCK_TYPES: frozenset[str] = frozenset(
    {"thinking", "redacted_thinking", "reasoning", "reasoning_text"}
)


# ---------------------------------------------------------------------------
# SessionUI protocol — the contract every frontend must implement
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backend boundary exception classification
# ---------------------------------------------------------------------------
#
# ``_record_fatal_error`` routes a fatal exception through
# ``_format_backend_error`` (defined on :class:`ChatSession`) which
# matches the exception's class name against the sets below.  Matching
# by name keeps the helper free of httpx / openai / anthropic imports —
# the three SDKs each define their own subclasses, but the names
# (``ReadTimeout``, ``APITimeoutError``, …) are stable across them and
# the OpenAI and Anthropic SDKs use the same names.
#
# Lifted to module scope (rather than ``ClassVar`` constants on
# ``ChatSession``) so the test suite can bind ``_format_backend_error``
# to lightweight stubs that don't subclass the session — keeping the
# helper testable without the full ChatSession construction surface
# (storage init, prompt composition, registry plumbing).

_BACKEND_TIMEOUT_EXC_NAMES: frozenset[str] = frozenset(
    {"ReadTimeout", "WriteTimeout", "PoolTimeout", "APITimeoutError"}
)
_BACKEND_CONNECT_EXC_NAMES: frozenset[str] = frozenset(
    {"ConnectTimeout", "ConnectError", "APIConnectionError"}
)
_BACKEND_NOT_FOUND_EXC_NAMES: frozenset[str] = frozenset({"NotFoundError"})
_BACKEND_AUTH_EXC_NAMES: frozenset[str] = frozenset(
    {"AuthenticationError", "PermissionDeniedError"}
)
_BACKEND_RATE_LIMIT_EXC_NAMES: frozenset[str] = frozenset({"RateLimitError"})

_BACKEND_KNOWN_EXC_NAMES: frozenset[str] = (
    _BACKEND_TIMEOUT_EXC_NAMES
    | _BACKEND_CONNECT_EXC_NAMES
    | _BACKEND_NOT_FOUND_EXC_NAMES
    | _BACKEND_AUTH_EXC_NAMES
    | _BACKEND_RATE_LIMIT_EXC_NAMES
)


def _is_ctx_overflow(exc: BaseException) -> bool:
    """True when *exc* looks like a context-window overflow from any backend.

    Detection is by message *text* AMONG classes that aren't already a recognized
    backend error: vLLM surfaces the SAME overflow as HTTP 400 ``BadRequestError``
    on the OpenAI endpoint but HTTP 500 ``InternalServerError`` on the Anthropic
    (``/v1/messages``) endpoint — neither is in ``_BACKEND_KNOWN_EXC_NAMES`` — so
    text matching is what unifies them.  The class gate is the safety rail: an
    overflow is never a recognized error, so excluding known classes can't suppress
    a real overflow, but it keeps a retryable 429 ``RateLimitError`` — whose
    token-quota text can read "… maximum number of tokens allowed per minute …" —
    from being misread as a deterministic overflow.  That matters because EVERY
    caller (the retry gates via ``_stop_retrying``, the send-loop recovery, the
    chunker, the task_agent loop, the fatal-error formatter) routes an overflow to
    a non-retryable / compaction path; a false positive on a 429 would turn a
    transient rate-limit into a hard failure.  Centralizing the gate here keeps all
    those callers consistent without each re-checking the class.

    A free function (not a method) so the fatal-error formatter — unit-tested with
    a stand-in ``self`` — and the other callers can share one definition.

    The phrases are deliberately overflow-specific and cover the core providers
    (OpenAI/vLLM "maximum context length"; Anthropic "exceed context limit,
    decrease input length"; Google/Gemini "exceeds the maximum number of tokens
    allowed").
    """
    if type(exc).__name__ in _BACKEND_KNOWN_EXC_NAMES:
        return False
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "context length",
            "maximum context",
            "context window",
            "context limit",
            "prompt is too long",
            "input is too long",
            "reduce the length of the input",
            "maximum number of tokens",
        )
    )


class SessionUI(Protocol):
    def on_turn_start(self) -> None: ...
    def on_turn_committed(self) -> None: ...
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
    def on_info(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_system_turn(
        self, content: str, source: str, meta: dict[str, Any] | None = None
    ) -> int | None: ...
    def on_state_change(self, state: str) -> None: ...
    def on_rename(self, name: str) -> None: ...
    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Called when the LLM judge produces a verdict for a pending approval."""
        ...

    def on_output_warning(self, call_id: str, assessment: dict[str, Any]) -> None:
        """Called when the output guard detects risk signals in tool output."""
        ...

    def record_output_assessment(
        self,
        call_id: str,
        assessment: dict[str, Any],
        *,
        tier: str = "heuristic",
        reasoning: str = "",
        judge_model: str = "",
        latency_ms: int = 0,
        confidence: float = 0.0,
    ) -> None:
        """Persist one output-guard assessment row (one per ``(call_id, tier)``)."""
        ...


# ---------------------------------------------------------------------------
# MCP dispatch helpers
# ---------------------------------------------------------------------------


def _format_mcp_dispatch_error(prefix: str, exc: Exception) -> str:
    """Preserve structured-error JSON when the dispatcher signals via ``RuntimeError(json_str)``.

    The pool dispatcher raises ``RuntimeError(json_str)`` with a
    :func:`turnstone.core.mcp_client._structured_error` payload (e.g.
    ``mcp_consent_required``, ``mcp_insufficient_scope``) when the
    failure has a user-actionable remedy. The dashboard renderer keys
    on this shape; surrounding it with ``f"{prefix}: {exc}"`` would
    make the JSON un-parseable. Non-structured exceptions are still
    rendered with the prefix so the agent has a human-readable label.

    Shared by :meth:`ChatSession._exec_mcp_tool`,
    :meth:`ChatSession._exec_read_resource`, and
    :meth:`ChatSession._exec_use_prompt` — placed at module scope so
    no single exec site can claim ownership.
    """
    text = str(exc)
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return f"{prefix}: {exc}"
    if isinstance(decoded, dict) and isinstance(decoded.get("error"), dict):
        code = decoded["error"].get("code")
        if isinstance(code, str) and code.startswith("mcp_"):
            return text
    return f"{prefix}: {exc}"


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


def _effect_status_meta(status: EffectStatus | None) -> str | None:
    """Serialize a tool effect status to the ``conversations.meta`` JSON
    envelope. Role-exclusive with ``source_meta`` (which rides SYSTEM turns),
    so a tool row's meta column holds only ``{"effect_status": ...}``; the
    decode + role routing lives in ``reconstruct_turns``. ``None`` → no meta."""
    return json.dumps({"effect_status": status.value}) if status is not None else None


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
        auto_compact_pct: float = DEFAULT_AUTO_COMPACT_PCT,
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
        skill_arguments: str = "",
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
        project_id: str = "",
    ):
        if kind == WorkstreamKind.COORDINATOR and not user_id:
            # Coordinators carry real authority — they mint child-spawn
            # tokens as their creator and own a durable per-user memory
            # namespace — so an anonymous one must never exist. The
            # empty-string user_id sentinel is an interactive-only lane
            # (CLI / eval / placeholder sessions); every coordinator
            # host authenticates (console HTTP create 401s an empty
            # principal), leaving rehydration of a corrupt or legacy
            # row as the path this guard surfaces.
            raise ValueError(
                "coordinator sessions require an authenticated user_id; "
                f"refusing to construct an anonymous coordinator (ws_id={ws_id!r}). "
                "If this is a persisted legacy row, delete or close it."
            )
        self.client = client
        self.model = model
        # Coordinator plumbing: populated by the console's session factory
        # only — ``kind == COORDINATOR`` sessions run COORDINATOR_TOOLS
        # and dispatch tool execs through ``coord_client``.
        self._kind = kind
        self._parent_ws_id = parent_ws_id if parent_ws_id else None
        self._coord_client: Any = coord_client
        self._trust_send: bool = False
        self._revoked_tools: frozenset[str] = frozenset()
        self._governance_lock = threading.Lock()
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
        # auto_compact_pct < 0.1 is invalid: 0 used to mean "disabled" (no
        # longer supported), and at 0 the "> context_window * pct" checks become
        # "> 0" (always true → compact every turn). Coerce to the default, not
        # the 0.1 floor — someone who set 0 wanted *less* compaction, so 0.8 is
        # closer to intent than near-constant compaction at 10%. Matches the
        # settings-registry path, which rejects sub-0.1 stored values and
        # reverts to this same default.
        self.auto_compact_pct = (
            auto_compact_pct if auto_compact_pct >= 0.1 else DEFAULT_AUTO_COMPACT_PCT
        )
        # Cooperative-compaction latch: set once the model has been advised to
        # reach a stopping point under context pressure; if it keeps working
        # past the advisory the send loop forces a compaction.
        self._compaction_advised = False
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
        # ``user_id`` is the authenticated principal's UUID for HTTP-borne
        # sessions (server, console) and the empty string ``""`` for CLI
        # / eval / unauthenticated callers. The empty string is a SENTINEL
        # that collapses to ``None`` for MCPClientManager's optional
        # ``user_id`` arguments (cached once below as ``_mcp_user_id``),
        # which short-circuits per-user OAuth pool lookup → CLI / eval
        # sessions cannot use ``auth_type=oauth_user`` MCP servers
        # (no per-request user identity to attach a bearer to). End
        # users running such servers must pre-link via the web UI;
        # static-path MCP servers continue to work in all session
        # contexts.
        self._user_id = user_id
        # ``_user_id`` is set once here and never reassigned, so we
        # compute the MCP-API form (empty-string-to-None collapse) once
        # rather than re-asserting the invariant at each call site.
        # Listener identity, catalog merge, and dispatch all consume
        # this through ``_mcp_user_id``.
        self._mcp_user_id: str | None = user_id or None
        # Acting user for per-user MCP credential resolution on SHARED
        # workstreams: the authenticated principal who most recently
        # initiated a turn (bound by ``bind_acting_user`` from the send
        # path). Empty until the first authenticated send, and empty
        # forever on CLI / eval / scheduled sessions — every consumer
        # goes through ``_mcp_effective_user_id``, which falls back to
        # the session owner. Rebinding also swaps the user-scoped MCP
        # listeners; ``_mcp_listener_user_id`` tracks the identity the
        # current registrations were made under (listener identity is
        # the ``(user_id, callback)`` pair).
        self._acting_user_id: str = ""
        self._mcp_listener_user_id: str | None = user_id or None
        # Shared-workstream context state: the model must be TOLD when more
        # than one human is in the room. ``_shared_workstream`` flips True once
        # a non-owner sender appears (live send OR rehydrated history) and
        # drives the ``## Session Context`` banner; it LATCHES — reverting
        # would misframe a known-multi-user conversation and churn the
        # provider-cached prompt prefix. ``_known_senders`` (everyone who has
        # ever spoken here) gates the one-time "has joined" note and only
        # grows: deriving it from the in-memory slice alone would forget
        # participants once compaction narrows history. ``_senders_dirty``
        # memoizes recomputes per turn (the composer runs many times per
        # turn); ``_db_senders_loaded`` marks the one-time full-history read.
        self._shared_workstream: bool = False
        self._known_senders: set[str] = set()
        self._senders_dirty: bool = True
        self._db_senders_loaded: bool = False
        # user_id -> display username cache for shared-workstream labels / join
        # notes, so senders read as usernames (like the owner banner) not raw
        # id hashes. Resolved lazily via storage; a handful of entries per ws.
        self._sender_name_cache: dict[str, str] = {}
        self._username = username
        self._client_type = client_type
        # Whether the user is online to complete an in-flight OAuth
        # consent redirect.  WEB and CLI users are; CHAT (Discord /
        # Slack) and SCHEDULED (autonomous runs) are not — their
        # consent-required errors must be persisted to
        # ``mcp_pending_consent`` by the pool dispatchers for later
        # surfacing on the dashboard badge, rather than relying on the
        # in-flight SSE rendering path that Phase 8 ships for
        # interactive surfaces.
        self._is_interactive_for_consent: bool = client_type in INTERACTIVE_CONSENT_CLIENT_TYPES
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
        # Per-turn cache for _search_visible_memories — _init_system_messages
        # fires many times within one turn (state transitions, MCP refresh,
        # tool results) and the recent-context string is identical across
        # them.  Invalidated on user-turn append and on memory write/delete.
        self._mem_search_cache: dict[tuple[str, str, int], list[dict[str, str]]] = {}
        # Per-turn dedup for composition touches: ``_init_system_messages`` runs
        # many times within a turn, so the injected set is touched at most once
        # per memory per turn.  Cleared alongside the search cache.
        self._touched_memory_keys: set[tuple[str, str, str]] = set()
        # Per-send memo for the wire attachment resolver (set in send(), None
        # outside a send).  _resolve_attachments re-runs on every agentic
        # round-trip, so this caches the materialized part by
        # (attachment_id, caps-signature) to avoid re-fetching + re-rasterizing +
        # re-base64'ing the same blob once per round-trip.
        self._wire_part_cache: (
            dict[tuple[str, tuple[bool, bool, bool]], dict[str, Any] | list[dict[str, Any]]] | None
        ) = None
        self._ws_id = ws_id or uuid.uuid4().hex
        # Project attachment + access, resolved ONCE here (mid-session attach or
        # access-revoke takes effect on the next session load — same contract as
        # user_id / coordinator scope).  ``_project_id`` is set only when the user
        # can READ the project, so it gates recall in ``_visible_scopes``;
        # ``_project_writable`` additionally gates the memory(save) path so a
        # non-member of a *public* project can read but not write its memory.
        self._project_id = ""
        self._project_name = ""
        self._project_writable = False
        if project_id and self._user_id:
            from turnstone.core.auth import resolve_project_access

            # One fetch resolves read access, write access, the display name, and
            # the project state (vs three round-trips).  Recall is gated on READ
            # access AND a non-archived project: an archived project is "not
            # recalled" per the schema contract, even though its owner can still
            # reach it through the management routes (to rename / unarchive).
            acc = resolve_project_access(self._user_id, project_id)
            if acc.can_read and acc.state != "archived":
                self._project_id = project_id
                self._project_writable = acc.can_write
                self._project_name = acc.name
        self._title_generated = False
        self._read_files: set[str] = set()
        # The canonical in-memory trajectory.  Wire prep (fold/repair) + the
        # provider translators still consume dicts, so ``_full_messages`` lowers
        # Turns→dicts at that boundary until those layers migrate.
        self.messages: list[Turn] = []
        self._last_usage: dict[str, int] | None = None
        self._msg_tokens: list[int] = []  # parallel to self.messages
        self._system_tokens = 0  # tokens for system_messages
        # Workstream template metadata
        self._token_budget: int = 0
        self._budget_warned: bool = False
        self._budget_exhausted: bool = False
        self._notify_on_complete: str = "[]"
        self._applied_skill_id: str = ""
        self._applied_skill_version: int = 0
        self._applied_skill_content: str = ""  # inline prompt from applied skill
        self._assistant_pending_tokens = 0
        self._calibrated_msg_count = 0  # len(messages) at last _update_token_table
        self.creative_mode = False
        self._notify_count = 0
        # Watch support: server-level runner injected via set_watch_runner()
        self._watch_runner: Any = None  # WatchRunner | None
        # Metacognitive nudges: ephemeral prompts for proactive memory use.
        # One ``NudgeQueue`` per session; producers tag each entry with a
        # channel and consumers drain by filter, emitting each drained nudge
        # as a first-class ``{"role": "system"}`` turn (see
        # ``tool_advisory.make_system_turn``):
        #   - "user" entries drain at the next user-message seam via
        #     ``_emit_pending_user_nudges`` (system turn AFTER the user turn)
        #   - "tool" entries drain at the next tool-result batch via
        #     ``_collect_advisories`` (system turns AFTER the tool batch)
        #   - "any" entries drain at whichever seam fires first; used for
        #     wake-trigger-driven nudges that should not pin to a
        #     specific seam
        # Cooldown timestamps live separately in ``_metacog_state`` for
        # ``should_nudge`` gating.
        self._metacog_state: dict[str, float] = {}
        self._nudge_queue = NudgeQueue()
        # Wake-trigger plumbing: ``deliver_wake_nudge_from_queue`` sets
        # this tag while sending a synthetic empty user turn so that
        # ``_check_metacognitive_nudge`` and the ``_queue_*_advisory``
        # producers short-circuit (the wake's nudge text contains
        # trigger words like "don't" that would otherwise re-fire
        # correction nudges on top of it) and so the synthesized user
        # message gets stamped ``_source`` for audit / replay distinction.
        self._wake_source_tag: str = ""
        # Nudge entries pre-drained by ``deliver_wake_nudge_from_queue``
        # — handed to ``_emit_pending_user_nudges`` so the synthesized
        # send doesn't re-drain (and so we can bail out before send when
        # every entry's ``valid_until`` predicate dropped its item).
        self._wake_drained_reminders: list[dict[str, Any]] | None = None
        # User message queue: messages sent while model is executing.
        # OrderedDict preserves FIFO order and supports O(1) removal by ID.
        # Queued user turns never carry attachments — see
        # ``AttachmentsNotQueueableError`` for the role-ordering reason —
        # so the entry tuple is just ``(cleaned, priority)``.
        self._queued_messages: collections.OrderedDict[str, tuple[str, str]] = (
            collections.OrderedDict()
        )
        self._queued_lock = threading.Lock()
        # Repeat detection: streak counter over tool-call signatures.
        # Fires when a (name, args) signature has been seen N times in
        # a row; recording any different signature resets the streak.
        # Also cleared after a write tool succeeds (state changed) or
        # after a warning fires (clean slate, re-fire on the next streak).
        self._repeat_detector = RepeatDetector()
        # Tool error tracking: call_id → is_error for message persistence
        self._tool_error_flags: dict[str, bool] = {}
        # Typed effect disposition: call_id → EffectStatus, set by the producer
        # (only for non-ordinary outcomes — e.g. UNKNOWN on a timeout/cancel)
        # and popped at the fold; same lifecycle as ``_tool_error_flags``.
        self._tool_status: dict[str, EffectStatus] = {}
        # Cooperative cancellation: set from outside to stop generation
        self._cancel_event = threading.Event()
        self._cancel_ref: _CancelRef = _CancelRef(self)  # provider appends SDK stream here
        self._cancel_stream: Any = None  # closeable SDK stream handle
        self._generation: int = 0  # monotonic counter; orphaned threads skip cleanup
        self._active_procs: set[subprocess.Popen[str]] = set()  # for force-kill
        self._procs_lock = threading.Lock()
        self._cancelled_partial_msg: dict[str, Any] | None = None
        self._pending_retry: str | None = None
        # True when a fatal exception's text has been persisted to
        # workstream_config["last_error"] for the coord's inspect/wait
        # surface.  Cleared when state transitions back to idle/running
        # so a once-leaked exception body doesn't outlive the workstream
        # — see ``_emit_state``.
        self._has_persisted_error: bool = False
        # Intent validation judge (lazy-initialized)
        self._judge_config: JudgeConfig | None = judge_config
        self._judge: IntentJudge | None = None
        self._judge_cancel_event: threading.Event | None = None
        # Output-guard LLM judge (lazy-initialized, issue #560 mitigation #1).
        # Lives alongside ``_judge`` and is reset by the same client/model
        # swap paths so both judges pick up new credentials.
        self._output_guard_judge: OutputGuardJudge | None = None
        self._output_guard_judge_cancel: threading.Event | None = None
        # Rate limiter for the LLM-judge stage — 60 calls/minute caps
        # adversarial fan-out cost.  Bucket starts full so a single turn
        # with many tools is not throttled.  Reset alongside the judge
        # instance at the model-swap paths.
        self._output_guard_judge_rl = TokenBucket(rate=1.0, burst=60)
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
        elif mcp_client:
            mcp_tools = mcp_client.get_tools(user_id=self._mcp_user_id)
            self._tools = merge_mcp_tools(INTERACTIVE_TOOLS, mcp_tools)
            self._task_tools = merge_mcp_tools(TASK_AGENT_TOOLS, mcp_tools)
            # Register for tool-change notifications from MCP servers.
            # ``user_id`` is the listener identity component — pool-only
            # changes for OTHER users must not fire this callback.
            self._mcp_refresh_cb = self._on_mcp_tools_changed
            mcp_client.add_listener(self._mcp_refresh_cb, user_id=self._mcp_user_id)
            # Register for resource-change notifications.
            # ``user_id`` scopes the listener so pool-only resource
            # changes for OTHER users do not wake this session.
            self._mcp_resource_cb = self._on_mcp_resources_changed
            mcp_client.add_resource_listener(self._mcp_resource_cb, user_id=self._mcp_user_id)
            # Register for prompt-change notifications.
            # ``user_id`` scopes the listener so pool-only prompt changes
            # for OTHER users do not wake this session.
            self._mcp_prompt_cb = self._on_mcp_prompts_changed
            mcp_client.add_prompt_listener(self._mcp_prompt_cb, user_id=self._mcp_user_id)
            # Proactively warm this user's per-user OAuth (oauth_user) pools so
            # their tools are present without a manual reconnect (e.g. after a
            # reboot/upgrade, or right after consent). Fire-and-forget — the
            # listeners registered just above deliver the catalog to this
            # session once each prime completes. No-op for users with no
            # consented oauth_user servers.
            if self._mcp_user_id and hasattr(mcp_client, "prime_user_pools"):
                try:
                    mcp_client.prime_user_pools(self._mcp_user_id)
                except Exception:
                    log.debug(
                        "mcp prime_user_pools scheduling failed user=%s",
                        self._mcp_user_id,
                        exc_info=True,
                    )
        else:
            self._tools = INTERACTIVE_TOOLS
            self._task_tools = TASK_AGENT_TOOLS
        # Inject the live alias list into the task_agent tool
        # description so the calling LLM sees its `model` parameter options.
        # Replaces affected tool dicts with deep copies — module-level
        # constants are not mutated.
        self._render_agent_tool_descriptions()
        # Web search backend (pluggable: auto/searxng/mcp:server:tool)
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
                reranker=self._bm25_reranker(),
            )
        # Skill: explicit name overrides is_default skills.  ``skill_arguments``
        # carries the spec's $ARGUMENTS payload — set at create/load time,
        # substituted into the skill body by ``_load_skills``.
        self._skill_name: str | None = skill
        self._skill_arguments: str = skill_arguments
        self._skill_content: str | None = None
        self._skill_resources: dict[str, str] = {}
        self._skill_resources_dir: str | None = None
        # Per-session nonce for the operator-instruction fence — the fold
        # path's ``[start system-reminder_{nonce}]`` marker.  Minted once per session,
        # declared in the system prompt as the sole trusted marker, and reused by
        # every fold this session (the declaration pins the exact value, so it
        # must stay stable across the cached prefix — see ``fence`` for why the
        # judge fence can rotate per-call but this one cannot).  Transient/
        # wire-only (not persisted): the fold re-wraps each build, so a fresh
        # nonce per session-load is fine.  64-bit + per-fold host escaping (see
        # ``lowering.fold_system_turns``) keep a mid-session leak from forging a
        # block.  Owned here; ``lowering`` borrows it as a parameter.
        self._envelope_nonce = fence.mint_nonce()
        # Per-session nonce for the sender-label fence (shared workstreams).
        # Distinct tag + distinct value from the operator nonce so a forged
        # sender label can never claim operator authority; same lifecycle —
        # pinned in the cached prefix by ``build_shared_workstream_declaration``
        # so it must stay stable, and reused by every label this session.
        self._sender_label_nonce = fence.mint_nonce()
        self._load_skills()
        # Memory selection keys off the recent-user-message query, but a fresh
        # session has no messages yet here -> the first compose would inject
        # recency-only memories with no relevance/rerank. Track whether we've
        # composed against a real query so send() can defer the memory-bearing
        # recompose to the first user turn (keeps the cached prefix stable).
        self._system_composed_with_context: bool = False
        self._init_system_messages()
        # Skip on rehydrate — ``_save_config`` is ``INSERT OR
        # REPLACE`` per-key, and the persisted row is what
        # ``ChatSession.resume`` is about to read back.  Pairs with
        # ``SessionManager.open``'s saved-alias threading; together
        # they keep reopened workstreams on their original model and
        # settings instead of silently resetting to constructor
        # defaults.
        if not load_workstream_config(self._ws_id):
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
            smart_approvals=cs.get("judge.smart_approvals"),
            confidence_threshold=cs.get("judge.confidence_threshold"),
            max_context_ratio=cs.get("judge.max_context_ratio"),
            timeout=cs.get("judge.timeout"),
            read_only_tools=cs.get("judge.read_only_tools"),
            output_guard=cs.get("judge.output_guard"),
            output_guard_budget_seconds=cs.get("judge.output_guard_budget_seconds"),
            output_guard_llm=cs.get("judge.output_guard_llm"),
            output_guard_model=cs.get("judge.output_guard_model"),
            output_guard_llm_timeout=cs.get("judge.output_guard_llm_timeout"),
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
        """Return a web search client for the configured backend, or None.

        SearxNG URL/engine resolution follows the project precedence
        ``storage → config.toml → env → registry default``:

          1. An explicit admin (ConfigStore) value wins — including ``""``,
             which means "disabled" and is NOT overridden by env/config.
          2. ``config.toml`` / the ``TURNSTONE_SEARXNG_*`` env vars.
          3. The registry default (``http://searxng:8080``) the store returns
             for unset keys, so the bundled SearxNG resolves out of the box.

        A bare CLI has no ConfigStore and uses (2) only, disabling the tool
        when nothing is set. ``stored_keys()`` distinguishes an explicit empty
        value (deliberate disable) from an unset key (fall through).
        """
        from turnstone.core.web_search import resolve_web_search_client

        cs = getattr(self, "_config_store", None)
        stored = cs.stored_keys() if cs is not None else frozenset()

        def _setting(key: str, env_value: str | None) -> str:
            if cs is not None and key in stored:  # explicit admin value wins
                return str(cs.get(key) or "").strip()
            if env_value:  # config.toml / env var
                return env_value
            if cs is not None:  # registry default, surfaced by the store
                return str(cs.get(key) or "").strip()
            return ""

        searxng_url = _setting("tools.searxng_url", get_searxng_url()) or None
        searxng_engines = _setting("tools.searxng_engines", get_searxng_engines())

        return resolve_web_search_client(
            backend=self._get_web_search_backend(),
            searxng_url=searxng_url,
            searxng_engines=searxng_engines,
            mcp_client=self._mcp_client,
            timeout=self.tool_timeout,
        )

    def _resolve_rerank_client(self) -> RerankClient | None:
        """Return a rerank client, or None when reranking is unconfigured.

        The reranker is a **model definition** (capability ``supports_rerank``)
        selected via the Reranker role (``tools.reranker_alias``); its base_url
        is the full /rerank endpoint. There is no bundled rerank endpoint and no
        global URL fallback, so reranking stays disabled until such a model is
        selected.
        """
        from turnstone.core.rerank_config import resolve_rerank_client_from

        return resolve_rerank_client_from(
            getattr(self, "_config_store", None),
            getattr(self, "_registry", None),
            timeout=min(self.tool_timeout, _RERANK_TIMEOUT_CAP_S),
        )

    def _rerank_enabled_for(self, tool: str) -> bool:
        """Whether reranking is enabled for ``tool`` (currently: 'web_search', 'bm25').

        The per-tool toggles default on; the operative gate is whether an
        endpoint is configured (``_resolve_rerank_client`` returns None when
        not). A bare CLI without a ConfigStore inherits the on-by-default toggle.
        """
        cs = getattr(self, "_config_store", None)
        if cs is not None:
            return bool(cs.get(f"tools.rerank_{tool}"))
        return True

    def _web_search_reranker(self) -> Reranker | None:
        """Build a web_search reranker callable, or None when disabled.

        Returns a ``(query, docs) -> ranked indices`` adapter over the configured
        rerank endpoint; None when reranking is off or no endpoint is set.
        """
        if not self._rerank_enabled_for("web_search"):
            return None
        rc = self._resolve_rerank_client()
        if rc is None:
            return None

        def _rank(query: str, docs: list[str]) -> list[int]:
            return [hit.index for hit in rc.rerank(query, docs)]

        return _rank

    def _bm25_reranker(self, threshold: float = 0.0) -> Reranker | None:
        """Build a BM25 reranker callable, or None when disabled.

        Mirrors ``_web_search_reranker``. ``threshold`` is a relevance FLOOR
        applied in this closure (where scores still exist); the BM25Index seam
        stays indices-only. ``threshold <= 0`` disables the floor. An empty
        response for non-empty input is an endpoint failure (a conforming
        reranker scores every doc), NOT a floor result, so it raises
        ``RerankError`` -> BM25Index falls back to BM25 order regardless of
        threshold. Only memory composition passes a configured threshold;
        reactive surfaces pass 0.
        """
        if not self._rerank_enabled_for("bm25"):
            return None
        rc = self._resolve_rerank_client()
        if rc is None:
            return None

        from turnstone.core.rerank import RerankError, normalize_scores

        def _rank(query: str, docs: list[str]) -> list[int]:
            hits = rc.rerank(query, docs)
            if docs and not hits:
                # A conforming reranker scores every document; an empty result
                # for non-empty input means the endpoint response was
                # unparseable (rc.rerank -> _parse_hits returns [] without
                # raising). That is an endpoint FAILURE -- a discrete branch
                # from the relevance floor below -- so raise and let BM25Index
                # fall back to BM25 order in BOTH modes, instead of the
                # filter-mode floor honoring it as "nothing relevant".
                raise RerankError("rerank endpoint returned no scores for non-empty input")
            # Normalise to a 0-1 relevance space (sigmoid for logit endpoints) so
            # ``threshold`` means the same on every reranker. Sigmoid is monotonic
            # -> ordering is unchanged; only the floor compare gains a uniform scale.
            scores = normalize_scores([h.score for h in hits])
            return [
                h.index
                for h, score in zip(hits, scores, strict=True)
                if threshold <= 0 or score >= threshold
            ]

        return _rank

    def _bm25_rerank_threshold(self) -> float:
        """Configured proactive-memory relevance floor (0.0 = disabled).

        Precedence: the ACTIVE reranker model's per-model calibration (populated
        by calibrate-on-detect) wins over the global ``tools.rerank_bm25_threshold``
        fallback. A reranker is "calibrated" once its capabilities carry a
        non-empty ``rerank_scale``; the calibrated floor is only used when the
        calibration also found a clean separation (``rerank_separated``) — a
        calibrated-but-not-separated reranker means no single floor works, so the
        floor is disabled (0.0) rather than falling back to the global value.
        Reads the RAW capabilities dict (independent of ``_resolve_capabilities``
        field filtering) so the marker survives regardless of the dataclass.
        """
        cs = getattr(self, "_config_store", None)
        if cs is None:
            return 0.0
        try:
            alias = str(cs.get("tools.reranker_alias") or "").strip()
            registry = getattr(self, "_registry", None)
            if alias and registry is not None and registry.has_alias(alias):
                caps = registry.get_config(alias).capabilities
                if caps.get("rerank_scale"):  # calibrated marker
                    if caps.get("rerank_separated"):
                        return float(caps.get("rerank_threshold") or 0.0)
                    return 0.0  # calibrated, no clean separation -> no floor
            return float(cs.get("tools.rerank_bm25_threshold") or 0.0)
        except (TypeError, ValueError):
            return 0.0

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

    def _resolve_server_type(self, alias: str | None = None) -> str:
        """Read ``server_compat.server_type`` for an alias from the registry.

        Used by :meth:`_maybe_synth_reasoning_block` to tag synthetic
        path-3 reasoning blocks with their origin server (vllm,
        llama.cpp, sglang, etc.) — informational metadata for UI
        rehydration.  Returns ``""`` on any lookup miss.

        Phase 5 (:meth:`_maybe_attach_vllm_chat_reasoning`) does NOT
        call this resolver — it reads ``cfg.server_compat["server_type"]``
        directly off the single ``cfg`` it already fetched for the
        ``replay_reasoning_to_model`` flag check, to avoid a second
        ``registry.get_config`` round-trip.  Both readers MUST stay
        aligned on the same field path; if you change one, change the
        other.

        Reads ``cfg.server_compat`` (the dedicated dataclass field set
        by the model_registry loader) — NOT ``cfg.capabilities``.  Both
        loader paths (DB at ``model_registry.py:401`` and config.toml at
        ``model_registry.py:485``) ``caps.pop("server_compat", {})`` and
        hoist the dict to the top-level field, so the capabilities dict
        never carries server_compat in production.
        """
        target_alias = alias or self._model_alias or ""
        if not self._registry or not target_alias:
            return ""
        try:
            cfg: ModelConfig = self._registry.get_config(target_alias)
            sc = cfg.server_compat if isinstance(cfg.server_compat, dict) else None
            if isinstance(sc, dict):
                return str(sc.get("server_type") or "")
        except Exception:
            # Best-effort lookup — synth-block source tagging is
            # informational, never load-bearing.  Log at debug so a
            # repeated registry-lookup failure during a session shows
            # up under DEBUG triage but doesn't spam normal logs.
            log.debug(
                "_resolve_server_type lookup failed for alias=%s; defaulting to empty",
                target_alias,
                exc_info=True,
            )
        return ""

    def _maybe_synth_reasoning_block(
        self,
        provider_blocks: list[dict[str, Any]],
        reasoning_parts: list[str],
    ) -> list[dict[str, Any]]:
        """Stamp captured ``reasoning_parts`` as a synthetic ``reasoning_text``
        block when no reasoning-bearing block already appears in
        ``provider_blocks``.

        Anthropic emits native ``thinking`` blocks; OpenAI Responses
        emits native ``reasoning`` items via ``output_item.done``.
        Both populate ``provider_blocks`` with reasoning-bearing
        shapes during streaming and need no synthesis here.

        OpenAI Chat Completions (vLLM ``--reasoning-parser``, llama.cpp
        ``reasoning_format``, Gemini's ``/v1beta/openai/`` endpoint
        when it surfaces ``reasoning_content``) streams reasoning as
        ``reasoning_delta`` chunks but never emits a reasoning-bearing
        provider block.  Without this synthesis the captured text would
        be dropped at the end of the stream — visible live, invisible
        on page reload.

        Crucially, GoogleProvider attaches raw tool_call dicts as
        ``provider_blocks`` on the finish chunk for ``thought_signature``
        round-trip (``_google.py:_iter_stream``).  An earlier version
        bailed out whenever ``provider_blocks`` was non-empty, which
        silently lost reasoning text on Google + reasoning_delta turns.
        The fix tests for reasoning-bearing block types specifically
        (see ``_REASONING_BEARING_BLOCK_TYPES``) and APPENDS the
        synthetic block to the existing list rather than replacing it
        — preserving Google's tool-call fidelity blocks alongside the
        new synthetic reasoning entry.

        The synthetic block uses ``type="reasoning_text"`` (NOT
        ``"thinking"``) so it falls through Phase 2's
        ``ANTHROPIC_VALID_BLOCK_TYPES`` shape filter on cross-model
        resumption — protecting against operator-switches from a
        local-model session to Anthropic, which would otherwise hit
        Anthropic's input boundary with an unsigned ``thinking`` block.

        The optional ``source`` field tags the block with the
        originating server (``vllm``, ``llamacpp``, ``sglang``, etc.)
        resolved via :meth:`_resolve_server_type`, which reads
        ``cfg.server_compat["server_type"]`` (the dedicated dataclass
        field hoisted by the model_registry loader, NOT
        ``cfg.capabilities``).  The synthetic block's ``source`` field
        itself is informational metadata; Phase 5's vLLM replay path
        (:meth:`_maybe_attach_vllm_chat_reasoning`) reads
        ``cfg.server_compat`` directly rather than the synthetic
        block's tag.
        """
        text = "".join(reasoning_parts)
        if not text.strip():
            return provider_blocks
        # Native reasoning already present — Anthropic / OpenAI
        # Responses path.  No synth needed; return reference unchanged
        # so the existing identity contract holds.
        for b in provider_blocks:
            if isinstance(b, dict) and b.get("type") in _REASONING_BEARING_BLOCK_TYPES:
                return provider_blocks
        block: dict[str, Any] = {
            "type": "reasoning_text",
            "text": text,
        }
        server_type = self._resolve_server_type()
        if server_type:
            block["source"] = server_type
        # Append rather than replace so non-reasoning fidelity blocks
        # (e.g. Google tool_calls with thought_signature) survive.
        return [*provider_blocks, block]

    def _resolve_replay_reasoning_to_model(
        self,
        alias: str | None = None,
        *,
        caps: ModelCapabilities | None = None,
    ) -> bool:
        """Read ``ModelConfig.replay_reasoning_to_model`` for an alias.

        Used by the streaming + non-streaming wire-build paths to gate
        verbatim reasoning-block replay (Phase 2 of the reasoning-
        persistence feature).  The resolver's miss-fallback is
        ``False``: when no registry / alias is available, or the lookup
        raises, return ``False`` so the provider-side strip path runs.
        Losing the strip on operator-flagged-on models would be a
        worse default than losing the replay on operator-flagged-off
        models — replaying reasoning text against an unknown operator
        preference shouldn't happen.  The False-on-miss matches the
        ``model_definitions`` server-side default for the column, so
        cold workstreams behave the same as unconfigured ones.

        When ``caps`` is provided, the operator flag is AND-gated with
        ``caps.supports_reasoning_replay`` so a model lacking the
        capability silently skips replay even when the operator flag
        is set.  Mirrors the gate in
        ``OpenAIResponsesProvider._build_kwargs`` and protects against
        future Claude entries (or other Anthropic-shaped surfaces)
        shipping with ``supports_reasoning_replay=False``.  When
        ``caps`` is omitted the resolver returns the operator flag
        unchanged — back-compat for callers that haven't been updated
        to thread caps yet.
        """
        target_alias = alias or self._model_alias or ""
        if not self._registry or not target_alias:
            return False
        try:
            cfg: ModelConfig = self._registry.get_config(target_alias)
            operator_on = bool(cfg.replay_reasoning_to_model)
        except Exception:
            return False
        if caps is None:
            return operator_on
        return operator_on and bool(caps.supports_reasoning_replay)

    def _maybe_attach_vllm_chat_reasoning(
        self,
        messages: list[dict[str, Any]],
        provider: LLMProvider,
        alias: str | None = None,
    ) -> list[dict[str, Any]]:
        """Conditionally attach vLLM's non-standard ``reasoning`` field to
        outgoing assistant messages so a vLLM-served reasoning model can
        thread CoT across turns.

        Phase 5 of reasoning-persistence — parallel path to Paths 1+2,
        not a modification.  Three gates:

        1. Provider is ``OpenAIChatCompletionsProvider`` (Chat Completions
           surface, not Responses or Anthropic — those have their own
           replay paths with loud-failure-protected dual-gates).
        2. ``server_compat.server_type == "vllm"`` — bounds blast radius
           to vLLM; canonical OpenAI / llama.cpp / sglang never see the
           non-standard field.
        3. Operator-set ``ModelConfig.replay_reasoning_to_model`` — same
           per-model toggle PR #498 added; defaults False.

        The static ``supports_reasoning_replay`` capability gate that
        guards Paths 1+2 is intentionally NOT used here.  vLLM's chat
        template silently drops ``reasoning`` if the loaded template
        doesn't read ``reasoning_content`` — the gate would add code-
        edit friction (capability tables live in
        ``providers/_openai_common.py``, not the admin UI) without
        preventing the silent failure that's the actual misconfiguration
        risk.  Paths 1+2 keep the dual-gate because their failure mode
        is loud (Anthropic 400 on unsigned thinking, OpenAI Responses
        400 on ResponseReasoningItemParam for non-reasoning models);
        Path C's failure is silent so the gate doesn't help.

        Returns *messages* unchanged when any gate fails.
        """
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        if not isinstance(provider, OpenAIChatCompletionsProvider):
            return messages
        target_alias = alias or self._model_alias or ""
        if not self._registry or not target_alias:
            return messages
        try:
            cfg = self._registry.get_config(target_alias)
        except Exception:
            return messages
        # Read both gate fields off the single ``cfg`` we already
        # fetched, rather than re-entering ``_resolve_server_type``
        # (which would do a second ``get_config`` call).  Mirrors the
        # field location ``_resolve_server_type`` reads, so the two
        # gates stay aligned if the loader ever changes shape.
        sc = cfg.server_compat if isinstance(cfg.server_compat, dict) else None
        if not isinstance(sc, dict) or sc.get("server_type") != "vllm":
            return messages
        if not bool(cfg.replay_reasoning_to_model):
            return messages
        return attach_vllm_chat_reasoning_field(messages)

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
                # SKILL.md spec ``$ARGUMENTS`` payload (#572).  Stored
                # so a resumed workstream re-renders the skill with the
                # same args the original load supplied — otherwise the
                # rehydrate path would silently swap to empty args.
                "skill_arguments": self._skill_arguments,
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
        effort = getattr(self, "reasoning_effort", "") or ""
        if self._skill_name:
            skill_data = get_skill_by_name(self._skill_name)
            if skill_data:
                # Two-pass render — legacy ``{{model}}`` first, spec
                # ``$ARGUMENTS`` second.  Order is load-bearing: user-
                # supplied arg values may legitimately contain
                # ``{{...}}`` patterns (literal text the model wanted to
                # quote), and running ``_render_template`` AFTER the
                # spec substitution would silently re-expand them
                # against the live context.  Running it FIRST resolves
                # the curly-brace placeholders against the immutable
                # skill source, then the spec pass writes substituted
                # values in last — those values can't be re-expanded
                # because the curly-brace renderer has already
                # finished.
                arg_names = self._skill_arg_names(skill_data)
                content = _render_template(skill_data["content"], context)
                self._skill_content = _substitute_skill_args(
                    content,
                    arguments_str=self._skill_arguments,
                    arg_names=arg_names,
                    ws_id=self._ws_id,
                    effort=effort,
                )
                self._check_skill_budget(skill_data)
                self._skill_resources = self._load_skill_resources(
                    skill_data.get("template_id", "")
                )
                if skill_data.get("risk_level") in ("high", "critical"):
                    risk_tier = skill_data["risk_level"]
                    log.warning(
                        "skill.high_risk_loaded",
                        skill=skill_data["name"],
                        risk_level=risk_tier,
                    )
                    self.ui.on_info(
                        f"⚠ Skill '{skill_data['name']}' has risk level: {risk_tier}. "
                        f"Review scan report in admin panel before enabling in production."
                    )
            else:
                log.warning("skill.not_found", name=self._skill_name)
                self._skill_content = None
                self._skill_resources = {}
        else:
            defaults = list_default_skills()
            if defaults:
                # ``arg_names`` and ``arguments_str`` are empty for the
                # default-skill path — defaults are always-on and don't
                # take user-supplied invocation args — but env subs
                # (``${CLAUDE_SESSION_ID}`` / ``${CLAUDE_EFFORT}``)
                # still resolve.  Same render-then-substitute order as
                # the explicit-skill branch (see comment above).
                parts = [
                    _substitute_skill_args(
                        _render_template(t["content"], context),
                        arguments_str="",
                        arg_names=[],
                        ws_id=self._ws_id,
                        effort=effort,
                    )
                    for t in defaults
                ]
                self._skill_content = "\n\n".join(parts)
            else:
                self._skill_content = None
            self._skill_resources = {}
        self._materialize_skill_resources()
        self._validate_skill_resources()

    @staticmethod
    def _skill_arg_names(skill_data: dict[str, Any]) -> list[str]:
        """Extract the named-argument list from a stored skill row.

        Storage shape is a JSON-array TEXT column (``arguments`` —
        added by migration 056).  Malformed JSON falls back to an
        empty list rather than blowing up the load — the
        substitution pass treats missing names as unresolved and
        leaves the ``$<name>`` placeholder as a literal, which is
        the spec's "graceful degradation" behaviour.

        Names are filtered against ``_SKILL_ARG_NAME_RE`` so the
        substitution regex can actually match them.  A SKILL.md
        author writing ``arguments: [issue-number]`` would otherwise
        produce a stored name the ``$<name>`` regex can't reach
        cleanly (``$issue-number`` matches only the ``$issue`` prefix,
        leaving ``-number`` as stray text); the parser-time filter
        drops those names with a warning so the rendered output is
        predictable.
        """
        raw = skill_data.get("arguments") or "[]"
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
        valid: list[str] = []
        dropped: list[str] = []
        for n in parsed:
            if not isinstance(n, str):
                continue
            if _SKILL_ARG_NAME_RE.match(n):
                valid.append(n)
            else:
                dropped.append(n)
        if dropped:
            log.warning(
                "skill.arguments.invalid_names_dropped",
                name=skill_data.get("name", ""),
                dropped=dropped,
            )
        return valid

    def set_skill(self, name: str | None, arguments: str = "") -> None:
        """Set or clear the active skill, optionally with invocation args.

        ``arguments`` carries the spec's $ARGUMENTS payload — re-set
        each time ``set_skill`` is called so a reload doesn't smuggle
        stale args.  Empty string clears them.
        """
        self._skill_name = name
        self._skill_arguments = arguments
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
        # Pass the effective user_id (acting user on shared workstreams,
        # owner otherwise) so the merged tool list includes that user's
        # pool catalog. The static path is included by ``get_tools``
        # regardless; ``user_id=None`` would silently drop pool tools
        # that the LLM is allowed to call.
        mcp_tools = self._mcp_client.get_tools(user_id=self._mcp_effective_user_id)
        self._tools = merge_mcp_tools(INTERACTIVE_TOOLS, mcp_tools)
        self._task_tools = merge_mcp_tools(TASK_AGENT_TOOLS, mcp_tools)
        self._render_agent_tool_descriptions()
        self._rebuild_tool_search()

    def _render_agent_tool_descriptions(self) -> None:
        """Inject the live alias list into the ``model`` parameter description
        on the task_agent tool.

        Lets the calling LLM see which aliases are valid right now.
        Called on session init and on registry reload (via
        ``refresh_agent_tool_schemas``).  No-op when no registry is
        configured (CLI single-model case).

        Replaces affected tool dicts with deep copies so the module-level
        tool-list constants stay untouched across sessions.

        task_agent lives in ``self._tools`` (the main session's tool set) —
        not in ``self._task_tools``, which is what *sub-agents* see
        (sub-agents don't get delegation tools to avoid infinite recursion).
        """
        if self._registry is None:
            return
        # Hide ``default`` from the alias list — the LLM reads the English
        # word and picks it explicitly, which routes to whichever model
        # carries that alias rather than the operator-configured per-role
        # default (task_alias).  Omitting ``model=`` already selects the
        # per-role default; offering the literal name as an alternative
        # invites the bypass.
        aliases = sorted(a for a in self._registry.list_aliases() if a != "default")
        aliases_str = ", ".join(f"`{a}`" for a in aliases)

        new_tools: list[dict[str, Any]] = []
        for tool in self._tools:
            fn = tool.get("function") or {}
            name = fn.get("name", "")
            if name != "task_agent":
                new_tools.append(tool)
                continue
            new_tool = copy.deepcopy(tool)
            props = new_tool.get("function", {}).get("parameters", {}).get("properties", {})
            if "model" in props:
                # Always rewrite — a reload that filters down to no
                # alternatives (only ``default`` remains in the registry)
                # must clear any stale alias names left over from a prior
                # render, not return early and leave them in place.
                if aliases:
                    props["model"]["description"] = (
                        "Optional model alias to run this task_agent on. "
                        "Omit to use the operator-configured task model. "
                        f"Available aliases: {aliases_str}."
                    )
                else:
                    props["model"]["description"] = (
                        "Optional model alias to run this task_agent on. "
                        "Omit to use the current session model. "
                        "(No alternative aliases configured in this session.)"
                    )
            new_tools.append(new_tool)
        self._tools = new_tools

    def refresh_agent_tool_schemas(self) -> None:
        """Public entry point: re-render the task_agent tool
        description to reflect the current ModelRegistry state, and
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
        # Reset judges so they pick up the new model/provider
        if self._judge is not None:
            self._judge = None
        if self._output_guard_judge is not None:
            self._output_guard_judge = None
            # Rate limiter is tied to the judge model; a swap invalidates it.
            self._output_guard_judge_rl = TokenBucket(rate=1.0, burst=60)
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
                reranker=self._bm25_reranker(),
            )
            # Restore previously expanded tools that still exist
            if old_expanded:
                self._tool_search.expand_visible(old_expanded)
        else:
            self._tool_search = None

    def set_watch_runner(self, runner: Any) -> None:
        """Inject the server-level WatchRunner and register a dispatch fn
        that routes watch results onto this session's NudgeQueue.

        The dispatch closure is the unified pull-model path: each watch
        fire enqueues a ``"watch_triggered"`` entry on ``"any"`` channel,
        and the existing drain seams (USER_DRAIN, TOOL_DRAIN,
        ``IdleNudgeWatcher`` IDLE wake) emit it as a first-class
        ``{"role": "system"}`` turn — uniform with every other metacog
        nudge.

        The closure carries:
          - a soft cap on per-session ``"watch_triggered"`` depth via
            :data:`_WATCH_QUEUE_SOFT_CAP` + drop-oldest-on-saturation.
          - producer-side :func:`sanitize_payload` over the whole
            formatted message so steering-vector / control-char payloads
            sourced from arbitrary shell output can't tamper with the
            envelope at interpolation time.

        No ``valid_until`` predicate is wired: ``WatchRunner._poll_watch``
        commits ``active=False`` for terminal fires right after dispatch
        returns, and an ``is_watch_active`` predicate would race that
        write at drain time and drop the fire the model was meant to see.
        A user-cancelled watch's last splat is informative (the reminder
        carries ``is_final=True``), not stale-noise to suppress.
        """
        self._watch_runner = runner
        nudge_queue = self._nudge_queue
        ws_id = self._ws_id

        def _dispatch(reminder: dict[str, Any], watch_id: str) -> None:
            # ``reminder`` is the structured dict produced by
            # :func:`build_watch_reminder`.  ``text`` carries the
            # formatted body — sanitised here over the full string so
            # steering-vector / control-char payloads sourced from
            # arbitrary shell output can't tamper with the envelope at
            # interpolation time.  The remaining fields ride as the
            # queue entry's ``metadata`` → sibling keys on the
            # ``watch_triggered`` system turn, surfaced in the operator
            # bubble (command preview + poll counter).
            text = reminder.get("text", "") if isinstance(reminder, dict) else ""
            sanitized = sanitize_payload(text)
            if not sanitized:
                # All control chars / empty after strip — silently drop.
                return
            # Soft cap drops oldest — latest output is most useful.
            if nudge_queue.cap_at_or_drop_oldest(
                "watch_triggered", _WATCH_QUEUE_SOFT_CAP, channel="any"
            ):
                log.warning(
                    "watch_dispatch.queue_full ws=%s cap=%d dropped_oldest=True",
                    ws_id,
                    _WATCH_QUEUE_SOFT_CAP,
                )

            def _maybe_sanitize(v: Any) -> Any:
                return sanitize_payload(v) if isinstance(v, str) else v

            metadata = {
                k: _maybe_sanitize(reminder[k])
                for k in WATCH_REMINDER_OPTIONAL_KEYS
                if k in reminder
            }
            nudge_queue.enqueue(
                "watch_triggered",
                sanitized,
                "any",
                metadata=metadata or None,
            )

        runner.set_dispatch_fn(self._ws_id, _dispatch)

    def close(self) -> None:
        """Release resources (listener registrations, etc.)."""
        if self._judge_cancel_event is not None:
            self._judge_cancel_event.set()
        if self._mcp_client and self._mcp_refresh_cb:
            # ``user_id`` MUST match the value used at registration —
            # the listener identity is ``(user_id, callback)``, not
            # callback alone. ``bind_acting_user`` may have re-scoped
            # the registrations since construction, so the tracked
            # ``_mcp_listener_user_id`` (not ``_mcp_user_id``) is the
            # registration identity.
            self._mcp_client.remove_listener(
                self._mcp_refresh_cb, user_id=self._mcp_listener_user_id
            )
            self._mcp_refresh_cb = None
        if self._mcp_client and self._mcp_resource_cb:
            # ``user_id`` MUST mirror the value passed at registration —
            # the listener identity is ``(user_id, callback)`` and an
            # unscoped removal would leave the registration in place.
            self._mcp_client.remove_resource_listener(
                self._mcp_resource_cb, user_id=self._mcp_listener_user_id
            )
            self._mcp_resource_cb = None
        if self._mcp_client and self._mcp_prompt_cb:
            self._mcp_client.remove_prompt_listener(
                self._mcp_prompt_cb, user_id=self._mcp_listener_user_id
            )
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
        status: EffectStatus | None = None,
    ) -> None:
        """Notify the UI and record error flag for message persistence.

        ``status`` is the typed effect disposition (HYPOTHESIS.md effect-record
        appendix), set only for non-ordinary outcomes — UNKNOWN on a timeout or
        mid-flight cancel — and folded onto the persisted tool turn. ``None``
        leaves the turn unclassified (the ordinary case)."""
        if is_error:
            self._tool_error_flags[call_id] = True
        if status is not None:
            self._tool_status[call_id] = status
        self.ui.on_tool_result(call_id, name, output, is_error=is_error)

    def _ui_event_id(self) -> int | None:
        """Current per-ws SSE ring-buffer high-water mark for stamping
        saved messages with the ``Last-Event-ID`` resume cursor.

        ``None`` for UIs without an integer counter — CLI / eval /
        placeholder UIs (no ``_event_id``), and test doubles whose
        ``self.ui`` is a ``MagicMock`` (a non-int ``_event_id`` would
        otherwise reach the INSERT and fail to bind).  Those rows stay
        NULL and are treated by ``/history`` as "no fast-forward cursor
        available" (the synthetic-snapshot floor).
        """
        eid = getattr(self.ui, "_event_id", None)
        return eid if isinstance(eid, int) else None

    def _tool_def_chars(self) -> int:
        """Total serialized char size of the active tool definitions (resent on
        every request, folded into the provider's ``prompt_tokens``)."""
        return sum(len(json.dumps(t)) for t in (self._get_active_tools() or []))

    def _tool_def_tokens(self) -> int:
        """Estimated token cost of the active tool definitions."""
        return int(self._tool_def_chars() / self._chars_per_token)

    def _estimated_prompt_tokens(self) -> int:
        """Best estimate of the current prompt size, in tokens.

        Anchors to the provider-reported ``prompt_tokens`` from the last API
        call — which already includes tool-definition tokens and the cached
        prefix (providers fold cached + non-cached into one count at the API
        boundary; see ``_anthropic.py`` ``total_input``) — and adds a local
        estimate for only the messages appended since calibration.  Falls
        back to a pure local estimate before the first API call.

        Single source of truth for "how full is the context": tool-output
        truncation (via :meth:`_remaining_token_budget`) and the
        auto-compaction triggers all read it, so they cannot disagree about
        the fullness of the same state.
        """
        if self._last_usage:
            # Clamp the index: a stale _calibrated_msg_count must not
            # over-slice after compaction or message-list mutations.
            start = min(self._calibrated_msg_count, len(self._msg_tokens))
            return self._last_usage["prompt_tokens"] + sum(self._msg_tokens[start:])
        # No provider anchor yet (e.g. a just-resumed session before its first
        # API call): add an estimate for the tool definitions, which are resent
        # on every request and folded into the provider's prompt_tokens above.
        # Omitting them here made a resumed session undercount and skip proactive
        # compaction until the first reply re-anchored the estimate.
        tool_def_tokens = self._tool_def_tokens()
        return self._system_tokens + sum(self._msg_tokens) + tool_def_tokens

    def _remaining_token_budget(self) -> int:
        """Estimate how many tokens are available for new content.

        Reserves a response budget (capped at 25% of context window, since
        ``max_tokens`` is an upper bound, not guaranteed consumption) plus a
        5% safety margin.  Returns at least 0.  Context fullness comes from
        :meth:`_estimated_prompt_tokens` (provider-anchored), shared with the
        auto-compaction triggers so truncation and compaction agree.
        """
        used = self._estimated_prompt_tokens()
        response_reserve = min(self.max_tokens, self.context_window // 4)
        safety_margin = int(self.context_window * 0.05)
        return max(0, self.context_window - used - response_reserve - safety_margin)

    def _maybe_compact_midturn(self, my_generation: int = 0) -> None:
        """Cooperative mid-turn compaction policy.

        Reads the provider-anchored fullness estimate (the same measure
        tool-output truncation uses, so the two cannot disagree about the
        same state) and escalates:

        - over the **hard** ceiling — no turn to spare, compact now;
        - over the **soft** threshold and already advised — the model kept
          working past the wrap-up advisory, compact;
        - over the **soft** threshold, first crossing — append a
          ``compaction_pending`` advisory asking the model to reach a stopping
          point and record its remaining plan, so the summariser preserves it
          (a register-spill onto the transcript before the tape is collapsed),
          and latch ``_compaction_advised``.

        No-op below the soft threshold.  The latch is cleared by
        :meth:`_compact_messages` and at end-of-turn.
        """
        est = self._estimated_prompt_tokens()
        if self._compaction_owed(est):
            self._do_auto_compact("mid-turn", my_generation=my_generation)
        elif self._over_soft(est):
            self._append_system_turn("compaction_pending", format_nudge("compaction_pending"))
            self._compaction_advised = True

    def _compaction_owed(self, used: int | None = None) -> bool:
        """True when fullness mandates compaction now: over the hard ceiling, or
        over the soft threshold after the model worked past a wrap-up advisory.

        Shared by the compact-before-truncate check in the send loop and
        :meth:`_maybe_compact_midturn`.  Pass a precomputed ``used`` to avoid a
        redundant :meth:`_estimated_prompt_tokens` sum.  The hard ceiling sits a
        band above the soft threshold (capped at 95%); if ``auto_compact_pct`` is
        set above that, the band collapses and any over-soft state is "owed".
        """
        if used is None:
            used = self._estimated_prompt_tokens()
        return self._over_hard(used) or (self._over_soft(used) and self._compaction_advised)

    def _over_soft(self, used: int) -> bool:
        """True when ``used`` tokens exceed the soft auto-compaction threshold.

        Single source for the ``context_window * auto_compact_pct`` predicate
        shared by the mid-turn policy, :meth:`_compaction_owed`, and the
        end-of-turn check, so they can't drift.
        """
        return used > self.context_window * self.auto_compact_pct

    def _over_hard(self, used: int) -> bool:
        """True when ``used`` tokens exceed the hard ceiling — a band above the
        soft threshold (capped at 95%).

        The "compact now, no turn to spare" line.  Single source shared by
        :meth:`_compaction_owed` and the proactive pre-send guard; the latter uses
        it ALONE (not ``_compaction_owed``, whose advised-soft arm belongs to the
        cooperative mid/end-of-turn wind-down, not to a pre-send emergency).
        """
        return used > self.context_window * min(0.95, self.auto_compact_pct + 0.10)

    def _do_auto_compact(
        self,
        where: str = "",
        preserve_tail: int = 0,
        my_generation: int = 0,
        carry_spill: bool = False,
    ) -> bool:
        """Emit the auto-compaction notice, compact, and refresh the status
        line.  Shared by the mid-turn policy (:meth:`_maybe_compact_midturn`)
        and the end-of-turn check so the notice wording, the percentage, and
        the post-compaction status refresh stay in lockstep.  ``where`` is an
        optional qualifier for the notice (e.g. ``"mid-turn"``); ``preserve_tail``
        is forwarded to :meth:`_compact_messages` (e.g. to keep an in-flight
        tool-call turn during compact-before-truncate); ``my_generation`` is
        forwarded so the message swap aborts if a newer send supersedes this one
        mid-compaction (0 = the manual /compact path, which has no generation);
        ``carry_spill`` is forwarded so the end-of-turn site can copy the
        model's wind-down turn onto the summary verbatim.
        Returns whether a summary was actually produced (False if compaction
        bailed) so callers can avoid acting on a compaction that did not happen."""
        qualifier = f" {where}" if where else ""
        pct_display = round(self.auto_compact_pct * 100)
        self.ui.on_info(
            f"\n[Auto-compacting{qualifier}: prompt exceeds {pct_display}% of context window]"
        )
        compacted = self._compact_messages(
            auto=True,
            preserve_tail=preserve_tail,
            my_generation=my_generation,
            carry_spill=carry_spill,
        )
        self._print_status_line()
        return compacted

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
            # Gather first user message and first assistant reply.
            # Snapshot ``self.messages`` (C-level atomic copy under the
            # GIL): this runs in a background thread that may now fire
            # while the main ``send`` loop is still streaming and
            # appending turns, so iterating the live list directly could
            # raise "list changed size during iteration".
            user_msg = ""
            asst_msg = ""
            for m in list(self.messages):
                content = m.text  # joins text blocks; multipart attachments contribute none
                # Skip the synthetic [Conversation summary] turn (source tag,
                # not content match — same rule as _find_turn_boundaries):
                # after a compaction it is messages[0], and titling from the
                # bare label yields a meaningless title.
                if m.role is Role.USER and not user_msg and m.source != COMPACTION_SOURCE:
                    user_msg = content[:300]
                elif m.role is Role.ASSISTANT and not asst_msg:
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

            # No temperature override here: defer to the session/registry
            # temperature (``_utility_completion`` default).  A manual refresh
            # leans on the prompt's "generate a DIFFERENT title" instruction and
            # the changing ``current_title`` it feeds in for variety, rather than
            # forcing a hotter sample on top of the operator's chosen model.

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
                max_tokens=_TITLE_MAX_TOKENS,
            )
            raw = result.content or ""
            log.info("ws.title.llm_response", ws_id=ws_id[:8], raw=raw[:200])
            # Take the assistant's answer (``content``), never its reasoning.
            # Reuse the canonical reasoning stripper, then drop a leftover close
            # tag from lanes that pre-inject the opening ``<think>`` into the
            # prompt (only ``</think>`` reaches ``content``). See ``_TITLE_*``.
            stripped = self._strip_reasoning(raw)
            for _close in ("</think>", "</reasoning>"):
                _pos = stripped.rfind(_close)
                if _pos != -1:
                    stripped = stripped[_pos + len(_close) :]
            # First non-empty line, with a ``Title:`` label and wrapping
            # markdown/quote decoration peeled (internal punctuation kept).
            line = next((ln for ln in stripped.splitlines() if ln.strip()), "")
            line = _TITLE_LABEL_RE.sub("", line.strip(_TITLE_WRAP_CHARS))
            title = line.strip(_TITLE_WRAP_CHARS)[:_TITLE_MAX_CHARS]
            if title and self._ws_id == ws_id:
                log.info("ws.title.updating", ws_id=ws_id[:8], title=title)
                update_workstream_title(ws_id, title)
                self.ui.on_rename(title)
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
        turns = load_message_turns(ws_id)
        if not turns:
            return False
        if not fork:
            self._ws_id = ws_id
        self.messages = turns
        # Shared-workstream state is per-workstream: this session object now
        # points at (possibly different) history, so forget and re-derive.
        self._reset_shared_state()
        self._read_files.clear()
        self._repeat_detector.clear()
        self._last_usage = None
        self._calibrated_msg_count = 0
        self._title_generated = True  # don't re-title resumed workstreams
        self._msg_tokens = [
            max(1, int(self._msg_char_count(m) / self._chars_per_token)) for m in self.messages
        ]
        log.info(
            "Resuming ws=%s: %d messages, provider=%s, model=%s",
            ws_id,
            len(self.messages),
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
                self._output_guard_judge = None  # same — re-create
                self._output_guard_judge_rl = TokenBucket(rate=1.0, burst=60)
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
            elif saved_alias or saved_model:
                # Saved alias is unset or no longer in the registry.
                # Don't copy ``saved_model`` onto the constructor's
                # default provider/client — pairing a removed model
                # name with the default provider produces an API call
                # the default provider can't service, which is exactly
                # the broken state operators see today on the reopen
                # path.  The constructor already resolved a coherent
                # default; keep it intact and warn so the missing
                # alias is auditable.
                log.warning(
                    "Resume: saved alias=%r model=%r unreachable; "
                    "keeping default provider=%s model=%s",
                    saved_alias,
                    saved_model,
                    type(self._provider).__name__,
                    self.model,
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
                # Restore #572's invocation-args payload BEFORE
                # ``_load_skills`` so the substitution pass renders with
                # the original args instead of an empty default.
                self._skill_arguments = config.get("skill_arguments", "") or ""
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
            for turn in self.messages:
                msg = turn_to_dict(turn)
                tc = msg.get("tool_calls")
                tc_json = json.dumps(tc) if tc else None
                # Provider-fidelity blocks ride the in-memory
                # ``_provider_content`` key (the live save path at
                # ``_run_loop`` reads the same key); the storage column is
                # ``provider_data``.  Reading ``provider_data`` here silently
                # lost the blocks on every fork.
                pd = msg.get("_provider_content")
                try:
                    pd_str = json.dumps(pd) if pd and not isinstance(pd, str) else pd
                except (TypeError, ValueError):
                    pd_str = None
                src = msg.get("_source")
                # The ``conversations.meta`` column rides the fork too, so a
                # forked watch-result keeps its structured card and a forked
                # user turn keeps its sender attribution. The two sources are
                # role-exclusive (``_source_meta`` rides SYSTEM turns, the
                # sender stamp rides USER turns — see ``reconstruct_turns``),
                # mirroring the live save paths in ``_run_loop`` and
                # ``_append_user_turn``.
                sm = msg.get("_source_meta")
                sender = msg.get("_sender")
                if isinstance(sm, dict) and sm:
                    meta_json = json.dumps(sm)
                elif isinstance(sender, str) and sender:
                    meta_json = json.dumps({"sender": sender})
                else:
                    meta_json = None
                bulk_rows.append(
                    {
                        "ws_id": self._ws_id,
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", ""),
                        "tool_name": msg.get("name"),
                        "tool_call_id": msg.get("tool_call_id"),
                        "tool_calls": tc_json,
                        "provider_data": pd_str,
                        "source": src if isinstance(src, str) and src else None,
                        "is_error": bool(msg.get("is_error", False)),
                        "producer": msg.get("_producer"),
                        "meta": meta_json,
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
            self._queue_user_advisory("resume", format_nudge("resume"))
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
            # Round to the top of the hour. Anthropic and OpenAI both cache the
            # system prefix; minute-precision time stamps invalidated the cache
            # on every turn that crossed a minute boundary. Hour-precision still
            # gives the model time-of-day awareness without paying for a full
            # prefix recompute every ~60 seconds.
            # Refresh shared-workstream state so the banner matches the current
            # participant set (fresh compose or rehydrated multi-user history).
            self._recompute_shared_state()
            ctx = SessionContext(
                current_datetime=now.strftime("%Y-%m-%dT%H:00"),
                timezone=now.tzname() or "UTC",
                username=self._username or self._user_id or "unknown",
                project=self._project_name,
                shared=self._shared_workstream,
                ws_id=self._ws_id,
                project_id=self._project_id,
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
        # Capability-gated system-prompt additions.  Resolve caps once here, via
        # _resolve_capabilities (NOT _get_capabilities) so we don't populate
        # self._cached_capabilities during __init__ — that would make later
        # patches of provider.get_capabilities (common in tests) silently no-op
        # for the primary session model.  Cheap to recompute; no caching needed.
        # Guarded on _provider for early/edge init paths.
        caps = (
            self._resolve_capabilities(self._provider, self.model, self._model_alias)
            if self._provider is not None
            else None
        )
        # Operator-instruction trust anchor — declared only on the fold path.
        # The native mid-conversation-system path (claude-opus-4-8,
        # claude-fable-5) delivers operator turns as real {"role":"system"}
        # messages with no fence, so no [start system-reminder_{nonce}] marker
        # appears and no declaration applies.
        if caps is not None and not caps.supports_mid_conversation_system:
            dev_parts.append("\n\n" + build_operator_instruction_declaration(self._envelope_nonce))
        # Shared-workstream trust declaration — pins the authentic sender-label
        # nonce in the cached prefix so a participant's typed `[message from …]`
        # look-alike cannot forge another sender's attribution.  Gated on the
        # (latched) shared flag, so single-user prompts are unchanged and the
        # prefix flips at most once per workstream.  Applies on every provider
        # lane (labels ride the wire content, not the fold), unlike the
        # fold-only operator declaration above.
        if self._shared_workstream:
            dev_parts.append("\n\n" + build_shared_workstream_declaration(self._sender_label_nonce))
        # Tool search hint (client-side mode only — native mode needs no hint).
        if self._tool_search and caps is not None and not caps.supports_tool_search:
            dev_parts.append(
                "\n\nAdditional tools are available via tool_search. "
                "Use it when you need a capability not in your current tool set."
            )
        # MCP resource catalog (lets the model know what's available for read_resource)
        if self._mcp_client:
            # Per-user merge: pool entries for the effective user (acting
            # user on shared workstreams, owner otherwise) are included;
            # other users' pool resources are not.
            all_resources = self._mcp_client.get_resources(user_id=self._mcp_effective_user_id)
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
            # Per-user merge: pool entries for the effective user (acting
            # user on shared workstreams, owner otherwise) are included;
            # other users' pool prompts are not.
            prompts = self._mcp_client.get_prompts(user_id=self._mcp_effective_user_id)
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
        context = extract_recent_context(dicts_from_turns(self.messages))
        if context.strip():
            # Composed against a real user-message query at least once; send()
            # uses this to know the deferred first-turn recompose is done.
            self._system_composed_with_context = True
        visible_mems, candidate_source = self._select_memory_candidates(context)
        if visible_mems:
            thr = self._bm25_rerank_threshold()
            relevant = score_memories(
                visible_mems,
                context,
                k=self._mem_cfg.relevance_k,
                reranker=self._bm25_reranker(thr),
                rerank_filters=thr > 0,
            )
            log.info(
                "memory.composition",
                source=candidate_source,
                candidates=len(visible_mems),
                injected=len(relevant),
            )
            # Access metadata tracks what the model actually saw — touch the
            # injected top-k, not the candidate pool.
            self._touch_injected_memories(relevant)
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
        new_system_messages.append({"role": "system", "content": "\n".join(dev_parts)})
        # Atomic swap — readers see either old or new, never partial
        self.system_messages = new_system_messages
        # Agent prefix: system + developer only (no memories)
        self._agent_system_messages = list(new_system_messages)

    def _full_messages(self) -> list[dict[str, Any]]:
        """System messages + conversation history as wire dicts.

        ``self.messages`` is the canonical ``Turn`` trajectory; lowering it here
        emits by-reference attachments as ``{type: kind, attachment_id}``
        placeholders.  The provider translator materializes them to inline bytes
        via :meth:`_resolve_attachments` — resolution lives at the C layer."""
        return self.system_messages + dicts_from_turns(self.messages)

    def _resolve_attachments(
        self, ids: list[str], caps: ModelCapabilities | None = None
    ) -> dict[str, Any]:
        """Resolve content-addressed attachment ids to inline wire content parts.

        The send-time materialization of the by-reference content lane: handed to
        the provider translator, which calls it with the placeholder ids it finds
        and expands each to the inline part the wire needs.  Blobs are
        batch-fetched from the content-addressed store; a pruned id resolves to
        nothing and the translator drops its placeholder.

        Kinds the active model can't ingest natively (pdf without ``supports_pdf``,
        audio without ``supports_audio_input``) are converted client-side here —
        see :meth:`_wire_content_part`.  This is the wire path only; the display /
        export resolvers stay native-only, so no conversion (or external STT call)
        fires on a history render."""
        if not ids:
            return {}
        # caps is the ACTIVE attempt's capabilities, threaded from _try_stream so
        # a fallback to a model with different media support converts on the
        # right caps; default to the primary only when called without one.
        if caps is None:
            caps = self._get_capabilities()
        # Per-send memo (see send()): the wire resolver is re-invoked on every
        # round-trip and per fallback model, so without this a PDF in history is
        # re-rasterized / a blob re-base64'd once per round-trip.  Key on
        # (id, caps-signature): the same stored blob materializes differently per
        # capability set, and a fallback to a different-caps model can resolve
        # within one send.  Set in send() and cleared in its finally, so it is
        # None outside a send; the wire resolver runs only during a send.  A None
        # cache disables memoization (the original behavior).
        cache = self._wire_part_cache
        caps_sig = (caps.supports_pdf, caps.supports_vision, caps.supports_audio_input)
        out: dict[str, Any] = {}
        missing: list[str] = []
        for att_id in ids:
            hit = cache.get((att_id, caps_sig)) if cache is not None else None
            if hit is not None:
                out[att_id] = hit
            else:
                missing.append(att_id)
        if missing:
            for att in get_attachments(missing):
                part = self._wire_content_part(att, caps)
                if part is not None:
                    aid = str(att["attachment_id"])
                    out[aid] = part
                    if cache is not None:
                        cache[(aid, caps_sig)] = part
        return out

    def _wire_content_part(
        self, att: dict[str, Any], caps: ModelCapabilities
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """The active model's inline part(s) for one blob: native where
        supported, else the fallback ladder for a kind it can't read.

        PDF → rasterized page images (vision primary) → perception → extracted
        text → placeholder.  Image → native image_url, or perception first when
        the primary has no vision.  Audio → STT transcript → perception →
        placeholder.  Perception (the ``perception.model_alias`` role) is the
        universal bottom tier: it engages only when the primary can't handle the
        kind and a capable perception model is configured.  A PDF rasterized to
        images returns several parts."""
        kind = att.get("kind")
        if kind == "pdf" and not caps.supports_pdf:
            # Vision primary: rasterize pages to images (better fidelity, esp.
            # for scanned PDFs with no text layer).  Non-vision primary:
            # perception, else extracted text / placeholder.
            if caps.supports_vision:
                return self._pdf_rasterize_fallback_parts(att)
            return self._pdf_nonvision_part(att)
        if kind == "image" and not caps.supports_vision:
            perceived = self._perception_fallback_part(att, "image")
            if perceived is not None:
                return perceived
            # No usable perception backend (none configured, or it can't see):
            # emit the native image_url unchanged — a no-vision model ignores it.
            # Image is intentionally left ungated here (pre-existing behavior).
        if kind == "audio" and not caps.supports_audio_input:
            return self._audio_fallback_part(att)
        return attachment_to_content_part(att)

    def _pdf_rasterize_fallback_parts(
        self, att: dict[str, Any]
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Vision model without native PDF: render pages to images (one part per
        page).  Falls back to text extraction if rendering yields nothing."""
        import base64

        from turnstone.core.pdf import rasterize_pdf

        raw = att.get("content")
        pages = rasterize_pdf(raw) if isinstance(raw, bytes) else []
        if not pages:
            return self._pdf_text_fallback_part(att)
        return [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(p).decode('ascii')}"
                },
            }
            for p in pages
        ]

    def _pdf_text_fallback_part(self, att: dict[str, Any]) -> dict[str, Any]:
        """Non-PDF model: extract the PDF's text and carry it as a text document."""
        from turnstone.core.pdf import extract_pdf_text

        name = str(att.get("filename") or "document.pdf")
        raw = att.get("content")
        text = extract_pdf_text(raw) if isinstance(raw, bytes) else ""
        if not text:
            return {
                "type": "text",
                "text": (
                    f"[PDF attachment '{safe_attachment_label(name)}' — no extractable "
                    "text; this model cannot read PDFs natively]"
                ),
            }
        return {
            "type": "document",
            "document": {
                "name": f"{name} (extracted text)",
                "media_type": "text/plain",
                "data": text,
            },
        }

    def _pdf_nonvision_part(self, att: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
        """Non-vision primary + PDF: perception (renders pages for a perception
        model that can see) when configured, else extracted text / placeholder."""
        perceived = self._perception_fallback_part(att, "pdf")
        if perceived is not None:
            return perceived
        return self._pdf_text_fallback_part(att)

    def _audio_fallback_part(self, att: dict[str, Any]) -> dict[str, Any]:
        """Non-omni primary + audio: STT transcript (preferred), else perception
        (if the perception model can hear), else a placeholder."""
        transcript = self._stt_transcript_part(att)
        if transcript is not None:
            return transcript
        perceived = self._perception_fallback_part(att, "audio")
        if perceived is not None:
            return perceived
        name = str(att.get("filename") or "audio")
        return {
            "type": "text",
            "text": (
                f"[audio attachment '{safe_attachment_label(name)}' — "
                "no transcription backend configured]"
            ),
        }

    def _stt_transcript_part(self, att: dict[str, Any]) -> dict[str, Any] | None:
        """Transcribe via the STT role, or ``None`` when no STT role is
        configured or the transcript is empty (caller falls through to
        perception).  Only engages a configured backend — never a surprise call."""
        from turnstone.core.audio import resolve_role_alias, transcribe_cached

        raw = att.get("content")
        alias = resolve_role_alias(
            config_store=self._config_store, registry=self._registry, role="stt"
        )
        if not alias or not isinstance(raw, bytes):
            return None
        name = str(att.get("filename") or "audio")
        transcript = transcribe_cached(
            registry=self._registry,
            alias=alias,
            content_hash=str(att.get("attachment_id")),
            data=raw,
            filename=name,
        )
        if not transcript:
            return None
        return {
            "type": "text",
            "text": (
                f"[Transcript of audio attachment '{safe_attachment_label(name)}' "
                f"(untrusted)]\n\n{transcript}"
            ),
        }

    def _resolve_perception(
        self,
    ) -> tuple[LLMProvider, Any, str, str, ModelCapabilities] | None:
        """Resolve the perception role → ``(provider, client, model, alias, caps)``.

        ``None`` when no ``perception.model_alias`` is configured / resolvable, so
        the caller falls through to the next fallback tier."""
        from turnstone.core.perception import PERCEPTION_SETTING

        if self._config_store is None or self._registry is None:
            return None
        alias = (self._config_store.get(PERCEPTION_SETTING) or "").strip()
        if not alias or not self._registry.has_alias(alias):
            return None
        try:
            client, model, _cfg = self._registry.resolve(alias)
            provider = self._registry.get_provider(alias)
            caps = self._resolve_capabilities(provider, model, alias)
        except Exception as exc:
            log.warning("perception alias %r not resolvable: %s", alias, exc)
            return None
        return provider, client, model, alias, caps

    def _perception_parts(self, att: dict[str, Any], kind: str) -> list[dict[str, Any]]:
        """Build the OpenAI-shaped parts handed to the perception model: PDF →
        rasterized page images; image / audio → the native content part."""
        raw = att.get("content")
        if not isinstance(raw, bytes):
            return []
        if kind == "pdf":
            import base64

            from turnstone.core.pdf import rasterize_pdf

            return [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64.b64encode(p).decode('ascii')}"
                    },
                }
                for p in rasterize_pdf(raw)
            ]
        part = attachment_to_content_part(att)  # image_url / input_audio, native shape
        return [part] if part is not None else []

    def _perception_fallback_part(self, att: dict[str, Any], kind: str) -> dict[str, Any] | None:
        """Universal bottom-tier fallback: have the configured perception model
        perceive the attachment and carry its output as text.  ``None`` when no
        perception backend is configured, it can't handle this modality, or it
        produced nothing — the caller falls through."""
        resolved = self._resolve_perception()
        if resolved is None:
            return None
        provider, client, model, alias, caps = resolved
        if kind in ("pdf", "image") and not caps.supports_vision:
            return None
        if kind == "audio" and not caps.supports_audio_input:
            return None
        from turnstone.core.perception import describe_cached, describe_peek

        # Peek the (alias, content_hash) memo BEFORE building parts: for a PDF,
        # _perception_parts rasterizes every page, but describe_cached returns a
        # memoized description without touching parts on a hit — so on a cross-send
        # hit the rasterize would be pure waste.
        content_hash = str(att.get("attachment_id"))
        text = describe_peek(alias=alias, content_hash=content_hash)
        if text is None:
            parts = self._perception_parts(att, kind)
            if not parts:
                return None
            text = describe_cached(
                provider=provider,
                client=client,
                model=model,
                alias=alias,
                content_hash=content_hash,
                parts=parts,
            )
        if not text:
            return None
        name = str(att.get("filename") or kind)
        return {
            "type": "text",
            "text": (
                f"[Perception of {kind} attachment '{safe_attachment_label(name)}' "
                f"(untrusted)]\n\n{text}"
            ),
        }

    def _resolve_display_name(self, user_id: str) -> str:
        """Resolve a user_id to its display username for shared-workstream
        labels / join notes — the same *kind* of identity the owner gets in the
        Session Context banner (``self._username``), rather than a raw id hash.

        The owner short-circuits to ``self._username`` (already known, matches
        the banner exactly). Others are looked up once via storage and cached on
        the session; a lookup miss / no user row falls back to the raw id so the
        label degrades gracefully rather than disappearing."""
        if not user_id:
            return ""
        if user_id == self._mcp_user_id and self._username:
            return self._username
        cached = self._sender_name_cache.get(user_id)
        if cached is not None:
            return cached
        name = user_id
        try:
            storage = get_storage()
            if storage:
                row = storage.get_user(user_id)
                if row:
                    # username-first (unlike auth.py's display_name-first for the
                    # UI): sender labels must read as the SAME identity kind the
                    # owner gets in the banner (``self._username`` = users.username),
                    # so owner and participants are labelled consistently.
                    name = row.get("username") or row.get("display_name") or user_id
                # Cache hits and definite misses (row is None). A transient
                # storage error, by contrast, skips the cache and falls through
                # to return the raw id, so a later call can retry rather than
                # pinning the sender to their id for the session's lifetime.
                self._sender_name_cache[user_id] = name
        except Exception:
            log.debug("display-name lookup failed for user=%s", user_id, exc_info=True)
        return name

    def _invalidate_shared_state(self) -> None:
        """Mark shared-workstream state for recompute.

        The cheap flag half of the per-turn memo in
        :meth:`_recompute_shared_state`; called when a sender-stamped user turn
        is appended (the only live event that can change the participant set)."""
        self._senders_dirty = True

    def _reset_shared_state(self) -> None:
        """Forget shared-workstream state entirely.

        For :meth:`resume`, which points this session object at (possibly
        different) history — the monotonic guarantees in
        :meth:`_recompute_shared_state` hold per *workstream*, not per session
        object, so carrying senders across a resume would leak one
        workstream's participant set into another's framing."""
        self._shared_workstream = False
        self._known_senders = set()
        self._db_senders_loaded = False
        self._senders_dirty = True

    def _load_persisted_senders(self) -> set[str]:
        """One-time full-history sender read for :meth:`_recompute_shared_state`.

        Compaction narrows ``self.messages`` to a ``[summary] + [tail]`` slice,
        so scanning it alone forgets participants whose turns were summarized
        away. The persisted rows keep every sender ever stamped; read them once
        per workstream. A storage error leaves ``_db_senders_loaded`` unset so
        the next recompute (at most one per user turn, via the memo) retries
        instead of pinning an incomplete participant set for the session's
        lifetime."""
        try:
            storage = get_storage()
            if storage is not None:
                senders = {s for s in storage.list_message_senders(self._ws_id) if s}
                self._db_senders_loaded = True
                return senders
            # No storage configured (ephemeral session): nothing to read, ever.
            self._db_senders_loaded = True
        except Exception:
            log.debug("persisted-sender load failed for ws=%s", self._ws_id, exc_info=True)
        return set()

    def _recompute_shared_state(self) -> None:
        """Refresh shared-workstream state from history — monotonically.

        ``_known_senders`` unions the current trajectory's recorded senders
        (the ``meta.extra["sender"]`` stamped by :meth:`_append_user_turn`)
        with a one-time read of the full persisted history; it never shrinks,
        so a participant summarized out of the compacted slice stays known and
        cannot re-trigger the one-time join note. ``_shared_workstream`` flips
        True once any non-owner (``_mcp_user_id``) has spoken and then latches:
        reverting would misattribute a known-multi-user conversation AND flip
        the banner bytes, invalidating the provider prompt-prefix cache that
        the hour-rounded timestamp above exists to protect.

        Memoized per turn via ``_senders_dirty``: system-prompt composition
        runs many times within a turn (state transitions, MCP refresh, tool
        results) but the sender set only changes on user-turn append and
        history (re)load."""
        if not self._senders_dirty:
            return
        owner = (self._mcp_user_id or "").strip()
        senders = {
            s
            for t in self.messages
            if t.role is Role.USER and (s := (t.meta.extra.get("sender") or "").strip())
        }
        if not self._db_senders_loaded:
            senders |= self._load_persisted_senders()
        self._known_senders |= senders
        if not self._shared_workstream:
            self._shared_workstream = any(s != owner for s in self._known_senders)
        self._senders_dirty = False

    def _maybe_note_new_participant(self, sender_user_id: str | None) -> None:
        """Announce a first-time non-owner sender and flip the ws to shared.

        Called from :meth:`send` right after the user turn is appended, with the
        turn's acting user. The first non-owner sender recomposes the system
        prefix so the banner switches to multi-user framing; every first-time
        participant (2nd, 3rd, …) also gets a one-time ``participant_joined``
        system turn the model sees on that same turn — the "bob has joined the
        chat" signal, since we can't know a participant exists until they speak.
        The owner and repeat senders are no-ops."""
        owner = (self._mcp_user_id or "").strip()
        s = (sender_user_id or "").strip()
        if not s or s == owner or s in self._known_senders:
            return
        was_shared = self._shared_workstream
        # Set state directly (not just via _recompute_shared_state, which is
        # memoized and may no-op this call): the recompose below and the gate
        # above both need it now.  _recompute_shared_state later unions rather
        # than overwrites, so these survive.
        self._known_senders.add(s)
        self._shared_workstream = True
        if not was_shared:
            # First non-owner sender: recompose so the banner gains the shared
            # section (and the sender-label trust declaration).
            self._init_system_messages()
        name = self._resolve_display_name(s)
        self._append_system_turn(
            "participant_joined",
            f"{name} has joined this shared workstream. Their messages carry an "
            "authenticated sender-label naming them — attribute those messages to this "
            "sender, not the owner.",
        )

    def _inject_sender_labels(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fold per-message sender attribution into user turns for the wire.

        The context-layer half of per-user identity: on a genuinely multi-user
        (shared) workstream the model must be told *who* sent each turn, or it
        conflates every participant. Only user turns that recorded a real sender
        (the ``_sender`` side channel from :meth:`_append_user_turn`) are
        considered — synthetic turns (wake, compaction-resume, advisory) carry
        none and stay unlabeled.

        "Shared" is authoritative session state (``_shared_workstream`` — flipped
        once a non-owner speaks and re-derived when a worker rehydrates history).
        The per-slice sender count is only a fallback for when that state is still
        unset/uncomputed: keying off the count alone would drop labels whenever
        compaction narrows the retained wire slice to a single participant on a
        known-shared workstream, reintroducing the misattribution the banner tells
        the model these labels prevent. A single-user workstream returns the input
        unchanged (same object reference — the allocation-free common case
        ``_prepare_wire_messages`` relies on). The label rides the model-visible
        ``content``; the wire-invisible ``_sender`` key is stripped downstream by
        ``sanitize_messages``. ``self.messages`` is never mutated."""
        if not self._shared_workstream:
            senders = {
                s
                for m in messages
                if m.get("role") == "user" and (s := (m.get("_sender") or "").strip())
            }
            if len(senders) <= 1:
                return messages
        # Resolve each distinct sender's display name at most once per call, not
        # once per turn: _resolve_display_name does a blocking storage lookup
        # whose error path is deliberately uncached, so per-turn resolution
        # would re-hit storage for every user turn on every round-trip during an
        # outage.  A shared workstream has a handful of distinct senders.
        names: dict[str, str] = {}
        out: list[dict[str, Any]] = []
        for m in messages:
            sender = (m.get("_sender") or "").strip() if m.get("role") == "user" else ""
            if sender:
                name = names.get(sender)
                if name is None:
                    name = self._resolve_display_name(sender)
                    names[sender] = name
                nm = dict(m)
                nm["content"] = _prefix_sender_label(
                    m.get("content"), name, self._sender_label_nonce
                )
                out.append(nm)
            else:
                out.append(m)
        return out

    def _prepare_wire_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return a transient copy of *messages* prepared for the provider wire.

        Operator-context lives as first-class ``{"role": "system",
        "_source": ...}`` turns in the conversation trajectory (output-guard
        findings, user interjections, metacognitive nudges — see
        :func:`turnstone.core.tool_advisory.make_system_turn`).  This pass
        folds them for the wire via
        :func:`turnstone.core.lowering.fold_system_turns`: non-native
        models get each turn wrapped as a nonce-delimited
        ``[start system-reminder]`` block on the preceding turn; native
        mid-conversation-system models (claude-opus-4-8, claude-fable-5)
        keep them inline for the Anthropic converter to emit as real
        ``system`` messages.

        Messages without a foldable system turn pass through unchanged
        (same object reference) so the common case is allocation-free.
        ``self.messages`` is never mutated.

        After folding, empty-content user turns are dropped: the wake pipeline
        drives a synthetic empty ``send("")`` so any-channel nudges drain, and on
        the native path that user turn is left empty (the nudge stays inline) —
        an empty user message is invalid on every provider wire.  The drop runs
        *after* the fold so the fold-path wake turn, which the nudge folds into
        and thereby fills, is kept.

        Finally, :func:`turnstone.core.lowering.repair_wire_messages`
        synthesizes cancellation results for any orphaned client tool calls so
        the provider translator (the ``C`` layer) never sees an unanswered
        tool call — this is the sole send-time orphan repair; the translators
        carry none.  Identity-preserving when nothing is orphaned.
        """
        # The lowering passes (fold / drop / repair) are dict-native and the
        # provider translators consume the same dict projection, so the wire prep
        # threads the dicts ``_full_messages`` already produced straight through —
        # no Turn round-trip per send (``self.messages`` stays the canonical Turn
        # truth; only this transient wire copy is dict-native).
        # Per-user attribution first: label user turns by sender on shared
        # workstreams (no-op / same-ref on single-user) BEFORE folding so the
        # label is part of the content the fold + repair passes carry through.
        messages = self._inject_sender_labels(messages)
        folded = messages
        if self._provider is not None:
            folded = fold_system_turns(
                messages,
                supports_mid_conversation_system=(
                    self._get_capabilities().supports_mid_conversation_system
                ),
                nonce=self._envelope_nonce,
            )
        dropped = drop_empty_user_turns(folded)
        return repair_wire_messages(dropped)

    def _emit_state(self, state: str) -> None:
        """Notify UI of a workstream state transition.

        Also clears any persisted ``last_error`` row when the transition
        is a real recovery (``idle`` / ``running``) — a once-leaked
        exception body shouldn't outlive the failure that produced it,
        and the inspect/wait surface only displays ``last_error`` for
        ``state=='error'`` rows so a stale value would be invisible to
        the model but still queryable in storage forever.
        """
        if state in ("idle", "running") and self._has_persisted_error:
            from turnstone.core.memory import clear_last_error

            clear_last_error(self._ws_id)
            self._has_persisted_error = False
        self.ui.on_state_change(state)

    def _record_fatal_error(self, exc: BaseException) -> None:
        """Surface, sanitize, and persist a fatal exception, then emit state=error.

        Single chokepoint for the worker-thread fatal path: every
        ``except`` branch in :meth:`send` routes here so the
        sequence is fixed (sanitize → ``ui.on_error`` → persist →
        emit state=error) and the persist always lands BEFORE the
        synchronous state write in ``state_writer.record(flush_now=True)``.
        That ordering is what makes a coord polling at the moment of
        failure see ``state=error`` paired with a meaningful
        ``last_error``, not bare state=error with a missing config row.

        ``ui.on_error`` and the persist BOTH receive the sanitized
        text — a misconfigured ``OPENAI_BASE_URL`` of the form
        ``https://user:pass@host`` produces an httpx ``ConnectError``
        whose ``str()`` carries the credentials verbatim, and they'd
        otherwise land in (a) the dashboard via ``on_error`` and (b)
        the coord LLM's prompt via inspect/wait.

        Known backend boundary exceptions (httpx read/connect timeouts,
        OpenAI/Anthropic SDK ``APITimeoutError`` / ``APIConnectionError``
        / ``NotFoundError`` / ``AuthenticationError`` / ``RateLimitError``)
        get rewritten into operator-actionable text that includes the
        provider name, base URL, and model — the bare ``ReadTimeout:
        timed out`` shape produced by ``f"{type(exc).__name__}: {exc}"``
        leaves the user with no way to tell whether a model server hung,
        the URL is wrong, or the model isn't loaded.  Unknown exceptions
        fall through to the default formatting unchanged.
        """
        from turnstone.core.memory import persist_last_error, sanitize_error_text

        raw = self._format_backend_error(exc) or f"{type(exc).__name__}: {exc}"
        safe = sanitize_error_text(raw)
        try:
            self.ui.on_error(safe)
        except Exception:
            log.debug("session.on_error_dispatch_failed", exc_info=True)
        persist_last_error(self._ws_id, safe)
        self._has_persisted_error = True
        self._emit_state("error")

    def _format_backend_error(self, exc: BaseException) -> str | None:
        """Return an enriched message for known backend boundary errors.

        Returns ``None`` for exceptions outside the recognised set so the
        caller falls back to the bare ``f"{type(exc).__name__}: {exc}"``
        shape.  A context-window overflow is matched first by message *text* (it
        can arrive as several exception classes); everything else is matched by
        class name (see the ``_BACKEND_*_EXC_NAMES`` sets above) so the same
        helper covers
        httpx ``ReadTimeout`` / ``ConnectError``, OpenAI SDK
        ``APITimeoutError`` / ``APIConnectionError`` /
        ``NotFoundError`` / ``RateLimitError`` / ``AuthenticationError``,
        and the Anthropic SDK equivalents (which share names).

        Bad input (a ``base_url`` accessor that raises, a missing
        ``_provider``) silently degrades to a ``"?"`` placeholder rather
        than failing — this helper runs from the fatal-error path and
        must never itself raise.  The returned text still goes through
        :func:`sanitize_error_text` in the caller, so credentials in the
        base URL are redacted before display / persist.
        """
        # Backend identity shared by every branch — model label + raw tail.
        # Hoisted so the context-overflow branch and the class-name branches use
        # one derivation.  self.model/_model_alias are plain __init__ attributes
        # (always set), so reading them here can't raise on the fatal path.
        model_label = self.model or self._model_alias or "?"
        raw_msg = str(exc).strip()
        raw_tail = f" raw={raw_msg!r}" if raw_msg else ""

        name = type(exc).__name__

        # Context overflow — matched by text, because it arrives as BadRequestError
        # (OpenAI 400) OR InternalServerError (Anthropic-compat 500), which would
        # otherwise render as an opaque class name with no hint that the prompt was
        # simply too large.  _is_ctx_overflow self-gates on "not a known class", so a
        # recognized error (e.g. a RateLimitError whose quota text mentions a token
        # maximum) returns False here and falls through to its own specific message.
        if _is_ctx_overflow(exc):
            return (
                f"Context window exceeded for model={model_label}: the conversation "
                f"is too large to send. Auto-compaction should shrink it and retry; "
                f"seeing this means compaction could not reduce it enough.{raw_tail}"
            )

        if name not in _BACKEND_KNOWN_EXC_NAMES:
            return None

        # Pull backend identity — every branch swallows so a bad
        # accessor on a partially-initialised session can't hide the
        # original exception behind a NoneType error.
        base_url = "?"
        try:
            raw_url = str(
                getattr(self.client, "base_url", None)
                or getattr(self.client, "_base_url", None)
                or "?"
            )
            base_url = raw_url.split("?")[0].rstrip("/")
        except Exception:
            log.debug("session.fatal.base_url_lookup_failed", exc_info=True)
        provider_label = "?"
        try:
            prov = self._provider
            provider_label = (
                getattr(prov, "provider_name", None) or type(prov).__name__ if prov else "?"
            )
        except Exception:
            log.debug("session.fatal.provider_lookup_failed", exc_info=True)
        if name in _BACKEND_TIMEOUT_EXC_NAMES:
            return (
                f"Backend timeout ({name}): no response from {provider_label} "
                f"at {base_url} for model={model_label}. "
                f"The model server may be wedged — check it's accepting completion requests."
                f"{raw_tail}"
            )
        if name in _BACKEND_CONNECT_EXC_NAMES:
            return (
                f"Backend unreachable ({name}): cannot reach {provider_label} "
                f"at {base_url} for model={model_label}. "
                f"Check the URL, that the server is running, and that this host can reach it."
                f"{raw_tail}"
            )
        if name in _BACKEND_NOT_FOUND_EXC_NAMES:
            return (
                f"Backend reports model not loaded ({name}): {provider_label} "
                f"at {base_url} has no model named '{model_label}'. "
                f"Confirm the served model name matches the alias configuration "
                f"(GET /v1/models on the backend lists what it actually has)."
                f"{raw_tail}"
            )
        if name in _BACKEND_AUTH_EXC_NAMES:
            return (
                f"Backend rejected credentials ({name}): {provider_label} "
                f"at {base_url} (model={model_label}). "
                f"Check the API key configured for this model alias."
                f"{raw_tail}"
            )
        if name in _BACKEND_RATE_LIMIT_EXC_NAMES:
            return (
                f"Backend rate-limited ({name}): {provider_label} "
                f"at {base_url} (model={model_label})."
                f"{raw_tail}"
            )
        return None  # unreachable — `name` is in _BACKEND_KNOWN_EXC_NAMES by construction

    def _provider_extra_params(
        self,
        provider: LLMProvider | None = None,
        model_alias: str | None = None,
    ) -> dict[str, Any] | None:
        """Build provider-specific extra parameters.

        Forwards operator-supplied ``server_compat["extra_body"]`` overrides
        (``skip_special_tokens``, ``reasoning_format``, or explicit
        ``chat_template_kwargs``) to the OpenAI SDK ``extra_body`` on the
        OpenAI-shaped lanes, and to the Anthropic SDK ``extra_body`` on the
        anthropic-compatible lane (the channel for vLLM's
        ``chat_template_kwargs`` reasoning toggle).  Operators
        running gpt-oss-style local templates that consume ``reasoning_effort``
        from ``chat_template_kwargs`` should set it explicitly under
        ``server_compat["extra_body"]["chat_template_kwargs"]``.

        Thinking-mode params (``enable_thinking``, ``thinking``) are added
        separately by ``OpenAIChatCompletionsProvider._apply_thinking_mode``
        based on ``ModelCapabilities.thinking_mode`` — the Responses API
        surface handles reasoning natively and ignores ``extra_body``.

        *model_alias* selects which stored config supplies server compat
        settings.  When ``None``, defaults to the session's primary alias.
        """
        from turnstone.core.server_compat import merge_server_compat

        prov = provider or self._provider
        # extra_body consumers: the OpenAI-shaped providers, plus the
        # anthropic-compatible lane (server_compat extra_body rides the
        # Anthropic SDK's extra_body).  Real Anthropic and Google keep
        # their own param paths handled inside their providers.
        if prov.provider_name not in ("openai", "openai-compatible", "anthropic-compatible"):
            return None
        extra = merge_server_compat(None, self._get_server_compat(model_alias))
        return extra or None

    def _get_server_compat(self, model_alias: str | None = None) -> dict[str, Any]:
        """Get server compatibility settings from a model config.

        *model_alias* selects the config to read.  Falls back to the
        session's primary alias when ``None``.  The returned dict is the
        live ``ModelConfig.server_compat`` reference — callers must not
        mutate it.  ``merge_server_compat`` reads only.
        """
        alias = model_alias or self._model_alias
        if self._registry and alias:
            try:
                return self._registry.get_config(alias).server_compat
            except (ValueError, KeyError):
                pass
        return {}

    def _utility_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float | None = None,
        reasoning_effort: str = "low",
    ) -> CompletionResult:
        """Run a lightweight internal completion (title gen, compaction, extraction).

        Threads ``reasoning_effort`` through both the direct keyword (for
        commercial providers) and ``extra_params`` (for local model servers)
        so callers don't need to duplicate it.  ``max_tokens`` is clamped to
        the model's advertised output limit so small models don't error.

        ``temperature`` defaults to the session temperature (``self.temperature``)
        — the same operator/registry-resolved value the main turn uses — rather
        than a hard-coded constant: utility calls should not silently override an
        explicit ``[models.*]`` temperature.  The provider still drops it for
        models that forbid temperature (GPT-5 base, O-series) or pins it (Claude
        with thinking), so this only governs models that genuinely accept one.
        """
        caps = self._get_capabilities()
        clamped = min(max_tokens, caps.max_output_tokens) if caps.max_output_tokens else max_tokens
        messages = self._maybe_attach_vllm_chat_reasoning(messages, self._provider)
        result = self._provider.create_completion(
            client=self.client,
            model=self.model,
            messages=messages,
            max_tokens=clamped,
            temperature=self.temperature if temperature is None else temperature,
            reasoning_effort=reasoning_effort,
            extra_params=self._provider_extra_params(),
            capabilities=caps,
            replay_reasoning_to_model=self._resolve_replay_reasoning_to_model(caps=caps),
        )
        # Utility completions (title gen, compaction, web-fetch extraction)
        # bypass the streaming on_status path — record their usage so the
        # governance dashboard reflects this spend.
        self._record_aux_usage(result)
        return result

    def _record_aux_usage(self, result: CompletionResult, *, model: str | None = None) -> None:
        """Persist token usage for a non-streaming auxiliary completion.

        Title generation, compaction, web-fetch summarisation, and
        task sub-agents all run via ``create_completion`` and bypass
        the streaming ``on_status`` accounting path; without this their
        spend never reaches the usage dashboard. Delegates to the UI's
        ``on_aux_usage`` hook (which owns the storage write + any node
        metrics), mirroring how ``_print_status_line`` routes main-loop
        usage through ``on_status``.

        ``model`` defaults to the session model (utility calls share it);
        sub-agent callers pass the agent's own model so per-model
        attribution stays accurate. The hook is looked up defensively —
        minimal UI stubs (some tests, replay shims) predate it and should
        skip recording rather than crash a title-gen or sub-agent turn.
        """
        u = result.usage
        if u is None:
            return
        record = getattr(self.ui, "on_aux_usage", None)
        if record is None:
            return
        record(
            {
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "cache_creation_tokens": u.cache_creation_tokens,
                "cache_read_tokens": u.cache_read_tokens,
                "model": model or self.model,
            }
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
        (SearxNG or MCP — see ``_resolve_search_client``).

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

        # Gate MCP tools: only include when relevant MCP servers are
        # connected. Per-user variants (scope decision 0.2) keep the
        # tool visible for a pool-only user even when the static catalog
        # is empty.
        if not self._mcp_client or not self._mcp_client.resource_count_for_user(
            self._mcp_effective_user_id
        ):
            tools = _without_tool(tools, "read_resource")
        if not self._mcp_client or not self._mcp_client.prompt_count_for_user(
            self._mcp_effective_user_id
        ):
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

    # Chunked-compaction tuning (see _summarize_blocks / _summary_input_budget_chars).
    _SUMMARY_SAFETY_MARGIN = 0.05  # fraction of context_window held back
    _SUMMARY_BUDGET_FRACTION = 0.75  # derate for the uncalibrated _chars_per_token
    _MAX_SUMMARY_DEPTH = 5  # recursion ceiling before bailing
    _MIN_SUMMARY_BUDGET_CHARS = 2000  # floor so a tiny window still makes progress
    _MIN_SUMMARY_OUTPUT_TOKENS = 512  # floor on summary output even on a tiny window
    _MIN_CARRY_BUDGET_CHARS = 2000  # floor on verbatim carry (ask quote / wind-down spill)

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

    def _stop_retrying(self, exc: BaseException, attempt: int, provider: LLMProvider) -> bool:
        """Terminal-retry predicate shared by every API retry loop (stream, summary,
        task_agent): stop on a non-retryable error class, a deterministic
        context-overflow (retrying an identical oversized payload is pointless), or
        retry exhaustion."""
        return (
            type(exc).__name__ not in provider.retryable_error_names
            or _is_ctx_overflow(exc)
            or attempt == self._MAX_RETRIES
        )

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
        # Resolve once outside the retry loop — caps don't change per
        # attempt, and the resolver below threads them into the
        # ``replay_reasoning_to_model`` AND-gate.
        resolved_caps = capabilities or self._get_capabilities(prov, model)
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
        msgs = self._maybe_attach_vllm_chat_reasoning(msgs, prov, model_alias)
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
                    capabilities=resolved_caps,
                    replay_reasoning_to_model=self._resolve_replay_reasoning_to_model(
                        model_alias, caps=resolved_caps
                    ),
                    resolve_attachments=lambda ids: self._resolve_attachments(ids, resolved_caps),
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
                if self._stop_retrying(e, attempt, prov):
                    # Non-retryable class, deterministic overflow (the send-loop
                    # compact-and-retry handles it), or retries exhausted — raise
                    # immediately rather than burn backoff sleeps.
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
        *,
        from_wake: bool = False,
        source: str | None = None,
    ) -> int:
        """Append a user turn (plain or multipart) and persist it.

        When ``attachments`` is non-empty the in-memory message carries
        list content (text + image_url + document parts); the DB
        conversations row stores only the text.  At commit each attachment's
        bytes are written content-addressed + reference-counted into
        ``workstream_attachments`` (``attachment_id`` = the content hash) and
        the ordered id-list is recorded on the row's ``attachments`` ref-list
        column — the sole message->blob link.  Returns the saved conversations
        row id (0 on save failure, per the storage wrapper's no-raise
        contract).

        ``send_id`` (when provided) is the end-to-end send token; it no longer
        gates a DB reservation (the upload buffer is the pending store — the
        bytes were already drained from it before this call).
        """
        # New user content invalidates the per-turn memory-search cache
        # (composition will see a different recent-context string).
        self._invalidate_memory_cache()
        user_content: str | list[dict[str, Any]]
        if attachments:
            # Attachments ride by reference (``{type: kind, attachment_id}`` →
            # AttachmentRef); the bytes — already committed content-addressed
            # below — materialize at each output (wire / display), where the
            # bad-UTF-8 / unrenderable handling now lives.
            parts: list[dict[str, Any]] = [{"type": "text", "text": user_input}]
            for att in attachments:
                if att.is_image:
                    parts.append({"type": "image", "attachment_id": att.attachment_id})
                elif att.is_text:
                    parts.append({"type": "document", "attachment_id": att.attachment_id})
                elif att.is_pdf:
                    parts.append({"type": "pdf", "attachment_id": att.attachment_id})
                elif att.is_audio:
                    parts.append({"type": "audio", "attachment_id": att.attachment_id})
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
        if from_wake and self._wake_source_tag:
            # Sibling tag for audit / replay: the synthetic empty user
            # message emitted by ``deliver_wake_nudge_from_queue`` is
            # marked so UI can render it distinctly (or hide it) and
            # log consumers can tell self-prompted turns from real
            # user input.  Stripped at the sanitize boundary by the
            # leading-underscore filter.  ``from_wake`` is required
            # explicitly because :meth:`_flush_queued_messages` also
            # calls this method during a wake's chat loop — those
            # messages are real user input that happens to be
            # delivered while a wake is in flight, NOT synthetic, so
            # they must not inherit the wake tag.
            user_msg["_source"] = self._wake_source_tag
        elif source:
            # Provenance for other self-prompted turns (e.g. the post-compaction
            # auto-resume): marks the turn for audit / replay / UI so it isn't
            # mistaken for real user input.  Stripped at the sanitize boundary.
            user_msg["_source"] = source
        # Per-message sender identity for shared-workstream attribution.
        # Stamp genuine user turns with the ACTING user — the turn initiator
        # bound by ``bind_acting_user`` (owner fallback for CLI/eval/internal).
        # Synthetic turns (wake, compaction-resume, advisory) carry a ``_source``
        # / ``from_wake`` and stay unstamped so they never get a speaker label.
        # Rides the wire-invisible ``_sender`` side channel and, below, the
        # persisted ``meta`` column so history replay re-attributes correctly.
        sender = "" if (from_wake or source) else (self._mcp_effective_user_id or "").strip()
        if sender:
            user_msg["_sender"] = sender
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
                    # Doc-budget proxy mirrored from the reconstruct path so the
                    # live and reloaded shapes count identically (``_msg_text_chars``).
                    "size_bytes": len(a.content),
                }
                for a in attachments
            ]
        self.messages.append(turn_from_dict(user_msg))
        if sender:
            # A newly recorded sender can change shared-workstream state; let
            # the next system-prompt compose re-derive it (memoized otherwise).
            self._invalidate_shared_state()
        self._msg_tokens.append(max(1, int(self._msg_char_count(user_msg) / self._chars_per_token)))
        # DB row stores the raw text only; attachment bytes are written
        # content-addressed into workstream_attachments and the ordered id-list
        # is recorded on this row's ``attachments`` ref-list column (the sole
        # message->blob link), joined back in on load.  ``send_id`` no longer
        # gates a reservation — the bytes were drained from the upload buffer
        # before this call.
        #
        # The wake's synthesised empty turn carries ``_source`` onto the
        # row so reconnecting tabs render the marker instead of an
        # unanchored assistant reply.  Drained user-channel nudges no
        # longer ride this row — the caller appends them as first-class
        # ``system`` turns AFTER this user turn (uniform attach rule).
        source = user_msg.get("_source")
        # Persist the sender in the row's ``meta`` JSON (no schema change — the
        # column already carries opaque per-row metadata). ``reconstruct_turns``
        # restores it to ``Turn.meta.extra["sender"]`` so a worker rehydrating a
        # shared workstream re-attributes each user turn.
        meta_json = json.dumps({"sender": sender}) if sender else None
        message_id = save_message(
            self._ws_id,
            "user",
            user_input,
            source=source if isinstance(source, str) and source else None,
            event_id=self._ui_event_id(),
            meta=meta_json,
        )
        if attachments and message_id:
            self._persist_attachment_refs(message_id, attachments)
            # Drain the now-committed handles from the per-node upload buffer
            # (content-addressed: the bytes are persisted + referenced).  A
            # peek-then-commit split (resolve in the route, drain here) lets an
            # uncommitted send — e.g. one the queue rejected — keep the staged
            # bytes for a retry; anything not drained expires on the buffer TTL.
            buffer = get_attachment_buffer()
            for att in attachments:
                buffer.discard(att.attachment_id, ws_id=self._ws_id, user_id=self._user_id)
        return message_id

    def _persist_attachment_refs(
        self,
        message_id: int,
        attachments: list[Attachment] | tuple[Attachment, ...],
        *,
        origin: str = "upload",
    ) -> None:
        """Write each attachment's bytes content-addressed and record the ref-list.

        ``attachment_id`` is the content hash, so identical bytes dedupe to one
        blob and each reference bumps its refcount; the ordered id-list is
        recorded on the conversations row's ``attachments`` column.  Used by
        the user-turn commit (``origin='upload'``) and the tool-image persist
        (``origin='tool'``).
        """
        ref_ids: list[str] = []
        for att in attachments:
            save_attachment(
                att.attachment_id,
                att.filename,
                att.mime_type,
                len(att.content),
                att.kind,
                att.content,
                origin,
            )
            ref_ids.append(att.attachment_id)
        set_message_attachments(self._ws_id, message_id, ref_ids)

    @staticmethod
    def _decode_image_part(part: Any, tool_name: str) -> Attachment | None:
        """Decode one ``image_url`` data-URI content part into an Attachment.

        Returns ``None`` for non-image / non-data-URI / undecodable parts.  The
        ``attachment_id`` is the content hash, so persisting is idempotent and
        the same bytes dedupe across turns/conversations.
        """
        if not (isinstance(part, dict) and part.get("type") == "image_url"):
            return None
        url = (part.get("image_url") or {}).get("url") or ""
        if not url.startswith("data:") or ";base64," not in url:
            return None
        header, _, b64 = url.partition(";base64,")
        mime = header[len("data:") :] or "image/png"
        try:
            # ``binascii.Error`` (malformed payload) subclasses ``ValueError``.
            raw = base64.b64decode(b64, validate=True)
        except ValueError:
            log.warning("tool %s emitted an undecodable image data URI; not persisting", tool_name)
            return None
        ext = (mimetypes.guess_extension(mime) or ".png").lstrip(".")
        return Attachment(
            attachment_id=hashlib.sha256(raw).hexdigest(),
            filename=f"{tool_name or 'tool'}-image.{ext}",
            mime_type=mime,
            kind="image",
            content=raw,
        )

    @classmethod
    def _tool_content_by_reference(
        cls, output: Any, tool_name: str
    ) -> tuple[Any, list[Attachment]]:
        """Build the tool turn's content with image parts BY REFERENCE.

        For list output (vision tool results), each decodable ``image_url`` part
        becomes a ``{type: "image", attachment_id}`` placeholder and its bytes
        are returned for content-addressed persistence; text parts pass through;
        an undecodable image part is dropped (it was never persistable, and a
        by-reference trajectory carries no inline bytes).  Non-list output is
        returned unchanged with no attachments.
        """
        if not isinstance(output, list):
            return output, []
        content: list[Any] = []
        atts: list[Attachment] = []
        for part in output:
            att = cls._decode_image_part(part, tool_name)
            if att is not None:
                content.append({"type": "image", "attachment_id": att.attachment_id})
                atts.append(att)
            elif isinstance(part, dict) and part.get("type") == "image_url":
                continue  # undecodable image — unpersistable, drop from the by-ref turn
            else:
                content.append(part)
        return content, atts

    def _append_system_turn(self, source: str, content: str, **meta: Any) -> None:
        """Append a first-class operator-context system turn and persist it.

        Operator context (output-guard findings, user interjections,
        metacognitive nudges) lives in the conversation trajectory as a
        real ``{"role": "system", "_source": <source>, ...}`` turn (see
        :func:`turnstone.core.tool_advisory.make_system_turn`) rather than
        spliced into a neighbouring turn's ``content``.  Uniform attach
        rule: a system turn FOLLOWS the turn it relates to — callers append
        it after the user / tool / assistant turn it advises.

        Mirrors :meth:`_append_user_turn`'s bookkeeping: pushes a
        ``_msg_tokens`` estimate (parallel to ``self.messages``), fires the
        live ``on_system_turn`` SSE hook so multi-tab mirrors render it in
        lockstep, then persists the row via
        ``save_message(ws, "system", content, source=source)`` so
        reconnecting tabs replay the same bubble.  The hook runs *before* the
        persist so the row can be stamped with its own SSE event id (see the
        inline note for why the ordering matters).  Hook failures are logged
        and swallowed — the in-memory append + persist are the load-bearing
        ops; a UI implementation throwing here must not abort the turn.

        *source* must be one of :data:`tool_advisory.SYSTEM_TURN_SOURCES`;
        extra *meta* is the turn's structured per-kind data (e.g.
        ``watch_triggered``'s ``watch_name`` / command / poll counters).  It
        rides three boundaries in lockstep: the in-memory ``Turn`` (as
        ``meta.extra["source_meta"]`` via :func:`make_system_turn` →
        ``turn_from_dict``), the persisted ``conversations.meta`` column (JSON),
        and the live ``on_system_turn`` SSE hook — so a reconnecting tab and a
        live mirror both rebuild the same per-kind bubble.  It is stripped before
        the LLM wire (``_source_meta`` is a ``_``-prefixed key).
        """
        turn = make_system_turn(source, content, **meta)
        self.messages.append(turn_from_dict(turn))
        self._msg_tokens.append(max(1, int(self._msg_char_count(turn) / self._chars_per_token)))
        meta_json = json.dumps(meta) if meta else None
        # Fire the live SSE hook BEFORE persisting so the row carries the SAME
        # event_id its ``system_turn`` event carries.  ``on_system_turn``
        # returns the id ``_enqueue`` assigned (``None`` for non-SSE UIs / test
        # doubles).  The hook stays best-effort: on failure the persist below
        # still runs and the row falls back to the current cursor (no live
        # event was delivered to double anyway).
        emitted_event_id: int | None = None
        try:
            emitted_event_id = self.ui.on_system_turn(content, source, meta or None)
        except Exception:
            log.warning("ui.on_system_turn failed; system turn still appended", exc_info=True)
        save_message(
            self._ws_id,
            "system",
            content,
            source=source,
            event_id=emitted_event_id if isinstance(emitted_event_id, int) else self._ui_event_id(),
            meta=meta_json,
        )

    # -- Main generation loop ------------------------------------------------

    @property
    def _mcp_effective_user_id(self) -> str | None:
        """Identity for per-user MCP (oauth_user) credential resolution.

        The acting user — the authenticated principal who last initiated
        a turn on this session — when one is bound; otherwise the session
        owner. On a shared workstream this makes MCP tool calls run under
        the credentials (and catalog) of whoever is actually driving,
        rather than whoever created the workstream. Falls back to the
        owner for CLI / eval / scheduled / internal turns, preserving the
        pre-existing single-user behaviour.
        """
        return self._acting_user_id or self._mcp_user_id

    def _history_scope_user_id(self) -> str | None:
        """Identity that scopes conversation-history reads (recall tool,
        ``/history``).

        The acting user when one is bound — on a shared workstream the
        search runs with the visibility of whoever is driving the turn —
        otherwise the session owner.  ``None`` (CLI / eval / internal
        single-user lanes) leaves history unscoped.  Deliberately no
        admin/service bypass: the model-facing recall tool always reads as
        a plain user, even when an admin is driving — bypass is a surface
        property (cluster inspect), not a principal property.
        """
        return self._acting_user_id or self._user_id or None

    def bind_acting_user(self, user_id: str) -> None:
        """Bind the authenticated initiator of the current turn.

        Called from the HTTP send path with the caller's authenticated
        user id. No-ops when ``user_id`` is empty (unauthenticated lanes
        keep the owner fallback) or unchanged. On a genuine change this
        re-scopes the session's MCP view to the new acting user:

        - swaps the user-scoped tool/resource/prompt listeners so pool
          catalog changes for the acting user reach this session
          (listener identity is the ``(user_id, callback)`` pair);
        - fire-and-forget primes the acting user's oauth_user pools so
          their tools surface without a manual reconnect;
        - rebuilds the merged tool list and catalog-dependent state via
          the same callbacks a pool notification would fire.

        The binding is sticky — it persists until the next authenticated
        send — so wake nudges and auto-resume continuations keep running
        under the user whose turn they continue. It intentionally does
        NOT rebind mid-turn: queued interjections fold into the current
        turn under the initiator's identity, and prepared tool items pin
        the identity at prepare time (see ``_prepare_mcp_tool``).
        """
        if not user_id or user_id == (self._acting_user_id or self._user_id):
            self._acting_user_id = self._acting_user_id or user_id
            return
        self._acting_user_id = user_id
        mcp = self._mcp_client
        if not mcp or self._kind == WorkstreamKind.COORDINATOR:
            return
        old_listener_uid = self._mcp_listener_user_id
        new_listener_uid: str | None = self._mcp_effective_user_id
        if new_listener_uid != old_listener_uid:
            if self._mcp_refresh_cb:
                mcp.remove_listener(self._mcp_refresh_cb, user_id=old_listener_uid)
                mcp.add_listener(self._mcp_refresh_cb, user_id=new_listener_uid)
            if self._mcp_resource_cb:
                mcp.remove_resource_listener(self._mcp_resource_cb, user_id=old_listener_uid)
                mcp.add_resource_listener(self._mcp_resource_cb, user_id=new_listener_uid)
            if self._mcp_prompt_cb:
                mcp.remove_prompt_listener(self._mcp_prompt_cb, user_id=old_listener_uid)
                mcp.add_prompt_listener(self._mcp_prompt_cb, user_id=new_listener_uid)
            self._mcp_listener_user_id = new_listener_uid
        if new_listener_uid and hasattr(mcp, "prime_user_pools"):
            try:
                mcp.prime_user_pools(new_listener_uid)
            except Exception:
                log.debug(
                    "mcp prime_user_pools scheduling failed user=%s",
                    new_listener_uid,
                    exc_info=True,
                )
        # Rebuild the merged tool list and resource/prompt-dependent
        # state under the new identity NOW — the prime above completes
        # asynchronously and only notifies on catalog changes, while
        # already-warm pool entries for this user produce no
        # notification at all.
        self._on_mcp_tools_changed()
        self._on_mcp_resources_changed()
        self._on_mcp_prompts_changed()

    def send(
        self,
        user_input: str,
        attachments: list[Attachment] | None = None,
        send_id: str | None = None,
        *,
        from_wake: bool = False,
        acting_user_id: str | None = None,
    ) -> None:
        """Send user input and handle the response loop (including tool calls).

        When ``attachments`` is provided the in-memory user message carries
        multipart list content (text + image_url + document parts) while the
        DB conversations row stores only the text — the attachment bytes are
        written content-addressed into ``workstream_attachments`` and the
        ordered id-list is recorded on the row's ``attachments`` ref-list
        column.

        ``send_id`` is an end-to-end tracking token only; it no longer gates a
        DB reservation (the upload buffer is the pending store, and the bytes
        in ``attachments`` were already drained/peeked from it by the caller).

        ``acting_user_id`` is the authenticated caller who initiated this
        turn (HTTP send / retry paths). It rebinds per-user MCP credential
        resolution to that user for this and subsequent turns — see
        :meth:`bind_acting_user`. ``None`` (internal callers: wake nudges,
        auto-resume, CLI) leaves the current binding untouched.
        """
        if acting_user_id is not None:
            self.bind_acting_user(acting_user_id)
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
        # Per-send cooperative-compaction latch: each send starts a fresh
        # advise→compact cycle, so reset here.  This single chokepoint covers
        # the cancel / error / superseded / resume / clear / new exits that
        # would otherwise leave the latch set on the long-lived session and
        # trip a premature, advisory-skipping compaction on the next send.
        self._compaction_advised = False
        self._generation += 1
        my_generation = self._generation
        # Fresh cancel event per generation.  The old event object stays
        # set for any abandoned thread — _exec_bash captures a local
        # reference so subprocesses from old generations are still killed.
        self._cancel_event = threading.Event()
        self._cancelled_partial_msg = None
        # Fresh per-send attachment wire-part memo (see __init__): bounds the
        # heavy rasterized-page parts to one send and picks up any mid-session
        # capability / config change.
        self._wire_part_cache = {}

        # Metacognitive nudge: check for correction/completion signals
        # before _append_user_turn so any fired nudge (plus any nudges
        # queued earlier — e.g. denial during the previous tool batch,
        # resume on rehydrate) is drained right after the user turn.
        nudge = self._check_metacognitive_nudge(user_input)
        if nudge:
            self._queue_user_advisory(*nudge)

        self._append_user_turn(user_input, attachments or (), send_id=send_id, from_wake=from_wake)
        # Context-identity: if a new (non-owner) participant just spoke, flip the
        # workstream to shared framing (banner recompose) and drop a one-time
        # "has joined" note so the model learns a second human exists — it can't
        # know until they send a message. Sourced from the acting user bound
        # above (empty/owner for CLI/eval/internal turns → no-op).
        if not from_wake:
            self._maybe_note_new_participant(self._mcp_effective_user_id)
        # Drained user-channel nudges become first-class ``system`` turns
        # appended AFTER the user turn (uniform attach rule), replacing the
        # legacy per-message ``_reminders`` side-channel splice.
        self._emit_pending_user_nudges()

        # Auto-title from the opening user message — fire NOW rather than
        # waiting for the assistant's final tool-call-free turn.  The old
        # trigger sat in the ``not tool_calls`` branch of the loop below;
        # coordinators spend nearly every turn in tool calls and may never
        # reach that terminal text turn, so the title almost never
        # generated for them.  Gate on a real user message: synthetic wake
        # sends carry no content and ``_generate_title`` would no-op on the
        # empty/attachment-only case anyway (it needs first-user-message
        # text).  Concurrency: this background thread runs alongside the
        # streaming turn started below, but safely — it snapshots
        # ``self.messages`` for iteration, and the only UI it touches is
        # ``on_aux_usage`` (storage/metrics, no ``_ws_lock`` state) and
        # ``on_rename`` (queue/locked fan-out), both documented
        # auxiliary-thread-safe on ``SessionUIBase``; the provider + client
        # handle concurrent requests (the same path ``task_agent`` uses).
        if not self._title_generated and user_input.strip() and not from_wake:
            self._title_generated = True
            threading.Thread(target=self._generate_title, daemon=True).start()

        # A fresh session composed its system prefix at __init__ with an empty
        # history, so memory selection fell back to recency (no query, no rerank).
        # Recompose once the first real user message exists so the opening turn
        # gets a query-relevant memory set. Gate on a non-empty query (not just
        # the flag) so synthetic wake sends -- which carry no user content and
        # leave the flag False -- don't re-pay the compose every wake; the flag
        # flips True inside the recompose, so this fires once and the prefix
        # stays cache-stable after. Per-turn refresh is the larger redesign on
        # another branch.
        if (
            not self._system_composed_with_context
            and extract_recent_context(dicts_from_turns(self.messages)).strip()
        ):
            self._init_system_messages()

        try:
            # Bail an orphaned/superseded send BEFORE the pre-send compaction below
            # can mutate history.  The old code's first in-try act was the loop-top
            # _check_cancelled(my_generation); the new pre-send layer sits ahead of
            # the loop, so without this guard a stale thread (a newer send already
            # bumped _generation and installed a fresh, clear cancel event) would
            # sail past the event-only checks inside compaction and compact-and-swap
            # the live generation's history.  A stale thread raises here and the
            # except-GenerationCancelled handler returns without touching state.
            self._check_cancelled(my_generation)

            # Proactive pre-send compaction (Layer A): a rehydrated resume — or a
            # session that never compacted under a larger-window model before a
            # switch to a smaller one — can arrive already over the window before
            # the FIRST send.  The mid-turn and end-of-turn triggers only fire AFTER
            # a successful call, so without this the first post-resume send goes out
            # blind.  Gate on the HARD ceiling alone (NOT _compaction_owed, whose
            # advised-soft arm is the cooperative wind-down the model must still get
            # to act on), run it ONCE here, and preserve from the last USER turn to
            # the end — the just-sent message plus any nudge _emit_pending_user_nudges
            # appended after it — so it isn't summarized out from under its own reply.
            # Sits INSIDE the try so a raise (or a cooperative cancel) lands in the
            # fatal/cancel handlers like every other compaction site.  Passes
            # my_generation so the swap inside compaction also aborts if a newer send
            # starts DURING the (slow) summary call.  Best-effort prevention, not a
            # guarantee: the char-based estimate under-counts dense/CJK history on an
            # uncalibrated resume, so the send-loop compact-and-retry below stays the
            # backstop.
            if self._over_hard(self._estimated_prompt_tokens()):
                boundaries = self._find_turn_boundaries()
                preserve = len(self.messages) - boundaries[-1] if boundaries else 1
                self._do_auto_compact(
                    "pre-send", preserve_tail=preserve, my_generation=my_generation
                )
                if self._generation != my_generation:
                    return
            while True:
                self._check_cancelled(my_generation)
                msgs = self._prepare_wire_messages(self._full_messages())

                if self.debug:
                    self._debug_print_request(msgs)

                # Reset the per-turn inflight buffers BEFORE entering
                # the streaming phase so the SSE refresh-resume snapshot
                # only ever represents the CURRENT in-progress turn —
                # not prior already-committed turns within this send
                # loop. Distinct from on_thinking_start (which can fire
                # twice within a single iteration on compact-retry).
                self.ui.on_turn_start()
                self._emit_state("thinking")
                self.ui.on_thinking_start()
                try:
                    try:
                        stream = self._create_stream_with_retry(msgs)
                    except Exception as ctx_err:
                        # Context overflow recovery: if the API rejects the
                        # request due to exceeding the context window, compact
                        # the conversation and retry once.
                        if not _is_ctx_overflow(ctx_err):
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
                            # my_generation: without it a stale send that hits
                            # overflow here could compact-and-swap the LIVE
                            # generation's history after a force-cancel started
                            # a newer one — the same race every other compaction
                            # site already guards.
                            self._compact_messages(auto=True, my_generation=my_generation)
                            msgs = self._prepare_wire_messages(self._full_messages())
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

                # Reuse the wire-bound ``msgs`` we already built for the
                # stream call instead of re-folding the system turns
                # (perf-2); passing the already-prepared list keeps the
                # calibration char count aligned with what the provider
                # actually counted.
                self._update_token_table(assistant_msg, msgs=msgs)
                self._print_status_line()  # Report usage for EVERY API call
                self.messages.append(turn_from_dict(assistant_msg))
                # Clear per-turn inflight buffers — the assistant
                # message is now in the history list a refresh would
                # replay, so the in_progress_snapshot shouldn't re-
                # render the same text during the next tool-execution
                # window or the next streaming turn.
                self.ui.on_turn_committed()
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

                tool_calls_json: str | None = json.dumps(tc) if tc else None

                # Save assistant message atomically (content + tool_calls in one row)
                if content or provider_data is not None or tool_calls_json:
                    save_message(
                        self._ws_id,
                        "assistant",
                        content,
                        provider_data=provider_data,
                        tool_calls=tool_calls_json,
                        event_id=self._ui_event_id(),
                        producer=self._provider.provider_name if self._provider else None,
                    )

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    # Did the model stop because we asked it to wind down for a
                    # compaction (cooperative), or because the task is actually
                    # done?  Capture before the reset — it gates the auto-resume.
                    stopped_to_compact = self._compaction_advised
                    self._compaction_advised = False
                    # A concurrent force-cancel may have started a new generation
                    # while this turn was finishing; don't run end-of-turn
                    # compaction or persist a resume turn under it (the mid-turn
                    # and end-of-loop compaction sites guard the same race).
                    if self._generation != my_generation:
                        return
                    # Auto-compact when the context exceeds the threshold, so the
                    # next turn starts with headroom.  Bare-soft check (NOT
                    # _compaction_owed()): the turn already ended, so there's no
                    # model cooperation to wait for and the latch was just
                    # consumed above — compact whenever over soft.  None-safe via
                    # _estimated_prompt_tokens(), so no _last_usage guard.
                    if self._over_soft(self._estimated_prompt_tokens()):
                        # carry_spill: when the model stopped to let us compact,
                        # its final turn is the plan spill the advisory asked
                        # for — copy it across verbatim, don't just paraphrase.
                        compacted = self._do_auto_compact(
                            my_generation=my_generation, carry_spill=stopped_to_compact
                        )
                        # _do_auto_compact's summary call is slow; unlike the
                        # mid-turn siblings this site also PERSISTS the resume
                        # turn, so re-check the generation didn't change during
                        # the call before writing it into history.
                        if stopped_to_compact and compacted and self._generation == my_generation:
                            # The model paused mid-task to let us compact, not
                            # because it was finished.  Hand the compacted state
                            # back as a user turn so it resumes instead of being
                            # stranded at idle — but only when a summary was
                            # actually produced (else there's nothing to continue
                            # from).  The prompt lets a genuinely-finished model
                            # give its final answer and stop.
                            self._append_user_turn(
                                NUDGE_COMPACTION_RESUME,
                                (),
                                source="compaction_resume",
                            )
                            continue
                    # Flush any queued messages that weren't injected
                    # (no tool calls → no advisory seam to inject at).
                    # If anything drained, the model hasn't seen those
                    # messages yet — keep the loop alive so it gets a
                    # turn over the extended history rather than
                    # orphaning them until the next user send.
                    if self._flush_queued_messages():
                        continue
                    self._emit_state("idle")
                    break

                # Execute tool calls (potentially in parallel)
                self._emit_state("running")
                results, user_feedback = self._execute_tools(tool_calls)

                # Bail if generation was superseded during tool execution.
                if self._generation != my_generation:
                    return

                # Repeat-detection + tool-error nudge.  Mutates *results*
                # in place to inject inline warning text on identical
                # repeats; queues advisories for the next drain pass.
                self._apply_post_execute_advisories(tool_calls, results)

                # Map tool_call_id → tool name for logging
                _tc_names = {c["id"]: c.get("function", {}).get("name", "") for c in tool_calls}
                # Tool arguments (JSON string) per call_id — threaded into the
                # LLM judge so it can reason about output-vs-request plausibility.
                _tc_args = {c["id"]: c.get("function", {}).get("arguments", "") for c in tool_calls}
                _last_idx = len(results) - 1

                # Pre-truncate (cp-2): the LLM judge stage must see the
                # same text that lands in the assistant's context, not the
                # full pre-truncation blob.  Otherwise a fat web_fetch can
                # OOM the judge model or burn tokens on content that won't
                # even reach the assistant.  Truncation is a safety
                # invariant that runs regardless of the guard stage.
                #
                # The remaining-token budget shrinks as each output is sized;
                # without per-output bookkeeping, N parallel tool results
                # could each claim the full remaining budget and collectively
                # overflow the prompt.
                # Compact-before-truncate: if a compaction is already owed (over
                # the hard ceiling, or the model worked past a wrap-up advisory),
                # do it BEFORE sizing the truncation budget — preserving the
                # in-flight assistant tool-call turn (preserve_tail=1) so the
                # results about to be appended aren't orphaned from their
                # tool_use.  The freed context then lets the fresh results
                # through (largely) untruncated instead of snipping them only to
                # summarise them moments later.  Generation-guarded so an
                # orphaned thread can't replace history under the active one.
                pre_attempted_compact = False
                if self._generation == my_generation and self._compaction_owed():
                    self._do_auto_compact("mid-turn", preserve_tail=1, my_generation=my_generation)
                    pre_attempted_compact = True
                truncation_budget = self._remaining_token_budget()
                _truncated: dict[str, str] = {}
                for tc_id, output in results:
                    if isinstance(output, str):
                        truncated = self._truncate_output(
                            output, remaining_budget_tokens=truncation_budget
                        )
                        _truncated[tc_id] = truncated
                        truncation_budget = max(
                            0,
                            truncation_budget - int(len(truncated) / self._chars_per_token),
                        )
                results = [(tc_id, _truncated.get(tc_id, output)) for tc_id, output in results]

                # Pre-evaluate the guard stage concurrently when LLM is
                # enabled and there are multiple string outputs (perf-2).
                # The judge stage is the dominant per-turn latency at
                # 5-20 tool calls; running them in parallel collapses
                # N×LLM-latency to ⌈N/max_workers⌉×latency.  Limited to
                # str outputs — structured (list) outputs stay sequential
                # (per-part recursion below).  Single-result turns also
                # skip the parallel path since there's no parallelism to
                # gain and the overhead isn't worth it.
                _batch_guard: dict[str, tuple[str, OutputAssessment | None]] = {}
                _judge_cfg = self._judge_cfg
                if (
                    _judge_cfg
                    and _judge_cfg.output_guard
                    and _judge_cfg.output_guard_llm
                    and sum(1 for _, o in results if isinstance(o, str)) > 1
                ):
                    _batch_guard = self._batch_evaluate_outputs(
                        [
                            (tc_id, o, _tc_names.get(tc_id, ""), _tc_args.get(tc_id, ""))
                            for tc_id, o in results
                            if isinstance(o, str)
                        ]
                    )

                # Operator-context system turns are accumulated across the
                # per-result loop and emitted AFTER the whole tool batch (see
                # the flush below).  This keeps every system turn after the
                # COMPLETE tool block — the placement the Anthropic native
                # mid-conversation-system path requires (a system turn must
                # never land between a ``tool_use`` and its ``tool_result``,
                # and the converter packs the batch's tool results into one
                # user turn).
                pending_system_turns: list[tuple[str, str, dict[str, Any]]] = []

                for _ri, (tc_id, output) in enumerate(results):
                    # Output guard: evaluate tool result before it enters context
                    assessment: OutputAssessment | None = None
                    if self._judge_cfg and self._judge_cfg.output_guard:
                        if isinstance(output, str):
                            if tc_id in _batch_guard:
                                output, assessment = _batch_guard[tc_id]
                            else:
                                output, assessment = self._evaluate_output(
                                    tc_id,
                                    output,
                                    _tc_names.get(tc_id, ""),
                                    tool_args=_tc_args.get(tc_id, ""),
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
                                        tc_id,
                                        p["text"],
                                        _tc_names.get(tc_id, ""),
                                        tool_args=_tc_args.get(tc_id, ""),
                                    )
                                    if _part_assess is not None:
                                        assessment = _part_assess

                    # Operator context for this result: output-guard
                    # findings + queued user messages (Seam 1), plus
                    # tool-channel metacog nudges (tool_error / repeat) and
                    # any-channel nudges (watch_triggered / idle_children).
                    # All of them are now emitted as first-class
                    # ``{"role": "system"}`` turns AFTER this clean tool
                    # message (uniform attach rule) — the tool message content
                    # stays the raw tool output.  Accumulated here and flushed
                    # after the batch so every system turn lands after the
                    # COMPLETE tool block (the native-path placement rule).
                    result_advisories = self._collect_advisories(
                        assessment, _tc_names.get(tc_id, ""), _ri == _last_idx
                    )

                    _tname = _tc_names.get(tc_id, "")
                    # Image output rides the canonical turn BY REFERENCE: the
                    # inline bytes stay on ``output`` (token est + store_text +
                    # persist below) while the turn carries ``{type:image,
                    # attachment_id}`` placeholders the wire resolves at send.
                    tool_content, tool_image_atts = self._tool_content_by_reference(output, _tname)
                    tool_msg: dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_content,
                    }
                    tool_is_error = self._tool_error_flags.pop(tc_id, False)
                    if tool_is_error:
                        tool_msg["is_error"] = True
                    tool_status = self._tool_status.pop(tc_id, None)
                    if tool_status is not None:
                        tool_msg["_effect_status"] = tool_status.value
                    self.messages.append(turn_from_dict(tool_msg))

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

                    # Log the clean tool result.  Store the joined text for
                    # list-typed output (image / structured MCP results), the
                    # string verbatim otherwise — the persisted row matches
                    # ``self.messages[i]['content']`` (no envelope).  Size is
                    # already bounded by ``_truncate_output`` above (per-turn
                    # context budget); no second cap needed.
                    # ``tool_content``/``tool_image_atts`` were computed above
                    # (the turn carries the refs); persist the same bytes
                    # content-addressed so the vision output survives a reload.
                    if isinstance(output, list):
                        store_text: str = " ".join(
                            p.get("text", "")
                            for p in output
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    else:
                        store_text = output
                    tool_message_id = save_message(
                        self._ws_id,
                        "tool",
                        store_text,
                        _tname,
                        tool_call_id=tc_id,
                        event_id=self._ui_event_id(),
                        is_error=tool_is_error,
                        meta=_effect_status_meta(tool_status),
                    )
                    if tool_image_atts and tool_message_id:
                        self._persist_attachment_refs(
                            tool_message_id, tool_image_atts, origin="tool"
                        )

                    # Accumulate this result's operator context (guard
                    # findings per-result; queued interjections + metacog
                    # nudges only on the last result).  Flushed as system
                    # turns after the batch.
                    pending_system_turns.extend(result_advisories)

                # Flush accumulated operator context as first-class system
                # turns AFTER the complete tool batch.  Each
                # ``_append_system_turn`` persists its own row + fires the live
                # ``on_system_turn`` SSE hook + pushes a ``_msg_tokens`` entry,
                # so multi-tab mirrors and the token budget stay in lockstep.
                for source, content, meta in pending_system_turns:
                    self._append_system_turn(source, content, **meta)
                # Fold ``user_feedback`` (text typed alongside an approval,
                # e.g. "y, use full path") and any queued messages that
                # raced past Seam 1's drain into a single trailing user
                # row.  Seam 2 in the queued-message architecture: the
                # safety net for items that landed in ``_queued_messages``
                # *after* ``_collect_advisories`` ran for the last result
                # but *before* ``_execute_tools`` returned.  Common case
                # (no feedback, queue empty) no-ops; coexistence case
                # produces one user turn with feedback as the prefix
                # joined to queued items by ``\n\n``.
                self._flush_queued_messages(prefix=user_feedback or "")

                # Don't mutate shared history from an orphaned (superseded)
                # thread: a force-cancel handoff bumps _generation, and
                # _maybe_compact_midturn can replace self.messages out from
                # under the active generation.
                if self._generation != my_generation:
                    return
                # Cooperative mid-turn compaction — advise once under context
                # pressure so the model can reach a stopping point and spill its
                # plan, then compact if it keeps working (or immediately over
                # the hard ceiling).  Skip if a compaction was already attempted
                # pre-truncation this iteration: re-running would double the work
                # (and retry-storm on a failed summary); truncation already
                # bounded the batch, and the next iteration / end-of-turn
                # re-checks.
                if not pre_attempted_compact:
                    self._maybe_compact_midturn(my_generation)
        except GenerationCancelled:
            # If a newer send() has started (force cancel), this thread is
            # orphaned — skip all message mutations and state changes.
            if self._generation != my_generation:
                return
            # Cooperative cancellation — preserve partial content if
            # available and annotate it so downstream readers can
            # distinguish a cancelled fragment from a completed turn.
            # Without the annotation, an inspect_workstream / wait
            # surface caller (or a coord-LLM reading the child's
            # transcript on the next turn) sees a truncated-but-real
            # text fragment with no marker and may treat it as the
            # final answer — same hazard the operator-shakedown report
            # flagged ("…cannot simultaneously guarantee Consistency,"
            # surfaced as if it were a complete sentence).
            if self._cancelled_partial_msg:
                # _stream_response was interrupted — save partial
                # assistant msg.  Two shapes:
                #
                # - Some text streamed before cancel: append the
                #   marker so downstream readers can distinguish a
                #   cancelled fragment from a completed turn.
                # - Cancel landed before the first content token:
                #   keep the marker AS the message so the in-memory
                #   history and the persisted row stay consistent
                #   (the prior shape skipped persistence in this
                #   case, leaving the next-turn replay with an
                #   empty-content assistant message in messages but
                #   nothing in storage — divergent on rehydrate).
                msg = self._cancelled_partial_msg
                self._cancelled_partial_msg = None
                content = msg.get("content", "")
                if content:
                    msg["content"] = content + "\n\n[generation cancelled before completion]"
                else:
                    msg["content"] = "[generation cancelled before completion]"
                save_message(self._ws_id, "assistant", msg["content"], event_id=self._ui_event_id())
                self.messages.append(turn_from_dict(msg))
                tok_est = max(
                    1,
                    int(self._msg_char_count(msg) / self._chars_per_token),
                )
                self._msg_tokens.append(tok_est)
            else:
                # Cancelled during tool execution — synthesize cancelled
                # tool_result for any tool_calls that lack a matching result.
                # This keeps the conversation valid for both providers while
                # preserving the full tool call structure in history.
                self._synthesize_cancelled_results("Cancelled by user.")
            # Drain any queued user messages so they appear in the
            # conversation and are visible on the next send().
            self._flush_queued_messages()
            self._drain_pending_advisories()
            # No need to clear _cancel_event — it's replaced per-generation
            # in send(), so this generation's event is simply discarded.
            self.ui.on_info("[Generation cancelled]")
            self._emit_state("idle")
            # Do NOT re-raise — return normally so server worker thread
            # completes cleanly.
        except KeyboardInterrupt as exc:
            self._synthesize_cancelled_results("Interrupted by user.")
            self._flush_queued_messages()
            self._drain_pending_advisories()
            self._record_fatal_error(exc)
            raise
        except Exception as exc:
            self._flush_queued_messages()
            self._drain_pending_advisories()
            self._record_fatal_error(exc)
            raise
        finally:
            # Release the per-send wire-part memo (it can hold large rasterized
            # PDF page-images) so it is GC'd at send end rather than retained on
            # an idle session until the next send.  Restores the "None outside a
            # send" invariant on every exit (success, cancel, or error).
            self._wire_part_cache = None
            # Consume this generation's cancel signal on exit so a cancel that
            # targeted THIS send can't later abort an unrelated idle operation
            # (e.g. a manual /compact between sends would otherwise inherit the
            # still-set event).  Only when still the active generation — a newer
            # send owns a fresh event we must not clear out from under it.  This is
            # why a manual /compact no longer needs to reset the event itself
            # (which would have disarmed a cancel aimed at a concurrent send).
            if self._generation == my_generation:
                self._cancel_event.clear()

    def _drain_pending_advisories(self) -> None:
        """Drop every pending nudge regardless of channel.

        Tool-channel nudges (``tool_error``, ``repeat``) queued earlier
        in this batch and user-channel nudges (``correction``,
        ``denial``, …) queued during ``_check_metacognitive_nudge`` but
        not yet drained share the same per-session :class:`NudgeQueue`.
        When a generation is abandoned (cancel, KeyboardInterrupt,
        unexpected exception) the entire queue drops so nothing bleeds
        into the next send's tool loop or next user turn.
        """
        self._nudge_queue.clear()

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
            if msg.role is Role.ASSISTANT and msg.tool_calls:
                assistant_idx = i
                break
        if assistant_idx is None:
            return

        # Collect tool_call IDs that already have results
        answered_ids: set[str] = set()
        for msg in self.messages[assistant_idx + 1 :]:
            if msg.role is Role.TOOL:
                answered_ids.add(msg.tool_call_id or "")

        # An orphaned tool_call had no result when cancel landed.  We can't
        # tell here whether it was mid-execution (outcome unobserved) or
        # never started, so we mark it UNKNOWN rather than let the bare
        # reason read as "it didn't happen" — which invites a re-send as
        # readily as a dropped record causes an orphan (cancellation
        # appendix, HYPOTHESIS.md: unknown, never none).  ``is_error`` stays
        # True: it is genuinely not a successful result, and both the SSE
        # batch completion and the existing UI rendering key on it.
        detail = f"{reason} {UNOBSERVED_OUTCOME_CLAUSE}"
        # Synthesize results for unanswered tool_calls
        for tc in self.messages[assistant_idx].tool_calls:
            tc_id = tc.id
            func_name = tc.name
            if tc_id and tc_id not in answered_ids:
                self.messages.append(
                    Turn.tool(tc_id, detail, is_error=True, effect_status=EffectStatus.UNKNOWN)
                )
                self._msg_tokens.append(1)
                save_message(
                    self._ws_id,
                    "tool",
                    detail,
                    func_name,
                    tool_call_id=tc_id,
                    event_id=self._ui_event_id(),
                    is_error=True,
                    meta=_effect_status_meta(EffectStatus.UNKNOWN),
                )
                # Emit synthetic tool_result so live SSE listeners can
                # complete the in-DOM tool batch — without this the
                # coord ``--running`` indicator (added by SSE
                # tool_info) would spin forever on cancelled batches.
                # Defensive: we're already on a cancel/error path, so
                # a UI hook failure must not compound the problem.
                try:
                    self.ui.on_tool_result(tc_id, func_name, detail, is_error=True)
                except Exception:
                    log.debug(
                        "session.synthesize_cancelled.ui_emit_failed ws=%s",
                        self._ws_id[:8],
                        exc_info=True,
                    )

    # -- Rewind / retry -------------------------------------------------------

    def _find_turn_boundaries(self) -> list[int]:
        """Return indices of real user messages in self.messages (turn starts).

        Excludes the synthetic ``[Conversation summary]`` user turn that
        :meth:`_compact_messages` injects ahead of a summary.  It is a compaction
        artifact, not a real turn: as a retry/rewind target it would re-send the
        bare label and regenerate over it (clobbering the summary), and as the
        pre-send ``preserve_tail`` anchor it would preserve the label instead of
        the real question.  Tested by the ``source`` tag both producers set
        (:meth:`_compact_messages` in memory, ``reconstruct_turns_checkpointed``
        on resume), not by content — so a user who literally types the label
        keeps a real boundary, and retry/rewind treat their message normally.
        """
        return [
            i
            for i, m in enumerate(self.messages)
            if m.role is Role.USER and m.source != COMPACTION_SOURCE
        ]

    def _persist_truncation(self, removed_count: int) -> None:
        """Mirror a rewind/retry tail-trim into storage, compaction-safe.

        rewind/retry remove turns from the in-memory TAIL, which map 1:1 to the
        most recent storage rows.  Deleting by *removed-row count* from the
        storage end — rather than keeping the first ``len(self.messages)`` rows —
        stays correct after a compaction, where ``self.messages`` holds synthetic
        summary turns with no 1:1 storage rows while storage still holds the full
        pre-compaction transcript plus the checkpoint marker.  The keep-count is
        floored at the compaction boundary (:func:`get_compaction_floor`) so the
        summary's backing rows and the marker are never deleted.  Identical to the
        old ``keep = len(self.messages)`` when the ws never compacted (floor 0,
        total == in-memory length).

        Residual: an over-deep rewind that would cross the summary boundary clamps
        at the floor in storage, so the in-memory trim can drop more than storage
        does; a later resume rehydrates ``[summary] + [surviving tail]`` and the
        two reconcile.
        """
        if removed_count <= 0:
            return
        total = count_messages(self._ws_id)
        if total <= 0:
            # Count unavailable (storage error) — skip the delete rather than risk
            # a wrong truncation; the in-memory trim holds and resume reconciles.
            return
        floor = get_compaction_floor(self._ws_id)
        if floor < 0:
            # Floor unavailable (storage error). A 0 here is indistinguishable from
            # "never compacted", so an over-deep trim could delete the marker and
            # summarized prefix — skip rather than risk it; resume reconciles.
            return
        delete_messages_after(self._ws_id, max(floor, total - removed_count))

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
        self._persist_truncation(removed_count)
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
        content = turn_to_dict(self.messages[last_user_idx]).get("content")
        # Multipart messages (vision/images) have list-type content;
        # retry only supports plain text.
        if not isinstance(content, str) or not content:
            return None
        # Drop everything from (and including) the user message onward;
        # send() will re-append the user message.
        removed_count = len(self.messages) - last_user_idx
        del self.messages[last_user_idx:]
        del self._msg_tokens[last_user_idx:]
        self._persist_truncation(removed_count)
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
            # Flush whatever was buffered and build a partial message.
            # Both ``tool_calls`` and ``_provider_content`` are
            # DELIBERATELY OMITTED:
            #   * ``tool_calls`` — incomplete, no matching tool_result;
            #     re-emitting on the next turn would orphan them.
            #   * ``_provider_content`` — the Anthropic provider reads
            #     this lane verbatim ahead of plain ``content`` (see
            #     ``providers/_anthropic.py``), and a cancellation can
            #     leave partial tool_use blocks here too.  Keeping it
            #     would also cause the next-turn replay to bypass the
            #     ``[generation cancelled before completion]`` marker
            #     the cancel handler appends to ``content``, hiding
            #     the partial-output signal from the model.
            if pending:
                _flush_text(pending, in_think)
            self.ui.on_stream_end()
            partial: dict[str, Any] = {"role": "assistant"}
            partial_content = "".join(content_parts)
            partial["content"] = partial_content or ""
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
                # Same reasoning as the cooperative-cancel branch
                # above: ``_provider_content`` is omitted so the
                # next-turn replay reads from the marker-bearing
                # plain content and any partial tool_use blocks
                # inside provider_blocks don't leak through.
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
        # (e.g. Anthropic web_search_tool_result with encrypted_content).
        # Phase 3 path-3 capture: when no native blocks were emitted but
        # ``reasoning_delta`` chunks accumulated text, synthesize a
        # ``reasoning_text`` block so the captured reasoning survives
        # past the live stream and surfaces on history reload.
        provider_blocks = self._maybe_synth_reasoning_block(provider_blocks, reasoning_parts)
        # Enforce the native↔tool_calls mirror in memory too.  A truncation that cleared
        # tool_calls (finish_reason="length") can leave an orphan tool_use in the captured
        # blocks; the save-time chokepoint fixes the persisted row, but a same-session
        # continuation reads this in-memory copy, so strip the orphan here as well.
        # See storage._utils.normalize_native_for_save.
        if provider_blocks and not msg.get("tool_calls"):
            provider_blocks = strip_orphan_client_tool_blocks(provider_blocks)
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
                    elif p.get("type") in ("image_url", "image"):
                        # ``image`` is the by-reference vision placeholder
                        # ({type, attachment_id}); ``image_url`` the resolved
                        # inline form — both flatten to the same marker.
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
    def _msg_text_chars(msg: dict[str, Any] | Turn) -> tuple[int, int, int]:
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

        Accepts a wire dict or a canonical ``Turn`` (the latter is lowered to
        its dict form so the char accounting matches what the provider sees).
        """
        if isinstance(msg, Turn):
            msg = turn_to_dict(msg)
        content = msg.get("content")
        n = 0
        images = 0
        doc_chars = 0
        inline_doc = False
        if isinstance(content, list):
            for p in content:
                ptype = p.get("type")
                if ptype == "text":
                    n += len(p.get("text", ""))
                elif ptype == "image_url" or (ptype == "image" and p.get("attachment_id")):
                    # Resolved inline image, or the by-reference image placeholder
                    # — both cost one fixed image budget.
                    images += 1
                elif ptype == "document" and not p.get("attachment_id"):
                    # Resolved inline document (the transient materialized form):
                    # count its data chars directly.
                    inline_doc = True
                    d = p.get("document", {})
                    doc_chars += len(d.get("data", ""))
                    doc_chars += len(d.get("name", ""))
                    doc_chars += len(d.get("media_type", ""))
        else:
            n += len(content or "")
        # A by-reference document placeholder (``{type:document, attachment_id}``)
        # carries no inline bytes, so its budget comes from the sibling
        # ``_attachments_meta`` (``size_bytes`` per text-kind attachment).  Skip
        # when an inline document was already counted: canonical messages are
        # by-reference + meta and the materialized wire form is inline-without-meta,
        # so the two are mutually exclusive — the guard makes that robust either way.
        if not inline_doc:
            meta = msg.get("_attachments_meta")
            if isinstance(meta, list):
                for e in meta:
                    if not isinstance(e, dict):
                        continue
                    k = e.get("kind")
                    sz = int(e.get("size_bytes") or 0)
                    if k == "text":
                        doc_chars += sz
                    elif k in ("pdf", "audio"):
                        # By-reference media materializes to a much smaller form
                        # whose exact size isn't known here; charge a bounded
                        # estimate so the turn is neither budgeted as ~zero
                        # (over-context) nor as the full source blob (over-trim).
                        doc_chars += min(sz, _DOC_BUDGET_CHAR_CAP)
                    # image by-reference is already charged a fixed image budget
                    # in the content loop above.
        for tc in msg.get("tool_calls", []):
            n += len(tc.get("id", ""))
            n += len(tc.get("function", {}).get("name", ""))
            n += len(tc.get("function", {}).get("arguments", ""))
        # Structural overhead: role, tool_call_id
        n += len(msg.get("role", ""))
        n += len(msg.get("tool_call_id", ""))
        return n, images, doc_chars

    def _msg_char_count(self, msg: dict[str, Any] | Turn) -> int:
        """Count characters in a message, including structural overhead.

        Includes role markers, tool_call IDs, image placeholders, and
        document-part characters so that the budget estimate reflects
        the full payload the provider sees.  Accepts a wire dict or a ``Turn``.
        """
        text_chars, images, doc_chars = self._msg_text_chars(msg)
        return text_chars + doc_chars + int(images * self._IMAGE_TOKENS * self._chars_per_token)

    def _update_token_table(
        self,
        assistant_msg: dict[str, Any],
        *,
        msgs: list[dict[str, Any]] | None = None,
    ) -> None:
        """Update per-message token estimates using API usage data.

        *msgs* (optional) is the wire-bound message list already built
        for the stream call — passing it avoids a redundant
        ``_prepare_wire_messages`` walk and ensures the char count matches
        the bytes the provider counted.  When *msgs* is None the caller
        didn't pre-build (rare path) — fall back to folding on the fly.
        """
        if not self._last_usage:
            return

        prompt_tok = self._last_usage["prompt_tokens"]
        compl_tok = self._last_usage["completion_tokens"]

        # Calibrate chars_per_token ratio from actual usage.
        # Images get a fixed token budget (subtracted).  Documents
        # tokenize non-linearly depending on provider — excluded from
        # calibration so they don't skew the text ratio.  ``all_msgs``
        # must reflect what the provider actually counted in
        # ``prompt_tokens``: when called from the loop with the
        # pre-built ``msgs``, that's exact; without it, fall back to
        # folding the system turns fresh.
        all_msgs = (
            msgs if msgs is not None else self._prepare_wire_messages(self._full_messages())
        )  # system + self.messages (before append)
        tool_def_chars = self._tool_def_chars()
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

    # Shared "## Output format" section for both compactor prompts — the section
    # list plus its trailing blank line.  Single-sourced so the two prompts can't
    # drift (the merge prompt's "explicitly stated" line had already lost "the
    # user").
    _COMPACT_OUTPUT_FORMAT = (
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
    )

    # System prompt for the depth-0 summary call (the conversation compactor).
    _COMPACTOR_SYSTEM_PROMPT = (
        "# Conversation Compactor\n\n"
        "Your output REPLACES the conversation history — the assistant "
        "will continue from your summary with no access to the original messages.\n\n"
        + _COMPACT_OUTPUT_FORMAT
        + "2. **Density rules:**\n"
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
    )

    # System prompt for recursion levels (depth > 0): merge partial summaries of
    # consecutive slices of ONE conversation back into a single summary.
    _COMPACTOR_MERGE_SYSTEM_PROMPT = (
        "# Summary Merger\n\n"
        "You are given several partial summaries of ONE conversation, produced by "
        "compacting consecutive slices in order. Merge these partial summaries into a "
        "single summary that REPLACES the conversation history — the assistant will "
        "continue from your merged summary with no access to the originals.\n\n"
        + _COMPACT_OUTPUT_FORMAT
        + "2. **Merge rules:**\n"
        "   - Preserve every distinct decision, file, identifier, and open task across "
        "all partials; later partials reflect more recent state, so on conflict prefer "
        "the later one.\n"
        "   - Deduplicate: fold repeated items into one, keeping the most specific.\n"
        "   - Preserve exact paths, identifiers, and numbers — never paraphrase these.\n"
        "   - Be dense; this is a summary, not a transcript."
    )

    # User-message wrapper prepended to every summary-call body (all depths).
    _COMPACT_USER_PREFIX = "Compact the following conversation:\n\n"

    @staticmethod
    def _summary_tc_names(messages: list[dict[str, Any]]) -> dict[str, str]:
        """Build a tool_call_id → tool_name lookup for labeling tool results.

        Built over the full message set (not one batch) so a ``tool_use`` and its
        later ``tool_result`` keep a consistent name even when chunking packs them
        into different batches.
        """
        tc_names: dict[str, str] = {}
        for m in messages:
            for tc in m.get("tool_calls", []):
                tc_id = tc.get("id", "")
                tc_name = tc.get("function", {}).get("name", "unknown")
                if tc_id:
                    tc_names[tc_id] = tc_name
        return tc_names

    def _format_message_for_summary(
        self, m: dict[str, Any], tc_names: dict[str, str]
    ) -> str | None:
        """Format one message into a summary line, or ``None`` when it carries no
        renderable content.

        ``tc_names`` (tool_call_id → tool_name, built by the caller over the full
        selected set) labels tool results.  Long content is capped head+tail so a
        single message can't dominate the summary input.
        """
        role = m["role"].upper()
        content = m.get("content") or ""

        # Flatten list content (image tool results) to text for summary
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if p.get("type") == "text":
                    text_parts.append(p["text"])
                elif p.get("type") in ("image_url", "image"):
                    # by-reference vision placeholder OR resolved inline image
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
            return f"{role}: {content}"
        return None

    def _summary_blocks(self, messages: list[dict[str, Any]]) -> list[str]:
        """Format messages into summary blocks — one string per renderable
        message, in order (drops messages with no renderable content)."""
        tc_names = self._summary_tc_names(messages)
        return [
            line
            for m in messages
            if (line := self._format_message_for_summary(m, tc_names)) is not None
        ]

    def _format_messages_for_summary(self, messages: list[dict[str, Any]]) -> str:
        """Format messages into a readable string for the summarization prompt."""
        return "\n\n".join(self._summary_blocks(messages))

    def _summary_output_tokens(self) -> int:
        """Output-token reserve for a summary call, bounded so the input (the
        history being summarized) always keeps the larger share of the window.

        ``compact_max_tokens`` defaults to the full window (32768); clamped only
        by ``max_output_tokens`` it would reserve the entire context for output
        and leave no room to send anything to summarize — flooring the input
        budget and making the summary call overflow (or bail as "irreducible")
        on a small/default window.  Cap the reserve at half the window so
        compaction can actually run; large windows are unaffected because
        ``compact_max_tokens`` stays the binding limit there.
        """
        caps = self._get_capabilities()
        hard_cap = (
            min(self.compact_max_tokens, caps.max_output_tokens)
            if caps.max_output_tokens
            else self.compact_max_tokens
        )
        window_cap = max(self._MIN_SUMMARY_OUTPUT_TOKENS, self.context_window // 2)
        return min(hard_cap, window_cap)

    def _carry_budget_chars(self, carries: int = 1) -> int:
        """Per-carry char budget for content carried VERBATIM across a
        compaction — the continuation hint's quote of the user's last
        message, and the wind-down spill.

        A quarter of the window per carry, clamped so ALL concurrent carries
        fit what the window spares after the summary output reserve AND the
        fixed prompt overhead (system message + tool definitions — the same
        terms the ``_estimated_prompt_tokens`` fallback counts, because they
        ride every request): the carried text lands in the same
        post-compaction prompt as all of those, so
        ``overhead + reserve + carries·budget + margin ≤ window`` must hold
        by construction — sizing carries independently (or ignoring the
        overhead) stacks past the window at default config exactly when
        spill and hint fire together, and the overflow backstop would then
        re-compact WITHOUT the carries.  Floored at
        ``_MIN_CARRY_BUDGET_CHARS`` so small windows still carry something
        meaningful; when the floor exceeds the spare (a tiny window, or a
        system prompt that fills it) the floor wins deliberately — carrying
        something beats carrying nothing, and the overflow backstop absorbs
        the worst case.  Chars via the calibrated ``_chars_per_token``.
        """
        reserve = self._summary_output_tokens()
        margin = int(self.context_window * self._SUMMARY_SAFETY_MARGIN)
        overhead = self._system_tokens + self._tool_def_tokens()
        spare = max(0, self.context_window - reserve - margin - overhead)
        budget_tokens = min(self.context_window // 4, spare // max(1, carries))
        return max(self._MIN_CARRY_BUDGET_CHARS, int(budget_tokens * self._chars_per_token))

    def _summary_input_budget_chars(self) -> int:
        """Per-call input budget for a summary completion, in characters.

        The summary runs on ``self.model`` via :meth:`_utility_completion`, so its
        input + output must fit ``self.context_window``.  Sizing the *selection* by
        the per-message token estimate (which disagrees with the head+tail-capped
        formatted text) is what let a long conversation overflow the summary call
        itself; this measures the call's real budget instead.

        Reserve the output (the bounded :meth:`_summary_output_tokens` reserve),
        the compactor prompt, and a safety margin; scale the remainder by
        ``_SUMMARY_BUDGET_FRACTION`` to cover the uncalibrated ``_chars_per_token``
        on the reactive path (no ``_last_usage`` yet); convert to chars; floor at
        ``_MIN_SUMMARY_BUDGET_CHARS`` so a usable window still makes progress, but
        never above the true input capacity — flooring past what actually fits
        would reintroduce a summary-call overflow on a pathologically small
        window (the call bails as "irreducible" instead).
        """
        output_reserve = self._summary_output_tokens()
        prompt_chars = len(self._COMPACTOR_SYSTEM_PROMPT) + len(self._COMPACT_USER_PREFIX)
        prompt_tokens = int(prompt_chars / self._chars_per_token)
        safety = int(self.context_window * self._SUMMARY_SAFETY_MARGIN)
        input_tokens = self.context_window - output_reserve - prompt_tokens - safety
        budget_tokens = max(0, int(input_tokens * self._SUMMARY_BUDGET_FRACTION))
        budget_chars = max(
            self._MIN_SUMMARY_BUDGET_CHARS, int(budget_tokens * self._chars_per_token)
        )
        # Cap the floor at the true input capacity: on a pathologically small
        # window the floor would otherwise push the summary call (input + output
        # reserve + prompt) past context_window instead of letting it bail.
        return min(budget_chars, max(0, int(input_tokens * self._chars_per_token)))

    @staticmethod
    def _truncate_block(block: str, budget: int) -> str:
        """Hard-truncate a single oversized block to ``budget`` chars, keeping
        the head and tail around a marker that reports the ORIGINAL size — the
        reader (the summarizer, or on the verbatim-carry paths the
        post-compaction model) sees how much is missing, not just that a cut
        happened.

        Used for a lone block that can't fit any summary batch, and for the
        verbatim carries (ask quote / wind-down spill).  The result is always
        ``<= budget``.
        """
        if len(block) <= budget:
            # Already fits — return as-is.  Without this, a budget wider than the
            # block makes the head slice ``block[:head]`` and tail slice
            # ``block[-tail:]`` overlap and DUPLICATE content around the marker.
            return block
        marker = f"\n…[truncated — {len(block):,} chars total]…\n"
        if budget <= len(marker):
            return block[:budget]
        keep = budget - len(marker)
        head = (keep * 2) // 3
        tail = keep - head
        return block[:head] + marker + block[-tail:] if tail else block[:head] + marker

    def _pack_blocks(self, blocks: list[str], budget_chars: int) -> list[list[str]]:
        """Greedily pack formatted blocks into batches that each fit ``budget_chars``.

        In order, no reordering and no drops: blocks accumulate into the current
        batch until the next one (plus the ``"\\n\\n"`` join) would overflow, then a
        new batch starts.  A lone block larger than the budget gets its own batch,
        hard-truncated via :meth:`_truncate_block`.  Never emits an empty batch.

        ``current_len`` tracks ``len("\\n\\n".join(current))`` exactly, so every
        returned batch's joined length is ``<= budget_chars``.
        """
        budget = max(1, budget_chars)
        sep = len("\n\n")
        batches: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for block in blocks:
            if len(block) > budget:
                # Oversized lone block: flush the in-progress batch, then emit it
                # alone, hard-truncated (head + tail preserved).
                if current:
                    batches.append(current)
                    current = []
                    current_len = 0
                batches.append([self._truncate_block(block, budget)])
                continue
            added = len(block) + (sep if current else 0)
            if current and current_len + added > budget:
                batches.append(current)
                current = [block]
                current_len = len(block)
            else:
                current.append(block)
                current_len += added
        if current:
            batches.append(current)
        return batches

    def _summarize_once(self, system_prompt: str, body: str) -> str:
        """Run one summary completion over ``body`` and return the cleaned text.

        Owns the retry loop (transient errors only, exponential backoff) and the
        reasoning-tag strip.  Raises on a non-retryable error or retry exhaustion
        so the caller can abort the whole compaction before any message swap.
        """
        summary_msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._COMPACT_USER_PREFIX + body},
        ]
        result: CompletionResult | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                result = self._utility_completion(
                    summary_msgs,
                    max_tokens=self._summary_output_tokens(),
                )
                break
            except Exception as e:
                ename = type(e).__name__
                if self._stop_retrying(e, attempt, self._provider):
                    # Overflow is deterministic — let _summarize_batch subdivide
                    # instead of retrying an identical oversized call.
                    raise
                delay = self._RETRY_BASE_DELAY * (2**attempt)
                self.ui.on_info(f"[Compact retrying in {delay:.0f}s: {ename}]")
                time.sleep(delay)
        assert result is not None
        # Strip any <think>/<reasoning> tags the summarizer may emit
        summary = self._strip_reasoning(result.content or "")
        if result.finish_reason == "length":
            self.ui.on_info("[Warning: compaction summary was truncated]")
        return summary

    def _summarize_blocks(self, blocks: list[str], *, depth: int = 0) -> str:
        """Summarize ``blocks`` into one dense summary, chunking + recursing so no
        single model call exceeds the model window.

        Blocks are packed to the char budget (:meth:`_summary_input_budget_chars`)
        and each batch is summarized; the partials are recursively merged one level
        deeper until they collapse to a single summary.  That char budget is only an
        *estimate* — it divides by the uncalibrated ``_chars_per_token`` (which sits
        at an optimistic 4.0 on resume) — so a batch that fits by chars can still
        overflow the *token* window.  :meth:`_summarize_batch` absorbs that: an
        over-window multi-block batch is split in half and each half summarized
        (recursing as needed), then the partials merged (chunking, not truncation),
        and only a lone block that overflows by itself is head/tail-truncated.

        Bails with :class:`_CompactionIrreducibleError` only at the recursion
        ceiling ``_MAX_SUMMARY_DEPTH`` (or when a floored lone block still overflows)
        — the caller turns that into a ``return False`` rather than fabricate a
        summary.
        """
        system_prompt = (
            self._COMPACTOR_SYSTEM_PROMPT if depth == 0 else self._COMPACTOR_MERGE_SYSTEM_PROMPT
        )
        # Recursion ceiling FIRST — before packing and the single-batch base case —
        # so it also bounds the per-block-split merge, which can re-pack into one
        # batch and would otherwise recurse without ever consulting the ceiling.
        if depth >= self._MAX_SUMMARY_DEPTH:
            raise _CompactionIrreducibleError
        batches = self._pack_blocks(blocks, self._summary_input_budget_chars())
        if len(batches) == 1:
            return self._summarize_batch(system_prompt, batches[0], depth)

        # More than one batch: recurse-merge the per-batch summaries.  A block-count
        # guard would be wrong here — _summarize_batch's binary subdivision can
        # legitimately leave one batch per block — so termination rides the depth
        # ceiling above plus the progressive-shrink-to-floor bail in _summarize_batch.
        total = len(batches)
        summaries: list[str] = []
        for k, batch in enumerate(batches, start=1):
            self.ui.on_info(f"[compacting part {k}/{total}…]")
            summaries.append(self._summarize_batch(system_prompt, batch, depth))
        return self._summarize_blocks(summaries, depth=depth + 1)

    def _summarize_batch(self, system_prompt: str, batch: list[str], depth: int) -> str:
        """Summarize one packed batch, subdividing on a token-window overflow.

        The char budget that produced ``batch`` is only an estimate, so the model
        call can overflow the real token window.  When a multi-block batch overflows,
        it is split in HALF and each half summarized (recursing if a half still
        overflows), then the two partials merged — binary subdivision, so an
        over-window batch of N blocks costs ~log2(N) levels and a near-optimal number
        of leaf calls, NOT N per-block calls (which on a wide rehydrated resume meant
        hundreds of serial summaries).  A lone block that overflows even by itself is
        head/tail-truncated progressively (halving the budget down to the floor, the
        single-block analogue of the binary subdivision above) so it keeps as much as
        the window allows; only if even the floor still overflows is the window too
        small to compact anything and we bail irreducible.
        """
        # Cooperative cancellation: a cancel mid-compaction aborts here.  It raises
        # GenerationCancelled (a BaseException), so _compact_messages' ``except
        # Exception`` can't swallow it and the message-swap below never runs — the
        # history is left untouched.
        self._check_cancelled()
        try:
            return self._summarize_once(system_prompt, "\n\n".join(batch))
        except Exception as e:
            if not _is_ctx_overflow(e):
                raise
            if len(batch) > 1:
                # Token-overflow despite the char budget: split the batch in HALF
                # and summarize each (each half recurses + halves again if it still
                # overflows), then merge.  Binary subdivision keeps each leaf as full
                # as fits — a wide over-window batch costs ~log2(N) calls, not one
                # model call per block.
                mid = len(batch) // 2
                left = self._summarize_batch(system_prompt, batch[:mid], depth)
                right = self._summarize_batch(system_prompt, batch[mid:], depth)
                return self._summarize_blocks([left, right], depth=depth + 1)
            # A lone block overflows by itself: the char budget over-estimated how
            # many tokens it holds.  Shrink progressively — halve the truncation
            # budget and retry, keeping as much of the message as the real window
            # allows — mirroring the multi-block binary subdivision above rather than
            # slamming straight to the 2 000-char floor (which would discard far more
            # than the window actually forces).  Bail irreducible only once even the
            # floor still overflows.
            budget = max(self._MIN_SUMMARY_BUDGET_CHARS, len(batch[0]) // 2)
            while True:
                try:
                    return self._summarize_once(
                        system_prompt, self._truncate_block(batch[0], budget)
                    )
                except Exception as e2:
                    if not _is_ctx_overflow(e2):
                        raise
                    if budget <= self._MIN_SUMMARY_BUDGET_CHARS:
                        raise _CompactionIrreducibleError from e2
                    budget = max(self._MIN_SUMMARY_BUDGET_CHARS, budget // 2)

    def _compact_messages(
        self,
        auto: bool = False,
        preserve_tail: int = 0,
        my_generation: int = 0,
        carry_spill: bool = False,
    ) -> bool:
        """Compact conversation history by summarizing it into a summary turn.

        Summarizes the whole selection via :meth:`_summarize_blocks`, which packs
        to the char-budget estimate and, when a batch still overflows the real token
        window, binary-subdivides it (:meth:`_summarize_batch`) — so the summary call
        recovers from an under-estimate by chunking rather than overflowing, and the
        most-recent messages are no longer silently dropped.

        When auto=True (triggered by context limit), appends a continuation
        hint quoting the last user message — verbatim up to
        :meth:`_carry_budget_chars` — so the model can resume seamlessly.

        ``preserve_tail`` keeps the last N messages verbatim (re-appended after
        the summary) instead of summarizing them — used to keep an in-flight
        assistant tool-call turn so the tool results appended after compaction
        aren't orphaned from their ``tool_use``.

        ``carry_spill`` copies the final summarized assistant turn's text onto
        the summary under a ``## Wind-down (verbatim)`` heading — shell
        concatenation, not summarizer output.  The end-of-turn site sets it
        when the model stopped *because it was advised* to wrap up: that turn
        holds the goal/remaining-tasks/next-steps the advisory asked it to
        record, and the plan must cross a compaction copied, not paraphrased —
        the summarizer also reads the spill, but its paraphrase must not be
        the only survivor.
        """
        # Clear the cooperative latch on every compaction *attempt*, ahead of
        # the early-return guards below — a bailed compaction (too few/large
        # messages, summary error) must fall back to the advisory grace state
        # next cycle rather than retry-storm on the same over-soft estimate.
        self._compaction_advised = False
        if len(self.messages) < 2:
            self.ui.on_info("Not enough messages to compact.")
            return False

        # Optionally keep the last ``preserve_tail`` messages verbatim — e.g. an
        # in-flight assistant tool-call whose results are about to be appended, or
        # the just-sent user turn on the pre-send path — so compacting them away
        # can't orphan a tool result or drop the current question.  Re-appended
        # after the summary, below.
        preserved = self.messages[-preserve_tail:] if preserve_tail > 0 else []
        to_summarize = self.messages[:-preserve_tail] if preserve_tail > 0 else self.messages

        # Continuation hint: re-inject the user's last REAL message ONLY when it is
        # being summarized away.  When preserve_tail keeps it verbatim (the pre-send
        # path), the preserved tail already carries it — adding the hint would
        # duplicate the message and frame a fresh ask as "continue where we left
        # off".  Use _find_turn_boundaries so the synthetic [Conversation summary]
        # turn is excluded: when an already-compacted history is re-compacted, the
        # last "user" turn is that label, and quoting it would hand the model a
        # content-free "The user's last message was: [Conversation summary]".
        last_user_content = None
        if auto:
            split = len(to_summarize)  # preserved == self.messages[split:]
            real_users = self._find_turn_boundaries()
            if real_users and real_users[-1] < split:  # summarized, not preserved
                last_user_content = self.messages[real_users[-1]].text or ""

        # Build summary blocks from ALL of to_summarize (per-message), then
        # summarize via chunked/hierarchical compaction.  Sizing by the actual
        # formatted size (not the per-message token estimate, which disagreed
        # with the head+tail-capped formatted text) is what bounds the summary
        # call so it can't overflow self.context_window.  Summarizing the whole
        # selection — not a fitting prefix — also fixes the latent drop of the
        # most-recent (unselected) messages.
        to_summarize_dicts = dicts_from_turns(to_summarize)
        blocks = self._summary_blocks(to_summarize_dicts)
        if not blocks:
            self.ui.on_info("Not enough messages to compact.")
            return False

        self.ui.on_thinking_start()
        try:
            summary = self._summarize_blocks(blocks)
        except _CompactionIrreducibleError:
            self.ui.on_info("Messages too large to fit in summary context.")
            return False
        except Exception as e:
            self.ui.on_error(f"Compaction failed: {e}")
            return False
        finally:
            self.ui.on_thinking_stop()

        if not summary.strip():
            self.ui.on_info("Compaction produced an empty summary; keeping history.")
            return False

        # The verbatim carries: the wind-down spill and the continuation-hint
        # quote of the ask.  Both can fire on the SAME compaction (the
        # end-of-turn site), so they must share ONE budget — sized per-carry
        # against each other, the summary reserve stacks past the window at
        # default config (reserve cw/2 + 2·(cw/4) + margin > cw) and the
        # overflow backstop would then re-compact WITHOUT the spill,
        # paraphrasing away exactly what this carries.
        spill_text = ""
        if carry_spill and to_summarize:
            # to_summarize[-1] is the model's final turn only because the
            # end-of-turn site passes preserve_tail=0; a carry_spill caller
            # with preserve_tail > 0 must locate the spill ahead of the
            # preserved tail instead.
            spill = to_summarize[-1]
            if spill.role is Role.ASSISTANT:
                spill_text = (spill.text or "").strip()
        carries = (1 if spill_text else 0) + (1 if last_user_content else 0)
        carry_budget = self._carry_budget_chars(carries) if carries else 0

        # Wind-down first, then how to resume — the summary reads: sections,
        # what the model recorded, then the ask to continue from.
        carry_truncated = False
        if spill_text:
            carry_truncated = len(spill_text) > carry_budget
            summary += "\n\n## Wind-down (verbatim)\n" + self._truncate_block(
                spill_text, carry_budget
            )

        # Append continuation hint for auto-compact
        if last_user_content:
            # The quoted ask crosses verbatim up to the carry budget — the last
            # user message may BE the task (a pasted spec or diff), so a fixed
            # few-hundred-char clip amputates it.  Beyond the budget keep head
            # + tail around an honest marker.
            carry_truncated = carry_truncated or len(last_user_content) > carry_budget
            last_user_content = self._truncate_block(last_user_content, carry_budget)
            summary += (
                f"\n\n## Continue\n"
                f"The user's last message was: {last_user_content}\n"
                f"Continue assisting from where we left off."
            )

        # A truncated carry is a cache miss, not a loss: storage keeps the
        # full transcript, so tell the model where the rest lives instead of
        # leaving a bare cut.
        if carry_truncated:
            summary += (
                "\n\nTruncated content above is not lost: the full text remains "
                "in conversation history and the recall tool can retrieve it."
            )

        # Honor a cancel — or a newer generation that superseded this send DURING
        # the (possibly only, possibly slow) summary call — before we mutate state.
        # GenerationCancelled is a BaseException, so it propagates past the
        # except-clauses above and leaves self.messages untouched.  Passing
        # my_generation here is what stops an orphaned send from swapping the live
        # generation's history after its summary call returns (the event arm alone
        # can't see it: a newer send installed a fresh, clear event).
        self._check_cancelled(my_generation)

        # Replace messages — summary, then any preserved tail verbatim.
        before_tokens = self._system_tokens + sum(self._msg_tokens) + self._tool_def_tokens()
        # Both synthetic turns carry the compaction source tag so consumers
        # (_find_turn_boundaries, _generate_title) test provenance, not
        # content — a user who literally types the label stays a real turn.
        summary_user = {
            "role": "user",
            "content": COMPACTION_SUMMARY_LABEL,
            "_source": COMPACTION_SOURCE,
        }
        summary_asst = {
            "role": "assistant",
            "content": summary,
            "_source": COMPACTION_SOURCE,
        }
        self.messages = turns_from_dicts([summary_user, summary_asst]) + list(preserved)
        # File contents are gone after compaction — force re-read before edit_file
        self._read_files.clear()
        self._repeat_detector.clear()

        # Rebuild token table — summary turns + preserved-tail estimates.
        su_tok = max(1, int(self._msg_char_count(summary_user) / self._chars_per_token))
        sa_tok = max(1, int(self._msg_char_count(summary_asst) / self._chars_per_token))
        tail_toks = [
            max(1, int(self._msg_char_count(m) / self._chars_per_token)) for m in preserved
        ]
        self._msg_tokens = [su_tok, sa_tok, *tail_toks]
        self._calibrated_msg_count = len(self.messages)  # anchored to compacted state
        after_tokens = self._system_tokens + sum(self._msg_tokens) + self._tool_def_tokens()

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

        # Persist a compaction checkpoint so a reopen rehydrates [summary]+[tail]
        # instead of the full transcript — which, on a long session or one switched
        # to a smaller-context model, can exceed the window and deadlock the first
        # send. Storage keeps the full history for /history/export/audit; this
        # marker only governs the resume slice (load_message_turns). The watermark
        # is read BEFORE the marker row is written, so it bounds the summarized
        # rows and the marker takes the next (higher) id. Best-effort: both calls
        # swallow storage errors, so a failed marker just means the next reopen
        # reloads more history (today's behavior) rather than crashing compaction.
        if self._ws_id:
            watermark = get_compaction_watermark(self._ws_id, preserve_tail)
            if watermark is not None:
                save_message(
                    self._ws_id,
                    "assistant",
                    summary,
                    source=COMPACTION_SOURCE,
                    meta=json.dumps({"watermark": watermark}),
                    event_id=self._ui_event_id(),
                    producer=self._provider.provider_name if self._provider else None,
                )
        return True

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

    def _ensure_output_guard_judge(self) -> OutputGuardJudge | None:
        """Lazily initialize the output-guard LLM judge if configured.

        Re-checks the live ``output_guard_llm`` flag every call so toggling
        the LLM stage via admin settings takes immediate effect on
        existing sessions — same hot-reload semantics as
        :meth:`_ensure_judge`.
        """
        jc = self._judge_cfg
        if jc is None or not jc.output_guard_llm:
            return None
        if self._output_guard_judge is not None:
            return self._output_guard_judge
        if self._judge_config is None:
            return None
        try:
            from turnstone.core.output_guard_judge import OutputGuardJudge

            self._output_guard_judge = OutputGuardJudge(
                config=jc,
                session_provider=self._provider,
                session_client=self.client,
                session_model=self.model,
                model_registry=self._registry,
            )
        except Exception:
            log.warning("output_guard_judge.init_failed", exc_info=True)
        return self._output_guard_judge

    def _lookup_tool_description(self, name: str) -> str:
        """Look up a tool's description from the session's tools registry.

        Returns empty string when the name is unknown — the output-guard
        judge prompt skips empty sections, so an unknown tool simply
        loses the description line.  O(N) over ``self._tools`` (small
        list, ~20 tools).
        """
        for t in self._tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict) and fn.get("name") == name:
                desc = fn.get("description", "")
                return desc if isinstance(desc, str) else ""
        return ""

    def _evaluate_intent(
        self,
        items: list[dict[str, Any]],
    ) -> threading.Event | None:
        """Run intent validation on pending approval items.

        Attaches heuristic verdicts to items immediately.  Spawns the
        async LLM judge that delivers final verdicts via UI callback.

        Returns a cancel event that, when set, tells the daemon judge
        thread to abandon remaining work (each undone item degrades to
        an ``llm_fallback`` verdict carrying the heuristic content).
        ``_execute_tools`` fires it
        unconditionally when the next batch supersedes this generation,
        ``close()`` fires it on session teardown, and the approval
        gate's ``finally`` fires it on decision only when
        ``judge.cancel_on_approval`` is enabled — the default leaves
        the daemon running to completion so every call gets a real
        LLM verdict for the audit trail.
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
                it["func_args"] = {"query": it.get("query", ""), "category": it.get("category", "")}
            elif name == "skills":
                # Projection for judge / audit on the model-facing skills
                # tool. Mutating actions surface name + action + a snippet
                # of the proposed content / fields so a heuristic rule can
                # match on suspicious patterns (e.g. allowed_tools
                # expansion).  Long fields are capped for verdict-row size.
                action_val = it.get("action", "")
                fa: dict[str, Any] = {"action": action_val, "name": it.get("name", "")}
                if action_val in ("find", "get"):
                    pass
                elif action_val == "load":
                    pass  # name only — covers the surface
                elif action_val == "create":
                    fa["category"] = it.get("category", "")
                    fa["kind"] = it.get("kind", "")
                    fa["description"] = (it.get("description") or "")[:200]
                    fa["content"] = (it.get("content") or "")[:400]
                    fa["projected_risk"] = it.get("projected_risk", "")
                elif action_val == "update":
                    upd = it.get("updates") or {}
                    fa["updated_fields"] = sorted(upd.keys()) if isinstance(upd, dict) else []
                    if isinstance(upd, dict) and "content" in upd:
                        fa["content"] = (upd.get("content") or "")[:400]
                    fa["projected_risk"] = it.get("projected_risk", "")
                    fa["current_risk"] = it.get("current_risk", "")
                elif action_val in ("enable", "disable"):
                    pass  # name + action is the auditable surface
            elif name == "watch":
                it["func_args"] = {
                    "action": it.get("action", ""),
                    "command": it.get("command", ""),
                    "name": it.get("watch_name", ""),
                }
            elif name == "notify":
                it["func_args"] = {"message": (it.get("message") or "")[:200]}
            elif name == "task_agent":
                # Pending items reach this point already shaped by
                # ``_prepare_task``, so ``it["skill"]`` is the resolved
                # skill_data dict (or ``None``), not the raw string the
                # LLM passed.  Mirror the ``spawn_workstream`` projection
                # so heuristic ``arg_pattern`` rules can match on skill
                # name and the judge / audit row sees which persona was
                # selected.
                skill_dict = it.get("skill") or {}
                it["func_args"] = {
                    "prompt": (it.get("prompt") or "")[:200],
                    "skill": skill_dict.get("name", "") if isinstance(skill_dict, dict) else "",
                }
            # Coordinator tool args — only the ``needs_approval=True`` set
            # reaches this point (read-only inspect / list_* / wait
            # tools are filtered above), so this matches the auditable
            # surface 1:1. Free-form fields capped to keep the verdict
            # row size bounded.
            elif name == "spawn_workstream":
                it["func_args"] = {
                    "skill": it.get("skill", ""),
                    "initial_message": (it.get("initial_message") or "")[:200],
                    "target_node": it.get("target_node", ""),
                    "name": it.get("name", ""),
                    "model": it.get("model", ""),
                }
            elif name == "spawn_batch":
                # Project every child so the judge sees the full fan-out.
                # First-child-only projection (the prior shape) hid a
                # malicious mid-batch entry from both heuristic and LLM
                # tiers. Tool schema caps ``children`` at 10, so worst
                # case is ~3 KiB of JSON in the verdict row — comparable
                # to the existing ``reasoning`` / ``evidence`` fields.
                # ``name`` (cosmetic) and ``model`` (registry alias)
                # skipped to keep the payload lean; risk-relevant fields
                # are skill, initial_message, target_node.
                children = it.get("children") or []
                it["func_args"] = {
                    "child_count": len(children),
                    "children": [
                        {
                            "skill": c.get("skill", "") if isinstance(c, dict) else "",
                            "initial_message": (
                                (c.get("initial_message") or "")[:200]
                                if isinstance(c, dict)
                                else ""
                            ),
                            "target_node": (
                                c.get("target_node", "") if isinstance(c, dict) else ""
                            ),
                        }
                        for c in children
                    ],
                }
            elif name == "send_to_workstream":
                it["func_args"] = {
                    "ws_id": it.get("ws_id", ""),
                    "message": (it.get("message") or "")[:200],
                }
            elif name == "close_workstream":
                it["func_args"] = {
                    "ws_id": it.get("ws_id", ""),
                    "reason": (it.get("reason") or "")[:200],
                }
            elif name == "close_all_children":
                it["func_args"] = {"reason": (it.get("reason") or "")[:200]}
            elif name in ("cancel_workstream", "delete_workstream"):
                it["func_args"] = {"ws_id": it.get("ws_id", "")}
            elif name == "tasks":
                # ``title`` is optional on update — _prepare_tasks stores
                # ``None`` when omitted, so dict.get(x, "") returns None
                # (not the default) and slicing crashes.  Collapse via
                # ``or ""`` so absent and explicit-None both fall back to
                # empty string.  The other projections above use the same
                # pattern defensively — a future preparer that stores
                # None for any of these fields shouldn't take down the
                # whole batch (every sibling tool call gets reported as
                # cancelled) on a single missing optional field.
                it["func_args"] = {
                    "action": it.get("action", ""),
                    "task_id": it.get("task_id", ""),
                    "title": (it.get("title") or "")[:100],
                }
            elif it.get("mcp_args"):
                it["func_args"] = it["mcp_args"]

        # Publish this judge generation as the session's current cancel event
        # BEFORE spawning the daemon, so the callback can detect when a later
        # turn has superseded it (this turn always runs before its own
        # approve_tools, so the assignment is in place before any verdict can
        # land).  ``_execute_tools`` re-asserts the same value and handles the
        # judge-disabled (None) case.
        cancel_event = threading.Event()
        self._judge_cancel_event = cancel_event

        def _on_verdict(verdict: object) -> None:
            """Callback from the daemon judge thread.

            Withhold the verdict from the live surfaces when a newer turn has
            replaced this judge generation.  With ``cancel_on_approval=False``
            (the default) the prior turn's daemon runs to completion and would
            otherwise write a stale verdict — keyed only by ``call_id`` — into
            the freshly-reset ``_llm_verdicts`` cache; a model that reuses a
            ``call_id`` across turns could then ride that stale ``approve`` to
            a wrongful Smart Approval of a *different* call.  Identity-
            comparing the live generation closes that without affecting
            same-turn late delivery (``cancel_on_approval=False`` still
            streams this turn's verdicts, since the session event still
            points at this ``cancel_event``).

            Superseded verdicts still reach the audit table via the UI's
            ``on_superseded_intent_verdict`` (persist-only, duck-typed —
            display-only UIs like the CLI don't define it and skip straight
            to the drop).  Without that, every judge ruling that landed after
            the next turn began left ``intent_verdicts`` claiming the judge
            never answered.
            """
            if self._judge_cancel_event is not cancel_event:
                persist_only = getattr(self.ui, "on_superseded_intent_verdict", None)
                if persist_only is not None:
                    try:
                        persist_only(verdict.to_dict())  # type: ignore[attr-defined]
                    except Exception:
                        log.debug("judge.superseded_verdict_persist_failed", exc_info=True)
                return
            try:
                self.ui.on_intent_verdict(verdict.to_dict())  # type: ignore[attr-defined]
            except Exception:
                log.debug("judge.verdict_delivery_failed", exc_info=True)

        heuristic_verdicts = judge.evaluate(
            pending,
            dicts_from_turns(self.messages),  # snapshot — daemon thread must not see mutations
            callback=_on_verdict,
            cancel_event=cancel_event,
        )

        # Attach heuristic verdicts to items for the approval UI
        for item, verdict in zip(pending, heuristic_verdicts, strict=True):
            item["_heuristic_verdict"] = verdict.to_dict()

        return cancel_event

    def _evaluate_output(
        self,
        call_id: str,
        output: str,
        func_name: str,
        *,
        tool_args: str = "",
    ) -> tuple[str, OutputAssessment | None]:
        """Run the output guard on tool result text.

        Two stages.  The heuristic regex stage always runs; the LLM judge
        (issue #560 mitigation #1) runs when ``judge.output_guard_llm``
        is enabled.  When both run and the LLM succeeds, the LLM verdict
        is the *acted* assessment (informs redaction + UI + return);
        otherwise the heuristic stands.  Both tier rows are persisted
        whenever the LLM ran, for audit completeness.

        ``tool_args`` is the JSON-string args the tool was called with
        — passed through to the LLM judge so it can reason about
        output-vs-request plausibility (e.g. ``read_file("/etc/passwd")``
        returning password-shaped content is plausible; ``read_file
        ("README.md")`` returning the same is suspicious).  Empty for
        agent-synthesis call sites where no tool call exists.

        Returns ``(possibly_sanitized_output, acted_assessment)``.  The
        acted assessment is ``None`` when its risk_level is ``"none"``.
        """
        from turnstone.core.output_guard import (
            OutputAssessment,
            evaluate_output,
            merge_guard_display_payload,
        )

        og_patterns = None
        rule_reg = self._rule_registry
        if rule_reg is not None:
            og_patterns = rule_reg.output_patterns
        jc = self._judge_cfg
        budget = jc.output_guard_budget_seconds if jc is not None else 30.0
        heuristic = evaluate_output(
            output,
            func_name=func_name,
            call_id=call_id,
            budget_seconds=budget,
            patterns=og_patterns,
            trusted_marker_nonce=self._envelope_nonce,
        )

        # Stage 2: LLM judge (opt-in, capability-gated).  The rate limiter
        # bounds adversarial fan-out cost (60 calls/min/session).  The
        # judge sees the heuristic verdict + tool args so it can defer to
        # the regex on credential_leak and focus on prompt-injection
        # signals the regex set misses.  On disable / rate-limit / error /
        # timeout the heuristic stands.
        tool_description = self._lookup_tool_description(func_name) if func_name else ""
        llm_verdict = self._invoke_output_guard_judge(
            call_id,
            output,
            func_name,
            tool_description=tool_description,
            tool_args=tool_args,
            heuristic_risk=heuristic.risk_level,
            heuristic_flags=tuple(heuristic.flags),
            heuristic_annotations=tuple(heuristic.annotations),
        )

        output_len = len(output)

        # Merge the two detectors into one acted finding (issue #560,
        # "show, annotated").  Risk = max(heuristic, llm); flags = union.
        # An LLM "none" — or a failed/absent LLM — never LOWERS a heuristic
        # positive: the judge evaluates adversarial tool output, so it may
        # escalate but must not be able to hide a deterministic regex
        # finding.  Credential redaction stays a heuristic-only signal the
        # LLM cannot override (bug-1 / sec-1): a secret is redacted whether
        # or not the judge sees injection.
        # ``llm`` is the narrowed, succeeded-only verdict (None on
        # disable / rate-limit / error / timeout) — lets the type checker
        # follow attribute access below without re-asserting succeeded.
        llm = llm_verdict if (llm_verdict is not None and llm_verdict.succeeded) else None
        wants_redaction = heuristic.sanitized is not None and jc is not None and jc.redact_secrets

        # Persistence — one row per (call_id, tier).  Heuristic row when it
        # has signal; the LLM row carries the judge's OWN verdict on success
        # so the replay merge can recombine the two.  A failed judge is
        # recorded under tier="llm_error" (NOT "llm") so audit can tell
        # "attempted but failed" from "never enabled" AND the replay merge
        # treats it as absent — a risk="none" failure row must never shadow
        # a real heuristic finding on reconnect (the vanishing-chip bug).
        heuristic_has_signal = heuristic.risk_level != "none" or bool(heuristic.flags)
        verdicts_disagree = llm is not None and (
            llm.risk_level != heuristic.risk_level or set(llm.flags) != set(heuristic.flags)
        )
        if heuristic_has_signal or verdicts_disagree:
            self._record_output_tier(call_id, func_name, output_len, heuristic, tier="heuristic")
        if llm_verdict is not None:
            if llm_verdict.succeeded:
                self._record_output_tier(
                    call_id,
                    func_name,
                    output_len,
                    OutputAssessment(
                        flags=list(llm_verdict.flags),
                        risk_level=llm_verdict.risk_level,
                        # Reasoning rides the dedicated ``reasoning`` column
                        # below; keep ``annotations`` heuristic-only so audit
                        # consumers don't see the judge prose duplicated here.
                        annotations=[],
                    ),
                    tier="llm",
                    reasoning=llm_verdict.reasoning,
                    judge_model=llm_verdict.judge_model,
                    latency_ms=llm_verdict.latency_ms,
                    confidence=llm_verdict.confidence,
                )
            else:
                # Failure row — empty assessment + error reason for audit,
                # under the distinct "llm_error" tier (see comment above).
                self._record_output_tier(
                    call_id,
                    func_name,
                    output_len,
                    OutputAssessment(risk_level="none"),
                    tier="llm_error",
                    reasoning=llm_verdict.error,
                    judge_model=llm_verdict.judge_model,
                    latency_ms=llm_verdict.latency_ms,
                )

        # Chip payload — built through the SAME merge the replay path uses
        # (build_merged_output_assessment_payload) so the live SSE chip and
        # the reconnect chip render identically.  ``None`` means nothing to
        # show (merged risk "none", no redaction).
        d = merge_guard_display_payload(
            heuristic_risk=heuristic.risk_level,
            heuristic_flags=list(heuristic.flags),
            heuristic_annotations=list(heuristic.annotations),
            redacted=wants_redaction,
            llm_succeeded=llm is not None,
            llm_risk=llm.risk_level if llm else "none",
            llm_flags=list(llm.flags) if llm else [],
            llm_reasoning=llm.reasoning if llm else "",
            llm_confidence=llm.confidence if llm else 0.0,
            llm_model=llm.judge_model if llm else "",
        )
        if d is None:
            return output, None

        # Context-facing annotations (what the MODEL sees via the
        # output_guard operator-context system turn):
        # the heuristic findings, plus the LLM's reasoning ONLY when the LLM
        # ESCALATED (flagged something itself).  We deliberately never inject
        # the judge's "benign" reasoning into the model context — a judge
        # fooled into "none" on a real heuristic finding must not get to tell
        # the model the output is safe.  The operator UI still shows the full
        # LLM verdict via the chip payload below.
        context_annotations = list(heuristic.annotations)
        if llm is not None and llm.risk_level != "none" and llm.reasoning:
            context_annotations.append(llm.reasoning)
        # acted's risk/flags come straight from the merge payload so the
        # context advisory can't drift from the chip.
        acted = OutputAssessment(
            flags=list(d["flags"]),
            risk_level=str(d["risk_level"]),
            annotations=context_annotations,
            sanitized=heuristic.sanitized,
        )

        d["func_name"] = func_name
        d["output_length"] = output_len
        log.debug(
            "output_guard.flagged",
            call_id=call_id,
            func_name=func_name,
            risk=d["risk_level"],
            flags=d["flags"],
            tier=d["tier"],
            redacted=wants_redaction,
        )
        try:
            self.ui.on_output_warning(call_id, d)
        except Exception:
            log.debug("output_guard.callback_failed", exc_info=True)

        if wants_redaction:
            # heuristic.sanitized is guaranteed non-None inside this branch
            # (wants_redaction's first clause), narrow for the type checker.
            sanitized = heuristic.sanitized
            assert sanitized is not None
            return sanitized, acted
        return output, acted

    def _batch_evaluate_outputs(
        self,
        items: list[tuple[str, str, str, str]],
    ) -> dict[str, tuple[str, OutputAssessment | None]]:
        """Run ``_evaluate_output`` for each ``(call_id, output, func_name,
        tool_args)`` 4-tuple concurrently, return a dict keyed by
        ``call_id`` (perf-2).

        Bounded thread pool (4 workers) — high enough to parallelise the
        common 5-20 tool-calls-per-turn case, low enough to avoid blowing
        the provider's rate limit.  The per-call LLM timeout enforced
        inside ``OutputGuardJudge.evaluate`` bounds the worst case.

        Failures inside a worker are wrapped so the dict always contains
        an entry — the caller can fall back to the sequential path if
        an entry is missing.
        """
        out: dict[str, tuple[str, OutputAssessment | None]] = {}
        if not items:
            return out
        max_workers = min(4, len(items))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="output-guard-batch",
        ) as ex:
            futures = {
                ex.submit(
                    self._evaluate_output, tc_id, output, func_name, tool_args=tool_args
                ): tc_id
                for tc_id, output, func_name, tool_args in items
            }
            for fut in concurrent.futures.as_completed(futures):
                tc_id = futures[fut]
                try:
                    out[tc_id] = fut.result()
                except Exception:
                    log.warning("output_guard.batch_eval_failed", tc_id=tc_id, exc_info=True)
        return out

    def _invoke_output_guard_judge(
        self,
        call_id: str,
        output: str,
        func_name: str,
        *,
        tool_description: str = "",
        tool_args: str = "",
        heuristic_risk: str = "none",
        heuristic_flags: tuple[str, ...] = (),
        heuristic_annotations: tuple[str, ...] = (),
    ) -> OutputJudgeVerdict | None:
        """Run the LLM judge with per-session rate limiting.

        Returns the verdict (success or failure flavour) when the LLM
        stage ran, or ``None`` when LLM was disabled / rate-limited /
        the call itself raised.  The TokenBucket caps adversarial
        fan-out at 60 calls/minute per session.

        Framing context (tool description, args, heuristic verdict +
        annotations) is forwarded to the judge so its user-message
        prompt can carry the full signal.
        """
        llm_judge = self._ensure_output_guard_judge()
        if llm_judge is None:
            return None
        if not self._output_guard_judge_rl.consume():
            log.info(
                "output_guard_judge.rate_limited",
                call_id=call_id,
                func_name=func_name,
            )
            return None
        try:
            return llm_judge.evaluate(
                output,
                func_name=func_name,
                call_id=call_id,
                tool_description=tool_description,
                tool_args=tool_args,
                heuristic_risk=heuristic_risk,
                heuristic_flags=heuristic_flags,
                heuristic_annotations=heuristic_annotations,
                cancel_event=self._output_guard_judge_cancel,
            )
        except Exception:
            log.warning("output_guard_judge.evaluate_raised", exc_info=True)
            return None

    def _record_output_tier(
        self,
        call_id: str,
        func_name: str,
        output_length: int,
        assessment: OutputAssessment,
        *,
        tier: str,
        reasoning: str = "",
        judge_model: str = "",
        latency_ms: int = 0,
        confidence: float = 0.0,
    ) -> None:
        """Persist one ``(call_id, tier)`` row via the UI's storage hook."""
        try:
            d = assessment.to_dict()
            d["func_name"] = func_name
            d["output_length"] = output_length
            d["redacted"] = assessment.sanitized is not None
            self.ui.record_output_assessment(
                call_id,
                d,
                tier=tier,
                reasoning=reasoning,
                judge_model=judge_model,
                latency_ms=latency_ms,
                confidence=confidence,
            )
        except Exception:
            log.debug("output_guard.record_failed", exc_info=True)

    def _guard_subagent_synthesis(self, content: str, label: str) -> str:
        """Run output_guard on a sub-agent's final synthesis text.

        Sub-agent intermediate tool results are guarded inside ``_run_agent``,
        but the synthesized response a sub-agent returns to its parent is
        another laundering surface — the sub-agent may quote or paraphrase
        a domain-camouflaged injection from one of its tool results (see
        issue #560).  Guarding the synthesis here gives us a dedicated
        scan point at the sub-agent boundary; the parent's tool-result
        loop runs the same guard a second time when the synthesis lands
        as that call's result — accepted defense-in-depth duplication
        (~10-40ms per 5-16KB synthesis, bounded by ``output_guard_budget_seconds``).
        """
        jc = self._judge_cfg
        if jc is None or not jc.output_guard:
            return content
        if not isinstance(content, str):
            return content
        synth_id = f"agent_synth_{label}_{uuid.uuid4().hex[:8]}"
        guarded, _ = self._evaluate_output(synth_id, content, f"{label}_agent_synthesis")
        return guarded

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
        Raises ``queue.Full`` if the queue is saturated.  Raises
        :class:`AttachmentsNotQueueableError` when ``attachment_ids`` is
        non-empty: attachments cannot ride the advisory seam (which is
        text-only), and appending them as a separate user turn would
        violate strict-provider role-ordering rules.  Callers surface
        this rejection to the user — interactive UIs typically wait for
        the current turn to finish before allowing an attached send.

        ``queue_msg_id`` lets the caller supply the id (so it matches the
        ``send_id`` tracking token threaded through the send) — when
        omitted, an id is generated.
        """
        from turnstone.core.tool_advisory import parse_priority

        if attachment_ids:
            raise AttachmentsNotQueueableError(
                "Cannot queue a message with attachments — wait for the "
                "current turn to finish before sending an attachment."
            )

        cleaned, priority = parse_priority(text)
        # Cap individual message length to prevent context bloat
        if len(cleaned) > 2000:
            cleaned = cleaned[:2000] + "..."
        # Full UUID hex (128 bits) rather than a truncated prefix — this id is
        # the ``send_id`` tracking token threaded through the turn, so the wide
        # space keeps the birthday bound comfortable.
        msg_id = queue_msg_id or uuid.uuid4().hex
        with self._queued_lock:
            if len(self._queued_messages) >= self._QUEUE_MAX:
                raise queue.Full()
            self._queued_messages[msg_id] = (cleaned, priority)
        return cleaned, priority, msg_id

    def dequeue_message(self, msg_id: str) -> bool:
        """Remove a queued message by ID.  Returns True if removed."""
        with self._queued_lock:
            popped = self._queued_messages.pop(msg_id, None)
        return popped is not None

    def _flush_queued_messages(self, prefix: str = "") -> bool:
        """Drain queued messages into a single combined user turn.

        Queued items are always text-only (attachments are rejected at
        ``queue_message`` time — see :class:`AttachmentsNotQueueableError`),
        so a single combined turn avoids back-to-back user messages
        that some models handle poorly.

        ``prefix`` (when non-empty) is the ``user_feedback`` string from
        the post-tool-batch path: text the user typed alongside an
        approval prompt (e.g. "y, use full path").  Folding it into
        the same flush keeps the role sequence to exactly one trailing
        user row whether feedback alone, queued items alone, or both
        are present.  When both are present the rendered content is
        ``prefix + "\\n\\n" + "\\n\\n".join(parts)``.

        Returns ``True`` when any user row was appended (prefix or
        items), ``False`` when both were empty.
        """
        from turnstone.core.tool_advisory import PRIORITY_IMPORTANT

        with self._queued_lock:
            items = list(self._queued_messages.values())
            self._queued_messages.clear()
        if not items and not prefix:
            return False

        parts = [f"[IMPORTANT] {msg}" if pri == PRIORITY_IMPORTANT else msg for msg, pri in items]
        if prefix and parts:
            content = prefix + "\n\n" + "\n\n".join(parts)
        elif prefix:
            content = prefix
        else:
            content = "\n\n".join(parts)
        self._append_user_turn(content, ())
        return True

    def _collect_advisories(
        self,
        assessment: OutputAssessment | None,
        func_name: str,
        is_last_in_batch: bool,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Gather operator context for a tool result as system-turn specs.

        Returns a list of ``(source, content, meta)`` tuples — one per
        operator-context item that should be appended as a first-class
        ``{"role": "system"}`` turn AFTER the tool message (the caller
        feeds each through :meth:`_append_system_turn`):

        - **Output guard** findings (``source="output_guard"``) — rendered
          inline here (flags + risk level + annotations + the
          redaction notice).  Attach per-result.
        - **Queued user messages** (Seam 1, ``source="user_interjection"``)
          — drained on the LAST result of a batch.  ``meta`` carries the
          ``priority`` so the UI can frame important interjections
          distinctly.  Cancel / exception / no-tool-call paths drain the
          queue as a real user row instead (Seams 2 and 3).
        - **Metacognitive tool-channel nudges** (``tool_error`` / ``repeat``)
          and any-channel nudges (``watch_triggered`` / ``idle_children``)
          — drained on the last result.  ``meta`` carries the producer's
          optional fields (e.g. ``watch_triggered``'s ``watch_name``).

        Empty when no advisories apply (common case).  Guard findings
        attach per-result; queued messages and metacognitive nudges drain
        on the last result only — no duplication across N tool results.
        """
        specs: list[tuple[str, str, dict[str, Any]]] = []

        # Output guard advisory.  The structured finding is the source of
        # truth: build ``meta`` (flags / risk / annotations / redaction), then
        # derive the wire/UI text ``content`` from it via
        # ``render_output_guard_text`` so the prose the model reads and the FE
        # guard-finding card cannot drift.  Mirrors the legacy GuardAdvisory.
        if assessment is not None:
            guard_meta: dict[str, Any] = {
                "flags": list(assessment.flags),
                "risk_level": assessment.risk_level,
                "annotations": list(assessment.annotations),
                "redacted": assessment.sanitized is not None,
            }
            specs.append(("output_guard", render_output_guard_text(guard_meta), guard_meta))

        # Last-result-in-batch drain seams: queued user messages (Seam 1)
        # and tool/any-channel metacog nudges.  Both fire once per batch so a
        # parallel fan-out doesn't paint the same advisory N times.
        if is_last_in_batch:
            with self._queued_lock:
                queued_items = list(self._queued_messages.values())
                self._queued_messages.clear()
            for text, priority in queued_items:
                # Drop empty/whitespace interjections (e.g. a bare "!!!" whose
                # priority prefix ``parse_priority`` strips to "") — an empty
                # operator turn would fold to an empty fence / paint a blank
                # bubble.  Frame the rest as the user's words so the turn keeps
                # user (not operator) authority, including on the native path
                # where it enters as a real role=system message.
                if not text.strip():
                    continue
                # ``framed`` (preamble + "User message: …") is the model-facing
                # content — it keeps the user's authority framing on the wire.
                # The structured meta carries the user's RAW words + priority so
                # the FE renders a clean "queued message" bubble (the operator
                # reads the message, not the model-directed preamble) with
                # priority emphasis.  Both derive from ``(text, priority)``.
                framed = render_user_interjection(text, priority)
                specs.append(("user_interjection", framed, {"priority": priority, "message": text}))

            # Metacognitive tool-channel drain.  Queued by
            # ``_queue_tool_advisory`` from the tool_error / repeat
            # detection paths just before this loop.
            for nt, text, meta in self._nudge_queue.drain(TOOL_DRAIN):
                specs.append((nt, text, dict(meta) if meta else {}))

        return specs

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

        Per-call exception isolation: a buggy preparer or runtime
        failure is converted into an error tool_result for THAT call
        only — sibling calls in a parallel batch keep running.  The
        prior shape let a single ``_prepare_tool`` raise propagate out
        of the list comprehension and abort the whole batch, leaving
        the assistant's ``tool_calls`` orphaned (no matching
        tool_results, conversation invalid for the next turn).
        """
        # Phase 1: prepare all tool calls.  Each preparer call is
        # individually shielded so a single failure (buggy preparer,
        # MCP server in a weird state, etc.) becomes an error item
        # for that call only — the other parallel siblings keep
        # going.  Without the shield, the list comprehension would
        # propagate, _execute_tools would raise to send()'s except
        # clause, and EVERY tool_call in the batch would lose its
        # result entry — the conversation would then be invalid on
        # the next turn (assistant tool_calls with no matching tool
        # results).  See the docstring on this method.
        items = [self._safe_prepare_tool(tc) for tc in tool_calls]

        # Reject the read+write mix on ``tasks`` within a single
        # parallel batch.  ``tasks`` mutates an ordered planning
        # list and supports a ``list`` read; a batch like
        # ``[tasks(add=...), tasks(list)]`` has unspecified
        # ordering inside ``_execute_tools.run_one``'s
        # ThreadPoolExecutor — the read can land before or after
        # the write and produce inconsistent state to the model.
        #
        # All-write and all-read batches are SAFE:
        #   - Writes serialise under the per-ws lock in
        #     ``CoordinatorClient.tasks_*``, AND a batch containing
        #     any ``tasks`` write runs serially in input order (see
        #     the run-loop branch below) so the final task list
        #     ordering matches what the model emitted, not the
        #     scheduler's acquisition order.
        #   - Reads can't race against anything.
        #
        # The rule below only fires on the MIX, so the natural
        # batch shapes ("add four tasks at once", "list nodes +
        # list skills + tasks(list) for a planning snapshot") are
        # both permitted; only the genuinely-broken shape gets
        # rejected.  Non-tasks siblings paralleled with tasks()
        # are unaffected — they don't touch the tasks state.
        if len(items) > 1:
            tasks_items = [
                it
                for it in items
                if it.get("func_name") == "tasks" and not it.get("error") and not it.get("denied")
            ]
            if tasks_items:
                has_read = any(it.get("action") in _TASKS_READ_ACTIONS for it in tasks_items)
                has_write = any(it.get("action") in _TASKS_WRITE_ACTIONS for it in tasks_items)
                if has_read and has_write:
                    for it in tasks_items:
                        it["error"] = (
                            "Error: tasks(...) read (`list`) and write "
                            "(`add` / `update` / `remove` / `reorder`) actions "
                            "cannot run in the same parallel tool batch — the "
                            "read-after-write ordering is not guaranteed. "
                            "All-reads or all-writes are fine; mix only by "
                            "splitting them across separate assistant turns."
                        )
                        it["needs_approval"] = False

        # Intent validation (advisory, non-blocking).
        # Cancel any prior judge thread before spawning a new one.
        if self._judge_cancel_event is not None:
            self._judge_cancel_event.set()
        judge_cancel = self._evaluate_intent(items)
        self._judge_cancel_event = judge_cancel  # track for close()

        # Push the live Smart Approvals config onto the UI just before the
        # gate so a hot-reloaded ``judge.*`` change takes effect on this
        # batch.  Only SessionUIBase carries the smart-approval gate (the
        # CLI / eval UIs have their own ``approve_tools``); the isinstance
        # check both skips those and narrows the type for the attribute
        # writes.  ``approve_tools`` acts on these only when the judge is
        # enabled AND ``judge.smart_approvals`` is on, so the feature stays
        # inert (human-gated, as today) unless explicitly turned on.
        from turnstone.core.session_ui_base import SessionUIBase

        if isinstance(self.ui, SessionUIBase):
            jc = self._judge_cfg
            self.ui.smart_approvals_enabled = bool(jc and jc.enabled and jc.smart_approvals)
            if jc is not None:
                self.ui.smart_approval_threshold = jc.confidence_threshold
                self.ui.smart_approval_wait_seconds = jc.timeout

        # Phase 2: approve via UI
        self._emit_state("attention")
        try:
            approved, user_feedback = self.ui.approve_tools(items)
        finally:
            # Gate resolution fires the judge's abort signal only when the
            # operator opted in: with ``judge.cancel_on_approval`` the daemon
            # stops spending inference the moment a decision lands (remaining
            # items degrade to ``llm_fallback`` verdicts, heuristic
            # content relabeled).  With the
            # default False the daemon runs every item to completion — the
            # contract the setting's help text promises — and late verdicts
            # stream + persist through ``_on_verdict``.  An unconditional
            # set here used to defeat that: ``_evaluate_single`` polls the
            # event regardless of config, so every undone item silently
            # became a fallback row the instant the gate resolved.  A stale
            # daemon is still bounded to one batch of real work by the
            # unconditional supersede-set at the top of the next batch and
            # by ``close()``.
            jc_live = self._judge_cfg
            if judge_cancel and jc_live and jc_live.cancel_on_approval:
                judge_cancel.set()
        self._emit_state("running")
        if not approved:
            # Mark all pending items as denied
            for item in items:
                if item.get("needs_approval") and not item.get("error"):
                    item["denied"] = True
                    if not item.get("denial_msg"):
                        # approve_tools already stamps the specific reason (the
                        # matched policy pattern, or the operator's feedback) on
                        # a denied item; only fill the flat default when it left
                        # denial_msg unset — never clobber the specific reason
                        # (mirrors the sub-agent loop's guard).
                        item["denial_msg"] = (
                            f"Denied by user: {user_feedback}"
                            if user_feedback
                            else "Denied by user"
                        )
            user_feedback = None  # feedback is in the denial_msg
            if self._mem_cfg.nudges and should_nudge(
                "denial",
                self._metacog_state,
                message_count=len(self.messages),
                memory_count=self._visible_memory_count(),
                cooldown_secs=self._mem_cfg.nudge_cooldown,
            ):
                self._queue_user_advisory("denial", format_nudge("denial"))

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
                msg = item.get("denial_msg", "Denied by user")
                self._report_tool_result(
                    item["call_id"],
                    item.get("func_name", "unknown"),
                    msg,
                    is_error=True,
                )
                return item["call_id"], msg
            try:
                result: tuple[str, str | list[dict[str, Any]]] = item["execute"](item)
                return result
            except (KeyboardInterrupt, GenerationCancelled):
                raise
            except Exception as e:
                from turnstone.core.memory import sanitize_error_text

                func = item.get("func_name", "unknown")
                # Include the exception class so triage doesn't have
                # to guess (RateLimitError vs. TimeoutError vs. a
                # tool-policy reject vs. a real bug all look very
                # different to a coord trying to recover).  Append a
                # short hint that the failure is local — sibling
                # tool calls in this parallel batch returned their
                # own results, the model can adapt instead of
                # treating this as a session-wide failure.
                #
                # Redact before formatting both the log and the
                # model-facing result: tool exceptions can carry
                # credentials in their str() (HTTP error bodies, env
                # values, etc.) and the same pattern set as the
                # fatal-error path applies.
                safe_exc_text = sanitize_error_text(f"{type(e).__name__}: {e}")
                msg = (
                    f"Error executing {func}: {safe_exc_text}\n"
                    f"This tool raised an unexpected exception. "
                    f"Sibling tool calls in this batch (if any) "
                    f"completed independently. You can retry with "
                    f"adjusted arguments or try a different approach."
                )
                log.warning("tool_exec.failed", tool=func, error=safe_exc_text, exc_info=True)
                self._report_tool_result(item["call_id"], func, msg, is_error=True)
                return item["call_id"], msg

        if len(items) == 1:
            results = [run_one(items[0])]
        else:
            # When the batch contains any ``tasks`` write, run every
            # item serially in input order.  ``tasks_add`` appends
            # under a per-ws lock; a parallel ThreadPoolExecutor's
            # scheduler-dependent acquisition order would otherwise
            # produce a final task list whose ordering varies
            # run-to-run, even though the SET of tasks is consistent.
            # The model emitted the writes in a particular order;
            # respecting that is the deterministic shape both
            # operators and the model expect.  Other batches stay
            # parallel — the perf payoff is real and there's no
            # ordering hazard against state outside ``tasks``.
            has_tasks_write = any(
                it.get("func_name") == "tasks" and it.get("action") in _TASKS_WRITE_ACTIONS
                for it in items
            )
            if has_tasks_write:
                results = [run_one(it) for it in items]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(run_one, items))

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

    def _safe_prepare_tool(self, tc: dict[str, Any]) -> dict[str, Any]:
        """Wrap :meth:`_prepare_tool` so a single failing preparer is
        an error item, not a propagating exception.

        ``_prepare_tool`` is the per-call dispatcher into per-tool
        preparers (validation, arg coercion, preview building).  A
        bug in any one of those — KeyError on a missing optional, an
        MCP client raising during ``is_mcp_tool``, anything — would
        otherwise blow up the list comprehension in
        :meth:`_execute_tools` and abort EVERY sibling call in the
        same parallel batch.  Worse, the caught-too-late exception
        leaves the assistant message's ``tool_calls`` orphaned
        (no matching ``tool_result`` rows), which makes the next
        turn invalid for both OpenAI and Anthropic schemas.

        Cancellation semantics: ``KeyboardInterrupt`` /
        ``GenerationCancelled`` re-raise so the cooperative cancel
        path still works (the worker thread observes the cancel and
        synthesizes results for orphaned tool_calls in
        :meth:`_synthesize_cancelled_results`).
        """
        from turnstone.core.memory import sanitize_error_text

        try:
            return self._prepare_tool(tc)
        except (KeyboardInterrupt, GenerationCancelled):
            raise
        except Exception as exc:
            call_id = str(tc.get("id") or f"call_{uuid.uuid4().hex}")
            func_name = ""
            try:
                func_name = str(tc.get("function", {}).get("name", "") or "").strip()
            except Exception:
                func_name = ""
            if not func_name:
                func_name = "unknown"
            # Redact before logging AND before returning.  The raw
            # exception text can carry credentials (e.g. a misconfigured
            # base URL with userinfo, an echoed Bearer token, a
            # connection-string envvar) — both the structured log and
            # the model-facing tool_result must scrub via the same
            # pattern set the audit log + output guard use.
            safe_exc_text = sanitize_error_text(f"{type(exc).__name__}: {exc}")
            log.warning(
                "tool_prepare.failed tool=%s call_id=%s error=%s",
                func_name,
                call_id[:32],
                safe_exc_text,
                exc_info=True,
            )
            return {
                "call_id": call_id,
                "func_name": func_name,
                "header": f"✗ {func_name}: prepare failed",
                "preview": "",
                "needs_approval": False,
                "error": (
                    f"Internal error preparing {func_name}: {safe_exc_text}\n"
                    f"Sibling tool calls in this batch were unaffected. "
                    f"You can retry this tool with adjusted arguments "
                    f"or pick a different approach."
                ),
            }

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

        # Short-circuit revoked tools before preparer dispatch so the
        # model sees an unambiguous "revoked" error rather than a
        # preparer-level validation message.
        if func_name in self._revoked_tools:
            return {
                "call_id": call_id,
                "func_name": func_name,
                "header": f"\u2717 {func_name}: revoked",
                "preview": "",
                "needs_approval": False,
                "error": (
                    f"Tool '{func_name}' has been revoked on this "
                    "coordinator session by an operator.  The session "
                    "is still live but this tool is no longer "
                    "available — continue with the tools you have."
                ),
            }

        preparers = {
            "bash": self._prepare_bash,
            "read_file": self._prepare_read_file,
            "search": self._prepare_search,
            "diff_file": self._prepare_diff,
            "write_file": self._prepare_write_file,
            "edit_file": self._prepare_edit_file,
            "web_fetch": self._prepare_web_fetch,
            "web_search": self._prepare_web_search,
            "tool_search": self._prepare_tool_search,
            "task_agent": self._prepare_task,
            "memory": self._prepare_memory,
            "recall": self._prepare_recall,
            "notify": self._prepare_notify,
            "watch": self._prepare_watch,
            "read_resource": self._prepare_read_resource,
            "use_prompt": self._prepare_use_prompt,
            # ``skills`` is the unified read+write+activate tool replacing
            # the legacy ``skill`` + ``list_skills`` pair.  Lives in the
            # interactive block (not coordinator-only) because it serves
            # both kinds: per-action gating inside _prepare_skills routes
            # coord vs interactive where the actions differ.
            "skills": self._prepare_skills,
            # Coordinator tools: only reachable when this session was
            # constructed with kind="coordinator" (COORDINATOR_TOOLS set).
            "spawn_workstream": self._prepare_spawn_workstream,
            "spawn_batch": self._prepare_spawn_batch,
            "close_all_children": self._prepare_close_all_children,
            "inspect_workstream": self._prepare_inspect_workstream,
            "send_to_workstream": self._prepare_send_to_workstream,
            "close_workstream": self._prepare_close_workstream,
            "cancel_workstream": self._prepare_cancel_workstream,
            "delete_workstream": self._prepare_delete_workstream,
            "list_workstreams": self._prepare_list_workstreams,
            "list_nodes": self._prepare_list_nodes,
            "tasks": self._prepare_tasks,
            "wait_for_workstream": self._prepare_wait_for_workstream,
        }
        preparer = preparers.get(func_name)
        if not preparer:
            # Check if this is an MCP tool. Pass the effective ``user_id``
            # (acting user on shared workstreams) so per-user pool tools
            # become reachable here — without this kwarg the gate stays
            # static-only and pool dispatch is structurally unreachable
            # from ``ChatSession._prepare_tool`` (RFC §3, invariant 8).
            if self._mcp_client and self._mcp_client.is_mcp_tool(
                func_name, user_id=self._mcp_effective_user_id
            ):
                return self._prepare_mcp_tool(call_id, func_name, args)
            self.ui.on_error(f"Model called unknown tool: {func_name!r}")
            available = list(preparers)
            if self._mcp_client:
                available.extend(
                    sorted(
                        t["function"]["name"]
                        for t in self._mcp_client.get_tools(user_id=self._mcp_effective_user_id)
                    )
                )
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

    @property
    def _current_read_files(self) -> set[str]:
        """The read-tracking set for the current execution context: a task
        agent's own per-run set while one is active (so the 4-wide pool can't
        cross-contaminate the blind-overwrite guard), else the main session's
        ``_read_files``.  See :data:`_active_read_files`."""
        active = _active_read_files.get()
        return active if active is not None else self._read_files

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
        self._current_read_files.add(resolved)
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
        is_overwrite = exists and resolved not in self._current_read_files and not is_append

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

        if resolved not in self._current_read_files:
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
        """Prepare a web search via the configured backend for approval."""
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
                    "Set tools.searxng_url (or $TURNSTONE_SEARXNG_URL) to a SearxNG "
                    "instance, or set tools.web_search_backend to an MCP search tool."
                ),
            }
        try:
            max_results = min(max(int(args.get("max_results") or 5), 1), 20)
        except (ValueError, TypeError):
            max_results = 5
        category = args.get("category", "general") or "general"
        if category not in ("general", "news", "it", "science"):
            category = "general"
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
            "category": category,
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
        """Pull and validate the optional `model` arg for the task agent.

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
        # ``default`` is operator-only — the alias either back-compat-shims
        # a single-CLI-model registry or aliases a hand-named DB row, and
        # in both cases an LLM that explicitly routes here bypasses the
        # operator-configured ``task_alias`` per-role
        # default.  Symmetric with the description filter at
        # ``_render_agent_tool_descriptions`` — closes the loophole where
        # an LLM that learned the alias name out-of-band (training data,
        # prior turn, prompt injection) can re-issue it directly.
        if alias == "default":
            return None, {
                "call_id": call_id,
                "func_name": func_name,
                "header": f"\u2717 {func_name}: 'default' is not selectable",
                "preview": "",
                "needs_approval": False,
                "error": (
                    "Error: 'default' is not a selectable model alias for "
                    f"{func_name}. Omit `model=` to use the operator-configured "
                    "per-role default."
                ),
            }
        if self._registry is None or not self._registry.has_alias(alias):
            # ``default`` excluded from the retry list so an LLM probing
            # with a bogus alias can't enumerate it back from the error.
            if self._registry is None:
                available_str = "(no registry configured)"
            else:
                available = sorted(a for a in self._registry.list_aliases() if a != "default")
                available_str = (
                    ", ".join(available)
                    if available
                    else "(no alternative aliases configured — omit `model=`)"
                )
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
        skill_arg = (args.get("skill") or "").strip()
        skill_data: dict[str, Any] | None = None
        if skill_arg:
            skill_data = get_skill_by_name(skill_arg)
            if skill_data is None:
                return {
                    "call_id": call_id,
                    "func_name": "task_agent",
                    "header": f"\u2717 task_agent: unknown skill '{skill_arg}'",
                    "preview": "",
                    "needs_approval": False,
                    "error": (
                        f"Error: unknown skill '{skill_arg}'. "
                        "Use skills(action='find', query='...') to find available names."
                    ),
                }
            # ``enabled=False`` is an admin's quarantine flag \u2014 mirror the
            # gate that ``_exec_skills_load`` and ``_exec_skills_find``
            # apply so task_agent can't sidestep it.  Distinct from the
            # not-found case so the LLM's recovery path can tell them apart.
            if not skill_data.get("enabled", True):
                return {
                    "call_id": call_id,
                    "func_name": "task_agent",
                    "header": f"\u2717 task_agent: skill '{skill_arg}' is disabled",
                    "preview": "",
                    "needs_approval": False,
                    "error": (
                        f"Error: skill '{skill_arg}' is disabled and cannot be used. "
                        "Use skills(action='find', query='...') to find available names."
                    ),
                }
            # ``get_skill_by_name`` returns the full prompt_templates row
            # (~30 columns including ``scan_report``, ``installed_by``,
            # ``source_url``, etc.).  Project to the minimal field set
            # that ``_exec_task`` and ``_evaluate_intent`` actually read,
            # so the approval item doesn't drag governance metadata
            # through any future audit serializer.
            skill_data = {
                "name": skill_data["name"],
                "content": skill_data["content"],
                "risk_level": skill_data.get("risk_level", ""),
            }
        preview_text = prompt[:300] + ("..." if len(prompt) > 300 else "")
        header = "\u2699 task_agent (autonomous agent"
        if skill_data:
            header += f", skill: {skill_data['name']}"
            # Surface high/critical risk at approval time so the operator
            # sees the same signal ``_load_skills`` emits for session-level
            # skills (session.py:1336).  Log mirrors that path's structured
            # event for forensic continuity.
            risk_tier = skill_data.get("risk_level", "")
            if risk_tier in ("high", "critical"):
                header += f", risk: {risk_tier}"
                log.warning(
                    "task_agent.high_risk_skill",
                    skill=skill_data["name"],
                    risk_level=risk_tier,
                )
        header += ")"
        return {
            "call_id": call_id,
            "func_name": "task_agent",
            "header": header,
            "preview": f"    {preview_text}",
            "needs_approval": True,
            "approval_label": "task_agent",
            "execute": self._exec_task,
            "prompt": prompt,
            "model_override": model_override,
            "skill": skill_data,
        }

    def _resolve_scope_id(self, scope: str) -> str:
        """Map a scope name to its scope_id.

        ``coordinator`` is COORDINATOR-only \u2014 the coord can save and
        read memories in its own private namespace, but its child
        interactive workstreams cannot see or write the row.  This
        closes the cross-session prompt-injection lane that an
        adversarially-steered child would otherwise have through the
        coord's system message: the coord's children consume external
        content (MCP tool output, attachments) which can be steered to
        plant instructions, and the new scope must not become a
        delivery channel back into the parent's prompt.

        The containment gate is the session KIND (children are always
        INTERACTIVE \u2014 :meth:`_validate_scope` rejects them before this
        resolver runs), not secrecy of the scope_id value.  That is
        what lets the coord scope key on the durable ``user_id``
        (shared with children, visible cluster-wide as display
        metadata) without widening the write surface: no lane \u2014 memory
        tool or REST (``_VALID_MEMORY_SCOPES`` in ``server.py`` omits
        ``coordinator``) \u2014 accepts a caller-supplied coordinator
        scope_id.
        """
        if scope == "workstream":
            return self._ws_id
        if scope == "user":
            return self._user_id
        if scope == "coordinator":
            return self._coordinator_scope_id()
        if scope == "project":
            return self._project_id
        return ""

    def _coordinator_scope_id(self) -> str:
        """Return the user_id anchoring the ``coordinator`` memory scope, or ``""``.

        Only a coordinator session has a coord scope \u2014 returns
        ``self._user_id`` for ``kind == COORDINATOR``, ``""`` otherwise.
        Keying on the user (not the ws_id) makes the namespace durable:
        every coordinator session the same user runs shares one
        orchestration memory, so notes survive close/reopen.  Children
        of a coord get an empty scope_id, which :meth:`_validate_scope`
        translates into an explicit reject \u2014 children must use
        ``workstream`` or ``user`` scope for their own memories.

        ``""`` for an unauthenticated coordinator is unreachable in
        practice (``__init__`` refuses to construct one) but kept
        fail-closed: an empty scope_id never resolves to a readable or
        writable namespace.

        See :meth:`_resolve_scope_id`'s docstring for the security
        rationale (cross-session prompt-injection containment).
        """
        if self._kind == WorkstreamKind.COORDINATOR:
            return self._user_id
        return ""

    def _validate_scope(self, scope: str, call_id: str) -> dict[str, Any] | None:
        """Return an error dict if scope is invalid, None if OK.

        Coord sessions are isolated to coord-scope: they reject every
        other scope (``global`` / ``workstream`` / ``user``) so the
        coord's memory namespace stays focused on orchestration and
        doesn't accidentally mutate or read user-context rows.

        Interactive sessions reject ``coordinator`` for the symmetric
        reason \u2014 coord-scope rows belong to a per-user namespace read
        only by that user's COORDINATOR sessions, and an IC writer
        (children share the parent's user_id, so the kind check is the
        gate) could otherwise be a cross-session prompt-injection lane
        into the parent coord's system message.
        """
        if scope == "user" and not self._user_id:
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": "\u2717 memory: user scope requires authentication",
                "preview": "",
                "needs_approval": False,
                "error": "Error: 'user' scope requires authenticated user identity",
            }
        if scope == "project" and not self._project_id:
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": "✗ memory: not attached to an accessible project",
                "preview": "",
                "needs_approval": False,
                "error": (
                    "Error: 'project' scope requires this workstream to be "
                    "attached to a project you can access."
                ),
            }
        if (
            scope == "coordinator"
            and self._kind == WorkstreamKind.COORDINATOR
            and not self._user_id
        ):
            # Backstop for the save lane: search/list reject empty
            # scope_ids in _exec_memory, but save would otherwise write
            # a ("coordinator", "") row shared by every unauthenticated
            # session. Unreachable through real hosts (__init__ refuses
            # COORDINATOR without a user_id); guards test doubles and
            # future hosts. Kind-scoped so non-coordinator callers keep
            # the clearer kind-mismatch error below regardless of their
            # auth state.
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": "\u2717 memory: coordinator scope requires authentication",
                "preview": "",
                "needs_approval": False,
                "error": "Error: 'coordinator' scope requires authenticated user identity",
            }
        if self._kind == WorkstreamKind.COORDINATOR and scope not in (
            "coordinator",
            "project",
        ):
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": f"\u2717 memory: scope '{scope}' unavailable to coordinator",
                "preview": "",
                "needs_approval": False,
                "error": (
                    f"Error: '{scope}' scope is not available to coordinator "
                    "sessions. Coord sessions only see and write the "
                    "'coordinator' scope \u2014 their orchestration namespace is "
                    "isolated from the user's interactive memory. Use "
                    "scope='coordinator' or omit scope (it defaults to "
                    "'coordinator' for coord sessions)."
                ),
            }
        if scope == "coordinator" and self._kind != WorkstreamKind.COORDINATOR:
            return {
                "call_id": call_id,
                "func_name": "memory",
                "header": "\u2717 memory: coordinator scope unavailable",
                "preview": "",
                "needs_approval": False,
                "error": (
                    "Error: 'coordinator' scope is only valid for coordinator "
                    "sessions. This is an interactive workstream \u2014 use "
                    "'workstream' or 'user' scope for context private to this "
                    "session, or ask the parent coordinator to manage shared "
                    "context on your behalf."
                ),
            }
        return None

    def _default_memory_scope(self) -> str:
        """Default ``scope`` for a memory(action='save') with no explicit scope.

        An attached, WRITABLE project wins for both kinds: work done inside a
        project lands in the project bucket by default instead of leaking into
        the kind default (``global`` / ``coordinator``).  The model can still
        target another scope explicitly (a genuine cross-project ``user`` fact,
        say).  A read-only project session keeps the kind default — it can't
        write the project anyway, so defaulting there would only hit the
        write-gate.

        Otherwise: coord sessions default to ``coordinator`` (the only scope
        they can write); interactive sessions default to ``global``.
        """
        if self._project_id and self._project_writable:
            return "project"
        if self._kind == WorkstreamKind.COORDINATOR:
            return "coordinator"
        return "global"

    def _implicit_scope_walk(self) -> tuple[str, ...]:
        """Walk for memory(action='get'/'delete') with no explicit scope.

        Coord sessions only walk ``coordinator`` \u2014 anything else would
        search namespaces the coord can't write to.  Interactive sessions
        keep the narrowest-first walk (workstream \u2192 user \u2192 global); a
        ``coordinator`` step there would always resolve to empty
        scope_id and be a wasted lookup.
        """
        if self._kind == WorkstreamKind.COORDINATOR:
            return ("coordinator",)
        return _IMPLICIT_SCOPE_WALK

    def _visible_memory_count(self) -> int:
        """Count memories visible to this session.

        Coord sessions are isolated to their own coord-scope namespace —
        they don't see global / workstream / user rows.  The orchestration
        role doesn't need user-context memory and pulling those rows in
        would also surface memories from sibling interactive sessions
        (same user, different workstream) into the coord's system
        message, which the coord shouldn't be reasoning over.
        """
        if self._kind == WorkstreamKind.COORDINATOR:
            scope_id = self._coordinator_scope_id()
            n = 0
            if scope_id:
                n += count_structured_memories(scope="coordinator", scope_id=scope_id)
            if self._project_id:
                n += count_structured_memories(scope="project", scope_id=self._project_id)
            return n
        n = count_structured_memories(scope="global")
        n += count_structured_memories(scope="workstream", scope_id=self._ws_id)
        if self._user_id:
            n += count_structured_memories(scope="user", scope_id=self._user_id)
        if self._project_id:
            n += count_structured_memories(scope="project", scope_id=self._project_id)
        return n

    def _visible_scopes(self) -> list[tuple[str, str]]:
        """Return the (scope, scope_id) pairs visible to this session.

        Coord sessions see their coord-scope; interactive sessions see global +
        their workstream + their user (when uid present).  Either kind also sees
        its attached ``project`` scope when the session resolved read access to a
        project at construction.  Drives the single-query visibility helpers.
        """
        if self._kind == WorkstreamKind.COORDINATOR:
            scope_id = self._coordinator_scope_id()
            # Fail-closed on an empty scope_id (unreachable through real hosts —
            # __init__ refuses anonymous coordinators): the storage helpers treat
            # a falsy scope_id as "no scope_id filter" (that's how ``global``
            # works), so a ("coordinator", "") pair would read EVERY user's
            # coordinator rows instead of none.
            coord_scopes: list[tuple[str, str]] = []
            if scope_id:
                coord_scopes.append(("coordinator", scope_id))
            # A coordinator attached to a project also recalls the shared project
            # bucket (read + write), alongside its isolated coordinator scope.
            if self._project_id:
                coord_scopes.append(("project", self._project_id))
            return coord_scopes
        scopes: list[tuple[str, str]] = [("global", ""), ("workstream", self._ws_id)]
        if self._user_id:
            scopes.append(("user", self._user_id))
        if self._project_id:
            scopes.append(("project", self._project_id))
        return scopes

    def _list_visible_memories(self, mem_type: str = "", limit: int = 50) -> list[dict[str, str]]:
        """List memories visible to this session with optional type filter.

        Single SQL round-trip — collapses the prior per-scope fan-out.
        See :meth:`_visible_memory_count` for the coord-isolation rule.
        """
        return list_visible_structured_memories(
            self._visible_scopes(), mem_type=mem_type, limit=limit
        )

    def _search_visible_memories(
        self, query: str, mem_type: str = "", limit: int = 20
    ) -> list[dict[str, str]]:
        """Search memories visible to this session (scope-filtered).

        Single SQL round-trip with a per-turn cache: ``_init_system_messages``
        is invoked many times within a turn (state transitions, MCP refresh,
        tool results) and the recent-context query is identical across them.
        Cache is cleared on each new user turn and after memory writes/deletes.
        See :meth:`_visible_memory_count` for the coord-isolation rule.
        """
        cache_key = (query, mem_type, limit)
        cached = self._mem_search_cache.get(cache_key)
        if cached is not None:
            return cached
        rows = search_visible_structured_memories(
            query, self._visible_scopes(), mem_type=mem_type, limit=limit
        )
        self._mem_search_cache[cache_key] = rows
        return rows

    def _invalidate_memory_cache(self) -> None:
        """Drop the per-turn search cache; call on user-turn append + memory writes."""
        self._mem_search_cache.clear()
        self._touched_memory_keys.clear()

    def _select_memory_candidates(self, context: str) -> tuple[list[dict[str, str]], str]:
        """Pick the candidate set fed into BM25 ranking.

        Returns ``(memories, source_label)`` where source is one of:
        ``recency`` (no context, or search returned nothing),
        ``search`` (search saturated the fetch_limit budget alone), or
        ``union`` (search hits ∪ recency, deduped by memory_id).

        Invariant: the candidate pool is always a SUPERSET of the
        recency-only pool the original bug used — recency is fully
        preserved (not truncated) whenever it gets unioned.  Worst
        case the union is 2 × fetch_limit candidates (~100 with
        defaults), which BM25 ranks in pure Python in well under a
        millisecond.  BM25's score>0 cutoff in bm25.py drops anything
        that doesn't match the query, so unranked recency tail items
        cost nothing on irrelevant candidates while saving the
        relevant ones.

        Capping the union at fetch_limit (the prior behavior) would
        evict the recency tail when search added distinct hits — and
        the recency tail is exactly where ancient-but-recently-touched
        memories live, which is the recall the PR sets out to improve.
        """
        fetch_limit = self._mem_cfg.fetch_limit
        if not context:
            return self._list_visible_memories(limit=fetch_limit), "recency"
        search_hits = self._search_visible_memories(context, limit=fetch_limit)
        if len(search_hits) >= fetch_limit:
            return search_hits, "search"
        recency = self._list_visible_memories(limit=fetch_limit)
        seen = {m["memory_id"] for m in search_hits}
        extra = [m for m in recency if m["memory_id"] not in seen]
        if not search_hits:
            return extra, "recency"
        return search_hits + extra, ("union" if extra else "search")

    @staticmethod
    def _memory_keys(rows: list[dict[str, str]]) -> list[tuple[str, str, str]]:
        """Build ``(name, scope, scope_id)`` touch keys from memory rows.

        The storage read helpers return ``SELECT *`` rows, so all three
        columns are present.
        """
        return [(r.get("name", ""), r.get("scope", ""), r.get("scope_id", "")) for r in rows]

    def _touch_injected_memories(self, rows: list[dict[str, str]]) -> None:
        """Touch the memories injected into the system prefix this turn.

        ``_init_system_messages`` recomposes many times per turn; gate on the
        per-turn touched-key set so each surfaced memory is counted at most
        once between user turns.  Best-effort: the facade swallows storage
        errors, so a failed touch never breaks composition.
        """
        fresh = [k for k in self._memory_keys(rows) if k not in self._touched_memory_keys]
        if not fresh:
            return
        self._touched_memory_keys.update(fresh)
        touch_structured_memories(fresh)

    def _touch_read_memories(self, rows: list[dict[str, str]]) -> None:
        """Touch memories returned by an explicit memory-tool read.

        A search/get is a distinct user-driven access each time it runs, so
        these are counted unconditionally (not subject to the composition
        per-turn dedup).  Best-effort via the facade.
        """
        touch_structured_memories(self._memory_keys(rows))

    def _check_metacognitive_nudge(self, user_message: str) -> tuple[str, str] | None:
        """Check if a metacognitive nudge should fire for *user_message*.

        Called *before* the user turn is appended to ``self.messages``,
        so ``msg_count`` counts the about-to-be-appended message — this
        keeps the ``should_nudge('start', ..., message_count=1)`` semantic
        intact (one user message = first turn).

        Returns ``(nudge_type, nudge_text)`` or ``None``.
        """
        # Wake-channel guard: don't re-detect nudges on the synthetic
        # empty input emitted by ``deliver_wake_nudge_from_queue``.
        # Belt-and-braces with the "" → no-match short-circuit in
        # ``detect_correction`` / ``detect_completion`` since future
        # text changes shouldn't be load-bearing for correctness.
        if self._wake_source_tag:
            return None
        if not self._mem_cfg.nudges:
            return None
        mem_count = self._visible_memory_count()
        msg_count = len(self.messages) + 1
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

    def _queue_user_advisory(self, nudge_type: str, text: str) -> None:
        """Queue a metacognitive nudge for the next user turn.

        Drains in ``_emit_pending_user_nudges`` and is appended as a
        first-class ``{"role": "system"}`` turn AFTER the user turn.  Used
        for nudges that respond to user behaviour: ``correction``,
        ``denial``, ``resume``, ``start``, ``completion``.

        No-ops while the session is inside a wake-driven turn
        (``_wake_source_tag`` set) so model behaviour during the wake
        send (e.g. denying a tool the wake suggested, hitting a
        repeat / tool_error) doesn't queue a nudge that would land on
        top of the wake's own turn or on the user's next real
        turn referencing a context the user never saw.
        """
        if self._wake_source_tag:
            return
        self._nudge_queue.enqueue(nudge_type, text, "user")

    def _emit_pending_user_nudges(self) -> None:
        """Drain user-channel nudges from :class:`NudgeQueue` and append each
        as a first-class operator-context ``system`` turn AFTER the user turn.

        Drains entries whose ``channel`` is in ``{"user", "any"}``;
        ``"tool"`` entries stay queued for the next tool-result batch.

        Called by ``send`` immediately after :meth:`_append_user_turn`, so
        the system turns sit after the user turn they advise (uniform attach
        rule).  Each drained nudge becomes one ``{"role": "system",
        "_source": <nudge_type>, ...}`` turn via :meth:`_append_system_turn`
        — the source is the nudge type (``correction`` / ``denial`` /
        ``resume`` / ``start`` / ``completion`` / ``idle_children`` /
        ``watch_triggered``) and any optional metadata (e.g.
        ``watch_triggered``'s ``watch_name``) rides as sibling keys.
        ``_append_system_turn`` persists each row and fires the live
        ``on_system_turn`` SSE hook so reconnecting / multi-tab consumers
        render the same operator bubble.

        When ``deliver_wake_nudge_from_queue`` is the caller, it has already
        drained the queue and stashed the entries on
        ``self._wake_drained_reminders``; we consume that list directly
        rather than draining again (the predicate-aware drain runs once, in
        ``deliver_wake_nudge_from_queue``).
        """
        if self._wake_drained_reminders is not None:
            entries = self._wake_drained_reminders
            self._wake_drained_reminders = None  # consume — only delivered once
        else:
            items = self._nudge_queue.drain(USER_DRAIN)
            entries = []
            for nudge_type, text, meta in items:
                entry: dict[str, Any] = {"type": nudge_type, "text": text}
                if meta:
                    entry.update(meta)
                entries.append(entry)
        for entry in entries:
            source = str(entry.get("type") or "")
            if not source:
                continue
            meta = {k: v for k, v in entry.items() if k not in ("type", "text")}
            self._append_system_turn(source, str(entry.get("text") or ""), **meta)

    def _queue_tool_advisory(self, nudge_type: str, text: str) -> None:
        """Queue a metacognitive nudge for the next tool-result batch.

        Drains in ``_collect_advisories`` alongside guard findings, then is
        emitted as a first-class ``{"role": "system"}`` turn AFTER the tool
        batch (see the per-result loop in ``_run_loop``).  Used for nudges
        that respond to model behaviour at a tool boundary: ``tool_error``,
        ``repeat``.

        No-ops while the session is inside a wake-driven turn (see
        ``_queue_user_advisory`` for the rationale).
        """
        if self._wake_source_tag:
            return
        self._nudge_queue.enqueue(nudge_type, text, "tool")

    def deliver_wake_nudge_from_queue(self) -> None:
        """Drive a synthetic empty user turn so any-channel nudges drain.

        The standard pipeline does the rendering: the ``send("")`` we
        trigger lands at ``_append_user_turn`` (which stamps
        ``_source = "system_nudge"`` on the synthetic empty user message),
        then ``_emit_pending_user_nudges`` appends each drained nudge as a
        first-class ``{"role": "system"}`` turn after it.  Those turns are
        real conversation history — folded to a ``[start system-reminder]`` block
        on the preceding (empty user) turn for non-native providers, or kept
        inline for native mid-conversation-system models.

        ``_wake_source_tag`` is set for the duration of the synthetic
        send so:

        * ``_check_metacognitive_nudge`` short-circuits (no
          recursive correction / completion detection on the
          envelope text)
        * ``_queue_user_advisory`` / ``_queue_tool_advisory`` short-
          circuit if model behaviour during the wake (e.g. a denied
          tool call) would otherwise queue a fresh nudge on top of
          the wake itself
        * ``_append_user_turn`` stamps ``_source = "system_nudge"``
          on the synthesized user message for audit / replay parity

        Drains the queue inline (running every entry's ``valid_until``
        predicate) BEFORE synthesizing the empty user turn — bails if
        no entry survives the predicate check.  Without this, the
        watcher's ``len(queue)`` peek can succeed on entries whose
        predicate later drops them inside ``_emit_pending_user_nudges``'s
        drain, leaving us synthesizing an empty user turn with no nudge
        context (and risking provider rejection of empty user content).
        The drained items are handed to ``_emit_pending_user_nudges`` via
        ``_wake_drained_reminders`` instead of re-draining the queue from
        inside ``send``.  System turns are persistent (not one-shot), so a
        post-retry stream failure leaves them in place — operator
        intervention is required for the underlying failure anyway.
        """
        items = self._nudge_queue.drain(USER_DRAIN)
        if not items:
            return
        self._wake_source_tag = "system_nudge"
        wake_reminders: list[dict[str, Any]] = []
        for nudge_type, text, meta in items:
            entry: dict[str, Any] = {"type": nudge_type, "text": text}
            if meta:
                entry.update(meta)
            wake_reminders.append(entry)
        self._wake_drained_reminders = wake_reminders
        try:
            self.send("", from_wake=True)
        finally:
            self._wake_source_tag = ""
            self._wake_drained_reminders = None

    def _apply_post_execute_advisories(
        self,
        tool_calls: list[dict[str, Any]],
        results: list[tuple[str, str | list[dict[str, Any]]]],
    ) -> None:
        """Run repeat detection + tool-error nudge over a freshly-executed batch.

        Mutates *results* in place when an identical-repeat warning is
        appended to a tool's text output.  Updates ``self._repeat_detector``,
        ``self._nudge_queue`` (via ``_queue_tool_advisory``), and
        ``self._metacog_state`` (cooldown timestamp via ``should_nudge``).
        The operator-visible signal is the first-class operator-context
        ``{"role": "system"}`` turn the per-result loop downstream emits
        after the tool batch when the drained metacog nudges flush.

        Repeat detection's job is to nudge a flaky local model out of a
        loop where it keeps making the same tool call ("``bash(cmd='echo
        test')`` × 3" being the canonical example).  It fires on the
        consecutive-streak signal alone, with no regard for the tool's
        success / failure / output content — same (name, args) for N
        turns in a row is by definition stuck.  ``RepeatDetector.record``
        already resets the streak on any different signature, so an
        intervening tool call (read, write, anything different) breaks
        the streak naturally without an explicit clear here.

        ``_tool_error_flags`` is the authoritative is_error signal —
        consumed below for the tool-error nudge gate; the per-result
        loop in ``_run_loop`` ``.pop``s it after this returns.
        """
        # Repeat detection: warn when a tool is called with identical
        # args N times in a row.  Independent of success/failure — the
        # stuck-loop pattern is sig-driven, not state-driven.  JSON
        # outputs (MCP structured results) are tracked but exempt from
        # the inline warning text (appending text would corrupt the
        # payload).
        _tc_by_id = {c["id"]: c for c in tool_calls}
        _repeat_detected = False

        for i, (tc_id, output) in enumerate(results):
            tc = _tc_by_id.get(tc_id)
            if tc and isinstance(output, str):
                raw = tc["function"]["name"] + ":" + tc["function"]["arguments"]
                sig = hashlib.sha256(raw.encode()).hexdigest()
                is_json = output.lstrip().startswith(("{", "["))
                if self._repeat_detector.record(sig):
                    _repeat_detected = True
                    if not is_json:
                        output += (
                            "\n\n⚠ Warning: this is an identical repeat of a "
                            "previous tool call. The result is the same. "
                            "Try a different approach."
                        )
                        results[i] = (tc_id, output)
                    # The operator-context system turn after the tool batch
                    # carries the operator-visible signal; the tool-name
                    # context comes from the tool block above it, so a
                    # separate diagnostic info line would just duplicate it.

        if _repeat_detected:
            # Reset so the model gets a clean slate after the warning.
            # If it repeats again, a new warning fires.
            self._repeat_detector.clear()
            if self._mem_cfg.nudges and should_nudge(
                "repeat",
                self._metacog_state,
                message_count=len(self.messages),
                cooldown_secs=self._mem_cfg.nudge_cooldown,
            ):
                self._queue_tool_advisory("repeat", format_nudge("repeat"))

        # Tool-error nudge — queued so it rides the same _collect_advisories
        # drain pass as guard findings and is emitted as a system turn after
        # the tool batch.  Cooldown gating in should_nudge keeps this to one
        # nudge per batch even with many failing tools.
        if (
            self._mem_cfg.nudges
            and any(self._tool_error_flags.get(tc_id) for tc_id, _ in results)
            and should_nudge(
                "tool_error",
                self._metacog_state,
                message_count=len(self.messages),
                memory_count=self._visible_memory_count(),
                cooldown_secs=self._mem_cfg.nudge_cooldown,
            )
        ):
            self._queue_tool_advisory("tool_error", format_nudge("tool_error"))

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

    # -- Coordinator governance (console-driven toggles) -----------------
    #
    # Writes hold ``_governance_lock``; reads are lock-free.  ``_trust_send``
    # is a single bool and ``_revoked_tools`` is a frozenset swapped by
    # reference on each write, so neither read can tear.

    def set_trust_send(self, value: bool) -> None:
        with self._governance_lock:
            self._trust_send = bool(value)

    def get_trust_send(self) -> bool:
        return self._trust_send

    def revoke_tools(self, names: Iterable[str]) -> frozenset[str]:
        """Union ``names`` into the revoked-tools set; return the post-state."""
        additions = frozenset(names)
        with self._governance_lock:
            self._revoked_tools = self._revoked_tools | additions
            return self._revoked_tools

    def get_revoked_tools(self) -> frozenset[str]:
        return self._revoked_tools

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
        project = (args.get("project") or "").strip()
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
            "project": project,
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
                project=item["project"],
            )
        except Exception as e:
            msg = f"Error: spawn_workstream failed: {e}"
            self._report_tool_result(call_id, "spawn_workstream", msg, is_error=True)
            return call_id, msg
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "spawn_workstream", msg, is_error=True)
            return call_id, msg
        # Defensive: absence of ``error`` is the success signal, but
        # a malformed upstream response could land here with no
        # ``ws_id``.  Without this check the LLM gets
        # ``{"child_ws_id": null}`` and chases a null id through
        # follow-up tools.  Mirrors the matching guard in
        # ``_exec_spawn_batch`` (denied row on empty ws_id).
        child_ws_id = str(result.get("ws_id") or "")
        if not child_ws_id:
            msg = "Error: spawn returned no ws_id"
            self._report_tool_result(call_id, "spawn_workstream", msg, is_error=True)
            return call_id, msg
        # Successful spawn — surface child_ws_id + node_id + name +
        # routing strategy so the coordinator can follow up with inspect
        # / send and explain why a given node was chosen.  ``status`` was
        # historically included but it was the routing-proxy's HTTP
        # code (always 200 on this branch); the absence of an
        # ``error`` field is the success signal.  Dropped here to
        # avoid the silent footgun where ``if result["status"] ==
        # "idle"`` looked plausible against the (incorrectly
        # documented) lifecycle-state-string contract.  Lifecycle
        # state lives on the workstream row — read it via
        # ``inspect_workstream``.
        summary = json.dumps(
            {
                # Key is ``child_ws_id`` (not ``ws_id``) so the coordinator
                # LLM doesn't recency-bias toward feeding the spawn-return
                # straight back into another ``spawn_workstream(ws_id=...)``
                # call.  On large fan-outs this cascaded into self-inflicted
                # re-spawn loops instead of progressing to ``wait_for_workstream``.
                "child_ws_id": child_ws_id,
                "name": result.get("name"),
                "node_id": result.get("node_id"),
                "routing_strategy": result.get("routing_strategy"),
            },
            separators=(",", ":"),
        )
        self._report_tool_result(call_id, "spawn_workstream", f"spawned {child_ws_id}")
        return call_id, summary

    # Cap per batch call.  Matches the ``wait_for_workstream`` ws_ids
    # intuition (small enough to fit an operator's eyes in one approval
    # card; if the model wants more, make a second call).  Hard error
    # rather than silent truncation — a silently-dropped child is much
    # harder to notice than an explicit retry prompt.
    _SPAWN_BATCH_MAX_CHILDREN = 10

    def _prepare_spawn_batch(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(call_id, "spawn_batch", "coordinator client unavailable")
        raw_children = args.get("children")
        if not isinstance(raw_children, list) or not raw_children:
            return self._coord_tool_error(
                call_id, "spawn_batch", "children must be a non-empty list"
            )
        if len(raw_children) > self._SPAWN_BATCH_MAX_CHILDREN:
            return self._coord_tool_error(
                call_id,
                "spawn_batch",
                f"children exceeds cap ({len(raw_children)} > "
                f"{self._SPAWN_BATCH_MAX_CHILDREN}); split across multiple calls",
            )

        # Per-item normalisation.  Invalid items surface in ``denied`` at
        # exec time rather than failing the whole batch — we want
        # partial-success semantics so a single malformed row doesn't
        # poison the other approved spawns.
        normalised: list[dict[str, Any]] = []
        preview_rows: list[str] = []
        for idx, raw in enumerate(raw_children):
            if not isinstance(raw, dict):
                normalised.append({"idx": idx, "_error": "child spec must be an object"})
                preview_rows.append(f"  {idx}. [invalid — not an object]")
                continue
            initial_message = self._coord_str_arg(raw, "initial_message").strip()
            skill = self._coord_str_arg(raw, "skill").strip()
            name = self._coord_str_arg(raw, "name").strip()
            model = self._coord_str_arg(raw, "model").strip()
            target_node = self._coord_str_arg(raw, "target_node").strip()
            spec: dict[str, Any] = {
                "idx": idx,
                "initial_message": initial_message,
                "skill": skill,
                "name": name,
                "model": model,
                "target_node": target_node,
            }
            normalised.append(spec)
            if initial_message:
                first_line = initial_message.splitlines()[0]
                preview_line = first_line[:80] + ("..." if len(first_line) > 80 else "")
                tag_bits = []
                if skill:
                    tag_bits.append(f"skill={skill}")
                if target_node:
                    tag_bits.append(f"node={target_node}")
                tags = (" [" + ", ".join(tag_bits) + "]") if tag_bits else ""
                preview_rows.append(f"  {idx}. {preview_line}{tags}")
            else:
                preview_rows.append(f"  {idx}. (idle)")

        # If every row was invalid at normalisation, skip the approval
        # round — operators shouldn't approve a batch with nothing to
        # spawn.  Surface the first denial reason directly so the model
        # gets actionable feedback.
        valid_count = sum(1 for spec in normalised if "_error" not in spec)
        if valid_count == 0:
            first_err = next(
                (s.get("_error") for s in normalised if s.get("_error")), "batch rejected"
            )
            return self._coord_tool_error(call_id, "spawn_batch", first_err or "batch rejected")

        header = f"\u2699 spawn_batch: {len(normalised)} children"
        preview_body = f"{DIM}{chr(10).join(preview_rows)}{RESET}"
        return {
            "call_id": call_id,
            "func_name": "spawn_batch",
            "header": header,
            "preview": preview_body,
            "needs_approval": True,
            "approval_label": "spawn_batch",
            "execute": self._exec_spawn_batch,
            "children": normalised,
        }

    def _exec_spawn_batch(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        children: list[dict[str, Any]] = item["children"]
        total = len(children)

        # Emit ``batch_started`` so the coordinator sidebar can show a
        # "spawning N children" indicator.  Mirrors wait_for_workstream's
        # _emit_wait_event plumbing (best-effort via ui._enqueue).
        self._emit_batch_event(
            "batch_started",
            {"call_id": call_id, "op": "spawn_batch", "total": total},
        )

        results: dict[str, dict[str, Any]] = {}
        denied: list[dict[str, Any]] = []
        for spec in children:
            idx = spec["idx"]
            # A cancel mid-batch stops creating the REST of the children —
            # don't keep spawning workstreams the owner asked to stop.
            # Cooperative: observed at this safe point between spawns, never
            # mid-spawn.  Children already spawned this batch stay in
            # ``results`` and are reported below (their ws_ids must survive —
            # they are live remote workstreams, also durably parent-linked in
            # storage); the rest are marked not-spawned.  Checked with the
            # raw flag, not ``_check_cancelled``, so we fall through to the
            # normal report path rather than raising and dropping the
            # already-spawned ws_ids.
            if self._cancel_event.is_set():
                denied.append({"idx": idx, "reason": "not spawned: cancelled"})
                continue
            # Validation failures from _prepare surface here as denied
            # rows — partial-success: don't abort the rest of the batch.
            if "_error" in spec:
                denied.append({"idx": idx, "reason": spec["_error"]})
                continue
            try:
                result = self._coord_client.spawn(
                    initial_message=spec["initial_message"],
                    parent_ws_id=self._ws_id,
                    user_id=self._user_id,
                    skill=spec["skill"],
                    name=spec["name"],
                    model=spec["model"],
                    target_node=spec["target_node"],
                )
            except Exception as e:
                denied.append({"idx": idx, "reason": f"spawn failed: {e}"})
                continue
            if result.get("error"):
                denied.append({"idx": idx, "reason": str(result["error"])})
                continue
            ws_id = str(result.get("ws_id") or "")
            if not ws_id:
                denied.append({"idx": idx, "reason": "spawn returned no ws_id"})
                continue
            results[str(idx)] = {
                # ``child_ws_id`` (not ``ws_id``) — see the matching
                # comment in ``_exec_spawn_workstream``.
                "child_ws_id": ws_id,
                "name": result.get("name", ""),
                "node_id": result.get("node_id", ""),
                # ``status`` deliberately omitted — see the matching
                # comment in ``_exec_spawn_workstream``: the routing
                # proxy fills it with HTTP 200 on success, which the
                # model can't usefully act on.  Errors land in
                # ``denied[]`` instead.
            }

        # ``truncated`` intentionally omitted — the prepare step
        # hard-errors on >10 children rather than silent truncation,
        # so the bulk-shape flag would always be false and just pads
        # the LLM's tool-result payload.
        summary_payload = {
            "results": results,
            "denied": denied,
        }
        output = json.dumps(summary_payload, separators=(",", ":"), default=str)
        desc = f"spawned {len(results)}/{total}"
        if denied:
            desc += f" ({len(denied)} denied)"
        self._report_tool_result(call_id, "spawn_batch", desc)
        self._emit_batch_event(
            "batch_ended",
            {
                "call_id": call_id,
                "op": "spawn_batch",
                "total": total,
                "succeeded": len(results),
                "denied": len(denied),
            },
        )
        return call_id, output

    def _emit_batch_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fan out a ``batch_*`` SSE event via the session UI.  Best-effort.

        Matches the ``_emit_wait_event`` pattern — the batch itself must
        never fail because of observer plumbing.  The sidebar keys on
        ``call_id`` to pair started/ended into a single indicator.
        """
        ui = getattr(self, "ui", None)
        enqueue = getattr(ui, "_enqueue", None)
        if enqueue is None:
            return
        try:
            enqueue({"type": event_type, **payload})
        except Exception:
            log.debug("batch_event.enqueue_failed type=%s", event_type, exc_info=True)

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
        from turnstone.console.coordinator_client import _format_inspect_tiered

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
        # Tiered output: full → compact (head/tail-snipped messages) →
        # skeleton (counts + last-assistant preview).  First tier that
        # fits the budget wins; the LLM sees a ``_tier`` field on every
        # non-error response.  ``_truncate_output`` remains the safety
        # net for the (rare) skeleton-exceeds-budget case — guarding
        # against a single-field blowup we didn't anticipate.
        output = _format_inspect_tiered(result)
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
        # Trust only relaxes own-subtree sends; foreign ws_ids always
        # prompt for approval even under trust.
        needs_approval = True
        trust_auto_approved = False
        if self._trust_send and self._coord_client._is_own_subtree(ws_id):
            needs_approval = False
            trust_auto_approved = True
        return {
            "call_id": call_id,
            "func_name": "send_to_workstream",
            "header": header,
            "preview": preview_body,
            "needs_approval": needs_approval,
            "approval_label": "send_to_workstream",
            "execute": self._exec_send_to_workstream,
            "ws_id": ws_id,
            "message": message,
            "trust_auto_approved": trust_auto_approved,
        }

    def _exec_send_to_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        if item.get("trust_auto_approved"):
            # Audit before dispatch so a downstream failure doesn't drop the trail.
            try:
                message = item.get("message") or ""
                preview_line = message.splitlines()[0] if message else ""
                self._coord_client.emit_audit(
                    "coordinator.send.auto_approved",
                    {
                        "src": "coordinator",
                        "trust": True,
                        "ws_id": item["ws_id"],
                        "message_preview": preview_line[:120],
                    },
                )
            except Exception:
                log.debug("coord.trust_send.audit_failed", exc_info=True)
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

    def _prepare_close_all_children(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._coord_client is None:
            return self._coord_tool_error(
                call_id, "close_all_children", "coordinator client unavailable"
            )
        reason = self._coord_str_arg(args, "reason").strip()
        header = "\u2699 close_all_children"
        if reason:
            header += f": {reason[:80]}"
        return {
            "call_id": call_id,
            "func_name": "close_all_children",
            "header": header,
            "preview": "",
            "needs_approval": True,
            "approval_label": "close_all_children",
            "execute": self._exec_close_all_children,
            "reason": reason,
        }

    def _exec_close_all_children(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        reason = item.get("reason", "") or ""
        self._emit_batch_event(
            "batch_started",
            {"call_id": call_id, "op": "close_all_children"},
        )
        try:
            result = self._coord_client.close_all_children(reason=reason)
        except Exception as e:
            msg = f"Error: close_all_children failed: {e}"
            self._report_tool_result(call_id, "close_all_children", msg, is_error=True)
            self._emit_batch_event(
                "batch_ended",
                {"call_id": call_id, "op": "close_all_children", "error": str(e)},
            )
            return call_id, msg
        if result.get("error"):
            msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "close_all_children", msg, is_error=True)
            self._emit_batch_event(
                "batch_ended",
                {
                    "call_id": call_id,
                    "op": "close_all_children",
                    "error": str(result["error"]),
                },
            )
            return call_id, msg
        closed = [str(x) for x in result.get("closed") or [] if x]
        failed = [str(x) for x in result.get("failed") or [] if x]
        skipped = [str(x) for x in result.get("skipped") or [] if x]
        summary_payload: dict[str, Any] = {
            "closed": closed,
            "failed": failed,
            "skipped": skipped,
        }
        if reason:
            summary_payload["reason"] = reason
        output = json.dumps(summary_payload, separators=(",", ":"))
        desc = f"closed {len(closed)}"
        if failed:
            desc += f", {len(failed)} failed"
        if skipped:
            desc += f", {len(skipped)} skipped"
        self._report_tool_result(call_id, "close_all_children", desc)
        self._emit_batch_event(
            "batch_ended",
            {
                "call_id": call_id,
                "op": "close_all_children",
                "closed": len(closed),
                "failed": len(failed),
                "skipped": len(skipped),
            },
        )
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
        # ``dropped`` — forensic snapshot captured by the node's cancel
        # handler before it invokes session.cancel().  Carries pending
        # approval tool names, queued-message count/preview, and whether
        # a worker was running.  Empty dict when nothing was in flight.
        out_payload: dict[str, Any] = {
            "ws_id": item["ws_id"],
            "cancelled": True,
            "status": result.get("status"),
        }
        dropped = result.get("dropped")
        if isinstance(dropped, dict) and dropped:
            out_payload["dropped"] = dropped
        output = json.dumps(out_payload, separators=(",", ":"))
        summary = f"cancelled {item['ws_id']}"
        if isinstance(dropped, dict):
            hints: list[str] = []
            pa = dropped.get("pending_approval")
            if isinstance(pa, dict):
                names = pa.get("tool_names") or []
                if names:
                    hints.append(f"approval={','.join(str(n) for n in names)}")
            qm = dropped.get("queued_messages")
            if isinstance(qm, dict) and qm.get("count"):
                hints.append(f"queued={qm['count']}")
            if hints:
                summary += " (" + "; ".join(hints) + ")"
        self._report_tool_result(call_id, "cancel_workstream", summary)
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
        # Metadata values are JSON-encoded at rest; the client handles
        # the encode/decode so preserve the model's natural types (``4``
        # stays an int, ``"gpu"`` stays a string) rather than
        # stringifying here.
        #
        # Two accepted shapes:
        #
        #   list_nodes(filters={"os": "Linux"})   ← canonical, nested
        #   list_nodes(os="Linux")                ← flat top-level args
        #
        # Several models drop the ``filters`` nesting and emit each
        # filter as a top-level kwarg; the strict-nested-only shape
        # silently degraded those calls to "no filter" and returned
        # the full cluster, which an operator hit during shakedown
        # ("``os="DefinitelyNotAnOS"`` returned all 10 nodes").
        # Treating top-level non-reserved args as filters fixes the
        # natural mistake without changing the canonical shape;
        # nested entries still win on key collision.
        raw_filters = args.get("filters")
        filters: dict[str, Any] = {}
        if isinstance(raw_filters, dict):
            for k, v in raw_filters.items():
                if isinstance(k, str) and k and isinstance(v, (str, int, float, bool)):
                    filters[k] = v
        for k, v in args.items():
            if k in _LIST_NODES_RESERVED_ARGS:
                continue
            if isinstance(k, str) and k and isinstance(v, (str, int, float, bool)):
                filters.setdefault(k, v)
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

    # -- skills tool ----------------------------------------------------------
    #
    # Single action-multiplexed tool replacing legacy ``skill`` +
    # ``list_skills``.  Read actions (find, get) auto-approve.  Write
    # actions (create, update, enable, disable) require approval AND the
    # ``model.skills.write`` permission on the session user (default-
    # ungranted; operators opt themselves in).  ``load`` mutates session
    # state — works on both interactive and coordinator sessions.

    # Projected ``allowed_tools`` count above which the scanner is likely
    # to bump the risk tier.  Surfaced on the approval card via
    # ``_scan_proposed_skill`` so the operator sees risk drift before
    # approving.
    _SKILLS_TOOLS_PROJECTION_CAP: ClassVar[int] = 20

    def _require_model_skills_write(
        self, call_id: str, action: str, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Gate model-proposed skill writes on the ``model.skills.write``
        permission, called BOTH at prepare time (gating the approval card
        render) AND at exec time (catching grant revocation between
        approval and write).  Denials are audited with
        ``skill.write_denied`` so probing attempts leave a trail.

        Returns ``None`` on grant, or a ``_coord_tool_error`` dict on
        deny.  The dict shape is compatible with both prepare-time return
        (the dispatcher hands it back to the chat loop) and exec-time
        early-return (callers detect ``deny is not None`` and short-circuit).
        """
        from turnstone.core.auth import user_has_permission
        from turnstone.core.storage._registry import get_storage

        if user_has_permission(self._user_id, "model.skills.write"):
            return None
        # Audit the deny so probing for the grant leaves a forensic
        # record.  ``skill.write_denied`` action distinguishes denied
        # attempts from approved-and-failed writes (which audit under
        # ``skill.create`` / ``skill.update`` etc.).
        name = args.get("name") if isinstance(args.get("name"), str) else ""
        storage = get_storage()
        if storage is not None:
            self._audit_skill_action(
                storage,
                "skill.write_denied",
                "",
                {"action": action, "name": name or ""},
            )
        return self._coord_tool_error(
            call_id,
            "skills",
            self._skill_hint(
                f"permission denied: action='{action}' requires the "
                "'model.skills.write' permission",
                system_reminder=(
                    "model.skills.write is default-ungranted on every "
                    "role including builtin-admin. Ask the operator to "
                    "grant it via the Roles tab in the admin panel "
                    "before retrying. Read actions (find, get) remain "
                    "available without the grant."
                ),
            ),
        )

    def _skill_hint(self, message: str, *, system_reminder: str = "") -> str:
        """Return a skills tool-result *message*, queuing an optional hint turn.

        *system_reminder* is guidance for the model's next move (broaden a
        filter, ask the operator for a permission, …).  It is no longer spliced
        into the tool result as a bare ``[start system-reminder]`` block — that marker
        is declared *untrusted* on the fold path, which would silently demote the
        hint.  Instead it is queued onto the tool channel via
        :meth:`_queue_tool_advisory` and drained by :meth:`_collect_advisories`
        into a first-class ``{"role": "system", "_source": "skill_hint"}`` turn
        that lands after the (clean) tool result — folded inside the trusted
        nonce fence for non-native models, kept inline for native ones.  (Queuing
        no-ops during a wake, like the other tool-channel advisories.)

        *message* is returned verbatim as the tool result.  It needs no escaping:
        it is ordinary tool output (untrusted by nature), and if a call site
        interpolates a model-controlled value that contains a ``[start system-reminder]``
        marker, the fold's host-escaping
        (:func:`turnstone.core.lowering._neutralize_host`) defangs it.
        """
        if system_reminder:
            self._queue_tool_advisory("skill_hint", system_reminder)
        return message

    def _prepare_skills(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch on ``action``.  Reads auto-approve; writes require both
        operator approval and the ``model.skills.write`` permission."""
        action = self._coord_str_arg(args, "action").strip().lower()
        if action == "find":
            return self._prepare_skills_find(call_id, args)
        if action == "get":
            return self._prepare_skills_get(call_id, args)
        if action == "load":
            return self._prepare_skills_load(call_id, args)
        if action in ("create", "update", "enable", "disable"):
            deny = self._require_model_skills_write(call_id, action, args)
            if deny is not None:
                return deny
            if action == "create":
                return self._prepare_skills_create(call_id, args)
            if action == "update":
                return self._prepare_skills_update(call_id, args)
            return self._prepare_skills_toggle(call_id, args, enable=(action == "enable"))
        return self._coord_tool_error(
            call_id,
            "skills",
            f"action must be one of: find, get, load, create, update, enable, "
            f"disable; got '{action}'",
        )

    # -- find ----------------------------------------------------------------

    def _prepare_skills_find(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        from turnstone.core.skill_kind import SkillKind

        category = self._coord_str_arg(args, "category").strip() or None
        tag = self._coord_str_arg(args, "tag").strip() or None
        risk_level = self._coord_str_arg(args, "risk_level").strip() or None
        query = self._coord_str_arg(args, "query").strip() or None
        # ``kind`` is an opt-in discoverability filter — metadata-only
        # after the flatten (no code path branches on it), but useful to
        # the model for narrowing a catalog browse to skills authored for
        # a specific audience.  Validated against the ``SkillKind`` enum
        # the same way create/update validate (matches the consistency
        # contract the model expects on the same field across actions);
        # ``SkillKind.ANY`` collapses to ``None`` so the documented
        # "default returns every kind" semantic holds when the model
        # passes the enum value explicitly.  When the narrower kinds
        # are supplied the storage filter threads ``[<kind>, 'any']``
        # so ``any``-tagged rows remain visible.
        raw_kind = self._coord_str_arg(args, "kind").strip().lower() or None
        if raw_kind is None:
            kind: str | None = None
        else:
            try:
                parsed = SkillKind(raw_kind)
            except ValueError:
                return self._coord_tool_error(
                    call_id,
                    "skills",
                    f"find: kind must be one of: "
                    f"{', '.join(sorted(k.value for k in SkillKind))}; got {raw_kind!r}",
                )
            kind = None if parsed is SkillKind.ANY else parsed.value
        enabled_only = self._coord_bool_arg(args, "enabled_only")
        try:
            limit = int(args.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        header_bits = ["⚙ skills find"]
        if query:
            header_bits.append(f'query="{query[:40]}"')
        if category:
            header_bits.append(f"category={category}")
        if tag:
            header_bits.append(f"tag={tag}")
        if risk_level:
            header_bits.append(f"risk_level={risk_level}")
        if kind:
            header_bits.append(f"kind={kind}")
        if enabled_only:
            header_bits.append("enabled_only=true")
        return {
            "call_id": call_id,
            "func_name": "skills",
            "header": " ".join(header_bits),
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_skills,
            "action": "find",
            "category": category,
            "tag": tag,
            "risk_level": risk_level,
            "query": query,
            "kind": kind,
            "enabled_only": enabled_only,
            "limit": limit,
        }

    def _exec_skills_find(self, item: dict[str, Any]) -> tuple[str, str]:
        from turnstone.core.storage._registry import get_storage

        call_id = item["call_id"]
        storage = get_storage()
        if storage is None:
            msg = "Error: storage unavailable"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        # ``kind`` is opt-in only — the default catalog browse returns all
        # kinds and the model sorts/groups client-side on the ``kind`` field
        # in the projection.  Passing ``kind=interactive`` (etc.) widens the
        # filter to include ``any``-tagged rows so the audience-neutral
        # entries don't drop out of the narrowed view.
        kind_filter = item.get("kind")
        kinds = [kind_filter, "any"] if kind_filter else None
        try:
            rows = storage.list_skills_filtered(
                category=item["category"],
                tag=item["tag"],
                risk_level=item["risk_level"],
                kinds=kinds,
                enabled_only=item["enabled_only"],
                limit=item["limit"] + 1,  # +1 to detect truncation
            )
        except Exception as e:
            msg = f"Error: skills find failed: {e}"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        truncated = len(rows) > item["limit"]
        rows = rows[: item["limit"]]
        query = item.get("query")
        if query and rows:
            from turnstone.core.bm25 import BM25Index

            corpus = [
                " ".join(
                    filter(
                        None,
                        [
                            r.get("name", ""),
                            r.get("description", ""),
                            self._skills_tags_text(r.get("tags", "[]")),
                            r.get("category", ""),
                        ],
                    )
                )
                for r in rows
            ]
            index = BM25Index(corpus, reranker=self._bm25_reranker())
            top = index.search(query, k=min(len(rows), 50))
            rows = [rows[i] for i in top]
        skills = [self._skills_project_row(r) for r in rows]
        result = {"skills": skills, "truncated": truncated}
        any_filter = bool(
            item.get("category")
            or item.get("tag")
            or item.get("risk_level")
            or item.get("query")
            or item.get("kind")
            or item.get("enabled_only")
        )
        summary = f"{len(skills)} skills"
        if truncated:
            summary += " (truncated; narrow filters or raise limit)"
        output_msg: str | None = None
        if not skills and any_filter:
            try:
                unfiltered = storage.list_skills_filtered(enabled_only=False, limit=10)
            except Exception:
                unfiltered = []
            if unfiltered:
                # The hint must NOT echo the model-supplied filter values: it
                # now rides into a TRUSTED operator system turn (the fold fence
                # / native system role), and the filter args are model-
                # controlled — under an indirect injection they could carry an
                # attacker directive, which echoing here would launder into
                # operator authority.  The count is harness-derived, and the
                # model already knows the filters it just sent, so fixed
                # guidance suffices.
                output_msg = self._skill_hint(
                    "0 skills matched the supplied filters.",
                    system_reminder=(
                        f"The applied filters matched nothing, but the catalog "
                        f"has at least {len(unfiltered)} skill(s). Omit the "
                        "most-restrictive filter or use a broader query."
                    ),
                )
                summary = "0 skills (hint included)"
        output = (
            output_msg
            if output_msg is not None
            else json.dumps(result, separators=(",", ":"), default=str)
        )
        self._report_tool_result(call_id, "skills", summary)
        return call_id, self._truncate_output(output)

    @staticmethod
    def _skills_tags_text(raw: Any) -> str:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else list(raw)
            if isinstance(parsed, list):
                return " ".join(str(t) for t in parsed)
        except (ValueError, TypeError):
            pass
        return raw if isinstance(raw, str) else ""

    def _skills_project_row(self, r: dict[str, Any]) -> dict[str, Any]:
        """Narrow projection for ``find`` output.  Mirrors the previous
        ``coord_client.list_skills`` shape so existing callers and the
        model's mental model don't shift on the merge: full content
        body is reserved for ``get``.
        """
        tags_raw = r.get("tags") or "[]"
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else list(tags_raw)
        except (TypeError, ValueError):
            tags = []
        allowed_raw = r.get("allowed_tools") or "[]"
        try:
            allowed_full = (
                json.loads(allowed_raw) if isinstance(allowed_raw, str) else list(allowed_raw)
            )
        except (TypeError, ValueError):
            allowed_full = []
        if not isinstance(allowed_full, list):
            allowed_full = []
        cap = self._SKILLS_TOOLS_PROJECTION_CAP
        allowed_tools: list[str] = [str(t) for t in allowed_full[:cap]]
        if len(allowed_full) > cap:
            allowed_tools.append(f"+{len(allowed_full) - cap} more")
        row: dict[str, Any] = {
            "name": r.get("name") or "",
            "category": r.get("category") or "",
            "tags": tags,
            "version": r.get("version") or "",
            "description": r.get("description") or "",
            "model": r.get("model") or "",
            "enabled": bool(r.get("enabled")),
            "risk_level": r.get("risk_level") or "",
            "activation": r.get("activation") or "",
            "kind": r.get("kind") or "any",
        }
        if allowed_tools:
            row["allowed_tools"] = allowed_tools
        return row

    # -- get -----------------------------------------------------------------

    def _prepare_skills_get(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        name = self._coord_str_arg(args, "name").strip()
        if not name:
            return self._coord_tool_error(call_id, "skills", "get: 'name' is required")
        return {
            "call_id": call_id,
            "func_name": "skills",
            "header": f"⚙ skills get: {name}",
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_skills,
            "action": "get",
            "name": name,
        }

    def _exec_skills_get(self, item: dict[str, Any]) -> tuple[str, str]:
        from turnstone.core.storage._registry import get_storage

        call_id = item["call_id"]
        name = item["name"]
        storage = get_storage()
        if storage is None:
            msg = "Error: storage unavailable"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        row = storage.get_prompt_template_by_name(name)
        if row is None:
            msg = self._skill_hint(
                f"skill '{name}' not found",
                system_reminder=(
                    "Use skills(action='find', query='...') to discover "
                    "available skill names. Names are exact-match and "
                    "case-sensitive."
                ),
            )
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        projected = self._skills_project_row(row)
        projected["content"] = row.get("content") or ""
        projected["scan_report"] = row.get("scan_report") or ""
        projected["readonly"] = bool(row.get("readonly"))
        output = json.dumps(projected, separators=(",", ":"), default=str)
        self._report_tool_result(call_id, "skills", f"got {name}")
        return call_id, self._truncate_output(output)

    # -- load -------------------------------------------------------------------

    def _prepare_skills_load(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        # Both kinds can ``load`` — interactive sessions replace their own
        # persona, coordinator sessions do the same for their orchestrator.
        # Parity with the admin / HTTP create path that already accepts a
        # ``skill`` body field on coord workstreams: anything an operator
        # can do at create time, the model can do at runtime through this
        # same action.  Any skill in the catalog is loadable regardless of
        # the row's ``kind`` marker — ``kind`` is authored audience
        # metadata, not an enforcement boundary.  Real access control
        # remains the ``allowed_tools`` + ``auto_approve`` pair, which
        # applies identically across kinds.  Use ``spawn_workstream(skill=...)``
        # for assigning to children; ``load`` activates on the *current*
        # session.
        name = self._coord_str_arg(args, "name").strip()
        if not name:
            return self._coord_tool_error(call_id, "skills", "load: 'name' is required")
        # SKILL.md spec ``$ARGUMENTS`` payload — optional.  Mirror
        # the spec's free-form string shape (``/skill-name a b "c d"``)
        # rather than a list, so the model can pass quoted positional
        # args and have ``shlex.split`` at substitution time produce
        # the same result a CLI user would type.
        arguments = self._coord_str_arg(args, "arguments")
        # Approval surfaces the args so the operator can see what the
        # model is about to substitute into the system message — and
        # so a once-approved skill name can't grant cover for a future
        # invocation with a different (potentially injected) payload.
        # ``approval_label`` includes a digest of the args so each
        # distinct payload is a distinct approval decision.
        approval_args_digest = (
            hashlib.sha256(arguments.encode("utf-8")).hexdigest()[:8] if arguments else "no-args"
        )
        preview = f"arguments: {arguments}" if arguments else "(no arguments)"
        return {
            "call_id": call_id,
            "func_name": "skills",
            "header": f"⚙ skills load: {name}",
            "preview": preview,
            "needs_approval": True,
            "approval_label": f"skills__load__{name}__{approval_args_digest}",
            "execute": self._exec_skills,
            "action": "load",
            "name": name,
            "arguments": arguments,
        }

    def _exec_skills_load(self, item: dict[str, Any]) -> tuple[str, str]:
        from turnstone.core.storage._registry import get_storage

        call_id = item["call_id"]
        name = item["name"]
        arguments = item.get("arguments", "")
        storage = get_storage()
        if storage is None:
            msg = "Error: storage unavailable"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        # ``enabled=False`` is the admin's quarantine flag — collapse the
        # missing-row and disabled cases into a single recovery hint so
        # the model has one consistent next step.  The ``kind`` column is
        # passive metadata after the flatten — no visibility gate here.
        skill_data = storage.get_prompt_template_by_name(name)
        if not skill_data or not skill_data.get("enabled", True):
            msg = self._skill_hint(
                f"skill '{name}' not found or disabled",
                system_reminder=(
                    "Use skills(action='find') to discover loadable "
                    "skills, or skills(action='enable', name='...') if a "
                    "disabled skill is the one you want."
                ),
            )
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        if self._skill_name == name and self._skill_arguments == arguments:
            # ``arguments`` is load-bearing for the spec's substitution —
            # an existing load with a DIFFERENT args payload should
            # re-render, not no-op.  Only short-circuit on full identity.
            msg = f"Skill '{name}' is already active"
            self._report_tool_result(call_id, "skills", msg)
            return call_id, msg
        self.set_skill(name, arguments=arguments)
        desc = skill_data.get("description", "")
        risk = skill_data.get("risk_level", "")
        parts = [f"Loaded skill '{name}'"]
        if desc:
            parts.append(f"Description: {desc}")
        if risk:
            parts.append(f"Risk tier: {risk}")
        msg = "\n".join(parts)
        self._report_tool_result(call_id, "skills", msg)
        return call_id, msg

    # -- create --------------------------------------------------------------

    def _prepare_skills_create(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        from turnstone.core.skill_field_validation import parse_skill_session_config
        from turnstone.core.skill_kind import SkillKind

        name = self._coord_str_arg(args, "name").strip()[:256]
        content = self._coord_str_arg(args, "content").strip()[:32768]
        description = self._coord_str_arg(args, "description").strip()[:MAX_SKILL_DESCRIPTION_LEN]
        category = self._coord_str_arg(args, "category").strip()[:64] or "general"
        if not name:
            return self._coord_tool_error(call_id, "skills", "create: 'name' is required")
        if not content:
            return self._coord_tool_error(call_id, "skills", "create: 'content' is required")
        if not description:
            return self._coord_tool_error(call_id, "skills", "create: 'description' is required")
        raw_kind = self._coord_str_arg(args, "kind").strip().lower() or "any"
        try:
            kind = SkillKind(raw_kind).value
        except ValueError:
            return self._coord_tool_error(
                call_id,
                "skills",
                f"create: kind must be one of: "
                f"{', '.join(sorted(k.value for k in SkillKind))}; got {raw_kind!r}",
            )
        raw_tags = args.get("tags", [])
        tags_str = json.dumps([str(t) for t in raw_tags]) if isinstance(raw_tags, list) else "[]"
        session_fields, err_msg = parse_skill_session_config(args)
        if err_msg is not None:
            return self._coord_tool_error(call_id, "skills", f"create: {err_msg}")
        projected_risk = self._scan_proposed_skill(content, session_fields.get("allowed_tools"))
        preview_lines = [
            f"    name: {name}",
            f"    kind: {kind}",
            f"    category: {category}",
            f"    description: {description[:120]}",
            f"    content length: {len(content)} chars",
            f"    projected risk: {projected_risk or 'unknown'}",
        ]
        at_str = session_fields.get("allowed_tools")
        has_allowed_tools = bool(at_str) and at_str != "[]"
        if has_allowed_tools:
            preview_lines.append(f"    allowed_tools: {at_str}")
        if session_fields.get("auto_approve"):
            preview_lines.append("    auto_approve: true")
            if has_allowed_tools:
                # Spell out the operational consequence: the combination
                # of auto_approve + allowed_tools means each tool in the
                # allowlist auto-fires when this skill is later loaded.
                # Operator approving the create needs to see this, not
                # just two innocuous-looking field values.
                preview_lines.append(
                    "    WARNING: auto_approve + allowed_tools means the "
                    "tools above auto-fire when this skill is loaded"
                )
        header = f"⚙ skills create: {name}"
        if projected_risk in ("high", "critical"):
            header += f" (risk={projected_risk})"
        return {
            "call_id": call_id,
            "func_name": "skills",
            "header": header,
            "preview": "\n".join(preview_lines),
            "needs_approval": True,
            "approval_label": f"skills__create__{name}",
            "execute": self._exec_skills,
            "action": "create",
            "name": name,
            "content": content,
            "description": description,
            "category": category,
            "kind": kind,
            "tags": tags_str,
            "session_fields": session_fields,
            "projected_risk": projected_risk,
        }

    def _exec_skills_create(self, item: dict[str, Any]) -> tuple[str, str]:
        from turnstone.core.storage._registry import get_storage

        call_id = item["call_id"]
        name = item["name"]
        # Re-check the permission at exec time — operator may have
        # revoked ``model.skills.write`` between prepare and approval.
        # See ``_require_model_skills_write`` for the deny-audit shape.
        deny = self._require_model_skills_write(call_id, "create", {"name": name})
        if deny is not None:
            err = deny.get("error", "Error: permission denied")
            self._report_tool_result(call_id, "skills", err, is_error=True)
            return call_id, err
        storage = get_storage()
        if storage is None:
            msg = "Error: storage unavailable"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        if storage.get_prompt_template_by_name(name) is not None:
            msg = self._skill_hint(
                f"skill name '{name}' already exists",
                system_reminder=(
                    "Use skills(action='update', name='...') to modify "
                    "an existing skill, or pick a unique name."
                ),
            )
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        skill_id = uuid.uuid4().hex
        session_fields = dict(item.get("session_fields") or {})
        activation = session_fields.pop("activation", "named")
        is_default = activation == "default"
        try:
            storage.create_prompt_template(
                template_id=skill_id,
                name=name,
                category=item["category"],
                content=item["content"],
                variables="[]",
                is_default=is_default,
                org_id="",
                created_by=self._user_id,
                origin="model",
                description=item["description"],
                tags=item["tags"],
                activation=activation,
                token_estimate=len(item["content"]) // 4,
                kind=item["kind"],
                **session_fields,
            )
        except Exception as e:
            msg = f"Error: skills create failed: {e}"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        self._audit_skill_action(
            storage,
            "skill.create",
            skill_id,
            {"name": name, "kind": item["kind"], "category": item["category"]},
        )
        created = storage.get_prompt_template(skill_id) or {}
        output = json.dumps(
            {
                "template_id": skill_id,
                "name": name,
                "risk_level": created.get("risk_level", ""),
                "kind": item["kind"],
            },
            separators=(",", ":"),
            default=str,
        )
        self._report_tool_result(call_id, "skills", f"created {name}")
        return call_id, output

    # -- update --------------------------------------------------------------

    # Source of truth for readonly-skill update field set lives in
    # ``turnstone.core.skill_field_validation`` so this and the admin
    # HTTP path (``console/server.py``) read the same set — drift between
    # them would let one surface accept a field the other rejects.
    _SKILLS_READONLY_FIELDS: ClassVar[frozenset[str]] = SKILL_RUNTIME_CONFIG_FIELDS

    def _prepare_skills_update(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        from turnstone.core.skill_field_validation import parse_skill_session_config
        from turnstone.core.skill_kind import SkillKind
        from turnstone.core.storage._registry import get_storage

        name = self._coord_str_arg(args, "name").strip()
        if not name:
            return self._coord_tool_error(call_id, "skills", "update: 'name' is required")
        storage = get_storage()
        if storage is None:
            return self._coord_tool_error(call_id, "skills", "storage unavailable")
        existing = storage.get_prompt_template_by_name(name)
        if existing is None:
            return self._coord_tool_error(
                call_id,
                "skills",
                self._skill_hint(
                    f"update: skill '{name}' not found",
                    system_reminder=(
                        "Use skills(action='find') to discover existing "
                        "skill names, or skills(action='create') to make "
                        "a new one."
                    ),
                ),
            )
        session_fields, err_msg = parse_skill_session_config(args)
        if err_msg is not None:
            return self._coord_tool_error(call_id, "skills", f"update: {err_msg}")
        updates: dict[str, Any] = dict(session_fields)
        if "content" in args:
            new_content = self._coord_str_arg(args, "content").strip()[:32768]
            if not new_content:
                # Reject hollow-out: an empty content body is a soft-delete
                # the model isn't supposed to be able to perform.  Hard
                # delete stays admin-UI exclusive; an empty-content update
                # bypassed that invariant by hollowing rather than removing.
                return self._coord_tool_error(
                    call_id,
                    "skills",
                    self._skill_hint(
                        "update: content must not be empty",
                        system_reminder=(
                            "Use skills(action='disable', name='...') to "
                            "hide a skill without removing it.  Hard "
                            "delete is admin-UI only by design."
                        ),
                    ),
                )
            updates["content"] = new_content
            updates["token_estimate"] = len(new_content) // 4
        if "description" in args:
            new_desc = self._coord_str_arg(args, "description").strip()[:MAX_SKILL_DESCRIPTION_LEN]
            if not new_desc:
                return self._coord_tool_error(
                    call_id, "skills", "update: description must not be empty"
                )
            updates["description"] = new_desc
        if "category" in args:
            new_cat = self._coord_str_arg(args, "category").strip()[:64]
            if not new_cat:
                # Mirror description's empty-check.  Category is used for
                # discovery filtering — silently accepting "" would hide
                # the skill from category-filtered finds.
                return self._coord_tool_error(
                    call_id, "skills", "update: category must not be empty"
                )
            updates["category"] = new_cat
        if "kind" in args:
            raw_kind = self._coord_str_arg(args, "kind").strip().lower()
            try:
                updates["kind"] = SkillKind(raw_kind).value
            except ValueError:
                return self._coord_tool_error(
                    call_id,
                    "skills",
                    f"update: kind must be one of: "
                    f"{', '.join(sorted(k.value for k in SkillKind))}; got {raw_kind!r}",
                )
        if "tags" in args:
            raw_tags = args["tags"]
            if not isinstance(raw_tags, list):
                # Non-list tags previously silently dropped, then surfaced
                # as the misleading "no recognized fields" error when tags
                # was the only field — fail loudly instead.
                return self._coord_tool_error(
                    call_id,
                    "skills",
                    "update: tags must be a JSON array of strings",
                )
            updates["tags"] = json.dumps([str(t) for t in raw_tags])
        if not updates:
            return self._coord_tool_error(
                call_id, "skills", "update: no recognized fields to change"
            )
        if bool(existing.get("readonly")):
            filtered = {k: v for k, v in updates.items() if k in self._SKILLS_READONLY_FIELDS}
            if not filtered:
                return self._coord_tool_error(
                    call_id,
                    "skills",
                    self._skill_hint(
                        f"update: skill '{name}' is readonly (externally "
                        "installed); only runtime config fields may be "
                        "changed",
                        system_reminder=(
                            "Readonly skills preserve external-source "
                            "fidelity. Editable runtime fields: "
                            + ", ".join(sorted(self._SKILLS_READONLY_FIELDS))
                        ),
                    ),
                )
            updates = filtered
        current_risk = str(existing.get("risk_level") or "")
        # Skip the scanner when no scan-relevant fields change.  Storage
        # re-scans authoritatively on write; the prepare-time scan is
        # purely to surface the projected tier on the approval card.
        # A metadata-only update (kind/category/tags/description) can
        # reuse current_risk without spending ~25 regex passes.
        if "content" in updates or "allowed_tools" in updates:
            final_content = updates.get("content", existing.get("content", ""))
            final_at = updates.get("allowed_tools", existing.get("allowed_tools", "[]"))
            projected_risk = self._scan_proposed_skill(final_content, final_at)
        else:
            projected_risk = current_risk
        preview_lines = [f"    name: {name}"]
        for k, v in updates.items():
            if k == "content":
                preview_lines.append(f"    content: <{len(v)} chars>")
            elif k == "allowed_tools":
                preview_lines.append(f"    allowed_tools: {v}")
            else:
                vstr = str(v)
                preview_lines.append(f"    {k}: {vstr[:120]}")
        # Self-escalation warning: if the *final* state has both
        # auto_approve=True AND non-empty allowed_tools, spell out the
        # operational consequence on the approval card.  Compute against
        # the resolved final state — not just the existing row — so an
        # update that explicitly turns auto_approve OFF doesn't false-
        # positive the warning, and an update that turns it ON without
        # touching allowed_tools still fires it against the inherited
        # allowlist.
        final_auto_approve = bool(
            updates["auto_approve"] if "auto_approve" in updates else existing.get("auto_approve")
        )
        final_allowed_tools = updates.get("allowed_tools", existing.get("allowed_tools", "[]"))
        final_has_allowed_tools = bool(final_allowed_tools) and final_allowed_tools != "[]"
        if final_auto_approve and final_has_allowed_tools:
            preview_lines.append(
                "    WARNING: auto_approve + allowed_tools means the listed "
                "tools auto-fire when this skill is loaded"
            )
        if current_risk or projected_risk:
            arrow = "->" if current_risk != projected_risk else "="
            preview_lines.append(
                f"    risk_level: {current_risk or 'unknown'} {arrow} {projected_risk or 'unknown'}"
            )
        header = f"⚙ skills update: {name}"
        if (
            projected_risk
            and projected_risk != current_risk
            and projected_risk in ("high", "critical")
        ):
            header += f" (risk {current_risk or 'unknown'}->{projected_risk})"
        return {
            "call_id": call_id,
            "func_name": "skills",
            "header": header,
            "preview": "\n".join(preview_lines),
            "needs_approval": True,
            "approval_label": f"skills__update__{name}",
            "execute": self._exec_skills,
            "action": "update",
            "name": name,
            "template_id": existing["template_id"],
            "updates": updates,
            "projected_risk": projected_risk,
            "current_risk": current_risk,
            "readonly": bool(existing.get("readonly")),
        }

    def _exec_skills_update(self, item: dict[str, Any]) -> tuple[str, str]:
        from turnstone.core.storage._registry import get_storage

        call_id = item["call_id"]
        name = item["name"]
        template_id = item["template_id"]
        # Re-check the write permission (see _require_model_skills_write).
        deny = self._require_model_skills_write(call_id, "update", {"name": name})
        if deny is not None:
            err = deny.get("error", "Error: permission denied")
            self._report_tool_result(call_id, "skills", err, is_error=True)
            return call_id, err
        storage = get_storage()
        if storage is None:
            msg = "Error: storage unavailable"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        # Re-fetch the row to catch a readonly flip between prepare and
        # exec.  If the row went readonly since prepare, drop any updates
        # that the prepare-time filter would no longer accept.  If it
        # went unreadonly, the prepare-time filter was too restrictive
        # but no harm — proceed with whatever fields survived prepare.
        current_row = storage.get_prompt_template(template_id) or {}
        if bool(current_row.get("readonly")) and not item.get("readonly"):
            allowed = self._SKILLS_READONLY_FIELDS
            filtered = {k: v for k, v in item["updates"].items() if k in allowed}
            if not filtered:
                msg = self._skill_hint(
                    f"update: skill '{name}' became readonly between "
                    "approval and exec; no fields applied",
                    system_reminder=(
                        "An admin flipped the readonly flag on this "
                        "skill after the operator approved the update. "
                        "Re-issue the update against only runtime fields "
                        "(model, temperature, allowed_tools, etc.)."
                    ),
                )
                self._report_tool_result(call_id, "skills", msg, is_error=True)
                return call_id, msg
            item["updates"] = filtered
            item["readonly"] = True
        # Snapshot existing row to skill_versions for rollback.  Uses
        # max(version) + 1 (via list_skill_versions, which returns rows
        # ordered by version DESC so [0] is the max) instead of
        # count + 1 — ``count`` re-uses numbers when versions have been
        # deleted via storage.delete_skill_versions, and the schema has
        # no (skill_id, version) unique constraint to catch collisions.
        # Matches the storage-level pattern in storage.unlock_skill.
        # A storage-side allocator (atomic max+1 with a unique index)
        # is the right architectural fix and is tracked separately.
        try:
            existing_versions = storage.list_skill_versions(template_id)
            current_max = max((int(v.get("version") or 0) for v in existing_versions), default=0)
            next_version = current_max + 1
            storage.create_skill_version(
                skill_id=template_id,
                version=next_version,
                snapshot=json.dumps(storage.get_prompt_template(template_id) or {}, default=str),
                changed_by=self._user_id,
            )
        except Exception:
            log.warning("skills.update.snapshot_failed name=%s", name, exc_info=True)
        try:
            storage.update_prompt_template(template_id, **item["updates"])
        except Exception as e:
            msg = f"Error: skills update failed: {e}"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        action = "skill.update.config" if item.get("readonly") else "skill.update"
        self._audit_skill_action(
            storage,
            action,
            template_id,
            {
                "name": name,
                "fields": sorted(item["updates"].keys()),
                "projected_risk": item.get("projected_risk", ""),
                "previous_risk": item.get("current_risk", ""),
            },
        )
        updated = storage.get_prompt_template(template_id) or {}
        output = json.dumps(
            {
                "template_id": template_id,
                "name": name,
                "risk_level": updated.get("risk_level", ""),
                "updated_fields": sorted(item["updates"].keys()),
            },
            separators=(",", ":"),
            default=str,
        )
        self._report_tool_result(call_id, "skills", f"updated {name}")
        return call_id, output

    # -- enable / disable ----------------------------------------------------

    def _prepare_skills_toggle(
        self, call_id: str, args: dict[str, Any], *, enable: bool
    ) -> dict[str, Any]:
        from turnstone.core.storage._registry import get_storage

        verb = "enable" if enable else "disable"
        name = self._coord_str_arg(args, "name").strip()
        if not name:
            return self._coord_tool_error(call_id, "skills", f"{verb}: 'name' is required")
        storage = get_storage()
        if storage is None:
            return self._coord_tool_error(call_id, "skills", "storage unavailable")
        existing = storage.get_prompt_template_by_name(name)
        if existing is None:
            return self._coord_tool_error(
                call_id,
                "skills",
                self._skill_hint(
                    f"{verb}: skill '{name}' not found",
                    system_reminder=(
                        "Use skills(action='find', enabled_only=false) "
                        "to list every skill including currently-disabled "
                        "ones."
                    ),
                ),
            )
        if bool(existing.get("enabled")) == enable:
            return self._coord_tool_error(
                call_id,
                "skills",
                f"{verb}: skill '{name}' is already {'enabled' if enable else 'disabled'}",
            )
        # Surface the existing skill's risk profile on the approval card
        # so the operator sees *what* they're enabling before clicking
        # approve — a model-planted critical-tier skill should never be
        # re-enabled by an operator who saw only the name on the card.
        existing_risk = str(existing.get("risk_level") or "")
        try:
            existing_allowed = json.loads(existing.get("allowed_tools") or "[]")
            if not isinstance(existing_allowed, list):
                existing_allowed = []
        except (TypeError, ValueError):
            existing_allowed = []
        preview_lines = [
            f"    name: {name}",
            f"    enabled: {bool(existing.get('enabled'))} -> {enable}",
        ]
        if existing_risk:
            preview_lines.append(f"    risk_level: {existing_risk}")
        if existing_allowed:
            preview_lines.append(f"    allowed_tools: {len(existing_allowed)} entries")
        if bool(existing.get("auto_approve")) and enable:
            preview_lines.append(
                "    WARNING: this skill has auto_approve=True; re-enabling "
                "lets its allowed_tools auto-fire when it loads"
            )
        header = f"⚙ skills {verb}: {name}"
        if enable and existing_risk in ("high", "critical"):
            header += f" (risk={existing_risk})"
        return {
            "call_id": call_id,
            "func_name": "skills",
            "header": header,
            "preview": "\n".join(preview_lines),
            "needs_approval": True,
            "approval_label": f"skills__{verb}__{name}",
            "execute": self._exec_skills,
            "action": verb,
            "name": name,
            "template_id": existing["template_id"],
        }

    def _exec_skills_toggle(self, item: dict[str, Any], *, enable: bool) -> tuple[str, str]:
        from turnstone.core.storage._registry import get_storage

        call_id = item["call_id"]
        name = item["name"]
        template_id = item["template_id"]
        verb = "enable" if enable else "disable"
        # Re-check the write permission (see _require_model_skills_write).
        deny = self._require_model_skills_write(call_id, verb, {"name": name})
        if deny is not None:
            err = deny.get("error", "Error: permission denied")
            self._report_tool_result(call_id, "skills", err, is_error=True)
            return call_id, err
        storage = get_storage()
        if storage is None:
            msg = "Error: storage unavailable"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        try:
            storage.update_prompt_template(template_id, enabled=enable)
        except Exception as e:
            msg = f"Error: skills {verb} failed: {e}"
            self._report_tool_result(call_id, "skills", msg, is_error=True)
            return call_id, msg
        self._audit_skill_action(
            storage,
            f"skill.{verb}",
            template_id,
            {"name": name, "enabled": enable},
        )
        output = json.dumps(
            {"template_id": template_id, "name": name, "enabled": enable},
            separators=(",", ":"),
        )
        self._report_tool_result(call_id, "skills", f"{verb}d {name}")
        return call_id, output

    # -- shared dispatch + helpers -------------------------------------------

    def _exec_skills(self, item: dict[str, Any]) -> tuple[str, str]:
        action = item["action"]
        if action == "find":
            return self._exec_skills_find(item)
        if action == "get":
            return self._exec_skills_get(item)
        if action == "load":
            return self._exec_skills_load(item)
        if action == "create":
            return self._exec_skills_create(item)
        if action == "update":
            return self._exec_skills_update(item)
        if action == "enable":
            return self._exec_skills_toggle(item, enable=True)
        if action == "disable":
            return self._exec_skills_toggle(item, enable=False)
        msg = f"Error: unknown skills action: {action}"
        self._report_tool_result(item["call_id"], "skills", msg, is_error=True)
        return item["call_id"], msg

    def _scan_proposed_skill(self, content: str, allowed_tools: Any) -> str:
        """Run the skill scanner against a proposed final state so the
        operator approval card sees the projected risk tier.  Storage
        re-scans authoritatively on write, but surfacing the projection
        at approval time prevents "I didn't realize this would bump
        risk" surprise post-merge.

        ``allowed_tools`` may arrive as list (from args) or JSON string
        (from existing row) - normalize to JSON string for the scanner.
        Scanner errors fold to empty tier so an approval card never
        fails to render.
        """
        from turnstone.core.storage._utils import scan_skill_content

        if isinstance(allowed_tools, list):
            at_str = json.dumps(allowed_tools)
        elif isinstance(allowed_tools, str):
            at_str = allowed_tools or "[]"
        else:
            at_str = "[]"
        try:
            tier, _, _ = scan_skill_content(content, at_str)
        except Exception:
            log.debug("skills.scan_proposed_failed", exc_info=True)
            return ""
        return tier

    def _audit_skill_action(
        self,
        storage: Any,
        action: str,
        resource_id: str,
        detail: dict[str, Any],
    ) -> None:
        """Record an audit row for a model-proposed skill mutation.

        Stamps ``actor_source='model'`` and the spawning ``ws_id`` so
        post-incident review can distinguish admin-UI writes from
        approved model proposals.  ``record_audit`` already redacts
        credentials inside ``detail`` strings by default.

        Audit is the **authoritative actor-lineage trail** for skill
        writes — the ``prompt_templates`` row itself does not stamp
        ``actor_source`` on update, so a forensic reader looking at the
        row alone cannot tell whether a model touched it.  Always
        cross-reference the audit table for the full provenance story.

        Audit failure is logged at ``error`` (not ``warning``) because a
        successful write without a row is exactly the gap this trail
        exists to close — surfacing the failure loudly lets monitoring
        catch it rather than letting writes accumulate forensically dark.
        """
        from turnstone.core.audit import record_audit

        full_detail = {**detail, "actor_source": "model", "ws_id": self._ws_id}
        try:
            record_audit(
                storage,
                self._user_id,
                action,
                "skill",
                resource_id,
                full_detail,
                "",
            )
        except Exception:
            # Critical: a write succeeded without an audit row is the
            # exact gap this trail exists to prevent.  Surface as error
            # (not warning) so monitoring catches it.
            log.error("skills.audit_failed action=%s", action, exc_info=True)

    def _prepare_tasks(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a tasks action — list is auto-approved, mutations gated."""
        if self._coord_client is None:
            return self._coord_tool_error(call_id, "tasks", "coordinator client unavailable")
        action = self._coord_str_arg(args, "action").strip().lower()
        if action not in {"add", "update", "remove", "reorder", "list"}:
            return self._coord_tool_error(
                call_id,
                "tasks",
                "action must be one of: add, update, remove, reorder, list",
            )
        if action == "list":
            return {
                "call_id": call_id,
                "func_name": "tasks",
                "header": "\u2699 tasks list",
                "preview": "",
                "needs_approval": False,
                "execute": self._exec_tasks,
                "action": "list",
            }
        # --- mutating actions -------------------------------------------------
        item: dict[str, Any] = {
            "call_id": call_id,
            "func_name": "tasks",
            "needs_approval": True,
            "execute": self._exec_tasks,
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
                        call_id, "tasks", f"add: {field_name} must be a string"
                    )
            title = self._coord_str_arg(args, "title").strip()
            if not title:
                return self._coord_tool_error(call_id, "tasks", "add: title is required")
            status = self._coord_str_arg(args, "status", "pending").strip() or "pending"
            child_ws_id = self._coord_str_arg(args, "child_ws_id").strip()
            item["header"] = f"\u2699 tasks add: {title[:60]}"
            item["preview"] = f"status={status} child_ws_id={child_ws_id or '-'}"
            item["title"] = title
            item["status"] = status
            item["child_ws_id"] = child_ws_id
        elif action == "update":
            task_id = self._coord_str_arg(args, "task_id").strip()
            if not task_id:
                return self._coord_tool_error(call_id, "tasks", "update: task_id is required")
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
                        "tasks",
                        f"update: {field_name} must be a string",
                    )
            if upd_title is None and upd_status is None and upd_child is None:
                return self._coord_tool_error(
                    call_id,
                    "tasks",
                    "update: at least one of title / status / child_ws_id is required",
                )
            item["header"] = f"\u2699 tasks update: {task_id}"
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
                return self._coord_tool_error(call_id, "tasks", "remove: task_id is required")
            item["header"] = f"\u2699 tasks remove: {task_id}"
            item["preview"] = ""
            item["task_id"] = task_id
        elif action == "reorder":
            raw_ids = args.get("task_ids")
            if not isinstance(raw_ids, list) or not all(isinstance(x, str) for x in raw_ids):
                return self._coord_tool_error(
                    call_id, "tasks", "reorder: task_ids must be a list of strings"
                )
            item["header"] = f"\u2699 tasks reorder: {len(raw_ids)} ids"
            item["preview"] = ",".join(raw_ids[:6]) + ("..." if len(raw_ids) > 6 else "")
            item["task_ids"] = raw_ids
        return item

    def _exec_tasks(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        action = item["action"]
        try:
            if action == "list":
                envelope = self._coord_client.tasks_get(self._ws_id)
                tasks = envelope.get("tasks", [])
                truncated = len(tasks) > 200
                tasks = tasks[:200]
                result: dict[str, Any] = {"tasks": tasks, "truncated": truncated}
            elif action == "add":
                result = self._coord_client.tasks_add(
                    self._ws_id,
                    title=item["title"],
                    status=item["status"],
                    child_ws_id=item["child_ws_id"],
                )
            elif action == "update":
                result = self._coord_client.tasks_update(
                    self._ws_id,
                    task_id=item["task_id"],
                    title=item["title"],
                    status=item["status"],
                    child_ws_id=item["child_ws_id"],
                )
            elif action == "remove":
                result = self._coord_client.tasks_remove(self._ws_id, task_id=item["task_id"])
            elif action == "reorder":
                result = self._coord_client.tasks_reorder(self._ws_id, task_ids=item["task_ids"])
            else:  # unreachable — _prepare validated the enum
                result = {"error": f"unknown action: {action}"}
        except Exception as e:
            msg = f"Error: tasks {action} failed: {e}"
            self._report_tool_result(call_id, "tasks", msg, is_error=True)
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
        self._report_tool_result(call_id, "tasks", summary, is_error=is_error)
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
        raw_since = args.get("since")
        since_hint = raw_since if isinstance(raw_since, dict) else None
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
            "since": since_hint,
        }

    def _exec_wait_for_workstream(self, item: dict[str, Any]) -> tuple[str, str]:
        call_id = item["call_id"]
        clean_ws_ids = item["ws_ids"]
        timeout_val = item["timeout"] if item["timeout"] is not None else 60.0
        mode_val = item["mode"] if item["mode"] is not None else "any"
        # Emit a ``wait_started`` SSE event so the coordinator sidebar
        # can show a "waiting on N children, T elapsed" indicator while
        # the worker thread blocks inside wait_for_workstream (the tool
        # can otherwise pin the worker for up to 600s with no UI
        # signal).  Best-effort — swallow failures so a broken UI never
        # blocks a model-invoked wait (#14).
        self._emit_wait_event(
            "wait_started",
            {
                "call_id": call_id,
                "ws_ids": clean_ws_ids,
                "mode": mode_val,
                "timeout": timeout_val,
            },
        )

        # Throttle wait_progress emission (#perf-3).  The wait loop polls
        # every 0.5s; emitting on every tick with the full results dict
        # would flood each SSE listener's maxsize=500 queue — a 600s
        # wait produces 1200 events per listener, pushing out unrelated
        # state_change / content events via put_nowait drop.  Emit only
        # when the polled snapshot actually differs from the last
        # emitted snapshot, OR when at least ~5s has elapsed since the
        # last emission (so a stuck wait still shows a heartbeat for
        # the operator).  The sidebar indicator only needs
        # seconds-granularity elapsed; the full results dict is only
        # useful on transitions, so dropping redundant ticks is free.
        progress_state: dict[str, Any] = {
            "last_snap": None,
            "last_emit_mono": 0.0,
        }
        progress_heartbeat_s = 5.0

        def _progress(snap: dict[str, Any], elapsed: float) -> None:
            # Cooperative cancel seam.  ``wait_for_workstream`` holds no
            # cancel handle and its wait loop blocks on the ChildEventBus
            # (woken only by *child* state changes), so without this a
            # cancelled coordinator parked in a wait stays pinned for up to
            # WAIT_MAX_TIMEOUT (600s).  The loop calls this callback every
            # ~2s heartbeat and wraps it in ``except Exception`` — but
            # ``GenerationCancelled`` is a ``BaseException``, so raising
            # here propagates cleanly out of the wait into the send() cancel
            # handler.  ~2s abort instead of up to 600s.
            self._check_cancelled()
            now = time.monotonic()
            changed = snap != progress_state["last_snap"]
            heartbeat_due = (now - progress_state["last_emit_mono"]) >= progress_heartbeat_s
            if not changed and not heartbeat_due:
                return
            payload: dict[str, Any] = {
                "call_id": call_id,
                "elapsed": round(elapsed, 3),
            }
            # Attach the full results dict only on transitions — a
            # heartbeat-only tick reports progress (liveness) without
            # the per-listener payload cost.
            if changed:
                payload["results"] = snap
            self._emit_wait_event("wait_progress", payload)
            progress_state["last_snap"] = snap
            progress_state["last_emit_mono"] = now

        try:
            result = self._coord_client.wait_for_workstream(
                clean_ws_ids,
                timeout=timeout_val,
                mode=mode_val,
                since=item.get("since"),
                progress_callback=_progress,
            )
        except Exception as e:
            msg = f"Error: wait_for_workstream failed: {e}"
            self._report_tool_result(call_id, "wait_for_workstream", msg, is_error=True)
            self._emit_wait_event(
                "wait_ended",
                {"call_id": call_id, "complete": False, "error": str(e)},
            )
            return call_id, msg
        # Surface client-side validation errors as tool errors rather
        # than rendering them as a "successful" wait result.
        if result.get("error"):
            if result.get("not_found") or result.get("invalid_ws_ids"):
                # Unresolvable-id failures carry a structured recovery
                # payload — per-id ``did_you_mean``, the children
                # roster, and (on the in-loop abort) live ``results``
                # for the still-observable lanes.  Serialize the whole
                # object so the model can fix the id and re-issue; a
                # bare-string collapse would discard exactly the hints
                # the client built for it.
                msg = "Error: " + json.dumps(result, separators=(",", ":"), default=str)
            else:
                msg = f"Error: {result['error']}"
            self._report_tool_result(call_id, "wait_for_workstream", msg, is_error=True)
            self._emit_wait_event(
                "wait_ended",
                {"call_id": call_id, "complete": False, "error": result["error"]},
            )
            return call_id, msg
        output = json.dumps(result, separators=(",", ":"), default=str)
        elapsed = result.get("elapsed", 0.0)
        complete = result.get("complete", False)
        # Count children that genuinely finished work (real terminals
        # only — ``not_found`` is a rejection, not a resolution).  Earlier
        # versions counted any non-empty ``state`` and inverted the
        # truth on timeout (rendered as ``"timeout (N/N resolved)"``).
        # Inline import — ``turnstone.core`` shouldn't import from
        # ``turnstone.console`` at module load (layering), so the
        # tool-exec read pulls the canonical state set lazily.
        from turnstone.console.coordinator_client import WAIT_REAL_TERMINAL_STATES

        results_dict = result.get("results") or {}
        resolved_count = sum(
            1
            for snap in results_dict.values()
            if isinstance(snap, dict) and snap.get("state") in WAIT_REAL_TERMINAL_STATES
        )
        verb = "complete" if complete else "timeout"
        # Denominator = polled set (not raw item['ws_ids']) so the ratio
        # stays coherent with what the client actually tracked after dedup.
        summary = f"{verb} after {elapsed}s ({resolved_count}/{len(results_dict)} resolved)"
        self._report_tool_result(call_id, "wait_for_workstream", summary)
        self._emit_wait_event(
            "wait_ended",
            {
                "call_id": call_id,
                "complete": complete,
                "elapsed": elapsed,
                "results": results_dict,
                "resolved": resolved_count,
            },
        )
        return call_id, self._truncate_output(output)

    def _emit_wait_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fan out a ``wait_*`` SSE event via the session UI.

        Used by the coordinator-side wait dashboard (#14) so the sidebar
        can render a "waiting on N children, T elapsed" indicator while
        the worker thread blocks inside wait_for_workstream.  Best-effort:
        no UI, no ``_enqueue`` method, or a raising enqueue all swallow
        silently — the wait itself must never break because of observer
        plumbing.
        """
        ui = getattr(self, "ui", None)
        enqueue = getattr(ui, "_enqueue", None)
        if enqueue is None:
            return
        try:
            enqueue({"type": event_type, **payload})
        except Exception:
            log.debug("wait_event.enqueue_failed type=%s", event_type, exc_info=True)

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
            # None (field omitted) means "leave unset": the upsert keeps the
            # stored value on update and defaults on insert; an explicit value
            # (including "" / "general") overwrites.
            description = args.get("description")
            if description is not None:
                description = str(description).strip()
            mem_type = args.get("type")
            if mem_type is not None:
                mem_type = str(mem_type).strip().lower()
                if mem_type not in ("user", "general", "feedback", "reference"):
                    # An unrecognized type (e.g. a typo) is treated as unset
                    # (preserve the stored type on update / default on insert)
                    # rather than silently overwriting it with "general".
                    mem_type = None
            # Default scope is kind-aware: coord sessions default to
            # ``coordinator`` (their only writable scope); IC sessions
            # default to ``global`` (matches pre-fix behaviour).
            default_scope = self._default_memory_scope()
            scope = (args.get("scope") or default_scope).strip().lower()
            if scope not in _VALID_MEMORY_SCOPES:
                scope = default_scope
            scope_err = self._validate_scope(scope, call_id)
            if scope_err:
                return scope_err
            if scope == "project" and not self._project_writable:
                return {
                    "call_id": call_id,
                    "func_name": "memory",
                    "header": "\u2717 memory save: project is read-only for you",
                    "preview": "",
                    "needs_approval": False,
                    "error": (
                        "Error: you have read-only access to this project; you "
                        "cannot save project-scoped memory."
                    ),
                }
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
            valid_scopes = _VALID_MEMORY_SCOPES
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
                # Implicit fallback walk \u2014 kind-aware narrowest-to-widest.
                # Coord sessions only walk ``coordinator``; IC sessions
                # walk workstream \u2192 user \u2192 global.  See
                # :meth:`_implicit_scope_walk`.
                scopes_to_try = []
                for s in self._implicit_scope_walk():
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
            valid_scopes = _VALID_MEMORY_SCOPES
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
                # _validate_scope gates on READ access (_project_id); deleting a
                # shared project row is a WRITE, so gate it on _project_writable
                # too — mirrors the save path so a read-only member of a public
                # project can't destroy project-scoped memory.  (The implicit
                # walk below never includes project scope, so it needs no gate.)
                if explicit_scope == "project" and not self._project_writable:
                    return {
                        "call_id": call_id,
                        "func_name": "memory",
                        "header": "✗ memory delete: project is read-only for you",
                        "preview": "",
                        "needs_approval": False,
                        "error": (
                            "Error: you have read-only access to this project; you "
                            "cannot delete project-scoped memory."
                        ),
                    }
                scope_id = self._resolve_scope_id(explicit_scope)
                scopes_to_try = [(explicit_scope, scope_id)]
            else:
                # Kind-aware implicit walk — coord sessions stay in coord-scope;
                # IC sessions walk narrowest-to-widest (workstream → user → global).
                # See :meth:`_implicit_scope_walk`.
                scopes_to_try = []
                for s in self._implicit_scope_walk():
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
            if mem_type and mem_type not in ("user", "general", "feedback", "reference"):
                mem_type = ""
            scope = (args.get("scope") or "").strip().lower()
            if scope and scope not in _VALID_MEMORY_SCOPES:
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
            if mem_type and mem_type not in ("user", "general", "feedback", "reference"):
                mem_type = ""
            scope = (args.get("scope") or "").strip().lower()
            if scope and scope not in _VALID_MEMORY_SCOPES:
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
            # Pin the tenancy scope at prepare time: an item that sits in
            # the queue must search as the user whose turn requested it,
            # not whoever binds the session later (same discipline as
            # ``mcp_user_id`` in ``_prepare_mcp_tool``).
            "scope_user_id": self._history_scope_user_id(),
        }

    # -- skill prepare/execute -------------------------------------------------

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
            # Pin the credential identity at prepare time: an item that
            # sits pending approval must execute under the user whose
            # turn requested it, not whoever binds the session later.
            "mcp_user_id": self._mcp_effective_user_id,
        }

    def _exec_mcp_tool(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute an MCP tool call via the MCPClientManager."""
        self._check_cancelled()
        call_id: str = item["call_id"]
        func_name: str = item["mcp_func_name"]
        args: dict[str, Any] = item["mcp_args"]

        assert self._mcp_client is not None
        mcp_error = False
        mcp_status: EffectStatus | None = None
        try:
            output = self._mcp_client.call_tool_sync(
                func_name,
                args,
                user_id=item.get("mcp_user_id", self._mcp_effective_user_id),
                timeout=self.tool_timeout,
                is_interactive_for_consent=self._is_interactive_for_consent,
            )
        except TimeoutError:
            # An MCP tool is an opaque action — the server may have run it to
            # completion before we stopped waiting, so the outcome is unobserved.
            # Read UNKNOWN, never none (HYPOTHESIS.md effect-record appendix).
            # Resource/prompt reads below stay plain: they are idempotent reads
            # with nothing to reconcile.
            output = f"MCP tool timed out after {self.tool_timeout}s. {TIMEOUT_OUTCOME_CLAUSE}"
            mcp_error = True
            mcp_status = EffectStatus.UNKNOWN
            self.ui.on_error(output)
        except Exception as e:
            output = _format_mcp_dispatch_error("MCP tool error", e)
            mcp_error = True
            self.ui.on_error(output)

        output = self._truncate_output(output)
        self._report_tool_result(call_id, func_name, output, is_error=mcp_error, status=mcp_status)
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
            # Pinned at prepare time — see _prepare_mcp_tool.
            "mcp_user_id": self._mcp_effective_user_id,
        }

    def _exec_read_resource(self, item: dict[str, Any]) -> tuple[str, str]:
        """Read an MCP resource by URI."""
        self._check_cancelled()
        call_id: str = item["call_id"]
        uri: str = item["resource_uri"]

        assert self._mcp_client is not None
        mcp_error = False
        try:
            # Per-user pool dispatch (Phase 7b): when ``user_id`` is set
            # and the URI resolves to an oauth_user pool entry, the read
            # goes through the per-(user, server) pool with token /
            # 401 / 403 / consent-required handling. Otherwise the
            # static path runs byte-identical (invariant 1).
            output = self._mcp_client.read_resource_sync(
                uri,
                user_id=item.get("mcp_user_id", self._mcp_effective_user_id),
                timeout=self.tool_timeout,
                is_interactive_for_consent=self._is_interactive_for_consent,
            )
        except TimeoutError:
            output = f"MCP resource read timed out after {self.tool_timeout}s"
            mcp_error = True
            self.ui.on_error(output)
        except Exception as e:
            # exc_info=True would let chained ``__context__`` carry an
            # ``httpx.Request`` whose ``Authorization`` header holds the
            # per-user bearer (Phase 7b pool dispatch path). Use
            # structured fields with ``type(e).__name__`` only.
            log.warning("mcp.resource_read_failed", uri=uri, error=type(e).__name__)
            output = _format_mcp_dispatch_error("MCP resource error", e)
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
        if not self._mcp_client.is_mcp_prompt(name, user_id=self._mcp_effective_user_id):
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
            # Pinned at prepare time — see _prepare_mcp_tool.
            "mcp_user_id": self._mcp_effective_user_id,
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
            # Per-user pool dispatch (Phase 7b): structured-error
            # responses (consent required, decrypt failure, insufficient
            # scope, etc.) surface here as ``RuntimeError`` carrying the
            # JSON payload — caught by the broad ``except Exception``
            # below so the agent renders the error message.
            messages = self._mcp_client.get_prompt_sync(
                name,
                arguments or None,
                user_id=item.get("mcp_user_id", self._mcp_effective_user_id),
                timeout=self.tool_timeout,
                is_interactive_for_consent=self._is_interactive_for_consent,
            )
            output = "\n\n".join(f"[{m['role']}]: {m['content']}" for m in messages)
        except TimeoutError:
            output = f"MCP prompt timed out after {self.tool_timeout}s"
            mcp_error = True
            self.ui.on_error(output)
        except Exception as e:
            # See ``_exec_read_resource`` for the no-``exc_info`` rationale —
            # bearer-leak via chained ``httpx.Request`` __context__.
            log.warning("mcp.prompt_invoke_failed", name=name, error=type(e).__name__)
            output = _format_mcp_dispatch_error("MCP prompt error", e)
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
                # SIGKILL'd mid-flight (the command was parked on a silent
                # read, so the in-loop cooperative check never fired).  Its
                # side effects are unobserved: record outcome UNKNOWN and
                # mark it an error, not a clean empty success — a destructive
                # command killed here must not read as "did not run" on
                # replay.  Keep whatever partial stdout we captured.
                partial = "".join(stdout_parts).strip()
                msg = (
                    "Cancelled by user. Outcome UNKNOWN — the command was "
                    "stopped mid-execution; it may have run partially or had "
                    "side effects. Do not assume it did not run."
                )
                if partial:
                    msg += "\n\nPartial output before cancel:\n" + self._truncate_output(partial)
                self._report_tool_result(
                    call_id, "bash", msg, is_error=True, status=EffectStatus.UNKNOWN
                )
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
            # The watchdog SIGKILL'd the command at its deadline — the same
            # mid-flight kill as the cooperative-cancel branch above, so its
            # side effects are equally unobserved.  Read UNKNOWN, never a flat
            # failure that invites a blind re-run (HYPOTHESIS.md effect-record
            # appendix: unknown, never none).  Keep any partial stdout captured
            # before the kill, exactly as the cancel path does.
            msg = f"Command timed out after {timeout}s. {TIMEOUT_OUTCOME_CLAUSE}"
            partial = "".join(stdout_parts).strip()
            if partial:
                msg += "\n\nPartial output before timeout:\n" + self._truncate_output(partial)
            self._report_tool_result(
                call_id, "bash", msg, is_error=True, status=EffectStatus.UNKNOWN
            )
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
            self._current_read_files.discard(resolved)
            self._report_tool_result(call_id, "read_file", err, is_error=True)
            return call_id, err

        self._current_read_files.add(resolved)
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
                self._current_read_files.discard(resolved)
                msg = f"Error: {path}: {e}"
                self._report_tool_result(call_id, "read_file", msg, is_error=True)
                return call_id, msg
            self._current_read_files.add(resolved)
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
            self._current_read_files.discard(resolved)
            msg = f"Error: {path} not found"
            self._report_tool_result(call_id, "read_file", msg, is_error=True)
            return call_id, msg
        except Exception as e:
            self._current_read_files.discard(resolved)
            msg = f"Error reading {path}: {e}"
            self._report_tool_result(call_id, "read_file", msg, is_error=True)
            return call_id, msg

        if len(raw) > _IMAGE_SIZE_CAP:
            self._current_read_files.discard(resolved)
            size_mb = len(raw) / (1024 * 1024)
            cap_mb = _IMAGE_SIZE_CAP / (1024 * 1024)
            msg = (
                f"Error: image {path} is {size_mb:.1f} MB, "
                f"exceeds {cap_mb:.0f} MB limit for vision."
            )
            self._report_tool_result(call_id, "read_file", msg, is_error=True)
            return call_id, msg

        self._current_read_files.add(resolved)
        mime, _ = mimetypes.guess_type(path)
        if not mime:
            mime = "image/png"

        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": f"Image file: {path} ({len(raw):,} bytes)"},
            {"type": "image_url", "image_url": {"url": _encode_image_data_uri(raw, mime)}},
        ]

        self._report_tool_result(call_id, "read_file", f"image ({len(raw):,} bytes)")
        return call_id, content_parts

    def _search_capture(self, args: list[str]) -> tuple[bytes, int, bytes, bool]:
        """Run a search subprocess with a streaming, byte-capped stdout read.

        Returns ``(stdout, returncode, stderr, capped)``. Drains stderr in a
        background thread to avoid pipe deadlock when the child writes a lot
        to stderr while we're still reading stdout.

        The byte cap is the load-bearing defense for pathological inputs
        (multi-GB JSONL, single-line minified bundles): on overflow, we
        kill the child and trim to the last newline so the parser never
        sees a partial trailing line.
        """
        from turnstone.core.env import scrubbed_env

        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=scrubbed_env(),
        )

        # Watchdog: ``proc.stdout.read`` is a blocking pipe read with no
        # timeout, so a child stuck in kernel I/O (NFS, FUSE, broken
        # backend) would hang forever despite ``tool_timeout``. The timer
        # arms ``proc.kill`` after the deadline; we detect "child was
        # killed by us, not by the byte cap" by setting ``timed_out``
        # before invoking kill.
        timed_out = [False]

        def _watchdog() -> None:
            timed_out[0] = True
            with contextlib.suppress(Exception):
                proc.kill()

        watchdog = threading.Timer(self.tool_timeout, _watchdog)
        watchdog.daemon = True
        watchdog.start()

        stderr_chunks: list[bytes] = []

        def _drain_stderr() -> None:
            if not proc.stderr:
                return
            total = 0
            try:
                while total < _SEARCH_STDERR_CAP:
                    chunk = proc.stderr.read(_SEARCH_DRAIN_CHUNK)
                    if not chunk:
                        return
                    stderr_chunks.append(chunk)
                    total += len(chunk)
                while proc.stderr.read(_SEARCH_DRAIN_CHUNK):
                    pass  # discard tail so the child can finish writing
            except Exception:
                pass  # best-effort: pipe may be torn down by proc.kill()/child exit

        drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
        drain_thread.start()

        capped = False
        try:
            stdout = proc.stdout.read(_SEARCH_RAW_BYTE_CAP + 1) if proc.stdout else b""
            if len(stdout) > _SEARCH_RAW_BYTE_CAP:
                capped = True
                proc.kill()
                stdout = stdout[:_SEARCH_RAW_BYTE_CAP]
                last_nl = stdout.rfind(b"\n")
                if last_nl >= 0:
                    stdout = stdout[:last_nl]
            rc = proc.wait()
        finally:
            watchdog.cancel()
            drain_thread.join(timeout=_SEARCH_DRAIN_JOIN_TIMEOUT)
            for stream in (proc.stdout, proc.stderr):
                # best-effort: pipe may be torn down by proc.kill()/OS
                with contextlib.suppress(Exception):
                    if stream is not None:
                        stream.close()

        if timed_out[0]:
            raise subprocess.TimeoutExpired(args, self.tool_timeout)
        return stdout, rc, b"".join(stderr_chunks), capped

    def _exec_search(self, item: dict[str, Any]) -> tuple[str, str]:
        """Search file contents for a regex pattern via ripgrep (preferred) or grep."""
        call_id = item["call_id"]
        pattern, path = item["pattern"], item["path"]
        try:
            backend = _detect_search_backend()
            args = _build_search_args(pattern, path, backend)
            stdout, rc, stderr, capped = self._search_capture(args)

            # ripgrep and grep share rc semantics: 0 = matches, 1 = no
            # matches, ≥2 = error. When ``capped`` is True we killed the
            # child intentionally (byte-cap), so a negative rc is from
            # our SIGKILL — normalise to 0 so the partial output flows
            # through. But preserve a non-negative rc: there's a narrow
            # race where the child can exit naturally between our read
            # and our kill, and we don't want to silently swallow rg's
            # rc=2 ("matches found but some files had errors") just
            # because we also tripped the byte cap.
            if capped and rc < 0:
                rc = 0

            if rc == 1:
                self._report_tool_result(call_id, "search", "no matches")
                return call_id, "(no matches)"
            if rc < 0:
                # Signal-killed by something other than us (OOM killer,
                # external SIGTERM). Surface it instead of parsing the
                # truncated stdout as if the search had completed.
                msg = f"{backend} killed by signal {-rc}"
                self._report_tool_result(call_id, "search", msg, is_error=True)
                return call_id, msg
            if rc > 1:
                err_text = stderr.decode("utf-8", errors="replace").strip()
                msg = err_text or f"{backend} error (exit {rc})"
                self._report_tool_result(call_id, "search", msg, is_error=True)
                return call_id, msg

            records = _parse_search_records(stdout)
            original_len = len(stdout)

            if not records:
                if capped:
                    # The byte cap fired before any parseable line
                    # completed (typical shape: a single multi-MB line
                    # without a newline, e.g. minified bundle / training
                    # JSONL record). The malformed-output message would
                    # blame the query; surface the real cause instead.
                    msg = (
                        "(search output exceeded the raw byte cap before "
                        "any parseable line completed — narrow your query "
                        "or restrict the path)"
                    )
                    self._report_tool_result(call_id, "search", "byte cap hit", is_error=True)
                    return call_id, msg
                # rc 0 with no parseable records means matches were found
                # but every line was malformed.
                self._report_tool_result(call_id, "search", "all matches malformed", is_error=True)
                return call_id, _SEARCH_ALL_TRUNCATED_MSG

            output = _format_search_results(records, capped)
            output = self._truncate_output(output)  # belt-and-suspenders

            match_count = len(records)
            desc = f"{match_count} matches"
            if original_len > 500:
                desc += f" ({original_len} bytes raw)"
            if capped:
                desc += " [capped]"
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
        self._current_read_files.add(resolved_a)

        if path_b:
            label_b = path_b
            lines_b, resolved_b, err = self._read_text_lines(path_b)
            if err:
                self._report_tool_result(call_id, "diff_file", err, is_error=True)
                return call_id, err
            self._current_read_files.add(resolved_b)
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

    def _note_agent_child(self, child_call_id: str, parent_call_id: str | None) -> None:
        """Register a sub-agent tool call under its parent task_agent call so the
        UI can nest the step (see ``SessionUIBase.note_agent_child``).  No-op when
        there's no parent (a top-level ``_run_agent``) or the UI doesn't support
        it (CLI / eval / fixtures)."""
        if not parent_call_id:
            return
        fn = getattr(self.ui, "note_agent_child", None)
        if fn is not None:
            fn(child_call_id, parent_call_id)

    def _clear_agent_children(self, parent_call_id: str | None) -> None:
        """Drop a finished task agent's child registrations — getattr-guarded
        twin of :meth:`_note_agent_child`."""
        if not parent_call_id:
            return
        fn = getattr(self.ui, "clear_agent_children", None)
        if fn is not None:
            fn(parent_call_id)

    def _paint_agent_step(self, parent_call_id: str | None, item: dict[str, Any]) -> None:
        """Paint a sub-agent's auto-tool step (web pending row / CLI leg) — the
        typed successor to the old per-turn ``on_info`` leg.  No-op without a
        parent or on a UI that doesn't render agent steps (eval / fixtures)."""
        if not parent_call_id:
            return
        fn = getattr(self.ui, "on_agent_step", None)
        if fn is not None:
            fn(parent_call_id, item)

    def _begin_agent_scope(self) -> None:
        """Mark a task agent as in flight so the web pane drops its ``on_info``
        progress chatter (it can't nest under the card — no call_id).  Bracketed
        with :meth:`_end_agent_scope`; getattr-guarded → no-op on the CLI (keeps
        its info lines) and on eval / fixtures."""
        fn = getattr(self.ui, "begin_agent_scope", None)
        if fn is not None:
            fn()

    def _end_agent_scope(self) -> None:
        """Leave a task agent's scope — getattr-guarded twin of
        :meth:`_begin_agent_scope`."""
        fn = getattr(self.ui, "end_agent_scope", None)
        if fn is not None:
            fn()

    @staticmethod
    def _clip_with_count(text: str, cap: int) -> str:
        """Head-clip ``text`` to ``cap`` chars with a uniform truncation marker.
        The one format for sub-agent output clips — the in-loop 16k clip and the
        recall per-step cap — distinct from the token-budget-aware
        :meth:`_truncate_output`."""
        if len(text) <= cap:
            return text
        return text[:cap] + f"\n\n... (truncated from {len(text)} chars)"

    @staticmethod
    def _iter_agent_tool_results(
        agent_turns: list[Turn],
    ) -> Iterator[tuple[ToolCall, Turn | None]]:
        """Yield ``(tool_call, result_turn_or_None)`` for every sub-tool the
        sub-agent issued, in order, pairing each call to its result FIFO per
        call_id — a queue per id consumed once, NOT a last-wins dict, so a local
        provider that reuses ids across turns (``call_0`` …) can't collapse
        distinct calls onto one result.  Shared by :meth:`_project_agent_steps`
        (recall) and :meth:`_cancel_ledger` (cancel disposition)."""
        pending: dict[str, collections.deque[Turn]] = {}
        for t in agent_turns:
            if t.role is Role.TOOL and t.tool_call_id:
                pending.setdefault(t.tool_call_id, collections.deque()).append(t)
        for t in agent_turns:
            if t.role is not Role.ASSISTANT:
                continue
            for tc in t.tool_calls:
                q = pending.get(tc.id)
                yield tc, (q.popleft() if q else None)

    @staticmethod
    def _project_agent_steps(agent_turns: list[Turn]) -> list[dict[str, Any]]:
        """Project a finished sub-agent's trajectory into recall step items for
        the task card — one per sub-tool call, matched to its result (FIFO per
        call_id via :meth:`_iter_agent_tool_results`).

        Reads the tool turn's payload directly rather than via ``Turn.text``,
        which raises on a multimodal ``list[dict]`` result (the deferred-finding
        landmine); a non-``str`` payload becomes a light placeholder so the
        recall stays text-only and ``/history`` doesn't carry image bytes.  Each
        step's output AND arguments are capped (:data:`_AGENT_STEP_OUTPUT_CAP`)
        and the step COUNT (:data:`_AGENT_STEP_COUNT_CAP`); on overflow the most
        RECENT steps are kept (a sub-agent's latest edits/writes matter more on
        recall than its opening searches) behind an honest leading marker — so
        the stash stays memory-bounded even for a 100+-tool agent and never
        silently under-reports its step count."""
        pairs = list(ChatSession._iter_agent_tool_results(agent_turns))
        dropped = max(0, len(pairs) - _AGENT_STEP_COUNT_CAP)
        # Build dicts only for the retained tail, not the dropped head.
        tail = pairs[-_AGENT_STEP_COUNT_CAP:] if dropped else pairs
        steps: list[dict[str, Any]] = []
        if dropped:
            # Leading marker naming how many earlier steps fell out — honest, and
            # correct now that the RETAINED steps are the recent ones.
            steps.append(
                {
                    "id": "",
                    "name": "…",
                    "arguments": "{}",
                    "output": f"(+{dropped} earlier steps not retained)",
                    "is_error": False,
                }
            )
        for tc, res in tail:
            output, is_error = "", False
            if res is not None:
                is_error = res.is_error
                raw = (
                    res.content[0].text
                    if res.content and isinstance(res.content[0], TextBlock)
                    else ""
                )
                output = raw if isinstance(raw, str) else "[non-text result]"
            steps.append(
                {
                    "id": tc.id,
                    "name": tc.name,
                    # Clip arguments too: a write_file/edit_file call carries full
                    # file content here, which the per-step output cap alone would
                    # leave unbounded in the in-memory stash.
                    "arguments": ChatSession._clip_with_count(tc.arguments, _AGENT_STEP_OUTPUT_CAP),
                    "output": ChatSession._clip_with_count(output, _AGENT_STEP_OUTPUT_CAP),
                    "is_error": is_error,
                }
            )
        return steps

    def _stash_agent_trajectory(self, call_id: str | None, agent_turns: list[Turn]) -> None:
        """Retain a finished task agent's projected sub-trajectory for ``/history``
        card recall (getattr-guarded → no-op on CLI / eval / fixtures).  Called
        from ``_exec_task``'s ``finally`` so it captures the full record on
        success and the partial one on cancel/error — both honest."""
        if not call_id:
            return
        fn = getattr(self.ui, "stash_agent_trajectory", None)
        if fn is not None:
            fn(call_id, self._project_agent_steps(agent_turns))

    def _run_agent(
        self,
        agent_turns: list[Turn],
        label: str = "agent",
        tools: list[dict[str, Any]] | None = None,
        auto_tools: set[str] | None = None,
        reasoning_effort: str | None = None,
        agent_alias: str | None = None,
        parent_call_id: str | None = None,
    ) -> str:
        """Run an autonomous agent loop.

        Args:
            agent_turns: Pre-built sub-harness trajectory (system + user) as
                neutral ``Turn`` objects, lowered to wire dicts at the API
                boundary.  Mutated in place — every assistant turn and tool
                result is appended as the loop runs.
            label: Display prefix for progress lines (e.g. "task").
            tools: Tool definitions to send to the API. Defaults to the
                session's task tool set.
            auto_tools: Set of tool names the agent may execute. Defaults to
                TASK_AUTO_TOOLS.
            reasoning_effort: Override reasoning effort for this agent.
            agent_alias: Per-call model alias override (the LLM passed
                ``model="<alias>"`` to task_agent).  Wins over
                the registry's per-kind resolution when set.  Caller is
                expected to have validated the alias against the registry;
                an unknown alias here raises ``ValueError``.
            parent_call_id: The task_agent call_id this sub-agent runs under,
                threaded so each sub-tool's events get tagged for UI nesting.
                ``None`` for a top-level run (no nesting).

        Returns:
            Final content string from the agent.
        """
        if tools is None:
            tools = self._task_tools
        if auto_tools is None:
            auto_tools = TASK_AUTO_TOOLS
        max_tool_turns = self.agent_max_turns

        # Resolve agent model: explicit per-call override wins, then the
        # per-kind registry override (task_model), then the legacy single-
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
            # When falling through to the session's primary model, use the
            # session's primary alias for capability and server_compat
            # resolution so the agent sees the same caps as the main loop.
            agent_alias = self._model_alias

        # Per-kind reasoning effort.  Explicit caller arg wins; otherwise
        # delegate to the registry which knows the per-kind default (task
        # returns None to inherit the session).
        if reasoning_effort is None and self._registry:
            reasoning_effort = self._registry.resolve_agent_effort(label)

        # Gate web_search: remove when no backend exists for the agent model
        agent_caps = self._resolve_capabilities(agent_provider, agent_model, agent_alias)
        if not agent_caps.supports_web_search and not self._resolve_search_client():
            tools = _without_tool(tools, "web_search")

        # Build extra params for agent calls — resolve server compat from the
        # agent's own model alias, not the session's primary model.
        agent_extra = self._provider_extra_params(
            provider=agent_provider,
            model_alias=agent_alias,
        )

        def _api_call(
            turns: list[Turn],
            _tools: list[dict[str, Any]] | None = tools,
        ) -> CompletionResult:
            # NOTE: Phase 5 vLLM ``reasoning`` field replay is intentionally
            # NOT wired here.  Agent assistant messages are built from
            # ``CompletionResult.content + tool_calls`` only (no
            # ``_provider_content`` carried), so the helper would no-op
            # every turn anyway.  Task agents are excluded from the
            # persistence/replay contract — their conversation history
            # is in-memory and rebuilt per ``_run_agent`` invocation.
            # Lower the trajectory once, not once per retry attempt — ``turns``
            # is invariant across attempts (the retry path only sleeps and
            # re-sends the same messages).
            wire = dicts_from_turns(turns)
            last_err: Exception | None = None
            for attempt in range(self._MAX_RETRIES + 1):
                try:
                    agent_result = agent_provider.create_completion(
                        client=agent_client,
                        model=agent_model,
                        messages=wire,
                        tools=_tools,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                        reasoning_effort=reasoning_effort or self.reasoning_effort,
                        extra_params=agent_extra,
                        capabilities=agent_caps,
                        replay_reasoning_to_model=self._resolve_replay_reasoning_to_model(
                            agent_alias, caps=agent_caps
                        ),
                    )
                    # Sub-agent turns bypass on_status — record per-turn so
                    # task-agent spend is visible in the dashboard, attributed
                    # to the agent's own model.
                    self._record_aux_usage(agent_result, model=agent_model)
                    return agent_result
                except Exception as e:
                    ename = type(e).__name__
                    if self._stop_retrying(e, attempt, agent_provider):
                        # Overflow is deterministic — raise straight to the
                        # context-limit handler below, no backoff.
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
                result = _api_call(agent_turns)
            except Exception as e:
                # Terminal API error: overflow, or any non-retryable error that
                # escaped the retry loop above.  Salvage the sub-agent's partial work
                # rather than crash the whole task — losing a substantial synthesis to
                # a late failure is worse than returning it with a note.  (This must
                # NOT be narrowed to overflow only: a non-overflow terminal error used
                # to be salvaged too, via the old "context"/"token" text gate, and
                # narrowing it discarded partial work on e.g. a persistent timeout.)
                # GenerationCancelled is a BaseException, so a real cancel still
                # propagates past this ``except Exception``.
                overflow = _is_ctx_overflow(e)
                note = "context limit reached" if overflow else f"error ({type(e).__name__})"
                for t in reversed(agent_turns):
                    if t.role is Role.ASSISTANT and t.text:
                        self.ui.on_info(f"[{label}] {note}, returning partial work")
                        return self._guard_subagent_synthesis(t.text, label)
                # No partial work to salvage: surface overflow as a calm stop message,
                # but re-raise any other terminal error so the real failure isn't
                # masked as an empty success.
                if overflow:
                    self.ui.on_info(f"[{label}] context limit reached, stopping early")
                    return f"({label} stopped: context limit exceeded)"
                raise

            # Handle truncation or content filter — stop agent early
            if result.finish_reason == "length":
                self.ui.on_info(f"[{label}] response truncated, stopping early")
                return self._guard_subagent_synthesis(result.content or "(truncated)", label)
            if result.finish_reason == "content_filter":
                self.ui.on_info(f"[{label}] blocked by content filter")
                return "(content filter)"

            # Append the assistant turn to the sub-harness trajectory.
            agent_tool_calls: tuple[ToolCall, ...] = ()
            if result.tool_calls:
                self._ensure_tool_call_ids(result.tool_calls)
                # Namespace sub-agent tool ids by the parent task_agent so the
                # UI nesting registry can't collide across concurrent task
                # agents whose (local) provider reuses sequential ids ("call_0").
                # Tool-call ids are opaque correlation tokens — a provider
                # validates only intra-request assistant/tool consistency on
                # replay, never against its own prior generation — so rewriting
                # them in this ephemeral sub-conversation is wire-safe.  Skipped
                # for a top-level run (no parent → no nesting).
                if parent_call_id:
                    for tc in result.tool_calls:
                        tc["id"] = f"{parent_call_id}::{tc['id']}"
                agent_tool_calls = tuple(
                    ToolCall(
                        id=tc["id"],
                        name=tc.get("function", {}).get("name", ""),
                        arguments=tc.get("function", {}).get("arguments", ""),
                    )
                    for tc in result.tool_calls
                )
            agent_turns.append(Turn.assistant(result.content or "", tool_calls=agent_tool_calls))

            if not result.tool_calls:
                content = result.content or "(no output)"
                self.ui.on_info(f"[{label} done] {len(content)} chars")
                return self._guard_subagent_synthesis(content, label)

            # Execute tools sequentially (not parallel) to avoid
            # concurrent _read_files mutation from worker threads.
            tool_names = {t["function"]["name"] for t in tools}
            for tc_dict in result.tool_calls:
                self._check_cancelled()
                tool_name = tc_dict["function"]["name"].strip()
                # Register every issued sub-tool under its parent task_agent —
                # not only the execute path — so a guard-branch error's
                # output_warning still nests under the task card.
                self._note_agent_child(tc_dict["id"], parent_call_id)

                # is_error for the recalled step: the guard / prepare / unknown
                # branches produce explicit error text; the execute paths record
                # the authoritative flag via ``_report_tool_result`` (consumed
                # below).  Without this every recalled sub-step reads as success
                # and a failed sub-tool recalls as a green "done" card.
                is_tool_error = False
                # Guard 1: block recursive agent calls.
                if tool_name == "task_agent":
                    output = "Error: agents cannot spawn further agents"
                    is_tool_error = True
                # Guard 2: tool not in this agent's API tool list.
                elif tool_name not in tool_names:
                    output = (
                        f"Error: tool '{tool_name}' is not available in "
                        f"agent mode. "
                        f"Available: {', '.join(sorted(tool_names))}"
                    )
                    is_tool_error = True
                else:
                    prepared = self._prepare_tool(tc_dict)

                    if prepared.get("error"):
                        output = prepared["error"]
                        is_tool_error = True
                    # Auto-execute tools in the auto_tools set.
                    elif tool_name in auto_tools:
                        # Paint the step pending under the task card before it
                        # runs (web) / print the leg (CLI) — the typed successor
                        # to the old on_info turn-leg.  Approval-gated tools
                        # paint via approve_tools instead.
                        self._paint_agent_step(parent_call_id, prepared)
                        _, output = prepared["execute"](prepared)
                        is_tool_error = self._tool_error_flags.pop(tc_dict["id"], False)
                    # Tools not in auto_tools require user approval.
                    elif "execute" in prepared:
                        approved, denial_feedback = self.ui.approve_tools([prepared])
                        if not approved and not prepared.get("denied"):
                            # ``approve_tools`` already stamps a SPECIFIC
                            # denial_msg on a denied item (the matched policy
                            # pattern, or the operator's feedback) and returns
                            # the reason as its second value.  Only fill a
                            # default when some other not-approved path left it
                            # unset — never clobber the specific reason with a
                            # flat "Denied by user", and fold the returned
                            # feedback in so the sub-agent can adapt (mirrors the
                            # main tool loop's denial handling).
                            prepared["denied"] = True
                            prepared["denial_msg"] = (
                                f"Denied by user: {denial_feedback}"
                                if denial_feedback
                                else "Denied by user"
                            )
                        if prepared.get("denied"):
                            # A denial is not an execution error — keep is_error
                            # False so recall shows the denial text, not red.
                            # The web gate records the reason in ``denial_msg``;
                            # the CLI gate records a policy block in ``error``
                            # (and returns approved=True) — honour whichever the
                            # gate set before the flat default.
                            output = (
                                prepared.get("denial_msg")
                                or prepared.get("error")
                                or "Denied by user"
                            )
                        else:
                            _, output = prepared["execute"](prepared)
                            is_tool_error = self._tool_error_flags.pop(tc_dict["id"], False)
                    else:
                        output = f"Unknown tool: {tool_name}"
                        is_tool_error = True

                # Output guard: evaluate before truncation so the guard
                # sees full output (credentials split by truncation would
                # evade detection).  Agent outputs are always str.
                if self._judge_cfg and self._judge_cfg.output_guard and isinstance(output, str):
                    output, _ = self._evaluate_output(
                        tc_dict["id"],
                        output,
                        tool_name,
                        tool_args=tc_dict.get("function", {}).get("arguments", ""),
                    )

                # Truncate large tool outputs to avoid blowing context limits.
                # Agents operate autonomously; they can refine their queries
                # if truncation loses important detail.
                if isinstance(output, str) and len(output) > _AGENT_TOOL_OUTPUT_CAP:
                    output = self._clip_with_count(output, _AGENT_TOOL_OUTPUT_CAP)

                # NOTE: for a vision tool result ``output`` is a list[dict] of
                # inline content parts (read_file on an image).  It lowers back
                # to the same inline list on the wire (behaviour preserved), but
                # it is NOT a valid ``TextBlock`` — ``Turn.text`` would raise on
                # it.  No consumer evaluates ``.text`` on a sub-agent tool turn
                # today.  The proper by-reference representation needs the
                # attachment resolver wired into this sub-agent's
                # ``create_completion`` (it currently isn't) plus content-
                # addressed byte storage — deferred to the recall/persist work
                # where that attachment path is already in scope.
                agent_turns.append(Turn.tool(tc_dict["id"], output, is_error=is_tool_error))
            turn += 1

        # Exhausted tool turns — force a final synthesis response.
        self.ui.on_info(f"[{label}] turn limit reached, requesting synthesis...")
        agent_turns.append(
            Turn.user(
                "You have reached the tool call limit. "
                "Provide your complete response now using "
                "the information you have gathered so far."
            )
        )
        result = _api_call(agent_turns, _tools=[])
        content = result.content or "(no output)"
        self.ui.on_info(f"[{label} done] {len(content)} chars")
        return self._guard_subagent_synthesis(content, label)

    _TASK_DEFAULT_IDENTITY = (
        "# Task Agent\n\n"
        "You are an autonomous task agent with full tool access. "
        "You can use bash, read_file, write_file, edit_file, search, "
        "web_fetch, and web_search."
    )
    # Operating guidance always applies — these are sub-agent semantics
    # (one-shot, tool-use over narration, no follow-up questions) that a
    # persona skill should layer on top of, not replace.
    _TASK_OPERATING_GUIDANCE = (
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
    )

    def _exec_task(self, item: dict[str, Any]) -> tuple[str, str]:
        """Delegate to a general-purpose autonomous sub-agent."""
        call_id, prompt = item["call_id"], item["prompt"]
        skill_data = item.get("skill")
        if skill_data:
            # Structured forensic record naming the skill the LLM ran
            # under.  The approval row captures the choice at consent
            # time; this log captures it at exec time so post-incident
            # search ("which sessions ran skill X?") doesn't have to
            # cross-walk approval and exec tables.
            log.info(
                "task_agent.skill_invoked",
                skill=skill_data["name"],
                risk_level=skill_data.get("risk_level", ""),
                ws_id=self._ws_id,
            )
            context = {
                "model": self.model,
                "ws_id": self._ws_id,
                "node_id": self._node_id or "",
            }
            persona = _render_template(skill_data["content"], context)
            if len(persona) > _MAX_SKILL_CONTENT:
                log.warning(
                    "skill_content.truncated",
                    length=len(persona),
                    agent="task",
                    skill=skill_data.get("name", ""),
                )
                persona = persona[:_MAX_SKILL_CONTENT]
        else:
            persona = self._TASK_DEFAULT_IDENTITY
        identity = persona + "\n\n" + self._TASK_OPERATING_GUIDANCE
        # Task agent gets the base system prompt (tool patterns) merged
        # with its own identity in a single system message. No conversation
        # history — it's an autonomous sub-agent. Merged to avoid
        # multi-system-message errors on models like Qwen.
        base = self._agent_system_messages[0]["content"] if self._agent_system_messages else ""
        agent_turns: list[Turn] = [
            Turn.system(base + "\n\n" + identity),
            Turn.user(prompt),
        ]
        self._begin_agent_scope()
        # Per-sub-agent file-read tracking.  COPY the parent's current set in (so
        # the agent can edit a file the parent already read for it — no spurious
        # "must read before editing"), but as an INDEPENDENT set, so a pool
        # sibling's reads can't suppress THIS agent's blind-overwrite guard.  The
        # agent's own reads merge back to the parent in ``finally``.
        read_token = _active_read_files.set(set(self._current_read_files))
        try:
            result = self._run_agent(
                agent_turns,
                label="task",
                tools=self._task_tools,
                auto_tools=TASK_AUTO_TOOLS,
                agent_alias=item.get("model_override"),
                parent_call_id=call_id,
            )
            # Self-report the task_agent's OWN result.  The parent run-loop only
            # reports error/denied/exception results centrally; success results
            # rely on each tool self-reporting (bash, search, … all do), and the
            # task_agent never did — so without this the live card has no
            # completion signal (it stays "running" and the synthesis never
            # renders live, only on reload).
            self._report_tool_result(call_id, "task_agent", result)
            return call_id, result
        except GenerationCancelled:
            # Fold back an honest disposition built from the agent's own
            # ledger.  ``agent_turns`` is mutated in place by
            # ``_run_agent`` (it appends every assistant turn and tool
            # result), so at this catch point it holds the full record of
            # what the sub-agent did before cancel — including a partial
            # result from a tool that was SIGKILL'd mid-flight.  The old
            # bare "(task interrupted by user)" string discarded all of
            # that and fabricated an *outcome*: downstream it read as
            # "nothing happened" and invited a re-dispatch / double-send.
            # See the cancellation appendix in HYPOTHESIS.md ("ρ may
            # fabricate the acknowledgment but must not fabricate the
            # outcome … unknown, never none").
            self._tool_status[call_id] = self._cancelled_agent_status(agent_turns)
            disposition = self._cancelled_agent_disposition(agent_turns, "task")
            self._report_tool_result(call_id, "task_agent", disposition)
            return call_id, disposition
        except KeyboardInterrupt:
            # CLI Ctrl-C: keep the terse string and let the outer loop own
            # propagation (unchanged behavior).
            return call_id, "(task interrupted by user)"
        except Exception as e:
            # Report as the task_agent's errored result so the card flips to
            # "failed" and the message renders (replaces the old on_info, which
            # was suppressed during the agent scope anyway).
            msg = f"Task error: {e}"
            self._report_tool_result(call_id, "task_agent", msg, is_error=True)
            return call_id, msg
        finally:
            # Teardown first (cheap + critical): merge the agent's reads back to
            # the parent, restore the contextvar, drop the scope + child tags —
            # all BEFORE the best-effort stash, so a stash raise can't leak the
            # scope depth (phantom card nesting) or the child registry.
            sub_reads = _active_read_files.get()
            _active_read_files.reset(read_token)
            if sub_reads:
                self._current_read_files.update(sub_reads)
            self._end_agent_scope()
            self._clear_agent_children(call_id)
            try:
                self._stash_agent_trajectory(call_id, agent_turns)
            except Exception:
                log.debug("task_agent.stash_failed call_id=%s", call_id, exc_info=True)

    @staticmethod
    def _cancel_ledger(
        agent_turns: list[Turn],
    ) -> tuple[list[tuple[str, bool]], int | None]:
        """Read a cancelled sub-agent's ledger: every issued tool call as
        ``(name, was_answered)`` in order, plus the index of the first in-flight
        gap (the first issued call with no result), or ``None`` if every call
        returned.

        ``_run_agent`` runs a turn's tool_calls sequentially (cancel raises at
        the per-tool checkpoint, or mid-tool for a SIGKILL'd bash), so the first
        gap is the call that was executing when cancel landed: everything before
        it returned, everything after never started. (The LAST gap would invert
        unknown/none on a multi-call turn.) Shared by the disposition string and
        its typed status so the two can't disagree.

        Pairs via :meth:`_iter_agent_tool_results` (FIFO per call_id), so on a
        provider that reuses ids a half-answered colliding pair is correctly read
        as one answered + one in-flight gap, not (set-membership) both answered.
        """
        issued = [
            ((tc.name or "tool").strip(), res is not None)
            for tc, res in ChatSession._iter_agent_tool_results(agent_turns)
        ]
        first_gap = next((i for i, (_n, ans) in enumerate(issued) if not ans), None)
        return issued, first_gap

    def _cancelled_agent_status(self, agent_turns: list[Turn]) -> EffectStatus:
        """Typed twin of :meth:`_cancelled_agent_disposition`: ``none`` if the
        agent never acted, ``unknown`` if a tool was in flight when cancel
        landed (its effect unobserved), else ``partial`` — every issued call
        returned, but the agent was stopped before finishing."""
        issued, first_gap = self._cancel_ledger(agent_turns)
        if not issued:
            return EffectStatus.NONE
        if first_gap is not None:
            return EffectStatus.UNKNOWN
        return EffectStatus.PARTIAL

    def _cancelled_agent_disposition(self, agent_turns: list[Turn], label: str) -> str:
        """Build an honest, deterministic disposition for a cancelled sub-agent.

        A cancelled agent must not report a fabricated *outcome*.  The bare
        "(interrupted)" string read downstream as *the task did not happen*
        — which causes a double-send as readily as a dropped record causes
        an orphan (cancellation appendix, HYPOTHESIS.md).  Instead we fold
        back the agent's actual ledger: which tool actions completed, which
        never started, and an explicit ``UNKNOWN`` flag on the in-flight
        action — the FIRST issued call without a result.  ``_run_agent``
        executes a turn's tool_calls sequentially, so on cancel everything
        before the first gap completed, the gap itself was executing (its
        side effect may or may not have landed, its result never observed),
        and everything after it never ran.

        Pure string assembly over the in-memory ``agent_turns`` — no
        model call, because we are on the cancel path and the gate is
        closed.  The owner (parent / coordinator) reads this to decide what,
        if anything, to compensate.
        """
        issued, first_gap = self._cancel_ledger(agent_turns)
        if not issued:
            return f"({label} cancelled by user before any action — no side effects)"

        def _summ(names: list[str]) -> str:
            counts: dict[str, int] = {}
            for n in names:
                counts[n] = counts.get(n, 0) + 1
            return ", ".join(f"{n}×{c}" if c > 1 else n for n, c in counts.items())

        parts = [f"({label} cancelled by user before completion)"]
        if first_gap is None:
            # Every issued call returned a result — cancel landed between
            # turns, nothing in flight.  Each result already carries its own
            # disposition (a SIGKILL'd tool's row reads UNKNOWN); just
            # summarise what completed.
            parts.append("Completed before cancel: " + _summ([n for n, _ in issued]) + ".")
            return "\n".join(parts)
        boundary = issued[first_gap][0]
        completed = [n for n, _ in issued[:first_gap]]
        not_run = [n for n, _ in issued[first_gap + 1 :]]
        if completed:
            parts.append("Completed before cancel: " + _summ(completed) + ".")
        parts.append(
            f"In flight at cancel: {boundary} — outcome UNKNOWN. It may have "
            "completed, partially executed, or caused side effects before the "
            "agent was stopped; its result was never observed. Do not assume "
            "it did or did not happen — reconcile before re-running."
        )
        if not_run:
            parts.append("Not started (cancelled first): " + _summ(not_run) + ".")
        return "\n".join(parts)

    def _audit_memory_event(
        self,
        action: str,
        memory_id: str,
        *,
        name: str,
        scope: str,
        scope_id: str,
        mem_type: str,
    ) -> None:
        """Emit an audit row for a mutating memory tool action.

        Closes the audit gap that previously masked out-of-band deletes
        when investigating "save reports success but get returns
        not-found": only the admin-console DELETE route emitted
        ``memory.delete`` rows, so a long-running session whose row was
        deleted by the console UI couldn't tell from logs alone whether
        the row had been deleted, never persisted, or was never visible.

        ``scope_id`` is the empty string for ``scope='global'`` and the
        actor's user_id / ws_id for the other scopes — written as-is so
        forensic queries can filter on it.  ``ws_id`` always rides in
        the detail (``self._ws_id`` is unconditional on ChatSession).

        Best-effort: failures log at debug and swallow so an audit hiccup
        never breaks the tool call itself. Reads (get/search/list) are
        intentionally not audited — they'd multiply audit volume
        without forensic value.
        """
        try:
            from turnstone.core.audit import record_audit

            detail: dict[str, Any] = {
                "name": name,
                "scope": scope,
                "scope_id": scope_id,
                "type": mem_type,
                "ws_id": self._ws_id,
            }
            record_audit(
                get_storage(),
                self._user_id,
                action,
                "memory",
                memory_id,
                detail,
            )
        except Exception:
            log.debug("memory.audit_failed action=%s name=%s", action, name, exc_info=True)

    def _exec_memory(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute a memory tool action."""
        call_id = item["call_id"]
        action = item["action"]

        try:
            if action == "save":
                row, was_update = save_structured_memory(
                    item["name"],
                    item["content"],
                    description=item["description"],
                    mem_type=item["mem_type"],
                    scope=item["scope"],
                    scope_id=item["scope_id"],
                )
                if not row:
                    msg = f"Error: failed to save memory '{item['name']}'"
                    self._report_tool_result(call_id, "memory", msg, is_error=True)
                    return call_id, msg
                # Invalidate the per-turn search cache so an in-turn
                # memory(search)/(list) reflects this write.  Deliberately do NOT
                # recompose the system prefix here: injected memories ride in the
                # cached system block, so re-initing on every save/update busts
                # the prompt cache (a full system + history re-write) — for a
                # memory the model already holds via this tool result.  The write
                # folds into the prefix at the next natural recompose
                # (skill/model/MCP/resume/compaction) or the next session.
                self._invalidate_memory_cache()
                self._audit_memory_event(
                    "memory.update" if was_update else "memory.save",
                    row["memory_id"],
                    name=row["name"],
                    scope=row["scope"],
                    scope_id=row["scope_id"],
                    mem_type=row["type"],
                )
                verb = "Updated" if was_update else "Saved"
                msg = f"{verb} memory '{row['name']}' (type={row['type']}, scope={row['scope']})"
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
                    self._touch_read_memories([mem])
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
                deleted: dict[str, str] | None = None
                deleted_scope = ""
                deleted_scope_id = ""
                # Look up first so the audit row can record the deleted
                # memory_id + type (delete-by-name returns only a bool).
                # Falling back through the scope walk keeps the current
                # narrowest-first IC semantics; coord sessions only see
                # ``coordinator`` here.
                for scope, scope_id in scopes:
                    existing = get_structured_memory_by_name(item["name"], scope, scope_id)
                    if existing and delete_structured_memory_by_id(existing["memory_id"]):
                        deleted = existing
                        deleted_scope = scope
                        deleted_scope_id = scope_id
                        break
                if deleted is None:
                    tried = ", ".join(s for s, _ in scopes)
                    msg = f"Error: memory '{item['name']}' not found (searched scopes: {tried})"
                    self._report_tool_result(call_id, "memory", msg, is_error=True)
                else:
                    self._invalidate_memory_cache()
                    self._init_system_messages()
                    self._audit_memory_event(
                        "memory.delete",
                        deleted["memory_id"],
                        name=item["name"],
                        scope=deleted_scope,
                        scope_id=deleted_scope_id,
                        mem_type=deleted.get("type", ""),
                    )
                    msg = f"Deleted memory '{item['name']}' (scope={deleted_scope})"
                    self._report_tool_result(call_id, "memory", msg)
                return call_id, msg

            if action == "search":
                scope = item.get("scope", "")
                scope_id = item.get("scope_id", "")
                # Defense-in-depth: reject scoped queries with empty scope_id
                if scope in ("user", "workstream", "coordinator") and not scope_id:
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
                log.info(
                    "memory.search",
                    term_count=len(normalize_search_terms(item["query"])),
                    result_count=len(rows),
                    query=item["query"][:120],
                )
                self._touch_read_memories(rows)
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
                if scope in ("user", "workstream", "coordinator") and not scope_id:
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
        """Search conversation history, scoped to the prepare-time user.

        The session's own workstream is searchable only BELOW its compaction
        checkpoint: rows above it are the live segment, already in context —
        returning them would spend result slots on duplicates.  Below it is
        the summarized-away past, exactly what recall exists to re-derive
        (the summary is a cache over the originals, not their replacement).
        The boundary is read fresh at execution, not pinned at prepare, so a
        compaction that ran while the item was queued is respected.

        Known limit: a FORKED session excludes only its own ws — the parent
        rows it inherited into context remain searchable under the parent's
        id (and unlabeled, since they aren't this ws).  Harmless duplication
        bounded by tenancy, and precise dedup needs a fork-time row cursor;
        not worth carrying until forks matter here.
        """
        call_id = item["call_id"]
        query, limit, offset = item["query"], item["limit"], item.get("offset", 0)

        # KeyError on a missing pin is deliberate — an unpinned item must
        # fail loudly, not fall back to an unscoped (tenant-wide) search.
        conv_rows = search_history(
            query,
            limit,
            offset,
            user_id=item["scope_user_id"],
            exclude_ws_id=self._ws_id or None,
            exclude_after=(get_compaction_checkpoint(self._ws_id) if self._ws_id else None),
        )
        if conv_rows:
            lines = []
            for ts, sid, role, content, tool_name in conv_rows:
                label = f"{role}({tool_name})" if tool_name else role
                text = (content or "")[:2000]
                if content and len(content) > 2000:
                    text += f"... ({len(content)} chars total)"
                own = " (earlier in this conversation, compacted)" if sid == self._ws_id else ""
                lines.append(f"[{ts} {sid}]{own} {label}: {text}")
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
            target = storage.find_watch_by_name(self._ws_id, name)
            if target is None:
                msg = f'Watch "{name}" not found.'
                self._report_tool_result(call_id, "watch", msg, is_error=True)
                return call_id, msg
            # In either branch below the row leaves ``list_due_watches``
            # view (already-inactive or just-cancelled with empty
            # next_poll), so the runner's retry-deactivate branch will
            # never reclaim a pending ``_terminal_dispatched`` entry.
            # Clear it here to bound the lifetime of any leftover from
            # a previous dispatch-then-failed-row-write.
            if self._watch_runner is not None:
                self._watch_runner.forget_terminal_dispatched(target["watch_id"])
            if not target["active"]:
                msg = f'Watch "{target["name"]}" already completed (auto-cancelled).'
                self._report_tool_result(call_id, "watch", msg)
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
            self._current_read_files.add(resolved)
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
        # budget on deep reasoning for a simple extraction task.  Temperature
        # is left to the session/registry default rather than overridden here.
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
        """Search the web via the configured backend (SearxNG or MCP)."""
        self._check_cancelled()
        call_id = item["call_id"]
        query = item["query"]
        max_results = item.get("max_results", 5)
        category = item.get("category", "general")

        client = self._resolve_search_client()
        if not client:
            msg = "Error: web search backend not available"
            self._report_tool_result(call_id, "web_search", msg, is_error=True)
            return call_id, msg

        try:
            output = client.search(
                query,
                max_results=max_results,
                category=category,
                reranker=self._web_search_reranker(),
            )
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
            self._repeat_detector.clear()
            self._last_usage = None
            self._calibrated_msg_count = 0
            self._msg_tokens = []
            self.ui.on_info("Context cleared (messages preserved in database).")

        elif cmd == "/new":
            from turnstone.core.memory import register_workstream

            self.messages.clear()
            self._read_files.clear()
            self._repeat_detector.clear()
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
                rows = search_history(query, limit=20, user_id=self._history_scope_user_id())
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
                rows = search_history_recent(limit=20, user_id=self._history_scope_user_id())
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
                    # No system-prefix recompose here: reasoning effort rides in
                    # request kwargs (output_config / thinking), not the composed
                    # prompt, so re-initing would only re-pay the compose for an
                    # identical prefix.
                    self._save_config()
                    self.ui.on_info(f"Reasoning effort set to {cyan(self.reasoning_effort)}")
                else:
                    self.ui.on_info(f"Invalid. Choose from: {', '.join(valid)}")

        elif cmd == "/compact":
            try:
                self._compact_messages()
            except GenerationCancelled:
                # Ctrl-C during a manual compaction aborts cleanly — the message
                # swap never ran (the cancel-check precedes it), so history is
                # intact, exactly like cancelling a send.
                self.ui.on_info("Compaction cancelled.")

        elif cmd == "/creative":
            self.creative_mode = not self.creative_mode
            self._init_system_messages()
            self._save_config()
            # Clear history when toggling ON if it contains tool messages,
            # because the API rejects tool-call history without tool definitions
            if self.creative_mode and any(
                m.tool_calls or m.role is Role.TOOL for m in self.messages
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
                # Phase 7 + 7b: pass the effective user_id so the /mcp
                # listing surfaces this user's pool tools, resources,
                # and prompts alongside the static catalog.
                tools = self._mcp_client.get_tools(user_id=self._mcp_effective_user_id)
                resources = self._mcp_client.get_resources(user_id=self._mcp_effective_user_id)
                prompts = self._mcp_client.get_prompts(user_id=self._mcp_effective_user_id)
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
