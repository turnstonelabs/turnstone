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
import contextlib
import difflib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from openai import OpenAI

import turnstone.core.memory as _memory_module
from turnstone.core.providers import LLMProvider, create_provider
from turnstone.core.session import ChatSession
from turnstone.core.tools import PRIMARY_KEY_MAP, TOOLS

# ─── ANSI & logging helpers ───────────────────────────────────────────────────

DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"


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

    def on_tool_result(self, call_id: str, name: str, output: str) -> None:
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

    def on_state_change(self, state: str) -> None:
        pass

    def on_rename(self, name: str) -> None:
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


# ─── Stdout suppression ──────────────────────────────────────────────────────


@contextlib.contextmanager
def _suppress_stdout() -> Iterator[None]:
    """Redirect stdout to devnull temporarily."""
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = old


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
        if system_prompt_override is not None:
            self._override_system_prompt(system_prompt_override)

    def _override_system_prompt(self, content: str) -> None:
        """Replace the developer message content with a custom prompt."""
        for i, msg in enumerate(self.system_messages):
            if msg["role"] == "developer":
                self.system_messages[i] = {"role": "developer", "content": content}
                return
        self.system_messages.append({"role": "developer", "content": content})

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
            if verbose:
                _log(f"{log_prefix}  turn {turn}: calling API...", dim=True)

            t0 = time.monotonic()
            msgs = self._full_messages()

            result = self._provider.create_completion(
                client=self.client,
                model=self.model,
                messages=msgs,
                tools=TOOLS,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                reasoning_effort=self.reasoning_effort,
                extra_params=self._provider_extra_params(),
            )
            elapsed = time.monotonic() - t0

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

            # Execute tools with stdout suppressed
            with _suppress_stdout():
                results, _ = self._execute_tools(assistant_msg["tool_calls"])

            for tc, (tc_id, output) in zip(assistant_msg["tool_calls"], results, strict=False):
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
                    "content": output,
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
) -> dict[str, Any]:
    """Run a single test case once in an isolated temp directory.

    Must be called serially — uses os.chdir which is process-global.

    Returns dict with keys: tool_log, final_content, message_count, elapsed.
    """
    workdir = tempfile.mkdtemp(prefix="turnstone_eval_")
    original_cwd = os.getcwd()
    eval_db = os.path.join(workdir, ".turnstone_eval.db")
    _memory_module.db_override = eval_db
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

        session = HeadlessSession(
            client=client,
            model=model,
            system_prompt_override=system_prompt,
            instructions=None,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_timeout=30,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
        )

        max_turns = case.get("max_turns", 10)
        # Retry on transient API errors to avoid poisoning eval scores
        tool_log: list[dict[str, Any]] = []
        _last_err: Exception | None = None
        for _attempt in range(3):
            try:
                tool_log = session.send_headless(
                    case["user_prompt"],
                    max_turns=max_turns,
                    verbose=verbose,
                    log_prefix=log_prefix,
                )
                break
            except Exception as _e:
                _last_err = _e
                if _attempt < 2:
                    import time as _time

                    _time.sleep(2**_attempt)
        else:
            raise _last_err or RuntimeError("send_headless failed after 3 attempts")

        final_content = ""
        for msg in reversed(session.messages):
            if msg["role"] == "assistant" and msg.get("content"):
                final_content = msg["content"]
                break

        elapsed = time.monotonic() - t0
        return {
            "tool_log": tool_log,
            "final_content": final_content,
            "message_count": len(session.messages),
            "elapsed": round(elapsed, 1),
        }
    finally:
        _memory_module.db_override = None
        _memory_module.db_initialized.discard(eval_db)
        os.chdir(original_cwd)
        shutil.rmtree(workdir, ignore_errors=True)


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
) -> dict[str, Any]:
    """Run all test cases n_runs times and score them."""
    case_results: dict[str, Any] = {}

    for ci, case in enumerate(cases):
        case_id = case["id"]
        case_n = case.get("n_runs", n_runs)
        runs: list[dict[str, Any]] = []

        print(f"\n  {CYAN}[{ci + 1}/{len(cases)}]{RESET} {BOLD}{case_id}{RESET} ({case_n} runs)")
        if verbose:
            _log(f"    prompt: {case['user_prompt']}", dim=True)

        for run_idx in range(case_n):
            log_prefix = f"      [{run_idx + 1}/{case_n}]"

            try:
                run_result = _run_single_test(
                    client=client,
                    model=model,
                    system_prompt=system_prompt,
                    case=case,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    context_window=context_window,
                    verbose=verbose,
                    log_prefix=log_prefix,
                )

                score_result = score_run(
                    tool_log=run_result["tool_log"],
                    expected_actions=case.get("expected_actions", []),
                    match_mode=case.get("match_mode", "ordered_subset"),
                )

                score_result["tool_sequence"] = [t["tool"] for t in run_result["tool_log"]]
                score_result["tool_args"] = [{t["tool"]: t["args"]} for t in run_result["tool_log"]]
                score_result["elapsed"] = run_result.get("elapsed", 0)

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
                    "elapsed": 0,
                }

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

        pass_count = sum(1 for r in runs if r["pass"])
        case_results[case_id] = {
            "runs": runs,
            "pass_rate": pass_count / len(runs) if runs else 0,
            "avg_score": (sum(r["score"] for r in runs) / len(runs) if runs else 0),
        }

    # Aggregate
    total_runs = sum(len(cr["runs"]) for cr in case_results.values())
    total_passes = sum(sum(1 for r in cr["runs"] if r["pass"]) for cr in case_results.values())
    total_json_dumps = sum(
        sum(1 for r in cr["runs"] if r.get("json_dump")) for cr in case_results.values()
    )

    return {
        "cases": case_results,
        "aggregate": {
            "total_cases": len(cases),
            "total_runs": total_runs,
            "overall_pass_rate": total_passes / total_runs if total_runs else 0,
            "json_dumps": total_json_dumps,
            "overall_avg_score": (
                sum(cr["avg_score"] for cr in case_results.values()) / len(case_results)
                if case_results
                else 0
            ),
            "per_case_pass_rates": {cid: cr["pass_rate"] for cid, cr in case_results.items()},
        },
    }


# ─── Prompt optimizer ────────────────────────────────────────────────────────


OPTIMIZER_SYSTEM = """\
You are a text rewriter. You receive a developer prompt (instructions \
for a coding assistant on how to use its tools) and test results \
showing how well the assistant followed them. Rewrite the prompt so \
the assistant picks the right tools more often.

Context: Tests score whether the assistant calls specific tools in \
the right order. The critical failure modes to address: \
(1) responding with only text when a tool call is needed — the \
assistant must ALWAYS call a tool, (2) using write_file to rewrite \
an entire file instead of edit_file for small changes, (3) not calling \
plan(prompt='...') when asked to think through a complex task, \
(4) searching for a file before creating it with write_file. When a \
test shows 100%, preserve whatever phrasing drove that behavior.

Style: direct imperative instructions organized with newlines. Short \
sentences. Concrete tool call examples like bash(command='git log -5').

Length: no longer than 130% of the original prompt's length.

Output ONLY the rewritten prompt. No commentary, no fences.\
"""


OBSERVER_SYSTEM = """\
You edit the rewriter's instructions shown below. The rewriter takes a \
paragraph and test results, then rewrites the paragraph to score higher. \
Your job: tune the rewriter's instructions so it does a better job.

Your output replaces the rewriter's instructions. It must stay at the \
same level — telling the rewriter HOW to rewrite, not doing the \
rewriting yourself.

Example of the right level (abbreviated):
\"\"\"
You are a text rewriter. You receive a paragraph and test results...
Style: flowing prose, no bullet points...
Length: aim for 600-1200 chars...
Be bold — reword, restructure...
\"\"\"

Make 2-3 targeted edits based on the iteration history. Remove guidance \
that isn't working. Stay under 150% of the input length.

Output ONLY the modified rewriter instructions.\
"""


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
        notes: list[str] = []
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
        f"## Rewriter Instructions (edit these)\n"
        f"```\n{optimizer_system}\n```\n\n"
        f"## What the Rewriter Produced (do NOT mimic this)\n"
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
        max_tokens=2048,
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


def _propose_prompt_modification(
    client: Any,
    model: str,
    current_prompt: str,
    test_cases: list[dict[str, Any]],
    iteration_result: dict[str, Any],
    history: list[dict[str, Any]],
    optimizer_system: str = OPTIMIZER_SYSTEM,
    provider: LLMProvider | None = None,
) -> str:
    """Use the model to propose a new prompt based on evaluation results."""
    # Build summary of results
    summary_parts: list[str] = []
    for case_id, case_result in iteration_result["cases"].items():
        case_def = next((c for c in test_cases if c["id"] == case_id), None)
        if not case_def:
            continue
        pr = case_result["pass_rate"]
        status = "PASS" if pr == 1.0 else "WEAK" if pr >= 0.5 else "FAIL"
        summary_parts.append(
            f"[{status}] {case_id} (pass_rate={case_result['pass_rate']:.0%})\n"
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

    user_content = (
        f"## Current Prompt\n```\n{current_prompt}\n```\n\n"
        f"## Test Results (iteration {iteration_result.get('iteration', '?')})\n"
        + "\n\n".join(summary_parts)
        + f"\n\n## Score History\n{history_text}"
        + "\n\nPropose an improved prompt. Output ONLY the new prompt text."
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
) -> dict[str, Any]:
    """Main optimization loop."""
    client = OpenAI(
        base_url=base_url,
        api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
    )

    eval_provider = create_provider("openai")

    if not model:
        from turnstone.core.model_registry import detect_model

        model, _ = detect_model(client)

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
        initial_prompt = next(m["content"] for m in tmp.system_messages if m["role"] == "developer")
        # Strip memory reminder — it's a runtime artifact, not part of the prompt
        initial_prompt = re.sub(
            r"\n*REMINDER: You currently have \d+ memories stored\..*$",
            "",
            initial_prompt,
        ).strip()

    current_prompt = initial_prompt
    results: dict[str, Any] = {
        "meta": {
            "model": model,
            "base_url": base_url,
            "started": datetime.now().isoformat(),
            "test_suite": test_file,
            "n_runs_default": resolved_n_runs,
        },
        "iterations": [],
    }

    current_optimizer_system = OPTIMIZER_SYSTEM

    for iteration in range(max_iterations):
        print(f"\n{'=' * 60}")
        print(f"Iteration {iteration}")
        print(f"{'=' * 60}")

        iter_result = _run_iteration(
            client=client,
            model=model,
            system_prompt=current_prompt,
            cases=cases,
            n_runs=resolved_n_runs,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
            verbose=verbose,
        )
        iter_result["iteration"] = iteration
        iter_result["prompt"] = current_prompt
        iter_result["prompt_diff"] = None
        iter_result["optimizer_system"] = current_optimizer_system
        iter_result["timestamp"] = datetime.now().isoformat()

        results["iterations"].append(iter_result)

        # Write intermediate results
        with open(output_file, "w") as f:
            json.dump(results, f, indent=2)

        # Print summary
        agg = iter_result["aggregate"]
        json_dumps = agg.get("json_dumps", 0)
        jd_str = f"  {YELLOW}json_dumps={json_dumps}{RESET}" if json_dumps else ""
        print(f"\nOverall pass rate: {agg['overall_pass_rate']:.0%}{jd_str}")
        print(f"Overall avg score: {agg['overall_avg_score']:.2f}")
        for case_id, rate in agg["per_case_pass_rates"].items():
            status = "PASS" if rate == 1.0 else "FAIL"
            print(f"  [{status}] {case_id}: {rate:.0%}")

        # Check if all passing
        if agg["overall_pass_rate"] == 1.0:
            print("\nAll test cases passing! Stopping.")
            break

        # Propose new prompt (skip on last iteration)
        if iteration < max_iterations - 1:
            # Observer: update optimizer developer prompt every 3 iterations
            if iteration >= 2 and iteration % 3 == 2:
                _log("  Observer updating optimizer prompt...", dim=True)
                try:
                    new_opt = _observe_and_update_optimizer(
                        client,
                        model,
                        current_optimizer_system,
                        results["iterations"],
                        provider=eval_provider,
                    )
                    if new_opt != current_optimizer_system:
                        opt_diff = _simple_diff(current_optimizer_system, new_opt)
                        _log(f"  Observer diff:\n{opt_diff}", dim=True)
                        current_optimizer_system = new_opt
                        # Reset to the best-performing developer prompt so far.
                        best_iter = max(
                            results["iterations"],
                            key=lambda it: it["aggregate"]["overall_pass_rate"],
                        )
                        best_rate = best_iter["aggregate"]["overall_pass_rate"]
                        best_idx = best_iter["iteration"]
                        current_prompt = best_iter["prompt"]
                        _log(
                            f"  Observer changed strategy → reset developer prompt "
                            f"to best (iter {best_idx}, {best_rate:.0%})",
                            dim=True,
                        )
                    else:
                        _log("  Observer: no changes", dim=True)
                except Exception as e:
                    _log(f"  Observer error: {e}", dim=True)

            print("\nOptimizing prompt...")
            try:
                new_prompt = _propose_prompt_modification(
                    client=client,
                    model=model,
                    current_prompt=current_prompt,
                    test_cases=cases,
                    iteration_result=iter_result,
                    history=results["iterations"],
                    optimizer_system=current_optimizer_system,
                    provider=eval_provider,
                )
            except Exception as e:
                _log(f"  Prompt modification failed: {e}", dim=True)
                continue

            if new_prompt != current_prompt:
                diff = _simple_diff(current_prompt, new_prompt)
                print(f"Prompt modified ({len(current_prompt)} -> {len(new_prompt)} chars)")
                if diff:
                    print(diff)
                iter_result["prompt_diff"] = diff
                # Re-write with the diff included
                with open(output_file, "w") as f:
                    json.dump(results, f, indent=2)
                current_prompt = new_prompt
            else:
                print("Optimizer returned identical prompt. Stopping.")
                break

    print(f"\nResults written to {output_file}")
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
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed per-turn logging (API calls, tool args, results)",
    )
    from turnstone.core.config import apply_config

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
    )


if __name__ == "__main__":
    main()
