"use client";
// app/admin/platform/compliance/ObligationsTab.tsx — AA-1174: the Obligations tab of Platform › Compliance.
//
// The fixed note ("readiness, not legal advice"), the applicability matrix (kit DataTable: one row per market x data
// segment, one column per law; each cell a verdict badge, a Counsel badge and, when the verdict is old, a Review due
// badge; the test, threshold, primary source and assessment date in the cell's info tip) and one Section per law with a
// checklist:
// Ref, Obligation, AA's role, State (with "partly"), Counsel, Evidence (link, date, state, document status) and the
// controls. Laws are never a percentage. The law filter lives in the URL (?law=<key>).
//
// API: GET /api/admin/platform/compliance/obligations (compliance.read; api/services/compliance_obligations.py). Pure
// helpers in lib/compliance.ts (pivotApplicability, safeSourceLink, tones). Kit only; test ids on wrapper spans.

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, FileText } from "lucide-react";
import { useMemo } from "react";
import {
  Badge, DataTable, EmptyState, ErrorState, Field, InfoTip, K, Section, Select, TableSkeleton, apiGet, mono,
  useFormat, useT, useUrlFilters, type ColumnDef,
} from "../../../_kit";
import {
  OBLIGATIONS_PATH, checkTone, lawsWithItems, obligationTone, pivotApplicability, safeLink, safeSourceLink,
  sortObligations, verdictTone,
  type ApplicabilityRow, type LawSummary, type ObligationItem, type ObligationsResp,
} from "@/lib/compliance";
import { COMPLIANCE } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const grid = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 } as const;
const linkStyle = { color: K.accentDeep, display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12 } as const;

function useCT() {
  return useT(COMPLIANCE);
}
type CT = ReturnType<typeof useCT>;

function SourceLink({ href, label }: { href: string | null | undefined; label: string }) {
  const safe = safeSourceLink(href);
  if (!safe) return null;
  return <a href={safe} target="_blank" rel="noopener noreferrer" style={linkStyle}>{label}<ExternalLink size={11} /></a>;
}

function lawLabel(laws: LawSummary[], key: string): string {
  return laws.find((l) => l.framework === key)?.title ?? key;
}

function MatrixCell({ row, t, reviewDays }: { row: ApplicabilityRow | undefined; t: CT; reviewDays: number }) {
  const { formatDate } = useFormat();
  if (!row) return <span style={small}>—</span>;
  const tip = [`${t("matrixTest")}: ${row.test}`, row.threshold ? `${t("matrixThreshold")}: ${row.threshold}` : null,
    `${t("matrixSource")}: ${row.source_title}`, t("matrixAssessed", formatDate(row.assessed_on)),
    row.note].filter(Boolean).join("\n");
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
      <Badge tone={verdictTone(row.verdict)}>{t(`verdict_${row.verdict}` as Parameters<CT>[0]) as string}</Badge>
      {row.counsel_review && <span title={t("counselTip")}><Badge tone="purple" dot={false}>{t("counsel")}</Badge></span>}
      {row.review_due && <span title={t("reviewDueTip", reviewDays)}>
        <Badge tone="warning" dot={false}>{t("reviewDue")}</Badge></span>}
      <InfoTip text={tip} testId={`matrix-tip-${row.market}-${row.segment}-${row.framework}`} />
      <SourceLink href={row.source_url} label={t("matrixSource")} />
    </span>
  );
}

type PivotRow = ReturnType<typeof pivotApplicability>["rows"][number];

function Matrix({ data, t }: { data: ObligationsResp; t: CT }) {
  const pivot = useMemo(() => pivotApplicability(data.applicability), [data.applicability]);
  const reviewDays = data.settings.applicability_review_max_age_days;
  const columns: ColumnDef<PivotRow, unknown>[] = [
    { id: "market", header: t("colMarket"), accessorKey: "market",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{r.market}</span> },
    { id: "segment", header: t("colSegment"), accessorKey: "segment",
      cell: ({ row: { original: r } }) => <span style={small}>
        {t(`segment_${r.segment}` as Parameters<CT>[0]) as string}</span> },
    ...pivot.laws.map((l): ColumnDef<PivotRow, unknown> => ({
      id: `law-${l}`, header: lawLabel(data.laws, l), enableSorting: false,
      cell: ({ row: { original: r } }) => <MatrixCell row={r.cells[l]} t={t} reviewDays={reviewDays} />,
    })),
  ];
  return (
    <Section title={t("matrixTitle")} description={t("matrixDescription")} testId="compliance-matrix">
      <DataTable<PivotRow> tableId="compliance-matrix" data={pivot.rows} columns={columns} getRowId={(r) => r.key}
        paginate={false} searchable={false} tableMinWidth={260 + pivot.laws.length * 200}
        emptyTitle={t("matrixEmpty")} />
    </Section>
  );
}

function EvidenceCell({ item, t }: { item: ObligationItem; t: CT }) {
  const { formatDate } = useFormat();
  const doc = safeLink(item.doc_url);
  return (
    <span style={{ display: "grid", gap: 4 }}>
      {item.evidence.map((e) => {
        const href = safeLink(e.link);
        return (
          <span key={e.check_id} style={{ display: "inline-flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
            <Badge tone={checkTone(e.state)}>{t(`state_${e.state}` as Parameters<CT>[0]) as string}</Badge>
            <span style={{ ...small, fontFamily: mono }}>{e.check_id}</span>
            {e.collected_at && <span style={small}>{formatDate(e.collected_at)}</span>}
            {e.doc_status && <span style={small}>{t("docStatus", e.doc_status)}</span>}
            {href && <a href={href} target="_blank" rel="noopener noreferrer" style={linkStyle}>
              {t("open")}<ExternalLink size={11} /></a>}
          </span>
        );
      })}
      {!item.evidence.length && <span style={small}>{t("noAutoCheck")}</span>}
      {doc && <a href={doc} target="_blank" rel="noopener noreferrer" style={linkStyle}>
        <FileText size={11} />{t("docLink")}</a>}
    </span>
  );
}

function LawSection({ law, items, t }: { law: LawSummary; items: ObligationItem[]; t: CT }) {
  const { formatDate } = useFormat();
  const c = law.counts;
  const columns: ColumnDef<ObligationItem, unknown>[] = [
    { id: "ref", header: t("colRef"), accessorKey: "ref",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{r.ref}</span> },
    { id: "title", header: t("colObligation"), accessorKey: "title",
      cell: ({ row: { original: r } }) => (
        <span style={{ display: "grid", gap: 2 }}>
          <span>{r.title}</span>
          <SourceLink href={r.source_url} label={t("officialText")} />
        </span>
      ) },
    { id: "role", header: t("colRole"), accessorKey: "applies_as",
      cell: ({ row: { original: r } }) => <span style={small}>
        {r.applies_as ? t(`role_${r.applies_as}` as Parameters<CT>[0]) as string : "—"}</span> },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: ({ row: { original: r } }) => (
        <span data-testid={`obligation-state-${r.framework}-${r.ref}`}>
          <Badge tone={obligationTone(r.state)}>
            {t(`ob_${r.state}` as Parameters<CT>[0]) as string}{r.partly ? ` (${t("partly")})` : ""}
          </Badge>
        </span>
      ) },
    { id: "counsel", header: t("counsel"), accessorKey: "counsel_review",
      cell: ({ row: { original: r } }) => r.counsel_review
        ? <span title={t("counselTip")}><Badge tone="purple" dot={false}>{t("counsel")}</Badge></span>
        : <span style={small}>—</span> },
    { id: "evidence", header: t("colEvidence"), cell: ({ row: { original: r } }) => <EvidenceCell item={r} t={t} /> },
    { id: "controls", header: t("colControls"),
      cell: ({ row: { original: r } }) => <span style={{ ...small, fontFamily: mono }}>
        {r.controls.map((x) => x.control_id).join(", ") || "—"}</span> },
  ];
  return (
    <Section
      title={law.title}
      testId={`compliance-law-${law.framework}`}
      description={
        <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          {law.citation && <span>{law.citation}</span>}
          {law.in_force_on && <span>{t("inForce", formatDate(law.in_force_on))}</span>}
          {law.applies_from && law.applies_from !== law.in_force_on
            && <span>{t("appliesFrom", formatDate(law.applies_from))}</span>}
          <SourceLink href={law.source_url} label={t("officialText")} />
          <span>{t("lawCounts", c.met, c.not_met, c.not_checked, c.counsel_review)}</span>
        </span>
      }>
      <DataTable<ObligationItem> tableId={`compliance-law-${law.framework}`} data={sortObligations(items)}
        columns={columns} getRowId={(r) => `${r.framework}|${r.ref}`} paginate={false} searchable={false}
        tableMinWidth={1040} emptyTitle={t("lawsEmpty")} />
    </Section>
  );
}

export function ObligationsTab() {
  const t = useCT();
  const { get, setMany } = useUrlFilters();
  const law = get("law") || "all";
  const q = useQuery({ queryKey: ["platform-compliance", "obligations"], retry: false, staleTime: 60_000,
    queryFn: () => apiGet<ObligationsResp>(OBLIGATIONS_PATH) });
  const laws = useMemo(() => lawsWithItems(q.data?.laws ?? []), [q.data]);
  const shown = law === "all" ? laws : laws.filter((l) => l.framework === law);
  return (
    <div style={grid}>
      <div role="note" data-testid="compliance-obligations-note" style={{ ...small, display: "grid", gap: 4 }}>
        <span><Badge tone="warning">{t("notLegalAdvice")}</Badge></span>
        <span>{t("obligationsIntro")}</span>
      </div>
      {q.error && <ErrorState message={`${t("obligationsLoadError")}: ${(q.error as Error).message}`}
        onRetry={() => void q.refetch()} />}
      {q.isLoading && <TableSkeleton rows={6} cols={5} />}
      {q.data && (
        <>
          <Matrix data={q.data} t={t} />
          {!laws.length ? <EmptyState title={t("lawsEmpty")} /> : (
            <>
              <div style={{ maxWidth: 320 }}>
                <Field label={t("filterLaw")}>
                  <Select value={law} onChange={(e) => setMany({ law: e.target.value === "all" ? null : e.target.value })}
                    options={[{ value: "all", label: t("allLaws") },
                      ...laws.map((l) => ({ value: l.framework, label: l.title }))]} />
                </Field>
              </div>
              {shown.map((l) => (
                <LawSection key={l.framework} law={l} t={t}
                  items={q.data.items.filter((i) => i.framework === l.framework)} />
              ))}
            </>
          )}
        </>
      )}
    </div>
  );
}
