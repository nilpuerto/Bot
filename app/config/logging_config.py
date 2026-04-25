"""Structured logging setup.

Uses ``structlog`` with a JSON renderer in production and a coloured
key-value renderer in ``dev``.  All log records share a bound context
(``trade_id``, ``signal_id``, ``user_id`` where applicable) so that
trades can be traced end-to-end from ingestion to close.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.config.settings import settings


def _build_processors(is_dev: bool) -> list[Any]:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if is_dev:
        shared.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        shared.append(structlog.processors.JSONRenderer())
    return shared


def configure_logging() -> None:
    """Configure stdlib logging and structlog. Call once at startup."""
    is_dev = settings.app_env.lower() == "dev"
    level = getattr(logging, settings.log_level, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    # Quiet down overly chatty third-party libs.
    for noisy in ("httpx", "httpcore", "telegram.ext", "telegram.request"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    structlog.configure(
        processors=_build_processors(is_dev),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()
