from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest


def stop_loop_thread(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    """Fully tear down a ``loop.run_forever``-in-a-thread test loop.

    Shuts the loop's default executor down ON the loop (joining its worker
    threads — the ``asyncio_N`` threads that otherwise leak past the test),
    then stops the loop, joins the thread, and closes the loop. Use in the
    ``finally`` of a background-loop fixture so nothing outlives the test.
    """
    with contextlib.suppress(Exception):
        asyncio.run_coroutine_threadsafe(loop.shutdown_default_executor(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    with contextlib.suppress(Exception):
        loop.close()


def serve_until_exit(server: Any) -> None:
    """Run a uvicorn ``Server`` on a fresh event loop until it exits.

    The thread target for an in-thread test upstream: when ``server.serve()``
    returns (the fixture set ``server.should_exit`` / ``force_exit``), the loop
    is closed so it doesn't leak past the fixture.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        # Cancel + drain anything the app left pending (e.g. sse_starlette's
        # shutdown watcher) so loop.close() doesn't warn "Task was destroyed
        # but it is pending".
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


class _PendingResolver:
    """Race-free drop-in for ``threading.Timer(delay, ui.resolve_approval)``.

    ``approve_tools`` runs ``_approval_event.clear()`` -> register
    ``_pending_approval`` -> ``_approval_event.wait(_APPROVAL_WAIT_TIMEOUT)``
    (3600s). A *fixed-delay* timer can fire ``resolve_approval``
    (``_approval_event.set()``) BEFORE that ``.clear()`` on a slow/loaded
    runner, so the set is wiped by the clear and ``approve_tools`` blocks the
    full hour -- surfacing as a CI hang. This instead waits until the approval
    is actually registered (which happens *after* the clear), then resolves, so
    the wakeup can never be lost. ``start()`` / ``cancel()`` mirror
    ``threading.Timer`` so it drops into existing scaffolding. ``cancel()``
    signals the worker to stop and joins it, so a test that errors *before* the
    approval registers can't leak the thread or resolve late into a finished
    test. ``before`` runs just before resolving -- e.g. to snapshot
    pending-state fields the test asserts on.
    """

    def __init__(
        self,
        ui: Any,
        *args: Any,
        before: Callable[[], None] | None = None,
        deadline: float = 10.0,
        **kwargs: Any,
    ) -> None:
        self._ui = ui
        self._args = args
        self._kwargs = kwargs
        self._before = before
        self._deadline = deadline
        self._cancelled = threading.Event()
        self._started = False
        self._thread = threading.Thread(target=self._run, name="resolve-when-pending", daemon=True)

    def _run(self) -> None:
        end = time.monotonic() + self._deadline
        while time.monotonic() < end:
            if self._cancelled.is_set():
                return
            # getattr (not a bare read) so a UI without _pending_approval can't
            # crash the worker into a silent death that leaves approve_tools
            # blocked for the full _APPROVAL_WAIT_TIMEOUT.
            if getattr(self._ui, "_pending_approval", None) is not None:
                if self._before is not None:
                    self._before()
                self._ui.resolve_approval(*self._args, **self._kwargs)
                return
            time.sleep(0.001)
        # Deadline without registration: approve_tools isn't parked on the
        # approval event (returned early, or never reached it) -- don't resolve
        # into an unknown state; let the test's own assertions speak.

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()
        if self._started:
            self._thread.join(timeout=5)


def resolve_when_pending(ui: Any, *args: Any, **kwargs: Any) -> _PendingResolver:
    """Build a race-free approval resolver (see :class:`_PendingResolver`)."""
    return _PendingResolver(ui, *args, **kwargs)


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from turnstone.core.mcp_client import MCPClientManager, StaticServerState
    from turnstone.core.mcp_crypto import MCPTokenCipher
    from turnstone.core.oidc import OIDCConfig


# A background daemon (e.g. title generation) can log into pytest's per-test
# capture as it is torn down — a benign "I/O operation on closed file" handler
# error. Don't let the logging module turn that race into noisy stderr
# tracebacks. (Process-global, test-only — product runtime keeps the default.)
logging.raiseExceptions = False


# Threads a test leaves running after teardown bleed into LATER tests' captured
# output (the "I/O operation on closed file" heisenbug) and, worse, can wedge
# the whole run (a leaked event loop / server that never stops). This grace
# lets a legitimately-finishing quick daemon settle before we judge a leak.
_THREAD_LEAK_GRACE = 5.0


@pytest.fixture(autouse=True)
def _no_leaked_threads(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test that leaves a background thread running past teardown.

    Snapshots the live threads at setup; at teardown, gives any NEW thread a
    short grace to finish, then fails listing those still alive — so a leak is
    caught here instead of as a heisenbug days later. Opt out with
    ``@pytest.mark.allow_thread_leak`` (e.g. module-scoped servers in the live
    suite).
    """
    if request.node.get_closest_marker("allow_thread_leak"):
        yield
        return
    # Snapshot the Thread OBJECTS, not their idents: Thread.ident is recycled
    # after a thread exits, so an ident-based snapshot could mistake a new
    # leaked thread (reusing an exited thread's ident) for a pre-existing one.
    before = set(threading.enumerate())
    yield
    main = threading.main_thread()
    current = threading.current_thread()
    # One deadline shared across all joined threads — a deliberate TOTAL
    # teardown budget (not per-thread), so a pathological test can't stall
    # teardown by N×grace. A genuine never-stopping leak exhausts it and fails.
    deadline = time.monotonic() + _THREAD_LEAK_GRACE
    leaked = []
    for t in threading.enumerate():
        if t in before or t is main or t is current or not t.is_alive():
            continue
        t.join(timeout=max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            leaked.append(t.name)
    if leaked:
        pytest.fail(
            f"test left background threads running after teardown: {leaked}. "
            "Stop them in teardown (shut down servers / close event loops / join "
            "threads), or mark @pytest.mark.allow_thread_leak if intentional."
        )


def make_mcp_token_cipher() -> MCPTokenCipher:
    """Build a single-key MCP token cipher for tests.

    Used by test files that need to exercise ``MCPTokenStore`` round-
    trips without the lifespan-side configuration loader; centralised
    here so the key/material defaults stay aligned across files.
    """
    import base64

    from cryptography.fernet import Fernet

    from turnstone.core.mcp_crypto import MCPTokenCipher, MCPTokenCipherConfig

    raw = base64.urlsafe_b64decode(Fernet.generate_key())
    return MCPTokenCipher(MCPTokenCipherConfig(keys=(raw,)))


def _seed_static_state(mgr: MCPClientManager, name: str, **overrides: Any) -> StaticServerState:
    """Get-or-create a ``StaticServerState`` on ``mgr`` and apply ``overrides``.

    Shared across MCP test files so the helper stays in one place. Imported
    where needed; ``StaticServerState`` is constructed lazily so non-MCP
    tests don't pay the import cost.
    """
    from turnstone.core.mcp_client import StaticServerState

    state = mgr._static_servers.get(name)
    if state is None:
        state = StaticServerState(name=name)
        mgr._static_servers[name] = state
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def make_oidc_test_config(**overrides: Any) -> OIDCConfig:
    """Build a test ``OIDCConfig`` with sensible defaults.

    Shared between ``test_oidc.py`` and ``test_oidc_handlers.py`` so the
    defaults (including the now-required ``redirect_base``) stay aligned.
    """
    from turnstone.core.oidc import OIDCConfig

    defaults: dict[str, Any] = {
        "enabled": True,
        "issuer": "https://idp.example.com",
        "client_id": "my-client",
        "client_secret": "my-secret",
        "scopes": "openid email profile",
        "provider_name": "TestIDP",
        "role_claim": "",
        "role_map": {},
        "password_enabled": True,
        "redirect_base": "https://app.example.com",
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
        "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--storage-backend",
        default="sqlite",
        choices=["sqlite", "postgresql"],
        help="Storage backend for integration tests (default: sqlite)",
    )


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary SQLite storage backend (singleton registry)."""
    from turnstone.core.storage import init_storage, reset_storage

    db_path = str(tmp_path / "test.db")
    reset_storage()
    init_storage("sqlite", path=db_path, run_migrations=False)
    yield db_path
    reset_storage()


@pytest.fixture
def storage_backend(request, tmp_path):
    """Shared storage backend fixture — respects --storage-backend flag.

    Returns a StorageBackend instance (SQLite or PostgreSQL).
    Tests that use this fixture run against whichever backend CI selects.
    """
    from turnstone.core.storage import init_storage, reset_storage

    backend_type = request.config.getoption("--storage-backend")
    reset_storage()

    if backend_type == "postgresql":
        pg_url = os.environ.get(
            "TURNSTONE_TEST_PG_URL",
            "postgresql+psycopg://postgres:postgres@localhost:5432/turnstone_test",
        )
        backend = init_storage("postgresql", url=pg_url, run_migrations=False)
        yield backend
        # Truncate all tables between tests — faster than DELETE and resets
        # autoincrement sequences.  CASCADE handles any future FK constraints.
        # NOTE: accesses backend._engine (SQLAlchemy internal) — both SQLite
        # and PostgreSQL backends expose this.  If a non-SQLAlchemy backend is
        # ever added, this cleanup will need a protocol-level hook.
        try:
            import sqlalchemy as sa

            from turnstone.core.storage._schema import metadata as db_metadata

            with backend._engine.connect() as conn:
                table_names = ", ".join(t.name for t in reversed(db_metadata.sorted_tables))
                conn.execute(sa.text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
                conn.commit()
        except Exception:
            pass  # best-effort cleanup; reset_storage disposes engine
        finally:
            reset_storage()
    else:
        db_path = str(tmp_path / "test.db")
        backend = init_storage("sqlite", path=db_path, run_migrations=False)
        yield backend
        reset_storage()


@pytest.fixture
def backend(storage_backend):
    """Alias for storage_backend — used by test_storage_sqlite.py etc."""
    return storage_backend


@pytest.fixture
def db(storage_backend):
    """Alias for storage_backend — used by domain-specific storage tests."""
    return storage_backend


@pytest.fixture
def storage(storage_backend):
    """Alias for storage_backend — used by services/skill resource tests."""
    return storage_backend


@pytest.fixture
def mock_openai_client():
    """Return a minimal mock OpenAI client."""
    client = MagicMock()
    client.models.list.return_value.data = [MagicMock(id="test-model")]
    return client


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    """Drop the in-process tool-policy cache between tests.

    The cache is keyed by org_id (default ``""``), so without this
    autouse hook a policy created in test A would leak into test B's
    ``evaluate_tool_policy`` call — distinct storage instances, same
    cache slot. Production singleton storage doesn't see the leak
    because there's only one storage instance for the process lifetime;
    the test isolation requirement is what motivates the autouse.
    """
    from turnstone.core.policy import invalidate_policy_cache

    invalidate_policy_cache()
    yield
    invalidate_policy_cache()
