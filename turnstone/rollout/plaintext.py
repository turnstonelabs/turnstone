"""What the environment hands the model, read as plain text.

A tokenizer has tokens of its own: the ones a chat template writes to open and
close a turn, a tool call or a stretch of reasoning, the placeholder of an
image, and others.  It writes such a token wherever it meets the token's text,
and a server renders a conversation by writing its template out as text and
tokenizing the whole.  So a tool result that holds the characters of the
end-of-turn token puts a real end of turn inside the tool result, one that
holds the characters of a tool call's marker forges a call, and one that holds
the image placeholder's characters takes the next image.  Nothing on a server
says "this part is content".

With prompts as token IDs it can be said on this side.  Before a conversation
goes to the tokenize route, each such text in what the environment wrote (the
system prompt, user messages, tool results, tool definitions) is replaced by
one stand-in: a token of the tokenizer that the template never writes.  A
token of the tokenizer comes out of any text as itself, never joined with its
neighbours, so the rendering holds one stand-in token per replaced text, in
order.  Each is then replaced by an ordinary spelling of the original text:
the tokens of the text cut in two, neither half being a token's text any
more.  The model sees the characters a person reading the file sees, and no
token the environment could not have typed as text.

What the model itself generated is never touched: its turns stand in the
sequence as the tokens it wrote.  Where a turn of the model's is rendered
again from its text (to find where the turn ends, and for a sub-agent's
prompts, which are renderings throughout) its text is read the same way: a
model that wrote the characters of a token wrote characters.

A rendering whose stand-in tokens do not match the texts replaced cannot be
spliced: that is a fault of this code or of the check that chose the stand-in,
never of what the model or the environment wrote, and it is raised as
:class:`HarnessFaultError`.

:func:`plain_text_verdict` establishes, as part of the check before a run,
what a run needs: which texts the tokenizer has tokens for, an ordinary
spelling of each, a stand-in, and where the template writes the tool
definitions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

from turnstone.rollout.settings import SetupError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


class HarnessFaultError(RuntimeError):
    """The harness found itself at fault: something it guarantees did not hold,
    and nothing the model or the environment wrote can have caused it.  The run
    ends as ``harness_fault``, which no caller should run again without a
    look."""


def added_tokens_of(info: Any) -> dict[str, int]:
    """The tokenizer's own tokens, text to ID, as a description of the
    tokenizer names them: what a server's ``/tokenizer_info`` answers with or
    a ``tokenizer_config.json`` (``added_tokens_decoder``), or a
    ``tokenizer.json`` (``added_tokens``).  Empty where it names none."""
    found: dict[str, int] = {}
    if not isinstance(info, dict):
        return found
    decoder = info.get("added_tokens_decoder")
    if isinstance(decoder, dict):
        for token, entry in decoder.items():
            text = entry.get("content") if isinstance(entry, dict) else entry
            if isinstance(text, str) and text and str(token).isdigit():
                found[text] = int(token)
    for entry in info.get("added_tokens") or []:
        if isinstance(entry, dict) and isinstance(entry.get("content"), str):
            token = entry.get("id")
            if isinstance(token, int) and not isinstance(token, bool) and entry["content"]:
                found[entry["content"]] = token
    return found


@dataclass(frozen=True)
class PlainText:
    """What :func:`plain_text_verdict` established: all a run needs to have
    the environment's text read as plain text."""

    # The stand-in: a token of the tokenizer the template never writes.
    stand_in: str
    stand_in_id: int
    # Each text the tokenizer has a token for, with an ordinary spelling of it.
    spelled: dict[str, tuple[int, ...]]
    # Whether the template writes the tool definitions before the text of a
    # system message at the head of the conversation.
    tools_first: bool = False

    @classmethod
    def from_verdict(cls, verdict: Any) -> PlainText | None:
        """What a check's verdict describes; ``None`` for a check that could
        not establish it.  Raises :class:`SetupError` for one that says it did
        and does not hold what that takes."""
        if not isinstance(verdict, dict) or not verdict.get("ok"):
            return None
        spelled = verdict.get("spelled")
        text, token = verdict.get("stand_in"), verdict.get("stand_in_id")
        sound = (
            isinstance(text, str)
            and isinstance(token, int)
            and not isinstance(token, bool)
            and isinstance(spelled, dict)
            and text in spelled
            and all(
                isinstance(k, str)
                and isinstance(v, list)
                and v
                and all(isinstance(t, int) and not isinstance(t, bool) for t in v)
                for k, v in spelled.items()
            )
        )
        if (
            not sound
            or not isinstance(text, str)
            or not isinstance(token, int)
            or not isinstance(spelled, dict)
        ):
            raise SetupError(
                "the given check of the server does not hold what it found about the "
                "tokenizer's own tokens (special_text: a stand-in and a spelling for each "
                "text): check the server again"
            )
        return cls(
            stand_in=text,
            stand_in_id=token,
            spelled={k: tuple(v) for k, v in spelled.items()},
            tools_first=bool(verdict.get("tools_first")),
        )

    @cached_property
    def _every(self) -> re.Pattern[str]:
        # The longest text first: one token's text can begin another's.
        return re.compile(
            "|".join(re.escape(t) for t in sorted(self.spelled, key=len, reverse=True))
        )

    def _text(self, text: str, replaced: list[str]) -> str:
        def note(match: re.Match[str]) -> str:
            replaced.append(match.group(0))
            return self.stand_in

        return self._every.sub(note, text)

    def _walk(self, value: Any, replaced: list[str]) -> Any:
        """A JSON value with every text of the tokenizer's replaced, in the
        order a template writes the value out."""
        if isinstance(value, str):
            return self._text(value, replaced)
        if isinstance(value, list):
            return [self._walk(item, replaced) for item in value]
        if isinstance(value, dict):
            return {
                self._walk(key, replaced) if isinstance(key, str) else key: self._walk(
                    item, replaced
                )
                for key, item in value.items()
            }
        return value

    def _content(self, content: Any, replaced: list[str]) -> Any:
        if isinstance(content, str):
            return self._text(content, replaced)
        if isinstance(content, list):
            return [
                {**part, "text": self._text(part["text"], replaced)}
                if isinstance(part, dict) and isinstance(part.get("text"), str)
                else part
                for part in content
            ]
        return content

    def _message(self, message: Mapping[str, Any], replaced: list[str]) -> dict[str, Any]:
        """One message with its texts replaced, in the order a template writes
        a message out: reasoning, text, tool calls."""
        out = dict(message)
        for name in ("reasoning", "reasoning_content", "content"):
            if name in out:
                out[name] = self._content(out[name], replaced)
        calls = out.get("tool_calls")
        if isinstance(calls, list):
            swapped = []
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                    arguments = self._text(function["arguments"], replaced)
                    call = {**call, "function": {**function, "arguments": arguments}}
                swapped.append(call)
            out["tool_calls"] = swapped
        return out

    def swap(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, list[str]]:
        """*messages* and *tools* with each text of the tokenizer's own tokens
        replaced by the stand-in, and the replaced texts in the order their
        stand-ins come out of the rendering.  A conversation that holds none
        comes back as the very objects it was given."""
        head: list[str] = []
        rest: list[str] = []
        defined: list[str] = []
        swapped = []
        at_head = True
        for message in messages:
            at_head = at_head and message.get("role") in ("system", "developer")
            swapped.append(self._message(message, head if at_head else rest))
        new_tools = self._walk(list(tools), defined) if tools else None
        replaced = (defined + head if self.tools_first else head + defined) + rest
        if not replaced:
            # The very objects: a request without such a text is the request it was.
            return messages, tools, []  # type: ignore[return-value]
        return swapped, new_tools, replaced

    def splice(self, ids: list[int], replaced: Sequence[str]) -> list[int]:
        """*ids*, a rendering of a swapped conversation, with each stand-in
        token replaced by the ordinary spelling of the text it stands for."""
        count = ids.count(self.stand_in_id)
        if count != len(replaced):
            raise HarnessFaultError(
                f"a rendering holds {count} stand-in tokens ({self.stand_in_id}) for "
                f"{len(replaced)} texts that were replaced: the texts of the tokenizer's own "
                "tokens cannot be put back as plain text"
            )
        if not count:
            return ids
        out: list[int] = []
        texts = iter(replaced)
        for token in ids:
            if token == self.stand_in_id:
                out.extend(self.spelled[next(texts)])
            else:
                out.append(token)
        return out


_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look a word up.",
        "parameters": {"type": "object", "properties": {"word": {"type": "string"}}},
    },
}


def plain_text_verdict(
    render: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], list[int]],
    tokens_of: Callable[[str], list[int]],
    text_of: Callable[[list[int]], str],
    added: Mapping[str, int],
    written: Sequence[int],
) -> dict[str, Any]:
    """What a run needs to read the environment's text as plain text, for a
    tokenizer whose own tokens are *added* (text to ID).

    *render* renders a conversation with tools on the server's tokenize
    route, *tokens_of* tokenizes a text and *text_of* turns tokens back into
    text.  *written* are tokens the template is known to write or a reply's
    reading relies on: none of them is taken as the stand-in.

    ``ok`` is true when everything was established; ``reason`` says what was
    not.  A token of *added* the server does not write for its text is left
    out and named (``left_out``).
    """
    verdict: dict[str, Any] = {"ok": False, "reason": None}

    def failed(reason: str) -> dict[str, Any]:
        verdict["reason"] = reason
        return verdict

    known: dict[str, int] = {}
    left_out: list[str] = []
    for text, token in added.items():
        if tokens_of(text) == [token]:
            known[text] = token
        else:
            left_out.append(text)
    if not known:
        return failed("the server writes none of the named tokens for its text")
    ids = set(known.values())
    spelled: dict[str, list[int]] = {}
    for text in known:
        for cut in range(len(text) // 2, 0, -1):
            halves = tokens_of(text[:cut]) + tokens_of(text[cut:])
            if not (set(halves) & ids) and text_of(halves) == text:
                spelled[text] = halves
                break
        else:
            return failed(f"no ordinary spelling was found for the text of token {known[text]}")
    # The stand-in: the template writes it nowhere in a conversation with a
    # system message, tools, a call and its result.
    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Look up alpha."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_check",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"word": "alpha"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_check", "content": "alpha: first letter"},
    ]
    plain = render(conversation, [_TOOL])
    taken = set(written) | set(plain)
    free = [
        text
        for text, token in sorted(known.items(), key=lambda item: -item[1])
        if token not in taken
    ]
    if not free:
        return failed(
            "the template writes every one of the tokenizer's own tokens: none can stand in"
        )
    stand_in = free[0]
    stand_id = known[stand_in]
    # Where the template writes the tool definitions: one stand-in in the
    # system message, two in a tool's description, three in the user's.
    marked_tool = {**_TOOL, "function": {**_TOOL["function"], "description": stand_in * 2}}
    probe = [
        {"role": "system", "content": "Be brief. " + stand_in},
        {"role": "user", "content": "Look up alpha. " + stand_in * 3},
    ]
    runs: list[int] = []
    previous = -2
    for at, token in enumerate(render(probe, [marked_tool])):
        if token == stand_id:
            if at == previous + 1:
                runs[-1] += 1
            else:
                runs.append(1)
            previous = at
    if runs not in ([1, 2, 3], [2, 1, 3]):
        return failed(
            "the template does not write a system message, the tool definitions and a user "
            f"message once each, in an order this code knows (stand-in tokens came as {runs})"
        )
    route = PlainText(
        stand_in=stand_in,
        stand_in_id=stand_id,
        spelled={k: tuple(v) for k, v in spelled.items()},
        tools_first=runs[0] == 2,
    )
    # The whole of it once: every text in a system message, a tool's
    # description and a tool result comes out as its characters, and the
    # rendering holds the tokens it holds without them and no other.
    every = " ".join(known)
    loaded = [dict(m) for m in conversation]
    loaded[0]["content"] = "Be brief. " + every
    loaded[3]["content"] = "alpha: " + every + " first letter"
    loaded_tool = {
        **_TOOL,
        "function": {**_TOOL["function"], "description": "Look a word up. " + every},
    }
    messages, tools, replaced = route.swap(loaded, [loaded_tool])
    try:
        spliced = route.splice(render(messages, tools), replaced)
    except HarnessFaultError as fault:
        return failed(str(fault))
    if sorted(t for t in spliced if t in ids) != sorted(t for t in plain if t in ids):
        return failed(
            "a rendering with the texts replaced and put back does not hold the tokens the "
            "template writes and no others"
        )
    if text_of(spliced).count(every) != 3:
        return failed("a rendering with the texts replaced and put back does not read as the texts")
    verdict.update(
        ok=True,
        stand_in=stand_in,
        stand_in_id=stand_id,
        tools_first=route.tools_first,
        tokens=len(known),
        spelled=spelled,
        left_out=left_out,
    )
    return verdict
