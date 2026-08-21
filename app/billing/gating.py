# app/billing/gating.py — Premium feature entitlement checks

from __future__ import annotations

from fastapi import HTTPException, status

from app.db.models import Account


def require_premium_account(account: Account) -> None:
    """Enforce premium subscription entitlement check."""
    if account.plan_tier != "premium":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "Payment Required",
                "message": f"Account '{account.org_login}' is on the '{account.plan_tier}' plan. This feature requires a Premium subscription.",
                "upsell_url": "https://hiero-bot.com/pricing",
            },
        )
