"""Recording fake SDK client — captures the kwargs at each provider's seam.

Every provider's ``create_streaming`` assembles its kwargs and calls the
SDK *eagerly* before returning the stream iterator (Anthropic
``client.messages.stream``, OpenAI ``client.chat.completions.create``,
Responses ``client.responses.create/stream``), so driving a provider
against a :class:`RecordingClient` captures the full composed request
payload without a network round-trip.

Shared by the wire-payload golden harness (``test_wire_payload_golden``)
and the effort-ladder parity harness (``test_effort_ladder_wire_parity``)
so both assert against the same capture seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


class _EmptyStream:
    """Stand-in for an SDK stream / stream-manager: empty iterable AND no-op CM."""

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __enter__(self) -> _EmptyStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Seam:
    """Records the kwargs of a single SDK call, returns an empty stream stub."""

    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    def __call__(self, **kwargs: Any) -> _EmptyStream:
        # Last write wins; only one seam is exercised per provider call.
        self._sink["payload"] = kwargs
        return _EmptyStream()


class _Completions:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.create = _Seam(sink)


class _Chat:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.completions = _Completions(sink)


class _Messages:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.stream = _Seam(sink)


class _Responses:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.create = _Seam(sink)
        self.stream = _Seam(sink)


class RecordingClient:
    """Fake SDK client exposing every provider's call seam, recording kwargs."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.messages = _Messages(self.captured)
        self.chat = _Chat(self.captured)
        self.responses = _Responses(self.captured)
