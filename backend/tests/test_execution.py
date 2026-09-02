"""Execution phase tests — the circuit breakers (CB-001..008), the idempotent
executor across all six tools, the deterministic due-action core, the HITL
approve/modify/skip queue (including the compliance re-check on a merchant edit),
and the audit trail endpoints.

Everything here runs with **no credentials**: the Razorpay client is in
simulation mode and the LLM falls back to the deterministic planner, so the whole
recovery loop is exercised offline. Timing is always injected via ``now`` rather
than read from the wall clock — the TRAI window and the retry schedule are
real constraints, and a test that only passes between 9 AM and 8 PM IST is not a
test.

Choosing a failure profile is not cosmetic
------------------------------------------
Tests that drive the whole pipeline through ``POST /api/simulator/inject`` can
only assert on the gate's routing if that routing is deterministic. The injector
invents a *random* customer, and the enricher derives that customer's synthetic
payment history from a hash of their identity — so a strong history (+15
recoverability, +5 confidence) or a poor one (-15/-5) is drawn essentially at
random, which moves most failure types across two or three confidence bands from
run to run. Two injections are routing-stable for every possible history:

* ``bank_downtime`` under Rs 10,000 — soft failure (+20/+10) plus active issuer
  downtime (+10) yields confidence 70/90/100, so the gate always says
  ``approved`` and the planner always picks ``schedule_smart_retry``.
* anything at Rs 15,000 — the high-value override pins it to ``pending_review``
  however confident the agent is (that is the point of the override).

So those two are what the routing assertions use. A Rs 2,500
``insufficient_funds`` can legitimately land in any band, and is only used where
the assertion does not depend on where it landed.
"""
import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agent import confidence
from app.diagnosis import classifier, enricher
from app.execution import statuses
from app.execution.circuit_breakers import (
    COST_CEILING_FRACTION,
    MAX_RETRIES,
    MAX_WINDOW_DAYS,
    check_circuit_breakers,
    evaluate_breakers,
    notification_window,
)
from app.execution.executor import execute_action
from app.execution.scheduler import run_due_actions
from app.models import CircuitBreakerEvent, RecoveryAction, RecoveryEvent
from app.strategy import planner

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _ev_dict(**overrides):
    """A minimal ``event_to_dict``-shaped payload for the pure breaker rules.

    ``created_at`` is deliberately *relative to now* rather than a fixed date:
    CB-006 closes recovery after 14 days, so a hardcoded timestamp would make
    every "healthy event" assertion here start failing once the calendar moved
    past it. Tests that care about the window pass an explicit ``now``.
    """
    base = {
        "id": str(uuid.uuid4()),
        "amount": 250000,
        "recovery_status": statuses.EV_IN_PROGRESS,
        "recovery_attempts": 0,
        "recovery_cost_paise": 0,
        "has_dispute": False,
        "customer_opted_out": False,
        "subscription_cancelled": False,
        "created_at": datetime.now(IST).isoformat(),
    }
    base.update(overrides)
    return base


async def _make_event(session, **overrides):
    """Persist a diagnosed failure ready for execution."""
    suffix = uuid.uuid4().hex[:10]
    fields = {
        "razorpay_payment_id": f"pay_test_{suffix}",
        "razorpay_order_id": f"order_test_{suffix}",
        "event_type": "payment.failed",
        "amount": 250000,
        "currency": "INR",
        "payment_method": "card",
        "customer_email": "aarav.sharma@example.com",
        "customer_contact": "+919812345678",
        "customer_name": "Aarav Sharma",
        "error_reason": "insufficient_funds",
        "error_source": "customer",
        "failure_category": "hard",
        "failure_label": "Insufficient Funds",
        "recoverability_score": 70,
        "recovery_status": statuses.EV_DIAGNOSED,
        "is_simulated": True,
    }
    fields.update(overrides)
    event = RecoveryEvent(**fields)
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def _make_action(session, event, action_type, params=None, **overrides):
    """Persist an approved strategy action, as the decision phase would."""
    fields = {
        "recovery_event_id": event.id,
        "agent_name": "strategy",
        "action_type": action_type,
        "action_params": params or {},
        "agent_reasoning": "test",
        "confidence_score": 88,
        "compliance_decision": "APPROVED",
        "status": statuses.APPROVED,
        "cost_paise": 0,
    }
    fields.update(overrides)
    action = RecoveryAction(**fields)
    session.add(action)
    await session.commit()
    await session.refresh(action)
    return action


# Fixed instants inside and outside the TRAI messaging window, anchored to
# today's date so nothing here drifts out of the CB-006 recovery window. Every
# execution test injects one of these rather than reading the clock — a
# notification test that only passes between 9 AM and 8 PM IST is not a test.
_TODAY_IST = datetime.now(IST).date()
IN_WINDOW = datetime.combine(_TODAY_IST, datetime.min.time(), tzinfo=IST).replace(hour=11)
OUT_OF_WINDOW = IN_WINDOW.replace(hour=23, minute=30)


def _as_utc(dt):
    """Read a timestamp the way the app stores it: UTC, naive-means-UTC.

    SQLite discards the offset on write, so anything read back out of a datetime
    column is naive. Calling ``.astimezone()`` on that would quietly adopt the
    *test machine's* timezone and make the assertion pass or fail depending on
    where it runs.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# --------------------------------------------------------------------------- #
# Determinism invariants the pipeline tests below stand on
# --------------------------------------------------------------------------- #
#: Every success rate the enricher can synthesize (0.35-0.98 in 1% steps).
_SYNTHETIC_RATES = [round(0.35 + n / 100.0, 2) for n in range(64)]

#: Boundaries worth sweeping: night/day either side of the 6 AM and 22:00 cuts,
#: and days either side of the month-end salary-credit rule.
_SWEEP_HOURS = (0, 5, 6, 11, 20, 22, 23)
_SWEEP_DAYS = (1, 24, 25, 31)


def _route(reason, source, amount, rate, *, hour, day):
    """Run the deterministic decision path once and report (tool, gate action).

    Mirrors ``strategy_agent.build_strategy_payload`` -> ``planner.plan`` ->
    ``confidence.evaluate``, which is the path taken when no Groq key is present.
    """
    event = {
        "razorpay_payment_id": "pay_determinism",
        "razorpay_order_id": "order_determinism",
        "amount": amount,
        "amount_inr": amount / 100,
        "currency": "INR",
        "payment_method": "card",
        "error_code": "X",
        "error_source": source,
        "error_step": "payment_authorization",
        "error_reason": reason,
        "error_description": "d",
        "customer_name": "Aarav Sharma",
        "customer_email": "aarav.sharma@example.com",
        "customer_contact": "+919812345678",
        "customer_dnd": False,
    }
    history = {
        "total_payments": 10,
        "successful_payments": int(10 * rate),
        "success_rate": rate,
        "tenure_days": 200,
        "returning_customer": True,
    }
    current_time = {
        # A fixed 31-day month: the sweep includes day 31, which replace() on
        # today's date would reject in February.
        "iso": datetime(2026, 1, day, hour, tzinfo=IST).isoformat(),
        "hour": hour,
        "day_of_month": day,
        "is_night": hour >= 22 or hour < 6,
        "age_days": 0.0,
    }
    diagnosis = classifier.diagnose(
        {
            "event": event,
            "customer_history": history,
            "bank_status": enricher.bank_status(event),
            "current_time": current_time,
            "prior_attempts": 0,
        }
    )
    tool, _args, meta = planner.plan(
        {
            "diagnostic": diagnosis,
            "event": {**event, "failure_label": diagnosis["failure_label"]},
            "customer_history": history,
            "prior_attempts": 0,
            "current_time": current_time,
        }
    )
    return tool, confidence.evaluate(meta["confidence"], amount).action


def test_bank_downtime_injection_always_auto_approves_a_retry():
    """Guards the routing assumption every /inject test below is built on.

    The injector invents a random customer and the enricher hashes that identity
    into a synthetic payment history, so a strong history (+15/+5) or a poor one
    (-15/-5) is effectively drawn at random. Bank downtime is the one sub-Rs
    10,000 profile that clears the 70-point auto-execute threshold on *every*
    possible draw: soft (+20/+10) plus active issuer downtime (+10) puts the
    floor at 70. If a confidence weight moves, this test says so directly
    instead of leaving the pipeline tests to fail once every few runs.
    """
    for day in _SWEEP_DAYS:
        for hour in _SWEEP_HOURS:
            for rate in _SYNTHETIC_RATES:
                tool, action = _route(
                    "issuer_bank_down", "gateway", 250000, rate, hour=hour, day=day
                )
                assert tool == "schedule_smart_retry"
                assert action in (
                    confidence.AUTO_EXECUTE,
                    confidence.AUTO_EXECUTE_FLAGGED,
                ), f"success_rate={rate} hour={hour} day={day} routed to {action}"


def test_high_value_injection_always_needs_a_human():
    """The other fixed point: Rs 15,000 is pinned to review at any confidence."""
    for day in _SWEEP_DAYS:
        for hour in _SWEEP_HOURS:
            for rate in _SYNTHETIC_RATES:
                tool, action = _route(
                    "insufficient_funds", "customer", 15_00_000, rate, hour=hour, day=day
                )
                assert tool == "generate_payment_link"
                assert action == confidence.HITL_REVIEW, (
                    f"success_rate={rate} hour={hour} day={day} routed to {action}"
                )


# --------------------------------------------------------------------------- #
# Circuit breakers — the pure rules
# --------------------------------------------------------------------------- #
def test_cb001_recovered_trips_first():
    trip = evaluate_breakers(_ev_dict(recovery_status=statuses.EV_RECOVERED))
    assert trip is not None
    assert trip.breaker_id == "CB-001"
    assert trip.only_cancel_types is None  # cancels everything


def test_cb002_dispute_halts():
    trip = evaluate_breakers(_ev_dict(has_dispute=True))
    assert trip.breaker_id == "CB-002"
    assert trip.event_status == statuses.EV_HALTED


def test_cb003_opt_out_halts():
    trip = evaluate_breakers(_ev_dict(customer_opted_out=True))
    assert trip.breaker_id == "CB-003"
    assert trip.event_status == statuses.EV_HALTED


def test_cb004_subscription_cancelled_halts():
    trip = evaluate_breakers(_ev_dict(subscription_cancelled=True))
    assert trip.breaker_id == "CB-004"


def test_cb005_allows_the_third_retry_and_stops_the_fourth():
    """recovery_attempts is incremented on authorisation, so 3 is *at* the NPCI
    ceiling, not past it. Tripping at 3 would cancel a retry policy permits."""
    assert evaluate_breakers(_ev_dict(recovery_attempts=MAX_RETRIES)) is None
    trip = evaluate_breakers(_ev_dict(recovery_attempts=MAX_RETRIES + 1))
    assert trip.breaker_id == "CB-005"
    # Hitting the cap leaves the case notification-only, not closed.
    assert trip.only_cancel_types == ("schedule_smart_retry",)
    assert trip.event_status is None


def test_cb006_window_expiry():
    now = datetime(2026, 3, 5, 12, 0, tzinfo=IST)
    old = (now - timedelta(days=MAX_WINDOW_DAYS + 1)).isoformat()
    trip = evaluate_breakers(_ev_dict(created_at=old), now=now)
    assert trip.breaker_id == "CB-006"
    assert trip.event_status == statuses.EV_ESCALATED

    fresh = (now - timedelta(days=MAX_WINDOW_DAYS - 1)).isoformat()
    assert evaluate_breakers(_ev_dict(created_at=fresh), now=now) is None


def test_cb007_negative_economics():
    amount = 100000  # Rs 1,000
    ceiling = int(amount * COST_CEILING_FRACTION)
    assert evaluate_breakers(_ev_dict(amount=amount, recovery_cost_paise=ceiling)) is None
    trip = evaluate_breakers(
        _ev_dict(amount=amount, recovery_cost_paise=ceiling + 100)
    )
    assert trip.breaker_id == "CB-007"
    assert trip.event_status == statuses.EV_ESCALATED


def test_breaker_precedence_recovered_beats_dispute():
    """Good news wins: a recovered payment is reported as CB-001, not CB-002."""
    trip = evaluate_breakers(
        _ev_dict(recovery_status=statuses.EV_RECOVERED, has_dispute=True)
    )
    assert trip.breaker_id == "CB-001"


def test_no_breaker_on_a_healthy_event():
    assert evaluate_breakers(_ev_dict()) is None


def test_cb008_notification_window():
    allowed, nxt = notification_window(IN_WINDOW)
    assert allowed is True and nxt is None

    # 11:30 PM -> tomorrow at 9 AM IST.
    allowed, nxt = notification_window(OUT_OF_WINDOW)
    assert allowed is False
    assert nxt.astimezone(IST).hour == 9
    assert nxt.astimezone(IST).date() == (OUT_OF_WINDOW + timedelta(days=1)).date()

    # 6 AM -> *today* at 9 AM, not tomorrow. Getting this backwards would delay
    # every early-morning nudge by a full day.
    allowed, nxt = notification_window(IN_WINDOW.replace(hour=6))
    assert allowed is False
    assert nxt.astimezone(IST).hour == 9
    assert nxt.astimezone(IST).date() == IN_WINDOW.date()

    # The 9 AM and 8 PM boundaries are inclusive.
    assert notification_window(IN_WINDOW.replace(hour=9))[0] is True
    assert notification_window(IN_WINDOW.replace(hour=20))[0] is True


# --------------------------------------------------------------------------- #
# Circuit breakers — the stateful wrapper
# --------------------------------------------------------------------------- #
async def test_breaker_cancels_pending_work_and_logs_an_audit_row(session):
    event = await _make_event(session, recovery_status=statuses.EV_RECOVERED)
    retry = await _make_action(session, event, "schedule_smart_retry")
    nudge = await _make_action(session, event, "send_recovery_notification")

    trip = await check_circuit_breakers(session, event, trigger_source="test")

    assert trip.breaker_id == "CB-001"
    await session.refresh(retry)
    await session.refresh(nudge)
    assert retry.status == statuses.CANCELLED
    assert nudge.status == statuses.CANCELLED
    assert "CB-001" in retry.result["cancelled_reason"]

    logged = (
        await session.scalars(
            select(CircuitBreakerEvent).where(
                CircuitBreakerEvent.recovery_event_id == event.id
            )
        )
    ).all()
    assert len(logged) == 1
    assert logged[0].trigger_id == "CB-001"
    assert logged[0].cancelled_actions == 2


async def test_cb005_spares_notifications(session):
    event = await _make_event(session, recovery_attempts=MAX_RETRIES + 1)
    retry = await _make_action(session, event, "schedule_smart_retry")
    nudge = await _make_action(session, event, "send_recovery_notification")

    trip = await check_circuit_breakers(session, event)

    assert trip.breaker_id == "CB-005"
    await session.refresh(retry)
    await session.refresh(nudge)
    assert retry.status == statuses.CANCELLED
    assert nudge.status == statuses.APPROVED  # notification-only from here


async def test_breaker_never_walks_back_a_final_outcome(session):
    """A dispute arriving after the money landed must not un-recover the event."""
    event = await _make_event(session, recovery_status=statuses.EV_RECOVERED, has_dispute=True)
    await check_circuit_breakers(session, event)
    await session.refresh(event)
    assert event.recovery_status == statuses.EV_RECOVERED


# --------------------------------------------------------------------------- #
# Executor — the six tool dispatches
# --------------------------------------------------------------------------- #
async def test_execute_payment_link(session):
    event = await _make_event(session)
    action = await _make_action(
        session, event, "generate_payment_link", {"amount_paise": 250000, "expiry_hours": 24}
    )

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is True
    assert out["status"] == statuses.COMPLETED
    assert out["result"]["payment_link_url"]
    assert out["result"]["simulated"] is True
    await session.refresh(action)
    await session.refresh(event)
    assert action.executed_at is not None
    assert event.recovery_status == statuses.EV_IN_PROGRESS


async def test_execute_alternative_method_tags_the_link(session):
    event = await _make_event(session)
    action = await _make_action(
        session, event, "offer_alternative_method", {"suggested_method": "upi"}
    )

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is True
    notes = out["result"]["razorpay_response"].get("notes") or {}
    assert notes.get("preferred_method") == "upi"


async def test_execute_retry_schedules_rather_than_completes(session):
    event = await _make_event(session, failure_category="soft")
    retry_at = IN_WINDOW + timedelta(minutes=30)
    action = await _make_action(
        session, event, "schedule_smart_retry", {"retry_at": retry_at.isoformat()}
    )

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is True
    assert out["status"] == statuses.SCHEDULED  # arranged, not yet fired
    assert out["result"]["retry_order_id"]
    await session.refresh(action)
    assert action.scheduled_at is not None


async def test_retry_at_without_an_offset_is_read_as_ist_not_utc(session):
    """A bare timestamp from the LLM means Indian wall-clock time.

    The Strategy Agent's prompt states the current time in IST, so the model
    answers in IST — and it routinely answers *without* an offset. If that naive
    value were stored as-is, the scheduler (which compares in UTC) would read it
    as UTC and fire the retry 5.5 hours late. Worse, SQLite discards offsets on
    write, so the mistake would be invisible in the row itself.

    Pinning the expected instant, rather than just asserting non-null, is what
    makes this a regression test: it fails if anyone drops the ``to_utc_from_ist``
    normalisation on the write path.
    """
    event = await _make_event(session, failure_category="soft")
    naive_ist = (IN_WINDOW + timedelta(hours=2)).replace(tzinfo=None)  # 13:00, no zone
    action = await _make_action(
        session, event, "schedule_smart_retry", {"retry_at": naive_ist.isoformat()}
    )

    await execute_action(session, action, event=event, now=IN_WINDOW)
    await session.refresh(action)

    assert _as_utc(action.scheduled_at).astimezone(IST).hour == 13


async def test_execute_notification_in_window_sends(session):
    event = await _make_event(session)
    action = await _make_action(
        session, event, "send_recovery_notification", {"channel": "email"}
    )

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is True
    assert out["status"] == statuses.COMPLETED
    assert out["result"]["channel"] == "email"


async def test_execute_notification_uses_an_existing_payment_link(session):
    event = await _make_event(session)
    link = await _make_action(session, event, "generate_payment_link", {})
    await execute_action(session, link, event=event, now=IN_WINDOW)

    nudge = await _make_action(
        session, event, "send_recovery_notification", {"channel": "sms"}
    )
    out = await execute_action(session, nudge, event=event, now=IN_WINDOW)

    assert out["result"]["delivery"] == "razorpay_payment_link_notify"
    assert out["result"]["payment_link_id"]


async def test_execute_escalate(session):
    event = await _make_event(session)
    action = await _make_action(
        session, event, "escalate_to_merchant", {"severity": "high"}
    )

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is True
    await session.refresh(event)
    assert event.recovery_status == statuses.EV_ESCALATED


async def test_execute_mark_unrecoverable_closes_the_case(session):
    event = await _make_event(session)
    action = await _make_action(
        session, event, "mark_unrecoverable", {"reason": "terminal failure"}
    )

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is True
    await session.refresh(event)
    assert event.recovery_status == statuses.EV_UNRECOVERABLE


# --------------------------------------------------------------------------- #
# Executor — safety properties
# --------------------------------------------------------------------------- #
async def test_execution_is_idempotent(session):
    """The single most important property: four callers can race, nobody pays twice."""
    event = await _make_event(session)
    action = await _make_action(session, event, "generate_payment_link", {})

    first = await execute_action(session, action, event=event, now=IN_WINDOW)
    second = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert first["executed"] is True
    assert second["executed"] is False
    assert second["reason"] == "already_final"
    assert first["result"]["payment_link_id"]  # only one link was ever created


async def test_executor_refuses_an_unapproved_action(session):
    event = await _make_event(session)
    action = await _make_action(
        session, event, "generate_payment_link", {}, status=statuses.PENDING_REVIEW
    )

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is False
    assert out["reason"] == "not_approved"
    # ...unless a human explicitly says so.
    forced = await execute_action(session, action, event=event, now=IN_WINDOW, force=True)
    assert forced["executed"] is True


async def test_preflight_breaker_abandons_a_stale_action(session):
    """The customer paid between deciding and acting — do not nag them."""
    event = await _make_event(session, recovery_status=statuses.EV_RECOVERED)
    action = await _make_action(session, event, "send_recovery_notification", {})

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is False
    assert out["reason"] == "circuit_breaker"
    assert out["breaker_id"] == "CB-001"
    await session.refresh(action)
    assert action.status == statuses.CANCELLED


async def test_notification_outside_trai_window_is_deferred_not_dropped(session):
    event = await _make_event(session)
    action = await _make_action(session, event, "send_recovery_notification", {})

    out = await execute_action(session, action, event=event, now=OUT_OF_WINDOW)

    assert out["executed"] is False
    assert out["reason"] == "deferred"
    await session.refresh(action)
    assert action.status == statuses.SCHEDULED
    assert action.scheduled_at is not None
    # Datetime columns are stored in UTC (see app/timeutil.py), and SQLite hands
    # them back naive — so pin the timezone explicitly rather than letting
    # .astimezone() assume the machine's local zone. Without this the assertion
    # silently depends on the developer's clock being set to IST.
    assert _as_utc(action.scheduled_at).astimezone(IST).hour == 9
    assert "CB-008" in action.result["deferred_reason"]


async def test_unknown_tool_is_reported_not_raised(session):
    event = await _make_event(session)
    action = await _make_action(session, event, "teleport_the_money", {})

    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is False
    assert out["reason"] == "unknown_tool"


async def test_execution_failure_is_recorded(session, monkeypatch):
    from app.razorpay import client as rp

    async def boom(*args, **kwargs):
        raise RuntimeError("Razorpay is having a day")

    monkeypatch.setattr(rp.razorpay_client, "create_payment_link", boom)

    event = await _make_event(session)
    action = await _make_action(session, event, "generate_payment_link", {})
    out = await execute_action(session, action, event=event, now=IN_WINDOW)

    assert out["executed"] is False
    assert out["reason"] == "error"
    await session.refresh(action)
    assert action.status == statuses.FAILED
    assert "Razorpay is having a day" in action.result["error"]


# --------------------------------------------------------------------------- #
# The deterministic due-action core
# --------------------------------------------------------------------------- #
async def test_run_due_actions_fires_a_due_retry(session):
    event = await _make_event(session, failure_category="soft")
    retry_at = IN_WINDOW + timedelta(minutes=30)
    action = await _make_action(
        session, event, "schedule_smart_retry", {"retry_at": retry_at.isoformat()}
    )
    await execute_action(session, action, event=event, now=IN_WINDOW)

    # Not yet due.
    assert await run_due_actions(session, now=IN_WINDOW) == []

    fired = await run_due_actions(session, now=retry_at + timedelta(minutes=1))

    assert len(fired) == 1
    assert fired[0]["fired"] is True
    await session.refresh(action)
    assert action.status == statuses.COMPLETED
    assert action.result["outcome"] == "awaiting_payment_result"


async def test_run_due_actions_cancels_work_the_breakers_now_forbid(session):
    event = await _make_event(session, failure_category="soft")
    retry_at = IN_WINDOW + timedelta(minutes=30)
    action = await _make_action(
        session, event, "schedule_smart_retry", {"retry_at": retry_at.isoformat()}
    )
    await execute_action(session, action, event=event, now=IN_WINDOW)

    # The customer pays while the retry sits in the queue.
    event.recovery_status = statuses.EV_RECOVERED
    await session.commit()

    fired = await run_due_actions(session, now=retry_at + timedelta(minutes=1))

    assert len(fired) == 1
    assert fired[0]["fired"] is False
    assert fired[0]["breaker_id"] == "CB-001"
    await session.refresh(action)
    assert action.status == statuses.CANCELLED


async def test_deferred_notification_is_sent_when_its_window_opens(session):
    event = await _make_event(session)
    action = await _make_action(session, event, "send_recovery_notification", {})
    await execute_action(session, action, event=event, now=OUT_OF_WINDOW)
    await session.refresh(action)
    assert action.status == statuses.SCHEDULED

    fired = await run_due_actions(session, now=action.scheduled_at + timedelta(minutes=5))

    assert len(fired) == 1
    assert fired[0]["fired"] is True
    await session.refresh(action)
    assert action.status == statuses.COMPLETED


# --------------------------------------------------------------------------- #
# HITL queue
# --------------------------------------------------------------------------- #
async def test_hitl_pending_lists_held_actions(client):
    # Rs 15,000 -> the high-value override pins this to human review.
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 15_00_000},
    )

    body = (await client.get("/api/hitl/pending")).json()

    assert body["total"] == 1
    item = body["pending"][0]
    assert item["proposed_action"] == "generate_payment_link"
    assert item["amount_inr"] == 15000.0
    assert item["compliance"]["decision"] in ("APPROVED", "MODIFIED")
    assert item["reasoning"]


async def test_hitl_approve_executes(client):
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 15_00_000},
    )
    action_id = (await client.get("/api/hitl/pending")).json()["pending"][0]["action_id"]

    r = await client.post(f"/api/hitl/{action_id}/approve")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["execution"]["executed"] is True
    assert (await client.get("/api/hitl/pending")).json()["total"] == 0


async def test_hitl_skip_does_not_execute(client):
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 15_00_000},
    )
    action_id = (await client.get("/api/hitl/pending")).json()["pending"][0]["action_id"]

    r = await client.post(
        f"/api/hitl/{action_id}/skip", json={"reason": "customer called in"}
    )

    assert r.status_code == 200
    detail = (await client.get(f"/api/actions/{action_id}")).json()
    assert detail["status"] == statuses.SKIPPED
    assert detail["executed_at"] is None


async def test_hitl_modify_reruns_compliance_and_executes(client):
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 15_00_000},
    )
    action_id = (await client.get("/api/hitl/pending")).json()["pending"][0]["action_id"]

    r = await client.post(
        f"/api/hitl/{action_id}/modify",
        json={"params": {"expiry_hours": 12}, "note": "shorter window"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["params"]["expiry_hours"] == 12
    assert body["compliance"]["decision"] in ("APPROVED", "MODIFIED")
    assert body["execution"]["executed"] is True


async def test_hitl_modify_downgrades_a_dnd_sms_to_email(client, session):
    """A human may overrule the agent; nobody hand-edits past the DND registry.

    The merchant asks for an SMS to a DND-registered customer. The engine does
    not argue and does not refuse — it silently rewrites the channel to email,
    and the correction wins over the merchant's edit.
    """
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 15_00_000},
    )
    pending = (await client.get("/api/hitl/pending")).json()["pending"][0]
    action_id = pending["action_id"]

    event = await session.get(RecoveryEvent, uuid.UUID(pending["recovery_event_id"]))
    event.customer_dnd = True
    action = await session.get(RecoveryAction, uuid.UUID(action_id))
    action.action_type = "send_recovery_notification"
    await session.commit()

    r = await client.post(
        f"/api/hitl/{action_id}/modify", json={"params": {"channel": "sms"}}
    )

    assert r.status_code == 200
    body = r.json()
    assert body["compliance"]["decision"] == "MODIFIED"
    assert body["compliance"]["rule_id"] == "DND-001"
    assert body["params"]["channel"] == "email"  # the merchant asked for sms


async def test_hitl_modify_cannot_smuggle_a_blocked_edit(client, session):
    """A dispute halts everything — editing the params does not get you past it."""
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 15_00_000},
    )
    pending = (await client.get("/api/hitl/pending")).json()["pending"][0]
    action_id = pending["action_id"]

    event = await session.get(RecoveryEvent, uuid.UUID(pending["recovery_event_id"]))
    event.has_dispute = True
    await session.commit()

    r = await client.post(
        f"/api/hitl/{action_id}/modify", json={"params": {"expiry_hours": 72}}
    )

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["rule_id"] == "DISP-001"
    after = (await client.get(f"/api/actions/{action_id}")).json()
    assert after["status"] == statuses.BLOCKED
    assert after["executed_at"] is None


async def test_hitl_rejects_an_action_not_awaiting_review(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )
    action_id = r.json()["events"][0]["recovery"]["action_id"]

    resp = await client.post(f"/api/hitl/{action_id}/approve")

    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Action endpoints
# --------------------------------------------------------------------------- #
async def test_execute_endpoint_is_idempotent(client, session):
    """Two identical POSTs, one payment link. The second is a recorded no-op.

    Built below the pipeline on purpose: the endpoint's contract is what is under
    test, so the action's starting status has to be a given rather than whatever
    the gate happened to decide for a randomly invented customer.
    """
    event = await _make_event(session)
    action = await _make_action(
        session,
        event,
        "generate_payment_link",
        {"amount_paise": 250000, "expiry_hours": 24},
    )
    body = {"now": IN_WINDOW.isoformat()}

    first = (await client.post(f"/api/actions/{action.id}/execute", json=body)).json()
    second = (await client.post(f"/api/actions/{action.id}/execute", json=body)).json()

    assert first["executed"] is True
    assert first["status"] == statuses.COMPLETED
    assert first["result"]["payment_link_url"]
    assert second["executed"] is False
    assert second["reason"] == "already_final"


async def test_execute_endpoint_404s_on_an_unknown_action(client):
    r = await client.post(f"/api/actions/{uuid.uuid4()}/execute")
    assert r.status_code == 404


async def test_scheduled_listing_shows_queued_retries(client):
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )
    body = (await client.get("/api/actions/scheduled")).json()

    assert body["total"] == 1
    assert body["scheduled"][0]["action_type"] == "schedule_smart_retry"
    assert body["scheduled"][0]["scheduled_at"]


async def test_inject_execute_false_leaves_the_action_unexecuted(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000, "execute": False},
    )
    ev = r.json()["events"][0]
    assert ev["recovery"]["status"] == statuses.APPROVED
    assert "execution" not in ev


async def test_inject_auto_executes_an_approved_action(client):
    """The gate cleared it, so /inject runs it — no second call needed.

    A bank-downtime retry executes into ``scheduled``, not ``completed``:
    executing a retry means *arranging* it (a fresh order to charge against) and
    recording when it should fire. ``executed_at`` is what proves the work
    happened.
    """
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )
    ev = r.json()["events"][0]

    assert ev["recovery"]["status"] == statuses.APPROVED
    assert ev["recovery"]["executed"] is True
    assert ev["execution"]["status"] == statuses.SCHEDULED
    assert ev["execution"]["result"]["retry_order_id"]
    assert ev["execution"]["executed_at"]


# --------------------------------------------------------------------------- #
# Webhook + simulator circuit-event wiring
# --------------------------------------------------------------------------- #
async def test_captured_webhook_trips_cb001_and_cancels_the_retry(client):
    """The headline safety story, end to end."""
    inject = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )
    ev = inject.json()["events"][0]
    action_id = ev["recovery"]["action_id"]
    assert (await client.get(f"/api/actions/{action_id}")).json()["status"] == (
        statuses.SCHEDULED
    )

    webhook = await client.post(
        "/webhooks/razorpay",
        json={
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": ev["razorpay_payment_id"],
                        "order_id": ev["razorpay_order_id"],
                        "amount": ev["amount"],
                    }
                }
            },
        },
    )

    assert webhook.status_code == 200
    body = webhook.json()
    assert body["affected"] == 1
    assert body["breakers_tripped"][0]["breaker_id"] == "CB-001"
    after = (await client.get(f"/api/actions/{action_id}")).json()
    assert after["status"] == statuses.CANCELLED


async def test_opt_out_event_trips_cb003(client):
    # Routing-agnostic: whichever band the gate picks, the action is queued and
    # the breaker's job is to close the channel on it.
    inject = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 250000, "execute": False},
    )
    ev = inject.json()["events"][0]

    r = await client.post(
        "/api/simulator/circuit-event",
        json={"event_type": "customer.opted_out", "order_id": ev["razorpay_order_id"]},
    )

    assert r.status_code == 200
    assert r.json()["breakers_tripped"][0]["breaker_id"] == "CB-003"
    detail = (await client.get(f"/api/dashboard/events/{ev['id']}")).json()
    assert detail["customer_opted_out"] is True
    assert detail["recovery_status"] == statuses.EV_HALTED


async def test_dispute_event_trips_cb002(client):
    inject = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 250000, "execute": False},
    )
    ev = inject.json()["events"][0]

    r = await client.post(
        "/api/simulator/circuit-event",
        json={
            "event_type": "payment.dispute.created",
            "order_id": ev["razorpay_order_id"],
        },
    )

    assert r.json()["breakers_tripped"][0]["breaker_id"] == "CB-002"


async def test_run_due_actions_endpoint_fast_forwards(client):
    inject = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )
    action_id = inject.json()["events"][0]["recovery"]["action_id"]
    scheduled_at = (await client.get(f"/api/actions/{action_id}")).json()["scheduled_at"]
    later = datetime.fromisoformat(scheduled_at) + timedelta(minutes=5)

    r = await client.post("/api/simulator/run-due-actions", json={"now": later.isoformat()})

    assert r.status_code == 200
    assert r.json()["fired"] == 1
    assert (await client.get(f"/api/actions/{action_id}")).json()["status"] == (
        statuses.COMPLETED
    )


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #
async def test_audit_log_carries_the_full_reasoning_chain(client):
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )

    body = (await client.get("/api/audit/log")).json()

    assert body["total"] == 2  # diagnostic + strategy
    strategy = next(e for e in body["entries"] if e["agent_name"] == "strategy")
    assert strategy["razorpay_order_id"]
    assert strategy["amount_inr"] == 2500.0
    assert strategy["compliance_decision"] in ("APPROVED", "MODIFIED")
    assert isinstance(strategy["confidence_score"], int)
    assert strategy["gate"]["action"]
    assert strategy["action_type"] == "schedule_smart_retry"
    assert strategy["status"] == statuses.SCHEDULED  # a fired retry awaits its slot
    assert strategy["executed_at"]


async def test_audit_log_filters(client):
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )

    only_strategy = (await client.get("/api/audit/log?agent_name=strategy")).json()
    assert only_strategy["total"] == 1
    assert only_strategy["entries"][0]["agent_name"] == "strategy"

    by_tool = (
        await client.get("/api/audit/log?action_type=schedule_smart_retry")
    ).json()
    assert by_tool["total"] == 1

    assert (await client.get("/api/audit/log?agent_name=nobody")).json()["total"] == 0


async def test_audit_log_rejects_a_bad_event_id(client):
    assert (await client.get("/api/audit/log?event_id=not-a-uuid")).status_code == 400


async def test_audit_export_csv_and_json(client):
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )

    csv_resp = await client.get("/api/audit/export?format=csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers["content-disposition"]
    # Parsed with the csv module, not splitlines(): agent reasoning is free text
    # and may legitimately contain newlines inside a quoted field.
    rows = list(csv.reader(io.StringIO(csv_resp.text)))
    assert rows[0][:2] == ["timestamp", "recovery_event_id"]
    assert len(rows) == 3  # header + diagnostic + strategy
    assert {r[7] for r in rows[1:]} == {"diagnostic", "strategy"}  # agent_name column

    json_resp = await client.get("/api/audit/export?format=json")
    assert json_resp.status_code == 200
    assert len(json_resp.json()["entries"]) == 2

    assert (await client.get("/api/audit/export?format=xml")).status_code == 422


async def test_audit_export_is_never_truncated(client, session):
    """The export defaults to the WHOLE filtered chain, not a 500-row page.

    Regression: the endpoint used to default ``limit=500`` and the dashboard
    never passed one, so once the audit log passed 500 rows the downloaded
    file silently had fewer entries than the drawer said existed — an
    authoritative-looking file that was wrong. Bulk rows are inserted directly
    (the pipeline need not run 500 times to prove a query has no ceiling).
    """
    ev = RecoveryEvent(
        razorpay_payment_id="pay_bulk", razorpay_order_id="order_bulk",
        event_type="payment.failed", amount=250000,
    )
    session.add(ev)
    await session.flush()
    session.add_all(
        RecoveryAction(
            recovery_event_id=ev.id, agent_name="strategy",
            action_type="generate_payment_link", action_params={},
            status="completed",
        )
        for _ in range(600)
    )
    await session.commit()

    csv_resp = await client.get("/api/audit/export?format=csv")
    assert csv_resp.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_resp.text)))
    assert len(rows) == 601  # header + 600 — no 500-row ceiling

    json_resp = await client.get("/api/audit/export?format=json")
    assert len(json_resp.json()["entries"]) == 600

    # An explicit limit is still honoured for callers who want a bounded file.
    bounded = await client.get("/api/audit/export?format=json&limit=10")
    assert len(bounded.json()["entries"]) == 10


async def test_breaker_log_endpoint(client):
    inject = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "bank_downtime", "amount": 250000},
    )
    ev = inject.json()["events"][0]
    await client.post(
        "/api/simulator/circuit-event",
        json={"event_type": "payment.captured", "order_id": ev["razorpay_order_id"]},
    )

    body = (await client.get("/api/audit/breakers")).json()

    assert body["total"] == 1
    assert body["breakers"][0]["trigger_id"] == "CB-001"
    assert body["breakers"][0]["cancelled_actions"] == 1
    assert body["breakers"][0]["trigger_details"]["source"].startswith("simulator:")
