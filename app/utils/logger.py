# app/utils/logger.py — Structured logging

from __future__ import annotations

import json
import logging
import math
import sys
from datetime import UTC, datetime
from typing import Any

from app.utils.settings import settings

_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
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
    }
)

VALID_LOG_FORMATS = frozenset({"auto", "json", "text"})
RESERVED_JSON_FIELDS = frozenset(
    {"timestamp", "level", "logger", "message", "exception"}
)


def _safe_str(val: Any) -> str:
    try:
        return str(val)
    except Exception:
        try:
            return f"<unserializable {type(val).__name__}>"
        except Exception:
            return "<unserializable>"


def _safe_json_value(val: Any, seen: set[int] | None = None) -> Any:
    if seen is None:
        seen = set()

    if val is None or isinstance(val, (bool, int, str)):
        return val

    if isinstance(val, float):
        if not math.isfinite(val):
            return str(val)
        return val

    val_id = id(val)
    if val_id in seen:
        return "<circular reference>"

    if isinstance(val, dict):
        seen.add(val_id)
        try:
            return {_safe_str(k): _safe_json_value(v, seen) for k, v in val.items()}
        finally:
            seen.remove(val_id)

    if isinstance(val, (list, tuple, set, frozenset)):
        seen.add(val_id)
        try:
            return [_safe_json_value(item, seen) for item in val]
        finally:
            seen.remove(val_id)

    return _safe_str(val)


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
                if key in RESERVED_JSON_FIELDS:
                    continue
                log_obj[str(key)] = _safe_json_value(val)

        for key, val in record.__dict__.items():
            if (
                key not in _STANDARD_LOG_RECORD_ATTRS
                and not key.startswith("_")
                and key != "extra"
                and key not in RESERVED_JSON_FIELDS
            ):
                log_obj[str(key)] = _safe_json_value(val)

        try:
            return json.dumps(log_obj, ensure_ascii=False)
        except Exception:
            fallback_obj = {
                "timestamp": log_obj["timestamp"],
                "level": log_obj["level"],
                "logger": log_obj["logger"],
                "message": _safe_str(log_obj["message"]),
            }
            if "exception" in log_obj:
                fallback_obj["exception"] = _safe_str(log_obj["exception"])
            return json.dumps(fallback_obj, ensure_ascii=False)


def is_json_logging(log_format: str | None = None) -> bool:
    fmt = (
        log_format
        if log_format is not None
        else getattr(settings, "log_format", "auto")
    ).lower()
    if fmt not in VALID_LOG_FORMATS:
        raise ValueError(
            f"Invalid log_format '{fmt}'. Expected one of: auto, json, text"
        )
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
    """Configures a single handler and formatter on the root/parent hiero logger."""
    fmt = (log_format or "auto").lower()
    if fmt not in VALID_LOG_FORMATS:
        raise ValueError(
            f"Invalid log_format '{fmt}'. Expected one of: auto, json, text"
        )

    hiero_logger = logging.getLogger("hiero")
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    hiero_logger.setLevel(resolved_level)

    formatter = get_formatter(fmt)

    if not hiero_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        hiero_logger.addHandler(handler)
    else:
        for existing_handler in hiero_logger.handlers:
            existing_handler.setFormatter(formatter)


def _ensure_logging_initialized() -> None:
    hiero_logger = logging.getLogger("hiero")
    if not hiero_logger.handlers:
        level = getattr(settings, "log_level", "INFO")
        log_format = getattr(settings, "log_format", "auto")
        setup_logging(level=level, log_format=log_format)


def get_logger(name: str) -> logging.Logger:
    """Returns a child logger under hiero.<name> that propagates to the centrally configured hiero logger."""
    _ensure_logging_initialized()
    logger = logging.getLogger(f"hiero.{name}")
    logger.propagate = True
    return logger
