"""Deterministic strategy planner — the mock brain behind the Strategy Agent.

Pure-Python and dependency-light (stdlib only) so it is directly unit-testable
and safe to import anywhere. It plays two roles, exactly mirroring how
``diagnosis.classifier`` backs the Diagnostic Agent:

1. **Ground-truth policy** mapping a diagnosis -> the single best recovery tool,
   following the PRD section-14 STRATEGY GUIDELINES verbatim.
2. **The mock fallback** for LLM #2 — when no Groq key is set (or the live
   tool-call errors / returns junk), :func:`plan` returns the same
   ``(tool_name, tool_args, meta)`` shape the live path yields, so the pipeline
   stays fully functional and reproducible offline.

The planner *chooses and parameterises* an action; it never executes it and
never makes compliance decisions — those belong to the deterministic
``compliance.engine`` downstream. Monetary amounts are in **paise** throughout.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.diagnosis import classifier

IST = ZoneInfo("Asia/Kolkata")

# Tool names (kept as constants to avoid stringly-typed drift with tools.py).
SCHEDULE_SMART_RETRY = "schedule_smart_retry"
GENERATE_PAYMENT_LINK = "generate_payment_link"
SEND_RECOVERY_NOTIFICATION = "send_recovery_notification"
OFFER_ALTERNATIVE_METHOD = "offer_alternative_method"
ESCALATE_TO_MERCHANT = "escalate_to_merchant"
MARK_UNRECOVERABLE = "mark_unrecoverable"

# Default payment-link validity for hard failures (PRD: 48h for insufficient
# funds; we use it as the general hard-failure default).
_DEFAULT_EXPIRY_HOURS = 48

# A card that keeps failing -> suggest UPI, and vice-versa.
_ALTERNATIVE_METHOD = {
    "card": "upi",
    "upi": "card",
    "netbanking": "upi",
    "wallet": "upi",
}


# ---- Timing helpers ------------------------------------------------------ #
def _now_ist(now: datetime | None) -> datetime:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def _retry_at(reason: str, timing: str, now_ist: datetime) -> datetime:
    """Translate the diagnosis' ``recommended_timing`` into a concrete IST time.

    The compliance engine will still shift this out of NPCI peak hours if
    needed; here we just honour the diagnostic intent (immediate / delay_N /
    wait_for_event) with sensible defaults per the STRATEGY GUIDELINES.
    """
    reason_key = (reason or "").strip().lower()

    # Bank downtime: wait for the bank to recover (~6 AM IST heuristic).
    if reason_key == "issuer_bank_down" or timing == "wait_for_event":
        target = now_ist.replace(hour=6, minute=30, second=0, microsecond=0)
        if target <= now_ist:
            target += timedelta(days=1)
        return target

    # Explicit delay_hours_N from the diagnosis.
    if timing.startswith("delay_hours_"):
        try:
            hours = int(timing.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            hours = 2
        return now_ist + timedelta(hours=hours)

    # Gateway/network timeout default: PRD says "2h later".
    if reason_key in ("gateway_timeout", "network_timeout"):
        return now_ist + timedelta(hours=2)

    # Immediate (with a tiny buffer so the timestamp is in the near future).
    return now_ist + timedelta(minutes=2)


# ---- Confidence ---------------------------------------------------------- #
def _confidence(category: str, recoverability: int, history: dict[str, Any]) -> int:
    """A bounded 0-100 confidence for the chosen action.

    Anchored on the diagnostic recoverability score, nudged by failure class and
    customer history. The confidence gate (agent/confidence.py) turns this into
    an auto-execute / HITL / escalate routing decision.
    """
    score = recoverability
    if category == classifier.SOFT:
        score += 10          # clear, well-understood recovery path
    elif category == classifier.TERMINAL:
        score = min(score, 40)  # never present terminal calls as high-confidence

    rate = history.get("success_rate")
    if isinstance(rate, (int, float)):
        if rate > 0.80:
            score += 5
        elif rate < 0.50:
            score -= 5

    return max(0, min(100, int(round(score))))


# ---- The planner --------------------------------------------------------- #
def plan(payload: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Pick exactly one recovery tool for a diagnosed event.

    ``payload`` is the strategy input assembled by the orchestrator::

        {
          "diagnostic": {...normalised diagnosis...},
          "event": {...event summary (order_id, amount, contacts, method)...},
          "customer_history": {...},
          "prior_attempts": int,
          "current_time": {"iso","hour","day_of_month",...},
        }

    Returns ``(tool_name, tool_args, meta)`` where ``meta`` carries
    ``confidence`` (int), ``risk_factors`` (list) and ``uncertainty_factors``
    (list) — the same envelope the live tool-call path produces.
    """
    diagnostic = payload.get("diagnostic") or {}
    event = payload.get("event") or {}
    history = payload.get("customer_history") or {}
    prior_attempts = int(payload.get("prior_attempts") or 0)
    now_ist = _now_ist(_parse_iso((payload.get("current_time") or {}).get("iso")))

    category = str(diagnostic.get("failure_category") or classifier.HARD).lower()
    reason = (event.get("error_reason") or "").strip().lower()
    timing = str(diagnostic.get("recommended_timing") or "immediate")
    recoverability = int(diagnostic.get("recoverability_score") or 50)

    order_id = event.get("razorpay_order_id") or event.get("order_id") or ""
    amount_paise = int(event.get("amount") or 0)
    method = (event.get("payment_method") or "any").lower()
    label = diagnostic.get("failure_label") or "Payment Failed"

    confidence = _confidence(category, recoverability, history)
    risk_factors = list(diagnostic.get("risk_factors") or [])
    uncertainty: list[str] = []

    # ---- TERMINAL: never retry (constraint 9) ---------------------------- #
    if category == classifier.TERMINAL:
        if reason == "payment_risk_threshold_breached":
            return (
                MARK_UNRECOVERABLE,
                {
                    "order_id": order_id,
                    "reason": (
                        "Risk threshold breached — Razorpay's fraud engine blocked this "
                        "payment. Retrying is unsafe and raises dispute exposure; closing "
                        "the case as unrecoverable."
                    ),
                },
                _meta(confidence, risk_factors, uncertainty),
            )
        # Other terminal (e.g. business/integration error) -> human review.
        return (
            ESCALATE_TO_MERCHANT,
            {
                "order_id": order_id,
                "severity": "high",
                "reason": (
                    f"Terminal failure ({label}) that automated recovery cannot safely "
                    "handle — routing to the merchant for manual review."
                ),
                "recommended_action": "Investigate the integration / risk flag before any manual retry.",
            },
            _meta(confidence, risk_factors, uncertainty),
        )

    # ---- SOFT: prefer a silent retry (constraints 7) --------------------- #
    if category == classifier.SOFT:
        # Retry cap (constraint 1): after 3 attempts, stop retrying and hand to
        # the customer / merchant instead of violating the NPCI cap.
        if prior_attempts >= 3:
            uncertainty.append(f"Retry cap reached ({prior_attempts}/3) — cannot schedule another retry")
            return (
                GENERATE_PAYMENT_LINK,
                _payment_link_args(event, amount_paise, order_id, label, _DEFAULT_EXPIRY_HOURS,
                                   note="Automated retries exhausted; offering a self-service payment link."),
                _meta(min(confidence, 65), risk_factors, uncertainty),
            )

        retry_at = _retry_at(reason, timing, now_ist)
        retry_method = method if method in ("card", "upi", "netbanking") else "any"
        return (
            SCHEDULE_SMART_RETRY,
            {
                "order_id": order_id,
                "retry_at": retry_at.isoformat(),
                "payment_method": retry_method,
                "reason": (
                    f"{label}: transient/soft failure. {diagnostic.get('timing_rationale') or ''} "
                    f"Scheduling a silent retry at {retry_at.strftime('%d %b %I:%M %p IST')} "
                    f"(attempt {prior_attempts + 1}/3), no customer contact needed."
                ).strip(),
            },
            _meta(confidence, risk_factors, uncertainty),
        )

    # ---- HARD: always a payment link (constraint 8), with nuances -------- #
    # Card that keeps failing while alternatives exist -> suggest switching.
    if reason == "card_expired" and method == "card":
        # A payment link that nudges card/UPI is the PRD's card_expired play;
        # but if the card is the consistently failing method, offering UPI is
        # the more targeted move. We keep the PRD default (payment link) and
        # note UPI in the copy.
        pass

    if reason == "insufficient_funds":
        # 48h link; the diagnosis already defers timing around the salary cycle.
        note = "Insufficient funds — 48h payment link; timing accounts for the salary cycle so the customer can complete when funded."
        expiry = 48
    elif reason == "card_expired":
        note = "Card expired — payment link inviting the customer to pay with a new card or UPI."
        expiry = _DEFAULT_EXPIRY_HOURS
    elif reason == "invalid_otp":
        note = "OTP failed while the customer was actively paying — sending an immediate payment link to complete now."
        expiry = 24
    elif reason == "payment_cancelled_by_user":
        note = "Customer abandoned checkout — a friendly payment link after a short cool-off to re-engage without nagging."
        expiry = _DEFAULT_EXPIRY_HOURS
        uncertainty.append("Customer cancelled deliberately — intent to complete is uncertain")
    elif reason == "mandate_inactive":
        note = "E-mandate inactive/revoked — payment link for re-registration so the auto-debit can resume."
        expiry = _DEFAULT_EXPIRY_HOURS
    else:
        note = f"{label}: hard failure requiring customer action — issuing a self-service payment link."
        expiry = _DEFAULT_EXPIRY_HOURS

    return (
        GENERATE_PAYMENT_LINK,
        _payment_link_args(event, amount_paise, order_id, label, expiry, note=note),
        _meta(confidence, risk_factors, uncertainty),
    )


# ---- Small builders ------------------------------------------------------ #
def _payment_link_args(
    event: dict[str, Any],
    amount_paise: int,
    order_id: str,
    label: str,
    expiry_hours: int,
    *,
    note: str,
) -> dict[str, Any]:
    email = event.get("customer_email")
    contact = event.get("customer_contact")
    dnd = bool(event.get("customer_dnd"))
    return {
        "order_id": order_id,
        "amount_paise": amount_paise,
        "expiry_hours": int(max(4, min(168, expiry_hours))),
        "description": f"Complete your payment — {label}",
        "customer_email": email,
        "customer_contact": contact,
        # DND customers: email only (constraint 6). Otherwise notify on both.
        "notify_sms": (not dnd) and bool(contact),
        "notify_email": bool(email),
        "reason": note,
    }


def _meta(confidence: int, risk_factors: list[str], uncertainty: list[str]) -> dict[str, Any]:
    return {
        "confidence": int(max(0, min(100, confidence))),
        "risk_factors": list(risk_factors),
        "uncertainty_factors": list(uncertainty),
        "source": "mock",
    }


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
