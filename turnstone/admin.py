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
    }
    dispatch[args.command](args)
