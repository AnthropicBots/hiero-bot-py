# tests/unit/test_billing.py

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.billing.gating import require_premium_account
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
