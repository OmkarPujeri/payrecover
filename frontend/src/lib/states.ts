/**
 * The five-lamp semantics, in one place. The PRD mandates exactly five state
 * colors and forbids any other chroma, so every component that shows status maps
 * through here rather than picking its own colors.
 *
 * Class strings are written out in full (never composed at runtime) so Tailwind's
 * compiler sees them and doesn't purge them.
 *
 * Status vocabularies mirror backend `app/execution/statuses.py`, the confidence
 * gate (`app/agent/confidence.py`), and the six executor tools.
 */

import type { Gate } from "./types";
import type { TraceKind, TraceStep } from "./store";

export type StateKey = "pending" | "running" | "done" | "waiting" | "blocked";

export interface LampStyle {
  key: StateKey;
  label: string;
  color: string; // CSS var, for inline styles (dynamic contexts)
  text: string; // text-* utility
  dot: string; // bg-* for the lamp dot
  chip: string; // full chip: bg wash + text + border
}

export const LAMP: Record<StateKey, LampStyle> = {
  pending: {
    key: "pending",
    label: "Pending",
    color: "var(--color-state-pending)",
    text: "text-state-pending",
    dot: "bg-state-pending",
    chip: "bg-state-pending/10 text-state-pending border border-state-pending/30",
  },
  running: {
    key: "running",
    label: "Running",
    color: "var(--color-state-running)",
    text: "text-state-running",
    dot: "bg-state-running",
    chip: "bg-state-running/10 text-state-running border border-state-running/30",
  },
  done: {
    key: "done",
    label: "Done",
    color: "var(--color-state-done)",
    text: "text-state-done",
    dot: "bg-state-done",
    chip: "bg-state-done/10 text-state-done border border-state-done/30",
  },
  waiting: {
    key: "waiting",
    label: "Waiting",
    color: "var(--color-state-waiting)",
    text: "text-state-waiting",
    dot: "bg-state-waiting",
    chip: "bg-state-waiting/10 text-state-waiting border border-state-waiting/30",
  },
  blocked: {
    key: "blocked",
    label: "Blocked",
    color: "var(--color-state-blocked)",
    text: "text-state-blocked",
    dot: "bg-state-blocked",
    chip: "bg-state-blocked/10 text-state-blocked border border-state-blocked/30",
  },
};

// --------------------------------------------------------------------------- //
// event recovery_status → lamp  (EV_* in statuses.py)
// --------------------------------------------------------------------------- //
export function eventState(status: string | null | undefined): StateKey {
  switch (status) {
    case "recovered":
      return "done";
    case "in_progress":
    case "diagnosed":
      return "running";
    case "needs_review":
    case "escalated":
      return "waiting";
    case "blocked":
    case "unrecoverable":
    case "halted":
      return "blocked";
    case "skipped":
    case "pending":
    default:
      return "pending";
  }
}

const EVENT_STATUS_LABEL: Record<string, string> = {
  pending: "Pending",
  diagnosed: "Diagnosed",
  in_progress: "In progress",
  needs_review: "Needs review",
  escalated: "Escalated",
  blocked: "Blocked",
  halted: "Halted",
  skipped: "Skipped",
  recovered: "Recovered",
  unrecoverable: "Unrecoverable",
};

export function eventStatusLabel(status: string | null | undefined): string {
  if (!status) return "-";
  return EVENT_STATUS_LABEL[status] ?? status;
}

// --------------------------------------------------------------------------- //
// action status → lamp
// --------------------------------------------------------------------------- //
export function actionState(status: string | null | undefined): StateKey {
  switch (status) {
    case "completed":
      return "done";
    case "executing":
    case "approved":
      return "running";
    case "scheduled":
    case "pending_review":
    case "escalated":
      return "waiting";
    case "blocked":
    case "failed":
      return "blocked";
    case "cancelled":
    case "skipped":
    default:
      return "pending";
  }
}

const ACTION_STATUS_LABEL: Record<string, string> = {
  approved: "Approved",
  pending_review: "Pending review",
  escalated: "Escalated",
  blocked: "Blocked",
  executing: "Executing",
  scheduled: "Scheduled",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  skipped: "Skipped",
};

export function actionStatusLabel(status: string | null | undefined): string {
  if (!status) return "-";
  return ACTION_STATUS_LABEL[status] ?? status;
}

// --------------------------------------------------------------------------- //
// confidence gate → lamp + label
// --------------------------------------------------------------------------- //
export function gateMeta(action: string | null | undefined): {
  state: StateKey;
  label: string;
} {
  switch (action) {
    case "auto_execute":
      return { state: "done", label: "Auto-execute" };
    case "auto_execute_flagged":
      return { state: "running", label: "Auto-execute · flagged" };
    case "hitl_review":
      return { state: "waiting", label: "Human review" };
    case "escalate":
      return { state: "waiting", label: "Escalate" };
    case "blocked":
      return { state: "blocked", label: "Blocked" };
    default:
      return { state: "pending", label: action ?? "-" };
  }
}

// --------------------------------------------------------------------------- //
// tools → human labels (the six executor tools)
// --------------------------------------------------------------------------- //
const TOOL_LABEL: Record<string, string> = {
  schedule_smart_retry: "Smart retry",
  generate_payment_link: "Payment link",
  send_recovery_notification: "Recovery notification",
  offer_alternative_method: "Alternative method",
  escalate_to_merchant: "Escalate to merchant",
  mark_unrecoverable: "Mark unrecoverable",
};

export function toolLabel(tool: string | null | undefined): string {
  if (!tool) return "-";
  return TOOL_LABEL[tool] ?? tool;
}

// --------------------------------------------------------------------------- //
// compliance decision → lamp
// --------------------------------------------------------------------------- //
export function complianceState(decision: string | null | undefined): StateKey {
  switch (decision) {
    case "APPROVED":
      return "done";
    case "MODIFIED":
      return "waiting";
    case "BLOCKED":
      return "blocked";
    default:
      return "pending";
  }
}

// --------------------------------------------------------------------------- //
// trace step → lamp (some kinds derive from payload)
// --------------------------------------------------------------------------- //
const KIND_STATE: Partial<Record<TraceKind, StateKey>> = {
  detected: "pending",
  duplicate: "pending",
  diagnosed: "running",
  strategy: "running",
  compliance: "running",
  deferred: "waiting",
  failed: "blocked",
  retry_scheduled: "waiting",
  retry_fired: "running",
  circuit: "pending",
  breaker: "blocked",
};

export function traceStepState(step: TraceStep): StateKey {
  switch (step.kind) {
    case "gate":
      return gateMeta(step.gate?.action).state;
    case "executed":
      return step.status === "failed" ? "blocked" : "done";
    case "resolved":
      if (step.detail === "modify_blocked") return "blocked";
      if (step.detail === "skipped") return "pending";
      return "done";
    default:
      return KIND_STATE[step.kind] ?? "pending";
  }
}

const KIND_LABEL: Record<TraceKind, string> = {
  detected: "Failure detected",
  duplicate: "Duplicate suppressed",
  diagnosed: "Diagnosed",
  strategy: "Strategy selected",
  compliance: "Compliance checked",
  gate: "Confidence gate",
  executed: "Action executed",
  deferred: "Action deferred",
  failed: "Action failed",
  retry_scheduled: "Retry scheduled",
  retry_fired: "Retry fired",
  circuit: "Lifecycle event",
  breaker: "Circuit breaker",
  resolved: "Human decision",
};

export function traceKindLabel(kind: TraceKind): string {
  return KIND_LABEL[kind];
}

/** Convenience: the gate a trace/gate step carries, typed. */
export function stepGate(step: TraceStep): Gate | null {
  return step.gate ?? null;
}
