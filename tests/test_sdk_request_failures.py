"""Every way an SDK call fails in transport raises one of ``turnstone.sdk.REQUEST_FAILURES``."""

import httpx2
import pytest

from tests._http_failure_cases import UNREACHABLE_REPLIES
from turnstone.sdk import REQUEST_FAILURES
from turnstone.sdk._base import _BaseClient


async def _drain(stream):
    async for _ in stream:
        pass


@pytest.mark.anyio
@UNREACHABLE_REPLIES
@pytest.mark.parametrize(
    "call",
    [
        lambda client: client._request("GET", "/v1/api/health"),
        lambda client: client._request_bytes("GET", "/v1/api/health"),
        lambda client: _drain(client._stream_sse("/v1/api/events/global")),
    ],
    ids=["request", "request-bytes", "stream-sse"],
)
async def test_a_request_that_fails_raises_one_of_request_failures(reply, call):
    transport = httpx2.MockTransport(reply)
    async with httpx2.AsyncClient(transport=transport, base_url="http://node.example") as http:
        with pytest.raises(REQUEST_FAILURES):
            await call(_BaseClient(httpx_client=http))
