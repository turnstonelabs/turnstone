from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from turnstone.core.mcp_client import MCPClientManager, StaticServerState
    from turnstone.core.mcp_crypto import MCPTokenCipher
    from turnstone.core.oidc import OIDCConfig


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
