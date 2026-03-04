from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary SQLite storage backend."""
    from turnstone.core.storage import init_storage, reset_storage

    db_path = str(tmp_path / "test.db")
    reset_storage()
    init_storage("sqlite", path=db_path, run_migrations=False)
    yield db_path
    reset_storage()


@pytest.fixture
def mock_openai_client():
    """Return a minimal mock OpenAI client."""
    client = MagicMock()
    client.models.list.return_value.data = [MagicMock(id="test-model")]
    return client
