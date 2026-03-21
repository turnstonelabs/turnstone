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
_QUIET_LOGGERS = ("httpx", "httpcore", "openai", "anthropic", "uvicorn.access")


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
