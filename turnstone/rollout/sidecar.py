"""A second container for one rollout, holding what the task brings: servers
the model reaches through MCP tools.

Some tasks come with systems of their own (mock services with their state),
served as MCP servers.  They run in a *sidecar*: a container of the task's,
beside the one the model's commands run in, started for that rollout and
removed with it.

What reaches what:

- The sidecar has no network, like the episode's container.  Its servers
  listen inside it and nothing outside can connect to them.
- The session runs outside both containers and is the only client.  It reaches
  a server through the runtime's own channel into the container: a listener on
  this machine's loopback interface hands each connection to a small relay
  started inside the sidecar, which connects to the server's port there.
  No address of the sidecar is routable from anywhere, and the sidecar has no
  route to this machine, to other rollouts or to the model's container.
- The listener carries a connection only when its first request names the
  secret this rollout made for it, which the session's own client sends with
  every request and nothing else knows.  Another user of this machine can
  connect to a loopback port, and would otherwise call the task's servers
  during a run; such a connection is closed before anything is started for it.
  What it can hold of the runner is bounded: the head of a request has one
  time limit for the whole of it, and only so many connections are read for
  theirs at a time.
- The secret stays between the session's client and the listener.  The header
  that names it is taken out before a request goes on, and each connection
  carries one request, so that no later request brings it to the task's own
  server either.
- The model's container has no path to the sidecar at all.  The model uses the
  servers as tools, through the session; every call is in the tool log.  A
  model that could connect to them itself would act on them unseen, and could
  reach whatever else the sidecar serves (a task's own health or grading
  endpoint).

The MCP client is built from the servers the task names and nothing else, so
no server configured on this machine is reachable; its tool list is read once
when the session is built.

What a model can do to a sidecar is call its tools, with any arguments.  That
changes the task's state, which is the task, and is what a check reads
afterwards.  It cannot start a process there or stop the container.  A sidecar
that does not come up, or stops during the run, is a failure of the machinery.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from turnstone.core.log import get_logger
from turnstone.rollout.container import Container, ContainerSpec
from turnstone.rollout.verification import build_archive

log = get_logger(__name__)

_NL = chr(10)


@dataclass
class Sidecar:
    """What a task's sidecar is and how it is brought up."""

    # A sidecar is the task's own: root over a writable root is usual.
    container: ContainerSpec
    # Files put in before the setup command, as root: path inside to content.
    files: dict[str, bytes] = field(default_factory=dict)
    # Run once, as the spec's user.  It must return; what it leaves running
    # (the servers) stays.  Its output goes to a file inside the container.
    setup: list[str] | None = None
    setup_timeout: int = 300
    # Ports that must be listening inside before the run starts.
    wait_ports: list[int] = field(default_factory=list)
    wait_timeout: int = 120
    # MCP servers by name, each with its address as seen inside the sidecar
    # (``http://127.0.0.1:PORT/path``).
    mcp_servers: dict[str, str] = field(default_factory=dict)


class SidecarError(RuntimeError):
    """The sidecar could not be brought up or reached."""


# Programs that connect standard input and output to a port inside the
# container, in the order they are looked for.  ``$1`` is the port.
_RELAY_PYTHON = (
    "import os,socket,sys,threading"
    + _NL
    + "s=socket.create_connection(('127.0.0.1',int(sys.argv[1])))"
    + _NL
    + "def up():"
    + _NL
    + "    try:"
    + _NL
    + "        while True:"
    + _NL
    + "            d=os.read(0,65536)"
    + _NL
    + "            if not d: break"
    + _NL
    + "            s.sendall(d)"
    + _NL
    + "    except OSError: pass"
    + _NL
    + "    try: s.shutdown(socket.SHUT_WR)"
    + _NL
    + "    except OSError: pass"
    + _NL
    + "threading.Thread(target=up,daemon=True).start()"
    + _NL
    + "try:"
    + _NL
    + "    while True:"
    + _NL
    + "        d=s.recv(65536)"
    + _NL
    + "        if not d: break"
    + _NL
    + "        os.write(1,d)"
    + _NL
    + "except OSError: pass"
    + _NL
)
_RELAYS: dict[str, list[str]] = {
    "python3": ["python3", "-c", _RELAY_PYTHON],
    "socat": ["sh", "-c", 'exec socat - "TCP:127.0.0.1:$1"', "sh"],
    "nc": ["sh", "-c", 'exec nc 127.0.0.1 "$1"', "sh"],
}

# Succeeds when every port given is in the listening state.  Read from the
# kernel's table: nothing is connected to, and no program but the shell and
# ``grep`` is needed.
_LISTENING = (
    'for p in "$@"; do h=$(printf "%04X" "$p"); '
    'grep -Eqi "^ *[0-9]+: [0-9A-F]+:$h [0-9A-F]+:0000 0A " /proc/net/tcp /proc/net/tcp6 '
    "2>/dev/null || exit 1; done"
)


# The request header in which the session's client names a forward's secret.
FORWARD_HEADER = "x-turnstone-forward"
# The most that is read of a connection's first request to find the header,
# and the seconds a connection has to send that much, all of it together.
_REQUEST_HEAD_BYTES = 64 * 1024
_REQUEST_HEAD_SECONDS = 20.0
# How many connections are read for their head at a time.  The others wait in
# the listener's queue, so connections that send nothing, or a byte now and
# then, hold a bounded number of the runner's threads and sockets, each for
# the seconds above at most.
_WAITING_HEADS = 16
_CRLF = bytes([13, 10])
# Headers of a request that say how long its connection is to be kept.
_CONNECTION_HEADERS = (b"connection", b"keep-alive")


class PortForward:
    """A listener on this machine's loopback interface whose connections are
    carried to one port inside a container, through the runtime.

    A loopback port is open to every user of the machine.  A connection is
    carried only when the head of its first request holds *secret* in the
    header ``FORWARD_HEADER``; any other is closed, and nothing is started
    for it.  The head has ``_REQUEST_HEAD_SECONDS`` to arrive, all of it, and
    at most ``_WAITING_HEADS`` connections are read for theirs at a time.

    The request goes on without that header, and as the only one of its
    connection (``Connection: close``): only a connection's first request is
    looked at here, and the client names the secret in every one.
    """

    def __init__(self, container: Container, port: int, relay: list[str], secret: str) -> None:
        self._container = container
        self._port = port
        self._relay = relay
        self._secret = secret.encode("ascii")
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.local_port: int = self._listener.getsockname()[1]
        self._closed = threading.Event()
        self._procs: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()
        # One place for each connection whose head is being read.
        self._heads = threading.BoundedSemaphore(_WAITING_HEADS)
        self._acceptor = threading.Thread(target=self._accept, daemon=True, name="sidecar-forward")
        self._acceptor.start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            # Without a free place no further connection is taken: it waits
            # in the listener's queue, where it costs the runner nothing.
            if not self._heads.acquire(timeout=0.5):
                continue
            try:
                connection, _peer = self._listener.accept()
            except OSError:
                self._heads.release()
                return
            try:
                threading.Thread(target=self._carry, args=(connection,), daemon=True).start()
            except RuntimeError:
                # No thread to be had: this connection is not carried.
                self._heads.release()
                connection.close()

    def _request_head(self, connection: socket.socket) -> bytes | None:
        """What goes on of what *connection* sent up to the end of its first
        request's header lines, and of anything it sent after them, when one
        of the lines names the secret.  ``None`` for any other connection,
        also one that sends too much, or has not sent the whole head within
        the time a head may take.

        The header that names the secret does not go on, and the request is
        made the only one of its connection: see the class."""
        head = b""
        deadline = time.monotonic() + _REQUEST_HEAD_SECONDS
        try:
            while _CRLF + _CRLF not in head:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                connection.settimeout(remaining)
                data = connection.recv(65536)
                if not data or len(head) + len(data) > _REQUEST_HEAD_BYTES:
                    return None
                head += data
            connection.settimeout(None)
        except OSError:
            return None
        lines, _, after = head.partition(_CRLF + _CRLF)
        request, *headers = lines.split(_CRLF)
        named = False
        kept = [request]
        for line in headers:
            name, _, value = line.partition(b":")
            name = name.strip().lower()
            if name == FORWARD_HEADER.encode("ascii"):
                named = hmac.compare_digest(value.strip(), self._secret) or named
            elif name not in _CONNECTION_HEADERS:
                kept.append(line)
        if not named:
            return None
        kept.append(b"Connection: close")
        return _CRLF.join(kept) + _CRLF + _CRLF + after

    def _carry(self, connection: socket.socket) -> None:
        try:
            head = self._request_head(connection)
        finally:
            # The head is read, or will not be: the place is another's.
            self._heads.release()
        if head is None:
            connection.close()
            return
        argv = self._container.exec_argv([*self._relay, str(self._port)], stdin=True, user="0:0")
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError:
            connection.close()
            return
        with self._lock:
            self._procs.append(proc)
        assert proc.stdin is not None and proc.stdout is not None
        inward, outward = proc.stdin, proc.stdout

        def to_container() -> None:
            with contextlib.suppress(OSError, ValueError):
                inward.write(head)
                inward.flush()
                while True:
                    data = connection.recv(65536)
                    if not data:
                        break
                    inward.write(data)
                    inward.flush()
            with contextlib.suppress(OSError, ValueError):
                inward.close()

        threading.Thread(target=to_container, daemon=True).start()
        with contextlib.suppress(OSError, ValueError):
            while True:
                data = os.read(outward.fileno(), 65536)
                if not data:
                    break
                connection.sendall(data)
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        with self._lock, contextlib.suppress(ValueError):
            self._procs.remove(proc)

    def close(self) -> None:
        self._closed.set()
        # Shutting the listener down wakes the thread waiting on it.
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._listener.close()
        self._acceptor.join(timeout=5)
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            with contextlib.suppress(OSError):
                proc.kill()


class RunningSidecar:
    """One rollout's sidecar: its container, the way to its servers and the
    MCP client for them."""

    def __init__(self, spec: Sidecar, *, max_lifetime: int = 3600, runtime: str = "docker") -> None:
        self.spec = spec
        self.container = Container(spec.container, max_lifetime=max_lifetime, runtime=runtime)
        self.name = self.container.name
        self.forwards: dict[str, PortForward] = {}
        # Server name to the address the session uses for it.
        self.urls: dict[str, str] = {}
        self.mcp_client: Any = None

    def start(self) -> None:
        """Bring the sidecar up.  Raises :class:`SidecarError` (after removing
        nothing: the caller closes) when any step fails."""
        spec = self.spec
        try:
            self.container.start()
        except Exception as error:
            raise SidecarError(f"the sidecar's container did not start: {error}") from error
        if spec.files:
            put = self.container.exec(
                ["tar", "-x", "-C", "/", "-f", "-"],
                stdin_data=build_archive(spec.files),
                timeout=300,
                user="0:0",
            )
            if put.returncode != 0:
                raise SidecarError(
                    f"files could not be put into the sidecar: {put.stderr_text[:300]}"
                )
        if spec.setup:
            # Output to a file: what the command leaves running keeps writing,
            # and must not hold this call open or lose its output's reader.
            done = self.container.exec(
                [
                    "sh",
                    "-c",
                    'exec "$@" > /tmp/.sidecar-setup.log 2>&1 < /dev/null',
                    "sh",
                    *spec.setup,
                ],
                timeout=spec.setup_timeout,
            )
            if done.returncode != 0:
                tail = self.container.exec(
                    ["tail", "-c", "2000", "/tmp/.sidecar-setup.log"], timeout=30
                )
                why = "timed out" if done.timed_out else f"exit code {done.returncode}"
                said = tail.stdout.decode("utf-8", errors="replace").strip()
                raise SidecarError(f"the sidecar's setup command failed ({why}): {said[-500:]}")
        self._wait_for_ports()
        self._connect()

    def _wait_for_ports(self) -> None:
        ports = sorted(
            {*self.spec.wait_ports, *(self._port(u) for u in self.spec.mcp_servers.values())}
        )
        if not ports:
            return
        deadline = time.monotonic() + self.spec.wait_timeout
        while True:
            done = self.container.exec(
                ["sh", "-c", _LISTENING, "sh", *map(str, ports)], timeout=30, user="0:0"
            )
            if done.returncode == 0:
                return
            if time.monotonic() > deadline:
                raise SidecarError(
                    f"the sidecar's ports {ports} were not all listening after "
                    f"{self.spec.wait_timeout} s"
                )
            time.sleep(0.5)

    @staticmethod
    def _port(url: str) -> int:
        parts = urlsplit(url)
        if parts.hostname not in ("127.0.0.1", "localhost") or parts.port is None:
            raise SidecarError(f"a sidecar server's address must be a port on its own host: {url}")
        return parts.port

    def _relay(self) -> list[str]:
        for program, argv in _RELAYS.items():
            found = self.container.exec(["sh", "-c", 'command -v "$1"', "sh", program], timeout=30)
            if found.returncode == 0 and found.stdout.strip():
                return argv
        raise SidecarError(
            f"the sidecar's image has none of {', '.join(_RELAYS)} to reach its servers with"
        )

    def _connect(self) -> None:
        if not self.spec.mcp_servers:
            return
        from turnstone.core.mcp_client import MCPClientManager

        relay = self._relay()
        configs: dict[str, dict[str, Any]] = {}
        for name, url in self.spec.mcp_servers.items():
            # Known to this process alone: the forward carries only a
            # connection whose first request names it.
            secret = secrets.token_urlsafe(32)
            forward = PortForward(self.container, self._port(url), relay, secret)
            self.forwards[name] = forward
            parts = urlsplit(url)
            local = urlunsplit(parts._replace(netloc=f"127.0.0.1:{forward.local_port}"))
            self.urls[name] = local
            # One request to a connection, which the client has to know: the
            # forward looks at a connection's first request only, and takes
            # the secret out of that one.
            headers = {FORWARD_HEADER: secret, "Connection": "close"}
            configs[name] = {"type": "http", "url": local, "headers": headers}
        # Built from this rollout's servers only: no configuration is read.
        self.mcp_client = MCPClientManager(configs)
        self.mcp_client.start()
        connected = self.mcp_client.server_count
        if connected != len(configs):
            errors = dict(getattr(self.mcp_client, "_last_error", {}) or {})
            raise SidecarError(
                f"{len(configs) - connected} of the sidecar's {len(configs)} MCP servers "
                f"could not be connected: {errors or 'no reason given'}"
            )

    def tool_names(self) -> set[str]:
        if self.mcp_client is None:
            return set()
        return {t["function"]["name"] for t in self.mcp_client.get_tools()}

    def lost(self) -> str | None:
        """Why the sidecar is not there any more, ``None`` while it runs."""
        if self.container.started and not self.container.removed:
            state = self.container.state()
            # ``None`` is a question the runtime did not answer, which is not
            # "stopped".
            if state["running"] is False:
                return f"the sidecar stopped during the run (exit code {state['exit_code']})"
        return None

    def keep_for(self, seconds: float) -> None:
        """Have the sidecar's container removed *seconds* from now if nobody
        removes it before (:meth:`Container.keep_for`)."""
        self.container.keep_for(seconds)

    def stop(self) -> bool:
        """End the sidecar's processes and keep the container, to be read from."""
        self._disconnect()
        return self.container.stop()

    def _disconnect(self) -> None:
        client, self.mcp_client = self.mcp_client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.shutdown()
        for forward in self.forwards.values():
            forward.close()
        self.forwards = {}

    def close(self) -> bool:
        """Shut the client down and remove the container; ``True`` once its
        absence is confirmed.  Never raises."""
        self._disconnect()
        return self.container.close()

    def describe(self) -> dict[str, Any]:
        return {**self.container.describe(), "mcp_servers": dict(self.spec.mcp_servers)}
