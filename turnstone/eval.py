#!/usr/bin/env python3
"""eval.py — Prompt optimization and evaluation for turnstone.

Iteratively evaluates and optimizes the turnstone developer prompt by running
test cases, scoring tool call sequences against expected actions, and using
the model to self-modify the prompt based on results.

Usage:
    python -m turnstone.eval tests.json
    python -m turnstone.eval tests.json --no-optimize
    python -m turnstone.eval tests.json --prompt prompt.txt --n-runs 5 --max-iter 10
"""

import argparse
import copy
import difflib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import textwrap
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openai import OpenAI

from turnstone.core.providers import LLMProvider, create_client, create_provider
from turnstone.core.session import ChatSession
from turnstone.core.storage import init_storage, reset_storage
from turnstone.core.tools import INTERACTIVE_TOOLS, PRIMARY_KEY_MAP

# Eval evaluates interactive-session agent behaviour — coordinator tools
# require a console-hosted session and aren't exercised by the harness.
# Re-export as ``TOOLS`` so all downstream references stay identical.
TOOLS = INTERACTIVE_TOOLS

# Tools that require MCP servers — excluded from headless eval
_MCP_ONLY_TOOLS = frozenset({"read_resource", "use_prompt"})

# ─── Provider auto-detection ──────────────────────────────────────────────────


def _detect_provider(base_url: str) -> str:
    """Infer provider name from a base URL."""
    from urllib.parse import urlparse

    normalized = base_url if "://" in base_url else f"https://{base_url}"
    hostname = urlparse(normalized).hostname or ""
    if hostname == "anthropic.com" or hostname.endswith(".anthropic.com"):
        return "anthropic"
    return "openai"


def _make_client_and_provider(base_url: str, api_key: str) -> tuple[Any, LLMProvider]:
    """Create a client + provider pair, auto-detecting the provider."""
    name = _detect_provider(base_url)
    client = create_client(name, base_url=base_url, api_key=api_key)
    provider = create_provider(name)
    return client, provider


# ─── ANSI & logging helpers ───────────────────────────────────────────────────

DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"


@dataclass
class EvolutionNode:
    """A node in the prompt evolution tree (UCB-based search)."""

    node_id: int
    parent_id: int | None
    prompt: str
    tool_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    score: float = 0.0
    visit_count: int = 0
    children: list[int] = field(default_factory=list)
    iteration: int = 0
    optimizer_system: str = ""


class NullUI:
    """UI adapter that discards all output. Used by HeadlessSession."""

    def on_thinking_start(self) -> None:
        pass

    def on_thinking_stop(self) -> None:
        pass

    def on_reasoning_token(self, text: str) -> None:
        pass

    def on_content_token(self, text: str) -> None:
        pass

    def on_stream_end(self) -> None:
        pass

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        return True, None

    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
        pass

    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None:
        pass

    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None:
        pass

    def on_plan_review(self, content: str) -> str:
        return ""

    def on_info(self, message: str) -> None:
        pass

    def on_error(self, message: str) -> None:
        pass

    def on_user_reminder(self, reminders: list[dict[str, str]]) -> None:
        pass

    def on_tool_reminder(self, reminders: list[dict[str, str]], tool_call_id: str) -> None:
        pass

    def on_state_change(self, state: str) -> None:
        pass

    def on_rename(self, name: str) -> None:
        pass

    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        pass

    def on_output_warning(self, call_id: str, assessment: dict[str, Any]) -> None:
        pass


def _log(msg: str, dim: bool = False) -> None:
    """Print a log line with optional dim styling."""
    if dim:
        sys.stderr.write(f"{DIM}{msg}{RESET}\n")
    else:
        sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def _fmt_args(args: dict[str, Any], max_len: int = 80) -> str:
    """Format tool args as a compact one-line summary."""
    parts = []
    for k, v in args.items():
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "..."
        parts.append(f"{k}={sv!r}")
    out = ", ".join(parts)
    if len(out) > max_len:
        out = out[: max_len - 3] + "..."
    return out


# ─── Headless session ────────────────────────────────────────────────────────


class HeadlessSession(ChatSession):
    """ChatSession subclass for headless evaluation.

    Differences from ChatSession:
    - auto_approve is always True
    - Tool calls are recorded into a structured log
    - All stdout output is suppressed
    - send_headless() uses non-streaming API
    """

    def __init__(
        self,
        client: Any,
        model: str,
        system_prompt_override: str | None = None,
        instructions: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 32768,
        tool_timeout: int = 30,
        reasoning_effort: str = "medium",
        context_window: int = 131072,
        compact_max_tokens: int = 32768,
        auto_compact_pct: float = 0.8,
        agent_max_turns: int = -1,
        tool_truncation: int = 0,
        tool_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            client=client,
            model=model,
            ui=NullUI(),
            instructions=instructions,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_timeout=tool_timeout,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
            compact_max_tokens=compact_max_tokens,
            auto_compact_pct=auto_compact_pct,
            agent_max_turns=agent_max_turns,
            tool_truncation=tool_truncation,
        )
        self.tool_call_log: list[dict[str, Any]] = []
        self.auto_approve = True
        self._cancelled = threading.Event()
        self._total_usage: dict[str, int] = {"prompt": 0, "completion": 0}
        # Filter MCP-only tools that can't work in headless mode
        base_tools = [t for t in TOOLS if t["function"]["name"] not in _MCP_ONLY_TOOLS]
        self._eval_tools = (
            _apply_tool_overrides(base_tools, tool_overrides) if tool_overrides else base_tools
        )
        if system_prompt_override is not None:
            self._override_system_prompt(system_prompt_override)

    @property
    def total_usage(self) -> dict[str, int]:
        """Accumulated token usage across all turns."""
        return self._total_usage

    def _override_system_prompt(self, content: str) -> None:
        """Replace the system/developer message content with a custom prompt."""
        for i, msg in enumerate(self.system_messages):
            if msg["role"] in ("developer", "system"):
                self.system_messages[i] = {"role": msg["role"], "content": content}
                return
        self.system_messages.append({"role": "system", "content": content})

    def send_headless(
        self,
        user_input: str,
        max_turns: int = 10,
        verbose: bool = False,
        log_prefix: str = "",
    ) -> list[dict[str, Any]]:
        """Run a complete conversation turn headlessly.

        Uses non-streaming API calls. Captures all tool calls into
        self.tool_call_log.

        Returns the tool call log: list of dicts with keys:
            tool: str, args: dict, result: str (truncated), turn: int
        """
        self.tool_call_log = []
        self.messages.append({"role": "user", "content": user_input})
        self._msg_tokens.append(max(1, int(len(user_input) / self._chars_per_token)))

        for turn in range(max_turns):
            if self._cancelled.is_set():
                break

            if verbose:
                _log(f"{log_prefix}  turn {turn}: calling API...", dim=True)

            t0 = time.monotonic()
            msgs = self._full_messages()

            if self._cancelled.is_set():
                break

            result = self._provider.create_completion(
                client=self.client,
                model=self.model,
                messages=msgs,
                tools=self._eval_tools,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                reasoning_effort=self.reasoning_effort,
                extra_params=self._provider_extra_params(),
            )
            elapsed = time.monotonic() - t0

            if result.usage:
                self._total_usage["prompt"] += result.usage.prompt_tokens
                self._total_usage["completion"] += result.usage.completion_tokens

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or None,
            }

            if result.tool_calls:
                # Cap parallel tool calls to prevent degenerate repetition
                assistant_msg["tool_calls"] = result.tool_calls[:10]

            self.messages.append(assistant_msg)
            msg_len = len(assistant_msg.get("content") or "")
            self._msg_tokens.append(max(1, int(msg_len / self._chars_per_token)))

            # Log usage and content
            if verbose:
                toks = ""
                if result.usage:
                    toks = (
                        f"  [{result.usage.prompt_tokens}p/{result.usage.completion_tokens}c tok]"
                    )
                _log(
                    f"{log_prefix}  turn {turn}: response in {elapsed:.1f}s{toks}",
                    dim=True,
                )
                if assistant_msg["content"]:
                    text = assistant_msg["content"][:200]
                    if len(assistant_msg["content"]) > 200:
                        text += "..."
                    _log(f"{log_prefix}    content: {text}", dim=True)

            if not result.tool_calls:
                if verbose:
                    _log(f"{log_prefix}  turn {turn}: no tool calls, done", dim=True)
                break

            # Log tool calls
            if verbose:
                names = [tc["function"]["name"] for tc in result.tool_calls]
                _log(f"{log_prefix}  turn {turn}: tools -> {names}")

            # Execute tools (NullUI discards session output; tools return
            # results as strings, not via stdout)
            results, _ = self._execute_tools(assistant_msg["tool_calls"])

            for tc, (tc_id, raw_output) in zip(assistant_msg["tool_calls"], results, strict=False):
                # Flatten list content (image tool results) to text for logging
                if isinstance(raw_output, list):
                    output = " ".join(
                        p.get("text", "[image]") if p.get("type") == "text" else "[image]"
                        for p in raw_output
                    )
                else:
                    output = raw_output
                func_name = tc["function"]["name"]
                args: dict[str, Any]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    raw = tc["function"]["arguments"]
                    # Map bare strings to the primary arg key
                    pk = PRIMARY_KEY_MAP.get(func_name)
                    if pk and raw.strip() and not raw.strip().startswith("{"):
                        args = {pk: raw}
                    else:
                        args = {"_raw": raw}

                self.tool_call_log.append(
                    {
                        "tool": func_name,
                        "args": args,
                        "result": output[:500],
                        "turn": turn,
                    }
                )

                if verbose:
                    # Show compact args summary
                    arg_summary = _fmt_args(args)
                    result_preview = output[:120].replace("\n", "\\n")
                    if len(output) > 120:
                        result_preview += "..."
                    _log(f"{log_prefix}    {func_name}({arg_summary})", dim=False)
                    _log(f"{log_prefix}      -> {result_preview}", dim=True)

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": raw_output,
                }
                self.messages.append(tool_msg)
                self._msg_tokens.append(max(1, int(len(output) / self._chars_per_token)))

        return self.tool_call_log


# ─── Test runner ─────────────────────────────────────────────────────────────


def _run_single_test(
    client: Any,
    model: str,
    system_prompt: str,
    case: dict[str, Any],
    temperature: float,
    max_tokens: int,
    reasoning_effort: str,
    context_window: int,
    verbose: bool = False,
    log_prefix: str = "",
    test_timeout: int = 300,
    tool_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a single test case once in an isolated temp directory.

    Uses os.chdir (process-global), so concurrent calls must run in
    separate processes (see _run_and_score_subprocess / --parallel).

    Returns dict with keys: tool_log, final_content, message_count,
    elapsed, usage.
    """
    workdir = tempfile.mkdtemp(prefix="turnstone_eval_")
    original_cwd = os.getcwd()
    eval_db = os.path.join(workdir, ".turnstone_eval.db")
    reset_storage()
    init_storage("sqlite", path=eval_db, run_migrations=False)
    t0 = time.monotonic()

    try:
        # Write setup files
        setup_files = list(case.get("setup", {}).get("files", {}).items())
        for path, content in setup_files:
            full = os.path.join(workdir, path)
            os.makedirs(os.path.dirname(full) or workdir, exist_ok=True)
            with open(full, "w") as f:
                f.write(content)

        if verbose and setup_files:
            _log(f"{log_prefix}  setup: created {[p for p, _ in setup_files]}", dim=True)

        os.chdir(workdir)

        max_turns = case.get("max_turns", 15)
        # Retry on transient API errors to avoid poisoning eval scores
        tool_log: list[dict[str, Any]] = []
        final_content = ""
        message_count = 0
        total_usage: dict[str, int] = {"prompt": 0, "completion": 0}
        _last_err: Exception | None = None
        for _attempt in range(3):
            # Per-attempt client with request-level timeout so httpx aborts
            # the HTTP request itself — no zombie connections on the server.
            run_client = OpenAI(
                base_url=client.base_url,
                api_key=client.api_key,
                timeout=float(test_timeout),
            )
            session = HeadlessSession(
                client=run_client,
                model=model,
                system_prompt_override=system_prompt,
                instructions=None,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_timeout=30,
                reasoning_effort=reasoning_effort,
                context_window=context_window,
                tool_truncation=2000,
                tool_overrides=tool_overrides,
            )
            executor: ThreadPoolExecutor | None = None
            try:
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(
                    session.send_headless,
                    case["user_prompt"],
                    max_turns=max_turns,
                    verbose=verbose,
                    log_prefix=log_prefix,
                )
                try:
                    tool_log = future.result(timeout=test_timeout)
                except FuturesTimeoutError:
                    session._cancelled.set()
                    run_client.close()
                    executor.shutdown(wait=False, cancel_futures=True)
                    executor = None
                    session = None  # type: ignore[assignment]
                    raise TimeoutError(f"Test timed out after {test_timeout}s") from None
                else:
                    run_client.close()
                    executor.shutdown(wait=False)
                    executor = None

                # Extract results before releasing session
                for msg in reversed(session.messages):
                    if msg["role"] == "assistant" and msg.get("content"):
                        final_content = msg["content"]
                        break
                message_count = len(session.messages)
                total_usage = session.total_usage
                session = None  # type: ignore[assignment]  # release memory
                break
            except TimeoutError:
                raise
            except Exception as _e:
                _last_err = _e
                run_client.close()
                session = None  # type: ignore[assignment]
                if _attempt < 2:
                    time.sleep(2**_attempt)
            finally:
                if executor is not None:
                    executor.shutdown(wait=False)
        else:
            raise _last_err or RuntimeError("send_headless failed after 3 attempts")

        elapsed = time.monotonic() - t0
        return {
            "tool_log": tool_log,
            "final_content": final_content,
            "message_count": message_count,
            "elapsed": round(elapsed, 1),
            "usage": total_usage,
        }
    finally:
        reset_storage()
        os.chdir(original_cwd)
        shutil.rmtree(workdir, ignore_errors=True)


def _run_and_score_subprocess(params: dict[str, Any]) -> dict[str, Any]:
    """Subprocess worker for parallel test execution.

    Creates its own OpenAI client (the parent's is not picklable),
    runs a single test case once, scores it, and returns a serializable
    result dict.  Must live at module scope for ProcessPoolExecutor.
    """
    client = OpenAI(
        base_url=params["base_url"],
        api_key=params["api_key"],
    )
    case: dict[str, Any] = params["case"]
    run_tokens = 0

    try:
        run_result = _run_single_test(
            client=client,
            model=params["model"],
            system_prompt=params["system_prompt"],
            case=case,
            temperature=params["temperature"],
            max_tokens=params["max_tokens"],
            reasoning_effort=params["reasoning_effort"],
            context_window=params["context_window"],
            verbose=False,
            log_prefix="",
            test_timeout=params["test_timeout"],
            tool_overrides=params.get("tool_overrides"),
        )

        score_result = score_run(
            tool_log=run_result["tool_log"],
            expected_actions=case.get("expected_actions", []),
            match_mode=case.get("match_mode", "ordered_subset"),
        )

        score_result["tool_sequence"] = [t["tool"] for t in run_result["tool_log"]]
        score_result["tool_args"] = [{t["tool"]: t["args"]} for t in run_result["tool_log"]]
        score_result["elapsed"] = run_result.get("elapsed", 0)
        # Record which prompt variant was used (if diversified)
        original = params.get("original_user_prompt", "")
        if original and case["user_prompt"] != original:
            score_result["prompt_variant"] = case["user_prompt"]
        run_tokens = sum(run_result.get("usage", {}).values())

        # Detect JSON dumped into final channel (tool call not made)
        fc = (run_result.get("final_content") or "").strip()
        if fc and not score_result["pass"]:
            has_json = bool(
                re.search(
                    r'\{\s*"(tool|function|name|arguments|command|query|path|url)"',
                    fc,
                )
            )
            if has_json:
                score_result["json_dump"] = True

    except Exception as e:
        score_result = {
            "pass": False,
            "score": 0.0,
            "matched": [],
            "unmatched": list(range(len(case.get("expected_actions", [])))),
            "extra_tools": [],
            "detail": f"Subprocess error: {e}",
            "tool_sequence": [],
            "tool_args": [],
            "elapsed": 0,
        }

    return {
        "case_id": params["case_id"],
        "run_idx": params["run_idx"],
        "score_result": score_result,
        "run_tokens": run_tokens,
    }


# ─── Scoring ─────────────────────────────────────────────────────────────────


def _match_action(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Check if a single actual tool call matches an expected action spec."""
    if actual["tool"] != expected["tool"]:
        return False

    actual_args = actual["args"]

    # If args were unparseable (_raw fallback), can only match on tool name
    if "_raw" in actual_args and len(actual_args) == 1:
        return "args" not in expected and "args_pattern" not in expected

    # Check exact args (partial key matching)
    if "args" in expected:
        for key, expected_val in expected["args"].items():
            actual_val = actual_args.get(key)
            if actual_val is None:
                return False
            if str(actual_val) != str(expected_val):
                return False

    # Check regex args_pattern
    if "args_pattern" in expected:
        for key, pattern in expected["args_pattern"].items():
            actual_val = str(actual_args.get(key, ""))
            if not re.search(pattern, actual_val):
                return False

    return True


def score_run(
    tool_log: list[dict[str, Any]],
    expected_actions: list[dict[str, Any]],
    match_mode: str = "ordered_subset",
) -> dict[str, Any]:
    """Score a single run's tool log against expected actions.

    Returns dict with: pass, score, matched, unmatched, extra_tools, detail.
    """
    if not expected_actions:
        return {
            "pass": True,
            "score": 1.0,
            "matched": [],
            "unmatched": [],
            "extra_tools": [],
            "detail": "No expected actions defined",
        }

    n_expected = len(expected_actions)

    if match_mode == "exact":
        matched = []
        for i, (actual, expected) in enumerate(zip(tool_log, expected_actions, strict=False)):
            if _match_action(actual, expected):
                matched.append(i)
        score = len(matched) / n_expected
        length_ok = len(tool_log) == n_expected
        detail = f"Exact: {len(matched)}/{n_expected} matched"
        if not length_ok:
            detail += f" (length {len(tool_log)} vs {n_expected})"
        return {
            "pass": length_ok and len(matched) == n_expected,
            "score": score,
            "matched": matched,
            "unmatched": [i for i in range(n_expected) if i not in matched],
            "extra_tools": [t["tool"] for t in tool_log[n_expected:]],
            "detail": detail,
        }

    elif match_mode == "ordered_subset":
        matched = []
        search_from = 0
        for ei, expected in enumerate(expected_actions):
            for ai in range(search_from, len(tool_log)):
                if _match_action(tool_log[ai], expected):
                    matched.append(ei)
                    search_from = ai + 1
                    break
        score = len(matched) / n_expected
        unmatched = [i for i in range(n_expected) if i not in matched]
        return {
            "pass": len(matched) == n_expected,
            "score": score,
            "matched": matched,
            "unmatched": unmatched,
            "extra_tools": [],
            "detail": f"Ordered subset: {len(matched)}/{n_expected}",
        }

    elif match_mode == "subset":
        matched = []
        used = set()
        for ei, expected in enumerate(expected_actions):
            for ai, actual in enumerate(tool_log):
                if ai not in used and _match_action(actual, expected):
                    matched.append(ei)
                    used.add(ai)
                    break
        score = len(matched) / n_expected
        unmatched = [i for i in range(n_expected) if i not in matched]
        return {
            "pass": len(matched) == n_expected,
            "score": score,
            "matched": matched,
            "unmatched": unmatched,
            "extra_tools": [],
            "detail": f"Subset: {len(matched)}/{n_expected}",
        }

    elif match_mode == "contains_any":
        for ei, expected in enumerate(expected_actions):
            for actual in tool_log:
                if _match_action(actual, expected):
                    return {
                        "pass": True,
                        "score": 1.0,
                        "matched": [ei],
                        "unmatched": [],
                        "extra_tools": [],
                        "detail": "Contains at least one match",
                    }
        return {
            "pass": False,
            "score": 0.0,
            "matched": [],
            "unmatched": list(range(n_expected)),
            "extra_tools": [t["tool"] for t in tool_log],
            "detail": "None of the expected actions were found",
        }

    else:
        return {
            "pass": False,
            "score": 0.0,
            "matched": [],
            "unmatched": list(range(n_expected)),
            "extra_tools": [],
            "detail": f"Unknown match_mode: {match_mode}",
        }


# ─── Iteration runner ────────────────────────────────────────────────────────


def _format_eta(seconds: float) -> str:
    """Format seconds as a human-readable ETA string."""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"~{seconds:.0f}s left"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"~{m}m{s}s left"
    h, m = divmod(m, 60)
    return f"~{h}h{m}m left"


def _aggregate_case_results(
    cases: list[dict[str, Any]],
    case_results: dict[str, Any],
    total_tokens: int,
) -> dict[str, Any]:
    """Build the iteration result dict from per-case results."""
    agg_total_runs = sum(len(cr["runs"]) for cr in case_results.values())
    agg_total_passes = sum(sum(1 for r in cr["runs"] if r["pass"]) for cr in case_results.values())
    total_json_dumps = sum(
        sum(1 for r in cr["runs"] if r.get("json_dump")) for cr in case_results.values()
    )
    return {
        "cases": case_results,
        "aggregate": {
            "total_cases": len(cases),
            "total_runs": agg_total_runs,
            "overall_pass_rate": agg_total_passes / agg_total_runs if agg_total_runs else 0,
            "json_dumps": total_json_dumps,
            "overall_avg_score": (
                sum(cr["avg_score"] for cr in case_results.values()) / len(case_results)
                if case_results
                else 0
            ),
            "per_case_pass_rates": {cid: cr["pass_rate"] for cid, cr in case_results.items()},
            "total_tokens": total_tokens,
        },
    }


def _run_iteration_parallel(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    cases: list[dict[str, Any]],
    n_runs: int,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str,
    context_window: int,
    test_timeout: int,
    parallel: int,
    prompt_variants: dict[str, list[str]] | None = None,
    tool_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all test cases in parallel using ProcessPoolExecutor."""
    # Build work items for every (case, run) combination
    work_items: list[dict[str, Any]] = []
    for case in cases:
        case_id = case["id"]
        case_n = case.get("n_runs", n_runs)
        variants = (prompt_variants or {}).get(case_id)
        for run_idx in range(case_n):
            # Select prompt variant for this run (cycle through variants)
            if variants and len(variants) > 1:
                run_case = {**case, "user_prompt": variants[run_idx % len(variants)]}
            else:
                run_case = case
            work_items.append(
                {
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": model,
                    "system_prompt": system_prompt,
                    "case": run_case,
                    "case_id": case_id,
                    "run_idx": run_idx,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "reasoning_effort": reasoning_effort,
                    "context_window": context_window,
                    "test_timeout": test_timeout,
                    "original_user_prompt": case["user_prompt"],
                    "tool_overrides": tool_overrides,
                }
            )

    total_planned = len(work_items)
    max_workers = min(parallel, total_planned) if parallel > 0 else total_planned
    max_workers = max(max_workers, 1)

    print(f"\n  Running {total_planned} tests across {max_workers} workers...")

    # Collect results grouped by case_id (preserving case order)
    case_runs: dict[str, list[tuple[int, dict[str, Any]]]] = {case["id"]: [] for case in cases}
    completed = 0
    total_passes = 0
    total_tokens = 0
    t0 = time.monotonic()

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_and_score_subprocess, item): item for item in work_items}

        for future in as_completed(futures):
            try:
                # Workers enforce their own test_timeout internally;
                # this outer timeout catches process-level hangs.
                result = future.result(timeout=test_timeout + 30)
            except Exception as exc:
                item = futures[future]
                result = {
                    "case_id": item["case_id"],
                    "run_idx": item["run_idx"],
                    "score_result": {
                        "pass": False,
                        "score": 0.0,
                        "matched": [],
                        "unmatched": list(range(len(item["case"].get("expected_actions", [])))),
                        "extra_tools": [],
                        "detail": f"Subprocess error: {exc}",
                        "tool_sequence": [],
                        "tool_args": [],
                        "elapsed": 0,
                    },
                    "run_tokens": 0,
                }
            cid = result["case_id"]
            ridx = result["run_idx"]
            sr = result["score_result"]
            tok = result["run_tokens"]

            case_runs[cid].append((ridx, sr))

            # Progress
            completed += 1
            if sr.get("pass"):
                total_passes += 1
            total_tokens += tok

            passed = sr["pass"]
            sc = GREEN if passed else RED
            sl = "PASS" if passed else "FAIL"
            tools = sr.get("tool_sequence", [])
            elapsed = sr.get("elapsed", 0)
            jf = f" {YELLOW}[JSON_DUMP]{RESET}" if sr.get("json_dump") else ""

            wall = time.monotonic() - t0
            avg = wall / completed
            remaining = total_planned - completed
            eta = _format_eta(avg * remaining) if remaining > 0 else ""
            rate = total_passes / completed
            tok_k = total_tokens / 1000

            prog = f"[{completed}/{total_planned} | {rate:.0%}"
            if total_tokens > 0:
                prog += f" | {tok_k:.0f}k tok"
            if eta:
                prog += f" | {eta}"
            prog += "]"

            print(
                f"    {DIM}{cid}{RESET} run {ridx + 1}: "
                f"{sc}[{sl}]{RESET} "
                f"score={sr['score']:.2f} "
                f"tools={tools}"
                f"{jf}"
                f"  {DIM}({elapsed:.1f}s)  {prog}{RESET}"
            )

    # Build case_results in original case order
    case_results: dict[str, Any] = {}
    for case in cases:
        cid = case["id"]
        # Sort by run_idx so results are in order
        sorted_runs = [sr for _, sr in sorted(case_runs[cid], key=lambda x: x[0])]
        pass_count = sum(1 for r in sorted_runs if r["pass"])
        case_results[cid] = {
            "runs": sorted_runs,
            "pass_rate": pass_count / len(sorted_runs) if sorted_runs else 0,
            "avg_score": (
                sum(r["score"] for r in sorted_runs) / len(sorted_runs) if sorted_runs else 0
            ),
        }

    return _aggregate_case_results(cases, case_results, total_tokens)


def _run_iteration(
    client: Any,
    model: str,
    system_prompt: str,
    cases: list[dict[str, Any]],
    n_runs: int,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str,
    context_window: int,
    verbose: bool = False,
    test_timeout: int = 300,
    fast_fail: bool = True,
    parallel: int = 1,
    base_url: str = "",
    api_key: str = "",
    prompt_variants: dict[str, list[str]] | None = None,
    tool_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all test cases n_runs times and score them."""
    if parallel > 1 and base_url:
        return _run_iteration_parallel(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            cases=cases,
            n_runs=n_runs,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
            test_timeout=test_timeout,
            parallel=parallel,
            prompt_variants=prompt_variants,
            tool_overrides=tool_overrides,
        )

    case_results: dict[str, Any] = {}

    total_planned_runs = sum(c.get("n_runs", n_runs) for c in cases)
    completed_runs = 0
    iter_total_passes = 0
    total_tokens = 0
    iter_t0 = time.monotonic()

    for ci, case in enumerate(cases):
        case_id = case["id"]
        case_n = case.get("n_runs", n_runs)
        runs: list[dict[str, Any]] = []

        print(f"\n  {CYAN}[{ci + 1}/{len(cases)}]{RESET} {BOLD}{case_id}{RESET} ({case_n} runs)")
        if verbose:
            _log(f"    prompt: {case['user_prompt']}", dim=True)

        for run_idx in range(case_n):
            log_prefix = f"      [{run_idx + 1}/{case_n}]"
            run_tokens = 0

            # Select prompt variant for this run (cycle through variants)
            variants = (prompt_variants or {}).get(case_id)
            if variants and len(variants) > 1:
                variant_idx = run_idx % len(variants)
                run_case = {**case, "user_prompt": variants[variant_idx]}
                if verbose:
                    _log(
                        f"{log_prefix}  variant {variant_idx}: {variants[variant_idx][:80]}...",
                        dim=True,
                    )
            else:
                run_case = case

            try:
                run_result = _run_single_test(
                    client=client,
                    model=model,
                    system_prompt=system_prompt,
                    case=run_case,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    context_window=context_window,
                    verbose=verbose,
                    log_prefix=log_prefix,
                    test_timeout=test_timeout,
                    tool_overrides=tool_overrides,
                )

                score_result = score_run(
                    tool_log=run_result["tool_log"],
                    expected_actions=case.get("expected_actions", []),
                    match_mode=case.get("match_mode", "ordered_subset"),
                )

                score_result["tool_sequence"] = [t["tool"] for t in run_result["tool_log"]]
                score_result["tool_args"] = [{t["tool"]: t["args"]} for t in run_result["tool_log"]]
                score_result["elapsed"] = run_result.get("elapsed", 0)
                if run_case is not case:
                    score_result["prompt_variant"] = run_case["user_prompt"]
                run_tokens = sum(run_result.get("usage", {}).values())

                # Detect JSON dumped into final channel (tool call not made)
                fc = (run_result.get("final_content") or "").strip()
                if fc and not score_result["pass"]:
                    has_json = bool(
                        re.search(
                            r'\{\s*"(tool|function|name|arguments|command|query|path|url)"',
                            fc,
                        )
                    )
                    if has_json:
                        score_result["json_dump"] = True

            except Exception as e:
                score_result = {
                    "pass": False,
                    "score": 0.0,
                    "matched": [],
                    "unmatched": list(range(len(case.get("expected_actions", [])))),
                    "extra_tools": [],
                    "detail": f"Error: {e}",
                    "tool_sequence": [],
                    "tool_args": [],
                    "elapsed": 0,
                }
                run_tokens = 0

            passed = score_result["pass"]
            status_color = GREEN if passed else RED
            status_label = "PASS" if passed else "FAIL"
            tools = score_result.get("tool_sequence", [])
            elapsed = score_result.get("elapsed", 0)
            json_flag = f" {YELLOW}[JSON_DUMP]{RESET}" if score_result.get("json_dump") else ""
            print(
                f"    Run {run_idx + 1}: "
                f"{status_color}[{status_label}]{RESET} "
                f"score={score_result['score']:.2f} "
                f"tools={tools}"
                f"{json_flag}"
                f"  {DIM}({elapsed:.1f}s){RESET}"
            )

            runs.append(score_result)

            # Update progress counters
            completed_runs += 1
            if score_result.get("pass"):
                iter_total_passes += 1
            total_tokens += run_tokens

            # Progress stats
            iter_elapsed = time.monotonic() - iter_t0
            avg_per_run = iter_elapsed / completed_runs if completed_runs else 0
            remaining = total_planned_runs - completed_runs
            eta = _format_eta(avg_per_run * remaining) if remaining > 0 else ""
            running_rate = iter_total_passes / completed_runs if completed_runs else 0
            tok_k = total_tokens / 1000

            progress = f"  {DIM}[{completed_runs}/{total_planned_runs} | {running_rate:.0%}"
            if total_tokens > 0:
                progress += f" | {tok_k:.0f}k tok"
            if eta:
                progress += f" | {eta}"
            progress += f"]{RESET}"
            print(progress)

            # Fast-fail: skip remaining runs if first ceil(n/2) all score 0.0
            if (
                fast_fail
                and len(runs) >= math.ceil(case_n / 2)
                and all(r["score"] == 0.0 for r in runs)
            ):
                skipped = case_n - len(runs)
                if skipped > 0:
                    _log(
                        f"    {DIM}(skipped {skipped} remaining"
                        f" run{'s' if skipped != 1 else ''}"
                        f" — all 0.0){RESET}",
                        dim=True,
                    )
                    for _ in range(skipped):
                        runs.append(
                            {
                                "pass": False,
                                "score": 0.0,
                                "matched": [],
                                "unmatched": list(range(len(case.get("expected_actions", [])))),
                                "extra_tools": [],
                                "detail": "Skipped (fast-fail)",
                                "tool_sequence": [],
                                "tool_args": [],
                                "elapsed": 0,
                                "skipped": True,
                            }
                        )
                    completed_runs += skipped
                    break

        pass_count = sum(1 for r in runs if r["pass"])
        case_results[case_id] = {
            "runs": runs,
            "pass_rate": pass_count / len(runs) if runs else 0,
            "avg_score": (sum(r["score"] for r in runs) / len(runs) if runs else 0),
        }

    return _aggregate_case_results(cases, case_results, total_tokens)


# ─── Prompt optimizer ────────────────────────────────────────────────────────


DIVERSIFIER_SYSTEM = """\
You generate paraphrased variations of user prompts for a tool-use \
evaluation harness. Each variation must preserve the EXACT SAME INTENT \
— the same task, same expected outcome, same level of specificity — \
but use different phrasing, vocabulary, sentence structure, or tone.

Rules:
- Preserve the core action the user is asking for. If the original \
says "fix the typo in config.py", all variants must ask to fix a \
typo in config.py.
- Vary along these dimensions: formality (casual ↔ formal), \
directness (imperative ↔ descriptive), verbosity (terse ↔ detailed), \
framing (command ↔ question ↔ description of need).
- Do NOT change the expected tool behavior. If the original implies \
using bash, the variant must also imply bash.
- Do NOT add new requirements, constraints, or context not in the \
original.
- Each variant should be a single message, roughly similar length \
to the original.

Output a JSON array of strings, one per variant. No commentary, \
no markdown fences, just the JSON array.\
"""


ANALYST_SYSTEM = """\
You are a test failure analyst for an LLM tool-use evaluation harness. \
You receive test results showing how a coding assistant performed \
against expected tool call sequences.

Your job: identify SEMANTIC PATTERNS across failures and successes — \
not just what failed, but WHY, and what the failures have in common.

You have access to `math` (Python with numpy/scipy/collections) and \
`bash` tools. Use them to compute statistics, build confusion matrices, \
analyze tool co-occurrence, or quantify patterns — don't just eyeball \
the data.

## Analysis Framework

### 1. Failure Mode Patterns
Look across all failing runs and identify shared root causes:
- Does the model treat certain phrasings as conversation vs action?
- Are there tool confusion pairs (e.g., always picks write_file over edit_file)?
- Do failures cluster around implicit vs explicit instructions?
- Are there complexity thresholds where the model breaks down?

### 2. Success/Failure Contrast
Compare passing and failing cases to isolate what makes the difference:
- What do passing prompts have that failing ones lack?
- Do passing cases use action verbs while failing ones describe goals?
- Is there a pattern in prompt length, specificity, or structure?

### 3. Consistency Signals
For each failing case, classify:
- Systematic (0% pass): the prompt is fundamentally missing an instruction
- Flaky (1-79% pass): the prompt is ambiguous — wording nudge needed
- Marginal (80-99%): minor edge case — low priority

### 4. Actionable Diagnosis
For each pattern found, state:
- What the model is doing wrong (observed behavior)
- Why it's doing it (root cause hypothesis)
- What pattern or example could demonstrate the right behavior \
(prefer "show" over "tell" — a concrete tool chain example teaches \
better than a rule like "always do X")

## Output Format

```
## Failure Patterns
- [Pattern 1]: [which cases] — [observed behavior] because [root cause]
- [Pattern 2]: ...

## Success/Failure Contrast
[What distinguishes passing from failing cases]

## Consistency
- Systematic: [case_ids] — [what's missing]
- Flaky: [case_ids] — [what's ambiguous]
- Marginal: [case_ids] — [edge case description]

## Recommended Fixes (priority order)
1. [Highest impact fix]: addresses [N] cases — [pattern/example to add or adjust]
2. [Next fix]: ...
```

Be concise. Focus on patterns, not individual case narratives. \
The optimizer that reads your output needs actionable signal, \
not lengthy explanations. Frame fixes as patterns/examples to add, \
not imperative rules (avoid suggesting "ALWAYS", "NEVER", "MUST").\
"""


OPTIMIZER_SYSTEM = """\
You are a prompt optimizer. You receive a developer prompt that \
teaches a coding assistant how to use its tools, along with test \
results showing which tool-selection patterns the assistant gets \
right and wrong.

Your job: edit the prompt so more tests pass.

Style — the prompt teaches through patterns and examples, not rules:
- Good: "Plan a refactor → plan_agent:\\n   plan_agent(goal='...')"
- Bad: "You MUST call plan_agent. NEVER skip it. ALWAYS use it."
- If the current prompt contains imperative rules (MUST, NEVER, \
ALWAYS, ABSOLUTE RULE, etc.), replace them with a pattern that \
demonstrates the right behavior. Rules are noise — examples teach.

Approach:
- Study the analyst's failure diagnosis (included in the input) and \
address the highest-priority patterns first.
- For each failing case, trace why the assistant picked the wrong \
tool, then add or adjust a pattern that demonstrates the right \
choice.
- Leave phrasing that drives 100%-pass cases alone.
- Remove lines that aren't pulling their weight. Shorter prompts \
that score the same are better prompts.
- Stay within 130% of the original prompt length.

Output the modified prompt only. No commentary, no fences.\
"""


OBSERVER_SYSTEM = """\
You tune the optimizer's instructions. The optimizer edits a developer \
prompt to improve tool-selection test scores. Your output replaces \
the optimizer's instructions — stay at the meta level (how the \
optimizer should work, not the prompt itself).

Read the iteration history and look for trends:
- Improving → small refinements only.
- Plateau (same score 2+ iters) → the optimizer is stuck. Remove \
ineffective guidance and try a different angle.
- Regression → the last change hurt. Steer the optimizer away from \
that kind of edit.
- Oscillation → edits are too broad. Push toward smaller, more \
targeted changes.

If scores haven't improved for 3+ iterations, nudge the optimizer \
toward more structural changes — reordering sections, pruning dead \
weight, or replacing rules with concrete tool chain examples.

Core principle: the developer prompt teaches by showing patterns \
(e.g., "Find and modify → search then read_file then edit_file"), \
not by stating rules (e.g., "always search before editing"). If the \
optimizer is producing rules, steer it back toward examples.

You can adjust: which failure modes to prioritize, whether to add \
patterns or simplify, length guidance, and diagnosis-to-fix mappings.

Make 2-3 targeted edits. Remove guidance that isn't working. Stay \
under 150% of the input length.

Output the modified optimizer instructions only.\
"""


def _diversify_prompts(
    client: Any,
    model: str,
    cases: list[dict[str, Any]],
    n_variants: int,
    provider: LLMProvider | None = None,
) -> dict[str, list[str]]:
    """Generate paraphrased prompt variants for each test case.

    Returns {case_id: [variant1, variant2, ...]}. The original
    user_prompt is always included as the first variant.
    """
    prov = provider or create_provider("openai")
    result: dict[str, list[str]] = {}

    for ci, case in enumerate(cases):
        cid = case["id"]
        original = case["user_prompt"]

        if n_variants <= 1:
            result[cid] = [original]
            continue

        # Use cached variants if present in the test case
        cached = case.get("user_prompts")
        if cached and isinstance(cached, list) and len(cached) >= n_variants:
            # Ensure original is always at slot 0
            variants = cached[:n_variants]
            if variants[0] != original:
                variants = [v for v in variants if v != original]
                variants.insert(0, original)
                variants = variants[:n_variants]
            result[cid] = variants
            print(
                f"  {DIM}[{ci + 1}/{len(cases)}] {cid}...{RESET}"
                f" {CYAN}{len(result[cid])} cached{RESET}"
            )
            continue

        # Partial cache: keep existing variants, only generate the delta
        existing: list[str] = []
        if cached and isinstance(cached, list):
            # Ensure original is at slot 0
            existing = [v for v in cached if v != original]
            existing.insert(0, original)

        needed = n_variants - max(len(existing), 1)  # original is always slot 0
        if needed <= 0:
            result[cid] = (existing or [original])[:n_variants]
            print(
                f"  {DIM}[{ci + 1}/{len(cases)}] {cid}...{RESET}"
                f" {CYAN}{len(result[cid])} cached{RESET}"
            )
            continue

        print(f"  {DIM}[{ci + 1}/{len(cases)}] {cid}...{RESET}", end="", flush=True)
        # Generate only the missing variants
        existing_json = json.dumps(existing[1:]) if len(existing) > 1 else "[]"
        user_content = f"Original prompt: {json.dumps(original)}\n\n"
        if len(existing) > 1:
            user_content += f"Existing variations (do NOT repeat these): {existing_json}\n\n"
        user_content += (
            f"Generate {needed} new paraphrased variations of the original prompt. "
            f"Output a JSON array of {needed} strings."
        )

        try:
            cr = prov.create_completion(
                client=client,
                model=model,
                messages=[
                    {"role": "system", "content": DIVERSIFIER_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=8192,
                temperature=0.8,
                reasoning_effort="low",
            )
            raw = (cr.content or "").strip()
            # Strip reasoning tags
            raw = re.sub(
                r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
                "",
                raw,
                flags=re.DOTALL,
            ).strip()
            # Strip markdown fences
            fence_match = re.search(r"```[^\n]*\n(.*?)```", raw, re.DOTALL)
            if fence_match:
                raw = fence_match.group(1).strip()

            new_variants = json.loads(raw)
            if isinstance(new_variants, list) and all(isinstance(v, str) for v in new_variants):
                # Merge existing + new, then deduplicate
                base = existing if existing else [original]
                seen: set[str] = {v.strip().lower() for v in base}
                unique: list[str] = list(base)
                dupes = 0
                for v in new_variants[:needed]:
                    key = v.strip().lower()
                    if key in seen:
                        dupes += 1
                    else:
                        seen.add(key)
                        unique.append(v)
                result[cid] = unique
                dupe_note = f", {dupes} dupes removed" if dupes else ""
                short_note = (
                    f" {YELLOW}(< {n_variants} requested, will cycle){RESET}"
                    if len(unique) < n_variants
                    else ""
                )
                print(f" {GREEN}{len(unique)} unique{dupe_note}{RESET}{short_note}")
            else:
                result[cid] = [original]
                print(f" {YELLOW}parse error, using original{RESET}")
        except Exception as e:
            _log(f"\n  Diversifier failed for {cid}: {e}", dim=True)
            result[cid] = [original]

    # Summary statistics
    total_variants = sum(len(v) for v in result.values())
    cases_with_variants = sum(1 for v in result.values() if len(v) > 1)
    avg_variants = total_variants / len(result) if result else 0
    print(
        f"  {total_variants} total variants across {cases_with_variants} cases"
        f" (avg {avg_variants:.1f}/case)"
    )

    return result


def _observe_and_update_optimizer(
    client: Any,
    model: str,
    optimizer_system: str,
    iterations: list[dict[str, Any]],
    provider: LLMProvider | None = None,
) -> str:
    """Analyze optimizer behavior and return a modified OPTIMIZER_SYSTEM."""
    parts: list[str] = []
    for i in range(1, len(iterations)):
        prev, curr = iterations[i - 1], iterations[i]
        prev_agg = prev.get("aggregate", {})
        curr_agg = curr.get("aggregate", {})
        prev_len = len(prev.get("prompt", ""))
        curr_len = len(curr.get("prompt", ""))
        len_delta = curr_len - prev_len
        score_prev = prev_agg.get("overall_pass_rate", 0)
        score_curr = curr_agg.get("overall_pass_rate", 0)
        score_delta = score_curr - score_prev

        part = f"Iteration {i - 1} → {i}:\n"
        part += f"  Prompt: {len_delta:+d} chars ({prev_len} → {curr_len})\n"
        part += f"  Score: {score_prev:.0%} → {score_curr:.0%} ({score_delta:+.0%})\n"

        prev_rates = prev_agg.get("per_case_pass_rates", {})
        curr_rates = curr_agg.get("per_case_pass_rates", {})
        improved: list[str] = []
        regressed: list[str] = []
        for case_id in set(prev_rates) | set(curr_rates):
            p = prev_rates.get(case_id, 0)
            c = curr_rates.get(case_id, 0)
            if c > p:
                improved.append(f"{case_id} ({p:.0%}→{c:.0%})")
            elif c < p:
                regressed.append(f"{case_id} ({p:.0%}→{c:.0%})")
        if improved:
            part += f"  Improved: {', '.join(improved)}\n"
        if regressed:
            part += f"  Regressed: {', '.join(regressed)}\n"
        if curr.get("prompt_diff"):
            diff_text = curr["prompt_diff"][:500]
            part += f"  Diff:\n{diff_text}\n"
        parts.append(part)

    # Summarize what the optimizer's output looked like (without showing
    # full developer messages, which cause the observer to mimic them)
    behavior_notes: list[str] = []
    for it in iterations[-3:]:
        idx = it.get("iteration", "?")
        prompt = it.get("prompt", "")
        score = it.get("aggregate", {}).get("overall_pass_rate", 0)
        has_bullets = "- " in prompt or "* " in prompt
        has_numbers = bool(re.search(r"^\d+\.", prompt, re.MULTILINE))
        has_headers = "**" in prompt or "##" in prompt
        has_rules = bool(
            re.search(
                r"(?i)(always|never|do not|must|rule|important)",
                prompt,
            )
        )
        has_patterns = "→" in prompt
        notes: list[str] = []
        if has_rules and not has_patterns:
            notes.append("rule-heavy, few patterns")
        elif has_rules and has_patterns:
            notes.append("mixed rules and patterns")
        elif has_patterns:
            notes.append("pattern-based")
        if has_bullets or has_numbers:
            notes.append("used bullet/numbered lists")
        if has_headers:
            notes.append("used bold headers")
        if len(prompt) > 1200:
            notes.append(f"length={len(prompt)} chars (over 1200)")
        elif len(prompt) < 600:
            notes.append(f"length={len(prompt)} chars (under 600)")
        else:
            notes.append(f"length={len(prompt)} chars")
        style = ", ".join(notes) if notes else "prose style"
        behavior_notes.append(f"Iteration {idx} ({score:.0%}): {style}")

    user_content = (
        f"## Optimizer Instructions (edit these)\n"
        f"```\n{optimizer_system}\n```\n\n"
        f"## What the Optimizer Produced (style observations, not content to copy)\n"
        + "\n".join(behavior_notes)
        + "\n\n## Iteration History\n"
        + "\n".join(parts)
    )

    prov = provider or create_provider("openai")
    cr = prov.create_completion(
        client=client,
        model=model,
        messages=[
            {"role": "system", "content": OBSERVER_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        max_tokens=8192,
        temperature=0.3,
        reasoning_effort="low",
    )

    result = cr.content or optimizer_system
    result = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
        "",
        result,
        flags=re.DOTALL,
    ).strip()

    # Strip markdown code fences if wrapped
    fence_match = re.search(r"```[^\n]*\n(.*?)```", result, re.DOTALL)
    if fence_match:
        result = fence_match.group(1).strip()

    # Reject degenerate outputs (>200% of input length)
    if len(result) > len(optimizer_system) * 2.0:
        _log(
            f"  Observer output too long ({len(result)} vs {len(optimizer_system)}), "
            "keeping current optimizer system",
            dim=True,
        )
        return optimizer_system

    return result


def _classify_failure(
    run: dict[str, Any],
    expected_actions: list[dict[str, Any]],
) -> str:
    """Classify a failed run into a failure mode bucket."""
    tools = run.get("tool_sequence", [])
    if run.get("json_dump"):
        return "json_dump"
    if not tools:
        return "no_tool_call"
    detail = run.get("detail", "")
    lower_detail = detail.lower()
    if "timed out" in lower_detail:
        return "timeout"
    if "skipped" in lower_detail:
        return "skipped"
    if detail.startswith("Error:") or lower_detail.startswith("subprocess error:"):
        return "error"

    unmatched = run.get("unmatched", [])
    extra = run.get("extra_tools", [])
    matched = run.get("matched", [])

    if not unmatched and extra:
        return "extra_tools"

    # Check for tool substitution — expected one tool, got a different one
    expected_names = {expected_actions[i]["tool"] for i in unmatched if i < len(expected_actions)}
    actual_names = set(tools)
    if expected_names and not expected_names & actual_names:
        return "wrong_tool"

    # Check if right tools were called but args didn't match
    if matched and unmatched:
        unmatched_expected = {
            expected_actions[i]["tool"] for i in unmatched if i < len(expected_actions)
        }
        if unmatched_expected & actual_names:
            return "wrong_args"

    return "missing_tool"


def _build_failure_analysis(
    iteration_result: dict[str, Any],
    test_cases: list[dict[str, Any]],
) -> str:
    """Build a semantic failure analysis summary across all cases."""
    case_index = {c["id"]: c for c in test_cases}

    # Classify every failed run
    mode_cases: dict[str, list[str]] = {}  # mode -> [case_id, ...]
    mode_details: dict[str, list[str]] = {}  # mode -> [detail strings]
    consistency: dict[str, str] = {}  # case_id -> systematic|flaky|marginal

    for case_id, case_result in iteration_result["cases"].items():
        case_def = case_index.get(case_id)
        expected = case_def.get("expected_actions", []) if case_def else []
        pr = case_result["pass_rate"]

        if pr == 1.0:
            continue

        # Consistency classification
        if pr == 0:
            consistency[case_id] = "systematic"
        elif pr < 0.8:
            consistency[case_id] = "flaky"
        else:
            consistency[case_id] = "marginal"

        for run in case_result["runs"]:
            if run.get("pass"):
                continue
            mode = _classify_failure(run, expected)
            mode_cases.setdefault(mode, [])
            if case_id not in mode_cases[mode]:
                mode_cases[mode].append(case_id)

            # Build a detail string for tool substitution cases
            if mode == "wrong_tool":
                expected_names = [
                    expected[i]["tool"] for i in run.get("unmatched", []) if i < len(expected)
                ]
                actual = run.get("tool_sequence", [])
                detail = f"{case_id}: expected {expected_names}, got {actual}"
                mode_details.setdefault(mode, []).append(detail)

    if not mode_cases:
        return ""

    # Build the summary
    lines: list[str] = []

    # Failure mode summary
    mode_labels = {
        "no_tool_call": "Text-only response (no tool call made)",
        "wrong_tool": "Wrong tool selected",
        "missing_tool": "Missing required tool in sequence",
        "wrong_args": "Right tool, wrong arguments",
        "extra_tools": "Correct sequence but unnecessary extra calls",
        "timeout": "Timed out",
        "error": "Tool execution error",
        "skipped": "Skipped (fast-fail)",
        "json_dump": "Tool call emitted as JSON text instead of function call",
    }
    for mode, cases in sorted(mode_cases.items(), key=lambda x: -len(x[1])):
        label = mode_labels.get(mode, mode)
        lines.append(f"- {label}: {', '.join(cases)}")
        for detail in (mode_details.get(mode, []))[:3]:
            lines.append(f"    {detail}")

    # Consistency summary
    systematic = [c for c, v in consistency.items() if v == "systematic"]
    flaky = [c for c, v in consistency.items() if v == "flaky"]
    marginal = [c for c, v in consistency.items() if v == "marginal"]

    if systematic or flaky or marginal:
        lines.append("")
        if systematic:
            lines.append(
                f"Systematic failures (always fail, need new instruction): {', '.join(systematic)}"
            )
        if flaky:
            lines.append(
                f"Flaky failures (sometimes pass, prompt is ambiguous): {', '.join(flaky)}"
            )
        if marginal:
            lines.append(f"Marginal failures (mostly pass, minor edge case): {', '.join(marginal)}")

    return "\n".join(lines)


_ANALYST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "math",
            "description": (
                "Execute Python code for analysis. Available: numpy, scipy, "
                "collections, itertools, math, json, re. Use print() for output. "
                "Example: print(numpy.mean([0.8, 0.6, 1.0]))"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute."},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command. Use for jq, awk, sort, uniq, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to execute."},
                },
                "required": ["command"],
            },
        },
    },
]


def _exec_analyst_tool(name: str, arguments: str) -> str:
    """Execute a tool call from the analyst agent."""
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError:
        return f"Invalid JSON arguments: {arguments[:200]}"

    if name == "math":
        from turnstone.core.sandbox import execute_math_sandboxed

        output, is_error = execute_math_sandboxed(args.get("code", ""), timeout=15.0)
        return output[:4000]
    elif name == "bash":
        import subprocess

        try:
            proc = subprocess.run(
                ["bash", "-c", args.get("command", "")],
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = proc.stdout + proc.stderr
            return output[:4000] or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 15s"
    return f"Unknown tool: {name}"


def _run_analyst(
    client: Any,
    model: str,
    test_cases: list[dict[str, Any]],
    iteration_result: dict[str, Any],
    provider: LLMProvider | None = None,
    optimize_tools: bool = False,
    tool_overrides: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Run the analyst agent to produce a semantic failure analysis.

    Multi-turn agent with math and bash tools for computing statistics.
    Phase 1 of the two-phase optimization: analyst diagnoses patterns,
    then optimizer uses the diagnosis to modify the prompt.
    """
    # Build structured input: per-case results with rule-based pre-analysis
    case_index = {c["id"]: c for c in test_cases}
    case_parts: list[str] = []
    for case_id, case_result in iteration_result["cases"].items():
        case_def = case_index.get(case_id)
        if not case_def:
            continue
        pr = case_result["pass_rate"]
        status = "PASS" if pr == 1.0 else "WEAK" if pr >= 0.5 else "FAIL"

        part = (
            f"[{status}] {case_id} (pass_rate={pr:.0%})\n"
            f"  User prompt: {case_def['user_prompt']}\n"
            f"  Expected tools: {json.dumps(case_def.get('expected_actions', []))}\n"
            f"  Actual sequences: "
            f"{[r.get('tool_sequence', []) for r in case_result['runs']]}"
        )

        # Add per-run failure classifications
        expected = case_def.get("expected_actions", [])
        failed_runs = [r for r in case_result["runs"] if not r.get("pass")]
        if failed_runs:
            modes = [_classify_failure(r, expected) for r in failed_runs]
            part += f"\n  Failure modes: {modes}"

        case_parts.append(part)

    # Include rule-based pre-analysis as a starting point
    rule_analysis = _build_failure_analysis(iteration_result, test_cases)

    # Always show available tool names so the analyst doesn't hallucinate
    # about missing tools
    tool_names = sorted(
        t["function"]["name"] for t in TOOLS if t["function"]["name"] not in _MCP_ONLY_TOOLS
    )
    user_content = f"## Available Tools\n{', '.join(tool_names)}\n\n"
    user_content += "## Test Results\n" + "\n\n".join(case_parts)
    if rule_analysis:
        user_content += f"\n\n## Rule-Based Pre-Analysis\n{rule_analysis}"

    if optimize_tools:
        # Include current tool descriptions (with any overrides applied)
        effective_tools = _apply_tool_overrides(TOOLS, tool_overrides) if tool_overrides else TOOLS
        tool_index = {t["function"]["name"]: t for t in effective_tools}
        # Collect tools that appear in expected or actual sequences
        relevant_names: set[str] = set()
        for case_id, case_result in iteration_result["cases"].items():
            case_def = case_index.get(case_id)
            if not case_def:
                continue
            for action in case_def.get("expected_actions", []):
                if isinstance(action, str):
                    relevant_names.add(action)
                elif isinstance(action, dict):
                    tool_name = action.get("tool")
                    if isinstance(tool_name, str):
                        relevant_names.add(tool_name)
            for run in case_result["runs"]:
                for tool_name in run.get("tool_sequence", []):
                    relevant_names.add(tool_name)
        tool_parts: list[str] = []
        for name in sorted(relevant_names):
            tool = tool_index.get(name)
            if tool:
                desc = tool["function"].get("description", "")
                tool_parts.append(f"**{name}**: {desc}")
        if tool_parts:
            user_content += "\n\n## Current Tool Descriptions\n" + "\n\n".join(tool_parts)

    user_content += (
        "\n\nAnalyze the semantic patterns across these results. "
        "Focus on WHY failures happen and what passing cases have in common. "
        "Use the math or bash tools if you need to compute statistics, "
        "build confusion matrices, or analyze distributions."
    )

    analyst_system = ANALYST_SYSTEM
    if optimize_tools:
        analyst_system += (
            "\n\nFOCUS: Tool description optimization mode. The system prompt "
            "is frozen — your recommendations should target tool descriptions "
            "and schemas, not system prompt wording. Analyze:\n"
            "- Which tools are confused with each other and why\n"
            "- What description changes would disambiguate them\n"
            "- Whether parameter names/descriptions mislead the model"
        )

    prov = provider or create_provider("openai")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": analyst_system},
        {"role": "user", "content": user_content},
    ]

    # Multi-turn loop: let the analyst call tools up to 5 rounds
    max_turns = 5
    for _turn in range(max_turns):
        cr = prov.create_completion(
            client=client,
            model=model,
            messages=messages,
            tools=_ANALYST_TOOLS,
            max_tokens=8192,
            temperature=0.3,
            reasoning_effort="medium",
        )

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": cr.content or None,
        }
        if cr.tool_calls:
            assistant_msg["tool_calls"] = cr.tool_calls[:5]
        messages.append(assistant_msg)

        if not cr.tool_calls:
            break

        # Execute tool calls
        for tc in assistant_msg["tool_calls"]:
            func_name = tc["function"]["name"]
            output = _exec_analyst_tool(func_name, tc["function"]["arguments"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": output,
                }
            )

    # Extract final text response
    result = ""
    for msg in reversed(messages):
        if msg["role"] == "assistant" and msg.get("content"):
            result = msg["content"]
            break

    # Strip reasoning tags if present
    result = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
        "",
        result,
        flags=re.DOTALL,
    ).strip()

    return result


TOOL_OPTIMIZER_SYSTEM = """\
You are a tool description optimizer for an LLM coding assistant. You \
receive the current tool definitions and test results showing where the \
assistant picked the wrong tool.

GOAL: Modify tool descriptions so the assistant picks the correct tool \
for each task. Make MINIMAL, TARGETED changes.

WHAT YOU CAN CHANGE:
- The top-level "description" field on each tool.
- The "description" field on individual parameters.

WHAT YOU CANNOT CHANGE:
- Tool names, parameter names, types, or required fields.
- The number of tools or their parameter schemas.

PRINCIPLES:
- When two tools are confused, clarify the BOUNDARY between them. \
State when to use each one and when NOT to.
- Add negative guidance ("Do NOT use this for X") when confusion \
is systematic.
- Keep descriptions concise. Every word should help tool selection.
- Preserve accurate descriptions of what the tool actually does.
- Do not invent capabilities a tool does not have.

OUTPUT FORMAT: A JSON object mapping tool names to override objects. \
Only include tools you are modifying. Each override object can have:
- "description": new top-level description string
- "parameters": object mapping parameter names to new description strings

Example:
{"bash": {"description": "Execute a bash command. For running programs, \
git, and system commands. Do NOT use for reading files (use read_file) \
or searching code (use search)."}}

Output ONLY the JSON object. No commentary outside the JSON.\
"""


def _propose_tool_overrides(
    client: Any,
    model: str,
    current_tools: list[dict[str, Any]],
    current_overrides: dict[str, dict[str, Any]],
    test_cases: list[dict[str, Any]],
    iteration_result: dict[str, Any],
    analyst_output: str,
    provider: LLMProvider | None = None,
) -> dict[str, dict[str, Any]]:
    """Propose tool description overrides based on failure analysis.

    Returns a merged override dict (current + new changes).
    """
    # Build effective tool descriptions for display
    effective_tools = _apply_tool_overrides(current_tools, current_overrides)
    tool_parts: list[str] = []
    for tool in effective_tools:
        fn = tool["function"]
        params = fn.get("parameters", {}).get("properties", {})
        param_descs = ", ".join(f"{k}: {v.get('description', '')}" for k, v in params.items())
        tool_parts.append(f"- {fn['name']}: {fn['description']}")
        if param_descs:
            tool_parts.append(f"    params: {param_descs}")

    # Collect only wrong_tool failures
    case_index = {c["id"]: c for c in test_cases}
    confusion_parts: list[str] = []
    for case_id, case_result in iteration_result["cases"].items():
        if case_result["pass_rate"] == 1.0:
            continue
        case_def = case_index.get(case_id)
        expected = case_def.get("expected_actions", []) if case_def else []
        for run in case_result["runs"]:
            if run.get("pass"):
                continue
            if _classify_failure(run, expected) == "wrong_tool" and case_def:
                expected_names = [
                    expected[i]["tool"] for i in run.get("unmatched", []) if i < len(expected)
                ]
                actual = run.get("tool_sequence", [])
                confusion_parts.append(
                    f"- {case_id}: expected {expected_names}, got {actual}\n"
                    f"    prompt: {case_def['user_prompt']}"
                )

    user_content = "## Current Tool Descriptions\n" + "\n".join(tool_parts) + "\n\n"
    if current_overrides:
        user_content += (
            f"## Active Overrides\nAlready modified: {', '.join(sorted(current_overrides))}\n\n"
        )
    user_content += "## Tool Confusion Failures\n" + "\n".join(confusion_parts) + "\n\n"
    if analyst_output:
        user_content += f"## Analyst Diagnosis\n{analyst_output}\n\n"
    user_content += (
        "Modify the MINIMUM tool descriptions needed to resolve the confusion. "
        "Output ONLY a JSON override object."
    )

    prov = provider or create_provider("openai")
    cr = prov.create_completion(
        client=client,
        model=model,
        messages=[
            {"role": "system", "content": TOOL_OPTIMIZER_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        max_tokens=8192,
        temperature=0.3,
        reasoning_effort="medium",
    )

    raw = (cr.content or "").strip()
    # Strip reasoning tags
    raw = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>", "", raw, flags=re.DOTALL
    ).strip()
    # Strip markdown fences
    fence_match = re.search(r"```[^\n]*\n(.*?)```", raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()

    try:
        new_overrides = json.loads(raw)
    except json.JSONDecodeError:
        _log(f"  Tool optimizer returned invalid JSON: {raw[:200]}", dim=True)
        return current_overrides

    if not isinstance(new_overrides, dict):
        return current_overrides

    # Validate: only allow known tool names, only description/parameters keys
    valid_names = {t["function"]["name"] for t in current_tools}
    validated: dict[str, dict[str, Any]] = {}
    for name, override in new_overrides.items():
        if name not in valid_names or not isinstance(override, dict):
            continue
        clean: dict[str, Any] = {}
        if "description" in override and isinstance(override["description"], str):
            clean["description"] = override["description"]
        if "parameters" in override and isinstance(override["parameters"], dict):
            clean["parameters"] = {
                k: v for k, v in override["parameters"].items() if isinstance(v, str)
            }
        if clean:
            validated[name] = clean

    # Merge: new overrides take precedence, deep-merge parameters
    merged = {**current_overrides}
    for name, override in validated.items():
        if name in merged:
            existing = merged[name]
            combined = {**existing, **override}
            if "parameters" in existing and "parameters" in override:
                combined["parameters"] = {**existing["parameters"], **override["parameters"]}
            merged[name] = combined
        else:
            merged[name] = override

    return merged


def _propose_prompt_modification(
    client: Any,
    model: str,
    current_prompt: str,
    test_cases: list[dict[str, Any]],
    iteration_result: dict[str, Any],
    history: list[dict[str, Any]],
    optimizer_system: str = OPTIMIZER_SYSTEM,
    provider: LLMProvider | None = None,
    parent_scores: dict[str, float] | None = None,
    analyst_output: str = "",
    tool_overrides: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Use the model to propose a new prompt based on evaluation results."""
    # Build summary of results
    case_index = {c["id"]: c for c in test_cases}
    summary_parts: list[str] = []
    for case_id, case_result in iteration_result["cases"].items():
        case_def = case_index.get(case_id)
        if not case_def:
            continue
        pr = case_result["pass_rate"]
        status = "PASS" if pr == 1.0 else "WEAK" if pr >= 0.5 else "FAIL"
        delta_str = ""
        if parent_scores is not None:
            delta = pr - parent_scores.get(case_id, 0)
            delta_str = f", delta={delta:+.0%}"
        summary_parts.append(
            f"[{status}] {case_id} (pass_rate={case_result['pass_rate']:.0%}{delta_str})\n"
            f"  User prompt: {case_def['user_prompt']}\n"
            f"  Expected: {json.dumps(case_def['expected_actions'])}\n"
            f"  Actual sequences: "
            f"{[r.get('tool_sequence', []) for r in case_result['runs']]}"
        )

    # Build history summary (last 3 iterations)
    history_parts: list[str] = []
    for h in history[-3:]:
        agg = h.get("aggregate", {})
        history_parts.append(
            f"Iteration {h['iteration']}: "
            f"overall_pass_rate={agg.get('overall_pass_rate', 0):.0%}, "
            f"per_case={agg.get('per_case_pass_rates', {})}"
        )

    history_text = "\n".join(history_parts) if history_parts else "(first iteration)"

    # Separate passing vs failing cases for clearer diagnosis
    passing = [p for p in summary_parts if p.startswith("[PASS]")]
    failing = [p for p in summary_parts if not p.startswith("[PASS]")]

    user_content = f"## Current Prompt\n```\n{current_prompt}\n```\n\n"
    if passing:
        user_content += (
            "## Passing Tests (leave the phrasing that drives these alone)\n"
            + "\n\n".join(passing)
            + "\n\n"
        )
    if failing:
        user_content += (
            "## Failing Tests (diagnose each, one targeted fix per case)\n"
            + "\n\n".join(failing)
            + "\n\n"
        )

    # Failure analysis from analyst agent (phase 1)
    if analyst_output:
        user_content += f"## Failure Analysis (from analyst)\n{analyst_output}\n\n"

    if tool_overrides:
        user_content += (
            "## Note: Tool Descriptions Modified\n"
            "The tool optimizer has customized descriptions for: "
            f"{', '.join(sorted(tool_overrides))}. "
            "You do not need to add workarounds for tool confusion already "
            "addressed by the modified descriptions.\n\n"
        )

    user_content += (
        f"## Score History\n{history_text}\n\n"
        "Fix failing tests without breaking passing ones. "
        "Output the modified prompt text only."
    )

    prov = provider or create_provider("openai")
    cr = prov.create_completion(
        client=client,
        model=model,
        messages=[
            {"role": "system", "content": optimizer_system},
            {"role": "user", "content": user_content},
        ],
        max_tokens=16384,
        temperature=0.6,
        reasoning_effort="medium",
    )

    new_prompt = cr.content or current_prompt

    # Strip reasoning tags if present
    new_prompt = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
        "",
        new_prompt,
        flags=re.DOTALL,
    ).strip()

    # Strip markdown code fences if the model wrapped the prompt.
    # Also discard any explanation text outside the fences.
    fence_match = re.search(r"```[^\n]*\n(.*?)```", new_prompt, re.DOTALL)
    if fence_match:
        new_prompt = fence_match.group(1).strip()
    elif new_prompt.startswith("```"):
        # Opening fence without closing — strip just the first line
        new_prompt = "\n".join(new_prompt.split("\n")[1:]).strip()

    return new_prompt


def _simple_diff(old: str, new: str) -> str:
    """Generate a simple line-level diff between two prompts."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after")
    return "".join(diff)


# ─── Evolution tree helpers ───────────────────────────────────────────────────


def _ucb_select(nodes: dict[int, EvolutionNode], c: float) -> int:
    """Select the node with highest UCB1 score.

    Unvisited nodes (visit_count == 0) are selected first, in creation order.
    N (total plays) is the sum of all visit counts across nodes.
    """
    best_id = 0
    best_score = -1.0
    total_visits = sum(n.visit_count for n in nodes.values())
    log_n = math.log(total_visits) if total_visits > 1 else 0.0
    for nid in sorted(nodes):
        node = nodes[nid]
        if node.visit_count == 0:
            return nid
        exploit = node.score
        explore = c * math.sqrt(log_n / node.visit_count)
        ucb = exploit + explore
        if ucb > best_score:
            best_score = ucb
            best_id = nid
    return best_id


def _node_to_dict(node: EvolutionNode) -> dict[str, Any]:
    """Serialize an EvolutionNode for JSON output."""
    d: dict[str, Any] = {
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "prompt": node.prompt,
        "score": node.score,
        "visit_count": node.visit_count,
        "children": node.children,
        "iteration": node.iteration,
    }
    if node.tool_overrides:
        d["tool_overrides"] = node.tool_overrides
    return d


def _apply_tool_overrides(
    tools: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply description overrides to a tool list.

    Returns the original list when overrides is empty, otherwise a new list
    with deep-copied entries for modified tools (unmodified tools are shared).
    Never mutates the input tools or the module-level TOOLS constant.
    """
    if not overrides:
        return tools
    result = []
    for tool in tools:
        name = tool["function"]["name"]
        if name not in overrides:
            result.append(tool)
            continue
        t = copy.deepcopy(tool)
        ov = overrides[name]
        if "description" in ov:
            t["function"]["description"] = ov["description"]
        for param, desc in ov.get("parameters", {}).items():
            props = t["function"].get("parameters", {}).get("properties", {})
            if param in props:
                props[param]["description"] = desc
        result.append(t)
    return result


def _filter_for_optimizer(
    cases: list[dict[str, Any]],
    iter_result: dict[str, Any],
    holdout_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Strip holdout cases from optimizer feedback, including aggregates."""
    if not holdout_ids:
        return cases, iter_result
    filtered_cases = [c for c in cases if c["id"] not in holdout_ids]
    filtered_case_results = {
        cid: cr for cid, cr in iter_result["cases"].items() if cid not in holdout_ids
    }
    # Recompute aggregate from non-holdout cases only so no holdout signal leaks
    total_runs = sum(len(cr["runs"]) for cr in filtered_case_results.values())
    total_passes = sum(
        sum(1 for r in cr["runs"] if r["pass"]) for cr in filtered_case_results.values()
    )
    filtered_result = dict(iter_result)
    filtered_result["cases"] = filtered_case_results
    filtered_result["aggregate"] = {
        **iter_result["aggregate"],
        "overall_pass_rate": total_passes / total_runs if total_runs else 0,
        "overall_avg_score": (
            sum(cr["avg_score"] for cr in filtered_case_results.values())
            / len(filtered_case_results)
            if filtered_case_results
            else 0
        ),
        "per_case_pass_rates": {cid: cr["pass_rate"] for cid, cr in filtered_case_results.items()},
    }
    return filtered_cases, filtered_result


def _compute_holdout_score(iter_result: dict[str, Any], holdout_ids: set[str]) -> float:
    """Compute pass rate from holdout cases only (or all if no holdout)."""
    cases = iter_result.get("cases", {})
    if holdout_ids:
        rates = [cr["pass_rate"] for cid, cr in cases.items() if cid in holdout_ids]
    else:
        rates = [cr["pass_rate"] for cr in cases.values()]
    return sum(rates) / len(rates) if rates else 0.0


# ─── Summary & reporting ─────────────────────────────────────────────────────


def _print_summary_table(iter_result: dict[str, Any]) -> None:
    """Print a formatted summary table for an iteration's results."""
    cases = iter_result.get("cases", {})
    if not cases:
        return

    # Column widths
    max_id = max(len(cid) for cid in cases)
    max_id = max(max_id, 4)  # min "CASE" header

    header = f"  {'CASE'.ljust(max_id)}  {'PASS':>5}  {'AVG':>5}  {'RUNS':>5}  {'TIME':>6}  STATUS"
    print(f"\n{BOLD}{header}{RESET}")
    print(f"  {'─' * (max_id + 34)}")

    total_passes = 0
    total_runs_count = 0
    total_elapsed = 0.0

    for case_id, cr in cases.items():
        runs = cr.get("runs", [])
        passes = sum(1 for r in runs if r.get("pass"))
        run_count = len(runs)
        avg_score = cr.get("avg_score", 0)
        elapsed = sum(r.get("elapsed", 0) for r in runs)
        pass_rate = cr.get("pass_rate", 0)

        total_passes += passes
        total_runs_count += run_count
        total_elapsed += elapsed

        if pass_rate == 1.0:
            status = f"{GREEN}PASS{RESET}"
        elif pass_rate >= 0.5:
            status = f"{YELLOW}WEAK{RESET}"
        else:
            status = f"{RED}FAIL{RESET}"

        print(
            f"  {case_id.ljust(max_id)}  {pass_rate:>4.0%}  {avg_score:>5.2f}  "
            f"{passes:>2}/{run_count:<2}  {elapsed:>5.1f}s  {status}"
        )

    # Totals
    overall_pass_rate = total_passes / total_runs_count if total_runs_count else 0
    overall_avg = iter_result.get("aggregate", {}).get("overall_avg_score", 0)
    json_dumps = iter_result.get("aggregate", {}).get("json_dumps", 0)
    jd_str = f"  {YELLOW}({json_dumps} json_dumps){RESET}" if json_dumps else ""

    print(f"  {'─' * (max_id + 34)}")
    print(
        f"  {BOLD}{'TOTAL'.ljust(max_id)}{RESET}  {overall_pass_rate:>4.0%}  "
        f"{overall_avg:>5.2f}  "
        f"{total_passes:>2}/{total_runs_count:<2}  "
        f"{total_elapsed:>5.1f}s{jd_str}"
    )


def _append_summary_tsv(
    path: str,
    iter_result: dict[str, Any],
    case_ids: list[str],
    cumulative_tokens: int = 0,
    node_score: float = 0.0,
    wall_time: float = 0.0,
) -> None:
    """Append one row per iteration to a TSV summary file."""
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    agg = iter_result.get("aggregate", {})
    per_case = agg.get("per_case_pass_rates", {})
    prompt_len = len(iter_result.get("prompt", ""))

    with open(path, "a") as f:
        if write_header:
            cols = [
                "iter",
                "timestamp",
                "pass_rate",
                "avg_score",
                "runs",
                "json_dumps",
                "tree_node",
                "node_score",
                "elapsed_s",
                "prompt_len",
                "iter_tokens",
                "cumul_tokens",
                "tool_changes",
            ] + [f"case:{cid}" for cid in case_ids]
            f.write("\t".join(cols) + "\n")

        vals = [
            str(iter_result.get("iteration", "")),
            iter_result.get("timestamp", ""),
            f"{agg.get('overall_pass_rate', 0):.4f}",
            f"{agg.get('overall_avg_score', 0):.4f}",
            str(agg.get("total_runs", 0)),
            str(agg.get("json_dumps", 0)),
            str(iter_result.get("tree_node_id", "")),
            f"{node_score:.4f}",
            f"{wall_time:.1f}",
            str(prompt_len),
            str(agg.get("total_tokens", 0)),
            str(cumulative_tokens),
            str(len(iter_result.get("tool_overrides", {}))),
        ] + [f"{per_case.get(cid, 0):.4f}" for cid in case_ids]
        f.write("\t".join(vals) + "\n")


# ─── Main optimization loop ─────────────────────────────────────────────────


def run_optimization(
    base_url: str,
    model: str | None,
    test_file: str,
    initial_prompt: str | None = None,
    n_runs: int | None = 3,
    max_iterations: int = 5,
    temperature: float = 0.7,
    max_tokens: int = 32768,
    reasoning_effort: str = "medium",
    output_file: str = "eval_results.json",
    context_window: int = 131072,
    verbose: bool = False,
    test_timeout: int = 300,
    suite_timeout: int = 0,
    fast_fail: bool = True,
    parallel: int = 1,
    optimizer_base_url: str | None = None,
    optimizer_model: str | None = None,
    observer_base_url: str | None = None,
    observer_model: str | None = None,
    analyst_base_url: str | None = None,
    analyst_model: str | None = None,
    diversifier_base_url: str | None = None,
    diversifier_model: str | None = None,
    diversify: int = 0,
    save_variants: bool = False,
    optimize_tools: bool = False,
    tool_optimizer_base_url: str | None = None,
    tool_optimizer_model: str | None = None,
    save_tools: bool = False,
    explore_constant: float = 1.414,
) -> dict[str, Any]:
    """Main optimization loop with UCB tree search.

    Maintains an evolution tree of prompt variants. Each iteration selects
    the most promising node via UCB1, evaluates it, then uses the optimizer
    to propose a child variant. The observer tunes optimizer strategy every
    3 iterations.

    Three separate model roles:
      - test: the model being evaluated (base_url / model)
      - optimizer: rewrites the developer prompt (--optimizer-*)
      - observer: tunes the optimizer's strategy (--observer-*)

    Inheritance: observer defaults ← optimizer defaults ← test defaults.
    Provider is auto-detected from the base URL.
    """
    # --- Test model (always OpenAI-compatible for headless eval) ---
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
    )
    if not model:
        from turnstone.core.model_registry import detect_model

        detected, _ = detect_model(client)
        assert detected is not None  # fatal=True guarantees non-None or SystemExit
        model = detected

    # --- Optimizer model (inherits from test if not specified) ---
    opt_base = optimizer_base_url or base_url
    opt_model = optimizer_model or model
    opt_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(opt_base) == "anthropic" else "OPENAI_API_KEY"
    )
    opt_key = os.environ.get(opt_key_env, api_key)
    opt_client, opt_provider = _make_client_and_provider(opt_base, opt_key)

    # --- Observer model (inherits from optimizer if not specified) ---
    obs_base = observer_base_url or opt_base
    obs_model = observer_model or opt_model
    obs_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(obs_base) == "anthropic" else "OPENAI_API_KEY"
    )
    obs_key = os.environ.get(obs_key_env, opt_key)
    obs_client, obs_provider = _make_client_and_provider(obs_base, obs_key)

    # --- Analyst model (inherits from optimizer if not specified) ---
    ana_base = analyst_base_url or opt_base
    ana_model = analyst_model or opt_model
    ana_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(ana_base) == "anthropic" else "OPENAI_API_KEY"
    )
    ana_key = os.environ.get(ana_key_env, opt_key)
    ana_client, ana_provider = _make_client_and_provider(ana_base, ana_key)

    # --- Diversifier model (inherits from optimizer if not specified) ---
    div_base = diversifier_base_url or opt_base
    div_model = diversifier_model or opt_model
    div_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(div_base) == "anthropic" else "OPENAI_API_KEY"
    )
    div_key = os.environ.get(div_key_env, opt_key)
    div_client, div_provider = _make_client_and_provider(div_base, div_key)

    # --- Tool optimizer model (inherits from optimizer if not specified) ---
    tool_opt_base = tool_optimizer_base_url or opt_base
    tool_opt_model = tool_optimizer_model or opt_model
    tool_opt_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(tool_opt_base) == "anthropic" else "OPENAI_API_KEY"
    )
    tool_opt_key = os.environ.get(tool_opt_key_env, opt_key)
    tool_opt_client, tool_opt_provider = _make_client_and_provider(tool_opt_base, tool_opt_key)

    # Log role assignments
    _log(f"  Test model:  {model} @ {base_url}", dim=True)
    _log(f"  Optimizer:   {opt_model} @ {opt_base}", dim=True)
    if obs_model != opt_model or obs_base != opt_base:
        _log(f"  Observer:    {obs_model} @ {obs_base}", dim=True)
    if ana_model != opt_model or ana_base != opt_base:
        _log(f"  Analyst:     {ana_model} @ {ana_base}", dim=True)
    if diversify > 0 and (div_model != opt_model or div_base != opt_base):
        _log(f"  Diversifier: {div_model} @ {div_base}", dim=True)
    if optimize_tools and (tool_opt_model != opt_model or tool_opt_base != opt_base):
        _log(f"  Tool opt:    {tool_opt_model} @ {tool_opt_base}", dim=True)

    # Load test cases
    with open(test_file) as f:
        suite: dict[str, Any] = json.load(f)

    cases: list[dict[str, Any]] = suite["cases"]
    for i, case in enumerate(cases):
        if "id" not in case:
            raise SystemExit(f"Test case {i} missing required 'id' field")
        if "user_prompt" not in case:
            raise SystemExit(f"Test case '{case.get('id', i)}' missing 'user_prompt'")
    defaults = suite.get("defaults", {})
    # Precedence: CLI arg (non-None) > tests.json defaults > code default (3)
    resolved_n_runs: int = n_runs if n_runs is not None else int(defaults.get("n_runs", 3))

    total_runs = sum(c.get("n_runs", resolved_n_runs) for c in cases)
    print(
        f"  {len(cases)} cases, {resolved_n_runs} runs/case ({total_runs} total), "
        f"max {max_iterations} iterations"
    )

    # Holdout split — holdout cases are evaluated but excluded from optimizer feedback
    holdout_ids: set[str] = {c["id"] for c in cases if c.get("holdout", False)}
    training_count = len(cases) - len(holdout_ids)
    if holdout_ids and training_count < 2:
        _log(
            f"  Warning: only {training_count} non-holdout cases, disabling holdout split",
            dim=True,
        )
        holdout_ids = set()
    elif holdout_ids:
        _log(
            f"  Holdout: {len(holdout_ids)} cases ({', '.join(sorted(holdout_ids))})",
            dim=True,
        )

    # Get initial prompt
    if initial_prompt is None:
        # Extract the default developer message from a temporary ChatSession
        tmp = ChatSession(
            client=client,
            model=model,
            ui=NullUI(),
            instructions=None,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_timeout=30,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
        )
        initial_prompt = next(
            (m["content"] for m in tmp.system_messages if m["role"] in ("developer", "system")),
            None,
        )
        if initial_prompt is None:
            raise SystemExit("No developer prompt found. Provide one with --prompt <file>")
        # Strip memory reminder — it's a runtime artifact, not part of the prompt
        initial_prompt = re.sub(
            r"\n*REMINDER: You currently have \d+ memories stored\..*$",
            "",
            initial_prompt,
        ).strip()

    # --- Evolution tree ---
    nodes: dict[int, EvolutionNode] = {
        0: EvolutionNode(node_id=0, parent_id=None, prompt=initial_prompt),
    }
    next_node_id = 1

    current_optimizer_system = OPTIMIZER_SYSTEM

    results: dict[str, Any] = {
        "meta": {
            "model": model,
            "base_url": base_url,
            "optimizer_model": opt_model,
            "optimizer_base_url": opt_base,
            "observer_model": obs_model,
            "observer_base_url": obs_base,
            "analyst_model": ana_model,
            "analyst_base_url": ana_base,
            "started": datetime.now().isoformat(),
            "test_suite": test_file,
            "n_runs_default": resolved_n_runs,
            "explore_constant": explore_constant,
            "holdout_ids": sorted(holdout_ids) if holdout_ids else [],
        },
        "iterations": [],
        "tree": [],
    }

    tsv_path = os.path.splitext(output_file)[0] + ".tsv"
    case_ids = [c["id"] for c in cases]

    # Diversify prompts — generate paraphrased variants before the loop
    # Auto-detect: if any case has cached user_prompts, use them even without --diversify
    prompt_variants: dict[str, list[str]] | None = None
    if diversify == 0:
        cached_variants = {
            c["id"]: c["user_prompts"]
            for c in cases
            if isinstance(c.get("user_prompts"), list) and len(c["user_prompts"]) > 1
        }
        if cached_variants:
            prompt_variants = cached_variants
            total_v = sum(len(v) for v in cached_variants.values())
            print(
                f"\n  Using cached variants for {len(cached_variants)} cases"
                f" ({total_v} total prompts)"
            )

    if diversify > 0:
        print(f"\nGenerating {diversify} prompt variants per case...")
        prompt_variants = _diversify_prompts(
            client=div_client,
            model=div_model,
            cases=cases,
            n_variants=diversify,
            provider=div_provider,
        )
        results["meta"]["diversify"] = diversify
        results["meta"]["prompt_variants"] = prompt_variants

        # Save variants back to test suite JSON for caching
        if save_variants:
            updated = False
            for case in cases:
                cid = case["id"]
                variants = prompt_variants.get(cid, [])
                if len(variants) > 1 and case.get("user_prompts") != variants:
                    case["user_prompts"] = variants
                    updated = True
            if updated:
                suite["cases"] = cases
                with open(test_file, "w") as f:
                    json.dump(suite, f, indent=2)
                    f.write("\n")
                print(f"  Saved variants to {test_file}")

    cumulative_tokens = 0
    suite_t0 = time.monotonic()

    for iteration in range(max_iterations):
        if suite_timeout > 0:
            suite_elapsed = time.monotonic() - suite_t0
            if suite_elapsed >= suite_timeout:
                print(
                    f"\nSuite timeout ({suite_timeout}s) reached"
                    f" after {suite_elapsed:.0f}s. Stopping."
                )
                break

        # UCB select — pick the most promising node to evaluate/extend
        selected_id = _ucb_select(nodes, explore_constant)
        selected = nodes[selected_id]

        tree_info = f"node {selected_id}"
        if selected.visit_count > 0:
            tree_info += f", score={selected.score:.0%}, visits={selected.visit_count}"
        else:
            tree_info += ", unvisited"
        if len(nodes) > 1:
            tree_info += f", tree={len(nodes)} nodes"

        print(f"\n{'=' * 60}")
        print(f"Iteration {iteration} ({tree_info})")
        print(f"{'=' * 60}")

        iter_t0 = time.monotonic()
        iter_result = _run_iteration(
            client=client,
            model=model,
            system_prompt=selected.prompt,
            cases=cases,
            n_runs=resolved_n_runs,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
            verbose=verbose,
            test_timeout=test_timeout,
            fast_fail=fast_fail,
            parallel=parallel,
            base_url=base_url,
            api_key=api_key,
            prompt_variants=prompt_variants,
            tool_overrides=selected.tool_overrides or None,
        )
        iter_result["iteration"] = iteration
        iter_result["prompt"] = selected.prompt
        iter_result["prompt_diff"] = None
        iter_result["optimizer_system"] = current_optimizer_system
        iter_result["timestamp"] = datetime.now().isoformat()
        iter_result["tree_node_id"] = selected_id
        if selected.tool_overrides:
            iter_result["tool_overrides"] = selected.tool_overrides

        # Update node score (rolling mean)
        new_score = _compute_holdout_score(iter_result, holdout_ids)
        old_score = selected.score
        if selected.visit_count == 0:
            selected.score = new_score
        else:
            selected.score = (selected.score * selected.visit_count + new_score) / (
                selected.visit_count + 1
            )
        selected.visit_count += 1
        if selected.visit_count > 1:
            _log(
                f"  Node {selected_id} score: {old_score:.0%} → {selected.score:.0%}"
                f" (this eval: {new_score:.0%})",
                dim=True,
            )

        cumulative_tokens += iter_result.get("aggregate", {}).get("total_tokens", 0)

        results["iterations"].append(iter_result)

        # Serialize tree state
        results["tree"] = [_node_to_dict(n) for n in nodes.values()]
        with open(output_file, "w") as f:
            json.dump(results, f, indent=2)

        _print_summary_table(iter_result)
        iter_wall = time.monotonic() - iter_t0
        _append_summary_tsv(
            tsv_path,
            iter_result,
            case_ids,
            cumulative_tokens=cumulative_tokens,
            node_score=selected.score,
            wall_time=iter_wall,
        )

        # Check if all passing
        agg = iter_result["aggregate"]
        if agg["overall_pass_rate"] == 1.0:
            print("\nAll test cases passing! Stopping.")
            break

        # Propose new prompt (skip on last iteration)
        if iteration < max_iterations - 1:
            # Observer: update optimizer strategy every 3 iterations
            if iteration >= 2 and iteration % 3 == 2:
                _log("  Observer updating optimizer prompt...", dim=True)
                try:
                    new_opt = _observe_and_update_optimizer(
                        obs_client,
                        obs_model,
                        current_optimizer_system,
                        results["iterations"],
                        provider=obs_provider,
                    )
                    if new_opt != current_optimizer_system:
                        opt_diff = _simple_diff(current_optimizer_system, new_opt)
                        _log(f"  Observer diff:\n{opt_diff}", dim=True)
                        current_optimizer_system = new_opt
                    else:
                        _log("  Observer: no changes", dim=True)
                except Exception as e:
                    _log(f"  Observer error: {e}", dim=True)

            # Build parent scores for improvement-based feedback (delta signal).
            # Use the parent node's last evaluation, not the current node's.
            parent_scores: dict[str, float] | None = None
            if selected.parent_id is not None:
                for prev_iter in reversed(results["iterations"][:-1]):
                    if prev_iter.get("tree_node_id") == selected.parent_id:
                        parent_scores = {
                            cid: cr["pass_rate"] for cid, cr in prev_iter["cases"].items()
                        }
                        break

            # Filter out holdout cases from optimizer feedback
            opt_cases, opt_result = _filter_for_optimizer(
                cases,
                iter_result,
                holdout_ids,
            )

            # Phase 1: Analyst diagnoses semantic patterns
            analyst_output = ""
            if opt_result["aggregate"].get("overall_pass_rate", 0) < 1.0:
                print("\nAnalyzing failures...")
                try:
                    analyst_output = _run_analyst(
                        client=ana_client,
                        model=ana_model,
                        test_cases=opt_cases,
                        iteration_result=opt_result,
                        provider=ana_provider,
                        optimize_tools=optimize_tools,
                        tool_overrides=selected.tool_overrides or None,
                    )
                    if analyst_output:
                        _log(f"  Analyst:\n{analyst_output}", dim=True)
                except Exception as e:
                    _log(f"  Analyst failed: {e}", dim=True)

            # Store analyst output before optimizer (survives optimizer failure)
            if analyst_output:
                iter_result["analyst"] = analyst_output

            # Phase 2: Tool description optimizer
            new_tool_overrides = selected.tool_overrides
            if optimize_tools:
                print("\nOptimizing tool descriptions...")
                try:
                    new_tool_overrides = _propose_tool_overrides(
                        client=tool_opt_client,
                        model=tool_opt_model,
                        current_tools=TOOLS,
                        current_overrides=selected.tool_overrides,
                        test_cases=opt_cases,
                        iteration_result=opt_result,
                        analyst_output=analyst_output,
                        provider=tool_opt_provider,
                    )
                    if new_tool_overrides != selected.tool_overrides:
                        added_tools = set(new_tool_overrides) - set(selected.tool_overrides)
                        modified_tools = set(new_tool_overrides) & set(selected.tool_overrides)
                        parts = []
                        if added_tools:
                            parts.append(f"added: {', '.join(sorted(added_tools))}")
                        if modified_tools:
                            parts.append(f"updated: {', '.join(sorted(modified_tools))}")
                        _log(f"  Tool overrides: {'; '.join(parts)}", dim=True)

                        # Show description diffs for each changed tool
                        base_index = {t["function"]["name"]: t for t in TOOLS}
                        prev_overrides = selected.tool_overrides or {}
                        for tname in sorted(set(new_tool_overrides) | set(prev_overrides)):
                            base_desc = (
                                base_index.get(tname, {}).get("function", {}).get("description", "")
                            )
                            old_desc = prev_overrides.get(tname, {}).get("description", base_desc)
                            new_desc = new_tool_overrides.get(tname, {}).get(
                                "description", base_desc
                            )
                            if old_desc != new_desc:
                                print(f"\n  {BOLD}{tname}{RESET}:")
                                print(f"    {DIM}old: {old_desc}{RESET}")
                                print(f"    new: {new_desc}")

                        iter_result["tool_overrides"] = new_tool_overrides
                except Exception as e:
                    _log(f"  Tool optimization failed: {e}", dim=True)
                    if optimize_tools:
                        continue

            # Phase 3: Prompt optimizer (skipped in --optimize-tools mode)
            new_prompt = selected.prompt
            if not optimize_tools:
                print("\nOptimizing prompt...")
                try:
                    new_prompt = _propose_prompt_modification(
                        client=opt_client,
                        model=opt_model,
                        current_prompt=selected.prompt,
                        test_cases=opt_cases,
                        iteration_result=opt_result,
                        history=results["iterations"],
                        optimizer_system=current_optimizer_system,
                        provider=opt_provider,
                        parent_scores=parent_scores,
                        analyst_output=analyst_output,
                        tool_overrides=new_tool_overrides or None,
                    )
                except Exception as e:
                    _log(f"  Prompt modification failed: {e}", dim=True)
                    continue

            prompt_changed = new_prompt != selected.prompt
            tools_changed = new_tool_overrides != selected.tool_overrides

            if prompt_changed:
                diff = _simple_diff(selected.prompt, new_prompt)
                print(f"Prompt modified ({len(selected.prompt)} -> {len(new_prompt)} chars)")
                if diff:
                    print(diff)
                iter_result["prompt_diff"] = diff

            if prompt_changed or tools_changed:
                # Add child node to evolution tree
                child = EvolutionNode(
                    node_id=next_node_id,
                    parent_id=selected_id,
                    prompt=new_prompt,
                    tool_overrides=new_tool_overrides,
                    iteration=iteration,
                    optimizer_system=current_optimizer_system,
                )
                nodes[next_node_id] = child
                selected.children.append(next_node_id)
                iter_result["tree_child_id"] = next_node_id
                _log(
                    f"  Created node {next_node_id} (child of {selected_id},"
                    f" tree={len(nodes)} nodes)",
                    dim=True,
                )
                next_node_id += 1

                # Re-write with tree update and diff
                results["tree"] = [_node_to_dict(n) for n in nodes.values()]
                with open(output_file, "w") as f:
                    json.dump(results, f, indent=2)
            else:
                print("No changes to prompt or tool descriptions. Stopping.")
                break

    # Report best node
    best_node = max(
        (n for n in nodes.values() if n.visit_count > 0),
        key=lambda n: n.score,
        default=nodes[0],
    )
    print(
        f"\nBest node: {best_node.node_id} "
        f"(score={best_node.score:.2%}, visits={best_node.visit_count})"
    )
    if best_node.tool_overrides:
        print(f"  Tool overrides: {', '.join(sorted(best_node.tool_overrides))}")

    # Save optimized tool descriptions back to JSON files
    if save_tools and best_node.tool_overrides:
        from turnstone.core.tools import _TOOLS_DIR

        for tool_name, override in best_node.tool_overrides.items():
            tool_path = _TOOLS_DIR / f"{tool_name}.json"
            if not tool_path.exists():
                continue
            with open(tool_path) as f:
                tool_json = json.load(f)
            if "description" in override:
                tool_json["description"] = override["description"]
            for param, desc in override.get("parameters", {}).items():
                if param in tool_json.get("parameters", {}).get("properties", {}):
                    tool_json["parameters"]["properties"][param]["description"] = desc
            with open(tool_path, "w") as f:
                json.dump(tool_json, f, indent=2)
                f.write("\n")
            print(f"  Saved: {tool_path}")

    print(f"Results written to {output_file}")
    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prompt optimization and evaluation for turnstone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Run evaluation with default prompt
              turnstone-eval tests.json

              # Run with custom initial prompt from file
              turnstone-eval tests.json --prompt prompt.txt

              # Single evaluation pass (no optimization)
              turnstone-eval tests.json --no-optimize

              # Configure runs and iterations
              turnstone-eval tests.json --n-runs 5 --max-iter 10
        """),
    )
    parser.add_argument(
        "test_file",
        help="Path to test cases JSON file",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000/v1",
        help="API base URL (default: http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (default: auto-detect)",
    )
    parser.add_argument(
        "--optimizer-model",
        default=None,
        help="Model for prompt optimization (default: same as --model)",
    )
    parser.add_argument(
        "--optimizer-base-url",
        default=None,
        help="Base URL for optimizer model (default: same as --base-url)",
    )
    parser.add_argument(
        "--observer-model",
        default=None,
        help="Model for meta-optimization (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--observer-base-url",
        default=None,
        help="Base URL for observer model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--analyst-model",
        default=None,
        help="Model for failure analysis (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--analyst-base-url",
        default=None,
        help="Base URL for analyst model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--diversifier-model",
        default=None,
        help="Model for prompt diversification (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--diversifier-base-url",
        default=None,
        help="Base URL for diversifier model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--diversify",
        type=int,
        default=0,
        help="Generate N prompt variants per test case (0=disabled, includes original)",
    )
    parser.add_argument(
        "--save-variants",
        action="store_true",
        help="Save generated variants back to test suite JSON for caching",
    )
    parser.add_argument(
        "--optimize-tools",
        action="store_true",
        help="Optimize tool descriptions only (freeze system prompt). "
        "Useful after system prompt optimization plateaus",
    )
    parser.add_argument(
        "--tool-optimizer-model",
        default=None,
        help="Model for tool description optimization (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--tool-optimizer-base-url",
        default=None,
        help="Base URL for tool optimizer model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--save-tools",
        action="store_true",
        help="Write optimized tool descriptions back to turnstone/tools/*.json",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Path to initial prompt text file (default: use turnstone's built-in)",
    )
    parser.add_argument(
        "--n-runs",
        type=int,
        default=None,
        help="Number of runs per test case (default: from tests.json or 3)",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=5,
        help="Maximum optimization iterations (default: 5)",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Run evaluation only, no prompt optimization",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature (default: 0.7)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32768,
        help="Max completion tokens (default: 32768)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["low", "medium", "high"],
        help="Reasoning effort (default: medium)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=131072,
        help="Context window size (default: 131072)",
    )
    parser.add_argument(
        "--output",
        default="eval_results.json",
        help="Output results file (default: eval_results.json)",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=300,
        help="Per-test timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--suite-timeout",
        type=int,
        default=0,
        help="Total suite timeout in seconds (default: unlimited)",
    )
    parser.add_argument(
        "--no-fast-fail",
        action="store_true",
        help="Disable early termination when all initial runs score 0.0",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Parallel workers (default: 1=serial, 0=auto)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed per-turn logging (API calls, tool args, results)",
    )
    parser.add_argument(
        "--explore-constant",
        type=float,
        default=1.414,
        help="UCB exploration constant C (default: sqrt(2) ≈ 1.414)",
    )
    from turnstone.core.config import add_config_arg, apply_config

    add_config_arg(parser)
    apply_config(parser, ["api", "model"])
    args = parser.parse_args()

    # Load initial prompt if provided
    initial_prompt = None
    if args.prompt:
        with open(args.prompt) as f:
            initial_prompt = f.read()

    max_iter = 1 if args.no_optimize else args.max_iter

    run_optimization(
        base_url=args.base_url,
        model=args.model,
        test_file=args.test_file,
        initial_prompt=initial_prompt,
        n_runs=args.n_runs,
        max_iterations=max_iter,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        output_file=args.output,
        context_window=args.context_window,
        verbose=args.verbose,
        test_timeout=args.test_timeout,
        suite_timeout=args.suite_timeout,
        fast_fail=not args.no_fast_fail,
        parallel=args.parallel if args.parallel != 0 else (os.cpu_count() or 4),
        optimizer_base_url=args.optimizer_base_url,
        optimizer_model=args.optimizer_model,
        observer_base_url=args.observer_base_url,
        observer_model=args.observer_model,
        analyst_base_url=args.analyst_base_url,
        analyst_model=args.analyst_model,
        diversifier_base_url=args.diversifier_base_url,
        diversifier_model=args.diversifier_model,
        diversify=args.diversify,
        save_variants=args.save_variants,
        optimize_tools=args.optimize_tools,
        tool_optimizer_base_url=args.tool_optimizer_base_url,
        tool_optimizer_model=args.tool_optimizer_model,
        save_tools=args.save_tools,
        explore_constant=args.explore_constant,
    )


if __name__ == "__main__":
    main()
