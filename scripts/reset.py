#!/usr/bin/env python3
"""Reset the database.

    python scripts/reset.py            # empty: nothing logged, week 1 untouched
    python scripts/reset.py --demo     # a plausible first week, for screenshots

Default is empty, deliberately. Fabricated progress in a training log is worse
than an empty one -- it reads as a bug, and it quietly poisons the benchmark
trends the whole app is judged on.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="seed a sample first week")
    parser.add_argument("--start", help="plan start (ISO date, a Monday)")
    args = parser.parse_args()

    db_path = pathlib.Path(os.environ.get("HYROX_DB_PATH", "data/hyrox.sqlite"))
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

    from hyrox import db, service
    from hyrox.plan import load_plan
    from hyrox.timeutil import today, week_start

    plan = load_plan()
    start = date.fromisoformat(args.start) if args.start else week_start(today())

    conn = db.connect(db_path)
    db.migrate(conn)
    db.seed(conn, plan, plan_start=start)

    if args.demo:
        def done(day: date, **kw) -> None:
            nxt = service.current_session(conn, plan)
            service.log_session(
                conn, slug=nxt.session.slug, actor="dragos", training_date=day, plan=plan, **kw
            )

        done(start, rpe=6, duration_min=38, note="Felt heavier than it looked.")
        done(start.replace(day=start.day + 2), rpe=5, duration_min=32,
             pain_score=2, pain_location="ankle")
        done(start.replace(day=start.day + 4), rpe=7, duration_min=40)
        for key, first, second in [
            ("run_12min", 1800, 1980), ("thrusters", 15, 18),
            ("farmers", 60, 80), ("split_squat", 8, 10), ("body_mass", 89, 87.5),
        ]:
            service.record_benchmark(conn, cycle=1, test_key=key, value=first)
            service.record_benchmark(conn, cycle=2, test_key=key, value=second)

    nxt = service.current_session(conn, plan)
    logged = service.sessions_completed(conn)
    print(f"plan starts {start}, {len(plan.sessions)} sessions, {logged} logged")
    print(f"up next: {nxt.session.title}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
