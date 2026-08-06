"""Behavior pins for the interactive think-tag splitting layer.

``turnstone.core.session._StreamTurnConsumer`` (the main loop's chunk→UI
translation, ``model_turn``'s ``on_chunk`` body) splits streamed content
into content vs reasoning around ``<think>``/``<reasoning>`` tags,
buffering potential partial tags across chunk boundaries.  These tables
pin the CURRENT emission behavior — exact UI token sequence and final
displayed content — so the logic can move into a standalone
``ThinkTagSplitter`` class with byte-identical output.  Every case drives
the real chunk consumer end to end; none reaches into the
implementation, so the same rows must stay green across the extraction.

Pinned rules:

- partial-tag buffering: a chunk ending in a possible tag prefix emits
  nothing until the tag resolves or the safe-flush margin clears it;
- safe-flush: with no tag in sight, everything but the trailing
  MAX-tag-length chars flushes immediately (live streaming), the tail
  only at stream end;
- open/close tag selection by EARLIEST index among the tag variants;
- ``in_think`` transitions, including the ``reasoning_delta`` (path-1)
  interplay and the raw pending flush when tool calls begin (pending
  text can no longer be a partial tag).
"""

import random
from unittest.mock import MagicMock

import pytest

from tests._reasoning_dialect import CASES as DIALECT_CASES
from tests._session_helpers import make_session, scripted_provider
from turnstone.core.model_turn import ModelLane
from turnstone.core.providers import StreamChunk, ToolCallDelta
from turnstone.core.session import _CancelRef, _StreamTurnConsumer
from turnstone.core.streaming_text import (
    ThinkTagSplitter,
    partial_tag_tail,
    split_inline_reasoning,
)
from turnstone.core.trajectory import Turn


class _TokenRecorderUI:
    """Records dispatched token events; stubs the rest of the surface
    ``_StreamTurnConsumer`` touches."""

    def __init__(self):
        self.tokens = []

    def on_content_token(self, text):
        self.tokens.append(("content", text))

    def on_reasoning_token(self, text):
        self.tokens.append(("reasoning", text))

    def on_thinking_stop(self):
        pass

    def on_stream_end(self):
        pass

    def on_info(self, message):
        pass

    def on_error(self, message):
        pass


def _drive(chunks, *, show_reasoning=True, capabilities=None):
    """Drive *chunks* through a bare ``_StreamTurnConsumer`` — the
    display-grid seam.  Tool-call assembly is the drain's job, so this
    helper serves display-only pins: content emission order plus the
    accumulated displayed text.
    """
    session = make_session()
    session.show_reasoning = show_reasoning
    ui = _TokenRecorderUI()
    session.ui = ui
    lane = ModelLane(
        provider=MagicMock(), client=MagicMock(), model="test-model", capabilities=capabilities
    )
    consumer = _StreamTurnConsumer(session, 0)
    # begin_attempt is the consumer's SOLE per-attempt initializer (the
    # lane-free constructor carries no display state of its own).
    consumer.begin_attempt(_CancelRef(session, 0), None, lane)
    for chunk in chunks:
        consumer(chunk)
    consumer.finish_stream()
    return "".join(consumer._content_parts), ui.tokens


def _c(text):
    return StreamChunk(content_delta=text)


_FINISH = StreamChunk(finish_reason="stop")

# (case id, chunks, expected ordered token events, expected final content)
CASES = [
    (
        "plain_short_held_until_end",
        [_c("hello"), _FINISH],
        [("content", "hello")],
        "hello",
    ),
    (
        "think_block_single_chunk",
        [_c("<think>deep</think>answer"), _FINISH],
        [("reasoning", "deep"), ("content", "answer")],
        "answer",
    ),
    (
        "tag_split_across_chunk_boundary",
        [_c("before<thi"), _c("nk>r</think>after"), _FINISH],
        [("content", "before"), ("reasoning", "r"), ("content", "after")],
        "beforeafter",
    ),
    (
        "reasoning_tag_variant",
        [_c("<reasoning>x</reasoning>done"), _FINISH],
        [("reasoning", "x"), ("content", "done")],
        "done",
    ),
    (
        "earliest_tag_index_wins",
        [_c("a<reasoning>b</reasoning>c<think>d</think>e"), _FINISH],
        [
            ("content", "a"),
            ("reasoning", "b"),
            ("content", "c"),
            ("reasoning", "d"),
            ("content", "e"),
        ],
        "ace",
    ),
    (
        "safe_flush_streams_all_but_max_tag_len",
        [_c("x" * 30), _FINISH],
        [("content", "x" * 18), ("content", "x" * 12)],
        "x" * 30,
    ),
    (
        "path1_reasoning_then_content",
        [StreamChunk(reasoning_delta="rr"), _c("cc"), _FINISH],
        [("reasoning", "rr"), ("content", "cc")],
        "cc",
    ),
    (
        "unterminated_think_tail_flushes_as_reasoning",
        [_c("<think>abc"), _FINISH],
        [("reasoning", "abc")],
        "",
    ),
    (
        "content_surrounds_think_block",
        [_c("before<think>mid</think>after"), _FINISH],
        [("content", "before"), ("reasoning", "mid"), ("content", "after")],
        "beforeafter",
    ),
    (
        "safe_flush_inside_think_block",
        [_c("<think>" + "y" * 20), _c("</think>ok"), _FINISH],
        [("reasoning", "y" * 8), ("reasoning", "y" * 12), ("content", "ok")],
        "ok",
    ),
    # Reasoning-boundary run close: a buffered content tail must emit as
    # CONTENT when a reasoning_delta arrives, exactly as the drain closes
    # its per-run split there.
    (
        "reasoning_boundary_closes_short_content_run",
        [_c("Short"), StreamChunk(reasoning_delta="(r)"), _FINISH],
        [("content", "Short"), ("reasoning", "(r)")],
        "Short",
    ),
    (
        "reasoning_boundary_closes_long_run_tail",
        [_c("A much longer content run here"), StreamChunk(reasoning_delta="(r)"), _FINISH],
        [
            ("content", "A much longer cont"),
            ("content", "ent run here"),
            ("reasoning", "(r)"),
        ],
        "A much longer content run here",
    ),
    (
        "partial_tag_carries_across_reasoning_boundary",
        [
            _c("Ans<thi"),
            StreamChunk(reasoning_delta="(r)"),
            _c("nk>hidden</think>done"),
            _FINISH,
        ],
        [
            ("content", "Ans"),
            ("reasoning", "(r)"),
            ("reasoning", "hidden"),
            ("content", "done"),
        ],
        "Ansdone",
    ),
]


@pytest.mark.parametrize(
    ("chunks", "expected_events", "expected_content"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_tag_splitting_emissions(chunks, expected_events, expected_content):
    content, tokens = _drive(chunks)
    assert tokens == expected_events
    assert content == expected_content


def test_show_reasoning_off_suppresses_reasoning_dispatch_only():
    content, tokens = _drive([_c("<think>deep</think>answer"), _FINISH], show_reasoning=False)
    assert tokens == [("content", "answer")]
    assert content == "answer"


def test_splitter_standalone_contract():
    """The extracted class is consumable without a session: feed() emits
    tag-resolved spans through the callback, flush_pending() drains the
    carry buffer raw, and in_think is externally writable for
    out-of-band transitions."""
    events = []
    splitter = ThinkTagSplitter(lambda text, is_reasoning: events.append((text, is_reasoning)))
    splitter.feed("a<think>b</think>c")
    assert events == [("a", False), ("b", True)]
    assert splitter.pending == "c"
    splitter.flush_pending()
    assert events == [("a", False), ("b", True), ("c", False)]
    assert splitter.pending == ""
    splitter.in_think = True
    splitter.feed("tail")
    splitter.flush_pending()
    assert events[-1] == ("tail", True)


@pytest.mark.parametrize("case", DIALECT_CASES, ids=[c.id for c in DIALECT_CASES])
def test_one_shot_dialect_conformance(case):
    """Every dialect-catalog case through the one-shot form, exact lanes."""
    content, reasoning = split_inline_reasoning(case.utterance)
    assert content == case.content
    assert reasoning == case.reasoning


_PASSTHROUGH_CASES = [c for c in DIALECT_CASES if c.passthrough]


@pytest.mark.parametrize("case", _PASSTHROUGH_CASES, ids=[c.id for c in _PASSTHROUGH_CASES])
def test_one_shot_passthrough_byte_identity(case):
    """Unconsumed input (tag-free or orphan-close-only) returns
    byte-identical — every generated row asserts."""
    content, _ = split_inline_reasoning(case.utterance)
    assert content == case.utterance


@pytest.mark.parametrize("case", DIALECT_CASES, ids=[c.id for c in DIALECT_CASES])
def test_scan_tags_off_returns_every_utterance_byte_identical(case):
    """``server_parses_reasoning`` backends put reasoning in their own
    channel, so content carries none — the scan is turned OFF and EVERY
    catalog utterance passes through untouched, including the ones the
    scan would otherwise consume.  This is what buys back residual R2:
    prose that merely QUOTES a tag can no longer be misrouted."""
    content, reasoning = split_inline_reasoning(case.utterance, scan_tags=False)
    assert content == case.utterance
    assert reasoning == ""


def test_session_consumer_scan_follows_server_parses_reasoning():
    """The interactive consumer wires ``scan_tags`` from the SAME capability
    the drain seam reads (``server_parses_reasoning``), taken off the
    ACTIVE lane.  With the flag declared, streamed tag text reaches the UI
    verbatim as content — it is prose on such a backend, not a
    boundary."""
    from turnstone.core.providers._protocol import ModelCapabilities

    content, tokens = _drive(
        [_c("<think>quoted</think>answer"), _FINISH],
        capabilities=ModelCapabilities(server_parses_reasoning=True),
    )
    assert content == "<think>quoted</think>answer"
    assert all(kind == "content" for kind, _ in tokens)


def test_scan_tags_off_holds_no_carry_and_honors_out_of_band_state():
    """With the scan off there is nothing to resolve, so nothing is held:
    every span emits immediately at the current state.  The state machine
    stays live — the consumer still writes ``in_think`` for the
    provider-parsed reasoning transitions, which is the whole point on a
    backend that segregates."""
    events = []
    splitter = ThinkTagSplitter(
        lambda text, is_reasoning: events.append((text, is_reasoning)), scan_tags=False
    )
    splitter.feed("a<think>b</think>c")
    assert events == [("a<think>b</think>c", False)]
    assert splitter.pending == ""
    splitter.in_think = True
    splitter.feed("<think>still content-lane text")
    assert events[-1] == ("<think>still content-lane text", True)


@pytest.mark.parametrize("case", DIALECT_CASES, ids=[c.id for c in DIALECT_CASES])
def test_one_shot_equivalent_to_streaming_over_random_chunkings(case):
    """One-shot ≡ the streaming class fed the same utterance in arbitrary
    chunkings, EXACTLY — the one-shot is a pure raw split with no rules of
    its own — for EVERY catalog case."""
    rng = random.Random(case.id)  # deterministic per case
    one_content, one_reasoning = split_inline_reasoning(case.utterance)
    for _ in range(25):
        spans = []
        splitter = ThinkTagSplitter(lambda text, is_r, _s=spans: _s.append((text, is_r)))
        i = 0
        while i < len(case.utterance):
            j = rng.randint(i + 1, len(case.utterance))
            splitter.feed(case.utterance[i:j])
            i = j
        splitter.flush_pending()
        assert "".join(t for t, is_r in spans if not is_r) == one_content
        assert "".join(t for t, is_r in spans if is_r) == one_reasoning


def test_tool_calls_flush_pending_raw_at_current_state():
    # Once tool calls begin, buffered text cannot be a partial tag: it
    # flushes RAW (no tag scan) at the current in_think state.  Assembly
    # is the drain's job while the consumer only flushes the splitter, so
    # this pin drives the real seam to hold the display order and the
    # assembled call together.
    chunks = [
        _c("part<thi"),
        StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, id="tc1", name="bash")]),
        StreamChunk(
            tool_call_deltas=[ToolCallDelta(index=0, arguments_delta="{}")],
            finish_reason="tool_calls",
        ),
    ]
    session = make_session()
    ui = _TokenRecorderUI()
    session.ui = ui
    session._provider = scripted_provider(chunks)
    session.messages.append(Turn.user("hi"))
    result = session._stream_response(0)
    assert ui.tokens == [("content", "part<thi")]
    assert result.content == "part<thi"
    assert result.tool_calls == [
        {"id": "tc1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    ]


class TestPartialTagTail:
    """Boundary-contract rows for ``partial_tag_tail``: only a PROPER
    prefix of a tag is a partial tag.  A complete tag self-matching via
    ``startswith`` makes the drain carry a finished ``<reasoning>`` across
    a run boundary as if it might still grow, relabeling the next run."""

    @pytest.mark.parametrize(
        ("text", "tail"),
        [
            ("Answer<reasoning>", ""),  # complete tag is NOT partial
            ("Answer<think>", ""),
            ("orphan</think>", ""),
            ("Answer</reasoning>", ""),
            ("Answer<reasonin", "<reasonin"),
            ("Ans<thi", "<thi"),
            ("trailing<", "<"),
            ("no tags here", ""),
            ("", ""),
        ],
    )
    def test_contract(self, text, tail):
        assert partial_tag_tail(text) == tail


class TestCloseRun:
    """``close_run`` — the reasoning-boundary run close, mirroring the
    drain's per-run rule: decided text emits at the current state, only
    a partial tag prefix carries."""

    def _splitter(self):
        events = []
        return ThinkTagSplitter(lambda t, r: events.append((t, r))), events

    def test_plain_tail_emits_as_content(self):
        sp, events = self._splitter()
        sp.feed("Short")
        assert sp.close_run() == ""
        assert events == [("Short", False)]
        assert sp.pending == ""

    def test_partial_tag_tail_is_returned_not_held(self):
        # The carry's lifetime belongs to the RUN OWNER: the splitter's
        # own pending would be re-read under a flipped in_think and
        # relabeled, so close_run hands the tail back and clears its
        # buffer.
        sp, events = self._splitter()
        sp.feed("Ans<thi")
        assert sp.close_run() == "<thi"
        assert events == [("Ans", False)]
        assert sp.pending == ""

    def test_reasoning_state_tail_emits_as_reasoning(self):
        sp, events = self._splitter()
        sp.in_think = True
        sp.feed("held thought")
        assert sp.close_run() == ""
        assert events == [("held thought", True)]
        assert sp.pending == ""

    def test_empty_pending_is_noop(self):
        sp, events = self._splitter()
        assert sp.close_run() == ""
        assert events == []
