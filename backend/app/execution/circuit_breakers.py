"""Circuit Breakers — CB-001..008 (PRD section 18).

Eight deterministic rules that HALT in-flight recovery when the world changes
underneath a decision. This is the safety net that sits *after* the Compliance
Engine, and the distinction between the two matters:

* **Compliance Engine** (``app.compliance.engine``) vets a *proposed* action
  before it runs — "may we do this?"
* **Circuit breakers** (this module) watch *persisted state* and stop work that
  is already queued or scheduled — "should we still be doing this at all?"

They deliberately overlap on three limits (retry cap, recovery window, cost
ceiling) because the two questions are asked at different times: compliance
prevents a bad action being *chosen*, a breaker cancels work already in flight
when the same limit is crossed by a later event.

    ID       Name                    Trigger                              Effect
    CB-001   Payment Recovered       recovery_status == recovered         cancel all pending
    CB-002   Dispute Raised          has_dispute                          halt (legal risk)
    CB-003   Customer Opt-Out        customer_opted_out                   halt comms
    CB-004   Subscription Cancelled  subscription_cancelled               halt
    CB-005   NPCI Retry Cap          recovery_attempts > 3                cancel pending RETRIES only
    CB-006   Max Recovery Window     days_since_failure > 14              escalate + cancel
    CB-007   Negative Economics      cost > 15% of order value            escalate + cancel
    CB-008   TRAI Timing             outside 09:00-20:00 IST              defer notifications

``evaluate_breakers`` is pure (a dict in, a verdict out) so all eight rules are
unit-testable offline with an injected ``now``; ``check_circuit_breakers`` is the
thin DB/SSE wrapper that logs the trip, cancels work, and notifies the dashboard.

Two design notes worth keeping:

*   **CB-005 fires on ``> 3``, not ``>= 3``.** ``recovery_attempts`` is
    incremented when a retry is *authorised*, so the value 3 means "three
    retries authorised" — exactly at the NPCI ceiling, not beyond it. Tripping at
    ``>= 3`` would cancel the very third retry the policy permits. Preventing a
    *fourth* is compliance rule NPCI-001's job (it blocks the proposal); this
    breaker is the backstop for state that drifted past the ceiling by another
    route. It also cancels retries *only* — per the PRD, hitting the cap leaves
    the case notification-only rather than closing it.
*   **CB-008 is not a halt.** Being outside the TRAI window is a *timing*
    constraint, not a reason to abandon recovery, so it is exposed separately as
    :func:`notification_window` and consumed by the executor to defer a
    notification to the next 9 AM IST.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import statuses
from app.models import CircuitBreakerEvent, RecoveryAction, RecoveryEvent
from app.sse import sse_manager

logger = logging.getLogger("payrecover.breakers")

IST = ZoneInfo("Asia/Kolkata")

MAX_RETRIES = 3
MAX_WINDOW_DAYS = 14
COST_CEILING_FRACTION = 0.15

# TRAI transactional/promotional messaging window (IST).
TRAI_START = time(9, 0)
TRAI_END = time(20, 0)

_RETRY_TOOL = "schedule_smart_retry"


@dataclass(frozen=True)
class BreakerTrip:
    """One tripped breaker — the binding verdict for this evaluation."""

    breaker_id: str            # CB-001 .. CB-007
    breaker_name: str
    trigger_type: str          # machine-readable trigger key
    reason: str                # human-readable, shown in the dashboard
    event_status: str | None = None      # recovery_status to set, if any
    only_cancel_types: tuple[str, ...] | None = None  # None = cancel everything
    details: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure evaluation
# --------------------------------------------------------------------------- #
def _to_ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _days_since_failure(ev: dict[str, Any], now_ist: datetime) -> float:
    explicit = ev.get("days_since_failure")
    if isinstance(explicit, (int, float)):
        return float(explicit)
    created = _parse_dt(ev.get("created_at"))
    if created is None:
        return 0.0
    return max(0.0, (now_ist - _to_ist(created)).total_seconds() / 86400.0)


def evaluate_breakers(
    ev: dict[str, Any], *, now: datetime | None = None
) -> BreakerTrip | None:
    """Return the first tripped breaker for an event dict, else ``None``.

    ``ev`` is the shape produced by :func:`app.ingest.event_to_dict`. Pure and
    side-effect free — ordered so good news (recovered) and legal risk (dispute)
    win over the economic limits.
    """
    now_ist = _to_ist(now or datetime.now(IST))

    # CB-001 — the payment came through. Stop everything, cheerfully.
    if ev.get("recovery_status") == statuses.EV_RECOVERED:
        return BreakerTrip(
            breaker_id="CB-001",
            breaker_name="Payment Recovered",
            trigger_type="payment_recovered",
            reason="Payment already recovered: cancelling all pending recovery actions.",
        )

    # CB-002 — a dispute is a legal process; automated dunning must not continue.
    if ev.get("has_dispute"):
        return BreakerTrip(
            breaker_id="CB-002",
            breaker_name="Dispute Raised",
            trigger_type="dispute_created",
            reason="Dispute raised on this payment: halting all recovery (legal risk).",
            event_status=statuses.EV_HALTED,
        )

    # CB-003 — the customer asked us to stop. Non-negotiable.
    if ev.get("customer_opted_out"):
        return BreakerTrip(
            breaker_id="CB-003",
            breaker_name="Customer Opt-Out",
            trigger_type="customer_opted_out",
            reason="Customer opted out of communications: closing the channel permanently.",
            event_status=statuses.EV_HALTED,
        )

    # CB-004 — no subscription, no reason to chase the payment.
    if ev.get("subscription_cancelled"):
        return BreakerTrip(
            breaker_id="CB-004",
            breaker_name="Subscription Cancelled",
            trigger_type="subscription_cancelled",
            reason="Subscription cancelled: the customer has left; stopping recovery.",
            event_status=statuses.EV_HALTED,
        )

    # CB-005 — past the NPCI ceiling: no more retries, but notifications may live.
    attempts = int(ev.get("recovery_attempts") or 0)
    if attempts > MAX_RETRIES:
        return BreakerTrip(
            breaker_id="CB-005",
            breaker_name="NPCI Retry Cap",
            trigger_type="retry_cap_exceeded",
            reason=(
                f"Retry cap exceeded ({attempts} authorised > {MAX_RETRIES} NPCI max): "
                "cancelling queued retries; recovery is notification-only from here."
            ),
            only_cancel_types=(_RETRY_TOOL,),
            details={"recovery_attempts": attempts, "max_retries": MAX_RETRIES},
        )

    # CB-006 — the recovery window has closed.
    days = _days_since_failure(ev, now_ist)
    if days > MAX_WINDOW_DAYS:
        return BreakerTrip(
            breaker_id="CB-006",
            breaker_name="Max Recovery Window",
            trigger_type="window_expired",
            reason=(
                f"Recovery window expired ({days:.0f} days > {MAX_WINDOW_DAYS} day max): "
                "closing automated recovery and escalating."
            ),
            event_status=statuses.EV_ESCALATED,
            details={"days_since_failure": round(days, 1)},
        )

    # CB-007 — chasing this payment now costs more than it is worth.
    amount = int(ev.get("amount") or 0)
    cost = int(ev.get("recovery_cost_paise") or 0)
    if amount > 0 and cost > amount * COST_CEILING_FRACTION:
        return BreakerTrip(
            breaker_id="CB-007",
            breaker_name="Negative Economics",
            trigger_type="negative_economics",
            reason=(
                f"Recovery cost (Rs {cost / 100:.2f}) exceeds 15% of the order "
                f"(Rs {amount / 100:.2f}); not worth pursuing, escalating."
            ),
            event_status=statuses.EV_ESCALATED,
            details={"recovery_cost_paise": cost, "amount": amount},
        )

    return None


def notification_window(
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    """CB-008 — may we message a customer right now?

    Returns ``(allowed, next_window_start)``. When outside the TRAI window the
    second element is the next 09:00 IST, so the caller can *defer* rather than
    drop the notification.
    """
    now_ist = _to_ist(now or datetime.now(IST))
    t = now_ist.time()
    if TRAI_START <= t <= TRAI_END:
        return True, None

    next_window = now_ist.replace(hour=TRAI_START.hour, minute=TRAI_START.minute,
                                  second=0, microsecond=0)
    if t > TRAI_END:
        next_window += timedelta(days=1)
    return False, next_window


# --------------------------------------------------------------------------- #
# Stateful wrapper: log the trip, cancel work, tell the dashboard
# --------------------------------------------------------------------------- #
async def cancel_pending_actions(
    session: AsyncSession,
    event_id: Any,
    *,
    exclude_action_id: Any = None,
    only_types: tuple[str, ...] | None = None,
    reason: str = "Cancelled by circuit breaker",
) -> int:
    """Cancel queued-but-not-in-flight actions for an event. Returns the count.

    ``exclude_action_id`` protects the action that triggered the check — a
    pre-flight breaker check must never cancel the very action being executed.
    ``only_types`` narrows the sweep to specific tools (used by CB-005, which
    kills retries but leaves notification work alone).
    """
    stmt = select(RecoveryAction).where(
        RecoveryAction.recovery_event_id == event_id,
        RecoveryAction.status.in_(tuple(statuses.CANCELLABLE_STATUSES)),
    )
    if only_types:
        stmt = stmt.where(RecoveryAction.action_type.in_(only_types))

    rows = list((await session.scalars(stmt)).all())
    cancelled = 0
    for action in rows:
        if exclude_action_id is not None and action.id == exclude_action_id:
            continue
        action.status = statuses.CANCELLED
        action.result = {**(action.result or {}), "cancelled_reason": reason}
        cancelled += 1
    return cancelled


async def check_circuit_breakers(
    session: AsyncSession,
    event: RecoveryEvent,
    *,
    now: datetime | None = None,
    exclude_action_id: Any = None,
    trigger_source: str | None = None,
    broadcast: bool = True,
) -> BreakerTrip | None:
    """Evaluate all breakers for ``event``; on a trip, halt and record it.

    Returns the :class:`BreakerTrip` that fired (recovery should stop), or
    ``None`` when nothing tripped. Side effects on a trip: cancel pending
    actions, insert a ``circuit_breaker_events`` audit row, advance the event's
    ``recovery_status`` when the breaker calls for it, and broadcast a
    ``circuit_breaker`` SSE frame.
    """
    # Imported here to avoid a circular import at module load
    # (ingest imports models; this module is imported by the webhook path).
    from app.ingest import event_to_dict

    trip = evaluate_breakers(event_to_dict(event), now=now)
    if trip is None:
        return None

    cancelled = await cancel_pending_actions(
        session,
        event.id,
        exclude_action_id=exclude_action_id,
        only_types=trip.only_cancel_types,
        reason=f"{trip.breaker_id} {trip.breaker_name}: {trip.reason}",
    )

    session.add(
        CircuitBreakerEvent(
            recovery_event_id=event.id,
            trigger_type=trip.trigger_type,
            trigger_id=trip.breaker_id,
            trigger_details={
                "name": trip.breaker_name,
                "reason": trip.reason,
                "source": trigger_source or "state_check",
                **trip.details,
            },
            cancelled_actions=cancelled,
        )
    )

    # Advance the event, but never walk back a final outcome.
    if trip.event_status and event.recovery_status not in statuses.EV_FINAL_STATUSES:
        event.recovery_status = trip.event_status
    event.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(event)

    logger.info(
        "Circuit breaker %s (%s) fired on event %s — %d action(s) cancelled",
        trip.breaker_id, trip.breaker_name, event.id, cancelled,
    )

    if broadcast:
        await sse_manager.broadcast(
            {
                "type": "circuit_breaker",
                "event_id": str(event.id),
                "breaker": trip.breaker_name,
                "breaker_id": trip.breaker_id,
                "trigger_type": trip.trigger_type,
                "reason": trip.reason,
                "cancelled_actions": cancelled,
                "event": event_to_dict(event),
            }
        )

    return trip
