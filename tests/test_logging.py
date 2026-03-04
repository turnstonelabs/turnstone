"""Tests for turnstone.core.log — structured logging configuration."""

from __future__ import annotations

import json
import logging

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
        for name in ("httpx", "httpcore", "openai", "anthropic", "uvicorn.access"):
            assert logging.getLogger(name).level == logging.WARNING

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
