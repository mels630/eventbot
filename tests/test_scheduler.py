from datetime import datetime, timedelta, UTC
from zoneinfo import ZoneInfo

import pytest

from eventbot.prefs import Schedule, UserPrefs
from eventbot.scheduler import _cron_trigger


def _prefs(
    frequency: str = "weekly",
    timezone: str = "America/Los_Angeles",
    **schedule_kwargs,
) -> UserPrefs:
    schedule = Schedule(frequency=frequency, **schedule_kwargs)
    return UserPrefs(
        slug="tester",
        display_name="Tester",
        email="",
        location="Nowhere",
        timezone=timezone,
        schedule=schedule,
    )


# A Tuesday; weekly/thursday tests use this so the next fire is the coming Thursday.
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def test_weekly_uses_user_timezone():
    trigger = _cron_trigger(_prefs(day_of_week="thursday", hour=2))
    assert trigger.timezone == ZoneInfo("America/Los_Angeles")

    next_fire = trigger.get_next_fire_time(None, NOW)
    assert next_fire.strftime("%A") == "Thursday"
    assert (next_fire.hour, next_fire.minute) == (2, 0)
    # October is PDT (UTC-7): 02:00 local should land at 09:00 UTC, not 02:00 UTC.
    assert next_fire.utcoffset() == timedelta(hours=-7)
    assert next_fire.astimezone(UTC).hour == 9


def test_daily_fires_every_day_at_local_hour():
    trigger = _cron_trigger(_prefs(frequency="daily", hour=8))
    first = trigger.get_next_fire_time(None, NOW)
    second = trigger.get_next_fire_time(None, first + timedelta(seconds=1))

    assert first.utcoffset() == timedelta(hours=-7)
    assert first.hour == 8
    assert second - first == timedelta(days=1)


def test_monthly_fires_on_day_of_month():
    trigger = _cron_trigger(
        _prefs(frequency="monthly", day_of_month=15, hour=6)
    )
    next_fire = trigger.get_next_fire_time(None, NOW)
    assert next_fire.day == 15
    assert next_fire.hour == 6
    assert next_fire.utcoffset() == timedelta(hours=-7)


@pytest.mark.parametrize("bad_tz", ["Mars/Olympus_Mons", ""])
def test_invalid_timezone_falls_back_to_utc(bad_tz):
    trigger = _cron_trigger(
        _prefs(timezone=bad_tz, day_of_week="thursday", hour=2)
    )
    assert trigger.timezone == ZoneInfo("UTC")

    next_fire = trigger.get_next_fire_time(None, NOW)
    assert next_fire.utcoffset() == timedelta(0)
    assert next_fire.hour == 2


def test_timezone_dst_transition():
    # "Local hour" must follow the user's DST rules: a daily 08:00 job in
    # Los Angeles fires at 15:00 UTC in PDT but 16:00 UTC in PST.
    trigger = _cron_trigger(_prefs(frequency="daily", hour=8))
    pdt_fire = trigger.get_next_fire_time(None, datetime(2026, 10, 6, tzinfo=UTC))
    pst_fire = trigger.get_next_fire_time(None, datetime(2026, 11, 10, tzinfo=UTC))
    assert pdt_fire.astimezone(UTC).hour == 15  # PDT = UTC-7
    assert pst_fire.astimezone(UTC).hour == 16  # PST = UTC-8
