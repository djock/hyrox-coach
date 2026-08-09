"""The plan compiler.

The 12-month plan is authored as four phase files of weekly *slots* and expands
here into ~240 concrete sessions. Authoring 240 sessions by hand would be
unmaintainable; authoring 20 slots and a progression table is not.

Two properties matter downstream and are the reason this module exists at all:

* **Slugs are immutable.** `p1-w03-s02` identifies a session for the life of the
  plan. Logs reference slugs, never row ids or ordinals, so inserting a session
  in month 4 cannot silently repoint March's easy run at a deadlift.
* **The plan has a content hash.** Any change to the YAML produces a new
  revision, which is what lets the seeder retire superseded templates instead of
  mutating rows that history points at.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PLAN_DIR = Path(__file__).parent / "plandata"

# Weeks 49-52 exist so the buffer has somewhere to live: the plan is 48 weeks of
# training and 4 weeks of slack before the baseline race date.
BUFFER_WEEKS = 4
TOTAL_WEEKS = 52


@dataclass(frozen=True)
class Alternative:
    """A declared substitute for a session.

    Alternatives are *authored*, never selected from the queue. That is what
    makes it structurally impossible to swap a hard session for an easy one
    from three months ahead and defer the hard work to month 12.
    """

    slug: str
    reason: str


@dataclass(frozen=True)
class PlanSession:
    slug: str
    ordinal: int
    phase: int
    week_in_phase: int
    global_week: int
    slot: int
    kind: str
    title: str
    counts_as: str
    in_floor: bool
    cap_minutes: int
    variant: str | None
    detail: tuple[dict[str, Any], ...]
    alternatives: tuple[Alternative, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "ordinal": self.ordinal,
            "phase": self.phase,
            "week_in_phase": self.week_in_phase,
            "global_week": self.global_week,
            "slot": self.slot,
            "kind": self.kind,
            "title": self.title,
            "counts_as": self.counts_as,
            "in_floor": self.in_floor,
            "cap_minutes": self.cap_minutes,
            "variant": self.variant,
            "detail": [dict(d) for d in self.detail],
            "alternatives": [{"slug": a.slug, "reason": a.reason} for a in self.alternatives],
        }


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    value: int | None = None


@dataclass(frozen=True)
class Phase:
    number: int
    name: str
    purpose: str
    months: str
    location: str
    weeks: int
    floor: int
    target: int
    cap_minutes: int
    first_week: int
    entry_criteria: tuple[Criterion, ...] = ()
    exit_criteria: tuple[Criterion, ...] = ()


@dataclass(frozen=True)
class Plan:
    version: str
    sessions: tuple[PlanSession, ...]
    phases: tuple[Phase, ...]
    benchmarks: dict[str, Any]
    by_slug: dict[str, PlanSession] = field(default_factory=dict, compare=False)

    def session(self, slug: str) -> PlanSession | None:
        return self.by_slug.get(slug)

    def phase(self, number: int) -> Phase:
        return next(p for p in self.phases if p.number == number)

    def after(self, slug: str | None) -> tuple[PlanSession, ...]:
        """Sessions from `slug` onward, inclusive. Empty once the plan is done."""
        if slug is None:
            return self.sessions
        session = self.by_slug.get(slug)
        if session is None:
            return ()
        return self.sessions[session.ordinal :]

    def battery_for_phase(self, phase: int) -> list[dict[str, Any]]:
        key = "home" if phase <= 2 else "gym"
        return list(self.benchmarks[key]["tests"])


def _resolve_by_week(spec: dict[str, Any], week: int) -> dict[str, Any]:
    """Pick the `by_week` entry in force for `week`.

    Entries are sparse -- week 1, 3, 5 ... -- and each stays in force until the
    next one starts, so the author writes changes rather than repetition.
    """
    by_week: dict[int, Any] = spec["by_week"]
    applicable = [w for w in sorted(by_week) if w <= week]
    if not applicable:
        raise ValueError(f"by_week has no entry at or before week {week}: {sorted(by_week)}")
    return by_week[applicable[-1]]


def _slug(phase: int, week: int, slot: int) -> str:
    return f"p{phase}-w{week:02d}-s{slot}"


def _benchmark_detail(battery: dict[str, Any], always: list[dict[str, Any]]) -> list[dict[str, Any]]:
    detail = [
        {"name": test["name"], "prescription": test["protocol"], "note": f"Record: {test['unit']}"}
        for test in battery["tests"]
    ]
    detail += [{"name": item["name"], "prescription": item["protocol"]} for item in always]
    return detail


def _compile_phase(
    raw: dict[str, Any],
    first_week: int,
    start_ordinal: int,
    benchmarks: dict[str, Any],
) -> tuple[Phase, list[PlanSession]]:
    number = raw["phase"]
    phase = Phase(
        number=number,
        name=raw["name"],
        purpose=raw["purpose"],
        months=raw["months"],
        location=raw["location"],
        weeks=raw["weeks"],
        floor=raw["floor"],
        target=raw["target"],
        cap_minutes=raw["cap_minutes"],
        first_week=first_week,
        entry_criteria=tuple(
            Criterion(c["key"], c["label"], c.get("value")) for c in raw.get("entry_criteria", [])
        ),
        exit_criteria=tuple(
            Criterion(c["key"], c["label"], c.get("value")) for c in raw.get("exit_criteria", [])
        ),
    )

    battery = benchmarks["home" if number <= 2 else "gym"]
    always = benchmarks["always"]
    sessions: list[PlanSession] = []
    ordinal = start_ordinal

    for week in range(1, phase.weeks + 1):
        global_week = first_week + week - 1
        is_benchmark_week = global_week % 4 == 0

        for spec in raw["slots"]:
            slot = spec["slot"]

            # The benchmark battery replaces the last optional slot rather than
            # adding a sixth session -- a test week should not be a bigger week.
            if is_benchmark_week and slot == 5:
                sessions.append(
                    PlanSession(
                        slug=_slug(number, week, slot),
                        ordinal=ordinal,
                        phase=number,
                        week_in_phase=week,
                        global_week=global_week,
                        slot=slot,
                        kind="benchmark",
                        title=f"Monthly benchmark - {battery['label']}",
                        counts_as="benchmark",
                        in_floor=False,
                        cap_minutes=30,
                        variant=None,
                        detail=tuple(_benchmark_detail(battery, always)),
                        alternatives=(),
                    )
                )
                ordinal += 1
                continue

            variant = spec.get("variant")
            if spec.get("variant_alternates"):
                variant = "A" if week % 2 == 1 else "B"

            if "by_week" in spec:
                resolved = _resolve_by_week(spec, week)
                title = resolved["title"]
                detail = resolved["detail"]
            elif "detail_by_variant" in spec:
                title = spec["title"].format(variant=variant)
                detail = spec["detail_by_variant"][variant]
            else:
                title = spec["title"].format(variant=variant) if variant else spec["title"]
                detail = spec["detail"]

            alternatives: list[Alternative] = []
            alt = spec.get("alternative")
            if alt:
                alternatives.append(
                    Alternative(slug=_slug(number, week, alt["slot"]), reason=alt["reason"])
                )

            sessions.append(
                PlanSession(
                    slug=_slug(number, week, slot),
                    ordinal=ordinal,
                    phase=number,
                    week_in_phase=week,
                    global_week=global_week,
                    slot=slot,
                    kind=spec["kind"],
                    title=title,
                    counts_as=spec["counts_as"],
                    in_floor=spec.get("in_floor", False),
                    cap_minutes=spec.get("cap_minutes", phase.cap_minutes),
                    variant=variant,
                    detail=tuple(detail),
                    alternatives=tuple(alternatives),
                )
            )
            ordinal += 1

    return phase, sessions


def compile_plan(plan_dir: Path = PLAN_DIR) -> Plan:
    benchmarks = yaml.safe_load((plan_dir / "benchmarks.yaml").read_text())

    phases: list[Phase] = []
    sessions: list[PlanSession] = []
    first_week = 1

    for path in sorted(plan_dir.glob("phase-*.yaml")):
        raw = yaml.safe_load(path.read_text())
        phase, phase_sessions = _compile_phase(raw, first_week, len(sessions), benchmarks)
        phases.append(phase)
        sessions.extend(phase_sessions)
        first_week += phase.weeks

    _validate(sessions, phases)

    payload = json.dumps([s.as_dict() for s in sessions], sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(payload.encode()).hexdigest()[:16]

    return Plan(
        version=version,
        sessions=tuple(sessions),
        phases=tuple(phases),
        benchmarks=benchmarks,
        by_slug={s.slug: s for s in sessions},
    )


def _validate(sessions: list[PlanSession], phases: list[Phase]) -> None:
    slugs = {s.slug for s in sessions}
    if len(slugs) != len(sessions):
        raise ValueError("duplicate slugs in compiled plan")

    for session in sessions:
        for alt in session.alternatives:
            if alt.slug not in slugs:
                raise ValueError(f"{session.slug} declares unknown alternative {alt.slug}")
            if alt.slug == session.slug:
                raise ValueError(f"{session.slug} declares itself as an alternative")

    training_weeks = sum(p.weeks for p in phases)
    if training_weeks + BUFFER_WEEKS != TOTAL_WEEKS:
        raise ValueError(
            f"phases cover {training_weeks} weeks; with {BUFFER_WEEKS} buffer weeks "
            f"that is not the expected {TOTAL_WEEKS}"
        )


@lru_cache(maxsize=1)
def load_plan() -> Plan:
    return compile_plan()
