"""Phase 5a — the demo slice: economics, the manual-recovery comparison, and the
chaos presets that drive the dashboard's left pane.

These are the four endpoints the command centre needs and the backend did not have.
Everything here is composition over primitives that were already tested in earlier
phases, so what these tests guard is the *composition*: that a preset injects what
it claims to inject, that a circuit step reaches only the events that preset
created, that the money on the economics table equals the money on the metrics
panel, and that a receipt reflects the state of the row at the end of the run
rather than at the moment it was injected.

The arithmetic itself lives in ``tests/test_analytics.py``, called directly with
hand-computed numbers. Here we only check the wiring.

Routing assertions use the two provably deterministic profiles documented in
``tests/test_execution.py``: ``bank_downtime`` under Rs 10,000 always auto-approves
a retry, and anything over Rs 10,000 is always pinned to human review. Nothing
here asserts an exact count of *fired* scheduled actions — a retry's scheduled
time depends on the hour the test runs, and a test that only passes in the
afternoon is not a test.
"""
from datetime import datetime, timedelta, timezone

from app.chaos import CHAOS_PRESETS
from app.models import RecoveryEvent

# Rs 2,500 bank downtime: always approved, always a retry.
_AUTO = {"failure_type": "bank_downtime", "amount": 250_000, "method": "card"}
# Above the Rs 10,000 ceiling: always human-gated, whatever the model thinks.
_HITL_CEILING_PAISE = 1_000_000


# --------------------------------------------------------------------------- #
# GET /api/dashboard/economics
# --------------------------------------------------------------------------- #
async def test_economics_on_an_empty_database(client):
    """The dashboard renders this before anything has been injected."""
    body = (await client.get("/api/dashboard/economics")).json()

    assert body["rows"] == []
    assert body["total"]["count"] == 0
    assert body["total"]["roi_display"] == "N/A"
    assert body["callout"]["zero_cost_channels"] == []
    assert body["callout"]["share_of_recovered_pct"] == 0.0


async def test_economics_groups_by_error_reason(client):
    await client.post("/api/simulator/inject", json={**_AUTO, "count": 2})
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "card_expired", "amount": 99_900, "diagnose": False},
    )

    body = (await client.get("/api/dashboard/economics")).json()
    by_reason = {r["failure_reason"]: r for r in body["rows"]}

    assert set(by_reason) == {"issuer_bank_down", "card_expired"}
    assert by_reason["issuer_bank_down"]["count"] == 2
    assert by_reason["issuer_bank_down"]["failed_paise"] == 500_000
    assert by_reason["card_expired"]["count"] == 1

    # The diagnosed rows carry the curated label the Diagnostic Agent assigned.
    assert by_reason["issuer_bank_down"]["failure_label"]
    assert by_reason["issuer_bank_down"]["failure_category"] == "soft"
    # The undiagnosed one has no label to show, so the reason is prettified —
    # never a raw enum in front of a merchant.
    assert by_reason["card_expired"]["failure_label"] == "Card Expired"


async def test_economics_total_agrees_with_the_metrics_panel(client):
    """Two views of the same money, side by side on screen. They must match."""
    await client.post("/api/simulator/inject", json={**_AUTO, "count": 3})
    await client.post(
        "/api/simulator/inject",
        json={"failure_type": "card_expired", "amount": 120_000, "count": 2},
    )

    economics = (await client.get("/api/dashboard/economics")).json()
    metrics = (await client.get("/api/dashboard/metrics")).json()

    assert economics["total"]["count"] == metrics["total_events"] == 5
    assert economics["total"]["failed_paise"] == metrics["failed_amount_paise"]
    assert economics["total"]["recovered_paise"] == metrics["recovered_amount_paise"]
    assert economics["total"]["cost_paise"] == metrics["recovery_cost_paise"]
    assert sum(r["count"] for r in economics["rows"]) == economics["total"]["count"]


# --------------------------------------------------------------------------- #
# GET /api/dashboard/metrics/comparison
# --------------------------------------------------------------------------- #
async def test_comparison_models_the_baseline_on_the_real_failed_amount(client):
    await client.post("/api/simulator/inject", json={**_AUTO, "count": 4})

    body = (await client.get("/api/dashboard/metrics/comparison")).json()
    metrics = (await client.get("/api/dashboard/metrics")).json()

    assert body["baseline_rate_pct"] == 12.0
    # Both columns describe the same batch of failures.
    assert body["with"]["failed_paise"] == body["without"]["failed_paise"]
    assert body["with"]["failed_paise"] == metrics["failed_amount_paise"] == 1_000_000
    assert body["without"]["recovered_paise"] == 120_000
    assert body["total_events"] == metrics["total_events"] == 4

    # And it admits the "without" column is an assumption.
    assert "Modelled, not measured" in body["basis"]
    assert body["without"]["has_audit_trail"] is False
    assert body["with"]["has_audit_trail"] is True


async def test_comparison_reports_a_negative_saving_before_anything_recovers(client):
    """Nothing recovered yet means we are *behind* the modelled baseline. Say so."""
    await client.post("/api/simulator/inject", json={**_AUTO, "count": 2})

    body = (await client.get("/api/dashboard/metrics/comparison")).json()

    assert body["with"]["recovered_paise"] == 0
    assert body["with"]["recovery_rate_pct"] == 0.0
    assert body["revenue_saved_paise"] == -60_000  # Rs 5,000 failed -> Rs 600 baseline
    assert body["with"]["avg_recovery_hours"] is None
    assert body["with"]["recovery_time_label"] == "No recoveries yet"


async def test_avg_recovery_hours_measures_the_real_span(client, session):
    """Seeded timestamps, because a 5.5-hour skew is invisible in a live-clock test.

    Two recoveries, three and five hours wide, both written as aware UTC. SQLite
    hands them back naive and Postgres hands them back aware; ``to_utc`` is what
    makes the subtraction mean the same thing on both. If either end of the span
    were assumed to be IST while the other was UTC, this would read 4.0 as 9.5 or
    -1.5 rather than failing loudly — which is exactly why the span is asserted
    instead of merely "not None".
    """
    base = datetime(2026, 3, 5, 6, 0, tzinfo=timezone.utc)
    for i, hours in enumerate((3, 5)):
        session.add(
            RecoveryEvent(
                razorpay_payment_id=f"pay_seed_{i}",
                razorpay_order_id=f"order_seed_{i}",
                event_type="payment.failed",
                amount=400_000,
                error_reason="issuer_bank_down",
                failure_label="Bank Downtime",
                recovery_status="recovered",
                recovered_amount=400_000,
                created_at=base,
                recovered_at=base + timedelta(hours=hours),
            )
        )
    await session.commit()

    metrics = (await client.get("/api/dashboard/metrics")).json()
    assert metrics["recovered_count"] == 2
    assert metrics["avg_recovery_hours"] == 4.0

    comparison = (await client.get("/api/dashboard/metrics/comparison")).json()
    assert comparison["with"]["avg_recovery_hours"] == 4.0
    assert comparison["with"]["recovery_time_label"] == "Avg 4.0 hours"
    # Rs 8,000 failed and fully recovered vs a modelled Rs 960.
    assert comparison["revenue_saved_paise"] == 800_000 - 96_000


# --------------------------------------------------------------------------- #
# GET /api/simulator/presets
# --------------------------------------------------------------------------- #
async def test_presets_drive_the_button_row_from_the_server(client):
    body = (await client.get("/api/simulator/presets")).json()
    presets = {p["preset"]: p for p in body["presets"]}

    assert set(presets) == set(CHAOS_PRESETS)
    for key, p in presets.items():
        assert p["name"] and p["description"] and p["narrative"], key
        # Step scripts stay server-side; a client that could see them could
        # reimplement a preset and drift from what the demo actually runs.
        assert "steps" not in p, key

    assert presets["hdfc_bank_crash"]["available"] is True
    assert presets["hdfc_bank_crash"]["event_count"] == 5
    assert presets["salary_day_batch"]["event_count"] == 20

    # An unavailable preset renders disabled, with its own reason.
    assert presets["cascade_failure"]["available"] is False
    assert presets["cascade_failure"]["unavailable_reason"]
    assert presets["cascade_failure"]["event_count"] == 0


# --------------------------------------------------------------------------- #
# POST /api/simulator/chaos/{preset}
# --------------------------------------------------------------------------- #
async def test_chaos_rejects_an_unknown_preset(client):
    r = await client.post("/api/simulator/chaos/not_a_preset")
    assert r.status_code == 404
    assert "not_a_preset" in r.json()["detail"]


async def test_chaos_refuses_an_unavailable_preset_loudly(client):
    """A 200 with an empty run would read as "the scenario found nothing to do"."""
    r = await client.post("/api/simulator/chaos/cascade_failure")

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["preset"] == "cascade_failure"
    assert detail["reason"]
    # And it must not have injected anything on the way out.
    assert (await client.get("/api/dashboard/metrics")).json()["total_events"] == 0


async def test_chaos_hdfc_bank_crash(client):
    """Inject -> fast-forward -> three customers pay -> CB-001 halts the rest."""
    r = await client.post(
        "/api/simulator/chaos/hdfc_bank_crash", json={"max_events": 2}
    )
    assert r.status_code == 200
    body = r.json()

    assert body["preset"] == "hdfc_bank_crash"
    assert body["narrative"]
    assert body["injected"] == 2

    for ev in body["events"]:
        assert ev["failure_type"] == "bank_downtime"
        assert ev["amount"] == 250_000
        # Deterministic for every synthetic history at this amount.
        assert ev["tool"] == "schedule_smart_retry"
        assert ev["action_status"] == "approved"
        assert ev["requires_human"] is False
        assert ev["gate_action"] in ("auto_execute", "auto_execute_flagged")

    # The step receipts, in order, describing what actually ran.
    assert [s["op"] for s in body["steps"]] == ["inject", "fast_forward", "circuit"]
    assert body["steps"][0]["count"] == 2
    assert body["steps"][1]["hours"] == 12
    # How many retries came due depends on the hour the test runs; that they were
    # *considered* does not.
    assert body["steps"][1]["processed"] >= 0
    assert body["actions_processed"] == body["steps"][1]["processed"]

    # The preset asks for the first 3 captures but only 2 events exist here.
    assert body["steps"][2]["event_type"] == "payment.captured"
    assert body["steps"][2]["events"] == 2
    assert len(body["breakers_tripped"]) == 2
    assert {t["breaker_id"] for t in body["breakers_tripped"]} == {"CB-001"}

    # The receipt reflects the end of the run, not the moment of injection: these
    # rows were "pending" when they were summarised into the events list.
    for ev in body["events"]:
        assert ev["recovery_status"] == "recovered"
        assert ev["recovered_amount"] == 250_000

    assert body["metrics"]["recovered_count"] == 2
    assert body["metrics"]["recovered_amount_paise"] == 500_000
    # And the embedded metrics are the dashboard's own, not a second opinion.
    assert body["metrics"] == (await client.get("/api/dashboard/metrics")).json()


async def test_chaos_dispute_storm_halts_every_case_it_touches(client):
    r = await client.post("/api/simulator/chaos/dispute_storm", json={"max_events": 2})
    body = r.json()

    assert body["injected"] == 2
    assert [s["op"] for s in body["steps"]] == ["inject", "circuit"]
    assert body["steps"][1]["target"] == "all"
    assert body["steps"][1]["events"] == 2

    assert len(body["breakers_tripped"]) == 2
    for trip in body["breakers_tripped"]:
        assert trip["breaker_id"] == "CB-002"
        assert trip["trigger_type"] == "dispute_created"
        assert trip["order_id"]

    # CB-002 halts rather than recovers — chasing a disputed payment is how a
    # merchant loses the representment.
    for ev in body["events"]:
        assert ev["recovery_status"] == "halted"
    assert body["metrics"]["recovered_count"] == 0


async def test_chaos_upi_timeout_wave_schedules_without_firing(client):
    """The point of this preset is a full schedule, not a full feed."""
    r = await client.post(
        "/api/simulator/chaos/upi_timeout_wave", json={"max_events": 2}
    )
    body = r.json()

    assert body["injected"] == 2
    # No fast-forward step, so nothing was due and nothing fired.
    assert [s["op"] for s in body["steps"]] == ["inject"]
    assert body["steps"][0]["method"] == "upi"
    assert body["actions_processed"] == 0
    assert body["actions_fired"] == 0
    assert body["breakers_tripped"] == []

    events = (await client.get("/api/dashboard/events")).json()["events"]
    assert len(events) == 2
    for ev in events:
        assert ev["payment_method"] == "upi"
        assert ev["error_reason"] == "network_timeout"


async def test_chaos_salary_day_batch_splits_the_gate(client):
    """Two inject steps at different values, and the cap preserves the mix."""
    r = await client.post(
        "/api/simulator/chaos/salary_day_batch", json={"max_events": 2}
    )
    body = r.json()

    # Capped per step, not per run: 2 mixed-value + 2 high-value, not 2 total.
    assert body["injected"] == 4
    assert [s["op"] for s in body["steps"]] == ["inject", "inject", "fast_forward"]
    assert body["steps"][0]["count"] == 2
    assert body["steps"][0]["amount"] is None
    assert body["steps"][1]["count"] == 2
    assert body["steps"][1]["amount"] == 2_500_000

    # The invariant that must never regress: every order above the ceiling is
    # pinned to a human, however confident the agent was. Asserted on whatever
    # cleared the ceiling, since the mixed-value step draws random amounts and
    # some of those legitimately land above Rs 10,000 too.
    over_ceiling = [e for e in body["events"] if e["amount"] > _HITL_CEILING_PAISE]
    assert len(over_ceiling) >= 2
    for ev in over_ceiling:
        assert ev["requires_human"] is True
        assert ev["gate_action"] == "hitl_review"
        assert ev["executed"] is False

    # The two pinned Rs 25,000 orders are the deterministic pair — same failure
    # type and amount every run — so the held-action status is asserted on those.
    pinned = [e for e in body["events"] if e["amount"] == 2_500_000]
    assert len(pinned) >= 2
    for ev in pinned:
        assert ev["action_status"] == "pending_review"

    assert body["metrics"]["total_events"] == 4


async def test_chaos_high_value_hitl_lands_in_the_queue(client):
    # Deliberately bodyless — this preset needs no cap, and a plain POST with no
    # body is how the dashboard's button row and a demo `curl` will call it. The
    # handler declares ``body: ChaosRequest | None = None``, so FastAPI treats the
    # body as optional and passes None. If this and the two tests above are the
    # only failures on a first run and all three are 422, that assumption is the
    # cause and ``json={}`` is the fix — not a bug in the preset.
    r = await client.post("/api/simulator/chaos/high_value_hitl")
    body = r.json()

    assert body["injected"] == 1
    ev = body["events"][0]
    assert ev["amount"] == 2_500_000
    assert ev["amount_inr"] == 25_000.0
    assert ev["tool"] == "generate_payment_link"
    assert ev["requires_human"] is True
    assert ev["executed"] is False

    # Held for a human means actually in the queue, not merely flagged.
    pending = (await client.get("/api/hitl/pending")).json()
    assert pending["total"] == 1
    assert pending["pending"][0]["proposed_action"] == "generate_payment_link"


async def test_chaos_circuit_step_touches_only_this_run(client):
    """A preset must never reach into rows it did not create."""
    bystander = await client.post("/api/simulator/inject", json={**_AUTO, "count": 1})
    bystander_id = bystander.json()["events"][0]["id"]

    await client.post("/api/simulator/chaos/dispute_storm", json={"max_events": 1})

    untouched = (await client.get(f"/api/dashboard/events/{bystander_id}")).json()
    assert untouched["has_dispute"] is False
    assert untouched["recovery_status"] != "halted"


# --------------------------------------------------------------------------- #
# POST /api/simulator/run-batch
# --------------------------------------------------------------------------- #
async def test_run_batch_returns_the_shape_of_the_outcome(client):
    r = await client.post("/api/simulator/run-batch", json={"count": 6})
    assert r.status_code == 200
    body = r.json()

    assert body["requested"] == body["injected"] == 6
    # The aggregation is the whole reason this exists next to /inject: 6 events of
    # per-event JSON is already unreadable, and 100 is the actual demo size.
    assert "events" not in body
    for key in ("by_failure_type", "by_gate", "by_status", "by_tool"):
        assert sum(body[key].values()) == 6, key

    assert body["metrics"]["total_events"] == 6
    assert body["economics"]["total"]["count"] == 6
    assert body["economics"]["total"]["failed_paise"] == body["metrics"]["failed_amount_paise"]
    # Whatever mix came up, the failure types are real profiles.
    reasons = {r["failure_reason"] for r in body["economics"]["rows"]}
    assert reasons and all(r != "unknown" for r in reasons)


async def test_run_batch_counts_human_gated_orders(client):
    body = (await client.post("/api/simulator/run-batch", json={"count": 8})).json()

    # ``requires_human`` is a count of events, so it cannot exceed the batch, and
    # it must equal what the gate breakdown says about human-routed tiers.
    assert 0 <= body["requires_human"] <= 8
    assert body["requires_human"] == body["by_gate"].get("hitl_review", 0) + body[
        "by_gate"
    ].get("escalate", 0)


async def test_run_batch_refuses_an_absurd_count(client):
    """The cap is the same 200 the injector enforces — one shared ceiling."""
    assert (await client.post("/api/simulator/run-batch", json={"count": 500})).status_code == 422
    assert (await client.post("/api/simulator/run-batch", json={"count": 0})).status_code == 422
