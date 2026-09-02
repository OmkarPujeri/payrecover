"use client";

/**
 * The human-in-the-loop queue. Anything the confidence gate routes to review
 * lands here with its full reasoning context, and a human decides: approve,
 * modify the parameters, or skip.
 *
 * Open policy: a *new* arrival (an action_id the page has never seen) opens the
 * modal by itself - the demo's "it appears without a click" beat. Items that
 * were already pending on load seed the seen-set silently, so reloading the
 * page never ambushes the operator; the header badge remains the way back in.
 * Dismissing the modal only silences the *current* queue - the next new
 * arrival opens it again.
 *
 * Three correctness details the design depends on:
 *   - A 200 from approve/modify is a recorded *decision*, not an execution -
 *     `execution.executed` decides what the notice says.
 *   - A blocked modify comes back as ApiError 409 whose `detail` is an object
 *     ({rule_name, reason}); the action is dead on the server, so it leaves the
 *     queue either way.
 *   - The optimistic HITL_REMOVE makes the card vanish instantly; the provider's
 *     refresh() brings back the authoritative queue.
 */

import { useEffect, useRef, useState } from "react";
import { Amount, Lamp } from "@/components/primitives";
import { useStream } from "@/components/StreamProvider";
import { ApiError, approveHITL, modifyHITL, skipHITL } from "@/lib/api";
import { normalizeCompliance } from "@/lib/adapters";
import { complianceState, gateMeta, toolLabel } from "@/lib/states";
import type { HITLResolution } from "@/lib/types";

type Phase = "idle" | "approving" | "modifying" | "skipping";

interface Notice {
  text: string;
  tone: "done" | "blocked";
}

export function HITLModal() {
  const { state, removeHitl, refresh } = useStream();

  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [phase, setPhase] = useState<Phase>("idle");
  const [editing, setEditing] = useState(false);
  const [paramsText, setParamsText] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const queue = state.hitl;
  const item = queue.length ? queue[Math.min(cursor, queue.length - 1)] : null;

  // --- auto-open on a live gate_decided frame that requires a human --------- //
  // Trigger is the *trace*, not the queue: a gate step with requires_human can
  // only come from a live frame, so a page load (queue seeded by the REST
  // snapshot) never ambushes the operator - the header badge is the way back in.
  const seenGatesRef = useRef<Set<string>>(new Set());
  const wantedRef = useRef<string | null>(null); // event awaiting its queue card
  useEffect(() => {
    for (const [eventId, steps] of Object.entries(state.traces)) {
      for (const s of steps) {
        if (s.kind !== "gate" || !s.gate?.requires_human) continue;
        const key = `${eventId}:${s.key}`;
        if (seenGatesRef.current.has(key)) continue;
        seenGatesRef.current.add(key);
        wantedRef.current = eventId; // consumed by the queue effect below
      }
    }
  }, [state.traces]);

  useEffect(() => {
    if (!wantedRef.current) return;
    const idx = state.hitl.findIndex(
      (h) => h.recovery_event_id === wantedRef.current,
    );
    if (idx >= 0) {
      wantedRef.current = null;
      setCursor(idx);
      setNotice(null);
      setOpen(true);
    }
  }, [state.hitl]);

  // --- per-item reset ------------------------------------------------------- //
  useEffect(() => {
    setEditing(false);
    setFormError(null);
  }, [item?.action_id]);

  // --- Esc closes; timer cleanup ------------------------------------------- //
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  useEffect(
    () => () => {
      if (closeTimer.current) clearTimeout(closeTimer.current);
    },
    [],
  );

  if (!open || !item) return null;

  const gate = item.gate;
  const compliance = normalizeCompliance(item.compliance);
  const pos = Math.min(cursor, queue.length - 1) + 1;

  function dismiss() {
    setOpen(false);
    setNotice(null);
  }

  function describe(res: HITLResolution): Notice {
    const exec = res.execution;
    if (!exec) {
      return { text: `${res.status}: parked (${res.reason ?? "no execution"})`, tone: "done" };
    }
    if (exec.executed) {
      return { text: `${res.status}: executed · ${exec.status}`, tone: "done" };
    }
    return {
      text: `${res.status}: not executed (${exec.reason ?? exec.status})`,
      tone: "blocked",
    };
  }

  function afterResolution(res: HITLResolution) {
    setNotice(describe(res));
    removeHitl(item!.action_id); // stays until the refetch confirms
    void refresh();
    if (queue.length <= 1) {
      // nothing behind this one - show the notice, then close
      closeTimer.current = setTimeout(() => {
        setOpen(false);
        setNotice(null);
      }, 1800);
    }
    // else: the item-change effect resets editing state; notice stays visible
  }

  async function resolve(kind: Exclude<Phase, "idle">) {
    if (!item) return;
    setPhase(kind);
    setFormError(null);
    setNotice(null);
    if (closeTimer.current) clearTimeout(closeTimer.current);
    try {
      if (kind === "approving") {
        afterResolution(await approveHITL(item.action_id));
      } else if (kind === "skipping") {
        afterResolution(await skipHITL(item.action_id));
      } else {
        let params: Record<string, unknown>;
        try {
          params = JSON.parse(paramsText || "{}");
        } catch {
          setFormError("Parameters must be valid JSON");
          return;
        }
        afterResolution(await modifyHITL(item.action_id, params));
      }
    } catch (e) {
      if (e instanceof ApiError) {
        const d = e.detail as Record<string, unknown> | string | null;
        const text =
          d && typeof d === "object"
            ? `${String(d.rule_name ?? "Compliance")}: ${String(d.reason ?? d.message ?? e.message)}`
            : e.message;
        setFormError(text);
        if (e.status === 409) {
          // blocked on the server: it's out of the queue for good
          setNotice({ text, tone: "blocked" });
          removeHitl(item.action_id);
          void refresh();
        }
      } else {
        setFormError("Request failed. Is the backend up?");
      }
    } finally {
      setPhase("idle");
    }
  }

  const busy = phase !== "idle";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => e.target === e.currentTarget && dismiss()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Human review required"
        className="flex max-h-[88vh] w-full max-w-xl flex-col overflow-hidden rounded border border-rule bg-ink-raised text-ink-text shadow-panel"
      >
        {/* header */}
        <div className="flex items-center gap-3 border-b border-rule px-4 py-3">
          <Lamp state="waiting" pulse />
          <h2 className="text-sm font-semibold">Human review required</h2>
          {queue.length > 1 && (
            <span className="font-mono text-[11px] text-ink-faint">
              {pos} of {queue.length}
              <button
                type="button"
                onClick={() => setCursor(Math.max(0, pos - 2))}
                disabled={pos <= 1}
                className="ml-2 rounded border border-rule px-1.5 disabled:opacity-40"
                aria-label="Previous item"
              >
                ‹
              </button>
              <button
                type="button"
                onClick={() => setCursor(Math.min(queue.length - 1, pos))}
                disabled={pos >= queue.length}
                className="ml-1 rounded border border-rule px-1.5 disabled:opacity-40"
                aria-label="Next item"
              >
                ›
              </button>
            </span>
          )}
          <button
            type="button"
            onClick={dismiss}
            className="ml-auto rounded px-2 text-ink-dim transition-colors hover:text-ink-text"
            aria-label="Close review queue"
          >
            ✕
          </button>
        </div>

        {/* body */}
        <div className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
          {/* the money */}
          <div>
            <Amount paise={item.amount_paise} className="text-2xl font-semibold" />
            <p className="mt-1 text-xs text-ink-dim">
              {item.customer_name ?? "Unknown customer"} · {item.failure_label ?? "Unclassified failure"}
              {item.customer_contact ? ` · ${item.customer_contact}` : ""}
            </p>
          </div>

          {/* proposed action */}
          <div className="rounded border border-rule bg-ink px-3 py-2.5">
            <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint">
              Proposed action
            </p>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <span className="font-medium text-ink-text">{toolLabel(item.proposed_action)}</span>
              {item.confidence !== null && (
                <span className="font-mono text-ink-dim">{item.confidence}% confident</span>
              )}
              {item.recoverability_score !== null && (
                <span className="font-mono text-ink-faint">
                  recoverability {item.recoverability_score}
                </span>
              )}
            </div>
            {item.diagnostic_summary && (
              <p className="mt-2 text-xs leading-relaxed text-ink-dim">{item.diagnostic_summary}</p>
            )}
            {item.reasoning && (
              <p className="mt-2 border-l-2 border-rule pl-2.5 text-xs leading-relaxed text-ink-dim italic">
                {item.reasoning}
              </p>
            )}
          </div>

          {/* gate + compliance */}
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div className="rounded border border-rule bg-ink px-3 py-2">
              <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint">
                Confidence gate
              </p>
              <p className="text-ink-text">
                {gate ? gateMeta(gate.action).label : "-"}
                {gate?.tier ? <span className="text-ink-faint"> · {gate.tier}</span> : null}
              </p>
              {gate?.reason && (
                <p className="mt-1 leading-relaxed text-ink-dim">{gate.reason}</p>
              )}
            </div>
            <div className="rounded border border-rule bg-ink px-3 py-2">
              <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint">
                Compliance
              </p>
              <p
                className={
                  complianceState(compliance.decision) === "blocked"
                    ? "text-state-blocked"
                    : "text-state-done"
                }
              >
                {compliance.decision ?? "-"}
              </p>
              {compliance.reason && (
                <p className="mt-1 leading-relaxed text-ink-dim">{compliance.reason}</p>
              )}
            </div>
          </div>

          {/* risk factors */}
          {(item.risk_factors.length > 0 || item.uncertainty_factors.length > 0) && (
            <div className="text-xs">
              {item.risk_factors.length > 0 && (
                <p className="text-ink-dim">
                  <span className="font-medium text-state-waiting">Risk:</span>{" "}
                  {item.risk_factors.join(" · ")}
                </p>
              )}
              {item.uncertainty_factors.length > 0 && (
                <p className="mt-1 text-ink-dim">
                  <span className="font-medium text-ink-faint">Uncertainty:</span>{" "}
                  {item.uncertainty_factors.join(" · ")}
                </p>
              )}
            </div>
          )}

          {/* modify editor */}
          {editing && (
            <div>
              <label
                htmlFor="hitl-params"
                className="mb-1 block text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint"
              >
                Action parameters (JSON). Merges over the proposal; compliance re-runs on submit.
              </label>
              <textarea
                id="hitl-params"
                value={paramsText}
                onChange={(e) => setParamsText(e.target.value)}
                rows={6}
                spellCheck={false}
                className="w-full rounded border border-rule bg-ink px-3 py-2 font-mono text-xs text-ink-text focus-visible:outline-none"
              />
            </div>
          )}
        </div>

        {/* footer */}
        <div className="border-t border-rule px-4 py-3">
          {formError && (
            <p className="mb-2 text-xs text-state-blocked" role="alert">
              {formError}
            </p>
          )}
          {notice && (
            <p
              className={`mb-2 font-mono text-xs ${
                notice.tone === "blocked" ? "text-state-blocked" : "text-state-done"
              }`}
              aria-live="polite"
            >
              {notice.text}
            </p>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => void resolve("skipping")}
              disabled={busy}
              className="rounded border border-rule px-3 py-1.5 text-xs text-ink-dim transition-colors hover:text-ink-text disabled:opacity-50"
            >
              {phase === "skipping" ? "Skipping…" : "Skip"}
            </button>
            <button
              type="button"
              onClick={() => {
                if (editing) {
                  void resolve("modifying");
                } else {
                  setParamsText(JSON.stringify(item.proposed_params ?? {}, null, 2));
                  setEditing(true);
                }
              }}
              disabled={busy}
              className="rounded border border-state-running/40 bg-state-running-wash px-3 py-1.5 text-xs font-medium text-state-running transition-colors hover:bg-state-running/20 disabled:opacity-50"
            >
              {phase === "modifying" ? "Submitting…" : editing ? "Submit changes" : "Modify"}
            </button>
            <button
              type="button"
              onClick={() => void resolve("approving")}
              disabled={busy || editing}
              autoFocus
              className="rounded border border-state-done/40 bg-state-done-wash px-3 py-1.5 text-xs font-medium text-state-done transition-colors hover:bg-state-done/20 disabled:opacity-50"
            >
              {phase === "approving" ? "Approving…" : "Approve"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
