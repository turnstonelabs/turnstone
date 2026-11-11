"""A task as a JSON file, and the command line that takes one.

No container: the file is read into a ``Task``, and where a rollout runs it
runs in the directory stand-in of ``test_rollout_episode.py``.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import json
import os
import socket
import stat
import subprocess
import sys
from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from tests.test_rollout_episode import BELOW_THE_LEAST, SCRIPT, _DirSandbox
from turnstone.core import config as config_module
from turnstone.rollout import cli
from turnstone.rollout.container import ContainerSpec
from turnstone.rollout.episode import Task
from turnstone.rollout.preparation import BeforeRunCommand
from turnstone.rollout.settings import SetupError, strict_json
from turnstone.rollout.sidecar import Sidecar
from turnstone.rollout.taskfile import CLASSES, load_task, task_from_data, task_to_data
from turnstone.rollout.verification import AfterRunCommand

NL = chr(10)


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


# A task that says something other than the default in every field of every
# class a task file is read into.
FULL = Task(
    prompt="Fix the failing test.",
    key="task-0042",
    system_prompt="You fix tests.",
    setup_files={"notes.txt": "read me", "/etc/task/hint": "absolute"},
    files={"data/blob.bin": bytes(range(256))},
    max_turns=7,
    timeout=None,
    max_generated_tokens=60_000,
    max_command_seconds=12.5,
    container=ContainerSpec(
        image="tasks/one:latest",
        workdir="/testbed",
        user="0:0",
        read_only_root=False,
        writable={"/scratch": "64m"},
        root_size="2g",
        volume_size="8m",
        shm_size="16m",
        capabilities=("CHOWN", "SETUID"),
        environment={"TASK": "one"},
        cpus=2.5,
        memory="3g",
        pids_limit=128,
    ),
    before_run=[
        BeforeRunCommand(["sh", "-c", "echo ready"], user="7:7", timeout=9, max_output_chars=99)
    ],
    after_run=[
        AfterRunCommand(
            ["sh", "-c", "echo done"],
            user="8:8",
            timeout=11,
            max_output_chars=77,
            collect=["report.txt"],
            max_collect_bytes=4096,
        )
    ],
    sidecar=Sidecar(
        container=ContainerSpec(image="tasks/servers:latest", user="0:0", read_only_root=False),
        files={"/srv/server.py": b"print('serving')", "/srv/state.bin": bytes(range(8))},
        setup=["sh", "-c", "start-the-servers"],
        setup_timeout=33,
        wait_ports=[39101, 39102],
        wait_timeout=44,
        mcp_servers={"ledger": "http://127.0.0.1:39101/mcp"},
    ),
)


def _refused(data: Any) -> str:
    with pytest.raises(SetupError) as refusal:
        task_from_data(data)
    return str(refusal.value)


class TestWhatAFileCanSay:
    def test_everything_a_task_holds(self, tmp_path):
        path = tmp_path / "task.json"
        path.write_text(json.dumps(task_to_data(FULL)), encoding="utf-8")
        assert load_task(str(path)) == FULL

    def test_the_full_task_leaves_no_field_at_its_default(self):
        """So that a field added to one of the classes fails here until the task
        above says it, and with that the file is shown to be able to."""
        instances = [FULL, FULL.container, FULL.before_run[0], FULL.after_run[0], FULL.sidecar]
        assert {type(i) for i in instances} == set(CLASSES)
        for instance in instances:
            for field in dataclasses.fields(instance):
                if field.default is not dataclasses.MISSING:
                    default = field.default
                elif field.default_factory is not dataclasses.MISSING:
                    default = field.default_factory()
                else:
                    continue
                assert getattr(instance, field.name) != default, (
                    f"{type(instance).__name__}.{field.name} is at its default"
                )

    def test_the_keys_are_the_fields(self):
        data = task_to_data(FULL)
        fields = {f.name for f in dataclasses.fields(Task)}
        assert set(data) == fields - {"setup_files"}
        assert set(data["container"]) == {f.name for f in dataclasses.fields(ContainerSpec)}
        assert set(data["before_run"][0]) == {f.name for f in dataclasses.fields(BeforeRunCommand)}
        assert set(data["after_run"][0]) == {f.name for f in dataclasses.fields(AfterRunCommand)}
        assert set(data["sidecar"]) == {f.name for f in dataclasses.fields(Sidecar)}

    def test_only_the_prompt_is_required(self):
        assert task_from_data({"prompt": "do it"}) == Task(prompt="do it")
        assert _refused({"key": "k"}) == "task file: prompt is missing"
        assert (
            _refused({"prompt": "p", "sidecar": {}}) == "task file: sidecar: container is missing"
        )
        assert _refused({"prompt": "p", "before_run": [{}]}) == (
            "task file: before_run[0]: argv is missing"
        )

    def test_a_files_content_is_text_or_base64(self):
        blob = base64.b64encode(bytes([0, 255, 10])).decode("ascii")
        task = task_from_data(
            {
                "prompt": "p",
                "files": {"a.txt": "plain", "b.txt": {"text": "said"}, "c.bin": {"base64": blob}},
                "sidecar": {
                    "container": {},
                    "files": {"/srv/a.txt": "plain", "/srv/c.bin": {"base64": blob}},
                },
            }
        )
        assert task.setup_files == {"a.txt": "plain", "b.txt": "said"}
        assert task.files == {"c.bin": bytes([0, 255, 10])}
        assert task.sidecar is not None
        assert task.sidecar.files == {"/srv/a.txt": b"plain", "/srv/c.bin": bytes([0, 255, 10])}

    def test_the_time_limit_left_out_is_the_command_lines(self):
        assert task_from_data({"prompt": "p"}).timeout == 300
        assert task_from_data({"prompt": "p", "max_generated_tokens": 1000}).timeout is None
        assert task_from_data({"prompt": "p", "timeout": None}).timeout is None
        assert (
            task_from_data({"prompt": "p", "max_generated_tokens": 9, "timeout": 60}).timeout == 60
        )


class TestWhatAFileMustNotHold:
    @pytest.mark.parametrize(
        ("data", "said"),
        [
            (
                {"prompt": "p", "promt": "q"},
                "unknown key 'promt' at the top (did you mean 'prompt'?)",
            ),
            (
                {"prompt": "p", "container": {"imge": "x"}},
                "unknown key 'imge' in container (did you mean 'image'?)",
            ),
            (
                {"prompt": "p", "before_run": [{"argv": ["a"], "time_out": 5}]},
                "unknown key 'time_out' in before_run[0] (did you mean 'timeout'?)",
            ),
            (
                {"prompt": "p", "after_run": [{"argv": ["a"]}, {"argv": ["b"], "colect": []}]},
                "unknown key 'colect' in after_run[1] (did you mean 'collect'?)",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {}, "mcp": {}}},
                "unknown key 'mcp' in sidecar",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {"workdirr": "/w"}}},
                "unknown key 'workdirr' in sidecar.container (did you mean 'workdir'?)",
            ),
            (
                {"prompt": "p", "verifier_command": ["x"]},
                "unknown key 'verifier_command' at the top",
            ),
            (
                {"prompt": "p", "setup_files": {}},
                "unknown key 'setup_files': a task file has one key, 'files'",
            ),
        ],
    )
    def test_a_key_the_classes_do_not_have_is_refused_by_name(self, data, said):
        assert said in _refused(data)

    def test_the_refusal_lists_the_keys_there_are(self):
        said = _refused({"prompt": "p", "before_run": [{"argv": ["a"], "shell": True}]})
        assert said.endswith("the keys there are argv, max_output_chars, timeout, user")

    def test_a_key_given_twice_is_refused(self, tmp_path):
        path = tmp_path / "task.json"
        path.write_text('{"prompt": "p", "max_turns": 3, "max_turns": 30}', encoding="utf-8")
        with pytest.raises(SetupError, match="the key 'max_turns' is given twice"):
            load_task(str(path))

    @pytest.mark.parametrize(
        ("data", "said"),
        [
            ({"prompt": 5}, "prompt: must be a string, not 5"),
            ({"prompt": "p", "max_turns": "5"}, 'max_turns: must be a whole number, not "5"'),
            ({"prompt": "p", "max_turns": True}, "max_turns: must be a whole number, not true"),
            ({"prompt": "p", "timeout": 1.5}, "timeout: must be a whole number, not 1.5"),
            (
                {"prompt": "p", "max_command_seconds": "9"},
                'max_command_seconds: must be a number, not "9"',
            ),
            ({"prompt": "p", "container": []}, "container: must be an object"),
            ({"prompt": "p", "container": None}, "container: must be an object"),
            (
                {"prompt": "p", "container": {"read_only_root": "no"}},
                'container.read_only_root: must be true or false, not "no"',
            ),
            (
                {"prompt": "p", "container": {"capabilities": "CHOWN"}},
                "container.capabilities: must be a list",
            ),
            (
                {"prompt": "p", "container": {"environment": {"A": 1}}},
                "container.environment['A']: must be a string, not 1",
            ),
            (
                {"prompt": "p", "container": {"image": None}},
                "container.image: must be a string, not null",
            ),
            ({"prompt": "p", "before_run": {"argv": ["a"]}}, "before_run: must be a list"),
            ({"prompt": "p", "before_run": [["sh"]]}, "before_run[0]: must be an object"),
            (
                {"prompt": "p", "before_run": [{"argv": "sh -c x"}]},
                "before_run[0].argv: must be a list",
            ),
            (
                {"prompt": "p", "before_run": [{"argv": ["sh", 1]}]},
                "before_run[0].argv[1]: must be a string, not 1",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {}, "wait_ports": ["80"]}},
                'sidecar.wait_ports[0]: must be a whole number, not "80"',
            ),
            ({"prompt": "p", "files": []}, "files: must be an object of paths and their contents"),
            ({"prompt": "p", "files": {"a": 5}}, "files['a']: a file's content is a string"),
            ({"prompt": "p", "files": {"a": {"base64": "no base64!"}}}, "files['a']: not base64"),
            (
                {"prompt": "p", "files": {"a": {"text": "x", "base64": "eA=="}}},
                "files['a']: a file's content is a string",
            ),
            ({"prompt": "p", "files": {"": "x"}}, "files: a file's path is empty"),
        ],
    )
    def test_a_value_of_the_wrong_kind_is_refused(self, data, said):
        assert said in _refused(data)

    @pytest.mark.parametrize(
        ("data", "said"),
        [
            ({"prompt": "  "}, "prompt: is empty"),
            ({"prompt": "p", "max_turns": 0}, "max_turns: must be at least 1"),
            (
                {"prompt": "p", "container": {"capabilities": ["SYS_ADMIN"]}},
                "container: capability SYS_ADMIN is refused",
            ),
            (
                {"prompt": "p", "container": {"workdir": "work"}},
                "container: the working directory must be absolute",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {"capabilities": ["NET_RAW"]}}},
                "sidecar.container: capability NET_RAW is refused",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {}, "files": {"srv/a": "x"}}},
                "sidecar.files['srv/a']: a sidecar's paths are absolute",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {}, "setup": []}},
                "sidecar.setup: names no program",
            ),
            ({"prompt": "p", "before_run": [{"argv": []}]}, "before_run[0].argv: names no program"),
            (
                {"prompt": "p", "after_run": [{"argv": ["a"], "timeout": 0}]},
                "after_run[0].timeout: must be above zero",
            ),
            # At zero every file read out would count as more than is read,
            # which is taken for the model's doing.
            (
                {"prompt": "p", "after_run": [{"argv": ["a"], "max_collect_bytes": 0}]},
                "after_run[0].max_collect_bytes: must be above zero",
            ),
            (
                {"prompt": "p", "after_run": [{"argv": ["a"], "max_output_chars": -1}]},
                "after_run[0].max_output_chars: must not be below zero",
            ),
            (
                {"prompt": "p", "before_run": [{"argv": ["a"], "max_output_chars": -1}]},
                "before_run[0].max_output_chars: must not be below zero",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {}, "setup_timeout": 0}},
                "sidecar.setup_timeout: must be above zero",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {}, "wait_timeout": -1}},
                "sidecar.wait_timeout: must not be below zero",
            ),
            (
                {"prompt": "p", "sidecar": {"container": {}, "wait_ports": [39101, 0]}},
                "sidecar.wait_ports[1]: must be a port, 1 to 65535",
            ),
        ],
    )
    def test_what_no_container_could_be_is_refused(self, data, said):
        assert said in _refused(data)

    def test_a_file_that_is_no_json_object(self, tmp_path):
        path = tmp_path / "task.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(SetupError, match="is not JSON"):
            load_task(str(path))
        path.write_text('["prompt"]', encoding="utf-8")
        with pytest.raises(SetupError, match="must hold one JSON object"):
            load_task(str(path))
        with pytest.raises(SetupError, match="cannot be read"):
            load_task(str(tmp_path / "missing.json"))


def _never(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("nothing may be run")


class TestTheCommand:
    BASE = ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
    BASE += ["--sampler-logprobs", "yes"]

    def _main(
        self, monkeypatch: Any, server: ScriptedChatServer, *more: str, box: Any = None
    ) -> tuple[int, _DirSandbox]:
        sandbox = box or _DirSandbox()
        real = cli.run_episode
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c, **check: real(
                t, c, sandbox_factory=lambda _t, _c: sandbox, allow_host_execution=True, **check
            ),
        )
        argv = ["--base-url", server.base_url, "--model", "scripted", *self.BASE, *more]
        return cli.main(argv), sandbox

    def _task_file(self, tmp_path: Any, **settings: Any) -> str:
        path = tmp_path / "task.json"
        data = {"prompt": "make a file", "key": "from-a-file", "files": {"seed.txt": "seeded"}}
        path.write_text(json.dumps({**data, **settings}), encoding="utf-8")
        return str(path)

    def test_it_runs_the_task_a_file_describes(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        task = self._task_file(tmp_path, max_turns=9)
        with ScriptedChatServer(SCRIPT) as server:
            status, _box = self._main(monkeypatch, server, "--task", task, "--out", str(out))
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["key"] == "from-a-file" and record["task"]["max_turns"] == 9
        assert record["task"]["setup_files"] == {"seed.txt": "seeded"}
        assert record["tool_log"][1]["result"].endswith("helloseeded")
        capsys.readouterr()

    def test_a_task_flag_beside_a_task_file_is_refused(self, monkeypatch, tmp_path, capsys):
        task = self._task_file(tmp_path)
        monkeypatch.setattr(cli, "run_episode", _never)
        with ScriptedChatServer(SCRIPT) as server:
            base = ["--base-url", server.base_url, "--model", "scripted", "--task", task]
            assert cli.main([*base, "--max-turns", "3", "--image", "other", "--cap", "CHOWN"]) == 3
            said = capsys.readouterr().err
            assert "--cap, --image, --max-turns cannot be given with --task" in said
            # Also one whose value is the default, and one that only switches something on.
            assert cli.main([*base, "--timeout", "300"]) == 3
            assert "--timeout cannot be given with --task" in capsys.readouterr().err
            assert cli.main([*base, "--writable-root"]) == 3
            assert cli.main([*base, "--prompt", "another"]) == 3
            assert server.requests == []

    def test_no_task_at_all_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "run_episode", _never)
        assert cli.main(["--base-url", "http://127.0.0.1:9/v1", "--model", "m"]) == 3
        assert "give the task: --prompt, or --task" in capsys.readouterr().err

    def test_a_task_file_that_is_refused_runs_nothing_and_writes_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        task = self._task_file(tmp_path, contianer={"image": "x"})
        monkeypatch.setattr(cli, "run_episode", _never)
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m"]
        assert cli.main([*base, "--task", task, "--out", str(out)]) == 3
        assert "unknown key 'contianer' at the top (did you mean 'container'?)" in (
            capsys.readouterr().err
        )
        assert not out.exists()

    def test_a_flag_the_command_does_not_have_is_refused_with_the_same_status(self, capsys):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["--base-url", "u", "--model", "m", "--prompt", "p", "--no-such-flag"])
        assert stopped.value.code == 3
        assert "--no-such-flag" in capsys.readouterr().err

    def test_a_configuration_that_cannot_work_is_refused_too(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SCRIPT) as server:
            base = ["--base-url", server.base_url, "--model", "scripted", "--prompt", "p"]
            assert cli.main([*base, "--out", str(out)]) == 3
            assert server.requests == []
        assert "the context window of scripted is not known" in capsys.readouterr().err
        assert not out.exists()

    def test_the_record_of_a_run_that_did_not_start_is_written(self, monkeypatch, tmp_path, capsys):
        """The directory stand-in cannot run a task's before-run commands, so the
        run does not start; the record says so and is there."""
        out = tmp_path / "episode.json"
        task = self._task_file(tmp_path, before_run=[{"argv": ["true"]}])
        with ScriptedChatServer(SCRIPT) as server:
            status, _box = self._main(monkeypatch, server, "--task", task, "--out", str(out))
            assert server.requests == []
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 1 and record["finish_state"] == "before_run_failed"
        assert record["trainable"] is False and record["turns"] == 0
        capsys.readouterr()

    def test_the_record_is_there_whole_or_not_at_all(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SCRIPT) as server:
            status, _box = self._main(monkeypatch, server, "--prompt", "go", "--out", str(out))
        assert status == 0
        # One file, under its own name, and nothing beside it.
        assert os.listdir(tmp_path) == ["episode.json"]
        assert json.loads(out.read_text(encoding="ascii"))["finish_state"] == "completed"
        capsys.readouterr()

    def test_a_write_that_fails_leaves_nothing_behind(self, monkeypatch, tmp_path):
        target = tmp_path / "episode.json"
        target.write_text("the record of an earlier run", encoding="ascii")

        def fails(_source: str, _target: str) -> None:
            raise OSError("no room")

        monkeypatch.setattr(cli.os, "replace", fails)
        with pytest.raises(OSError, match="no room"):
            cli._write_whole(str(target), "half a rec")
        assert os.listdir(tmp_path) == ["episode.json"]
        assert target.read_text(encoding="ascii") == "the record of an earlier run"

    def test_the_limits_can_be_set(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        flags = ["--max-output-chars", "5000", "--max-read-bytes", "4096", "--key", "named"]
        with ScriptedChatServer(SCRIPT) as server:
            status, _box = self._main(
                monkeypatch, server, "--prompt", "go", "--out", str(out), *flags
            )
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0 and record["key"] == "named"
        assert record["limits"]["max_output_chars"] == {"value": 5000, "source": "rollout config"}
        capsys.readouterr()

    def test_a_rule_file_that_cannot_be_used_is_refused(self, monkeypatch, tmp_path, capsys):
        rules = tmp_path / "rules.json"
        rules.write_text('{"name": "mine"}', encoding="utf-8")
        monkeypatch.setattr(cli, "run_episode", _never)
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p"]
        assert cli.main([*base, "--rules", str(rules)]) == 3
        assert "cannot be used as a rule file" in capsys.readouterr().err
        assert cli.main([*base, "--rules", str(tmp_path / "missing.json")]) == 3

    def test_a_container_flag_no_container_could_have_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "run_episode", _never)
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p"]
        assert cli.main([*base, "--cap", "SYS_ADMIN"]) == 3
        assert "capability SYS_ADMIN is refused" in capsys.readouterr().err

    def test_the_key_is_read_from_the_environment(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "run_episode", _never)
        monkeypatch.delenv("ROLLOUT_TEST_KEY", raising=False)
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p"]
        assert cli.main([*base, "--api-key-env", "ROLLOUT_TEST_KEY"]) == 3
        assert "the environment has no ROLLOUT_TEST_KEY" in capsys.readouterr().err


MODEL_DEFINITION = """
[models.scripted]
model = "scripted"
context_window = 200000
max_tokens = 1234
reasoning_effort = "low"

[models.scripted.capabilities.server_compat.extra_body]
top_k = 20
top_p = 0.95
"""


class TestTheConfigurationFile:
    """A configuration file that cannot be used is refused, never read as empty:
    the run would lose the model's definition without a word."""

    BASE = ["--base-url", "http://127.0.0.1:9/v1", "--model", "scripted", "--prompt", "p"]

    @pytest.fixture(autouse=True)
    def _the_real_loader(self, monkeypatch, tmp_path):
        """The framework's own loader, with nothing named and a default path of
        the test's own; what a test names is forgotten again afterwards."""
        monkeypatch.setattr(config_module, "_cache", None)
        monkeypatch.setattr(config_module, "_config_path", None)
        monkeypatch.setattr(config_module, "_DEFAULT_CONFIG_PATH", tmp_path / "default.toml")
        monkeypatch.delenv("TURNSTONE_CONFIG", raising=False)

    def _refused(self, monkeypatch: Any, capsys: Any, *flags: str) -> str:
        monkeypatch.setattr(cli, "run_episode", _never)
        monkeypatch.setattr(cli, "_logprob_verdict", _never)
        assert cli.main([*self.BASE, *flags]) == 3
        said = capsys.readouterr().err
        assert "the configuration file cannot be used" in said
        return str(said)

    def test_a_named_file_that_is_not_there_is_refused(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        missing = tmp_path / "mistyped.toml"
        said = self._refused(monkeypatch, capsys, "--config", str(missing), "--out", str(out))
        assert str(missing) in said and "No such file" in said
        assert not out.exists()

    def test_a_named_file_that_is_no_toml_is_refused(self, monkeypatch, tmp_path, capsys):
        broken = tmp_path / "broken.toml"
        broken.write_text("[models.scripted" + NL + "max_tokens = ", encoding="utf-8")
        assert str(broken) in self._refused(monkeypatch, capsys, "--config", str(broken))

    def test_a_named_path_that_cannot_be_read_is_refused(self, monkeypatch, tmp_path, capsys):
        # A directory, and a name that is empty.
        assert str(tmp_path) in self._refused(monkeypatch, capsys, "--config", str(tmp_path))
        self._refused(monkeypatch, capsys, "--config", "")

    def test_a_file_named_by_the_environment_is_refused_the_same_way(
        self, monkeypatch, tmp_path, capsys
    ):
        missing = tmp_path / "from-the-environment.toml"
        monkeypatch.setenv("TURNSTONE_CONFIG", str(missing))
        assert str(missing) in self._refused(monkeypatch, capsys)

    def test_a_default_file_that_is_broken_is_refused_too(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "default.toml").write_text("not = [toml", encoding="utf-8")
        assert "default.toml" in self._refused(monkeypatch, capsys)

    def test_the_probe_alone_is_refused_as_well(self, monkeypatch, tmp_path, capsys):
        self._refused(monkeypatch, capsys, "--probe-only", "--config", str(tmp_path / "no.toml"))

    def test_from_python_it_is_a_setup_error(self, tmp_path):
        from turnstone.rollout.settings import resolve_limits, resolve_sampling

        config_module.set_config_path(str(tmp_path / "missing.toml"))
        asked = {"model": "scripted", "base_url": "http://127.0.0.1:9/v1"}
        with pytest.raises(SetupError, match="the configuration file cannot be used"):
            resolve_sampling(**asked, temperature=None, reasoning_effort=None, extra_body={})
        with pytest.raises(SetupError, match="the configuration file cannot be used"):
            resolve_limits(
                **asked,
                max_tokens=None,
                context_window=1000,
                tool_timeout=None,
                subagent_max_turns=None,
                max_output_chars=None,
            )

    def test_no_file_named_and_no_default_file_is_an_empty_configuration(
        self, monkeypatch, tmp_path, capsys
    ):
        """Only the default path may be absent."""
        box = _DirSandbox()
        real = cli.run_episode
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c: real(t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True),
        )
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SCRIPT) as server:
            flags = ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            flags += ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            assert cli.main([*flags, "--sampler-logprobs", "yes", "--out", str(out)]) == 0
        record = json.loads(out.read_text(encoding="ascii"))
        assert record["sampling"]["sources"]["reasoning_effort"] == (
            "not set: the model's own default"
        )
        capsys.readouterr()

    def test_a_file_that_can_be_read_gives_the_run_the_models_definition(
        self, monkeypatch, tmp_path, capsys
    ):
        """What a mistyped path used to cost a run without a word."""
        config = tmp_path / "config.toml"
        config.write_text(MODEL_DEFINITION, encoding="utf-8")
        box = _DirSandbox()
        real = cli.run_episode
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c: real(t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True),
        )
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SCRIPT) as server:
            flags = ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            flags += ["--sampler-logprobs", "yes", "--out", str(out), "--config", str(config)]
            assert cli.main(flags) == 0
            sent = server.requests[0]
        record = json.loads(out.read_text(encoding="ascii"))
        assert record["limits"]["max_tokens"] == {
            "value": 1234,
            "source": "config.toml [models.scripted] max_tokens",
        }
        assert record["sampling"]["reasoning_effort"] == "low"
        assert record["sampling"]["requested"] == {"top_k": 20, "top_p": 0.95, "temperature": 1.0}
        assert sent["reasoning_effort"] == "low" and sent["top_k"] == 20
        capsys.readouterr()


# Text no ASCII output can write, and a control sequence a terminal would obey.
CAFE = "caf" + chr(0xE9) + " " + chr(0x2615)
ESCAPE = chr(27) + "[31m"
SPOKEN: list[dict[str, Any]] = [
    {
        "reasoning": "Write it.",
        "tool_calls": [
            {"name": "write_file", "arguments": {"path": "out/a.txt", "content": CAFE}},
            {"name": "bash" + ESCAPE, "arguments": {"command": "echo never offered"}},
        ],
    },
    {"content": "Done: " + CAFE},
]

# What a child interpreter runs in place of ``-m turnstone.rollout``: someone
# else writes to an output stream first, then the command runs and its status
# is the process's, the way the module's own entry point makes it.
SOMEONE_WRITES = {
    "a logger": "from turnstone.core.log import get_logger"
    + NL
    + 'get_logger("someone.else").warning("a line the framework logged")',
    "a warning": "import warnings" + NL + 'warnings.warn("a warning someone raised")',
    "a print": 'print("a line someone printed")',
}
THEN_THE_COMMAND = NL.join(
    ["import sys", "from turnstone.rollout.cli import main", "sys.exit(main(sys.argv[1:]))"]
)
# Closes one of its own descriptors and becomes the interpreter it is told to
# be, which then starts with that descriptor closed.
CLOSED_FIRST = NL.join(
    [
        "import os, sys",
        "os.close(int(sys.argv[1]))",
        "os.execv(sys.executable, [sys.executable, *sys.argv[2:]])",
    ]
)


def _takes_no_write(state: str, tmp_path: Any) -> int:
    """A descriptor every write to which fails, for the reason *state* names."""
    if state == "its reader has gone":
        reading, writing = os.pipe()
        os.close(reading)
        return writing
    if state == "it is not open for writing":
        path = tmp_path / "nothing-to-read"
        path.write_text("", encoding="utf-8")
        return os.open(path, os.O_RDONLY)
    assert state == "its device is full"
    if not os.path.exists("/dev/full"):
        pytest.skip("this system has no device that is always full")
    return os.open("/dev/full", os.O_WRONLY)


class TestWhatIsPrinted:
    """The exit status is read off the record.  Nothing that happens while the
    summary is printed changes it: not text the output cannot encode, not an
    output that is closed.  Nor does what anyone else wrote to an output
    stream that did not take it."""

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
            + ["--sampler-logprobs", "yes", "--out", str(out), *more]
        )

    def test_text_the_output_cannot_encode_is_escaped(self, monkeypatch, tmp_path):
        """The model chooses the characters of its reply.  With an output that
        takes ASCII only the command used to die on them with status 1, after a
        trainable record had been written."""
        out = tmp_path / "episode.json"
        printed = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(printed, encoding="ascii"))
        with ScriptedChatServer(SPOKEN) as server:
            status = self._main(monkeypatch, server, out)
        record = json.loads(out.read_text(encoding="ascii"))
        assert record["final_content"] == "Done: " + CAFE and record["trainable"] is True
        assert status == 0
        said = printed.getvalue().decode("ascii")
        assert "final content: Done: caf" + chr(92) + "xe9 " + chr(92) + "u2615" in said
        assert said.endswith(NL) and "sandbox:" in said

    def test_a_name_the_model_wrote_reaches_the_terminal_escaped(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SPOKEN) as server:
            assert self._main(monkeypatch, server, out) == 0
        said = capsys.readouterr().out
        record = json.loads(out.read_text(encoding="ascii"))
        named = [entry["tool"] for entry in record["tool_log"]]
        assert named == ["write_file", "bash" + ESCAPE]
        assert chr(27) not in said and "bash" + chr(92) + "x1b[31m" in said

    def test_an_output_that_is_closed_changes_nothing(self, monkeypatch, tmp_path):
        """A pipe whose reader has gone: every write to it fails."""
        out = tmp_path / "episode.json"
        reading, writing = os.pipe()
        os.close(reading)
        closed = os.fdopen(writing, "w", buffering=1, encoding="utf-8")
        monkeypatch.setattr(sys, "stdout", closed)
        try:
            with ScriptedChatServer(SCRIPT) as server:
                status = self._main(monkeypatch, server, out)
            assert status == 0
            assert json.loads(out.read_text(encoding="ascii"))["trainable"] is True
            # Nothing is left to fail when the stream is flushed at exit.
            closed.write("more" + NL)
            closed.flush()
        finally:
            closed.close()

    def test_the_probe_alone_started_with_a_closed_output_keeps_its_status(self, tmp_path):
        """As a process: an output nobody reads used to turn a status of 0 into
        the interpreter's own for a flush that failed at exit."""
        config = tmp_path / "empty.toml"
        config.write_text("", encoding="utf-8")
        verdict = tmp_path / "verdict.json"
        reading, writing = os.pipe()
        os.close(reading)
        try:
            with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
                done = subprocess.run(
                    [sys.executable, "-m", "turnstone.rollout", "--probe-only"]
                    + ["--base-url", server.base_url, "--model", "scripted"]
                    + ["--config", str(config), "--out", str(verdict)],
                    stdin=subprocess.DEVNULL,
                    stdout=writing,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=False,
                )
        finally:
            os.close(writing)
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
        assert b"BrokenPipeError" not in done.stderr
        assert json.loads(verdict.read_text(encoding="ascii"))["sampler_logprobs"] is True

    def test_what_someone_else_wrote_to_a_stream_that_takes_nothing_changes_nothing(
        self, monkeypatch, tmp_path
    ):
        """The framework's logger writes to standard error.  A line it wrote
        to a pipe whose reader has gone stayed in the stream, and the flush at
        exit failed on it: a trainable rollout ended with the interpreter's
        status in place of 0."""
        out = tmp_path / "episode.json"
        reading, writing = os.pipe()
        os.close(reading)
        gone = os.fdopen(writing, "w", buffering=1, encoding="utf-8")
        monkeypatch.setattr(sys, "stderr", gone)
        real = cli.run_episode

        def logs_a_line(task: Any, config: Any, **more: Any) -> Any:
            # As a logging handler writes: the write fails and nobody is told.
            with pytest.raises(BrokenPipeError):
                sys.stderr.write("a line the framework logged" + NL)
            return real(task, config, **more)

        monkeypatch.setattr(cli, "run_episode", logs_a_line)
        try:
            with ScriptedChatServer(SCRIPT) as server:
                status = self._main(monkeypatch, server, out)
            assert status == 0
            assert json.loads(out.read_text(encoding="ascii"))["trainable"] is True
            # Nothing is left to fail when the stream is flushed at exit.
            gone.flush()
            gone.write("more" + NL)
            gone.flush()
        finally:
            with contextlib.suppress(OSError):
                gone.close()

    def test_a_stream_known_to_take_no_write_is_pointed_at_nothing(self, tmp_path):
        """Also one that holds no text yet: what is written to it once the
        command has returned must not fail the flush at exit either."""
        for state in ("its reader has gone", "it is not open for writing"):
            stream = os.fdopen(_takes_no_write(state, tmp_path), "w", encoding="utf-8")
            try:
                assert cli._refuses_writes(stream) is True, state
                cli._settle(stream)
                assert cli._refuses_writes(stream) is False, state
                stream.write("written after the command returned" + NL)
                stream.flush()
            finally:
                stream.close()
        # One that works is left as it is: a file, and a pipe that is read.
        kept = tmp_path / "kept.txt"
        with open(kept, "w", encoding="utf-8") as stream:
            stream.write("said" + NL)
            cli._settle(stream)
            stream.write("and more" + NL)
        assert kept.read_text(encoding="utf-8") == "said" + NL + "and more" + NL
        reading, writing = os.pipe()
        with os.fdopen(reading, "r", encoding="utf-8") as reader:
            with os.fdopen(writing, "w", encoding="utf-8") as stream:
                assert cli._refuses_writes(stream) is False
                stream.write("heard" + NL)
                cli._settle(stream)
            assert reader.read() == "heard" + NL
        # A stream without a descriptor is not the interpreter's to flush.
        assert cli._refuses_writes(io.StringIO()) is False
        cli._settle(None)

    def test_a_descriptor_closed_under_its_stream_is_opened_on_nothing(self, tmp_path):
        """The system gives the number of a closed descriptor out again, to
        the very file the stream is to be pointed at: that one must stay."""
        path = tmp_path / "written.txt"
        stream = open(path, "w", encoding="utf-8")  # noqa: SIM115 - closed under the test's hand
        try:
            descriptor = stream.fileno()
            stream.write("held by the stream")
            os.close(descriptor)
            assert cli._refuses_writes(stream) is True
            cli._settle(stream)
            # The stream's descriptor is open again, and on nothing.
            stream.flush()
            assert os.fstat(descriptor).st_rdev == os.stat(os.devnull).st_rdev
        finally:
            stream.close()
        assert path.read_text(encoding="utf-8") == ""

    @pytest.mark.parametrize(
        ("output", "errors", "writers", "does", "status"),
        [
            # A verdict of the probe that says yes: status 0, as for a rollout
            # that is trainable.
            ("kept", "its reader has gone", ["a logger"], "probes", 0),
            ("kept", "its reader has gone", ["a warning"], "probes", 0),
            ("kept", "it is not open for writing", ["a logger"], "probes", 0),
            ("kept", "its device is full", ["a logger"], "probes", 0),
            ("kept", "it is closed", ["a logger"], "probes", 0),
            ("its reader has gone", "its reader has gone", ["a logger", "a print"], "probes", 0),
            # A refusal says nothing on standard output, so nothing of the
            # command's own found out that the stream takes no write.
            ("its reader has gone", "kept", ["a print"], "is refused", 3),
            ("its device is full", "kept", ["a print"], "is refused", 3),
            # A command line that cannot be read: its words are the parser's.
            ("kept", "its reader has gone", [], "cannot be read", 3),
        ],
    )
    def test_started_as_a_process_no_stream_that_takes_no_write_changes_the_status(
        self, tmp_path, output, errors, writers, does, status
    ):
        """What the framework's logger wrote to a standard error nobody reads
        stayed in the stream, the interpreter's flush at exit failed, and the
        process ended with status 120 where the record said 0."""
        config = tmp_path / "empty.toml"
        config.write_text("", encoding="utf-8")
        verdict = tmp_path / "verdict.json"
        code = NL.join([*(SOMEONE_WRITES[who] for who in writers), THEN_THE_COMMAND])
        start = [sys.executable, "-c", code]
        if errors == "it is closed":
            start = [sys.executable, "-c", CLOSED_FIRST, "2", "-c", code]
        streams: dict[str, int] = {}
        try:
            for name, state in (("stdout", output), ("stderr", errors)):
                if state == "kept":
                    streams[name] = subprocess.PIPE
                elif state == "it is closed":
                    streams[name] = subprocess.DEVNULL
                else:
                    streams[name] = _takes_no_write(state, tmp_path)
            with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
                flags = {
                    "probes": ["--probe-only", "--base-url", server.base_url, "--model", "scripted"]
                    + ["--config", str(config), "--out", str(verdict)],
                    # No task is named.
                    "is refused": ["--base-url", server.base_url, "--model", "scripted"]
                    + ["--config", str(config)],
                    "cannot be read": ["--no-such-flag"],
                }[does]
                done = subprocess.run(
                    [*start, *flags], stdin=subprocess.DEVNULL, timeout=120, check=False, **streams
                )
        finally:
            for descriptor in streams.values():
                if descriptor >= 0:
                    os.close(descriptor)
        said = (done.stdout or b"") + (done.stderr or b"")
        assert done.returncode == status, said.decode("utf-8", "replace")[-2000:]
        if does == "probes":
            assert json.loads(verdict.read_text(encoding="ascii"))["sampler_logprobs"] is True
        if does == "is refused":
            # The stream that works still says why.
            assert b"give the task" in done.stderr

    def test_a_record_that_cannot_be_written_is_no_trainable_rollout(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"

        def fails(_path: str, _text: str) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(cli, "_write_whole", fails)
        with ScriptedChatServer(SCRIPT) as server:
            status = self._main(monkeypatch, server, out)
        said = capsys.readouterr()
        assert status == 1 and not out.exists()
        assert f"the record could not be written to {out}" in said.err
        assert "No space left on device" in said.err and "no file is there" in said.err
        # Nothing is printed of a rollout whose record is lost.
        assert "finish_state" not in said.out

    def test_a_summary_that_cannot_be_made_leaves_the_status_alone(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"

        def fails(_record: dict[str, Any]) -> list[str]:
            raise KeyError("a key the summary expected")

        monkeypatch.setattr(cli, "_summary", fails)
        with ScriptedChatServer(SCRIPT) as server:
            status = self._main(monkeypatch, server, out)
        assert status == 0 and json.loads(out.read_text(encoding="ascii"))["trainable"] is True
        assert "the summary could not be made (KeyError" in capsys.readouterr().err

    def test_a_refusal_is_said_whatever_the_output_can_encode(self, monkeypatch, tmp_path):
        said = io.BytesIO()
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(said, encoding="ascii"))
        monkeypatch.setattr(cli, "run_episode", _never)
        task = tmp_path / ("t" + chr(0xE2) + "che.json")
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m"]
        assert cli.main([*base, "--task", str(task)]) == 3
        assert b"cannot be read" in said.getvalue() and b"t" + chr(92).encode() + b"xe2che" in (
            said.getvalue()
        )

    def test_the_writer_never_raises(self):
        assert cli._put(None, "nobody listens" + NL) is False
        gone = io.StringIO()
        gone.close()
        assert cli._put(gone, "closed" + NL) is False
        plain = io.StringIO()
        assert cli._put(plain, CAFE + NL) is True and plain.getvalue() == CAFE + NL


class TestWhereTheRecordGoes:
    """``--out`` is looked at before anything runs.  Afterwards, whatever the
    command did, the file there is this run's record or is not there."""

    BASE = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p"]

    def _refused(self, monkeypatch: Any, capsys: Any, *flags: str) -> str:
        monkeypatch.setattr(cli, "run_episode", _never)
        monkeypatch.setattr(cli, "_logprob_verdict", _never)
        assert cli.main([*self.BASE, *flags]) == 3
        return str(capsys.readouterr().err)

    def test_a_folder_that_is_not_there_is_refused_before_anything_runs(
        self, monkeypatch, tmp_path, capsys
    ):
        """The whole rollout used to run first, and then had nowhere to go."""
        out = tmp_path / "no-such-folder" / "episode.json"
        said = self._refused(monkeypatch, capsys, "--out", str(out))
        assert f"--out: the folder {out.parent} is not there" in said
        assert os.listdir(tmp_path) == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="every folder takes a file from root")
    def test_a_folder_that_takes_no_file_is_refused(self, monkeypatch, tmp_path, capsys):
        folder = tmp_path / "read-only"
        folder.mkdir()
        folder.chmod(0o500)
        try:
            said = self._refused(monkeypatch, capsys, "--out", str(folder / "episode.json"))
        finally:
            folder.chmod(0o700)
        assert f"--out: no file can be written in the folder {folder}" in said
        assert os.listdir(folder) == []

    def test_a_folder_where_the_record_belongs_is_refused(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        out.mkdir()
        said = self._refused(monkeypatch, capsys, "--out", str(out))
        assert f"--out: what is at {out} is no file that can be replaced" in said
        assert out.is_dir() and os.listdir(tmp_path) == ["episode.json"]

    def test_a_name_that_is_empty_is_refused(self, monkeypatch, capsys):
        assert "--out names no file" in self._refused(monkeypatch, capsys, "--out", "")

    def test_a_place_that_is_a_file_the_command_reads_is_refused_and_left_alone(
        self, monkeypatch, tmp_path, capsys
    ):
        """What is at the place is removed first of all: that must never be the
        task file or a verdict the same command line names."""
        task = tmp_path / "task.json"
        task.write_text('{"prompt": "p"}', encoding="utf-8")
        verdict = tmp_path / "server.json"
        verdict.write_text("{}", encoding="utf-8")
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m"]
        monkeypatch.setattr(cli, "run_episode", _never)
        for flags, said in (
            (["--task", str(task), "--out", str(task)], "--task reads"),
            (["--task", str(task), "--out", str(tmp_path / "." / "task.json")], "--task reads"),
            (
                ["--prompt", "p", "--sampler-logprobs", f"@{verdict}", "--out", str(verdict)],
                "--sampler-logprobs reads",
            ),
            (["--prompt", "p", "--rules", str(task), "--out", str(task)], "--rules reads"),
            (["--prompt", "p", "--config", str(task), "--out", str(task)], "--config reads"),
        ):
            assert cli.main([*base, *flags]) == 3
            assert f"--out names the file that {said}" in capsys.readouterr().err
        assert task.read_text(encoding="utf-8") == '{"prompt": "p"}'
        assert verdict.read_text(encoding="utf-8") == "{}"

    def test_a_place_that_holds_no_file_is_refused_and_left_alone(
        self, monkeypatch, tmp_path, capsys
    ):
        """Whatever stood at the place was removed unless it was a folder: a
        named pipe, a socket, and with root's rights the node of a device."""
        pipe = tmp_path / "episode.json"
        os.mkfifo(pipe)
        said = self._refused(monkeypatch, capsys, "--out", str(pipe))
        assert f"--out: what is at {pipe} is no file that can be replaced (a named pipe)" in said
        assert "it is left as it is" in said
        assert stat.S_ISFIFO(os.lstat(pipe).st_mode)
        # A socket's path has a short limit, so it is named from inside the folder.
        monkeypatch.chdir(tmp_path)
        with socket.socket(socket.AF_UNIX) as listening:
            listening.bind("s.json")
            said = self._refused(monkeypatch, capsys, "--out", "s.json")
            assert "--out: what is at s.json is no file that can be replaced (a socket)" in said
            assert stat.S_ISSOCK(os.lstat("s.json").st_mode)
        # Nothing of the look at the place is left beside them either.
        assert sorted(os.listdir(tmp_path)) == ["episode.json", "s.json"]

    def test_a_link_at_the_place_is_removed_and_what_it_points_to_is_not(
        self, monkeypatch, tmp_path, capsys
    ):
        kept = tmp_path / "another-runs-record.json"
        kept.write_text('{"key": "another run"}', encoding="ascii")
        out = tmp_path / "episode.json"
        out.symlink_to(kept)
        said = self._refused(
            monkeypatch, capsys, "--out", str(out), "--rules", str(tmp_path / "no-rules.json")
        )
        assert "cannot be used as a rule file" in said
        assert os.listdir(tmp_path) == ["another-runs-record.json"]
        assert kept.read_text(encoding="ascii") == '{"key": "another run"}'

    def test_the_configuration_file_is_held_however_it_is_named(
        self, monkeypatch, tmp_path, capsys
    ):
        """One that the environment names, and the default one, are read by
        the command as one named by a flag is: none may be removed first."""
        monkeypatch.setattr(config_module, "_config_path", None)
        named = tmp_path / "from-the-environment.toml"
        named.write_text("[model]" + NL, encoding="utf-8")
        monkeypatch.setenv("TURNSTONE_CONFIG", str(named))
        said = self._refused(monkeypatch, capsys, "--out", str(named))
        assert f"--out names the file that the configuration is read from ({named})" in said
        assert named.read_text(encoding="utf-8") == "[model]" + NL
        monkeypatch.delenv("TURNSTONE_CONFIG")
        default = tmp_path / "default.toml"
        default.write_text("[tools]" + NL, encoding="utf-8")
        monkeypatch.setattr(config_module, "_DEFAULT_CONFIG_PATH", default)
        said = self._refused(monkeypatch, capsys, "--out", str(default))
        assert f"--out names the file that the configuration is read from ({default})" in said
        assert default.read_text(encoding="utf-8") == "[tools]" + NL

    def test_what_the_verifier_command_starts_and_is_given_is_held(
        self, monkeypatch, tmp_path, capsys
    ):
        """The record would take the place of the grader, or of its tasks."""
        programs = tmp_path / "bin"
        programs.mkdir()
        grade = programs / "grade"
        grade.write_text("#!/bin/sh" + NL, encoding="utf-8")
        grade.chmod(0o700)
        tasks = tmp_path / "tasks.jsonl"
        tasks.write_text("{}" + NL, encoding="utf-8")
        monkeypatch.setenv("PATH", str(programs), prepend=os.pathsep)
        for command, out, said in (
            (f"{grade} {tasks} -- {{key}}", grade, "--verifier-command starts"),
            # Found the way the system finds a program that is only named.
            (f"grade {tasks} -- {{key}}", grade, "--verifier-command starts"),
            (f"grade {tasks} -- {{key}}", tasks, "--verifier-command names"),
        ):
            refused = self._refused(
                monkeypatch, capsys, "--verifier-command", command, "--out", str(out)
            )
            assert f"--out names the file that {said} ({out})" in refused
        assert grade.read_text(encoding="utf-8") == "#!/bin/sh" + NL
        assert tasks.read_text(encoding="utf-8") == "{}" + NL

    def test_the_probe_alone_is_refused_the_same_way(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "no-such-folder" / "verdict.json"
        said = self._refused(monkeypatch, capsys, "--probe-only", "--out", str(out))
        assert "is not there" in said

    def test_a_file_of_an_earlier_run_is_gone_when_the_command_is_refused(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        out.write_text('{"key": "an earlier run"}', encoding="ascii")
        said = self._refused(
            monkeypatch, capsys, "--out", str(out), "--rules", str(tmp_path / "no-rules.json")
        )
        assert "cannot be used as a rule file" in said
        # Nothing of the look at the place is left either.
        assert os.listdir(tmp_path) == []

    def test_a_file_of_an_earlier_run_is_gone_when_the_run_dies(self, monkeypatch, tmp_path):
        """Status 1 beside a file an earlier run left looked like a record."""
        out = tmp_path / "episode.json"
        out.write_text('{"key": "an earlier run"}', encoding="ascii")

        def dies(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the run died here")

        monkeypatch.setattr(cli, "run_episode", dies)
        flags = ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
        with pytest.raises(RuntimeError, match="the run died here"):
            cli.main([*self.BASE, *flags, "--sampler-logprobs", "yes", "--out", str(out)])
        assert os.listdir(tmp_path) == []

    def test_after_a_run_the_file_is_this_runs_record(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        out.write_text('{"key": "an earlier run"}', encoding="ascii")
        box = _DirSandbox()
        real = cli.run_episode
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c: real(t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True),
        )
        with ScriptedChatServer(SCRIPT) as server:
            flags = ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            flags += ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            flags += ["--sampler-logprobs", "yes", "--key", "this run", "--out", str(out)]
            assert cli.main(flags) == 0
        assert json.loads(out.read_text(encoding="ascii"))["key"] == "this run"
        assert os.listdir(tmp_path) == ["episode.json"]
        capsys.readouterr()


class TestATaskGivenByFlags:
    """It gets the check a task file gets: what is refused in a file must not
    start a container when a flag says it."""

    BASE = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m"]

    @pytest.mark.parametrize(
        ("flags", "data", "the_flags_are_told", "the_file_is_told"),
        [
            (
                ["--prompt", "p", "--max-turns", "0"],
                {"prompt": "p", "max_turns": 0},
                "--max-turns: must be at least 1",
                "task file: max_turns: must be at least 1",
            ),
            (
                ["--prompt", "  "],
                {"prompt": "  "},
                "--prompt: is empty",
                "task file: prompt: is empty",
            ),
            (
                ["--prompt", "p", "--file", "=text"],
                {"prompt": "p", "files": {"": "text"}},
                "--file: a file's path is empty",
                "task file: files: a file's path is empty",
            ),
            (
                ["--prompt", "p", "--workdir", "work"],
                {"prompt": "p", "container": {"workdir": "work"}},
                "the container the flags describe cannot be: the working directory must be",
                "task file: container: the working directory must be absolute",
            ),
            (
                ["--prompt", "p", "--cap", "NET_RAW"],
                {"prompt": "p", "container": {"capabilities": ["NET_RAW"]}},
                "the container the flags describe cannot be: capability NET_RAW is refused",
                "task file: container: capability NET_RAW is refused",
            ),
        ],
    )
    def test_what_a_file_may_not_say_a_flag_may_not_say_either(
        self, monkeypatch, tmp_path, capsys, flags, data, the_flags_are_told, the_file_is_told
    ):
        """``--max-turns 0`` used to start a container and end without a model
        call; the same value in a task file was refused."""
        monkeypatch.setattr(cli, "run_episode", _never)
        assert cli.main([*self.BASE, *flags]) == 3
        assert the_flags_are_told in capsys.readouterr().err
        path = tmp_path / "task.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert cli.main([*self.BASE, "--task", str(path)]) == 3
        assert the_file_is_told in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("flags", "said"),
        [
            (["--file", "notes.txt"], "--file takes PATH=CONTENT, and 'notes.txt' has no ="),
            (["--env", "NAME"], "--env takes NAME=VALUE, and 'NAME' has no ="),
        ],
    )
    def test_half_a_pair_is_refused(self, monkeypatch, capsys, flags, said):
        monkeypatch.setattr(cli, "run_episode", _never)
        assert cli.main([*self.BASE, "--prompt", "p", *flags]) == 3
        assert said in capsys.readouterr().err

    def test_a_task_the_flags_describe_whole_is_run(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        box = _DirSandbox()
        real = cli.run_episode
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c: real(t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True),
        )
        with ScriptedChatServer(SCRIPT) as server:
            flags = ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            flags += ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            flags += ["--sampler-logprobs", "yes", "--out", str(out)]
            flags += ["--file", "seed.txt=a=b", "--env", "MODE=x=y", "--max-turns", "4"]
            assert cli.main(flags) == 0
        task = json.loads(out.read_text(encoding="ascii"))["task"]
        assert task["setup_files"] == {"seed.txt": "a=b"} and task["max_turns"] == 4
        assert task["container"]["environment"] == {"MODE": "x=y"}
        capsys.readouterr()


class TestNumbersBelowTheLeast:
    """A number of the run's own below the least it can be is refused with
    status 3 before anything runs, whichever way the task is given, by the
    check a run from Python makes."""

    def _refused(
        self, monkeypatch: Any, tmp_path: Any, capsys: Any, task_by: str, *flags: str
    ) -> str:
        out = tmp_path / "episode.json"
        box = _DirSandbox()
        real = cli.run_episode
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c, **check: real(
                t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True, **check
            ),
        )
        task = ["--prompt", "go"]
        if task_by == "a file":
            path = tmp_path / "task.json"
            path.write_text(json.dumps({"prompt": "go"}), encoding="utf-8")
            task = ["--task", str(path)]
        with ScriptedChatServer(SCRIPT) as server:
            argv = ["--base-url", server.base_url, "--model", "scripted", *task]
            argv += ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            argv += ["--sampler-logprobs", "yes", "--out", str(out), *flags]
            assert cli.main(argv) == 3
            assert server.requests == []
        # No sandbox was started, and no record is there.
        assert box.root == "" and box.closed == 0 and not out.exists()
        return str(capsys.readouterr().err)

    @pytest.mark.parametrize("task_by", ["flags", "a file"])
    @pytest.mark.parametrize(
        ("flag", "value", "said"), [(c[0], c[1], c[3]) for c in BELOW_THE_LEAST]
    )
    def test_the_command_refuses_it_before_anything_runs(
        self, monkeypatch, tmp_path, capsys, task_by, flag, value, said
    ):
        """``--context-window 0`` used to end as the model's ``length`` with
        status 0; ``--max-tokens 0`` and ``--temperature -1`` started a
        container first; the others ran as if nothing were wrong."""
        assert said in self._refused(monkeypatch, tmp_path, capsys, task_by, flag, value)

    @pytest.mark.parametrize("seconds", ["0", "-5"])
    def test_a_verifier_command_given_no_time_is_refused(
        self, monkeypatch, tmp_path, capsys, seconds
    ):
        """The whole rollout used to run first and was then lost to a check
        that "did not end within 0 s".  The program named is not there."""
        flags = ["--verifier-command", f"{tmp_path}/no-such-grader {{key}}"]
        said = self._refused(
            monkeypatch, tmp_path, capsys, "flags", *flags, "--verifier-timeout", seconds
        )
        assert "verifier_timeout must be above zero" in said


NAN, INFINITY = float("nan"), float("inf")


class TestNumbersThatAreNone:
    """``NaN`` and the infinities are numbers to the interpreter and to its
    reader of JSON.  They are refused wherever the command reads a number, and
    none is written into a record: a record is strict JSON."""

    BASE = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m"]

    @pytest.mark.parametrize(
        ("line", "said"),
        [
            ('"max_command_seconds": NaN', "NaN is no number in JSON"),
            ('"max_command_seconds": Infinity', "Infinity is no number in JSON"),
            ('"max_generated_tokens": -Infinity', "-Infinity is no number in JSON"),
            ('"container": {"cpus": 1e999}', "1e999 is too large to be a number"),
        ],
    )
    def test_a_task_file_that_holds_one_is_refused(self, monkeypatch, tmp_path, capsys, line, said):
        """One used to be a traceback out of the budgets; the other ran and was
        written into the record twice."""
        path = tmp_path / "task.json"
        path.write_text('{"prompt": "Say hi.", ' + line + "}", encoding="utf-8")
        with pytest.raises(SetupError, match="is not JSON") as refusal:
            load_task(str(path))
        assert said in str(refusal.value)
        monkeypatch.setattr(cli, "run_episode", _never)
        out = tmp_path / "episode.json"
        assert cli.main([*self.BASE, "--task", str(path), "--out", str(out)]) == 3
        assert said in capsys.readouterr().err and not out.exists()

    @pytest.mark.parametrize("value", [NAN, INFINITY, -INFINITY])
    def test_a_task_given_as_data_is_held_to_the_same(self, value):
        for data, where in (
            ({"prompt": "p", "max_command_seconds": value}, "max_command_seconds"),
            ({"prompt": "p", "container": {"cpus": value}}, "container.cpus"),
        ):
            assert f"{where}: must be a finite number, not {value}" in _refused(data)

    @pytest.mark.parametrize(
        "flag",
        ["--max-command-seconds", "--cpus", "--temperature", "--top-p", "--verifier-timeout"]
        + ["--min-tokens-per-second", "--min-prompt-tokens-per-second", "--server-stall-seconds"],
    )
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "NaN", "Infinity"])
    def test_a_flag_that_is_given_one_is_refused(self, capsys, flag, value):
        with pytest.raises(SystemExit) as stopped:
            cli.main([*self.BASE, "--prompt", "p", f"{flag}={value}"])
        assert stopped.value.code == 3
        assert f"argument {flag}: {value!r} is no finite number" in capsys.readouterr().err

    def test_a_flag_that_is_given_no_number_says_so(self, capsys):
        with pytest.raises(SystemExit) as stopped:
            cli.main([*self.BASE, "--prompt", "p", "--cpus", "two"])
        assert stopped.value.code == 3
        assert "argument --cpus: 'two' is no number" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("flag", "value", "said"),
        [
            ("--extra-body", '{"top_p": NaN}', "--extra-body is not JSON"),
            ("--extra-body", '{"min_p": 1e999}', "--extra-body is not JSON"),
            ("--sampler-logprobs", '{"reflects": {}, "likeliest": -Infinity}', "--sampler-logp"),
        ],
    )
    def test_json_given_on_the_command_line_is_read_as_json_and_nothing_more(
        self, monkeypatch, capsys, flag, value, said
    ):
        monkeypatch.setattr(cli, "run_episode", _never)
        assert cli.main([*self.BASE, "--prompt", "p", flag, value]) == 3
        assert said in capsys.readouterr().err

    def test_a_configuration_file_that_writes_one_among_the_request_parameters_is_refused(
        self, monkeypatch, tmp_path, capsys
    ):
        """TOML can write ``inf`` and ``nan``."""
        config = tmp_path / "config.toml"
        config.write_text(MODEL_DEFINITION + "min_p = inf" + NL, encoding="utf-8")
        monkeypatch.setattr(config_module, "_cache", None)
        monkeypatch.setattr(config_module, "_config_path", None)
        monkeypatch.setattr(cli, "run_episode", _never)
        flags = ["--base-url", "http://127.0.0.1:9/v1", "--model", "scripted", "--prompt", "p"]
        assert cli.main([*flags, "--probe-only", "--config", str(config)]) == 3
        said = capsys.readouterr().err
        assert "the request parameters hold a number that is not finite (min_p; " in said
        assert "config.toml [models.scripted] capabilities.server_compat.extra_body" in said

    def test_a_limit_the_configuration_writes_as_one_is_no_limit(self, monkeypatch):
        """It is skipped like any value that is no usable number, and does not
        raise: the framework reads a definition the same way."""
        from turnstone.rollout.settings import resolve_limits, resolve_sampling

        definition = {"model": "scripted", "max_tokens": INFINITY, "temperature": NAN}
        monkeypatch.setattr(config_module, "_cache", {"models": {"scripted": definition}})
        asked = {"model": "scripted", "base_url": "http://127.0.0.1:9/v1"}
        limits = resolve_limits(
            **asked,
            max_tokens=None,
            context_window=200_000,
            tool_timeout=None,
            subagent_max_turns=None,
            max_output_chars=None,
        )
        assert limits["max_tokens"].source == "the framework's default for model.max_tokens"
        sampling = resolve_sampling(**asked, temperature=None, reasoning_effort=None, extra_body={})
        assert sampling["temperature"].value == 1.0
        with pytest.raises(SetupError, match="the temperature must be a finite number"):
            resolve_sampling(**asked, temperature=NAN, reasoning_effort=None, extra_body={})
        with pytest.raises(SetupError, match=r"not finite \(top_p; rollout config\)"):
            resolve_sampling(
                **asked, temperature=None, reasoning_effort=None, extra_body={"top_p": INFINITY}
            )

    def test_a_record_is_strict_json_whatever_reached_it(self, monkeypatch, tmp_path, capsys):
        """A server can report a log-probability that is no number, and a
        verifier can print one.  The record is written all the same, as JSON a
        strict reader takes, and says what it could not write."""
        out = tmp_path / "episode.json"
        box = _DirSandbox()
        real = cli.run_episode

        def with_such_numbers(task: Any, config: Any) -> dict[str, Any]:
            record = real(
                task, config, sandbox_factory=lambda _t, _c: box, allow_host_execution=True
            )
            record["tokens"]["calls"][0]["logprobs"][0] = NAN
            record["time"]["longest_server_silence"] = -INFINITY
            return record

        monkeypatch.setattr(cli, "run_episode", with_such_numbers)
        with ScriptedChatServer(SCRIPT) as server:
            flags = ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            flags += ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            status = cli.main([*flags, "--sampler-logprobs", "yes", "--out", str(out)])
        text = out.read_text(encoding="ascii")
        assert "NaN" not in text and "Infinity" not in text
        record = strict_json(text)
        assert record["tokens"]["calls"][0]["logprobs"][0] is None
        assert record["time"]["longest_server_silence"] is None
        (warning,) = record["warnings"]
        assert warning.startswith("2 of the record's values are no finite numbers and are written")
        assert "tokens.calls[0].logprobs[0]" in warning and "time.longest_server_silence" in warning
        # The status is the record's, and the summary says what was done.
        assert status == 0 and "warning: 2 of the record's values" in capsys.readouterr().out


class TestProbingOnce:
    """A caller with many rollouts probes the server once and passes the verdict on."""

    def test_the_probe_alone_prints_its_verdict(self, capsys):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            base = ["--base-url", server.base_url, "--model", "scripted"]
            assert cli.main([*base, "--probe-only", "--top-k", "20", "--top-p", "0.95"]) == 0
            probes = len(server.requests)
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["sampler_logprobs"] is True and probes == 5
        assert verdict["reflects"] == {"temperature": True, "top_k": True, "top_p": True}

    def test_a_server_reporting_the_models_own_has_another_status(self, capsys):
        with ScriptedChatServer(SCRIPT) as server:
            base = ["--base-url", server.base_url, "--model", "scripted"]
            assert cli.main([*base, "--probe-only"]) == 2
        assert json.loads(capsys.readouterr().out)["sampler_logprobs"] is False

    def test_what_it_printed_is_passed_to_a_rollout(self, monkeypatch, tmp_path, capsys):
        verdict = tmp_path / "probe.json"
        out = tmp_path / "episode.json"
        box = _DirSandbox()
        real = cli.run_episode
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            base = ["--base-url", server.base_url, "--model", "scripted"]
            base += ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            assert cli.main([*base, "--probe-only", "--out", str(verdict)]) == 0
            probes = len(server.requests)
            monkeypatch.setattr(
                cli,
                "run_episode",
                lambda t, c: real(
                    t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True
                ),
            )
            run = [*base, "--prompt", "go", "--out", str(out)]
            # As a file, and as the text itself.
            assert cli.main([*run, "--sampler-logprobs", f"@{verdict}"]) == 0
            assert cli.main([*run, "--sampler-logprobs", verdict.read_text()]) == 0
            # No rollout probed again: three model calls each.
            assert len(server.requests) == probes + 6
        said = json.loads(out.read_text(encoding="ascii"))["logprobs"]
        assert said == {
            "sampler_logprobs": True,
            "reflects": {"temperature": True, "top_k": True, "top_p": True},
            "reason": "given",
        }
        capsys.readouterr()

    def test_an_earlier_verdict_stays_until_the_new_one_takes_its_place(
        self, monkeypatch, tmp_path, capsys
    ):
        """A batch's verdict is made again while its rollouts read the file.
        It used to be removed before the probe began, and for the seconds a
        probe takes every rollout that read it was refused."""
        verdict = tmp_path / "server.json"
        verdict.write_text('{"an earlier": "verdict"}', encoding="ascii")
        seen: list[Any] = []
        real = cli._logprob_verdict

        def probes(config: Any) -> Any:
            # What a rollout of the batch reads while the probe is under way.
            seen.append(verdict.read_text(encoding="ascii"))
            seen.append(os.listdir(tmp_path))
            return real(config)

        monkeypatch.setattr(cli, "_logprob_verdict", probes)
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            base = ["--base-url", server.base_url, "--model", "scripted"]
            base += ["--top-k", "20", "--top-p", "0.95"]
            assert cli.main([*base, "--probe-only", "--out", str(verdict)]) == 0
        assert seen == ['{"an earlier": "verdict"}', ["server.json"]]
        assert json.loads(verdict.read_text(encoding="ascii"))["sampler_logprobs"] is True
        assert os.listdir(tmp_path) == ["server.json"]
        capsys.readouterr()

    def test_a_probe_that_ends_without_a_verdict_leaves_no_earlier_one(
        self, monkeypatch, tmp_path, capsys
    ):
        """When the command has ended, the file is its verdict or is not there."""
        verdict = tmp_path / "server.json"
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--probe-only"]
        base += ["--out", str(verdict)]
        # Refused: nothing is asked of the server.
        verdict.write_text("{}", encoding="ascii")
        monkeypatch.setattr(cli, "_logprob_verdict", _never)
        assert cli.main([*base, "--extra-body", "{no json"]) == 3
        assert not verdict.exists()

        # It died.
        def dies(_config: Any) -> Any:
            raise RuntimeError("the probe died here")

        verdict.write_text("{}", encoding="ascii")
        monkeypatch.setattr(cli, "_logprob_verdict", dies)
        with pytest.raises(RuntimeError, match="the probe died here"):
            cli.main(base)
        assert not verdict.exists()

        # Its verdict could not be written.
        def fails(_path: str, _text: str) -> None:
            raise OSError(28, "No space left on device")

        verdict.write_text("{}", encoding="ascii")
        monkeypatch.setattr(cli, "_logprob_verdict", lambda _config: {"sampler_logprobs": True})
        monkeypatch.setattr(cli, "_write_whole", fails)
        assert cli.main(base) == 1
        assert os.listdir(tmp_path) == []
        assert "the verdict could not be written" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "said",
        [
            "maybe",
            "{}",
            '{"top_k": true}',
            '{"temperature": 1, "top_k": 1, "top_p": 1}',
            "@/no/such/file",
        ],
    )
    def test_a_verdict_that_is_none_is_refused(self, monkeypatch, capsys, said):
        monkeypatch.setattr(cli, "run_episode", _never)
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p"]
        assert cli.main([*base, "--sampler-logprobs", said]) == 3
        assert "--sampler-logprobs" in capsys.readouterr().err
