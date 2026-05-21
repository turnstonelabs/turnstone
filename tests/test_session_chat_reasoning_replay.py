"""Session-level integration tests for Phase 5 (Chat Completions
``reasoning`` field replay against vLLM).

Phase 5 is the only reasoning-replay path that does NOT use the static
``supports_reasoning_replay`` capability gate.  It's a parallel path to
Paths 1+2, gated entirely at the session level on three conditions:

1. Provider is ``OpenAIChatCompletionsProvider``.
2. ``server_compat.server_type == "vllm"``.
3. Operator-set ``ModelConfig.replay_reasoning_to_model`` is True.

These tests drive through ``ChatSession._maybe_attach_vllm_chat_reasoning``
to pin each gate independently, then one round-trip test through the real
OpenAI Python SDK + httpx MockTransport confirms the ``reasoning`` field
actually reaches the wire bytes (the SDK-boundary guarantee that the
session-level attach approach hinges on).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests._session_helpers import make_session as _make_session
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider


def _vllm_registry(*, replay: bool = True, alias: str = "qwen3") -> Any:
    """Stub registry with a vLLM-typed server_compat profile and the
    Phase 5 operator flag toggleable.

    Mirrors production ModelConfig shape: ``server_compat`` lives at
    the top-level dataclass field, NOT inside ``capabilities``.  Both
    model_registry loader paths (DB row at line 401, config.toml at
    line 485) ``caps.pop("server_compat", {})`` and hoist it up, so a
    stub that populates ``capabilities["server_compat"]`` would mask
    the same bug Phase 5 stepped on initially.
    """

    cfg = SimpleNamespace(
        replay_reasoning_to_model=replay,
        capabilities={},
        server_compat={"server_type": "vllm"},
    )
    return SimpleNamespace(
        get_config=lambda a: cfg if a == alias else (_ for _ in ()).throw(KeyError(a)),
    )


def _registry_with_server_type(server_type: str, *, replay: bool = True) -> Any:
    cfg = SimpleNamespace(
        replay_reasoning_to_model=replay,
        capabilities={},
        server_compat={"server_type": server_type},
    )
    return SimpleNamespace(
        get_config=lambda _alias: cfg,
    )


def _assistant_msg_with_thinking(text: str = "let me think") -> dict[str, Any]:
    """Anthropic-shape persisted reasoning — the cross-provider case
    where workstream started on Anthropic and operator flipped to
    vLLM-served Qwen3.  Helper must extract the text and discard the
    Anthropic signature."""

    return {
        "role": "assistant",
        "content": "Final answer.",
        "_provider_content": [
            {"type": "thinking", "thinking": text, "signature": "sig"},
            {"type": "text", "text": "Final answer."},
        ],
    }


# ---------------------------------------------------------------------------
# Gate tests via ``_maybe_attach_vllm_chat_reasoning`` directly
# ---------------------------------------------------------------------------


class TestMaybeAttachVllmChatReasoningGates:
    """The session-level method that combines all three Phase 5 gates."""

    def test_all_gates_pass_attaches_reasoning(self) -> None:
        session = _make_session()
        session._registry = _vllm_registry(replay=True)
        session._model_alias = "qwen3"
        provider = OpenAIChatCompletionsProvider()

        msgs = [{"role": "user", "content": "q"}, _assistant_msg_with_thinking("CoT")]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert out[1]["reasoning"] == "CoT"

    def test_non_chat_completions_provider_is_no_op(self) -> None:
        # Provider isinstance gate: Anthropic / Responses / Google all
        # have their own reasoning-replay paths (Paths 1 / 2) — Phase 5
        # must not double-attach.
        session = _make_session()
        session._registry = _vllm_registry(replay=True)
        session._model_alias = "qwen3"
        provider = AnthropicProvider()

        msgs = [_assistant_msg_with_thinking()]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out[0]
        # Same reference — no copy made.
        assert out[0] is msgs[0]

    def test_openai_responses_provider_is_no_op(self) -> None:
        # OpenAIResponsesProvider is a top-level class (not a subclass of
        # OpenAIChatCompletionsProvider) — the isinstance gate rejects
        # it cleanly.  This is the load-bearing distinction; an
        # accidental inheritance refactor would break the gate.
        session = _make_session()
        session._registry = _vllm_registry(replay=True)
        session._model_alias = "qwen3"
        provider = OpenAIResponsesProvider()

        msgs = [_assistant_msg_with_thinking()]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out[0]

    @pytest.mark.parametrize("server_type", ["", "llama.cpp", "sglang", "openai", "unknown"])
    def test_non_vllm_server_type_is_no_op(self, server_type: str) -> None:
        # Server-type pin bounds blast radius — canonical OpenAI Chat
        # Completions, llama.cpp, sglang, and any unrecognised server
        # never receive the non-standard ``reasoning`` field.
        session = _make_session()
        session._registry = _registry_with_server_type(server_type, replay=True)
        session._model_alias = "some-model"
        provider = OpenAIChatCompletionsProvider()

        msgs = [_assistant_msg_with_thinking()]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out[0]

    def test_operator_flag_off_is_no_op(self) -> None:
        session = _make_session()
        session._registry = _vllm_registry(replay=False)  # operator flag OFF
        session._model_alias = "qwen3"
        provider = OpenAIChatCompletionsProvider()

        msgs = [_assistant_msg_with_thinking()]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out[0]

    def test_missing_registry_is_no_op(self) -> None:
        session = _make_session()
        session._registry = None
        session._model_alias = "qwen3"
        provider = OpenAIChatCompletionsProvider()

        msgs = [_assistant_msg_with_thinking()]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out[0]

    def test_missing_alias_is_no_op(self) -> None:
        session = _make_session()
        session._registry = _vllm_registry(replay=True)
        session._model_alias = ""
        provider = OpenAIChatCompletionsProvider()

        msgs = [_assistant_msg_with_thinking()]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out[0]

    def test_registry_exception_is_no_op(self) -> None:
        # Defensive: registry lookup raising must degrade to no-attach,
        # not break the call.  Conservative default — operator can
        # always re-flip the flag once the registry is healthy.
        def boom(_alias: str) -> Any:
            raise KeyError("missing")

        session = _make_session()
        session._registry = SimpleNamespace(get_config=boom)
        session._model_alias = "qwen3"
        provider = OpenAIChatCompletionsProvider()

        msgs = [_assistant_msg_with_thinking()]
        out = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out[0]

    def test_explicit_alias_arg_overrides_session_default(self) -> None:
        # When _try_stream forwards an explicit ``model_alias`` (different
        # from the session's primary), the helper must read THAT alias'
        # config — not the session's primary.  Mirrors the per-alias
        # behaviour pinned for _resolve_replay_reasoning_to_model.
        def per_alias(alias: str) -> Any:
            return SimpleNamespace(
                replay_reasoning_to_model=(alias == "wants-replay"),
                capabilities={},
                server_compat={"server_type": "vllm"},
            )

        session = _make_session()
        session._registry = SimpleNamespace(get_config=per_alias)
        session._model_alias = "primary"
        provider = OpenAIChatCompletionsProvider()

        msgs = [_assistant_msg_with_thinking()]
        # Default alias → flag off → no attach.
        out_default = session._maybe_attach_vllm_chat_reasoning(msgs, provider)
        assert "reasoning" not in out_default[0]
        # Explicit alias arg → flag on → attached.
        out_explicit = session._maybe_attach_vllm_chat_reasoning(msgs, provider, "wants-replay")
        assert out_explicit[0]["reasoning"] == "let me think"


# ---------------------------------------------------------------------------
# End-to-end: SDK passthrough is the load-bearing assumption.  Verify it
# with a real OpenAI client wired against an httpx MockTransport that
# inspects the body (per feedback_mock_transport_body_inspection).
# ---------------------------------------------------------------------------


class TestReasoningFieldReachesWireBytes:
    """One round-trip test through the real OpenAI Python SDK confirms
    the ``reasoning`` field on an assistant message dict survives the
    sanitize_messages strip (only ``_``-prefixed keys are dropped) AND
    the SDK's TypedDict input shape (no runtime field filtering)."""

    def _capture_client(self) -> tuple[Any, list[dict[str, Any]]]:
        from openai import OpenAI

        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8") if request.content else ""
            captured.append({"url": str(request.url), "body": body})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-vllm-spike",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "qwen3-test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        client = OpenAI(
            api_key="sk-test",
            base_url="http://mock.local/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        return client, captured

    def test_reasoning_field_present_in_wire_body_when_attached(self) -> None:
        # Send messages that have the Phase 5 ``reasoning`` field
        # attached.  Drive a real provider call through the real OpenAI
        # SDK + mock httpx and verify the field is in the captured POST
        # body — the SDK passthrough assumption that the entire
        # session-level approach hinges on.
        client, captured = self._capture_client()
        provider = OpenAIChatCompletionsProvider()

        # Mimic the post-attach message shape that
        # ``_maybe_attach_vllm_chat_reasoning`` produces, then sanitize.
        # ``sanitize_messages`` runs inside provider._prepare_messages
        # and must preserve the non-``_``-prefixed ``reasoning`` field.
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "Final answer.",
                "reasoning": "vLLM-shaped CoT text",
                "_provider_content": [{"type": "reasoning_text", "text": "vLLM-shaped CoT text"}],
            },
            {"role": "user", "content": "follow-up"},
        ]

        provider.create_completion(
            client=client,
            model="qwen3-test",
            messages=messages,
            max_tokens=10,
            temperature=0.5,
            reasoning_effort="medium",
            extra_params=None,
            capabilities=provider.get_capabilities("qwen3-test"),
        )

        assert captured, "no request captured"
        body = json.loads(captured[0]["body"])
        assistant_msg = next(m for m in body["messages"] if m["role"] == "assistant")
        # Wire-format guarantee: field survives sanitize_messages + SDK.
        assert assistant_msg.get("reasoning") == "vLLM-shaped CoT text"
        # And the ``_``-prefixed sibling is stripped by sanitize_messages.
        assert "_provider_content" not in assistant_msg

    def test_reasoning_field_absent_when_not_attached(self) -> None:
        # Negative case: when the session-level gate decided NOT to
        # attach (any of the 3 gates failed), the SDK round-trip carries
        # no ``reasoning`` field — the operator's opt-out / non-vLLM
        # destination is honoured all the way to the wire.
        client, captured = self._capture_client()
        provider = OpenAIChatCompletionsProvider()

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "Final answer.",
                # No ``reasoning`` field — pre-attach shape, gate said no.
                "_provider_content": [{"type": "reasoning_text", "text": "would-have-replayed"}],
            },
            {"role": "user", "content": "follow-up"},
        ]

        provider.create_completion(
            client=client,
            model="gpt-4o",  # canonical OpenAI, not vLLM
            messages=messages,
            max_tokens=10,
            temperature=0.5,
            reasoning_effort="medium",
            extra_params=None,
            capabilities=provider.get_capabilities("gpt-4o"),
        )

        body = json.loads(captured[0]["body"])
        assistant_msg = next(m for m in body["messages"] if m["role"] == "assistant")
        assert "reasoning" not in assistant_msg
        assert "_provider_content" not in assistant_msg


# ---------------------------------------------------------------------------
# Call-site integration: confirm _try_stream and _utility_completion both
# invoke the helper.  Pins that the 2 hoist points stay in sync; a missed
# call site is exactly the kind of regression this catches.  The agent
# _run_agent path is deliberately NOT a Phase 5 hoist — see the NOTE
# comment inside _run_agent's nested _api_call closure (grep session.py
# for "Phase 5 vLLM ``reasoning`` field replay is intentionally NOT
# wired here"): agent assistant messages don't carry
# ``_provider_content`` so the helper would no-op every turn anyway.
# ---------------------------------------------------------------------------


class TestCallSitesInvokeMaybeAttach:
    """The helper does nothing unless one of the 2 call sites calls it.
    Verify the wiring at each — without this, a refactor that drops a
    call site would silently regress Phase 5 on that path."""

    def test_try_stream_call_site_attaches(self) -> None:
        session = _make_session()
        session._registry = _vllm_registry(replay=True)
        session._model_alias = "qwen3"

        captured: dict[str, Any] = {}

        def capture_streaming(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return iter([])

        provider = OpenAIChatCompletionsProvider()
        # Patch only the network-facing method so we don't actually call
        # an LLM, but keep the real provider instance (so the isinstance
        # gate sees the right type).
        provider.create_streaming = capture_streaming  # type: ignore[method-assign]

        with (
            patch.object(session, "_get_active_tools", return_value=None),
            patch.object(session, "_provider_extra_params", return_value=None),
            patch.object(session, "_get_deferred_names", return_value=frozenset()),
            patch.object(session, "_check_cancelled"),
        ):
            session._try_stream(
                client=MagicMock(),
                model="qwen3",
                msgs=[_assistant_msg_with_thinking("from try_stream")],
                provider=provider,
                model_alias="qwen3",
            )

        # The messages handed to the provider include the attached
        # reasoning field — proves _try_stream invoked
        # _maybe_attach_vllm_chat_reasoning before the call.
        msgs_sent = captured["messages"]
        assert msgs_sent[0]["reasoning"] == "from try_stream"

    def test_utility_completion_call_site_attaches(self) -> None:
        session = _make_session()
        session._registry = _vllm_registry(replay=True)
        session._model_alias = "qwen3"

        captured: dict[str, Any] = {}

        def capture_completion(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                content="", tool_calls=[], usage=None, raw_blocks=None, provider_blocks=None
            )

        provider = OpenAIChatCompletionsProvider()
        provider.create_completion = capture_completion  # type: ignore[method-assign]
        session._provider = provider

        with (
            patch.object(session, "_provider_extra_params", return_value=None),
            patch.object(
                session, "_get_capabilities", return_value=provider.get_capabilities("qwen3")
            ),
        ):
            session._utility_completion(
                messages=[_assistant_msg_with_thinking("from utility")],
            )

        msgs_sent = captured["messages"]
        assert msgs_sent[0]["reasoning"] == "from utility"
