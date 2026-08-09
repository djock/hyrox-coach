"""The adaptation engine.

Each test below corresponds to a defect the three-way design review found in
v1, or to a row of the policy table in the plan document.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hyrox.engine import (
    BUFFER_CAP,
    MAX_RATE,
    MIN_RATE,
    BenchmarkCycle,
    PauseWindow,
    PriorOutcome,
    SessionRecord,
    WeekContext,
    evaluate_week,
    training_rate,
)
from hyrox.plan import load_plan

MONDAY = date(2026, 8, 3)


def record(day: date, *, counts_as: str = "strength", pain: int | None = None, location=None):
    return SessionRecord(
        slug=f"x-{day}",
        kind="completed",
        training_date=day,
        counts_as=counts_as,
        pain_score=pain,
        pain_location=location,
    )


def context(**overrides) -> WeekContext:
    plan = load_plan()
    base = dict(
        iso_week="2026-W32",
        week_start=MONDAY,
        week_end=MONDAY + timedelta(days=6),
        phase=plan.phase(1),
        events=(),
        history=(),
        prior_outcomes=(),
        pauses=(),
        benchmarks=(),
        buffer_weeks=4.0,
        sessions_remaining=100,
        baseline_race_date=date(2027, 8, 2),
        previous_projection=date(2027, 8, 2),
        evaluated_on=MONDAY + timedelta(days=7),
    )
    base.update(overrides)
    return WeekContext(**base)


def outcomes(*specs: tuple[int, bool]) -> tuple[PriorOutcome, ...]:
    return tuple(
        PriorOutcome(iso_week=f"2026-W{20 + i:02d}", floor_met=met, sessions_counted=n, status="x")
        for i, (n, met) in enumerate(specs)
    )


# ---------------------------------------------------------------- rate maths


def test_rate_bootstraps_before_any_history():
    """Week 1 has no trailing window and must not divide by zero."""
    assert training_rate((), MONDAY, planned=4.0) == 4.0


def test_rate_bootstraps_until_a_full_window_exists():
    history = (record(MONDAY),)
    assert training_rate(history, MONDAY + timedelta(days=6), planned=4.0) == 4.0


def test_rate_is_clamped_below_so_an_absence_cannot_explode_the_projection():
    """One session in four weeks would otherwise project three years out."""
    history = (record(MONDAY),)
    rate = training_rate(history, MONDAY + timedelta(weeks=5), planned=4.0)
    assert rate == MIN_RATE


def test_rate_is_clamped_above():
    days = [MONDAY + timedelta(days=i) for i in range(28)]
    history = tuple(record(d) for d in days)
    rate = training_rate(history, MONDAY + timedelta(days=27), planned=4.0)
    assert rate == MAX_RATE


def test_burst_logging_counts_distinct_days_not_sessions():
    """Ten sessions logged on one Sunday must not look like a heroic month."""
    reference = MONDAY + timedelta(days=27)
    anchor = record(MONDAY)  # so the window counts as full for both cases

    burst = (anchor,) + tuple(record(reference) for _ in range(10))
    spread = (anchor,) + tuple(record(MONDAY + timedelta(days=i)) for i in range(1, 12))

    # Eleven sessions either way. Two training days versus twelve.
    assert training_rate(burst, reference, 4.0) == MIN_RATE
    assert training_rate(spread, reference, 4.0) == 3.0


def test_floor_credit_is_capped_at_two_sessions_per_day():
    events = tuple(record(MONDAY) for _ in range(6))
    outcome = evaluate_week(context(events=events))
    assert outcome.sessions_counted == 2
    assert not outcome.floor_met


# ---------------------------------------------------------------- projection


def test_projection_never_moves_more_than_a_week_per_evaluation():
    """A date that swings by months and snaps back destroys trust in it."""
    ctx = context(sessions_remaining=200, buffer_weeks=0.0, events=())
    outcome = evaluate_week(ctx)
    assert abs((outcome.projected_race_date - ctx.previous_projection).days) <= 7


def test_projection_never_beats_the_baseline():
    ctx = context(sessions_remaining=1, buffer_weeks=4.0)
    outcome = evaluate_week(ctx)
    assert outcome.projected_race_date >= ctx.baseline_race_date


# ------------------------------------------------------------------- buffer


def test_missing_the_floor_spends_buffer():
    outcome = evaluate_week(context(events=(record(MONDAY),)))
    assert outcome.buffer_delta == -1.0
    assert outcome.buffer_after == 3.0


def test_buffer_never_goes_below_zero():
    outcome = evaluate_week(context(buffer_weeks=0.0, events=()))
    assert outcome.buffer_after == 0.0
    assert outcome.buffer_delta == 0.0


def test_buffer_regenerates_after_four_weeks_at_target():
    """v1 only ever depleted: one bad month removed the cushion permanently."""
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(4))
    ctx = context(
        buffer_weeks=1.0,
        events=events,
        prior_outcomes=outcomes((4, True), (4, True), (4, True)),
    )
    outcome = evaluate_week(ctx)
    assert outcome.buffer_delta == 1.0
    assert outcome.buffer_after == 2.0


def test_buffer_regeneration_needs_the_full_run_of_weeks():
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(4))
    ctx = context(buffer_weeks=1.0, events=events, prior_outcomes=outcomes((4, True), (2, False)))
    assert evaluate_week(ctx).buffer_delta == 0.0


def test_buffer_is_capped():
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(4))
    ctx = context(
        buffer_weeks=BUFFER_CAP,
        events=events,
        prior_outcomes=outcomes((4, True), (4, True), (4, True)),
    )
    assert evaluate_week(ctx).buffer_after == BUFFER_CAP


# -------------------------------------------------------------------- status


def test_hitting_the_floor_is_green():
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(3))
    assert evaluate_week(context(events=events)).status == "green"


def test_one_missed_week_is_amber_not_red():
    assert evaluate_week(context(events=(record(MONDAY),))).status == "amber"


def test_two_missed_weeks_in_a_row_is_red():
    ctx = context(events=(record(MONDAY),), prior_outcomes=outcomes((1, False)))
    assert evaluate_week(ctx).status == "red"


def test_red_clears_as_soon_as_the_floor_is_met_again():
    """No penalty box: both reviewers flagged v1 trapping a returning athlete."""
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(3))
    ctx = context(events=events, prior_outcomes=outcomes((1, False), (0, False)))
    assert evaluate_week(ctx).status in ("green", "restart")


def test_serious_pain_is_red_regardless_of_adherence():
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(4))
    events += (record(MONDAY + timedelta(days=5), pain=6, location="knee"),)
    assert evaluate_week(context(events=events)).status == "red"


# --------------------------------------------------------- pauses and re-entry


def test_a_pause_suspends_the_floor_and_spends_no_buffer():
    pause = PauseWindow("holiday", MONDAY, MONDAY + timedelta(days=6))
    outcome = evaluate_week(context(events=(), pauses=(pause,)))
    assert outcome.status == "paused"
    assert outcome.floor_required == 0
    assert outcome.buffer_delta == 0.0
    assert outcome.floor_met


def test_two_empty_weeks_trigger_re_entry_mode():
    ctx = context(events=(record(MONDAY),), prior_outcomes=outcomes((0, False), (0, False)))
    outcome = evaluate_week(ctx)
    assert outcome.status == "restart"
    assert outcome.floor_required == 1
    assert outcome.floor_met


def test_re_entry_does_not_apply_inside_a_pause():
    pause = PauseWindow("illness", MONDAY, None)
    ctx = context(events=(), pauses=(pause,), prior_outcomes=outcomes((0, False), (0, False)))
    assert evaluate_week(ctx).status == "paused"


# ---------------------------------------------------------------- benchmarks


def test_missing_benchmarks_are_never_a_failure():
    """A lazy athlete avoids testing; a design that stalls without it stalls forever."""
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(3))
    outcome = evaluate_week(context(events=events, benchmarks=()))
    assert outcome.detail["benchmark_verdict"] == "untested"
    assert outcome.status == "green"


def test_flat_benchmarks_are_amber():
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(3))
    cycles = (
        BenchmarkCycle(1, {"run_12min": 2000, "thrusters": 20}),
        BenchmarkCycle(2, {"run_12min": 1960, "thrusters": 18}),
    )
    assert evaluate_week(context(events=events, benchmarks=cycles)).status == "amber"


def test_a_big_run_decline_is_red():
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(3))
    cycles = (
        BenchmarkCycle(1, {"run_12min": 2000}),
        BenchmarkCycle(2, {"run_12min": 1700}),
    )
    assert evaluate_week(context(events=events, benchmarks=cycles)).status == "red"


# ------------------------------------------------------------------ purity


def test_evaluate_week_is_idempotent():
    events = tuple(record(MONDAY + timedelta(days=i)) for i in range(3))
    ctx = context(events=events)
    assert evaluate_week(ctx) == evaluate_week(ctx)
