"""Tests for fuzzy dedup in the sources/ICS ingestion path (_upsert_event)."""
import pytest
from datetime import datetime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import pytz

from eventbot.models import Base, Event
from eventbot.sources import _upsert_event
from eventbot.calendar_util import parse_date_to_datetime

TZ = "America/Los_Angeles"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _data(title, venue, date, end=None, is_recurring=False, rrule="", url="http://x.com"):
    from slugify import slugify
    return {
        "title": title,
        "title_slug": slugify(title, max_length=80) or "untitled",
        "venue": venue,
        "event_date": date,
        "end_date": end,
        "url": url,
        "description": "",
        "start_at": parse_date_to_datetime(date, TZ),
        "end_at": parse_date_to_datetime(end or date, TZ),
        "is_all_day": True,
        "timezone": TZ,
        "is_recurring": is_recurring,
        "rrule": rrule,
        "recurrence_id": "",
        "source": "Test Feed",
        "source_url": "http://feed",
        "categories": "",
        "image_url": "",
    }


_WEEKLY_RRULE = "FREQ=WEEKLY;UNTIL=20261231T000000Z;INTERVAL=1;BYDAY=TU,TH"


def _first_weekday(year, month, weekday):
    """ISO date of the first given weekday (Mon=0) in a month."""
    from datetime import date
    d = date(year, month, 1)
    return date(year, month, 1 + (weekday - d.weekday()) % 7).isoformat()


async def _count(session):
    return await session.scalar(select(func.count()).select_from(Event))


async def test_per_day_listings_collapse_into_span(session_factory):
    async with session_factory() as session:
        for d in ("2026-10-03", "2026-10-04", "2026-10-05"):
            await _upsert_event(session, _data("Beautiful TRASH Exhibit", "Sebastopol Center", d))
        assert await _count(session) == 1
        ev = await session.scalar(select(Event))
        assert ev.event_date == "2026-10-03"
        assert ev.end_date == "2026-10-05"


async def test_cross_feed_variant_merges(session_factory):
    async with session_factory() as session:
        await _upsert_event(session, _data(
            "Robert Hass & Brenda Hillman at Copperfield's Books",
            "Copperfield's Books, 138 N Main St, Sebastopol, CA", "2026-10-02"))
        await _upsert_event(session, _data(
            "Robert Hass & Brenda Hillman – Poetry Reading & Signing",
            "Copperfield's Books, Sebastopol", "2026-10-02"))
        assert await _count(session) == 1


async def test_duplicate_recurring_masters_collapse(session_factory):
    # A feed emitting the same RRULE on several occurrence dates -> one series.
    async with session_factory() as session:
        for d in ("2026-09-03", "2026-09-08", "2026-10-06"):
            await _upsert_event(session, _data(
                "Transfer Talks", "Santa Rosa Junior College", d,
                is_recurring=True, rrule=_WEEKLY_RRULE))
        assert await _count(session) == 1


async def test_standalone_occurrence_folds_into_series(session_factory):
    from datetime import date, timedelta
    tue_d = date.fromisoformat(_first_weekday(2026, 10, 1))  # a Tuesday
    tue, thu = tue_d.isoformat(), (tue_d + timedelta(days=2)).isoformat()  # Thu same week
    async with session_factory() as session:
        await _upsert_event(session, _data(
            "Transfer Talks", "Santa Rosa Junior College", tue,
            is_recurring=True, rrule=_WEEKLY_RRULE))
        # A standalone occurrence on a date the series already covers is redundant.
        await _upsert_event(session, _data(
            "Transfer Talks", "Santa Rosa Junior College", thu))
        assert await _count(session) == 1


async def test_standalone_not_covered_creates_row(session_factory):
    from datetime import date, timedelta
    tue_d = date.fromisoformat(_first_weekday(2026, 10, 1))  # a Tuesday (series start)
    tue, wed = tue_d.isoformat(), (tue_d + timedelta(days=1)).isoformat()  # Wed, not TU/TH
    async with session_factory() as session:
        await _upsert_event(session, _data(
            "Transfer Talks", "Santa Rosa Junior College", tue,
            is_recurring=True, rrule=_WEEKLY_RRULE))
        await _upsert_event(session, _data(
            "Transfer Talks", "Santa Rosa Junior College", wed))
        assert await _count(session) == 2


async def test_distinct_same_day_events_stay_separate(session_factory):
    async with session_factory() as session:
        await _upsert_event(session, _data("Jazz Night", "Blue Moon", "2026-10-02"))
        await _upsert_event(session, _data("Comedy Open Mic", "Blue Moon", "2026-10-02"))
        assert await _count(session) == 2


async def test_language_variants_stay_separate(session_factory):
    async with session_factory() as session:
        await _upsert_event(session, _data(
            "Community Dinner and Discussion; presented in Spanish", "City Hall", "2026-11-04"))
        await _upsert_event(session, _data(
            "Community Dinner and Discussion; presented in English", "City Hall", "2026-11-04"))
        assert await _count(session) == 2


async def test_exact_refetch_updates_in_place(session_factory):
    async with session_factory() as session:
        await _upsert_event(session, _data("Art Fair", "Plaza", "2026-10-02", url="http://old.com"))
        await _upsert_event(session, _data("Art Fair", "Plaza", "2026-10-02", url="http://new.com"))
        assert await _count(session) == 1
        ev = await session.scalar(select(Event))
        assert ev.url == "http://new.com"
