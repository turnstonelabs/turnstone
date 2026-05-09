"""Coordinator alias resolution shared by the placeholder API and the
session factory.

Both ``/v1/api/models`` (advertises the resolved default to the home
composer) and ``console/session_factory.py:factory`` (resolves the
alias new coordinator sessions launch on) walk the same three-tier
chain.  Centralising it here means the tier names and the tier-2
validation policy live once — the prior arrangement was two
implementations coupled by a "keep these in sync" comment, which is
exactly the drift trap that produced the historical bug where the
home composer advertised one alias while sessions ran on another.

Tiers, in priority order:

  1. **Explicit pin** — per-call ``model_alias`` arg (factory only) or
     the ``coordinator.model_alias`` ConfigStore setting.
  2. **System default** — ``model.default_alias`` ConfigStore setting,
     admin-managed in the Models tab.  Validated against
     ``registry.has_alias()`` — a stale or typo'd value falls through
     with a logged warning rather than 503ing.
  3. **Registry default** — ``registry.default`` (config.toml
     ``[model].default``), guaranteed by the registry to resolve.

Tier 1 is intentionally passed through unvalidated by default: an
explicit operator pin should surface as 503 at ``registry.resolve``
when stale, not silently fall through to a different alias.  Callers
that need stricter filtering (the placeholder API restricts to
enabled DB rows so the home composer doesn't advertise a model the
workstream picker can't offer) supply an ``alias_filter`` predicate
applied to every tier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.config_store import ConfigStore
    from turnstone.core.model_registry import ModelRegistry

log = get_logger(__name__)


def resolve_coordinator_alias(
    *,
    explicit: str | None,
    config_store: ConfigStore,
    registry: ModelRegistry,
    alias_filter: Callable[[str], bool] | None = None,
) -> str:
    """Resolve the effective coordinator alias through the three tiers.

    See module docstring for the full chain.  Returns the concrete
    alias name, or ``""`` when every tier failed (rare — only when
    ``registry.default`` itself fails the filter).
    """

    def _accept(alias: str) -> bool:
        if not alias:
            return False
        return alias_filter(alias) if alias_filter is not None else True

    explicit_alias = (explicit or "").strip()
    if not explicit_alias:
        explicit_alias = (config_store.get("coordinator.model_alias") or "").strip()
    if _accept(explicit_alias):
        return explicit_alias

    fallback_alias = (config_store.get("model.default_alias") or "").strip()
    if fallback_alias and not registry.has_alias(fallback_alias):
        log.warning(
            "coord_alias.model_default_alias_unknown alias=%r "
            "— falling through to registry.default",
            fallback_alias,
        )
        fallback_alias = ""
    if _accept(fallback_alias):
        return fallback_alias

    registry_default = registry.default or ""
    if _accept(registry_default):
        return registry_default

    return ""
