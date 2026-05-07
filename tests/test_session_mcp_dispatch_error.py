"""Tests for ``_format_mcp_dispatch_error`` and the three MCP exec sites.

The Phase 7b pool dispatcher signals user-actionable failures (consent
required, insufficient scope) via ``RuntimeError(json_str)`` where
``json_str`` is the structured-error payload built by
:func:`turnstone.core.mcp_client._structured_error`. The exec sites in
:mod:`turnstone.core.session` previously wrapped that JSON in
``f"MCP X error: {e}"``, destroying the structured shape the dashboard
renderer keys on. The helper preserves the JSON when the exception
text decodes to a structured-error envelope and prefixes otherwise.

Sibling-bug coverage: every exec site (tool / read_resource /
use_prompt) gets two assertions — JSON preserved on a consent-required
exception, JSON-prefixed on a generic transport failure.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tests.test_session import _make_session
from turnstone.core.session import _format_mcp_dispatch_error

# ---------------------------------------------------------------------------
# Unit tests for the helper
# ---------------------------------------------------------------------------


class TestFormatMcpDispatchError:
    def test_preserves_consent_required_payload(self) -> None:
        payload = json.dumps(
            {
                "error": {
                    "code": "mcp_consent_required",
                    "server": "srv-x",
                    "detail": "No token for user. Consent flow required.",
                    "consent_url": "/v1/api/mcp/oauth/start?server=srv-x",
                }
            }
        )
        out = _format_mcp_dispatch_error("MCP tool error", RuntimeError(payload))
        assert out == payload

    def test_preserves_insufficient_scope_payload(self) -> None:
        payload = json.dumps(
            {
                "error": {
                    "code": "mcp_insufficient_scope",
                    "server": "srv-x",
                    "detail": "Tool requires elevated scopes.",
                    "scopes_required": ["read", "write"],
                    "consent_url": "/v1/api/mcp/oauth/start?server=srv-x&scopes=read+write",
                }
            }
        )
        out = _format_mcp_dispatch_error("MCP tool error", RuntimeError(payload))
        assert out == payload

    def test_prefixes_generic_runtime_error(self) -> None:
        out = _format_mcp_dispatch_error("MCP tool error", RuntimeError("connection lost"))
        assert out == "MCP tool error: connection lost"

    def test_prefixes_value_error(self) -> None:
        out = _format_mcp_dispatch_error("MCP tool error", ValueError("bad input"))
        assert out == "MCP tool error: bad input"

    def test_prefixes_random_json_without_mcp_code(self) -> None:
        # JSON that isn't a structured-error envelope must NOT be passed
        # through verbatim — the helper only opens the gate for codes
        # prefixed ``mcp_``.
        payload = json.dumps({"foo": "bar"})
        out = _format_mcp_dispatch_error("MCP tool error", RuntimeError(payload))
        assert out == f"MCP tool error: {payload}"

    def test_prefixes_envelope_with_non_mcp_code(self) -> None:
        payload = json.dumps({"error": {"code": "other_error", "server": "x", "detail": "y"}})
        out = _format_mcp_dispatch_error("MCP tool error", RuntimeError(payload))
        assert out == f"MCP tool error: {payload}"

    def test_prefixes_envelope_without_dict_error(self) -> None:
        payload = json.dumps({"error": "plain string"})
        out = _format_mcp_dispatch_error("MCP tool error", RuntimeError(payload))
        assert out == f"MCP tool error: {payload}"


# ---------------------------------------------------------------------------
# Integration tests against the three MCP exec sites
# ---------------------------------------------------------------------------


_CONSENT_REQUIRED_JSON = json.dumps(
    {
        "error": {
            "code": "mcp_consent_required",
            "server": "srv-oauth",
            "detail": "No token for user. Consent flow required.",
            "consent_url": "/v1/api/mcp/oauth/start?server=srv-oauth",
        }
    }
)


def _record_outputs(session) -> list[tuple[str, str, str, bool]]:
    """Patch ``_report_tool_result`` to capture (call_id, name, output, is_error)."""
    captures: list[tuple[str, str, str, bool]] = []

    def _capture(call_id: str, name: str, output: str, *, is_error: bool = False) -> None:
        captures.append((call_id, name, output, is_error))

    session._report_tool_result = _capture  # type: ignore[method-assign]
    return captures


class TestExecMcpToolDispatchError:
    def test_exec_mcp_tool_preserves_structured_error_json(self, tmp_db) -> None:
        session = _make_session()
        captures = _record_outputs(session)

        mock_client = MagicMock()
        mock_client.call_tool_sync.side_effect = RuntimeError(_CONSENT_REQUIRED_JSON)
        session._mcp_client = mock_client

        item = {
            "call_id": "tc_1",
            "mcp_func_name": "mcp__srv-oauth__do",
            "mcp_args": {},
        }
        session._exec_mcp_tool(item)

        assert len(captures) == 1
        _, _, output, is_error = captures[0]
        assert output == _CONSENT_REQUIRED_JSON
        assert is_error is True

    def test_exec_mcp_tool_prefixes_non_structured_error(self, tmp_db) -> None:
        session = _make_session()
        captures = _record_outputs(session)

        mock_client = MagicMock()
        mock_client.call_tool_sync.side_effect = RuntimeError("connection lost")
        session._mcp_client = mock_client

        item = {
            "call_id": "tc_2",
            "mcp_func_name": "mcp__srv-oauth__do",
            "mcp_args": {},
        }
        session._exec_mcp_tool(item)

        assert captures[0][2] == "MCP tool error: connection lost"
        assert captures[0][3] is True


class TestExecReadResourceDispatchError:
    def test_exec_read_resource_preserves_structured_error_json(self, tmp_db) -> None:
        session = _make_session()
        captures = _record_outputs(session)

        mock_client = MagicMock()
        mock_client.read_resource_sync.side_effect = RuntimeError(_CONSENT_REQUIRED_JSON)
        session._mcp_client = mock_client

        item = {
            "call_id": "rc_1",
            "resource_uri": "https://example.com/r",
        }
        # The exec site emits a ``log.warning`` (no ``exc_info`` — bearer-leak
        # invariant) on failure. Patch the logger so the test doesn't emit
        # noise to the captured stderr — assertions don't depend on log
        # output.
        with patch("turnstone.core.session.log"):
            session._exec_read_resource(item)

        assert len(captures) == 1
        assert captures[0][2] == _CONSENT_REQUIRED_JSON
        assert captures[0][3] is True

    def test_exec_read_resource_prefixes_non_structured_error(self, tmp_db) -> None:
        session = _make_session()
        captures = _record_outputs(session)

        mock_client = MagicMock()
        mock_client.read_resource_sync.side_effect = RuntimeError("connection lost")
        session._mcp_client = mock_client

        item = {
            "call_id": "rc_2",
            "resource_uri": "https://example.com/r",
        }
        with patch("turnstone.core.session.log"):
            session._exec_read_resource(item)

        assert captures[0][2] == "MCP resource error: connection lost"
        assert captures[0][3] is True


class TestExecUsePromptDispatchError:
    def test_exec_use_prompt_preserves_structured_error_json(self, tmp_db) -> None:
        session = _make_session()
        captures = _record_outputs(session)

        mock_client = MagicMock()
        mock_client.get_prompt_sync.side_effect = RuntimeError(_CONSENT_REQUIRED_JSON)
        session._mcp_client = mock_client

        item = {
            "call_id": "pc_1",
            "prompt_name": "mcp__srv-oauth__greet",
            "prompt_arguments": {},
        }
        with patch("turnstone.core.session.log"):
            session._exec_use_prompt(item)

        assert len(captures) == 1
        assert captures[0][2] == _CONSENT_REQUIRED_JSON
        assert captures[0][3] is True

    def test_exec_use_prompt_prefixes_non_structured_error(self, tmp_db) -> None:
        session = _make_session()
        captures = _record_outputs(session)

        mock_client = MagicMock()
        mock_client.get_prompt_sync.side_effect = RuntimeError("connection lost")
        session._mcp_client = mock_client

        item = {
            "call_id": "pc_2",
            "prompt_name": "mcp__srv-oauth__greet",
            "prompt_arguments": {},
        }
        with patch("turnstone.core.session.log"):
            session._exec_use_prompt(item)

        assert captures[0][2] == "MCP prompt error: connection lost"
        assert captures[0][3] is True
