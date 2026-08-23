"""POST /webhooks/razorpay — inbound Razorpay webhook receiver.

Flow: verify HMAC signature (skipped with a warning if no secret is
configured, so simulator traffic flows in dev) -> parse -> route by event
type -> persist/update via the ingestion service -> broadcast over SSE.
Responses are idempotent (duplicates return 200).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.ingest import apply_circuit_event, event_to_dict, ingest_failure
from app.sse import sse_manager
from app.webhooks.signature import verify_webhook_signature

logger = logging.getLogger("payrecover.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Webhook events that halt/close recovery (subset of PRD circuit breakers).
CIRCUIT_EVENTS = {
    "payment.captured",
    "order.paid",
    "invoice.paid",
    "payment.dispute.created",
    "subscription.cancelled",
    "refund.created",
}


def _extract_entity(payload: dict) -> dict:
    for key in ("payment", "order", "invoice", "subscription", "refund", "dispute"):
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
        return {"status": "ok", "created": created, "event_id": str(event.id)}

    # --- Recovery-halting events: update matching recovery event(s) ---
    if event_type in CIRCUIT_EVENTS:
        entity = _extract_entity(payload)
        affected = await apply_circuit_event(session, event_type, entity)
        for ev in affected:
            await sse_manager.broadcast(
                {
                    "type": "circuit_event",
                    "event_type": event_type,
                    "event": event_to_dict(ev),
                }
            )
        return {"status": "ok", "event": event_type, "affected": len(affected)}

    # --- Everything else: acknowledged no-op ---
    logger.info("Unhandled webhook event: %s", event_type)
    return {"status": "ignored", "event": event_type}
