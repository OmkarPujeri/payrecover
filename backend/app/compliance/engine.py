"""Compliance Engine — deterministic, NOT an LLM (PRD section 15).

This is the headline design decision of PayRecover: compliance is enforced by
**code**, never by a model. An LLM asked to "check compliance" can hallucinate
an APPROVED where it should BLOCK; pure if/else logic cannot. It is also faster
(no extra ~500ms call) and free (no Groq quota burned on rule checks).

``check_compliance`` takes a *proposed* action (the tool the Strategy Agent
chose) plus current state and prior actions, and returns a
:class:`ComplianceResult` with one of three decisions:

* ``APPROVED`` — take the action as-is.
* ``MODIFIED`` — take the action, but with the returned ``modification`` merged
  into its params (e.g. a retry shifted out of NPCI peak hours, or an SMS
  switched to email for a DND customer).
* ``BLOCKED`` — do not take the action; ``rule_id`` cites why.

Pure and dependency-light (stdlib + ``zoneinfo``), so every rule is directly
unit-testable with an injected ``now``. Times are IST; amounts are in **paise**.

The 8 rules (PRD section 15):
    NPCI-001  Retry cap (max 3 retries / order)               -> BLOCKED
    NPCI-002  Peak hours (10-13, 17-21:30 IST) no retries      -> MODIFIED
    TRAI-001  Notification hours (09-20 IST only)              -> MODIFIED
    FREQ-001  Max 1 notification / 24h / customer              -> BLOCKED
    DND-001   DND customer -> email only (no SMS)              -> MODIFIED
    DISP-001  Active dispute halts all recovery                -> BLOCKED
    WINDOW-001 Recovery window > 14 days                       -> BLOCKED
    COST-001  Cumulative recovery cost > 15% of order value    -> BLOCKED
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.tools import estimate_cost_paise

IST = ZoneInfo("Asia/Kolkata")

# NPCI peak windows (no retries scheduled inside these) — IST.
_PEAK_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(10, 0), time(13, 0)),
    (time(17, 0), time(21, 30)),
)

# TRAI promotional/transactional messaging window — IST.
_TRAI_START = time(9, 0)
_TRAI_END = time(20, 0)

_MAX_RETRIES = 3
_MAX_WINDOW_DAYS = 14
_COST_CEILING_FRACTION = 0.15
_NOTIF_COOLDOWN_SECONDS = 24 * 3600

# Tool / action-type names (must match app.agent.tools + planner).
_RETRY = "schedule_smart_retry"
_NOTIFY = "send_recovery_notification"


@dataclass
class ComplianceResult:
    decision: str                         # APPROVED | MODIFIED | BLOCKED
    reason: str
    rule_id: str | None = None
    rule_name: str | None = None
    modification: dict[str, Any] | None = None

    @property
    def approved(self) -> bool:
        return self.decision in ("APPROVED", "MODIFIED")

    @property
    def blocked(self) -> bool:
        return self.decision == "BLOCKED"


# ---- Time helpers -------------------------------------------------------- #
def _to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def is_peak_hour(dt: datetime) -> bool:
    """True if ``dt`` (IST) falls inside any NPCI peak window."""
    t = _to_ist(dt).time()
    return any(start <= t < end for start, end in _PEAK_WINDOWS)


def next_non_peak_time(dt: datetime) -> datetime:
    """The earliest instant >= ``dt`` that is outside every peak window."""
    cur = _to_ist(dt)
    for start, end in _PEAK_WINDOWS:
        if start <= cur.time() < end:
            # Jump to the end of the window we're inside.
            cur = cur.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return cur


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


# ---- The engine ---------------------------------------------------------- #
def check_compliance(
    action_type: str,
    action_params: dict[str, Any],
    recovery_event: dict[str, Any],
    prior_actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> ComplianceResult:
    """Run all 8 compliance rules against a proposed action.

    Rules are ordered so that hard, event-level blocks (dispute, expired window)
    win over action-specific modifications. Only the *first* triggered rule is
    returned — that is the binding decision.
    """
    action_params = action_params or {}
    prior_actions = prior_actions or []
    now_ist = _to_ist(now or datetime.now(IST))

    # RULE 6: Active dispute halts ALL recovery (checked first — overrides all).
    if recovery_event.get("has_dispute"):
        return ComplianceResult(
            decision="BLOCKED",
            rule_id="DISP-001",
            rule_name="Active Dispute",
            reason="Active dispute on this payment. All recovery actions halted.",
        )

    # RULE 7: Max recovery window (14 days).
    days_since = _days_since_failure(recovery_event, now_ist)
    if days_since > _MAX_WINDOW_DAYS:
        return ComplianceResult(
            decision="BLOCKED",
            rule_id="WINDOW-001",
            rule_name="Max Recovery Window",
            reason=f"Recovery window expired ({days_since:.0f} days > {_MAX_WINDOW_DAYS} day max).",
        )

    # RULE 8: Cost ceiling (cumulative recovery cost < 15% of order value).
    order_amount = int(recovery_event.get("amount") or 0)
    prospective = estimate_cost_paise(action_type, action_params)
    cumulative = sum(int(a.get("cost_paise") or 0) for a in prior_actions) + prospective
    if order_amount > 0 and cumulative > order_amount * _COST_CEILING_FRACTION:
        return ComplianceResult(
            decision="BLOCKED",
            rule_id="COST-001",
            rule_name="Cost Ceiling",
            reason=(
                f"Recovery cost (Rs {cumulative / 100:.2f}) would exceed 15% of order "
                f"(Rs {order_amount / 100:.2f}). Escalate instead."
            ),
        )

    # RULE 1: NPCI retry cap (max 3 retries per order).
    if action_type == _RETRY:
        retry_count = sum(1 for a in prior_actions if a.get("action_type") == _RETRY)
        if retry_count >= _MAX_RETRIES:
            return ComplianceResult(
                decision="BLOCKED",
                rule_id="NPCI-001",
                rule_name="NPCI Retry Cap",
                reason=f"Retry cap reached ({retry_count}/{_MAX_RETRIES}). NPCI mandates max 3 retries.",
            )

    # RULE 2: NPCI peak hours -> shift the retry to the next safe window.
    if action_type == _RETRY:
        retry_dt = _parse_dt(action_params.get("retry_at"))
        if retry_dt is not None and is_peak_hour(retry_dt):
            safe = next_non_peak_time(retry_dt)
            return ComplianceResult(
                decision="MODIFIED",
                rule_id="NPCI-002",
                rule_name="NPCI Peak Hours",
                modification={"retry_at": safe.isoformat()},
                reason=(
                    f"Retry shifted from {_to_ist(retry_dt).strftime('%I:%M %p')} to "
                    f"{safe.strftime('%I:%M %p')} IST (outside NPCI peak hours 10-13, 17-21:30)."
                ),
            )

    # RULE 5: DND customer -> email only (evaluate before the TRAI/freq checks
    # so an SMS to a DND customer is corrected to email first).
    if action_type == _NOTIFY:
        channel = (action_params.get("channel") or "").lower()
        if recovery_event.get("customer_dnd") and channel == "sms":
            return ComplianceResult(
                decision="MODIFIED",
                rule_id="DND-001",
                rule_name="DND Registry",
                modification={"channel": "email"},
                reason="Customer is on DND. Switching the notification from SMS to email.",
            )

    # RULE 3: TRAI notification hours (09:00-20:00 IST only).
    if action_type == _NOTIFY:
        t = now_ist.time()
        if t < _TRAI_START or t > _TRAI_END:
            next_window = now_ist.replace(hour=9, minute=0, second=0, microsecond=0)
            if t > _TRAI_END:
                next_window += timedelta(days=1)
            return ComplianceResult(
                decision="MODIFIED",
                rule_id="TRAI-001",
                rule_name="TRAI Notification Hours",
                modification={"scheduled_at": next_window.isoformat()},
                reason=(
                    f"Notification queued for {next_window.strftime('%d %b %I:%M %p')} "
                    "(TRAI messaging window is 9 AM-8 PM only)."
                ),
            )

    # RULE 4: Notification frequency (max 1 per 24h per customer).
    if action_type == _NOTIFY:
        contact = action_params.get("customer_contact") or ""
        recent = [
            a for a in prior_actions
            if a.get("action_type") == _NOTIFY
            and (a.get("customer_contact") or a.get("action_params", {}).get("customer_contact")) == contact
            and _within_cooldown(a.get("executed_at"), now_ist)
        ]
        if recent:
            return ComplianceResult(
                decision="BLOCKED",
                rule_id="FREQ-001",
                rule_name="Notification Frequency Cap",
                reason=f"Customer already notified {len(recent)}x in the last 24h. Max 1/day.",
            )

    # ALL CHECKS PASSED.
    return ComplianceResult(
        decision="APPROVED",
        reason="All compliance checks passed.",
    )


# ---- Rule helpers -------------------------------------------------------- #
def _days_since_failure(recovery_event: dict[str, Any], now_ist: datetime) -> float:
    """Prefer an explicit ``days_since_failure``; else derive from timestamps."""
    explicit = recovery_event.get("days_since_failure")
    if isinstance(explicit, (int, float)):
        return float(explicit)
    created = _parse_dt(recovery_event.get("created_at"))
    if created is None:
        return 0.0
    return max(0.0, (now_ist - _to_ist(created)).total_seconds() / 86400.0)


def _within_cooldown(executed_at: Any, now_ist: datetime) -> bool:
    dt = _parse_dt(executed_at)
    if dt is None:
        return False
    return (now_ist - _to_ist(dt)).total_seconds() < _NOTIF_COOLDOWN_SECONDS
