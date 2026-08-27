"""Human-in-the-Loop endpoints — /api/hitl/* (PRD sections 16 & 21).

The confidence gate parks an action in ``pending_review`` when the agent is not
confident enough (50-69) or the order is large (> Rs 10,000). These endpoints are
how a merchant resolves that queue: **approve**, **modify**, or **skip**.

The design point worth defending: *modify does not bypass compliance*. When a
merchant edits an action's parameters we re-run the deterministic Compliance
Engine against the edited version, with the same prior-action history the agent
saw. A human may overrule the *agent*; nobody overrules NPCI, TRAI, or the DND
registry by hand-editing a payload. If the edit is BLOCKED we say so and refuse
to execute; if it is MODIFIED the engine's correction is merged on top of the
merchant's edit, and the new verdict is written back onto the audit row.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.strategy_agent import load_prior_strategy_actions
from app.agent.tools import estimate_cost_paise
from app.compliance.engine import check_compliance
from app.database import get_session
from app.execution import statuses
from app.execution.executor import execute_action
from app.ingest import event_to_dict
from app.models import RecoveryAction, RecoveryEvent
from app.sse import sse_manager

router = APIRouter(prefix="/api/hitl", tags=["hitl"])


class ModifyRequest(BaseModel):
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameter overrides merged into the action before re-checking compliance",
    )
    note: str | None = Field(default=None, description="Optional merchant note for the audit trail")


class SkipRequest(BaseModel):
    reason: str | None = Field(default=None, description="Why the merchant declined this action")


def _parse_uuid(value: str, label: str = "action id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc


async def _load_pending(
    session: AsyncSession, action_id: str
) -> tuple[RecoveryAction, RecoveryEvent]:
    """Fetch an action that is still awaiting a human decision, plus its event."""
    action = await session.get(RecoveryAction, _parse_uuid(action_id))
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status in statuses.TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Action is already {action.status} and cannot be actioned again",
        )
    if action.status not in (statuses.PENDING_REVIEW, statuses.ESCALATED):
        raise HTTPException(
            status_code=409,
            detail=f"Action status {action.status!r} is not awaiting human review",
        )
    event = await session.get(RecoveryEvent, action.recovery_event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Parent recovery event not found")
    return action, event


def _hitl_request(action: RecoveryAction, event: RecoveryEvent) -> dict[str, Any]:
    """Shape one queue entry — mirrors the PRD ``HITLRequest`` dataclass."""
    return {
        "action_id": str(action.id),
        "recovery_event_id": str(event.id),
        "order_id": event.razorpay_order_id,
        "payment_id": event.razorpay_payment_id,
        "amount_paise": event.amount,
        "amount_inr": round((event.amount or 0) / 100, 2),
        "failure_label": event.failure_label,
        "failure_category": event.failure_category,
        "diagnostic_summary": event.root_cause_analysis,
        "recoverability_score": event.recoverability_score,
        "proposed_action": action.action_type,
        "proposed_params": action.action_params,
        "confidence": action.confidence_score,
        "reasoning": action.agent_reasoning,
        "risk_factors": action.risk_factors or [],
        "uncertainty_factors": action.uncertainty_factors or [],
        "compliance": {
            "decision": action.compliance_decision,
            "rule_id": action.compliance_rule_id,
            "rule_name": action.compliance_rule_name,
            "reason": action.compliance_reason,
        },
        "gate": (action.result or {}).get("gate"),
        "status": action.status,
        "customer_name": event.customer_name,
        "customer_email": event.customer_email,
        "customer_contact": event.customer_contact,
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }


@router.get("/pending")
async def list_pending(session: AsyncSession = Depends(get_session)):
    """Everything waiting on a human, newest first."""
    rows = (
        await session.scalars(
            select(RecoveryAction)
            .where(
                RecoveryAction.status.in_((statuses.PENDING_REVIEW, statuses.ESCALATED))
            )
            .order_by(RecoveryAction.created_at.desc())
        )
    ).all()

    items: list[dict[str, Any]] = []
    for action in rows:
        event = await session.get(RecoveryEvent, action.recovery_event_id)
        if event is not None:
            items.append(_hitl_request(action, event))

    return {"total": len(items), "pending": items}


async def _recheck_compliance(
    session: AsyncSession,
    action: RecoveryAction,
    event: RecoveryEvent,
    params: dict[str, Any],
):
    """Re-run the deterministic Compliance Engine over ``params``."""
    prior = await load_prior_strategy_actions(
        session, event, exclude_action_id=action.id
    )
    return check_compliance(action.action_type, params, event_to_dict(event), prior)


def _apply_verdict(action: RecoveryAction, verdict) -> None:
    action.compliance_decision = verdict.decision
    action.compliance_rule_id = verdict.rule_id
    action.compliance_rule_name = verdict.rule_name
    action.compliance_reason = verdict.reason


@router.post("/{action_id}/approve")
async def approve(action_id: str, session: AsyncSession = Depends(get_session)):
    """Merchant approves the agent's proposal as-is — execute it now."""
    action, event = await _load_pending(session, action_id)

    action.status = statuses.APPROVED
    action.result = {**(action.result or {}), "hitl": {"decision": "approved"}}
    await session.commit()

    await sse_manager.broadcast(
        {
            "type": "hitl_resolved",
            "decision": "approved",
            "event_id": str(event.id),
            "action_id": str(action.id),
            "action_type": action.action_type,
        }
    )

    execution = await execute_action(session, action, event=event, force=True)
    return {"status": "approved", "action_id": str(action.id), "execution": execution}


@router.post("/{action_id}/modify")
async def modify(
    action_id: str,
    body: ModifyRequest,
    session: AsyncSession = Depends(get_session),
):
    """Merchant edits the parameters — re-check compliance, then execute.

    A BLOCKED verdict is returned as ``409`` with the citing rule and the action
    is marked ``blocked``: the edit is refused rather than quietly executed.
    """
    action, event = await _load_pending(session, action_id)

    merged = {**(action.action_params or {}), **(body.params or {})}
    verdict = await _recheck_compliance(session, action, event, merged)

    if verdict.blocked:
        action.status = statuses.BLOCKED
        _apply_verdict(action, verdict)
        action.result = {
            **(action.result or {}),
            "hitl": {
                "decision": "modify_rejected",
                "note": body.note,
                "attempted_params": merged,
            },
        }
        event.recovery_status = statuses.EV_BLOCKED
        await session.commit()
        await sse_manager.broadcast(
            {
                "type": "hitl_resolved",
                "decision": "modify_blocked",
                "event_id": str(event.id),
                "action_id": str(action.id),
                "compliance": {
                    "decision": verdict.decision,
                    "rule_id": verdict.rule_id,
                    "rule_name": verdict.rule_name,
                    "reason": verdict.reason,
                },
            }
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Modified action blocked by the Compliance Engine",
                "rule_id": verdict.rule_id,
                "rule_name": verdict.rule_name,
                "reason": verdict.reason,
            },
        )

    # MODIFIED: the engine's correction wins over the merchant's edit.
    if verdict.decision == "MODIFIED" and verdict.modification:
        merged.update(verdict.modification)

    action.action_params = merged
    action.agent_reasoning = merged.get("reason") or action.agent_reasoning
    action.cost_paise = estimate_cost_paise(action.action_type, merged)
    action.status = statuses.APPROVED
    _apply_verdict(action, verdict)
    action.result = {
        **(action.result or {}),
        "hitl": {
            "decision": "modified",
            "note": body.note,
            "overrides": body.params,
            "compliance_recheck": {
                "decision": verdict.decision,
                "rule_id": verdict.rule_id,
                "reason": verdict.reason,
            },
        },
    }
    await session.commit()
    await session.refresh(action)

    await sse_manager.broadcast(
        {
            "type": "hitl_resolved",
            "decision": "modified",
            "event_id": str(event.id),
            "action_id": str(action.id),
            "action_type": action.action_type,
            "params": merged,
            "compliance": {
                "decision": verdict.decision,
                "rule_id": verdict.rule_id,
                "rule_name": verdict.rule_name,
                "reason": verdict.reason,
            },
        }
    )

    execution = await execute_action(session, action, event=event, force=True)
    return {
        "status": "modified",
        "action_id": str(action.id),
        "params": merged,
        "compliance": {
            "decision": verdict.decision,
            "rule_id": verdict.rule_id,
            "rule_name": verdict.rule_name,
            "reason": verdict.reason,
        },
        "execution": execution,
    }


@router.post("/{action_id}/skip")
async def skip(
    action_id: str,
    body: SkipRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Merchant declines the action — nothing executes, the case is closed out."""
    body = body or SkipRequest()
    action, event = await _load_pending(session, action_id)

    action.status = statuses.SKIPPED
    action.result = {
        **(action.result or {}),
        "hitl": {"decision": "skipped", "reason": body.reason},
    }
    if event.recovery_status not in statuses.EV_FINAL_STATUSES:
        event.recovery_status = statuses.EV_SKIPPED
    await session.commit()
    await session.refresh(action)
    await session.refresh(event)

    await sse_manager.broadcast(
        {
            "type": "hitl_resolved",
            "decision": "skipped",
            "event_id": str(event.id),
            "action_id": str(action.id),
            "action_type": action.action_type,
            "reason": body.reason,
            "event": event_to_dict(event),
        }
    )

    return {"status": "skipped", "action_id": str(action.id), "reason": body.reason}
