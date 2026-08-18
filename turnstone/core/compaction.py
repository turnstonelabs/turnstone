"""Lifecycle-agnostic policy, sizing, and recursive summarization.

The two compaction lifecycle owners live in :mod:`turnstone.core.session`:
foreground conversation compaction owns durability/UI/generation commits, while
task-agent compaction owns an ephemeral model context beside a bounded execution
journal. This module contains only the mechanics both lifecycles share.
It deliberately knows nothing about ``ChatSession``, storage, UI protocols, or
which trajectory will receive the resulting summary.  Both owners deliberately
use the same conversation-summary contract; lifecycle independence does not
imply owner-specific prompt semantics.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from turnstone.core.model_turn import ModelTurnResult
from turnstone.core.trajectory import Turn, TurnProvenance


class CompactionIrreducibleError(Exception):
    """The recursive summarizer could not shrink an over-window input."""


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """Compacted text plus the identity of its final model turn."""

    text: str
    producer: str | None
    provenance: TurnProvenance = field(default_factory=TurnProvenance)


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Pure soft/hard compaction threshold policy.

    ``owed`` is the cooperative mid-turn decision: compact immediately above
    the hard ceiling, or above the soft threshold after the model has already
    received its one wind-down advisory.  End-of-turn callers intentionally use
    ``over_soft`` directly because there is no further cooperative turn to wait
    for.
    """

    context_window: int
    auto_compact_pct: float

    def over_soft(self, used: int) -> bool:
        return used > self.context_window * self.auto_compact_pct

    def over_hard(self, used: int) -> bool:
        return used > self.context_window * min(0.95, self.auto_compact_pct + 0.10)

    def owed(self, used: int, *, advised: bool) -> bool:
        return self.over_hard(used) or (self.over_soft(used) and advised)


MessageMeasure = Callable[[dict[str, Any] | Turn], tuple[int, int, int]]


def calibrated_chars_per_token(
    *,
    prompt_tokens: int,
    messages: Sequence[dict[str, Any] | Turn],
    tool_def_chars: int,
    measure: MessageMeasure,
    fallback: float,
    image_tokens: int = 1000,
) -> float:
    """Return a provider-anchored text chars/token ratio.

    Images receive a fixed token charge and documents contribute to budgeting
    without polluting the text ratio, matching the foreground estimator's
    established accounting.  If the provider count cannot yield a positive text
    denominator, retain ``fallback``.
    """

    text_chars = tool_def_chars
    image_count = 0
    for message in messages:
        message_text, images, _document_chars = measure(message)
        text_chars += message_text
        image_count += images
    text_prompt_tokens = prompt_tokens - image_count * image_tokens
    if text_prompt_tokens <= 0 or text_chars <= 0:
        return fallback
    return text_chars / text_prompt_tokens


@dataclass(slots=True)
class PromptTokenEstimator:
    """One immutable-lane trajectory's provider-anchored prompt estimate.

    A successful provider call anchors the exact object-identity prefix it saw.
    Locally appended turns are estimated with the calibrated ratio.  Structural
    replacement (compaction) invalidates only the anchor; the learned ratio and
    most recently served tool-definition size remain useful.
    """

    measure: MessageMeasure
    tool_def_chars: int
    chars_per_token: float = 4.0
    image_tokens: int = 1000
    _prompt_tokens: int | None = None
    _prefix_ids: tuple[int, ...] = ()

    def _message_tokens(self, message: dict[str, Any] | Turn) -> int:
        text_chars, images, document_chars = self.measure(message)
        text_tokens = int((text_chars + document_chars) / self.chars_per_token)
        return max(1, text_tokens + images * self.image_tokens)

    def estimate(self, messages: Sequence[dict[str, Any] | Turn]) -> int:
        prefix_len = len(self._prefix_ids)
        if (
            self._prompt_tokens is not None
            and prefix_len <= len(messages)
            and self._prefix_ids == tuple(id(message) for message in messages[:prefix_len])
        ):
            return self._prompt_tokens + sum(
                self._message_tokens(message) for message in messages[prefix_len:]
            )
        tool_tokens = int(self.tool_def_chars / self.chars_per_token)
        return sum(self._message_tokens(message) for message in messages) + tool_tokens

    def estimate_with_tool_defs(
        self,
        messages: Sequence[dict[str, Any] | Turn],
        *,
        tool_def_chars: int,
    ) -> int:
        """Estimate an exact request shape with a different tool envelope.

        The live provider anchor includes the tool definitions served on that
        request.  A caller preparing a differently shaped next request (task
        turn-limit synthesis uses no tools) must therefore rebase the anchor,
        not merely estimate the same messages and ignore the discarded schema.
        The calibrated ratio is the only provider-specific conversion available
        before that next request is served, so apply the tool-character delta to
        either the anchored or standalone estimate.
        """

        estimate = self.estimate(messages)
        tool_delta = int((tool_def_chars - self.tool_def_chars) / self.chars_per_token)
        return max(0, estimate + tool_delta)

    def observe(
        self,
        *,
        prompt_tokens: int,
        messages: Sequence[dict[str, Any] | Turn],
        wire_messages: Sequence[dict[str, Any]] | None = None,
        tool_def_chars: int | None = None,
    ) -> None:
        """Anchor to one successful call before its assistant turn is appended."""

        if tool_def_chars is not None:
            self.tool_def_chars = tool_def_chars
        measured_messages: Sequence[dict[str, Any] | Turn] = (
            wire_messages if wire_messages is not None else messages
        )
        self.chars_per_token = calibrated_chars_per_token(
            prompt_tokens=prompt_tokens,
            messages=measured_messages,
            tool_def_chars=self.tool_def_chars,
            measure=self.measure,
            fallback=self.chars_per_token,
            image_tokens=self.image_tokens,
        )
        self._prompt_tokens = prompt_tokens
        self._prefix_ids = tuple(id(message) for message in messages)

    def append_exact(self, message: dict[str, Any] | Turn, tokens: int) -> None:
        """Extend a live provider anchor with one accepted completion turn."""

        if self._prompt_tokens is None:
            return
        self._prompt_tokens += max(1, tokens)
        self._prefix_ids += (id(message),)

    def invalidate(self) -> None:
        self._prompt_tokens = None
        self._prefix_ids = ()

    def tokens_for(self, messages: Sequence[dict[str, Any] | Turn]) -> int:
        """Estimate a standalone sequence without tool definitions or anchoring."""

        return sum(self._message_tokens(message) for message in messages)


SummaryCompletion = Callable[[str, str, int], ModelTurnResult]


def _ignore_progress(_payload: dict[str, Any]) -> None:
    return None


@dataclass(frozen=True, slots=True)
class SummaryRuntime:
    """All mutable-world dependencies required by one summary transaction."""

    context_window: int
    chars_per_token: float
    compact_max_tokens: int
    lane_max_output_tokens: int | None
    continuation_overhead_tokens: int
    complete: SummaryCompletion
    stop_retrying: Callable[[BaseException, int], bool]
    is_context_overflow: Callable[[BaseException], bool]
    check_cancelled: Callable[[], None]
    backoff_or_cancelled: Callable[[float], None]
    on_progress: Callable[[dict[str, Any]], None] = _ignore_progress
    max_retries: int = 3
    retry_base_delay: float = 1.0


class CompactionEngine:
    """Stateless recursive conversation summarizer."""

    SUMMARY_SAFETY_MARGIN = 0.05
    SUMMARY_BUDGET_FRACTION = 0.75
    MAX_SUMMARY_DEPTH = 5
    MIN_SUMMARY_BUDGET_CHARS = 2000
    MIN_SUMMARY_OUTPUT_TOKENS = 512
    MIN_CARRY_BUDGET_CHARS = 2000

    COMPACT_OUTPUT_FORMAT = (
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
    COMPACTOR_SYSTEM_PROMPT = (
        "# Conversation Compactor\n\n"
        "Your output REPLACES the conversation history — the assistant "
        "will continue from your summary with no access to the original messages.\n\n"
        + COMPACT_OUTPUT_FORMAT
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
    COMPACTOR_MERGE_SYSTEM_PROMPT = (
        "# Summary Merger\n\n"
        "You are given several partial summaries of ONE conversation, produced by "
        "compacting consecutive slices in order. Merge these partial summaries into a "
        "single summary that REPLACES the conversation history — the assistant will "
        "continue from your merged summary with no access to the originals.\n\n"
        + COMPACT_OUTPUT_FORMAT
        + "2. **Merge rules:**\n"
        "   - Preserve every distinct decision, file, identifier, and open task across "
        "all partials; later partials reflect more recent state, so on conflict prefer "
        "the later one.\n"
        "   - Deduplicate: fold repeated items into one, keeping the most specific.\n"
        "   - Preserve exact paths, identifiers, and numbers — never paraphrase these.\n"
        "   - Be dense; this is a summary, not a transcript."
    )
    COMPACT_USER_PREFIX = "Compact the following conversation:\n\n"

    @staticmethod
    def summary_tool_names(messages: Sequence[dict[str, Any]]) -> dict[str, str]:
        """Map tool-call IDs over the full selection, not one packed batch."""

        names: dict[str, str] = {}
        for message in messages:
            for tool_call in message.get("tool_calls", []):
                call_id = tool_call.get("id", "")
                name = tool_call.get("function", {}).get("name", "unknown")
                if call_id:
                    names[call_id] = name
        return names

    @staticmethod
    def format_message_for_summary(
        message: dict[str, Any], tool_names: dict[str, str]
    ) -> str | None:
        role = message["role"].upper()
        content = message.get("content") or ""
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part["text"])
                elif part.get("type") in ("image_url", "image"):
                    text_parts.append("[image]")
            content = " ".join(text_parts)
        if message.get("tool_calls"):
            calls = []
            for tool_call in message["tool_calls"]:
                name = tool_call.get("function", {}).get("name", "?")
                arguments = tool_call.get("function", {}).get("arguments", "")
                calls.append(f"{name}({arguments})")
            content += "\n[Called: " + ", ".join(calls) + "]"
        if role == "TOOL":
            call_id = message.get("tool_call_id", "")
            role = f"TOOL[{tool_names.get(call_id, 'tool')}]"
        if not content:
            return None
        if len(content) > 2000:
            content = content[:1000] + "\n...[truncated]...\n" + content[-500:]
        return f"{role}: {content}"

    def summary_blocks(self, messages: Sequence[dict[str, Any]]) -> list[str]:
        tool_names = self.summary_tool_names(messages)
        return [
            line
            for message in messages
            if (line := self.format_message_for_summary(message, tool_names)) is not None
        ]

    def format_messages_for_summary(self, messages: Sequence[dict[str, Any]]) -> str:
        return "\n\n".join(self.summary_blocks(messages))

    def summary_output_tokens(self, runtime: SummaryRuntime) -> int:
        """Reserve summary output while leaving at least half the window for input.

        ``compact_max_tokens`` may itself equal a small model's entire window.
        Applying only that setting and the lane output cap would leave no room
        for the history being summarized.
        """

        hard_cap = (
            min(runtime.compact_max_tokens, runtime.lane_max_output_tokens)
            if runtime.lane_max_output_tokens
            else runtime.compact_max_tokens
        )
        window_cap = max(self.MIN_SUMMARY_OUTPUT_TOKENS, runtime.context_window // 2)
        return min(hard_cap, window_cap)

    def carry_budget_chars(self, runtime: SummaryRuntime, carries: int = 1) -> int:
        """Return one verbatim carry's budget after accounting for its siblings.

        Foreground compaction can carry a wind-down, the last user request, and
        coordinator handles at once; task compaction may carry its wind-down.
        Dividing the common spare prevents independently sized carries from
        stacking past the post-compaction window. The floor deliberately wins
        on pathological tiny windows so some exact state survives and the
        provider-overflow backstop can make the final ruling.
        """

        reserve = self.summary_output_tokens(runtime)
        margin = int(runtime.context_window * self.SUMMARY_SAFETY_MARGIN)
        spare = max(
            0,
            runtime.context_window - reserve - margin - runtime.continuation_overhead_tokens,
        )
        budget_tokens = min(runtime.context_window // 4, spare // max(1, carries))
        return max(self.MIN_CARRY_BUDGET_CHARS, int(budget_tokens * runtime.chars_per_token))

    def summary_input_budget_chars(self, runtime: SummaryRuntime) -> int:
        """Bound one summary call's formatted input in calibrated characters.

        Reserve output, the fixed compactor prompt, and a safety margin, then
        derate the remainder because a reactive path may still have the default
        optimistic chars/token ratio. The ordinary minimum is capped at the
        true remaining capacity; flooring beyond it would manufacture the very
        summary-call overflow this budget prevents.
        """

        output_reserve = self.summary_output_tokens(runtime)
        prompt_chars = len(self.COMPACTOR_SYSTEM_PROMPT) + len(self.COMPACT_USER_PREFIX)
        prompt_tokens = int(prompt_chars / runtime.chars_per_token)
        safety = int(runtime.context_window * self.SUMMARY_SAFETY_MARGIN)
        input_tokens = runtime.context_window - output_reserve - prompt_tokens - safety
        budget_tokens = max(0, int(input_tokens * self.SUMMARY_BUDGET_FRACTION))
        budget_chars = max(
            self.MIN_SUMMARY_BUDGET_CHARS,
            int(budget_tokens * runtime.chars_per_token),
        )
        return min(budget_chars, max(0, int(input_tokens * runtime.chars_per_token)))

    @staticmethod
    def truncate_block(block: str, budget: int) -> str:
        """Fit one oversized block head+tail around an honest size marker."""

        if len(block) <= budget:
            return block
        marker = f"\n…[truncated — {len(block):,} chars total]…\n"
        if budget <= len(marker):
            return block[:budget]
        keep = budget - len(marker)
        head = (keep * 2) // 3
        tail = keep - head
        return block[:head] + marker + block[-tail:] if tail else block[:head] + marker

    def pack_blocks(self, blocks: Sequence[str], budget_chars: int) -> list[list[str]]:
        """Greedily pack ordered blocks without drops or oversized batches.

        ``current_len`` exactly tracks the joined size including separators.
        A block that cannot fit alone is the sole lossy case and is truncated
        explicitly before being placed in its own batch.
        """

        budget = max(1, budget_chars)
        separator_len = len("\n\n")
        batches: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for block in blocks:
            if len(block) > budget:
                if current:
                    batches.append(current)
                    current = []
                    current_len = 0
                batches.append([self.truncate_block(block, budget)])
                continue
            added = len(block) + (separator_len if current else 0)
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

    def summarize_messages(
        self,
        messages: Sequence[dict[str, Any]],
        runtime: SummaryRuntime,
    ) -> SummaryResult:
        blocks = self.summary_blocks(messages)
        if not blocks:
            raise CompactionIrreducibleError
        return self.summarize_blocks(blocks, runtime)

    def summarize_once(
        self,
        system_prompt: str,
        body: str,
        runtime: SummaryRuntime,
    ) -> SummaryResult:
        """Run one complete-or-error summary call with cancellable retries.

        Deterministic context overflow escapes immediately for subdivision by
        :meth:`summarize_batch`; other retryable failures report their backoff.
        The lifecycle owner's cancellation check runs before classification so
        a transport closed by Stop is never mislabeled as a summary failure.
        """

        result: ModelTurnResult | None = None
        for attempt in range(runtime.max_retries + 1):
            try:
                result = runtime.complete(
                    system_prompt,
                    self.COMPACT_USER_PREFIX + body,
                    self.summary_output_tokens(runtime),
                )
                break
            except Exception as error:
                runtime.check_cancelled()
                if runtime.stop_retrying(error, attempt):
                    raise
                delay = runtime.retry_base_delay * (2**attempt)
                runtime.on_progress(
                    {
                        "phase": "progress",
                        "retry_in": delay,
                        "error": type(error).__name__,
                    }
                )
                runtime.backoff_or_cancelled(delay)
        if result is None:
            raise RuntimeError("summary retry ladder exhausted without a result")
        summary = (result.content or "").strip()
        if result.finish_reason == "length":
            runtime.on_progress({"phase": "progress", "warning": "summary_truncated"})
        return SummaryResult(
            text=summary,
            producer=result.producer,
            provenance=getattr(result, "provenance", TurnProvenance()),
        )

    def summarize_blocks(
        self,
        blocks: Sequence[str],
        runtime: SummaryRuntime,
        *,
        depth: int = 0,
    ) -> SummaryResult:
        """Summarize ordered blocks through packed leaves and recursive merges.

        The recursion ceiling is checked before the single-batch base case so
        it also bounds overflow-driven split/merge recursion. A block-count
        progress guard would be incorrect: binary subdivision can legitimately
        leave one summary per input block before the next merge shrinks them.
        """

        system_prompt = (
            self.COMPACTOR_SYSTEM_PROMPT if depth == 0 else self.COMPACTOR_MERGE_SYSTEM_PROMPT
        )
        if depth >= self.MAX_SUMMARY_DEPTH:
            raise CompactionIrreducibleError
        batches = self.pack_blocks(blocks, self.summary_input_budget_chars(runtime))
        if len(batches) == 1:
            return self.summarize_batch(system_prompt, batches[0], depth, runtime)
        total = len(batches)
        summaries: list[str] = []
        for part, batch in enumerate(batches, start=1):
            runtime.on_progress(
                {
                    "phase": "progress",
                    "part": part,
                    "total": total,
                    "depth": depth,
                }
            )
            partial = self.summarize_batch(system_prompt, batch, depth, runtime)
            summaries.append(partial.text)
        return self.summarize_blocks(summaries, runtime, depth=depth + 1)

    def summarize_batch(
        self,
        system_prompt: str,
        batch: Sequence[str],
        depth: int,
        runtime: SummaryRuntime,
    ) -> SummaryResult:
        """Summarize one batch, recursively recovering from real overflows.

        A multi-block overflow splits in half, summarizes both halves, and
        merges them. A lone block progressively halves its head+tail input down
        to the configured floor; if even that does not fit, the operation is
        irreducible rather than silently dropping the block or fabricating a
        summary.
        """

        runtime.check_cancelled()
        try:
            return self.summarize_once(system_prompt, "\n\n".join(batch), runtime)
        except Exception as error:
            if not runtime.is_context_overflow(error):
                raise
            if len(batch) > 1:
                midpoint = len(batch) // 2
                left = self.summarize_batch(system_prompt, batch[:midpoint], depth, runtime)
                right = self.summarize_batch(system_prompt, batch[midpoint:], depth, runtime)
                return self.summarize_blocks(
                    [left.text, right.text],
                    runtime,
                    depth=depth + 1,
                )
            budget = max(self.MIN_SUMMARY_BUDGET_CHARS, len(batch[0]) // 2)
            while True:
                try:
                    return self.summarize_once(
                        system_prompt,
                        self.truncate_block(batch[0], budget),
                        runtime,
                    )
                except Exception as retry_error:
                    if not runtime.is_context_overflow(retry_error):
                        raise
                    if budget <= self.MIN_SUMMARY_BUDGET_CHARS:
                        raise CompactionIrreducibleError from retry_error
                    budget = max(self.MIN_SUMMARY_BUDGET_CHARS, budget // 2)
