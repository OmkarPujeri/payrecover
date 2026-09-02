"use client";

/**
 * The demo controls. The button row is driven off `GET /api/simulator/presets`
 * - the *server's* registry, not a client copy - so `cascade_failure` renders
 * disabled with its `unavailable_reason` until phase 5c flips one flag, and a
 * preset added later appears here with no frontend edit.
 *
 * Running a preset is a demo beat: the button holds a running lamp while the
 * backend walks its script, then a one-line receipt lands underneath (injected,
 * fired, needs-a-human, breakers) and the provider's `refresh()` re-pulls every
 * aggregate so the metrics above and the ledger beside agree with what ran.
 */

import { useEffect, useState } from "react";
import { Empty, Lamp } from "@/components/primitives";
import { useStream } from "@/components/StreamProvider";
import { ApiError, getPresets, runBatch, runChaos } from "@/lib/api";
import type { ChaosRun, PresetSummary, RunBatch } from "@/lib/types";

const MAX_BATCH = 200;

/** The one-line receipt under the buttons - the beat the operator reads out. */
interface Receipt {
  title: string;
  injected: number;
  fired: number | null; // null: run-batch doesn't report fired actions
  humans: number;
  breakers: number;
}

export function ChaosPresets() {
  const { refresh } = useStream();
  const [presets, setPresets] = useState<PresetSummary[] | null>(null);
  const [loadError, setLoadError] = useState(false);

  /** The running control: a preset id, "batch", or null. */
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);

  const [batchCount, setBatchCount] = useState(100);

  useEffect(() => {
    let alive = true;
    getPresets()
      .then((p) => alive && setPresets(p.presets))
      .catch(() => alive && setLoadError(true));
    return () => {
      alive = false;
    };
  }, []);

  async function firePreset(p: PresetSummary) {
    setRunning(p.preset);
    setError(null);
    try {
      const run: ChaosRun = await runChaos(p.preset);
      setReceipt({
        title: p.name,
        injected: run.injected,
        fired: run.actions_fired,
        humans: run.events.filter((e) => e.requires_human).length,
        breakers: run.breakers_tripped.length,
      });
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Preset failed");
    } finally {
      setRunning(null);
    }
  }

  async function fireBatch() {
    const count = Math.min(MAX_BATCH, Math.max(1, Math.round(batchCount) || 0));
    setRunning("batch");
    setError(null);
    try {
      const run: RunBatch = await runBatch(count);
      setReceipt({
        title: `Weighted batch ×${run.injected}`,
        injected: run.injected,
        fired: null,
        humans: run.requires_human,
        breakers: 0,
      });
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Batch failed");
    } finally {
      setRunning(null);
    }
  }

  if (loadError) {
    return (
      <Empty material="dark">Cannot reach the preset registry. Is the backend up?</Empty>
    );
  }
  if (!presets) {
    return <Empty material="dark">Loading preset registry…</Empty>;
  }

  const busy = running !== null;

  return (
    <div>
      <div className="grid grid-cols-2 gap-2">
        {presets.map((p) => {
          const isRunning = running === p.preset;
          return (
            <button
              key={p.preset}
              type="button"
              disabled={!p.available || busy}
              onClick={() => void firePreset(p)}
              title={
                p.available
                  ? p.description
                  : `${p.description} (${p.unavailable_reason ?? "not yet available"})`
              }
              className={`flex min-h-[52px] flex-col justify-center gap-0.5 rounded border px-3 py-2 text-left transition-colors ${
                p.available
                  ? "border-rule bg-ink text-ink-text hover:border-state-running/50 enabled:hover:bg-ink-raised"
                  : "cursor-not-allowed border-rule/60 bg-ink/60 text-ink-faint"
              } disabled:cursor-wait`}
            >
              <span className="flex items-center gap-1.5 text-xs font-medium">
                {isRunning && <Lamp state="running" pulse size={7} />}
                {p.name}
                {!p.available && (
                  <span className="ml-auto text-[9px] uppercase tracking-wider text-ink-faint">
                    soon
                  </span>
                )}
              </span>
              <span className="font-mono text-[10px] text-ink-faint">
                {p.available ? `${p.event_count} failure${p.event_count === 1 ? "" : "s"}` : p.unavailable_reason}
              </span>
            </button>
          );
        })}
      </div>

      {/* weighted batch - the volume control */}
      <div className="mt-3 flex items-center gap-2 rounded border border-rule bg-ink px-3 py-2">
        <label htmlFor="batch-count" className="text-xs text-ink-dim">
          Weighted batch
        </label>
        <input
          id="batch-count"
          type="number"
          min={1}
          max={MAX_BATCH}
          value={batchCount}
          onChange={(e) => setBatchCount(Number(e.target.value))}
          disabled={busy}
          className="w-16 rounded border border-rule bg-ink-raised px-2 py-1 text-right font-mono text-xs tabular-nums text-ink-text focus-visible:outline-none disabled:opacity-50"
        />
        <button
          type="button"
          onClick={() => void fireBatch()}
          disabled={busy}
          className="ml-auto flex items-center gap-1.5 rounded border border-rule px-2.5 py-1 text-xs font-medium text-ink-text transition-colors hover:border-state-running/50 disabled:cursor-wait disabled:opacity-50"
        >
          {running === "batch" && <Lamp state="running" pulse size={7} />}
          {running === "batch" ? "Running…" : "Run"}
        </button>
      </div>

      {receipt && (
        <p className="mt-3 font-mono text-[11px] leading-relaxed text-ink-dim" aria-live="polite">
          <span className="text-ink-text">{receipt.title}</span> · {receipt.injected} injected
          {receipt.fired !== null && <> · {receipt.fired} fired</>}
          {receipt.humans > 0 && (
            <>
              {" · "}
              <span className="text-state-waiting">{receipt.humans} for human review</span>
            </>
          )}
          {receipt.breakers > 0 && (
            <>
              {" · "}
              <span className="text-state-blocked">
                {receipt.breakers} breaker trip{receipt.breakers === 1 ? "" : "s"}
              </span>
            </>
          )}
        </p>
      )}

      {error && (
        <p className="mt-2 text-[11px] text-state-blocked" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
