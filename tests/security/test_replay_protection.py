# tests/security/test_replay_protection.py — delivery replay guard (#20)

import time

from cachetools import TTLCache

from app.github import replay_guard
from app.github.replay_guard import is_replay


def fresh_cache(monkeypatch, maxsize=10_000, ttl=600):
    monkeypatch.setattr(
        replay_guard, "_seen_deliveries", TTLCache(maxsize=maxsize, ttl=ttl)
    )


def test_first_delivery_is_not_a_replay(monkeypatch):
    fresh_cache(monkeypatch)
    assert is_replay("delivery-1") is False


def test_same_delivery_id_twice_is_a_replay(monkeypatch):
    fresh_cache(monkeypatch)

    assert is_replay("delivery-1") is False
    assert is_replay("delivery-1") is True


def test_replay_is_detected_many_times(monkeypatch):
    fresh_cache(monkeypatch)
    is_replay("delivery-1")

    assert all(is_replay("delivery-1") for _ in range(10))


def test_distinct_deliveries_are_independent(monkeypatch):
    fresh_cache(monkeypatch)

    assert is_replay("a") is False
    assert is_replay("b") is False
    assert is_replay("a") is True


def test_missing_delivery_id_is_not_treated_as_a_replay(monkeypatch):
    """GitHub always sends one; absence must not wedge the endpoint shut."""
    fresh_cache(monkeypatch)

    assert is_replay("") is False
    assert is_replay("") is False


def test_ids_expire_after_the_ttl(monkeypatch):
    fresh_cache(monkeypatch, ttl=0.05)
    is_replay("delivery-1")

    time.sleep(0.1)

    assert is_replay("delivery-1") is False


def test_cache_is_bounded(monkeypatch):
    """An attacker replaying unique IDs must not grow memory without limit."""
    fresh_cache(monkeypatch, maxsize=100)

    for i in range(1000):
        is_replay(f"delivery-{i}")

    assert len(replay_guard._seen_deliveries) <= 100


def test_eviction_under_pressure_does_not_error(monkeypatch):
    fresh_cache(monkeypatch, maxsize=10)

    for i in range(50):
        is_replay(f"delivery-{i}")

    assert is_replay("delivery-49") is True


def test_default_guard_has_a_bounded_size_and_ttl():
    assert replay_guard._seen_deliveries.maxsize == 10_000
    assert replay_guard._DELIVERY_TTL_SECONDS == 600
