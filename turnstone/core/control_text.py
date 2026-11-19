"""Control-token text in a conversation, for servers that tokenize a whole prompt.

A self-hosted server renders a conversation by writing its chat template out as
text and tokenizing all of it.  The tokenizer writes one of its control tokens
wherever it meets the token's text, so a tool result that holds the characters
of the end-of-turn token ends the tool result there, and what follows it in the
result reads as a new turn: a file, a web page or the output of a tool can
forge a user message or a tool call.  Nothing on such a server marks a part of
the prompt as content.

On these routes the prompt is text, so the text is what can be changed.
:func:`escape_control_tokens` puts a backslash after the first character of
every control-token text, the way :func:`turnstone.core.fence.neutralize`
defangs a fence marker: the text stays readable and is no token's text any
more.  :func:`escape_conversation` applies it to every text of a request: tool
results (file reads, command output, results of other servers, fetched pages),
user and system messages, the definitions of tools, and the model's own
earlier turns, which are rendered again from their text, so that a model which
quotes a file in its answer or passes such text to a command does not forge a
turn for itself.  The template writes the control tokens; no text of a
conversation is one.

Which texts are control tokens depends on the tokenizer, which these routes do
not show.  The pattern covers the families of the common templates: the
name-between-bars form, the tool-call, tool-response and reasoning tags, and
the instruction, system and turn markers of the older ones.  A text that is
none of these to a given tokenizer gets a backslash it did not need.

Only what is sent is changed: the stored conversation keeps the texts as they
were.  The model reads the escaped text; what it writes is not changed, so a
server that parses tool calls out of generated text still does so.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MARK = chr(92)
_CONTROL = re.compile(
    "<[|][^ |<>" + chr(9) + chr(10) + chr(13) + "]{1,64}[|]>"
    "|</?(?:tool_call|tool_response|think|s)>"
    "|<(?:start_of_turn|end_of_turn|bos|eos)>"
    "|" + _MARK + "[/?INST" + _MARK + "]"
    "|<</?SYS>>"
    "|<tts_[a-z_]{1,32}>"
)


def escape_control_tokens(text: str) -> str:
    """*text* with a backslash after the first character of every control-token
    text; the very object when it holds none.  Applied twice it changes nothing
    more: an escaped text is not matched."""
    if "<" not in text and "[" not in text:
        return text
    escaped = _CONTROL.sub(lambda found: found.group(0)[0] + _MARK + found.group(0)[1:], text)
    return text if escaped == text else escaped


def escape_conversation(value: Any) -> Any:
    """*value* (the messages of a request, or its tool definitions) with every
    text escaped, wherever it stands: contents and their parts, reasoning, the
    names and arguments of calls, descriptions and parameter names.  A call's
    arguments are escaped as the values they decode to and written out again.
    A part that holds nothing to escape is the object it was, and so is the
    whole."""
    if isinstance(value, str):
        return escape_control_tokens(value)
    if isinstance(value, list):
        items = [escape_conversation(item) for item in value]
        return value if all(a is b for a, b in zip(items, value, strict=True)) else items
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        same = True
        for key, item in value.items():
            new_key = escape_control_tokens(key) if isinstance(key, str) else key
            new_item = _arguments(item) if key == "arguments" else escape_conversation(item)
            same = same and new_key is key and new_item is item
            out[new_key] = new_item
        return value if same else out
    return value


def _arguments(value: Any) -> Any:
    """A call's arguments: a JSON text is escaped as what it decodes to (a
    backslash put into the JSON itself would be no JSON), anything else as it
    is."""
    if not isinstance(value, str):
        return escape_conversation(value)
    if "<" not in value and "[" not in value:
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return escape_control_tokens(value)
    if not isinstance(decoded, (dict, list)):
        return escape_control_tokens(value)
    escaped = escape_conversation(decoded)
    return value if escaped is decoded else json.dumps(escaped, ensure_ascii=False)
