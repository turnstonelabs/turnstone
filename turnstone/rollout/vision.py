"""Images in a rollout whose prompts go out as token IDs.

Off unless a run asks for it (``RolloutConfig.vision``).  With it on, the
session's model is given vision, so the framework's own paths bring an image
into the conversation: ``read_file`` on an image file and an image a tool of
the task's servers returns both arrive as ``image_url`` parts of the request's
messages, the way they do for any model with vision.  Nothing of that is built
here.  What this module adds is the part a prompt of token IDs lacks: the
pixels.

The server's tokenize route renders an image as a run of one placeholder
token, as many as the image has patches after the server's own resizing.  The
completions route takes token IDs and nothing else, so a prompt with such a run
would reach the model without the image.  A request whose messages hold an
image therefore goes to the server's generate route
(``POST /inference/v1/generate``) instead, with the same token IDs and the
images beside them.  That route expands placeholders itself: given one
placeholder token per image, in the order of the images, it writes the run and
puts the image's embeddings there.  So the prompt is sent with every image's
run cut to one token, and the server is held to answering with exactly the
prompt the tokenize route rendered:

- the prompt it reports must be that rendering, token by token;
- the places it reports for the images must be the runs found in it.

A reply that fails either is not trainable, and no prompt is built on it: the
run ends as a failure of the machinery.  Everything else is as on the
completions route.  The prompt is still the previous prompt, the tokens
generated after it and the template's tail; the reply is read on this side;
token IDs and log-probabilities are kept one to one and compared by ID; the
context window is kept exactly, images counted at their real length.  A
request without an image goes to the completions route as before.

What the record says of each image is in :meth:`Vision.describe`, and what a
server must do for all of this is established once, before a run, by
:func:`check_vision_route`.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import math
import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openai import APIStatusError, Stream

from turnstone.core.log import get_logger
from turnstone.core.session import ChatSession
from turnstone.rollout.idprompt import (
    IdStream,
    PromptAssemblyError,
    _root,
    completion_request,
    render,
    template_arguments,
)
from turnstone.rollout.plaintext import HarnessFaultError
from turnstone.rollout.recording import (
    _ID_PREFIX,
    PARENT,
    CallRecord,
    _misaligned,
    _usage_dict,
)
from turnstone.rollout.settings import SetupError
from turnstone.rollout.tokens import common_prefix_len

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from turnstone.rollout.idprompt import PromptAssembly

log = get_logger(__name__)

GENERATE_PATH = "/inference/v1/generate"

# The settings of a request that the generate route takes as sampling
# parameters.  The route drops a parameter it does not know without a word, so
# one that is not listed here is refused on this side instead.
_SAMPLING_PARAMETERS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "seed",
        "stop",
        "stop_token_ids",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
        "max_tokens",
        "min_tokens",
        "ignore_eos",
        "skip_special_tokens",
        "spaces_between_special_tokens",
        "include_stop_str_in_output",
        "logit_bias",
        "allowed_token_ids",
        "bad_words",
    }
)
# Keywords of a completions request that are sampling parameters there too.
_KEYWORDS = (
    "temperature",
    "top_p",
    "seed",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "max_tokens",
)
# What a completions request asks for in its body that the generate route is
# asked for another way: the prompt it used at the top of the request, and
# tokens named by ID by the way the server was started.
_ASKED_ANOTHER_WAY = frozenset({"return_token_ids", "return_tokens_as_token_ids"})
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class PlaceholderGeneratedError(Exception):
    """The model generated the token an image is rendered as, and a later
    request holds an image.  A server gives the image to the first such token
    it finds, so the image cannot be sent where it belongs.  The run ends
    there, as the model's own outcome (``image_token_generated``)."""


@dataclass(frozen=True)
class Image:
    """One image of a request: its bytes as they are sent, and their hash."""

    data: bytes
    media_type: str
    sha256: str

    @classmethod
    def of(cls, data: bytes, media_type: str) -> Image:
        return cls(data, media_type, hashlib.sha256(data).hexdigest())

    @property
    def url(self) -> str:
        return f"data:{self.media_type};base64,{base64.b64encode(self.data).decode('ascii')}"

    @property
    def file(self) -> str:
        """The name of the file that holds the bytes: their hash."""
        return f"{self.sha256}.{_EXTENSIONS.get(self.media_type, 'bin')}"

    def part(self) -> dict[str, Any]:
        """The image as a content part of a message."""
        return {"type": "image_url", "image_url": {"url": self.url}}


def images_of(
    messages: Sequence[Mapping[str, Any]], known: dict[str, Image] | None = None
) -> list[Image]:
    """The images a request's messages carry, in the order a chat template
    writes them: message by message, part by part.  *known* remembers each
    image by the text that carries it, so that one is decoded and hashed once
    however many requests hold it.

    Raises :class:`PromptAssemblyError` for an image that is not carried in
    the request itself (an address the server would fetch): its bytes could
    not be kept, and the record could not say what the model saw.
    """
    found: list[Image] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            given = part.get("image_url")
            url = str((given.get("url") if isinstance(given, dict) else given) or "")
            image = known.get(url) if known is not None else None
            if image is None:
                # The framework's own reading of such a part: the bytes and their hash.
                decoded = ChatSession._decode_image_part(
                    {"type": "image_url", "image_url": {"url": url}}, "tool"
                )
                if decoded is None:
                    raise PromptAssemblyError(
                        "a message holds an image that is not carried in the request itself "
                        "(no data URL that can be decoded), so its bytes cannot be sent beside "
                        "the token IDs or kept"
                    )
                image = Image(decoded.content, decoded.mime_type, decoded.attachment_id)
                if known is not None:
                    known[url] = image
            found.append(image)
    return found


def collapsed(prompt: Sequence[int], places: Sequence[tuple[int, int]]) -> list[int]:
    """*prompt* with each run of placeholder tokens at *places* cut to its
    first token: the form the generate route expands again."""
    out: list[int] = []
    cursor = 0
    for start, length in places:
        out += prompt[cursor : start + 1]
        cursor = start + length
    return out + list(prompt[cursor:])


def find_places(
    prompt: list[int], images: int, placeholder: int, frame: tuple[int, int]
) -> list[tuple[int, int]]:
    """Where the *images* images of a conversation stand in *prompt*, its
    rendering: the runs of placeholder tokens the template wrote, each
    between the two tokens of *frame*, in order.

    A server gives the first image the first placeholder token it finds, the
    next image the first after that image's run, and so on.  So the prompt
    can be sent with each run cut to one token only when the template's
    places are the first placeholder tokens there are.  Raises
    :class:`PromptAssemblyError` where they are not: the prompt holds another
    number of places than the conversation has images, or a placeholder token
    that is no image's stands before one.  With the conversation's text read
    as plain text neither comes from what the environment wrote.
    """
    places: list[tuple[int, int]] = []
    loose: list[int] = []
    at = 0
    size = len(prompt)
    while at < size:
        if prompt[at] != placeholder:
            at += 1
            continue
        end = at
        while end < size and prompt[end] == placeholder:
            end += 1
        if at and prompt[at - 1] == frame[0] and end < size and prompt[end] == frame[1]:
            places.append((at, end - at))
        else:
            loose.append(at)
        at = end
    if len(places) != images:
        raise PromptAssemblyError(
            f"the prompt holds {len(places)} places the template wrote for an image, and the "
            f"conversation holds {images} images"
        )
    if places and loose and loose[0] < places[-1][0]:
        raise PromptAssemblyError(
            f"a placeholder token that is no image's stands at position {loose[0]}, before an "
            "image: the server would give the image to it"
        )
    return places


def generate_body(
    outgoing: Mapping[str, Any], token_ids: list[int], images: Sequence[Image]
) -> dict[str, Any]:
    """The request to the generate route that stands for the completions
    request *outgoing* (:func:`~turnstone.rollout.idprompt.completion_request`)
    once its prompt is *token_ids* and *images* go with it: same model, same
    sampler settings, same reply limit, streamed, text and token IDs and
    log-probabilities asked for.

    Raises :class:`PromptAssemblyError` for a request parameter the route has
    no place for.  It would drop the parameter and answer, and the reply
    would have been sampled another way than the record says.
    """
    sampling: dict[str, Any] = {name: outgoing[name] for name in _KEYWORDS if name in outgoing}
    for name, value in (outgoing.get("extra_body") or {}).items():
        if name in _ASKED_ANOTHER_WAY:
            continue
        if name not in _SAMPLING_PARAMETERS:
            raise PromptAssemblyError(
                f"the request parameter {name!r} has no place in a request to the generate "
                "route, which drops what it does not know and answers all the same"
            )
        sampling[name] = copy.deepcopy(value)
    # The value of each sampled token, no alternatives; more where asked.
    sampling["logprobs"] = outgoing.get("logprobs", 0)
    body: dict[str, Any] = {
        "model": outgoing.get("model"),
        "token_ids": token_ids,
        "sampling_params": sampling,
        # The prompt after the server's own expansion, and where the images are.
        "return_token_ids": True,
        "output_mode": "text",
        "stream": bool(outgoing.get("stream")),
    }
    if images:
        body["content_parts"] = [{"type": "image_url", "url": image.url} for image in images]
    if body["stream"]:
        body["stream_options"] = {"include_usage": True}
    return body


def _image_places(given: Any) -> list[list[int]] | None:
    """The places a reply reports for its images, ``None`` where it reports none."""
    ranges = given.get("image") if isinstance(given, dict) else None
    if ranges is None:
        return None
    return [[int(one["offset"]), int(one["length"])] for one in ranges]


def take_generate(record: CallRecord, part: Any) -> tuple[str, list[int]] | None:
    """Copy token data out of one part of a reply of the generate route into
    *record*; return the part's text and token IDs.  Never raises.

    The pairing rules of the completions route: token IDs and
    log-probabilities are kept only one to one, and where the server names
    each value's token by ID the two channels are compared token by token.
    The prompt the server reports is held against the one it was to arrive at
    (``record.prompt_sent``, the tokenize route's rendering), and the places
    it reports for the images against the runs found there
    (``record.placeholders_sent``).
    """
    try:
        if not isinstance(part, dict):
            raise TypeError("a part of the reply is no object")
        taken: tuple[str, list[int]] | None = None
        reported = part.get("prompt_token_ids")
        if reported is not None and record.prompt_token_ids is None:
            prompt = [int(i) for i in reported]
            if record.prompt_sent is not None and prompt == record.prompt_sent:
                # One list for both: a long run holds every prompt.
                prompt = record.prompt_sent
            elif record.prompt_sent is not None:
                record.prompt_differs = True
            record.prompt_token_ids = prompt
            record.prompt_sent = None
            if record.placeholders_sent is not None:
                places = _image_places(part.get("mm_placeholders"))
                record.placeholders_differ = places != record.placeholders_sent
        for choice in part.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            ids = [int(i) for i in (choice.get("token_ids") or [])]
            text = choice.get("text") or ""
            record.token_ids.extend(ids)
            record.raw.extend(text.encode("utf-8", errors="replace"))
            entries = list((choice.get("logprobs") or {}).get("content") or [])
            if len(ids) != len(entries):
                _misaligned(record)
            elif record.aligned:
                named = [str(entry.get("token")) for entry in entries]
                if named and all(n.startswith(_ID_PREFIX) for n in named):
                    record.ids_checked = True
                    if named != [f"{_ID_PREFIX}{i}" for i in ids]:
                        _misaligned(record)
                if record.aligned:
                    record.logprobs.extend(float(entry["logprob"]) for entry in entries)
            reason = choice.get("finish_reason")
            if reason:
                record.finish_reason = reason
            taken = (text, ids)
        usage = _usage_dict(part.get("usage"))
        if usage:
            record.usage = usage
        return taken
    except Exception as error:
        record.unreadable = f"{type(error).__name__}: {str(error)[:200]}"
        if record.aligned:
            _misaligned(record)
        log.warning("rollout.recording_unreadable", call=record.index, error=record.unreadable)
        return None


def ask_generate(
    client: Any,
    model: str,
    token_ids: list[int],
    images: Sequence[Image] = (),
    *,
    expected: list[int] | None = None,
    places: Sequence[tuple[int, int]] | None = None,
    alternatives: int = 0,
    **settings: Any,
) -> tuple[CallRecord, str, list[dict[str, Any]]]:
    """One reply of the generate route to *token_ids* with *images*, asked
    for and read the way a run does it, streamed: the call's record, the
    reply's text and its log-probability entries (each with the alternatives
    the server listed).

    *expected* is the prompt the server is to report (the IDs sent where
    nothing is to be expanded) and *places* where it is to say the images
    are.  *settings* are sampler settings and the reply limit; *alternatives*
    is how many tokens the server is to list at each position.
    """
    outgoing = {"model": model, "extra_body": settings, "stream": True, "logprobs": alternatives}
    body = generate_body(outgoing, token_ids, images)
    call = CallRecord(index=0, streamed=True, request={}, route="generate")
    call.prompt_sent = list(token_ids) if expected is None else expected
    if images:
        call.placeholders_sent = [list(place) for place in places or ()]
    text = ""
    entries: list[dict[str, Any]] = []
    stream = client.post(
        _root(client) + GENERATE_PATH,
        body=body,
        cast_to=object,
        stream=True,
        stream_cls=Stream[object],
    )
    for part in stream:
        taken = take_generate(call, part)
        if taken is not None:
            text += taken[0]
        for choice in (part.get("choices") if isinstance(part, dict) else None) or []:
            if isinstance(choice, dict) and choice.get("index", 0) == 0:
                entries += list((choice.get("logprobs") or {}).get("content") or [])
    call.complete = call.finish_reason is not None
    return call, text, entries


# -- what a check established ------------------------------------------------------


@dataclass(frozen=True)
class ResizeRule:
    """How a server sizes an image before it cuts it into patches, as far as
    the number of placeholder tokens shows it: each side is rounded to a
    multiple of *multiple* pixels, the whole is scaled to hold at least
    *min_pixels* and at most *max_pixels*, and one token stands for a square
    of *multiple* pixels.  The rule of the Qwen image processors; a check
    says whether the server at hand follows it."""

    multiple: int = 32
    min_pixels: int = 65536
    max_pixels: int = 16777216
    patch: int = 16

    def size(self, width: int, height: int) -> tuple[int, int]:
        """The width and height an image of *width* by *height* is resized to."""
        m = self.multiple
        high = max(m, round(height / m) * m)
        wide = max(m, round(width / m) * m)
        if high * wide > self.max_pixels:
            beta = math.sqrt(height * width / self.max_pixels)
            high = max(m, math.floor(height / beta / m) * m)
            wide = max(m, math.floor(width / beta / m) * m)
        elif high * wide < self.min_pixels:
            beta = math.sqrt(self.min_pixels / (height * width))
            high = math.ceil(height * beta / m) * m
            wide = math.ceil(width * beta / m) * m
        return wide, high

    def tokens(self, width: int, height: int) -> int:
        wide, high = self.size(width, height)
        return (wide // self.multiple) * (high // self.multiple)

    def describe(self) -> dict[str, int]:
        return {
            "multiple": self.multiple,
            "min_pixels": self.min_pixels,
            "max_pixels": self.max_pixels,
            "patch": self.patch,
        }


@dataclass(frozen=True)
class VisionRoute:
    """What :func:`check_vision_route` established about a server: all a run
    needs to send its images."""

    # The token an image is rendered as, once per patch.
    placeholder: int
    # The two tokens the template writes around an image's placeholders.
    frame: tuple[int, int]
    # The rule the server's counts follow, where the check found one.
    resize: ResizeRule | None = None

    @classmethod
    def from_verdict(cls, verdict: Mapping[str, Any]) -> VisionRoute:
        """The route a passed check's verdict describes.

        Raises :class:`~turnstone.rollout.settings.SetupError` for a verdict
        that says the server cannot, and for one that does not hold what a
        passed check found.  A verdict can come from a file somebody else
        wrote.
        """
        if not verdict.get("ok"):
            raise SetupError(f"the server cannot be sent images: {verdict.get('reason')}")
        probed = verdict.get("sampler_logprobs")
        if not isinstance(probed, dict) or probed.get("sampler_logprobs") is not True:
            raise SetupError(
                "the given check of the server for images does not hold what a passed check "
                "found (sampler_logprobs must be the probe of the generate route, with every "
                "sampler setting reflected): check again"
            )
        token = verdict.get("placeholder")
        if isinstance(token, bool) or not isinstance(token, int):
            raise SetupError(
                "the given check of the server for images does not hold what a passed check "
                "found (placeholder must be the token an image is rendered as): check again"
            )
        rule = verdict.get("resize_rule")
        resize: ResizeRule | None = None
        if rule is not None:
            try:
                resize = ResizeRule(**{name: int(rule[name]) for name in ResizeRule().describe()})
            except (KeyError, TypeError, ValueError):
                raise SetupError(
                    "the given check of the server for images does not hold what a passed "
                    "check found (resize_rule must name multiple, min_pixels, max_pixels and "
                    "patch, or be null): check again"
                ) from None
        around = verdict.get("frame")
        if (
            not isinstance(around, (list, tuple))
            or len(around) != 2
            or any(isinstance(t, bool) or not isinstance(t, int) for t in around)
        ):
            raise SetupError(
                "the given check of the server for images does not hold what a passed check "
                "found (frame must be the two tokens the template writes around an image's "
                "placeholders): check again"
            )
        return cls(placeholder=token, frame=(int(around[0]), int(around[1])), resize=resize)


# -- a run's images ----------------------------------------------------------------


@dataclass
class Placed:
    """One image of the session's own sequence: what it is and where it stands."""

    image: Image
    start: int
    length: int


@dataclass
class Vision:
    """Sends the requests of one run that carry images, and keeps what the
    record says of them (``PromptAssembly(vision=...)``)."""

    route: VisionRoute
    # The images of the session's own conversation, in its order.  Each is
    # kept once it has a place in a prompt, and the place never changes.
    images: list[Placed] = field(default_factory=list)
    # The most images one request of a sub-agent carried: counted, not kept.
    # Nothing of a sub-agent is part of the rollout's sequence.
    subagent_images: int = 0
    # Whether the model itself generated the placeholder token in one of the
    # session's own turns.
    generated: bool = False
    # Each image by the text that carried it: decoded and hashed once.
    _known: dict[str, Image] = field(default_factory=dict)

    def images_of(self, messages: Sequence[Mapping[str, Any]]) -> list[Image]:
        """The images a request's messages carry, in order."""
        return images_of(messages, self._known)

    def note_generated(self, tokens: Sequence[int]) -> None:
        """Note what one of the session's own replies holds."""
        if self.route.placeholder in tokens:
            self.generated = True

    def start(
        self,
        assembly: PromptAssembly,
        inner: Any,
        call: CallRecord,
        request: dict[str, Any],
        prompt: list[int],
        images: list[Image],
    ) -> IdStream:
        """Send the chat request *request*, whose messages hold *images*, with
        its prompt as token IDs and the images beside them.  *prompt* is the
        prompt as the tokenize route's rendering has it."""
        model = str(request.get("model"))
        arguments = template_arguments(request)
        try:
            places = find_places(prompt, len(images), self.route.placeholder, self.route.frame)
        except PromptAssemblyError as misplaced:
            # The environment's text holds no placeholder token: it is read as
            # plain text.  So either the model generated one, which is its
            # own doing, or this code is at fault.
            if call.scope == PARENT and self.generated:
                raise PlaceholderGeneratedError(
                    "the model generated the token an image is rendered as, and the server "
                    f"would give a later image to it ({misplaced})"
                ) from None
            assembly.fault = f"an image has no place in a prompt the harness built: {misplaced}"
            raise HarnessFaultError(assembly.fault) from None
        if call.scope == PARENT:
            self._keep(images, places)
        else:
            self.subagent_images = max(self.subagent_images, len(images))
        # The same request as on the completions route, with its record.
        outgoing = completion_request(request, prompt)
        body = generate_body(outgoing, collapsed(prompt, places), images)
        messages = request.get("messages") or []
        # What the server is to report: the rendering, and the images' runs.
        call.prompt_sent = prompt
        call.placeholders_sent = [list(place) for place in places]
        call.route = "generate"
        call.request = {
            "model": outgoing.get("model"),
            "messages": len(messages),
            "roles": [m.get("role") for m in messages if isinstance(m, dict)],
            "tools": [t.get("function", {}).get("name") for t in request.get("tools") or []],
            "sampling": {
                k: outgoing[k] for k in ("temperature", "top_p", "max_tokens") if k in outgoing
            },
            "extra_body": dict(outgoing["extra_body"]),
            "template_arguments": arguments,
            "prompt_tokens_sent": len(prompt),
            "images": len(images),
        }
        options: dict[str, Any] = {}
        if outgoing.get("extra_headers"):
            options["headers"] = dict(outgoing["extra_headers"])
        response = inner.post(
            _root(inner) + GENERATE_PATH,
            body=body,
            cast_to=object,
            options=options,
            stream=True,
            stream_cls=Stream[object],
        )
        return IdStream(
            response, call, assembly, request.get("tools") or None, model, take=take_generate
        )

    def _keep(self, images: list[Image], places: list[tuple[int, int]]) -> None:
        """Keep the images of one of the session's own requests.  The
        conversation only grows, so what was kept before must stand where it
        stood."""
        held = self.images
        if len(images) < len(held):
            raise PromptAssemblyError(
                f"the request holds {len(images)} images and the one before it held "
                f"{len(held)}: an image of the conversation is gone"
            )
        for index, (image, (start, length)) in enumerate(zip(images, places, strict=True)):
            if index >= len(held):
                held.append(Placed(image, start, length))
            elif (held[index].image.sha256, held[index].start, held[index].length) != (
                image.sha256,
                start,
                length,
            ):
                raise PromptAssemblyError(
                    f"image {index} of the conversation is not what, or not where, it was in "
                    "the request before"
                )

    def describe(
        self,
        calls: Sequence[Mapping[str, Any]],
        final: Sequence[int],
        sources: Sequence[Mapping[str, Any] | None],
        folder: str | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """What the record says of the run's images, and why the record
        cannot be trained on where it cannot be rebuilt.

        *calls* are the token record's calls and *final* the final sequence.
        An image is the record's when its run lies in the prompt of a call the
        record holds; one a request carried that never came back whole is left
        out.  *sources* says where each image came from, in order (``None``
        where that is not known).  The bytes are written to *folder* under
        their hash, before the record is, so that no record names a file that
        is not there.
        """
        problems: list[str] = []
        prompt_lens = [int(call["prompt_len"]) for call in calls]
        last = prompt_lens[-1] if prompt_lens else 0
        token = self.route.placeholder
        entries: list[dict[str, Any]] = []
        for index, placed in enumerate(self.images):
            end = placed.start + placed.length
            if end > last:
                continue
            image = placed.image
            if any(t != token for t in final[placed.start : end]):
                problems.append(
                    f"image {index}: the final sequence does not hold its placeholder tokens "
                    f"at {placed.start}..{end}"
                )
            width, height = image_size(image.data)
            processed: dict[str, Any] | None = None
            rule = self.route.resize
            if (
                rule is not None
                and width
                and height
                and rule.tokens(width, height) == (placed.length)
            ):
                wide, high = rule.size(width, height)
                processed = {
                    "width": wide,
                    "height": high,
                    # Frames, rows and columns of patches, before they are merged.
                    "grid": [1, high // rule.patch, wide // rule.patch],
                }
            entries.append(
                {
                    "index": index,
                    "sha256": image.sha256,
                    "file": image.file,
                    "media_type": image.media_type,
                    "bytes": len(image.data),
                    "width": width,
                    "height": height,
                    # Where its placeholder tokens stand in the final sequence.
                    "start": placed.start,
                    "end": end,
                    "tokens": placed.length,
                    "processed": processed,
                    # The first call whose prompt holds it.
                    "first_call": next(n for n, size in enumerate(prompt_lens) if size >= end),
                    "source": _source(sources, index, image),
                }
            )
            written = write_image(folder, image)
            if written is not None:
                problems.append(f"image {index} could not be kept ({written})")
        inside = [False] * len(final)
        for entry in entries:
            inside[entry["start"] : entry["end"]] = [True] * entry["tokens"]
        stray = [i for i, t in enumerate(final) if t == token and not inside[i]]
        return {
            "placeholder": token,
            "images": entries,
            # Placeholder tokens of the final sequence that are no image's:
            # written by a tokenizer for the placeholder's text, or generated.
            # The server saw them as ordinary tokens.
            "stray_placeholders": {"count": len(stray), "positions": stray[:64]},
            "subagent_images": self.subagent_images,
        }, problems


def _source(
    sources: Sequence[Mapping[str, Any] | None], index: int, image: Image
) -> dict[str, Any] | None:
    """Where image *index* came from, where the session's conversation says
    so of an image with these bytes."""
    source = sources[index] if index < len(sources) else None
    if source is None or source.get("sha256") not in (None, image.sha256):
        return None
    return {name: value for name, value in source.items() if name != "sha256"}


def image_size(data: bytes) -> tuple[int | None, int | None]:
    """Width and height of the image *data* holds, turned the way its
    orientation tag says: the size a server works from, which turns the image
    upright before it resizes it."""
    try:
        from PIL import Image as _Pil

        from turnstone.core.images import exif_orientation, too_many_jpeg_scans

        if too_many_jpeg_scans(data):
            return None, None
        with _Pil.open(io.BytesIO(data)) as opened:
            width, height = opened.size
            # Orientations 5 to 8 lay the picture on its side.
            if exif_orientation(opened) >= 5:
                width, height = height, width
            return int(width), int(height)
    except Exception:
        return None, None


def write_image(folder: str | None, image: Image) -> str | None:
    """Write *image* into *folder* under its hash, whole or not at all; say
    why where that failed.  A file that is there already holds the same
    bytes and is left alone."""
    if not folder:
        return "the run names no folder for its images"
    path = os.path.join(folder, image.file)
    temporary = f"{path}.{os.getpid()}.part"
    try:
        os.makedirs(folder, exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) == len(image.data):
            return None
        with open(temporary, "wb") as handle:
            handle.write(image.data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        return f"{type(error).__name__}: {error}"
    return None


def image_sources(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any] | None]:
    """Where each image of a session's conversation came from, in order: the
    tool call whose result held it (its id, the tool, its arguments, and the
    turn that made it, counted as the tool log counts turns), or ``None`` for
    an image of another kind of message.

    *messages* are the session's own, as the record holds them: an image is a
    part that names its bytes by their hash, not the bytes.
    """
    calls: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any] | None] = []
    turn = -1
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            turn += 1
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                calls[str(call.get("id"))] = {
                    "tool_call_id": call.get("id"),
                    "tool": function.get("name"),
                    "arguments": function.get("arguments"),
                    "turn": turn,
                }
        content = message.get("content")
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") in ("image", "image_url"):
                made_by = calls.get(str(message.get("tool_call_id"))) if role == "tool" else None
                source = dict(made_by) if made_by is not None else None
                if source is not None and part.get("attachment_id"):
                    source["sha256"] = part.get("attachment_id")
                sources.append(source)
    return sources


# -- the check before a run ----------------------------------------------------------


def png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    """A PNG of one colour, *width* by *height*."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = bytes([0]) + bytes(colour) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        bytes([137, 80, 78, 71, 13, 10, 26, 10])
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height, 9))
        + chunk(b"IEND", b"")
    )


# The sizes whose placeholder counts a check asks for: small ones the server
# scales up, sides it has to round, a common screen, and one above any limit.
_SIZES = ((64, 64), (37, 53), (640, 480), (801, 601), (1280, 800), (390, 844), (5000, 4000))
_RED, _BLUE = (220, 30, 30), (30, 60, 220)
_QUESTION = "What colour fills this image? Answer with one word."
# Positions at which the two routes' distributions are compared.
_COMPARED = 8
# How far two values for one token may be apart between the routes: more than
# a server's own values move between two requests, far less than a setting
# that one route leaves out changes them.
_AGREE_WITHIN = 0.05
_IMPOSSIBLE_BELOW = -100.0
_CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def _status_words(error: BaseException) -> str:
    status = f"status {error.status_code}, " if isinstance(error, APIStatusError) else ""
    return f"{status}{type(error).__name__}: {str(error)[:200]}"


def check_vision_route(
    client: Any,
    model: str,
    *,
    extra_body: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Whether the server behind *client* (an SDK client) can be sent images
    beside prompts of token IDs, and what a run needs to know to do it.

    Asks for exactly what the mode uses.  ``ok`` is true when every check
    passed; ``reason`` names the first that did not.  The verdict is JSON-safe
    and can be passed to further runs with the same model and template
    arguments (:func:`vision_verdict`).  A server passes when

    - its generate route answers a prompt of token IDs with text, token IDs
      and log-probabilities named by token ID, one to one;
    - its tokenize route renders an image as a run of one placeholder token;
    - given the rendering with each run cut to one token and the images, the
      generate route reports that very rendering as its prompt and the runs as
      the images' places: one image, two images, an image after a tool result;
    - two different images under the same token IDs get different replies, so
      the images reach the model;
    - the generate route reports log-probabilities of the sampling
      distribution (:func:`~turnstone.rollout.probe.sampler_logprobs_reported`
      on that route), and under the run's sampler settings lists the same
      distribution as the completions route at the same place.

    ``sizes`` lists how many placeholder tokens the server gives images of a
    few sizes, and ``resize_rule`` the rule those counts follow, where they
    follow the one known here.
    """
    from turnstone.rollout.probe import sampler_logprobs_reported

    request: dict[str, Any] = {"extra_body": dict(extra_body or {})}
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
    arguments = template_arguments(request)
    settings = {k: v for k, v in (extra_body or {}).items() if k != "chat_template_kwargs"}
    if temperature is not None:
        settings["temperature"] = temperature
    checks: dict[str, Any] = {}
    # Part of the check for prompts as token IDs, which says what it was made for.
    verdict: dict[str, Any] = {"ok": False, "reason": None, "checks": checks}

    def failed(reason: str) -> dict[str, Any]:
        verdict["reason"] = reason
        return verdict

    def rendered(conversation: list[dict[str, Any]], tools: bool = False) -> list[int]:
        return render(client, model, conversation, [_CHECK_TOOL] if tools else None, arguments)

    def shows(image: Image, text: str = _QUESTION) -> list[dict[str, Any]]:
        return [{"role": "user", "content": [image.part(), {"type": "text", "text": text}]}]

    # 1. The generate route answers a prompt of token IDs the way a run reads it.
    try:
        plain = rendered([{"role": "user", "content": "Reply with the word yes."}])
    except Exception as error:
        return failed(f"the tokenize route does not render messages ({_status_words(error)})")
    try:
        # What every later request carries: a parameter the route has no
        # place for is found here.
        generate_body({"extra_body": settings}, plain, ())
        answer, _text, _entries = ask_generate(client, model, plain, max_tokens=4, **settings)
    except PromptAssemblyError as error:
        return failed(str(error))
    except Exception as error:
        hint = ""
        if isinstance(error, APIStatusError) and error.status_code == 403:
            hint = ": the route is switched off; start the server with --enable-scale-out"
        return failed(
            f"the generate route did not answer a prompt of token IDs ({_status_words(error)})"
            + hint
        )
    problems = answer.problems()
    checks["generate_route"] = {
        "token_ids": answer.token_ids,
        "finish_reason": answer.finish_reason,
        "problems": problems,
    }
    if problems:
        hint = ""
        if answer.aligned and answer.token_ids and not answer.ids_checked:
            hint = "; start the server with --return-tokens-as-token-ids"
        return failed(
            "the generate route's reply to a prompt of token IDs cannot be trained on: "
            + "; ".join(problems)
            + hint
        )

    # 2. An image is rendered as a run of one placeholder token.
    small = Image.of(png(64, 64, _RED), "image/png")
    large = Image.of(png(320, 256, _RED), "image/png")
    try:
        first, second = rendered(shows(small)), rendered(shows(large))
    except Exception as error:
        return failed(
            f"the tokenize route does not render a message with an image ({_status_words(error)})"
        )
    head = common_prefix_len(first, second)
    tail = common_prefix_len(first[head:][::-1], second[head:][::-1])
    runs = (first[head : len(first) - tail], second[head : len(second) - tail])
    longer = max(runs, key=len)
    if head == 0 or not longer or len(set(longer)) != 1 or first[head - 1] != longer[0]:
        return failed(
            "the tokenize route does not render an image as a run of one placeholder token "
            "whose length depends on the image's size"
        )
    placeholder = longer[0]
    verdict["placeholder"] = placeholder
    # What the template writes around the run: the token before it and the one after.
    opened = first.index(placeholder)
    closed = opened + first.count(placeholder)
    if (
        opened == 0
        or closed >= len(first)
        or first[opened:closed] != [placeholder] * (closed - opened)
    ):
        return failed(
            "the tokenize route's rendering of an image has nothing around its placeholders"
        )
    frame = (first[opened - 1], first[closed])
    verdict["frame"] = list(frame)

    checks["image_is_a_run_of_one_token"] = True

    # 3. How many tokens images of a few sizes take, and the rule behind it.
    sizes: list[dict[str, int]] = []
    try:
        for width, height in _SIZES:
            ids = rendered(shows(Image.of(png(width, height, _BLUE), "image/png")))
            sizes.append({"width": width, "height": height, "tokens": ids.count(placeholder)})
    except Exception as error:
        return failed(
            f"the tokenize route does not render every image size ({_status_words(error)})"
        )
    verdict["sizes"] = sizes
    rule = ResizeRule()
    follows = all(rule.tokens(s["width"], s["height"]) == s["tokens"] for s in sizes)
    verdict["resize_rule"] = rule.describe() if follows else None

    # 4. The generate route expands the placeholders to the very rendering.
    red = Image.of(png(96, 96, _RED), "image/png")
    blue = Image.of(png(96, 96, _BLUE), "image/png")
    after_a_tool: list[dict[str, Any]] = [
        {"role": "user", "content": "Look at shot.png."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_check",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "shot.png"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_check", "content": "Image file: shot.png"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "Images returned by the tool:"}, large.part()],
        },
    ]
    # The same with a turn that reasoned first: what a run's second request looks like.
    reasoned = [dict(m) for m in after_a_tool]
    reasoned[1] = {**reasoned[1], "reasoning": "The user wants shot.png looked at."}
    two: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [red.part(), large.part(), {"type": "text", "text": "Compare them."}],
        }
    ]
    layouts: dict[str, tuple[list[dict[str, Any]], list[Image], bool]] = {
        "one_image": (shows(red), [red], False),
        "two_images": (two, [red, large], False),
        "an_image_after_a_tool_result": (after_a_tool, [large], True),
        "an_image_after_a_turn_with_reasoning": (reasoned, [large], True),
    }
    expands: dict[str, bool] = {}
    for name, (conversation, images, tools) in layouts.items():
        try:
            whole = rendered(conversation, tools)
            places = find_places(whole, len(images), placeholder, frame)
            answer, _text, _entries = ask_generate(
                client,
                model,
                collapsed(whole, places),
                images,
                expected=whole,
                places=places,
                max_tokens=1,
                **settings,
            )
        except Exception as error:
            return failed(
                f"the generate route did not take a prompt with images ({name.replace('_', ' ')}: "
                f"{_status_words(error)})"
            )
        expands[name] = not answer.prompt_differs and not answer.placeholders_differ
        if answer.problems():
            checks["generate_expands_like_tokenize"] = expands
            return failed(
                f"the generate route's reply to a prompt with images ({name.replace('_', ' ')}) "
                "cannot be trained on: " + "; ".join(answer.problems())
            )
    checks["generate_expands_like_tokenize"] = expands

    # A run cuts the template's tail out of the rendering of the whole
    # conversation: the rendering up to the turn must begin it, images or not.
    try:
        through = render(
            client, model, reasoned[:2], [_CHECK_TOOL], arguments, generation_prompt=False
        )
        extends = rendered(reasoned, True)[: len(through)] == through
    except Exception as error:
        return failed(
            f"the tokenize route does not render a turn before an image ({_status_words(error)})"
        )
    checks["turn_is_a_prefix_of_the_rendering_with_an_image"] = extends
    if not extends:
        return failed(
            "the server renders the conversation up to an assistant turn differently once a "
            "message with an image follows it"
        )

    # 5. The images reach the model: two of them, one prompt, two replies.
    whole = rendered(shows(red))
    places = find_places(whole, 1, placeholder, frame)
    replies: list[str] = []
    seen: list[list[int]] = []
    try:
        for image in (red, blue):
            greedy = {**settings, "temperature": 0.0, "max_tokens": 256}
            answer, text, _entries = ask_generate(
                client,
                model,
                collapsed(whole, places),
                [image],
                expected=whole,
                places=places,
                **greedy,
            )
            replies.append(text[-80:])
            seen.append(answer.token_ids)
    except Exception as error:
        return failed(f"the generate route did not answer about an image ({_status_words(error)})")
    checks["replies_to_two_images"] = replies
    checks["images_reach_the_model"] = seen[0] != seen[1]
    if seen[0] == seen[1]:
        return failed(
            "two different images under the same token IDs got the same reply: the images do "
            "not reach the model"
        )

    # 6. Which log-probabilities the route reports, measured as on any route.
    try:
        probed = sampler_logprobs_reported(
            client, model, extra_body=dict(extra_body or {}), route="generate"
        )
    except Exception as error:
        return failed(f"the probe of the generate route failed ({_status_words(error)})")
    verdict["sampler_logprobs"] = probed
    if probed.get("sampler_logprobs") is not True:
        return failed(
            "the generate route does not report log-probabilities of the sampling "
            f"distribution: {probed.get('reason')}"
        )

    # 7. Under the run's own settings the two routes list one distribution.
    try:
        agree = _routes_agree(client, model, plain, settings)
    except Exception as error:
        return failed(f"the two routes could not be compared at one place ({_status_words(error)})")
    checks["routes_agree"] = agree
    if agree["differing_positions"]:
        return failed(
            "the generate route and the completions route list different distributions at the "
            f"same place under the run's sampler settings ({agree['differing_positions']} of "
            f"{agree['positions']} positions): a setting reaches one route and not the other"
        )
    verdict["ok"] = True
    return verdict


def _routes_agree(
    client: Any, model: str, prompt: list[int], settings: Mapping[str, Any]
) -> dict[str, Any]:
    """The distributions the two routes list at the first positions of a
    reply to *prompt* under *settings*, each position reached by the
    completions route's likeliest tokens."""
    alternatives = 20
    positions = differing = unsure = 0
    widest = 0.0
    sent = list(prompt)
    keywords = {k: settings[k] for k in ("temperature", "top_p") if k in settings}
    body = {k: v for k, v in settings.items() if k not in keywords}
    for _ in range(_COMPARED):
        answer = client.completions.create(
            model=model,
            prompt=sent,
            max_tokens=1,
            logprobs=alternatives,
            extra_body={**body, "return_tokens_as_token_ids": True},
            **keywords,
        )
        listed = dict(answer.choices[0].logprobs.top_logprobs[0])
        there = {name: value for name, value in listed.items() if value > _IMPOSSIBLE_BELOW}
        _call, _text, entries = ask_generate(
            client, model, sent, alternatives=alternatives, max_tokens=1, **settings
        )
        here = {
            str(one["token"]): float(one["logprob"])
            for one in (entries[0].get("top_logprobs") if entries else None) or []
            if float(one["logprob"]) > _IMPOSSIBLE_BELOW
        }
        if not there or not here:
            break
        positions += 1
        unsure += len(there) > 1
        apart = max((abs(there[name] - here[name]) for name in there if name in here), default=0.0)
        widest = max(widest, apart)
        if set(there) != set(here) or apart > _AGREE_WITHIN:
            differing += 1
        likeliest = max(there, key=there.__getitem__)
        if not likeliest.startswith(_ID_PREFIX):
            break
        sent = [*sent, int(likeliest[len(_ID_PREFIX) :])]
    return {
        "positions": positions,
        # Positions with more than one possible token: the ones that show a
        # cut-off or a temperature one route leaves out.
        "unsure_positions": unsure,
        "differing_positions": differing,
        "widest_difference": round(widest, 6),
    }
