/**
 * Wire + client types, derived from the backend source that produces them, not
 * from assumption. Where a concept is serialized in more than one shape across
 * endpoints (compliance blocks, action rows), the *raw* shapes live here and are
 * collapsed to one client type in `adapters.ts`. Components import the client
 * types only.
 *
 * Provenance is noted per block so a future edit can re-check against the exact
 * function that emits it.
 */

// --------------------------------------------------------------------------- //
// Metrics - GET /api/dashboard/metrics (dashboard.get_metrics)
// --------------------------------------------------------------------------- //
export interface Metrics {
  total_events: number;
  failed_amount_paise: number;
  failed_amount_inr: number;
  recovered_amount_paise: number;
  recovered_amount_inr: number;
  recovered_count: number;
  recovery_cost_paise: number;
  recovery_cost_inr: number;
  recovery_rate_by_amount_pct: number;
  recovery_rate_by_count_pct: number;
  avg_recovery_hours: number | null;
  status_breakdown: Record<string, number>;
  failure_breakdown: Record<string, number>;
}

// --------------------------------------------------------------------------- //
// Economics - GET /api/dashboard/economics (analytics.build_economics)
// --------------------------------------------------------------------------- //
export interface EconomicsRow {
  failure_reason: string;
  failure_label: string;
  failure_category: string | null;
  count: number;
  failed_paise: number;
  failed_inr: number;
  recovered_count: number;
  recovered_paise: number;
  recovered_inr: number;
  cost_paise: number;
  cost_inr: number;
  recovery_rate_pct: number;
  roi: number | null; // null == not a finite ratio; read roi_display
  roi_display: string; // "∞" | "N/A" | "0x" | "181x"
}

export interface Economics {
  rows: EconomicsRow[];
  total: EconomicsRow;
  callout: {
    zero_cost_channels: string[];
    zero_cost_recovered_paise: number;
    zero_cost_recovered_inr: number;
    share_of_recovered_pct: number;
  };
}

// --------------------------------------------------------------------------- //
// Comparison - GET /api/dashboard/metrics/comparison (analytics.build_comparison)
// --------------------------------------------------------------------------- //
export interface ComparisonColumn {
  label: string;
  failed_paise: number;
  failed_inr: number;
  recovered_paise: number;
  recovered_inr: number;
  recovery_rate_pct: number;
  lost_paise: number;
  lost_inr: number;
  avg_recovery_hours: number | null;
  recovery_time_label: string;
  has_audit_trail: boolean;
  compliance_enforced: boolean;
}

export interface Comparison {
  baseline_rate_pct: number;
  basis: string;
  without: ComparisonColumn;
  with: ComparisonColumn;
  revenue_saved_paise: number;
  revenue_saved_inr: number;
  uplift_pct: number;
  total_events: number;
  recovered_count: number;
}

// --------------------------------------------------------------------------- //
// Recovery event - ingest.event_to_dict
// Used by the events list, the SSE frames, and (nested) event detail.
// --------------------------------------------------------------------------- //
export interface RecoveryEvent {
  id: string;
  razorpay_payment_id: string;
  razorpay_order_id: string;
  event_type: string;
  amount: number;
  amount_inr: number;
  currency: string;
  error_code: string | null;
  error_source: string | null;
  error_step: string | null;
  error_reason: string | null;
  error_description: string | null;
  customer_name: string | null;
  customer_email: string | null;
  customer_contact: string | null;
  payment_method: string | null;
  customer_dnd: boolean;
  failure_category: string | null;
  failure_label: string | null;
  recoverability_score: number | null;
  recovery_status: string;
  recovery_attempts: number;
  recovered_amount: number;
  recovered_amount_inr: number;
  recovery_cost_paise: number;
  has_dispute: boolean;
  customer_opted_out: boolean;
  subscription_cancelled: boolean;
  is_simulated: boolean;
  cascade_group_id: string | null;
  recovered_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  // Simulator rides this along on failure_detected only.
  failure_type?: string;
}

export interface EventsPage {
  total: number;
  limit: number;
  skip: number;
  events: RecoveryEvent[];
}

// --------------------------------------------------------------------------- //
// Compliance - appears in FOUR shapes across the API (trap #3). Raw union here;
// adapters.normalizeCompliance collapses it.
// --------------------------------------------------------------------------- //
/** Nested shape: hitl `_hitl_request`, actions `_action_dict`, SSE strategy/gate. */
export interface ComplianceNested {
  decision: string | null;
  rule_id: string | null;
  rule_name: string | null;
  reason: string | null;
  modification?: Record<string, unknown> | null;
}

/** Flat shape: dashboard `_action_to_dict`, audit `_audit_row`. */
export interface ComplianceFlat {
  compliance_decision: string | null;
  compliance_rule_id: string | null;
  compliance_rule_name: string | null;
  compliance_reason: string | null;
}

/** Normalized client compliance. */
export interface Compliance {
  decision: string | null; // APPROVED | MODIFIED | BLOCKED | null
  ruleId: string | null;
  ruleName: string | null;
  reason: string | null;
  modification?: Record<string, unknown> | null;
}

// --------------------------------------------------------------------------- //
// Gate - confidence.GateDecision serialized (strategy_agent._gate_dict)
// --------------------------------------------------------------------------- //
export interface Gate {
  action: string; // auto_execute | auto_execute_flagged | hitl_review | escalate | blocked
  requires_human: boolean;
  tier: string; // high | moderate | low | very_low | blocked
  confidence: number | null;
  reason: string;
}

// --------------------------------------------------------------------------- //
// Action - THREE raw shapes (trap #3). Raw types below; adapters.normalizeAction
// collapses each to ClientAction.
// --------------------------------------------------------------------------- //
/** Dashboard event-detail action (`_action_to_dict`) - flat compliance. */
export interface DashboardActionDTO extends ComplianceFlat {
  id: string;
  agent_name: string;
  action_type: string;
  action_params: Record<string, unknown> | null;
  agent_reasoning: string | null;
  confidence_score: number | null;
  risk_factors: string[] | null;
  uncertainty_factors: string[] | null;
  status: string;
  result: Record<string, unknown> | null;
  cost_paise: number | null;
  scheduled_at: string | null;
  executed_at: string | null;
  created_at: string | null;
}

/** Audit row (`_audit_row`) - flat compliance, `timestamp` not `created_at`. */
export interface AuditRowDTO extends ComplianceFlat {
  id: string;
  timestamp: string | null;
  recovery_event_id: string;
  razorpay_order_id: string | null;
  razorpay_payment_id: string | null;
  amount_paise: number | null;
  amount_inr: number | null;
  failure_category: string | null;
  failure_label: string | null;
  recovery_status: string | null;
  agent_name: string;
  action_type: string;
  action_params: Record<string, unknown> | null;
  agent_reasoning: string | null;
  confidence_score: number | null;
  risk_factors: string[] | null;
  uncertainty_factors: string[] | null;
  status: string;
  cost_paise: number | null;
  scheduled_at: string | null;
  executed_at: string | null;
  source: string | null;
  gate: Gate | null;
  result: Record<string, unknown> | null;
}

export interface AuditPage {
  total: number;
  limit: number;
  skip: number;
  entries: AuditRowDTO[];
}

/**
 * Normalized client action - the ONE shape components render, collapsed from all
 * three raw action DTOs. The `ctx*` fields are populated only from audit rows
 * (which carry event context inline); they're undefined for event-detail actions,
 * where the parent event already supplies that context.
 */
export interface ClientAction {
  id: string;
  eventId: string | null;
  agent: string;
  actionType: string;
  params: Record<string, unknown> | null;
  reasoning: string | null;
  confidence: number | null;
  riskFactors: string[];
  uncertaintyFactors: string[];
  compliance: Compliance;
  gate: Gate | null;
  status: string;
  costPaise: number | null;
  scheduledAt: string | null;
  executedAt: string | null;
  createdAt: string | null;
  result: Record<string, unknown> | null;
  // Audit-row context (undefined for event-detail actions).
  source?: string | null;
  ctxOrderId?: string | null;
  ctxPaymentId?: string | null;
  ctxAmountPaise?: number | null;
  ctxFailureLabel?: string | null;
  ctxFailureCategory?: string | null;
  ctxRecoveryStatus?: string | null;
}

// --------------------------------------------------------------------------- //
// Circuit-breaker event - dashboard._cb_to_dict (nested in event detail)
// --------------------------------------------------------------------------- //
export interface CircuitBreakerEventDTO {
  id: string;
  trigger_type: string;
  trigger_id: string; // this is the CB-00x id
  trigger_details: Record<string, unknown> | null;
  cancelled_actions: number;
  created_at: string | null;
}

/** GET /api/dashboard/events/{id} - event_to_dict + actions + breakers. */
export interface EventDetailDTO extends RecoveryEvent {
  actions: DashboardActionDTO[];
  circuit_breaker_events: CircuitBreakerEventDTO[];
}

// --------------------------------------------------------------------------- //
// HITL - GET /api/hitl/pending (hitl_routes._hitl_request)
// Note: `confidence` (not confidence_score), `proposed_action`, `amount_paise`.
// --------------------------------------------------------------------------- //
export interface HITLRequestDTO {
  action_id: string;
  recovery_event_id: string;
  order_id: string;
  payment_id: string;
  amount_paise: number;
  amount_inr: number;
  failure_label: string | null;
  failure_category: string | null;
  diagnostic_summary: string | null;
  recoverability_score: number | null;
  proposed_action: string;
  proposed_params: Record<string, unknown> | null;
  confidence: number | null;
  reasoning: string | null;
  risk_factors: string[];
  uncertainty_factors: string[];
  compliance: ComplianceNested;
  gate: Gate | null;
  status: string;
  customer_name: string | null;
  customer_email: string | null;
  customer_contact: string | null;
  created_at: string | null;
}

export interface HITLPending {
  total: number;
  pending: HITLRequestDTO[];
}

/** Response of approve/modify/skip - the 200 does NOT mean it executed. */
export interface HITLResolution {
  status: string; // approved | modified | skipped
  action_id: string;
  params?: Record<string, unknown>;
  compliance?: ComplianceNested;
  execution?: ExecutionResult | null;
  reason?: string | null;
}

/** executor.execute_action / execute_action_by_id return shape. */
export interface ExecutionResult {
  executed: boolean;
  action_id: string;
  event_id?: string;
  action_type: string;
  status: string;
  result?: Record<string, unknown>;
  reason?: string; // deferred | error | already_final | ...
  detail?: string;
  scheduled_at?: string | null;
  executed_at?: string;
  razorpay_mode?: string;
}

// --------------------------------------------------------------------------- //
// Scheduled actions - GET /api/actions/scheduled (action_routes.list_scheduled)
// The compliance band's tick source.
// --------------------------------------------------------------------------- //
export interface ScheduledAction {
  action_id: string;
  recovery_event_id: string;
  action_type: string;
  scheduled_at: string | null;
  deferred_to: string | null;
  deferred_reason: string | null;
  retry_order_id: string | null;
  attempt: number | null;
}

export interface ScheduledPage {
  total: number;
  scheduled: ScheduledAction[];
}

// --------------------------------------------------------------------------- //
// Breaker log - GET /api/audit/breakers (audit_routes.breaker_log)
// --------------------------------------------------------------------------- //
export interface BreakerLogRow {
  id: string;
  recovery_event_id: string;
  trigger_id: string; // CB-00x
  trigger_type: string;
  trigger_details: Record<string, unknown> | null;
  cancelled_actions: number;
  created_at: string | null;
}

export interface BreakerPage {
  total: number;
  breakers: BreakerLogRow[];
}

// --------------------------------------------------------------------------- //
// Simulator - presets, chaos run, run-batch
// --------------------------------------------------------------------------- //
export interface PresetSummary {
  preset: string;
  name: string;
  description: string;
  narrative: string;
  available: boolean;
  unavailable_reason: string | null;
  event_count: number;
}

export interface PresetList {
  presets: PresetSummary[];
}

/** Per-event receipt from a chaos run (simulator_routes._summarise_event). */
export interface ChaosEventSummary {
  id: string;
  order_id: string;
  amount: number;
  amount_inr: number;
  failure_type: string | null;
  failure_label: string | null;
  recovery_status: string;
  recovered_amount: number;
  tool: string | null;
  confidence: number | null;
  gate_action: string | null;
  requires_human: boolean | null;
  action_status: string | null;
  executed: boolean;
}

export interface ChaosStep {
  op: string; // inject | fast_forward | circuit
  [k: string]: unknown;
}

export interface ChaosBreakerTrip {
  event_id: string;
  order_id: string;
  breaker_id: string;
  breaker: string;
  trigger_type: string;
  reason: string;
}

export interface ChaosRun {
  status: string;
  preset: string;
  name: string;
  description: string;
  narrative: string;
  steps: ChaosStep[];
  injected: number;
  events: ChaosEventSummary[];
  actions_processed: number;
  actions_fired: number;
  breakers_tripped: ChaosBreakerTrip[];
  metrics: Metrics;
}

export interface RunBatch {
  status: string;
  requested: number;
  injected: number;
  requires_human: number;
  by_failure_type: Record<string, number>;
  by_gate: Record<string, number>;
  by_status: Record<string, number>;
  by_tool: Record<string, number>;
  metrics: Metrics;
  economics: Economics;
}

// --------------------------------------------------------------------------- //
// SSE frames - every `type` observed in app/, broadcast adds `ts` (except the
// bare `connected` frame, which is sent without going through broadcast).
// See adapters.ts for the action_executed `action` → `action_type` fix (trap #2).
// --------------------------------------------------------------------------- //
export type SSEFrame =
  | { type: "connected"; ts?: string }
  | { type: "failure_detected"; ts: string; event: RecoveryEvent }
  | { type: "failure_duplicate"; ts: string; event: RecoveryEvent }
  | { type: "event_diagnosed"; ts: string; event: RecoveryEvent; diagnosis: Record<string, unknown> }
  | {
      type: "strategy_selected";
      ts: string;
      event_id: string;
      strategy: {
        tool: string;
        reason: string;
        confidence: number | null;
        source: string;
        risk_factors: string[];
        uncertainty_factors: string[];
      };
    }
  | { type: "compliance_checked"; ts: string; event_id: string; compliance: ComplianceNested }
  | {
      type: "gate_decided";
      ts: string;
      event: RecoveryEvent;
      gate: Gate;
      status: string;
      scheduled_at: string | null;
      action_id: string;
    }
  | {
      type: "action_executed";
      ts: string;
      event_id: string;
      action_id: string;
      action: string; // TRAP #2: tool name lives under `action` here, not action_type
      status: string;
      result: Record<string, unknown>;
      razorpay_mode: string;
      event: RecoveryEvent;
    }
  | {
      type: "action_deferred";
      ts: string;
      event_id: string;
      action_id: string;
      action_type: string;
      deferred_to: string;
      breaker_id: string | null;
      reason: string | null;
    }
  | { type: "action_failed"; ts: string; event_id: string; action_id: string; action_type: string; error: string }
  | {
      type: "retry_scheduled";
      ts: string;
      event_id: string;
      action_id: string;
      retry_at: string;
      retry_order_id: string | null;
      attempt: number;
      payment_method: string;
    }
  | {
      type: "retry_fired";
      ts: string;
      event_id: string;
      action_id: string;
      action_type: string;
      fired_at: string;
      retry_order_id: string | null;
      attempt: number | null;
    }
  | { type: "circuit_event"; ts: string; event_type: string; event: RecoveryEvent }
  | {
      type: "circuit_breaker";
      ts: string;
      event_id: string;
      breaker: string;
      breaker_id: string;
      trigger_type: string;
      reason: string;
      cancelled_actions: number;
      event: RecoveryEvent;
    }
  | {
      type: "hitl_resolved";
      ts: string;
      decision: string; // approved | modified | modify_blocked | skipped
      event_id: string;
      action_id: string;
      action_type?: string;
      params?: Record<string, unknown>;
      compliance?: ComplianceNested;
      reason?: string | null;
      event?: RecoveryEvent;
    };

export type SSEFrameType = SSEFrame["type"];
