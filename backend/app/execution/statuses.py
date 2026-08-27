"""Canonical status vocabulary for the execution layer.

Kept in its own dependency-free module so both the executor and the
circuit-breaker engine can import it without a cycle (the executor imports the
breakers; the breakers must not import the executor).

Action lifecycle
----------------
The decision phase persists one of four *forward-looking* statuses::

    approved        compliant + auto-cleared to execute
    pending_review  compliant but held for a human (HITL)
    escalated       low confidence / escalation tool — awaiting merchant
    blocked         the Compliance Engine refused it

The execution phase then advances an ``approved`` action through::

    executing   -> completed        (link created, notification sent, ...)
                -> scheduled        (retry registered / notification deferred)
                -> failed           (the API call raised)

and a circuit breaker or a merchant may terminate it as ``cancelled`` /
``skipped``. ``executed_at`` is stamped the moment real work happened.
"""
from __future__ import annotations

# ---- Action statuses: decision phase ------------------------------------- #
APPROVED = "approved"
PENDING_REVIEW = "pending_review"
ESCALATED = "escalated"
BLOCKED = "blocked"

# ---- Action statuses: execution phase ------------------------------------ #
EXECUTING = "executing"
SCHEDULED = "scheduled"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
SKIPPED = "skipped"

#: Nothing further will ever happen to an action in one of these states.
TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED, SKIPPED})

#: Statuses the executor will act on without an explicit ``force``.
EXECUTABLE_STATUSES = frozenset({APPROVED})

#: Statuses a circuit breaker may cancel — work that is queued but not in
#: flight. ``executing`` is deliberately excluded: we never cancel an action
#: mid-API-call, because we cannot know whether the remote side already acted.
CANCELLABLE_STATUSES = frozenset({APPROVED, PENDING_REVIEW, SCHEDULED, ESCALATED})

# ---- Event-level recovery_status ----------------------------------------- #
EV_PENDING = "pending"
EV_DIAGNOSED = "diagnosed"
EV_IN_PROGRESS = "in_progress"
EV_NEEDS_REVIEW = "needs_review"
EV_ESCALATED = "escalated"
EV_BLOCKED = "blocked"
EV_HALTED = "halted"
EV_SKIPPED = "skipped"
EV_RECOVERED = "recovered"
EV_UNRECOVERABLE = "unrecoverable"

#: Once an event reaches one of these, execution must not move it back.
EV_FINAL_STATUSES = frozenset({EV_RECOVERED, EV_UNRECOVERABLE, EV_HALTED})
