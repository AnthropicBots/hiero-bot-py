# tests/unit/test_session_gc.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.auth.session import purge_expired_sessions
from app.db.models import Session
from app.scheduler.jobs import BotScheduler


def make_session(session_id: str, user_id: int, expires_in: timedelta) -> Session:
    return Session(
        id=session_id,
        user_id=user_id,
        expires_at=datetime.now(timezone.utc) + expires_in,
    )


@pytest.mark.asyncio
async def test_purge_deletes_only_expired_sessions(db):
    db.add(make_session("expired-1", 1, timedelta(days=-1)))
    db.add(make_session("expired-2", 1, timedelta(seconds=-1)))
    db.add(make_session("active-1", 1, timedelta(days=7)))
    await db.commit()

    purged = await purge_expired_sessions(db)

    assert purged == 2

    remaining = (await db.execute(select(Session))).scalars().all()
    assert [s.id for s in remaining] == ["active-1"]


@pytest.mark.asyncio
async def test_purge_deletes_sessions_at_expiration_boundary(db):
    boundary = datetime.now(timezone.utc)
    db.add(
        Session(
            id="expired-at-boundary",
            user_id=1,
            expires_at=boundary,
        )
    )
    await db.commit()

    assert await purge_expired_sessions(db) == 1


@pytest.mark.asyncio
async def test_purge_returns_zero_when_nothing_expired(db):
    db.add(make_session("active-1", 1, timedelta(days=7)))
    await db.commit()

    assert await purge_expired_sessions(db) == 0


@pytest.mark.asyncio
async def test_purge_is_a_noop_on_an_empty_table(db):
    assert await purge_expired_sessions(db) == 0


@pytest.mark.asyncio
async def test_run_session_gc_delegates_to_purge_and_returns_count():
    scheduler = BotScheduler(AsyncMock(), AsyncMock())

    with patch("app.scheduler.jobs.purge_expired_sessions", AsyncMock(return_value=3)) as mock_purge:
        purged = await scheduler.run_session_gc()

    assert purged == 3
    mock_purge.assert_awaited_once()
