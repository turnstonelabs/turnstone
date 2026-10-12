"""Model registry: ``secret://`` api_key references are materialised at load, per alias."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from tests._secret_refs_helpers import FakeBackend, install_fake_resolver
from turnstone.core.http_headers import HeaderValueError
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
        "base_url": "https://llm.example.com/v1",
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

    def test_literal_drops_one_trailing_line_break(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A variable filled from a file keeps the file's line break, which is not part of the key.
        monkeypatch.setenv("TS_TEST_KEY", "sk-from-file" + chr(10))
        assert materialize_api_key("${TS_TEST_KEY}") == "sk-from-file"
        assert materialize_api_key("sk-literal" + chr(13) + chr(10)) == "sk-literal"

    @pytest.mark.parametrize(
        "value",
        [
            "sk-padded ",
            "sk-two-breaks" + chr(10) * 2,
            "sk-inner" + chr(10) + "break",
            "sk-caf" + chr(0xE9),
        ],
        ids=["space", "two-breaks", "inner-break", "non-ascii"],
    )
    def test_literal_a_header_cannot_carry_is_refused_unquoted(self, value: str) -> None:
        with pytest.raises(HeaderValueError, match="cannot carry") as excinfo:
            materialize_api_key(value)
        assert value.strip() not in str(excinfo.value)

    def test_literal_that_is_only_a_line_break_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empty once the line break is dropped, it would select the SDK's own variable instead.
        monkeypatch.setenv("TS_TEST_KEY", chr(13) + chr(10))
        for value in (chr(10), "${TS_TEST_KEY}"):
            with pytest.raises(HeaderValueError, match="^api_key is a value an HTTP header"):
                materialize_api_key(value)
        assert materialize_api_key("") == ""

    def test_literal_that_is_not_text_is_refused(self) -> None:
        # A config.toml key written without quotes.
        with pytest.raises(HeaderValueError, match="^api_key must be a string$"):
            materialize_api_key(12345)

    def test_literal_a_variable_empties_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The empty key would select the provider SDK's own variable, a different credential.
        monkeypatch.delenv("TS_UNSET_KEY", raising=False)
        monkeypatch.setenv("TS_EMPTY_KEY", "")
        for value in ("${TS_UNSET_KEY}", "${TS_EMPTY_KEY}"):
            with pytest.raises(HeaderValueError, match="^api_key expands to nothing"):
                materialize_api_key(value)


class TestLoadWithLiteralKeys:
    def test_a_literal_key_a_header_cannot_carry_skips_its_alias(self) -> None:
        storage = _MockStorage([_row("bad", "sk-padded "), _row("ok", "sk-from-file" + chr(10))])
        registry = load_model_registry(storage=storage, strict=True)
        assert registry.list_aliases() == ["ok"]
        assert registry.get_config("ok").api_key == "sk-from-file"
        assert "cannot carry" in registry.skipped_aliases["bad"]
        assert "sk-padded" not in registry.skipped_aliases["bad"]

    def test_config_toml_keys_follow_the_same_rule(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from turnstone.core import model_registry as mr

        monkeypatch.setenv("TS_TEST_KEY", "sk-from-file" + chr(10))
        entries = {
            "models": {
                "env": {"model": "m", "provider": "openai", "api_key": "${TS_TEST_KEY}"},
                "bad": {"model": "m", "provider": "openai", "api_key": "sk-padded "},
                "unquoted": {"model": "m", "provider": "openai", "api_key": 12345},
                "unset": {"model": "m", "provider": "openai", "api_key": "${TS_UNSET_KEY}"},
            }
        }
        monkeypatch.delenv("TS_UNSET_KEY", raising=False)
        monkeypatch.setattr(mr, "load_config", lambda section=None: entries)
        with caplog.at_level(logging.ERROR):
            registry = load_model_registry(storage=_MockStorage([]), allow_empty=True)
        assert registry.list_aliases() == ["env"]
        assert registry.get_config("env").api_key == "sk-from-file"
        assert "cannot carry" in registry.skipped_aliases["bad"]
        assert registry.skipped_aliases["unquoted"] == "api_key must be a string"
        assert registry.skipped_aliases["unset"].startswith("api_key expands to nothing")
        # The file and the environment are read once per process: Sync would refuse it again.
        skips = [r.getMessage() for r in caplog.records if "alias_skipped" in r.getMessage()]
        assert len(skips) == 3 and all("then restart" in m for m in skips)


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

    def test_a_key_a_header_cannot_carry_skips_its_alias(self, fake_store: FakeBackend) -> None:
        # Sent, the SDK would fail every request with a bare connection error.
        fake_store.values["/padded"] = "sk-padded "
        storage = _MockStorage([_row("bad", "secret://fake/padded"), _row("ok", "sk-literal")])
        registry = load_model_registry(storage=storage, strict=True)
        assert registry.list_aliases() == ["ok"]
        assert "cannot carry" in registry.skipped_aliases["bad"]
        assert "sk-padded" not in registry.skipped_aliases["bad"]

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

    def test_cli_does_not_stand_in_for_skipped_models(
        self, fake_store: FakeBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core import model_registry as mr

        entries = {
            "models": {
                "gw": {
                    "model": "m",
                    "provider": "openai",
                    "base_url": "https://llm-gw.example.com/v1",
                    "api_key": "secret://fake/missing",
                }
            }
        }
        monkeypatch.setattr(mr, "load_config", lambda section=None: entries)
        # The CLI always passes its --base-url/--model; with every configured
        # model skipped it must fail and name them, not serve a "default" alias.
        with pytest.raises(ValueError, match="Skipped: gw"):
            load_model_registry(
                base_url="http://localhost:8000/v1", api_key="dummy", model="local-model"
            )

    def test_every_row_skipped_gives_an_empty_registry_that_names_them(
        self, fake_store: FakeBackend
    ) -> None:
        storage = _MockStorage([_row("a", "secret://fake/missing")])
        registry = load_model_registry(storage=storage, allow_empty=True)
        assert registry.list_aliases() == []
        assert set(registry.skipped_aliases) == {"a"}
        # A host that needs models says why there are none.
        with pytest.raises(ValueError, match="Skipped: a: .*not found"):
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
