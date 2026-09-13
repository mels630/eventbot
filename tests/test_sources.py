from datetime import datetime, UTC

import pytest
from icalendar import Calendar, Event as ICalEvent

from eventbot.sources import (
    Source,
    _coerce_datetime,
    _detect_recurrence,
    _event_features,
    _sanitize_ics,
    _score_text,
    _score_with_feedback,
)


def test_sanitize_ics_fixes_shorthand_duration():
    raw = b"REFRESH-INTERVAL;VALUE=DURATION:P15M\r\nDURATION:P30M\r\nBEGIN:VEVENT\r\nEND:VEVENT\r\n"
    cleaned = _sanitize_ics(raw)
    assert "PT15M" in cleaned
    assert "PT30M" in cleaned


def test_sanitize_ics_removes_blank_lines():
    raw = b"BEGIN:VEVENT\r\n \r\nSUMMARY:Test\r\nEND:VEVENT\r\n"
    cleaned = _sanitize_ics(raw)
    assert " \r\n" not in cleaned


def _make_ical_event(summary: str, rrule: dict | None = None) -> ICalEvent:
    event = ICalEvent()
    event.add("summary", summary)
    event.add("dtstart", datetime(2026, 8, 27, 10, 0, 0))
    if rrule:
        event.add("rrule", rrule)
    return event


def test_detect_recurrence_from_rrule():
    event = _make_ical_event("Weekly meet", rrule={"freq": "weekly"})
    assert _detect_recurrence(event, "Weekly meet", "", "auto") is True


def test_detect_recurrence_from_heuristic():
    event = _make_ical_event("Yoga every Wednesday")
    assert _detect_recurrence(event, "Yoga every Wednesday", "", "auto") is True


def test_detect_recurrence_hint_never():
    event = _make_ical_event("Yoga every Wednesday", rrule={"freq": "weekly"})
    assert _detect_recurrence(event, "Yoga every Wednesday", "", "never") is False


def test_score_text_interests_and_blocklist():
    score = _score_text(
        "Live jazz night at the winery", ["live music", "jazz", "winery"], ["religious"]
    )
    assert score > 0.5


def test_score_text_blocklist_penalty():
    score = _score_text("Sunday church service", ["music"], ["church"])
    assert score == 0.0


def test_event_features_extracts_words():
    from eventbot.models import Event

    event = Event(title="Jazz at the Barlow", categories="Music, Wine", venue="Barlow")
    features = _event_features(event)
    assert "jazz" in features
    assert "barlow" in features
    assert "music" in features


def test_score_with_feedback_direct_rating():
    from eventbot.models import Event

    event = Event(id=1, title="Jazz", categories="Music", venue="Barlow")
    score, notes = _score_with_feedback(
        event, ["music"], [], {1: -1}, [], []
    )
    assert score == 0.0
    assert "disliked" in notes


def test_score_with_feedback_liked():
    from eventbot.models import Event

    event = Event(id=2, title="Jazz", categories="Music", venue="Barlow")
    score, notes = _score_with_feedback(
        event, ["music"], [], {2: 1}, [], []
    )
    assert score == 1.0
    assert "liked" in notes


def test_coerce_datetime_ignore_feed_timezone():
    # Feeds that mislabel local times as UTC (e.g. DTSTART:...T103000Z for a
    # 10:30 AM storytime) should be reinterpreted in the source timezone.
    src = Source(name="Museum", url="https://x.test/feed.ics",
                 timezone="America/Los_Angeles", ignore_feed_timezone=True)
    dt, is_all_day, tz_name = _coerce_datetime(
        datetime(2026, 9, 24, 10, 30, tzinfo=UTC), None, src
    )
    assert is_all_day is False
    assert tz_name == "America/Los_Angeles"
    assert dt.hour == 10 and dt.minute == 30  # wall clock kept, tz re-labelled


def test_coerce_datetime_keeps_feed_timezone_by_default():
    src = Source(name="Venue", url="https://x.test/feed.ics",
                 timezone="America/Los_Angeles")
    dt, _, tz_name = _coerce_datetime(
        datetime(2026, 9, 13, 2, 0, tzinfo=UTC), None, src
    )
    assert tz_name == "UTC"
    assert dt.hour == 2  # genuine UTC times are preserved
