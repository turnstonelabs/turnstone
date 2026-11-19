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
    ResizeRule,
    Vision,
    VisionRoute,
    check_vision_route,
    collapsed,
    find_places,
    generate_body,
    image_size,
    image_sources,
    images_of,
    png,
    take_generate,
    write_image,
)

NL = chr(10)
RED = Image.of(png(24, 16, (220, 30, 30)), "image/png")  # six placeholder tokens
BLUE = Image.of(png(16, 16, (30, 60, 220)), "image/png")  # four
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
        assert image_size(png(40, 24, (1, 2, 3))) == (40, 24)
        assert image_size(b"no image") == (None, None)

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
        with pytest.raises(PromptAssemblyError, match="no image's stands at position 1"):
            find_places(prompt, 1, PAD, FRAME)

    def test_another_number_of_places_than_images_is_refused(self) -> None:
        prompt = [1, IMAGE, PAD, PAD, IMAGE_END, 7, IMAGE, PAD, IMAGE_END, 8]
        with pytest.raises(PromptAssemblyError, match="2 places .* 1 images"):
            find_places(prompt, 1, PAD, FRAME)
        with pytest.raises(PromptAssemblyError, match="0 places .* 1 images"):
            find_places([1, 2, 3], 1, PAD, FRAME)


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
        # The toy server counts patches its own way: no rule is claimed for it.
        assert verdict["resize_rule"] is None
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
        # A server that cannot be sent images still passes the check itself.
        with ScriptedChatServer([step], sampler_logprobs=True, generate_status=403) as server:
            verdict = check_token_id_route(make_client(server), "m", vision=True)
        assert verdict["ok"] is True and verdict["vision"]["ok"] is False

    def test_a_verdict_must_hold_what_a_passed_check_found(self) -> None:
        verdict = self.check()
        for key, why in (
            ("placeholder", "placeholder must be the token"),
            ("frame", "frame must be the two tokens"),
            ("sampler_logprobs", "sampler_logprobs must be the probe"),
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
        assert call.prompt_token_ids is not None and call.prompt_token_ids.count(PAD) == 6
        assert sent["sampling_params"]["logprobs"] == 0
        assert sent["sampling_params"]["temperature"] == 1.0
        assert sent["sampling_params"]["max_tokens"] == 500
        (placed,) = vision.images
        assert placed.image.sha256 == RED.sha256 and placed.length == 6
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
            (RED.sha256, 6),
            (BLUE.sha256, 4),
        ]
        for placed in vision.images:
            assert set(final[placed.start : placed.start + placed.length]) == {PAD}
        assert final.count(PAD) == 10

    @pytest.mark.parametrize(
        ("fault", "problem"),
        [
            ("other_prompt", "another prompt than the token IDs it was sent"),
            ("other_places", "did not place the images where the prompt holds them"),
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
        assert prompt.count(PAD) == 6 and sent["token_ids"].count(PAD) == 1
        assert (prompt[placed.start - 1], prompt[placed.start + 6]) == (IMAGE, IMAGE_END)
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


class TestThePromptsLength:
    def test_the_servers_speed_is_held_against_the_tokens_sent_not_the_bytes(self) -> None:
        """A request with a large image is a few thousand tokens, whatever the
        size of the image's data."""
        meter = Meter(Budgets(wall_seconds=100, context_window=128000), lambda: [])
        call = CallRecord(index=0, streamed=True, request={}, prompt_bytes=900_000)
        assert meter._prompt_tokens(call) == 128000
        call.prompt_tokens_sent = 4115
        assert meter._prompt_tokens(call) == 4115


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
            assert (image["width"], image["height"], image["tokens"]) == (24, 16, 6)
            assert image["end"] - image["start"] == 6 and image["processed"] is None
            assert set(final[image["start"] : image["end"]]) == {PAD}
            assert final[image["start"] - 1] == IMAGE and final[image["end"]] == IMAGE_END
            assert image["first_call"] == number + 1
            assert image["source"]["tool"] == "read_file" and image["source"]["turn"] == number
            assert json.loads(image["source"]["arguments"]) == {"path": "shot.png"}
        assert final.count(PAD) == 12
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
            ({}, {"generate_status": 403}, "the server cannot be sent images"),
            ({"plain_text": False}, {}, "not read as plain text"),
            ({"image_dir": None}, {}, "names no folder for its images"),
        ],
    )
    def test_by_default_a_run_that_cannot_goes_without_and_says_why(
        self, tmp_path: Any, config: dict[str, Any], server: dict[str, Any], why: str
    ) -> None:
        settings = {"image_dir": str(tmp_path / "i"), **config}
        record = self.run(tmp_path, vision=None, server=server, **settings)
        assert record["trainable"] is True
        assert record["vision"]["applied"] is False and why in record["vision"]["reason"]
        assert "does not support vision" in record["tool_log"][0]["result"]

    def test_a_run_that_asks_for_vision_and_cannot_have_it_is_refused(self, tmp_path: Any) -> None:
        with pytest.raises(SetupError, match="--enable-scale-out"):
            self.run(
                tmp_path, vision=True, image_dir=str(tmp_path), server={"generate_status": 403}
            )
        with pytest.raises(SetupError, match="names the folder its images are written to"):
            self.run(tmp_path, vision=True)
        with pytest.raises(
            SetupError, match="vision needs the conversation's text read as plain text"
        ):
            self.run(tmp_path, vision=True, image_dir=str(tmp_path), plain_text=False)
        with (
            ScriptedChatServer(LOOK, sampler_logprobs=True) as server,
            pytest.raises(SetupError, match="vision is for prompts as token IDs"),
        ):
            _run(server, vision=True, image_dir=str(tmp_path))

    def test_a_given_verdict_spares_the_checks(self, tmp_path: Any) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            from turnstone.rollout.episode import RolloutConfig, plain_text_check

            config = RolloutConfig(
                base_url=server.base_url,
                model="scripted",
                extra_body={"top_k": 20, "top_p": 0.95},
                token_id_prompts=True,
                added_tokens=ADDED_TOKENS,
            )
            route = token_id_route_verdict(config)
            plain = plain_text_check(config)
            assert route["ok"] is True and route["vision"]["ok"] is True and plain["ok"] is True
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
        assert record["vision"]["stray_placeholders"]["count"] == 0 and final.count(PAD) == 6
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
            from turnstone.rollout.episode import RolloutConfig, plain_text_check

            config = RolloutConfig(
                base_url=server.base_url,
                model="scripted",
                extra_body={"top_k": 20, "top_p": 0.95},
                token_id_prompts=True,
                added_tokens=ADDED_TOKENS,
            )
            route, plain = token_id_route_verdict(config), plain_text_check(config)
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
        assert verdict["sampler_logprobs"] is True and verdict["route"] == "completions"
        assert verdict["token_id_route"]["ok"] is True
        assert verdict["token_id_route"]["vision"]["ok"] is True
        assert verdict["plain_text"]["ok"] is True
        assert json.loads(capsys.readouterr().out) == verdict

    def test_without_images_it_passes_and_says_so(self, tmp_path: Any, capsys: Any) -> None:
        status, verdict, _asked = self.probe(
            tmp_path, "--tokenizer-info", "server", generate_status=403
        )
        assert status == 0 and verdict["token_id_route"]["vision"]["ok"] is False
        assert "the model will see no images" in capsys.readouterr().err

    def test_asked_for_images_it_fails_where_the_server_cannot(self, tmp_path: Any) -> None:
        status, _verdict, _asked = self.probe(
            tmp_path, "--tokenizer-info", "server", "--vision", generate_status=403
        )
        assert status == 2

    def test_asked_for_images_it_fails_where_plain_text_is_not_established(
        self, tmp_path: Any
    ) -> None:
        """Every rollout with images would be refused at its start."""
        status, verdict, _asked = self.probe(
            tmp_path, "--tokenizer-info", "server", "--vision", tokenizer_info=False
        )
        assert status == 2 and verdict["plain_text"]["ok"] is False

    def test_told_not_to_it_asks_nothing_about_images(self, tmp_path: Any) -> None:
        status, verdict, asked = self.probe(tmp_path, "--no-vision", "--no-plain-text")
        assert status == 0 and asked == 0
        assert "vision" not in verdict["token_id_route"] and "plain_text" not in verdict

    @pytest.mark.parametrize(
        ("flags", "why"),
        [
            (["--vision"], "--vision is for --token-id-prompts"),
            (["--no-plain-text"], "--no-plain-text is for --token-id-prompts"),
            (["--image-dir", "x"], "--image-dir is for --token-id-prompts"),
            (["--token-id-prompts", "--vision", "--no-vision"], "cannot both be given"),
            (
                ["--token-id-prompts", "--vision", "--no-plain-text"],
                "--vision needs the conversation's text",
            ),
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
