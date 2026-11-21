"""One bound on the size of a single tool result in a rollout.

A rollout has a cap on its whole length, and one tool result must not be able
to use it up.  The framework's tools bound their results each in their own
way, and some not at all:

- a command's output and a file comparison are the rendering of a bounded
  capture: half the framework's truncation setting, and never less than
  32,000 characters;
- the output of a background shell is read up to the truncation setting;
- a file is read whole up to the environment's file size limit;
- a search has its own budget of 32,768 characters;
- what a sub-agent hands back is its whole last reply;
- what a tool of the task's own servers returns is whatever the server sends;
- an error can quote the lines it is about, however long they are.

``RolloutConfig.max_tool_result_chars`` is one bound for all of them, applied
where a result enters the session's conversation: no single result is longer.
Unset, nothing here runs.

The cut is the same for the same result, so the same call gives the same
text and the prompt still extends.  It says what was left out in a fixed
wording, with the counts, and how to get the rest:

- a file read keeps whole lines from its start and names the line to read on
  from, with ``read_file``'s ``offset`` and ``limit``;
- everything else keeps its beginning and its end, where a command says how it
  went, around a line that counts what is left out between them.

Inside a sub-agent the framework's own clip of 16,000 characters a result
applies as before; this bound is on what the sub-agent hands back.

One thing is done to a result whether or not a bound is set
(:func:`sendable_result`): half of a surrogate pair, which a tool can hand
back when a model wrote one as an escape, is replaced.  No request body can
carry it, so the next request could not be sent at all, and the run would end
as a failure of the machinery over a text the model chose.
"""

from __future__ import annotations

from typing import Any

from turnstone.core.truncation import source_chars, truncate_text

# The smallest bound that leaves room for the text that says what was cut.
MIN_RESULT_CHARS = 1000
# Room kept for that text where it is put together after the cut.
_NOTE_ROOM = 400

# How to get what a cut left out, by tool.
_HOW = {
    "bash": "Narrow the command, or send its output to a file and read part of it",
    "bash_output": "Read less at a time: give bash_output a filter",
    "diff_file": "Compare less at a time",
    "search": "Narrow the query or the path",
    "task_agent": (
        "Ask the sub-agent for a shorter answer, or have it write its findings to a file and "
        "read part of it"
    ),
}
_HOW_SERVER_TOOL = "Ask for less: narrower arguments, fewer items, a page at a time"
_HOW_OTHER = "Ask for less at a time"


def _how(tool: str) -> str:
    if tool.startswith("mcp__"):
        return _HOW_SERVER_TOOL
    return _HOW.get(tool, _HOW_OTHER)


def _cut_read(text: str, limit: int) -> str:
    """A file read cut after its last whole line that fits, with the line to
    read on from."""
    original = len(text)
    newline = chr(10)
    head = text[: limit - _NOTE_ROOM]
    end = head.rfind(newline)
    if end > 0:
        head = head[:end]
        number = head[head.rfind(newline) + 1 :].split(chr(9), 1)[0].strip()
        if number.isdigit():
            return (
                f"{head}{newline}{newline}[read_file: the result stops after line {number}. "
                f"{original - len(head)} of its {original} characters are left out: one tool "
                f"result holds at most {limit}. Read on with offset={int(number) + 1} and a "
                "limit.]"
            )
    # One line longer than a result may be, or text that is not numbered lines.
    return (
        f"{head}{newline}{newline}[read_file: the result stops here. {original - len(head)} of "
        f"its {original} characters are left out: one tool result holds at most {limit}. "
        "Read less at a time with offset and limit; a line longer than that is read in "
        "pieces with a shell command.]"
    )


def bound_text(tool: str, text: str, limit: int) -> str | None:
    """*text*, the result of *tool*, cut to at most *limit* characters; ``None``
    when it is within the bound as it is.

    The same text gives the same cut.
    """
    if len(text) <= limit:
        return None
    if tool == "read_file":
        return _cut_read(text, limit)
    how = _how(tool)
    newline = chr(10)

    def note(omitted: int, original: int, _limit: int) -> str:
        return (
            f"{newline}{newline}[... {omitted} of {original} characters left out here: one tool "
            f"result holds at most {limit}. {how}. ...]{newline}{newline}"
        )

    # A rendering of a bounded capture is cut again from what the capture
    # kept, so the count is of the command's whole output.
    return truncate_text(text, limit, mode="head_tail", marker_factory=note).text


def sendable_text(text: str) -> tuple[str, int]:
    """*text* as a request body can carry it, and how many halves of surrogate
    pairs it held (0: it is returned as it is).

    A request body is UTF-8, which has no way to write half of a surrogate
    pair.  Each half that stands alone becomes the replacement character
    (U+FFFD); two halves that stand together are the one character they spell.
    The same text gives the same result.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        halves = sum(1 for character in text if 0xD800 <= ord(character) <= 0xDFFF)
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace"), halves
    return text, 0


def sendable_result(output: Any) -> tuple[Any, int]:
    """A tool result as a request body can carry it, and how many halves of
    surrogate pairs it held (0: it is returned as it is).

    A result that is text is repaired as text.  One made of parts has each
    text part repaired; what is no text is left as it is.
    """
    if isinstance(output, str):
        repaired, halves = sendable_text(output)
        # A result that needs nothing stays the object it was.
        return (repaired, halves) if halves else (output, 0)
    if isinstance(output, list):
        total = 0
        parts: list[Any] = []
        for part in output:
            text = part.get("text") if isinstance(part, dict) else None
            repaired, halves = sendable_text(text) if isinstance(text, str) else ("", 0)
            parts.append({**part, "text": repaired} if halves else part)
            total += halves
        return (parts, total) if total else (output, 0)
    return output, 0


def bound_result(tool: str, output: Any, limit: int) -> tuple[Any, int | None]:
    """A tool result as it may enter the conversation, and the size it had
    before a cut (``None`` when nothing was cut).

    A result that is text is cut as text.  One made of parts (a text beside
    images) has each text part cut; what is no text is left as it is.
    """
    if isinstance(output, str):
        cut = bound_text(tool, output, limit)
        return (output, None) if cut is None else (cut, source_chars(output))
    if isinstance(output, list):
        size: int | None = None
        parts: list[Any] = []
        for part in output:
            text = part.get("text") if isinstance(part, dict) else None
            cut = bound_text(tool, text, limit) if isinstance(text, str) else None
            if cut is None or not isinstance(text, str):
                parts.append(part)
            else:
                size = (size or 0) + len(text)
                parts.append({**part, "text": cut})
        return (output, None) if size is None else (parts, size)
    return output, None
