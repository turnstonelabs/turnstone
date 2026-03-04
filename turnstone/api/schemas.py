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
    """POST /v1/api/auth/login request body.

    Either username+password or token must be provided.
    """

    username: str = Field(default="", description="Login username")
    password: str = Field(default="", description="Login password")
    token: str = Field(default="", description="Legacy: bearer token to authenticate")


class AuthLoginResponse(BaseModel):
    """POST /v1/api/auth/login success response."""

    status: str = Field(default="ok")
    role: str = Field(description="Legacy role", examples=["full", "read"])
    scopes: str = Field(
        default="", description="Comma-separated scopes", examples=["read,write,approve"]
    )
    jwt: str = Field(default="", description="JWT session token (if JWT auth is configured)")


# ---------------------------------------------------------------------------
# Admin — User identity + API tokens
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    """POST /v1/api/admin/users request body."""

    username: str = Field(description="Login username (unique)")
    display_name: str = Field(description="Human-readable display name")
    password: str = Field(description="Initial password")


class UserInfo(BaseModel):
    """User record (no password_hash)."""

    user_id: str
    username: str
    display_name: str
    created: str


class ListUsersResponse(BaseModel):
    """GET /v1/api/admin/users response."""

    users: list[UserInfo]


class CreateTokenRequest(BaseModel):
    """POST /v1/api/admin/users/{user_id}/tokens request body."""

    name: str = Field(default="", description="Human label for the token")
    scopes: str = Field(
        default="read,write,approve",
        description="Comma-separated scopes: read, write, approve",
    )
    expires_days: int | None = Field(
        default=None,
        description="Days until expiry (null = no expiry)",
    )


class TokenInfo(BaseModel):
    """Token metadata (never includes the hash or raw token)."""

    token_id: str
    token_prefix: str
    name: str
    scopes: str
    created: str
    expires: str | None = None


class CreateTokenResponse(BaseModel):
    """POST /v1/api/admin/users/{user_id}/tokens response (raw token shown once)."""

    token: str = Field(description="Raw API token — save this, it cannot be retrieved again")
    token_id: str
    token_prefix: str
    scopes: str


class ListTokensResponse(BaseModel):
    """GET /v1/api/admin/users/{user_id}/tokens response."""

    tokens: list[TokenInfo]


# ---------------------------------------------------------------------------
# Auth — Setup + status
# ---------------------------------------------------------------------------


class AuthSetupRequest(BaseModel):
    """POST /v1/api/auth/setup request body."""

    username: str = Field(description="Login username (1-64 ASCII characters)")
    display_name: str = Field(description="Display name")
    password: str = Field(description="Password (minimum 8 characters)")


class AuthSetupResponse(BaseModel):
    """POST /v1/api/auth/setup success response."""

    status: str = Field(default="ok")
    user_id: str
    username: str
    role: str = Field(default="full")
    scopes: str = Field(default="approve,read,write")
    jwt: str = Field(default="", description="JWT session token")


class AuthStatusResponse(BaseModel):
    """GET /v1/api/auth/status response."""

    auth_enabled: bool
    has_users: bool
    setup_required: bool
