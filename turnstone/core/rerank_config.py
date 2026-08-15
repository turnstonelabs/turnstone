"""Resolve an immutable runtime rerank lane from configuration.

Sole caller is ``ChatSession._resolve_rerank_lane``: the reranker is the model
definition (capability ``supports_rerank``) selected via the Reranker role
(``tools.reranker_alias``); there is no global endpoint fallback. Resolution
creates a fresh frozen binding around a registry-owned shared runtime, never a
session-owned client. Calibration remains an isolated one-shot path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from turnstone.core.rerank import RerankLane


def _effective_snapshot(config_store: Any) -> tuple[int, dict[str, Any]]:
    snapshot = getattr(config_store, "effective_snapshot", None)
    if callable(snapshot):
        version, settings = snapshot()
        return int(version), dict(settings)
    # Compatibility for lightweight embedders/tests implementing only get().
    return int(getattr(config_store, "version", 0)), {
        "tools.reranker_alias": config_store.get("tools.reranker_alias"),
        "tools.rerank_instruction": config_store.get("tools.rerank_instruction"),
    }


def _deactivate(registry: Any) -> None:
    deactivate = getattr(registry, "deactivate_rerank_runtime", None)
    if callable(deactivate):
        deactivate()


def resolve_rerank_lane_from(
    config_store: Any | None,
    registry: Any | None,
    *,
    max_attempts: int = 3,
) -> RerankLane | None:
    """Return a coherent lane for the selected Reranker model, or ``None``.

    The reranker is the model definition (capability ``supports_rerank``) picked
    via the Reranker role (``tools.reranker_alias``) — managed like every other
    model, its ``base_url`` the full Cohere/Jina-compatible /rerank endpoint. A
    ConfigStore snapshot and registry generation witness keep multi-key settings
    and model binding coherent across concurrent reloads.
    """
    if config_store is None or registry is None:
        return None

    from turnstone.core.config import get_rerank_instruction

    for _attempt in range(max(1, max_attempts)):
        version, settings = _effective_snapshot(config_store)
        alias = str(settings.get("tools.reranker_alias") or "").strip()
        if not alias:
            _deactivate(registry)
            return None
        instruction = (
            str(settings.get("tools.rerank_instruction") or "").strip() or get_rerank_instruction()
        )
        try:
            lane = cast(
                "RerankLane",
                registry.resolve_rerank_lane(
                    alias,
                    instruction=instruction,
                    config_version=version,
                ),
            )
        except (KeyError, ValueError):
            _deactivate(registry)
            return None
        if (
            int(getattr(config_store, "version", version)) == version
            and int(getattr(registry, "generation", lane.registry_generation))
            == lane.registry_generation
        ):
            return lane
    # A continuously changing configuration is not a safe binding. Retrieval
    # callers preserve native order and retry resolution on their next use.
    return None
