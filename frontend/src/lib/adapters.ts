/**
 * The wire↔client boundary. Every response and every SSE frame passes through
 * here before a component sees it, so the four format traps are solved exactly
 * once. If a key ever moves on the backend, this is the only file that changes.
 *
 * TRAP #1 - offset-less datetimes. Handled in `format.parseInstant`; this module
 *   never calls `new Date()` on a server string, it defers to the render helpers.
 *
 * TRAP #2 - `action_executed` carries the tool name under `action`, while every
 *   other action-bearing frame/row uses `action_type`. `frameTool()` reads the
 *   right key so the reducer and trace never guess.
 *
 * TRAP #3 - compliance ships in two physical shapes (nested object vs. flat
 *   `compliance_*` columns) and actions in three. `normalizeCompliance` and
 *   `toClientAction` collapse them to one client type each.
 *
 * TRAP #4 - no SSE replay. There is no `id:`/`Last-Event-ID`, and the server
 *   sends no backlog on reconnect. So on every reconnect *after the first*, the
 *   client must re-pull the REST snapshot or it silently drifts. That rule is
 *   documented by `RESYNC` below and enforced by `api.loadSnapshot` + the hook.
 */

import type {
  AuditRowDTO,
  ClientAction,
  Compliance,
  ComplianceFlat,
  ComplianceNested,
  DashboardActionDTO,
  SSEFrame,
} from "./types";

// --------------------------------------------------------------------------- //
// TRAP #3 - compliance
// --------------------------------------------------------------------------- //
function isFlat(
  src: ComplianceNested | ComplianceFlat,
): src is ComplianceFlat {
  return "compliance_decision" in src;
}

/**
 * Collapse either compliance shape (nested `{decision,...}` or flat
 * `{compliance_decision,...}`) to the client `Compliance`. A missing block
 * becomes all-null rather than throwing, because plenty of actions (e.g. a
 * diagnostic step) legitimately have no compliance verdict.
 */
export function normalizeCompliance(
  src: ComplianceNested | ComplianceFlat | null | undefined,
): Compliance {
  if (!src) {
    return { decision: null, ruleId: null, ruleName: null, reason: null };
  }
  if (isFlat(src)) {
    return {
      decision: src.compliance_decision,
      ruleId: src.compliance_rule_id,
      ruleName: src.compliance_rule_name,
      reason: src.compliance_reason,
    };
  }
  return {
    decision: src.decision,
    ruleId: src.rule_id,
    ruleName: src.rule_name,
    reason: src.reason,
    modification: src.modification ?? null,
  };
}

// --------------------------------------------------------------------------- //
// TRAP #3 - actions (3 raw shapes → 1 ClientAction)
// --------------------------------------------------------------------------- //
/** Event-detail action (`_action_to_dict`, flat compliance). */
export function dashboardActionToClient(
  dto: DashboardActionDTO,
  eventId: string | null,
): ClientAction {
  return {
    id: dto.id,
    eventId,
    agent: dto.agent_name,
    actionType: dto.action_type,
    params: dto.action_params,
    reasoning: dto.agent_reasoning,
    confidence: dto.confidence_score,
    riskFactors: dto.risk_factors ?? [],
    uncertaintyFactors: dto.uncertainty_factors ?? [],
    compliance: normalizeCompliance(dto),
    gate: null, // event-detail rows don't re-serialize the gate
    status: dto.status,
    costPaise: dto.cost_paise,
    scheduledAt: dto.scheduled_at,
    executedAt: dto.executed_at,
    createdAt: dto.created_at,
    result: dto.result,
  };
}

/** Audit-log row (`_audit_row`, flat compliance, `timestamp`, event context). */
export function auditRowToClient(dto: AuditRowDTO): ClientAction {
  return {
    id: dto.id,
    eventId: dto.recovery_event_id,
    agent: dto.agent_name,
    actionType: dto.action_type,
    params: dto.action_params,
    reasoning: dto.agent_reasoning,
    confidence: dto.confidence_score,
    riskFactors: dto.risk_factors ?? [],
    uncertaintyFactors: dto.uncertainty_factors ?? [],
    compliance: normalizeCompliance(dto),
    gate: dto.gate,
    status: dto.status,
    costPaise: dto.cost_paise,
    scheduledAt: dto.scheduled_at,
    executedAt: dto.executed_at,
    createdAt: dto.timestamp, // audit rows name it `timestamp`
    result: dto.result,
    source: dto.source,
    ctxOrderId: dto.razorpay_order_id,
    ctxPaymentId: dto.razorpay_payment_id,
    ctxAmountPaise: dto.amount_paise,
    ctxFailureLabel: dto.failure_label,
    ctxFailureCategory: dto.failure_category,
    ctxRecoveryStatus: dto.recovery_status,
  };
}

// --------------------------------------------------------------------------- //
// TRAP #2 - the tool name lives under different keys per frame
// --------------------------------------------------------------------------- //
/**
 * The tool a frame is about, read from whichever key that frame uses:
 * `action_executed` → `action`; deferred/failed/fired → `action_type`;
 * `strategy_selected` → `strategy.tool`. Returns null for frames that name no
 * tool (connected, failure_detected, compliance_checked, circuit_*).
 */
export function frameTool(frame: SSEFrame): string | null {
  switch (frame.type) {
    case "action_executed":
      return frame.action; // TRAP #2
    case "action_deferred":
    case "action_failed":
    case "retry_fired":
      return frame.action_type;
    case "strategy_selected":
      return frame.strategy.tool;
    default:
      return null;
  }
}

/** The recovery-event id a frame concerns, regardless of how it's carried. */
export function frameEventId(frame: SSEFrame): string | null {
  if ("event_id" in frame && frame.event_id) return frame.event_id;
  if ("event" in frame && frame.event) return frame.event.id;
  return null;
}

/**
 * Parse one SSE `data:` payload into a typed frame. Returns null for anything
 * unparseable or without a `type` (keep-alive comments never reach here - the
 * browser's EventSource strips `:`-prefixed lines before dispatch).
 */
export function parseFrame(data: string): SSEFrame | null {
  try {
    const obj = JSON.parse(data);
    if (obj && typeof obj === "object" && typeof obj.type === "string") {
      return obj as SSEFrame;
    }
  } catch {
    /* fall through */
  }
  return null;
}

// --------------------------------------------------------------------------- //
// TRAP #4 - resync contract (no replay on reconnect)
// --------------------------------------------------------------------------- //
/**
 * What the client must re-pull on every reconnect *after the first* connect,
 * because the stream carries no backlog. The hook owns the "after the first"
 * logic; `api.loadSnapshot` owns the fetching. This constant exists so the
 * intent is greppable and `verify-wire` can assert the endpoints still exist.
 */
export const RESYNC = {
  reason:
    "SSE has no id:/Last-Event-ID and the server sends no backlog on reconnect, " +
    "so REST is the only way to recover state missed while disconnected.",
  endpoints: [
    "/api/dashboard/metrics",
    "/api/dashboard/economics",
    "/api/dashboard/metrics/comparison",
    "/api/dashboard/events",
    "/api/hitl/pending",
    "/api/actions/scheduled",
  ] as const,
} as const;
