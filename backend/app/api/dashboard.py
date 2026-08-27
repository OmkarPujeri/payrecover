"""Dashboard read endpoints: metrics, economics, comparison, events."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import build_comparison, build_economics
from app.database import get_session
from app.ingest import event_to_dict
from app.models import CircuitBreakerEvent, RecoveryAction, RecoveryEvent
from app.timeutil import to_utc

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


async def _avg_recovery_hours(session: AsyncSession) -> float | None:
    """Mean wall-clock hours from failure to recovery, or ``None`` if nothing has
    recovered yet.

    Computed in Python rather than SQL on purpose: the elapsed-time expression
    differs between SQLite (``julianday``) and Postgres (``EXTRACT(EPOCH ...)``),
    and the project runs on both. Only recovered rows are fetched, so the set is
    small by construction.

    Both timestamps go through ``to_utc`` before subtracting. SQLite hands back
    naive datetimes and Postgres hands back aware ones; subtracting one of each
    raises, and — worse — assuming a zone would reintroduce the 5.5-hour skew this
    project has already been bitten by once.
    """
    rows = (
        await session.execute(
            select(RecoveryEvent.created_at, RecoveryEvent.recovered_at).where(
                RecoveryEvent.recovery_status == "recovered",
                RecoveryEvent.recovered_at.is_not(None),
            )
        )
    ).all()

    spans = []
    for created_at, recovered_at in rows:
        start, end = to_utc(created_at), to_utc(recovered_at)
        if start is None or end is None:
            continue
        hours = (end - start).total_seconds() / 3600
        if hours >= 0:
            spans.append(hours)

    if not spans:
        return None
    return round(sum(spans) / len(spans), 1)


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
        "avg_recovery_hours": await _avg_recovery_hours(session),
        "status_breakdown": {row[0]: row[1] for row in status_rows},
        "failure_breakdown": {(row[0] or "unknown"): row[1] for row in failure_rows},
    }


@router.get("/economics")
async def get_economics(session: AsyncSession = Depends(get_session)):
    """ROI per failure type — which recovery channels actually pay for themselves.

    Grouped on the Razorpay ``error_reason`` rather than on the simulator's
    profile names, so the table means the same thing for live webhook traffic as
    it does for injected traffic. ``failure_label`` is the curated human name the
    Diagnostic Agent assigned; undiagnosed rows fall back to a prettified reason.

    All arithmetic lives in ``app.analytics`` — the client renders
    ``roi_display`` and never divides anything itself.
    """
    recovered_flag = case((RecoveryEvent.recovery_status == "recovered", 1), else_=0)

    rows = (
        await session.execute(
            select(
                RecoveryEvent.error_reason,
                func.max(RecoveryEvent.failure_label),
                func.max(RecoveryEvent.failure_category),
                func.count(RecoveryEvent.id),
                func.coalesce(func.sum(RecoveryEvent.amount), 0),
                func.coalesce(func.sum(recovered_flag), 0),
                func.coalesce(func.sum(RecoveryEvent.recovered_amount), 0),
                func.coalesce(func.sum(RecoveryEvent.recovery_cost_paise), 0),
            ).group_by(RecoveryEvent.error_reason)
        )
    ).all()

    return build_economics(
        {
            "error_reason": r[0],
            "failure_label": r[1],
            "failure_category": r[2],
            "count": r[3],
            "failed_paise": r[4],
            "recovered_count": r[5],
            "recovered_paise": r[6],
            "cost_paise": r[7],
        }
        for r in rows
    )


@router.get("/metrics/comparison")
async def get_comparison(session: AsyncSession = Depends(get_session)):
    """Before/after against the 12% manual-recovery baseline, on the same batch.

    The "without" column is modelled rather than measured and the response says
    so in ``basis`` — the same failures cannot be replayed without the agent, so
    the honest move is to apply the industry manual rate to the identical failed
    amount and label it as an assumption.
    """
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

    return build_comparison(
        failed_paise=failed_amount,
        recovered_paise=recovered_amount,
        total_events=total_events,
        recovered_count=recovered_count,
        avg_recovery_hours=await _avg_recovery_hours(session),
    )


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
