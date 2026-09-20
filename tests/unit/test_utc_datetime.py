# tests/unit/test_utc_datetime.py - timezone-safe datetime column type

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import UTCDateTime, WebhookDelivery

IST = timezone(timedelta(hours=5, minutes=30))


def test_bind_converts_aware_datetime_to_naive_utc():
    aware = datetime(2026, 9, 20, 5, 30, tzinfo=IST)

    stored = UTCDateTime().process_bind_param(aware, None)

    assert stored == datetime(2026, 9, 20, 0, 0)  # noqa: DTZ001
    assert stored.tzinfo is None


def test_bind_leaves_naive_datetime_and_none_untouched():
    naive = datetime(2026, 9, 20, 0, 0)  # noqa: DTZ001

    assert UTCDateTime().process_bind_param(naive, None) == naive
    assert UTCDateTime().process_bind_param(None, None) is None


def test_result_attaches_utc_to_stored_naive_value():
    stored = datetime(2026, 9, 20, 0, 0)  # noqa: DTZ001

    loaded = UTCDateTime().process_result_value(stored, None)

    assert loaded == datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert UTCDateTime().process_result_value(None, None) is None


async def test_aware_datetime_round_trips_through_the_database(db):
    when = datetime(2026, 9, 20, 5, 30, tzinfo=IST)
    db.add(WebhookDelivery(id="tz-roundtrip", received_at=when))
    await db.commit()

    loaded = (
        await db.execute(
            select(WebhookDelivery.received_at).where(
                WebhookDelivery.id == "tz-roundtrip"
            )
        )
    ).scalar_one()

    assert loaded == when
    assert loaded.tzinfo is not None
