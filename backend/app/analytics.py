"""Recovery economics — the money arithmetic, as pure functions.

Split out from ``api/dashboard.py`` deliberately. Everything here is plain
integer/float work over plain dicts, so it is unit-testable without a database,
an event loop, or FastAPI — the same reason ``diagnosis/classifier.py`` and
``compliance/engine.py`` are pure. The router does the SQL; this module decides
what the numbers *mean*.

Two rules the whole product leans on:

*   **Amounts are paise everywhere.** Rupee values exist only as a derived
    ``*_inr`` sibling for display, rounded to 2dp at the boundary.
*   **ROI is reported, never inferred by the client.** A zero-cost recovery is
    infinite return, not a divide-by-zero for the frontend to guess at, so this
    module emits both a machine value and the string to render.
"""
from __future__ import annotations

from typing import Any, Iterable

# The industry baseline for *manual* payment recovery. Merchants who chase failed
# payments by hand — a support agent with a spreadsheet — land around 10-18%; 12%
# is the midpoint and the figure the comparison view is honest about using, since
# we cannot measure a counterfactual on the same batch.
BASELINE_MANUAL_RECOVERY_RATE = 0.12

__all__ = [
    "BASELINE_MANUAL_RECOVERY_RATE",
    "roi",
    "prettify_reason",
    "build_economics",
    "build_comparison",
]


def _inr(paise: int | float | None) -> float:
    return round((paise or 0) / 100, 2)


def _pct(part: int | float | None, whole: int | float | None) -> float:
    if not whole:
        return 0.0
    return round((part or 0) / whole * 100, 1)


def roi(recovered_paise: int, cost_paise: int) -> tuple[float | None, str]:
    """Return-on-investment for a recovery channel, as ``(value, display)``.

    Four cases, and the distinction between the middle two matters:

    *   spent nothing, recovered something -> infinite return (``∞``)
    *   spent nothing, recovered nothing   -> nothing happened yet (``N/A``)
    *   spent money, recovered nothing     -> a real loss, and ``0x`` says so
        where ``N/A`` would quietly hide it
    *   otherwise                          -> floored ratio, e.g. ``181x``

    Floored rather than rounded so the number is never flattering: 181.9x is
    reported as ``181x``.
    """
    recovered = recovered_paise or 0
    cost = cost_paise or 0

    if cost == 0:
        return (None, "∞") if recovered > 0 else (None, "N/A")
    if recovered == 0:
        return 0.0, "0x"

    ratio = recovered / cost
    return round(ratio, 1), f"{int(ratio)}x"


def prettify_reason(reason: str | None) -> str:
    """``issuer_bank_down`` -> ``Issuer Bank Down``.

    Only a fallback. A diagnosed event carries a curated ``failure_label``
    ("Bank Downtime"); this is what an undiagnosed row gets so the table never
    shows a raw enum to a merchant.
    """
    if not reason:
        return "Unknown"
    return reason.replace("_", " ").title()


def _row(
    *,
    reason: str | None,
    label: str | None,
    category: str | None,
    count: int,
    failed_paise: int,
    recovered_count: int,
    recovered_paise: int,
    cost_paise: int,
) -> dict[str, Any]:
    value, display = roi(recovered_paise, cost_paise)
    return {
        "failure_reason": reason or "unknown",
        "failure_label": label or prettify_reason(reason),
        "failure_category": category,
        "count": count,
        "failed_paise": failed_paise,
        "failed_inr": _inr(failed_paise),
        "recovered_count": recovered_count,
        "recovered_paise": recovered_paise,
        "recovered_inr": _inr(recovered_paise),
        "cost_paise": cost_paise,
        "cost_inr": _inr(cost_paise),
        "recovery_rate_pct": _pct(recovered_paise, failed_paise),
        "roi": value,
        "roi_display": display,
    }


def build_economics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Per-failure-type ROI table plus a total row and the zero-cost callout.

    ``rows`` are raw aggregates straight off the ``GROUP BY``, one per Razorpay
    ``error_reason``. Grouping on ``error_reason`` rather than on the simulator's
    profile name is deliberate: a real ``payment.failed`` webhook carries an
    error reason and knows nothing about our profile names, so this table means
    the same thing for live traffic as it does for injected traffic.

    Ordered by rupees recovered, descending — the operator question this answers
    is "where is the money coming back from", not "what fails most often".
    """
    built = [
        _row(
            reason=r.get("error_reason"),
            label=r.get("failure_label"),
            category=r.get("failure_category"),
            count=int(r.get("count") or 0),
            failed_paise=int(r.get("failed_paise") or 0),
            recovered_count=int(r.get("recovered_count") or 0),
            recovered_paise=int(r.get("recovered_paise") or 0),
            cost_paise=int(r.get("cost_paise") or 0),
        )
        for r in rows
    ]
    built.sort(key=lambda r: (-r["recovered_paise"], -r["count"], r["failure_label"]))

    total = _row(
        reason="__total__",
        label="TOTAL",
        category=None,
        count=sum(r["count"] for r in built),
        failed_paise=sum(r["failed_paise"] for r in built),
        recovered_count=sum(r["recovered_count"] for r in built),
        recovered_paise=sum(r["recovered_paise"] for r in built),
        cost_paise=sum(r["cost_paise"] for r in built),
    )

    # The headline insight, and it is a real one rather than a slogan: retries and
    # payment links cost nothing to attempt, so every rupee they bring back is
    # pure margin. Only SMS costs money. Computing it from the rows means the
    # callout cannot drift away from the table above it.
    silent = [r for r in built if r["cost_paise"] == 0 and r["recovered_paise"] > 0]
    silent_paise = sum(r["recovered_paise"] for r in silent)

    return {
        "rows": built,
        "total": total,
        "callout": {
            "zero_cost_channels": [r["failure_label"] for r in silent],
            "zero_cost_recovered_paise": silent_paise,
            "zero_cost_recovered_inr": _inr(silent_paise),
            "share_of_recovered_pct": _pct(silent_paise, total["recovered_paise"]),
        },
    }


def build_comparison(
    *,
    failed_paise: int,
    recovered_paise: int,
    total_events: int,
    recovered_count: int,
    avg_recovery_hours: float | None,
    baseline_rate: float = BASELINE_MANUAL_RECOVERY_RATE,
) -> dict[str, Any]:
    """Before/after against the manual-recovery baseline, on the same batch.

    The "without" column is modelled, not measured — we cannot replay the same
    failures without the agent — so it applies the industry manual rate to the
    identical failed amount and says so via ``basis``. Being explicit about that
    is the difference between a comparison and a claim.
    """
    failed_paise = failed_paise or 0
    recovered_paise = recovered_paise or 0

    baseline_recovered = int(round(failed_paise * baseline_rate))
    saved = recovered_paise - baseline_recovered

    return {
        "baseline_rate_pct": round(baseline_rate * 100, 1),
        "basis": (
            "The 'without' column applies the industry manual-recovery rate "
            f"({round(baseline_rate * 100, 1)}%) to the same failed amount. "
            "Modelled, not measured — the same batch cannot be replayed twice."
        ),
        "without": {
            "label": "WITHOUT PAYRECOVER",
            "failed_paise": failed_paise,
            "failed_inr": _inr(failed_paise),
            "recovered_paise": baseline_recovered,
            "recovered_inr": _inr(baseline_recovered),
            "recovery_rate_pct": round(baseline_rate * 100, 1),
            "lost_paise": failed_paise - baseline_recovered,
            "lost_inr": _inr(failed_paise - baseline_recovered),
            "avg_recovery_hours": None,
            "recovery_time_label": "Manual, or never",
            "has_audit_trail": False,
            "compliance_enforced": False,
        },
        "with": {
            "label": "WITH PAYRECOVER",
            "failed_paise": failed_paise,
            "failed_inr": _inr(failed_paise),
            "recovered_paise": recovered_paise,
            "recovered_inr": _inr(recovered_paise),
            "recovery_rate_pct": _pct(recovered_paise, failed_paise),
            "lost_paise": failed_paise - recovered_paise,
            "lost_inr": _inr(failed_paise - recovered_paise),
            "avg_recovery_hours": avg_recovery_hours,
            "recovery_time_label": (
                f"Avg {avg_recovery_hours} hours"
                if avg_recovery_hours is not None
                else "No recoveries yet"
            ),
            "has_audit_trail": True,
            "compliance_enforced": True,
        },
        "revenue_saved_paise": saved,
        "revenue_saved_inr": _inr(saved),
        # Uplift over the baseline, not a recovery rate: +279% means we recovered
        # 3.79x what manual chasing would have.
        "uplift_pct": _pct(saved, baseline_recovered) if baseline_recovered else 0.0,
        "total_events": total_events,
        "recovered_count": recovered_count,
    }
