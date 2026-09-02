"""Execution Engine — the idempotent action executor (PRD section 17).

Stage 6 of the pipeline. The decision phase persisted a ``RecoveryAction``; this
module carries it out and records exactly what happened.

    Tool                        -> What actually runs
    generate_payment_link       -> Razorpay Payment Links  POST /v1/payment_links
    offer_alternative_method    -> Payment Link with a method hint
    send_recovery_notification  -> Payment-link notify, else a simulated send
    schedule_smart_retry        -> Razorpay Orders (retry order) + scheduler handoff
    escalate_to_merchant        -> internal state + dashboard
    mark_unrecoverable          -> internal state, case closed

Every Razorpay call goes through the key-optional ``razorpay_client``, so this
runs end-to-end with **zero credentials** (realistic simulated responses) and
switches to live API calls the moment keys appear. Nothing here needs a new key.

Three properties this module is built around:

* **Idempotency.** Executing the same action twice must never charge a customer
  twice or create a second payment link. A terminal or in-flight action is a
  no-op that reports why, so the pipeline, a HITL approval, the scheduler, and a
  manual ``POST /api/actions/{id}/execute`` can all race safely.
* **A pre-flight circuit-breaker check.** State can change between deciding and
  acting (the customer may have paid, or raised a dispute). We re-check CB-001..
  007 immediately before acting and abandon the action if recovery should stop.
* **Deferral over dropping.** A notification outside the TRAI window (CB-008) is
  re-scheduled for the next 9 AM IST rather than discarded.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import statuses
from app.execution.circuit_breakers import check_circuit_breakers, notification_window
from app.ingest import event_to_dict
from app.models import RecoveryAction, RecoveryEvent
from app.razorpay.client import razorpay_client
from app.sse import sse_manager
from app.timeutil import to_utc, to_utc_from_ist

logger = logging.getLogger("payrecover.executor")

# Tool names (mirrors app.agent.tools / strategy.planner).
GENERATE_PAYMENT_LINK = "generate_payment_link"
SCHEDULE_SMART_RETRY = "schedule_smart_retry"
SEND_RECOVERY_NOTIFICATION = "send_recovery_notification"
OFFER_ALTERNATIVE_METHOD = "offer_alternative_method"
ESCALATE_TO_MERCHANT = "escalate_to_merchant"
MARK_UNRECOVERABLE = "mark_unrecoverable"

#: Event ``recovery_status`` to set once a tool has executed successfully.
_EVENT_STATUS_AFTER: dict[str, str] = {
    GENERATE_PAYMENT_LINK: statuses.EV_IN_PROGRESS,
    SCHEDULE_SMART_RETRY: statuses.EV_IN_PROGRESS,
    SEND_RECOVERY_NOTIFICATION: statuses.EV_IN_PROGRESS,
    OFFER_ALTERNATIVE_METHOD: statuses.EV_IN_PROGRESS,
    ESCALATE_TO_MERCHANT: statuses.EV_ESCALATED,
    MARK_UNRECOVERABLE: statuses.EV_UNRECOVERABLE,
}

_LINK_TOOLS = (GENERATE_PAYMENT_LINK, OFFER_ALTERNATIVE_METHOD)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _aware(dt: datetime) -> datetime:
    """Treat a naive timestamp as UTC so comparisons never raise."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass
class Outcome:
    """What a tool handler produced."""

    result: dict[str, Any]
    status: str = statuses.COMPLETED
    scheduled_at: datetime | None = None
    extra_frames: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Tool handlers — each returns an Outcome; exceptions bubble to execute_action
# --------------------------------------------------------------------------- #
async def _handle_payment_link(
    session: AsyncSession,
    action: RecoveryAction,
    event: RecoveryEvent,
    params: dict[str, Any],
    now: datetime,
) -> Outcome:
    """Create a Razorpay Payment Link — the primary recovery instrument."""
    amount = int(params.get("amount_paise") or event.amount or 0)
    expiry_hours = int(params.get("expiry_hours") or 48)
    expire_by = int((now + timedelta(hours=expiry_hours)).timestamp())

    customer = {
        k: v
        for k, v in {
            "name": params.get("customer_name") or event.customer_name,
            "email": params.get("customer_email") or event.customer_email,
            "contact": params.get("customer_contact") or event.customer_contact,
        }.items()
        if v
    }
    notify = {
        "sms": bool(params.get("notify_sms")),
        "email": bool(params.get("notify_email")),
    }
    description = params.get("description") or f"Complete your payment: {event.failure_label or 'Payment Failed'}"

    notes = {
        "recovery_event_id": str(event.id),
        "razorpay_order_id": event.razorpay_order_id,
        "payrecover_action": action.action_type,
    }
    if action.action_type == OFFER_ALTERNATIVE_METHOD:
        # Method-specific link: Razorpay has no hard "only this method" flag on
        # links, so the hint travels in the notes + description where it is both
        # auditable and visible to the customer.
        suggested = params.get("suggested_method") or "upi"
        notes["preferred_method"] = suggested
        notes["original_method"] = params.get("original_method") or (event.payment_method or "")
        description = f"{description} · pay via {str(suggested).upper()}"

    link = await razorpay_client.create_payment_link(
        amount=amount,
        description=description,
        customer=customer or None,
        notify=notify,
        expire_by=expire_by,
        reminder_enable=True,
        notes=notes,
        currency=event.currency or "INR",
    )

    return Outcome(
        result={
            "payment_link_id": link.get("id"),
            "payment_link_url": link.get("short_url"),
            "amount_paise": amount,
            "expiry_hours": expiry_hours,
            "expire_by": expire_by,
            "notify": notify,
            "simulated": bool(link.get("_simulated")),
            "razorpay_response": link,
        }
    )


async def _handle_retry(
    session: AsyncSession,
    action: RecoveryAction,
    event: RecoveryEvent,
    params: dict[str, Any],
    now: datetime,
) -> Outcome:
    """Register a smart retry: create the retry order, hand timing to the scheduler.

    Executing a retry action means *arranging* the retry — a fresh Razorpay order
    the customer's payment can be reattempted against — and recording when it
    should fire. The attempt itself is fired later by
    ``app.execution.scheduler.run_due_actions``, which is what makes the cascade
    demo (retry fails differently -> agent pivots) reproducible on command.
    """
    # A retry time supplied by the agent/compliance layers is Indian wall-clock
    # time, so a naive value means IST — never UTC. Normalise before it is stored,
    # or the scheduler will read it back 5.5 hours out on SQLite.
    retry_at = to_utc_from_ist(_parse_dt(params.get("retry_at"))) or (now + timedelta(minutes=2))
    order = await razorpay_client.create_order(
        amount=int(event.amount or 0),
        currency=event.currency or "INR",
        receipt=f"retry_{event.razorpay_order_id}"[:40],
        notes={
            "recovery_event_id": str(event.id),
            "original_order_id": event.razorpay_order_id,
            "retry_attempt": str(event.recovery_attempts or 0),
            "retry_at": retry_at.isoformat(),
        },
    )

    return Outcome(
        result={
            "retry_order_id": order.get("id"),
            "retry_at": retry_at.isoformat(),
            "payment_method": params.get("payment_method") or "any",
            "attempt": int(event.recovery_attempts or 0),
            "simulated": bool(order.get("_simulated")),
            "razorpay_response": order,
        },
        status=statuses.SCHEDULED,
        scheduled_at=retry_at,
        extra_frames=[
            {
                "type": "retry_scheduled",
                "event_id": str(event.id),
                "action_id": str(action.id),
                "retry_at": retry_at.isoformat(),
                "retry_order_id": order.get("id"),
                "attempt": int(event.recovery_attempts or 0),
                "payment_method": params.get("payment_method") or "any",
            }
        ],
    )


async def _latest_payment_link(
    session: AsyncSession, event_id: Any
) -> dict[str, Any] | None:
    """The most recent successfully-created payment link for this event."""
    rows = (
        await session.scalars(
            select(RecoveryAction)
            .where(
                RecoveryAction.recovery_event_id == event_id,
                RecoveryAction.action_type.in_(_LINK_TOOLS),
                RecoveryAction.status == statuses.COMPLETED,
            )
            .order_by(RecoveryAction.created_at.desc())
        )
    ).all()
    for row in rows:
        result = row.result or {}
        if result.get("payment_link_id"):
            return result
    return None


async def _handle_notification(
    session: AsyncSession,
    action: RecoveryAction,
    event: RecoveryEvent,
    params: dict[str, Any],
    now: datetime,
) -> Outcome:
    """Send a recovery nudge.

    When a payment link already exists for this event we use Razorpay's own
    notify endpoint (real deliverability, real audit trail). Otherwise the send
    is *simulated and logged* — per PRD design decision #10, the judges care
    about the decision logic, not SMS delivery, and we will not wire a paid SMS
    vendor for a demo.
    """
    channel = (params.get("channel") or "email").lower()
    link = await _latest_payment_link(session, event.id)

    if link and link.get("payment_link_id"):
        medium = "sms" if channel == "sms" else "email"
        resp = await razorpay_client.notify_payment_link(
            link["payment_link_id"], medium=medium
        )
        return Outcome(
            result={
                "channel": medium,
                "delivery": "razorpay_payment_link_notify",
                "payment_link_id": link["payment_link_id"],
                "payment_link_url": link.get("payment_link_url"),
                "template_id": params.get("template_id"),
                "simulated": bool(resp.get("_simulated")),
                "razorpay_response": resp,
            }
        )

    logger.info(
        "Simulated %s notification to %s for event %s",
        channel, params.get("customer_contact") or event.customer_contact, event.id,
    )
    return Outcome(
        result={
            "channel": channel,
            "delivery": "simulated",
            "recipient": params.get("customer_contact") or event.customer_contact,
            "template_id": params.get("template_id"),
            "message": params.get("personalized_message") or params.get("reason"),
            "simulated": True,
        }
    )


async def _handle_escalate(
    session: AsyncSession,
    action: RecoveryAction,
    event: RecoveryEvent,
    params: dict[str, Any],
    now: datetime,
) -> Outcome:
    """Internal: surface the case to a human in the dashboard."""
    return Outcome(
        result={
            "status": "escalated",
            "severity": params.get("severity") or "medium",
            "recommended_action": params.get("recommended_action"),
            "visible_in_dashboard": True,
            "simulated": False,
        }
    )


async def _handle_unrecoverable(
    session: AsyncSession,
    action: RecoveryAction,
    event: RecoveryEvent,
    params: dict[str, Any],
    now: datetime,
) -> Outcome:
    """Internal: close the case."""
    return Outcome(
        result={
            "status": "closed",
            "closed_reason": params.get("reason"),
            "visible_in_dashboard": True,
            "simulated": False,
        }
    )


_HANDLERS = {
    GENERATE_PAYMENT_LINK: _handle_payment_link,
    OFFER_ALTERNATIVE_METHOD: _handle_payment_link,
    SCHEDULE_SMART_RETRY: _handle_retry,
    SEND_RECOVERY_NOTIFICATION: _handle_notification,
    ESCALATE_TO_MERCHANT: _handle_escalate,
    MARK_UNRECOVERABLE: _handle_unrecoverable,
}


# --------------------------------------------------------------------------- #
# The executor
# --------------------------------------------------------------------------- #
def _skip(action: RecoveryAction, reason: str, detail: str) -> dict[str, Any]:
    return {
        "executed": False,
        "action_id": str(action.id),
        "action_type": action.action_type,
        "status": action.status,
        "reason": reason,
        "detail": detail,
    }


async def execute_action(
    session: AsyncSession,
    action: RecoveryAction,
    *,
    event: RecoveryEvent | None = None,
    now: datetime | None = None,
    force: bool = False,
    ignore_defer: bool = False,
) -> dict[str, Any]:
    """Execute one persisted ``RecoveryAction``. Safe to call more than once.

    ``force`` executes an action that is not in ``approved`` (used by HITL
    approve/modify and by the scheduler firing a deferred notification).

    ``ignore_defer`` skips the TRAI messaging-window check. Only the manual
    ``POST /api/actions/{id}/execute`` sets it — an explicit merchant override,
    logged as such. The scheduler deliberately does **not**: an action deferred
    to 09:00 that only gets picked up at 22:00 must be re-checked, or a late tick
    becomes a TRAI breach.

    Returns a dict describing what happened — ``executed`` tells you whether
    real work occurred, and ``reason`` explains any no-op.
    """
    now = _aware(now or _utcnow())

    # 1) Idempotency + eligibility.
    if action.status in statuses.TERMINAL_STATUSES:
        return _skip(action, "already_final", f"Action is already {action.status}.")
    if action.status == statuses.EXECUTING:
        return _skip(action, "in_flight", "Action is already executing.")
    if action.status == statuses.SCHEDULED and not force:
        return _skip(action, "already_scheduled", "Action is scheduled for later.")
    if action.status not in statuses.EXECUTABLE_STATUSES and not force:
        return _skip(
            action,
            "not_approved",
            f"Action status {action.status!r} is not cleared for execution.",
        )

    if event is None:
        event = await session.get(RecoveryEvent, action.recovery_event_id)
    if event is None:
        return _skip(action, "event_missing", "Parent recovery event not found.")

    handler = _HANDLERS.get(action.action_type)
    if handler is None:
        return _skip(
            action, "unknown_tool", f"No executor for tool {action.action_type!r}."
        )

    # 2) Pre-flight circuit breakers — state may have moved since we decided.
    trip = await check_circuit_breakers(
        session, event, now=now, exclude_action_id=action.id, trigger_source="pre_execution"
    )
    if trip is not None:
        action.status = statuses.CANCELLED
        action.result = {
            **(action.result or {}),
            "cancelled_reason": f"{trip.breaker_id} {trip.breaker_name}: {trip.reason}",
        }
        await session.commit()
        await session.refresh(action)
        return {
            **_skip(action, "circuit_breaker", trip.reason),
            "breaker_id": trip.breaker_id,
            "breaker": trip.breaker_name,
        }

    params = dict(action.action_params or {})

    # 3) CB-008 / TRAI deferral — queue a notification instead of dropping it.
    if action.action_type == SEND_RECOVERY_NOTIFICATION and not ignore_defer:
        allowed, next_window = notification_window(now)
        # A send time from the agent/compliance layers is Indian wall-clock time,
        # so read a naive value as IST — and read it that way *once*, for both the
        # comparison and the store, or the two disagree by 5.5 hours.
        explicit = to_utc_from_ist(_parse_dt(params.get("scheduled_at")))
        defer_to = None
        defer_reason = None
        breaker_id = None
        if not allowed and next_window is not None:
            defer_to = next_window
            defer_reason = "CB-008 TRAI messaging window is 09:00-20:00 IST"
            breaker_id = "CB-008"
        elif explicit is not None and explicit > now:
            # Not a breaker: the action simply isn't due yet. Attributing this to
            # CB-008 would put a compliance trip in the audit trail that never
            # happened, so it gets its own reason.
            defer_to = explicit
            defer_reason = "Not yet due: waiting for the agent's chosen send time"

        if defer_to is not None:
            action.status = statuses.SCHEDULED
            action.scheduled_at = to_utc(defer_to)
            action.result = {
                **(action.result or {}),
                "deferred_to": defer_to.isoformat(),
                "deferred_reason": defer_reason,
            }
            await session.commit()
            await session.refresh(action)
            await sse_manager.broadcast(
                {
                    "type": "action_deferred",
                    "event_id": str(event.id),
                    "action_id": str(action.id),
                    "action_type": action.action_type,
                    "deferred_to": defer_to.isoformat(),
                    "breaker_id": breaker_id,
                    "reason": defer_reason,
                }
            )
            return {
                "executed": False,
                "action_id": str(action.id),
                "action_type": action.action_type,
                "status": action.status,
                "reason": "deferred",
                "detail": f"Deferred to {defer_to.isoformat()}: {defer_reason}.",
                "scheduled_at": defer_to.isoformat(),
            }

    # 4) Mark in-flight so a concurrent caller sees it and backs off.
    action.status = statuses.EXECUTING
    await session.commit()

    # 5) Dispatch.
    try:
        outcome = await handler(session, action, event, params, now)
    except Exception as exc:  # noqa: BLE001 — record the failure, never crash the pipeline
        logger.exception("Execution failed for action %s (%s)", action.id, action.action_type)
        action.status = statuses.FAILED
        action.executed_at = to_utc(now)
        action.result = {
            **(action.result or {}),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        await session.commit()
        await session.refresh(action)
        await sse_manager.broadcast(
            {
                "type": "action_failed",
                "event_id": str(event.id),
                "action_id": str(action.id),
                "action_type": action.action_type,
                "error": str(exc),
            }
        )
        return {
            "executed": False,
            "action_id": str(action.id),
            "action_type": action.action_type,
            "status": statuses.FAILED,
            "reason": "error",
            "detail": str(exc),
        }

    # 6) Persist success + advance the event.
    action.status = outcome.status
    action.executed_at = to_utc(now)
    action.scheduled_at = to_utc(outcome.scheduled_at) or action.scheduled_at
    action.result = {**(action.result or {}), **outcome.result, "executed_at": now.isoformat()}

    next_event_status = _EVENT_STATUS_AFTER.get(action.action_type)
    if next_event_status and event.recovery_status not in statuses.EV_FINAL_STATUSES:
        event.recovery_status = next_event_status
    event.updated_at = _utcnow()

    await session.commit()
    await session.refresh(action)
    await session.refresh(event)

    logger.info(
        "Executed %s for event %s -> %s", action.action_type, event.id, action.status
    )

    payload = {
        "executed": True,
        "action_id": str(action.id),
        "event_id": str(event.id),
        "action_type": action.action_type,
        "status": action.status,
        "result": outcome.result,
        "scheduled_at": action.scheduled_at.isoformat() if action.scheduled_at else None,
        "executed_at": now.isoformat(),
        "razorpay_mode": razorpay_client.mode,
    }

    await sse_manager.broadcast(
        {
            "type": "action_executed",
            "event_id": str(event.id),
            "action_id": str(action.id),
            "action": action.action_type,
            "status": action.status,
            "result": outcome.result,
            "razorpay_mode": razorpay_client.mode,
            "event": event_to_dict(event),
        }
    )
    for frame in outcome.extra_frames:
        await sse_manager.broadcast(frame)

    return payload


async def execute_action_by_id(
    session: AsyncSession, action_id: Any, *, now: datetime | None = None, force: bool = False
) -> dict[str, Any] | None:
    """Convenience wrapper: load an action by primary key, then execute it.

    Accepts the id as a ``uuid.UUID`` *or* as its string form, because the two
    live on opposite sides of the JSON boundary. ``recover_event`` returns
    ``action_id`` as a string (it goes out over HTTP and SSE, which cannot carry
    a ``UUID``), while SQLAlchemy's ``Uuid`` column type does ``value.hex`` on
    whatever it is handed and raises ``AttributeError`` on a ``str``. Coercing
    here keeps that one conversion in a single place instead of asking every
    caller to remember it.

    Returns ``None`` when there is no such action — including when the id is not
    a well-formed UUID, since "no row matches this" is the same outcome for the
    caller and a malformed id should 404, not 500.
    """
    if isinstance(action_id, str):
        try:
            action_id = uuid.UUID(action_id)
        except ValueError:
            logger.warning("execute_action_by_id got a malformed id: %r", action_id)
            return None
    action = await session.get(RecoveryAction, action_id)
    if action is None:
        return None
    return await execute_action(session, action, now=now, force=force)
