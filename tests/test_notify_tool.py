"""Tests for the notify tool (prepare + execute) in ChatSession."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from turnstone.core.session import ChatSession


def _make_session() -> ChatSession:
    """Create a minimal ChatSession with mocked dependencies."""
    from unittest.mock import patch

    with (
        patch("turnstone.core.memory.register_workstream"),
        patch("turnstone.core.session.save_message"),
    ):
        from turnstone.core.session import ChatSession

        ui = MagicMock()
        session = ChatSession(
            client=MagicMock(),
            model="test-model",
            ui=ui,
            instructions=None,
            temperature=0.7,
            max_tokens=1000,
            tool_timeout=30,
        )
    return session


class TestNotifyAuthHeaders:
    @pytest.fixture(autouse=True)
    def auth_config(self, tmp_path, monkeypatch):
        import turnstone.core.config as config
        import turnstone.core.session as session

        config_path = tmp_path / "config.toml"
        config_path.touch(mode=0o600)
        monkeypatch.setattr(config, "_config_path", config_path)
        monkeypatch.setattr(config, "_cache", None)
        monkeypatch.setattr(session, "_notify_token_manager", None)
        monkeypatch.delenv("TURNSTONE_JWT_SECRET", raising=False)
        monkeypatch.delenv("TURNSTONE_CHANNEL_AUTH_TOKEN", raising=False)
        return config_path

    def test_config_only_secret_authenticates_with_gateway(self, auth_config):
        """The documented bare-metal config must authenticate outbound notifications."""
        from unittest.mock import AsyncMock

        from starlette.testclient import TestClient

        from turnstone.channels._http import create_channel_app
        from turnstone.core.session import _notify_auth_headers

        secret = "a" * 32
        auth_config.write_text(f'[auth]\njwt_secret = "  {secret}  "\n')
        adapter = AsyncMock()
        adapter.send.return_value = "message-1"
        app = create_channel_app({"discord": adapter}, MagicMock(), jwt_secret=secret)
        with TestClient(app) as client:
            response = client.post(
                "/v1/api/notify",
                json={
                    "target": {"channel_type": "discord", "channel_id": "123"},
                    "message": "Hello!",
                },
                headers=_notify_auth_headers(),
            )

        assert response.status_code == 200
        assert response.json()["results"][0]["status"] == "sent"
        adapter.send.assert_awaited_once_with("123", "Hello!")

    def test_environment_secret_takes_precedence(self, auth_config, monkeypatch):
        from turnstone.core.auth import JWT_AUD_CHANNEL, validate_jwt
        from turnstone.core.session import _notify_auth_headers

        config_secret = "a" * 32
        auth_config.write_text(f'[auth]\njwt_secret = "{config_secret}"\n')
        secret = "b" * 32
        monkeypatch.setenv("TURNSTONE_JWT_SECRET", f"  {secret}  ")

        token = _notify_auth_headers()["Authorization"].removeprefix("Bearer ")
        auth = validate_jwt(token, secret, audience=JWT_AUD_CHANNEL)
        assert auth is not None
        assert "write" in auth.scopes

    def test_static_token_takes_precedence(self, auth_config, monkeypatch):
        from turnstone.core.session import _notify_auth_headers

        config_secret = "a" * 32
        auth_config.write_text(f'[auth]\njwt_secret = "{config_secret}"\n')
        monkeypatch.setenv("TURNSTONE_JWT_SECRET", "b" * 32)
        monkeypatch.setenv("TURNSTONE_CHANNEL_AUTH_TOKEN", "  static-token  ")
        assert _notify_auth_headers() == {"Authorization": "Bearer static-token"}

    def test_missing_secret_returns_no_headers(self):
        from turnstone.core.session import _notify_auth_headers

        assert _notify_auth_headers() == {}


class TestNotifyDiagnostics:
    @pytest.fixture(params=["tool", "completion"])
    def notify_caller(self, request, monkeypatch):
        """Exercise both outbound paths with the same gateway failures."""
        import turnstone.core.session as session_module
        import turnstone.server as server_module

        session = _make_session()
        session._ws_id = "abcdef1234567890"
        monkeypatch.setattr(session, "_backoff_or_cancelled", MagicMock())
        monkeypatch.setattr(server_module.time, "sleep", MagicMock())
        monkeypatch.setattr(
            session_module,
            "_notify_auth_headers",
            lambda: {"Authorization": "Bearer private-auth-token"},
        )
        storage = MagicMock()
        monkeypatch.setattr(session_module, "get_storage", lambda: storage)
        caller_module = session_module if request.param == "tool" else server_module
        logger = MagicMock()
        monkeypatch.setattr(caller_module, "log", logger)
        post = MagicMock()
        monkeypatch.setattr(session_module.httpx, "post", post)

        def deliver():
            if request.param == "tool":
                _, result = session._exec_notify(
                    {
                        "call_id": "call-1",
                        "channel_type": "discord",
                        "channel_id": "123",
                        "message": "private-message-content",
                    }
                )
                assert result == "Error: notification delivery failed"
                assert session._notify_count == 0
            else:
                server_module._deliver_notification(
                    storage,
                    {"ws_id": session._ws_id, "message": "private-message-content"},
                    {"Authorization": "Bearer private-auth-token"},
                )

        return storage, post, logger, deliver, request.param

    def test_preserves_each_gateway_failure_without_secrets(self, notify_caller):
        storage, post, logger, deliver, caller = notify_caller
        storage.list_services.return_value = [
            {"service_id": "gateway-1", "url": "http://user:private-password@gw.example.com:8091"},
            {"service_id": "gateway-2", "url": "http://gw2.example.com:8091"},
        ]
        post.side_effect = [
            MagicMock(status_code=401),
            ConnectionError("private-exception-content"),
        ] * 3

        deliver()

        failures = [c.kwargs for c in logger.warning.call_args_list if "gateway_id" in c.kwargs]
        assert len(failures) == 6
        for attempt in range(1, 4):
            rejected, unreachable = failures[(attempt - 1) * 2 : attempt * 2]
            assert rejected["gateway_id"] == "gateway-1"
            assert rejected["gateway_url"] == "http://gw.example.com:8091/v1/api/notify"
            assert rejected.get("status_code", rejected.get("status")) == 401
            assert unreachable["gateway_id"] == "gateway-2"
            assert unreachable["error_type"] == "ConnectionError"
            for failure in (rejected, unreachable):
                assert failure["attempt"] == attempt
                assert failure["ws_id"] == "abcdef1234567890"
                assert failure["auth_present"] is True
                if caller == "tool":
                    assert failure["call_id"] == "call-1"
        assert "private-" not in repr(logger.mock_calls)

    @pytest.mark.parametrize(
        "response_kind", ["failed_deliveries", "invalid_json", "invalid_results"]
    )
    def test_unsuccessful_response_logs_safe_details(self, notify_caller, response_kind):
        storage, post, logger, deliver, _ = notify_caller
        storage.list_services.return_value = [
            {"service_id": "gateway-1", "url": "http://gw.example.com:8091"},
        ]
        response = MagicMock(status_code=200)
        if response_kind == "invalid_json":
            response.json.side_effect = ValueError("private-response-content")
        elif response_kind == "invalid_results":
            response.json.return_value = {"results": "private-response-content"}
        else:
            response.json.return_value = {
                "results": [
                    {"status": "no_adapter", "channel_id": "private-target"},
                    {"status": "private-response-content"},
                    {"status": ["private-response-content"]},
                ]
            }
        post.return_value = response

        deliver()

        failures = [c for c in logger.warning.call_args_list if "gateway_id" in c.kwargs]
        assert len(failures) == 3
        for failure in failures:
            if response_kind == "invalid_json":
                assert (
                    failure.kwargs.get("reason") == "invalid_response"
                    or failure.args[0] == "notify_completion.response_parse_error"
                )
            else:
                expected = (
                    ["invalid_response"]
                    if response_kind == "invalid_results"
                    else ["no_adapter", "unknown"]
                )
                assert failure.kwargs["delivery_statuses"] == expected
        assert "private-" not in repr(logger.mock_calls)

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://user:password@gw.example.com/prefix?token=secret#fragment",
                "https://gw.example.com/prefix",
            ),
            (
                "http://user:password@[2001:db8::1]:8091/v1/api/notify",
                "http://[2001:db8::1]:8091/v1/api/notify",
            ),
            ("http://[invalid", "<invalid URL>"),
            ("file:///private-credential", "<invalid URL>"),
        ],
    )
    def test_gateway_url_redaction(self, url, expected):
        from turnstone.core.session import _notify_log_url

        assert _notify_log_url(url) == expected


class TestPrepareNotify:
    def test_valid_username_target(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "Hello!",
                "username": "admin",
            },
        )
        assert "execute" in result
        assert result["func_name"] == "notify"
        assert result["needs_approval"] is False
        assert "@admin" in result["header"]
        assert result["username"] == "admin"
        assert result["message"] == "Hello!"

    def test_valid_direct_target(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "Hello!",
                "channel_type": "discord",
                "channel_id": "123456",
            },
        )
        assert "execute" in result
        assert result["channel_type"] == "discord"
        assert result["channel_id"] == "123456"
        assert "discord:123456" in result["header"]

    def test_missing_message(self):
        session = _make_session()
        result = session._prepare_notify("call_1", {"username": "admin"})
        assert "error" in result
        assert "message" in result["error"].lower()

    def test_empty_message(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "",
                "username": "admin",
            },
        )
        assert "error" in result

    def test_message_too_long(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "x" * 2001,
                "username": "admin",
            },
        )
        assert "error" in result
        assert "2000" in result["error"]

    def test_both_username_and_direct(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "Hello!",
                "username": "admin",
                "channel_type": "discord",
                "channel_id": "123",
            },
        )
        assert "error" in result
        assert "both" in result["error"].lower() or "ambiguous" in result["error"].lower()

    def test_no_target(self):
        session = _make_session()
        result = session._prepare_notify("call_1", {"message": "Hello!"})
        assert "error" in result

    def test_channel_type_without_id(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "Hello!",
                "channel_type": "discord",
            },
        )
        assert "error" in result
        assert "channel_id" in result["error"]

    def test_channel_id_without_type(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "Hello!",
                "channel_id": "123456",
            },
        )
        assert "error" in result
        assert "channel_type" in result["error"]

    def test_preview_truncated(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "a" * 200,
                "username": "admin",
            },
        )
        assert result["preview"].endswith("...")
        assert len(result["preview"]) <= 123  # 120 chars + "..."

    def test_title_passed_through(self):
        session = _make_session()
        result = session._prepare_notify(
            "call_1",
            {
                "message": "Hello!",
                "username": "admin",
                "title": "Alert",
            },
        )
        assert result["title"] == "Alert"


class TestExecNotify:
    def test_sends_http_to_channel_gateway(self, tmp_path, sqlite_backend_factory):

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "Alert",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"channel_type": "discord", "channel_id": "123", "status": "sent"}]
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch("turnstone.core.session.httpx.post", return_value=mock_resp) as mock_post,
            patch.dict("os.environ", {}, clear=False),
        ):
            call_id, msg = session._exec_notify(item)

        assert call_id == "call_1"
        assert "sent successfully" in msg.lower()
        mock_post.assert_called_once()
        post_kwargs = mock_post.call_args
        assert post_kwargs.kwargs["json"]["target"] == {"username": "admin"}
        assert post_kwargs.kwargs["json"]["message"] == "Hello!"

    def test_no_services_available(self, tmp_path, sqlite_backend_factory):

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        # No services registered

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch.object(session, "_backoff_or_cancelled"),
        ):
            call_id, msg = session._exec_notify(item)

        assert "no channel gateway" in msg.lower()

    def test_rate_limit(self, tmp_path, sqlite_backend_factory):

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"channel_type": "discord", "channel_id": "123", "status": "sent"}]
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch("turnstone.core.session.httpx.post", return_value=mock_resp),
            patch.dict("os.environ", {}, clear=False),
        ):
            for _i in range(5):
                call_id, msg = session._exec_notify(item)
                assert "sent successfully" in msg.lower()

            # 6th should fail
            call_id, msg = session._exec_notify(item)
            assert "rate limit" in msg.lower()

    def test_rate_limit_not_consumed_on_failure(self, tmp_path, sqlite_backend_factory):
        """Failed delivery should not consume rate limit slots."""

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch(
                "turnstone.core.session.httpx.post",
                side_effect=ConnectionError("refused"),
            ),
            patch.dict("os.environ", {}, clear=False),
            patch.object(session, "_backoff_or_cancelled"),
        ):
            # All fail — counter should stay at 0
            for _i in range(3):
                session._exec_notify(item)
            assert session._notify_count == 0

    def test_counter_on_init(self):
        session = _make_session()
        assert session._notify_count == 0

    def test_http_failure_reported(self, tmp_path, sqlite_backend_factory):

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "",
            "channel_type": "discord",
            "channel_id": "999",
            "title": "",
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch(
                "turnstone.core.session.httpx.post",
                side_effect=ConnectionError("refused"),
            ),
            patch.dict("os.environ", {}, clear=False),
            patch.object(session, "_backoff_or_cancelled"),
        ):
            call_id, msg = session._exec_notify(item)

        # Error message should be generic (no internal details)
        assert "delivery failed" in msg.lower()
        assert "refused" not in msg
        assert "ch-1" not in msg

    def test_first_healthy_only(self, tmp_path, sqlite_backend_factory):
        """Only the first healthy gateway should receive the request."""

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")
        storage.register_service("channel", "ch-2", "http://localhost:8092")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"channel_type": "discord", "channel_id": "123", "status": "sent"}]
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch("turnstone.core.session.httpx.post", return_value=mock_resp) as mock_post,
            patch.dict("os.environ", {}, clear=False),
        ):
            session._exec_notify(item)

        # Should only have been called once (first healthy)
        assert mock_post.call_count == 1

    def test_ssrf_protection(self, tmp_path, sqlite_backend_factory):
        """URLs with non-http(s) schemes should be skipped."""

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        # Register a service with an invalid scheme
        storage.register_service("channel", "ch-bad", "ftp://evil.example.com")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch("turnstone.core.session.httpx.post") as mock_post,
            patch.dict("os.environ", {}, clear=False),
            patch.object(session, "_backoff_or_cancelled"),
        ):
            call_id, msg = session._exec_notify(item)

        # httpx.post should never be called for ftp:// URL
        mock_post.assert_not_called()
        assert "delivery failed" in msg.lower()

    def test_retry_on_no_services(self, tmp_path):
        """Retries service lookup when no gateways are initially available."""
        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        # First two calls return empty, third returns a service
        call_count = 0

        def _list_services(stype: str, max_age_seconds: int = 120) -> list[dict[str, str]]:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return []
            return [
                {
                    "service_type": "channel",
                    "service_id": "ch-1",
                    "url": "http://localhost:8091",
                    "metadata": "{}",
                    "last_heartbeat": "",
                    "created": "",
                }
            ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"channel_type": "discord", "channel_id": "123", "status": "sent"}]
        }

        from unittest.mock import patch

        mock_storage = MagicMock()
        mock_storage.list_services = _list_services

        with (
            patch("turnstone.core.session.get_storage", return_value=mock_storage),
            patch("turnstone.core.session.httpx.post", return_value=mock_resp),
            patch.dict("os.environ", {}, clear=False),
            patch.object(session, "_backoff_or_cancelled") as mock_backoff,
        ):
            call_id, msg = session._exec_notify(item)

        assert "sent successfully" in msg.lower()
        # Should have backed off twice (retry delays) — via the shared
        # cancel-aware helper, not a Stop-blind time.sleep.
        assert mock_backoff.call_count == 2

    def test_retry_on_all_gateways_failed(self, tmp_path, sqlite_backend_factory):
        """Retries when all gateways fail on first attempt but succeed on retry."""

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        call_count = 0

        def _post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise ConnectionError("refused")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "results": [{"channel_type": "discord", "channel_id": "123", "status": "sent"}]
            }
            return resp

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch("turnstone.core.session.httpx.post", side_effect=_post),
            patch.dict("os.environ", {}, clear=False),
            patch.object(session, "_backoff_or_cancelled") as mock_backoff,
        ):
            call_id, msg = session._exec_notify(item)

        assert "sent successfully" in msg.lower()
        assert mock_backoff.call_count == 1

    def test_no_services_logs_warning(self, tmp_path, sqlite_backend_factory):
        """Server-side warning is logged when no services are available."""

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch.object(session, "_backoff_or_cancelled"),
            patch("turnstone.core.session.log") as mock_log,
        ):
            session._exec_notify(item)

        # Should have logged warnings for retries + final exhaustion
        warning_calls = [c for c in mock_log.warning.call_args_list]
        assert len(warning_calls) >= 3  # 2 retry warnings + 1 exhaustion
        events = [c.args[0] for c in warning_calls]
        assert "notify.no_services" in events
        assert "notify.no_services_exhausted" in events

    def test_all_gateways_failed_logs_warning(self, tmp_path, sqlite_backend_factory):
        """Server-side warning is logged when all gateways fail."""

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch(
                "turnstone.core.session.httpx.post",
                side_effect=ConnectionError("refused"),
            ),
            patch.dict("os.environ", {}, clear=False),
            patch.object(session, "_backoff_or_cancelled"),
            patch("turnstone.core.session.log") as mock_log,
        ):
            session._exec_notify(item)

        warning_calls = [c for c in mock_log.warning.call_args_list]
        events = [c.args[0] for c in warning_calls]
        # 2 retry warnings + 1 final failure
        assert "notify.all_gateways_failed" in events
        assert "notify.delivery_failed" in events

    def test_gateway_200_but_no_delivery(self, tmp_path, sqlite_backend_factory):
        """HTTP 200 with all results failed should not count as success."""

        storage = sqlite_backend_factory(str(tmp_path / "test.db"))
        storage.register_service("channel", "ch-1", "http://localhost:8091")

        session = _make_session()
        item = {
            "call_id": "call_1",
            "func_name": "notify",
            "message": "Hello!",
            "username": "admin",
            "channel_type": "",
            "channel_id": "",
            "title": "",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"channel_type": "discord", "channel_id": "123", "status": "no_adapter"}]
        }

        from unittest.mock import patch

        with (
            patch("turnstone.core.session.get_storage", return_value=storage),
            patch("turnstone.core.session.httpx.post", return_value=mock_resp),
            patch.dict("os.environ", {}, clear=False),
            patch.object(session, "_backoff_or_cancelled"),
        ):
            call_id, msg = session._exec_notify(item)

        assert "delivery failed" in msg.lower()
        assert session._notify_count == 0
