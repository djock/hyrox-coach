"""The plan compiler, and the properties everything downstream depends on."""

from __future__ import annotations

import pytest

from hyrox.plan import BUFFER_WEEKS, TOTAL_WEEKS, compile_plan, load_plan


def test_compiles_a_full_year(plan):
    assert len(plan.sessions) == 240
    assert len(plan.phases) == 4
    assert sum(p.weeks for p in plan.phases) + BUFFER_WEEKS == TOTAL_WEEKS


def test_slugs_are_unique_and_stable(plan):
    slugs = [s.slug for s in plan.sessions]
    assert len(slugs) == len(set(slugs))
    assert plan.sessions[0].slug == "p1-w01-s1"
    # Recompiling the same YAML must produce the same identifiers and hash,
    # otherwise every restart would look like a plan revision.
    assert compile_plan().version == plan.version


def test_ordinals_are_dense_and_ordered(plan):
    assert [s.ordinal for s in plan.sessions] == list(range(len(plan.sessions)))


def test_every_alternative_resolves(plan):
    for session in plan.sessions:
        for alt in session.alternatives:
            assert plan.session(alt.slug) is not None
            assert alt.slug != session.slug


def test_runs_declare_a_bike_alternative(plan):
    """The impact rule needs an unambiguous target, per the design review."""
    runs = [s for s in plan.sessions if s.counts_as == "run_exposure"]
    assert runs
    for session in runs:
        assert any(a.reason == "impact" for a in session.alternatives), session.slug


def test_floor_sessions_exist_every_week(plan):
    for week in range(1, 49):
        weekly = [s for s in plan.sessions if s.global_week == week]
        assert len([s for s in weekly if s.in_floor]) >= 3, week


def test_benchmark_weeks_replace_a_session_not_add_one(plan):
    benchmark_weeks = {s.global_week for s in plan.sessions if s.kind == "benchmark"}
    assert benchmark_weeks == {w for w in range(1, 49) if w % 4 == 0}
    for week in benchmark_weeks:
        assert len([s for s in plan.sessions if s.global_week == week]) == 5


def test_benchmark_battery_switches_at_the_gym_transition(plan):
    home = next(s for s in plan.sessions if s.kind == "benchmark" and s.phase == 1)
    gym = next(s for s in plan.sessions if s.kind == "benchmark" and s.phase == 3)
    assert "Home battery" in home.title
    assert "Gym battery" in gym.title
    assert any("Goblet thrusters" == d["name"] for d in home.detail)
    assert any("Wall balls" == d["name"] for d in gym.detail)


def test_strength_alternates_in_later_phases(plan):
    phase3 = [s for s in plan.sessions if s.phase == 3 and s.kind == "strength"]
    variants = [s.variant for s in phase3[:4]]
    # Deterministic alternation: free choice means Strength A forty times.
    assert variants == ["A", "B", "A", "B"]


def test_by_week_progression_carries_forward(plan):
    """Sparse by_week entries stay in force until the next one."""
    w1 = plan.session("p1-w01-s2")
    w2 = plan.session("p1-w02-s2")
    w3 = plan.session("p1-w03-s2")
    assert w1.title == w2.title == "Ankle prep + 30 min brisk walk"
    assert w3.title != w1.title


def test_running_starts_with_two_weeks_of_walking(plan):
    """The impact protocol: tissue prep before any jogging."""
    for week in (1, 2):
        session = plan.session(f"p1-w{week:02d}-s2")
        assert "walk" in session.title.lower()
        assert "jog" not in session.title.lower()


def test_load_plan_is_cached():
    assert load_plan() is load_plan()


def test_validation_rejects_a_dangling_alternative(tmp_path, monkeypatch):
    import shutil

    from hyrox import plan as plan_module

    shutil.copytree(plan_module.PLAN_DIR, tmp_path / "plandata")
    target = tmp_path / "plandata" / "phase-1.yaml"
    target.write_text(target.read_text().replace("slot: 4\n      reason: impact", "slot: 9\n      reason: impact"))

    with pytest.raises(ValueError, match="unknown alternative"):
        compile_plan(tmp_path / "plandata")
