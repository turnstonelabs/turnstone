"""How a model writes a reply, and reading one without the server's help.

A chat-completions server may read a reply itself and hand back reasoning,
text and tool calls as separate fields.  A completions route hands back the
generated text and nothing else, and a server may run with no reader of its
own.  A rollout that sends its prompts as token IDs
(:mod:`turnstone.rollout.idprompt`) therefore reads the reply here.

A :class:`ReplyFormat` describes one way of writing a reply and reads it.  One
format is defined, :data:`PARAMETER_BLOCKS`: reasoning between two markers,
then text, then tool calls written as tagged blocks with one tagged block per
argument::

    reasoning
    </think>

    text

    <tool_call>
    <function=NAME>
    <parameter=KEY>
    VALUE
    </parameter>
    </function>
    </tool_call>

A server with another format needs another :class:`ReplyFormat`; nothing else
in the package names a marker.

What a reader is given is not plain text but *pieces*: text, and
:class:`Marker` items for the markers the model generated as one token of
their own.  The four outer markers of this format are single tokens in the
vocabulary of the models that use it, and a server that reads replies itself
takes them for markers only as those tokens: the same characters spelled with
ordinary tokens are text.  A model that writes a file documenting its own call
format produces exactly that.  :func:`pieces_from_tokens` and
:func:`pieces_from_chunks` build pieces from what a server returns.

The reading is the one a server of this format gives in its non-streamed
reply, down to the type of every argument value; ``docs/rollout.md`` lists
what is compared and where a streamed reply of such a server differs.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence

_NL = chr(10)

# The states a reader is in once the reasoning is over.
_CONTENT, _PREAMBLE, _NAME, _ARGUMENTS, _BETWEEN = range(5)
# A name spelled in a call's body, for a call whose function tag names nothing.
# Why it takes time in proportion to the body: see ``ParameterBlocks._patterns``.
_NAMED = re.compile(r'"name"\s*+:\s*+"([^"]*+)"')


@dataclass(frozen=True)
class Marker:
    """A marker of the format that the model generated as one token of its own.

    ``name`` is the marker's name in the format's description
    (:attr:`ReplyFormat.token_markers`), not its text.
    """

    name: str


Piece = str | Marker


@dataclass(frozen=True)
class ParsedCall:
    """One tool call: the function's name and its arguments as a JSON object text."""

    name: str
    arguments: str


@dataclass(frozen=True)
class ParsedReply:
    """What one reply holds.  ``None`` stands for a part that is not there.
    Every text in it can be carried by a request body."""

    reasoning: str | None = None
    content: str | None = None
    calls: tuple[ParsedCall, ...] = ()


class ReplyFormat(Protocol):
    """One way of writing a reply."""

    @property
    def name(self) -> str:
        """The name a run's configuration asks for the format by."""
        ...

    @property
    def token_markers(self) -> Mapping[str, str]:
        """The markers a model of this format generates as one token each:
        name to text.  A server is asked for each one's token ID."""
        ...

    def opens_in_reasoning(self, markers: Sequence[str]) -> bool:
        """Whether a reply begins inside reasoning, given the names of the
        markers the generation prompt holds, in order."""
        ...

    def read(
        self,
        pieces: Iterable[Piece],
        *,
        tools: Sequence[Mapping[str, Any]] | None,
        in_reasoning: bool,
        text_markers: Collection[str] = (),
    ) -> ParsedReply:
        """Read one reply.

        *tools* are the tool definitions the request offered; they give each
        argument its type.  *in_reasoning* says whether the reply begins
        inside reasoning.  *text_markers* names the token markers the server
        has no single token for: those are taken from the text.
        """
        ...

    def written_calls(self, text: str) -> list[tuple[str, dict[str, str]] | None]:
        """The tool calls in *text* with every argument as the text the model
        wrote, ``None`` for a call that is not written the way the format's
        template writes one."""
        ...


def pieces_from_tokens(
    token_bytes: Sequence[bytes], token_ids: Sequence[int], marker_ids: Mapping[str, int]
) -> list[Piece]:
    """Pieces of a reply whose every token's bytes are known: exact.

    *marker_ids* maps a marker's name to its token ID.
    """
    names = {token_id: name for name, token_id in marker_ids.items()}
    pieces: list[Piece] = []
    run = bytearray()
    for data, token_id in zip(token_bytes, token_ids, strict=True):
        name = names.get(token_id)
        if name is None:
            run.extend(data)
            continue
        if run:
            pieces.append(bytes(run).decode("utf-8", errors="replace"))
            run.clear()
        pieces.append(Marker(name))
    if run:
        pieces.append(bytes(run).decode("utf-8", errors="replace"))
    return pieces


def pieces_from_chunks(
    chunks: Iterable[tuple[str, Sequence[int]]],
    marker_ids: Mapping[str, int],
    marker_text: Mapping[str, str],
) -> tuple[list[Piece], int, int]:
    """Pieces of a reply that arrived in parts, each with its text and its
    token IDs; the number of marker tokens whose text was not in their part;
    and the number whose place in their part's text is not certain.

    A part's text is the text of its tokens, in their order, so a marker token
    is bound to the occurrence of its text that stands where the token does:

    - where a part holds the texts of its marker tokens and no such text
      besides, each occurrence is its token.  That is nearly every part;
    - where a part holds a marker's text more often than its token (the same
      characters spelled with ordinary tokens, complete within that part), a
      token with no other token before it in the part is the first
      occurrence, one with none after it is the last, and one with no token
      between it and a marker token already bound stands right beside that
      one;
    - a marker token with other tokens on both sides, in a part that holds its
      text more often than its token, cannot be told from the spelled-out
      text by the part alone: the texts of the other tokens are not known
      here.  It is taken for the first occurrence left, and counted.

    A marker token whose text is not in its part at all is counted and left
    out.  Every part is gone through a bounded number of times.
    """
    names = {token_id: name for name, token_id in marker_ids.items()}
    scan = _marker_scan(tuple(sorted({marker_text[name] for name in marker_ids})))
    by_text = {marker_text[name]: name for name in marker_ids}
    pieces: list[Piece] = []
    # The text since the last marker, in the parts it came in: joined once, at
    # the next marker, so that a long text in many parts is copied once.
    run: list[str] = []
    missing = uncertain = 0

    for text, token_ids in chunks:
        wanted = [(at, names[t]) for at, t in enumerate(token_ids) if t in names]
        if not wanted:
            if text:
                run.append(text)
            continue
        found = [(m.start(), by_text[m.group()]) for m in scan.finditer(text)]
        if [name for _at, name in found] == [name for _at, name in wanted]:
            spans = found
        else:
            spans, lost, unsure = _bind(text, len(token_ids), wanted, found, marker_text)
            missing += lost
            uncertain += unsure
        position = 0
        for start, name in spans:
            if start > position:
                run.append(text[position:start])
            if run:
                pieces.append("".join(run))
                run.clear()
            pieces.append(Marker(name))
            position = start + len(marker_text[name])
        if position < len(text):
            run.append(text[position:])
    if run:
        pieces.append("".join(run))
    return pieces, missing, uncertain


@lru_cache(maxsize=16)
def _marker_scan(texts: tuple[str, ...]) -> re.Pattern[str]:
    """A scanner for the marker texts: a choice between fixed texts, the
    longest that begins at a place first."""
    return re.compile("|".join(re.escape(text) for text in sorted(texts, key=len, reverse=True)))


def _bind(
    text: str,
    tokens: int,
    wanted: list[tuple[int, str]],
    found: list[tuple[int, str]],
    marker_text: Mapping[str, str],
) -> tuple[list[tuple[int, str]], int, int]:
    """The place of each marker token of one part whose text does not hold
    exactly its marker tokens' texts: the spans bound, in order, the number
    of tokens without a text and the number whose place is not certain.

    *wanted* are the marker tokens as (index among the part's *tokens*, name),
    *found* the occurrences of marker texts as (offset, name), both in order.
    """
    bound: dict[int, int] = {}  # index in ``wanted`` -> offset
    # From the end: a marker token with no token after it ends the text, and
    # one with no token between it and that one stands right before it.
    end, last = len(text), len(wanted)
    following = tokens
    while last and wanted[last - 1][0] == following - 1:
        mark = marker_text[wanted[last - 1][1]]
        if not text.endswith(mark, 0, end):
            break
        end -= len(mark)
        last -= 1
        following = wanted[last][0]
        bound[last] = end
    # From the start, through the occurrences that lie before those.
    starts: dict[str, list[int]] = {}
    for offset, name in found:
        if offset < end:
            starts.setdefault(name, []).append(offset)
    taken = dict.fromkeys(starts, 0)  # per name, the occurrences passed
    still = dict.fromkeys(starts, 0)  # per name, the tokens still to be bound
    for _at, name in wanted[:last]:
        still[name] = still.get(name, 0) + 1
    missing = uncertain = 0
    cursor, previous = 0, -1
    for number, (at, name) in enumerate(wanted[:last]):
        offsets = starts.get(name, [])
        index = taken.get(name, 0)
        while index < len(offsets) and offsets[index] < cursor:
            index += 1
        still[name] -= 1
        # The occurrences this token can be: those left, less one for each
        # later token of its kind.
        choices = len(offsets) - index - still[name]
        if choices < 1:
            taken[name] = index
            missing += 1
            continue
        if choices > 1 and at - previous > 1:
            # Other tokens stand before it, and their text is not known.
            uncertain += 1
        bound[number] = offsets[index]
        taken[name] = index + 1
        cursor = offsets[index] + len(marker_text[name])
        previous = at
    spans = [(bound[number], wanted[number][1]) for number in sorted(bound)]
    return spans, missing, uncertain


def _lenient(marker: str) -> str:
    """A pattern for *marker* that allows white space between its parts
    (``<``, ``/``, ``=``, ``>`` and the word between them).

    A run of white space keeps what it took (``*+``): it is never gone through
    a second time to try a shorter run.  That reads the same as long as no
    part begins with white space itself, which is refused here.
    """
    parts = re.findall(r"[<>/=]|[^<>/=]+", marker)
    if any(part[0].isspace() for part in parts):
        raise ValueError(
            f"the marker {marker!r} has white space at the start of one of its parts, which "
            "the white space allowed between the parts would always take"
        )
    return r"\s*+".join(re.escape(part) for part in parts)


# Other names schemas use for the JSON types.
_TYPE_NAMES = {
    "str": "string",
    "text": "string",
    "varchar": "string",
    "char": "string",
    "enum": "string",
    "int": "integer",
    "int32": "integer",
    "int64": "integer",
    "uint": "integer",
    "uint32": "integer",
    "uint64": "integer",
    "long": "integer",
    "short": "integer",
    "unsigned": "integer",
    "float": "number",
    "float32": "number",
    "float64": "number",
    "double": "number",
    "bool": "boolean",
    "dict": "object",
    "arr": "array",
    "list": "array",
    "sequence": "array",
}
# The order in which the types a schema allows are tried on a value's text.
_TYPE_ORDER = ("null", "integer", "number", "boolean", "object", "array", "string")


def schema_types(schema: Any) -> list[str]:
    """Every type a JSON schema allows for a value; ``["string"]`` where it
    names none.  Read from ``type``, from the values of ``enum`` and from the
    branches of ``anyOf``, ``oneOf`` and ``allOf``."""
    if not isinstance(schema, dict):
        return ["string"]
    found: set[str] = set()
    named = schema.get("type")
    if isinstance(named, str):
        found.add(named)
    elif isinstance(named, list):
        found.update(t for t in named if isinstance(t, str))
    choices = schema.get("enum")
    if isinstance(choices, list):
        for value in choices:
            if value is None:
                found.add("null")
            elif isinstance(value, bool):
                found.add("boolean")
            elif isinstance(value, int):
                found.add("integer")
            elif isinstance(value, float):
                found.add("number")
            elif isinstance(value, str):
                found.add("string")
            elif isinstance(value, list):
                found.add("array")
            elif isinstance(value, dict):
                found.add("object")
    for key in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            for branch in branches:
                found.update(schema_types(branch))
    return list(found) if found else ["string"]


def _finite(value: Any) -> bool:
    """Whether *value* can be written as JSON (no infinity, no not-a-number)."""
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return False
    return True


def _carried(text: str) -> str:
    """*text* as a request body can carry it.

    A model can write half of a surrogate pair as an escape in JSON, and a
    value read from that JSON then holds a character no request body can
    carry: the session could not send the turn back, and the run would end as
    a failure of the machinery over a text the model chose.  Each half that
    stands alone becomes the replacement character (U+FFFD), the same way
    every time; any other text is returned as it is.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text


def _loads(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return False, None


def typed_value(text: str, types: Iterable[str]) -> Any:
    """The value an argument's text stands for under the types its schema allows.

    The types are tried in a fixed order (null, integer, number, boolean,
    object, array, string) and the first that fits wins; a text none fits is
    read as JSON where it is JSON and stays text otherwise.  A whole number
    written for a ``number`` becomes an integer.
    """
    allowed = {_TYPE_NAMES.get(key, key) for key in (t.strip().lower() for t in types)}
    for kind in _TYPE_ORDER:
        if kind not in allowed:
            continue
        if kind == "null":
            if text.lower() == "null":
                return None
        elif kind == "string":
            return text
        elif kind == "integer":
            try:
                return int(text)
            except (ValueError, TypeError):
                pass
        elif kind == "number":
            try:
                number = float(text)
            except (ValueError, TypeError):
                continue
            if math.isfinite(number):
                return number if number != int(number) else int(number)
        elif kind == "boolean":
            lowered = text.lower().strip()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
        else:
            ok, parsed = _loads(text)
            if ok and _finite(parsed):
                return parsed
    ok, parsed = _loads(text)
    return parsed if ok and _finite(parsed) else text


def _typed(value: Any, schema: Mapping[str, Any]) -> tuple[Any, bool]:
    """*value* under *schema*, and whether that changed it."""
    if isinstance(value, str):
        typed = typed_value(value, schema_types(schema))
        return typed, typed is not value
    if isinstance(value, dict):
        nested = schema.get("properties")
        return value, isinstance(nested, dict) and _typed_object(value, nested)
    if isinstance(value, list):
        items = schema.get("items")
        if not isinstance(items, dict):
            return value, False
        changed = False
        for index, item in enumerate(value):
            typed, item_changed = _typed(item, items)
            if item_changed:
                value[index] = typed
                changed = True
        return value, changed
    typed = typed_value(json.dumps(value, ensure_ascii=False), schema_types(schema))
    return typed, type(typed) is not type(value) or typed != value


def _typed_object(arguments: dict[str, Any], properties: Mapping[str, Any]) -> bool:
    """Give every value of *arguments* its schema's type, in place."""
    changed = False
    for key, value in arguments.items():
        schema = properties.get(key)
        if not isinstance(schema, dict):
            continue
        typed, value_changed = _typed(value, schema)
        if value_changed:
            arguments[key] = typed
            changed = True
    return changed


def tool_properties(tools: Sequence[Mapping[str, Any]] | None, name: str) -> Mapping[str, Any]:
    """The ``properties`` of the first offered tool called *name*, ``{}`` without one."""
    for tool in tools or ():
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name") == name:
            parameters = function.get("parameters")
            properties = parameters.get("properties") if isinstance(parameters, dict) else None
            return properties if isinstance(properties, dict) else {}
    return {}


# What a marker does in a state: the state that follows and whether a call
# starts.  A marker with no entry for the state it meets is text there.
_MOVES: dict[tuple[int, str], tuple[int, bool]] = {
    # A second closing marker after the reasoning has ended is dropped.
    (_CONTENT, "reasoning_close"): (_CONTENT, False),
    (_CONTENT, "call_open"): (_PREAMBLE, True),
    # A function tag without the call marker before it starts a call too.
    (_CONTENT, "function_open"): (_NAME, True),
    (_PREAMBLE, "call_close"): (_CONTENT, False),
    (_PREAMBLE, "function_open"): (_NAME, False),
    (_NAME, "name_end"): (_ARGUMENTS, False),
    (_NAME, "function_close"): (_BETWEEN, False),
    (_ARGUMENTS, "function_close"): (_BETWEEN, False),
    (_BETWEEN, "call_close"): (_CONTENT, False),
    # A next call that follows without the closing marker.
    (_BETWEEN, "call_open"): (_PREAMBLE, True),
    (_BETWEEN, "function_open"): (_NAME, True),
}


@dataclass
class _Slot:
    """One call while it is read: its name and its body, each in the pieces it
    came in.  They are joined once, at the end: a body of many pieces is then
    copied once, not once per piece."""

    name: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParameterBlocks:
    """Tool calls as tagged blocks, one tagged block per argument, after
    reasoning that a marker closes.  See the module's description."""

    name: str = "parameter_blocks"
    reasoning_open: str = "<think>"
    reasoning_close: str = "</think>"
    call_open: str = "<tool_call>"
    call_close: str = "</tool_call>"
    function_open: str = "<function="
    function_close: str = "</function>"
    parameter_open: str = "<parameter="
    parameter_close: str = "</parameter>"
    name_end: str = ">"

    def __post_init__(self) -> None:
        # A description whose patterns cannot be built is refused when it is
        # made, not at the first reply.
        _ = self._patterns

    @property
    def token_markers(self) -> dict[str, str]:
        return {
            "reasoning_open": self.reasoning_open,
            "reasoning_close": self.reasoning_close,
            "call_open": self.call_open,
            "call_close": self.call_close,
        }

    def opens_in_reasoning(self, markers: Sequence[str]) -> bool:
        last = [m for m in markers if m in ("reasoning_open", "reasoning_close")]
        return bool(last) and last[-1] == "reasoning_open"

    # -- reading ---------------------------------------------------------------

    # Every pattern below is applied to text the model wrote, inside the model
    # call and with the interpreter held, so each has to take time in proportion
    # to the text and no more, whatever the text is:
    #
    # - ``scan`` is a choice between fixed texts: a bounded number of
    #   characters is looked at from each place;
    # - ``opening``, ``ending`` and ``_NAMED`` are fixed texts with runs
    #   between them, and every run keeps what it took (``*+``): none is gone
    #   through again to try a shorter one.  That reads nothing differently: a
    #   run of white space is followed by a text that does not begin with
    #   white space, and the run of a name by the one character it leaves out,
    #   so a shorter run could not match where the whole one did not.
    #
    # A search with one of these is then linear in the text.  An attempt
    # begins at a ``<`` (``_NAMED``: at a quote) and goes through a few fixed
    # texts and the runs between them, and a run ends before the place the
    # next attempt could begin: white space holds no ``<`` and no quote, a
    # name no quote.  So no character lies in the runs of attempts from two
    # places.  What is done with the matches (``_arguments``) moves forward
    # only.

    @cached_property
    def _patterns(
        self,
    ) -> tuple[dict[str, str], re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
        """The terminals by their text, a scanner for them, and the two
        patterns an argument block is found with: its opening tag up to where
        the key begins, and what ends its value (its closing tag, in the group
        ``closed``, or the opening tag of the next)."""
        terminals = {
            **self.token_markers,
            "function_open": self.function_open,
            "function_close": self.function_close,
            "parameter_open": self.parameter_open,
            "parameter_close": self.parameter_close,
            "name_end": self.name_end,
        }
        by_text = {text: name for name, text in terminals.items()}
        # The longest marker that begins at a place is the one that counts.
        longest_first = sorted(by_text, key=len, reverse=True)
        scan = re.compile("|".join(re.escape(text) for text in longest_first))
        opened = _lenient(self.parameter_open)
        opening = re.compile(opened + r"\s*+")
        ending = re.compile("(?P<closed>" + _lenient(self.parameter_close) + ")|" + opened)
        return by_text, scan, opening, ending

    def _terminals(
        self, pieces: Iterable[Piece], text_markers: Collection[str]
    ) -> Iterator[tuple[str | None, str]]:
        """*pieces* as ``(terminal name or None, text)``: ``None`` is plain text."""
        by_text, scan, _opening, _ending = self._patterns
        token_markers = self.token_markers
        for piece in pieces:
            if isinstance(piece, Marker):
                yield piece.name, token_markers[piece.name]
                continue
            position = 0
            for match in scan.finditer(piece):
                if match.start() > position:
                    yield None, piece[position : match.start()]
                name = by_text[match.group()]
                # A token marker spelled out in text is text, unless the
                # server has no token for it.
                is_text = name in token_markers and name not in text_markers
                yield (None if is_text else name), match.group()
                position = match.end()
            if position < len(piece):
                yield None, piece[position:]

    @staticmethod
    def _after_reasoning(
        terminals: Iterator[tuple[str | None, str]], reasoning: list[str]
    ) -> list[tuple[str | None, str]]:
        """Take the reasoning off the front of a reply that begins inside it.

        The reasoning runs to its closing marker, or to a call marker, which
        ends it and belongs to what follows; an opening marker inside it is
        dropped.  A closing marker anywhere after the reasoning has ended is
        dropped as well, inside a call included.
        """
        rest: list[tuple[str | None, str]] = []
        inside = True
        for terminal, text in terminals:
            if inside:
                if terminal == "reasoning_close":
                    inside = False
                elif terminal == "call_open":
                    inside = False
                    rest.append((terminal, text))
                elif terminal != "reasoning_open":
                    reasoning.append(text)
            elif terminal != "reasoning_close":
                rest.append((terminal, text))
        return rest

    def read(
        self,
        pieces: Iterable[Piece],
        *,
        tools: Sequence[Mapping[str, Any]] | None,
        in_reasoning: bool,
        text_markers: Collection[str] = (),
    ) -> ParsedReply:
        terminals = self._terminals(pieces, text_markers)
        reasoning: list[str] = []
        if in_reasoning:
            terminals = iter(self._after_reasoning(terminals, reasoning))
        state = _CONTENT
        before: list[str] = []  # text before the first call
        after: list[str] = []  # text after a call began
        slots: list[_Slot] = []
        for terminal, text in terminals:
            move = _MOVES.get((state, terminal)) if terminal is not None else None
            if move is not None:
                state, starts_call = move
                if starts_call:
                    slots.append(_Slot())
                continue
            # Plain text, or a marker that means nothing here.
            if state == _CONTENT:
                (after if slots else before).append(text)
            elif state == _NAME:
                slots[-1].name.append(text)
            elif state == _ARGUMENTS:
                slots[-1].body.append(text)
            # Between a call's markers and its function block nothing is kept.
        written: list[tuple[str, str]] = []
        for slot in slots:
            name, body = "".join(slot.name), "".join(slot.body)
            # A call whose function tag names nothing takes a name its body spells.
            written.append((name or self._named_in_body(body), body))
        calls = self._calls(written, tools)
        # Text after a call is kept only where no call had a name.
        named = any(name for name, _body in written)
        content = "".join(before) + ("" if named else "".join(after))
        if slots and not content.strip():
            content = ""
        if calls:
            content = content.strip()
        if not tools:
            # A request that offered no tools is given no calls.
            calls = []
        return ParsedReply(
            reasoning=_carried("".join(reasoning)) or None,
            content=_carried(content) or None,
            calls=tuple(ParsedCall(_carried(c.name), _carried(c.arguments)) for c in calls),
        )

    @staticmethod
    def _named_in_body(body: str) -> str:
        found = _NAMED.search(body)
        return found.group(1) if found else ""

    def _calls(
        self, written: list[tuple[str, str]], tools: Sequence[Mapping[str, Any]] | None
    ) -> list[ParsedCall]:
        """The calls among *written*, each the name and the body of one."""
        calls: list[ParsedCall] = []
        for written_name, body in written:
            if not written_name and not body:
                continue
            name: Any = written_name.strip()
            stripped = body.strip()
            if not name and stripped:
                name, arguments = self._json_call(stripped)
            elif stripped:
                arguments = json.dumps(self._arguments(body), ensure_ascii=False)
            else:
                arguments = "{}"
            if not isinstance(name, str) or not name:
                continue
            calls.append(ParsedCall(name, self._with_types(arguments, name, tools)))
        return calls

    def _arguments(self, body: str) -> dict[str, str]:
        """Each argument's text in a call's body.

        An argument is its opening tag, the key (from the first character that
        is no white space to the first closing bracket), and as its value
        everything up to its closing tag or to the opening tag of the next.
        The line break after the opening tag and the one before the closing
        tag belong to the format; an argument written twice counts as written
        last; one whose closing tag never came is left out.

        The body is gone through once, from left to right.
        """
        _by_text, _scan, opening, ending = self._patterns
        found: dict[str, str] = {}
        at = 0
        while True:
            opened = opening.search(body, at)
            if opened is None:
                break
            key_end = body.find(self.name_end, opened.end())
            if key_end < 0:
                # No closing bracket is left for a later tag either.
                break
            value_start = key_end + len(self.name_end)
            ended = ending.search(body, value_start)
            if ended is None:
                # Nothing ends this value, nor that of a tag written inside
                # this one's key, and no tag comes after it.
                break
            value = body[value_start : ended.start()]
            if value.startswith(_NL):
                value = value[1:]
            if value.endswith(_NL):
                value = value[:-1]
            found[body[opened.end() : key_end]] = value
            # A closing tag is passed; the opening tag of the next argument is
            # where the search goes on.
            at = ended.end() if ended.group("closed") is not None else ended.start()
        return found

    @staticmethod
    def _json_call(body: str) -> tuple[Any, str]:
        """A call with white space for a name and a JSON object for a body:
        the object's name and arguments."""
        ok, parsed = _loads(body)
        if not ok or not isinstance(parsed, dict):
            return "", body
        name = parsed.get("name", "")
        for key in ("arguments", "parameters"):
            if key in parsed:
                value = parsed[key]
                return name, value if isinstance(value, str) else json.dumps(
                    value, ensure_ascii=False
                )
        rest = {k: v for k, v in parsed.items() if k != "name"}
        return name, json.dumps(rest, ensure_ascii=False)

    @staticmethod
    def _with_types(arguments: str, name: str, tools: Sequence[Mapping[str, Any]] | None) -> str:
        """*arguments* with every value of the type the tool's schema gives it."""
        if not tools:
            return arguments
        ok, parsed = _loads(arguments)
        if not ok or not isinstance(parsed, dict):
            return arguments
        properties = tool_properties(tools, name)
        if not properties or not _typed_object(parsed, properties):
            return arguments
        return json.dumps(parsed, ensure_ascii=False)

    # -- arguments as written --------------------------------------------------

    def _written_call(self, block: str) -> tuple[str, dict[str, str]] | None:
        start = block.find(self.function_open)
        if start < 0:
            return None
        name_end = block.find(self.name_end, start)
        end = block.rfind(self.function_close)
        if name_end < 0 or end < name_end:
            return None
        name = block[start + len(self.function_open) : name_end]
        body = block[name_end + 1 : end]
        closing = _NL + self.parameter_close
        values: dict[str, str] = {}
        at = 0
        while True:
            opened = body.find(self.parameter_open, at)
            if opened < 0:
                # Only white space may stand outside the blocks.
                return (name, values) if not body[at:].strip() else None
            if body[at:opened].strip():
                return None
            key_end = body.find(self.name_end, opened)
            if key_end < 0:
                return None
            key = body[opened + len(self.parameter_open) : key_end]
            value_start = key_end + 1
            closed = body.find(closing, value_start)
            if not body.startswith(_NL, value_start) or closed < value_start or key in values:
                return None
            # An empty value is written as one line break shared by both markers.
            values[key] = body[value_start + 1 : closed] if closed > value_start else ""
            at = closed + len(closing)

    def written_calls(self, text: str) -> list[tuple[str, dict[str, str]] | None]:
        """The tool calls in *text*, in order: ``(name, {argument: text})`` each.

        A value is everything between the line break after its opening tag and
        the line break before its closing tag, as written, line breaks inside
        it included.  ``None`` stands for a call whose text does not have the
        shape the format's template writes.
        """
        calls: list[tuple[str, dict[str, str]] | None] = []
        at = 0
        while True:
            opened = text.find(self.call_open, at)
            if opened < 0:
                return calls
            closed = text.find(self.call_close, opened)
            if closed < 0:
                calls.append(None)
                return calls
            calls.append(self._written_call(text[opened + len(self.call_open) : closed]))
            at = closed + len(self.call_close)


PARAMETER_BLOCKS = ParameterBlocks()

_FORMATS: dict[str, ReplyFormat] = {PARAMETER_BLOCKS.name: PARAMETER_BLOCKS}


def reply_format(name: str) -> ReplyFormat:
    """The format called *name*; ``ValueError`` names the known ones otherwise."""
    try:
        return _FORMATS[name]
    except KeyError:
        raise ValueError(
            f"no reply format is called {name!r}; known: {', '.join(sorted(_FORMATS))}"
        ) from None
