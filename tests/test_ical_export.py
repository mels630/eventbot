from datetime import datetime, UTC

import pytest
from icalendar import Calendar

from eventbot.ical_export import build_ical_feed
from eventbot.models import Event


def test_build_ical_feed_has_events():
    event = Event(
        id=1,
        title="Jazz Night",
        venue="Blue Moon",
        event_date="2026-08-27",
        start_at=datetime(2026, 8, 27, 19, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 27, 22, 0, tzinfo=UTC),
        url="https://example.com/jazz",
        description="A great evening",
        categories="Music,Live Music",
        is_recurring=False,
    )
    ics = build_ical_feed([event])
    cal = Calendar.from_ical(ics)
    vevents = [c for c in cal.walk() if c.name == "VEVENT"]
    assert len(vevents) == 1
    assert str(vevents[0]["summary"]) == "Jazz Night"


def test_build_ical_feed_all_day_event():
    from pytz import timezone

    tz = timezone("America/Los_Angeles")
    event = Event(
        id=2,
        title="Art Fair",
        venue="Town Plaza",
        event_date="2026-08-28",
        start_at=tz.localize(datetime(2026, 8, 28, 0, 0)),
        end_at=tz.localize(datetime(2026, 8, 29, 0, 0)),
        url="https://example.com/art",
        is_all_day=True,
    )
    ics = build_ical_feed([event])
    cal = Calendar.from_ical(ics)
    vevent = [c for c in cal.walk() if c.name == "VEVENT"][0]
    assert vevent["dtstart"].params.get("VALUE") == "DATE"


def test_build_ical_feed_recurring_has_rrule():
    event = Event(
        id=3,
        title="Yoga",
        venue="Studio",
        event_date="2026-08-27",
        start_at=datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        is_recurring=True,
        rrule="FREQ=WEEKLY;BYDAY=TH",
    )
    ics = build_ical_feed([event])
    cal = Calendar.from_ical(ics)
    vevent = [c for c in cal.walk() if c.name == "VEVENT"][0]
    assert "rrule" in vevent
    assert str(vevent["rrule"].to_ical(), "utf-8") == "FREQ=WEEKLY;BYDAY=TH"


def test_build_ical_feed_calname_variants():
    event = Event(id=4, title="Test", venue="X", event_date="2026-08-27")
    default = build_ical_feed([event])
    one_off = build_ical_feed([event], calname="eventbot one-off feed")
    recurring = build_ical_feed([event], calname="eventbot recurring feed")

    for ics, name in (
        (default, "eventbot personal feed"),
        (one_off, "eventbot one-off feed"),
        (recurring, "eventbot recurring feed"),
    ):
        cal = Calendar.from_ical(ics)
        assert str(cal["x-wr-calname"]) == name
