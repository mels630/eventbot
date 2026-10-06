"""Integration tests for persist_recommendations deduplication."""
import pytest
from datetime import datetime, UTC
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from eventbot.models import Base, Event, Recommendation, Run, User
from eventbot.agent import EventCandidate, persist_recommendations


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _setup(session):
    user = User(slug="alice", display_name="Alice")
    session.add(user)
    await session.flush()
    run = Run(user_id=user.id, is_household=False, started_at=datetime.now(UTC))
    session.add(run)
    await session.flush()
    return user, run


async def _count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


async def test_near_duplicates_from_different_sources_merge(session_factory):
    async with session_factory() as session:
        user, run = await _setup(session)
        candidates = [
            EventCandidate.from_dict({
                "title": "Jazz Night at Blue Moon",
                "venue": "Blue Moon",
                "event_date": "2026-08-01",
                "url": "http://a.com",
                "score": 0.8,
            }),
            EventCandidate.from_dict({
                "title": "Blue Moon Presents: Jazz Night — Tickets",
                "venue": "Blue Moon Tavern",
                "event_date": "2026-08-01",
                "url": "http://b.com",
                "description": "A longer, richer description of the jazz night.",
                "score": 0.9,
            }),
        ]
        await persist_recommendations(candidates, user, run, session)

        assert await _count(session, Event) == 1
        assert await _count(session, Recommendation) == 1
        rec = await session.scalar(select(Recommendation))
        assert rec.score == 0.9  # best score kept
        ev = await session.scalar(select(Event))
        assert "richer description" in (ev.description or "")


async def test_multiday_event_single_row_with_span(session_factory):
    async with session_factory() as session:
        user, run = await _setup(session)
        candidates = [
            EventCandidate.from_dict({
                "title": "Summer Art Festival",
                "venue": "City Park",
                "event_date": "2026-08-01",
                "end_date": "2026-08-05",
                "url": "http://fest.com",
                "score": 0.7,
            }),
        ]
        await persist_recommendations(candidates, user, run, session)
        ev = await session.scalar(select(Event))
        assert ev.event_date == "2026-08-01"
        assert ev.end_date == "2026-08-05"
        assert ev.end_at is not None


async def test_multiday_perday_listings_collapse(session_factory):
    """Agent (or sources) emitting the same festival on consecutive days
    should collapse into one row spanning the range."""
    async with session_factory() as session:
        user, run = await _setup(session)
        candidates = [
            EventCandidate.from_dict({
                "title": "Summer Art Festival",
                "venue": "City Park",
                "event_date": d,
                "url": "http://fest.com",
                "score": 0.7,
            })
            for d in ("2026-08-01", "2026-08-02", "2026-08-03")
        ]
        await persist_recommendations(candidates, user, run, session)
        assert await _count(session, Event) == 1
        ev = await session.scalar(select(Event))
        assert ev.event_date == "2026-08-01"
        assert ev.end_date == "2026-08-03"


async def test_different_events_stay_separate(session_factory):
    async with session_factory() as session:
        user, run = await _setup(session)
        candidates = [
            EventCandidate.from_dict({
                "title": "Jazz Night", "venue": "Blue Moon",
                "event_date": "2026-08-01", "url": "http://a.com", "score": 0.8,
            }),
            EventCandidate.from_dict({
                "title": "Comedy Open Mic", "venue": "Blue Moon",
                "event_date": "2026-08-01", "url": "http://b.com", "score": 0.6,
            }),
        ]
        await persist_recommendations(candidates, user, run, session)
        assert await _count(session, Event) == 2
        assert await _count(session, Recommendation) == 2


async def test_dedup_across_separate_runs(session_factory):
    async with session_factory() as session:
        user, run = await _setup(session)
        await persist_recommendations([
            EventCandidate.from_dict({
                "title": "Jazz Night at Blue Moon", "venue": "Blue Moon",
                "event_date": "2026-08-01", "url": "http://a.com", "score": 0.8,
            })
        ], user, run, session)

        run2 = Run(user_id=user.id, is_household=False, started_at=datetime.now(UTC))
        session.add(run2)
        await session.flush()
        await persist_recommendations([
            EventCandidate.from_dict({
                "title": "Blue Moon Presents Jazz Night", "venue": "Blue Moon Tavern",
                "event_date": "2026-08-01", "url": "http://b.com", "score": 0.5,
            })
        ], user, run2, session)

        assert await _count(session, Event) == 1
        assert await _count(session, Recommendation) == 1
