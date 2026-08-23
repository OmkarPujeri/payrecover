"""Strategy layer — LLM #2 (tool selection) and its deterministic mock brain.

``planner.plan`` maps a diagnosis onto exactly one bounded recovery tool and is
used both as the offline fallback and as ground-truth policy for tests.
"""
