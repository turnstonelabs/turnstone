"""Browser-fidelity SSE recovery harness helpers.

The load-bearing assembly for ``tests/test_sse_recovery_e2e.py``: a
``BrowserlikeSSEClient`` that speaks the exact wire contract the real
``turnstone/shared_static/interactive.js`` pane speaks, and the
assertion helpers the scenarios share. The server boot machinery lives
in ``_sse_recovery_server.py``.

Why a raw-socket SSE reader (and not ``httpx.stream``): the slow-consumer
overflow scenario needs the consumer to STALL — stop reading the socket
so the server's SSE generator blocks on ``await send`` and stops draining
the per-UI listener queue, which then poisons at its cap. A faithful
stall needs (a) precise control over when bytes are read and (b) a small
``SO_RCVBUF`` so the in-flight backlog before poison stays bounded to
~100 KB instead of the client kernel's multi-MB autotuned default (which
would need tens of thousands of events to overflow). A raw socket gives
both; httpx (used here only for the plain ``/history`` request/response)
gives neither. This is ALSO closer to the browser: EventSource has a
bounded receive buffer, not an unbounded one.

Client contract mirrored from interactive.js (line references are to
that file on the ``fix/sse-truncated-resync`` branch):

- ``_last_event_id`` advances ONLY from SSE ``id:`` fields, and only
  ring-buffer events carry one — synthetic replay frames (connected /
  status / state_change / in_progress_snapshot / replay_truncated /
  stream_overflow) do not, exactly like ``EventSource.lastEventId``
  (interactive.js onmessage ~1378).
- reconnect presents ``connectCursor = _truncatedFromCursor ??
  _lastEventId`` as ``?last_event_id=`` (manual path) or a
  ``Last-Event-ID`` header (native EventSource auto-reconnect path)
  (interactive.js connectSSE ~1328).
- on a ``replay_truncated`` envelope the client records the
  truncation-time cursor keep-oldest (``_truncatedFromCursor =
  _lastEventId`` only when null) and runs ``_loadHistoryThenConnect``
  (disconnect → /history → adopt cursor → reconnect); a FAILED
  /history leaves the record armed so the reconnect re-presents the
  truncation-time cursor and re-draws the envelope (interactive.js
  handleEvent replay_truncated ~2303, _loadHistoryThenConnect ~1604,
  _refetchHistory seedCursor ~1706).
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

# Small client receive buffer so a stalled consumer's in-flight backlog
# before the server-side poison stays bounded (~100 KB) instead of the
# multi-MB autotuned default. Paired with the server's small SO_SNDBUF
# (see _sse_recovery_server.build_recovery_server).
_CLIENT_RCVBUF = 2048


@dataclass
class SSEFrame:
    """One decoded SSE frame, tagged with the connection it arrived on.

    ``event_id`` is the ``id:`` field verbatim (a stringified integer,
    or ``None`` for id-less synthetic frames — the same string domain as
    ``EventSource.lastEventId``). ``etype`` is the ``type`` field of the
    JSON ``data:`` payload (the application event type), distinct from
    any SSE ``event:`` field, which the server never uses.
    """

    conn_index: int
    event_id: str | None
    etype: str | None
    payload: dict[str, Any] | None
    raw: str

    @property
    def event_id_int(self) -> int | None:
        if self.event_id is None:
            return None
        try:
            return int(self.event_id)
        except ValueError:
            return None


class BrowserlikeSSEClient:
    """A single interactive pane's SSE + /history state machine.

    Not thread-safe against concurrent public calls; drive it from one
    test thread. Internally a per-connection reader thread decodes the
    stream; ``stall()`` / ``resume()`` gate that thread's socket reads so
    a test can build server-side backpressure without closing the
    connection (the slow-consumer → listener-queue-poison path).
    """

    def __init__(self, base_url: str, ws_id: str, token: str) -> None:
        parts = urlsplit(base_url)
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 80
        self._ws_id = ws_id
        self._token = token
        self._auth = {"Authorization": f"Bearer {token}"}
        self._http = httpx.Client(
            base_url=f"http://{self._host}:{self._port}", timeout=httpx.Timeout(15.0)
        )

        # EventSource-equivalent cursor state.
        self._last_event_id: str | None = None
        self._truncated_from_cursor: str | None = None

        # Transcript. ``_all_frames`` is the cross-connection accumulation
        # (what "the client eventually saw"); ``_conn_frames`` keeps each
        # connection's slice for per-connection assertions (contiguity).
        self._all_frames: list[SSEFrame] = []
        self._conn_frames: list[list[SSEFrame]] = []
        self._frames_lock = threading.Lock()

        # Reader plumbing.
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._read_gate = threading.Event()
        self._read_gate.set()  # reading permitted by default
        self._status: int | None = None
        self._headers_done = threading.Event()

    # -- connection lifecycle ------------------------------------------------

    def _events_path(self, cursor: str | None) -> str:
        path = f"/v1/api/workstreams/{self._ws_id}/events"
        if cursor is not None:
            path += f"?last_event_id={cursor}"
        return path

    def connect(self, *, native: bool = False, rcvbuf: int | None = None) -> None:
        """Open the SSE stream, presenting the client's current cursor.

        ``native=True`` models the browser's EventSource auto-reconnect:
        the cursor rides a ``Last-Event-ID`` HEADER and never appears in
        the URL. ``native=False`` models the manual ``new EventSource(url
        + '?last_event_id=')`` path interactive.js uses when it must
        override the live cursor (the ``connectCursor`` chokepoint).

        ``rcvbuf`` shrinks this connection's ``SO_RCVBUF`` — pass
        ``_CLIENT_RCVBUF`` on a connection the test will ``stall()`` so the
        in-flight backlog before the server-side poison stays bounded.
        Leave it ``None`` (OS default) on recovery reconnects so the ring
        replay is not throttled to a crawl.
        """
        if self._reader is not None:
            raise RuntimeError("already connected; disconnect() first")
        connect_cursor = (
            self._truncated_from_cursor
            if self._truncated_from_cursor is not None
            else self._last_event_id
        )
        header_lines = [
            f"Host: {self._host}:{self._port}",
            f"Authorization: Bearer {self._token}",
            "Accept: text/event-stream",
            "Cache-Control: no-cache",
        ]
        if native:
            path = self._events_path(None)
            if connect_cursor is not None:
                header_lines.append(f"Last-Event-ID: {connect_cursor}")
        else:
            path = self._events_path(connect_cursor)
        request = f"GET {path} HTTP/1.1\r\n" + "\r\n".join(header_lines) + "\r\n\r\n"

        sock = socket.create_connection((self._host, self._port), timeout=10)
        if rcvbuf is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        sock.sendall(request.encode())
        self._sock = sock

        self._stop.clear()
        self._read_gate.set()
        self._status = None
        self._headers_done.clear()
        conn_index = len(self._conn_frames)
        frames: list[SSEFrame] = []
        self._conn_frames.append(frames)
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(sock, conn_index, frames),
            name=f"sse-reader-{self._ws_id[:6]}-{conn_index}",
            daemon=True,
        )
        self._reader.start()

        # Surface a non-200 handshake to the caller (409 half-built UI,
        # 404 unknown ws, 401 auth) rather than silently reading nothing.
        if not self._headers_done.wait(timeout=10):
            self.disconnect()
            raise AssertionError("events connect: no HTTP response headers")
        if self._status != 200:
            status = self._status
            self.disconnect()
            raise AssertionError(f"events connect returned HTTP {status}")

    def disconnect(self) -> None:
        """Close the stream and join the reader (leak-guard clean)."""
        self._stop.set()
        self._read_gate.set()  # release a stalled reader so it sees _stop
        sock = self._sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # interrupt a blocked recv
        reader = self._reader
        if reader is not None:
            reader.join(timeout=15)
            if reader.is_alive():
                raise AssertionError("SSE reader thread failed to stop")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        self._sock = None
        self._reader = None

    def close(self) -> None:
        """Full teardown: disconnect any live stream + close the HTTP client."""
        if self._reader is not None:
            self.disconnect()
        self._http.close()

    # -- the stall gate (backpressure driver) --------------------------------

    def stall(self) -> None:
        """Stop reading the socket. The kernel + uvicorn send buffers fill,
        blocking the server's SSE generator on its ``await send``, so it
        stops draining the per-UI listener queue — which poisons at its cap.
        """
        self._read_gate.clear()

    def resume(self) -> None:
        """Resume reading. A poisoned-and-closed stream delivers its
        ``stream_overflow`` farewell frame once the backlog drains."""
        self._read_gate.set()

    # -- reader --------------------------------------------------------------

    def _read_loop(self, sock: socket.socket, conn_index: int, frames: list[SSEFrame]) -> None:
        raw = b""  # undecoded bytes (headers, then chunked framing)
        sse = b""  # decoded SSE byte stream
        headers_parsed = False
        chunked = False
        while not self._stop.is_set():
            # Backpressure gate: while stalled we do NOT read the socket, so
            # its receive buffer fills and TCP flow control stalls the server.
            if not self._read_gate.wait(timeout=0.1):
                continue
            if self._stop.is_set():
                break
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break  # server closed
            raw += chunk
            if not headers_parsed:
                if b"\r\n\r\n" not in raw:
                    continue
                header_blob, raw = raw.split(b"\r\n\r\n", 1)
                self._parse_headers(header_blob)
                chunked = b"transfer-encoding: chunked" in header_blob.lower()
                headers_parsed = True
                self._headers_done.set()
            if chunked:
                decoded, raw = _dechunk(raw)
                sse += decoded
            else:
                sse += raw
                raw = b""
            sse = sse.replace(b"\r\n", b"\n")
            while b"\n\n" in sse:
                block, sse = sse.split(b"\n\n", 1)
                self._handle_block(block.decode("utf-8", "replace"), conn_index, frames)

    def _parse_headers(self, header_blob: bytes) -> None:
        first_line = header_blob.split(b"\r\n", 1)[0].decode("latin-1")
        # "HTTP/1.1 200 OK"
        parts = first_line.split(" ", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            self._status = int(parts[1])

    def _handle_block(self, block_text: str, conn_index: int, frames: list[SSEFrame]) -> None:
        event_id: str | None = None
        data_parts: list[str] = []
        retry: str | None = None
        for line in block_text.split("\n"):
            if not line or line.startswith(":"):
                continue  # blank or comment (ping)
            field_name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]  # SSE strips a single leading space
            if field_name == "id":
                event_id = value
            elif field_name == "data":
                data_parts.append(value)
            elif field_name == "retry":
                retry = value
        # EventSource semantics: an event carrying an ``id:`` sets the
        # last-event-id buffer; an event without one leaves it unchanged.
        if event_id is not None:
            self._last_event_id = event_id
        if not data_parts:
            if retry is not None:
                self._record(SSEFrame(conn_index, None, "retry", None, block_text), frames)
            return
        data_str = "\n".join(data_parts)
        payload: dict[str, Any] | None
        try:
            parsed = json.loads(data_str)
            payload = parsed if isinstance(parsed, dict) else None
        except ValueError:
            payload = None
        etype = payload.get("type") if payload is not None else None
        frame = SSEFrame(conn_index, event_id, etype, payload, data_str)
        self._record(frame, frames)
        # Mirror the pane: the FIRST replay_truncated for an unrepaired gap
        # records the truncation-time cursor (keep-oldest). Its consumer is
        # the reconnect chokepoint (see ``connect``).
        if etype == "replay_truncated" and self._truncated_from_cursor is None:
            self._truncated_from_cursor = self._last_event_id

    def _record(self, frame: SSEFrame, frames: list[SSEFrame]) -> None:
        with self._frames_lock:
            frames.append(frame)
            self._all_frames.append(frame)

    # -- /history + cursor flow ----------------------------------------------

    def fetch_history(self) -> dict[str, Any]:
        """GET /history and return the parsed JSON ({ws_id, messages, cursor})."""
        r = self._http.get(f"/v1/api/workstreams/{self._ws_id}/history", headers=self._auth)
        r.raise_for_status()
        result: dict[str, Any] = r.json()
        return result

    def seed_from_history(self) -> dict[str, Any]:
        """The seedCursor step: fetch /history, adopt a non-null resume
        cursor into ``_last_event_id``, and clear the truncation record on
        a successful render (replayHistory clears ``_truncatedFromCursor``).
        """
        data = self.fetch_history()
        cursor = data.get("cursor")
        if cursor is not None:
            self._last_event_id = str(cursor)
        self._truncated_from_cursor = None  # successful full render repairs the gap
        return data

    def load_history_then_connect(
        self, *, fail_history: bool = False, native: bool = False
    ) -> dict[str, Any] | None:
        """Reproduce interactive.js ``_loadHistoryThenConnect``.

        Disconnect first, drop the live cursor (``_last_event_id = None``)
        but KEEP ``_truncated_from_cursor`` armed, then fetch /history and
        reconnect. On success adopt the returned cursor and clear the
        truncation record; on a FAILED /history (``fail_history`` — the
        harness IS the client here, so a client-side simulated failure is
        faithful) leave the record armed so the reconnect re-presents the
        truncation-time cursor and re-draws ``replay_truncated``.

        Returns the /history JSON, or ``None`` when the fetch failed.
        """
        if self._reader is not None:
            self.disconnect()
        self._last_event_id = None
        data: dict[str, Any] | None
        if fail_history:
            data = None
        else:
            data = self.fetch_history()
            cursor = data.get("cursor")
            if cursor is not None:
                self._last_event_id = str(cursor)
            self._truncated_from_cursor = None
        self.connect(native=native)
        return data

    # -- accessors + waits ---------------------------------------------------

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    @property
    def truncated_from_cursor(self) -> str | None:
        return self._truncated_from_cursor

    def all_frames(self) -> list[SSEFrame]:
        with self._frames_lock:
            return list(self._all_frames)

    def conn_frames(self, conn_index: int) -> list[SSEFrame]:
        with self._frames_lock:
            return list(self._conn_frames[conn_index])

    def latest_conn_frames(self) -> list[SSEFrame]:
        with self._frames_lock:
            return list(self._conn_frames[-1]) if self._conn_frames else []

    def num_connections(self) -> int:
        with self._frames_lock:
            return len(self._conn_frames)

    def frames_of_type(self, etype: str) -> list[SSEFrame]:
        return [f for f in self.all_frames() if f.etype == etype]

    def has_type(self, etype: str) -> bool:
        return any(f.etype == etype for f in self.all_frames())

    def tool_output_by_call(self) -> dict[str, str]:
        """Concatenate every ``tool_output_chunk`` payload per call_id, in
        arrival order — the reconstructed live stream for each call."""
        out: dict[str, str] = {}
        for f in self.all_frames():
            if f.etype == "tool_output_chunk" and f.payload is not None:
                cid = str(f.payload.get("call_id", ""))
                out[cid] = out.get(cid, "") + str(f.payload.get("chunk", ""))
        return out

    def tool_results_by_call(self) -> dict[str, str]:
        """The last ``tool_result`` output seen per call_id."""
        out: dict[str, str] = {}
        for f in self.all_frames():
            if f.etype == "tool_result" and f.payload is not None:
                out[str(f.payload.get("call_id", ""))] = str(f.payload.get("output", ""))
        return out

    def wait_for_type(self, etype: str, *, timeout: float = 45.0) -> SSEFrame:
        """Block until a frame of ``etype`` has arrived on ANY connection."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for f in self.all_frames():
                if f.etype == etype:
                    return f
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for a {etype!r} frame")

    def wait_for(
        self, predicate: Callable[[BrowserlikeSSEClient], bool], *, timeout: float = 45.0
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self):
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for predicate")

    def wait_for_call_result(self, call_id: str, *, timeout: float = 45.0) -> None:
        self.wait_for(lambda c: call_id in c.tool_results_by_call(), timeout=timeout)


def _dechunk(buf: bytes) -> tuple[bytes, bytes]:
    """Incrementally decode HTTP/1.1 chunked transfer-encoding.

    Consumes as many COMPLETE chunks from ``buf`` as possible and returns
    ``(decoded_bytes, remainder)`` where ``remainder`` is the trailing
    partial chunk to carry into the next read. A zero-length chunk (stream
    end) simply stops consumption; the reader's ``recv`` EOF handles close.
    """
    decoded = b""
    while True:
        if b"\r\n" not in buf:
            break  # incomplete size line
        size_line, rest = buf.split(b"\r\n", 1)
        try:
            n = int(size_line.strip() or b"z", 16)
        except ValueError:
            break  # malformed / partial — wait for more bytes
        if n == 0:
            break  # last chunk marker
        if len(rest) < n + 2:  # need n data bytes + trailing CRLF
            break
        decoded += rest[:n]
        buf = rest[n + 2 :]
    return decoded, buf


# ---------------------------------------------------------------------------
# Assertion helpers (shared by the scenarios).
# ---------------------------------------------------------------------------


def assert_contiguous_ids(frames: list[SSEFrame]) -> None:
    """Every id-bearing frame in a connection forms a gap-free, dup-free,
    strictly increasing run.

    Holds for a connection that took no ``_seq``-filtered fresh path — a
    fresh connect made before any event (snap_seq == 0) and every
    ``replay_ok`` reconnect (snap_seq == 0). The server stamps a fresh
    monotonic id per enqueue with no in-ring coalescing, so a
    non-filtered consumer sees consecutive ids.
    """
    ids = [f.event_id_int for f in frames if f.event_id_int is not None]
    assert ids, "connection carried no id-bearing frames"
    assert len(set(ids)) == len(ids), f"duplicate SSE ids: {ids}"
    assert ids == sorted(ids), f"SSE ids not monotonic: {ids}"
    for prev, cur in zip(ids, ids[1:], strict=False):
        assert cur == prev + 1, f"gap in SSE ids between {prev} and {cur}: {ids}"


def assert_ids_monotonic_no_dupes(frames: list[SSEFrame]) -> None:
    """Weaker invariant that holds on EVERY connection (including
    ``_seq``-filtered fresh/truncated paths, where gaps are legal): ids
    are strictly increasing with no duplicates."""
    ids = [f.event_id_int for f in frames if f.event_id_int is not None]
    assert len(set(ids)) == len(ids), f"duplicate SSE ids: {ids}"
    assert ids == sorted(ids), f"SSE ids not monotonic: {ids}"


def assert_chunk_result_ordering(frames: list[SSEFrame]) -> None:
    """Every ``tool_output_chunk`` for a call precedes that call's own
    ``tool_result`` on the wire (the load-bearing ordering — the client
    removes the streaming <pre> when it renders the result)."""
    result_index: dict[str, int] = {}
    for i, f in enumerate(frames):
        if f.etype == "tool_result" and f.payload is not None:
            result_index[str(f.payload.get("call_id", ""))] = i
    for i, f in enumerate(frames):
        if f.etype == "tool_output_chunk" and f.payload is not None:
            cid = str(f.payload.get("call_id", ""))
            assert cid in result_index, f"chunk for call {cid} has no tool_result"
            assert i < result_index[cid], (
                f"chunk for call {cid} arrived AFTER its tool_result "
                f"(chunk idx {i} >= result idx {result_index[cid]})"
            )


def assert_children_stamped(frames: list[SSEFrame], parent_call_id: str) -> None:
    """Every sub-agent child tool event carries ``parent_call_id`` (stamped
    at the flush chokepoint). Sub-tool call_ids are minted
    ``{parent}::r{run}s{step}::{provider_id}`` — the ``::`` segment is the
    identifying mark — and NONE may escape unstamped to the top level."""
    unstamped: list[tuple[str | None, str, Any]] = []
    stamped = 0
    for f in frames:
        if f.payload is None:
            continue
        items = f.payload.get("items")
        entries = items if isinstance(items, list) else [f.payload]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("call_id", ""))
            if "::" not in cid:
                continue
            if entry.get("parent_call_id") == parent_call_id:
                stamped += 1
            else:
                unstamped.append((f.etype, cid, entry.get("parent_call_id")))
    assert stamped > 0, f"no child events found for parent {parent_call_id}"
    assert not unstamped, f"child events escaped unstamped (parent {parent_call_id}): {unstamped}"


def history_tool_outputs(history_json: dict[str, Any]) -> dict[str, str]:
    """Extract {call_id: output} from a /history projection, however the
    projection surfaces results (a folded ``output`` on a tool_call, or a
    trailing ``role: tool`` row keyed by ``tool_call_id``)."""
    out: dict[str, str] = {}
    for msg in history_json.get("messages", []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id") or msg.get("call_id")
            if cid is not None:
                out[str(cid)] = str(msg.get("content", ""))
        for tc in msg.get("tool_calls") or ():
            if not isinstance(tc, dict):
                continue
            cid = tc.get("id") or tc.get("call_id")
            if cid is not None and tc.get("output") is not None:
                out[str(cid)] = str(tc.get("output", ""))
    return out


def assert_converged(client: BrowserlikeSSEClient, history_json: dict[str, Any]) -> None:
    """Turn-level equivalence: every tool result the client assembled live
    is present, with the same output, in a fresh /history projection.

    Compares by call_id so a reconnect that re-delivered a result can't
    hide a divergence, and asserts the /history side isn't empty (a
    silently-lost turn would leave the projection short)."""
    live = client.tool_results_by_call()
    hist = history_tool_outputs(history_json)
    assert hist, "fresh /history projected no tool results — a turn was lost"
    for call_id, output in live.items():
        assert call_id in hist, f"call {call_id} seen live but absent from /history: {sorted(hist)}"
        assert hist[call_id] == output, (
            f"call {call_id} output diverged: live={output!r} history={hist[call_id]!r}"
        )
