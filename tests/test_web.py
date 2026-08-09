"""Routes, roles, CSRF and the offline replay endpoint."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from hyrox import service

from conftest import ATHLETE_PW, COACH_PW


def token(client: TestClient, path: str = "/") -> str:
    page = client.get(path, follow_redirects=True).text
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, f"no csrf token on {path}"
    return match.group(1)


# -------------------------------------------------------------------- auth


def test_anonymous_is_redirected_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_is_public(client):
    assert client.get("/login").status_code == 200


def test_healthz_is_public(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_bad_password_is_rejected(client):
    response = client.post("/login", data={"username": "dragos", "password": "wrong"})
    assert response.status_code == 401
    assert "Wrong username or password" in response.text


def test_unknown_user_is_rejected(client):
    response = client.post("/login", data={"username": "nobody", "password": "x"})
    assert response.status_code == 401


def test_athlete_lands_on_the_session_screen(athlete):
    page = athlete.get("/").text
    assert "Strength A" in page
    assert "Done" in page


def test_coach_is_redirected_to_the_coach_view(coach):
    response = coach.get("/", follow_redirects=False)
    assert response.headers["location"] == "/coach"


def test_bumping_the_session_version_revokes_cookies(app, config, client):
    client.post("/login", data={"username": "dragos", "password": ATHLETE_PW})
    assert client.get("/", follow_redirects=False).status_code == 200

    object.__setattr__(config, "session_version", 2)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_api_returns_401_rather_than_a_redirect(client):
    assert client.get("/api/state").status_code == 401


# ------------------------------------------------------------------- roles


def test_athlete_cannot_reach_the_coach_view(athlete):
    assert athlete.get("/coach").status_code == 403


def test_athlete_owns_his_own_plan(athlete, app):
    """No coach hierarchy: Dragos releases his own pain stop.

    Deliberately his call rather than a gated one -- it is his body. The app
    still records who cleared it and why, and still tells the observer.
    """
    response = athlete.post(
        "/complete",
        data={"slug": "p1-w01-s2", "csrf_token": token(athlete)},
        follow_redirects=False,
    )
    event_id = int(response.headers["location"].rsplit("/", 1)[1])
    athlete.post(
        f"/log/{event_id}",
        data={"csrf_token": token(athlete), "pain_score": "7", "pain_location": "knee"},
    )
    conn = app.state.conn
    assert service.active_pain_stop(conn) is not None

    athlete.post(
        "/coach/ack-pain",
        data={"event_id": event_id, "csrf_token": token(athlete), "note": "settled overnight"},
    )
    assert service.active_pain_stop(conn) is None
    ack = conn.execute("SELECT * FROM acknowledgements WHERE kind = 'pain_stop'").fetchone()
    assert ack["actor"] == "dragos" and ack["note"] == "settled overnight"


def test_athlete_can_confirm_a_phase_criterion(athlete, app):
    athlete.post("/coach/confirm-phase", data={"phase": 1, "csrf_token": token(athlete)})
    assert service.phase_manual_confirmed(app.state.conn, 1)


def test_athlete_can_pause_his_own_training(athlete, app):
    athlete.post(
        "/coach/pause",
        data={
            "kind": "holiday",
            "start": "2026-08-10",
            "end": "",
            "csrf_token": token(athlete),
        },
    )
    row = app.state.conn.execute("SELECT * FROM pause_windows").fetchone()
    assert row["kind"] == "holiday"


def test_coach_cannot_use_the_athlete_logging_screen(coach):
    assert coach.get("/log/1").status_code == 403


def test_coach_cannot_post_to_the_offline_api(coach):
    response = coach.post("/api/complete", json={"slug": "p1-w01-s1"})
    assert response.status_code == 403


# -------------------------------------------------------------------- csrf


def test_write_without_a_csrf_token_is_refused(athlete):
    response = athlete.post("/complete", data={"slug": "p1-w01-s1"})
    assert response.status_code == 422  # missing required form field


def test_write_with_a_forged_csrf_token_is_refused(athlete):
    response = athlete.post(
        "/complete", data={"slug": "p1-w01-s1", "csrf_token": "forged"}
    )
    assert response.status_code == 403


def test_a_token_issued_to_one_user_does_not_work_for_another(app, coach):
    coach_token = token(coach, "/coach")
    with TestClient(app, base_url="https://testserver") as other:
        other.post("/login", data={"username": "dragos", "password": ATHLETE_PW})
        response = other.post(
            "/complete", data={"slug": "p1-w01-s1", "csrf_token": coach_token}
        )
    assert response.status_code == 403


# ------------------------------------------------------------ athlete flows


def test_completing_a_session_leads_to_the_optional_detail_screen(athlete):
    response = athlete.post(
        "/complete",
        data={"slug": "p1-w01-s1", "csrf_token": token(athlete)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/log/")

    detail = athlete.get(response.headers["location"]).text
    assert "the session is saved" in detail
    assert "optional" in detail


def test_the_queue_advances_after_completing(athlete):
    athlete.post("/complete", data={"slug": "p1-w01-s1", "csrf_token": token(athlete)})
    assert "Ankle prep" in athlete.get("/").text


def test_saving_details_records_pain(athlete, app):
    response = athlete.post(
        "/complete",
        data={"slug": "p1-w01-s1", "csrf_token": token(athlete)},
        follow_redirects=False,
    )
    event_id = int(response.headers["location"].rsplit("/", 1)[1])
    athlete.post(
        f"/log/{event_id}",
        data={
            "csrf_token": token(athlete),
            "rpe": "7",
            "pain_score": "3",
            "pain_location": "ankle",
            "note": "left side",
        },
    )
    row = app.state.conn.execute(
        "SELECT * FROM session_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert (row["rpe"], row["pain_score"], row["pain_location"]) == (7, 3, "ankle")


def test_skipping_records_a_reason_and_keeps_the_session(athlete):
    athlete.post(
        "/skip",
        data={"slug": "p1-w01-s1", "reason": "tired", "csrf_token": token(athlete)},
    )
    assert "Strength A" in athlete.get("/").text


def test_undo_removes_the_completion(athlete, app):
    response = athlete.post(
        "/complete",
        data={"slug": "p1-w01-s1", "csrf_token": token(athlete)},
        follow_redirects=False,
    )
    event_id = int(response.headers["location"].rsplit("/", 1)[1])
    athlete.post(f"/void/{event_id}", data={"csrf_token": token(athlete)})
    assert "Strength A" in athlete.get("/").text


def test_a_rejected_swap_shows_a_readable_message(athlete):
    response = athlete.post(
        "/swap",
        data={
            "slug": "p1-w01-s1",
            "alternative": "p4-w12-s5",
            "csrf_token": token(athlete),
        },
    )
    assert response.status_code == 400
    assert "declared alternative" in response.text


def test_session_detail_is_reachable(athlete):
    page = athlete.get("/session/p1-w01-s1").text
    assert "Goblet squat to box" in page


def test_unknown_session_is_a_404(athlete):
    assert athlete.get("/session/nope").status_code == 404


# --------------------------------------------------------------- checkpoint


def test_recording_a_checkpoint(athlete, app):
    athlete.post(
        "/checkpoint",
        data={"csrf_token": token(athlete, "/checkpoint"), "cycle": "1", "test_run_12min": "1900"},
    )
    row = app.state.conn.execute("SELECT * FROM benchmarks").fetchone()
    assert row["test_key"] == "run_12min" and row["value"] == 1900


def test_blank_checkpoint_fields_are_not_recorded(athlete, app):
    athlete.post(
        "/checkpoint",
        data={
            "csrf_token": token(athlete, "/checkpoint"),
            "cycle": "1",
            "test_run_12min": "",
            "test_thrusters": "20",
        },
    )
    rows = app.state.conn.execute("SELECT test_key FROM benchmarks").fetchall()
    assert [r["test_key"] for r in rows] == ["thrusters"]


# ------------------------------------------------------------ offline queue


def test_offline_replay_logs_once(athlete, app):
    payload = {
        "slug": "p1-w01-s1",
        "csrf_token": token(athlete),
        "idempotency_key": "offline-1",
    }
    first = athlete.post("/api/complete", json=payload)
    second = athlete.post("/api/complete", json=payload)

    assert first.status_code == 200
    assert first.json()["event_id"] == second.json()["event_id"]
    count = app.state.conn.execute(
        "SELECT COUNT(*) AS n FROM session_events WHERE voided_at IS NULL"
    ).fetchone()
    assert count["n"] == 1


def test_offline_replay_requires_a_valid_csrf_token(athlete):
    response = athlete.post(
        "/api/complete", json={"slug": "p1-w01-s1", "csrf_token": "forged"}
    )
    assert response.status_code == 403


def test_api_state_reports_the_queue(athlete):
    body = athlete.get("/api/state").json()
    assert body["up_next"] == "p1-w01-s1"
    assert body["sessions_remaining"] == 240


# ------------------------------------------------------------------- coach


def test_coach_can_comment(coach, athlete, app):
    athlete.post("/complete", data={"slug": "p1-w01-s1", "csrf_token": token(athlete)})
    event_id = app.state.conn.execute("SELECT id FROM session_events").fetchone()["id"]

    coach.post(
        "/coach/comment",
        data={"event_id": event_id, "body": "good start", "csrf_token": token(coach, "/coach")},
    )
    row = app.state.conn.execute("SELECT * FROM comments").fetchone()
    assert row["body"] == "good start" and row["author"] == "ionut"


def test_coach_can_open_and_close_a_pause(coach, app):
    coach.post(
        "/coach/pause",
        data={
            "kind": "holiday",
            "start": "2026-08-10",
            "end": "",
            "csrf_token": token(coach, "/coach"),
        },
    )
    row = app.state.conn.execute("SELECT * FROM pause_windows").fetchone()
    assert row["kind"] == "holiday" and row["end_date"] is None

    coach.post(f"/coach/pause/{row['id']}/end", data={"csrf_token": token(coach, "/coach")})
    row = app.state.conn.execute("SELECT * FROM pause_windows").fetchone()
    assert row["end_date"] is not None


def test_coach_view_renders_with_no_data(coach):
    page = coach.get("/coach").text
    assert "Dragos" in page
    assert "No completed weeks yet" in page


# ----------------------------------------------------------------- assets


def test_service_worker_is_served_from_the_root(client):
    """A worker under /static could only ever control /static."""
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "application/javascript" in response.headers["content-type"]


def test_manifest_is_public(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.json()["short_name"] == "Hyrox"


# --------------------------------------------------------------- whole plan


def test_plan_view_shows_every_phase_and_session(athlete):
    page = athlete.get("/plan").text
    assert "The whole plan" in page
    assert "240 sessions" in page
    for name in ("Show up", "Engine and leg capacity", "Stations and partner", "Rehearse and taper"):
        assert name in page


def test_plan_view_marks_what_has_happened(athlete):
    athlete.post("/complete", data={"slug": "p1-w01-s1", "csrf_token": token(athlete)})
    page = athlete.get("/plan").text
    assert "plan-completed" in page
    assert "plan-current" in page


def test_plan_view_is_open_to_the_coach_too(coach):
    assert coach.get("/plan").status_code == 200


def test_plan_view_links_to_session_detail(athlete):
    assert '/session/p1-w01-s1' in athlete.get("/plan").text


def test_releasing_a_pain_stop_notifies_the_observer(athlete, app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "hyrox.web.notify.post", lambda webhook, content: sent.append(content) or True
    )
    object.__setattr__(app.state.config, "discord_webhook", "https://example.invalid/hook")

    response = athlete.post(
        "/complete",
        data={"slug": "p1-w01-s2", "csrf_token": token(athlete)},
        follow_redirects=False,
    )
    event_id = int(response.headers["location"].rsplit("/", 1)[1])
    athlete.post(
        f"/log/{event_id}",
        data={"csrf_token": token(athlete), "pain_score": "6", "pain_location": "shin"},
    )
    athlete.post(
        "/coach/ack-pain",
        data={"event_id": event_id, "csrf_token": token(athlete), "note": "felt fine after"},
    )
    assert any("resumed training after pain 6/10 at the shin" in m for m in sent)
