"""Unit tests for the perception wire-fallback (turnstone/core/perception.py)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests._session_helpers import as_stream, mock_completion_result
from turnstone.core import perception
from turnstone.core.completion_recovery import ModelTurnLocalError
from turnstone.core.model_turn import ModelLane, ResolvedModelBinding, resolve_lane
from turnstone.core.providers._protocol import ProviderRequestMetrics

if TYPE_CHECKING:
    from collections.abc import Iterator


class _StubProvider:
    """Minimal LLMProvider stand-in: counts calls, can fail the first N.

    ``describe`` routes through ``model_turn``, so the stub carries the lane
    surface (``provider_name``, ``get_capabilities``) and returns a full
    ``CompletionResult`` shape, and it records that ``model_turn`` already
    materialized by-reference parts and cleared the provider callback.
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
        request_metrics_ref: list[ProviderRequestMetrics] | None = None,
        **_: Any,
    ) -> Any:
        if request_metrics_ref is not None:
            request_metrics_ref.append(ProviderRequestMetrics(native_tools_enabled=False))
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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(perception, "monotonic", lambda: now[0])
    return now


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
    # model_turn materializes before the model-capacity lease, then hands the
    # provider the prebuilt inline part with no resolver left under the gate.
    assert content[1] == _parts()[0]
    assert prov.last_resolve is None


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


@pytest.mark.parametrize("outcome", ["success", "backend_failure", "local_failure"])
def test_completed_cancelled_description_is_not_memoized(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef

    binding = _binding(_StubProvider())
    cancelled_ref = StreamAbortRef()
    calls: list[str] = []

    def complete_after_cancel(**_kwargs: Any) -> str:
        calls.append("cancelled")
        cancelled_ref.abort()
        if outcome == "backend_failure":
            raise perception.PerceptionBackendError("backend down")
        if outcome == "local_failure":
            raise ModelTurnLocalError(RuntimeError("local processing failed"))
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


@pytest.mark.parametrize("fails", [False, True])
def test_describe_cached_memoizes_by_principal_alias_generation_and_hash(fails: bool) -> None:
    prov = _StubProvider(content="desc", fail_times=100 if fails else 0)
    expected = "" if fails else "desc"
    binding = _binding(prov)
    kw: dict[str, Any] = {
        "binding": binding,
        "principal_id": "user-a",
        "content_hash": "h1",
        "parts": _parts(),
    }
    assert perception.describe_cached(**kw) == expected
    assert perception.describe_cached(**kw) == expected
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
        == expected
    )
    assert (
        perception.describe_peek(
            principal_id="user-a",
            binding=_binding(prov, generation=2),
            content_hash="h1",
        )
        is None
    )


def test_describe_cached_memoizes_success_suffix() -> None:
    prov = _StubProvider(content="desc")
    binding = _binding(prov)

    first = perception.describe_cached(
        binding=binding,
        principal_id="user-a",
        content_hash="h-suffix",
        parts=_parts(),
        result_suffix="[partial source]",
    )
    cached = perception.describe_peek(
        principal_id="user-a",
        binding=binding,
        content_hash="h-suffix",
    )

    assert first == cached == "desc\n\n[partial source]"
    assert prov.calls == 1


def test_describe_cached_retries_failure_after_cooldown(clock: list[float]) -> None:
    prov = _StubProvider(content="recovered", fail_times=1)
    kw: dict[str, Any] = {
        "binding": _binding(prov),
        "principal_id": "user-a",
        "content_hash": "h",
        "parts": _parts(),
    }
    assert perception.describe_cached(**kw) == ""
    clock[0] += 59.0
    assert perception.describe_cached(**kw) == ""
    assert prov.calls == 1
    clock[0] += 1.0
    assert perception.describe_cached(**kw) == "recovered"
    assert perception.describe_cached(**kw) == "recovered"
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


def test_empty_description_memoizes_for_the_binding_generation(
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
) -> None:
    # A model that produces no description after the shared recovery allowance
    # is a deterministic outcome for this binding and content: memoize it like
    # any other result, so later sends neither rebuild parts nor re-perceive.
    # A new registry generation is a different binding and perceives afresh.
    monkeypatch.setattr("turnstone.core.model_turn._DRAIN_RETRY_BASE_DELAY", 0.0)
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
    assert prov.calls == 3  # one shared recovery allowance, then the memo
    peek = {"principal_id": "user-a", "binding": binding, "content_hash": "h-empty"}
    assert perception.describe_peek(**peek) == ""

    clock[0] += 3600.0
    assert perception.describe_peek(**peek) == ""
    assert perception.describe_cached(**kw) == ""
    assert prov.calls == 3

    prov._content = "recovered description"
    rebound = _binding(prov, generation=1)
    assert perception.describe_cached(**{**kw, "binding": rebound}) == "recovered description"
    assert prov.calls == 4


@pytest.mark.parametrize("outcome", ["empty", "backend_failure", "local_failure"])
def test_racing_empty_result_never_clobbers_memoized_real_description(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
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
        if outcome == "backend_failure":
            raise perception.PerceptionBackendError("no usable description")
        if outcome == "local_failure":
            raise ModelTurnLocalError(RuntimeError("local processing failed"))
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


def test_racing_success_replaces_failure_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _binding(_StubProvider())
    key = {"principal_id": "user-a", "content_hash": "h-race", "binding": binding}
    calls = 0

    def racing_describe(**_kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Another call finishes while the first successful call is in flight.
            assert perception.describe_cached(**key, parts=_parts()) == ""
            assert perception.describe_peek(**key) == ""
            return "useful description"
        raise perception.PerceptionBackendError("backend down")

    monkeypatch.setattr(perception, "describe", racing_describe)
    assert perception.describe_cached(**key, parts=_parts()) == "useful description"
    assert perception.describe_peek(**key) == "useful description"
    assert calls == 2


def test_failures_and_successes_share_one_bounded_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(perception, "_CACHE_MAX", 2)
    describe = Mock(
        side_effect=[
            perception.PerceptionBackendError("first failure"),
            "useful description",
            perception.PerceptionBackendError("second failure"),
        ]
    )
    monkeypatch.setattr(perception, "describe", describe)
    binding = _binding(_StubProvider())
    key = {"principal_id": "user-a", "binding": binding}
    for content_hash in ("h-first", "h-success", "h-last"):
        perception.describe_cached(**key, content_hash=content_hash, parts=_parts())
    assert len(perception._cache) == 2
    assert perception.describe_peek(**key, content_hash="h-first") is None
    assert perception.describe_peek(**key, content_hash="h-success") == "useful description"
    assert perception.describe_peek(**key, content_hash="h-last") == ""
    assert describe.call_count == 3


@pytest.mark.parametrize("fails", [False, True])
def test_cancelled_cache_hit_still_propagates(fails: bool) -> None:
    from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef

    prov = _StubProvider(fail_times=1 if fails else 0)
    key = {"principal_id": "user-a", "binding": _binding(prov), "content_hash": "h"}
    perception.describe_cached(**key, parts=_parts())
    ref = StreamAbortRef()
    ref.abort()
    with pytest.raises(DeadlineCancelledError):
        perception.describe_cached(**key, parts=_parts(), cancel_ref=ref)
    assert prov.calls == 1


def test_local_perception_failure_preserves_parent_send_and_skips_parts_during_cooldown(
    tmp_db,
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
) -> None:
    import importlib

    from tests.test_empty_completion import _session
    from turnstone.core.attachments import Attachment
    from turnstone.core.workstream import WorkstreamKind

    model_turn_module = importlib.import_module("turnstone.core.model_turn")
    original_ingest = model_turn_module._ingest_completion
    child_calls = 0
    private_text = "private conversation content: context window exceeded"

    def fail_first_child(result, lane, **kwargs):
        nonlocal child_calls
        if lane.alias == "perception-test":
            child_calls += 1
            if child_calls == 1:
                raise RuntimeError(private_text)
        return original_ingest(result, lane, **kwargs)

    warning = Mock()
    monkeypatch.setattr(perception.log, "warning", warning)
    monkeypatch.setattr(model_turn_module, "_ingest_completion", fail_first_child)
    with _session(WorkstreamKind.INTERACTIVE, ["answer"] * 6) as (session, ui, requests):
        session.context_window = 100_000
        primary = session._primary_lane()
        binding = ResolvedModelBinding(
            lane=replace(
                primary,
                alias="perception-test",
                capabilities=replace(primary.capabilities, supports_audio_input=True),
            ),
            config=None,
            registry_generation=41,
        )
        monkeypatch.setattr(session, "_resolve_perception", lambda _principal: binding)
        monkeypatch.setattr(session, "_stt_transcript_part", lambda *args, **kwargs: None)
        parts = Mock(wraps=session._perception_parts)
        monkeypatch.setattr(session, "_perception_parts", parts)
        compact = Mock(side_effect=AssertionError("optional child fault must not compact parent"))
        monkeypatch.setattr(session, "_compact_messages", compact)

        session.send(
            "Describe this audio.",
            attachments=[
                Attachment(
                    attachment_id="audio-local-failure",
                    filename="sample.wav",
                    mime_type="audio/wav",
                    kind="audio",
                    content=b"audio bytes",
                )
            ],
        )
        assert len(requests) == 2
        assert parts.call_count == child_calls == 1
        assert ui.of("state")[-1] == "idle"
        assert "no transcription backend configured" in str(requests[1]["messages"])

        clock[0] += 59.0
        session.send("Continue.")
        assert len(requests) == 3
        assert parts.call_count == child_calls == 1

        clock[0] += 1.0
        session.send("Try the audio again.")
        assert len(requests) == 5
        assert parts.call_count == child_calls == 2
        assert "Perception of audio attachment" in str(requests[4]["messages"])

        session.send("Continue.")
        assert len(requests) == 6
        assert parts.call_count == child_calls == 2
        assert ui.of("state")[-1] == "idle"
        compact.assert_not_called()

    warning.assert_called_once()
    assert "perception local processing failed" in warning.call_args.args[0]
    assert "RuntimeError" in warning.call_args.args
    assert private_text not in str(warning.call_args)
