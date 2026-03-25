"""Tests for TLS storage backend and lacme Store adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from turnstone.core.storage import get_storage, init_storage, reset_storage


@pytest.fixture(autouse=True)
def _storage(tmp_path):
    """Initialize ephemeral SQLite storage for each test."""
    reset_storage()
    db = str(tmp_path / "test.db")
    init_storage("sqlite", path=db)
    yield
    reset_storage()


# ── Account keys ──────────────────────────────────────────────────────────────


def test_save_and_load_account_key():
    s = get_storage()
    s.save_tls_account_key(
        "default",
        "-----BEGIN EC PRIVATE KEY-----\nfake\n-----END EC PRIVATE KEY-----",
    )
    result = s.load_tls_account_key("default")
    assert result is not None
    assert "EC PRIVATE KEY" in result


def test_load_account_key_missing():
    s = get_storage()
    assert s.load_tls_account_key("nonexistent") is None


def test_save_account_key_upsert():
    s = get_storage()
    s.save_tls_account_key("default", "key-v1")
    s.save_tls_account_key("default", "key-v2")
    assert s.load_tls_account_key("default") == "key-v2"


# ── CA ────────────────────────────────────────────────────────────────────────


def test_save_and_load_ca():
    s = get_storage()
    s.save_tls_ca("Turnstone CA", "cert-pem-data", "key-pem-data")
    result = s.load_tls_ca("Turnstone CA")
    assert result is not None
    assert result["cert_pem"] == "cert-pem-data"
    assert result["key_pem"] == "key-pem-data"
    assert result["name"] == "Turnstone CA"


def test_load_ca_missing():
    s = get_storage()
    assert s.load_tls_ca("nonexistent") is None


def test_save_ca_upsert():
    s = get_storage()
    s.save_tls_ca("CA", "cert-v1", "key-v1")
    s.save_tls_ca("CA", "cert-v2", "key-v2")
    result = s.load_tls_ca("CA")
    assert result["cert_pem"] == "cert-v2"
    assert result["key_pem"] == "key-v2"


# ── Certificates ──────────────────────────────────────────────────────────────


def test_save_and_load_cert():
    s = get_storage()
    s.save_tls_cert(
        domain="node-1.internal",
        cert_pem="cert-data",
        fullchain_pem="fullchain-data",
        key_pem="key-data",
        issued_at="2026-03-25T00:00:00",
        expires_at="2026-03-27T00:00:00",
        meta=json.dumps({"domains": ["node-1.internal", "10.0.1.5"]}),
    )
    result = s.load_tls_cert("node-1.internal")
    assert result is not None
    assert result["domain"] == "node-1.internal"
    assert result["cert_pem"] == "cert-data"
    assert result["fullchain_pem"] == "fullchain-data"
    assert result["key_pem"] == "key-data"
    assert result["issued_at"] == "2026-03-25T00:00:00"
    assert result["expires_at"] == "2026-03-27T00:00:00"
    meta = json.loads(result["meta"])
    assert meta["domains"] == ["node-1.internal", "10.0.1.5"]


def test_load_cert_missing():
    s = get_storage()
    assert s.load_tls_cert("nonexistent") is None


def test_save_cert_upsert():
    s = get_storage()
    s.save_tls_cert("d", "c1", "f1", "k1", "2026-01-01", "2026-01-02")
    s.save_tls_cert("d", "c2", "f2", "k2", "2026-02-01", "2026-02-02")
    result = s.load_tls_cert("d")
    assert result["cert_pem"] == "c2"
    assert result["issued_at"] == "2026-02-01"


def test_list_certs_empty():
    s = get_storage()
    assert s.list_tls_certs() == []


def test_list_certs():
    s = get_storage()
    s.save_tls_cert("alpha.internal", "c", "f", "k", "2026-01-01", "2026-01-02")
    s.save_tls_cert("beta.internal", "c", "f", "k", "2026-01-01", "2026-01-02")
    certs = s.list_tls_certs()
    assert len(certs) == 2
    assert certs[0]["domain"] == "alpha.internal"  # sorted by domain
    assert certs[1]["domain"] == "beta.internal"


def test_delete_cert():
    s = get_storage()
    s.save_tls_cert("d", "c", "f", "k", "2026-01-01", "2026-01-02")
    assert s.delete_tls_cert("d") is True
    assert s.load_tls_cert("d") is None


def test_delete_cert_missing():
    s = get_storage()
    assert s.delete_tls_cert("nonexistent") is False


# ── StorageStore adapter ──────────────────────────────────────────────────────


@pytest.fixture
def store_adapter():
    """Create a StorageStore backed by the test database."""
    from turnstone.core.tls_store import StorageStore

    return StorageStore(get_storage())


def test_adapter_save_load_ca(store_adapter):
    store_adapter.save_ca("test-ca", b"cert-pem", b"key-pem")
    result = store_adapter.load_ca("test-ca")
    assert result is not None
    cert_pem, key_pem = result
    assert cert_pem == b"cert-pem"
    assert key_pem == b"key-pem"


def test_adapter_load_ca_missing(store_adapter):
    assert store_adapter.load_ca("missing") is None


def test_adapter_save_load_cert(store_adapter):
    lacme = pytest.importorskip("lacme")
    now = datetime.now(UTC)
    bundle = lacme.CertBundle(
        domain="test.internal",
        domains=("test.internal", "10.0.1.1"),
        cert_pem=b"cert",
        fullchain_pem=b"fullchain",
        key_pem=b"key",
        issued_at=now,
        expires_at=now,
    )
    store_adapter.save_cert(bundle)
    loaded = store_adapter.load_cert("test.internal")
    assert loaded is not None
    assert loaded.domain == "test.internal"
    assert loaded.domains == ("test.internal", "10.0.1.1")
    assert loaded.cert_pem == b"cert"
    assert loaded.fullchain_pem == b"fullchain"
    assert loaded.key_pem == b"key"


def test_adapter_list_certs(store_adapter):
    lacme = pytest.importorskip("lacme")
    now = datetime.now(UTC)
    for name in ["alpha", "beta"]:
        bundle = lacme.CertBundle(
            domain=f"{name}.internal",
            domains=(f"{name}.internal",),
            cert_pem=b"c",
            fullchain_pem=b"f",
            key_pem=b"k",
            issued_at=now,
            expires_at=now,
        )
        store_adapter.save_cert(bundle)
    certs = store_adapter.list_certs()
    assert len(certs) == 2
    assert certs[0].domain == "alpha.internal"


def test_adapter_load_cert_missing(store_adapter):
    assert store_adapter.load_cert("missing") is None


def test_adapter_account_key_roundtrip(store_adapter):
    """Test account key save/load with real cryptography objects."""
    pytest.importorskip("lacme")
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    store_adapter.save_account_key(key)
    loaded = store_adapter.load_account_key()
    assert loaded is not None
    # Verify it's a usable EC key
    assert loaded.key_size == key.key_size


def test_adapter_account_key_missing(store_adapter):
    assert store_adapter.load_account_key() is None
