"""The console's request sites catch every way an httpx2 request fails."""

import ast
import pathlib

import pytest

import turnstone.console.coordinator_client as coordinator_client
import turnstone.console.server as console_server


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
