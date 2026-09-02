"use client";

/**
 * One event's complete story, as a right-side sheet over the ledger - the feed
 * keeps streaming behind it, because "judges never switch tabs" (plan §2).
 *
 * Data is `GET /api/dashboard/events/{id}`: the event, every action with its
 * reasoning/compliance/cost, and the breaker events that cancelled anything.
 * Actions and breakers merge into one chronological rail - the journey reads
 * failure → decisions → breaker → recovery, in the order it actually happened.
 *
 * While the sheet is open it refetches whenever the live feed copy of the event
 * changes (status, attempts, recovered amount), so a breaker firing mid-read
 * appears without a manual refresh. Raw rows go through
 * `adapters.dashboardActionToClient`; nothing renders a wire shape directly.
 */

import { useEffect, useState } from "react";
import { Amount, Empty, Lamp, StateChip } from "@/components/primitives";
import { useStream } from "@/components/StreamProvider";
import { getEventDetail, ApiError } from "@/lib/api";
import { dashboardActionToClient } from "@/lib/adapters";
import { istDateTime, parseInstant, shortId } from "@/lib/format";
import { actionState, actionStatusLabel, complianceState, eventState, eventStatusLabel, toolLabel } from "@/lib/states";
import type { CircuitBreakerEventDTO, EventDetailDTO } from "@/lib/types";
import type { ClientAction } from "@/lib/types";

type Entry =
  | { kind: "action"; at: string | null; action: ClientAction }
  | { kind: "breaker"; at: string | null; breaker: CircuitBreakerEventDTO };

export function JourneyTimeline({
  eventId,
  onClose,
}: {
  eventId: string | null;
  onClose: () => void;
}) {
  const { state } = useStream();
  // The live feed copy drives refetches while the sheet is open.
  const live = eventId ? state.feed.find((e) => e.id === eventId) : undefined;

  const [detail, setDetail] = useState<EventDetailDTO | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!eventId) {
      setDetail(null);
      setError(null);
      return;
    }
    let alive = true;
    getEventDetail(eventId)
      .then((d) => alive && (setDetail(d), setError(null)))
      .catch((e) =>
        alive &&
        setError(e instanceof ApiError ? e.message : "Cannot load the journey"),
      );
    return () => {
      alive = false;
    };
  }, [eventId, live?.recovery_status, live?.recovery_attempts, live?.recovered_amount]);

  // Esc closes.
  useEffect(() => {
    if (!eventId) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [eventId, onClose]);

  if (!eventId) return null;

  const event = detail;
  const entries = event ? buildEntries(event) : [];

  return (
    <>
      {/* click-catcher - visible focus stays on the sheet, the page behind dims */}
      <div
        className="fixed inset-0 z-40 bg-black/25"
        onClick={onClose}
        aria-hidden
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Recovery journey"
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-paper-rule bg-paper text-paper-text shadow-sheet"
      >
        {/* header */}
        <div className="flex items-center gap-3 border-b border-paper-rule px-4 py-3">
          <h2 className="text-sm font-semibold">Recovery journey</h2>
          {event && (
            <span className="font-mono text-xs text-paper-faint">
              {shortId(event.razorpay_order_id)}
            </span>
          )}
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded px-2 text-paper-dim transition-colors hover:text-paper-text"
            aria-label="Close journey"
          >
            ✕
          </button>
        </div>

        {/* body */}
        <div className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
          {error && <Empty>{error}</Empty>}
          {!error && !event && <Empty>Loading the journey…</Empty>}

          {event && (
            <>
              {/* the payment */}
              <div>
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <Amount paise={event.amount} className="text-xl font-semibold" />
                  <StateChip state={eventState(event.recovery_status)}>
                    {eventStatusLabel(event.recovery_status)}
                  </StateChip>
                  {event.recovered_amount > 0 && (
                    <Amount
                      paise={event.recovered_amount}
                      className="text-xs text-state-done"
                    />
                  )}
                </div>
                <p className="mt-1 text-xs text-paper-dim">
                  {event.customer_name ?? "Unknown"} ·{" "}
                  {event.payment_method ?? "-"} ·{" "}
                  {event.failure_label ?? event.error_reason ?? "Unclassified"}
                </p>
              </div>

              {/* facts */}
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 rounded border border-paper-rule bg-paper-raised px-3 py-2.5 text-xs">
                <Fact label="Error">
                  {event.error_code ?? "-"}
                  {event.error_source ? ` (${event.error_source})` : ""}
                </Fact>
                <Fact label="Recoverability">
                  {event.recoverability_score ?? "-"}
                </Fact>
                <Fact label="Attempts">{event.recovery_attempts}</Fact>
                <Fact label="Recovery cost">
                  {event.recovery_cost_paise > 0
                    ? `${(event.recovery_cost_paise / 100).toFixed(2)}`
                    : "₹0"}
                </Fact>
                <Fact label="DND">{event.customer_dnd ? "yes" : "no"}</Fact>
                <Fact label="Failed at">{istDateTime(event.created_at)}</Fact>
              </dl>

              {/* the rail */}
              {entries.length === 0 ? (
                <Empty>No actions recorded for this failure yet.</Empty>
              ) : (
                <ol className="relative ml-1">
                  {entries.map((entry, i) => {
                    const isLast = i === entries.length - 1;
                    return (
                      <li key={keyOf(entry)} className="relative flex gap-3 pb-3 last:pb-0">
                        <div className="flex flex-col items-center">
                          <Lamp
                            state={entry.kind === "breaker" ? "blocked" : actionState(entry.action.status)}
                            size={10}
                          />
                          {!isLast && (
                            <span className="mt-0.5 w-px flex-1 bg-paper-rule" aria-hidden />
                          )}
                        </div>
                        <div className="-mt-0.5 min-w-0 flex-1">
                          {entry.kind === "breaker" ? (
                            <BreakerNode breaker={entry.breaker} />
                          ) : (
                            <ActionNode action={entry.action} />
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ol>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  );
}

/* ------------------------------------------------------------------ nodes */

function ActionNode({ action }: { action: ClientAction }) {
  const comp = action.compliance;
  const blocked = comp.decision === "BLOCKED";
  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
        <span className="text-[13px] font-medium text-paper-text">
          {toolLabel(action.actionType)}
          <span className="ml-1.5 text-[11px] font-normal text-paper-faint">
            {action.agent}
          </span>
        </span>
        <time className="shrink-0 font-mono text-[11px] text-paper-faint">
          {istDateTime(action.executedAt ?? action.scheduledAt ?? action.createdAt)}
        </time>
      </div>

      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
        <StateChip state={actionState(action.status)}>
          {actionStatusLabel(action.status)}
        </StateChip>
        {action.confidence != null && (
          <span className="font-mono text-paper-faint">{Math.round(action.confidence)}%</span>
        )}
        {action.costPaise != null && action.costPaise > 0 && (
          <span className="font-mono text-paper-faint">
            ₹{(action.costPaise / 100).toFixed(2)}
          </span>
        )}
        {comp.decision && (
          <span
            className={
              blocked ? "font-medium text-state-blocked" : "text-paper-dim"
            }
          >
            {comp.decision}
            {comp.ruleName ? ` · ${comp.ruleName}` : ""}
          </span>
        )}
      </div>

      {action.reasoning && (
        <p className="mt-1 border-l-2 border-paper-rule pl-2.5 text-xs leading-relaxed text-paper-dim italic">
          {action.reasoning}
        </p>
      )}
      {comp.reason && blocked && (
        <p className="mt-1 text-xs text-state-blocked">{comp.reason}</p>
      )}
    </div>
  );
}

function BreakerNode({ breaker }: { breaker: CircuitBreakerEventDTO }) {
  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-2">
        <span className="text-[13px] font-medium text-state-blocked">
          {breaker.trigger_id} · circuit breaker
        </span>
        <time className="shrink-0 font-mono text-[11px] text-paper-faint">
          {istDateTime(breaker.created_at)}
        </time>
      </div>
      <p className="mt-0.5 text-xs text-paper-dim">
        {breaker.trigger_type.replace(/_/g, " ")}: cancelled{" "}
        {breaker.cancelled_actions} action
        {breaker.cancelled_actions === 1 ? "" : "s"}
      </p>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[10px] font-medium uppercase tracking-[0.12em] text-paper-faint">
        {label}
      </dt>
      <dd className="mt-0.5 font-mono text-paper-text">{children}</dd>
    </div>
  );
}

/* ------------------------------------------------------------------ assembly */

function buildEntries(event: EventDetailDTO): Entry[] {
  const entries: Entry[] = event.actions.map((a) => ({
    kind: "action" as const,
    at: a.executed_at ?? a.scheduled_at ?? a.created_at ?? null,
    action: dashboardActionToClient(a, event.id),
  }));
  for (const b of event.circuit_breaker_events) {
    entries.push({ kind: "breaker", at: b.created_at, breaker: b });
  }
  entries.sort((x, y) => stamp(x.at) - stamp(y.at));
  return entries;
}

function stamp(at: string | null): number {
  const d = parseInstant(at);
  return d ? d.getTime() : 0;
}

function keyOf(entry: Entry): string {
  return entry.kind === "action" ? entry.action.id : entry.breaker.id;
}
