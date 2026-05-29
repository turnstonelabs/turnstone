"""``GET /v1/api/models`` (server) default-alias resolution.

The server handler surfaces effective defaults for the web UI's dashboard
composer + the channel gateway.  The dashboard's Options panel renders
each one as a "Default — alias (model)" placeholder, so the resolution
chain has to stay honest:

* ``default_alias``        ← ``model.default_alias``, falling back to the
  registry default; blanked when it points at a disabled/removed alias.
* ``channel_default_alias`` ← ``channels.default_model_alias``.
* ``judge_default_alias``   ← ``judge.model``, but *only* when it names an
  enabled alias.  An unset / whitespace / unknown / disabled value stays
  blank: at runtime the judge then inherits the per-workstream agent model
  (``session_factory``: ``judge_config.model or model``), which the UI
  renders as "Default (agent model)".  Surfacing a fixed alias there would
  mislabel the common follow-the-agent case.

These tests pin the judge branch (added so the server dashboard matches
the coordinator launcher) alongside the pre-existing model default.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from turnstone.server import list_available_models


class _StubRegistry:
    """Mimics the ``ModelRegistry`` surface ``list_available_models``
    reads: ``list_aliases()`` / ``get_config(alias)`` / ``.default``."""

    def __init__(self, *, aliases: dict[str, str], default: str = "") -> None:
        # alias -> underlying model id
        self._aliases = aliases
        self.default = default

    def list_aliases(self) -> list[str]:
        return list(self._aliases)

    def get_config(self, alias: str) -> SimpleNamespace:
        return SimpleNamespace(
            alias=alias,
            model=self._aliases[alias],
            provider="openai-compatible",
        )


def _make_client(
    *,
    aliases: dict[str, str] | None = None,
    settings: dict[str, str] | None = None,
    registry_default: str = "",
) -> TestClient:
    app = Starlette(
        routes=[Route("/v1/api/models", list_available_models)],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.registry = _StubRegistry(aliases=aliases or {}, default=registry_default)
    app.state.config_store = _FakeConfigStore(dict(settings or {}))
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": ""})
    return client


def _get_models(client: TestClient) -> dict[str, Any]:
    resp = client.get("/v1/api/models")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Judge resolution (new — drives the dashboard "Judge Model" placeholder)
# ---------------------------------------------------------------------------


def test_judge_unset_stays_blank_for_agent_model_fallback() -> None:
    """No ``judge.model`` configured → blank, so the dashboard keeps the
    "Default (agent model)" wording rather than advertising a fixed alias
    the judge won't actually use."""
    body = _get_models(
        _make_client(
            aliases={"primary": "vendor/primary"},
            settings={"model.default_alias": "primary"},
        )
    )
    assert body["default_alias"] == "primary"
    assert body["judge_default_alias"] == ""


def test_judge_explicit_enabled_alias_passes_through() -> None:
    body = _get_models(
        _make_client(
            aliases={"primary": "vendor/primary", "judge-fast": "vendor/judge-fast"},
            settings={
                "model.default_alias": "primary",
                "judge.model": "judge-fast",
            },
        )
    )
    assert body["judge_default_alias"] == "judge-fast"


def test_judge_set_to_unknown_alias_stays_blank() -> None:
    """``judge.model`` naming a non-enabled alias falls back to the agent
    model at runtime, so the field is blanked rather than echoing a value
    workstream creation can't honour."""
    body = _get_models(
        _make_client(
            aliases={"primary": "vendor/primary"},
            settings={
                "model.default_alias": "primary",
                "judge.model": "ghost",
            },
        )
    )
    assert body["judge_default_alias"] == ""


def test_judge_whitespace_only_value_stays_blank() -> None:
    """``judge.model`` is ``.strip()``-ed — a whitespace-only setting is
    treated as unset, not as an (always-unknown) alias."""
    body = _get_models(
        _make_client(
            aliases={"primary": "vendor/primary"},
            settings={"model.default_alias": "primary", "judge.model": "   "},
        )
    )
    assert body["judge_default_alias"] == ""


# ---------------------------------------------------------------------------
# Pre-existing model default stays correct under the new resolution code
# ---------------------------------------------------------------------------


def test_model_default_falls_back_to_registry_default() -> None:
    """Unset ``model.default_alias`` → the registry default is surfaced so
    the placeholder reports the alias sessions actually launch on."""
    body = _get_models(
        _make_client(aliases={"primary": "vendor/primary"}, registry_default="primary")
    )
    assert body["default_alias"] == "primary"
    assert body["judge_default_alias"] == ""


def test_model_default_blanked_when_pointing_at_unknown_alias() -> None:
    body = _get_models(
        _make_client(
            aliases={"primary": "vendor/primary"},
            settings={"model.default_alias": "ghost"},
        )
    )
    assert body["default_alias"] == ""
