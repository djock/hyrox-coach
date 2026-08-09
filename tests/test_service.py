"""Queue, swaps, the impact rule, benchmarks, phases and re-seeding."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hyrox import db, service
from hyrox.plan import compile_plan

from conftest import PLAN_START


def log_next(conn, plan, day, **kwargs):
    up_next = service.current_session(conn, plan)
    return service.log_session(
        conn, slug=up_next.session.slug, actor="dragos", training_date=day, plan=plan, **kwargs
    )


# ------------------------------------------------------------------- queue


def test_pointer_walks_the_plan(conn, plan):
    assert service.current_session(conn, plan).session.slug == "p1-w01-s1"
    log_next(conn, plan, PLAN_START)
    assert service.current_session(conn, plan).session.slug == "p1-w01-s2"


def test_replay_of_the_same_idempotency_key_does_not_double_log(conn, plan):
    first = service.log_session(
        conn, slug="p1-w01-s1", actor="dragos", idempotency_key="k1", plan=plan
    )
    second = service.log_session(
        conn, slug="p1-w01-s1", actor="dragos", idempotency_key="k1", plan=plan
    )
    assert first == second
    assert service.sessions_completed(conn) == 1


def test_voiding_rewinds_the_pointer(conn, plan):
    event_id = log_next(conn, plan, PLAN_START)
    log_next(conn, plan, PLAN_START + timedelta(days=1))
    assert service.current_session(conn, plan).session.slug == "p1-w01-s3"

    service.void_event(conn, event_id=event_id, actor="dragos", plan=plan)
    assert service.current_session(conn, plan).session.slug == "p1-w01-s1"
    assert service.sessions_completed(conn) == 1


def test_voiding_keeps_the_row_for_audit(conn, plan):
    event_id = log_next(conn, plan, PLAN_START)
    service.void_event(conn, event_id=event_id, actor="dragos", plan=plan)
    row = conn.execute("SELECT * FROM session_events WHERE id = ?", (event_id,)).fetchone()
    assert row is not None
    assert row["voided_at"] and row["voided_by"] == "dragos"


def test_skipping_does_not_advance_the_queue(conn, plan):
    service.skip_session(conn, slug="p1-w01-s1", actor="dragos", reason="tired", plan=plan)
    # Nothing is lost: the session waits. The skip exists so the coach sees a
    # reason rather than silence.
    assert service.current_session(conn, plan).session.slug == "p1-w01-s1"
    assert service.sessions_completed(conn) == 0


def test_events_freeze_a_snapshot_of_what_was_prescribed(conn, plan):
    event_id = log_next(conn, plan, PLAN_START)
    row = conn.execute("SELECT snapshot_json FROM session_events WHERE id = ?", (event_id,)).fetchone()
    assert "Goblet squat to box" in row["snapshot_json"]


# ------------------------------------------------------------------- swaps


def test_a_declared_alternative_is_accepted(conn, plan):
    replacement = service.resolve_swap(conn, slug="p1-w01-s2", alternative_slug="p1-w01-s4", plan=plan)
    assert replacement.kind == "bike"


def test_an_undeclared_alternative_is_rejected(conn, plan):
    """No cherry-picking: he cannot pull an easy session forward from month 12."""
    with pytest.raises(service.ServiceError, match="declared alternative"):
        service.resolve_swap(conn, slug="p1-w01-s2", alternative_slug="p4-w12-s5", plan=plan)


def test_only_one_voluntary_swap_per_week(conn, plan):
    service.log_session(
        conn,
        slug="p1-w01-s4",
        actor="dragos",
        training_date=PLAN_START,
        substituted_from="p1-w01-s2",
        substitution_reason="voluntary",
        plan=plan,
    )
    with pytest.raises(service.ServiceError, match="already used"):
        service.resolve_swap(conn, slug="p1-w02-s2", alternative_slug="p1-w02-s4", plan=plan)


def test_the_swap_allowance_resets_next_week(conn, plan):
    service.log_session(
        conn,
        slug="p1-w01-s4",
        actor="dragos",
        training_date=PLAN_START,
        substituted_from="p1-w01-s2",
        substitution_reason="voluntary",
        plan=plan,
    )
    assert service.swap_allowance_used(conn, PLAN_START + timedelta(days=7)) == 0


def test_a_swap_advances_past_the_session_it_replaced(conn, plan):
    log_next(conn, plan, PLAN_START)  # clear s1
    service.log_session(
        conn,
        slug="p1-w01-s4",
        actor="dragos",
        training_date=PLAN_START,
        substituted_from="p1-w01-s2",
        substitution_reason="voluntary",
        plan=plan,
    )
    assert service.current_session(conn, plan).session.slug == "p1-w01-s3"


# ------------------------------------------------------------- impact rule


def _log_painful_run(conn, plan, slug, day, score=4, location="ankle"):
    service.log_session(
        conn,
        slug=slug,
        actor="dragos",
        training_date=day,
        pain_score=score,
        pain_location=location,
        plan=plan,
    )


def test_one_painful_run_is_not_enough(conn, plan):
    _log_painful_run(conn, plan, "p1-w01-s2", PLAN_START)
    assert not service.impact_substitution_active(conn, PLAN_START)


def test_two_painful_runs_inside_the_window_trigger_a_substitution(conn, plan):
    _log_painful_run(conn, plan, "p1-w01-s2", PLAN_START)
    _log_painful_run(conn, plan, "p1-w02-s2", PLAN_START + timedelta(days=7))
    assert service.impact_substitution_active(conn, PLAN_START + timedelta(days=7))


def test_painful_runs_outside_the_window_do_not_trigger(conn, plan):
    """v1 said 'two consecutive logs', which could mean sessions weeks apart."""
    _log_painful_run(conn, plan, "p1-w01-s2", PLAN_START)
    _log_painful_run(conn, plan, "p1-w04-s2", PLAN_START + timedelta(days=21))
    assert not service.impact_substitution_active(conn, PLAN_START + timedelta(days=21))


def test_different_locations_do_not_trigger(conn, plan):
    _log_painful_run(conn, plan, "p1-w01-s2", PLAN_START, location="ankle")
    _log_painful_run(conn, plan, "p1-w02-s2", PLAN_START + timedelta(days=7), location="knee")
    assert not service.impact_substitution_active(conn, PLAN_START + timedelta(days=7))


def test_pain_below_the_threshold_does_not_trigger(conn, plan):
    _log_painful_run(conn, plan, "p1-w01-s2", PLAN_START, score=2)
    _log_painful_run(conn, plan, "p1-w02-s2", PLAN_START + timedelta(days=7), score=2)
    assert not service.impact_substitution_active(conn, PLAN_START + timedelta(days=7))


def test_non_running_pain_does_not_trigger_the_run_rule(conn, plan):
    """The rule keys on run exposures, not on any two sessions that hurt."""
    _log_painful_run(conn, plan, "p1-w01-s1", PLAN_START)
    _log_painful_run(conn, plan, "p1-w01-s3", PLAN_START + timedelta(days=2))
    assert not service.impact_substitution_active(conn, PLAN_START + timedelta(days=2))


def test_the_substitution_shows_up_in_the_queue(conn, plan):
    _log_painful_run(conn, plan, "p1-w03-s2", PLAN_START)
    _log_painful_run(conn, plan, "p1-w04-s2", PLAN_START + timedelta(days=3))
    conn.execute("UPDATE progress SET pointer_slug = 'p1-w05-s2'")

    up_next = service.current_session(conn, plan)
    assert up_next.is_substitution
    assert up_next.session.kind == "bike"
    assert up_next.substituted_for.slug == "p1-w05-s2"


# ---------------------------------------------------------------- pain stop


def test_severe_pain_halts_that_kind_of_training(conn, plan):
    service.log_session(
        conn, slug="p1-w01-s2", actor="dragos", pain_score=7, pain_location="knee", plan=plan
    )
    stop = service.active_pain_stop(conn)
    assert stop is not None and stop["counts_as"] == "run_exposure"

    with pytest.raises(service.ServiceError, match="paused until"):
        service.log_session(conn, slug="p1-w02-s2", actor="dragos", plan=plan)


def test_other_training_continues_during_a_pain_stop(conn, plan):
    service.log_session(
        conn, slug="p1-w01-s2", actor="dragos", pain_score=7, pain_location="knee", plan=plan
    )
    # Strength is a different modality and is not blocked.
    assert service.log_session(conn, slug="p1-w01-s1", actor="dragos", plan=plan) > 0


def test_coach_acknowledgement_clears_the_stop(conn, plan):
    event_id = service.log_session(
        conn, slug="p1-w01-s2", actor="dragos", pain_score=7, pain_location="knee", plan=plan
    )
    service.acknowledge_pain_stop(conn, event_id=event_id, actor="ionut")
    assert service.active_pain_stop(conn) is None
    assert service.log_session(conn, slug="p1-w02-s2", actor="dragos", plan=plan) > 0


# --------------------------------------------------------------- benchmarks


def test_recording_and_updating_a_benchmark(conn, plan):
    service.record_benchmark(conn, cycle=1, test_key="run_12min", value=1800)
    service.record_benchmark(conn, cycle=1, test_key="run_12min", value=1850)
    cycles = service.load_benchmark_cycles(conn)
    assert len(cycles) == 1
    assert cycles[0].values["run_12min"] == 1850


def test_benchmark_table_reports_change(conn, plan):
    service.record_benchmark(conn, cycle=1, test_key="run_12min", value=1800)
    service.record_benchmark(conn, cycle=2, test_key="run_12min", value=1980)
    table = service.benchmark_table(conn, plan)
    run = next(t for t in table["tests"] if t["key"] == "run_12min")
    assert run["current"] == 1980
    assert round(run["change_pct"]) == 10


def test_untested_reads_as_untested_not_as_failure(conn, plan):
    table = service.benchmark_table(conn, plan)
    assert table["tested"] is False


# ------------------------------------------------------------ weekly rollup


def test_closed_weeks_are_evaluated_once(conn, plan):
    for offset in range(3):
        log_next(conn, plan, PLAN_START + timedelta(days=offset))

    reference = PLAN_START + timedelta(days=10)
    first = service.evaluate_closed_weeks(conn, reference=reference, plan=plan)
    second = service.evaluate_closed_weeks(conn, reference=reference, plan=plan)

    assert len(first) == 1
    assert second == []
    assert first[0].floor_met


def test_outcomes_survive_a_later_void(conn, plan):
    """Written once, never recomputed from mutable logs."""
    ids = [log_next(conn, plan, PLAN_START + timedelta(days=i)) for i in range(3)]
    reference = PLAN_START + timedelta(days=10)
    service.evaluate_closed_weeks(conn, reference=reference, plan=plan)

    service.void_event(conn, event_id=ids[0], actor="dragos", plan=plan)
    service.evaluate_closed_weeks(conn, reference=reference, plan=plan)

    row = conn.execute("SELECT * FROM week_outcomes").fetchone()
    assert row["sessions_counted"] == 3


# ----------------------------------------------------------------- phases


def test_phase_does_not_advance_without_its_manual_criterion(conn, plan):
    conn.execute("UPDATE progress SET phase = 1")
    for i in range(40):
        log_next(conn, plan, PLAN_START + timedelta(days=i))
    service.evaluate_closed_weeks(conn, reference=PLAN_START + timedelta(days=70), plan=plan)

    assert service.advance_phase_if_ready(conn, plan) is False
    assert db.fetch_progress(conn)["phase"] == 1


def test_coach_confirmation_can_complete_a_phase(conn, plan):
    for i in range(40):
        log_next(conn, plan, PLAN_START + timedelta(days=i))
    service.evaluate_closed_weeks(conn, reference=PLAN_START + timedelta(days=70), plan=plan)
    service.confirm_phase_manual(conn, phase=1, actor="ionut")

    status = service.phase_progress(conn, plan)
    assert all(row["met"] for row in status["criteria"]) == status["complete"]


def test_phase_entry_never_blocks_on_a_missing_benchmark(conn, plan):
    status = service.phase_progress(conn, plan)
    assert not any("benchmark" in row["label"].lower() for row in status["criteria"])


# -------------------------------------------------------------- re-seeding


def test_reseeding_the_same_plan_changes_nothing(conn, plan):
    log_next(conn, plan, PLAN_START)
    before = db.fetch_progress(conn)["pointer_slug"]
    db.seed(conn, plan)
    assert db.fetch_progress(conn)["pointer_slug"] == before
    assert conn.execute("SELECT COUNT(*) AS n FROM plan_revisions").fetchone()["n"] == 1


def test_a_revised_plan_retires_the_old_revision_and_keeps_logs(conn, plan, tmp_path):
    import shutil

    from hyrox import plan as plan_module

    log_next(conn, plan, PLAN_START)
    log_next(conn, plan, PLAN_START + timedelta(days=1))
    pointer_before = db.fetch_progress(conn)["pointer_slug"]

    shutil.copytree(plan_module.PLAN_DIR, tmp_path / "plandata")
    target = tmp_path / "plandata" / "phase-4.yaml"
    target.write_text(target.read_text().replace("2 x 8, comfortable load", "3 x 8, comfortable load"))
    revised = compile_plan(tmp_path / "plandata")

    assert revised.version != plan.version
    db.seed(conn, revised)

    progress = db.fetch_progress(conn)
    assert progress["plan_revision"] == revised.version
    # The athlete keeps his place, and his logged history is intact.
    assert progress["pointer_slug"] == pointer_before
    assert service.sessions_completed(conn) == 2
    retired = conn.execute(
        "SELECT COUNT(*) AS n FROM plan_sessions WHERE revision = ? AND retired_at IS NOT NULL",
        (plan.version,),
    ).fetchone()
    assert retired["n"] == len(plan.sessions)


def test_a_log_still_reads_correctly_after_the_plan_changes(conn, plan, tmp_path):
    import json
    import shutil

    from hyrox import plan as plan_module

    event_id = log_next(conn, plan, PLAN_START)

    shutil.copytree(plan_module.PLAN_DIR, tmp_path / "plandata")
    target = tmp_path / "plandata" / "phase-1.yaml"
    target.write_text(target.read_text().replace("Goblet squat to box", "Something else entirely"))
    db.seed(conn, compile_plan(tmp_path / "plandata"))

    row = conn.execute("SELECT snapshot_json FROM session_events WHERE id = ?", (event_id,)).fetchone()
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["detail"][0]["name"] == "Goblet squat to box"


# --------------------------------------------------------------- read models


def test_week_strip_counts_only_the_current_week(conn, plan):
    log_next(conn, plan, PLAN_START)
    log_next(conn, plan, PLAN_START + timedelta(days=8))
    strip = service.week_strip(conn, PLAN_START + timedelta(days=2))
    assert strip["counted"] == 1
    assert strip["floor"] == 3


def test_dashboard_state_reports_the_plan_as_complete_at_the_end(conn, plan):
    conn.execute("UPDATE progress SET pointer_slug = NULL")
    assert service.dashboard_state(conn, plan)["plan_complete"] is True
