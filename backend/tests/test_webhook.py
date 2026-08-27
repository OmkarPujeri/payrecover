"""Webhook tests: HMAC signature verification, ingestion, dedup, circuit events,
and the handoff into the agent pipeline.
"""
import hashlib
import hmac
import json

import pytest

from app.config import settings
from app.execution import statuses
from app.webhooks.signature import verify_webhook_signature

# (error_source, error_step, error_reason, error_code) — mirrors the simulator's
# FAILURE_PROFILES so a webhook payload can land on a known failure profile.
INSUFFICIENT_FUNDS = (
    "customer",
    "payment_authorization",
    "insufficient_funds",
    "BAD_REQUEST_ERROR",
)
BANK_DOWNTIME = (
    "gateway",
    "payment_authorization",
    "issuer_bank_down",
    "GATEWAY_ERROR",
)


def _failed_body(
    payment_id: str,
    order_id: str,
    amount: int = 50000,
    *,
    error: tuple[str, str, str, str] = INSUFFICIENT_FUNDS,
) -> dict:
    source, step, reason, code = error
    return {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "email": "jane@example.com",
                    "contact": "+919876543210",
                    "error_code": code,
                    "error_source": source,
                    "error_step": step,
                    "error_reason": reason,
                    "error_description": reason.replace("_", " "),
                }
            }
        },
        "created_at": 1692543665,
    }


def _captured_body(payment_id: str, order_id: str, amount: int = 50000) -> dict:
    return {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
        "created_at": 1692543999,
    }


# --- Pure unit tests for the signature helper -------------------------------
def test_signature_valid():
    body = b'{"hello":"world"}'
    secret = "whsec_test"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, sig, secret) is True


def test_signature_invalid():
    body = b'{"hello":"world"}'
    assert verify_webhook_signature(body, "deadbeef", "whsec_test") is False
    assert verify_webhook_signature(body, None, "whsec_test") is False
    assert verify_webhook_signature(body, "x", None) is False


# --- Endpoint: ingestion + dedup -------------------------------------------
async def test_webhook_ingest_and_dedup(client):
    body = _failed_body("pay_test_001", "order_test_001")

    r1 = await client.post("/webhooks/razorpay", json=body)
    assert r1.status_code == 200
    assert r1.json()["created"] is True

    # Redelivered identical event -> idempotent, not created again.
    r2 = await client.post("/webhooks/razorpay", json=body)
    assert r2.status_code == 200
    assert r2.json()["created"] is False

    events = (await client.get("/api/dashboard/events")).json()
    assert events["total"] == 1
    assert events["events"][0]["error_reason"] == "insufficient_funds"


# --- Endpoint: circuit event marks recovery --------------------------------
async def test_webhook_captured_marks_recovered(client):
    await client.post("/webhooks/razorpay", json=_failed_body("pay_test_002", "order_test_002", 120000))
    r = await client.post("/webhooks/razorpay", json=_captured_body("pay_test_002b", "order_test_002", 120000))
    assert r.status_code == 200
    assert r.json()["affected"] == 1

    events = (await client.get("/api/dashboard/events?status=recovered")).json()
    assert events["total"] == 1
    assert events["events"][0]["recovered_amount"] == 120000


# --- Endpoint: signature enforced when a secret is configured --------------
async def test_webhook_rejects_bad_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "whsec_live")
    body = _failed_body("pay_test_003", "order_test_003")
    r = await client.post(
        "/webhooks/razorpay",
        content=json.dumps(body).encode(),
        headers={"X-Razorpay-Signature": "bad", "Content-Type": "application/json"},
    )
    assert r.status_code == 400


async def test_webhook_accepts_good_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "whsec_live")
    body = _failed_body("pay_test_004", "order_test_004")
    raw = json.dumps(body).encode()
    sig = hmac.new(b"whsec_live", raw, hashlib.sha256).hexdigest()
    r = await client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["created"] is True


# --- Endpoint: payment.failed hands off to the agent pipeline ---------------
async def test_failed_webhook_runs_the_whole_agent_pipeline(client):
    """A real webhook must drive the agents, not just get filed.

    This is the regression test for a genuine gap: the webhook used to ingest,
    broadcast and return, so every agent stage was reachable only through the
    simulator. The pipeline existed but the front door wasn't wired to it.

    Uses bank downtime under ₹10,000 because that is one of only two routes the
    mock pipeline decides deterministically (the enricher seeds a synthetic
    customer history from ``sha256(identity)``, so most profiles vary). Unlike
    ``/inject``, which invents a random customer, a webhook carries a fixed
    identity — so this is stable for that reason too.
    """
    r = await client.post(
        "/webhooks/razorpay",
        json=_failed_body("pay_wh_010", "order_wh_010", 250000, error=BANK_DOWNTIME),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["pipeline"] == {"ran": True}

    event = body["event"]
    assert event["diagnosis_source"] in {"llm", "mock"}

    recovery = event["recovery"]
    assert recovery["tool"] == "schedule_smart_retry"
    assert recovery["status"] == statuses.APPROVED
    assert recovery["requires_human"] is False
    assert recovery["executed"] is True
    # Executing a retry means *arranging* it, so it lands scheduled, not completed.
    assert event["execution"]["status"] == statuses.SCHEDULED

    # And it is durable, not merely echoed back in the response.
    actions = (await client.get(f"/api/actions?event_id={body['event_id']}")).json()
    assert [a["agent_name"] for a in actions["actions"]].count("strategy") == 1


async def test_redelivered_webhook_is_not_decided_twice(client):
    """Razorpay retries until it gets a 2xx — a retry must not buy a second link.

    Ingestion was already idempotent; the decision has to be too, or a redelivered
    webhook mints a duplicate payment link for one failure and the merchant pays
    twice for one recovery.
    """
    body = _failed_body("pay_wh_011", "order_wh_011", 250000, error=BANK_DOWNTIME)

    first = (await client.post("/webhooks/razorpay", json=body)).json()
    assert first["pipeline"]["ran"] is True

    second = (await client.post("/webhooks/razorpay", json=body)).json()
    assert second["created"] is False
    assert second["pipeline"] == {"ran": False, "reason": "duplicate_delivery"}
    assert second["event_id"] == first["event_id"]
    assert "event" not in second

    actions = (await client.get(f"/api/actions?event_id={first['event_id']}")).json()
    strategy = [a for a in actions["actions"] if a["agent_name"] == "strategy"]
    assert len(strategy) == 1


async def test_webhook_acks_even_when_the_pipeline_fails(client, monkeypatch):
    """A pipeline error must not turn into a Razorpay retry loop.

    The ingested failure is the part that cannot be lost — Razorpay will not tell
    us about this payment again. Since a redelivery would skip the pipeline anyway
    (the event now exists, so ``created`` is False), answering non-2xx would buy
    nothing and risk looping, so we ack and record the error instead.
    """

    async def boom(*_args, **_kwargs):
        raise RuntimeError("strategy agent exploded")

    monkeypatch.setattr("app.webhooks.router.run_pipeline", boom)

    r = await client.post(
        "/webhooks/razorpay", json=_failed_body("pay_wh_012", "order_wh_012")
    )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["pipeline"]["ran"] is False
    assert body["pipeline"]["reason"] == "error"
    assert "exploded" in body["pipeline"]["error"]

    # The failed payment is still on record.
    events = (await client.get("/api/dashboard/events")).json()
    assert events["total"] == 1
