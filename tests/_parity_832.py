"""#832 replay-parity harness: scenario table + runner.

The audit is controller determinism: with the plant's chunk sequence held
fixed, the streaming phase must produce an identical UI event sequence
and an identical committed message — modulo the RULED behavior changes
restated in full on the transforms in ``test_832_parity.py``.  This
module is the shared half: the scenario scripts (one row per
chunk-field→UI translation the consumer performs) and the runner that
drives one through the streaming seam, recording everything the turn
observably produced.

Baselines are captured from the PRE-FOLD path (``UPDATE_832_PARITY=1``,
run at a tree where ``session.py`` is byte-identical to pre-fold main)
into ``tests/data/parity_832/``.  The runner adapts to EITHER world by
signature, so a recapture at an old tree records real old-world
behavior, and capture mode refuses to write a record whose failure is
the harness's own call shape.  Assert mode replays the same scripts
through the current tree and compares against the baseline, applying the
ruled transforms; a mismatch outside a ruled transform is a regression.

The provider fake arms ``cancel_ref`` EAGERLY (a closeable sentinel
appended inside ``create_streaming``, before the iterator is returned),
mirroring every real adapter: the wrapper classifies
creation-vs-midstream failures by that arming, so a fake that skipped it
would exercise only the creation arm.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any

from tests._session_helpers import RecordingUI, make_session, scripted_provider
from turnstone.core.providers._protocol import StreamChunk, ToolCallDelta, UsageInfo
from turnstone.core.trajectory import Turn

FIXTURE_DIR = Path(__file__).parent / "data" / "parity_832"
UPDATE = os.environ.get("UPDATE_832_PARITY") == "1"


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

    The record is everything the streaming phase observably produced: the
    ordered UI events, the committed-message projection, the mid-stream
    usage slot, and the exception class if the seam raised.  Deliberately
    seam-level at ``_stream_response`` — full ``send()`` scenarios ride
    the ported ladder suites instead.

    Signature-adaptive so ``UPDATE_832_PARITY=1`` at a PRE-fold tree
    records real old-world behavior: the pre-fold seam was
    ``_stream_response(msgs, my_generation) -> dict``, the post-fold one
    is ``_stream_response(my_generation) -> ModelTurnResult`` (wire
    prepared inside).  A harness-shape failure must never be recorded as
    behavior — ``write_fixture`` refuses one.
    """
    ui = RecordingUI()
    session = make_session(ui=ui)
    # Zero the ladder backoff: a scenario that reaches the mid-stream
    # re-issue ladder (no_finish_clean_exhaust) must not sleep real
    # exponential delays in a unit run.  The retry-notice transform in
    # test_832_parity hardcodes the matching "0s" wording.
    session._RETRY_BASE_DELAY = 0
    session._provider = scripted_provider(SCENARIOS[name])

    pre_fold = "msgs" in inspect.signature(type(session)._stream_response).parameters
    record: dict[str, Any] = {"scenario": name}
    try:
        if pre_fold:
            # Splatted: the pre-fold seam took (msgs, my_generation), and a
            # literal two-argument call reads as an arity error against the
            # signature this tree actually has.
            pre_fold_args: tuple[Any, ...] = ([{"role": "user", "content": "hi"}], 0)
            msg = session._stream_response(*pre_fold_args)
            msg.pop("_wire_msgs", None)
            record["result"] = {
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls"),
                "provider_content": msg.get("_provider_content"),
            }
        else:
            session.messages.append(Turn.user("hi"))
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
    # A TypeError before ANY UI event is the harness's own call-shape
    # failure (run_scenario's signature adapter no longer matches this
    # tree's seam), not old-world behavior — refuse to destroy the
    # baseline with it.
    if record.get("raised") == "TypeError" and not record.get("ui_events"):
        raise AssertionError(
            f"parity capture for {name!r} died calling the seam (TypeError before "
            f"any UI event) — fix run_scenario's signature adapter; do not record"
        )
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fixture_path(name).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
