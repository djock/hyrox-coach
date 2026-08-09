"""Environment-driven configuration.

Everything the app needs to run comes from the environment so the container and
the local dev loop are configured identically. Defaults are chosen so that
`uvicorn --factory hyrox.app:create_app` works with no environment at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

# All week logic is local-time. A fixed zone rather than the host's, so the Pi
# and the laptop agree about which ISO week a 23:30 session belongs to.
TIMEZONE = ZoneInfo("Europe/Bucharest")

ROLE_ATHLETE = "athlete"
ROLE_COACH = "coach"


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str
    role: str


@dataclass(frozen=True)
class Config:
    db_path: Path
    session_secret: str
    session_version: int
    users: tuple[User, ...]
    discord_webhook: str | None
    backup_dir: Path | None
    plan_start: date | None
    dev_mode: bool = False
    _by_name: dict[str, User] = field(default_factory=dict, compare=False)

    def user(self, username: str) -> User | None:
        return self._by_name.get(username)

    @property
    def athlete(self) -> User | None:
        return next((u for u in self.users if u.role == ROLE_ATHLETE), None)


def _parse_users(raw: str) -> tuple[User, ...]:
    """Parse `name:bcrypt-hash:role` triples.

    bcrypt hashes contain `$` and `.` but never `:`, so a plain split is safe.
    """
    users: list[User] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError(f"HYROX_USERS entry is not name:hash:role -- {entry!r}")
        username, password_hash, role = (p.strip() for p in parts)
        if role not in (ROLE_ATHLETE, ROLE_COACH):
            raise ValueError(f"unknown role {role!r} for user {username!r}")
        users.append(User(username, password_hash, role))
    return tuple(users)


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    secret = env.get("HYROX_SESSION_SECRET", "")
    dev_mode = not secret
    if dev_mode:
        # Local dev convenience only. A restart invalidates sessions, which is
        # exactly what you want when there is no configured secret.
        secret = "dev-insecure-secret"

    plan_start = env.get("HYROX_PLAN_START", "").strip()
    backup_dir = env.get("HYROX_BACKUP_DIR", "").strip()

    users = _parse_users(env.get("HYROX_USERS", ""))
    return Config(
        db_path=Path(env.get("HYROX_DB_PATH", "data/hyrox.sqlite")),
        session_secret=secret,
        session_version=int(env.get("HYROX_SESSION_VERSION", "1")),
        users=users,
        discord_webhook=env.get("HYROX_DISCORD_WEBHOOK") or None,
        backup_dir=Path(backup_dir) if backup_dir else None,
        plan_start=date.fromisoformat(plan_start) if plan_start else None,
        dev_mode=dev_mode,
        _by_name={u.username: u for u in users},
    )
