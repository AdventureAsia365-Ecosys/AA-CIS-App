"use client";
// app/admin/lab/eval-runs/EvalRuns.tsx — AA-1169: Writing Lab › Eval runs (/admin/lab/eval-runs).
//
// Three tabs, read-only and $0 (no model call, no write):
//   Eval runs         the writing-memory evaluation runs (filters: writer, kind, pre-registration id; server pages)
//                     -> one run (?run=): its numbers, its arms and its golden samples (verdicts, flagged claims
//                     per policy class, hard stops) -> one sample (?item=): the source the judge read beside every
//                     row's stored output, the flagged claims marked in the text and listed with their class, field
//                     and reason, the deterministic gate replay, the stored checks and the row's lineage on demand.
//                     Export one sample, or a whole run, as Markdown (the page saves no note: no endpoint stores one).
//   Lineage coverage  GET /api/admin/quality/lineage/coverage: per workflow the share of recent outputs that carry
//                     each lineage field.
//   Look up           GET /api/admin/quality/lineage/{workflow}/{id} (one output's versions and model calls) and
//                     GET /api/admin/quality/lineage/fanout/{tour_id} (one tour's source-change marks).
// API through the BFF (/api/admin/writing-memory/eval-runs/*: lib/rbac.ts API_READ_RULES writing-memory; the backend
// re-checks: lab.read for the lists, tenant_content.read or lab.tenant_text.read, logged, for one sample).
// Every string comes from messages.ts (English by default, Vietnamese when picked).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, RefreshCw } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, EmptyState, ErrorState, Field, Input, K, KpiTile, PageFrame, PageHeader, Section, Select,
  TableSkeleton, Tabs, apiGet, mono, useFormat, useT, useToast, type ColumnDef,
} from "../../../_kit";
import type { Translator } from "../../../_kit/core/i18n";
import {
  COVERAGE_DAYS, EVAL_RUNS_PATH, EVAL_WRITERS, LINEAGE_WORKFLOWS, RUN_KINDS, armRow, classLine, coverageUrl,
  evalRunHref, fanoutUrl, highlightSegments, isUuid, itemMarkdown, itemUrl, lineageUrl, markdownFilename,
  parseEvalTab, pct, resultTone, runMarkdown, runTone, runUrl, runsUrl, usd,
  type CoverageView, type EvalRun, type EvalTab, type ItemResult, type ItemView, type LineageWorkflow,
  type ObjectLineage, type RunFilters, type RunItem, type RunsPage, type RunView, type TourFanout,
} from "@/lib/evalRuns";
import { ER } from "./messages";

type T = Translator<typeof ER.en>;

const code = (v: React.ReactNode) => <span style={{ fontFamily: mono, fontSize: 12 }}>{v ?? "—"}</span>;
const grid: React.CSSProperties = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 };
const tiles: React.CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 170px), 1fr))", gap: 12,
};
// Side by side on a wide screen (source | candidate | control), stacked on a phone.
const sideBySide: React.CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 380px), 1fr))", gap: 16,
  alignItems: "start",
};
const textBox: React.CSSProperties = {
  whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 13, lineHeight: 1.55, color: K.body,
};
const PAGE_SIZE = 25;

function downloadMarkdown(filename: string, text: string): void {
  if (typeof window === "undefined") return;
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function setTabParam(value: EvalTab): void {
  try {
    const url = new URL(window.location.href);
    if (value === "runs") url.searchParams.delete("tab");
    else url.searchParams.set("tab", value);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    /* the address bar is a convenience */
  }
}

export default function EvalRuns() {
  const t = useT(ER);
  const qc = useQueryClient();
  const params = useSearchParams();
  const runId = params.get("run");
  const itemId = params.get("item");
  const [tab, setTabState] = useState<EvalTab>(() => parseEvalTab(params.get("tab")));
  const setTab = (k: string) => {
    const next = parseEvalTab(k);
    setTabState(next);
    setTabParam(next);
  };
  const tabs = [
    { key: "runs", label: t("tabRuns") }, { key: "coverage", label: t("tabCoverage") },
    { key: "lookup", label: t("tabLookup") },
  ];
  return (
    <PageFrame>
      <PageHeader title={t("title")}
        breadcrumbs={[{ label: t("crumbGroup"), href: "/admin/lab" }, { label: t("title"), href: EVAL_RUNS_PATH }]}
        description={t("description")}
        actions={
          <Button variant="ghost" size="sm" onClick={() => qc.invalidateQueries({ queryKey: ["eval-runs"] })}>
            <RefreshCw size={14} /> {t("refresh")}</Button>
        } />
      <div style={{ marginBottom: 14 }} data-testid="eval-runs-tabs">
        <Tabs tabs={tabs} active={tab} onChange={setTab} variant="underline" />
      </div>
      {tab === "runs" && (
        isUuid(runId) && isUuid(itemId) ? <ItemPanel runId={runId!} itemId={itemId!} t={t} />
          : isUuid(runId) ? <RunPanel runId={runId!} t={t} />
          : <RunsList t={t} />
      )}
      {tab === "coverage" && <CoverageTab t={t} />}
      {tab === "lookup" && <LookupTab t={t} />}
    </PageFrame>
  );
}

// ── runs ────────────────────────────────────────────────────────────────────────────────────────────────────────

function RunsList({ t }: { t: T }) {
  const router = useRouter();
  const { formatDateTime } = useFormat();
  const [filters, setFilters] = useState<RunFilters>({});
  const [prereg, setPrereg] = useState("");
  const [page, setPage] = useState({ index: 0, size: PAGE_SIZE });
  const q = useQuery({
    queryKey: ["eval-runs", "list", filters, page],
    queryFn: () => apiGet<RunsPage>(runsUrl(filters, page.size, page.index * page.size)),
    staleTime: 30_000,
  });
  const set = (f: RunFilters) => { setFilters(f); setPage((p) => ({ ...p, index: 0 })); };
  const cols: ColumnDef<EvalRun, unknown>[] = useMemo(() => [
    { accessorKey: "created_at", header: t("colCreated"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDateTime(c.row.original.created_at)}</span> },
    { accessorKey: "prereg_id", header: t("colPrereg"),
      cell: (c) => c.row.original.prereg_id
        ? code(`${c.row.original.prereg_id}${c.row.original.prereg_arm ? ` · ${c.row.original.prereg_arm}` : ""}`)
        : "—" },
    { accessorKey: "writer", header: t("colWriter"), cell: (c) => code(c.row.original.writer) },
    { accessorKey: "run_kind", header: t("colKind"), cell: (c) => code(c.row.original.run_kind) },
    { accessorKey: "verdict", header: t("colVerdict"),
      cell: (c) => <Badge tone={runTone(c.row.original.verdict)}>{c.row.original.verdict ?? "—"}</Badge> },
    { id: "results", header: t("colResults"),
      cell: (c) => <span style={{ fontSize: 12 }}>
        {t("resultsLine", c.row.original.passes, c.row.original.fails, c.row.original.errors)}</span> },
    { accessorKey: "cost_usd", header: t("colCost"), cell: (c) => code(usd(c.row.original.cost_usd)) },
    { accessorKey: "stop_reason", header: t("colStop"), cell: (c) => code(c.row.original.stop_reason) },
  ], [t, formatDateTime]);
  return (
    <Section title={t("runsTitle")} description={t("runsHelp")} padded={false} testId="eval-runs-list">
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", padding: "12px 16px", alignItems: "flex-end" }}>
        <Field label={t("filterWriter")}>
          <Select value={filters.writer ?? ""} placeholder={t("anyWriter")}
            options={EVAL_WRITERS.map((w) => ({ value: w, label: w }))}
            onChange={(e) => set({ ...filters, writer: e.target.value || undefined })} />
        </Field>
        <Field label={t("filterKind")}>
          <Select value={filters.run_kind ?? ""} placeholder={t("anyKind")}
            options={RUN_KINDS.map((k) => ({ value: k, label: k }))}
            onChange={(e) => set({ ...filters, run_kind: e.target.value || undefined })} />
        </Field>
        <Field label={t("filterPrereg")} hint={t("filterPreregHint")}>
          <form style={{ display: "flex", gap: 8 }}
            onSubmit={(e) => { e.preventDefault(); set({ ...filters, prereg: prereg.trim() || undefined }); }}>
            <Input value={prereg} maxLength={64} onChange={(e) => setPrereg(e.target.value)}
              style={{ fontFamily: mono, minWidth: 0, width: 180 }} />
            <Button type="submit" size="sm" variant="secondary">{t("show")}</Button>
          </form>
        </Field>
        {(filters.writer || filters.run_kind || filters.prereg) && (
          <Button size="sm" variant="ghost" onClick={() => { setPrereg(""); set({}); }}>{t("clear")}</Button>
        )}
      </div>
      {q.error ? <ErrorState message={(q.error as Error).message || t("loadError")} onRetry={() => q.refetch()} /> : (
        <DataTable tableId="eval-runs" data={q.data?.runs ?? []} columns={cols} loading={q.isLoading}
          getRowId={(r) => r.run_id} compact searchable={false} tableMinWidth={860}
          emptyTitle={t("noRuns")}
          onRowClick={(r) => router.push(evalRunHref(r.run_id))}
          serverPagination={{ pageIndex: page.index, pageSize: page.size, total: q.data?.total ?? 0,
            onChange: (index, size) => setPage({ index, size }) }} />
      )}
    </Section>
  );
}

function RunPanel({ runId, t }: { runId: string; t: T }) {
  const router = useRouter();
  const toast = useToast();
  const { formatDateTime } = useFormat();
  const [progress, setProgress] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["eval-runs", "run", runId],
    queryFn: () => apiGet<RunView>(runUrl(runId)),
    staleTime: 30_000,
  });
  const exportRun = async (v: RunView) => {
    const items: ItemView[] = [];
    try {
      for (let i = 0; i < v.items.length; i++) {
        setProgress(t("exporting", i + 1, v.items.length));
        items.push(await apiGet<ItemView>(itemUrl(runId, v.items[i].item_id)));
      }
      downloadMarkdown(markdownFilename(v.run), runMarkdown(v.run, items));
      toast.success(t("exportDone"));
    } catch {
      toast.error(t("exportFailed"));
    } finally {
      setProgress(null);
    }
  };
  const cols: ColumnDef<RunItem, unknown>[] = useMemo(() => {
    return [
      { accessorKey: "sample_id", header: t("colSample"), cell: (c) => code(c.row.original.sample_id) },
      { accessorKey: "split", header: t("colSplit"), cell: (c) => code(c.row.original.split) },
      { accessorKey: "label_verdict", header: t("colLabel"),
        cell: (c) => <Badge tone={resultTone(c.row.original.label_verdict)}>{c.row.original.label_verdict}</Badge> },
      { id: "candidate", header: t("colCandidate"), cell: (c) => <ArmCell item={c.row.original} which="candidate" t={t} /> },
      { id: "control", header: t("colControl"), cell: (c) => <ArmCell item={c.row.original} which="control" t={t} /> },
      { id: "classes", header: t("colClasses"),
        cell: (c) => code(classLine(armRow(c.row.original, "candidate")?.claim_classes ?? {}) || "—") },
    ];
  }, [t]);
  if (q.isLoading) return <TableSkeleton rows={6} cols={5} />;
  if (q.error || !q.data) {
    return <ErrorState message={(q.error as Error | null)?.message || t("loadError")} onRetry={() => q.refetch()} />;
  }
  const { run, items, summary } = q.data;
  const arms = Object.entries(summary);
  return (
    <div style={grid} data-testid="eval-run-ready">
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "space-between" }}>
        <Button size="sm" variant="ghost" onClick={() => router.push(EVAL_RUNS_PATH)}>
          <ArrowLeft size={14} /> {t("backToRuns")}</Button>
        <Button size="sm" variant="secondary" disabled={!!progress || !items.length} onClick={() => exportRun(q.data!)}>
          <Download size={14} /> {progress ?? t("exportRun")}</Button>
      </div>
      <Section title={t("runTitle", run.prereg_id ?? run.run_id.slice(0, 8))}
        description={<>{code(run.run_id)} · {formatDateTime(run.created_at)}</>}>
        <div style={tiles}>
          <KpiTile size="sm" label={t("kpiKind")} value={`${run.run_kind} · ${run.writer ?? "—"}`} />
          <KpiTile size="sm" label={t("kpiVerdict")} value={run.verdict ?? "—"} hint={run.stop_reason ?? undefined} />
          <KpiTile size="sm" label={t("kpiCost")} value={usd(run.cost_usd)}
            hint={run.cost_cap_usd != null ? `cap ${usd(run.cost_cap_usd)}` : undefined} />
          <KpiTile size="sm" label={t("kpiResults")} value={run.results}
            hint={t("resultsLine", run.passes, run.fails, run.errors)} />
          <KpiTile size="sm" label={t("kpiOutputs")} value={run.outputs} />
        </div>
      </Section>
      <Section title={t("armsTitle")} description={t("armsHelp")} padded={false}>
        <DataTable tableId="eval-run-arms" data={arms} getRowId={([a]) => a} compact searchable={false}
          paginate={false} columns={[
            { id: "arm", header: t("colArm"), cell: (c) => code(c.row.original[0]) },
            { id: "rows", header: t("colRows"), cell: (c) => c.row.original[1].rows },
            { id: "judged", header: t("colJudged"), cell: (c) => c.row.original[1].judged },
            { id: "passes", header: t("colPasses"), cell: (c) => c.row.original[1].passes },
            { id: "share", header: t("colPassShare"), cell: (c) => pct(c.row.original[1].pass_share) },
            { id: "claims", header: t("colClaims"), cell: (c) => c.row.original[1].claims },
            { id: "stops", header: t("colHardStops"), cell: (c) => c.row.original[1].hard_stops },
          ]} />
      </Section>
      <Section title={t("itemsTitle")} description={t("itemsHelp")} padded={false} testId="eval-run-items">
        <DataTable tableId="eval-run-items" data={items} columns={cols} getRowId={(i) => i.item_id} compact
          searchable={false} paginate={false} tableMinWidth={760}
          onRowClick={(i) => router.push(evalRunHref(runId, i.item_id))} />
      </Section>
    </div>
  );
}

function ArmCell({ item, which, t }: { item: RunItem; which: "candidate" | "control"; t: T }) {
  const r = armRow(item, which);
  if (!r) return <>—</>;
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      <Badge tone={resultTone(r.verdict)}>{r.verdict}</Badge>
      <span style={{ fontSize: 12, color: K.muted }}>{t("claimsCount", r.claims)}</span>
      {r.hard_stop && <Badge tone="danger">{t("hardStop")}</Badge>}
    </span>
  );
}

// ── one sample ──────────────────────────────────────────────────────────────────────────────────────────────────

function ItemPanel({ runId, itemId, t }: { runId: string; itemId: string; t: T }) {
  const router = useRouter();
  const q = useQuery({
    queryKey: ["eval-runs", "item", runId, itemId],
    queryFn: () => apiGet<ItemView>(itemUrl(runId, itemId)),
    staleTime: 60_000,
  });
  if (q.isLoading) return <TableSkeleton rows={8} cols={3} />;
  if (q.error || !q.data) {
    return <ErrorState message={(q.error as Error | null)?.message || t("loadError")} onRetry={() => q.refetch()} />;
  }
  const v = q.data;
  return (
    <div style={grid} data-testid="eval-item-ready">
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "space-between" }}>
        <Button size="sm" variant="ghost" onClick={() => router.push(evalRunHref(runId))}>
          <ArrowLeft size={14} /> {t("backToRun")}</Button>
        <Button size="sm" variant="secondary"
          onClick={() => downloadMarkdown(markdownFilename(v.run, v.item.sample_id), itemMarkdown(v))}>
          <Download size={14} /> {t("exportItem")}</Button>
      </div>
      <Section title={t("itemTitle", v.item.sample_id)}
        description={<>{code(`${v.item.writer}${v.item.channel ? ` · ${v.item.channel}` : ""}`)} · {v.item.scope} ·{" "}
          {v.item.split} · {v.item.kind}{v.item.country ? ` · ${v.item.country}` : ""}
          {v.item.days ? ` · ${v.item.days}d` : ""} · {t("labelTitle")}{" "}
          <Badge tone={resultTone(v.item.label_verdict)}>{v.item.label_verdict}</Badge></>}>
        <div style={{ fontSize: 12, color: K.muted }}>{t("reviewNote")}</div>
      </Section>
      <div style={sideBySide}>
        <Section title={t("sourceTitle")} description={t("sourceHelp")} testId="eval-item-source">
          {v.source_text
            ? <div style={{ ...textBox, maxHeight: "75vh", overflowY: "auto" }}>{v.source_text}</div>
            : <div style={{ fontSize: 13, color: K.muted }}>{t("noSource")}</div>}
          <details style={{ marginTop: 12 }}>
            <summary style={{ cursor: "pointer", fontSize: 12.5, color: K.muted }}>{t("rawInput")}</summary>
            <pre style={{ ...textBox, fontFamily: mono, fontSize: 11.5, maxHeight: 420, overflow: "auto" }}>
              {JSON.stringify(v.input, null, 2)}</pre>
          </details>
        </Section>
        {v.results.map((r) => <ResultCard key={r.result_id} r={r} t={t} />)}
      </div>
    </div>
  );
}

const MARK_TONE = [K.amberSoft, K.dangerSoft, K.infoSoft, K.purpleSoft];

function ResultCard({ r, t }: { r: ItemResult; t: T }) {
  const [lineage, setLineage] = useState(false);
  const gate = r.gate_replay;
  return (
    <Section title={t("outputTitle", r.arm, r.mode)} testId={`eval-result-${r.arm}`}
      description={<span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        <Badge tone={resultTone(r.verdict)}>{r.verdict}</Badge>
        {r.hard_stop && <Badge tone="danger">{t("hardStop")}</Badge>}
        <span>{t("claimsCount", r.claims.length)}</span>
        {r.judge_model && <span>{t("judgeLine", r.judge_model, r.judge_version ?? "—")}</span>}
      </span>}
      actions={<Button size="sm" variant="ghost" onClick={() => setLineage((x) => !x)}>
        {lineage ? t("hideLineage") : t("showLineage")}</Button>}>
      <div style={{ display: "grid", gap: 12 }}>
        <div style={{ fontSize: 12.5 }}>
          <span style={{ color: K.muted }}>{t("failureCodes")}: </span>
          {r.failure_codes.length ? code(r.failure_codes.join(", ")) : t("none")}
        </div>
        {gate && (
          <div style={{ fontSize: 12.5 }} title={t("gateHelp")}>
            <span style={{ color: K.muted }}>{t("gateTitle")}: </span>
            {gate.error ? t("gateError", gate.error) : (
              <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                <Badge tone={gate.passed ? "success" : "danger"}>{gate.passed ? t("gatePassed") : t("gateFailed")}</Badge>
                {gate.first_failure && <span>{t("gateFirst", gate.first_failure)}</span>}
                {!!gate.blocking?.length && <span>{t("gateBlocking")}: {code(gate.blocking.join(", "))}</span>}
              </span>
            )}
          </div>
        )}
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink, marginBottom: 6 }}>
            {t("claimsTitle", r.claims.length)}</div>
          {r.claims.length ? (
            <ol style={{ margin: 0, paddingLeft: 20, display: "grid", gap: 6, fontSize: 13 }}>
              {r.claims.map((c, i) => (
                <li key={i} data-testid="eval-claim">
                  <span style={{ background: MARK_TONE[i % MARK_TONE.length], borderRadius: 4, padding: "0 3px" }}>
                    {c.claim}</span>{" "}
                  <Badge tone="warning">{c.policy_class}</Badge> {code(c.field)}
                  {c.why && <div style={{ fontSize: 12, color: K.muted }}>{t("claimWhy")}: {c.why}</div>}
                </li>
              ))}
            </ol>
          ) : <div style={{ fontSize: 13, color: K.muted }}>{t("noClaims")}</div>}
        </div>
        <div>
          {r.output_source === "scored" && (
            <div style={{ fontSize: 12, color: K.muted, marginBottom: 6 }}>{t("scoredFrom")}</div>
          )}
          {r.output_fields.length ? r.output_fields.map((f) => (
            <div key={f.field} style={{ marginBottom: 10 }}>
              <div style={{ fontFamily: mono, fontSize: 11.5, color: K.muted2, marginBottom: 2 }}>{f.field}</div>
              <div style={textBox}>
                {highlightSegments(f.text, r.claims).map((s, i) => s.claim == null ? <span key={i}>{s.text}</span> : (
                  <mark key={i} title={`${s.claim + 1}. ${r.claims[s.claim].policy_class}`}
                    style={{ background: MARK_TONE[s.claim % MARK_TONE.length], color: "inherit", borderRadius: 3,
                      padding: "0 2px" }}>
                    {s.text}<sup style={{ fontSize: 10, marginLeft: 2 }}>{s.claim + 1}</sup></mark>
                ))}
              </div>
            </div>
          )) : <div style={{ fontSize: 13, color: K.muted }}>{t("noOutput")}</div>}
        </div>
        {Object.keys(r.checks).length > 0 && (
          <details>
            <summary style={{ cursor: "pointer", fontSize: 12.5, color: K.muted }}>{t("checksTitle")}</summary>
            <pre style={{ ...textBox, fontFamily: mono, fontSize: 11.5, maxHeight: 300, overflow: "auto" }}>
              {JSON.stringify(r.checks, null, 2)}</pre>
          </details>
        )}
        {lineage && <LineageView workflow="eval" objectId={r.result_id} t={t} />}
      </div>
    </Section>
  );
}

// ── lineage ─────────────────────────────────────────────────────────────────────────────────────────────────────

function CoverageTab({ t }: { t: T }) {
  const [days, setDays] = useState<number>(COVERAGE_DAYS[0]);
  const q = useQuery({
    queryKey: ["eval-runs", "coverage", days],
    queryFn: () => apiGet<CoverageView>(coverageUrl(days)),
    staleTime: 60_000,
  });
  const fields = q.data?.fields ?? [];
  return (
    <Section title={t("coverageTitle")} description={t("coverageHelp")} padded={false} testId="lineage-coverage"
      actions={<Select size="sm" value={String(days)} aria-label={t("coverageDays")}
        options={COVERAGE_DAYS.map((d) => ({ value: String(d), label: t("days", d) }))}
        onChange={(e) => setDays(Number(e.target.value))} />}>
      {q.error ? <ErrorState message={(q.error as Error).message || t("loadError")} onRetry={() => q.refetch()} /> : (
        <DataTable tableId="lineage-coverage" data={q.data?.workflows ?? []} loading={q.isLoading}
          getRowId={(w) => w.workflow} compact searchable={false} paginate={false} tableMinWidth={720}
          columns={[
            { accessorKey: "workflow", header: t("colWorkflow"), cell: (c) => code(c.row.original.workflow) },
            { accessorKey: "objects", header: t("colObjects") },
            ...fields.map((f): ColumnDef<CoverageView["workflows"][number], unknown> => ({
              id: f, header: () => code(f),
              cell: (c) => {
                const v = c.row.original.fields[f];
                return v == null ? <span style={{ fontSize: 12, color: K.muted }}>{t("notExpected")}</span>
                  : <span style={{ fontSize: 12.5 }}>{v.count} · {pct(v.share)}</span>;
              },
            })),
            { id: "floor", header: t("colFloor"), cell: (c) => pct(c.row.original.complete_share_floor) },
          ]} />
      )}
    </Section>
  );
}

function LookupTab({ t }: { t: T }) {
  const [workflow, setWorkflow] = useState<LineageWorkflow>("t9");
  const [objectId, setObjectId] = useState("");
  const [shown, setShown] = useState<{ workflow: LineageWorkflow; id: string } | null>(null);
  const [tourId, setTourId] = useState("");
  const [tourShown, setTourShown] = useState<string | null>(null);
  const objectBad = objectId.trim() !== "" && !isUuid(objectId);
  const tourBad = tourId.trim() !== "" && !isUuid(tourId);
  return (
    <div style={grid}>
      <Section title={t("lookupTitle")} description={t("lookupHelp")} testId="lineage-lookup">
        <form style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}
          onSubmit={(e) => { e.preventDefault(); if (isUuid(objectId)) setShown({ workflow, id: objectId.trim() }); }}>
          <Field label={t("workflow")}>
            <Select value={workflow} options={LINEAGE_WORKFLOWS.map((w) => ({ value: w, label: w }))}
              onChange={(e) => setWorkflow(e.target.value as LineageWorkflow)} />
          </Field>
          <Field label={t("objectId")} error={objectBad ? t("invalidId") : undefined}>
            <Input value={objectId} onChange={(e) => setObjectId(e.target.value)}
              style={{ fontFamily: mono, width: "min(100%, 340px)" }} />
          </Field>
          <Button type="submit" size="sm" disabled={!isUuid(objectId)}>{t("show")}</Button>
        </form>
        {shown && <div style={{ marginTop: 14 }}><LineageView workflow={shown.workflow} objectId={shown.id} t={t} /></div>}
      </Section>
      <Section title={t("fanoutTitle")} description={t("fanoutHelp")} testId="lineage-fanout">
        <form style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}
          onSubmit={(e) => { e.preventDefault(); if (isUuid(tourId)) setTourShown(tourId.trim()); }}>
          <Field label={t("tourId")} error={tourBad ? t("invalidId") : undefined}>
            <Input value={tourId} onChange={(e) => setTourId(e.target.value)}
              style={{ fontFamily: mono, width: "min(100%, 340px)" }} />
          </Field>
          <Button type="submit" size="sm" disabled={!isUuid(tourId)}>{t("show")}</Button>
        </form>
        {tourShown && <div style={{ marginTop: 14 }}><FanoutView tourId={tourShown} t={t} /></div>}
      </Section>
    </div>
  );
}

function LineageView({ workflow, objectId, t }: { workflow: LineageWorkflow; objectId: string; t: T }) {
  const { formatDateTime } = useFormat();
  const q = useQuery({
    queryKey: ["eval-runs", "lineage", workflow, objectId],
    queryFn: () => apiGet<ObjectLineage>(lineageUrl(workflow, objectId)),
    staleTime: 60_000,
  });
  if (q.isLoading) return <TableSkeleton rows={4} cols={2} />;
  if (q.error || !q.data) {
    return <ErrorState message={(q.error as Error | null)?.message || t("loadError")} onRetry={() => q.refetch()} />;
  }
  const l = q.data;
  const stages = Object.entries(l.prompt_version.stages).map(([s, v]) => `${s} ${v.join(", ")}`).join(" · ");
  const rows: [string, React.ReactNode][] = [
    [t("fCreated"), l.created_at ? formatDateTime(l.created_at) : "—"],
    [t("fStatus"), code(l.status)],
    [t("fPolicy"), code(l.policy_sha)],
    [t("fFactSheet"), code(l.fs_id)],
    [t("fWriterPrompt"), code(l.prompt_version.writer)],
    [t("fStagePrompts"), code(stages || null)],
    [t("fRouteStamped"), code(l.route_config_version.stamped)],
    [t("fRouteLedger"), code(l.route_config_version.ledger.join(", ") || null)],
    [t("fLedgerJoin"), code(l.ledger_join)],
  ];
  if (l.run) rows.push([t("fRun"), code(l.run.run_id)]);
  return (
    <div style={{ display: "grid", gap: 10 }} data-testid="lineage-view">
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", fontSize: 12.5 }}>
        <Badge tone={l.complete ? "success" : "warning"}>{l.complete ? t("complete") : t("incomplete")}</Badge>
        {!l.complete && <span>{t("missingFields", l.missing.join(", "))}</span>}
      </div>
      <dl style={{ display: "grid", gridTemplateColumns: "minmax(120px, max-content) minmax(0, 1fr)", gap: "4px 12px",
        margin: 0, fontSize: 12.5 }}>
        {rows.map(([k, v]) => [
          <dt key={`k-${k}`} style={{ color: K.muted }}>{k}</dt>,
          <dd key={`v-${k}`} style={{ margin: 0, overflowWrap: "anywhere" }}>{v}</dd>,
        ])}
      </dl>
      <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink }}>{t("callsTitle")}</div>
      <DataTable tableId="lineage-calls" data={l.calls} compact searchable={false} paginate={false}
        getRowId={(c) => `${c.stage}-${c.prompt_version}-${c.route_config_version}-${c.model}`}
        emptyTitle={t("noCalls")} tableMinWidth={560}
        columns={[
          { accessorKey: "stage", header: t("colStage"), cell: (c) => code(c.row.original.stage) },
          { accessorKey: "prompt_version", header: t("colPrompt"), cell: (c) => code(c.row.original.prompt_version) },
          { accessorKey: "route_config_version", header: t("colRoute"),
            cell: (c) => code(c.row.original.route_config_version) },
          { accessorKey: "model", header: t("colModel"), cell: (c) => code(c.row.original.model) },
          { accessorKey: "calls", header: t("colCalls") },
          { accessorKey: "last_at", header: t("colLast"),
            cell: (c) => c.row.original.last_at ? formatDateTime(c.row.original.last_at) : "—" },
        ]} />
    </div>
  );
}

function FanoutView({ tourId, t }: { tourId: string; t: T }) {
  const { formatDateTime } = useFormat();
  const q = useQuery({
    queryKey: ["eval-runs", "fanout", tourId],
    queryFn: () => apiGet<TourFanout>(fanoutUrl(tourId)),
    staleTime: 60_000,
  });
  if (q.isLoading) return <TableSkeleton rows={4} cols={4} />;
  if (q.error || !q.data) {
    return <ErrorState message={(q.error as Error | null)?.message || t("loadError")} onRetry={() => q.refetch()} />;
  }
  const f = q.data;
  const s = (v: unknown) => (typeof v === "string" ? v : v == null ? null : String(v));
  const tally = (m: Record<string, number>) => Object.entries(m).map(([k, n]) => `${k} ${n}`).join(" · ") || "—";
  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="fanout-view">
      <div style={tiles}>
        <KpiTile size="sm" label={t("openMarks")} value={f.open} />
        <KpiTile size="sm" label={t("clearedRecent")} value={f.cleared_recent} />
        <KpiTile size="sm" label={t("byKind")} value={<span style={{ fontSize: 13 }}>{tally(f.by_object_kind)}</span>} />
        <KpiTile size="sm" label={t("byReason")} value={<span style={{ fontSize: 13 }}>{tally(f.by_reason)}</span>} />
      </div>
      {f.marks.length === 0 ? <EmptyState title={t("noMarks")} /> : (
        <DataTable tableId="fanout-marks" data={f.marks} compact searchable={false} tableMinWidth={720}
          getRowId={(m) => String(m.mark_id)}
          columns={[
            { id: "kind", header: t("colObjectKind"), cell: (c) => code(s(c.row.original.object_kind)) },
            { id: "object", header: t("colObject"), cell: (c) => code(s(c.row.original.object_id)) },
            { id: "reason", header: t("colReason"), cell: (c) => code(s(c.row.original.reason)) },
            { id: "detected", header: t("colMarked"),
              cell: (c) => { const v = s(c.row.original.detected_at); return v ? formatDateTime(v) : "—"; } },
            { id: "cleared", header: t("colCleared"),
              cell: (c) => { const v = s(c.row.original.cleared_at); return v ? formatDateTime(v) : "—"; } },
          ]} />
      )}
    </div>
  );
}
