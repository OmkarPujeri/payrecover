"use client";

/**
 * The signature element: a 24-hour horizontal band in IST that makes the
 * product's time-reasoning watchable instead of buried in JSON. Three layers,
 * all absolutely-positioned divs on one track (no chart library, per the PRD):
 *
 *   1. Regions - the TRAI legal messaging window (09:00–20:00) and the two
 *      NPCI peak windows (10:00–13:00, 17:00–21:30) where retries are shifted
 *      out. Same windows the compliance engine enforces (NPCI-002 / CB-008);
 *      constants here mirror `app/compliance/engine.py` and
 *      `app/execution/circuit_breakers.py`.
 *   2. Ticks - one per scheduled action from `GET /api/actions/scheduled`,
 *      plotted at its fire time. A CB-008 deferral keeps a ghost tick at the
 *      illegal slot, a connector line, and a live tick at `deferred_to` that
 *      *animates* across the band when the refresh moves it - the moment the
 *      plan calls the band's payoff.
 *   3. Now - a hairline at the current IST instant, re-read each minute.
 *
 * Clicking a tick focuses that event's trace (the journey sheet takes over
 * when it lands in #19).
 */

import { useEffect, useMemo, useState } from "react";
import { Empty } from "@/components/primitives";
import { useStream } from "@/components/StreamProvider";
import { ist, istHourOfDay } from "@/lib/format";
import { toolLabel } from "@/lib/states";
import type { ScheduledAction } from "@/lib/types";

// Compliance-engine constants, mirrored. IST hours-of-day on a 0–24 scale.
const TRAI_START = 9;
const TRAI_END = 20;
const NPCI_PEAKS: Array<[number, number]> = [
  [10, 13],
  [17, 21.5],
];

const pctOf = (hour: number) => `${(hour / 24) * 100}%`;

/** Hour rules on the scale - every 4h, edges included. */
const HOURS = [0, 4, 8, 12, 16, 20, 24];

/** Half-hour buckets - retries legitimately collide (the engine targets the
 *  same legal slots), so ticks stack by count rather than overlapping. */
const BUCKET = 0.5;

interface Tick {
  key: string;
  bucketHour: number;
  count: number;
  action: ScheduledAction; // representative (lowest attempt) for the tooltip/focus
  deferred: boolean;
}

export function ComplianceBand() {
  const { state, setActiveEvent } = useStream();
  const scheduled = state.scheduled;

  // Re-read "now" every minute; the band is static otherwise.
  const [nowHour, setNowHour] = useState<number | null>(null);
  useEffect(() => {
    const read = () => setNowHour(istHourOfDay(new Date().toISOString()));
    read();
    const t = setInterval(read, 60_000);
    return () => clearInterval(t);
  }, []);

  const ticks = useMemo<Tick[]>(() => {
    const byBucket = new Map<number, Tick>();
    for (const a of scheduled) {
      // A deferral plots at its legal destination; anything else at its slot.
      const when = a.deferred_to ?? a.scheduled_at;
      const hour = istHourOfDay(when);
      if (hour === null) continue;
      const bucketHour = Math.floor(hour / BUCKET) * BUCKET;
      const existing = byBucket.get(bucketHour);
      if (existing) {
        existing.count += 1;
        if ((a.attempt ?? 0) < (existing.action.attempt ?? 0)) existing.action = a;
      } else {
        byBucket.set(bucketHour, {
          key: a.action_id,
          bucketHour,
          count: 1,
          action: a,
          deferred: a.deferred_to !== null,
        });
      }
    }
    // Earliest bucket first so DOM order matches reading order.
    return [...byBucket.values()].sort((x, y) => x.bucketHour - y.bucketHour);
  }, [scheduled]);

  if (scheduled.length === 0) {
    return (
      <Empty>
        Nothing scheduled. A retry planned by the agent plots here, shifted out
        of NPCI peak hours and inside the TRAI window.
      </Empty>
    );
  }

  return (
    <div>
      {/* the track */}
      <div className="relative h-16 select-none">
        {/* TRAI legal window - labeled in place; the band explains itself */}
        <div
          aria-hidden
          className="absolute inset-y-0 border-x border-state-done/30 bg-state-done-wash"
          style={{ left: pctOf(TRAI_START), width: pctOf(TRAI_END - TRAI_START) }}
        >
          <span className="absolute inset-x-0 top-0.5 text-center text-[9px] font-semibold tracking-wide text-state-done">
            TRAI legal window
          </span>
        </div>
        {/* NPCI peak windows */}
        {NPCI_PEAKS.map(([start, end]) => (
          <div
            key={start}
            aria-hidden
            className="absolute inset-y-0 bg-state-waiting-wash"
            style={{
              left: pctOf(start),
              width: pctOf(end - start),
              backgroundImage:
                "repeating-linear-gradient(45deg, transparent 0 4px, rgb(201 138 46 / 0.12) 4px 8px)",
            }}
          >
            <span className="absolute inset-x-0 top-0.5 text-center text-[9px] font-semibold tracking-wide text-state-waiting">
              NPCI peak
            </span>
          </div>
        ))}

        {/* hour rules + labels live below the track; hairlines here */}
        {[6, 12, 18].map((h) => (
          <div
            key={h}
            aria-hidden
            className="absolute inset-y-0 w-px bg-paper-rule"
            style={{ left: pctOf(h) }}
          />
        ))}

        {/* now */}
        {nowHour !== null && (
          <div
            aria-hidden
            className="absolute inset-y-0 z-10 w-px bg-paper-text/60"
            style={{ left: pctOf(nowHour) }}
          >
            <span className="absolute -top-0.5 left-1 font-mono text-[9px] text-paper-faint">
              now
            </span>
          </div>
        )}

        {/* ticks */}
        {ticks.map((t) => {
          const a = t.action;
          const when = a.deferred_to ?? a.scheduled_at;
          const label = `${toolLabel(a.action_type)}${a.attempt ? ` · attempt ${a.attempt}` : ""} · ${ist(when)} IST${a.deferred_reason ? ` · ${a.deferred_reason}` : ""}${t.count > 1 ? ` · ${t.count} stacked` : ""}`;
          return (
            <button
              key={t.bucketHour}
              type="button"
              onClick={() => setActiveEvent(a.recovery_event_id)}
              title={label}
              aria-label={label}
              className="group absolute bottom-1 top-3 z-20 -translate-x-1/2 transition-[left] duration-700 ease-out"
              style={{ left: pctOf(t.bucketHour + BUCKET / 2) }}
            >
              {/* the tick - taller and saturated with stack depth */}
              <span
                className={`block w-[3px] rounded-full transition-colors ${
                  t.deferred ? "bg-state-running" : "bg-state-waiting"
                }`}
                style={{
                  height: `${Math.min(100, 55 + (t.count - 1) * 15)}%`,
                  opacity: t.count > 1 ? 1 : 0.85,
                }}
              />
              {t.count > 1 && (
                <span className="absolute -top-1 left-1/2 -translate-x-1/2 font-mono text-[9px] font-semibold text-paper-dim">
                  {t.count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* hour scale - edge labels clamp inside so 00 and 24 don't clip */}
      <div className="relative mt-1 h-4 font-mono text-[9px] text-paper-faint">
        {HOURS.map((h) => (
          <span
            key={h}
            className="absolute"
            style={{
              left: pctOf(h),
              transform:
                h === 0
                  ? "none"
                  : h === 24
                    ? "translateX(-100%)"
                    : "translateX(-50%)",
            }}
          >
            {String(h).padStart(2, "0")}
          </span>
        ))}
      </div>

      {/* legend - one item per line-group, swatch first, so it reads at a glance */}
      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[10px] text-paper-dim">
        <span className="inline-flex items-center gap-2">
          <LegendSwatch className="bg-state-done-wash border-x border-state-done/30" />
          Notifications legal (09–20)
        </span>
        <span className="inline-flex items-center gap-2">
          <LegendSwatch className="bg-state-waiting-wash" hatch />
          NPCI peak: retries shifted out
        </span>
        <span className="inline-flex items-center gap-2">
          <span className="inline-block h-3 w-[3px] rounded-full bg-state-waiting" />
          Scheduled retry (number = stacked)
        </span>
        <span className="inline-flex items-center gap-2">
          <span className="inline-block h-3 w-[3px] rounded-full bg-state-running" />
          Deferred to a legal slot
        </span>
      </div>
    </div>
  );
}

function LegendSwatch({ className, hatch = false }: { className: string; hatch?: boolean }) {
  return (
    <span
      aria-hidden
      className={`inline-block h-3 w-5 rounded-sm ${className}`}
      style={
        hatch
          ? {
              backgroundImage:
                "repeating-linear-gradient(45deg, transparent 0 4px, rgb(201 138 46 / 0.12) 4px 8px)",
            }
          : undefined
      }
    />
  );
}
