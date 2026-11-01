"use client";
// app/admin/decisions/page.tsx — AA-660 Jev (TypeSafe) decision layer, v2 (S203 feedback: explain every
// term, filters + sort, sticky tabs/headers, one column per fact, solid status colours, fuller stats).
// API: /admin/decisions/{summary,log,questions/{key}}. Jev spend also appears in Cost Operations (Jev tab).
// AA-994: text in the UI language (messages.ts); stage / question keys, question texts and API text stay as sent.

import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, RefreshCw } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Button, PageFrame, PageFrameBody, PageFrameHeader, PageHeader, useFormat, useT, Input, Select,
  K, sans, mono, Section, Eyebrow, LoadingScreen, CHART_TOOLTIP,
  DataTable, Tabs, TONE, type Tone, type ColumnDef,
  Badge,
} from "../../_kit";
import { useChartColors } from "../../_kit/useTheme";
import { stripIssueIds } from "@/lib/issueIds";
import { traceHref } from "@/lib/aiGateway";
import { pageSortProps } from "@/lib/tableSort";
import { DEC } from "./messages";

// ── types ──────────────────────────────────────────────────────────────────────────────────────
type Mode = "off" | "shadow" | "enforce";
type Zone = "accept" | "grey" | "reject" | "error" | "skipped";

interface Question {
  question_key: string; stage: string; kind: string; instructions: string; criteria: unknown; mode: Mode;
  accept_floor: number | null; reject_ceiling: number | null; threshold_version: number;
  calibration_ref: string | null; notes: string | null; updated_at: string; updated_by: string;
  verdicts: number; accept: number; grey: number; reject: number; error: number; skipped: number;
  acted: number; cached: number; cost_usd: number; avg_latency_ms: number | null; p50_latency_ms: number | null;
  p95_latency_ms: number | null; avg_probability: number | null; first_used_at: string | null; last_used_at: string | null;
}
interface StageRow {
  stage: string; questions: number; verdicts: number; acted: number; errors: number; skipped: number; cached: number;
  cost_usd: number; avg_latency_ms: number | null; last_used_at: string | null;
}
interface DayRow { day: string; verdicts: number; accept: number; grey: number; reject: number; error: number; skipped: number; cost_usd: number }
interface Summary {
  days: number; questions: Question[]; stages: StageRow[]; daily: DayRow[];
  calls_by_stage: { stage: string; calls: number; cost_usd: number; tokens_in: number }[];
  unregistered: { stage: string; question_key: string; verdicts: number; cost_usd: number }[];
  total_cost_usd: number; total_calls: number;
  tenant_allowlist: { tenant_id: string; slug: string; name: string; reason: string }[];
}
interface Verdict {
  id: number; created_at: string; stage: string; question_key: string; subject_key: string;
  tenant_slug: string | null; job_id: string | null; mode: Mode; zone: Zone; probability: number | null;
  choice: string | null; threshold_version: number | null; latency_ms: number | null; cost_usd: number;
  error: string | null; outcome: string | null; cached?: boolean;
}

// ── meaning of every status, shown in the Guide and as tooltips ────────────────────────────────
const ZONES: Zone[] = ["accept", "grey", "reject", "error", "skipped"];
// Labels and help texts are message keys (messages.ts DEC): `${label}` and `${label}Help`.
// AA-1006: each status is a kit tone (Badge pill, text colour from the same tone); `cssVar` stays for the chart, which
// resolves the variable to a concrete colour for its SVG fills.
const ZONE_META: Record<Zone, { label: "zoneAccept" | "zoneGrey" | "zoneReject" | "zoneError" | "zoneSkipped"; tone: Tone; cssVar: string }> = {
  accept:  { label: "zoneAccept",  tone: "success", cssVar: "--aa-green-deep" },
  grey:    { label: "zoneGrey",    tone: "neutral", cssVar: "--aa-neutral-fg2" },
  reject:  { label: "zoneReject",  tone: "danger",  cssVar: "--aa-red-strong" },
  error:   { label: "zoneError",   tone: "warning", cssVar: "--aa-amber-strong" },
  skipped: { label: "zoneSkipped", tone: "info",    cssVar: "--aa-blue-link" },
};
const MODE_META: Record<Mode, { label: "modeOff" | "modeShadow" | "modeEnforce"; tone: Tone }> = {
  off:     { label: "modeOff",     tone: "neutral" },
  shadow:  { label: "modeShadow",  tone: "info" },
  enforce: { label: "modeEnforce", tone: "warning" },
};
/** The text colour of a tone (the Badge foreground). */
const toneText = (tone: Tone) => TONE[tone].fg;
const KIND_HELP: Record<string, "kindNoul" | "kindChoice" | "kindScore"> = {
  noul: "kindNoul",
  choice: "kindChoice",
  score: "kindScore",
};

// ── small UI pieces ────────────────────────────────────────────────────────────────────────────
const usd = (v: number | null | undefined) => (v == null ? "—" : v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(3)}`);
// AA-601: a count that arrives null/undefined (a stage seen only in the cache-hit rollup, or a
// FILTERed sum that was NULL) must count as 0 — summing it directly would make the whole total NaN
// (the "Acted on = NaN" bug). The backend now zero-fills every aggregate; this is the FE guard so
// one bad row can never poison a reduce again.
const n0 = (v: number | null | undefined): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const num = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));
const pct = (a: number, b: number) => (b ? `${Math.round((a / b) * 100)}%` : "—");

function Pill({ tone, children, title }: { tone: Tone; children: React.ReactNode; title?: string }) {
  return (
    <span title={title} style={{ display: "inline-flex", whiteSpace: "nowrap", cursor: title ? "help" : "default" }}>
      <Badge tone={tone} dot={false}>{children}</Badge>
    </span>
  );
}
function ZonePill({ z }: { z: Zone }) {
  const t = useT(DEC);
  const k = ZONE_META[z].label;
  return <Pill tone={ZONE_META[z].tone} title={t(`${k}Help`)}>{t(k)}</Pill>;
}
function ModePill({ m }: { m: Mode }) {
  const t = useT(DEC);
  const k = MODE_META[m].label;
  return <Pill tone={MODE_META[m].tone} title={t(`${k}Help`)}>{t(k)}</Pill>;
}

function Kpi({ label, value, sub, color = K.ink }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: K.muted }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, color, marginTop: 4, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: K.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

const TABLE_BOX_HEIGHT = "62vh";
const input: React.CSSProperties = { padding: "7px 9px", border: `1px solid ${K.line}`, borderRadius: 7, fontSize: 12.5, fontFamily: sans, background: K.card };

/** The page's own sort as a kit DataTable controlled sort (the page sorts, or asks the server to). As the old sort
 * headers did: a click on the sorted column flips it, a click on another column sorts it descending first. */
function tableSort<K extends string>(sort: { key: K; dir: "asc" | "desc" }, set: (s: { key: K; dir: "asc" | "desc" }) => void) {
  return pageSortProps(sort, set, () => "desc");
}

/** A column with a head tooltip; `sortable` when the page's sort knows the key. */
function dcol<T>(id: string, header: string, cell: (r: T) => React.ReactNode,
  opts: { sortable?: boolean; title?: string; style?: (r: T) => React.CSSProperties } = {}): ColumnDef<T, unknown> {
  return {
    id,
    header: opts.title ? () => <span title={opts.title}>{header}</span> : header,
    enableSorting: !!opts.sortable,
    // TanStack enables a header only for a column with an accessor; the page sorts the rows itself (manualSorting).
    ...(opts.sortable ? { accessorFn: (r: T) => (r as Record<string, unknown>)[id] } : {}),
    cell: ({ row }) => <span style={opts.style?.(row.original)}>{cell(row.original)}</span>,
  };
}

function ZoneBar({ counts }: { counts: Record<Zone, number> }) {
  const t = useT(DEC);
  const total = ZONES.reduce((n, z) => n + counts[z], 0);
  if (!total) return <span style={{ color: K.muted2, fontSize: 12 }}>{t("noVerdictsYet")}</span>;
  return (
    <div>
      <div style={{ display: "flex", height: 12, borderRadius: 6, overflow: "hidden", background: K.line }}>
        {ZONES.map(z => counts[z] ? <div key={z} title={`${t(ZONE_META[z].label)}: ${counts[z]}`}
          style={{ width: `${(counts[z] / total) * 100}%`, background: toneText(ZONE_META[z].tone) }} /> : null)}
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 6, fontSize: 12 }}>
        {ZONES.map(z => (
          <span key={z} style={{ display: "inline-flex", alignItems: "center", gap: 5 }} title={t(`${ZONE_META[z].label}Help`)}>
            <span style={{ width: 10, height: 10, borderRadius: 3, background: toneText(ZONE_META[z].tone) }} />
            {t(ZONE_META[z].label)} <b>{counts[z]}</b> <span style={{ color: K.muted }}>({pct(counts[z], total)})</span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ── Guide ──────────────────────────────────────────────────────────────────────────────────────
function Guide() {
  const t = useT(DEC);
  const term = (name: string, d: React.ReactNode) => (
    <div style={{ padding: "10px 0", borderTop: `1px solid ${K.line}` }}>
      <div style={{ fontWeight: 700, color: K.ink, fontSize: 13 }}>{name}</div>
      <div style={{ fontSize: 12.5, color: K.body, marginTop: 3, lineHeight: 1.55 }}>{d}</div>
    </div>
  );
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))", gap: 16 }}>
      <Section>
        <Eyebrow>{t("whatThisPageIs")}</Eyebrow>
        <p style={{ fontSize: 13, lineHeight: 1.6, margin: 0 }}>
          <b>Jev</b> {t("g1a")} <i>{t("g1q")}</i> {t("g1b")} <b>{t("g1c")}</b>{t("g1d")} <b>{t("g1e")}</b>{t("g1f")}
        </p>
        <Eyebrow style={{ marginTop: 14 }}>{t("howAction")}</Eyebrow>
        <ol style={{ fontSize: 12.5, lineHeight: 1.7, margin: 0, paddingLeft: 18 }}>
          <li>{t("li1")}</li>
          <li>{t("li2a")} <b>{t("li2b")}</b> {t("li2c")} <ZonePill z="accept" />{t("li2d")} <ZonePill z="reject" />{t("li2e")} <ZonePill z="grey" />.</li>
          <li>{t("li3a")} <ModePill m="enforce" /> {t("li3b")} {t("li3d")}</li>
        </ol>
      </Section>
      <Section>
        <Eyebrow>{t("terms")}</Eyebrow>
        {term(t("termStage"), t("termStageHelp"))}
        {term(t("termQuestion"), t("termQuestionHelp"))}
        {term(t("termType"), Object.entries(KIND_HELP).map(([k, v]) => <div key={k}><b>{k}</b>: {t(v)}</div>))}
        {term(t("termMode"), (["off", "shadow", "enforce"] as Mode[]).map(m => <div key={m} style={{ marginTop: 3 }}><ModePill m={m} /> {t(`${MODE_META[m].label}Help`)}</div>))}
      </Section>
      <Section>
        <Eyebrow>{t("zonesTitle")}</Eyebrow>
        {ZONES.map(z => <div key={z} style={{ padding: "6px 0" }}><ZonePill z={z} /> <span style={{ fontSize: 12.5 }}>{t(`${ZONE_META[z].label}Help`)}</span></div>)}
        {term(t("termFloors"), t("termFloorsHelp"))}
        {term(t("termCalibration"), t("termCalibrationHelp"))}
        {term(t("termVerdict"), t("termVerdictHelp"))}
        {term(t("termCached"), t("termCachedHelp"))}
        {term(t("termActed"), t("termActedHelp"))}
        {term(t("termCost"), t("termCostHelp"))}
      </Section>
    </div>
  );
}

// ── Question editor ────────────────────────────────────────────────────────────────────────────
function QuestionEditor({ q, onSaved }: { q: Question; onSaved: () => void }) {
  const t = useT(DEC);
  const [mode, setMode] = useState<Mode>(q.mode);
  const [accept, setAccept] = useState(q.accept_floor?.toString() ?? "");
  const [reject, setReject] = useState(q.reject_ceiling?.toString() ?? "");
  const [calib, setCalib] = useState(q.calibration_ref ?? "");
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const toNum = (s: string) => (s.trim() === "" ? null : Number(s));
  async function save() {
    if (mode === "enforce" && !window.confirm(t("confirmEnforce", q.question_key, q.stage))) return;
    setSaving(true); setErr(null);
    try {
      const r = await fetch(`/api/admin/decisions/questions/${encodeURIComponent(q.question_key)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, accept_floor: toNum(accept), reject_ceiling: toNum(reject), calibration_ref: calib.trim() || null }),
      });
      if (!r.ok) { const b = await r.json().catch(() => ({})); setErr(String(b.detail ?? r.status)); return; }
      onSaved();
    } finally { setSaving(false); }
  }
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
      <label style={{ fontSize: 11.5, color: K.muted }}>{t("mode")}<br />
        <Select value={mode} onChange={e => setMode(e.target.value as Mode)} style={input}>
          <option value="off">{t("modeOff")}</option><option value="shadow">{t("modeShadow")}</option><option value="enforce">{t("modeEnforce")}</option>
        </Select>
      </label>
      <label style={{ fontSize: 11.5, color: K.muted }}>{t("acceptFloor")}<br /><Input value={accept} onChange={e => setAccept(e.target.value)} placeholder="0.95" style={{ ...input, width: 90 }} /></label>
      <label style={{ fontSize: 11.5, color: K.muted }}>{t("rejectCeiling")}<br /><Input value={reject} onChange={e => setReject(e.target.value)} placeholder="0.30" style={{ ...input, width: 90 }} /></label>
      <label style={{ fontSize: 11.5, color: K.muted, flex: 1, minWidth: 240 }}>{t("calibrationRecord")}<br />
        <Input value={calib} onChange={e => setCalib(e.target.value)} placeholder="docs/calibration/<question>.md" style={{ ...input, width: "100%" }} />
      </label>
      <Button variant="primary" onClick={save} disabled={saving}>{saving ? t("saving") : t("save")}</Button>
      {err && <div style={{ width: "100%", color: K.danger, fontSize: 12 }}>{err}</div>}
    </div>
  );
}

// ── page ───────────────────────────────────────────────────────────────────────────────────────
type Tab = "overview" | "questions" | "verdicts" | "guide";
type QSortKey = "stage" | "question_key" | "mode" | "verdicts" | "acted" | "reject" | "error" | "cost_usd" | "avg_latency_ms" | "last_used_at";
type SSortKey = "stage" | "verdicts" | "acted" | "errors" | "cost_usd" | "avg_latency_ms" | "last_used_at";
type VSortKey = "created_at" | "probability" | "latency_ms" | "cost_usd";
const PAGE = 50;

export default function DecisionsPage() {
  const t = useT(DEC);
  const { formatDateTime: when } = useFormat();
  const [tab, setTab] = useState<Tab>("overview");
  const [days, setDays] = useState(7);
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  // AA-601 part B — recharts fill/stroke are SVG attributes; resolve zone colours + grid to concrete
  // values for portability (Chromium resolves var() in SVG attrs, other engines/exports do not).
  const chartColors = useChartColors({
    accept: ZONE_META.accept.cssVar, grey: ZONE_META.grey.cssVar, reject: ZONE_META.reject.cssVar,
    error: ZONE_META.error.cssVar, skipped: ZONE_META.skipped.cssVar, line: "--aa-line",
  });
  // questions tab
  const [qSearch, setQSearch] = useState(""); const [qStage, setQStage] = useState(""); const [qMode, setQMode] = useState("");
  const [qSort, setQSort] = useState<{ key: QSortKey; dir: "asc" | "desc" }>({ key: "stage", dir: "asc" });
  const [open, setOpen] = useState<string | null>(null);
  const [sSort, setSSort] = useState<{ key: SSortKey; dir: "asc" | "desc" }>({ key: "verdicts", dir: "desc" });
  // verdicts tab
  const [vStage, setVStage] = useState(""); const [vQuestion, setVQuestion] = useState(""); const [vZone, setVZone] = useState("");
  const [vMode, setVMode] = useState(""); const [vTenant, setVTenant] = useState(""); const [vSubject, setVSubject] = useState("");
  const [vSort, setVSort] = useState<{ key: VSortKey; dir: "asc" | "desc" }>({ key: "created_at", dir: "desc" });
  const [vPage, setVPage] = useState(0);
  const [log, setLog] = useState<{ rows: Verdict[]; total: number }>({ rows: [], total: 0 });

  const loadSummary = useCallback(async () => {
    try {
      const r = await fetch(`/api/admin/decisions/summary?days=${days}`);
      if (!r.ok) throw r.status;
      setData(await r.json()); setError(null);
    } catch (e) { setError(t("loadSummaryError", String(e))); }
  }, [days, t]);

  const loadLog = useCallback(async () => {
    const qs = new URLSearchParams({ days: String(days), limit: String(PAGE), offset: String(vPage * PAGE), sort: vSort.key, direction: vSort.dir });
    if (vStage) qs.set("stage", vStage); if (vQuestion) qs.set("question_key", vQuestion); if (vZone) qs.set("zone", vZone);
    if (vMode) qs.set("mode", vMode); if (vTenant) qs.set("tenant", vTenant); if (vSubject) qs.set("subject", vSubject);
    try {
      const r = await fetch(`/api/admin/decisions/log?${qs}`);
      if (!r.ok) throw r.status;
      const d = await r.json(); setLog({ rows: d.decisions ?? [], total: d.total ?? 0 });
    } catch (e) { setError(t("loadVerdictsError", String(e))); }
  }, [days, vStage, vQuestion, vZone, vMode, vTenant, vSubject, vSort, vPage, t]);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on window change, same pattern as every admin page
  useEffect(() => { loadSummary(); }, [loadSummary]);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on filter change
  useEffect(() => { if (tab === "verdicts") loadLog(); }, [tab, loadLog]);

  const qs = useMemo(() => data?.questions ?? [], [data]);
  const openQ = qs.find(q => q.question_key === open) ?? null;
  const stagesAll = useMemo(() => Array.from(new Set([...(data?.stages ?? []).map(s => s.stage), ...qs.map(q => q.stage)])).sort(), [data, qs]);
  const totals = useMemo(() => {
    const sum = { accept: 0, grey: 0, reject: 0, error: 0, skipped: 0 } as Record<Zone, number>;
    for (const d of data?.daily ?? []) for (const z of ZONES) sum[z] += n0(d[z]);
    return sum;
  }, [data]);
  const verdictTotal = ZONES.reduce((n, z) => n + n0(totals[z]), 0);
  const acted = (data?.stages ?? []).reduce((n, s) => n + n0(s.acted), 0);
  const latencies = (data?.stages ?? []).filter(s => s.avg_latency_ms != null);
  const avgLatency = latencies.length ? latencies.reduce((n, s) => n + n0(s.avg_latency_ms) * n0(s.verdicts), 0) / Math.max(1, latencies.reduce((n, s) => n + n0(s.verdicts), 0)) : null;

  const filteredQs = useMemo(() => {
    const s = qSearch.toLowerCase();
    const rows = qs.filter(q => (!qStage || q.stage === qStage) && (!qMode || q.mode === qMode)
      && (!s || q.question_key.toLowerCase().includes(s) || q.instructions.toLowerCase().includes(s)));
    const dir = qSort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const x = a[qSort.key] ?? ""; const y = b[qSort.key] ?? "";
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }, [qs, qSearch, qStage, qMode, qSort]);

  const stageRows = useMemo(() => {
    const dir = sSort.dir === "asc" ? 1 : -1;
    return [...(data?.stages ?? [])].sort((a, b) => {
      const x = a[sSort.key] ?? ""; const y = b[sSort.key] ?? "";
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }, [data, sSort]);


  const resetPage = <T,>(set: (v: T) => void) => (v: T) => { set(v); setVPage(0); };

  return (
    <PageFrame variant="split">
        {/* Sticky header + tabs */}
        <PageFrameHeader sticky style={{ paddingBottom: 0 }}>
          <PageHeader
            title={t("title")}
            description={
              <>
                {t("description")} {t("newHere")}{" "}
                <a onClick={() => setTab("guide")} style={{ color: K.accentDeep, fontWeight: 600, cursor: "pointer" }}>{t("guide")}</a>.
              </>
            }
            actions={
              <>
                <Select value={days} onChange={e => { setDays(Number(e.target.value)); setVPage(0); }} style={input} title={t("windowTitle")}>
                  <option value={1}>{t("last24h")}</option><option value={7}>{t("last7")}</option><option value={30}>{t("last30")}</option><option value={90}>{t("last90")}</option>
                </Select>
                <Button onClick={() => { loadSummary(); if (tab === "verdicts") loadLog(); }}><RefreshCw size={13} /> {t("refresh")}</Button>
              </>
            }
          />
          <Tabs
            variant="underline"
            ariaLabel={t("title")}
            style={{ marginTop: 12 }}
            active={tab}
            onChange={(k) => setTab(k as Tab)}
            tabs={[
              { key: "overview", label: t("tabOverview") },
              { key: "questions", label: t("tabQuestions", qs.length) },
              { key: "verdicts", label: t("tabVerdicts") },
              { key: "guide", label: t("guide") },
            ]}
          />
        </PageFrameHeader>

        <PageFrameBody>
          {error && <Section style={{ marginBottom: 16 }} bodyStyle={{ color: K.danger }}>{error}</Section>}
          {!data && !error && tab !== "guide" && <LoadingScreen message={t("loading")} />}

          {tab === "guide" && <Guide />}

          {data && tab === "overview" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12, marginBottom: 18 }}>
                <Kpi label={t("kpiCalls", data.days)} value={data.total_calls.toLocaleString()} sub={t("kpiCallsSub")} />
                <Kpi label={t("kpiCost", data.days)} value={usd(data.total_cost_usd)} sub={t("kpiCostSub")} />
                <Kpi label={t("kpiVerdicts")} value={verdictTotal.toLocaleString()} sub={t("kpiVerdictsSub", (data.stages ?? []).reduce((n, s) => n + (s.cached ?? 0), 0))} />
                <Kpi label={t("kpiActed")} value={acted.toLocaleString()} sub={t("kpiActedSub", pct(acted, verdictTotal))} color={acted ? toneText(MODE_META.enforce.tone) : K.ink} />
                <Kpi label={t("kpiErrors")} value={totals.error.toLocaleString()} sub={t("kpiErrorsSub", pct(totals.error, verdictTotal))} color={totals.error ? toneText(ZONE_META.error.tone) : K.ink} />
                <Kpi label={t("kpiLatency")} value={avgLatency != null ? `${Math.round(avgLatency)} ms` : "—"} sub={t("kpiLatencySub")} />
                <Kpi label={t("kpiQuestions")} value={`${qs.length}`} sub={t("kpiQuestionsSub", qs.filter(q => q.mode === "enforce").length, qs.filter(q => q.mode === "shadow").length, qs.filter(q => q.mode === "off").length)} />
                <Kpi label={t("kpiAllowlist")} value={`${data.tenant_allowlist.length}`} sub={data.tenant_allowlist.map(a => a.slug).join(", ") || t("platformOnly")} />
              </div>

              <Section style={{ marginBottom: 18 }}>
                <Eyebrow>{t("howFell", data.days)}</Eyebrow>
                <ZoneBar counts={totals} />
              </Section>

              <Section style={{ marginBottom: 18 }}>
                <Eyebrow>{t("perDay")}</Eyebrow>
                {(data.daily ?? []).length === 0 ? <div style={{ fontSize: 12.5, color: K.muted }}>{t("noVerdictsWindow")}</div> : (
                  <div style={{ height: 240 }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={data.daily ?? []}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.line} />
                        <XAxis dataKey="day" fontSize={11} /><YAxis fontSize={11} allowDecimals={false} />
                        <Tooltip {...CHART_TOOLTIP} />
                        <Legend />
                        {ZONES.map(z => <Bar key={z} dataKey={z} name={t(ZONE_META[z].label)} stackId="z" fill={chartColors[z]} />)}
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </Section>

              <Eyebrow>{t("byStage")}</Eyebrow>
              <div style={{ marginBottom: 12 }}>
                <DataTable<StageRow>
                  tableId="decisions-by-stage"
                  data={stageRows}
                  getRowId={(r) => r.stage}
                  searchable={false}
                  paginate={false}
                  compact
                  emptyTitle={t("noStage")}
                  {...tableSort(sSort, setSSort)}
                  columns={[
                    dcol<StageRow>("stage", t("colStage"), (r) => r.stage, { sortable: true, title: t("colStageTitle"), style: () => ({ fontFamily: mono, fontWeight: 700 }) }),
                    dcol<StageRow>("questions", t("colQuestions"), (r) => n0(r.questions), { title: t("colQuestionsTitle") }),
                    dcol<StageRow>("verdicts", t("colVerdicts"), (r) => n0(r.verdicts).toLocaleString(), { sortable: true }),
                    dcol<StageRow>("acted", t("colActed"), (r) => n0(r.acted), { sortable: true, title: t("colActedTitle"),
                      style: (r) => ({ fontWeight: 700, color: r.acted ? toneText(MODE_META.enforce.tone) : K.muted }) }),
                    dcol<StageRow>("errors", t("colErrors"), (r) => (
                      <>{n0(r.errors)} <span style={{ color: K.muted }}>({pct(n0(r.errors), n0(r.verdicts))})</span></>
                    ), { sortable: true, style: (r) => ({ color: r.errors ? toneText(ZONE_META.error.tone) : K.muted }) }),
                    dcol<StageRow>("skipped", t("colSkipped"), (r) => n0(r.skipped)),
                    dcol<StageRow>("calls", t("colCalls"), (r) => (data.calls_by_stage ?? []).find(c => c.stage === r.stage)?.calls ?? "—", { title: t("colCallsTitle") }),
                    dcol<StageRow>("cost_usd", t("colCost"), (r) => usd((data.calls_by_stage ?? []).find(c => c.stage === r.stage)?.cost_usd ?? r.cost_usd),
                      { sortable: true, style: () => ({ fontFamily: mono }) }),
                    dcol<StageRow>("avg_latency_ms", t("colAvgLatency"), (r) => (r.avg_latency_ms != null ? `${Math.round(r.avg_latency_ms)} ms` : "—"),
                      { sortable: true, style: () => ({ fontFamily: mono }) }),
                    dcol<StageRow>("last_used_at", t("colLastUsed"), (r) => when(r.last_used_at), { sortable: true, style: () => ({ whiteSpace: "nowrap" }) }),
                  ]}
                />
              </div>
              <div style={{ fontSize: 11.5, color: K.muted }}>
                {t("adhocA")} <span style={{ fontFamily: mono }}>adhoc_</span> {t("adhocB")}
              </div>
            </>
          )}

          {data && tab === "questions" && (
            <>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                <Input value={qSearch} onChange={e => setQSearch(e.target.value)} placeholder={t("searchQuestion")} style={{ ...input, width: 220 }} />
                <Select value={qStage} onChange={e => setQStage(e.target.value)} style={input}><option value="">{t("allStages")}</option>{stagesAll.map(s => <option key={s} value={s}>{s}</option>)}</Select>
                <Select value={qMode} onChange={e => setQMode(e.target.value)} style={input}><option value="">{t("allModes")}</option><option value="off">{t("modeOff")}</option><option value="shadow">{t("modeShadow")}</option><option value="enforce">{t("modeEnforce")}</option></Select>
                <span style={{ fontSize: 12, color: K.muted, alignSelf: "center" }}>{t("qShown", filteredQs.length, qs.length)}</span>
              </div>
              <DataTable<Question>
                tableId="decisions-questions"
                data={filteredQs}
                getRowId={(q) => q.question_key}
                onRowClick={(q) => setOpen(open === q.question_key ? null : q.question_key)}
                searchable={false}
                paginate={false}
                compact
                maxBodyHeight={TABLE_BOX_HEIGHT}
                emptyTitle={t("noQuestions")}
                {...tableSort(qSort, setQSort)}
                columns={[
                  dcol<Question>("stage", t("colStage"), (q) => q.stage, { sortable: true, style: () => ({ fontFamily: mono }) }),
                  { ...dcol<Question>("question_key", t("colQuestion"), (q) => <span title={q.instructions}>{q.question_key}</span>,
                    { sortable: true, style: () => ({ fontFamily: mono, fontWeight: 700 }) }) },
                  dcol<Question>("kind", t("colType"), (q) => <span title={KIND_HELP[q.kind] ? t(KIND_HELP[q.kind]) : undefined}>{q.kind}</span>, { title: t("colTypeTitle") }),
                  dcol<Question>("mode", t("mode"), (q) => <ModePill m={q.mode} />, { sortable: true, title: t("colModeTitle") }),
                  dcol<Question>("accept_floor", t("colAcceptAt"), (q) => num(q.accept_floor), { title: t("colAcceptAtTitle"), style: () => ({ fontFamily: mono }) }),
                  dcol<Question>("reject_ceiling", t("colRejectAt"), (q) => num(q.reject_ceiling), { title: t("colRejectAtTitle"), style: () => ({ fontFamily: mono }) }),
                  dcol<Question>("calibrated", t("colCalibrated"), (q) => (q.calibration_ref
                    ? <Pill tone="success">{t("yes")}</Pill> : <Pill tone="neutral">{t("no")}</Pill>)),
                  dcol<Question>("verdicts", t("colVerdicts"), (q) => q.verdicts, { sortable: true, style: () => ({ fontWeight: 700 }) }),
                  dcol<Question>("cached", t("colCached"), (q) => q.cached ?? 0, { title: t("colCachedTitle") }),
                  dcol<Question>("accept", t("zoneAccept"), (q) => q.accept, { style: () => ({ color: toneText("success"), fontWeight: 600 }) }),
                  dcol<Question>("grey", t("zoneGrey"), (q) => q.grey),
                  dcol<Question>("reject", t("zoneReject"), (q) => q.reject, { sortable: true, style: () => ({ color: toneText("danger"), fontWeight: 600 }) }),
                  dcol<Question>("error", t("colError"), (q) => q.error, { sortable: true, style: (q) => ({ color: q.error ? toneText("warning") : undefined }) }),
                  dcol<Question>("skipped", t("colSkipped"), (q) => q.skipped),
                  dcol<Question>("acted", t("colActed"), (q) => q.acted, { sortable: true,
                    style: (q) => ({ fontWeight: 700, color: q.acted ? toneText(MODE_META.enforce.tone) : K.muted }) }),
                  dcol<Question>("avg_probability", t("colAvgP"), (q) => num(q.avg_probability), { title: t("colAvgPTitle"), style: () => ({ fontFamily: mono }) }),
                  dcol<Question>("avg_latency_ms", t("colLatencyP"), (q) => (q.p50_latency_ms != null
                    ? `${Math.round(q.p50_latency_ms)} / ${Math.round(q.p95_latency_ms ?? 0)} ms` : "—"),
                    { sortable: true, style: () => ({ fontFamily: mono, whiteSpace: "nowrap" }) }),
                  dcol<Question>("cost_usd", t("colCost"), (q) => usd(q.cost_usd), { sortable: true, style: () => ({ fontFamily: mono }) }),
                  dcol<Question>("last_used_at", t("colLastUsed"), (q) => when(q.last_used_at), { sortable: true, style: () => ({ whiteSpace: "nowrap" }) }),
                ]}
              />
              {openQ && (() => { const q = openQ; return (
                <Section style={{ marginTop: 14, borderLeft: `4px solid ${K.accent}` }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                    <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 14 }}>{q.question_key}</span>
                    <ModePill m={q.mode} /><span style={{ fontSize: 12, color: K.muted }}>{t("stage", q.stage)}</span>
                    <span style={{ flex: 1 }} /><Button size="sm" onClick={() => setOpen(null)}>{t("close")}</Button>
                  </div>
<div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))", gap: 18 }}>
                              <div>
                                <Eyebrow>{t("questionAsked")}</Eyebrow>
                                <div style={{ fontSize: 13.5, fontWeight: 600, color: K.ink }}>{q.instructions}</div>
                                <div style={{ fontSize: 12, color: K.muted, marginTop: 4 }}>{KIND_HELP[q.kind] ? t(KIND_HELP[q.kind]) : null}</div>
                                {q.criteria != null && (
                                  <>
                                    <Eyebrow style={{ marginTop: 12 }}>{q.kind === "noul" ? t("meaningYesNo") : t("options")}</Eyebrow>
                                    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
                                      {Object.entries(q.criteria as Record<string, unknown>).map(([k, v]) => <li key={k}><b>{k}</b>{v ? ` — ${String(v)}` : ""}</li>)}
                                    </ul>
                                  </>
                                )}
                                {/* AA-994: the seeded notes (migrations 181-211) open with their issue reference; it is taken out here. */}
                                {q.notes && <div style={{ fontSize: 12, color: K.muted, marginTop: 10 }}>{stripIssueIds(q.notes)}</div>}
                              </div>
                              <div>
                                <Eyebrow>{t("verdictsDays", data.days)}</Eyebrow>
                                <ZoneBar counts={{ accept: q.accept, grey: q.grey, reject: q.reject, error: q.error, skipped: q.skipped }} />
                                <div style={{ fontSize: 12, color: K.muted, marginTop: 10 }}>
                                  {t("floorsLine", q.threshold_version, q.calibration_ref ?? t("noCalibration"), when(q.first_used_at), when(q.updated_at), q.updated_by)}
                                </div>
                                <Button size="sm" style={{ marginTop: 10 }} onClick={() => { setVQuestion(q.question_key); setVPage(0); setTab("verdicts"); }}>{t("seeVerdicts")}</Button>
                              </div>
                            </div>
                            <div style={{ marginTop: 16 }}><Eyebrow>{t("settings")}</Eyebrow><QuestionEditor q={q} onSaved={() => { setOpen(null); loadSummary(); }} /></div>
                                          </Section>
              ); })()}
            </>
          )}

          {data && tab === "verdicts" && (
            <>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                <Select value={vStage} onChange={e => resetPage(setVStage)(e.target.value)} style={input}><option value="">{t("allStages")}</option>{stagesAll.map(s => <option key={s} value={s}>{s}</option>)}</Select>
                <Select value={vQuestion} onChange={e => resetPage(setVQuestion)(e.target.value)} style={input}><option value="">{t("allQuestions")}</option>{qs.map(q => <option key={q.question_key} value={q.question_key}>{q.question_key}</option>)}</Select>
                <Select value={vZone} onChange={e => resetPage(setVZone)(e.target.value)} style={input}><option value="">{t("allZones")}</option>{ZONES.map(z => <option key={z} value={z}>{t(ZONE_META[z].label)}</option>)}</Select>
                <Select value={vMode} onChange={e => resetPage(setVMode)(e.target.value)} style={input}><option value="">{t("allModes")}</option><option value="off">{t("modeOff")}</option><option value="shadow">{t("modeShadow")}</option><option value="enforce">{t("modeEnforce")}</option></Select>
                <Select value={vTenant} onChange={e => resetPage(setVTenant)(e.target.value)} style={input}>
                  <option value="">{t("allContent")}</option><option value="platform">{t("platform")}</option>
                  {data.tenant_allowlist.map(a => <option key={a.slug} value={a.slug}>{a.slug}</option>)}
                </Select>
                <Input value={vSubject} onChange={e => resetPage(setVSubject)(e.target.value)} placeholder={t("subjectContains")} style={{ ...input, width: 200 }} />
              </div>
              <DataTable<Verdict>
                tableId="decisions-verdicts"
                data={log.rows}
                getRowId={(v) => String(v.id)}
                searchable={false}
                paginate={false}
                compact
                maxBodyHeight={TABLE_BOX_HEIGHT}
                emptyTitle={t("noVerdictsFilters")}
                {...tableSort(vSort, (next) => { setVSort(next); setVPage(0); })}
                columns={[
                  dcol<Verdict>("created_at", t("colTime"), (v) => when(v.created_at), { sortable: true, style: () => ({ whiteSpace: "nowrap" }) }),
                  dcol<Verdict>("stage", t("colStage"), (v) => v.stage, { style: () => ({ fontFamily: mono }) }),
                  dcol<Verdict>("question", t("colQuestion"), (v) => v.question_key, { style: () => ({ fontFamily: mono }) }),
                  dcol<Verdict>("subject", t("colSubject"), (v) => <span title={v.subject_key}>{v.subject_key}</span>, { title: t("colSubjectTitle"),
                    style: () => ({ display: "inline-block", maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", verticalAlign: "bottom" }) }),
                  dcol<Verdict>("content", t("colContent"), (v) => v.tenant_slug ?? t("platformLower"), { title: t("colContentTitle") }),
                  dcol<Verdict>("mode", t("mode"), (v) => <ModePill m={v.mode} />),
                  dcol<Verdict>("zone", t("colZone"), (v) => <ZonePill z={v.zone} />),
                  dcol<Verdict>("probability", t("colProbability"), (v) => (v.choice ? `${v.choice} (${num(v.probability)})` : num(v.probability, 3)),
                    { sortable: true, style: () => ({ fontFamily: mono, fontWeight: 700 }) }),
                  dcol<Verdict>("acted", t("colActed"), (v) => (v.mode === "enforce" && (v.zone === "accept" || v.zone === "reject") ? t("yes") : t("no")),
                    { title: t("colActedVTitle"), style: (v) => ({ fontWeight: 700,
                      color: v.mode === "enforce" && (v.zone === "accept" || v.zone === "reject") ? toneText(MODE_META.enforce.tone) : K.muted }) }),
                  dcol<Verdict>("latency_ms", t("colLatency"), (v) => (v.latency_ms != null ? `${v.latency_ms} ms` : "—"), { sortable: true, style: () => ({ fontFamily: mono }) }),
                  dcol<Verdict>("cost_usd", t("colCost"), (v) => usd(v.cost_usd ?? 0), { sortable: true, style: () => ({ fontFamily: mono }) }),
                  dcol<Verdict>("note", t("colNote"), (v) => v.error ?? v.outcome ?? "", { style: () => ({ fontSize: 11.5, color: K.muted }) }),
                  // AA-804 UI part 3: the job that asked, opened as its AI trace (every paid call of the job).
                  dcol<Verdict>("job", t("colJob"), (v) => (v.job_id ? (
                    <a href={traceHref(v.job_id)} title={t("openTraceTitle")} data-testid="decision-open-trace"
                      style={{ color: K.accent }}>{v.job_id.slice(0, 8)}</a>
                  ) : <span style={{ color: K.muted }}>—</span>), { title: t("colJobTitle"), style: () => ({ fontFamily: mono, whiteSpace: "nowrap" }) }),
                ]}
              />
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10, fontSize: 12.5 }}>
                <span style={{ color: K.muted }}>
                  {log.total ? t("range", vPage * PAGE + 1, Math.min((vPage + 1) * PAGE, log.total), log.total.toLocaleString()) : t("zeroVerdicts")}
                </span>
                <Button size="sm" disabled={vPage === 0} onClick={() => setVPage(p => p - 1)}>{t("prev")}</Button>
                <Button size="sm" disabled={(vPage + 1) * PAGE >= log.total} onClick={() => setVPage(p => p + 1)}>{t("next")}</Button>
              </div>
            </>
          )}

          {tab !== "guide" && (
            <div style={{ marginTop: 22, fontSize: 12, color: K.muted, display: "flex", alignItems: "center", gap: 6 }}>
              <BookOpen size={13} /> {t("colours")} {ZONES.map(z => <ZonePill key={z} z={z} />)} {t("modes")} {(["off", "shadow", "enforce"] as Mode[]).map(m => <ModePill key={m} m={m} />)}
            </div>
          )}
        </PageFrameBody>
      </PageFrame>
  );
}
