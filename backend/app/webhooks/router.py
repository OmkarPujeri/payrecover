"""POST /webhooks/razorpay — inbound Razorpay webhook receiver.

Flow: verify HMAC signature (skipped with a warning if no secret is
configured, so simulator traffic flows in dev) -> parse -> route by event
type -> persist/update via the ingestion service -> **run the agent pipeline or
the circuit breakers** -> broadcast over SSE. Responses are idempotent
(duplicates return 200 and are not re-decided).

A ``payment.failed`` is the system's primary trigger, so it hands straight off to
``run_pipeline`` — the same orchestration ``POST /api/simulator/inject`` runs.
Real traffic and simulated traffic therefore take one code path and behave
identically, including the confidence gate's decision about what may fire without
a human. The pipeline runs inline and its result comes back in the response, so a
single curl shows the whole reasoning chain; with live LLM keys that costs a
couple of seconds of webhook latency, which is the deliberate trade (a production
deployment would ack first and hand off to a queue).

The breaker step is the important one for *state changes*. A ``payment.captured``
arriving while a retry sits scheduled and a reminder sits queued is the moment the
system either proves itself or embarrasses the merchant: ``apply_circuit_event``
records that the payment landed, then ``check_circuit_breakers`` trips CB-001 and
cancels the queued work — so nobody gets nagged for money they already paid.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.execution.circuit_breakers import check_circuit_breakers
from app.ingest import OPT_OUT_EVENTS, apply_circuit_event, event_to_dict, ingest_failure
from app.pipeline import run_pipeline
from app.sse import sse_manager
from app.webhooks.signature import verify_webhook_signature

logger = logging.getLogger("payrecover.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Webhook events that change the state the circuit breakers watch.
CIRCUIT_EVENTS = {
    "payment.captured",
    "order.paid",
    "invoice.paid",
    "payment.dispute.created",
    "subscription.cancelled",
    "refund.created",
    *OPT_OUT_EVENTS,
}


def _extract_entity(payload: dict) -> dict:
    for key in (
        "payment",
        "order",
        "invoice",
        "subscription",
        "refund",
        "dispute",
        "customer",
    ):
        node = payload.get(key)
        if isinstance(node, dict):
            entity = node.get("entity")
            if isinstance(entity, dict):
                return entity
    return {}


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request, session: AsyncSession = Depends(get_session)
):
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")

    if settings.razorpay_webhook_secret:
        if not verify_webhook_signature(raw, signature, settings.razorpay_webhook_secret):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    else:
        logger.warning(
            "RAZORPAY_WEBHOOK_SECRET not set — skipping signature verification (dev/sim)"
        )

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    event_type = body.get("event", "")
    payload = body.get("payload") or {}

    # --- Primary trigger: a new failed payment ---
    if event_type == "payment.failed":
        entity = (payload.get("payment") or {}).get("entity") or {}
        if not entity.get("id"):
            raise HTTPException(status_code=400, detail="Missing payment entity")
        event, created = await ingest_failure(session, entity, event_type=event_type)
        await sse_manager.broadcast(
            {
                "type": "failure_detected" if created else "failure_duplicate",
                "event": event_to_dict(event),
            }
        )
        response: dict = {"status": "ok", "created": created, "event_id": str(event.id)}

        if not created:
            # A redelivery. Razorpay retries until it gets a 2xx, so re-running the
            # pipeline here would mint a *second* payment link for a failure we have
            # already decided on. Ingestion is idempotent; the decision has to be
            # too, and the cheapest way to guarantee that is to not re-decide.
            response["pipeline"] = {"ran": False, "reason": "duplicate_delivery"}
            return response

        try:
            response["event"] = await run_pipeline(session, event)
            response["pipeline"] = {"ran": True}
        except Exception as exc:  # noqa: BLE001 — a webhook must never retry-loop
            # The failed payment is already recorded, which is the part that must
            # not be lost. A non-2xx here would make Razorpay redeliver, and the
            # redelivery would skip the pipeline anyway (created=False above), so
            # retrying buys nothing and risks a loop. Ack, log loudly, and leave
            # the event mid-pipeline where the audit log and a human can see it.
            logger.exception("Agent pipeline failed for recovery event %s", event.id)
            response["pipeline"] = {"ran": False, "reason": "error", "error": str(exc)}
        return response

    # --- State-changing events: update the event(s), then run the breakers ---
    if event_type in CIRCUIT_EVENTS:
        entity = _extract_entity(payload)
        affected = await apply_circuit_event(session, event_type, entity)
        trips = []
        for ev in affected:
            await sse_manager.broadcast(
                {
                    "type": "circuit_event",
                    "event_type": event_type,
                    "event": event_to_dict(ev),
                }
            )
            # The state moved; ask the breakers whether recovery should continue.
            # This is what cancels a scheduled retry the instant the customer pays.
            trip = await check_circuit_breakers(
                session, ev, trigger_source=f"webhook:{event_type}"
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
            "event": event_type,
            "affected": len(affected),
            "breakers_tripped": trips,
        }

    # --- Everything else: acknowledged no-op ---
    logger.info("Unhandled webhook event: %s", event_type)
    return {"status": "ignored", "event": event_type}
