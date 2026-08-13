"""Tests for :meth:`ChatSession._format_backend_error`.

The helper turns bare backend-boundary exceptions (HTTPX/HTTPX2 ``ReadTimeout``,
OpenAI SDK ``APITimeoutError`` / ``APIConnectionError`` /
``NotFoundError`` / ``RateLimitError`` / ``AuthenticationError``) into
operator-actionable messages that include the provider, base URL, and
model. We bind the method to lightweight stubs carrying one coherent
``ModelLane`` rather than constructing a full :class:`ChatSession`.
"""

from __future__ import annotations

import dataclasses
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests._session_helpers import RecordingUI, make_session, provider_shell
from turnstone.core.model_turn import ModelLane
from turnstone.core.providers import ModelCapabilities
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
    lane = ModelLane(
        client=SimpleNamespace(**client_kwargs),
        provider=SimpleNamespace(provider_name=provider_name),
        model=model,
        alias=model_alias or "",
    )
    stub = SimpleNamespace(
        model=model,
        _model_alias=model_alias,
        # Dead-binding latches, clear: the formatter checks them first and
        # short-circuits when both are unset, like a healthy session.
        _registry_alias_removed=None,
        _rebind_failed_key=None,
    )
    stub._lane = lane
    stub._primary_lane = lambda: stub._lane
    return stub


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


class ReadError(Exception):  # noqa: N818
    pass


class RemoteProtocolError(Exception):  # noqa: N818
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


# ---------------------------------------------------------------------------
# Model label — lead with the alias the model references, annotate the id
# ---------------------------------------------------------------------------


def test_error_leads_with_alias_and_annotates_backend_id():
    """When the display alias differs from the backend model id, the enriched
    error leads with the ALIAS (the identifier the model references everywhere —
    list_nodes, spawn) and annotates the backend id for the operator, so a
    coordinator correlates the failure with those surfaces without a lookup."""
    msg = _format(
        _stub(model="deepseek-v4-flash", model_alias="DeepSeek-V4-Flash"),
        APIConnectionError("cannot reach"),
    )
    assert msg is not None
    assert "model=DeepSeek-V4-Flash (id=deepseek-v4-flash)" in msg


def test_error_model_label_collapses_when_alias_equals_id():
    """No redundant (id=...) annotation when the alias and backend id coincide."""
    msg = _format(_stub(model="flatspark", model_alias="flatspark"), APIConnectionError("x"))
    assert msg is not None
    assert "model=flatspark" in msg
    assert "(id=" not in msg


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


def test_rate_limit_with_overflow_phrasing_is_not_mislabeled_overflow():
    """A recognized RateLimitError whose quota text happens to contain a
    context-overflow phrase must still render as rate-limited — the text-based
    overflow branch is gated on 'not a known class', so it can't hijack a
    recognized error and mark a transient 429 as a hard 'Context window exceeded'."""
    msg = _format(
        _stub(), RateLimitError("exceeds the maximum number of tokens allowed per minute")
    )
    assert msg is not None
    assert "Backend rate-limited" in msg
    assert "Context window exceeded" not in msg


# ---------------------------------------------------------------------------
# Stream-death branch — the mid-response wire-failure wording (#937)
# ---------------------------------------------------------------------------


def _stream_death_exemplars() -> list[BaseException]:
    """One realistic instance per name in ``_BACKEND_STREAM_EXC_NAMES``:
    the normalized shape the guarded iterators raise, plus the raw HTTPX-family
    names for any future unguarded path."""
    from turnstone.core.providers import IncompleteStreamError

    return [
        IncompleteStreamError(
            "stream transport failed mid-response "
            "(ReadError: [SSL] record layer failure (_ssl.c:2590))"
        ),
        ReadError("[SSL] record layer failure (_ssl.c:2590)"),
        RemoteProtocolError("peer closed connection without sending complete message body"),
    ]


@pytest.mark.parametrize("exc", _stream_death_exemplars(), ids=lambda e: type(e).__name__)
def test_stream_death_names_backend_and_model(exc):
    msg = _format(_stub(), exc)
    assert msg is not None
    assert "Backend stream died mid-response" in msg
    assert type(exc).__name__ in msg
    assert "openai-compatible" in msg
    assert "http://192.168.0.5:8000/v1" in msg
    assert "model=flatspark" in msg
    assert "retries did not recover it" in msg
    # Raw exception text is preserved as a tail for grep-correlation.
    assert str(exc) in msg


def test_stream_death_first_sentence_survives_discord_cut():
    """Discord truncates ``on_error`` text to 500 chars — the identity-bearing
    first sentence must fit even with realistic-length alias/URL inputs."""
    msg = _format(
        _stub(
            base_url="https://inference-gateway.internal.example-corp.net:8443/serving/v1",
            provider_name="openai-compatible",
            model="deepseek-r2-awq-128k-instruct-20260115",
            model_alias="prod-reasoning-primary",
        ),
        ReadError("[SSL] record layer failure (_ssl.c:2590)"),
    )
    assert msg is not None
    first_sentence = msg[: msg.index(". ") + 1]
    assert "Backend stream died mid-response" in first_sentence
    assert len(first_sentence) < 500


def test_registry_diagnosed_binding_outranks_stream_death():
    """An alias the per-send refresh diagnosed dead outranks the raw stream
    symptom: the rebind wording points at the admin action, the transport
    wording at network health — the former is the actionable one."""
    from turnstone.core.providers import IncompleteStreamError

    stub = _stub()
    stub._registry_alias_removed = "flatspark"
    stub._registry = None
    stub._kind = None
    msg = _format(stub, IncompleteStreamError("stream transport failed mid-response"))
    assert msg is not None
    assert "has been removed from the registry" in msg
    assert "Backend stream died" not in msg


def test_stream_death_with_overflow_phrasing_stays_stream_death():
    """Joining the stream names into ``_BACKEND_KNOWN_EXC_NAMES`` removes them
    from ``_is_ctx_overflow``'s text-detection eligibility (its class
    self-gate) — deliberate: transport/SSL texts never carry real overflow
    phrases, and a stream death must never be misfiled as a deterministic
    overflow (which callers route to a non-retryable compaction path)."""
    msg = _format(_stub(), ReadError("proxy said: maximum context length hint in banner"))
    assert msg is not None
    assert "Backend stream died mid-response" in msg
    assert "Context window exceeded" not in msg


def test_terminal_fallback_stream_death_reports_actual_lane(tmp_db):
    """Fatal formatting consumes the fallback lane that armed the stream.

    The original exception object survives the retry wrapper, while its
    side-table context contains no client, credential, principal, or query.
    """
    from turnstone.core.providers import IncompleteStreamError

    ui = RecordingUI()  # type: ignore[no-untyped-call]
    session = make_session(model_alias="primary", ui=ui)
    provider = provider_shell("fallback-provider")
    fallback_lane = ModelLane(
        provider=provider,
        client=SimpleNamespace(base_url="https://fallback.example/v1?api_key=terminal-secret"),
        model="fallback-kernel",
        alias="fallback-alias",
        registry_generation=19,
        capabilities=ModelCapabilities(),
    )
    death = IncompleteStreamError("peer closed the response")

    def _fail_on_fallback(consumer, *_args, **_kwargs):
        consumer.begin_attempt(SimpleNamespace(armed=True), None, fallback_lane)
        raise death

    session._MID_STREAM_RETRIES = 0
    with (
        patch.object(session, "_model_turn_with_fallback", side_effect=_fail_on_fallback),
        pytest.raises(IncompleteStreamError) as raised,
    ):
        session._stream_response()

    assert raised.value is death
    session._record_fatal_error(raised.value)
    message = ui.of("error")[-1]
    assert "fallback-provider" in message
    assert "https://fallback.example/v1" in message
    assert "model=fallback-alias (id=fallback-kernel)" in message
    assert "primary" not in message
    assert "terminal-secret" not in message


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
    stub._lane = dataclasses.replace(stub._lane, provider=None)
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

    stub = _stub()
    stub._lane = dataclasses.replace(stub._lane, client=_BadClient())
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
    client = stub._lane.client
    delattr(client, "base_url") if hasattr(client, "base_url") else None
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
    stub = _stub()
    stub._ws_id = "ws-test"
    stub._generation_lock = threading.RLock()
    stub._has_persisted_error = False
    stub.ui = ui
    stub._emit_state = lambda state, **_kwargs: captured.setdefault("state", state)
    stub._format_backend_error = lambda exc: ChatSession._format_backend_error(stub, exc)
    stub._save_last_error = lambda ws_id, text: ChatSession._save_last_error(stub, ws_id, text)
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

    monkeypatch.setattr("turnstone.core.session.persist_last_error", fake_persist)
    monkeypatch.setattr("turnstone.core.session.sanitize_error_text", fake_sanitize)

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

    monkeypatch.setattr("turnstone.core.session.persist_last_error", fake_persist)
    monkeypatch.setattr("turnstone.core.session.sanitize_error_text", fake_sanitize)

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


@pytest.mark.parametrize(
    ("exc", "expect_error_level", "message_substring"),
    [
        pytest.param(ReadTimeout("timed out"), True, "ReadTimeout", id="fault-at-error"),
        pytest.param(KeyboardInterrupt(), False, None, id="ctrl-c-at-info"),
    ],
)
def test_record_fatal_log_level_contract(
    monkeypatch, caplog, exc, expect_error_level, message_substring
):
    """The one journal trace of a fatal turn emits ``session.fatal.recorded``
    with the sanitized text — at ERROR for genuine faults; a Ctrl-C routes
    through the same chokepoint but is a user action, not a fault, and must
    not add an ERROR-level line per CLI interrupt."""
    import logging

    monkeypatch.setattr("turnstone.core.session.persist_last_error", lambda ws_id, msg: None)
    monkeypatch.setattr("turnstone.core.session.sanitize_error_text", lambda text, **kw: text)

    class _UI:
        def on_error(self, msg: str) -> None:
            pass

    stub = _record_fatal_stub(_UI(), {})
    with caplog.at_level(logging.INFO, logger="turnstone.core.session"):
        ChatSession._record_fatal_error(stub, exc)  # type: ignore[arg-type]

    recorded = [r for r in caplog.records if "session.fatal.recorded" in r.message]
    assert recorded
    if expect_error_level:
        assert any(r.levelno == logging.ERROR for r in recorded)
    else:
        assert all(r.levelno < logging.ERROR for r in recorded)
        assert any(r.levelno == logging.INFO for r in recorded)
    if message_substring:
        assert any(message_substring in r.message for r in recorded)


def test_backend_auth_unavailable_names_the_mint_not_the_key():
    """The prefix here IS the exception text, so no ``raw_tail`` is appended,
    and the hint points at the mint configuration, not the static key."""
    from turnstone.core.session import BackendAuthUnavailableError

    exc = BackendAuthUnavailableError(
        "Delegated backend authentication unavailable for model alias 'gw'"
    )
    msg = _format(_stub(), exc)
    assert msg is not None
    assert "model alias 'gw'" in msg
    assert "check its auth mode and gateway audience" in msg
    assert "NOT the alias's static API key" in msg
    assert "raw=" not in msg
    assert msg.count("unavailable for model alias 'gw'") == 1
