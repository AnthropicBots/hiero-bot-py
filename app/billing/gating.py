# app/billing/gating.py — Premium feature entitlement checks

from __future__ import annotations

from fastapi import HTTPException, status

from app.db.models import Account

# Stripe subscription statuses that keep an account on Premium.
# `incomplete`, `incomplete_expired`, `past_due`, `unpaid`, `canceled`,
# and `paused` all map to free, so a failed or lapsed payment does not
# leave the paid features on until a later `deleted` event.
PREMIUM_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


def tier_for_subscription_status(subscription_status: str | None) -> str:
    """Map a Stripe subscription status onto `accounts.plan_tier`."""
    if subscription_status in PREMIUM_SUBSCRIPTION_STATUSES:
        return "premium"
    return "free"


def is_premium_account(account: Account | None) -> bool:
    """True only for an account whose stored tier is premium.

    A missing account (installation never synced into the database) is
    free. Premium features must not run just because no row exists.
    """
    return account is not None and account.plan_tier == "premium"


def require_premium_account(account: Account) -> None:
    """HTTP form of the premium check, for request handlers.

    Workflow code uses `is_premium_account` and skips the step instead.
    Raising 402 from a GitHub webhook would make GitHub retry the delivery.
    """
    if is_premium_account(account):
        return
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "error": "Payment Required",
            "message": (
                f"Account '{account.org_login}' is on the '{account.plan_tier}' plan. "
                "This feature requires a Premium subscription."
            ),
            "upsell_url": "https://hiero-bot.com/pricing",
        },
    )
