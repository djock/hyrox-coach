"""The adaptation engine.

`evaluate_week` is pure and idempotent: it takes an explicit `WeekContext`
assembled from durable state and returns a `WeekOutcome` that the caller
persists. v1 of the design had `evaluate_week(logs, plan)`, which both reviewers
independently rejected -- the result needs buffer state, prior outcomes, phase
state, benchmark history, pause windows and the clock, none of which were in the
signature. Rather than let it reach into a database, everything it needs is
named here.

The projection maths carries four guards, each closing a hole the review found:

* rate counts **distinct training days**, max 2 sessions credited per day, so
  batch-logging ten sessions on a Sunday cannot fake a heroic rate;
* rate is clamped to `[1.0, 5.0]`, so a three-week absence cannot divide by zero
  or project a race date three years out;
* with fewer than four weeks of history the planned rate is used instead;
* the projected date may move at most one week per evaluation, because a date
  that swings by months and snaps back destroys trust in the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from .plan import Phase, Plan

Status = Literal["green", "amber", "red", "restart", "paused"]

# Rate bounds. The floor of 1.0 is what stops division by zero and absurd
# projections; the ceiling stops a catch-up binge pulling the race date forward.
MIN_RATE = 1.0
MAX_RATE = 5.0
RATE_WINDOW_WEEKS = 4
MAX_SESSIONS_CREDITED_PER_DAY = 2
MAX_DATE_MOVE_WEEKS = 1.0

# How the plan actually advances: one plan week per calendar week in which the
# floor was met. `progress_rate` is the fraction of recent weeks that qualified,
# so 1.0 means the plan and the calendar are moving together and the race date
# holds. Floored at 0.25 so a long absence cannot project years into the future.
PROGRESS_WINDOW_WEEKS = 4
MIN_PROGRESS_RATE = 0.25
MAX_PROGRESS_RATE = 1.0

BUFFER_CAP = 4.0
WEEKS_AT_TARGET_FOR_BUFFER = 4

# An absence this long triggers re-entry mode: reduced floor, reduced loads, and
# a status that is neither green nor red. Both reviewers flagged that v1 trapped
# a returning athlete in a red penalty box while he was doing everything right.
REENTRY_ABSENCE_WEEKS = 2
REENTRY_FLOOR = 1

PAIN_STOP_THRESHOLD = 5
BENCHMARK_DECLINE_FRACTION = 0.10


@dataclass(frozen=True)
class SessionRecord:
    """One live (non-voided) session event, flattened for the engine."""

    slug: str
    kind: str
    training_date: date
    counts_as: str
    pain_score: int | None = None
    pain_location: str | None = None

    @property
    def counts_toward_floor(self) -> bool:
        return self.kind in ("completed", "swapped")


@dataclass(frozen=True)
class PauseWindow:
    kind: str
    start_date: date
    end_date: date | None

    def covers(self, day: date) -> bool:
        if day < self.start_date:
            return False
        return self.end_date is None or day <= self.end_date

    def overlaps(self, start: date, end: date) -> bool:
        if self.end_date is not None and self.end_date < start:
            return False
        return self.start_date <= end


@dataclass(frozen=True)
class BenchmarkCycle:
    cycle: int
    values: dict[str, float]


@dataclass(frozen=True)
class PriorOutcome:
    iso_week: str
    floor_met: bool
    sessions_counted: int
    status: str


@dataclass(frozen=True)
class WeekContext:
    """Everything `evaluate_week` needs, named explicitly."""

    iso_week: str
    week_start: date
    week_end: date
    phase: Phase
    events: tuple[SessionRecord, ...]
    history: tuple[SessionRecord, ...]          # live events before this week
    prior_outcomes: tuple[PriorOutcome, ...]    # oldest first
    pauses: tuple[PauseWindow, ...]
    benchmarks: tuple[BenchmarkCycle, ...]      # oldest first
    buffer_weeks: float
    plan_week: int
    total_plan_weeks: int
    baseline_race_date: date
    previous_projection: date
    evaluated_on: date


@dataclass(frozen=True)
class WeekOutcome:
    iso_week: str
    plan_week: int
    floor_required: int
    sessions_counted: int
    floor_met: bool
    status: Status
    buffer_delta: float
    buffer_after: float
    projected_race_date: date
    advance_plan_week: bool
    detail: dict[str, Any] = field(default_factory=dict)


def _credited_sessions(events: tuple[SessionRecord, ...]) -> int:
    """Sessions counted, at most two per calendar day.

    Without the cap, logging a backlog on Sunday satisfies a weekly floor
    without establishing any training habit -- Codex's "burst logging" defect.
    """
    per_day: dict[date, int] = {}
    for event in events:
        if not event.counts_toward_floor:
            continue
        per_day[event.training_date] = per_day.get(event.training_date, 0) + 1
    return sum(min(count, MAX_SESSIONS_CREDITED_PER_DAY) for count in per_day.values())


def training_rate(history: tuple[SessionRecord, ...], reference: date, planned: float) -> float:
    """Sessions per week over the trailing window, as distinct training days.

    Returns the planned rate until a full window of history exists -- week 1 has
    no trailing data and must not produce an infinite projection.
    """
    window_start = reference - timedelta(weeks=RATE_WINDOW_WEEKS) + timedelta(days=1)
    if not history:
        return planned

    # Inclusive span: earliest == reference is one day of history, not zero.
    earliest = min(event.training_date for event in history)
    if (reference - earliest).days + 1 < RATE_WINDOW_WEEKS * 7:
        return planned

    days = {
        event.training_date
        for event in history
        if event.counts_toward_floor and window_start <= event.training_date <= reference
    }
    rate = len(days) / RATE_WINDOW_WEEKS
    return min(max(rate, MIN_RATE), MAX_RATE)


def progress_rate(prior_outcomes: tuple[PriorOutcome, ...], floor_met_now: bool) -> float:
    """Plan weeks gained per calendar week, over the recent window.

    This -- not sessions completed -- is what actually moves the finish line: the
    plan advances a week when the floor is met and repeats it otherwise. Until a
    full window exists, assume he is keeping up, so a new athlete sees the real
    target date rather than a pessimistic guess.
    """
    recent = [o.floor_met for o in prior_outcomes[-(PROGRESS_WINDOW_WEEKS - 1) :]]
    recent.append(floor_met_now)
    if len(recent) < PROGRESS_WINDOW_WEEKS:
        return MAX_PROGRESS_RATE
    rate = sum(1 for met in recent if met) / len(recent)
    return min(max(rate, MIN_PROGRESS_RATE), MAX_PROGRESS_RATE)


def project_race_date(ctx: WeekContext, buffer_after: float, floor_met: bool) -> date:
    """Projected finish, rate-limited so the number stays trustworthy.

    Counted in plan *weeks* remaining, not sessions. The plan holds five sessions
    a week against a target of four, so a session-based projection implied more
    than 52 weeks of work even at perfect adherence and walked the date away from
    the athlete forever.
    """
    rate = progress_rate(ctx.prior_outcomes, floor_met)
    weeks_left = (ctx.total_plan_weeks - ctx.plan_week + 1) / rate
    raw = ctx.week_end + timedelta(days=round(weeks_left * 7))
    raw -= timedelta(days=round(buffer_after * 7))

    # Never earlier than the baseline: buffer protects the date, it cannot beat it.
    if raw < ctx.baseline_race_date:
        raw = ctx.baseline_race_date

    limit = timedelta(days=round(MAX_DATE_MOVE_WEEKS * 7))
    if raw > ctx.previous_projection + limit:
        return ctx.previous_projection + limit
    if raw < ctx.previous_projection - limit:
        return ctx.previous_projection - limit
    return raw


def _weeks_absent_before(ctx: WeekContext) -> int:
    """Consecutive fully-empty weeks immediately preceding this one."""
    count = 0
    for outcome in reversed(ctx.prior_outcomes):
        if outcome.sessions_counted == 0:
            count += 1
        else:
            break
    return count


def _in_pause(ctx: WeekContext) -> bool:
    return any(pause.overlaps(ctx.week_start, ctx.week_end) for pause in ctx.pauses)


def _benchmark_verdict(ctx: WeekContext) -> tuple[str, dict[str, Any]]:
    """Compare the two most recent cycles.

    A missing benchmark is never a failure. A lazy athlete avoids max-effort
    testing, and a design that stalls without it stalls forever -- so an
    untested cycle reports `untested` and the caller treats it as neutral.
    """
    if len(ctx.benchmarks) < 2:
        return "untested", {}

    previous, latest = ctx.benchmarks[-2], ctx.benchmarks[-1]
    improved = held = declined = 0
    run_decline = False

    for key, value in latest.values.items():
        if key not in previous.values:
            continue
        before = previous.values[key]
        if before == 0:
            continue
        change = (value - before) / before
        if change > 0.01:
            improved += 1
        elif change >= -0.01:
            held += 1
        else:
            declined += 1
        if key == "run_12min" and change <= -BENCHMARK_DECLINE_FRACTION:
            run_decline = True

    detail = {
        "improved": improved,
        "held": held,
        "declined": declined,
        "run_decline": run_decline,
    }
    if run_decline:
        return "run_decline", detail
    if improved + held >= 2:
        return "good", detail
    return "flat", detail


def evaluate_week(ctx: WeekContext) -> WeekOutcome:
    """Pure, idempotent. The caller persists the result exactly once."""
    paused = _in_pause(ctx)
    absent_weeks = _weeks_absent_before(ctx)
    restarting = not paused and absent_weeks >= REENTRY_ABSENCE_WEEKS

    floor_required = ctx.phase.floor
    if paused:
        floor_required = 0
    elif restarting:
        floor_required = REENTRY_FLOOR

    counted = _credited_sessions(ctx.events)
    floor_met = counted >= floor_required

    # Buffer. Pauses are intentional absence and never cost slack.
    buffer_delta = 0.0
    if not paused and not floor_met:
        buffer_delta = -1.0
    elif not paused and counted >= ctx.phase.target:
        recent = list(ctx.prior_outcomes[-(WEEKS_AT_TARGET_FOR_BUFFER - 1) :])
        at_target = all(o.sessions_counted >= ctx.phase.target for o in recent)
        if len(recent) == WEEKS_AT_TARGET_FOR_BUFFER - 1 and at_target:
            buffer_delta = 1.0

    buffer_after = min(max(ctx.buffer_weeks + buffer_delta, 0.0), BUFFER_CAP)
    # A delta that would push past a bound did not really happen.
    buffer_delta = buffer_after - ctx.buffer_weeks

    worst_pain = max(
        (e.pain_score for e in ctx.events if e.pain_score is not None), default=None
    )
    verdict, benchmark_detail = _benchmark_verdict(ctx)

    previous_missed = bool(ctx.prior_outcomes) and not ctx.prior_outcomes[-1].floor_met

    status = _status(
        paused=paused,
        restarting=restarting,
        floor_met=floor_met,
        previous_missed=previous_missed,
        worst_pain=worst_pain,
        verdict=verdict,
    )

    projected = project_race_date(ctx, buffer_after, floor_met)

    # A paused week is neither progress nor failure: the plan waits where it is.
    advance = floor_met and not paused

    return WeekOutcome(
        iso_week=ctx.iso_week,
        plan_week=ctx.plan_week,
        floor_required=floor_required,
        sessions_counted=counted,
        floor_met=floor_met,
        status=status,
        buffer_delta=buffer_delta,
        buffer_after=buffer_after,
        projected_race_date=projected,
        advance_plan_week=advance,
        detail={
            "paused": paused,
            "restarting": restarting,
            "absent_weeks_before": absent_weeks,
            "worst_pain": worst_pain,
            "benchmark_verdict": verdict,
            **benchmark_detail,
        },
    )


def _status(
    *,
    paused: bool,
    restarting: bool,
    floor_met: bool,
    previous_missed: bool,
    worst_pain: int | None,
    verdict: str,
) -> Status:
    """Status for one week.

    Computed from this week alone plus the immediately preceding week, which is
    what makes red clear the moment its cause clears rather than locking the
    athlete in a penalty box until a monthly cycle unlocks it.
    """
    if paused:
        return "paused"
    if worst_pain is not None and worst_pain >= PAIN_STOP_THRESHOLD:
        return "red"
    if restarting:
        return "restart"
    if not floor_met and previous_missed:
        return "red"
    if verdict == "run_decline":
        return "red"
    if not floor_met or verdict == "flat":
        return "amber"
    return "green"


def phase_exit_status(
    plan: Plan,
    phase: Phase,
    outcomes: tuple[PriorOutcome, ...],
    plan_week: int,
    manual_confirmed: bool,
) -> tuple[bool, list[dict[str, Any]]]:
    """Whether `phase` is complete, and a per-criterion breakdown for the UI.

    Phase entry never blocks on a missing benchmark -- only on criteria that are
    measurable from logs, plus explicit coach confirmation of the manual ones.
    """
    rows: list[dict[str, Any]] = []
    met_all = True

    phase_weeks = [o for o in outcomes if o.floor_met]

    for criterion in phase.exit_criteria:
        if criterion.key == "weeks_at_floor":
            have = len(phase_weeks)
            met = have >= (criterion.value or 0)
            rows.append(
                {"label": criterion.label, "met": met, "have": have, "need": criterion.value}
            )
        elif criterion.key == "plan_week":
            met = plan_week >= (criterion.value or 0)
            rows.append(
                {"label": criterion.label, "met": met, "have": plan_week, "need": criterion.value}
            )
        elif criterion.key == "manual":
            met = manual_confirmed
            rows.append({"label": criterion.label, "met": met, "have": None, "need": None})
        else:
            raise ValueError(f"unknown exit criterion {criterion.key!r}")
        met_all = met_all and met

    return met_all, rows
