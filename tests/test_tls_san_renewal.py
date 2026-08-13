"""Tests for the mTLS SAN/identity, renewal-scoping, and GC fixes.

Regression coverage for the cluster-wide mTLS breakage where:
  * service certs were keyed on ``socket.gethostname()`` (the container ID)
    and never carried the advertised service name, so every collector/proxy
    handshake failed the hostname check; and
  * every node ran an unscoped ``RenewalManager`` over the *shared* store,
    renewing every other node's cert (an N×M renewal storm).
"""

from __future__ import annotations

import ipaddress
import socket

import lacme
import pytest

from turnstone.core.storage import get_storage, init_storage, reset_storage


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
    assert ipaddress.IPv4Address("127.0.0.1") in names
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


def test_extra_sans_rejects_wildcard():
    """A stray wildcard SAN must not reach the cert."""
    from turnstone.core.tls import build_cert_hostnames

    names = build_cert_hostnames("http://server-1:8080", extra_sans="*, edge")
    assert "*" not in names
    assert "edge" in names


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.0.2.10:8080", ipaddress.IPv4Address("192.0.2.10")),
        ("http://[2001:db8::10]:8080", ipaddress.IPv6Address("2001:db8::10")),
    ],
)
def test_advertised_ip_literal_is_typed_primary(url, expected):
    """URL IP literals cross the lacme boundary as typed IP identifiers."""
    from turnstone.core.tls import build_cert_hostnames

    assert build_cert_hostnames(url)[0] == expected


def test_extra_ip_literal_sans_are_typed():
    from turnstone.core.tls import build_cert_hostnames

    names = build_cert_hostnames(
        "http://server-1:8080",
        extra_sans="192.0.2.10, 2001:db8::10",
    )
    assert ipaddress.IPv4Address("192.0.2.10") in names
    assert ipaddress.IPv6Address("2001:db8::10") in names


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("0.0.0.0", "Unspecified address"),
        ("::", "Unspecified address"),
        ("fe80::1%eth0", "Scoped IPv6 address"),
    ],
)
def test_unusable_extra_ip_identity_rejected(value, message):
    from turnstone.core.tls import build_cert_hostnames

    with pytest.raises(ValueError, match=message):
        build_cert_hostnames("http://server-1:8080", extra_sans=value)


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8080",
        "http://[::]:8080",
        "http://[fe80::1%25eth0]:8080",
    ],
)
def test_unusable_advertised_ip_identity_rejected(url):
    from turnstone.core.tls import build_cert_hostnames

    with pytest.raises(ValueError, match="not a valid certificate identity"):
        build_cert_hostnames(url)


def test_identifier_normalization_is_typed_and_order_preserving():
    """Semantic duplicates collapse without changing DNS presentation."""
    from turnstone.core.tls import normalize_certificate_identifiers

    identifiers = normalize_certificate_identifiers(
        [
            "Node.Example",
            "node.example",
            "192.0.2.10",
            ipaddress.IPv4Address("192.0.2.10"),
            "2001:0db8:0:0::10",
            ipaddress.IPv6Address("2001:db8::10"),
        ]
    )

    assert identifiers == [
        "Node.Example",
        ipaddress.IPv4Address("192.0.2.10"),
        ipaddress.IPv6Address("2001:db8::10"),
    ]


# ── _SingleDomainStore ────────────────────────────────────────────────────────


def _san_values(cert_pem: bytes) -> list[str]:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return [str(g.value) for g in san]


def _sans(cert_pem: bytes):
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem)
    return list(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)


@pytest.mark.anyio
async def test_single_domain_store_filters_and_delegates():
    """list_certs exposes only the wrapped domain; other ops delegate."""
    from turnstone.console.tls import TLSManager
    from turnstone.core.tls_store import RenewalStoreView

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    for dom in ("server-1", "server-2", "server-3"):
        mgr._store.save_cert(mgr._ca.issue([dom]))

    wrapped = RenewalStoreView(mgr._store, "server-2")
    assert isinstance(wrapped, lacme.Store)
    listed = wrapped.list_certs()
    assert [b.domain for b in listed] == ["server-2"]
    # Explicit Store delegation still reaches the real store.
    assert wrapped.load_cert("server-1") is not None
    assert wrapped.delete_cert("server-3") is True
    assert len(mgr._store.list_certs()) == 2
    # An empty domain (missing identity) matches nothing — the safe fallback
    # that prevents an unscoped sweep of the whole shared store.
    assert RenewalStoreView(mgr._store, "").list_certs() == []


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


@pytest.mark.anyio
async def test_issued_cert_preserves_dns_and_ip_san_types():
    """Typed IP inputs become x509.IPAddress while DNS stays x509.DNSName."""
    from cryptography import x509

    from turnstone.console.tls import TLSManager

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    bundle = mgr._ca.issue(
        [
            "Node.Example",
            ipaddress.IPv4Address("192.0.2.10"),
            ipaddress.IPv6Address("2001:db8::10"),
        ]
    )

    sans = _sans(bundle.cert_pem)
    assert sans == [
        x509.DNSName("Node.Example"),
        x509.IPAddress(ipaddress.IPv4Address("192.0.2.10")),
        x509.IPAddress(ipaddress.IPv6Address("2001:db8::10")),
    ]
    assert bundle.domain == "Node.Example"
    assert bundle.domains == ("Node.Example", "192.0.2.10", "2001:db8::10")


# ── Renewal scoping (the storm fix) ───────────────────────────────────────────


@pytest.mark.anyio
async def test_renewal_sweep_only_touches_own_domain():
    """A scoped sweep renews this node's cert and leaves siblings alone."""
    from turnstone.console.tls import TLSManager
    from turnstone.core.tls_store import RenewalStoreView

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    for dom in ("server-1", "server-2", "server-3"):
        mgr._store.save_cert(mgr._ca.issue([dom]))

    # days_before_expiry is huge so every cert would be "due" — only scoping
    # keeps the sweep from renewing siblings.
    rm = lacme.RenewalManager(
        ca=mgr._ca,
        store=RenewalStoreView(mgr._store, "server-1"),
        days_before_expiry=99999,
    )
    renewed = await rm.check_and_renew()
    assert {b.domain for b in renewed} == {"server-1"}
    # Turnstone's CA policy applies to RenewalManager's implicit CA.issue()
    # call, and the fresh 48-hour leaf is no longer immediately due.
    from datetime import timedelta

    from cryptography import x509

    leaf = x509.load_pem_x509_certificate(renewed[0].cert_pem)
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == timedelta(hours=48)
    normal_policy = lacme.RenewalManager(
        ca=mgr._ca,
        store=RenewalStoreView(mgr._store, "server-1"),
        days_before_expiry=1,
    )
    assert await normal_policy.check_and_renew() == []


@pytest.mark.anyio
async def test_renewal_uses_signed_leaf_expiry_not_row_metadata():
    """Mutable DB timestamps cannot suppress or spuriously force renewal."""
    from datetime import UTC, datetime, timedelta

    from turnstone.console.tls import TLSManager
    from turnstone.core.tls_store import RenewalStoreView

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    live = mgr._ca.issue(["live-node"])
    expired = mgr._ca.issue(["expired-node"], validity_hours=0)
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    past = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    get_storage().save_tls_cert(
        domain="live-node",
        cert_pem=live.cert_pem.decode(),
        fullchain_pem=live.fullchain_pem.decode(),
        key_pem=live.key_pem.decode(),
        issued_at=past,
        expires_at=past,
        meta='{"domain": "live-node", "domains": ["live-node"], "namespace": ""}',
    )
    get_storage().save_tls_cert(
        domain="expired-node",
        cert_pem=expired.cert_pem.decode(),
        fullchain_pem=expired.fullchain_pem.decode(),
        key_pem=expired.key_pem.decode(),
        issued_at=future,
        expires_at=future,
        meta='{"domain": "expired-node", "domains": ["expired-node"], "namespace": ""}',
    )

    live_renewal = lacme.RenewalManager(
        ca=mgr._ca,
        store=RenewalStoreView(mgr._store, "live-node"),
        days_before_expiry=1,
    )
    expired_renewal = lacme.RenewalManager(
        ca=mgr._ca,
        store=RenewalStoreView(mgr._store, "expired-node"),
        days_before_expiry=1,
    )

    assert await live_renewal.check_and_renew() == []
    renewed = await expired_renewal.check_and_renew()
    assert [bundle.domain for bundle in renewed] == ["expired-node"]


@pytest.mark.anyio
async def test_renewal_recovers_ip_identifier_types_from_sans():
    """DB metadata stays strings while renewal preserves authoritative IP SANs."""
    from cryptography import x509

    from turnstone.console.tls import TLSManager

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    await mgr.issue_console_certs(
        [
            ipaddress.IPv6Address("2001:0db8::10"),
            ipaddress.IPv4Address("192.0.2.10"),
        ]
    )

    renewed = mgr.renew_cert("2001:db8::10")

    assert renewed.domain == "2001:db8::10"
    assert renewed.domains == ("2001:db8::10", "192.0.2.10")
    assert _sans(renewed.cert_pem) == [
        x509.IPAddress(ipaddress.IPv6Address("2001:db8::10")),
        x509.IPAddress(ipaddress.IPv4Address("192.0.2.10")),
    ]
    assert mgr._store.load_cert("2001:db8::10") == renewed


# ── Orphan GC ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_gc_removes_only_long_expired_certs():
    """GC trusts signed leaf expiry, not mutable database timestamps."""
    from datetime import UTC, datetime, timedelta

    from turnstone.console.tls import TLSManager

    mgr = TLSManager(get_storage())
    await mgr.init_ca()
    live = mgr._ca.issue(["server-1"])
    # A stale/tampered metadata timestamp cannot delete a still-valid identity.
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    get_storage().save_tls_cert(
        domain="server-1",
        cert_pem=live.cert_pem.decode(),
        fullchain_pem=live.fullchain_pem.decode(),
        key_pem=live.key_pem.decode(),
        issued_at=old,
        expires_at=old,
        meta='{"domain": "server-1", "domains": ["server-1"], "namespace": ""}',
    )
    # validity_hours=0 produces a genuinely expired signed leaf.
    mgr._ca.issue(["dead-node"], validity_hours=0)

    removed = mgr.gc_expired_certs(max_age_days=0)
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


@pytest.mark.anyio
async def test_renew_callback_updates_bundle_and_runs_reload_hook():
    """The renewal callback caches the new bundle and fires the reload hook."""
    from types import SimpleNamespace

    from turnstone.core.tls import TLSClient

    client = TLSClient(storage=get_storage(), hostnames=["server-1"])
    seen: list[object] = []
    client.set_cert_reload_hook(seen.append)

    bundle = SimpleNamespace(domain="server-1")
    await client._handle_renewed(bundle)

    assert client.bundle is bundle
    assert seen == [bundle]


@pytest.mark.anyio
async def test_renew_callback_rolls_back_persistence_on_reload_error():
    """A failed live reload leaves the previous DB/runtime identity authoritative."""
    import lacme

    from turnstone.core.tls import TLSClient

    client = TLSClient(storage=get_storage(), hostnames=["server-1"])
    ca = lacme.CertificateAuthority()
    ca.init()
    previous = ca.issue(["server-1"])
    replacement = ca.issue(["server-1"])
    client._store.save_cert(previous)
    client._bundle = previous

    reloaded = []

    def _boom(bundle: object) -> None:
        reloaded.append(bundle)
        if bundle is replacement:
            raise RuntimeError("listener swap failed")

    client.set_cert_reload_hook(_boom)
    with pytest.raises(RuntimeError, match="listener swap failed"):
        await client._handle_renewed(replacement)

    assert client.bundle is previous
    assert client._store.load_cert("server-1") == previous
    assert reloaded == [replacement, previous]


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
