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
goes to the tokenize route, each such text in it is cut: a stand-in, a token
of the tokenizer that the template never writes, is put after the text's
first character.  The tokenizer then meets the first character, the stand-in
and the rest, none of which is the token's text, and writes the stand-in as
the one token it is.  After the rendering every stand-in token is taken out
again.  What is left is the text's own characters as ordinary tokens: the
model sees what a person reading the file sees, and no token the environment
could not have typed as text.

Nothing has to be put back and nothing depends on where a template writes
what, so it does not matter in which order a server renders a tool
definition's fields or which of them it leaves out.  What it takes is that no
token's text can come about across a cut, which the check before a run
establishes once for the tokenizer's whole set of texts
(:func:`plain_text_verdict`).

Every text of a conversation is cut: the system prompt, user messages, tool
results, tool definitions, and the model's own turns where they are rendered
again from their text.  A model that wrote the characters of a token wrote
characters.  Its turn in the rollout's sequence is the tokens it generated
and is never touched.  A tool call's arguments are cut as the values their
JSON holds, which is how a server renders them.  The walk that finds every
text is the one the escape on the routes that take messages uses
(:mod:`turnstone.core.text_walk`): strings and keys however deep, the text
parts of a message as the one text a template makes of them, and nothing
skipped but the data of an image or a sound in a message's content.

The stand-ins a rendering holds are also counted, as an alarm.  In the part
of a rendering a prompt is built from, which holds what the environment wrote
and nothing of the model's, there can be no more stand-in tokens than cuts
were made.  More means the template wrote one or a text went uncut: a fault
of this code or of the check, raised as :class:`HarnessFaultError`.  Fewer is
no fault: a template need not write everything it is given.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

from turnstone.core.control_text import can_meet
from turnstone.core.text_walk import map_message, map_strings
from turnstone.rollout.settings import SetupError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from turnstone.core.text_walk import Edit

# What a control token's text looks like where nothing else says that a
# token is one: a word in angle or square brackets.
_MARKER_LOOK = re.compile(r"<[^\s<>]{1,64}>|\[[^\s\[\]]{1,64}\]")


class HarnessFaultError(RuntimeError):
    """The harness found itself at fault: something it guarantees did not hold,
    and nothing the model or the environment wrote can have caused it.  The run
    ends as ``harness_fault``, which no caller should run again without a
    look."""


@dataclass(frozen=True)
class Swapped:
    """A conversation with its texts cut, and what was cut."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    # Cuts made per message, and in the tool definitions.
    per_message: list[int]
    in_tools: int
    # The texts cut in what the environment wrote (every message that is not
    # the model's own turn, and the tool definitions), each with how often.
    environment: dict[str, int]


@dataclass(frozen=True)
class PlainText:
    """What :func:`plain_text_verdict` established: all a run needs to have
    a conversation's text read as plain text."""

    # The stand-in: a token of the tokenizer the template never writes.
    stand_in: str
    stand_in_id: int
    # The texts the tokenizer has tokens for.
    texts: tuple[str, ...]

    @classmethod
    def from_verdict(cls, verdict: Any) -> PlainText:
        """What a passed check's verdict describes.  Raises
        :class:`SetupError` for one that says the server cannot and for one
        that does not hold what a passed check found."""
        if not isinstance(verdict, dict) or not verdict.get("ok"):
            said = verdict.get("reason") if isinstance(verdict, dict) else None
            raise SetupError(
                "the text of the tokenizer's own tokens cannot be read as plain text: "
                f"{said or 'the check of the server did not establish how'}"
            )
        texts = verdict.get("texts")
        text, token = verdict.get("stand_in"), verdict.get("stand_in_id")
        if (
            not isinstance(text, str)
            or isinstance(token, bool)
            or not isinstance(token, int)
            or not isinstance(texts, list)
            or not texts
            or any(not isinstance(t, str) or not t for t in texts)
            or text not in texts
            or verdict.get("cut_checked") is not True
        ):
            raise SetupError(
                "the given check of the server does not hold what it found about the "
                "tokenizer's own tokens (plain_text: the texts, a stand-in among them, and "
                "that a cut keeps them apart): check the server again"
            )
        return cls(stand_in=text, stand_in_id=token, texts=tuple(texts))

    @cached_property
    def _every(self) -> re.Pattern[str]:
        # The longest text first, though none holds another.
        return re.compile("|".join(re.escape(t) for t in sorted(self.texts, key=len, reverse=True)))

    def cut_edits(self, text: str, seen: Counter[str]) -> list[Edit]:
        """The stand-in after the first character of every text of a token
        in *text*; *seen* is told which."""
        if not any(first in text for first in self._firsts):
            return []
        edits: list[Edit] = []
        for found in self._every.finditer(text):
            seen[found.group(0)] += 1
            edits.append((found.start() + 1, found.start() + 1, self.stand_in))
        return edits

    @cached_property
    def _firsts(self) -> frozenset[str]:
        return frozenset(text[0] for text in self.texts)

    def _cutter(self, seen: Counter[str]) -> Callable[[str], list[Edit]]:
        """The rule that cuts a text and tells *seen* what it cut."""

        def rule(text: str) -> list[Edit]:
            return self.cut_edits(text, seen)

        return rule

    def swap(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
    ) -> Swapped:
        """*messages* and *tools* with every text of a token cut.  A
        conversation that holds none comes back as the very objects it was
        given."""
        environment: Counter[str] = Counter()
        per_message: list[int] = []
        swapped: list[dict[str, Any]] = []
        for message in messages:
            seen: Counter[str] = Counter()
            swapped.append(map_message(message, self._cutter(seen)))
            per_message.append(sum(seen.values()))
            if message.get("role") != "assistant":
                environment.update(seen)
        defined: Counter[str] = Counter()
        new_tools = map_strings(list(tools), self._cutter(defined)) if tools else None
        environment.update(defined)
        in_tools = sum(defined.values())
        if not in_tools and not any(per_message):
            # The very objects: a request without such a text is the request it was.
            return Swapped(messages, tools, per_message, 0, {})  # type: ignore[arg-type]
        return Swapped(swapped, new_tools, per_message, in_tools, dict(environment))

    def strip(self, ids: list[int]) -> list[int]:
        """*ids*, a rendering of a cut conversation, without its stand-in
        tokens; the very list where it holds none."""
        if self.stand_in_id not in ids:
            return ids
        return [token for token in ids if token != self.stand_in_id]

    def surplus(self, raw: Sequence[int], kept_before: int, cuts: int) -> int:
        """How many more stand-in tokens *raw*, a rendering as it came, holds
        than *cuts* were made, in the part that follows its first
        *kept_before* tokens that are no stand-ins; 0 or less is no fault."""
        at = 0
        kept = 0
        while at < len(raw) and kept < kept_before:
            kept += raw[at] != self.stand_in_id
            at += 1
        return sum(1 for token in raw[at:] if token == self.stand_in_id) - cuts


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
    required: Sequence[int] = (),
) -> dict[str, Any]:
    """What a run needs to read a conversation's text as plain text, for a
    tokenizer whose own tokens are *added* (text to ID).

    *render* renders a conversation with tools on the server's tokenize
    route, *tokens_of* tokenizes a text and *text_of* turns tokens back into
    text.  *written* are tokens the template is known to write or a reply's
    reading relies on, and the tokens it writes around and for an image where
    those are known: none of them is taken as the stand-in.  *required* are
    those of them that are control tokens for certain (the format's markers
    that are one token, the end-of-turn token): *added* must name each.

    ``ok`` is true when everything was established; ``reason`` says what was
    not.  What is established:

    - which texts the server writes a token for (``texts``).  A token of
      *added* whose text the server writes as several tokens is left out and
      named (``left_out``): it is no token of this server's.  One whose text
      the server writes as ANOTHER single token fails the check: the
      description is not of this server's tokenizer;
    - that *added* is whole as far as the template shows it: every token of
      *required*, and every other token the template writes whose text has
      the look of a control token (a word in angle or square brackets that
      the server writes as that one token), is named.  A description that
      leaves one out is incomplete or stale, and its text would go uncut;
    - a stand-in among them that the template writes nowhere in a
      conversation with a system message, tools, a call, its result and a
      message of two text parts;
    - that a cut keeps the texts apart (``cut_checked``): none can come about
      across a cut by the texts themselves (:func:`can_meet`), and for each
      one the server, given the cut text, writes the stand-in once, no other
      token of the set, and tokens that read as the text;
    - the whole of it once: a conversation with every text in a system
      message, a user message, a tool's description, a tool result and both
      parts of a content list of two text parts, and with one text begun in
      the first of those parts and ended in the second, comes out with the
      tokens the template writes and no others, and reads as the texts.
    """
    left_out: list[str] = []
    verdict: dict[str, Any] = {"ok": False, "reason": None, "left_out": left_out}

    def failed(reason: str) -> dict[str, Any]:
        verdict["reason"] = reason
        return verdict

    known: dict[str, int] = {}
    for text, token in added.items():
        got = tokens_of(text)
        if got == [token]:
            known[text] = token
        elif len(got) == 1:
            return failed(
                f"the description of the tokenizer names token {token} for the text {text!r}, "
                f"and the server writes token {got[0]} for it: the description is not of this "
                "server's tokenizer"
            )
        else:
            left_out.append(text)
    if not known:
        return failed("the server writes none of the named tokens for its text")
    meet = can_meet(list(known))
    if meet is not None:
        return failed(f"a cut does not keep the texts of the tokenizer's tokens apart: {meet}")
    ids = set(known.values())
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
        # A message whose content is a list of two text parts, which a
        # template writes one after the other.
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "And the next word,"},
                {"type": "text", "text": " beta."},
            ],
        },
    ]
    plain = render(conversation, [_TOOL])
    taken = set(written) | set(plain)
    # The description names every control token the template is seen to
    # write: one it leaves out is one whose text would never be cut.
    for token in sorted(taken - ids):
        certain = token in set(required)
        text = "" if certain else text_of([token])
        if certain or (_MARKER_LOOK.fullmatch(text) and tokens_of(text) == [token]):
            return failed(
                f"the template writes token {token}"
                + (f" ({text!r})" if text else "")
                + ", a control token that the description of the tokenizer does not name: the "
                "description is incomplete or not of this server's tokenizer"
            )
    free = [
        t for t, token in sorted(known.items(), key=lambda item: -item[1]) if token not in taken
    ]
    if not free:
        return failed(
            "the template writes every one of the tokenizer's own tokens: none can stand in"
        )
    stand_in = free[0]
    stand_id = known[stand_in]
    for text in known:
        cut = tokens_of(text[0] + stand_in + text[1:])
        rest = [token for token in cut if token != stand_id]
        if cut.count(stand_id) != 1 or set(rest) & ids or text_of(rest) != text:
            return failed(
                f"the text of token {known[text]}, cut after its first character, does not come "
                "out as the stand-in and ordinary tokens that read as the text"
            )
    route = PlainText(stand_in=stand_in, stand_in_id=stand_id, texts=tuple(known))
    every = " ".join(known)
    loaded = [dict(m) for m in conversation]
    loaded[0]["content"] = "Be brief. " + every
    loaded[1]["content"] = "Look up alpha. " + every
    loaded[3]["content"] = "alpha: " + every + " first letter"
    # In both parts of the list, and one text begun in the first part and
    # ended in the second: a template that writes the parts one after the
    # other makes the token's text of the two halves.
    split = max(known, key=len)
    half = len(split) // 2
    loaded[4]["content"] = [
        {"type": "text", "text": "And the next word, " + every + " " + split[:half]},
        {"type": "text", "text": split[half:] + " " + every + " beta."},
    ]
    loaded_tool = {
        **_TOOL,
        "function": {**_TOOL["function"], "description": "Look a word up. " + every},
    }
    swapped = route.swap(loaded, [loaded_tool])
    raw = render(swapped.messages, swapped.tools)
    if route.surplus(raw, 0, sum(swapped.per_message) + swapped.in_tools) > 0:
        return failed("a rendering holds more stand-in tokens than cuts were made")
    stripped = route.strip(raw)
    if sorted(t for t in stripped if t in ids) != sorted(t for t in plain if t in ids):
        return failed(
            "a rendering with the texts cut does not hold the tokens the template writes and "
            "no others"
        )
    # The system message, the user message, the tool's description, the tool
    # result and the two parts of the list.
    if text_of(stripped).count(every) != 6:
        return failed("a rendering with the texts cut does not read as the texts")
    verdict.update(
        ok=True,
        stand_in=stand_in,
        stand_in_id=stand_id,
        texts=list(known),
        cut_checked=True,
    )
    return verdict
