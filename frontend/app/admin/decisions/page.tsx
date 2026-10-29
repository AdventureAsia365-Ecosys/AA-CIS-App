"use client";
// app/admin/decisions/page.tsx — AA-660 Jev (TypeSafe) decision layer, v2 (S203 feedback: explain every
// term, filters + sort, sticky tabs/headers, one column per fact, solid status colours, fuller stats).
// API: /admin/decisions/{summary,log,questions/{key}}. Jev spend also appears in Cost Operations (Jev tab).
// AA-994: text in the UI language (messages.ts); stage / question keys, question texts and API text stay as sent.

import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { A, sans, mono, Card, SLabel, Btn, LoadingScreen, TH, TD, CHART_TOOLTIP } from "../_components/adminUi";
import { AdminMain, AdminPaneBody, AdminPaneHeader, PageHeader, useFormat, useT } from "../../_kit";
import { useChartColors } from "../../_kit/useTheme";
import { stripIssueIds } from "@/lib/issueIds";
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
const ZONE_META: Record<Zone, { label: "zoneAccept" | "zoneGrey" | "zoneReject" | "zoneError" | "zoneSkipped"; color: string; cssVar: string }> = {
  accept:  { label: "zoneAccept",  color: "var(--aa-green-deep)",  cssVar: "--aa-green-deep" },
  grey:    { label: "zoneGrey",    color: "var(--aa-neutral-fg2)", cssVar: "--aa-neutral-fg2" },
  reject:  { label: "zoneReject",  color: "var(--aa-red-strong)",  cssVar: "--aa-red-strong" },
  error:   { label: "zoneError",   color: "var(--aa-amber-strong)",cssVar: "--aa-amber-strong" },
  skipped: { label: "zoneSkipped", color: "var(--aa-blue-link)",   cssVar: "--aa-blue-link" },
};
const MODE_META: Record<Mode, { label: "modeOff" | "modeShadow" | "modeEnforce"; color: string }> = {
  off:     { label: "modeOff",     color: "var(--aa-neutral-fg)" },
  shadow:  { label: "modeShadow",  color: "var(--aa-blue-link)" },
  enforce: { label: "modeEnforce", color: "var(--aa-amber-strong)" },
};
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

function Pill({ color, children, title }: { color: string; children: React.ReactNode; title?: string }) {
  return (
    <span title={title} style={{
      display: "inline-block", padding: "3px 10px", borderRadius: 999, background: color, color: "var(--aa-on-solid)",
      fontSize: 11, fontWeight: 700, letterSpacing: "0.03em", whiteSpace: "nowrap", cursor: title ? "help" : "default",
    }}>{children}</span>
  );
}
function ZonePill({ z }: { z: Zone }) {
  const t = useT(DEC);
  const k = ZONE_META[z].label;
  return <Pill color={ZONE_META[z].color} title={t(`${k}Help`)}>{t(k)}</Pill>;
}
function ModePill({ m }: { m: Mode }) {
  const t = useT(DEC);
  const k = MODE_META[m].label;
  return <Pill color={MODE_META[m].color} title={t(`${k}Help`)}>{t(k)}</Pill>;
}

function Kpi({ label, value, sub, color = A.ink }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{ background: A.card, border: `1px solid ${A.line}`, borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: A.muted }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, color, marginTop: 4, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: A.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

const STICKY_TH: React.CSSProperties = { ...TH, position: "sticky", top: 0, zIndex: 2, background: "var(--aa-bg)", whiteSpace: "nowrap" };
const TABLE_BOX: React.CSSProperties = { overflow: "auto", maxHeight: "62vh", border: `1px solid ${A.line}`, borderRadius: 10, background: A.card };
const input: React.CSSProperties = { padding: "7px 9px", border: `1px solid ${A.line}`, borderRadius: 7, fontSize: 12.5, fontFamily: sans, background: A.card };

function SortTh<K extends string>({ label, k, sort, setSort, title }: {
  label: string; k: K; sort: { key: K; dir: "asc" | "desc" }; setSort: (s: { key: K; dir: "asc" | "desc" }) => void; title?: string;
}) {
  const active = sort.key === k;
  return (
    <th title={title} style={{ ...STICKY_TH, cursor: "pointer", color: active ? A.ink : undefined }}
      onClick={() => setSort({ key: k, dir: active && sort.dir === "desc" ? "asc" : "desc" })}>
      {label}{active ? (sort.dir === "desc" ? " ▼" : " ▲") : ""}
    </th>
  );
}

function ZoneBar({ counts }: { counts: Record<Zone, number> }) {
  const t = useT(DEC);
  const total = ZONES.reduce((n, z) => n + counts[z], 0);
  if (!total) return <span style={{ color: A.muted2, fontSize: 12 }}>{t("noVerdictsYet")}</span>;
  return (
    <div>
      <div style={{ display: "flex", height: 12, borderRadius: 6, overflow: "hidden", background: A.line }}>
        {ZONES.map(z => counts[z] ? <div key={z} title={`${t(ZONE_META[z].label)}: ${counts[z]}`}
          style={{ width: `${(counts[z] / total) * 100}%`, background: ZONE_META[z].color }} /> : null)}
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 6, fontSize: 12 }}>
        {ZONES.map(z => (
          <span key={z} style={{ display: "inline-flex", alignItems: "center", gap: 5 }} title={t(`${ZONE_META[z].label}Help`)}>
            <span style={{ width: 10, height: 10, borderRadius: 3, background: ZONE_META[z].color }} />
            {t(ZONE_META[z].label)} <b>{counts[z]}</b> <span style={{ color: A.muted }}>({pct(counts[z], total)})</span>
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
    <div style={{ padding: "10px 0", borderTop: `1px solid ${A.line}` }}>
      <div style={{ fontWeight: 700, color: A.ink, fontSize: 13 }}>{name}</div>
      <div style={{ fontSize: 12.5, color: A.body, marginTop: 3, lineHeight: 1.55 }}>{d}</div>
    </div>
  );
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))", gap: 16 }}>
      <Card>
        <SLabel>{t("whatThisPageIs")}</SLabel>
        <p style={{ fontSize: 13, lineHeight: 1.6, margin: 0 }}>
          <b>Jev</b> {t("g1a")} <i>{t("g1q")}</i> {t("g1b")} <b>{t("g1c")}</b>{t("g1d")} <b>{t("g1e")}</b>{t("g1f")}
        </p>
        <SLabel style={{ marginTop: 14 }}>{t("howAction")}</SLabel>
        <ol style={{ fontSize: 12.5, lineHeight: 1.7, margin: 0, paddingLeft: 18 }}>
          <li>{t("li1")}</li>
          <li>{t("li2a")} <b>{t("li2b")}</b> {t("li2c")} <ZonePill z="accept" />{t("li2d")} <ZonePill z="reject" />{t("li2e")} <ZonePill z="grey" />.</li>
          <li>{t("li3a")} <ModePill m="enforce" /> {t("li3b")} {t("li3d")}</li>
        </ol>
      </Card>
      <Card>
        <SLabel>{t("terms")}</SLabel>
        {term(t("termStage"), t("termStageHelp"))}
        {term(t("termQuestion"), t("termQuestionHelp"))}
        {term(t("termType"), Object.entries(KIND_HELP).map(([k, v]) => <div key={k}><b>{k}</b>: {t(v)}</div>))}
        {term(t("termMode"), (["off", "shadow", "enforce"] as Mode[]).map(m => <div key={m} style={{ marginTop: 3 }}><ModePill m={m} /> {t(`${MODE_META[m].label}Help`)}</div>))}
      </Card>
      <Card>
        <SLabel>{t("zonesTitle")}</SLabel>
        {ZONES.map(z => <div key={z} style={{ padding: "6px 0" }}><ZonePill z={z} /> <span style={{ fontSize: 12.5 }}>{t(`${ZONE_META[z].label}Help`)}</span></div>)}
        {term(t("termFloors"), t("termFloorsHelp"))}
        {term(t("termCalibration"), t("termCalibrationHelp"))}
        {term(t("termVerdict"), t("termVerdictHelp"))}
        {term(t("termCached"), t("termCachedHelp"))}
        {term(t("termActed"), t("termActedHelp"))}
        {term(t("termCost"), t("termCostHelp"))}
      </Card>
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
      <label style={{ fontSize: 11.5, color: A.muted }}>{t("mode")}<br />
        <select value={mode} onChange={e => setMode(e.target.value as Mode)} style={input}>
          <option value="off">{t("modeOff")}</option><option value="shadow">{t("modeShadow")}</option><option value="enforce">{t("modeEnforce")}</option>
        </select>
      </label>
      <label style={{ fontSize: 11.5, color: A.muted }}>{t("acceptFloor")}<br /><input value={accept} onChange={e => setAccept(e.target.value)} placeholder="0.95" style={{ ...input, width: 90 }} /></label>
      <label style={{ fontSize: 11.5, color: A.muted }}>{t("rejectCeiling")}<br /><input value={reject} onChange={e => setReject(e.target.value)} placeholder="0.30" style={{ ...input, width: 90 }} /></label>
      <label style={{ fontSize: 11.5, color: A.muted, flex: 1, minWidth: 240 }}>{t("calibrationRecord")}<br />
        <input value={calib} onChange={e => setCalib(e.target.value)} placeholder="docs/calibration/<question>.md" style={{ ...input, width: "100%" }} />
      </label>
      <Btn variant="primary" onClick={save} disabled={saving}>{saving ? t("saving") : t("save")}</Btn>
      {err && <div style={{ width: "100%", color: A.red, fontSize: 12 }}>{err}</div>}
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

  const tabBtn = (key: Tab, label: string) => (
    <button onClick={() => setTab(key)} style={{
      padding: "9px 16px", border: "none", borderBottom: `3px solid ${tab === key ? A.accent : "transparent"}`,
      background: "none", fontFamily: sans, fontSize: 13.5, fontWeight: tab === key ? 700 : 500,
      color: tab === key ? A.ink : A.muted, cursor: "pointer", whiteSpace: "nowrap", flexShrink: 0,
    }}>{label}</button>
  );
  const resetPage = <T,>(set: (v: T) => void) => (v: T) => { set(v); setVPage(0); };

  return (
    <AdminMain variant="split">
        {/* Sticky header + tabs */}
        <AdminPaneHeader sticky style={{ paddingBottom: 0 }}>
          <PageHeader
            title={t("title")}
            description={
              <>
                {t("description")} {t("newHere")}{" "}
                <a onClick={() => setTab("guide")} style={{ color: A.accentDeep, fontWeight: 600, cursor: "pointer" }}>{t("guide")}</a>.
              </>
            }
            actions={
              <>
                <select value={days} onChange={e => { setDays(Number(e.target.value)); setVPage(0); }} style={input} title={t("windowTitle")}>
                  <option value={1}>{t("last24h")}</option><option value={7}>{t("last7")}</option><option value={30}>{t("last30")}</option><option value={90}>{t("last90")}</option>
                </select>
                <Btn onClick={() => { loadSummary(); if (tab === "verdicts") loadLog(); }}><RefreshCw size={13} /> {t("refresh")}</Btn>
              </>
            }
          />
          <div style={{ display: "flex", gap: 4, marginTop: 12, maxWidth: "100%", overflowX: "auto" }}>
            {tabBtn("overview", t("tabOverview"))}{tabBtn("questions", t("tabQuestions", qs.length))}{tabBtn("verdicts", t("tabVerdicts"))}{tabBtn("guide", t("guide"))}
          </div>
        </AdminPaneHeader>

        <AdminPaneBody>
          {error && <Card style={{ color: A.red, marginBottom: 16 }}>{error}</Card>}
          {!data && !error && tab !== "guide" && <LoadingScreen msg={t("loading")} />}

          {tab === "guide" && <Guide />}

          {data && tab === "overview" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12, marginBottom: 18 }}>
                <Kpi label={t("kpiCalls", data.days)} value={data.total_calls.toLocaleString()} sub={t("kpiCallsSub")} />
                <Kpi label={t("kpiCost", data.days)} value={usd(data.total_cost_usd)} sub={t("kpiCostSub")} />
                <Kpi label={t("kpiVerdicts")} value={verdictTotal.toLocaleString()} sub={t("kpiVerdictsSub", (data.stages ?? []).reduce((n, s) => n + (s.cached ?? 0), 0))} />
                <Kpi label={t("kpiActed")} value={acted.toLocaleString()} sub={t("kpiActedSub", pct(acted, verdictTotal))} color={acted ? MODE_META.enforce.color : A.ink} />
                <Kpi label={t("kpiErrors")} value={totals.error.toLocaleString()} sub={t("kpiErrorsSub", pct(totals.error, verdictTotal))} color={totals.error ? ZONE_META.error.color : A.ink} />
                <Kpi label={t("kpiLatency")} value={avgLatency != null ? `${Math.round(avgLatency)} ms` : "—"} sub={t("kpiLatencySub")} />
                <Kpi label={t("kpiQuestions")} value={`${qs.length}`} sub={t("kpiQuestionsSub", qs.filter(q => q.mode === "enforce").length, qs.filter(q => q.mode === "shadow").length, qs.filter(q => q.mode === "off").length)} />
                <Kpi label={t("kpiAllowlist")} value={`${data.tenant_allowlist.length}`} sub={data.tenant_allowlist.map(a => a.slug).join(", ") || t("platformOnly")} />
              </div>

              <Card style={{ marginBottom: 18 }}>
                <SLabel>{t("howFell", data.days)}</SLabel>
                <ZoneBar counts={totals} />
              </Card>

              <Card style={{ marginBottom: 18 }}>
                <SLabel>{t("perDay")}</SLabel>
                {(data.daily ?? []).length === 0 ? <div style={{ fontSize: 12.5, color: A.muted }}>{t("noVerdictsWindow")}</div> : (
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
              </Card>

              <SLabel>{t("byStage")}</SLabel>
              <div style={{ ...TABLE_BOX, maxHeight: "none", marginBottom: 12 }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                  <thead><tr>
                    <SortTh label={t("colStage")} k="stage" sort={sSort} setSort={setSSort} title={t("colStageTitle")} />
                    <th style={STICKY_TH} title={t("colQuestionsTitle")}>{t("colQuestions")}</th>
                    <SortTh label={t("colVerdicts")} k="verdicts" sort={sSort} setSort={setSSort} />
                    <SortTh label={t("colActed")} k="acted" sort={sSort} setSort={setSSort} title={t("colActedTitle")} />
                    <SortTh label={t("colErrors")} k="errors" sort={sSort} setSort={setSSort} />
                    <th style={STICKY_TH}>{t("colSkipped")}</th>
                    <th style={STICKY_TH} title={t("colCallsTitle")}>{t("colCalls")}</th>
                    <SortTh label={t("colCost")} k="cost_usd" sort={sSort} setSort={setSSort} />
                    <SortTh label={t("colAvgLatency")} k="avg_latency_ms" sort={sSort} setSort={setSSort} />
                    <SortTh label={t("colLastUsed")} k="last_used_at" sort={sSort} setSort={setSSort} />
                  </tr></thead>
                  <tbody>
                    {stageRows.map(s => {
                      const calls = (data.calls_by_stage ?? []).find(c => c.stage === s.stage);
                      return (
                        <tr key={s.stage}>
                          <td style={{ ...TD, fontFamily: mono, fontWeight: 700 }}>{s.stage}</td>
                          <td style={TD}>{n0(s.questions)}</td>
                          <td style={TD}>{n0(s.verdicts).toLocaleString()}</td>
                          <td style={{ ...TD, fontWeight: 700, color: s.acted ? MODE_META.enforce.color : A.muted }}>{n0(s.acted)}</td>
                          <td style={{ ...TD, color: s.errors ? ZONE_META.error.color : A.muted }}>{n0(s.errors)} <span style={{ color: A.muted }}>({pct(n0(s.errors), n0(s.verdicts))})</span></td>
                          <td style={TD}>{n0(s.skipped)}</td>
                          <td style={TD}>{calls?.calls ?? "—"}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{usd(calls?.cost_usd ?? s.cost_usd)}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{s.avg_latency_ms != null ? `${Math.round(s.avg_latency_ms)} ms` : "—"}</td>
                          <td style={{ ...TD, whiteSpace: "nowrap" }}>{when(s.last_used_at)}</td>
                        </tr>
                      );
                    })}
                    {!stageRows.length && <tr><td style={TD} colSpan={10}>{t("noStage")}</td></tr>}
                  </tbody>
                </table>
              </div>
              <div style={{ fontSize: 11.5, color: A.muted }}>
                {t("adhocA")} <span style={{ fontFamily: mono }}>adhoc_</span> {t("adhocB")}
              </div>
            </>
          )}

          {data && tab === "questions" && (
            <>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                <input value={qSearch} onChange={e => setQSearch(e.target.value)} placeholder={t("searchQuestion")} style={{ ...input, width: 220 }} />
                <select value={qStage} onChange={e => setQStage(e.target.value)} style={input}><option value="">{t("allStages")}</option>{stagesAll.map(s => <option key={s} value={s}>{s}</option>)}</select>
                <select value={qMode} onChange={e => setQMode(e.target.value)} style={input}><option value="">{t("allModes")}</option><option value="off">{t("modeOff")}</option><option value="shadow">{t("modeShadow")}</option><option value="enforce">{t("modeEnforce")}</option></select>
                <span style={{ fontSize: 12, color: A.muted, alignSelf: "center" }}>{t("qShown", filteredQs.length, qs.length)}</span>
              </div>
              <div style={TABLE_BOX}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                  <thead><tr>
                    <th style={STICKY_TH} />
                    <SortTh label={t("colStage")} k="stage" sort={qSort} setSort={setQSort} />
                    <SortTh label={t("colQuestion")} k="question_key" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH} title={t("colTypeTitle")}>{t("colType")}</th>
                    <SortTh label={t("mode")} k="mode" sort={qSort} setSort={setQSort} title={t("colModeTitle")} />
                    <th style={STICKY_TH} title={t("colAcceptAtTitle")}>{t("colAcceptAt")}</th>
                    <th style={STICKY_TH} title={t("colRejectAtTitle")}>{t("colRejectAt")}</th>
                    <th style={STICKY_TH}>{t("colCalibrated")}</th>
                    <SortTh label={t("colVerdicts")} k="verdicts" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH} title={t("colCachedTitle")}>{t("colCached")}</th>
                    <th style={{ ...STICKY_TH, color: ZONE_META.accept.color }}>{t("zoneAccept")}</th>
                    <th style={STICKY_TH}>{t("zoneGrey")}</th>
                    <SortTh label={t("zoneReject")} k="reject" sort={qSort} setSort={setQSort} />
                    <SortTh label={t("colError")} k="error" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH}>{t("colSkipped")}</th>
                    <SortTh label={t("colActed")} k="acted" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH} title={t("colAvgPTitle")}>{t("colAvgP")}</th>
                    <SortTh label={t("colLatencyP")} k="avg_latency_ms" sort={qSort} setSort={setQSort} />
                    <SortTh label={t("colCost")} k="cost_usd" sort={qSort} setSort={setQSort} />
                    <SortTh label={t("colLastUsed")} k="last_used_at" sort={qSort} setSort={setQSort} />
                  </tr></thead>
                  <tbody>
                    {filteredQs.map(q => {
                      const isOpen = open === q.question_key;
                      return (
                        <tr key={q.question_key} onClick={() => setOpen(isOpen ? null : q.question_key)} style={{ cursor: "pointer", background: isOpen ? A.accentTint : undefined }}>
                          <td style={TD}>{isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{q.stage}</td>
                          <td style={{ ...TD, fontFamily: mono, fontWeight: 700 }} title={q.instructions}>{q.question_key}</td>
                          <td style={TD} title={KIND_HELP[q.kind] ? t(KIND_HELP[q.kind]) : undefined}>{q.kind}</td>
                          <td style={TD}><ModePill m={q.mode} /></td>
                          <td style={{ ...TD, fontFamily: mono }}>{num(q.accept_floor)}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{num(q.reject_ceiling)}</td>
                          <td style={TD}>{q.calibration_ref ? <Pill color="var(--aa-green-deep)">{t("yes")}</Pill> : <Pill color="var(--aa-muted2)">{t("no")}</Pill>}</td>
                          <td style={{ ...TD, fontWeight: 700 }}>{q.verdicts}</td>
                          <td style={TD}>{q.cached ?? 0}</td>
                          <td style={{ ...TD, color: ZONE_META.accept.color, fontWeight: 600 }}>{q.accept}</td>
                          <td style={TD}>{q.grey}</td>
                          <td style={{ ...TD, color: ZONE_META.reject.color, fontWeight: 600 }}>{q.reject}</td>
                          <td style={{ ...TD, color: q.error ? ZONE_META.error.color : undefined }}>{q.error}</td>
                          <td style={TD}>{q.skipped}</td>
                          <td style={{ ...TD, fontWeight: 700, color: q.acted ? MODE_META.enforce.color : A.muted }}>{q.acted}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{num(q.avg_probability)}</td>
                          <td style={{ ...TD, fontFamily: mono, whiteSpace: "nowrap" }}>{q.p50_latency_ms != null ? `${Math.round(q.p50_latency_ms)} / ${Math.round(q.p95_latency_ms ?? 0)} ms` : "—"}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{usd(q.cost_usd)}</td>
                          <td style={{ ...TD, whiteSpace: "nowrap" }}>{when(q.last_used_at)}</td>
                        </tr>
                      );
                    })}
                    {!filteredQs.length && <tr><td style={TD} colSpan={20}>{t("noQuestions")}</td></tr>}
                  </tbody>
                </table>
              </div>
              {openQ && (() => { const q = openQ; return (
                <Card style={{ marginTop: 14, borderLeft: `4px solid ${A.accent}` }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                    <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 14 }}>{q.question_key}</span>
                    <ModePill m={q.mode} /><span style={{ fontSize: 12, color: A.muted }}>{t("stage", q.stage)}</span>
                    <span style={{ flex: 1 }} /><Btn size="sm" onClick={() => setOpen(null)}>{t("close")}</Btn>
                  </div>
<div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))", gap: 18 }}>
                              <div>
                                <SLabel>{t("questionAsked")}</SLabel>
                                <div style={{ fontSize: 13.5, fontWeight: 600, color: A.ink }}>{q.instructions}</div>
                                <div style={{ fontSize: 12, color: A.muted, marginTop: 4 }}>{KIND_HELP[q.kind] ? t(KIND_HELP[q.kind]) : null}</div>
                                {q.criteria != null && (
                                  <>
                                    <SLabel style={{ marginTop: 12 }}>{q.kind === "noul" ? t("meaningYesNo") : t("options")}</SLabel>
                                    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
                                      {Object.entries(q.criteria as Record<string, unknown>).map(([k, v]) => <li key={k}><b>{k}</b>{v ? ` — ${String(v)}` : ""}</li>)}
                                    </ul>
                                  </>
                                )}
                                {/* AA-994: the seeded notes (migrations 181-211) open with their issue reference; it is taken out here. */}
                                {q.notes && <div style={{ fontSize: 12, color: A.muted, marginTop: 10 }}>{stripIssueIds(q.notes)}</div>}
                              </div>
                              <div>
                                <SLabel>{t("verdictsDays", data.days)}</SLabel>
                                <ZoneBar counts={{ accept: q.accept, grey: q.grey, reject: q.reject, error: q.error, skipped: q.skipped }} />
                                <div style={{ fontSize: 12, color: A.muted, marginTop: 10 }}>
                                  {t("floorsLine", q.threshold_version, q.calibration_ref ?? t("noCalibration"), when(q.first_used_at), when(q.updated_at), q.updated_by)}
                                </div>
                                <Btn size="sm" style={{ marginTop: 10 }} onClick={() => { setVQuestion(q.question_key); setVPage(0); setTab("verdicts"); }}>{t("seeVerdicts")}</Btn>
                              </div>
                            </div>
                            <div style={{ marginTop: 16 }}><SLabel>{t("settings")}</SLabel><QuestionEditor q={q} onSaved={() => { setOpen(null); loadSummary(); }} /></div>
                                          </Card>
              ); })()}
            </>
          )}

          {data && tab === "verdicts" && (
            <>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                <select value={vStage} onChange={e => resetPage(setVStage)(e.target.value)} style={input}><option value="">{t("allStages")}</option>{stagesAll.map(s => <option key={s} value={s}>{s}</option>)}</select>
                <select value={vQuestion} onChange={e => resetPage(setVQuestion)(e.target.value)} style={input}><option value="">{t("allQuestions")}</option>{qs.map(q => <option key={q.question_key} value={q.question_key}>{q.question_key}</option>)}</select>
                <select value={vZone} onChange={e => resetPage(setVZone)(e.target.value)} style={input}><option value="">{t("allZones")}</option>{ZONES.map(z => <option key={z} value={z}>{t(ZONE_META[z].label)}</option>)}</select>
                <select value={vMode} onChange={e => resetPage(setVMode)(e.target.value)} style={input}><option value="">{t("allModes")}</option><option value="off">{t("modeOff")}</option><option value="shadow">{t("modeShadow")}</option><option value="enforce">{t("modeEnforce")}</option></select>
                <select value={vTenant} onChange={e => resetPage(setVTenant)(e.target.value)} style={input}>
                  <option value="">{t("allContent")}</option><option value="platform">{t("platform")}</option>
                  {data.tenant_allowlist.map(a => <option key={a.slug} value={a.slug}>{a.slug}</option>)}
                </select>
                <input value={vSubject} onChange={e => resetPage(setVSubject)(e.target.value)} placeholder={t("subjectContains")} style={{ ...input, width: 200 }} />
              </div>
              <div style={TABLE_BOX}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                  <thead><tr>
                    <SortTh label={t("colTime")} k="created_at" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <th style={STICKY_TH}>{t("colStage")}</th>
                    <th style={STICKY_TH}>{t("colQuestion")}</th>
                    <th style={STICKY_TH} title={t("colSubjectTitle")}>{t("colSubject")}</th>
                    <th style={STICKY_TH} title={t("colContentTitle")}>{t("colContent")}</th>
                    <th style={STICKY_TH}>{t("mode")}</th>
                    <th style={STICKY_TH}>{t("colZone")}</th>
                    <SortTh label={t("colProbability")} k="probability" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <th style={STICKY_TH} title={t("colActedVTitle")}>{t("colActed")}</th>
                    <SortTh label={t("colLatency")} k="latency_ms" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <SortTh label={t("colCost")} k="cost_usd" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <th style={STICKY_TH}>{t("colNote")}</th>
                  </tr></thead>
                  <tbody>
                    {log.rows.map(v => {
                      const actedOn = v.mode === "enforce" && (v.zone === "accept" || v.zone === "reject");
                      return (
                        <tr key={v.id}>
                          <td style={{ ...TD, whiteSpace: "nowrap" }}>{when(v.created_at)}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{v.stage}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{v.question_key}</td>
                          <td style={{ ...TD, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={v.subject_key}>{v.subject_key}</td>
                          <td style={TD}>{v.tenant_slug ?? t("platformLower")}</td>
                          <td style={TD}><ModePill m={v.mode} /></td>
                          <td style={TD}><ZonePill z={v.zone} /></td>
                          <td style={{ ...TD, fontFamily: mono, fontWeight: 700 }}>{v.choice ? `${v.choice} (${num(v.probability)})` : num(v.probability, 3)}</td>
                          <td style={{ ...TD, fontWeight: 700, color: actedOn ? MODE_META.enforce.color : A.muted }}>{actedOn ? t("yes") : t("no")}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{v.latency_ms != null ? `${v.latency_ms} ms` : "—"}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{usd(v.cost_usd ?? 0)}</td>
                          <td style={{ ...TD, fontSize: 11.5, color: A.muted, maxWidth: 240 }}>{v.error ?? v.outcome ?? ""}</td>
                        </tr>
                      );
                    })}
                    {!log.rows.length && <tr><td style={TD} colSpan={12}>{t("noVerdictsFilters")}</td></tr>}
                  </tbody>
                </table>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10, fontSize: 12.5 }}>
                <span style={{ color: A.muted }}>
                  {log.total ? t("range", vPage * PAGE + 1, Math.min((vPage + 1) * PAGE, log.total), log.total.toLocaleString()) : t("zeroVerdicts")}
                </span>
                <Btn size="sm" disabled={vPage === 0} onClick={() => setVPage(p => p - 1)}>{t("prev")}</Btn>
                <Btn size="sm" disabled={(vPage + 1) * PAGE >= log.total} onClick={() => setVPage(p => p + 1)}>{t("next")}</Btn>
              </div>
            </>
          )}

          {tab !== "guide" && (
            <div style={{ marginTop: 22, fontSize: 12, color: A.muted, display: "flex", alignItems: "center", gap: 6 }}>
              <BookOpen size={13} /> {t("colours")} {ZONES.map(z => <ZonePill key={z} z={z} />)} {t("modes")} {(["off", "shadow", "enforce"] as Mode[]).map(m => <ModePill key={m} m={m} />)}
            </div>
          )}
        </AdminPaneBody>
      </AdminMain>
  );
}
