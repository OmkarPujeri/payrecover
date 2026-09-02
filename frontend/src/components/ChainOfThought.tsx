"use client";

import { useActiveTrace } from "@/components/StreamProvider";
import { Empty, StateChip } from "@/components/primitives";
import { complianceState, gateMeta, toolLabel } from "@/lib/states";
import type { TraceStep } from "@/lib/store";

function lastOf(steps: TraceStep[], kind: TraceStep["kind"]): TraceStep | undefined {
  for (let i = steps.length - 1; i >= 0; i--) if (steps[i].kind === kind) return steps[i];
  return undefined;
}

/** Neutral fact chips - deliberately NOT one of the five state colors, which are
 *  reserved for status. Used for risk / uncertainty factors. */
function Factors({ label, items }: { label: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mt-1.5">
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-paper-faint">
        {label}
      </p>
      <div className="flex flex-wrap gap-1">
        {items.map((f, i) => (
          <span
            key={i}
            className="rounded border border-paper-rule bg-paper px-1.5 py-0.5 text-[11px] text-paper-dim"
          >
            {f}
          </span>
        ))}
      </div>
    </div>
  );
}

function Block({
  heading,
  children,
}: {
  heading: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="border-t border-paper-rule pt-2.5 first:border-t-0 first:pt-0">
      <div className="mb-1 flex items-center gap-2">{heading}</div>
      {children}
    </div>
  );
}

export function ChainOfThought() {
  const { eventId, steps } = useActiveTrace();
  const strategy = lastOf(steps, "strategy");
  const gate = lastOf(steps, "gate");
  const compliance = lastOf(steps, "compliance");

  if (!eventId || (!strategy && !gate && !compliance)) {
    return <Empty>Agent reasoning shows here: the diagnosis, the chosen tool, and the compliance verdict.</Empty>;
  }

  return (
    <div className="space-y-3 text-sm">
      {strategy && (
        <Block
          heading={
            <>
              <span className="text-[11px] font-semibold uppercase tracking-wider text-paper-dim">
                Strategy
              </span>
              <span className="font-mono text-xs font-semibold text-paper-text">
                {toolLabel(strategy.tool)}
              </span>
              {strategy.confidence != null && (
                <span className="font-mono text-xs text-paper-faint">
                  {Math.round(strategy.confidence)}% confident
                </span>
              )}
              {strategy.source && (
                <span className="ml-auto text-[11px] text-paper-faint">
                  {strategy.source === "llm" ? "LLM" : strategy.source}
                </span>
              )}
            </>
          }
        >
          {strategy.detail && <p className="text-xs text-paper-dim">{strategy.detail}</p>}
          <Factors label="Risk factors" items={strategy.riskFactors} />
          <Factors label="Uncertainty" items={strategy.uncertaintyFactors} />
        </Block>
      )}

      {gate && (
        <Block
          heading={
            <>
              <span className="text-[11px] font-semibold uppercase tracking-wider text-paper-dim">
                Confidence gate
              </span>
              <StateChip state={gateMeta(gate.gate?.action).state}>
                {gateMeta(gate.gate?.action).label}
              </StateChip>
              {gate.gate?.tier && (
                <span className="ml-auto text-[11px] text-paper-faint">
                  tier: {gate.gate.tier}
                </span>
              )}
            </>
          }
        >
          {gate.detail && <p className="text-xs text-paper-dim">{gate.detail}</p>}
        </Block>
      )}

      {compliance && (
        <Block
          heading={
            <>
              <span className="text-[11px] font-semibold uppercase tracking-wider text-paper-dim">
                Compliance engine
              </span>
              <StateChip state={complianceState(compliance.compliance?.decision)}>
                {compliance.compliance?.decision ?? "-"}
              </StateChip>
              <span className="ml-auto text-[10px] italic text-paper-faint">
                deterministic
              </span>
            </>
          }
        >
          {compliance.compliance?.ruleName && (
            <p className="font-mono text-[11px] text-paper-text">
              {compliance.compliance.ruleId
                ? `${compliance.compliance.ruleId} · `
                : ""}
              {compliance.compliance.ruleName}
            </p>
          )}
          {compliance.compliance?.reason && (
            <p className="text-xs text-paper-dim">{compliance.compliance.reason}</p>
          )}
        </Block>
      )}
    </div>
  );
}
