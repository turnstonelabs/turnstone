"""Images in a rollout whose prompts go out as token IDs: finding the images'
places, the request to the generate route, the check of the server, the
assembly of prompts with images, and whole rollouts.  Scripted server, no
container; the sandbox stand-ins are those of ``test_rollout_episode.py`` and
every script holds harmless commands only."""

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
from turnstone.rollout.episode import Task, vision_route_verdict
from turnstone.rollout.idprompt import PromptAssembly, PromptAssemblyError
from turnstone.rollout.plaintext import HarnessFaultError, PlainText
from turnstone.rollout.probe import sampler_logprobs_reported
from turnstone.rollout.recording import CallRecord, RecordingClient
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
    vision_verdict,
    write_image,
)

NL = chr(10)
RED = Image.of(png(24, 16, (220, 30, 30)), "image/png")  # six placeholder tokens
BLUE = Image.of(png(16, 16, (30, 60, 220)), "image/png")  # four
FRAME = (IMAGE, IMAGE_END)
SEEING = VisionRoute(placeholder=PAD, frame=FRAME)


def shows(image: Image, text: str = "look") -> dict[str, Any]:
    return {"role": "user", "content": [image.part(), {"type": "text", "text": text}]}


PLAIN = PlainText(
    stand_in="<spare>",
    stand_in_id=SPARE,
    spelled={text: tuple(encode(text)) for text in ADDED_TOKENS},
    tools_first=True,
)


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


class TestThePlacesOfTheImages:
    def test_each_image_takes_the_first_placeholder_left(self) -> None:
        prompt = [1, IMAGE, PAD, PAD, PAD, IMAGE_END, 7, IMAGE, PAD, PAD, IMAGE_END, 8]
        places = find_places(prompt, [3, 2], PAD)
        assert places == [(2, 3), (8, 2)]
        short = collapsed(prompt, places)
        assert short == [1, IMAGE, PAD, IMAGE_END, 7, IMAGE, PAD, IMAGE_END, 8]
        # Writing the runs out again gives the prompt back.
        assert collapsed(prompt, []) == prompt

    def test_a_placeholder_that_is_no_images_after_the_images_stays(self) -> None:
        prompt = [1, IMAGE, PAD, PAD, IMAGE_END, 7, PAD, 8]
        places = find_places(prompt, [2], PAD)
        assert places == [(2, 2)]
        assert collapsed(prompt, places) == [1, IMAGE, PAD, IMAGE_END, 7, PAD, 8]

    def test_a_placeholder_that_is_no_images_before_an_image_is_refused(self) -> None:
        """The server would put the image at the first placeholder it finds."""
        prompt = [1, PAD, 7, IMAGE, PAD, PAD, PAD, IMAGE_END]
        with pytest.raises(PromptAssemblyError, match="no image's stands before the image"):
            find_places(prompt, [3], PAD)

    def test_an_image_the_server_put_at_a_placeholder_of_a_text_is_refused(self) -> None:
        """A tokenize route has already written the image's run where the
        placeholder's text stood, and left the template's own place bare."""
        prompt = [1, PAD, PAD, PAD, 7, IMAGE, PAD, IMAGE_END, 8]
        with pytest.raises(PromptAssemblyError, match="no place the template wrote for an image"):
            find_places(prompt, [3], PAD, FRAME)
        # Without knowing what the template writes around an image, it cannot be told.
        assert find_places(prompt, [3], PAD) == [(1, 3)]

    def test_a_place_of_the_templates_left_without_its_image_is_refused(self) -> None:
        """The text that took the image can hold the template's markers too."""
        prompt = [1, IMAGE, PAD, PAD, PAD, IMAGE_END, 7, IMAGE, PAD, IMAGE_END, 8]
        with pytest.raises(PromptAssemblyError, match="holds no image"):
            find_places(prompt, [3], PAD, FRAME)

    def test_the_templates_places_pass_and_a_stray_token_after_them_stays(self) -> None:
        prompt = [1, IMAGE, PAD, PAD, IMAGE_END, IMAGE, PAD, IMAGE_END, 7, PAD, 8]
        assert find_places(prompt, [2, 1], PAD, FRAME) == [(2, 2), (6, 1)]

    def test_an_image_without_a_placeholder_is_refused(self) -> None:
        with pytest.raises(PromptAssemblyError, match="no placeholder for image 1"):
            find_places([1, PAD, PAD, 2], [2, 2], PAD)


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
    # What a served Qwen model's tokenize route gave images of these sizes.
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
        with ScriptedChatServer(
            [{"content": "It is one colour."}], sampler_logprobs=True, **faults
        ) as server:
            return check_vision_route(
                make_client(server), "m", extra_body={"top_k": 20, "top_p": 0.95}, temperature=1.0
            )

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
        }
        assert checks["images_reach_the_model"] is True
        assert verdict["sampler_logprobs"]["sampler_logprobs"] is True
        assert verdict["sampler_logprobs"]["route"] == "generate"
        assert checks["routes_agree"]["differing_positions"] == 0
        assert checks["routes_agree"]["positions"] > 0
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

    def test_a_given_verdict_is_held_against_the_run(self) -> None:
        with ScriptedChatServer([{"content": "x"}], sampler_logprobs=True) as server:
            verdict = check_vision_route(make_client(server), "m")
            endpoint = server.base_url
        assert vision_verdict(verdict, model="m", endpoint=endpoint, arguments={}) == verdict
        with pytest.raises(SetupError, match="made for the model"):
            vision_verdict(verdict, model="other", endpoint=endpoint, arguments={})
        with pytest.raises(SetupError, match="template arguments"):
            vision_verdict(verdict, model="m", endpoint=endpoint, arguments={"a": 1})
        with pytest.raises(SetupError, match="is not what a check returns"):
            vision_verdict([], model="m", endpoint=endpoint, arguments={})
        with pytest.raises(SetupError, match="placeholder must be the token"):
            vision_verdict(
                {**verdict, "placeholder": "x"}, model="m", endpoint=endpoint, arguments={}
            )
        with pytest.raises(SetupError, match="frame must be the two tokens"):
            vision_verdict({**verdict, "frame": [1]}, model="m", endpoint=endpoint, arguments={})
        with pytest.raises(SetupError, match="sampler_logprobs must be the probe"):
            vision_verdict(
                {**verdict, "sampler_logprobs": None}, model="m", endpoint=endpoint, arguments={}
            )


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
        call = recorder.calls[0]
        assert completions == [] and call.route == "generate"
        assert call.problems() == [] and call.ids_checked
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
        # The reply is read as on the completions route, and the model saw the image.
        delta = chunks[0].choices[0].delta
        assert delta.tool_calls[0].function.name == "bash"
        assert delta.content.endswith("saw:" + RED.sha256[:6])

    def test_the_prompts_of_a_conversation_with_images_form_one_sequence(self) -> None:
        script = [STEP, STEP, {"content": "done"}]
        with ScriptedChatServer(script) as server:
            recorder, vision = seeing_recorder(server)
            messages = list(MESSAGES)
            first = ask(recorder, messages)
            # A tool result, and the image the tool returned in a user message.
            messages += [
                turn_of(first),
                {
                    "role": "tool",
                    "tool_call_id": turn_of(first)["tool_calls"][0]["id"],
                    "content": "a",
                },
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
            sent = list(server.generate_requests)
        assert [c.route for c in recorder.calls] == ["completions", "generate", "generate"]
        assert all(c.problems() == [] for c in recorder.calls)
        record = build_token_record([c.to_dict() for c in recorder.calls])
        assert record["one_sequence"] is True and record["prefix"]["ok"] is True
        # Each request carried every image of the conversation so far.
        assert [len(r["content_parts"]) for r in sent] == [1, 2]
        assert [r["token_ids"].count(PAD) for r in sent] == [1, 2]
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
    def test_a_server_that_expands_another_way_is_found_out(self, fault: str, problem: str) -> None:
        messages = [MESSAGES[0], shows(RED)]
        with ScriptedChatServer([STEP, STEP], generate_fault=fault) as server:
            recorder, _vision = seeing_recorder(server)
            first = ask(recorder, messages)
            assert any(problem in said for said in recorder.calls[0].problems())
            messages += [
                turn_of(first),
                {
                    "role": "tool",
                    "tool_call_id": turn_of(first)["tool_calls"][0]["id"],
                    "content": "a",
                },
            ]
            # No prompt is built on a reply to another prompt, and it is no
            # conversation's doing.
            with pytest.raises(HarnessFaultError, match="a request with images"):
                ask(recorder, messages)
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
        """A tokenizer writes the placeholder token for its text, and a server
        puts the image there, on its tokenize route already.  A run with images
        always reads such text as plain text, so this cannot come from a
        conversation: nothing is sent, and it is the harness's fault."""
        with ScriptedChatServer([STEP], own_tokens=True) as server:
            recorder, _vision = seeing_recorder(server)
            with pytest.raises(HarnessFaultError, match="no place the template wrote"):
                ask(recorder, [MESSAGES[0], self.STRAY])
            assert server.generate_requests == [] and server.requests == []
        assert (
            recorder.id_prompts is not None and "an image has no place" in recorder.id_prompts.fault
        )

    def test_the_placeholders_text_after_the_images_is_an_ordinary_token(self) -> None:
        after = {
            "role": "user",
            "content": [RED.part(), {"type": "text", "text": "it says " + PAD_TEXT}],
        }
        with ScriptedChatServer([STEP]) as server:
            recorder, vision = seeing_recorder(server)
            ask(recorder, [MESSAGES[0], after])
        call = recorder.calls[0]
        assert call.problems() == [] and call.prompt_token_ids is not None
        assert call.prompt_token_ids.count(PAD) == 7
        described, problems = vision.describe(
            build_token_record([call.to_dict()])["calls"],
            call.prompt_token_ids + call.token_ids,
            [None],
            None,
        )
        assert described["stray_placeholders"]["count"] == 1
        (position,) = described["stray_placeholders"]["positions"]
        assert position > vision.images[0].start + vision.images[0].length
        # No folder was named, so the image could not be kept.
        assert problems == ["image 0 could not be kept (the run names no folder for its images)"]

    def test_a_reply_part_that_cannot_be_read_marks_the_call(self) -> None:
        call = CallRecord(index=0, streamed=True, request={})
        assert take_generate(call, "no object") is None
        assert call.unreadable is not None and call.aligned is False


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
# generate route, which a run with vision makes before it starts, has something to measure.
# A server whose tokenizer knows its own tokens' texts and names them when asked.
SERVED: dict[str, Any] = {"own_tokens": True, "tokenizer_info": True}
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


class TestWholeRollouts:
    def run(self, tmp_path: Any, **config: Any) -> tuple[dict[str, Any], ScriptedChatServer]:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            record, _box = _run(
                server,
                task=Task(prompt="what is in shot.png?", key="t1"),
                sandbox=_ImageSandbox(),
                token_id_prompts=True,
                **config,
            )
            return record, server

    def test_without_vision_the_model_is_told_it_cannot_see(self, tmp_path: Any) -> None:
        record, server = self.run(tmp_path)
        assert record["trainable"] is True and "vision" not in record
        assert server.generate_requests == []
        assert "does not support vision" in record["tool_log"][0]["result"]
        assert PAD not in rebuild_final(record["tokens"])
        assert {c["route"] for c in record["calls"]} == {"completions"}

    def test_with_vision_the_model_sees_the_image_it_reads(self, tmp_path: Any) -> None:
        folder = str(tmp_path / "images")
        record, server = self.run(tmp_path, vision=True, image_dir=folder)
        assert record["problems"] == []
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["finish_state"] == "completed"
        # The first request holds no image; the two after it do.
        assert [c["route"] for c in record["calls"]] == ["completions", "generate", "generate"]
        assert [len(r["content_parts"]) for r in server.generate_requests[-2:]] == [1, 2]
        seen = record["vision"]
        assert seen["route"]["ok"] is True and seen["placeholder"] == PAD
        assert seen["image_dir"] == folder and seen["stray_placeholders"]["count"] == 0
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
        with ScriptedChatServer(
            parent, agent_script=agent, sampler_logprobs=True, **SERVED
        ) as server:
            record, _box = _run(
                server,
                task=Task(prompt="what is in shot.png?", key="t1"),
                sandbox=_ImageSandbox(),
                token_id_prompts=True,
                vision=True,
                image_dir=folder,
            )
            with_images = [r for r in server.generate_requests if r.get("content_parts")]
        assert record["problems"] == [] and record["trainable"] is True
        assert record["subagents"]["started"] == 1
        # The sub-agent's request with the image went to the generate route, with
        # the pixels; the session's own requests held no image.
        own = [len(r["content_parts"]) for r in with_images[-1:]]
        assert own == [1] and RED.url in json.dumps(with_images[-1])
        assert {c["route"] for c in record["calls"]} == {"completions"}
        assert record["vision"]["images"] == [] and record["vision"]["subagent_images"] == 1
        assert PAD not in rebuild_final(record["tokens"])
        assert not os.path.exists(folder) or os.listdir(folder) == []

    def test_vision_needs_token_id_prompts_and_a_folder(self, tmp_path: Any) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True) as server:
            with pytest.raises(SetupError, match="vision is for prompts as token IDs"):
                _run(server, vision=True, image_dir=str(tmp_path))
            with pytest.raises(SetupError, match="names the folder its images are written to"):
                _run(server, vision=True, token_id_prompts=True)
            assert server.requests == [] and server.generate_requests == []

    def test_a_server_without_the_route_is_refused_before_the_run(self, tmp_path: Any) -> None:
        with (
            ScriptedChatServer(
                LOOK, sampler_logprobs=True, generate_status=403, **SERVED
            ) as server,
            pytest.raises(SetupError, match="--enable-scale-out"),
        ):
            _run(server, vision=True, token_id_prompts=True, image_dir=str(tmp_path))

    def test_a_given_verdict_spares_the_check(self, tmp_path: Any) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            verdict = check_vision_route(
                make_client(server), "scripted", extra_body={"top_k": 20, "top_p": 0.95}
            )
            assert verdict["ok"] is True
            server.forget()
            server.generate_requests.clear()
            record, _box = _run(
                server,
                task=Task(prompt="what is in shot.png?", key="t1"),
                sandbox=_ImageSandbox(),
                token_id_prompts=True,
                vision=True,
                vision_route=verdict,
                image_dir=str(tmp_path / "i"),
            )
            asked = len(server.generate_requests)
        assert record["trainable"] is True and record["vision"]["route"] == verdict
        # Only the run's own two requests with images went to the generate route.
        assert asked == 2

    def test_vision_needs_the_environments_text_read_as_plain_text(self, tmp_path: Any) -> None:
        with (
            ScriptedChatServer(LOOK, sampler_logprobs=True) as server,
            pytest.raises(
                SetupError, match="vision needs the environment's text read as plain text"
            ),
        ):
            _run(server, vision=True, token_id_prompts=True, image_dir=str(tmp_path))

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
                token_id_prompts=True,
                vision=True,
                image_dir=str(tmp_path / "i"),
            )
        assert record["problems"] == [] and record["finish_state"] == "completed"
        (image,) = record["vision"]["images"]
        final = rebuild_final(record["tokens"])
        assert (final[image["start"] - 1], final[image["end"]]) == (IMAGE, IMAGE_END)
        assert record["vision"]["stray_placeholders"]["count"] == 0 and final.count(PAD) == 6
        assert image["source"]["arguments"] == json.dumps({"path": "shot.png"})

    def test_a_server_that_answers_for_another_prompt_ends_the_run_as_a_harness_fault(
        self, tmp_path: Any
    ) -> None:
        with ScriptedChatServer(LOOK, sampler_logprobs=True, **SERVED) as server:
            verdict = check_vision_route(
                make_client(server), "scripted", extra_body={"top_k": 20, "top_p": 0.95}
            )
            assert verdict["ok"] is True
            # From here on the server writes one placeholder token too many.
            server.generate_fault = "other_prompt"
            record, _box = _run(
                server,
                task=Task(prompt="what is in shot.png?", key="t1"),
                sandbox=_ImageSandbox(),
                token_id_prompts=True,
                vision=True,
                vision_route=verdict,
                image_dir=str(tmp_path / "i"),
            )
        assert record["finish_state"] == "harness_fault" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False and "a request with images" in record["error"]
        # Not a status a caller runs again: the one that stops a batch.
        assert cli._status(record) == cli.REFUSED

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
        with ScriptedChatServer(script, sampler_logprobs=True, **SERVED) as server:
            record, _box = _run(
                server,
                task=Task(prompt="look", key="t1"),
                sandbox=_ImageSandbox(),
                token_id_prompts=True,
                vision=True,
                image_dir=str(tmp_path / "i"),
            )
            with_images = [r for r in server.generate_requests if r.get("content_parts")]
        assert record["finish_state"] == "image_token_generated"
        assert record["finish_cause"] == "model" and record["trainable"] is True
        assert record["turns"] == 1 and cli._status(record) == 0
        # The request with the image never went out.
        assert not [r for r in with_images if RED.url in json.dumps(r)]
        assert record["vision"]["images"] == []
        assert record["vision"]["stray_placeholders"]["count"] == 1

    def test_an_image_that_cannot_be_kept_makes_the_record_untrainable(self, tmp_path: Any) -> None:
        blocked = tmp_path / "file"
        blocked.write_text("x")
        record, _server = self.run(tmp_path, vision=True, image_dir=str(blocked / "images"))
        assert record["trainable"] is False
        assert any("could not be kept" in p for p in record["problems"])


class TestTheCommand:
    def test_the_probe_prints_the_check_for_images(self, tmp_path: Any, capsys: Any) -> None:
        out = tmp_path / "server.json"
        with ScriptedChatServer([{"content": "x"}], sampler_logprobs=True) as server:
            status = cli.main(
                [
                    "--base-url",
                    server.base_url,
                    "--model",
                    "m",
                    "--top-k",
                    "20",
                    "--top-p",
                    "0.95",
                    "--token-id-prompts",
                    "--vision",
                    "--probe-only",
                    "--out",
                    str(out),
                ]
            )
            endpoint = server.base_url
        assert status == 0
        verdict = json.loads(out.read_text())
        assert verdict["sampler_logprobs"] is True and verdict["route"] == "completions"
        assert verdict["token_id_route"]["ok"] is True
        assert (
            verdict["vision_route"]["ok"] is True
            and verdict["vision_route"]["endpoint"] == endpoint
        )
        assert json.loads(capsys.readouterr().out) == verdict

    def test_without_the_flag_the_probe_prints_what_it_printed_before(self, tmp_path: Any) -> None:
        out = tmp_path / "server.json"
        with ScriptedChatServer([{"content": "x"}], sampler_logprobs=True) as server:
            status = cli.main(
                [
                    "--base-url",
                    server.base_url,
                    "--model",
                    "m",
                    "--token-id-prompts",
                    "--probe-only",
                    "--out",
                    str(out),
                ]
            )
            asked = len(server.generate_requests)
        assert status == 0 and asked == 0
        assert "vision_route" not in json.loads(out.read_text())

    @pytest.mark.parametrize(
        ("flags", "why"),
        [
            (["--vision"], "--vision is for --token-id-prompts"),
            (["--vision-route", "{}"], "--vision-route is for --vision"),
            (["--image-dir", "x"], "--image-dir is for --vision"),
            (["--token-id-prompts", "--vision"], "--vision needs a place for the rollout's images"),
        ],
    )
    def test_flags_that_do_not_go_together_are_refused(
        self, flags: list[str], why: str, capsys: Any
    ) -> None:
        status = cli.main(
            ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p", *flags]
        )
        assert status == cli.REFUSED and why in capsys.readouterr().err

    def test_a_probe_verdict_with_the_check_for_images_is_taken_by_a_run_without_vision(
        self,
    ) -> None:
        """One file for a batch: what it says of images is none of such a run's."""
        with ScriptedChatServer([{"content": "x"}], sampler_logprobs=True) as server:
            verdict = vision_route_verdict_for(server)
            record, _box = _run(server, sampler_logprobs=verdict)
        assert "vision_route" not in record["logprobs"] and "vision" not in record


def vision_route_verdict_for(server: ScriptedChatServer) -> dict[str, Any]:
    """What ``--probe-only --vision`` prints for *server*, without the token-ID check."""
    from turnstone.rollout.episode import RolloutConfig, logprob_verdict

    config = RolloutConfig(
        base_url=server.base_url, model="scripted", extra_body={"top_k": 20, "top_p": 0.95}
    )
    return {**logprob_verdict(config), "vision_route": vision_route_verdict(config)}
