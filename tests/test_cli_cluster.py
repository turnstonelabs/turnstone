"""Tests for the CLI's /cluster command and its console requests."""

from __future__ import annotations

import httpx2
import pytest

from turnstone.cli import _handle_cluster_command

CONSOLE_URL = "http://console.example"


@pytest.fixture(autouse=True)
def _no_service_token(monkeypatch):
    monkeypatch.delenv("TURNSTONE_JWT_SECRET", raising=False)


def _route_get(monkeypatch, handler):
    """Send ``httpx2.get`` through a real client on *handler*'s MockTransport."""

    def _get(url, **kwargs):
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx2, "get", _get)


def test_status_reads_the_cluster_overview(monkeypatch, capsys):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200, json={"nodes": 2, "workstreams": 3, "states": {}, "aggregate": {}}
        )

    _route_get(monkeypatch, handler)
    _handle_cluster_command("/cluster status", CONSOLE_URL)

    assert [str(r.url) for r in seen] == [f"{CONSOLE_URL}/v1/api/cluster/overview"]
    assert "Cluster Overview" in capsys.readouterr().out


def test_a_refused_connection_names_the_console(monkeypatch, capsys):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("Connection refused", request=request)

    _route_get(monkeypatch, handler)
    _handle_cluster_command("/cluster status", CONSOLE_URL)

    out = capsys.readouterr().out
    assert f"Cannot connect to console at {CONSOLE_URL}" in out
    assert "Cluster command failed" not in out
