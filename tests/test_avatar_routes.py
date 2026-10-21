"""Tests for the avatar routes, the avatar in identity payloads, and the admin list.

Runs the console app against a real storage backend (the ``db`` fixture).  The
node server mounts the same handlers from ``turnstone.core.avatar_routes``; one
smoke test drives them there.
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from turnstone.core import avatar_routes, avatars
from turnstone.core.auth import JWT_AUD_CONSOLE, JWT_AUD_SERVER, create_jwt, required_scope
from turnstone.core.avatars import hashed_color

_SECRET = "test-jwt-secret-minimum-32-chars!"
ME = "a" * 32
OTHER = "b" * 32


def _hdr(
    user_id: str,
    scopes: set[str],
    permissions: set[str] | None = None,
    audience: str = JWT_AUD_CONSOLE,
) -> dict[str, str]:
    token = create_jwt(
        user_id,
        frozenset(scopes),
        "password",
        _SECRET,
        audience=audience,
        permissions=frozenset(permissions or set()),
    )
    return {"Authorization": f"Bearer {token}"}


def _png(size: tuple[int, int] = (40, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (0, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def console_app(db) -> Any:
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app

    _load_static()
    db.create_user(ME, "jin", "Jin Park", "x")
    db.create_user(OTHER, "ana", "Ana Ruiz", "x")
    return create_app(
        collector=MagicMock(spec=ClusterCollector), jwt_secret=_SECRET, auth_storage=db
    )


@pytest.fixture
def client(console_app) -> Any:
    # Without the app's startup and shutdown, which these routes don't need:
    # each shutdown waits about a second on a background poll.
    from starlette.testclient import TestClient

    test_client = TestClient(console_app, raise_server_exceptions=False)
    yield test_client
    test_client.close()


@pytest.fixture
def running_client(console_app) -> Any:
    # Every request on one event loop, which overlapping uploads need: they
    # share the module's decode limiter.
    from starlette.testclient import TestClient

    with TestClient(console_app, raise_server_exceptions=False) as test_client:
        yield test_client


VIEWER = _hdr(ME, {"read"})
OPERATOR = _hdr(ME, {"read", "write"})
OTHER_OPERATOR = _hdr(OTHER, {"read", "write"})


class TestScopes:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("PUT", "/v1/api/users/abc/avatar"),
            ("PUT", "/v1/api/users/abc/avatar/picture"),
            ("DELETE", "/v1/api/users/abc/avatar/picture"),
            ("PUT", "/node/n1/v1/api/users/abc/avatar"),
            ("DELETE", "/node/n1/api/users/abc/avatar/picture"),
        ],
    )
    def test_avatar_changes_need_write(self, method: str, path: str) -> None:
        # Read means no writes, the caller's own avatar included.
        assert required_scope(method, path) == "write"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/v1/api/users/abc/avatar"),
            ("PUT", "/v1/api/users/abc/avatars"),
            ("PUT", "/v1/api/users/abc/avatar/picture/extra"),
        ],
    )
    def test_fetching_and_other_paths_stay_at_read(self, method: str, path: str) -> None:
        assert required_scope(method, path) == "read"

    def test_admin_removal_needs_approve(self) -> None:
        assert required_scope("DELETE", "/v1/api/admin/users/abc/avatar/picture") == "approve"

    def test_viewer_cannot_change_their_avatar(self, client, db) -> None:
        put = client.put(f"/v1/api/users/{ME}/avatar", json={"color": 1}, headers=VIEWER)
        assert put.status_code == 403
        upload = client.put(
            f"/v1/api/users/{ME}/avatar/picture",
            files={"file": ("me.png", _png(), "image/png")},
            headers=VIEWER,
        )
        assert upload.status_code == 403
        assert (
            client.delete(f"/v1/api/users/{ME}/avatar/picture", headers=VIEWER).status_code == 403
        )
        assert db.get_user_avatar(ME) is None


class TestApiTokens:
    """API tokens follow the same rule as sign-in sessions: write scope to change."""

    @staticmethod
    def _token(db, scopes: str) -> dict[str, str]:
        from turnstone.core.auth import generate_token, hash_token, token_prefix

        raw = generate_token()
        db.create_api_token(f"tok-{scopes}", hash_token(raw), token_prefix(raw), ME, "s", scopes)
        return {"Authorization": f"Bearer {raw}"}

    def test_read_only_token_cannot_change_the_avatar(self, client, db) -> None:
        hdr = self._token(db, "read")
        assert (
            client.put(f"/v1/api/users/{ME}/avatar", json={"color": 1}, headers=hdr).status_code
            == 403
        )
        upload = client.put(
            f"/v1/api/users/{ME}/avatar/picture",
            files={"file": ("me.png", _png(), "image/png")},
            headers=hdr,
        )
        assert upload.status_code == 403
        assert client.delete(f"/v1/api/users/{ME}/avatar/picture", headers=hdr).status_code == 403
        assert db.get_user_avatar(ME) is None

    def test_read_only_token_can_still_fetch_pictures(self, client, db) -> None:
        db.set_user_avatar_image(ME, _png(), "f" * 64)
        hdr = self._token(db, "read")
        assert client.get(f"/v1/api/users/{ME}/avatar", headers=hdr).status_code == 200

    def test_write_token_can_change_the_avatar(self, client, db) -> None:
        hdr = self._token(db, "read,write")
        resp = client.put(f"/v1/api/users/{ME}/avatar", json={"color": 1}, headers=hdr)
        assert resp.status_code == 200
        assert db.get_user_avatar(ME) == {"color": 1, "image_sha256": None}


class TestColour:
    def test_sets_own_colour(self, client, db) -> None:
        resp = client.put(f"/v1/api/users/{ME}/avatar", json={"color": 4}, headers=OPERATOR)
        assert resp.status_code == 200
        assert resp.json() == {"avatar": {"color": 4, "initial": "J", "picture": None}}
        assert db.get_user_avatar(ME) == {"color": 4, "image_sha256": None}

    def test_someone_elses_colour_is_refused(self, client, db) -> None:
        resp = client.put(f"/v1/api/users/{OTHER}/avatar", json={"color": 1}, headers=OPERATOR)
        assert resp.status_code == 403
        assert db.get_user_avatar(OTHER) is None

    @pytest.mark.parametrize("bad", [6, -1, "2", True, 2.0, None])
    def test_bad_colour(self, client, bad: Any) -> None:
        resp = client.put(f"/v1/api/users/{ME}/avatar", json={"color": bad}, headers=OPERATOR)
        assert resp.status_code == 400

    def test_principal_without_a_user_row(self, client) -> None:
        hdr = _hdr("channel-gateway", {"read", "write"})
        resp = client.put("/v1/api/users/channel-gateway/avatar", json={"color": 1}, headers=hdr)
        assert resp.status_code == 404

    def test_account_deleted_while_saving(self, client, db, monkeypatch) -> None:
        real = db.set_user_avatar_color

        def delete_first(user_id: str, color: int) -> bool:
            db.delete_user(user_id)
            return real(user_id, color)

        monkeypatch.setattr(db, "set_user_avatar_color", delete_first)
        resp = client.put(f"/v1/api/users/{ME}/avatar", json={"color": 4}, headers=OPERATOR)
        assert resp.status_code == 404
        assert db.get_user_avatar(ME) is None, "no avatar row outlives the account"


class TestPicture:
    def _upload(
        self, client, data: bytes, headers: dict[str, str] = OPERATOR, user_id: str = ME
    ) -> Any:
        return client.put(
            f"/v1/api/users/{user_id}/avatar/picture",
            files={"file": ("me.png", data, "image/png")},
            headers=headers,
        )

    def test_no_picture_is_404(self, client) -> None:
        assert client.get(f"/v1/api/users/{ME}/avatar", headers=VIEWER).status_code == 404

    def test_upload_then_fetch(self, client) -> None:
        resp = self._upload(client, _png())
        assert resp.status_code == 200
        avatar = resp.json()["avatar"]
        version = avatar["picture"]
        assert avatar["initial"] == "J" and len(version) == 16

        # Anyone signed in can fetch it.
        other = _hdr(OTHER, {"read"})
        got = client.get(f"/v1/api/users/{ME}/avatar", headers=other)
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/png"
        assert got.headers["cache-control"] == "private, no-cache"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert Image.open(BytesIO(got.content)).size == (256, 256)

        versioned = client.get(f"/v1/api/users/{ME}/avatar?v={version}", headers=other)
        assert "immutable" in versioned.headers["cache-control"]
        # An older version's link revalidates, so it never pins the current picture.
        stale = client.get(f"/v1/api/users/{ME}/avatar?v={'0' * 16}", headers=other)
        assert stale.headers["cache-control"] == "private, no-cache"

        etag = got.headers["etag"]
        again = client.get(f"/v1/api/users/{ME}/avatar", headers={**other, "If-None-Match": etag})
        assert again.status_code == 304

    def test_fetch_needs_a_signed_in_caller(self, client) -> None:
        assert self._upload(client, _png()).status_code == 200
        assert client.get(f"/v1/api/users/{ME}/avatar").status_code == 401

    def test_gif_is_refused(self, client) -> None:
        buf = BytesIO()
        Image.new("RGB", (8, 8)).save(buf, format="GIF")
        resp = self._upload(client, buf.getvalue())
        assert resp.status_code == 400
        assert "PNG, JPEG or WebP" in resp.json()["error"]

    def test_too_large(self, client) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"\0" * (avatars.MAX_PICTURE_BYTES + 1)
        assert self._upload(client, data).status_code == 413

    def test_overlapping_uploads_wait_their_turn(self, running_client, monkeypatch) -> None:
        decoded: list[int] = []
        first_started = threading.Event()
        release = threading.Event()
        real = avatar_routes.normalize_picture

        def held(data: bytes) -> tuple[bytes, str]:
            decoded.append(len(data))
            if len(decoded) == 1:
                first_started.set()
                assert release.wait(10)
            return real(data)

        monkeypatch.setattr(avatar_routes, "normalize_picture", held)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self._upload, running_client, _png())
            assert first_started.wait(10)
            second = pool.submit(
                self._upload, running_client, _png((30, 30)), OTHER_OPERATOR, OTHER
            )
            deadline = time.monotonic() + 10
            while avatar_routes._DECODE_LIMITER.statistics().tasks_waiting == 0:
                assert time.monotonic() < deadline, "the second upload never queued"
                time.sleep(0.01)
            assert len(decoded) == 1, "the second decode waits for the first"
            release.set()
            assert first.result(10).status_code == 200
            assert second.result(10).status_code == 200
        assert len(decoded) == 2

    def test_one_upload_at_a_time_per_person(self, running_client, monkeypatch) -> None:
        # So nobody can fill the queue alone.
        started = threading.Event()
        release = threading.Event()
        real = avatar_routes.normalize_picture

        def held(data: bytes) -> tuple[bytes, str]:
            started.set()
            assert release.wait(10)
            return real(data)

        monkeypatch.setattr(avatar_routes, "normalize_picture", held)
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(self._upload, running_client, _png())
            assert started.wait(10)
            again = self._upload(running_client, _png((30, 30)))
            assert again.status_code == 503
            assert again.headers["retry-after"] == "2"
            assert "Your last picture" in again.json()["error"]
            release.set()
            assert first.result(10).status_code == 200
        assert self._upload(running_client, _png((30, 30))).status_code == 200

    def test_a_full_queue_asks_to_retry(self, client, monkeypatch) -> None:
        busy = {f"someone-{n}" for n in range(avatar_routes._MAX_UPLOADS_IN_FLIGHT)}
        monkeypatch.setattr(avatar_routes, "_admitted", busy)
        resp = self._upload(client, _png())
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "2"
        assert "Too many pictures" in resp.json()["error"]

    def test_account_deleted_while_the_picture_decodes(self, client, db, monkeypatch) -> None:
        real = avatar_routes.normalize_picture

        def then_delete(data: bytes) -> tuple[bytes, str]:
            result = real(data)
            db.delete_user(ME)
            return result

        monkeypatch.setattr(avatar_routes, "normalize_picture", then_delete)
        resp = self._upload(client, _png())
        assert resp.status_code == 404
        assert db.get_user_avatar(ME) is None, "no avatar row outlives the account"

    def test_upload_for_someone_else_is_refused(self, client) -> None:
        resp = client.put(
            f"/v1/api/users/{OTHER}/avatar/picture",
            files={"file": ("x.png", _png(), "image/png")},
            headers=OPERATOR,
        )
        assert resp.status_code == 403

    def test_remove_own_picture(self, client, db) -> None:
        db.set_user_avatar_color(ME, 2)
        assert self._upload(client, _png()).status_code == 200
        resp = client.delete(f"/v1/api/users/{ME}/avatar/picture", headers=OPERATOR)
        assert resp.status_code == 200
        assert resp.json() == {"avatar": {"color": 2, "initial": "J", "picture": None}}
        assert client.get(f"/v1/api/users/{ME}/avatar", headers=VIEWER).status_code == 404

    def test_removing_someone_elses_picture_is_refused(self, client, db) -> None:
        db.set_user_avatar_image(OTHER, _png(), "f" * 64)
        resp = client.delete(f"/v1/api/users/{OTHER}/avatar/picture", headers=OPERATOR)
        assert resp.status_code == 403
        assert db.get_user_avatar_image(OTHER) is not None

    def test_own_picture_changes_are_audited(self, client, db) -> None:
        def events(action: str) -> list[dict[str, Any]]:
            found = db.list_audit_events(action=action)
            for event in found:
                if isinstance(event["detail"], str):
                    event["detail"] = json.loads(event["detail"])
            return found

        version = self._upload(client, _png()).json()["avatar"]["picture"]
        [stored] = events("user.avatar.set_picture")
        assert (stored["user_id"], stored["resource_id"]) == (ME, ME)
        assert stored["detail"] == {"username": "jin", "picture": version}
        client.delete(f"/v1/api/users/{ME}/avatar/picture", headers=OPERATOR)
        client.delete(f"/v1/api/users/{ME}/avatar/picture", headers=OPERATOR)
        [removed] = events("user.avatar.remove_picture")  # the second removed nothing
        assert removed["detail"] == {"username": "jin", "picture": version}


@pytest.mark.anyio
async def test_uploads_arriving_together_are_capped(db, monkeypatch) -> None:
    # They reach the check in one pass of the event loop.  Five get a place,
    # one decoding and four waiting, and the rest are refused at once.
    import asyncio

    import httpx2

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app

    people = [f"{n:032x}" for n in range(10)]
    for n, user_id in enumerate(people):
        db.create_user(user_id, f"user{n}", f"User {n}", "x")
    release = threading.Event()
    real = avatar_routes.normalize_picture

    def held(data: bytes) -> tuple[bytes, str]:
        assert release.wait(10)
        return real(data)

    monkeypatch.setattr(avatar_routes, "normalize_picture", held)
    _load_static()
    app = create_app(
        collector=MagicMock(spec=ClusterCollector), jwt_secret=_SECRET, auth_storage=db
    )
    png = _png()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://console.example.com"
    ) as http:
        uploads = [
            asyncio.ensure_future(
                http.put(
                    f"/v1/api/users/{user_id}/avatar/picture",
                    files={"file": ("me.png", png, "image/png")},
                    headers=_hdr(user_id, {"read", "write"}),
                )
            )
            for user_id in people
        ]
        try:
            deadline = time.monotonic() + 10
            while sum(upload.done() for upload in uploads) < 5:
                assert time.monotonic() < deadline, "no upload past the cap was refused"
                await asyncio.sleep(0.01)
            early = sorted(upload.result().status_code for upload in uploads if upload.done())
        finally:
            release.set()
            answered = await asyncio.gather(*uploads, return_exceptions=True)
    assert early == [503] * 5
    assert sorted(response.status_code for response in answered) == [200] * 5 + [503] * 5
    assert avatar_routes._uploading == set()
    assert avatar_routes._admitted == set()


@pytest.mark.anyio
async def test_a_second_upload_is_refused_while_the_first_still_arrives(console_app) -> None:
    # The per-person place is taken before the body is read, so one person
    # can't have several bodies arriving at once.
    import asyncio

    import httpx2

    boundary = "avatar-boundary"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="me.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()
    arriving = asyncio.Event()
    finish = asyncio.Event()

    async def slow_body() -> Any:
        yield head
        arriving.set()
        await finish.wait()
        yield _png() + f"\r\n--{boundary}--\r\n".encode()

    url = f"/v1/api/users/{ME}/avatar/picture"
    slow_headers = {**OPERATOR, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    transport = httpx2.ASGITransport(app=console_app)
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://console.example.com"
    ) as http:
        first = asyncio.ensure_future(http.put(url, content=slow_body(), headers=slow_headers))
        try:
            await asyncio.wait_for(arriving.wait(), 10)
            again = await http.put(
                url, files={"file": ("me.png", _png(), "image/png")}, headers=OPERATOR
            )
        finally:
            finish.set()
        assert (await first).status_code == 200
    assert again.status_code == 503
    assert "Your last picture" in again.json()["error"]
    assert avatar_routes._uploading == set()


class TestAdmin:
    ADMIN = _hdr(OTHER, {"read", "write", "approve"}, {"admin.users"})

    def test_admin_removes_someone_elses_picture(self, client, db) -> None:
        db.set_user_avatar_image(ME, _png(), "f" * 64)
        resp = client.delete(f"/v1/api/admin/users/{ME}/avatar/picture", headers=self.ADMIN)
        assert resp.status_code == 200
        assert resp.json()["avatar"]["picture"] is None
        assert db.get_user_avatar_image(ME) is None
        events = db.list_audit_events(action="user.avatar.remove_picture")
        assert [e["resource_id"] for e in events] == [ME]
        detail = events[0]["detail"]
        detail = json.loads(detail) if isinstance(detail, str) else detail
        assert detail == {"username": "jin", "picture": "f" * 16}

    def test_removing_no_picture_writes_no_audit_row(self, client, db) -> None:
        resp = client.delete(f"/v1/api/admin/users/{ME}/avatar/picture", headers=self.ADMIN)
        assert resp.status_code == 200
        assert db.list_audit_events(action="user.avatar.remove_picture") == []

    def test_without_admin_users_permission(self, client, db) -> None:
        db.set_user_avatar_image(ME, _png(), "f" * 64)
        hdr = _hdr(OTHER, {"read", "write", "approve"})
        resp = client.delete(f"/v1/api/admin/users/{ME}/avatar/picture", headers=hdr)
        assert resp.status_code == 403
        assert db.get_user_avatar_image(ME) is not None

    def test_unknown_user(self, client) -> None:
        resp = client.delete(f"/v1/api/admin/users/{'c' * 32}/avatar/picture", headers=self.ADMIN)
        assert resp.status_code == 404

    def test_list_users_carries_avatars(self, client, db) -> None:
        db.set_user_avatar_color(ME, 5)
        resp = client.get("/v1/api/admin/users", headers=self.ADMIN)
        assert resp.status_code == 200
        by_id = {u["user_id"]: u["avatar"] for u in resp.json()["users"]}
        assert by_id[ME] == {"color": 5, "initial": "J", "picture": None}
        assert by_id[OTHER] == {"color": hashed_color(OTHER), "initial": "A", "picture": None}

    def test_create_user_picks_a_colour_away_from_the_same_initial(self, client, db) -> None:
        db.set_user_avatar_color(ME, 3)  # Jin Park
        resp = client.post(
            "/v1/api/admin/users",
            json={"username": "jo", "display_name": "Jo Chen", "password": "long-enough"},
            headers=self.ADMIN,
        )
        assert resp.status_code == 200
        body = resp.json()
        stored = db.get_user_avatar(body["user_id"])
        assert stored is not None and stored["color"] not in (None, 3)
        assert body["avatar"] == {"color": stored["color"], "initial": "J", "picture": None}


class TestIdentityPayloads:
    def test_whoami_carries_name_and_avatar(self, client, db) -> None:
        db.set_user_avatar_color(ME, 1)
        resp = client.get("/v1/api/auth/whoami", headers=VIEWER)
        assert resp.status_code == 200
        body = resp.json()
        # whoami's username has always been the display name.
        assert (body["username"], body["display_name"]) == ("Jin Park", "Jin Park")
        assert body["avatar"] == {"color": 1, "initial": "J", "picture": None}

    def test_whoami_for_a_principal_without_a_user_row(self, client) -> None:
        resp = client.get("/v1/api/auth/whoami", headers=_hdr("channel-gateway", {"read"}))
        assert resp.status_code == 200
        assert not {"username", "display_name", "avatar"} & set(resp.json())


def test_setup_then_login_carry_the_identity(
    sqlite_migrated_template: Any, sqlite_backend_factory: Any, tmp_path: Any
) -> None:
    """Setup and login return the login name, the display name and the avatar."""
    from starlette.testclient import TestClient

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app

    _load_static()
    path = tmp_path / "setup.db"
    shutil.copyfile(sqlite_migrated_template, path)
    storage = sqlite_backend_factory(str(path), create_tables=False)
    app = create_app(
        collector=MagicMock(spec=ClusterCollector), jwt_secret=_SECRET, auth_storage=storage
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        setup = test_client.post(
            "/v1/api/auth/setup",
            json={"username": "ada", "display_name": "Ada Admin", "password": "long-enough"},
        )
        login = test_client.post(
            "/v1/api/auth/login", json={"username": "ada", "password": "long-enough"}
        )
    assert setup.status_code == 200, setup.text
    assert login.status_code == 200, login.text
    user_id = setup.json()["user_id"]
    stored = storage.get_user_avatar(user_id)
    assert stored is not None and stored["color"] is not None, "setup picks a colour"
    avatar = {"color": stored["color"], "initial": "A", "picture": None}
    for body in (setup.json(), login.json()):
        assert (body["username"], body["display_name"]) == ("ada", "Ada Admin")
        assert body["avatar"] == avatar


def test_node_server_mounts_the_avatar_routes(db) -> None:
    from starlette.testclient import TestClient

    from turnstone.server import create_app

    db.create_user(ME, "jin", "Jin Park", "x")
    manager = MagicMock()
    manager.list_all.return_value = []
    manager.max_active = 10
    app = create_app(
        workstreams=manager,
        global_queue=queue.Queue(),
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret=_SECRET,
        auth_storage=db,
    )
    hdr = _hdr(ME, {"read", "write"}, audience=JWT_AUD_SERVER)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        colour = test_client.put(f"/v1/api/users/{ME}/avatar", json={"color": 2}, headers=hdr)
        upload = test_client.put(
            f"/v1/api/users/{ME}/avatar/picture",
            files={"file": ("me.png", _png(), "image/png")},
            headers=hdr,
        )
        fetch = test_client.get(f"/v1/api/users/{ME}/avatar", headers=hdr)
        viewer = test_client.delete(
            f"/v1/api/users/{ME}/avatar/picture",
            headers=_hdr(ME, {"read"}, audience=JWT_AUD_SERVER),
        )
    assert colour.status_code == 200 and colour.json()["avatar"]["color"] == 2
    assert upload.status_code == 200 and fetch.status_code == 200
    assert viewer.status_code == 403


def test_refresh_keeps_the_name_and_avatar() -> None:
    """The refresh body is stored like whoami's, so it must carry the identity fields."""
    from starlette.testclient import TestClient

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app

    _load_static()
    storage = MagicMock()
    storage.get_user.side_effect = lambda uid: (
        {"user_id": ME, "username": "jin", "display_name": "Jin Park"} if uid == ME else None
    )
    storage.get_user_avatar.return_value = {"color": 2, "image_sha256": None}
    storage.get_user_permissions.return_value = {"read"}
    storage.list_user_roles.return_value = [{"role_id": "builtin-viewer", "scopes": "read"}]
    app = create_app(
        collector=MagicMock(spec=ClusterCollector), jwt_secret=_SECRET, auth_storage=storage
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        resp = test_client.post("/v1/api/auth/refresh", headers=VIEWER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["username"], body["display_name"]) == ("jin", "Jin Park")
    assert body["avatar"] == {"color": 2, "initial": "J", "picture": None}
