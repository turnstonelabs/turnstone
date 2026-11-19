"""Every text of a conversation, changed by one rule: the walk shared by what
changes text before it is sent.

Two things rewrite the text of a request before a server tokenizes it: the
escape of control-token text on the routes that take messages
(:mod:`turnstone.core.control_text`), and a rollout's cut of the same text
before a conversation is rendered (:mod:`turnstone.rollout.plaintext`).  Both
must reach the same places, so both walk with this module.

A rule is a function from a text to its edits (:data:`Edit`): where to take
characters out and what to put in.  The walk applies it to

- every string and every key of an object, however deep.  The walk keeps its
  own stack, so no depth of nesting ends it;
- a call's ``arguments``, where they are a text: the string literals of the
  JSON in it are changed as the values they decode to and written back as
  literals, so JSON stays JSON whatever it holds
  (:func:`map_json_text`);
- the text parts of a message's content as the one text a template makes of
  them: parts that follow each other are searched joined, and each keeps its
  share of the result.

One thing is left as it is: a part of a message's ``content`` list that holds
data (an image, a sound).  Only there: an object elsewhere that happens to say
``"type": "image"``, such as a tool's schema or an argument, is walked like
any other.

Whatever holds nothing to change comes back as the very object it was.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

# One edit of a text: the characters from ``start`` to ``end`` are replaced by
# ``put``.
Edit = tuple[int, int, str]

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    # A rule: the edits to make in a text, in order, none overlapping another.
    Rule = Callable[[str], list[Edit]]

# The parts of a message's content that hold data, not text.
DATA_PARTS = frozenset({"image_url", "image", "input_audio", "audio_url", "video_url"})
# The keys whose text is JSON written out: a call's arguments.
JSON_KEYS = frozenset({"arguments"})

# One string literal of a JSON text.
_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)


def apply_edits(text: str, edits: Sequence[Edit]) -> str:
    """*text* with *edits* made; the very object where there are none."""
    if not edits:
        return text
    out: list[str] = []
    at = 0
    for start, end, put in edits:
        out.append(text[at:start])
        out.append(put)
        at = end
    out.append(text[at:])
    return "".join(out)


def _literal(value: str) -> str:
    """*value* as a JSON string literal: its characters kept as they are where
    they can be sent, escaped where they cannot (half of a surrogate pair)."""
    written = json.dumps(value, ensure_ascii=False)
    try:
        written.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value)
    return written


def map_json_text(text: str, rule: Rule) -> str:
    """*text*, the JSON of a call's arguments, with *rule* applied to every
    string literal in it (values and keys alike) as the text the literal
    decodes to, and to whatever stands outside the literals as the text it
    is.

    Nothing is decoded as a whole, so no depth of nesting matters, and a
    literal is replaced by a literal: JSON that goes in comes out as JSON
    with the same structure.  A literal that spells its characters with
    escapes is found by what it decodes to.  Text that is no JSON is changed
    all the same: what stands outside a complete literal, or in one that
    does not decode, is changed as plain text, which leaves valid JSON
    alone because outside its literals it holds nothing a rule looks for.
    The very object comes back where nothing changed.
    """
    out: list[str] = []
    changed = False
    at = 0
    for found in _LITERAL.finditer(text):
        between = text[at : found.start()]
        new_between = apply_edits(between, rule(between)) if between else between
        literal = found.group(0)
        new_literal = literal
        try:
            value = json.loads(literal)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, str):
            edits = rule(value)
            if edits:
                new_literal = _literal(apply_edits(value, edits))
        else:
            new_literal = apply_edits(literal, rule(literal))
        changed = changed or new_between is not between or new_literal is not literal
        out.append(new_between)
        out.append(new_literal)
        at = found.end()
    rest = text[at:]
    new_rest = apply_edits(rest, rule(rest)) if rest else rest
    if not changed and new_rest is rest:
        return text
    out.append(new_rest)
    return "".join(out)


class _Frame:
    """One object or list on the way through :func:`map_strings`."""

    __slots__ = ("source", "entries", "at", "made", "changed")

    def __init__(self, source: Any) -> None:
        self.source = source
        self.entries: list[tuple[Any, Any]] = (
            list(source.items()) if isinstance(source, dict) else [(None, item) for item in source]
        )
        self.at = 0
        self.made: list[tuple[Any, Any]] = []
        self.changed = False

    def put(self, key: Any, value: Any) -> None:
        old_key, old = self.entries[self.at]
        self.changed = self.changed or key is not old_key or value is not old
        self.made.append((key, value))
        self.at += 1

    def result(self) -> Any:
        if not self.changed:
            return self.source
        if isinstance(self.source, dict):
            return dict(self.made)
        return [value for _key, value in self.made]


def map_strings(
    value: Any,
    rule: Rule,
    *,
    json_keys: frozenset[str] = frozenset(),
) -> Any:
    """*value*, a JSON value, with *rule* applied to every string in it and to
    the keys of its objects, which a template writes out as well.  A string
    under one of *json_keys* is changed as the JSON it is
    (:func:`map_json_text`).  The walk keeps a stack of its own: a value
    nested a hundred thousand deep is walked like any other.  The very
    object comes back where nothing changed."""
    if isinstance(value, str):
        return apply_edits(value, rule(value))
    if not isinstance(value, (dict, list)):
        return value
    frames = [_Frame(value)]
    done: Any = value
    while frames:
        frame = frames[-1]
        if frame.at == len(frame.entries):
            frames.pop()
            done = frame.result()
            if frames:
                parent = frames[-1]
                parent.put(_key(parent, rule), done)
            continue
        key, item = frame.entries[frame.at]
        if isinstance(item, (dict, list)):
            frames.append(_Frame(item))
        elif isinstance(item, str):
            if isinstance(key, str) and key in json_keys:
                frame.put(_key(frame, rule), map_json_text(item, rule))
            else:
                frame.put(_key(frame, rule), apply_edits(item, rule(item)))
        else:
            frame.put(_key(frame, rule), item)
    return done


def _key(frame: _Frame, rule: Rule) -> Any:
    """The key of the entry *frame* is at, changed where it is a text."""
    key = frame.entries[frame.at][0]
    return apply_edits(key, rule(key)) if isinstance(key, str) else key


def _is_data(part: Any) -> bool:
    kind = part.get("type") if isinstance(part, dict) else None
    return isinstance(kind, str) and kind in DATA_PARTS


def _text_of(part: Any) -> str | None:
    """The text a template writes for *part* of a content list, ``None``
    where the part is no text part."""
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and not _is_data(part) and isinstance(part.get("text"), str):
        return str(part["text"])
    return None


def _with_text(part: Any, text: str, rule: Rule) -> Any:
    """The text part *part* with its text replaced by *text*, and whatever
    else it holds walked."""
    if isinstance(part, str):
        return text
    rest = {name: item for name, item in part.items() if name != "text"}
    walked = map_strings(rest, rule, json_keys=JSON_KEYS)
    if text is part["text"] and walked is rest:
        return part
    out: dict[Any, Any] = {}
    for name in part:
        if name == "text":
            out[name] = text
        else:
            new_name = _new(name, rule)
            out[new_name] = walked[new_name]
    return out


def _new(name: Any, rule: Rule) -> Any:
    return apply_edits(name, rule(name)) if isinstance(name, str) else name


def _shares(texts: Sequence[str], edits: Sequence[Edit]) -> list[str]:
    """What each of *texts* becomes when *edits* are made in the text they
    make together: every text keeps what was changed between its first
    character and the next text's."""
    out: list[str] = []
    begin = 0
    waiting = list(edits)
    for index, text in enumerate(texts):
        end = begin + len(text)
        last = index == len(texts) - 1
        mine: list[Edit] = []
        while waiting and (last or waiting[0][0] < end):
            start, stop, put = waiting.pop(0)
            if stop > end and not last:
                # Characters taken out across the border: this text gives
                # what it has of them, the next one the rest.
                waiting.insert(0, (end, stop, ""))
                stop = end
            mine.append((start - begin, stop - begin, put))
        out.append(apply_edits(text, mine))
        begin = end
    return out


def map_content(parts: Sequence[Any], rule: Rule) -> Any:
    """The parts of a message's content with *rule* applied.  Text parts
    that follow each other are searched as the one text a template makes of
    them, so a text that begins in one part and ends in the next is found;
    each part keeps its share.  A part that holds data is left as it is.
    The very list comes back where nothing changed."""
    out: list[Any] = []
    changed = False
    at = 0
    while at < len(parts):
        part = parts[at]
        if _text_of(part) is None:
            new = part if _is_data(part) else map_strings(part, rule, json_keys=JSON_KEYS)
            changed = changed or new is not part
            out.append(new)
            at += 1
            continue
        run = at
        texts: list[str] = []
        while run < len(parts) and (text := _text_of(parts[run])) is not None:
            texts.append(text)
            run += 1
        edits = rule("".join(texts)) if len(texts) > 1 else rule(texts[0])
        shares = _shares(texts, edits) if edits else texts
        for old, text in zip(parts[at:run], shares, strict=True):
            new = _with_text(old, text, rule)
            changed = changed or new is not old
            out.append(new)
        at = run
    return out if changed else parts


def map_message(message: Mapping[str, Any], rule: Rule) -> Any:
    """One message with *rule* applied to every text in it: its content and
    the parts of it, reasoning, the names and arguments of its calls, and
    whatever else it holds.  The role is the template's and is kept.  The
    very object comes back where nothing changed."""
    out: dict[Any, Any] = {}
    changed = False
    for name, value in message.items():
        if name == "role":
            new: Any = value
        elif name == "content" and isinstance(value, list):
            new = map_content(value, rule)
        else:
            new = map_strings(value, rule, json_keys=JSON_KEYS)
        new_name = _new(name, rule)
        changed = changed or new is not value or new_name is not name
        out[new_name] = new
    return out if changed else message


def map_messages(messages: Sequence[Any], rule: Rule) -> Any:
    """The messages of a request with *rule* applied to each
    (:func:`map_message`); the very list where nothing changed."""
    out = [
        map_message(message, rule)
        if isinstance(message, dict)
        else map_strings(message, rule, json_keys=JSON_KEYS)
        for message in messages
    ]
    if all(new is old for new, old in zip(out, messages, strict=True)):
        return messages
    return out
