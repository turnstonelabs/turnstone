"""``[secrets]`` configuration: the resolver cache and the backends it may use."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from turnstone.core.secret_refs._errors import SecretConfigError

DEFAULT_CACHE_TTL_SECONDS = 300.0
DEFAULT_VAULT_TIMEOUT_SECONDS = 5.0

VAULT_AUTH_METHODS = ("jwt", "approle")


@dataclass(frozen=True)
class FileBackendConfig:
    """``[secrets.file]``: references resolve to files under ``root``."""

    root: str


@dataclass(frozen=True)
class VaultBackendConfig:
    """``[secrets.vault]``: a Vault-compatible KV v2 store (Vault or OpenBao)."""

    address: str
    auth: str
    auth_mount: str
    mount: str = "secret"
    namespace: str = ""
    role: str = ""
    jwt_path: str = ""
    role_id: str = ""
    secret_id: str = ""
    secret_id_file: str = ""
    ca_cert: str = ""
    timeout_seconds: float = DEFAULT_VAULT_TIMEOUT_SECONDS

    def __repr__(self) -> str:
        return (
            f"VaultBackendConfig(address={self.address!r}, auth={self.auth!r}, "
            f"auth_mount={self.auth_mount!r}, mount={self.mount!r}, "
            f"namespace={self.namespace!r})"
        )


@dataclass(frozen=True)
class SecretsConfig:
    """The whole ``[secrets]`` section, validated."""

    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    file: FileBackendConfig | None = None
    vault: VaultBackendConfig | None = None


def _text(section: dict[str, Any], key: str, *, where: str, default: str = "") -> str:
    raw = section.get(key, default)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise SecretConfigError(f"config.toml [{where}] {key}: must be a string")
    return raw.strip()


def _number(section: dict[str, Any], key: str, *, where: str, default: float) -> float:
    raw = section.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SecretConfigError(f"config.toml [{where}] {key}: must be a number")
    if raw <= 0:
        raise SecretConfigError(f"config.toml [{where}] {key}: must be positive")
    return float(raw)


def _file_config(section: Any, *, config_path: Path | None) -> FileBackendConfig | None:
    if section is None:
        return None
    if not isinstance(section, dict):
        raise SecretConfigError("config.toml [secrets.file]: must be a table")
    root = _text(section, "root", where="secrets.file")
    if not root:
        raise SecretConfigError(
            "config.toml [secrets.file] root: required; secret://file/... references resolve "
            "only to files under this directory"
        )
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        raise SecretConfigError("config.toml [secrets.file] root: must be an absolute path")
    # The root is the trust boundary for everyone allowed to edit a model or
    # MCP server, so it must never contain the file that holds the keyring,
    # the JWT secret and the database URL.
    if config_path is not None and _under(root, str(config_path)):
        raise SecretConfigError(
            "config.toml [secrets.file] root: must not contain config.toml itself; "
            "use a dedicated directory for secret files"
        )
    return FileBackendConfig(root=root)


def _vault_config(section: Any) -> VaultBackendConfig | None:
    if section is None:
        return None
    if not isinstance(section, dict):
        raise SecretConfigError("config.toml [secrets.vault]: must be a table")
    where = "secrets.vault"
    known = {
        "address",
        "auth",
        "auth_mount",
        "mount",
        "namespace",
        "role",
        "jwt_path",
        "role_id",
        "secret_id",
        "secret_id_file",
        "ca_cert",
        "timeout_seconds",
    }
    unknown = sorted(k for k in section if k not in known)
    if unknown:
        raise SecretConfigError(
            f"config.toml [secrets.vault]: unknown key(s) {', '.join(unknown)}; known: "
            f"{', '.join(sorted(known))}"
        )
    address = _text(section, "address", where=where)
    if not address:
        raise SecretConfigError("config.toml [secrets.vault] address: required")
    if not address.startswith(("http://", "https://")):
        raise SecretConfigError("config.toml [secrets.vault] address: must start with http(s)://")
    auth = _text(section, "auth", where=where)
    if auth not in VAULT_AUTH_METHODS:
        raise SecretConfigError(
            f"config.toml [secrets.vault] auth: must be one of {', '.join(VAULT_AUTH_METHODS)}"
        )
    mount = _text(section, "mount", where=where, default="secret").strip("/")
    if not mount:
        raise SecretConfigError("config.toml [secrets.vault] mount: must not be empty")
    auth_mount = _text(section, "auth_mount", where=where).strip("/") or auth
    role = _text(section, "role", where=where)
    jwt_path = _text(section, "jwt_path", where=where)
    role_id = _text(section, "role_id", where=where)
    secret_id = _text(section, "secret_id", where=where)
    secret_id_file = _text(section, "secret_id_file", where=where)
    if auth == "jwt":
        if not role:
            raise SecretConfigError("config.toml [secrets.vault] role: required for auth = 'jwt'")
        if not jwt_path:
            raise SecretConfigError(
                "config.toml [secrets.vault] jwt_path: required for auth = 'jwt' (the file "
                "holding the workload's JWT, e.g. a projected service-account token)"
            )
    else:
        if not role_id:
            raise SecretConfigError(
                "config.toml [secrets.vault] role_id: required for auth = 'approle'"
            )
        if bool(secret_id) == bool(secret_id_file):
            raise SecretConfigError(
                "config.toml [secrets.vault]: auth = 'approle' needs exactly one of secret_id "
                "or secret_id_file"
            )
    ca_cert = _text(section, "ca_cert", where=where)
    if ca_cert:
        ca_cert = os.path.expanduser(ca_cert)
        if not os.path.isfile(ca_cert):
            raise SecretConfigError(f"config.toml [secrets.vault] ca_cert: {ca_cert} is not a file")
    return VaultBackendConfig(
        address=address.rstrip("/"),
        auth=auth,
        auth_mount=auth_mount,
        mount=mount,
        namespace=_text(section, "namespace", where=where),
        role=role,
        jwt_path=os.path.expanduser(jwt_path) if jwt_path else "",
        role_id=role_id,
        secret_id=secret_id,
        secret_id_file=os.path.expanduser(secret_id_file) if secret_id_file else "",
        ca_cert=ca_cert,
        timeout_seconds=_number(
            section, "timeout_seconds", where=where, default=DEFAULT_VAULT_TIMEOUT_SECONDS
        ),
    )


def parse_secrets_config(section: Any, *, config_path: Path | None = None) -> SecretsConfig:
    """Validate the ``[secrets]`` table (``{}`` or ``None`` when absent).

    *config_path* is the active config.toml, used to refuse a file root that
    would expose it.
    """
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise SecretConfigError("config.toml [secrets]: must be a table")
    known = {"cache_ttl_seconds", "file", "vault"}
    unknown = sorted(k for k in section if k not in known)
    if unknown:
        raise SecretConfigError(
            f"config.toml [secrets]: unknown key(s) {', '.join(unknown)}; known: "
            f"{', '.join(sorted(known))}"
        )
    file = _file_config(section.get("file"), config_path=config_path)
    vault = _vault_config(section.get("vault"))
    if file is not None and vault is not None:
        # The store's own login credential must stay out of reach of a
        # reference, or a reference could hand it to an endpoint of its author's
        # choosing.
        for key, credential in (
            ("secret_id_file", vault.secret_id_file),
            ("jwt_path", vault.jwt_path),
        ):
            if credential and _under(file.root, credential):
                raise SecretConfigError(
                    f"config.toml [secrets.vault] {key}: must not be under the [secrets.file] "
                    "root, where a secret://file reference could read it"
                )
    return SecretsConfig(
        cache_ttl_seconds=_number(
            section, "cache_ttl_seconds", where="secrets", default=DEFAULT_CACHE_TTL_SECONDS
        ),
        file=file,
        vault=vault,
    )


def _under(root: str, path: str) -> bool:
    try:
        real_root, real_path = Path(root).resolve(), Path(path).resolve()
    except OSError:
        real_root, real_path = Path(root), Path(path)
    return real_path == real_root or real_path.is_relative_to(real_root)
