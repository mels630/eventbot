"""Shared calendar helpers: date parsing, recurrence expansion, upcoming
filtering, human-friendly formatting, and Google Calendar "add event" links.

These are deliberately dependency-light (pytz + python-dateutil, both already
required) so they can be reused by the web UI, the .ics export, and tests.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta, UTC
from urllib.parse import urlencode

import pytz
from dateutil import rrule as _rrule

logger = logging.getLogger(__name__)

DEFAULT_TZ = "America/Los_Angeles"


def parse_date_to_datetime(date_str: str | None, tz_name: str = DEFAULT_TZ) -> datetime | None:
    """Parse an ISO date string (YYYY-MM-DD...) into a tz-aware midnight datetime."""
    if not date_str:
        return None
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except (ValueError, TypeError):
        return None
    try:
        tz = pytz.timezone(tz_name or DEFAULT_TZ)
    except Exception:
        tz = pytz.timezone(DEFAULT_TZ)
    return tz.localize(datetime(d.year, d.month, d.day))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def event_start(event) -> datetime | None:
    """Best available start datetime for an event, falling back to its date string.

    SQLite does not persist tzinfo, so `start_at` comes back naive — it holds
    the wall-clock time in the event's own `timezone` column. Localize it there
    (not UTC) so displayed times match the source, matching the .ics exporter.
    """
    start = getattr(event, "start_at", None)
    if start is not None:
        if start.tzinfo is None:
            tz_name = getattr(event, "timezone", None) or DEFAULT_TZ
            try:
                tz = pytz.timezone(tz_name)
            except Exception:
                tz = pytz.timezone(DEFAULT_TZ)
            return tz.localize(start)
        return start
    return parse_date_to_datetime(
        getattr(event, "event_date", None), getattr(event, "timezone", None) or DEFAULT_TZ
    )


def next_occurrence(event, reference: datetime) -> datetime | None:
    """Return the next start >= reference for an event.

    For one-off events this is simply the event start. For recurring events we
    expand the stored RRULE with python-dateutil and return the first
    occurrence at or after `reference`.
    """
    start = event_start(event)
    if start is None:
        return None

    if not getattr(event, "is_recurring", False) or not getattr(event, "rrule", None):
        return start

    try:
        rule = _rrule.rrulestr(event.rrule, dtstart=start)
        nxt = rule.after(_aware(reference), inc=True)
        return _aware(nxt) if nxt else None
    except Exception as exc:  # malformed RRULE — fall back to the raw start
        logger.debug("Could not expand RRULE for event %s: %s", getattr(event, "id", "?"), exc)
        return start


def is_upcoming(event, now: datetime, days_ahead: int = 60, days_back: int = 1) -> bool:
    """True if the event (or its next recurrence) falls within the window."""
    now = _aware(now)
    window_start = now - timedelta(days=days_back)
    window_end = now + timedelta(days=days_ahead)
    occ = next_occurrence(event, window_start)
    if occ is None:
        return False
    return window_start <= occ <= window_end


def expand_occurrences(
    event, range_start: datetime, range_end: datetime
) -> list[datetime]:
    """All start datetimes for an event within [range_start, range_end]."""
    start = event_start(event)
    if start is None:
        return []
    range_start = _aware(range_start)
    range_end = _aware(range_end)

    if getattr(event, "is_recurring", False) and getattr(event, "rrule", None):
        try:
            rule = _rrule.rrulestr(event.rrule, dtstart=start)
            return [_aware(dt) for dt in rule.between(range_start, range_end, inc=True)]
        except Exception as exc:
            logger.debug("RRULE expand failed for event %s: %s", getattr(event, "id", "?"), exc)

    return [start] if range_start <= start <= range_end else []


def has_specific_time(event) -> bool:
    """True if the event has a real clock time (not all-day / date-only)."""
    if getattr(event, "is_all_day", False):
        return False
    if getattr(event, "start_at", None) is None:
        return False  # only a date string was available
    start = event_start(event)
    if start is None:
        return False
    # A date-only source parses to midnight; treat that as "no specific time".
    return not (start.hour == 0 and start.minute == 0)


def format_when(event, tz_name: str | None = None) -> str:
    """Human-friendly date/time label, e.g. 'Fri, Sep 12 · 5:00 PM'."""
    start = event_start(event)
    if start is None:
        return getattr(event, "event_date", "") or ""
    tz_name = tz_name or getattr(event, "timezone", None) or DEFAULT_TZ
    try:
        start = start.astimezone(pytz.timezone(tz_name))
    except Exception:
        pass
    day = start.strftime("%a, %b %-d")
    if getattr(event, "is_all_day", False):
        return f"{day} (all day)"
    if not has_specific_time(event):
        return day
    return f"{day} · {start.strftime('%-I:%M %p')}"


def google_calendar_url(event, tz_name: str | None = None) -> str:
    """Build an 'Add to Google Calendar' template URL for a single event.

    Uses the public render?action=TEMPLATE endpoint — no OAuth or API key
    required. The user is taken to Google Calendar with the event pre-filled.
    """
    start = event_start(event)
    title = getattr(event, "title", "") or "Event"
    location = getattr(event, "venue", "") or ""
    details_parts = []
    if getattr(event, "description", None):
        details_parts.append(event.description)
    if getattr(event, "url", None):
        details_parts.append(event.url)
    details = "\n\n".join(details_parts)

    params: dict[str, str] = {"action": "TEMPLATE", "text": title}
    if location:
        params["location"] = location
    if details:
        params["details"] = details

    if start is not None:
        is_all_day = getattr(event, "is_all_day", False)
        if is_all_day:
            end = _aware(getattr(event, "end_at", None)) or (start + timedelta(days=1))
            params["dates"] = f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
        else:
            end = _aware(getattr(event, "end_at", None)) or (start + timedelta(hours=1))
            s_utc = start.astimezone(UTC)
            e_utc = end.astimezone(UTC)
            params["dates"] = (
                f"{s_utc.strftime('%Y%m%dT%H%M%SZ')}/{e_utc.strftime('%Y%m%dT%H%M%SZ')}"
            )

    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def occurrence_entries(
    events: Iterable,
    range_start: datetime,
    range_end: datetime,
    tz_name: str = DEFAULT_TZ,
) -> list[tuple[date, dict]]:
    """Expand events into per-occurrence display entries within a range.

    Returns (local_date, entry) pairs sorted by start time. All-day and
    date-only events localize to midnight, so they naturally sort first
    within a day. Shared by the month grid and the agenda view.
    """
    try:
        tz = pytz.timezone(tz_name or DEFAULT_TZ)
    except Exception:
        tz = pytz.timezone(DEFAULT_TZ)

    entries: list[tuple[date, dict]] = []
    for event in events:
        for occ in expand_occurrences(event, range_start, range_end):
            local = occ.astimezone(tz)
            entries.append((local.date(), {
                "id": getattr(event, "id", None),
                "title": event.title,
                "url": event.url,
                "venue": getattr(event, "venue", "") or "",
                "gcal_url": google_calendar_url(event),
                "is_all_day": getattr(event, "is_all_day", False),
                "time": local.strftime("%-I:%M %p") if has_specific_time(event) else "",
                "sort_key": local,
            }))
    entries.sort(key=lambda pair: pair[1]["sort_key"])
    return entries
