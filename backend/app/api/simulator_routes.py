"""Synthetic failure generator, chaos presets, and the demo's control surface.

Lets you POST a failure and watch it flow through webhook-equivalent ingestion ->
diagnosis -> strategy -> compliance -> the confidence gate -> **execution**,
validating the whole pipeline without needing real Razorpay traffic.

Everything here is a composition of primitives that already exist; no endpoint in
this module can produce an outcome the normal pipeline could not, which is what
keeps the demo evidence rather than theatre.

* ``POST /inject`` — one or many failures through the real pipeline.
* ``POST /run-due-actions`` fires scheduled work *on command* instead of waiting
  for the background scheduler's next tick. Nobody waits 30 minutes on stage.
* ``POST /circuit-event`` replays a state change (the customer paid, a dispute
  landed, someone opted out) so a circuit breaker can be shown tripping without
  hand-crafting a webhook payload.
* ``POST /chaos/{preset}`` runs a scripted scenario from ``app.chaos`` — a short
  sequence of exactly those three verbs — so a whole story is one click.
* ``POST /run-batch`` injects volume and returns an *aggregate*, not 100 event
  payloads. The aggregation is the reason it exists separately from ``/inject``.

Cascade mode is the one preset that needs new behaviour rather than composition
and is registered unavailable until it lands.
"""
from __future__ import annotations

import random
import secrets
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Importing the dashboard's own handlers rather than re-deriving the numbers here.
# They take an explicit session, so the ``Depends`` default is never evaluated —
# and the totals a preset reports back cannot drift from what the dashboard shows
# a second later, which is exactly the disagreement that would be noticed on stage.
from app.api.dashboard import get_economics, get_metrics
from app.chaos import CHAOS_PRESETS, preset_summaries
from app.database import get_session
from app.execution.circuit_breakers import check_circuit_breakers
from app.execution.scheduler import run_due_actions
from app.ingest import apply_circuit_event, event_to_dict, ingest_failure
from app.models import RecoveryEvent
from app.pipeline import run_pipeline
from app.sse import sse_manager
from app.timeutil import utcnow

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


# --------------------------------------------------------------------------- #
# Shared primitives. /inject, /chaos/{preset} and /run-batch all go through
# these, so there is exactly one injection path and one circuit-event path in the
# simulator. A preset that took a shortcut around the pipeline would be a demo of
# the shortcut.
# --------------------------------------------------------------------------- #
async def _inject_one(
    session: AsyncSession,
    *,
    failure_type: str | None,
    amount: int | None,
    method: str | None,
    diagnose: bool,
    recover: bool,
    execute: bool,
) -> tuple[RecoveryEvent, dict]:
    """Mint one synthetic failure, ingest it, and run the full agent pipeline.

    Returns the live ORM object alongside the pipeline payload. Callers that keep
    running (a preset with steps after the injection) need the object, because the
    row keeps changing underneath the payload.
    """
    entity, ftype = _make_failure_entity(failure_type, amount, method)
    event, _created = await ingest_failure(session, entity, is_simulated=True)
    await sse_manager.broadcast(
        {"type": "failure_detected", "event": {**event_to_dict(event), "failure_type": ftype}}
    )

    # Everything past ingestion is the shared pipeline — the same code a real
    # payment.failed webhook runs. ``failure_type`` rides along in ``extra``
    # because it is a simulator concept the pipeline knows nothing about.
    payload = await run_pipeline(
        session,
        event,
        diagnose=diagnose,
        recover=recover,
        execute=execute,
        extra={"failure_type": ftype},
    )
    return event, payload


async def _fire_circuit_event(
    session: AsyncSession, event_type: str, entity: dict
) -> tuple[list[RecoveryEvent], list[dict]]:
    """Record a state change, then let CB-001..007 respond to it.

    Same code path as the real webhook: persist the change first, evaluate the
    breakers second. Returns the events it matched and the trips it caused.

    The trips carry what ``BreakerTrip`` actually knows. The *count* of actions a
    breaker cancelled is not on the trip — it is computed inside
    ``check_circuit_breakers`` and lands on the ``circuit_breaker`` SSE frame and
    the ``circuit_breaker_events`` audit row, which is where the dashboard reads
    it. Don't add a ``cancelled_actions`` key here; it would always be null.
    """
    affected = await apply_circuit_event(session, event_type, entity)

    trips = []
    for ev in affected:
        await sse_manager.broadcast(
            {"type": "circuit_event", "event_type": event_type, "event": event_to_dict(ev)}
        )
        trip = await check_circuit_breakers(
            session, ev, trigger_source=f"simulator:{event_type}"
        )
        if trip is not None:
            trips.append(
                {
                    "event_id": str(ev.id),
                    "order_id": ev.razorpay_order_id,
                    "breaker_id": trip.breaker_id,
                    "breaker": trip.breaker_name,
                    "trigger_type": trip.trigger_type,
                    "reason": trip.reason,
                }
            )
    return affected, trips


def _summarise_event(event: RecoveryEvent, payload: dict) -> dict:
    """A dozen fields per event instead of thirty-three.

    Re-serialises the live ORM object rather than trusting the injection-time
    payload, because a preset's later steps mutate the row: an order captured
    after a fast-forward would otherwise still be reported ``pending`` in the same
    response body whose ``metrics`` block counts it as recovered. The routing
    fields have no column to read back from, so those do come from the payload.

    The SSE stream carries the full detail; this is the receipt.
    """
    fresh = event_to_dict(event)
    recovery = payload.get("recovery") or {}
    return {
        "id": fresh["id"],
        "order_id": fresh["razorpay_order_id"],
        "amount": fresh["amount"],
        "amount_inr": fresh["amount_inr"],
        "failure_type": payload.get("failure_type"),
        "failure_label": fresh["failure_label"],
        "recovery_status": fresh["recovery_status"],
        "recovered_amount": fresh["recovered_amount"],
        "tool": recovery.get("tool"),
        "confidence": recovery.get("confidence"),
        "gate_action": recovery.get("gate_action"),
        "requires_human": recovery.get("requires_human"),
        # The *action's* status (approved / pending_review / escalated), not the
        # event's — deliberately not called ``status``, since a payload carrying
        # both would invite reading the wrong one.
        "action_status": recovery.get("status"),
        "executed": bool(recovery.get("executed")),
    }


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
        _event, payload = await _inject_one(
            session,
            failure_type=body.failure_type,
            amount=body.amount,
            method=body.method,
            diagnose=body.diagnose,
            recover=body.recover,
            execute=body.execute,
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
    affected, trips = await _fire_circuit_event(session, body.event_type, entity)

    return {
        "status": "ok",
        "event_type": body.event_type,
        "affected": len(affected),
        "breakers_tripped": trips,
    }


class ChaosRequest(BaseModel):
    diagnose: bool = Field(default=True, description="Run the Diagnostic Agent (LLM #1)")
    recover: bool = Field(default=True, description="Run the Strategy Agent (LLM #2) + Compliance Engine")
    execute: bool = Field(default=True, description="Execute what the confidence gate approved")
    max_events: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description=(
            "Cap each inject step at this many events. Applied per step, not per "
            "run, so the preset's mix of order values survives the cap — a capped "
            "Salary Day Batch still contains both sub-₹10,000 and ₹25,000 orders. "
            "Mainly for tests; the demo runs presets at full size."
        ),
    )


@router.post("/chaos/{preset}")
async def run_chaos_preset(
    preset: str,
    body: ChaosRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Run a scripted scenario from the ``app.chaos`` registry — one click, one story.

    A preset is a short sequence of three verbs (inject a failure, fast-forward
    the clock, replay a state change), each of which is an endpoint you could call
    by hand. The runner is deliberately nothing but a loop over them: a preset must
    not be able to produce an outcome the normal pipeline could not, or the demo
    stops being evidence of anything.

    The response is a receipt, not a data dump — a dozen fields per event rather
    than thirty-three, because the SSE stream is already carrying the detail to
    whoever is watching. ``metrics`` is the dashboard's own ``/metrics`` handler,
    so the totals here cannot disagree with the panel next to them.
    """
    spec = CHAOS_PRESETS.get(preset)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown preset '{preset}'. Known presets: {', '.join(CHAOS_PRESETS)}",
        )
    if not spec.get("available", False):
        # Refuse loudly. A preset registered but not yet implemented must not
        # return 200 with an empty run — that reads as "the scenario found nothing
        # to do", which is a far more expensive misunderstanding than an error.
        raise HTTPException(
            status_code=409,
            detail={
                "preset": preset,
                "error": "Preset is registered but not available yet",
                "reason": spec.get("unavailable_reason"),
            },
        )

    body = body or ChaosRequest()
    injected: list[tuple[RecoveryEvent, dict]] = []
    trips: list[dict] = []
    steps_run: list[dict] = []
    processed = fired = 0

    for step in spec["steps"]:
        op = step["op"]

        if op == "inject":
            count = step.get("count", 1)
            if body.max_events is not None:
                count = min(count, body.max_events)
            for _ in range(count):
                injected.append(
                    await _inject_one(
                        session,
                        failure_type=step.get("failure_type"),
                        amount=step.get("amount"),
                        method=step.get("method"),
                        diagnose=body.diagnose,
                        recover=body.recover,
                        execute=body.execute,
                    )
                )
            steps_run.append(
                {
                    "op": "inject",
                    "failure_type": step.get("failure_type"),
                    "amount": step.get("amount"),
                    "method": step.get("method"),
                    "count": count,
                }
            )

        elif op == "fast_forward":
            hours = step["hours"]
            actions = await run_due_actions(session, now=utcnow() + timedelta(hours=hours))
            processed += len(actions)
            step_fired = sum(1 for a in actions if a.get("fired"))
            fired += step_fired
            steps_run.append(
                {
                    "op": "fast_forward",
                    "hours": hours,
                    "processed": len(actions),
                    "fired": step_fired,
                    "cancelled": sum(1 for a in actions if a.get("breaker_id")),
                }
            )

        elif op == "circuit":
            # Scoped to the events *this run* injected, in injection order. A
            # preset must never reach into rows it did not create.
            targets = (
                injected
                if step.get("target") == "all"
                else injected[: step.get("n", 1)]
            )
            step_trips = 0
            for event, _payload in targets:
                _affected, new_trips = await _fire_circuit_event(
                    session,
                    step["event_type"],
                    {"order_id": event.razorpay_order_id, "amount": None},
                )
                trips.extend(new_trips)
                step_trips += len(new_trips)
            steps_run.append(
                {
                    "op": "circuit",
                    "event_type": step["event_type"],
                    "target": step.get("target", "first"),
                    "events": len(targets),
                    "breakers_tripped": step_trips,
                }
            )

        else:  # pragma: no cover — the registry is ours, so this is a typo guard
            raise HTTPException(
                status_code=500, detail=f"Preset '{preset}' has an unknown op '{op}'"
            )

    return {
        "status": "ok",
        "preset": preset,
        "name": spec["name"],
        "description": spec["description"],
        "narrative": spec["narrative"],
        "steps": steps_run,
        "injected": len(injected),
        "events": [_summarise_event(event, payload) for event, payload in injected],
        "actions_processed": processed,
        "actions_fired": fired,
        "breakers_tripped": trips,
        "metrics": await get_metrics(session=session),
    }


class RunBatchRequest(BaseModel):
    count: int = Field(default=100, ge=1, le=200, description="How many failures to inject")
    diagnose: bool = Field(default=True, description="Run the Diagnostic Agent (LLM #1)")
    recover: bool = Field(default=True, description="Run the Strategy Agent (LLM #2) + Compliance Engine")
    execute: bool = Field(default=True, description="Execute what the confidence gate approved")


@router.post("/run-batch")
async def run_batch(
    body: RunBatchRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Inject volume and return the shape of the outcome, not the outcome itself.

    The aggregation *is* the reason this exists next to ``/inject``: 100 events is
    around 3,300 fields of JSON nobody reads, and the question a batch actually
    answers is distributional — how the gate split the work, which failure types
    dominated, what came back. Per-event detail is on the SSE stream and one
    ``GET /api/dashboard/events`` away.

    Failure types are drawn from the weighted profiles, whose weights sum to 100,
    so the mix approximates real Indian merchant traffic rather than being uniform.
    """
    body = body or RunBatchRequest()

    by_failure_type: Counter[str] = Counter()
    by_gate: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    by_tool: Counter[str] = Counter()
    requires_human = 0

    for _ in range(body.count):
        event, payload = await _inject_one(
            session,
            failure_type=None,
            amount=None,
            method=None,
            diagnose=body.diagnose,
            recover=body.recover,
            execute=body.execute,
        )
        summary = _summarise_event(event, payload)
        by_failure_type[summary["failure_type"] or "unknown"] += 1
        by_gate[summary["gate_action"] or "not_gated"] += 1
        by_status[summary["recovery_status"] or "unknown"] += 1
        by_tool[summary["tool"] or "none"] += 1
        if summary["requires_human"]:
            requires_human += 1

    return {
        "status": "ok",
        "requested": body.count,
        "injected": body.count,
        "requires_human": requires_human,
        "by_failure_type": dict(by_failure_type.most_common()),
        "by_gate": dict(by_gate.most_common()),
        "by_status": dict(by_status.most_common()),
        "by_tool": dict(by_tool.most_common()),
        "metrics": await get_metrics(session=session),
        "economics": await get_economics(session=session),
    }


@router.get("/presets")
async def list_presets():
    """The chaos presets, as the dashboard's button row consumes them.

    Served from the registry rather than hardcoded in the frontend so adding a
    preset is a dict entry with no client edit, and so an unavailable preset
    renders disabled with its own reason instead of failing on click.
    """
    return {"presets": preset_summaries()}


@router.get("/profiles")
async def list_profiles():
    """The failure profiles the injector can produce."""
    return {
        "profiles": [
            {"type": t, "label": p["label"], "weight": p["weight"]}
            for t, p in FAILURE_PROFILES.items()
        ]
    }
