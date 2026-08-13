"""TLS Client — certificate provisioning for service nodes.

Non-console services (server, channel gateway) use this to
request certificates from the console's ACME endpoint and build
SSL contexts for mTLS communication.

Flow:
1. Fetch CA root cert from console (plain HTTP, first boot)
2. Request service cert via ACME (plain HTTP, first boot)
3. Build SSL contexts for the server listener and outbound HTTP clients
4. Start auto-renewal through the console's ACME responder
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import lacme
from lacme import CertBundle, IdentifierValue

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable, Coroutine, Generator, Sequence
    from typing import Any

    from lacme.mtls import PemPaths

    from turnstone.core.storage._protocol import StorageBackend

from turnstone.core.log import get_logger

log = get_logger(__name__)

CERT_VALIDITY_HOURS = 48
RENEW_INTERVAL_HOURS = 12
RENEW_BEFORE_EXPIRY_DAYS = 1
RENEW_MAX_JITTER_SECONDS = 600

# Boot-time init retry budget: 1+2+4+8+16 s ≈ 31 s of backoff. Sized to
# absorb a whole-stack restart, where every node races the console for the
# CA cert (compose re-enforces depends_on ordering only on `up`, not
# `restart`) and the console needs a few seconds to start accepting
# connections.
TLS_INIT_RETRY_ATTEMPTS = 6

_ACME_PUBLIC_RESOURCES = frozenset({"/directory", "/new-nonce", "/ca.pem"})
_ACME_PROTECTED_RESOURCES = frozenset({"/new-account", "/new-order", "/key-change", "/revoke-cert"})
_ACME_PROTECTED_RESOURCE_RE = re.compile(r"^/(?:authz|chall|finalize|order|cert)/[A-Za-z0-9._~-]+$")


class TurnstoneAutoApproveChallengeHandler:
    """No-op challenge handler for Turnstone's authenticated ACME responder.

    The responder intentionally auto-approves after the enrollment JWT has
    authorized the signing route, so locally publishing an HTTP-01 response
    would imply validation that never occurs. Do not use this with an external
    ACME service; a validating CA requires a real challenge handler.
    """

    async def provision(self, _domain: str, _token: str, _key_authorization: str) -> None:
        return None

    async def deprovision(self, _domain: str, _token: str) -> None:
        return None


def _url_origin(url: httpx2.URL) -> tuple[str, str, int]:
    """Return a normalized origin tuple, including default ports."""
    default_port = 443 if url.scheme == "https" else 80
    return url.scheme, url.host, url.port or default_port


class ACMEHTTPAuth(httpx2.Auth):
    """Attach a rotating enrollment JWT only to configured ACME responders.

    ACME directory entries are absolute URLs controlled by the responder.  A
    client-wide Authorization header would therefore leak a cluster service JWT
    if a compromised directory named another host.  This auth policy permits
    only known responder origins + mount prefixes and authenticates only the
    state-changing resources behind Turnstone's enrollment gate.
    """

    def __init__(
        self,
        responder_bases: list[str],
        token_provider: Callable[[], str] | None,
    ) -> None:
        bases: list[tuple[tuple[str, str, int], str]] = []
        for raw in responder_bases:
            parsed = httpx2.URL(raw.rstrip("/"))
            raw_path = parsed.raw_path
            if (
                not parsed.is_absolute_url
                or parsed.scheme not in {"http", "https"}
                or parsed.userinfo
                or parsed.query
                or parsed.fragment
                or b"%" in raw_path
            ):
                raise ValueError(f"ACME responder base is invalid: {raw!r}")
            item = (_url_origin(parsed), parsed.path.rstrip("/"))
            if item not in bases:
                bases.append(item)
        if not bases:
            raise ValueError("At least one ACME responder base is required")
        self._bases = tuple(bases)
        self._token_provider = token_provider

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        if request.url.userinfo or request.url.query or request.url.fragment:
            raise RuntimeError(f"Refusing non-canonical ACME request URL: {request.url}")
        raw_path = request.url.raw_path
        if b"%" in raw_path:
            # Reverse proxies disagree on whether encoded separators and dot
            # segments are normalized before routing.  ACME responder IDs use
            # only URL-unreserved ASCII, so any escape is both unnecessary and
            # an authorization-boundary ambiguity.
            raise RuntimeError(f"Refusing non-canonical ACME request URL: {request.url}")
        try:
            request_path = raw_path.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Refusing non-canonical ACME request URL: {request.url}") from exc

        suffix: str | None = None
        request_origin = _url_origin(request.url)
        for origin, base_path in self._bases:
            if request_origin != origin:
                continue
            if request_path.startswith(f"{base_path}/"):
                suffix = request_path[len(base_path) :]
                break

        if suffix is None:
            raise RuntimeError(
                f"Refusing ACME request outside configured responder bases: {request.url}"
            )
        if suffix in _ACME_PUBLIC_RESOURCES:
            yield request
            return
        if suffix not in _ACME_PROTECTED_RESOURCES and not _ACME_PROTECTED_RESOURCE_RE.fullmatch(
            suffix
        ):
            raise RuntimeError(f"Refusing unknown ACME responder resource: {request.url}")
        if self._token_provider is None:
            raise RuntimeError("ACME enrollment credentials are not configured")
        token = self._token_provider()
        if not token:
            raise RuntimeError("ACME enrollment token provider returned an empty token")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def build_acme_http_client(
    console_url: str,
    *,
    external_url: str = "",
    token_provider: Callable[[], str] | None,
) -> httpx2.AsyncClient:
    """Build a fresh HTTPX2 client pinned to trusted ACME responder bases."""
    bases = [f"{console_url.rstrip('/')}/acme"]
    if external_url:
        bases.append(external_url.rstrip("/"))
    return httpx2.AsyncClient(
        auth=ACMEHTTPAuth(bases, token_provider),
        follow_redirects=False,
        trust_env=False,
    )


async def complete_tls_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
    """Finish resource cleanup before propagating caller cancellation.

    A one-shot ``asyncio.shield`` still returns immediately when its caller is
    cancelled.  Retrying the shield keeps cleanup owned and observed even under
    repeated cancellation, then restores the first cancellation to the caller.
    """
    import asyncio

    task = asyncio.create_task(cleanup)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            # If the cleanup task itself was cancelled, preserve that result;
            # otherwise remember the caller cancellation and keep draining.
            if task.done() and task.cancelled():
                break
            if cancellation is None:
                cancellation = exc

    cleanup_error: BaseException | None = None
    try:
        task.result()
    except BaseException as exc:
        cleanup_error = exc

    if cancellation is not None:
        if cleanup_error is not None:
            log.error(
                "tls.cleanup.failed_during_cancellation",
                error=f"{type(cleanup_error).__name__}: {cleanup_error}",
            )
        raise cancellation
    if cleanup_error is not None:
        raise cleanup_error


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


def refresh_runtime_pems(
    bundle: CertBundle,
    *,
    ca_pem: bytes | None,
    previous: Path | None,
) -> PemPaths:
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


_IP_ADDRESS_TYPES = (ipaddress.IPv4Address, ipaddress.IPv6Address)


def parse_certificate_identifier(value: str) -> IdentifierValue:
    """Parse an operator-supplied DNS name or IP literal for lacme.

    lacme deliberately treats every plain string as DNS, even when it looks
    like an address. Turnstone's configuration/CLI boundary therefore converts
    IP literals to typed objects while preserving DNS spelling. Unspecified and
    scoped addresses are not usable peer identities and fail before enrollment.
    """
    candidate = value.strip()
    if not candidate:
        raise ValueError("Certificate identifiers must be non-empty")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return candidate
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        raise ValueError(f"Scoped IPv6 address {candidate!r} is not a valid certificate identity")
    if address.is_unspecified:
        raise ValueError(f"Unspecified address {candidate!r} is not a valid certificate identity")
    return address


def _identifier_key(value: IdentifierValue) -> tuple[str, str]:
    """Return a case-insensitive, type-preserving identity key."""
    if isinstance(value, _IP_ADDRESS_TYPES):
        return "ip", str(value)
    return "dns", value.lower()


def normalize_certificate_identifiers(
    values: Sequence[IdentifierValue],
) -> list[IdentifierValue]:
    """Validate and de-duplicate Turnstone identities in first-seen order."""
    if not values:
        raise ValueError("At least one certificate identifier is required")
    ordered: list[IdentifierValue] = []
    seen: set[tuple[str, str]] = set()
    for raw in values:
        if isinstance(raw, str):
            value = parse_certificate_identifier(raw)
        elif isinstance(raw, _IP_ADDRESS_TYPES):
            value = parse_certificate_identifier(str(raw))
        else:
            raise TypeError(
                "Certificate identifiers must be str, IPv4Address, or IPv6Address, "
                f"got {type(raw).__name__}"
            )
        key = _identifier_key(value)
        if key not in seen:
            seen.add(key)
            ordered.append(value)
    return ordered


def certificate_bundle_identifiers(bundle: CertBundle) -> list[IdentifierValue]:
    """Recover authoritative typed identities from a bundle's leaf SANs.

    Bundle metadata and database keys intentionally remain strings. The leaf
    certificate is authoritative for whether a numeric value is a legacy DNS
    SAN or a real IP SAN, then metadata restores the original primary ordering.
    """
    from cryptography import x509

    if not bundle.domains or bundle.domain != bundle.domains[0]:
        raise ValueError(f"Invalid certificate metadata for {bundle.domain!r}")
    try:
        certificates = x509.load_pem_x509_certificates(bundle.cert_pem)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid leaf certificate for {bundle.domain!r}") from exc
    if len(certificates) != 1:
        raise ValueError(f"Expected one leaf certificate for {bundle.domain!r}")
    try:
        sans = certificates[0].extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as exc:
        raise ValueError(f"Certificate for {bundle.domain!r} has no subjectAltName") from exc

    remaining: list[IdentifierValue] = []
    for san in sans:
        if isinstance(san, x509.DNSName):
            remaining.append(san.value)
        elif isinstance(san, x509.IPAddress) and isinstance(san.value, _IP_ADDRESS_TYPES):
            remaining.append(parse_certificate_identifier(str(san.value)))
        else:
            raise ValueError(
                f"Certificate for {bundle.domain!r} has unsupported SAN {type(san).__name__}"
            )

    ordered: list[IdentifierValue] = []
    for domain in bundle.domains:
        match = next(
            (
                index
                for index, value in enumerate(remaining)
                if (
                    value.lower() == domain.lower()
                    if isinstance(value, str)
                    else str(value) == domain
                )
            ),
            None,
        )
        if match is None:
            raise ValueError(f"Certificate SANs do not match stored metadata for {bundle.domain!r}")
        ordered.append(remaining.pop(match))
    if remaining:
        raise ValueError(f"Certificate SANs do not match stored metadata for {bundle.domain!r}")
    return ordered


def certificate_matches_identifiers(
    bundle: CertBundle,
    requested: Sequence[IdentifierValue],
) -> bool:
    """Return whether an existing leaf has exactly the requested typed SANs."""
    actual = certificate_bundle_identifiers(bundle)
    desired = normalize_certificate_identifiers(requested)
    return len(actual) == len(desired) and {_identifier_key(value) for value in actual} == {
        _identifier_key(value) for value in desired
    }


def validate_cluster_certificate_bundle(
    bundle: CertBundle,
    requested: Sequence[IdentifierValue],
    ca_pem: bytes,
) -> None:
    """Validate a persisted dual-purpose identity against the active CA.

    Store metadata is only an index.  Reuse is authorized by the actual leaf,
    private key, typed SANs, validity window, EKU, and the exact active cluster
    root.  This also prevents a same-domain public frontend certificate from
    being reused as the console's internal mTLS identity.
    """
    from datetime import UTC, datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID

    if not bundle.key_pem:
        raise ValueError(f"Certificate for {bundle.domain!r} has no private key")
    if not certificate_matches_identifiers(bundle, requested):
        raise ValueError(f"Certificate identifiers do not match {bundle.domain!r}")

    try:
        leaf_certificates = x509.load_pem_x509_certificates(bundle.cert_pem)
        chain = x509.load_pem_x509_certificates(bundle.fullchain_pem)
        roots = x509.load_pem_x509_certificates(ca_pem)
        private_key = serialization.load_pem_private_key(bundle.key_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Certificate material for {bundle.domain!r} is invalid") from exc
    if len(leaf_certificates) != 1 or len(chain) != 2 or len(roots) != 1:
        raise ValueError(f"Certificate chain for {bundle.domain!r} is invalid")

    leaf = leaf_certificates[0]
    root = roots[0]
    if chain[0].fingerprint(hashes.SHA256()) != leaf.fingerprint(hashes.SHA256()):
        raise ValueError(f"Full chain for {bundle.domain!r} does not start with its leaf")
    if chain[-1].fingerprint(hashes.SHA256()) != root.fingerprint(hashes.SHA256()):
        raise ValueError(f"Certificate for {bundle.domain!r} is not chained to the active CA")
    try:
        leaf.verify_directly_issued_by(root)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Certificate for {bundle.domain!r} was not issued by the active CA"
        ) from exc

    public_format = serialization.PublicFormat.SubjectPublicKeyInfo
    cert_public_key = leaf.public_key().public_bytes(serialization.Encoding.DER, public_format)
    key_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER, public_format
    )
    if cert_public_key != key_public_key:
        raise ValueError(f"Private key does not match certificate for {bundle.domain!r}")

    now = datetime.now(UTC)
    if leaf.not_valid_before_utc > now or leaf.not_valid_after_utc <= now:
        raise ValueError(f"Certificate for {bundle.domain!r} is outside its validity window")
    try:
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound as exc:
        raise ValueError(f"Certificate for {bundle.domain!r} has no extended key usage") from exc
    if not {
        ExtendedKeyUsageOID.SERVER_AUTH,
        ExtendedKeyUsageOID.CLIENT_AUTH,
    }.issubset(set(eku)):
        raise ValueError(f"Certificate for {bundle.domain!r} is not valid for cluster mTLS")


def build_cert_hostnames(
    advertise_url: str = "",
    *,
    bind_host: str = "",
    extra_sans: str = "",
) -> list[IdentifierValue]:
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

    names: list[IdentifierValue] = []
    if advertise_url:
        host = urlsplit(advertise_url).hostname or ""
        if host:
            names.append(parse_certificate_identifier(host))
    # OS hostname (container ID under Docker) — keeps in-container self-dial
    # working and provides a fallback primary on bare metal.
    hostname = socket.gethostname()
    names.append(parse_certificate_identifier(hostname))
    fqdn = socket.getfqdn()
    if fqdn and fqdn != hostname:
        names.append(parse_certificate_identifier(fqdn))
    names.extend(["localhost", ipaddress.IPv4Address("127.0.0.1")])
    if bind_host and bind_host not in ("0.0.0.0", "::", "*"):
        names.append(parse_certificate_identifier(bind_host))
    # A bare wildcard is an invalid service identity. Unspecified IPs fail
    # through the central parser rather than becoming unusable SANs.
    for raw in extra_sans.split(","):
        san = raw.strip()
        if not san or san == "*":
            continue
        names.append(parse_certificate_identifier(san))
    return normalize_certificate_identifiers(names)


def swap_context_cert(
    ctx: ssl.SSLContext,
    bundle: CertBundle,
    *,
    ca_pem: bytes | None = None,
) -> None:
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
    SSL contexts for server (uvicorn) and HTTP clients.

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
        hostnames: Sequence[IdentifierValue] | None = None,
        acme_external_url: str = "",
        enrollment_token_provider: Callable[[], str] | None = None,
    ) -> None:
        from turnstone.core.tls_store import CertificateValidationStore, StorageStore

        self._storage = storage
        self._store = StorageStore(storage)
        self._validated_store = CertificateValidationStore(self._store, self._validate_bundle)
        self._console_url = console_url.rstrip("/") if console_url else ""
        self._acme_external_url = acme_external_url.rstrip("/") if acme_external_url else ""
        self._enrollment_token_provider = enrollment_token_provider
        self._hostnames = normalize_certificate_identifiers(hostnames) if hostnames else []
        self._event_dispatcher = lacme.EventDispatcher()
        self._ca_pem: bytes | None = None
        self._bundle: CertBundle | None = None
        self._renewal_manager: lacme.RenewalManager | None = None
        self._renewal_client: lacme.Client | None = None
        self._renewal_http_client: httpx2.AsyncClient | None = None
        # Optional hook invoked with each renewed bundle so the live HTTPS
        # listener can swap in the new cert (uvicorn never reloads its SSL
        # context on its own — see ``set_cert_reload_hook``).
        self._cert_reload_hook: Callable[[CertBundle], None] | None = None

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

    def set_cert_reload_hook(self, hook: Callable[[CertBundle], None]) -> None:
        """Register a callback that installs a renewed bundle into the listener.

        Renewal updates the DB + ``self._bundle`` but not the running uvicorn
        listener, which keeps serving its boot cert until this hook swaps the
        renewed cert into the live SSL context.
        """
        self._cert_reload_hook = hook

    async def _handle_renewed(self, bundle: CertBundle) -> None:
        """Renewal callback: cache the new bundle and run the reload hook."""
        previous = self._bundle
        try:
            if self._cert_reload_hook is not None:
                self._cert_reload_hook(bundle)
        except Exception:
            # Client.issue saved before the callback. Restore the last identity
            # so shared persistence cannot claim a rotation the live listener
            # failed to adopt; the next sweep will retry.
            if previous is not None:
                try:
                    if self._cert_reload_hook is not None:
                        self._cert_reload_hook(previous)
                except Exception:
                    log.error("tls.cert.reload_rollback_failed", exc_info=True)
                self._store.save_cert(previous)
            log.warning("tls.cert.reload_hook_failed", exc_info=True)
            raise
        self._bundle = bundle
        log.info("tls.cert.renewed", domain=bundle.domain)

    def _validate_bundle(self, bundle: CertBundle) -> None:
        """Authorize a complete node identity before it reaches shared storage."""
        if self._ca_pem is None:
            raise ValueError("Cluster CA certificate is unavailable")
        validate_cluster_certificate_bundle(bundle, self._hostnames, self._ca_pem)

    async def init(self, *, attempts: int = 1, base_delay: float = 1.0) -> None:
        """Fetch CA root cert and request a service certificate.

        If no console_url was provided, discovers it from the services
        table. Direct deployments provision over HTTP on a trusted network;
        the dedicated enrollment JWT authenticates the node but does not make
        plaintext transport confidential or resistant to an on-path attacker.

        With ``attempts > 1``, failures are retried with exponential backoff
        (``base_delay * 2**n``). A node restarted alongside the console loses
        the race for the console's listener by well under a second; without
        retries that one refused connection downgrades the node to plain HTTP
        for its entire lifetime, even when a valid cert sits in the store.
        Discovery, CA fetch, and cert request are all idempotent, so the whole
        sequence is retried as a unit.
        """
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
                await self._sleep(delay)

    async def _sleep(self, delay: float) -> None:
        """Backoff sleep behind a seam so tests can stub it in isolation.

        Patching the module-global ``asyncio.sleep`` would also intercept it
        for every other task sharing the event loop; routing the retry backoff
        through a method keeps test stubs from corrupting concurrent tasks.
        """
        import asyncio

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
        url = str(consoles[0]["url"])
        log.info("tls.console.discovered", url=url)
        return url

    async def _fetch_ca_cert(self) -> None:
        """Fetch the CA root cert from the console.

        Uses the configured scheme. Direct deployments normally use plain HTTP
        (TOFU); an explicitly configured HTTPS proxy can provide independently
        trusted server authentication before the cluster CA is available.
        """
        url = f"{self._console_url}/acme/ca.pem"
        try:
            async with httpx2.AsyncClient(trust_env=False) as client:
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

        primary = str(self._hostnames[0])
        existing = self._store.load_cert(primary)
        if existing is not None:
            try:
                self._validate_bundle(existing)
            except ValueError as exc:
                log.warning(
                    "tls.cert.invalid_existing_identity",
                    domain=primary,
                    error=str(exc),
                )
            else:
                self._bundle = existing
                log.info("tls.cert.loaded", domain=primary)
                return
            # Keep the old row until the complete client-side bundle is ready;
            # StorageStore ignores the responder's intermediate keyless result,
            # so successful issuance atomically replaces it without sacrificing
            # the last usable identity on a transient failure.
            log.info("tls.cert.identifiers_changed", domain=primary)

        # Request a new cert through a responder-pinned, authenticated HTTPX2
        # client. lacme deliberately leaves injected client ownership here.
        directory_url = f"{self._console_url}/acme/directory"
        http_client = build_acme_http_client(
            self._console_url,
            external_url=self._acme_external_url,
            token_provider=self._enrollment_token_provider,
        )
        try:
            async with lacme.Client(
                directory_url=directory_url,
                store=self._validated_store,
                event_dispatcher=self._event_dispatcher,
                challenge_handler=TurnstoneAutoApproveChallengeHandler(),
                http_client=http_client,
                allow_insecure=True,
            ) as client:
                self._bundle = await client.issue(self._hostnames)
                log.info("tls.cert.issued", domain=primary)
        finally:

            async def _close_http_client() -> None:
                try:
                    await http_client.aclose()
                except Exception:
                    log.exception("tls.enrollment.http_close_error")

            await complete_tls_cleanup(_close_http_client())

    # -- Auto-renewal ----------------------------------------------------------

    async def start_renewal(self) -> None:
        """Start background auto-renewal via the console's ACME endpoint."""
        if self._renewal_manager is not None:
            raise RuntimeError("TLS renewal is already running")

        directory_url = f"{self._console_url}/acme/directory"
        http_client = build_acme_http_client(
            self._console_url,
            external_url=self._acme_external_url,
            token_provider=self._enrollment_token_provider,
        )
        client: lacme.Client | None = None
        try:
            client = lacme.Client(
                directory_url=directory_url,
                store=self._validated_store,
                event_dispatcher=self._event_dispatcher,
                challenge_handler=TurnstoneAutoApproveChallengeHandler(),
                http_client=http_client,
                allow_insecure=True,
            )

            # Scope the renewal sweep to this node's own certificate. The store
            # is shared cluster-wide; an unscoped manager would renew every
            # node's cert on every node (see RenewalStoreView). Empty domain
            # matches nothing rather than re-signing the whole cluster.
            own_domain = str(self._hostnames[0]) if self._hostnames else ""
            from turnstone.core.tls_store import RenewalStoreView

            renewal_store = RenewalStoreView(self._validated_store, own_domain)
            manager = lacme.RenewalManager(
                client=client,
                store=renewal_store,
                interval_hours=RENEW_INTERVAL_HOURS,
                days_before_expiry=RENEW_BEFORE_EXPIRY_DAYS,
                max_jitter_seconds=RENEW_MAX_JITTER_SECONDS,
                on_renewed=self._handle_renewed,
                event_dispatcher=self._event_dispatcher,
            )
            manager.start()
        except BaseException:

            async def _cleanup_failed_start() -> None:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.close()
                with contextlib.suppress(Exception):
                    await http_client.aclose()

            await complete_tls_cleanup(_cleanup_failed_start())
            raise
        self._renewal_manager = manager
        self._renewal_client = client
        self._renewal_http_client = http_client
        log.info("tls.renewal.started", directory=directory_url)

    async def stop_renewal(self) -> None:
        """Stop background renewal and close the ACME client."""
        manager, self._renewal_manager = self._renewal_manager, None
        client, self._renewal_client = self._renewal_client, None
        http_client, self._renewal_http_client = self._renewal_http_client, None
        if manager is None and client is None and http_client is None:
            return

        async def _cleanup() -> None:
            try:
                if manager is not None:
                    await manager.stop()
            except Exception:
                log.exception("tls.renewal.stop_error")
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        log.exception("tls.renewal.client_close_error")
                if http_client is not None:
                    try:
                        await http_client.aclose()
                    except Exception:
                        log.exception("tls.renewal.http_close_error")

        await complete_tls_cleanup(_cleanup())

    # -- SSL contexts ----------------------------------------------------------

    def get_server_ssl_context(self) -> ssl.SSLContext | None:
        """Build SSL context for uvicorn HTTPS listener."""
        if self._bundle is None or self._ca_pem is None:
            return None
        from lacme.mtls import server_ssl_context

        return server_ssl_context(
            cert_pem=self._bundle.fullchain_pem,
            key_pem=self._bundle.key_pem,
            ca_cert_pem=self._ca_pem,
        )

    def get_client_ssl_context(self) -> ssl.SSLContext | None:
        """Build mTLS client context for httpx connections."""
        if self._bundle is None or self._ca_pem is None:
            return None
        from lacme.mtls import client_ssl_context

        return client_ssl_context(
            cert_pem=self._bundle.cert_pem,
            key_pem=self._bundle.key_pem,
            ca_cert_pem=self._ca_pem,
        )

    # -- Properties ------------------------------------------------------------

    @property
    def ca_pem(self) -> bytes | None:
        return self._ca_pem

    @property
    def bundle(self) -> CertBundle | None:
        return self._bundle

    @property
    def initialized(self) -> bool:
        return self._bundle is not None and self._ca_pem is not None
