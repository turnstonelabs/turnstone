"""lacme Store adapter backed by turnstone's storage backend.

Bridges lacme's Store protocol to turnstone's StorageBackend, keeping
all TLS state (account keys, CA, certificates) in the shared database
rather than the filesystem.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from lacme import CertBundle, Store

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.storage._protocol import StorageBackend


def _parse_utc(iso: str) -> datetime:
    """Parse an ISO timestamp, assuming UTC if naive."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _with_authoritative_leaf_times(bundle: CertBundle) -> CertBundle:
    """Replace mutable row timestamps with the signed leaf validity window."""
    from cryptography import x509

    try:
        certificates = x509.load_pem_x509_certificates(bundle.cert_pem)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Certificate {bundle.domain!r} is malformed") from exc
    if len(certificates) != 1:
        raise ValueError(f"Certificate {bundle.domain!r} does not contain one leaf")
    leaf = certificates[0]
    return replace(
        bundle,
        issued_at=leaf.not_valid_before_utc,
        expires_at=leaf.not_valid_after_utc,
    )


_SCOPED_CERT_PREFIX = "turnstone-scope:"
_STORE_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class StorageStore:
    """lacme Store implementation backed by turnstone's database.

    Implements the 8-method Store protocol that lacme's CertificateAuthority
    and Client use for persistence.
    """

    def __init__(
        self,
        storage: StorageBackend,
        *,
        namespace: str = "",
        account_key_id: str = "default",
    ) -> None:
        if namespace and _STORE_NAMESPACE_RE.fullmatch(namespace) is None:
            raise ValueError(f"Invalid TLS store namespace: {namespace!r}")
        if not account_key_id:
            raise ValueError("TLS account key ID must be non-empty")
        self._storage = storage
        self._namespace = namespace
        self._account_key_id = account_key_id

    def _stored_domain(self, domain: str) -> str:
        if not self._namespace:
            return domain
        return f"{_SCOPED_CERT_PREFIX}{self._namespace}:{domain}"

    def _owns_row(self, row: dict[str, Any]) -> bool:
        domain = str(row["domain"])
        meta = json.loads(row.get("meta") or "{}")
        row_namespace = meta.get("namespace")
        if not self._namespace:
            return not domain.startswith(_SCOPED_CERT_PREFIX) and not row_namespace
        return (
            domain.startswith(f"{_SCOPED_CERT_PREFIX}{self._namespace}:")
            and row_namespace == self._namespace
        )

    # -- Account key -----------------------------------------------------------

    def save_account_key(self, key: ec.EllipticCurvePrivateKey) -> None:
        """Persist the ACME account private key."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        self._storage.save_tls_account_key(self._account_key_id, key_pem)

    def load_account_key(self) -> ec.EllipticCurvePrivateKey | None:
        """Load the ACME account private key, or None."""
        pem = self._storage.load_tls_account_key(self._account_key_id)
        if pem is None:
            return None
        key = load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise TypeError(f"Expected EC private key, got {type(key).__name__}")
        if not isinstance(key.curve, ec.SECP256R1):
            raise TypeError(f"Expected P-256 key, got {key.curve.name}")
        return key

    # -- CA --------------------------------------------------------------------

    def save_ca(self, name: str, cert_pem: bytes, key_pem: bytes) -> None:
        """Persist a CA root certificate and key."""
        self._storage.save_tls_ca(name, cert_pem.decode(), key_pem.decode())

    def load_ca(self, name: str) -> tuple[bytes, bytes] | None:
        """Load CA cert+key by name. Returns (cert_pem, key_pem) or None."""
        row = self._storage.load_tls_ca(name)
        if row is None:
            return None
        return row["cert_pem"].encode(), row["key_pem"].encode()

    # -- Certificates ----------------------------------------------------------

    def save_cert(self, bundle: CertBundle) -> CertBundle:
        """Persist a usable, private-key-bearing certificate identity.

        The ACME responder signs a client CSR and briefly produces a keyless
        bundle before the client saves its complete identity. Persisting that
        responder-side bundle would overwrite an existing private key with an
        empty value if enrollment is interrupted.
        """
        if not bundle.key_pem:
            return bundle
        meta = json.dumps(
            {
                "domain": bundle.domain,
                "domains": list(bundle.domains),
                "namespace": self._namespace,
            }
        )
        self._storage.save_tls_cert(
            domain=self._stored_domain(bundle.domain),
            cert_pem=bundle.cert_pem.decode(),
            fullchain_pem=bundle.fullchain_pem.decode(),
            key_pem=bundle.key_pem.decode(),
            issued_at=bundle.issued_at.isoformat(),
            expires_at=bundle.expires_at.isoformat(),
            meta=meta,
        )
        return bundle

    def load_cert(self, domain: str) -> CertBundle | None:
        """Load a certificate bundle by domain."""
        row = self._storage.load_tls_cert(self._stored_domain(domain))
        if row is None or not row.get("key_pem"):
            return None
        bundle = self._row_to_bundle(row)
        if bundle.domain != domain:
            raise ValueError(
                f"Stored TLS identity {bundle.domain!r} does not match lookup key {domain!r}"
            )
        return bundle

    def list_certs(self) -> list[CertBundle]:
        """List all managed, private-key-bearing certificate bundles."""
        rows = self._storage.list_tls_certs()
        return [
            self._row_to_bundle(row) for row in rows if row.get("key_pem") and self._owns_row(row)
        ]

    def delete_cert(self, domain: str) -> bool:
        """Delete a stored certificate bundle by domain."""
        return bool(self._storage.delete_tls_cert(self._stored_domain(domain)))

    def _row_to_bundle(self, row: dict[str, Any]) -> CertBundle:
        """Convert a storage row dict to a lacme CertBundle."""
        meta = json.loads(row.get("meta") or "{}")
        domain = str(meta.get("domain", row["domain"]))
        domains = tuple(meta.get("domains", [domain]))
        return CertBundle(
            domain=domain,
            domains=domains,
            cert_pem=row["cert_pem"].encode(),
            fullchain_pem=row["fullchain_pem"].encode(),
            key_pem=row["key_pem"].encode(),
            issued_at=_parse_utc(row["issued_at"]),
            expires_at=_parse_utc(row["expires_at"]),
        )


class RenewalStoreView:
    """Store view exposing one managed identity to a renewal sweep.

    Turnstone's certificate store is shared cluster-wide. lacme renews every
    bundle returned by ``list_certs()``, so each service must receive a scoped
    view while every other Store operation continues to delegate normally.
    """

    def __init__(self, inner: Store, domain: str) -> None:
        self._inner = inner
        self._domain = domain

    def save_account_key(self, key: ec.EllipticCurvePrivateKey) -> None:
        self._inner.save_account_key(key)

    def load_account_key(self) -> ec.EllipticCurvePrivateKey | None:
        return self._inner.load_account_key()

    def save_ca(self, name: str, cert_pem: bytes, key_pem: bytes) -> None:
        self._inner.save_ca(name, cert_pem, key_pem)

    def load_ca(self, name: str) -> tuple[bytes, bytes] | None:
        return self._inner.load_ca(name)

    def save_cert(self, bundle: CertBundle) -> CertBundle:
        return self._inner.save_cert(bundle)

    def load_cert(self, domain: str) -> CertBundle | None:
        return self._inner.load_cert(domain)

    def list_certs(self) -> list[CertBundle]:
        cert = self._inner.load_cert(self._domain)
        return [_with_authoritative_leaf_times(cert)] if cert is not None else []

    def delete_cert(self, domain: str) -> bool:
        return self._inner.delete_cert(domain)


class CertificateValidationStore:
    """Store wrapper that validates complete identities before persistence."""

    def __init__(self, inner: Store, validator: Callable[[CertBundle], None]) -> None:
        self._inner = inner
        self._validator = validator

    def save_account_key(self, key: ec.EllipticCurvePrivateKey) -> None:
        self._inner.save_account_key(key)

    def load_account_key(self) -> ec.EllipticCurvePrivateKey | None:
        return self._inner.load_account_key()

    def save_ca(self, name: str, cert_pem: bytes, key_pem: bytes) -> None:
        self._inner.save_ca(name, cert_pem, key_pem)

    def load_ca(self, name: str) -> tuple[bytes, bytes] | None:
        return self._inner.load_ca(name)

    def save_cert(self, bundle: CertBundle) -> CertBundle:
        if bundle.key_pem:
            self._validator(bundle)
        return self._inner.save_cert(bundle)

    def load_cert(self, domain: str) -> CertBundle | None:
        return self._inner.load_cert(domain)

    def list_certs(self) -> list[CertBundle]:
        return self._inner.list_certs()

    def delete_cert(self, domain: str) -> bool:
        return self._inner.delete_cert(domain)
