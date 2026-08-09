"""Sessions, roles and CSRF.

Two accounts, two roles, one login ever. Any repeated auth step is adherence
friction for the athlete, so the cookie is long-lived and he adds the PWA to his
home screen once.

`samesite=lax` alone is not a CSRF design, so every write endpoint carries a
signed per-session token. The cookie also carries a session *version*: bumping
`HYROX_SESSION_VERSION` invalidates every issued cookie, which is the only
revocation story two static accounts need.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

import bcrypt
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import ROLE_ATHLETE, ROLE_COACH, Config

COOKIE_NAME = "hyrox_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # a year; he should never see a login twice
CSRF_FIELD = "csrf_token"
_CSRF_SALT = "hyrox-csrf"
_SESSION_SALT = "hyrox-session"


@dataclass(frozen=True)
class Principal:
    username: str
    role: str

    @property
    def is_coach(self) -> bool:
        return self.role == ROLE_COACH

    @property
    def is_athlete(self) -> bool:
        return self.role == ROLE_ATHLETE


def _serializer(config: Config, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret, salt=salt)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        # A malformed hash in configuration must not look like a valid login.
        return False


def issue_cookie(config: Config, username: str) -> str:
    return _serializer(config, _SESSION_SALT).dumps(
        {"u": username, "v": config.session_version}
    )


def read_cookie(config: Config, raw: str | None) -> Principal | None:
    if not raw:
        return None
    try:
        data = _serializer(config, _SESSION_SALT).loads(raw, max_age=COOKIE_MAX_AGE)
    except BadSignature:
        return None
    if data.get("v") != config.session_version:
        return None
    user = config.user(data.get("u", ""))
    if user is None:
        return None
    return Principal(user.username, user.role)


def issue_csrf(config: Config, principal: Principal) -> str:
    return _serializer(config, _CSRF_SALT).dumps(principal.username)


def check_csrf(config: Config, principal: Principal, token: str | None) -> bool:
    if not token:
        return False
    try:
        owner = _serializer(config, _CSRF_SALT).loads(token, max_age=COOKIE_MAX_AGE)
    except BadSignature:
        return False
    return hmac.compare_digest(owner, principal.username)


def current_principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def require(request: Request, *roles: str) -> Principal:
    principal = current_principal(request)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in")
    if roles and principal.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed")
    return principal


def require_athlete(request: Request) -> Principal:
    return require(request, ROLE_ATHLETE)


def require_coach(request: Request) -> Principal:
    return require(request, ROLE_COACH)


def require_any(request: Request) -> Principal:
    return require(request)
