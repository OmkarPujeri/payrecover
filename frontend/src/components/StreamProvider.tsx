"use client";

/**
 * App-wide live store. Mounts once at the root, opens the single SSE connection,
 * and exposes the reducer state plus a few intent methods through context. Every
 * surface reads from here; nothing else opens a socket or holds cross-cutting
 * state.
 *
 * Refresh policy (TRAP #4 + "never recompute a metric from a frame"):
 *   - On mount: one REST snapshot to seed, so the dashboard paints before the
 *     first frame arrives.
 *   - On every reconnect after the first open: a full resync, because the stream
 *     replays nothing.
 *   - After mutating frames: debounced, *targeted* refetches - aggregates
 *     (metrics/economics/comparison), the HITL queue, and the scheduled band -
 *     so the numbers stay authoritative without the reducer ever doing money math.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from "react";
import { useSSE } from "@/hooks/useSSE";
import {
  getComparison,
  getEconomics,
  getMetrics,
  getPendingHITL,
  getScheduled,
  loadSnapshot,
  streamUrl,
  type Snapshot,
} from "@/lib/api";
import {
  initialState,
  reducer,
  type ConnStatus,
  type StreamState,
  type TraceStep,
} from "@/lib/store";
import type { SSEFrame } from "@/lib/types";

const EMPTY_SNAPSHOT: Snapshot = {
  metrics: null,
  economics: null,
  comparison: null,
  events: null,
  hitl: null,
  scheduled: null,
};

interface StreamContextValue {
  state: StreamState;
  setActiveEvent: (eventId: string | null) => void;
  removeHitl: (actionId: string) => void;
  /** Full REST resync - used by the reconnect banner and after a chaos run. */
  refresh: () => Promise<void>;
}

const StreamContext = createContext<StreamContextValue | null>(null);

// Which follow-up refetch each frame type warrants.
const AGG_FRAMES = new Set<SSEFrame["type"]>([
  "action_executed",
  "action_failed",
  "action_deferred",
  "retry_fired",
  "circuit_breaker",
  "circuit_event",
  "hitl_resolved",
]);
const HITL_FRAMES = new Set<SSEFrame["type"]>(["gate_decided", "hitl_resolved"]);
const SCHED_FRAMES = new Set<SSEFrame["type"]>([
  "gate_decided",
  "action_deferred",
  "retry_scheduled",
  "retry_fired",
  "circuit_breaker",
]);

export function StreamProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);

  const hasOpenedRef = useRef(false);
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  // --- targeted, coalesced refetches -------------------------------------- //
  const refreshAggregates = useCallback(async () => {
    const [m, e, c] = await Promise.allSettled([
      getMetrics(),
      getEconomics(),
      getComparison(),
    ]);
    dispatch({
      type: "SNAPSHOT",
      snapshot: {
        ...EMPTY_SNAPSHOT,
        metrics: m.status === "fulfilled" ? m.value : null,
        economics: e.status === "fulfilled" ? e.value : null,
        comparison: c.status === "fulfilled" ? c.value : null,
      },
    });
  }, []);

  const refreshHitl = useCallback(async () => {
    try {
      const hitl = await getPendingHITL();
      dispatch({ type: "SNAPSHOT", snapshot: { ...EMPTY_SNAPSHOT, hitl } });
    } catch {
      /* keep prior queue */
    }
  }, []);

  const refreshScheduled = useCallback(async () => {
    try {
      const scheduled = await getScheduled();
      dispatch({ type: "SNAPSHOT", snapshot: { ...EMPTY_SNAPSHOT, scheduled } });
    } catch {
      /* keep prior band */
    }
  }, []);

  const debounce = useCallback((key: string, fn: () => void, ms: number) => {
    clearTimeout(timers.current[key]);
    timers.current[key] = setTimeout(fn, ms);
  }, []);

  const refresh = useCallback(async () => {
    const snapshot = await loadSnapshot();
    dispatch({ type: "SNAPSHOT", snapshot });
  }, []);

  // --- seed once on mount ------------------------------------------------- //
  useEffect(() => {
    void refresh();
    const t = timers.current;
    return () => {
      for (const id of Object.values(t)) clearTimeout(id);
    };
  }, [refresh]);

  // --- SSE lifecycle ------------------------------------------------------ //
  const onStatus = useCallback(
    (status: ConnStatus) => {
      dispatch({ type: "CONN", status });
      if (status === "open") {
        if (hasOpenedRef.current) {
          // A reconnect: resync everything the stream didn't replay.
          void refresh();
        } else {
          hasOpenedRef.current = true; // first open - mount seed already covered it
        }
      }
    },
    [refresh],
  );

  const onFrame = useCallback(
    (frame: SSEFrame) => {
      dispatch({ type: "FRAME", frame });
      if (AGG_FRAMES.has(frame.type)) debounce("agg", refreshAggregates, 700);
      if (HITL_FRAMES.has(frame.type)) debounce("hitl", refreshHitl, 400);
      if (SCHED_FRAMES.has(frame.type)) debounce("sched", refreshScheduled, 400);
    },
    [debounce, refreshAggregates, refreshHitl, refreshScheduled],
  );

  useSSE({ url: streamUrl(), onFrame, onStatus });

  const setActiveEvent = useCallback(
    (eventId: string | null) => dispatch({ type: "SET_ACTIVE", eventId }),
    [],
  );
  const removeHitl = useCallback(
    (actionId: string) => dispatch({ type: "HITL_REMOVE", actionId }),
    [],
  );

  const value = useMemo<StreamContextValue>(
    () => ({ state, setActiveEvent, removeHitl, refresh }),
    [state, setActiveEvent, removeHitl, refresh],
  );

  return <StreamContext.Provider value={value}>{children}</StreamContext.Provider>;
}

// --------------------------------------------------------------------------- //
// selectors
// --------------------------------------------------------------------------- //
export function useStream(): StreamContextValue {
  const ctx = useContext(StreamContext);
  if (!ctx) throw new Error("useStream must be used within <StreamProvider>");
  return ctx;
}

/** The reasoning trace for the currently-featured event, newest step last. */
export function useActiveTrace(): { eventId: string | null; steps: TraceStep[] } {
  const { state } = useStream();
  const eventId = state.activeEventId;
  return { eventId, steps: eventId ? (state.traces[eventId] ?? []) : [] };
}
