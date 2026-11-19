"""Images in a rollout whose prompts go out as token IDs: the images of a
request, their places, the request to the generate route, the check of the
server, the assembly of prompts with images, and whole rollouts.  Scripted
server, no container; the sandbox stand-ins are those of
``test_rollout_episode.py`` and every script holds harmless commands only."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any

import pytest

from tests._scripted_chat_server import (
    ADDED_TOKENS,
    IMAGE,
    IMAGE_END,
    PAD,
    PAD_TEXT,
    SPARE,
    ScriptedChatServer,
    encode,
    image_patches,
)
from tests.test_rollout_episode import (  # noqa: F401
    SPAWN,
    _DirSandbox,
    _no_configuration_file,
    _run,
)
from tests.test_rollout_idprompt import MESSAGES, ROUTE, STEP, ask, make_client, turn_of
from turnstone.rollout import cli
from turnstone.rollout.budget import Budgets, Meter
from turnstone.rollout.episode import Task, token_id_route_verdict
from turnstone.rollout.idprompt import PromptAssembly, PromptAssemblyError, check_token_id_route
from turnstone.rollout.plaintext import HarnessFaultError, PlainText
from turnstone.rollout.probe import sampler_logprobs_reported
from turnstone.rollout.recording import CallRecord, RecordingClient, _size
from turnstone.rollout.settings import SetupError
from turnstone.rollout.tokens import build_token_record, rebuild_final
from turnstone.rollout.vision import (
    Image,
    PlaceholderGeneratedError,
    ResizeRule,
    Vision,
    VisionRoute,
    check_vision_route,
    collapsed,
    find_places,
    generate_body,
    image_sources,
    images_of,
    png,
    server_image_size,
    smart_resize,
    take_generate,
    write_image,
)

NL = chr(10)
RED = Image.of(png(24, 16, (220, 30, 30)), "image/png")
BLUE = Image.of(png(16, 16, (30, 60, 220)), "image/png")
# The placeholder tokens the toy server gives each, by the rule it sizes images by.
RED_TOKENS = image_patches(RED.data)
BLUE_TOKENS = image_patches(BLUE.data)
assert (RED_TOKENS, BLUE_TOKENS) == (70, 64)
FRAME = (IMAGE, IMAGE_END)
SEEING = VisionRoute(placeholder=PAD, frame=FRAME)
PLAIN = PlainText(stand_in="<spare>", stand_in_id=SPARE, texts=tuple(ADDED_TOKENS))
# A server whose tokenizer knows its own tokens' texts and names them when asked.
SERVED: dict[str, Any] = {"own_tokens": True, "tokenizer_info": True}
# A run that reads text as plain text, asking that server for its tokens.
SEES: dict[str, Any] = {
    "token_id_prompts": True,
    "plain_text": True,
    "tokenizer_info_from_server": True,
    "vision": None,
}


def shows(image: Image, text: str = "look") -> dict[str, Any]:
    return {"role": "user", "content": [image.part(), {"type": "text", "text": text}]}


def seeing_recorder(
    server: ScriptedChatServer, plain: PlainText | None = None
) -> tuple[RecordingClient, Vision]:
    vision = Vision(SEEING)
    assembly = PromptAssembly(ROUTE, vision=vision, plain=plain)
    return RecordingClient(make_client(server), id_prompts=assembly), vision


class TestTheImagesOfARequest:
    def test_they_are_found_in_the_order_of_the_messages_and_their_parts(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [RED.part(), {"type": "text", "text": "a"}, BLUE.part()]},
            {"role": "tool", "tool_call_id": "c", "content": "text only"},
            shows(RED),
        ]
        found = images_of(messages)
        assert [i.sha256 for i in found] == [RED.sha256, BLUE.sha256, RED.sha256]
        assert found[0].data == RED.data and found[0].media_type == "image/png"
        assert RED.sha256 == hashlib.sha256(RED.data).hexdigest()
        assert RED.file == RED.sha256 + ".png"

    def test_an_image_is_decoded_once_however_often_it_is_met(self) -> None:
        known: dict[str, Image] = {}
        first = images_of([shows(RED), shows(RED)], known)
        again = images_of([shows(RED)], known)
        assert len(known) == 1 and first[0] is first[1] is again[0]

    def test_a_conversation_without_images_has_none(self) -> None:
        assert images_of(MESSAGES) == []

    @pytest.mark.parametrize(
        "url", ["http://example.invalid/a.png", "data:image/png;base64,@@@", ""]
    )
    def test_an_image_whose_bytes_are_not_in_the_request_is_refused(self, url: str) -> None:
        message = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}
        with pytest.raises(PromptAssemblyError):
            images_of([message])

    def test_the_png_made_for_the_check_is_an_image(self) -> None:
        assert server_image_size(png(40, 24, (1, 2, 3))) == (40, 24)
        with pytest.raises(OSError):
            server_image_size(b"no image")

    def test_an_images_data_does_not_count_as_prompt_text(self) -> None:
        """Its size in bytes says nothing about the tokens it becomes."""
        big = Image.of(png(640, 480, (1, 2, 3)), "image/png")
        assert _size([shows(big, "look")]) == _size(
            [{"role": "user", "content": [{"type": "image_url"}, {"type": "text", "text": "look"}]}]
        )
        assert _size([shows(big, "look")]) < 200 < len(big.url)


class TestThePlacesOfTheImages:
    def test_the_places_are_the_runs_the_template_wrote(self) -> None:
        prompt = [1, IMAGE, PAD, PAD, PAD, IMAGE_END, 7, IMAGE, PAD, PAD, IMAGE_END, 8]
        places = find_places(prompt, 2, PAD, FRAME)
        assert places == [(2, 3), (8, 2)]
        assert collapsed(prompt, places) == [1, IMAGE, PAD, IMAGE_END, 7, IMAGE, PAD, IMAGE_END, 8]
        assert collapsed(prompt, []) == prompt

    def test_a_placeholder_that_is_no_images_after_the_images_stays(self) -> None:
        prompt = [1, IMAGE, PAD, PAD, IMAGE_END, 7, PAD, 8]
        assert find_places(prompt, 1, PAD, FRAME) == [(2, 2)]

    def test_a_placeholder_that_is_no_images_before_an_image_is_refused(self) -> None:
        """The server would give the image to the first placeholder it finds."""
        prompt = [1, PAD, 7, IMAGE, PAD, PAD, PAD, IMAGE_END]
        with pytest.raises(PromptAssemblyError, match="did not generate stands at position 1"):
            find_places(prompt, 1, PAD, FRAME)

    def test_another_number_of_places_than_images_is_refused(self) -> None:
        prompt = [1, IMAGE, PAD, PAD, IMAGE_END, 7, IMAGE, PAD, IMAGE_END, 8]
        with pytest.raises(PromptAssemblyError, match="2 places .* 1 images"):
            find_places(prompt, 1, PAD, FRAME)
        with pytest.raises(PromptAssemblyError, match="0 places .* 1 images"):
            find_places([1, 2, 3], 1, PAD, FRAME)

    def test_a_placeholder_the_model_generated_before_an_image_is_the_models_doing(self) -> None:
        """Only one that stands in a stretch the model generated: the same
        token anywhere else was written by the template or by the harness."""
        prompt = [1, PAD, 7, IMAGE, PAD, PAD, PAD, IMAGE_END]
        with pytest.raises(PlaceholderGeneratedError, match="position 1 of the sequence"):
            find_places(prompt, 1, PAD, FRAME, [(1, 3)])
        # Whatever the model wrote around it: a place the model spelled out
        # token by token is no place of an image.
        spelled = [1, IMAGE, PAD, IMAGE_END, 7, IMAGE, PAD, PAD, IMAGE_END]
        with pytest.raises(PlaceholderGeneratedError):
            find_places(spelled, 1, PAD, FRAME, [(1, 4)])
        # After the last image the server gives it nothing: the prompt is sent.
        late = [1, IMAGE, PAD, PAD, IMAGE_END, 7, IMAGE, PAD, IMAGE_END, PAD]
        assert find_places(late, 1, PAD, FRAME, [(6, 10)]) == [(2, 2)]

    def test_a_stray_placeholder_outside_what_the_model_generated_is_not_the_models(self) -> None:
        """The model generated one somewhere, and another stands where it
        generated nothing: that one is not explained by the model."""
        prompt = [1, PAD, 7, PAD, 8, IMAGE, PAD, PAD, IMAGE_END]
        with pytest.raises(PromptAssemblyError, match="did not generate stands at position 1"):
            find_places(prompt, 1, PAD, FRAME, [(3, 5)])
        # A place that is missing outside the model's turns is not explained either.
        with pytest.raises(PromptAssemblyError, match="0 places .* 1 images"):
            find_places([1, PAD, 7], 1, PAD, FRAME, [(1, 2)])


class TestTheRequest:
    OUTGOING = {
        "model": "m",
        "stream": True,
        "logprobs": 0,
        "max_tokens": 500,
        "temperature": 1.0,
        "top_p": 0.9,
        "extra_body": {
            "top_k": 20,
            "top_p": 0.95,
            "return_token_ids": True,
            "return_tokens_as_token_ids": True,
            "skip_special_tokens": True,
        },
    }

    def test_it_carries_the_completions_requests_settings(self) -> None:
        body = generate_body(self.OUTGOING, [1, PAD, 2], [RED])
        assert body["token_ids"] == [1, PAD, 2] and body["model"] == "m"
        assert body["content_parts"] == [{"type": "image_url", "url": RED.url}]
        # A setting in the body wins over the keyword, as on the completions route.
        assert body["sampling_params"] == {
            "max_tokens": 500,
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "skip_special_tokens": True,
            "logprobs": 0,
        }
        assert body["return_token_ids"] is True and body["output_mode"] == "text"
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}

    def test_a_parameter_the_route_has_no_place_for_is_refused(self) -> None:
        """The route drops what it does not know and answers all the same."""
        outgoing = copy.deepcopy(self.OUTGOING)
        outgoing["extra_body"]["no_such_setting"] = 1
        with pytest.raises(PromptAssemblyError, match="'no_such_setting' has no place"):
            generate_body(outgoing, [1], [])


class TestTheResizeRule:
    # What a served model's tokenize route gave images of these sizes.
    MEASURED = {
        (128, 128): 64,
        (640, 480): 300,
        (1280, 800): 1000,
        (1920, 1080): 2040,
        (3000, 2000): 5828,
        (5000, 4000): 16302,
        (37, 53): 70,
        (1, 1): 64,
        (1000, 30): 94,
        (801, 601): 475,
        (1366, 768): 1032,
        (390, 844): 312,
    }

    def test_it_gives_the_counts_a_server_gave(self) -> None:
        rule = ResizeRule()
        assert {size: rule.tokens(*size) for size in self.MEASURED} == self.MEASURED
        assert rule.size(1920, 1080) == (1920, 1088)
        assert rule.size(128, 128) == (256, 256)

    def test_it_is_the_processors_own_function_also_where_a_side_rounds_to_nothing(self) -> None:
        """A strip 12 pixels high: its height rounds to no multiple of 32 at
        all, the area counts as nothing, and the processor scales the image
        up from its true size.  A rule that put a least side in first gave
        75 tokens for it; the processor gives 114."""
        assert smart_resize(12, 2400, 32, 65536, 16777216) == (32, 3648)
        assert ResizeRule().tokens(2400, 12) == 114
        # As the processor: more than 200 times longer than wide is refused.
        with pytest.raises(ValueError, match="aspect ratio"):
            smart_resize(10, 2400, 32, 65536, 16777216)


def _turned_pictures() -> dict[str, bytes]:
    """One picture, 48 by 16, as files that say in different places that it
    is to be shown turned on its side."""
    import io
    import struct

    from PIL import Image as Pil
    from PIL import PngImagePlugin

    from tests._exif_helpers import exif_block, jpeg_with_exif, orientation_entry, png_with_exif

    picture = Pil.new("RGB", (48, 16), (10, 20, 30))

    def saved(fmt: str, **options: Any) -> bytes:
        out = io.BytesIO()
        picture.save(out, fmt, **options)
        return out.getvalue()

    exif = Pil.Exif()
    exif[0x0112] = 6
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description tiff:Orientation="6"/>'
        "</rdf:RDF></x:xmpmeta>"
    )
    text = PngImagePlugin.PngInfo()
    text.add_itxt("XML:com.adobe.xmp", xmp)
    turned = exif_block([orientation_entry(6)])
    # A second tag stored in a type Pillow's rewriting of the EXIF fails on.
    resolution_unit = (0x0128, 4, 1, struct.pack("<L", 70000))
    return {
        "plain": saved("PNG"),
        "exif in a jpeg": saved("JPEG", exif=exif),
        "exif in a png": saved("PNG", exif=exif),
        "exif in a webp": saved("WEBP", exif=exif, lossless=True),
        "exif after a png's pixels": png_with_exif(turned, picture, after_pixels=True),
        "xmp only, in a png": saved("PNG", pnginfo=text),
        "xmp only, in a jpeg": saved("JPEG", xmp=xmp.encode("ascii")),
        "oddly typed exif": jpeg_with_exif(
            exif_block([orientation_entry(6), resolution_unit]), picture
        ),
    }


class TestTheSizeAServerWorksFrom:
    """The size is taken by the server's own steps: Pillow's reading of an
    orientation, wherever Pillow finds one, and the image as it is where
    Pillow's turning fails."""

    def test_it_is_what_pillows_own_turning_gives(self) -> None:
        import io

        from PIL import Image as Pil
        from PIL import ImageOps

        pictures = _turned_pictures()
        for name, data in pictures.items():
            with Pil.open(io.BytesIO(data)) as opened:
                try:
                    expected = ImageOps.exif_transpose(opened).size
                except Exception:
                    expected = opened.size
            assert server_image_size(data) == expected, name
        assert server_image_size(pictures["plain"]) == (48, 16)
        # Turned, wherever the file says so: in its EXIF, or outside it.
        for name in ("exif in a jpeg", "exif in a png", "exif in a webp", "xmp only, in a png"):
            assert server_image_size(pictures[name]) == (16, 48), name

    def test_the_frameworks_own_reader_is_not_the_servers(self) -> None:
        """Why the size is not taken with the framework's reader: it reads
        the EXIF block only, so an image turned by its XMP is upright to it,
        and the record would name the grid a server made of it on its side
        with the very same number of tokens."""
        import io

        from PIL import Image as Pil

        from turnstone.core.images import exif_orientation

        data = _turned_pictures()["xmp only, in a png"]
        with Pil.open(io.BytesIO(data)) as opened:
            assert exif_orientation(opened) == 1
        assert server_image_size(data) == (16, 48)
        rule = ResizeRule()
        assert rule.tokens(16, 48) == rule.tokens(48, 16)
        assert rule.size(16, 48) != rule.size(48, 16)


class TestTheCheck:
    def check(self, **faults: Any) -> dict[str, Any]:
        script = [{"content": "It is one colour."}]
        with ScriptedChatServer(script, sampler_logprobs=True, **faults) as server:
            verdict = check_vision_route(
                make_client(server), "m", extra_body={"top_k": 20, "top_p": 0.95}, temperature=1.0
            )
            self.streamed = [r.get("stream") for r in server.generate_requests]
        return verdict

    def test_a_server_that_can(self) -> None:
        verdict = self.check()
        assert verdict["ok"] is True and verdict["reason"] is None
        assert verdict["placeholder"] == PAD and verdict["frame"] == [IMAGE, IMAGE_END]
        assert VisionRoute.from_verdict(verdict) == SEEING
        checks = verdict["checks"]
        assert checks["generate_route"]["problems"] == []
        assert checks["image_is_a_run_of_one_token"] is True
        assert checks["generate_expands_like_tokenize"] == {
            "one_image": True,
            "two_images": True,
            "an_image_after_a_tool_result": True,
            "an_image_after_a_turn_with_reasoning": True,
        }
        assert checks["turn_is_a_prefix_of_the_rendering_with_an_image"] is True
        assert checks["images_reach_the_model"] is True
        assert verdict["sampler_logprobs"]["sampler_logprobs"] is True
        assert verdict["sampler_logprobs"]["route"] == "generate"
        assert checks["routes_agree"]["differing_positions"] == 0
        assert checks["routes_agree"]["positions"] > 0
        # Asked the way a run asks: every request streamed.
        assert self.streamed and all(self.streamed)
        # The toy server sizes images by the rule the check knows.
        assert verdict["resize_rule"] == ResizeRule().describe()
        assert (
            checks["routes_agree"]["positions"] == 8 and checks["routes_agree"]["stopped"] is None
        )
        assert checks["routes_agree"]["unsure_positions"] > 0
        assert [s["tokens"] for s in verdict["sizes"]] == [
            image_patches(png(s["width"], s["height"], (0, 0, 0))) for s in verdict["sizes"]
        ]
        assert json.loads(json.dumps(verdict)) == verdict

    @pytest.mark.parametrize(
        ("fault", "reason"),
        [
            ({"generate_status": 403}, "start the server with --enable-scale-out"),
            ({"generate_status": 404}, "did not answer a prompt of token IDs"),
            ({"generate_fault": "plain_names"}, "--return-tokens-as-token-ids"),
            ({"generate_fault": "other_prompt"}, "another prompt than the token IDs"),
            ({"generate_fault": "other_places"}, "did not place the images"),
            ({"generate_fault": "blind"}, "the images do not reach the model"),
            ({"generate_fault": "no_usage"}, "reported no usage"),
            ({"tokenize_status": 404}, "the tokenize route does not render messages"),
            # A server that sizes images another way: the record could not say
            # what it makes of one.
            ({"own_image_sizes": True}, "does not size images as the model's image processor does"),
        ],
    )
    def test_a_server_that_cannot_is_told_why(self, fault: dict[str, Any], reason: str) -> None:
        verdict = self.check(**fault)
        assert verdict["ok"] is False and reason in verdict["reason"]
        with pytest.raises(SetupError, match="cannot be sent images"):
            VisionRoute.from_verdict(verdict)

    def test_a_server_reporting_the_models_own_values_on_that_route(self) -> None:
        with ScriptedChatServer([{"content": "x"}]) as server:
            verdict = check_vision_route(make_client(server), "m", extra_body={"top_k": 20})
        assert verdict["ok"] is False
        assert "does not report log-probabilities of the sampling distribution" in verdict["reason"]

    def test_a_request_parameter_without_a_place_fails_the_check(self) -> None:
        with ScriptedChatServer([{"content": "x"}], sampler_logprobs=True) as server:
            verdict = check_vision_route(make_client(server), "m", extra_body={"priority": 3})
            asked = list(server.generate_requests)
        assert verdict["ok"] is False and "'priority' has no place" in verdict["reason"]
        assert asked == []

    def test_the_probe_measures_the_generate_route(self) -> None:
        with ScriptedChatServer([{"content": "x"}], sampler_logprobs=True) as server:
            verdict = sampler_logprobs_reported(make_client(server), "m", route="generate")
            asked = len(server.generate_requests)
        assert verdict["sampler_logprobs"] is True and verdict["route"] == "generate"
        assert asked > 0

    def test_it_is_part_of_the_check_for_prompts_as_token_ids_when_asked_for(self) -> None:
        # A first reply that opens with a token the toy model is unsure of.
        step = {**STEP, "reasoning": "I think first."}
        with ScriptedChatServer([step], sampler_logprobs=True) as server:
            without = check_token_id_route(make_client(server), "m")
            asked = len(server.generate_requests)
            with_it = check_token_id_route(make_client(server), "m", vision=True)
        assert without["ok"] is True and "vision" not in without and asked == 0
        assert with_it["ok"] is True and with_it["vision"]["ok"] is True
        # A server that cannot be sent images still passes the check for text,
        # and the verdict says of images that it cannot.
        with ScriptedChatServer([step], sampler_logprobs=True, generate_status=403) as server:
            verdict = check_token_id_route(make_client(server), "m", vision=True)
        assert verdict["ok"] is True and verdict["vision"]["ok"] is False

    def test_an_image_check_that_breaks_off_says_so_in_its_own_words(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not "the check of the server failed": the route for text passed, and
        what could not be finished is named."""
        import turnstone.rollout.vision as vision_module

        def breaks(*_args: Any, **_settings: Any) -> dict[str, Any]:
            raise ConnectionError("the server went away")

        monkeypatch.setattr(vision_module, "check_vision_route", breaks)
        step = {**STEP, "reasoning": "I think first."}
        with ScriptedChatServer([step], sampler_logprobs=True) as server:
            verdict = check_token_id_route(make_client(server), "m", vision=True)
        assert verdict["ok"] is True and verdict["vision"]["ok"] is False
        assert "the image check could not be finished" in verdict["vision"]["reason"]
        assert "the server went away" in verdict["vision"]["reason"]

    def test_routes_that_were_compared_at_too_few_places_fail_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import turnstone.rollout.vision as vision_module

        agreed = {"differing_positions": 0, "widest_difference": 0.0}
        for found, why in (
            ({"positions": 0, "unsure_positions": 0, "stopped": "nothing was listed"}, "compared at 0 positions"),
            ({"positions": 3, "unsure_positions": 2, "stopped": "nothing was listed"}, "compared at 3 positions"),
            ({"positions": 8, "unsure_positions": 0, "stopped": None}, "more than one possible token"),
        ):  # fmt: skip
            monkeypatch.setattr(vision_module, "_routes_agree", lambda *_a, found=found: {**agreed, **found})  # fmt: skip
            verdict = self.check()
            assert verdict["ok"] is False and why in verdict["reason"]

    def test_a_verdict_must_hold_what_a_passed_check_found(self) -> None:
        verdict = self.check()
        for key, why in (
            ("placeholder", "placeholder must be the token"),
            ("frame", "frame must be the two tokens"),
            ("sampler_logprobs", "sampler_logprobs must be the probe"),
            ("resize_rule", "resize_rule must name"),
        ):
            with pytest.raises(SetupError, match=why):
                VisionRoute.from_verdict({**verdict, key: None})


class TestPromptsWithImages:
    def test_a_request_without_an_image_stays_on_the_completions_route(self) -> None:
        with ScriptedChatServer([STEP]) as server:
            recorder, vision = seeing_recorder(server)
            ask(recorder, MESSAGES)
            generate = list(server.generate_requests)
        assert generate == [] and recorder.calls[0].route == "completions"
        assert vision.images == []

    def test_a_request_with_an_image_takes_the_generate_route(self) -> None:
        messages = [MESSAGES[0], shows(RED)]
        with ScriptedChatServer([STEP]) as server:
            recorder, vision = seeing_recorder(server)
            chunks = ask(recorder, messages)
            (sent,) = server.generate_requests
            completions = [r for r in server.requests if "_generate" not in r]
            rendered = len(server.tokenize_requests)
        call = recorder.calls[0]
        assert completions == [] and call.route == "generate"
        assert call.problems() == [] and call.ids_checked
        # One rendering for the request, and none for the image by itself.
        assert rendered == 1
        # One placeholder went out for the image; its run came back.
        assert sent["token_ids"].count(PAD) == 1
        assert sent["content_parts"] == [{"type": "image_url", "url": RED.url}]
        assert call.prompt_token_ids is not None and call.prompt_token_ids.count(PAD) == RED_TOKENS
        assert sent["sampling_params"]["logprobs"] == 0
        assert sent["sampling_params"]["temperature"] == 1.0
        assert sent["sampling_params"]["max_tokens"] == 500
        (placed,) = vision.images
        assert placed.image.sha256 == RED.sha256 and placed.length == RED_TOKENS
        assert call.prompt_token_ids[placed.start - 1] == IMAGE
        assert call.prompt_token_ids[placed.start + placed.length] == IMAGE_END
        assert call.request["images"] == 1
        # The prompt's length is known before the request goes out.
        assert call.prompt_tokens_sent == len(call.prompt_token_ids)
        # The reply is read as on the completions route, and the model saw the image.
        delta = chunks[0].choices[0].delta
        assert delta.tool_calls[0].function.name == "bash"
        assert delta.content.endswith("saw:" + RED.sha256[:6])

    def conversation(self, server: ScriptedChatServer, recorder: RecordingClient) -> list[Any]:
        """Three turns; a tool returns an image after the first and another
        after the second."""
        messages = list(MESSAGES)
        first = ask(recorder, messages)
        messages += [
            turn_of(first),
            {"role": "tool", "tool_call_id": turn_of(first)["tool_calls"][0]["id"], "content": "a"},
            shows(RED, "Images returned by the tool:"),
        ]
        second = ask(recorder, messages)
        messages += [
            turn_of(second),
            {
                "role": "tool",
                "tool_call_id": turn_of(second)["tool_calls"][0]["id"],
                "content": "b",
            },
            shows(BLUE, "Another one:"),
        ]
        ask(recorder, messages)
        return list(server.generate_requests)

    def test_the_prompts_of_a_conversation_with_images_form_one_sequence(self) -> None:
        with ScriptedChatServer([STEP, STEP, {"content": "done"}]) as server:
            recorder, vision = seeing_recorder(server)
            sent = self.conversation(server, recorder)
            renderings = [r for r in server.tokenize_requests if "messages" in r]
        assert [c.route for c in recorder.calls] == ["completions", "generate", "generate"]
        assert all(c.problems() == [] for c in recorder.calls)
        record = build_token_record([c.to_dict() for c in recorder.calls])
        assert record["one_sequence"] is True and record["prefix"]["ok"] is True
        # Each request carried every image of the conversation so far.
        assert [len(r["content_parts"]) for r in sent] == [1, 2]
        assert [r["token_ids"].count(PAD) for r in sent] == [1, 2]
        # One rendering a request once it holds images: the whole conversation's
        # begins with what was sent and generated, so no second one is asked for.
        assert len(renderings) == 3
        final = rebuild_final(record)
        assert [(p.image.sha256, p.length) for p in vision.images] == [
            (RED.sha256, RED_TOKENS),
            (BLUE.sha256, BLUE_TOKENS),
        ]
        for placed in vision.images:
            assert set(final[placed.start : placed.start + placed.length]) == {PAD}
        assert final.count(PAD) == RED_TOKENS + BLUE_TOKENS

    @pytest.mark.parametrize(
        ("fault", "problem"),
        [
            ("other_prompt", "another prompt than the token IDs it was sent"),
            ("other_places", "did not place the images where the prompt holds them"),
            # A reply that came whole and never said which prompt it answered.
            ("no_prompt", "no prompt token IDs returned"),
        ],
    )
    def test_a_server_that_expands_another_way_is_the_alarm(self, fault: str, problem: str) -> None:
        """Found when the reply is read, whoever asked; nothing goes out after."""
        messages = [MESSAGES[0], shows(RED)]
        with ScriptedChatServer([STEP, STEP], generate_fault=fault) as server:
            recorder, _vision = seeing_recorder(server)
            with pytest.raises(HarnessFaultError, match="a request with images"):
                ask(recorder, messages)
            assert any(problem in said for said in recorder.calls[0].problems())
            asked = len(server.generate_requests)
            with pytest.raises(HarnessFaultError):
                ask(recorder, messages)
            assert len(server.generate_requests) == asked
        assert recorder.id_prompts is not None and recorder.id_prompts.fault

    def test_a_sub_agents_reply_that_names_no_prompt_is_the_alarm_too(self) -> None:
        """Nothing of a sub-agent is in the token record, so nothing else
        would hold such a reply against the prompt that was sent."""
        from turnstone.core import session as session_module

        messages = [MESSAGES[0], shows(RED)]
        with ScriptedChatServer([STEP, STEP], generate_fault="no_prompt") as server:
            recorder, _vision = seeing_recorder(server)
            inside = session_module._active_task_agent_cancel_scope.set(object())  # type: ignore[arg-type]
            try:
                with pytest.raises(HarnessFaultError, match="without saying which prompt"):
                    ask(recorder, messages)
            finally:
                session_module._active_task_agent_cancel_scope.reset(inside)
        assert recorder.calls[0].scope == "sub_agent"
        assert recorder.id_prompts is not None and "sub_agent" in str(recorder.id_prompts.fault)

    def test_one_rendering_a_request_also_after_a_prompt_that_is_not_the_rendering(self) -> None:
        """A model's own split of a text is not the tokenizer's: from then on
        the whole rendering no longer begins with what was sent and generated.
        The end of the turn is found in the one rendering all the same."""
        merged = {**STEP, "content": "a cab drives", "merge": True}
        with ScriptedChatServer([merged, STEP, {"content": "done"}], own_tokens=True) as server:
            recorder, vision = seeing_recorder(server, PLAIN)
            sent = self.conversation(server, recorder)
            renderings = [r for r in server.tokenize_requests if "messages" in r]
        assembly = recorder.id_prompts
        assert assembly is not None and assembly.fault is None
        assert [e["event"] for e in assembly.events] == ["prompt_differs_from_rendering"] * 2
        assert all(c.problems() == [] for c in recorder.calls)
        record = build_token_record([c.to_dict() for c in recorder.calls])
        assert record["one_sequence"] is True
        assert [len(r["content_parts"]) for r in sent] == [1, 2]
        # One rendering a request: three requests, three renderings.
        assert len(renderings) == 3
        assert [p.length for p in vision.images] == [RED_TOKENS, BLUE_TOKENS]

    STRAY = {
        "role": "user",
        "content": [{"type": "text", "text": "it says " + PAD_TEXT}, RED.part()],
    }

    def test_the_placeholders_text_before_an_image_is_read_as_plain_text(self) -> None:
        """What a tool printed is characters: the image stands where the template
        wrote its place, and the text beside it is no token."""
        with ScriptedChatServer([STEP], own_tokens=True) as server:
            recorder, vision = seeing_recorder(server, PLAIN)
            ask(recorder, [MESSAGES[0], self.STRAY])
            (sent,) = server.generate_requests
        call = recorder.calls[0]
        assert call.problems() == [] and call.prompt_token_ids is not None
        (placed,) = vision.images
        prompt = call.prompt_token_ids
        assert prompt.count(PAD) == RED_TOKENS and sent["token_ids"].count(PAD) == 1
        assert (prompt[placed.start - 1], prompt[placed.start + RED_TOKENS]) == (IMAGE, IMAGE_END)
        # The text itself, spelled out, before the image.
        spelled = encode("it says " + PAD_TEXT)
        assert prompt[placed.start - 1 - len(spelled) : placed.start - 1] == spelled

    def test_without_that_the_server_gives_the_image_to_the_text_and_the_harness_says_so(
        self,
    ) -> None:
        """A run with images always reads such text as plain text, so this
        cannot come from a conversation: nothing is sent, and it is the alarm."""
        with ScriptedChatServer([STEP], own_tokens=True) as server:
            recorder, _vision = seeing_recorder(server)
            with pytest.raises(HarnessFaultError, match="an image has no place"):
                ask(recorder, [MESSAGES[0], self.STRAY])
            assert server.generate_requests == [] and server.requests == []

    def test_a_reply_part_that_cannot_be_read_marks_the_call(self) -> None:
        call = CallRecord(index=0, streamed=True, request={})
        assert take_generate(call, "no object") is None
        assert call.unreadable is not None and call.aligned is False

    def test_a_part_whose_values_cannot_be_read_costs_the_call_no_token(self) -> None:
        """The IDs are the call's as soon as they are read: a value after
        them that cannot be read costs the log-probabilities, not a token the
        next prompt would then be built without."""
        call = CallRecord(index=0, streamed=True, request={})
        part = {"choices": [{"index": 0, "token_ids": [5, 6], "text": "ab", "logprobs": {"content": [7, 8]}}]}  # fmt: skip
        assert take_generate(call, part) is None
        assert call.token_ids == [5, 6] and call.unreadable is not None
        assert call.aligned is False and call.logprobs == []

    def test_places_that_cannot_be_read_are_not_the_places_that_were_sent(self) -> None:
        call = CallRecord(index=0, streamed=True, request={}, route="generate")
        call.prompt_sent, call.placeholders_sent = [1, 2, 3], [[1, 1]]
        part = {"prompt_token_ids": [1, 2, 3], "mm_placeholders": {"image": [{"offset": "x"}]}}
        assert take_generate(call, part) is None
        assert call.placeholders_differ is True and call.unreadable is not None
        assert "did not place the images" in " ".join(call.problems())


class TestThePromptsLength:
    def test_the_servers_speed_is_held_against_the_tokens_sent_not_the_bytes(self) -> None:
        """A request with a large image is a few thousand tokens, whatever the
        size of the image's data."""
        meter = Meter(Budgets(wall_seconds=100, context_window=128000), lambda: [])
        call = CallRecord(index=0, streamed=True, request={}, prompt_bytes=900_000)
        assert meter._prompt_tokens(call) == 128000
        call.prompt_tokens_sent = 4115
        assert meter._prompt_tokens(call) == 4115

    def test_when_a_silent_server_counts_as_stalled_is_set_by_the_tokens_sent(self) -> None:
        """With a prompt sent as token IDs, text only or with images, the time
        a server may take for it is the prompt's exact length at the minimum
        speed.  A prompt sent as messages is still bounded by its size in
        bytes, which nothing else is known of."""
        from types import SimpleNamespace

        from turnstone.rollout.episode import _classify_timeout

        budgets = Budgets(
            wall_seconds=100, context_window=128000, min_prompt_tokens_per_second=1000.0
        )

        def waiting(silence: float, **said: Any) -> str:
            call = CallRecord(index=0, streamed=True, request={}, prompt_bytes=50_000, **said)
            call.started_at = call.last_heard_at = 1000.0
            recorder: Any = SimpleNamespace(calls=[call])
            return _classify_timeout(recorder, 0.0, 1000.0 + silence, 30.0, budgets)[0]

        # 4,000 tokens at 1,000 a second: 4 s for the prompt, then 30 s of silence.
        assert waiting(33.99, prompt_tokens_sent=4000) == "timeout"
        assert waiting(34.0, prompt_tokens_sent=4000) == "server_stalled"
        # Sent as messages, the same request is allowed its 50,000 bytes: 50 s, then 30.
        assert waiting(34.0) == "timeout" and waiting(79.99) == "timeout"
        assert waiting(80.0) == "server_stalled"


class TestKeepingTheImages:
    def test_an_image_is_written_under_its_hash_once(self, tmp_path: Any) -> None:
        folder = str(tmp_path / "images")
        assert write_image(folder, RED) is None
        path = os.path.join(folder, RED.sha256 + ".png")
        with open(path, "rb") as handle:
            assert handle.read() == RED.data
        before = os.stat(path).st_mtime_ns
        assert write_image(folder, RED) is None
        assert os.stat(path).st_mtime_ns == before and os.listdir(folder) == [RED.sha256 + ".png"]

    def test_a_folder_that_takes_no_file_is_said(self, tmp_path: Any) -> None:
        blocked = tmp_path / "file"
        blocked.write_text("x")
        assert "Error" in str(write_image(str(blocked / "images"), RED))

    def test_where_an_image_came_from_is_read_off_the_conversation(self) -> None:
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "read_file", "arguments": '{"path": "a.png"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [
                    {"type": "text", "text": "x"},
                    {"type": "image", "attachment_id": "h1"},
                ],
            },
            {"role": "user", "content": [{"type": "image", "attachment_id": "h2"}]},
        ]
        assert image_sources(messages) == [
            {
                "tool_call_id": "c1",
                "tool": "read_file",
                "arguments": '{"path": "a.png"}',
                "turn": 0,
                "sha256": "h1",
            },
            None,
        ]


class _ImageSandbox(_DirSandbox):
    """A directory sandbox that starts with an image file in it."""

    def start(self) -> Any:
        environment = super().start()
        with open(os.path.join(self.root, "shot.png"), "wb") as handle:
            handle.write(RED.data)
        return environment


# The first reply opens with a token the toy model is unsure of, so that the probe of the
# generate route, which a check for images makes, has something to measure.
LOOK: list[dict[str, Any]] = [
    {
        "reasoning": "I look at it.",
        "tool_calls": [{"name": "read_file", "arguments": {"path": "shot.png"}}],
    },
    {
        "reasoning": "And once more.",
        "tool_calls": [{"name": "read_file", "arguments": {"path": "shot.png"}}],
    },
    {"content": "It is red."},
]
LOOKS = Task(prompt="what is in shot.png?", key="t1")


class TestWholeRollouts:
    def run(
        self, tmp_path: Any, script: list[dict[str, Any]] = LOOK, **config: Any
    ) -> dict[str, Any]:
        options = {**SERVED, **config.pop("server", {})}
        with ScriptedChatServer(script, sampler_logprobs=True, **options) as server:
            record, _box = _run(server, task=LOOKS, sandbox=_ImageSandbox(), **{**SEES, **config})
            self.generate_requests = list(server.generate_requests)
        return record

    def test_switched_off_the_model_is_told_it_cannot_see(self, tmp_path: Any) -> None:
        record = self.run(tmp_path, vision=False, image_dir=str(tmp_path / "i"))
        assert record["trainable"] is True
        assert record["vision"] == {"applied": False, "reason": "switched off"}
        # The check asked nothing about images.
        assert self.generate_requests == [] and "vision" not in record["token_id_route"]
        assert "does not support vision" in record["tool_log"][0]["result"]
        assert PAD not in rebuild_final(record["tokens"])
        assert {c["route"] for c in record["calls"]} == {"completions"}

    def test_by_default_the_model_sees_where_the_server_can_be_sent_images(
        self, tmp_path: Any
    ) -> None:
        folder = str(tmp_path / "images")
        record = self.run(tmp_path, vision=None, image_dir=folder)
        assert record["problems"] == []
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["finish_state"] == "completed"
        # The first request holds no image; the two after it do.
        assert [c["route"] for c in record["calls"]] == ["completions", "generate", "generate"]
        assert [len(r["content_parts"]) for r in self.generate_requests[-2:]] == [1, 2]
        seen = record["vision"]
        assert (
            seen["applied"] is True and seen["check"]["ok"] is True and seen["placeholder"] == PAD
        )
        assert record["token_id_route"]["vision"] == seen["check"]
        assert seen["image_dir"] == folder and seen["stray_placeholders"]["count"] == 0
        assert record["plain_text"]["applied"] is True
        final = rebuild_final(record["tokens"])
        first, second = seen["images"]
        for number, image in enumerate((first, second)):
            assert image["index"] == number and image["sha256"] == RED.sha256
            assert image["file"] == RED.sha256 + ".png" and image["media_type"] == "image/png"
            assert (image["width"], image["height"], image["tokens"]) == (24, 16, RED_TOKENS)
            assert image["end"] - image["start"] == RED_TOKENS
            # What the server made of it: the size it was scaled to, and the
            # patches of 16 pixels before they are merged two by two.
            assert image["processed"] == {"width": 320, "height": 224, "grid": [1, 14, 20]}
            rows, columns = image["processed"]["grid"][1:]
            assert rows * columns // 4 == image["tokens"]
            assert set(final[image["start"] : image["end"]]) == {PAD}
            assert final[image["start"] - 1] == IMAGE and final[image["end"]] == IMAGE_END
            assert image["first_call"] == number + 1
            assert image["source"]["tool"] == "read_file" and image["source"]["turn"] == number
            assert json.loads(image["source"]["arguments"]) == {"path": "shot.png"}
        assert final.count(PAD) == 2 * RED_TOKENS
        # The bytes are kept once, under their hash.
        assert os.listdir(folder) == [RED.sha256 + ".png"]
        with open(os.path.join(folder, first["file"]), "rb") as handle:
            assert hashlib.sha256(handle.read()).hexdigest() == first["sha256"]
        # The record itself holds no image data.
        assert "base64" not in json.dumps(record)
        assert "It is red. saw:" + RED.sha256[:6] in record["final_content"]

    @pytest.mark.parametrize(
        ("config", "server", "why"),
        [
            # The image check of the server did not pass.
            ({}, {"generate_status": 403}, "--enable-scale-out"),
            ({}, {"own_image_sizes": True}, "does not size images as the model's image processor"),
            # Plain text is off.
            ({"plain_text": False}, {}, "needs the conversation's text read as plain text"),
            # No folder for the images.
            ({"image_dir": None}, {}, "names no folder for them"),
            # A request parameter the generate route has no place for.
            ({"extra_body": {"top_k": 20, "priority": 3}}, {}, "'priority' has no place"),
        ],
    )
    def test_a_run_that_cannot_have_vision_is_refused_whatever_the_reason(
        self, tmp_path: Any, config: dict[str, Any], server: dict[str, Any], why: str
    ) -> None:
        """Vision is on unless the run says otherwise, and never switched off
        by what turns out to be missing: nothing is run."""
        settings = {"image_dir": str(tmp_path / "i"), **config}
        for vision in (None, True):
            with pytest.raises(SetupError, match=why):
                self.run(tmp_path, vision=vision, server=server, **settings)
        # Said aloud, the same run goes ahead without images.
        settings["plain_text"] = False
        record = self.run(tmp_path, vision=False, server=server, **settings)
        assert record["vision"] == {"applied": False, "reason": "switched off"}
        assert "does not support vision" in record["tool_log"][0]["result"]

    def given(
        self, server: ScriptedChatServer, **config: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The verdicts of the checks of *server*, as a batch is handed them."""
        from turnstone.rollout.episode import RolloutConfig, plain_text_check, template_writes

        made_for = RolloutConfig(
            base_url=server.base_url,
            model="scripted",
            extra_body={"top_k": 20, "top_p": 0.95},
            token_id_prompts=True,
            added_tokens=ADDED_TOKENS,
            **config,
        )
        route = token_id_route_verdict(made_for)
        plain = plain_text_check(made_for, *template_writes(route))
        assert route["ok"] is True and plain["ok"] is True
        return route, plain

    def test_a_verdict_made_without_images_cannot_drive_a_run_with_them(
        self, tmp_path: Any
    ) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            route, plain = self.given(server, vision=False, plain_text=True)
            assert "vision" not in route
            handed = {
                "token_id_prompts": True,
                "token_id_route": route,
                "plain_text_verdict": plain,
                "image_dir": str(tmp_path / "i"),
            }
            with pytest.raises(SetupError, match="asked nothing about images"):
                _run(server, task=LOOKS, sandbox=_ImageSandbox(), plain_text=True, vision=None, **handed)  # fmt: skip
            # The stand-in of a check that knew nothing of images may be a
            # token the template writes for one.
            seeing, _plain = self.given(server)
            collides = {**plain, "stand_in": PAD_TEXT, "stand_in_id": PAD}
            handed.update(token_id_route=seeing, plain_text_verdict=collides)
            with pytest.raises(SetupError, match="stand-in for plain text is a token"):
                _run(server, task=LOOKS, sandbox=_ImageSandbox(), plain_text=True, vision=None, **handed)  # fmt: skip

    def test_the_stand_in_is_chosen_clear_of_the_tokens_of_an_image(self) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            route, plain = self.given(server)
        seeing = route["vision"]
        assert plain["stand_in_id"] not in (seeing["placeholder"], *seeing["frame"])

    def test_what_was_set_up_is_said_to_be_applied_only_once_a_model_call_was_made(
        self, tmp_path: Any
    ) -> None:
        """A run that never built a prompt applied nothing to a conversation."""
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            route, plain = self.given(server)
            server.forget()
            record, _box = _run(
                server,
                task=LOOKS,
                sandbox=_DirSandbox(fail_start=True),
                token_id_prompts=True,
                token_id_route=route,
                plain_text=True,
                plain_text_verdict=plain,
                vision=None,
                image_dir=str(tmp_path / "i"),
            )
            assert server.requests == []
        assert record["finish_state"] == "sandbox_failed"
        said = {"applied": False, "reason": "the run made no model call"}
        assert record["plain_text"] == said and record["vision"] == said
        assert record["control_text_escape"]["applied"] is False
        assert record["control_text_escape"]["reason"] == "no model call was made"

    def test_a_run_refused_for_vision_in_the_wrong_mode(self, tmp_path: Any) -> None:
        with (
            ScriptedChatServer(LOOK, sampler_logprobs=True) as server,
            pytest.raises(SetupError, match="vision is for prompts as token IDs"),
        ):
            _run(server, vision=True, image_dir=str(tmp_path))

    def test_a_given_verdict_spares_the_checks(self, tmp_path: Any) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            route, plain = self.given(server)
            assert route["vision"]["ok"] is True
            server.forget()
            server.generate_requests.clear()
            record, _box = _run(
                server,
                task=LOOKS,
                sandbox=_ImageSandbox(),
                token_id_prompts=True,
                token_id_route=route,
                plain_text=True,
                plain_text_verdict=plain,
                vision=None,
                image_dir=str(tmp_path / "i"),
            )
            asked = len(server.generate_requests)
        assert record["trainable"] is True and record["vision"]["check"] == route["vision"]
        # Only the run's own two requests with images went to the generate route.
        assert asked == 2

    def test_a_verdict_for_plain_text_made_with_other_template_arguments_is_refused(
        self, tmp_path: Any
    ) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            route, plain = self.given(server)
            assert plain["template_arguments"] == route["template_arguments"] == {}
            handed = {
                "task": LOOKS,
                "sandbox": _ImageSandbox(),
                "token_id_prompts": True,
                "token_id_route": route,
                "plain_text": True,
                "image_dir": str(tmp_path / "i"),
            }
            other = {**plain, "template_arguments": {"enable_thinking": False}}
            with pytest.raises(SetupError, match="other template arguments"):
                _run(server, plain_text_verdict=other, **handed)
            unsaid = {k: v for k, v in plain.items() if k != "template_arguments"}
            with pytest.raises(SetupError, match="template_arguments missing"):
                _run(server, plain_text_verdict=unsaid, **handed)

    def test_sizing_that_is_not_the_servers_is_a_harness_fault(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sizing here is the server's own code.  Another number of
        tokens than the server's, or an image the server rendered that cannot
        be sized here, is no property of an image: the run stops, loudly,
        before the request with the image goes out."""
        import turnstone.rollout.vision as vision_module

        real = vision_module.server_image_size

        # The run's own image only: the images of the check before the run
        # are sized as they are.
        def other(data: bytes) -> tuple[int, int]:
            return (640, 480) if data == RED.data else real(data)

        def cannot(data: bytes) -> tuple[int, int]:
            if data == RED.data:
                raise OSError("cannot identify image file")
            return real(data)

        for sizing, why in (
            (other, "the sizing here gives 300"),
            (cannot, "could not be sized here (OSError: cannot identify image file)"),
        ):
            monkeypatch.setattr(vision_module, "server_image_size", sizing)
            record = self.run(tmp_path, image_dir=str(tmp_path / "i"))
            assert record["finish_state"] == "harness_fault", record["error"]
            assert why in record["error"] and cli._status(record) == cli.FAULT
            assert record["trainable"] is False and record["vision"]["images"] == []
            # The request with the run's image never went out.
            assert not [r for r in self.generate_requests if RED.url in json.dumps(r)]

    def test_a_sub_agent_sees_the_image_too_and_none_of_it_is_the_rollouts(
        self, tmp_path: Any
    ) -> None:
        parent = [
            {"reasoning": "I delegate.", "tool_calls": [SPAWN]},
            {"content": "The sub-agent looked."},
        ]
        agent = [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "shot.png"}}]},
            {"content": "It is red."},
        ]
        folder = str(tmp_path / "images")
        record = self.run(tmp_path, parent, image_dir=folder, server={"agent_script": agent})
        with_images = [r for r in self.generate_requests if r.get("content_parts")]
        assert record["problems"] == [] and record["trainable"] is True
        assert record["subagents"]["started"] == 1
        # The sub-agent's request with the image went to the generate route, with
        # the pixels; the session's own requests held no image.
        assert RED.url in json.dumps(with_images[-1])
        assert {c["route"] for c in record["calls"]} == {"completions"}
        assert record["vision"]["images"] == [] and record["vision"]["subagent_images"] == 1
        assert PAD not in rebuild_final(record["tokens"])
        assert not os.path.exists(folder) or os.listdir(folder) == []

    def test_an_image_that_cannot_be_kept_makes_the_record_untrainable(self, tmp_path: Any) -> None:
        blocked = tmp_path / "file"
        blocked.write_text("x")
        record = self.run(tmp_path, image_dir=str(blocked / "images"))
        assert record["trainable"] is False
        assert any("could not be kept" in p for p in record["problems"])

    def test_a_text_file_with_the_placeholders_text_beside_an_image_changes_nothing(
        self, tmp_path: Any
    ) -> None:
        """Read in one turn, the text first: its result stands before the image."""
        script = [
            {
                "reasoning": "I read both.",
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "notes.txt"}},
                    {"name": "read_file", "arguments": {"path": "shot.png"}},
                ],
            },
            {"content": "Seen."},
        ]
        with ScriptedChatServer(script, sampler_logprobs=True, **SERVED) as server:
            record, _box = _run(
                server,
                task=Task(prompt="look", key="t1", setup_files={"notes.txt": "writes " + PAD_TEXT}),
                sandbox=_ImageSandbox(),
                image_dir=str(tmp_path / "i"),
                **SEES,
            )
        assert record["problems"] == [] and record["finish_state"] == "completed"
        (image,) = record["vision"]["images"]
        final = rebuild_final(record["tokens"])
        assert (final[image["start"] - 1], final[image["end"]]) == (IMAGE, IMAGE_END)
        assert record["vision"]["stray_placeholders"]["count"] == 0
        assert final.count(PAD) == RED_TOKENS
        assert image["source"]["arguments"] == json.dumps({"path": "shot.png"})
        assert record["plain_text"]["seen"] == {PAD_TEXT: 1}

    def faulty(
        self, tmp_path: Any, script: list[dict[str, Any]], **server_options: Any
    ) -> dict[str, Any]:
        """A run on a server that, once the checks are made, writes one
        placeholder token too many."""
        with ScriptedChatServer(
            script, sampler_logprobs=True, **{**SERVED, **server_options}
        ) as server:
            route, plain = self.given(server)
            assert route["vision"]["ok"] is True
            server.generate_fault = "other_prompt"
            server.generate_requests.clear()
            record, _box = _run(
                server,
                task=LOOKS,
                sandbox=_ImageSandbox(),
                token_id_prompts=True,
                token_id_route=route,
                plain_text=True,
                plain_text_verdict=plain,
                image_dir=str(tmp_path / "i"),
                vision=None,
            )
            self.generate_requests = list(server.generate_requests)
        return record

    def test_a_server_that_answers_for_another_prompt_ends_the_run_as_a_harness_fault(
        self, tmp_path: Any
    ) -> None:
        record = self.faulty(tmp_path, LOOK)
        assert record["finish_state"] == "harness_fault" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False and "a request with images" in record["error"]
        # One request with an image went out; nothing after the fault was found.
        assert len(self.generate_requests) == 1
        # A status of its own: not a refusal, and not one a caller runs again.
        assert cli._status(record) == cli.FAULT == 4

    def test_the_same_in_a_sub_agents_request_is_a_harness_fault_too(self, tmp_path: Any) -> None:
        parent = [
            {"reasoning": "I delegate.", "tool_calls": [SPAWN]},
            {"content": "The sub-agent looked."},
        ]
        agent = [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "shot.png"}}]},
            {"content": "It is red."},
        ]
        record = self.faulty(tmp_path, parent, agent_script=agent)
        assert record["finish_state"] == "harness_fault" and "sub_agent" in record["error"]
        assert cli._status(record) == cli.FAULT
        # The sub-agent's one request with the image; neither it nor the session asked again.
        assert len(self.generate_requests) == 1

    def test_a_model_that_generates_the_placeholder_ends_its_own_run(self, tmp_path: Any) -> None:
        """The server would give the next image to the token the model wrote.
        That is the model's doing, and the run's ending is its outcome."""
        script = [
            {
                "reasoning": "I look.",
                "content": "see " + PAD_TEXT,
                "own_tokens": True,
                "tool_calls": [{"name": "read_file", "arguments": {"path": "shot.png"}}],
            },
            {"content": "never asked"},
        ]
        record = self.run(tmp_path, script, image_dir=str(tmp_path / "i"))
        with_images = [r for r in self.generate_requests if r.get("content_parts")]
        assert record["finish_state"] == "image_token_generated"
        assert record["finish_cause"] == "model" and record["trainable"] is True
        assert record["turns"] == 1 and cli._status(record) == 0
        # The request with the image never went out.
        assert not [r for r in with_images if RED.url in json.dumps(r)]
        assert record["vision"]["images"] == []
        assert record["vision"]["stray_placeholders"]["count"] == 1


class TestTheCommand:
    def probe(self, tmp_path: Any, *flags: str, **server_options: Any) -> tuple[int, Any, int]:
        out = tmp_path / "server.json"
        with ScriptedChatServer(
            [{"content": "x"}], sampler_logprobs=True, **{**SERVED, **server_options}
        ) as server:
            status = cli.main(
                ["--base-url", server.base_url, "--model", "m", "--top-k", "20", "--top-p", "0.95",
                 "--token-id-prompts", "--probe-only", "--out", str(out), *flags]
            )  # fmt: skip
            asked = len(server.generate_requests)
        return status, json.loads(out.read_text()), asked

    def test_the_probe_checks_for_images_by_default(self, tmp_path: Any, capsys: Any) -> None:
        status, verdict, _asked = self.probe(tmp_path, "--tokenizer-info", "server")
        assert status == 0
        # Three verdicts under keys of their own, and nothing of one at the top.
        assert sorted(verdict) == ["plain_text", "probe", "route", "server_check"]
        assert verdict["server_check"] == cli.SERVER_CHECK_LAYOUT == 2
        probe = verdict["probe"]
        assert probe["sampler_logprobs"] is True and probe["route"] == "completions"
        assert verdict["route"]["ok"] is True
        assert verdict["route"]["vision"]["ok"] is True
        assert verdict["plain_text"]["ok"] is True
        assert json.loads(capsys.readouterr().out) == verdict

    def test_a_server_that_cannot_be_sent_images_fails_the_probe(
        self, tmp_path: Any, capsys: Any
    ) -> None:
        """The rollouts handed this verdict would be refused: the probe says so
        with its status, with or without --vision."""
        for flags in ((), ("--vision",)):
            status, verdict, _asked = self.probe(
                tmp_path, "--tokenizer-info", "server", *flags, generate_status=403
            )
            assert status == 2 and verdict["route"]["ok"] is True
            assert verdict["route"]["vision"]["ok"] is False
            assert "refused unless they say --no-vision" in capsys.readouterr().err

    def test_the_same_server_passes_for_runs_without_images(self, tmp_path: Any) -> None:
        status, verdict, asked = self.probe(
            tmp_path, "--tokenizer-info", "server", "--no-vision", generate_status=403
        )
        assert status == 0 and asked == 0 and "vision" not in verdict["route"]

    def test_it_fails_where_plain_text_is_not_established(self, tmp_path: Any) -> None:
        """Every rollout with images would be refused at its start."""
        status, verdict, _asked = self.probe(
            tmp_path, "--tokenizer-info", "server", tokenizer_info=False
        )
        assert status == 2 and verdict["plain_text"]["ok"] is False

    def test_told_not_to_it_asks_nothing_about_images(self, tmp_path: Any) -> None:
        status, verdict, asked = self.probe(tmp_path, "--no-vision", "--no-plain-text")
        assert status == 0 and asked == 0
        assert "vision" not in verdict["route"] and "plain_text" not in verdict

    @pytest.mark.parametrize(
        ("flags", "why"),
        [
            (["--vision"], "--vision is for --token-id-prompts"),
            (["--no-plain-text"], "--no-plain-text is for --token-id-prompts"),
            (["--image-dir", "x"], "--image-dir is for --token-id-prompts"),
            (["--token-id-prompts", "--vision", "--no-vision"], "cannot both be given"),
            (["--token-id-prompts", "--vision", "--no-plain-text"], "Give --no-vision with"),
            # Without --no-vision the model sees images, so plain text cannot go.
            (["--token-id-prompts", "--no-plain-text"], "Give --no-vision with --no-plain-text"),
            (["--token-id-prompts", "--no-control-text-escape"], "nothing is escaped"),
            (["--control-text-escape", "--no-control-text-escape"], "cannot both be given"),
        ],
    )
    def test_flags_that_do_not_go_together_are_refused(
        self, flags: list[str], why: str, capsys: Any
    ) -> None:
        status = cli.main(
            ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p", *flags]
        )
        assert status == cli.REFUSED and why in capsys.readouterr().err

    def test_a_rollout_takes_both_verdicts_from_the_probes_file(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """One file for a batch: the route with what it found about images, and
        plain text beside it."""
        server_json = tmp_path / "server.json"
        out = tmp_path / "episode.json"
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            base = ["--base-url", server.base_url, "--model", "m", "--top-k", "20", "--top-p", "0.95",
                    "--token-id-prompts"]  # fmt: skip
            assert (
                cli.main(
                    [*base, "--probe-only", "--tokenizer-info", "server", "--out", str(server_json)]
                )
                == 0
            )
            real = cli.run_episode
            monkeypatch.setattr(
                cli,
                "run_episode",
                lambda task, config, **check: real(
                    task,
                    config,
                    sandbox_factory=lambda _t, _c: _ImageSandbox(),
                    allow_host_execution=True,
                    **check,
                ),
            )
            server.generate_requests.clear()
            status = cli.main(
                [*base, "--prompt", "look at shot.png", "--context-window", "200000",
                 "--sampler-logprobs", "@" + str(server_json), "--token-id-route", "@" + str(server_json),
                 "--out", str(out)]
            )  # fmt: skip
            asked = len(server.generate_requests)
        record = json.loads(out.read_text())
        assert status == 0 and asked == 2
        assert record["plain_text"]["applied"] is True and record["vision"]["applied"] is True
        # The images beside the record, named from the record's own folder.
        assert record["vision"]["image_dir"] == "episode.json.images"
        assert os.listdir(str(out) + ".images") == [RED.sha256 + ".png"]
        # What the probe said of plain text is not passed on as the probe's own.
        assert "plain_text" not in record["logprobs"]

    def test_a_probe_from_a_tokenizer_file_without_images_drives_a_rollout_without_them(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """What a text-only gym runs: the probe is handed the model's
        tokenizer file and --no-vision, and each rollout --no-vision and the
        probe's file.  Plain text is applied, nothing is asked about images."""
        described = tmp_path / "tokenizer_config.json"
        decoder = {str(token): {"content": text} for text, token in ADDED_TOKENS.items()}
        described.write_text(json.dumps({"added_tokens_decoder": decoder}))
        server_json = tmp_path / "server.json"
        out = tmp_path / "episode.json"
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            base = ["--base-url", server.base_url, "--model", "m", "--top-k", "20", "--top-p", "0.95",
                    "--token-id-prompts", "--no-vision"]  # fmt: skip
            probed = cli.main(
                [
                    *base,
                    "--probe-only",
                    "--tokenizer-info",
                    str(described),
                    "--out",
                    str(server_json),
                ]
            )
            verdict = json.loads(server_json.read_text())
            real = cli.run_episode
            monkeypatch.setattr(
                cli,
                "run_episode",
                lambda task, config, **check: real(
                    task,
                    config,
                    sandbox_factory=lambda _t, _c: _ImageSandbox(),
                    allow_host_execution=True,
                    **check,
                ),
            )
            status = cli.main(
                [*base, "--prompt", "look at shot.png", "--context-window", "200000",
                 "--sampler-logprobs", "@" + str(server_json), "--token-id-route", "@" + str(server_json),
                 "--out", str(out)]
            )  # fmt: skip
            asked = len(server.generate_requests)
            # The same file without --no-vision: refused, it asked nothing about images.
            refused = cli.main(
                [*base[:-1], "--prompt", "look at shot.png", "--context-window", "200000",
                 "--sampler-logprobs", "@" + str(server_json), "--token-id-route", "@" + str(server_json),
                 "--out", str(tmp_path / "never.json")]
            )  # fmt: skip
        assert probed == 0 and "vision" not in verdict["route"]
        assert verdict["plain_text"]["tokenizer_info"] == {
            "source": "named",
            "compared_with_server": True,
        }
        record = json.loads(out.read_text())
        assert status == 0 and asked == 0 and refused == cli.REFUSED
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["plain_text"]["applied"] is True
        assert record["vision"] == {"applied": False, "reason": "switched off"}
        assert record["control_text_escape"]["applied"] is False
        assert not os.path.exists(str(out) + ".images")

    @pytest.mark.parametrize("layout", [1, 3, "2", True, None])
    def test_a_file_laid_out_by_another_build_is_refused_by_both_flags(
        self, tmp_path: Any, capsys: Any, layout: Any
    ) -> None:
        """Both readers go by the layout's number: the one this build writes
        is read, a file without the key is read as an earlier build wrote
        it, and any other number is refused, whatever else the file holds."""
        status, verdict, _asked = self.probe(tmp_path, "--tokenizer-info", "server")
        assert status == 0
        capsys.readouterr()
        other = tmp_path / "other.json"
        # Everything a reader could go by is there, twice over: under this
        # build's keys and where an earlier build kept it.
        other.write_text(
            json.dumps(
                {
                    **verdict["probe"],
                    "token_id_route": verdict["route"],
                    **verdict,
                    "server_check": layout,
                }
            )
        )
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p",
                "--token-id-prompts", "--no-vision", "--no-plain-text"]  # fmt: skip
        for flags in (
            ["--sampler-logprobs", f"@{other}"],
            ["--sampler-logprobs", "yes", "--token-id-route", f"@{other}"],
        ):
            assert cli.main([*base, *flags]) == cli.REFUSED
            said = capsys.readouterr().err
            assert "made by another build" in said and flags[-2] in said

    def test_a_parameter_the_generate_route_has_no_place_for_is_refused_before_anything_is_asked(
        self, tmp_path: Any
    ) -> None:
        """It is known from the run's own settings, so the server is asked
        nothing for a run that will be refused."""
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            with pytest.raises(SetupError, match="'guided_json' has no place"):
                _run(
                    server,
                    sandbox=_ImageSandbox(),
                    **{**SEES, "image_dir": str(tmp_path / "i")},
                    extra_body={"top_k": 20, "top_p": 0.95, "guided_json": {"type": "object"}},
                )
            assert server.requests == [] and server.tokenize_requests == []
            assert server.generate_requests == [] and server.version_requests == []
