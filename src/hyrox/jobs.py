"""Scheduled jobs: the Monday evaluation, reminders, digests and backups.

Run from cron on the Pi. Every job is idempotent, so a double-fire or a catch-up
run after downtime is harmless.

    0  3 * * 1   hyrox-job evaluate     # Monday 03:00 local: close last week
    0 18 * * 4   hyrox-job nudge        # Thursday evening: floor at risk?
    5  3 * * 1   hyrox-job digest       # Monday: weekly summary to the coach
    30 2 * * *   hyrox-job backup       # nightly
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from . import db, notify, service
from .config import Config, load_config
from .timeutil import iso_week, today, week_start

BACKUP_RETENTION_DAYS = 14


def _open(config: Config) -> sqlite3.Connection:
    conn = db.connect(config.db_path)
    db.migrate(conn)
    return conn


def evaluate(config: Config, reference: date | None = None) -> int:
    """Close every complete week that has no outcome yet."""
    conn = _open(config)
    outcomes = service.evaluate_closed_weeks(conn, reference=reference)
    service.advance_phase_if_ready(conn)

    consecutive = 0
    for outcome in reversed(outcomes):
        if outcome.floor_met:
            break
        consecutive += 1
    if consecutive >= 2:
        notify.alert_sub_floor(config.discord_webhook, weeks=consecutive)

    return len(outcomes)


def nudge(config: Config, reference: date | None = None) -> bool:
    """One nudge, when the floor is still reachable but at risk.

    Deliberately a single message rather than a stream. Nagging a lazy person
    daily trains them to ignore the app, which is the one outcome that kills it.
    """
    conn = _open(config)
    day = reference or today()
    strip = service.week_strip(conn, day)
    if strip["floor_met"]:
        return False
    if service.active_pain_stop(conn) is not None:
        return False
    pauses = service.load_pauses(conn)
    if any(p.covers(day) for p in pauses):
        return False
    return notify.nudge_athlete(config.discord_webhook, counted=strip["counted"], floor=strip["floor"])


def digest(config: Config, reference: date | None = None) -> bool:
    conn = _open(config)
    day = reference or today()
    label = iso_week(week_start(day) - timedelta(days=1))
    row = conn.execute("SELECT * FROM week_outcomes WHERE iso_week = ?", (label,)).fetchone()
    if row is None:
        return False
    return notify.weekly_digest(
        config.discord_webhook,
        iso_week=row["iso_week"],
        counted=row["sessions_counted"],
        floor=row["floor_required"],
        status=row["status"],
        buffer_weeks=row["buffer_after"],
        projected=row["projected_race_date"],
    )


def backup(config: Config) -> Path | None:
    """Nightly snapshot.

    The only valuable thing here is months of a person's training history, on an
    SD card. `sqlite3 .backup` rather than a file copy, because copying a live
    WAL database can yield a torn snapshot.
    """
    if config.backup_dir is None:
        return None
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    target = config.backup_dir / f"hyrox-{today().isoformat()}.sqlite"

    source = _open(config)
    destination = sqlite3.connect(target)
    try:
        with destination:
            source.backup(destination)
    finally:
        # Both must close. A lingering handle on the live database leaves a WAL
        # that a later restore would overwrite out from under, which SQLite
        # reports as a disk I/O error rather than anything legible.
        destination.close()
        source.close()

    cutoff = today() - timedelta(days=BACKUP_RETENTION_DAYS)
    for old in config.backup_dir.glob("hyrox-*.sqlite"):
        stamp = old.stem.removeprefix("hyrox-")
        try:
            if date.fromisoformat(stamp) < cutoff:
                old.unlink()
        except ValueError:
            continue
    return target


def restore(config: Config, snapshot: Path) -> None:
    """The other half of a backup. An untested restore path is not a backup."""
    if not snapshot.exists():
        raise SystemExit(f"no such snapshot: {snapshot}")
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        stale = config.db_path.with_name(config.db_path.name + suffix)
        stale.unlink(missing_ok=True)
    shutil.copy2(snapshot, config.db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hyrox-job")
    parser.add_argument("job", choices=["evaluate", "nudge", "digest", "backup", "restore"])
    parser.add_argument("--snapshot", type=Path, help="for restore")
    args = parser.parse_args(argv)

    config = load_config()

    if args.job == "evaluate":
        print(f"evaluated {evaluate(config)} week(s)")
    elif args.job == "nudge":
        print("nudged" if nudge(config) else "no nudge needed")
    elif args.job == "digest":
        print("digest sent" if digest(config) else "no closed week to report")
    elif args.job == "backup":
        target = backup(config)
        print(f"backed up to {target}" if target else "HYROX_BACKUP_DIR unset")
    elif args.job == "restore":
        if args.snapshot is None:
            parser.error("--snapshot is required for restore")
        restore(config, args.snapshot)
        print(f"restored from {args.snapshot}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
