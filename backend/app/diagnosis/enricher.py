"""Context enricher for the Diagnostic Agent.

Turns a serialised failure event into the richer payload the agent reasons
over: a synthetic-but-stable customer history, issuer-bank status, and the
current IST clock context (hour, day-of-month, night flag, order age).

Design notes:
* Dependency-light — stdlib only (``hashlib``, ``datetime``, ``zoneinfo``). It
  operates on a plain ``dict`` (the output of ``ingest.event_to_dict``), never
  the ORM object, so it stays importable and unit-testable without SQLAlchemy.
* Customer history is **deterministic per customer** — seeded from the email /
  contact / name — so the same customer always yields the same history across
  runs and processes. This keeps demos and tests reproducible while still
  giving the agent realistic variation to reason about.
* IST via ``zoneinfo.ZoneInfo("Asia/Kolkata")`` (with the ``tzdata`` package on
  Windows) rather than ``pytz``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Reasons that indicate an active issuer-bank downtime window.
_BANK_DOWNTIME_REASONS = {"issuer_bank_down"}


def _seed(event: dict[str, Any]) -> int:
    """A stable integer seed derived from customer identity."""
    key = (
        (event.get("customer_email") or "")
        + "|"
        + (event.get("customer_contact") or "")
        + "|"
        + (event.get("customer_name") or "")
    ).lower()
    if not key.strip("|"):
        # Fall back to the payment id so anonymous events are still stable.
        key = event.get("razorpay_payment_id") or "anonymous"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def synthesize_customer_history(event: dict[str, Any]) -> dict[str, Any]:
    """Deterministic synthetic payment history for the event's customer."""
    seed = _seed(event)
    # Total lifetime payments: 1–40, skewed low.
    total = 1 + seed % 40
    # Success rate bucketed 0.35–0.98 deterministically from the seed.
    rate = 0.35 + ((seed >> 8) % 64) / 100.0  # 0.35 .. 0.98
    rate = round(min(0.98, rate), 2)
    successful = round(total * rate)
    tenure_days = 15 + (seed >> 16) % 900  # ~2 weeks .. ~2.5 years
    return {
        "total_payments": total,
        "successful_payments": successful,
        "success_rate": rate,
        "tenure_days": tenure_days,
        "returning_customer": total >= 3,
    }


def bank_status(event: dict[str, Any]) -> dict[str, Any]:
    """Whether the issuer bank is in an active downtime window."""
    reason = (event.get("error_reason") or "").strip().lower()
    active = reason in _BANK_DOWNTIME_REASONS
    return {
        "downtime_active": active,
        "note": (
            "Issuer bank reported in maintenance; expected to clear by ~6 AM IST"
            if active
            else "No active issuer downtime detected"
        ),
    }


def _time_context(event: dict[str, Any], now: datetime | None) -> dict[str, Any]:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    hour = now.hour
    age_days = 0.0
    created = event.get("created_at")
    if created:
        try:
            created_dt = datetime.fromisoformat(str(created))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=IST)
            age_days = max(0.0, (now - created_dt.astimezone(IST)).total_seconds() / 86400.0)
        except (ValueError, TypeError):
            age_days = 0.0

    return {
        "iso": now.isoformat(),
        "hour": hour,
        "day_of_month": now.day,
        "is_night": hour >= 22 or hour < 6,
        "age_days": round(age_days, 2),
    }


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    """The subset of event fields the agent needs to reason about."""
    return {
        "razorpay_payment_id": event.get("razorpay_payment_id"),
        "amount": event.get("amount"),
        "amount_inr": event.get("amount_inr"),
        "currency": event.get("currency"),
        "payment_method": event.get("payment_method"),
        "error_code": event.get("error_code"),
        "error_source": event.get("error_source"),
        "error_step": event.get("error_step"),
        "error_reason": event.get("error_reason"),
        "error_description": event.get("error_description"),
        "customer_name": event.get("customer_name"),
        "customer_email": event.get("customer_email"),
        "customer_contact": event.get("customer_contact"),
        "customer_dnd": event.get("customer_dnd"),
    }


def enrich(
    event: dict[str, Any],
    *,
    prior_attempts: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the full diagnostic payload for one failure event.

    ``event`` is the dict from ``ingest.event_to_dict``. ``now`` may be injected
    for deterministic tests; it defaults to the current time in IST.
    """
    return {
        "event": _event_summary(event),
        "customer_history": synthesize_customer_history(event),
        "bank_status": bank_status(event),
        "current_time": _time_context(event, now),
        "prior_attempts": int(prior_attempts),
    }
