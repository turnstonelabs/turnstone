"""The text a model wrote for each tool-call argument, read from the generated text.

A server that parses tool calls out of the generated text delivers typed
arguments; what the model actually wrote (``True`` or ``true``, ``None`` or
``null``) is only in the text.  The headless session's ``raw_arguments`` option
sends each argument back as that text, so the next prompt's tokens match what
was generated; this module is the source of the text for a rollout.

How a call is written is the format's business
(:mod:`turnstone.rollout.replyformat`): a value is everything between the line
break after an argument's opening tag and the line break before its closing
tag, as written, line breaks inside it included.  A call that does not have the
shape the format's template writes is reported as ``None``: the session then
replays it from the parsed values and says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.rollout.replyformat import PARAMETER_BLOCKS, ReplyFormat

if TYPE_CHECKING:
    from turnstone.rollout.recording import RecordingClient


def parameter_block_calls(text: str) -> list[tuple[str, dict[str, str]] | None]:
    """The tool calls in *text* as the parameter-block format writes them, in
    order: ``(name, {argument: text})`` each, ``None`` for a call whose text
    does not have the expected shape."""
    return PARAMETER_BLOCKS.written_calls(text)


class RecordedArguments:
    """``raw_arguments`` source for a session: reads the recorder's latest own call.

    The session asks right after each of its model turns, before any tool runs,
    so the latest call the recorder holds for the session itself is that turn.
    """

    def __init__(self, recorder: RecordingClient, fmt: ReplyFormat = PARAMETER_BLOCKS) -> None:
        self._recorder = recorder
        self._format = fmt

    def __call__(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, str] | None] | None:
        record = self._recorder.last_parent_call()
        if record is None or not record.complete:
            return None
        written = self._format.written_calls(record.raw_text)
        if len(written) != len(tool_calls):
            return None
        out: list[dict[str, str] | None] = []
        for call, parsed in zip(written, tool_calls, strict=True):
            name = (parsed.get("function") or {}).get("name")
            out.append(call[1] if call is not None and call[0] == name else None)
        return out
