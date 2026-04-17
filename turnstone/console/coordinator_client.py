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

import threading
import time
from typing import TYPE_CHECKING, Any

import httpx

from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt
from turnstone.core.log import get_logger

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
        ``limit``-sized page *and* the final result is shorter than ``limit``
        after post-filtering (state / skill) — the model can signal to the
        user there may be more rows and request pagination.  ``kind`` is
        pushed into the SQL query so coordinator-siblings never burn the
        row budget here.
        """
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
                # Filter on skill_id — pull it from get_workstream since
                # list_workstreams doesn't project it.  Cheap: bounded by
                # the already-filtered ``raw`` set.
                full = self._storage.get_workstream(m["ws_id"])
                if not full or full.get("skill_id") != skill:
                    continue
                child["skill_id"] = full.get("skill_id")
                child["skill_version"] = full.get("skill_version")
            children.append(child)
        # truncated is True when the DB returned a full page *and* post-
        # filtering dropped at least one row — the model should know more
        # children may be available via pagination.
        truncated = len(raw) >= limit and len(children) < len(raw)
        return {"children": children, "truncated": truncated}

    def inspect(self, ws_id: str, *, message_limit: int = 20) -> dict[str, Any]:
        """Return persisted workstream state + tail-N messages + recent verdicts."""
        full = self._storage.get_workstream(ws_id)
        if full is None:
            return {"error": f"workstream not found: {ws_id}", "ws_id": ws_id}
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
