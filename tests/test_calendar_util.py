from datetime import datetime, timedelta, UTC
from urllib.parse import parse_qs, urlparse

import pytz

from eventbot.calendar_util import (
    expand_occurrences,
    format_when,
    google_calendar_url,
    has_specific_time,
    is_upcoming,
    next_occurrence,
    parse_date_to_datetime,
)
from eventbot.models import Event


def _ev(**kw):
    return Event(**kw)


def test_parse_date_to_datetime():
    dt = parse_date_to_datetime("2026-09-12", "America/Los_Angeles")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 9 and dt.day == 12
    assert dt.tzinfo is not None


def test_parse_date_to_datetime_bad_input():
    assert parse_date_to_datetime("") is None
    assert parse_date_to_datetime(None) is None
    assert parse_date_to_datetime("not-a-date") is None


def test_is_upcoming_excludes_past():
    past = _ev(title="Old", event_date="2020-01-01",
               start_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert is_upcoming(past, datetime.now(UTC)) is False


def test_is_upcoming_includes_future():
    soon = datetime.now(UTC) + timedelta(days=5)
    ev = _ev(title="Soon", event_date=soon.date().isoformat(), start_at=soon)
    assert is_upcoming(ev, datetime.now(UTC)) is True


def test_is_upcoming_recurring_past_start_still_upcoming():
    # Weekly event that started a year ago but still recurs
    start = datetime.now(UTC) - timedelta(days=365)
    ev = _ev(title="Weekly Yoga", event_date=start.date().isoformat(),
             start_at=start, is_recurring=True, rrule="FREQ=WEEKLY")
    assert is_upcoming(ev, datetime.now(UTC)) is True


def test_is_upcoming_uses_date_string_when_no_start():
    soon = datetime.now(UTC) + timedelta(days=3)
    ev = _ev(title="No start", event_date=soon.date().isoformat(), start_at=None)
    assert is_upcoming(ev, datetime.now(UTC)) is True


def test_next_occurrence_one_off():
    start = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
    ev = _ev(title="Show", event_date="2026-09-12", start_at=start)
    assert next_occurrence(ev, datetime(2026, 1, 1, tzinfo=UTC)) == start


def test_expand_occurrences_recurring_in_month():
    start = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
    ev = _ev(title="Weekly", event_date="2026-01-01", start_at=start,
             is_recurring=True, rrule="FREQ=WEEKLY;BYDAY=TH")
    occ = expand_occurrences(
        ev, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    )
    # September 2026 has 4 Thursdays
    assert len(occ) == 4


def test_google_calendar_url_timed():
    start = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
    end = datetime(2026, 9, 12, 19, 0, tzinfo=UTC)
    ev = _ev(title="Jazz Night", venue="Blue Moon", event_date="2026-09-12",
             start_at=start, end_at=end, is_all_day=False, url="https://x.com")
    url = google_calendar_url(ev)
    q = parse_qs(urlparse(url).query)
    assert q["action"] == ["TEMPLATE"]
    assert q["text"] == ["Jazz Night"]
    assert q["dates"] == ["20260912T170000Z/20260912T190000Z"]
    assert q["location"] == ["Blue Moon"]


def test_google_calendar_url_all_day():
    tz = pytz.timezone("America/Los_Angeles")
    start = tz.localize(datetime(2026, 9, 12))
    ev = _ev(title="Art Fair", venue="Plaza", event_date="2026-09-12",
             start_at=start, is_all_day=True)
    url = google_calendar_url(ev)
    q = parse_qs(urlparse(url).query)
    assert q["dates"] == ["20260912/20260913"]


def test_naive_start_localized_to_event_timezone():
    # SQLite returns naive datetimes; they must be read in the event's tz, not UTC
    ev = _ev(title="Farmers Market", event_date="2026-09-12",
             start_at=datetime(2026, 9, 12, 11, 0), timezone="America/Los_Angeles",
             is_all_day=False)
    when = format_when(ev)
    assert "11:00 AM" in when
    url = google_calendar_url(ev)
    q = parse_qs(urlparse(url).query)
    # 11:00 PT == 18:00 UTC
    assert q["dates"] == ["20260912T180000Z/20260912T190000Z"]


def test_format_when_all_day():
    tz = pytz.timezone("America/Los_Angeles")
    ev = _ev(title="Fair", event_date="2026-09-12",
             start_at=tz.localize(datetime(2026, 9, 12)), is_all_day=True)
    assert "all day" in format_when(ev)


def test_date_only_event_has_no_time():
    # start_at None -> only a date string; should not show a clock time
    ev = _ev(title="Festival", event_date="2026-09-12", start_at=None)
    assert has_specific_time(ev) is False
    when = format_when(ev)
    assert "AM" not in when and "PM" not in when


def test_midnight_event_treated_as_date_only():
    ev = _ev(title="Fair", event_date="2026-09-12",
             start_at=datetime(2026, 9, 12, 0, 0), timezone="America/Los_Angeles")
    assert has_specific_time(ev) is False
