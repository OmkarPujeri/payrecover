"""Audit trail endpoints — /api/audit/* (PRD sections 21 & 22, step 8).

Every decision and every execution already writes a ``recovery_actions`` row
carrying the full reasoning chain: which agent acted, what it chose, why, how
confident it was, what the Compliance Engine ruled, and what actually happened.
These endpoints expose that chain as a filterable log plus a CSV/JSON export.

This is the "show me your work" surface: for any recovered rupee a merchant (or
an auditor) can trace the exact chain of custody from failure to outcome, and
nothing in the chain was authored by a language model without a deterministic
rule signing off on it.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import CircuitBreakerEvent, RecoveryAction, RecoveryEvent

router = APIRouter(prefix="/api/audit", tags=["audit"])

#: Flat column order used by the CSV export.
_CSV_COLUMNS = [
    "timestamp",
    "recovery_event_id",
    "razorpay_order_id",
    "razorpay_payment_id",
    "amount_inr",
    "failure_category",
    "failure_label",
    "agent_name",
    "action_type",
    "confidence_score",
    "compliance_decision",
    "compliance_rule_id",
    "compliance_rule_name",
    "status",
    "cost_paise",
    "scheduled_at",
    "executed_at",
    "agent_reasoning",
]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _audit_row(action: RecoveryAction, event: RecoveryEvent | None) -> dict[str, Any]:
    """One fully-joined audit record."""
    result = action.result or {}
    return {
        "id": str(action.id),
        "timestamp": _iso(action.created_at),
        "recovery_event_id": str(action.recovery_event_id),
        "razorpay_order_id": event.razorpay_order_id if event else None,
        "razorpay_payment_id": event.razorpay_payment_id if event else None,
        "amount_paise": event.amount if event else None,
        "amount_inr": round((event.amount or 0) / 100, 2) if event else None,
        "failure_category": event.failure_category if event else None,
        "failure_label": event.failure_label if event else None,
        "recovery_status": event.recovery_status if event else None,
        "agent_name": action.agent_name,
        "action_type": action.action_type,
        "action_params": action.action_params,
        "agent_reasoning": action.agent_reasoning,
        "confidence_score": action.confidence_score,
        "risk_factors": action.risk_factors or [],
        "uncertainty_factors": action.uncertainty_factors or [],
        "compliance_decision": action.compliance_decision,
        "compliance_rule_id": action.compliance_rule_id,
        "compliance_rule_name": action.compliance_rule_name,
        "compliance_reason": action.compliance_reason,
        "status": action.status,
        "cost_paise": action.cost_paise,
        "scheduled_at": _iso(action.scheduled_at),
        "executed_at": _iso(action.executed_at),
        "source": result.get("source"),
        "gate": result.get("gate"),
        "result": result,
    }


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """Reduce an audit record to the flat CSV column set."""
    return {col: row.get(col) for col in _CSV_COLUMNS}


async def _query_actions(
    session: AsyncSession,
    *,
    event_id: str | None,
    agent_name: str | None,
    action_type: str | None,
    status: str | None,
    compliance_decision: str | None,
    limit: int | None,
    skip: int,
) -> tuple[int, list[dict[str, Any]]]:
    stmt = select(RecoveryAction)
    count_stmt = select(func.count(RecoveryAction.id))

    filters = []
    if event_id:
        try:
            filters.append(RecoveryAction.recovery_event_id == uuid.UUID(event_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid event_id") from exc
    if agent_name:
        filters.append(RecoveryAction.agent_name == agent_name)
    if action_type:
        filters.append(RecoveryAction.action_type == action_type)
    if status:
        filters.append(RecoveryAction.status == status)
    if compliance_decision:
        filters.append(RecoveryAction.compliance_decision == compliance_decision.upper())

    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)

    total = await session.scalar(count_stmt) or 0
    # `limit=None` means unbounded — used by the export, which must never
    # silently truncate: a file with fewer rows than the log claims is worse
    # than no file, because it looks authoritative.
    query = stmt.order_by(RecoveryAction.created_at.desc()).offset(skip)
    if limit is not None:
        query = query.limit(limit)
    actions = (await session.scalars(query)).all()

    # Cache events so a page of actions on one event is a single lookup.
    events: dict[Any, RecoveryEvent | None] = {}
    rows: list[dict[str, Any]] = []
    for action in actions:
        key = action.recovery_event_id
        if key not in events:
            events[key] = await session.get(RecoveryEvent, key)
        rows.append(_audit_row(action, events[key]))

    return total, rows


@router.get("/log")
async def audit_log(
    event_id: str | None = Query(None, description="Filter to one recovery event"),
    agent_name: str | None = Query(None, description="diagnostic | strategy"),
    action_type: str | None = Query(None, description="Tool name, e.g. generate_payment_link"),
    status: str | None = Query(None, description="Action status, e.g. completed"),
    compliance_decision: str | None = Query(None, description="APPROVED | MODIFIED | BLOCKED"),
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """The full agent reasoning chain, newest first and filterable."""
    total, rows = await _query_actions(
        session,
        event_id=event_id,
        agent_name=agent_name,
        action_type=action_type,
        status=status,
        compliance_decision=compliance_decision,
        limit=limit,
        skip=skip,
    )
    return {"total": total, "limit": limit, "skip": skip, "entries": rows}


@router.get("/breakers")
async def breaker_log(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """Every circuit breaker that has fired, newest first."""
    rows = (
        await session.scalars(
            select(CircuitBreakerEvent)
            .order_by(CircuitBreakerEvent.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "total": len(rows),
        "breakers": [
            {
                "id": str(c.id),
                "recovery_event_id": str(c.recovery_event_id),
                "trigger_id": c.trigger_id,
                "trigger_type": c.trigger_type,
                "trigger_details": c.trigger_details,
                "cancelled_actions": c.cancelled_actions,
                "created_at": _iso(c.created_at),
            }
            for c in rows
        ],
    }


@router.get("/export")
async def export_audit(
    format: str = Query("csv", pattern="^(csv|json)$"),
    event_id: str | None = None,
    agent_name: str | None = None,
    action_type: str | None = None,
    status: str | None = None,
    compliance_decision: str | None = None,
    # No default: an export that quietly stopped at a ceiling would hand a
    # judge a file with fewer rows than the drawer says exist. Callers who
    # want a bounded export may pass one explicitly.
    limit: int | None = Query(None, ge=1, le=20000),
    session: AsyncSession = Depends(get_session),
):
    """Download the audit trail as CSV (flat) or JSON (full nested records)."""
    _, rows = await _query_actions(
        session,
        event_id=event_id,
        agent_name=agent_name,
        action_type=action_type,
        status=status,
        compliance_decision=compliance_decision,
        limit=limit,
        skip=0,
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if format == "json":
        return Response(
            content=json.dumps({"exported_at": stamp, "entries": rows}, default=str, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="payrecover_audit_{stamp}.json"'
            },
        )

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(_flatten(row))

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="payrecover_audit_{stamp}.csv"'
        },
    )
