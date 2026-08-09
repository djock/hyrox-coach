"""Schema, migrations and plan seeding.

The schema exists to make state transitions explicit. The review of v1 found
that its five tables could not represent the things that actually happen to a
training plan -- a holiday, a mis-tap, a revised plan -- so those are all rows
here rather than implicit gaps in the log.

Two invariants everything else depends on:

* **`session_events` is append-only.** A mis-tapped "Done" is voided, never
  deleted, and every event carries a frozen snapshot of what the session said at
  the time. Revising the plan cannot rewrite history.
* **`week_outcomes` is written once** by the Monday evaluation and never
  recomputed. Adherence history stays stable even as logs are voided or the plan
  is re-seeded underneath it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .plan import BUFFER_WEEKS, TOTAL_WEEKS, Plan, load_plan
from .timeutil import iso_week, today, week_start

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_revisions (
    version       TEXT PRIMARY KEY,
    seeded_at     TEXT NOT NULL,
    session_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_sessions (
    slug          TEXT NOT NULL,
    revision      TEXT NOT NULL REFERENCES plan_revisions(version),
    ordinal       INTEGER NOT NULL,
    phase         INTEGER NOT NULL,
    week_in_phase INTEGER NOT NULL,
    global_week   INTEGER NOT NULL,
    slot          INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    counts_as     TEXT NOT NULL,
    in_floor      INTEGER NOT NULL,
    cap_minutes   INTEGER NOT NULL,
    variant       TEXT,
    detail_json   TEXT NOT NULL,
    alternatives_json TEXT NOT NULL,
    retired_at    TEXT,
    PRIMARY KEY (slug, revision)
);
CREATE INDEX IF NOT EXISTS idx_sessions_revision_ordinal
    ON plan_sessions(revision, ordinal);

CREATE TABLE IF NOT EXISTS progress (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    plan_start         TEXT NOT NULL,
    plan_revision      TEXT NOT NULL,
    -- The plan week he is ON (1..48). This is the authoritative position.
    -- `pointer_slug` is a derived cache of the next unfinished session inside
    -- this week; the plan advances a week at a time, not a session at a time.
    plan_week          INTEGER NOT NULL DEFAULT 1,
    pointer_slug       TEXT,
    phase              INTEGER NOT NULL,
    phase_entry_date   TEXT NOT NULL,
    buffer_weeks       REAL NOT NULL,
    baseline_race_date TEXT NOT NULL,
    projected_race_date TEXT NOT NULL
);

-- Append-only. `voided_at` reverses an event; nothing is ever deleted.
CREATE TABLE IF NOT EXISTS session_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL,
    revision        TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- completed | skipped | swapped
    training_date   TEXT NOT NULL,          -- local date; what adherence counts
    completed_at    TEXT NOT NULL,          -- UTC timestamp; immutable
    variant         TEXT,
    substituted_from TEXT,                  -- slug this stood in for
    substitution_reason TEXT,               -- impact | voluntary
    duration_min    INTEGER,
    rpe             INTEGER,
    pain_score      INTEGER,
    pain_location   TEXT,
    skip_reason     TEXT,
    note            TEXT,
    idempotency_key TEXT UNIQUE,
    snapshot_json   TEXT NOT NULL,          -- frozen title/kind/detail
    actor           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    voided_at       TEXT,
    voided_by       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_training_date ON session_events(training_date);
CREATE INDEX IF NOT EXISTS idx_events_slug ON session_events(slug);

CREATE TABLE IF NOT EXISTS pause_windows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,               -- holiday | illness | injury
    start_date TEXT NOT NULL,
    end_date   TEXT,
    note       TEXT,
    created_at TEXT NOT NULL
);

-- Written once by the Monday evaluation, never recomputed.
CREATE TABLE IF NOT EXISTS week_outcomes (
    iso_week            TEXT PRIMARY KEY,
    -- Which plan week this calendar week was spent on. The two diverge every
    -- time a sub-floor week repeats, and that divergence is the clearest
    -- signal that he is stalling.
    plan_week           INTEGER NOT NULL DEFAULT 1,
    floor_required      INTEGER NOT NULL,
    sessions_counted    INTEGER NOT NULL,
    floor_met           INTEGER NOT NULL,
    status              TEXT NOT NULL,
    buffer_delta        REAL NOT NULL,
    buffer_after        REAL NOT NULL,
    projected_race_date TEXT NOT NULL,
    detail_json         TEXT NOT NULL,
    evaluated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmarks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle       INTEGER NOT NULL,
    iso_week    TEXT NOT NULL,
    test_key    TEXT NOT NULL,
    value       REAL NOT NULL,
    extra_json  TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    UNIQUE (cycle, test_key)
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES session_events(id),
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acknowledgements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,               -- pain_stop
    event_id   INTEGER REFERENCES session_events(id),
    actor      TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """Additive, idempotent. `CREATE TABLE IF NOT EXISTS` cannot add columns to a
    table that already exists, so live databases need this explicitly."""
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        return
    if row["version"] > SCHEMA_VERSION:
        raise RuntimeError(
            f"database is at schema {row['version']}, this code speaks {SCHEMA_VERSION}"
        )
    if row["version"] < 2:
        _migrate_to_week_based(conn)
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def _migrate_to_week_based(conn: sqlite3.Connection) -> None:
    """v1 tracked position as a plan-wide session pointer; v2 tracks a plan week.

    Backfilled from the pointer's own `global_week`, so an athlete mid-plan keeps
    his place rather than being sent back to week 1.
    """
    added = _add_column(conn, "progress", "plan_week", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "week_outcomes", "plan_week", "INTEGER NOT NULL DEFAULT 1")

    if not added:
        return

    progress = conn.execute("SELECT * FROM progress WHERE id = 1").fetchone()
    if progress is None or progress["pointer_slug"] is None:
        return

    row = conn.execute(
        "SELECT global_week FROM plan_sessions WHERE slug = ? AND revision = ?",
        (progress["pointer_slug"], progress["plan_revision"]),
    ).fetchone()
    if row is not None:
        conn.execute("UPDATE progress SET plan_week = ? WHERE id = 1", (row["global_week"],))


def _insert_sessions(conn: sqlite3.Connection, plan: Plan) -> None:
    conn.executemany(
        """
        INSERT INTO plan_sessions (
            slug, revision, ordinal, phase, week_in_phase, global_week, slot,
            kind, title, counts_as, in_floor, cap_minutes, variant,
            detail_json, alternatives_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                s.slug,
                plan.version,
                s.ordinal,
                s.phase,
                s.week_in_phase,
                s.global_week,
                s.slot,
                s.kind,
                s.title,
                s.counts_as,
                int(s.in_floor),
                s.cap_minutes,
                s.variant,
                json.dumps([dict(d) for d in s.detail]),
                json.dumps([{"slug": a.slug, "reason": a.reason} for a in s.alternatives]),
            )
            for s in plan.sessions
        ],
    )


def seed(conn: sqlite3.Connection, plan: Plan | None = None, plan_start: date | None = None) -> str:
    """Seed or re-seed the plan. Idempotent; safe with logs in flight.

    A changed YAML file yields a new content hash and therefore a new revision.
    Old revisions are retired rather than mutated, so `session_events` rows keep
    pointing at the template text that was actually shown to the athlete.
    """
    plan = plan or load_plan()
    now_iso = _now_iso()

    known = conn.execute(
        "SELECT version FROM plan_revisions WHERE version = ?", (plan.version,)
    ).fetchone()

    if known is None:
        conn.execute(
            "INSERT INTO plan_revisions (version, seeded_at, session_count) VALUES (?,?,?)",
            (plan.version, now_iso, len(plan.sessions)),
        )
        _insert_sessions(conn, plan)
        # Retire everything from prior revisions. The rows stay for history.
        conn.execute(
            "UPDATE plan_sessions SET retired_at = ? WHERE revision != ? AND retired_at IS NULL",
            (now_iso, plan.version),
        )

    progress = conn.execute("SELECT * FROM progress WHERE id = 1").fetchone()
    if progress is None:
        start = plan_start or week_start(today())
        baseline = start + timedelta(weeks=TOTAL_WEEKS)
        conn.execute(
            """
            INSERT INTO progress (
                id, plan_start, plan_revision, plan_week, pointer_slug, phase,
                phase_entry_date, buffer_weeks, baseline_race_date, projected_race_date
            ) VALUES (1,?,?,?,?,?,?,?,?,?)
            """,
            (
                start.isoformat(),
                plan.version,
                1,
                plan.sessions[0].slug,
                1,
                start.isoformat(),
                float(BUFFER_WEEKS),
                baseline.isoformat(),
                baseline.isoformat(),
            ),
        )
    elif progress["plan_revision"] != plan.version:
        _migrate_pointer(conn, progress, plan)

    return plan.version


def _migrate_pointer(conn: sqlite3.Connection, progress: sqlite3.Row, plan: Plan) -> None:
    """Move an in-flight athlete onto a new plan revision.

    If the slug he is standing on still exists, he keeps it. If the revision
    deleted it, he advances to the next surviving session rather than silently
    repeating or skipping work.
    """
    pointer = progress["pointer_slug"]
    if pointer is not None and plan.session(pointer) is None:
        old = conn.execute(
            "SELECT ordinal FROM plan_sessions WHERE slug = ? AND revision = ?",
            (pointer, progress["plan_revision"]),
        ).fetchone()
        old_ordinal = old["ordinal"] if old else 0
        survivor = next((s for s in plan.sessions if s.ordinal >= old_ordinal), None)
        pointer = survivor.slug if survivor else None

    conn.execute(
        "UPDATE progress SET plan_revision = ?, pointer_slug = ? WHERE id = 1",
        (plan.version, pointer),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_progress(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM progress WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("progress row missing -- seed() was never run")
    return row


def live_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All non-voided events, oldest first."""
    return list(
        conn.execute(
            "SELECT * FROM session_events WHERE voided_at IS NULL ORDER BY training_date, id"
        )
    )


def events_for_weeks(conn: sqlite3.Connection, iso_weeks: Iterable[str]) -> list[sqlite3.Row]:
    wanted = set(iso_weeks)
    if not wanted:
        return []
    # Recomputing ISO weeks in SQL would duplicate timeutil's rules, so the
    # filter happens in Python against training_date.
    rows = conn.execute(
        "SELECT * FROM session_events WHERE voided_at IS NULL ORDER BY training_date, id"
    ).fetchall()
    return [r for r in rows if iso_week(date.fromisoformat(r["training_date"])) in wanted]


def json_or(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return json.loads(value)
