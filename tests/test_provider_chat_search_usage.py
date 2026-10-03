"""Hosted search usage reaches context consumers through the real streaming SDK."""

from __future__ import annotations

import json
from dataclasses import asdict

import httpx2
import openai
import pytest

from tests._session_helpers import make_session
from turnstone.core.compaction import PromptTokenEstimator, resolve_context_usage
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._protocol import ModelCapabilities, drain_stream
from turnstone.core.trajectory import Turn

SEARCH_TOOL = {"type": "function", "function": {"name": "web_search", "parameters": {}}}
MESSAGES = [{"role": "user", "content": "Search for three facts and cite their sources."}]


@pytest.fixture(params=[False, True], ids=["usage_only", "usage_with_choice"])
def sdk_boundary(request):
    requests = []
    usage_with_choice = request.param

    def handle(request):
        assert request.url.path == "/v1/chat/completions"
        requests.append(json.loads(request.content))
        base = {
            "id": "chat_test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "search-model",
        }
        usage = {
            "prompt_tokens": 29_193,
            "completion_tokens": 117,
            "total_tokens": 29_310,
            "prompt_tokens_details": {"cached_tokens": 1_234},
        }
        chunks = [
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {"content": "Found it."}, "finish_reason": "stop"}
                ],
            }
        ]
        if usage_with_choice:
            chunks[0]["usage"] = usage
        else:
            chunks.append({**base, "choices": [], "usage": usage})
        newline = chr(10)
        frames = ["data: " + json.dumps(chunk) for chunk in chunks] + ["data: [DONE]"]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(newline * 2).join(frames) + newline * 2,
        )

    with openai.OpenAI(
        api_key="test-only",
        base_url="https://api.example.com/v1",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
    ) as client:
        yield client, requests


@pytest.mark.parametrize(
    "supports_search,tools,extra,options_enabled,search_enabled",
    [
        (True, [SEARCH_TOOL], None, True, True),
        (False, [SEARCH_TOOL], None, False, False),
        (True, None, None, False, True),
        (True, [{"type": "function", "function": {"name": "lookup"}}], None, False, True),
        (False, None, {"web_search_options": {}}, True, True),
        (True, [SEARCH_TOOL], {"web_search_options": None}, False, True),
        (False, None, {"web_search_options": None}, False, False),
        (True, [SEARCH_TOOL], {"web_search_options": {"search_context_size": "low"}}, True, True),
        (False, None, {"tools": [{"type": "code_interpreter"}]}, False, False),
    ],
)
def test_context_usage_follows_search_capability_and_final_options(
    sdk_boundary, supports_search, tools, extra, options_enabled, search_enabled
):
    client, requests = sdk_boundary
    result = drain_stream(
        OpenAIChatCompletionsProvider().create_streaming(
            client=client,
            model="search-model",
            messages=MESSAGES,
            tools=tools,
            capabilities=ModelCapabilities(supports_web_search=supports_search),
            extra_params=extra,
        )
    )

    assert (requests[0].get("web_search_options") is not None) is options_enabled
    assert result.content == "Found it."
    usage = result.usage
    assert usage is not None
    assert usage.prompt_tokens == 29_193
    assert usage.completion_tokens == 117
    assert usage.total_tokens == 29_310
    assert usage.cache_read_tokens == 1_234
    assert usage.prompt_tokens_cumulative is search_enabled
    context = resolve_context_usage(usage, local_request_estimate=lambda: 100)
    assert (context.anchor, context.served) == ((100, None) if search_enabled else (29_193, 29_193))


@pytest.mark.parametrize("model", ["local-model", "gpt-5-search-api", "gpt-6-astra"])
@pytest.mark.parametrize("tools", [None, [SEARCH_TOOL]], ids=["no_tools", "client_search"])
def test_compatible_defaults_keep_reported_context(sdk_boundary, model, tools):
    client, requests = sdk_boundary
    metrics = []
    result = drain_stream(
        OpenAIChatCompletionsProvider().create_streaming(
            client=client,
            model=model,
            messages=MESSAGES,
            tools=tools,
            request_metrics_ref=metrics,
        )
    )

    # Compatible model IDs are operator-owned; even a commercial-looking ID
    # must keep client search and ordinary usage unless explicitly configured.
    assert requests[0]["model"] == model
    assert requests[0].get("tools") == tools
    assert "web_search_options" not in requests[0]
    assert metrics[0].native_tools_enabled is False
    assert result.usage is not None
    assert result.usage.prompt_tokens_cumulative is False
    context = resolve_context_usage(result.usage, local_request_estimate=lambda: 100)
    assert (context.anchor, context.served) == (29_193, 29_193)


@pytest.mark.parametrize("tools", [None, [SEARCH_TOOL]], ids=["implicit", "explicit"])
def test_hosted_search_keeps_session_and_agent_context_local(sdk_boundary, tmp_db, tools):
    client, requests = sdk_boundary
    session = make_session()
    session.messages.append(Turn.user(MESSAGES[0]["content"]))
    messages = session._prepare_wire_messages(session._full_messages())
    request_metrics = []
    result = drain_stream(
        OpenAIChatCompletionsProvider().create_streaming(
            client=client,
            model="search-model",
            messages=messages,
            tools=tools,
            capabilities=ModelCapabilities(supports_web_search=True),
            request_metrics_ref=request_metrics,
        )
    )
    assert result.usage is not None
    assert ("web_search_options" in requests[0]) is (tools is not None)
    assert request_metrics[0].native_tools_enabled is True

    session._token_budget = 10_000
    before = session._estimated_prompt_tokens(tool_def_chars=0)
    ratio = session._chars_per_token
    session._last_usage = asdict(result.usage)
    session._update_token_table(msgs=messages, tool_def_chars=0, native_tokens=0)

    assert session._last_usage["prompt_tokens"] == before
    assert session._last_usage["billed_prompt_tokens"] == 29_193
    assert session._last_usage["cache_read_tokens"] == 1_234
    assert session._chars_per_token == ratio
    assert session._budget_exhausted

    estimator = PromptTokenEstimator(
        measure=lambda message: (len(message["content"]), 0, 0),
        tool_def_chars=0,
    )
    before = estimator.estimate(messages)
    ratio = estimator.chars_per_token
    assert estimator.observe(usage=result.usage, messages=messages) == before
    assert estimator.chars_per_token == ratio
