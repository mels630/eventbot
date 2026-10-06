"""Shared DB-backed fuzzy event matching.

Both the Claude agent (``agent.persist_recommendations``) and the calendar/ICS
ingestion (``sources._upsert_event``) need to answer the same question: "is there
already an event in the DB that is really the same as this one?" Keeping that
logic in one place stops the two paths from drifting apart.

The pure similarity rules live in :mod:`eventbot.dedup`; this module adds the
date-windowed DB query and bounds parsing around them.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timedelta

from . import dedup
from .calendar_util import DEFAULT_TZ, parse_date_to_datetime
from .models import Event


def time_key(dt: datetime | None) -> str | None:
    """Local HH:MM of a start, or None for midnight / all-day (treated as a
    wildcard when deciding if two recurring series are the same)."""
    if dt is None:
        return None
    hm = dt.strftime("%H:%M")
    return None if hm == "00:00" else hm


def bounds_from_strings(
    event_date: str | None, end_date: str | None, tz: str | None = DEFAULT_TZ
) -> tuple[datetime | None, datetime | None]:
    """Parse an event's (start, end) ISO date strings into tz-aware datetimes.

    Deriving both sides from the stored strings keeps comparisons tz-aware and
    avoids mixing naive ``start_at`` columns (SQLite drops tzinfo) with aware ones.
    """
    tz = tz or DEFAULT_TZ
    start = parse_date_to_datetime(event_date, tz)
    end = parse_date_to_datetime(end_date, tz) if end_date else None
    return start, end


async def find_db_match(
    session: AsyncSession,
    title: str | None,
    venue: str | None,
    start: datetime | None,
    end: datetime | None,
    tz: str = DEFAULT_TZ,
    *,
    exclude_ids: frozenset[int] | set[int] = frozenset(),
    non_recurring_only: bool = False,
) -> Event | None:
    """Return an existing event that is the same as the described one, or None.

    Only events whose stored span overlaps ``[start, end]`` (padded by the dedup
    gap) are considered; ISO date strings compare chronologically so the window
    filter runs directly on the columns.
    """
    if start is None:
        return None

    gap = timedelta(days=dedup.DATE_GAP_DAYS)
    lo = (start - gap).date().isoformat()
    hi = ((end or start) + gap).date().isoformat()

    stmt = select(Event).where(
        Event.event_date <= hi,
        func.coalesce(Event.end_date, Event.event_date) >= lo,
    )
    if non_recurring_only:
        stmt = stmt.where(Event.is_recurring.is_(False))

    for ev in await session.scalars(stmt):
        if ev.id in exclude_ids:
            continue
        es, ee = bounds_from_strings(ev.event_date, ev.end_date, ev.timezone or tz)
        if dedup.is_same_event(title, venue, start, end, ev.title, ev.venue, es, ee):
            return ev
    return None
