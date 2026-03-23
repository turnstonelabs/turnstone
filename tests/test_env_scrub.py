"""Tests for turnstone.core.env — subprocess environment scrubbing."""

from __future__ import annotations

import os
from unittest.mock import patch

from turnstone.core.env import _is_safe, _is_secret, scrubbed_env


class TestIsSecret:
    def test_explicit_scrub_list(self):
        assert _is_secret("OPENAI_API_KEY") is True
        assert _is_secret("ANTHROPIC_API_KEY") is True
        assert _is_secret("TURNSTONE_JWT_SECRET") is True
        assert _is_secret("AWS_SECRET_ACCESS_KEY") is True

    def test_pattern_matching(self):
        assert _is_secret("MY_CUSTOM_API_KEY") is True
        assert _is_secret("DB_PASSWORD") is True
        assert _is_secret("AUTH_TOKEN") is True
        assert _is_secret("SERVICE_CREDENTIAL") is True

    def test_safe_vars_not_secret(self):
        assert _is_secret("PATH") is False
        assert _is_secret("HOME") is False
        assert _is_secret("LANG") is False

    def test_non_secret_vars(self):
        assert _is_secret("PYTHONPATH") is False
        assert _is_secret("EDITOR") is False
        assert _is_secret("GOPATH") is False


class TestIsSafe:
    def test_safe_names(self):
        assert _is_safe("PATH") is True
        assert _is_safe("HOME") is True
        assert _is_safe("TERM") is True
        assert _is_safe("MANWIDTH") is True

    def test_safe_prefixes(self):
        assert _is_safe("LC_ALL") is True
        assert _is_safe("LC_CTYPE") is True
        assert _is_safe("XDG_RUNTIME_DIR") is True

    def test_non_safe_names(self):
        assert _is_safe("OPENAI_API_KEY") is False
        assert _is_safe("CUSTOM_VAR") is False


class TestScrubbedEnv:
    def test_strips_api_keys(self):
        fake_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/user",
            "OPENAI_API_KEY": "sk-secret",
            "ANTHROPIC_API_KEY": "ant-secret",
            "CUSTOM_VAR": "safe_value",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env()

        assert result["PATH"] == "/usr/bin"
        assert result["HOME"] == "/home/user"
        assert result["CUSTOM_VAR"] == "safe_value"
        assert "OPENAI_API_KEY" not in result
        assert "ANTHROPIC_API_KEY" not in result

    def test_strips_pattern_matched_secrets(self):
        fake_env = {
            "PATH": "/usr/bin",
            "MY_SERVICE_TOKEN": "tok-123",
            "DB_PASSWORD": "pass123",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env()

        assert "MY_SERVICE_TOKEN" not in result
        assert "DB_PASSWORD" not in result

    def test_extra_vars_merged(self):
        fake_env = {"PATH": "/usr/bin"}
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env(extra={"MANWIDTH": "80"})

        assert result["MANWIDTH"] == "80"
        assert result["PATH"] == "/usr/bin"

    def test_passthrough_overrides_scrub(self):
        fake_env = {
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "sk-needed",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env(passthrough=["OPENAI_API_KEY"])

        assert result["OPENAI_API_KEY"] == "sk-needed"

    def test_preserves_locale_vars(self):
        fake_env = {
            "PATH": "/usr/bin",
            "LC_ALL": "en_US.UTF-8",
            "LC_CTYPE": "en_US.UTF-8",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env()

        assert result["LC_ALL"] == "en_US.UTF-8"
        assert result["LC_CTYPE"] == "en_US.UTF-8"

    def test_preserves_unknown_non_secret_vars(self):
        fake_env = {
            "PATH": "/usr/bin",
            "PYTHONPATH": "/opt/lib",
            "GOPATH": "/home/user/go",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env()

        assert result["PYTHONPATH"] == "/opt/lib"
        assert result["GOPATH"] == "/home/user/go"

    def test_extra_can_reintroduce_scrubbed_var(self):
        """extra= intentionally overrides scrubbing (operator-controlled)."""
        fake_env = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-original"}
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env(extra={"OPENAI_API_KEY": "sk-injected"})

        assert result["OPENAI_API_KEY"] == "sk-injected"

    def test_less_prefix_does_not_leak_secrets(self):
        """LESS pager vars are safe but LESS_SECRET_TOKEN is not."""
        fake_env = {
            "PATH": "/usr/bin",
            "LESS": "-R",
            "LESSOPEN": "| lesspipe %s",
            "LESS_SECRET_TOKEN": "tok-secret",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = scrubbed_env()

        assert result["LESS"] == "-R"
        assert result["LESSOPEN"] == "| lesspipe %s"
        assert "LESS_SECRET_TOKEN" not in result
