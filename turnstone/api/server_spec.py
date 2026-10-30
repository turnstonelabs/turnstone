"""Server endpoint catalog for OpenAPI spec generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.api.openapi import EndpointSpec, PathParam, QueryParam, build_openapi

if TYPE_CHECKING:
    from pydantic import BaseModel
from turnstone.api.schemas import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthRefreshResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    AuthWhoamiResponse,
    ErrorResponse,
    SetUserAvatarColorRequest,
    StatusResponse,
    UserAvatarResponse,
    WorkstreamDeleteResponse,
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
    ListFilesystemResponse,
    ListMemoriesResponse,
    ListPersonaChoicesResponse,
    ListSavedWorkstreamsResponse,
    ListSkillSummaryResponse,
    ListWorkstreamsResponse,
    MemoryInfo,
    MemorySummary,
    PersonaChoice,
    ResolveFilesystemResponse,
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
from turnstone.core.workstream import (
    SAVED_PAGE_DEFAULT_LIMIT,
    SAVED_PAGE_MAX_LIMIT,
    SAVED_SEARCH_MAX_CHARS,
    SAVED_WORKSTREAM_SORT_KEYS,
)

#: Query parameters of ``GET /v1/api/workstreams/saved``, shared with the
#: console spec.
SAVED_WORKSTREAMS_QUERY_PARAMS: list[QueryParam] = [
    QueryParam(
        "limit",
        f"Rows per page (default {SAVED_PAGE_DEFAULT_LIMIT}, at most {SAVED_PAGE_MAX_LIMIT}; "
        "larger values are clamped, and 0 returns only the total).",
        schema_type="integer",
        default=SAVED_PAGE_DEFAULT_LIMIT,
    ),
    QueryParam(
        "offset",
        "Matching rows to skip before the page starts.",
        schema_type="integer",
        default=0,
    ),
    QueryParam(
        "q",
        "Case-insensitive substring of the alias, title, name, project name or ws_id "
        f"(at most {SAVED_SEARCH_MAX_CHARS} characters).",
    ),
    QueryParam(
        "sort",
        "Column to sort by; ws_id breaks ties so pages never overlap.",
        default="updated",
        enum=sorted(SAVED_WORKSTREAM_SORT_KEYS),
    ),
    QueryParam("order", "Sort direction.", default="desc", enum=["asc", "desc"]),
]

#: User avatar endpoints.  Both processes mount the same handlers
#: (``turnstone.core.avatar_routes``), so the console spec shares this list.
USER_AVATAR_ENDPOINTS: list[EndpointSpec] = [
    EndpointSpec(
        "/v1/api/users/{user_id}/avatar",
        "GET",
        "Return a user's uploaded avatar picture as a 256px PNG",
        description=(
            "Any signed-in caller may fetch any user's picture.  With ``v`` equal to the "
            "picture version from the user's avatar descriptor the response is cacheable for "
            "a year; otherwise it revalidates against the ETag."
        ),
        query_params=[QueryParam("v", "Picture version from the avatar descriptor")],
        error_codes=[404],
        tags=["Users"],
    ),
    EndpointSpec(
        "/v1/api/users/{user_id}/avatar",
        "PUT",
        "Set your avatar's colour",
        description="Your own account only, with write scope.",
        request_model=SetUserAvatarColorRequest,
        response_model=UserAvatarResponse,
        error_codes=[400, 403, 404],
        tags=["Users"],
    ),
    EndpointSpec(
        "/v1/api/users/{user_id}/avatar/picture",
        "PUT",
        "Upload your avatar picture",
        description=(
            "Multipart form with a ``file`` field: PNG, JPEG or WebP, up to 4 MB, and up to "
            "4096 by 4096 pixels for JPEG or 2048 by 2048 for PNG and WebP.  A JPEG saved in "
            "more than 100 progressive passes is refused.  Pictures are processed one at a "
            "time per server, in arrival order, and one per person; a 503 with Retry-After "
            "means your last picture or too many others are still being processed, so try "
            "again.  Stored as a 256px square PNG (centre-cropped, EXIF orientation applied, "
            "no metadata kept, a wide-gamut colour profile read as sRGB).  Your own account "
            "only, with write scope."
        ),
        response_model=UserAvatarResponse,
        error_codes=[400, 403, 404, 413, 503],
        tags=["Users"],
    ),
    EndpointSpec(
        "/v1/api/users/{user_id}/avatar/picture",
        "DELETE",
        "Remove your avatar picture; your initial and colour show again",
        description="Your own account only, with write scope.",
        response_model=UserAvatarResponse,
        error_codes=[403, 404],
        tags=["Users"],
    ),
]

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
            "is retained. Returns 503 when cancellation cleanup has not yet "
            "finished; retrying close is safe."
        ),
        request_model=CloseWorkstreamRequest,
        response_model=StatusResponse,
        error_codes=[400, 404, 409, 503],
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
        error_codes=[400, 404, 409, 503],
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
        error_codes=[400, 404, 503],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/approve",
        "POST",
        "Approve or deny a tool call",
        request_model=ApproveRequest,
        response_model=ApproveResponse,
        error_codes=[400, 404, 409, 503],
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
        error_codes=[400, 404, 503],
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
        error_codes=[404, 503],
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
        response_model=WorkstreamDeleteResponse,
        error_codes=[400, 404, 409, 500],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/open",
        "POST",
        "Load a saved workstream into memory",
        description=(
            "``409`` with code ``workstream_lease_held`` names the node that has "
            "the workstream open. ``429`` when every session slot on the node is "
            "busy (retry later). ``503`` when its history or saved settings could "
            "not be read (retry shortly) or the session factory is misconfigured."
        ),
        error_codes=[400, 404, 409, 429, 500, 503],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/title",
        "POST",
        "Set workstream title manually",
        error_codes=[400, 409, 503],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/refresh-title",
        "POST",
        "Regenerate workstream title via LLM",
        error_codes=[404, 503],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}",
        "GET",
        "Get workstream detail (rehydrates lazily on miss)",
        description=(
            "Returns the persisted workstream's display fields. If the "
            "session isn't currently in memory, write scope is additionally "
            "required before the manager rehydrates it "
            "before responding; ``429`` when every session slot is busy "
            "(retry later); ``500`` on rehydrate failure carries a "
            "correlation id matching the server log line. Lifted from "
            "the coord-only surface in the Stage 2 history/detail verb "
            "lift — interactive previously had no detail endpoint."
        ),
        response_model=WorkstreamDetailResponse,
        error_codes=[400, 403, 404, 409, 429, 500, 503],
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
    # --- Node workspace browsing ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/files",
        "GET",
        "Browse an interactive node's shared filesystem root",
        description=(
            "Read-only access to workspace, /data, or /tmp. Workspace uses configured "
            "tools.workspace_dir / TURNSTONE_WORKSPACE, falling back to the node process "
            "working directory when unset. Requires files.read permission, write "
            "scope and an active local interactive workstream. Workstream "
            "project-access checks apply; the directory is node-shared, not per-workstream. "
            "Symlinks and special files are excluded. At most 1000 directory entries are "
            "examined, with truncation reported explicitly."
        ),
        response_model=ListFilesystemResponse,
        query_params=[
            QueryParam("path", "Directory path relative to the selected root", default=""),
            QueryParam(
                "root", "Filesystem root", default="workspace", enum=["workspace", "data", "tmp"]
            ),
        ],
        error_codes=[400, 403, 404, 409, 503],
        tags=["Files"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/files/resolve",
        "GET",
        "Resolve a tool file path to its browsable parent directory",
        description=(
            "Requires files.read permission, write scope, and an accessible active local "
            "interactive workstream. Accepts absolute paths, home-relative paths, and paths "
            "relative to the node working directory, selecting the most specific matching "
            "workspace, /data, or /tmp root. "
            "Returns the parent directory listing with the selected file included even when "
            "the listing is truncated. Paths outside these roots, symlinks below a root, "
            "and special files are refused. Reads the current filesystem, not a snapshot."
        ),
        response_model=ResolveFilesystemResponse,
        query_params=[
            QueryParam(
                "path",
                "Tool file path, absolute, home-relative, or relative to the node working directory",
                required=True,
            ),
        ],
        error_codes=[400, 403, 404, 409, 503],
        tags=["Files"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/files/preview",
        "GET",
        "Render a workspace file with the preview content security policy",
        description=(
            "Requires files.read permission, write scope, and an accessible active local "
            "interactive workstream. Uses the existing per-kind preview size limits. "
            "Symlinks are refused."
        ),
        query_params=[
            QueryParam("path", "File path relative to the selected root"),
            QueryParam(
                "root", "Filesystem root", default="workspace", enum=["workspace", "data", "tmp"]
            ),
            QueryParam("probe", "Set to 1 for a bodyless 204 availability check"),
            QueryParam("assets", "Set to 1 to permit remote HTML images and styles"),
        ],
        error_codes=[400, 403, 404, 409, 413, 415, 503],
        tags=["Files"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/files/content",
        "GET",
        "Download original workspace file bytes",
        description=(
            "Requires files.read permission, write scope, and an accessible active local "
            "interactive workstream. Returns an attachment download, capped at 64 MiB. "
            "Symlinks are refused."
        ),
        query_params=[
            QueryParam("path", "File path relative to the selected root"),
            QueryParam(
                "root", "Filesystem root", default="workspace", enum=["workspace", "data", "tmp"]
            ),
        ],
        error_codes=[400, 403, 404, 409, 413, 503],
        tags=["Files"],
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
        error_codes=[400, 403, 404, 409, 413, 503],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments",
        "GET",
        "List the caller's pending (unconsumed) attachments for this "
        "workstream.  Ownership failures are masked as 404.",
        response_model=ListAttachmentsResponse,
        error_codes=[403, 404, 503],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}/content",
        "GET",
        "Return raw bytes of an attachment with its stored Content-Type.  "
        "Ownership failures are masked as 404.",
        error_codes=[403, 404, 503],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}",
        "DELETE",
        "Remove a pending attachment (consumed attachments return 404).  "
        "Ownership failures are also masked as 404.",
        error_codes=[403, 404, 503],
        tags=["Attachments"],
    ),
    # --- Voice I/O ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/speech-to-text",
        "POST",
        "Transcribe a short audio clip (multipart/form-data, field 'audio') "
        "using the configured STT model role.  Returns the transcript for the "
        "client to place into the composer; this endpoint never sends on the "
        "user's behalf.  Returns 429 with Retry-After when the node has no free "
        "dictation thread, or the STT model no free max_concurrency slot, within "
        "15 seconds; 503 when no STT role is configured.",
        response_model=SpeechToTextResponse,
        error_codes=[400, 403, 404, 413, 429, 502, 503],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/tts",
        "POST",
        "Synthesize text to speech audio for browser playback using the "
        "configured TTS model role.  Returns audio bytes; 429 with Retry-After "
        "when the node is already synthesizing as many clips as it runs at once, "
        "or the TTS model has no free max_concurrency slot; 503 when no TTS role "
        "is configured.",
        request_model=TextToSpeechRequest,
        error_codes=[400, 429, 502, 503],
        tags=["Chat"],
    ),
    # --- Saved workstreams ---
    EndpointSpec(
        "/v1/api/workstreams/saved",
        "GET",
        "List saved workstreams",
        description=(
            "One page of interactive workstreams with history that no process has "
            "loaded, visible through creator/project access; requires read scope. "
            "total counts every matching row across pages. A malformed parameter "
            "returns 400."
        ),
        response_model=ListSavedWorkstreamsResponse,
        query_params=SAVED_WORKSTREAMS_QUERY_PARAMS,
        error_codes=[400, 503],
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
        "Authenticate with a password or raw stored API token",
        request_model=AuthLoginRequest,
        response_model=AuthLoginResponse,
        error_codes=[400, 401, 403, 429, 503],
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
        "/v1/api/auth/refresh",
        "POST",
        "Renew a password/OIDC session from current role permissions",
        response_model=AuthRefreshResponse,
        error_codes=[401, 403, 503],
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
    *USER_AVATAR_ENDPOINTS,
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
    AuthRefreshResponse,
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
    ListFilesystemResponse,
    ResolveFilesystemResponse,
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
