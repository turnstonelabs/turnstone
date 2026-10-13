"""Coordinator alias resolution shared by the placeholder API and the
session factory.

Both ``/v1/api/models`` (advertises the resolved default to the home
composer) and ``console/session_factory.py:factory`` (resolves the
alias new coordinator sessions launch on) walk the same three-tier
chain.  Centralising it here means the tier names live once — the prior
arrangement was two implementations coupled by a "keep these in sync"
comment, which is exactly the drift trap that produced the historical bug
where the home composer advertised one alias while sessions ran on another.

Tiers, in priority order:

  1. **Explicit pin** — per-call ``model_alias`` arg (factory only) or
     the ``coordinator.model_alias`` ConfigStore setting.
  2. **System default** — ``model.default_alias`` ConfigStore setting,
     admin-managed in the Models tab.
  3. **Registry default** — ``registry.default`` (config.toml
     ``[model].default``, else the model named ``default``, else the first
     defined chat model).

No tier is checked against what loaded (#1359): the first configured alias
wins, and when its model isn't loaded the create fails with
``model_not_loaded`` instead of quietly launching on a later tier's model.
The placeholder API reports the same alias, so the home composer names the
model a create would use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turnstone.core.config_store import ConfigStore
    from turnstone.core.model_registry import ModelRegistry


def resolve_coordinator_alias(
    *,
    explicit: str | None,
    config_store: ConfigStore,
    registry: ModelRegistry,
) -> str:
    """Resolve the effective coordinator alias through the three tiers.

    See module docstring for the full chain.  Returns the concrete alias
    name, or ``""`` when no tier names one (no model defined anywhere).
    """
    explicit_alias = (explicit or "").strip()
    if not explicit_alias:
        explicit_alias = (config_store.get("coordinator.model_alias") or "").strip()
    if explicit_alias:
        return explicit_alias
    return registry.effective_default(config_store)
