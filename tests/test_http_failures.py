"""The console's request sites catch every way an httpx2 request fails, and whether a failed
request may have reached the server."""

import ast
import pathlib

import pytest

import turnstone.console.coordinator_client as coordinator_client
import turnstone.console.server as console_server
from tests._http_failure_cases import MAYBE_PROCESSED_REPLIES, UNSENT_REPLIES


def _httpx2_catches(module) -> list[int]:
    """Lines of each ``except`` that names an ``httpx2`` class.

    ``httpx2.InvalidURL`` and ``UnicodeError`` are not ``httpx2.HTTPError``s, so a request site that
    catches ``HTTPError``, or a narrower httpx2 class, lets a request httpx2 can't build escape.
    These modules catch ``REQUEST_FAILURES`` instead.
    """
    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and node.type is not None
        and any(
            isinstance(name, ast.Attribute)
            and isinstance(name.value, ast.Name)
            and name.value.id == "httpx2"
            for name in ast.walk(node.type)
        )
    ]


@pytest.mark.parametrize(
    "module", [console_server, coordinator_client], ids=["server", "coordinator_client"]
)
def test_request_sites_catch_every_request_failure(module):
    assert _httpx2_catches(module) == [], (
        "catch REQUEST_FAILURES at a request site; to act on a status, check resp.status_code or "
        "isinstance inside that handler"
    )


def _failure(reply, url: str) -> BaseException:
    """The error a POST to *url* raises when *reply* answers it."""
    import httpx2

    from turnstone.core.http_failures import REQUEST_FAILURES

    with (
        httpx2.Client(transport=httpx2.MockTransport(reply)) as client,
        pytest.raises(REQUEST_FAILURES) as caught,
    ):
        client.post(url, json={})
    return caught.value


_URL = "http://node.example.com/v1/api/workstreams/new"


@UNSENT_REPLIES
def test_a_request_no_handler_got_never_reached_the_server(reply):
    from turnstone.core.http_failures import may_have_reached_server

    assert may_have_reached_server(_failure(reply, _URL)) is False


@MAYBE_PROCESSED_REPLIES
def test_a_failure_after_the_request_went_out_may_have_reached_it(reply):
    from turnstone.core.http_failures import may_have_reached_server

    assert may_have_reached_server(_failure(reply, _URL)) is True


@pytest.mark.parametrize(
    ("url", "kwargs"),
    [
        (_URL + chr(1), {}),
        ("http://a..xn--b/v1/api/workstreams/new", {}),
        (_URL, {"json": {"message": chr(0xD83D)}}),
        (_URL, {"json": {}, "headers": {"X-Note": "caf" + chr(0xE9)}}),
    ],
    ids=["unparseable-url", "not-idna-host", "lone-surrogate-body", "non-ascii-header"],
)
def test_a_request_httpx2_cannot_build_never_reached_the_server(url, kwargs):
    import httpx2

    from turnstone.core.http_failures import REQUEST_FAILURES, may_have_reached_server

    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json={})

    with (
        httpx2.Client(transport=httpx2.MockTransport(handler)) as client,
        pytest.raises(REQUEST_FAILURES) as caught,
    ):
        client.post(url, **kwargs)
    assert sent == []
    assert may_have_reached_server(caught.value) is False


def test_a_reply_that_wont_decode_may_have_reached_it():
    # Decoding comes after the request went out, unlike encoding.
    from turnstone.core.http_failures import may_have_reached_server

    exc = UnicodeDecodeError("utf-8", bytes([0xFF]), 0, 1, "invalid start byte")
    assert may_have_reached_server(exc) is True


@pytest.mark.parametrize(
    "exc_type",
    ["PoolTimeout", "ProxyError", "UnsupportedProtocol"],
)
def test_other_failures_before_sending_never_reached_the_server(exc_type):
    import httpx2

    from turnstone.core.http_failures import may_have_reached_server

    assert may_have_reached_server(getattr(httpx2, exc_type)("failed")) is False
