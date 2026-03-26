"""TLS Client — certificate provisioning for service nodes.

Non-console services (server, bridge, channel gateway) use this to
request certificates from the console's ACME endpoint and build
SSL contexts for mTLS communication.

Flow:
1. Fetch CA root cert from console (plain HTTP, first boot)
2. Request service cert via ACME (plain HTTP, first boot)
3. Build SSL contexts for uvicorn (server) and httpx (client)
4. Start auto-renewal (uses existing cert for mTLS to console)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl

    from turnstone.core.storage._protocol import StorageBackend

from turnstone.core.log import get_logger

log = get_logger(__name__)

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


class TLSClient:
    """TLS client for service nodes.

    Requests certificates from the console's ACME endpoint and provides
    SSL contexts for server (uvicorn) and client (httpx) use.

    Typical usage::

        client = TLSClient(storage, console_url="http://console:8080")
        await client.init()             # Fetch CA, request cert
        server_ctx = client.get_server_ssl_context()  # For uvicorn
        client_ctx = client.get_client_ssl_context()  # For httpx
        await client.start_renewal()    # Background auto-renewal
    """

    def __init__(
        self,
        storage: StorageBackend,
        console_url: str = "",
        hostnames: list[str] | None = None,
    ) -> None:
        lacme = _require_lacme()

        from turnstone.core.tls_store import StorageStore

        self._storage = storage
        self._store = StorageStore(storage)
        self._console_url = console_url.rstrip("/") if console_url else ""
        self._hostnames = hostnames or []
        self._event_dispatcher = lacme.EventDispatcher()
        self._ca_pem: bytes | None = None
        self._bundle: Any | None = None
        self._renewal_task: Any | None = None
        self._renewal_client: Any | None = None

        # Wire Prometheus metrics
        try:
            from lacme.metrics import setup_metrics

            setup_metrics(self._event_dispatcher)
        except ImportError:
            pass

    async def init(self) -> None:
        """Fetch CA root cert and request a service certificate.

        If no console_url was provided, discovers it from the services
        table. Performs initial cert provisioning over plain HTTP (ACME
        protocol provides integrity via JWS).
        """
        if not self._console_url:
            self._console_url = self._discover_console_url()
        await self._fetch_ca_cert()
        await self._request_cert()

    def _discover_console_url(self) -> str:
        """Look up the console URL from the services table."""
        consoles = self._storage.list_services("console", max_age_seconds=3600)
        if not consoles:
            raise RuntimeError(
                "No console service found in services table. "
                "Ensure the console is running and has registered, "
                "or provide console_url explicitly."
            )
        url = consoles[0]["url"]
        log.info("tls.console.discovered", url=url)
        return url

    async def _fetch_ca_cert(self) -> None:
        """Fetch the CA root cert from the console.

        Always uses plain HTTP for bootstrapping — the node doesn't have
        the CA cert yet, so it can't verify HTTPS.
        """
        import httpx

        # Force HTTP for bootstrap (can't verify HTTPS without CA cert)
        base = self._console_url.replace("https://", "http://")
        url = f"{base}/acme/ca.pem"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
                resp.raise_for_status()
                self._ca_pem = resp.content
                log.info("tls.ca.fetched", url=url)
        except Exception:
            log.error("tls.ca.fetch_failed", url=url, exc_info=True)
            raise

    async def _request_cert(self) -> None:
        """Request a certificate from the console's ACME endpoint."""
        if not self._hostnames:
            raise ValueError("No hostnames configured for TLS cert request")

        # Check for existing valid cert
        from datetime import UTC, datetime

        existing = self._store.load_cert(self._hostnames[0])
        if existing is not None and existing.expires_at > datetime.now(UTC):
            self._bundle = existing
            log.info("tls.cert.loaded", domain=self._hostnames[0])
            return

        # Request new cert via ACME (plain HTTP for initial request)
        lacme = _require_lacme()
        directory_url = f"{self._console_url}/acme/directory"

        async with lacme.Client(
            directory_url=directory_url,
            store=self._store,
            event_dispatcher=self._event_dispatcher,
            allow_insecure=True,
        ) as client:
            self._bundle = await client.issue(self._hostnames)
            self._store.save_cert(self._bundle)
            log.info("tls.cert.issued", domain=self._hostnames[0])

    # -- Auto-renewal ----------------------------------------------------------

    async def start_renewal(self) -> None:
        """Start background auto-renewal via the console's ACME endpoint."""
        lacme = _require_lacme()

        def _on_renewed(bundle: Any) -> None:
            self._bundle = bundle
            log.info("tls.cert.renewed", domain=bundle.domain)

        directory_url = f"{self._console_url}/acme/directory"
        client = lacme.Client(
            directory_url=directory_url,
            store=self._store,
            event_dispatcher=self._event_dispatcher,
            allow_insecure=True,
        )
        await client.__aenter__()

        manager = lacme.RenewalManager(
            client=client,
            store=self._store,
            interval_hours=_RENEW_INTERVAL_HOURS,
            days_before_expiry=_RENEW_BEFORE_EXPIRY_DAYS,
            on_renewed=_on_renewed,
            event_dispatcher=self._event_dispatcher,
        )
        self._renewal_task = manager.start()
        self._renewal_client = client
        log.info("tls.renewal.started", directory=directory_url)

    async def stop_renewal(self) -> None:
        """Stop background renewal and close the ACME client."""
        import contextlib

        if self._renewal_task is not None:
            import asyncio

            self._renewal_task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._renewal_task
            except Exception:
                log.exception("tls.renewal.stop_error")
            self._renewal_task = None
        if self._renewal_client is not None:
            with contextlib.suppress(Exception):
                await self._renewal_client.__aexit__(None, None, None)
            self._renewal_client = None

    # -- SSL contexts ----------------------------------------------------------

    def get_server_ssl_context(self) -> ssl.SSLContext | None:
        """Build SSL context for uvicorn HTTPS listener."""
        if self._bundle is None or self._ca_pem is None:
            return None
        _require_lacme()
        from lacme.mtls import server_ssl_context

        return server_ssl_context(  # type: ignore[no-any-return]
            cert_pem=self._bundle.fullchain_pem,
            key_pem=self._bundle.key_pem,
            ca_cert_pem=self._ca_pem,
        )

    def get_client_ssl_context(self) -> ssl.SSLContext | None:
        """Build mTLS client context for httpx connections."""
        if self._bundle is None or self._ca_pem is None:
            return None
        _require_lacme()
        from lacme.mtls import client_ssl_context

        return client_ssl_context(  # type: ignore[no-any-return]
            cert_pem=self._bundle.cert_pem,
            key_pem=self._bundle.key_pem,
            ca_cert_pem=self._ca_pem,
        )

    # -- Properties ------------------------------------------------------------

    @property
    def ca_pem(self) -> bytes | None:
        return self._ca_pem

    @property
    def bundle(self) -> Any | None:
        return self._bundle

    @property
    def initialized(self) -> bool:
        return self._bundle is not None and self._ca_pem is not None
