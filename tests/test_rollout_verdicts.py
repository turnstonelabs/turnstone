"""A verdict about a server, handed to a run in place of asking again: what it
says it was made for, and what a run refuses.

No container.  The scripted chat server answers the probe and the check; where
a rollout runs, it runs in the directory stand-in of ``test_rollout_episode.py``
with that file's script of harmless commands.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import openai
import pytest

from tests._scripted_chat_server import ScriptedChatServer, encode
from tests.test_rollout_episode import SAMPLED, SCRIPT, _DirSandbox, _run
from turnstone.core import config as config_module
from turnstone.rollout import cli
from turnstone.rollout.episode import RolloutConfig, logprob_verdict, token_id_route_verdict
from turnstone.rollout.idprompt import TokenIdRoute, check_token_id_route
from turnstone.rollout.probe import sampler_logprobs_reported
from turnstone.rollout.settings import SetupError
from turnstone.rollout.verdicts import check_verdict, probe_verdict

HERE = "http://127.0.0.1:8000/v1"
REFLECTS = {"temperature": True, "top_k": True, "top_p": True}
# The token of each marker of the reply format, as a check finds them.
MARKERS = {"reasoning_open": 3, "reasoning_close": 4, "call_open": 5, "call_close": 6}
# A probe's verdict and a check's, as far as a run looks at them.
PROBED = {
    "sampler_logprobs": True,
    "reflects": REFLECTS,
    "route": "chat",
    "model": "m",
    "endpoint": HERE,
    "checked_at": "2026-11-11T12:00:00Z",
    "reason": "a token the sampler had to pick is reported with log-probability 0",
    "tried": [],
    "positions": {},
}
CHECKED = {
    "ok": True,
    "reason": None,
    "format": "parameter_blocks",
    "template_arguments": {"reasoning_effort": "low"},
    "model": "m",
    "endpoint": HERE,
    "checked_at": "2026-11-11T12:00:00Z",
    "checks": {},
    "marker_ids": MARKERS,
    "text_markers": [],
    "closing": [2, 1010],
    "generation_prompt": [1, 1097, 3, 1010],
    "in_reasoning": True,
}


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


def _client(server: ScriptedChatServer) -> openai.OpenAI:
    return openai.OpenAI(base_url=server.base_url, api_key="none", max_retries=0)


def _without(verdict: dict[str, Any], *names: str) -> dict[str, Any]:
    return {key: value for key, value in verdict.items() if key not in names}


def _refused(call: Any) -> str:
    with pytest.raises(SetupError) as refusal:
        call()
    return str(refusal.value)


class TestWhatAVerdictNames:
    def test_a_probe_says_what_it_was_made_for(self):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            verdict = sampler_logprobs_reported(_client(server), "scripted")
            here = server.base_url
        assert verdict["model"] == "scripted" and verdict["endpoint"] == here
        assert verdict["route"] == "chat" and verdict["sampler_logprobs"] is True
        made = datetime.fromisoformat(verdict["checked_at"])
        assert made.tzinfo is not None and verdict["checked_at"].endswith("Z")
        assert json.loads(json.dumps(verdict)) == verdict

    def test_a_check_says_what_it_was_made_for(self):
        with ScriptedChatServer(SCRIPT) as server:
            verdict = check_token_id_route(_client(server), "scripted", reasoning_effort="low")
            here = server.base_url
        assert verdict["ok"] is True
        assert verdict["model"] == "scripted" and verdict["endpoint"] == here
        assert datetime.fromisoformat(verdict["checked_at"]).tzinfo is not None
        assert verdict["format"] == "parameter_blocks"
        assert verdict["template_arguments"] == {"reasoning_effort": "low", "enable_thinking": True}

    def test_a_check_and_a_probe_that_could_not_be_made_say_it_too(self):
        nobody = RolloutConfig(base_url="http://127.0.0.1:9/v1", model="m", token_id_prompts=True)
        failed, unchecked = logprob_verdict(nobody), token_id_route_verdict(nobody)
        for verdict in (failed, unchecked):
            assert verdict["model"] == "m" and verdict["endpoint"] == "http://127.0.0.1:9/v1"
            assert datetime.fromisoformat(verdict["checked_at"]).tzinfo is not None
        assert failed["sampler_logprobs"] is None and failed["route"] == "completions"
        assert unchecked["ok"] is False
        # The reason holds the error's own words, not only its kind.
        kind, _, words = failed["reason"].removeprefix("the probe failed: ").partition(": ")
        assert failed["reason"].startswith("the probe failed: ") and kind and words

    def test_the_command_alone_writes_them(self, tmp_path, capsys):
        out = tmp_path / "server.json"
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            flags = ["--base-url", server.base_url, "--model", "scripted", "--probe-only"]
            assert cli.main([*flags, "--token-id-prompts", "--out", str(out)]) == 0
            here = server.base_url
        written = json.loads(out.read_text(encoding="ascii"))
        assert written == json.loads(capsys.readouterr().out)
        for verdict in (written, written["token_id_route"]):
            assert verdict["model"] == "scripted" and verdict["endpoint"] == here
            assert datetime.fromisoformat(verdict["checked_at"]).tzinfo is not None
        assert written["route"] == "completions"


class TestAVerdictOfTheProbe:
    """What :func:`probe_verdict` takes for a run that asks ``m`` at ``HERE``."""

    def _for_this_run(self, given: Any, route: str = "chat") -> dict[str, Any]:
        return probe_verdict(given, model="m", endpoint=HERE, route=route)

    def test_one_made_for_this_run_comes_back_as_it_was_given(self):
        assert self._for_this_run(PROBED) == PROBED

    def test_a_check_printed_into_the_same_object_is_left_out(self):
        """``--probe-only --token-id-prompts`` prints both into one object."""
        assert self._for_this_run({**PROBED, "token_id_route": CHECKED}) == PROBED

    @pytest.mark.parametrize(
        "spelled",
        [HERE + "/", "HTTP://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1//"],
    )
    def test_another_spelling_of_the_same_address_is_the_same(self, spelled):
        assert self._for_this_run({**PROBED, "endpoint": spelled})["endpoint"] == spelled

    def test_a_port_the_scheme_has_by_itself_is_the_same_too(self):
        given = {**PROBED, "endpoint": "http://models.example/v1"}
        taken = probe_verdict(
            given, model="m", endpoint="http://models.example:80/v1", route="chat"
        )
        assert taken == given

    @pytest.mark.parametrize(
        ("change", "said"),
        [
            ({"model": "another"}, "was made for the model 'another', and this run asks 'm'"),
            (
                {"endpoint": "http://127.0.0.1:8001/v1"},
                "was made at http://127.0.0.1:8001/v1, and this run's requests go to " + HERE,
            ),
            # Another name for the same machine is another address: nothing is looked up.
            ({"endpoint": "http://localhost:8000/v1"}, "was made at http://localhost:8000/v1"),
            ({"endpoint": "http://127.0.0.1:8000/v2"}, "was made at http://127.0.0.1:8000/v2"),
            ({"route": "completions"}, "was probed on the completions route"),
            ({"route": "both"}, "names a route there is none of"),
            ({"model": 5}, "names no model"),
            ({"checked_at": "yesterday"}, "does not say when it was made"),
            ({"checked_at": "2026-11-11T12:00:00"}, "does not say when it was made"),
            ({"sampler_logprobs": False}, "contradicts itself"),
            ({"reflects": {"temperature": True}}, "is not what a probe returns"),
            ({"reflects": {**REFLECTS, "top_k": 1}}, "is not what a probe returns"),
            ({"reflects": {**REFLECTS, "min_p": True}}, "is not what a probe returns"),
        ],
    )
    def test_one_made_for_something_else_is_refused(self, change, said):
        assert said in _refused(lambda: self._for_this_run({**PROBED, **change}))

    @pytest.mark.parametrize("name", ["model", "endpoint", "route", "checked_at"])
    def test_one_that_does_not_say_is_told_to_be_made_again(self, name):
        said = _refused(lambda: self._for_this_run(_without(PROBED, name)))
        assert f"does not say what it was made for ({name} missing)" in said
        assert said.endswith("probe again")

    def test_a_verdict_of_before_it_named_anything_is_told_to_be_made_again(self):
        old = {"sampler_logprobs": True, "reflects": REFLECTS, "route": "chat", "reason": "x"}
        said = _refused(lambda: self._for_this_run(old))
        assert "(model, endpoint, checked_at missing)" in said

    @pytest.mark.parametrize("given", [REFLECTS, {}, [], "yes", None, 1])
    def test_what_is_no_verdict_is_refused_with_a_sentence(self, given):
        """Its ``reflects`` alone used to be taken, for any server."""
        assert "is not what a probe returns" in _refused(lambda: self._for_this_run(given))

    def test_the_other_route_is_named_before_anything_else(self):
        said = _refused(
            lambda: self._for_this_run({"route": "chat", "reflects": REFLECTS}, "completions")
        )
        assert "was probed on the chat route" in said and "with prompts as token IDs" in said


class TestAVerdictOfTheCheck:
    """What :func:`check_verdict` and ``TokenIdRoute.from_verdict`` take."""

    def _for_this_run(self, given: Any, **other: Any) -> dict[str, Any]:
        run = {
            "model": "m",
            "endpoint": HERE,
            "fmt": "parameter_blocks",
            "arguments": {"reasoning_effort": "low"},
        }
        return check_verdict(given, **{**run, **other})

    def test_one_made_for_this_run_comes_back_as_it_was_given(self):
        assert self._for_this_run(CHECKED) == CHECKED
        route = TokenIdRoute.from_verdict(CHECKED)
        assert route.closing == (2, 1010) and route.in_reasoning is True

    @pytest.mark.parametrize(
        ("change", "said"),
        [
            ({"model": "another"}, "was made for the model 'another'"),
            ({"endpoint": "http://127.0.0.1:8001/v1"}, "was made at http://127.0.0.1:8001/v1"),
            ({"format": "other"}, 'is for the reply format "other"'),
            ({"template_arguments": {"reasoning_effort": "high"}}, "with the template arguments"),
            ({"template_arguments": {}}, "with the template arguments {}"),
            ({"ok": "yes"}, "is not what a check returns"),
            ({"ok": False, "reason": "no tokenize route"}, "cannot be sent as token IDs: no token"),
        ],
    )
    def test_one_made_for_something_else_is_refused(self, change, said):
        assert said in _refused(lambda: self._for_this_run({**CHECKED, **change}))

    @pytest.mark.parametrize(
        "name", ["model", "endpoint", "checked_at", "format", "template_arguments"]
    )
    def test_one_that_does_not_say_is_told_to_be_made_again(self, name):
        """Without its template arguments a verdict used to pass for any."""
        said = _refused(lambda: self._for_this_run(_without(CHECKED, name)))
        assert f"does not say what it was made for ({name} missing)" in said
        assert said.endswith("check the server again for this run")

    @pytest.mark.parametrize("given", [{}, [], "ok", None, {"ok": 1}])
    def test_what_is_no_verdict_is_refused_with_a_sentence(self, given):
        assert "is not what a check returns" in _refused(lambda: self._for_this_run(given))

    @pytest.mark.parametrize(
        ("change", "said"),
        [
            ({"marker_ids": None}, "marker_ids must give a token ID"),
            ({"marker_ids": [3, 4]}, "marker_ids must give a token ID"),
            ({"marker_ids": {"reasoning_open": "3"}}, "marker_ids must give a token ID"),
            ({"marker_ids": {"reasoning_open": True}}, "marker_ids must give a token ID"),
            ({"text_markers": None}, "text_markers must list"),
            ({"text_markers": "call_open"}, "text_markers must list"),
            ({"text_markers": [5]}, "text_markers must list"),
            ({"closing": None}, "closing must be the token IDs"),
            ({"closing": []}, "closing must be the token IDs"),
            ({"closing": [2.5]}, "closing must be the token IDs"),
            ({"closing": "2"}, "closing must be the token IDs"),
            ({"generation_prompt": None}, "generation_prompt must be the token IDs"),
            ({"generation_prompt": [True]}, "generation_prompt must be the token IDs"),
            ({"in_reasoning": None}, "in_reasoning must be true or false"),
            ({"in_reasoning": "yes"}, "in_reasoning must be true or false"),
            ({"format": "another"}, "format names no reply format"),
            ({"format": None}, "format names no reply format"),
            # Where a reply begins is read off the generation prompt.
            ({"in_reasoning": False}, "in_reasoning contradicts the generation prompt"),
            ({"generation_prompt": [1, 1097, 1010]}, "in_reasoning contradicts"),
            # Every marker of the format is one token or spelled out, and
            # nothing else is named.
            (
                {"marker_ids": {**MARKERS, "call_end": 7}},
                "names what is no marker of the format parameter_blocks: call_end",
            ),
            ({"text_markers": ["reasoning"]}, "names what is no marker of the format"),
            ({"text_markers": ["call_open"]}, "in both marker_ids and text_markers: call_open"),
        ],
    )
    def test_one_that_does_not_hold_what_a_passed_check_found_is_refused(self, change, said):
        """A key that is missing or of the wrong kind used to be a traceback,
        or was found out in the middle of a run."""
        verdict = {**CHECKED, **change}
        verdict = _without(verdict, *[name for name, value in change.items() if value is None])
        refusal = _refused(lambda: TokenIdRoute.from_verdict(verdict))
        assert said in refusal and refusal.endswith("check the server again")

    @pytest.mark.parametrize("marker", sorted(MARKERS))
    def test_one_with_a_marker_missing_is_refused_and_told_which(self, marker):
        """Without the token of the marker that ends the reasoning every
        reply would be read as reasoning alone, with no call run and nothing
        noted.  The marker has to be in one of the two lists."""
        fewer = {name: token for name, token in MARKERS.items() if name != marker}
        refusal = _refused(lambda: TokenIdRoute.from_verdict({**CHECKED, "marker_ids": fewer}))
        assert f"is in neither marker_ids nor text_markers: {marker}): check" in refusal
        # Spelled out instead of one token, it is there.  (The generation
        # prompt holds the token that opens the reasoning, so that marker
        # cannot be the one spelled out in this verdict.)
        if marker != "reasoning_open":
            spelled = {**CHECKED, "marker_ids": fewer, "text_markers": [marker]}
            route = TokenIdRoute.from_verdict(spelled)
            assert route.text_markers == (marker,) and marker not in route.marker_ids

    def test_several_missing_markers_are_all_named(self):
        refusal = _refused(lambda: TokenIdRoute.from_verdict({**CHECKED, "marker_ids": {}}))
        assert "reasoning_open, reasoning_close, call_open, call_close" in refusal

    def test_one_that_says_the_server_cannot_names_no_route(self):
        with pytest.raises(ValueError, match="cannot be sent token-ID prompts: no route"):
            TokenIdRoute.from_verdict({"ok": False, "reason": "no route"})


def _probed(server: ScriptedChatServer, **settings: Any) -> dict[str, Any]:
    """The verdict of a probe of *server*, made for the runs below."""
    config = RolloutConfig(
        base_url=server.base_url, model="scripted", extra_body=SAMPLED["extra_body"], **settings
    )
    return logprob_verdict(config)


def _checked(server: ScriptedChatServer) -> dict[str, Any]:
    """The verdict of a check of *server*, made for the runs below."""
    config = RolloutConfig(
        base_url=server.base_url, model="scripted", extra_body=SAMPLED["extra_body"]
    )
    verdict = token_id_route_verdict(config)
    assert verdict["ok"] is True, verdict["reason"]
    return verdict


class TestARunGivenAVerdict:
    def test_the_whole_verdict_of_a_probe_is_taken_and_kept_as_it_was_given(self):
        """Read as the answers for each setting, the whole verdict used to give
        a record that says nothing is known about the server."""
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            verdict = _probed(server)
            asked = len(server.requests)
            record, _box = _run(server, sampler_logprobs=verdict)
            # The session's three calls and no probe.
            assert len(server.requests) == asked + 3
        assert record["trainable"] is True and record["problems"] == []
        assert record["logprobs"] == verdict
        assert record["logprobs"]["model"] == "scripted" and "tried" in record["logprobs"]

    def test_a_verdict_that_says_the_models_own_makes_the_record_say_so(self):
        with ScriptedChatServer(SCRIPT) as server:
            verdict = _probed(server)
            record, _box = _run(server, sampler_logprobs=verdict)
        assert verdict["sampler_logprobs"] is False and record["logprobs"] == verdict
        assert record["trainable"] is False
        assert "the server reports the model's own log-probabilities" in record["problems"][0]

    @pytest.mark.parametrize(
        ("change", "said"),
        [
            ({"model": "another"}, "was made for the model 'another'"),
            ({"endpoint": "http://127.0.0.1:9/v1"}, "was made at http://127.0.0.1:9/v1"),
            ({"route": "completions"}, "was probed on the completions route"),
            ({"model": None}, "does not say what it was made for (model missing)"),
            ({"checked_at": None}, "does not say what it was made for (checked_at missing)"),
        ],
    )
    def test_one_that_is_not_this_runs_is_refused_and_nothing_is_run(self, change, said):
        box = _DirSandbox()
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            verdict = {**_probed(server), **change}
            asked = len(server.requests)
            with pytest.raises(SetupError) as refusal:
                _run(server, sandbox=box, sampler_logprobs=verdict)
            assert len(server.requests) == asked
        assert said in str(refusal.value) and box.root == ""

    def test_its_reflects_alone_is_refused(self):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            with pytest.raises(SetupError, match="is not what a probe returns"):
                _run(server, sampler_logprobs=_probed(server)["reflects"])
            with pytest.raises(SetupError, match="takes the verdict of a probe, True, False"):
                _run(server, sampler_logprobs="yes")

    def test_a_verdict_made_at_another_server_is_refused(self):
        with (
            ScriptedChatServer(SCRIPT, sampler_logprobs=True) as elsewhere,
            ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server,
        ):
            verdict, route = _probed(elsewhere), _checked(elsewhere)
            with pytest.raises(SetupError, match="verdict of the probe was made at"):
                _run(server, sampler_logprobs=verdict)
            with pytest.raises(SetupError, match="check of the server was made at"):
                _run(server, token_id_prompts=True, token_id_route=route)
            assert server.requests == [] and server.tokenize_requests == []

    def test_a_probe_of_the_other_route_is_refused_before_the_routes_are_checked(self):
        """The check of the routes makes model calls; none is made for a run
        that is going to be refused."""
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            verdict = _probed(server)
            asked = len(server.requests)
            with pytest.raises(SetupError, match="was probed on the chat route"):
                _run(server, token_id_prompts=True, sampler_logprobs=verdict)
            assert len(server.requests) == asked and server.tokenize_requests == []

    def test_both_verdicts_are_kept_as_they_were_given(self):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            route = _checked(server)
            verdict = _probed(server, token_id_prompts=True)
            asked, rendered = len(server.requests), len(server.tokenize_requests)
            record, _box = _run(
                server, token_id_prompts=True, token_id_route=route, sampler_logprobs=verdict
            )
            assert len(server.requests) == asked + 3
            assert len(server.tokenize_requests) == rendered + 1 + 2 + 2
        assert record["one_sequence"] is True
        assert record["token_id_route"] == route and record["logprobs"] == verdict
        assert record["logprobs"]["route"] == "completions"

    @pytest.mark.parametrize(
        ("change", "said"),
        [
            ({"model": "another"}, "was made for the model 'another'"),
            ({"template_arguments": None}, "(template_arguments missing)"),
            ({"template_arguments": {"enable_thinking": False}}, "with the template arguments"),
            ({"marker_ids": None}, "marker_ids must give a token ID"),
            ({"closing": []}, "closing must be the token IDs"),
            ({"in_reasoning": False}, "in_reasoning contradicts the generation prompt"),
        ],
    )
    def test_a_check_that_is_not_this_runs_is_refused_and_nothing_is_run(self, change, said):
        box = _DirSandbox()
        with ScriptedChatServer(SCRIPT) as server:
            route = {**_checked(server), **change}
            route = _without(route, *[name for name, value in change.items() if value is None])
            asked, rendered = len(server.requests), len(server.tokenize_requests)
            with pytest.raises(SetupError) as refusal:
                _run(server, sandbox=box, token_id_prompts=True, token_id_route=route)
            assert len(server.requests) == asked and len(server.tokenize_requests) == rendered
        assert said in str(refusal.value) and box.root == ""

    def test_a_first_prompt_that_does_not_end_as_the_check_found_ends_the_run_there(self):
        """A check made for another template: its generation prompt is another.
        The run's first prompt is held against it, and the run ends before its
        first model call instead of reading every reply from the wrong start."""
        with ScriptedChatServer(SCRIPT) as server:
            route = _checked(server)
            other = {**route, "generation_prompt": [*route["generation_prompt"], *encode("!")]}
            asked = len(server.requests)
            record, _box = _run(server, token_id_prompts=True, token_id_route=other)
            # The first request was rendered and never sent.
            assert len(server.requests) == asked
        assert record["finish_state"] == "error" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False and record["turns"] == 0
        assert "PromptAssemblyError" in record["error"]
        assert (
            "the first prompt does not end with the generation prompt found before"
            in (record["error"])
        )
        assert "made for another template" in record["error"]
        assert record["token_id_route"] == other


def _never(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("nothing may be run")


class TestTheCommand:
    BASE = ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]

    def _made(self, server: ScriptedChatServer, tmp_path: Any, *more: str) -> dict[str, Any]:
        """What ``--probe-only --token-id-prompts`` writes for *server*."""
        out = tmp_path / "server.json"
        flags = ["--base-url", server.base_url, "--model", "scripted", *self.BASE, *more]
        assert cli.main([*flags, "--token-id-prompts", "--probe-only", "--out", str(out)]) == 0
        written: dict[str, Any] = json.loads(out.read_text(encoding="ascii"))
        return written

    def test_a_rollout_given_both_verdicts_keeps_them_as_they_were_given(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        box = _DirSandbox()
        real = cli.run_episode
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            given = self._made(server, tmp_path)
            asked = len(server.requests)
            monkeypatch.setattr(
                cli,
                "run_episode",
                lambda t, c: real(
                    t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True
                ),
            )
            verdicts = f"@{tmp_path / 'server.json'}"
            status = cli.main(
                ["--base-url", server.base_url, "--model", "scripted", *self.BASE]
                + ["--token-id-prompts", "--prompt", "go", "--out", str(out)]
                + ["--sampler-logprobs", verdicts, "--token-id-route", verdicts]
            )
            assert len(server.requests) == asked + 3
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["token_id_route"] == given["token_id_route"]
        assert record["logprobs"] == _without(given, "token_id_route")
        assert record["logprobs"]["model"] == record["model"] == "scripted"
        assert record["logprobs"]["endpoint"] == record["endpoint"]
        capsys.readouterr()

    @pytest.mark.parametrize(
        ("flag", "change", "said"),
        [
            (
                "--sampler-logprobs",
                lambda v: {**v, "model": "another"},
                "--sampler-logprobs: the given verdict of the probe was made for the model",
            ),
            (
                "--sampler-logprobs",
                lambda v: {**v, "endpoint": "http://127.0.0.1:9/v1"},
                "--sampler-logprobs: the given verdict of the probe was made at",
            ),
            (
                "--sampler-logprobs",
                lambda v: _without(v, "model", "endpoint", "checked_at"),
                "(model, endpoint, checked_at missing), so nothing shows that it is this run's: "
                "probe again",
            ),
            (
                "--sampler-logprobs",
                lambda v: v["reflects"],
                "--sampler-logprobs: the given verdict of the probe is not what a probe returns",
            ),
            (
                "--sampler-logprobs",
                lambda v: {**v, "route": "chat"},
                "was probed on the chat route",
            ),
            (
                "--token-id-route",
                lambda v: {**v["token_id_route"], "model": "another"},
                "--token-id-route: the given check of the server was made for the model",
            ),
            (
                "--token-id-route",
                lambda v: _without(v["token_id_route"], "template_arguments"),
                "(template_arguments missing)",
            ),
            (
                "--token-id-route",
                lambda v: {"ok": True, "format": "parameter_blocks"},
                "--token-id-route: the given check of the server does not say what it was made "
                "for (model, endpoint, checked_at, template_arguments missing)",
            ),
            (
                "--token-id-route",
                lambda v: _without(v["token_id_route"], "marker_ids"),
                "--token-id-route: the given check of the server does not hold what a passed "
                "check found (marker_ids must give a token ID",
            ),
            (
                "--token-id-route",
                lambda v: {**v["token_id_route"], "closing": "2"},
                "(closing must be the token IDs the template writes, at least one)",
            ),
        ],
    )
    def test_a_verdict_that_is_not_this_runs_is_refused_with_a_sentence(
        self, monkeypatch, tmp_path, capsys, flag, change, said
    ):
        """Status 3, nothing run and no traceback, whatever is wrong with it."""
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            given = self._made(server, tmp_path)
            capsys.readouterr()
            asked, rendered = len(server.requests), len(server.tokenize_requests)
            monkeypatch.setattr(cli, "run_episode", _never)
            flags = ["--base-url", server.base_url, "--model", "scripted", *self.BASE]
            flags += ["--token-id-prompts", "--prompt", "go", flag, json.dumps(change(given))]
            assert cli.main(flags) == 3
            assert len(server.requests) == asked and len(server.tokenize_requests) == rendered
        assert said in capsys.readouterr().err

    def test_a_check_made_with_another_reasoning_effort_is_refused(
        self, monkeypatch, tmp_path, capsys
    ):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            given = self._made(server, tmp_path, "--effort", "low")
            capsys.readouterr()
            monkeypatch.setattr(cli, "run_episode", _never)
            flags = ["--base-url", server.base_url, "--model", "scripted", *self.BASE]
            flags += ["--token-id-prompts", "--prompt", "go", "--sampler-logprobs", "yes"]
            flags += ["--token-id-route", json.dumps(given), "--effort", "high"]
            assert cli.main(flags) == 3
        said = capsys.readouterr().err
        assert "--token-id-route: the given check of the server was made with the template" in said
        assert '"reasoning_effort": "low"' in said and '"reasoning_effort": "high"' in said
