from __future__ import annotations

from datetime import date, timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient

from hyrox import db, service
from hyrox.app import create_app
from hyrox.config import load_config
from hyrox.plan import load_plan

# A Monday, so ISO weeks line up predictably in every test.
PLAN_START = date(2026, 8, 3)

ATHLETE_PW = "dragos-pw"
COACH_PW = "ionut-pw"


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture
def config(tmp_path):
    users = (
        f"dragos:{_hash(ATHLETE_PW)}:athlete,"
        f"ionut:{_hash(COACH_PW)}:coach"
    )
    return load_config(
        {
            "HYROX_DB_PATH": str(tmp_path / "hyrox.sqlite"),
            "HYROX_SESSION_SECRET": "test-secret",
            "HYROX_SESSION_VERSION": "1",
            "HYROX_USERS": users,
            "HYROX_PLAN_START": PLAN_START.isoformat(),
            "HYROX_BACKUP_DIR": str(tmp_path / "backups"),
        }
    )


@pytest.fixture
def plan():
    return load_plan()


@pytest.fixture
def conn(config, plan):
    connection = db.connect(config.db_path)
    db.migrate(connection)
    db.seed(connection, plan, plan_start=PLAN_START)
    return connection


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    # https, because session cookies are issued Secure outside dev mode and a
    # plain-http test client would silently drop them.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def athlete(client):
    client.post("/login", data={"username": "dragos", "password": ATHLETE_PW})
    return client


@pytest.fixture
def coach(app):
    with TestClient(app, base_url="https://testserver") as c:
        c.post("/login", data={"username": "ionut", "password": COACH_PW})
        yield c


def csrf_for(client) -> str:
    """Pull the CSRF token out of whatever page the client can see."""
    import re

    page = client.get("/", follow_redirects=True).text
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "no csrf token on page"
    return match.group(1)


def complete_days(conn, plan, days: list[date], actor: str = "dragos") -> None:
    """Log one session per given day, walking the queue forward."""
    for day in days:
        up_next = service.current_session(conn, plan)
        assert up_next is not None
        service.log_session(
            conn, slug=up_next.session.slug, actor=actor, training_date=day, plan=plan
        )


def week_of(start: date, offset_weeks: int = 0) -> list[date]:
    monday = start + timedelta(weeks=offset_weeks)
    return [monday + timedelta(days=i) for i in range(7)]
