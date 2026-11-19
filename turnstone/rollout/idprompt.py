"""Prompts sent as token IDs, so that one rollout is one token sequence.

A session sends messages, and a chat route writes the earlier assistant turns
back into the prompt by turning their text into tokens again.  A model can
generate a split of a text into tokens that a tokenizer does not produce for
the same text, so the next prompt then differs from what was generated, at a
place nothing sent as messages can reach.

With this mode the prompt of every model call of the session is built here::

    prompt(t + 1) = prompt(t) + generated(t) + tail

and goes to the server's completions route as token IDs.  ``tail`` is what the
chat template writes after the generated tokens up to the next generation
position: the rest of the assistant turn's closing, the tool results, the next
turn's opening.  It is never rendered here.  It is cut out of two renderings
the server makes itself, on its tokenize route, from the request's own
messages, tools and template arguments:

- ``through``: the conversation up to and including the assistant turn, with no
  generation prompt;
- ``whole``: the whole conversation of the new request, with one.

``through`` ends with the template's closing of an assistant turn and is a
prefix of ``whole``; what ``whole`` holds after it follows the closing.  Every
assumption in that sentence is checked on every call, and a call on which one
fails raises :class:`PromptAssemblyError`: the run ends as a failure of the
machinery, never with a prompt that is silently something else.  A reply is
held to what its reading relies on in the same way
(:meth:`PromptAssembly.read`).

A prompt built this way is what the model saw and wrote.  It need not be what
the chat route renders for the same conversation: the reply as it was read,
written back by the template, can come out as other tokens.  ``whole`` is at
hand, so the two are compared on every call and a difference is noted as an
event of the record (``prompt_differs_from_rendering``), once the call the
prompt belongs to has returned a whole reply: the event names a call of the
token record, never a request that was not made or not answered.

The completions route reads nothing, so the reply is read here
(:mod:`turnstone.rollout.replyformat`) and handed to the session as a
chat-shaped stream.  :func:`check_token_id_route` establishes before a run that
a server can do all of this, and what its template's closing and generation
prompt are.

The prompt's length is known before each request, so the context window is
kept exactly (``PromptAssembly(context_window=...)``): a request's reply limit
is at most what the window has left after the prompt, a request with nothing
left is not made, and the whole sequence never grows past the window.

What a server must have: a tokenize route that renders messages with tools and
template arguments exactly as its chat route does; a completions route that
takes a prompt of token IDs, stops on the template's end-of-turn token, and
returns token IDs, log-probabilities named by token ID, usage and the prompt it
used; and generated text that keeps the format's markers.  It needs no reader
of reasoning or tool calls.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openai import APIStatusError
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from turnstone.core.control_text import added_tokens_of
from turnstone.core.log import get_logger
from turnstone.core.model_turn import ModelContextLimitError
from turnstone.rollout.budget import ContextFullError
from turnstone.rollout.plaintext import HarnessFaultError, PlainText, plain_text_verdict
from turnstone.rollout.recording import (
    PARENT,
    CallRecord,
    _usage_dict,
    extra_field,
    note_reported_prompt,
    pair_tokens,
    unreadable,
)
from turnstone.rollout.replyformat import (
    PARAMETER_BLOCKS,
    ParsedReply,
    ReplyFormat,
    pieces_from_chunks,
    reply_format,
)
from turnstone.rollout.settings import SetupError
from turnstone.rollout.tokens import common_prefix_len
from turnstone.rollout.verdicts import made_for

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from turnstone.rollout.vision import Vision

log = get_logger(__name__)

_NL = chr(10)
# Keyword arguments of a chat request that mean the same on the completions route.
_SAME_ON_BOTH_ROUTES = (
    "temperature",
    "top_p",
    "seed",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "extra_headers",
)


class PromptAssemblyError(RuntimeError):
    """A prompt could not be built as the previous prompt, the tokens generated
    after it and the template's tail: something the method relies on did not
    hold.  Nothing was sent."""


def template_arguments(request: Mapping[str, Any]) -> dict[str, Any]:
    """The chat-template arguments a chat request carries, as a tokenize route
    takes them: those of its body, and a reasoning effort given beside them,
    which a chat route passes to the template too (with thinking switched on
    for any effort but ``none`` unless the request says otherwise)."""
    body = request.get("extra_body") or {}
    arguments = dict(body.get("chat_template_kwargs") or {})
    effort = request.get("reasoning_effort")
    if effort is not None:
        arguments["reasoning_effort"] = effort
        arguments.setdefault("enable_thinking", effort != "none")
    return arguments


def _root(client: Any) -> str:
    """The server's root: the client's API root without its version part."""
    base = str(client.base_url).rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _tokens(client: Any, body: dict[str, Any]) -> list[int]:
    reply = client.post(_root(client) + "/tokenize", body=body, cast_to=object)
    tokens = reply.get("tokens") if isinstance(reply, dict) else None
    if not isinstance(tokens, list):
        raise PromptAssemblyError("the tokenize route returned no token IDs")
    return [int(t) for t in tokens]


def render(
    client: Any,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    arguments: Mapping[str, Any],
    *,
    generation_prompt: bool = True,
) -> list[int]:
    """The server's own rendering of a conversation, as token IDs."""
    body: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "add_generation_prompt": generation_prompt,
    }
    if tools:
        body["tools"] = list(tools)
    if arguments:
        body["chat_template_kwargs"] = dict(arguments)
    return _tokens(client, body)


def text_tokens(client: Any, model: str, text: str) -> list[int]:
    """The token IDs the server's tokenizer gives *text*, nothing added."""
    return _tokens(client, {"model": model, "prompt": text, "add_special_tokens": False})


def completion_request(request: Mapping[str, Any], prompt: list[int]) -> dict[str, Any]:
    """The completions request that stands for the chat request *request*
    once its prompt is *prompt*: same model, same sampler settings, same reply
    limit, streamed, with token IDs and log-probabilities asked for."""
    body = {
        k: copy.deepcopy(v)
        for k, v in (request.get("extra_body") or {}).items()
        # The template arguments went into the prompt.
        if k != "chat_template_kwargs"
    }
    body["return_token_ids"] = True
    # Name each log-probability's token by its ID, to compare the channels.
    body["return_tokens_as_token_ids"] = True
    # Special tokens are left out of the text, as a server that reads replies
    # itself leaves them out; the format's markers are not special tokens.
    body["skip_special_tokens"] = True
    out: dict[str, Any] = {
        "model": request.get("model"),
        "prompt": prompt,
        "stream": True,
        "stream_options": {"include_usage": True},
        # The value of each sampled token, no alternatives.
        "logprobs": 0,
        "extra_body": body,
    }
    limit = request.get("max_tokens", request.get("max_completion_tokens"))
    if limit is not None:
        out["max_tokens"] = limit
    for name in _SAME_ON_BOTH_ROUTES:
        if name in request:
            out[name] = request[name]
    return out


def take_completion(record: CallRecord, part: Any) -> tuple[str, list[int]] | None:
    """Copy token data out of one part of a completions reply into *record*;
    return the part's text and token IDs.  Never raises.

    The same pairing rules as for a chat reply
    (:func:`~turnstone.rollout.recording.pair_tokens`).
    """
    try:
        taken: tuple[str, list[int]] | None = None
        for choice in getattr(part, "choices", None) or []:
            if getattr(choice, "index", 0) != 0:
                continue
            note_reported_prompt(record, extra_field(choice, "prompt_token_ids"))
            ids = [int(i) for i in (extra_field(choice, "token_ids") or [])]
            # Among the call's tokens before anything else of the part is read.
            record.token_ids.extend(ids)
            text = getattr(choice, "text", None) or ""
            record.raw.extend(text.encode("utf-8", errors="replace"))
            given = getattr(choice, "logprobs", None)
            pair_tokens(
                record,
                ids,
                list(getattr(given, "token_logprobs", None) or []),
                [str(n) for n in (getattr(given, "tokens", None) or [])],
            )
            reason = getattr(choice, "finish_reason", None)
            if reason:
                record.finish_reason = reason
            taken = (text, ids)
        usage = _usage_dict(getattr(part, "usage", None))
        if usage:
            record.usage = usage
        return taken
    except Exception as error:
        unreadable(record, error)
        return None


@dataclass(frozen=True)
class TokenIdRoute:
    """What :func:`check_token_id_route` established about a server and a
    run's template arguments: all a run needs to build its prompts and read
    its replies."""

    format: str
    # Each token marker of the format that is one token on this server.
    marker_ids: dict[str, int]
    # The token markers this server has no single token for.
    text_markers: tuple[str, ...]
    # What the template writes at the end of an assistant turn; its first
    # token is the one a model ends its turn on.
    closing: tuple[int, ...]
    generation_prompt: tuple[int, ...]
    # Whether a reply begins inside reasoning.
    in_reasoning: bool

    @classmethod
    def from_verdict(cls, verdict: Mapping[str, Any]) -> TokenIdRoute:
        """The route a passed check's verdict describes.

        Raises :class:`~turnstone.rollout.settings.SetupError`, which is a
        ``ValueError``, for a verdict that says the server cannot, and for one
        that does not hold what a passed check found: a key that is missing, a
        value of the wrong kind, a marker of the format that is neither among
        the tokens nor among the markers spelled out, a marker the format
        does not have, or a verdict that contradicts itself.  A verdict can
        come from a file somebody else wrote.
        """
        if not verdict.get("ok"):
            raise SetupError(f"the server cannot be sent token-ID prompts: {verdict.get('reason')}")

        def wrong(what: str) -> SetupError:
            return SetupError(
                f"the given check of the server does not hold what a passed check found "
                f"({what}): check the server again"
            )

        def tokens(name: str) -> tuple[int, ...]:
            value = verdict.get(name)
            if (
                not isinstance(value, (list, tuple))
                or not value
                or any(isinstance(t, bool) or not isinstance(t, int) for t in value)
            ):
                raise wrong(f"{name} must be the token IDs the template writes, at least one")
            return tuple(value)

        name = verdict.get("format")
        try:
            fmt = reply_format(name) if isinstance(name, str) else None
        except ValueError:
            fmt = None
        if fmt is None:
            raise wrong("format names no reply format")
        markers = verdict.get("marker_ids")
        if not isinstance(markers, dict) or any(
            not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int)
            for k, v in markers.items()
        ):
            raise wrong("marker_ids must give a token ID for each marker that is one token")
        spelled = verdict.get("text_markers")
        if not isinstance(spelled, (list, tuple)) or any(not isinstance(m, str) for m in spelled):
            raise wrong("text_markers must list the markers that are no single token")
        # A check puts every marker of the format into one of the two, and
        # nothing else.  A reply read without one of its markers is read as
        # something else and nothing says so; a name the format does not
        # have cannot be looked up when the first reply is read.
        named = [*markers, *spelled]
        missing = [m for m in fmt.token_markers if m not in named]
        if missing:
            raise wrong(
                "a marker of the format is in neither marker_ids nor text_markers: "
                + ", ".join(missing)
            )
        unknown = sorted({m for m in named if m not in fmt.token_markers})
        if unknown:
            raise wrong(
                f"marker_ids or text_markers names what is no marker of the format {fmt.name}: "
                + ", ".join(unknown)
            )
        twice = sorted(set(markers) & set(spelled))
        if twice:
            raise wrong("a marker is in both marker_ids and text_markers: " + ", ".join(twice))
        if not isinstance(verdict.get("in_reasoning"), bool):
            raise wrong("in_reasoning must be true or false")
        route = cls(
            format=fmt.name,
            marker_ids=dict(markers),
            text_markers=tuple(spelled),
            closing=tokens("closing"),
            generation_prompt=tokens("generation_prompt"),
            in_reasoning=verdict["in_reasoning"],
        )
        # Whether a reply begins inside reasoning is read off the generation
        # prompt; a verdict that says otherwise was not made by a check.
        names = {token: marker for marker, token in route.marker_ids.items()}
        opened = fmt.opens_in_reasoning([names[t] for t in route.generation_prompt if t in names])
        if opened != route.in_reasoning:
            raise wrong("in_reasoning contradicts the generation prompt and the markers' tokens")
        return route

    def first_prompt(self, prompt: list[int]) -> list[int]:
        """*prompt*, the server's rendering of a run's first request, once it
        is known to end with the generation prompt the check found.

        A check made for another template would have found another one, and
        every reply of the run would be read from the wrong start.  Raises
        :class:`PromptAssemblyError` before the run's first model call.
        """
        opening = list(self.generation_prompt)
        if prompt[-len(opening) :] != opening:
            raise PromptAssemblyError(
                "the first prompt does not end with the generation prompt found before the "
                f"run ({opening}): it ends {prompt[-len(opening) :]}. The check of the server "
                "was made for another template"
            )
        return prompt


@dataclass(frozen=True)
class _Rendering:
    """One rendering of a conversation: its tokens with the stand-ins taken
    out, the tokens as they came, and how many texts were cut in each
    message and in the tool definitions."""

    ids: list[int]
    raw: list[int]
    cuts: list[int]
    tool_cuts: int


def _holds_images(messages: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )


@dataclass
class _Link:
    """The session's latest request: its messages, its prompt and its call."""

    messages: list[dict[str, Any]]
    prompt: list[int]
    call: CallRecord
    # What is to be noted of this prompt once a call has returned a whole
    # reply to it: that it is not the chat route's rendering.
    differs: dict[str, Any] | None = None
    # The server's rendering of ``messages`` with a generation prompt, where
    # one was made: the next request's rendering begins with it.
    rendered: list[int] | None = None


def _common_suffix(a: Sequence[int], b: Sequence[int]) -> list[int]:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return list(a[len(a) - n :])


@dataclass
class PromptAssembly:
    """Builds the prompts of one run and reads its replies.

    The session's own calls form one chain: each prompt is the previous
    prompt, the tokens generated after it and the template's tail.  A
    sub-agent's call gets the server's rendering of its own messages: its
    conversation is not part of the rollout's sequence.
    """

    route: TokenIdRoute
    # The context window to keep, in tokens; ``None`` keeps none.
    context_window: int | None = None
    # What happened that a reader of the record should know: a turn that ended
    # on another token than the template's, a prompt that is not what the chat
    # route would have rendered, a marker token whose place in its part of
    # the reply is not certain.  The session's own calls only, numbered as
    # the token record numbers them.
    events: list[dict[str, Any]] = field(default_factory=list)
    # What was noted of a sub-agent's replies, kept apart: nothing of a
    # sub-agent is among the session's events.  A call is numbered here as
    # the record numbers a sub-agent's calls (``CallRecord.index``).
    subagent_events: list[dict[str, Any]] = field(default_factory=list)
    # Sends the requests that carry images (:mod:`turnstone.rollout.vision`);
    # ``None``, the default, sends every request to the completions route.
    vision: Vision | None = None
    # Reads the text of the tokenizer's own tokens as plain text wherever a
    # conversation is rendered (:mod:`turnstone.rollout.plaintext`); ``None``
    # renders a conversation as it is.
    plain: PlainText | None = None
    # The texts that were read as plain text in the session's latest request,
    # each with how often; empty when there were none.
    plain_seen: dict[str, int] = field(default_factory=dict)
    # What the harness found itself at fault for, once it has: the run ends
    # as ``harness_fault`` whatever else happens to it.
    fault: str | None = None
    # The stretches of the session's sequence that the model generated, each
    # by where it begins and ends: every turn a later prompt was built on.
    # What stands there is the model's own, whatever token it is.
    generated: list[tuple[int, int]] = field(default_factory=list)
    _last: _Link | None = None
    _turn: int = 0

    @property
    def format(self) -> ReplyFormat:
        return reply_format(self.route.format)

    @property
    def started(self) -> bool:
        """Whether a prompt of the session's own was built: what the run was
        set up to apply to a conversation was then applied to one."""
        return self._last is not None

    # -- the prompt --------------------------------------------------------------

    def prompt(self, client: Any, call: CallRecord, request: Mapping[str, Any]) -> list[int]:
        """The token IDs to send for the chat request *request*."""
        messages = [dict(m) for m in request.get("messages") or []]
        tools = request.get("tools") or None
        arguments = template_arguments(request)
        model = str(request.get("model"))

        def rendered(conversation: list[dict[str, Any]], generation_prompt: bool) -> _Rendering:
            plain = self.plain
            if plain is None:
                raw = render(
                    client,
                    model,
                    conversation,
                    tools,
                    arguments,
                    generation_prompt=generation_prompt,
                )
                made = _Rendering(raw, raw, [0] * len(conversation), 0)
            else:
                cut = plain.swap(conversation, tools)
                raw = render(
                    client,
                    model,
                    cut.messages,
                    cut.tools,
                    arguments,
                    generation_prompt=generation_prompt,
                )
                made = _Rendering(plain.strip(raw), raw, cut.per_message, cut.in_tools)
                if generation_prompt and call.scope == PARENT:
                    self.plain_seen = cut.environment
            # The server answered: it is working on this call.
            call.heard(time.monotonic())
            return made

        if call.scope != PARENT:
            # A rendering throughout, the sub-agent's own turns among it: what
            # a model wrote is never held against the count of stand-ins.
            return rendered(messages, True).ids
        last = self._last
        differs: dict[str, Any] | None = None
        if last is None:
            whole = rendered(messages, True)
            # The first prompt holds what the environment wrote and nothing else.
            self._no_surplus(whole, 0, sum(whole.cuts) + whole.tool_cuts)
            prompt = self.route.first_prompt(whole.ids)
            made: list[int] | None = whole.ids
        elif messages == last.messages and not last.call.complete:
            # The same request again, after an attempt that did not end.
            prompt, differs, made = last.prompt, last.differs, last.rendered
        else:
            prompt, differs, made = self._extended(last, messages, rendered)
            self._turn += 1
            begins = len(last.call.prompt_token_ids or ())
            self.generated.append((begins, begins + len(last.call.token_ids)))
        self._last = _Link(copy.deepcopy(messages), prompt, call, differs, made)
        return prompt

    def _no_surplus(self, made: _Rendering, kept_before: int, cuts: int) -> None:
        """Hold a rendering's stand-in tokens against the cuts that were made,
        in the part of it a prompt is built from (what follows its first
        *kept_before* tokens): that part holds what the environment wrote and
        nothing of the model's, so more stand-ins than cuts is this code's
        fault or the check's, never a conversation's."""
        if self.plain is None:
            return
        over = self.plain.surplus(made.raw, kept_before, cuts)
        if over > 0:
            self.fault = (
                f"a rendering holds {over} more stand-in tokens ({self.plain.stand_in_id}) than "
                "texts were cut in what the environment wrote: the template writes the stand-in, "
                "or a text went uncut"
            )
            raise HarnessFaultError(self.fault)

    def _extended(
        self,
        last: _Link,
        messages: list[dict[str, Any]],
        rendered: Callable[[list[dict[str, Any]], bool], _Rendering],
    ) -> tuple[list[int], dict[str, Any] | None, list[int]]:
        """The previous prompt, what was generated after it, and the tail;
        where that is not what the chat route renders for the same
        conversation, the event that says so; and that rendering.  The event
        is noted by
        :meth:`read`, once a call has returned a whole reply to the prompt: a
        prompt can be built and never sent (the context window is full), and
        a request can go out and never be answered."""
        route = self.route
        call = last.call
        earlier = len(last.messages)
        if len(messages) <= earlier or messages[:earlier] != last.messages:
            raise PromptAssemblyError(
                f"request {call.index + 1} does not begin with the messages of the request "
                "before it, so its prompt cannot extend that one's"
            )
        if messages[earlier].get("role") != "assistant":
            raise PromptAssemblyError(
                f"the message after the earlier request's is a {messages[earlier].get('role')!r} "
                "message, not the assistant turn that request returned"
            )
        if not call.complete or call.error or not call.ended_by_itself:
            raise PromptAssemblyError(
                f"call {call.index} did not end by itself ({call.finish_reason or call.error}); "
                "a turn that was cut cannot be continued as it was generated"
            )
        if call.prompt_differs or call.prompt_token_ids is None:
            raise PromptAssemblyError(
                f"the server did not report the prompt of call {call.index} as it was sent"
            )
        if call.unreadable:
            # A part that could not be read may have held tokens that are not
            # among the call's: the sequence would go on without them.
            raise PromptAssemblyError(
                f"a part of the reply of call {call.index} could not be read "
                f"({call.unreadable}), so the tokens it generated are not certain to be all there "
                "and no prompt can be built on them"
            )
        closing = list(route.closing)
        opening = list(route.generation_prompt)
        generated = call.token_ids
        ended = bool(generated) and generated[-1] == closing[0]
        new_cuts = 0
        if self.vision is not None and _holds_images(messages):
            # A request with images: every rendering carries them, so the
            # conversation is rendered once wherever that is enough.  It is
            # where the whole rendering begins with what was sent and
            # generated, and is then the prompt.  Where it does not (the
            # model's own split of a text is not the tokenizer's, in this
            # turn or an earlier one), the end of the turn is found in the
            # one rendering, which begins with the rendering of the request
            # before.  Only where neither holds is the conversation up to
            # the turn rendered by itself, as for text.
            made = rendered(messages, True)
            head = call.prompt_token_ids + generated + (closing[1:] if ended else closing)
            if made.ids[: len(head)] == head and made.ids[-len(opening) :] == opening:
                self._no_surplus(made, len(head), sum(made.cuts[earlier + 1 :]))
                if not ended:
                    self.events.append(self._other_token(generated, closing[0]))
                return made.ids, None, made.ids
            turn_end = self._turn_end(last, made.ids)
            if turn_end is not None:
                through = made.ids[:turn_end]
            else:
                through = rendered(messages[: earlier + 1], False).ids
        else:
            through = rendered(messages[: earlier + 1], False).ids
            made = rendered(messages, True)
        whole = made.ids
        new_cuts = sum(made.cuts[earlier + 1 :])
        if whole[: len(through)] != through:
            raise PromptAssemblyError(
                "the server renders the conversation up to an assistant turn differently once "
                "more messages follow it, so the end of that turn cannot be found in the longer "
                "rendering"
            )
        if through[-len(closing) :] != closing:
            raise PromptAssemblyError(
                "the server's rendering of an assistant turn does not end with the closing "
                f"found before the run ({closing}): it ends {through[-len(closing) :]}"
            )
        # The part the prompt takes from the rendering holds what the
        # environment wrote since the turn.
        self._no_surplus(made, len(through), new_cuts)
        if ended:
            # The model generated the end-of-turn token itself.
            rest = closing[1:]
        else:
            # It ended on another token: the template's closing follows whole.
            rest = closing
            self.events.append(self._other_token(generated, closing[0]))
        tail = rest + whole[len(through) :]
        if tail[-len(opening) :] != opening:
            raise PromptAssemblyError(
                "the server's rendering does not end with the generation prompt found before "
                f"the run ({opening}): it ends {tail[-len(opening) :]}"
            )
        prompt = call.prompt_token_ids + generated + tail
        if prompt == whole:
            return prompt, None, whole
        # What the model saw and wrote, and still not what the chat route
        # renders for the same conversation: the reader of the record is
        # told, because a model served through that route never sees it.
        return (
            prompt,
            {
                "event": "prompt_differs_from_rendering",
                # The call whose prompt it is, counted as the token record
                # counts the session's own calls.
                "call": self._turn + 1,
                "prompt_tokens": len(prompt),
                "rendered_tokens": len(whole),
                "same_tokens": common_prefix_len(prompt, whole),
            },
            whole,
        )

    def _turn_end(self, last: _Link, whole: list[int]) -> int | None:
        """Where the assistant turn that answered *last* ends in *whole*,
        the rendering of the conversation that now follows it; ``None``
        where the one rendering does not show it.

        The rendering of the request before, without its generation prompt,
        is the conversation up to the turn; where *whole* begins with it,
        the turn begins there, and it ends with the first closing the
        template writes after that.  With the conversation's text read as
        plain text nothing inside a turn is the closing's token but the
        template's own.
        """
        before = last.rendered
        closing = list(self.route.closing)
        opening = list(self.route.generation_prompt)
        if self.plain is None or before is None or len(before) < len(opening):
            return None
        start = len(before) - len(opening)
        if before[start:] != opening or whole[:start] != before[:start]:
            return None
        size = len(closing)
        for at in range(start, len(whole) - size + 1):
            if whole[at] == closing[0] and whole[at : at + size] == closing:
                return at + size
        return None

    def _other_token(self, generated: list[int], end_of_turn: int) -> dict[str, Any]:
        return {
            "event": "turn_ended_on_other_token",
            "turn": self._turn,
            "token_id": generated[-1] if generated else None,
            "end_of_turn_token_id": end_of_turn,
        }

    # -- the request -------------------------------------------------------------

    def start(self, inner: Any, call: CallRecord, request: dict[str, Any]) -> IdStream:
        """Send the chat request *request* with its prompt as token IDs."""
        if self.fault is not None:
            # Nothing more goes out once the harness has found itself at
            # fault, whoever asks: the session, a sub-agent, a retry.
            raise HarnessFaultError(self.fault)
        first = self._last is None
        prompt = self.prompt(inner, call, request)
        # The prompt's length is known exactly: what the server's speed is
        # held against, and never the size of what the messages carry.
        call.prompt_tokens_sent = len(prompt)
        request = self._within_window(call, request, len(prompt), first)
        if self.vision is not None:
            images = self.vision.images_of(request.get("messages") or [])
            if images:
                # Token IDs alone would leave the pixels behind.
                return self.vision.start(self, inner, call, request, prompt, images)
        outgoing = completion_request(request, prompt)
        messages = request.get("messages") or []
        call.prompt_sent = prompt
        call.request = {
            "model": outgoing.get("model"),
            "messages": len(messages),
            "roles": [m.get("role") for m in messages if isinstance(m, dict)],
            "tools": [t.get("function", {}).get("name") for t in request.get("tools") or []],
            "sampling": {
                k: outgoing[k] for k in ("temperature", "top_p", "max_tokens") if k in outgoing
            },
            "extra_body": dict(outgoing["extra_body"]),
            "template_arguments": template_arguments(request),
            "prompt_tokens_sent": len(prompt),
        }
        response = inner.completions.create(**outgoing)
        return IdStream(response, call, self, request.get("tools") or None, str(outgoing["model"]))

    def _within_window(
        self, call: CallRecord, request: dict[str, Any], prompt_tokens: int, first: bool
    ) -> dict[str, Any]:
        """*request* with its reply limit cut to what the context window has
        left after a prompt of *prompt_tokens*; raises when nothing is left."""
        window = self.context_window
        if window is None:
            return request
        left = window - prompt_tokens
        call.window_left = left
        if left <= 0:
            what = f"the prompt holds {prompt_tokens:,} tokens and the context window {window:,}"
            if call.scope != PARENT:
                # A sub-agent filled its own context window.
                raise ModelContextLimitError(f"the context window is full: {what}")
            if first:
                raise PromptAssemblyError(
                    f"the task's own first prompt does not fit the context window: {what}"
                )
            raise ContextFullError(f"nothing is left for a reply: {what}")
        keys = [k for k in ("max_tokens", "max_completion_tokens") if k in request]
        asked = [request[k] for k in keys if isinstance(request[k], int)]
        # Less than the request would have had without the window.
        call.window_cut = not asked or left < min(asked)
        if not call.window_cut:
            return request
        request = dict(request)
        for key in keys or ["max_tokens"]:
            request[key] = left
        return request

    # -- the reply ---------------------------------------------------------------

    def read(
        self,
        call: CallRecord,
        parts: Sequence[tuple[str, Sequence[int]]],
        tools: Sequence[Mapping[str, Any]] | None,
    ) -> ParsedReply:
        """Reasoning, text and tool calls of a reply that arrived in *parts*.

        Two things the reading relies on are looked at on every reply.  The
        check before the run sees them only where the server could be brought
        to generate the tokens concerned, so this is where a server that does
        not keep them is found out: generation stops on the template's
        end-of-turn token, and the text of a marker token comes with it.  A
        reply that breaks one raises :class:`PromptAssemblyError`.  Neither is
        something a model can do: both are the server's.

        A marker token is bound to its text within its part of the reply
        (:func:`~turnstone.rollout.replyformat.pieces_from_chunks`).  Where a
        part does not say which of several occurrences the token is, the
        reply is read by the reader's rule and an event says so
        (``marker_place_uncertain``): among ``events`` for the session's own
        call, among ``subagent_events`` for a sub-agent's.

        What is noted of one of the session's own calls is noted here, once
        its reply has ended and been read, and only where the server said
        which prompt it answered: the call is then one of the token record's.
        That holds for ``prompt_differs_from_rendering`` too, which is found
        when the prompt is built.
        """
        route = self.route
        fmt = self.format
        end_of_turn = route.closing[0]
        generated = call.token_ids
        if end_of_turn in generated and generated.index(end_of_turn) < len(generated) - 1:
            raise PromptAssemblyError(
                f"call {call.index}: the server went on generating after the template's "
                f"end-of-turn token (token {end_of_turn}, generated as number "
                f"{generated.index(end_of_turn) + 1} of {len(generated)})"
            )
        pieces, missing, uncertain = pieces_from_chunks(parts, route.marker_ids, fmt.token_markers)
        if missing:
            raise PromptAssemblyError(
                f"call {call.index}: {missing} marker tokens of the reply came without their "
                "text, so the reply cannot be read as it was generated"
            )
        reply = fmt.read(
            pieces, tools=tools, in_reasoning=route.in_reasoning, text_markers=route.text_markers
        )
        # ``uncertain``: a part held a marker's text more often than its token,
        # between other tokens.  Which of them the token was is the reader's
        # rule, not something the part said.  The model can write that, so the
        # reply is read; whoever reads the record is told.
        if call.scope != PARENT:
            if uncertain:
                noted = {"event": "marker_place_uncertain", "call": call.index}
                self.subagent_events.append({**noted, "tokens": uncertain})
            return reply
        if not call.prompt_token_ids:
            # The server did not say which prompt it answered: the call is
            # none of the token record's, and no event may name it.
            return reply
        last = self._last
        if last is not None and last.call is call and last.differs is not None:
            # The prompt this reply answers is not the chat route's rendering.
            self.events.append(last.differs)
            last.differs = None
        if uncertain:
            noted = {"event": "marker_place_uncertain", "call": self._turn}
            self.events.append({**noted, "tokens": uncertain})
        return reply


class IdStream:
    """A completions reply as the chat-shaped stream a session's provider
    reads, filling the call's record on the way.

    Nothing is handed on until the reply has ended: only then can it be read.
    A reply that ends without a finish reason hands on nothing, which the
    provider takes for the broken stream it is.
    """

    def __init__(
        self,
        stream: Any,
        call: CallRecord,
        assembly: PromptAssembly,
        tools: Sequence[Mapping[str, Any]] | None,
        model: str,
        take: Callable[[CallRecord, Any], tuple[str, list[int]] | None] | None = None,
    ) -> None:
        self._stream = stream
        self._call = call
        self._assembly = assembly
        self._tools = tools
        self._model = model
        # What copies the token data out of each part of the reply: the
        # completions route's reader unless another route's is given.
        self._take = take or take_completion
        self.reply: ParsedReply | None = None

    def _drain(self) -> str | None:
        """Read the reply to its end; the finish reason for the session."""
        call = self._call
        parts: list[tuple[str, list[int]]] = []
        try:
            for part in self._stream:
                now = time.monotonic()
                call.heard(now)
                call.chunks += 1
                taken = self._take(call, part)
                if taken is not None:
                    parts.append(taken)
                if call.first_token_at is None and call.token_ids:
                    call.first_token_at = now
            reason = call.finish_reason
            unsaid = reason is not None and call.prompt_token_ids is None
            if call.route == "generate" and (
                call.prompt_differs or call.placeholders_differ or unsaid
            ):
                # The prompt of a request with images is held to the rendering
                # before it goes out, so no conversation brings this about,
                # the session's or a sub-agent's.  A reply that came whole
                # and never said which prompt it answered shows as little
                # that the images stood where they belong.
                self._assembly.fault = (
                    f"the server answered a request with images (call {call.index}, "
                    f"{call.scope}) "
                    + (
                        "without saying which prompt it answered"
                        if unsaid
                        else "for another prompt, or other places of the images, than it was sent"
                    )
                )
                raise HarnessFaultError(self._assembly.fault)
            if reason is None:
                return None
            call.ended_by_itself = reason == "stop"
            self.reply = self._assembly.read(call, parts, self._tools)
            if reason == "stop" and self.reply.calls:
                # What a chat route says of a turn that ended by itself with calls.
                reason = call.finish_reason = "tool_calls"
            call.complete = True
            return reason
        except BaseException as error:
            call.error = f"{type(error).__name__}: {str(error)[:300]}"
            raise
        finally:
            call.end(time.monotonic())

    def _message(self) -> dict[str, Any]:
        reply = self.reply
        assert reply is not None
        message: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.reasoning is not None:
            message["reasoning"] = reply.reasoning
        if reply.calls:
            message["tool_calls"] = [
                {
                    "index": index,
                    "id": "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {"name": one.name, "arguments": one.arguments},
                }
                for index, one in enumerate(reply.calls)
            ]
        return message

    def _base(self, kind: str) -> dict[str, Any]:
        return {
            "id": "rollout-" + uuid.uuid4().hex[:24],
            "object": kind,
            "created": int(time.time()),
            "model": self._model,
        }

    def __iter__(self) -> Iterator[Any]:
        reason = self._drain()
        if reason is None:
            return
        base = self._base("chat.completion.chunk")
        choice = {"index": 0, "delta": self._message(), "finish_reason": reason, "logprobs": None}
        yield ChatCompletionChunk.model_validate({**base, "choices": [choice]})
        if self._call.usage:
            yield ChatCompletionChunk.model_validate(
                {**base, "choices": [], "usage": self._call.usage}
            )

    def whole(self) -> Any:
        """The reply as one chat completion, for a call that was not streamed."""
        reason = self._drain()
        if reason is None:
            raise PromptAssemblyError("the reply ended without a finish reason")
        message = self._message()
        for one in message.get("tool_calls") or []:
            del one["index"]
        choice = {"index": 0, "message": message, "finish_reason": reason, "logprobs": None}
        return ChatCompletion.model_validate(
            {**self._base("chat.completion"), "choices": [choice], "usage": self._call.usage}
        )

    def __enter__(self) -> IdStream:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        # ``response`` (read by the provider's guard against a non-stream reply)
        # and whatever else the SDK stream offers.
        return getattr(self._stream, name)


# -- the check before a run ------------------------------------------------------

_CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look a word up.",
        "parameters": {
            "type": "object",
            "properties": {"word": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["word"],
        },
    },
}
_CHECK_BASE: list[dict[str, Any]] = [
    {"role": "system", "content": "You are a careful assistant."},
    {"role": "user", "content": "Look up the word alpha."},
]
_CHECK_TURN: dict[str, Any] = {
    "role": "assistant",
    "content": "I will look it up.",
    "reasoning": "The user wants the word alpha looked up. I will call lookup.",
    "tool_calls": [
        {
            "id": "call_check",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"word": "alpha", "limit": 3}'},
        }
    ],
}
_CHECK_RESULT = {"role": "tool", "tool_call_id": "call_check", "content": "alpha: first letter"}


# The request parameters that make a server generate one given token, in the
# order they are tried: a bias on the token's logit at its largest value, which
# a plain completions route takes, and a restriction of the choice to that one
# token, which some servers take that refuse the first.
_STEERING = ("logit_bias", "allowed_token_ids")


def _one_token(
    client: Any,
    model: str,
    prompt: list[int],
    body: Mapping[str, Any],
    *,
    steer: tuple[str, int] | None = None,
) -> tuple[CallRecord, str]:
    """Ask the completions route for one token after *prompt*, read the way a
    run reads it: the likeliest one, or with *steer* (one of ``_STEERING`` and
    a token) the token the server is steered to that way."""
    request = completion_request({"model": model, "extra_body": dict(body)}, prompt)
    request.update({"max_tokens": 1, "temperature": 0.0})
    if steer is not None and steer[0] == "logit_bias":
        request["logit_bias"] = {str(steer[1]): 100}
    elif steer is not None:
        request["extra_body"][steer[0]] = [steer[1]]
    call = CallRecord(index=0, streamed=True, request={}, prompt_sent=prompt)
    text = ""
    for part in client.completions.create(**request):
        taken = take_completion(call, part)
        if taken is not None:
            text += taken[0]
    call.complete = call.finish_reason is not None
    return call, text


@dataclass
class _Steering:
    """The ways still worth trying to make a server generate a given token,
    and why each of the others did not work on it."""

    ways: list[str] = field(default_factory=lambda: list(_STEERING))
    failed: dict[str, str] = field(default_factory=dict)


@dataclass
class _Produced:
    """What came of having the server generate one given token at one place.

    ``own`` is the reply with the model's own likeliest token there, whatever
    it is.  ``call`` is the reply that holds the wanted token, with its text
    and how the token came about (``by``: ``"model"``, or the request
    parameter the server was steered with); it is ``None`` where the token
    could not be had, and ``why_not`` says why.
    """

    own: CallRecord
    own_text: str
    call: CallRecord | None = None
    text: str = ""
    by: str | None = None
    why_not: str | None = None


def _produce(
    client: Any,
    model: str,
    prompt: list[int],
    body: Mapping[str, Any],
    token: int,
    steering: _Steering,
) -> _Produced:
    """Have the server generate *token* after *prompt*, to see how it reports
    that token.

    Which token a model writes at a place is the model's business, and a model
    that is being trained changes.  So the model's own choice is taken where
    it is *token*; where it is not, the server is steered to *token*, in each
    way *steering* has left; and where the server refuses them all, or follows
    none, the token was not seen and the answer says so.  That is no fault of
    the server's.  A reply that holds no token at all is handed back as it is,
    for the caller to refuse.
    """
    own, text = _one_token(client, model, prompt, body)
    if own.token_ids == [token] or not own.token_ids:
        return _Produced(own, text, own, text, by="model")
    for way in list(steering.ways):
        try:
            steered, steered_text = _one_token(client, model, prompt, body, steer=(way, token))
        except Exception as error:
            steering.failed[way] = f"refused ({type(error).__name__}: {str(error)[:120]})"
        else:
            if steered.token_ids == [token] or not steered.token_ids:
                return _Produced(own, text, steered, steered_text, by=way)
            steering.failed[way] = f"not followed (token {steered.token_ids} came for {token})"
        # A way that did not work here is not tried at the next place.
        steering.ways.remove(way)
    return _Produced(
        own,
        text,
        why_not=f"the model wrote token {own.token_ids} there, and the server could not be "
        f"steered to token {token}: "
        + "; ".join(f"{way} {why}" for way, why in steering.failed.items()),
    )


def check_token_id_route(
    client: Any,
    model: str,
    *,
    extra_body: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
    fmt: ReplyFormat = PARAMETER_BLOCKS,
    vision: bool = False,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Whether the server behind *client* (an SDK client) can be sent prompts
    as token IDs, and what a run needs to know to do it.

    Asks for exactly what the mode uses.  ``ok`` is true when every check
    passed; ``reason`` names the first that did not.  The verdict is JSON-safe
    and can be passed to further runs with the same model and template
    arguments (:meth:`TokenIdRoute.from_verdict`).

    The check fails only for what the server does.  Two of its steps need the
    server to generate one given token (each marker, and the end-of-turn
    token) to see how the server reports it.  Where the model's own likeliest
    token is another one, the server is steered to it (``_STEERING``); where it
    cannot be, the step is left unseen: ``checks["not_observed"]`` says which
    and why, ``ok`` stays true, and every reply of a run is held to the same
    thing when it is read.
    """
    body = {k: v for k, v in (extra_body or {}).items() if k != "chat_template_kwargs"}
    request: dict[str, Any] = {"extra_body": dict(extra_body or {})}
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
    arguments = template_arguments(request)
    checks: dict[str, Any] = {}
    verdict: dict[str, Any] = {
        "ok": False,
        "reason": None,
        "format": fmt.name,
        "template_arguments": arguments,
        # What the verdict was made for, and when: a run that is handed it
        # holds these against its own (:mod:`turnstone.rollout.verdicts`).
        **made_for(client, model),
        "checks": checks,
    }

    def failed(reason: str) -> dict[str, Any]:
        verdict["reason"] = reason
        return verdict

    def rendered(conversation: list[dict[str, Any]], generation_prompt: bool) -> list[int]:
        return render(
            client,
            model,
            conversation,
            [_CHECK_TOOL],
            arguments,
            generation_prompt=generation_prompt,
        )

    conversation = [*_CHECK_BASE, _CHECK_TURN, _CHECK_RESULT]
    try:
        # 1. The tokenize route renders messages with tools and template arguments.
        whole = rendered(conversation, True)
        through = rendered(conversation[:3], False)
        before = rendered(_CHECK_BASE, False)
        opened = rendered(_CHECK_BASE, True)
        plain = [
            rendered([*_CHECK_BASE, {"role": "assistant", "content": text}], False)
            for text in ("alpha", "7")
        ]
    except Exception as error:
        return failed(
            "the tokenize route does not render messages with tools and template arguments "
            f"({type(error).__name__}: {str(error)[:200]})"
        )
    checks["tokenize_renders_messages"] = True

    # 2. That rendering is the one the chat route uses.
    chat: dict[str, Any] = {
        "model": model,
        "messages": conversation,
        "tools": [_CHECK_TOOL],
        "max_tokens": 1,
        "extra_body": {**dict(extra_body or {}), "return_token_ids": True},
    }
    if reasoning_effort is not None:
        chat["reasoning_effort"] = reasoning_effort
    reported: Any = None
    asked: list[str] = []
    for choice in (None, "none"):
        # A server without a reader of tool calls refuses a request that
        # leaves the choice of tools to the model; it renders the same.
        try:
            answer = client.chat.completions.create(
                **chat, **({"tool_choice": choice} if choice else {})
            )
        except Exception as error:
            asked.append(f"{type(error).__name__}: {str(error)[:120]}")
            continue
        reported = extra_field(answer, "prompt_token_ids")
        break
    if reported is None:
        return failed(
            "the chat route did not report the prompt it rendered, so the tokenize route's "
            f"rendering cannot be compared with it ({'; '.join(asked) or 'no prompt token IDs'})"
        )
    checks["chat_route_renders_the_same"] = [int(t) for t in reported] == whole
    if not checks["chat_route_renders_the_same"]:
        return failed(
            "the tokenize route renders the conversation differently from the chat route "
            f"({len(whole)} tokens against {len(reported)})"
        )

    # 3. The generation prompt, the closing of an assistant turn, and where a
    # turn ends in a longer rendering.
    if opened[: len(before)] != before or len(opened) == len(before):
        return failed("the rendering with a generation prompt does not extend the one without")
    generation_prompt = opened[len(before) :]
    closing = _common_suffix(*plain)
    checks["turn_is_a_prefix_of_the_longer_rendering"] = whole[: len(through)] == through
    checks["turn_ends_with_the_closing"] = bool(closing) and through[-len(closing) :] == closing
    checks["rendering_ends_with_the_generation_prompt"] = (
        whole[-len(generation_prompt) :] == generation_prompt
    )
    for name in (
        "turn_is_a_prefix_of_the_longer_rendering",
        "turn_ends_with_the_closing",
        "rendering_ends_with_the_generation_prompt",
    ):
        if not checks[name]:
            return failed(
                f"the template does not render the way the mode needs ({name.replace('_', ' ')})"
            )

    # 4. The format's markers: one token each, and kept in the generated text.
    marker_ids: dict[str, int] = {}
    text_markers: list[str] = []
    for name, text in fmt.token_markers.items():
        try:
            ids = text_tokens(client, model, text)
        except Exception as error:
            return failed(
                f"the tokenize route does not tokenize text ({type(error).__name__}: "
                f"{str(error)[:200]})"
            )
        if len(ids) == 1:
            marker_ids[name] = ids[0]
        else:
            text_markers.append(name)
    names = {token: name for name, token in marker_ids.items()}
    in_prompt = [names[t] for t in generation_prompt if t in names]
    turn = through[len(before) :]
    # What the next two steps see needs the server to generate one given token
    # at one place.  Which token a model writes there is the model's business,
    # so a token that could not be had is not the server's failure: the thing
    # is left unseen (``None``, with the reason under ``not_observed``), the
    # run goes ahead, and what is checked on every reply of a run catches a
    # server that does it wrong (:meth:`PromptAssembly.read`).
    kept: dict[str, bool | None] = {}
    produced_by: dict[str, str] = {}
    not_observed: dict[str, str] = {}
    no_token: CallRecord | None = None
    steering = _Steering()
    try:
        for name, token in marker_ids.items():
            if token not in turn:
                kept[name] = None
                not_observed[name] = "the template does not write it in an assistant turn"
                continue
            at = len(before) + turn.index(token)
            seen = _produce(client, model, through[:at], body, token, steering)
            if seen.call is None:
                kept[name] = None
                not_observed[name] = str(seen.why_not)
            elif not seen.call.token_ids:
                no_token = no_token or seen.call
            else:
                kept[name] = seen.text == fmt.token_markers[name]
                produced_by[name] = str(seen.by)
        # 5. The end of a turn: the template's token, among the IDs, and a stop.
        end = _produce(client, model, through[: -len(closing)], body, closing[0], steering)
    except Exception as error:
        return failed(
            "the completions route does not take a prompt of token IDs "
            f"({type(error).__name__}: {str(error)[:200]})"
        )
    checks["markers_kept_in_text"] = kept
    lost = [name for name, value in kept.items() if value is False]
    if lost:
        return failed(
            "the generated text does not keep the markers the reader needs: " + ", ".join(lost)
        )
    # The reply itself, on whichever token the model wrote at the end of the turn.
    problems = end.own.problems()
    checks["completions_route"] = {
        "token_ids": end.own.token_ids,
        "finish_reason": end.own.finish_reason,
        "text": end.own_text,
        "problems": problems,
    }
    no_token = no_token or (end.call if end.call is not None and not end.call.token_ids else None)
    if not problems and no_token is not None:
        problems = no_token.problems()
    if problems:
        return failed(
            "the completions route's reply to a prompt of token IDs cannot be trained on: "
            + "; ".join(problems)
        )
    ends: bool | None = None
    if end.call is None:
        not_observed["end_of_turn"] = str(end.why_not)
    elif end.call.finish_reason != "stop":
        return failed(
            "a reply that ended on the template's end-of-turn token did not end with a stop: "
            f"finish reason {end.call.finish_reason!r}"
        )
    else:
        ends = True
        produced_by["end_of_turn"] = str(end.by)
    checks["end_of_turn_token_ends_the_reply"] = ends
    checks["produced_by"] = produced_by
    checks["not_observed"] = not_observed
    verdict.update(
        ok=True,
        marker_ids=marker_ids,
        text_markers=text_markers,
        closing=closing,
        generation_prompt=generation_prompt,
        in_reasoning=fmt.opens_in_reasoning(in_prompt),
    )
    if vision:
        # Whether images can go beside the token IDs: part of the same
        # verdict, with a word of its own.  A run that is to see images is
        # refused on it; the route for text is not failed by it.
        from turnstone.rollout.vision import check_vision_route

        try:
            verdict["vision"] = check_vision_route(
                client,
                model,
                extra_body=extra_body,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
            )
        except Exception as error:
            verdict["vision"] = {
                "ok": False,
                "reason": (
                    "the image check could not be finished "
                    f"({type(error).__name__}: {str(error)[:300]})"
                ),
                "checks": {},
            }
    return verdict


def check_plain_text(
    client: Any,
    model: str,
    *,
    extra_body: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
    added_tokens: Mapping[str, int] | None = None,
    written: Sequence[int] = (),
    required: Sequence[int] = (),
    uncompared: bool = False,
) -> dict[str, Any]:
    """How the text of the tokenizer's own tokens is read as plain text on
    the server behind *client* (:func:`~turnstone.rollout.plaintext.plain_text_verdict`).

    *added_tokens* are those tokens, text to ID, from a description of the
    tokenizer; ``None`` asks the server for them (``/tokenizer_info``, which a
    server offers when it is started for it).  *written* are tokens a reply's
    reading relies on or the template writes for an image, and *required*
    those of them the description must name.

    A description from a file is held against the server's own: one that
    names other tokens than the server's tokenizer has fails the check.  A
    file that is stale or leaves a token out reads that token's text as a
    control token, and nothing the template shows need give it away, so the
    comparison is not left out quietly: on a server that does not offer its
    own description (the route answers 404) the check FAILS, unless the
    caller says with *uncompared* that the file is to be taken as it is.
    The verdict then says so (``tokenizer_info``: ``compared_with_server``
    false, ``accepted_uncompared`` true).  Only a 404 means "not offered":
    any other failure to get the server's description fails the check,
    whatever *uncompared* says, and so does an answer that names no tokens.

    Never raises: what could not be established is a verdict that says so.
    The verdict names what it was made for, the template arguments
    included."""
    request: dict[str, Any] = {"extra_body": dict(extra_body or {})}
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
    arguments = template_arguments(request)
    for_what = {"template_arguments": arguments, **made_for(client, model)}

    def failed(reason: str, **more: Any) -> dict[str, Any]:
        return {"ok": False, "reason": reason, **more, **for_what}

    served: dict[str, int] | None = None
    try:
        served = added_tokens_of(client.get(_root(client) + "/tokenizer_info", cast_to=object))
    except Exception as error:
        said = f"{type(error).__name__}: {str(error)[:120]}"
        if not (isinstance(error, APIStatusError) and error.status_code == 404):
            # The server may well offer it: nothing is known of its tokens,
            # and nothing passes on that.
            return failed(
                "the server's own description of its tokenizer could not be had "
                f"(/tokenizer_info: {said}), so nothing could be held against it: ask again",
                tokenizer_info={"asked": False, "why_not": said},
            )
    else:
        if not served:
            return failed(
                "the server's own description of its tokenizer (/tokenizer_info) names no "
                "tokens of the tokenizer's own, so nothing could be held against it",
                tokenizer_info={"asked": True, "why_not": "the server's answer names no tokens"},
            )
    added = added_tokens
    compared: dict[str, Any]
    if added is None:
        if served is None:
            return failed(
                "the server does not say which tokens its tokenizer has of its own "
                "(/tokenizer_info answers 404): start it with "
                "--enable-tokenizer-info-endpoint, or name them from the tokenizer's files"
            )
        added = served
        compared = {"source": "server"}
    elif served is None:
        compared = {
            "source": "named",
            "compared_with_server": False,
            "why_not": "the server does not offer /tokenizer_info (404)",
            "accepted_uncompared": bool(uncompared),
        }
        if not uncompared:
            return failed(
                "the named description of the tokenizer could not be compared with the "
                "server's own: the server does not offer /tokenizer_info (404). A file that is "
                "stale or leaves a token out would pass unnoticed. Start the server with "
                "--enable-tokenizer-info-endpoint, or say that the file is to be taken as it is "
                "(--tokenizer-info-uncompared, tokenizer_info_uncompared)",
                tokenizer_info=compared,
            )
    else:
        compared = {"source": "named", "compared_with_server": True}
        apart = sorted(set(added.items()) ^ set(served.items()))
        if apart:
            return failed(
                "the named description of the tokenizer is not the server's own "
                f"(/tokenizer_info): they differ in {len(apart)} tokens, first "
                + ", ".join(f"{text!r} ({token})" for text, token in apart[:5]),
                tokenizer_info=compared,
            )
    if not added:
        return failed(
            "the description of the tokenizer names no tokens of its own", tokenizer_info=compared
        )

    def text_of(ids: list[int]) -> str:
        reply = client.post(
            _root(client) + "/detokenize", body={"model": model, "tokens": ids}, cast_to=object
        )
        return str(reply.get("prompt")) if isinstance(reply, dict) else ""

    try:
        found = plain_text_verdict(
            lambda conversation, tools: render(client, model, conversation, tools, arguments),
            lambda text: text_tokens(client, model, text),
            text_of,
            added,
            written,
            required,
        )
    except Exception as error:
        return failed(
            f"it could not be established ({type(error).__name__}: {str(error)[:200]})",
            tokenizer_info=compared,
        )
    return {**found, "tokenizer_info": compared, **for_what}
