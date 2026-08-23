"""Diagnostic Agent tests — pure classifier/enricher, the normalizer, and the
full inject -> diagnose -> persist -> read-back path (mock LLM, no Groq key)."""
from app.agent.diagnostic_agent import normalize_diagnosis
from app.diagnosis import classifier, enricher

# (source, step, reason, code) -> expected category, mirroring the 9 profiles.
_PROFILE_CASES = {
    "gateway_timeout": (("gateway", "payment_authorization", "gateway_timeout", "GATEWAY_ERROR"), "soft"),
    "network_timeout": (("gateway", "payment_authorization", "network_timeout", "GATEWAY_ERROR"), "soft"),
    "bank_downtime": (("gateway", "payment_authorization", "issuer_bank_down", "GATEWAY_ERROR"), "soft"),
    "otp_failed": (("customer", "payment_authentication", "invalid_otp", "BAD_REQUEST_ERROR"), "soft"),
    "insufficient_funds": (("customer", "payment_authorization", "insufficient_funds", "BAD_REQUEST_ERROR"), "hard"),
    "card_expired": (("customer", "payment_initiation", "card_expired", "BAD_REQUEST_ERROR"), "hard"),
    "user_cancelled": (("customer", "payment_authentication", "payment_cancelled_by_user", "BAD_REQUEST_ERROR"), "hard"),
    "mandate_inactive": (("customer", "payment_authorization", "mandate_inactive", "BAD_REQUEST_ERROR"), "hard"),
    "risk_flagged": (("razorpay", "payment_authorization", "payment_risk_threshold_breached", "BAD_REQUEST_ERROR"), "terminal"),
}


# --- Pure classifier ------------------------------------------------------ #
def test_classify_matches_expected_categories():
    for name, ((src, step, reason, code), expected) in _PROFILE_CASES.items():
        category, label = classifier.classify(src, step, reason, code)
        assert category == expected, f"{name}: expected {expected}, got {category}"
        assert label  # non-empty human label


def test_classify_unknown_reason_falls_back_to_source():
    assert classifier.classify("gateway", "x", "some_new_reason", "E")[0] == "soft"
    assert classifier.classify("business", "x", "some_new_reason", "E")[0] == "terminal"
    assert classifier.classify("customer", "x", "some_new_reason", "E")[0] == "hard"
    # Completely unknown -> conservative HARD default.
    assert classifier.classify(None, None, None, None)[0] == "hard"


def test_score_is_bounded_and_signed():
    low = classifier.score_recoverability(
        "terminal", success_rate=0.1, prior_attempts=5, age_days=30, amount_paise=100
    )[0]
    high = classifier.score_recoverability(
        "soft", success_rate=0.99, bank_downtime=True, amount_paise=10_000_00
    )[0]
    assert 0 <= low <= 100
    assert 0 <= high <= 100
    assert high > low


def test_timing_intelligence():
    # Gateway timeout: immediate by day, deferred at night.
    assert classifier.recommend_timing("gateway_timeout", "soft", hour=13)[0] == "immediate"
    assert classifier.recommend_timing("gateway_timeout", "soft", hour=23)[0] != "immediate"
    # Insufficient funds: wait for salary near month-end, short delay mid-month.
    assert classifier.recommend_timing("insufficient_funds", "hard", day_of_month=28)[0] == "wait_for_event"
    assert classifier.recommend_timing("insufficient_funds", "hard", day_of_month=12)[0].startswith("delay_hours_")
    # OTP: strike while the customer is trying.
    assert classifier.recommend_timing("invalid_otp", "soft")[0] == "immediate"


# --- Pure enricher -------------------------------------------------------- #
def test_customer_history_is_deterministic():
    ev = {"customer_email": "repeat@example.com"}
    assert enricher.synthesize_customer_history(ev) == enricher.synthesize_customer_history(ev)


def test_bank_status_flags_downtime():
    assert enricher.bank_status({"error_reason": "issuer_bank_down"})["downtime_active"] is True
    assert enricher.bank_status({"error_reason": "gateway_timeout"})["downtime_active"] is False


def test_enrich_shape():
    payload = enricher.enrich({"customer_email": "a@b.com", "error_reason": "gateway_timeout"})
    assert set(payload) == {"event", "customer_history", "bank_status", "current_time", "prior_attempts"}
    assert "hour" in payload["current_time"]


# --- Normalizer ----------------------------------------------------------- #
def test_normalize_handles_junk():
    out = normalize_diagnosis(
        {
            "failure_category": "SOFT",  # wrong case
            "recoverability_score": "150",  # out of range string
            "recoverability_factors": "single",  # not a list
            "risk_factors": None,
        }
    )
    assert out["failure_category"] == "soft"
    assert out["recoverability_score"] == 100  # clamped
    assert out["recoverability_factors"] == ["single"]
    assert out["risk_factors"] == []
    assert out["failure_label"]  # defaulted, non-empty


def test_normalize_bad_category_defaults_hard():
    assert normalize_diagnosis({"failure_category": "banana"})["failure_category"] == "hard"


# --- Full pipeline via the inject endpoint (mock LLM) --------------------- #
async def test_inject_diagnoses_event(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "gateway_timeout", "amount": 250000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["diagnosed"] is True
    ev = body["events"][0]
    assert ev["failure_category"] == "soft"
    assert isinstance(ev["recoverability_score"], int)
    assert 0 <= ev["recoverability_score"] <= 100
    assert ev["diagnosis_source"] == "mock"  # no Groq key in the test env


async def test_diagnosis_persists_and_records_action(client):
    r = await client.post(
        "/api/simulator/inject",
        json={"failure_type": "risk_flagged", "amount": 500000, "recover": False},
    )
    event_id = r.json()["events"][0]["id"]

    detail = (await client.get(f"/api/dashboard/events/{event_id}")).json()
    assert detail["recovery_status"] == "diagnosed"
    assert detail["failure_category"] == "terminal"
    assert detail["recoverability_score"] is not None

    assert len(detail["actions"]) == 1
    action = detail["actions"][0]
    assert action["agent_name"] == "diagnostic"
    assert action["action_type"] == "diagnose"
    assert action["status"] == "completed"
    assert action["result"]["failure_category"] == "terminal"
    assert action["result"]["source"] == "mock"


async def test_inject_can_skip_diagnosis(client):
    r = await client.post("/api/simulator/inject", json={"count": 1, "diagnose": False})
    ev = r.json()["events"][0]
    assert ev["recovery_status"] == "pending"
    assert ev["failure_category"] is None

    detail = (await client.get(f"/api/dashboard/events/{ev['id']}")).json()
    assert detail["actions"] == []
