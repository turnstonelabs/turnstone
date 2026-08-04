"""Behavior pins for the interactive think-tag splitting layer.

``ChatSession._stream_attempt`` splits streamed content into content vs
reasoning around ``<think>``/``<reasoning>`` tags, buffering potential
partial tags across chunk boundaries.  These tables pin the CURRENT
emission behavior — exact UI token sequence and final message content —
so the logic can move into a standalone ``ThinkTagSplitter`` class with
byte-identical output.  Every case drives the real chunk consumer end to
end; none reaches into the implementation, so the same rows must stay
green across the extraction.

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

import pytest

from tests._session_helpers import make_session
from turnstone.core.providers import StreamChunk, ToolCallDelta
from turnstone.core.streaming_text import ThinkTagSplitter


class _TokenRecorderUI:
    """Records dispatched token events; stubs the rest of the surface
    ``_stream_attempt`` touches."""

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


def _drive(chunks, *, show_reasoning=True):
    session = make_session()
    session.show_reasoning = show_reasoning
    ui = _TokenRecorderUI()
    session.ui = ui
    msg = session._stream_attempt(iter(chunks))
    return msg, ui.tokens


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
]


@pytest.mark.parametrize(
    ("chunks", "expected_events", "expected_content"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_tag_splitting_emissions(chunks, expected_events, expected_content):
    msg, tokens = _drive(chunks)
    assert tokens == expected_events
    assert msg["content"] == expected_content


def test_show_reasoning_off_suppresses_reasoning_dispatch_only():
    msg, tokens = _drive([_c("<think>deep</think>answer"), _FINISH], show_reasoning=False)
    assert tokens == [("content", "answer")]
    assert msg["content"] == "answer"


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


def test_tool_calls_flush_pending_raw_at_current_state():
    # Once tool calls begin, buffered text cannot be a partial tag: it
    # flushes RAW (no tag scan) at the current in_think state.
    chunks = [
        _c("part<thi"),
        StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, id="tc1", name="bash")]),
        StreamChunk(
            tool_call_deltas=[ToolCallDelta(index=0, arguments_delta="{}")],
            finish_reason="tool_calls",
        ),
    ]
    msg, tokens = _drive(chunks)
    assert tokens == [("content", "part<thi")]
    assert msg["content"] == "part<thi"
    assert msg["tool_calls"] == [
        {"id": "tc1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    ]
