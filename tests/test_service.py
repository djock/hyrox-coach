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


def test_only_one_voluntary_swap_per_week(conn, plan, monkeypatch):
    monkeypatch.setattr(service, "today", lambda: PLAN_START)
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
    conn.execute("UPDATE progress SET plan_week = 5")
    service.log_session(conn, slug="p1-w05-s1", actor="dragos", plan=plan)

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


def test_perfect_adherence_advances_one_plan_week_per_calendar_week(conn, plan):
    for week in range(4):
        monday = PLAN_START + timedelta(weeks=week)
        for offset in range(3):
            log_next(conn, plan, monday + timedelta(days=offset))
        service.evaluate_closed_weeks(conn, reference=monday + timedelta(days=7), plan=plan)

    assert db.fetch_progress(conn)["plan_week"] == 5


def test_a_sub_floor_week_repeats_the_same_plan_week(conn, plan):
    offered = [row["session"].slug for row in service.current_week_sessions(conn, plan)]
    log_next(conn, plan, PLAN_START)
    log_next(conn, plan, PLAN_START + timedelta(days=1))

    service.evaluate_closed_weeks(conn, reference=PLAN_START + timedelta(days=7), plan=plan)

    assert db.fetch_progress(conn)["plan_week"] == 1
    assert [row["session"].slug for row in service.current_week_sessions(conn, plan)] == offered


def test_unfinished_optional_sessions_expire_when_a_week_closes(conn, plan):
    optional = {
        row["session"].slug
        for row in service.current_week_sessions(conn, plan)
        if not row["session"].in_floor
    }
    for offset in range(3):
        log_next(conn, plan, PLAN_START + timedelta(days=offset))

    service.evaluate_closed_weeks(conn, reference=PLAN_START + timedelta(days=7), plan=plan)

    assert db.fetch_progress(conn)["plan_week"] == 2
    assert optional.isdisjoint(
        row["session"].slug for row in service.current_week_sessions(conn, plan)
    )


def test_a_paused_week_does_not_advance_or_spend_buffer(conn, plan):
    before = db.fetch_progress(conn)["buffer_weeks"]
    service.start_pause(conn, kind="holiday", start=PLAN_START, end=PLAN_START + timedelta(days=6))

    service.evaluate_closed_weeks(conn, reference=PLAN_START + timedelta(days=7), plan=plan)

    progress = db.fetch_progress(conn)
    assert progress["plan_week"] == 1
    assert progress["buffer_weeks"] == before


def test_outcomes_survive_a_later_void(conn, plan):
    """Written once, never recomputed from mutable logs."""
    ids = [log_next(conn, plan, PLAN_START + timedelta(days=i)) for i in range(3)]
    reference = PLAN_START + timedelta(days=10)
    service.evaluate_closed_weeks(conn, reference=reference, plan=plan)

    service.void_event(conn, event_id=ids[0], actor="dragos", plan=plan)
    service.evaluate_closed_weeks(conn, reference=reference, plan=plan)

    row = conn.execute("SELECT * FROM week_outcomes").fetchone()
    assert row["sessions_counted"] == 3


def test_migration_backfills_plan_week_without_losing_events(tmp_path, plan):
    path = tmp_path / "v1.sqlite"
    legacy = db.connect(path)
    legacy.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        CREATE TABLE progress (
            id INTEGER PRIMARY KEY CHECK (id = 1), plan_start TEXT NOT NULL,
            plan_revision TEXT NOT NULL, pointer_slug TEXT, phase INTEGER NOT NULL,
            phase_entry_date TEXT NOT NULL, buffer_weeks REAL NOT NULL,
            baseline_race_date TEXT NOT NULL, projected_race_date TEXT NOT NULL
        );
        CREATE TABLE plan_revisions (
            version TEXT PRIMARY KEY, seeded_at TEXT NOT NULL, session_count INTEGER NOT NULL
        );
        CREATE TABLE plan_sessions (
            slug TEXT NOT NULL, revision TEXT NOT NULL, ordinal INTEGER NOT NULL,
            phase INTEGER NOT NULL, week_in_phase INTEGER NOT NULL, global_week INTEGER NOT NULL,
            slot INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
            counts_as TEXT NOT NULL, in_floor INTEGER NOT NULL, cap_minutes INTEGER NOT NULL,
            variant TEXT, detail_json TEXT NOT NULL, alternatives_json TEXT NOT NULL,
            retired_at TEXT, PRIMARY KEY (slug, revision)
        );
        CREATE TABLE session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL,
            training_date TEXT NOT NULL
        );
        CREATE TABLE week_outcomes (
            iso_week TEXT PRIMARY KEY, floor_required INTEGER NOT NULL,
            sessions_counted INTEGER NOT NULL, floor_met INTEGER NOT NULL, status TEXT NOT NULL,
            buffer_delta REAL NOT NULL, buffer_after REAL NOT NULL,
            projected_race_date TEXT NOT NULL, detail_json TEXT NOT NULL, evaluated_at TEXT NOT NULL
        );
        INSERT INTO schema_version VALUES (1);
        """
    )
    session = plan.session("p1-w05-s2")
    assert session is not None
    legacy.execute(
        "INSERT INTO plan_revisions VALUES (?,?,?)", (plan.version, "2026-08-03T00:00:00+00:00", 240)
    )
    legacy.execute(
        """INSERT INTO plan_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        (
            session.slug, plan.version, session.ordinal, session.phase, session.week_in_phase,
            session.global_week, session.slot, session.kind, session.title, session.counts_as,
            int(session.in_floor), session.cap_minutes, session.variant, "[]", "[]",
        ),
    )
    legacy.execute(
        "INSERT INTO progress VALUES (1,?,?,?,?,?,?,?,?)",
        (PLAN_START.isoformat(), plan.version, session.slug, 1, PLAN_START.isoformat(), 4.0,
         "2027-07-05", "2027-07-05"),
    )
    legacy.execute("INSERT INTO session_events (slug, training_date) VALUES (?,?)", (session.slug, PLAN_START.isoformat()))

    db.migrate(legacy)

    assert db.fetch_progress(legacy)["plan_week"] == 5
    assert legacy.execute("SELECT COUNT(*) AS n FROM session_events").fetchone()["n"] == 1


# ----------------------------------------------------------------- phases


def test_phase_does_not_advance_without_its_manual_criterion(conn, plan):
    conn.execute("UPDATE progress SET phase = 1, plan_week = 12")

    assert service.advance_phase_if_ready(conn, plan) is False
    assert db.fetch_progress(conn)["phase"] == 1


def test_coach_confirmation_can_complete_a_phase(conn, plan):
    conn.execute("UPDATE progress SET plan_week = 12")
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
    conn.execute("UPDATE progress SET plan_week = 49")
    assert service.dashboard_state(conn, plan)["plan_complete"] is True
