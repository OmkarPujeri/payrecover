"""Diagnostic Agent (LLM #1).

Stage 1 of the recovery pipeline. Given a persisted ``RecoveryEvent``, it:

1. Enriches the event with synthetic customer history, bank status and IST
   timing context (see ``app.diagnosis.enricher``).
2. Asks the LLM for a structured diagnosis — falling back to the deterministic
   classifier (``app.diagnosis.classifier.diagnose``) whenever no Groq key is
   configured or the live call fails. Both paths satisfy the same JSON contract.
3. Normalises/validates the result, writes the diagnosis onto the event, and
   records an auditable ``RecoveryAction`` row (``agent_name="diagnostic"``).

The heavy lifting (classification, scoring, timing) lives in pure modules; this
file is the thin orchestration + persistence seam.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import DIAGNOSTIC_SYSTEM_PROMPT
from app.diagnosis import classifier, enricher
from app.ingest import event_to_dict
from app.llm.client import llm_client
from app.models import RecoveryAction, RecoveryEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_diagnosis(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a (possibly LLM-authored) diagnosis into a safe, typed shape.

    Pure function — guards against a live model returning slightly off-spec
    JSON so downstream persistence and agents can rely on the contract.
    """
    category = str(raw.get("failure_category", "")).strip().lower()
    if category not in classifier.VALID_CATEGORIES:
        category = classifier.HARD

    try:
        score = int(round(float(raw.get("recoverability_score", 50))))
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))

    def _str_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if value:
            return [str(value)]
        return []

    label = str(raw.get("failure_label") or "Payment Failed").strip()
    timing = str(raw.get("recommended_timing") or "immediate").strip()

    return {
        "failure_category": category,
        "failure_label": label,
        "root_cause_analysis": str(raw.get("root_cause_analysis") or "").strip(),
        "recoverability_score": score,
        "recoverability_factors": _str_list(raw.get("recoverability_factors")),
        "risk_factors": _str_list(raw.get("risk_factors")),
        "recommended_timing": timing,
        "timing_rationale": str(raw.get("timing_rationale") or "").strip(),
    }


def build_payload(
    event: RecoveryEvent, *, now: datetime | None = None
) -> dict[str, Any]:
    """Assemble the LLM/mock input payload for a persisted event."""
    return enricher.enrich(
        event_to_dict(event),
        prior_attempts=event.recovery_attempts or 0,
        now=now,
    )


async def diagnose_event(
    session: AsyncSession,
    event: RecoveryEvent,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Diagnose one event, persist the result, and return it.

    The returned dict is the normalised diagnosis plus bookkeeping keys
    (``source`` = ``"llm"``|``"mock"``, ``action_id``, ``recovery_event_id``).
    """
    payload = build_payload(event, now=now)

    raw, source = await llm_client.complete_json(
        system_prompt=DIAGNOSTIC_SYSTEM_PROMPT,
        user_payload=payload,
        fallback=classifier.diagnose,
    )
    diagnosis = normalize_diagnosis(raw)

    # 1) Write the diagnosis onto the event.
    event.failure_category = diagnosis["failure_category"]
    event.failure_label = diagnosis["failure_label"]
    event.root_cause_analysis = diagnosis["root_cause_analysis"]
    event.recoverability_score = diagnosis["recoverability_score"]
    if event.recovery_status == "pending":
        event.recovery_status = "diagnosed"
    event.updated_at = _utcnow()

    # 2) Record an auditable action row.
    action = RecoveryAction(
        recovery_event_id=event.id,
        agent_name="diagnostic",
        action_type="diagnose",
        action_params={
            "model": llm_client.model,
            "source": source,
            "recommended_timing": diagnosis["recommended_timing"],
        },
        agent_reasoning=diagnosis["root_cause_analysis"],
        confidence_score=diagnosis["recoverability_score"],
        risk_factors=diagnosis["risk_factors"],
        uncertainty_factors=diagnosis["recoverability_factors"],
        status="completed",
        executed_at=_utcnow(),
        result={**diagnosis, "source": source},
        cost_paise=0,
    )
    session.add(action)
    await session.commit()
    await session.refresh(event)
    await session.refresh(action)

    return {
        **diagnosis,
        "source": source,
        "action_id": str(action.id),
        "recovery_event_id": str(event.id),
    }


# Convenience for the enum-like status this stage sets.
STATUS_DIAGNOSED = "diagnosed"
