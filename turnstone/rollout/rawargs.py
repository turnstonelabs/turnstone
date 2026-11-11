"""The text a model wrote for each tool-call argument, read from the generated text.

A server that parses tool calls out of the generated text delivers typed
arguments; what the model actually wrote (``True`` or ``true``, ``None`` or
``null``) is only in the text.  The headless session's ``raw_arguments`` option
sends each argument back as that text, so the next prompt's tokens match what
was generated; this module is the source of the text for a rollout.

One text format is read here, parameter blocks::

    <tool_call>
    <function=NAME>
    <parameter=KEY>
    VALUE
    </parameter>
    </function>
    </tool_call>

A value is everything between the line break after ``<parameter=KEY>`` and the
line break before ``</parameter>``, as written, line breaks inside it included.
A call that does not have this shape is reported as ``None``: the session then
replays it from the parsed values and says so.  A server with another format
needs another reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turnstone.rollout.recording import RecordingClient

_NL = chr(10)
_CALL_OPEN, _CALL_CLOSE = "<tool_call>", "</tool_call>"
_FUNCTION_OPEN, _FUNCTION_CLOSE = "<function=", "</function>"
_PARAMETER_OPEN, _PARAMETER_CLOSE = "<parameter=", "</parameter>"


def _one_call(block: str) -> tuple[str, dict[str, str]] | None:
    start = block.find(_FUNCTION_OPEN)
    if start < 0:
        return None
    name_end = block.find(">", start)
    end = block.rfind(_FUNCTION_CLOSE)
    if name_end < 0 or end < name_end:
        return None
    name = block[start + len(_FUNCTION_OPEN) : name_end]
    body = block[name_end + 1 : end]
    values: dict[str, str] = {}
    at = 0
    while True:
        opened = body.find(_PARAMETER_OPEN, at)
        if opened < 0:
            # Only white space may stand outside the blocks.
            return (name, values) if not body[at:].strip() else None
        if body[at:opened].strip():
            return None
        key_end = body.find(">", opened)
        if key_end < 0:
            return None
        key = body[opened + len(_PARAMETER_OPEN) : key_end]
        value_start = key_end + 1
        closed = body.find(_NL + _PARAMETER_CLOSE, value_start)
        if not body.startswith(_NL, value_start) or closed < value_start or key in values:
            return None
        # An empty value is written as one line break shared by both markers.
        values[key] = body[value_start + 1 : closed] if closed > value_start else ""
        at = closed + len(_NL + _PARAMETER_CLOSE)


def parameter_block_calls(text: str) -> list[tuple[str, dict[str, str]] | None]:
    """The tool calls in *text*, in order: ``(name, {argument: text})`` each.

    ``None`` stands for a call whose text does not have the expected shape.
    """
    calls: list[tuple[str, dict[str, str]] | None] = []
    at = 0
    while True:
        opened = text.find(_CALL_OPEN, at)
        if opened < 0:
            return calls
        closed = text.find(_CALL_CLOSE, opened)
        if closed < 0:
            calls.append(None)
            return calls
        calls.append(_one_call(text[opened + len(_CALL_OPEN) : closed]))
        at = closed + len(_CALL_CLOSE)


class RecordedArguments:
    """``raw_arguments`` source for a session: reads the recorder's latest own call.

    The session asks right after each of its model turns, before any tool runs,
    so the latest call the recorder holds for the session itself is that turn.
    """

    def __init__(self, recorder: RecordingClient) -> None:
        self._recorder = recorder

    def __call__(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, str] | None] | None:
        record = self._recorder.last_parent_call()
        if record is None or not record.complete:
            return None
        written = parameter_block_calls(record.raw_text)
        if len(written) != len(tool_calls):
            return None
        out: list[dict[str, str] | None] = []
        for call, parsed in zip(written, tool_calls, strict=True):
            name = (parsed.get("function") or {}).get("name")
            out.append(call[1] if call is not None and call[0] == name else None)
        return out
