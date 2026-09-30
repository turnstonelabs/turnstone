"""Structured logging configuration for all Turnstone services."""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Context variables — set in request handlers, workstream managers, etc.
# Any non-empty value is automatically injected into every log event.
# ---------------------------------------------------------------------------

ctx_node_id: ContextVar[str] = ContextVar("node_id", default="")
ctx_ws_id: ContextVar[str] = ContextVar("ws_id", default="")
ctx_user_id: ContextVar[str] = ContextVar("user_id", default="")
ctx_request_id: ContextVar[str] = ContextVar("request_id", default="")

_CONTEXT_VARS: list[tuple[ContextVar[str], str]] = [
    (ctx_node_id, "node_id"),
    (ctx_ws_id, "ws_id"),
    (ctx_user_id, "user_id"),
    (ctx_request_id, "request_id"),
]

# Third-party loggers that are noisy at INFO level.
_QUIET_LOGGERS = (
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "openai",
    "anthropic",
    "uvicorn.access",
)


def _request_origin(url: Any) -> str:
    """Render an httpx2 request URL as its scheme, host and port."""
    return f"{url.scheme}://{url.netloc.decode('ascii', 'replace')}"


class _RedactCredentialsFilter(logging.Filter):
    """Cut httpx2's request URLs to their origin and redact the rest of each record.

    httpx2 logs every request's full URL at INFO. Its logger is held at WARNING
    above, but importing the Anthropic SDK with ``ANTHROPIC_LOG`` set lowers it
    again, which would print the URLs of guarded fetches and provider requests.
    A model-chosen URL can carry a secret anywhere after its host (userinfo, a
    query or fragment parameter, a webhook's path), so every URL among a
    record's arguments, where httpx2 puts the request URL, is replaced by its
    scheme, host and port. They are read from the URL's attributes, so
    rendering them cannot fail. The formatted message then passes through the
    output guard's credential redactor. Only httpx2's own records pass through
    this filter: the SDKs' debug loggers enabled by ``ANTHROPIC_LOG=debug`` are
    outside it, and so is the ``httpx`` logger of Turnstone's other clients,
    which no installed SDK lowers from WARNING. A record that cannot be
    formatted passes unchanged, so logging never breaks the request that
    emitted it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        import httpx2

        from turnstone.core.output_guard import redact_credentials

        if isinstance(record.args, tuple):
            record.args = tuple(
                _request_origin(arg) if isinstance(arg, httpx2.URL) else arg for arg in record.args
            )
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_credentials(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


# Attached at import rather than in configure_logging(): the level can be
# lowered by code that never calls it, and a logger filter survives handler
# replacement.
logging.getLogger("httpx2").addFilter(_RedactCredentialsFilter())


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------


def _inject_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Add non-empty context variables to every log event."""
    for var, key in _CONTEXT_VARS:
        val = var.get("")
        if val:
            event_dict[key] = val
    return event_dict


def _add_service(service: str) -> structlog.types.Processor:
    """Return a processor that stamps *service* onto every event."""

    def _processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict["service"] = service
        return event_dict

    return _processor  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool | None = None,
    service: str = "",
) -> None:
    """Configure structured logging for a Turnstone service.

    Call this once, early in each entry-point's ``main()``.

    Parameters
    ----------
    level:
        Log level name (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``,
        ``CRITICAL``).  The ``TURNSTONE_LOG_LEVEL`` env var, if set,
        overrides this.
    json_output:
        Force JSON (``True``) or console (``False``) output.  ``None``
        auto-detects: JSON when stderr is not a TTY.  The
        ``TURNSTONE_LOG_FORMAT`` env var (``json`` / ``text``) overrides.
    service:
        Service name added to every log line (e.g. ``"server"``).
    """
    # Env-var overrides -------------------------------------------------------
    env_level = os.environ.get("TURNSTONE_LOG_LEVEL", "").upper()
    if env_level:
        level = env_level

    env_fmt = os.environ.get("TURNSTONE_LOG_FORMAT", "").lower()
    if env_fmt in ("json", "text"):
        json_output = env_fmt == "json"
    elif json_output is None:
        json_output = not sys.stderr.isatty()

    # Shared processor chain --------------------------------------------------
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,  # type: ignore[list-item]
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if service:
        processors.append(_add_service(service))

    # Renderer ----------------------------------------------------------------
    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    # structlog config (for structlog.get_logger()) ---------------------------
    structlog.configure(
        processors=[
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # stdlib handler (for logging.getLogger()) --------------------------------
    # foreign_pre_chain runs on events from stdlib loggers (not structlog).
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet noisy third-party loggers -----------------------------------------
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _ensure_stdlib_factory() -> None:
    """Ensure structlog routes through stdlib even before configure_logging().

    Without this, ``structlog.get_logger()`` defaults to ``PrintLogger``
    which bypasses stdlib handlers (and pytest caplog).  Calling
    ``configure_logging()`` later overwrites this minimal config.
    """
    cfg = structlog.get_config()
    if not isinstance(cfg.get("logger_factory"), structlog.stdlib.LoggerFactory):
        structlog.configure(
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
        )


_ensure_stdlib_factory()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger backed by the stdlib."""
    result: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return result


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def add_log_args(parser: Any) -> None:
    """Add ``--log-level`` and ``--log-format`` arguments to *parser*."""
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: %(default)s)",
    )
    parser.add_argument(
        "--log-format",
        default="auto",
        choices=["auto", "json", "text"],
        help="Log output format (default: auto — JSON when stderr is not a TTY)",
    )


def configure_logging_from_args(args: Any, service: str) -> None:
    """Call :func:`configure_logging` using parsed CLI arguments."""
    configure_logging(
        level=args.log_level,
        json_output={"json": True, "text": False}.get(args.log_format),
        service=service,
    )
