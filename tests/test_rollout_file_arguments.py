"""Arguments of a sidecar's tools that take the bytes of a file as base64, given
by the model as a file in its own sandbox (:mod:`turnstone.rollout.file_arguments`).

The client and the sandbox are stand-ins here; ``tests/test_rollout_sidecar.py``
runs a rollout with a real sidecar.  Reading all of a file runs the
environment's own helper script on this machine, which takes GNU tools.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import mcp.types as mcp_types
import pytest

from turnstone.rollout import container as container_module
from turnstone.rollout.container import (
    Completed,
    ContainerEnvironment,
    ContainerLostError,
    ContainerSpec,
)
from turnstone.rollout.episode import ContainerSandbox, RolloutConfig, Task
from turnstone.rollout.file_arguments import (
    MAX_FILE_BYTES,
    PREFIX,
    Base64FileArguments,
    FileArgumentError,
)
from turnstone.rollout.sidecar import Sidecar

if TYPE_CHECKING:
    from pathlib import Path

NL = chr(10)
TOOL = "mcp__grader__submit"


class _Client:
    """A sidecar's MCP client that keeps every call it is sent."""

    server_count = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def call_tool_sync(
        self, func_name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> mcp_types.CallToolResult:
        self.calls.append((func_name, arguments, kwargs))
        return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="ok")])

    def get_tools(self, user_id: str | None = None) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": TOOL}}]


class _Files:
    """The files of a model's sandbox, read as ``ContainerEnvironment.read_whole``
    reads them."""

    def __init__(
        self, files: dict[str, bytes] | None = None, error: BaseException | None = None
    ) -> None:
        self.files = files or {}
        self.error = error
        self.reads: list[tuple[str, int]] = []
        # What the client was told of failures of the machinery.
        self.failures: list[str] = []

    def failed(self, what: str) -> None:
        self.failures.append(what)

    def read_whole(self, path: str, limit: int) -> bytes:
        self.reads.append((path, limit))
        if self.error is not None:
            raise self.error
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        if len(self.files[path]) > limit:
            raise OSError(errno.EFBIG, "file is larger than the limit", path)
        return self.files[path]


def _wrapped(
    files: dict[str, bytes] | None = None,
    error: BaseException | None = None,
    arguments: dict[str, list[str]] | None = None,
) -> tuple[Base64FileArguments, _Client, _Files]:
    client, sandbox = _Client(), _Files(files, error)
    named = arguments if arguments is not None else {TOOL: ["poc_b64"]}
    return Base64FileArguments(client, named, sandbox.read_whole, sandbox.failed), client, sandbox


def _answer(result: mcp_types.CallToolResult) -> dict[str, Any]:
    """What a refusal tells the model: an ordinary result, one JSON object."""
    assert result.is_error is False
    (content,) = result.content
    assert isinstance(content, mcp_types.TextContent)
    answer: dict[str, Any] = json.loads(content.text)
    return answer


class TestAReferenceIsSentAsTheBytesOfTheFile:
    def test_the_server_gets_the_bytes_as_base64(self) -> None:
        poc = b"A" * 1359
        wrapper, client, sandbox = _wrapped({"/home/agent/poc.bin": poc})
        wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///home/agent/poc.bin"}, timeout=300)
        ((name, sent, kwargs),) = client.calls
        assert name == TOOL and kwargs == {"timeout": 300}
        assert base64.b64decode(sent["poc_b64"], validate=True) == poc
        assert sandbox.reads == [("/home/agent/poc.bin", MAX_FILE_BYTES)]

    def test_any_bytes_arrive_as_they_are(self) -> None:
        data = bytes(range(256)) * 4
        wrapper, client, _ = _wrapped({"/p": data})
        wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///p"})
        assert base64.b64decode(client.calls[0][1]["poc_b64"], validate=True) == data

    def test_an_empty_file_is_empty_base64(self) -> None:
        wrapper, client, _ = _wrapped({"/p": b""})
        wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///p"})
        assert client.calls[0][1]["poc_b64"] == ""

    def test_what_the_model_wrote_is_left_as_it_wrote_it(self) -> None:
        """The session keeps the arguments it parsed (for the record, and to
        replay the call): the server is sent a copy."""
        wrapper, client, _ = _wrapped({"/home/agent/poc.bin": b"xyz"})
        arguments = {"poc_b64": "file:///home/agent/poc.bin", "note": "first try"}
        wrapper.call_tool_sync(TOOL, arguments)
        assert arguments == {"poc_b64": "file:///home/agent/poc.bin", "note": "first try"}
        sent = client.calls[0][1]
        assert sent is not arguments
        assert sent == {"poc_b64": base64.b64encode(b"xyz").decode(), "note": "first try"}

    def test_each_named_argument_is_read(self) -> None:
        wrapper, client, sandbox = _wrapped(
            {"/a": b"a", "/b": b"b"}, arguments={TOOL: ["poc_b64", "extra_b64"]}
        )
        wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///a", "extra_b64": "file:///b"})
        assert client.calls[0][1] == {"poc_b64": "YQ==", "extra_b64": "Yg=="}
        assert [path for path, _ in sandbox.reads] == ["/a", "/b"]


class TestNothingElseIsLookedAt:
    def test_inline_base64_goes_on_as_written(self) -> None:
        wrapper, client, sandbox = _wrapped()
        inline = base64.b64encode(b"AAAA").decode()
        wrapper.call_tool_sync(TOOL, {"poc_b64": inline})
        assert client.calls[0][1] == {"poc_b64": inline}
        assert sandbox.reads == []

    def test_another_tool_is_sent_its_arguments_as_they_are(self) -> None:
        wrapper, client, sandbox = _wrapped()
        arguments = {"url": "file:///home/agent/index.html"}
        wrapper.call_tool_sync("mcp__grader__open", arguments)
        assert client.calls[0][1] is arguments
        assert sandbox.reads == []

    def test_another_argument_of_the_tool_is_not_read(self) -> None:
        wrapper, client, _ = _wrapped({"/a": b"x"})
        wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///a", "label": "file:///a"})
        assert client.calls[0][1] == {"poc_b64": "eA==", "label": "file:///a"}

    @pytest.mark.parametrize("value", [["file:///a"], {"path": "file:///a"}, 7, None])
    def test_a_value_that_is_no_text_goes_on_as_it_is(self, value: Any) -> None:
        wrapper, client, sandbox = _wrapped({"/a": b"x"})
        wrapper.call_tool_sync(TOOL, {"poc_b64": value})
        assert client.calls[0][1] == {"poc_b64": value}
        assert sandbox.reads == []

    @pytest.mark.parametrize("value", [" file:///a", "FILE:///a", "file:/a", "/a"])
    def test_only_a_value_that_begins_with_the_prefix_is_a_reference(self, value: str) -> None:
        wrapper, client, sandbox = _wrapped({"/a": b"x"})
        wrapper.call_tool_sync(TOOL, {"poc_b64": value})
        assert client.calls[0][1] == {"poc_b64": value}
        assert sandbox.reads == []

    def test_a_missing_argument_is_left_missing(self) -> None:
        wrapper, client, _ = _wrapped()
        wrapper.call_tool_sync(TOOL, {})
        assert client.calls[0][1] == {}

    def test_everything_but_a_tool_call_is_the_clients(self) -> None:
        wrapper, client, _ = _wrapped()
        assert wrapper.get_tools() == client.get_tools()
        assert wrapper.server_count == 1


class TestAReferenceTheModelHasToFix:
    """The model is told what is wrong, as a tool answers an argument it cannot
    use, and the server is not called."""

    @pytest.mark.parametrize("path", ["home/agent/poc.bin", "", "localhost/home/agent/poc.bin"])
    def test_a_path_that_is_not_absolute(self, path: str) -> None:
        wrapper, client, sandbox = _wrapped()
        result = wrapper.call_tool_sync(TOOL, {"poc_b64": PREFIX + path})
        assert _answer(result) == {
            "error": "path_not_absolute",
            "argument": "poc_b64",
            "path": path,
        }
        assert client.calls == [] and sandbox.reads == [] and sandbox.failures == []

    @pytest.mark.parametrize(
        ("error", "said"),
        [
            (FileNotFoundError(errno.ENOENT, "No such file or directory", "/p"), "file_not_found"),
            (IsADirectoryError(errno.EISDIR, "Is a directory", "/p"), "is_a_directory"),
            (OSError(errno.EINVAL, "not a regular file", "/p"), "not_a_regular_file"),
            (PermissionError(errno.EACCES, "Permission denied", "/p"), "not_readable"),
        ],
    )
    def test_a_file_that_cannot_be_read(self, error: OSError, said: str) -> None:
        wrapper, client, sandbox = _wrapped(error=error)
        result = wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///p"})
        assert _answer(result) == {"error": said, "argument": "poc_b64", "path": "/p"}
        assert client.calls == [] and sandbox.failures == []

    def test_a_file_larger_than_the_limit(self) -> None:
        wrapper, client, _ = _wrapped(error=OSError(errno.EFBIG, "file is larger", "/p"))
        result = wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///p"})
        assert _answer(result) == {
            "error": "file_too_large",
            "argument": "poc_b64",
            "path": "/p",
            "max_bytes": MAX_FILE_BYTES,
        }
        assert client.calls == []

    def test_one_bad_reference_stops_the_whole_call(self) -> None:
        wrapper, client, _ = _wrapped({"/a": b"a"}, arguments={TOOL: ["poc_b64", "extra_b64"]})
        result = wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///a", "extra_b64": "file:///x"})
        assert _answer(result)["error"] == "file_not_found"
        assert client.calls == []


class TestAFailureOfTheMachineryStopsTheRun:
    """It is not the model's to see or to work around: the client is told (and
    stops the run as the machinery's), and the call is not made."""

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError(errno.ETIMEDOUT, "operation did not finish within 60 s", "/p"),
            ContainerLostError(errno.EIO, "the sandbox has stopped"),
            # Lost is lost, whatever errno it carries.
            ContainerLostError(errno.ENOENT, "the sandbox has stopped"),
            OSError(errno.EIO, "Input/output error", "/p"),
            OSError(errno.ENOSPC, "No space left on device", "/p"),
            RuntimeError("the runtime did not answer"),
        ],
    )
    def test_the_client_is_told_and_the_server_is_not_called(self, error: BaseException) -> None:
        wrapper, client, sandbox = _wrapped(error=error)
        with pytest.raises(FileArgumentError) as raised:
            wrapper.call_tool_sync(TOOL, {"poc_b64": "file:///p"})
        assert raised.value.__cause__ is error
        (what,) = sandbox.failures
        assert str(raised.value) == what
        assert what.startswith(
            f"the machinery could not read /p, which the model named for {TOOL} (poc_b64): "
            f"{type(error).__name__}: "
        )
        assert client.calls == []


def _sandbox(task: Task | None = None, **sidecar_settings: Any) -> ContainerSandbox:
    if task is None:
        sidecar = Sidecar(
            container=ContainerSpec(user="0:0", read_only_root=False),
            mcp_servers={"grader": "http://127.0.0.1:39101/mcp"},
            **sidecar_settings,
        )
        task = Task(prompt="p", sidecar=sidecar)
    return ContainerSandbox(task, RolloutConfig(base_url="http://127.0.0.1:9/v1", model="m"))


class TestTheSandboxHandsOutTheClient:
    def test_without_named_arguments_it_is_the_sidecars_own(self) -> None:
        sandbox = _sandbox()
        client = _Client()
        assert sandbox.sidecar is not None
        sandbox.sidecar.mcp_client = client
        assert sandbox.mcp_client is client

    def test_with_named_arguments_references_are_read_from_its_own_container(self) -> None:
        sandbox = _sandbox(base64_file_arguments={TOOL: ["poc_b64"]})
        client, files = _Client(), _Files({"/home/agent/poc.bin": b"A" * 1359})
        assert sandbox.sidecar is not None
        sandbox.sidecar.mcp_client = client
        sandbox.environment = files  # type: ignore[assignment]
        handed = sandbox.mcp_client
        assert isinstance(handed, Base64FileArguments)
        handed.call_tool_sync(TOOL, {"poc_b64": "file:///home/agent/poc.bin"})
        assert base64.b64decode(client.calls[0][1]["poc_b64"]) == b"A" * 1359
        assert files.reads == [("/home/agent/poc.bin", MAX_FILE_BYTES)]

    def test_before_the_sidecar_is_connected_there_is_none(self) -> None:
        assert _sandbox(base64_file_arguments={TOOL: ["poc_b64"]}).mcp_client is None

    def test_without_a_sidecar_there_is_none(self) -> None:
        assert _sandbox(Task(prompt="p")).mcp_client is None

    def test_a_failure_of_the_machinery_is_kept_and_stops_the_run(self) -> None:
        sandbox = _sandbox(base64_file_arguments={TOOL: ["poc_b64"]})
        stops: list[bool] = []
        assert sandbox.sidecar is not None
        sandbox.sidecar.mcp_client = _Client()
        sandbox.environment = _Files(error=OSError(errno.EIO, "Input/output error", "/p"))  # type: ignore[assignment]
        sandbox.notify_lost(lambda: stops.append(True))
        with pytest.raises(FileArgumentError):
            sandbox.mcp_client.call_tool_sync(TOOL, {"poc_b64": "file:///p"})
        assert sandbox.file_argument_failure is not None
        assert "OSError: [Errno 5] Input/output error" in sandbox.file_argument_failure
        assert stops == [True]

    def test_a_reference_the_model_has_to_fix_does_not_stop_the_run(self) -> None:
        sandbox = _sandbox(base64_file_arguments={TOOL: ["poc_b64"]})
        stops: list[bool] = []
        assert sandbox.sidecar is not None
        sandbox.sidecar.mcp_client = _Client()
        sandbox.environment = _Files()  # type: ignore[assignment]
        sandbox.notify_lost(lambda: stops.append(True))
        result = sandbox.mcp_client.call_tool_sync(TOOL, {"poc_b64": "file:///gone"})
        assert _answer(result)["error"] == "file_not_found"
        assert sandbox.file_argument_failure is None and stops == []

    def test_one_asked_for_before_the_sandbox_started_is_refused(self) -> None:
        sandbox = _sandbox(base64_file_arguments={TOOL: ["poc_b64"]})
        assert sandbox.sidecar is not None
        sandbox.sidecar.mcp_client = _Client()
        with pytest.raises(RuntimeError, match="has not started"):
            _ = sandbox.mcp_client


class _HereContainer:
    """Runs the environment's helper scripts on this machine, where a container
    would run them inside."""

    workdir = "/"
    user = "65534:65534"
    lost = None

    def exec(self, argv: list[str], **kwargs: Any) -> Completed:
        if argv[2] == container_module._SPAWN_SCRIPT:
            return Completed(0, b"turnstone-shell-tool-check" + NL.encode(), b"", False, 27)
        if argv[2] != container_module._READ_SCRIPT:
            return Completed(0, b"", b"", False, 0)
        done = subprocess.run(argv, capture_output=True, timeout=30, check=False)
        return Completed(done.returncode, done.stdout, done.stderr, False, len(done.stdout))

    def ensure_running(self) -> None:
        return None


def _environment() -> ContainerEnvironment:
    return ContainerEnvironment(_HereContainer(), max_file_bytes=64)  # type: ignore[arg-type]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs the helper's GNU tools")
class TestReadingAllOfAFile:
    def test_a_regular_file_is_read_whole(self, tmp_path: Path) -> None:
        data = bytes(range(256)) * 2
        (tmp_path / "poc.bin").write_bytes(data)
        assert _environment().read_whole(str(tmp_path / "poc.bin"), len(data)) == data

    def test_a_larger_file_is_refused_and_the_callers_limit_named(self, tmp_path: Path) -> None:
        (tmp_path / "poc.bin").write_bytes(b"A" * 101)
        with pytest.raises(OSError) as refused:
            _environment().read_whole(str(tmp_path / "poc.bin"), 100)
        assert refused.value.errno == errno.EFBIG
        assert "larger than 100 bytes" in str(refused.value)

    def test_read_text_still_names_its_own_limit(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("x" * 65)
        with pytest.raises(OSError) as refused:
            _environment().read_text(str(tmp_path / "notes.txt"))
        assert refused.value.errno == errno.EFBIG
        assert "larger than 64 bytes" in str(refused.value)

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _environment().read_whole(str(tmp_path / "gone"), 100)

    def test_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(IsADirectoryError):
            _environment().read_whole(str(tmp_path), 100)

    def test_a_fifo_is_not_waited_on(self, tmp_path: Path) -> None:
        """Nothing writes to it, so a read would wait: all of a file is a
        regular file's."""
        os.mkfifo(tmp_path / "pipe")
        with pytest.raises(OSError) as refused:
            _environment().read_whole(str(tmp_path / "pipe"), 100)
        assert refused.value.errno == errno.EINVAL

    @pytest.mark.skipif(os.geteuid() == 0, reason="root may read any file")
    def test_a_file_that_may_not_be_read(self, tmp_path: Path) -> None:
        path = tmp_path / "secret"
        path.write_bytes(b"x")
        path.chmod(0)
        with pytest.raises(PermissionError):
            _environment().read_whole(str(path), 100)
