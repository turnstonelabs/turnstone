"""Tests for the channel gateway HTTP notify endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from turnstone.channels._http import create_channel_app
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def mock_adapter():
    adapter = AsyncMock()
    adapter.channel_type = "discord"
    adapter.send = AsyncMock(return_value="msg_001")
    return adapter


@pytest.fixture
def client(storage, mock_adapter):
    app = create_channel_app({"discord": mock_adapter}, storage)
    return TestClient(app)


@pytest.fixture
def authed_client(storage, mock_adapter):
    """Client with static auth token configured."""
    app = create_channel_app({"discord": mock_adapter}, storage, auth_token="test-secret-token")
    return TestClient(app)


@pytest.fixture
def jwt_client(storage, mock_adapter):
    """Client with JWT auth configured."""
    app = create_channel_app({"discord": mock_adapter}, storage, jwt_secret="a" * 32)
    return TestClient(app)


class TestNotifyEndpoint:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_direct_discord_target(self, client, mock_adapter):
        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123456"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["status"] == "sent"
        assert results[0]["message_id"] == "msg_001"
        mock_adapter.send.assert_called_once_with("123456", "Hello!")

    def test_with_title(self, client, mock_adapter):
        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123456"},
                "message": "Hello!",
                "title": "Alert",
            },
        )
        assert resp.status_code == 200
        mock_adapter.send.assert_called_once_with("123456", "**Alert**\nHello!")

    def test_username_resolution(self, client, storage, mock_adapter):
        # Create a user and link a channel
        storage.create_user("u1", "testuser", "Test User", "hash")
        storage.create_channel_user("discord", "disc_123", "u1")

        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"username": "testuser"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["status"] == "sent"
        mock_adapter.send.assert_called_once_with("disc_123", "Hello!")

    def test_unknown_username(self, client):
        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"username": "nobody"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]

    def test_user_no_channels(self, client, storage):
        storage.create_user("u1", "testuser", "Test User", "hash")

        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"username": "testuser"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 404
        assert "no linked channels" in resp.json()["error"]

    def test_missing_fields(self, client):
        resp = client.post("/v1/api/notify", json={"target": {"username": "x"}})
        assert resp.status_code == 400

    def test_missing_target(self, client):
        resp = client.post("/v1/api/notify", json={"message": "Hello!"})
        assert resp.status_code == 400

    def test_invalid_target(self, client):
        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"invalid": "field"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 400

    def test_no_adapter(self, client, storage):
        # App has discord adapter, try email target
        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "email", "channel_id": "test@example.com"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["status"] == "no_adapter"

    def test_adapter_failure(self, client, mock_adapter):
        mock_adapter.send.side_effect = RuntimeError("Discord API error")
        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123456"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["status"] == "failed"

    def test_invalid_json(self, client):
        resp = client.post(
            "/v1/api/notify",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


class TestNotifyAuth:
    """Tests for authentication on the /v1/api/notify endpoint."""

    def test_no_auth_when_unconfigured(self, client, mock_adapter):
        """Requests pass through when no auth is configured."""
        resp = client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 200

    def test_reject_without_token(self, authed_client):
        """Requests without Authorization header are rejected when auth is configured."""
        resp = authed_client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123"},
                "message": "Hello!",
            },
        )
        assert resp.status_code == 401

    def test_reject_wrong_token(self, authed_client):
        """Requests with wrong token are rejected."""
        resp = authed_client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123"},
                "message": "Hello!",
            },
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_accept_valid_static_token(self, authed_client, mock_adapter):
        """Requests with correct static token are accepted."""
        resp = authed_client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123"},
                "message": "Hello!",
            },
            headers={"Authorization": "Bearer test-secret-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["status"] == "sent"

    def test_accept_valid_jwt(self, jwt_client, mock_adapter):
        """Requests with a valid JWT for the channel audience are accepted."""
        from turnstone.core.auth import JWT_AUD_CHANNEL, create_jwt

        token = create_jwt(
            user_id="system",
            scopes=frozenset({"write"}),
            source="service",
            secret="a" * 32,
            audience=JWT_AUD_CHANNEL,
        )
        resp = jwt_client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123"},
                "message": "Hello!",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_reject_jwt_wrong_audience(self, jwt_client):
        """JWTs with wrong audience are rejected."""
        from turnstone.core.auth import create_jwt

        token = create_jwt(
            user_id="system",
            scopes=frozenset({"write"}),
            source="service",
            secret="a" * 32,
            audience="turnstone-server",  # wrong audience
        )
        resp = jwt_client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123"},
                "message": "Hello!",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    def test_reject_jwt_wrong_secret(self, jwt_client):
        """JWTs signed with wrong secret are rejected."""
        from turnstone.core.auth import JWT_AUD_CHANNEL, create_jwt

        token = create_jwt(
            user_id="system",
            scopes=frozenset({"write"}),
            source="service",
            secret="b" * 32,  # wrong secret
            audience=JWT_AUD_CHANNEL,
        )
        resp = jwt_client.post(
            "/v1/api/notify",
            json={
                "target": {"channel_type": "discord", "channel_id": "123"},
                "message": "Hello!",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    def test_health_bypasses_auth(self, authed_client):
        """Health endpoint is always accessible regardless of auth config."""
        resp = authed_client.get("/health")
        assert resp.status_code == 200
