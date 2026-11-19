"""Control-token text in a conversation, for servers that tokenize a whole prompt.

A self-hosted server renders a conversation by writing its chat template out as
text and tokenizing all of it.  The tokenizer writes one of its control tokens
wherever it meets the token's text, so a tool result that holds the characters
of the end-of-turn token ends the tool result there, and what follows it in the
result reads as a new turn: a file, a web page or the output of a tool can
forge a user message or a tool call.  Nothing on such a server marks a part of
the prompt as content.

On these routes the prompt is text, so the text is what can be changed.

**The escape.**  In what is sent, every control-token text gets a backslash
after its first character, the way :func:`turnstone.core.fence.neutralize`
defangs a fence marker: the text stays readable and is no token's text any
more.  A text that already stands escaped gets one backslash more, so that no
two texts are sent as the same one and the escape can be undone exactly.  It is
applied to every text of a request (:func:`escape_request`): tool results,
user and system messages, the definitions of tools, and the model's own
earlier turns, which a server renders again from their text.

**Undoing it.**  The model reads the escaped text, and when it writes such a
text back (an edit of the file it read, a quotation in its answer) it writes
the backslash too.  So what the model writes is put through the exact inverse
before anything uses it (:func:`unescape_result`): one backslash is taken out
of every escaped control-token text, in the reply's text, its reasoning and the
arguments of its calls.  A tool gets the characters the file holds, and the
stored turn holds what a reader would have typed.  The stored turn is escaped
again when it is next sent, so the model never gets a control token out of its
own words either.  The inverse is applied only to a reply whose request held
an escaped text: a model that was shown none has nothing to write back.

**Which texts.**  The served model's own control tokens, where they can be
learned (:func:`tokens_for`): a list the operator configured for the model, or
what the server says of its tokenizer (``GET /tokenizer_info``).  Where neither
is to be had, a pattern over the families of the common templates stands in,
and the log says once that the lane's coverage could not be established: a
template with tokens outside those families is then not protected, and a text
that only looks like a token gets a backslash it did not need.

**Where.**  Only on lanes declared self-hosted (:func:`decide`), and there by
default; a model's definition can switch it either way.

Only what is sent is changed: the stored conversation keeps the texts as they
were.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from turnstone.core.log import get_logger
from turnstone.core.text_walk import JSON_KEYS, apply_edits, map_messages, map_strings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from turnstone.core.providers._protocol import CompletionResult, StreamChunk
    from turnstone.core.text_walk import Edit

log = get_logger(__name__)

_MARK = chr(92)
# A run of backslashes, as a pattern.
_MARKS = re.escape(_MARK) + "*"
# The fullwidth bar some templates write their token names between.
_WIDE_BAR = chr(0xFF5C)
# The most backslashes an escaped text is still looked for with while it is
# being streamed: one per time a text went through an escape.
_DEEPEST = 8

# The server types that render a chat template as text and tokenize the whole.
SELF_HOSTED_SERVER_TYPES = frozenset({"vllm", "sglang", "llama.cpp", "llamacpp"})
# The provider lanes such servers are configured on.
_TEMPLATE_LANES = frozenset({"openai-compatible", "anthropic-compatible"})


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
    added = info.get("added_tokens")
    for entry in added if isinstance(added, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("content"), str):
            token = entry.get("id")
            if isinstance(token, int) and not isinstance(token, bool) and entry["content"]:
                found[entry["content"]] = token
    return found


def can_meet(texts: Sequence[str]) -> str | None:
    """Why a mark after the first character does not keep the texts of
    *texts* apart, or ``None`` when it does.

    A marked text must not be able to become another text, or itself, with
    what stands before or after it.  That holds when no text ends the way
    another begins (two occurrences cannot overlap) and none holds another:
    every occurrence in a text is then found and marked by itself, and
    neither side of a mark can complete a text.
    """
    kept, left = _apart(texts)
    return left[0][1] if left else None


def _apart(texts: Iterable[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """The texts of *texts* that a mark after the first character keeps
    apart, and each of the others with why it is not among them.

    A text that holds another is left out: where it stands, the one it holds
    stands too and is marked, which takes the longer one apart as well.  A
    text that ends the way another begins (or itself) is left out because
    two of them can share characters, and only one of the two would be
    found.
    """
    given = sorted(set(texts), key=lambda text: (len(text), text))
    whole = set(given)
    kept: list[str] = []
    left: list[tuple[str, str]] = []
    begins: dict[str, str] = {}
    ends: dict[str, str] = {}
    for text in given:
        size = len(text)
        held = next(
            (
                text[a:b]
                for a in range(size)
                for b in range(a + 1, size + 1)
                if b - a < size and text[a:b] in whole
            ),
            None,
        )
        if held is not None:
            left.append((text, f"{text!r} holds {held!r}"))
            continue
        why: str | None = None
        for cut in range(1, size):
            head, tail = text[:cut], text[cut:]
            if head == text[size - cut :]:
                why = f"{text!r} ends the way {text!r} begins"
            elif tail in begins:
                why = f"{text!r} ends the way {begins[tail]!r} begins"
            elif head in ends:
                why = f"{ends[head]!r} ends the way {text!r} begins"
            if why is not None:
                break
        if why is not None:
            left.append((text, why))
            continue
        kept.append(text)
        for cut in range(1, size):
            begins.setdefault(text[:cut], text)
            ends.setdefault(text[cut:], text)
    return kept, left


def _looks_like_a_marker(text: str) -> bool:
    """Whether a token of a tokenizer's own is taken for a control token: a
    text of two characters or more without white space or a backslash that
    is not a plain word.  A tokenizer can keep runs of spaces and whole
    words as tokens of its own; those are text."""
    return (
        len(text) >= 2
        and _MARK not in text
        and not any(ch.isspace() for ch in text)
        and not text.replace("_", "").isalnum()
    )


@dataclass(frozen=True)
class ControlTokens:
    """The texts that are escaped on a lane, and where they are known from."""

    # ``"configured"``, ``"server"`` or ``"pattern"``.
    source: str
    # Every text, with any run of backslashes after its first character.
    pattern: re.Pattern[str] = field(repr=False)
    # The characters a text can begin with.
    first: frozenset[str] = field(repr=False)
    # The longest text, in characters.
    longest: int
    # How many texts are covered, and the texts that could not be
    # (:func:`_apart`), each with why; a pattern covers what it matches.
    count: int | None = None
    left_out: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, texts: Iterable[str], source: str) -> ControlTokens | None:
        """The control tokens *texts* names; ``None`` where it names none
        that can be escaped."""
        usable = [t for t in texts if isinstance(t, str) and len(t) >= 2 and _MARK not in t]
        kept, left = _apart(usable)
        if not kept:
            return None
        by_first: dict[str, list[str]] = {}
        for text in kept:
            by_first.setdefault(text[0], []).append(text[1:])
        pattern = "|".join(
            re.escape(first)
            + _MARKS
            + "(?:"
            + "|".join(re.escape(rest) for rest in sorted(rests, key=len, reverse=True))
            + ")"
            for first, rests in by_first.items()
        )
        return cls(
            source=source,
            pattern=re.compile(pattern),
            first=frozenset(by_first),
            longest=max(len(text) for text in kept),
            count=len(kept),
            left_out=tuple(left),
        )

    def _found(self, text: str) -> list[re.Match[str]]:
        if len(self.first) <= 8 and not any(first in text for first in self.first):
            return []
        return list(self.pattern.finditer(text))

    def escape_edits(self, text: str, seen: Counter[str] | None = None) -> list[Edit]:
        """One backslash more after the first character of every control-token
        text in *text*; *seen* is told which texts, without their
        backslashes."""
        edits: list[Edit] = []
        for found in self._found(text):
            at = found.start() + 1
            edits.append((at, at, _MARK))
            if seen is not None:
                whole = found.group(0)
                seen[whole[0] + whole[1:].lstrip(_MARK)] += 1
        return edits

    def unescape_edits(self, text: str) -> list[Edit]:
        """One backslash fewer in every escaped control-token text in
        *text*: the exact inverse of :meth:`escape_edits`."""
        return [
            (found.start() + 1, found.start() + 2, "")
            for found in self._found(text)
            if text[found.start() + 1] == _MARK
        ]

    def escape(self, text: str) -> str:
        """*text* escaped; the very object where it holds nothing to escape."""
        return apply_edits(text, self.escape_edits(text))

    def unescape(self, text: str) -> str:
        """*text* with the escape undone; ``unescape(escape(text))`` is
        *text* for every text."""
        return apply_edits(text, self.unescape_edits(text))

    def holds_back(self, text: str) -> int:
        """Where the end of *text*, a stream so far, may still become an
        escaped control-token text with what follows: everything before that
        place can be shown.  ``len(text)`` where nothing is held back."""
        size = len(text)
        covered = [(m.start(), m.end()) for m in self._found(text)]
        for at in range(max(0, size - self.longest - _DEEPEST), size):
            if text[at] not in self.first:
                continue
            if at + 1 < size and text[at + 1] != _MARK:
                continue
            if any(start <= at < end for start, end in covered):
                continue
            return at
        return size


def _fallback() -> ControlTokens:
    """The pattern that stands in where a lane's control tokens are not
    known: the families of the common chat templates.

    - a name between bars, ASCII or fullwidth;
    - the tool-call, tool-response and reasoning tags, and the sentence
      markers;
    - turn, image and paragraph markers written as one word in angle
      brackets;
    - the system markers and the speech tags of older templates;
    - capital-letter names in square brackets (instruction, tool and system
      markers), and the mask token one family writes there.
    """
    name = "[^ |<>" + _WIDE_BAR + chr(9) + chr(10) + chr(13) + "]{1,64}"
    bar = "[|" + _WIDE_BAR + "]"
    angle = "|".join(
        (
            bar + name + bar + ">",
            "/?(?:tool_call|tool_response|think|s)>",
            "(?:start_of_turn|end_of_turn|start_of_image|end_of_image|bos|eos|sop|eop)>",
            "</?SYS>>",
            "tts_[a-z_]{1,32}>",
        )
    )
    square = "/?[A-Z_]+" + re.escape("]") + "|gMASK" + re.escape("]")
    pattern = "<" + _MARKS + "(?:" + angle + ")|" + re.escape("[") + _MARKS + "(?:" + square + ")"
    return ControlTokens(
        source="pattern",
        pattern=re.compile(pattern),
        first=frozenset("<["),
        longest=68,
    )


FALLBACK = _fallback()


# -- a request and its reply ---------------------------------------------------


def escape_request(
    tokens: ControlTokens, messages: Any, tools: Any
) -> tuple[Any, Any, dict[str, int]]:
    """*messages* and *tools* as they are sent: every control-token text
    escaped, wherever it stands (contents and their parts, reasoning, the
    names and arguments of calls, descriptions and parameter names).  A
    call's arguments are escaped as the values their JSON holds and stay
    JSON.  With them, the texts that were escaped, each with how often.  A
    part that holds nothing to escape is the object it was, and so is the
    whole."""
    seen: Counter[str] = Counter()

    def rule(text: str) -> list[Edit]:
        return tokens.escape_edits(text, seen)

    sent = map_messages(messages, rule) if isinstance(messages, list) else messages
    offered = map_strings(tools, rule) if tools else tools
    return sent, offered, dict(seen)


def unescape_result(tokens: ControlTokens, result: CompletionResult) -> CompletionResult:
    """*result*, a model's reply, with the escape undone in everything the
    model wrote: its text, its reasoning, the names and arguments of its
    calls, and the provider's own blocks, which hold the same.  The very
    object comes back where the reply holds no escaped text."""
    rule = tokens.unescape_edits
    content = tokens.unescape(result.content) if result.content else result.content
    reasoning = tokens.unescape(result.reasoning) if result.reasoning else result.reasoning
    calls = map_strings(result.tool_calls, rule, json_keys=JSON_KEYS)
    blocks = map_strings(result.provider_blocks, rule, json_keys=JSON_KEYS)
    if (
        content is result.content
        and reasoning is result.reasoning
        and calls is result.tool_calls
        and blocks is result.provider_blocks
    ):
        return result
    return replace(
        result, content=content, reasoning=reasoning, tool_calls=calls, provider_blocks=blocks
    )


class _Shown:
    """One stream of text on its way to a display, the escape undone as it
    passes.  The end of what has arrived is held back while it could still
    become an escaped text."""

    def __init__(self, tokens: ControlTokens) -> None:
        self._tokens = tokens
        self._held = ""

    def take(self, delta: str) -> str:
        text = self._held + delta
        keep = self._tokens.holds_back(text)
        self._held = text[keep:]
        return self._tokens.unescape(text[:keep])

    def rest(self) -> str:
        text, self._held = self._held, ""
        return self._tokens.unescape(text)


def unescaped_stream(
    tokens: ControlTokens, on_chunk: Callable[[StreamChunk], None]
) -> Callable[[StreamChunk], None]:
    """*on_chunk*, a display's reader of a reply as it streams, given the
    reply's text and reasoning with the escape undone
    (:func:`unescape_result` does the same for the reply as a whole).  Text
    that could still become an escaped text is held back until the next part
    settles it, and goes out before anything that follows it in the stream:
    reasoning after text, a tool call, the end."""
    text, thought = _Shown(tokens), _Shown(tokens)

    def passed(chunk: StreamChunk) -> None:
        closes = bool(chunk.tool_call_deltas or chunk.finish_reason)
        # A reader takes a part's reasoning first, then its text, then its
        # calls.  Text held back from earlier parts came before this part's
        # reasoning, so it goes out in a part of its own.
        earlier = text.rest() if chunk.reasoning_delta else ""
        reasoning = thought.take(chunk.reasoning_delta) if chunk.reasoning_delta else ""
        if chunk.content_delta or closes:
            reasoning += thought.rest()
        content = text.take(chunk.content_delta) if chunk.content_delta else ""
        if closes:
            content += text.rest()
        if earlier:
            on_chunk(
                replace(
                    chunk,
                    content_delta=earlier,
                    reasoning_delta="",
                    tool_call_deltas=[],
                    usage=None,
                    finish_reason=None,
                    is_first=False,
                    info_delta="",
                    provider_blocks=[],
                )
            )
        if content == chunk.content_delta and reasoning == chunk.reasoning_delta:
            on_chunk(chunk)
        else:
            on_chunk(replace(chunk, content_delta=content, reasoning_delta=reasoning))

    return passed


# -- which lanes, and which texts ----------------------------------------------


def is_local_address(base_url: str) -> bool:
    """Whether *base_url* names this machine or a private network by its
    address: ``localhost``, a loopback address, or an address of a private or
    link-local range.  A name that would have to be looked up says nothing."""
    try:
        host = (urlsplit(base_url.strip()).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def decide(
    *, override: Any, provider_name: Any, server_type: Any, base_url: Any
) -> tuple[bool, str]:
    """Whether the escape is applied on a lane, and why.

    *override* is the model definition's own word (``escape_control_tokens``
    in its capabilities) and decides where it is a yes or a no.  Otherwise
    the escape is on only for a lane declared self-hosted: an
    OpenAI-compatible Chat Completions lane or an Anthropic-compatible lane
    whose model names the type of a self-hosted server, or whose endpoint is
    a loopback or private address.  A hosted service configured as a
    compatible endpoint, the Responses surface and the providers' own APIs
    are left alone: their prompts are not built from this text.
    """
    if override is True:
        return True, "switched on for the model (escape_control_tokens)"
    if override is False:
        return False, "switched off for the model (escape_control_tokens)"
    if not isinstance(provider_name, str) or provider_name not in _TEMPLATE_LANES:
        return False, "the lane's provider is not one a self-hosted server is configured on"
    if isinstance(server_type, str) and server_type.strip().lower() in SELF_HOSTED_SERVER_TYPES:
        return True, f"the model is declared served by {server_type.strip().lower()}"
    if isinstance(base_url, str) and is_local_address(base_url):
        return True, "the endpoint is a loopback or private address"
    return False, (
        "the lane is not declared self-hosted: its model names no self-hosted server type "
        "and its endpoint is no loopback or private address"
    )


_lock = threading.Lock()
# What each lane's server said of its tokenizer, by endpoint and model.
_learned: dict[tuple[str, str], ControlTokens] = {}
# The lanes whose decision, and whose coverage, the log has been told.
_said: set[tuple[str, str, str]] = set()


def _say_once(kind: str, base_url: str, model: str) -> bool:
    with _lock:
        if (kind, base_url, model) in _said:
            return False
        _said.add((kind, base_url, model))
        return True


def note_decision(base_url: str, model: str, alias: str, applied: bool, reason: str) -> None:
    """Tell the log, once per lane, whether the escape is applied there."""
    if _say_once("decision", base_url, model):
        log.info(
            "control_text.escape",
            applied=applied,
            reason=reason,
            model=model,
            alias=alias,
            base_url=base_url.split("?")[0],
        )


def _root(base_url: str) -> str:
    """The server's root: the API root without its version part."""
    base = base_url.split("?")[0].rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def tokens_for(
    client: Any, base_url: str, model: str, configured: Any = None, alias: str = ""
) -> ControlTokens:
    """The control tokens of the model served at *base_url*.

    *configured* (the model definition's ``control_tokens``) is taken where
    it names any.  Otherwise the server is asked once what tokens its
    tokenizer has of its own (``GET /tokenizer_info``; a server offers it
    when it is started for it), and its answer is kept for the lane.  Where
    neither names them the pattern stands in (:data:`FALLBACK`), and the log
    is told once that the lane's coverage could not be established.  Never
    raises.
    """
    if isinstance(configured, (list, tuple)) and configured:
        named = ControlTokens.of(configured, "configured")
        if named is not None:
            _warn_left_out(named, base_url, model, alias)
            return named
    key = (base_url, model)
    with _lock:
        known = _learned.get(key)
    if known is not None:
        return known
    why: str
    settled = True
    try:
        answer = client.get(
            _root(base_url) + "/tokenizer_info",
            cast_to=object,
            options={"timeout": 5.0, "max_retries": 0},
        )
    except Exception as error:
        # A server that answered with a refusal has said what it will say;
        # one that could not be reached is asked again with the next request.
        settled = getattr(error, "status_code", None) is not None
        why = f"{type(error).__name__}: {str(error)[:120]}"
    else:
        named = ControlTokens.of(
            [text for text in added_tokens_of(answer) if _looks_like_a_marker(text)], "server"
        )
        if named is not None:
            with _lock:
                _learned[key] = named
            _warn_left_out(named, base_url, model, alias)
            return named
        why = "the answer names no tokens of the tokenizer's own"
    if settled:
        with _lock:
            _learned[key] = FALLBACK
    if _say_once("coverage", base_url, model):
        log.warning(
            "control_text.coverage_not_established",
            model=model,
            alias=alias,
            base_url=base_url.split("?")[0],
            tokenizer_info=why,
            detail=(
                "the model's control tokens are not configured (control_tokens) and the "
                "server does not name them (/tokenizer_info): a pattern over the common "
                "template families is escaped instead, which may miss this model's tokens"
            ),
        )
    return FALLBACK


def _warn_left_out(tokens: ControlTokens, base_url: str, model: str, alias: str) -> None:
    if tokens.left_out and _say_once("left_out", base_url, model):
        log.warning(
            "control_text.tokens_left_out",
            model=model,
            alias=alias,
            source=tokens.source,
            count=len(tokens.left_out),
            first=[why for _text, why in tokens.left_out[:5]],
        )


def forget_lanes() -> None:
    """Forget what was learned and said of every lane (for tests)."""
    with _lock:
        _learned.clear()
        _said.clear()


@dataclass
class EscapeLog:
    """What became of the escape in one session's requests, for a caller
    that records it (a rollout): attached to the session's lane and told of
    every model call made on it, the session's own and its sub-agents'.

    It adds up over the calls.  ``applied`` is the latest call's decision;
    ``ever_applied`` says whether any call's lane had the escape on, and the
    reason and the source of the tokens are then that call's.  ``escaped``
    sums the texts escaped in all requests, and ``requests`` counts the
    requests that held one.

    A session whose requests must go out as they are says so with
    ``never_because`` (a rollout that sends its prompts as token IDs: the
    prompt is built from the request's text, and an escape would put a
    character into it that no file holds).  A call whose lane has the escape
    on all the same is then a breach: ``on_breach`` is told and raises, and
    where it does not, this raises itself.  Nothing is escaped and nothing
    is sent.
    """

    applied: bool | None = None
    ever_applied: bool = False
    reason: str = "no model call was made"
    # Where the lane's control tokens are known from.
    tokens_from: str | None = None
    # The texts escaped in all requests so far, each with how often.
    escaped: dict[str, int] = field(default_factory=dict)
    # How many requests held a text that was escaped.
    requests: int = 0
    # Why nothing may ever be escaped in this session's requests, in words
    # that go before ": nothing is escaped"; ``None`` where the lane's rule
    # is free to decide.
    never_because: str | None = None
    # Told of a breach, with what happened.  It raises.
    on_breach: Callable[[str], None] | None = None

    def note(self, applied: bool, reason: str, tokens: ControlTokens | None) -> None:
        """Note the decision for one model call: whether its lane has the
        escape on, and why."""
        self.applied = applied
        if applied and not self.ever_applied:
            self.ever_applied = True
            self.reason = reason
            self.tokens_from = tokens.source if tokens is not None else None
        elif not self.ever_applied:
            self.reason = (
                reason
                if self.never_because is None
                else f"{self.never_because}: nothing is escaped (the lane says: {reason})"
            )
        if applied and self.never_because is not None:
            self.breach(f"the lane's rule switched the escape on: {reason}")

    def sent(self, escaped: dict[str, int]) -> None:
        """Note the texts that were escaped in one request."""
        for text, count in escaped.items():
            self.escaped[text] = self.escaped.get(text, 0) + count
        self.requests += bool(escaped)
        if escaped and self.never_because is not None:
            self.breach(f"{sum(escaped.values())} texts of a request were escaped")

    def breach(self, what: str) -> None:
        """Refuse what was about to happen to a request that must go out as it
        is.  Always raises."""
        if self.on_breach is not None:
            self.on_breach(what)
        raise RuntimeError(
            f"control-token text was to be escaped where {self.never_because} ({what})"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "applied": self.ever_applied,
            "reason": self.reason,
            "tokens_from": self.tokens_from,
            "escaped": dict(self.escaped),
            "requests": self.requests,
        }
