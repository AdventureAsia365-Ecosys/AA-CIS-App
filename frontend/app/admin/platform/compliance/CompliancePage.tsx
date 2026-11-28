"use client";
// app/admin/platform/compliance/CompliancePage.tsx — AA-1170: Platform › Compliance (readiness per framework, phase 1).
//
// Overview: one card per framework (Evidenced % strict and weighted, the S286 hand assessment as a reference, the
// requirement counts), the controls / checks tiles, the 90-day trend, the evidence that needs attention (stale,
// failing, error) and the collectors' heartbeat with the state of the published evidence file. Controls: the control
// table (worst first, filter by framework, search) and a drawer with the requirements a control covers, its checks and
// their evidence history. The fixed banner says it is a Dev readiness score, not a certification.
//
// API (backend api/routers/admin_platform_compliance.py; BFF /api/admin/platform/compliance, compliance.read; Refresh
// compliance.write — lib/rbac.ts): GET /, GET /controls, GET /controls/{id}, POST /refresh. Pure helpers:
// lib/compliance.ts. Kit only. The tab lives in the URL (?tab=controls) and so does the open control (?control=<id>).
//
// AA-1174: a third tab, Obligations (ObligationsTab.tsx: the law checklists and the applicability matrix; GET
// /obligations). Framework names and kinds come from the API (compliance.framework), so laws mapped to controls show
// their names in the Controls tab and its filter. Refresh evidence reports its result: a toast and a line under the
// header with the counts read from the published file and from the API's own sources, or the error.
//
// AA-1173: two more tabs, Exceptions and Reviews (GovernanceTabs.tsx: maker-checker exceptions with an expiry, the
// Security posture monthly review). A control with an approved, unexpired exception shows "Accepted risk" beside its
// real state (table and drawer); the Collectors section lists the sealed sources of the Object Lock evidence store
// (state, rows, hash chain), and an evidence row read from it carries a "sealed" tag in the drawer history.
//
// AA-1184: a sixth tab, Calendar (CalendarTab.tsx: the root ops calendar as the daily collector published it in the
// check ops_calendar_overdue; read-only; GET /calendar).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardCheck, ExternalLink, RefreshCw, SlidersHorizontal } from "lucide-react";
import { useMemo } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Badge, Button, CHART_TOOLTIP, DataTable, Drawer, EmptyState, ErrorState, Field, K, KpiTile, PageFrame, PageHeader,
  Section, Select, SkeletonStyle, Tabs, apiGet, apiSend, mono, useFormat, useT, useToast, useUrlFilters,
  type ColumnDef,
} from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import {
  CONTROLS_PATH, OVERVIEW_PATH, REFRESH_PATH, SETTINGS_HREF, checkTone, controlTone, deltaToReference,
  fmtPct, frameworkList, frameworkName, inFramework, isLegal, refreshCounts, safeLink, skipReason, sortControls, trendSeries,
  type CheckView, type ControlDetail, type ControlRow, type ControlsResp, type FrameworkInfo, type FrameworkScore,
  type Heartbeat, type IngestState, type LiveSourceState, type OverviewResp, type SealedState, sealedTone,
} from "@/lib/compliance";
import { COMPLIANCE } from "./messages";
import { ObligationsTab } from "./ObligationsTab";
import { ExceptionsTab, ReviewsTab } from "./GovernanceTabs";
import { GOVERNANCE } from "./governance.messages";
import { CalendarTab } from "./CalendarTab";
import { CALENDAR } from "./calendar.messages";

const KEY = ["platform-compliance"];
const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const grid = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 } as const;
const cards = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))", gap: 12 } as const;
const tiles = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 170px), 1fr))", gap: 10 } as const;
const linkStyle = { color: K.accentDeep, display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12 } as const;

function useCT() {
  return useT(COMPLIANCE);
}
type CT = ReturnType<typeof useCT>;

function stateLabel(t: CT, state: string | null | undefined): string {
  return state ? t(`state_${state}` as Parameters<CT>[0]) as string : "—";
}

// ── overview ────────────────────────────────────────────────────────────────────────────────────────────────────────

function FrameworkCard({ score, weight, info, t }: { score: FrameworkScore; weight: number;
  info?: FrameworkInfo[]; t: CT }) {
  const delta = deltaToReference(score);
  return (
    <Section title={score.title ?? frameworkName(score.framework, info)} testId={`compliance-card-${score.framework}`}
      description={(score.kind ? score.kind === "law" : isLegal(score.framework, info)) ? t("legalNote") : undefined}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <KpiTile size="sm" label={t("weighted")} value={fmtPct(score.weighted_pct)} info={t("weightedTip", weight)}
          hint={delta ?? t("noReference")} hintTone={delta?.startsWith("−") ? "warning" : "neutral"} />
        <KpiTile size="sm" label={t("strict")} value={fmtPct(score.strict_pct)} info={t("strictTip")}
          hint={`${t("reference")}: ${fmtPct(score.reference_pct)}`} />
      </div>
      <p style={{ ...small, margin: "8px 0 0" }}>
        {t("counts", score.n_met, score.n_partial, score.n_gap, score.n_stale, score.n_unmapped)}
      </p>
    </Section>
  );
}

function Trend({ data, t }: { data: OverviewResp; t: CT }) {
  const c = useChartColors({ s0: "--aa-accent", s1: "--aa-info", s2: "--aa-purple", s3: "--aa-green",
    s4: "--aa-kit-amber", s5: "--aa-red", axis: "--aa-muted", grid: "--aa-border" }) as Record<string, string>;
  const rows = useMemo(() => trendSeries(data.trend), [data.trend]);
  const dates = new Set(data.trend.map((p) => p.date));
  const series = data.frameworks.map((f) => ({ key: f.framework, name: f.title ?? frameworkName(f.framework) }));
  return (
    <Section title={t("trend")} description={data.settings_changed_on ? t("settingsChanged", data.settings_changed_on)
      : undefined}>
      {dates.size < 2 ? <p style={small}>{t("trendEmpty")}</p> : (
        <div style={{ width: "100%", height: 260 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke={c.grid} strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: c.axis }} />
              <YAxis domain={[0, 100]} width={36} tick={{ fontSize: 11, fill: c.axis }} />
              <Tooltip {...CHART_TOOLTIP} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {series.map((f, i) => (
                <Line key={f.key} type="monotone" dataKey={f.key} name={f.name} stroke={c[`s${i % 6}`]} strokeWidth={1.6}
                  dot={false} connectNulls isAnimationActive={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Section>
  );
}

function Attention({ rows, t }: { rows: CheckView[]; t: CT }) {
  const { formatDateTime } = useFormat();
  const columns: ColumnDef<CheckView, unknown>[] = [
    { id: "check", header: t("colCheck"), accessorKey: "check_id",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{r.check_id}</span> },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: ({ row: { original: r } }) => <Badge tone={checkTone(r.state)}>{stateLabel(t, r.state)}</Badge> },
    { id: "reason", header: t("colReason"), cell: ({ row: { original: r } }) => <span style={small}>{r.reason}</span> },
    { id: "collected", header: t("colCollected"), accessorKey: "collected_at",
      cell: ({ row: { original: r } }) => <span style={small}>{r.collected_at ? formatDateTime(r.collected_at) : "—"}</span> },
    { id: "controls", header: t("colControls"),
      cell: ({ row: { original: r } }) => <span style={{ ...small, fontFamily: mono }}>{(r.controls ?? []).join(", ")}</span> },
  ];
  return (
    <Section title={t("attention")}>
      <DataTable<CheckView> tableId="compliance-attention" data={rows} columns={columns} getRowId={(r) => r.check_id}
        paginate={false} searchable={false} tableMinWidth={760} emptyTitle={t("attentionEmpty")} />
    </Section>
  );
}

function ingestText(state: IngestState | null, t: CT): string | null {
  if (!state) return null;
  if (state.state === "ok") return t("ingestOk", state.rows ?? 0, state.new ?? 0);
  if (state.state === "missing") return t("ingestMissing");
  if (state.state === "invalid") return t("ingestInvalid");
  if (state.state === "superseded") return t("ingestSuperseded");
  return t("ingestUnavailable");
}

function useGT() {
  return useT(GOVERNANCE);
}
type GT = ReturnType<typeof useGT>;

function sealedText(s: SealedState, g: GT): string {
  if (s.state === "ok") return g("sealed_ok", s.rows ?? 0, s.chain === "linked" ? g("chain_linked") : g("chain_first"));
  return g(`sealed_${s.state}`) as string;
}

function Collectors({ beats, ingest, t }: { beats: Heartbeat[]; ingest: IngestState | null; t: CT }) {
  const { formatDateTime } = useFormat();
  const g = useGT();
  const text = ingestText(ingest, t);
  const sealed = Object.entries(ingest?.sealed ?? {});
  return (
    <Section title={t("heartbeat")} testId="compliance-heartbeat">
      <div style={{ display: "grid", gap: 8 }}>
        {beats.map((b) => (
          <div key={b.source} style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <span style={{ fontFamily: mono, fontSize: 12 }}>{b.source}</span>
            {b.scheduled === false
              ? <Badge tone={b.ok ? "success" : b.last_seen ? "warning" : "neutral"}>{t("heartbeatOnDemand",
                b.heartbeat_hours)}</Badge>
              : <Badge tone={b.ok ? "success" : b.last_seen ? "danger" : "neutral"}>
                {b.ok ? t("heartbeatOk", b.heartbeat_hours) : b.last_seen ? t("heartbeatLate", b.heartbeat_hours)
                  : t("heartbeatNever")}
              </Badge>}
            {b.last_seen && <span style={small}>{t("lastSeen", formatDateTime(b.last_seen))}</span>}
          </div>
        ))}
        {text && <p style={{ ...small, margin: 0 }}>{t("ingest")}: {text}
          {ingest?.dropped?.length ? ` ${t("dropped", ingest.dropped.length)}` : ""}</p>}
        {sealed.length > 0 && <div style={{ display: "grid", gap: 4 }} data-testid="compliance-sealed">
          <span style={{ ...small, fontWeight: 600 }}>{g("sealed")}</span>
          {sealed.map(([source, s]) => (
            <span key={source} style={{ ...small, display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              <span style={{ fontFamily: mono }}>{source}</span>
              <Badge tone={sealedTone(s.state)}>{sealedText(s, g)}</Badge>
              {s.sha256 && <span style={{ fontFamily: mono }} title={s.object ?? undefined}>{s.sha256.slice(0, 12)}</span>}
            </span>
          ))}
        </div>}
      </div>
    </Section>
  );
}

function Overview({ data, t }: { data: OverviewResp; t: CT }) {
  return (
    <div style={grid}>
      <div style={tiles}>
        <KpiTile size="sm" label={t("controlsTotal")} value={data.controls.total ?? 0} hint={t("register",
          data.register.requirements, data.register.controls, data.register.mappings)} />
        <KpiTile size="sm" label={t("controlsMet")} value={data.controls.met ?? 0} valueTone="success" />
        <KpiTile size="sm" label={t("checksFailing")} value={(data.checks.fail ?? 0) + (data.checks.error ?? 0)}
          valueTone={(data.checks.fail ?? 0) + (data.checks.error ?? 0) ? "danger" : undefined} />
        <KpiTile size="sm" label={t("checksStale")} value={data.checks.stale ?? 0}
          valueTone={data.checks.stale ? "warning" : undefined} />
      </div>
      <div style={cards}>
        {data.frameworks.map((f) => (
          <FrameworkCard key={f.framework} score={f} weight={data.settings.partial_weight} info={data.framework_info}
            t={t} />
        ))}
      </div>
      <Trend data={data} t={t} />
      <Attention rows={data.attention} t={t} />
      <Collectors beats={data.heartbeat} ingest={data.ingest} t={t} />
    </div>
  );
}

// ── controls ────────────────────────────────────────────────────────────────────────────────────────────────────────

function Controls({ t }: { t: CT }) {
  const { formatDate, formatDateTime } = useFormat();
  const g = useGT();
  const { get, setMany } = useUrlFilters();
  const framework = get("framework") || "all";
  const q = useQuery({ queryKey: [...KEY, "controls"], queryFn: () => apiGet<ControlsResp>(CONTROLS_PATH),
    staleTime: 60_000, retry: false });
  const rows = useMemo(() => sortControls(q.data?.controls ?? []).filter((r) => inFramework(r, framework)),
    [q.data, framework]);
  const info = q.data?.framework_info;
  const columns: ColumnDef<ControlRow, unknown>[] = [
    { id: "id", header: t("colControl"), accessorKey: "control_id",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{r.control_id}</span> },
    { id: "title", header: t("colTitle"), accessorKey: "title" },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: ({ row: { original: r } }) => <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
        <Badge tone={controlTone(r.state)}>{stateLabel(t, r.state)}</Badge>
        {r.exception && <span title={g("acceptedUntil", formatDate(r.exception.expires_on))}>
          <Badge tone="purple" dot={false}>{g("acceptedRisk")}</Badge></span>}</span> },
    { id: "domain", header: t("colDomain"), accessorKey: "domain" },
    { id: "owner", header: t("colOwner"), accessorKey: "owner_role" },
    { id: "frequency", header: t("colFrequency"), accessorKey: "frequency" },
    { id: "coverage", header: t("colCoverage"),
      cell: ({ row: { original: r } }) => <span style={small}>{Object.entries(r.frameworks)
        .map(([k, n]) => `${frameworkName(k, info)} ${n}`).join(" · ") || "—"}</span> },
    { id: "last", header: t("colLast"), accessorKey: "last_evidence_at",
      cell: ({ row: { original: r } }) => <span style={small}>{r.last_evidence_at ? formatDateTime(r.last_evidence_at)
        : "—"}</span> },
  ];
  const open = get("control");
  return (
    <div style={grid}>
      <div style={{ maxWidth: 320 }}>
        <Field label={t("filterFramework")}>
          <Select value={framework} onChange={(e) => setMany({ framework: e.target.value === "all" ? null : e.target.value })}
            options={[{ value: "all", label: t("allFrameworks") },
              ...frameworkList(info).map((f) => ({ value: f.key, label: f.name }))]} />
        </Field>
      </div>
      <DataTable<ControlRow> tableId="compliance-controls" data={rows} columns={columns}
        getRowId={(r) => r.control_id} loading={q.isLoading} error={q.error ? (q.error as Error).message : null}
        onRetry={() => void q.refetch()} searchable searchLabel={t("searchControls")} paginate={false}
        tableMinWidth={980} enableCsv csvFilename="compliance-controls.csv" emptyTitle={t("noControls")}
        onRowClick={(r) => setMany({ control: r.control_id })} />
      <ControlDrawer id={open || null} onClose={() => setMany({ control: null })} info={info} t={t} />
    </div>
  );
}

function ControlDrawer({ id, onClose, info, t }: { id: string | null; onClose: () => void;
  info?: FrameworkInfo[]; t: CT }) {
  const { formatDate, formatDateTime } = useFormat();
  const g = useGT();
  const q = useQuery({ queryKey: [...KEY, "control", id], enabled: !!id, retry: false,
    queryFn: () => apiGet<ControlDetail>(`${CONTROLS_PATH}/${encodeURIComponent(id ?? "")}`) });
  const d = q.data;
  return (
    <Drawer open={!!id} onClose={onClose} width={640} testId="compliance-control-drawer"
      title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
        <span style={{ fontFamily: mono }}>{id}</span>
        {d && <Badge tone={controlTone(d.state)}>{stateLabel(t, d.state)}</Badge>}</span>}>
      {q.error && <ErrorState message={(q.error as Error).message} onRetry={() => void q.refetch()} />}
      {d && (
        <div style={grid}>
          <div>
            <strong>{d.title}</strong>
            {d.intent && <p style={{ ...small, margin: "4px 0 0" }}>{t("intent")}: {d.intent}</p>}
            <p style={{ ...small, margin: "4px 0 0" }}>{d.domain} · {d.owner_role} · {d.frequency} · {d.test_type}</p>
            {d.exception && <p style={{ ...small, margin: "6px 0 0" }} data-testid="compliance-control-exception">
              <Badge tone="purple" dot={false}>{g("acceptedUntil", formatDate(d.exception.expires_on))}</Badge>{" "}
              #{d.exception.exception_id} {d.exception.title}</p>}
          </div>
          <Section title={t("requirements")}>
            <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }}>
              {d.requirements.map((r) => (
                <li key={`${r.framework}-${r.ref}`} style={small}>
                  <span style={{ fontFamily: mono }}>{frameworkName(r.framework, info)} {r.ref}</span>
                  {r.title ? ` — ${r.title}` : ""} ({r.coverage === "full" ? t("full") : t("partial")})
                  {r.state && <> <Badge tone={controlTone(r.state)}>{stateLabel(t, r.state)}</Badge></>}
                </li>
              ))}
            </ul>
          </Section>
          <Section title={t("checks")}>
            {!d.checks.length && <p style={small}>{t("noChecks")}</p>}
            <div style={{ display: "grid", gap: 12 }}>
              {d.checks.map((c) => (
                <div key={c.check_id}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span style={{ fontFamily: mono, fontSize: 12 }}>{c.check_id}</span>
                    <Badge tone={checkTone(c.state)}>{stateLabel(t, c.state)}</Badge>
                    <span style={small}>{c.required ? t("required") : t("optional")}</span>
                    {c.expires_at && <span style={small}>{t("expires", formatDateTime(c.expires_at))}</span>}
                  </div>
                  {c.reason && <p style={{ ...small, margin: "2px 0 0" }}>{c.reason}</p>}
                  {!c.history.length ? <p style={{ ...small, margin: "2px 0 0" }}>{t("noHistory")}</p> : (
                    <ul style={{ margin: "4px 0 0", paddingLeft: 18, display: "grid", gap: 2 }}>
                      {c.history.map((h) => {
                        const href = safeLink(h.link);
                        return (
                          <li key={`${h.source}-${h.collected_at}`} style={small}>
                            {formatDateTime(h.collected_at)} · <Badge tone={checkTone(h.status)}>{stateLabel(t, h.status)}</Badge>
                            {h.summary ? ` ${h.summary}` : ""}{h.run_ref ? ` · ${t("runRef", h.run_ref)}` : ""}
                            {h.object_sha256 && <span title={h.object_key ?? undefined}> · <Badge tone="success"
                              dot={false}>{g("sealedTag")}</Badge></span>}
                            {href && <> · <a href={href} target="_blank" rel="noreferrer" style={linkStyle}>
                              {t("open")}<ExternalLink size={11} /></a></>}
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          </Section>
        </div>
      )}
    </Drawer>
  );
}

// ── refresh result ──────────────────────────────────────────────────────────────────────────────────────────────────

function sourceLine(source: string, s: LiveSourceState, t: CT): string {
  const name = source === "cis_db" || source === "crm_ops" ? t(`source_${source}`) as string : source;
  if (s.state === "ok") return t("refreshSource", name, s.rows ?? 0, s.new ?? 0);
  if (s.state === "recent") return t("refreshRecent", name);
  if (s.state === "skipped") return t("refreshSkipped", name, t(`skip_${skipReason(s.reason, s.http)}`) as string);
  return t("refreshSourceError", name);
}

/** The lines the Refresh action reports: the published file, then each of the API's own sources. */
function refreshLines(state: IngestState, t: CT): string[] {
  const c = refreshCounts(state);
  const head = state.state === "ok" ? t("refreshOk", c.read, c.fresh)
    : state.state === "skipped" ? t("refreshFileSkipped", t(`skip_${skipReason(state.reason)}`) as string)
      : ingestText(state, t) ?? "";
  const lines = [head, ...Object.entries(state.live ?? {}).map(([source, s]) => sourceLine(source, s, t))];
  return lines.filter(Boolean);
}

function RefreshResult({ state, error, t }: { state: IngestState | null; error: string | null; t: CT }) {
  if (!state && !error) return null;
  const tone = error ? "error" : refreshCounts(state).tone;
  return (
    <div role="status" data-testid="compliance-refresh-result" data-tone={tone} style={{ ...small, display: "grid",
      gap: 2, marginBottom: 10 }}>
      <span><Badge tone={tone === "ok" ? "success" : tone === "warning" ? "warning" : "danger"}>
        {error ? t("refreshFailed") : tone === "ok" ? t("refresh") : tone === "warning" ? t("refreshWarnings")
          : t("refreshProblems")}</Badge></span>
      {error ? <span>{error}</span> : refreshLines(state as IngestState, t).map((l) => <span key={l}>{l}</span>)}
    </div>
  );
}

// ── page ────────────────────────────────────────────────────────────────────────────────────────────────────────────

const TABS = ["overview", "controls", "obligations", "exceptions", "reviews", "calendar"] as const;
type TabKey = (typeof TABS)[number];

function tabOf(value: string | null | undefined): TabKey {
  return (TABS as readonly string[]).includes(value ?? "") ? value as TabKey : "overview";
}

function OverviewTab({ t }: { t: CT }) {
  const q = useQuery({ queryKey: [...KEY, "overview"], queryFn: () => apiGet<OverviewResp>(OVERVIEW_PATH),
    staleTime: 60_000, retry: false });
  return (
    <>
      {q.error && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
        onRetry={() => void q.refetch()} />}
      {!q.data && !q.error && <KpiTile label={t("weighted")} value={undefined} />}
      {q.data && !q.data.frameworks.length && <EmptyState title={t("loadError")} />}
      {q.data && <Overview data={q.data} t={t} />}
    </>
  );
}

export default function CompliancePage() {
  const t = useCT();
  const g = useGT();
  const cal = useT(CALENDAR);
  const qc = useQueryClient();
  const { get, setMany } = useUrlFilters();
  const toast = useToast();
  const tab = tabOf(get("tab"));
  const refresh = useMutation({
    mutationFn: () => apiSend<{ ingest: IngestState }>(REFRESH_PATH, { method: "POST" }),
    onSuccess: (res) => {
      const lines = refreshLines(res.ingest, t);
      const tone = refreshCounts(res.ingest).tone;
      if (tone === "ok") toast.success(lines.join(" · "));
      else if (tone === "warning") toast.info(`${t("refreshWarnings")}: ${lines.join(" · ")}`);   // no warning tone
      else toast.error(`${t("refreshProblems")}: ${lines.join(" · ")}`);
    },
    onError: (err) => toast.error(`${t("refreshFailed")}: ${(err as Error).message}`),
    onSettled: () => void qc.invalidateQueries({ queryKey: KEY }),
  });
  return (
    <PageFrame>
      <SkeletonStyle />
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <ClipboardCheck size={18} /> {t("title")} <Badge tone="warning">{t("devNumbers")}</Badge></span>}
        description={t("description")}
        actions={<span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
          <a href={SETTINGS_HREF} style={linkStyle}><SlidersHorizontal size={13} /> {t("settings")}</a>
          <span title={t("refreshHint")}>
            <Button size="sm" variant="secondary" onClick={() => refresh.mutate()} disabled={refresh.isPending}
              testId="compliance-refresh"><RefreshCw size={13} /> {t("refresh")}</Button>
          </span>
        </span>}
      />
      <RefreshResult state={refresh.data?.ingest ?? null} error={refresh.error ? (refresh.error as Error).message : null}
        t={t} />
      <div role="note" data-testid="compliance-banner" style={{ ...small, marginBottom: 14, display: "grid", gap: 4 }}>
        <span><Badge tone="warning">{t("banner")}</Badge></span>
        <span>{t("bannerNote")}</span>
      </div>
      <div style={grid}>
        <Tabs ariaLabel={t("title")} active={tab} testId="compliance-tabs"
          onChange={(k) => setMany({ tab: k === "overview" ? null : k, control: null, law: null, cal: null })}
          tabs={[{ key: "overview", label: t("tabOverview") }, { key: "controls", label: t("tabControls") },
            { key: "obligations", label: t("tabObligations") }, { key: "exceptions", label: g("tabExceptions") },
            { key: "reviews", label: g("tabReviews") }, { key: "calendar", label: cal("tabCalendar") }]} />
        {tab === "obligations" ? <ObligationsTab /> : tab === "controls" ? <Controls t={t} />
          : tab === "exceptions" ? <ExceptionsTab /> : tab === "reviews" ? <ReviewsTab />
            : tab === "calendar" ? <CalendarTab /> : <OverviewTab t={t} />}
      </div>
    </PageFrame>
  );
}
