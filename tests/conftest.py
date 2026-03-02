import pytest
from unittest.mock import MagicMock


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Provide a temporary SQLite database."""
    import turnstone.core.memory as memory

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(memory, "db_override", db_path)
    memory.db_initialized.discard(db_path)
    yield db_path
    memory.db_initialized.discard(db_path)


@pytest.fixture
def mock_openai_client():
    """Return a minimal mock OpenAI client."""
    client = MagicMock()
    client.models.list.return_value.data = [MagicMock(id="test-model")]
    return client
