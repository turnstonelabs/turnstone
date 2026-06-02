"""Resolve the runtime rerank client from configuration.

Sole caller is ``ChatSession._resolve_rerank_client``: the reranker is the model
definition (capability ``supports_rerank``) selected via the Reranker role
(``tools.reranker_alias``); there is no global endpoint fallback. The calibrate
CLI (``admin.py``) and the calibrate endpoint (``_global_rerank_instruction`` in
console/server.py) resolve the endpoint themselves via ``calibrate_model`` and
only share the instruction precedence below, not this function.
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
    # like Qwen3); it applies to whichever reranker model is active. Stored admin
    # value wins, else config.toml/env -- the same precedence the calibrate CLI
    # (admin.py) and the calibrate endpoint (_global_rerank_instruction) use, so
    # the instruction used at calibration time matches the one used at runtime.
    instruction = str(cs.get("tools.rerank_instruction") or "").strip() or get_rerank_instruction()

    return resolve_rerank_client(
        url=cfg.base_url,
        model=cfg.model or "",
        api_key=cfg.api_key,
        timeout=timeout,
        instruction=instruction,
    )
