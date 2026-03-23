from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


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
