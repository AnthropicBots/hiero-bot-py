from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WebhookDelivery


async def is_replay(db: AsyncSession, delivery_id: str) -> bool:
    if not delivery_id:
        return False

    try:
        async with db.begin_nested():
            db.add(WebhookDelivery(id=delivery_id))
            await db.flush()
    except IntegrityError:
        existing = await db.scalar(
            select(WebhookDelivery.id).where(WebhookDelivery.id == delivery_id)
        )
        if existing:
            return True
        raise

    # Persist the claim immediately. Without this, events that write nothing
    # else (ping, skipped/no-config, unknown commands) roll back at request end
    # on PostgreSQL and the delivery id is never recorded, so replays pass.
    await db.commit()
    return False