"""Deterministic failure classifier + recoverability scorer.

Pure-Python and dependency-free (stdlib ``typing`` only) so it is directly
unit-testable and safe to import anywhere. It plays two roles:

1. **Ground-truth taxonomy** for every Razorpay failure, keyed on the
   ``(source, step, reason, code)`` error structure.
2. **The mock brain** behind the Diagnostic Agent — when no Groq key is set (or
   the LLM errors/returns junk), :func:`diagnose` produces the exact JSON shape
   the LLM is asked for, so the pipeline stays fully functional and every run is
   reproducible offline.

All logic follows PRD section 13 (classification rules, scoring guidelines,
timing intelligence). Monetary amounts are in **paise** throughout.
"""
from __future__ import annotations

from typing import Any

# ---- Failure categories -------------------------------------------------- #
SOFT = "soft"        # transient; a straight retry will likely succeed
HARD = "hard"        # needs customer action (new method, funds, a nudge)
TERMINAL = "terminal"  # never retry (risk block, integration error)

VALID_CATEGORIES = (SOFT, HARD, TERMINAL)

# Value threshold (paise) above which an order counts as "high value": ₹5,000.
HIGH_VALUE_PAISE = 5_000_00

# ---- Reason-level rules -------------------------------------------------- #
# reason -> (category, human label). Reason is the most specific signal, so it
# takes priority over the coarser source-based rules below.
_REASON_RULES: dict[str, tuple[str, str]] = {
    # Soft — transient infrastructure / auth blips, retry-friendly.
    "gateway_timeout": (SOFT, "Gateway Timeout"),
    "network_timeout": (SOFT, "Network Timeout"),
    "issuer_bank_down": (SOFT, "Bank Downtime"),
    "invalid_otp": (SOFT, "OTP Failed"),
    # Hard — require the customer to do something.
    "insufficient_funds": (HARD, "Insufficient Funds"),
    "card_expired": (HARD, "Card Expired"),
    "payment_cancelled_by_user": (HARD, "User Cancelled"),
    "mandate_inactive": (HARD, "Mandate Inactive"),
    # Terminal — do not retry.
    "payment_risk_threshold_breached": (TERMINAL, "Risk Flagged"),
}

# Coarser fallback when the exact reason is unknown, keyed on error source.
_SOURCE_RULES: dict[str, str] = {
    "gateway": SOFT,
    "razorpay": TERMINAL,
    "business": TERMINAL,
    "customer": HARD,
}


def classify(
    source: str | None,
    step: str | None,
    reason: str | None,
    code: str | None,
) -> tuple[str, str]:
    """Return ``(category, human_label)`` for a Razorpay error structure.

    Priority: exact reason rule -> source rule -> conservative HARD default.
    """
    reason_key = (reason or "").strip().lower()
    if reason_key in _REASON_RULES:
        return _REASON_RULES[reason_key]

    source_key = (source or "").strip().lower()
    category = _SOURCE_RULES.get(source_key, HARD)
    label = _prettify(reason_key) or _prettify(source_key) or "Payment Failed"
    return category, label


def _prettify(token: str) -> str:
    return token.replace("_", " ").title() if token else ""


# ---- Recoverability scoring --------------------------------------------- #
def score_recoverability(
    category: str,
    *,
    success_rate: float | None = None,
    bank_downtime: bool = False,
    prior_attempts: int = 0,
    age_days: float = 0.0,
    amount_paise: int = 0,
) -> tuple[int, list[str], list[str]]:
    """Compute a 0–100 recoverability score with its supporting factors.

    Returns ``(score, recoverability_factors, risk_factors)``. The point
    weights are lifted directly from PRD section 13.
    """
    score = 50
    positives: list[str] = []
    risks: list[str] = []

    if category == SOFT:
        score += 20
        positives.append("Soft failure: a retry will likely succeed")
    elif category == TERMINAL:
        score -= 20
        risks.append("Terminal failure: not safely retryable")

    if success_rate is not None:
        pct = round(success_rate * 100)
        if success_rate > 0.80:
            score += 15
            positives.append(f"Strong payment history ({pct}% success)")
        elif success_rate < 0.50:
            score -= 15
            risks.append(f"Poor payment history ({pct}% success)")

    if bank_downtime:
        score += 10
        positives.append("Issuer bank downtime is active: will self-resolve")

    if prior_attempts >= 2:
        score -= 10
        risks.append(f"{prior_attempts} prior recovery attempts already made")

    if age_days > 7:
        score -= 10
        risks.append(f"Order is stale ({int(age_days)} days since failure)")

    if amount_paise >= HIGH_VALUE_PAISE:
        score += 5
        positives.append("High-value order: customer has more incentive to pay")

    score = max(0, min(100, score))
    return score, positives, risks


# ---- Timing intelligence ------------------------------------------------- #
def recommend_timing(
    reason: str | None,
    category: str,
    *,
    hour: int | None = None,
    day_of_month: int | None = None,
) -> tuple[str, str]:
    """Return ``(recommended_timing, rationale)`` per PRD timing intelligence.

    ``recommended_timing`` is one of ``"immediate"``, ``"delay_hours_N"``, or
    ``"wait_for_event"``.
    """
    reason_key = (reason or "").strip().lower()
    is_night = hour is not None and (hour >= 22 or hour < 6)

    if reason_key in ("gateway_timeout", "network_timeout"):
        if is_night:
            return "delay_hours_8", "Transient gateway error at night: retry after banks clear maintenance around 6 AM IST"
        return "immediate", "Transient gateway error: an immediate retry will most likely clear it"

    if reason_key == "issuer_bank_down":
        return "wait_for_event", "Issuer bank is in maintenance: wait for it to come back up (typically by ~6 AM IST)"

    if reason_key == "invalid_otp":
        return "immediate", "Customer was actively trying to pay: retry immediately while intent is high"

    if reason_key == "insufficient_funds":
        if day_of_month is not None and day_of_month >= 25:
            return "wait_for_event", "Insufficient funds near month-end: wait for the 1st when salary is likely credited"
        return "delay_hours_48", "Insufficient funds: give the customer ~48 hours before re-attempting"

    if reason_key == "card_expired":
        return "immediate", "Card expired: send a payment link now so the customer can pay with a valid method"

    if reason_key == "payment_cancelled_by_user":
        return "delay_hours_24", "Customer cancelled: wait ~24 hours before a gentle nudge, don't annoy them immediately"

    if reason_key == "mandate_inactive":
        return "wait_for_event", "E-mandate is inactive: recovery needs mandate re-authorisation first"

    if category == TERMINAL:
        return "wait_for_event", "Terminal failure: do not retry; escalate for manual review"

    if category == SOFT:
        return "immediate", "Soft failure: safe to retry immediately"
    return "delay_hours_24", "Needs customer action: allow some time before a follow-up"


# ---- Root-cause narrative ------------------------------------------------ #
def _root_cause(reason: str | None, category: str, label: str) -> str:
    reason_key = (reason or "").strip().lower()
    templates = {
        "gateway_timeout": "The payment gateway timed out before the bank confirmed the charge. This is a transient infrastructure issue on the acquiring side, not a problem with the customer's funds or card.",
        "network_timeout": "A network timeout interrupted the authorisation round-trip. The customer's payment instrument is almost certainly fine; the request simply didn't complete in time.",
        "issuer_bank_down": "The customer's issuing bank was temporarily down for maintenance and could not authorise the payment. This resolves on its own once the bank's systems come back online.",
        "invalid_otp": "Authentication failed because an incorrect or expired OTP was entered. The customer was actively trying to pay, so a fresh attempt usually succeeds.",
        "insufficient_funds": "The bank declined the charge for insufficient funds. The customer intends to pay but lacked balance at that moment; timing the retry around a salary credit materially improves recovery.",
        "card_expired": "The charge was declined because the card has expired. Recovery requires the customer to supply a valid payment method, best prompted with a payment link.",
        "payment_cancelled_by_user": "The customer abandoned the payment mid-flow. There is no technical fault; recovery depends on re-engaging them, ideally after a short cool-off.",
        "mandate_inactive": "The e-mandate backing this payment is inactive or revoked, so the auto-debit could not run. The mandate must be re-authorised before recovery is possible.",
        "payment_risk_threshold_breached": "Razorpay's risk engine blocked this payment for breaching a fraud/risk threshold. Retrying is unsafe and could increase dispute exposure; this should be escalated, not automated.",
    }
    if reason_key in templates:
        return templates[reason_key]
    return (
        f"Classified as a {category} failure ({label}). "
        "Root cause could not be mapped to a known pattern from the error fields; "
        "treat with the default policy for this category."
    )


# ---- The mock brain ------------------------------------------------------ #
def diagnose(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic diagnosis matching the Diagnostic Agent's JSON contract.

    ``payload`` mirrors what the LLM receives (see the enricher):
    ``{event, customer_history, bank_status, current_time, prior_attempts}``.
    Returns the PRD section-13 output schema.
    """
    event = payload.get("event") or {}
    history = payload.get("customer_history") or {}
    bank = payload.get("bank_status") or {}
    now = payload.get("current_time") or {}
    prior_attempts = int(payload.get("prior_attempts") or 0)

    source = event.get("error_source")
    step = event.get("error_step")
    reason = event.get("error_reason")
    code = event.get("error_code")

    category, label = classify(source, step, reason, code)

    score, positives, risks = score_recoverability(
        category,
        success_rate=history.get("success_rate"),
        bank_downtime=bool(bank.get("downtime_active")),
        prior_attempts=prior_attempts,
        age_days=float(now.get("age_days") or 0.0),
        amount_paise=int(event.get("amount") or 0),
    )

    timing, timing_rationale = recommend_timing(
        reason,
        category,
        hour=now.get("hour"),
        day_of_month=now.get("day_of_month"),
    )

    return {
        "failure_category": category,
        "failure_label": label,
        "root_cause_analysis": _root_cause(reason, category, label),
        "recoverability_score": score,
        "recoverability_factors": positives,
        "risk_factors": risks,
        "recommended_timing": timing,
        "timing_rationale": timing_rationale,
    }
