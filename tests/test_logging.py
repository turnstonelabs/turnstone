"""Tests for turnstone.core.log — structured logging configuration."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from turnstone.core.log import (
    configure_logging,
    ctx_node_id,
    ctx_request_id,
    ctx_user_id,
    ctx_ws_id,
    get_logger,
)


class TestConfigureLogging:
    """Test configure_logging() sets up handlers and formatters."""

    def setup_method(self):
        # Reset structlog and stdlib between tests
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        # Reset context vars
        for var in (ctx_node_id, ctx_ws_id, ctx_user_id, ctx_request_id):
            var.set("")

    def test_sets_root_handler(self):
        configure_logging(level="INFO", json_output=False, service="test")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.level == logging.INFO

    def test_level_debug(self):
        configure_logging(level="DEBUG", json_output=False)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_level_warning(self):
        configure_logging(level="WARNING", json_output=False)
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_json_output(self, capsys):
        configure_logging(level="INFO", json_output=True, service="test-svc")
        log = logging.getLogger("test.json_output")
        log.info("hello world")
        captured = capsys.readouterr()
        # JSON goes to stderr
        line = captured.err.strip()
        data = json.loads(line)
        assert data["event"] == "hello world"
        assert data["level"] == "info"
        assert data["service"] == "test-svc"
        assert "timestamp" in data

    def test_console_output(self, capsys):
        configure_logging(level="INFO", json_output=False)
        log = logging.getLogger("test.console_output")
        log.info("console hello")
        captured = capsys.readouterr()
        assert "console hello" in captured.err

    def test_quiet_third_party(self):
        configure_logging(level="DEBUG", json_output=False)
        for name in (
            "httpx",
            "httpcore",
            "httpx2",
            "httpcore2",
            "openai",
            "anthropic",
            "uvicorn.access",
        ):
            assert logging.getLogger(name).level == logging.WARNING

    def test_guarded_fetch_urls_stay_out_of_info_logs(self, monkeypatch, capsys):
        from tests.test_web_transport import _PUBLIC, _Network, _response
        from turnstone.core.web import fetch_with_ssrf_guard

        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.lower(), raising=False)
        _Network(monkeypatch, {"service.example": [[_PUBLIC]]}, [[_response()]])
        configure_logging(level="INFO", json_output=False)
        logging.getLogger("test.fetch_probe").info("probe line")

        fetch_with_ssrf_guard("http://user:secret@service.example/x?token=abc", timeout=5)

        err = capsys.readouterr().err
        assert "probe line" in err
        assert "service.example" not in err

    def test_httpx2_records_are_redacted_when_its_level_is_lowered(self, monkeypatch, capsys):
        # ANTHROPIC_LOG=info makes the SDK set the httpx2 logger to INFO on import.
        from tests.test_web_transport import _PUBLIC, _Network, _response
        from turnstone.core.web import fetch_with_ssrf_guard

        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.lower(), raising=False)
        _Network(monkeypatch, {"service.example": [[_PUBLIC]]}, [[_response()]])
        configure_logging(level="INFO", json_output=False)
        httpx2_logger = logging.getLogger("httpx2")
        previous = httpx2_logger.level
        httpx2_logger.setLevel(logging.INFO)
        try:
            fetch_with_ssrf_guard(
                "http://user:s3cretpass@service.example/x?password=hunter2&page=2#frag",
                timeout=5,
            )
        finally:
            httpx2_logger.setLevel(previous)

        err = capsys.readouterr().err
        assert 'HTTP Request: GET http://service.example "HTTP/1.1 200 Test"' in err
        assert "s3cretpass" not in err
        assert "hunter2" not in err

    @pytest.mark.parametrize(
        ("url", "origin"),
        [
            ("https://u:pw@h.example:8443/p?sig=abc#frag", "https://h.example:8443"),
            (
                "https://hooks.chat.example/services/T0/B0/XyZsEcReTwEbHoOk",
                "https://hooks.chat.example",
            ),
            ("http://[::1]:8080/p#frag", "http://[::1]:8080"),
            # Rebuilding this URL re-checked its 66 KB percent-encoded path and raised.
            ("http://h.example/" + chr(0xE9) * 11000, "http://h.example"),
        ],
    )
    def test_request_url_argument_is_cut_to_its_origin(self, url, origin):
        import httpx2

        from turnstone.core.log import _RedactCredentialsFilter

        args = ("GET", httpx2.URL(url), "HTTP/1.1", 200, "OK")
        record = logging.LogRecord(
            "httpx2", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"', args, None
        )

        assert _RedactCredentialsFilter().filter(record)

        assert record.getMessage() == f'HTTP Request: GET {origin} "HTTP/1.1 200 OK"'

    def test_every_url_argument_is_cut_to_its_origin(self):
        import httpx2

        from turnstone.core.log import _RedactCredentialsFilter

        url = httpx2.URL("https://hooks.chat.example/services/T0/B0/XyZsEcReTwEbHoOk")
        record = logging.LogRecord(
            "httpx2", logging.INFO, __file__, 1, "%s after %s", (url, url), None
        )

        _RedactCredentialsFilter().filter(record)

        origin = "https://hooks.chat.example"
        assert record.getMessage() == f"{origin} after {origin}"

    def test_filter_is_linear_in_nested_schemes(self):
        # Every nested "https://" once rescanned the rest of the line for a
        # connection string's "@" (seconds at 64 KB).
        import time

        from turnstone.core.log import _RedactCredentialsFilter

        # No "@" anywhere, so every candidate start fails and none may rescan.
        message = "HTTP Request: GET https://h.example/" + "https://" * 8000 + " x"
        record = logging.LogRecord("httpx2", logging.INFO, __file__, 1, message, None, None)
        start = time.perf_counter()
        _RedactCredentialsFilter().filter(record)
        elapsed = time.perf_counter() - start
        assert record.getMessage() == message
        assert elapsed < 2.0

    def test_replaces_existing_handlers(self):
        root = logging.getLogger()
        # Count existing handlers (pytest may add its own)
        before = len(root.handlers)
        root.addHandler(logging.StreamHandler())
        root.addHandler(logging.StreamHandler())
        assert len(root.handlers) == before + 2
        configure_logging(level="INFO", json_output=False)
        # configure_logging clears all and adds exactly 1
        assert len(root.handlers) == 1

    def test_env_var_level_override(self, monkeypatch):
        monkeypatch.setenv("TURNSTONE_LOG_LEVEL", "ERROR")
        configure_logging(level="DEBUG", json_output=False)
        root = logging.getLogger()
        assert root.level == logging.ERROR

    def test_env_var_format_json(self, monkeypatch, capsys):
        monkeypatch.setenv("TURNSTONE_LOG_FORMAT", "json")
        configure_logging(level="INFO", service="test")
        log = logging.getLogger("test.env_json")
        log.info("env json test")
        captured = capsys.readouterr()
        data = json.loads(captured.err.strip())
        assert data["event"] == "env json test"

    def test_env_var_format_text(self, monkeypatch, capsys):
        monkeypatch.setenv("TURNSTONE_LOG_FORMAT", "text")
        configure_logging(level="INFO", json_output=True)  # json_output overridden by env
        log = logging.getLogger("test.env_text")
        log.info("env text test")
        captured = capsys.readouterr()
        # Should NOT be JSON
        line = captured.err.strip()
        assert "env text test" in line
        # Verify it's not JSON
        try:
            json.loads(line)
            is_json = True
        except json.JSONDecodeError:
            is_json = False
        assert not is_json


class TestContextInjection:
    """Test that context variables appear in log output."""

    def setup_method(self):
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        for var in (ctx_node_id, ctx_ws_id, ctx_user_id, ctx_request_id):
            var.set("")

    def test_node_id_in_output(self, capsys):
        configure_logging(level="INFO", json_output=True)
        ctx_node_id.set("worker-01_a3f2")
        log = logging.getLogger("test.ctx")
        log.info("ctx test")
        data = json.loads(capsys.readouterr().err.strip())
        assert data["node_id"] == "worker-01_a3f2"

    def test_ws_id_in_output(self, capsys):
        configure_logging(level="INFO", json_output=True)
        ctx_ws_id.set("abc123")
        log = logging.getLogger("test.ctx")
        log.info("ws test")
        data = json.loads(capsys.readouterr().err.strip())
        assert data["ws_id"] == "abc123"

    def test_empty_context_omitted(self, capsys):
        configure_logging(level="INFO", json_output=True)
        # All context vars are empty string (default)
        log = logging.getLogger("test.ctx")
        log.info("empty ctx")
        data = json.loads(capsys.readouterr().err.strip())
        assert "node_id" not in data
        assert "ws_id" not in data
        assert "user_id" not in data
        assert "request_id" not in data

    def test_multiple_context_vars(self, capsys):
        configure_logging(level="INFO", json_output=True)
        ctx_node_id.set("node-1")
        ctx_ws_id.set("ws-2")
        ctx_request_id.set("req-3")
        log = logging.getLogger("test.ctx")
        log.info("multi ctx")
        data = json.loads(capsys.readouterr().err.strip())
        assert data["node_id"] == "node-1"
        assert data["ws_id"] == "ws-2"
        assert data["request_id"] == "req-3"
        assert "user_id" not in data


class TestGetLogger:
    """Test get_logger() returns a usable bound logger."""

    def setup_method(self):
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)

    def test_get_logger_returns_bound_logger(self):
        configure_logging(level="INFO", json_output=False)
        log = get_logger("test.bound")
        assert log is not None

    def test_get_logger_outputs(self, capsys):
        configure_logging(level="INFO", json_output=True)
        log = get_logger("test.bound")
        log.info("bound logger test", extra_key="extra_val")
        data = json.loads(capsys.readouterr().err.strip())
        assert data["event"] == "bound logger test"
        assert data["extra_key"] == "extra_val"


class TestServiceField:
    """Test that service name is injected when configured."""

    def setup_method(self):
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)

    def test_service_present(self, capsys):
        configure_logging(level="INFO", json_output=True, service="myservice")
        log = logging.getLogger("test.svc")
        log.info("svc test")
        data = json.loads(capsys.readouterr().err.strip())
        assert data["service"] == "myservice"

    def test_no_service_when_empty(self, capsys):
        configure_logging(level="INFO", json_output=True)
        log = logging.getLogger("test.svc")
        log.info("no svc")
        data = json.loads(capsys.readouterr().err.strip())
        assert "service" not in data
