"""Replies that fail an httpx2 request, for the tests of each site that catches
``REQUEST_FAILURES``."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

import httpx2
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


def refuse(request: httpx2.Request) -> httpx2.Response:
    """Refuse the connection, as a peer that isn't listening does."""
    raise httpx2.ConnectError("Connection refused", request=request)


def time_out_connecting(request: httpx2.Request) -> httpx2.Response:
    """Time out opening the connection, as a peer that drops the handshake does."""
    raise httpx2.ConnectTimeout("timed out", request=request)


def time_out_reading(request: httpx2.Request) -> httpx2.Response:
    """Time out waiting for the reply to a request the peer received."""
    raise httpx2.ReadTimeout("timed out", request=request)


def disconnect(request: httpx2.Request) -> httpx2.Response:
    """Close the connection without a reply, as a peer that stops mid-request does."""
    raise httpx2.RemoteProtocolError(
        "Server disconnected without sending a response.", request=request
    )


def redirect_to(location: str) -> Callable[[httpx2.Request], httpx2.Response]:
    """A 302 to *location*, which httpx2 can't turn into a request even with redirects off."""
    return lambda request: httpx2.Response(302, headers={"Location": location})


def refuse_the_certificate(request: httpx2.Request) -> httpx2.Response:
    """Fail the first read with the alert a TLS 1.3 server sends after the handshake when it refuses
    the client's certificate, which an async httpx2 client can pass on as a raw ``ssl.SSLError`` (a
    sync one maps it to ``ReadError``)."""
    raise ssl.SSLError(
        1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required"
    )


# A peer the client can't reach, replies httpx2 can't follow (InvalidURL, and idna's IDNAError, a
# UnicodeError) and a TLS alert on read: a site that catches REQUEST_FAILURES and treats every
# failure alike treats them alike.
UNREACHABLE_REPLIES = pytest.mark.parametrize(
    "reply",
    [
        refuse,
        redirect_to("http:foo"),
        redirect_to("http://a..xn--b/"),
        refuse_the_certificate,
    ],
    ids=["refused", "unparseable-redirect", "not-idna-redirect", "tls-alert-on-read"],
)

# Sites that tell a request a Turnstone handler never got from one it may have acted on (see
# may_have_reached_server): failures to open the connection, and 3xx replies httpx2 can't follow,
# which come from something in front of the node; then failures after the request went out (a reply
# that never came, a connection closed without one, a TLS alert on read).
UNSENT_REPLIES = pytest.mark.parametrize(
    "reply",
    [refuse, time_out_connecting, redirect_to("http:foo"), redirect_to("http://a..xn--b/")],
    ids=["refused", "connect-timeout", "unparseable-redirect", "not-idna-redirect"],
)
MAYBE_PROCESSED_REPLIES = pytest.mark.parametrize(
    "reply",
    [time_out_reading, disconnect, refuse_the_certificate],
    ids=["read-timeout", "disconnected", "tls-alert-on-read"],
)
