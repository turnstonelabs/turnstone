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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt
from turnstone.core.log import get_logger

_TASK_STATUSES = frozenset({"pending", "in_progress", "done", "blocked"})
# Hard cap on tasks per coordinator — the full list is read and re-serialized
# on every mutation, so unbounded growth is both a storage and a tool-output-size
# hazard.  Hitting the cap is an explicit signal to prune done/blocked rows.
_TASK_LIST_MAX = 500


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with seconds precision — used for task timestamps."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
        url = f"{self._base_url}{path}"
        try:
            resp = self._http.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            log.warning("coord_client.http_error path=%s err=%s", path, exc)
            return {"error": f"upstream unreachable: {exc}", "status": 0}
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            data.setdefault("error", f"HTTP {resp.status_code}")
        data.setdefault("status", resp.status_code)
        return data

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
            "kind": "interactive",
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
        return self._post("send", {"ws_id": ws_id, "message": message})

    def close_workstream(self, ws_id: str, reason: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"ws_id": ws_id}
        if reason:
            # Forwarded for audit / future server-side use.  The server's
            # close handler currently ignores the key but the routing
            # proxy re-mint preserves the payload so downstream audit
            # middleware (when it lands) can read it.
            body["reason"] = reason
        return self._post("close", body)

    def delete(self, ws_id: str) -> dict[str, Any]:
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
        return self._post("cancel", {"ws_id": ws_id})

    # -- model-invoked read ops (direct storage) ---------------------------

    def list_children(
        self,
        parent_ws_id: str,
        *,
        state: str | None = None,
        skill: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return children of ``parent_ws_id`` excluding other coordinators.

        ``skill`` matches on ``skill_id`` (template id) when provided.

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
        raw = self._storage.list_workstreams(
            limit=limit,
            parent_ws_id=parent_ws_id,
            kind="interactive",
        )
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
                    "kind": row[6] if len(row) > 6 else "interactive",
                    "parent_ws_id": row[7] if len(row) > 7 else None,
                    "skill_id": row[8] if len(row) > 8 else None,
                    "skill_version": row[9] if len(row) > 9 else None,
                }
            if state is not None and m["state"] != state:
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

    def list_nodes(
        self,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return ``{"nodes": [...], "truncated": bool}``.

        Each row carries the node's full metadata dict — both auto-populated
        keys (``arch``, ``cpu_count``, ``fqdn``, ``hostname``, ``os``,
        ``os_release``, ``python``; always present, ``source="auto"``) and
        operator-supplied user keys (deployment-specific, ``source="user"``).
        ``filters`` matches all key=value pairs (AND semantics) and is
        pushed into SQL via ``filter_nodes_by_metadata`` — no per-row
        lookups.

        Storage stores metadata values as JSON-encoded strings (the write
        path in ``server.py`` / ``admin.py`` / ``console/server.py`` all
        go through ``json.dumps``).  Filter values get re-encoded so the
        stored-text comparison succeeds, and read values get decoded so
        the model sees the natural Python form (``"x86_64"`` not
        ``'"x86_64"'``, ``4`` not ``"4"``).
        """
        page_size = max(1, min(int(limit), 500))
        if filters:
            # Filtered case: narrow to the matching ids first, then pull
            # metadata only for the ``page_size``-bounded slice.  Avoids
            # the full-cluster ``get_all_node_metadata`` scan when the
            # model is asking for a handful of nodes.  Per-node lookups
            # are bounded at 500 by the limit clamp.
            encoded_filters = {str(k): json.dumps(v) for k, v in filters.items()}
            matching = self._storage.filter_nodes_by_metadata(encoded_filters)
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
            node_ids = sorted(all_meta.keys())
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
                raw_value = r.get("value", "")
                try:
                    decoded = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
                except (TypeError, ValueError):
                    decoded = raw_value
                meta[str(key)] = {
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
        scan_status: str | None = None,
        enabled_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return ``{"skills": [...], "truncated": bool}``.

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
            scan_status=scan_status,
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
            skills.append(
                {
                    "name": r.get("name") or "",
                    "category": r.get("category") or "",
                    "tags": tags,
                    "version": r.get("version") or "",
                    "description": r.get("description") or "",
                    "model": r.get("model") or "",
                    "enabled": bool(r.get("enabled")),
                    "scan_status": r.get("scan_status") or "",
                    "activation": r.get("activation") or "",
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
        payload is non-empty and unparseable as the expected shape."""
        empty: dict[str, Any] = {"version": 1, "tasks": []}
        if ws_id != self._coord_ws_id:
            return empty, False
        raw = self._storage.load_workstream_config(ws_id) or {}
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
        clean_title = (title or "").strip()[:200]
        if not clean_title:
            return {"error": "title is required"}
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
                        clean = title.strip()[:200]
                        if not clean:
                            return {"error": "title cannot be empty"}
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

    def inspect(self, ws_id: str, *, message_limit: int = 20) -> dict[str, Any]:
        """Return persisted workstream state + tail-N messages + recent verdicts.

        Cross-tenant guard: the coordinator's LLM input is untrusted, so
        the inspectable scope is restricted to (a) the coordinator
        itself or (b) a row whose ``parent_ws_id`` is this coordinator
        (i.e. one of its own children).  Any other ws_id returns the
        same not-found shape used for genuine misses, avoiding an
        existence oracle.
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
        return {
            **full,
            "messages": _serialize_messages(messages),
            "verdicts": _serialize_verdicts(verdicts),
        }


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_messages(rows: list[Any]) -> list[dict[str, Any]]:
    """Normalize load_messages rows to JSON-friendly dicts.

    ``load_messages`` historically returns provider-specific message dicts
    (``role``/``content``/``tool_name``/...). Keep the passthrough but
    ensure the list is serializable.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
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
