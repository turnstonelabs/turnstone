"""TLS Manager — Certificate Authority and ACME server for the console.

Owns the lacme CertificateAuthority, ACMEResponder, and RenewalManager
lifecycle. When TLS is enabled, the console acts as the cluster's internal
CA and ACME server, issuing short-lived mTLS certificates to all services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import ssl

    from starlette.types import ASGIApp

    from turnstone.core.config_store import ConfigStore
    from turnstone.core.storage._protocol import StorageBackend

log = structlog.get_logger(__name__)

# Hardcoded defaults — no operator config needed
_CA_CN = "Turnstone CA"
_CA_VALIDITY_DAYS = 3650  # 10 years
_CERT_VALIDITY_HOURS = 48
_RENEW_INTERVAL_HOURS = 24
_RENEW_BEFORE_EXPIRY_DAYS = 1


def _require_lacme() -> Any:
    try:
        import lacme
    except ImportError:
        raise ImportError(
            "lacme is required for TLS support. Install with: pip install turnstone[tls]",
        ) from None
    return lacme


class TLSManager:
    """Manages the internal CA, ACME responder, and certificate lifecycle.

    Typical usage::

        mgr = TLSManager(storage, config_store)
        await mgr.init_ca()
        responder = mgr.get_responder()       # Mount at /acme
        await mgr.issue_console_certs()        # Self-issue for this node
        mgr.start_renewal()                    # Background auto-renewal
    """

    def __init__(
        self,
        storage: StorageBackend,
        config_store: ConfigStore | None = None,
        port: int = 8080,
    ) -> None:
        lacme = _require_lacme()

        from turnstone.core.tls_store import StorageStore

        self._store = StorageStore(storage)
        self._config_store = config_store
        self._port = port
        self._event_dispatcher = lacme.EventDispatcher()
        self._ca: Any | None = None
        self._responder: Any | None = None
        self._renewal_task: Any | None = None
        self._renewal_manager: Any | None = None
        self._renewal_client: Any | None = None
        self._internal_bundle: Any | None = None
        self._frontend_bundle: Any | None = None

        # Wire structlog to lacme events
        self._subscribe_events()

        # Wire Prometheus metrics (if prometheus_client available)
        try:
            from lacme.metrics import setup_metrics

            setup_metrics(self._event_dispatcher)
        except ImportError:
            pass  # prometheus_client not installed

    def _subscribe_events(self) -> None:
        """Subscribe structlog handlers to lacme lifecycle events."""
        _require_lacme()
        from lacme.events import (
            CertificateExpiring,
            CertificateIssued,
            CertificateRenewed,
            ChallengeFailed,
        )

        def _on_issued(event: Any) -> None:
            if isinstance(event, CertificateIssued):
                log.info("tls.cert.issued", domain=event.domain)

        def _on_renewed(event: Any) -> None:
            if isinstance(event, CertificateRenewed):
                log.info("tls.cert.renewed", domain=event.domain)

        def _on_expiring(event: Any) -> None:
            if isinstance(event, CertificateExpiring):
                log.warning("tls.cert.expiring", domain=event.domain)

        def _on_failed(event: Any) -> None:
            if isinstance(event, ChallengeFailed):
                log.error("tls.challenge.failed", domain=getattr(event, "domain", "unknown"))

        self._event_dispatcher.subscribe(_on_issued, event_type=CertificateIssued)
        self._event_dispatcher.subscribe(_on_renewed, event_type=CertificateRenewed)
        self._event_dispatcher.subscribe(_on_expiring, event_type=CertificateExpiring)
        self._event_dispatcher.subscribe(_on_failed, event_type=ChallengeFailed)

    # -- CA lifecycle ----------------------------------------------------------

    async def init_ca(self) -> None:
        """Initialize the internal Certificate Authority.

        Loads an existing CA from storage or generates a new root key+cert.
        """
        lacme = _require_lacme()
        self._ca = lacme.CertificateAuthority(
            self._store,
            event_dispatcher=self._event_dispatcher,
        )
        self._ca.init(cn=_CA_CN, validity_days=_CA_VALIDITY_DAYS)
        log.info("tls.ca.initialized", cn=_CA_CN)

    def get_responder(self) -> ASGIApp:
        """Return the ACME responder ASGI app for mounting."""
        if self._ca is None:
            raise RuntimeError("CA not initialized — call init_ca() first")
        lacme = _require_lacme()
        if self._responder is None:
            self._responder = lacme.ACMEResponder(
                ca=self._ca,
                auto_approve=True,
            )
        return self._responder  # type: ignore[no-any-return]

    def get_root_cert_pem(self) -> bytes:
        """Return the CA root certificate in PEM format."""
        if self._ca is None:
            raise RuntimeError("CA not initialized — call init_ca() first")
        return self._ca.root_cert_pem  # type: ignore[no-any-return]

    # -- Cert issuance ---------------------------------------------------------

    async def issue_console_certs(self, hostnames: list[str]) -> None:
        """Issue certificates for the console node.

        Raises ValueError if hostnames is empty.

        Issues two certificates:
        - Internal cert: always from the internal CA (for mTLS with cluster)
        - Frontend cert: from external ACME CA if configured, else internal CA
        """
        if not hostnames:
            raise ValueError("issue_console_certs requires at least one hostname")

        # Internal cert — always from our own CA
        await self._issue_internal_cert(hostnames)

        # Frontend cert — external CA if configured
        acme_directory = ""
        if self._config_store:
            acme_directory = self._config_store.get("tls.acme_directory") or ""

        if acme_directory:
            await self._issue_frontend_cert(hostnames, acme_directory)
        else:
            # Self-issue from internal CA (behind reverse proxy or internal only)
            self._frontend_bundle = self._internal_bundle
            log.info("tls.frontend.self_issued", hostnames=hostnames)

    async def _issue_internal_cert(self, hostnames: list[str]) -> None:
        """Issue an internal mTLS cert from the internal CA."""
        if self._ca is None:
            raise RuntimeError("CA not initialized")

        # Check for existing cert in store (skip if expired)
        existing = self._store.load_cert(hostnames[0])
        if existing is not None:
            from datetime import UTC, datetime

            if existing.expires_at > datetime.now(UTC):
                self._internal_bundle = existing
                log.info("tls.internal.loaded", domain=hostnames[0])
                return
            log.info("tls.internal.expired", domain=hostnames[0])
            self._store.delete_cert(hostnames[0])

        # Issue new cert
        bundle = self._ca.issue(
            hostnames,
            validity_hours=_CERT_VALIDITY_HOURS,
        )
        self._store.save_cert(bundle)
        self._internal_bundle = bundle
        log.info("tls.internal.issued", domain=hostnames[0])

    async def _issue_frontend_cert(
        self,
        hostnames: list[str],
        acme_directory: str,
    ) -> None:
        """Issue a frontend cert from an external ACME CA."""
        lacme = _require_lacme()
        from lacme.challenges.http01 import HTTP01Handler

        handler = HTTP01Handler()

        async with lacme.Client(
            directory_url=acme_directory,
            store=self._store,
            challenge_handler=handler,
            event_dispatcher=self._event_dispatcher,
        ) as client:
            self._frontend_bundle = await client.issue(hostnames)
            self._store.save_cert(self._frontend_bundle)
            log.info(
                "tls.frontend.issued",
                domain=hostnames[0],
                ca=acme_directory,
            )

    # -- Auto-renewal ----------------------------------------------------------

    async def start_renewal(self) -> None:
        """Start background auto-renewal for all stored certificates.

        Creates a lacme Client pointed at the console's own ACME endpoint
        (localhost loopback) for cert renewal requests.
        """
        if self._ca is None:
            raise RuntimeError("CA not initialized")
        lacme = _require_lacme()

        def _on_renewed(bundle: Any) -> None:
            # Update our cached bundles if the renewed domain matches
            if self._internal_bundle and bundle.domain == self._internal_bundle.domain:
                self._internal_bundle = bundle
            if self._frontend_bundle and bundle.domain == self._frontend_bundle.domain:
                self._frontend_bundle = bundle

        # Create a client pointed at our own ACME endpoint for renewal
        directory_url = f"http://localhost:{self._port}/acme/directory"
        client = lacme.Client(
            directory_url=directory_url,
            store=self._store,
            event_dispatcher=self._event_dispatcher,
            allow_insecure=True,  # localhost loopback
        )
        await client.__aenter__()

        self._renewal_manager = lacme.RenewalManager(
            client=client,
            store=self._store,
            interval_hours=_RENEW_INTERVAL_HOURS,
            days_before_expiry=_RENEW_BEFORE_EXPIRY_DAYS,
            on_renewed=_on_renewed,
            event_dispatcher=self._event_dispatcher,
        )
        self._renewal_task = self._renewal_manager.start()
        self._renewal_client = client
        log.info(
            "tls.renewal.started",
            interval_hours=_RENEW_INTERVAL_HOURS,
            directory=directory_url,
        )

    async def stop_renewal(self) -> None:
        """Stop the background renewal task and close the ACME client."""
        import asyncio
        import contextlib

        if self._renewal_task is not None:
            self._renewal_task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._renewal_task
            except Exception:
                log.exception("tls.renewal.stop_error")
            self._renewal_task = None
        # Close the loopback ACME client
        if self._renewal_client is not None:
            with contextlib.suppress(Exception):
                await self._renewal_client.__aexit__(None, None, None)
            self._renewal_client = None

    # -- SSL contexts ----------------------------------------------------------

    def get_server_ssl_context(self) -> ssl.SSLContext | None:
        """Build an SSL context for the uvicorn HTTPS listener.

        Uses the frontend cert (external CA or self-issued).
        Returns None if no certs are available.
        """
        if self._frontend_bundle is None:
            return None
        _require_lacme()
        from lacme.mtls import server_ssl_context

        return server_ssl_context(  # type: ignore[no-any-return]
            cert_pem=self._frontend_bundle.fullchain_pem,
            key_pem=self._frontend_bundle.key_pem,
            ca_cert_pem=self.get_root_cert_pem(),
        )

    def get_client_ssl_context(self) -> ssl.SSLContext | None:
        """Build an mTLS client context for connecting to cluster services.

        Uses the internal cert for mutual authentication.
        Returns None if no certs are available.
        """
        if self._internal_bundle is None:
            return None
        _require_lacme()
        from lacme.mtls import client_ssl_context

        return client_ssl_context(  # type: ignore[no-any-return]
            cert_pem=self._internal_bundle.cert_pem,
            key_pem=self._internal_bundle.key_pem,
            ca_cert_pem=self.get_root_cert_pem(),
        )

    # -- Properties ------------------------------------------------------------

    def list_certs(self) -> list[Any]:
        """List all stored certificate bundles."""
        return self._store.list_certs()

    @property
    def ca_initialized(self) -> bool:
        return self._ca is not None

    @property
    def internal_bundle(self) -> Any | None:
        return self._internal_bundle

    @property
    def frontend_bundle(self) -> Any | None:
        return self._frontend_bundle
