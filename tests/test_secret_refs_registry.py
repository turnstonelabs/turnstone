"""Model registry: ``secret://`` api_key references are materialised at load, per alias."""

from __future__ import annotations

from typing import Any

import pytest

from turnstone.core.model_registry import (
    ModelConfig,
    load_model_registry,
    materialize_api_key,
)
from turnstone.core.secret_refs import SecretReferenceError, SecretResolver, reset_for_tests
from turnstone.core.secret_refs import _resolver as resolver_module
from turnstone.core.secret_refs._config import parse_secrets_config
from turnstone.core.secret_refs._errors import SecretBackendError


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.values: dict[str, str] = {"/openai": "sk-from-store", "/other": "sk-other"}
        self.fail: SecretBackendError | None = None
        self.calls = 0

    def fetch(self, path: str, key: str | None) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if path not in self.values:
            raise SecretBackendError(f"{path.strip('/')!r} not found", retryable=False)
        return self.values[path]

    def close(self) -> None:
        pass


class _MockStorage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def list_model_definitions(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return (
            [r for r in self._rows if r.get("enabled", True)] if enabled_only else list(self._rows)
        )


def _row(alias: str, api_key: str) -> dict[str, Any]:
    return {
        "alias": alias,
        "model": "gpt-5",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": api_key,
        "context_window": 8192,
        "capabilities": "{}",
        "enabled": True,
    }


@pytest.fixture(autouse=True)
def _no_host_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import turnstone.core.config as cfg_mod
    from turnstone.core import model_registry as mr

    monkeypatch.setattr(cfg_mod, "load_config", lambda section=None: {})
    monkeypatch.setattr(mr, "load_config", lambda section=None: {})


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = FakeBackend()
    resolver = SecretResolver(parse_secrets_config({}), backends={"fake": backend})
    monkeypatch.setattr(resolver_module, "_instance", resolver)
    yield backend
    reset_for_tests()


class TestMaterializeApiKey:
    def test_literal_keeps_env_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TS_TEST_KEY", "from-env")
        assert materialize_api_key("sk-${TS_TEST_KEY}") == "sk-from-env"
        assert materialize_api_key("plain") == "plain"
        assert materialize_api_key("") == ""

    def test_reference_resolves_without_env_expansion(
        self, fake_store: FakeBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEAM", "openai")
        assert materialize_api_key("secret://fake/openai") == "sk-from-store"
        with pytest.raises(SecretReferenceError, match="not expanded inside a reference"):
            materialize_api_key("secret://fake/${TEAM}")


class TestLoadWithReferences:
    def test_db_row_reference_becomes_plaintext_in_the_registry(
        self, fake_store: FakeBackend
    ) -> None:
        storage = _MockStorage([_row("a", "secret://fake/openai"), _row("b", "sk-literal")])
        registry = load_model_registry(storage=storage)
        assert registry.get_config("a").api_key == "sk-from-store"
        assert registry.get_config("b").api_key == "sk-literal"
        assert registry.skipped_aliases == {}

    def test_definitive_failure_drops_only_that_alias(self, fake_store: FakeBackend) -> None:
        storage = _MockStorage([_row("bad", "secret://fake/missing"), _row("ok", "sk-literal")])
        registry = load_model_registry(storage=storage, strict=True)
        assert registry.list_aliases() == ["ok"]
        assert "bad" in registry.skipped_aliases
        assert "not found" in registry.skipped_aliases["bad"]
        assert "secret://fake/missing" in registry.skipped_aliases["bad"]

    def test_retryable_failure_carries_the_prior_key_forward(self, fake_store: FakeBackend) -> None:
        fake_store.fail = SecretBackendError("store unavailable", retryable=True)
        prior = {
            "a": ModelConfig(
                alias="a",
                base_url="https://api.openai.com/v1",
                api_key="sk-previous",
                model="gpt-5",
            )
        }
        storage = _MockStorage(
            [_row("a", "secret://fake/openai"), _row("new", "secret://fake/other")]
        )
        registry = load_model_registry(storage=storage, strict=True, prior=prior)
        assert registry.get_config("a").api_key == "sk-previous"
        assert registry.list_aliases() == ["a"]
        assert "new" in registry.skipped_aliases  # no prior value to fall back on

    def test_every_row_skipped_gives_an_empty_registry_that_names_them(
        self, fake_store: FakeBackend
    ) -> None:
        storage = _MockStorage([_row("a", "secret://fake/missing")])
        registry = load_model_registry(storage=storage, allow_empty=True)
        assert registry.list_aliases() == []
        assert set(registry.skipped_aliases) == {"a"}

    def test_config_toml_entry_reference(
        self, fake_store: FakeBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core import model_registry as mr

        entries = {
            "models": {
                "c": {"model": "m", "provider": "openai", "api_key": "secret://fake/openai"},
                "d": {"model": "m", "provider": "openai", "api_key": "secret://fake/missing"},
            }
        }
        monkeypatch.setattr(mr, "load_config", lambda section=None: entries)
        storage = _MockStorage([_row("d", "sk-db-literal")])
        registry = load_model_registry(storage=storage)
        assert registry.get_config("c").api_key == "sk-from-store"
        # The failing config entry is skipped and the database row of the same
        # alias stays in force.
        assert registry.get_config("d").api_key == "sk-db-literal"
        assert "d" in registry.skipped_aliases

    def test_one_fetch_per_distinct_reference(self, fake_store: FakeBackend) -> None:
        storage = _MockStorage(
            [_row("a", "secret://fake/openai"), _row("b", "secret://fake/openai")]
        )
        load_model_registry(storage=storage)
        assert fake_store.calls == 1
