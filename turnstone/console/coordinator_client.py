"""In-process helper for coordinator workstream tool execs.

A coordinator's ChatSession runs on a worker thread inside
``turnstone-console`` and drives its child workstreams through two
channels:

- **Mutating ops** (``spawn``, ``send``, ``approve``, ``cancel``,
  ``close``, ``delete``) go through the console's own HTTP routing
  proxy (``/v1/api/route/*``).  Sending over HTTP keeps the normal
  middleware stack (auth, rate limit, route pinning) in the loop —
  the coordinator gets no special privileges the proxy can't see.
- **Read ops** (``list_children``, ``inspect``) hit the shared
  storage backend directly because the routing proxy doesn't cover
  list/inspect paths today.  Storage is same-process and same-DB, so
  this is as safe as any other read inside the console.

The client is **synchronous by design** — coordinator tool execs run
on the ChatSession's worker thread, not on the event loop — so it uses
``httpx.Client`` rather than the async client.  A per-session
:class:`CoordinatorTokenManager` mints short-lived console-audience
JWTs carrying the real user's identity + scopes.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt
from turnstone.core.log import get_logger
from turnstone.core.workstream import WorkstreamKind

# ---------------------------------------------------------------------------
# wait_for_workstream constants — module-level so ``turnstone.core.session``
# can import them without reading a class internal (see #12).  The
# ``CoordinatorClient`` ClassVar aliases below are kept so external callers
# that still import via the class surface don't break.
# ---------------------------------------------------------------------------

# Real terminal states for the wait_for_workstream tool — these drive the
# any/all completion condition.  A workstream in one of these states has
# actually finished work the coordinator can observe.
WAIT_REAL_TERMINAL_STATES: frozenset[str] = frozenset({"idle", "error", "closed", "deleted"})

# Reportable terminal states — superset of the real ones, also includes the
# ``denied`` short-circuit shape returned for foreign / missing ws_ids.
# Used inside ``wait_for_workstream`` to decide when ``mode='any'`` on a
# pure-denied list should short-circuit with ``complete=False`` (no real
# work to wait for) and when ``mode='all'`` has fully settled.  NOT used
# for the ``mode='any'`` real-terminal completion condition and NOT used
# by the resolved-count summary (which counts only real terminals —
# ``denied`` is a rejection, not a resolution).  A single typo'd /
# foreign id shouldn't satisfy ``mode="any"`` and let the model declare
# a wait complete while every real child is still running.
WAIT_TERMINAL_STATES: frozenset[str] = WAIT_REAL_TERMINAL_STATES | frozenset({"denied"})

# Hard cap on ws_ids per call.  Polling happens once per ws_id per tick, so a
# runaway list would amplify storage load without giving the model anything
# useful — coordinators rarely fan out past a handful of children at once.
WAIT_MAX_WS_IDS: int = 32

# Cap on the total wait so a stuck child can't pin a coordinator worker
# thread indefinitely.  Coordinators that need a longer wait call
# wait_for_workstream again with the same ws_ids — each call re-arms freshly.
WAIT_MAX_TIMEOUT: float = 600.0

# Storage-poll cadence.  500ms is short enough that the wait terminates
# promptly after a child finishes (well under the human-perceptible-latency
# floor), and long enough that a 60s wait incurs at most 120 cheap row
# reads — still cheaper than the 20+ inspect_workstream model turns the
# tool replaces.
WAIT_POLL_INTERVAL: float = 0.5

_TASK_STATUSES = frozenset({"pending", "in_progress", "done", "blocked"})
# Hard cap on tasks per coordinator — the full list is read and re-serialized
# on every mutation, so unbounded growth is both a storage and a tool-output-size
# hazard.  Hitting the cap is an explicit signal to prune done/blocked rows.
_TASK_LIST_MAX = 500
# Max task title length.  Exceeded titles return an error rather than
# silently truncating — mutating the coordinator's planning state
# under its nose masks real planning bugs (the model may rely on the
# title it SENT, not the stored one).
_TASK_TITLE_MAX = 200
# Short TTL on the per-ws_id live-inspect cache.  Back-to-back inspect()
# calls in a model's tool loop hit this hot-path; 2s is short enough
# that cached data stays meaningful to a human watching output and long
# enough to bound one stall per child per turn regardless of how many
# inspect calls the model fires.
_LIVE_CACHE_TTL_SECONDS = 2.0
# Cap on the number of tool names projected per skill in list_skills.
# A skill that whitelists a wide MCP surface (Slack/Gmail/Drive +
# dozens of helpers) would otherwise bloat the per-row payload and
# defeat the bounded-output contract.  Anything beyond the cap is
# rolled into a "+N more" sentinel so the model knows to fetch the
# full row if the inventory matters.
_SKILL_TOOLS_PROJECTION_CAP = 20


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with seconds precision — used for task timestamps."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_task_envelope(storage: Any, ws_id: str) -> tuple[dict[str, Any], bool]:
    """Decode the persisted task envelope for ``ws_id``.

    Shared between :class:`CoordinatorClient` (the model-tool write path)
    and the coordinator UI's read endpoints so both agree on the schema
    and corruption-tolerant semantics.  Returns ``(envelope, corrupt)``:

    - ``corrupt=False, envelope={"version": 1, "tasks": []}`` — row absent
      or empty.
    - ``corrupt=False, envelope=stored_dict`` — parseable and shape-checks.
    - ``corrupt=True, envelope={"version": 1, "tasks": []}`` — a non-empty
      stored value failed decode / shape check.  Callers that mutate the
      list refuse to overwrite a corrupt blob (preserve for operator
      inspection); the UI read path treats it as an empty envelope.
    """
    empty: dict[str, Any] = {"version": 1, "tasks": []}
    try:
        raw = storage.load_workstream_config(ws_id) or {}
    except Exception:
        log.debug("load_task_envelope.storage_failed ws=%s", ws_id, exc_info=True)
        return empty, False
    payload = raw.get("tasks")
    if not payload:
        return empty, False
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        log.warning("task_list.corrupt_envelope ws=%s (unparseable JSON)", ws_id)
        return empty, True
    if not (isinstance(data, dict) and isinstance(data.get("tasks"), list)):
        log.warning("task_list.corrupt_envelope ws=%s (wrong shape)", ws_id)
        return empty, True
    return data, False


if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-session coordinator JWT
# ---------------------------------------------------------------------------


class CoordinatorTokenManager:
    """Auto-rotating console-audience JWT for a single coordinator session.

    Mints a token with:

    - ``sub`` — the coordinator's real creator ``user_id``.
    - ``scopes`` — the creator's scopes (narrowed in the creator's identity
      already; the coordinator inherits without escalation).
    - ``src`` — ``"coordinator"`` so server-side audit can attribute tool
      calls to a coordinator session.
    - ``aud`` — :data:`JWT_AUD_CONSOLE` because the issued token is
      consumed by the console's own routing-proxy auth middleware.
    - ``coord_ws_id`` — the coordinator session's ``ws_id`` for forensics.

    Thread-safe: :attr:`token` re-mints on demand when the current JWT is
    within the refresh margin of expiry.
    """

    def __init__(
        self,
        user_id: str,
        scopes: frozenset[str],
        permissions: frozenset[str],
        secret: str,
        coord_ws_id: str,
        ttl_seconds: int = 300,
        refresh_margin: float = 0.2,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._user_id = user_id
        self._scopes = scopes
        self._permissions = permissions
        self._secret = secret
        self._coord_ws_id = coord_ws_id
        self._ttl = ttl_seconds
        self._margin = ttl_seconds * refresh_margin
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def _mint(self) -> None:
        self._token = create_jwt(
            user_id=self._user_id,
            scopes=self._scopes,
            source="coordinator",
            secret=self._secret,
            audience=JWT_AUD_CONSOLE,
            permissions=self._permissions,
            expiry_seconds=self._ttl,
            extra_claims={"coord_ws_id": self._coord_ws_id},
        )
        self._expires_at = time.time() + self._ttl
        # Observability — without this, mint races + premature-401
        # diagnostics require ad-hoc logging.  Mirrors the pattern in
        # ServiceTokenManager._mint (turnstone/core/auth.py).
        log.debug(
            "coordinator.token_mint coord_ws_id=%s user=%s ttl=%ds expires_at=%.1f",
            self._coord_ws_id,
            self._user_id,
            self._ttl,
            self._expires_at,
        )

    @property
    def token(self) -> str:
        with self._lock:
            if time.time() >= self._expires_at - self._margin:
                self._mint()
            return self._token


# ---------------------------------------------------------------------------
# Coordinator client
# ---------------------------------------------------------------------------


# URL paths on the console's routing proxy — must match the routes
# registered in turnstone/console/server.py (_CONSOLE_ROUTES).  Tested
# in test_coordinator_client.py against the live route table.
_ROUTE_PATHS: dict[str, str] = {
    "spawn": "/v1/api/route/workstreams/new",
    "send": "/v1/api/route/send",
    "approve": "/v1/api/route/approve",
    "cancel": "/v1/api/route/cancel",
    "close": "/v1/api/route/workstreams/close",
    "delete": "/v1/api/route/workstreams/delete",
    # The cascade endpoints live on the console itself (not a node), so
    # the path uses the coordinator ws_id in the URL rather than a
    # routing-proxy prefix.  ``_post`` still formats against
    # ``_base_url``; formatting of the ``{ws_id}`` slot happens at call
    # time in ``close_all_children``.
    "close_all_children": "/v1/api/workstreams/{ws_id}/close_all_children",
}


class CoordinatorClient:
    """Sync helper driving a coordinator session's children.

    See module docstring.  Not part of the public SDK — internal to
    ``turnstone-console`` only.
    """

    def __init__(
        self,
        console_base_url: str,
        storage: StorageBackend,
        token_factory: Callable[[], str],
        *,
        coord_ws_id: str,
        user_id: str,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = console_base_url.rstrip("/")
        self._storage = storage
        self._token_factory = token_factory
        self._coord_ws_id = coord_ws_id
        self._user_id = user_id
        self._timeout = timeout
        # ``http_client`` override exists for testing — prod always
        # constructs a fresh sync client so connection pools live and die
        # with the coordinator session.
        self._http = http_client or httpx.Client(timeout=timeout)
        self._owns_http = http_client is None
        # task_list per-ws lock cache — populated lazily by _task_lock().
        # Single-session so a plain dict behind a coarse lock is fine;
        # WeakValueDictionary isn't needed (entries live as long as the
        # CoordinatorClient instance).
        self._task_lock_cache: dict[str, threading.Lock] = {}
        self._task_lock_cache_lock = threading.Lock()
        # Per-ws_id short-TTL cache for the cluster-inspect live block.
        # Back-to-back inspect() calls against the same child (common
        # when a model is iterating over its children) would otherwise
        # each fire an HTTP round-trip at the 1s timeout and stall the
        # session thread.  2s is short enough that cached data stays
        # meaningful for a human reading model output.
        # OrderedDict + size cap turns the cache into an LRU — long-
        # running coordinators that walk many spawned-and-closed
        # children no longer accumulate dict entries for every ws_id
        # ever inspected.  256 entries is comfortably larger than any
        # realistic fan-out batch while bounding memory at ~O(256 *
        # tuple-size + dict-overhead) per coordinator.
        self._live_cache: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()
        self._live_cache_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        if self._owns_http:
            try:
                self._http.close()
            except httpx.HTTPError:
                log.debug("coord_client.close.failed", exc_info=True)

    # -- internal helpers ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_factory()}"}

    def _post(self, path_key: str, body: dict[str, Any]) -> dict[str, Any]:
        path = _ROUTE_PATHS[path_key]
        return self._post_url(f"{self._base_url}{path}", body, log_path=path)

    def _post_url(
        self,
        url: str,
        body: dict[str, Any],
        *,
        log_path: str,
    ) -> dict[str, Any]:
        """POST a pre-built URL with the canonical error handling.

        Split out so endpoints whose path slots in runtime data (e.g.
        the coord's own ``ws_id`` for cascade ops) can reuse the same
        transport-error / JSON-fallback / setdefault-status shape
        without duplicating the body of ``_post``.  ``log_path`` is a
        stable key for telemetry grouping — the URL itself embeds
        per-session ids that would fragment log aggregation.
        """
        try:
            resp = self._http.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            log.warning("coord_client.http_error path=%s err=%s", log_path, exc)
            return {"error": f"upstream unreachable: {exc}", "status": 0}
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            data.setdefault("error", f"HTTP {resp.status_code}")
        data.setdefault("status", resp.status_code)
        return data

    # -- tenant guard -------------------------------------------------------

    def _is_own_subtree(self, ws_id: str) -> bool:
        """Return True if ``ws_id`` is the coordinator itself or one of its
        own children.  Defense-in-depth gate for every model-invoked
        mutating op (send / close / cancel / delete) so a coordinator
        can't drive a foreign tenant's workstream even if the upstream
        node forgets to enforce ownership.  Read ops use the same gate
        inline (see ``inspect`` / ``wait_for_workstream``).

        The child must match BOTH the coord's ws_id in ``parent_ws_id``
        AND the coord's owner in ``user_id`` — a corrupted or
        cross-tenant ``parent_ws_id`` alone is not enough, so the
        trusted-send auto-approval path can't be fooled into sending
        to a foreign-tenant workstream.

        404-shape on miss matches the shape inspect() uses to avoid
        being an existence oracle.
        """
        if not ws_id:
            return False
        if ws_id == self._coord_ws_id:
            return True
        try:
            row = self._storage.get_workstream(ws_id)
        except Exception:
            log.debug("coord_client.is_own_subtree.lookup_failed ws=%s", ws_id, exc_info=True)
            return False
        if row is None:
            return False
        if row.get("parent_ws_id") != self._coord_ws_id:
            return False
        return bool(row.get("user_id")) and row.get("user_id") == self._user_id

    # -- model-invoked mutating ops (HTTP) ---------------------------------

    def spawn(
        self,
        *,
        initial_message: str,
        parent_ws_id: str,
        user_id: str,
        skill: str = "",
        name: str = "",
        model: str = "",
        target_node: str = "",
    ) -> dict[str, Any]:
        """Create a child workstream via the routing proxy."""
        body: dict[str, Any] = {
            "kind": WorkstreamKind.INTERACTIVE.value,
            "parent_ws_id": parent_ws_id,
            "user_id": user_id,
            "initial_message": initial_message,
        }
        if skill:
            body["skill"] = skill
        if name:
            body["name"] = name
        if model:
            body["model"] = model
        if target_node:
            body["target_node"] = target_node
        return self._post("spawn", body)

    def send(self, ws_id: str, message: str) -> dict[str, Any]:
        if not self._is_own_subtree(ws_id):
            return {"error": f"workstream not in coordinator subtree: {ws_id}", "status": 404}
        return self._post("send", {"ws_id": ws_id, "message": message})

    def emit_audit(self, action: str, detail: dict[str, Any]) -> None:
        """Record an audit row attributed to this coordinator session.

        Uses the client's own ``storage``, ``user_id``, and
        ``coord_ws_id`` so callers don't need to reach into the client's
        private attributes.  ``resource_type`` is always ``"coordinator"``
        and ``resource_id`` is the coord's ws_id.
        """
        from turnstone.core.audit import record_audit

        record_audit(
            self._storage,
            user_id=self._user_id,
            action=action,
            resource_type="coordinator",
            resource_id=self._coord_ws_id,
            detail=detail,
        )

    def close_workstream(self, ws_id: str, reason: str = "") -> dict[str, Any]:
        if not self._is_own_subtree(ws_id):
            return {"error": f"workstream not in coordinator subtree: {ws_id}", "status": 404}
        body: dict[str, Any] = {"ws_id": ws_id}
        if reason:
            body["reason"] = reason
        return self._post("close", body)

    def close_all_children(self, reason: str = "") -> dict[str, Any]:
        """Soft-close every direct child of this coordinator (console-side fan-out).

        Returns ``{closed, failed, skipped}`` — mirrors ``stop_cascade``.
        The console does the Semaphore-bounded gather so the model-side
        tool call stays a single HTTP round-trip regardless of fan-out
        size.  No tenant guard here: ownership is enforced on the
        endpoint via ``_resolve_coord_session``.
        """
        # Pass the unformatted template as ``log_path`` so telemetry
        # aggregates cleanly — the baked-in ws_id would fragment log
        # grouping across sessions.  The URL itself still embeds the
        # real ws_id.
        log_path = _ROUTE_PATHS["close_all_children"]
        path = log_path.format(ws_id=self._coord_ws_id)
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return self._post_url(f"{self._base_url}{path}", body, log_path=log_path)

    def delete(self, ws_id: str) -> dict[str, Any]:
        if not self._is_own_subtree(ws_id):
            return {"error": f"workstream not in coordinator subtree: {ws_id}", "status": 404}
        return self._post("delete", {"ws_id": ws_id})

    # -- console-endpoint helpers (NOT model-invoked tools) -----------------

    def approve(
        self,
        ws_id: str,
        *,
        call_id: str,
        approved: bool,
        feedback: str = "",
        always: bool = False,
    ) -> dict[str, Any]:
        body = {
            "ws_id": ws_id,
            "call_id": call_id,
            "approved": approved,
            "feedback": feedback,
            "always": always,
        }
        return self._post("approve", body)

    def cancel(self, ws_id: str) -> dict[str, Any]:
        if not self._is_own_subtree(ws_id):
            return {"error": f"workstream not in coordinator subtree: {ws_id}", "status": 404}
        return self._post("cancel", {"ws_id": ws_id})

    # -- model-invoked block-wait -----------------------------------------

    # ClassVar aliases for the module-level wait_for_workstream constants.
    # Kept so existing callers that read ``CoordinatorClient._WAIT_*`` keep
    # working; prefer the module-level ``WAIT_*`` constants in new code
    # (see #12 — the class-nesting was an inline-import smell).
    _WAIT_REAL_TERMINAL_STATES: ClassVar[frozenset[str]] = WAIT_REAL_TERMINAL_STATES
    _WAIT_TERMINAL_STATES: ClassVar[frozenset[str]] = WAIT_TERMINAL_STATES
    _WAIT_MAX_WS_IDS: ClassVar[int] = WAIT_MAX_WS_IDS
    _WAIT_MAX_TIMEOUT: ClassVar[float] = WAIT_MAX_TIMEOUT
    _WAIT_POLL_INTERVAL: ClassVar[float] = WAIT_POLL_INTERVAL

    def wait_for_workstream(
        self,
        ws_ids: list[str],
        *,
        timeout: float = 60.0,
        mode: str = "any",
        since: dict[str, dict[str, Any]] | None = None,
        progress_callback: Callable[[dict[str, dict[str, Any]], float], None] | None = None,
    ) -> dict[str, Any]:
        """Block until child workstreams reach a terminal state.

        ``mode='any'`` returns as soon as the first ws_id reaches a
        real terminal state (``idle`` / ``error`` / ``closed`` /
        ``deleted``; the last is unreachable in normal operation
        because hard-delete cascades the row out of storage, but it
        stays in the set so a legacy / synthetic-test row carrying
        that state still counts).  ``mode='all'`` returns once every
        ws_id has settled (real terminal OR ``denied``).  Returns
        ``{"results": {ws_id: {state, tokens, updated}},
        "elapsed": float, "complete": bool, "mode": mode}``.  ``complete``
        is True when the wait condition was met before the deadline,
        False when the timeout fired (results carry whatever last state
        was observed).

        ``since`` — optional prior snapshot (typically the ``results``
        dict from an earlier ``wait_for_workstream`` call).  When
        provided, the wait short-circuits as soon as ANY polled ws_id
        that ALSO has a ``since`` entry differs from that prior
        snapshot (``state`` / ``tokens`` / ``updated``), regardless of
        ``mode``.  Lets a follow-up wait skip re-counting
        already-completed children — the classic "spawn 3, wait for
        the first, then wait for the next change" loop turns into two
        calls instead of spinning the timeout on the still-terminal
        first child.  ws_ids absent from ``since`` do NOT themselves
        trigger the diff-based early exit — they fall back to the
        normal ``mode`` condition.  This prevents a disjoint ``since``
        dict from silently exiting on tick one (which the naive
        missing-entry-counts-as-changed rule would cause).

        Cross-tenant guard: a ws_id that's neither the coordinator
        itself nor one of its own children appears with
        ``state="denied"`` and never blocks the wait — a model that
        emits a foreign id learns immediately rather than spinning
        until timeout.  A ws_id that doesn't exist at all collapses
        into the same ``denied`` shape so wait can't be used as an
        existence oracle.

        ``progress_callback`` is invoked once per poll cycle with the
        current snapshot dict + elapsed seconds.  Swallows callback
        errors so a buggy observer can't break the wait loop.  Used
        by the coordinator-side wait dashboard (#14) to emit
        ``wait_progress`` SSE events; tests pass it to assert loop
        cadence.

        Performance: each tick issues exactly two storage calls
        (``get_workstreams_batch`` + ``sum_workstream_tokens_batch``),
        independent of ``len(ws_ids)``.  At the
        ``_WAIT_MAX_WS_IDS`` / ``_WAIT_MAX_TIMEOUT`` cap that's ~2400
        round-trips for a 600s wait — far below the ~38k of the naive
        per-id polling shape.
        """
        # Single source of truth for input validation — the session-side
        # tool prepare just builds a header and dispatches.  Returns an
        # error-shaped dict (matching the rest of the client surface) on
        # bad input rather than raising, so the session exec can surface
        # it through ``_report_tool_result`` like any other tool error.
        mode = str(mode if mode is not None else "any").strip().lower()
        if mode not in {"any", "all"}:
            return {
                "error": f"invalid mode: {mode!r} (must be 'any' or 'all')",
                "results": {},
                "complete": False,
                "elapsed": 0.0,
                "mode": mode,
            }
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in ws_ids or []:
            if not isinstance(raw, str):
                continue
            wid = raw.strip()
            if not wid or wid in seen:
                continue
            seen.add(wid)
            cleaned.append(wid)
        if not cleaned:
            return {
                "error": "ws_ids must contain at least one valid id",
                "results": {},
                "complete": False,
                "elapsed": 0.0,
                "mode": mode,
            }
        # Reject overflow rather than silently truncating — a mode='all'
        # wait with N>cap ids that returns complete=True after polling
        # only the first cap would falsely signal "all done" while the
        # dropped ids were never tracked.
        if len(cleaned) > self._WAIT_MAX_WS_IDS:
            return {
                "error": (f"too many ws_ids ({len(cleaned)}); cap is {self._WAIT_MAX_WS_IDS}"),
                "results": {},
                "complete": False,
                "elapsed": 0.0,
                "mode": mode,
            }
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError):
            timeout_f = 60.0
        timeout_f = max(0.0, min(timeout_f, self._WAIT_MAX_TIMEOUT))
        # Normalize since into a per-ws_id dict of the fields we diff
        # against.  Hostile / malformed entries silently drop — the
        # wait is advisory, not a gatekeeper, so an invalid hint
        # degrades to "no diff signal" rather than failing the call.
        since_map: dict[str, dict[str, Any]] = {}
        if isinstance(since, dict):
            for wid, prev in since.items():
                if isinstance(wid, str) and isinstance(prev, dict):
                    since_map[wid] = prev
        start = time.monotonic()
        deadline = start + timeout_f

        def _snapshot_all() -> dict[str, dict[str, Any]]:
            """One-tick snapshot for every cleaned ws_id.

            Issues two storage calls per tick (workstreams batch + token
            aggregate batch) instead of two-per-id, cutting per-tick
            round-trips from O(N) to O(1) at the documented cap.
            Cross-tenant + missing-row cases collapse into a single
            ``denied`` shape so wait can't be used as an existence oracle.
            """
            try:
                rows = self._storage.get_workstreams_batch(cleaned)
            except Exception:
                log.debug("coord_client.wait.get_ws_batch_failed", exc_info=True)
                rows = {wid: None for wid in cleaned}
            try:
                tokens_by_wid = self._storage.sum_workstream_tokens_batch(cleaned)
            except Exception:
                log.debug("coord_client.wait.sum_tokens_batch_failed", exc_info=True)
                tokens_by_wid = {}
            snaps: dict[str, dict[str, Any]] = {}
            for wid in cleaned:
                row = rows.get(wid)
                if row is None:
                    snaps[wid] = {"state": "denied", "tokens": 0}
                    continue
                is_self = wid == self._coord_ws_id
                is_own_child = row.get("parent_ws_id") == self._coord_ws_id
                if not (is_self or is_own_child):
                    snaps[wid] = {"state": "denied", "tokens": 0}
                    continue
                snaps[wid] = {
                    "state": str(row.get("state") or ""),
                    "tokens": int(tokens_by_wid.get(wid, 0) or 0),
                    "updated": row.get("updated") or "",
                }
            return snaps

        def _is_real_terminal(snap: dict[str, Any]) -> bool:
            # Real-terminal — these states drive ``complete=True``.
            # ``denied`` is intentionally excluded so a single typo'd /
            # foreign / nonexistent ws_id can't satisfy ``mode="any"``
            # while every real child is still running.
            return snap.get("state", "") in self._WAIT_REAL_TERMINAL_STATES

        def _is_settled(snap: dict[str, Any]) -> bool:
            # Settled — terminal OR denied.  Used to decide when the
            # wait should give up because there's nothing left to
            # observe (no real ws_ids in the polled set, or every real
            # one has already finished).
            return snap.get("state", "") in self._WAIT_TERMINAL_STATES

        def _diff_since(snap: dict[str, Any], prev: dict[str, Any]) -> bool:
            """True when ``snap`` differs from the ``since`` hint on any
            of the diffed fields.  Called only for ws_ids that appear in
            ``since_map`` — callers handling missing entries is the
            wrong default (it would make a disjoint since-dict exit
            the wait on tick one with complete=True)."""
            return any(snap.get(key) != prev.get(key) for key in ("state", "tokens", "updated"))

        last_results: dict[str, dict[str, Any]] = {}
        complete = False
        while True:
            results = _snapshot_all()
            last_results = results
            if progress_callback is not None:
                try:
                    progress_callback(results, time.monotonic() - start)
                except Exception:
                    log.debug("coord_client.wait.progress_cb_failed", exc_info=True)
            real_terminal = [_is_real_terminal(snap) for snap in results.values()]
            settled = [_is_settled(snap) for snap in results.values()]
            # ``since`` — orthogonal to mode.  If the caller supplied a
            # prior snapshot, any diff on a ws_id that IS in ``since_map``
            # exits the wait so a follow-up call doesn't re-count
            # already-terminal children.  ws_ids absent from ``since_map``
            # are ignored for the diff-exit check — they fall through to
            # the normal mode='any' / mode='all' conditions below.  This
            # prevents a disjoint since-dict from exiting on tick one
            # with complete=True (previous shape did, silently).
            if since_map and any(
                _diff_since(snap, since_map[wid])
                for wid, snap in results.items()
                if wid in since_map
            ):
                complete = True
                break
            if mode == "any":
                if any(real_terminal):
                    complete = True
                    break
                # Pure-denied list: every snap is settled but none is a
                # real terminal — no work to wait for.  Short-circuit so
                # the model sees the denied results immediately rather
                # than spinning the timeout (``complete=False`` because
                # the wait condition never had a real chance to fire).
                if all(settled):
                    break
            else:  # mode == "all"
                if all(settled):
                    # Every ws_id is settled (real-terminal or denied).
                    # The wait condition is met — the model gets the
                    # full results dict and decides what each terminal
                    # state means.
                    complete = True
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self._WAIT_POLL_INTERVAL, remaining))
        return {
            "results": last_results,
            "complete": complete,
            "elapsed": round(time.monotonic() - start, 3),
            "mode": mode,
        }

    # -- model-invoked read ops (direct storage) ---------------------------

    def list_children(
        self,
        parent_ws_id: str,
        *,
        state: str | None = None,
        skill: str | None = None,
        limit: int = 100,
        include_closed: bool = False,
    ) -> dict[str, Any]:
        """Return children of ``parent_ws_id`` excluding other coordinators.

        ``skill`` matches on ``skill_id`` (template id) when provided.
        ``include_closed`` controls whether soft-closed children appear;
        default False so the common "what's active?" query doesn't have
        to filter them out post-hoc.  Explicit ``state="closed"`` filter
        still works regardless (overrides the default).  Deleted rows
        are never returned because hard-delete cascades the row out of
        storage.

        Returns a dict ``{"children": [...], "truncated": bool}``.  The
        ``truncated`` flag is ``True`` when the SQL fetch returned a full
        ``limit``-sized page — the model can signal to the user there may
        be more rows and request pagination.  ``kind`` is pushed into the
        SQL query so coordinator-siblings never burn the row budget here.

        Cross-tenant guard: the coordinator's LLM input is untrusted
        (prompt injection is a first-class threat), so ``parent_ws_id``
        is constrained to the coordinator's own ws_id.  A model that
        emits some other ws_id gets an empty result rather than a peek
        into another tenant's subtree.
        """
        if parent_ws_id != self._coord_ws_id:
            return {"children": [], "truncated": False}
        # Tenant filter: push the coord's owner into SQL.  Children of a
        # coord share its owner by construction (the create-path
        # parent_ws_id gate at server.py enforces this), but the filter
        # is defense-in-depth for migration-era rows where that gate
        # didn't exist yet.
        raw = self._storage.list_workstreams(
            limit=limit,
            parent_ws_id=parent_ws_id,
            kind=WorkstreamKind.INTERACTIVE,
            user_id=self._user_id or None,
        )
        # ``deleted`` stays in the filter set so any legacy/synthetic row
        # that still carries that state (e.g. unmigrated data) is treated
        # as terminal.  Hard-delete cascades the row out of storage in the
        # normal path, so this only affects edge cases.
        _terminal_states = {"closed", "deleted"}
        children: list[dict[str, Any]] = []
        for row in raw:
            # Dict access via ``._mapping`` is resilient to SELECT
            # column-order changes; a positional row[6] lookup would
            # silently corrupt the response if a future migration added
            # a column earlier in the projection.
            try:
                m = row._mapping  # SQLAlchemy Row
            except AttributeError:
                # Fallback for non-Row tuples (test doubles, etc.).
                m = {
                    "ws_id": row[0],
                    "node_id": row[1],
                    "name": row[2],
                    "state": row[3],
                    "created": row[4],
                    "updated": row[5],
                    "kind": WorkstreamKind.from_raw(row[6] if len(row) > 6 else None),
                    "parent_ws_id": row[7] if len(row) > 7 else None,
                    "skill_id": row[8] if len(row) > 8 else None,
                    "skill_version": row[9] if len(row) > 9 else None,
                }
            if state is not None and m["state"] != state:
                continue
            # Default-exclude terminal states.  An explicit state
            # filter takes precedence (caller asking for state="closed"
            # clearly wants them); only drop terminal rows when the
            # caller didn't specify a state at all.
            if state is None and not include_closed and m["state"] in _terminal_states:
                continue
            child: dict[str, Any] = {
                "ws_id": m["ws_id"],
                "node_id": m["node_id"],
                "name": m["name"],
                "state": m["state"],
                "created": m["created"],
                "updated": m["updated"],
                "kind": m["kind"],
                "parent_ws_id": m["parent_ws_id"],
            }
            if skill is not None:
                # skill_id / skill_version are projected by list_workstreams —
                # no per-row get_workstream round-trip needed.
                if m["skill_id"] != skill:
                    continue
                child["skill_id"] = m["skill_id"]
                child["skill_version"] = m["skill_version"]
            children.append(child)
        # The DB filled a full page → more matching rows may exist behind
        # the cap; tell the model so it can re-query with a narrower filter
        # or larger limit.  Python-side post-filtering is unrelated to
        # whether the DB has more pages.
        truncated = len(raw) >= limit
        return {"children": children, "truncated": truncated}

    # Auto-metadata keys that expose internal network topology (RFC 1918
    # addresses, interface maps) without contributing to any routing
    # decision a coordinator makes.  Stripped from the default response
    # so the output guard's private_ip_disclosure check doesn't fire on
    # every ``list_nodes`` call.  Operators who need this for
    # debugging can opt back in via ``include_network_detail=True``.
    _NODES_NETWORK_KEYS: ClassVar[frozenset[str]] = frozenset({"interfaces"})

    # A node counts as "routable" for coordinator spawn targeting when
    # its service-registry heartbeat is within this window.  Matches
    # the default max_age_seconds on storage.list_services and the
    # ClusterCollector's own discovery freshness.
    _NODES_HEARTBEAT_WINDOW_S: ClassVar[int] = 120

    def list_nodes(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        include_network_detail: bool = False,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        """Return ``{"nodes": [...], "truncated": bool}``.

        Each row carries the node's metadata dict — both auto-populated
        keys (``arch``, ``cpu_count``, ``fqdn``, ``hostname``, ``os``,
        ``os_release``, ``python``; always present, ``source="auto"``) and
        operator-supplied user keys (deployment-specific, ``source="user"``).
        ``filters`` matches all key=value pairs (AND semantics) and is
        pushed into SQL via ``filter_nodes_by_metadata`` — no per-row
        lookups.

        Internal network metadata (``interfaces`` — container IPs and
        interface names) is stripped by default; pass
        ``include_network_detail=True`` to include it.  The model never
        needs this for routing decisions (routing is by capability/region
        tags) and it trips the private-IP output guard.

        Storage stores metadata values as JSON-encoded strings (the write
        path in ``server.py`` / ``admin.py`` / ``console/server.py`` all
        go through ``json.dumps``).  Filter values get re-encoded so the
        stored-text comparison succeeds, and read values get decoded so
        the model sees the natural Python form (``"x86_64"`` not
        ``'"x86_64"'``, ``4`` not ``"4"``).
        """
        page_size = max(1, min(int(limit), 500))
        # Liveness filter: node_metadata rows persist across node
        # restarts and never get deleted automatically, so
        # ``get_all_node_metadata`` returns every node that ever
        # registered — including long-dead container ids.  Intersect
        # against the service registry (last_heartbeat within
        # _NODES_HEARTBEAT_WINDOW_S) so target_node suggestions actually
        # route.  Operators debugging stale registrations can pass
        # include_inactive=True.
        active_ids: set[str] | None = None
        if not include_inactive:
            try:
                services = self._storage.list_services(
                    "server", max_age_seconds=self._NODES_HEARTBEAT_WINDOW_S
                )
                active_ids = {s["service_id"] for s in services if s.get("service_id")}
            except Exception:
                log.debug("coord_client.list_services_failed", exc_info=True)
                active_ids = None  # fail-open: return metadata as-is

        if filters:
            # Filtered case: narrow to the matching ids first, then pull
            # metadata only for the ``page_size``-bounded slice.  Avoids
            # the full-cluster ``get_all_node_metadata`` scan when the
            # model is asking for a handful of nodes.  Per-node lookups
            # are bounded at 500 by the limit clamp.
            encoded_filters = {str(k): json.dumps(v) for k, v in filters.items()}
            matching = self._storage.filter_nodes_by_metadata(encoded_filters)
            if active_ids is not None:
                matching = {nid for nid in matching if nid in active_ids}
            node_ids = sorted(matching)
            truncated = len(node_ids) > page_size
            node_ids = node_ids[:page_size]
            meta_rows_by_node: dict[str, list[dict[str, Any]]] = {
                nid: self._storage.get_node_metadata(nid) for nid in node_ids
            }
        else:
            # Unfiltered case: one wide query.  The caller is paging
            # through the whole cluster and needs metadata for every
            # node anyway — per-node lookups would be a true N+1.
            all_meta = self._storage.get_all_node_metadata()
            meta_node_ids = set(all_meta.keys())
            if active_ids is not None:
                meta_node_ids &= active_ids
            node_ids = sorted(meta_node_ids)
            truncated = len(node_ids) > page_size
            node_ids = node_ids[:page_size]
            meta_rows_by_node = {nid: all_meta.get(nid, []) for nid in node_ids}
        nodes: list[dict[str, Any]] = []
        for nid in node_ids:
            meta: dict[str, dict[str, Any]] = {}
            for r in meta_rows_by_node.get(nid, []):
                key = r.get("key")
                if not key:
                    continue
                key_str = str(key)
                if not include_network_detail and key_str in self._NODES_NETWORK_KEYS:
                    continue
                raw_value = r.get("value", "")
                try:
                    decoded = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
                except (TypeError, ValueError):
                    decoded = raw_value
                meta[key_str] = {
                    "value": decoded,
                    "source": str(r.get("source", "")),
                }
            nodes.append({"node_id": nid, "metadata": meta})
        return {"nodes": nodes, "truncated": truncated}

    def list_skills(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        risk_level: str | None = None,
        enabled_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return ``{"skills": [...], "truncated": bool}``.

        Coordinator-visible skills only: the storage filter narrows to
        ``kind IN ('coordinator', 'any')``.  Skills tagged
        ``interactive`` are hidden from the coordinator's
        ``list_skills`` tool (they're meant for child workstreams, not
        the orchestrator), while ``any``-tagged skills show up on both
        sides for backwards compatibility with pre-tagging catalogs.

        Filters pushed into SQL via ``list_skills_filtered`` — no per-row
        lookups.  ``tag`` matches when the value appears in the
        JSON-array ``tags`` column (quote-bracketed substring).
        ``tags`` is decoded from JSON at the edge so the model sees a
        list, not the escaped string.  Projection is intentionally narrow
        — discovery metadata only, not full row.
        """
        page_size = max(1, min(int(limit), 500))
        rows = self._storage.list_skills_filtered(
            category=category,
            tag=tag,
            risk_level=risk_level,
            kinds=["coordinator", "any"],
            enabled_only=enabled_only,
            limit=page_size + 1,  # +1 to detect truncation
        )
        truncated = len(rows) > page_size
        rows = rows[:page_size]
        skills: list[dict[str, Any]] = []
        for r in rows:
            tags_raw = r.get("tags") or "[]"
            try:
                tags = json.loads(tags_raw) if isinstance(tags_raw, str) else list(tags_raw)
            except (TypeError, ValueError):
                tags = []
            allowed_raw = r.get("allowed_tools") or "[]"
            try:
                allowed_full = (
                    json.loads(allowed_raw) if isinstance(allowed_raw, str) else list(allowed_raw)
                )
            except (TypeError, ValueError):
                allowed_full = []
            if not isinstance(allowed_full, list):
                allowed_full = []
            # Cap the projected tool list so a skill that whitelists a
            # large MCP surface doesn't bloat the coordinator's
            # list_skills payload.  Coordinators that need the full
            # inventory can fetch the skill row directly.
            allowed_tools: list[str] = [str(t) for t in allowed_full[:_SKILL_TOOLS_PROJECTION_CAP]]
            if len(allowed_full) > _SKILL_TOOLS_PROJECTION_CAP:
                allowed_tools.append(f"+{len(allowed_full) - _SKILL_TOOLS_PROJECTION_CAP} more")
            skills.append(
                {
                    "name": r.get("name") or "",
                    "category": r.get("category") or "",
                    "tags": tags,
                    "version": r.get("version") or "",
                    "description": r.get("description") or "",
                    "model": r.get("model") or "",
                    "enabled": bool(r.get("enabled")),
                    "risk_level": r.get("risk_level") or "",
                    "activation": r.get("activation") or "",
                    "kind": r["kind"],
                    "allowed_tools": allowed_tools,
                }
            )
        return {"skills": skills, "truncated": truncated}

    # ------------------------------------------------------------------
    # task_list — coordinator-local planning state persisted on workstream_config
    # ------------------------------------------------------------------

    def task_list_get(self, ws_id: str) -> dict[str, Any]:
        """Return the task envelope ``{"version": 1, "tasks": [...]}``.

        Corrupt / legacy config rows return an empty envelope rather than
        raising — a hand-edited DB shouldn't break the read path.  The
        mutating methods use ``_load_task_envelope_strict`` to detect
        corruption and refuse to overwrite silently.
        """
        env, _ = self._load_task_envelope(ws_id)
        return env

    def _load_task_envelope(self, ws_id: str) -> tuple[dict[str, Any], bool]:
        """Return ``(envelope, corrupt)``; ``corrupt=True`` iff the stored
        payload is non-empty and unparseable as the expected shape.

        Enforces the coordinator's cross-tenant guard (untrusted LLM input
        cannot be used to peek at another coordinator's task list), then
        delegates to :func:`load_task_envelope` so the HTTP read endpoint
        and this method share the same decoder + corruption semantics.
        """
        empty: dict[str, Any] = {"version": 1, "tasks": []}
        if ws_id != self._coord_ws_id:
            return empty, False
        return load_task_envelope(self._storage, ws_id)

    def _save_task_list(self, ws_id: str, envelope: dict[str, Any]) -> None:
        # Save only the ``tasks`` key so concurrent writers to other
        # workstream_config keys (e.g. reasoning_effort from the admin UI)
        # aren't clobbered by a read-modify-write on the full row.
        self._storage.save_workstream_config(
            ws_id, {"tasks": json.dumps(envelope, separators=(",", ":"))}
        )

    def _task_lock(self, ws_id: str) -> threading.Lock:
        """Per-ws lock cached on the client.

        Coordinator tool execs run on a single worker thread so contention
        is unlikely in practice, but the lock is cheap defence-in-depth
        for any future caller (maintenance script, HTTP handler) that
        mutates the list outside the worker thread.
        """
        with self._task_lock_cache_lock:
            lk = self._task_lock_cache.get(ws_id)
            if lk is None:
                lk = threading.Lock()
                self._task_lock_cache[ws_id] = lk
            return lk

    def task_list_add(
        self,
        ws_id: str,
        *,
        title: str,
        status: str = "pending",
        child_ws_id: str = "",
    ) -> dict[str, Any]:
        if ws_id != self._coord_ws_id:
            return {"error": f"task_list scope violation: {ws_id}"}
        clean_title = (title or "").strip()
        if not clean_title:
            return {"error": "title is required"}
        # Reject overlong titles rather than silently truncating —
        # mutating the coordinator's planning state under its nose
        # masks real planning bugs (the model may rely on the title it
        # SENT, not the stored one).  Callers that want a long title
        # must shorten it themselves.
        if len(clean_title) > _TASK_TITLE_MAX:
            return {
                "error": (
                    f"title too long ({len(clean_title)} chars, max "
                    f"{_TASK_TITLE_MAX}).  Shorten and retry."
                )
            }
        if status not in _TASK_STATUSES:
            return {"error": f"invalid status: {status}"}
        with self._task_lock(ws_id):
            envelope, corrupt = self._load_task_envelope(ws_id)
            if corrupt:
                return {
                    "error": (
                        "task_list envelope is corrupt on disk; refusing to "
                        "overwrite.  Inspect workstream_config.tasks manually "
                        "or clear it before retrying."
                    )
                }
            if len(envelope["tasks"]) >= _TASK_LIST_MAX:
                return {
                    "error": (
                        f"task_list capacity reached ({_TASK_LIST_MAX}).  "
                        "Remove completed tasks before adding more."
                    )
                }
            now = _utc_now_iso()
            task = {
                "id": "tsk_" + secrets.token_hex(6),
                "title": clean_title,
                "status": status,
                "child_ws_id": child_ws_id,
                "created": now,
                "updated": now,
            }
            envelope["tasks"].append(task)
            self._save_task_list(ws_id, envelope)
            return task

    def task_list_update(
        self,
        ws_id: str,
        *,
        task_id: str,
        title: str | None = None,
        status: str | None = None,
        child_ws_id: str | None = None,
    ) -> dict[str, Any]:
        if ws_id != self._coord_ws_id:
            return {"error": f"task_list scope violation: {ws_id}"}
        if status is not None and status not in _TASK_STATUSES:
            return {"error": f"invalid status: {status}"}
        with self._task_lock(ws_id):
            envelope, corrupt = self._load_task_envelope(ws_id)
            if corrupt:
                return {"error": ("task_list envelope is corrupt on disk; refusing to overwrite.")}
            for t in envelope["tasks"]:
                if t.get("id") == task_id:
                    if title is not None:
                        clean = title.strip()
                        if not clean:
                            return {"error": "title cannot be empty"}
                        if len(clean) > _TASK_TITLE_MAX:
                            return {
                                "error": (
                                    f"title too long ({len(clean)} chars, max "
                                    f"{_TASK_TITLE_MAX}).  Shorten and retry."
                                )
                            }
                        t["title"] = clean
                    if status is not None:
                        t["status"] = status
                    if child_ws_id is not None:
                        t["child_ws_id"] = child_ws_id
                    t["updated"] = _utc_now_iso()
                    self._save_task_list(ws_id, envelope)
                    # t is a dict pulled out of a json-decoded list; mypy
                    # sees it as Any from the decode path.  Cast back to
                    # the annotated return type.
                    return dict(t)
            return {"error": f"task not found: {task_id}"}

    def task_list_remove(self, ws_id: str, *, task_id: str) -> dict[str, Any]:
        """Remove a task by id.  Returns a result dict shaped like the
        other mutators — the caller can then distinguish scope violation
        vs corrupt envelope vs genuine not-found rather than collapsing
        all three into ``False`` (which would mis-report a corrupt DB
        as "task not found" to the coordinator LLM).
        """
        if ws_id != self._coord_ws_id:
            return {"error": f"task_list scope violation: {ws_id}"}
        with self._task_lock(ws_id):
            envelope, corrupt = self._load_task_envelope(ws_id)
            if corrupt:
                return {"error": ("task_list envelope is corrupt on disk; refusing to overwrite.")}
            before = len(envelope["tasks"])
            envelope["tasks"] = [t for t in envelope["tasks"] if t.get("id") != task_id]
            if len(envelope["tasks"]) == before:
                return {"error": f"task not found: {task_id}"}
            self._save_task_list(ws_id, envelope)
            return {"ok": True, "task_id": task_id}

    def cleanup_dead_task_child_refs(self, ws_id: str) -> int:
        """Clear ``child_ws_id`` pointers on tasks whose referenced
        workstream no longer exists in storage.  Returns the number of
        links blanked (0 if nothing needed doing, envelope was corrupt,
        or the lookup failed).

        Called by :meth:`SessionManager.close` after the state
        transition — the task envelope is a per-coordinator planning
        structure, so cross-coord scope guards don't apply the same way
        they do for add/update/remove.  Held under the same per-ws
        ``_task_lock`` as add/update/remove/reorder so a close racing
        an in-flight mutation can't lose the mutation (#bug-6).
        """
        with self._task_lock(ws_id):
            envelope, corrupt = load_task_envelope(self._storage, ws_id)
            if corrupt:
                return 0
            tasks = envelope.get("tasks") or []
            if not tasks:
                return 0
            candidate_ids = sorted(
                {
                    str(t.get("child_ws_id") or "")
                    for t in tasks
                    if isinstance(t, dict) and t.get("child_ws_id")
                }
            )
            if not candidate_ids:
                return 0
            try:
                existing_rows = self._storage.get_workstreams_batch(candidate_ids)
            except Exception:
                log.debug(
                    "coord_client.task_ref_batch_failed ws=%s",
                    ws_id,
                    exc_info=True,
                )
                return 0
            dead_ids = {cid for cid in candidate_ids if existing_rows.get(cid) is None}
            if not dead_ids:
                return 0
            blanked = 0
            for t in tasks:
                if isinstance(t, dict) and str(t.get("child_ws_id") or "") in dead_ids:
                    t["child_ws_id"] = ""
                    blanked += 1
            if not blanked:
                return 0
            try:
                self._save_task_list(ws_id, envelope)
            except Exception:
                # Write-side divergence — the task envelope on disk
                # now disagrees with what the close path intended.
                # Bump to warning (not debug) so operators see it;
                # read-side corruption (already silent on load) stays
                # at debug.  #q-6.
                log.warning(
                    "coord_client.task_ref_save_failed ws=%s blanked=%d",
                    ws_id,
                    blanked,
                    exc_info=True,
                )
                return 0
            return blanked

    def task_list_reorder(self, ws_id: str, *, task_ids: list[str]) -> dict[str, Any]:
        """Reject unless ``task_ids`` is an exact permutation of the
        current set — prevents silent task loss from a partial reorder.
        """
        if ws_id != self._coord_ws_id:
            return {"error": f"task_list scope violation: {ws_id}"}
        with self._task_lock(ws_id):
            envelope, corrupt = self._load_task_envelope(ws_id)
            if corrupt:
                return {"error": ("task_list envelope is corrupt on disk; refusing to overwrite.")}
            current = [t.get("id") for t in envelope["tasks"]]
            if set(task_ids) != set(current) or len(task_ids) != len(current):
                return {
                    "error": (
                        "task_ids must be a permutation of the existing set. "
                        f"current={sorted(filter(None, current))}"
                    ),
                }
            by_id = {t.get("id"): t for t in envelope["tasks"]}
            envelope["tasks"] = [by_id[tid] for tid in task_ids]
            self._save_task_list(ws_id, envelope)
            return {"ok": True, "order": task_ids}

    def inspect(
        self,
        ws_id: str,
        *,
        message_limit: int = 20,
        include_provider_content: bool = False,
    ) -> dict[str, Any]:
        """Return persisted workstream state + tail-N messages + recent verdicts.

        Cross-tenant guard: the coordinator's LLM input is untrusted, so
        the inspectable scope is restricted to (a) the coordinator
        itself or (b) a row whose ``parent_ws_id`` is this coordinator
        (i.e. one of its own children).  Any other ws_id returns the
        same not-found shape used for genuine misses, avoiding an
        existence oracle.

        ``include_provider_content`` defaults to False.  Provider-native
        content blocks (``_provider_content`` / ``provider_blocks``)
        duplicate the plain ``content`` string and roughly double the
        response size on longer conversations.  The model only needs
        them for provider-fidelity replay tooling; regular inspect
        calls get the trimmed shape.
        """
        full = self._storage.get_workstream(ws_id)
        miss = {"error": f"workstream not found: {ws_id}", "ws_id": ws_id}
        if full is None:
            return miss
        is_self = ws_id == self._coord_ws_id
        is_own_child = full.get("parent_ws_id") == self._coord_ws_id
        if not (is_self or is_own_child):
            return miss
        # load_messages returns the full history in chronological order
        # (no limit param in the Protocol) — slice the tail here.  Defensive
        # try/except: storage errors should not break inspect.
        messages: list[Any] = []
        try:
            all_msgs = self._storage.load_messages(ws_id)
            if message_limit and message_limit > 0:
                messages = all_msgs[-message_limit:]
            else:
                messages = all_msgs
        except Exception:
            log.debug("coord_client.load_messages.failed ws=%s", ws_id, exc_info=True)
        # Recent intent-judge verdicts — useful for "did this child go off
        # the rails?" inspection.  Capped at 10; advisory, so swallow failures.
        verdicts: list[Any] = []
        try:
            verdicts = self._storage.list_intent_verdicts(ws_id=ws_id, limit=10)
        except Exception:
            log.debug("coord_client.list_verdicts.failed ws=%s", ws_id, exc_info=True)
        result: dict[str, Any] = {
            **full,
            "messages": _serialize_messages(
                messages, include_provider_content=include_provider_content
            ),
            "verdicts": _serialize_verdicts(verdicts),
        }
        # Surface the operator-supplied close reason (persisted via
        # workstream_config by the server's close handler).  Only the
        # terminal-state shapes can carry a close_reason — gating on
        # state avoids a per-inspect DB read on the hot live-child path.
        if full.get("state") in {"closed", "error", "deleted"}:
            try:
                cfg = self._storage.load_workstream_config(ws_id) or {}
            except Exception:
                log.debug("coord_client.load_workstream_config.failed ws=%s", ws_id, exc_info=True)
                cfg = {}
            close_reason = cfg.get("close_reason")
            if close_reason:
                result["close_reason"] = close_reason
        live = self._fetch_cluster_live(ws_id)
        if live is not None:
            result["live"] = live
        return result

    def _fetch_cluster_live(self, ws_id: str) -> dict[str, Any] | None:
        """Optionally merge live state from the cluster-inspect endpoint.

        Best-effort: an error / non-2xx / missing ``live`` key all fall
        back to ``None`` so a node outage never breaks ``inspect``.  The
        model-facing tool schema is unchanged — the returned dict just
        gains an optional ``live`` key when available.

        Permission inheritance: the coordinator's per-session JWT carries
        the creator's scopes and permissions (see
        :class:`CoordinatorTokenManager`).  The cluster-inspect endpoint
        is gated on ``admin.cluster.inspect`` — creators without that
        permission get a 403 here and ``inspect`` silently degrades to
        storage-only.  This is correct behavior (the coordinator cannot
        exceed its creator's privilege), not a bug.  Operators who want
        live state in coordinator outputs must explicitly grant
        ``admin.cluster.inspect`` to those users.
        """
        # Short-TTL cache: repeated inspect() against the same child
        # (e.g. a model walking its children in a loop) amortizes to
        # one HTTP call per 2s instead of one per inspect.
        now = time.time()
        with self._live_cache_lock:
            cached = self._live_cache.get(ws_id)
            if cached is not None and now - cached[0] < _LIVE_CACHE_TTL_SECONDS:
                # LRU touch — move the fresh entry to the most-recent
                # position so it's not the next eviction candidate.
                self._live_cache.move_to_end(ws_id)
                return cached[1]
        try:
            url = f"{self._base_url}/v1/api/cluster/ws/{ws_id}/detail"
            # 1s timeout is ample for a same-host console call.  The
            # previous 2s was conservatively generous and stalled the
            # session thread visibly when a node was unhealthy.
            resp = self._http.get(url, headers=self._headers(), timeout=1.0)
        except httpx.HTTPError:
            log.debug("coord_client.cluster_inspect.http_error ws=%s", ws_id, exc_info=True)
            self._store_live_cache(ws_id, now, None)
            return None
        if resp.status_code < 200 or resp.status_code >= 300:
            self._store_live_cache(ws_id, now, None)
            return None
        try:
            payload = resp.json()
        except ValueError:
            self._store_live_cache(ws_id, now, None)
            return None
        if not isinstance(payload, dict):
            self._store_live_cache(ws_id, now, None)
            return None
        live = payload.get("live")
        result: dict[str, Any] | None = live if isinstance(live, dict) else None
        if result is not None and not result.get("tokens"):
            # Idle children's live block carries tokens=0 because the
            # node-dashboard counters only surface in-flight values; fall
            # back to the persisted aggregate so a child that already
            # burned thousands doesn't read as 0.  Folded into the live
            # cache (not done at the inspect call site) so back-to-back
            # inspects of an idle child don't each fire a fresh
            # ``sum_workstream_tokens`` aggregation.
            try:
                persisted = self._storage.sum_workstream_tokens(ws_id)
            except Exception:
                log.debug("coord_client.sum_tokens.failed ws=%s", ws_id, exc_info=True)
                persisted = 0
            if persisted:
                result = {**result, "tokens": persisted}
        self._store_live_cache(ws_id, now, result)
        return result

    _LIVE_CACHE_MAX = 256

    def _store_live_cache(self, ws_id: str, ts: float, value: dict[str, Any] | None) -> None:
        """Write an entry and evict the oldest if over the cap.

        Thread-safe: all mutation + eviction happens under
        ``_live_cache_lock``.  Eviction is LRU — ``OrderedDict`` orders
        by insertion order, ``move_to_end`` on read turns it into the
        LRU touch, and ``popitem(last=False)`` drops the least-recent.
        """
        with self._live_cache_lock:
            if ws_id in self._live_cache:
                self._live_cache.move_to_end(ws_id)
            self._live_cache[ws_id] = (ts, value)
            while len(self._live_cache) > self._LIVE_CACHE_MAX:
                self._live_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


_PROVIDER_FIDELITY_KEYS: frozenset[str] = frozenset({"_provider_content", "provider_blocks"})


def _serialize_messages(
    rows: list[Any], *, include_provider_content: bool = False
) -> list[dict[str, Any]]:
    """Normalize load_messages rows to JSON-friendly dicts.

    ``load_messages`` historically returns provider-specific message dicts
    (``role``/``content``/``tool_name``/...).  Keep the passthrough but
    ensure the list is serializable.

    When ``include_provider_content=False`` (default), strip
    provider-native content blocks — they duplicate the plain
    ``content`` string and roughly double response size on longer
    conversations.  Callers that need the full provider-fidelity
    payload (replay tooling, round-trip tests) pass True to restore.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            if include_provider_content:
                out.append(r)
            else:
                out.append({k: v for k, v in r.items() if k not in _PROVIDER_FIDELITY_KEYS})
        else:
            # Fall back to a string repr so at least something lands.
            out.append({"raw": str(r)})
    return out


def _serialize_verdicts(rows: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        else:
            try:
                out.append(dict(r._mapping))  # SQLAlchemy Row
            except Exception:
                out.append({"raw": str(r)})
    return out
