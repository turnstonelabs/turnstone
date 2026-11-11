"""One bound on the size of a single tool result, whatever the tool.

The first classes need no container: the cut itself, and rollouts in the
directory stand-in of ``test_rollout_episode.py`` with harmless commands.  The
last bounds what a tool of a task's own servers returns, in containers made for
the test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from tests.test_rollout_container import IMAGE, SMALL, _listed, needs_container, note_containers
from tests.test_rollout_episode import _DirSandbox, _run
from tests.test_rollout_sidecar import SERVER, _sidecar
from turnstone.core import config as config_module
from turnstone.core.truncation import BoundedTextBuffer
from turnstone.rollout import cli
from turnstone.rollout.container import ContainerSpec
from turnstone.rollout.episode import RolloutConfig, Task, run_episode
from turnstone.rollout.result_bound import (
    MIN_RESULT_CHARS,
    bound_result,
    bound_text,
    sendable_result,
    sendable_text,
)
from turnstone.rollout.settings import SetupError

NL = chr(10)
TAB = chr(9)


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


def _numbered(lines: int, start: int = 1) -> str:
    """What ``read_file`` returns for a file of that many lines."""
    return NL.join(f"{n:>4}{TAB}line {n} of the file" for n in range(start, start + lines))


class TestTheCut:
    def test_a_result_within_the_bound_is_left_as_it_is(self):
        assert bound_text("bash", "x" * 2000, 2000) is None
        assert bound_text("read_file", _numbered(10), 2000) is None

    def test_a_file_read_keeps_whole_lines_and_says_where_to_read_on(self):
        text = _numbered(5000)
        cut = bound_text("read_file", text, 2000)
        assert cut is not None and len(cut) <= 2000
        shown, note = cut.split(NL + NL + "[read_file: ")
        # Whole lines from the start, the last one complete.
        lines = shown.split(NL)
        assert lines == text.split(NL)[: len(lines)]
        last = len(lines)
        assert note == (
            f"the result stops after line {last}. {len(text) - len(shown)} of its {len(text)} "
            f"characters are left out: one tool result holds at most 2000. Read on with "
            f"offset={last + 1} and a limit.]"
        )

    def test_a_part_of_a_file_names_the_files_own_line(self):
        cut = bound_text("read_file", _numbered(5000, start=700), 2000)
        assert cut is not None
        last = int(cut.split(NL + NL + "[read_file: ")[0].split(NL)[-1].split(TAB)[0])
        assert last > 700 and f"Read on with offset={last + 1} and a limit.]" in cut

    def test_one_line_longer_than_a_result_may_be(self):
        cut = bound_text("read_file", "   1" + TAB + "x" * 9000, 2000)
        assert cut is not None and len(cut) <= 2000
        assert cut.endswith("a line longer than that is read in pieces with a shell command.]")
        assert "7405 of its 9005 characters are left out" in cut

    @pytest.mark.parametrize(
        ("tool", "how"),
        [
            ("bash", "Narrow the command, or send its output to a file and read part of it."),
            ("bash_output", "Read less at a time: give bash_output a filter."),
            ("search", "Narrow the query or the path."),
            ("diff_file", "Compare less at a time."),
            ("task_agent", "Ask the sub-agent for a shorter answer, or have it write its"),
            ("mcp__ledger__list_entries", "Ask for less: narrower arguments, fewer items, a page"),
            ("some_other_tool", "Ask for less at a time."),
        ],
    )
    def test_any_other_result_keeps_its_beginning_and_its_end(self, tool, how):
        text = NL.join(f"output line {n}" for n in range(3000))
        cut = bound_text(tool, text, 3000)
        assert cut is not None and len(cut) <= 3000
        head, rest = cut.split(NL + NL + "[... ", 1)
        note, tail = rest.split(" ...]" + NL + NL, 1)
        assert text.startswith(head) and text.endswith(tail) and tail.endswith("line 2999")
        left_out = len(text) - len(head) - len(tail)
        assert note.startswith(
            f"{left_out} of {len(text)} characters left out here: one tool result holds at "
            f"most 3000. {how}"
        )

    def test_a_commands_output_is_counted_whole(self):
        """What a command printed is already the rendering of a bounded capture.  The
        cut is made from what the capture kept, and counts what the command wrote."""
        capture = BoundedTextBuffer(5000)
        for n in range(100_000):
            capture.append(f"row {n}{NL}")
        rendered = capture.projected() + NL + "[exit code: 3]"
        whole = sum(len(f"row {n}{NL}") for n in range(100_000)) + len(NL + "[exit code: 3]")
        cut = bound_text("bash", rendered, 2000)
        assert cut is not None and len(cut) <= 2000
        assert cut.startswith("row 0" + NL) and cut.endswith("[exit code: 3]")
        assert f"of {whole} characters left out here" in cut
        assert bound_result("bash", rendered, 2000) == (cut, whole)

    def test_the_cut_never_passes_the_bound_and_is_the_same_each_time(self):
        for limit in (MIN_RESULT_CHARS, 1001, 1500, 4096, 32768):
            for size in (limit + 1, limit + 399, 3 * limit, 400_000):
                for tool in ("read_file", "bash", "mcp__x__y"):
                    text = _numbered(size // 20 + 1)[:size]
                    cut = bound_text(tool, text, limit)
                    assert cut is not None and len(cut) <= limit, (tool, limit, size)
                    assert bound_text(tool, text, limit) == cut

    def test_a_result_of_parts_has_its_text_cut_and_the_rest_left(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        parts = [{"type": "text", "text": "t" * 5000}, image]
        cut, size = bound_result("mcp__x__picture", parts, 2000)
        assert size == 5000 and len(cut[0]["text"]) <= 2000 and cut[1] is image
        assert parts[0]["text"] == "t" * 5000  # the result handed in is not changed
        assert bound_result("mcp__x__picture", [image], 2000) == ([image], None)

    def test_what_is_no_text_is_left_alone(self):
        assert bound_result("bash", None, 2000) == (None, None)
        assert bound_result("bash", "short", 2000) == ("short", None)


HIGH, LOW, REPLACED = chr(0xD83D), chr(0xDE00), chr(0xFFFD)


class TestWhatARequestCanCarry:
    """Half of a surrogate pair cannot be written in a request body."""

    def test_text_that_can_be_sent_is_returned_as_it_is(self):
        for text in ("", "plain", "caf" + chr(0xE9), chr(0x1F600) + " and " + chr(0), REPLACED):
            assert sendable_text(text) == (text, 0)
        whole = "x" * 100_000
        assert sendable_result(whole)[0] is whole

    def test_each_half_that_stands_alone_is_replaced(self):
        assert sendable_text("a" + HIGH + "b") == ("a" + REPLACED + "b", 1)
        assert sendable_text(LOW + "a" + LOW) == (REPLACED + "a" + REPLACED, 2)
        # The wrong way round, each is alone.
        assert sendable_text(LOW + HIGH) == (REPLACED + REPLACED, 2)
        repaired, _halves = sendable_text("a" + HIGH + chr(0x1F600) + "b")
        assert repaired == "a" + REPLACED + chr(0x1F600) + "b"
        repaired.encode("utf-8")

    def test_two_halves_that_stand_together_are_the_character_they_spell(self):
        assert sendable_text("a" + HIGH + LOW + "b") == ("a" + chr(0x1F600) + "b", 2)

    def test_the_same_text_gives_the_same_result(self):
        text = ("line " + HIGH + NL) * 500
        assert sendable_text(text) == sendable_text(text)
        assert sendable_text(text)[1] == 500

    def test_a_result_of_parts_has_its_text_repaired_and_the_rest_left(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        parts = [{"type": "text", "text": "t" + HIGH}, image, {"type": "text", "text": "fine"}]
        repaired, halves = sendable_result(parts)
        assert halves == 1 and repaired[0] == {"type": "text", "text": "t" + REPLACED}
        assert repaired[1] is image and repaired[2] is parts[2]
        assert parts[0]["text"] == "t" + HIGH  # the result handed in is not changed
        assert sendable_result([image]) == ([image], 0)
        assert sendable_result(None) == (None, 0) and sendable_result(7) == (7, 0)


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def _tool_messages(server: ScriptedChatServer) -> list[str]:
    return [m["content"] for m in server.parent_requests[-1]["messages"] if m["role"] == "tool"]


BIG = NL.join(f"line {n} of the big file" for n in range(1, 4001)) + NL
SCRIPT: list[dict[str, Any]] = [
    {"tool_calls": [_call("bash", command="seq 1 5000")]},
    {"tool_calls": [_call("read_file", path="big.txt")]},
    {"tool_calls": [_call("read_file", path="big.txt", offset=56, limit=3)]},
    {"tool_calls": [_call("task_agent", prompt="Report at length.")]},
    {"tool_calls": [_call("bash", command="echo short")]},
    {"content": "done"},
]
AGENT: list[dict[str, Any]] = [{"content": "finding " * 1500}]


def _bounded(**settings: Any) -> tuple[dict[str, Any], list[str]]:
    with ScriptedChatServer(SCRIPT, agent_script=AGENT) as server:
        record, _box = _run(
            server,
            task=Task(prompt="read a lot", setup_files={"big.txt": BIG}),
            # The scripted server counts a token for every character.
            context_window=4_000_000,
            max_tokens=1000,
            **settings,
        )
        return record, _tool_messages(server)


class TestInARollout:
    def test_no_result_enters_the_conversation_longer_than_the_bound(self):
        record, results = _bounded(max_tool_result_chars=2000)
        assert record["finish_state"] == "completed" and len(results) == 5
        assert all(len(result) <= 2000 for result in results)
        command, whole_file, part, delegated, short = results

        # A command: how it began and how it ended.
        assert command.startswith("1" + NL + "2" + NL) and command.endswith("4999" + NL + "5000")
        assert "characters left out here: one tool result holds at most 2000. Narrow the" in command
        # A file: whole lines, and where to read on, which the next call did.
        assert whole_file.startswith("   1" + TAB + "line 1 of the big file" + NL)
        assert "[read_file: the result stops after line 55." in whole_file
        assert whole_file.endswith("Read on with offset=56 and a limit.]")
        assert part == NL.join(f"{n:>4}{TAB}line {n} of the big file" for n in (56, 57, 58))
        # What a sub-agent handed back.
        assert (
            delegated.startswith("finding finding")
            and "Ask the sub-agent for a shorter" in delegated
        )
        assert short == "short"

        # A cut result is text from the environment like any other: the prompt
        # still extends, and the rollout is trained on.
        assert record["trainable"] is True and record["one_sequence"] is True

    def test_the_record_says_what_the_bound_was_and_what_was_cut(self):
        record, _results = _bounded(max_tool_result_chars=2000)
        assert record["limits"]["max_tool_result_chars"] == {
            "value": 2000,
            "source": "rollout config",
        }
        assert record["tool_results"] == {
            "cut": 3,
            "cuts": [
                {"tool": "bash", "chars": 23892},
                {"tool": "read_file", "chars": len(BIG) - 1 + 5 * 4000},
                {"tool": "task_agent", "chars": 12000},
            ],
        }
        log = record["tool_log"]
        assert [entry.get("cut_from") for entry in log] == [
            23892,
            len(BIG) - 1 + 5 * 4000,
            None,
            12000,
            None,
        ]

    def test_unset_nothing_changes(self):
        record, results = _bounded()
        assert record["limits"]["max_tool_result_chars"] == {
            "value": None,
            "source": "not set: each tool's own bound applies",
        }
        assert record["tool_results"] == {"cut": 0, "cuts": []}
        command, whole_file, _part, delegated, _short = results
        assert len(command) == 23892 and len(whole_file) == len(BIG) - 1 + 5 * 4000
        assert delegated == "finding " * 1500
        assert all("cut_from" not in entry for entry in record["tool_log"])

    def test_the_same_call_gives_the_same_result(self):
        script = [
            {
                "tool_calls": [
                    _call("read_file", path="big.txt"),
                    _call("read_file", path="big.txt"),
                ]
            },
            {"tool_calls": [_call("bash", command="seq 1 5000")]},
            {"tool_calls": [_call("bash", command="seq 1 5000")]},
            {"content": "done"},
        ]
        with ScriptedChatServer(script) as server:
            record, _box = _run(
                server,
                task=Task(prompt="twice", setup_files={"big.txt": BIG}),
                context_window=4_000_000,
                max_tokens=1000,
                max_tool_result_chars=1500,
            )
            first, second, third, fourth = _tool_messages(server)
        assert first == second and third == fourth and len(first) <= 1500
        assert record["tool_results"]["cut"] == 4 and record["one_sequence"] is True

    def test_a_bound_too_small_to_say_what_was_cut_is_refused(self):
        with ScriptedChatServer(SCRIPT) as server:
            with pytest.raises(SetupError, match="max_tool_result_chars must be at least 1000"):
                _run(server, max_tool_result_chars=999)
            assert server.requests == []

    def test_the_command_takes_it(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        box = _DirSandbox()
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c: run_episode(
                t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True
            ),
        )
        script = [{"tool_calls": [_call("bash", command="seq 1 5000")]}, {"content": "done"}]
        with ScriptedChatServer(script) as server:
            status = cli.main(
                ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
                + ["--top-k", "20", "--top-p", "0.95", "--context-window", "4000000"]
                + ["--max-tokens", "1000", "--sampler-logprobs", "yes", "--out", str(out)]
                + ["--max-tool-result-chars", "1200"]
            )
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["limits"]["max_tool_result_chars"]["value"] == 1200
        assert record["tool_results"]["cut"] == 1 and record["tool_log"][0]["cut_from"] == 23892
        capsys.readouterr()


@pytest.fixture(name="containers_made")
def _made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return note_containers(monkeypatch)


# The ledger of ``test_rollout_sidecar.py`` with a tool that returns a long text.
LONG = 'return socket.gethostname() + " " + ",".join(names)'
LONG_SERVER = SERVER.replace(LONG, 'return "".join("entry %d; " % n for n in range(20000))')


@needs_container
class TestAToolOfTheTasksOwnServers:
    def test_what_it_returns_is_bounded_like_any_other_result(self, containers_made):
        assert LONG_SERVER != SERVER
        script = [
            {"tool_calls": [{"name": "mcp__ledger__where", "arguments": {}}]},
            {"content": "listed"},
        ]
        task = Task(
            prompt="list",
            container=ContainerSpec(image=IMAGE, memory="512m"),
            sidecar=_sidecar(files={"/srv/ledger.py": LONG_SERVER.encode()}),
        )
        with ScriptedChatServer(script) as server:
            record = run_episode(
                task,
                RolloutConfig(
                    base_url=server.base_url,
                    model="scripted",
                    max_tool_result_chars=3000,
                    **{**SMALL, "context_window": 4_000_000, "max_tokens": 1000},
                ),
            )
            (result,) = _tool_messages(server)

        assert record["finish_state"] == "completed", record["error"]
        whole = len("".join(f"entry {n}; " for n in range(20000)))
        assert len(result) <= 3000
        assert result.startswith("entry 0; entry 1; ") and result.endswith("entry 19999; ")
        assert (
            f"of {whole} characters left out here: one tool result holds at most 3000. "
            "Ask for less: narrower arguments, fewer items, a page at a time."
        ) in result
        assert record["tool_results"] == {
            "cut": 1,
            "cuts": [{"tool": "mcp__ledger__where", "chars": whole}],
        }
        assert record["tool_log"][0]["cut_from"] == whole
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["sandbox"]["removed"] is True
        assert [name for name in containers_made if _listed(name)] == []
