# tests/unit/test_billing.py

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.billing.gating import (
    is_premium_account,
    require_premium_account,
    tier_for_subscription_status,
)
from app.db.models import Account


def test_require_premium_account_free_tier():
    acc = Account(github_installation_id=1, org_login="FreeOrg", plan_tier="free")
    with pytest.raises(HTTPException) as exc_info:
        require_premium_account(acc)
    assert exc_info.value.status_code == 402
    assert "FreeOrg" in exc_info.value.detail["message"]


def test_require_premium_account_premium_tier():
    acc = Account(github_installation_id=2, org_login="ProOrg", plan_tier="premium")
    # Should not raise exception
    require_premium_account(acc)


def test_missing_account_is_not_premium():
    assert is_premium_account(None) is False


def test_free_account_is_not_premium():
    acc = Account(github_installation_id=3, org_login="FreeOrg", plan_tier="free")
    assert is_premium_account(acc) is False


@pytest.mark.parametrize(
    ("subscription_status", "expected"),
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
    ],
)
def test_tier_for_subscription_status(subscription_status, expected):
    assert tier_for_subscription_status(subscription_status) == expected
