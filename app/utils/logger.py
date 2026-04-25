"""Thin re-export so callers can ``from app.utils.logger import get_logger``."""
from app.config.logging_config import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
