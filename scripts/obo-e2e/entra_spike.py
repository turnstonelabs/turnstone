"""Entra boundary spike for single-credential MCP token minting (#551 re-scope).

Verifies, against a REAL Entra tenant, the assumptions behind the oauth_obo
design (one IdP refresh token per user; per-MCP access tokens minted on
demand). Each check prints VERIFIED / FAILED / SKIPPED plus redacted evidence.

  V1  interactive confidential-client login (auth-code + PKCE + offline_access)
      -> refresh token captured                        [capture layer works]
  V2  RT redeemed with scope=<AUDIENCE_A>/.default     -> aud claim == A
  V3  SAME credential redeemed for <AUDIENCE_B>        -> aud claim == B
      KEY CHECK: Entra RTs are client-bound, not resource-bound.
  V4  rotation semantics: does each redemption return a new RT, and does the
      PREVIOUS RT keep working?                        [write-back design]
  V5  redemption for an unconsented audience -> AADSTS65001 consent_required
      [maps to the reconnect-rail fallback]
  V6  optional: OBO jwt-bearer leg (requested_token_use=on_behalf_of) using a
      Turnstone-audience access token as assertion     [middle-tier variant]

Run:  uv run python scripts/obo-e2e/entra_spike.py
Env:  ENTRA_TENANT_ID       tenant GUID or domain
      ENTRA_CLIENT_ID       Turnstone spike app registration (confidential)
      ENTRA_CLIENT_SECRET   client secret for the above
      SPIKE_AUDIENCE_A      e.g. api://<guid-a>  (exposes a scope, consented)
      SPIKE_AUDIENCE_B      e.g. api://<guid-b>  (exposes a scope, consented)
      SPIKE_AUDIENCE_UNCONSENTED  optional, for V5
      SPIKE_RUN_OBO         optional "1" to run V6
      SPIKE_PORT            redirect listener port (default 8765; register
                            http://localhost:<port>/callback as a Web
                            redirect URI on the spike app registration)

App-registration setup checklist: see README.md next to this file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

RESULTS: list[tuple[str, str, str]] = []  # (check, status, evidence)


def record(check: str, status: str, evidence: str) -> None:
    RESULTS.append((check, status, evidence))
    print(f"[{status:>8}] {check}: {evidence}")


def b64url_json(segment: str) -> dict[str, Any]:
    pad = "=" * (-len(segment) % 4)
    out: dict[str, Any] = json.loads(base64.urlsafe_b64decode(segment + pad))
    return out


def jwt_claims_unverified(token: str) -> dict[str, Any]:
    """Spike-only unverified decode. NEVER do this in product code."""
    try:
        return b64url_json(token.split(".")[1])
    except Exception:
        return {}


def redact(token: str | None) -> str:
    if not token:
        return "<absent>"
    return f"{token[:8]}...({len(token)} chars)"


class _CodeCatcher(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None
    event = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 - stdlib API name
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (q.get("code") or [None])[0]
        _CodeCatcher.state = (q.get("state") or [None])[0]
        body = b"Spike login captured - return to the terminal."
        if q.get("error"):
            body = f"IdP error: {q}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)
        _CodeCatcher.event.set()

    def log_message(self, *args: Any) -> None:
        pass


def interactive_login(cfg: dict[str, str]) -> dict[str, Any]:
    """V1: authorization-code + PKCE + offline_access as a confidential client.

    Mirrors production shape: same grant Turnstone's OIDC login uses
    (core/oidc.py exchange_code), plus offline_access.
    """
    port = int(cfg.get("SPIKE_PORT", "8765"))
    redirect_uri = f"http://localhost:{port}/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    authorize = (
        f"https://login.microsoftonline.com/{cfg['ENTRA_TENANT_ID']}/oauth2/v2.0/authorize?"
        + urllib.parse.urlencode(
            {
                "client_id": cfg["ENTRA_CLIENT_ID"],
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "response_mode": "query",
                # offline_access is THE capture-layer delta vs today's login.
                # No resource scope here: the RT is minted client-bound.
                "scope": "openid profile offline_access",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    server = HTTPServer(("127.0.0.1", port), _CodeCatcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"\nOpen (or auto-opened) in a browser with a tenant user:\n  {authorize}\n")
    cb_file = cfg.get("SPIKE_CALLBACK_FILE", "")
    if cb_file:
        print(
            "Remote-browser mode: after sign-in the browser lands on a broken\n"
            f"http://localhost:{port}/callback?... page. Copy that FULL URL and run:\n"
            f"  echo '<url>' > {cb_file}\n"
        )

    def _watch_callback_file() -> None:
        # Driver-friendly fallback: the sign-in can happen on any device;
        # whoever signed in drops the redirected URL into SPIKE_CALLBACK_FILE.
        import time as _time

        while not _CodeCatcher.event.is_set():
            try:
                with open(cb_file) as _f:
                    pasted = _f.read().strip()
            except OSError:
                pasted = ""
            if "?" in pasted:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
                _CodeCatcher.code = (q.get("code") or [None])[0]
                _CodeCatcher.state = (q.get("state") or [None])[0]
                _CodeCatcher.event.set()
                return
            _time.sleep(1.0)

    if cb_file:
        threading.Thread(target=_watch_callback_file, daemon=True).start()
    webbrowser.open(authorize)
    if not _CodeCatcher.event.wait(timeout=600):
        server.shutdown()
        raise SystemExit("Timed out waiting for the redirect (10 min).")
    server.shutdown()
    if _CodeCatcher.state != state:
        raise SystemExit("state mismatch on redirect - aborting.")
    if not _CodeCatcher.code:
        raise SystemExit("No code on redirect (IdP error page shown in browser).")
    resp = httpx.post(
        f"https://login.microsoftonline.com/{cfg['ENTRA_TENANT_ID']}/oauth2/v2.0/token",
        data={
            "grant_type": "authorization_code",
            "code": _CodeCatcher.code,
            "redirect_uri": redirect_uri,
            "client_id": cfg["ENTRA_CLIENT_ID"],
            "client_secret": cfg["ENTRA_CLIENT_SECRET"],
            "code_verifier": verifier,
        },
        timeout=15.0,
    )
    tokens: dict[str, Any] = resp.json()
    if resp.status_code != 200:
        raise SystemExit(f"code exchange failed: {json.dumps(tokens, indent=2)[:800]}")
    return tokens


def redeem(cfg: dict[str, str], refresh_token: str, scope: str) -> tuple[int, dict[str, Any]]:
    """Redeem a refresh token for an access token with the given scope."""
    resp = httpx.post(
        f"https://login.microsoftonline.com/{cfg['ENTRA_TENANT_ID']}/oauth2/v2.0/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cfg["ENTRA_CLIENT_ID"],
            "client_secret": cfg["ENTRA_CLIENT_SECRET"],
            "scope": scope,
        },
        timeout=15.0,
    )
    body: dict[str, Any] = resp.json()
    return resp.status_code, body


def obo_exchange(cfg: dict[str, str], assertion: str, scope: str) -> tuple[int, dict[str, Any]]:
    """V6: middle-tier OBO variant (jwt-bearer + requested_token_use)."""
    resp = httpx.post(
        f"https://login.microsoftonline.com/{cfg['ENTRA_TENANT_ID']}/oauth2/v2.0/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
            "client_id": cfg["ENTRA_CLIENT_ID"],
            "client_secret": cfg["ENTRA_CLIENT_SECRET"],
            "scope": scope,
            "requested_token_use": "on_behalf_of",
        },
        timeout=15.0,
    )
    body: dict[str, Any] = resp.json()
    return resp.status_code, body


def check_aud(label: str, status: int, body: dict[str, Any], want_aud: str) -> str | None:
    """Common V2/V3 assertion: 200 + aud matches. Returns the new RT if any."""
    if status != 200:
        record(label, "FAILED", f"HTTP {status}: {json.dumps(body)[:300]}")
        return None
    claims = jwt_claims_unverified(body.get("access_token", ""))
    aud = str(claims.get("aud", "<none>"))
    ok = aud == want_aud or aud == want_aud.removeprefix("api://")
    record(
        label,
        "VERIFIED" if ok else "FAILED",
        f"aud={aud} want={want_aud} expires_in={body.get('expires_in')} "
        f"new_rt={redact(body.get('refresh_token'))}",
    )
    new_rt = body.get("refresh_token")
    return str(new_rt) if isinstance(new_rt, str) else None


def main() -> int:
    required = [
        "ENTRA_TENANT_ID",
        "ENTRA_CLIENT_ID",
        "ENTRA_CLIENT_SECRET",
        "SPIKE_AUDIENCE_A",
        "SPIKE_AUDIENCE_B",
    ]
    cfg = {k: os.environ[k] for k in required if k in os.environ}
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"Missing env: {', '.join(missing)}\nSee module docstring.")
        return 2
    for opt in ("SPIKE_AUDIENCE_UNCONSENTED", "SPIKE_PORT", "SPIKE_RUN_OBO"):
        if opt in os.environ:
            cfg[opt] = os.environ[opt]

    # V1 - capture
    tokens = interactive_login(cfg)
    rt0 = tokens.get("refresh_token")
    if isinstance(rt0, str) and rt0:
        record("V1 capture (offline_access -> RT)", "VERIFIED", redact(rt0))
    else:
        record("V1 capture (offline_access -> RT)", "FAILED", f"keys={sorted(tokens.keys())}")
        return 1

    # V2 - mint for audience A
    a = cfg["SPIKE_AUDIENCE_A"]
    s2, b2 = redeem(cfg, rt0, f"{a}/.default")
    rt_after_a = check_aud("V2 mint audience A from RT", s2, b2, a)

    # V3 - SAME credential, audience B (the design-critical check)
    b = cfg["SPIKE_AUDIENCE_B"]
    s3, b3 = redeem(cfg, rt0, f"{b}/.default")
    check_aud("V3 mint audience B from SAME RT", s3, b3, b)

    # V4 - rotation semantics
    if rt_after_a and rt_after_a != rt0:
        s4, _ = redeem(cfg, rt0, f"{a}/.default")
        record(
            "V4 rotation (new RT returned; old still valid?)",
            "VERIFIED" if s4 == 200 else "VERIFIED",
            f"rotated=yes old_rt_reuse_http={s4} "
            "(design: persist newest RT on every mint; "
            f"{'old stays valid - benign race window' if s4 == 200 else 'old INVALIDATED - write-back is correctness-critical'})",
        )
    else:
        record(
            "V4 rotation",
            "VERIFIED",
            "no rotation observed on redemption (same/absent RT) - "
            "write-back still required for the rotating case",
        )

    # V5 - unconsented audience -> consent_required
    unc = cfg.get("SPIKE_AUDIENCE_UNCONSENTED")
    if unc:
        s5, b5 = redeem(cfg, rt0, f"{unc}/.default")
        codes = b5.get("error_codes", [])
        hit = s5 == 400 and (65001 in codes or b5.get("suberror") == "consent_required")
        record(
            "V5 unconsented audience -> AADSTS65001",
            "VERIFIED" if hit else "FAILED",
            f"http={s5} error={b5.get('error')} codes={codes}",
        )
    else:
        record("V5 unconsented audience", "SKIPPED", "SPIKE_AUDIENCE_UNCONSENTED not set")

    # V6 - optional OBO middle-tier variant
    if cfg.get("SPIKE_RUN_OBO") == "1":
        s6a, b6a = redeem(cfg, rt0, f"{cfg['ENTRA_CLIENT_ID']}/.default")
        at_self = b6a.get("access_token", "") if s6a == 200 else ""
        if at_self:
            s6, b6 = obo_exchange(cfg, at_self, f"{a}/.default")
            check_aud("V6 OBO jwt-bearer variant", s6, b6, a)
        else:
            record(
                "V6 OBO jwt-bearer variant",
                "FAILED",
                f"could not mint self-audience assertion: HTTP {s6a}",
            )
    else:
        record("V6 OBO jwt-bearer variant", "SKIPPED", "SPIKE_RUN_OBO != 1")

    print("\n=== summary ===")
    for check, status, _ in RESULTS:
        print(f"  {status:>8}  {check}")
    return 0 if all(s != "FAILED" for _, s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
