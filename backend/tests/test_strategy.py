"""Strategy phase tests — the deterministic planner (tool selection), the pure
Compliance Engine (all 8 rules), the confidence/HITL gate, and the full
inject -> diagnose -> strategy -> compliance -> gate -> persist path (mock LLM,
no Groq key)."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.agent import confidence
from app.compliance.engine import (
    ComplianceResult,
    check_compliance,
    is_peak_hour,
    next_non_peak_time,
)
from app.strategy import planner

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Planner — tool selection per failure category
# --------------------------------------------------------------------------- #
def _payload(
    category,
    reason,
    *,
    recoverability=70,
    prior_attempts=0,
    dnd=False,
    amount=250000,
    method="card",
    timing="immediate",
    email="aarav@example.com",
    contact="+919812345678",
    success_rate=0.7,
    label="Test Failure",
):
    return {
        "diagnostic": {
            "failure_category": category,
            "failure_label": label,
            "recoverability_score": recoverability,
            "recommended_timing": timing,
            "risk_factors": [],
            "timing_rationale": "",
        },
        "event": {
            "razorpay_order_id": "order_test_1",
            "amount": amount,
            "payment_method": method,
            "error_reason": reason,
            "customer_email": email,
            "customer_contact": contact,
            "customer_dnd": dnd,
            "failure_label": label,
        },
        "customer_history": {"success_rate": success_rate},
        "prior_attempts": prior_attempts,
        "current_time": {"iso": "2026-03-05T09:00:00+05:30", "hour": 9},
    }


def test_plan_soft_schedules_retry():
    tool, args, meta = planner.plan(_payload("soft", "gateway_timeout"))
    assert tool == "schedule_smart_retry"
    assert args["order_id"] == "order_test_1"
    assert args["retry_at"]  # a concrete IST timestamp
    assert 0 <= meta["confidence"] <= 100


def test_plan_soft_retry_cap_switches_to_link():
    # After 3 attempts we must not schedule another retry (NPCI cap).
    tool, args, meta = planner.plan(
        _payload("soft", "gateway_timeout", prior_attempts=3)
    )
    assert tool == "generate_payment_link"
    assert meta["confidence"] <= 65


def test_plan_hard_generates_payment_link():
    for reason in ("insufficient_funds", "card_expired", "invalid_otp", "mandate_inactive"):
        tool, args, _ = planner.plan(_payload("hard", reason))
        assert tool == "generate_payment_link", reason
        assert args["amount_paise"] == 250000
        assert 4 <= args["expiry_hours"] <= 168


def test_plan_terminal_risk_marks_unrecoverable():
    tool, _, meta = planner.plan(
        _payload("terminal", "payment_risk_threshold_breached", recoverability=20)
    )
    assert tool == "mark_unrecoverable"
    assert meta["confidence"] <= 40  # never present terminal as high-confidence


def test_plan_terminal_other_escalates():
    tool, args, _ = planner.plan(_payload("terminal", "integration_error"))
    assert tool == "escalate_to_merchant"
    assert args["severity"] == "high"


def test_plan_dnd_customer_gets_email_only():
    # DND hard failure -> payment link, but never notify over SMS (constraint 6).
    _, args, _ = planner.plan(_payload("hard", "insufficient_funds", dnd=True))
    assert args["notify_sms"] is False
    assert args["notify_email"] is True


# --------------------------------------------------------------------------- #
# Compliance Engine — all 8 rules
# --------------------------------------------------------------------------- #
def _event(**over):
    base = {
        "amount": 500000,
        "has_dispute": False,
        "customer_dnd": False,
        "created_at": None,
        "days_since_failure": 0,
    }
    base.update(over)
    return base


def test_npci_001_retry_cap_blocks():
    prior = [{"action_type": "schedule_smart_retry", "cost_paise": 0} for _ in range(3)]
    res = check_compliance(
        "schedule_smart_retry",
        {"retry_at": datetime(2026, 3, 5, 14, 0, tzinfo=IST)},
        _event(),
        prior,
    )
    assert res.decision == "BLOCKED"
    assert res.rule_id == "NPCI-001"


def test_npci_002_peak_hours_shifts_retry():
    peak = datetime(2026, 3, 5, 11, 0, tzinfo=IST)  # inside 10:00-13:00
    assert is_peak_hour(peak) is True
    res = check_compliance("schedule_smart_retry", {"retry_at": peak}, _event(), [])
    assert res.decision == "MODIFIED"
    assert res.rule_id == "NPCI-002"
    shifted = datetime.fromisoformat(res.modification["retry_at"])
    assert not is_peak_hour(shifted)
    assert shifted.astimezone(IST).hour == 13


def test_trai_001_notification_hours_shift():
    night = datetime(2026, 3, 5, 21, 30, tzinfo=IST)  # after 20:00
    res = check_compliance(
        "send_recovery_notification",
        {"channel": "email", "customer_contact": "+919812345678"},
        _event(),
        [],
        now=night,
    )
    assert res.decision == "MODIFIED"
    assert res.rule_id == "TRAI-001"


def test_freq_001_frequency_cap_blocks():
    now = datetime(2026, 3, 5, 10, 0, tzinfo=IST)
    prior = [
        {
            "action_type": "send_recovery_notification",
            "customer_contact": "+919812345678",
            "executed_at": (now - timedelta(hours=1)).isoformat(),
            "cost_paise": 0,
        }
    ]
    res = check_compliance(
        "send_recovery_notification",
        {"channel": "email", "customer_contact": "+919812345678"},
        _event(),
        prior,
        now=now,
    )
    assert res.decision == "BLOCKED"
    assert res.rule_id == "FREQ-001"


def test_dnd_001_switches_sms_to_email():
    now = datetime(2026, 3, 5, 10, 0, tzinfo=IST)
    res = check_compliance(
        "send_recovery_notification",
        {"channel": "sms", "customer_contact": "+919812345678"},
        _event(customer_dnd=True),
        [],
        now=now,
    )
    assert res.decision == "MODIFIED"
    assert res.rule_id == "DND-001"
    assert res.modification["channel"] == "email"


def test_disp_001_dispute_blocks_everything():
    res = check_compliance(
        "schedule_smart_retry",
        {"retry_at": datetime(2026, 3, 5, 14, 0, tzinfo=IST)},
        _event(has_dispute=True),
        [],
    )
    assert res.decision == "BLOCKED"
    assert res.rule_id == "DISP-001"


def test_window_001_expired_window_blocks():
    res = check_compliance(
        "schedule_smart_retry",
        {"retry_at": datetime(2026, 3, 5, 14, 0, tzinfo=IST)},
        _event(days_since_failure=20),
        [],
    )
    assert res.decision == "BLOCKED"
    assert res.rule_id == "WINDOW-001"


def test_cost_001_cost_ceiling_blocks():
    # Tiny order (Rs 1) — a single 20-paise SMS exceeds the 15% ceiling.
    now = datetime(2026, 3, 5, 10, 0, tzinfo=IST)
    res = check_compliance(
        "send_recovery_notification",
        {"channel": "sms", "customer_contact": "+919812345678"},
        _event(amount=100),
        [],
        now=now,
    )
    assert res.decision == "BLOCKED"
    assert res.rule_id == "COST-001"


def test_compliance_approves_clean_action():
    res = check_compliance(
        "generate_payment_link",
        {"order_id": "o1", "amount_paise": 250000},
        _event(),
        [],
    )
    assert res.decision == "APPROVED"
    assert res.approved is True
    assert res.blocked is False


def test_next_non_peak_time_leaves_non_peak_untouched():
    safe = datetime(2026, 3, 5, 14, 30, tzinfo=IST)
    assert next_non_peak_time(safe) == safe


# --------------------------------------------------------------------------- #
# Confidence / HITL gate
# --------------------------------------------------------------------------- #
def test_gate_high_confidence_auto_executes():
    d = confidence.evaluate(90, order_value_paise=500000)
    assert d.action == confidence.AUTO_EXECUTE
    assert d.requires_human is False


def test_gate_moderate_confidence_flags():
    d = confidence.evaluate(75, order_value_paise=500000)
    assert d.action == confidence.AUTO_EXECUTE_FLAGGED
    assert d.auto is True
    assert d.requires_human is False


def test_gate_low_confidence_requires_review():
    d = confidence.evaluate(60, order_value_paise=500000)
    assert d.action == confidence.HITL_REVIEW
    assert d.requires_human is True


def test_gate_very_low_confidence_escalates():
    d = confidence.evaluate(30, order_value_paise=500000)
    assert d.action == confidence.ESCALATE
    assert d.requires_human is True


def test_gate_high_value_overrides_to_review():
    # High confidence but a Rs 15,000 order -> still needs a human.
    d = confidence.evaluate(95, order_value_paise=15_00_000)
    assert d.action == confidence.HITL_REVIEW
    assert d.requires_human is True


# --------------------------------------------------------------------------- #
# Full pipeline via the inject endpoint (mock LLM)
# --------------------------------------------------------------------------- #
async def test_inject_recovers_soft_failure(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "gateway_timeout", "amount": 250000, "recover": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["recovered"] is True
    ev = body["events"][0]
    assert ev["recovery"]["tool"] == "schedule_smart_retry"
    assert ev["recovery"]["compliance_decision"] in ("APPROVED", "MODIFIED")

    detail = (await client.get(f"/api/dashboard/events/{ev['id']}")).json()
    agents = {a["agent_name"] for a in detail["actions"]}
    assert agents == {"diagnostic", "strategy"}
    strat = next(a for a in detail["actions"] if a["agent_name"] == "strategy")
    assert strat["action_type"] == "schedule_smart_retry"
    assert strat["compliance_decision"] in ("APPROVED", "MODIFIED")
    assert isinstance(strat["confidence_score"], int)


async def test_inject_recovers_terminal_escalates(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "risk_flagged", "amount": 500000, "recover": True},
    )
    ev = r.json()["events"][0]
    assert ev["recovery"]["tool"] == "mark_unrecoverable"
    assert ev["recovery"]["requires_human"] is True
    assert ev["recovery_status"] == "escalated"


async def test_inject_high_value_requires_human(client):
    # Rs 15,000 hard failure -> payment link, but pinned to human review.
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "insufficient_funds", "amount": 15_00_000, "recover": True},
    )
    ev = r.json()["events"][0]
    assert ev["recovery"]["tool"] == "generate_payment_link"
    assert ev["recovery"]["requires_human"] is True
    assert ev["recovery"]["gate_action"] == confidence.HITL_REVIEW
    assert ev["recovery_status"] == "needs_review"


async def test_inject_recover_false_skips_strategy(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "gateway_timeout", "recover": False},
    )
    ev = r.json()["events"][0]
    detail = (await client.get(f"/api/dashboard/events/{ev['id']}")).json()
    agents = [a["agent_name"] for a in detail["actions"]]
    assert agents == ["diagnostic"]  # strategy did not run


def test_compliance_result_helpers():
    assert ComplianceResult("APPROVED", "ok").approved is True
    assert ComplianceResult("MODIFIED", "x").approved is True
    assert ComplianceResult("BLOCKED", "x").blocked is True
