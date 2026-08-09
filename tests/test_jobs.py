"""Cron jobs: evaluation, reminders, digests, backup and restore."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hyrox import jobs, service
from hyrox.plan import load_plan

from conftest import PLAN_START


@pytest.fixture
def sent(monkeypatch):
    """Capture Discord posts instead of making network calls."""
    messages: list[str] = []
    monkeypatch.setattr(jobs.notify, "post", lambda webhook, content: messages.append(content) or True)
    return messages


@pytest.fixture
def wired(config, conn):
    """A config whose webhook is set, sharing the seeded database."""
    object.__setattr__(config, "discord_webhook", "https://example.invalid/hook")
    return config


def log(conn, plan, day):
    up_next = service.current_session(conn, plan)
    service.log_session(conn, slug=up_next.session.slug, actor="dragos", training_date=day, plan=plan)


# ------------------------------------------------------------------- nudge


def test_nudge_fires_when_the_floor_is_at_risk(wired, conn, sent):
    plan = load_plan()
    log(conn, plan, PLAN_START)
    assert jobs.nudge(wired, reference=PLAN_START + timedelta(days=3)) is True
    assert "1/3 sessions" in sent[0]


def test_no_nudge_once_the_floor_is_met(wired, conn, sent):
    plan = load_plan()
    for offset in range(3):
        log(conn, plan, PLAN_START + timedelta(days=offset))
    assert jobs.nudge(wired, reference=PLAN_START + timedelta(days=3)) is False
    assert sent == []


def test_no_nudge_during_a_pause(wired, conn, sent):
    """Nagging someone on holiday is how you teach them to ignore the app."""
    service.start_pause(conn, kind="holiday", start=PLAN_START, end=PLAN_START + timedelta(days=6))
    assert jobs.nudge(wired, reference=PLAN_START + timedelta(days=3)) is False


def test_no_nudge_while_training_is_halted_for_pain(wired, conn, sent):
    plan = load_plan()
    service.log_session(
        conn,
        slug="p1-w01-s2",
        actor="dragos",
        training_date=PLAN_START,
        pain_score=7,
        pain_location="knee",
        plan=plan,
    )
    assert jobs.nudge(wired, reference=PLAN_START + timedelta(days=3)) is False


# ---------------------------------------------------------------- evaluate


def test_evaluate_closes_weeks_and_is_idempotent(wired, conn, sent):
    plan = load_plan()
    for offset in range(3):
        log(conn, plan, PLAN_START + timedelta(days=offset))
    reference = PLAN_START + timedelta(days=10)

    assert jobs.evaluate(wired, reference=reference) == 1
    assert jobs.evaluate(wired, reference=reference) == 0


def test_evaluate_alerts_the_coach_after_two_sub_floor_weeks(wired, conn, sent):
    jobs.evaluate(wired, reference=PLAN_START + timedelta(days=17))
    assert any("below the floor" in m for m in sent)


# ------------------------------------------------------------------ digest


def test_digest_reports_the_closed_week(wired, conn, sent):
    plan = load_plan()
    for offset in range(3):
        log(conn, plan, PLAN_START + timedelta(days=offset))
    jobs.evaluate(wired, reference=PLAN_START + timedelta(days=7))

    assert jobs.digest(wired, reference=PLAN_START + timedelta(days=7)) is True
    assert any("2026-W32" in m for m in sent)


def test_digest_is_silent_with_nothing_to_report(wired, conn, sent):
    assert jobs.digest(wired, reference=PLAN_START) is False


# ------------------------------------------------------------------ backup


def test_backup_writes_a_snapshot(wired, conn):
    plan = load_plan()
    log(conn, plan, PLAN_START)
    target = jobs.backup(wired)
    assert target is not None and target.exists()


def test_backup_and_restore_round_trip(wired, conn):
    """An untested restore path is not a backup."""
    plan = load_plan()
    log(conn, plan, PLAN_START)
    snapshot = jobs.backup(wired)

    # Lose everything after the snapshot.
    conn.execute("DELETE FROM session_events")
    conn.close()
    jobs.restore(wired, snapshot)

    from hyrox import db

    restored = db.connect(wired.db_path)
    count = restored.execute("SELECT COUNT(*) AS n FROM session_events").fetchone()
    assert count["n"] == 1


def test_backup_prunes_old_snapshots(wired, conn):
    stale = wired.backup_dir
    stale.mkdir(parents=True, exist_ok=True)
    old = stale / f"hyrox-{(date.today() - timedelta(days=90)).isoformat()}.sqlite"
    old.write_text("stale")

    jobs.backup(wired)
    assert not old.exists()


def test_backup_is_a_no_op_without_a_configured_directory(config, conn):
    object.__setattr__(config, "backup_dir", None)
    assert jobs.backup(config) is None
