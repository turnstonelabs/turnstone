"""cli.py — ``turnstone-rollout``: run one rollout and print what was recorded.

Usage:
    turnstone-rollout --base-url http://127.0.0.1:8000/v1 --model NAME
        --prompt "Create hello.txt containing hi, then list the directory."
        --top-k 20 --top-p 0.95
        [--file notes.txt=some text] [--image IMAGE]
        [--extra-body '{"chat_template_kwargs": {...}}'] [--out episode.json]

    turnstone-rollout --base-url ... --model NAME --task task.json
        [--verifier-command 'grade TASKS {key} {container}'] --out episode.json

The task is given by flags (``--prompt`` and the container flags) or as a
whole by a task file (``--task``, :mod:`turnstone.rollout.taskfile`), never by
both: a flag that says something a task file holds is refused beside one.

The reply limit, the context window, the tool time limit, the sub-agent turn
limit, the temperature and the reasoning effort are the framework's for the
model (its definition in ``config.toml``, the configured setting, the default)
unless a flag overrides them.

Exit status, for a caller that starts this command as a process:

- 0: a trainable rollout, however the model ended it;
- 1: the server, the harness or the runtime ended it, or the record could not
  be written;
- 2: it ran to its own end but cannot be trained on (its token data, a
  changed server version, a failed check);
- 3: nothing was run: the command line, the task file or the configuration
  was refused, and standard error says why.

The status is read off the record.  What is printed is a summary for a reader:
text the output cannot encode is escaped, a closed output is left alone, and
neither changes the status.  Nor does text that anyone else wrote to standard
output or standard error (the framework's logger, a warning) and that the
stream did not take.

With ``--out`` the record is written for 0, 1 and 2, also for a run that did
not start, and never for 3.  It is written under another name and renamed, so
the file is there whole or not at all.  Where it points is looked at before
anything runs: a folder that is not there or takes no file is refused, so is a
place that holds a file the command itself reads or anything but a file, and a
file already there is removed, so that afterwards the file is this run's
record or is not there.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import math
import os
import shlex
import shutil
import stat
import sys
from typing import Any, NoReturn

from turnstone.core.config import config_file_path, set_config_path
from turnstone.rollout.container import ContainerSpec
from turnstone.rollout.episode import (
    DEFAULT_SYSTEM_PROMPT,
    RolloutConfig,
    Task,
    _logprob_verdict,
    run_episode,
)
from turnstone.rollout.probe import SETTINGS as PROBED_SETTINGS
from turnstone.rollout.rules import RuleSet
from turnstone.rollout.settings import (
    SetupError,
    read_configuration,
    resolve_sampling,
    strict_json,
)
from turnstone.rollout.taskfile import check_task, load_task
from turnstone.rollout.tokens import generated_positions
from turnstone.rollout.verification import check_verifier_command

_NL = chr(10)

# The exit status of a command that ran nothing: its command line, its task
# file or its configuration was refused.
REFUSED = 3

# The flags that say what the task is.  A task file says all of it, so beside
# ``--task`` each of them is refused: two sources for one value is how a stale
# flag wins over a file without a word.
_TASK_FLAGS = {
    "prompt": "--prompt",
    "key": "--key",
    "system_prompt": "--system-prompt",
    "file": "--file",
    "image": "--image",
    "workdir": "--workdir",
    "user": "--user",
    "writable_root": "--writable-root",
    "cap": "--cap",
    "env": "--env",
    "memory": "--memory",
    "cpus": "--cpus",
    "max_turns": "--max-turns",
    "timeout": "--timeout",
    "max_generated_tokens": "--max-generated-tokens",
    "max_command_seconds": "--max-command-seconds",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # Not the usual status of a usage error: 2 is a rollout that ran.
        self.print_usage(sys.stderr)
        self.exit(REFUSED, f"{self.prog}: error: {message}{_NL}")


def _shown(text: object) -> str:
    """*text* for the terminal: what the model, a tool or the server wrote may
    hold control sequences, which are shown, not sent."""
    return "".join(
        ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in str(text)
    )


def _number(text: str) -> float:
    """A flag's number: any that is finite.  ``nan`` and ``inf`` are numbers
    to the interpreter and are no limit, no budget and no sampler setting."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is no number") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text!r} is no finite number")
    return value


def _put(stream: Any, text: str) -> bool:
    """Write *text* to *stream*; ``False`` when the stream did not take it.
    Never raises.

    How a rollout went is said by the record and the exit status, never by
    whether something could be printed.  So what the stream's encoding cannot
    write is escaped (the text is the model's, a tool's or the server's, and
    an encoding chosen by the environment must not fail the command over it),
    and a stream that takes nothing any more, such as a pipe whose reader has
    gone, is left alone.
    """
    try:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        data = text.encode(encoding, errors="backslashreplace")
        binary = getattr(stream, "buffer", None)
        if binary is not None:
            # After what was written as text before, and out at once.
            stream.flush()
            binary.write(data)
            binary.flush()
        else:
            stream.write(data.decode(encoding))
            stream.flush()
        return True
    except OSError:
        _silence(stream)
        return False
    except Exception:
        return False


def _silence(stream: Any) -> None:
    """Point a stream that takes nothing any more at nothing, so that the
    interpreter's own flush at exit cannot fail there and change the exit
    status.  Never raises; a stream without a descriptor is left as it is."""
    with contextlib.suppress(Exception):
        descriptor = stream.fileno()
        nothing = os.open(os.devnull, os.O_WRONLY)
        if nothing == descriptor:
            # The descriptor had been closed under the stream, so the system
            # gave out its number again: the stream is open on nothing now.
            return
        try:
            os.dup2(nothing, descriptor)
        finally:
            os.close(nothing)


def _refuses_writes(stream: Any) -> bool:
    """Whether the descriptor under *stream* is known to take no write: one
    that is closed or was not opened for writing, a pipe or a socket whose
    reader has gone.  ``False`` where that cannot be known without writing (a
    full disk), and for a stream without a descriptor.  Never raises."""
    try:
        descriptor = stream.fileno()
    except Exception:
        return False
    try:
        import fcntl
        import select

        if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            return True
        watch = select.poll()
        watch.register(descriptor, select.POLLOUT)
        return any(said & (select.POLLERR | select.POLLHUP) for _, said in watch.poll(0))
    except OSError as error:
        return error.errno == errno.EBADF
    except Exception:
        return False


def _settle(stream: Any) -> None:
    """Leave *stream* so that the interpreter's flush at exit cannot fail on
    it, whoever wrote to it.  Never raises.

    The command is not the only writer: the framework's logger writes to
    standard error, and so does a warning.  Text such a writer handed to a
    stream that did not take it stays in the stream, the interpreter tries
    once more when the process ends, and where that fails the process ends
    with a status of the interpreter's own in place of the record's.  So what
    the stream still holds is written now, and a stream that fails at that,
    or whose descriptor is known to take no write, is pointed at nothing.
    """
    if _put(stream, "") and _refuses_writes(stream):
        _silence(stream)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="turnstone-rollout", description=__doc__.split(_NL)[0])
    parser.add_argument("--base-url", required=True, help="chat-completions API root, ending /v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", default=None, help="what the user asks (or --task)")
    parser.add_argument(
        "--task",
        default=None,
        metavar="FILE",
        help="a JSON file that says the whole task: prompt, files, limits, container, "
        "before-run and after-run commands, sidecar; no other task flag goes with it",
    )
    parser.add_argument("--key", default="cli", help="the task's key in the record")
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument(
        "--file", action="append", default=[], metavar="PATH=CONTENT", help="setup file, repeatable"
    )
    parser.add_argument("--image", default=ContainerSpec.image)
    parser.add_argument("--workdir", default=ContainerSpec.workdir)
    parser.add_argument(
        "--user", default=ContainerSpec.user, help="the user commands run as inside (0:0 is root)"
    )
    parser.add_argument(
        "--writable-root",
        action="store_true",
        help="make the image's whole file system writable (a root size, which bounds what is "
        "written there, is named in a task file)",
    )
    parser.add_argument(
        "--cap", action="append", default=[], metavar="NAME", help="a capability, repeatable"
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="NAME=VALUE", help="repeatable"
    )
    parser.add_argument("--memory", default=ContainerSpec.memory)
    parser.add_argument("--cpus", type=_number, default=ContainerSpec.cpus)
    parser.add_argument("--max-turns", type=int, default=Task.max_turns)
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="time limit for the session's run, seconds (default: 300; with a token budget "
        "it is a backstop for the machinery and, left out, worked out from the budgets)",
    )
    parser.add_argument(
        "--max-generated-tokens",
        type=int,
        default=None,
        help="the model's budget of generated tokens, its sub-agents' included",
    )
    parser.add_argument(
        "--max-command-seconds",
        type=_number,
        default=None,
        help="the model's budget of time its commands may run",
    )
    parser.add_argument(
        "--min-tokens-per-second",
        type=_number,
        default=None,
        help="tokens a second the server must at least generate (needed with a token budget)",
    )
    parser.add_argument(
        "--min-prompt-tokens-per-second",
        type=_number,
        default=None,
        help="tokens a second the server must at least process of a prompt",
    )
    parser.add_argument(
        "--tool-timeout",
        type=int,
        default=None,
        help="seconds one tool call may run (default: the framework's tools.timeout)",
    )
    parser.add_argument(
        "--temperature",
        type=_number,
        default=None,
        help="sampling temperature (default: the framework's for this model, else 1)",
    )
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--top-p", type=_number, default=None)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="reply limit per model call (default: the framework's for this model)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=None,
        help="the model's context window (default: its definition, else what its endpoint reports)",
    )
    parser.add_argument(
        "--subagent-max-turns",
        type=int,
        default=None,
        help="turns one sub-agent may take (default: the framework's tools.agent_max_turns)",
    )
    parser.add_argument("--max-subagents", type=int, default=RolloutConfig.max_subagents)
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="path to config.toml; a file that is not there or cannot be read or parsed is "
        "refused, never taken as an empty configuration",
    )
    parser.add_argument(
        "--rules",
        default="shipped",
        metavar="PATH",
        help="the pattern rules enforced on tool calls: a rule file, 'shipped' (the default) "
        "or 'none'",
    )
    parser.add_argument(
        "--effort",
        default=None,
        help="reasoning effort (default: the framework's for this model, if it names one)",
    )
    parser.add_argument(
        "--extra-body",
        default=None,
        metavar="JSON",
        help="JSON object merged into every request's body (sampler and template arguments)",
    )
    parser.add_argument(
        "--keep-text", action="store_true", help="keep each call's generated text in the record"
    )
    parser.add_argument(
        "--sampler-logprobs",
        default="probe",
        metavar="VERDICT",
        help="whether the server reports log-probabilities of the sampling distribution: "
        "'probe' (the default) asks it before the run, 'yes' or 'no' says so, and a JSON "
        "object (or @FILE holding one) passes on what --probe-only printed",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="run no rollout: probe which log-probabilities the server reports, print the "
        "verdict as one JSON object and exit (0 when they are the sampling distribution's)",
    )
    parser.add_argument(
        "--server-version-url",
        default=None,
        metavar="URL",
        help="an address that answers with the server's version, asked before the first model "
        "call and after the last; a rollout that saw two versions is not trainable",
    )
    parser.add_argument(
        "--server-version-field",
        default=None,
        metavar="NAME",
        help="the field that holds the version in the JSON object --server-version-url "
        "answers with (default: the whole answer is the version)",
    )
    parser.add_argument(
        "--verifier-command",
        default=None,
        metavar="COMMAND",
        help="a program of yours that checks the rollout, started on this machine without a "
        "shell once the episode's container is stopped; split into arguments by shell "
        "quoting rules, with {container}, {sidecar} and {key} in the arguments replaced by "
        "the stopped container's name, the sidecar's and the task's key (one argument "
        "whatever it holds; put -- before it). It must print one JSON object with a verdict",
    )
    parser.add_argument(
        "--verifier-timeout",
        type=_number,
        default=None,
        metavar="SECONDS",
        help="how long the verifier command may take (default: 3600)",
    )
    parser.add_argument(
        "--max-output-chars",
        type=int,
        default=None,
        help="characters of a command's output the model gets (default: the framework's "
        "tools.truncation)",
    )
    parser.add_argument(
        "--max-read-bytes",
        type=int,
        default=RolloutConfig.max_read_bytes,
        help="largest file a file tool reads whole",
    )
    parser.add_argument(
        "--server-stall-seconds",
        type=_number,
        default=RolloutConfig.server_stall_seconds,
        help="a run that reaches its time limit while the server has given nothing for this "
        "long ended on the server",
    )
    parser.add_argument(
        "--api-key-env",
        default=None,
        metavar="NAME",
        help="the environment variable that holds the server's API key (default: no key)",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        help="write the full episode record here as JSON: under another name first, then "
        "renamed, so the file is there whole or not at all. Looked at before anything runs: "
        "a folder that is not there or takes no file is refused, so is a place that holds "
        "a file the command reads or anything but a file, and a file already there is "
        "removed",
    )
    return parser


def _given(argv: list[str] | None) -> set[str]:
    """The flags the command line really holds, by the names they are parsed to."""
    probe = _parser()
    # A second parse in which a flag that is not given leaves nothing behind.
    for action in probe._actions:
        action.default = argparse.SUPPRESS
        action.required = False
    return set(vars(probe.parse_args(argv)))


def _write_whole(path: str, text: str) -> None:
    """Write *text* to *path* so that the file is there whole or not at all:
    under a name of this process's own beside it, then renamed."""
    temporary = f"{path}.{os.getpid()}.part"
    try:
        # ASCII output: text the model produced may hold what no encoding can write.
        with open(temporary, "w", encoding="ascii") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_by(args: argparse.Namespace) -> list[tuple[str, str]]:
    """The files this command itself reads or starts, each with how it comes
    to: the flag that names it, the place the configuration is read from where
    no flag names one, the verifier command."""
    read = [("--task reads", args.task)]
    if args.config is not None:
        read.append(("--config reads", args.config))
    else:
        # The one ``TURNSTONE_CONFIG`` names, else the default place: the
        # framework's own word for the file this process reads.
        read.append(("the configuration is read from", str(config_file_path())))
    if args.rules not in ("shipped", "none"):
        read.append(("--rules reads", args.rules))
    for flag, said in (("--sampler-logprobs", args.sampler_logprobs),):
        if said is not None and said.startswith("@"):
            read.append((f"{flag} reads", said[1:]))
    if args.verifier_command:
        try:
            program, *arguments = shlex.split(args.verifier_command)
        except ValueError:
            # No program, or no command at all: refused later, in its own words.
            program, arguments = "", []
        # The program as the system would find it, and what it is given that
        # is a file: a grader's script, its list of tasks.
        started: str | None = program
        if program and os.sep not in program:
            started = shutil.which(program)
        read.append(("--verifier-command starts", started or ""))
        read += [("--verifier-command names", said) for said in arguments if os.path.isfile(said)]
    return [(how, path) for how, path in read if path]


def _kind(mode: int) -> str:
    """What stands at a place, in words, from the mode the system gives it."""
    if stat.S_ISDIR(mode):
        return "a folder"
    if stat.S_ISFIFO(mode):
        return "a named pipe"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device"
    return "no regular file"


def _claim(path: str, read: list[tuple[str, str]], *, remove: bool = True) -> None:
    """Look at where ``--out`` points before anything runs: make sure a file
    can be written there, and remove one that is there already.

    Without *remove* the file that is there stays for now: the caller puts
    its own in the place in one step, or removes what is there when it has
    none to put (a probe's verdict, which the rollouts of a batch read).

    *read* are the files the command itself reads or starts.  A place that is
    one of them is refused: removing what is there would take the task file,
    the verdict or the verifier's program from under the command.

    Only a file is removed, and a symbolic link, which is a name: what it
    points to stays (a link to one of the files above is refused with them).
    Anything else that stands there is refused and left as it is: a folder, a
    named pipe, a socket, a device.  A command with root's rights that was
    pointed at the null device would otherwise remove the node.

    A rollout whose record cannot be written is lost, and it is found out only
    when the run is over.  And a file an earlier run left at the same place
    would, after a run that died, look like this run's record.  So afterwards,
    whatever the command did, the file at *path* is this run's or is not there.

    Raises :class:`SetupError` where the folder is not there or takes no file,
    and where what is at *path* is no file or cannot be removed.
    """
    if not path:
        raise SetupError("--out names no file")
    for how, other in read:
        if os.path.realpath(path) == os.path.realpath(other):
            raise SetupError(
                f"--out names the file that {how} ({other}): the record would take its place"
            )
    try:
        # The place itself, not what a link there points to.
        there: os.stat_result | None = os.lstat(path)
    except OSError:
        # Nothing there, or a folder on the way that is none: the checks below.
        there = None
    if there is not None and not (stat.S_ISREG(there.st_mode) or stat.S_ISLNK(there.st_mode)):
        raise SetupError(
            f"--out: what is at {path} is no file that can be replaced "
            f"({_kind(there.st_mode)}); it is left as it is"
        )
    folder = os.path.dirname(path) or "."
    if not os.path.isdir(folder):
        raise SetupError(
            f"--out: the folder {folder} is not there, so the record could not be written "
            "when the run is over"
        )
    # The very name the record is written under before it is renamed.
    temporary = f"{path}.{os.getpid()}.part"
    try:
        with open(temporary, "w", encoding="ascii"):
            pass
    except OSError as error:
        raise SetupError(
            f"--out: no file can be written in the folder {folder} "
            f"({type(error).__name__}: {error})"
        ) from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
    if not remove:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SetupError(
            f"--out: what is at {path} is no file that can be replaced "
            f"({type(error).__name__}: {error})"
        ) from None


def _gone(path: str) -> None:
    """Remove the file at *path* if one is there.  Never raises."""
    with contextlib.suppress(OSError):
        os.unlink(path)


def _verdict_given(said: str) -> bool | dict[str, bool | None] | None:
    """What ``--sampler-logprobs`` says: ``None`` to probe, a yes or no for
    every sampler setting, or one per setting as a probe reported them."""
    if said in ("probe", "yes", "no"):
        return {"probe": None, "yes": True, "no": False}[said]
    text = said
    if said.startswith("@"):
        try:
            with open(said[1:], encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            raise SetupError(f"--sampler-logprobs: {said[1:]} cannot be read ({error})") from None
    try:
        verdict = strict_json(text)
    except (ValueError, RecursionError):
        verdict = None
    reflects = verdict.get("reflects", verdict) if isinstance(verdict, dict) else None
    if (
        not isinstance(reflects, dict)
        or set(reflects) != set(PROBED_SETTINGS)
        or any(v is not None and not isinstance(v, bool) for v in reflects.values())
    ):
        raise SetupError(
            "--sampler-logprobs takes probe, yes, no, or what --probe-only printed: a JSON "
            f"object saying true, false or null for each of {', '.join(PROBED_SETTINGS)}"
        )
    return {name: reflects[name] for name in PROBED_SETTINGS}


def _task(args: argparse.Namespace, given: set[str]) -> Task:
    """The task the command line names: a task file, or the task flags."""
    if args.task is not None:
        beside = sorted(flag for name, flag in _TASK_FLAGS.items() if name in given)
        if beside:
            raise SetupError(
                f"{', '.join(beside)} cannot be given with --task: the task file says the "
                "whole task, and two sources for one value would leave open which one counts"
            )
        return load_task(args.task)
    if args.prompt is None:
        raise SetupError("give the task: --prompt, or --task with a task file")
    files = {}
    for item in args.file:
        path, given, content = item.partition("=")
        if not given:
            raise SetupError(f"--file takes PATH=CONTENT, and {item!r} has no =")
        files[path] = content
    for item in args.env:
        if "=" not in item:
            raise SetupError(f"--env takes NAME=VALUE, and {item!r} has no =")
    container = ContainerSpec(
        image=args.image,
        workdir=args.workdir,
        user=args.user,
        read_only_root=not args.writable_root,
        capabilities=tuple(args.cap),
        environment=dict(item.partition("=")[::2] for item in args.env),
        memory=args.memory,
        cpus=args.cpus,
    )
    task = Task(
        prompt=args.prompt,
        key=args.key,
        system_prompt=args.system_prompt,
        setup_files=files,
        max_turns=args.max_turns,
        # Without a token budget the time limit is the model's and has a default.
        timeout=args.timeout
        if args.timeout is not None or args.max_generated_tokens is not None
        else 300,
        max_generated_tokens=args.max_generated_tokens,
        max_command_seconds=args.max_command_seconds,
        container=container,
    )
    # The check a task file gets: what is refused there must not start a
    # container here.
    check_task(task, _flag_refusal)
    return task


# Where a task's value stands, as the flag that gives it.
_FLAG_OF = {"prompt": "--prompt", "max_turns": "--max-turns", "files": "--file"}


def _flag_refusal(where: str, why: str) -> SetupError:
    """The refusal of a task the flags describe, naming the flag."""
    if where == "container":
        return SetupError(f"the container the flags describe cannot be: {why}")
    return SetupError(f"{_FLAG_OF.get(where, where)}: {why}")


def _rules(said: str) -> RuleSet | str | None:
    """The rule set ``--rules`` names: the shipped one, none, or a rule file."""
    if said in ("shipped", "none"):
        return None if said == "none" else said
    try:
        return RuleSet.from_file(said)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise SetupError(
            f"--rules: {said} cannot be used as a rule file ({type(error).__name__}: {error})"
        ) from None


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        try:
            return _main(args, _given(argv))
        except SetupError as error:
            # Nothing was run: the command line, the task file or the
            # configuration cannot work.
            _put(sys.stderr, f"turnstone-rollout: {error}{_NL}")
            return REFUSED
    finally:
        # The status is the command's last word, on every way out of it: no
        # text that anyone wrote to an output stream that does not take it
        # may replace it when the process ends.
        _settle(sys.stdout)
        _settle(sys.stderr)


def _probe_only(args: argparse.Namespace, api_key: str, extra_body: dict[str, Any]) -> int:
    """Probe the server and print the verdict, for a caller that probes once
    and hands the verdict to every rollout of a batch."""
    sampling = resolve_sampling(
        model=args.model,
        base_url=args.base_url,
        temperature=args.temperature,
        reasoning_effort=args.effort,
        extra_body=extra_body,
    )
    verdict = _logprob_verdict(
        RolloutConfig(
            base_url=args.base_url,
            model=args.model,
            api_key=api_key,
            extra_body=sampling["extra_body"].value,
        )
    )
    # The status is the verdict's, whatever becomes of printing it.
    status = 0 if verdict.get("sampler_logprobs") is True else 2
    line = _json_text(verdict)
    if args.out and not _written(args.out, line + _NL, "verdict"):
        # This command has no verdict to put there, so the earlier one goes:
        # it must not stand for this probe.
        _gone(args.out)
        status = 1
    _put(sys.stdout, line + _NL)
    return status


def _main(args: argparse.Namespace, given: set[str]) -> int:
    # A probe's verdict is read by the rollouts of a batch that is under way.
    # The one an earlier probe left stays readable until the new one takes
    # its place in one step, and is removed when this command ends with none.
    replacing = bool(args.probe_only and args.out is not None)
    if args.out is not None:
        # First of all: whatever is refused or fails from here on, no file of
        # an earlier run is left where this run's record belongs.
        _claim(args.out, _read_by(args), remove=not replacing)
    if not replacing:
        return _command(args, given)
    try:
        return _command(args, given)
    except BaseException:
        _gone(args.out)
        raise


def _command(args: argparse.Namespace, given: set[str]) -> int:
    """What the command does once its ``--out`` is looked at."""
    if args.config is not None:
        set_config_path(args.config)
    # Read now, so that a configuration file that cannot be used is refused
    # before anything is asked of anyone, whatever this command goes on to do.
    read_configuration()
    try:
        extra_body: dict[str, Any] = strict_json(args.extra_body) if args.extra_body else {}
    except (ValueError, RecursionError):
        raise SetupError("--extra-body is not JSON") from None
    if not isinstance(extra_body, dict):
        raise SetupError("--extra-body must be a JSON object")
    if args.top_k is not None:
        extra_body["top_k"] = args.top_k
    if args.top_p is not None:
        extra_body["top_p"] = args.top_p
    api_key = "none"
    if args.api_key_env is not None:
        if not os.environ.get(args.api_key_env):
            raise SetupError(f"--api-key-env: the environment has no {args.api_key_env}")
        api_key = os.environ[args.api_key_env]
    if args.probe_only:
        return _probe_only(args, api_key, extra_body)
    task = _task(args, given)
    verifier_command: list[str] | None = None
    if args.verifier_command is not None:
        try:
            verifier_command = shlex.split(args.verifier_command)
        except ValueError as error:
            raise SetupError(
                f"--verifier-command cannot be split into arguments: {error}"
            ) from None
        if not verifier_command:
            raise SetupError("--verifier-command names no program")
        # The check a run makes itself, made here before anything else is
        # looked at: a placeholder in the program's own name, a sidecar's
        # name asked of a task that has none.
        check_verifier_command(verifier_command, has_sidecar=task.sidecar is not None)
    elif args.verifier_timeout is not None:
        raise SetupError("--verifier-timeout is for --verifier-command, which is not given")
    config = RolloutConfig(
        base_url=args.base_url,
        model=args.model,
        api_key=api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        context_window=args.context_window,
        tool_timeout=args.tool_timeout,
        subagent_max_turns=args.subagent_max_turns,
        max_subagents=args.max_subagents,
        reasoning_effort=args.effort,
        keep_text=args.keep_text,
        extra_body=extra_body,
        sampler_logprobs=_verdict_given(args.sampler_logprobs),
        max_output_chars=args.max_output_chars,
        max_read_bytes=args.max_read_bytes,
        server_stall_seconds=args.server_stall_seconds,
        rules=_rules(args.rules),
        min_tokens_per_second=args.min_tokens_per_second,
        min_prompt_tokens_per_second=args.min_prompt_tokens_per_second,
        server_version_url=args.server_version_url,
        server_version_field=args.server_version_field,
    )
    check: dict[str, Any] = {}
    if verifier_command is not None:
        check["verifier_command"] = verifier_command
        if args.verifier_timeout is not None:
            check["verifier_timeout"] = args.verifier_timeout
    record = run_episode(task, config, **check)
    # The status is read off the record, before anything is written or
    # printed: nothing that happens to the summary below can change it.
    status = _status(record)
    if args.out and not _written(args.out, _json_text(record) + _NL, "record"):
        # The rollout is lost with its record; no caller may take it for one
        # that is there.
        return 1
    try:
        summary = _NL.join(_summary(record)) + _NL
    except Exception as error:  # the summary is for a reader and decides nothing
        _put(
            sys.stderr,
            f"turnstone-rollout: the summary could not be made ({type(error).__name__}: "
            f"{error}); the record and the exit status stand{_NL}",
        )
    else:
        _put(sys.stdout, summary)
    return status


def _status(record: dict[str, Any]) -> int:
    """The exit status a record stands for."""
    if record["finish_cause"] == "machinery":
        return 1
    return 0 if record["trainable"] else 2


def _json_text(value: dict[str, Any]) -> str:
    """*value* as JSON a strict reader takes.

    A number JSON has no way to write (NaN, an infinity) can reach a record
    from outside: a server can report one as a log-probability, a verifier can
    print one.  Each is written as null, and a record says so among its
    ``warnings``, here and in the file.
    """
    try:
        return json.dumps(value, allow_nan=False)
    except ValueError:
        found: list[str] = []
        clean = _finite_only(value, "", found)
    if isinstance(value.get("warnings"), list):
        said = ", ".join(found[:8]) + (" and others" if len(found) > 8 else "")
        value["warnings"].append(
            f"{len(found)} of the record's values are no finite numbers and are written as "
            f"null, because JSON has none: {said}"
        )
        clean["warnings"] = list(value["warnings"])
    return json.dumps(clean, allow_nan=False)


def _finite_only(value: Any, where: str, found: list[str]) -> Any:
    """A copy of *value* with every number that is not finite replaced by
    ``None``; *found* is given where each stood."""
    if isinstance(value, float) and not math.isfinite(value):
        found.append(where)
        return None
    if isinstance(value, dict):
        return {
            key: _finite_only(item, f"{where}.{key}" if where else str(key), found)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_finite_only(item, f"{where}[{index}]", found) for index, item in enumerate(value)]
    return value


def _written(path: str, text: str, what: str) -> bool:
    """Write *text* to *path* whole; where that fails say so on standard error
    and return ``False``.  Never raises for the file's sake."""
    try:
        _write_whole(path, text)
    except OSError as error:
        _put(
            sys.stderr,
            f"turnstone-rollout: the {what} could not be written to {path} "
            f"({type(error).__name__}: {error}); no file is there{_NL}",
        )
        return False
    return True


def _summary(record: dict[str, Any]) -> list[str]:
    """What a rollout's record says, as lines for a reader at a terminal."""
    lines: list[str] = []

    def say(*parts: object) -> None:
        lines.append(" ".join(str(part) for part in parts))

    tokens = record["tokens"]
    say(
        f"finish_state: {record['finish_state']} (cause: {record['finish_cause']})",
        "| error:",
        _shown(record["error"]),
    )
    say("turns:", record["turns"], "| seconds:", record["seconds"], "|", json.dumps(record["time"]))
    for name, limit in record["limits"].items():
        say(f"  limit {name}: {limit['value']} ({limit['source']})")
    for done in (record["before_run"] or {}).get("commands", []):
        say(
            f"  before the run: {_shown(json.dumps(done['argv'])[:100])} exit "
            f"{done['exit_code']} in {done['seconds']} s"
        )
    say("budgets:", json.dumps(record["budgets"]))
    if record["timeout"]:
        say("at the time limit:", json.dumps(record["timeout"]))
    say("sampling:", json.dumps(record["sampling"]["requested"]))
    for name, source in record["sampling"]["sources"].items():
        say(f"  setting {name}: {json.dumps(record['sampling'][name])} ({source})")
    say("log-probabilities:", json.dumps(record["logprobs"]))
    if record["server_version"] is not None:
        say("server version:", _shown(json.dumps(record["server_version"])))
    for call in tokens["calls"]:
        logprobs = "none" if call["logprobs"] is None else len(call["logprobs"])
        say(
            f"  call {call['call']}: prompt {call['prompt_len']} tokens ({call['shared']} shared "
            f"with the sequence so far), generated {len(call['token_ids'])}, log-probabilities "
            f"{logprobs}, finish {_shown(call['finish_reason'])}, in final sequence: "
            f"{call['in_final']}"
        )
    say("final sequence:", tokens["final_len"], "tokens | generated:", record["generated_tokens"])
    if record["one_sequence"]:
        say("generated tokens with a position:", len(generated_positions(tokens)))
    say("prefix check:", "held" if record["prefix_ok"] else "BROKEN")
    for line in record["prefix_breaks"]:
        say("  " + _shown(line))
    say("prefix events:", record["prefix_events"] or "none")
    say("trainable:", "yes" if record["trainable"] else "NO")
    say("one sequence:", "yes" if record["one_sequence"] else "NO")
    for line in record["problems"]:
        say("  problem: " + _shown(line))
    for line in record["warnings"]:
        say("  warning: " + _shown(line))
    say("discarded calls:", len(record["discarded_calls"]))
    sub = record["subagents"]
    say(
        f"sub-agents: {sub['started']} started, {sub['refused_at_limit']} refused at a limit, "
        f"{sub['model_calls']} model calls, {sub['generated_tokens']} generated tokens "
        f"(not for training), failures: {_shown(sub['failures'] or 'none')}"
    )
    rules = record["rules"]
    if rules is not None:
        say(f"rules: {rules['name']} ({rules['rules']} rules, {rules['sha256'][:12]})")
    for entry in record["tool_log"]:
        arguments = json.dumps(entry["args"])[:100]
        rule = f" rule={entry['rule']} ({entry['rule_says']})" if "rule" in entry else ""
        say(
            f"  tool turn {entry['turn']} {_shown(entry['tool'])} {arguments} "
            f"ok={entry['ok']}{rule}"
        )
    say("final content:", _shown((record["final_content"] or "")[:300]))
    checked = record["verification"]
    if checked is not None:
        say(
            "check after the run:",
            _shown(json.dumps(checked["result"])[:300]),
            "| failure:",
            _shown(checked["failure"]),
        )
    say("sandbox:", json.dumps(record["sandbox"]))
    return lines


if __name__ == "__main__":
    sys.exit(main())
