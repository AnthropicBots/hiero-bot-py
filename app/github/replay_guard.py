from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WebhookDelivery


async def is_replay(db: AsyncSession, delivery_id: str, event: str) -> bool:
    # A single insert lets the primary key arbitrate concurrent claims.
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else postgresql_insert
    stmt = (
        insert(WebhookDelivery)
        .values(id=delivery_id, event=event)
        .on_conflict_do_nothing(index_elements=[WebhookDelivery.id])
    )
    result = await db.execute(stmt)

    # Persist the claim even if dispatch makes no database writes. Close the
    # transaction before dispatch so workflow I/O never holds the claim lock.
    await db.commit()
    return result.rowcount == 0


async def release_delivery(db: AsyncSession, delivery_id: str) -> None:
    """Release a delivery claim so a retryable webhook can be processed again."""
    if not delivery_id:
        return

    await db.execute(
        delete(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
    )
    await db.commit()
