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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardCheck, ExternalLink, RefreshCw, SlidersHorizontal } from "lucide-react";
import { useMemo } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Badge, Button, CHART_TOOLTIP, DataTable, Drawer, EmptyState, ErrorState, Field, K, KpiTile, PageFrame, PageHeader,
  Section, Select, SkeletonStyle, Tabs, apiGet, apiSend, mono, useFormat, useT, useUrlFilters, type ColumnDef,
} from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import {
  CONTROLS_PATH, FRAMEWORKS, OVERVIEW_PATH, REFRESH_PATH, SETTINGS_HREF, checkTone, controlTone, deltaToReference,
  fmtPct, frameworkName, inFramework, isLegal, safeLink, sortControls, trendSeries,
  type CheckView, type ControlDetail, type ControlRow, type ControlsResp, type FrameworkScore, type Heartbeat,
  type IngestState, type OverviewResp,
} from "@/lib/compliance";
import { COMPLIANCE } from "./messages";

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

function FrameworkCard({ score, weight, t }: { score: FrameworkScore; weight: number; t: CT }) {
  const delta = deltaToReference(score);
  return (
    <Section title={frameworkName(score.framework)} testId={`compliance-card-${score.framework}`}
      description={isLegal(score.framework) ? t("legalNote") : undefined}>
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
              {FRAMEWORKS.map((f, i) => (
                <Line key={f.key} type="monotone" dataKey={f.key} name={f.name} stroke={c[`s${i}`]} strokeWidth={1.6}
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
  return t("ingestUnavailable");
}

function Collectors({ beats, ingest, t }: { beats: Heartbeat[]; ingest: IngestState | null; t: CT }) {
  const { formatDateTime } = useFormat();
  const text = ingestText(ingest, t);
  return (
    <Section title={t("heartbeat")} testId="compliance-heartbeat">
      <div style={{ display: "grid", gap: 8 }}>
        {beats.map((b) => (
          <div key={b.source} style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <span style={{ fontFamily: mono, fontSize: 12 }}>{b.source}</span>
            <Badge tone={b.ok ? "success" : b.last_seen ? "danger" : "neutral"}>
              {b.ok ? t("heartbeatOk", b.heartbeat_hours) : b.last_seen ? t("heartbeatLate", b.heartbeat_hours)
                : t("heartbeatNever")}
            </Badge>
            {b.last_seen && <span style={small}>{t("lastSeen", formatDateTime(b.last_seen))}</span>}
          </div>
        ))}
        {text && <p style={{ ...small, margin: 0 }}>{t("ingest")}: {text}
          {ingest?.dropped?.length ? ` ${t("dropped", ingest.dropped.length)}` : ""}</p>}
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
          <FrameworkCard key={f.framework} score={f} weight={data.settings.partial_weight} t={t} />
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
  const { formatDateTime } = useFormat();
  const { get, setMany } = useUrlFilters();
  const framework = get("framework") || "all";
  const q = useQuery({ queryKey: [...KEY, "controls"], queryFn: () => apiGet<ControlsResp>(CONTROLS_PATH),
    staleTime: 60_000, retry: false });
  const rows = useMemo(() => sortControls(q.data?.controls ?? []).filter((r) => inFramework(r, framework)),
    [q.data, framework]);
  const columns: ColumnDef<ControlRow, unknown>[] = [
    { id: "id", header: t("colControl"), accessorKey: "control_id",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{r.control_id}</span> },
    { id: "title", header: t("colTitle"), accessorKey: "title" },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: ({ row: { original: r } }) => <Badge tone={controlTone(r.state)}>{stateLabel(t, r.state)}</Badge> },
    { id: "domain", header: t("colDomain"), accessorKey: "domain" },
    { id: "owner", header: t("colOwner"), accessorKey: "owner_role" },
    { id: "frequency", header: t("colFrequency"), accessorKey: "frequency" },
    { id: "coverage", header: t("colCoverage"),
      cell: ({ row: { original: r } }) => <span style={small}>{Object.entries(r.frameworks)
        .map(([k, n]) => `${frameworkName(k)} ${n}`).join(" · ") || "—"}</span> },
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
              ...FRAMEWORKS.map((f) => ({ value: f.key, label: f.name }))]} />
        </Field>
      </div>
      <DataTable<ControlRow> tableId="compliance-controls" data={rows} columns={columns}
        getRowId={(r) => r.control_id} loading={q.isLoading} error={q.error ? (q.error as Error).message : null}
        onRetry={() => void q.refetch()} searchable searchLabel={t("searchControls")} paginate={false}
        tableMinWidth={980} enableCsv csvFilename="compliance-controls.csv" emptyTitle={t("noControls")}
        onRowClick={(r) => setMany({ control: r.control_id })} />
      <ControlDrawer id={open || null} onClose={() => setMany({ control: null })} t={t} />
    </div>
  );
}

function ControlDrawer({ id, onClose, t }: { id: string | null; onClose: () => void; t: CT }) {
  const { formatDateTime } = useFormat();
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
          </div>
          <Section title={t("requirements")}>
            <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }}>
              {d.requirements.map((r) => (
                <li key={`${r.framework}-${r.ref}`} style={small}>
                  <span style={{ fontFamily: mono }}>{frameworkName(r.framework)} {r.ref}</span>
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

// ── page ────────────────────────────────────────────────────────────────────────────────────────────────────────────

export default function CompliancePage() {
  const t = useCT();
  const qc = useQueryClient();
  const { get, setMany } = useUrlFilters();
  const tab = get("tab") === "controls" ? "controls" : "overview";
  const q = useQuery({ queryKey: [...KEY, "overview"], queryFn: () => apiGet<OverviewResp>(OVERVIEW_PATH),
    staleTime: 60_000, retry: false });
  const refresh = useMutation({
    mutationFn: () => apiSend<{ ingest: IngestState }>(REFRESH_PATH, { method: "POST" }),
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
      <div role="note" data-testid="compliance-banner" style={{ ...small, marginBottom: 14, display: "grid", gap: 4 }}>
        <span><Badge tone="warning">{t("banner")}</Badge></span>
        <span>{t("bannerNote")}</span>
      </div>
      <div style={grid}>
        <Tabs ariaLabel={t("title")} active={tab} testId="compliance-tabs"
          onChange={(k) => setMany({ tab: k === "overview" ? null : k, control: null })}
          tabs={[{ key: "overview", label: t("tabOverview") }, { key: "controls", label: t("tabControls") }]} />
        {tab === "controls" ? <Controls t={t} /> : (
          <>
            {q.error && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
              onRetry={() => void q.refetch()} />}
            {!q.data && !q.error && <KpiTile label={t("weighted")} value={undefined} />}
            {q.data && !q.data.frameworks.length && <EmptyState title={t("loadError")} />}
            {q.data && <Overview data={q.data} t={t} />}
          </>
        )}
      </div>
    </PageFrame>
  );
}
