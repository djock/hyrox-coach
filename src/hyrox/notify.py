"""Discord notifications.

The whole product targets someone who will not remember to train, and v1 of the
design had no reminder mechanism at all -- it waited passively for him to open a
URL. This is the fix, reusing the webhook pattern `milo_coach` already runs.

Best-effort throughout, exactly as milo does it: the log commits first, the
webhook fires afterwards and swallows its own failures. A Discord outage costs
the ping, never the data.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 5.0


def post(webhook: str | None, content: str) -> bool:
    if not webhook:
        return False
    try:
        response = httpx.post(webhook, json={"content": content}, timeout=TIMEOUT)
        response.raise_for_status()
        return True
    except Exception:  # noqa: BLE001 -- a notification must never break a write
        log.warning("discord webhook failed", exc_info=True)
        return False


def nudge_athlete(webhook: str | None, *, counted: int, floor: int) -> bool:
    """One nudge when the floor is at risk. One, not a stream."""
    missing = floor - counted
    return post(
        webhook,
        f"**{counted}/{floor} sessions this week.** "
        f"{missing} to go and three days left. Ten minutes counts — "
        f"you're allowed to stop after that.",
    )


def alert_pain(webhook: str | None, *, score: int, location: str) -> bool:
    return post(
        webhook,
        f"⚠️ **Pain {score}/10 at the {location}.** Training of that kind is "
        f"paused until you acknowledge it in the coach view.",
    )


def alert_sub_floor(webhook: str | None, *, weeks: int) -> bool:
    return post(
        webhook,
        f"📉 **{weeks} consecutive weeks below the floor.** Buffer is being spent.",
    )


def weekly_digest(
    webhook: str | None,
    *,
    iso_week: str,
    counted: int,
    floor: int,
    status: str,
    buffer_weeks: float,
    projected: str,
) -> bool:
    return post(
        webhook,
        f"**{iso_week}** — {counted}/{floor} sessions, status **{status}**. "
        f"Buffer {buffer_weeks:g} weeks. Projected race date {projected}.",
    )
