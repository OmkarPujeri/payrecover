"""Confidence-based Human-in-the-Loop (HITL) gate — deterministic routing.

The Strategy Agent proposes an action and the Compliance Engine clears it; this
gate then decides *who presses go*. It maps a confidence score (0-100) onto one
of four routes, with a hard override that forces human review for high-value
orders regardless of how confident the agent is.

    confidence 85-100  -> AUTO_EXECUTE          (act now)
    confidence 70-84   -> AUTO_EXECUTE_FLAGGED  (act now, but flag for monitoring)
    confidence 50-69   -> HITL_REVIEW           (pause for a human to approve)
    confidence  0-49   -> ESCALATE              (hand to the merchant)

Override: any order above Rs 10,000 (1,000,000 paise) is pinned to HITL_REVIEW
regardless of confidence — a large order is never silently auto-executed *nor*
silently auto-escalated/closed without a human decision. The downside of an
automated mistake (or an automated write-off) on a large order is not worth the
saved click.

Pure and side-effect-free, so the routing is trivially testable and identical in
mock and live modes. Confidence itself is produced deterministically upstream
(see ``strategy.planner``), so this whole gate is hallucination-proof.
"""
from __future__ import annotations

from dataclasses import dataclass

# Routing outcomes.
AUTO_EXECUTE = "auto_execute"
AUTO_EXECUTE_FLAGGED = "auto_execute_flagged"
HITL_REVIEW = "hitl_review"
ESCALATE = "escalate"

# Orders above this value always require human approval (paise). Rs 10,000.
HITL_ORDER_VALUE_PAISE = 10_00_000

# Confidence tier thresholds (inclusive lower bounds).
_HIGH = 85
_MODERATE = 70
_LOW = 50


@dataclass
class GateDecision:
    action: str          # one of the routing constants above
    requires_human: bool
    tier: str            # high | moderate | low | very_low
    confidence: int
    reason: str

    @property
    def auto(self) -> bool:
        """True when the pipeline may execute without waiting for a human."""
        return self.action in (AUTO_EXECUTE, AUTO_EXECUTE_FLAGGED)


def evaluate(confidence: int, order_value_paise: int = 0) -> GateDecision:
    """Route a proposed action by confidence, with a high-value HITL override."""
    c = max(0, min(100, int(confidence or 0)))
    high_value = int(order_value_paise or 0) > HITL_ORDER_VALUE_PAISE

    if c >= _HIGH:
        tier, action, human = "high", AUTO_EXECUTE, False
        reason = f"High confidence ({c}): executing automatically."
    elif c >= _MODERATE:
        tier, action, human = "moderate", AUTO_EXECUTE_FLAGGED, False
        reason = f"Moderate confidence ({c}): executing but flagged for monitoring."
    elif c >= _LOW:
        tier, action, human = "low", HITL_REVIEW, True
        reason = f"Low confidence ({c}): pausing for merchant review."
    else:
        tier, action, human = "very_low", ESCALATE, True
        reason = f"Very low confidence ({c}): escalating to the merchant."

    # High-value override: a large order is ALWAYS pinned to human review — we
    # neither auto-execute it nor auto-escalate/close it without a human. (An
    # order already routed to HITL_REVIEW needs no change.)
    if high_value and action != HITL_REVIEW:
        return GateDecision(
            action=HITL_REVIEW,
            requires_human=True,
            tier=tier,
            confidence=c,
            reason=(
                f"Order value Rs {order_value_paise / 100:,.0f} exceeds the "
                f"Rs {HITL_ORDER_VALUE_PAISE / 100:,.0f} auto-handling ceiling, so it is "
                f"pinned to human review regardless of {tier} confidence ({c})."
            ),
        )

    return GateDecision(
        action=action,
        requires_human=human,
        tier=tier,
        confidence=c,
        reason=reason,
    )
