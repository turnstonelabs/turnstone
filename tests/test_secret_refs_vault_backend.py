"""The Vault/OpenBao backend against an in-process fake that answers with the
response shapes recorded from Vault 2.1.2 and OpenBao 2.5.0 development servers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from turnstone.core.secret_refs._config import VaultBackendConfig, parse_secrets_config
from turnstone.core.secret_refs._errors import SecretBackendError, SecretReferenceError
from turnstone.core.secret_refs._vault import VaultBackend

if TYPE_CHECKING:
    from pathlib import Path


class FakeStore:
    """A tiny KV v2 store with approle and jwt auth, answering as the real ones do."""

    def __init__(self) -> None:
        self.kv2: dict[str, dict[str, Any]] = {"turnstone/openai": {"api_key": "sk-v2", "n": 1}}
        self.policy_paths = {"turnstone/"}
        self.sealed = False
        self.lease = 1200
        self.tokens: dict[str, bool] = {}
        self.logins: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[httpx2.Request] = []
        self.counter = 0

    def _auth(self) -> dict[str, Any]:
        self.counter += 1
        token = f"s.login{self.counter}"
        self.tokens[token] = True
        return {"client_token": token, "lease_duration": self.lease, "renewable": True}

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if self.sealed:
            return httpx2.Response(503, json={"errors": ["Vault is sealed"]})
        if path.startswith("/v1/auth/") and path.endswith("/login"):
            body = json.loads(request.content or b"{}")
            self.logins.append((path, body))
            if path == "/v1/auth/approle/login":
                if body.get("secret_id") != "good-secret":
                    return httpx2.Response(400, json={"errors": ["invalid role or secret ID"]})
                return httpx2.Response(200, json={"auth": self._auth()})
            if path in ("/v1/auth/jwt/login", "/v1/auth/kubernetes/login"):
                if body.get("role") != "turnstone":
                    return httpx2.Response(
                        400, json={"errors": [f'role "{body.get("role")}" could not be found']}
                    )
                return httpx2.Response(200, json={"auth": self._auth()})
            return httpx2.Response(403, json={"errors": ["permission denied"]})
        token = request.headers.get("X-Vault-Token", "")
        if not self.tokens.get(token):
            return httpx2.Response(403, json={"errors": ["permission denied"]})
        if path.startswith("/v1/secret/data/"):
            rel = path[len("/v1/secret/data/") :]
            if not any(rel.startswith(p) for p in self.policy_paths):
                return httpx2.Response(
                    403, json={"errors": ["1 error occurred:\n\t* permission denied\n\n"]}
                )
            if rel not in self.kv2:
                return httpx2.Response(404, json={"errors": []})
            return httpx2.Response(
                200, json={"data": {"data": self.kv2[rel], "metadata": {"version": 2}}}
            )
        if path.startswith("/v1/kv1/"):
            # A KV v1 mount answers the v2 path shape with a flat body.
            return httpx2.Response(200, json={"data": {"api_key": "flat"}})
        return httpx2.Response(404, json={"errors": []})


def _config(**overrides: Any) -> VaultBackendConfig:
    section: dict[str, Any] = {
        "address": "https://vault.example.com:8200",
        "auth": "approle",
        "role_id": "rid",
        "secret_id": "good-secret",
    }
    section.update(overrides)
    cfg = parse_secrets_config({"vault": section})
    assert cfg.vault is not None
    return cfg.vault


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def _backend(
    store: FakeStore, config: VaultBackendConfig | None = None, clock: FakeClock | None = None
) -> VaultBackend:
    return VaultBackend(
        config or _config(),
        transport=httpx2.MockTransport(store.handle),
        clock=clock or FakeClock(),
    )


class TestReads:
    def test_kv2_read_takes_data_data(self, store: FakeStore) -> None:
        assert _backend(store).fetch("/turnstone/openai", "api_key") == "sk-v2"
        assert store.requests[-1].url.path == "/v1/secret/data/turnstone/openai"
        assert "X-Vault-Namespace" not in store.requests[-1].headers

    def test_namespace_header_only_when_configured(self, store: FakeStore) -> None:
        _backend(store, _config(namespace="team-a")).fetch("turnstone/openai", "api_key")
        assert store.requests[-1].headers["X-Vault-Namespace"] == "team-a"
        assert store.requests[0].headers["X-Vault-Namespace"] == "team-a"  # the login too

    def test_field_required(self, store: FakeStore) -> None:
        with pytest.raises(SecretReferenceError, match="field name after '#'"):
            _backend(store).fetch("turnstone/openai", None)

    def test_missing_secret_is_definitive(self, store: FakeStore) -> None:
        with pytest.raises(SecretBackendError, match="not found") as info:
            _backend(store).fetch("turnstone/nope", "api_key")
        assert info.value.retryable is False

    def test_missing_and_non_string_fields(self, store: FakeStore) -> None:
        with pytest.raises(SecretBackendError, match="not present"):
            _backend(store).fetch("turnstone/openai", "other")
        with pytest.raises(SecretBackendError, match="not a string"):
            _backend(store).fetch("turnstone/openai", "n")

    def test_kv1_mount_reports_no_data(self, store: FakeStore) -> None:
        store.policy_paths.add("")
        backend = _backend(store, _config(mount="kv1"))
        with pytest.raises(SecretBackendError, match="KV v2"):
            backend.fetch("flat", "api_key")

    def test_sealed_store_is_retryable_without_address_in_message(self, store: FakeStore) -> None:
        store.sealed = True
        with pytest.raises(SecretBackendError, match="login failed") as info:
            _backend(store).fetch("turnstone/openai", "api_key")
        assert info.value.retryable is True
        assert "vault.example.com" not in str(info.value)
        assert "sealed" in info.value.detail

    def test_sealed_after_login_is_retryable(self, store: FakeStore) -> None:
        backend = _backend(store)
        backend.fetch("turnstone/openai", "api_key")
        store.sealed = True
        with pytest.raises(SecretBackendError, match="store unavailable") as info:
            backend.fetch("turnstone/openai", "api_key")
        assert info.value.retryable is True

    def test_unreachable_is_retryable(self) -> None:
        def boom(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=request)

        backend = VaultBackend(_config(), transport=httpx2.MockTransport(boom), clock=FakeClock())
        with pytest.raises(SecretBackendError, match="unreachable") as info:
            backend.fetch("turnstone/openai", "api_key")
        assert info.value.retryable is True
        assert "vault.example.com" not in str(info.value)

    def test_rate_limited_is_retryable(self) -> None:
        state = {"n": 0}

        def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/login"):
                return httpx2.Response(
                    200, json={"auth": {"client_token": "t", "lease_duration": 0}}
                )
            state["n"] += 1
            return httpx2.Response(429, json={"errors": ["rate limit"]})

        backend = VaultBackend(_config(), transport=httpx2.MockTransport(handle), clock=FakeClock())
        with pytest.raises(SecretBackendError, match="store unavailable") as info:
            backend.fetch("turnstone/openai", "api_key")
        assert info.value.retryable is True


class TestLogins:
    def test_approle_login_then_read_reuses_the_token(self, store: FakeStore) -> None:
        backend = _backend(store)
        assert backend.fetch("turnstone/openai", "api_key") == "sk-v2"
        assert store.logins == [
            ("/v1/auth/approle/login", {"role_id": "rid", "secret_id": "good-secret"})
        ]
        backend.fetch("turnstone/openai", "api_key")
        assert len(store.logins) == 1

    def test_approle_secret_id_file(self, store: FakeStore, tmp_path: Path) -> None:
        secret_file = tmp_path / "secret-id"
        secret_file.write_text("good-secret\n")
        cfg = _config(secret_id="", secret_id_file=str(secret_file))
        assert _backend(store, cfg).fetch("turnstone/openai", "api_key") == "sk-v2"

    def test_approle_bad_secret_is_a_login_failure(self, store: FakeStore) -> None:
        with pytest.raises(SecretBackendError, match="approle login failed") as info:
            _backend(store, _config(secret_id="bad")).fetch("turnstone/openai", "api_key")
        assert info.value.retryable is True
        assert "invalid role or secret ID" in info.value.detail

    def test_jwt_login_reads_the_token_file(self, store: FakeStore, tmp_path: Path) -> None:
        jwt_file = tmp_path / "token"
        jwt_file.write_text("eyJ.jwt\n")
        cfg = _config(
            auth="jwt", role="turnstone", jwt_path=str(jwt_file), role_id="", secret_id=""
        )
        assert _backend(store, cfg).fetch("turnstone/openai", "api_key") == "sk-v2"
        assert store.logins == [("/v1/auth/jwt/login", {"role": "turnstone", "jwt": "eyJ.jwt"})]

    def test_kubernetes_workloads_log_in_through_auth_mount(
        self, store: FakeStore, tmp_path: Path
    ) -> None:
        jwt_file = tmp_path / "sa-token"
        jwt_file.write_text("sa.jwt")
        cfg = _config(
            auth="jwt",
            role="turnstone",
            jwt_path=str(jwt_file),
            auth_mount="kubernetes",
            role_id="",
            secret_id="",
        )
        _backend(store, cfg).fetch("turnstone/openai", "api_key")
        assert store.logins[0] == (
            "/v1/auth/kubernetes/login",
            {"role": "turnstone", "jwt": "sa.jwt"},
        )

    def test_missing_jwt_file(self, store: FakeStore, tmp_path: Path) -> None:
        cfg = _config(
            auth="jwt",
            role="turnstone",
            jwt_path=str(tmp_path / "absent"),
            role_id="",
            secret_id="",
        )
        with pytest.raises(SecretBackendError, match="workload JWT file cannot be read"):
            _backend(store, cfg).fetch("turnstone/openai", "api_key")

    def test_rejected_token_triggers_one_relogin_and_retry(self, store: FakeStore) -> None:
        backend = _backend(store)
        backend.fetch("turnstone/openai", "api_key")
        store.tokens.clear()  # revoke every token the store issued
        assert backend.fetch("turnstone/openai", "api_key") == "sk-v2"
        assert len(store.logins) == 2

    def test_permission_denied_after_relogin_is_definitive(self, store: FakeStore) -> None:
        store.kv2["other/db"] = {"password": "pw"}
        with pytest.raises(SecretBackendError, match="permission denied") as info:
            _backend(store).fetch("other/db", "password")
        assert info.value.retryable is False
        assert len(store.logins) == 2

    def test_logs_in_again_at_three_quarters_of_the_lease(self, store: FakeStore) -> None:
        clock = FakeClock()
        backend = _backend(store, clock=clock)
        backend.fetch("turnstone/openai", "api_key")
        clock.now += 1200 * 0.5
        backend.fetch("turnstone/openai", "api_key")
        assert len(store.logins) == 1
        clock.now += 1200 * 0.3
        backend.fetch("turnstone/openai", "api_key")
        assert len(store.logins) == 2
        assert not any(r.url.path == "/v1/auth/token/renew-self" for r in store.requests)

    def test_token_without_lease_is_kept(self, store: FakeStore) -> None:
        store.lease = 0
        clock = FakeClock()
        backend = _backend(store, clock=clock)
        backend.fetch("turnstone/openai", "api_key")
        clock.now += 10_000
        backend.fetch("turnstone/openai", "api_key")
        assert len(store.logins) == 1
