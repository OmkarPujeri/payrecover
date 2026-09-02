"use client";

import { useStream } from "@/components/StreamProvider";
import { Amount, Empty, Lamp, StateChip } from "@/components/primitives";
import { relativeTime, shortId } from "@/lib/format";
import { eventState, eventStatusLabel } from "@/lib/states";

export function RecoveryFeed({
  onOpenJourney,
}: {
  /** Opens the journey sheet for an event (page owns the open state). */
  onOpenJourney: (eventId: string) => void;
}) {
  const { state, setActiveEvent } = useStream();
  const { feed, activeEventId } = state;

  if (feed.length === 0) {
    return <Empty>Failed payments and their recoveries stream in here.</Empty>;
  }

  return (
    <ul className="divide-y divide-paper-rule">
      {feed.map((e) => {
        const s = eventState(e.recovery_status);
        const active = e.id === activeEventId;
        const recovered = e.recovery_status === "recovered";
        return (
          <li key={e.id}>
            <button
              type="button"
              onClick={() => {
                setActiveEvent(e.id);
                onOpenJourney(e.id);
              }}
              aria-pressed={active}
              className={`flex w-full items-center gap-3 px-1 py-2 text-left transition-colors ${
                active ? "bg-state-running/10" : "hover:bg-paper"
              }`}
            >
              <Lamp state={s} pulse={s === "running"} />
              <span className="font-mono text-xs font-medium text-paper-text">
                {shortId(e.razorpay_order_id)}
              </span>
              <Amount paise={e.amount} className="text-xs text-paper-text" />
              <span className="min-w-0 flex-1 truncate text-xs text-paper-dim">
                {e.failure_label ?? e.error_reason ?? "-"}
              </span>
              {recovered ? (
                <StateChip state="done">
                  <Amount paise={e.recovered_amount} className="text-[11px]" />
                </StateChip>
              ) : (
                <StateChip state={s}>{eventStatusLabel(e.recovery_status)}</StateChip>
              )}
              <time className="w-12 shrink-0 text-right font-mono text-[11px] text-paper-faint">
                {relativeTime(e.created_at)}
              </time>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
