"""Recovery-economics arithmetic — pure functions, no database, no event loop.

``app.analytics`` exists as a separate module precisely so the money maths can be
tested like this: called directly, with hand-written numbers whose answers were
worked out by hand. Every figure the demo puts on screen — ROI per channel, the
zero-cost callout, the uplift over manual recovery — comes out of these four
functions, and a wrong one is a wrong claim in front of judges.

The cases that matter are the degenerate ones. A denominator of zero appears in
this domain constantly (a channel that costs nothing, an empty database before
the first injection), and the difference between "infinite return", "nothing has
happened yet" and "we spent money and got nothing back" is the difference between
a useful table and a flattering one.
"""
from app.analytics import (
    BASELINE_MANUAL_RECOVERY_RATE,
    build_comparison,
    build_economics,
    prettify_reason,
    roi,
)

# One shared fixture, reused across the economics and comparison tests so the two
# views can be asserted to agree — the same disagreement that would be visible on
# the dashboard if either drifted.
#
#   Bank Downtime      5 events, Rs 5,000 failed, 4 recovered, Rs 4,000 back, free
#   Insufficient Funds 3 events, Rs 3,000 failed, 1 recovered, Rs 1,000 back, Rs 5.50
#   Card Expired       2 events, Rs 2,000 failed, 0 recovered, nothing back, Rs 11
#   User Cancelled     1 event,  Rs 1,000 failed, 0 recovered, nothing back, free
#
# Totals: 11 events, Rs 11,000 failed, 5 recovered, Rs 5,000 back, Rs 16.50 spent.
#
# That last row earns its place. It is free *and* idle, which is the only way to
# tell "recovered money at zero cost" apart from "cost nothing because nothing
# happened" — without it, a callout that listed every free channel regardless of
# whether it worked would pass every assertion here.
_ROWS = [
    {
        "error_reason": "insufficient_funds",
        "failure_label": None,  # undiagnosed -> falls back to a prettified reason
        "failure_category": "soft",
        "count": 3,
        "failed_paise": 300_000,
        "recovered_count": 1,
        "recovered_paise": 100_000,
        "cost_paise": 550,
    },
    {
        "error_reason": "issuer_bank_down",
        "failure_label": "Bank Downtime",
        "failure_category": "soft",
        "count": 5,
        "failed_paise": 500_000,
        "recovered_count": 4,
        "recovered_paise": 400_000,
        "cost_paise": 0,
    },
    {
        "error_reason": "card_expired",
        "failure_label": "Card Expired",
        "failure_category": "hard",
        "count": 2,
        "failed_paise": 200_000,
        "recovered_count": 0,
        "recovered_paise": 0,
        "cost_paise": 1_100,
    },
    {
        "error_reason": "payment_cancelled_by_user",
        "failure_label": "User Cancelled",
        "failure_category": "terminal",
        "count": 1,
        "failed_paise": 100_000,
        "recovered_count": 0,
        "recovered_paise": 0,
        "cost_paise": 0,
    },
]


# --------------------------------------------------------------------------- #
# ROI
# --------------------------------------------------------------------------- #
def test_roi_distinguishes_free_from_idle_from_wasted():
    """Three zero-ish cases that must not collapse into one another."""
    # Spent nothing, recovered something: a retry costs nothing to attempt.
    assert roi(400_000, 0) == (None, "∞")
    # Spent nothing, recovered nothing: no activity, not a result.
    assert roi(0, 0) == (None, "N/A")
    # Spent money, recovered nothing: a real loss. "N/A" here would hide it.
    assert roi(0, 1_100) == (0.0, "0x")


def test_roi_floors_so_the_number_is_never_flattering():
    """181.8x is reported as ``181x`` — rounding up would overstate the return."""
    value, display = roi(100_000, 550)
    assert value == 181.8
    assert display == "181x"


def test_roi_treats_none_as_zero():
    """Coalesced SUMs can still arrive as ``None``; that must not raise."""
    assert roi(None, None) == (None, "N/A")
    assert roi(None, 500) == (0.0, "0x")


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
def test_prettify_reason_never_shows_a_raw_enum():
    assert prettify_reason("issuer_bank_down") == "Issuer Bank Down"
    assert prettify_reason("card_expired") == "Card Expired"
    assert prettify_reason(None) == "Unknown"
    assert prettify_reason("") == "Unknown"


# --------------------------------------------------------------------------- #
# Economics table
# --------------------------------------------------------------------------- #
def test_economics_orders_by_money_recovered_not_by_frequency():
    """The operator question is "where is the money coming back from"."""
    rows = build_economics(_ROWS)["rows"]
    assert [r["failure_reason"] for r in rows] == [
        "issuer_bank_down",          # Rs 4,000 back
        "insufficient_funds",        # Rs 1,000 back
        "card_expired",              # nothing back, 2 events
        "payment_cancelled_by_user", # nothing back, 1 event -> count breaks the tie
    ]


def test_economics_row_maths():
    by_reason = {r["failure_reason"]: r for r in build_economics(_ROWS)["rows"]}

    bank = by_reason["issuer_bank_down"]
    assert bank["failure_label"] == "Bank Downtime"
    assert bank["failed_inr"] == 5_000.0
    assert bank["recovered_inr"] == 4_000.0
    assert bank["recovery_rate_pct"] == 80.0
    assert bank["roi_display"] == "∞"

    funds = by_reason["insufficient_funds"]
    # No curated label was stored, so the table shows a prettified reason.
    assert funds["failure_label"] == "Insufficient Funds"
    assert funds["recovery_rate_pct"] == 33.3
    assert funds["cost_inr"] == 5.5
    assert funds["roi_display"] == "181x"

    expired = by_reason["card_expired"]
    assert expired["recovery_rate_pct"] == 0.0
    assert expired["roi_display"] == "0x"


def test_economics_total_is_the_sum_of_its_rows():
    built = build_economics(_ROWS)
    total = built["total"]

    assert total["failure_label"] == "TOTAL"
    for key in ("count", "failed_paise", "recovered_count", "recovered_paise", "cost_paise"):
        assert total[key] == sum(r[key] for r in built["rows"]), key

    assert total["count"] == 11
    assert total["recovered_paise"] == 500_000
    assert total["cost_paise"] == 1_650
    assert total["recovery_rate_pct"] == 45.5
    # Rs 5,000 back for Rs 16.50 spent.
    assert total["roi_display"] == "303x"


def test_economics_callout_counts_only_channels_that_were_free_and_worked():
    built = build_economics(_ROWS)
    callout = built["callout"]

    # Card Expired and Insufficient Funds cost money, so neither is free.
    # User Cancelled was free but recovered nothing, so it is not a channel that
    # "brought money back at zero cost" — listing it would inflate the headline.
    assert callout["zero_cost_channels"] == ["Bank Downtime"]
    assert "User Cancelled" not in callout["zero_cost_channels"]

    assert callout["zero_cost_recovered_paise"] == 400_000
    assert callout["zero_cost_recovered_inr"] == 4_000.0
    # Rs 4,000 of the Rs 5,000 recovered came back at zero marginal cost.
    assert callout["share_of_recovered_pct"] == 80.0
    # And the share is of what was *recovered*, not of what failed.
    assert callout["share_of_recovered_pct"] != round(
        400_000 / built["total"]["failed_paise"] * 100, 1
    )


def test_economics_survives_an_empty_database():
    """The dashboard renders this before the first injection; it must not divide by zero."""
    built = build_economics([])

    assert built["rows"] == []
    assert built["total"]["count"] == 0
    assert built["total"]["recovery_rate_pct"] == 0.0
    assert built["total"]["roi_display"] == "N/A"
    assert built["callout"]["zero_cost_channels"] == []
    assert built["callout"]["share_of_recovered_pct"] == 0.0


# --------------------------------------------------------------------------- #
# Before/after comparison
# --------------------------------------------------------------------------- #
def test_comparison_applies_the_manual_baseline_to_the_same_failed_amount():
    """Rs 10,000 failed: manual recovery models Rs 1,200; we got Rs 5,000."""
    assert BASELINE_MANUAL_RECOVERY_RATE == 0.12

    c = build_comparison(
        failed_paise=1_000_000,
        recovered_paise=500_000,
        total_events=10,
        recovered_count=5,
        avg_recovery_hours=4.5,
    )

    assert c["baseline_rate_pct"] == 12.0
    assert c["without"]["recovered_paise"] == 120_000
    assert c["without"]["recovery_rate_pct"] == 12.0
    assert c["without"]["lost_paise"] == 880_000

    assert c["with"]["recovered_paise"] == 500_000
    assert c["with"]["recovery_rate_pct"] == 50.0
    assert c["with"]["lost_paise"] == 500_000

    # Rs 5,000 - Rs 1,200 = Rs 3,800 that would not otherwise have come back.
    assert c["revenue_saved_paise"] == 380_000
    assert c["revenue_saved_inr"] == 3_800.0
    # Uplift over the baseline, not a recovery rate: 3,800 / 1,200.
    assert c["uplift_pct"] == 316.7


def test_comparison_agrees_with_the_economics_table_on_the_same_batch():
    """Both views read the same two totals, so they can never legitimately differ."""
    total = build_economics(_ROWS)["total"]
    c = build_comparison(
        failed_paise=total["failed_paise"],
        recovered_paise=total["recovered_paise"],
        total_events=total["count"],
        recovered_count=total["recovered_count"],
        avg_recovery_hours=None,
    )

    assert c["with"]["failed_paise"] == total["failed_paise"]
    assert c["with"]["recovered_paise"] == total["recovered_paise"]
    assert c["with"]["recovery_rate_pct"] == total["recovery_rate_pct"]
    assert c["total_events"] == total["count"]
    assert c["recovered_count"] == total["recovered_count"]


def test_comparison_is_honest_when_nothing_has_recovered_yet():
    """Recovering nothing while the baseline models Rs 1,200 is a negative saving."""
    c = build_comparison(
        failed_paise=1_000_000,
        recovered_paise=0,
        total_events=10,
        recovered_count=0,
        avg_recovery_hours=None,
    )

    assert c["with"]["recovery_rate_pct"] == 0.0
    assert c["revenue_saved_paise"] == -120_000
    assert c["uplift_pct"] == -100.0
    assert c["with"]["recovery_time_label"] == "No recoveries yet"
    assert c["with"]["avg_recovery_hours"] is None


def test_comparison_survives_an_empty_database():
    c = build_comparison(
        failed_paise=0,
        recovered_paise=0,
        total_events=0,
        recovered_count=0,
        avg_recovery_hours=None,
    )

    assert c["without"]["recovered_paise"] == 0
    assert c["with"]["recovery_rate_pct"] == 0.0
    assert c["revenue_saved_paise"] == 0
    # No baseline to divide by; 0% rather than an exception or a null.
    assert c["uplift_pct"] == 0.0


def test_comparison_labels_the_without_column_as_modelled():
    """The claim is only defensible if the response says it is an assumption."""
    c = build_comparison(
        failed_paise=1_000_000,
        recovered_paise=500_000,
        total_events=10,
        recovered_count=5,
        avg_recovery_hours=2.0,
    )

    assert "12.0%" in c["basis"]
    assert "Modelled, not measured" in c["basis"]
    # And the qualitative columns the merchant actually cares about.
    assert c["without"]["has_audit_trail"] is False
    assert c["without"]["compliance_enforced"] is False
    assert c["with"]["has_audit_trail"] is True
    assert c["with"]["compliance_enforced"] is True
    assert c["with"]["recovery_time_label"] == "Avg 2.0 hours"


def test_comparison_baseline_rate_is_injectable():
    """The 12% is a documented assumption, not a constant welded into the maths."""
    c = build_comparison(
        failed_paise=1_000_000,
        recovered_paise=500_000,
        total_events=10,
        recovered_count=5,
        avg_recovery_hours=None,
        baseline_rate=0.20,
    )

    assert c["baseline_rate_pct"] == 20.0
    assert c["without"]["recovered_paise"] == 200_000
    assert c["revenue_saved_paise"] == 300_000
    assert "20.0%" in c["basis"]
