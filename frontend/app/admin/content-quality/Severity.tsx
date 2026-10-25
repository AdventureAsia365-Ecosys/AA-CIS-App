"use client";
// app/admin/content-quality/Severity.tsx — AA-924 PR2. The per-violation severity badge and finding list of the
// Content Quality drawers. Severity rules: lib/gateSeverity.ts (findings when the entry has them, else the entry's
// own `blocking`).

import { A, mono } from "../_components/adminUi";
import { Badge } from "../../_kit";
import { SEVERITY_LABEL, SEVERITY_TONE, findingsOf, type LedgerEntryLike, type Severity } from "@/lib/gateSeverity";
import { gateLabel } from "./shared";

/** "blocking" (danger) or "warning" (warning) — the kit Badge, no dot. */
export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span data-testid="cq-severity" data-severity={severity} style={{ flexShrink: 0, display: "inline-flex" }}>
      <Badge tone={SEVERITY_TONE[severity]} dot={false}>{SEVERITY_LABEL[severity]}</Badge>
    </span>
  );
}

/** The source gate inside a merged entry ("found by Cliché / banned phrase (retired) · F2_banned_patterns"). */
export function SourceNote({ source }: { source: string | null | undefined }) {
  if (!source) return null;
  return (
    <span style={{ fontSize: 10.5, color: A.muted }} title={source}>
      found by {gateLabel(source)} <span style={{ fontFamily: mono }}>{source}</span>
    </span>
  );
}

/** One line per finding of a failed ledger entry: severity badge, text, and the source gate when it differs. */
export function FindingList({ entry }: { entry: LedgerEntryLike }) {
  const findings = findingsOf(entry);
  if (findings.length === 0) return <span style={{ color: A.muted }}>failed (no detail stored)</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {findings.map((f, j) => (
        <div key={j} data-testid="cq-finding" style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap", minWidth: 0 }}>
          <SeverityBadge severity={f.severity} />
          <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{f.text}</span>
          <SourceNote source={f.source} />
        </div>
      ))}
    </div>
  );
}
