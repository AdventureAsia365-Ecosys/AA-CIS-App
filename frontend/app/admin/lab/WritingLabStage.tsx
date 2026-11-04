"use client";
// app/admin/lab/WritingLabStage.tsx — AA-1039 R6 slice 3 (root ADR 0019 d13, TL-13 `regroup_lab`): Writing Lab › one
// stage. A stage is a writing-memory writer (lib/writingLab.ts LAB_STAGES); the stage bar is the only navigation
// between stages. Seven tabs: the first five share ONE read, GET /api/admin/writing-memory/lab/<writer>
// (services/writing_memory/lab.py); Releases and Metrics (S268) each make their own read, only when opened:
//   Eval gate        the release machinery's state: memory switch, live / newest platform release with its evidence
//                    runs, releases per status, tenant releases live, rules per status, the newest finished
//                    evaluation, and one plain sentence for the state.
//   Samples          the active golden set items of the writer: counts per split / kind, the newest items (ids, tiers,
//                    labels; never the text).
//   Labels           golden labels (verdict, source) and the writing signals (fact check, reason, action, who, owner).
//   Experiments      evaluation runs, release shadow runs, model shadow of the writer's prompt stages (30 days).
//   Prompt versions  the prompt register rows of those stages (hash and file, no prompt text).
//   Releases         every release of the writer, platform and tenant: status, training, evidence verdicts,
//                    check-again flag and dates (GET /api/admin/writing-memory/releases?writer=); the actions stay
//                    in Writing memory › Releases (one click away).
//   Metrics          the stored weekly reports seen from the writer (GET /api/admin/writing-memory/metrics): writes,
//                    re-flag, light edits, minutes to fix, cost per write, replay hit, clean pairs, soft stops and
//                    what the weekly job did. Run / keep stay in Writing memory › Metrics.
// Read-only and $0: no learning logic (the S262 TL-11 freeze stays); the Writing memory tools stay one click away
// (/admin/lab/writing-memory). No tenant text on these pages (the logged ai_ops read of tenant text is R2's).
// `?tab=` picks the tab (kept with history.replaceState). Admin only (lib/rbac.ts PAGE_RULES /admin/lab; BFF
// API_READ_RULES writing-memory; backend tenant.read).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, RefreshCw } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, EmptyState, ErrorState, K, KpiTile, PageFrame, PageHeader, Section, TableSkeleton, Tabs,
  apiGet, mono, useFormat, useT, type ColumnDef,
} from "../../_kit";
import {
  DEFAULT_STAGE, LAB_API, LAB_STAGES, LAB_TABS, WM_METRICS_API, countRows, evidenceSummary, gateTone, parseLabTab, pct,
  releaseTone, releasesUrl, share, stageBySlug, stageHref, evalTone, verdictTone, writerWeeks, writingMemoryHref,
  type Counts, type EvalRun, type GoldenItem, type LabRelease, type LabStageView, type LabTab, type LabWriter,
  type ModelShadow, type PromptVersion, type Release, type SectionStatus, type ShadowRun, type WriterWeek,
} from "@/lib/writingLab";
import type { MetricsPage, ReleasesPage } from "@/lib/writingMemory";
import type { Translator } from "../../_kit/core/i18n";
import { LAB } from "./messages";

type T = Translator<typeof LAB.en>;

const STAGE_LABEL: Record<LabWriter, { full: keyof typeof LAB.en; short: keyof typeof LAB.en }> = {
  a1: { full: "stageA1", short: "stageA1Short" },
  t2: { full: "stageT2", short: "stageT2Short" },
  t9_blog: { full: "stageT9Blog", short: "stageT9BlogShort" },
  t9_social: { full: "stageT9Social", short: "stageT9SocialShort" },
};
const TAB_LABEL: Record<LabTab, keyof typeof LAB.en> = {
  gate: "tabGate", samples: "tabSamples", labels: "tabLabels", experiments: "tabExperiments", prompts: "tabPrompts",
  releases: "tabReleases", metrics: "tabMetrics",
};

function setParam(name: string, value: string | null): void {
  try {
    const url = new URL(window.location.href);
    if (value == null) url.searchParams.delete(name);
    else url.searchParams.set(name, value);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    /* the address bar is a convenience */
  }
}

const usd = (n: number | null | undefined) => (n == null ? "—" : `$${n.toFixed(n > 0 && n < 1 ? 4 : 2)}`);
const code = (v: React.ReactNode) => <span style={{ fontFamily: mono, fontSize: 12 }}>{v ?? "—"}</span>;
const grid: React.CSSProperties = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 };
const tiles: React.CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))", gap: 12,
};

export default function WritingLabStage({ slug }: { slug: string }) {
  const t = useT(LAB);
  const router = useRouter();
  const qc = useQueryClient();
  const params = useSearchParams();
  const stage = stageBySlug(slug);
  const [tab, setTabState] = useState<LabTab>(() => parseLabTab(params.get("tab")));
  const setTab = (k: string) => {
    const next = parseLabTab(k);
    setTabState(next);
    setParam("tab", next === "gate" ? null : next);
  };
  const view = useQuery({
    queryKey: ["writing-lab", stage?.writer],
    queryFn: () => apiGet<LabStageView>(`${LAB_API}/${stage!.writer}`),
    enabled: !!stage,
    staleTime: 30_000,
  });

  const stageTabs = LAB_STAGES.map((s) => ({ key: s.slug, label: t(STAGE_LABEL[s.writer].short) }));
  const tabs = LAB_TABS.map((k) => ({ key: k, label: t(TAB_LABEL[k]) }));
  const title = stage ? t(STAGE_LABEL[stage.writer].full) : t("title");

  return (
    <PageFrame>
      <PageHeader title={title}
        breadcrumbs={[{ label: t("crumbGroup"), href: stageHref(DEFAULT_STAGE) }, { label: title }]}
        description={t("description")}
        actions={
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button variant="secondary" size="sm" onClick={() => router.push("/admin/lab/writing-memory")}>
              {t("openWritingMemory")}</Button>
            <Button variant="ghost" size="sm" onClick={() => qc.invalidateQueries({ queryKey: ["writing-lab"] })}>
              <RefreshCw size={14} /> {t("refresh")}</Button>
          </div>
        } />
      <div data-testid="lab-stage-bar" style={{ marginBottom: 12 }}>
        <Tabs tabs={stageTabs} active={stage?.slug ?? ""} ariaLabel={t("stageBar")}
          onChange={(k) => router.push(stageHref(k, tab))} />
      </div>
      {!stage ? (
        <EmptyState icon={<FlaskConical size={22} />} title={t("loadError")}
          action={<Button onClick={() => router.push(stageHref(DEFAULT_STAGE))}>{t(STAGE_LABEL.a1.full)}</Button>} />
      ) : (
        <>
          <div style={{ marginBottom: 14 }} data-testid="lab-tabs">
            <Tabs tabs={tabs} active={tab} onChange={setTab} variant="underline" />
          </div>
          {tab === "releases" ? <ReleasesTab writer={stage.writer} t={t} />
            : tab === "metrics" ? <MetricsTab writer={stage.writer} t={t} />
            : view.isLoading ? <TableSkeleton rows={5} cols={4} /> : view.error ? (
            <ErrorState message={(view.error as Error).message || t("loadError")} onRetry={() => view.refetch()} />
          ) : view.data ? (
            <div data-testid="writing-lab-ready" style={grid}>
              <div style={{ fontSize: 12, color: K.muted }}>{t("noText")}</div>
              {tab === "gate" && <Part status={view.data.sections.gate} t={t}>
                {view.data.gate && <GateTab g={view.data.gate} t={t} />}</Part>}
              {tab === "samples" && <Part status={view.data.sections.samples} t={t}>
                {view.data.samples && <SamplesTab s={view.data.samples} t={t} />}</Part>}
              {tab === "labels" && <Part status={view.data.sections.labels} t={t}>
                {view.data.labels && <LabelsTab l={view.data.labels} t={t} />}</Part>}
              {tab === "experiments" && <Part status={view.data.sections.experiments} t={t}>
                {view.data.experiments && <ExperimentsTab e={view.data.experiments} t={t} />}</Part>}
              {tab === "prompts" && <Part status={view.data.sections.prompts} t={t}>
                {view.data.prompts && <PromptsTab p={view.data.prompts} t={t} />}</Part>}
            </div>
          ) : null}
        </>
      )}
    </PageFrame>
  );
}

/** One section of the read: its data, or why it is empty (tables not applied yet, or the read failed). */
function Part({ status, t, children }: { status: SectionStatus; t: T; children: React.ReactNode }) {
  if (status.ready) return <>{children}</>;
  return status.error ? <ErrorState message={t("sectionError")} /> : <EmptyState title={t("notReady")} />;
}

// ── Eval gate ───────────────────────────────────────────────────────────────────────────────────────────────────────

function ReleaseFacts({ r, t }: { r: Release; t: T }) {
  const { formatDateTime } = useFormat();
  const rows: [string, React.ReactNode][] = [
    [t("fStatus"), <Badge key="s" tone={r.status === "active" ? "success" : r.status === "rejected" ? "danger" : "neutral"}>
      {r.status}</Badge>],
    [t("fClass"), code(r.content_class)],
    [t("fTrain"), code(r.train_kind)],
    [t("fRules"), r.rules],
    [t("fEvidence"), t("evidence", !!r.r0_run_id, !!r.eval_run_id, !!r.shadow_run_id)],
    [t("fActivated"), r.activated_at ? formatDateTime(r.activated_at) : "—"],
    [t("fCreated"), formatDateTime(r.created_at)],
  ];
  return (
    <dl style={{ display: "grid", gridTemplateColumns: "minmax(110px, auto) minmax(0, 1fr)", gap: "6px 14px", margin: 0,
      fontSize: 13 }}>
      {rows.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt style={{ color: K.muted }}>{k}</dt>
          <dd style={{ margin: 0, color: K.ink, overflowWrap: "anywhere" }}>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function GateTab({ g, t }: { g: NonNullable<LabStageView["gate"]>; t: T }) {
  const { formatDateTime } = useFormat();
  const state = g.state;
  const sw = g.switch;
  return (
    <>
      <Section title={t("gateTitle")} description={t("gateHelp")} testId="lab-gate">
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginBottom: 14 }}>
          <span data-testid="lab-gate-state"><Badge tone={gateTone(state.code)}>{t(`gate_${state.code}`)}</Badge></span>
          {state.reasons && state.reasons.length > 0 && (
            <span style={{ fontSize: 12.5, color: K.muted }}>{t("reverifyBecause", state.reasons.join(", "))}</span>
          )}
        </div>
        <div style={tiles}>
          <KpiTile size="sm" label={t("kpiMemory")} value={sw ? (sw.memory_enabled ? t("memoryOn") : t("memoryOff")) : t("noSwitch")}
            valueTone={sw?.memory_enabled ? "success" : "neutral"}
            hint={sw?.train_closed_month ? t("trainClosed", sw.train_closed_month.slice(0, 7)) : undefined} />
          <KpiTile size="sm" label={t("kpiLive")}
            value={g.active_release ? t("releaseNo", g.active_release.release_no) : t("noneLive")}
            hint={g.active_release?.activated_at ? formatDateTime(g.active_release.activated_at) : undefined} />
          <KpiTile size="sm" label={t("kpiTenantLive")} value={g.tenant_releases_active} />
          <KpiTile size="sm" label={t("kpiActiveEval")} value={g.active_eval ? g.active_eval.verdict ?? "—" : t("noEval")}
            valueTone={evalTone(g.active_eval)}
            hint={g.active_eval ? `${g.active_eval.run_kind} · ${formatDateTime(g.active_eval.finished_at)}` : undefined} />
          <KpiTile size="sm" label={t("kpiLastEval")} value={g.last_eval ? g.last_eval.verdict ?? "—" : t("noEval")}
            hint={g.last_eval ? `${g.last_eval.run_kind} · ${formatDateTime(g.last_eval.finished_at)} · ${
              g.last_eval.for_active ? t("evalOfLive") : g.last_eval.release_id ? t("evalOfOther") : t("evalOfNone")}`
              : undefined} />
          <KpiTile size="sm" label={t("kpiRules")} value={Object.values(g.rules).reduce((a, b) => a + b, 0)}
            hint={t("rulesHint", g.rules.candidate ?? 0, g.rules.active ?? 0)} />
        </div>
      </Section>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))", gap: 16 }}>
        <Section title={t("liveTitle")}>
          {g.active_release ? <ReleaseFacts r={g.active_release} t={t} /> : (
            <div style={{ fontSize: 13, color: K.muted }}>{t("noRelease")}</div>)}
        </Section>
        <Section title={t("latestTitle")}>
          {g.latest_release ? <ReleaseFacts r={g.latest_release} t={t} /> : (
            <div style={{ fontSize: 13, color: K.muted }}>{t("noRelease")}</div>)}
        </Section>
      </div>
      <Section title={t("releasesTitle")} description={t("releasesHelp")} padded={false}>
        <CountTable id="lab-releases" counts={g.releases} t={t} />
      </Section>
    </>
  );
}

// ── Samples ─────────────────────────────────────────────────────────────────────────────────────────────────────────

function SamplesTab({ s, t }: { s: NonNullable<LabStageView["samples"]>; t: T }) {
  const { formatDate } = useFormat();
  const cols = useMemo<ColumnDef<GoldenItem, unknown>[]>(() => [
    { accessorKey: "sample_id", header: t("colSample"), cell: (c) => code(c.row.original.sample_id) },
    { accessorKey: "set_name", header: t("colSet"), cell: (c) => code(c.row.original.set_name) },
    { accessorKey: "split", header: t("colSplit"),
      cell: (c) => <Badge tone={c.row.original.split === "trap" ? "warning" : c.row.original.split === "holdout"
        ? "info" : "neutral"}>{c.row.original.split}</Badge> },
    { accessorKey: "kind", header: t("colKind") },
    { id: "tiers", accessorFn: (i) => i.tiers.join(", "), header: t("colTiers"),
      cell: (c) => <span style={{ fontSize: 12 }}>{c.row.original.tiers.join(", ") || "—"}</span> },
    { accessorKey: "channel", header: t("colChannel"), cell: (c) => code(c.row.original.channel) },
    { accessorKey: "label_verdict", header: t("colLabel"),
      cell: (c) => <Badge tone={verdictTone(c.row.original.label_verdict)}>{c.row.original.label_verdict}</Badge> },
    { accessorKey: "label_source", header: t("colSource"), cell: (c) => code(c.row.original.label_source) },
    { accessorKey: "created_at", header: t("colAdded"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDate(c.row.original.created_at)}</span> },
  ], [t, formatDate]);
  return (
    <Section title={t("samplesTitle")} description={t("samplesHelp")} testId="lab-samples">
      <div style={{ ...tiles, marginBottom: 12 }}>
        <KpiTile label={t("kpiSamples")} value={s.total}
          hint={s.by_kind.synthetic ? t("kpiSynthetic", s.by_kind.synthetic) : undefined} />
        <KpiTile label={t("kpiTune")} value={s.by_split.tune ?? 0} />
        <KpiTile label={t("kpiHoldout")} value={s.by_split.holdout ?? 0} />
        <KpiTile label={t("kpiTrap")} value={s.by_split.trap ?? 0} />
      </div>
      <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 10 }}>
        {s.golden_sets.length ? t("setsLine", s.golden_sets.map((g) => `${g.name} ${g.version} (${g.items})`).join(", "))
          : t("noSets")}
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>{t("newestItems", s.limit)}</div>
      <div style={grid}>
        <DataTable tableId="lab-samples" data={s.items} columns={cols} getRowId={(i) => i.item_id} compact
          tableMinWidth={860} emptyTitle={t("samplesEmpty")} />
      </div>
    </Section>
  );
}

// ── Labels ──────────────────────────────────────────────────────────────────────────────────────────────────────────

function CountTable({ id, counts, t }: { id: string; counts: Counts; t: T }) {
  const rows = countRows(counts);
  const total = rows.reduce((a, r) => a + r.n, 0);
  const cols = useMemo<ColumnDef<{ key: string; n: number }, unknown>[]>(() => [
    { accessorKey: "key", header: t("colCode"), cell: (c) => code(c.row.original.key) },
    { accessorKey: "n", header: t("colCount"), meta: { align: "right" } },
    { id: "share", accessorFn: (r) => r.n, header: t("colShare"), meta: { align: "right" },
      cell: (c) => share(c.row.original.n, total) },
  ], [t, total]);
  return (
    <div style={grid}>
      <DataTable tableId={id} data={rows} columns={cols} getRowId={(r) => r.key} compact searchable={false}
        paginate={false} emptyTitle={t("labelsEmpty")} />
    </div>
  );
}

function LabelsTab({ l, t }: { l: NonNullable<LabStageView["labels"]>; t: T }) {
  const pair: React.CSSProperties = {
    display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))", gap: 14,
  };
  const block = (title: string, id: string, c: Counts) => (
    <div key={id} style={{ minWidth: 0 }}>
      <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink2, margin: "0 0 6px" }}>{title}</div>
      <CountTable id={id} counts={c} t={t} />
    </div>
  );
  const sg = l.signals;
  return (
    <>
      <Section title={t("goldenLabels")} description={t("goldenLabelsHelp")} testId="lab-labels-golden">
        <div style={pair}>
          {block(t("byVerdict"), "lab-golden-verdict", l.golden.by_verdict)}
          {block(t("bySource"), "lab-golden-source", l.golden.by_source)}
        </div>
      </Section>
      <Section title={t("signalLabels")} description={t("signalLabelsHelp", sg.total, sg.last_30d)}
        testId="lab-labels-signals">
        <div style={pair}>
          {block(t("byFact"), "lab-sig-fact", sg.by_fact_status)}
          {block(t("byReason"), "lab-sig-reason", sg.by_reason_code)}
          {block(t("byAction"), "lab-sig-action", sg.by_action)}
          {block(t("byActor"), "lab-sig-actor", sg.by_actor_type)}
          {block(t("byScope"), "lab-sig-scope", sg.by_scope)}
        </div>
      </Section>
    </>
  );
}

// ── Experiments ─────────────────────────────────────────────────────────────────────────────────────────────────────

function ExperimentsTab({ e, t }: { e: NonNullable<LabStageView["experiments"]>; t: T }) {
  const { formatDateTime } = useFormat();
  const runCols = useMemo<ColumnDef<EvalRun, unknown>[]>(() => [
    { accessorKey: "created_at", header: t("colStarted"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDateTime(c.row.original.created_at)}</span> },
    { id: "run", accessorFn: (r) => `${r.pipeline} ${r.run_kind}`, header: t("colRun"),
      cell: (c) => <span>{code(c.row.original.run_kind)}{c.row.original.pipeline !== "writing_memory"
        ? <span style={{ color: K.muted, fontSize: 11.5 }}> · {c.row.original.pipeline}</span> : null}</span> },
    { id: "verdict", accessorFn: (r) => r.verdict ?? "", header: t("colVerdict"),
      cell: (c) => { const r = c.row.original;
        return <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
          <Badge tone={verdictTone(r.verdict)}>{r.verdict ?? (r.finished_at ? "—" : t("running"))}</Badge>
          {r.regression_detected && <Badge tone="danger">{t("regression")}</Badge>}</span>; } },
    { accessorKey: "tour_count", header: t("colItems"), meta: { align: "right" } },
    { id: "score", accessorFn: (r) => r.avg_quality_score ?? -1, header: t("colScore"), meta: { align: "right" },
      cell: (c) => c.row.original.avg_quality_score == null ? "—" : c.row.original.avg_quality_score.toFixed(2) },
    { id: "cost", accessorFn: (r) => r.cost_usd ?? 0, header: t("colCost"), meta: { align: "right" },
      cell: (c) => usd(c.row.original.cost_usd) },
    { accessorKey: "stop_reason", header: t("colStop"), cell: (c) => code(c.row.original.stop_reason) },
  ], [t, formatDateTime]);
  const shadowCols = useMemo<ColumnDef<ShadowRun, unknown>[]>(() => [
    { accessorKey: "started_at", header: t("colStarted"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDateTime(c.row.original.started_at)}</span> },
    { accessorKey: "items", header: t("colItems"), meta: { align: "right" } },
    { id: "pf", accessorFn: (r) => r.pass, header: t("colPassFail"),
      cell: (c) => { const r = c.row.original; return `${r.pass} / ${r.fail} / ${r.errors}`; } },
    { id: "cost", accessorFn: (r) => r.cost_usd, header: t("colCost"), meta: { align: "right" },
      cell: (c) => usd(c.row.original.cost_usd) },
    { accessorKey: "last_at", header: t("colLast"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDateTime(c.row.original.last_at)}</span> },
  ], [t, formatDateTime]);
  const modelCols = useMemo<ColumnDef<ModelShadow, unknown>[]>(() => [
    { accessorKey: "stage", header: t("colStage"), cell: (c) => code(c.row.original.stage) },
    { id: "models", accessorFn: (r) => `${r.primary_model} ${r.shadow_model}`, header: t("colModels"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 11.5, overflowWrap: "anywhere" }}>
        {c.row.original.primary_model} → {c.row.original.shadow_model}</span> },
    { accessorKey: "calls", header: t("colCalls"), meta: { align: "right" } },
    { accessorKey: "errors", header: t("colErrors"), meta: { align: "right" } },
    { id: "cost", accessorFn: (r) => r.shadow_cost_usd, header: t("colCostPair"), meta: { align: "right" },
      cell: (c) => `${usd(c.row.original.primary_cost_usd)} / ${usd(c.row.original.shadow_cost_usd)}` },
    { id: "lat", accessorFn: (r) => r.shadow_p50_ms ?? -1, header: t("colLatency"), meta: { align: "right" },
      cell: (c) => c.row.original.shadow_p50_ms == null ? "—" : `${Math.round(c.row.original.shadow_p50_ms)} ms` },
  ], [t]);
  return (
    <>
      <Section title={t("evalRunsTitle")} description={t("evalRunsHelp")} testId="lab-eval-runs">
        <div style={grid}>
          <DataTable tableId="lab-eval-runs" data={e.eval_runs} columns={runCols} getRowId={(r) => r.id} compact
            tableMinWidth={760} emptyTitle={t("evalEmpty")} />
        </div>
      </Section>
      <Section title={t("shadowTitle")} description={t("shadowHelp")}>
        <div style={grid}>
          <DataTable tableId="lab-shadow-runs" data={e.shadow_runs} columns={shadowCols} getRowId={(r) => r.run_id}
            compact searchable={false} tableMinWidth={560} emptyTitle={t("shadowEmpty")} />
        </div>
      </Section>
      <Section title={t("modelShadowTitle", e.model_shadow_days)} description={t("modelShadowHelp")}>
        <div style={grid}>
          <DataTable tableId="lab-model-shadow" data={e.model_shadow} columns={modelCols}
            getRowId={(r) => `${r.stage}:${r.primary_model}:${r.shadow_model}`} compact searchable={false}
            tableMinWidth={760} emptyTitle={t("modelShadowEmpty")} />
        </div>
      </Section>
    </>
  );
}

// ── Prompt versions ─────────────────────────────────────────────────────────────────────────────────────────────────

function PromptsTab({ p, t }: { p: NonNullable<LabStageView["prompts"]>; t: T }) {
  const { formatDate } = useFormat();
  const cols = useMemo<ColumnDef<PromptVersion, unknown>[]>(() => [
    { accessorKey: "stage", header: t("colStage"), cell: (c) => code(c.row.original.stage) },
    { accessorKey: "version", header: t("colVersion"), cell: (c) => code(c.row.original.version) },
    { accessorKey: "sha_short", header: t("colHash"),
      cell: (c) => <span title={c.row.original.prompt_sha256}>{code(c.row.original.sha_short)}</span> },
    { accessorKey: "source_path", header: t("colPath"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 11.5, overflowWrap: "anywhere" }}>
        {c.row.original.source_path}</span> },
    { accessorKey: "created_by", header: t("colBy"), cell: (c) => code(c.row.original.created_by) },
    { accessorKey: "created_at", header: t("colAdded"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDate(c.row.original.created_at)}</span> },
  ], [t, formatDate]);
  return (
    <Section title={t("promptsTitle")} description={t("promptsHelp", p.stages.join(", "))} testId="lab-prompts">
      <div style={grid}>
        <DataTable tableId="lab-prompts" data={p.versions} columns={cols}
          getRowId={(r) => `${r.stage}:${r.version}`} compact tableMinWidth={760} emptyTitle={t("promptsEmpty")} />
      </div>
    </Section>
  );
}

// ── Releases (S268) ─────────────────────────────────────────────────────────────────────────────────────────────────

function ReleasesTab({ writer, t }: { writer: LabWriter; t: T }) {
  const router = useRouter();
  const { formatDate } = useFormat();
  const q = useQuery({
    queryKey: ["writing-lab", writer, "releases"],
    queryFn: () => apiGet<ReleasesPage>(releasesUrl(writer)),
    staleTime: 30_000,
  });
  const cols = useMemo<ColumnDef<LabRelease, unknown>[]>(() => [
    { accessorKey: "release_no", header: t("colRelease"), cell: (c) => code(c.row.original.release_no) },
    { accessorKey: "scope", header: t("colScope"),
      cell: (c) => code(c.row.original.scope === "tenant" && c.row.original.tenant_id
        ? t("tenantScope", c.row.original.tenant_id.slice(0, 8)) : c.row.original.scope) },
    { accessorKey: "status", header: t("colStatus"),
      cell: (c) => <Badge tone={releaseTone(c.row.original.status)} dot={false}>{c.row.original.status}</Badge> },
    { accessorKey: "content_class", header: t("colClass"), cell: (c) => code(c.row.original.content_class) },
    { accessorKey: "train_kind", header: t("fTrain"), cell: (c) => code(c.row.original.train_kind) },
    { accessorKey: "rule_ids", header: t("fRules"), enableSorting: false,
      cell: (c) => c.row.original.rule_ids?.length ?? 0 },
    { id: "evidence", header: t("colEvidence"), enableSorting: false,
      cell: (c) => {
        const ev = evidenceSummary(c.row.original);
        return ev.length === 0 ? "—" : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {ev.map((e) => (
              <Badge key={e.slot} tone={e.finished ? verdictTone(e.verdict) : "neutral"} dot={false}>
                {`${e.slot} ${e.finished ? e.verdict ?? "—" : "…"}`}</Badge>
            ))}
          </div>
        );
      } },
    { accessorKey: "reverify", header: t("colReverify"),
      cell: (c) => (c.row.original.reverify ? <Badge tone="warning" dot={false}>{t("reverifyYes")}</Badge> : "—") },
    { accessorKey: "activated_at", header: t("colActivated"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>
        {c.row.original.activated_at ? formatDate(c.row.original.activated_at) : "—"}</span> },
    { accessorKey: "retired_at", header: t("colRetired"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>
        {c.row.original.retired_at ? formatDate(c.row.original.retired_at) : "—"}</span> },
    { accessorKey: "created_at", header: t("fCreated"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDate(c.row.original.created_at)}</span> },
  ], [t, formatDate]);
  return (
    <Section title={t("relTitle")} description={t("relHelp")} testId="lab-releases-list" padded={false}
      actions={<Button variant="secondary" size="sm" onClick={() => router.push(writingMemoryHref("releases"))}>
        {t("manageReleases")}</Button>}>
      {q.isLoading ? <TableSkeleton rows={4} cols={6} /> : q.error ? (
        <ErrorState message={t("sectionError")} onRetry={() => q.refetch()} />
      ) : (
        <div data-testid="lab-releases-ready">
          <DataTable tableId="lab-releases-list" data={q.data?.items ?? []} columns={cols}
            getRowId={(r) => r.release_id} compact tableMinWidth={980} emptyTitle={t("relEmpty")} />
        </div>
      )}
    </Section>
  );
}

// ── Metrics (S268) ──────────────────────────────────────────────────────────────────────────────────────────────────

function MetricsTab({ writer, t }: { writer: LabWriter; t: T }) {
  const router = useRouter();
  const { formatDate } = useFormat();
  const q = useQuery({
    queryKey: ["writing-lab", "metrics"],
    queryFn: () => apiGet<MetricsPage>(WM_METRICS_API),
    staleTime: 60_000,
  });
  const weeks = useMemo(() => writerWeeks(q.data?.items, writer), [q.data, writer]);
  const num = (v: number | null | undefined, digits = 1) => (v === null || v === undefined ? "—" : v.toFixed(digits));
  const cols = useMemo<ColumnDef<WriterWeek, unknown>[]>(() => [
    { accessorKey: "week_start", header: t("colWeek"),
      cell: (c) => <span style={{ whiteSpace: "nowrap", fontSize: 12 }}>{formatDate(c.row.original.week_start)}
        {c.row.original.trigger === "manual" ? <span style={{ color: K.muted }}> · {t("manual")}</span> : null}</span> },
    { id: "writes", header: t("colWrites"), cell: (c) => c.row.original.kpi?.writes ?? "—" },
    { id: "reflag", header: t("colReflag"), cell: (c) => pct(c.row.original.kpi?.reflag_rate) },
    { id: "light", header: t("colLightEdit"), cell: (c) => pct(c.row.original.kpi?.light_edit_share) },
    { id: "minutes", header: t("colMinutes"), cell: (c) => num(c.row.original.kpi?.minutes_to_fix) },
    { id: "cost", header: t("colCostWrite"), cell: (c) => usd(c.row.original.kpi?.cost_per_write) },
    { id: "replay", header: t("colReplay"), cell: (c) => pct(c.row.original.replay_rate) },
    { id: "pairs", header: t("colCleanPairs"), cell: (c) => c.row.original.clean_pairs ?? "—" },
    { id: "stops", header: t("colStops"), enableSorting: false,
      cell: (c) => {
        const w = c.row.original;
        if (w.stops.length === 0 && w.actions.length === 0) return "—";
        return (
          <div style={{ display: "grid", gap: 2, fontSize: 12 }}>
            {w.stops.map((s) => <span key={`s${s.release_id}`} style={{ fontFamily: mono, overflowWrap: "anywhere" }}>
              {t("stopLine", s.release_no, s.stops.map((x) => x.code).join(", "))}</span>)}
            {w.actions.map((a, i) => <span key={`a${i}`} style={{ color: K.muted }}>
              {t("actionLine", a.release_no, a.action)}</span>)}
          </div>
        );
      } },
  ], [t, formatDate]);
  return (
    <Section title={t("metTitle")} description={t("metHelp")} testId="lab-metrics" padded={false}
      actions={<Button variant="secondary" size="sm" onClick={() => router.push(writingMemoryHref("metrics"))}>
        {t("openMetrics")}</Button>}>
      {q.isLoading ? <TableSkeleton rows={4} cols={6} /> : q.error ? (
        <ErrorState message={t("sectionError")} onRetry={() => q.refetch()} />
      ) : (
        <div data-testid="lab-metrics-ready">
          <DataTable tableId="lab-metrics" data={weeks} columns={cols}
            getRowId={(r) => `${r.week_start}:${r.trigger}`} compact tableMinWidth={900} emptyTitle={t("metEmpty")} />
        </div>
      )}
    </Section>
  );
}
