"""HTTP header parsing helpers shared by the MCP client and OAuth modules.

Both ``mcp_client`` and ``mcp_oauth`` need to extract structured values from
``WWW-Authenticate: Bearer ...`` headers — ``mcp_client`` to classify
401/403 responses for the user-pool dispatcher, and ``mcp_oauth`` to pull
the ``resource_metadata`` URL out of a discovery challenge. This module
hosts the shared primitives so both modules can call them without
duplicating fragile substring scanners.

The earlier hand-rolled scanners (``_parse_www_authenticate_scope`` /
``_parse_www_authenticate_error`` in ``mcp_client``) used
``header.lower().find(needle, i)`` to locate parameter names. That made
them vulnerable to:

* matching ``scope`` inside ``xscope`` or ``ascope``,
* matching the literal text ``scope=...`` embedded inside the quoted
  ``realm`` value of a preceding ``auth-param``,
* O(N**2) behaviour on pathological input (each ``find`` rescans the prefix).

This module replaces those with a single tokenizer that walks the RFC 7235
``challenge → auth-param`` grammar once, tracks quoted-string state, and
returns a normalised ``{key.lower(): value}`` dict. The thin extraction
wrappers (``parse_www_authenticate_scope`` / ``parse_www_authenticate_error``)
preserve the original return shapes so call sites only need to swap the
import.
"""

from __future__ import annotations


def _parse_quoted_string(text: str, start: int) -> tuple[str, int] | None:
    """Parse an RFC 7230 ``quoted-string`` starting at ``text[start]``.

    Returns ``(value, end_index)`` where ``end_index`` is the index just
    past the closing quote, or ``None`` if the input is malformed (no
    opening quote, unterminated string).

    Handles ``\\"`` and ``\\\\`` escapes per RFC 7230 section 3.2.6 — the
    prior naive ``([^"]+)`` regex truncated the URL at the first
    unescaped quote and silently dropped backslash escapes from the
    value.
    """
    if start >= len(text) or text[start] != '"':
        return None
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None


# Maximum header length we'll attempt to parse. Real ASes emit a handful
# of short auth-params; anything past this is either malformed or
# adversarial. Returning ``{}`` (rather than raising) keeps callers' error
# paths uniform with "unparseable header → no signal".
_MAX_HEADER_LEN = 4096

_TOKEN_DELIMS = frozenset('()<>@,;:\\"/[]?={} \t')


def _is_token_char(ch: str) -> bool:
    """RFC 7230 token character: visible ASCII minus the delimiter set."""
    return ch.isascii() and ch.isprintable() and ch not in _TOKEN_DELIMS


def _looks_like_bearer_challenge_start(header: str, i: int) -> bool:
    """Peek at ``header[i:]`` for the start of a fresh ``Bearer`` challenge.

    Returns True when the slice begins with the case-insensitive token
    ``Bearer`` followed by whitespace — the RFC 7235 marker for a new
    ``challenge`` after a separator comma. This is the cue the bearer
    tokenizer uses to stop parsing rather than fold a second challenge's
    auth-params into the first challenge's dict.
    """
    n = len(header)
    if i + 6 > n:
        return False
    if header[i : i + 6].lower() != "bearer":
        return False
    after = i + 6
    # ``Bearer`` must be followed by whitespace to qualify as a scheme
    # boundary; ``Bearer-like-token`` is just a regular token.
    return after < n and header[after] in " \t"


def parse_www_authenticate_bearer(header: str) -> dict[str, str]:
    """Extract ``auth-param``s from a ``WWW-Authenticate: Bearer ...`` header.

    Walks the RFC 7235 challenge grammar once, returning a dict of
    ``{lowercased-key: value}`` pairs. Quoted-strings are unquoted (with
    backslash escapes resolved). Unknown / malformed input returns an
    empty dict — never raises.

    Only ``Bearer`` challenges are recognised. The function ignores any
    leading whitespace before the scheme. When a second ``Bearer``
    challenge appears after a separator comma — as it would when
    httpx joins repeated ``WWW-Authenticate`` headers via
    ``response.headers.get(...)`` — the tokenizer stops at the
    challenge boundary rather than folding the second challenge's
    auth-params into the first challenge's dict. This is the
    parser-side defence-in-depth mirror of the
    ``response.headers.get_list(...)[0]`` guard in the dispatcher's
    capturing httpx factory; either layer alone neutralises the
    multi-header injection vector but both run together so a
    regression in one cannot silently re-open it.
    A ``realm`` value that contains the literal text ``scope=fake`` is
    correctly attributed to ``realm`` because the tokenizer respects
    quoted-string boundaries.
    """
    if not header or len(header) > _MAX_HEADER_LEN:
        return {}

    n = len(header)
    i = 0

    # Skip leading whitespace then the ``Bearer`` scheme token.
    while i < n and header[i] in " \t":
        i += 1
    scheme_start = i
    while i < n and _is_token_char(header[i]):
        i += 1
    scheme = header[scheme_start:i]
    if scheme.lower() != "bearer":
        return {}
    # Require at least one space between scheme and first auth-param.
    if i >= n or header[i] not in " \t":
        return {}

    out: dict[str, str] = {}
    while i < n:
        # Skip whitespace and stray commas between params.
        while i < n and header[i] in " \t,":
            i += 1
        if i >= n:
            break
        # If a fresh ``Bearer`` challenge starts here, the upstream is
        # multi-challenge — stop before reading any of its auth-params.
        if _looks_like_bearer_challenge_start(header, i):
            break
        # Read the param key (a token).
        key_start = i
        while i < n and _is_token_char(header[i]):
            i += 1
        if i == key_start:
            # Not a valid token start — skip one char to make forward
            # progress and continue. This bounds total cost to O(N).
            i += 1
            continue
        key = header[key_start:i].lower()
        # Optional whitespace, then ``=``.
        while i < n and header[i] in " \t":
            i += 1
        if i >= n or header[i] != "=":
            # Param without a value — skip.
            continue
        i += 1
        while i < n and header[i] in " \t":
            i += 1
        if i >= n:
            break
        # Value: either a quoted-string or a token.
        if header[i] == '"':
            parsed = _parse_quoted_string(header, i)
            if parsed is None:
                # Unterminated quoted-string — treat the rest of the
                # header as garbage and stop. Returning what we already
                # have is safer than guessing where the value ends.
                break
            value, i = parsed
            out.setdefault(key, value)
        else:
            val_start = i
            while i < n and header[i] not in ", \t":
                i += 1
            value = header[val_start:i]
            out.setdefault(key, value)
    return out


def parse_www_authenticate_scope(header: str) -> tuple[str, ...]:
    """Return the ``scope=...`` value as a tuple of individual scopes.

    Splits on a single space per RFC 6749 section 3.3 (``scope-token``
    sequence). Returns ``()`` when the header is malformed or carries no
    ``scope`` parameter.

    Each token is validated against the RFC 6749 §3.3 ``scope-token``
    grammar (visible ASCII ``0x21..0x7E`` excluding ``"`` and ``\\``)
    so that a malicious or buggy AS cannot smuggle CR/LF/tab/control
    bytes through a future log or notification path. Today scopes are
    JSON-encoded everywhere downstream so no concrete exploit exists,
    but the validation is cheap and forecloses regressions in
    structured-error rendering.
    """
    params = parse_www_authenticate_bearer(header)
    value = params.get("scope")
    if not value:
        return ()
    return tuple(
        s
        for s in value.split(" ")
        if s and all(0x21 <= ord(c) <= 0x7E and c not in '"\\' for c in s)
    )


def parse_www_authenticate_error(header: str) -> str | None:
    """Return the ``error=...`` value or ``None`` when absent.

    The tokenizer naturally distinguishes ``error`` from
    ``error_description`` / ``error_uri`` because ``_`` is not a valid
    token-character delimiter — they parse as separate keys.
    """
    params = parse_www_authenticate_bearer(header)
    return params.get("error") or None
