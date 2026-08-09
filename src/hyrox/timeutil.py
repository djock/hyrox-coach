"""ISO-week time handling.

Every correctness hole the design review found in the projection maths traced
back to "a week" being undefined. One module owns it.

Two rules:

* All week logic is local time in a fixed zone, never UTC. A session finished at
  00:15 on Monday belongs to Sunday's week, because that is what the athlete
  means, and `training_date` (not `completed_at`) is what adherence counts.
* Weeks are ISO weeks, Monday-start, identified as `2026-W33` -- the same
  convention `milo_coach` already uses for everything.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import TIMEZONE


def now() -> datetime:
    return datetime.now(TIMEZONE)


def today() -> date:
    return now().date()


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(day: date) -> date:
    """The Monday of `day`'s ISO week."""
    return day - timedelta(days=day.isoweekday() - 1)


def week_end(day: date) -> date:
    return week_start(day) + timedelta(days=6)


def parse_iso_week(label: str) -> date:
    """Monday of the given `YYYY-Www` label."""
    year_part, week_part = label.split("-W")
    return date.fromisocalendar(int(year_part), int(week_part), 1)


def weeks_between(start: date, end: date) -> int:
    """Whole ISO weeks from `start`'s week to `end`'s week. Negative if before."""
    return (week_start(end) - week_start(start)).days // 7


def recent_weeks(reference: date, count: int) -> list[str]:
    """The `count` ISO weeks ending with (and including) `reference`'s week."""
    monday = week_start(reference)
    return [iso_week(monday - timedelta(weeks=offset)) for offset in reversed(range(count))]


def add_weeks(day: date, weeks: float) -> date:
    return day + timedelta(days=round(weeks * 7))
