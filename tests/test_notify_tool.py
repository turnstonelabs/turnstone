"""Tests for the notify tool (prepare + execute) in ChatSession."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

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
    def test_sends_http_to_channel_gateway(self, tmp_path):
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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

    def test_no_services_available(self, tmp_path):
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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
            patch("turnstone.core.session.time.sleep"),
        ):
            call_id, msg = session._exec_notify(item)

        assert "no channel gateway" in msg.lower()

    def test_rate_limit(self, tmp_path):
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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

    def test_rate_limit_not_consumed_on_failure(self, tmp_path):
        """Failed delivery should not consume rate limit slots."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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
            patch("turnstone.core.session.time.sleep"),
        ):
            # All fail — counter should stay at 0
            for _i in range(3):
                session._exec_notify(item)
            assert session._notify_count == 0

    def test_counter_on_init(self):
        session = _make_session()
        assert session._notify_count == 0

    def test_http_failure_reported(self, tmp_path):
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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
            patch("turnstone.core.session.time.sleep"),
        ):
            call_id, msg = session._exec_notify(item)

        # Error message should be generic (no internal details)
        assert "delivery failed" in msg.lower()
        assert "refused" not in msg
        assert "ch-1" not in msg

    def test_first_healthy_only(self, tmp_path):
        """Only the first healthy gateway should receive the request."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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

    def test_ssrf_protection(self, tmp_path):
        """URLs with non-http(s) schemes should be skipped."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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
            patch("turnstone.core.session.time.sleep"),
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
            patch("turnstone.core.session.time.sleep") as mock_sleep,
        ):
            call_id, msg = session._exec_notify(item)

        assert "sent successfully" in msg.lower()
        # Should have slept twice (retry delays)
        assert mock_sleep.call_count == 2

    def test_retry_on_all_gateways_failed(self, tmp_path):
        """Retries when all gateways fail on first attempt but succeed on retry."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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
            patch("turnstone.core.session.time.sleep") as mock_sleep,
        ):
            call_id, msg = session._exec_notify(item)

        assert "sent successfully" in msg.lower()
        assert mock_sleep.call_count == 1

    def test_no_services_logs_warning(self, tmp_path):
        """Server-side warning is logged when no services are available."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))

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
            patch("turnstone.core.session.time.sleep"),
            patch("turnstone.core.session.log") as mock_log,
        ):
            session._exec_notify(item)

        # Should have logged warnings for retries + final exhaustion
        warning_calls = [c for c in mock_log.warning.call_args_list]
        assert len(warning_calls) >= 3  # 2 retry warnings + 1 exhaustion
        events = [c.args[0] for c in warning_calls]
        assert "notify.no_services" in events
        assert "notify.no_services_exhausted" in events

    def test_all_gateways_failed_logs_warning(self, tmp_path):
        """Server-side warning is logged when all gateways fail."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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
            patch("turnstone.core.session.time.sleep"),
            patch("turnstone.core.session.log") as mock_log,
        ):
            session._exec_notify(item)

        warning_calls = [c for c in mock_log.warning.call_args_list]
        events = [c.args[0] for c in warning_calls]
        # 2 retry warnings + 1 final failure
        assert "notify.all_gateways_failed" in events
        assert "notify.delivery_failed" in events

    def test_gateway_200_but_no_delivery(self, tmp_path):
        """HTTP 200 with all results failed should not count as success."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_path / "test.db"))
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
            patch("turnstone.core.session.time.sleep"),
        ):
            call_id, msg = session._exec_notify(item)

        assert "delivery failed" in msg.lower()
        assert session._notify_count == 0
