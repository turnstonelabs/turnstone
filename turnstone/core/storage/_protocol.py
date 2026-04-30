"""Storage backend protocol — the contract every persistence adapter must implement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from turnstone.core.workstream import WorkstreamKind


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that every storage backend adapter must implement.

    Provides workstream management, conversation persistence, structured
    memories, and full-text search.

    Cross-cutting contracts:

    **Tenancy filter on aggregates.**  Every list / count / aggregate
    method that can span rows from more than one ``user_id`` MUST
    accept ``user_id: str | None = None`` as a keyword-only argument
    and push ``WHERE user_id = :user_id`` into SQL when a uid is
    supplied.  ``None`` is reserved for service-scoped callers that
    legitimately need cluster-wide visibility.  Calling endpoints
    MUST resolve the effective filter (typically via
    ``_effective_user_filter`` in ``turnstone.console.server``) and
    pass it through — never post-filter in Python; handler-side
    filtering lets orphan rows, forged ``parent_ws_id`` references,
    and empty-sub tokens leak cross-tenant counts.

    **Row access via ``_mapping``.**  List-style methods return
    SQLAlchemy ``Row`` objects; callers MUST access columns through
    ``row._mapping[<col>]`` (or ``.get("<col>")`` on the mapping).
    Positional indexing is not a supported access pattern — a SELECT
    reorder or a new trailing column silently corrupts the
    projection.  Test doubles for list-style storage methods MUST
    expose a ``_mapping`` attribute matching the production ``Row``
    shape; ``turnstone.testing.row_contract.assert_row_like`` is the
    canonical check for fixtures and fakes.
    """

    # -- Core conversation operations ------------------------------------------

    def save_message(
        self,
        ws_id: str,
        role: str,
        content: str | None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        provider_data: str | None = None,
        tool_calls: str | None = None,
    ) -> int:
        """Log a message to the conversations table.

        Returns the inserted row's ``id`` (autoincrement PK).  Callers
        that need to link side tables (e.g. ``workstream_attachments``)
        use this to associate the row after save.
        """
        ...

    def save_messages_bulk(self, rows: list[dict[str, Any]]) -> None:
        """Insert multiple conversation rows in a single transaction.

        Each dict must include ``ws_id``, ``role``, and ``content``
        (which may be ``None`` for assistant messages with only tool_calls).
        Optional keys: ``tool_name``, ``tool_call_id``, ``provider_data``,
        ``tool_calls``.  Timestamp and workstream
        updated-at are handled internally.
        """
        ...

    def load_messages(self, ws_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Load messages for a workstream and reconstruct OpenAI message format.

        ``limit`` caps the number of underlying conversation rows fetched
        (from the tail, then reversed), bounding memory for callers that
        only need a recent slice — e.g. the cluster-inspect endpoint.  The
        returned message list may have slightly fewer *reconstructed*
        entries than ``limit`` when a tool-call group splits across the
        boundary; callers that need strict tail-N semantics must slice
        again client-side.  Default ``None`` fetches the full history.
        """
        ...

    # -- Workstream attachments -----------------------------------------------

    def save_attachment(
        self,
        attachment_id: str,
        ws_id: str,
        user_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        kind: str,
        content: bytes,
    ) -> None:
        """Persist an uploaded attachment in pending (unconsumed) state."""
        ...

    def list_pending_attachments(self, ws_id: str, user_id: str) -> list[dict[str, Any]]:
        """Return un-consumed attachments for ``(ws_id, user_id)``.

        Each dict contains: ``attachment_id``, ``filename``, ``mime_type``,
        ``size_bytes``, ``kind``, ``created``.  Content bytes are NOT returned.
        """
        ...

    def get_attachments(self, attachment_ids: list[str]) -> list[dict[str, Any]]:
        """Bulk fetch attachments by id, including their ``content`` bytes.

        Unknown ids are silently skipped.  Order is unspecified.
        """
        ...

    def get_pending_attachments_with_content(
        self, ws_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Fetch all pending attachments for ``(ws_id, user_id)`` in a single
        query, including ``content`` bytes.

        Used by the auto-consume path on send — saves the two-roundtrip
        list-then-get dance.  Excluded by design from the user-facing
        listing API (which must never expose bytes).
        """
        ...

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        """Return a single attachment row (with content bytes) or None."""
        ...

    def delete_attachment(self, attachment_id: str, ws_id: str, user_id: str) -> bool:
        """Delete a pending attachment.

        Only succeeds when the row matches ``ws_id``, ``user_id``, AND
        ``message_id IS NULL`` (i.e. not yet consumed).  Returns True if
        a row was deleted.
        """
        ...

    def mark_attachments_consumed(
        self,
        attachment_ids: list[str],
        message_id: int,
        ws_id: str,
        user_id: str,
        reserved_for_msg_id: str | None = None,
    ) -> None:
        """Link a set of attachments to a freshly-saved user message.

        The UPDATE is scoped to ``(ws_id, user_id)`` and
        ``message_id IS NULL`` as defense-in-depth: even if a caller
        passes attachment ids that don't belong to them, nothing will be
        consumed.  When ``reserved_for_msg_id`` is set, also requires
        the reservation to match — prevents a stale send from consuming
        rows reserved to a different one.  Clears ``reserved_for_msg_id``
        on transition.
        """
        ...

    def reserve_attachments(
        self,
        attachment_ids: list[str],
        queue_msg_id: str,
        ws_id: str,
        user_id: str,
    ) -> list[str]:
        """Soft-lock pending attachments to a queued user message.

        Only rows where ``(ws_id, user_id)`` match and both
        ``message_id`` and ``reserved_for_msg_id`` are NULL are updated.
        Returns the list of ids that were actually reserved (others
        silently skipped — caller should not assume completeness).
        """
        ...

    def unreserve_attachments(self, queue_msg_id: str, ws_id: str, user_id: str) -> None:
        """Release any reservation for ``queue_msg_id``.

        Used when a queued message is dequeued (cancelled) before
        dispatch — the attachments return to ``pending``.
        """
        ...

    def sweep_orphan_reservations(self, older_than_seconds: int) -> int:
        """Clear ``reserved_for_msg_id`` on stale reservations.

        Targets rows with ``reserved_for_msg_id IS NOT NULL`` AND
        ``message_id IS NULL`` AND ``reserved_at`` older than the cutoff.
        Self-heals reservations leaked by process crashes between
        ``reserve_attachments`` and ``mark_attachments_consumed`` /
        ``unreserve_attachments``.

        Uses ``reserved_at`` (set on reserve, cleared on consume /
        unreserve) rather than ``created`` (upload time) so an attachment
        that sat pending for hours before being reserved is not
        mistakenly unreserved mid-send.  Returns the row count swept.
        """
        ...

    def load_attachments_for_messages(
        self,
        ws_id: str,
        *,
        message_ids: list[int] | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        """Return attachments grouped by ``message_id`` for history replay.

        Each attachment dict includes ``attachment_id``, ``filename``,
        ``mime_type``, ``size_bytes``, ``kind``, and ``content`` (bytes).
        Pending (un-consumed) rows are excluded.

        ``message_ids`` narrows the scan to attachments tied to the
        given message rows — used by the tail-N path in
        :func:`load_messages` so the attachment read doesn't defeat
        the conversations-table LIMIT.  Default ``None`` returns every
        attachment for the workstream.
        """
        ...

    def delete_messages_after(self, ws_id: str, keep_count: int) -> int:
        """Delete conversation rows beyond the first *keep_count* rows for a workstream.

        Rows are ordered by auto-increment ``id``.  If the workstream has
        N rows total and ``keep_count`` < N, the last N - keep_count rows
        are deleted.  Returns the number of rows deleted.
        """
        ...

    # -- Workstream management -------------------------------------------------

    def list_workstreams_with_history(
        self,
        limit: int = 20,
        *,
        kind: WorkstreamKind | str | None = None,
        user_id: str | None = None,
        state: str | None = None,
    ) -> list[Any]:
        """List workstreams that have messages, ordered by updated DESC.

        ``kind`` filters at the SQL layer — pass ``WorkstreamKind.INTERACTIVE``
        from the interactive "saved workstreams" sidebar so coordinator rows
        (which also persist conversation history) don't leak into that
        surface.  Default ``None`` preserves the legacy all-kinds behaviour.

        ``user_id`` pushes ``WHERE user_id = :user_id`` into SQL so tenant
        scoping is enforced server-side rather than relying on handlers to
        remember a client-side filter.  Pass the authenticated caller's
        uid from any tenant-visible endpoint; pass ``None`` for
        service-scoped callers that legitimately need cluster-wide
        visibility.  Mirrors the same contract on ``list_workstreams``.

        ``state`` filters by lifecycle state — pass ``"closed"`` from the
        coordinator "saved" surface so the list excludes deleted /
        currently-active rows.  Default ``None`` preserves all-states
        behaviour.  Accepts a string (rather than the WorkstreamState
        enum) to match the on-disk column type.
        """
        ...

    def prune_workstreams(self, retention_days: int = 90) -> tuple[int, int]:
        """Remove orphaned + stale unnamed workstreams. Returns (orphans, stale)."""
        ...

    def resolve_workstream(self, alias_or_id: str) -> str | None:
        """Resolve an alias or ws_id (or prefix) to a full ws_id."""
        ...

    # -- Workstream config -----------------------------------------------------

    def save_workstream_config(self, ws_id: str, config: dict[str, str]) -> None:
        """Persist workstream configuration key/value pairs."""
        ...

    def load_workstream_config(self, ws_id: str) -> dict[str, str]:
        """Load workstream configuration. Returns empty dict if none stored."""
        ...

    # -- Workstream metadata ---------------------------------------------------

    def set_workstream_alias(self, ws_id: str, alias: str) -> bool:
        """Set a human-friendly alias. Returns False if alias is taken."""
        ...

    def get_workstream_display_name(self, ws_id: str) -> str | None:
        """Return the alias (or title) for a workstream, or None if unset."""
        ...

    def get_workstream_display_names(self, ws_ids: list[str]) -> dict[str, str | None]:
        """Bulk variant of :meth:`get_workstream_display_name`.

        Returns a dict keyed on every requested ws_id. Missing rows
        map to ``None``; the caller falls back to ``ws.name`` per-row.
        Used by the lifted ``list`` verb to avoid the per-row
        N+1 storage round-trip pre-lift had.
        """
        ...

    def get_workstream_metadata(self, ws_id: str) -> dict[str, Any] | None:
        """Return workstream metadata dict or None if not found."""
        ...

    def get_workstream(self, ws_id: str) -> dict[str, Any] | None:
        """Return the full ``workstreams`` row as a dict, or ``None``.

        Richer than :meth:`get_workstream_metadata` — includes ``state``,
        ``user_id``, ``kind``, ``parent_ws_id``, and timestamps.  Used by
        coordinator ``inspect_workstream`` and any caller that needs the
        authoritative row.
        """
        ...

    def get_workstream_owner(self, ws_id: str) -> str | None:
        """Return the workstream's owner ``user_id``.

        Returns ``None`` when the workstream doesn't exist, ``""`` when
        it exists but has no owner recorded.  Used by ownership-gating
        endpoints (attachments).
        """
        ...

    def update_workstream_title(self, ws_id: str, title: str) -> None:
        """Set or update the auto-generated title for a workstream."""
        ...

    # -- Structured memories ---------------------------------------------------

    def create_structured_memory(
        self,
        memory_id: str,
        name: str,
        description: str,
        mem_type: str,
        scope: str,
        scope_id: str,
        content: str,
    ) -> None:
        """Create a structured memory record."""
        ...

    def get_structured_memory(self, memory_id: str) -> dict[str, str] | None:
        """Return structured memory dict or None."""
        ...

    def get_structured_memory_by_name(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> dict[str, str] | None:
        """Lookup structured memory by (name, scope, scope_id). Returns dict or None."""
        ...

    def update_structured_memory(self, memory_id: str, **fields: str) -> bool:
        """Update specified fields on a structured memory. Returns True if found."""
        ...

    def delete_structured_memory(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> bool:
        """Delete a structured memory by (name, scope, scope_id). Returns True if existed."""
        ...

    def delete_structured_memory_by_id(self, memory_id: str) -> bool:
        """Delete a structured memory by its primary key. Returns True if existed."""
        ...

    def list_structured_memories(
        self,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """Return structured memories with optional filters, ordered by updated DESC."""
        ...

    def search_structured_memories(
        self,
        query: str,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Search structured memories by query. Returns matching memory dicts."""
        ...

    def touch_structured_memories(self, keys: list[tuple[str, str, str]]) -> int:
        """Batch-touch multiple memories.

        Each key is ``(name, scope, scope_id)``.  Callers should deduplicate
        before calling; each key increments ``access_count`` once per call.
        Returns count of rows found and updated.
        """
        ...

    def count_structured_memories(
        self, mem_type: str = "", scope: str = "", scope_id: str = ""
    ) -> int:
        """Count structured memories with optional type and scope filters."""
        ...

    # -- Workstream operations -------------------------------------------------

    def register_workstream(
        self,
        ws_id: str,
        node_id: str | None = None,
        name: str = "",
        state: str = "idle",
        user_id: str | None = None,
        alias: str | None = None,
        title: str | None = None,
        skill_id: str = "",
        skill_version: int = 0,
        kind: WorkstreamKind | str = "interactive",
        parent_ws_id: str | None = None,
    ) -> None:
        """Create a workstreams row (no-op if already exists).

        ``kind`` accepts a ``WorkstreamKind`` member or its raw string value
        (``"interactive"`` / ``"coordinator"``); the storage edge validates
        the value and rejects unknown kinds with ``ValueError``.
        ``parent_ws_id`` is non-NULL for children spawned by a coordinator;
        the storage edge normalizes the empty string to ``None``.
        """
        ...

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        """Update a workstream's state and bump updated timestamp."""
        ...

    def bulk_close_stale_orphans(
        self,
        kind: WorkstreamKind | str,
        cutoff: str,
        exclude_ws_ids: list[str],
        live_node_ids: list[str] | None = None,
    ) -> list[str]:
        """Close DB-side workstream rows of *kind* whose state is in
        ``BULK_CLOSE_STATE_VALUES`` and whose ``updated`` is lex-older than
        *cutoff*, excluding rows currently loaded in memory.  Sets
        ``state='closed'`` and bumps ``updated``.  Returns the list of ws_ids
        actually transitioned.

        ``cutoff`` is a UTC ``YYYY-MM-DDTHH:MM:SS`` string matching the on-disk
        format ``update_workstream_state`` writes — lex compare is safe for
        same-offset timestamps.  Empty ``exclude_ws_ids`` means no exclusion.

        ``live_node_ids`` is the set of ``services.service_id`` values whose
        ``last_heartbeat`` is recent (i.e. owning processes still alive);
        rows whose ``node_id`` matches one of these are protected because
        their owning process may legitimately have them loaded on another
        worker.  ``None`` skips the filter entirely (single-process / tests
        / operator backfill).  Empty list ``[]`` treats every node as dead —
        useful when operator scripts want to reap regardless of liveness.

        Rows with ``NULL`` ``node_id`` are always eligible: they have no
        meaningful owner identity, so age alone gates the reap.

        Liveness scoping replaces an earlier ``node_id == self`` heuristic.
        That heuristic broke in the post-rendezvous-routing world (PR #384):
        ``workstreams.node_id`` is stamped at create time and never updated,
        so dead-pod orphans in containerized deployments with dynamic
        hostnames couldn't be reclaimed.  ``services.last_heartbeat`` is the
        rendezvous router's authoritative liveness primitive — using it here
        keeps reap scoping aligned with routing.

        Asymmetric with ``SessionManager.close_idle``'s in-memory pass on
        purpose: that pass closes only ``IDLE`` (legitimately-attentive rows
        stay), this method closes the broader ``BULK_CLOSE_STATE_VALUES`` set
        because any row matching here is by definition not loaded by any
        live process and cannot be in a live interaction.
        """
        ...

    def touch_workstream(self, ws_id: str) -> None:
        """Bump a workstream row's ``updated`` timestamp without touching its
        state.

        Used by ``SessionManager.open()`` on cold rehydrate so a freshly-
        loaded row's ``updated`` can't be older than the orphan-reaper cutoff
        — protects against a same-process race where a parallel
        ``close_idle`` pass-2 snapshots loaded keys after the storage read
        but before the in-memory install.  Distinct from
        ``update_workstream_state(ws_id, current_state)`` because the
        rehydrate path explicitly avoids a state write (see the
        ``open()`` no-DB-state-flip-on-resurrect comment): a state write
        could race a concurrent ``close()`` and resurrect a closed row.
        Bumping only ``updated`` is safe — close still wins on the state
        column.
        """
        ...

    def update_workstream_name(self, ws_id: str, name: str) -> None:
        """Update a workstream's display name."""
        ...

    def delete_workstream(self, ws_id: str) -> bool:
        """Delete a workstream and all its conversations + config."""
        ...

    def list_workstreams(
        self,
        node_id: str | None = None,
        limit: int = 100,
        *,
        parent_ws_id: str | None = None,
        kind: WorkstreamKind | str | None = None,
        user_id: str | None = None,
    ) -> list[Any]:
        """List workstreams, optionally filtered.

        Filters are additive.  When ``parent_ws_id`` / ``kind`` / ``user_id``
        are ``None`` (default) they are not applied — behavior is identical
        to the pre-1.5 two-arg call shape.

        ``user_id`` pushes ``WHERE user_id = :user_id`` into SQL so tenant
        scoping is enforced server-side rather than relying on every
        handler to remember a client-side filter.  Pass the authenticated
        caller's uid unless the caller holds a service scope.

        Returns a list of SQLAlchemy ``Row`` objects.  **Prefer dict access
        via ``row._mapping[<col>]``**; positional indexing is brittle against
        future SELECT reorders and against new columns appearing in the
        tail (the select currently ends with ``user_id``).
        """
        ...

    def count_workstreams_by_state(
        self,
        *,
        parent_ws_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, int]:
        """Return ``{state: count}`` for workstreams matching the filters.

        Cheaper than ``list_workstreams`` when the caller only needs
        the histogram (e.g. per-coordinator metrics).  Filters are
        additive; empty kwargs mean cluster-wide (caller must gate on
        their own authz).
        """
        ...

    def count_workstreams_since(
        self,
        since: str,
        *,
        parent_ws_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        """Return the count of workstream rows whose ``created`` is >= ``since``.

        ``since`` is an ISO-8601 string matching the storage format
        (``YYYY-MM-DDTHH:MM:SS`` in UTC).  Lex compare is safe for the
        same-offset timestamps storage writes.
        """
        ...

    # -- Conversation search ---------------------------------------------------

    def search_history(self, query: str, limit: int = 20, offset: int = 0) -> list[Any]:
        """Search conversation history. Returns (timestamp, ws_id, role, content, tool_name)."""
        ...

    def search_history_recent(self, limit: int = 20) -> list[Any]:
        """Return most recent conversation messages."""
        ...

    # -- User identity operations -----------------------------------------------

    def create_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> None:
        """Create a user row. No-op if user_id already exists."""
        ...

    def create_first_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> bool:
        """Atomically create a user only if no users exist. Returns True if created."""
        ...

    def get_user(self, user_id: str) -> dict[str, str] | None:
        """Return user dict {user_id, username, display_name, password_hash, created} or None."""
        ...

    def get_user_by_username(self, username: str) -> dict[str, str] | None:
        """Lookup user by username. Returns same dict as get_user or None."""
        ...

    def list_users(self) -> list[dict[str, str]]:
        """Return all users ordered by created DESC."""
        ...

    def delete_user(self, user_id: str) -> bool:
        """Delete user and cascade-delete all their tokens. Returns True if existed."""
        ...

    def create_api_token(
        self,
        token_id: str,
        token_hash: str,
        token_prefix: str,
        user_id: str,
        name: str,
        scopes: str,
        expires: str | None = None,
    ) -> None:
        """Store a hashed API token."""
        ...

    def get_api_token_by_hash(self, token_hash: str) -> dict[str, str] | None:
        """Lookup token by SHA-256 hash. Returns dict with all columns or None."""
        ...

    def list_api_tokens(self, user_id: str) -> list[dict[str, str]]:
        """List tokens for a user (no hash in results, prefix only)."""
        ...

    def delete_api_token(self, token_id: str) -> bool:
        """Revoke/delete a token by ID. Returns True if existed."""
        ...

    # -- Channel user mapping ---------------------------------------------------

    def create_channel_user(self, channel_type: str, channel_user_id: str, user_id: str) -> None:
        """Map an external channel user to a turnstone user_id. No-op if exists."""
        ...

    def get_channel_user(self, channel_type: str, channel_user_id: str) -> dict[str, str] | None:
        """Lookup turnstone user for a channel user. Returns dict or None."""
        ...

    def list_channel_users_by_user(self, user_id: str) -> list[dict[str, str]]:
        """List all channel mappings for a turnstone user."""
        ...

    def delete_channel_user(self, channel_type: str, channel_user_id: str) -> bool:
        """Remove a channel user mapping. Returns True if existed."""
        ...

    # -- OIDC identity ---------------------------------------------------------

    def create_oidc_identity(self, issuer: str, subject: str, user_id: str, email: str) -> None:
        """Link an OIDC subject to a turnstone user. No-op if exists."""
        ...

    def get_oidc_identity(self, issuer: str, subject: str) -> dict[str, str] | None:
        """Lookup turnstone user by OIDC issuer+subject. Returns dict or None."""
        ...

    def update_oidc_identity_login(self, issuer: str, subject: str) -> bool:
        """Update last_login timestamp. Returns True if row existed."""
        ...

    def list_oidc_identities_for_user(self, user_id: str) -> list[dict[str, str]]:
        """List all OIDC identities linked to a turnstone user."""
        ...

    def delete_oidc_identity(self, issuer: str, subject: str) -> bool:
        """Remove an OIDC identity link. Returns True if existed."""
        ...

    # -- OIDC pending state ----------------------------------------------------

    def create_oidc_pending_state(
        self, state: str, nonce: str, code_verifier: str, audience: str
    ) -> None:
        """Store OIDC authorization flow state for callback validation."""
        ...

    def pop_oidc_pending_state(
        self, state: str, max_age_seconds: int = 300
    ) -> dict[str, str] | None:
        """Fetch and delete pending state atomically. Returns None if expired or missing."""
        ...

    def cleanup_expired_oidc_states(self, max_age_seconds: int = 300) -> int:
        """Delete expired pending states. Returns count of deleted rows."""
        ...

    # -- Channel routing -------------------------------------------------------

    def create_channel_route(
        self, channel_type: str, channel_id: str, ws_id: str, node_id: str = ""
    ) -> None:
        """Map a channel/thread to a workstream. No-op if exists."""
        ...

    def get_channel_route(self, channel_type: str, channel_id: str) -> dict[str, str] | None:
        """Lookup workstream for a channel/thread."""
        ...

    def get_channel_route_by_ws(self, ws_id: str) -> dict[str, str] | None:
        """Reverse lookup: find channel/thread for a workstream."""
        ...

    def list_channel_routes_by_type(self, channel_type: str) -> list[dict[str, str]]:
        """List all routes for a channel type, ordered by created DESC."""
        ...

    def delete_channel_route(self, channel_type: str, channel_id: str) -> bool:
        """Remove a channel route. Returns True if existed."""
        ...

    # -- Scheduled tasks -------------------------------------------------------

    def create_scheduled_task(
        self,
        task_id: str,
        name: str,
        description: str,
        schedule_type: str,
        cron_expr: str,
        at_time: str,
        target_mode: str,
        model: str,
        initial_message: str,
        auto_approve: bool,
        auto_approve_tools: list[str],
        created_by: str,
        next_run: str,
        skill: str = "",
        notify_targets: str = "[]",
    ) -> None:
        """Create a scheduled task. No-op if task_id already exists."""
        ...

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        """Return scheduled task dict or None."""
        ...

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        """Return all scheduled tasks ordered by created DESC."""
        ...

    def update_scheduled_task(self, task_id: str, **fields: Any) -> bool:
        """Update specified fields on a scheduled task. Returns True if found."""
        ...

    def delete_scheduled_task(self, task_id: str) -> bool:
        """Delete a scheduled task and its run history. Returns True if found."""
        ...

    def list_due_tasks(self, now: str) -> list[dict[str, Any]]:
        """Return enabled tasks whose next_run <= now, ordered by next_run."""
        ...

    def record_task_run(
        self,
        run_id: str,
        task_id: str,
        node_id: str,
        ws_id: str,
        correlation_id: str,
        started: str,
        status: str,
        error: str,
    ) -> None:
        """Record a scheduled task execution."""
        ...

    def list_task_runs(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """List run history for a task, ordered by started DESC."""
        ...

    def prune_task_runs(self, retention_days: int = 90) -> int:
        """Delete task runs older than retention_days. Returns count deleted."""
        ...

    # -- Watches ---------------------------------------------------------------

    def create_watch(
        self,
        watch_id: str,
        ws_id: str,
        node_id: str,
        name: str,
        command: str,
        interval_secs: float,
        stop_on: str | None,
        max_polls: int,
        created_by: str,
        next_poll: str,
    ) -> None:
        """Create a watch. No-op if watch_id already exists."""
        ...

    def get_watch(self, watch_id: str) -> dict[str, Any] | None:
        """Return watch dict or None."""
        ...

    def list_watches_for_ws(self, ws_id: str) -> list[dict[str, Any]]:
        """Return active watches for a workstream, ordered by created DESC."""
        ...

    def list_watches_for_node(self, node_id: str) -> list[dict[str, Any]]:
        """Return all active watches on a node, ordered by created DESC."""
        ...

    def list_due_watches(self, now: str) -> list[dict[str, Any]]:
        """Return active watches whose next_poll <= now, ordered by next_poll."""
        ...

    def update_watch(self, watch_id: str, **fields: Any) -> bool:
        """Update specified fields on a watch. Returns True if found."""
        ...

    def delete_watch(self, watch_id: str) -> bool:
        """Delete a watch. Returns True if found."""
        ...

    def delete_watches_for_ws(self, ws_id: str) -> int:
        """Delete all watches for a workstream. Returns count deleted."""
        ...

    # -- Service registry ------------------------------------------------------

    def register_service(
        self, service_type: str, service_id: str, url: str, metadata: str = "{}"
    ) -> None:
        """Register or update a service instance. Upserts by (service_type, service_id)."""
        ...

    def heartbeat_service(self, service_type: str, service_id: str) -> bool:
        """Update last_heartbeat for a registered service. Returns False if not found."""
        ...

    def list_services(self, service_type: str, max_age_seconds: int = 120) -> list[dict[str, str]]:
        """Return healthy services of a given type (heartbeat within max_age_seconds)."""
        ...

    def deregister_service(self, service_type: str, service_id: str) -> bool:
        """Remove a service registration. Returns True if existed."""
        ...

    # -- Node metadata ---------------------------------------------------------

    def get_node_metadata(self, node_id: str) -> list[dict[str, Any]]:
        """Return all metadata rows for a node."""
        ...

    def get_all_node_metadata(self) -> dict[str, list[dict[str, Any]]]:
        """Return metadata grouped by node_id for all nodes."""
        ...

    def set_node_metadata(self, node_id: str, key: str, value: str, source: str = "user") -> None:
        """Upsert a single metadata key for a node."""
        ...

    def set_node_metadata_bulk(self, node_id: str, entries: list[tuple[str, str, str]]) -> None:
        """Upsert multiple (key, value, source) entries for a node. Atomic."""
        ...

    def delete_node_metadata(self, node_id: str, key: str) -> bool:
        """Delete a single metadata key. Returns True if existed."""
        ...

    def delete_node_metadata_by_source(self, node_id: str, source: str) -> int:
        """Delete all metadata for a node with the given source. Returns count."""
        ...

    def filter_nodes_by_metadata(self, filters: dict[str, str]) -> set[str]:
        """Return node_ids where ALL key=value filters match (exact match)."""
        ...

    # -- Routing overrides ---

    def set_workstream_override(self, ws_id: str, node_id: str, reason: str = "targeted") -> None:
        """Pin a workstream to a specific node. Upserts."""
        ...

    def delete_workstream_override(self, ws_id: str) -> bool:
        """Remove a pin. Returns True if one existed."""
        ...

    def list_workstream_overrides(self) -> list[dict[str, str]]:
        """Return all overrides."""
        ...

    # -- Roles (RBAC) ----------------------------------------------------------

    def create_role(
        self,
        role_id: str,
        name: str,
        display_name: str,
        permissions: str,
        builtin: bool,
        org_id: str,
    ) -> None:
        """Create a role. No-op if role_id already exists."""
        ...

    def get_role(self, role_id: str) -> dict[str, Any] | None:
        """Return role dict or None."""
        ...

    def get_role_by_name(self, name: str) -> dict[str, Any] | None:
        """Lookup role by name. Returns same dict as get_role or None."""
        ...

    def list_roles(self, org_id: str = "") -> list[dict[str, Any]]:
        """Return all roles, optionally filtered by org_id. Ordered by name."""
        ...

    def update_role(self, role_id: str, **fields: Any) -> bool:
        """Update specified fields on a role. Returns True if found."""
        ...

    def delete_role(self, role_id: str) -> bool:
        """Delete a custom role. Returns True if found."""
        ...

    def assign_role(self, user_id: str, role_id: str, assigned_by: str) -> None:
        """Assign a role to a user. No-op if already assigned."""
        ...

    def unassign_role(self, user_id: str, role_id: str) -> bool:
        """Unassign a role from a user. Returns True if existed."""
        ...

    def list_user_roles(self, user_id: str) -> list[dict[str, Any]]:
        """List roles assigned to a user (joins user_roles with roles)."""
        ...

    def get_user_permissions(self, user_id: str) -> set[str]:
        """Return the union of all permissions from the user's assigned roles."""
        ...

    # -- Organizations ---------------------------------------------------------

    def create_org(self, org_id: str, name: str, display_name: str, settings: str = "{}") -> None:
        """Create an organization. No-op if org_id already exists."""
        ...

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        """Return org dict or None."""
        ...

    def list_orgs(self) -> list[dict[str, Any]]:
        """Return all organizations ordered by name."""
        ...

    def update_org(self, org_id: str, **fields: Any) -> bool:
        """Update specified fields on an org. Returns True if found."""
        ...

    # -- Tool policies ---------------------------------------------------------

    def create_tool_policy(
        self,
        policy_id: str,
        name: str,
        tool_pattern: str,
        action: str,
        priority: int,
        org_id: str,
        enabled: bool,
        created_by: str,
    ) -> None:
        """Create a tool policy."""
        ...

    def get_tool_policy(self, policy_id: str) -> dict[str, Any] | None:
        """Return tool policy dict or None."""
        ...

    def list_tool_policies(self, org_id: str = "") -> list[dict[str, Any]]:
        """Return all tool policies ordered by priority DESC."""
        ...

    def update_tool_policy(self, policy_id: str, **fields: Any) -> bool:
        """Update specified fields on a tool policy. Returns True if found."""
        ...

    def delete_tool_policy(self, policy_id: str) -> bool:
        """Delete a tool policy. Returns True if found."""
        ...

    # -- Prompt templates ------------------------------------------------------

    def create_prompt_template(
        self,
        template_id: str,
        name: str,
        category: str,
        content: str,
        variables: str,
        is_default: bool,
        org_id: str,
        created_by: str,
        origin: str = "manual",
        mcp_server: str = "",
        readonly: bool = False,
        description: str = "",
        tags: str = "[]",
        source_url: str = "",
        version: str = "1.0.0",
        author: str = "",
        activation: str = "named",
        token_estimate: int = 0,
        model: str = "",
        auto_approve: bool = False,
        temperature: float | None = None,
        reasoning_effort: str = "",
        max_tokens: int | None = None,
        token_budget: int = 0,
        agent_max_turns: int | None = None,
        notify_on_complete: str = "{}",
        enabled: bool = True,
        allowed_tools: str = "[]",
        skill_license: str = "",
        compatibility: str = "",
        priority: int = 0,
        kind: str = "any",
    ) -> None:
        """Create a prompt template (skill)."""
        ...

    def get_prompt_template(self, template_id: str) -> dict[str, Any] | None:
        """Return prompt template dict or None."""
        ...

    def get_prompt_template_by_name(self, name: str) -> dict[str, Any] | None:
        """Lookup prompt template by name. Returns same dict as get_prompt_template or None."""
        ...

    def list_prompt_templates(
        self, org_id: str = "", limit: int = 0, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return all prompt templates ordered by name."""
        ...

    def list_default_templates(self, org_id: str = "") -> list[dict[str, Any]]:
        """Return all templates where is_default=True, ordered by name."""
        ...

    def list_prompt_templates_by_origin(self, origin: str) -> list[dict[str, Any]]:
        """Return all prompt templates with the given origin, ordered by name."""
        ...

    def update_prompt_template(self, template_id: str, **fields: Any) -> bool:
        """Update specified fields on a prompt template. Returns True if found."""
        ...

    def delete_prompt_template(self, template_id: str) -> bool:
        """Delete a prompt template. Returns True if found."""
        ...

    def count_prompt_templates(self, org_id: str = "") -> int:
        """Count prompt templates, optionally filtered by org_id."""
        ...

    def list_skills_by_activation(
        self,
        activation: str,
        *,
        enabled_only: bool = False,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Return prompt templates filtered by activation value, ordered by priority then name."""
        ...

    def list_skills_filtered(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        risk_level: str | None = None,
        kinds: list[str] | None = None,
        enabled_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return prompt templates filtered by optional category/tag/risk_level/kinds,
        ordered by priority then name.

        Filters are pushed into SQL — no per-row Python filter loops.  The
        ``tag`` filter matches if the tag string appears in the JSON-array
        ``tags`` column (quote-bracketed substring against the JSON text:
        ``%"<tag>"%``).  Cheap and correct for tag values without quote
        characters; upgrade to true JSON-array containment if the
        convention ever needs to expand.

        ``kinds`` (when non-empty) narrows the result to rows whose
        ``kind`` column is in the supplied list.  Coordinator-side
        callers typically pass ``["coordinator", "any"]`` and
        interactive-side callers pass ``["interactive", "any"]`` so
        skills tagged ``any`` remain visible to both.  ``None`` means
        no kind filter — all rows regardless of kind.
        """
        ...

    def get_skill_by_name(self, name: str) -> dict[str, Any] | None:
        """Lookup skill (prompt template) by name. Returns dict or None."""
        ...

    def get_skill_by_source_url(self, source_url: str) -> dict[str, Any] | None:
        """Lookup skill (prompt template) by source_url. Returns dict or None."""
        ...

    def list_installed_skill_urls(self) -> list[dict[str, str]]:
        """Return [{source_url, template_id, risk_level}] for skills with non-empty source_url."""
        ...

    # -- Skill resources -------------------------------------------------------

    def create_skill_resource(
        self,
        resource_id: str,
        skill_id: str,
        path: str,
        content: str,
        content_type: str = "text/plain",
    ) -> None:
        """Create a bundled resource file for a skill."""
        ...

    def list_skill_resources(self, skill_id: str) -> list[dict[str, Any]]:
        """Return all resource files for a skill, ordered by path."""
        ...

    def get_skill_resource(self, skill_id: str, path: str) -> dict[str, Any] | None:
        """Return a single resource file by skill ID and path."""
        ...

    def delete_skill_resources(self, skill_id: str) -> int:
        """Delete all resource files for a skill. Returns count deleted."""
        ...

    def delete_skill_resource_by_path(self, skill_id: str, path: str) -> bool:
        """Delete a single resource file by skill_id and path. Returns True if found."""
        ...

    def count_skill_resources_bulk(self, skill_ids: list[str]) -> dict[str, int]:
        """Count resources per skill in a single query. Returns {skill_id: count}."""
        ...

    # -- Skill versions --------------------------------------------------------

    def create_skill_version(
        self,
        skill_id: str,
        version: int,
        snapshot: str,
        changed_by: str = "",
    ) -> None:
        """Create a version snapshot for a skill."""
        ...

    def list_skill_versions(self, skill_id: str) -> list[dict[str, Any]]:
        """List version history for a skill, ordered by version DESC."""
        ...

    def count_skill_versions(self, skill_id: str) -> int:
        """Return the count of version snapshots for ``skill_id``.

        Cheaper than ``list_skill_versions`` when the caller only needs
        the count (e.g. computing the next version number on the
        coordinator create path).
        """
        ...

    def delete_skill_versions(self, skill_id: str) -> int:
        """Delete all version snapshots for a skill. Returns count deleted."""
        ...

    # -- Usage events ----------------------------------------------------------

    def record_usage_event(
        self,
        event_id: str,
        user_id: str,
        ws_id: str,
        node_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        tool_calls_count: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Record a usage event (token counts, tool calls for one LLM request)."""
        ...

    def query_usage(
        self,
        since: str,
        until: str = "",
        user_id: str = "",
        model: str = "",
        group_by: str = "",
    ) -> list[dict[str, Any]]:
        """Query aggregated usage data. group_by: 'day', 'hour', 'model', 'user'."""
        ...

    def prune_usage_events(self, retention_days: int = 90) -> int:
        """Delete usage events older than retention_days. Returns count deleted."""
        ...

    def sum_workstream_tokens(self, ws_id: str) -> int:
        """Return SUM(prompt_tokens + completion_tokens) across all usage_events
        for ``ws_id``.  Returns 0 when no events exist or the ws_id is empty.

        Used as a fallback when the live token counter on a child workstream is
        zero (e.g. an idle child whose node hasn't published a fresh tick) so
        coordinator inspect doesn't report 0 tokens for a child that's already
        burned thousands.
        """
        ...

    def sum_workstream_tokens_batch(self, ws_ids: list[str]) -> dict[str, int]:
        """Bulk variant of ``sum_workstream_tokens`` — returns
        ``{ws_id: total_tokens}`` for every id in ``ws_ids``.  Missing ids
        default to 0.  Empty input returns ``{}``.

        Used by ``wait_for_workstream`` to amortize per-tick polling
        across N children into a single ``WHERE ws_id IN (...) GROUP BY``
        query — at the 32-ws/600s/0.5s-tick cap (1200 ticks × two
        storage calls per tick — ``get_workstreams_batch`` paired with
        this one) that's ~2400 round-trips per wait, down from ~38k
        under the naive per-id polling shape.

        SECURITY: this primitive does NO ownership / authorization
        check — callers MUST gate the input ws_ids against the caller's
        tenant subtree before invoking, the same way ``sum_workstream_tokens``
        and ``get_workstream`` rely on caller-side gating.  The single
        in-tree caller (``CoordinatorClient.wait_for_workstream``)
        enforces this via its own dedup + cap path; new callers must
        do the same.
        """
        ...

    def get_workstreams_batch(self, ws_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """Bulk variant of ``get_workstream`` — returns ``{ws_id: row | None}``
        for every id in ``ws_ids``.  Missing rows surface as ``None``.
        Empty input returns ``{}``.

        Pairs with ``sum_workstream_tokens_batch`` to give the
        coordinator wait-loop one query per tick instead of two-per-id.
        Row shape matches ``get_workstream`` (same projection).

        SECURITY: same caveat as ``sum_workstream_tokens_batch`` —
        no ownership / authorization check inside the batch result.
        Callers MUST enforce subtree ownership before invoking.
        """
        ...

    # -- Audit events ----------------------------------------------------------

    def record_audit_event(
        self,
        event_id: str,
        user_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        detail: str,
        ip_address: str,
    ) -> None:
        """Record an audit event."""
        ...

    def list_audit_events(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
        resource_id: str = "",
    ) -> list[dict[str, Any]]:
        """List audit events with optional filters, ordered by timestamp DESC.

        ``resource_id`` filters to events scoped to a single
        workstream (or other resource id) — added so per-ws
        consumers like
        ``SessionUIBase.replay_recent_auto_approvals_from_audit``
        can pull a workstream's bypass history without scanning
        the full table.
        """
        ...

    def count_audit_events(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
    ) -> int:
        """Count audit events matching the filters."""
        ...

    def prune_audit_events(self, retention_days: int = 365) -> int:
        """Delete audit events older than retention_days. Returns count deleted."""
        ...

    # -- Intent verdicts -------------------------------------------------------

    def create_intent_verdict(
        self,
        verdict_id: str,
        ws_id: str,
        call_id: str,
        func_name: str,
        func_args: str,
        intent_summary: str,
        risk_level: str,
        confidence: float,
        recommendation: str,
        reasoning: str,
        evidence: str,
        tier: str,
        judge_model: str,
        latency_ms: int,
    ) -> None:
        """Record an intent validation verdict."""
        ...

    def create_intent_verdicts_bulk(self, verdicts: list[dict[str, Any]]) -> None:
        """Insert many intent_verdict rows in one transaction.

        Each dict mirrors :meth:`create_intent_verdict`'s keyword args
        (``verdict_id`` / ``ws_id`` / ``call_id`` / ``func_name`` /
        ``func_args`` / ``intent_summary`` / ``risk_level`` /
        ``confidence`` / ``recommendation`` / ``reasoning`` / ``evidence`` /
        ``tier`` / ``judge_model`` / ``latency_ms``). Used by the
        synchronous heuristic-verdict persistence loop in
        ``approve_tools`` so a tool-heavy turn doesn't pay N×commit
        latency before the approval prompt renders.
        """
        ...

    def get_intent_verdict(self, verdict_id: str) -> dict[str, Any] | None:
        """Return intent verdict dict or None."""
        ...

    def list_intent_verdicts(
        self,
        ws_id: str = "",
        since: str = "",
        until: str = "",
        risk_level: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List intent verdicts with optional filters, ordered by created DESC."""
        ...

    def update_intent_verdict(self, verdict_id: str, **fields: Any) -> bool:
        """Update fields on an intent verdict (e.g. user_decision). Returns True if found."""
        ...

    def count_intent_verdicts(
        self,
        ws_id: str = "",
        since: str = "",
        until: str = "",
        risk_level: str = "",
    ) -> int:
        """Count intent verdicts matching the filters."""
        ...

    # -- Output assessments ----------------------------------------------------

    def record_output_assessment(
        self,
        assessment_id: str,
        ws_id: str,
        call_id: str,
        func_name: str,
        flags: str,
        risk_level: str,
        annotations: str,
        output_length: int,
        redacted: bool,
    ) -> None:
        """Record an output guard assessment."""
        ...

    def list_output_assessments(
        self,
        ws_id: str = "",
        risk_level: str = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List output assessments with optional filters, ordered by created DESC."""
        ...

    def count_output_assessments(
        self,
        ws_id: str = "",
        risk_level: str = "",
        since: str = "",
        until: str = "",
    ) -> int:
        """Count output assessments matching the filters."""
        ...

    # -- System settings -------------------------------------------------------

    def get_system_setting(self, key: str, node_id: str = "") -> dict[str, Any] | None:
        """Return setting dict or None."""
        ...

    def list_system_settings(self, node_id: str = "") -> list[dict[str, Any]]:
        """Return settings ordered by key.

        When *node_id* is provided, returns both global (node_id="")
        and node-specific settings.  When empty, returns all settings.
        """
        ...

    def upsert_system_setting(
        self,
        key: str,
        value: str,
        node_id: str = "",
        is_secret: bool = False,
        changed_by: str = "",
    ) -> None:
        """Create or update a system setting. Value is JSON-encoded."""
        ...

    def delete_system_setting(self, key: str, node_id: str = "") -> bool:
        """Delete a setting by (key, node_id). Returns True if existed."""
        ...

    def get_system_settings_bulk(self, node_id: str = "") -> dict[str, str]:
        """Return all settings as {key: json_value} dict.

        Loads global settings (node_id="") first, then overlays per-node
        overrides if node_id is provided.
        """
        ...

    # -- MCP server definitions ------------------------------------------------

    def create_mcp_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: str = "",
        args: str = "[]",
        url: str = "",
        headers: str = "{}",
        env: str = "{}",
        auto_approve: bool = False,
        enabled: bool = True,
        created_by: str = "",
        registry_name: str | None = None,
        registry_version: str = "",
        registry_meta: str = "{}",
    ) -> None:
        """Create an MCP server definition. No-op if server_id already exists."""
        ...

    def get_mcp_server(self, server_id: str) -> dict[str, Any] | None:
        """Return MCP server dict or None."""
        ...

    def get_mcp_server_by_name(self, name: str) -> dict[str, Any] | None:
        """Return MCP server dict by name or None."""
        ...

    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Return MCP servers ordered by name."""
        ...

    def update_mcp_server(self, server_id: str, **fields: Any) -> bool:
        """Update specified fields on an MCP server. Returns True if found."""
        ...

    def get_mcp_server_by_registry_name(self, registry_name: str) -> dict[str, Any] | None:
        """Return MCP server dict by registry name or None."""
        ...

    def delete_mcp_server(self, server_id: str) -> bool:
        """Delete an MCP server definition. Returns True if existed."""
        ...

    # -- Model definitions -----------------------------------------------------

    def create_model_definition(
        self,
        definition_id: str,
        alias: str,
        model: str,
        provider: str = "openai",
        base_url: str = "",
        api_key: str = "",
        context_window: int = 32768,
        capabilities: str = "{}",
        enabled: bool = True,
        created_by: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Create a model definition. No-op if definition_id already exists."""
        ...

    def get_model_definition(self, definition_id: str) -> dict[str, Any] | None:
        """Return model definition dict or None."""
        ...

    def get_model_definition_by_alias(self, alias: str) -> dict[str, Any] | None:
        """Return model definition dict by alias or None."""
        ...

    def list_model_definitions(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Return model definitions ordered by alias."""
        ...

    def update_model_definition(self, definition_id: str, **fields: Any) -> bool:
        """Update specified fields on a model definition. Returns True if found."""
        ...

    def delete_model_definition(self, definition_id: str) -> bool:
        """Delete a model definition. Returns True if existed."""
        ...

    # -- Prompt policies -------------------------------------------------------

    def list_prompt_policies(self, org_id: str = "") -> list[dict[str, Any]]:
        """Return all prompt policies ordered by priority."""
        ...

    def get_prompt_policy(self, policy_id: str) -> dict[str, Any] | None:
        """Return prompt policy dict or None."""
        ...

    def upsert_prompt_policy(self, policy: dict[str, Any]) -> None:
        """Create or update a prompt policy."""
        ...

    def delete_prompt_policy(self, policy_id: str) -> bool:
        """Delete a prompt policy. Returns True if existed."""
        ...

    # -- Heuristic rules -------------------------------------------------------

    def create_heuristic_rule(
        self,
        rule_id: str,
        name: str,
        risk_level: str,
        confidence: float,
        recommendation: str,
        tool_pattern: str,
        arg_patterns: str = "[]",
        intent_template: str = "",
        reasoning_template: str = "",
        tier: str = "medium",
        priority: int = 0,
        builtin: bool = False,
        enabled: bool = True,
        created_by: str = "",
    ) -> None:
        """Create a heuristic rule. No-op if rule_id already exists."""
        ...

    def get_heuristic_rule(self, rule_id: str) -> dict[str, Any] | None:
        """Return heuristic rule dict or None."""
        ...

    def get_heuristic_rule_by_name(self, name: str) -> dict[str, Any] | None:
        """Return heuristic rule dict by name or None."""
        ...

    def list_heuristic_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Return heuristic rules ordered by tier priority then rule priority."""
        ...

    def update_heuristic_rule(self, rule_id: str, **fields: Any) -> bool:
        """Update specified fields on a heuristic rule. Returns True if found."""
        ...

    def delete_heuristic_rule(self, rule_id: str) -> bool:
        """Delete a heuristic rule. Returns True if existed."""
        ...

    # -- Output guard patterns -------------------------------------------------

    def create_output_guard_pattern(
        self,
        pattern_id: str,
        name: str,
        category: str,
        risk_level: str,
        pattern: str,
        flag_name: str,
        annotation: str,
        pattern_flags: str = "",
        is_credential: bool = False,
        redact_label: str = "",
        priority: int = 0,
        builtin: bool = False,
        enabled: bool = True,
        created_by: str = "",
    ) -> None:
        """Create an output guard pattern. No-op if pattern_id already exists."""
        ...

    def get_output_guard_pattern(self, pattern_id: str) -> dict[str, Any] | None:
        """Return output guard pattern dict or None."""
        ...

    def get_output_guard_pattern_by_name(self, name: str) -> dict[str, Any] | None:
        """Return output guard pattern dict by name or None."""
        ...

    def list_output_guard_patterns(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Return output guard patterns ordered by category then priority."""
        ...

    def update_output_guard_pattern(self, pattern_id: str, **fields: Any) -> bool:
        """Update specified fields on an output guard pattern. Returns True if found."""
        ...

    def delete_output_guard_pattern(self, pattern_id: str) -> bool:
        """Delete an output guard pattern. Returns True if existed."""
        ...

    # -- TLS / ACME (lacme Store) ----------------------------------------------

    def save_tls_account_key(self, key_id: str, key_pem: str) -> None:
        """Persist an ACME account private key."""
        ...

    def load_tls_account_key(self, key_id: str) -> str | None:
        """Load an ACME account key PEM by ID. Returns None if not found."""
        ...

    def save_tls_ca(self, name: str, cert_pem: str, key_pem: str) -> None:
        """Persist a CA root certificate and key."""
        ...

    def load_tls_ca(self, name: str) -> dict[str, Any] | None:
        """Load CA cert+key by name. Returns dict with cert_pem, key_pem or None."""
        ...

    def save_tls_cert(
        self,
        domain: str,
        cert_pem: str,
        fullchain_pem: str,
        key_pem: str,
        issued_at: str,
        expires_at: str,
        meta: str | None = None,
    ) -> None:
        """Persist an issued certificate (upsert by domain)."""
        ...

    def load_tls_cert(self, domain: str) -> dict[str, Any] | None:
        """Load certificate by domain. Returns dict or None."""
        ...

    def list_tls_certs(self) -> list[dict[str, Any]]:
        """List all stored certificates."""
        ...

    def delete_tls_cert(self, domain: str) -> bool:
        """Delete a certificate by domain. Returns True if existed."""
        ...

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Release resources (connection pool, engine, etc.)."""
        ...
