"""The server's version, asked before a rollout's first model call and after its last.

No container: the scripted chat server answers the version question from a list,
and notes for each question how many chat requests had arrived before it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tests import test_rollout_episode as episode_tests
from tests._scripted_chat_server import ScriptedChatServer, VersionFault
from tests.test_rollout_episode import SCRIPT, _DirSandbox, _run
from turnstone.core import config as config_module
from turnstone.rollout import cli, server_version
from turnstone.rollout.episode import Task
from turnstone.rollout.server_version import ServerVersion, ask
from turnstone.rollout.settings import SetupError


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


@pytest.fixture(autouse=True)
def _no_pause_between_tries(monkeypatch):
    monkeypatch.setattr(server_version, "_PAUSE_SECONDS", 0.0)


def _server(*answers: Any, **options: Any) -> ScriptedChatServer:
    return ScriptedChatServer(
        options.pop("script", SCRIPT), version_answers=list(answers), **options
    )


class TestAsking:
    def test_the_whole_answer_is_the_version(self):
        with _server({"revision": 7, "loaded": "today"}) as server:
            assert ask(server.version_url) == ({"revision": 7, "loaded": "today"}, None)
            assert len(server.version_requests) == 1

    def test_or_one_named_field_of_it(self):
        with _server({"revision": 7, "loaded": "today"}) as server:
            assert ask(server.version_url, "revision") == (7, None)

    def test_an_answer_without_the_field_is_no_version(self):
        with _server({"version": 7}, 7) as server:
            version, why = ask(server.version_url, "revision")
            assert version is None
            assert why == "the answer is no JSON object with the field 'revision'"
            # Asked once: the same question gets the same answer.
            assert len(server.version_requests) == 1
            assert ask(server.version_url, "revision")[1] == why

    def test_null_is_no_version(self):
        with _server({"revision": None}, None) as server:
            assert ask(server.version_url, "revision")[1] == ("the answer names no version (null)")
            assert ask(server.version_url) == (None, "the answer names no version (null)")

    def test_a_server_without_the_address_is_not_asked_again(self):
        with ScriptedChatServer(SCRIPT) as server:
            assert ask(server.version_url) == (None, "the server answered with status 404")
            assert len(server.version_requests) == 1

    def test_a_server_that_is_busy_is_asked_again(self):
        with _server(VersionFault(503), VersionFault(503), 7) as server:
            assert ask(server.version_url) == (7, None)
            assert len(server.version_requests) == 3

    def test_but_not_for_ever(self):
        with _server(VersionFault(503)) as server:
            assert ask(server.version_url) == (None, "the server answered with status 503")
            assert len(server.version_requests) == 3

    def test_an_answer_that_is_not_json(self):
        with _server(VersionFault(200, "seven")) as server:
            assert ask(server.version_url) == (None, "the answer is not JSON")
        # What the standard reader takes beside JSON is none either: it could
        # not be written into a record.
        for almost in ("NaN", '{"revision": Infinity}', "1e999"):
            with _server(VersionFault(200, almost)) as server:
                assert ask(server.version_url) == (None, "the answer is not JSON")

    def test_an_answer_too_large_to_be_a_version_is_not_read_to_its_end(self):
        with _server("x" * 200_000) as server:
            assert ask(server.version_url) == (None, "the answer is larger than 65536 bytes")

    def test_a_server_that_is_not_there(self):
        with ScriptedChatServer(SCRIPT) as server:
            url = server.version_url
        version, why = ask(url)
        assert version is None and why is not None and "Connect" in why

    def test_the_key_goes_with_the_question_only_to_the_models_own_server(self):
        with _server(7) as server:
            ServerVersion(server.version_url, base_url=server.base_url, api_key="k1").ask_first()
            ServerVersion(server.version_url, base_url=server.base_url, api_key="none").ask_first()
            elsewhere = "http://models.invalid/v1"
            ServerVersion(server.version_url, base_url=elsewhere, api_key="k1").ask_first()
            sent = [asked["authorization"] for asked in server.version_requests]
        assert sent == ["Bearer k1", None, None]

    def test_an_address_that_is_none(self):
        for url in ("version", "file:///etc/hostname", "http://"):
            with pytest.raises(SetupError, match="asked over HTTP"):
                ServerVersion(url)

    @pytest.mark.parametrize(
        "url",
        ["http://127.0.0.1:99999/version", "http://127.0.0.1:port/version", "http://[::1/version"],
    )
    def test_a_port_or_a_host_that_is_none_is_refused_with_a_sentence(self, url):
        """A port out of range used to be a traceback, and only where a key
        was to go with the question."""
        for key in ("none", "k1"):
            with pytest.raises(SetupError, match="asked over HTTP") as refusal:
                ServerVersion(url, base_url="http://127.0.0.1:8000/v1", api_key=key)
            assert url in str(refusal.value)

    def test_a_model_address_that_is_none_gets_no_key(self):
        with _server(7) as server:
            asked = ServerVersion(
                server.version_url, base_url="http://127.0.0.1:99999/v1", api_key="k"
            )
            assert asked.ask_first() is None
            assert [a["authorization"] for a in server.version_requests] == [None]


class _Dripping:
    """A server that sends its headers at once and then one byte of *answer*
    every *every* seconds; once the answer is out it goes on with spaces until
    *lasting* seconds have passed, where *lasting* is given: an answer that
    keeps coming and does not end."""

    def __init__(self, answer: bytes, *, every: float, lasting: float = 0.0) -> None:
        owner = self
        self.asked = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                owner.asked += 1
                started = time.monotonic()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for byte in answer:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(every)
                    while time.monotonic() - started < lasting:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(every)
                except OSError:
                    pass  # the asker went away

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Dripping:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/version"


class TestTheTimeAQuestionHas:
    """The limit is for the whole question, not for each read of its answer."""

    def test_an_answer_that_keeps_coming_is_given_up_when_the_time_has_passed(self, monkeypatch):
        """Every read came well within the limit, so the question stayed open
        for as long as the server went on."""
        monkeypatch.setattr(server_version, "_TIMEOUT_SECONDS", 0.5)
        with _Dripping(b"7", every=0.1, lasting=12.0) as server:
            started = time.monotonic()
            version, why = ask(server.url)
            took = time.monotonic() - started
            assert server.asked == 3
        assert version is None and why == "no whole answer within 0.5 s"
        # Three tries of half a second each, not the twelve seconds the server
        # would have gone on.
        assert took < 3.0

    def test_an_answer_that_comes_slowly_and_ends_in_time_is_read(self, monkeypatch):
        monkeypatch.setattr(server_version, "_TIMEOUT_SECONDS", 3.0)
        with _Dripping(b'{"revision": 7}', every=0.02) as server:
            assert ask(server.url, "revision") == (7, None)
            assert server.asked == 1


def _versioned(server: ScriptedChatServer, **options: Any) -> tuple[dict[str, Any], _DirSandbox]:
    return _run(server, server_version_url=server.version_url, **options)


class TestAroundARollout:
    def test_the_same_version_at_both_ends(self):
        with _server({"revision": 7}) as server:
            record, _box = _versioned(server, server_version_field="revision")
            asked = list(server.version_requests)
            requests = len(server.requests)

        said = record["server_version"]
        assert said["first"] == 7 and said["last"] == 7 and said["same"] is True
        assert said["url"] == server_url(asked, said) and said["field"] == "revision"
        assert said["first_error"] is None and said["last_error"] is None
        assert said["first_at"] <= said["last_at"]
        assert record["trainable"] is True and record["problems"] == []
        # Asked before the first model call went out and after the last had returned.
        assert [a["chat_requests_before"] for a in asked] == [0, requests] and requests == 3

    def test_the_probe_and_the_sub_agents_are_inside_the_two_questions(self):
        delegating = episode_tests.TestSubAgents
        parent, agent = delegating.PARENT, delegating.AGENT
        with _server(7, script=parent, agent_script=agent, sampler_logprobs=True) as server:
            record, _box = _versioned(server, sampler_logprobs=None)
            asked = list(server.version_requests)
            probes = len([r for r in server.requests if r.get("top_logprobs")])
            everything = len(server.requests)

        assert probes == 5 and record["subagents"]["model_calls"] == 2
        assert everything == probes + 3 + 2
        assert [a["chat_requests_before"] for a in asked] == [0, everything]
        assert record["server_version"]["same"] is True and record["trainable"] is True

    def test_with_prompts_as_token_ids_the_check_of_the_routes_is_inside_them_too(self):
        """The check of the two routes and the probe on the completions route make
        requests of their own: the first question is put before any of them."""
        with _server(7, sampler_logprobs=True) as server:
            record, _box = _versioned(server, token_id_prompts=True, sampler_logprobs=None)
            first, last = server.version_requests
            everything = len(server.requests)
            rendered = len(server.tokenize_requests)

        assert record["prompt_mode"] == "token_ids" and record["token_id_route"]["ok"] is True
        assert record["logprobs"]["route"] == "completions"
        # More went out than the session's three calls: the check and the probe.
        assert everything > 3 and rendered > 0
        assert first["chat_requests_before"] == 0 and first["tokenize_requests_before"] == 0
        assert last["chat_requests_before"] == everything
        assert last["tokenize_requests_before"] == rendered
        assert record["server_version"]["same"] is True and record["trainable"] is True

    def test_a_version_that_changed_is_not_trained_on_and_the_ending_stays(self):
        with _server(7, 8) as server:
            record, _box = _versioned(server)

        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["turns"] == 3 and record["prefix_ok"] is True
        assert record["trainable"] is False and record["one_sequence"] is False
        assert record["problems"] == [
            "the server's version changed during the run: 7 before the first model call, "
            "8 after the last"
        ]
        said = record["server_version"]
        assert (said["first"], said["last"], said["same"]) == (7, 8, False)

    def test_a_last_answer_that_cannot_be_had_is_not_trained_on_either(self):
        with _server(7, VersionFault(503)) as server:
            record, _box = _versioned(server)
            asked = len(server.version_requests)

        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is False
        assert record["problems"] == [
            "the server's version could not be asked after the run (the server answered "
            "with status 503), so it is not known whether it changed during the run"
        ]
        said = record["server_version"]
        assert said["first"] == 7 and said["last"] is None and said["same"] is None
        assert said["last_error"] == "the server answered with status 503"
        assert asked == 1 + 3  # the last question was put three times

    def test_without_a_first_answer_the_run_does_not_start(self):
        """Nothing is asked of the model, the probe included, and no sandbox is started."""
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            record, box = _versioned(server, sampler_logprobs=None)
            assert server.requests == [] and len(server.version_requests) == 1

        assert record["finish_state"] == "server_version_unknown"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        assert record["error"] == "the server answered with status 404"
        assert (
            "the server's version could not be asked before the run, so the run did not "
            "start: the server answered with status 404"
        ) in record["problems"]
        said = record["server_version"]
        assert said["first"] is None and said["last"] is None and said["same"] is None
        assert said["first_error"] == record["error"] and said["last_error"] is None
        assert record["logprobs"]["reason"] == "not probed: the run did not start"
        assert record["turns"] == 0 and record["verification"] is None
        assert box.root == "" and box.closed == 1

    def test_without_a_first_answer_the_routes_are_not_checked_either(self):
        """With prompts as token IDs the check before the run asks the server too:
        it is not made, and the record says which mode the run was to use."""
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            record, box = _versioned(server, token_id_prompts=True, sampler_logprobs=None)
            assert server.requests == [] and server.tokenize_requests == []
            assert len(server.version_requests) == 1

        assert record["finish_state"] == "server_version_unknown"
        assert record["prompt_mode"] == "token_ids"
        assert record["token_id_route"] == {
            "ok": None,
            "reason": "not checked: the run did not start",
        }
        assert record["logprobs"]["reason"] == "not probed: the run did not start"
        assert record["turns"] == 0 and box.root == "" and box.closed == 1

    def test_a_run_the_machinery_ended_is_still_asked_about(self):
        failing = dict.fromkeys(range(1, 40), "http_500")
        with _server(7, faults=failing) as server:
            record, _box = _versioned(server)
            asked = len(server.version_requests)
        assert record["finish_state"] == "error" and asked == 2
        assert record["server_version"]["same"] is True

    def test_a_run_cut_at_its_time_limit_is_asked_about_after_the_cut(self):
        with _server(7, 8, delay=3.0) as server:
            record, _box = _versioned(server, task=Task(prompt="slow", timeout=1))
            asked = len(server.version_requests)
        assert record["finish_state"] == "timeout" and asked == 2
        assert record["trainable"] is False
        assert record["problems"][0].startswith("the server's version changed during the run")

    def test_a_run_given_no_address_asks_nobody(self):
        with _server(7) as server:
            record, _box = _run(server)
            assert server.version_requests == []
        assert record["server_version"] is None

    def test_a_field_needs_an_address(self):
        with (
            _server(7) as server,
            pytest.raises(SetupError, match="no address to ask is given"),
        ):
            _run(server, server_version_field="revision")


def server_url(asked: list[dict[str, Any]], said: dict[str, Any]) -> str:
    """The address in the record, checked for its shape only: the port is the test's."""
    assert said["url"].startswith("http://127.0.0.1:") and said["url"].endswith("/version")
    assert len(asked) == 2
    return str(said["url"])


class TestTheCommand:
    def _main(self, monkeypatch: Any, server: ScriptedChatServer, out: Any, *more: str) -> int:
        box = _DirSandbox()
        real = cli.run_episode
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c: real(t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True),
        )
        return cli.main(
            ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            + ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            + ["--sampler-logprobs", "yes", "--out", str(out)]
            + ["--server-version-url", server.version_url, *more]
        )

    def test_it_takes_the_address_and_the_field(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        with _server({"revision": "w-3"}) as server:
            status = self._main(monkeypatch, server, out, "--server-version-field", "revision")
        written = json.loads(out.read_text())
        assert status == 0 and written["server_version"]["first"] == "w-3"
        assert written["server_version"]["same"] is True
        assert "server version:" in capsys.readouterr().out

    def test_an_address_with_a_port_that_is_none_is_refused_before_anything_runs(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        monkeypatch.setenv("ROLLOUT_TEST_KEY", "k1")
        with ScriptedChatServer(SCRIPT) as server:
            flags = ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            flags += ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            flags += ["--sampler-logprobs", "yes", "--out", str(out)]
            flags += ["--api-key-env", "ROLLOUT_TEST_KEY"]
            assert cli.main([*flags, "--server-version-url", "http://127.0.0.1:99999/x"]) == 3
            assert server.requests == [] and server.version_requests == []
        assert "'http://127.0.0.1:99999/x' is no such address" in capsys.readouterr().err
        assert not out.exists()

    def test_a_changed_version_is_the_status_of_a_run_that_cannot_be_trained_on(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        with _server(1, 2) as server:
            status = self._main(monkeypatch, server, out)
        assert status == 2 and json.loads(out.read_text())["trainable"] is False
        assert "the server's version changed during the run" in capsys.readouterr().out
