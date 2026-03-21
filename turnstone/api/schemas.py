"""Shared Pydantic v2 models for API request/response schemas.

These models define the API contract for OpenAPI spec generation.
They are NOT used for runtime validation in handlers — they serve
as the single source of truth for the generated OpenAPI spec.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

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


class DeleteSettingResponse(BaseModel):
    """DELETE /v1/api/admin/settings/{key} response."""

    status: str = Field(default="ok", examples=["ok"])
    key: str = Field(description="Dotted setting key that was reset")
    default: Any = Field(description="Registry default value the setting reverted to")


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
    user_id: str = Field(default="", description="Authenticated user ID")
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
    oidc_enabled: bool = False
    oidc_provider_name: str = ""
    password_enabled: bool = True


class AuthWhoamiResponse(BaseModel):
    """GET /v1/api/auth/whoami response."""

    user_id: str
    permissions: str = ""


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


class CreateScheduleRequest(BaseModel):
    """POST /v1/api/admin/schedules request body."""

    name: str = Field(description="Human-readable schedule name")
    description: str = Field(default="", description="Optional description")
    schedule_type: str = Field(description="'cron' or 'at'")
    cron_expr: str = Field(default="", description="Cron expression (when schedule_type='cron')")
    at_time: str = Field(default="", description="ISO8601 timestamp (when schedule_type='at')")
    target_mode: str = Field(default="auto", description="auto, pool, all, or specific node_id")
    model: str = Field(default="", description="Model alias for the workstream")
    initial_message: str = Field(description="Message sent to the new workstream")
    auto_approve: bool = Field(default=False)
    auto_approve_tools: list[str] = Field(default_factory=list)
    skill: str = Field(default="", description="Skill name (replaces default skills)")
    enabled: bool = Field(default=True)


class UpdateScheduleRequest(BaseModel):
    """PUT /v1/api/admin/schedules/{task_id} request body (partial update)."""

    name: str | None = None
    description: str | None = None
    schedule_type: str | None = None
    cron_expr: str | None = None
    at_time: str | None = None
    target_mode: str | None = None
    model: str | None = None
    initial_message: str | None = None
    auto_approve: bool | None = None
    auto_approve_tools: list[str] | None = None
    skill: str | None = None
    enabled: bool | None = None


class ScheduleInfo(BaseModel):
    """Scheduled task details."""

    task_id: str
    name: str
    description: str = ""
    schedule_type: str
    cron_expr: str = ""
    at_time: str = ""
    target_mode: str = "auto"
    model: str = ""
    initial_message: str
    auto_approve: bool = False
    auto_approve_tools: list[str] = Field(default_factory=list)
    skill: str = ""
    enabled: bool = True
    created_by: str = ""
    last_run: str | None = None
    next_run: str | None = None
    created: str = ""
    updated: str = ""


class ListSchedulesResponse(BaseModel):
    """GET /v1/api/admin/schedules response."""

    schedules: list[ScheduleInfo]


class ScheduleRunInfo(BaseModel):
    """Single execution record for a scheduled task."""

    run_id: str
    task_id: str
    node_id: str = ""
    ws_id: str = ""
    correlation_id: str = ""
    started: str
    status: str = "dispatched"
    error: str = ""


class ListScheduleRunsResponse(BaseModel):
    """GET /v1/api/admin/schedules/{task_id}/runs response."""

    runs: list[ScheduleRunInfo]
