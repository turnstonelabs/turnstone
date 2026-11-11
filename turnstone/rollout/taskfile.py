"""A task as a JSON file, for a caller that starts ``turnstone-rollout`` as a
process and cannot build a :class:`~turnstone.rollout.episode.Task` itself.

The file is one JSON object that can say everything a ``Task`` holds::

    {
      "prompt": "Fix the failing test.",
      "key": "task-0042",
      "system_prompt": "...",
      "files": {"notes.txt": "plain text", "data.bin": {"base64": "AAEC"}},
      "max_turns": 30,
      "timeout": 900,
      "max_generated_tokens": 60000,
      "max_command_seconds": 600,
      "container": {"image": "...", "workdir": "/testbed", "user": "0:0", "read_only_root": false},
      "before_run": [{"argv": ["sh", "-c", "..."], "timeout": 120}],
      "after_run": [{"argv": ["sh", "-c", "..."], "collect": ["report.txt"]}],
      "sidecar": {"container": {...}, "files": {...}, "setup": [...], "wait_ports": [39101],
                  "mcp_servers": {"ledger": "http://127.0.0.1:39101/mcp"}}
    }

Only ``prompt`` is required.  Every other key has the default the class has,
and the nested objects take the fields of their classes by name:
``container`` those of ``ContainerSpec``, the items of ``before_run`` and
``after_run`` those of ``BeforeRunCommand`` and ``AfterRunCommand``,
``sidecar`` those of ``Sidecar``.

A file's content is a string (text, written as UTF-8), ``{"text": "..."}`` or
``{"base64": "..."}``.  A task's files go to its working directory unless the
path is absolute; a sidecar's paths are absolute.

The file is read strictly, because a task that is not the one its author
meant gives rollouts that look sound and are not:

- a key the classes do not have is refused with its name, and so is a key
  given twice: a misspelt key must never become a default without a word;
- a value of the wrong kind is refused (``true`` is no number, ``"5"`` is no
  number, a number is no string), and so is what is no number at all: ``NaN``,
  ``Infinity`` and a number too large to be one are not JSON, and a limit that
  is one of them would not be a limit.

``timeout`` left out is 300 seconds, or with ``max_generated_tokens`` worked
out from the budgets, as on the command line; ``null`` asks for that
explicitly.

A task file describes what happens inside containers.  It cannot name a
program to run on the machine the rollout runs on: a verifier command is the
operator's, and is given on the command line.

Every refusal is a :class:`~turnstone.rollout.settings.SetupError`: nothing
was run.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import difflib
import json
import math
import types
import typing
from typing import TYPE_CHECKING, Any

from turnstone.rollout.container import ContainerSpec
from turnstone.rollout.episode import Task
from turnstone.rollout.preparation import BeforeRunCommand
from turnstone.rollout.settings import SetupError, strict_json
from turnstone.rollout.sidecar import Sidecar
from turnstone.rollout.verification import AfterRunCommand

if TYPE_CHECKING:
    from collections.abc import Callable

# The classes a task file's objects are read into.  Their fields are the keys.
CLASSES = (Task, ContainerSpec, BeforeRunCommand, AfterRunCommand, Sidecar)
# ``Task`` keeps text and other content apart; the file has one ``files``.
_TASK_KEYS = tuple(f.name for f in dataclasses.fields(Task) if f.name != "setup_files")


def _refuse(where: str, why: str) -> SetupError:
    return SetupError(f"task file: {where}: {why}" if where else f"task file: {why}")


def _unknown(key: str, known: typing.Iterable[str], where: str) -> SetupError:
    names = sorted(known)
    close = difflib.get_close_matches(key, names, n=1)
    hint = f" (did you mean {close[0]!r}?)" if close else ""
    place = f"in {where}" if where else "at the top"
    return SetupError(
        f"task file: unknown key {key!r} {place}{hint}; the keys there are {', '.join(names)}"
    )


def _content(value: Any, where: str) -> str | bytes:
    """A file's content: a string is text; an object says which it is."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        ((kind, said),) = value.items()
        if kind == "text" and isinstance(said, str):
            return said
        if kind == "base64" and isinstance(said, str):
            try:
                return base64.b64decode(said, validate=True)
            except (binascii.Error, ValueError) as error:
                raise _refuse(where, f"not base64 ({error})") from None
    raise _refuse(where, 'a file\'s content is a string, {"text": "..."} or {"base64": "..."}')


def _files(value: Any, where: str) -> dict[str, str | bytes]:
    if not isinstance(value, dict):
        raise _refuse(where, "must be an object of paths and their contents")
    out: dict[str, str | bytes] = {}
    for path, content in value.items():
        if not path:
            raise _refuse(where, "a file's path is empty")
        out[path] = _content(content, f"{where}[{path!r}]")
    return out


def _convert(hint: Any, value: Any, where: str) -> Any:
    """*value* as the kind of thing *hint* names, or a refusal that says where."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        options = [h for h in typing.get_args(hint) if h is not type(None)]
        if value is None and len(options) < len(typing.get_args(hint)):
            return None
        if len(options) == 1:
            return _convert(options[0], value, where)
    elif dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return _build(hint, value, where)
    elif hint is bool:
        if isinstance(value, bool):
            return value
        raise _refuse(where, f"must be true or false, not {json.dumps(value)[:60]}")
    elif hint is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise _refuse(where, f"must be a whole number, not {json.dumps(value)[:60]}")
    elif hint is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise _refuse(where, f"must be a finite number, not {value}")
            return value
        raise _refuse(where, f"must be a number, not {json.dumps(value)[:60]}")
    elif hint is str:
        if isinstance(value, str):
            return value
        raise _refuse(where, f"must be a string, not {json.dumps(value)[:60]}")
    elif origin in (list, tuple):
        if not isinstance(value, list):
            raise _refuse(where, "must be a list")
        item = typing.get_args(hint)[0]
        items = [_convert(item, v, f"{where}[{n}]") for n, v in enumerate(value)]
        return tuple(items) if origin is tuple else items
    elif origin is dict:
        _key, item = typing.get_args(hint)
        if item is bytes:
            # A sidecar's files: every content as bytes.
            return {
                path: content.encode("utf-8") if isinstance(content, str) else content
                for path, content in _files(value, where).items()
            }
        if not isinstance(value, dict):
            raise _refuse(where, "must be an object")
        return {key: _convert(item, v, f"{where}[{key!r}]") for key, v in value.items()}
    raise _refuse(where, f"this reader does not know how to read {hint!r}")


def _hints(cls: type) -> dict[str, Any]:
    """The kind of thing each field of *cls* holds."""
    return typing.get_type_hints(cls)


def _build(cls: type, data: Any, where: str) -> Any:
    """An instance of the dataclass *cls* from the JSON object *data*."""
    if not isinstance(data, dict):
        raise _refuse(where, "must be an object")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    hints = _hints(cls)
    for key in data:
        if key not in fields:
            raise _unknown(key, fields, where)
    values: dict[str, Any] = {}
    for name, field in fields.items():
        if name in data:
            values[name] = _convert(hints[name], data[name], f"{where}.{name}" if where else name)
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise _refuse(where, f"{name} is missing")
    return cls(**values)


def task_from_data(data: Any) -> Task:
    """The :class:`Task` a task file's JSON object describes.

    Raises :class:`SetupError` for anything the file must not hold.
    """
    if not isinstance(data, dict):
        raise _refuse("", "the file must hold one JSON object")
    for key in data:
        if key not in _TASK_KEYS:
            if key == "setup_files":
                raise _refuse("", "unknown key 'setup_files': a task file has one key, 'files'")
            raise _unknown(key, _TASK_KEYS, "")
    if "prompt" not in data:
        raise _refuse("", "prompt is missing")
    hints = _hints(Task)
    values: dict[str, Any] = {}
    for name in _TASK_KEYS:
        if name in data and name != "files":
            values[name] = _convert(hints[name], data[name], name)
    contents = _files(data.get("files", {}), "files")
    values["setup_files"] = {p: c for p, c in contents.items() if isinstance(c, str)}
    values["files"] = {p: c for p, c in contents.items() if isinstance(c, bytes)}
    if "timeout" not in data:
        # As on the command line: the model's time has a default; a backstop
        # for the machinery is worked out from the budgets.
        values["timeout"] = 300 if values.get("max_generated_tokens") is None else None
    task = Task(**values)
    check_task(task)
    return task


def check_task(task: Task, refuse: Callable[[str, str], SetupError] = _refuse) -> None:
    """Refuse what no class refuses by itself: a task that could not be run as
    it is, whichever way it was given.

    A task file is checked with it, and so is a task the command line's flags
    describe: a value that is refused in one must not start a container in the
    other.  *refuse* makes the refusal from where in the task the value stands
    and what is wrong with it; the command passes one that names the flag.
    """
    if not task.prompt.strip():
        raise refuse("prompt", "is empty")
    if task.max_turns < 1:
        raise refuse("max_turns", "must be at least 1")
    for path in (*task.setup_files, *task.files):
        if not path:
            raise refuse("files", "a file's path is empty")
    specs = [("container", task.container)]
    if task.sidecar is not None:
        specs.append(("sidecar.container", task.sidecar.container))
        if task.sidecar.setup is not None and not task.sidecar.setup:
            raise refuse("sidecar.setup", "names no program")
        if task.sidecar.setup_timeout <= 0:
            raise refuse("sidecar.setup_timeout", "must be above zero")
        if task.sidecar.wait_timeout < 0:
            raise refuse("sidecar.wait_timeout", "must not be below zero")
        for number, port in enumerate(task.sidecar.wait_ports):
            if not 1 <= port <= 65535:
                raise refuse(f"sidecar.wait_ports[{number}]", "must be a port, 1 to 65535")
        for path in task.sidecar.files:
            if not path.startswith("/"):
                raise refuse(f"sidecar.files[{path!r}]", "a sidecar's paths are absolute")
    for where, spec in specs:
        try:
            spec.validate()
        except ValueError as error:
            raise refuse(where, str(error)) from None
    commands: list[tuple[str, BeforeRunCommand | AfterRunCommand]] = [
        (f"before_run[{n}]", c) for n, c in enumerate(task.before_run)
    ]
    commands += [(f"after_run[{n}]", c) for n, c in enumerate(task.after_run)]
    for where, command in commands:
        if not command.argv:
            raise refuse(f"{where}.argv", "names no program")
        if command.timeout <= 0:
            raise refuse(f"{where}.timeout", "must be above zero")
        if command.max_output_chars < 0:
            raise refuse(f"{where}.max_output_chars", "must not be below zero")
        if isinstance(command, AfterRunCommand) and command.max_collect_bytes <= 0:
            # At zero every file read out would count as more than is read.
            raise refuse(f"{where}.max_collect_bytes", "must be above zero")


def _no_key_twice(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SetupError(f"task file: the key {key!r} is given twice in one object")
        out[key] = value
    return out


def load_task(path: str) -> Task:
    """Read the task file at *path*.  Raises :class:`SetupError` when it cannot
    be read or holds what a task file must not."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = strict_json(handle.read(), object_pairs_hook=_no_key_twice)
    except OSError as error:
        raise SetupError(f"task file: {path} cannot be read ({error})") from None
    except (ValueError, RecursionError) as error:
        if isinstance(error, SetupError):
            raise
        raise SetupError(f"task file: {path} is not JSON ({error})") from None
    return task_from_data(data)


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def task_to_data(task: Task) -> dict[str, Any]:
    """The JSON object that describes *task*: what :func:`task_from_data` reads
    back into an equal task."""
    data: dict[str, Any] = _plain(task)
    text = data.pop("setup_files")
    data["files"] = {**text, **data["files"]}
    return data
