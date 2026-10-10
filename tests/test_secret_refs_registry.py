"""Model registry: ``secret://`` api_key references are materialised at load, per alias."""

from __future__ import annotations

from typing import Any

import pytest

from tests._secret_refs_helpers import FakeBackend, install_fake_resolver
from turnstone.core.model_registry import load_model_registry, materialize_api_key
from turnstone.core.secret_refs import SecretReferenceError, invalidate_cache
from turnstone.core.secret_refs._errors import SecretBackendError

VALUES = {"/openai": "sk-from-store", "/other": "sk-other", "/third": "sk-third"}


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
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    return install_fake_resolver(monkeypatch, VALUES)


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

    def test_outage_keeps_resolved_keys_and_skips_only_unresolved_references(
        self, fake_store: FakeBackend
    ) -> None:
        rows = [_row("a", "secret://fake/openai"), _row("b", "secret://fake/other")]
        load_model_registry(storage=_MockStorage(rows), strict=True)
        # A reload asks the store again; the store is down. The values fetched
        # before stay in use, whatever endpoint the row now names; a reference
        # this process never resolved is skipped with a retryable reason.
        invalidate_cache()
        fake_store.fail = SecretBackendError("store unavailable", retryable=True)
        rows[0]["base_url"] = "https://llm-gw.internal/v1"
        rows.append(_row("fresh", "secret://fake/third"))
        registry = load_model_registry(storage=_MockStorage(rows), strict=True)
        assert registry.get_config("a").api_key == "sk-from-store"
        assert registry.get_config("b").api_key == "sk-other"
        assert registry.list_aliases() == ["a", "b"]
        assert "store unavailable" in registry.skipped_aliases["fresh"]

    def test_every_row_skipped_gives_an_empty_registry_that_names_them(
        self, fake_store: FakeBackend
    ) -> None:
        storage = _MockStorage([_row("a", "secret://fake/missing")])
        registry = load_model_registry(storage=storage, allow_empty=True)
        assert registry.list_aliases() == []
        assert set(registry.skipped_aliases) == {"a"}
        # A host that needs models says why there are none.
        with pytest.raises(
            ValueError, match="Skipped over their api_key reference: a: .*not found"
        ):
            load_model_registry(storage=storage)

    def test_config_toml_entry_reference(
        self, fake_store: FakeBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core import model_registry as mr

        entries = {
            "models": {
                "c": {"model": "m", "provider": "openai", "api_key": "secret://fake/openai"},
                "d": {"model": "m", "provider": "openai", "api_key": "secret://fake/missing"},
                "e": {"model": "m", "provider": "openai", "api_key": "sk-config-literal"},
            }
        }
        monkeypatch.setattr(mr, "load_config", lambda section=None: entries)
        storage = _MockStorage([_row("d", "sk-db-literal"), _row("e", "secret://fake/missing")])
        registry = load_model_registry(storage=storage)
        assert registry.get_config("c").api_key == "sk-from-store"
        # config.toml has the last word on an alias it names: when its reference
        # fails the alias is skipped outright, and the database row of the same
        # alias does not take over.
        assert "d" not in registry.list_aliases()
        assert "d" in registry.skipped_aliases
        # The other way round the alias is served, so it is not reported skipped.
        assert registry.get_config("e").api_key == "sk-config-literal"
        assert "e" not in registry.skipped_aliases

    def test_one_fetch_per_distinct_reference(self, fake_store: FakeBackend) -> None:
        storage = _MockStorage(
            [_row("a", "secret://fake/openai"), _row("b", "secret://fake/openai")]
        )
        load_model_registry(storage=storage)
        assert fake_store.calls == 1
