# tests/unit/test_stripe_status_mapping.py — Verify Stripe status → plan_tier mapping

import pytest

from app.billing.stripe_webhooks import _tier_for_subscription_status


@pytest.mark.parametrize(
    "status,expected_tier",
    [
        ("active", "premium"),
        ("trialing", "premium"),
        ("incomplete", "free"),
        ("incomplete_expired", "free"),
        ("past_due", "free"),
        ("unpaid", "free"),
        ("canceled", "free"),
        ("paused", "free"),
        (None, "free"),
        ("", "free"),
        ("unknown_future_status", "free"),
    ],
)
def test_tier_for_subscription_status(status, expected_tier):
    assert _tier_for_subscription_status(status) == expected_tier