"""Tests for TLSClient — service node certificate provisioning."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from turnstone.core.storage import get_storage, init_storage, reset_storage

lacme = pytest.importorskip("lacme")


@pytest.fixture(autouse=True)
def _storage(tmp_path):
    """Initialize ephemeral SQLite storage for each test."""
    reset_storage()
    db = str(tmp_path / "test.db")
    init_storage("sqlite", path=db)
    yield
    reset_storage()


# ── Console URL discovery ─────────────────────────────────────────────────────


def test_discover_console_url():
    """TLSClient discovers console URL from services table."""
    from turnstone.core.tls import TLSClient

    storage = get_storage()
    storage.register_service("console", "console", "http://console:8080")

    client = TLSClient(storage=storage, hostnames=["node-1"])
    url = client._discover_console_url()
    assert url == "http://console:8080"


def test_discover_console_url_missing():
    """TLSClient raises if no console registered."""
    from turnstone.core.tls import TLSClient

    client = TLSClient(storage=get_storage(), hostnames=["node-1"])
    with pytest.raises(RuntimeError, match="No console service found"):
        client._discover_console_url()


def test_explicit_console_url_skips_discovery():
    """When console_url is provided, discovery is skipped."""
    from turnstone.core.tls import TLSClient

    client = TLSClient(
        storage=get_storage(),
        console_url="http://explicit:9090",
        hostnames=["node-1"],
    )
    assert client._console_url == "http://explicit:9090"


# ── SSL context construction ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ssl_contexts_none_before_init():
    """SSL contexts are None before init()."""
    from turnstone.core.tls import TLSClient

    client = TLSClient(
        storage=get_storage(),
        console_url="http://localhost:8080",
        hostnames=["node-1"],
    )
    assert client.get_server_ssl_context() is None
    assert client.get_client_ssl_context() is None
    assert not client.initialized


# ── Backward compatibility ───────────────────────────────────────────────────


def test_collector_tls_defaults():
    """Collector with default TLS params works without changes."""
    from turnstone.console.collector import ClusterCollector

    storage_mock = MagicMock()
    collector = ClusterCollector(storage=storage_mock)
    # Should store TLS settings for async client creation
    assert collector._tls_verify is True


# ── init() retry ─────────────────────────────────────────────────────────────


def _make_flaky_client(monkeypatch, failures: int):
    """TLSClient whose CA fetch fails ``failures`` times, then succeeds.

    Returns (client, calls, sleeps) — mutable lists recording each CA-fetch
    attempt and each backoff delay (asyncio.sleep is stubbed out).
    """
    import asyncio

    from turnstone.core.tls import TLSClient

    client = TLSClient(
        storage=get_storage(),
        console_url="http://console:9999",
        hostnames=["node-1"],
    )
    calls: list[int] = []
    sleeps: list[float] = []

    async def flaky_fetch():
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            raise ConnectionError("console not accepting connections yet")

    async def ok_request():
        pass

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        # Record the backoff delay and skip the real wait, but still yield to
        # the loop. An async stub that returns without ever suspending lets the
        # whole retry run complete in a single event-loop step with no
        # checkpoint, which is fragile under the async test runner.
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(client, "_fetch_ca_cert", flaky_fetch)
    monkeypatch.setattr(client, "_request_cert", ok_request)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return client, calls, sleeps


@pytest.mark.anyio
async def test_init_rejects_invalid_retry_params():
    """attempts < 1 would make init() a silent no-op; fail fast instead."""
    from turnstone.core.tls import TLSClient

    client = TLSClient(
        storage=get_storage(),
        console_url="http://console:9999",
        hostnames=["node-1"],
    )
    with pytest.raises(ValueError, match="attempts must be >= 1"):
        await client.init(attempts=0)
    with pytest.raises(ValueError, match="base_delay must be >= 0"):
        await client.init(attempts=2, base_delay=-1.0)


@pytest.mark.anyio
async def test_init_default_single_attempt(monkeypatch):
    """Default init() keeps the old behavior: one attempt, no sleep."""
    client, calls, sleeps = _make_flaky_client(monkeypatch, failures=1)
    with pytest.raises(ConnectionError):
        await client.init()
    assert calls == [1]
    assert sleeps == []


@pytest.mark.anyio
async def test_init_retries_transient_failure(monkeypatch):
    """A transient console outage is absorbed by retries with backoff."""
    client, calls, sleeps = _make_flaky_client(monkeypatch, failures=2)
    await client.init(attempts=6)
    assert calls == [1, 2, 3]
    assert sleeps == [1.0, 2.0]


@pytest.mark.anyio
async def test_init_retries_exhausted_raises(monkeypatch):
    """When every attempt fails, the last error propagates."""
    client, calls, sleeps = _make_flaky_client(monkeypatch, failures=99)
    with pytest.raises(ConnectionError):
        await client.init(attempts=3)
    assert calls == [1, 2, 3]
    assert sleeps == [1.0, 2.0]


@pytest.mark.anyio
async def test_init_retries_discovery_failure(monkeypatch):
    """Console discovery (not-yet-registered console) is retried too."""
    import asyncio

    from turnstone.core.tls import TLSClient

    client = TLSClient(storage=get_storage(), hostnames=["node-1"])
    attempts: list[int] = []

    def flaky_discover():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("No console service found in services table.")
        return "http://console:9999"

    real_sleep = asyncio.sleep

    async def ok():
        pass

    async def fake_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(client, "_discover_console_url", flaky_discover)
    monkeypatch.setattr(client, "_fetch_ca_cert", ok)
    monkeypatch.setattr(client, "_request_cert", ok)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await client.init(attempts=2)
    assert attempts == [1, 2]
    assert client._console_url == "http://console:9999"


# ── PEM runtime dir ──────────────────────────────────────────────────────────


def test_pem_runtime_dir_env_override(monkeypatch, tmp_path):
    """TURNSTONE_TLS_PEM_DIR overrides the default location."""
    from turnstone.core.tls import tls_pem_runtime_dir

    monkeypatch.setenv("TURNSTONE_TLS_PEM_DIR", str(tmp_path / "custom"))
    assert tls_pem_runtime_dir() == tmp_path / "custom"


def test_pem_runtime_dir_default(monkeypatch):
    """Default lives under the system tempdir."""
    import tempfile

    from turnstone.core.tls import tls_pem_runtime_dir

    monkeypatch.delenv("TURNSTONE_TLS_PEM_DIR", raising=False)
    assert tls_pem_runtime_dir() == Path(tempfile.gettempdir()) / "turnstone-tls"


def test_prepare_pem_runtime_dir_clears_stale(monkeypatch, tmp_path):
    """Boot prep creates the dir 0700 and removes stale lacme-pem-* dirs."""
    from turnstone.core.tls import prepare_pem_runtime_dir

    root = tmp_path / "tls"
    monkeypatch.setenv("TURNSTONE_TLS_PEM_DIR", str(root))
    stale = root / "lacme-pem-stale"
    stale.mkdir(parents=True)
    (stale / "key.pem").write_text("old")
    (root / "unrelated").mkdir()

    result = prepare_pem_runtime_dir()

    assert result == root
    assert not stale.exists()
    assert (root / "unrelated").exists()  # only lacme-pem-* is cleared
    assert (root.stat().st_mode & 0o777) == 0o700


def test_prepare_pem_runtime_dir_rejects_symlink(monkeypatch, tmp_path):
    """A pre-created symlink at the root must be refused, not followed.

    On bare metal the default root sits in shared /tmp; following a
    planted symlink would land key material under an attacker-chosen
    path."""
    from turnstone.core.tls import prepare_pem_runtime_dir

    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "tls-link"
    link.symlink_to(target)
    monkeypatch.setenv("TURNSTONE_TLS_PEM_DIR", str(link))

    with pytest.raises(RuntimeError, match="symlink or not owned"):
        prepare_pem_runtime_dir()


def test_refresh_runtime_pems_rotates_dir(monkeypatch, tmp_path):
    """Renewal writes a fresh complete PEM dir, then drops the old one."""
    from lacme import CertificateAuthority, MemoryStore

    from turnstone.core.tls import prepare_pem_runtime_dir, refresh_runtime_pems

    monkeypatch.setenv("TURNSTONE_TLS_PEM_DIR", str(tmp_path / "tls"))
    root = prepare_pem_runtime_dir()

    ca = CertificateAuthority(store=MemoryStore())
    ca.init()
    boot_bundle = ca.issue(["node-1", "localhost"])
    renewed_bundle = ca.issue(["node-1", "localhost"])

    boot = refresh_runtime_pems(boot_bundle, ca_pem=ca.root_cert_pem, previous=None)
    boot_dir = boot.cert.parent
    assert boot_dir.parent == root

    renewed = refresh_runtime_pems(renewed_bundle, ca_pem=ca.root_cert_pem, previous=boot_dir)
    new_dir = renewed.cert.parent
    assert new_dir.parent == root
    assert not boot_dir.exists()
    for name in ("fullchain.pem", "key.pem", "ca.pem"):
        assert (new_dir / name).is_file()
