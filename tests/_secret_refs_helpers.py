"""Shared fakes for the ``secret://`` tests: an in-memory backend and a settable clock."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.core.secret_refs import SecretResolver
from turnstone.core.secret_refs import _resolver as resolver_module
from turnstone.core.secret_refs._config import parse_secrets_config
from turnstone.core.secret_refs._errors import SecretBackendError

if TYPE_CHECKING:
    import pytest


class FakeBackend:
    """A ``secret://fake/...`` store: ``values`` maps ``/path`` to the secret.

    Set ``fail`` to make every fetch raise that error; ``calls`` counts fetches.
    """

    name = "fake"

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})
        self.fail: SecretBackendError | None = None
        self.calls = 0
        self.closed = False

    def fetch(self, path: str, key: str | None) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if path not in self.values:
            raise SecretBackendError(f"{path.strip('/')!r} not found", retryable=False)
        return self.values[path]

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_resolver(
    backend: FakeBackend, *, ttl: float = 300.0, clock: FakeClock | None = None
) -> SecretResolver:
    cfg = parse_secrets_config({"cache_ttl_seconds": ttl})
    return SecretResolver(cfg, backends={"fake": backend}, clock=clock or FakeClock())


def install_fake_resolver(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str], **kwargs: Any
) -> FakeBackend:
    """Make the process-wide resolver a fake one for this test; conftest resets it."""
    backend = FakeBackend(values)
    monkeypatch.setattr(resolver_module, "_instance", make_resolver(backend, **kwargs))
    return backend
