"""TLS Client — certificate provisioning for service nodes.

Non-console services (server, channel gateway) use this to
request certificates from the console's ACME endpoint and build
SSL contexts for mTLS communication.

Flow:
1. Fetch CA root cert from console (plain HTTP, first boot)
2. Request service cert via ACME (plain HTTP, first boot)
3. Build SSL contexts for uvicorn (server) and httpx (client)
4. Start auto-renewal (uses existing cert for mTLS to console)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable

    from turnstone.core.storage._protocol import StorageBackend

from turnstone.core.log import get_logger

log = get_logger(__name__)

_RENEW_INTERVAL_HOURS = 24
_RENEW_BEFORE_EXPIRY_DAYS = 1

# Boot-time init retry budget: 1+2+4+8+16 s ≈ 31 s of backoff. Sized to
# absorb a whole-stack restart, where every node races the console for the
# CA cert (compose re-enforces depends_on ordering only on `up`, not
# `restart`) and the console needs a few seconds to start accepting
# connections.
TLS_INIT_RETRY_ATTEMPTS = 6


def tls_pem_runtime_dir() -> Path:
    """Parent directory for the boot-time PEM files.

    A fixed, well-known location (override: ``TURNSTONE_TLS_PEM_DIR``) so the
    container healthcheck can present the node's own cert as an mTLS client
    cert without DB access. ``write_pem_files`` creates a ``lacme-pem-*``
    subdirectory under it.
    """
    import os
    import tempfile

    env = os.environ.get("TURNSTONE_TLS_PEM_DIR")
    return Path(env) if env else Path(tempfile.gettempdir()) / "turnstone-tls"


def prepare_pem_runtime_dir() -> Path:
    """Create the PEM runtime dir (0700) and clear stale ``lacme-pem-*`` dirs.

    Stale subdirectories accumulate when a previous process dies before its
    atexit cleanup runs (SIGKILL, OOM). Clearing them at boot — before the new
    PEM dir is written — keeps exactly one live dir, so the healthcheck can't
    pick up an expired cert. Assumes one node per PEM root: two processes
    sharing a root would clear each other's live dirs (containers each get a
    private tmpfs; on bare metal set TURNSTONE_TLS_PEM_DIR per node).
    """
    import os
    import shutil
    import stat

    root = tls_pem_runtime_dir()
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        st = None
    if st is not None and (stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid()):
        # The default root lives in shared /tmp on bare metal: a hostile
        # local user could pre-create it as a symlink (redirecting where the
        # key material lands) or as a dir they own. Refuse both; our own
        # stale dir from a prior boot passes (chmod below repairs mode).
        raise RuntimeError(
            f"PEM runtime dir {root} exists but is a symlink or not owned by this process"
        )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for stale in root.glob("lacme-pem-*"):
        shutil.rmtree(stale, ignore_errors=True)
    return root


def refresh_runtime_pems(bundle: Any, *, ca_pem: bytes | None, previous: Path | None) -> Any:
    """Write a renewed bundle under the runtime root and drop the old dir.

    Keeps the on-disk PEMs (the healthcheck's mTLS client identity) in
    lockstep with the served cert: certs live 48 hours, so the boot-time
    files would expire and flip the container unhealthy two renewals in.
    The new dir is written before the old one is removed, so a concurrent
    probe always finds at least one complete dir.
    """
    import shutil

    from lacme.mtls import write_pem_files

    new_paths = write_pem_files(bundle, ca_pem=ca_pem, directory=tls_pem_runtime_dir())
    if previous is not None and previous != new_paths.cert.parent:
        shutil.rmtree(previous, ignore_errors=True)
    return new_paths


def _require_lacme() -> Any:
    try:
        import lacme
    except ImportError:
        raise ImportError(
            "lacme is required for TLS support. Install with: pip install turnstone[tls]",
        ) from None
    return lacme


def build_cert_hostnames(
    advertise_url: str = "",
    *,
    bind_host: str = "",
    extra_sans: str = "",
) -> list[str]:
    """Build the ordered, de-duplicated SAN list for a service certificate.

    The advertised host (the name peers dial) goes **first**, becoming the
    cert's primary domain. That makes it (a) a SAN, so mTLS hostname checks
    pass — deriving SANs from ``gethostname()`` alone (the container ID) omits
    it — and (b) a stable store key, so the cert reuses one row across
    container recreations instead of orphaning one each time. Falls back to
    ``gethostname()`` as primary only when no advertise URL is given.
    """
    import socket
    from urllib.parse import urlsplit

    names: list[str] = []
    if advertise_url:
        host = urlsplit(advertise_url).hostname or ""
        if host:
            names.append(host)
    # OS hostname (container ID under Docker) — keeps in-container self-dial
    # working and provides a fallback primary on bare metal.
    hostname = socket.gethostname()
    names.append(hostname)
    fqdn = socket.getfqdn()
    if fqdn and fqdn != hostname:
        names.append(fqdn)
    names.extend(["localhost", "127.0.0.1"])
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):
        names.append(bind_host)
    # Reject wildcard / unspecified-address SANs so a stray TURNSTONE_TLS_SANS
    # can't mint an over-broad cert the internal CA would have peers trust.
    for raw in extra_sans.split(","):
        san = raw.strip()
        if san and san not in ("0.0.0.0", "::", "*"):
            names.append(san)
    # De-duplicate, preserving first-seen order so the advertised host stays
    # primary.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


class _SingleDomainStore:
    """Store view exposing only one domain's cert to a renewal sweep.

    lacme's RenewalManager renews everything ``list_certs()`` returns. The
    store is shared cluster-wide, so an unscoped manager on each node renews
    every other node's (and every dead container's) cert — an N×M storm. This
    wrapper limits the sweep to one domain; all other operations delegate to
    the real store so renewed certs still persist to the shared database.
    """

    def __init__(self, inner: Any, domain: str) -> None:
        self._inner = inner
        self._domain = domain

    def list_certs(self) -> list[Any]:
        cert = self._inner.load_cert(self._domain)
        return [cert] if cert is not None else []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def swap_context_cert(ctx: ssl.SSLContext, bundle: Any, *, ca_pem: bytes | None = None) -> None:
    """Hot-swap a renewed bundle into a live :class:`ssl.SSLContext`.

    Writes the bundle to short-lived PEM files, calls ``load_cert_chain`` (so
    new handshakes use the renewed cert), then removes the temp dir — even on
    failure, so a malformed bundle can't leave private-key material on disk.
    Used by both the server listener context and the console client context.
    """
    import contextlib
    import shutil

    from lacme.mtls import write_pem_files

    paths = write_pem_files(bundle, ca_pem=ca_pem)
    try:
        ctx.load_cert_chain(str(paths.cert), str(paths.key))
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(paths.cert.parent)


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
        # Optional hook invoked with each renewed bundle so the live HTTPS
        # listener can swap in the new cert (uvicorn never reloads its SSL
        # context on its own — see ``set_cert_reload_hook``).
        self._cert_reload_hook: Callable[[Any], None] | None = None

        # Wire Prometheus metrics
        try:
            from lacme.metrics import setup_metrics

            setup_metrics(self._event_dispatcher)
        except ImportError:
            pass  # prometheus_client or lacme.metrics missing
        except ValueError as exc:
            if "Duplicated timeseries" in str(exc):
                log.debug("tls_metrics_already_registered")
            else:
                raise

    def set_cert_reload_hook(self, hook: Callable[[Any], None]) -> None:
        """Register a callback that installs a renewed bundle into the listener.

        Renewal updates the DB + ``self._bundle`` but not the running uvicorn
        listener, which keeps serving its boot cert until this hook swaps the
        renewed cert into the live SSL context.
        """
        self._cert_reload_hook = hook

    def _handle_renewed(self, bundle: Any) -> None:
        """Renewal callback: cache the new bundle and run the reload hook."""
        self._bundle = bundle
        log.info("tls.cert.renewed", domain=bundle.domain)
        if self._cert_reload_hook is not None:
            try:
                self._cert_reload_hook(bundle)
            except Exception:
                log.warning("tls.cert.reload_hook_failed", exc_info=True)

    async def init(self, *, attempts: int = 1, base_delay: float = 1.0) -> None:
        """Fetch CA root cert and request a service certificate.

        If no console_url was provided, discovers it from the services
        table. Performs initial cert provisioning over plain HTTP (ACME
        protocol provides integrity via JWS).

        With ``attempts > 1``, failures are retried with exponential backoff
        (``base_delay * 2**n``). A node restarted alongside the console loses
        the race for the console's listener by well under a second; without
        retries that one refused connection downgrades the node to plain HTTP
        for its entire lifetime, even when a valid cert sits in the store.
        Discovery, CA fetch, and cert request are all idempotent, so the whole
        sequence is retried as a unit.
        """
        import asyncio

        if attempts < 1:
            # range(1, attempts + 1) would be empty: init() would return
            # "successfully" with no CA and no cert.
            raise ValueError(f"attempts must be >= 1, got {attempts}")
        if base_delay < 0:
            raise ValueError(f"base_delay must be >= 0, got {base_delay}")

        for attempt in range(1, attempts + 1):
            try:
                if not self._console_url:
                    self._console_url = self._discover_console_url()
                await self._fetch_ca_cert()
                await self._request_cert()
                return
            except Exception as exc:
                if attempt >= attempts:
                    raise
                delay = base_delay * 2 ** (attempt - 1)
                log.warning(
                    "tls.init.retrying",
                    attempt=attempt,
                    max_attempts=attempts,
                    delay_seconds=delay,
                    error=f"{type(exc).__name__}: {exc}",
                )
                await asyncio.sleep(delay)

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
        except Exception as exc:
            # Warning, not error: init() may retry this, and the terminal
            # failure is logged by the caller. Full traceback at debug.
            log.warning("tls.ca.fetch_failed", url=url, error=f"{type(exc).__name__}: {exc}")
            log.debug("tls.ca.fetch_failed traceback", exc_info=True)
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
        from lacme.challenges.http01 import HTTP01Handler

        directory_url = f"{self._console_url}/acme/directory"

        async with lacme.Client(
            directory_url=directory_url,
            store=self._store,
            event_dispatcher=self._event_dispatcher,
            challenge_handler=HTTP01Handler(),
            allow_insecure=True,
        ) as client:
            self._bundle = await client.issue(self._hostnames)
            self._store.save_cert(self._bundle)
            log.info("tls.cert.issued", domain=self._hostnames[0])

    # -- Auto-renewal ----------------------------------------------------------

    async def start_renewal(self) -> None:
        """Start background auto-renewal via the console's ACME endpoint."""
        lacme = _require_lacme()

        from lacme.challenges.http01 import HTTP01Handler

        directory_url = f"{self._console_url}/acme/directory"
        client = lacme.Client(
            directory_url=directory_url,
            store=self._store,
            event_dispatcher=self._event_dispatcher,
            challenge_handler=HTTP01Handler(),
            allow_insecure=True,
        )
        await client.__aenter__()

        # Scope the renewal sweep to this node's own certificate.  The store
        # is shared cluster-wide; an unscoped RenewalManager would renew every
        # node's cert on every node (see :class:`_SingleDomainStore`).  An
        # empty domain matches nothing, so a missing hostname renews nothing
        # rather than falling back to re-signing the whole cluster.
        own_domain = self._hostnames[0] if self._hostnames else ""
        renewal_store = _SingleDomainStore(self._store, own_domain)
        manager = lacme.RenewalManager(
            client=client,
            store=renewal_store,
            interval_hours=_RENEW_INTERVAL_HOURS,
            days_before_expiry=_RENEW_BEFORE_EXPIRY_DAYS,
            on_renewed=self._handle_renewed,
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

        return server_ssl_context(  # type: ignore[no-any-return,unused-ignore]
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

        return client_ssl_context(  # type: ignore[no-any-return,unused-ignore]
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
