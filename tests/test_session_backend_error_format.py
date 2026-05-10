"""Tests for :meth:`ChatSession._format_backend_error`.

The helper turns bare backend-boundary exceptions (httpx ``ReadTimeout``,
OpenAI SDK ``APITimeoutError`` / ``APIConnectionError`` /
``NotFoundError`` / ``RateLimitError`` / ``AuthenticationError``) into
operator-actionable messages that include the provider, base URL, and
model.  We bind the method to lightweight stubs rather than constructing
a full :class:`ChatSession`: the helper only reads ``self.client``,
``self._provider``, ``self.model``, and ``self._model_alias``, so a
SimpleNamespace stub exercises the same surface without dragging in the
storage / prompt composition fixtures.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from turnstone.core.session import ChatSession


def _stub(
    *,
    base_url: str = "http://192.168.0.5:8000/v1",
    provider_name: str = "openai-compatible",
    model: str = "flatspark",
    model_alias: str | None = "flatspark",
    client_attr: str = "base_url",
) -> Any:
    """Build a minimal session-like stub for ``_format_backend_error``.

    ``client_attr`` selects which attribute on the client carries the
    URL — both ``base_url`` (OpenAI / Anthropic SDK public surface) and
    ``_base_url`` (httpx fallback) are exercised by the helper.
    """
    client_kwargs: dict[str, Any] = {client_attr: base_url}
    return SimpleNamespace(
        client=SimpleNamespace(**client_kwargs),
        _provider=SimpleNamespace(provider_name=provider_name),
        model=model,
        _model_alias=model_alias,
    )


def _format(stub: Any, exc: BaseException) -> str | None:
    """Invoke the method as if on a real session — ``__func__`` skips
    the descriptor protocol so we can pass any object as ``self``."""
    return ChatSession._format_backend_error(stub, exc)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Synthetic exception classes — class name is what the helper matches on,
# so we don't need real httpx / openai imports here.
# ---------------------------------------------------------------------------


# N818 (Error suffix on Exception names) is intentionally suppressed
# for the four classes below — they exist to impersonate httpx /
# Anthropic SDK exception class names verbatim, since the formatter
# matches by class name.  Renaming them defeats the test.


class ReadTimeout(Exception):  # noqa: N818
    pass


class WriteTimeout(Exception):  # noqa: N818
    pass


class APITimeoutError(Exception):
    pass


class ConnectError(Exception):  # noqa: N818
    pass


class ConnectTimeout(Exception):  # noqa: N818
    pass


class APIConnectionError(Exception):
    pass


class NotFoundError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class RateLimitError(Exception):
    pass


class SomeUnrelatedError(Exception):
    """Outside the recognised set — should fall through to ``None``."""


# ---------------------------------------------------------------------------
# Known categories — each branch produces an operator-actionable message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc_cls", [ReadTimeout, WriteTimeout, APITimeoutError])
def test_timeout_message_names_backend_and_model(exc_cls):
    msg = _format(_stub(), exc_cls())
    assert msg is not None
    assert "Backend timeout" in msg
    assert exc_cls.__name__ in msg
    assert "openai-compatible" in msg
    assert "http://192.168.0.5:8000/v1" in msg
    assert "model=flatspark" in msg
    assert "wedged" in msg


@pytest.mark.parametrize("exc_cls", [ConnectError, ConnectTimeout, APIConnectionError])
def test_connect_message_says_unreachable(exc_cls):
    msg = _format(_stub(), exc_cls("dial tcp: i/o timeout"))
    assert msg is not None
    assert "Backend unreachable" in msg
    assert exc_cls.__name__ in msg
    assert "http://192.168.0.5:8000/v1" in msg
    # Raw exception text is preserved as a tail for grep-correlation.
    assert "dial tcp: i/o timeout" in msg


def test_not_found_points_at_model_name_mismatch():
    msg = _format(_stub(model="flatspark"), NotFoundError("model flatspark not found"))
    assert msg is not None
    assert "Backend reports model not loaded" in msg
    assert "no model named 'flatspark'" in msg
    assert "/v1/models" in msg  # operator hint


@pytest.mark.parametrize("exc_cls", [AuthenticationError, PermissionDeniedError])
def test_auth_message_mentions_api_key(exc_cls):
    msg = _format(_stub(), exc_cls("invalid api key"))
    assert msg is not None
    assert "Backend rejected credentials" in msg
    assert "API key" in msg


def test_rate_limit_message():
    msg = _format(_stub(), RateLimitError("limit exceeded"))
    assert msg is not None
    assert "Backend rate-limited" in msg
    assert "limit exceeded" in msg


# ---------------------------------------------------------------------------
# Fall-through + degradation behaviour
# ---------------------------------------------------------------------------


def test_unknown_exception_returns_none():
    assert _format(_stub(), SomeUnrelatedError("anything")) is None


def test_unknown_exception_value_error_returns_none():
    assert _format(_stub(), ValueError("not a backend error")) is None


def test_trailing_slash_and_query_string_stripped():
    msg = _format(
        _stub(base_url="http://node-a:8000/v1/?api_key=secret&foo=1"),
        ReadTimeout(),
    )
    assert msg is not None
    assert "http://node-a:8000/v1" in msg
    # Query string (which may carry credentials) is stripped before the
    # message is built — sanitize_error_text is a second line of defence
    # but the helper itself must not embed query params verbatim.
    assert "api_key" not in msg
    assert "secret" not in msg


def test_missing_provider_degrades_to_placeholder():
    stub = _stub()
    stub._provider = None
    msg = _format(stub, ReadTimeout())
    assert msg is not None
    # No exception, no NoneType formatting leaking through.
    assert "Backend timeout" in msg
    assert "from ?" in msg or "openai-compatible" not in msg


def test_client_base_url_raises_degrades_gracefully():
    class _BadClient:
        @property
        def base_url(self) -> str:
            raise RuntimeError("boom")

    stub = SimpleNamespace(
        client=_BadClient(),
        _provider=SimpleNamespace(provider_name="openai-compatible"),
        model="flatspark",
        _model_alias="flatspark",
    )
    msg = _format(stub, ReadTimeout())
    assert msg is not None
    assert "Backend timeout" in msg
    # base_url accessor blew up — message still renders with placeholder.
    assert "at ?" in msg


def test_httpx_underscore_base_url_fallback():
    # httpx client carries ``_base_url`` on some versions instead of
    # ``base_url`` — the helper checks both.
    stub = _stub(base_url="http://alt-host:9000", client_attr="_base_url")
    # SimpleNamespace exposes the attr; remove the public one so the
    # fallback path is exercised.
    delattr(stub.client, "base_url") if hasattr(stub.client, "base_url") else None
    msg = _format(stub, ReadTimeout())
    assert msg is not None
    assert "http://alt-host:9000" in msg


# ---------------------------------------------------------------------------
# Integration with _record_fatal_error — original bare-class string is
# replaced by the enriched message when the exception type is recognised.
# ---------------------------------------------------------------------------


def _record_fatal_stub(ui: Any, captured: dict[str, str]) -> Any:
    """Build a stub for the ``_record_fatal_error`` integration tests.

    ``_record_fatal_error`` calls ``self._format_backend_error(...)``
    internally, so the stub binds the unbound method to itself rather
    than relying on Python's descriptor protocol (which only kicks in
    when ``self`` is a real instance of the class)."""
    stub = SimpleNamespace(
        client=SimpleNamespace(base_url="http://192.168.0.5:8000/v1"),
        _provider=SimpleNamespace(provider_name="openai-compatible"),
        model="flatspark",
        _model_alias="flatspark",
        _ws_id="ws-test",
        _has_persisted_error=False,
        ui=ui,
        _emit_state=lambda state: captured.setdefault("state", state),
    )
    stub._format_backend_error = lambda exc: ChatSession._format_backend_error(stub, exc)
    return stub


def test_record_fatal_uses_enriched_message_for_known(monkeypatch):
    """End-to-end: a recognised exception flows through
    ``_record_fatal_error`` and the enriched text reaches both the UI
    and the persist hook."""

    captured: dict[str, str] = {}

    def fake_persist(ws_id: str, msg: str) -> None:
        captured["persist"] = msg

    def fake_sanitize(text: str, *, max_len: int = 1024) -> str:
        # Skip the credential-redaction module (and its module-level
        # regex compile) by returning the input verbatim — the helper
        # under test produces no credentials.
        return text

    import turnstone.core.memory as memory_mod

    monkeypatch.setattr(memory_mod, "persist_last_error", fake_persist)
    monkeypatch.setattr(memory_mod, "sanitize_error_text", fake_sanitize)

    class _UI:
        def __init__(self) -> None:
            self.errors: list[str] = []

        def on_error(self, msg: str) -> None:
            self.errors.append(msg)

    ui = _UI()
    stub = _record_fatal_stub(ui, captured)

    ChatSession._record_fatal_error(stub, ReadTimeout())  # type: ignore[arg-type]

    assert ui.errors, "UI never received error"
    assert "Backend timeout" in ui.errors[0]
    assert "ReadTimeout" in ui.errors[0]
    assert captured["persist"] == ui.errors[0]
    assert captured["state"] == "error"
    assert stub._has_persisted_error is True


def test_record_fatal_falls_back_for_unknown(monkeypatch):
    """An unrecognised exception keeps the legacy
    ``f"{type(exc).__name__}: {exc}"`` shape so we don't regress
    existing call sites that grep on it."""

    captured: dict[str, str] = {}

    def fake_persist(ws_id: str, msg: str) -> None:
        captured["persist"] = msg

    def fake_sanitize(text: str, *, max_len: int = 1024) -> str:
        return text

    import turnstone.core.memory as memory_mod

    monkeypatch.setattr(memory_mod, "persist_last_error", fake_persist)
    monkeypatch.setattr(memory_mod, "sanitize_error_text", fake_sanitize)

    class _UI:
        def __init__(self) -> None:
            self.errors: list[str] = []

        def on_error(self, msg: str) -> None:
            self.errors.append(msg)

    ui = _UI()
    stub = _record_fatal_stub(ui, captured)

    ChatSession._record_fatal_error(stub, ValueError("plain old error"))  # type: ignore[arg-type]

    assert ui.errors == ["ValueError: plain old error"]
    assert captured["persist"] == "ValueError: plain old error"
