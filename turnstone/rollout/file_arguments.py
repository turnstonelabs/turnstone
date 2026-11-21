"""Tool arguments the model may give as a file in its own sandbox.

A task's sidecar can name arguments of its servers' tools that take the bytes
of a file as base64 (``Sidecar.base64_file_arguments``).  Writing out a long
base64 text one token at a time is a skill of its own, one such a task does not
mean to measure: a model that has made the file can still fail to hand it over.
So the model may give one of these arguments as ``file:///ABSOLUTE/PATH``, a
file in its own sandbox, in place of the text.

:class:`Base64FileArguments` stands in for the sidecar's MCP client.  Before a
call to one of the named tools it reads each file so named from the model's
container, never from the sidecar, and sends the file's bytes as base64 in a
copy of the arguments.  What the model wrote stays as it wrote it: in the
conversation, in the next prompt and in the record.  Everything other than a
call to one of those tools is the client's own.

A reference the model has to fix (a path that is not absolute; no file there;
a directory, or anything else that is not a regular file; a file it may not
read; a file of more than :data:`MAX_FILE_BYTES`) is answered with an
``{"error": ...}`` object, the way a tool answers an argument it cannot use,
and the server is not called.  A failure of the machinery, such as the
container gone or a read that does not finish, is not the model's to see or to
work around: the client is told of it (*failed*), which stops the run as the
machinery's, and the call raises :class:`FileArgumentError`.
"""

from __future__ import annotations

import base64
import errno
import json
from typing import TYPE_CHECKING, Any

import mcp.types as mcp_types

from turnstone.rollout.container import ContainerLostError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

PREFIX = "file://"

# The most bytes one argument may name.  Its base64 is a third more, and has to
# fit into one request to the task's server along with the rest of the call.
MAX_FILE_BYTES = 16 * 1024 * 1024

# What the model is told for each refusal of the read, by its errno.
_REFUSALS = {
    errno.ENOENT: "file_not_found",
    errno.EISDIR: "is_a_directory",
    errno.EINVAL: "not_a_regular_file",
    errno.EACCES: "not_readable",
    errno.EFBIG: "file_too_large",
}


class FileArgumentError(RuntimeError):
    """The machinery could not read a file the model named: the call was not made."""


class _RefusedReferenceError(Exception):
    """A reference the model has to fix, with the answer it is given."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__(answer["error"])
        self.answer = answer


class Base64FileArguments:
    """A sidecar's MCP client whose calls to the named tools have their
    ``file://`` arguments read from the model's sandbox first.

    *arguments* holds the names of the arguments by tool, the tool named as the
    model calls it (``mcp__SERVER__TOOL``).  *read* returns all of a file in the
    model's sandbox that has at most the number of bytes it is given, and raises
    :class:`OSError` for any other (``ContainerEnvironment.read_whole``).
    *failed* is told what went wrong when the machinery fails a read; it stops
    the run (``ContainerSandbox``).
    """

    def __init__(
        self,
        client: Any,
        arguments: Mapping[str, list[str]],
        read: Callable[[str, int], bytes],
        failed: Callable[[str], None],
    ) -> None:
        self._client = client
        self._arguments = {tool: tuple(names) for tool, names in arguments.items()}
        self._read = read
        self._failed = failed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def call_tool_sync(
        self, func_name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> mcp_types.CallToolResult:
        sent = arguments
        names = self._arguments.get(func_name, ())
        if names:
            sent = dict(arguments)
            try:
                for name in names:
                    value = arguments.get(name)
                    if isinstance(value, str) and value.startswith(PREFIX):
                        sent[name] = self._encoded(func_name, name, value[len(PREFIX) :])
            except _RefusedReferenceError as refused:
                return mcp_types.CallToolResult(
                    content=[mcp_types.TextContent(type="text", text=json.dumps(refused.answer))]
                )
        result: mcp_types.CallToolResult = self._client.call_tool_sync(func_name, sent, **kwargs)
        return result

    def _encoded(self, tool: str, name: str, path: str) -> str:
        """The base64 of the file at *path* in the model's sandbox."""
        about: dict[str, Any] = {"argument": name, "path": path}
        if not path.startswith("/"):
            raise _RefusedReferenceError({"error": "path_not_absolute", **about})
        try:
            data = self._read(path, MAX_FILE_BYTES)
        except Exception as error:
            refusal = None
            if isinstance(error, OSError) and not isinstance(error, ContainerLostError):
                refusal = _REFUSALS.get(error.errno) if error.errno is not None else None
            if refusal is None:
                what = (
                    f"the machinery could not read {path}, which the model named for "
                    f"{tool} ({name}): {type(error).__name__}: {str(error)[:300]}"
                )
                self._failed(what)
                raise FileArgumentError(what) from error
            if refusal == "file_too_large":
                about["max_bytes"] = MAX_FILE_BYTES
            raise _RefusedReferenceError({"error": refusal, **about}) from None
        return base64.b64encode(data).decode("ascii")
