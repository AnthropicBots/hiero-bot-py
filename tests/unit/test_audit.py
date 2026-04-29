# tests/unit/test_audit.py

import pytest
from sqlalchemy import select
from app.utils.audit import record, VALID_ACTIONS
from app.db.models import AuditLog


@pytest.mark.asyncio
async def test_record_creates_db_entry(db):
    entry = await record(
        db, action="issue.assigned", owner="hiero", repo="sdk-js",
        target_number=42, target_login="alice", reason="Self-assigned"
    )
    await db.commit()
    result = await db.execute(select(AuditLog))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "issue.assigned"
    assert rows[0].target_login == "alice"
    assert rows[0].target_number == 42
    assert rows[0].actor == "hiero-bot"


@pytest.mark.asyncio
async def test_record_stores_metadata(db):
    await record(
        db, action="pr.reviewed", owner="hiero", repo="sdk",
        reason="AI review", metadata={"score": 85, "verdict": "approve"}
    )
    await db.commit()
    result = await db.execute(select(AuditLog))
    rows = result.scalars().all()
    assert rows[0].metadata_json["score"] == 85


@pytest.mark.asyncio
async def test_record_raises_on_invalid_action(db):
    with pytest.raises(ValueError, match="Unknown audit action"):
        await record(db, action="fake.action", owner="o", repo="r", reason="test")


@pytest.mark.asyncio
async def test_multiple_records_stored(db):
    for action in ["issue.assigned", "issue.closed", "pr.labeled"]:
        await record(db, action=action, owner="hiero", repo="sdk", reason="test")
    await db.commit()
    result = await db.execute(select(AuditLog))
    rows = result.scalars().all()
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_timestamp_set_automatically(db):
    from datetime import datetime
    before = datetime.utcnow()
    await record(db, action="issue.assigned", owner="o", repo="r", reason="t")
    await db.commit()
    result = await db.execute(select(AuditLog))
    row = result.scalars().first()
    assert row.timestamp >= before


def test_valid_actions_set_is_not_empty():
    assert len(VALID_ACTIONS) > 10
    assert "issue.assigned" in VALID_ACTIONS
    assert "pr.reviewed" in VALID_ACTIONS
    assert "contributor.welcomed" in VALID_ACTIONS
