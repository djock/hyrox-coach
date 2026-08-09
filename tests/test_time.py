"""ISO weeks, midnight and DST.

The review's finding: with no defined week boundary, a session finished at
00:15 on Monday lands in the wrong week and burns a week of buffer that the
athlete never actually missed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo

from hyrox.config import TIMEZONE
from hyrox.timeutil import (
    add_weeks,
    iso_week,
    parse_iso_week,
    recent_weeks,
    week_end,
    week_start,
    weeks_between,
)

BUCHAREST = ZoneInfo("Europe/Bucharest")


def test_weeks_are_monday_start():
    monday = date(2026, 8, 3)
    assert week_start(monday) == monday
    assert week_start(date(2026, 8, 9)) == monday  # Sunday belongs to it
    assert week_end(monday) == date(2026, 8, 9)


def test_iso_week_labels_round_trip():
    monday = date(2026, 8, 3)
    assert parse_iso_week(iso_week(monday)) == monday


def test_a_late_sunday_session_stays_in_sunday_s_week():
    """23:30 Sunday and 00:15 Monday are different weeks -- deliberately."""
    sunday_night = datetime(2026, 8, 9, 23, 30, tzinfo=BUCHAREST)
    monday_morning = datetime(2026, 8, 10, 0, 15, tzinfo=BUCHAREST)
    assert iso_week(sunday_night.date()) == "2026-W32"
    assert iso_week(monday_morning.date()) == "2026-W33"


def test_utc_and_local_disagree_at_the_boundary():
    """Why the app keeps a local training_date instead of using completed_at."""
    local = datetime(2026, 8, 10, 0, 30, tzinfo=BUCHAREST)  # Monday, locally
    as_utc = local.astimezone(ZoneInfo("UTC"))
    assert as_utc.date() == date(2026, 8, 9)  # still Sunday in UTC
    assert iso_week(local.date()) != iso_week(as_utc.date())


def test_week_arithmetic_survives_the_autumn_dst_change():
    """Clocks go back on 25 October 2026; a week is still seven dates."""
    before = date(2026, 10, 19)
    after = week_start(before + timedelta(days=7))
    assert after == date(2026, 10, 26)
    assert weeks_between(before, after) == 1


def test_week_arithmetic_survives_the_spring_dst_change():
    before = date(2027, 3, 22)
    after = week_start(before + timedelta(days=7))
    assert after == date(2027, 3, 29)
    assert weeks_between(before, after) == 1


def test_year_boundary_uses_iso_rules():
    # 1 January 2027 is a Friday and belongs to ISO week 53 of 2026.
    assert iso_week(date(2027, 1, 1)) == "2026-W53"
    assert iso_week(date(2027, 1, 4)) == "2027-W01"


def test_recent_weeks_is_ordered_and_inclusive():
    weeks = recent_weeks(date(2026, 8, 12), 4)
    assert len(weeks) == 4
    assert weeks[-1] == "2026-W33"
    assert weeks == sorted(weeks)


def test_add_weeks_lands_on_the_same_weekday():
    start = date(2026, 8, 3)
    assert add_weeks(start, 52).isoweekday() == start.isoweekday()


def test_the_configured_zone_is_fixed_not_the_host_s():
    """The Pi and the laptop must agree about which week a session is in."""
    assert TIMEZONE.key == "Europe/Bucharest"
