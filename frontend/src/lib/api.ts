/**
 * The one place the browser talks to the backend. Every REST call and the SSE
 * URL are keyed off `NEXT_PUBLIC_API_BASE`, baked at build time (see
 * next.config.ts). Nothing else in the app constructs a backend URL.
 *
 * Responses are returned raw-typed; callers pass action/compliance blocks through
 * `adapters` to normalize. Errors throw `ApiError`, which preserves the HTTP
 * status and the parsed `detail` - the HITL modify path needs that, because a
 * compliance block comes back as `409` with an *object* detail
 * (`{message, rule_id, rule_name, reason}`), not a string.
 */

import type {
  AuditPage,
  BreakerPage,
  ChaosRun,
  Comparison,
  Economics,
  EventDetailDTO,
  EventsPage,
  HITLPending,
  HITLResolution,
  Metrics,
  PresetList,
  RunBatch,
  ScheduledPage,
} from "./types";

export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"
).replace(/\/$/, "");

/** The SSE endpoint. The stream is opened by the hook, not by `request`. */
export function streamUrl(): string {
  return `${API_BASE}/api/stream`;
}

export class ApiError extends Error {
  readonly status: number;
  /** Parsed FastAPI `detail` - a string for most errors, an object for the
   *  compliance-block 409 on HITL modify. */
  readonly detail: unknown;
  constructor(status: number, detail: unknown, fallback: string) {
    const msg =
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail
          ? String((detail as { message: unknown }).message)
          : fallback;
    super(msg);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch {
    // Network-level failure (backend down, CORS, offline). Give a stable message.
    throw new ApiError(0, null, `Cannot reach the API at ${API_BASE}`);
  }

  if (!res.ok) {
    let detail: unknown = null;
    try {
      const body = await res.json();
      detail = body?.detail ?? body;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

// --------------------------------------------------------------------------- //
// Dashboard reads
// --------------------------------------------------------------------------- //
export const getMetrics = () => request<Metrics>("/api/dashboard/metrics");
export const getEconomics = () => request<Economics>("/api/dashboard/economics");
export const getComparison = () =>
  request<Comparison>("/api/dashboard/metrics/comparison");

export const getEvents = (limit = 50, skip = 0) =>
  request<EventsPage>(`/api/dashboard/events${qs({ limit, skip })}`);

export const getEventDetail = (eventId: string) =>
  request<EventDetailDTO>(`/api/dashboard/events/${eventId}`);

// --------------------------------------------------------------------------- //
// HITL - approve/modify/skip. A 200 means the decision was recorded, NOT that
// the action executed: callers must read `execution.executed`. A blocked modify
// throws ApiError(409) whose `detail` is an object.
// --------------------------------------------------------------------------- //
export const getPendingHITL = () => request<HITLPending>("/api/hitl/pending");

export const approveHITL = (actionId: string) =>
  request<HITLResolution>(`/api/hitl/${actionId}/approve`, { method: "POST" });

export const modifyHITL = (
  actionId: string,
  params: Record<string, unknown>,
  note?: string,
) =>
  request<HITLResolution>(`/api/hitl/${actionId}/modify`, {
    method: "POST",
    body: JSON.stringify({ params, note }),
  });

export const skipHITL = (actionId: string, reason?: string) =>
  request<HITLResolution>(`/api/hitl/${actionId}/skip`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

// --------------------------------------------------------------------------- //
// Scheduled actions - the compliance band's tick source
// --------------------------------------------------------------------------- //
export const getScheduled = () =>
  request<ScheduledPage>("/api/actions/scheduled");

// --------------------------------------------------------------------------- //
// Audit
// --------------------------------------------------------------------------- //
export interface AuditFilters {
  event_id?: string;
  agent_name?: string;
  action_type?: string;
  status?: string;
  compliance_decision?: string;
  limit?: number;
  skip?: number;
}

export const getAuditLog = (filters: AuditFilters = {}) =>
  request<AuditPage>(`/api/audit/log${qs({ limit: 100, skip: 0, ...filters })}`);

export const getBreakerLog = (limit = 100) =>
  request<BreakerPage>(`/api/audit/breakers${qs({ limit })}`);

/**
 * Download the audit export (CSV or JSON) as a file, via `fetch` + Blob.
 *
 * Why not a plain `<a href>` to the endpoint: the anchor navigates the tab,
 * so any failure - backend down, a filter the API rejects - replaces the whole
 * dashboard with a raw error page and downloads nothing. Fetching instead
 * means failures throw ApiError like every other call (the drawer shows them
 * inline), and only a successful response becomes a file. The bytes saved are
 * still exactly the server's - the Blob is the response body, unmodified.
 */
export async function downloadAuditExport(
  format: "csv" | "json",
  filters: Omit<AuditFilters, "skip"> = {},
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(
      `${API_BASE}/api/audit/export${qs({ format, ...filters })}`,
    );
  } catch {
    throw new ApiError(0, null, `Cannot reach the API at ${API_BASE}`);
  }
  if (!res.ok) {
    let detail: unknown = null;
    try {
      const body = await res.json();
      detail = body?.detail ?? body;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail, `Export failed: ${res.status} ${res.statusText}`);
  }

  const now = new Date().toISOString(); // 2026-09-02T04:20:30.123Z
  const stamp = `${now.slice(0, 10).replace(/-/g, "")}_${now.slice(11, 19).replace(/:/g, "")}`;
  const filename = `payrecover_audit_${stamp}.${format}`;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// Simulator - the demo controls
// --------------------------------------------------------------------------- //
export const getPresets = () =>
  request<PresetList>("/api/simulator/presets");

export interface ChaosOptions {
  diagnose?: boolean;
  recover?: boolean;
  execute?: boolean;
  max_events?: number;
}

/** Run a named chaos preset end-to-end (a demo beat). */
export const runChaos = (preset: string, opts: ChaosOptions = {}) =>
  request<ChaosRun>(`/api/simulator/chaos/${preset}`, {
    method: "POST",
    body: JSON.stringify({
      diagnose: true,
      recover: true,
      execute: true,
      ...opts,
    }),
  });

/** Seed a spread of events through the full pipeline. */
export const runBatch = (count = 20) =>
  request<RunBatch>(`/api/simulator/run-batch`, {
    method: "POST",
    body: JSON.stringify({ count }),
  });

/** Advance the scheduler - fire any actions whose time has come. Used to make
 *  the compliance band's deferred ticks resolve on demand during a demo. */
export const runDueActions = () =>
  request<{ status: string; fired: number; [k: string]: unknown }>(
    `/api/simulator/run-due-actions`,
    { method: "POST" },
  );

// --------------------------------------------------------------------------- //
// Snapshot - TRAP #4. The full REST state the client seeds on first connect and
// re-pulls on every reconnect (the stream has no backlog). Uses allSettled so a
// single slow endpoint can't blank the whole dashboard; missing pieces stay null
// and the reducer keeps the last good value.
// --------------------------------------------------------------------------- //
export interface Snapshot {
  metrics: Metrics | null;
  economics: Economics | null;
  comparison: Comparison | null;
  events: EventsPage | null;
  hitl: HITLPending | null;
  scheduled: ScheduledPage | null;
}

export async function loadSnapshot(): Promise<Snapshot> {
  // Order mirrors adapters.RESYNC.endpoints - verify-wire cross-checks the two.
  const [metrics, economics, comparison, events, hitl, scheduled] =
    await Promise.allSettled([
      getMetrics(),
      getEconomics(),
      getComparison(),
      getEvents(),
      getPendingHITL(),
      getScheduled(),
    ]);
  const val = <T>(r: PromiseSettledResult<T>): T | null =>
    r.status === "fulfilled" ? r.value : null;
  return {
    metrics: val(metrics),
    economics: val(economics),
    comparison: val(comparison),
    events: val(events),
    hitl: val(hitl),
    scheduled: val(scheduled),
  };
}
