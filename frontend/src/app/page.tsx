"use client";

import { useState } from "react";
import { Header } from "@/components/Header";
import { AgentTrace } from "@/components/AgentTrace";
import { ChainOfThought } from "@/components/ChainOfThought";
import { RecoveryFeed } from "@/components/RecoveryFeed";
import { ComplianceBand } from "@/components/ComplianceBand";
import { HITLModal } from "@/components/HITLModal";
import { JourneyTimeline } from "@/components/JourneyTimeline";
import { AuditDrawer } from "@/components/AuditDrawer";
import { MetricsCards } from "@/components/MetricsCards";
import { ChaosPresets } from "@/components/ChaosPresets";
import { EconomicsTable } from "@/components/EconomicsTable";
import { Card } from "@/components/primitives";

/*
 * The command center. A full-height column: the console top-bar, then the
 * 35/65 split that is the product's central structural idea -
 *   left  = CONTROLS, the dark console (the machine you operate)
 *   right = LIVE INTELLIGENCE, the light ledger (the record it produces)
 * The seam between them is a real material change, drawn as a single rule.
 *
 * The compliance band lands below the trace in #18; until then its slot shows
 * an honest "waiting" state so the shell is never visibly broken.
 */
export default function Page() {
  // The journey sheet's open state lives here: feed rows open it, the sheet
  // itself closes (Esc / ✕ / backdrop). The audit drawer hangs off the header.
  const [journeyId, setJourneyId] = useState<string | null>(null);
  const [auditOpen, setAuditOpen] = useState(false);

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <Header onOpenAudit={() => setAuditOpen(true)} />
      <main className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[35fr_65fr]">
        <ConsolePane />
        <LedgerPane onOpenJourney={setJourneyId} />
      </main>
      <HITLModal />
      <JourneyTimeline eventId={journeyId} onClose={() => setJourneyId(null)} />
      <AuditDrawer open={auditOpen} onClose={() => setAuditOpen(false)} />
    </div>
  );
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-current opacity-55">
      {children}
    </p>
  );
}

/* ------------------------------------------------------------------ console */
function ConsolePane() {
  return (
    <section
      aria-label="Controls"
      className="scroll-quiet min-h-0 space-y-4 overflow-y-auto border-b border-rule bg-ink px-5 py-5 text-ink-text lg:border-b-0 lg:border-r"
    >
      <Eyebrow>Controls</Eyebrow>

      <Card material="dark" title="Recovery metrics">
        <MetricsCards />
      </Card>

      <Card material="dark" title="Scenario presets">
        <ChaosPresets />
      </Card>

      <Card material="dark" title="Recovery economics">
        <EconomicsTable />
      </Card>
    </section>
  );
}

/* ------------------------------------------------------------------- ledger */
function LedgerPane({
  onOpenJourney,
}: {
  onOpenJourney: (eventId: string) => void;
}) {
  return (
    <section
      aria-label="Live intelligence"
      className="scroll-quiet min-h-0 space-y-4 overflow-y-auto bg-paper px-5 py-5 text-paper-text"
    >
      <Eyebrow>Live intelligence</Eyebrow>

      <Card title="Agent trace">
        <AgentTrace />
      </Card>

      <Card title="Chain of thought">
        <ChainOfThought />
      </Card>

      {/* ComplianceBand → #18 */}
      <Card title="IST compliance band">
        <ComplianceBand />
      </Card>

      <Card title="Recovery feed">
        <RecoveryFeed onOpenJourney={onOpenJourney} />
      </Card>
    </section>
  );
}
