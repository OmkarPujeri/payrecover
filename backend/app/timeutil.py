"""Time helpers shared across layers that disagree about what "naive" means.

The project has two legitimate timezone conventions and they meet at the
database:

*   The **agent/compliance layers think in IST.** NPCI peak hours, the TRAI
    messaging window, and "tomorrow morning at 7" are all Indian wall-clock
    concepts, so ``planner``/``compliance`` emit IST timestamps.
*   The **persistence and scheduling layers think in UTC**, because that is the
    only sane thing to store.

Mixing the two silently corrupts comparisons. Postgres would normalise an aware
timestamp on the way in, but SQLite — which the tests and the local demo run on
— stores what it is given and compares the results as *strings*. So an action
scheduled for ``09:00+05:30`` and a "now" of ``03:30+00:00`` are the same
instant and still compare unequal.

Hence: **normalise to UTC at every write to a datetime column, and at every
comparison against one.** ``to_utc`` is the single place that decides what a
naive datetime meant, so the assumption is stated once instead of implied in
four modules.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

__all__ = ["IST", "utcnow", "to_utc", "to_utc_from_ist", "parse_dt"]


def utcnow() -> datetime:
    """The current instant, UTC-aware."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime | None, *, naive_is: ZoneInfo | timezone = timezone.utc) -> datetime | None:
    """Convert to UTC. A naive value is assumed to be in ``naive_is``.

    Pass ``naive_is=IST`` when the value came from the agent or compliance
    layers, where a bare timestamp means Indian wall-clock time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_is)
    return dt.astimezone(timezone.utc)


def to_utc_from_ist(dt: datetime | None) -> datetime | None:
    """Convert to UTC, reading a naive value as IST."""
    return to_utc(dt, naive_is=IST)


def parse_dt(value: Any) -> datetime | None:
    """Best-effort ISO-8601 / datetime parse. Never raises."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
