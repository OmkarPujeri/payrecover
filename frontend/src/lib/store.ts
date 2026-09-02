/**
 * The single client store. One `useReducer` holds everything three surfaces read
 * from: connection health, the REST snapshot aggregates, the live recovery feed,
 * and a per-event reasoning trace assembled frame by frame.
 *
 * Division of labour with the provider:
 *   - The reducer owns what the STREAM alone can build: feed, traces, live
 *     breaker flashes, the active event, connection status.
 *   - Authoritative collections that a single frame can't fully reconstruct -
 *     metrics/economics/comparison, the HITL queue, the scheduled-action band -
 *     are refreshed from REST by the provider and land here via SNAPSHOT. The
 *     reducer never tries to recompute a metric from a frame; that's how numbers
 *     drift. HITL_REMOVE is the one optimistic exception, so a resolved card
 *     disappears instantly instead of waiting for the refetch.
 */

import { frameEventId, frameTool, normalizeCompliance } from "./adapters";
import { parseInstant } from "./format";
import type {
  Comparison,
  Compliance,
  Economics,
  Gate,
  HITLRequestDTO,
  Metrics,
  RecoveryEvent,
  ScheduledAction,
  SSEFrame,
} from "./types";
import type { Snapshot } from "./api";

const FEED_CAP = 60;
const TRACE_CAP = 40; // steps kept per event
const BREAKER_CAP = 12;

export type ConnStatus = "connecting" | "open" | "reconnecting" | "closed";

/** One line in an event's reasoning trace, derived from a single frame. */
export type TraceKind =
  | "detected"
  | "duplicate"
  | "diagnosed"
  | "strategy"
  | "compliance"
  | "gate"
  | "executed"
  | "deferred"
  | "failed"
  | "retry_scheduled"
  | "retry_fired"
  | "circuit"
  | "breaker"
  | "resolved";

export interface TraceStep {
  key: string;
  kind: TraceKind;
  ts: string | null;
  tool?: string | null;
  status?: string | null;
  gate?: Gate | null;
  compliance?: Compliance | null;
  confidence?: number | null;
  source?: string | null;
  detail?: string | null;
  riskFactors?: string[];
  uncertaintyFactors?: string[];
}

export interface BreakerFlash {
  key: string;
  breakerId: string; // CB-00x
  breaker: string;
  eventId: string;
  reason: string;
  cancelledActions: number;
  ts: string | null;
}

export interface StreamState {
  connection: ConnStatus;
  everConnected: boolean;
  lastFrameAt: string | null;

  metrics: Metrics | null;
  economics: Economics | null;
  comparison: Comparison | null;

  feed: RecoveryEvent[]; // newest-first by created_at
  traces: Record<string, TraceStep[]>;
  activeEventId: string | null;

  hitl: HITLRequestDTO[];
  scheduled: ScheduledAction[];
  breakers: BreakerFlash[];
}

export const initialState: StreamState = {
  connection: "connecting",
  everConnected: false,
  lastFrameAt: null,
  metrics: null,
  economics: null,
  comparison: null,
  feed: [],
  traces: {},
  activeEventId: null,
  hitl: [],
  scheduled: [],
  breakers: [],
};

export type StoreAction =
  | { type: "CONN"; status: ConnStatus }
  | { type: "SNAPSHOT"; snapshot: Snapshot }
  | { type: "FRAME"; frame: SSEFrame }
  | { type: "SET_ACTIVE"; eventId: string | null }
  | { type: "HITL_REMOVE"; actionId: string };

// --------------------------------------------------------------------------- //
// helpers
// --------------------------------------------------------------------------- //
function sortKey(e: RecoveryEvent): number {
  const d = parseInstant(e.created_at);
  return d ? d.getTime() : 0;
}

/** Insert-or-merge an event, keeping the feed newest-first and capped. */
function upsertEvent(feed: RecoveryEvent[], event: RecoveryEvent): RecoveryEvent[] {
  const idx = feed.findIndex((e) => e.id === event.id);
  let next: RecoveryEvent[];
  if (idx >= 0) {
    next = feed.slice();
    next[idx] = { ...next[idx], ...event };
  } else {
    next = [event, ...feed];
  }
  next.sort((a, b) => sortKey(b) - sortKey(a));
  return next.length > FEED_CAP ? next.slice(0, FEED_CAP) : next;
}

function appendStep(
  traces: Record<string, TraceStep[]>,
  eventId: string,
  step: TraceStep,
): Record<string, TraceStep[]> {
  const prior = traces[eventId] ?? [];
  const merged = [...prior, step];
  const capped = merged.length > TRACE_CAP ? merged.slice(-TRACE_CAP) : merged;
  return { ...traces, [eventId]: capped };
}

let stepSeq = 0;
function stepKey(kind: string, ts: string | null): string {
  // ts alone isn't unique (batch frames share a ms); add a monotonic counter.
  return `${kind}:${ts ?? "?"}:${stepSeq++}`;
}

/** Turn a frame into a trace step, or null if it isn't trace-worthy. */
function frameToStep(frame: SSEFrame): TraceStep | null {
  const ts = "ts" in frame ? (frame.ts ?? null) : null;
  const base = (kind: TraceKind, extra: Partial<TraceStep> = {}): TraceStep => ({
    key: stepKey(kind, ts),
    kind,
    ts,
    ...extra,
  });

  switch (frame.type) {
    case "failure_detected":
      return base("detected", {
        detail: frame.event.failure_label ?? frame.event.failure_type ?? null,
      });
    case "failure_duplicate":
      return base("duplicate", { detail: "Duplicate failure suppressed" });
    case "event_diagnosed": {
      const dx = frame.diagnosis as Record<string, unknown> | null;
      const detail =
        (dx?.["failure_label"] as string) ??
        (dx?.["root_cause"] as string) ??
        (frame.event.failure_label ?? null);
      return base("diagnosed", { detail });
    }
    case "strategy_selected":
      return base("strategy", {
        tool: frame.strategy.tool,
        confidence: frame.strategy.confidence,
        source: frame.strategy.source,
        detail: frame.strategy.reason,
        riskFactors: frame.strategy.risk_factors ?? [],
        uncertaintyFactors: frame.strategy.uncertainty_factors ?? [],
      });
    case "compliance_checked":
      return base("compliance", {
        compliance: normalizeCompliance(frame.compliance),
      });
    case "gate_decided":
      return base("gate", {
        gate: frame.gate,
        status: frame.status,
        confidence: frame.gate?.confidence ?? null,
        detail: frame.gate?.reason ?? null,
      });
    case "action_executed":
      return base("executed", {
        tool: frameTool(frame),
        status: frame.status,
        detail: frame.razorpay_mode ? `via ${frame.razorpay_mode}` : null,
      });
    case "action_deferred":
      return base("deferred", {
        tool: frameTool(frame),
        detail: frame.reason ?? `deferred to ${frame.deferred_to}`,
      });
    case "action_failed":
      return base("failed", { tool: frameTool(frame), detail: frame.error });
    case "retry_scheduled":
      return base("retry_scheduled", {
        detail: `attempt ${frame.attempt} at ${frame.retry_at}`,
      });
    case "retry_fired":
      return base("retry_fired", {
        tool: frameTool(frame),
        detail: `attempt ${frame.attempt ?? "?"} fired`,
      });
    case "circuit_event":
      return base("circuit", { detail: frame.event_type });
    case "circuit_breaker":
      return base("breaker", {
        detail: `${frame.breaker_id} · ${frame.reason}`,
      });
    case "hitl_resolved":
      return base("resolved", { detail: frame.decision });
    default:
      return null; // `connected` and anything unknown
  }
}

// --------------------------------------------------------------------------- //
// reducer
// --------------------------------------------------------------------------- //
export function reducer(state: StreamState, action: StoreAction): StreamState {
  switch (action.type) {
    case "CONN":
      return {
        ...state,
        connection: action.status,
        everConnected: state.everConnected || action.status === "open",
      };

    case "SNAPSHOT": {
      const s = action.snapshot;
      // Merge only the pieces that came back; a failed fetch keeps the last good
      // value rather than blanking the panel.
      return {
        ...state,
        metrics: s.metrics ?? state.metrics,
        economics: s.economics ?? state.economics,
        comparison: s.comparison ?? state.comparison,
        feed: s.events ? mergeSnapshotFeed(state.feed, s.events.events) : state.feed,
        hitl: s.hitl ? s.hitl.pending : state.hitl,
        scheduled: s.scheduled ? s.scheduled.scheduled : state.scheduled,
      };
    }

    case "SET_ACTIVE":
      return { ...state, activeEventId: action.eventId };

    case "HITL_REMOVE":
      return {
        ...state,
        hitl: state.hitl.filter((h) => h.action_id !== action.actionId),
      };

    case "FRAME": {
      const frame = action.frame;
      if (frame.type === "connected") return state;

      let next = state;
      const ts = "ts" in frame ? (frame.ts ?? null) : null;
      const eventId = frameEventId(frame);

      // Upsert the event wherever a frame carries a full one.
      if ("event" in frame && frame.event) {
        next = { ...next, feed: upsertEvent(next.feed, frame.event) };
      }

      // Append the reasoning step and focus the event it belongs to.
      const step = frameToStep(frame);
      if (step && eventId) {
        next = {
          ...next,
          traces: appendStep(next.traces, eventId, step),
          activeEventId: eventId,
        };
      }

      // Live breaker flash (authoritative breaker log is fetched elsewhere).
      if (frame.type === "circuit_breaker" && eventId) {
        const flash: BreakerFlash = {
          key: stepKey("cb", ts),
          breakerId: frame.breaker_id,
          breaker: frame.breaker,
          eventId,
          reason: frame.reason,
          cancelledActions: frame.cancelled_actions,
          ts,
        };
        next = {
          ...next,
          breakers: [flash, ...next.breakers].slice(0, BREAKER_CAP),
        };
      }

      return { ...next, lastFrameAt: ts ?? next.lastFrameAt };
    }

    default:
      return state;
  }
}

/**
 * Fold a REST events page into the live feed. Snapshot events are authoritative
 * for fields but must not clobber the ordering or evict live-only events, so we
 * upsert each and re-cap.
 */
function mergeSnapshotFeed(
  feed: RecoveryEvent[],
  incoming: RecoveryEvent[],
): RecoveryEvent[] {
  let next = feed;
  for (const e of incoming) next = upsertEvent(next, e);
  return next;
}
