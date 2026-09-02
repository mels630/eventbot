from __future__ import annotations

import asyncio
import logging
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, UTC
from pathlib import Path
from typing import Any

import httpx
import yaml
from icalendar import Calendar
from pydantic import BaseModel, Field
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Event, Feedback, Recommendation, Run, User
from .settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_SOURCES_PATH = Path(__file__).parent / "default_sources.yaml"
SOURCES_FILE = "sources.yaml"


class Source(BaseModel):
    name: str
    type: str = Field(default="ics", pattern=r"^(ics|webcal|jsonld|scrape|meetup|eventbrite)$")
    url: str
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    timezone: str | None = None
    recurrence_hint: str = Field(default="auto", pattern=r"^(auto|always|never)$")


class SourcesConfig(BaseModel):
    version: str = "1.0"
    default_timezone: str = "America/Los_Angeles"
    window_days: int = 60
    sources: list[Source] = Field(default_factory=list)


def ensure_sources_file(data_dir: Path) -> Path:
    """Copy the bundled default sources.yaml into the data dir if the user has not created one."""
    target = data_dir / SOURCES_FILE
    if not target.exists() and DEFAULT_SOURCES_PATH.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_SOURCES_PATH.read_text())
        logger.info("Copied default %s to %s", SOURCES_FILE, target)
    return target


def load_sources(settings: Settings) -> SourcesConfig:
    """Load user sources.yaml, or fall back to the bundled default."""
    target = ensure_sources_file(settings.data_dir)
    if not target.exists():
        raise FileNotFoundError(f"No source config found at {target}")
    data = yaml.safe_load(target.read_text()) or {}
    return SourcesConfig.model_validate(data)


def _title_slug(title: str) -> str:
    return slugify(title, max_length=80) or "untitled"


def _event_date(dt: datetime | date) -> str:
    return dt.date().isoformat() if isinstance(dt, datetime) else dt.isoformat()


def _coerce_datetime(
    value: Any,
    default: datetime | date,
    source: Source,
) -> tuple[datetime, bool, str | None]:
    """Return a timezone-aware datetime, whether it was all-day, and the timezone name."""
    import pytz

    if value is None:
        value = default

    if isinstance(value, datetime):
        is_all_day = False
        if value.tzinfo is None:
            # Naive datetimes are interpreted in the source timezone
            tz_name = source.timezone or "America/Los_Angeles"
            tz = pytz.timezone(tz_name)
            value = tz.localize(value)
        tz_name = str(value.tzinfo) if value.tzinfo else None
        return value, is_all_day, tz_name

    # All-day (date)
    is_all_day = True
    tz_name = source.timezone or "America/Los_Angeles"
    tz = pytz.timezone(tz_name)
    dt = datetime(value.year, value.month, value.day, tzinfo=tz)
    return dt, is_all_day, tz_name


def _detect_recurrence(component: Any, title: str, description: str, hint: str) -> bool:
    if hint == "always":
        return True
    if hint == "never":
        return False

    # Direct RRULE in the .ics
    if component.get("rrule"):
        return True

    # Heuristic: common recurrence language in English event text
    text = f"{title} {description}".lower()
    recurrence_words = [
        r"\bevery\b",
        r"\bweekly\b",
        r"\beach week\b",
        r"\bevery week\b",
        r"\bmonthly\b",
        r"\beach month\b",
        r"\bdaily\b",
        r"\bmondays?\b",
        r"\btuesdays?\b",
        r"\bwednesdays?\b",
        r"\bthursdays?\b",
        r"\bfridays?\b",
        r"\bsaturdays?\b",
        r"\bsundays?\b",
        r"\brecurring\b",
        r"\breoccurring\b",
    ]
    return any(re.search(pattern, text) for pattern in recurrence_words)


def _to_str(value: Any) -> str:
    """Return a plain Python string from an icalendar property value.

    Use __str__() rather than to_ical() so iCalendar escape sequences
    (\\n -> newline, \\, -> comma, etc.) are decoded before we store them.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _extract_categories(component: Any) -> str:
    cats = component.get("categories")
    if cats is None:
        return ""
    if hasattr(cats, "cats"):
        return ", ".join(str(c) for c in cats.cats)
    return _to_str(cats)


def _extract_rrule(component: Any) -> str:
    rrule = component.get("rrule")
    if rrule is None:
        return ""
    if hasattr(rrule, "to_ical"):
        ical = rrule.to_ical()
        if isinstance(ical, bytes):
            return ical.decode("utf-8")
        return str(ical)
    return str(rrule)


def _extract_url(component: Any, source: Source) -> str:
    url = component.get("url")
    if url:
        return _to_str(url)
    # Some calendars put the link in the description
    desc = _to_str(component.get("description", ""))
    m = re.search(r"https?://[^\s\"<>]+", desc)
    if m:
        return m.group(0)
    return source.url


def _extract_venue(component: Any, source: Source) -> str:
    location = _to_str(component.get("location", ""))
    return location.strip() or source.name


def _dt_from_component(component: Any, key: str) -> datetime | date | None:
    prop = component.get(key)
    if prop is None:
        return None
    dt = getattr(prop, "dt", None)
    if dt is None:
        # Fallback: some properties return the wrapped value directly
        dt = prop
    return dt


def _event_from_component(component: Any, source: Source) -> dict[str, Any]:
    start = _dt_from_component(component, "dtstart")
    end = _dt_from_component(component, "dtend")
    if start is None:
        raise ValueError("Event has no DTSTART")

    title = _to_str(component.get("summary", "Untitled event")).strip() or "Untitled event"
    venue = _extract_venue(component, source)
    description = _to_str(component.get("description", ""))
    url = _extract_url(component, source)
    categories = _extract_categories(component)
    rrule = _extract_rrule(component)
    recurrence_id = _to_str(component.get("recurrence-id", ""))

    start_dt, is_all_day, tz_name = _coerce_datetime(start, start, source)
    end_dt, _, _ = _coerce_datetime(end, start_dt + timedelta(hours=1), source)

    # For all-day events, the .ics standard end date is exclusive; we keep that
    if is_all_day and end and isinstance(end, date) and not isinstance(end, datetime):
        # icalendar end date for all-day is exclusive; model end is exclusive too
        pass

    image = component.get("attach")
    image_url = ""
    if image:
        image_url = _to_str(image)
        if not image_url.startswith("http"):
            image_url = ""

    is_recurring = _detect_recurrence(component, title, description, source.recurrence_hint)

    return {
        "title": title,
        "title_slug": _title_slug(title),
        "venue": venue,
        "event_date": _event_date(start),
        "url": url,
        "description": description,
        "start_at": start_dt,
        "end_at": end_dt,
        "is_all_day": is_all_day,
        "timezone": tz_name,
        "is_recurring": is_recurring,
        "rrule": rrule,
        "recurrence_id": recurrence_id,
        "source": source.name,
        "source_url": source.url,
        "categories": categories,
        "image_url": image_url,
    }


def _sanitize_ics(content: bytes) -> str:
    """Clean up common non-standard .ics issues before parsing."""
    text = content.decode("utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned: list[str] = []
    for line in lines:
        # Drop blank or whitespace-only lines (invalid in iCalendar)
        if not line.strip():
            continue
        # Fix shorthand durations like P15M -> PT15M for any property value
        # (DURATION, TRIGGER, REFRESH-INTERVAL, X-PUBLISHED-TTL, ...)
        line = re.sub(r"(?<=:)(P)(\d+[HMS])", r"PT\2", line)
        cleaned.append(line)
    return "\r\n".join(cleaned)


async def _fetch_ics(client: httpx.AsyncClient, source: Source) -> str:
    url = source.url
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]

    logger.info("Fetching source %s from %s", source.name, url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/calendar, text/plain, */*",
    }
    response = await client.get(url, headers=headers, timeout=60, follow_redirects=True)
    response.raise_for_status()
    return _sanitize_ics(response.content)


async def _upsert_event(session: AsyncSession, event_data: dict[str, Any]) -> Event:
    existing = await session.scalar(
        select(Event).where(
            Event.venue == event_data["venue"],
            Event.event_date == event_data["event_date"],
            Event.title_slug == event_data["title_slug"],
        )
    )

    if existing:
        # Update mutable fields from the latest fetch
        for key in ("url", "description", "start_at", "end_at", "is_all_day",
                    "timezone", "is_recurring", "rrule", "recurrence_id",
                    "source", "source_url", "categories", "image_url"):
            if event_data.get(key) is not None:
                setattr(existing, key, event_data[key])
        return existing

    event = Event(**event_data)
    session.add(event)
    await session.flush()
    return event


async def fetch_source(
    client: httpx.AsyncClient,
    source: Source,
    window_start: datetime,
    window_end: datetime,
    session: AsyncSession,
) -> list[Event]:
    """Fetch and parse a single source, returning events inside the window."""
    if not source.enabled:
        return []

    if source.type == "ics" or source.type == "webcal" or source.type == "meetup":
        content = await _fetch_ics(client, source)
        cal = Calendar.from_ical(content)
        events: list[Event] = []

        for component in cal.walk("VEVENT"):
            try:
                event_data = _event_from_component(component, source)
            except Exception as exc:
                logger.warning("Skipping malformed event from %s: %s", source.name, exc)
                continue

            start = event_data["start_at"]
            # Include events whose start falls in the window
            if start < window_start or start > window_end:
                continue

            event = await _upsert_event(session, event_data)
            events.append(event)

        logger.info("Source %s contributed %d events", source.name, len(events))
        return events

    # TODO: support jsonld, scrape, eventbrite
    logger.warning("Unsupported source type for %s: %s", source.name, source.type)
    return []


async def update_source_events(session: AsyncSession, settings: Settings) -> list[Event]:
    """Fetch all enabled sources and upsert events into the global events table."""
    config = load_sources(settings)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = today - timedelta(days=1)
    window_end = today + timedelta(days=config.window_days)

    all_events: list[Event] = []
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        for source in config.sources:
            try:
                events = await fetch_source(client, source, window_start, window_end, session)
                all_events.extend(events)
            except Exception as exc:
                logger.exception("Failed to fetch source %s: %s", source.name, exc)

    await session.flush()
    logger.info("Total source events in window: %d", len(all_events))
    return all_events


def _score_text(text: str, interests: list[str], blocklist: list[str]) -> float:
    """Simple keyword-based score from 0.0 to 1.0."""
    text = text.lower()
    score = 0.25

    # Blocklist is a hard penalty
    for block in blocklist:
        block = block.strip().lower()
        if block and re.search(rf"\b{re.escape(block)}\b", text):
            score -= 0.6

    # Interests add positive signals
    interest_hits = 0
    for interest in interests:
        interest = interest.strip().lower()
        if not interest:
            continue
        # Try phrase match first, then single-word match
        if interest in text:
            interest_hits += 1
            score += 0.12
            if len(interest.split()) > 1:
                score += 0.08  # bonus for multi-word phrase match
        elif any(re.search(rf"\b{re.escape(w)}\b", text) for w in interest.split()):
            interest_hits += 1
            score += 0.05

    if interest_hits >= 3:
        score += 0.1
    if interest_hits == 0 and interests:
        score -= 0.1

    return max(0.0, min(1.0, score))


def _event_features(event: Event) -> set[str]:
    """Extract a set of meaningful lowercase words from an event for similarity checks."""
    text = f"{event.title or ''} {event.categories or ''} {event.venue or ''}"
    return set(re.findall(r"\b[a-z]{4,}\b", text.lower()))


def _score_with_feedback(
    event: Event,
    interests: list[str],
    blocklist: list[str],
    feedback_map: dict[int, int],
    liked_features: list[set[str]],
    disliked_features: list[set[str]],
) -> tuple[float, str]:
    """Score an event using declared interests and past thumbs-up/down feedback."""
    # Direct feedback overrides everything
    if event.id in feedback_map:
        rating = feedback_map[event.id]
        if rating == -1:
            return 0.0, "You disliked this event"
        if rating == 1:
            return 1.0, "You liked this event"

    text = f"{event.title} {event.description or ''} {event.categories or ''}"
    score = _score_text(text, interests, blocklist)

    # Adjust based on similar liked/disliked events
    event_features = _event_features(event)
    for liked in liked_features:
        overlap = len(event_features & liked)
        if overlap:
            score += 0.05 * min(overlap, 3)
            score = min(1.0, score)
    for disliked in disliked_features:
        overlap = len(event_features & disliked)
        if overlap:
            score -= 0.12 * min(overlap, 3)
            score = max(0.0, score)

    notes = f"Source: {event.source}"
    if score <= 0.0:
        notes = f"{notes}; blocked by preferences"
    return score, notes


async def recommend_source_events_for_user(
    user: User,
    run: Run,
    session: AsyncSession,
    settings: Settings,
) -> list[Recommendation]:
    """Create/update recommendations for all source events using feedback and preferences."""
    from .prefs import load_all_prefs

    all_prefs = load_all_prefs(settings.preferences_dir)
    prefs = all_prefs.get(user.slug)
    if prefs is None:
        logger.warning("No preferences for user %s; skipping source recommendations", user.slug)
        return []

    interests = [i.lower() for i in prefs.interests]
    blocklist = [b.lower() for b in prefs.blocklist]

    # Load all source events with any existing recommendation for this user
    rows = await session.execute(
        select(Event, Recommendation)
        .outerjoin(
            Recommendation,
            (Recommendation.event_id == Event.id) & (Recommendation.user_id == user.id),
        )
        .where(Event.source.isnot(None))
    )

    # Load this user's feedback history
    feedback_rows = await session.execute(
        select(Feedback, Event)
        .join(Event, Feedback.event_id == Event.id)
        .where(Feedback.user_id == user.id)
    )
    feedback_map: dict[int, int] = {}
    liked_features: list[set[str]] = []
    disliked_features: list[set[str]] = []
    for fb, ev in feedback_rows:
        feedback_map[fb.event_id] = fb.rating
        features = _event_features(ev)
        if fb.rating == 1:
            liked_features.append(features)
        elif fb.rating == -1:
            disliked_features.append(features)

    recommendations: list[Recommendation] = []
    for event, existing_rec in rows:
        score, notes = _score_with_feedback(
            event, interests, blocklist, feedback_map, liked_features, disliked_features
        )

        if score <= 0.0 and existing_rec:
            # Remove a recommendation the user has disliked or that is now blocked
            await session.delete(existing_rec)
            continue

        if existing_rec:
            existing_rec.run_id = run.id
            existing_rec.score = score
            existing_rec.relevance_notes = notes
            recommendations.append(existing_rec)
        elif score > 0.0:
            rec = Recommendation(
                event_id=event.id,
                user_id=user.id,
                run_id=run.id,
                score=score,
                relevance_notes=notes,
                is_household=False,
            )
            session.add(rec)
            recommendations.append(rec)

    await session.flush()
    logger.info("Updated %d source recommendations for %s", len(recommendations), user.slug)
    return recommendations
