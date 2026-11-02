"""Workspace browsing: bounded file access and the authenticated node surface."""

from __future__ import annotations

import errno
import os
import stat
from contextlib import suppress
from types import SimpleNamespace

import pytest

from tests.test_server_attachments_endpoints import (
    _TEST_JWT_SECRET,
    PNG_1x1,
    _seed_committed,
)
from tests.test_server_attachments_endpoints import app_client as app_client
from turnstone.core import filesystem as fs
from turnstone.core.auth import JWT_AUD_SERVER, create_jwt, required_scope
from turnstone.core.workstream import WorkstreamKind


def _auth(
    user: str,
    *,
    scopes: tuple[str, ...] = ("read", "write"),
    permissions: tuple[str, ...] = ("files.read",),
    audience: str = JWT_AUD_SERVER,
) -> dict[str, str]:
    token = create_jwt(
        user_id=user,
        scopes=frozenset(scopes),
        permissions=frozenset(permissions),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=audience,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(fs, "get_workspace_dir", lambda: str(root))
    return root


@pytest.fixture
def files_client(app_client, workspace):
    client, mgr = app_client
    ws = SimpleNamespace(kind=WorkstreamKind.INTERACTIVE, _closed=False)
    mgr.loaded.side_effect = lambda ws_id: ws if ws_id in ("ws-A", "ws-B") else None
    return client, mgr, workspace


def test_listing_navigation_metadata_and_source_sniff(workspace):
    (workspace / "folder").mkdir()
    (workspace / "folder" / "note.md").write_text("# Hello")
    (workspace / "page.html").write_text("<h1>Hello</h1>")
    (workspace / "script.js").write_text("console.log('hello');")
    (workspace / "binary.dat").write_bytes(b"\0\xff")
    got = fs.list_directory("")
    assert got["root"] == str(workspace)
    assert got["root_id"] == "workspace"
    assert got["path"] == ""
    assert got["parent"] is None
    assert got["truncated"] is False
    assert got["entries"][0]["name"] == "folder"
    entries = {entry["name"]: entry for entry in got["entries"]}
    assert entries["page.html"]["preview_kind"] == "web"
    assert entries["script.js"]["preview_kind"] == "text"
    assert entries["binary.dat"]["preview_kind"] == "binary"
    assert entries["script.js"]["size"] == 21
    subdir = fs.list_directory("folder")
    assert subdir["parent"] == ""
    assert subdir["entries"][0]["path"] == "folder/note.md"


@pytest.mark.parametrize("root_id", ["workspace", "data", "tmp"])
@pytest.mark.parametrize("content", [b"", b"\x00\xff original bytes\r\n"])
def test_upload_preserves_bytes_and_targets_selected_directory(
    workspace, monkeypatch, root_id, content
):
    root = workspace / root_id
    root.mkdir()
    if root_id == "workspace":
        monkeypatch.setattr(fs, "get_workspace_dir", lambda: str(root))
    else:
        monkeypatch.setitem(fs._FIXED_ROOTS, root_id, str(root))
    (root / "reports").mkdir()
    result = fs.upload_file("reports", "文書 #1.bin", content, root_id=root_id)
    assert result == {
        "root_id": root_id,
        "path": "reports/文書 #1.bin",
        "name": "文書 #1.bin",
        "size": len(content),
    }
    target = root / result["path"]
    assert target.read_bytes() == content
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list((root / "reports").iterdir()) == [target]


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_upload_never_replaces_existing_entries(workspace, kind):
    target = workspace / "existing"
    outside = workspace / "original"
    outside.write_bytes(b"keep me")
    if kind == "file":
        target.write_bytes(b"keep me")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(outside)
    before = sorted(p.name for p in workspace.iterdir())
    with pytest.raises(fs.FilesystemError) as exc:
        fs.upload_file("", "existing", b"replacement")
    assert exc.value.status == 409
    assert exc.value.code == "file_exists"
    assert outside.read_bytes() == b"keep me"
    if kind == "directory":
        assert target.is_dir()
    else:
        assert target.read_bytes() == b"keep me"
        assert target.is_symlink() == (kind == "symlink")
    assert sorted(p.name for p in workspace.iterdir()) == before


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "/absolute",
        "../escape",
        "a/b",
        "a\\b",
        "nul\0",
        "line\n",
        "é" * 128,
        ".file-upload-" + "a" * 32,
    ],
)
def test_upload_rejects_non_basename_names(workspace, name):
    with pytest.raises(fs.FilesystemError) as exc:
        fs.upload_file("", name, b"unwritten")
    assert exc.value.status == 400
    assert list(workspace.iterdir()) == []


def test_upload_rejects_parent_traversal_and_symlink_directories(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    for directory in ("../outside", str(outside), "escape"):
        with pytest.raises(fs.FilesystemError):
            fs.upload_file(directory, "file.txt", b"unwritten")
    assert list(outside.iterdir()) == []


def test_upload_is_hidden_until_atomic_publication_and_handles_partial_writes(
    workspace, monkeypatch
):
    real_write = os.write
    seen = []

    def partial_write(fd, content):
        stages = list(workspace.iterdir())
        assert len(stages) == 1
        stage = stages[0]
        assert fs._UPLOAD_TEMP_RE.fullmatch(stage.name)
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
        assert fs.list_directory("")["entries"] == []
        for path in (stage.name, stage.name + "/content"):
            with pytest.raises(fs.FilesystemError) as exc:
                fs.read_file(path, preview=False)
            assert exc.value.status == 403
        seen.append(bytes(content))
        return real_write(fd, content[:2])

    monkeypatch.setattr(os, "write", partial_write)
    fs.upload_file("", "ready.bin", b"abcdef")
    assert seen == [b"abcdef", b"cdef", b"ef"]
    assert (workspace / "ready.bin").read_bytes() == b"abcdef"
    assert [entry["name"] for entry in fs.list_directory("")["entries"]] == ["ready.bin"]


@pytest.mark.parametrize("operation", ["write", "fsync", "link"])
def test_upload_io_failure_removes_staging_and_does_not_publish(workspace, monkeypatch, operation):
    def failed(*args, **kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, operation, failed)
    with pytest.raises(fs.FilesystemError) as exc:
        fs.upload_file("", "never.bin", b"bytes")
    assert exc.value.status == 503
    assert list(workspace.iterdir()) == []


def test_upload_directory_replacement_cannot_redirect_publication(workspace, tmp_path, monkeypatch):
    target = workspace / "reports"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = workspace / "moved"
    real_fsync = os.fsync

    def replace_directory(fd):
        real_fsync(fd)
        target.rename(moved)
        target.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(os, "fsync", replace_directory)
    fs.upload_file("reports", "file.txt", b"bytes")
    assert (moved / "file.txt").read_bytes() == b"bytes"
    assert list(outside.iterdir()) == []
    assert list(moved.iterdir()) == [moved / "file.txt"]


def test_upload_does_not_remove_a_replaced_staging_directory(workspace, monkeypatch):
    real_link = os.link
    replacement = None

    def replace_stage(*args, **kwargs):
        nonlocal replacement
        real_link(*args, **kwargs)
        replacement = next(p for p in workspace.iterdir() if p.is_dir())
        replacement.rename(workspace / "moved-stage")
        replacement.mkdir()
        (replacement / "keep.txt").write_text("unrelated")

    monkeypatch.setattr(os, "link", replace_stage)
    fs.upload_file("", "file.txt", b"bytes")
    assert (workspace / "file.txt").read_bytes() == b"bytes"
    assert (replacement / "keep.txt").read_text() == "unrelated"
    assert list((workspace / "moved-stage").iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("prefix", ["/v1/api", "/node/n1/v1/api"])
async def test_upload_http_permissions_bytes_and_bounds(files_client, prefix, monkeypatch):
    from unittest.mock import MagicMock

    import httpx2

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import create_app as create_console
    from turnstone.core.auth import JWT_AUD_CONSOLE

    client, _, workspace = files_client
    (workspace / "reports").mkdir()
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
        proxied = prefix.startswith("/node/")
        target = proxy if proxied else node
        audience = JWT_AUD_CONSOLE if proxied else JWT_AUD_SERVER
        url = f"{prefix}/workstreams/ws-A/files/upload"
        params = {"path": "reports", "name": "file.bin"}
        for scopes, permissions in (
            (("read", "write"), ()),
            (("read", "write"), ("files.read",)),
            (("read", "write"), ("files.write",)),
            (("read",), ("files.read", "files.write")),
        ):
            response = await target.post(
                url,
                params=params,
                content=b"no",
                headers=_auth("userA", scopes=scopes, permissions=permissions, audience=audience),
            )
            assert response.status_code == 403, response.text
        auth = _auth("userA", permissions=("files.read", "files.write"), audience=audience)
        body = b"\0\xff exact bytes"
        response = await target.post(url, params=params, content=body, headers=auth)
        assert response.status_code == 201, response.text
        assert response.json() == {
            "root_id": "workspace",
            "path": "reports/file.bin",
            "name": "file.bin",
            "size": len(body),
        }
        assert (workspace / "reports/file.bin").read_bytes() == body
        conflict = await target.post(url, params=params, content=b"replacement", headers=auth)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "file_exists"
        assert (workspace / "reports/file.bin").read_bytes() == body
        params["name"] = "empty.txt"
        assert (await target.post(url, params=params, content=b"", headers=auth)).status_code == 201
        assert (workspace / "reports/empty.txt").read_bytes() == b""
        params["name"] = "streamed.bin"

        async def streamed_chunks():
            yield b"first"
            stages = list((workspace / "reports").glob(".file-upload-*"))
            assert len(stages) == 1
            assert (stages[0] / "content").read_bytes() == b"first"
            assert not (workspace / "reports/streamed.bin").exists()
            yield b"second"

        streamed = await target.post(url, params=params, content=streamed_chunks(), headers=auth)
        assert streamed.status_code == 201, streamed.text
        assert (workspace / "reports/streamed.bin").read_bytes() == b"firstsecond"
        assert not list((workspace / "reports").glob(".file-upload-*"))
        monkeypatch.setattr(fs, "UPLOAD_SIZE_CAP", 4)
        params["name"] = "over.bin"
        assert (
            await target.post(url, params=params, content=b"large", headers=auth)
        ).status_code == 413
        read = []

        async def chunks():
            for chunk in (b"123", b"456", b"unread"):
                read.append(chunk)
                yield chunk

        response = await target.post(url, params=params, content=chunks(), headers=auth)
        assert response.status_code == 413
        assert read == [b"123", b"456"]
        assert not (workspace / "reports/over.bin").exists()


@pytest.mark.parametrize("state", ["nonlocal", "closed", "coordinator", "private-project"])
def test_upload_requires_active_accessible_interactive_workstream(files_client, state):
    client, mgr, workspace = files_client
    ws = SimpleNamespace(kind=WorkstreamKind.INTERACTIVE, _closed=state == "closed")
    if state == "coordinator":
        ws.kind = WorkstreamKind.COORDINATOR
    if state == "private-project":
        import sqlalchemy as sa

        from turnstone.core.storage import get_storage
        from turnstone.core.storage._schema import workstreams

        storage = get_storage()
        storage.create_project("private-upload", "Private", "userA", visibility="private")
        with storage._conn() as conn:
            conn.execute(
                sa.update(workstreams)
                .where(workstreams.c.ws_id == "ws-A")
                .values(project_id="private-upload")
            )
            conn.commit()
    mgr.loaded.side_effect = lambda ws_id: None if state == "nonlocal" else ws
    response = client.post(
        "/v1/api/workstreams/ws-A/files/upload",
        params={"name": "file.txt"},
        content=b"no",
        headers=_auth(
            "userB" if state == "private-project" else "userA",
            permissions=("files.read", "files.write"),
        ),
    )
    assert response.status_code == (403 if state == "private-project" else 409)
    if state != "private-project":
        assert response.json()["code"] == "workstream_inactive"
    assert list(workspace.iterdir()) == []


@pytest.mark.anyio
async def test_interrupted_upload_body_is_rejected_without_publication(files_client):
    from starlette.requests import Request

    from turnstone.core.web_helpers import BoundedUploadStream, UploadBodyError

    messages = iter(
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    with pytest.raises(UploadBodyError) as exc:
        await fs.stream_upload_file("", "partial.txt", BoundedUploadStream(request, max_bytes=64))
    assert exc.value.status == 400
    assert list(files_client[2].iterdir()) == []


def test_utf8_sniff_boundary_is_not_binary(workspace):
    (workspace / "source.py").write_text("a" * (fs._SNIFF_SIZE - 1) + "é")
    assert fs.list_directory("")["entries"][0]["preview_kind"] == "text"


def test_directory_truncation_is_explicit(workspace, monkeypatch):
    monkeypatch.setattr(fs, "DIRECTORY_ENTRY_CAP", 2)
    for name in ("a", "b", "c"):
        (workspace / name).write_text(name)
    result = fs.list_directory("")
    assert len(result["entries"]) == 2
    assert result["truncated"] is True


def test_resolve_tool_path_selects_file_even_beyond_listing_cap(workspace, monkeypatch):
    folder = workspace / "reports"
    folder.mkdir()
    (folder / "report #1.html").write_text("<h1>Report</h1>")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(fs, "DIRECTORY_ENTRY_CAP", 0)
    result = fs.resolve_file("reports/report #1.html")
    assert result["root_id"] == "workspace"
    assert result["path"] == "reports"
    assert result["selected"] == "reports/report #1.html"
    assert result["truncated"] is True
    assert result["entries"][0]["preview_kind"] == "web"


def test_resolve_uses_tool_cwd_not_configured_workspace(workspace, tmp_path, monkeypatch):
    (workspace / "note.txt").write_text("wrong target")
    other = tmp_path / "data"
    other.mkdir()
    (other / "note.txt").write_text("correct target")
    monkeypatch.setitem(fs._FIXED_ROOTS, "data", str(other))
    monkeypatch.chdir(other)
    result = fs.resolve_file("note.txt")
    assert result["root_id"] == "data"
    assert result["root"] == str(other.resolve())
    assert result["selected"] == "note.txt"


@pytest.mark.parametrize("root_id", ["data", "tmp"])
def test_resolve_absolute_paths_and_root_aliases(workspace, tmp_path, monkeypatch, root_id):
    root = tmp_path / "actual"
    root.mkdir()
    (root / "file.txt").write_text("contents")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setitem(fs._FIXED_ROOTS, root_id, str(alias))
    for path in (root / "file.txt", alias / "file.txt"):
        result = fs.resolve_file(str(path))
        assert result["root_id"] == root_id
        assert result["selected"] == "file.txt"


def test_resolve_prefers_nested_workspace_and_expands_home(workspace, tmp_path, monkeypatch):
    monkeypatch.setitem(fs._FIXED_ROOTS, "tmp", str(tmp_path))
    monkeypatch.setenv("HOME", str(workspace))
    (workspace / "file.txt").write_text("hello")
    assert fs.resolve_file("~/file.txt")["root_id"] == "workspace"
    assert fs.resolve_file(str(workspace / "file.txt"))["root_id"] == "workspace"


def test_configured_root_preserves_symlink_parent_resolution(workspace, tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    (actual / "child").mkdir(parents=True)
    (actual / "file.txt").write_text("contents")
    link = workspace / "link"
    link.symlink_to(actual / "child", target_is_directory=True)
    monkeypatch.setattr(fs, "get_workspace_dir", lambda: str(link / ".."))
    result = fs.list_directory("")
    assert result["root"] == str(actual.resolve())
    assert result["entries"][1]["name"] == "file.txt"


def test_resolve_rejects_outside_symlinks_missing_and_special_files(
    workspace, tmp_path, monkeypatch
):
    monkeypatch.setitem(fs._FIXED_ROOTS, "data", str(workspace))
    monkeypatch.setitem(fs._FIXED_ROOTS, "tmp", str(workspace))
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "inside.txt").write_text("inside")
    (workspace / "link.txt").symlink_to(workspace / "inside.txt")
    (workspace / "outside-link.txt").symlink_to(outside)
    os.mkfifo(workspace / "pipe")
    for path, status in (
        (outside, 403),
        (workspace / "link.txt", 403),
        (workspace / "outside-link.txt", 403),
        (workspace / "missing.txt", 404),
        (workspace / "pipe", 400),
        (workspace, 400),
    ):
        with pytest.raises(fs.FilesystemError) as exc:
            fs.resolve_file(str(path))
        assert exc.value.status == status


def test_resolve_parent_segments_cannot_hide_symlink_target(workspace, tmp_path):
    folder = workspace / "folder"
    folder.mkdir()
    (workspace / "file.txt").write_text("inside")
    (workspace / "link").symlink_to(tmp_path, target_is_directory=True)
    assert fs.resolve_file(str(folder / ".." / "file.txt"))["selected"] == "file.txt"
    with pytest.raises(fs.FilesystemError) as exc:
        fs.resolve_file(str(workspace / "link" / ".." / "file.txt"))
    assert exc.value.status == 403


@pytest.mark.parametrize("name", ["report #1.html", "report?copy.html"])
def test_literal_filename_punctuation_preserves_preview_kind(workspace, name):
    original = b"<h1>Report</h1>"
    (workspace / name).write_bytes(original)
    assert fs.list_directory("")["entries"][0]["preview_kind"] == "web"
    data, mime, filename = fs.read_file(name, preview=True)
    assert data == original
    assert mime.startswith("text/html")
    assert filename == name


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "folder/../../outside", "a\0b"])
def test_rejects_escape_paths(workspace, path):
    with pytest.raises(fs.FilesystemError) as exc:
        fs.read_file(path, preview=False)
    assert exc.value.status == 400


def test_symlinks_and_special_files_are_inaccessible(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (workspace / "linked-dir").symlink_to(outside, target_is_directory=True)
    (workspace / "linked-file").symlink_to(outside / "secret")
    os.mkfifo(workspace / "pipe")
    assert fs.list_directory("")["entries"] == []
    for path in ("linked-dir/secret", "linked-file", "pipe"):
        with pytest.raises(fs.FilesystemError):
            fs.read_file(path, preview=False)


def test_file_replacement_by_symlink_before_open_is_refused(workspace, tmp_path, monkeypatch):
    target = workspace / "note.txt"
    target.write_text("safe")
    secret = tmp_path / "secret"
    secret.write_text("secret")
    real_open = os.open

    def replaced(path, flags, *args, **kwargs):
        if path == "note.txt":
            target.unlink()
            target.symlink_to(secret)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced)
    with pytest.raises(fs.FilesystemError):
        fs.read_file("note.txt", preview=False)


@pytest.mark.parametrize("failure", ["none", "open", "body", "close"])
def test_path_walk_closes_all_descriptors_when_operations_fail(workspace, monkeypatch, failure):
    folder = workspace / "folder"
    folder.mkdir()
    (folder / "note.txt").write_text("contents")
    real_open, real_close = os.open, os.close
    opened, closed = [], []

    def tracked_open(path, flags, *args, **kwargs):
        if failure == "open" and path == "note.txt":
            raise OSError(errno.EACCES, "Cannot open file")
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    def tracked_close(fd):
        closed.append(fd)
        real_close(fd)
        if failure == "close" and len(closed) == 1:
            raise OSError(errno.EIO, "Close reported an error")

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    try:

        def walk():
            with fs._open_path(workspace, "folder/note.txt", directory=False) as fd:
                assert os.read(fd, 8) == b"contents"
                if failure == "body":
                    raise RuntimeError("Consumer failed")

        if failure == "none":
            walk()
        else:
            expected = {"open": fs.FilesystemError, "body": RuntimeError, "close": OSError}
            with pytest.raises(expected[failure]):
                walk()
        assert closed == list(reversed(opened))
        for fd in opened:
            with pytest.raises(OSError) as exc:
                os.fstat(fd)
            assert exc.value.errno == errno.EBADF
    finally:
        for fd in opened:
            with suppress(OSError):
                real_close(fd)


def test_missing_configured_workspace_does_not_fall_back(workspace, monkeypatch):
    monkeypatch.setattr(fs, "get_workspace_dir", lambda: str(workspace / "missing"))
    with pytest.raises(fs.FilesystemError) as exc:
        fs.list_directory("")
    assert exc.value.status == 503


def test_unconfigured_workspace_uses_cwd(workspace, monkeypatch):
    monkeypatch.setattr(fs, "get_workspace_dir", lambda: None)
    monkeypatch.chdir(workspace)
    assert fs.list_directory("")["root"] == str(workspace)


@pytest.mark.parametrize("root_id", ["data", "tmp"])
def test_fixed_roots_are_selectable_and_still_confined(
    files_client, tmp_path, monkeypatch, root_id
):
    client, _, workspace = files_client
    selected = tmp_path / root_id
    selected.mkdir()
    monkeypatch.setitem(fs._FIXED_ROOTS, root_id, str(selected))
    (workspace / "note.txt").write_text("workspace content")
    (selected / "note.txt").write_text("selected content")
    url = "/v1/api/workstreams/ws-A/files"
    listing = client.get(url, params={"root": root_id}, headers=_auth("userA"))
    assert listing.status_code == 200
    assert listing.json()["root_id"] == root_id
    assert listing.json()["root"] == str(selected)
    for suffix in ("/preview", "/content"):
        response = client.get(
            url + suffix, params={"root": root_id, "path": "note.txt"}, headers=_auth("userA")
        )
        assert response.content == b"selected content"
        rejected = client.get(
            url + suffix,
            params={"root": root_id, "path": "../workspace/note.txt"},
            headers=_auth("userA"),
        )
        assert rejected.status_code == 400
    (selected / "escape").symlink_to(workspace, target_is_directory=True)
    with pytest.raises(fs.FilesystemError):
        fs.read_file("escape/note.txt", preview=False, root_id=root_id)


@pytest.mark.parametrize("root_id", ["/", "../../", "home"])
def test_arbitrary_roots_are_rejected(files_client, root_id):
    client, _, _ = files_client
    for suffix in ("", "/preview", "/content"):
        response = client.get(
            "/v1/api/workstreams/ws-A/files" + suffix,
            params={"root": root_id},
            headers=_auth("userA"),
        )
        assert response.status_code == 400


def test_missing_fixed_root_is_explicit(files_client, tmp_path, monkeypatch):
    client, _, _ = files_client
    monkeypatch.setitem(fs._FIXED_ROOTS, "data", str(tmp_path / "missing"))
    response = client.get(
        "/v1/api/workstreams/ws-A/files", params={"root": "data"}, headers=_auth("userA")
    )
    assert response.status_code == 503
    assert "unavailable" in response.json()["error"]


def test_preview_and_download_caps_and_original_bytes(workspace, monkeypatch):
    target = workspace / "note.md"
    original = b"caf\xe9"
    target.write_bytes(original)
    assert fs.read_file("note.md", preview=False)[0] == original
    assert fs.read_file("note.md", preview=True)[0] == "café".encode()
    monkeypatch.setitem(fs.PREVIEW_SIZE_CAPS, "markdown", 3)
    with pytest.raises(fs.FilesystemError) as exc:
        fs.read_file("note.md", preview=True)
    assert exc.value.status == 413
    monkeypatch.setattr(fs, "DOWNLOAD_SIZE_CAP", 3)
    with pytest.raises(fs.FilesystemError) as exc:
        fs.read_file("note.md", preview=False)
    assert exc.value.status == 413


def test_changed_binary_content_is_not_previewed(workspace):
    target = workspace / "code.js"
    target.write_text("safe")
    assert fs.list_directory("")["entries"][0]["preview_kind"] == "text"
    target.write_bytes(b"\0secret")
    with pytest.raises(fs.FilesystemError) as exc:
        fs.read_file("code.js", preview=True)
    assert exc.value.status == 415
    assert fs.read_file("code.js", preview=False)[0] == b"\0secret"


@pytest.mark.parametrize("suffix", ["", "/preview", "/content", "/resolve"])
@pytest.mark.parametrize("prefix", ["/v1/api", "/api", "/node/n1/v1/api", "/node/n1/api"])
def test_filesystem_requires_write_scope(prefix, suffix):
    assert required_scope("GET", f"{prefix}/workstreams/ws-A/files{suffix}") == "write"
    assert required_scope("HEAD", f"{prefix}/workstreams/ws-A/files{suffix}") == "write"


@pytest.mark.anyio
@pytest.mark.parametrize("prefix", ["/v1/api", "/node/n1/v1/api"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_file_permission_and_write_scope_reach_every_route(files_client, prefix, method):
    from unittest.mock import MagicMock

    import httpx2

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import create_app as create_console
    from turnstone.core.auth import JWT_AUD_CONSOLE

    client, _, workspace = files_client
    (workspace / "note.txt").write_text("workspace bytes")
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
        proxied = prefix.startswith("/node/")
        target = proxy if proxied else node
        audience = JWT_AUD_CONSOLE if proxied else JWT_AUD_SERVER
        for scopes, permissions, status in (
            (("read", "write"), (), 403),
            (("read",), ("files.read",), 403),
            (("read", "write"), ("files.read",), 200),
            (("read", "write", "service"), (), 200),
        ):
            for suffix in ("", "/preview", "/content", "/resolve"):
                response = await target.request(
                    method,
                    f"{prefix}/workstreams/ws-A/files{suffix}",
                    params={
                        "path": str(workspace / "note.txt")
                        if suffix == "/resolve"
                        else "note.txt"
                        if suffix
                        else ""
                    },
                    headers=_auth(
                        "userA", scopes=scopes, permissions=permissions, audience=audience
                    ),
                )
                assert response.status_code == status, response.text
                if method == "GET" and not permissions and status == 403:
                    assert "files.read" in response.json()["error"]


def test_api_token_observes_file_permission_grants_and_revocations(files_client):
    from turnstone.core.auth import generate_token, hash_token, token_prefix
    from turnstone.core.storage import get_storage

    client, _, workspace = files_client
    (workspace / "note.txt").write_text("workspace bytes")
    storage = get_storage()
    client.app.state.auth_storage = storage
    storage.create_user("userA", "userA", "User A", "hash")
    storage.create_role("file-reader", "file-reader", "File reader", "read,write", builtin=True)
    storage.assign_role("userA", "file-reader")
    token = generate_token()
    storage.create_api_token(
        "files-token", hash_token(token), token_prefix(token), "userA", "files", "read,write"
    )
    headers = {"Authorization": f"Bearer {token}"}
    for granted, expected in ((False, 403), (True, 200), (False, 403)):
        storage.set_role_overrides("file-reader", {"files.read"} if granted else set(), set())
        for suffix in ("", "/preview", "/content", "/resolve"):
            response = client.get(
                "/v1/api/workstreams/ws-A/files" + suffix,
                params={
                    "path": str(workspace / "note.txt")
                    if suffix == "/resolve"
                    else "note.txt"
                    if suffix
                    else ""
                },
                headers=headers,
            )
            assert response.status_code == expected, response.text


def test_http_listing_preview_probe_download_and_auth(files_client):
    client, _, workspace = files_client
    original = b"<html><script>alert('hello')</script><h1>Hello</h1></html>"
    (workspace / "hello.html").write_bytes(original)
    url = "/v1/api/workstreams/ws-A/files"
    assert client.get(url).status_code == 401
    readonly = create_jwt(
        user_id="userA",
        scopes=frozenset({"read"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_SERVER,
    )
    for suffix in ("", "/preview", "/content", "/resolve"):
        assert (
            client.get(url + suffix, headers={"Authorization": f"Bearer {readonly}"}).status_code
            == 403
        )
    assert client.get(url, headers=_auth("userA")).json()["entries"][0]["name"] == "hello.html"
    query = {"path": "hello.html"}
    preview = client.get(url + "/preview", params=query, headers=_auth("userA"))
    assert preview.status_code == 200
    assert preview.content == original
    assert "text/html" in preview.headers["content-type"]
    assert "sandbox; default-src 'none'" in preview.headers["content-security-policy"]
    probe = client.get(url + "/preview", params={**query, "probe": "1"}, headers=_auth("userA"))
    assert probe.status_code == 204
    assert probe.content == b""
    assert probe.headers["content-security-policy"] == preview.headers["content-security-policy"]
    assets = client.get(url + "/preview", params={**query, "assets": "1"}, headers=_auth("userA"))
    assert assets.headers["content-security-policy"] == "sandbox"
    download = client.get(url + "/content", params=query, headers=_auth("userA"))
    assert download.content == original
    assert download.headers["content-disposition"] == 'attachment; filename="hello.html"'
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("state", ["nonlocal", "closed", "coordinator"])
def test_stale_workstream_never_serves_wrong_node_files(files_client, state):
    client, mgr, _ = files_client
    ws = SimpleNamespace(kind=WorkstreamKind.INTERACTIVE, _closed=False)
    if state == "closed":
        ws._closed = True
    if state == "coordinator":
        ws.kind = WorkstreamKind.COORDINATOR
    mgr.loaded.side_effect = lambda ws_id: None if state == "nonlocal" else ws
    # ws-A still exists in shared storage. A stale pane must not use that row
    # as proof that its original node still hosts the workstream.
    for suffix in ("", "/preview", "/content", "/resolve"):
        result = client.get(f"/v1/api/workstreams/ws-A/files{suffix}", headers=_auth("userA"))
        assert result.status_code == 409
        assert "not active on this node" in result.json()["error"]
        assert "code" not in result.json()
    mgr.open_with_outcome.assert_not_called()


def test_private_project_access_is_checked(files_client):
    import sqlalchemy as sa

    from turnstone.core.storage import get_storage
    from turnstone.core.storage._schema import workstreams

    client, _, _ = files_client
    storage = get_storage()
    storage.create_project("private", "Private", "userA", visibility="private")
    with storage._conn() as conn:
        conn.execute(
            sa.update(workstreams).where(workstreams.c.ws_id == "ws-A").values(project_id="private")
        )
        conn.commit()
    assert client.get("/v1/api/workstreams/ws-A/files", headers=_auth("userB")).status_code == 403
    assert client.get("/v1/api/workstreams/ws-A/files", headers=_auth("userA")).status_code == 200


_RESPONSE_POLICY_HEADERS = (
    "content-type",
    "content-security-policy",
    "content-disposition",
    "cache-control",
    "x-content-type-options",
)


@pytest.mark.parametrize(
    ("filename", "mime", "body"),
    [
        ("page.html", "text/html; charset=utf-8", b"<h1>Page</h1>"),
        ("document.pdf", "application/pdf", b"%PDF-1.4 test"),
        ("image.png", "image/png", PNG_1x1),
        ('文書 — "\r\n.txt', "text/plain; charset=utf-8", b"text"),
        ("notes.md", "text/markdown; charset=utf-8", b"# Title"),
        ("data.csv", "text/csv; charset=utf-8", b"a,b\n1,2"),
        ("data.json", "application/json", b'{"a": 1}'),
    ],
    ids=["html", "pdf", "image", "unsafe-filename", "markdown", "csv", "json"],
)
@pytest.mark.parametrize("flags", [{}, {"assets": "1"}, {"probe": "1"}])
def test_attachment_and_filesystem_previews_share_response_policy(
    files_client, filename, mime, body, flags
):
    client, _, workspace = files_client
    (workspace / filename).write_bytes(body)
    aid = _seed_committed("ws-A", "preview", mime, body, filename)
    stored = client.get(
        f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
        params=flags,
        headers=_auth("userA"),
    )
    live = client.get(
        "/v1/api/workstreams/ws-A/files/preview",
        params={"path": filename, **flags},
        headers=_auth("userA"),
    )
    assert stored.status_code == live.status_code == (204 if "probe" in flags else 200)
    assert stored.content == live.content == (b"" if "probe" in flags else body)
    assert {key: stored.headers.get(key) for key in _RESPONSE_POLICY_HEADERS} == {
        key: live.headers.get(key) for key in _RESPONSE_POLICY_HEADERS
    }


@pytest.mark.parametrize(
    ("kind", "mime", "body", "expected_mime"),
    [
        ("text", "text/html", b"<h1>Text upload</h1>", "text/plain; charset=utf-8"),
        ("image", "image/png", PNG_1x1, "image/png"),
        ("audio", "audio/wav", b"\x00\xff", "audio/wav"),
    ],
)
def test_content_sources_preserve_mime_and_disposition_with_shared_hardening(
    files_client, kind, mime, body, expected_mime
):
    client, _, workspace = files_client
    filename = '文書 — "\r\n.bin'
    (workspace / filename).write_bytes(body)
    aid = _seed_committed("ws-A", kind, mime, body, filename)
    stored = client.get(
        f"/v1/api/workstreams/ws-A/attachments/{aid}/content", headers=_auth("userA")
    )
    live = client.get(
        "/v1/api/workstreams/ws-A/files/content",
        params={"path": filename},
        headers=_auth("userA"),
    )
    assert stored.status_code == live.status_code == 200
    assert stored.content == live.content == body
    assert stored.headers["content-type"] == expected_mime
    assert live.headers["content-type"] == "application/octet-stream"
    for key in ("content-security-policy", "cache-control", "x-content-type-options"):
        assert live.headers[key] == stored.headers[key]
    assert stored.headers["content-disposition"].startswith("inline;")
    assert live.headers["content-disposition"] == stored.headers["content-disposition"].replace(
        "inline;", "attachment;", 1
    )
    assert "\r" not in live.headers["content-disposition"]
    assert "\n" not in live.headers["content-disposition"]
    assert live.headers["content-disposition"].isascii()


@pytest.mark.parametrize("probe", ["", "1"])
def test_shared_preview_allowlist_rejects_unsupported_sources_before_probe(
    files_client, monkeypatch, probe
):
    client, _, _ = files_client
    body = b"<svg><script>alert(1)</script></svg>"
    mime = "image/svg+xml"
    aid = _seed_committed("ws-A", "preview", mime, body, "image.svg")
    # The filesystem usually classifies SVG as text. Even if a source hands
    # the renderer an unsupported MIME, the serving boundary must refuse it.
    monkeypatch.setattr(fs, "read_file", lambda *args, **kwargs: (body, mime, "image.svg"))
    for url in (
        f"/v1/api/workstreams/ws-A/attachments/{aid}/preview",
        "/v1/api/workstreams/ws-A/files/preview",
    ):
        response = client.get(url, params={"probe": probe}, headers=_auth("userA"))
        assert response.status_code == 415
        assert "not previewable" in response.json()["error"]


@pytest.mark.parametrize("flag", ["", "0", "1", "yes"])
def test_existing_preview_query_flag_semantics_are_preserved(files_client, flag):
    client, _, workspace = files_client
    body = b"<h1>Page</h1>"
    (workspace / "page.html").write_bytes(body)
    aid = _seed_committed("ws-A", "preview", "text/html", body, "page.html")
    for url, enabled in (
        (f"/v1/api/workstreams/ws-A/attachments/{aid}/preview", bool(flag)),
        ("/v1/api/workstreams/ws-A/files/preview", flag == "1"),
    ):
        response = client.get(
            url, params={"path": "page.html", "assets": flag}, headers=_auth("userA")
        )
        assert response.status_code == 200
        assert (response.headers["content-security-policy"] == "sandbox") is enabled
        probe = client.get(url, params={"path": "page.html", "probe": flag}, headers=_auth("userA"))
        assert probe.status_code == (204 if enabled else 200)
        assert probe.content == (b"" if enabled else body)
