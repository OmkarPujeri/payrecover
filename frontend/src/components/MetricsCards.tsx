"use client";

/**
 * The hero metrics. A recovered-₹ counter that animates toward its target -
 * the closing beat of the demo ("watch it climb") - over a quiet progress rule
 * showing recovery by value, plus the four figures the demo speaks out loud:
 * rate, average time to recovery, spend, and volume.
 *
 * Pure render off `state.metrics`. The provider owns keeping that authoritative
 * (the reducer never does money math); the counter only *displays* a number it
 * is handed, so it can never disagree with the API by more than one animation.
 *
 * Reduced motion: the counter snaps instead of counting up (the pulse already
 * degrades the same way, in globals.css).
 */

import { useEffect, useRef, useState } from "react";
import { Empty } from "@/components/primitives";
import { useStream } from "@/components/StreamProvider";
import { inr, pct } from "@/lib/format";

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

/**
 * Animate a paise figure from its previous value to `target`. First target
 * counts up from 0 - that is the point of the hero. Returns the value to
 * render; `null` target freezes the last one so a failed refetch never
 * animates the number back down to zero.
 */
function useAnimatedPaise(target: number | null, duration = 900): number {
  const [shown, setShown] = useState(0);
  const fromRef = useRef(0);

  useEffect(() => {
    if (target === null) return;
    if (window.matchMedia(REDUCED_MOTION).matches) {
      fromRef.current = target;
      setShown(target);
      return;
    }

    const from = fromRef.current;
    let raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / duration);
      const eased = 1 - Math.pow(1 - k, 3); // ease-out cubic: fast rise, soft landing
      setShown(Math.round(from + (target - from) * eased));
      if (k < 1) raf = requestAnimationFrame(tick);
      else fromRef.current = target;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);

  return shown;
}

export function MetricsCards() {
  const { state } = useStream();
  const m = state.metrics;
  const recovered = useAnimatedPaise(m ? m.recovered_amount_paise : null);

  if (!m) {
    return (
      <Empty material="dark">
        {state.everConnected
          ? "No failures tracked yet. Run a preset to begin."
          : "Waiting for the first snapshot…"}
      </Empty>
    );
  }

  const rate = m.recovery_rate_by_amount_pct;

  return (
    <div>
      {/* hero counter */}
      <div className="mb-4">
        <p className="mb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-faint">
          Recovered
        </p>
        <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
          <span className="font-mono text-[28px] font-semibold leading-none tracking-tight text-ink-text">
            {inr(recovered)}
          </span>
          <span className="text-xs text-ink-faint">
            of {inr(m.failed_amount_paise)} failed
          </span>
        </div>

        {/* progress rule - one green fill on a hairline track, nothing fancier */}
        <div
          className="mt-3 h-1 overflow-hidden rounded bg-rule"
          role="progressbar"
          aria-valuenow={Math.round(rate)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Recovery rate by amount"
        >
          <div
            className="h-full rounded bg-state-done transition-[width] duration-700 ease-out"
            style={{ width: `${Math.min(100, Math.max(0, rate))}%` }}
          />
        </div>
        <p className="mt-1.5 font-mono text-[11px] text-ink-dim">
          {pct(rate)} by value · {m.recovered_count} of {m.total_events} payments
        </p>
      </div>

      {/* the four figures */}
      <div className="grid grid-cols-2 gap-2">
        <Tile label="Avg time to recover">
          {m.avg_recovery_hours === null ? (
            <span className="text-ink-faint">-</span>
          ) : (
            <>
              {m.avg_recovery_hours.toFixed(1)}
              <Unit>h</Unit>
            </>
          )}
        </Tile>
        <Tile label="Recovery spend">
          {m.recovery_cost_paise > 0 ? (
            inr(m.recovery_cost_paise, { paise: true })
          ) : (
            <span className="text-state-done">free</span>
          )}
        </Tile>
        <Tile label="Recovery rate (count)">{pct(m.recovery_rate_by_count_pct)}</Tile>
        <Tile label="Failures tracked">
          {m.total_events}
          <Unit>events</Unit>
        </Tile>
      </div>
    </div>
  );
}

/** One quiet stat tile - a notch darker than the card it sits on. */
function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded border border-rule bg-ink px-3 py-2.5">
      <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint">
        {label}
      </p>
      <p className="font-mono text-sm tabular-nums text-ink-text">{children}</p>
    </div>
  );
}

function Unit({ children }: { children: React.ReactNode }) {
  return <span className="ml-1 text-[11px] text-ink-faint">{children}</span>;
}
