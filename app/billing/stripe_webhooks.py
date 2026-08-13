# app/billing/stripe_webhooks.py — Stripe subscription webhook receiver

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import Account
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("billing.stripe")
router = APIRouter(prefix="/webhooks", tags=["billing"])


import json


def verify_stripe_signature(payload_bytes: bytes, sig_header: str, secret: str) -> bool:
    if not secret:
        return not settings.is_production
    if not sig_header:
        return False

    try:
        elements = dict(item.split("=", 1) for item in sig_header.split(","))
        t = elements.get("t")
        v1 = elements.get("v1")

        if not t or not v1:
            return False

        # Reject old signatures (> 5 minutes)
        if abs(time.time() - int(t)) > 300:
            return False

        signed_payload = f"{t}.".encode() + payload_bytes
        computed_sig = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed_sig, v1)
    except Exception:
        return False


@router.post("/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    if settings.is_production and not settings.stripe_webhook_secret:
        log.error("STRIPE_WEBHOOK_SECRET is not configured in production")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe webhook endpoint is not properly configured.",
        )

    if not verify_stripe_signature(body, sig_header, settings.stripe_webhook_secret or ""):
        log.warning("Invalid or missing Stripe webhook signature")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature",
        )

    try:
        event = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = event.get("type", "")
    data_obj = (event.get("data") or {}).get("object", {})

    log.info("Received Stripe webhook event: %s", event_type)

    if event_type in ("checkout.session.completed", "customer.subscription.created", "customer.subscription.updated"):
        metadata = data_obj.get("metadata", {})
        org_login = metadata.get("org_login") or data_obj.get("client_reference_id")
        inst_id = metadata.get("installation_id")

        account = None
        if inst_id:
            try:
                inst_int = int(inst_id)
                stmt = select(Account).where(Account.github_installation_id == inst_int)
                res = await db.execute(stmt)
                account = res.scalar_one_or_none()
            except (ValueError, TypeError):
                pass
        if not account and org_login:
            stmt = select(Account).where(Account.org_login == org_login)
            res = await db.execute(stmt)
            account = res.scalar_one_or_none()

        if account:
            account.plan_tier = "premium"
            await db.commit()
            log.info("Upgraded Account ID %d (%s) to premium tier", account.id, account.org_login)

    elif event_type == "customer.subscription.deleted":
        metadata = data_obj.get("metadata", {})
        org_login = metadata.get("org_login") or data_obj.get("client_reference_id")
        inst_id = metadata.get("installation_id")

        account = None
        if inst_id:
            try:
                inst_int = int(inst_id)
                stmt = select(Account).where(Account.github_installation_id == inst_int)
                res = await db.execute(stmt)
                account = res.scalar_one_or_none()
            except (ValueError, TypeError):
                pass
        if not account and org_login:
            stmt = select(Account).where(Account.org_login == org_login)
            res = await db.execute(stmt)
            account = res.scalar_one_or_none()

        if account:
            account.plan_tier = "free"
            await db.commit()
            log.info("Downgraded Account ID %d (%s) to free tier", account.id, account.org_login)

    return {"status": "success"}
