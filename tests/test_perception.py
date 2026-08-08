"""Unit tests for the perception wire-fallback (turnstone/core/perception.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests._session_helpers import as_stream, mock_completion_result
from turnstone.core import perception
from turnstone.core.model_turn import ModelLane, ResolvedModelBinding, resolve_lane

if TYPE_CHECKING:
    from collections.abc import Iterator


class _StubProvider:
    """Minimal LLMProvider stand-in: counts calls, can fail the first N.

    ``describe`` routes through ``model_turn``, so the stub carries the lane
    surface (``provider_name``, ``get_capabilities``) and returns a full
    ``CompletionResult`` shape, and it records the ``resolve_attachments``
    callback the translator would use to materialize the by-reference parts.
    """

    provider_name = "openai-compatible"
    retryable_error_names: frozenset[str] = frozenset()

    def __init__(self, *, content: str = "a description", fail_times: int = 0) -> None:
        self.calls = 0
        self._content = content
        self._fail_times = fail_times
        self.last_messages: list[dict[str, Any]] | None = None
        self.last_resolve: Any = None

    def get_capabilities(self, model: str) -> Any:
        from turnstone.core.providers._protocol import ModelCapabilities

        return ModelCapabilities()

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def extract_reasoning_text(self, provider_blocks: list[dict[str, Any]] | None) -> str:
        return ""

    def create_streaming(
        self,
        *,
        client: Any,
        model: str,
        messages: list[dict[str, Any]],
        resolve_attachments: Any = None,
        **_: Any,
    ) -> Any:
        self.calls += 1
        self.last_messages = messages
        self.last_resolve = resolve_attachments
        if self.calls <= self._fail_times:
            raise RuntimeError("backend down")
        # Shared field inventory: when model_turn's re-ingest reads a new
        # CompletionResult field, mock_completion_result is the ONE
        # definition to extend and this suite moves with it.
        return as_stream(mock_completion_result(self._content))


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    perception._clear_perception_cache_for_test()
    yield
    perception._clear_perception_cache_for_test()


def _parts() -> list[dict[str, Any]]:
    return [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]


def _lane(provider: _StubProvider, *, alias: str = "omni") -> ModelLane:
    """Build the same resolved binding snapshot production hands perception."""
    return resolve_lane(provider, object(), "m", alias=alias)


def _binding(
    provider: _StubProvider,
    *,
    alias: str = "omni",
    generation: int = 0,
) -> ResolvedModelBinding:
    return ResolvedModelBinding(
        lane=_lane(provider, alias=alias),
        config=None,
        registry_generation=generation,
    )


def test_describe_lowers_prompt_then_by_reference_parts() -> None:
    prov = _StubProvider(content="desc")
    out = perception.describe(lane=_lane(prov), parts=_parts())
    assert out == "desc"
    assert prov.last_messages is not None
    content = prov.last_messages[0]["content"]
    assert content[0]["type"] == "text"  # prompt leads
    # The attachment rides by reference; the translator materializes it via
    # the threaded resolver, which must return the prebuilt parts verbatim.
    assert content[1]["attachment_id"] == "perception-input"
    assert prov.last_resolve is not None
    assert prov.last_resolve(["perception-input"]) == {"perception-input": _parts()}


def test_describe_empty_parts_skips_backend() -> None:
    prov = _StubProvider()
    assert perception.describe(lane=_lane(prov), parts=[]) == ""
    assert prov.calls == 0


def test_describe_passes_the_exact_supplied_lane_to_model_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.core.model_turn import ModelTurnResult
    from turnstone.core.trajectory import Turn

    binding = _binding(_StubProvider())
    lane = binding.lane
    seen: list[ModelLane] = []

    def _sample(sample_lane: ModelLane, *_args: Any, **_kwargs: Any) -> ModelTurnResult:
        seen.append(sample_lane)
        return ModelTurnResult(
            turn=Turn.assistant("from seam"),
            finish_reason="stop",
            usage=None,
            tool_calls=[],
        )

    monkeypatch.setattr(perception, "model_turn", _sample)

    assert perception.describe(lane=lane, parts=_parts()) == "from seam"
    assert seen == [lane]
    assert seen[0] is lane


def test_cancellation_ref_reaches_model_turn_and_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.core.deadline import DeadlineCancelledError

    ref = object()
    seen: list[Any] = []

    def abort(*_args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("cancel_ref"))
        raise DeadlineCancelledError("stopped")

    monkeypatch.setattr(perception, "model_turn", abort)
    binding = _binding(_StubProvider())
    lane = binding.lane

    with pytest.raises(DeadlineCancelledError, match="stopped"):
        perception.describe(lane=lane, parts=_parts(), cancel_ref=ref)
    with pytest.raises(DeadlineCancelledError, match="stopped"):
        perception.describe_cached(
            binding=binding,
            principal_id="user-a",
            content_hash="h-cancel",
            parts=_parts(),
            cancel_ref=ref,
        )

    assert seen == [ref, ref]
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=binding,
            content_hash="h-cancel",
        )
        is None
    )


def test_completed_cancelled_description_is_not_memoized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef

    binding = _binding(_StubProvider())
    cancelled_ref = StreamAbortRef()
    calls: list[str] = []

    def complete_after_cancel(**_kwargs: Any) -> str:
        calls.append("cancelled")
        cancelled_ref.abort()
        return "late description"

    monkeypatch.setattr(perception, "describe", complete_after_cancel)
    with pytest.raises(DeadlineCancelledError):
        perception.describe_cached(
            binding=binding,
            principal_id="user-a",
            content_hash="late",
            parts=_parts(),
            cancel_ref=cancelled_ref,
        )
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=binding,
            content_hash="late",
        )
        is None
    )

    monkeypatch.setattr(
        perception,
        "describe",
        lambda **_kwargs: calls.append("fresh") or "fresh description",
    )
    assert (
        perception.describe_cached(
            binding=binding,
            principal_id="user-a",
            content_hash="late",
            parts=_parts(),
            cancel_ref=StreamAbortRef(),
        )
        == "fresh description"
    )
    assert calls == ["cancelled", "fresh"]


def test_describe_cached_memoizes_by_principal_alias_generation_and_hash() -> None:
    prov = _StubProvider(content="desc")
    binding = _binding(prov)
    kw: dict[str, Any] = {
        "binding": binding,
        "principal_id": "user-a",
        "content_hash": "h1",
        "parts": _parts(),
    }
    assert perception.describe_cached(**kw) == "desc"
    assert perception.describe_cached(**kw) == "desc"
    assert prov.calls == 1  # second served from cache
    perception.describe_cached(**{**kw, "content_hash": "h2"})
    assert prov.calls == 2  # distinct hash → fresh perceive
    perception.describe_cached(**{**kw, "principal_id": "user-b"})
    assert prov.calls == 3  # same content under another user's grant → fresh perceive
    perception.describe_cached(**{**kw, "binding": _binding(prov, alias="other")})
    assert prov.calls == 4  # same content under another alias → fresh perceive
    newer = _binding(prov, generation=1)
    perception.describe_cached(**{**kw, "binding": newer})
    assert prov.calls == 5  # same alias under a new registry generation → fresh perceive
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=binding,
            content_hash="h1",
        )
        == "desc"
    )
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=_binding(prov, generation=2),
            content_hash="h1",
        )
        is None
    )


def test_describe_cached_does_not_cache_failures() -> None:
    prov = _StubProvider(content="recovered", fail_times=1)
    kw: dict[str, Any] = {
        "binding": _binding(prov),
        "principal_id": "user-a",
        "content_hash": "h",
        "parts": _parts(),
    }
    assert perception.describe_cached(**kw) == ""  # backend down → "" (uncached)
    assert perception.describe_cached(**kw) == "recovered"  # retried, succeeds
    assert prov.calls == 2


def test_describe_peek_returns_none_when_absent() -> None:
    binding = _binding(_StubProvider())
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=binding,
            content_hash="missing",
        )
        is None
    )


def test_describe_peek_returns_cached_without_recompute() -> None:
    prov = _StubProvider(content="desc")
    binding = _binding(prov)
    kw: dict[str, Any] = {
        "binding": binding,
        "principal_id": "user-a",
        "content_hash": "h",
        "parts": _parts(),
    }
    perception.describe_cached(**kw)  # populate the memo
    assert prov.calls == 1
    # Peek serves the memoized text and never re-invokes the backend — this is
    # what lets the wire resolver skip the PDF rasterize on a cross-send hit.
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=binding,
            content_hash="h",
        )
        == "desc"
    )
    assert (
        perception.describe_peek(
            principal_id="user-b",
            binding=binding,
            content_hash="h",
        )
        is None
    )
    assert prov.calls == 1


def test_describe_cached_memoizes_empty_descriptions() -> None:
    # A completed-but-empty description (an all-reasoning pass) memoizes
    # like any other result: one perceive per key, ever — bounded cost.
    # The pin-until-restart residual is deliberate; the remediation is
    # server-side (reasoning parser / template thinking toggle).
    prov = _StubProvider(content="")
    binding = _binding(prov)
    kw: dict[str, Any] = {
        "binding": binding,
        "principal_id": "user-a",
        "content_hash": "h-empty",
        "parts": _parts(),
    }
    assert perception.describe_cached(**kw) == ""
    assert perception.describe_cached(**kw) == ""
    assert prov.calls == 1  # second call served from the memo
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=binding,
            content_hash="h-empty",
        )
        == ""
    )


def test_racing_empty_result_never_clobbers_memoized_real_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The describe call runs unlocked: a racer can memoize a REAL
    # description while another call is producing "".  The empty commit
    # must yield to the existing memo, never overwrite it.
    binding = _binding(_StubProvider(content=""))
    call_key = {"principal_id": "user-a", "content_hash": "h-race"}
    cache_key = {**call_key, "binding": binding}

    def _racing_describe(**_kw: Any) -> str:
        with perception._cache_lock:
            perception._cache[perception._cache_key(**cache_key)] = "real from racer"
        return ""

    monkeypatch.setattr(perception, "describe", _racing_describe)
    out = perception.describe_cached(
        binding=binding,
        parts=_parts(),
        **call_key,
    )
    assert out == "real from racer"
    assert (
        perception.describe_peek(**cache_key) == "real from racer"
    )  # the billed real description survived
