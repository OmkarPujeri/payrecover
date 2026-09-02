"use client";

/**
 * The record, on demand. A right-side drawer over the ledger with the full
 * audit chain - every agent decision in order, filterable by event, agent,
 * status and compliance verdict - plus a tab of breaker trips, and the export
 * that turns the whole reasoning chain into a file (CSV or JSON) in one click.
 *
 * Rows render through `adapters.auditRowToClient`, so the audit's three-shape
 * trap is handled at the boundary like everywhere else. "Load more" pages with
 * skip; filters refetch from scratch. Export fetches the file and saves it via
 * a Blob - the bytes are still exactly the server's, but a failure (backend
 * down, a filter the API rejects) shows inline instead of navigating the whole
 * dashboard to a raw error page.
 */

import { useEffect, useState } from "react";
import { Amount, Empty, Lamp, StateChip } from "@/components/primitives";
import {
  ApiError,
  downloadAuditExport,
  getAuditLog,
  getBreakerLog,
  type AuditFilters,
} from "@/lib/api";
import { auditRowToClient } from "@/lib/adapters";
import { istDateTime, shortId } from "@/lib/format";
import { actionState, actionStatusLabel, toolLabel } from "@/lib/states";
import type { BreakerLogRow, ClientAction } from "@/lib/types";

const PAGE = 100;

interface UiFilters {
  event: string;
  agent: string;
  status: string;
  compliance: string;
}

const NO_FILTERS: UiFilters = { event: "", agent: "", status: "", compliance: "" };

/** UI filters → API filters (empty strings dropped by the client's qs). */
function toApi(f: UiFilters): AuditFilters {
  return {
    event_id: f.event.trim() || undefined,
    agent_name: f.agent || undefined,
    status: f.status || undefined,
    compliance_decision: f.compliance || undefined,
  };
}

export function AuditDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [tab, setTab] = useState<"log" | "breakers">("log");
  const [filters, setFilters] = useState<UiFilters>(NO_FILTERS);
  const [rows, setRows] = useState<ClientAction[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [breakers, setBreakers] = useState<BreakerLogRow[]>([]);
  const [exporting, setExporting] = useState<"csv" | "json" | null>(null);

  // Esc closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // The log - refetched from scratch whenever the filters change while open.
  useEffect(() => {
    if (!open || tab !== "log") return;
    let alive = true;
    setLoading(true);
    getAuditLog({ ...toApi(filters), limit: PAGE, skip: 0 })
      .then((page) => {
        if (!alive) return;
        setRows(page.entries.map(auditRowToClient));
        setTotal(page.total);
        setError(null);
      })
      .catch((e) =>
        alive && setError(e instanceof ApiError ? e.message : "Cannot load the audit log"),
      )
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [open, tab, filters]);

  // The breaker trips - loaded once per open.
  useEffect(() => {
    if (!open || tab !== "breakers") return;
    let alive = true;
    getBreakerLog(100)
      .then((page) => alive && (setBreakers(page.breakers), setError(null)))
      .catch((e) =>
        alive && setError(e instanceof ApiError ? e.message : "Cannot load breaker trips"),
      );
    return () => {
      alive = false;
    };
  }, [open, tab]);

  function loadMore() {
    setLoading(true);
    getAuditLog({ ...toApi(filters), limit: PAGE, skip: rows.length })
      .then((page) => {
        setRows((prev) => [...prev, ...page.entries.map(auditRowToClient)]);
        setTotal(page.total);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Cannot load more"))
      .finally(() => setLoading(false));
  }

  async function download(format: "csv" | "json") {
    setExporting(format);
    try {
      await downloadAuditExport(format, toApi(filters));
      setError(null);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : `Cannot export ${format.toUpperCase()}`,
      );
    } finally {
      setExporting(null);
    }
  }

  if (!open) return null;

  // Options derive from what's loaded - the vocabulary is closed server-side.
  const agents = [...new Set(rows.map((r) => r.agent))].sort();
  const statuses = [...new Set(rows.map((r) => r.status))].sort();

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/25" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Audit log"
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col border-l border-paper-rule bg-paper text-paper-text shadow-sheet"
      >
        {/* header */}
        <div className="flex items-center gap-3 border-b border-paper-rule px-4 py-3">
          <h2 className="text-sm font-semibold">Audit log</h2>
          {tab === "log" && total > 0 && (
            <span className="font-mono text-xs text-paper-faint">{total} entries</span>
          )}
          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => download("csv")}
              disabled={exporting !== null}
              className="rounded border border-paper-rule px-2 py-1 text-[11px] font-medium text-paper-dim transition-colors hover:border-state-done/50 hover:text-state-done disabled:opacity-50"
            >
              {exporting === "csv" ? "…" : "↓ CSV"}
            </button>
            <button
              type="button"
              onClick={() => download("json")}
              disabled={exporting !== null}
              className="rounded border border-paper-rule px-2 py-1 text-[11px] font-medium text-paper-dim transition-colors hover:border-state-done/50 hover:text-state-done disabled:opacity-50"
            >
              {exporting === "json" ? "…" : "↓ JSON"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="ml-1 rounded px-2 text-paper-dim transition-colors hover:text-paper-text"
              aria-label="Close audit log"
            >
              ✕
            </button>
          </div>
        </div>

        {/* tabs */}
        <div className="flex gap-1 border-b border-paper-rule px-4 pt-2">
          {(["log", "breakers"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              aria-pressed={tab === t}
              className={`-mb-px rounded-t border border-b-0 px-3 py-1.5 text-xs font-medium transition-colors ${
                tab === t
                  ? "border-paper-rule bg-paper text-paper-text"
                  : "border-transparent text-paper-faint hover:text-paper-dim"
              }`}
            >
              {t === "log" ? "Decisions" : "Breaker trips"}
            </button>
          ))}
        </div>

        {/* filters (log tab only) */}
        {tab === "log" && (
          <div className="flex flex-wrap items-center gap-1.5 border-b border-paper-rule px-4 py-2.5">
            <input
              value={filters.event}
              onChange={(e) => setFilters({ ...filters, event: e.target.value })}
              placeholder="event / order id"
              aria-label="Filter by event or order id"
              className="w-40 rounded border border-paper-rule bg-paper-raised px-2 py-1 text-xs text-paper-text placeholder:text-paper-faint focus-visible:outline-none"
            />
            <Select
              label="Agent"
              value={filters.agent}
              onChange={(v) => setFilters({ ...filters, agent: v })}
              options={agents}
            />
            <Select
              label="Status"
              value={filters.status}
              onChange={(v) => setFilters({ ...filters, status: v })}
              options={statuses}
            />
            <Select
              label="Compliance"
              value={filters.compliance}
              onChange={(v) => setFilters({ ...filters, compliance: v })}
              options={["APPROVED", "MODIFIED", "BLOCKED"]}
            />
            {(filters.event || filters.agent || filters.status || filters.compliance) && (
              <button
                type="button"
                onClick={() => setFilters(NO_FILTERS)}
                className="text-[11px] text-paper-faint underline decoration-dotted underline-offset-2 hover:text-paper-dim"
              >
                clear
              </button>
            )}
          </div>
        )}

        {/* body */}
        <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {error && <Empty>{error}</Empty>}

          {tab === "log" && !error && (
            rows.length === 0 && !loading ? (
              <Empty>No audit entries match.</Empty>
            ) : (
              <>
                <ul className="divide-y divide-paper-rule">
                  {rows.map((r) => (
                    <li key={r.id} className="py-2">
                      <details className="group">
                        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs [&::-webkit-details-marker]:hidden">
                          <Lamp state={actionState(r.status)} />
                          <span className="font-medium text-paper-text">
                            {toolLabel(r.actionType)}
                          </span>
                          <span className="text-[11px] text-paper-faint">{r.agent}</span>
                          <StateChip state={actionState(r.status)}>
                            {actionStatusLabel(r.status)}
                          </StateChip>
                          {r.ctxAmountPaise != null && (
                            <Amount paise={r.ctxAmountPaise} className="text-[11px] text-paper-dim" />
                          )}
                          <time className="ml-auto shrink-0 font-mono text-[11px] text-paper-faint">
                            {istDateTime(r.createdAt)}
                          </time>
                        </summary>
                        <div className="mt-1.5 space-y-1.5 border-l-2 border-paper-rule pl-3 text-xs">
                          {r.ctxOrderId && (
                            <p className="text-paper-faint">
                              order <span className="font-mono">{shortId(r.ctxOrderId)}</span>
                              {r.compliance.decision && (
                                <>
                                  {" · "}
                                  <span
                                    className={
                                      r.compliance.decision === "BLOCKED"
                                        ? "text-state-blocked"
                                        : "text-paper-dim"
                                    }
                                  >
                                    {r.compliance.decision}
                                    {r.compliance.ruleName ? ` · ${r.compliance.ruleName}` : ""}
                                  </span>
                                </>
                              )}
                              {r.costPaise ? (
                                <>
                                  {" · "}
                                  <span className="font-mono text-paper-faint">
                                    ₹{(r.costPaise / 100).toFixed(2)}
                                  </span>
                                </>
                              ) : null}
                            </p>
                          )}
                          {r.reasoning && (
                            <p className="leading-relaxed text-paper-dim italic">{r.reasoning}</p>
                          )}
                          {r.compliance.reason && r.compliance.decision === "BLOCKED" && (
                            <p className="text-state-blocked">{r.compliance.reason}</p>
                          )}
                        </div>
                      </details>
                    </li>
                  ))}
                </ul>
                {rows.length < total && (
                  <button
                    type="button"
                    onClick={loadMore}
                    disabled={loading}
                    className="mt-3 w-full rounded border border-paper-rule py-1.5 text-xs text-paper-dim transition-colors hover:text-paper-text disabled:opacity-50"
                  >
                    {loading ? "Loading…" : `Load more (${total - rows.length} left)`}
                  </button>
                )}
              </>
            )
          )}

          {tab === "breakers" && !error && (
            breakers.length === 0 ? (
              <Empty>No circuit breakers have tripped yet.</Empty>
            ) : (
              <ul className="divide-y divide-paper-rule">
                {breakers.map((b) => (
                  <li key={b.id} className="flex items-center gap-2 py-2 text-xs">
                    <Lamp state="blocked" />
                    <span className="font-medium text-state-blocked">{b.trigger_id}</span>
                    <span className="text-paper-dim">{b.trigger_type.replace(/_/g, " ")}</span>
                    <span className="ml-auto font-mono text-[11px] text-paper-faint">
                      −{b.cancelled_actions} action{b.cancelled_actions === 1 ? "" : "s"}
                    </span>
                    <time className="shrink-0 font-mono text-[11px] text-paper-faint">
                      {istDateTime(b.created_at)}
                    </time>
                  </li>
                ))}
              </ul>
            )
          )}
        </div>
      </aside>
    </>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <label className="flex items-center gap-1 text-[11px] text-paper-faint">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded border border-paper-rule bg-paper-raised px-1.5 py-1 text-xs text-paper-text focus-visible:outline-none"
      >
        <option value="">all</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}
