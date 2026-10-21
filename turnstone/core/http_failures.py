"""What a failed httpx2 request raises, for request sites that treat every failure alike."""

from __future__ import annotations

import httpx2

# ``HTTPError`` covers transport failures and ``raise_for_status``. ``InvalidURL`` and
# ``UnicodeError`` are not ``HTTPError``s: httpx2 raises them while building a request it cannot
# send, such as one for a URL it cannot parse, a redirect's malformed Location, a host that isn't
# valid IDNA (``idna.IDNAError`` is a ``UnicodeError``) or a URL holding a lone surrogate.
REQUEST_FAILURES = (httpx2.HTTPError, httpx2.InvalidURL, UnicodeError)
