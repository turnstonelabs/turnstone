"""Create shared OAuth/SSO bootstrap config; used by run.sh and Docker operators."""

from __future__ import annotations

import argparse
import json
import os
import pwd
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


def create_config(path: Path, origin: str, *, owner: str | None = None) -> None:
    """Write once, without exposing the key in output or replacing existing config.

    The Docker bootstrap runs as root and assigns the file to the image's
    service account, including under user-namespace / rootless UID mapping.
    """
    origin = origin.rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in origin
        or "#" in origin
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in origin)
    ):
        raise ValueError("Use an HTTPS origin only, such as https://turnstone.example.com")
    # urlsplit accepts a non-numeric / out-of-range port until this property is read.
    _ = parsed.port
    account = pwd.getpwnam(owner) if owner else None
    content = (
        "# Shared by the console and every server node. Back up this file privately.\n"
        "[security]\n"
        f'mcp_token_encryption_key = "{Fernet.generate_key().decode()}"\n\n'
        "[oidc]\n"
        f"redirect_base = {json.dumps(origin, ensure_ascii=False)}\n"
        "\n# Optional SSO: configure all three fields after creating a local admin.\n"
        '# issuer = "https://identity.example.com"\n'
        '# client_id = "your-client-id"\n'
        '# client_secret = "your-client-secret"\n'
        "# password_enabled = true\n"
        "\n# Optional delegation of SSO credentials to MCP/model backends.\n"
        "# capture_user_credential = false # Opt in explicitly to store refresh tokens\n"
        '# obo_grant_profile = "entra"    # "entra" or "rfc8693", depending on the IdP\n'
        "# Setup: docs/docker.md#shared-bootstrap-config and docs/oidc.md\n"
    )
    # O_EXCL also refuses symlinks. Set private permissions before writing secrets.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            if account:
                os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
            stream.write(content)
    except BaseException:
        path.unlink()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("origin", help="Public HTTPS origin of the Turnstone dashboard")
    parser.add_argument("--owner", help="File owner inside the container (use turnstone)")
    args = parser.parse_args()
    try:
        create_config(args.path, args.origin, owner=args.owner)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Cannot create shared config: {exc}\n")


if __name__ == "__main__":
    main()
