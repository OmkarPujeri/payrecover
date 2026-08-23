"""Bounded tool set for the Strategy Agent (LLM #2).

Reproduced verbatim from PRD section 14 — this is the *entire* action space the
strategy agent may choose from. Keeping the schema in one module means the same
list feeds (a) the live Groq tool-calling request (``tools=AGENT_TOOLS,
tool_choice="required"``) and (b) validation of whatever tool the model — or the
deterministic planner — ends up selecting.

Nothing here executes anything; these are pure declarations plus a little
metadata (nominal per-action cost) used by the compliance cost-ceiling rule and
the recovery-economics view. Actual execution (calling Razorpay, scheduling)
lands in the Execution Engine phase.
"""
from __future__ import annotations

from typing import Any

# ---- The 6 bounded tools (PRD section 14) -------------------------------- #
AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "schedule_smart_retry",
        "description": (
            "Schedule a payment retry at an optimal time. Use for soft failures "
            "(gateway timeout, bank downtime, network issues). NEVER use if "
            "retry_count >= 3."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "retry_at": {"type": "string", "description": "ISO 8601 timestamp in IST"},
                "payment_method": {
                    "type": "string",
                    "enum": ["card", "upi", "netbanking", "any"],
                },
                "reason": {
                    "type": "string",
                    "description": "Detailed reasoning for this timing and method choice",
                },
            },
            "required": ["order_id", "retry_at", "payment_method", "reason"],
        },
    },
    {
        "name": "generate_payment_link",
        "description": (
            "Create a Razorpay Payment Link for customer self-service recovery. "
            "Use for hard failures requiring customer action (insufficient funds, "
            "card expired, OTP failed)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount_paise": {"type": "integer"},
                "expiry_hours": {"type": "integer", "minimum": 4, "maximum": 168},
                "description": {"type": "string"},
                "customer_email": {"type": "string"},
                "customer_contact": {"type": "string"},
                "notify_sms": {"type": "boolean"},
                "notify_email": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "amount_paise", "expiry_hours", "description", "reason"],
        },
    },
    {
        "name": "send_recovery_notification",
        "description": (
            "Send a recovery nudge via email or SMS. NEVER send more than 1 "
            "notification per 24 hours to the same customer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["email", "sms"]},
                "template_id": {"type": "string"},
                "customer_contact": {"type": "string"},
                "payment_link_url": {"type": "string"},
                "personalized_message": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["channel", "template_id", "customer_contact", "reason"],
        },
    },
    {
        "name": "offer_alternative_method",
        "description": (
            "Suggest switching payment method. Use when one method consistently "
            "fails but alternatives are available (e.g., card failed -> suggest UPI)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "original_method": {"type": "string"},
                "suggested_method": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "original_method", "suggested_method", "reason"],
        },
    },
    {
        "name": "escalate_to_merchant",
        "description": (
            "Flag for human review. Use for terminal failures, edge cases, or when "
            "automated recovery is inappropriate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                "recommended_action": {"type": "string"},
            },
            "required": ["order_id", "reason", "severity"],
        },
    },
    {
        "name": "mark_unrecoverable",
        "description": (
            "Close the recovery case. Use for terminal failures where no recovery "
            "is possible or advisable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "reason"],
        },
    },
]

# Fast membership check for validating a chosen tool name.
TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in AGENT_TOOLS)

# Groq/OpenAI-style tool wrapper (each entry nested under ``function``). Built
# once at import so the live client can pass it straight through.
GROQ_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": tool} for tool in AGENT_TOOLS
]

# ---- Nominal action costs (paise) ---------------------------------------- #
# Recovery is deliberately cheap — that is the ROI story. Retries and link
# creation are free; only an actual outbound SMS carries a real cost.
_SMS_PAISE = 20  # ~ Rs 0.20 per SMS segment

TOOL_COST_PAISE: dict[str, int] = {
    "schedule_smart_retry": 0,
    "generate_payment_link": 0,
    "send_recovery_notification": _SMS_PAISE,
    "offer_alternative_method": 0,
    "escalate_to_merchant": 0,
    "mark_unrecoverable": 0,
}


def estimate_cost_paise(tool_name: str, params: dict[str, Any]) -> int:
    """Best-effort cost of taking an action, in paise.

    Only ``send_recovery_notification`` over SMS carries a real cost; email and
    every other tool are free. Consumed by the compliance cost-ceiling rule.
    """
    if tool_name == "send_recovery_notification":
        return _SMS_PAISE if (params or {}).get("channel") == "sms" else 0
    return TOOL_COST_PAISE.get(tool_name, 0)
