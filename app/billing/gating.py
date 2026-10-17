# app/billing/gating.py — Premium feature entitlement checks

from __future__ import annotations

from fastapi import HTTPException, status

from app.db.models import Account
from app.utils.logger import get_logger

log = get_logger("billing.gating")


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


def is_premium(account: Account | None) -> bool:
    """Return True if the account has premium access, False otherwise.

    Use this in webhook workflows where a hard HTTP error is inappropriate
    (the webhook must return 200 to GitHub regardless of tier) and we
    simply want to skip premium-only logic.

    Returns False for a missing account (None) so callers don't need to
    guard against that case separately.
    """
    if account is None:
        return False
    return account.plan_tier == "premium"