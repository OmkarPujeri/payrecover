"use client";

import { useEffect, useRef } from "react";
import { useActiveTrace, useStream } from "@/components/StreamProvider";
import { Amount, Empty, Lamp, StateChip } from "@/components/primitives";
import { ist, shortId } from "@/lib/format";
import {
  eventState,
  eventStatusLabel,
  gateMeta,
  toolLabel,
  traceKindLabel,
  traceStepState,
} from "@/lib/states";
import type { TraceStep } from "@/lib/store";

/** One-line summary shown to the right of a node's label. */
function nodeDetail(step: TraceStep): string | null {
  switch (step.kind) {
    case "strategy":
      return [
        toolLabel(step.tool),
        step.confidence != null ? `${Math.round(step.confidence)}% confident` : null,
      ]
        .filter(Boolean)
        .join(" · ");
    case "gate":
      return gateMeta(step.gate?.action).label;
    case "compliance":
      return step.compliance?.decision ?? null;
    case "executed":
      return toolLabel(step.tool);
    default:
      return step.detail ?? null;
  }
}

export function AgentTrace() {
  const { state, setActiveEvent } = useStream();
  const { eventId, steps } = useActiveTrace();
  const event = eventId ? state.feed.find((e) => e.id === eventId) : undefined;

  const railRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    // Follow the newest node as frames arrive.
    railRef.current?.lastElementChild?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
  }, [steps.length]);

  return (
    <div>
      {/* which event we're tracing */}
      {event ? (
        <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
          <Lamp state={eventState(event.recovery_status)} />
          <span className="font-mono text-sm font-semibold text-paper-text">
            {shortId(event.razorpay_order_id)}
          </span>
          <Amount paise={event.amount} className="text-sm text-paper-text" />
          {event.failure_label && (
            <span className="text-xs text-paper-dim">{event.failure_label}</span>
          )}
          <StateChip state={eventState(event.recovery_status)}>
            {eventStatusLabel(event.recovery_status)}
          </StateChip>
          {event.customer_name && (
            <span className="ml-auto text-xs text-paper-faint">
              {event.customer_name}
              {event.payment_method ? ` · ${event.payment_method}` : ""}
            </span>
          )}
        </div>
      ) : (
        <Empty>The live reasoning trace appears here as failures arrive.</Empty>
      )}

      {/* the pipeline rail */}
      {steps.length > 0 && (
        <ol ref={railRef} className="relative ml-1">
          {steps.map((step, i) => {
            const s = traceStepState(step);
            const isLast = i === steps.length - 1;
            const detail = nodeDetail(step);
            return (
              <li key={step.key} className="relative flex gap-3 pb-3 last:pb-0">
                {/* connector + lamp */}
                <div className="flex flex-col items-center">
                  <Lamp state={s} pulse={isLast} size={10} />
                  {!isLast && (
                    <span className="mt-0.5 w-px flex-1 bg-paper-rule" aria-hidden />
                  )}
                </div>
                {/* content */}
                <div className="-mt-0.5 min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[13px] font-medium text-paper-text">
                      {traceKindLabel(step.kind)}
                    </span>
                    <time className="shrink-0 font-mono text-[11px] text-paper-faint">
                      {ist(step.ts)}
                    </time>
                  </div>
                  {detail && (
                    <p className="truncate text-xs text-paper-dim">{detail}</p>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {/* focus hint when tracing an event with no steps yet (post-resync) */}
      {event && steps.length === 0 && (
        <Empty>No live steps captured for this event yet.</Empty>
      )}

      {/* let keyboard users clear focus back to the newest */}
      {eventId && (
        <button
          type="button"
          onClick={() => setActiveEvent(null)}
          className="mt-2 text-[11px] text-paper-faint underline decoration-dotted underline-offset-2 hover:text-paper-dim"
        >
          clear
        </button>
      )}
    </div>
  );
}
