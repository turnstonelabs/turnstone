"""lacme Store adapter backed by turnstone's storage backend.

Bridges lacme's Store protocol to turnstone's StorageBackend, keeping
all TLS state (account keys, CA, certificates) in the shared database
rather than the filesystem.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend


def _parse_utc(iso: str) -> datetime:
    """Parse an ISO timestamp, assuming UTC if naive."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _ensure_lacme() -> Any:
    """Import lacme, raising a clear error if not installed."""
    try:
        import lacme
    except ImportError:
        raise ImportError(
            "lacme is required for TLS support. Install with: pip install turnstone[tls]",
        ) from None
    return lacme


class StorageStore:
    """lacme Store implementation backed by turnstone's database.

    Implements the 7-method Store protocol that lacme's CertificateAuthority
    and Client use for persistence.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    # -- Account key -----------------------------------------------------------

    def save_account_key(self, key: Any) -> None:
        """Persist the ACME account private key."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        self._storage.save_tls_account_key("default", key_pem)

    def load_account_key(self) -> Any | None:
        """Load the ACME account private key, or None."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        pem = self._storage.load_tls_account_key("default")
        if pem is None:
            return None
        return load_pem_private_key(pem.encode(), password=None)

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

    def save_cert(self, bundle: Any) -> Any:
        """Persist an issued certificate bundle."""
        meta = json.dumps({"domains": list(bundle.domains)})
        self._storage.save_tls_cert(
            domain=bundle.domain,
            cert_pem=bundle.cert_pem.decode(),
            fullchain_pem=bundle.fullchain_pem.decode(),
            key_pem=bundle.key_pem.decode(),
            issued_at=bundle.issued_at.isoformat(),
            expires_at=bundle.expires_at.isoformat(),
            meta=meta,
        )
        return bundle

    def load_cert(self, domain: str) -> Any | None:
        """Load a certificate bundle by domain."""
        row = self._storage.load_tls_cert(domain)
        if row is None:
            return None
        return self._row_to_bundle(row)

    def list_certs(self) -> list[Any]:
        """List all stored certificate bundles."""
        rows = self._storage.list_tls_certs()
        return [self._row_to_bundle(r) for r in rows]

    def delete_cert(self, domain: str) -> bool:
        """Delete a stored certificate bundle by domain."""
        return self._storage.delete_tls_cert(domain)

    def _row_to_bundle(self, row: dict[str, Any]) -> Any:
        """Convert a storage row dict to a lacme CertBundle."""
        lacme = _ensure_lacme()
        meta = json.loads(row.get("meta") or "{}")
        domains = tuple(meta.get("domains", [row["domain"]]))
        return lacme.CertBundle(
            domain=row["domain"],
            domains=domains,
            cert_pem=row["cert_pem"].encode(),
            fullchain_pem=row["fullchain_pem"].encode(),
            key_pem=row["key_pem"].encode(),
            issued_at=_parse_utc(row["issued_at"]),
            expires_at=_parse_utc(row["expires_at"]),
        )
