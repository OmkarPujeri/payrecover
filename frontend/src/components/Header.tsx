"use client";

import { useStream } from "@/components/StreamProvider";
import { relativeTime } from "@/lib/format";
import type { ConnStatus } from "@/lib/store";

const CONN: Record<
  ConnStatus,
  { label: string; color: string; pulse: boolean }
> = {
  connecting: { label: "Connecting", color: "var(--color-state-waiting)", pulse: true },
  open: { label: "Live", color: "var(--color-state-done)", pulse: true },
  reconnecting: { label: "Reconnecting", color: "var(--color-state-waiting)", pulse: true },
  closed: { label: "Offline", color: "var(--color-state-blocked)", pulse: false },
};

export function Header({ onOpenAudit }: { onOpenAudit: () => void }) {
  const { state, setActiveEvent } = useStream();
  const conn = CONN[state.connection];
  const queued = state.hitl.length;

  return (
    <header className="flex items-center justify-between gap-4 border-b border-rule bg-ink px-5 py-3">
      {/* wordmark */}
      <div className="flex items-baseline gap-3">
        <div className="flex items-center gap-2">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ background: "var(--color-state-done)" }}
          />
          <span className="text-[17px] font-bold tracking-tight text-ink-text">
            PayRecover
          </span>
        </div>
        <span className="hidden text-xs text-ink-faint sm:inline">
          Autonomous payment recovery, on the record
        </span>
      </div>

      {/* live state */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onOpenAudit}
          className="rounded border border-rule bg-ink-raised px-2.5 py-1 text-xs font-medium text-ink-dim transition-colors hover:border-state-done/40 hover:text-ink-text"
        >
          Audit log
        </button>

        {queued > 0 && (
          <button
            type="button"
            onClick={() =>
              setActiveEvent(state.hitl[0]?.recovery_event_id ?? null)
            }
            className="flex items-center gap-2 rounded border border-state-waiting/40 bg-state-waiting-wash px-2.5 py-1 text-xs font-medium text-state-waiting transition-colors hover:bg-state-waiting/20"
          >
            <span className="font-mono text-[13px] font-semibold">{queued}</span>
            awaiting review
          </button>
        )}

        <div className="flex items-center gap-2 rounded border border-rule bg-ink-raised px-2.5 py-1">
          <span
            aria-hidden
            className={`inline-block h-2 w-2 rounded-full ${conn.pulse ? "lamp-pulse" : ""}`}
            style={{ background: conn.color }}
          />
          <span
            className="text-xs font-medium"
            style={{ color: conn.color }}
            role="status"
            aria-live="polite"
          >
            {conn.label}
          </span>
          {state.lastFrameAt && state.connection === "open" && (
            <span className="ml-1 hidden font-mono text-[11px] text-ink-faint md:inline">
              {relativeTime(state.lastFrameAt)}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
