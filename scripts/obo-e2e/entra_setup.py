"""Create or repair the throwaway Entra registrations used by both harnesses.

Uses only the standard library and an authenticated Azure CLI. Existing scope
identities and the saved client secret survive setup reruns. The environment is
published only after the delegated grants have been read back successfully.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from entra_diagnostics import report_source

GRAPH = "https://graph.microsoft.com/v1.0"
NAMES = ("spike-mcp-a", "spike-mcp-b", "spike-mcp-c", "spike-turnstone")
ENV_FILE = Path(__file__).with_name(".env")
ATTEMPTS = 6
RETRY_SECONDS = 5


class SetupError(Exception):
    """An incomplete setup must not publish a new environment."""


def az(*args: str) -> Any:
    result = subprocess.run(
        ["az", *args, "--output", "json"], capture_output=True, text=True, check=False
    )
    if result.returncode:
        # Never echo stdout: credential creation returns a secret there.
        operation = " ".join(args[:3])
        raise SetupError(f"az {operation} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def patch(kind: str, object_id: str, body: dict[str, Any]) -> None:
    az(
        "rest",
        "--method",
        "PATCH",
        "--url",
        f"{GRAPH}/{kind}/{object_id}",
        "--body",
        json.dumps(body),
    )


def ensure_app(name: str, *, client: bool = False) -> dict[str, Any]:
    apps: list[dict[str, Any]] = [
        app for app in az("ad", "app", "list", "--display-name", name) if app["displayName"] == name
    ]
    if len(apps) > 1:
        raise SetupError(
            f"Multiple registrations named {name}; resolve the duplicates before setup"
        )
    if apps:
        app: dict[str, Any] = az("ad", "app", "show", "--id", apps[0]["appId"])
        return app
    args = ["ad", "app", "create", "--display-name", name, "--sign-in-audience", "AzureADMyOrg"]
    if client:
        args += ["--web-redirect-uris", "http://localhost:8765/callback"]
    app = az(*args)
    return app


def ensure_sp(app_id: str) -> dict[str, Any]:
    principals: list[dict[str, Any]] = az("ad", "sp", "list", "--filter", f"appId eq '{app_id}'")
    if principals:
        return principals[0]
    principal: dict[str, Any] = az("ad", "sp", "create", "--id", app_id)
    return principal


def resource_app(name: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    app = ensure_app(name)
    api = app.get("api") or {}
    scopes = [dict(scope) for scope in api.get("oauth2PermissionScopes", [])]
    matches = [scope for scope in scopes if scope["value"] == "mcp.access"]
    if len(matches) > 1:
        raise SetupError(f"Multiple mcp.access scopes on {name}")
    if matches:
        scope = matches[0]
        scope["isEnabled"] = True
    else:
        scope = {
            "id": str(uuid.uuid4()),
            "value": "mcp.access",
            "type": "Admin",
            "isEnabled": True,
            "adminConsentDisplayName": f"Access {name}",
            "adminConsentDescription": f"Spike scope for {name}",
        }
        scopes.append(scope)
    uris = list(app.get("identifierUris") or [])
    uri = f"api://{app['appId']}"
    if uri not in uris:
        uris.append(uri)
    desired_api = {**api, "requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes}
    if uris != app.get("identifierUris") or any(
        api.get(key) != value for key, value in desired_api.items()
    ):
        patch("applications", app["id"], {"identifierUris": uris, "api": desired_api})
    principal = ensure_sp(app["appId"])
    for attempt in range(ATTEMPTS):
        observed = az("ad", "sp", "show", "--id", principal["id"])
        if any(
            item["id"] == scope["id"] and item.get("isEnabled")
            for item in observed.get("oauth2PermissionScopes", [])
        ):
            return app, scope["id"], principal
        if attempt + 1 < ATTEMPTS:
            time.sleep(RETRY_SECONDS)
    raise SetupError(f"{name}: mcp.access has not reached its service principal; retry setup later")


def grants(client_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = az("ad", "app", "permission", "list-grants", "--id", client_id)
    return result


def ensure_grant(
    client: dict[str, Any], principal: dict[str, Any], resource: dict[str, Any]
) -> None:
    existing = [
        grant
        for grant in grants(client["appId"])
        if grant["resourceId"] == resource["id"] and grant["consentType"] == "AllPrincipals"
    ]
    if len(existing) > 1:
        raise SetupError("Duplicate delegated grants; resolve them before setup")
    if existing:
        grant = existing[0]
        scopes = set(grant.get("scope", "").split())
        if "mcp.access" in scopes:
            return
        patch(
            "oauth2PermissionGrants",
            grant["id"],
            {"scope": " ".join(sorted(scopes | {"mcp.access"}))},
        )
    else:
        az(
            "rest",
            "--method",
            "POST",
            "--url",
            f"{GRAPH}/oauth2PermissionGrants",
            "--body",
            json.dumps(
                {
                    "clientId": principal["id"],
                    "resourceId": resource["id"],
                    "consentType": "AllPrincipals",
                    "scope": "mcp.access",
                }
            ),
        )
    for attempt in range(ATTEMPTS):
        if any(
            grant["resourceId"] == resource["id"]
            and grant["consentType"] == "AllPrincipals"
            and "mcp.access" in grant.get("scope", "").split()
            for grant in grants(client["appId"])
        ):
            return
        if attempt + 1 < ATTEMPTS:
            time.sleep(RETRY_SECONDS)
    raise SetupError("Delegated grant was not visible after creation; retry setup later")


def read_env(path: Path) -> dict[str, str]:
    """Read literal shell assignments without executing the saved environment."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == "export":
            words.pop(0)
        if not words:
            continue
        if len(words) != 1 or "=" not in words[0]:
            raise SetupError(".env must contain literal NAME=value assignments")
        key, value = words[0].split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise SetupError(".env contains an invalid variable name")
        if key.startswith(("ENTRA_", "SPIKE_")):
            values[key] = value
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".env-", delete=False
        ) as out:
            temporary = Path(out.name)
            for key, value in values.items():
                out.write(f"export {key}={shlex.quote(value)}\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def setup(env_file: Path) -> None:
    saved = read_env(env_file)
    tenant = az("account", "show")["tenantId"]
    print(">> ensuring resource apps and their scopes...", flush=True)
    resources = [resource_app(name) for name in NAMES[:3]]
    client = ensure_app(NAMES[3], client=True)
    principal = ensure_sp(client["appId"])

    # Replace only our A/B entries: an earlier broken setup may have requested
    # newly generated scope UUIDs that the resources never accepted.
    audience_ids = {app["appId"] for app, _, _ in resources[:2]}
    required = [
        item
        for item in client.get("requiredResourceAccess", [])
        if item["resourceAppId"] not in audience_ids
    ] + [
        {"resourceAppId": app["appId"], "resourceAccess": [{"id": scope_id, "type": "Scope"}]}
        for app, scope_id, _ in resources[:2]
    ]
    if required != client.get("requiredResourceAccess"):
        patch("applications", client["id"], {"requiredResourceAccess": required})
    print(">> ensuring delegated grants for a/b and verifying them...", flush=True)
    for _, _, resource in resources[:2]:
        ensure_grant(client, principal, resource)
    if any(grant["resourceId"] == resources[2][2]["id"] for grant in grants(client["appId"])):
        raise SetupError("Audience C already has consent; it cannot be the unconsented control")

    secret = ""
    if saved.get("ENTRA_TENANT_ID") == tenant and saved.get("ENTRA_CLIENT_ID") == client["appId"]:
        secret = saved.get("ENTRA_CLIENT_SECRET", "")
    if secret:
        print(">> reusing saved client secret", flush=True)
    else:
        print(">> adding a client secret (existing credentials are preserved)", flush=True)
        secret = az(
            "ad",
            "app",
            "credential",
            "reset",
            "--id",
            client["appId"],
            "--append",
            "--display-name",
            "spike",
            "--years",
            "1",
        )["password"]
        if not secret:
            raise SetupError("Azure returned an empty client secret")
    saved.update(
        ENTRA_TENANT_ID=tenant,
        ENTRA_CLIENT_ID=client["appId"],
        ENTRA_CLIENT_SECRET=secret,
        SPIKE_AUDIENCE_A=f"api://{resources[0][0]['appId']}",
        SPIKE_AUDIENCE_B=f"api://{resources[1][0]['appId']}",
        SPIKE_AUDIENCE_UNCONSENTED=f"api://{resources[2][0]['appId']}",
        SPIKE_RUN_OBO="1",
    )
    write_env(env_file, saved)
    print(">> verified grants and wrote .env (mode 600). Run both harnesses:")
    print("  source scripts/obo-e2e/.env")
    print("  UV_NO_SYNC=1 uv run python scripts/obo-e2e/entra_spike.py")
    print("  UV_NO_SYNC=1 uv run python scripts/obo-e2e/entra_e2e.py")


def cleanup(env_file: Path) -> None:
    for name in NAMES:
        for app in az("ad", "app", "list", "--display-name", name):
            if app["displayName"] == name:
                az("ad", "app", "delete", "--id", app["appId"])
    env_file.unlink(missing_ok=True)
    print(">> cleanup done (app registrations + .env removed)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "cleanup"))
    args = parser.parse_args()
    report_source(__file__)
    try:
        if args.command == "setup":
            setup(ENV_FILE)
        else:
            cleanup(ENV_FILE)
    except (SetupError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
