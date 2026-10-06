"""Logging setup.

structlog is configured so that the same event stream can be rendered as
colourful key/value lines locally (``LOG_FORMAT=console``) or as one JSON
object per line in production (``LOG_FORMAT=json``), which is what Render's log
search expects.

Standard-library loggers -- aiogram, httpx, searx -- are routed through the
same pipeline, so third-party output is formatted identically.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor

# Libraries that log a line per HTTP request or per update are demoted; their
# useful failures still come through at WARNING.
_NOISY_LOGGERS: dict[str, int] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "hpack": logging.WARNING,
    "aiogram.event": logging.WARNING,
    "openai": logging.WARNING,
    "anthropic": logging.WARNING,
    "urllib3": logging.WARNING,
    "trafilatura": logging.WARNING,
}


def _drop_color_message_key(_: object, __: str, event_dict: EventDict) -> EventDict:
    """uvicorn/granian duplicate the message under ``color_message``."""
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog and the stdlib root logger. Idempotent."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _drop_color_message_key,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace handlers rather than adding, so repeated calls do not duplicate lines.
    root.handlers = [handler]
    root.setLevel(numeric_level)

    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(noisy_level, numeric_level))


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, optionally pre-loaded with context."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger.bind(**initial) if initial else logger


def bind_request_context(**values: Any) -> None:
    """Attach values (``user_id``, ``mode``, ...) to every log line in this task."""
    structlog.contextvars.bind_contextvars(**values)


def clear_request_context() -> None:
    """Drop everything bound by :func:`bind_request_context`."""
    structlog.contextvars.clear_contextvars()
