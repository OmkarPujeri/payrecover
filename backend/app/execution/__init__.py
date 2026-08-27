"""Execution layer — turns *decisions* into *actions*.

The Strategy Agent + Compliance Engine + confidence gate decide what should
happen and persist that decision as a ``RecoveryAction``. This package is what
actually acts on it:

* :mod:`app.execution.statuses`         — the shared status vocabulary.
* :mod:`app.execution.circuit_breakers` — CB-001..008, the halt/limit engine.
* :mod:`app.execution.executor`         — the idempotent action executor.
* :mod:`app.execution.scheduler`        — APScheduler driver + due-action core.

Deciding and acting are deliberately decoupled (the industry pattern for dunning
systems): a decision is durable, and the executor is a separate, idempotent
service that can be driven by the pipeline, by a human approval, or by the
scheduler — without ever double-charging a customer.
"""
