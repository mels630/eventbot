from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, UTC
from typing import Any

import icalendar
from icalendar import Calendar, Event as ICalEvent, vDate, vDatetime, vRecur
from sqlalchemy import not_, select, exists
from sqlalchemy.ext.asyncio import async_sessionmaker

from .calendar_util import is_upcoming, parse_date_to_datetime
from .models import Event, Feedback, Recommendation, User

logger = logging.getLogger(__name__)


def _event_uid(event: Event) -> str:
    if event.recurrence_id:
        return f"{event.recurrence_id}@eventbot"
    return f"event-{event.id}@eventbot"


def _build_vevent(event: Event) -> ICalEvent:
    """Convert a database Event into an icalendar VEVENT."""
    vevent = ICalEvent()
    vevent.add("summary", event.title)
    vevent.add("uid", _event_uid(event))
    vevent.add("dtstamp", datetime.now(UTC))

    if event.description:
        vevent.add("description", event.description)
    if event.url:
        vevent.add("url", event.url)
    if event.venue:
        vevent.add("location", event.venue)
    if event.categories:
        for cat in event.categories.split(","):
            cat = cat.strip()
            if cat:
                vevent.add("categories", cat)

    # Fall back to the event_date string when no datetime is stored (e.g. some
    # agent-discovered events) so we never emit a wrong "now" timestamp.
    start_at = event.start_at
    date_only = event.is_all_day
    if start_at is None:
        start_at = parse_date_to_datetime(event.event_date, event.timezone or "America/Los_Angeles")
        date_only = True

    # Date / time handling
    if date_only:
        # All-day event
        if start_at:
            start_date = start_at.date()
            vevent.add("dtstart", start_date, parameters={"VALUE": "DATE"})
            if event.end_at:
                # iCalendar all-day end is exclusive
                end_date = event.end_at.date()
            else:
                # No explicit end: end at start + 1 day
                end_date = start_date + timedelta(days=1)
            vevent.add("dtend", end_date, parameters={"VALUE": "DATE"})
    else:
        # Timed event
        start_dt = event.start_at
        end_dt = event.end_at
        if start_dt is None:
            start_dt = datetime.now(UTC)
        if end_dt is None:
            end_dt = start_dt + timedelta(hours=1)

        if event.timezone:
            tzid = event.timezone
            # Ensure the datetime is aware
            if start_dt.tzinfo is None:
                import pytz
                start_dt = pytz.timezone(tzid).localize(start_dt)
            if end_dt.tzinfo is None:
                import pytz
                end_dt = pytz.timezone(tzid).localize(end_dt)

            vevent.add("dtstart", start_dt, parameters={"TZID": tzid})
            vevent.add("dtend", end_dt, parameters={"TZID": tzid})
        else:
            # UTC if no timezone known
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
            vevent.add("dtstart", start_dt)
            vevent.add("dtend", end_dt)

    # Recurrence rule
    if event.rrule:
        try:
            rrule = vRecur.from_ical(event.rrule)
            vevent.add("rrule", rrule)
        except Exception as exc:
            logger.warning("Could not parse RRULE for event %d: %s", event.id, exc)

    return vevent


def build_ical_feed(events: list[Event], calname: str = "eventbot personal feed") -> bytes:
    """Build an .ics file from a list of events."""
    cal = Calendar()
    cal.add("prodid", "-//eventbot//NONSGML v1.0//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", calname)
    cal.add("x-wr-timezone", "America/Los_Angeles")
    cal.add("refresh-interval", timedelta(hours=6), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", timedelta(hours=6))

    for event in events:
        try:
            vevent = _build_vevent(event)
            cal.add_component(vevent)
        except Exception as exc:
            logger.warning("Skipping event %d in .ics export: %s", event.id, exc)

    return cal.to_ical()


def _event_to_dict(event: Event, rec: Recommendation) -> dict[str, Any]:
    return {
        "id": event.id,
        "title": event.title,
        "venue": event.venue,
        "event_date": event.event_date,
        "start_at": event.start_at,
        "end_at": event.end_at,
        "is_all_day": event.is_all_day,
        "timezone": event.timezone,
        "url": event.url,
        "description": event.description,
        "is_recurring": event.is_recurring,
        "rrule": event.rrule,
        "source": event.source,
        "categories": event.categories,
        "score": rec.score,
        "relevance_notes": rec.relevance_notes,
    }


async def load_user_feed_events(
    session_factory: async_sessionmaker,
    slug: str,
    only_recurring: bool | None = None,
    min_score: float = 0.0,
    future_only: bool = True,
    days_back: int = 1,
    days_ahead: int = 60,
) -> list[dict[str, Any]]:
    """Load recommended events for a user, excluding disliked events.

    only_recurring:
      - True  -> only recurring events
      - False -> only one-off events
      - None  -> both
    """
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.slug == slug))
        if not user:
            return []

        # Subquery: events this user has thumbs-downed
        disliked = (
            select(Feedback.event_id)
            .where(Feedback.user_id == user.id, Feedback.rating == -1)
            .scalar_subquery()
        )

        query = (
            select(Event, Recommendation)
            .join(Recommendation, Recommendation.event_id == Event.id)
            .where(
                Recommendation.user_id == user.id,
                Recommendation.score >= min_score,
                Event.id.notin_(disliked),
            )
        )

        if only_recurring is True:
            query = query.where(Event.is_recurring.is_(True))
        elif only_recurring is False:
            query = query.where(Event.is_recurring.is_(False))

        query = query.order_by(Event.start_at)
        rows = await session.execute(query)

        # Filter to upcoming in Python so recurring series (whose stored start
        # may be in the past) and events with only a date string are handled
        # by their next occurrence rather than their first.
        now = datetime.now(UTC)
        results = []
        for event, rec in rows:
            if future_only and not is_upcoming(
                event, now, days_ahead=days_ahead, days_back=days_back
            ):
                continue
            results.append(_event_to_dict(event, rec))
        return results


async def generate_ical_for_user(
    session_factory: async_sessionmaker,
    slug: str,
    only_recurring: bool | None = None,
) -> bytes:
    """Generate the .ics feed for a user."""
    events_data = await load_user_feed_events(session_factory, slug, only_recurring=only_recurring)
    # Build database-like objects from dicts so we can reuse _build_vevent
    events: list[Event] = []
    for d in events_data:
        event = Event()
        for key, value in d.items():
            if key not in ("score", "relevance_notes"):
                setattr(event, key, value)
        events.append(event)
    if only_recurring is True:
        calname = "eventbot recurring feed"
    elif only_recurring is False:
        calname = "eventbot one-off feed"
    else:
        calname = "eventbot personal feed"
    return build_ical_feed(events, calname=calname)
