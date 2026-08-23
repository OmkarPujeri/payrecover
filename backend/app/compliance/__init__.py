"""Compliance layer — the deterministic, non-LLM rule engine.

``engine.check_compliance`` is pure Python: given a proposed action it returns
APPROVED / MODIFIED / BLOCKED with the citing rule. Enforcing compliance in code
(never a model) is PayRecover's core guarantee — zero hallucinated approvals.
"""
