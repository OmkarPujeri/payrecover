"""Webhook tests: HMAC signature verification, ingestion, dedup, circuit events."""
import hashlib
import hmac
import json

import pytest

from app.config import settings
from app.webhooks.signature import verify_webhook_signature


def _failed_body(payment_id: str, order_id: str, amount: int = 50000) -> dict:
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
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_source": "customer",
                    "error_step": "payment_authorization",
                    "error_reason": "insufficient_funds",
                    "error_description": "insufficient funds",
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
