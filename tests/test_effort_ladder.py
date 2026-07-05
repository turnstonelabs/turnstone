"""Tests for the effective effort-ladder projection.

The ladder must mirror the request-time mapping functions exactly —
equal ``effective`` tokens promise byte-identical effort behavior on
the wire, which is what the UI annotations lean on.
"""

from __future__ import annotations

from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.providers.effort_ladder import (
    KNOB_VALUES,
    effort_ladder,
    effort_ladder_for_model,
)


def _as_map(ladder: list[dict[str, str]]) -> dict[str, str]:
    assert [r["value"] for r in ladder] == list(KNOB_VALUES)
    return {r["value"]: r["effective"] for r in ladder}


class TestLocalLanes:
    def test_qwen_style_toggle_only_two_groups(self) -> None:
        """Toggle-only model: none=off, everything else one 'on' group."""
        caps = ModelCapabilities(thinking_mode="manual", thinking_param="enable_thinking")
        eff = _as_map(effort_ladder("anthropic-compatible", caps))
        assert eff["none"] == "off"
        assert {eff[k] for k in KNOB_VALUES if k != "none"} == {"on"}

    def test_freeform_effort_param_forwards_each_value(self) -> None:
        """deepseek-style config: toggle + verbatim effort per position."""
        caps = ModelCapabilities(
            thinking_mode="manual",
            thinking_param="thinking",
            effort_param="reasoning_effort",
        )
        eff = _as_map(effort_ladder("anthropic-compatible", caps))
        assert eff["none"] == "off"
        assert eff["low"] == "on+low"
        assert eff["max"] == "on+max"

    def test_validated_effort_param_shows_snapping(self) -> None:
        """Declared values collapse off-list positions onto the default."""
        caps = ModelCapabilities(
            thinking_mode="manual",
            thinking_param="enable_thinking",
            effort_param="reasoning_effort",
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        )
        eff = _as_map(effort_ladder("openai-compatible", caps))
        assert eff["xhigh"] == "on+medium"
        assert eff["max"] == "on+medium"
        assert eff["high"] == "on+high"

    def test_openai_compatible_flat_param_without_effort_param(self) -> None:
        caps = ModelCapabilities(
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        )
        eff = _as_map(effort_ladder("openai-compatible", caps))
        assert eff["none"] == "default"
        assert eff["high"] == "high"
        assert eff["xhigh"] == "medium"

    def test_adaptive_local_never_off(self) -> None:
        caps = ModelCapabilities(thinking_mode="adaptive", thinking_param="enable_thinking")
        eff = _as_map(effort_ladder("openai-compatible", caps))
        assert eff["none"] == "on"
        assert eff["max"] == "on"


class TestNativeAnthropicLane:
    def test_adaptive_with_effort_levels(self) -> None:
        caps = ModelCapabilities(
            thinking_mode="adaptive",
            supports_effort=True,
            effort_levels=("low", "medium", "high", "xhigh", "max"),
        )
        eff = _as_map(effort_ladder("anthropic", caps))
        assert eff["none"] == "adaptive"  # thinking on, model decides
        assert eff["minimal"] == "adaptive"  # unmapped knob level
        assert eff["low"] == "low"
        assert eff["max"] == "max"

    def test_manual_budget_ladder(self) -> None:
        caps = ModelCapabilities(thinking_mode="manual")
        eff = _as_map(effort_ladder("anthropic", caps))
        assert eff["none"] == "off"
        assert eff["low"] == "budget:1024"
        assert eff["medium"] == "budget:4096"
        assert eff["high"] == "budget:16384"
        # Off-map knob levels share the default budget — aliased group.
        assert eff["minimal"] == eff["xhigh"] == eff["max"] == "budget:4096"


class TestFlatParamLanes:
    def test_google_default_caps(self) -> None:
        eff = _as_map(effort_ladder_for_model("google", "gemini-3-flash", None))
        assert eff["none"] == "default"
        assert eff["minimal"] == "minimal"
        assert eff["high"] == "high"
        assert eff["xhigh"] == eff["max"] == "high"

    def test_google_override_routes_through_chat_lane(self) -> None:
        """GoogleProvider inherits _finalize_extra_body — a thinking_mode
        override changes real requests, and the ladder must mirror it."""
        eff = _as_map(
            effort_ladder_for_model(
                "google",
                "gemini-3-flash",
                {"thinking_mode": "manual", "thinking_param": "enable_thinking"},
            )
        )
        assert eff["none"] == "off"
        assert eff["medium"] == "on+medium"  # toggle + inherited flat param

    def test_responses_surface_projects_flat_only(self) -> None:
        caps_overrides = {
            "thinking_mode": "manual",
            "reasoning_effort_values": ["low", "medium", "high"],
        }
        chat = _as_map(effort_ladder_for_model("openai-compatible", "m", caps_overrides))
        responses = _as_map(
            effort_ladder_for_model(
                "openai-compatible", "m", caps_overrides, api_surface="responses"
            )
        )
        assert chat["medium"] == "on+medium"
        assert responses["medium"] == "medium"
        assert responses["none"] == "default"

    def test_xai_projects_flat_only(self) -> None:
        """grok-4.3 declares values (none/low/medium/high, default low);
        off-list knob positions snap to the default."""
        eff = _as_map(effort_ladder_for_model("xai", "grok-4.3", None))
        assert eff["none"] == "default"  # resolve_ never forwards "none"
        assert eff["low"] == "low"
        assert eff["high"] == "high"
        assert eff["xhigh"] == eff["max"] == "low"

    def test_xai_ignores_template_overrides(self) -> None:
        """XAIProvider subclasses OpenAIResponsesProvider, which drops
        extra_body — a thinking_mode/effort_param override cannot change
        an xai request, so it must not change the ladder either."""
        eff = _as_map(
            effort_ladder_for_model(
                "xai",
                "grok-4.3",
                {
                    "thinking_mode": "manual",
                    "thinking_param": "enable_thinking",
                    "effort_param": "reasoning_effort",
                },
            )
        )
        assert eff["none"] == "default"  # not "off" — there is no toggle
        assert eff["medium"] == "medium"
        assert all("+" not in v and v not in ("on", "off") for v in eff.values())

    def test_anthropic_effort_applies_even_with_thinking_mode_none(self) -> None:
        """output_config gates on supports_effort alone at request time."""
        caps = ModelCapabilities(
            thinking_mode="none",
            supports_effort=True,
            effort_levels=("low", "medium", "high"),
        )
        eff = _as_map(effort_ladder("anthropic", caps))
        assert eff["high"] == "high"
        assert eff["none"] == "default"

    def test_overrides_merge_and_unknown_keys_ignored(self) -> None:
        eff = _as_map(
            effort_ladder_for_model(
                "google",
                "gemini-3-flash",
                {"reasoning_effort_values": [], "not_a_field": True},
            )
        )
        # Operator cleared the values → nothing effort-related is sent.
        assert set(eff.values()) == {"default"}
