"""The suite's guard on what the rollout package starts on the machine the
tests run on (``tests/_host_guard.py``, installed for every test by
``conftest.py``).

Nothing here hands a destructive command to anything.  The guard is shown to
refuse with a harmless program outside what it lets through (``true``,
``touch`` of a file under the test's temporary directory) and with the runtime
asked about a container and an image that nothing has.  If the guard did not
refuse, that is what would run.  Command lines that name somebody else's
container appear otherwise only as data for the functions that read a command
line, which start nothing.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from tests import _host_guard
from tests._host_guard import (
    DestructiveCommandError,
    Start,
    rollout_refusal,
    runtime_refusal,
    start_shape,
    started_program,
)
from tests.conftest import ROLLOUT_KEEPER_SCRIPT
from tests.test_host_guard import (
    PROBE,
    REPLACING,
    RETURNING,
    THROUGH_A_SHELL,
    TOUCH,
    TRUE,
    own_program,
    probe_module,
)
from turnstone.rollout import container as container_module
from turnstone.rollout.container import (
    ALLOWED_CAPABILITIES,
    NAME_PREFIX,
    Container,
    ContainerSpec,
    run_captured,
)

NL = chr(10)
ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = "turnstone.rollout"
# A module of the package that is listed nowhere: what one added later is.
LATER = PACKAGE + ".a_module_added_later"


@pytest.fixture(name="rollout_trips")
def _rollout_trips() -> list[str]:
    """What the guard on the rollout package noted in this test.  A test that
    made it refuse on purpose empties the list, or the guard fails the test."""
    trips: list[str] = container_module.subprocess._tripped
    return trips


class TestEveryStartOfThePackageIsJudged:
    """Whichever module of the rollout package asks for a process, and by
    whichever call, it gets only what the package may start."""

    @pytest.mark.parametrize("way", RETURNING)
    def test_a_module_listed_nowhere_cannot_start_another_program(
        self, tmp_path, rollout_trips, way
    ):
        later = probe_module(LATER)
        marker = tmp_path / "marker"
        with pytest.raises(DestructiveCommandError, match="not the one its commands go through"):
            getattr(later, way)([TOUCH, str(marker)])
        assert not marker.exists()
        # Noted, to fail the test at its end; this one is the test's own.
        assert len(rollout_trips) == 1
        rollout_trips.clear()

    @pytest.mark.parametrize("way", [way for way in RETURNING if way not in THROUGH_A_SHELL])
    def test_it_can_start_a_program_the_test_wrote(self, tmp_path, rollout_trips, way):
        later = probe_module(LATER)
        marker = tmp_path / "marker"
        program = own_program(tmp_path, f'touch "{marker}"')
        getattr(later, way)([program])
        assert marker.exists() and rollout_trips == []

    @pytest.mark.parametrize("way", THROUGH_A_SHELL)
    def test_but_not_through_a_shell(self, tmp_path, rollout_trips, way):
        """The program that would be started is the shell, which is not the test's."""
        later = probe_module(LATER)
        marker = tmp_path / "marker"
        program = own_program(tmp_path, f'touch "{marker}"')
        with pytest.raises(DestructiveCommandError, match="not the one its commands go through"):
            getattr(later, way)([program])
        assert not marker.exists() and len(rollout_trips) == 1
        rollout_trips.clear()

    @pytest.mark.parametrize("way", RETURNING)
    def test_the_same_calls_from_other_code_are_not_this_guards(self, rollout_trips, way):
        """The test's own code, and the framework's outside the package, start
        what they start: a runtime call of the test's, a harmless host command."""
        other = probe_module("tests.some_helper")
        assert getattr(other, way)([TRUE]) is not None
        assert rollout_trips == []

    def test_the_calls_that_replace_the_process_are_judged_too(self, tmp_path):
        """Tried in an interpreter of their own: one that was let through would
        replace it with ``true``, and its line below would be missing."""
        script = tmp_path / "replacing.py"
        lines = [
            "import types",
            "from _pytest.monkeypatch import MonkeyPatch",
            "from tests import _host_guard",
            f"probe = {PROBE!r}",
            f"later = types.ModuleType({LATER!r})",
            "exec(compile(probe, '<later>', 'exec'), later.__dict__)",
            "other = types.ModuleType('tests.some_helper')",
            "exec(compile(probe, '<other>', 'exec'), other.__dict__)",
            "tripped = []",
            "check = _host_guard.refuse_all_but_the_runtime(",
            f"    tripped, package={PACKAGE!r}, refusal=lambda start: 'this test says so'",
            ")",
            "_host_guard.judge_every_start(MonkeyPatch(), [check])",
            f"for way in {REPLACING!r}:",
            "    try:",
            f"        getattr(later, way)([{TRUE!r}])",
            "    except _host_guard.DestructiveCommandError as error:",
            "        print('refused', way, 'this test says so' in str(error), flush=True)",
            "print('trips', len(tripped), flush=True)",
            # Code that is not the package's is not this check's: the interpreter is
            # replaced by ``true``, and the line after it is never printed.
            f"other.execv([{TRUE!r}])",
            "print('still here')",
        ]
        script.write_text(NL.join(lines) + NL)
        done = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        assert done.stdout.splitlines() == [
            *(f"refused {way} True" for way in REPLACING),
            f"trips {len(REPLACING)}",
        ], done.stderr
        assert done.returncode == 0

    def test_the_modules_that_are_loaded_hold_the_check_in_place_of_subprocess(self, rollout_trips):
        loaded = [m for n, m in list(sys.modules.items()) if n.startswith(PACKAGE + ".")]
        assert container_module in loaded
        assert container_module.subprocess._tripped is rollout_trips
        assert container_module.subprocess.PIPE == subprocess.PIPE
        # No module of the package that is loaded holds the real one, under any name.
        holds_the_real_one = [
            m.__name__ for m in loaded if any(value is subprocess for value in vars(m).values())
        ]
        assert holds_the_real_one == []

    @pytest.mark.parametrize("call", ["Popen", "run", "call", "check_call", "check_output"])
    def test_every_call_of_that_stand_in_asks(self, tmp_path, rollout_trips, call):
        marker = tmp_path / "marker"
        with pytest.raises(DestructiveCommandError, match="not the one its commands go through"):
            getattr(container_module.subprocess, call)(["touch", str(marker)])
        with pytest.raises(DestructiveCommandError):
            container_module.subprocess.getoutput(f"touch {marker}")
        with pytest.raises(DestructiveCommandError):
            container_module.subprocess.getstatusoutput(f"touch {marker}")
        assert not marker.exists() and len(rollout_trips) == 3
        rollout_trips.clear()


def _imports(path: pathlib.Path) -> tuple[set[str], set[tuple[str, str]]]:
    """The modules a source file imports, and the names it imports from them."""
    modules: set[str] = set()
    names: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            names |= {(node.module, alias.name) for alias in node.names}
    return modules, names


def _uses_of(path: pathlib.Path, name: str) -> list[str]:
    """Every use of the name *name* in a source file, in the order they stand in
    it, each as the whole chain of attribute reads and calls it stands at the
    bottom of, written out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == name]
    found: list[str] = []
    for node in sorted(uses, key=lambda n: (n.lineno, n.col_offset)):
        top: ast.AST = node
        while True:
            parent = parents.get(top)
            if (isinstance(parent, ast.Attribute) and parent.value is top) or (
                isinstance(parent, ast.Call) and parent.func is top
            ):
                top = parent
            else:
                break
        found.append(ast.unparse(top))
    return found


# Names of ``os`` that start a process without a new program, past the guard.
_FORKS = frozenset({"fork", "forkpty"})
# What imports a module, or runs a pool of processes, by names the module list
# does not see.
_BY_A_NAME = frozenset({"ProcessPoolExecutor", "import_module", "__import__"})
# What may be imported of ``importlib``: it reads, and imports nothing by a name.
_IMPORTLIB_READS = frozenset({"importlib.metadata", "importlib.resources"})


def _ways_around(path: pathlib.Path) -> list[str]:
    """What in a source file starts a process past the guard without one of
    the modules the check lists, or imports a module by a name its source
    does not spell, each written out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    os_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "os"
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            forks = node.attr in _FORKS
            if (forks and isinstance(node.value, ast.Name) and node.value.id in os_names) or (
                node.attr in _BY_A_NAME
            ):
                found.append(ast.unparse(node))
        elif isinstance(node, ast.Name) and node.id in _BY_A_NAME:
            found.append(node.id)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.partition(".")[0]
            for alias in node.names:
                if alias.name in _BY_A_NAME or (node.module == "os" and alias.name in _FORKS):
                    found.append(f"from {node.module} import {alias.name}")
            if top == "importlib" and node.module not in _IMPORTLIB_READS:
                found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                if top == "importlib" and alias.name not in _IMPORTLIB_READS:
                    found.append(f"import {alias.name}")
    return found


class TestThePackageStartsProcessesOnlyWhereTheGuardStands:
    """The guard stands in ``subprocess.Popen`` and in the ``os`` module's
    functions.  A module that took one of ``os``'s functions by name, or
    started processes through something else, would go around it; every source
    file of the package is read here, so a module added later is read too."""

    FILES = sorted((ROOT / "turnstone" / "rollout").rglob("*.py"))
    # Other ways into a new process than the two the guard stands in, and
    # ``posix``, where the ``os`` module's functions are without the guards on
    # starts and signals.
    AROUND = {"_posixsubprocess", "multiprocessing", "pty", "ctypes", "cffi", "pexpect", "posix"}

    def test_the_package_has_source_files_to_read(self):
        assert self.FILES and all(path.is_file() for path in self.FILES)

    # The one use the package has of one of them.  ``container.py`` asks the kernel,
    # through the C library's ``prctl``, to end a child with the thread that started
    # it; that starts nothing.  The file may hold the module for that expression and
    # for nothing else: every use of the module's name in it must be this one, and
    # nothing may be imported from the module by name.
    KERNEL_REQUEST = {"container.py": ("ctypes", "ctypes.CDLL(None, use_errno=True).prctl")}

    @pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
    def test_no_module_goes_around_it(self, path):
        modules, names = _imports(path)
        around = {m for m in modules if m.split(".")[0] in self.AROUND}
        module, request = self.KERNEL_REQUEST.get(path.name, ("", ""))
        if module in around:
            assert not {name for imported, name in names if imported == module}
            assert _uses_of(path, module) == [request]
            around.discard(module)
        assert not around
        # Taken by name, one of ``os``'s functions would be the function itself,
        # from before a test put a guard in front of it: the signals too.
        starts = {"system", "popen", "fork", "forkpty", "*", *_host_guard._OS_STARTS}
        signals = {"kill", "killpg"}
        by_name = starts | signals
        assert not {name for imported, name in names if imported == "os" and name in by_name}
        # Nor ``os.fork`` and the like as attributes, a pool of processes, or an
        # import by a name the source does not spell.
        assert _ways_around(path) == []

    @pytest.mark.parametrize(
        "source",
        [
            "import os" + NL + "os.fork()",
            "import os" + NL + "pid, fd = os.forkpty()",
            "import os as system" + NL + "system.fork()",
            "from concurrent.futures import ProcessPoolExecutor",
            "import concurrent.futures" + NL + "concurrent.futures.ProcessPoolExecutor()",
            "from concurrent.futures.process import ProcessPoolExecutor as Pool",
            "pty = __import__('pty')",
            "import builtins" + NL + "pty = builtins.__import__('pty')",
            "import importlib" + NL + "importlib.import_module('multiprocessing')",
            "from importlib import import_module",
            "import importlib.util",
            "from importlib.util import spec_from_file_location",
            "import importlib.metadata" + NL + "importlib.import_module('pty')",
        ],
        ids=[
            "os.fork",
            "os.forkpty",
            "os-under-another-name",
            "pool-imported",
            "pool-as-attribute",
            "pool-from-its-module",
            "dunder-import",
            "dunder-import-of-builtins",
            "import_module",
            "import_module-by-name",
            "importlib.util",
            "from-importlib.util",
            "import_module-through-metadata",
        ],
    )
    def test_these_ways_around_it_are_seen(self, tmp_path, source):
        """Shown on files of the test's own."""
        path = tmp_path / "module.py"
        path.write_text(source + NL)
        assert _ways_around(path)

    def test_and_these_are_none(self, tmp_path):
        path = tmp_path / "module.py"
        lines = [
            "import os",
            "import importlib.metadata",
            "from importlib.resources import files",
            "pid = os.getpid()",
            "version = importlib.metadata.version('turnstone')",
            "class Tool:",
            "    def fork(self): ...",
            "Tool().fork()",
        ]
        path.write_text(NL.join(lines) + NL)
        assert _ways_around(path) == []

    def test_the_kernel_request_is_pinned_to_its_expression(self, tmp_path):
        """Shown on files of the test's own: the module's name used for anything
        else, or under another name, is not the request."""
        request = "ctypes.CDLL(None, use_errno=True).prctl"
        fine = tmp_path / "fine.py"
        fine.write_text("import ctypes" + NL + "ask = " + request + NL)
        assert _uses_of(fine, "ctypes") == [request]
        other = tmp_path / "other.py"
        other.write_text(
            "import ctypes" + NL + "ask = " + request + NL + "libc = ctypes.CDLL(None)" + NL
        )
        assert _uses_of(other, "ctypes") == [request, "ctypes.CDLL(None)"]
        renamed = tmp_path / "renamed.py"
        renamed.write_text("import ctypes as c" + NL + "ask = c.CDLL(None).fork" + NL)
        assert _uses_of(renamed, "ctypes") == []


def _start(*argv: str, program: str | None = None, caller: str = PACKAGE + ".container") -> Start:
    found = started_program(argv[0]) if program is None else program
    return Start("subprocess.Popen", tuple(argv), found, caller)


class TestWhatTheRolloutPackageMayStart:
    """The judgement itself, on command lines as data: nothing is started."""

    def test_the_guard_does_not_take_the_packages_word_for_its_containers_names(self):
        """The prefix is written out in the guard; the package's must be the same,
        or the guard would refuse the package's own containers."""
        from tests.conftest import ROLLOUT_CONTAINER_PREFIX

        assert NAME_PREFIX == ROLLOUT_CONTAINER_PREFIX == "tsgym-"

    def test_nor_for_its_keepers_script(self):
        """The keeper's script is written out in the guard as well: a script the
        package changed fails here, instead of being let through as the keeper."""
        assert container_module._LIFETIME_SCRIPT == ROLLOUT_KEEPER_SCRIPT

    # The keeper as the package starts it, and as the guard knows it.
    KEEPER = container_module._LIFETIME_SCRIPT
    A = NAME_PREFIX + "a"

    def _refusal(self, start: Start, tmp_path: Any) -> str | None:
        scratch = os.path.realpath(tmp_path) + os.sep
        return rollout_refusal(
            start, scratch=scratch, prefix=NAME_PREFIX, keeper=ROLLOUT_KEEPER_SCRIPT
        )

    def test_a_program_the_test_wrote(self, tmp_path):
        program = own_program(tmp_path, "true")
        assert self._refusal(_start(program, "exec", "anything", "at", "all"), tmp_path) is None
        # One that is not there yet starts nothing, and is the test's as well.
        assert self._refusal(_start(str(tmp_path / "missing")), tmp_path) is None

    def test_a_bare_name_is_not_a_file_of_that_name_in_the_tests_directory(
        self, tmp_path, monkeypatch
    ):
        """It used to be read against the working directory: with that under the
        temporary directory, any bare name passed as the test's own program."""
        (tmp_path / "true").write_text("")
        monkeypatch.chdir(tmp_path)
        assert self._refusal(_start("true"), tmp_path) is not None
        assert self._refusal(_start("sh", "-c", "true"), tmp_path) is not None

    def test_a_link_to_a_program_elsewhere_is_that_program(self, tmp_path):
        link = tmp_path / "mine"
        link.symlink_to(TRUE)
        assert self._refusal(_start(str(link)), tmp_path) is not None

    def test_the_runtime_is_the_program_the_system_finds_under_that_name(self, tmp_path):
        found = started_program("docker")
        assert self._refusal(_start("docker", "ps", program=found), tmp_path) is None
        if found:
            assert self._refusal(_start(found, "ps", program=found), tmp_path) is None
        # A program of that name somewhere else is not it.
        elsewhere = _start("/opt/elsewhere/docker", "ps", program="/opt/elsewhere/docker")
        assert self._refusal(elsewhere, tmp_path) is not None
        # Nor is another program that is given the runtime's arguments.
        assert self._refusal(_start("true", "exec", self.A, "true"), tmp_path) is not None

    def test_the_keeper_is_let_through_for_a_container_of_the_harness(self, tmp_path):
        keeper = ["sh", "-c", self.KEEPER, "sh", "5", "docker"]
        assert self._refusal(_start(*keeper, self.A), tmp_path) is None
        # With a stand-in for the runtime that the test wrote.
        stand_in = own_program(tmp_path, "true", name="runtime")
        assert self._refusal(_start(*keeper[:5], stand_in, self.A), tmp_path) is None
        assert "no container of the harness" in str(
            self._refusal(_start(*keeper, "someone-elses"), tmp_path)
        )
        assert self._refusal(_start("sh", "-c", "true"), tmp_path) is not None
        assert self._refusal(_start("sh", "-c", self.KEEPER), tmp_path) is not None

    def test_the_keeper_is_judged_by_the_word_its_script_removes(self, tmp_path):
        """The script removes its third argument, the seventh word: one word more
        and it would remove another container than the last word names."""
        keeper = ["sh", "-c", self.KEEPER, "sh", "5", "docker"]
        why = self._refusal(_start(*keeper, "someone-elses", self.A), tmp_path)
        assert "not its 3" in str(why)
        assert "not its 3" in str(self._refusal(_start(*keeper), tmp_path))
        assert "not its 3" in str(self._refusal(_start(*keeper[:5], self.A), tmp_path))

    @pytest.mark.parametrize(
        "program", ["/usr/bin/xargs", "xargs", "true", "/opt/elsewhere/docker", ""]
    )
    def test_the_keeper_runs_only_the_runtime(self, tmp_path, program):
        """The program the script runs on the name, its second argument."""
        keeper = ("sh", "-c", self.KEEPER, "sh", "5", program, self.A)
        assert "neither the container runtime" in str(self._refusal(_start(*keeper), tmp_path))

    def test_a_keeper_with_another_script_is_no_keeper(self, tmp_path):
        script = self.KEEPER.replace('"$3"', "$(docker ps --all --quiet)")
        keeper = ("sh", "-c", script, "sh", "5", "docker", self.A)
        assert "neither the container runtime" in str(self._refusal(_start(*keeper), tmp_path))

    def test_the_keeper_is_no_command_of_the_destructive_list(self):
        keeper = ("sh", "-c", self.KEEPER, "sh", "5", "docker", self.A)
        assert start_shape(Start("subprocess.Popen", keeper, "/bin/sh", PACKAGE)) is None

    def test_a_start_that_could_not_be_read_is_refused(self, tmp_path):
        unread = Start("os.execv", (), "", PACKAGE + ".container")
        assert self._refusal(unread, tmp_path) == "what it would start could not be read"

    LET_THROUGH = [
        ["exec", A, "true"],
        ["exec", "--interactive", "--user", "0:0", "--workdir", "/w", A, "sh", "-c", "echo hi"],
        ["exec", "-i", "-u", "0:0", A, "true"],
        ["exec", "-it", A, "true"],
        ["exec", "--user=0:0", A, "true"],
        # What follows the container is its command, whatever it looks like.
        ["exec", A, "docker", "rm", "--force", "someone-elses"],
        ["rm", "--force", "--volumes", A],
        ["rm", "-fv", A, NAME_PREFIX + "b"],
        ["kill", A],
        ["kill", "--signal", "KILL", A],
        ["stop", "--time", "5", A],
        ["pause", A],
        ["unpause", A],
        ["top", A, "-eo", "pid,uid"],
        ["inspect", "--format", "{{.Id}}", A],
        ["inspect", "--size", "--format", "{{.SizeRw}}", A],
        ["container", "inspect", "--format", "{{.Id}}", A],
        ["cp", A + ":/work/out.txt", "-"],
        ["cp", "-", A + ":/work"],
        ["image", "inspect", "--format", "{{json .Config.Volumes}}", "python:3.14-slim"],
        ["events", "--filter", "container=" + A, "--format", "{{.Action}}"],
        ["ps", "--all"],
        ["stats", "--no-stream"],
        [
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            A,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",
            "--entrypoint",
            "sh",
            "python:3.14-slim",
            "-c",
            "true",
        ],
        ["run", "--rm", "--name", A + "-main", "--pid", "container:" + A, "image", "true"],
        ["run", "--name=" + A, "image"],
        # The image's command may hold what looks like an option of the runtime.
        ["run", "--name", A, "image", "--privileged", "--volume", "/:/host"],
        # The helper that ends what is left in a container, as the package starts it.
        [
            "run",
            "--rm",
            "--name",
            NAME_PREFIX + "end-a",
            "--pid",
            "container:" + A,
            "--pull",
            "never",
            "--label",
            "turnstone.rollout=1",
            "--network",
            "none",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--memory",
            "64m",
            "--pids-limit",
            "16",
            "--tmpfs",
            "/data:size=1m",
            "--entrypoint",
            "sh",
            "python:3.14-slim",
            "-c",
            "true",
        ],
        ["run", "--name", A, "--storage-opt", "size=8g", "image"],
    ]
    REFUSED = [
        [],
        ["--host", "tcp://another-machine:2375", "ps"],
        ["exec", "someone-elses", "true"],
        ["exec"],
        # A value of an option is not the container.
        ["exec", "--user", A, "someone-elses", "true"],
        ["exec", "--privileged", A, "true"],
        ["rm", "--force", "someone-elses"],
        ["rm", "--force", A, "someone-elses"],
        ["rm", "--force"],
        ["kill", "someone-elses"],
        ["kill", "--signal", A, "someone-elses"],
        ["stop", "someone-elses"],
        ["restart", "someone-elses"],
        ["pause", "someone-elses"],
        ["top", "someone-elses"],
        ["inspect", "someone-elses"],
        ["update", "--memory", "1g", "someone-elses"],
        ["rename", A, "someone-elses"],
        ["container", "rm", "--force", "someone-elses"],
        ["container", "prune", "--force"],
        ["container"],
        ["cp", "someone-elses:/etc/hostname", "-"],
        ["cp", A + ":/work/out.txt", "/somewhere/on/this/machine"],
        ["cp", "/etc/hostname", A + ":/work"],
        ["cp", A + ":/a", NAME_PREFIX + "b:/b"],
        ["cp", A + ":/work/out.txt"],
        ["run", "image", "true"],
        ["run", "--name", "someone-elses", "image"],
        ["run", "--name", A],
        ["run", "--name", A, "--privileged", "image"],
        ["run", "--name", A, "--pid", "host", "image"],
        ["run", "--name", A, "--network=host", "image"],
        ["run", "--name", A, "--pid", "container:someone-elses", "image"],
        ["run", "--name", A, "--volumes-from", "someone-elses", "image"],
        ["run", "--name", A, "--volume", "/:/host", "image"],
        ["run", "--name", A, "-v", "/home:/h:ro", "image"],
        ["run", "--name", A, "-v/home:/h", "image"],
        ["run", "--name", A, "--mount", "type=bind,source=/,target=/host", "image"],
        ["run", "--name", A, "--device", "/dev/null", "image"],
        # Options the package never gives run, or values it never gives them.
        ["run", "--name", A, "--cap-add", "ALL", "image"],
        ["run", "--name", A, "--cap-add", "SYS_ADMIN", "image"],
        ["run", "--name", A, "--cap-add", "SYS_RESOURCE", "image"],
        ["run", "--name", A, "--security-opt", "seccomp=unconfined", "image"],
        ["run", "--name", A, "--security-opt", "apparmor=unconfined", "image"],
        ["run", "--name", A, "--device-cgroup-rule", "b *:* rwm", "image"],
        [
            "run",
            "--name",
            A,
            "--mount",
            "type=volume,dst=/h,volume-opt=type=none,volume-opt=device=/home,volume-opt=o=bind",
            "image",
        ],
        ["run", "--name", A, "-v", "a-volume-of-someone-else:/data", "image"],
        ["run", "--name", A, "--volume", "data:/data", "image"],
        ["run", "--name", A, "--mount", "type=tmpfs,dst=/t", "image"],
        ["run", "--name", A, "--network", "bridge", "image"],
        ["run", "--name", A, "--network", "container:" + A, "image"],
        ["run", "--name", A, "--ipc", "container:" + A, "image"],
        ["run", "--name", A, "--ulimit", "memlock=-1:-1", "image"],
        ["run", "--name", A, "--oom-score-adj", "-1000", "image"],
        ["run", "--name", A, "--pull", "always", "image"],
        ["run", "--name", A, "--storage-opt", "dm.basesize=1t", "image"],
        ["run", "--name", A, "--gpus", "all", "image"],
        ["run", "--name", A, "--publish", "80:80", "image"],
        ["run", "--name", A, "--init", "image"],
        ["run", "--name", A, "--rm=false", "image"],
        ["run", "--name", A, "-e", "A=1", "image"],
        ["run", "--name", A, "-it", "image"],
        ["create", "--name", "someone-elses", "image"],
        ["image", "rm", "python:3.14-slim"],
        ["image", "prune"],
        ["rmi", "python:3.14-slim"],
        ["pull", "python:3.14-slim"],
        ["build", "."],
        ["commit", A],
        ["export", A],
        ["system", "prune", "--force"],
        ["volume", "rm", "data"],
        ["network", "rm", "bridge"],
        ["compose", "down"],
        ["no-such-subcommand", A],
    ]

    def _own(self, tmp_path: Any) -> Any:
        scratch = os.path.realpath(tmp_path) + os.sep
        return lambda path: os.path.realpath(path).startswith(scratch)

    @pytest.mark.parametrize("words", LET_THROUGH, ids=" ".join)
    def test_runtime_commands_on_the_harness_own_containers(self, tmp_path, words):
        assert runtime_refusal(words, prefix=NAME_PREFIX, own=self._own(tmp_path)) is None

    @pytest.mark.parametrize("words", REFUSED, ids=lambda w: " ".join(w) or "nothing")
    def test_runtime_commands_on_anything_else(self, tmp_path, words):
        why = runtime_refusal(words, prefix=NAME_PREFIX, own=self._own(tmp_path))
        assert isinstance(why, str) and why

    def test_a_copy_may_use_the_tests_own_directory_and_a_mount_nothing(self, tmp_path):
        """The package mounts nothing into a container: no directory, the
        test's own included, and no volume."""
        own = self._own(tmp_path)
        there = str(tmp_path / "out")
        copy = ["cp", self.A + ":/work", there]
        assert runtime_refusal(copy, prefix=NAME_PREFIX, own=own) is None
        mount = ["run", "--name", self.A, "--volume", f"{tmp_path / 'data'}:/data:ro", "image"]
        assert "no option the harness gives run" in str(
            runtime_refusal(mount, prefix=NAME_PREFIX, own=own)
        )

    @staticmethod
    def _packages_runs() -> list[list[str]]:
        """The package's own ``run`` command lines, as it builds them; nothing
        is started."""
        default = Container(ContainerSpec())
        default.volumes = ["/data"]
        writable = Container(
            ContainerSpec(
                user="0:0",
                read_only_root=False,
                root_size="8g",
                capabilities=tuple(sorted(ALLOWED_CAPABILITIES)),
                environment={"A": "1"},
                writable={"/work": "32m"},
            )
        )
        return [default.run_argv(), default.protect_argv(), writable.run_argv()]

    def test_the_packages_own_runs_are_let_through(self, tmp_path):
        runs = self._packages_runs()
        assert "--storage-opt" in runs[2] and "--cap-add" in runs[2]
        for argv in runs:
            assert argv[:2] == ["docker", "run"]
            assert runtime_refusal(argv[1:], prefix=NAME_PREFIX, own=self._own(tmp_path)) is None

    def test_nor_for_the_capabilities_a_container_may_take(self):
        """Written out in the guard; the package's must be the same, or a
        capability the package came to allow would be let through unread."""
        written = _host_guard._RUN_VALUES["--cap-add"] - _host_guard._HELPER_CAPABILITIES
        assert written == ALLOWED_CAPABILITIES


class TestThePackageThroughItsOwnStart:
    """The same judgement where the package's own process start meets it."""

    # Names that nothing on any machine has.  If the guard did not refuse, the
    # runtime would be asked about a container and an image it does not know,
    # and would fetch nothing.
    NOBODYS = "no-such-container-of-anyone"

    def test_a_bare_name_with_the_working_directory_under_the_tests_own(
        self, tmp_path, monkeypatch, rollout_trips
    ):
        (tmp_path / "true").write_text("")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(DestructiveCommandError, match="not the one its commands go through"):
            run_captured(["true"], timeout=5, limit=10)
        assert len(rollout_trips) == 1
        rollout_trips.clear()

    @pytest.mark.parametrize(
        "words",
        [
            ["rm", "--force", NOBODYS],
            ["kill", NOBODYS],
            ["stop", NOBODYS],
            ["cp", NOBODYS + ":/etc/hostname", "-"],
            ["exec", NOBODYS, "true"],
            ["exec", "--privileged", NAME_PREFIX + "no-such-container", "true"],
            ["run", "--pull", "never", "--rm", "no-such-image-of-anyone:none", "true"],
        ],
        ids=" ".join,
    )
    def test_the_runtime_is_not_handed_another_container(self, rollout_trips, words):
        """Refused before the runtime is started: nothing is asked of it."""
        with pytest.raises(DestructiveCommandError, match="not the one its commands go through"):
            run_captured(["docker", *words], timeout=5, limit=10)
        assert len(rollout_trips) == 1
        rollout_trips.clear()

    def test_a_stand_in_for_the_runtime_is_started(self, tmp_path, rollout_trips):
        marker = tmp_path / "asked"
        stand_in = own_program(tmp_path, f'echo "$*" > "{marker}"', name="runtime")
        done = run_captured([stand_in, "rm", "--force", self.NOBODYS], timeout=5, limit=10)
        assert done.returncode == 0 and marker.read_text() == f"rm --force {self.NOBODYS}" + NL
        assert rollout_trips == []
