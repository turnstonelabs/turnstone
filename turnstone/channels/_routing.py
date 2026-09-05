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
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from turnstone.channels._config import CREATE_LOCK_CAP
from turnstone.core.log import get_logger
from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.console import AsyncTurnstoneConsole
from turnstone.sdk.server import AsyncTurnstoneServer

_V = TypeVar("_V")


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
    from collections.abc import Callable, Iterable, Mapping, MutableMapping

    from turnstone.core.storage import StorageBackend

log = get_logger(__name__)

_WS_CREATE_TIMEOUT = 30.0  # seconds
_CHANNEL_DEFAULT_TTL = 300.0  # cache channel default alias for 5 minutes
_MODELS_CACHE_TTL = 30.0  # cache model list for autocomplete
_FORK_SOURCE_NOT_FOUND = "Workstream not found"
_STARTUP_PROBE_CONCURRENCY = 8
_STARTUP_PROBE_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Pending-approval bookkeeping shared by the channel adapters.
#
# Every adapter tracks its posted approval prompts in a dict keyed by
# ``(ws_id, cycle_id)`` — one entry per concurrent approval cycle — and
# needs the same two lookups: "this exact cycle, falling back to the
# workstream's single entry when the cycle_id is empty" (events from a
# pre-multi-cycle server, or buttons posted before an in-flight
# upgrade), and "everything for this workstream" (stream end / close /
# unsubscribe sweeps).  Centralised here so the legacy-fallback
# semantics can't drift between adapters.
# ---------------------------------------------------------------------------


def get_cycle_entry(
    entries: Mapping[tuple[str, str], _V],
    ws_id: str,
    cycle_id: str,
) -> _V | None:
    """Exact ``(ws_id, cycle_id)`` lookup with the pre-multi-cycle fallback.

    An empty *cycle_id* falls back to the workstream's single tracked
    entry; a NON-empty one never falls back (a stale cycle must not
    resolve an unrelated prompt).
    """
    entry = entries.get((ws_id, cycle_id))
    if entry is None and not cycle_id:
        entry = next((v for (wid, _), v in entries.items() if wid == ws_id), None)
    return entry


def pop_cycle_entry(
    entries: MutableMapping[tuple[str, str], _V],
    ws_id: str,
    cycle_id: str,
) -> _V | None:
    """Like :func:`get_cycle_entry`, but removes the matched entry."""
    entry = entries.pop((ws_id, cycle_id), None)
    if entry is None and not cycle_id:
        key = next((k for k in entries if k[0] == ws_id), None)
        if key is not None:
            entry = entries.pop(key, None)
    return entry


def pop_ws_entries(entries: MutableMapping[tuple[str, str], _V], ws_id: str) -> None:
    """Drop every entry tracked under *ws_id* (all cycles)."""
    for key in [k for k in entries if k[0] == ws_id]:
        entries.pop(key, None)


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

    async def _is_ws_live(self, ws_id: str) -> bool:
        """Return whether *ws_id* is loaded and usable on its owning node.

        Durable storage existence is not enough: capacity eviction leaves
        the source row available for an atomic fork but removes the live
        session that accepts channel messages. Direct mode reads the
        manager-authoritative active list; console mode uses the routed,
        read-only live probe so collector lag cannot create a false miss.

        Probe failures deliberately propagate. Treating an uncertain route
        as stale could delete the only channel mapping or create a duplicate
        workstream during a control-plane outage.
        """
        if self._console:
            result = await self._console.route_workstream_live(ws_id)
            return result.live

        return ws_id in await self._direct_live_workstream_ids()

    async def _direct_live_workstream_ids(self) -> set[str]:
        assert self._server is not None
        response = await self._server.list_workstreams()
        return {ws.ws_id for ws in response.workstreams if ws.state != "creating"}

    async def get_live_workstream_ids(self, ws_ids: Iterable[str]) -> set[str]:
        """Best-effort, bounded discovery for eager startup subscriptions only.

        Direct mode needs one manager list for all routes. Console mode uses
        the authoritative routed probes with a fixed worker pool and a total
        time budget. Errors skip eager subscription; inbound messages retain
        their strict liveness checks and can recover the saved routes later.
        """
        candidates = dict.fromkeys(ws_ids)
        live: set[str] = set()
        if not candidates:
            return live
        pending = iter(candidates)

        async def probe() -> None:
            for ws_id in pending:
                try:
                    if await self._is_ws_live(ws_id):
                        live.add(ws_id)
                except Exception:
                    log.warning("channel_router.startup_probe_failed", ws_id=ws_id, exc_info=True)

        try:
            async with asyncio.timeout(_STARTUP_PROBE_TIMEOUT):
                if self._console is None:
                    return (await self._direct_live_workstream_ids()).intersection(candidates)
                async with asyncio.TaskGroup() as tasks:
                    for _ in range(min(len(candidates), _STARTUP_PROBE_CONCURRENCY)):
                        tasks.create_task(probe())
        except Exception:
            log.warning("channel_router.startup_discovery_incomplete", exc_info=True)
        return live

    # -- workstream management -----------------------------------------------

    async def get_or_create_workstream(
        self,
        channel_type: str,
        channel_id: str,
        name: str = "",
        model: str = "",
        initial_message: str = "",
        client_type: str = "",
        channel_user_id: str = "",
        require_existing: bool = False,
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
            if require_existing and route is None:
                raise RuntimeError("Channel conversation was closed; start a new conversation")
            if route:
                owner = route.get("channel_user_id", "")
                if owner and owner != channel_user_id:
                    raise RuntimeError("Channel conversation belongs to another user")
                # Persisted routes contain exact IDs. An alias matching a
                # deleted ID must never substitute another conversation.
                source = await asyncio.to_thread(self._storage.get_workstream, route["ws_id"])
                if source is not None and await self._is_ws_live(route["ws_id"]):
                    return route["ws_id"], False
                # The route is not currently usable. Keep it persisted until
                # its replacement has been created successfully so ACL,
                # routing, and operational failures leave recovery possible.
                old_ws_id = route["ws_id"]
                log.info(
                    "channel_router.stale_route_detected",
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

            async def _create(resume_from: str) -> str:
                if self._console:
                    result = await self._console.route_create_workstream(
                        name=name,
                        model=model,
                        resume_ws=resume_from,
                        resume_ws_exact=bool(resume_from),
                        skill=self._skill,
                        auto_approve=self._auto_approve,
                        auto_approve_tools=_tools_csv,
                        client_type=client_type,
                    )
                    return result.ws_id

                assert self._server is not None
                response = await self._server.create_workstream(
                    name=name,
                    model=model,
                    resume_ws=resume_from,
                    resume_ws_exact=bool(resume_from),
                    skill=self._skill,
                    auto_approve=self._auto_approve,
                    auto_approve_tools=_tools_csv,
                    client_type=client_type,
                )
                return response.ws_id

            try:
                ws_id = await _create(resume_ws)
            except TurnstoneAPIError as exc:
                # Retry fresh only when BOTH the API response and a new
                # authoritative storage lookup confirm the fork source is gone.
                # The server deliberately masks private-source ACL denials as
                # the same 404 text, so response matching alone would turn an
                # authorization failure into an empty replacement conversation.
                if not resume_ws or exc.status_code != 404 or exc.message != _FORK_SOURCE_NOT_FOUND:
                    raise
                source = await asyncio.to_thread(self._storage.get_workstream, resume_ws)
                if source is not None:
                    raise
                log.info(
                    "channel_router.fork_source_missing",
                    ws_id=resume_ws,
                    channel_type=channel_type,
                    channel_id=channel_id,
                )
                resume_ws = ""
                ws_id = await _create(resume_ws)

            if not ws_id:
                msg_err = "workstream creation returned empty ws_id"
                raise RuntimeError(msg_err)

            # Claim the route before dispatching anything. Other gateway
            # processes and explicit closes can race this local lock; a
            # conditional write must not resurrect or overwrite their route.
            if old_ws_id:
                claimed = await asyncio.to_thread(
                    self._storage.replace_channel_route,
                    channel_type,
                    channel_id,
                    old_ws_id,
                    ws_id,
                )
            else:
                claimed = await asyncio.to_thread(
                    self._storage.create_channel_route,
                    channel_type,
                    channel_id,
                    ws_id,
                    channel_user_id=channel_user_id,
                )
            if not claimed:
                try:
                    await self.close_workstream(ws_id)
                except Exception:
                    log.warning("channel_router.unclaimed_close_failed", ws_id=ws_id, exc_info=True)
                raise RuntimeError("Channel route changed; retry your message")

            if initial_message and not resume_ws:
                await self.send_message(ws_id, initial_message)

            log.info(
                "channel_router.route_created",
                ws_id=ws_id,
                channel_type=channel_type,
                channel_id=channel_id,
            )

            return ws_id, True

    async def get_node_url(self, ws_id: str) -> str:
        """Return the direct server URL for SSE connections to *ws_id*.

        Resolve through the console on every connection attempt so a node's
        changed address is picked up after restart. Lookup failures propagate
        to the SSE retry loop; they do not authorize a different destination.
        """
        if self._console:
            data = await self._console.route_lookup(ws_id)
            node_url = data.get("node_url", "")
            if not node_url:
                raise RuntimeError("Console route lookup returned no node URL")
            return str(node_url).rstrip("/")
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
        """Approve or deny a pending tool call via the server API.

        ``correlation_id`` is the cycle_id captured from the
        ``approve_request`` event the adapter displayed; forwarding it
        makes the decision land on exactly that cycle.  With parallel
        task agents a workstream can hold several prompts — a
        selector-less approve would resolve the OLDEST, which may not
        be the message the user answered.  Empty string (policy /
        auto-approve sweeps that act on "whatever is pending") keeps
        the legacy oldest-first behavior.
        """
        if self._console:
            await self._console.route_approve(
                ws_id=ws_id,
                approved=approved,
                feedback=feedback,
                always=always,
                cycle_id=correlation_id,
            )
        else:
            assert self._server is not None
            await self._server.approve(
                ws_id=ws_id,
                approved=approved,
                feedback=feedback or None,
                always=always,
                cycle_id=correlation_id or None,
            )
        log.debug(
            "channel_router.send_approval",
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
        )

    # -- route management ----------------------------------------------------

    async def delete_route(
        self, channel_type: str, channel_id: str, *, expected_ws_id: str | None = None
    ) -> bool:
        """Remove a channel-to-workstream mapping."""
        deleted = await asyncio.to_thread(
            self._storage.delete_channel_route,
            channel_type,
            channel_id,
            expected_ws_id=expected_ws_id,
        )
        log.info(
            "channel_router.delete_route",
            channel_type=channel_type,
            channel_id=channel_id,
            deleted=deleted,
        )
        return deleted

    async def close_workstream(self, ws_id: str) -> None:
        """Close a workstream via the server API."""
        try:
            if self._console:
                await self._console.route_close(ws_id)
            else:
                assert self._server is not None
                await self._server.close_workstream(ws_id)
            log.info("channel_router.close_workstream", ws_id=ws_id)
        except TurnstoneAPIError as exc:
            if exc.status_code != 404:
                raise
            log.warning(
                "channel_router.close_workstream_failed",
                ws_id=ws_id,
                status=exc.status_code,
            )
