"""Bounded recovery for product model completions, independent of their harness."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turnstone.core.deadline import DeadlineCancelledError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from turnstone.core.model_turn import ModelTurnResult


MAX_COMPLETION_REISSUES = 2


class ModelTurnLocalError(RuntimeError):
    """A local consumer failed; its cause must never drive provider recovery."""

    def __init__(self, error: Exception, *, stage: str = "processing") -> None:
        self.stage = stage
        super().__init__(f"Local model-call {stage} failed ({type(error).__name__}).")


@contextmanager
def local_model_call(
    stage: str,
    *,
    enabled: bool = True,
    passthrough: tuple[type[Exception], ...] = (),
) -> Iterator[None]:
    """Keep local faults off provider recovery; stage labels must be fixed text."""
    try:
        yield
    except (ModelTurnLocalError, DeadlineCancelledError):
        raise
    except passthrough:
        raise
    except Exception as error:
        if enabled:
            raise ModelTurnLocalError(error, stage=stage) from error
        raise


class CompletionRecoveryError(RuntimeError):
    """A product model call failed after dispatch, retaining its cause and replay hazard.

    ``native_tools_enabled`` is the replay hazard of the failed attempt: the
    accepted request's final posture, or ``False`` for a request the adapter
    rejected before returning an iterator, because nothing ran. Nonstreaming
    callers receive this only when recovery ends. Streaming callers apply the
    same controller themselves after finalizing their visible attempt.
    """

    def __init__(
        self,
        message: str,
        *,
        native_tools_enabled: bool | None,
        error: Exception | None = None,
    ) -> None:
        self.native_tools_enabled = native_tools_enabled
        self.error = error
        super().__init__(message)


class EmptyCompletionError(CompletionRecoveryError):
    """A billed ordinary stop without an answer, tool call, or native output."""

    def __init__(self, result: ModelTurnResult) -> None:
        self.result = result
        message = "Model returned no answer or tool call. Retry the turn or choose another model."
        if result.native_tools_enabled is not False:
            message += " Automatic retry was skipped because the request may run server-side tools."
        super().__init__(message, native_tools_enabled=result.native_tools_enabled)


def completion_cause(error: BaseException) -> BaseException:
    """The provider cause for diagnostics, never a local consumer's exception."""
    if isinstance(error, CompletionRecoveryError) and error.error is not None:
        return error.error
    return error


@dataclass
class CompletionRecovery:
    """One allowance shared by empty completions and accepted stream deaths."""

    max_reissues: int = MAX_COMPLETION_REISSUES
    reissues: int = 0

    def consume_reissue(
        self,
        error: BaseException,
        *,
        retryable: bool,
        stopped: bool = False,
        require_safe_replay: bool = True,
    ) -> bool:
        """Consume one eligible reissue, returning False without spending on refusal.

        An identical reissue never bypasses native-tool safety and never repeats
        a context overflow: shrinking is the compaction owner's separate path.
        """
        if stopped or isinstance(error, ModelTurnLocalError) or self.reissues >= self.max_reissues:
            return False
        if require_safe_replay and (
            not isinstance(error, CompletionRecoveryError)
            or error.native_tools_enabled is not False
        ):
            return False
        if not isinstance(error, EmptyCompletionError) and (
            not retryable or (require_safe_replay and is_context_overflow(error))
        ):
            return False
        self.reissues += 1
        return True


# ---------------------------------------------------------------------------
# Backend boundary exception classification
# ---------------------------------------------------------------------------
#
# ``ChatSession._record_fatal_error`` routes a fatal exception through
# ``ChatSession._format_backend_error``, which matches the exception's class
# name against the sets below, and :func:`is_context_overflow` uses the same
# sets as its class gate.  Matching by name keeps both free of httpx / openai /
# anthropic imports — the SDKs each define their own subclasses, but the names
# (``ReadTimeout``, ``APITimeoutError``, …) are stable across them and the
# OpenAI and Anthropic SDKs use the same names.
#
# Module scope (rather than ``ClassVar`` constants on ``ChatSession``) keeps the
# formatter testable against lightweight stubs that don't subclass the session,
# and lets the session import one classification table instead of keeping a
# private copy.  These sets are this module's public contract.

BACKEND_TIMEOUT_EXC_NAMES: frozenset[str] = frozenset(
    {"ReadTimeout", "WriteTimeout", "PoolTimeout", "APITimeoutError"}
)
BACKEND_CONNECT_EXC_NAMES: frozenset[str] = frozenset(
    {"ConnectTimeout", "ConnectError", "APIConnectionError"}
)
BACKEND_NOT_FOUND_EXC_NAMES: frozenset[str] = frozenset({"NotFoundError"})
BACKEND_AUTH_EXC_NAMES: frozenset[str] = frozenset({"AuthenticationError", "PermissionDeniedError"})
BACKEND_RATE_LIMIT_EXC_NAMES: frozenset[str] = frozenset(
    {"RateLimitError", "UpstreamRateLimitError"}
)
BACKEND_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset({"UpstreamTransientError"})
# Backend-reported stream errors stay OUT of ``BACKEND_KNOWN_EXC_NAMES``:
# their messages can carry a real context-window rejection, which
# ``is_context_overflow`` must see before the formatter categorizes them.  The
# OpenAI SDK raises ``APIError`` for an in-band SSE error object;
# ``UpstreamResponseError`` is Turnstone's equivalent for a compatibility
# proxy that returns the same error as an HTTP-200 JSON body.  Its classified
# rate-limit and transient subclasses stay IN the known set above so their
# token-quota wording cannot be mistaken for context overflow.
BACKEND_REPORTED_EXC_NAMES: frozenset[str] = frozenset({"APIError", "UpstreamResponseError"})
# Mid-response stream deaths: the normalized shape every guarded iterator
# raises (``IncompleteStreamError`` from ``drain_stream`` /
# ``transport_guarded``) plus the raw HTTPX/HTTPX2 names for any future
# unguarded path (defense in depth).  Unioning them into
# ``BACKEND_KNOWN_EXC_NAMES`` is required — ``ChatSession._format_backend_error`` gates
# on that set before the branch lookups — and makes these three names
# ineligible for ``is_context_overflow``'s text-based overflow detection (its class
# self-gate): harmless, since their texts are fixed transport/SSL strings
# that never carry overflow phrases.
BACKEND_STREAM_EXC_NAMES: frozenset[str] = frozenset(
    {"IncompleteStreamError", "ReadError", "RemoteProtocolError"}
)

BACKEND_KNOWN_EXC_NAMES: frozenset[str] = (
    BACKEND_TIMEOUT_EXC_NAMES
    | BACKEND_CONNECT_EXC_NAMES
    | BACKEND_NOT_FOUND_EXC_NAMES
    | BACKEND_AUTH_EXC_NAMES
    | BACKEND_RATE_LIMIT_EXC_NAMES
    | BACKEND_TRANSIENT_EXC_NAMES
    | BACKEND_STREAM_EXC_NAMES
)
# The adapter-raised overflow: a SUCCESSFUL response whose stop reason says
# the context window filled (``providers.ContextWindowExceededError``, raised
# by the Anthropic adapter on ``model_context_window_exceeded``).  By name,
# like the sets above, so this helper stays free of provider imports; kept
# OUT of ``BACKEND_KNOWN_EXC_NAMES`` because ``is_context_overflow`` must say
# yes to it, not skip it as an already-classified error.
CTX_OVERFLOW_EXC_NAMES: frozenset[str] = frozenset({"ContextWindowExceededError"})


def is_context_overflow(exc: BaseException) -> bool:
    """Classify overflow independently of permission to issue another inference.

    Adapter stop reasons have a dedicated class. Request rejections instead use
    different HTTP statuses across compatible endpoints, so their text decides.
    Known transport/auth/rate-limit classes are excluded: token quota wording
    must not turn a transient rate limit into a context reduction.

    Local consumer faults never qualify. The wrapper's replay hazard is not
    consulted: compaction shrinks the next request rather than replaying the
    failed one, so an accepted overflow may be compacted at any tool posture.
    """
    if isinstance(exc, ModelTurnLocalError):
        return False
    exc = completion_cause(exc)
    if type(exc).__name__ in CTX_OVERFLOW_EXC_NAMES:
        return True
    if type(exc).__name__ in BACKEND_KNOWN_EXC_NAMES:
        return False
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "context length",
            "maximum context",
            "available context size",
            "context window",
            "context limit",
            "prompt is too long",
            "input is too long",
            "reduce the length of the input",
            "maximum number of tokens",
        )
    )
