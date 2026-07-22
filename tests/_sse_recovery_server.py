"""Boot the REAL interactive Turnstone server for the SSE recovery e2e
harness: real ``SessionManager`` + real ``ChatSession`` engine driven
through a scripted chat-completions client at the SDK boundary, executing
REAL bash tools, exposed over a real uvicorn socket.

The recipe (verified end-to-end) has four load-bearing pieces:

1. **Provider injection seam.** ``create_app`` takes a PRE-BUILT
   ``SessionManager``, so the harness owns the ``session_factory``: it
   passes ``client=fake_client`` and OMITS the registry, so
   ``ChatSession`` falls back to ``create_provider("openai-compatible")``
   == ``OpenAIChatCompletionsProvider`` — exactly what
   ``tests._session_helpers.scripted_chat_client`` targets. No production
   monkeypatch of the engine.

2. **Auto-title suppression.** The first user message spawns a background
   ``_generate_title`` LLM call that would consume the first scripted
   response (the tool call) and desync a positional script. Setting
   ``session._title_generated = True`` before the first send disables it.

3. **Completion barrier.** ``/send`` returns immediately after spawning
   ``ws.worker_thread``; joining that thread is the true "turn complete,
   every SSE event enqueued" barrier (``stream_end`` is per-LLM-call, not
   per-turn, so it is NOT a completion marker).

4. **Thread hygiene.** ``create_app``'s lifespan unconditionally starts
   two daemon fan-out threads (``_global_fanout_thread`` blocking on
   ``global_queue.get()``, ``_aggregate_emitter_thread`` on a 10s loop)
   with no shutdown sentinel — they would trip conftest's leaked-thread
   guard. They serve the cluster/global lane, which the per-ws ``/events``
   path under test never touches, so the harness swaps them for no-ops
   before boot (restored on ``stop``). The result is a fully clean
   teardown — no ``allow_thread_leak`` needed — with the real per-ws SSE
   engine fully intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue as _q
import socket
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn

import turnstone.server as tsrv
from tests._session_helpers import scripted_chat_client
from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
from turnstone.core.auth import JWT_AUD_SERVER, create_jwt
from turnstone.core.session import ChatSession
from turnstone.core.session_manager import SessionManager
from turnstone.core.session_ui_base import SessionUIBase
from turnstone.core.storage import get_storage
from turnstone.core.workstream import WorkstreamKind
from turnstone.prompts import ClientType
from turnstone.server import WebUI, create_app

if TYPE_CHECKING:
    from turnstone.core.workstream import Workstream

_JWT_SECRET = "sse-recovery-e2e-jwt-secret-minimum-32-chars!"
# Small server send buffer so a stalled consumer's in-flight backlog before
# the listener-queue poison stays bounded (paired with the client's small
# SO_RCVBUF in _sse_recovery_helpers). Harmless for prompt readers.
_DEFAULT_SNDBUF = 8192


def _noop_thread(*_args: object, **_kwargs: object) -> None:
    """Stand-in for the cluster-lane daemon threads (see module docstring)."""


# The REAL daemon-thread factories, captured once at import so restore always
# targets them regardless of how many servers neuter/restore in a run (the
# restart scenarios build a second server before the run ends).
_REAL_FANOUT = tsrv._global_fanout_thread
_REAL_AGGREGATE = tsrv._aggregate_emitter_thread


def _fake_client(scripts: tuple[Any, ...]) -> Any:
    """An SDK-shaped fake whose ``chat.completions.create`` follows a
    positional script (each a :func:`fake_chat_stream` kwargs dict)."""
    create_fn = scripted_chat_client(*scripts)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_fn)))
    client.calls = create_fn.calls
    return client


class RecoveryServer:
    """A booted interactive node the recovery scenarios drive."""

    def __init__(
        self,
        *,
        sndbuf: int = _DEFAULT_SNDBUF,
        listener_cap: int | None = None,
        extra_routes: list[Any] | None = None,
        port: int = 0,
    ) -> None:
        self._global_queue: _q.Queue[dict[str, Any]] = _q.Queue(maxsize=100000)
        self._global_listeners: list[_q.Queue[dict[str, Any]]] = []
        self._global_listeners_lock = threading.Lock()
        # Per-ws scripted client, resolved at factory-call time.
        self._pending_client: Any = _fake_client((dict(content="ok", finish_reason="stop"),))
        self._clients: dict[str, Any] = {}

        WebUI._global_queue = self._global_queue

        def session_factory(
            ui: Any,
            model_alias: str | None = None,
            ws_id: str | None = None,
            *,
            skill: Any = None,
            client_type: str = "",
            kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
            parent_ws_id: str | None = None,
            project_id: str = "",
            **_extra: Any,
        ) -> ChatSession:
            client = self._pending_client
            if ws_id is not None:
                self._clients[ws_id] = client
            return ChatSession(
                client=client,
                model="test-model",
                ui=ui,
                instructions=None,
                temperature=None,
                max_tokens=1024,
                tool_timeout=30,
                ws_id=ws_id,
                user_id="recovery-user",
                client_type=ClientType.WEB,
                kind=kind,
                # Don't truncate large tool outputs: the harness tests
                # recovery, not the tool-result truncation budget, and a
                # truncated /history would diverge from the full live event
                # and defeat the convergence assertions.
                tool_truncation=10_000_000,
            )

        self._adapter = InteractiveAdapter(
            global_queue=self._global_queue,
            ui_factory=lambda ws: WebUI(
                ws_id=ws.id, user_id=ws.user_id, kind=ws.kind, parent_ws_id=ws.parent_ws_id
            ),
            session_factory=session_factory,
        )
        self._manager = SessionManager(
            self._adapter, storage=get_storage(), max_active=32, node_id="recovery-node"
        )
        self._adapter.attach(self._manager)
        WebUI._workstream_mgr = self._manager

        # Neuter the cluster-lane daemons for a clean teardown (see docstring).
        tsrv._global_fanout_thread = _noop_thread
        tsrv._aggregate_emitter_thread = _noop_thread

        # Optional small listener-queue cap. The cap is a default arg on the
        # registration methods with no config/env override, so lower it by
        # patching their ``__defaults__`` (restored on stop). fix-3's
        # de-amplification makes a real 500-cap overflow need a pathological
        # storm; a small cap exercises the identical _ListenerOverflow ->
        # stream_overflow -> reconnect-replay path within a bounded storm.
        self._orig_defaults: list[tuple[Any, tuple[Any, ...] | None]] = []
        if listener_cap is not None:
            for meth in (
                SessionUIBase._register_listener,
                SessionUIBase.register_listener_with_in_progress_snapshot,
                SessionUIBase.register_listener_with_replay,
            ):
                self._orig_defaults.append((meth, meth.__defaults__))
                meth.__defaults__ = (listener_cap,)

        self._app = create_app(
            workstreams=self._manager,
            global_queue=self._global_queue,
            global_listeners=self._global_listeners,
            global_listeners_lock=self._global_listeners_lock,
            skip_permissions=True,
            jwt_secret=_JWT_SECRET,
            node_id="recovery-node",
            # /history + tenant checks read app.state.auth_storage.
            auth_storage=get_storage(),
        )
        # Same-origin extras (Tier 2 serves its recovery page here so the real
        # Pane's cookie auth + EventSource work without cross-origin plumbing).
        if extra_routes:
            self._app.router.routes.extend(extra_routes)

        # Pre-bind a listening socket with a small SO_SNDBUF (accepted conns
        # inherit it), then hand it to uvicorn.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        self._sock.bind(("127.0.0.1", port))  # port=0 -> ephemeral; fixed -> restart reuse
        self._port = int(self._sock.getsockname()[1])
        self._sock.listen(128)

        # -- fault injection (public knobs below) ----------------------------
        # In-process arming: the Tier-2 runner holds this RecoveryServer and
        # arms a knob, THEN drives the browser request that consumes it.
        # Single-writer by construction — the runner never arms a knob while
        # the loop thread is mid-consume — and CPython makes each int read /
        # write atomic, so these need no lock even though the uvicorn loop
        # thread increments/decrements them while the runner thread reads.
        self.history_requests = 0
        self.rewind_requests = 0
        # Per-ws SSE connection opens (``GET …/events`` — the EventSource the
        # pane's connectSSE builds).  A TRANSPORT-FREE heal (the #890 idle-edge
        # staleness backstop, a quiesced REST refetch) must leave this FLAT; a
        # reload-based backstop would bump it once per reconnect (the round-5
        # storm).  Same lock-free single-writer int discipline as above.
        self.events_requests = 0
        self._history_fail_remaining = 0
        self._history_delay_ms = 0
        # A thin pure-ASGI fault layer wrapping the REAL app (the production
        # app itself is untouched): count + optionally delay/fail
        # ``GET …/history``, count ``POST …/rewind``, count each per-ws SSE
        # connection open (``GET …/events``), forward everything else (SSE
        # bodies, /send, lifespan, static) verbatim.
        production_app = self._app

        async def _fault_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") == "http":
                path = scope.get("path", "")
                method = scope.get("method", "")
                if path.endswith("/history") and method == "GET":
                    self.history_requests += 1
                    if self._history_delay_ms > 0:
                        await asyncio.sleep(self._history_delay_ms / 1000.0)
                    if self._history_fail_remaining > 0:
                        self._history_fail_remaining -= 1
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 500,
                                "headers": [(b"content-type", b"application/json")],
                            }
                        )
                        await send({"type": "http.response.body", "body": b'{"error": "injected"}'})
                        return
                elif path.endswith("/rewind") and method == "POST":
                    self.rewind_requests += 1
                elif path.endswith("/events") and method == "GET":
                    # Per-ws SSE connection open — count it (readable on
                    # RecoveryServer) and forward the long-lived stream
                    # verbatim below.  Uniquely the per-ws stream: the global
                    # lane is ``…/events/global`` (ends ``/global``), and the
                    # route the pane's EventSource hits is
                    # ``…/workstreams/{ws_id}/events`` (session_routes).
                    self.events_requests += 1
            await production_app(scope, receive, send)

        self._server = uvicorn.Server(
            uvicorn.Config(_fault_app, log_level="warning", lifespan="on")
        )
        self._thread = threading.Thread(
            target=self._serve, name=f"uvicorn-recovery-{self._port}", daemon=True
        )
        self._thread.start()
        if not _tcp_ready(self._port, 10.0):
            self.stop()
            raise AssertionError("recovery server did not accept TCP")

        self._token = create_jwt(
            user_id="recovery-user",
            scopes=frozenset({"read", "write", "approve", "service"}),
            source="recovery",
            secret=_JWT_SECRET,
            audience=JWT_AUD_SERVER,
        )
        self._http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(30.0))

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._server.serve(sockets=[self._sock]))
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    # -- properties ----------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def token(self) -> str:
        return self._token

    @property
    def manager(self) -> SessionManager:
        return self._manager

    # -- workstream lifecycle ------------------------------------------------

    def create_workstream(self, *scripts: Any, name: str = "recovery-ws") -> str:
        """Create a ws whose scripted LLM follows ``scripts`` (positional
        :func:`fake_chat_stream` kwargs). Auto-approves tools and suppresses
        the auto-title call so the positional script stays in sync."""
        self._pending_client = _fake_client(scripts)
        ws = self._manager.create(user_id="recovery-user", name=name)
        self._prime_ws(ws)
        return ws.id

    def open_workstream(self, ws_id: str, *scripts: Any) -> None:
        """Rehydrate a persisted ws on THIS node (the restart path). Fresh
        UI → empty ring + storage-seeded ``_event_id``."""
        if scripts:
            self._pending_client = _fake_client(scripts)
        ws = self._manager.open(ws_id)
        if ws is None:
            raise AssertionError(f"open_workstream: ws {ws_id} not resurrectable")
        self._prime_ws(ws)

    def _prime_ws(self, ws: Workstream) -> None:
        if isinstance(ws.ui, SessionUIBase):
            ws.ui.auto_approve = True  # blanket tool auto-approval
        if ws.session is not None:
            ws.session._title_generated = True  # suppress the auto-title LLM call

    def send(self, ws_id: str, message: str = "go") -> None:
        """POST /send — spawns the worker thread and returns immediately."""
        r = self._http.post(
            f"/v1/api/workstreams/{ws_id}/send",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"message": message},
        )
        r.raise_for_status()

    def wait_turn(self, ws_id: str, *, timeout: float = 45.0) -> None:
        """Block until the turn's worker thread finishes (the true
        turn-complete barrier) and the ws is idle."""
        deadline = time.monotonic() + timeout
        worker: threading.Thread | None = None
        while time.monotonic() < deadline:
            ws = self._manager.get(ws_id)
            worker = ws.worker_thread if ws is not None else None
            if worker is not None:
                break
            time.sleep(0.02)
        if worker is not None:
            worker.join(timeout=max(0.5, deadline - time.monotonic()))
            if worker.is_alive():
                raise AssertionError(f"turn worker for {ws_id} did not finish in {timeout}s")

    def get_ws(self, ws_id: str) -> Workstream | None:
        return self._manager.get(ws_id)

    def ws_state(self, ws_id: str) -> str:
        ws = self._manager.get(ws_id)
        return ws.state.value if ws is not None else ""

    def ring_span(self, ws_id: str) -> tuple[int | None, int]:
        """(earliest retained ring event_id or None, latest counter) — lets a
        scenario wait for the ring to evict a specific cursor."""
        ws = self._manager.get(ws_id)
        ui = ws.ui if ws is not None else None
        if not isinstance(ui, SessionUIBase):
            return None, 0
        buf = ui._event_buffer
        earliest = buf[0][0] if buf else None
        return earliest, ui._event_id

    def listener_poisoned(self, ws_id: str) -> bool:
        """True once any live SSE listener on the ws has poisoned (overflow)."""
        ws = self._manager.get(ws_id)
        ui = ws.ui if ws is not None else None
        if not isinstance(ui, SessionUIBase):
            return False
        return any(getattr(q, "poisoned", False) for q in list(ui._listeners))

    def max_event_id(self, ws_id: str) -> int | None:
        """The storage high-water ``MAX(conversations.event_id)`` — what a
        restarted node's fresh UI seeds ``_event_id`` from."""
        result: int | None = get_storage().get_max_event_id(ws_id)
        return result

    def fetch_history(self, ws_id: str) -> dict[str, Any]:
        r = self._http.get(
            f"/v1/api/workstreams/{ws_id}/history",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        r.raise_for_status()
        result: dict[str, Any] = r.json()
        return result

    # -- fault-injection knobs -----------------------------------------------
    # Armed in-process by the Tier-2 runner (single writer at a time — see
    # __init__).  A plain int is deliberate: CPython makes the loop thread's
    # increment/decrement and the runner thread's read each atomic, and the
    # arm-then-consume ordering means they never race.

    def fail_history(self, count: int) -> None:
        """Make the next ``count`` ``GET …/history`` responses a 500 — the
        failed refetch the #890 guard-before-wipe must survive."""
        self._history_fail_remaining = count

    def delay_history(self, ms: int) -> None:
        """Hold each ``GET …/history`` ``ms`` ms before forwarding (0
        clears).  Opens the clear_ui-refetch quiesce window that the row
        affordance gate (``busy || _replayQueue``) must close."""
        self._history_delay_ms = ms

    @property
    def history_fail_remaining(self) -> int:
        """Unconsumed forced-failure budget — 0 proves the armed failure
        actually fired (assert backend state, never scripted absence)."""
        return self._history_fail_remaining

    # -- teardown ------------------------------------------------------------

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            for ws in list(self._manager.list_all()):
                with contextlib.suppress(Exception):
                    self._manager.close(ws.id)
        self._server.should_exit = True
        self._thread.join(timeout=20)
        with contextlib.suppress(Exception):
            self._http.close()
        with contextlib.suppress(OSError):
            self._sock.close()
        # Restore the cluster-lane daemon factories + any patched cap defaults.
        tsrv._global_fanout_thread = _REAL_FANOUT
        tsrv._aggregate_emitter_thread = _REAL_AGGREGATE
        for meth, defaults in self._orig_defaults:
            meth.__defaults__ = defaults


def _tcp_ready(port: int, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def bash_toolcall_script(
    call_id: str, command: str, *, finish_reason: str = "tool_calls"
) -> dict[str, Any]:
    """A scripted assistant turn issuing ONE bash tool call."""
    return dict(
        tool_calls=[{"id": call_id, "name": "bash", "arguments": json.dumps({"command": command})}],
        finish_reason=finish_reason,
    )


def parallel_bash_script(commands: dict[str, str]) -> dict[str, Any]:
    """A scripted assistant turn issuing SEVERAL bash tool calls at once
    (the parallel-pool storm), ``{call_id: command}``.

    Each command is prefixed with a no-op ``: <call_id>;`` so the tool
    ARGUMENTS are distinct per call while the OUTPUT is unchanged (``:``
    ignores its args and prints nothing). Identical-argument parallel
    calls otherwise trip the session's repeat-tool-call guard, which
    appends a warning to the PERSISTED result only (not the live event) —
    an orthogonal divergence that would mask the recovery behavior the
    convergence assertions test.
    """
    return dict(
        tool_calls=[
            {
                "id": cid,
                "name": "bash",
                "arguments": json.dumps({"command": f": {cid}; {cmd}"}),
            }
            for cid, cmd in commands.items()
        ],
        finish_reason="tool_calls",
    )


def final_text_script(content: str = "done") -> dict[str, Any]:
    """The scripted assistant turn that ends the agent loop (no tools)."""
    return dict(content=content, finish_reason="stop")
