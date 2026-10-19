# tests/unit/test_logger.py — Unit tests for structured logging

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

import pytest

from app.utils.logger import (
    JSONLogFormatter,
    get_formatter,
    get_logger,
    is_json_logging,
    setup_logging,
)
from app.utils.settings import settings


def test_json_formatter_basic():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Test message with %s",
        args=("param",),
        exc_info=None,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["logger"] == "hiero.test"
    assert data["message"] == "Test message with param"
    assert "timestamp" in data

    # Verify timestamp is valid ISO 8601 UTC
    dt = datetime.fromisoformat(data["timestamp"])
    assert dt.tzinfo is not None


def test_json_formatter_exception():
    formatter = JSONLogFormatter()
    try:
        raise ValueError("Something went wrong")
    except ValueError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="hiero.error",
        level=logging.ERROR,
        pathname="test.py",
        lineno=25,
        msg="An error occurred",
        args=(),
        exc_info=exc_info,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "ERROR"
    assert data["message"] == "An error occurred"
    assert "exception" in data
    assert "ValueError: Something went wrong" in data["exception"]


def test_json_formatter_extra_fields():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.webhook",
        level=logging.INFO,
        pathname="test.py",
        lineno=40,
        msg="Received webhook",
        args=(),
        exc_info=None,
    )
    record.delivery_id = "del-12345"  # type: ignore[attr-defined]
    record.repo = "AnthropicBots/hiero-bot-py"  # type: ignore[attr-defined]

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["delivery_id"] == "del-12345"
    assert data["repo"] == "AnthropicBots/hiero-bot-py"


def test_json_formatter_non_serializable_extra():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.custom",
        level=logging.INFO,
        pathname="test.py",
        lineno=50,
        msg="Custom object test",
        args=(),
        exc_info=None,
    )

    class CustomObj:
        def __str__(self):
            return "CustomObjRepr"

    record.custom = CustomObj()  # type: ignore[attr-defined]

    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["custom"] == "CustomObjRepr"


def test_is_json_logging_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "log_format", "json")
    assert is_json_logging() is True
    assert is_json_logging("json") is True

    monkeypatch.setattr(settings, "log_format", "text")
    assert is_json_logging() is False
    assert is_json_logging("text") is False


def test_is_json_logging_auto(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "log_format", "auto")

    monkeypatch.setattr(settings, "environment", "production")
    assert is_json_logging() is True

    monkeypatch.setattr(settings, "environment", "development")
    assert is_json_logging() is False


def test_get_formatter_selection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "log_format", "json")
    formatter = get_formatter()
    assert isinstance(formatter, JSONLogFormatter)

    monkeypatch.setattr(settings, "log_format", "text")
    formatter = get_formatter()
    assert not isinstance(formatter, JSONLogFormatter)


def test_setup_logging(monkeypatch: pytest.MonkeyPatch):
    setup_logging(level="WARNING", log_format="json")
    hiero_logger = logging.getLogger("hiero")

    assert hiero_logger.level == logging.WARNING
    assert len(hiero_logger.handlers) >= 1
    assert isinstance(hiero_logger.handlers[0].formatter, JSONLogFormatter)

    # Reconfigure back to development text logging
    setup_logging(level="INFO", log_format="text")
    assert hiero_logger.level == logging.INFO
    assert not isinstance(hiero_logger.handlers[0].formatter, JSONLogFormatter)


def test_get_logger_naming():
    log = get_logger("unit_test")
    assert log.name == "hiero.unit_test"
    assert len(log.handlers) >= 1
