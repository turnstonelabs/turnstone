"""Unit tests for the perception wire-fallback (turnstone/core/perception.py)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from turnstone.core import perception

if TYPE_CHECKING:
    from collections.abc import Iterator


class _StubProvider:
    """Minimal LLMProvider stand-in: counts calls, can fail the first N."""

    def __init__(self, *, content: str = "a description", fail_times: int = 0) -> None:
        self.calls = 0
        self._content = content
        self._fail_times = fail_times
        self.last_messages: list[dict[str, Any]] | None = None

    def create_completion(
        self, *, client: Any, model: str, messages: list[dict[str, Any]], **_: Any
    ) -> SimpleNamespace:
        self.calls += 1
        self.last_messages = messages
        if self.calls <= self._fail_times:
            raise RuntimeError("backend down")
        return SimpleNamespace(content=self._content)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    perception._clear_perception_cache_for_test()
    yield
    perception._clear_perception_cache_for_test()


def _parts() -> list[dict[str, Any]]:
    return [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]


def test_describe_builds_prompt_then_parts() -> None:
    prov = _StubProvider(content="desc")
    out = perception.describe(provider=prov, client=object(), model="m", parts=_parts())  # type: ignore[arg-type]
    assert out == "desc"
    assert prov.last_messages is not None
    content = prov.last_messages[0]["content"]
    assert content[0]["type"] == "text"  # prompt leads
    assert content[1]["type"] == "image_url"  # attachment parts follow


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
