"""#832 replay-parity harness: scenario table + runner.

The fold's acceptance is a controller-determinism audit: with the plant's
chunk sequence held fixed, the streaming phase must produce an identical
UI event sequence and an identical committed message — modulo the deltas
the design's D12 table rules (docs/design/832-main-loop-model-turn.md,
local).  This module is the shared half: the scenario scripts (drawn from
the dataflow map's V11 chunk-field→UI grid) and the runner that drives one
scenario through the session's streaming seam, recording everything the
turn observably produced.

Baselines are captured from the PRE-FOLD path (``UPDATE_832_PARITY=1``,
run at a tree where ``session.py`` is byte-identical to main — the fixture
commit's history proves it) into ``tests/data/parity_832/``.  The assert
mode replays the same scripts through the current tree and compares
against the baseline, applying the D12 transforms where the design ruled
a behavior change.  A mismatch outside a ruled transform is a fold
regression.

The provider fake arms ``cancel_ref`` EAGERLY (a closeable sentinel
appended inside ``create_streaming``, before the iterator is returned),
mirroring every real adapter — the post-fold wrapper classifies
creation-vs-midstream failures by that arming, so a fake that skipped it
would exercise only the creation arm (design gap-check G8).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from tests._session_helpers import RecordingUI, make_session
from turnstone.core.model_turn import ModelTurnResult
from turnstone.core.providers._protocol import (
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
    UsageInfo,
)
from turnstone.core.trajectory import ProviderNative, ToolCall, Turn


def make_result(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: UsageInfo | None = None,
    native_blocks: list[dict[str, Any]] | None = None,
    producer: str = "openai-compatible",
    wire_msgs: list[dict[str, Any]] | None = None,
) -> ModelTurnResult:
    """A ``ModelTurnResult`` shaped like the streaming wrapper's return —
    triage recipe R1's patched-result form, for tests that only need "a
    turn happened" and patch ``_stream_response`` wholesale.  The Turn and
    the ``tool_calls`` mirror are built from the same dicts, preserving
    the #825 pairing invariant fakes must not break."""
    calls = list(tool_calls or [])
    tc_tuple = tuple(
        ToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", ""),
        )
        for tc in calls
    )
    native = (
        ProviderNative(producer=producer, blocks=tuple(native_blocks)) if native_blocks else None
    )
    return ModelTurnResult(
        turn=Turn.assistant(content, tool_calls=tc_tuple, native=native),
        finish_reason=finish_reason,
        usage=usage,
        tool_calls=calls,
        wire_msgs=wire_msgs,
        producer=producer,
    )


FIXTURE_DIR = Path(__file__).parent / "data" / "parity_832"
UPDATE = os.environ.get("UPDATE_832_PARITY") == "1"


class ArmedHandle:
    """Closeable sentinel standing in for the SDK stream handle."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def arm_session(
    session: Any,
    *streams: Any,
    retryable: frozenset[str] = frozenset({"IncompleteStreamError"}),
    name: str = "openai-compatible",
) -> MagicMock:
    """Install a sequential multi-turn armed provider fake on *session*.

    Each ``create_streaming`` call serves the next element of *streams*:
    an iterable/generator is armed (a closeable sentinel appended to
    ``cancel_ref`` — the eager append every real adapter performs, which
    the fold's creation-vs-midstream classifier keys on) and returned to
    be consumed once; an EXCEPTION instance is raised at create time
    WITHOUT arming — a creation-phase failure the per-lane ladder owns.
    Calls beyond the script fail loudly (the pre-fold lax consumer used
    to absorb an exhausted iterator as a silent empty turn; the strict
    finish gate rejects that now, so an under-scripted test must say so).

    Title generation is latched off — with a provider-LEVEL fake the
    best-effort title lane would otherwise consume the first script
    before the main loop ran.
    """
    session._title_generated = True
    provider = MagicMock()
    provider.provider_name = name
    provider.get_capabilities.return_value = ModelCapabilities()
    provider.retryable_error_names = retryable
    provider._armed_handle = MagicMock()
    remaining = list(streams)

    def _create(**kwargs: Any):
        assert remaining, "arm_session: script exhausted — send looped for more turns than scripted"
        nxt = remaining.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        ref = kwargs.get("cancel_ref")
        if ref is not None:
            ref.append(provider._armed_handle)
        return iter(nxt) if not hasattr(nxt, "__next__") else nxt

    provider.create_streaming = MagicMock(side_effect=_create)
    session._provider = provider
    return provider


def scripted_provider(chunks: list[StreamChunk]) -> MagicMock:
    """Provider fake replaying *chunks*, arming ``cancel_ref`` eagerly.

    Assign to ``session._provider`` (never mutate a resolved provider —
    the create_provider singleton rule in ``_session_helpers``).  Each
    call returns a FRESH iterator over the same script so ladder tests
    re-drive it; the armed handle is appended per call, matching the
    one-handle-per-create behavior of every real adapter.
    """
    provider = MagicMock()
    provider.provider_name = "openai-compatible"
    provider.get_capabilities.return_value = ModelCapabilities()
    provider.retryable_error_names = frozenset({"IncompleteStreamError"})

    def _create(**kwargs: Any):
        ref = kwargs.get("cancel_ref")
        if ref is not None:
            ref.append(ArmedHandle())
        return iter(chunks)

    provider.create_streaming = MagicMock(side_effect=_create)
    return provider


def _tc(index: int, call_id: str, name: str = "", args: str = "") -> ToolCallDelta:
    return ToolCallDelta(index=index, id=call_id, name=name, arguments_delta=args)


_USAGE_A = UsageInfo(prompt_tokens=11, completion_tokens=0, total_tokens=11)
_USAGE_B = UsageInfo(prompt_tokens=11, completion_tokens=7, total_tokens=18)

# Scenario table — the V11 grid, one script per row.  Scripts are chunk
# LISTS; the runner re-iterates a fresh iterator per attempt.
SCENARIOS: dict[str, list[StreamChunk]] = {
    "content_only": [
        StreamChunk(content_delta="Hello "),
        StreamChunk(content_delta="world."),
        StreamChunk(finish_reason="stop", usage=_USAGE_B),
    ],
    "reasoning_then_content": [
        StreamChunk(reasoning_delta="think a", usage=_USAGE_A),
        StreamChunk(reasoning_delta=" think b"),
        StreamChunk(content_delta="Answer."),
        StreamChunk(finish_reason="stop", usage=_USAGE_B),
    ],
    "tools_simple": [
        StreamChunk(content_delta="Calling."),
        StreamChunk(tool_call_deltas=[_tc(0, "call_1", "get_weather", '{"city": ')]),
        StreamChunk(tool_call_deltas=[_tc(0, "", "", '"Paris"}')]),
        StreamChunk(finish_reason="tool_calls", usage=_USAGE_B),
    ],
    "combined_content_tools_finish": [
        StreamChunk(content_delta="Before "),
        StreamChunk(
            content_delta="tools",
            tool_call_deltas=[_tc(0, "call_1", "get_weather", '{"city": "Nice"}')],
            finish_reason="tool_calls",
        ),
        StreamChunk(usage=_USAGE_B),
    ],
    "info_prefinish": [
        StreamChunk(info_delta="[Searching: pinniped taxonomy]"),
        StreamChunk(content_delta="Seals are pinnipeds."),
        StreamChunk(finish_reason="stop", usage=_USAGE_B),
    ],
    "info_postfinish_footer": [
        StreamChunk(content_delta="Answer with sources."),
        StreamChunk(finish_reason="stop", usage=_USAGE_B),
        StreamChunk(info_delta="Sources:\n- example.com/page"),
    ],
    "think_tags_split_across_chunks": [
        StreamChunk(content_delta="<thi"),
        StreamChunk(content_delta="nk>plan</think>\n\nAnswer"),
        StreamChunk(finish_reason="stop", usage=_USAGE_B),
    ],
    "blank_id_tools": [
        StreamChunk(tool_call_deltas=[_tc(0, "", "get_weather", '{"city": "Oslo"}')]),
        StreamChunk(finish_reason="tool_calls", usage=_USAGE_B),
    ],
    "length_with_tools": [
        StreamChunk(content_delta="Partial answer"),
        StreamChunk(tool_call_deltas=[_tc(0, "call_1", "get_weather", '{"city": "Par')]),
        StreamChunk(finish_reason="length", usage=_USAGE_B),
    ],
    "content_filter": [
        StreamChunk(content_delta="Redac"),
        StreamChunk(finish_reason="content_filter", usage=_USAGE_B),
    ],
    "no_finish_clean_exhaust": [
        StreamChunk(content_delta="Half an ans"),
        StreamChunk(usage=_USAGE_A),
    ],
    "finish_only_no_content": [
        StreamChunk(finish_reason="stop", usage=_USAGE_B),
    ],
    "provider_blocks_on_terminal": [
        StreamChunk(content_delta="Blocked."),
        StreamChunk(
            finish_reason="stop",
            usage=_USAGE_B,
            provider_blocks=[{"type": "reasoning_text", "text": "captured"}],
        ),
    ],
}


_SYNTH_ID = re.compile(r"^call_[0-9a-f]{32}$")


def _mask_synth_ids(record: dict[str, Any]) -> dict[str, Any]:
    """Replace uuid-backfilled tool-call ids with stable placeholders.

    The blank-id repair mints ``call_<uuid4hex>`` per run — real
    nondeterminism inside the seam, but not behavior: mask ONLY that exact
    shape (never a scripted provider id) with an index-stable token so
    captures compare across runs.  Applied to the committed projection;
    UI events never carry call ids in this harness.
    """
    result = record.get("result")
    if not result:
        return record
    for i, tc in enumerate(result.get("tool_calls") or []):
        if _SYNTH_ID.match(tc.get("id", "")):
            tc["id"] = f"synth-id-{i}"
    for i, block in enumerate(result.get("provider_content") or []):
        if isinstance(block, dict) and _SYNTH_ID.match(str(block.get("id", ""))):
            block["id"] = f"synth-id-{i}"
    return record


def run_scenario(name: str) -> dict[str, Any]:
    """Drive one scenario through the streaming seam; return the record.

    The record is everything the streaming phase observably produced:
    the ordered UI events, the committed-message projection, the
    mid-stream usage slot, and the exception class if the seam raised.
    Deliberately seam-level (the ``_stream_response`` boundary pre-fold,
    its wrapper successor post-fold) — full ``send()`` scenarios ride the
    ported ladder suites instead.
    """
    ui = RecordingUI()
    session = make_session(ui=ui)
    session._provider = scripted_provider(SCENARIOS[name])
    session.messages.append(Turn.user("hi"))

    record: dict[str, Any] = {"scenario": name}
    try:
        result = session._stream_response(0)
        record["result"] = {
            "content": result.content,
            "tool_calls": result.tool_calls or None,
            "provider_content": (
                [dict(b) for b in result.turn.native.blocks] if result.turn.native else None
            ),
        }
        record["raised"] = None
    except BaseException as exc:  # noqa: BLE001 — the record IS the observation
        record["result"] = None
        record["raised"] = type(exc).__name__
    record["ui_events"] = [[k, d] for k, d in ui.events]
    record["last_usage"] = session._last_usage
    record["cancelled_partial"] = session._cancelled_partial_msg
    return _mask_synth_ids(record)


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / f"{name}.json"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads(fixture_path(name).read_text())


def write_fixture(name: str, record: dict[str, Any]) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fixture_path(name).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
