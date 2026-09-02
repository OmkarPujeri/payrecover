"use client";

/**
 * Where the money comes back from, per failure channel - sorted by the server
 * (money recovered, not frequency), so the client never re-sorts and can't
 * disagree with `/api/dashboard/economics`.
 *
 * The ROI column keeps the backend's three-case zero vocabulary intact rather
 * than collapsing it: `∞` (free and working) reads green, `0x` (spent and got
 * nothing) reads red, `N/A` (nothing happened) reads faint. Those are three
 * different stories and the color says which one you're in.
 */

import { Empty } from "@/components/primitives";
import { useStream } from "@/components/StreamProvider";
import { inr, pct } from "@/lib/format";
import type { EconomicsRow } from "@/lib/types";

function roiClass(display: string): string {
  if (display === "N/A") return "text-ink-faint";
  if (display === "0x") return "text-state-blocked";
  return "text-state-done"; // "∞" and every finite multiple
}

function Roi({ row }: { row: EconomicsRow }) {
  return (
    <span className={`font-semibold ${roiClass(row.roi_display)}`}>
      {row.roi_display}
    </span>
  );
}

export function EconomicsTable() {
  const { state } = useStream();
  const eco = state.economics;

  if (!eco || eco.rows.length === 0) {
    return (
      <Empty material="dark">
        {state.everConnected
          ? "No failures yet. Economics land with the first recovery."
          : "Waiting for the first snapshot…"}
      </Empty>
    );
  }

  const total = eco.total;
  const callout = eco.callout;

  return (
    <div>
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-[0.12em] text-ink-faint">
            <th scope="col" className="pb-2 text-left font-medium">
              Channel
            </th>
            <th scope="col" className="pb-2 text-right font-medium">
              Failed
            </th>
            <th scope="col" className="pb-2 text-right font-medium">
              Recovered
            </th>
            <th scope="col" className="pb-2 text-right font-medium">
              Cost
            </th>
            <th scope="col" className="pb-2 text-right font-medium">
              ROI
            </th>
          </tr>
        </thead>
        <tbody className="font-mono tabular-nums">
          {eco.rows.map((r) => (
            <tr key={r.failure_reason} className="border-t border-rule">
              <th
                scope="row"
                className="py-2 pr-2 text-left font-sans font-normal text-ink-text"
                title={r.failure_reason}
              >
                {r.failure_label}
                <span className="ml-1.5 font-mono text-[10px] text-ink-faint">
                  ×{r.count}
                </span>
              </th>
              <td className="py-2 text-right text-ink-dim">{inr(r.failed_paise)}</td>
              <td className="py-2 text-right text-ink-text">{inr(r.recovered_paise)}</td>
              <td className="py-2 text-right text-ink-dim">
                {r.cost_paise > 0 ? inr(r.cost_paise, { paise: true }) : "-"}
              </td>
              <td className="py-2 text-right">
                <Roi row={r} />
              </td>
            </tr>
          ))}

          {/* total - the row the hero counter and this table must agree with */}
          <tr className="border-t-2 border-rule/60">
            <th scope="row" className="py-2 pr-2 text-left font-sans text-ink-text">
              All channels
              <span className="ml-1.5 font-mono text-[10px] text-ink-faint">
                ×{total.count}
              </span>
            </th>
            <td className="py-2 text-right text-ink-dim">{inr(total.failed_paise)}</td>
            <td className="py-2 text-right font-medium text-ink-text">
              {inr(total.recovered_paise)}
            </td>
            <td className="py-2 text-right text-ink-dim">
              {total.cost_paise > 0 ? inr(total.cost_paise, { paise: true }) : "-"}
            </td>
            <td className="py-2 text-right">
              <Roi row={total} />
            </td>
          </tr>
        </tbody>
      </table>

      {callout.zero_cost_recovered_paise > 0 && callout.zero_cost_channels.length > 0 && (
        <p className="mt-3 rounded border border-state-done/30 bg-state-done-wash px-2.5 py-2 text-[11px] leading-relaxed text-state-done">
          <span className="font-mono">{inr(callout.zero_cost_recovered_paise)}</span> recovered
          at zero cost ({pct(callout.share_of_recovered_pct)} of everything recovered) via{" "}
          {callout.zero_cost_channels.join(", ")}
        </p>
      )}
    </div>
  );
}
