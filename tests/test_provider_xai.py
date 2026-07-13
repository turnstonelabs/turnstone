"""Tests for the xAI / Grok provider.

Covers the boundaries the new code adds:

* Capability-table prefix-match on ``GROK_CAPABILITIES`` (aliases like
  ``grok-4.3-latest`` resolve to the documented ``grok-4.3`` row).
* ``XAIProvider._build_kwargs`` merging ``<tool>_call_output`` strings
  into ``include[]`` alongside the inherited ``reasoning.encrypted_content``
  entry, so xAI's hidden server-tool outputs become visible.
* ``resolve_server_side_tools`` folding the legacy
  ``supports_web_search`` boolean into the effective tuple.
* ``extra_headers`` forwarding through ``OpenAIResponsesProvider`` and
  ``OpenAIChatCompletionsProvider`` (Anthropic also accepts the kwarg;
  its streaming-context-manager shape is exercised by its own existing
  tests).
* ``model_registry._detect_openai_compat`` setting ``server_type="xai"``
  for ``api.x.ai`` and its subdomains (and not for look-alikes).
* End-to-end wiring via ``create_provider("xai")`` /
  ``create_client("xai", ...)`` / ``list_known_models("xai")`` /
  ``lookup_model_capabilities("xai", ...)``.

All tests drive through the real provider; only the OpenAI/Anthropic
SDK boundary is mocked, and the mock records call kwargs so the body
shape can be inspected (per the project's
``feedback_mock_transport_body_inspection`` rule).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from turnstone.core.model_registry import _detect_openai_compat, _select_best_model
from turnstone.core.providers import (
    create_client,
    create_provider,
    list_known_models,
    lookup_model_capabilities,
)
from turnstone.core.providers._openai_common import resolve_server_side_tools
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.providers._xai import (
    _GROK_DEFAULT,
    GROK_CAPABILITIES,
    XAI_DEFAULT_BASE_URL,
    XAIProvider,
    lookup_grok_capabilities,
)


@pytest.fixture
def provider() -> XAIProvider:
    return XAIProvider()


# ---------------------------------------------------------------------------
# Capability table
# ---------------------------------------------------------------------------


class TestCapabilityTable:
    def test_exact_match_grok_4_3(self) -> None:
        caps = lookup_grok_capabilities("grok-4.3")
        assert caps is GROK_CAPABILITIES["grok-4.3"]
        assert caps.context_window == 1_000_000
        assert caps.reasoning_effort_values == ("none", "low", "medium", "high")
        assert caps.default_reasoning_effort == "low"
        assert caps.supports_reasoning_replay is True
        assert caps.server_side_tools == ("web_search",)

    def test_latest_alias_resolves_via_longest_prefix(self) -> None:
        # `grok-4.3-latest` is documented as an accepted alias.  The
        # longest-prefix lookup must route it to the `grok-4.3` row
        # rather than falling through to GROK_DEFAULT or matching some
        # shorter prefix.
        assert lookup_grok_capabilities("grok-4.3-latest") is GROK_CAPABILITIES["grok-4.3"]

    def test_dated_snapshot_resolves(self) -> None:
        # Dated snapshots (`grok-4.20-0309-*`) appear as explicit
        # entries; bare prefix-match returns them.
        caps = lookup_grok_capabilities("grok-4.20-0309-reasoning")
        assert caps is GROK_CAPABILITIES["grok-4.20-0309-reasoning"]

    def test_multi_agent_effort_uses_xhigh(self) -> None:
        caps = lookup_grok_capabilities("grok-4.20-multi-agent-0309")
        # Effort controls agent count on this variant per xAI docs;
        # only the multi-agent table exposes `xhigh`.
        assert "xhigh" in caps.reasoning_effort_values

    def test_unknown_model_returns_default_identity(self) -> None:
        # Identity check matters: lookup_model_capabilities relies on
        # `caps is default` to return None for unknown rows.
        assert lookup_grok_capabilities("grok-x-unreleased") is _GROK_DEFAULT
        assert lookup_grok_capabilities("") is _GROK_DEFAULT

    def test_pdf_stays_a_rasterize_fallback(self) -> None:
        # supports_pdf is intentionally False on every Grok row: xAI's document
        # support is an agentic attachment_search workflow over Files-API uploads
        # (file_id / file_url), not the inline base64 document ingestion our
        # native path emits — so Grok PDFs take the rasterize-to-vision fallback.
        # Flipping this without wiring the Files-API upload flow would send xAI a
        # wire shape it can't read; see the note above GROK_CAPABILITIES in
        # _xai.py and docs.x.ai/developers/model-capabilities/files/chat-with-files.
        for caps in GROK_CAPABILITIES.values():
            assert caps.supports_pdf is False
        assert _GROK_DEFAULT.supports_pdf is False


# ---------------------------------------------------------------------------
# resolve_server_side_tools — legacy supports_web_search fold
# ---------------------------------------------------------------------------


class TestResolveServerSideTools:
    def test_explicit_tuple_used_directly(self) -> None:
        caps = ModelCapabilities(server_side_tools=("web_search", "x_search"))
        assert resolve_server_side_tools(caps) == ["web_search", "x_search"]

    def test_legacy_supports_web_search_appends_when_missing(self) -> None:
        # Capability rows that only set the legacy boolean still get
        # `web_search` injected by the helper.
        caps = ModelCapabilities(supports_web_search=True)
        assert resolve_server_side_tools(caps) == ["web_search"]

    def test_legacy_flag_does_not_duplicate(self) -> None:
        caps = ModelCapabilities(
            supports_web_search=True,
            server_side_tools=("web_search",),
        )
        result = resolve_server_side_tools(caps)
        assert result == ["web_search"]

    def test_neither_flag_returns_empty(self) -> None:
        assert resolve_server_side_tools(ModelCapabilities()) == []

    def test_returned_list_is_independent_copy(self) -> None:
        # Callers mutate the result (the OpenAIResponsesProvider
        # injection appends `_call_output` strings in xAI's override);
        # the helper must not hand back a shared reference.
        caps = ModelCapabilities(server_side_tools=("web_search",))
        first = resolve_server_side_tools(caps)
        first.append("x_search")
        second = resolve_server_side_tools(caps)
        assert second == ["web_search"]


# ---------------------------------------------------------------------------
# XAIProvider._build_kwargs — include[] merge
# ---------------------------------------------------------------------------


class TestBuildKwargs:
    def test_include_merges_call_output_with_encrypted_content(self, provider: XAIProvider) -> None:
        kwargs = provider._build_kwargs(
            model="grok-4.3",
            messages=[{"role": "user", "content": "hi"}],
            # web_search def present → replace-only injection fires → the
            # call_output include is forwarded (contrast the suppression test).
            tools=[{"type": "function", "function": {"name": "web_search"}}],
            max_tokens=512,
            temperature=0.5,
            reasoning_effort="low",
            deferred_names=None,
            capabilities=None,
            replay_reasoning_to_model=True,
        )
        includes = kwargs.get("include") or []
        # Both must be present; order matters less than the union.
        assert "reasoning.encrypted_content" in includes
        assert "web_search_call_output" in includes

    def test_include_omits_encrypted_content_when_replay_false(self, provider: XAIProvider) -> None:
        kwargs = provider._build_kwargs(
            model="grok-4.3",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "web_search"}}],
            max_tokens=512,
            temperature=0.5,
            reasoning_effort="low",
            deferred_names=None,
            capabilities=None,
            replay_reasoning_to_model=False,
        )
        includes = kwargs.get("include") or []
        assert "reasoning.encrypted_content" not in includes
        # `*_call_output` still added (independent of the replay flag) because
        # the web_search def survived and the native tool was injected.
        assert "web_search_call_output" in includes

    def test_call_output_include_suppressed_when_tool_not_injected(
        self, provider: XAIProvider
    ) -> None:
        # Orphan-include guard: with the web_search client def hidden (persona /
        # coordinator visibility set), the base does NOT inject the native tool,
        # so xAI must not forward a web_search_call_output include for a tool
        # absent from `tools`.
        kwargs = provider._build_kwargs(
            model="grok-4.3",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
            max_tokens=512,
            temperature=0.5,
            reasoning_effort="low",
            deferred_names=None,
            capabilities=None,
            replay_reasoning_to_model=True,
        )
        includes = kwargs.get("include") or []
        assert "web_search_call_output" not in includes
        assert {"type": "web_search"} not in (kwargs.get("tools") or [])

    def test_include_omitted_when_no_server_side_tools(self, provider: XAIProvider) -> None:
        # Custom caps row with no server-side tools and no legacy
        # web-search flag — include[] should carry only the
        # encrypted_content entry (gated by replay).
        bare_caps = ModelCapabilities(supports_reasoning_replay=True)
        kwargs = provider._build_kwargs(
            model="grok-bare-test",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=512,
            temperature=0.5,
            reasoning_effort="low",
            deferred_names=None,
            capabilities=bare_caps,
            replay_reasoning_to_model=True,
        )
        includes = kwargs.get("include") or []
        assert includes == ["reasoning.encrypted_content"]

    def test_web_search_tool_injected_into_tools_list(self, provider: XAIProvider) -> None:
        # The inherited generalised injection in
        # OpenAIResponsesProvider._build_kwargs walks server_side_tools;
        # grok-4.3 declares `("web_search",)`.  Injection is replace-only:
        # it stands in for a client web_search def that survived the
        # session's visibility filter, so the def must be present.
        kwargs = provider._build_kwargs(
            model="grok-4.3",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "web_search"}}],
            max_tokens=512,
            temperature=0.5,
            reasoning_effort="low",
            deferred_names=None,
            capabilities=None,
            replay_reasoning_to_model=False,
        )
        tools = kwargs.get("tools") or []
        assert {"type": "web_search"} in tools

    def test_web_search_not_injected_without_client_def(self, provider: XAIProvider) -> None:
        # A request whose envelope hides web_search (persona visibility
        # set, tool-less utility call) gains no native search.
        kwargs = provider._build_kwargs(
            model="grok-4.3",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=512,
            temperature=0.5,
            reasoning_effort="low",
            deferred_names=None,
            capabilities=None,
            replay_reasoning_to_model=False,
        )
        tools = kwargs.get("tools") or []
        assert {"type": "web_search"} not in tools


# ---------------------------------------------------------------------------
# extra_headers — protocol passthrough
# ---------------------------------------------------------------------------


class TestExtraHeadersForwarding:
    """The session layer doesn't populate ``extra_headers`` yet, but the
    plumbing must be in place so a future change wiring
    ``x-grok-conv-id`` for cache hinting reaches the SDK boundary."""

    def test_responses_streaming_forwards_extra_headers(self, provider: XAIProvider) -> None:
        client = MagicMock()
        client.responses.create.return_value = iter([])
        # Consume the iterator so the underlying call is made eagerly.
        list(
            provider.create_streaming(
                client=client,
                model="grok-4.3",
                messages=[{"role": "user", "content": "hi"}],
                extra_headers={"x-grok-conv-id": "ws_abc"},
            )
        )
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs.get("extra_headers") == {"x-grok-conv-id": "ws_abc"}

    def test_responses_streaming_omits_when_none(self, provider: XAIProvider) -> None:
        client = MagicMock()
        client.responses.create.return_value = iter([])
        list(
            provider.create_streaming(
                client=client,
                model="grok-4.3",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        kwargs = client.responses.create.call_args.kwargs
        assert "extra_headers" not in kwargs

    def test_responses_streaming_forwards_conv_id_header(self, provider: XAIProvider) -> None:
        client = MagicMock()
        client.responses.create.return_value = iter([])
        list(
            provider.create_streaming(
                client=client,
                model="grok-4.3",
                messages=[{"role": "user", "content": "hi"}],
                extra_headers={"x-grok-conv-id": "ws_xyz"},
            )
        )
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs.get("extra_headers") == {"x-grok-conv-id": "ws_xyz"}

    def test_chat_streaming_forwards_extra_headers(self) -> None:
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        chat_provider = OpenAIChatCompletionsProvider()
        client = MagicMock()
        client.chat.completions.create.return_value = iter([])
        list(
            chat_provider.create_streaming(
                client=client,
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                extra_headers={"x-custom": "value"},
            )
        )
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs.get("extra_headers") == {"x-custom": "value"}


# ---------------------------------------------------------------------------
# Hostname detection — model_registry._detect_openai_compat
# ---------------------------------------------------------------------------


class TestHostnameDetection:
    def _detect(self, base_url: str) -> str | None:
        result: dict[str, object] = {"context_window": None, "server_type": None}
        _detect_openai_compat(result, model_obj=None, model_id="grok-4.3", base_url=base_url)
        return result["server_type"]  # type: ignore[return-value]

    def test_api_x_ai_resolves_to_xai(self) -> None:
        assert self._detect("https://api.x.ai/v1") == "xai"

    def test_subdomain_x_ai_resolves_to_xai(self) -> None:
        assert self._detect("https://eu.api.x.ai/v1") == "xai"

    def test_lookalike_host_not_matched(self) -> None:
        # `evil-x.ai` and `x.ai.attacker.com` must not collide with the
        # `.x.ai` suffix check.  The hostname check is `endswith(".x.ai")`
        # — a leading-dot anchor avoids matching `notx.ai` etc., but a
        # full hostname *ending* in `.x.ai` is still matched; that's
        # the intent (any subdomain of x.ai).  This test asserts the
        # negative case where the suffix is not preceded by a dot.
        assert self._detect("https://evil-x.ai/v1") != "xai"

    def test_unrelated_hostname_falls_through(self) -> None:
        # Should pick up the openai-compatible default for an
        # unrecognised host.
        assert self._detect("https://example.test/v1") == "openai-compatible"


# ---------------------------------------------------------------------------
# End-to-end wiring
# ---------------------------------------------------------------------------


class TestProviderRegistration:
    def test_create_provider_returns_xai_singleton(self) -> None:
        prov_1 = create_provider("xai")
        prov_2 = create_provider("xai")
        assert prov_1 is prov_2
        assert prov_1.provider_name == "xai"

    def test_create_client_defaults_to_xai_base_url(self) -> None:
        # Without an explicit base_url, the factory should inject
        # XAI_DEFAULT_BASE_URL so callers don't have to know it.
        client = create_client("xai", base_url="", api_key="xai-test-key")
        # The openai-python SDK exposes `base_url` as a string-y attribute.
        assert XAI_DEFAULT_BASE_URL.rstrip("/") in str(client.base_url)

    def test_list_known_models_returns_documented_set(self) -> None:
        known = list_known_models("xai")
        assert "grok-4.3" in known
        assert "grok-4.20-multi-agent-0309" in known
        assert "grok-build-0.1" in known

    def test_lookup_model_capabilities_resolves_known(self) -> None:
        caps = lookup_model_capabilities("xai", "grok-4.3")
        assert caps is not None
        assert caps["context_window"] == 1_000_000

    def test_lookup_model_capabilities_returns_none_for_unknown(self) -> None:
        assert lookup_model_capabilities("xai", "grok-x-unreleased") is None


# ---------------------------------------------------------------------------
# _select_best_model — version-tuple ordering
# ---------------------------------------------------------------------------


class TestSelectBestModel:
    """Verify dotted-version sorting uses tuple-of-ints, not float.

    ``float("4.20") == 4.2``, so the float-based sort would route
    ``grok-4.20`` (newer dated-snapshot line) under ``grok-4.3``.  The
    fix parses each segment as an int so ``(4, 20) > (4, 3)`` as
    intended.  Same fix applied symmetrically to the openai branch
    guards against a future ``gpt-5.10`` regression."""

    def test_xai_prefers_higher_minor_version(self) -> None:
        # The bug: float("4.20") == 4.2 < 4.3, so the broken sort
        # picked grok-4.3 over grok-4.20.  The fix routes correctly.
        assert _select_best_model(["grok-4", "grok-4.3", "grok-4.20"], "xai") == "grok-4.20"

    def test_xai_bare_major_below_dotted(self) -> None:
        # (4,) < (4, 3) under tuple comparison, so a bare-major alias
        # is correctly ordered below any minor-versioned sibling.
        assert _select_best_model(["grok-4", "grok-4.3"], "xai") == "grok-4.3"

    def test_xai_falls_back_when_no_base_match(self) -> None:
        # No base-versioned entry → first model returned.  Mirrors the
        # openai/anthropic fallback at end of _select_best_model.
        assert (
            _select_best_model(["grok-4.20-0309-reasoning", "grok-build-0.1"], "xai")
            == "grok-4.20-0309-reasoning"
        )

    def test_openai_prefers_higher_minor_version(self) -> None:
        # Symmetric guard against future gpt-5.10 vs gpt-5.2 confusion.
        assert _select_best_model(["gpt-5", "gpt-5.2", "gpt-5.10"], "openai") == "gpt-5.10"
