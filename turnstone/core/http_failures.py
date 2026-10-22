"""What a failed httpx2 request raises, for request sites that treat every failure alike, and
whether such a failure may have come after the server had the request."""

from __future__ import annotations

import ssl

import httpx2

# ``HTTPError`` covers transport failures and ``raise_for_status``. ``InvalidURL`` and
# ``UnicodeError`` are not ``HTTPError``s: httpx2 raises them while building a request it cannot
# send, such as one for a URL it cannot parse, a redirect's malformed Location, a host that isn't
# valid IDNA (``idna.IDNAError`` is a ``UnicodeError``) or a URL holding a lone surrogate.
# ``ssl.SSLError`` isn't one either: httpx2 maps a TLS failure during the handshake to
# ``ConnectError``, but an alert that comes after it, such as a TLS 1.3 server refusing the client's
# certificate, can reach an async client's first read unmapped (a sync client maps it to
# ``ReadError``). ``turnstone.sdk`` re-exports the tuple as public API, so a change here changes
# what the SDK's callers catch.
REQUEST_FAILURES = (httpx2.HTTPError, httpx2.InvalidURL, UnicodeError, ssl.SSLError)

# The ``code`` of a failed request's error when no reply was read after the request may have
# reached the server: it may have been processed, so a caller checks before retrying it.
UPSTREAM_NO_REPLY_CODE = "upstream_no_reply"

# Raised before the request is sent, so nothing reached the server: a connection that never opened,
# a pool or proxy that refused it, a URL httpx2 will not send to.
_NEVER_SENT = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
    httpx2.ProxyError,
    httpx2.UnsupportedProtocol,
    httpx2.InvalidURL,
)


def may_have_reached_server(exc: BaseException) -> bool:
    """Whether a request that failed with *exc* may have reached a Turnstone handler that acted on
    it, so that retrying it could repeat the action.

    A failure while opening a connection says no, as do ``InvalidURL`` and a ``UnicodeError`` other
    than a decode error: httpx2 raises those while building a request it then never sends (a URL it
    can't parse, a header or body that won't encode, a host that isn't valid IDNA). A failure after
    sending (a read that times out, a connection that closes, a reply that won't decode) may have
    come after the handler acted.

    httpx2 raises ``InvalidURL`` and encode errors on a 3xx reply too, while it resolves the
    Location, and a client that follows redirects meets the other failures on the next hop. Both
    still mean nothing was done: Turnstone's handlers never redirect a POST (only the OIDC and
    MCP OAuth logins redirect, on GET), so a 3xx comes from something in front of the node, such as
    an http-to-https proxy, that didn't run the request.
    """
    if isinstance(exc, _NEVER_SENT):
        return False
    return not isinstance(exc, UnicodeError) or isinstance(exc, UnicodeDecodeError)
