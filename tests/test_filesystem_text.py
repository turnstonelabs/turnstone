"""Bounded text prefixes preserve file access and original-byte semantics."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import httpx2
import pytest

from tests.test_filesystem import _auth
from tests.test_filesystem import app_client as app_client
from tests.test_filesystem import files_client as files_client
from tests.test_filesystem import workspace as workspace
from tests.test_server_attachments_endpoints import _TEST_JWT_SECRET
from turnstone.core import filesystem as fs

_PREVIEW = "/v1/api/workstreams/ws-A/files/preview"


def test_large_text_prefix_expands_without_changing_complete_preview_or_download(files_client):
    client, _, root = files_client
    original = b"line one\nline two\n" * 40000
    (root / "large.log").write_bytes(original)
    params = {"path": "large.log", "limit": 256 * 1024}
    first = client.get(_PREVIEW, params=params, headers=_auth("userA"))
    assert first.status_code == 200
    assert first.content == original[: 256 * 1024]
    assert first.headers["x-preview-truncated"] == "true"
    assert int(first.headers["x-preview-bytes"]) == len(first.content)
    assert int(first.headers["x-file-size"]) == len(original)
    second = client.get(_PREVIEW, params={**params, "limit": 512 * 1024}, headers=_auth("userA"))
    assert second.status_code == 200
    assert second.content == original[: 512 * 1024]
    assert second.content.startswith(first.content)
    complete = client.get(_PREVIEW, params={"path": "large.log"}, headers=_auth("userA"))
    assert complete.status_code == 413
    download = client.get(
        _PREVIEW.replace("/preview", "/content"),
        params={"path": "large.log"},
        headers=_auth("userA"),
    )
    assert download.status_code == 200
    assert download.content == original


@pytest.mark.parametrize("original", [b"", b"# A complete note\n"])
def test_complete_prefix_and_probe_report_original_size(files_client, original):
    client, _, root = files_client
    (root / "note.md").write_bytes(original)
    params = {"path": "note.md", "limit": 256 * 1024}
    response = client.get(_PREVIEW, params=params, headers=_auth("userA"))
    assert response.status_code == 200
    assert response.content == original
    assert response.headers["x-preview-truncated"] == "false"
    assert response.headers["x-preview-bytes"] == str(len(original))
    assert response.headers["x-file-size"] == str(len(original))
    probe = client.get(_PREVIEW, params={**params, "probe": 1}, headers=_auth("userA"))
    assert probe.status_code == 204
    assert probe.content == b""
    for name in ("x-preview-truncated", "x-preview-bytes", "x-file-size"):
        assert probe.headers[name] == response.headers[name]


@pytest.mark.parametrize("limit", ["0", "-1", "2097153", "abc", "1.5", "", "true"])
def test_invalid_prefix_limits_are_rejected(files_client, limit):
    client, _, root = files_client
    (root / "note.txt").write_text("text")
    response = client.get(
        _PREVIEW, params={"path": "note.txt", "limit": limit}, headers=_auth("userA")
    )
    assert response.status_code == 400


@pytest.mark.parametrize("limit, expected", [(4, "abc"), (5, "abc"), (6, "abc"), (7, "abc😀")])
def test_prefix_preserves_utf8_character_boundaries(workspace, limit, expected):
    original = "abc😀tail".encode()
    (workspace / "emoji.txt").write_bytes(original)
    body, mime, filename, headers = fs.read_text_preview("emoji.txt", limit=limit)
    assert body.decode("utf-8") == expected
    assert mime.startswith("text/plain")
    assert filename == "emoji.txt"
    assert headers["X-Preview-Bytes"] == str(len(expected.encode()))
    assert headers["X-File-Size"] == str(len(original))
    assert headers["X-Preview-Truncated"] == "true"


def test_legacy_markdown_prefix_does_not_drop_a_complete_character(workspace):
    original = b"caf\xe9 next"
    (workspace / "legacy.md").write_bytes(original)
    body, _, _, headers = fs.read_text_preview("legacy.md", limit=4)
    assert body == "café".encode()
    assert headers["X-Preview-Bytes"] == "4"
    assert headers["X-File-Size"] == str(len(original))
    assert headers["X-Preview-Truncated"] == "true"


def test_binary_content_beyond_the_sniff_is_not_returned_as_text(workspace):
    (workspace / "binary.md").write_bytes(b"a" * 9000 + b"\0hidden bytes")
    with pytest.raises(fs.FilesystemError) as error:
        fs.read_text_preview("binary.md", limit=16384)
    assert error.value.status == 415


@pytest.mark.parametrize(
    "name, content",
    [("page.html", b"<h1>Page</h1>"), ("table.csv", b"name,value\na,1\n"), ("blob.bin", b"\0\xff")],
)
def test_prefix_option_is_only_for_text_and_markdown(files_client, name, content):
    client, _, root = files_client
    (root / name).write_bytes(content)
    response = client.get(_PREVIEW, params={"path": name, "limit": 256}, headers=_auth("userA"))
    assert response.status_code == 415


def test_prefix_keeps_path_and_authorization_gates(files_client):
    client, mgr, root = files_client
    (root / "note.txt").write_text("private bytes")
    (root / "link.txt").symlink_to(root / "note.txt")
    for path, status in (("../note.txt", 400), ("link.txt", 403)):
        response = client.get(_PREVIEW, params={"path": path, "limit": 256}, headers=_auth("userA"))
        assert response.status_code == status
    for headers in (
        _auth("userA", permissions=()),
        _auth("userA", scopes=("read",)),
    ):
        response = client.get(_PREVIEW, params={"path": "note.txt", "limit": 256}, headers=headers)
        assert response.status_code == 403
    mgr.loaded.side_effect = lambda _: None
    inactive = client.get(
        _PREVIEW, params={"path": "note.txt", "limit": 256}, headers=_auth("userA")
    )
    assert inactive.status_code == 409


def test_prefix_reads_only_a_bounded_part_of_a_large_file(workspace, monkeypatch):
    limit = 2 * 1024 * 1024
    total_size = 70 * 1024 * 1024
    target = workspace / "large.log"
    with target.open("wb") as stream:
        stream.write(b"a" * (limit + 16))
        stream.truncate(total_size)
    read_sizes = []
    real_read = os.read

    def counted_read(fd, size):
        data = real_read(fd, size)
        read_sizes.append(len(data))
        return data

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", counted_read)
        body, _, _, headers = fs.read_text_preview("large.log", limit=limit)
    assert body == b"a" * limit
    assert sum(read_sizes) <= limit + 16384
    assert headers["X-File-Size"] == str(total_size)
    assert headers["X-Preview-Truncated"] == "true"


def test_prefix_reload_reads_current_file_instead_of_combining_versions(workspace):
    target = workspace / "changing.txt"
    target.write_text("old file content")
    previous = fs.read_text_preview("changing.txt", limit=4)[0]
    target.write_text("newer content replaces the old file")
    current = fs.read_text_preview("changing.txt", limit=8)[0]
    assert previous == b"old "
    assert current == b"newer co"


@pytest.mark.anyio
async def test_console_proxy_preserves_prefix_metadata(files_client):
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import create_app as create_console
    from turnstone.core.auth import JWT_AUD_CONSOLE

    client, _, root = files_client
    (root / "note.txt").write_text("some original text")
    collector = MagicMock(spec=ClusterCollector)
    collector.get_node_detail.return_value = {"server_url": "http://node.test"}
    console = create_console(collector=collector, jwt_secret=_TEST_JWT_SECRET)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(client.app), base_url="http://node.test"
        ) as node,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(console), base_url="http://console.test"
        ) as proxy,
    ):
        console.state.proxy_client = node
        for probe in (0, 1):
            response = await proxy.get(
                "/node/n1" + _PREVIEW,
                params={"path": "note.txt", "limit": 4, "probe": probe},
                headers=_auth("userA", audience=JWT_AUD_CONSOLE),
            )
            assert response.status_code == (204 if probe else 200)
            assert response.content == (b"" if probe else b"some")
            assert response.headers["x-preview-truncated"] == "true"
            assert response.headers["x-preview-bytes"] == "4"
            assert response.headers["x-file-size"] == "18"
            assert response.headers["cache-control"] == "private, no-store"
