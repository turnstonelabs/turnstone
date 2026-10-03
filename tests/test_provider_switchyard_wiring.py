"""Reachability tests for the Switchyard provider.

A provider adapter that no gate accepts is dead code with a passing test suite:
the name has to be valid where model rows are written, where operator pins are
dispatched, where clients are built, and where the effort projection is computed.
These tests pin the wiring and, more usefully, pin that the wiring agrees with the
factory's own declaration of the names it accepts — in both directions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source
from turnstone.core.providers import (
    API_SURFACE_PROVIDERS,
    LOCAL_PROVIDERS,
    create_client,
    create_provider,
    lookup_model_capabilities,
)
from turnstone.core.providers._switchyard import PROVIDER_NAME

_SUPPORTED_PREFIX = "Supported: "
_CONSOLE_STATIC = Path(__file__).resolve().parent.parent / "turnstone/console/static"
_ADMIN_JS = _CONSOLE_STATIC / "admin.js"
_INDEX_HTML = _CONSOLE_STATIC / "index.html"


def _factory_declared_names() -> list[str]:
    """The names ``create_provider`` says it accepts, read from its own refusal.

    The message is the factory's own statement of its name set, so it is the
    closest thing to an enumeration the module exposes.  A change in that prose
    fails this test loudly rather than silently emptying the comparison.
    """
    with pytest.raises(ValueError) as excinfo:
        create_provider("__no_such_provider__")
    message = str(excinfo.value)
    assert _SUPPORTED_PREFIX in message, (
        "create_provider's refusal no longer states its supported names; the "
        f"parity check below cannot see the population. Message was: {message!r}"
    )
    tail = message.split(_SUPPORTED_PREFIX, 1)[1]
    names = [part.strip() for part in tail.replace("\n", " ").split(",")]
    names = [n for n in names if n]
    # Positive control: a parser that returned nothing would make the comparison
    # below vacuous, and "" would pass every assertion about membership.
    assert len(names) >= 6 and "openai" in names, f"parsed names look wrong: {names}"
    return names


def test_the_factory_still_declares_its_names_parseably() -> None:
    assert PROVIDER_NAME in _factory_declared_names()


def test_every_name_the_factory_accepts_is_writable_as_a_model_row() -> None:
    """Otherwise the row is rejected at the API and the adapter is unreachable."""
    from turnstone.console.server import _MODEL_PROVIDERS

    assert set(_factory_declared_names()) == set(_MODEL_PROVIDERS), (
        "the factory's accepted names and the admin validator's accepted names "
        "have drifted apart; a name in one list only is a lane nobody can save "
        "or a row that cannot be served"
    )


def test_every_gated_name_is_constructible_by_the_factory() -> None:
    """A name that appears in a gate but not in the factory is a dead gate."""
    from turnstone.console.server import _MODEL_PROVIDERS
    from turnstone.core.model_turn import EXTRA_BODY_PROVIDERS

    gated = set(_MODEL_PROVIDERS) | set(EXTRA_BODY_PROVIDERS)
    for name in sorted(gated):
        for surface in ("chat", "responses"):
            assert create_provider(name, api_surface=surface) is not None


def test_switchyard_lanes_take_the_same_operator_pins_as_the_name_they_replace() -> None:
    from turnstone.core.model_turn import EXTRA_BODY_PROVIDERS

    assert PROVIDER_NAME in EXTRA_BODY_PROVIDERS


def test_switchyard_is_operator_owned_and_never_a_commercial_table_lookup() -> None:
    assert PROVIDER_NAME in LOCAL_PROVIDERS
    assert lookup_model_capabilities(PROVIDER_NAME, "gpt-6-luna") is None


def test_switchyard_client_refuses_to_retarget_the_commercial_api() -> None:
    """No base_url means the SDK would default to api.openai.com — refuse."""
    with pytest.raises(ValueError, match="requires base_url"):
        create_client(PROVIDER_NAME, base_url="", api_key="")
    client = create_client(PROVIDER_NAME, base_url="http://127.0.0.1:4000/v1", api_key="")
    assert "127.0.0.1:4000" in str(client.base_url)


def test_the_effort_projection_matches_the_surface_the_lane_actually_rides() -> None:
    """The ladder must project switchyard exactly as it projects the sibling name."""
    from turnstone.core.providers._protocol import ModelCapabilities

    caps = ModelCapabilities(
        thinking_mode="template",
        thinking_param="enable_thinking",
        effort_param="reasoning_effort",
        supports_effort=True,
    )
    from turnstone.core.providers.effort_ladder import effort_ladder

    for surface in ("", "chat", "responses"):
        assert effort_ladder(PROVIDER_NAME, caps, surface) == effort_ladder(
            "openai-compatible", caps, surface
        ), f"the ladder diverges from the sibling name on api_surface={surface!r}"


# ---------------------------------------------------------------------------
# Console wiring: the model form gates its "Server compatibility" knobs on the
# provider, so a name the form does not recognise saves a row with no
# api_surface — the operator picks Responses and the lane still rides chat.
# ---------------------------------------------------------------------------


def _console_adapter_providers() -> list[str]:
    """The names admin.js gates its OpenAI-adapter knobs on."""
    match = re.search(
        r"const _OPENAI_ADAPTER_PROVIDERS = \[[^\]]*\];",
        _ADMIN_JS.read_text(encoding="utf-8"),
    )
    assert match, "_OPENAI_ADAPTER_PROVIDERS is gone from admin.js"
    names = re.findall(r'"([^"]+)"', match.group(0))
    assert names, "the console adapter list parsed empty"
    return names


def _console_provider_options() -> list[str]:
    """The provider names the model form actually offers."""
    select = re.search(
        r'<select id="model-provider">(.*?)</select>',
        _INDEX_HTML.read_text(encoding="utf-8"),
        re.S,
    )
    assert select, "the model-provider select is gone from index.html"
    options = re.findall(r'<option value="([^"]*)"', select.group(1))
    assert options, "the provider option list parsed empty"
    return options


def test_the_console_offers_the_switchyard_adapter_and_nothing_unwritable() -> None:
    writable = set(_factory_declared_names())
    options = set(_console_provider_options())
    assert PROVIDER_NAME in options, (
        "the console cannot create a switchyard row, so the adapter is unreachable from the UI"
    )
    assert options <= writable, (
        f"the console offers provider(s) the API refuses: {sorted(options - writable)}"
    )


def test_the_console_adapter_gate_is_the_factory_surface_set() -> None:
    """Both directions — a one-way check passes while the other side drifts."""
    console = set(_console_adapter_providers())
    assert console == set(API_SURFACE_PROVIDERS), (
        "admin.js's adapter gate and the factory's api_surface lanes have drifted: "
        f"console-only {sorted(console - set(API_SURFACE_PROVIDERS))}, "
        f"factory-only {sorted(set(API_SURFACE_PROVIDERS) - console)}"
    )


def test_only_the_gated_names_resolve_api_surface_on_the_wire() -> None:
    """The gate and the factory arms must agree about who honours the knob."""
    for name in sorted(set(_factory_declared_names())):
        chat = type(create_provider(name, api_surface="chat"))
        responses = type(create_provider(name, api_surface="responses"))
        if name in API_SURFACE_PROVIDERS:
            assert chat is not responses, (
                f"{name!r} is gated as an api_surface lane but both surfaces resolve "
                "to the same adapter"
            )
        else:
            assert chat is responses, (
                f"{name!r} is not an api_surface lane, yet the knob changes its adapter"
            )


@node_skip
def test_the_console_model_form_gates_its_knobs_the_way_the_factory_does() -> None:
    """Run the real sliced admin.js gates, not a string match on them."""
    source = _ADMIN_JS.read_text(encoding="utf-8")
    array = re.search(r"const _OPENAI_ADAPTER_PROVIDERS = \[[^\]]*\];", source)
    defaults = re.search(r"const _providerDefaults = \{.*?\n\};", source, re.S)
    assert array and defaults, "the console adapter gate / provider defaults moved"

    lines = [
        "const FIELDS = {",
        '  "model-provider": { value: "" },',
        '  "model-name": { value: "sw-openai-gpt-6-luna" },',
        '  "model-api-surface": { value: "" },',
        '  "model-base-url": { placeholder: "" },',
        '  "model-server-compat-section": { hidden: true },',
        '  "model-server-fields-row": { hidden: false },',
        "};",
        "globalThis.document = { getElementById: (id) => FIELDS[id] || null };",
        "function _updateModelResponseControls() {}",
        "const IDS = {};",
        "function pick(provider, surface) {",
        "  FIELDS['model-provider'].value = provider;",
        "  FIELDS['model-api-surface'].value = surface;",
        "}",
        "function eq(actual, expected, what) {",
        "  if (actual !== expected) throw new Error(what + ': ' + actual + ' !== ' + expected);",
        "}",
        array.group(0),
        defaults.group(0),
        extract_braced(source, "function _isOpenAIAdapter(provider) {"),
        extract_braced(source, "function _modelIdentity() {"),
        extract_braced(source, "function _modelUsesResponsesSurface() {"),
        extract_braced(source, "function _applyProviderDefaults() {"),
    ]

    gated = set(API_SURFACE_PROVIDERS)
    # Names with a defaults entry drive the section's visibility; the others
    # leave it alone (the early return is part of the contract).
    defdefs = set(re.findall(r'^\s{2}"?([a-z-]+)"?: \{', defaults.group(0), re.M))
    names = sorted((set(_factory_declared_names()) | {"anthropic-compatible"}) & defdefs)
    assert names, "no provider defaults parsed — the visibility checks would be vacuous"

    for name in names:
        lit = json.dumps(name)
        lines += [
            f"pick({lit}, 'responses');",
            f"eq(_isOpenAIAdapter({lit}), {str(name in gated).lower()}, 'adapter gate ' + {lit});",
            f"eq(_modelUsesResponsesSurface(), "
            f"{str(name in gated or name == 'openai').lower()}, 'responses surface ' + {lit});",
            f"pick({lit}, 'chat');",
            f"eq(_modelUsesResponsesSurface(), {str(name == 'openai').lower()}, "
            f"'chat surface ' + {lit});",
            f"IDS[{lit}] = _modelIdentity();",
            f"pick({lit}, 'responses');",
            f"eq(_modelIdentity() !== IDS[{lit}], "
            f"{str(name in gated).lower()}, 'surface joins the identity for ' + {lit});",
            f"pick({lit}, '');",
            "FIELDS['model-server-compat-section'].hidden = true;",
            "FIELDS['model-server-fields-row'].hidden = false;",
            "_applyProviderDefaults();",
            f"eq(FIELDS['model-server-compat-section'].hidden, "
            f"{str(not (name in gated or name == 'anthropic-compatible')).lower()}, "
            "'compat section ' + " + lit + ");",
            f"eq(FIELDS['model-server-fields-row'].hidden, "
            f"{str(name == 'anthropic-compatible').lower()}, 'server fields ' + {lit});",
        ]
        if name in gated:
            lines += [
                f"eq(FIELDS['model-base-url'].placeholder, "
                f"_providerDefaults[{lit}].urlPlaceholder, 'url hint ' + {lit});",
                f"eq(FIELDS['model-name'].placeholder, "
                f"_providerDefaults[{lit}].modelPlaceholder, 'model hint ' + {lit});",
            ]

    proc = run_node_source("\n".join(lines))
    assert proc.returncode == 0, (
        "the console model form disagrees with the factory about the Switchyard "
        f"lane. stderr={proc.stderr!r}"
    )
