"""What a failed httpx2 request raises, for request sites that treat every failure alike."""

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
