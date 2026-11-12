"""A task's sidecar: a second container whose servers the model reaches as MCP tools.

Needs a container runtime and the test image, like
``tests/test_rollout_container.py``.  The sidecar here runs a small MCP server
written with the standard library only, so the test image needs nothing added.
"""

from __future__ import annotations

import json
import socket
import stat
import threading
import time
from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from tests.test_rollout_container import (
    IMAGE,
    SMALL,
    _listed,
    _runtime,
    needs_container,
    note_containers,
)
from turnstone.core import config as config_module
from turnstone.core.tools import ENVIRONMENT_TOOL_NAMES
from turnstone.rollout.container import Container, ContainerSpec
from turnstone.rollout.episode import ContainerSandbox, RolloutConfig, Task, run_episode
from turnstone.rollout.sidecar import (
    FORWARD_HEADER,
    PortForward,
    RunningSidecar,
    Sidecar,
    SidecarError,
)
from turnstone.rollout.verification import VerificationContext, export_paths

NL = chr(10)
PORT = 39101

# A ledger served over MCP: JSON answers to POST /mcp, nothing else.
SERVER = """
import json, socket, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {
        "name": "post_entry",
        "description": "Post an amount to the ledger.",
        "inputSchema": {
            "type": "object",
            "properties": {"amount": {"type": "integer"}, "memo": {"type": "string"}},
            "required": ["amount"],
        },
    },
    {
        "name": "where",
        "description": "Name the host the ledger runs on and its network interfaces.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call(name, arguments):
    if name == "where":
        with open("/proc/net/dev") as f:
            names = sorted(line.split(":")[0].strip() for line in f.readlines()[2:])
        return socket.gethostname() + " " + ",".join(names)
    amount = arguments.get("amount")
    if not isinstance(amount, int):
        raise ValueError("amount must be a whole number")
    with open("/state/ledger.txt", "a") as f:
        f.write(str(amount) + chr(10))
    return "posted " + str(amount)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self, status, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._answer(405)

    def do_DELETE(self):
        self._answer(200)

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            return self._answer(202)
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ledger", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                text = call(message["params"]["name"], message["params"].get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as error:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        elif method == "ping":
            result = {}
        else:
            error = {"code": -32601, "message": "method not found"}
            return self._answer(200, {"jsonrpc": "2.0", "id": ident, "error": error})
        self._answer(200, {"jsonrpc": "2.0", "id": ident, "result": result})


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""
# Starts the server and returns, as a task's setup command must.
START = f"mkdir -p /state && (setsid python3 /srv/ledger.py {PORT} > /state/server.log 2>&1 &)"


@pytest.fixture(autouse=True)
def _no_configuration(monkeypatch):
    """No configuration file, and no MCP client built from one: a rollout's client
    is made of its sidecar's servers and nothing else."""
    from turnstone.core import mcp_client

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a rollout read the machine's MCP configuration")

    monkeypatch.setattr(config_module, "_cache", {})
    monkeypatch.setattr(mcp_client, "create_mcp_client", refuse)


def _sidecar(**changes: Any) -> Sidecar:
    settings: dict[str, Any] = {
        "container": ContainerSpec(
            image=IMAGE,
            user="0:0",
            read_only_root=False,
            writable={},
            workdir="/srv",
            memory="512m",
        ),
        "files": {"/srv/ledger.py": SERVER.encode()},
        "setup": ["sh", "-c", START],
        "wait_timeout": 30,
        "mcp_servers": {"ledger": f"http://127.0.0.1:{PORT}/mcp"},
    }
    settings.update(changes)
    return Sidecar(**settings)


def _task(**changes: Any) -> Task:
    settings: dict[str, Any] = {
        "prompt": "post it",
        "container": ContainerSpec(image=IMAGE, memory="512m"),
        "files": {"reach.py": REACH_SCRIPT.encode()},
        "sidecar": _sidecar(),
    }
    settings.update(changes)
    return Task(**settings)


@pytest.fixture(name="containers_made")
def _made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return note_containers(monkeypatch)


def _gone(record: dict[str, Any], made: list[str]) -> None:
    """Both containers the harness made for this test are gone, and it made no
    network for them.  Only its own are looked for: other rollouts may be
    running on the same machine."""
    assert record["sandbox"]["removed"] is True
    assert {record["sandbox"]["name"], record["sandbox"]["sidecar"]["name"]} == set(made)
    assert [name for name in made if _listed(name)] == []
    for name in made:
        listed = _runtime("network", "ls", "--quiet", "--filter", f"name={name}")
        assert listed.stdout.strip() == ""


# The same ledger, where a posting is answered only once two are under way at
# the same time; each of the two gets a number of its own.
MEETING_SERVER = SERVER.replace(
    "TOOLS = [", "import threading" + NL + "MEETING = threading.Barrier(2)" + NL + "TOOLS = [", 1
).replace(
    'return "posted " + str(amount)',
    'return "posted " + str(amount) + " as number " + str(MEETING.wait(timeout=20))',
)

# The same ledger as a server that keeps its connections open, as most do,
# and notes the headers of every request it is sent.
KEEPING_SERVER = SERVER.replace(
    "    def log_message(self, *args):",
    NL.join(
        [
            '    protocol_version = "HTTP/1.1"',
            "",
            "    def parse_request(self):",
            "        parsed = super().parse_request()",
            "        if parsed:",
            "            sent = sorted(k.lower() + '=' + v for k, v in self.headers.items())",
            '            with open("/state/headers.txt", "a") as f:',
            '                f.write(" ".join(sent) + chr(10))',
            "        return parsed",
            "",
            "    def log_message(self, *args):",
        ]
    ),
    1,
)

POST = {"name": "mcp__ledger__post_entry", "arguments": {"amount": 3}}
WHERE = {"name": "mcp__ledger__where", "arguments": {}}
# From the model's own container: is anything listening where the ledger is?
REACH_SCRIPT = (
    "import socket, sys"
    + NL
    + "s = socket.socket()"
    + NL
    + "s.settimeout(2)"
    + NL
    + "print(s.connect_ex((sys.argv[1], int(sys.argv[2]))))"
    + NL
)
REACH = f"python3 reach.py 127.0.0.1 {PORT}; cat /etc/hostname"


@needs_container
class TestARolloutWithASidecar:
    def test_the_models_tool_call_reaches_the_sidecar_and_is_in_the_log(self, containers_made):
        script = [
            {"tool_calls": [POST, WHERE]},
            {"tool_calls": [{"name": "bash", "arguments": {"command": REACH}}]},
            {"content": "posted"},
        ]
        seen: dict[str, Any] = {}

        def read_the_ledger(context: VerificationContext) -> dict[str, Any]:
            seen["sidecar"] = context.sidecar_name
            ledger = export_paths(context.sidecar_name or "", ["/state/ledger.txt"])
            return {"verdict": "pass", "ledger": ledger.files()["/state/ledger.txt"].decode()}

        with ScriptedChatServer(script) as server:
            record = run_episode(
                _task(),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                verifier=read_the_ledger,
            )
            first, second, third = server.requests

        assert record["finish_state"] == "completed", record["error"]
        log = record["tool_log"]
        assert [(e["tool"], e["ok"]) for e in log] == [
            ("mcp__ledger__post_entry", True),
            ("mcp__ledger__where", True),
            ("bash", True),
        ]
        assert log[0]["args"] == {"amount": 3} and log[0]["result"] == "posted 3"
        # The ledger runs in the sidecar, which has a loopback interface and no other...
        sidecar_host, interfaces = log[1]["result"].split()
        assert interfaces == "lo"
        assert sidecar_host == record["sandbox"]["sidecar"]["container_id"][:12]
        # ...and the model's own container is another place, from which nothing
        # listens at the ledger's address.
        refused, own_host = log[2]["result"].split()
        assert refused != "0" and own_host == record["sandbox"]["container_id"][:12]
        assert own_host != sidecar_host

        # The tools offered: the sandbox's, the sub-agent tool and this sidecar's;
        # the same list with every request, so the prompt still extends.
        offered = [t["function"]["name"] for t in first["tools"]]
        assert set(offered) == ENVIRONMENT_TOOL_NAMES | {
            "task_agent",
            "mcp__ledger__post_entry",
            "mcp__ledger__where",
        }
        assert first["tools"] == second["tools"] == third["tools"]
        assert record["prefix_ok"] and record["trainable"] and record["one_sequence"]

        # What the model's call did is in the sidecar's state, read once it is stopped.
        assert record["verification"]["result"] == {"verdict": "pass", "ledger": "3" + NL}
        assert seen["sidecar"] == record["sandbox"]["sidecar"]["name"]
        _gone(record, containers_made)

    def test_the_sidecars_server_is_never_sent_the_forwards_secret(self, containers_made):
        """The session's client names the secret in every request, and would
        send later requests over a connection the server keeps open; only the
        first request of a connection is looked at by the forward.  So every
        request goes on without the header and as the only one of its
        connection."""
        assert "HTTP/1.1" in KEEPING_SERVER and "headers.txt" in KEEPING_SERVER

        def read_what_it_was_sent(context: VerificationContext) -> dict[str, Any]:
            files = export_paths(
                context.sidecar_name or "", ["/state/headers.txt", "/state/ledger.txt"]
            ).files()
            return {
                "verdict": "pass",
                "headers": files["/state/headers.txt"].decode(),
                "ledger": files["/state/ledger.txt"].decode(),
            }

        script = [{"tool_calls": [POST]}, {"tool_calls": [POST]}, {"content": "posted twice"}]
        with ScriptedChatServer(script) as server:
            record = run_episode(
                _task(sidecar=_sidecar(files={"/srv/ledger.py": KEEPING_SERVER.encode()})),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                verifier=read_what_it_was_sent,
            )
        assert record["finish_state"] == "completed", record["error"]
        assert [(e["tool"], e["ok"]) for e in record["tool_log"]] == [
            ("mcp__ledger__post_entry", True),
            ("mcp__ledger__post_entry", True),
        ]
        seen = record["verification"]["result"]
        assert seen["ledger"] == "3" + NL + "3" + NL
        requests = seen["headers"].splitlines()
        # Setting the client up and two calls: several requests, each without
        # the header and each told that its connection ends with it.
        assert len(requests) >= 4
        assert all(FORWARD_HEADER not in line for line in requests)
        assert all("connection=close" in line.split() for line in requests)
        assert record["trainable"] is True
        _gone(record, containers_made)

    DELEGATING: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "task_agent", "arguments": {"prompt": "post 5"}}]},
        {"tool_calls": [POST]},
        {"content": "posted"},
    ]
    POSTING_AGENT: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "mcp__ledger__post_entry", "arguments": {"amount": 5}}]},
        {"content": "posted 5"},
    ]

    @staticmethod
    def _read_the_ledger(context: VerificationContext) -> dict[str, Any]:
        ledger = export_paths(context.sidecar_name or "", ["/state/ledger.txt"])
        return {"verdict": "pass", "ledger": ledger.files()["/state/ledger.txt"].decode()}

    def test_a_sub_agent_is_offered_the_sidecars_tools_and_its_calls_are_in_the_record(
        self, containers_made
    ):
        """A task's systems can be reached only as these tools, so a sub-agent is
        offered them as the session is.  What it does with them acts on the task's
        state, is in the record apart from the session's own calls, and the check
        after the run sees the state it left."""
        with ScriptedChatServer(self.DELEGATING, agent_script=self.POSTING_AGENT) as server:
            record = run_episode(
                _task(),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                verifier=self._read_the_ledger,
            )
            first_agent, _second_agent = server.agent_requests
            first_parent = server.parent_requests[0]

        assert record["finish_state"] == "completed", record["error"]
        # The session and its sub-agent are both offered the sidecar's tools.
        sidecars = {"mcp__ledger__post_entry", "mcp__ledger__where"}
        assert sidecars <= {t["function"]["name"] for t in first_parent["tools"]}
        assert {t["function"]["name"] for t in first_agent["tools"]} == (
            ENVIRONMENT_TOOL_NAMES | sidecars
        )
        assert record["subagents"]["sidecar_tools"] == "offered"
        # The sub-agent's call reached the server, before the session's own.
        assert record["verification"]["result"]["ledger"] == "5" + NL + "3" + NL
        # The session's log holds its own calls; the sub-agent's is in the side record.
        log = record["tool_log"]
        assert [e["tool"] for e in log] == ["task_agent", "mcp__ledger__post_entry"]
        assert log[0]["agent"] == 0 and log[1]["args"] == {"amount": 3}
        (call,) = record["subagents"]["tool_log"]
        assert (call["agent"], call["turn"], call["tool"]) == (0, 0, "mcp__ledger__post_entry")
        assert call["args"] == {"amount": 5} and call["ok"] is True
        assert call["result"] == "posted 5" and "rule" not in call
        assert record["subagents"]["not_for_training"] is True
        assert record["trainable"] is True and record["one_sequence"] is True
        _gone(record, containers_made)

    def test_a_run_can_withhold_the_sidecars_tools_from_sub_agents(self, containers_made):
        with ScriptedChatServer(self.DELEGATING, agent_script=self.POSTING_AGENT) as server:
            record = run_episode(
                _task(),
                RolloutConfig(
                    base_url=server.base_url,
                    model="scripted",
                    subagent_sidecar_tools=False,
                    **SMALL,
                ),
                verifier=self._read_the_ledger,
            )
            first_agent, second_agent = server.agent_requests
            first_parent = server.parent_requests[0]

        assert record["finish_state"] == "completed", record["error"]
        # The session is offered the sidecar's tools, its sub-agent is not...
        assert "mcp__ledger__post_entry" in {t["function"]["name"] for t in first_parent["tools"]}
        assert {t["function"]["name"] for t in first_agent["tools"]} == ENVIRONMENT_TOOL_NAMES
        assert record["subagents"]["sidecar_tools"] == "withheld"
        # ...and a call it makes anyway is refused before it reaches the server.
        (told,) = [m["content"] for m in second_agent["messages"] if m["role"] == "tool"]
        assert "mcp__ledger__post_entry" in told and "not available" in told
        # Only the session's own call is in the ledger; the attempt is in the record.
        assert record["verification"]["result"]["ledger"] == "3" + NL
        assert [e["tool"] for e in record["tool_log"]] == ["task_agent", "mcp__ledger__post_entry"]
        (attempt,) = record["subagents"]["tool_log"]
        assert attempt["tool"] == "mcp__ledger__post_entry" and attempt["ok"] is False
        assert record["trainable"] is True
        _gone(record, containers_made)

    def test_two_sub_agents_call_the_sidecar_at_the_same_time(self, containers_made):
        """One MCP client serves the session and every sub-agent.  The ledger here
        answers a posting only once two are under way at once, and gives each a
        number of its own: a client that took them one after the other would leave
        the first waiting until it failed."""
        parent = [
            {
                "tool_calls": [
                    {"name": "task_agent", "arguments": {"prompt": "post 5"}},
                    {"name": "task_agent", "arguments": {"prompt": "post 5 as well"}},
                ]
            },
            {"content": "both posted"},
        ]
        assert MEETING_SERVER.count("MEETING") == 2
        with ScriptedChatServer(parent, agent_script=self.POSTING_AGENT) as server:
            record = run_episode(
                _task(sidecar=_sidecar(files={"/srv/ledger.py": MEETING_SERVER.encode()})),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                verifier=self._read_the_ledger,
            )

        assert record["finish_state"] == "completed", record["error"]
        calls = record["subagents"]["tool_log"]
        assert sorted(c["agent"] for c in calls) == [0, 1] and all(c["ok"] for c in calls)
        assert sorted(c["result"] for c in calls) == [
            "posted 5 as number 0",
            "posted 5 as number 1",
        ]
        assert record["verification"]["result"]["ledger"] == "5" + NL + "5" + NL
        assert record["subagents"]["started"] == 2 and record["subagents"]["failures"] == []
        assert record["trainable"] is True and record["one_sequence"] is True
        _gone(record, containers_made)

    def test_a_call_the_model_gets_wrong_is_an_ordinary_result(self, containers_made):
        wrong = {"name": "mcp__ledger__post_entry", "arguments": {"amount": "three"}}
        script = [{"tool_calls": [wrong]}, {"content": "it did not take it"}]
        with ScriptedChatServer(script) as server:
            record = run_episode(
                _task(), RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            )
        (entry,) = record["tool_log"]
        assert entry["ok"] is False and "amount" in entry["result"]
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["turns"] == 2
        _gone(record, containers_made)

    def test_a_sidecar_that_does_not_come_up_is_the_machinerys_failure(self, containers_made):
        with ScriptedChatServer([{"content": "never asked"}]) as server:
            record = run_episode(
                _task(sidecar=_sidecar(setup=["sh", "-c", "echo no ledger today >&2; exit 3"])),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
            )
            assert server.requests == []
        assert record["finish_state"] == "sidecar_failed" and record["finish_cause"] == "machinery"
        assert "no ledger today" in record["error"] and "exit code 3" in record["error"]
        assert record["trainable"] is False
        _gone(record, containers_made)

    def test_a_sidecar_that_stops_during_the_run_is_the_machinerys_failure(self, containers_made):
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "sleep 6"}}]},
            {"tool_calls": [POST]},
            {"content": "done"},
        ]
        made: list[ContainerSandbox] = []

        def factory(task: Task, config: RolloutConfig) -> ContainerSandbox:
            made.append(ContainerSandbox(task, config))
            return made[-1]

        def stop_the_sidecar() -> None:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                side = made[0].sidecar if made else None
                if side is not None and side.mcp_client is not None:
                    break
                time.sleep(0.2)
            time.sleep(1.0)
            _runtime("kill", made[0].sidecar.name)  # type: ignore[union-attr]

        with ScriptedChatServer(script) as server:
            stopper = threading.Thread(target=stop_the_sidecar, daemon=True)
            stopper.start()
            record = run_episode(
                _task(),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                sandbox_factory=factory,
            )
            stopper.join(timeout=30)
        assert record["finish_state"] == "sidecar_lost" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False
        (problem,) = record["problems"]
        assert problem.startswith("the task's sidecar stopped during the run")
        _gone(record, containers_made)


@needs_container
class TestBringingASidecarUp:
    def test_a_servers_address_must_be_on_the_sidecars_own_host(self):
        side = RunningSidecar(_sidecar(mcp_servers={"x": "http://192.0.2.5:80/mcp"}))
        try:
            with pytest.raises(SidecarError, match="a port on its own host"):
                side.start()
        finally:
            assert side.close() is True

    def test_ports_that_never_listen_are_reported(self):
        side = RunningSidecar(_sidecar(setup=None, wait_timeout=2))
        try:
            with pytest.raises(SidecarError, match="were not all listening after 2 s"):
                side.start()
        finally:
            assert side.close() is True
        assert _listed(side.name) == []


CRLF = bytes([13, 10])
SECRET = "the-secret-of-this-run"


def _request(*headers: bytes, body: bytes = b"") -> bytes:
    return CRLF.join([b"POST /mcp HTTP/1.1", b"Host: ledger", *headers, b"", body])


class TestTheForwardCarriesOnlyTheSessionsOwnClient:
    """The forward listens on this machine's loopback interface, where every
    user of the machine can connect.  It carries a connection only when its
    first request names the secret the rollout made for it.

    No container is needed: the stand-in for the runtime gives back what it is
    sent, and notes that it was started."""

    @staticmethod
    def _forward(tmp_path: Any) -> PortForward:
        runtime = tmp_path / "runtime"
        started = tmp_path / "started"
        runtime.write_text("#!/bin/sh" + NL + f'echo started >> "{started}"' + NL + "exec cat" + NL)
        runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR)
        container = Container(ContainerSpec(image=IMAGE), runtime=str(runtime))
        return PortForward(container, PORT, ["relay"], SECRET)

    @pytest.fixture
    def forward(self, tmp_path):
        made = self._forward(tmp_path)
        yield made
        made.close()

    def _exchange(self, forward: PortForward, sent: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", forward.local_port), timeout=10) as client:
            if sent:
                client.sendall(sent)
            client.shutdown(socket.SHUT_WR)
            received = b""
            while data := client.recv(65536):
                received += data
        return received

    def test_a_request_that_names_the_secret_is_carried(self, forward, tmp_path):
        """Without the header that names the secret, and as the only request
        of its connection; the rest as it was sent."""
        carried = _request(b"Connection: close", body=b"the body")
        request = _request(b"X-Turnstone-Forward: " + SECRET.encode(), body=b"the body")
        assert self._exchange(forward, request) == carried
        assert (tmp_path / "started").read_text() == "started" + NL
        # The header's name in any case, with or without space around its value.
        again = _request(FORWARD_HEADER.upper().encode() + b":" + SECRET.encode() + b"  ")
        assert self._exchange(forward, again) == _request(b"Connection: close")

    def test_the_tasks_server_is_not_shown_the_secret(self, forward):
        """The secret is between the session's client and the forward.  Only a
        connection's first request is looked at, and the client names the
        secret in each, so the request goes on as the only one of its
        connection: what the server is told about keeping the connection is
        replaced."""
        request = _request(
            b"Accept: application/json",
            b"X-Turnstone-Forward: " + SECRET.encode(),
            b"connection: keep-alive",
            b"Keep-Alive: timeout=5",
            b"Content-Length: 8",
            b"x-turnstone-forward: " + SECRET.encode(),
            body=b"the body",
        )
        carried = self._exchange(forward, request)
        assert carried == _request(
            b"Accept: application/json",
            b"Content-Length: 8",
            b"Connection: close",
            body=b"the body",
        )
        assert SECRET.encode() not in carried and b"forward" not in carried.lower()

    def test_a_head_that_trickles_in_has_one_time_limit_for_all_of_it(
        self, forward, tmp_path, monkeypatch
    ):
        """A byte now and then kept such a connection open for as long as it
        liked: the time limit was each wait's, not the head's."""
        import turnstone.rollout.sidecar as sidecar

        monkeypatch.setattr(sidecar, "_REQUEST_HEAD_SECONDS", 1.0)
        started = time.monotonic()
        closed_after = None
        with socket.create_connection(("127.0.0.1", forward.local_port), timeout=10) as client:
            client.settimeout(0.2)
            while closed_after is None and time.monotonic() - started < 6:
                try:
                    client.sendall(b"x")
                    if client.recv(1) == b"":
                        closed_after = time.monotonic() - started
                except TimeoutError:
                    continue
                except OSError:
                    closed_after = time.monotonic() - started
        assert closed_after is not None and closed_after < 4
        assert not (tmp_path / "started").exists()

    def test_only_so_many_connections_are_read_for_their_head_at_a_time(
        self, tmp_path, monkeypatch
    ):
        """Connections that send nothing hold a bounded number of the runner's
        threads and sockets.  With every place taken a further connection
        waits its turn, and is carried once a place is free."""
        import turnstone.rollout.sidecar as sidecar

        monkeypatch.setattr(sidecar, "_WAITING_HEADS", 2)
        monkeypatch.setattr(sidecar, "_REQUEST_HEAD_SECONDS", 1.5)
        forward = self._forward(tmp_path)
        silent = [
            socket.create_connection(("127.0.0.1", forward.local_port), timeout=10)
            for _ in range(2)
        ]
        try:
            time.sleep(0.3)  # both are being read for a head that never comes
            request = _request(b"X-Turnstone-Forward: " + SECRET.encode(), body=b"waited")
            started = time.monotonic()
            carried = self._exchange(forward, request)
            waited = time.monotonic() - started
            assert carried == _request(b"Connection: close", body=b"waited")
            # Not before one of the two had run out of its time.
            assert 1.0 <= waited < 8
            for client in silent:
                assert client.recv(1) == b""
        finally:
            for client in silent:
                client.close()
            forward.close()
        # Once places are free again nobody waits.
        again = self._forward(tmp_path)
        try:
            started = time.monotonic()
            assert self._exchange(again, request) == carried
            assert time.monotonic() - started < 1.0
        finally:
            again.close()

    @pytest.mark.parametrize(
        "request_bytes",
        [
            _request(),
            _request(b"X-Turnstone-Forward: another-secret"),
            _request(b"X-Turnstone-Forward: " + SECRET.encode()[:-1]),
            _request(b"X-Turnstone-Forward: " + SECRET.encode() + b"x"),
            _request(b"X-Other: " + SECRET.encode()),
            # Named in the body, after the header lines have ended.
            _request(body=b"X-Turnstone-Forward: " + SECRET.encode() + CRLF + CRLF),
            # Sent before the request, where no header line is.
            b"X-Turnstone-Forward: " + SECRET.encode() + CRLF + CRLF,
            # More than a request's head, without its end.
            b"GET / HTTP/1.1" + CRLF + b"X-Padding: " + b"x" * 70_000,
            b"not a request at all",
            b"",
        ],
        ids=[
            "no-header",
            "another-secret",
            "the-secret-cut-short",
            "the-secret-and-more",
            "under-another-name",
            "in-the-body",
            "before-the-request",
            "a-head-without-end",
            "no-request",
            "nothing",
        ],
    )
    def test_any_other_connection_is_closed_and_nothing_is_started_for_it(
        self, forward, tmp_path, request_bytes
    ):
        try:
            answer = self._exchange(forward, request_bytes)
        except OSError:
            answer = b""  # closed before everything was sent
        assert answer == b""
        assert not (tmp_path / "started").exists()

    def test_a_connection_that_sends_nothing_is_not_waited_for_long(self, forward, monkeypatch):
        import turnstone.rollout.sidecar as sidecar

        monkeypatch.setattr(sidecar, "_REQUEST_HEAD_SECONDS", 0.3)
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", forward.local_port), timeout=10) as client:
            assert client.recv(1) == b""
        assert time.monotonic() - started < 5

    def test_the_sessions_client_is_given_the_secret_and_the_record_is_not(self, monkeypatch):
        import turnstone.core.mcp_client as mcp_client

        seen: dict[str, Any] = {}

        class Client:
            server_count = 1

            def __init__(self, configs: dict[str, Any]) -> None:
                seen.update(configs)

            def start(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        monkeypatch.setattr(mcp_client, "MCPClientManager", Client)
        running = RunningSidecar(
            Sidecar(
                container=ContainerSpec(image=IMAGE),
                mcp_servers={"ledger": f"http://127.0.0.1:{PORT}/mcp"},
            )
        )
        monkeypatch.setattr(running, "_relay", lambda: ["relay"])
        try:
            running._connect()
            config = seen["ledger"]
            secret = config["headers"][FORWARD_HEADER]
            assert len(secret) >= 32 and config["url"] == running.urls["ledger"]
            # One request to a connection: the forward looks at the first only.
            assert config["headers"] == {FORWARD_HEADER: secret, "Connection": "close"}
            assert secret not in json.dumps(running.describe())
            assert secret not in running.urls["ledger"]
        finally:
            running._disconnect()
