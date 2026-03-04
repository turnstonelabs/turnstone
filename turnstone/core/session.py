"""Core chat session — UI-agnostic engine for multi-turn LLM interaction.

The ChatSession class drives the conversation loop (send, stream, tool
execution) while delegating all user-facing I/O through the SessionUI
protocol.  Any frontend (terminal, web, test harness) implements SessionUI
to receive events and handle approval prompts.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from turnstone.core.config import get_tavily_key
from turnstone.core.edit import find_occurrences, pick_nearest
from turnstone.core.memory import (
    delete_memory,
    delete_session,
    get_session_name,
    list_sessions,
    load_memories,
    load_session_config,
    load_session_messages,
    normalize_key,
    register_session,
    resolve_session,
    save_memory,
    save_message,
    save_session_config,
    search_history,
    search_history_recent,
    search_memories,
    set_session_alias,
    update_session_title,
)
from turnstone.core.providers import create_provider
from turnstone.core.safety import is_command_blocked, sanitize_command
from turnstone.core.sandbox import execute_math_sandboxed
from turnstone.core.tools import (
    AGENT_AUTO_TOOLS,
    AGENT_TOOLS,
    PRIMARY_KEY_MAP,
    TASK_AGENT_TOOLS,
    TASK_AUTO_TOOLS,
    TOOLS,
    merge_mcp_tools,
)
from turnstone.core.web import check_ssrf, strip_html
from turnstone.ui.colors import DIM, GRAY, GREEN, RED, RESET, YELLOW, bold, cyan, dim

if TYPE_CHECKING:
    from collections.abc import Iterator

    from turnstone.core.healthcheck import BackendHealthMonitor
    from turnstone.core.mcp_client import MCPClientManager
    from turnstone.core.model_registry import ModelRegistry
    from turnstone.core.providers import CompletionResult, LLMProvider, StreamChunk

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
    def on_tool_result(self, call_id: str, name: str, output: str) -> None: ...
    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None: ...
    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None: ...
    def on_plan_review(self, content: str) -> str: ...
    def on_info(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_state_change(self, state: str) -> None: ...
    def on_rename(self, name: str) -> None: ...


# ---------------------------------------------------------------------------
# ChatSession — the core engine
# ---------------------------------------------------------------------------


class ChatSession:
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
        context_window: int = 131072,
        compact_max_tokens: int = 32768,
        auto_compact_pct: float = 0.8,
        agent_max_turns: int = -1,
        tool_truncation: int = 0,
        mcp_client: MCPClientManager | None = None,
        registry: ModelRegistry | None = None,
        model_alias: str | None = None,
        health_monitor: BackendHealthMonitor | None = None,
    ):
        self.client = client
        self.model = model
        self._registry = registry
        self._model_alias = model_alias
        self._health_monitor = health_monitor
        # Resolve provider for the current model
        self._provider: LLMProvider = (
            registry.get_provider(model_alias)
            if registry and model_alias
            else create_provider("openai")
        )
        self.ui = ui
        self.instructions = instructions
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.tool_timeout = tool_timeout
        self.reasoning_effort = reasoning_effort
        self.context_window = context_window
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
        self._session_id = uuid.uuid4().hex[:12]
        self._title_generated = False
        register_session(self._session_id)
        self._read_files: set[str] = set()
        self.messages: list[dict[str, Any]] = []
        self._last_usage: dict[str, int] | None = None
        self._msg_tokens: list[int] = []  # parallel to self.messages
        self._system_tokens = 0  # tokens for system_messages
        self._assistant_pending_tokens = 0
        self.creative_mode = False
        # MCP tool integration: merge external tools with built-in
        self._mcp_client = mcp_client
        if mcp_client:
            mcp_tools = mcp_client.get_tools()
            self._tools = merge_mcp_tools(TOOLS, mcp_tools)
            self._task_tools = merge_mcp_tools(TASK_AGENT_TOOLS, mcp_tools)
            self._agent_tools = merge_mcp_tools(AGENT_TOOLS, mcp_tools)
        else:
            self._tools = TOOLS
            self._task_tools = TASK_AGENT_TOOLS
            self._agent_tools = AGENT_TOOLS
        self._init_system_messages()
        self._save_config()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def model_alias(self) -> str | None:
        return self._model_alias

    def _save_config(self) -> None:
        """Persist LLM-affecting config so resumed sessions behave identically."""
        save_session_config(
            self._session_id,
            {
                "temperature": str(self.temperature),
                "reasoning_effort": self.reasoning_effort,
                "max_tokens": str(self.max_tokens),
                "instructions": self.instructions or "",
                "creative_mode": str(self.creative_mode),
            },
        )

    def _truncate_output(self, output: str) -> str:
        """Truncate tool output to self.tool_truncation chars, keeping head + tail."""
        limit = self.tool_truncation
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

    def _generate_title(self) -> None:
        """Generate a short title for this session via a background LLM call."""
        try:
            # Gather first user message and first assistant reply
            user_msg = ""
            asst_msg = ""
            for m in self.messages:
                if m["role"] == "user" and not user_msg:
                    user_msg = (m.get("content") or "")[:300]
                elif m["role"] == "assistant" and not asst_msg:
                    asst_msg = (m.get("content") or "")[:200]
                if user_msg and asst_msg:
                    break
            if not user_msg:
                return
            snippet = f"Generate a title for this conversation:\n\nUser: {user_msg}"
            if asst_msg:
                snippet += f"\nAssistant: {asst_msg}"
            snippet += "\n\nTitle:"
            result = self._provider.create_completion(
                client=self.client,
                model=self.model,
                messages=[
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
                temperature=0.3,
                reasoning_effort="low",
                extra_params=self._provider_extra_params(reasoning_effort="low"),
            )
            raw = (result.content or "").strip()
            # Take first line, strip quotes
            title = raw.split("\n")[0].strip().strip('"').strip("'")
            if title:
                update_session_title(self._session_id, title[:80])
        except Exception:
            pass  # Title generation is non-critical

    def resume_session(self, session_id: str) -> bool:
        """Load messages from a previous session and resume it.

        Replaces the current conversation with the loaded messages,
        adopting the old session_id so new messages continue in the same
        session.  Restores persisted config (temperature, reasoning_effort,
        etc.) so the resumed session behaves identically to the original.
        Returns True on success.
        """
        messages = load_session_messages(session_id)
        if not messages:
            return False
        self._session_id = session_id
        self.messages = messages
        self._read_files.clear()
        self._last_usage = None
        self._title_generated = True  # don't re-title resumed sessions
        self._msg_tokens = [
            max(1, int(self._msg_char_count(m) / self._chars_per_token)) for m in self.messages
        ]
        # Restore persisted config
        config = load_session_config(session_id)
        if config:
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
            self._init_system_messages()
        return True

    def _init_system_messages(self) -> None:
        """Build the system/developer prefix messages.

        Developer message contains tool patterns (or creative writing
        instructions when creative_mode is on), plus any user-supplied
        instructions and memory reminders.
        """
        self.system_messages: list[dict[str, Any]] = []

        # -- Chat template kwargs --
        self._chat_template_kwargs_base: dict[str, Any] = {
            "reasoning_effort": self.reasoning_effort,
        }
        self._chat_template_kwargs: dict[str, Any] = dict(self._chat_template_kwargs_base)

        # -- Developer message --
        if self.creative_mode:
            dev_parts = [
                "# Instructions",
                "",
                "You are a creative writing partner. Use the analysis channel to "
                "think through structure, voice, and intent before drafting.",
                "",
                "Craft principles:",
                "- Ground scenes in concrete sensory detail — what is seen, heard, felt.",
                "- Vary rhythm. Short sentences hit hard. Longer ones carry the reader "
                "through texture and nuance, building toward something.",
                "- Dialogue should do at least two things: reveal character AND advance "
                "plot or tension. Cut anything that's just exchanging information.",
                "- Earn your abstractions. Don't say 'she felt sad' — show the thing "
                "that makes the reader feel it.",
                "- Trust subtext. Leave room for the reader.",
                "",
                "Match the user's genre and tone. If they want literary fiction, write "
                "literary fiction. If they want pulp, write pulp with conviction. "
                "Never condescend to the form.",
            ]
        else:
            dev_parts = [
                "Always respond with tool calls, not just text.\n\n"
                "TOOL PATTERNS:\n\n"
                "Modify existing file → read_file then edit_file:\n"
                "   read_file(path='config.py') → "
                "edit_file(path='config.py')\n\n"
                "Create new file → write_file:\n"
                "   write_file(path='hello.py', content='...')\n\n"
                "Find something across files → search:\n"
                "   search(query='test_')\n\n"
                "Complex or multi-step task → plan first:\n"
                "   plan(prompt='refactor database from API')\n\n"
                "Run a command, git, or tests → bash:\n"
                "   bash(command='git log -5')\n"
                "   bash(command='pytest')\n\n"
                "Retrieve a URL → web_fetch:\n"
                "   web_fetch(url='https://example.com')\n\n"
                "Look up documentation → man:\n"
                "   man(page='tar')",
            ]
        if self.instructions:
            dev_parts.append("")
            dev_parts.append(self.instructions)
        memories = load_memories()
        if memories:
            dev_parts.append("")
            dev_parts.append(
                f"REMINDER: You currently have {len(memories)} memories stored. "
                "Use recall to see them."
            )
        self.system_messages.append({"role": "system", "content": "\n".join(dev_parts)})
        # Agent prefix: system + developer only (no memories)
        self._agent_system_messages = list(self.system_messages)

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
    ) -> dict[str, Any] | None:
        """Build provider-specific extra parameters."""
        prov = provider or self._provider
        if prov.provider_name == "openai":
            kwargs = dict(self._chat_template_kwargs_base)
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            return {"chat_template_kwargs": kwargs}
        return None

    # Retryable error names are now provided by LLMProvider.retryable_error_names.
    _MAX_RETRIES = 3
    _RETRY_BASE_DELAY = 1.0  # seconds

    def _create_stream_with_retry(self, msgs: list[dict[str, Any]]) -> Iterator[StreamChunk]:
        """Create a streaming request with retry on transient errors.

        If all retries fail and a fallback chain is configured, tries each
        fallback model in order before giving up.  Checks the circuit breaker
        before attempting a call — fast-fails when the backend is unreachable.
        """
        # Circuit breaker check — fast-fail if backend is known to be down
        if self._health_monitor and not self._health_monitor.acquire_request_permit():
            raise ConnectionError("Backend unreachable (circuit breaker open)")

        try:
            result = self._try_stream(self.client, self.model, msgs)
            if self._health_monitor:
                self._health_monitor.record_success()
            return result
        except BaseException as primary_err:
            if self._health_monitor:
                self._health_monitor.record_failure()
            if isinstance(primary_err, (KeyboardInterrupt, SystemExit)):
                raise
            if not self._registry or not self._registry.fallback:
                raise
            # Try each fallback model.  Fallbacks may use different backends;
            # we intentionally do NOT call record_success/failure for fallbacks —
            # recovery of the primary backend is detected by the background probe.
            for alias in self._registry.fallback:
                if alias == self._model_alias:
                    continue
                try:
                    fb_client, fb_model, _ = self._registry.resolve(alias)
                    fb_provider = self._registry.get_provider(alias)
                    self.ui.on_info(f"[Primary model failed, falling back to {alias}]")
                    return self._try_stream(fb_client, fb_model, msgs, provider=fb_provider)
                except Exception as fb_err:
                    self.ui.on_info(f"[Fallback {alias} also failed: {fb_err}]")
                    continue
            raise primary_err

    def _try_stream(
        self,
        client: Any,
        model: str,
        msgs: list[dict[str, Any]],
        provider: LLMProvider | None = None,
    ) -> Iterator[StreamChunk]:
        """Attempt a streaming API call with retries on transient errors."""
        prov = provider or self._provider
        last_err: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return prov.create_streaming(
                    client=client,
                    model=model,
                    messages=msgs,
                    tools=self._tools if not self.creative_mode else None,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    reasoning_effort=self.reasoning_effort,
                    extra_params=self._provider_extra_params(provider=prov),
                )
            except Exception as e:
                ename = type(e).__name__
                if ename not in prov.retryable_error_names or attempt == self._MAX_RETRIES:
                    raise
                last_err = e
                delay = self._RETRY_BASE_DELAY * (2**attempt)
                self.ui.on_info(f"[Retrying in {delay:.0f}s: {ename}]")
                time.sleep(delay)
        assert last_err is not None  # unreachable, but satisfies type checker
        raise last_err

    def send(self, user_input: str) -> None:
        """Send user input and handle the response loop (including tool calls)."""
        self.messages.append({"role": "user", "content": user_input})
        self._msg_tokens.append(max(1, int(len(user_input) / self._chars_per_token)))
        save_message(self._session_id, "user", user_input)

        try:
            while True:
                msgs = self._full_messages()

                if self.debug:
                    self._debug_print_request(msgs)

                self._emit_state("thinking")
                self.ui.on_thinking_start()
                try:
                    stream = self._create_stream_with_retry(msgs)
                    assistant_msg = self._stream_response(stream)
                finally:
                    self.ui.on_thinking_stop()

                self._update_token_table(assistant_msg)
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
                    import json as _json

                    provider_data = _json.dumps(assistant_msg["_provider_content"])
                if content or provider_data is not None:
                    save_message(
                        self._session_id, "assistant", content, provider_data=provider_data
                    )
                if tc:
                    for call in tc:
                        fn = call.get("function", {})
                        name = fn.get("name", "")
                        if name not in (
                            "remember",
                            "forget",
                            "recall",
                        ):
                            save_message(
                                self._session_id,
                                "tool_call",
                                None,
                                name,
                                fn.get("arguments", ""),
                                tool_call_id=call.get("id"),
                            )

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    self._print_status_line()
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
                    # Auto-title session after first exchange
                    if not self._title_generated:
                        self._title_generated = True
                        threading.Thread(target=self._generate_title, daemon=True).start()
                    self._emit_state("idle")
                    break

                # Execute tool calls (potentially in parallel)
                self._emit_state("running")
                results, user_feedback = self._execute_tools(tool_calls)
                # Map tool_call_id → tool name for logging
                _tc_names = {c["id"]: c.get("function", {}).get("name", "") for c in tool_calls}
                for tc_id, output in results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": output,
                    }
                    self.messages.append(tool_msg)
                    self._msg_tokens.append(max(1, int(len(output) / self._chars_per_token)))
                    # Log tool result (skip memory tools to avoid noise)
                    _tname = _tc_names.get(tc_id, "")
                    if _tname not in (
                        "remember",
                        "forget",
                        "recall",
                    ):
                        save_message(
                            self._session_id,
                            "tool_result",
                            output[:2000],
                            _tname,
                            tool_call_id=tc_id,
                        )
                # Inject user feedback from approval prompt (e.g. "y, use full path")
                if user_feedback:
                    self.messages.append({"role": "user", "content": user_feedback})
                    self._msg_tokens.append(max(1, int(len(user_feedback) / self._chars_per_token)))
        except KeyboardInterrupt:
            # Remove any partial tool results, then the originating assistant
            # message with unanswered tool_calls — keep _msg_tokens in sync
            while self.messages and self.messages[-1]["role"] == "tool":
                self.messages.pop()
                if self._msg_tokens:
                    self._msg_tokens.pop()
            while (
                self.messages
                and self.messages[-1]["role"] == "assistant"
                and self.messages[-1].get("tool_calls")
            ):
                self.messages.pop()
                if self._msg_tokens:
                    self._msg_tokens.pop()
            self._emit_state("error")
            raise
        except Exception:
            self._emit_state("error")
            raise

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

    def _stream_response(self, stream: Iterator[StreamChunk]) -> dict[str, Any]:
        """Stream response, dispatching tokens to the UI as they arrive.

        Handles two reasoning delivery mechanisms:
        1. The `reasoning_delta` field (e.g. vLLM with --reasoning-parser)
        2. <think>...</think> tags in regular content (common default)

        Calls self.ui.on_thinking_stop() on the first received delta.

        Returns the complete assistant message as a dict suitable for
        appending to self.messages.
        """
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
        for chunk in stream:
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
                    }
                else:
                    self._last_usage["prompt_tokens"] = max(
                        self._last_usage["prompt_tokens"], chunk.usage.prompt_tokens
                    )
                    self._last_usage["completion_tokens"] = max(
                        self._last_usage["completion_tokens"], chunk.usage.completion_tokens
                    )
                    self._last_usage["total_tokens"] = (
                        self._last_usage["prompt_tokens"] + self._last_usage["completion_tokens"]
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

        # Flush any remaining buffered text
        if pending:
            _flush_text(pending, in_think)

        # Warn on non-standard finish reasons
        if finish_reason == "length":
            self.ui.on_error(
                f"Warning: response truncated (hit {self.max_tokens} token limit). "
                f"Use --max-tokens to increase, or /compact to free context."
            )
            # Drop partial tool calls — they'll have malformed JSON
            if tool_calls_acc:
                self.ui.on_error("Discarding partial tool calls from truncated response.")
                tool_calls_acc.clear()
        elif finish_reason == "content_filter":
            self.ui.on_error("Warning: response blocked by content filter.")

        # Signal end of stream to the UI
        self.ui.on_stream_end()

        # Build assistant message dict
        msg: dict[str, Any] = {"role": "assistant"}

        content = "".join(content_parts)
        if content:
            msg["content"] = content
        else:
            msg["content"] = None

        if tool_calls_acc:
            msg["tool_calls"] = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]

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
            f"tools={0 if self.creative_mode else len(self._tools)}{RESET}"
        )
        lines.append(f"{GRAY}[request] {len(msgs)} messages:{RESET}")
        for i, m in enumerate(msgs):
            role = m["role"]
            content = m.get("content") or ""
            tool_calls = m.get("tool_calls")
            tc_id = m.get("tool_call_id")

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

    def _msg_char_count(self, msg: dict[str, Any]) -> int:
        """Count characters in a message, including tool call arguments."""
        n = len(msg.get("content") or "")
        for tc in msg.get("tool_calls", []):
            n += len(tc.get("function", {}).get("name", ""))
            n += len(tc.get("function", {}).get("arguments", ""))
        return n

    def _update_token_table(self, assistant_msg: dict[str, Any]) -> None:
        """Update per-message token estimates using API usage data."""
        if not self._last_usage:
            return

        prompt_tok = self._last_usage["prompt_tokens"]
        compl_tok = self._last_usage["completion_tokens"]

        # Calibrate chars_per_token ratio from actual usage.
        all_msgs = self._full_messages()  # system + self.messages (before append)
        tool_def_chars = sum(len(json.dumps(t)) for t in self._tools)
        total_chars = sum(self._msg_char_count(m) for m in all_msgs) + tool_def_chars
        if total_chars > 0 and prompt_tok > 0:
            self._chars_per_token = total_chars / prompt_tok

        # Compute system_tokens (stable after first call)
        sys_chars = sum(self._msg_char_count(m) for m in self.system_messages)
        self._system_tokens = max(1, int(sys_chars / self._chars_per_token))

        # Re-estimate all message token counts with calibrated ratio
        self._msg_tokens = [
            max(1, int(self._msg_char_count(m) / self._chars_per_token)) for m in self.messages
        ]

        # Stash completion_tokens for the assistant message about to be appended
        self._assistant_pending_tokens = compl_tok

    def _print_status_line(self) -> None:
        """Emit status info via the UI."""
        if not self._last_usage:
            return
        self.ui.on_status(self._last_usage, self.context_window, self.reasoning_effort)

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
                    "instructions the user stated.\n\n"
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
            _last_err: Exception | None = None
            result: CompletionResult | None = None
            for attempt in range(self._MAX_RETRIES + 1):
                try:
                    result = self._provider.create_completion(
                        client=self.client,
                        model=self.model,
                        messages=summary_msgs,
                        max_tokens=summary_max_tokens,
                        temperature=0.3,
                        reasoning_effort="low",
                        extra_params=self._provider_extra_params(reasoning_effort="low"),
                    )
                    break
                except Exception as e:
                    ename = type(e).__name__
                    if (
                        ename not in self._provider.retryable_error_names
                        or attempt == self._MAX_RETRIES
                    ):
                        raise
                    _last_err = e
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

        # Rebuild token table
        su_tok = max(1, int(self._msg_char_count(summary_user) / self._chars_per_token))
        sa_tok = max(1, int(self._msg_char_count(summary_asst) / self._chars_per_token))
        self._msg_tokens = [su_tok, sa_tok]
        after_tokens = self._system_tokens + sum(self._msg_tokens)

        self.ui.on_info(f"[compacted: ~{before_tokens:,} -> ~{after_tokens:,} tokens]")
        separator = "\u2500" * 60
        lines = [separator]
        for line in summary.splitlines():
            lines.append(f"  {line}")
        lines.append(separator)
        self.ui.on_info("\n".join(lines))

    # -- Two-phase tool execution -----------------------------------------------
    #
    # Phase 1 — prepare: parse args, validate, build preview text (serial)
    # Phase 2 — approve: display all previews, single prompt (serial)
    # Phase 3 — execute: run approved tools (parallel if multiple)

    def _execute_tools(
        self, tool_calls: list[dict[str, Any]]
    ) -> tuple[list[tuple[str, str]], str | None]:
        """Execute tool calls with batch preview and approval.

        Returns (results, user_feedback) where user_feedback is an optional
        message the user typed alongside their approval (e.g. "y, use full path").
        """
        # Phase 1: prepare all tool calls
        items = [self._prepare_tool(tc) for tc in tool_calls]

        # Phase 2: approve via UI
        self._emit_state("attention")
        approved, user_feedback = self.ui.approve_tools(items)
        self._emit_state("running")
        if not approved:
            # Mark all pending items as denied
            for item in items:
                if item.get("needs_approval") and not item.get("error"):
                    item["denied"] = True
                    item["denial_msg"] = user_feedback or "Denied by user"
            user_feedback = None  # feedback is in the denial_msg

        # Phase 3: execute
        def run_one(item: dict[str, Any]) -> tuple[str, str]:
            if item.get("error"):
                return item["call_id"], item["error"]
            if item.get("denied"):
                return item["call_id"], item.get("denial_msg", "Denied by user")
            result: tuple[str, str] = item["execute"](item)
            return result

        if len(items) == 1:
            results = [run_one(items[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(run_one, items))

        # Post-plan gate: prompt user on main thread after plan completes
        for i, item in enumerate(items):
            if (
                item.get("func_name") == "plan"
                and not item.get("error")
                and not item.get("denied")
                and not self.auto_approve
            ):
                cid, output = results[i]
                # Let the UI present the plan for review
                self._emit_state("attention")
                resp = self.ui.on_plan_review(output)
                self._emit_state("running")
                if resp.lower() in ("n", "no", "reject"):
                    output += (
                        "\n\n---\nUser REJECTED this plan. Do not proceed "
                        "with implementation. Ask the user what they want instead."
                    )
                elif resp:
                    output += f"\n\n---\nUser feedback on this plan: {resp}"
                results[i] = (cid, output)

        return results, user_feedback

    def _prepare_tool(self, tc: dict[str, Any]) -> dict[str, Any]:
        """Parse a tool call and prepare preview info for display."""
        call_id = tc["id"]
        func_name = tc["function"]["name"]
        raw_args = tc["function"]["arguments"]

        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            args = None
            # Fallback 1: regex-extract a known key from malformed JSON
            for key in (
                "command",
                "code",
                "content",
                "page",
                "path",
                "pattern",
                "prompt",
                "query",
                "url",
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
                return {
                    "call_id": call_id,
                    "func_name": func_name,
                    "header": f"\u2717 {func_name}: {exc}",
                    "preview": f"    {RED}{preview}{RESET}",
                    "needs_approval": False,
                    "error": f"JSON parse error: {exc}\nRaw arguments: {raw_args[:500]}",
                }

        preparers = {
            "bash": self._prepare_bash,
            "read_file": self._prepare_read_file,
            "search": self._prepare_search,
            "write_file": self._prepare_write_file,
            "edit_file": self._prepare_edit_file,
            "math": self._prepare_math,
            "man": self._prepare_man,
            "web_fetch": self._prepare_web_fetch,
            "web_search": self._prepare_web_search,
            "task": self._prepare_task,
            "plan": self._prepare_plan,
            "remember": self._prepare_remember,
            "recall": self._prepare_recall,
            "forget": self._prepare_forget,
        }
        preparer = preparers.get(func_name)
        if not preparer:
            # Check if this is an MCP tool
            if self._mcp_client and self._mcp_client.is_mcp_tool(func_name):
                return self._prepare_mcp_tool(call_id, func_name, args)
            return {
                "call_id": call_id,
                "func_name": func_name,
                "header": f"\u2717 Unknown tool: {func_name}",
                "preview": "",
                "needs_approval": False,
                "error": f"Unknown tool: {func_name}",
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
        if "\n" in command:
            display_cmd += f" ... ({command.count(chr(10))} more lines)"
        return {
            "call_id": call_id,
            "func_name": "bash",
            "header": f"\u2699 bash: {display_cmd}",
            "preview": "",
            "needs_approval": True,
            "approval_label": "bash",
            "execute": self._exec_bash,
            "command": command,
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
        exists = os.path.exists(resolved)
        is_overwrite = exists and resolved not in self._read_files

        # Build preview
        preview_parts = []
        if is_overwrite:
            preview_parts.append(
                f"    {YELLOW}Warning: overwriting existing file not previously read{RESET}"
            )
        text = content[:500]
        if len(content) > 500:
            text += f"\n... ({len(content)} chars total)"
        preview_parts.append(f"{DIM}{textwrap.indent(text, '    ')}{RESET}")

        return {
            "call_id": call_id,
            "func_name": "write_file",
            "header": f"\u2699 write_file: {path} ({len(content)} chars)",
            "preview": "\n".join(preview_parts),
            "needs_approval": True,
            "approval_label": "overwrite_file" if is_overwrite else "write_file",
            "execute": self._exec_write_file,
            "path": path,
            "resolved": resolved,
            "content": content,
        }

    def _prepare_edit_file(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        near_line = args.get("near_line")
        if isinstance(near_line, str):
            try:
                near_line = int(near_line)
            except ValueError:
                near_line = None
        if not path:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": "\u2717 edit_file: missing path",
                "preview": "",
                "needs_approval": False,
                "error": "Error: missing path",
            }
        if not old_string:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": "\u2717 edit_file: missing old_string",
                "preview": "",
                "needs_approval": False,
                "error": "Error: missing old_string",
            }
        path = os.path.expanduser(path)
        resolved = os.path.realpath(path)

        if resolved not in self._read_files:
            return {
                "call_id": call_id,
                "func_name": "edit_file",
                "header": f"\u2717 edit_file: {path}",
                "preview": "",
                "needs_approval": False,
                "error": f"Error: must read_file {path} before editing it",
            }

        # Pre-read to validate and build diff preview
        try:
            with open(path) as f:
                content = f.read()
            occurrences = find_occurrences(content, old_string)
            if len(occurrences) == 0:
                return {
                    "call_id": call_id,
                    "func_name": "edit_file",
                    "header": f"\u2717 edit_file: {path}",
                    "preview": "",
                    "needs_approval": False,
                    "error": f"Error: old_string not found in {path}",
                }
            if len(occurrences) > 1 and near_line is None:
                line_list = ", ".join(str(ln) for ln in occurrences)
                return {
                    "call_id": call_id,
                    "func_name": "edit_file",
                    "header": f"\u2717 edit_file: {path}",
                    "preview": "",
                    "needs_approval": False,
                    "error": (
                        f"Error: old_string found {len(occurrences)} times "
                        f"at lines {line_list} — use near_line to pick one"
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
        old_preview = old_string[:200] + ("..." if len(old_string) > 200 else "")
        new_preview = new_string[:200] + ("..." if len(new_string) > 200 else "")
        for line in old_preview.splitlines():
            preview_parts.append(f"    {RED}- {line}{RESET}")
        if new_string:
            for line in new_preview.splitlines():
                preview_parts.append(f"    {GREEN}+ {line}{RESET}")
        else:
            preview_parts.append(f"    {YELLOW}(deletion — {len(old_string)} chars removed){RESET}")

        return {
            "call_id": call_id,
            "func_name": "edit_file",
            "header": f"\u2699 edit_file: {path}",
            "preview": "\n".join(preview_parts),
            "needs_approval": True,
            "approval_label": "edit_file",
            "execute": self._exec_edit_file,
            "path": path,
            "resolved": resolved,
            "old_string": old_string,
            "new_string": new_string,
            "near_line": near_line,
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
        display = code[:300]
        if len(code) > 300:
            display += f"\n... ({len(code)} chars total)"
        preview = f"{DIM}{textwrap.indent(display, '    ')}{RESET}"
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
                "preview": f"    {RED}{page}{RESET}",
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
                "preview": f"    {RED}{url}{RESET}",
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
                "preview": f"    {RED}{url}{RESET}",
                "needs_approval": False,
                "error": f"Error: {ssrf_err}",
            }
        q_preview = question[:200] + ("..." if len(question) > 200 else "")
        preview = f"    {DIM}{url}\n    Q: {q_preview}{RESET}"
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
        if not get_tavily_key():
            return {
                "call_id": call_id,
                "func_name": "web_search",
                "header": "\u2717 web_search: no API key",
                "preview": "",
                "needs_approval": False,
                "error": (
                    "Error: Tavily API key not configured. "
                    "Set it in ~/.config/turnstone/tavily_key or $TAVILY_API_KEY. "
                    "Use web_fetch with a direct URL as an alternative."
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
        preview = f"    {DIM}{q_preview}{RESET}"
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

    def _prepare_task(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a general-purpose sub-agent task for approval."""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {
                "call_id": call_id,
                "func_name": "task",
                "header": "\u2717 task: empty prompt",
                "preview": "",
                "needs_approval": False,
                "error": "Error: empty prompt",
            }
        preview_text = prompt[:300] + ("..." if len(prompt) > 300 else "")
        return {
            "call_id": call_id,
            "func_name": "task",
            "header": "\u2699 task (autonomous agent)",
            "preview": f"    {DIM}{preview_text}{RESET}",
            "needs_approval": True,
            "approval_label": "task",
            "execute": self._exec_task,
            "prompt": prompt,
        }

    def _prepare_plan(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a planning agent for approval."""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {
                "call_id": call_id,
                "func_name": "plan",
                "header": "\u2717 plan: empty prompt",
                "preview": "",
                "needs_approval": False,
                "error": "Error: empty prompt",
            }
        preview_text = prompt[:300] + ("..." if len(prompt) > 300 else "")
        return {
            "call_id": call_id,
            "func_name": "plan",
            "header": "\u2699 plan (planning agent)",
            "preview": f"    {DIM}{preview_text}{RESET}",
            "needs_approval": True,
            "approval_label": "plan",
            "execute": self._exec_plan,
            "prompt": prompt,
        }

    def _prepare_remember(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a remember (save memory) action."""
        key = normalize_key((args.get("key") or "").strip())
        value = (args.get("value") or "").strip()
        if not key or not value:
            return {
                "call_id": call_id,
                "func_name": "remember",
                "header": "\u2717 remember: requires key and value",
                "preview": "",
                "needs_approval": False,
                "error": "Error: both 'key' and 'value' are required",
            }
        return {
            "call_id": call_id,
            "func_name": "remember",
            "header": f"\u2699 remember: {key}",
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_remember,
            "key": key,
            "value": value,
        }

    def _prepare_forget(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a forget (delete memory) action."""
        key = normalize_key((args.get("key") or "").strip())
        if not key:
            return {
                "call_id": call_id,
                "func_name": "forget",
                "header": "\u2717 forget: empty key",
                "preview": "",
                "needs_approval": False,
                "error": "Error: key is required",
            }
        return {
            "call_id": call_id,
            "func_name": "forget",
            "header": f"\u2699 forget: {key}",
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_forget,
            "key": key,
        }

    def _prepare_recall(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Prepare a recall action."""
        query = (args.get("query") or "").strip()
        limit = args.get("limit", 20)
        if isinstance(limit, str):
            try:
                limit = int(limit)
            except ValueError:
                limit = 20
        return {
            "call_id": call_id,
            "func_name": "recall",
            "header": f"\u2699 recall{': ' + query[:80] if query else ''}",
            "preview": "",
            "needs_approval": False,
            "execute": self._exec_recall,
            "query": query,
            "limit": min(limit, 50),
        }

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
            "preview": f"{DIM}{preview}{RESET}",
            "needs_approval": True,
            "approval_label": "mcp_tool",
            "execute": self._exec_mcp_tool,
            "mcp_func_name": func_name,
            "mcp_args": args,
        }

    def _exec_mcp_tool(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute an MCP tool call via the MCPClientManager."""
        call_id: str = item["call_id"]
        func_name: str = item["mcp_func_name"]
        args: dict[str, Any] = item["mcp_args"]

        assert self._mcp_client is not None
        try:
            output = self._mcp_client.call_tool_sync(func_name, args, timeout=self.tool_timeout)
        except TimeoutError:
            output = f"MCP tool timed out after {self.tool_timeout}s"
            self.ui.on_error(output)
        except Exception as e:
            output = f"MCP tool error: {e}"
            self.ui.on_error(output)

        output = self._truncate_output(output)
        self.ui.on_tool_result(call_id, func_name, output)
        return call_id, output

    # -- Execute methods (do the work, report output via UI) -------------------

    def _exec_bash(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute a bash command via temp script, streaming stdout."""
        call_id, command = item["call_id"], item["command"]
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
                f.write(command)
                script_path = f.name
            try:
                proc = subprocess.Popen(
                    ["bash", script_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
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

                timer = threading.Timer(self.tool_timeout, _on_timeout)
                timer.start()
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        stdout_parts.append(line)
                        with contextlib.suppress(Exception):
                            self.ui.on_tool_output_chunk(call_id, line)
                finally:
                    timer.cancel()

                proc.wait()
                stderr_thread.join()
            finally:
                os.unlink(script_path)

            if timed_out.is_set():
                raise subprocess.TimeoutExpired(cmd="bash", timeout=self.tool_timeout)

            output = "".join(stdout_parts)
            if stderr_lines:
                output += ("\n" if output else "") + "".join(stderr_lines)
            output = output.strip()
            output = self._truncate_output(output)

            self.ui.on_tool_result(call_id, "bash", output)

            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"

            return call_id, output if output else "(no output)"

        except subprocess.TimeoutExpired:
            msg = f"Command timed out after {self.tool_timeout}s"
            self.ui.on_error(msg)
            return call_id, msg
        except Exception as e:
            msg = f"Error executing command: {e}"
            self.ui.on_error(msg)
            return call_id, msg

    def _exec_read_file(self, item: dict[str, Any]) -> tuple[str, str]:
        """Read a file and return numbered lines, optionally sliced."""
        call_id, path = item["call_id"], item["path"]
        offset = item.get("offset")  # 1-based, or None
        limit = item.get("limit")  # max lines, or None
        resolved = os.path.realpath(path)

        try:
            with open(path) as f:
                all_lines = f.readlines()
        except FileNotFoundError:
            self._read_files.discard(resolved)
            return call_id, f"Error: {path} not found"
        except Exception as e:
            self._read_files.discard(resolved)
            return call_id, f"Error reading {path}: {e}"

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
        self.ui.on_tool_result(call_id, "read_file", desc)

        return call_id, output if output else "(empty file)"

    def _exec_search(self, item: dict[str, Any]) -> tuple[str, str]:
        """Search file contents for a regex pattern using grep."""
        call_id = item["call_id"]
        pattern, path = item["pattern"], item["path"]
        try:
            result = subprocess.run(
                [
                    "grep",
                    "-rn",
                    "-I",
                    "-E",
                    "-m",
                    "200",  # max matches per file
                    "--color=never",  # no ANSI codes in output
                    "--",
                    pattern,
                    path,  # -- prevents pattern as flag
                ],
                capture_output=True,
                text=True,
                timeout=self.tool_timeout,
            )
            output = result.stdout.strip()
            if result.returncode == 1:
                output = "(no matches)"
            elif result.returncode > 1:
                output = result.stderr.strip() or f"grep error (exit {result.returncode})"

            # Count matches BEFORE truncation
            match_count = output.count("\n") + 1 if result.returncode == 0 and output else 0

            original_len = len(output)
            output = self._truncate_output(output)

            desc = f"{match_count} matches" if match_count else "no matches"
            if original_len > 500:
                desc += f" ({original_len} chars)"
            self.ui.on_tool_result(call_id, "search", desc)

            return call_id, output

        except subprocess.TimeoutExpired:
            msg = f"Search timed out after {self.tool_timeout}s"
            self.ui.on_error(msg)
            return call_id, msg
        except Exception as e:
            msg = f"Search error: {e}"
            self.ui.on_error(msg)
            return call_id, msg

    # Tools the agent can auto-execute without user approval (read-only).
    _AGENT_AUTO_TOOLS = AGENT_AUTO_TOOLS
    _TASK_AUTO_TOOLS = TASK_AUTO_TOOLS

    def _run_agent(
        self,
        agent_messages: list[dict[str, Any]],
        label: str = "agent",
        tools: list[dict[str, Any]] | None = None,
        auto_tools: set[str] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Run an autonomous agent loop.

        Args:
            agent_messages: Pre-built message list (system + developer + user).
            label: Display prefix for progress lines ("agent" or "plan").
            tools: Tool definitions to send to the API. Defaults to AGENT_TOOLS (read-only).
            auto_tools: Set of tool names the agent may execute. Defaults to _AGENT_AUTO_TOOLS.
            reasoning_effort: Override reasoning effort for this agent.

        Returns:
            Final content string from the agent.
        """
        if tools is None:
            tools = self._agent_tools
        if auto_tools is None:
            auto_tools = self._AGENT_AUTO_TOOLS
        max_tool_turns = self.agent_max_turns

        # Resolve agent model and provider: use registry.agent_model if configured
        agent_client = self.client
        agent_model = self.model
        agent_provider = self._provider
        if self._registry and self._registry.agent_model:
            agent_client, agent_model, _ = self._registry.resolve(self._registry.agent_model)
            agent_provider = self._registry.get_provider(self._registry.agent_model)

        # Build extra params for agent calls
        agent_extra: dict[str, Any] | None = None
        if agent_provider.provider_name == "openai":
            agent_kwargs = dict(self._chat_template_kwargs_base)
            if reasoning_effort:
                agent_kwargs["reasoning_effort"] = reasoning_effort
            agent_extra = {"chat_template_kwargs": agent_kwargs}

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
            result = _api_call(agent_messages)

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
                tool_name = tc_dict["function"]["name"]

                # Guard 1: block recursive agent calls.
                if tool_name in ("task", "plan"):
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
        agent_messages = list(self._agent_system_messages) + [
            task_instruction,
            {"role": "user", "content": prompt},
        ]
        try:
            return call_id, self._run_agent(
                agent_messages,
                label="task",
                tools=self._task_tools,
                auto_tools=self._TASK_AUTO_TOOLS,
            )
        except KeyboardInterrupt:
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

    def _exec_plan(self, item: dict[str, Any]) -> tuple[str, str]:
        """Run a planning agent and write the result to .plan-<session_id>.md."""
        call_id, prompt = item["call_id"], item["prompt"]
        plan_path = f".plan-{self._session_id}.md"

        # If plan was called before in this session, the previous assistant
        # tool_call + tool result are already in self.messages — pass them
        # directly to the inner agent so it refines rather than restarts.
        prior_plan_msgs: list[dict[str, Any]] = []
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == "plan":
                        tc_id = tc["id"]
                        for j in range(i + 1, len(self.messages)):
                            if (
                                self.messages[j].get("role") == "tool"
                                and self.messages[j].get("tool_call_id") == tc_id
                            ):
                                prior_plan_msgs = [msg, self.messages[j]]
                                break

        agent_messages = list(self._agent_system_messages)
        agent_messages.append({"role": "system", "content": self._PLAN_IDENTITY})
        agent_messages.extend(prior_plan_msgs)
        agent_messages.append({"role": "user", "content": prompt})

        try:
            content = self._run_agent(
                agent_messages,
                label="plan",
                reasoning_effort="high",
            )
        except KeyboardInterrupt:
            return call_id, "(plan interrupted by user)"
        except Exception as e:
            self.ui.on_info(f"[plan error] {e}")
            return call_id, f"Plan error: {e}"

        # Write to file separately — always return content even if write fails
        try:
            with open(plan_path, "w") as f:
                f.write(content)
            self.ui.on_info(f"Plan written to {plan_path}")
        except OSError as e:
            self.ui.on_info(f"[plan] could not write {plan_path}: {e}")

        return call_id, content

    def _exec_remember(self, item: dict[str, Any]) -> tuple[str, str]:
        """Save a persistent memory."""
        call_id, key, value = item["call_id"], item["key"], item["value"]
        try:
            old_value = save_memory(key, value)
            self._init_system_messages()
            if old_value is not None:
                msg = f"Updated memory: {key} = {value} (was: {old_value})"
            else:
                msg = f"Saved memory: {key} = {value}"
            self.ui.on_tool_result(call_id, "remember", msg)
            return call_id, msg
        except Exception as e:
            return call_id, f"Error: {e}"

    def _exec_forget(self, item: dict[str, Any]) -> tuple[str, str]:
        """Remove a persistent memory by key."""
        call_id, key = item["call_id"], item["key"]
        try:
            deleted = delete_memory(key)
            if not deleted:
                msg = f"Error: memory '{key}' not found"
            else:
                self._init_system_messages()
                msg = f"Forgot: {key}"
            self.ui.on_tool_result(call_id, "forget", msg)
            return call_id, msg
        except Exception as e:
            return call_id, f"Error: {e}"

    def _exec_recall(self, item: dict[str, Any]) -> tuple[str, str]:
        """Search memories and conversation history."""
        call_id = item["call_id"]
        query, limit = item["query"], item["limit"]
        parts: list[str] = []

        # Memories: list all (no query) or search (with query)
        try:
            rows = search_memories(query) if query else load_memories()
            if rows:
                parts.append("Memories:\n" + "\n".join(f"  {k}={v}" for k, v in rows))
            elif not query:
                parts.append("No memories stored.")
        except Exception:
            pass

        # Conversations: only when a query is provided
        if query:
            conv_rows = search_history(query, limit)
            if conv_rows:
                lines = []
                for ts, sid, role, content, tool_name in conv_rows:
                    label = f"{role}({tool_name})" if tool_name else role
                    text = (content or "")[:500]
                    if content and len(content) > 500:
                        text += "..."
                    lines.append(f"[{ts} {sid}] {label}: {text}")
                parts.append(f"Conversations ({len(conv_rows)} matches):\n" + "\n".join(lines))

        output = "\n\n".join(parts) if parts else f"No results for '{query}'."
        self.ui.on_tool_result(call_id, "recall", output)
        return call_id, output

    def _exec_write_file(self, item: dict[str, Any]) -> tuple[str, str]:
        """Write content to a file, creating parent directories as needed."""
        call_id = item["call_id"]
        path, content, resolved = item["path"], item["content"], item["resolved"]
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            self._read_files.add(resolved)
            msg = f"Wrote {len(content)} chars to {path}"
            self.ui.on_tool_result(call_id, "write_file", msg)
            return call_id, msg
        except Exception as e:
            return call_id, f"Error writing {path}: {e}"

    def _exec_edit_file(self, item: dict[str, Any]) -> tuple[str, str]:
        """Replace an exact string in a file (re-reads to avoid TOCTOU).

        When near_line is set, picks the occurrence nearest that line
        instead of requiring uniqueness.
        """
        call_id = item["call_id"]
        path, old_string, new_string = (
            item["path"],
            item["old_string"],
            item["new_string"],
        )
        near_line = item.get("near_line")
        try:
            with open(path) as f:
                content = f.read()
            occurrences = find_occurrences(content, old_string)
            if len(occurrences) == 0:
                return (
                    call_id,
                    f"Error: old_string no longer found in {path} (file changed)",
                )
            if len(occurrences) > 1 and near_line is None:
                line_list = ", ".join(str(ln) for ln in occurrences)
                return (
                    call_id,
                    f"Error: old_string found {len(occurrences)} times "
                    f"at lines {line_list} (file changed)",
                )
            if near_line is not None and len(occurrences) > 1:
                # Replace only the occurrence nearest to near_line
                idx = pick_nearest(content, old_string, near_line)
                content = content[:idx] + new_string + content[idx + len(old_string) :]
            else:
                content = content.replace(old_string, new_string, 1)
            with open(path, "w") as f:
                f.write(content)
            msg = f"Edited {path}: replaced 1 occurrence"
            self.ui.on_tool_result(call_id, "edit_file", msg)
            return call_id, msg
        except Exception as e:
            return call_id, f"Error writing {path}: {e}"

    def _exec_math(self, item: dict[str, Any]) -> tuple[str, str]:
        """Execute Python code in sandboxed subprocess."""
        call_id, code = item["call_id"], item["code"]
        output, is_error = execute_math_sandboxed(code, timeout=self.tool_timeout)
        output = self._truncate_output(output)

        self.ui.on_tool_result(call_id, "math", output)

        if is_error:
            return call_id, f"Error:\n{output}"
        return call_id, output if output else "(no output)"

    def _exec_man(self, item: dict[str, Any]) -> tuple[str, str]:
        """Look up a man or info page."""
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
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "MANWIDTH": "80", "MAN_KEEP_FORMATTING": "0"},
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
                )
                if result.returncode == 0 and result.stdout.strip():
                    text = result.stdout
                else:
                    msg = f"No man or info page found for '{page}'"
                    self.ui.on_tool_result(call_id, "man", msg)
                    return call_id, msg
        except FileNotFoundError:
            return call_id, "Error: man command not available"
        except subprocess.TimeoutExpired:
            return call_id, "Error: man page lookup timed out"

        text = self._truncate_output(text)

        self.ui.on_tool_result(call_id, "man", f"{len(text)} chars")

        return call_id, text

    def _exec_web_fetch(self, item: dict[str, Any]) -> tuple[str, str]:
        """Fetch a URL, then summarize/extract using an API call."""
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
            msg = f"Fetch failed: HTTP {e.response.status_code}"
            self.ui.on_error(msg)
            return call_id, msg
        except (httpx.RequestError, ValueError) as e:
            msg = f"Fetch failed: {e}"
            self.ui.on_error(msg)
            return call_id, msg
        except Exception as e:
            msg = f"Error fetching URL: {e}"
            self.ui.on_error(msg)
            return call_id, msg

        if not text.strip():
            return call_id, "(empty response from URL)"

        original_len = len(text)
        self.ui.on_info(f"fetched {original_len} chars, extracting...")

        # Phase 2: truncate for summarization context
        max_content = 50_000
        if len(text) > max_content:
            text = (
                text[: max_content // 2]
                + f"\n\n... [{len(text) - max_content} chars omitted] ...\n\n"
                + text[-(max_content // 2) :]
            )

        # Phase 3: summarization API call
        try:
            result = self._provider.create_completion(
                client=self.client,
                model=self.model,
                messages=[
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
                max_tokens=2000,
                temperature=0.2,
                extra_params=self._provider_extra_params(),
            )
            answer = result.content or "(no answer)"
        except Exception as e:
            answer = f"Extraction failed (page was fetched but summarization errored): {e}"

        self.ui.on_tool_result(call_id, "web_fetch", answer)

        return call_id, answer

    def _exec_web_search(self, item: dict[str, Any]) -> tuple[str, str]:
        """Search the web via Tavily API."""
        call_id = item["call_id"]
        query = item["query"]
        max_results = item.get("max_results", 5)
        topic = item.get("topic", "general")
        api_key = get_tavily_key()

        try:
            resp = httpx.post(
                "https://api.tavily.com/search",
                json={
                    "query": query,
                    "max_results": max_results,
                    "topic": topic,
                    "include_answer": True,
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.tool_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            msg = f"Tavily search failed: {e}"
            self.ui.on_error(msg)
            return call_id, msg

        parts: list[str] = []
        answer = (data.get("answer") or "").strip()
        if answer:
            parts.append(f"Answer: {answer}")

        results = data.get("results") or []
        if results:
            lines = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "")
                url = r.get("url", "")
                content = (r.get("content") or "")[:500]
                lines.append(f"{i}. [{title}]({url})\n   {content}")
            parts.append("\n".join(lines))

        output = "\n\n".join(parts) if parts else f"No results for '{query}'."

        self.ui.on_tool_result(call_id, "web_search", output)

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

        elif cmd == "/clear":
            self.messages.clear()
            self._read_files.clear()
            self._last_usage = None
            self._msg_tokens = []
            self.ui.on_info("Context cleared (session preserved in database).")

        elif cmd == "/new":
            self.messages.clear()
            self._read_files.clear()
            self._last_usage = None
            self._msg_tokens = []
            self._session_id = uuid.uuid4().hex[:12]
            self._title_generated = False
            register_session(self._session_id)
            self._save_config()
            self.ui.on_info("New session started.")

        elif cmd == "/sessions":
            rows = list_sessions(limit=20)
            if not rows:
                self.ui.on_info("No saved sessions.")
            else:
                lines = ["Sessions:\n"]
                for sid, alias, title, _created, updated, count in rows:
                    display_name = alias or sid
                    display_title = f"  {title}" if title else ""
                    marker = " *" if sid == self._session_id else "  "
                    lines.append(
                        f" {marker} {bold(display_name)}{display_title}  "
                        f"{dim(f'{count} msgs, {updated}')}"
                    )
                self.ui.on_info("\n".join(lines))

        elif cmd == "/resume":
            if not arg:
                self.ui.on_info(
                    "Usage: /resume <alias_or_session_id>\n"
                    "Use /sessions to list available sessions."
                )
            else:
                target_id = resolve_session(arg.strip())
                if not target_id:
                    self.ui.on_info(f"Session not found: {arg.strip()}")
                elif target_id == self._session_id:
                    self.ui.on_info("Already in that session.")
                elif self.resume_session(target_id):
                    self.ui.on_info(
                        f"Resumed session {bold(target_id)} ({len(self.messages)} messages loaded)"
                    )
                    name = get_session_name(target_id)
                    if name:
                        self.ui.on_rename(name)
                else:
                    self.ui.on_info(f"Session {arg.strip()} has no messages.")

        elif cmd == "/name":
            if not arg:
                self.ui.on_info(f"Current session: {self._session_id}")
            elif set_session_alias(self._session_id, arg.strip()):
                self.ui.on_info(f"Session named: {bold(arg.strip())}")
                self.ui.on_rename(arg.strip())
            else:
                self.ui.on_info(f"Alias '{arg.strip()}' is already in use.")

        elif cmd == "/delete":
            if not arg:
                self.ui.on_info(
                    "Usage: /delete <alias_or_session_id>\nUse /sessions to list sessions."
                )
            else:
                target_id = resolve_session(arg.strip())
                if not target_id:
                    self.ui.on_info(f"Session not found: {arg.strip()}")
                elif target_id == self._session_id:
                    self.ui.on_info("Cannot delete the active session.")
                elif delete_session(target_id):
                    self.ui.on_info(f"Deleted session {arg.strip()}")
                else:
                    self.ui.on_info(f"Failed to delete session {arg.strip()}")

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
                self.context_window = cfg.context_window
                if not self._manual_tool_truncation:
                    self.tool_truncation = int(cfg.context_window * self._chars_per_token * 0.5)
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
            else:
                tools = self._mcp_client.get_tools()
                if not tools:
                    self.ui.on_info("MCP client connected but no tools available.")
                else:
                    lines = [f"MCP tools ({len(tools)}):"]
                    for t in tools:
                        name = t["function"]["name"]
                        desc = t["function"].get("description", "")[:80]
                        lines.append(f"  {name}  {dim(desc)}")
                    self.ui.on_info("\n".join(lines))

        elif cmd == "/help":
            self.ui.on_info(
                "\n".join(
                    [
                        "── Slash Commands ─────────────────────────────────────",
                        "  /instructions <text>   Set developer instructions",
                        "  /clear                 Clear context (session preserved in database)",
                        "  /new                   Start a new session (old session stays resumable)",
                        "",
                        "  /sessions              List saved sessions",
                        "  /resume <id|alias>     Resume a previous session",
                        "  /name <alias>          Name the current session",
                        "  /delete <id|alias>     Delete a saved session",
                        "",
                        "  /history [query]       Search conversation history (or show recent)",
                        "  /compact               Compact conversation (summarize old messages)",
                        "",
                        "  /model [alias]         Show/switch model (alias from config)",
                        "  /raw                   Toggle reasoning content display",
                        "  /reason [low|med|high] Set/show reasoning effort",
                        "  /creative              Toggle creative writing mode (no tools)",
                        "  /debug                 Toggle raw SSE delta logging",
                        "  /mcp                   List connected MCP tools",
                        "  /help                  Show this help",
                        "  /exit                  Exit (also: Ctrl+D)",
                        "────────────────────────────────────────────────────────",
                    ]
                )
            )

        else:
            self.ui.on_info(f"Unknown command: {cmd}. Type /help for available commands.")

        return False
