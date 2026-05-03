"""Console-local session factory for coordinator workstreams.

Mirrors the server's factory closure (``turnstone/server.py``
``session_factory``), but:

- Always builds a ``kind="coordinator"`` ChatSession.
- Injects the shared :class:`CoordinatorClient` as the ``coord_client``
  kwarg so coordinator tool execs can dispatch through the console's
  routing proxy + shared storage.
- Reads ``tools.*`` / ``judge.*`` / ``memory.*`` / ``session.*``
  settings from ``config_store.get(...)`` — same pattern the server
  uses, so admin hot-reloads flow through to new coordinator sessions.

Unlike the server factory this does not consult ``args`` (CLI argparse) —
the console doesn't carry that surface.  ``coordinator.model_alias`` +
``coordinator.reasoning_effort`` come from the DB-backed settings
registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turnstone.core.log import get_logger
from turnstone.core.session import ChatSession
from turnstone.core.workstream import WorkstreamKind
from turnstone.prompts import ClientType

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.console.coordinator_client import CoordinatorClient
    from turnstone.core.config_store import ConfigStore
    from turnstone.core.model_registry import ModelRegistry
    from turnstone.core.session import SessionUI

log = get_logger(__name__)


def build_console_session_factory(
    *,
    registry: ModelRegistry,
    config_store: ConfigStore,
    node_id: str,
    coord_client_factory: Callable[[str, str], CoordinatorClient],
) -> Callable[..., ChatSession]:
    """Return a session factory that builds coordinator-kind ChatSessions.

    The factory signature matches :class:`turnstone.core.workstream._SessionFactory`.
    ``coord_client_factory`` is called at session-create time with
    ``(ws_id, user_id)`` and returns a prepared :class:`CoordinatorClient`.

    Only ``kind="coordinator"`` is supported here — the console doesn't
    host interactive workstreams.  The factory rejects any other kind
    defensively so a bug upstream surfaces loudly instead of silently
    constructing a malformed session.
    """
    from turnstone.core.judge import JudgeConfig
    from turnstone.core.memory_relevance import MemoryConfig

    def _build_judge_config() -> JudgeConfig:
        return JudgeConfig(
            enabled=config_store.get("judge.enabled"),
            model=config_store.get("judge.model"),
            confidence_threshold=config_store.get("judge.confidence_threshold"),
            max_context_ratio=config_store.get("judge.max_context_ratio"),
            timeout=config_store.get("judge.timeout"),
            read_only_tools=config_store.get("judge.read_only_tools"),
            output_guard=config_store.get("judge.output_guard"),
            redact_secrets=config_store.get("judge.redact_secrets"),
        )

    def _build_memory_config() -> MemoryConfig:
        return MemoryConfig(
            relevance_k=config_store.get("memory.relevance_k"),
            fetch_limit=config_store.get("memory.fetch_limit"),
            max_content=config_store.get("memory.max_content"),
            nudge_cooldown=config_store.get("memory.nudge_cooldown"),
            nudges=config_store.get("memory.nudges"),
        )

    def factory(
        ui: SessionUI | None,
        model_alias: str | None = None,
        ws_id: str | None = None,
        *,
        skill: str | None = None,
        client_type: str = "web",
        kind: WorkstreamKind = WorkstreamKind.COORDINATOR,
        parent_ws_id: str | None = None,
        judge_model: str | None = None,
    ) -> ChatSession:
        assert ui is not None, "console session_factory requires a non-None UI"
        if kind != WorkstreamKind.COORDINATOR:
            raise ValueError(
                f"console session factory only supports kind=COORDINATOR, got {kind!r}"
            )

        # Resolve coordinator.model_alias from settings if caller didn't
        # override.  Unset ``coordinator.model_alias`` falls back to the
        # model registry's default alias — operators get a working
        # coordinator on a freshly-provisioned console without an extra
        # manual setting.  Resolve to the CONCRETE alias name
        # (``registry.default``) rather than passing None downstream:
        # ``ChatSession.__init__`` reads ``registry.get_provider(alias)``
        # to pick the right provider class, and passing None makes it
        # fall through to a generic OpenAI-compat provider — which
        # mismatches when the default is Anthropic/Google-backed.
        explicit_alias = model_alias or (config_store.get("coordinator.model_alias") or "").strip()
        # ``SessionManager.open`` passes the persisted ``model_alias``
        # through on the rehydrate path so reopened coordinators keep
        # the model they were created with.  An operator may have
        # removed that alias from the registry since — fall back to
        # the registry default in that case rather than 500-ing every
        # reopen.  Same fall-through covers an unset / empty
        # ``coordinator.model_alias`` ConfigStore value.
        if not explicit_alias or not registry.has_alias(explicit_alias):
            effective_alias = registry.default
        else:
            effective_alias = explicit_alias

        r_client, r_model, r_cfg = registry.resolve(effective_alias)

        uid = getattr(ui, "_user_id", "") or ""
        _username = ""
        if uid:
            try:
                from turnstone.core.storage._registry import get_storage as _gs

                st = _gs()
                if st:
                    u = st.get_user(uid)
                    if u:
                        _username = u.get("username", "")
            except Exception:
                log.debug("coord_factory.username_resolve_failed uid=%s", uid, exc_info=True)

        live_memory_config = _build_memory_config()
        live_judge_config = _build_judge_config()
        # NOTE: do not pre-resolve ``live_judge_config.model`` against the
        # registry here.  ``IntentJudge.__init__`` does a richer resolution
        # that also picks up the alias's *provider + client*; rewriting
        # ``model`` to the underlying model id strands the alias and forces
        # IntentJudge to fall back to the session's provider with a model
        # name that provider may not even know about (e.g. coordinator on
        # Anthropic, judge alias pointing at OpenAI gpt-5-mini → silent
        # ``llm_fallback`` verdicts on every tool call).
        if live_judge_config and judge_model:
            import dataclasses

            # Per-call judge_model override mirrors the server-side
            # interactive factory: pin the alias on the JudgeConfig but
            # leave alias→client/provider resolution to IntentJudge for
            # the same reason as above.  ``registry.resolve`` is only
            # called as a typo / unknown-alias guard so a misconfigured
            # body field surfaces in the log instead of silently falling
            # back to the session's provider.
            try:
                registry.resolve(judge_model)
                live_judge_config = dataclasses.replace(
                    live_judge_config,
                    model=judge_model,
                )
            except Exception as e:
                log.warning(
                    "coord_factory.judge_model_resolve_failed alias=%r err=%s",
                    judge_model,
                    e,
                )

        eff_temperature = (
            r_cfg.temperature
            if r_cfg.temperature is not None
            else config_store.get("model.temperature")
        )
        eff_max_tokens = (
            r_cfg.max_tokens
            if r_cfg.max_tokens is not None
            else config_store.get("model.max_tokens")
        )
        # Coordinator has its own effort setting; fall back to model-level
        # override, then global default.
        eff_reasoning_effort = (
            r_cfg.reasoning_effort
            if r_cfg.reasoning_effort is not None
            else (
                config_store.get("coordinator.reasoning_effort")
                or config_store.get("model.reasoning_effort")
            )
        )

        coord_client = coord_client_factory(ws_id or "", uid)

        return ChatSession(
            client=r_client,
            model=r_model,
            ui=ui,
            instructions=config_store.get("session.instructions") or None,
            temperature=eff_temperature,
            max_tokens=eff_max_tokens,
            tool_timeout=config_store.get("tools.timeout"),
            reasoning_effort=eff_reasoning_effort,
            context_window=r_cfg.context_window,
            compact_max_tokens=config_store.get("session.compact_max_tokens"),
            auto_compact_pct=config_store.get("session.auto_compact_pct"),
            agent_max_turns=config_store.get("tools.agent_max_turns"),
            tool_truncation=config_store.get("tools.truncation"),
            mcp_client=None,  # console doesn't host MCP today
            registry=registry,
            model_alias=effective_alias,
            health_registry=None,
            node_id=node_id,
            ws_id=ws_id,
            tool_search=config_store.get("tools.search"),
            tool_search_threshold=config_store.get("tools.search_threshold"),
            tool_search_max_results=config_store.get("tools.search_max_results"),
            web_search_backend=config_store.get("tools.web_search_backend"),
            skill=skill or None,
            judge_config=live_judge_config,
            user_id=uid,
            memory_config=live_memory_config,
            config_store=config_store,
            client_type=ClientType(client_type)
            if client_type in {ct.value for ct in ClientType}
            else ClientType.WEB,
            username=_username,
            kind=WorkstreamKind.COORDINATOR,
            parent_ws_id=parent_ws_id,
            coord_client=coord_client,
        )

    return factory
