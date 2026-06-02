"""Resolve a rerank client from configuration.

Shared by ``ChatSession`` and the ``turnstone-admin rerank-calibrate`` CLI (and
the admin "Calibrate" endpoint) so the resolution lives in one place: the
reranker is the model definition (capability ``supports_rerank``) selected via
the Reranker role (``tools.reranker_alias``). There is no global endpoint
fallback — the reranker is a per-model definition, nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turnstone.core.rerank import RerankClient


def resolve_rerank_client_from(
    config_store: Any | None,
    registry: Any | None,
    *,
    timeout: float,
) -> RerankClient | None:
    """Return a rerank client from the selected Reranker model, or ``None``.

    The reranker is the model definition (capability ``supports_rerank``) picked
    via the Reranker role (``tools.reranker_alias``) — managed like every other
    model, its ``base_url`` the full Cohere/Jina-compatible /rerank endpoint.
    Returns ``None`` until such a model is selected (there is no bundled rerank
    endpoint and no global URL fallback).
    """
    if config_store is None or registry is None:
        return None

    cs = config_store
    alias = str(cs.get("tools.reranker_alias") or "").strip()
    if not alias:
        return None
    try:
        cfg = registry.get_config(alias)
    except Exception:
        cfg = None
    if cfg is None or not cfg.base_url or not cfg.capabilities.get("supports_rerank"):
        return None

    from turnstone.core.config import get_rerank_instruction
    from turnstone.core.rerank import resolve_rerank_client

    # The query instruction is a global task knob (instruction-aware rerankers
    # like Qwen3); it applies to whichever reranker model is active. Explicit
    # stored value wins, then config.toml/env, then the registry default.
    stored = cs.stored_keys()
    if "tools.rerank_instruction" in stored:
        instruction = str(cs.get("tools.rerank_instruction") or "").strip()
    else:
        instruction = (
            get_rerank_instruction() or str(cs.get("tools.rerank_instruction") or "").strip()
        )

    return resolve_rerank_client(
        url=cfg.base_url,
        model=cfg.model or "",
        api_key=cfg.api_key,
        timeout=timeout,
        instruction=instruction,
    )
