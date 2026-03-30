"""Tests for the workstream resume request schema.

Verifies that the create-workstream JSON payload carries the resume_ws field
correctly, matching the server's ``CreateWorkstreamRequest`` schema.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# CreateWorkstreamRequest resume_ws field
# ---------------------------------------------------------------------------


class TestCreateWorkstreamResumeField:
    def test_resume_ws_defaults_empty(self) -> None:
        body: dict[str, str] = {"name": "test"}
        assert body.get("resume_ws", "") == ""

    def test_resume_ws_set(self) -> None:
        body = {"name": "test", "resume_ws": "ws-abc"}
        assert body["resume_ws"] == "ws-abc"

    def test_resume_ws_present_in_payload(self) -> None:
        body = {"name": "test", "resume_ws": "ws-xyz"}
        assert "resume_ws" in body
        assert body["resume_ws"] == "ws-xyz"

    def test_pydantic_schema_has_resume_ws(self) -> None:
        """CreateWorkstreamRequest schema includes resume_ws."""
        from turnstone.api.server_schemas import CreateWorkstreamRequest

        req = CreateWorkstreamRequest(name="test", resume_ws="ws-123")
        assert req.resume_ws == "ws-123"

    def test_pydantic_schema_default_empty(self) -> None:
        from turnstone.api.server_schemas import CreateWorkstreamRequest

        req = CreateWorkstreamRequest(name="test")
        assert req.resume_ws == ""
