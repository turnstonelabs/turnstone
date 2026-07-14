"""Unit tests for the perception wire-fallback (turnstone/core/perception.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests._session_helpers import as_stream, mock_completion_result
from turnstone.core import perception

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

    def __init__(self, *, content: str = "a description", fail_times: int = 0) -> None:
        self.calls = 0
        self._content = content
        self._fail_times = fail_times
        self.last_messages: list[dict[str, Any]] | None = None
        self.last_resolve: Any = None

    def get_capabilities(self, model: str) -> Any:
        from turnstone.core.providers._protocol import ModelCapabilities

        return ModelCapabilities()

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


def test_describe_lowers_prompt_then_by_reference_parts() -> None:
    prov = _StubProvider(content="desc")
    out = perception.describe(provider=prov, client=object(), model="m", parts=_parts())  # type: ignore[arg-type]
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
    assert perception.describe(provider=prov, client=object(), model="m", parts=[]) == ""  # type: ignore[arg-type]
    assert prov.calls == 0


def test_describe_cached_memoizes_by_alias_and_hash() -> None:
    prov = _StubProvider(content="desc")
    kw: dict[str, Any] = {
        "provider": prov,
        "client": object(),
        "model": "m",
        "alias": "omni",
        "content_hash": "h1",
        "parts": _parts(),
    }
    assert perception.describe_cached(**kw) == "desc"
    assert perception.describe_cached(**kw) == "desc"
    assert prov.calls == 1  # second served from cache
    perception.describe_cached(**{**kw, "content_hash": "h2"})
    assert prov.calls == 2  # distinct hash → fresh perceive


def test_describe_cached_does_not_cache_failures() -> None:
    prov = _StubProvider(content="recovered", fail_times=1)
    kw: dict[str, Any] = {
        "provider": prov,
        "client": object(),
        "model": "m",
        "alias": "omni",
        "content_hash": "h",
        "parts": _parts(),
    }
    assert perception.describe_cached(**kw) == ""  # backend down → "" (uncached)
    assert perception.describe_cached(**kw) == "recovered"  # retried, succeeds
    assert prov.calls == 2


def test_describe_peek_returns_none_when_absent() -> None:
    assert perception.describe_peek(alias="omni", content_hash="missing") is None


def test_describe_peek_returns_cached_without_recompute() -> None:
    prov = _StubProvider(content="desc")
    kw: dict[str, Any] = {
        "provider": prov,
        "client": object(),
        "model": "m",
        "alias": "omni",
        "content_hash": "h",
        "parts": _parts(),
    }
    perception.describe_cached(**kw)  # populate the memo
    assert prov.calls == 1
    # Peek serves the memoized text and never re-invokes the backend — this is
    # what lets the wire resolver skip the PDF rasterize on a cross-send hit.
    assert perception.describe_peek(alias="omni", content_hash="h") == "desc"
    assert prov.calls == 1
