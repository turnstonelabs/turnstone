"""Exercise the extracted protocol boundary and shared consumer coordination."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs

import httpx

from tests._oidc_test_helpers import ISSUER, make_oidc_config, mint_warn_state_reset
from tests.conftest import make_mcp_token_cipher
from turnstone.core import mcp_oauth, model_oauth
from turnstone.core.token_store.store import TokenStore

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend


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


def test_mcp_and_model_mints_share_credential_rotation(backend: StorageBackend) -> None:
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
        entered = asyncio.Event()
        release = asyncio.Event()
        waiting = asyncio.Event()
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
                await release.wait()
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
                mcp_token_store=store,
                oidc_config=make_oidc_config(),
                obo_http_client=client,
                mcp_oauth_refresh_locks={("u1", f"__obo__:{ISSUER}"): ObservedLock()},
            )
            mcp_task = asyncio.create_task(
                mcp_oauth.get_obo_access_token_classified(
                    app_state=state, user_id="u1", server_name="rotation-server"
                )
            )
            tasks: list[asyncio.Task[object]] = [mcp_task]
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
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
                await asyncio.wait_for(waiting.wait(), timeout=5)
                assert redemptions == ["rt-1"]
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
