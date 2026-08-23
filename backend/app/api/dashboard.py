"""Dashboard read endpoints: metrics, events list, and event detail."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.ingest import event_to_dict
from app.models import CircuitBreakerEvent, RecoveryAction, RecoveryEvent

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _action_to_dict(a: RecoveryAction) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "agent_name": a.agent_name,
        "action_type": a.action_type,
        "action_params": a.action_params,
        "agent_reasoning": a.agent_reasoning,
        "confidence_score": a.confidence_score,
        "risk_factors": a.risk_factors,
        "uncertainty_factors": a.uncertainty_factors,
        "compliance_decision": a.compliance_decision,
        "compliance_rule_id": a.compliance_rule_id,
        "compliance_rule_name": a.compliance_rule_name,
        "compliance_reason": a.compliance_reason,
        "status": a.status,
        "result": a.result,
        "cost_paise": a.cost_paise,
        "scheduled_at": a.scheduled_at.isoformat() if a.scheduled_at else None,
        "executed_at": a.executed_at.isoformat() if a.executed_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _cb_to_dict(c: CircuitBreakerEvent) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "trigger_type": c.trigger_type,
        "trigger_id": c.trigger_id,
        "trigger_details": c.trigger_details,
        "cancelled_actions": c.cancelled_actions,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/metrics")
async def get_metrics(session: AsyncSession = Depends(get_session)):
    total_events = await session.scalar(select(func.count(RecoveryEvent.id))) or 0
    failed_amount = await session.scalar(
        select(func.coalesce(func.sum(RecoveryEvent.amount), 0))
    ) or 0
    recovered_amount = await session.scalar(
        select(func.coalesce(func.sum(RecoveryEvent.recovered_amount), 0))
    ) or 0
    recovered_count = await session.scalar(
        select(func.count(RecoveryEvent.id)).where(
            RecoveryEvent.recovery_status == "recovered"
        )
    ) or 0
    recovery_cost = await session.scalar(
        select(func.coalesce(func.sum(RecoveryEvent.recovery_cost_paise), 0))
    ) or 0

    status_rows = (
        await session.execute(
            select(RecoveryEvent.recovery_status, func.count())
            .group_by(RecoveryEvent.recovery_status)
        )
    ).all()
    failure_rows = (
        await session.execute(
            select(RecoveryEvent.error_reason, func.count())
            .group_by(RecoveryEvent.error_reason)
        )
    ).all()

    rate_by_amount = (recovered_amount / failed_amount * 100) if failed_amount else 0.0
    rate_by_count = (recovered_count / total_events * 100) if total_events else 0.0

    return {
        "total_events": total_events,
        "failed_amount_paise": failed_amount,
        "failed_amount_inr": round(failed_amount / 100, 2),
        "recovered_amount_paise": recovered_amount,
        "recovered_amount_inr": round(recovered_amount / 100, 2),
        "recovered_count": recovered_count,
        "recovery_cost_paise": recovery_cost,
        "recovery_cost_inr": round(recovery_cost / 100, 2),
        "recovery_rate_by_amount_pct": round(rate_by_amount, 1),
        "recovery_rate_by_count_pct": round(rate_by_count, 1),
        "status_breakdown": {row[0]: row[1] for row in status_rows},
        "failure_breakdown": {(row[0] or "unknown"): row[1] for row in failure_rows},
    }


@router.get("/events")
async def list_events(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    base = select(RecoveryEvent)
    count_stmt = select(func.count(RecoveryEvent.id))
    if status:
        base = base.where(RecoveryEvent.recovery_status == status)
        count_stmt = count_stmt.where(RecoveryEvent.recovery_status == status)

    total = await session.scalar(count_stmt) or 0
    rows = (
        await session.scalars(
            base.order_by(RecoveryEvent.created_at.desc()).limit(limit).offset(skip)
        )
    ).all()

    return {
        "total": total,
        "limit": limit,
        "skip": skip,
        "events": [event_to_dict(e) for e in rows],
    }


@router.get("/events/{event_id}")
async def get_event(event_id: str, session: AsyncSession = Depends(get_session)):
    try:
        uid = uuid.UUID(event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid event id") from exc

    event = await session.get(RecoveryEvent, uid)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    data = event_to_dict(event)
    data["actions"] = [
        _action_to_dict(a) for a in sorted(event.actions, key=lambda a: a.created_at)
    ]
    data["circuit_breaker_events"] = [
        _cb_to_dict(c) for c in event.circuit_breaker_events
    ]
    return data
