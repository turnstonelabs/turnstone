"""Images in a rollout whose prompts go out as token IDs.

On by default with prompts as token IDs, and a run that cannot have it is
refused unless it says it wants none (``RolloutConfig.vision``).  With it on,
the session's model is given vision, so the framework's own paths bring an
image into the conversation: ``read_file`` on an image file and an image a
tool of the task's servers returns both arrive as ``image_url`` parts of the
request's messages, the way they do for any model with vision.  Nothing of
that is built here.  What this module adds is the part a prompt of token IDs
lacks: the pixels.

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

A reply that fails either, or that never says which prompt it answered, is
the harness's alarm: nothing a conversation holds brings it about, so the run
ends as ``harness_fault`` and nothing more is sent.  Everything else is as on
the completions route.  The prompt is still the previous prompt, the tokens
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
    _usage_dict,
    note_reported_prompt,
    pair_tokens,
    unreadable,
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
    there, as the model's own outcome (``image_token_generated``).

    Raised only for a placeholder token that stands in a stretch of the
    sequence the model generated.  One that stands anywhere else was put
    there by the template or by this code, and is the harness's fault."""


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
    prompt: list[int],
    images: int,
    placeholder: int,
    frame: tuple[int, int],
    generated: Sequence[tuple[int, int]] = (),
) -> list[tuple[int, int]]:
    """Where the *images* images of a conversation stand in *prompt*, its
    rendering: the runs of placeholder tokens the template wrote, each
    between the two tokens of *frame*, in order.

    A server gives the first image the first placeholder token it finds, the
    next image the first after that image's run, and so on.  So the prompt
    can be sent with each run cut to one token only when the template's
    places are the first placeholder tokens there are.

    *generated* are the stretches of *prompt* the model generated itself
    (where each begins and ends).  A placeholder token there is the model's,
    whatever stands around it, and is no place of an image.  Everything else
    was written by the template from what the environment wrote, which is
    read as plain text and holds no such token.  So:

    - :class:`PromptAssemblyError` where what lies outside *generated* does
      not hold exactly the images' places: another number of places than the
      conversation has images, or a placeholder token that is no image's
      before one.  The model did not do that;
    - :class:`PlaceholderGeneratedError` where it does, and a placeholder
      token the model generated stands before an image.
    """
    places: list[tuple[int, int]] = []
    loose: list[int] = []
    models: list[int] = []
    at = 0
    size = len(prompt)
    while at < size:
        if prompt[at] != placeholder:
            at += 1
            continue
        end = at
        while end < size and prompt[end] == placeholder:
            end += 1
        if any(begins <= at < ends for begins, ends in generated):
            models.append(at)
        elif at and prompt[at - 1] == frame[0] and end < size and prompt[end] == frame[1]:
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
            f"a placeholder token that is no image's and that the model did not generate stands "
            f"at position {loose[0]}, before an image: the server would give the image to it"
        )
    if places and models and models[0] < places[-1][0]:
        raise PlaceholderGeneratedError(
            "the model generated the token an image is rendered as (position "
            f"{models[0]} of the sequence), and the server would give a later image to it"
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
    (``record.placeholders_sent``).  Places that cannot be read count as
    other places: the reply does not show that the images stood where they
    belong.
    """
    try:
        if not isinstance(part, dict):
            raise TypeError("a part of the reply is no object")
        taken: tuple[str, list[int]] | None = None
        if (
            note_reported_prompt(record, part.get("prompt_token_ids"))
            and record.placeholders_sent is not None
        ):
            # Not where they belong until they are read and found there:
            # places that cannot be read are not the places that were sent.
            record.placeholders_differ = True
            places = _image_places(part.get("mm_placeholders"))
            record.placeholders_differ = places != record.placeholders_sent
        for choice in part.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            ids = [int(i) for i in (choice.get("token_ids") or [])]
            # Among the call's tokens before anything else of the part is read.
            record.token_ids.extend(ids)
            text = choice.get("text") or ""
            record.raw.extend(text.encode("utf-8", errors="replace"))
            entries = list((choice.get("logprobs") or {}).get("content") or [])
            pair_tokens(
                record,
                ids,
                [entry.get("logprob") for entry in entries],
                [str(entry.get("token")) for entry in entries],
            )
            reason = choice.get("finish_reason")
            if reason:
                record.finish_reason = reason
            taken = (text, ids)
        usage = _usage_dict(part.get("usage"))
        if usage:
            record.usage = usage
        return taken
    except Exception as error:
        unreadable(record, error)
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


# The image formats a server opens: vLLM 0.32, ``vllm/multimodal/image.py``,
# ``ALLOWED_IMAGE_FORMATS``, copied.
_SERVER_FORMATS = (
    "AVIF",
    "BLP",
    "BMP",
    "CUR",
    "DCX",
    "DDS",
    "DIB",
    "FITS",
    "FLI",
    "FTEX",
    "GBR",
    "GIF",
    "ICNS",
    "ICO",
    "IM",
    "IMT",
    "IPTC",
    "JPEG",
    "JPEG2000",
    "MCIDAS",
    "MSP",
    "PCD",
    "PCX",
    "PIXAR",
    "PNG",
    "PPM",
    "PSD",
    "QOI",
    "SGI",
    "SPIDER",
    "SUN",
    "TGA",
    "TIFF",
    "WEBP",
    "XBM",
    "XPM",
    "XVTHUMB",
)


# The most pixels a server loads an image of: vLLM 0.32, ``VLLM_MAX_IMAGE_PIXELS``,
# its default.  The same number as twice Pillow's own ``MAX_IMAGE_PIXELS``,
# above which Pillow refuses to open an image at all.
SERVER_MAX_IMAGE_PIXELS = 178_956_970


def server_image_size(data: bytes) -> tuple[int, int]:
    """Width and height of the image *data* holds as a server works from it:
    turned the way the server turns it, by the server's own steps in the
    server's order (vLLM 0.32, ``ImageMediaIO.load_bytes``): open, turn
    upright with Pillow's ``ImageOps.exif_transpose`` and go on with the
    image as it is where that raises, decode.

    The one place in the package that calls Pillow's own reading of an
    orientation, and it must.  The framework's reader
    (:func:`turnstone.core.images.exif_orientation`) is deliberately not
    Pillow's: it turns a picture whose EXIF holds a tag in a type Pillow
    fails on, and it reads the EXIF block only.  A server does what Pillow
    does: it leaves such a picture as it is, and it turns one whose
    orientation stands outside the EXIF block (in XMP, in a PNG's text).
    The number of placeholder tokens is the same either way round, so
    nothing would show that the two disagree, and the record would name
    the image's grid on its side.  The size is therefore taken with the
    server's call, and no other.

    No limit of the framework's is applied either (pixels, JPEG scans): the
    server applied none to an image it rendered, and an image it rendered
    is sized here.  The server's own limit on an image's pixels is applied
    where the server applies it, straight after the open
    (:data:`SERVER_MAX_IMAGE_PIXELS`), whatever Pillow's own limit is set to
    in this process.  Raises ``ValueError`` above that limit, and whatever
    Pillow raises for bytes it cannot open or decode.
    """
    from PIL import Image as _Pil
    from PIL import ImageOps

    with _Pil.open(io.BytesIO(data), formats=_SERVER_FORMATS) as opened:
        wide, high = opened.size
        if wide * high > SERVER_MAX_IMAGE_PIXELS:
            raise ValueError(
                f"Image dimensions {wide}x{high} ({wide * high} pixels) exceed the maximum of "
                f"{SERVER_MAX_IMAGE_PIXELS} pixels"
            )
        image: _Pil.Image = opened
        with contextlib.suppress(Exception):
            image = ImageOps.exif_transpose(opened)
        image.load()
        width, height = image.size
    return int(width), int(height)


def smart_resize(
    height: int, width: int, factor: int, min_pixels: int, max_pixels: int
) -> tuple[int, int]:
    """The height and width the model's image processor resizes an image of
    *height* by *width* to: both divisible by *factor*, the area within
    *min_pixels* and *max_pixels*, the proportions kept as nearly as that
    allows.

    An exact copy of the processor's own function (transformers 5.19,
    ``models/qwen2_vl/image_processing_qwen2_vl.py``, ``smart_resize``),
    statement for statement, the refusal of an image more than 200 times
    longer than wide included.  Not a description of it: a side that rounds
    to nothing is left at nothing here as it is there, which sends a thin
    image through the branch for small ones.
    """
    if max(height, width) / min(height, width) > 200:
        raise ValueError(
            "absolute aspect ratio must be smaller than 200, got "
            f"{max(height, width) / min(height, width)}"
        )
    h_bar = round(height / factor) * factor
    w_bar = round(width / factor) * factor
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


@dataclass(frozen=True)
class ResizeRule:
    """How a server sizes an image before it cuts it into patches: the
    function of the model's image processor (:func:`smart_resize`) with the
    processor's numbers.  Each side becomes a multiple of *multiple* pixels,
    the area is kept within *min_pixels* and *max_pixels*, a patch is a
    square of *patch* pixels and one token stands for a square of *multiple*.
    A check holds the server at hand to it on a set of sizes, and a run
    holds every image to it."""

    multiple: int = 32
    min_pixels: int = 65536
    max_pixels: int = 16777216
    patch: int = 16

    def size(self, width: int, height: int) -> tuple[int, int]:
        """The width and height an image of *width* by *height* is resized to."""
        high, wide = smart_resize(height, width, self.multiple, self.min_pixels, self.max_pixels)
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
    # The rule the server's image sizes follow: a check passes only on a
    # server that follows it.
    resize: ResizeRule = field(default_factory=ResizeRule)

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
        try:
            sized = rule if isinstance(rule, dict) else {}
            resize = ResizeRule(**{name: int(sized[name]) for name in ResizeRule().describe()})
        except (KeyError, TypeError, ValueError):
            raise SetupError(
                "the given check of the server for images does not hold what a passed check "
                "found (resize_rule must name multiple, min_pixels, max_pixels and patch: the "
                "rule the server sizes an image by): check again"
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
    # Each image by the text that carried it: decoded and hashed once.
    _known: dict[str, Image] = field(default_factory=dict)
    # What the server makes of each image, by the hash of its bytes: sized
    # once, by the server's own steps.
    _sizes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def images_of(self, messages: Sequence[Mapping[str, Any]]) -> list[Image]:
        """The images a request's messages carry, in order."""
        return images_of(messages, self._known)

    def _size(self, data: bytes) -> dict[str, Any]:
        """What the server makes of the image *data* holds, worked out here
        with the server's own code.  Raises what that code raises for an
        image it does not take."""
        rule = self.route.resize
        width, height = server_image_size(data)
        wide, high = rule.size(width, height)
        return {
            "width": width,
            "height": height,
            "tokens": (wide // rule.multiple) * (high // rule.multiple),
            "processed": {
                "width": wide,
                "height": high,
                # Frames, rows and columns of patches, before they are merged.
                "grid": [1, high // rule.patch, wide // rule.patch],
            },
        }

    def unshowable(self, data: bytes) -> str | None:
        """Why the image *data* holds cannot be shown to the model, ``None``
        where it can: asked where an image is about to enter a conversation.

        The image is sized here as it will be when it is sent, with the
        server's own code and so with the server's own limits: bytes Pillow
        cannot open or decode (a file cut short), more pixels than the
        server loads (:data:`SERVER_MAX_IMAGE_PIXELS`, which is also where
        Pillow, left at its default, refuses to open an image at all), an
        image more than 200 times longer than wide, which the image
        processor refuses.  A server refuses such an image, and not always as a
        request it will not take: one answers the largest of them as a
        failure of its own, which would end the run as a failure of the
        machinery over a picture the model made.  So it is kept out of the
        conversation, and the model is told why instead.  An image that is
        let in is sized already when it is sent."""
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 in self._sizes:
            return None
        try:
            self._sizes[sha256] = self._size(data)
        except Exception as error:
            return f"{type(error).__name__}: {str(error)[:200]}"
        return None

    def _sized(self, assembly: PromptAssembly, image: Image, length: int) -> None:
        """Size *image* here, by the server's own steps, and hold the result
        against the *length* placeholder tokens the server rendered it as.

        The sizing here is the server's own code (:func:`server_image_size`,
        :func:`smart_resize`) with the numbers a check held the server to.
        So another number of tokens than the server's, or an image the
        server rendered that cannot be sized here, is no property of the
        image: the code here is not the server's (another version of a
        library, another processor).  The record could not say what the
        server made of the image, and nothing says the next one would fare
        better, so it is the harness's alarm."""
        sized = self._sizes.get(image.sha256)
        if sized is None:
            try:
                sized = self._size(image.data)
            except Exception as error:
                assembly.fault = (
                    f"an image the server rendered as {length} placeholder tokens could not be "
                    f"sized here ({type(error).__name__}: {str(error)[:200]}): the image code "
                    "here is not the server's"
                )
                raise HarnessFaultError(assembly.fault) from None
            self._sizes[image.sha256] = sized
        if sized["tokens"] != length:
            assembly.fault = (
                f"an image of {sized['width']}x{sized['height']} stands as {length} placeholder "
                f"tokens in the server's rendering, and the sizing here gives {sized['tokens']}: "
                "the image code here is not the server's, or the server's image processor is "
                "not the one the check before the run found"
            )
            raise HarnessFaultError(assembly.fault)

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
            # A placeholder token in a turn the model generated is the model's
            # own doing (``PlaceholderGeneratedError``, which goes on up).  A
            # sub-agent's prompt is a rendering throughout, so nothing in it
            # is taken for generated.
            places = find_places(
                prompt,
                len(images),
                self.route.placeholder,
                self.route.frame,
                assembly.generated if call.scope == PARENT else (),
            )
        except PromptAssemblyError as misplaced:
            # The environment's text holds no placeholder token: it is read as
            # plain text.  What stands outside the model's own turns was
            # written by the template and by this code.
            assembly.fault = f"an image has no place in a prompt the harness built: {misplaced}"
            raise HarnessFaultError(assembly.fault) from None
        for image, (_start, length) in zip(images, places, strict=True):
            self._sized(assembly, image, length)
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
            # Sized when it was first sent, and held against its run then.
            sized = self._sizes.get(image.sha256) or {}
            if sized.get("tokens") != placed.length:
                problems.append(
                    f"image {index}: what the server made of it is not known here, so the "
                    "record cannot say it"
                )
                sized = {}
            entries.append(
                {
                    "index": index,
                    "sha256": image.sha256,
                    "file": image.file,
                    "media_type": image.media_type,
                    "bytes": len(image.data),
                    "width": sized.get("width"),
                    "height": sized.get("height"),
                    # Where its placeholder tokens stand in the final sequence.
                    "start": placed.start,
                    "end": end,
                    "tokens": placed.length,
                    "processed": sized.get("processed"),
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
# scales up, sides it has to round, a common screen, one above any limit, and
# a strip so thin that one side rounds to nothing.
_SIZES = (
    (64, 64),
    (37, 53),
    (640, 480),
    (801, 601),
    (1280, 800),
    (390, 844),
    (5000, 4000),
    (2400, 12),
)
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


def _pillow_version() -> str:
    import PIL

    return str(PIL.__version__)


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
    arguments, inside the verdict of
    :func:`~turnstone.rollout.idprompt.check_token_id_route`.  A server passes when

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
    few sizes, and ``resize_rule`` the numbers of the model's image
    processor those counts follow.  A server whose counts are not what the
    sizing on this side gives (:func:`server_image_size`,
    :func:`smart_resize`) fails the check: the record could not say what
    such a server makes of an image.  A run then holds every image to the
    same sizing, and a difference there is the harness's alarm.
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
    # The sizing a run does for every image, done here for each of them.
    rule = ResizeRule()
    samples = [Image.of(png(width, height, _BLUE), "image/png") for width, height in _SIZES]
    sizes: list[dict[str, int]] = []
    try:
        for (width, height), sample in zip(_SIZES, samples, strict=True):
            ids = rendered(shows(sample))
            sizes.append({"width": width, "height": height, "tokens": ids.count(placeholder)})
    except Exception as error:
        return failed(
            f"the tokenize route does not render every image size ({_status_words(error)})"
        )
    verdict["sizes"] = sizes
    try:
        here = [rule.tokens(*server_image_size(sample.data)) for sample in samples]
    except Exception as error:
        return failed(
            "the images of the check could not be sized on this side "
            f"({type(error).__name__}: {str(error)[:200]})"
        )
    for size, by_rule in zip(sizes, here, strict=True):
        if by_rule != size["tokens"]:
            return failed(
                "the server does not size images as the model's image processor does with the "
                f"numbers known here (sides made multiples of {rule.multiple}, the area kept "
                f"within {rule.min_pixels} to {rule.max_pixels} pixels): "
                f"{size['width']}x{size['height']} takes {size['tokens']} placeholder tokens, "
                f"the sizing here gives {by_rule}"
            )
    verdict["resize_rule"] = rule.describe()
    # The library the images are opened and turned with on this side: a
    # server turns an image with its own copy of it.
    verdict["sized_with"] = {"pillow": _pillow_version()}

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
    if agree["positions"] < _COMPARED:
        return failed(
            f"the two routes were compared at {agree['positions']} positions and the check "
            f"takes {_COMPARED}: {agree['stopped']}"
        )
    one_way = settings.get("temperature") == 0 or settings.get("top_k") == 1
    if not agree["unsure_positions"] and not one_way:
        return failed(
            "at none of the positions compared did either route list more than one possible "
            "token, so the comparison shows nothing about the run's sampler settings"
        )
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
    stopped: str | None = None
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
            stopped = (
                f"the {'completions' if not there else 'generate'} route listed no possible "
                f"token at position {positions}"
            )
            break
        likeliest = max(there, key=there.__getitem__)
        if not likeliest.startswith(_ID_PREFIX):
            stopped = "the completions route does not name the tokens it lists by ID"
            break
        positions += 1
        unsure += len(there) > 1 or len(here) > 1
        apart = max((abs(there[name] - here[name]) for name in there if name in here), default=0.0)
        widest = max(widest, apart)
        if set(there) != set(here) or apart > _AGREE_WITHIN:
            differing += 1
        sent = [*sent, int(likeliest[len(_ID_PREFIX) :])]
    return {
        "positions": positions,
        # Positions at which a route listed more than one possible token: the
        # ones that show a cut-off or a temperature one route leaves out.
        "unsure_positions": unsure,
        "differing_positions": differing,
        "widest_difference": round(widest, 6),
        # Why fewer positions were compared than the check takes.
        "stopped": stopped,
    }
