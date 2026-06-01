"""Resolve a rerank client from configuration.

Shared by ``ChatSession`` and the ``turnstone-admin rerank-calibrate`` CLI (and,
later, an admin "Calibrate" endpoint) so the precedence — a Reranker model role
(``tools.reranker_alias`` -> a model with ``supports_rerank``) over the
``tools.rerank_url`` settings (storage -> config.toml/env) — lives in one place.
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
    """Return a rerank client from settings, or ``None`` when unconfigured.

    A reranker model definition selected via the Reranker role
    (``tools.reranker_alias`` -> a model with ``supports_rerank``) takes
    precedence; otherwise the ``tools.rerank_url`` settings (explicit stored
    value -> config.toml/env -> registry default). There is no bundled rerank
    endpoint, so this returns ``None`` until one is configured.
    """
    from turnstone.core.config import (
        get_rerank_api_key,
        get_rerank_instruction,
        get_rerank_model,
        get_rerank_url,
    )
    from turnstone.core.rerank import resolve_rerank_client

    cs = config_store
    stored = cs.stored_keys() if cs is not None else frozenset()

    def _setting(key: str, env_value: str) -> str:
        if cs is not None and key in stored:  # explicit admin value wins
            return str(cs.get(key) or "").strip()
        if env_value:  # config.toml / env var
            return env_value
        if cs is not None:  # registry default, surfaced by the store
            return str(cs.get(key) or "").strip()
        return ""

    # The query instruction is global — it applies to whichever endpoint resolves.
    instruction = _setting("tools.rerank_instruction", get_rerank_instruction())

    # A reranker model definition (capability ``supports_rerank``), picked via
    # the Reranker role, wins — managed like every other model; its base_url is
    # the full Cohere/Jina-compatible rerank endpoint.
    if cs is not None and registry is not None:
        alias = str(cs.get("tools.reranker_alias") or "").strip()
        if alias:
            try:
                cfg = registry.get_config(alias)
            except Exception:
                cfg = None
            if cfg is not None and cfg.base_url and cfg.capabilities.get("supports_rerank"):
                return resolve_rerank_client(
                    url=cfg.base_url,
                    model=cfg.model or "",
                    api_key=cfg.api_key,
                    timeout=timeout,
                    instruction=instruction,
                )

    return resolve_rerank_client(
        url=_setting("tools.rerank_url", get_rerank_url()),
        model=_setting("tools.rerank_model", get_rerank_model()),
        api_key=_setting("tools.rerank_api_key", get_rerank_api_key()),
        timeout=timeout,
        instruction=instruction,
    )
