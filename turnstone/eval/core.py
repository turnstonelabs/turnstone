"""Measurement substrate for turnstone eval.

This is the measurement half of the eval/optimizer split: it runs test cases
against a model, scores the resulting tool-call sequences against expected
actions, and reports the aggregate. It has no notion of prompt optimization.

API contract:
    ``_run_iteration(client, model, system_prompt, cases, n_runs, ...) -> dict``
    is THE boundary between measurement and optimization. Core owns everything
    up to and including ``_run_iteration``. The optimizer (``turnstone.optimizer``)
    loops over it; the measure-only CLI (``turnstone.eval.cli``) calls it once.

Dependency direction is strictly one-way: the optimizer imports from this
module; this module never imports from the optimizer.
"""

import copy
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from openai import OpenAI

from turnstone.core.providers import LLMProvider, create_client, create_provider
from turnstone.core.session import ChatSession
from turnstone.core.storage import get_storage, init_storage, reset_storage
from turnstone.core.tools import INTERACTIVE_TOOLS, PRIMARY_KEY_MAP
from turnstone.core.trajectory import Role, turn_from_dict

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


class NullUI:
    """UI adapter that discards all output. Used by HeadlessSession."""

    def on_turn_start(self) -> None:
        pass

    def on_turn_committed(self) -> None:
        pass

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
        preview: dict[str, Any] | None = None,
    ) -> None:
        pass

    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None:
        pass

    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None:
        pass

    def on_info(self, message: str) -> None:
        pass

    def on_error(self, message: str) -> None:
        pass

    def on_system_turn(self, content: str, source: str, meta: dict[str, Any] | None = None) -> None:
        pass

    def on_state_change(self, state: str) -> None:
        pass

    def on_rename(self, name: str) -> None:
        pass

    def on_intent_verdict(self, verdict: dict[str, Any], judge_event: object | None = None) -> None:
        pass

    def on_output_warning(self, call_id: str, assessment: dict[str, Any]) -> None:
        pass

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


# ─── Tool overrides ───────────────────────────────────────────────────────────


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
        self.messages.append(turn_from_dict({"role": "user", "content": user_input}))
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

            self.messages.append(turn_from_dict(assistant_msg))
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
                self.messages.append(turn_from_dict(tool_msg))
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
    skill: dict[str, Any] | None = None,
    skill_mode: bool = False,
) -> dict[str, Any]:
    """Run a single test case once in an isolated temp directory.

    Uses os.chdir (process-global), so concurrent calls must run in
    separate processes (see _run_and_score_subprocess / --parallel).

    When ``skill_mode`` is True the session is built WITHOUT a system-prompt
    override so the model runs under turnstone's natural prompt composition
    (the base identity under test).  If ``skill`` is given it is seeded into
    the temp DB and activated via the real ``set_skill`` path — the skill
    flows through turnstone's natural composition exactly as in production,
    landing wherever THAT checkout places a named skill (the system message,
    or a separate context turn).  This harness measures adherence regardless
    of placement, which is the whole point of comparing across checkouts.
    ``skill`` None is the control arm (natural default, no skill).  When
    ``skill_mode`` is False behaviour is unchanged — the system prompt is
    overridden as before.

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

        # Skill-adherence treatment arm: seed the named skill into the temp DB
        # (once — before the retry loop) so set_skill can activate it through
        # the real composition path.  The subprocess/serial DB is fresh per
        # run, so template_id "eval-skill" never collides.
        if skill_mode and skill is not None:
            get_storage().create_prompt_template(
                template_id="eval-skill",
                name=skill["name"],
                category="eval",
                content=skill["content"],
                variables="[]",
                is_default=False,
                org_id="",
                created_by="eval",
                activation="named",
                enabled=True,
            )

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
                # skill_mode uses turnstone's natural composition (no override)
                # so the skill folds in wherever the checkout under test places
                # a named skill (system message, or a separate context turn).
                system_prompt_override=None if skill_mode else system_prompt,
                instructions=None,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_timeout=30,
                reasoning_effort=reasoning_effort,
                context_window=context_window,
                tool_truncation=2000,
                tool_overrides=tool_overrides,
            )
            if skill_mode and skill is not None:
                # Activate the seeded skill via the production path:
                # _load_skills() -> _init_system_messages() composes the
                # skill body into session.system_messages.
                session.set_skill(skill["name"])
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
                    if msg.role is Role.ASSISTANT and msg.text:
                        final_content = msg.text
                        break
                message_count = len(session.messages)
                total_usage = session.total_usage
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
            skill=params.get("skill"),
            skill_mode=params.get("skill_mode", False),
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
    skill: dict[str, Any] | None = None,
    skill_mode: bool = False,
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
                    "skill": skill,
                    "skill_mode": skill_mode,
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
    skill: dict[str, Any] | None = None,
    skill_mode: bool = False,
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
            skill=skill,
            skill_mode=skill_mode,
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
                    skill=skill,
                    skill_mode=skill_mode,
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


# ─── Skill-adherence driver ──────────────────────────────────────────────────


def run_skill_adherence(
    client: Any,
    base_url: str,
    api_key: str,
    model: str,
    cases: list[dict[str, Any]],
    n_runs: int,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str,
    context_window: int,
    test_timeout: int = 300,
    parallel: int = 1,
    verbose: bool = False,
) -> dict[str, Any]:
    """Measure how much a named skill changes tool-use behaviour.

    For every case that carries a ``skill`` this runs two arms ``n_runs``
    times each, scoring both against the case's ``expected_actions``:

    * **treatment** — the skill is applied via the real ``set_skill``
      composition path (``skill_mode=True, skill=<case skill>``); where its
      body lands (system message or a context turn) depends on the checkout;
    * **control** — the same base identity with no skill
      (``skill_mode=True, skill=None``).

    The adherence lift is ``pass_rate(treatment) - pass_rate(control)``.  The
    control isolates the skill's causal effect: a scenario the model passes
    anyway yields ~0 lift and is uninformative — that near-zero IS the signal.

    Returns ``{"cases": [{case_id, skill, treatment_rate, control_rate, lift,
    n_runs}, ...], "mean_lift": float}``.
    """
    case_results: list[dict[str, Any]] = []
    skill_cases = [c for c in cases if c.get("skill")]

    # Validate skill shape up front so a malformed dataset fails with a clear
    # message instead of a KeyError mid-run (after arms have already started).
    for case in skill_cases:
        s = case["skill"]
        if not isinstance(s, dict) or not s.get("name") or not s.get("content"):
            raise ValueError(
                f"case {case.get('id', '?')!r}: 'skill' must be an object with "
                "non-empty 'name' and 'content'"
            )

    for ci, case in enumerate(skill_cases):
        skill = case["skill"]
        # Drop the skill key from the case handed to the runner — it is
        # supplied out-of-band per arm, not read from the case dict.
        arm_case = {k: v for k, v in case.items() if k != "skill"}
        print(
            f"\n  {CYAN}[{ci + 1}/{len(skill_cases)}]{RESET} "
            f"{BOLD}{case['id']}{RESET} — skill {DIM}'{skill['name']}'{RESET}"
        )

        arm_rates: dict[str, float] = {}
        for arm, arm_skill in (("treatment", skill), ("control", None)):
            print(f"    {DIM}{arm}{RESET}")
            iter_result = _run_iteration(
                client=client,
                model=model,
                system_prompt="",
                cases=[arm_case],
                n_runs=n_runs,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                context_window=context_window,
                verbose=verbose,
                test_timeout=test_timeout,
                # Never fast-fail: the treatment arm's pass rate must be
                # counted over every run, and control runs are expected to
                # fail — skipping them would corrupt the lift.
                fast_fail=False,
                parallel=parallel,
                base_url=base_url,
                api_key=api_key,
                skill=arm_skill,
                skill_mode=True,
            )
            arm_rates[arm] = iter_result["aggregate"]["overall_pass_rate"]

        treatment_rate = arm_rates["treatment"]
        control_rate = arm_rates["control"]
        case_results.append(
            {
                "case_id": case["id"],
                "skill": skill["name"],
                "treatment_rate": treatment_rate,
                "control_rate": control_rate,
                "lift": treatment_rate - control_rate,
                "n_runs": n_runs,
            }
        )

    mean_lift = sum(c["lift"] for c in case_results) / len(case_results) if case_results else 0.0
    return {"cases": case_results, "mean_lift": mean_lift}


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


def _print_skill_adherence_table(result: dict[str, Any]) -> None:
    """Print a per-case treatment/control/lift table plus the mean lift."""
    rows = result.get("cases", [])
    if not rows:
        print("\n  No skill-bearing cases to measure.")
        return

    max_id = max(len(str(r["case_id"])) for r in rows)
    max_id = max(max_id, 4)  # min "CASE" header

    print(f"\n{BOLD}  {'CASE'.ljust(max_id)}  {'TREAT':>6}  {'CTRL':>6}  {'LIFT':>7}{RESET}")
    print(f"  {'─' * (max_id + 25)}")

    for r in rows:
        lift = r["lift"]
        color = GREEN if lift > 0.01 else (RED if lift < -0.01 else DIM)
        print(
            f"  {str(r['case_id']).ljust(max_id)}  "
            f"{r['treatment_rate']:>6.2f}  {r['control_rate']:>6.2f}  "
            f"{color}{lift:>+7.2f}{RESET}"
        )

    print(f"  {'─' * (max_id + 25)}")
    mean = result.get("mean_lift", 0.0)
    mcolor = GREEN if mean > 0.01 else (RED if mean < -0.01 else DIM)
    print(
        f"  {BOLD}{'MEAN'.ljust(max_id)}{RESET}  {'':>6}  {'':>6}  "
        f"{mcolor}{BOLD}{mean:>+7.2f}{RESET}"
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
