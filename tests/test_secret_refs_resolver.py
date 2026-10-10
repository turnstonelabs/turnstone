"""The resolver: cache, stale serving, validation and the process-wide instance."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from turnstone.core.secret_refs import (
    SecretConfigError,
    SecretReferenceError,
    SecretResolveError,
    SecretResolver,
    get_resolver,
    has_reference,
    invalidate_cache,
    load_secrets_config,
    reset_for_tests,
    resolve,
    resolve_mapping,
    validate_reference,
)
from turnstone.core.secret_refs._config import parse_secrets_config
from turnstone.core.secret_refs._errors import SecretBackendError

if TYPE_CHECKING:
    from pathlib import Path


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.values: dict[str, str] = {"/a": "alpha", "/empty": ""}
        self.fail: SecretBackendError | None = None
        self.calls = 0
        self.closed = False

    def fetch(self, path: str, key: str | None) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if path not in self.values:
            raise SecretBackendError(f"{path}: not found", retryable=False)
        return self.values[path]

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _resolver(backend: FakeBackend, clock: FakeClock, ttl: float = 300.0) -> SecretResolver:
    cfg = parse_secrets_config({"cache_ttl_seconds": ttl})
    return SecretResolver(cfg, backends={"fake": backend}, clock=clock)


class TestResolver:
    def test_literal_passes_through_without_a_fetch(self) -> None:
        backend, clock = FakeBackend(), FakeClock()
        r = _resolver(backend, clock)
        assert r.resolve("sk-literal") == "sk-literal"
        assert r.resolve("") == ""
        assert backend.calls == 0

    def test_reference_is_cached_for_the_ttl(self) -> None:
        backend, clock = FakeBackend(), FakeClock()
        r = _resolver(backend, clock, ttl=60)
        assert r.resolve("secret://fake/a") == "alpha"
        backend.values["/a"] = "beta"
        clock.now = 59
        assert r.resolve("secret://fake/a") == "alpha"
        clock.now = 61
        assert r.resolve("secret://fake/a") == "beta"
        assert backend.calls == 2

    def test_retryable_failure_serves_the_last_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend, clock = FakeBackend(), FakeClock()
        r = _resolver(backend, clock, ttl=10)
        assert r.resolve("secret://fake/a") == "alpha"
        clock.now = 11
        backend.fail = SecretBackendError("store unavailable", retryable=True, detail="HTTP 503")
        with caplog.at_level(logging.WARNING, logger="turnstone.core.secret_refs._resolver"):
            assert r.resolve("secret://fake/a") == "alpha"
        assert "secret_refs.resolve_failed_serving_cached" in caplog.text

    def test_retryable_failure_without_a_cached_value_raises(self) -> None:
        backend, clock = FakeBackend(), FakeClock()
        backend.fail = SecretBackendError("store unavailable", retryable=True)
        with pytest.raises(SecretResolveError, match="secret://fake/a: store unavailable"):
            _resolver(backend, clock).resolve("secret://fake/a")

    def test_definitive_failure_raises_and_drops_the_entry(self) -> None:
        backend, clock = FakeBackend(), FakeClock()
        r = _resolver(backend, clock, ttl=10)
        assert r.resolve("secret://fake/a") == "alpha"
        clock.now = 11
        backend.fail = SecretBackendError("permission denied", retryable=False)
        with pytest.raises(SecretResolveError, match="permission denied"):
            r.resolve("secret://fake/a")
        backend.fail = SecretBackendError("store unavailable", retryable=True)
        with pytest.raises(SecretResolveError):  # nothing cached any more
            r.resolve("secret://fake/a")

    def test_empty_value_is_an_error(self) -> None:
        backend, clock = FakeBackend(), FakeClock()
        with pytest.raises(SecretResolveError, match="empty value"):
            _resolver(backend, clock).resolve("secret://fake/empty")

    def test_unconfigured_backend(self) -> None:
        r = _resolver(FakeBackend(), FakeClock())
        with pytest.raises(SecretReferenceError, match="backend 'vault' is not configured"):
            r.resolve("secret://vault/x#k")
        with pytest.raises(SecretReferenceError):
            r.validate("secret://vault/x#k")
        r.validate("secret://fake/a")

    def test_backend_level_reference_error_becomes_resolve_error(self) -> None:
        class Picky(FakeBackend):
            def fetch(self, path: str, key: str | None) -> str:
                raise SecretReferenceError("needs a field")

        r = SecretResolver(parse_secrets_config({}), backends={"fake": Picky()}, clock=FakeClock())
        with pytest.raises(SecretResolveError, match="needs a field"):
            r.resolve("secret://fake/a")

    def test_resolve_mapping_copies_non_references(self) -> None:
        r = _resolver(FakeBackend(), FakeClock())
        out = r.resolve_mapping({"Authorization": "secret://fake/a", "X-Plain": "v", "n": 3})
        assert out == {"Authorization": "alpha", "X-Plain": "v", "n": 3}

    def test_invalidate_and_close(self) -> None:
        backend, clock = FakeBackend(), FakeClock()
        r = _resolver(backend, clock)
        r.resolve("secret://fake/a")
        r.invalidate("secret://fake/a")
        r.resolve("secret://fake/a")
        r.invalidate()
        r.resolve("secret://fake/a")
        assert backend.calls == 3
        r.close()
        assert backend.closed


class TestProcessWideInstance:
    @pytest.fixture(autouse=True)
    def _fresh(self) -> Any:
        reset_for_tests()
        yield
        reset_for_tests()

    def _patch(self, monkeypatch: pytest.MonkeyPatch, section: Any) -> None:
        import turnstone.core.config as config_mod

        def fake(name: str | None = None) -> Any:
            if name == "secrets":
                return dict(section) if isinstance(section, dict) else section
            return {} if name else {"secrets": section}

        monkeypatch.setattr(config_mod, "load_config", fake)

    def test_literal_never_reads_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import turnstone.core.config as config_mod

        def explode(name: str | None = None) -> Any:
            raise AssertionError("config read")

        monkeypatch.setattr(config_mod, "load_config", explode)
        assert resolve("sk-literal") == "sk-literal"
        assert resolve_mapping({"a": "b"}) == {"a": "b"}
        assert not has_reference({"a": "b"})
        invalidate_cache()  # never builds a resolver either

    def test_malformed_section_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, {"vault": {"auth": "approle"}})
        with pytest.raises(SecretConfigError):
            resolve("secret://vault/x#k")
        with pytest.raises(SecretConfigError):
            load_secrets_config()

    def test_no_backends_means_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, {})
        with pytest.raises(SecretReferenceError, match="not configured"):
            validate_reference("secret://file/run/secrets/x")

    def test_instance_is_built_once(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._patch(monkeypatch, {"file": {"root": str(tmp_path)}})
        first = get_resolver()
        assert get_resolver() is first
        reset_for_tests()
        assert get_resolver() is not first

    def test_file_reference_end_to_end_and_invalidate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        secret = tmp_path / "openai"
        secret.write_text("sk-file\n")
        self._patch(monkeypatch, {"file": {"root": str(tmp_path)}})
        assert resolve(f"secret://file{tmp_path}/openai") == "sk-file"
        secret.write_text("sk-rotated\n")
        assert resolve(f"secret://file{tmp_path}/openai") == "sk-file"  # cached
        invalidate_cache()
        assert resolve(f"secret://file{tmp_path}/openai") == "sk-rotated"
        assert resolve_mapping({"Authorization": f"secret://file{tmp_path}/openai"}) == {
            "Authorization": "sk-rotated"
        }
