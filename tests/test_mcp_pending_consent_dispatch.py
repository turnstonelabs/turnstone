"""Boundary tests for the Phase 9 pending-consent write path.

Drives ``MCPClientManager._dispatch_pool_sync`` (and the helper it
calls, ``_record_pending_consent_best_effort``) and asserts that
deferred-consent records reach storage only on non-interactive callers.

Per ``feedback_tests_through_boundaries.md``, at least one test must
drive the real sync dispatcher → real ``_is_structured_error`` →
real ``_record_pending_consent_best_effort`` plumb-through; the
``_helpers`` unit tests below cover the classifier in isolation, but
the end-to-end test is the structural gate that catches
plumb-through regressions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from typing import Any
from unittest.mock import patch

import pytest

from tests.conftest import make_mcp_token_cipher
from turnstone.core.mcp_client import (
    _PENDING_CONSENT_PERSIST_CODES,
    MCPClientManager,
    _parse_pending_consent_envelope,
)
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.mcp_oauth import TokenLookupResult

# ---------------------------------------------------------------------------
# Helper-level unit tests (cheap, no event loop)
# ---------------------------------------------------------------------------


class TestParseEnvelope:
    def test_consent_required_no_scopes(self) -> None:
        env = json.dumps({"error": {"code": "mcp_consent_required", "server": "x", "detail": "d"}})
        assert _parse_pending_consent_envelope(env) == ("mcp_consent_required", None)

    def test_insufficient_scope_with_scopes(self) -> None:
        env = json.dumps(
            {
                "error": {
                    "code": "mcp_insufficient_scope",
                    "server": "x",
                    "detail": "d",
                    "scopes_required": ["read", "write"],
                }
            }
        )
        assert _parse_pending_consent_envelope(env) == (
            "mcp_insufficient_scope",
            ["read", "write"],
        )

    def test_operator_codes_filtered(self) -> None:
        # Key-unknown / url-insecure / *_forbidden are operator-actionable,
        # NOT user-consent-shaped.  They must not produce pending-consent
        # rows, regardless of whether the caller is interactive.
        for code in (
            "mcp_token_undecryptable_key_unknown",
            "mcp_oauth_url_insecure",
            "mcp_tool_call_forbidden",
            "mcp_resource_read_forbidden",
            "mcp_prompt_get_forbidden",
        ):
            env = json.dumps({"error": {"code": code, "server": "x", "detail": "d"}})
            assert _parse_pending_consent_envelope(env) is None, code

    def test_malformed_json_returns_none(self) -> None:
        assert _parse_pending_consent_envelope("not json") is None
        assert _parse_pending_consent_envelope("") is None

    def test_persist_codes_set_is_expected(self) -> None:
        # Pin the contract — adding a new persistable code here is a
        # deliberate design decision and should require a test update.
        assert {
            "mcp_consent_required",
            "mcp_insufficient_scope",
        } == _PENDING_CONSENT_PERSIST_CODES


# ---------------------------------------------------------------------------
# End-to-end plumb-through (drives _dispatch_pool_sync)
# ---------------------------------------------------------------------------


def _seed_oauth_server(backend: Any, *, name: str = "pool-srv") -> None:
    backend.create_mcp_server(
        server_id="srv-" + name,
        name=name,
        transport="streamable-http",
        command="",
        args="[]",
        url="https://example.com/mcp",
        headers="{}",
        env="{}",
        auto_approve=False,
        enabled=True,
        created_by="admin",
    )
    backend.update_mcp_server("srv-" + name, auth_type="oauth_user")


@pytest.fixture
def running_loop_mgr():
    cfg: dict[str, Any] = {}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="phase9-test-loop")
    thread.start()
    mgr._loop = loop
    try:
        yield mgr, loop, thread
    finally:

        async def _drain(m: MCPClientManager) -> None:
            task = m._user_pool_eviction_task
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                m._user_pool_eviction_task = None

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(mgr), loop).result(timeout=2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def _wire_mgr(mgr: MCPClientManager, backend: Any) -> None:
    cipher = make_mcp_token_cipher()
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    app_state = SimpleNamespace(
        auth_storage=backend,
        mcp_token_store=MCPTokenStore(backend, cipher, node_id="test"),
        mcp_oauth_http_client=MagicMock(),
        mcp_oauth_refresh_locks={},
        mcp_oauth_metadata_cache={},
    )
    mgr.set_storage(backend)
    mgr.set_app_state(app_state)


def test_dispatch_persists_pending_for_non_interactive_caller(
    running_loop_mgr: Any, backend: Any
) -> None:
    """Non-interactive caller hits ``mcp_consent_required`` → a
    ``mcp_pending_consent`` row appears for ``(user_id, server_name)``."""
    mgr, _loop, _ = running_loop_mgr
    _seed_oauth_server(backend)
    _wire_mgr(mgr, backend)

    async def _missing_token(**kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="missing")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_missing_token,
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        mgr.call_tool_sync(
            "mcp__pool-srv__echo",
            {"payload": "hi"},
            user_id="user-a",
            timeout=10,
            is_interactive_for_consent=False,
        )

    # Structured error envelope surfaces as RuntimeError to the caller.
    payload = json.loads(str(exc_info.value)).get("error", {})
    assert payload.get("code") == "mcp_consent_required"

    # Persistent row written for the dashboard badge.
    rows = backend.list_mcp_pending_consent_by_user("user-a")
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == "user-a"
    assert r["server_name"] == "pool-srv"
    assert r["error_code"] == "mcp_consent_required"
    assert r["occurrence_count"] == 1


def test_dispatch_does_not_persist_for_interactive_caller(
    running_loop_mgr: Any, backend: Any
) -> None:
    """Interactive caller hits the same error path → NO row written.

    Interactive (WEB / CLI) sessions surface the consent prompt in-flight
    via the Phase 8 SSE renderer; persisting would just produce
    immediately-stale dashboard badges.
    """
    mgr, _loop, _ = running_loop_mgr
    _seed_oauth_server(backend)
    _wire_mgr(mgr, backend)

    async def _missing_token(**kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="missing")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_missing_token,
        ),
        pytest.raises(RuntimeError),
    ):
        mgr.call_tool_sync(
            "mcp__pool-srv__echo",
            {"payload": "hi"},
            user_id="user-a",
            timeout=10,
            is_interactive_for_consent=True,
        )

    assert backend.list_mcp_pending_consent_by_user("user-a") == []


def test_dispatch_returns_envelope_unchanged_on_storage_failure(
    running_loop_mgr: Any, backend: Any
) -> None:
    """When ``upsert_mcp_pending_consent`` raises, the agent-observable
    contract is unchanged: the structured-error ``RuntimeError`` still
    surfaces with the original ``mcp_consent_required`` code.  The doc-
    string promises best-effort persistence; this test pins that
    promise so a regression that propagates the storage exception would
    fail visibly.
    """
    mgr, _loop, _ = running_loop_mgr
    _seed_oauth_server(backend)
    _wire_mgr(mgr, backend)

    async def _missing_token(**kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="missing")

    original_upsert = backend.upsert_mcp_pending_consent

    def _raise(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("storage offline")

    backend.upsert_mcp_pending_consent = _raise  # type: ignore[method-assign]
    try:
        with (
            patch(
                "turnstone.core.mcp_client.get_user_access_token_classified",
                side_effect=_missing_token,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mgr.call_tool_sync(
                "mcp__pool-srv__echo",
                {"payload": "hi"},
                user_id="user-a",
                timeout=10,
                is_interactive_for_consent=False,
            )
    finally:
        backend.upsert_mcp_pending_consent = original_upsert  # type: ignore[method-assign]

    payload = json.loads(str(exc_info.value)).get("error", {})
    assert payload.get("code") == "mcp_consent_required"


def test_dispatch_does_not_persist_for_operator_actionable_code(
    running_loop_mgr: Any, backend: Any
) -> None:
    """Decrypt-failure → operator-actionable; even non-interactive callers
    must NOT produce a user-facing pending-consent record (the user can't
    resolve this by re-consenting).
    """
    mgr, _loop, _ = running_loop_mgr
    _seed_oauth_server(backend)
    _wire_mgr(mgr, backend)

    async def _decrypt_failure(**kwargs: Any) -> TokenLookupResult:
        return TokenLookupResult(kind="decrypt_failure")

    with (
        patch(
            "turnstone.core.mcp_client.get_user_access_token_classified",
            side_effect=_decrypt_failure,
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        mgr.call_tool_sync(
            "mcp__pool-srv__echo",
            {"payload": "hi"},
            user_id="user-a",
            timeout=10,
            is_interactive_for_consent=False,
        )

    payload = json.loads(str(exc_info.value)).get("error", {})
    assert payload.get("code") == "mcp_token_undecryptable_key_unknown"
    # The operator-actionable code does NOT produce a pending-consent row.
    assert backend.list_mcp_pending_consent_by_user("user-a") == []
