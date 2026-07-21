"""End-to-end SSE recovery harness (opt-in) for the three fixes on
``fix/sse-truncated-resync``:

1. **tool_output_chunk batching (de-amplifier).** A parallel storm of
   line-chatty bash tools no longer emits one SSE event per stdout line.
2. **Honest truncated replay.** An empty ring after eviction / node
   restart reports ``replay_truncated`` with a real ``lost_count`` instead
   of a silent ``replay_ok``; the client adopts the ``/history`` resume
   cursor to rebuild the in-flight turn.
3. **Sub-agent attribution.** Every child tool event a task_agent's
   sub-tools produce is stamped ``parent_call_id`` at the batch flush.

Each scenario drives a REAL interactive server (real ``SessionManager`` +
real ``ChatSession`` engine executing REAL bash) through a scripted
provider at the SDK boundary, with a ``BrowserlikeSSEClient`` that speaks
the exact interactive.js wire contract, and asserts convergence.

Marked ``e2e_recovery`` (select with ``-m e2e_recovery``) AND ``live`` so
the fast default suite (``-m "not live"``) skips them: the marker
mechanism the repo already deselects by. Each scenario runs in tens of
seconds; the tier-1 suite stays under ~5 minutes.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from tests._sse_recovery_helpers import (
    BrowserlikeSSEClient,
    assert_children_stamped,
    assert_chunk_result_ordering,
    assert_contiguous_ids,
    assert_converged,
    history_tool_outputs,
)
from tests._sse_recovery_server import (
    RecoveryServer,
    bash_toolcall_script,
    final_text_script,
    parallel_bash_script,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = [pytest.mark.e2e_recovery, pytest.mark.live]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def recovery_db(tmp_path: object) -> Iterator[str]:
    """A throwaway migrated sqlite DB (singleton registry) for one test."""
    from turnstone.core.storage import init_storage, reset_storage

    db_path = str(tmp_path / "recovery.db")  # type: ignore[operator]
    reset_storage()
    init_storage("sqlite", path=db_path, run_migrations=True)
    yield db_path
    reset_storage()


@pytest.fixture
def make_server(recovery_db: str) -> Iterator[Callable[..., RecoveryServer]]:
    """Factory that builds RecoveryServers and stops them all at teardown
    (LIFO), so a restart scenario can build a second node on the same DB."""
    servers: list[RecoveryServer] = []

    def _make(**kwargs: object) -> RecoveryServer:
        srv = RecoveryServer(**kwargs)  # type: ignore[arg-type]
        servers.append(srv)
        return srv

    yield _make
    for srv in reversed(servers):
        srv.stop()


SEQ_OUTPUT = "".join(f"{n}\n" for n in range(1, 501))  # `seq 1 500` chunk stream
# A PACED storm: streams slowly so an early disconnect captures a cursor
# genuinely below the turn's committed-message counter (the restart-loss case).
PACED_STORM = "for i in $(seq 1 40); do echo r-$i; sleep 0.05; done"


# ---------------------------------------------------------------------------
# Scenario 1 — storm without loss (fix-3 efficacy)
# ---------------------------------------------------------------------------


def test_storm_batches_without_loss(make_server: Callable[..., RecoveryServer]) -> None:
    """One turn issues 4 parallel ``seq 1 500`` bash calls. Assert the
    per-call chunk events are batched FAR below 500, each call's chunks
    strictly precede its own result, the concatenated chunks reconstruct
    the exact seq output, and a normally-draining consumer never overflows."""
    srv = make_server()
    call_ids = [f"call_{i}" for i in range(4)]
    ws_id = srv.create_workstream(
        parallel_bash_script(dict.fromkeys(call_ids, "seq 1 500")),
        final_text_script("ran the storm"),
        name="storm",
    )
    client = BrowserlikeSSEClient(srv.base_url, ws_id, srv.token)
    try:
        client.connect()  # before send — the listener must be registered
        srv.send(ws_id, "run the storm")
        srv.wait_turn(ws_id, timeout=45)
        for cid in call_ids:
            client.wait_for_call_result(cid, timeout=20)
        time.sleep(0.4)  # settle trailing frames

        # De-amplification: each call's chunk-event count is far below 500.
        per_call: dict[str, int] = {}
        for f in client.frames_of_type("tool_output_chunk"):
            assert f.payload is not None
            cid = str(f.payload["call_id"])
            per_call[cid] = per_call.get(cid, 0) + 1
        assert set(per_call) == set(call_ids), per_call
        for cid, count in per_call.items():
            assert count <= 60, f"{cid} emitted {count} chunk events (batching failed)"

        # Lossless reconstruction per call + chunks precede results.
        recon = client.tool_output_by_call()
        for cid in call_ids:
            assert recon[cid] == SEQ_OUTPUT, f"{cid} chunk stream diverged from seq 1..500"
        assert_chunk_result_ordering(client.all_frames())

        # A fresh connect made before any event has snap_seq==0, so the whole
        # id-bearing stream is a gap-free contiguous run.
        assert_contiguous_ids(client.conn_frames(0))

        # A normally-draining consumer never overflows, and every committed
        # tool result matches a fresh /history projection.
        assert not client.has_type("stream_overflow")
        assert_converged(client, srv.fetch_history(ws_id))
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Scenario 2 — slow-consumer overflow -> poison -> lossless reconnect
# ---------------------------------------------------------------------------


def test_slow_consumer_overflow_then_lossless_reconnect(
    make_server: Callable[..., RecoveryServer],
) -> None:
    """A stalled consumer poisons the listener queue; the server closes the
    stream with an id-less ``stream_overflow`` frame; a native reconnect
    with the pre-gap cursor replays the gap from the ring with contiguous
    ids; the final transcript converges with a fresh /history projection."""
    # fix-3 de-amplifies so hard that a real 500-cap overflow needs a
    # pathological storm; a small cap + small buffers exercise the identical
    # _ListenerOverflow -> stream_overflow -> reconnect-replay path.
    srv = make_server(sndbuf=8192, listener_cap=64)
    call_ids = [f"c{i}" for i in range(4)]
    # ~4 KB-per-line paced output so the stall builds a poisoning backlog.
    paced = "for i in $(seq 1 100); do printf '%04000d\\n' $i; sleep 0.03; done"
    ws_id = srv.create_workstream(
        parallel_bash_script(dict.fromkeys(call_ids, paced)),
        final_text_script("done"),
        name="overflow",
    )
    client = BrowserlikeSSEClient(srv.base_url, ws_id, srv.token)
    try:
        client.connect(rcvbuf=2048)  # small buffer -> the stall poisons quickly
        srv.send(ws_id, "overflow storm")
        client.wait_for(lambda c: len(c.frames_of_type("tool_output_chunk")) >= 2, timeout=20)
        pre_gap = client.last_event_id
        assert pre_gap is not None

        client.stall()
        _wait(lambda: srv.listener_poisoned(ws_id), timeout=20, what="listener poison")
        # The turn finishes while the consumer is stalled: the worker keeps
        # enqueuing (dropped by the poisoned queue) but the ring keeps all.
        srv.wait_turn(ws_id, timeout=45)
        client.resume()

        overflow = client.wait_for_type("stream_overflow", timeout=15)
        assert overflow.event_id is None, "stream_overflow must be id-less (cursor stays below gap)"

        # Native EventSource reconnect: the pre-gap cursor rides a
        # Last-Event-ID header; the ring replays everything past it.
        client.disconnect()
        client.connect(native=True)
        for cid in call_ids:
            client.wait_for_call_result(cid, timeout=30)

        # The reconnect replayed the gap on the replay_ok path (snap_seq==0):
        # a gap-free, dup-free contiguous run.
        assert_contiguous_ids(client.conn_frames(1))
        assert_converged(client, srv.fetch_history(ws_id))
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Scenario 3 — truncated mid-run -> cursor-adoption rebuild (fix-2 client)
# ---------------------------------------------------------------------------


def test_truncated_midrun_cursor_adoption_rebuild(
    make_server: Callable[..., RecoveryServer],
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer disconnects early in turn 1 (cursor C). Turn 1 commits
    (boundary B1 > C). Turn 2 — the in-flight orphan storm — evicts C from a
    small ring while keeping B1. Reconnect is truncated; /history's orphan
    trim returns a non-null cursor (B1 still replayable); the cursor
    reconnect's replay_ok delta rebuilds the in-flight turn; convergence
    holds with no duplicate ids per connection."""
    # A small ring so turn 1's tail evicts the disconnected consumer's cursor.
    # Read at UI construction, so patch BEFORE the workstream is created.
    monkeypatch.setattr("turnstone.core.session_ui_base._EVENT_BUFFER_MAX", 15)
    release = str(tmp_path / "release_turn2")  # type: ignore[operator]

    srv = make_server()
    t1 = "for i in $(seq 1 30); do echo t1-$i; sleep 0.04; done"
    # Turn 2 is FILE-GATED: it stays in-flight until the test releases it,
    # removing every timing race around "reconnect while the turn is live".
    gated = f"echo t2-start; while [ ! -f {release} ]; do sleep 0.05; done; seq 1 20; echo t2-done"
    t2_ids = [f"c{i}" for i in range(3)]
    ws_id = srv.create_workstream(
        bash_toolcall_script("t1call", t1),
        final_text_script("t1 done"),
        parallel_bash_script(dict.fromkeys(t2_ids, gated)),
        final_text_script("t2 done"),
        name="truncate",
    )
    client = BrowserlikeSSEClient(srv.base_url, ws_id, srv.token)
    try:
        client.connect()
        srv.send(ws_id, "turn 1")
        client.wait_for(lambda c: len(c.frames_of_type("tool_output_chunk")) >= 2, timeout=20)
        cursor_c = int(client.last_event_id or "-1")
        client.disconnect()
        srv.wait_turn(ws_id, timeout=30)  # turn 1 commits
        b1 = srv.max_event_id(ws_id)
        assert b1 is not None and b1 > cursor_c

        srv.send(ws_id, "turn 2 storm")  # the in-flight orphan

        def orphan_inflight_and_c_evicted() -> bool:
            earliest, latest = srv.ring_span(ws_id)
            return (
                srv.ws_state(ws_id) == "running"
                and earliest is not None
                and earliest > cursor_c
                and latest > b1 + 2
            )

        _wait(orphan_inflight_and_c_evicted, timeout=20, what="turn 2 in-flight + C evicted")

        # Reconnect stale -> replay_truncated with a real lost_count.
        client.connect()
        truncated = client.wait_for_type("replay_truncated", timeout=15)
        assert truncated.payload is not None
        assert truncated.payload["lost_count"] > 0

        # /history's orphan trim returns a non-null (replayable) cursor while
        # the turn is in flight.
        hist = client.fetch_history()
        assert hist["cursor"] is not None, "in-flight orphan should trim to a resume cursor"

        # The fix's flow: disconnect -> /history -> adopt cursor -> reconnect.
        client.load_history_then_connect()
        # The cursor-adoption reconnect takes replay_ok (no second truncation)
        # and its delta rebuilds the in-flight turn's tool events.
        client.wait_for(
            lambda c: any(
                f.conn_index == c.num_connections() - 1
                and f.etype in ("tool_pending", "tool_info", "tool_output_chunk")
                for f in c.all_frames()
            ),
            timeout=15,
        )
        adopt_conn = client.latest_conn_frames()
        assert not any(f.etype == "replay_truncated" for f in adopt_conn)

        # Release the orphan; it runs to completion and the transcript converges.
        open(release, "w").close()  # noqa: SIM115 — a one-shot release flag
        srv.wait_turn(ws_id, timeout=40)
        for cid in t2_ids:
            client.wait_for_call_result(cid, timeout=25)
        time.sleep(0.4)
        assert_converged(client, srv.fetch_history(ws_id))

        # No duplicate ids WITHIN any single connection (cross-connection
        # overlap on reconnect is legal; intra-connection dups are not).
        for i in range(client.num_connections()):
            ids = [f.event_id_int for f in client.conn_frames(i) if f.event_id_int is not None]
            assert len(ids) == len(set(ids)), f"duplicate ids on connection {i}: {ids}"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Scenario 4 — rehydrate / restart honesty (fix-2 server)
# ---------------------------------------------------------------------------


def test_restart_truncated_honesty(make_server: Callable[..., RecoveryServer]) -> None:
    """A client whose cursor predates the turn's committed messages
    disconnects; the turn completes (storage counter = seeded); the node
    restarts (fresh UI, empty ring, storage-seeded counter). Reconnecting
    with the stale cursor draws ``replay_truncated`` (NOT a silent
    ``replay_ok``) with the exact lost_count; /history returns cursor=null
    (empty ring); a fresh connect converges. The no-loss variant —
    cursor == seeded — draws ``replay_ok`` (empty)."""
    srv1 = make_server()
    call_ids = ["r0", "r1"]
    ws_id = srv1.create_workstream(
        parallel_bash_script(dict.fromkeys(call_ids, PACED_STORM)),
        final_text_script("done"),
        name="restart",
    )
    watcher = BrowserlikeSSEClient(srv1.base_url, ws_id, srv1.token)
    try:
        watcher.connect()
        srv1.send(ws_id, "go")
        # Disconnect MID-turn: the cursor predates the committed messages.
        watcher.wait_for(lambda c: len(c.frames_of_type("tool_output_chunk")) >= 2, timeout=20)
        stale_cursor = int(watcher.last_event_id or "-1")
        watcher.disconnect()
    finally:
        watcher.close()
    srv1.wait_turn(ws_id, timeout=45)  # turn completes; committed counter set
    seeded = srv1.max_event_id(ws_id)
    assert seeded is not None and stale_cursor < seeded
    srv1.stop()  # node crash — the ring dies with the process, the DB survives

    # Restart on the same DB.
    srv2 = make_server()
    srv2.open_workstream(ws_id)  # rehydrate: fresh UI, empty ring, seeded counter
    assert srv2.max_event_id(ws_id) == seeded

    # LOSS: reconnect with the stale cursor -> truncated with exact lost_count.
    loss = BrowserlikeSSEClient(srv2.base_url, ws_id, srv2.token)
    try:
        loss._last_event_id = str(stale_cursor)
        loss.connect()
        tf = loss.wait_for_type("replay_truncated", timeout=15)
        assert tf.payload is not None
        assert tf.payload["lost_count"] == seeded - stale_cursor
        # /history on an empty ring withholds the cursor (can_replay_from
        # asymmetry) — the client rebuilds from the REST snapshot instead.
        hist = loss.fetch_history()
        assert hist["cursor"] is None
        # The committed conversation is intact — a fresh /history projection
        # carries every tool result (no permanently lost turn).
        recovered = history_tool_outputs(hist)
        assert set(call_ids).issubset(set(recovered))
    finally:
        loss.close()

    # NO-LOSS: cursor == seeded counter -> replay_ok (empty), never truncated.
    noloss = BrowserlikeSSEClient(srv2.base_url, ws_id, srv2.token)
    try:
        noloss._last_event_id = str(seeded)
        noloss.connect()
        time.sleep(1.0)
        assert not noloss.has_type("replay_truncated"), (
            "cursor == seeded is no loss — must be replay_ok, not truncated"
        )
    finally:
        noloss.close()


# ---------------------------------------------------------------------------
# Scenario 5 — failed-resync retry via the truncation record
# ---------------------------------------------------------------------------


def test_failed_resync_retries_via_truncation_record(
    make_server: Callable[..., RecoveryServer],
) -> None:
    """Restart truncation as in scenario 4, but the client's FIRST /history
    resync fails: the reconnect re-presents the truncation-time cursor and
    draws ``replay_truncated`` AGAIN (the retry stays armed). A subsequent
    successful /history then converges."""
    srv1 = make_server()
    call_ids = ["r0", "r1"]
    ws_id = srv1.create_workstream(
        parallel_bash_script(dict.fromkeys(call_ids, PACED_STORM)),
        final_text_script("done"),
        name="failed-resync",
    )
    watcher = BrowserlikeSSEClient(srv1.base_url, ws_id, srv1.token)
    try:
        watcher.connect()
        srv1.send(ws_id, "go")
        watcher.wait_for(lambda c: len(c.frames_of_type("tool_output_chunk")) >= 2, timeout=20)
        stale_cursor = int(watcher.last_event_id or "-1")
        watcher.disconnect()
    finally:
        watcher.close()
    srv1.wait_turn(ws_id, timeout=45)
    seeded = srv1.max_event_id(ws_id)
    assert seeded is not None and stale_cursor < seeded
    srv1.stop()

    srv2 = make_server()
    srv2.open_workstream(ws_id)

    client = BrowserlikeSSEClient(srv2.base_url, ws_id, srv2.token)
    try:
        client._last_event_id = str(stale_cursor)
        client.connect()
        client.wait_for_type("replay_truncated", timeout=15)
        # The truncation-time cursor is recorded (keep-oldest).
        assert client.truncated_from_cursor == str(stale_cursor)

        # FIRST resync FAILS: /history is not fetched, so the record stays
        # armed and the reconnect re-presents the truncation-time cursor ->
        # the server re-answers replay_truncated on the NEW connection.
        client.load_history_then_connect(fail_history=True)
        client.wait_for(
            lambda c: any(f.etype == "replay_truncated" for f in c.latest_conn_frames()),
            timeout=15,
        )
        assert client.truncated_from_cursor == str(stale_cursor), "record must stay armed"

        # SUCCESSFUL resync: /history clears the record and the committed
        # conversation is recovered intact.
        data = client.load_history_then_connect()
        assert data is not None
        assert client.truncated_from_cursor is None
        recovered = history_tool_outputs(data)
        assert set(call_ids).issubset(set(recovered))
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Scenario 6 — sub-agent storm attribution (fix-3 parent stamping)
# ---------------------------------------------------------------------------


def test_subagent_storm_attribution(make_server: Callable[..., RecoveryServer]) -> None:
    """A task_agent whose sub-tools are chatty bashes. Every chunk / tool
    event for a child call carries ``parent_call_id`` (stamped at the batch
    flush by its still-registered call_id); none escape unstamped to the top
    level; the child chunk streams reconstruct losslessly."""
    import json

    srv = make_server()
    task_call = dict(
        tool_calls=[
            {
                "id": "task1",
                "name": "task_agent",
                "arguments": json.dumps({"prompt": "run the sub-tools"}),
            }
        ],
        finish_reason="tool_calls",
    )
    sub_bash = dict(
        tool_calls=[
            {"id": "sub_a", "name": "bash", "arguments": json.dumps({"command": ": a; seq 1 300"})},
            {"id": "sub_b", "name": "bash", "arguments": json.dumps({"command": ": b; seq 1 300"})},
        ],
        finish_reason="tool_calls",
    )
    ws_id = srv.create_workstream(
        task_call,  # parent issues the task_agent
        sub_bash,  # sub-agent issues chatty bashes
        final_text_script("sub done"),  # sub-agent finishes
        final_text_script("parent done"),  # parent finishes
        name="subagent",
    )
    client = BrowserlikeSSEClient(srv.base_url, ws_id, srv.token)
    try:
        client.connect()
        srv.send(ws_id, "spawn a sub agent")
        srv.wait_turn(ws_id, timeout=60)
        client.wait_for_call_result("task1", timeout=25)
        time.sleep(0.6)

        # Every child tool event carries parent_call_id="task1"; none escape.
        assert_children_stamped(client.all_frames(), "task1")

        # The two sub-tools' minted call_ids appear and reconstruct losslessly.
        child_output = {
            cid: text for cid, text in client.tool_output_by_call().items() if "::" in cid
        }
        assert len(child_output) == 2, sorted(child_output)
        seq_300 = "".join(f"{n}\n" for n in range(1, 301))
        for cid, text in child_output.items():
            assert text == seq_300, f"child {cid} chunk stream diverged from seq 1..300"

        # The parent's synthesis result is present.
        assert "task1" in client.tool_results_by_call()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Shared wait helper
# ---------------------------------------------------------------------------


def _wait(predicate: Callable[[], bool], *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError(f"timed out waiting for {what}")
