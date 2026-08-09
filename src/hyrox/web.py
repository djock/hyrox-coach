"""HTTP routes.

Server-rendered rather than a SPA: the interaction budget is "open link, tap
checkbox", and a framework buys nothing against that. The one piece of real
client-side JavaScript is the offline completion queue in `static/app.js`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import auth, db, notify, service
from .config import Config
from .plan import load_plan
from .timeutil import iso_week, today

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

router = APIRouter()


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def render(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    principal = auth.current_principal(request)
    config = get_config(request)
    base = {
        "principal": principal,
        "csrf_token": auth.issue_csrf(config, principal) if principal else "",
        "today": today(),
        "dev_mode": config.dev_mode,
    }
    return TEMPLATES.TemplateResponse(
        request, template, {**base, **context}, status_code=status_code
    )


def verify_csrf(request: Request, token: str | None) -> auth.Principal:
    principal = auth.require_any(request)
    if not auth.check_csrf(get_config(request), principal, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad csrf token")
    return principal


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    if auth.current_principal(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    config = get_config(request)
    user = config.user(username.strip())
    if user is None or not auth.verify_password(password, user.password_hash):
        return render(request, "login.html", {"error": "Wrong username or password."}, 401)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_cookie(config, user.username),
        max_age=auth.COOKIE_MAX_AGE,
        httponly=True,
        secure=not config.dev_mode,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)) -> Response:
    verify_csrf(request, csrf_token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# --------------------------------------------------------------------------
# Athlete
# --------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    principal = auth.current_principal(request)
    if principal is None:
        return RedirectResponse("/login", status_code=303)
    if principal.is_coach:
        return RedirectResponse("/coach", status_code=303)

    conn = get_conn(request)
    plan = load_plan()
    service.evaluate_closed_weeks(conn, plan=plan)
    service.advance_phase_if_ready(conn, plan)

    up_next = service.current_session(conn, plan)
    state = service.dashboard_state(conn, plan)
    return render(
        request,
        "athlete.html",
        {
            "up_next": up_next,
            "state": state,
            "strip": service.week_strip(conn),
            "benchmarks": service.benchmark_table(conn, plan),
            "phase_progress": service.phase_progress(conn, plan),
        },
    )


@router.get("/session/{slug}", response_class=HTMLResponse)
def session_detail(request: Request, slug: str) -> Response:
    auth.require_any(request)
    plan = load_plan()
    session = plan.session(slug)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such session")

    conn = get_conn(request)
    alternatives = [plan.session(a.slug) for a in session.alternatives]
    return render(
        request,
        "session.html",
        {
            "session": session,
            "alternatives": [a for a in alternatives if a is not None],
            "state": service.dashboard_state(conn, plan),
        },
    )


def _complete(
    request: Request,
    *,
    slug: str,
    idempotency_key: str | None,
    substituted_from: str | None = None,
    substitution_reason: str | None = None,
) -> int:
    conn = get_conn(request)
    principal = auth.require(request)
    return service.log_session(
        conn,
        slug=slug,
        actor=principal.username,
        idempotency_key=idempotency_key,
        substituted_from=substituted_from,
        substitution_reason=substitution_reason,
    )


@router.post("/complete")
def complete(
    request: Request,
    slug: str = Form(...),
    csrf_token: str = Form(...),
    idempotency_key: str = Form(""),
    substituted_from: str = Form(""),
    substitution_reason: str = Form(""),
) -> Response:
    """One tap. Everything else is optional and lives on the next screen."""
    verify_csrf(request, csrf_token)
    try:
        event_id = _complete(
            request,
            slug=slug,
            idempotency_key=idempotency_key or None,
            substituted_from=substituted_from or None,
            substitution_reason=substitution_reason or None,
        )
    except service.ServiceError as exc:
        return render(request, "error.html", {"message": str(exc)}, 400)
    return RedirectResponse(f"/log/{event_id}", status_code=303)


@router.get("/log/{event_id}", response_class=HTMLResponse)
def log_form(request: Request, event_id: int) -> Response:
    auth.require_athlete(request)
    conn = get_conn(request)
    row = conn.execute("SELECT * FROM session_events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such event")

    snapshot = json.loads(row["snapshot_json"])
    # Pain is prompted after every run exposure and otherwise every third
    # session. Prompted, never mandatory -- a required field just teaches him to
    # type zero to escape the form.
    prompt_pain = snapshot.get("counts_as") == "run_exposure" or event_id % 3 == 0
    return render(
        request,
        "log_detail.html",
        {
            "event": row,
            "snapshot": snapshot,
            "prompt_pain": prompt_pain,
            "pain_locations": service.PAIN_LOCATIONS,
        },
    )


@router.post("/log/{event_id}")
def log_details(
    request: Request,
    event_id: int,
    csrf_token: str = Form(...),
    duration_min: str = Form(""),
    rpe: str = Form(""),
    pain_score: str = Form(""),
    pain_location: str = Form(""),
    note: str = Form(""),
) -> Response:
    verify_csrf(request, csrf_token)
    conn = get_conn(request)
    config = get_config(request)

    row = conn.execute("SELECT * FROM session_events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such event")

    score = int(pain_score) if pain_score.strip() else None
    conn.execute(
        """
        UPDATE session_events
        SET duration_min = COALESCE(?, duration_min),
            rpe = ?, pain_score = ?, pain_location = ?, note = ?
        WHERE id = ?
        """,
        (
            int(duration_min) if duration_min.strip() else None,
            int(rpe) if rpe.strip() else None,
            score,
            pain_location or None,
            note or None,
            event_id,
        ),
    )

    if score is not None and score >= service.PAIN_STOP_THRESHOLD:
        notify.alert_pain(config.discord_webhook, score=score, location=pain_location or "unstated")

    return RedirectResponse("/", status_code=303)


@router.post("/skip")
def skip(
    request: Request,
    slug: str = Form(...),
    reason: str = Form(...),
    csrf_token: str = Form(...),
) -> Response:
    verify_csrf(request, csrf_token)
    principal = auth.require_athlete(request)
    try:
        service.skip_session(
            get_conn(request), slug=slug, actor=principal.username, reason=reason
        )
    except service.ServiceError as exc:
        return render(request, "error.html", {"message": str(exc)}, 400)
    return RedirectResponse("/", status_code=303)


@router.post("/swap")
def swap(
    request: Request,
    slug: str = Form(...),
    alternative: str = Form(...),
    csrf_token: str = Form(...),
) -> Response:
    """Perform a declared alternative instead, spending the weekly allowance."""
    verify_csrf(request, csrf_token)
    conn = get_conn(request)
    try:
        replacement = service.resolve_swap(conn, slug=slug, alternative_slug=alternative)
        event_id = _complete(
            request,
            slug=replacement.slug,
            idempotency_key=None,
            substituted_from=slug,
            substitution_reason="voluntary",
        )
    except service.ServiceError as exc:
        return render(request, "error.html", {"message": str(exc)}, 400)
    return RedirectResponse(f"/log/{event_id}", status_code=303)


@router.post("/void/{event_id}")
def void(request: Request, event_id: int, csrf_token: str = Form(...)) -> Response:
    principal = verify_csrf(request, csrf_token)
    try:
        service.void_event(get_conn(request), event_id=event_id, actor=principal.username)
    except service.ServiceError as exc:
        return render(request, "error.html", {"message": str(exc)}, 400)
    return RedirectResponse("/" if principal.is_athlete else "/coach", status_code=303)


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------


@router.get("/checkpoint", response_class=HTMLResponse)
def checkpoint(request: Request) -> Response:
    auth.require_any(request)
    conn = get_conn(request)
    plan = load_plan()
    progress = db.fetch_progress(conn)
    phase = plan.phase(progress["phase"])
    pointer = progress["pointer_slug"]
    session = plan.session(pointer) if pointer else None
    cycle = service.cycle_for_week(session.global_week if session else 1)

    return render(
        request,
        "checkpoint.html",
        {
            "table": service.benchmark_table(conn, plan),
            "cycle": cycle,
            "always": plan.benchmarks["always"],
            "phase": phase,
        },
    )


@router.post("/checkpoint")
async def save_checkpoint(request: Request) -> Response:
    """Values arrive as `test_<key>` fields; blanks are simply not recorded.

    A skipped test is not a failure -- an untested cycle reports "not tested"
    and never blocks phase entry.
    """
    conn = get_conn(request)
    form = await request.form()
    verify_csrf(request, form.get("csrf_token"))

    cycle = int(form.get("cycle") or 1)
    for key in form:
        if not key.startswith("test_"):
            continue
        raw = str(form.get(key) or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        service.record_benchmark(conn, cycle=cycle, test_key=key.removeprefix("test_"), value=value)
    return RedirectResponse("/checkpoint", status_code=303)


# --------------------------------------------------------------------------
# Coach
# --------------------------------------------------------------------------


@router.get("/coach", response_class=HTMLResponse)
def coach(request: Request) -> Response:
    auth.require_coach(request)
    conn = get_conn(request)
    plan = load_plan()
    service.evaluate_closed_weeks(conn, plan=plan)

    outcomes = list(
        conn.execute("SELECT * FROM week_outcomes ORDER BY iso_week DESC LIMIT 12")
    )
    events = list(
        conn.execute(
            """
            SELECT e.*, (SELECT COUNT(*) FROM comments c WHERE c.event_id = e.id) AS comment_count
            FROM session_events e
            WHERE e.voided_at IS NULL
            ORDER BY e.training_date DESC, e.id DESC
            LIMIT 30
            """
        )
    )
    pauses = list(conn.execute("SELECT * FROM pause_windows ORDER BY start_date DESC"))

    return render(
        request,
        "coach.html",
        {
            "state": service.dashboard_state(conn, plan),
            "outcomes": outcomes,
            "events": [dict(e, snapshot=json.loads(e["snapshot_json"])) for e in events],
            "pauses": pauses,
            "benchmarks": service.benchmark_table(conn, plan),
            "phase_progress": service.phase_progress(conn, plan),
            "streak": _streak(outcomes),
        },
    )


def _streak(outcomes: list[sqlite3.Row]) -> int:
    count = 0
    for outcome in outcomes:  # already newest first
        if outcome["floor_met"]:
            count += 1
        else:
            break
    return count


@router.post("/coach/comment")
def add_comment(
    request: Request,
    event_id: int = Form(...),
    body: str = Form(...),
    csrf_token: str = Form(...),
) -> Response:
    principal = verify_csrf(request, csrf_token)
    if not principal.is_coach:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "coach only")
    from .service import _utc_now

    get_conn(request).execute(
        "INSERT INTO comments (event_id, author, body, created_at) VALUES (?,?,?,?)",
        (event_id, principal.username, body.strip(), _utc_now()),
    )
    return RedirectResponse("/coach", status_code=303)


@router.post("/coach/ack-pain")
def ack_pain(
    request: Request,
    event_id: int = Form(...),
    csrf_token: str = Form(...),
    note: str = Form(""),
) -> Response:
    principal = verify_csrf(request, csrf_token)
    if not principal.is_coach:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "coach only")
    service.acknowledge_pain_stop(
        get_conn(request), event_id=event_id, actor=principal.username, note=note or None
    )
    return RedirectResponse("/coach", status_code=303)


@router.post("/coach/confirm-phase")
def confirm_phase(
    request: Request, phase: int = Form(...), csrf_token: str = Form(...)
) -> Response:
    principal = verify_csrf(request, csrf_token)
    if not principal.is_coach:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "coach only")
    conn = get_conn(request)
    service.confirm_phase_manual(conn, phase=phase, actor=principal.username)
    service.advance_phase_if_ready(conn)
    return RedirectResponse("/coach", status_code=303)


@router.post("/coach/pause")
def add_pause(
    request: Request,
    kind: str = Form(...),
    start: str = Form(...),
    end: str = Form(""),
    note: str = Form(""),
    csrf_token: str = Form(...),
) -> Response:
    principal = verify_csrf(request, csrf_token)
    if not principal.is_coach:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "coach only")
    service.start_pause(
        get_conn(request),
        kind=kind,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end) if end.strip() else None,
        note=note or None,
    )
    return RedirectResponse("/coach", status_code=303)


@router.post("/coach/pause/{pause_id}/end")
def finish_pause(request: Request, pause_id: int, csrf_token: str = Form(...)) -> Response:
    principal = verify_csrf(request, csrf_token)
    if not principal.is_coach:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "coach only")
    service.end_pause(get_conn(request), pause_id=pause_id)
    return RedirectResponse("/coach", status_code=303)


# --------------------------------------------------------------------------
# JSON API for the offline queue
# --------------------------------------------------------------------------


@router.post("/api/complete")
async def api_complete(request: Request) -> JSONResponse:
    """Replay target for completions queued while offline.

    Idempotency keys make replay safe: a duplicate returns the original event
    rather than logging a second one.
    """
    principal = auth.require_athlete(request)
    payload = await request.json()
    if not auth.check_csrf(get_config(request), principal, payload.get("csrf_token")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad csrf token")

    training_date = payload.get("training_date")
    try:
        event_id = service.log_session(
            get_conn(request),
            slug=payload["slug"],
            actor=principal.username,
            training_date=date.fromisoformat(training_date) if training_date else None,
            idempotency_key=payload.get("idempotency_key"),
            substituted_from=payload.get("substituted_from") or None,
            substitution_reason=payload.get("substitution_reason") or None,
            rpe=payload.get("rpe"),
            pain_score=payload.get("pain_score"),
            pain_location=payload.get("pain_location"),
            note=payload.get("note"),
        )
    except service.ServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"event_id": event_id})


@router.get("/api/state")
def api_state(request: Request) -> JSONResponse:
    auth.require_any(request)
    conn = get_conn(request)
    state = service.dashboard_state(conn)
    up_next = service.current_session(conn)
    return JSONResponse(
        {
            "iso_week": iso_week(today()),
            "up_next": up_next.session.slug if up_next else None,
            "buffer_weeks": state["buffer_weeks"],
            "projected_race_date": state["projected_race_date"].isoformat(),
            "sessions_remaining": state["sessions_remaining"],
        }
    )


@router.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
