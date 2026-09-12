"""Offline checks for the live Entra setup and failure diagnostics."""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

HARNESS = Path(__file__).resolve().parents[1] / "scripts" / "obo-e2e"


@pytest.fixture
def modules(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.syspath_prepend(str(HARNESS))
    return SimpleNamespace(
        setup=importlib.import_module("entra_setup"),
        e2e=importlib.import_module("entra_e2e"),
        spike=importlib.import_module("entra_spike"),
        diagnostics=importlib.import_module("entra_diagnostics"),
    )


class AzureFixture:
    """Stateful Graph/CLI stand-in; every call stays in this process."""

    def __init__(self, setup: Any) -> None:
        self.setup = setup
        self.apps: list[dict[str, Any]] = []
        self.principals: list[dict[str, Any]] = []
        self.grants: list[dict[str, Any]] = []
        self.calls: list[tuple[str, ...]] = []
        self.reject_patch = False
        self.hide_grants = False
        self.secret = "synthetic-'$secret`with spaces"
        self.secret_creations = 0

    def __call__(self, *args: str) -> Any:
        self.calls.append(args)

        def arg(name: str) -> str:
            return args[args.index(name) + 1]

        if args[:2] == ("account", "show"):
            return {"tenantId": "test-tenant"}
        if args[:3] == ("ad", "app", "list"):
            return copy.deepcopy(
                [app for app in self.apps if app["displayName"] == arg("--display-name")]
            )
        if args[:3] == ("ad", "app", "create"):
            name = arg("--display-name")
            app: dict[str, Any] = {
                "id": name + "-object",
                "appId": name + "-app",
                "displayName": name,
                "api": {},
                "identifierUris": [],
                "requiredResourceAccess": [],
            }
            self.apps.append(app)
            return copy.deepcopy(app)
        if args[:3] == ("ad", "app", "show"):
            return copy.deepcopy(next(app for app in self.apps if app["appId"] == arg("--id")))
        if args[:3] == ("ad", "sp", "list"):
            app_id = arg("--filter").split("'")[1]
            return copy.deepcopy([sp for sp in self.principals if sp["appId"] == app_id])
        if args[:3] == ("ad", "sp", "create"):
            principal = {"id": arg("--id") + "-sp", "appId": arg("--id")}
            self.principals.append(principal)
            return copy.deepcopy(principal)
        if args[:3] == ("ad", "sp", "show"):
            principal = next(sp for sp in self.principals if sp["id"] == arg("--id"))
            app = next(app for app in self.apps if app["appId"] == principal["appId"])
            return {
                **principal,
                "oauth2PermissionScopes": copy.deepcopy(app["api"]["oauth2PermissionScopes"]),
            }
        if args[:4] == ("ad", "app", "permission", "list-grants"):
            return [] if self.hide_grants else copy.deepcopy(self.grants)
        if args[:4] == ("ad", "app", "credential", "reset"):
            assert "--append" in args, "setup must not invalidate other client credentials"
            self.secret_creations += 1
            return {"password": self.secret}
        if args[0] == "rest":
            body = json.loads(arg("--body"))
            url = arg("--url")
            if arg("--method") == "POST":
                self.grants.append({"id": f"grant-{len(self.grants)}", **body})
                return copy.deepcopy(self.grants[-1])
            if self.reject_patch:
                raise self.setup.SetupError("CannotDeleteOrUpdateEnabledEntitlement")
            if "/applications/" in url:
                app = next(app for app in self.apps if url.endswith("/" + app["id"]))
                if "api" in body:
                    app["api"].update(body.pop("api"))
                app.update(body)
            else:
                next(grant for grant in self.grants if url.endswith("/" + grant["id"])).update(body)
            return None
        raise AssertionError(f"Unexpected Azure command: {args}")


@pytest.fixture
def azure(modules: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> AzureFixture:
    fixture = AzureFixture(modules.setup)
    monkeypatch.setattr(modules.setup, "az", fixture)
    monkeypatch.setattr(modules.setup.time, "sleep", lambda _: None)
    return fixture


def test_setup_rerun_preserves_scopes_secret_and_repairs_stale_permissions(
    modules: SimpleNamespace,
    azure: AzureFixture,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = tmp_path / ".env"
    modules.setup.setup(env)
    original = env.read_bytes()
    scope_ids = [app["api"]["oauth2PermissionScopes"][0]["id"] for app in azure.apps[:3]]
    unrelated_scope = {"id": "other-scope", "value": "other.read", "isEnabled": True}
    azure.apps[0]["api"]["oauth2PermissionScopes"].append(unrelated_scope)
    client = azure.apps[3]
    client["requiredResourceAccess"][0]["resourceAccess"][0]["id"] = "rejected-scope-uuid"

    modules.setup.setup(env)

    assert env.read_bytes() == original
    assert env.stat().st_mode & 0o777 == 0o600
    assert azure.secret_creations == 1
    assert len(azure.apps) == 4
    assert len(azure.grants) == 2
    assert [app["api"]["oauth2PermissionScopes"][0]["id"] for app in azure.apps[:3]] == scope_ids
    assert azure.apps[0]["api"]["oauth2PermissionScopes"][1] == unrelated_scope
    assert client["requiredResourceAccess"][0]["resourceAccess"][0]["id"] == scope_ids[0]
    assert modules.setup.read_env(env)["ENTRA_CLIENT_SECRET"] == azure.secret
    assert azure.secret not in capsys.readouterr().out
    # Literal shell quoting must preserve special characters without execution.
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; test "$ENTRA_CLIENT_SECRET" = "$2"',
            "check",
            str(env),
            azure.secret,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


@pytest.mark.parametrize("failure", ["scope_update", "grant_readback", "consented_control"])
def test_setup_failure_preserves_previous_env_and_does_not_create_secret(
    modules: SimpleNamespace,
    azure: AzureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    env = tmp_path / ".env"
    modules.setup.setup(env)
    previous = env.read_bytes()
    if failure == "scope_update":
        azure.apps[0]["api"]["requestedAccessTokenVersion"] = 1
        azure.reject_patch = True
    elif failure == "grant_readback":
        azure.grants.clear()
        azure.hide_grants = True
    else:
        azure.grants.append(
            {
                "id": "control",
                "resourceId": azure.principals[2]["id"],
                "consentType": "Principal",
                "scope": "mcp.access",
            }
        )
    monkeypatch.setattr(modules.setup, "ENV_FILE", env)
    monkeypatch.setattr(sys, "argv", ["entra_setup.py", "setup"])

    assert modules.setup.main() == 1
    assert env.read_bytes() == previous
    assert azure.secret_creations == 1
    assert not list(tmp_path.glob(".env-*"))


def test_setup_merges_existing_grant_without_dropping_other_scopes(
    modules: SimpleNamespace,
    azure: AzureFixture,
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    modules.setup.setup(env)
    azure.grants[0]["scope"] = "other.read"
    modules.setup.setup(env)
    assert set(azure.grants[0]["scope"].split()) == {"other.read", "mcp.access"}
    assert len(azure.grants) == 2


def test_setup_rejects_duplicate_registrations_before_mutating(
    modules: SimpleNamespace,
    azure: AzureFixture,
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    modules.setup.setup(env)
    azure.apps.append(copy.deepcopy(azure.apps[0]))
    before = copy.deepcopy((azure.apps, azure.grants))
    with pytest.raises(modules.setup.SetupError, match="Multiple registrations"):
        modules.setup.setup(env)
    assert (azure.apps, azure.grants) == before


def test_setup_shell_entrypoint_propagates_azure_failure(tmp_path: Path) -> None:
    for name in ("entra_setup.sh", "entra_setup.py", "entra_diagnostics.py"):
        shutil.copy2(HARNESS / name, tmp_path / name)
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "az"
    fake.write_text("""#!/usr/bin/env python3
import json
import sys
args = sys.argv[1:]
if args[:2] == ["account", "show"]:
    data = {"tenantId": "test-tenant"}
elif args[:3] == ["ad", "app", "list"]:
    data = []
elif args[:3] == ["ad", "app", "create"]:
    name = args[args.index("--display-name") + 1]
    data = {"id": name + "-object", "appId": name + "-app", "api": {}, "identifierUris": []}
elif args[:3] == ["ad", "app", "show"]:
    data = {"id": "app-object"}
elif args[:1] == ["rest"]:
    print("CannotDeleteOrUpdateEnabledEntitlement", file=sys.stderr)
    sys.exit(23)
elif args[:4] == ["ad", "app", "credential", "reset"]:
    data = {"password": "synthetic-secret"}
else:
    data = {}
if "--query" in args:
    print(data.get(args[args.index("--query") + 1], ""))
else:
    print(json.dumps(data))
""")
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{binary}{os.pathsep}{os.environ['PATH']}")
    previous = b"export ENTRA_CLIENT_SECRET=previous-value\n"
    (tmp_path / ".env").write_bytes(previous)
    result = subprocess.run(
        ["bash", str(tmp_path / "entra_setup.sh"), "setup"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "CannotDeleteOrUpdateEnabledEntitlement" in result.stderr
    assert (tmp_path / ".env").read_bytes() == previous


def test_error_diagnostics_never_print_response_payload(modules: SimpleNamespace) -> None:
    summary = modules.diagnostics.failure_summary(
        401,
        {
            "error": "invalid_client",
            "error_codes": [7000215, "secret-code"],
            "error_description": "client_secret=secret-value refresh_token=secret-refresh",
            "access_token": "secret-access",
            "error_uri": "https://example.com/?secret=secret-uri",
        },
    )
    assert summary == "http=401 error=invalid_client aadsts=7000215"
    assert modules.diagnostics.failure_summary(400, {"error": "secret-value"}) == (
        "http=400 error=unknown aadsts=none"
    )


def _jwt(audience: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"aud": audience}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.signature"


@pytest.mark.parametrize("outcome", ["success", "invalid_client", "transport_error"])
def test_product_harness_uses_runtime_and_reports_upstream_failure(
    modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        scope = parse_qs(request.content.decode())["scope"][0]
        if outcome == "transport_error":
            raise httpx.ReadTimeout("secret-response-payload", request=request)
        if outcome == "invalid_client":
            return httpx.Response(
                401,
                json={
                    "error": "invalid_client",
                    "error_codes": [7000215],
                    "error_description": "secret-response-payload",
                },
            )
        if scope.startswith("api://aud-c/"):
            return httpx.Response(400, json={"error": "invalid_grant", "error_codes": [65001]})
        audience = scope.removesuffix("/.default")
        return httpx.Response(
            200,
            json={
                "access_token": _jwt(audience),
                "refresh_token": "rotated",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    monkeypatch.setattr(
        modules.e2e,
        "json_http_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(modules.e2e, "tempfile", SimpleNamespace(mkdtemp=lambda **_: str(tmp_path)))
    monkeypatch.setattr(modules.e2e, "RESULTS", [])
    asyncio.run(
        modules.e2e._run(
            {
                "ENTRA_TENANT_ID": "tenant",
                "ENTRA_CLIENT_ID": "client",
                "ENTRA_CLIENT_SECRET": "client-secret",
                "SPIKE_AUDIENCE_A": "api://aud-a",
                "SPIKE_AUDIENCE_B": "api://aud-b",
                "SPIKE_AUDIENCE_UNCONSENTED": "api://aud-c",
            },
            "original-refresh",
        )
    )
    output = capsys.readouterr().out
    assert "secret-response-payload" not in output
    assert "client-secret" not in output
    if outcome != "success":
        if outcome == "invalid_client":
            assert "http=401 error=invalid_client aadsts=7000215" in output
        else:
            assert "exception=ReadTimeout" in output
        assert "E1 mint A: kind=refresh_failed_transient" in output
        assert "token_requests=1" in output
        assert [status for status, _ in modules.e2e.RESULTS] == ["VERIFIED", "FAILED"]
    else:
        assert len(modules.e2e.RESULTS) == 8
        assert all(status == "VERIFIED" for status, _ in modules.e2e.RESULTS)


def test_spike_does_not_verify_rotation_after_failed_redemption(
    modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in {
        "ENTRA_TENANT_ID": "tenant",
        "ENTRA_CLIENT_ID": "client",
        "ENTRA_CLIENT_SECRET": "secret",
        "SPIKE_AUDIENCE_A": "api://a",
        "SPIKE_AUDIENCE_B": "api://b",
        "SPIKE_RUN_OBO": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SPIKE_AUDIENCE_UNCONSENTED", raising=False)
    monkeypatch.setattr(modules.spike, "RESULTS", [])
    monkeypatch.setattr(modules.spike, "interactive_login", lambda _: {"refresh_token": "refresh"})
    monkeypatch.setattr(
        modules.spike,
        "redeem",
        lambda *args: (
            401,
            {
                "error": "invalid_client",
                "error_codes": [7000215],
                "error_description": "secret-value",
            },
        ),
    )
    assert modules.spike.main() == 1
    output = capsys.readouterr().out
    assert "[ SKIPPED] V4 rotation" in output
    assert (
        "could not mint self-audience assertion: http=401 error=invalid_client aadsts=7000215"
        in output
    )
    assert "secret-value" not in output
