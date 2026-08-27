"""The agent pipeline — one orchestration, reachable from every entry point.

Stages 2-6 for a single already-ingested ``RecoveryEvent``::

    diagnose (LLM #1) -> strategise (LLM #2) -> comply (deterministic) ->
    confidence gate -> execute

This module deliberately starts *after* ingestion, because ingestion is the one
part that legitimately differs by entry point: the simulator mints a synthetic
entity and flags it ``is_simulated``, while the webhook receives a real Razorpay
payload and has to handle duplicate deliveries. Everything downstream of "we
have a failed payment on record" is identical, so it lives here exactly once.

That matters for more than tidiness. This orchestration used to live inline in
``POST /api/simulator/inject``, which meant the agent stages were reachable only
by the simulator — a real ``payment.failed`` webhook was ingested, broadcast, and
then dropped on the floor. The pipeline was real but the front door wasn't
connected to it. Extracting it is what makes the architecture diagram true.

The SSE frames emitted here (``event_diagnosed``, ``strategy_selected``,
``compliance_checked``, ``gate_decided``) are what the dashboard renders as a
live agent trace, so they fire for webhook traffic and simulated traffic alike.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.diagnostic_agent import diagnose_event
from app.agent.strategy_agent import recover_event
from app.execution import statuses
from app.execution.executor import execute_action_by_id
from app.ingest import event_to_dict
from app.models import RecoveryEvent
from app.sse import sse_manager

logger = logging.getLogger("payrecover.pipeline")


async def run_pipeline(
    session: AsyncSession,
    event: RecoveryEvent,
    *,
    diagnose: bool = True,
    recover: bool = True,
    execute: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the agent pipeline over one ingested failure. Returns the event payload.

    The returned dict is the event as the dashboard wants it, progressively
    annotated: ``diagnosis_source`` once diagnosed, ``recovery`` once a strategy
    is chosen and gated, and ``execution`` once acted on. Callers append it to
    their response as-is.

    ``extra`` is merged into every payload the pipeline builds — the simulator
    uses it to carry ``failure_type`` through, which the webhook has no notion of.

    The three flags exist for the simulator's benefit (they let a demo stop after
    diagnosis to talk through it) and default to the full run. Note that
    ``execute`` cannot widen what fires: only an action the confidence gate
    actually approved is executed, so a flag can never talk past the gate.
    """
    extra = extra or {}

    def payload_now() -> dict[str, Any]:
        """A fresh view of the event; the ORM object mutates as stages persist."""
        return {**event_to_dict(event), **extra}

    payload = payload_now()

    # --- Stage 2: diagnosis (LLM #1, falls back to the deterministic brain) ---
    diagnosis = None
    if diagnose:
        diagnosis = await diagnose_event(session, event)
        payload = payload_now()
        payload["diagnosis_source"] = diagnosis["source"]
        await sse_manager.broadcast(
            {"type": "event_diagnosed", "event": payload, "diagnosis": diagnosis}
        )

    # Strategy needs a diagnosis to reason about; without one there is nothing
    # to decide, so the event stays parked at "diagnosed" rather than guessing.
    if not (recover and diagnosis is not None):
        return payload

    # --- Stages 3-5: strategy, compliance, confidence gate ---
    decision = await recover_event(session, event, diagnosis=diagnosis)

    # Stage 3 — the Strategy Agent's chosen action.
    await sse_manager.broadcast(
        {
            "type": "strategy_selected",
            "event_id": decision["recovery_event_id"],
            "strategy": {
                "tool": decision["tool"],
                "reason": decision["reason"],
                "confidence": decision["confidence"],
                "source": decision["source"],
                "risk_factors": decision["risk_factors"],
                "uncertainty_factors": decision["uncertainty_factors"],
            },
        }
    )
    # Stage 4 — the deterministic Compliance Engine's verdict.
    await sse_manager.broadcast(
        {
            "type": "compliance_checked",
            "event_id": decision["recovery_event_id"],
            "compliance": decision["compliance"],
        }
    )
    # Stage 5 — the confidence/HITL gate's routing + final status.
    recovery_summary = {
        "action_id": decision["action_id"],
        "tool": decision["tool"],
        "status": decision["status"],
        "confidence": decision["confidence"],
        "compliance_decision": decision["compliance"]["decision"],
        "gate_action": decision["gate"]["action"],
        "requires_human": decision["gate"]["requires_human"],
        "source": decision["source"],
    }
    payload = payload_now()
    payload["diagnosis_source"] = diagnosis["source"]
    payload["recovery"] = recovery_summary
    await sse_manager.broadcast(
        {
            "type": "gate_decided",
            "event": payload,
            "gate": decision["gate"],
            "status": decision["status"],
            "scheduled_at": decision["scheduled_at"],
            "action_id": decision["action_id"],
        }
    )

    # --- Stage 6: execution, but only for what the gate cleared ---
    # An action parked in pending_review/escalated stays parked: the whole point
    # of the gate is that the machine does not act on its own low-confidence
    # judgement, and no caller's flag may undo that.
    if execute and decision["status"] == statuses.APPROVED:
        execution = await execute_action_by_id(session, decision["action_id"])
        payload = payload_now()
        payload["diagnosis_source"] = diagnosis["source"]
        payload["recovery"] = {
            **recovery_summary,
            "executed": bool(execution and execution.get("executed")),
            "execution_status": (execution or {}).get("status"),
            "execution_reason": (execution or {}).get("reason"),
        }
        payload["execution"] = execution

    return payload
