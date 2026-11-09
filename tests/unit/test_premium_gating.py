# tests/unit/test_premium_gating.py
"""Verify the is_premium() soft-check helper."""

import types

import pytest

from app.billing.gating import is_premium


@pytest.mark.parametrize(
    "plan_tier, expected",
    [
        ("premium", True),
        ("free", False),
        ("enterprise", False),  # unknown tier should not be treated as premium
    ],
)
def test_is_premium(plan_tier, expected):
    # Use a plain namespace so we don't trigger SQLAlchemy instrumentation.
    account = types.SimpleNamespace(plan_tier=plan_tier)
    assert is_premium(account) is expected


def test_is_premium_none_returns_false():
    """is_premium(None) must return False, not raise AttributeError."""
    assert is_premium(None) is False
