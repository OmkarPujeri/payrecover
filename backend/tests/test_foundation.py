"""Foundation tests: health, simulator injection, event detail, metrics."""
import hashlib
import hmac
import json


def _failed_body(payment_id: str, order_id: str, amount: int) -> dict:
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
                    "method": "upi",
                    "email": "x@y.com",
                    "contact": "+919000000000",
                    "error_code": "GATEWAY_ERROR",
                    "error_source": "gateway",
                    "error_step": "payment_authorization",
                    "error_reason": "gateway_timeout",
                }
            }
        },
        "created_at": 1,
    }


def _captured_body(payment_id: str, order_id: str, amount: int) -> dict:
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
                    "status": "captured",
                }
            }
        },
        "created_at": 2,
    }


async def test_root_and_health(client):
    root = await client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "PayRecover"

    health = await client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "healthy"
    # No keys in the test env -> simulation mode.
    assert body["razorpay_mode"] == "simulation"
    assert body["simulation_mode"] is True


async def test_inject_creates_events(client):
    r = await client.post("/api/simulator/inject", json={"count": 3, "diagnose": False})
    assert r.status_code == 200
    assert r.json()["injected"] == 3

    events = (await client.get("/api/dashboard/events")).json()
    assert events["total"] == 3
    for ev in events["events"]:
        assert ev["is_simulated"] is True
        assert ev["recovery_status"] == "pending"
        assert ev["amount"] > 0


async def test_inject_specific_type(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "card_expired", "amount": 99900, "method": "card"},
    )
    assert r.status_code == 200
    ev = r.json()["events"][0]
    assert ev["error_reason"] == "card_expired"
    assert ev["amount"] == 99900
    assert ev["payment_method"] == "card"


async def test_event_detail_shape(client):
    r = await client.post("/api/simulator/inject", json={"count": 1, "diagnose": False})
    event_id = r.json()["events"][0]["id"]

    detail = await client.get(f"/api/dashboard/events/{event_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["id"] == event_id
    assert data["actions"] == []
    assert data["circuit_breaker_events"] == []

    # Unknown id -> 404; malformed id -> 400.
    assert (await client.get("/api/dashboard/events/00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await client.get("/api/dashboard/events/not-a-uuid")).status_code == 400


async def test_metrics_after_recovery(client):
    await client.post("/webhooks/razorpay", json=_failed_body("pay_m1", "order_m1", 200000))
    await client.post("/webhooks/razorpay", json=_captured_body("pay_m1b", "order_m1", 200000))

    metrics = (await client.get("/api/dashboard/metrics")).json()
    assert metrics["total_events"] == 1
    assert metrics["recovered_count"] == 1
    assert metrics["recovered_amount_paise"] == 200000
    assert metrics["recovery_rate_by_amount_pct"] == 100.0
    assert metrics["status_breakdown"].get("recovered") == 1


async def test_profiles_listed(client):
    r = await client.get("/api/simulator/profiles")
    assert r.status_code == 200
    types = {p["type"] for p in r.json()["profiles"]}
    assert "gateway_timeout" in types and "risk_flagged" in types
