"""The provider-native lane's replay cost (issue #1188, with the #1190 estimate log).

A server-side search turn appends result blocks the chars-per-token measure cannot see.  The
producing call records the provider's own count of what it appended on the assistant turn
(``native_tokens``), and every estimator charges it like the fixed image charge, only toward the
provider that replays the lane.  A request that carries such a charge is not a calibration
sample: the count is the provider's, not this process's, so the ratio keeps its value and stays
honest on every later turn of the workstream.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import (
    log_has_field,
    make_registered_session,
    make_result,
    make_session,
    provider_shell,
    replace_session_lane,
    scripted_provider,
)

# The shared helper's ``NullUI`` subclasses ``SessionUIBase``, which leaves
# ``on_state_change`` to subclasses, so it cannot drive ``send()``; the session
# suite's duck-typed stub implements the whole send-path surface.
from tests.test_session import NullUI as SendPathUI
from turnstone.core.compaction import (
    SummaryResult,
    accepted_turn_tokens,
)
from turnstone.core.model_turn import (
    ModelContextLimitError,
    ModelLane,
    ModelTurnResult,
    _ingest_completion,
    apply_capability_overrides,
    require_lane_capabilities,
)
from turnstone.core.providers import (
    CompletionResult,
    ModelCapabilities,
    StreamChunk,
    UsageInfo,
    replay_family,
)
from turnstone.core.session import ChatSession
from turnstone.core.storage import get_storage
from turnstone.core.storage._utils import _fork_turn_insert_row
from turnstone.core.trajectory import (
    NATIVE_TOKENS_CAP,
    NATIVE_TOKENS_META_KEY,
    PROVENANCE_META_KEY,
    ProviderNative,
    Turn,
    TurnProvenance,
    assistant_meta_envelope,
    dicts_from_turns,
    native_tokens_from,
    turn_from_dict,
    turn_to_dict,
    turns_from_dicts,
)

SEARCH_BLOCKS: list[dict[str, Any]] = [
    {
        "type": "server_tool_use",
        "id": "srvtoolu_1",
        "name": "web_search",
        "input": {"query": "spot price"},
    },
    {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": [
            {
                "type": "web_search_result",
                "url": "https://example.com/prices",
                "title": "prices",
                "encrypted_content": "b64" * 40,
            }
        ],
    },
    {"type": "text", "text": "found it"},
]
# The live shape (2026-09-19): the request carried 23,881 tokens, the server appended
# 29,910 that the next request replays, the response text cost 772.
SERVED, APPENDED, COMPLETION = 23_881, 29_910, 772
SEARCH_USAGE = UsageInfo(
    prompt_tokens=77_668,
    completion_tokens=COMPLETION,
    total_tokens=77_668 + COMPLETION,
    cache_read_tokens=47_622,
    served_prompt_tokens=SERVED,
    appended_prompt_tokens=APPENDED,
    prompt_tokens_cumulative=True,
)


def _search_turn(producer: str = "anthropic", native_tokens: int = APPENDED) -> Turn:
    turn = Turn.assistant(
        "found it", native=ProviderNative(producer=producer, blocks=tuple(SEARCH_BLOCKS))
    )
    turn.meta.extra[NATIVE_TOKENS_META_KEY] = native_tokens
    return turn


def _lane(
    provider_name: str = "anthropic", *, capabilities: ModelCapabilities | None = None
) -> ModelLane:
    return ModelLane(
        provider=provider_shell(provider_name),
        client=MagicMock(),
        model="claude-test",
        alias="fable",
        capabilities=ModelCapabilities() if capabilities is None else capabilities,
    )


def _ingest(result: CompletionResult, lane: ModelLane, *, cfg: Any = None) -> ModelTurnResult:
    return _ingest_completion(
        result,
        lane,
        mint=None,
        wire_id_map=None,
        acting_principal_id="user-a",
        cfg=cfg,
        wire_msgs=[],
        request_metrics=None,
        tools=None,
    )


# --------------------------------------------------------------------------- #
# The turn records the cost; the dict bridge and storage carry it.
# --------------------------------------------------------------------------- #


def test_completion_builder_records_the_appended_cost_on_the_turn() -> None:
    result = _ingest(
        CompletionResult(content="found it", usage=SEARCH_USAGE, provider_blocks=SEARCH_BLOCKS),
        _lane(),
    )

    assert result.turn.native is not None
    assert result.turn.native.producer == "anthropic"
    assert result.turn.native_tokens == APPENDED
    assert result.turn.meta.extra[NATIVE_TOKENS_META_KEY] == APPENDED


def test_completion_builder_records_nothing_without_an_appended_cost_or_a_lane() -> None:
    plain_usage = UsageInfo(prompt_tokens=100, completion_tokens=5, total_tokens=105)
    plain = _ingest(
        CompletionResult(content="ok", usage=plain_usage, provider_blocks=SEARCH_BLOCKS), _lane()
    )
    assert plain.turn.native is not None
    assert NATIVE_TOKENS_META_KEY not in plain.turn.meta.extra
    assert plain.turn.native_tokens == 0

    # An appended count with no lane surviving to be replayed is not charged.
    laneless = _ingest(CompletionResult(content="ok", usage=SEARCH_USAGE), _lane())
    assert laneless.turn.native is None
    assert NATIVE_TOKENS_META_KEY not in laneless.turn.meta.extra

    # A count no context window could hold is a broken usage report, not a cost.
    absurd = UsageInfo(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        served_prompt_tokens=10,
        appended_prompt_tokens=NATIVE_TOKENS_CAP + 1,
        prompt_tokens_cumulative=True,
    )
    unbounded = _ingest(
        CompletionResult(content="ok", usage=absurd, provider_blocks=SEARCH_BLOCKS), _lane()
    )
    assert unbounded.turn.native is not None
    assert NATIVE_TOKENS_META_KEY not in unbounded.turn.meta.extra


def test_completion_builder_records_the_raw_count_in_either_replay_posture() -> None:
    """The count is the provider's own, taken as reported.  The thinking and text the model
    emitted before the search, which the provider fed back into its own next pass, are neither
    discounted nor added, and the reasoning-replay posture never enters: a recorded turn stays
    right when the operator toggles the setting or moves to a same-provider alias with the other
    one.  The accepted turn is charged this count beside its completion count; the fed-back
    output sits in both, so the turn is over-charged by that share, bounded by its completion
    count.  A count at the lane's window is kept whole: the refusal above it is the only bound."""
    early = {"type": "thinking", "thinking": "a" * 4_000, "signature": "s1"}
    preface = {"type": "text", "text": "p" * 40}
    call_block, result_block, text = SEARCH_BLOCKS
    late = {"type": "thinking", "thinking": "c" * 40_000, "signature": "s3"}
    blocks = [early, preface, call_block, result_block, late, text]

    replay_off = _ingest(
        CompletionResult(content="found it", usage=SEARCH_USAGE, provider_blocks=blocks), _lane()
    )
    assert replay_off.turn.native_tokens == APPENDED

    replay_on = _ingest(
        CompletionResult(content="found it", usage=SEARCH_USAGE, provider_blocks=blocks),
        _lane(capabilities=ModelCapabilities(supports_reasoning_replay=True)),
        cfg=SimpleNamespace(replay_reasoning_to_model=True),
    )
    assert replay_on.turn.native_tokens == APPENDED

    at_window = UsageInfo(
        prompt_tokens=205_000,
        completion_tokens=5,
        total_tokens=205_005,
        served_prompt_tokens=5_000,
        appended_prompt_tokens=200_000,
        prompt_tokens_cumulative=True,
    )
    kept = _ingest(
        CompletionResult(content="found it", usage=at_window, provider_blocks=blocks),
        _lane(capabilities=ModelCapabilities(context_window=200_000)),
    )
    assert kept.turn.native_tokens == 200_000


def test_completion_builder_refuses_a_count_above_the_lane_window() -> None:
    """No response appends more than the window the next request uses: a larger count is a
    broken report and is not recorded, whatever the global decoder cap allows."""
    oversized = UsageInfo(
        prompt_tokens=300_000,
        completion_tokens=5,
        total_tokens=300_005,
        served_prompt_tokens=50_000,
        appended_prompt_tokens=250_000,
        prompt_tokens_cumulative=True,
    )
    small_window = _ingest(
        CompletionResult(content="ok", usage=oversized, provider_blocks=SEARCH_BLOCKS),
        _lane(capabilities=ModelCapabilities(context_window=200_000)),
    )
    assert NATIVE_TOKENS_META_KEY not in small_window.turn.meta.extra

    large_window = _ingest(
        CompletionResult(content="ok", usage=oversized, provider_blocks=SEARCH_BLOCKS),
        _lane(capabilities=ModelCapabilities(context_window=1_000_000)),
    )
    assert large_window.turn.native_tokens == 250_000

    # A plausible count is recorded whole, however full the request already was: the
    # capability table's window is not the window the session serves, so it bounds
    # only what no window could hold.
    crowded = UsageInfo(
        prompt_tokens=210_000,
        completion_tokens=5,
        total_tokens=210_005,
        served_prompt_tokens=150_000,
        appended_prompt_tokens=60_000,
        prompt_tokens_cumulative=True,
    )
    recorded = _ingest(
        CompletionResult(content="ok", usage=crowded, provider_blocks=SEARCH_BLOCKS),
        _lane(capabilities=ModelCapabilities(context_window=200_000)),
    )
    assert recorded.turn.native_tokens == 60_000

    # The operator's model-definition row can declare a larger window than the table
    # knows (every unlisted model id defaults to 200k there): the larger of the two
    # is the bound, so a legitimate count on such a lane is kept.
    rescued = _ingest(
        CompletionResult(content="ok", usage=oversized, provider_blocks=SEARCH_BLOCKS),
        _lane(capabilities=ModelCapabilities(context_window=200_000)),
        cfg=SimpleNamespace(context_window=1_000_000, replay_reasoning_to_model=False),
    )
    assert rescued.turn.native_tokens == 250_000


def test_completion_builder_logs_a_refused_count(caplog: pytest.LogCaptureFixture) -> None:
    """A refusal is the fallback to the old under-count, so it is never silent: the
    operator can tell a broken provider from a window mismatch."""
    oversized = UsageInfo(
        prompt_tokens=300_000,
        completion_tokens=5,
        total_tokens=300_005,
        served_prompt_tokens=50_000,
        appended_prompt_tokens=250_000,
        prompt_tokens_cumulative=True,
    )
    with caplog.at_level(logging.WARNING):
        _ingest(
            CompletionResult(content="ok", usage=oversized, provider_blocks=SEARCH_BLOCKS),
            _lane(capabilities=ModelCapabilities(context_window=200_000)),
        )
    record = next(r for r in caplog.records if "native_lane.count_refused" in r.getMessage())
    assert log_has_field(record, "appended", 250_000)
    assert log_has_field(record, "context_window", 200_000)
    assert log_has_field(record, "alias", "fable")


def test_completion_builder_holds_the_lane_window_under_the_decoder_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator row can name a window above the decoder's cap; a count between the two is
    refused through the same logged warning, carrying the cap as the bound applied and the
    row's window beside it, instead of being dropped silently by the decoder."""
    between = UsageInfo(
        prompt_tokens=NATIVE_TOKENS_CAP + 60_000,
        completion_tokens=5,
        total_tokens=NATIVE_TOKENS_CAP + 60_005,
        served_prompt_tokens=50_000,
        appended_prompt_tokens=NATIVE_TOKENS_CAP + 10_000,
        prompt_tokens_cumulative=True,
    )
    row_window = NATIVE_TOKENS_CAP + 1_000_000
    with caplog.at_level(logging.WARNING):
        result = _ingest(
            CompletionResult(content="ok", usage=between, provider_blocks=SEARCH_BLOCKS),
            _lane(capabilities=ModelCapabilities(context_window=200_000)),
            cfg=SimpleNamespace(context_window=row_window, replay_reasoning_to_model=False),
        )
    assert NATIVE_TOKENS_META_KEY not in result.turn.meta.extra
    record = next(r for r in caplog.records if "native_lane.count_refused" in r.getMessage())
    assert log_has_field(record, "context_window", NATIVE_TOKENS_CAP)
    assert log_has_field(record, "lane_window", row_window)


def test_completion_builder_records_nothing_on_a_lane_with_no_known_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bound fails closed.  An operator capability override can zero the table's window
    (any field-named key is applied unvalidated), and a lane built without capabilities has
    none; with no model-row window either the lane's window is 0, and a count that no known
    window bounds is refused through the same warning, not recorded up to the decoder cap."""
    zeroed = apply_capability_overrides(ModelCapabilities(), {"context_window": 0})
    assert zeroed.context_window == 0
    with caplog.at_level(logging.WARNING):
        result = _ingest(
            CompletionResult(content="ok", usage=SEARCH_USAGE, provider_blocks=SEARCH_BLOCKS),
            _lane(capabilities=zeroed),
        )
    assert NATIVE_TOKENS_META_KEY not in result.turn.meta.extra
    record = next(r for r in caplog.records if "native_lane.count_refused" in r.getMessage())
    assert log_has_field(record, "context_window", 0)


def test_completion_builder_records_nothing_when_the_blank_id_collapse_drops_the_lane() -> None:
    """A client tool call with a blank id that cannot be paired collapses the lane to the
    synthesized reasoning block: the search blocks the appended count paid for are dropped and
    nothing of that lane is replayed, so no count is recorded.  The same completion with a
    pairable mirror keeps the search blocks and the count."""
    search_and_tool = [*SEARCH_BLOCKS, {"type": "tool_use", "id": "", "name": "bash", "input": {}}]
    blank_call = {"id": "", "type": "function", "function": {"name": "bash", "arguments": "{}"}}

    collapsed = _ingest(
        CompletionResult(
            content="ok",
            tool_calls=[blank_call, dict(blank_call)],
            usage=SEARCH_USAGE,
            provider_blocks=[dict(b) for b in search_and_tool],
            reasoning="thinking it over",
        ),
        _lane(),
    )
    assert collapsed.turn.native is not None
    assert [b["type"] for b in collapsed.turn.native.blocks] == ["reasoning_text"]
    assert NATIVE_TOKENS_META_KEY not in collapsed.turn.meta.extra
    assert collapsed.turn.native_tokens == 0

    paired = _ingest(
        CompletionResult(
            content="ok",
            tool_calls=[blank_call],
            usage=SEARCH_USAGE,
            provider_blocks=[dict(b) for b in search_and_tool],
            reasoning="thinking it over",
        ),
        _lane(),
    )
    assert paired.turn.native is not None
    assert any(b["type"] == "web_search_tool_result" for b in paired.turn.native.blocks)
    assert paired.turn.native_tokens == APPENDED


def test_turn_that_lost_its_lane_carries_no_cost() -> None:
    """A length-truncated reply drops its partial tool call and, with it, a native lane made
    only of that call; the recorded count must not outlive the lane, so nothing is persisted
    or charged for blocks that will never be replayed."""
    session = make_session()
    tool_call = {"id": "toolu_1", "type": "function", "function": {"name": "bash", "arguments": ""}}
    result = make_result(
        "partial",
        tool_calls=[tool_call],
        finish_reason="length",
        usage=SEARCH_USAGE,
        native_blocks=[{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}],
        producer="anthropic",
    )
    result.turn.meta.extra[NATIVE_TOKENS_META_KEY] = APPENDED

    finalized = session._finalize_stream_result(result)

    assert finalized.turn.native is None
    assert finalized.turn.native_tokens == 0
    assert NATIVE_TOKENS_META_KEY not in assistant_meta_envelope(finalized.turn)
    assert "_native_tokens" not in turn_to_dict(finalized.turn)


def test_dict_bridge_round_trips_the_cost_and_reads_it_leniently() -> None:
    turn = _search_turn()
    msg = turn_to_dict(turn)
    assert msg["_native_tokens"] == APPENDED
    assert msg["_producer"] == "anthropic"
    assert turn_from_dict(msg).native_tokens == APPENDED
    assert turn_from_dict(msg).meta.extra[NATIVE_TOKENS_META_KEY] == APPENDED

    # Nothing recorded, nothing projected.
    assert "_native_tokens" not in turn_to_dict(Turn.assistant("plain"))
    assert turn_from_dict({"role": "assistant", "content": "plain"}).native_tokens == 0

    # The one decoder also bounds the count: the largest plausible value passes,
    # one more is refused, so a hostile or broken report can neither persist nor
    # reach the estimators' arithmetic.
    assert native_tokens_from(NATIVE_TOKENS_CAP) == NATIVE_TOKENS_CAP
    assert native_tokens_from(NATIVE_TOKENS_CAP + 1) == 0
    assert native_tokens_from(10**309) == 0

    # Corrupt stored values degrade to zero rather than crash a consumer.
    for raw in ("29910", True, -1, 0, 2.5, None, {"n": 1}):
        assert native_tokens_from(raw) == 0
        corrupt = _search_turn()
        corrupt.meta.extra[NATIVE_TOKENS_META_KEY] = raw
        assert corrupt.native_tokens == 0
        assert "_native_tokens" not in turn_to_dict(corrupt)


def test_cost_survives_storage_fork_and_resume(
    storage_backend: Any, mock_openai_client: Any
) -> None:
    st = storage_backend
    ws = "ws-native-lane"
    st.register_workstream(ws, user_id="u1", title="t", kind="interactive")
    reopened = make_session(client=mock_openai_client, context_window=200_000, max_tokens=1_000)
    # The producing provider is this session's own, so a resume replays the lane.
    producer = reopened._model_binding.lane.provider.provider_name
    provenance = TurnProvenance(
        model_alias="fable",
        backend_model_id="claude-test",
        registry_generation=3,
        acting_principal_id="user-a",
    )
    st.save_message(ws, "user", "search for it")
    st.save_message(
        ws,
        "assistant",
        "found it",
        provider_data=json.dumps(SEARCH_BLOCKS),
        producer=producer,
        meta=json.dumps(
            {PROVENANCE_META_KEY: provenance.to_meta(), NATIVE_TOKENS_META_KEY: APPENDED}
        ),
    )

    loaded = st.load_message_turns(ws)
    assert loaded[1].native_tokens == APPENDED
    assert loaded[1].meta.extra[PROVENANCE_META_KEY] == provenance.to_meta()
    # An estimator input, not display metadata: kept out of the public envelope.
    assert "source_meta" not in loaded[1].meta.extra

    fork_row, _ids = _fork_turn_insert_row(loaded[1], "ws-fork", "2026-09-19T00:00:00")
    fork_meta = json.loads(fork_row["meta"])
    assert fork_meta[NATIVE_TOKENS_META_KEY] == APPENDED
    assert fork_meta[PROVENANCE_META_KEY] == provenance.to_meta()

    assert reopened.resume(ws) is True
    assert reopened.messages[1].native_tokens == APPENDED
    # The resumed estimate charges the lane before any provider count exists.
    assert reopened._msg_tokens[1] >= APPENDED
    assert reopened._msg_tokens[0] < 100
    assert reopened._estimated_prompt_tokens() >= APPENDED


# --------------------------------------------------------------------------- #
# The measure charges the cost like an image, only toward the replaying provider.
# --------------------------------------------------------------------------- #


def test_measure_charges_the_lane_only_toward_its_producer() -> None:
    msg = turn_to_dict(_search_turn())
    text_chars, fixed_tokens, doc_chars = ChatSession._msg_text_chars(msg, replay_producer=None)
    assert fixed_tokens == 0  # no replaying provider named: no charge
    assert doc_chars == 0
    assert text_chars == len("found it") + len("assistant")

    assert ChatSession._msg_text_chars(msg, replay_producer="anthropic")[1] == APPENDED
    assert ChatSession._msg_text_chars(msg, replay_producer="openai")[1] == 0
    assert ChatSession._msg_text_chars(_search_turn(), replay_producer="anthropic")[1] == APPENDED

    # The image charge rides the same bucket.
    image_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "see"},
            {"type": "image_url", "image_url": {"url": "d"}},
        ],
    }
    assert (
        ChatSession._msg_text_chars(image_msg, replay_producer=None)[1] == ChatSession._IMAGE_TOKENS
    )


def test_session_char_count_follows_the_active_provider() -> None:
    session = make_session()
    turn = _search_turn()
    text_chars = len("found it") + len("assistant")

    session._active_replay_producer = "anthropic"
    assert session._lane_tokens(turn, replay_producer="anthropic") == APPENDED
    assert session._msg_char_count(turn) == text_chars + int(APPENDED * session._chars_per_token)

    session._active_replay_producer = "openai"
    assert session._lane_tokens(turn, replay_producer="openai") == 0
    assert session._msg_char_count(turn) == text_chars


@pytest.mark.parametrize(
    ("producer", "replay_producer"),
    [("anthropic", "anthropic-compatible"), ("anthropic-compatible", "anthropic")],
)
def test_lane_charge_follows_the_replay_family(producer: str, replay_producer: str) -> None:
    """The two Anthropic-protocol names are one provider class with one converter, which
    replays a stored lane by block shape: a turn produced under either name is replayed, and
    charged, toward both, in both message forms.  Toward a provider of another family the turn
    is rebuilt from its text and charges nothing."""
    assert replay_family(producer) == replay_family(replay_producer) == "anthropic"
    assert replay_family("openai") == "openai"
    turn = _search_turn(producer=producer)
    msg = turn_to_dict(turn)

    assert ChatSession._lane_tokens(turn, replay_producer=replay_producer) == APPENDED
    assert ChatSession._lane_tokens(msg, replay_producer=replay_producer) == APPENDED
    assert ChatSession._lane_tokens(turn, replay_producer=producer) == APPENDED
    assert ChatSession._msg_text_chars(msg, replay_producer=replay_producer)[1] == APPENDED

    assert ChatSession._lane_tokens(turn, replay_producer="openai") == 0
    assert ChatSession._lane_tokens(msg, replay_producer="openai") == 0
    assert ChatSession._lane_tokens({"role": "assistant", "content": "x"}, replay_producer="x") == 0


def test_activating_a_lane_names_its_provider_for_the_charge() -> None:
    session = make_session()
    session._active_replay_producer = None
    lane = _lane("anthropic")

    session._activate_token_calibration(lane)

    assert session._active_replay_producer == "anthropic"


def test_wire_preparation_keeps_the_lane_cost_on_the_assistant_dict() -> None:
    """The calibration reads the as-served wire, which has been through the session's
    lowering passes; the two private siblings the measure needs survive them."""
    session = make_session()
    prepared = session._prepare_lowered_wire_messages(
        [
            {"role": "user", "content": "search for it"},
            turn_to_dict(_search_turn()),
            {"role": "user", "content": "and now?"},
        ],
        caps=ModelCapabilities(),
    )
    assistant = next(m for m in prepared if m.get("role") == "assistant")
    assert assistant["_native_tokens"] == APPENDED
    assert assistant["_producer"] == "anthropic"


def test_calibration_skips_a_request_replaying_a_lane_toward_the_serving_provider() -> None:
    """Toward the provider that replays the lane the request carries a charged lane and is
    not a calibration sample; toward another provider the lane is not charged and the
    request calibrates as any text request does."""
    session = make_session()
    ratio_before = session._chars_per_token
    served_msgs = [
        {"role": "user", "content": "search for it"},
        turn_to_dict(_search_turn()),
        {"role": "user", "content": "and now?"},
    ]
    served_chars = sum(ChatSession._msg_text_chars(m, replay_producer=None)[0] for m in served_msgs)
    tool_chars = 37

    session._last_usage = {"prompt_tokens": 54_000, "completion_tokens": 10}
    session._update_token_table(
        msgs=served_msgs,
        tool_def_chars=tool_chars,
        bound=session._message_measure("anthropic"),
        native_tokens=0,
    )
    assert session._chars_per_token == ratio_before

    # Another provider rebuilds the turn from its text and never sends the blocks.
    session._last_usage = {"prompt_tokens": 54_000, "completion_tokens": 10}
    session._update_token_table(
        msgs=served_msgs,
        tool_def_chars=tool_chars,
        bound=session._message_measure("openai"),
        native_tokens=0,
    )
    assert session._chars_per_token == (served_chars + tool_chars) / 54_000


def test_anchor_covers_the_request_and_the_turn_carries_the_appended_cost() -> None:
    """The live shape end to end inside the session: the calibration anchors on what
    the request carried, the slot shows the next request's context, and the assistant
    turn's estimate is its completion count plus the lane, so the anchored estimate equals
    the sum."""
    session = make_session()
    session._active_replay_producer = "anthropic"
    session._last_usage = {
        "prompt_tokens": SEARCH_USAGE.prompt_tokens,
        "completion_tokens": COMPLETION,
        "served_prompt_tokens": SERVED,
        "appended_prompt_tokens": APPENDED,
        "prompt_tokens_cumulative": True,
    }
    # A served request sized so the calibration lands on four characters per token:
    # (95,483 + len("user") + 37) / 23,881.
    served = [{"role": "user", "content": "x" * 95_483}]
    session._update_token_table(
        msgs=served,
        tool_def_chars=37,
        bound=session._message_measure("anthropic"),
        native_tokens=APPENDED,
    )

    assert session._chars_per_token == 4.0
    key = session._active_token_calibration_key
    assert key is not None
    assert session._token_calibrations[key].prompt_tokens == SERVED
    # The slot shows the next request's context: the anchor plus the accepted turn's charge.
    assert session._last_usage["prompt_tokens"] == SERVED + APPENDED
    assert session._last_usage["billed_prompt_tokens"] == SEARCH_USAGE.prompt_tokens
    assert session._assistant_pending_tokens == COMPLETION

    # The accepted turn is charged its completion count plus the lane's recorded count:
    # the completion count holds the turn's text and thinking, the lane the results and
    # the output the provider fed back into its own next pass.
    turn = _search_turn()
    charge = accepted_turn_tokens(
        turn,
        completion_tokens=session._assistant_pending_tokens,
        measure=session._message_measure("anthropic").measure,
        chars_per_token=session._chars_per_token,
    )
    assert charge == COMPLETION + APPENDED
    session.messages.append(turn)
    session._msg_tokens.append(charge)
    assert session._estimated_prompt_tokens() == SERVED + charge

    # A re-estimate from characters (a lane switch and back) charges the same lane, with
    # the text at the ratio standing in for the completion count.
    text_tokens = int((len("found it") + len("assistant")) / 4.0)
    assert int(session._msg_char_count(turn) / session._chars_per_token) == text_tokens + APPENDED


def test_slot_adds_the_charge_the_accepted_turn_carries() -> None:
    """The status slot shows the next request's context.  When the accepted turn kept no
    native lane (the blocks did not survive finalization, or a gateway reported the usage
    without the blocks) nothing is replayed, so the slot adds nothing, whatever the provider
    said it appended; the estimator's badge figure follows the same rule."""
    session = make_session()
    session._last_usage = {
        "prompt_tokens": SEARCH_USAGE.prompt_tokens,
        "completion_tokens": COMPLETION,
        "served_prompt_tokens": SERVED,
        "appended_prompt_tokens": APPENDED,
        "prompt_tokens_cumulative": True,
    }
    session._update_token_table(
        msgs=[{"role": "user", "content": "search for it"}],
        tool_def_chars=37,
        bound=session._message_measure("anthropic"),
        native_tokens=0,
    )
    assert session._last_usage["prompt_tokens"] == SERVED
    assert session._last_usage["billed_prompt_tokens"] == SEARCH_USAGE.prompt_tokens
    key = session._active_token_calibration_key
    assert key is not None
    assert session._token_calibrations[key].prompt_tokens == SERVED

    laneless = Turn.assistant("found it")
    assert session._lane_tokens(laneless, replay_producer="anthropic") == 0


def test_run_loop_charges_and_persists_the_lane(tmp_db: Any) -> None:
    """One accepted search turn through the real seam: the completion builder records
    the cost, the append charges the completion count plus the lane, the slot shows the
    next request's context, and the row carries the cost for a resume.  The next request
    goes to the other Anthropic-protocol name, one replay family with the producer, so the
    lane is charged toward it and the request that carries it is not a calibration sample."""
    session = make_registered_session(ui=SendPathUI(), context_window=200_000)
    session._title_generated = True
    provider = scripted_provider(
        [
            StreamChunk(
                content_delta="found it",
                usage=SEARCH_USAGE,
                finish_reason="stop",
                provider_blocks=[dict(b) for b in SEARCH_BLOCKS],
            )
        ]
    )
    provider.provider_name = "anthropic"
    replace_session_lane(session, provider=provider, capabilities=ModelCapabilities())

    session.send("search for it")

    turn = session.messages[-1]
    assert turn.native is not None
    assert turn.native.producer == "anthropic"
    assert turn.native_tokens == APPENDED
    charge = accepted_turn_tokens(
        turn,
        completion_tokens=COMPLETION,
        measure=session._message_measure(provider.provider_name).measure,
        chars_per_token=session._chars_per_token,
    )
    assert charge == COMPLETION + APPENDED
    assert session._msg_tokens[-1] == charge
    assert session._last_usage is not None
    assert session._last_usage["prompt_tokens"] == SERVED + APPENDED
    key = session._active_token_calibration_key
    assert key is not None
    assert session._token_calibrations[key].prompt_tokens == SERVED
    assert session._estimated_prompt_tokens() == SERVED + charge

    rows = get_storage().load_message_turns(session._ws_id)
    assert rows[-1].native_tokens == APPENDED

    # The next request replays the lane, served under the other name of the same
    # family.  Its plain usage counts the whole context, and the calibration on that
    # served wire divides the wire's characters by the count minus the lane, not by
    # the whole count.
    next_prompt = SERVED + COMPLETION + APPENDED + 50
    plain = scripted_provider(
        [
            StreamChunk(
                content_delta="and the answer",
                usage=UsageInfo(
                    prompt_tokens=next_prompt, completion_tokens=3, total_tokens=next_prompt + 3
                ),
                finish_reason="stop",
            )
        ]
    )
    plain.provider_name = "anthropic-compatible"
    replace_session_lane(session, provider=plain, capabilities=ModelCapabilities())

    ratio_before = session._chars_per_token
    session.send("and now?")

    assert session._lane_tokens(turn, replay_producer=plain.provider_name) == APPENDED

    # The request that replays the lane carried a charged lane, so it was not a calibration
    # sample: the ratio keeps its value.
    served_wire = session._prepare_wire_messages(
        session.system_messages + dicts_from_turns(session.messages[:-1])
    )
    bound = session._message_measure(plain.provider_name)
    assert sum(bound.lane(message) for message in served_wire) == APPENDED
    assert session._chars_per_token == ratio_before


def test_accepted_turn_charge_rule() -> None:
    """One rule for the session and the task-agent estimator: a turn costs its exact
    completion count plus the lane's recorded count toward the provider that replays the
    lane (0 for a text turn); no completion count at all falls back to characters plus the
    lane."""
    session = make_session()
    measure = session._message_measure("anthropic").measure
    plain = Turn.assistant("just text")
    assert (
        accepted_turn_tokens(plain, completion_tokens=57, measure=measure, chars_per_token=4.0)
        == 57
    )
    assert accepted_turn_tokens(
        plain, completion_tokens=None, measure=measure, chars_per_token=4.0
    ) == int((len("just text") + len("assistant")) / 4.0)
    search = _search_turn()
    assert (
        accepted_turn_tokens(
            search, completion_tokens=COMPLETION, measure=measure, chars_per_token=4.0
        )
        == COMPLETION + APPENDED
    )
    assert (
        accepted_turn_tokens(search, completion_tokens=0, measure=measure, chars_per_token=4.0)
        == int((len("found it") + len("assistant")) / 4.0) + APPENDED
    )
    # Toward a provider that does not replay the lane, the turn is text only.
    foreign = session._message_measure("openai").measure
    assert (
        accepted_turn_tokens(
            search, completion_tokens=COMPLETION, measure=foreign, chars_per_token=4.0
        )
        == COMPLETION
    )


def test_input_budget_validator_charges_the_replayed_lane() -> None:
    """The pre-flight budget check counts a replayed lane toward the window it will fill:
    the same wire passes toward a provider that rebuilds the turn from its text."""
    session = make_session(context_window=10_000, max_tokens=1_000)
    lane = session._primary_lane()
    producer = lane.provider.provider_name
    budget = session._utility_budget_snapshot(lane)
    wire = [
        {"role": "user", "content": "search for it"},
        turn_to_dict(_search_turn(producer=producer, native_tokens=9_000)),
    ]

    with pytest.raises(ModelContextLimitError):
        session._validate_model_input_budget(wire, lane, tools=[], max_tokens=1_000, budget=budget)

    foreign_wire = [
        {"role": "user", "content": "search for it"},
        turn_to_dict(_search_turn(producer="someone-else", native_tokens=9_000)),
    ]
    session._validate_model_input_budget(
        foreign_wire, lane, tools=[], max_tokens=1_000, budget=budget
    )


def test_pdf_text_budget_charges_the_replayed_lane() -> None:
    """The extracted-text allowance for PDFs is what the window has left after the other
    input; a replayed lane is part of that input."""
    session = make_session(context_window=60_000, max_tokens=1_000)
    lane = session._primary_lane()
    producer = lane.provider.provider_name
    session.messages = [Turn.user("search for it"), _search_turn(producer=producer)]
    caps = ModelCapabilities()
    budget = session._utility_budget_snapshot(lane)

    with_lane = session._attachment_pdf_text_budget_chars(
        caps, [], budget, replay_producer=producer
    )
    without_lane = session._attachment_pdf_text_budget_chars(caps, [], budget, replay_producer=None)

    assert with_lane < without_lane


# --------------------------------------------------------------------------- #
# #1190: a compaction that did not shrink the estimate is logged with both figures.
# --------------------------------------------------------------------------- #


def _has_event(caplog: pytest.LogCaptureFixture, event: str) -> bool:
    return any(event in record.getMessage() for record in caplog.records)


def test_compaction_that_does_not_shrink_the_estimate_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The compaction stands and its marker records both figures; the operator's logs get
    the same shape with the ratio.  Nothing else acts on it: the tool-result drain already
    allows one estimate-driven compaction per batch and the next reply re-anchors."""
    session = make_session(context_window=10_000, max_tokens=1_000)
    session.messages = turns_from_dicts(
        [
            {"role": "user", "content": "short question"},
            {"role": "assistant", "content": "short answer"},
            {"role": "user", "content": "another"},
            {"role": "assistant", "content": "reply"},
        ]
    )
    session._msg_tokens = [4, 4, 3, 2]
    caps = require_lane_capabilities(session._primary_lane())
    before = session._system_tokens + sum(session._msg_tokens) + session._tool_def_tokens(caps)
    oversized = SummaryResult(text="x" * 4_000, producer=None)

    with (
        patch.object(
            session._compaction_engine, "summarize_blocks", return_value=oversized
        ) as summarize,
        caplog.at_level(logging.WARNING),
    ):
        assert session._compact_messages(auto=False) is True

    assert summarize.call_count == 1
    assert session.messages[1].text.startswith("x" * 100)
    record = next(r for r in caplog.records if "compaction.estimate_not_reduced" in r.getMessage())
    caps = require_lane_capabilities(session._primary_lane())
    after = session._system_tokens + sum(session._msg_tokens) + session._tool_def_tokens(caps)
    assert after >= before
    assert log_has_field(record, "before_tokens", before)
    assert log_has_field(record, "after_tokens", after)
    assert log_has_field(record, "chars_per_token", round(session._chars_per_token, 3))


def test_compaction_that_shrinks_the_estimate_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = make_session(context_window=10_000, max_tokens=1_000)
    session.messages = turns_from_dicts(
        [
            {"role": "user", "content": "q" * 400},
            {"role": "assistant", "content": "a" * 400},
            {"role": "user", "content": "q" * 400},
            {"role": "assistant", "content": "a" * 400},
        ]
    )
    session._msg_tokens = [100, 100, 100, 100]
    dense = SummaryResult(text="dense", producer=None)

    with (
        patch.object(session._compaction_engine, "summarize_blocks", return_value=dense),
        caplog.at_level(logging.WARNING),
    ):
        assert session._compact_messages(auto=False) is True

    assert not _has_event(caplog, "compaction.estimate_not_reduced")
