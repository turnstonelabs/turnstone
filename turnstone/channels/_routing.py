"""Channel router -- maps external channels/threads to turnstone workstreams.

:class:`ChannelRouter` uses the turnstone SDK clients to communicate with
the server (single-node) or console (multi-node) API, and the storage
backend for persistent channel-to-workstream mappings.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from turnstone.channels._config import CREATE_LOCK_CAP
from turnstone.core.log import get_logger
from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.console import AsyncTurnstoneConsole
from turnstone.sdk.server import AsyncTurnstoneServer


@dataclass
class PolicyVerdict:
    """Outcome of evaluating admin tool policies for an approval request.

    ``kind`` is one of:

    - ``"none"``: no tool needed approval evaluation (e.g. all items are
      errors or already resolved). Adapter should fall through to the
      auto-approve branch.
    - ``"deny"``: at least one tool was denied by policy. Adapter should
      notify the user and forward ``approved=False`` with the feedback.
    - ``"allow"``: every tool was allowed by policy. Adapter should
      notify the user and forward ``approved=True``.
    - ``"defer"``: mixed or unknown verdict. Adapter should fall through
      to interactive approval.
    """

    kind: Literal["none", "deny", "allow", "defer"]
    denied_tools: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)


if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.storage import StorageBackend

log = get_logger(__name__)

_WS_CREATE_TIMEOUT = 30.0  # seconds
_CHANNEL_DEFAULT_TTL = 300.0  # cache channel default alias for 5 minutes
_MODELS_CACHE_TTL = 30.0  # cache model list for autocomplete
_ROUTE_CACHE_TTL = 30.0  # cache (channel_type, channel_id) → ws_id lookups
_ROUTE_CACHE_CAP = 4096  # LRU bound on the lookup cache


class ChannelRouter:
    """Manage channel-to-workstream routing via SDK clients.

    Parameters
    ----------
    server_url:
        Base URL of the turnstone server (e.g. ``http://localhost:8080/v1``).
    storage:
        A :class:`StorageBackend` instance for persistent route lookups.
        All storage calls are synchronous and will be wrapped in
        :func:`asyncio.to_thread`.
    api_token:
        Optional bearer token for authenticating with the server API.
    """

    def __init__(
        self,
        server_url: str,
        storage: StorageBackend,
        *,
        auto_approve: bool = False,
        auto_approve_tools: list[str] | None = None,
        skill: str = "",
        api_token: str = "",
        console_url: str = "",
        console_token_factory: Callable[[], str] | None = None,
        server_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._console_url = console_url.rstrip("/") if console_url else ""
        self._storage = storage
        self._auto_approve = auto_approve
        self._auto_approve_tools: list[str] = auto_approve_tools or []
        self._skill = skill
        self._create_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        # Per-workstream node URLs from console routing responses.
        # Populated when console_url is set and the create response
        # includes node_url.
        self._node_urls: dict[str, str] = {}

        # SDK clients: use console for multi-node, server for single-node.
        self._console: AsyncTurnstoneConsole | None = None
        self._server: AsyncTurnstoneServer | None = None
        if self._console_url:
            self._console = AsyncTurnstoneConsole(
                base_url=self._console_url,
                token=api_token,
                token_factory=console_token_factory,
                timeout=_WS_CREATE_TIMEOUT,
            )
        else:
            self._server = AsyncTurnstoneServer(
                base_url=self._server_url,
                token=api_token,
                token_factory=server_token_factory,
                timeout=_WS_CREATE_TIMEOUT,
            )

        # Cached channel default alias (TTL-based).
        self._channel_default_alias: str = ""
        self._channel_default_ts: float = 0.0
        # Cached model list for autocomplete (shorter TTL).
        self._models_cache: dict[str, Any] = {}
        self._models_cache_ts: float = 0.0
        # TTL cache for (channel_type, channel_id) → ws_id so hot inbound
        # paths don't hit storage on every message. Bounded LRU.
        self._route_cache: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying SDK clients."""
        if self._server:
            await self._server.aclose()
        if self._console:
            await self._console.aclose()
        log.info("channel_router.closed")

    # -- model listing -------------------------------------------------------

    async def list_models(self, *, cached: bool = False) -> dict[str, Any]:
        """Fetch available model aliases and defaults from the server/console.

        When *cached* is True, returns a TTL-cached result to avoid
        per-keystroke HTTP traffic during autocomplete.
        """
        if cached:
            now = time.monotonic()
            if self._models_cache and (now - self._models_cache_ts) < _MODELS_CACHE_TTL:
                return self._models_cache

        if self._console:
            resp: Any = await self._console.list_models()
        else:
            assert self._server is not None
            resp = await self._server.list_models()
        # SDK returns a Pydantic model; convert to dict for callers.
        data: dict[str, Any] = resp.model_dump() if hasattr(resp, "model_dump") else resp

        # Update cache regardless of `cached` flag — a fresh fetch is
        # always worth caching for subsequent callers.
        self._models_cache = data
        self._models_cache_ts = time.monotonic()
        return data

    async def get_channel_default_alias(self) -> str:
        """Return the channel default model alias (cached with TTL)."""
        now = time.monotonic()
        if (now - self._channel_default_ts) < _CHANNEL_DEFAULT_TTL:
            return self._channel_default_alias
        # Mark refresh window before awaiting so concurrent callers
        # reuse the cached value instead of triggering duplicate fetches.
        prev_ts = self._channel_default_ts
        self._channel_default_ts = now
        try:
            data = await self.list_models()
            self._channel_default_alias = data.get("channel_default_alias", "")
        except Exception:
            # Roll the timestamp back so the next caller retries instead of
            # serving a stale/empty alias for the full TTL window.
            self._channel_default_ts = prev_ts
            log.debug("channel_router.channel_default_fetch_failed", exc_info=True)
        return self._channel_default_alias

    # -- internal helpers ----------------------------------------------------

    async def _is_ws_alive(self, ws_id: str) -> bool:
        """Check whether *ws_id* is a known workstream.

        Uses an O(1) storage lookup (primary-key query) instead of
        fetching the full workstream list from the server.  If the
        workstream exists in the database it is considered alive.  A
        false positive (exists in DB but not loaded on any server node)
        is harmless -- the subsequent ``send_message`` call will receive
        a 404 and the adapter will handle reconnection.
        """
        try:
            resolved = await asyncio.to_thread(self._storage.resolve_workstream, ws_id)
            return resolved is not None
        except Exception:
            return False

    # -- workstream management -----------------------------------------------

    async def get_or_create_workstream(
        self,
        channel_type: str,
        channel_id: str,
        name: str = "",
        model: str = "",
        initial_message: str = "",
        client_type: str = "",
    ) -> tuple[str, bool]:
        """Look up or create a workstream for a channel.

        Returns ``(ws_id, is_new)`` where *is_new* is ``True`` when a new
        workstream was created.

        A per-channel lock prevents duplicate workstreams when concurrent
        messages arrive for the same channel before the first creation
        completes.
        """
        key = f"{channel_type}:{channel_id}"
        lock = self._create_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._create_locks[key] = lock
            # Bound the map: once a route is persisted, the lock is no longer
            # needed on future requests, so evicting the LRU entry is safe —
            # UNLESS that entry is currently held by a task awaiting I/O
            # inside the critical section.  Evicting a held lock breaks
            # mutual exclusion because a subsequent cache miss for the
            # same key would create a fresh lock and run the create path
            # concurrently (→ duplicate server-side workstreams).  Scan
            # from oldest to newest and pop the first unheld entry; if
            # every entry is held we leave the map slightly over-cap
            # rather than corrupt ordering.
            if len(self._create_locks) > CREATE_LOCK_CAP:
                for candidate_key, candidate_lock in list(self._create_locks.items()):
                    if candidate_key == key:
                        continue
                    if not candidate_lock.locked():
                        del self._create_locks[candidate_key]
                        break
        else:
            self._create_locks.move_to_end(key)

        async with lock:
            # 1. Check for existing route.
            old_ws_id = ""
            route = await asyncio.to_thread(
                self._storage.get_channel_route, channel_type, channel_id
            )
            if route:
                # Verify the workstream is still alive on the server.
                if await self._is_ws_alive(route["ws_id"]):
                    return route["ws_id"], False
                # Workstream was evicted/closed — capture old ws_id for
                # resume, then remove the stale route.
                old_ws_id = route["ws_id"]
                await asyncio.to_thread(
                    self._storage.delete_channel_route, channel_type, channel_id
                )
                log.info(
                    "channel_router.stale_route_cleared",
                    ws_id=old_ws_id,
                    channel_type=channel_type,
                    channel_id=channel_id,
                )

            # 2. Create via SDK client with atomic resume.
            resume_ws = old_ws_id or ""
            _tools_csv = ",".join(self._auto_approve_tools) if self._auto_approve_tools else ""
            log.info(
                "channel_router.creating_workstream",
                channel_type=channel_type,
                channel_id=channel_id,
                resume_ws=resume_ws or None,
            )

            if self._console:
                data = await self._console.route_create_workstream(
                    name=name,
                    model=model,
                    resume_ws=resume_ws,
                    skill=self._skill,
                    auto_approve=self._auto_approve,
                    auto_approve_tools=_tools_csv,
                    client_type=client_type,
                )
                ws_id = data.get("ws_id", "")
            else:
                assert self._server is not None
                resp = await self._server.create_workstream(
                    name=name,
                    model=model,
                    resume_ws=resume_ws,
                    skill=self._skill,
                    auto_approve=self._auto_approve,
                    auto_approve_tools=_tools_csv,
                    client_type=client_type,
                )
                ws_id = resp.ws_id
                data = {"ws_id": resp.ws_id, "name": resp.name}

            if not ws_id:
                msg_err = "workstream creation returned empty ws_id"
                raise RuntimeError(msg_err)

            # When routed through the console, capture the node URL for
            # direct SSE connections.
            node_url = data.get("node_url", "")
            if node_url:
                self._node_urls[ws_id] = node_url.rstrip("/")

            # 3. Send the initial message if this is a brand-new workstream.
            if initial_message and not resume_ws:
                if self._console:
                    await self._console.route_send(initial_message, ws_id)
                else:
                    assert self._server is not None
                    await self._server.send(initial_message, ws_id)

            # 4. Persist the route.
            await asyncio.to_thread(
                self._storage.create_channel_route, channel_type, channel_id, ws_id
            )
            log.info(
                "channel_router.route_created",
                ws_id=ws_id,
                channel_type=channel_type,
                channel_id=channel_id,
            )

            return ws_id, True

    async def get_node_url(self, ws_id: str) -> str:
        """Return the direct server URL for SSE connections to *ws_id*.

        When routing through a console, this is the ``node_url`` from the
        create response.  If the ws_id is not cached (e.g. after a bot
        restart), queries the console's route lookup endpoint before
        falling back to the configured server_url.
        """
        url = self._node_urls.get(ws_id)
        if url:
            return url
        if self._console:
            try:
                data = await self._console.route_lookup(ws_id)
                node_url = data.get("node_url", "")
                if node_url:
                    self._node_urls[ws_id] = node_url.rstrip("/")
                    return self._node_urls[ws_id]
            except Exception:
                log.debug("Console route lookup failed for ws %s", ws_id, exc_info=True)
        return self._server_url

    # -- user resolution -----------------------------------------------------

    async def resolve_user(self, channel_type: str, channel_user_id: str) -> str | None:
        """Resolve an external platform user to a turnstone ``user_id``.

        Returns ``None`` if no mapping exists.
        """
        result = await asyncio.to_thread(
            self._storage.get_channel_user, channel_type, channel_user_id
        )
        if result is None:
            return None
        return result.get("user_id")

    # -- message dispatch ----------------------------------------------------

    async def send_message(self, ws_id: str, message: str) -> None:
        """Send a user message to a workstream via the server API."""
        if self._console:
            await self._console.route_send(message, ws_id)
        else:
            assert self._server is not None
            await self._server.send(message, ws_id)
        log.debug("channel_router.send_message", ws_id=ws_id)

    async def evaluate_tool_policies(
        self,
        items: list[dict[str, Any]],
    ) -> PolicyVerdict:
        """Evaluate admin tool policies for an ApproveRequestEvent batch.

        Returns a :class:`PolicyVerdict` summarising the outcome so each
        adapter only has to translate the verdict into platform-specific
        chat messages.
        """
        tool_names = [
            it.get("approval_label", "") or it.get("func_name", "")
            for it in items
            if it.get("needs_approval") and it.get("func_name") and not it.get("error")
        ]
        tool_names = [n for n in tool_names if n]
        if not tool_names:
            return PolicyVerdict(kind="none")

        try:
            from turnstone.core.policy import evaluate_tool_policies_batch

            verdicts = await asyncio.to_thread(
                evaluate_tool_policies_batch,
                self._storage,
                tool_names,
            )
        except Exception:
            # Fail-open: freezing every workstream on a storage hiccup is worse
            # than letting the approval fall through to interactive review.
            # Log at WARNING so the policy-DB outage is still auditable.
            log.warning("channel_router.policy_evaluation_failed", exc_info=True)
            return PolicyVerdict(kind="defer", tool_names=tool_names)

        denied = [n for n, v in verdicts.items() if v == "deny"]
        if denied:
            return PolicyVerdict(kind="deny", denied_tools=denied, tool_names=tool_names)
        if all(verdicts.get(n) == "allow" for n in tool_names):
            return PolicyVerdict(kind="allow", tool_names=tool_names)
        return PolicyVerdict(kind="defer", tool_names=tool_names)

    async def send_approval(
        self,
        ws_id: str,
        correlation_id: str,
        approved: bool,
        feedback: str = "",
        always: bool = False,
    ) -> None:
        """Approve or deny a pending tool call via the server API."""
        if self._console:
            await self._console.route_approve(
                ws_id=ws_id, approved=approved, feedback=feedback, always=always
            )
        else:
            assert self._server is not None
            await self._server.approve(
                ws_id=ws_id, approved=approved, feedback=feedback or None, always=always
            )
        log.debug(
            "channel_router.send_approval",
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
        )

    async def send_plan_feedback(self, ws_id: str, correlation_id: str, feedback: str) -> None:
        """Respond to a plan review via the server API."""
        if self._console:
            await self._console.route_plan_feedback(ws_id=ws_id, feedback=feedback)
        else:
            assert self._server is not None
            await self._server.plan_feedback(ws_id=ws_id, feedback=feedback)
        log.debug(
            "channel_router.send_plan_feedback",
            ws_id=ws_id,
            correlation_id=correlation_id,
        )

    # -- route management ----------------------------------------------------

    async def lookup_ws_id(self, channel_type: str, channel_id: str) -> str | None:
        """Return the ws_id bound to (channel_type, channel_id), or None.

        TTL-cached so the hot inbound-message path (thread replies,
        DM replies) doesn't hit storage on every token.
        """
        key = (channel_type, channel_id)
        now = time.monotonic()
        cached = self._route_cache.get(key)
        if cached is not None:
            ws_id, expires_at = cached
            if now < expires_at:
                self._route_cache.move_to_end(key)
                return ws_id
            # Expired — fall through to a fresh lookup.
            del self._route_cache[key]

        route = await asyncio.to_thread(self._storage.get_channel_route, channel_type, channel_id)
        if route is None:
            return None

        ws_id = route["ws_id"]
        self._route_cache[key] = (ws_id, now + _ROUTE_CACHE_TTL)
        if len(self._route_cache) > _ROUTE_CACHE_CAP:
            self._route_cache.popitem(last=False)
        return ws_id

    async def delete_route(self, channel_type: str, channel_id: str) -> None:
        """Remove a channel-to-workstream mapping."""
        self._route_cache.pop((channel_type, channel_id), None)
        deleted = await asyncio.to_thread(
            self._storage.delete_channel_route, channel_type, channel_id
        )
        log.info(
            "channel_router.delete_route",
            channel_type=channel_type,
            channel_id=channel_id,
            deleted=deleted,
        )

    async def close_workstream(self, ws_id: str) -> None:
        """Close a workstream via the server API."""
        self._node_urls.pop(ws_id, None)
        try:
            if self._console:
                await self._console.route_close(ws_id)
            else:
                assert self._server is not None
                await self._server.close_workstream(ws_id)
            log.info("channel_router.close_workstream", ws_id=ws_id)
        except TurnstoneAPIError as exc:
            log.warning(
                "channel_router.close_workstream_failed",
                ws_id=ws_id,
                status=exc.status_code,
            )
