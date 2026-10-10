"""``secret://`` reference parsing and the ``[secrets]`` configuration loader."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turnstone.core.secret_refs import (
    SecretConfigError,
    SecretReferenceError,
    contains_reference_text,
    is_reference,
    parse_reference,
)
from turnstone.core.secret_refs._config import DEFAULT_CACHE_TTL_SECONDS, parse_secrets_config

if TYPE_CHECKING:
    from pathlib import Path


class TestParseReference:
    def test_vault_reference_with_field(self) -> None:
        ref = parse_reference("secret://vault/turnstone/openai#api_key")
        assert (ref.backend, ref.path, ref.key) == ("vault", "/turnstone/openai", "api_key")
        assert str(ref) == "secret://vault/turnstone/openai#api_key"

    def test_file_reference_without_key(self) -> None:
        ref = parse_reference("secret://file/run/secrets/openai")
        assert (ref.backend, ref.path, ref.key) == ("file", "/run/secrets/openai", None)

    def test_empty_segments_are_refused(self) -> None:
        with pytest.raises(SecretReferenceError, match="never empty"):
            parse_reference("secret://vault//a//b#k")

    def test_leading_whitespace_is_not_a_reference(self) -> None:
        # Callers strip submitted values; a padded string is a literal, not a reference.
        assert not is_reference(" secret://file/a/b")

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            ("sk-literal", "not a secret reference"),
            ("secret://", "backend name"),
            ("secret://Vault/x", "backend name"),
            ("secret://vault", "path"),
            ("secret://vault/", "path"),
            ("secret://vault/x?version=2", "query string"),
            ("secret://vault/x#", "'#' must be followed"),
            ("secret://vault/x#bad key", "field name"),
            ("secret://vault/turnstone/../sys/x#k", "never empty, '.' or '..'"),
            ("secret://vault/./x#k", "never empty"),
            ("secret://vault/a%2Fb#k", "path segments"),
            ("secret://vault/a b#k", "path segments"),
            ("secret://vault/${TEAM}/openai#k", "not expanded inside a reference"),
        ],
    )
    def test_malformed(self, value: str, fragment: str) -> None:
        with pytest.raises(SecretReferenceError, match=fragment):
            parse_reference(value)

    def test_is_reference_and_embedded_text(self) -> None:
        assert is_reference("secret://file/x")
        assert not is_reference("sk-abc")
        assert not is_reference("")
        assert not is_reference(None)
        assert contains_reference_text("Bearer secret://vault/x#k")
        assert not contains_reference_text("secret://vault/x#k")
        assert not contains_reference_text("Bearer abc")


class TestParseSecretsConfig:
    def test_absent_section_means_no_backends(self) -> None:
        cfg = parse_secrets_config(None)
        assert cfg.file is None and cfg.vault is None
        assert cfg.cache_ttl_seconds == DEFAULT_CACHE_TTL_SECONDS

    def test_unknown_key_is_refused(self) -> None:
        with pytest.raises(SecretConfigError, match="unknown key"):
            parse_secrets_config({"cache_ttl": 5})
        with pytest.raises(SecretConfigError, match="unknown key"):
            parse_secrets_config(
                {"vault": {"address": "https://v", "auth": "approle", "kv_version": 1}}
            )

    def test_file_root_required_and_absolute(self, tmp_path: Path) -> None:
        with pytest.raises(SecretConfigError, match="root: required"):
            parse_secrets_config({"file": {}})
        with pytest.raises(SecretConfigError, match="absolute"):
            parse_secrets_config({"file": {"root": "relative/dir"}})
        cfg = parse_secrets_config({"file": {"root": str(tmp_path)}})
        assert cfg.file is not None and cfg.file.root == str(tmp_path)

    def test_file_root_may_not_contain_config_toml(self, tmp_path: Path) -> None:
        config = tmp_path / "etc" / "config.toml"
        config.parent.mkdir()
        config.write_text("")
        with pytest.raises(SecretConfigError, match="must not contain config.toml"):
            parse_secrets_config({"file": {"root": str(tmp_path)}}, config_path=config)
        with pytest.raises(SecretConfigError, match="must not contain config.toml"):
            parse_secrets_config({"file": {"root": str(tmp_path / "etc")}}, config_path=config)
        ok = parse_secrets_config({"file": {"root": str(tmp_path / "run")}}, config_path=config)
        assert ok.file is not None

    def test_vault_credential_file_may_not_live_under_the_file_root(self, tmp_path: Path) -> None:
        vault = {"address": "https://v", "auth": "approle", "role_id": "r"}
        with pytest.raises(SecretConfigError, match="secret_id_file: must not be under"):
            parse_secrets_config(
                {
                    "file": {"root": str(tmp_path)},
                    "vault": {**vault, "secret_id_file": str(tmp_path / "vault-secret-id")},
                }
            )
        with pytest.raises(SecretConfigError, match="jwt_path: must not be under"):
            parse_secrets_config(
                {
                    "file": {"root": str(tmp_path / "secrets")},
                    "vault": {
                        "address": "https://v",
                        "auth": "jwt",
                        "role": "t",
                        "jwt_path": str(tmp_path / "secrets" / "token"),
                    },
                }
            )
        cfg = parse_secrets_config(
            {
                "file": {"root": str(tmp_path / "secrets")},
                "vault": {**vault, "secret_id_file": str(tmp_path / "vault-secret-id")},
            }
        )
        assert cfg.file is not None and cfg.vault is not None

    def test_vault_jwt_defaults(self, tmp_path: Path) -> None:
        cfg = parse_secrets_config(
            {
                "vault": {
                    "address": "https://vault.example.com:8200/",
                    "auth": "jwt",
                    "role": "ts",
                    "jwt_path": str(tmp_path / "token"),
                }
            }
        )
        assert cfg.vault is not None
        assert cfg.vault.address == "https://vault.example.com:8200"
        assert cfg.vault.mount == "secret"
        assert cfg.vault.auth_mount == "jwt"
        assert cfg.vault.timeout_seconds == 5.0

    def test_kubernetes_workloads_use_jwt_with_an_auth_mount(self) -> None:
        cfg = parse_secrets_config(
            {
                "vault": {
                    "address": "https://v",
                    "auth": "jwt",
                    "auth_mount": "/kubernetes/",
                    "role": "ts",
                    "jwt_path": "/var/run/secrets/kubernetes.io/serviceaccount/token",
                }
            }
        )
        assert cfg.vault is not None and cfg.vault.auth_mount == "kubernetes"

    @pytest.mark.parametrize(
        ("section", "fragment"),
        [
            ({"auth": "approle", "role_id": "r", "secret_id": "s"}, "address: required"),
            (
                {"address": "vault:8200", "auth": "approle", "role_id": "r", "secret_id": "s"},
                "http",
            ),
            ({"address": "https://v", "auth": "token"}, "auth: must be one of"),
            ({"address": "https://v", "auth": "kubernetes", "role": "r"}, "auth: must be one of"),
            ({"address": "https://v", "auth": "jwt", "jwt_path": "/t"}, "role: required"),
            ({"address": "https://v", "auth": "jwt", "role": "r"}, "jwt_path: required"),
            ({"address": "https://v", "auth": "approle"}, "role_id: required"),
            (
                {"address": "https://v", "auth": "approle", "role_id": "r"},
                "exactly one of secret_id",
            ),
            (
                {
                    "address": "https://v",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "s",
                    "secret_id_file": "/f",
                },
                "exactly one of secret_id",
            ),
            (
                {
                    "address": "https://v",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "s",
                    "mount": "/",
                },
                "mount",
            ),
            (
                {
                    "address": "https://v",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "s",
                    "ca_cert": "/nope.pem",
                },
                "ca_cert",
            ),
            (
                {
                    "address": "https://v",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "s",
                    "timeout_seconds": "5",
                },
                "number",
            ),
            (
                {
                    "address": "https://v",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "s",
                    "timeout_seconds": 0,
                },
                "positive",
            ),
        ],
    )
    def test_vault_validation(self, section: dict[str, object], fragment: str) -> None:
        with pytest.raises(SecretConfigError, match=fragment):
            parse_secrets_config({"vault": section})

    def test_vault_repr_hides_credentials(self) -> None:
        cfg = parse_secrets_config(
            {
                "vault": {
                    "address": "https://v",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "topsecret",
                }
            }
        )
        assert "topsecret" not in repr(cfg)

    def test_ca_cert_path_is_kept_when_it_exists(self, tmp_path: Path) -> None:
        pem = tmp_path / "ca.pem"
        pem.write_text("x")
        cfg = parse_secrets_config(
            {
                "vault": {
                    "address": "https://v",
                    "auth": "approle",
                    "role_id": "r",
                    "secret_id": "s",
                    "ca_cert": str(pem),
                    "namespace": "team-a",
                }
            }
        )
        assert cfg.vault is not None
        assert cfg.vault.ca_cert == str(pem)
        assert cfg.vault.namespace == "team-a"
