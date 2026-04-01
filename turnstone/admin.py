"""CLI admin commands for user and token management.

Entry point: turnstone-admin
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Any


def _get_storage() -> Any:
    """Initialize and return the storage backend."""
    from turnstone.core.storage import init_storage

    db_backend = os.environ.get("TURNSTONE_DB_BACKEND", "sqlite")
    db_url = os.environ.get("TURNSTONE_DB_URL", "")
    db_path = os.environ.get("TURNSTONE_DB_PATH", "")
    return init_storage(db_backend, path=db_path, url=db_url)


def _cmd_create_user(args: argparse.Namespace) -> None:
    import getpass

    from turnstone.core.auth import (
        generate_token,
        hash_password,
        hash_token,
        is_valid_username,
        token_prefix,
    )

    if not is_valid_username(args.username):
        print("Error: invalid username (1-64 chars: letters, digits, . _ -)", file=sys.stderr)
        sys.exit(1)

    storage = _get_storage()
    user_id = uuid.uuid4().hex

    # Prompt for password
    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Error: passwords do not match", file=sys.stderr)
            sys.exit(1)

    pw_hash = hash_password(password)
    storage.create_user(user_id, args.username, args.name, pw_hash)
    print(f"Created user: {user_id}")
    print(f"  Username: {args.username}")
    print(f"  Name: {args.name}")

    if args.token:
        scopes = args.scopes or "read,write,approve"
        raw = generate_token()
        tid = uuid.uuid4().hex
        storage.create_api_token(
            token_id=tid,
            token_hash=hash_token(raw),
            token_prefix=token_prefix(raw),
            user_id=user_id,
            name="initial",
            scopes=scopes,
        )
        print(f"\n  Token: {raw}")
        print(f"  Token ID: {tid}")
        print(f"  Scopes: {scopes}")
        print("  (Save this token now — it cannot be retrieved again)")


def _cmd_create_token(args: argparse.Namespace) -> None:
    from turnstone.core.auth import generate_token, hash_token, token_prefix

    storage = _get_storage()

    if storage.get_user(args.user) is None:
        print(f"Error: user {args.user} not found", file=sys.stderr)
        sys.exit(1)

    expires = None
    if args.expires_days:
        from datetime import UTC, datetime, timedelta

        expires = (datetime.now(UTC) + timedelta(days=args.expires_days)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

    raw = generate_token()
    tid = uuid.uuid4().hex
    storage.create_api_token(
        token_id=tid,
        token_hash=hash_token(raw),
        token_prefix=token_prefix(raw),
        user_id=args.user,
        name=args.name or "",
        scopes=args.scopes,
        expires=expires,
    )
    print(f"Token: {raw}")
    print(f"  ID: {tid}")
    print(f"  Scopes: {args.scopes}")
    if expires:
        print(f"  Expires: {expires}")
    print("  (Save this token now — it cannot be retrieved again)")


def _cmd_list_users(args: argparse.Namespace) -> None:
    storage = _get_storage()
    users = storage.list_users()
    if not users:
        print("No users found.")
        return
    for u in users:
        print(f"  {u['user_id'][:12]}..  {u['display_name']}  ({u['created']})")


def _cmd_list_tokens(args: argparse.Namespace) -> None:
    storage = _get_storage()
    tokens = storage.list_api_tokens(args.user)
    if not tokens:
        print(f"No tokens found for user {args.user}.")
        return
    for t in tokens:
        exp = f"  expires={t['expires']}" if t.get("expires") else ""
        print(
            f"  {t['token_id'][:12]}..  {t['token_prefix']}..  scopes={t['scopes']}"
            f"  name={t['name']}{exp}"
        )


def _cmd_revoke_token(args: argparse.Namespace) -> None:
    storage = _get_storage()
    if storage.delete_api_token(args.token_id):
        print(f"Revoked token {args.token_id}")
    else:
        print("Token not found", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# TLS commands
# ---------------------------------------------------------------------------


def _cmd_tls_bootstrap(args: argparse.Namespace) -> None:
    """Initialize CA and issue certs offline."""
    try:
        from lacme import CertificateAuthority, FileStore
    except ImportError:
        print("lacme not installed. Run: pip install turnstone[tls]", file=sys.stderr)
        sys.exit(1)

    import contextlib
    import os
    from pathlib import Path

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(PermissionError):
        os.chmod(out_dir, 0o700)  # Restrict access — contains CA private key

    store = FileStore(str(out_dir))
    ca = CertificateAuthority(store, name="turnstone")
    ca.init(cn="Turnstone CA", validity_days=3650)
    print(f"CA initialized in {out_dir} (permissions: 0700)")

    # Write CA cert to a well-known location
    ca_cert_path = out_dir / "ca.pem"
    ca_cert_path.write_bytes(ca.root_cert_pem)
    with contextlib.suppress(PermissionError):
        os.chmod(ca_cert_path, 0o644)
    print(f"CA cert: {ca_cert_path}")

    # Issue certs for requested domains
    for domain in args.issue:
        bundle = ca.issue([domain], validity_hours=48)
        store.save_cert(bundle)
        cert_dir = out_dir / "certs" / domain
        print(f"Issued: {domain} -> {cert_dir}")

    print(f"\nBootstrap complete. {len(args.issue)} cert(s) issued.")
    print(f"CA and certs written to: {out_dir}")


def _cmd_tls_issue(args: argparse.Namespace) -> None:
    """Request a cert from the console's ACME endpoint."""
    try:
        from lacme import SyncClient
    except ImportError:
        print("lacme not installed. Run: pip install turnstone[tls]", file=sys.stderr)
        sys.exit(1)

    import os
    from pathlib import Path

    console_url = args.console_url
    if not console_url:
        console_url = _discover_console_url()

    domains = [args.domain] + args.san
    directory_url = f"{console_url}/acme/directory"
    print(f"Requesting cert for {domains} from {directory_url}")

    client = SyncClient(
        directory_url=directory_url,
        allow_insecure=True,
    )
    bundle = client.issue(domains)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cert.pem").write_bytes(bundle.cert_pem)
    (out_dir / "fullchain.pem").write_bytes(bundle.fullchain_pem)
    (out_dir / "key.pem").write_bytes(bundle.key_pem)
    os.chmod(out_dir / "key.pem", 0o600)

    print(f"Certificate written to {out_dir}/")
    print("  cert.pem      (leaf certificate)")
    print("  fullchain.pem (cert + chain)")
    print("  key.pem       (private key, 0600)")


def _cmd_tls_ca_cert(args: argparse.Namespace) -> None:
    """Download the CA root certificate from the console."""
    import httpx

    console_url = args.console_url
    if not console_url:
        console_url = _discover_console_url()

    # Use plain HTTP for bootstrap (node may not have CA cert yet)
    # WARNING: This is trust-on-first-use (TOFU) — verify the fingerprint
    base = console_url.replace("https://", "http://")
    url = f"{base}/acme/ca.pem"
    print(f"Fetching CA cert from {url}")
    print("WARNING: Fetching over plain HTTP — verify the fingerprint below")

    resp = httpx.get(url)
    resp.raise_for_status()

    # Show fingerprint for out-of-band verification
    import hashlib

    fingerprint = hashlib.sha256(resp.content).hexdigest()
    print(f"CA cert SHA-256: {fingerprint}")

    from pathlib import Path

    Path(args.out).write_bytes(resp.content)
    print(f"CA cert written to {args.out}")


def _cmd_tls_list(args: argparse.Namespace) -> None:
    """List certificates from the console."""
    import httpx

    console_url = args.console_url
    if not console_url:
        console_url = _discover_console_url()

    url = f"{console_url}/v1/api/admin/tls/certs"
    headers = {}
    # Prefer JWT via ServiceTokenManager when JWT secret is available
    jwt_secret = os.environ.get("TURNSTONE_JWT_SECRET", "").strip()
    if jwt_secret:
        from turnstone.core.auth import JWT_AUD_CONSOLE, ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="admin-cli",
            scopes=frozenset({"read", "write", "approve", "service"}),
            source="cli",
            secret=jwt_secret,
            audience=JWT_AUD_CONSOLE,
        )
        headers["Authorization"] = f"Bearer {mgr.token}"
    resp = httpx.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    certs = data.get("certs", [])
    if not certs:
        print("No certificates issued.")
        return

    print(f"{'DOMAIN':<30s} {'ISSUED':<22s} {'EXPIRES':<22s}")
    print("-" * 74)
    for c in certs:
        print(f"{c['domain']:<30s} {c['issued_at']:<22s} {c['expires_at']:<22s}")


def _discover_console_url() -> str:
    """Discover console URL from the services table."""
    from turnstone.core.storage import get_storage

    try:
        storage = get_storage()
    except Exception:
        print(
            "No storage configured. Use --console-url or run from a "
            "directory with a turnstone database.",
            file=sys.stderr,
        )
        sys.exit(1)
    consoles = storage.list_services("console", max_age_seconds=3600)
    if not consoles:
        print(
            "No console found in services table. Use --console-url explicitly.",
            file=sys.stderr,
        )
        sys.exit(1)
    return consoles[0]["url"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for turnstone-admin CLI."""
    parser = argparse.ArgumentParser(
        prog="turnstone-admin",
        description="Turnstone user and token administration",
    )
    sub = parser.add_subparsers(dest="command")

    p_cu = sub.add_parser("create-user", help="Create a new user")
    p_cu.add_argument("--username", required=True, help="Login username")
    p_cu.add_argument("--name", required=True, help="Display name")
    p_cu.add_argument("--password", default="", help="Password (prompted if not provided)")
    p_cu.add_argument("--token", action="store_true", help="Also create an initial API token")
    p_cu.add_argument("--scopes", default="read,write,approve", help="Scopes for initial token")

    p_ct = sub.add_parser("create-token", help="Create an API token for a user")
    p_ct.add_argument("--user", required=True, help="User ID")
    p_ct.add_argument("--name", default="", help="Human label for the token")
    p_ct.add_argument("--scopes", default="read,write", help="Comma-separated scopes")
    p_ct.add_argument("--expires-days", type=int, default=None, help="Days until expiry")

    sub.add_parser("list-users", help="List all users")

    p_lt = sub.add_parser("list-tokens", help="List tokens for a user")
    p_lt.add_argument("--user", required=True, help="User ID")

    p_rt = sub.add_parser("revoke-token", help="Revoke an API token")
    p_rt.add_argument("--token-id", required=True, help="Token ID to revoke")

    # TLS subcommands
    p_bootstrap = sub.add_parser(
        "tls-bootstrap",
        help="Initialize CA and issue certs offline (no running console needed)",
    )
    p_bootstrap.add_argument("--out", required=True, help="Output directory for PEM files")
    p_bootstrap.add_argument(
        "--issue",
        action="append",
        default=[],
        help="Domain to issue cert for (repeatable)",
    )

    p_issue = sub.add_parser("tls-issue", help="Request cert from console ACME")
    p_issue.add_argument("domain", help="Primary domain for the certificate")
    p_issue.add_argument("--san", action="append", default=[], help="Additional SAN (repeatable)")
    p_issue.add_argument("--out", default=".", help="Output directory for PEM files")
    p_issue.add_argument(
        "--console-url", default="", help="Console URL (discovered from DB if empty)"
    )

    p_cacert = sub.add_parser("tls-ca-cert", help="Download CA root certificate")
    p_cacert.add_argument("--out", default="ca.pem", help="Output file path")
    p_cacert.add_argument("--console-url", default="", help="Console URL")

    p_tlslist = sub.add_parser("tls-list", help="List issued certificates")
    p_tlslist.add_argument("--console-url", default="", help="Console URL")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "create-user": _cmd_create_user,
        "create-token": _cmd_create_token,
        "list-users": _cmd_list_users,
        "list-tokens": _cmd_list_tokens,
        "revoke-token": _cmd_revoke_token,
        "tls-bootstrap": _cmd_tls_bootstrap,
        "tls-issue": _cmd_tls_issue,
        "tls-ca-cert": _cmd_tls_ca_cert,
        "tls-list": _cmd_tls_list,
    }
    dispatch[args.command](args)
