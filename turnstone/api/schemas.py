"""Shared Pydantic v2 models for API request/response schemas.

These models define the API contract for OpenAPI spec generation.
They are NOT used for runtime validation in handlers — they serve
as the single source of truth for the generated OpenAPI spec.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class WorkstreamState(StrEnum):
    """Workstream lifecycle states."""

    idle = "idle"
    thinking = "thinking"
    running = "running"
    attention = "attention"
    error = "error"


class ErrorResponse(BaseModel):
    """Standard error response body."""

    error: str = Field(description="Error message")


class StatusResponse(BaseModel):
    """Generic success response."""

    status: str = Field(default="ok", examples=["ok"])


class AuthLoginRequest(BaseModel):
    """POST /v1/api/auth/login request body."""

    token: str = Field(description="Bearer token to authenticate")


class AuthLoginResponse(BaseModel):
    """POST /v1/api/auth/login success response."""

    status: str = Field(default="ok")
    role: str = Field(description="Assigned role", examples=["full", "read"])
