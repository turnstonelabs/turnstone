"""Replies that fail an httpx2 request, for the tests of each site that catches
``REQUEST_FAILURES``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


def refuse(request: httpx2.Request) -> httpx2.Response:
    """Refuse the connection, as a peer that isn't listening does."""
    raise httpx2.ConnectError("Connection refused", request=request)


def redirect_to(location: str) -> Callable[[httpx2.Request], httpx2.Response]:
    """A 302 to *location*, which httpx2 can't turn into a request even with redirects off."""
    return lambda request: httpx2.Response(302, headers={"Location": location})


# A peer the client can't reach, and replies httpx2 can't follow (InvalidURL, and idna's
# IDNAError, a UnicodeError): each site that catches REQUEST_FAILURES treats them alike.
UNREACHABLE_REPLIES = pytest.mark.parametrize(
    "reply",
    [refuse, redirect_to("http:foo"), redirect_to("http://a..xn--b/")],
    ids=["refused", "unparseable-redirect", "not-idna-redirect"],
)
