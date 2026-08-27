"""Chaos presets — one-click demo scenarios, as declarative data.

Each preset is a short script over primitives that already exist: inject a
failure, fast-forward the clock, replay a state change. Nothing here reaches into
the agent or the compliance engine, which is the point — a preset must not be
able to produce an outcome the normal pipeline could not, or the demo stops being
evidence.

The registry is pure data so the runner in ``api/simulator_routes.py`` stays a
loop over three verbs, and so ``GET /api/simulator/presets`` can drive the
dashboard's button row straight off the server. Adding a preset is a dict entry,
not a code change, and the frontend picks it up with no edit.

**Amounts are pinned, not random, wherever a preset promises a routing outcome.**
The mock enricher derives synthetic customer history from ``sha256(identity)``,
so confidence — and therefore whether the gate auto-approves — varies per
injected customer. Two combinations are provably deterministic across all 64
histories: ``bank_downtime`` under ₹10,000 always auto-approves a retry, and
anything above ₹10,000 always routes to a human. Presets that claim "watch it
auto-execute" or "watch HITL fire" use those; presets that just want volume
leave the amount random.
"""
from __future__ import annotations

from typing import Any

__all__ = ["CHAOS_PRESETS", "preset_summaries"]

# Pinned amounts, in paise. Named so the reason survives a copy-paste.
_DETERMINISTIC_AUTO = 250_000      # ₹2,500 — under the HITL ceiling
_HIGH_VALUE = 2_500_000            # ₹25,000 — over ₹10,000, so always human-gated


CHAOS_PRESETS: dict[str, dict[str, Any]] = {
    "hdfc_bank_crash": {
        "name": "⚡ HDFC Bank Crash",
        "description": "5 payments queued during HDFC downtime, watch retry-on-recovery",
        "narrative": (
            "Five card payments fail against a down issuer. Every one is classified "
            "soft, auto-approved, and given a scheduled retry rather than a message — "
            "nobody gets nagged for the bank's outage. The clock jumps forward, the "
            "retries fire, and when three customers pay CB-001 cancels the remaining "
            "work on those orders instantly."
        ),
        "available": True,
        "steps": [
            {"op": "inject", "failure_type": "bank_downtime", "amount": _DETERMINISTIC_AUTO, "method": "card", "count": 5},
            {"op": "fast_forward", "hours": 12},
            {"op": "circuit", "event_type": "payment.captured", "target": "first", "n": 3},
        ],
    },
    "salary_day_batch": {
        "name": "💸 Salary Day Batch",
        "description": "20 insufficient-funds failures, mixed values, clearing after payday",
        "narrative": (
            "A month-end wave of insufficient-funds failures at mixed order values. "
            "Watch the gate split the batch: small orders auto-execute, anything over "
            "₹10,000 parks in the HITL queue for a human regardless of how confident "
            "the agent is. Then the clock jumps to payday and the scheduled work fires."
        ),
        "available": True,
        "steps": [
            {"op": "inject", "failure_type": "insufficient_funds", "count": 16},
            {"op": "inject", "failure_type": "insufficient_funds", "amount": _HIGH_VALUE, "count": 4},
            {"op": "fast_forward", "hours": 72},
        ],
    },
    "dispute_storm": {
        "name": "⛔ Dispute Storm",
        "description": "3 chargebacks fire while recovery is in progress",
        "narrative": (
            "Three recoveries are mid-flight with retries on the schedule when the "
            "chargebacks land. CB-002 trips on each one and cancels every pending "
            "action — chasing a customer who has already disputed is how a merchant "
            "loses the representment. The audit trail records the trip and the count "
            "of actions it killed."
        ),
        "available": True,
        "steps": [
            {"op": "inject", "failure_type": "bank_downtime", "amount": _DETERMINISTIC_AUTO, "count": 3},
            {"op": "circuit", "event_type": "payment.dispute.created", "target": "all"},
        ],
    },
    "upi_timeout_wave": {
        "name": "📱 UPI Timeout Wave",
        "description": "8 UPI PSP timeouts, retries time-shifted out of peak hours",
        "narrative": (
            "Eight UPI payments time out at the PSP. Nothing fires immediately — the "
            "compliance engine shifts every retry out of NPCI peak hours and every "
            "notification into the TRAI 09:00-20:00 window, so the schedule fills up "
            "with work that is legal by construction. Watch the compliance band, not "
            "the feed."
        ),
        "available": True,
        "steps": [
            {"op": "inject", "failure_type": "network_timeout", "method": "upi", "count": 8},
        ],
    },
    "high_value_hitl": {
        "name": "🎯 High-Value HITL",
        "description": "₹25,000 order fails, over the ceiling, always needs a human",
        "narrative": (
            "A ₹25,000 order fails. The agent still does the full analysis and still "
            "proposes a bounded action, but the gate refuses to execute it: orders "
            "above ₹10,000 always go to a human, no matter how confident the model is. "
            "Approve it from the queue and it executes immediately."
        ),
        "available": True,
        "steps": [
            {"op": "inject", "failure_type": "insufficient_funds", "amount": _HIGH_VALUE, "count": 1},
        ],
    },
    "cascade_failure": {
        "name": "🔄 Cascade Failure",
        "description": "One order fails, the retry fails differently, the agent pivots",
        "narrative": (
            "The retry fires and fails with a *different* error than the original, so "
            "the Diagnostic Agent re-classifies soft to hard and the Strategy Agent "
            "abandons retrying for a payment link. The customer pays via UPI two days "
            "later. This is the agent adapting rather than repeating."
        ),
        # Deferred to phase 5c: a retry that fails with a *new* error is the one
        # preset that needs backend behaviour rather than composition. Shipped as
        # unavailable so the dashboard can render it disabled from the registry and
        # enabling it later is a one-line change with no frontend edit.
        "available": False,
        "unavailable_reason": "Cascade mode arrives with phase 5c — needs a retry that fails with a new error, which is new backend behaviour rather than a composition of existing endpoints.",
        "steps": [],
    },
}


def preset_summaries() -> list[dict[str, Any]]:
    """The registry as the dashboard consumes it — no ``steps``.

    Step scripts are an implementation detail; exposing them would invite a client
    to reimplement a preset and drift from the server.
    """
    return [
        {
            "preset": key,
            "name": p["name"],
            "description": p["description"],
            "narrative": p["narrative"],
            "available": p["available"],
            "unavailable_reason": p.get("unavailable_reason"),
            "event_count": sum(
                s.get("count", 0) for s in p["steps"] if s["op"] == "inject"
            ),
        }
        for key, p in CHAOS_PRESETS.items()
    ]
