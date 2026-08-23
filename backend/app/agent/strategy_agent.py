"""Strategy Agent (LLM #2) — the decision brain.

Stage 2 of the recovery pipeline. Given a *diagnosed* ``RecoveryEvent`` it:

1. Assembles a strategy payload (diagnosis + original event + customer history +
   IST clock) and asks the LLM to select exactly one bounded recovery tool
   (``tool_choice="required"``), falling back to the deterministic
   ``strategy.planner`` whenever no Groq key is set or the live call fails.
2. Runs the proposed action through the **deterministic Compliance Engine**
   (``compliance.engine.check_compliance``) — APPROVED / MODIFIED / BLOCKED. A
   MODIFIED verdict's changes are merged into the action's params.
3. Routes the (compliant) action through the confidence gate
   (``agent.confidence.evaluate``): auto-execute, auto-execute-flagged,
   human-in-the-loop review, or escalate — with a hard human-review override for
   high-value orders.
4. Persists an auditable ``RecoveryAction`` (``agent_name="strategy"``) carrying
   the chosen tool, its params, the confidence, and the full compliance verdict,
   and advances the event's ``recovery_status`` accordingly.

This phase *decides*; it does not execute. Calling Razorpay / sending anything
lands in the Execution Engine phase — hence actions are persisted with a
forward-looking status (``approved`` / ``pending_review`` / ``escalated`` /
``blocked``) and ``executed_at`` left null. The orchestration seam is thin; the
judgement lives in the pure ``planner`` / ``compliance`` / ``confidence`` modules
so every decision is reproducible and unit-testable offline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import confidence
from app.agent.diagnostic_agent import normalize_diagnosis
from app.agent.prompts import STRATEGY_SYSTEM_PROMPT
from app.agent.tools import estimate_cost_paise
from app.compliance.engine import check_compliance
from app.diagnosis import classifier, enricher
from app.ingest import event_to_dict
from app.llm.client import llm_client
from app.models import RecoveryAction, RecoveryEvent
from app.strategy import planner

# Action-row status set by this stage (execution happens in a later phase).
STATUS_APPROVED = "approved"            # compliant + auto-cleared to execute
STATUS_PENDING_REVIEW = "pending_review"  # compliant but held for a human
STATUS_ESCALATED = "escalated"          # low confidence / escalation tool
STATUS_BLOCKED = "blocked"              # compliance blocked the action

# Event-level recovery_status this stage sets.
_EVENT_STATUS = {
    STATUS_APPROVED: "in_progress",
    STATUS_PENDING_REVIEW: "needs_review",
    STATUS_ESCALATED: "escalated",
    STATUS_BLOCKED: "blocked",
}

_RETRY_TOOL = "schedule_smart_retry"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def diagnosis_for_event(
    event: RecoveryEvent, *, now: datetime | None = None
) -> dict[str, Any]:
    """Full diagnosis dict for a (possibly already-diagnosed) event.

    Re-derives timing / factor fields deterministically via the classifier —
    the event table only persists a diagnosis *summary* — then layers any
    persisted values (which may have come from the live LLM) back on top so the
    Strategy Agent reasons over the same category/score the Diagnostic Agent
    committed.
    """
    base = normalize_diagnosis(
        classifier.diagnose(
            enricher.enrich(
                event_to_dict(event),
                prior_attempts=event.recovery_attempts or 0,
                now=now,
            )
        )
    )
    if event.failure_category:
        base["failure_category"] = event.failure_category
    if event.failure_label:
        base["failure_label"] = event.failure_label
    if event.recoverability_score is not None:
        base["recoverability_score"] = int(event.recoverability_score)
    if event.root_cause_analysis:
        base["root_cause_analysis"] = event.root_cause_analysis
    return base


def build_strategy_payload(
    event: RecoveryEvent,
    diagnosis: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the LLM/planner input for a diagnosed event.

    The ``event`` block is built from ``event_to_dict`` (NOT the diagnostic
    enricher's summary) because the planner needs ``razorpay_order_id`` to
    parameterise every tool — the enricher summary omits it.
    """
    ev = event_to_dict(event)
    ctx = enricher.enrich(ev, prior_attempts=event.recovery_attempts or 0, now=now)
    return {
        "diagnostic": diagnosis,
        "event": {
            "razorpay_order_id": ev["razorpay_order_id"],
            "razorpay_payment_id": ev["razorpay_payment_id"],
            "amount": ev["amount"],
            "amount_inr": ev["amount_inr"],
            "payment_method": ev["payment_method"],
            "error_reason": ev["error_reason"],
            "error_source": ev["error_source"],
            "failure_label": ev["failure_label"],
            "customer_email": ev["customer_email"],
            "customer_contact": ev["customer_contact"],
            "customer_name": ev["customer_name"],
            "customer_dnd": ev["customer_dnd"],
        },
        "customer_history": ctx["customer_history"],
        "prior_attempts": ctx["prior_attempts"],
        "current_time": ctx["current_time"],
    }


async def _load_prior_strategy_actions(
    session: AsyncSession, event: RecoveryEvent
) -> list[dict[str, Any]]:
    """Prior *strategy* actions on this event, shaped for the compliance engine."""
    rows = (
        await session.scalars(
            select(RecoveryAction).where(
                RecoveryAction.recovery_event_id == event.id,
                RecoveryAction.agent_name == "strategy",
            )
        )
    ).all()
    prior: list[dict[str, Any]] = []
    for a in rows:
        params = a.action_params or {}
        # The frequency rule needs *a* timestamp; execution hasn't happened yet,
        # so fall back to when the action row was created.
        stamp = a.executed_at or a.created_at
        prior.append(
            {
                "action_type": a.action_type,
                "cost_paise": a.cost_paise or 0,
                "executed_at": stamp.isoformat() if stamp else None,
                "customer_contact": params.get("customer_contact"),
                "action_params": params,
            }
        )
    return prior


async def recover_event(
    session: AsyncSession,
    event: RecoveryEvent,
    *,
    diagnosis: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan + compliance-check + gate one diagnosed event; persist the decision.

    Pass ``diagnosis`` (e.g. the dict returned by ``diagnose_event``) to reuse the
    exact diagnosis just produced; omit it to reconstruct one from the event.
    Returns a rich decision dict (tool, params, confidence, compliance verdict,
    gate routing, final status) for SSE frames and API responses.
    """
    if diagnosis is None:
        diagnosis = diagnosis_for_event(event, now=now)

    payload = build_strategy_payload(event, diagnosis, now=now)

    # 1) Select a tool (LLM tool-call, else deterministic planner).
    tool_name, tool_args, meta, source = await llm_client.complete_tool_call(
        system_prompt=STRATEGY_SYSTEM_PROMPT,
        user_payload=payload,
        fallback=planner.plan,
    )

    # 2) Deterministic compliance check.
    ev_dict = event_to_dict(event)
    prior_actions = await _load_prior_strategy_actions(session, event)
    verdict = check_compliance(tool_name, tool_args, ev_dict, prior_actions, now=now)

    final_args = dict(tool_args)
    if verdict.decision == "MODIFIED" and verdict.modification:
        final_args.update(verdict.modification)

    confidence_score = int(meta.get("confidence") or 0)

    # 3) Confidence gate (skipped when compliance blocked the action).
    if verdict.blocked:
        gate = None
        status = STATUS_BLOCKED
        cost = 0
    else:
        gate = confidence.evaluate(confidence_score, event.amount or 0)
        if gate.action in (confidence.AUTO_EXECUTE, confidence.AUTO_EXECUTE_FLAGGED):
            status = STATUS_APPROVED
        elif gate.action == confidence.HITL_REVIEW:
            status = STATUS_PENDING_REVIEW
        else:  # ESCALATE
            status = STATUS_ESCALATED
        cost = estimate_cost_paise(tool_name, final_args)

    scheduled_at = _parse_dt(final_args.get("retry_at") or final_args.get("scheduled_at"))

    # 4) Persist an auditable action + advance the event.
    action = RecoveryAction(
        recovery_event_id=event.id,
        agent_name="strategy",
        action_type=tool_name,
        action_params=final_args,
        agent_reasoning=final_args.get("reason") or "",
        confidence_score=confidence_score,
        risk_factors=list(meta.get("risk_factors") or []),
        uncertainty_factors=list(meta.get("uncertainty_factors") or []),
        compliance_decision=verdict.decision,
        compliance_rule_id=verdict.rule_id,
        compliance_rule_name=verdict.rule_name,
        compliance_reason=verdict.reason,
        status=status,
        scheduled_at=scheduled_at,
        executed_at=None,  # execution engine is a later phase
        result={
            "tool": tool_name,
            "source": source,
            "confidence": confidence_score,
            "compliance": {
                "decision": verdict.decision,
                "rule_id": verdict.rule_id,
                "rule_name": verdict.rule_name,
                "reason": verdict.reason,
                "modification": verdict.modification,
            },
            "gate": _gate_dict(gate, verdict),
        },
        cost_paise=cost,
    )
    session.add(action)

    event.recovery_status = _EVENT_STATUS.get(status, event.recovery_status)
    if status == STATUS_APPROVED and tool_name == _RETRY_TOOL:
        event.recovery_attempts = (event.recovery_attempts or 0) + 1
    event.recovery_cost_paise = (event.recovery_cost_paise or 0) + cost
    event.updated_at = _utcnow()

    await session.commit()
    await session.refresh(event)
    await session.refresh(action)

    return {
        "action_id": str(action.id),
        "recovery_event_id": str(event.id),
        "tool": tool_name,
        "action_type": tool_name,
        "source": source,
        "params": final_args,
        "reason": final_args.get("reason") or "",
        "confidence": confidence_score,
        "risk_factors": list(meta.get("risk_factors") or []),
        "uncertainty_factors": list(meta.get("uncertainty_factors") or []),
        "compliance": {
            "decision": verdict.decision,
            "rule_id": verdict.rule_id,
            "rule_name": verdict.rule_name,
            "reason": verdict.reason,
            "modification": verdict.modification,
        },
        "gate": _gate_dict(gate, verdict),
        "status": status,
        "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        "cost_paise": cost,
    }


def _gate_dict(gate: confidence.GateDecision | None, verdict) -> dict[str, Any]:
    """Uniform gate block for the API — synthesised when compliance blocked."""
    if gate is None:
        return {
            "action": "blocked",
            "requires_human": True,
            "tier": "blocked",
            "confidence": None,
            "reason": verdict.reason,
        }
    return {
        "action": gate.action,
        "requires_human": gate.requires_human,
        "tier": gate.tier,
        "confidence": gate.confidence,
        "reason": gate.reason,
    }
