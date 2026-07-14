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
    def test_toggle_engaged_carries_graded_value_per_position(self) -> None:
        """No declared effort key: the toggle rides the knob AND the graded
        value is forwarded under the fallback template key — the user's
        effort setting always reaches the wire (a template that doesn't
        reference the kwarg ignores it), so every position is distinct."""
        caps = ModelCapabilities(thinking_mode="manual", thinking_param="enable_thinking")
        eff = _as_map(effort_ladder("anthropic-compatible", caps))
        assert eff["none"] == "off"
        assert eff["minimal"] == "on+minimal"
        assert eff["max"] == "on+max"
        assert len({eff[k] for k in KNOB_VALUES}) == len(KNOB_VALUES)

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
        """Off-list positions round up onto the declared values; above the
        ceiling they ride the ceiling — never the (possibly lower) default."""
        caps = ModelCapabilities(
            thinking_mode="manual",
            thinking_param="enable_thinking",
            effort_param="reasoning_effort",
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        )
        eff = _as_map(effort_ladder("openai-compatible", caps))
        assert eff["minimal"] == "on+low"
        assert eff["high"] == "on+high"
        assert eff["xhigh"] == "on+high"
        assert eff["max"] == "on+high"

    def test_openai_compatible_flat_param_without_effort_param(self) -> None:
        caps = ModelCapabilities(
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        )
        eff = _as_map(effort_ladder("openai-compatible", caps))
        assert eff["none"] == "default"
        assert eff["high"] == "high"
        assert eff["xhigh"] == "high"  # ceiling, not default

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
        assert eff["minimal"] == "low"  # rounds up onto the declared levels
        assert eff["low"] == "low"
        assert eff["max"] == "max"

    def test_sonnet_5_registry_row(self) -> None:
        """claude-sonnet-5: adaptive + full effort ladder incl. xhigh/max —
        every knob level above none is a distinct wire behavior."""
        eff = _as_map(effort_ladder_for_model("anthropic", "claude-sonnet-5", None))
        assert eff["none"] == "adaptive"
        assert eff["minimal"] == "low"  # rounds up onto declared levels
        assert eff["low"] == "low"
        assert eff["xhigh"] == "xhigh"
        assert eff["max"] == "max"

    def test_sonnet_4_6_xhigh_rides_max(self) -> None:
        """Sonnet 4.6 declares (low, medium, high, max) — no xhigh, so the
        knob's xhigh snaps up onto max rather than down onto high."""
        eff = _as_map(effort_ladder_for_model("anthropic", "claude-sonnet-4-6", None))
        assert eff["high"] == "high"
        assert eff["xhigh"] == "max"
        assert eff["max"] == "max"

    def test_manual_budget_ladder(self) -> None:
        """Budgets are monotone over the whole knob domain."""
        caps = ModelCapabilities(thinking_mode="manual")
        eff = _as_map(effort_ladder("anthropic", caps))
        assert eff["none"] == "off"
        assert eff["minimal"] == eff["low"] == "budget:1024"  # 1024 = API floor
        assert eff["medium"] == "budget:4096"
        assert eff["high"] == "budget:16384"
        assert eff["xhigh"] == "budget:32768"
        assert eff["max"] == "budget:65536"


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
        knob positions above the ceiling ride the ceiling (high).  The
        declared "none" IS forwarded for the knob's off position (xAI
        documents it as disabling reasoning) but is never a snap target
        for other positions."""
        eff = _as_map(effort_ladder_for_model("xai", "grok-4.3", None))
        assert eff["none"] == "none"  # explicit disable, declared by grok
        assert eff["minimal"] == "low"
        assert eff["low"] == "low"
        assert eff["high"] == "high"
        assert eff["xhigh"] == eff["max"] == "high"

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
        assert eff["none"] == "none"  # flat channel, not an "off" toggle
        assert eff["medium"] == "medium"
        assert all("+" not in v and v not in ("on", "off") for v in eff.values())

    def test_openai_gpt55_registry_row(self) -> None:
        """gpt-5.5 declares none/low/medium/high/xhigh with default medium:
        knob none sends the explicit "none" level (server default is
        MEDIUM, so omission would not disable), max rides the xhigh
        ceiling, minimal rounds up to low."""
        eff = _as_map(effort_ladder_for_model("openai", "gpt-5.5", None))
        assert eff["none"] == "none"
        assert eff["minimal"] == "low"
        assert eff["xhigh"] == "xhigh"
        assert eff["max"] == "xhigh"

    def test_openai_always_reasoning_row_snaps_without_none(self) -> None:
        """Always-reasoning rows (gpt-5.4-pro: medium/high/xhigh) declare no
        "none" level, so the knob's off position omits the param and low
        positions snap UP onto the declared floor."""
        eff = _as_map(effort_ladder_for_model("openai", "gpt-5.4-pro", None))
        assert eff["none"] == "default"
        assert eff["minimal"] == "medium"
        assert eff["medium"] == "medium"
        assert eff["max"] == "xhigh"

    def test_openai_pro_row_wins_longest_prefix(self) -> None:
        """gpt-5.4-pro must not prefix-fall onto the gpt-5.4 row (which
        declares "none") — the pro ladder has no off position, so the
        longest-prefix row must win or the knob would wrongly omit."""
        eff = _as_map(effort_ladder_for_model("openai", "gpt-5.4-pro", None))
        assert eff["none"] == "default"
        base = _as_map(effort_ladder_for_model("openai", "gpt-5.4", None))
        assert base["none"] == "none"

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
