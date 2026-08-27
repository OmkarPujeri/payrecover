"""Action endpoints — /api/actions/* (PRD sections 17 & 21).

Deciding and acting are deliberately decoupled: the Strategy Agent persists a
``RecoveryAction`` and the executor carries it out. That seam is what makes the
system safe (a decision can be reviewed before it fires) and it deserves a
first-class HTTP surface:

* ``POST /api/actions/{id}/execute`` — run one action on demand. Idempotent, so
  calling it twice is harmless; the response's ``executed`` flag and ``reason``
  say exactly what happened and why.
* ``GET /api/actions/{id}`` — the action's full record, including the compliance
  verdict, the confidence gate, and the execution result.
* ``GET /api/actions`` — list/filter actions, e.g. everything scheduled.

The execute endpoint exists mainly so a human (or a demo script) can drive the
executor without going through the full webhook pipeline; the same function is
what ``/inject``, the HITL approve path, and the scheduler all call.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.execution import statuses
from app.execution.executor import execute_action
from app.models import RecoveryAction, RecoveryEvent

router = APIRouter(prefix="/api/actions", tags=["actions"])


class ExecuteRequest(BaseModel):
    force: bool = Field(
        default=False,
        description="Execute even if the action is not in 'approved' (e.g. escalated)",
    )
    ignore_defer: bool = Field(
        default=False,
        description="Skip the CB-008 TRAI window deferral check and send now",
    )
    now: datetime | None = Field(
        default=None,
        description="Override the execution instant — for reproducible demos and tests",
    )


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid action id") from exc


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _action_dict(action: RecoveryAction, event: RecoveryEvent | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(action.id),
        "recovery_event_id": str(action.recovery_event_id),
        "agent_name": action.agent_name,
        "action_type": action.action_type,
        "action_params": action.action_params,
        "agent_reasoning": action.agent_reasoning,
        "confidence_score": action.confidence_score,
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
        "cost_paise": action.cost_paise,
        "scheduled_at": _iso(action.scheduled_at),
        "executed_at": _iso(action.executed_at),
        "created_at": _iso(action.created_at),
        "result": action.result,
    }
    if event is not None:
        payload["event"] = {
            "id": str(event.id),
            "razorpay_order_id": event.razorpay_order_id,
            "razorpay_payment_id": event.razorpay_payment_id,
            "amount_paise": event.amount,
            "amount_inr": round((event.amount or 0) / 100, 2),
            "failure_label": event.failure_label,
            "recovery_status": event.recovery_status,
            "recovery_attempts": event.recovery_attempts,
            "customer_name": event.customer_name,
        }
    return payload


@router.get("")
async def list_actions(
    event_id: str | None = Query(None, description="Filter to one recovery event"),
    status: str | None = Query(None, description="Action status, e.g. scheduled"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """Actions, newest first. Filter by event or status."""
    stmt = select(RecoveryAction)
    if event_id:
        stmt = stmt.where(RecoveryAction.recovery_event_id == _parse_uuid(event_id))
    if status:
        stmt = stmt.where(RecoveryAction.status == status)

    rows = (
        await session.scalars(
            stmt.order_by(RecoveryAction.created_at.desc()).limit(limit)
        )
    ).all()
    return {"total": len(rows), "actions": [_action_dict(a) for a in rows]}


@router.get("/scheduled")
async def list_scheduled(session: AsyncSession = Depends(get_session)):
    """Queued work: retries waiting to fire and notifications deferred by CB-008."""
    rows = (
        await session.scalars(
            select(RecoveryAction)
            .where(RecoveryAction.status == statuses.SCHEDULED)
            .order_by(RecoveryAction.scheduled_at.asc())
        )
    ).all()
    return {
        "total": len(rows),
        "scheduled": [
            {
                "action_id": str(a.id),
                "recovery_event_id": str(a.recovery_event_id),
                "action_type": a.action_type,
                "scheduled_at": _iso(a.scheduled_at),
                "deferred_to": (a.result or {}).get("deferred_to"),
                "deferred_reason": (a.result or {}).get("deferred_reason"),
                "retry_order_id": (a.result or {}).get("retry_order_id"),
                "attempt": (a.result or {}).get("attempt"),
            }
            for a in rows
        ],
    }


@router.get("/{action_id}")
async def get_action(action_id: str, session: AsyncSession = Depends(get_session)):
    """One action's complete record — decision, compliance verdict, and outcome."""
    action = await session.get(RecoveryAction, _parse_uuid(action_id))
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    event = await session.get(RecoveryEvent, action.recovery_event_id)
    return _action_dict(action, event)


@router.post("/{action_id}/execute")
async def execute(
    action_id: str,
    body: ExecuteRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Execute one action now.

    Idempotent: a second call on a completed action is a no-op that reports
    ``executed: false`` with ``reason: "already_final"`` rather than acting twice.
    A circuit-breaker trip or a TRAI-window deferral is likewise reported as a
    non-execution with the citing breaker, not as an error.
    """
    body = body or ExecuteRequest()
    action = await session.get(RecoveryAction, _parse_uuid(action_id))
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")

    if action.status == statuses.BLOCKED and not body.force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Action was blocked by the Compliance Engine and cannot be executed",
                "rule_id": action.compliance_rule_id,
                "rule_name": action.compliance_rule_name,
                "reason": action.compliance_reason,
            },
        )

    return await execute_action(
        session,
        action,
        now=body.now,
        force=body.force,
        ignore_defer=body.ignore_defer,
    )
