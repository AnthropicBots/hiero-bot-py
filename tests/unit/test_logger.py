# tests/unit/test_logger.py — Unit tests for structured logging

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from io import StringIO
from typing import Any

import pytest

from app.utils.logger import (
    MAX_SERIALIZATION_DEPTH,
    JSONLogFormatter,
    get_formatter,
    get_logger,
    is_json_logging,
    setup_logging,
)
from app.utils.settings import settings


@pytest.fixture(autouse=True)
def restore_hiero_logger():
    """Restore global hiero logger state (handlers, level, propagate) after each test."""
    hiero_logger = logging.getLogger("hiero")
    original_handlers = list(hiero_logger.handlers)
    original_formatters = [
        (handler, handler.formatter) for handler in original_handlers
    ]
    original_level = hiero_logger.level
    original_propagate = hiero_logger.propagate

    yield

    for handler in hiero_logger.handlers:
        if handler not in original_handlers:
            try:
                handler.close()
            except Exception:
                pass

    hiero_logger.handlers = original_handlers

    for handler, formatter in original_formatters:
        handler.setFormatter(formatter)

    hiero_logger.setLevel(original_level)
    hiero_logger.propagate = original_propagate


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


def test_json_formatter_circular_references():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.circular",
        level=logging.INFO,
        pathname="test.py",
        lineno=60,
        msg="Circular ref test",
        args=(),
        exc_info=None,
    )

    circular_dict: dict = {"key": "value"}
    circular_dict["self"] = circular_dict
    record.circular = circular_dict  # type: ignore[attr-defined]

    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["circular"]["self"] == "<circular reference>"


def test_json_formatter_nested_non_serializable():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.nested",
        level=logging.INFO,
        pathname="test.py",
        lineno=70,
        msg="Nested objects",
        args=(),
        exc_info=None,
    )

    class InnerObj:
        def __str__(self):
            return "InnerVal"

    record.nested = {"list": [1, {"obj": InnerObj()}]}  # type: ignore[attr-defined]

    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["nested"]["list"][1]["obj"] == "InnerVal"


def test_json_formatter_non_finite_floats():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.float",
        level=logging.INFO,
        pathname="test.py",
        lineno=80,
        msg="Float tests",
        args=(),
        exc_info=None,
    )
    record.nan_val = float("nan")  # type: ignore[attr-defined]
    record.inf_val = float("inf")  # type: ignore[attr-defined]

    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["nan_val"] == "nan"
    assert data["inf_val"] == "inf"


def test_json_formatter_conversion_failure_handling():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.fail",
        level=logging.INFO,
        pathname="test.py",
        lineno=90,
        msg="Failed str conversion",
        args=(),
        exc_info=None,
    )

    class BrokenStrObj:
        def __str__(self):
            raise RuntimeError("explode")

        def __repr__(self):
            raise RuntimeError("explode repr")

    record.broken = BrokenStrObj()  # type: ignore[attr-defined]

    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert "<unserializable BrokenStrObj>" in data["broken"]


def test_json_formatter_reserved_field_protection():
    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hiero.core",
        level=logging.WARNING,
        pathname="test.py",
        lineno=100,
        msg="Real message",
        args=(),
        exc_info=None,
    )
    # Attempt to overwrite core reserved fields via extra
    record.message = "Forged message"  # type: ignore[attr-defined]
    record.level = "FAKE_LEVEL"  # type: ignore[attr-defined]
    record.logger = "fake_logger"  # type: ignore[attr-defined]
    record.timestamp = "fake_timestamp"  # type: ignore[attr-defined]

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["message"] == "Real message"
    assert data["level"] == "WARNING"
    assert data["logger"] == "hiero.core"
    assert data["timestamp"] != "fake_timestamp"


def test_is_json_logging_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "log_format", "json")
    assert is_json_logging() is True
    assert is_json_logging("json") is True

    monkeypatch.setattr(settings, "log_format", "text")
    assert is_json_logging() is False
    assert is_json_logging("text") is False


def test_is_json_logging_invalid():
    with pytest.raises(ValueError, match="Invalid log_format 'invalid'"):
        is_json_logging("invalid")


def test_setup_logging_invalid():
    with pytest.raises(ValueError, match="Invalid log_format 'unknown'"):
        setup_logging(log_format="unknown")


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
    # Child logger must not install its own handlers and must propagate
    assert len(log.handlers) == 0
    assert log.propagate is True


def test_duplicate_prevention_and_propagation():
    stream = StringIO()
    root_stream = StringIO()

    custom_handler = logging.StreamHandler(stream)
    root_handler = logging.StreamHandler(root_stream)

    hiero_logger = logging.getLogger("hiero")
    root_logger = logging.getLogger()

    hiero_logger.handlers.clear()
    hiero_logger.addHandler(custom_handler)
    hiero_logger.setLevel(logging.INFO)

    root_logger.addHandler(root_handler)

    try:
        setup_logging(level="INFO", log_format="json")

        child_logger = get_logger("propagation_test")

        assert len(child_logger.handlers) == 0
        assert child_logger.propagate is True
        assert hiero_logger.propagate is False

        child_logger.info("Test message for propagation")

        lines = [
            line for line in stream.getvalue().splitlines()
            if line.strip()
        ]

        assert len(lines) == 1

        record_data = json.loads(lines[0])
        assert record_data["message"] == "Test message for propagation"
        assert record_data["logger"] == "hiero.propagation_test"

        assert root_stream.getvalue() == ""

    finally:
        root_logger.removeHandler(root_handler)
        root_handler.close()


def test_logger_exception_emission():
    stream = StringIO()
    custom_handler = logging.StreamHandler(stream)
    custom_handler.setFormatter(JSONLogFormatter())

    hiero_logger = logging.getLogger("hiero")
    hiero_logger.handlers.clear()
    hiero_logger.addHandler(custom_handler)
    hiero_logger.setLevel(logging.INFO)

    log = get_logger("exception_test")

    try:
        raise ZeroDivisionError("division by zero test")
    except ZeroDivisionError:
        log.exception("Caught an exception")

    output = stream.getvalue().strip()
    data = json.loads(output)

    assert data["level"] == "ERROR"
    assert data["message"] == "Caught an exception"
    assert "exception" in data
    assert "ZeroDivisionError: division by zero test" in data["exception"]


def test_production_and_development_output_format(monkeypatch: pytest.MonkeyPatch):
    stream = StringIO()
    handler = logging.StreamHandler(stream)

    hiero_logger = logging.getLogger("hiero")
    hiero_logger.handlers.clear()
    hiero_logger.addHandler(handler)

    # 1. Production output test (JSON)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "log_format", "auto")
    setup_logging(level="INFO", log_format="auto")
    hiero_logger.handlers[0].setStream(stream)

    log = get_logger("prod_test")
    log.info("Production event")

    prod_out = stream.getvalue().strip().split("\n")[-1]
    prod_data = json.loads(prod_out)
    assert prod_data["message"] == "Production event"
    assert prod_data["level"] == "INFO"

    # 2. Development output test (text)
    stream.truncate(0)
    stream.seek(0)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "log_format", "auto")
    setup_logging(level="INFO", log_format="auto")
    hiero_logger.handlers[0].setStream(stream)

    log.info("Development event")
    dev_out = stream.getvalue().strip().split("\n")[-1]
    assert "[INFO] hiero.prod_test — Development event" in dev_out


def test_json_formatter_deeply_nested_dict_recursion_prevention():
    formatter = JSONLogFormatter()
    # Build a 1200-deep nested dictionary (exceeds default sys.getrecursionlimit() = 1000)
    nested: dict[str, Any] = {"leaf": "deep_value"}
    for _ in range(1200):
        nested = {"inner": nested}

    record = logging.LogRecord(
        name="hiero.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Deeply nested dict test",
        args=(),
        exc_info=None,
    )
    record.nested = nested  # type: ignore[attr-defined]

    # Must format cleanly without raising RecursionError
    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["message"] == "Deeply nested dict test"

    # Verify traversal hits "<max depth exceeded>" at MAX_SERIALIZATION_DEPTH
    curr = data["nested"]
    depth = 0
    while isinstance(curr, dict) and "inner" in curr:
        curr = curr["inner"]
        depth += 1
    assert curr == "<max depth exceeded>"
    assert depth == MAX_SERIALIZATION_DEPTH


def test_json_formatter_deeply_nested_list_recursion_prevention():
    formatter = JSONLogFormatter()
    # Build a 1200-deep nested list (exceeds default sys.getrecursionlimit() = 1000)
    nested: list[Any] = ["bottom_item"]
    for _ in range(1200):
        nested = [nested]

    record = logging.LogRecord(
        name="hiero.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Deeply nested list test",
        args=(),
        exc_info=None,
    )
    record.nested = nested  # type: ignore[attr-defined]

    # Must format cleanly without raising RecursionError
    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["message"] == "Deeply nested list test"

    # Verify traversal hits "<max depth exceeded>" at MAX_SERIALIZATION_DEPTH
    curr = data["nested"]
    depth = 0
    while isinstance(curr, list) and len(curr) == 1 and isinstance(curr[0], list):
        curr = curr[0]
        depth += 1
    assert curr == ["<max depth exceeded>"]
    assert depth == MAX_SERIALIZATION_DEPTH - 1
