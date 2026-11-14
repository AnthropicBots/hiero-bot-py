# app/utils/logger.py — Structured logging

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.utils.settings import settings

_STANDARD_LOG_RECORD_ATTRS = frozenset({
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
})


class JSONLogFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        # Include custom extra fields if provided via extra={...} or attributes on record
        if hasattr(record, "extra") and isinstance(record.extra, dict):  # type: ignore[attr-defined]
            for key, val in record.extra.items():  # type: ignore[attr-defined]
                try:
                    json.dumps(val)
                    log_obj[key] = val
                except (TypeError, OverflowError):
                    log_obj[key] = str(val)

        for key, val in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_ATTRS and not key.startswith("_") and key != "extra":
                try:
                    json.dumps(val)
                    log_obj[key] = val
                except (TypeError, OverflowError):
                    log_obj[key] = str(val)

        return json.dumps(log_obj)


def is_json_logging(log_format: str | None = None) -> bool:
    fmt = (log_format or getattr(settings, "log_format", "auto")).lower()
    if fmt == "json":
        return True
    if fmt == "text":
        return False
    return bool(getattr(settings, "is_production", False))


def get_formatter(log_format: str | None = None) -> logging.Formatter:
    if is_json_logging(log_format):
        return JSONLogFormatter()
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    return logging.Formatter(fmt, datefmt="%H:%M:%S")


def setup_logging(level: str = "INFO", log_format: str = "auto") -> None:
    """Configures handlers and formatters on the root/parent hiero logger."""
    hiero_logger = logging.getLogger("hiero")
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    hiero_logger.setLevel(resolved_level)

    formatter = get_formatter(log_format)

    if not hiero_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        hiero_logger.addHandler(handler)
    else:
        for existing_handler in hiero_logger.handlers:
            existing_handler.setFormatter(formatter)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"hiero.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(get_formatter())
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    return logger
