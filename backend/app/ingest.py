"""Ingestion service — shared by the webhook router and the simulator.

Turns a Razorpay *payment entity* into a persisted ``RecoveryEvent`` (with
dedup), applies circuit-breaker-style webhook events (captured, dispute, …)
to existing events, and serialises events for SSE / API responses.

Keeping this here avoids duplicating logic between real webhooks and
simulator-injected events — both funnel through the same code path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import statuses
from app.models import RecoveryEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Opt-out signals. Razorpay itself does not emit one — an unsubscribe arrives
#: from the merchant's own STOP handler or a DLT/TRAI consent feed — so we accept
#: it on the same ingestion path under a first-party event name. Without this,
#: CB-003 (Customer Opt-Out) would be unreachable in a demo, and "we honour opt
#: outs" would be an untested claim.
OPT_OUT_EVENTS = frozenset({"customer.opted_out", "customer.unsubscribed"})


def parse_payment_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Extract the columns we care about from a Razorpay payment entity."""
    notes = entity.get("notes") or {}
    return {
        "razorpay_payment_id": entity.get("id") or "",
        "razorpay_order_id": entity.get("order_id") or "",
        "amount": int(entity.get("amount") or 0),
        "currency": entity.get("currency") or "INR",
        "payment_method": entity.get("method"),
        "customer_email": entity.get("email"),
        "customer_contact": entity.get("contact"),
        "customer_name": entity.get("name") or notes.get("customer_name"),
        "error_code": entity.get("error_code"),
        "error_source": entity.get("error_source"),
        "error_step": entity.get("error_step"),
        "error_reason": entity.get("error_reason"),
        "error_description": entity.get("error_description"),
    }


async def ingest_failure(
    session: AsyncSession,
    entity: dict[str, Any],
    *,
    event_type: str = "payment.failed",
    is_simulated: bool = False,
    cascade_group_id: str | None = None,
    customer_dnd: bool = False,
) -> tuple[RecoveryEvent, bool]:
    """Persist a failed-payment event. Returns (event, created).

    Dedups on (payment_id, event_type) so redelivered webhooks are idempotent.
    """
    fields = parse_payment_entity(entity)
    pid = fields["razorpay_payment_id"]

    if pid:
        existing = await session.scalar(
            select(RecoveryEvent).where(
                RecoveryEvent.razorpay_payment_id == pid,
                RecoveryEvent.event_type == event_type,
            )
        )
        if existing is not None:
            return existing, False

    event = RecoveryEvent(
        event_type=event_type,
        is_simulated=is_simulated,
        cascade_group_id=cascade_group_id,
        customer_dnd=customer_dnd,
        recovery_status="pending",
        **fields,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event, True


# Webhook events that flip the state a circuit breaker watches. This function
# only records *what happened*; deciding what to do about it (cancel queued
# work, halt the case, log the trip) is ``execution.circuit_breakers``, which the
# webhook router calls immediately afterwards. Keeping the two apart means the
# breaker engine has exactly one implementation, whether the trigger arrives by
# webhook, by scheduler tick, or as a pre-flight check before executing.
async def apply_circuit_event(
    session: AsyncSession, event_type: str, entity: dict[str, Any]
) -> list[RecoveryEvent]:
    order_id = entity.get("order_id") or ""
    payment_id = entity.get("id") or ""
    contact = entity.get("contact") or ""
    email = entity.get("email") or ""

    stmt = select(RecoveryEvent)
    if order_id:
        stmt = stmt.where(RecoveryEvent.razorpay_order_id == order_id)
    elif payment_id:
        stmt = stmt.where(RecoveryEvent.razorpay_payment_id == payment_id)
    elif event_type in OPT_OUT_EVENTS and (contact or email):
        # An opt-out is about a *person*, not one payment: it must close the
        # channel on every open case for that customer, not just the one that
        # happened to carry the identifier.
        stmt = stmt.where(
            RecoveryEvent.customer_contact == contact
            if contact
            else RecoveryEvent.customer_email == email
        )
    else:
        return []

    # Oldest first, so "the first row" below is the original failure, not
    # whichever row the query happened to return first.
    stmt = stmt.order_by(RecoveryEvent.created_at.asc(), RecoveryEvent.id.asc())
    matches = list((await session.scalars(stmt)).all())
    if not matches:
        return []

    for i, ev in enumerate(matches):
        if event_type in ("payment.captured", "order.paid", "invoice.paid"):
            ev.recovery_status = statuses.EV_RECOVERED
            # One capture is one payment. When several failure rows share an
            # order (a retry that failed again — the cascade journey), the
            # money still came back ONCE, so only the original row carries the
            # recovered amount; the follow-up rows resolve at zero. Assigning
            # the full amount per row would multiply recovered cash by the
            # number of failed attempts on the order.
            ev.recovered_amount = (
                int(entity.get("amount") or ev.amount) if i == 0 else 0
            )
            ev.recovered_at = _utcnow()
        elif event_type == "payment.dispute.created":
            ev.has_dispute = True
        elif event_type == "subscription.cancelled":
            ev.subscription_cancelled = True
        elif event_type in OPT_OUT_EVENTS:
            ev.customer_opted_out = True
        elif event_type in ("refund.created",):
            ev.recovery_status = statuses.EV_UNRECOVERABLE
        ev.updated_at = _utcnow()

    await session.commit()
    for ev in matches:
        await session.refresh(ev)
    return matches


def event_to_dict(ev: RecoveryEvent) -> dict[str, Any]:
    """Serialise a RecoveryEvent for SSE frames and API responses."""
    return {
        "id": str(ev.id),
        "razorpay_payment_id": ev.razorpay_payment_id,
        "razorpay_order_id": ev.razorpay_order_id,
        "event_type": ev.event_type,
        "amount": ev.amount,
        "amount_inr": round(ev.amount / 100, 2),
        "currency": ev.currency,
        "error_code": ev.error_code,
        "error_source": ev.error_source,
        "error_step": ev.error_step,
        "error_reason": ev.error_reason,
        "error_description": ev.error_description,
        "customer_name": ev.customer_name,
        "customer_email": ev.customer_email,
        "customer_contact": ev.customer_contact,
        "payment_method": ev.payment_method,
        "customer_dnd": ev.customer_dnd,
        "failure_category": ev.failure_category,
        "failure_label": ev.failure_label,
        "recoverability_score": ev.recoverability_score,
        "recovery_status": ev.recovery_status,
        "recovery_attempts": ev.recovery_attempts,
        "recovered_amount": ev.recovered_amount,
        "recovered_amount_inr": round(ev.recovered_amount / 100, 2),
        "recovery_cost_paise": ev.recovery_cost_paise,
        "has_dispute": ev.has_dispute,
        "customer_opted_out": ev.customer_opted_out,
        "subscription_cancelled": ev.subscription_cancelled,
        "is_simulated": ev.is_simulated,
        "cascade_group_id": ev.cascade_group_id,
        "recovered_at": ev.recovered_at.isoformat() if ev.recovered_at else None,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
        "updated_at": ev.updated_at.isoformat() if ev.updated_at else None,
    }
