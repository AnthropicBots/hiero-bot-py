from datetime import UTC, datetime


def parse_updated(value: str) -> datetime:
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def test_zulu_timestamp_parses_and_stays_aware():
    parsed = parse_updated("2024-01-01T00:00:00Z")
    assert parsed.tzinfo is not None
    assert (datetime.now(UTC) - parsed).days >= 0


def test_naive_timestamp_defaults_to_utc():
    parsed = parse_updated("2024-01-01T00:00:00")
    assert parsed.tzinfo is UTC
