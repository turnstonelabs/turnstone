"""``console/session_factory.py`` alias-resolution coverage.

The console session factory resolves the coordinator alias through a
three-tier chain that must stay in lockstep with the placeholder logic
in ``console/server.py:list_available_models`` — otherwise the home
composer advertises one alias while sessions launch on another.

Tier order (highest priority first):

1. Per-call ``model_alias`` arg, or the ``coordinator.model_alias``
   ConfigStore setting (admin-pinned coordinator-specific override).
2. ``model.default_alias`` ConfigStore setting (admin-managed system
   default surfaced in the Models tab).
3. ``registry.default`` (config.toml ``[model].default``, the boot-time
   fallback).

These tests pin each branch by intercepting ``registry.resolve`` —
they short-circuit before ChatSession construction so the test never
has to satisfy ChatSession's full kwarg contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._coord_test_helpers import _FakeConfigStore
from turnstone.console.session_factory import build_console_session_factory


class _StopBeforeChatSessionError(Exception):
    """Sentinel raised by the capturing registry to short-circuit
    factory execution after alias resolution but before ChatSession is
    built.  The factory's outer code path is irrelevant to alias
    resolution and would force the test to satisfy a long kwarg
    contract for no extra coverage."""


class _CapturingRegistry:
    """Records the alias passed to ``resolve()`` and short-circuits.

    ``has_alias`` answers from the configured known set so the
    ``model.default_alias`` validation tier behaves realistically.
    Mirrors the public surface ``ModelRegistry`` exposes to
    session_factory: ``has_alias``, ``resolve``, and ``default``.
    """

    def __init__(self, *, default: str, known: set[str]) -> None:
        self.default = default
        self._known = known
        self.captured_alias: str | None = None

    def has_alias(self, alias: str) -> bool:
        return alias in self._known

    def resolve(self, alias: str) -> Any:
        self.captured_alias = alias
        raise _StopBeforeChatSessionError()


def _build_factory(
    *,
    registry_default: str = "registry-default",
    known_aliases: set[str] | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[Any, _CapturingRegistry]:
    """Construct the factory with stub deps.  Returns ``(factory_callable,
    registry)`` so tests can read back ``registry.captured_alias``."""

    registry = _CapturingRegistry(
        default=registry_default,
        known=known_aliases if known_aliases is not None else {registry_default},
    )
    config_store = _FakeConfigStore(dict(settings or {}))
    factory = build_console_session_factory(
        registry=registry,  # type: ignore[arg-type]
        config_store=config_store,  # type: ignore[arg-type]
        node_id="console",
        coord_client_factory=lambda ws_id, uid: MagicMock(),
    )
    return factory, registry


def _invoke(factory: Any, **factory_kwargs: Any) -> None:
    """Call the factory with a stub UI and absorb the sentinel.

    Forwards ``factory_kwargs`` to the factory so per-call overrides
    (e.g. ``model_alias``) can flow through.  Raises if any other
    exception comes out — the test should fail loudly when alias
    resolution itself errors rather than swallowing it.
    """
    ui = MagicMock()
    ui._user_id = ""  # skip storage-backed username lookup branch
    with pytest.raises(_StopBeforeChatSessionError):
        factory(ui, **factory_kwargs)


# ---------------------------------------------------------------------------
# Tier 1 — explicit pin (per-call arg or coordinator.model_alias)
# ---------------------------------------------------------------------------


def test_per_call_model_alias_arg_wins_over_everything() -> None:
    """The ``model_alias`` kwarg on the factory call (e.g. body field on
    POST /workstreams/new) wins over both ConfigStore tiers and the
    registry default."""
    factory, registry = _build_factory(
        known_aliases={"per-call", "coord-pin", "admin-default", "registry-default"},
        settings={
            "coordinator.model_alias": "coord-pin",
            "model.default_alias": "admin-default",
        },
    )
    _invoke(factory, model_alias="per-call")
    assert registry.captured_alias == "per-call"


def test_coordinator_model_alias_wins_when_no_per_call_override() -> None:
    factory, registry = _build_factory(
        known_aliases={"coord-pin", "admin-default", "registry-default"},
        settings={
            "coordinator.model_alias": "coord-pin",
            "model.default_alias": "admin-default",
        },
    )
    _invoke(factory)
    assert registry.captured_alias == "coord-pin"


def test_coordinator_model_alias_passed_through_unvalidated() -> None:
    """Tier 1 is an *explicit* operator pin — when it's stale or typoed
    we deliberately pass it through to ``registry.resolve`` so the
    request layer turns it into a 503 with the alias surfaced in the
    error.  Falling through silently would mask the misconfiguration."""
    factory, registry = _build_factory(
        known_aliases={"admin-default", "registry-default"},
        settings={
            "coordinator.model_alias": "ghost",  # unknown
            "model.default_alias": "admin-default",
        },
    )
    _invoke(factory)
    assert registry.captured_alias == "ghost"


def test_per_call_model_alias_arg_passed_through_unvalidated() -> None:
    """The per-call ``model_alias`` kwarg (POST body field — the more
    common production trigger) is the same kind of explicit pin as the
    ConfigStore setting, so a stale value passes through to
    ``registry.resolve`` rather than silently falling through to the
    system default."""
    factory, registry = _build_factory(
        known_aliases={"registry-default"},
        settings={"model.default_alias": "registry-default"},
    )
    _invoke(factory, model_alias="ghost")
    assert registry.captured_alias == "ghost"


# ---------------------------------------------------------------------------
# Tier 2 — model.default_alias (admin-managed system default)
# ---------------------------------------------------------------------------


def test_model_default_alias_used_when_coordinator_unset() -> None:
    """Regression for the historical drift: admin sets the system
    default in the Models tab, the home composer advertises it, and new
    coordinator sessions must launch on the same alias rather than
    silently falling through to ``registry.default``."""
    factory, registry = _build_factory(
        known_aliases={"admin-default", "registry-default"},
        settings={"model.default_alias": "admin-default"},
    )
    _invoke(factory)
    assert registry.captured_alias == "admin-default"


def test_unknown_model_default_alias_falls_through_to_registry_default() -> None:
    """Tier 2 is *not* an explicit pin — operators set
    ``model.default_alias`` once in the UI and forget about it; an alias
    that's later disabled or typo'd should not 503 the coordinator,
    since tier 3 (``registry.default``) is guaranteed to resolve."""
    factory, registry = _build_factory(
        known_aliases={"registry-default"},  # admin-default got removed
        settings={"model.default_alias": "admin-default"},
    )
    _invoke(factory)
    assert registry.captured_alias == "registry-default"


def test_blank_model_default_alias_falls_through_to_registry_default() -> None:
    factory, registry = _build_factory(
        settings={"model.default_alias": ""},
    )
    _invoke(factory)
    assert registry.captured_alias == "registry-default"


# ---------------------------------------------------------------------------
# Tier 3 — registry.default (config.toml [model].default)
# ---------------------------------------------------------------------------


def test_no_settings_uses_registry_default() -> None:
    factory, registry = _build_factory()
    _invoke(factory)
    assert registry.captured_alias == "registry-default"


def test_whitespace_only_coord_alias_falls_through() -> None:
    """``"   "`` is not an explicit pin — ``.strip()`` reduces it to
    "", which the chain should treat as unset."""
    factory, registry = _build_factory(
        settings={"coordinator.model_alias": "   "},
    )
    _invoke(factory)
    assert registry.captured_alias == "registry-default"
