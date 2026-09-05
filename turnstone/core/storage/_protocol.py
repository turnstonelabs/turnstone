"""Storage backend protocol — the contract every persistence adapter must implement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from contextlib import AbstractContextManager

    from turnstone.core.storage._notify import NotifyStream
    from turnstone.core.trajectory import Turn
    from turnstone.core.workstream import WorkstreamKind


#: MCP auth types whose connections and per-user token rows are keyed
#: per-(user, server): ``oauth_user`` (per-server browser consent + refresh
#: token) and ``oauth_obo`` (minted on demand from the user's single captured
#: credential, issue #551). Defined here — the bottom of the import graph — so
#: the backend SQL predicates (``any_user_scoped_mcp_servers``) and the
#: application layer (via :mod:`turnstone.core.mcp_crypto`, which re-exports
#: it) agree on ONE set that cannot drift.
USER_SCOPED_AUTH_TYPES: frozenset[str] = frozenset({"oauth_user", "oauth_obo"})

# Internal durable-incarnation fence. New manager rows receive it at
# registration; legacy rows receive one atomically when an exact delete/fork
# snapshot is claimed. It lives in ``workstream_config`` so row + fence can be
# installed without a schema change. Clone and publication retain it for ABA
# protection; public config reads/writes and fork snapshots always exclude it.
FORK_RESERVATION_CONFIG_KEY = "__fork_destination_reservation"


class StorageConflictError(Exception):
    """Raised by storage methods when a unique-constraint violation occurs.

    Backends raise this so callers don't need to inspect dialect-specific
    ``IntegrityError`` payloads.  The message identifies which constraint
    conflicted (e.g. ``"users.username"`` vs ``"oidc_identities.PRIMARY"``)
    when the backend can distinguish them.
    """


class ConversationCommitConflictError(StorageConflictError):
    """A keyed conversation retry does not match the committed operation.

    A ``commit_key`` identifies one immutable logical write.  Returning the
    existing row for a retry with different role-specific fields, metadata,
    event cursor, or ordered attachment references would acknowledge data the
    caller did not commit, so atomic attachment paths refuse that mismatch.
    """


class ConversationCommitWorkstreamGoneError(RuntimeError):
    """A keyed conversation commit's durable parent row no longer exists.

    Keyed saves refuse to recreate a hard-deleted workstream, so this failure
    is permanent: no retry of the same commit can ever succeed.  The durability
    journal uses the type to stop retrying instead of classifying the miss as
    a transient storage failure.
    """


@dataclass(frozen=True, slots=True)
class AttachmentWrite:
    """One content-addressed blob reference in an atomic conversation write.

    The ordered input sequence is the message's reference list.  Repeated
    ``attachment_id`` values therefore represent repeated references and each
    contribute one to the global refcount.
    """

    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int
    kind: str
    content: bytes


class ForkCloneError(RuntimeError):
    """Base class for an atomic workstream-clone refusal."""


class ForkSourceUnavailableError(ForkCloneError):
    """The source is missing, inaccessible, or no longer safely cloneable.

    Missing and authorization-denied sources deliberately share one exception
    so an API caller cannot use the fork path as a private-workstream oracle.
    """


class ForkDestinationConflictError(ForkCloneError):
    """The destination is missing, belongs to another principal, or is non-empty."""


@dataclass(frozen=True, slots=True)
class ForkCloneExpectation:
    """Construction-time session envelope a fork transaction must still match.

    Fork creation constructs the destination session before the storage clone
    runs because persona MCP gating and project-memory wiring happen in the
    constructor.  The source can change between that preflight and the clone.
    Carrying this immutable witness into the transaction makes such drift a
    retryable source refusal instead of committing history under a stale live
    security envelope. Source and destination reservation tokens additionally
    fence delete/re-register and pre-publication rollback/retry races.
    """

    persona_config: tuple[tuple[str, str], ...]
    project_id: str
    project_name: str
    project_writable: bool
    destination_reservation_token: str
    source_reservation_token: str


@dataclass(frozen=True, slots=True)
class ForkCloneSnapshot:
    """Canonical source state committed by :meth:`StorageBackend.clone_workstream`.

    ``turns`` is the same recovered, checkpoint-bounded trajectory a resume
    loads. ``config`` and ``project_id`` are the source values installed on the
    destination in the same transaction.
    """

    turns: tuple[Turn, ...]
    config: dict[str, str]
    project_id: str | None


class OIDCIdentity(TypedDict):
    """Row shape returned by OIDC identity lookups."""

    issuer: str
    subject: str
    user_id: str
    email: str
    created: str
    last_login: str
    # Entra `oid`/`tid`: the stable cross-app user key (see oidc_identities schema).
    # "" when the IdP did not supply them.
    oid: str
    tid: str


class OIDCUserCredential(TypedDict):
    """Row shape for the per-(user, issuer) captured IdP refresh token.

    ``refresh_token_ct`` is a Fernet ciphertext blob (same envelope as
    ``mcp_user_tokens``); the storage layer returns it verbatim and
    ``MCPTokenStore`` handles encrypt/decrypt.  One row per user per
    issuer — the single credential that ``auth_type='oauth_obo'`` MCP
    servers redeem on demand (issue #551).
    """

    user_id: str
    issuer: str
    refresh_token_ct: bytes
    created: str
    last_refreshed: str


class OIDCPendingState(TypedDict):
    """Row shape returned when popping a pending OIDC authorization-flow state."""

    state: str
    nonce: str
    code_verifier: str
    audience: str
    created_at: str


class MCPUserToken(TypedDict):
    """Row shape returned by per-(user, MCP server) OAuth token lookups.

    ``access_token_ct`` and ``refresh_token_ct`` are Fernet ciphertext
    blobs; the storage layer returns them verbatim and ``MCPTokenStore``
    handles encrypt/decrypt.
    """

    user_id: str
    server_name: str
    access_token_ct: bytes
    refresh_token_ct: bytes | None
    expires_at: str | None
    scopes: str | None
    as_issuer: str
    audience: str
    created: str
    last_refreshed: str | None


class MCPUserTokenMetadataRow(TypedDict):
    """Non-secret projection of ``mcp_user_tokens`` for the settings UI.

    Excludes ``access_token_ct`` and ``refresh_token_ct`` so the
    storage layer never materialises ciphertext for list queries that
    only need metadata. ``MCPTokenStore.list_user_token_metadata``
    re-types these rows as ``MCPUserTokenMetadata`` (same field shape).
    """

    user_id: str
    server_name: str
    expires_at: str | None
    scopes: str | None
    as_issuer: str
    audience: str
    created: str
    last_refreshed: str | None


class MCPOAuthPendingState(TypedDict):
    """Row shape returned when popping a pending MCP OAuth flow state."""

    state: str
    user_id: str
    server_name: str
    code_verifier: str
    return_url: str
    created_at: str


class MCPPendingConsentRow(TypedDict):
    """Row shape for deferred-consent records.

    Emitted by the pool dispatchers (Phase 5+) when a non-interactive
    run (scheduled / channel) hits ``mcp_consent_required`` or
    ``mcp_insufficient_scope`` and the user can't be prompted in the
    moment.  Composite PK ``(user_id, server_name)`` collapses repeat
    occurrences for the same server into one row.
    """

    user_id: str
    server_name: str
    error_code: str
    scopes_required: str | None
    first_seen_at: str
    last_seen_at: str
    occurrence_count: int


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
        source: str | None = None,
        event_id: int | None = None,
        is_error: bool = False,
        producer: str | None = None,
        meta: str | None = None,
        commit_key: str | None = None,
    ) -> int:
        """Log a message to the conversations table.

        Returns the inserted row's ``id`` (autoincrement PK).  Callers
        that need to link side tables (e.g. ``workstream_attachments``)
        use this to associate the row after save.

        ``source`` is the persisted twin of the in-memory ``_source``
        side-channel — which producer synthesised the row (a wake
        ``"system_nudge"`` or an operator-context kind on a ``system`` turn);
        NULL for ordinary user/assistant/tool rows.

        ``event_id`` is the per-ws SSE ring-buffer high-water mark at save
        time (``SessionUIBase._event_id``) — the ``Last-Event-ID`` resume
        cursor space, distinct from the returned ``id`` PK.  NULL when the
        caller has no live UI counter (offline / bulk / fork re-saves).

        ``meta`` is pre-serialized role-specific conversation metadata
        (operator context, tool disposition/preview plus the tool turn's acting
        principal, shared-workstream sender, or accepted-assistant model
        provenance); opaque to storage and NULL when a row has no metadata.

        ``commit_key`` is a caller-generated, non-empty idempotency identity scoped to
        ``ws_id``.  The first save inserts the row; subsequent saves with the
        same non-NULL key and identical normalized fields return that row's
        existing ``id`` without appending or replacing content. A mismatched
        retry raises :class:`ConversationCommitConflictError`. A keyed save
        requires the durable workstream row to exist in the same transaction;
        it refuses a retry after hard delete instead of recreating an orphan
        conversation. NULL preserves append-only legacy/offline semantics,
        including historical parent-less writers. PostgreSQL conditionally
        takes the same parent-first lock as prune/delete when that parent
        exists. If the call observes a parent before blocking behind deletion,
        it refuses the post-delete insert; a call that genuinely begins
        parentless may still create an orphan. NULL therefore remains
        unsuitable for a live accepted row, which MUST use a key. An empty
        string is rejected.
        """
        ...

    def save_user_message_with_attachments(
        self,
        ws_id: str,
        content: str,
        attachments: list[AttachmentWrite] | tuple[AttachmentWrite, ...],
        *,
        source: str | None = None,
        event_id: int | None = None,
        meta: str | None = None,
        commit_key: str,
    ) -> int:
        """Atomically commit one keyed USER row and its attachment references.

        The conversation row, new content-addressed blobs, exact per-reference
        refcount increments, and ordered ``conversations.attachments`` list are
        one database transaction.  A retry with the same ``(ws_id,
        commit_key)`` and identical normalized payload returns the original row
        id without changing refcounts.  A retry whose row payload or ordered
        attachment ids differ raises
        :class:`ConversationCommitConflictError`; no partial mutation survives
        any failure. The durable workstream parent is locked/validated in the
        same transaction, so a retry after hard delete is refused without
        recreating either the row or attachment ownership.

        This operation is intentionally narrower than :meth:`save_message`:
        ordinary rows and attachment-free USER rows retain their established
        persistence path.
        """
        ...

    def save_tool_message_with_attachments(
        self,
        ws_id: str,
        content: str,
        tool_name: str,
        tool_call_id: str,
        attachments: list[AttachmentWrite] | tuple[AttachmentWrite, ...],
        *,
        event_id: int | None = None,
        is_error: bool = False,
        meta: str | None = None,
        commit_key: str,
    ) -> int:
        """Atomically commit one keyed TOOL row and its attachment references.

        The immutable identity covers content, tool name/call id, event cursor,
        error disposition, metadata, and the exact ordered attachment ids. Blob
        bytes, size, MIME, and kind are validated under the content-addressed id;
        newly inserted blobs carry ``origin='tool'``. An identical retry returns
        the original row id without replaying refcount increments. Any mismatch
        or partial failure raises and leaves the whole transaction unchanged.
        The durable workstream parent is locked/validated in that transaction;
        hard delete therefore cannot leave a later retry as an orphan.
        """
        ...

    def save_messages_bulk(self, rows: list[dict[str, Any]]) -> None:
        """Insert multiple conversation rows in a single transaction.

        Each dict must include ``ws_id``, ``role``, and ``content``
        (which may be ``None`` for assistant messages with only tool_calls).
        Optional keys: ``tool_name``, ``tool_call_id``, ``provider_data``,
        ``tool_calls``, ``source``, ``meta``, and ``attachment_ids`` (an
        ordered list of existing content-addressed ids). Attachment refcounts,
        timestamp, and workstream updated-at are handled in the same transaction.
        """
        ...

    def load_messages(
        self,
        ws_id: str,
        *,
        limit: int | None = None,
        repair: bool = True,
        include_compaction: bool = False,
    ) -> list[dict[str, Any]]:
        """Load messages for a workstream and reconstruct OpenAI message format.

        ``limit`` caps the number of underlying conversation rows fetched
        (from the tail, then reversed), bounding memory for callers that
        only need a recent slice — e.g. the cluster-inspect endpoint.  The
        returned message list may have slightly fewer *reconstructed*
        entries than ``limit`` when a tool-call group splits across the
        boundary; callers that need strict tail-N semantics must slice
        again client-side.  Default ``None`` fetches the full history.

        ``repair`` (default True) post-processes the result into a
        wire-shape valid for an LLM round-trip — drops a trailing
        ``assistant(tool_calls)`` whose results aren't all present and
        fills mid-conversation orphans with synthetic cancellation
        results.  Display-only readers (``/history`` REST) should pass
        ``repair=False`` so the user sees the actual partial state
        instead of having the trailing turn silently stripped during
        live tool execution.

        Attachments are resolved to inline content parts (the materialized
        bytes a display/export consumer needs); :meth:`load_message_turns` is
        the unresolved, by-reference counterpart for resume.

        ``include_compaction`` (default False) surfaces persisted compaction
        checkpoint markers as in-place ``role="system"`` display rows
        (``_source="compaction"``, ``meta`` = watermark/token counts) instead
        of dropping them — the ``/history`` display path passes True so the
        UI can re-render its compaction card after a reload; export/search
        keep the unannotated transcript.
        """
        ...

    def load_message_turns(self, ws_id: str, *, checkpointed: bool = True) -> list[Turn]:
        """Load a workstream's history as canonical ``Turn``s for resume.

        Unlike :meth:`load_messages` this keeps attachments *by reference*
        (:class:`AttachmentRef`) — ``session.messages`` is the canonical Turn
        trajectory and materializes bytes only at each output (wire / display).
        The trailing-incomplete-tool-call strip (``recover_trajectory``) is
        applied; mid-conversation orphans are left for the send-time repair.

        ``checkpointed=True`` (resume default) honors a persisted compaction
        marker and returns the bounded ``[summary] + [tail]`` view;
        ``checkpointed=False`` returns the full transcript (markers dropped) for
        export/audit consumers that must not lose pre-compaction history.
        """
        ...

    def clone_workstream(
        self,
        source_ws_id: str,
        destination_ws_id: str,
        *,
        principal_id: str,
        trusted_internal: bool = False,
        expected_session: ForkCloneExpectation | None = None,
    ) -> ForkCloneSnapshot:
        """Atomically authorize and snapshot-copy one workstream into another.

        The transaction re-evaluates the source's current project visibility
        and attachability for ``principal_id``, snapshots its canonical
        checkpoint-bounded history and configuration, retains every referenced
        attachment blob, replaces the destination configuration, and binds the
        destination to the source's effective project (or no project when the
        source link is absent/dangling).

        The destination must already exist, belong to ``principal_id``, contain
        no conversation rows, and carry the same normalized project binding the
        source resolves to inside the transaction. A mismatch means source
        project context changed after destination construction and refuses the
        clone. When ``expected_session`` is supplied, the transaction also
        requires both durable incarnation tokens, the source persona stamp, and
        the principal's effective active project-memory context to equal the
        values used to construct the live destination session.
        ``state='creating'`` sources are never cloneable. ``trusted_internal``
        is reserved for non-user
        service/CLI callers and bypasses the principal/ACL checks; it does not
        relax source or destination existence, project coherence, envelope
        coherence, emptiness, or attachment integrity checks.

        Raises :class:`ForkSourceUnavailableError` for a missing, inaccessible,
        or corrupt source and :class:`ForkDestinationConflictError` when the
        destination preconditions do not hold. No partial history, config, or
        attachment-refcount changes survive either failure.
        """
        ...

    def list_message_senders(self, ws_id: str) -> list[str]:
        """Distinct sender user-ids recorded on a workstream's USER rows.

        Reads the full persisted history (``meta`` → ``{"sender": ...}``), not
        a compaction-bounded view: the participant set drives shared-workstream
        framing and the one-time join note, so it must survive compaction
        narrowing the resumable ``[summary] + [tail]`` slice.
        """
        ...

    def get_max_event_id(self, ws_id: str) -> int | None:
        """Return the highest persisted ``event_id`` for ``ws_id``.

        The SSE ``Last-Event-ID`` resume-cursor high-water mark across
        the workstream's whole life.  ``None`` when no row carries one
        (fresh ws, or only pre-migration-059 / bulk-saved NULL rows).
        Used to reseed ``SessionUIBase._event_id`` on UI construction so
        the per-ws event-id space stays monotonic across process
        restarts / rehydrates (it resets to 0 otherwise, which would
        re-issue ids the ring buffer already handed out).
        """
        ...

    def get_compaction_watermark(self, ws_id: str, preserve_tail: int = 0) -> int | None:
        """Boundary id for a compaction checkpoint marker.

        The max conversation ``id`` among the rows a compaction would
        summarize: ``max(id)`` when ``preserve_tail=0`` (the auto/overflow
        path), or the ``(N+1)``-th newest id when ``preserve_tail=N`` keeps
        the newest ``N`` rows verbatim.  Persisted in the marker's ``meta`` so
        resume can rehydrate ``[summary] + [rows after the watermark]``.
        ``None`` when the workstream has no rows.
        """
        ...

    def count_messages(self, ws_id: str) -> int:
        """Total conversation rows for ``ws_id`` (compaction markers included)."""
        ...

    def get_compaction_floor(self, ws_id: str) -> int:
        """Rows backing the latest compaction summary that rewind/retry must not
        delete: every row with ``id <= the latest marker's id`` (summarized
        prefix + marker).  ``0`` when the workstream never compacted.  Used to
        floor the rewind/retry truncation so the summary's backing survives.
        """
        ...

    def get_compaction_checkpoint(self, ws_id: str) -> int | None:
        """The latest persisted compaction marker's watermark for ``ws_id``.

        Every row with ``id <=`` the returned boundary was folded into the
        summary the live session now holds — the summarized-away past; rows
        above it are the live segment still in the model's context.  Distinct
        from :meth:`get_compaction_watermark`, which computes the boundary a
        NEW compaction would use; this reads the one already persisted.
        ``None`` when the workstream never compacted or the marker's meta is
        malformed (callers must then treat the WHOLE workstream as live).
        """
        ...

    # -- Workstream attachments (content-addressed, refcounted) ---------------
    #
    # Pending (uploaded-but-unsent) bytes live in the per-node in-memory
    # ``attachment_buffer``, NOT in storage — the persisted pending/reserved/
    # consumed lifecycle (and its orphan-sweep) was retired by the
    # content-addressing cutover.  Storage holds only committed blobs: written
    # content-addressed at send-commit (or when a tool produces an image),
    # deduped by content hash, and reference-counted via the ordered
    # ``conversations.attachments`` ref-list.

    def save_attachment(
        self,
        attachment_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        kind: str,
        content: bytes,
        origin: str = "upload",
    ) -> None:
        """Write a content-addressed blob (INSERT-OR-IGNORE) and ``refcount += 1``.

        ``attachment_id`` is the content hash (sha256 hex); the caller computes
        it.  The first reference inserts the row at ``refcount = 1``; every
        later reference (a re-upload of identical bytes, or a second message
        referencing the same blob) only bumps the count.  A stored blob is thus
        always referenced (born at ≥ 1) and identical bytes dedupe to one row
        across messages and workstreams.  ``origin`` is ``'upload'`` (user
        attachment) or ``'tool'`` (e.g. a ``read_file`` image).
        """
        ...

    def set_message_attachments(
        self, ws_id: str, message_id: int, attachment_ids: list[str]
    ) -> None:
        """Record a turn's ordered content-addressed ref-list on its row.

        Writes the JSON id-list onto ``conversations.attachments`` for the
        ``(ws_id, message_id)`` conversations row — the sole message->blob
        link.  Empty input is a no-op (the column stays NULL).  Scoped to
        ``ws_id`` as defense-in-depth against a cross-ws message id.
        """
        ...

    def get_attachments(
        self, attachment_ids: list[str], exclude_kinds: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """Bulk fetch attachments by id, including their ``content`` bytes.

        Unknown ids are silently skipped.  Order is unspecified.
        ``exclude_kinds`` filters at the QUERY so callers that will discard a
        kind anyway (trajectory reconstruction skips ``preview`` blobs) don't
        pull multi-megabyte content off disk just to drop it.
        """
        ...

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        """Return a single attachment row (with content bytes) or None."""
        ...

    def attachment_referenced_in_ws(self, attachment_id: str, ws_id: str) -> bool:
        """True iff some conversations row in ``ws_id`` references ``attachment_id``.

        The committed-attachment ownership gate: the per-row ``ws_id`` /
        ``user_id`` scope columns are gone, so ``get_content`` for a committed
        blob is authorised by proving the requester (already gated to own
        ``ws_id``) has a turn in that workstream whose ``attachments`` ref-list
        names the id.
        """
        ...

    def delete_messages_after(self, ws_id: str, keep_count: int) -> int:
        """Delete conversation rows beyond the first *keep_count* rows for a workstream.

        Rows are ordered by auto-increment ``id``.  If the workstream has
        N rows total and ``keep_count`` < N, the last N - keep_count rows
        are deleted.  Returns the number of rows deleted.
        """
        ...

    def truncate_messages_tail(self, ws_id: str, remove_count: int) -> int:
        """Atomically remove up to *remove_count* newest conversation rows.

        The backend locks the durable workstream, derives both the current row
        count and latest compaction floor inside that transaction, and never
        deletes rows backing the latest compaction marker.  Attachment
        refcounts are released for exactly the rows deleted.  Missing
        workstreams and storage failures raise; a negative count is invalid.
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
        offset: int = 0,
    ) -> list[Any]:
        """List workstreams that have messages, ordered by updated DESC.

        ``offset`` skips that many rows before applying ``limit`` — the
        saved-list collector pages through with it so a post-SQL
        visibility filter can keep fetching until it fills its window.

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

        Returns rows of ``(ws_id, alias, title, name, created, updated,
        message_count, node_id, state, kind, model_alias, launch_skill,
        child_count, context_tokens, context_window, project_id, user_id,
        persona)`` ordered by updated DESC.  The trailing enrichment columns
        feed the saved-list DTO: ``model_alias`` / ``launch_skill`` come
        from ``workstream_config``; ``context_tokens`` is the most recent
        ``usage_events`` prompt size and ``context_window`` the model's
        window (the caller divides them for the occupancy ratio);
        ``child_count`` counts child workstreams.  New columns MUST keep
        appending at the tail — a full-arity unpack in session_routes
        consumes this exact tuple.
        """
        ...

    def prune_workstreams(self, retention_days: int = 90) -> tuple[int, int]:
        """Atomically remove orphaned + stale unnamed workstreams.

        Candidate predicates are rechecked while holding the same parent-row
        lock (or SQLite writer reservation) used by keyed conversation commits.
        Deletion releases all attachment references transactionally. Returns
        ``(orphans, stale)``.
        """
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

    def finalize_deferred_create(
        self,
        ws_id: str,
        fork_reservation_token: str,
        *,
        alias: str | None = None,
        config: dict[str, str] | None = None,
        node_id: str | None = None,
        override_reason: str = "local",
    ) -> bool:
        """Atomically apply prepublication writes to one reserved incarnation.

        The durable workstream row and private fork reservation must both
        match. Alias conflict or ownership loss returns ``False`` with no
        mutation. The private reservation is retained for exact cancellation
        rollback until lifecycle publication succeeds.
        """
        ...

    def publish_deferred_create(
        self,
        ws_id: str,
        fork_reservation_token: str,
    ) -> bool:
        """Publish exactly one reserved ``creating`` workstream.

        The state transition is an incarnation-checked compare-and-swap.  A
        missing row, mismatched token, or already-published row returns
        ``False`` without mutation.  The private token remains as the durable
        incarnation fence used by exact hard-delete; clone admission also
        requires ``state='creating'`` so the token is not a reusable fork
        capability after publication.
        """
        ...

    def get_workstream_reservation_token(self, ws_id: str) -> str:
        """Return the private durable incarnation token, or ``""``."""
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
        authoritative row. This is a raw internal read: it deliberately
        returns provisional ``state='creating'`` reservations. User-visible,
        open, export, and mutation surfaces must apply their lifecycle and
        authorization gates rather than treating every returned row as
        published.
        """
        ...

    def ensure_workstream_incarnation_snapshot(self, ws_id: str) -> dict[str, Any] | None:
        """Return the row plus a stable private incarnation token.

        The authoritative row read and creation of a token for legacy rows are
        one transaction.  Callers can therefore authorize this immutable
        snapshot and use its token for a later conditional mutation without a
        delete/re-register ABA becoming authorized by the old decision.
        Ordinary row/config reads must not expose the private token.
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
        """Create a structured memory record with a non-empty description."""
        ...

    def upsert_structured_memory(
        self,
        memory_id: str,
        name: str,
        description: str,
        mem_type: str | None,
        scope: str,
        scope_id: str,
        content: str,
        *,
        require_active_project: bool = False,
        acting_principal_id: str = "",
    ) -> tuple[dict[str, str], bool]:
        """Insert a structured memory, or update it in place on a
        ``(name, scope, scope_id)`` conflict.

        Atomic ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` — no
        IntegrityError round-trip, race-safe under concurrent saves of the same
        key. ``description`` must contain non-whitespace text on every insert
        or update. A ``mem_type`` of ``None`` means "unset": the column default
        is used on insert and the stored value is kept on conflict.

        Returns ``(row, was_update)`` (like Django's ``update_or_create``): a
        body-free summary row, and ``True`` when an existing row was updated rather
        than inserted.  Callers MUST supply a fresh unique ``memory_id`` — it is
        compared against the returned row's id to tell INSERT from UPDATE, so a
        reused id would report ``was_update=False`` on a real update.

        When ``acting_principal_id`` is supplied for project scope, the backend
        must resolve active project write access in the same transaction as the
        upsert. ``require_active_project`` retains the trusted internal path's
        active-project guard when there is no acting principal.

        Workstream-scoped writes similarly lock and verify their durable parent
        in the same transaction, so a deleted workstream cannot gain orphaned
        memory rows.
        """
        ...

    def get_structured_memory(self, memory_id: str) -> dict[str, str] | None:
        """Return structured memory dict or None."""
        ...

    def get_structured_memory_by_name(
        self,
        name: str,
        scope: str = "global",
        scope_id: str = "",
    ) -> dict[str, str] | None:
        """Lookup structured memory by (name, scope, scope_id). Returns dict or None."""
        ...

    def get_and_touch_structured_memory(self, memory_id: str) -> dict[str, str] | None:
        """Atomically return one full body while recording its access."""
        ...

    def get_and_touch_structured_memory_by_name(
        self,
        name: str,
        scope: str = "global",
        scope_id: str = "",
        *,
        acting_principal_id: str = "",
    ) -> dict[str, str] | None:
        """Atomically return one scoped full body while recording its access."""
        ...

    def update_structured_memory_description(
        self, memory_id: str, description: str
    ) -> dict[str, str] | None:
        """Atomically update an authored memory description and return the row."""
        ...

    def delete_structured_memory(
        self,
        name: str,
        scope: str = "global",
        scope_id: str = "",
    ) -> bool:
        """Delete a structured memory by (name, scope, scope_id). Returns True if existed."""
        ...

    def delete_structured_memory_returning(
        self,
        name: str,
        scope: str = "global",
        scope_id: str = "",
        *,
        acting_principal_id: str = "",
    ) -> dict[str, str] | None:
        """Atomically delete and return a memory selected by its scoped name."""
        ...

    def delete_structured_memory_by_id(self, memory_id: str) -> bool:
        """Delete a structured memory by its primary key. Returns True if existed."""
        ...

    def delete_structured_memory_by_id_returning(self, memory_id: str) -> dict[str, str] | None:
        """Atomically delete and return a memory selected by primary key."""
        ...

    def find_structured_memory_scopes(
        self,
        name: str,
        scopes: list[tuple[str, str]],
        *,
        acting_principal_id: str = "",
    ) -> list[tuple[str, str]]:
        """Return visible scope pairs containing ``name`` in one small query."""
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

    def list_visible_structured_memories(
        self,
        scopes: list[tuple[str, str]],
        mem_type: str = "",
        limit: int = 100,
        *,
        acting_principal_id: str = "",
    ) -> list[dict[str, str]]:
        """List memories matching ANY of the (scope, scope_id) pairs in *scopes*.

        Every pair is exact, including the canonical ``("global", "")`` pair.
        Single SQL query — replaces the per-scope fan-out pattern that issued
        one query per visible scope.
        """
        ...

    def search_visible_structured_memories(
        self,
        query: str,
        scopes: list[tuple[str, str]],
        mem_type: str = "",
        limit: int = 20,
        *,
        acting_principal_id: str = "",
    ) -> list[dict[str, str]]:
        """OR-of-terms search across memories visible under *scopes*.

        Single SQL query joining the scope OR-group with the term OR-group.
        Ranking is the caller's job (BM25 downstream).
        """
        ...

    def list_visible_memory_index_entries(
        self,
        scopes: list[tuple[str, str]],
        *,
        acting_principal_id: str = "",
    ) -> list[dict[str, str]]:
        """Return complete visible index metadata without memory bodies."""
        ...

    def get_memory_index_health_inputs(self) -> dict[str, list[dict[str, Any]]]:
        """Return live topology plus all index metadata in one read snapshot.

        Keys include index entries, workstreams, projects, memberships, users,
        roles, user-role assignments, and role overrides.
        The health calculator combines pre-rendered per-scope metrics from
        these rows; it never scans every memory once per visibility envelope.
        """
        ...

    def get_memory_index_snapshot(
        self,
        ws_id: str,
    ) -> dict[str, Any] | None:
        """Load the immutable index bound to one durable workstream row."""
        ...

    def acquire_memory_index_snapshot(
        self,
        ws_id: str,
        principal_id: str,
        *,
        commit_context: Callable[[dict[str, Any]], AbstractContextManager[None]] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically bind the first principal's complete index.

        Workstream validation, live project ACL resolution, the
        metadata read and first-writer insertion are one coherent database
        transaction. A missing, provisional, or deleted workstream returns
        ``None``. When a concrete candidate exists, ``commit_context`` is
        entered around the final commit, after all blocking reads/rendering.
        Its pre-yield phase may validate and raise, rolling back any newly
        inserted candidate; the backend commit occurs as the context body at
        yield. Its post-yield phase runs after commit and must remain
        deterministic publication — an exception there cannot roll back the
        already-committed snapshot.
        """
        ...

    def count_structured_memories(
        self,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        *,
        acting_principal_id: str = "",
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
        project_id: str | None = None,
        persona: str | None = None,
        fork_reservation_token: str = "",
    ) -> bool:
        """Create a workstreams row and report whether it was inserted.

        The ``workstreams.ws_id`` primary key is the authoritative live-ID
        reservation. An existing row returns ``False``; generated-ID callers
        may draw another ID while caller-selected IDs surface the collision.
        Hard deletion releases the ID for later reuse after removing the
        workstream and its owned state in the same transaction, unless a
        retained channel route still references it. Such references also
        return ``False`` to prevent redirecting a channel into a new incarnation.

        ``kind`` accepts a ``WorkstreamKind`` member or its raw string value
        (``"interactive"`` / ``"coordinator"``); the storage edge validates
        the value and rejects unknown kinds with ``ValueError``.
        ``parent_ws_id`` is non-NULL for children spawned by a coordinator;
        ``project_id`` is the attached project; ``persona`` is the slug the
        workstream was created with (display carrier — the snapshot lives in
        ``workstream_config``) — all normalized from the empty string to
        ``None`` at the storage edge.

        ``fork_reservation_token`` is private create-path plumbing. When
        non-empty, the backend stores it with the new row in the same
        transaction under :data:`FORK_RESERVATION_CONFIG_KEY`; a rejected
        duplicate must not alter the incumbent row's token. The token fences
        exact operations against delete/re-register races; it is not part of
        memory-index identity.
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

    def delete_stale_creating_reservations(
        self,
        kind: WorkstreamKind | str,
        cutoff: str,
        exclude_ws_ids: list[str],
        *,
        live_node_ids: list[str],
        local_node_id: str | None,
    ) -> list[str]:
        """Hard-delete abandoned provisional creates of *kind*.

        Eligible rows must still be ``state='creating'``, have
        ``updated < cutoff``, and not appear in ``exclude_ws_ids``. State, age
        and deletion are checked under one backend transaction/row lock. When
        the private incarnation token exists it is rechecked under that same
        lock. Legacy/corrupt tokenless reservations are also recoverable: the
        locked durable row itself is the incarnation fence, and backends emit
        a warning when reclaiming one. Implementations must use the ordinary
        complete-delete machinery so conversations, config, overrides and
        attachment refcounts are cleaned together.

        ``live_node_ids`` is required rather than optional: callers must skip
        this operation when service liveness cannot be established. Rows owned
        by a live peer are protected. ``local_node_id`` is the current
        process's service id and is deliberately exempt from that protection;
        after a restart the predecessor's rows carry the same stable id, while
        the current process's live reservations are protected by
        ``exclude_ws_ids`` plus the age cutoff.

        This is intentionally separate from
        :meth:`bulk_close_stale_orphans`. A provisional create was never
        advertised and must be deleted, never made reopenable as ``closed``.
        Returns the ids actually deleted.
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
        """Delete a workstream and owned state, releasing its ID for reuse."""
        ...

    def delete_workstream_if_fork_reserved(
        self,
        ws_id: str,
        fork_reservation_token: str,
    ) -> bool:
        """Delete only the durable incarnation carrying ``token``.

        The token check and complete workstream deletion are one transaction.
        It applies to provisional and published manager-created rows; a missing
        or replaced row returns ``False`` without mutation.
        """
        ...

    def list_orphan_conversations(self) -> list[dict[str, Any]]:
        """Conversation ws_ids with no ``workstreams`` row.

        One dict per orphan workstream — keys ``ws_id``, ``rows``, ``first``,
        ``last`` (ISO text timestamps), ``attachment_refs`` — ordered
        oldest-first.  Read-only; feeds the ``turnstone-admin
        orphan-conversations`` maintenance verb.
        """
        ...

    def delete_orphan_conversations(self, ws_ids: list[str]) -> dict[str, int]:
        """Purge conversation rows for the *ws_ids* that are STILL orphaned.

        Orphan-ness is enforced inside the DELETE itself (correlated
        ``NOT EXISTS`` against ``workstreams``) and refcounts are released
        from its ``RETURNING`` — a ws_id registered before or during the
        purge keeps both its rows and its refcounts.  Sweeps the purged
        ws_ids' ``workstream_config`` / ``workstream_overrides`` rows.
        Returns counts keyed ``workstreams``, ``rows``, ``released_refs``,
        ``skipped`` (distinct inputs not purged).
        """
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
        tail (the select currently ends with ``user_id, title, alias,
        project_id, persona`` — appended in that order, so positional
        fallbacks that index up to row[9] stay valid; new columns MUST
        keep appending at the tail).
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
        same-offset timestamps storage writes.  Provisional
        ``state='creating'`` rows are excluded until lifecycle publication.
        """
        ...

    # -- Conversation search ---------------------------------------------------

    def search_history(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        *,
        user_id: str | None = None,
        exclude_ws_id: str | None = None,
        exclude_after: int | None = None,
    ) -> list[Any]:
        """Search conversation history. Returns (timestamp, ws_id, role, content, tool_name).

        Conversation rows belonging to a provisional ``state='creating'``
        workstream are excluded until lifecycle publication.

        ``user_id`` scopes results by project tenancy: rows are dropped when
        their workstream sits in an existing PRIVATE project and *user_id* is
        neither the workstream creator, the project owner, nor a member.
        Everything else — no project link, dangling link, non-private project
        — stays visible (trusted-team default).  The SQL predicate mirrors
        ``WorkstreamProjectVisibility`` in ``core.auth`` (THE statement of the
        rule); ``tests/test_search_history_visibility.py`` pins the parity.
        ``None`` (default) applies no scoping — correct only for single-user
        lanes (local CLI); authenticated surfaces MUST pass the acting user.

        ``exclude_ws_id`` + ``exclude_after`` drop *exclude_ws_id*'s rows with
        ``id > exclude_after`` — the live-context exclusion for the
        model-facing recall tool (rows the model can already see; see
        ``HISTORY_CONTEXT_EXCLUSION_SQL``).  ``exclude_after=None`` with an
        ``exclude_ws_id`` set excludes the entire workstream (never
        compacted → all live).  Both applied in SQL so pagination stays
        honest.
        """
        ...

    def search_history_recent(self, limit: int = 20, *, user_id: str | None = None) -> list[Any]:
        """Return most recent conversation messages.

        ``user_id`` scopes rows by project tenancy exactly as in
        :meth:`search_history`; ``None`` applies no tenancy scoping.  Creating
        workstreams remain excluded for every caller.
        """
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

    def count_users(self) -> int:
        """Return the count of users.

        Cheaper than ``list_users`` when the caller only needs to know
        whether at least one user exists (e.g. the OIDC handlers'
        "setup complete?" gate).
        """
        ...

    def find_existing_usernames(self, candidates: list[str]) -> set[str]:
        """Return the subset of *candidates* already present in ``users.username``.

        Single ``WHERE username IN (...)`` query — replaces the
        per-candidate ``get_user_by_username`` loop on the OIDC
        username-derivation path.  Empty input returns ``set()``.
        """
        ...

    def delete_user(self, user_id: str) -> bool:
        """Delete a user and their dependent rows. Return whether the user existed.

        A missing user is side-effect free, including when malformed historical
        dependent rows still reference the absent id.
        """
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

    def create_oidc_user(
        self,
        user_id: str,
        username: str,
        display_name: str,
        password_hash: str,
        issuer: str,
        subject: str,
        email: str,
        oid: str = "",
        tid: str = "",
    ) -> None:
        """Atomically create a user row and bind their OIDC identity.

        Both inserts run in a single transaction so concurrent callbacks for
        the same ``(issuer, subject)`` pair (or username TOCTOU between
        :meth:`get_user_by_username` and this call) cannot leave orphan
        ``users`` rows or orphan ``user_role`` rows pointing at a user that
        was rolled back.

        Raises :class:`StorageConflictError` on UNIQUE / PK violations
        (username already taken, or ``(issuer, subject)`` already linked)
        so callers don't have to inspect dialect-specific ``IntegrityError``
        details.  The user / identity rows are rolled back together on any
        conflict.
        """
        ...

    def create_oidc_identity(self, issuer: str, subject: str, user_id: str, email: str) -> None:
        """Link an OIDC subject to a turnstone user. No-op if exists."""
        ...

    def get_oidc_identity(self, issuer: str, subject: str) -> OIDCIdentity | None:
        """Lookup turnstone user by OIDC issuer+subject. Returns dict or None."""
        ...

    def update_oidc_identity_login(
        self, issuer: str, subject: str, oid: str = "", tid: str = ""
    ) -> bool:
        """Update last_login; backfill oid/tid when provided. Returns True if row existed."""
        ...

    def list_oidc_identities_for_user(self, user_id: str) -> list[OIDCIdentity]:
        """List all OIDC identities linked to a turnstone user."""
        ...

    def delete_oidc_identity(self, issuer: str, subject: str) -> bool:
        """Remove an OIDC identity link. Returns True if existed."""
        ...

    # -- OIDC user credential (single-credential MCP minting, #551) -------------

    def upsert_oidc_user_credential(
        self, user_id: str, issuer: str, *, refresh_token_ct: bytes
    ) -> None:
        """Create or replace the user's captured IdP refresh token.

        Replace-on-conflict: a fresh login must overwrite a stale or
        revoked credential.  ``created`` is preserved on replace;
        ``last_refreshed`` is reset to now either way.
        """
        ...

    def get_oidc_user_credential(self, user_id: str, issuer: str) -> OIDCUserCredential | None:
        """Return the captured credential row or None."""
        ...

    def update_oidc_user_credential_refresh(
        self, user_id: str, issuer: str, *, refresh_token_ct: bytes
    ) -> bool:
        """Rotation write-back after a redemption returned a new refresh token.

        Both verified grant legs rotate (Entra returns a new RT per
        redemption; Keycloak rotates on the refresh grant), so the mint
        path MUST persist the newest token every time.  Returns True when
        a row was updated.
        """
        ...

    def delete_oidc_user_credential(self, user_id: str, issuer: str) -> bool:
        """Remove the captured credential (logout-all / admin revoke). Returns True if existed."""
        ...

    # -- OIDC pending state ----------------------------------------------------

    def create_oidc_pending_state(
        self, state: str, nonce: str, code_verifier: str, audience: str
    ) -> None:
        """Store OIDC authorization flow state for callback validation."""
        ...

    def pop_oidc_pending_state(
        self, state: str, max_age_seconds: int = 300
    ) -> OIDCPendingState | None:
        """Fetch and delete pending state atomically. Returns None if expired or missing."""
        ...

    def cleanup_expired_oidc_states(self, max_age_seconds: int = 300) -> int:
        """Delete expired pending states. Returns count of deleted rows."""
        ...

    # -- Channel routing -------------------------------------------------------

    def create_channel_route(
        self,
        channel_type: str,
        channel_id: str,
        ws_id: str,
        node_id: str = "",
        *,
        channel_user_id: str = "",
    ) -> bool:
        """Map a channel/thread and its external owner. Return whether inserted."""
        ...

    def replace_channel_route(
        self, channel_type: str, channel_id: str, expected_ws_id: str, ws_id: str
    ) -> bool:
        """Atomically replace the expected workstream, preserving owner and creation time."""
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

    def delete_channel_route(
        self, channel_type: str, channel_id: str, *, expected_ws_id: str | None = None
    ) -> bool:
        """Remove a route, optionally only if its workstream still matches. Return whether removed."""
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
        persona: str = "",
        project_id: str = "",
        timezone: str = "UTC",
    ) -> None:
        """Create a scheduled task. No-op if task_id already exists.

        ``persona`` (slug) and ``project_id`` are stamped onto the workstream
        each firing creates; empty = kind-default persona / no project.
        ``timezone`` is the IANA zone a cron schedule is evaluated in.
        """
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

    def is_watch_active(self, watch_id: str) -> bool:
        """Return True iff the watch exists and its ``active`` flag is set.

        Single-column read for hot paths that only need the active flag
        (e.g. the watch-dispatch ``valid_until`` predicate) without
        paying for the full row marshal that ``get_watch`` does.
        Returns ``False`` if the watch row is missing.
        """
        ...

    def list_watches_for_ws(self, ws_id: str) -> list[dict[str, Any]]:
        """Return active watches for a workstream, ordered by created DESC."""
        ...

    def find_watch_by_name(self, ws_id: str, name_or_prefix: str) -> dict[str, Any] | None:
        """Return a watch in ``ws_id`` whose ``name`` matches
        ``name_or_prefix`` exactly, or whose ``watch_id`` starts with it.

        Unlike :meth:`list_watches_for_ws` this DOES NOT filter on the
        ``active`` flag — callers can inspect ``row["active"]`` to
        distinguish a still-running watch from one that fired and
        auto-cancelled.  Returns ``None`` if no match.

        When multiple rows match, prefers active rows over inactive
        ones, then most-recently-created.  Without the active
        preference, a recreated-after-completion name would let the
        older inactive row shadow the new active one in the cancel
        path.
        """
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

    # -- Cross-process notifications -------------------------------------------

    def notify(self, channel: str, payload: str = "") -> None:
        """Broadcast a wake-up on ``channel`` to any listening process.

        Payloads are signal-only — a JSON-encoded string identifying
        which rows to re-read, capped well below Postgres's 8 KiB
        ``NOTIFY`` payload limit.  Full event content is NOT delivered
        this way; consumers reconcile by reading the relevant table on
        wake-up.  Safe to call from any thread.
        """
        ...

    def listen(self, channels: Iterable[str]) -> AbstractContextManager[NotifyStream]:
        """Subscribe to one or more channels for cross-process wake-ups.

        Returns a context manager wrapping a :class:`NotifyStream` the
        caller drains via :meth:`NotifyStream.poll`.  PostgreSQL holds a
        dedicated session-mode connection for the lifetime of the
        context (incompatible with ``pgbouncer`` transaction pooling —
        see :class:`PostgreSQLBackend.listen` for the bypass-URL config).
        SQLite emits a synthetic-sweep wake on its own cadence (see
        ``_SQLITE_NOTIFY_SWEEP_INTERVAL``) per subscribed channel so
        consumer code is identical across backends.
        """
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
        """Assign a role to a user. No-op if already assigned.

        Raises ``ValueError`` when the user or role does not exist; assignment
        never creates orphan authority rows.
        """
        ...

    def unassign_role(self, user_id: str, role_id: str) -> bool:
        """Unassign a role from a user. Returns True if existed."""
        ...

    def list_user_roles(self, user_id: str) -> list[dict[str, Any]]:
        """List roles assigned to a user (joins user_roles with roles)."""
        ...

    def replace_oidc_roles(
        self, user_id: str, desired_role_ids: set[str]
    ) -> tuple[set[str], set[str]]:
        """Atomically reconcile ``user_roles`` for OIDC-assigned rows.

        Reads every existing row for ``user_id`` and partitions them by
        ``assigned_by``:
        - rows where ``assigned_by == "oidc"`` are the reconciliation set
        - rows where ``assigned_by != "oidc"`` are *blocked* — manual
          assignments (``admin-ui``) and the ``oidc-default`` fallback are
          never touched, and a desired role already held under any
          non-``"oidc"`` source is dropped from the desired set rather
          than overwriting it (the table PK is ``(user_id, role_id)``
          only, so an insert would otherwise PK-conflict).
        Inserts each role in (``desired_role_ids - blocked``) not already
        held under ``"oidc"``; deletes each currently-OIDC-held role that
        is no longer desired.  All inside a single transaction.

        Returns ``(added, removed)`` — the role ids that actually
        transitioned in each direction so the caller can emit the same
        per-role audit log lines the per-role loop produced.

        Raises ``ValueError`` when ``user_id`` or a desired role does not
        exist; reconciliation never creates orphan authority rows.
        """
        ...

    def get_user_permissions(self, user_id: str) -> set[str]:
        """Return the union of all permissions from the user's assigned roles.

        For builtin roles, applies any rows in ``role_permission_overrides``
        on top of ``roles.permissions`` as ``baseline ∪ grants − revokes``.
        """
        ...

    def users_with_permission(
        self,
        permission: str,
        *,
        exclude_role_id: str | None = None,
    ) -> set[str]:
        """Return ``user_id``s whose effective perms include ``permission``.

        Walks every ``(user, assigned_role)`` pair in two bulk queries
        (one over ``user_roles ⋈ roles``, one over
        ``role_permission_overrides`` for the builtin role ids in the
        first query's result) instead of N round-trips, then folds the
        overlay in-process.  ``exclude_role_id``, when set, ignores any
        contribution from that role — used by the lockout guard to
        answer "would anyone still hold ``admin.roles`` via SOME OTHER
        role if we modified this one?" without first having to apply
        the proposed override.
        """
        ...

    def list_role_overrides(self, role_id: str) -> list[dict[str, str]]:
        """Return override rows for ``role_id`` (action in {'grant','revoke'})."""
        ...

    def set_role_overrides(
        self,
        role_id: str,
        grants: set[str],
        revokes: set[str],
        created_by: str = "",
    ) -> None:
        """Transactionally replace the override set for ``role_id``.

        Deletes any existing rows for the role and inserts one row per
        (permission, action) in ``grants`` / ``revokes``.  Empty inputs
        clear all overrides (equivalent to ``clear_role_overrides``).
        ``grants`` and ``revokes`` MUST be disjoint — the caller is
        responsible for ensuring no permission appears in both.
        """
        ...

    def clear_role_overrides(self, role_id: str) -> None:
        """Delete every override row for ``role_id`` (reset-to-default)."""
        ...

    def effective_role_permissions(self, role_id: str) -> dict[str, list[str]]:
        """Return ``{'baseline': [...], 'grants': [...], 'revokes': [...],
        'effective': [...]}`` for a single role, with overrides applied.
        Each list is sorted for stable rendering.
        """
        ...

    def effective_role_permissions_bulk(
        self, role_ids: list[str]
    ) -> dict[str, dict[str, list[str]]]:
        """Bulk variant of :meth:`effective_role_permissions`.

        Returns ``{role_id: {baseline, grants, revokes, effective}}``
        for every role_id in ``role_ids``.  Issues at most two queries
        regardless of list size (one over ``roles``, one IN-filter over
        ``role_permission_overrides``).  Missing role_ids are omitted
        from the result rather than mapped to an empty dict — caller
        can detect absence directly.
        """
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
        notify_on_complete: str = "[]",
        enabled: bool = True,
        allowed_tools: str = "[]",
        skill_license: str = "",
        compatibility: str = "",
        priority: int = 0,
        kind: str = "any",
        paths: str = "[]",
        hidden_from_menu: bool = False,
        arguments: str = "[]",
        argument_hint: str = "",
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

    def unlock_skill(self, template_id: str, snapshot: str, changed_by: str) -> int | None:
        """Atomically snapshot a readonly skill and flip ``readonly=False``.

        Writes ``snapshot`` into ``skill_versions`` with the next sequential
        version number, then sets ``readonly=False`` on the template row, all
        in a single transaction so concurrent updates can't produce
        ``(skill_id, version)`` collisions or a snapshot whose state is out of
        sync with the row at the moment readonly is flipped.

        Returns the assigned version number, or ``None`` if the template row
        does not exist. ``readonly`` is intentionally absent from
        :data:`SKILL_MUTABLE` — this dedicated writer is the only path for
        flipping it (matching the ``set_mcp_oauth_client_secret_ct`` pattern).
        """
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
        ``kind`` column is in the supplied list.  After the SkillKind
        enforcement flatten (#557), ``kind`` is passive audience metadata
        rather than a runtime visibility gate — the model-tool ``find``
        path no longer threads ``kinds=`` by default and supplies it only
        when the caller opts in via the tool's ``kind`` argument.  The
        parameter remains available for admin filtering and explicit
        scope narrowing.  ``None`` means no kind filter — all rows
        regardless of kind.
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
        Row shape and raw lifecycle semantics match ``get_workstream`` (same
        projection), including provisional ``state='creating'`` rows.

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
        user_decision: str = "pending",
        resolver_principal_id: str = "",
        execution_principal_id: str = "",
    ) -> None:
        """Record an intent validation verdict.

        ``user_decision`` defaults to ``"pending"`` rather than ``""``
        so an audit reader can distinguish "in-flight" rows from
        legacy pre-fix rows (which carry ``""`` from the column's
        server_default and indicate "convention not yet established
        when this row was written"). Resolution writers
        (:meth:`update_intent_verdict`) later overwrite the field with
        ``"approved"`` / ``"denied"`` / ``"timeout"`` (user-driven) or
        ``"policy"`` / ``"blanket"`` / ``"auto_approve_tools"``
        (auto-approve reason, mirroring :class:`AutoApproveReason`).
        Rows whose verdict landed only after a newer turn replaced the
        judge generation are written directly with ``"superseded"`` —
        no decision was ever taken on that verdict (its call's gate
        resolved before the judge finished); see
        ``SessionUIBase.on_superseded_intent_verdict``.
        """
        ...

    def upsert_intent_verdict(
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
        user_decision: str = "pending",
        resolver_principal_id: str = "",
        execution_principal_id: str = "",
    ) -> None:
        """INSERT a verdict row, or UPDATE the judge-output fields on conflict.

        Async LLM judge verdicts with ``tier="llm_fallback"`` deliberately
        reuse the heuristic verdict's ``verdict_id`` so the row gets
        "upgraded in place" from heuristic → fallback when the LLM tier
        doesn't return a real verdict (timeout / cancelled / no-content).
        A plain INSERT collides on ``intent_verdicts_pkey``; this method
        ``ON CONFLICT (verdict_id) DO UPDATE`` updates only the columns
        that genuinely change between the two tiers:

        - ``tier`` (the upgrade itself)
        - ``reasoning`` (gets " (LLM judge did not return a verdict)" appended)
        - ``judge_model`` (heuristic carries "", fallback carries the model)

        Every other column is EXCLUDED from the on-conflict SET clause:

        - Identity columns (``verdict_id``, ``ws_id``, ``call_id``,
          ``func_name``, ``func_args``) — already the same row.
        - Carried-verbatim columns (``intent_summary``, ``risk_level``,
          ``confidence``, ``recommendation``, ``evidence``, ``latency_ms``) —
          the fallback copies them from the heuristic verdict; updating
          would be a no-op.
        - ``user_decision`` — LOAD-BEARING exclusion.  ``IntentVerdict.to_dict()``
          doesn't project it, so a fallback verdict reaching this layer
          defaults the kwarg to ``"pending"``.  If the operator already
          resolved the approval between heuristic INSERT and fallback
          fire, the row's ``user_decision`` was already updated to
          ``"approved"``/``"denied"``/``"timeout"`` (or stamped to an
          auto-approve reason at heuristic-INSERT time).  Clobbering it
          back to ``"pending"`` would undo that.
        - ``created`` — preserve the original timestamp.

        Used by :meth:`SessionUIBase._persist_intent_verdict` for every
        async LLM-tier delivery; the synchronous heuristic-bulk path
        (:meth:`create_intent_verdicts_bulk`) inserts with per-row
        ``ON CONFLICT DO NOTHING`` instead — its UUIDs are freshly
        generated per turn, but the daemon can race a fallback UPSERT
        of one of those same IDs in ahead of the bulk write.
        """
        ...

    def create_intent_verdicts_bulk(self, verdicts: list[dict[str, Any]]) -> None:
        """Insert many intent_verdict rows in one transaction.

        Each dict mirrors :meth:`create_intent_verdict`'s keyword args
        (``verdict_id`` / ``ws_id`` / ``call_id`` / ``func_name`` /
        ``func_args`` / ``intent_summary`` / ``risk_level`` /
        ``confidence`` / ``recommendation`` / ``reasoning`` / ``evidence`` /
        ``tier`` / ``judge_model`` / ``latency_ms`` /
        ``user_decision``). ``user_decision`` defaults to ``"pending"``
        when absent — see :meth:`create_intent_verdict` for the
        vocabulary. Used by the synchronous heuristic-verdict
        persistence loop in ``approve_tools`` so a tool-heavy turn
        doesn't pay N×commit latency before the approval prompt renders.

        Inserts ``ON CONFLICT (verdict_id) DO NOTHING``: the async judge
        daemon's first delivery can UPSERT a fallback row — which reuses
        a heuristic ``verdict_id`` from this very batch — before the
        bulk write runs.  Aborting the whole statement on that collision
        (plain-INSERT behavior) silently discarded every other row in
        the batch; skipping just the colliding row keeps the rest AND
        preserves the daemon's ``llm_fallback`` tier upgrade rather
        than regressing it to the heuristic stamp.
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
        *,
        tier: str = "heuristic",
        reasoning: str = "",
        judge_model: str = "",
        latency_ms: int = 0,
        confidence: float = 0.0,
    ) -> None:
        """Record an output guard assessment.

        ``tier`` is ``"heuristic"`` (regex stage, default), ``"llm"`` (the
        judge's own successful verdict, issue #560 mitigation #1), or
        ``"llm_error"`` (the judge ran but failed — audit-only, excluded
        from the replay display merge; ``reasoning`` carries the error).
        One row per ``(call_id, tier)`` so a single tool call can produce
        up to two rows; mirrors the ``intent_verdicts`` table's row model.
        ``reasoning`` / ``judge_model`` / ``latency_ms`` / ``confidence``
        are LLM-tier fields and stay empty / zero on heuristic rows.
        """
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
        auth_type: str = "static",
        oauth_client_id: str | None = None,
        oauth_client_secret_ct: bytes | None = None,
        oauth_scopes: str | None = None,
        oauth_audience: str | None = None,
        oauth_registration_mode: str | None = None,
        oauth_authorization_server_url: str | None = None,
        oauth_as_issuer_cached: str | None = None,
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

    # -- MCP OAuth: client-secret + per-(user, server) tokens ------------------
    #
    # ``oauth_client_secret_ct`` is intentionally absent from
    # ``MCP_SERVER_MUTABLE`` (see ``_utils.py``).  It has its own dedicated
    # writer so the encrypt/None-to-clear semantics live in one place — see
    # ``set_mcp_oauth_client_secret_ct`` below.

    def set_mcp_oauth_client_secret_ct(self, server_id: str, secret_ct: bytes | None) -> bool:
        """Update only the encrypted OAuth client-secret column.

        Returns True when a row was updated. ``None`` clears the column.
        Bypasses ``MCP_SERVER_MUTABLE`` deliberately.
        """
        ...

    def create_mcp_user_token(
        self,
        user_id: str,
        server_name: str,
        *,
        access_token_ct: bytes,
        refresh_token_ct: bytes | None,
        expires_at: str | None,
        scopes: str | None,
        as_issuer: str,
        audience: str,
    ) -> None:
        """Insert a new per-(user, server) token row. No-op on conflict."""
        ...

    def get_mcp_user_token(self, user_id: str, server_name: str) -> MCPUserToken | None:
        """Return the per-(user, server) token row or None."""
        ...

    def update_mcp_user_token_after_refresh(
        self,
        user_id: str,
        server_name: str,
        *,
        access_token_ct: bytes,
        refresh_token_ct: bytes | None,
        expires_at: str | None,
    ) -> bool:
        """Rewrite token columns + ``last_refreshed`` after an AS refresh.

        Preserves columns this method does not rewrite (``scopes``,
        ``as_issuer``, ``audience``, ``created``). Returns True when a
        row was updated.
        """
        ...

    def delete_mcp_user_token(self, user_id: str, server_name: str) -> bool:
        """Delete the per-(user, server) token row. Returns True if existed."""
        ...

    def list_mcp_user_token_metadata_by_user(self, user_id: str) -> list[MCPUserTokenMetadataRow]:
        """Return non-secret metadata for every token row owned by ``user_id``,
        ordered by ``created`` ASC.

        Empty list when the user has no rows. Ciphertext columns are
        intentionally NOT loaded — the projection runs at the SQL boundary
        so the LargeBinary blobs never cross the wire for the list-view
        path. ``MCPTokenStore`` re-types the rows as
        ``MCPUserTokenMetadata`` (same field shape) for the settings UI.
        """
        ...

    def list_mcp_user_token_reconcile_targets(self) -> list[tuple[str, str, str | None]]:
        """Return ``(user_id, server_name, last_exercised_iso)`` for every MCP
        user-token row — the drive set for the background freshness sweep.

        ``last_exercised = COALESCE(last_refreshed, created)`` is the last time
        the grant's refresh token was exercised; the sweep force-refreshes once
        it exceeds the configured keepalive window, so a provider that ages out
        an *idle* refresh token can't expire one between a user's real sessions.
        Deliberately UNFILTERED by ``expires_at``: an expired access token backed
        by a live refresh token is still a consented, reconcilable grant. Only
        The storage query joins ``mcp_servers`` and keeps only
        ``auth_type='oauth_user'`` rows. Other auth paths also use the token
        table as a mint cache, but they are not refresh-grant sweep targets.
        Ciphertext columns are never touched — only identity + timestamp cross
        the wire.
        """
        ...

    def delete_mcp_oauth_rows_by_server_name(self, server_name: str) -> int:
        """Purge per-(user, server) tokens and pending OAuth states for *server_name*.

        Used when the operator renames or deletes an MCP server row to
        prevent old user tokens from rebinding to a freshly-created
        server with the same ``name``. Returns the total number of rows
        deleted across both tables.

        Both ``mcp_user_tokens`` and ``mcp_oauth_pending`` are keyed on
        the mutable ``server_name`` rather than the immutable
        ``server_id``; until those tables migrate to a server_id FK with
        ON DELETE CASCADE (a future schema migration), explicit purge on
        rename/delete is the only safe path.
        """
        ...

    def get_mcp_oauth_client_secret_ct(self, server_id: str) -> bytes | None:
        """Return the encrypted OAuth client secret column or None.

        Mirror of :meth:`set_mcp_oauth_client_secret_ct` for the read path.
        Returns ``None`` when the row does not exist or the column is NULL.
        """
        ...

    # -- MCP OAuth pending state (per-(user, server) flow) ---------------------

    def create_mcp_oauth_pending_state(
        self,
        state: str,
        user_id: str,
        server_name: str,
        code_verifier: str,
        return_url: str,
    ) -> None:
        """Insert a pending MCP OAuth flow row for callback validation."""
        ...

    def pop_mcp_oauth_pending_state(
        self, state: str, max_age_seconds: int = 600
    ) -> MCPOAuthPendingState | None:
        """Atomically fetch+delete a pending MCP OAuth row.

        Returns ``None`` when the row is missing or older than
        ``max_age_seconds``.
        """
        ...

    def cleanup_expired_mcp_oauth_pending_states(self, max_age_seconds: int = 600) -> int:
        """Bulk-delete expired pending MCP OAuth rows. Returns count deleted."""
        ...

    # -- MCP pending-consent (Phase 9; deferred-consent persistence) ----------

    def upsert_mcp_pending_consent(
        self,
        user_id: str,
        server_name: str,
        error_code: str,
        scopes_required: str | None,
        now_iso: str,
    ) -> None:
        """Insert or refresh a deferred-consent record for ``(user, server)``.

        On insert: ``first_seen_at = last_seen_at = now_iso``,
        ``occurrence_count = 1``.  On conflict (existing row for the
        same composite PK): rewrites ``error_code``, ``scopes_required``,
        ``last_seen_at`` to the current values; bumps
        ``occurrence_count`` by 1.  Preserves ``first_seen_at`` so the
        dashboard can show how long the deferred-consent need has been
        pending.
        """
        ...

    def list_mcp_pending_consent_by_user(self, user_id: str) -> list[MCPPendingConsentRow]:
        """Return all deferred-consent records for ``user_id``.

        Ordered by ``last_seen_at`` DESC.  Empty list when the user has
        none.  Used by the dashboard badge endpoint to render the
        servers-need-consent list.
        """
        ...

    def delete_mcp_pending_consent(self, user_id: str, server_name: str) -> bool:
        """Delete the pending-consent row for ``(user, server)``. Returns True if existed.

        Called automatically by the OAuth callback handler when consent
        completes, and manually via the user-facing DELETE endpoint.
        """
        ...

    def delete_all_mcp_pending_consent_by_user(self, user_id: str) -> int:
        """Bulk-delete every pending-consent row for ``user_id``. Returns count.

        Used by the manual "dismiss all" endpoint from the settings
        modal.
        """
        ...

    def count_mcp_consented_users_by_server(self, server_name: str) -> int:
        """Distinct-user count of non-expired tokens for ``server_name``.

        ``expires_at IS NULL`` is treated as non-expired (refresh-only
        tokens with no advertised expiry).  Used by the admin status
        indicator to show "N users consented" per MCP server row.
        """
        ...

    def count_mcp_consented_users_grouped_by_server(self) -> dict[str, int]:
        """Bulk distinct-user count of non-expired tokens, grouped by server.

        Single round-trip variant of
        :meth:`count_mcp_consented_users_by_server` for the admin list
        handler — replaces the N-call loop that issued one query per
        server with one ``GROUP BY`` query returning ``{server_name:
        count}`` for every server that has at least one non-expired
        token.  Servers with zero consented users are absent from the
        result; callers should ``dict.get(name, 0)`` rather than
        indexing.
        """
        ...

    def any_user_scoped_mcp_servers(self) -> bool:
        """Install-level gate for the pending-consent badge (issue #551).

        Returns True iff at least one ``mcp_servers`` row is pool-backed
        (``auth_type`` in ``oauth_user`` / ``oauth_obo``) — both write
        ``mcp_pending_consent`` rows on a non-interactive dispatch failure, so
        an oauth_obo-only install must NOT short-circuit the badge to
        ``{pending: 0}`` (that would hide the re-login affordance). Used to
        short-circuit the pending-consent badge endpoint on local-auth installs
        with no pool-backed MCP servers, so those code paths exercise zero new
        storage queries.
        """
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
        surface_persisted_reasoning: bool = True,
        replay_reasoning_to_model: bool = False,
        auth_mode: str = "static",
        obo_audience: str = "",
        obo_scopes: str = "",
        max_concurrency: int = 0,
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

    def update_model_definition(
        self, definition_id: str, *, expected_capabilities: Any = ..., **fields: Any
    ) -> bool:
        """Update specified fields on a model definition.

        Returns True when a row was updated. When ``expected_capabilities``
        is passed, the update applies only while the row's ``capabilities``
        column still equals it: a concurrent write turns the call into a
        False miss the caller re-reads and re-merges onto, never a silent
        last-writer-wins revert. Omitted, the update is unconditional and
        False means the row was not found.
        """
        ...

    def delete_model_definition(self, definition_id: str) -> bool:
        """Delete a model definition. Returns True if existed."""
        ...

    # -- Projects --------------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        name: str,
        owner_id: str,
        visibility: str = "private",
        state: str = "active",
        parent_project_id: str | None = None,
    ) -> None:
        """Create a project. No-op if project_id already exists."""
        ...

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        """Return project dict or None."""
        ...

    def list_projects_for_user(
        self, user_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Return projects the user owns, is a member of, or that are public.

        Ordered by name; excludes archived projects unless ``include_archived``.
        """
        ...

    def update_project(self, project_id: str, **fields: Any) -> bool:
        """Update mutable fields on a project. Returns True if a row changed."""
        ...

    def delete_project(self, project_id: str) -> bool:
        """Delete a project and its membership rows. Returns True if existed."""
        ...

    def add_project_member(self, project_id: str, user_id: str) -> None:
        """Add a member to a project. No-op if already a member."""
        ...

    def remove_project_member(self, project_id: str, user_id: str) -> bool:
        """Remove a member. Returns True if the membership existed."""
        ...

    def list_project_members(self, project_id: str) -> list[str]:
        """Return the user_ids of a project's members, ordered."""
        ...

    def is_project_member(self, project_id: str, user_id: str) -> bool:
        """Return True if user_id is a member of project_id."""
        ...

    def list_workstreams_for_project(self, project_id: str) -> list[dict[str, Any]]:
        """Return the project's workstreams (ws_id, name, title, state, kind,
        updated, node_id, user_id), newest-updated first."""
        ...

    def list_project_attachments(self, project_id: str) -> list[dict[str, Any]]:
        """Committed attachments referenced by any turn in the project's
        workstreams — metadata only, each with the first referencing ws_id
        (content serving is ws-scoped)."""
        ...

    # -- Personas ---------------------------------------------------------------
    # Template shelf only: workstreams snapshot the persona at creation and
    # never read this table again, so edits/archives don't touch existing
    # workstreams.  No delete method — archive via update_persona(enabled=False).
    # Dict shape: ``tool_allowlist`` is ``None`` (unrestricted) or ``list[str]``
    # (``[]`` = hard empty); ``applies_to_kinds`` is ``list[str]``;
    # mcp_enabled/memory_enabled/is_default/enabled are ``bool``.

    def list_personas(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        """Return personas ordered by name; enabled-only unless asked."""
        ...

    def get_persona(self, persona_id: str) -> dict[str, Any] | None:
        """Return persona dict or None."""
        ...

    def get_persona_by_name(self, name: str) -> dict[str, Any] | None:
        """Return persona dict by slug or None."""
        ...

    def get_default_persona(self, kind: str) -> dict[str, Any] | None:
        """Return the enabled default persona for a workstream kind, or None
        (pre-seed database — callers fall back to unstamped legacy creation)."""
        ...

    def create_persona(self, persona: dict[str, Any]) -> None:
        """Create a persona.  Requires ``persona_id`` and ``name``; accepts the
        Python-typed dict shape above (JSON serialization is internal).
        Raises ValueError on: missing ``persona_id``/``name``, duplicate
        name, invalid ``applies_to_kinds``, oversized fields (caps live in
        ``_utils.serialize_persona_fields``), or an ``is_default`` persona
        that is multi-kind or disabled."""
        ...

    def update_persona(self, persona_id: str, **fields: Any) -> bool:
        """Update PERSONA_MUTABLE fields.  Returns True only when the persona
        exists AND at least one mutable field was supplied — a no-op call on
        a real row returns False (check existence separately if you need to
        distinguish "not found" from "nothing to update").

        Invariants (raise ValueError): a default persona cannot be archived,
        cannot drop its ``is_default`` flag directly (flip the flag on the
        successor instead — that clears the old default atomically), and
        cannot change ``applies_to_kinds``; setting ``is_default=True`` clears
        the flag on other personas sharing a kind."""
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

    # -- Cross-node serialization ----------------------------------------------

    def acquire_advisory_lock_sync(self, key_text: str) -> AbstractContextManager[None]:
        """Acquire a backend-specific advisory lock for the duration of the context.

        PostgreSQL: spins on ``pg_try_advisory_xact_lock(hashtext(key_text))``
        with a short backoff between attempts. Each probe runs in a fresh
        transaction, and waiting probes return their connection to the pool
        between attempts; only the actual lock holder retains a connection
        for the body. The lock auto-releases on transaction end (commit /
        rollback). Raises ``TimeoutError`` if no probe succeeds within the
        backend-defined deadline. SQLite: returns ``contextlib.nullcontext``
        (single-node deployments rely on in-process ``asyncio.Lock`` for
        serialization).

        Caller is expected to wrap the resulting context manager in
        ``asyncio.to_thread`` (or a dedicated executor) when invoking from
        an async context — the underlying SQLAlchemy hops are blocking.
        """
        ...

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Release resources (connection pool, engine, etc.)."""
        ...
