"""Tests for the pre-existing duplicate backfill."""
import pytest
from datetime import datetime, timedelta, UTC
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from eventbot.models import Base, Event, Feedback, Recommendation, Run, User
from eventbot.backfill_dedup import backfill


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


async def _mk_event(session, title, venue, date, end=None, url="", desc=None,
                    is_recurring=False, rrule="", start_at=None):
    ev = Event(
        title=title, title_slug=title.lower().replace(" ", "-"),
        venue=venue, event_date=date, end_date=end, url=url, description=desc,
        is_recurring=is_recurring, rrule=rrule, timezone="America/Los_Angeles",
        start_at=start_at,
    )
    session.add(ev)
    await session.flush()
    return ev


_WEEKLY_RRULE = "FREQ=WEEKLY;UNTIL=20261231T000000Z;INTERVAL=1;BYDAY=TU,TH"


def _first_weekday(year, month, weekday):
    from datetime import date
    d = date(year, month, 1)
    return date(year, month, 1 + (weekday - d.weekday()) % 7).isoformat()


async def test_dry_run_does_not_mutate(session_factory):
    async with session_factory() as session:
        await _mk_event(session, "Jazz Night at Blue Moon", "Blue Moon", "2026-08-01")
        await _mk_event(session, "Blue Moon Presents Jazz Night Tickets", "Blue Moon Tavern", "2026-08-01")
        await session.commit()

        summary = await backfill(session, apply=False)
        assert summary["clusters_with_duplicates"] == 1
        assert summary["events_removed"] == 1
        # Nothing deleted in dry run.
        assert await _count(session, Event) == 2


async def test_apply_merges_events_and_reassigns(session_factory):
    async with session_factory() as session:
        user = User(slug="alice", display_name="Alice")
        session.add(user)
        await session.flush()
        run = Run(user_id=user.id, started_at=datetime.now(UTC))
        session.add(run)
        await session.flush()

        e1 = await _mk_event(session, "Jazz Night at Blue Moon", "Blue Moon", "2026-08-01", url="")
        e2 = await _mk_event(
            session, "Blue Moon Presents Jazz Night", "Blue Moon Tavern",
            "2026-08-01", url="http://rich.com", desc="Rich description here.",
        )
        # A recommendation on the duplicate (not on canonical) -> should repoint.
        session.add(Recommendation(event_id=e2.id, user_id=user.id, run_id=run.id, score=0.9))
        # Feedback on the canonical.
        session.add(Feedback(event_id=e1.id, user_id=user.id, rating=1))
        await session.commit()

        summary = await backfill(session, apply=True)
        assert summary["events_removed"] == 1

    async with session_factory() as session:
        assert await _count(session, Event) == 1
        assert await _count(session, Recommendation) == 1
        ev = await session.scalar(select(Event))
        # Canonical is the lower id (e1) but inherits richer url/description.
        assert ev.url == "http://rich.com"
        assert ev.description == "Rich description here."
        rec = await session.scalar(select(Recommendation))
        assert rec.event_id == ev.id


async def test_apply_merges_conflicting_recommendations(session_factory):
    """Both canonical and duplicate have a rec for the same user -> keep best."""
    async with session_factory() as session:
        user = User(slug="bob", display_name="Bob")
        session.add(user)
        await session.flush()
        run = Run(user_id=user.id, started_at=datetime.now(UTC))
        session.add(run)
        await session.flush()

        e1 = await _mk_event(session, "Jazz Night at Blue Moon", "Blue Moon", "2026-08-01")
        e2 = await _mk_event(session, "Blue Moon Jazz Night", "Blue Moon", "2026-08-01")
        session.add(Recommendation(event_id=e1.id, user_id=user.id, run_id=run.id, score=0.5))
        session.add(Recommendation(event_id=e2.id, user_id=user.id, run_id=run.id, score=0.95))
        await session.commit()

        await backfill(session, apply=True)

    async with session_factory() as session:
        assert await _count(session, Recommendation) == 1
        rec = await session.scalar(select(Recommendation))
        assert rec.score == 0.95  # best kept


async def test_multiday_chain_merges_into_span(session_factory):
    async with session_factory() as session:
        for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
            await _mk_event(session, "Summer Art Festival", "City Park", d)
        await session.commit()

        await backfill(session, apply=True)

    async with session_factory() as session:
        assert await _count(session, Event) == 1
        ev = await session.scalar(select(Event))
        assert ev.event_date == "2026-08-01"
        assert ev.end_date == "2026-08-03"


async def test_merge_does_not_trip_unique_constraint(session_factory):
    """Canonical (lower id) is the LATER date; merging the earlier dup widens
    the canonical onto the dup's date — must not collide on the unique
    (venue, event_date, title_slug) identity."""
    async with session_factory() as session:
        # Same title_slug + venue, canonical has the later date.
        canonical = await _mk_event(session, "Open Studios", "Art Hall", "2026-10-09")
        await _mk_event(session, "Open Studios", "Art Hall", "2026-10-08")
        await session.commit()

        summary = await backfill(session, apply=True)
        assert summary["events_removed"] == 1

    async with session_factory() as session:
        assert await _count(session, Event) == 1
        ev = await session.scalar(select(Event))
        assert ev.event_date == "2026-10-08"
        assert ev.end_date == "2026-10-09"


async def test_backfill_collapses_recurring_and_singletons(session_factory):
    """Mirror of the real 'Transfer Talks' mess: duplicate recurring masters plus
    standalone occurrences the series covers."""
    tue = _first_weekday(2026, 10, 1)
    thu = _first_weekday(2026, 10, 3)
    async with session_factory() as session:
        # Three recurring masters with the same rrule (should collapse to one).
        await _mk_event(session, "Transfer Talks", "SRJC", "2026-09-03",
                        is_recurring=True, rrule=_WEEKLY_RRULE)
        await _mk_event(session, "Transfer Talks", "SRJC", "2026-09-08",
                        is_recurring=True, rrule=_WEEKLY_RRULE)
        await _mk_event(session, "Transfer Talks", "SRJC", tue,
                        is_recurring=True, rrule=_WEEKLY_RRULE)
        # Two standalone occurrences the series already covers.
        await _mk_event(session, "Transfer Talks", "SRJC", thu)
        await session.commit()

        summary = await backfill(session, apply=True)
        assert summary["recurring_masters_collapsed"] == 2
        assert summary["covered_singletons_removed"] == 1

    async with session_factory() as session:
        assert await _count(session, Event) == 1
        ev = await session.scalar(select(Event))
        assert ev.is_recurring is True


async def test_recurring_with_distinct_times_not_collapsed(session_factory):
    """Same title/venue/rrule but genuinely different session times (9:30am vs
    5:30pm) are different sessions and must stay separate."""
    from datetime import datetime
    async with session_factory() as session:
        # Two duplicate masters of the morning session + one evening session.
        await _mk_event(session, "SUP Yoga", "River", "2026-10-03",
                        is_recurring=True, rrule=_WEEKLY_RRULE,
                        start_at=datetime(2026, 10, 3, 9, 30))
        await _mk_event(session, "SUP Yoga", "River", "2026-10-10",
                        is_recurring=True, rrule=_WEEKLY_RRULE,
                        start_at=datetime(2026, 10, 10, 9, 30))
        await _mk_event(session, "SUP Yoga", "River", "2026-10-05",
                        is_recurring=True, rrule=_WEEKLY_RRULE,
                        start_at=datetime(2026, 10, 5, 17, 30))
        await session.commit()

        summary = await backfill(session, apply=True)
        # The two 9:30 masters collapse; the 5:30pm session stays separate.
        assert summary["recurring_masters_collapsed"] == 1

    async with session_factory() as session:
        assert await _count(session, Event) == 2


async def test_distinct_events_untouched(session_factory):
    async with session_factory() as session:
        await _mk_event(session, "Jazz Night", "Blue Moon", "2026-08-01")
        await _mk_event(session, "Comedy Open Mic", "Blue Moon", "2026-08-01")
        await session.commit()

        summary = await backfill(session, apply=True)
        assert summary["events_removed"] == 0

    async with session_factory() as session:
        assert await _count(session, Event) == 2
