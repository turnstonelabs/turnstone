"""Server endpoint catalog for OpenAPI spec generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.api.openapi import EndpointSpec, PathParam, QueryParam, build_openapi

if TYPE_CHECKING:
    from pydantic import BaseModel
from turnstone.api.schemas import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    AuthWhoamiResponse,
    ErrorResponse,
    StatusResponse,
)
from turnstone.api.server_schemas import (
    MEMORY_NAME_INPUT_DESCRIPTION,
    ApproveRequest,
    ApproveResponse,
    AvailableModelInfo,
    CancelRequest,
    CancelResponse,
    CloseWorkstreamRequest,
    CommandRequest,
    CreateWorkstreamRequest,
    CreateWorkstreamResponse,
    DashboardResponse,
    DequeueRequest,
    HealthResponse,
    ListAttachmentsResponse,
    ListAvailableModelsResponse,
    ListMemoriesResponse,
    ListPersonaChoicesResponse,
    ListSavedWorkstreamsResponse,
    ListSkillSummaryResponse,
    ListWorkstreamsResponse,
    MemoryInfo,
    MemorySummary,
    PersonaChoice,
    RewindRequest,
    SaveMemoryRequest,
    SearchMemoriesRequest,
    SendRequest,
    SendResponse,
    SkillSummary,
    SpeechToTextResponse,
    TextToSpeechRequest,
    UploadAttachmentResponse,
    WorkstreamDetailResponse,
    WorkstreamHistoryResponse,
)

SERVER_ENDPOINTS: list[EndpointSpec] = [
    # --- Workstream management ---
    EndpointSpec(
        "/v1/api/workstreams",
        "GET",
        "List active workstreams",
        response_model=ListWorkstreamsResponse,
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/dashboard",
        "GET",
        "Dashboard with workstream details and aggregates",
        response_model=DashboardResponse,
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/new",
        "POST",
        "Create a new workstream",
        description=(
            "Accepts two content types. Default is `application/json` with a "
            "`CreateWorkstreamRequest` body. Alternatively, `multipart/form-data` "
            "with one `meta` field (JSON-encoded `CreateWorkstreamRequest` shape) "
            "plus zero-or-more `file` parts saves each file as an attachment "
            "under the new workstream. When `initial_message` is also set, "
            "attachments are resolved onto that turn before the worker thread "
            "dispatches; otherwise they remain pending for a follow-up "
            "`POST /v1/api/workstreams/{ws_id}/send`. Setting `resume_ws` "
            "atomically forks the visible source history, configuration, project, "
            "persona, and attachment references into a distinct destination; it "
            "does not reopen or mutate the source. Attachments and `resume_ws` "
            "cannot be combined. Creation stays unpublished until validation and "
            "the optional fork transaction complete."
        ),
        request_model=CreateWorkstreamRequest,
        response_model=CreateWorkstreamResponse,
        error_codes=[400, 403, 404, 409, 413, 429, 500, 503],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/close",
        "POST",
        "Close a workstream",
        description=(
            "Unloads the live workstream while preserving storage. Returns 409 "
            "when any accepted live conversation row still requires persistence "
            "reconciliation; the workstream remains loaded and its history journal "
            "is retained."
        ),
        request_model=CloseWorkstreamRequest,
        response_model=StatusResponse,
        error_codes=[400, 404, 409],
        tags=["Workstreams"],
    ),
    # --- Chat ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/send",
        "POST",
        "Send a user message",
        request_model=SendRequest,
        response_model=SendResponse,
        # 409: cross_user_interjection (shipped with the multi-user work;
        # the spec had drifted behind the implementation).
        error_codes=[400, 404, 409],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/send",
        "DELETE",
        "Cancel a queued message",
        description=(
            "Removes a previously-queued message from the workstream's "
            "pending queue. Returns ``status: removed`` when the queue "
            "had the entry, ``status: not_found`` otherwise."
        ),
        request_model=DequeueRequest,
        response_model=StatusResponse,
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/approve",
        "POST",
        "Approve or deny a tool call",
        request_model=ApproveRequest,
        response_model=ApproveResponse,
        error_codes=[400, 404, 409],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/command",
        "POST",
        "Execute a slash command",
        request_model=CommandRequest,
        response_model=StatusResponse,
        # 409 = worker slot busy (deliberate loud refusal); 503 = the
        # command worker could not be started (thread exhaustion — retry
        # shortly; the command did NOT run).
        error_codes=[400, 404, 409, 503],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/cancel",
        "POST",
        "Cancel the active generation in a workstream",
        request_model=CancelRequest,
        request_required=False,
        response_model=CancelResponse,
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/rewind",
        "POST",
        "Drop the last N conversation turns (emits clear_ui)",
        description=(
            "Claims the workstream mutation slot, durably truncates the requested "
            "tail, then emits clear_ui. Concurrent sends are ordered after the "
            "cut; a storage failure returns 503 without changing live history."
        ),
        request_model=RewindRequest,
        response_model=StatusResponse,
        error_codes=[400, 404, 503],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/retry",
        "POST",
        "Drop the last response and re-send the last user message",
        description=(
            "Uses one workstream worker claim for the durable truncation and the "
            "replacement generation, so another send cannot enter between them. "
            "A storage failure returns 503 without changing live history."
        ),
        response_model=StatusResponse,
        error_codes=[400, 404, 503],
        tags=["Chat"],
    ),
    # --- Streaming ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/events",
        "GET",
        "Per-workstream SSE event stream",
        description="Opens a Server-Sent Events stream scoped to a single workstream. "
        "After rendering REST history, pass its opaque handoff_token once as "
        "?history_token=; it names the exact accepted conversation-row prefix used "
        "for that render. A history_resync event closes this stream and requires a "
        "fresh history read; numeric event replay is not a substitute. Native "
        "Last-Event-ID reconnects take priority. Pass ?user_turn=1 to opt into "
        "typed accepted-user events; otherwise those rows become a backward-compatible "
        "strong-repair frame. Pass ?tool_turn=1 to receive the final accepted tool row "
        "as a typed tool_result with accepted=true; without it, accepted tool rows use "
        "the same pre-row strong-repair projection. Returns "
        "text/event-stream. See API reference for event types.",
        query_params=[
            QueryParam(
                "last_event_id",
                "Numeric per-workstream event cursor for manual reconnects.",
                schema_type="integer",
            ),
            QueryParam(
                "history_token",
                "Opaque one-shot token naming the accepted prefix rendered from REST history.",
            ),
            QueryParam(
                "user_turn",
                "Set to 1 to receive typed user_turn events instead of history-repair frames.",
                schema_type="integer",
            ),
            QueryParam(
                "tool_turn",
                "Set to 1 to receive final accepted tool_result projections.",
                schema_type="integer",
            ),
        ],
        error_codes=[404],
        tags=["Streaming"],
    ),
    EndpointSpec(
        "/v1/api/events/global",
        "GET",
        "Global SSE event stream",
        description="Server-Sent Events stream for node-level state broadcasts. "
        "Emits a node_snapshot event on connect (workstreams, health, aggregate), "
        "followed by real-time delta events (ws_state, ws_activity, ws_created, "
        "ws_closed, ws_rename, health_changed, aggregate). "
        "Pass ?expected_node_id=X for identity verification (returns 409 on mismatch). "
        "Every event's SSE id is an opaque '{boot_epoch}-{counter}' string; presenting "
        "it on reconnect (Last-Event-ID header or ?last_event_id=) replays missed "
        "events, or emits a replay_truncated event (reason: ring_evicted with "
        "lost_count + earliest_available_id, or boot_epoch when the cursor predates "
        "this server process) followed by a fresh node_snapshot. Treat the id as "
        "opaque — its format may change.",
        tags=["Streaming"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/delete",
        "POST",
        "Permanently delete a saved workstream",
        error_codes=[400, 404, 500],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/open",
        "POST",
        "Load a saved workstream into memory",
        error_codes=[400, 404, 500],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/title",
        "POST",
        "Set workstream title manually",
        error_codes=[400, 409],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/refresh-title",
        "POST",
        "Regenerate workstream title via LLM",
        error_codes=[404],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}",
        "GET",
        "Get workstream detail (rehydrates lazily on miss)",
        description=(
            "Returns the persisted workstream's display fields. If the "
            "session isn't currently in memory the manager rehydrates it "
            "before responding; ``500`` on rehydrate failure carries a "
            "correlation id matching the server log line. Lifted from "
            "the coord-only surface in the Stage 2 history/detail verb "
            "lift — interactive previously had no detail endpoint."
        ),
        response_model=WorkstreamDetailResponse,
        error_codes=[400, 404, 500, 503],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/history",
        "GET",
        "Read the workstream's reconstructed message history",
        description=(
            "Returns the tail of the conversation in OpenAI-like message "
            "format. Persisted-but-not-loaded workstreams (closed / "
            "evicted) are rehydrated before history is served so every "
            "successful response participates in the REST-to-SSE handoff. Lifted from "
            "the coord-only surface in the Stage 2 history/detail verb "
            "lift — interactive previously only exposed history through "
            "the SSE replay on ``/events``. Messages are the "
            "requested limit-bounded tail of one authoritative total accepted "
            "conversation-row prefix: "
            "user, assistant, tool, and system rows, including projected compaction "
            "checkpoints and cancellation markers. The opaque handoff_token names "
            "the exact prefix used for the render and is passed once on initial SSE "
            "registration. Admission of a later row changes the token; durable "
            "acknowledgement does not. If the durable prefix cannot be loaded, the "
            "endpoint returns 503 with `History temporarily unavailable`; that "
            "response is not authoritative and supplies no usable handoff token."
        ),
        response_model=WorkstreamHistoryResponse,
        query_params=[
            QueryParam(
                "limit",
                "Max conversation rows to fetch from storage (default 100, max 500).",
                schema_type="integer",
                default=100,
            ),
        ],
        error_codes=[400, 404, 500, 503],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/export",
        "GET",
        "Export the workstream's conversation as OpenAI messages JSON",
        description=(
            'Returns the full conversation as an ``{"messages": [...]}`` '
            "OpenAI Chat Completions envelope, served as a ``<ws_id>.json`` "
            "file download (``Content-Disposition: attachment``). Persisted "
            "reasoning is surfaced on assistant messages as a "
            "``reasoning_content`` field. Conversation-only — the parent + "
            "per-child zip bundle is exposed only through the "
            "``turnstone-admin export --children`` CLI."
        ),
        error_codes=[400, 404, 500, 503],
        tags=["Workstreams"],
    ),
    # --- Workstream attachments ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments",
        "POST",
        "Upload a file (multipart/form-data, field 'file') and attach it "
        "to the caller's next user turn on this workstream.  Validates "
        "size, MIME, and UTF-8 for text; magic-byte sniff for images.  "
        "Ownership failures are masked as 404 so non-owners cannot "
        "enumerate workstream existence; a 403 indicates a scope/auth "
        "failure from the middleware layer.",
        response_model=UploadAttachmentResponse,
        error_codes=[400, 403, 404, 409, 413],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments",
        "GET",
        "List the caller's pending (unconsumed) attachments for this "
        "workstream.  Ownership failures are masked as 404.",
        response_model=ListAttachmentsResponse,
        error_codes=[403, 404],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}/content",
        "GET",
        "Return raw bytes of an attachment with its stored Content-Type.  "
        "Ownership failures are masked as 404.",
        error_codes=[403, 404],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}",
        "DELETE",
        "Remove a pending attachment (consumed attachments return 404).  "
        "Ownership failures are also masked as 404.",
        error_codes=[403, 404],
        tags=["Attachments"],
    ),
    # --- Voice I/O ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/speech-to-text",
        "POST",
        "Transcribe a short audio clip (multipart/form-data, field 'audio') "
        "using the configured STT model role.  Returns the transcript for the "
        "client to place into the composer; this endpoint never sends on the "
        "user's behalf.  Returns 503 when no STT role is configured.",
        response_model=SpeechToTextResponse,
        error_codes=[400, 403, 404, 413, 502, 503],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/tts",
        "POST",
        "Synthesize text to speech audio for browser playback using the "
        "configured TTS model role.  Returns audio bytes; 503 when no TTS "
        "role is configured.",
        request_model=TextToSpeechRequest,
        error_codes=[400, 502, 503],
        tags=["Chat"],
    ),
    # --- Saved workstreams ---
    EndpointSpec(
        "/v1/api/workstreams/saved",
        "GET",
        "List saved workstreams",
        response_model=ListSavedWorkstreamsResponse,
        tags=["Workstreams"],
    ),
    # --- Skills ---
    EndpointSpec(
        "/v1/api/skills",
        "GET",
        "List available skills (summary)",
        response_model=ListSkillSummaryResponse,
        tags=["Skills"],
    ),
    # --- Personas ---
    EndpointSpec(
        "/v1/api/personas",
        "GET",
        "List enabled personas for the workstream-creation picker",
        response_model=ListPersonaChoicesResponse,
        tags=["Personas"],
    ),
    # --- Models ---
    EndpointSpec(
        "/v1/api/models",
        "GET",
        "List available model aliases",
        response_model=ListAvailableModelsResponse,
        tags=["Models"],
    ),
    # --- Auth ---
    EndpointSpec(
        "/v1/api/auth/login",
        "POST",
        "Authenticate with a token",
        request_model=AuthLoginRequest,
        response_model=AuthLoginResponse,
        error_codes=[401],
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/setup",
        "POST",
        "Create first admin user",
        request_model=AuthSetupRequest,
        response_model=AuthSetupResponse,
        error_codes=[400, 409, 503],
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/status",
        "GET",
        "Return auth state",
        response_model=AuthStatusResponse,
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/logout",
        "POST",
        "Clear auth cookie",
        response_model=StatusResponse,
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/oidc/authorize",
        "GET",
        "Redirect to OIDC provider for SSO login",
        response_code=302,
        error_codes=[404, 503],
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/oidc/callback",
        "GET",
        "OIDC callback — validates code, provisions user, sets JWT cookie, redirects to app",
        response_code=302,
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/whoami",
        "GET",
        "Return authenticated user info and permissions",
        response_model=AuthWhoamiResponse,
        error_codes=[401],
        tags=["Auth"],
    ),
    # --- Memories ---
    EndpointSpec(
        "/v1/api/memories",
        "GET",
        "List structured memories. Without a scope, returns global plus the authenticated user's memories; workstream scope is owner-bound.",
        response_model=ListMemoriesResponse,
        query_params=[
            QueryParam("type", "Filter by memory type"),
            QueryParam("scope", "Filter by public scope: global, workstream, or user"),
            QueryParam("scope_id", "Filter by scope identifier"),
            QueryParam(
                "limit", "Max results (default 100, max 200)", schema_type="integer", default=100
            ),
        ],
        error_codes=[400, 403, 404, 500],
        tags=["Memories"],
    ),
    EndpointSpec(
        "/v1/api/memories",
        "POST",
        "Save (upsert) a structured memory",
        request_model=SaveMemoryRequest,
        response_model=MemorySummary,
        error_codes=[400, 403, 404, 500],
        tags=["Memories"],
    ),
    EndpointSpec(
        "/v1/api/memories/search",
        "POST",
        "Search structured memories by query. Without a scope, searches global plus the authenticated user's memories.",
        request_model=SearchMemoriesRequest,
        response_model=ListMemoriesResponse,
        error_codes=[400, 403, 404, 500],
        tags=["Memories"],
    ),
    EndpointSpec(
        "/v1/api/memories/{name}",
        "GET",
        "Fetch a structured memory body by exact name and scope",
        response_model=MemoryInfo,
        path_params=[
            PathParam(
                "name",
                MEMORY_NAME_INPUT_DESCRIPTION,
            )
        ],
        query_params=[
            QueryParam("scope", "Scope (default: global)"),
            QueryParam("scope_id", "Scope identifier"),
        ],
        error_codes=[400, 403, 404, 500],
        tags=["Memories"],
    ),
    EndpointSpec(
        "/v1/api/memories/{name}",
        "DELETE",
        "Delete a structured memory by name and scope",
        response_model=StatusResponse,
        path_params=[
            PathParam(
                "name",
                MEMORY_NAME_INPUT_DESCRIPTION,
            )
        ],
        query_params=[
            QueryParam("scope", "Scope (default: global)"),
            QueryParam("scope_id", "Scope identifier"),
        ],
        error_codes=[400, 403, 404, 500],
        tags=["Memories"],
    ),
    # --- Admin settings ---
    EndpointSpec(
        "/v1/api/admin/settings",
        "GET",
        "List interface.* settings with values and sources",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/settings/{key}",
        "PUT",
        "Update an interface.* setting",
        error_codes=[400, 503],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/settings/{key}",
        "POST",
        "Update an interface.* setting (alias for PUT)",
        error_codes=[400, 503],
        tags=["Admin"],
    ),
    # --- Observability ---
    EndpointSpec(
        "/health",
        "GET",
        "Server health check",
        response_model=HealthResponse,
        tags=["Observability"],
    ),
]

_ALL_MODELS: list[type[BaseModel]] = [
    ErrorResponse,
    StatusResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    SendRequest,
    SendResponse,
    DequeueRequest,
    ApproveRequest,
    ApproveResponse,
    CommandRequest,
    CancelRequest,
    CancelResponse,
    RewindRequest,
    CreateWorkstreamRequest,
    CreateWorkstreamResponse,
    CloseWorkstreamRequest,
    ListWorkstreamsResponse,
    WorkstreamDetailResponse,
    WorkstreamHistoryResponse,
    DashboardResponse,
    ListSavedWorkstreamsResponse,
    UploadAttachmentResponse,
    ListAttachmentsResponse,
    SpeechToTextResponse,
    TextToSpeechRequest,
    HealthResponse,
    SaveMemoryRequest,
    MemoryInfo,
    ListMemoriesResponse,
    SearchMemoriesRequest,
    SkillSummary,
    ListSkillSummaryResponse,
    PersonaChoice,
    ListPersonaChoicesResponse,
    AvailableModelInfo,
    ListAvailableModelsResponse,
]


def build_server_spec() -> dict[str, Any]:
    """Build the OpenAPI spec for the turnstone server."""
    return build_openapi(
        title="turnstone Server API",
        description="Single-node workstream management, chat interaction, and real-time streaming.",
        endpoints=SERVER_ENDPOINTS,
        models=_ALL_MODELS,
    )
