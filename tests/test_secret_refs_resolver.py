"""The resolver: cache, stale serving, invalidation and the process-wide instance."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from tests._secret_refs_helpers import FakeBackend, FakeClock, make_resolver
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
)
from turnstone.core.secret_refs._config import parse_secrets_config
from turnstone.core.secret_refs._errors import SecretBackendError
from turnstone.core.secret_refs._resolver import FAILURE_COOLDOWN_SECONDS

if TYPE_CHECKING:
    from pathlib import Path

VALUES = {"/a": "alpha", "/b": "bravo", "/empty": ""}
OUTAGE = SecretBackendError("store unavailable", retryable=True, detail="HTTP 503")


def _resolver(ttl: float = 300.0) -> tuple[SecretResolver, FakeBackend, FakeClock]:
    backend, clock = FakeBackend(VALUES), FakeClock()
    return make_resolver(backend, ttl=ttl, clock=clock), backend, clock


class TestResolver:
    def test_reference_is_cached_for_the_ttl(self) -> None:
        r, backend, clock = _resolver(ttl=60)
        assert r.fetch("secret://fake/a") == "alpha"
        backend.values["/a"] = "beta"
        clock.now = 59
        assert r.fetch("secret://fake/a") == "alpha"
        clock.now = 61
        assert r.fetch("secret://fake/a") == "beta"
        assert backend.calls == 2

    def test_retryable_failure_serves_the_last_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        r, backend, clock = _resolver(ttl=10)
        assert r.fetch("secret://fake/a") == "alpha"
        clock.now = 11
        backend.fail = OUTAGE
        with caplog.at_level(logging.WARNING, logger="turnstone.core.secret_refs._resolver"):
            assert r.fetch("secret://fake/a") == "alpha"
        assert "secret_refs.resolve_failed_serving_cached" in caplog.text

    def test_retryable_failure_without_a_cached_value_raises(self) -> None:
        r, backend, _ = _resolver()
        backend.fail = OUTAGE
        with pytest.raises(SecretResolveError, match="secret://fake/a: store unavailable") as info:
            r.fetch("secret://fake/a")
        assert info.value.retryable is True

    def test_definitive_failure_raises_and_drops_the_entry(self) -> None:
        r, backend, clock = _resolver(ttl=10)
        assert r.fetch("secret://fake/a") == "alpha"
        clock.now = 11
        backend.fail = SecretBackendError("permission denied", retryable=False)
        with pytest.raises(SecretResolveError, match="permission denied") as info:
            r.fetch("secret://fake/a")
        assert info.value.retryable is False
        backend.fail = OUTAGE
        clock.now += FAILURE_COOLDOWN_SECONDS + 1
        with pytest.raises(SecretResolveError):  # nothing cached any more
            r.fetch("secret://fake/a")

    def test_empty_value_is_an_error(self) -> None:
        r, _, _ = _resolver()
        with pytest.raises(SecretResolveError, match="empty value"):
            r.fetch("secret://fake/empty")

    def test_unconfigured_backend(self) -> None:
        r, _, _ = _resolver()
        with pytest.raises(SecretReferenceError, match="backend 'vault' is not configured"):
            r.fetch("secret://vault/x#k")

    def test_backend_level_reference_error_becomes_resolve_error(self) -> None:
        class Picky(FakeBackend):
            def fetch(self, path: str, key: str | None) -> str:
                raise SecretReferenceError("needs a field")

        r = SecretResolver(parse_secrets_config({}), backends={"fake": Picky()}, clock=FakeClock())
        with pytest.raises(SecretResolveError, match="needs a field"):
            r.fetch("secret://fake/a")

    def test_invalidate_asks_the_store_again_but_keeps_the_fallback(self) -> None:
        r, backend, clock = _resolver()
        assert r.fetch("secret://fake/a") == "alpha"
        backend.values["/a"] = "rotated"
        assert r.fetch("secret://fake/a") == "alpha"  # within the TTL
        r.invalidate()
        assert r.fetch("secret://fake/a") == "rotated"  # asked again
        # An operator reload during an outage keeps the values already in use.
        r.invalidate()
        backend.fail = OUTAGE
        assert r.fetch("secret://fake/a") == "rotated"
        clock.now += FAILURE_COOLDOWN_SECONDS + 1
        assert r.fetch("secret://fake/a") == "rotated"
        assert backend.calls == 4

    def test_one_failure_cools_the_backend_down(self) -> None:
        r, backend, clock = _resolver()
        assert r.fetch("secret://fake/a") == "alpha"
        r.invalidate()
        backend.fail = OUTAGE
        assert r.fetch("secret://fake/a") == "alpha"  # one round trip fails
        with pytest.raises(SecretResolveError, match="store unavailable") as info:
            r.fetch("secret://fake/b")  # never fetched: refused without a round trip
        assert info.value.retryable is True
        assert r.fetch("secret://fake/a") == "alpha"
        assert backend.calls == 2
        clock.now += FAILURE_COOLDOWN_SECONDS + 1
        backend.fail = None
        assert r.fetch("secret://fake/b") == "bravo"  # asked again once the cool-down ends
        assert r.fetch("secret://fake/a") == "alpha"
        assert backend.calls == 4

    def test_cool_down_starts_when_a_slow_failure_comes_back(self) -> None:
        clock = FakeClock()

        class SlowBackend(FakeBackend):
            def fetch(self, path: str, key: str | None) -> str:
                self.calls += 1
                clock.now += FAILURE_COOLDOWN_SECONDS  # a timeout as long as the cool-down
                raise OUTAGE

        slow = SlowBackend(VALUES)
        r = make_resolver(slow, clock=clock)
        with pytest.raises(SecretResolveError):
            r.fetch("secret://fake/a")
        with pytest.raises(SecretResolveError, match="store unavailable"):
            r.fetch("secret://fake/b")  # still cooling down: no second timeout
        assert slow.calls == 1

    def test_a_fetch_in_flight_during_invalidate_is_not_trusted_for_a_ttl(self) -> None:
        r, backend, clock = _resolver()

        class RacyBackend(FakeBackend):
            def fetch(self, path: str, key: str | None) -> str:
                value = super().fetch(path, key)
                r.invalidate()  # a rotation lands while this read is in flight
                return value

        racy = RacyBackend(VALUES)
        r = make_resolver(racy, clock=clock)
        assert r.fetch("secret://fake/a") == "alpha"
        racy.values["/a"] = "rotated"
        assert r.fetch("secret://fake/a") == "rotated"
        assert racy.calls == 2

    def test_close_closes_the_backends(self) -> None:
        r, backend, _ = _resolver()
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
        assert resolve("") == ""
        assert resolve_mapping({"a": "b", "n": 3}) == {"a": "b", "n": 3}
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
            resolve("secret://file/run/secrets/x")

    def test_backend_setup_failure_is_a_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        junk = tmp_path / "ca.pem"
        junk.write_text("not a certificate")
        self._patch(
            monkeypatch,
            {
                "vault": {
                    "address": "https://vault.example.com",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "s",
                    "ca_cert": str(junk),
                }
            },
        )
        with pytest.raises(SecretConfigError, match="ca_cert"):
            get_resolver()
        # Anything else that breaks while the backends are built is reported the
        # same way, so a host's startup check catches it.
        from turnstone.core.secret_refs import _resolver as resolver_module

        self._patch(monkeypatch, {"file": {"root": str(tmp_path)}})
        monkeypatch.setattr(
            resolver_module,
            "_build_backends",
            lambda _cfg: (_ for _ in ()).throw(RuntimeError("x")),
        )
        with pytest.raises(SecretConfigError, match="cannot set up the backends"):
            get_resolver()

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
        assert resolve_mapping({"Authorization": f"secret://file{tmp_path}/openai", "n": 1}) == {
            "Authorization": "sk-rotated",
            "n": 1,
        }
