"""Tests for TLSClient — service node certificate provisioning."""

from __future__ import annotations

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


def test_bridge_tls_defaults():
    """Bridge with default TLS params works without changes."""
    from turnstone.mq.bridge import Bridge

    # Default: tls_verify=True, tls_cert=None — no mTLS
    bridge = Bridge(server_url="http://localhost:8080")
    assert bridge._tls_verify is True
    assert bridge._tls_cert is None


def test_collector_tls_defaults():
    """Collector with default TLS params works without changes."""
    from turnstone.console.collector import ClusterCollector

    broker_mock = MagicMock()
    collector = ClusterCollector(broker=broker_mock)
    # Should create httpx client without errors
    assert collector._http_client is not None
