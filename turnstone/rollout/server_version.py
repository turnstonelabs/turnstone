"""Which version of the model server a rollout ran on.

A server whose weights are replaced in place between batches serves two
models under one name.  A rollout that saw both cannot be trained on: its
tokens were sampled from two policies, and nothing in the token data says
which token came from which.

So a rollout can be given a URL that answers with the server's version (a
JSON value, or one named field of a JSON object).  It is asked twice: right
before the first model call of the run can go out, the probe of the server's
log-probabilities and every sub-agent call included, and right after the
session's last model call has returned.  Both answers are in the record.

What this shows, and what it takes for granted:

- A version that only ever moves forward cannot change and change back.  The
  same answer at both ends therefore means the same version in between.
- The server must change its version together with its weights: no later than
  the new weights serve a request, and no earlier than the old ones stop.  A
  server that swaps the weights first and reports the new version a moment
  later leaves a gap in which a rollout could end on new weights under the old
  version, and this check cannot see into that gap.
- It is one question at each end, not one per model call: a version that
  differs says that the rollout saw two versions, not which calls saw which.

A first answer that cannot be had means the run does not start.  A last
answer that cannot be had, or one that differs from the first, makes the
record not trainable; how the run ended stays what it was.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx2

from turnstone.core.log import get_logger
from turnstone.rollout.settings import SetupError

log = get_logger(__name__)

# One question: how long the server has to answer, from the connection to the
# last byte of its answer; how often it is asked again when it does not; and
# how long between two tries.
_TIMEOUT_SECONDS = 10.0
_TRIES = 3
_PAUSE_SECONDS = 1.0
# The most that is read of an answer.  A version is a few bytes.
_MAX_ANSWER_BYTES = 64 * 1024


class _NoAnswerError(Exception):
    """One try brought no version.  ``again`` says whether another try may."""

    def __init__(self, why: str, *, again: bool) -> None:
        super().__init__(why)
        self.again = again


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """The scheme, host and port *url* names; ``None`` for one that names none
    (a port that is no port, a host in brackets that do not close)."""
    try:
        parts = urlsplit(url)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None
    return parts.scheme, (parts.hostname or "").lower(), port


def _ask_once(url: str, field: str | None, headers: dict[str, str]) -> Any:
    """One try, bounded as a whole.

    The client's own time limit is one for each step: for the connection, and
    for every read by itself.  A server that sends its answer a few bytes at a
    time keeps every read short and the question open without end.  So the
    try is made on a thread of its own and given up when its time has passed;
    the thread sees that at its next read and ends there.
    """
    limit = _TIMEOUT_SECONDS
    deadline = time.monotonic() + limit
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["answer"] = _read(url, field, headers, limit, deadline)
        except BaseException as error:  # handed to the caller below
            box["error"] = error

    worker = threading.Thread(target=work, daemon=True, name="rollout-server-version")
    worker.start()
    worker.join(max(deadline - time.monotonic(), 0.0))
    if worker.is_alive():
        raise _NoAnswerError(f"no whole answer within {limit:g} s", again=True)
    if "error" in box:
        raise box["error"]
    return box["answer"]


def _read(
    url: str, field: str | None, headers: dict[str, str], limit: float, deadline: float
) -> Any:
    try:
        with httpx2.stream("GET", url, headers=headers, timeout=limit) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_ANSWER_BYTES:
                    raise _NoAnswerError(
                        f"the answer is larger than {_MAX_ANSWER_BYTES} bytes", again=False
                    )
                if time.monotonic() > deadline:
                    raise _NoAnswerError(f"no whole answer within {limit:g} s", again=True)
            status = response.status_code
    except httpx2.HTTPError as error:
        raise _NoAnswerError(f"{type(error).__name__}: {str(error)[:200]}", again=True) from error
    if status != 200:
        # A server that is busy or restarting may answer the next time; one
        # that has no such address will not.
        raise _NoAnswerError(f"the server answered with status {status}", again=status >= 500)
    try:
        answer = json.loads(bytes(body))
    except (ValueError, RecursionError) as error:
        raise _NoAnswerError("the answer is not JSON", again=False) from error
    if field is not None:
        if not isinstance(answer, dict) or field not in answer:
            raise _NoAnswerError(
                f"the answer is no JSON object with the field {field!r}", again=False
            )
        answer = answer[field]
    if answer is None:
        raise _NoAnswerError("the answer names no version (null)", again=False)
    return answer


def ask(
    url: str,
    field: str | None = None,
    *,
    headers: dict[str, str] | None = None,
    tries: int | None = None,
    pause: float | None = None,
) -> tuple[Any, str | None]:
    """Ask *url* for the server's version: ``(version, None)``, or
    ``(None, why)`` when it could not be had.  Never raises.

    The version is the JSON value the server answers with, or with *field* the
    value of that field of the JSON object it answers with.  A server that
    does not answer is asked again, *tries* times in all (three unless given),
    *pause* seconds apart (one unless given); each try has ten seconds as a
    whole.
    """
    why = "not asked"
    for attempt in range(max(_TRIES if tries is None else tries, 1)):
        if attempt:
            time.sleep(_PAUSE_SECONDS if pause is None else pause)
        try:
            return _ask_once(url, field, dict(headers or {})), None
        except _NoAnswerError as none:
            why = str(none)
            if not none.again:
                break
        except Exception as error:  # an answer is never worth a lost rollout
            why = f"{type(error).__name__}: {str(error)[:200]}"
            break
    return None, why


class ServerVersion:
    """The server's version around one rollout: asked before its first model
    call and after its last."""

    def __init__(
        self, url: str, field: str | None = None, *, base_url: str = "", api_key: str = ""
    ) -> None:
        asked = _origin(url)
        if asked is None or asked[0] not in ("http", "https") or not asked[1]:
            raise SetupError(f"the server's version is asked over HTTP: {url!r} is no such address")
        self.url = url
        self.field = field
        # The key the model calls carry goes with the question only where it
        # is put to the model's own server.
        self._headers: dict[str, str] = {}
        if api_key and api_key != "none" and base_url and asked == _origin(base_url):
            self._headers["Authorization"] = f"Bearer {api_key}"
        self.first: Any = None
        self.last: Any = None
        self.first_error: str | None = None
        self.last_error: str | None = None
        self.first_at: float | None = None
        self.last_at: float | None = None
        self._last_asked = False

    def ask_first(self) -> str | None:
        """Ask before the run; why the version could not be had, else ``None``."""
        self.first, self.first_error = ask(self.url, self.field, headers=self._headers)
        self.first_at = round(time.time(), 3)
        if self.first_error is not None:
            log.error("rollout.server_version_unknown", url=self.url, error=self.first_error)
        return self.first_error

    def ask_last(self) -> None:
        """Ask after the session's last model call has returned."""
        self._last_asked = True
        self.last, self.last_error = ask(self.url, self.field, headers=self._headers)
        self.last_at = round(time.time(), 3)

    @property
    def same(self) -> bool | None:
        """Whether both ends gave the same version; ``None`` while one is missing."""
        if self.first is None or self.last is None:
            return None
        return bool(self.first == self.last)

    def problems(self) -> list[str]:
        """Why a run that was asked about at both ends cannot be trained on."""
        if self.first is None or not self._last_asked:
            return []
        if self.last is None:
            return [
                "the server's version could not be asked after the run "
                f"({self.last_error}), so it is not known whether it changed during the run"
            ]
        if self.first != self.last:
            return [
                "the server's version changed during the run: "
                f"{json.dumps(self.first)} before the first model call, "
                f"{json.dumps(self.last)} after the last"
            ]
        return []

    def describe(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "field": self.field,
            "first": self.first,
            "last": self.last,
            "same": self.same,
            "first_error": self.first_error,
            "last_error": self.last_error,
            # When each question was answered, in seconds since the epoch.
            "first_at": self.first_at,
            "last_at": self.last_at,
        }
