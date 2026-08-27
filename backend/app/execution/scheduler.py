"""Retry scheduler — APScheduler driver over a deterministic core.

Two layers, deliberately separated:

* :func:`run_due_actions` is the **deterministic core**: "given this instant,
  which scheduled actions are due, and fire them." It takes an explicit ``now``,
  touches nothing else, and is what both the demo and the tests call. That makes
  the PRD's cascade journey (section 19: retry fires -> fails differently ->
  agent pivots to a payment link -> customer pays -> circuit breaker recovers)
  reproducible **on command** instead of depending on wall-clock timing. Nobody
  waits 30 minutes on stage, and no test goes flaky.
* :class:`RetryScheduler` is the **production driver**: an APScheduler
  ``AsyncIOScheduler`` that calls that same core on an interval. This is what
  makes the timing real outside a demo — the agent genuinely schedules a retry
  for 7 AM and it genuinely fires at 7 AM.

APScheduler is imported lazily inside :meth:`RetryScheduler.start` (mirroring the
lazy ``razorpay`` / ``groq`` imports) so the module stays importable — and the
deterministic core stays runnable — even where the dependency is absent.

"Firing" a retry means the reattempt happened; what comes back is a *new*
webhook: ``payment.captured`` (CB-001 recovers the case) or another
``payment.failed`` carrying a different error, which re-enters the pipeline and
lets the agent pivot. That is why firing a retry does not itself try to charge
anyone — Razorpay owns that side, and in simulation there is nothing to charge.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.execution import statuses
from app.execution.circuit_breakers import check_circuit_breakers
from app.execution.executor import SCHEDULE_SMART_RETRY, execute_action
from app.models import RecoveryAction, RecoveryEvent
from app.sse import sse_manager

logger = logging.getLogger("payrecover.scheduler")

#: How often the background driver looks for due work.
TICK_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _due_actions(
    session: AsyncSession, now: datetime, limit: int
) -> list[RecoveryAction]:
    """Scheduled actions whose time has come, oldest first."""
    rows = (
        await session.scalars(
            select(RecoveryAction)
            .where(
                RecoveryAction.status == statuses.SCHEDULED,
                RecoveryAction.scheduled_at.is_not(None),
            )
            .order_by(RecoveryAction.scheduled_at.asc())
        )
    ).all()
    due = [a for a in rows if a.scheduled_at and _aware(a.scheduled_at) <= now]
    return due[:limit]


async def _fire_retry(
    session: AsyncSession,
    action: RecoveryAction,
    event: RecoveryEvent,
    now: datetime,
) -> dict[str, Any]:
    """Mark a scheduled retry as having fired and announce it.

    The reattempt's *outcome* arrives asynchronously as a new webhook, so this
    closes out the scheduling action and leaves the case in progress.
    """
    action.status = statuses.COMPLETED
    action.result = {
        **(action.result or {}),
        "retry_fired_at": now.isoformat(),
        "outcome": "awaiting_payment_result",
    }
    event.updated_at = _utcnow()
    await session.commit()
    await session.refresh(action)

    frame = {
        "type": "retry_fired",
        "event_id": str(event.id),
        "action_id": str(action.id),
        "action_type": action.action_type,
        "fired_at": now.isoformat(),
        "retry_order_id": (action.result or {}).get("retry_order_id"),
        "attempt": (action.result or {}).get("attempt"),
    }
    await sse_manager.broadcast(frame)
    logger.info("Retry fired for event %s (action %s)", event.id, action.id)

    return {
        "action_id": str(action.id),
        "event_id": str(event.id),
        "action_type": action.action_type,
        "fired": True,
        "status": action.status,
    }


async def run_due_actions(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fire every scheduled action that is due at ``now``. The deterministic core.

    Handles both kinds of scheduled work:

    * **Retries** — closed out as fired (see :func:`_fire_retry`).
    * **Deferred notifications** (CB-008/TRAI) — handed back to the executor,
      which re-checks the messaging window before sending.

    Each action is re-checked against the circuit breakers first, so a payment
    that succeeded (or a dispute raised) between scheduling and firing cancels
    the work instead of nagging a customer who already paid.
    """
    now = _aware(now or _utcnow())
    fired: list[dict[str, Any]] = []

    for action in await _due_actions(session, now, limit):
        event = await session.get(RecoveryEvent, action.recovery_event_id)
        if event is None:
            continue

        trip = await check_circuit_breakers(
            session,
            event,
            now=now,
            exclude_action_id=action.id,
            trigger_source="scheduler",
        )
        if trip is not None:
            action.status = statuses.CANCELLED
            action.result = {
                **(action.result or {}),
                "cancelled_reason": f"{trip.breaker_id} {trip.breaker_name}: {trip.reason}",
            }
            await session.commit()
            fired.append(
                {
                    "action_id": str(action.id),
                    "event_id": str(event.id),
                    "action_type": action.action_type,
                    "fired": False,
                    "status": action.status,
                    "breaker_id": trip.breaker_id,
                    "reason": trip.reason,
                }
            )
            continue

        if action.action_type == SCHEDULE_SMART_RETRY:
            fired.append(await _fire_retry(session, action, event, now))
        else:
            # A deferred notification: send it for real this time.
            #
            # We deliberately do NOT pass ignore_defer here. The window check
            # runs again against ``now``, which matters when a tick is late (the
            # server was down overnight, or someone fast-forwards the clock):
            # firing a 09:00 notification at 22:00 would breach TRAI. Re-checking
            # cannot loop, because a deferral always targets the start of a legal
            # window — at worst the send slides to the next morning.
            result = await execute_action(
                session, action, event=event, now=now, force=True
            )
            fired.append(
                {
                    "action_id": str(action.id),
                    "event_id": str(event.id),
                    "action_type": action.action_type,
                    "fired": bool(result.get("executed")),
                    "status": result.get("status"),
                    "reason": result.get("reason"),
                }
            )

    return fired


class RetryScheduler:
    """APScheduler-backed driver that periodically runs the due-action core."""

    def __init__(self, tick_seconds: int = TICK_SECONDS) -> None:
        self.tick_seconds = tick_seconds
        self._scheduler: Any = None

    @property
    def running(self) -> bool:
        return bool(self._scheduler and getattr(self._scheduler, "running", False))

    async def _tick(self) -> None:
        """One scheduler beat, with its own session (jobs run outside requests)."""
        try:
            async with AsyncSessionLocal() as session:
                fired = await run_due_actions(session)
            if fired:
                logger.info("Scheduler tick fired %d due action(s)", len(fired))
        except Exception:  # noqa: BLE001 — a bad tick must not kill the scheduler
            logger.exception("Scheduler tick failed")

    def start(self) -> bool:
        """Start the background driver. Returns False if APScheduler is absent.

        A missing dependency is not fatal: the deterministic core still runs via
        ``POST /api/simulator/run-due-actions``, so the system stays demoable.
        """
        if self.running:
            return True
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except Exception:  # noqa: BLE001
            logger.warning(
                "APScheduler unavailable — timed retries disabled; "
                "use POST /api/simulator/run-due-actions to fire due work."
            )
            return False

        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(
            self._tick,
            trigger="interval",
            seconds=self.tick_seconds,
            id="payrecover_due_actions",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info("Retry scheduler started (every %ds)", self.tick_seconds)
        return True

    def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
                logger.info("Retry scheduler stopped")
            except Exception:  # noqa: BLE001
                logger.warning("Retry scheduler shutdown raised", exc_info=True)
            finally:
                self._scheduler = None


#: App-wide singleton, started/stopped by the FastAPI lifespan.
retry_scheduler = RetryScheduler()
