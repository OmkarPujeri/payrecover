"""Synthetic failure generator + injection endpoint.

A lightweight version of the full simulator (weighted profiles, chaos presets,
and cascade mode arrive in a later phase). It lets you POST a failure and watch
it flow through webhook-equivalent ingestion -> diagnosis -> strategy ->
compliance -> the confidence gate -> **execution**, validating the whole pipeline
without needing real Razorpay traffic.

Two extra levers live here because they are what make the demo reproducible:

* ``POST /run-due-actions`` fires scheduled work *on command* instead of waiting
  for the background scheduler's next tick. Nobody waits 30 minutes on stage.
* ``POST /circuit-event`` replays a state change (the customer paid, a dispute
  landed, someone opted out) so a circuit breaker can be shown tripping without
  hand-crafting a webhook payload.
"""
from __future__ import annotations

import random
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.execution.circuit_breakers import check_circuit_breakers
from app.execution.scheduler import run_due_actions
from app.ingest import apply_circuit_event, event_to_dict, ingest_failure
from app.pipeline import run_pipeline
from app.sse import sse_manager

router = APIRouter(prefix="/api/simulator", tags=["simulator"])

# type -> (source, step, reason, code, human label, description), weight
FAILURE_PROFILES: dict[str, dict] = {
    "gateway_timeout": {
        "error": ("gateway", "payment_authorization", "gateway_timeout", "GATEWAY_ERROR"),
        "label": "Gateway Timeout",
        "description": "Payment failed due to a gateway timeout",
        "weight": 20,
    },
    "insufficient_funds": {
        "error": ("customer", "payment_authorization", "insufficient_funds", "BAD_REQUEST_ERROR"),
        "label": "Insufficient Funds",
        "description": "Payment processing failed due to insufficient funds",
        "weight": 25,
    },
    "card_expired": {
        "error": ("customer", "payment_initiation", "card_expired", "BAD_REQUEST_ERROR"),
        "label": "Card Expired",
        "description": "Payment failed because the card has expired",
        "weight": 10,
    },
    "otp_failed": {
        "error": ("customer", "payment_authentication", "invalid_otp", "BAD_REQUEST_ERROR"),
        "label": "OTP Failed",
        "description": "Authentication failed due to incorrect otp",
        "weight": 15,
    },
    "bank_downtime": {
        "error": ("gateway", "payment_authorization", "issuer_bank_down", "GATEWAY_ERROR"),
        "label": "Bank Downtime",
        "description": "Issuer bank is temporarily down",
        "weight": 10,
    },
    "user_cancelled": {
        "error": ("customer", "payment_authentication", "payment_cancelled_by_user", "BAD_REQUEST_ERROR"),
        "label": "User Cancelled",
        "description": "Payment was cancelled by the user",
        "weight": 8,
    },
    "network_timeout": {
        "error": ("gateway", "payment_authorization", "network_timeout", "GATEWAY_ERROR"),
        "label": "Network Timeout",
        "description": "Payment failed due to a network timeout",
        "weight": 7,
    },
    "mandate_inactive": {
        "error": ("customer", "payment_authorization", "mandate_inactive", "BAD_REQUEST_ERROR"),
        "label": "Mandate Inactive",
        "description": "The e-mandate is inactive or revoked",
        "weight": 3,
    },
    "risk_flagged": {
        "error": ("razorpay", "payment_authorization", "payment_risk_threshold_breached", "BAD_REQUEST_ERROR"),
        "label": "Risk Flagged",
        "description": "Payment blocked: risk threshold breached",
        "weight": 2,
    },
}

_METHODS = ["card", "upi", "netbanking", "wallet"]
_METHOD_WEIGHTS = [40, 35, 15, 10]
_FIRST = ["Aarav", "Diya", "Vivaan", "Ananya", "Aditya", "Isha", "Kabir", "Meera",
          "Rohan", "Saanvi", "Arjun", "Priya", "Karan", "Neha", "Rahul", "Tara"]
_LAST = ["Sharma", "Patel", "Reddy", "Iyer", "Nair", "Gupta", "Singh", "Mehta",
         "Rao", "Das", "Kulkarni", "Bose", "Menon", "Chopra"]


def _weighted_amount_paise() -> int:
    """₹299–₹25,000, weighted toward ₹500–₹3,000 (values in paise)."""
    if random.random() < 0.8:
        rupees = random.randint(500, 3000)
    else:
        rupees = random.randint(3001, 25000)
    return rupees * 100


def _make_failure_entity(
    failure_type: str | None = None,
    amount: int | None = None,
    method: str | None = None,
) -> tuple[dict, str]:
    if failure_type not in FAILURE_PROFILES:
        types = list(FAILURE_PROFILES)
        weights = [FAILURE_PROFILES[t]["weight"] for t in types]
        failure_type = random.choices(types, weights=weights, k=1)[0]

    profile = FAILURE_PROFILES[failure_type]
    source, step, reason, code = profile["error"]
    amount = amount if amount and amount > 0 else _weighted_amount_paise()
    method = method if method in _METHODS else random.choices(_METHODS, _METHOD_WEIGHTS, k=1)[0]

    first, last = random.choice(_FIRST), random.choice(_LAST)
    name = f"{first} {last}"
    email = f"{first.lower()}.{last.lower()}@example.com"
    contact = f"+9198{random.randint(10000000, 99999999)}"

    entity = {
        "id": f"pay_sim_{secrets.token_hex(7)}",
        "order_id": f"order_sim_{secrets.token_hex(7)}",
        "amount": amount,
        "currency": "INR",
        "status": "failed",
        "method": method,
        "email": email,
        "contact": contact,
        "name": name,
        "error_code": code,
        "error_source": source,
        "error_step": step,
        "error_reason": reason,
        "error_description": profile["description"],
        "notes": {"customer_name": name},
    }
    return entity, failure_type


class InjectRequest(BaseModel):
    failure_type: str | None = Field(default=None, description="One of the known profiles; random if omitted")
    amount: int | None = Field(default=None, description="Amount in paise; random if omitted")
    method: str | None = Field(default=None, description="card|upi|netbanking|wallet; random if omitted")
    count: int = Field(default=1, ge=1, le=200)
    diagnose: bool = Field(default=True, description="Run the Diagnostic Agent (LLM #1) on each injected failure")
    recover: bool = Field(default=True, description="Run the Strategy Agent (LLM #2) + Compliance Engine on each diagnosed failure")
    execute: bool = Field(
        default=True,
        description=(
            "Execute actions the confidence gate auto-approved. Anything routed to "
            "human review is left in the HITL queue regardless of this flag."
        ),
    )


@router.post("/inject")
async def inject_failure(
    body: InjectRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    body = body or InjectRequest()
    created_events = []
    for _ in range(body.count):
        entity, ftype = _make_failure_entity(body.failure_type, body.amount, body.method)
        event, _created = await ingest_failure(session, entity, is_simulated=True)
        payload = {**event_to_dict(event), "failure_type": ftype}
        await sse_manager.broadcast({"type": "failure_detected", "event": payload})

        # Everything past ingestion is the shared pipeline — the same code a real
        # payment.failed webhook runs. ``failure_type`` rides along in ``extra``
        # because it is a simulator concept the pipeline knows nothing about.
        payload = await run_pipeline(
            session,
            event,
            diagnose=body.diagnose,
            recover=body.recover,
            execute=body.execute,
            extra={"failure_type": ftype},
        )
        created_events.append(payload)

    return {
        "status": "ok",
        "injected": len(created_events),
        "diagnosed": body.diagnose,
        "recovered": body.recover,
        "executed": body.execute,
        "events": created_events,
    }


class RunDueRequest(BaseModel):
    now: datetime | None = Field(
        default=None,
        description="Pretend it is this instant — fires work scheduled for any earlier time",
    )
    limit: int = Field(default=100, ge=1, le=500)


@router.post("/run-due-actions")
async def run_due(
    body: RunDueRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Fire every scheduled action that is due — the demo's fast-forward button.

    The background scheduler calls this same deterministic core on a timer. Pass
    ``now`` to jump the clock forward: a retry the agent scheduled for 7 AM
    tomorrow fires immediately, so the cascade story (retry -> fails differently
    -> agent pivots -> customer pays -> CB-001 halts everything) can be shown in
    a minute rather than a day. Each action is re-checked against the circuit
    breakers before firing, so fast-forwarding cannot nag someone who has paid.
    """
    body = body or RunDueRequest()
    fired = await run_due_actions(session, now=body.now, limit=body.limit)
    return {
        "status": "ok",
        "now": (body.now.isoformat() if body.now else None),
        "processed": len(fired),
        "fired": sum(1 for f in fired if f.get("fired")),
        "actions": fired,
    }


class CircuitEventRequest(BaseModel):
    event_type: str = Field(
        description=(
            "payment.captured | order.paid | payment.dispute.created | "
            "subscription.cancelled | refund.created | customer.opted_out"
        )
    )
    order_id: str | None = Field(default=None, description="Target by Razorpay order id")
    payment_id: str | None = Field(default=None, description="Target by Razorpay payment id")
    contact: str | None = Field(default=None, description="Target by customer contact (opt-outs)")
    amount: int | None = Field(default=None, description="Recovered amount in paise; defaults to the order amount")


@router.post("/circuit-event")
async def inject_circuit_event(
    body: CircuitEventRequest,
    session: AsyncSession = Depends(get_session),
):
    """Replay a state change and let the circuit breakers respond.

    Equivalent to the matching Razorpay webhook arriving (or, for an opt-out, the
    merchant's STOP handler firing) without hand-assembling a signed payload.
    Same code path as the real thing: record the state change, then evaluate
    CB-001..007 and cancel whatever should no longer happen.
    """
    entity = {
        "id": body.payment_id or "",
        "order_id": body.order_id or "",
        "contact": body.contact or "",
        "amount": body.amount,
    }
    affected = await apply_circuit_event(session, body.event_type, entity)

    trips = []
    for ev in affected:
        await sse_manager.broadcast(
            {
                "type": "circuit_event",
                "event_type": body.event_type,
                "event": event_to_dict(ev),
            }
        )
        trip = await check_circuit_breakers(
            session, ev, trigger_source=f"simulator:{body.event_type}"
        )
        if trip is not None:
            trips.append(
                {
                    "event_id": str(ev.id),
                    "breaker_id": trip.breaker_id,
                    "breaker": trip.breaker_name,
                    "reason": trip.reason,
                }
            )

    return {
        "status": "ok",
        "event_type": body.event_type,
        "affected": len(affected),
        "breakers_tripped": trips,
    }


@router.get("/profiles")
async def list_profiles():
    """The failure profiles the injector can produce."""
    return {
        "profiles": [
            {"type": t, "label": p["label"], "weight": p["weight"]}
            for t, p in FAILURE_PROFILES.items()
        ]
    }
