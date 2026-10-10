"""The Vault-compatible backend: KV v2 on Vault or OpenBao.

The HTTP shapes below were checked against Vault 2.1.2 and OpenBao 2.5.0
development servers: ``GET /v1/<mount>/data/<path>`` returns the fields under
``data.data``; a missing secret is a 404 with an empty ``errors`` list; a
rejected or expired token is a 403 ``permission denied`` (neither store tells
that apart from a denied path); a sealed store is a 503. Logins at
``/v1/auth/<mount>/login`` with ``{"role", "jwt"}`` (jwt; the kubernetes auth
method takes the same request) or ``{"role_id", "secret_id"}`` (approle) answer
with ``auth.client_token`` and ``auth.lease_duration`` (seconds, 0 for a token
that never expires). The two stores differ on one header: Vault ignores
``X-Vault-Namespace`` and OpenBao enforces it, so it is sent only when
configured.
"""

from __future__ import annotations

import ssl
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2

from turnstone.core.log import get_logger
from turnstone.core.secret_refs._errors import SecretBackendError, SecretReferenceError

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.secret_refs._config import VaultBackendConfig

log = get_logger(__name__)

# Log in again once this share of the lease has elapsed.
_RELOGIN_AT_FRACTION = 0.75


def _read_credential_file(path: str, *, what: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SecretBackendError(
            f"the {what} file cannot be read", retryable=True, detail=f"{path}: {exc}"
        ) from exc
    if not value:
        raise SecretBackendError(f"the {what} file is empty", retryable=True, detail=path)
    return value


def _errors_of(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    messages = body.get("errors") or body.get("warnings") or []
    if isinstance(messages, list):
        return "; ".join(str(m).strip() for m in messages if str(m).strip())
    return ""


class VaultBackend:
    """Resolve ``secret://vault/<path>#<field>`` against one KV v2 mount.

    One backend instance holds one client token for the process. The token is
    obtained with the configured auth method on first use and replaced by a
    fresh login once three quarters of its lease have elapsed or the store
    rejects it (one re-login, then one retry). Every method is thread-safe; a
    login happens under the token lock so concurrent resolutions share one
    round trip. Messages raised to callers name the path and a category only;
    the store address and the store's own error text go to the log through
    ``detail``.
    """

    name = "vault"

    def __init__(
        self,
        config: VaultBackendConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._relogin_at: float | None = None
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=config.ca_cert) if config.ca_cert else True
        )
        # The store address and CA come from config.toml: no proxy or SSL_CERT
        # environment, like the other httpx2 clients that carry credentials.
        self._client = httpx2.Client(
            base_url=config.address,
            timeout=config.timeout_seconds,
            verify=verify,
            trust_env=False,
            transport=transport,
        )

    # -- public -----------------------------------------------------------------

    def fetch(self, path: str, key: str | None) -> str:
        if not key:
            raise SecretReferenceError(
                "a vault reference needs the field name after '#' (secret://vault/<path>#<field>)"
            )
        relative = path.strip("/")
        mount = self._config.mount
        url = f"/v1/{mount}/data/{relative}"
        response = self._get(url)
        if response.status_code == 403:
            # The token may have been revoked or may have expired early: log
            # in once more and retry once. A denied path answers 403 as well,
            # so a misconfigured reference costs one extra login per resolve.
            self._forget_token()
            response = self._get(url)
        if response.status_code == 404:
            raise SecretBackendError(
                f"{relative!r} not found in mount {mount!r}",
                retryable=False,
                detail=_errors_of(response),
            )
        if response.status_code == 403:
            raise SecretBackendError(
                f"permission denied reading {relative!r} in mount {mount!r}",
                retryable=False,
                detail=_errors_of(response),
            )
        if response.status_code != 200:
            raise SecretBackendError(
                "store unavailable",
                retryable=True,
                detail=f"HTTP {response.status_code}: {_errors_of(response)}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SecretBackendError(
                "store unavailable", retryable=True, detail="non-JSON body"
            ) from exc
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            data = data.get("data")
        if not isinstance(data, dict):
            raise SecretBackendError(
                f"{relative!r} has no data (is the mount a KV v2 engine?)", retryable=False
            )
        if key not in data:
            raise SecretBackendError(
                f"field {key!r} is not present in {relative!r}", retryable=False
            )
        value = data[key]
        if not isinstance(value, str):
            raise SecretBackendError(
                f"field {key!r} in {relative!r} is not a string", retryable=False
            )
        return value

    def close(self) -> None:
        self._client.close()

    # -- token lifecycle ---------------------------------------------------------

    def _headers(self, token: str) -> dict[str, str]:
        headers = {"X-Vault-Token": token}
        if self._config.namespace:
            headers["X-Vault-Namespace"] = self._config.namespace
        return headers

    def _get(self, url: str) -> httpx2.Response:
        token = self._ensure_token()
        try:
            return self._client.get(url, headers=self._headers(token))
        except httpx2.HTTPError as exc:
            raise SecretBackendError(
                "store unreachable",
                retryable=True,
                detail=f"{self._config.address}: {type(exc).__name__}: {exc}",
            ) from exc

    def _forget_token(self) -> None:
        with self._lock:
            self._token = ""
            self._relogin_at = None

    def _ensure_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and (self._relogin_at is None or now < self._relogin_at):
                return self._token
            auth = self._login()
            self._token = str(auth["client_token"])
            lease = auth.get("lease_duration") or 0
            lease_seconds = float(lease) if isinstance(lease, (int, float)) else 0.0
            self._relogin_at = (
                now + lease_seconds * _RELOGIN_AT_FRACTION if lease_seconds > 0 else None
            )
            return self._token

    def _login(self) -> dict[str, Any]:
        cfg = self._config
        url = f"/v1/auth/{cfg.auth_mount}/login"
        if cfg.auth == "jwt":
            jwt = _read_credential_file(cfg.jwt_path, what="workload JWT")
            payload: dict[str, Any] = {"role": cfg.role, "jwt": jwt}
        else:
            secret_id = cfg.secret_id or _read_credential_file(
                cfg.secret_id_file, what="approle secret_id"
            )
            payload = {"role_id": cfg.role_id, "secret_id": secret_id}
        headers = {"X-Vault-Namespace": cfg.namespace} if cfg.namespace else {}
        try:
            response = self._client.post(url, json=payload, headers=headers)
        except httpx2.HTTPError as exc:
            raise SecretBackendError(
                "store unreachable",
                retryable=True,
                detail=f"{cfg.address}: {type(exc).__name__}: {exc}",
            ) from exc
        if response.status_code != 200:
            raise SecretBackendError(
                f"{cfg.auth} login failed",
                retryable=True,
                detail=f"{url}: HTTP {response.status_code}: {_errors_of(response)}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SecretBackendError(
                f"{cfg.auth} login failed", retryable=True, detail=f"{url}: non-JSON body"
            ) from exc
        auth = body.get("auth") if isinstance(body, dict) else None
        if not isinstance(auth, dict) or not isinstance(auth.get("client_token"), str):
            raise SecretBackendError(
                f"{cfg.auth} login failed",
                retryable=True,
                detail=f"{url}: no client_token in the answer",
            )
        return auth
