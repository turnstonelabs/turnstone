"""Tests for the mTLS SAN/identity, renewal-scoping, and GC fixes.

Regression coverage for the cluster-wide mTLS breakage where:
  * service certs were keyed on ``socket.gethostname()`` (the container ID)
    and never carried the advertised service name, so every collector/proxy
    handshake failed the hostname check; and
  * every node ran an unscoped ``RenewalManager`` over the *shared* store,
    renewing every other node's cert (an N×M renewal storm).
"""

from __future__ import annotations

import socket

import pytest

from turnstone.core.storage import get_storage, init_storage, reset_storage

lacme = pytest.importorskip("lacme")


@pytest.fixture(autouse=True)
def _storage(tmp_path):
    """Initialize ephemeral SQLite storage for each test."""
    reset_storage()
    init_storage("sqlite", path=str(tmp_path / "test.db"))
    yield
    reset_storage()


# ── build_cert_hostnames ──────────────────────────────────────────────────────


def test_advertised_host_is_primary():
    """The advertised host is first, so it becomes the cert's primary domain."""
    from turnstone.core.tls import build_cert_hostnames

    names = build_cert_hostnames("http://server-1:8080", bind_host="0.0.0.0")
    assert names[0] == "server-1"
    assert "localhost" in names
    assert "127.0.0.1" in names
    # 0.0.0.0 is a wildcard bind and must not become a SAN
    assert "0.0.0.0" not in names


def test_strips_scheme_and_port():
    """Only the hostname is extracted from the advertise URL."""
    from turnstone.core.tls import build_cert_hostnames

    assert build_cert_hostnames("https://node-7:9999")[0] == "node-7"


def test_extra_sans_appended_and_deduped():
    """Env SANs are added once; duplicates collapse, order preserved."""
    from turnstone.core.tls import build_cert_hostnames

    names = build_cert_hostnames("http://server-1:8080", extra_sans="server-1, edge, edge")
    assert names[0] == "server-1"
    assert names.count("server-1") == 1
    assert names.count("edge") == 1


def test_fallback_to_os_hostname_when_no_advertise_url():
    """Bare-metal fallback: OS hostname becomes primary when no URL is given."""
    from turnstone.core.tls import build_cert_hostnames

    assert build_cert_hostnames("")[0] == socket.gethostname()


def test_extra_sans_rejects_wildcard_and_unspecified():
    """A stray wildcard / unspecified-address SAN must not reach the cert."""
    from turnstone.core.tls import build_cert_hostnames

    names = build_cert_hostnames("http://server-1:8080", extra_sans="*, 0.0.0.0, ::, edge")
    assert "*" not in names
    assert "0.0.0.0" not in names
    assert "::" not in names
    assert "edge" in names


# ── _SingleDomainStore ────────────────────────────────────────────────────────


def _san_values(cert_pem: bytes) -> list[str]:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return [g.value for g in san]


@pytest.mark.anyio
async def test_single_domain_store_filters_and_delegates():
    """list_certs exposes only the wrapped domain; other ops delegate."""
    from turnstone.console.tls import TLSManager
    from turnstone.core.tls import _SingleDomainStore

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    for dom in ("server-1", "server-2", "server-3"):
        mgr._store.save_cert(mgr._ca.issue([dom]))

    wrapped = _SingleDomainStore(mgr._store, "server-2")
    listed = wrapped.list_certs()
    assert [b.domain for b in listed] == ["server-2"]
    # __getattr__ delegation still reaches the real store
    assert wrapped.load_cert("server-1") is not None
    assert wrapped.delete_cert("server-3") is True
    assert len(mgr._store.list_certs()) == 2
    # An empty domain (missing identity) matches nothing — the safe fallback
    # that prevents an unscoped sweep of the whole shared store.
    assert _SingleDomainStore(mgr._store, "").list_certs() == []


# ── End-to-end SAN identity ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_issued_cert_covers_advertised_host():
    """A cert issued from the helper's hostnames covers the dialed name."""
    from turnstone.console.tls import TLSManager
    from turnstone.core.tls import build_cert_hostnames

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    hostnames = build_cert_hostnames("https://server-1:8080", extra_sans="server-1")
    bundle = mgr._ca.issue(hostnames)

    # Stable, advertised-name store key (not the ephemeral container ID).
    assert bundle.domain == "server-1"
    assert "server-1" in _san_values(bundle.cert_pem)


# ── Renewal scoping (the storm fix) ───────────────────────────────────────────


@pytest.mark.anyio
async def test_renewal_sweep_only_touches_own_domain():
    """A scoped sweep renews this node's cert and leaves siblings alone."""
    from turnstone.console.tls import TLSManager
    from turnstone.core.tls import _SingleDomainStore

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    for dom in ("server-1", "server-2", "server-3"):
        mgr._store.save_cert(mgr._ca.issue([dom]))

    # days_before_expiry is huge so every cert would be "due" — only scoping
    # keeps the sweep from renewing siblings.
    rm = lacme.RenewalManager(
        ca=mgr._ca,
        store=_SingleDomainStore(mgr._store, "server-1"),
        days_before_expiry=99999,
    )
    renewed = await rm.check_and_renew()
    assert {b.domain for b in renewed} == {"server-1"}


# ── Orphan GC ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_gc_removes_only_long_expired_certs():
    """GC reclaims certs expired past the cutoff and keeps live ones."""
    from datetime import UTC, datetime, timedelta

    from turnstone.console.tls import TLSManager

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    live = mgr._ca.issue(["server-1"])
    mgr._store.save_cert(live)

    # A decommissioned node's row: reuse real PEMs but stamp it expired-long-ago.
    dead = mgr._ca.issue(["dead-node"])
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    get_storage().save_tls_cert(
        domain="dead-node",
        cert_pem=dead.cert_pem.decode(),
        fullchain_pem=dead.fullchain_pem.decode(),
        key_pem=dead.key_pem.decode(),
        issued_at=old,
        expires_at=old,
        meta="{}",
    )

    removed = mgr.gc_expired_certs(max_age_days=7)
    assert removed == 1
    domains = {b.domain for b in mgr._store.list_certs()}
    assert domains == {"server-1"}


# ── Client-context caching + in-place reload ──────────────────────────────────


@pytest.mark.anyio
async def test_client_ctx_cached_and_reloaded_in_place():
    """The client context is cached and mutated in place on renewal."""
    from turnstone.console.tls import TLSManager

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    await mgr.issue_console_certs(["console"])

    ctx1 = mgr.get_client_ssl_context()
    ctx2 = mgr.get_client_ssl_context()
    assert ctx1 is ctx2  # cached, not rebuilt per call

    # Reloading a renewed bundle must not raise and keeps the same object so
    # httpx clients holding it pick up the new cert without a rebuild.
    mgr._reload_client_ctx(mgr._ca.issue(["console"]))
    assert mgr.get_client_ssl_context() is ctx1


# ── Server-side renewal → reload-hook wiring ──────────────────────────────────


def test_renew_callback_updates_bundle_and_runs_reload_hook():
    """The renewal callback caches the new bundle and fires the reload hook."""
    from types import SimpleNamespace

    from turnstone.core.tls import TLSClient

    client = TLSClient(storage=get_storage(), hostnames=["server-1"])
    seen: list[object] = []
    client.set_cert_reload_hook(seen.append)

    bundle = SimpleNamespace(domain="server-1")
    client._handle_renewed(bundle)

    assert client.bundle is bundle
    assert seen == [bundle]


def test_renew_callback_swallows_reload_hook_errors():
    """A failing reload hook must not abort the renewal callback."""
    from types import SimpleNamespace

    from turnstone.core.tls import TLSClient

    client = TLSClient(storage=get_storage(), hostnames=["server-1"])

    def _boom(_bundle: object) -> None:
        raise RuntimeError("listener swap failed")

    client.set_cert_reload_hook(_boom)
    bundle = SimpleNamespace(domain="server-1")
    client._handle_renewed(bundle)  # must not raise
    assert client.bundle is bundle


# ── swap_context_cert (shared listener/client hot-swap) ───────────────────────


def _tmp_pem_dirs() -> set[str]:
    import glob
    import tempfile
    from pathlib import Path

    return set(glob.glob(str(Path(tempfile.gettempdir()) / "lacme-pem-*")))


@pytest.mark.anyio
async def test_swap_context_cert_loads_and_leaves_no_temp_dir():
    """The hot-swap loads the renewed cert and reclaims its temp PEM dir."""
    import ssl

    from turnstone.console.tls import TLSManager
    from turnstone.core.tls import swap_context_cert

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    bundle = mgr._ca.issue(["server-1"])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    before = _tmp_pem_dirs()
    swap_context_cert(ctx, bundle, ca_pem=mgr.get_root_cert_pem())
    assert _tmp_pem_dirs() == before  # no net leaked temp dir


@pytest.mark.anyio
async def test_swap_context_cert_cleans_up_on_failure():
    """A malformed bundle must not leave private-key material on disk."""
    import ssl
    from types import SimpleNamespace

    from turnstone.console.tls import TLSManager
    from turnstone.core.tls import swap_context_cert

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    bad = SimpleNamespace(domain="x", fullchain_pem=b"not a cert", key_pem=b"not a key")

    before = _tmp_pem_dirs()
    with pytest.raises(ssl.SSLError):
        swap_context_cert(ctx, bad, ca_pem=mgr.get_root_cert_pem())
    assert _tmp_pem_dirs() == before  # temp dir removed even on load failure
