"""Exercise the extracted protocol boundary and shared consumer coordination."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs

import httpx
import pytest

from tests._oauth_runtime_helpers import make_oauth_context
from tests._oidc_test_helpers import ISSUER, make_oidc_config, mint_warn_state_reset
from tests.conftest import make_mcp_token_cipher
from turnstone.core import mcp_oauth, model_oauth
from turnstone.core.oauth import locking
from turnstone.core.token_store.store import TokenStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from turnstone.core.oauth.context import TokenCoordination
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.core.storage._sqlite import SQLiteBackend


def test_protocol_and_store_work_without_loading_consumers() -> None:
    script = textwrap.dedent("""\
        import asyncio
        import dataclasses
        import importlib.abc
        import os
        import pkgutil
        import sys
        from unittest.mock import patch

        class RejectConsumers(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith((
                    "turnstone.core.mcp_", "turnstone.core.model_",
                    "turnstone.core.session", "turnstone.core.oidc",
                    "turnstone.server", "turnstone.console",
                )):
                    raise AssertionError("shared OAuth imported consumer: " + fullname)

        sys.meta_path.insert(0, RejectConsumers())
        from turnstone.core.token_store.crypto import TokenCipher, TokenCipherConfig
        from turnstone.core.token_store.store import TokenStore
        cipher = TokenCipher(TokenCipherConfig(keys=(bytes(range(32)),)))
        assert cipher.decrypt(cipher.encrypt(b"credential")) == b"credential"
        assert not any(name.startswith("turnstone.core.oauth") for name in sys.modules)

        import httpx
        from turnstone.core import oauth
        shared_modules = {
            info.name for info in pkgutil.walk_packages(oauth.__path__, oauth.__name__ + ".")
        }
        for name in sorted(shared_modules):
            importlib.import_module(name)
        assert shared_modules <= sys.modules.keys()
        from turnstone.core.oauth import grants, http, oidc
        config = {
            "issuer": "https://idp.example.com", "client_id": "client",
            "client_secret": "secret", "obo_grant_profile": "rfc8693",
        }
        with patch.dict(os.environ, {}, clear=True), patch(
            "turnstone.core.config.load_config", return_value=config
        ):
            cfg = oidc.load_oidc_config()
        assert cfg.enabled and cfg.obo_grant_profile == "rfc8693"
        assert cfg.obo_grant_profile in grants.OBO_GRANT_PROFILES
        cfg = dataclasses.replace(cfg, token_endpoint="https://idp.example.com/token")
        rotations = []
        calls = []

        async def persist(value):
            rotations.append(value)

        def request(req):
            calls.append(req)
            if len(calls) == 1:
                return httpx.Response(200, json={"access_token": "subject", "refresh_token": "new"})
            assert rotations == ["new"], "rotation must persist before exchange"
            return httpx.Response(400, json={"error": "invalid_grant"})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
                try:
                    await grants._obo_mint_rfc8693(
                        oidc_config=cfg, credential_refresh_token="old", audience="resource",
                        scopes="read", http_client=client, persist_rotation=persist,
                    )
                except http.OAuthRefreshError as exc:
                    assert exc.failure_class is http.RefreshFailureClass.PERMANENT
                else:
                    raise AssertionError("the rejected exchange succeeded")

        asyncio.run(run())
        assert len(calls) == 2 and rotations == ["new"]
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _assert_shared_credential_rotation(backend: StorageBackend) -> None:
    """Both public adapters must redeem the latest credential under one local lock."""
    backend.create_mcp_server(
        server_id="shared-rotation",
        name="rotation-server",
        transport="streamable-http",
        url="https://mcp.example.com/mcp",
        auth_type="oauth_obo",
        oauth_audience="api://mcp",
    )
    store = TokenStore(backend, make_mcp_token_cipher())
    store.upsert_oidc_credential("u1", ISSUER, refresh_token="rt-1")

    async def run() -> None:
        entered = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        redemptions: list[str] = []

        class ObservedLock(asyncio.Lock):
            async def acquire(self) -> Literal[True]:
                if self.locked():
                    waiting.set()
                return await super().acquire()

        async def request(req: httpx.Request) -> httpx.Response:
            form = parse_qs(req.content.decode())
            redemptions.append(form["refresh_token"][0])
            count = len(redemptions)
            if count == 1:
                entered.set()
                assert await asyncio.to_thread(release.wait, 5)
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{count}",
                    "refresh_token": f"rt-{count + 1}",
                    "expires_in": 300,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
            state = SimpleNamespace(
                auth_storage=backend,
                oauth_context=make_oauth_context(
                    token_store=store, oidc_config=make_oidc_config(), http_client=client
                ),
            )

            async def install_lock() -> None:
                state.oauth_context.coordination.locks[("u1", f"__obo__:{ISSUER}")] = ObservedLock()

            state.oauth_context.runtime.call_sync(install_lock)
            mcp_task = asyncio.create_task(
                mcp_oauth.get_obo_access_token_classified(
                    app_state=state, user_id="u1", server_name="rotation-server"
                )
            )
            tasks: list[asyncio.Task[object]] = [mcp_task]
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                model_task = asyncio.create_task(
                    model_oauth.mint_obo_access_token(
                        app_state=state,
                        user_id="u1",
                        alias="rotation-model",
                        audience="api://model",
                        grant_leg="entra",
                    )
                )
                tasks.append(model_task)
                async with asyncio.timeout(3):
                    while not waiting.is_set() and len(redemptions) < 2:
                        await asyncio.sleep(0)
                assert redemptions == ["rt-1"], "concurrent credential redemption"
                assert waiting.is_set()
                release.set()
                mcp_result, model_result = await asyncio.wait_for(
                    asyncio.gather(mcp_task, model_task), timeout=10
                )
                assert mcp_result.kind == "token" and mcp_result.token == "at-1"
                assert model_result == "at-2"
                assert redemptions == ["rt-1", "rt-2"]
                credential = store.get_oidc_credential("u1", ISSUER)
                assert credential is not None and credential["refresh_token"] == "rt-3"
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    reset = mint_warn_state_reset()
    next(reset)
    try:
        asyncio.run(run())
    finally:
        next(reset, None)


def test_mcp_and_model_mints_share_credential_rotation(backend: StorageBackend) -> None:
    _assert_shared_credential_rotation(backend)


def test_split_local_locks_fail_credential_rotation_control(
    tmp_path: Path,
    sqlite_backend_factory: Callable[..., SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL must not conceal a broken local-lock invariant in this control."""
    backend = sqlite_backend_factory(str(tmp_path / "split-locks.db"))
    original = locking._refresh_lock_for

    def split(state: TokenCoordination, user: str, key: str) -> asyncio.Lock:
        if key == f"__obo__:{ISSUER}":
            return asyncio.Lock()
        return original(state, user, key)

    with monkeypatch.context() as patch:
        patch.setattr(locking, "_refresh_lock_for", split)
        with pytest.raises(AssertionError, match="concurrent credential redemption"):
            _assert_shared_credential_rotation(backend)


def _assert_independent_postgresql_rotation(backend: StorageBackend) -> None:
    """Separate loop-local maps and connections must agree on the advisory key."""
    import sqlalchemy as sa

    from turnstone.core.model_oauth import OAuthModelTokenClient
    from turnstone.core.storage._postgresql import PostgreSQLBackend

    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("independent processes require PostgreSQL advisory coordination")
    backend.create_mcp_server(
        server_id="shared-pg",
        name="rotation-server",
        transport="streamable-http",
        url="https://mcp.example.com/mcp",
        auth_type="oauth_obo",
        oauth_audience="api://mcp",
    )
    cipher = make_mcp_token_cipher()
    first_store = TokenStore(backend, cipher)
    first_store.upsert_oidc_credential("u1", ISSUER, refresh_token="rt-1")
    other = PostgreSQLBackend(
        backend._engine.url.render_as_string(hide_password=False), create_tables=False
    )
    entered, release, contended = threading.Event(), threading.Event(), threading.Event()
    redemptions: list[str] = []
    attempts = 0

    def observe_probe(conn, cursor, statement, parameters, execution_context, executemany):
        nonlocal attempts
        if "pg_try_advisory_xact_lock" in statement:
            attempts += 1
            if attempts >= 2:
                contended.set()  # a second probe proves the first did not acquire

    sa.event.listen(other._engine, "after_cursor_execute", observe_probe)

    async def request(req: httpx.Request) -> httpx.Response:
        redemptions.append(parse_qs(req.content.decode())["refresh_token"][0])
        count = len(redemptions)
        if count == 1:
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        return httpx.Response(
            200,
            json={
                "access_token": f"at-{count}",
                "refresh_token": f"rt-{count + 1}",
                "expires_in": 300,
            },
        )

    first = make_oauth_context(
        storage=backend,
        token_store=first_store,
        oidc_config=make_oidc_config(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(request)),
    )
    second = make_oauth_context(
        storage=other,
        token_store=TokenStore(other, cipher),
        oidc_config=make_oidc_config(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(request)),
    )
    assert first.runtime is not second.runtime
    assert first.coordination is not second.coordination

    async def run() -> None:
        mcp = asyncio.create_task(
            mcp_oauth.get_obo_access_token_classified(
                app_state=SimpleNamespace(oauth_context=first),
                user_id="u1",
                server_name="rotation-server",
            )
        )
        model: asyncio.Task[str | None] | None = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            model = asyncio.create_task(
                asyncio.to_thread(
                    OAuthModelTokenClient(second).mint_model_obo_token_sync,
                    user_id="u1",
                    alias="pg-model",
                    audience="api://model",
                    grant_leg="entra",
                )
            )
            async with asyncio.timeout(3):
                while not contended.is_set() and len(redemptions) < 2:
                    await asyncio.sleep(0)
            assert redemptions == ["rt-1"], "concurrent credential redemption"
            assert contended.is_set(), "second runtime never observed advisory contention"
            release.set()
            mcp_result, model_result = await asyncio.wait_for(asyncio.gather(mcp, model), timeout=5)
            assert mcp_result.kind == "token" and model_result == "at-2"
            assert redemptions == ["rt-1", "rt-2"]
            assert first_store.get_oidc_credential("u1", ISSUER)["refresh_token"] == "rt-3"
        finally:
            release.set()
            await asyncio.gather(mcp, *([model] if model else []), return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        first.runtime.shutdown()
        second.runtime.shutdown()
        other.close()


def test_independent_runtimes_share_postgresql_credential_lock(backend: StorageBackend) -> None:
    _assert_independent_postgresql_rotation(backend)


def test_divergent_advisory_keys_fail_credential_rotation_control(
    backend: StorageBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = locking._refresh_advisory_key

    def divergent(user: str, key: str) -> str:
        return f"{original(user, key)}:{threading.get_ident()}"

    with monkeypatch.context() as patch:
        patch.setattr(locking, "_refresh_advisory_key", divergent)
        with pytest.raises(AssertionError, match="concurrent credential redemption"):
            _assert_independent_postgresql_rotation(backend)
